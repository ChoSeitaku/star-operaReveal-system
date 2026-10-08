"""会话模块：会话新增、查询、更新、删除，历史消息回放，长连接令牌签发。"""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from backend import config, conversations, database, session_auth, storage
from backend.utils import to_iso, utc_now, utc_now_iso

router = APIRouter(prefix="/api/chat", tags=["会话"])

DEFAULT_TITLE = "新会话"
MAX_TITLE_LENGTH = 60


class CreateRequest(BaseModel):
    title: str = Field(default="", description="会话标题，留空则自动生成")


class DeleteRequest(BaseModel):
    conversation_ids: list[int] = Field(default_factory=list)


class UpdateRequest(BaseModel):
    conversation_id: int
    title: str | None = None
    status: str | None = Field(default=None, description="扩展：active / archived（归档）")


class WsTokenRequest(BaseModel):
    conversation_id: int


def _conversation_payload(row: Any) -> dict[str, Any]:
    return {
        "conversation_id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "last_message_at": row["last_message_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.post("/create", summary="新建会话")
async def create_conversation(payload: CreateRequest, user: session_auth.CurrentUser) -> dict:
    title = (payload.title or "").strip()[:MAX_TITLE_LENGTH] or DEFAULT_TITLE
    now = utc_now_iso()
    conversation_id, _ = await database.execute(
        "INSERT INTO conversations (user_id, title, status, last_message_at, created_at, updated_at)"
        " VALUES (?, ?, 'active', NULL, ?, ?)",
        (user["id"], title, now, now),
    )
    storage.ensure_conv_dirs(user["id"], conversation_id)
    return {"conversation_id": conversation_id, "title": title, "status": "active"}


@router.post("/delete", summary="删除会话（级联删除消息、附件、任务、结果、摘要与文件目录）")
async def delete_conversations(payload: DeleteRequest, user: session_auth.CurrentUser) -> dict:
    if not payload.conversation_ids:
        raise HTTPException(status_code=400, detail="conversation_ids 不能为空")

    deleted, skipped = [], []
    for conversation_id in dict.fromkeys(payload.conversation_ids):  # 去重且保持顺序
        row = await database.fetch_one(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        if row is None or row["user_id"] != user["id"]:
            skipped.append({"conversation_id": conversation_id, "reason": "会话不存在"})
            continue
        if row["status"] == "deleted":
            skipped.append({"conversation_id": conversation_id, "reason": "会话已删除"})
            continue
        deleted.append(await conversations.delete_conversation(user["id"], conversation_id))

    return {
        "deleted_count": len(deleted),
        "deleted": [{"conversation_id": d["conversation_id"], "title": d["title"]} for d in deleted],
        "skipped": skipped,
    }


@router.post("/update", summary="重命名/归档会话")
async def update_conversation(payload: UpdateRequest, user: session_auth.CurrentUser) -> dict:
    row = await conversations.require_conversation(payload.conversation_id, user["id"])

    fields: list[str] = []
    params: list[Any] = []
    if payload.title is not None:
        title = payload.title.strip()[:MAX_TITLE_LENGTH]
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        fields.append("title = ?")
        params.append(title)
    if payload.status is not None:
        if payload.status not in ("active", "archived"):
            raise HTTPException(status_code=400, detail="status 仅支持 active / archived")
        fields.append("status = ?")
        params.append(payload.status)
    if not fields:
        raise HTTPException(status_code=400, detail="title 与 status 至少提供一个")

    fields.append("updated_at = ?")
    params.append(utc_now_iso())
    params.append(payload.conversation_id)
    await database.execute(f"UPDATE conversations SET {', '.join(fields)} WHERE id = ?", params)

    updated = await database.fetch_one(
        "SELECT * FROM conversations WHERE id = ?", (payload.conversation_id,)
    )
    return _conversation_payload(updated) | {"previous_title": row["title"]}


@router.get("/ls", summary="会话列表")
async def list_conversations(
    user: session_auth.CurrentUser, include_archived: bool = True
) -> dict:
    statuses = ("active", "archived") if include_archived else ("active",)
    placeholders = ",".join("?" for _ in statuses)
    rows = await database.fetch_all(
        f"SELECT * FROM conversations WHERE user_id = ? AND status IN ({placeholders})"
        f" ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC",
        (user["id"], *statuses),
    )
    items = []
    for row in rows:
        item = _conversation_payload(row)
        item["message_count"] = await database.fetch_value(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (row["id"],)
        )
        item["attachment_count"] = await database.fetch_value(
            "SELECT COUNT(*) FROM attachments WHERE conversation_id = ?", (row["id"],)
        )
        items.append(item)
    return {"total": len(items), "conversations": items}


@router.get("/ls/{conversation_id}", summary="会话历史消息")
async def list_messages(conversation_id: int, user: session_auth.CurrentUser) -> dict:
    await conversations.require_conversation(conversation_id, user["id"])

    rows = await database.fetch_all(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq_no ASC, id ASC",
        (conversation_id,),
    )
    attachments = await database.fetch_all(
        "SELECT * FROM attachments WHERE conversation_id = ? ORDER BY id ASC", (conversation_id,)
    )
    by_message: dict[int, list[dict]] = {}
    for att in attachments:
        if att["message_id"] is None:
            continue
        by_message.setdefault(att["message_id"], []).append(_attachment_brief(att))

    # 任务与用户消息一一对应（第 k 条用户消息 ↔ 第 k 个任务），用于前端关联结果。
    # 字段名必须与 /api/tasks/{id} 一致（task_id），否则前端拿不到任务号。
    task_rows = await database.fetch_all(
        "SELECT t.id AS task_id, t.task_status, t.current_step, t.input_text, t.started_at,"
        " t.finished_at, t.error_message,"
        " (SELECT r.id FROM analysis_results r WHERE r.task_id = t.id) AS result_id"
        " FROM analysis_tasks t WHERE t.conversation_id = ? ORDER BY t.id ASC",
        (conversation_id,),
    )
    task_queue = [dict(r) for r in task_rows]

    messages = []
    for row in rows:
        item = {
            "message_id": row["id"],
            "role": row["role"],
            "message_type": row["message_type"],
            "content": row["content"],
            "tool_name": row["tool_name"],
            "tool_status": row["tool_status"],
            "seq_no": row["seq_no"],
            "created_at": row["created_at"],
            "attachments": by_message.get(row["id"], []),
            "task": None,
        }
        if row["role"] == "user" and task_queue:
            task = task_queue.pop(0)
            task["has_result"] = task.pop("result_id", None) is not None
            item["task"] = task
        messages.append(item)

    return {
        "conversation_id": conversation_id,
        "total": len(messages),
        "messages": messages,
    }


def _attachment_brief(row: Any) -> dict:
    return {
        "attachment_id": row["id"],
        "file_name": row["file_name"],
        "file_type": row["file_type"],
        "file_size": row["file_size"],
        "parse_status": row["parse_status"],
    }


@router.post("/ws-token", summary="签发长连接一次性临时令牌")
async def issue_ws_token(payload: WsTokenRequest, user: session_auth.CurrentUser) -> dict:
    """令牌短时效、单次消费且绑定会话；重连必须重新签发。"""
    conversation = await conversations.require_conversation(payload.conversation_id, user["id"])
    if conversation["status"] == "deleted":
        raise HTTPException(status_code=400, detail="会话已删除")

    ttl = max(5, config.get_int("auth.ws_token_ttl_seconds", 60))
    now = utc_now()
    token = secrets.token_urlsafe(32)

    await database.execute(
        "INSERT INTO websocket_tokens (user_id, conversation_id, token, expires_at, consumed_at, created_at)"
        " VALUES (?, ?, ?, ?, NULL, ?)",
        (user["id"], conversation["id"], token, to_iso(now + timedelta(seconds=ttl)), to_iso(now)),
    )

    return {
        "websocket_token": token,
        "expires_in": ttl,
        "conversation_id": conversation["id"],
        "ws_url": f"/api/chat/ws/chat?websocket_token={token}&conversation_id={conversation['id']}",
    }
