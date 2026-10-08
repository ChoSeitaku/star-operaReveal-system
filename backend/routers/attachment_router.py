"""附件模块：文件保存、删除、下载、列表，含路径越界校验。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import config, conversations, database, session_auth, storage
from backend.attachments import parser
from backend.utils import utc_now_iso

router = APIRouter(prefix="/api/attachment", tags=["附件"])

CHUNK_SIZE = 1024 * 1024


class DeleteRequest(BaseModel):
    attachment_id: int


def _relative_path(file_path: str) -> str:
    """对外返回相对于数据目录的路径，避免泄露服务器绝对路径。"""
    try:
        return Path(file_path).resolve().relative_to(storage.data_dir().resolve()).as_posix()
    except (ValueError, OSError):
        return Path(file_path).name


def _attachment_payload(row) -> dict:
    return {
        "attachment_id": row["id"],
        "conversation_id": row["conversation_id"],
        "message_id": row["message_id"],
        "file_name": row["file_name"],
        "file_path": _relative_path(row["file_path"]),
        "file_type": row["file_type"],
        "file_size": row["file_size"],
        "parse_status": row["parse_status"],
        "created_at": row["created_at"],
    }


async def _owned_attachment(attachment_id: int, user_id: int):
    row = await database.fetch_one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在")
    conv = await database.fetch_one(
        "SELECT * FROM conversations WHERE id = ?", (row["conversation_id"],)
    )
    if conv is None or conv["user_id"] != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在")
    return row


@router.post("/upload", summary="上传附件")
async def upload_attachment(
    user: session_auth.CurrentUser,
    conversation_id: int = Form(..., description="会话 ID"),
    file: UploadFile = File(..., description="文件对象"),
) -> dict:
    conv = await conversations.require_conversation(conversation_id, user["id"])

    original_name = storage.sanitize_filename(file.filename or "未命名文件")
    file_type = storage.file_type_of(original_name)
    allowed = config.get_list("upload.allowed_extensions", ["csv", "txt", "md", "json", "log", "xlsx"])
    if file_type not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 .{file_type}，允许的类型：{', '.join(allowed)}",
        )

    max_bytes = config.get_int("upload.max_file_size_mb", 20) * 1024 * 1024
    upload_dir = storage.upload_dir(user["id"], conv["id"])
    upload_dir.mkdir(parents=True, exist_ok=True)

    now = utc_now_iso()
    attachment_id, _ = await database.execute(
        "INSERT INTO attachments (conversation_id, message_id, file_name, file_path, file_type,"
        " file_size, parse_status, created_at) VALUES (?, NULL, ?, '', ?, 0, 'pending', ?)",
        (conv["id"], original_name, file_type, now),
    )

    target = storage.safe_join(upload_dir, f"{attachment_id}_{original_name}")
    written = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小限制（{config.get_int('upload.max_file_size_mb', 20)} MB）",
                    )
                handle.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        await database.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        await database.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        raise HTTPException(status_code=500, detail=f"文件写入失败: {exc}") from exc
    finally:
        await file.close()

    if written == 0:
        target.unlink(missing_ok=True)
        await database.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        raise HTTPException(status_code=400, detail="上传内容为空")

    await database.execute(
        "UPDATE attachments SET file_path = ?, file_size = ? WHERE id = ?",
        (str(target), written, attachment_id),
    )

    # 后台解析，不阻塞上传响应
    asyncio.create_task(parser.parse_attachment(attachment_id))

    return {
        "attachment_id": attachment_id,
        "file_name": original_name,
        "file_path": _relative_path(str(target)),
        "file_type": file_type,
        "file_size": written,
        "parse_status": "pending",
        "size_text": storage.human_size(written),
    }


@router.post("/delete", summary="删除附件")
async def delete_attachment(payload: DeleteRequest, user: session_auth.CurrentUser) -> dict:
    row = await _owned_attachment(payload.attachment_id, user["id"])

    file_path = Path(row["file_path"])
    if storage.is_within(file_path, storage.data_dir()) and file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass
    parser.remove_sidecar(row["file_path"])

    await database.execute("DELETE FROM attachments WHERE id = ?", (row["id"],))
    return {"attachment_id": row["id"], "file_name": row["file_name"], "deleted": True}


@router.get("/get", summary="下载附件")
async def get_attachment(
    user: session_auth.CurrentUser,
    attachment_id: int = Query(..., description="附件 ID"),
) -> FileResponse:
    row = await _owned_attachment(attachment_id, user["id"])

    file_path = Path(row["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="附件文件已丢失")
    if not storage.is_within(file_path, storage.data_dir()):
        raise HTTPException(status_code=403, detail="非法的文件路径")
    return FileResponse(path=file_path, filename=row["file_name"], media_type="application/octet-stream")


@router.get("/ls", summary="会话附件列表（扩展接口）")
async def list_attachments(conversation_id: int, user: session_auth.CurrentUser) -> dict:
    await conversations.require_conversation(conversation_id, user["id"])
    rows = await database.fetch_all(
        "SELECT * FROM attachments WHERE conversation_id = ? ORDER BY id DESC", (conversation_id,)
    )
    items = [_attachment_payload(r) | {"size_text": storage.human_size(r["file_size"])} for r in rows]
    return {"total": len(items), "attachments": items}
