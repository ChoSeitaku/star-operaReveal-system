"""任务服务：创建（含单会话单任务校验）、取消、查询。

单飞约束的实现方式：在会话锁内做「检查 + 写入」的原子临界区，
检查条件是「该会话是否已存在 queued/running 任务」。被拒绝的发送不写入任何记录，
因此不会破坏「第 k 条用户消息 ↔ 第 k 个任务」的配对关系。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException, status

from backend import conversations, database, storage
from backend.tasks.runner import TaskRunner
from backend.tasks.state import (
    ACTIVE_STATUSES,
    STEP_CANCELLED,
    TaskStatus,
    cancel_registry,
    conversation_locks,
)
from backend.utils import utc_now_iso
from backend.ws.manager import manager as ws_manager

# 持有运行中的 asyncio 任务引用，避免被垃圾回收
_running: set[asyncio.Task] = set()


class ConversationBusy(Exception):
    """该会话已有正在执行的分析任务。"""


async def active_task(conversation_id: int) -> dict[str, Any] | None:
    row = await database.fetch_one(
        "SELECT * FROM analysis_tasks WHERE conversation_id = ? AND task_status IN (?, ?)"
        " ORDER BY id DESC LIMIT 1",
        (conversation_id, TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
    )
    return dict(row) if row else None


async def create_task_from_message(
    *,
    user_id: int,
    conversation_id: int,
    content: str,
    attachment_ids: list[int] | None = None,
) -> dict[str, Any]:
    """由一条用户消息创建分析任务，返回 {message_id, seq_no, task_id, created_at}。"""
    text = (content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    conversation = await conversations.require_conversation(conversation_id, user_id)
    if conversation["status"] != "active":
        raise HTTPException(status_code=400, detail="已归档的会话不能发起新分析，请先恢复为 active")

    attachment_ids = list(dict.fromkeys(attachment_ids or []))
    if attachment_ids:
        await _validate_attachments(conversation_id, attachment_ids)

    lock = await conversation_locks.get(conversation_id)
    async with lock:
        existing = await active_task(conversation_id)
        if existing is not None:
            raise ConversationBusy(
                f"该会话已有正在执行的分析任务（task_id={existing['id']}，状态 {existing['task_status']}）"
            )

        now = utc_now_iso()
        async with database.transaction() as conn:
            seq_no = await conversations.allocate_seq_no(conn, conversation_id)
            cursor = await conn.execute(
                "INSERT INTO messages (conversation_id, role, message_type, content,"
                " tool_name, tool_status, seq_no, created_at) VALUES (?, 'user', 'text', ?, NULL, NULL, ?, ?)",
                (conversation_id, text, seq_no, now),
            )
            message_id = cursor.lastrowid
            await cursor.close()

            cursor = await conn.execute(
                "INSERT INTO analysis_tasks (conversation_id, user_id, input_text, task_status,"
                " current_step, started_at, finished_at, error_message)"
                " VALUES (?, ?, ?, 'queued', '排队中', NULL, NULL, NULL)",
                (conversation_id, user_id, text),
            )
            task_id = cursor.lastrowid
            await cursor.close()

            for attachment_id in attachment_ids:
                await conn.execute(
                    "UPDATE attachments SET message_id = ? WHERE id = ? AND conversation_id = ?",
                    (message_id, attachment_id, conversation_id),
                )

            await conn.execute(
                "UPDATE conversations SET last_message_at = ?, updated_at = ? WHERE id = ?",
                (now, now, conversation_id),
            )

    storage.ensure_conv_dirs(user_id, conversation_id)

    await ws_manager.broadcast(
        conversation_id,
        {
            "type": "user_message_accepted",
            "message_id": message_id,
            "seq_no": seq_no,
            "task_id": task_id,
            "created_at": now,
        },
    )

    _spawn(task_id)
    return {
        "message_id": message_id,
        "seq_no": seq_no,
        "task_id": task_id,
        "created_at": now,
        "conversation_id": conversation_id,
    }


async def _validate_attachments(conversation_id: int, attachment_ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in attachment_ids)
    rows = await database.fetch_all(
        f"SELECT id FROM attachments WHERE conversation_id = ? AND id IN ({placeholders})",
        (conversation_id, *attachment_ids),
    )
    found = {r["id"] for r in rows}
    missing = [aid for aid in attachment_ids if aid not in found]
    if missing:
        raise HTTPException(status_code=404, detail=f"附件不存在或不属于该会话：{missing}")


def _spawn(task_id: int) -> None:
    runner = TaskRunner(task_id)
    task = asyncio.create_task(runner.run(), name=f"analysis-task-{task_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)


async def cancel_task(task_id: int, user_id: int) -> dict[str, Any]:
    """请求取消任务。排队中的立即取消，运行中的在下一个检查点退出。"""
    row = await database.fetch_one("SELECT * FROM analysis_tasks WHERE id = ?", (task_id,))
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")

    task = dict(row)
    current = task["task_status"]
    if current in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
        return {"task_id": task_id, "task_status": current, "cancelled": False,
                "message": "任务已结束，无需取消"}

    # 先置取消标记：若任务已经在运行，运行器会在下一个检查点自行退出
    cancel_registry.request(task_id)

    if current == TaskStatus.QUEUED.value:
        now = utc_now_iso()
        # 条件更新：仅当任务仍是 queued 时由本路径收尾。
        # 若运行器已抢先置为 running，则这里影响 0 行，交由运行器统一收尾，
        # 从而保证 done 事件只发一次、状态不会来回翻转。
        _rowid, affected = await database.execute(
            "UPDATE analysis_tasks SET task_status = 'cancelled', current_step = ?,"
            " finished_at = ?, error_message = '任务已取消'"
            " WHERE id = ? AND task_status = 'queued'",
            (STEP_CANCELLED, now, task_id),
        )
        if affected == 1:
            await database.execute(
                "INSERT INTO task_logs (task_id, log_level, log_type, log_content, created_at)"
                " VALUES (?, 'warning', 'step', '排队中的任务被取消', ?)",
                (task_id, now),
            )
            conversation_id = task["conversation_id"]
            await ws_manager.broadcast(
                conversation_id,
                {
                    "type": "task_status",
                    "task_id": task_id,
                    "task_status": TaskStatus.CANCELLED.value,
                    "current_step": STEP_CANCELLED,
                },
            )
            await ws_manager.broadcast(
                conversation_id,
                {"type": "done", "task_id": task_id, "finished_at": now,
                 "task_status": TaskStatus.CANCELLED.value},
            )
            return {"task_id": task_id, "task_status": TaskStatus.CANCELLED.value,
                    "cancelled": True, "message": "排队中的任务已取消"}

    return {
        "task_id": task_id,
        "task_status": current,
        "cancelled": True,
        "message": "已发出取消请求，任务将在当前步骤结束后停止",
    }


async def cancel_conversation_tasks(conversation_id: int) -> int:
    """取消会话下所有排队/运行中的任务（会话删除时调用）。"""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = await database.fetch_all(
        f"SELECT id, task_status FROM analysis_tasks WHERE conversation_id = ?"
        f" AND task_status IN ({placeholders})",
        (conversation_id, *[s.value for s in ACTIVE_STATUSES]),
    )
    cancelled = 0
    for row in rows:
        cancel_registry.request(row["id"])
        if row["task_status"] == TaskStatus.QUEUED.value:
            # 与 cancel_task 相同的条件更新语义：抢不到就交给运行器收尾
            await database.execute(
                "UPDATE analysis_tasks SET task_status = 'cancelled', current_step = ?,"
                " finished_at = ?, error_message = '会话已删除，任务取消'"
                " WHERE id = ? AND task_status = 'queued'",
                (STEP_CANCELLED, utc_now_iso(), row["id"]),
            )
        cancelled += 1
    await conversation_locks.discard(conversation_id)
    return cancelled


async def get_task(task_id: int, user_id: int) -> dict[str, Any]:
    row = await database.fetch_one("SELECT * FROM analysis_tasks WHERE id = ?", (task_id,))
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task_payload(row)


def task_payload(row: Any) -> dict[str, Any]:
    return {
        "task_id": row["id"],
        "conversation_id": row["conversation_id"],
        "input_text": row["input_text"],
        "task_status": row["task_status"],
        "current_step": row["current_step"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error_message": row["error_message"],
    }


async def conversation_snapshot(conversation_id: int) -> dict[str, Any] | None:
    """连接建立时下发当前任务状态快照，便于断线重连恢复界面。"""
    row = await database.fetch_one(
        "SELECT * FROM analysis_tasks WHERE conversation_id = ?"
        " AND task_status IN (?, ?) ORDER BY id DESC LIMIT 1",
        (conversation_id, TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
    )
    return task_payload(row) if row else None
