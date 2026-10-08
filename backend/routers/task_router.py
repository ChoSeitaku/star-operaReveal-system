"""任务查询与取消接口。"""
from __future__ import annotations

from fastapi import APIRouter

from backend import database, session_auth
from backend.tasks import service as task_service

router = APIRouter(prefix="/api/tasks", tags=["任务"])


@router.get("/{task_id}", summary="查询任务状态")
async def get_task(task_id: int, user: session_auth.CurrentUser) -> dict:
    return await task_service.get_task(task_id, user["id"])


@router.get("/{task_id}/logs", summary="查询任务执行日志（扩展接口）")
async def get_task_logs(task_id: int, user: session_auth.CurrentUser, limit: int = 200) -> dict:
    await task_service.get_task(task_id, user["id"])  # 归属校验
    rows = await database.fetch_all(
        "SELECT id, log_level, log_type, log_content, created_at FROM task_logs"
        " WHERE task_id = ? ORDER BY id ASC LIMIT ?",
        (task_id, max(1, min(limit, 1000))),
    )
    return {
        "task_id": task_id,
        "total": len(rows),
        "logs": [
            {
                "log_id": r["id"],
                "log_level": r["log_level"],
                "log_type": r["log_type"],
                "log_content": r["log_content"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@router.post("/{task_id}/cancel", summary="取消分析任务（扩展接口）")
async def cancel_task(task_id: int, user: session_auth.CurrentUser) -> dict:
    return await task_service.cancel_task(task_id, user["id"])
