"""会话共享逻辑：归属校验、序号分配、级联删除。

会话删除采用「软删除 + 子表物理删除」：
  conversations.status 置为 'deleted'（保留行，满足状态机与审计需要），
  messages / attachments / analysis_tasks / analysis_results / context_summaries /
  task_logs / websocket_tokens 全部物理删除，并清理 uploads/exports/workspace 目录。
"""
from __future__ import annotations

import aiosqlite
from fastapi import HTTPException, status

from backend import database, storage
from backend.utils import utc_now_iso


async def require_conversation(
    conversation_id: int, user_id: int, *, include_deleted: bool = False
) -> dict:
    """校验会话存在且属于当前用户，否则 404。"""
    row = await database.fetch_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    if not include_deleted and row["status"] == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话已删除")
    return dict(row)


async def allocate_seq_no(conn: aiosqlite.Connection, conversation_id: int) -> int:
    """分配会话内下一个消息序号。

    必须在「每会话单写者」的保护下调用（任务创建走会话锁，见 tasks/service.py），
    否则并发写入可能产生重复序号。
    """
    cursor = await conn.execute(
        "SELECT COALESCE(MAX(seq_no), 0) + 1 FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row[0]) if row else 1


async def touch_conversation(conversation_id: int, when: str | None = None) -> None:
    now = when or utc_now_iso()
    await database.execute(
        "UPDATE conversations SET last_message_at = ?, updated_at = ? WHERE id = ?",
        (now, now, conversation_id),
    )


async def _cancel_running_tasks(conversation_id: int) -> None:
    """取消该会话下排队/运行中的任务（任务模块在 P6 提供实现）。"""
    try:
        from backend.tasks import service as task_service
    except ImportError:
        return
    await task_service.cancel_conversation_tasks(conversation_id)


async def delete_conversation(user_id: int, conversation_id: int) -> dict:
    """级联删除单个会话，返回删除统计。"""
    row = await require_conversation(conversation_id, user_id, include_deleted=True)

    # 1) 先让运行中的任务停下来，避免删除后又写回数据
    await _cancel_running_tasks(conversation_id)

    # 2) 事务内按外键依赖顺序清理子表
    async with database.transaction() as conn:
        cursor = await conn.execute(
            "SELECT id FROM analysis_tasks WHERE conversation_id = ?", (conversation_id,)
        )
        task_ids = [r["id"] for r in await cursor.fetchall()]
        await cursor.close()

        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            await conn.execute(
                f"DELETE FROM task_logs WHERE task_id IN ({placeholders})", task_ids
            )
        await conn.execute("DELETE FROM analysis_results WHERE conversation_id = ?", (conversation_id,))
        await conn.execute("DELETE FROM websocket_tokens WHERE conversation_id = ?", (conversation_id,))
        # attachments 引用 messages，必须先删附件
        await conn.execute("DELETE FROM attachments WHERE conversation_id = ?", (conversation_id,))
        await conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        await conn.execute("DELETE FROM context_summaries WHERE conversation_id = ?", (conversation_id,))
        await conn.execute("DELETE FROM analysis_tasks WHERE conversation_id = ?", (conversation_id,))
        await conn.execute(
            "UPDATE conversations SET status = 'deleted', updated_at = ? WHERE id = ?",
            (utc_now_iso(), conversation_id),
        )

    # 3) 清理文件目录（附件 / 导出 / 临时工作区）
    removed_dirs = storage.delete_conv_dirs(user_id, conversation_id)

    return {
        "conversation_id": conversation_id,
        "title": row["title"],
        "messages_deleted": True,
        "removed_dirs": removed_dirs,
    }
