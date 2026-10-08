"""分析结果查询与导出下载接口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend import database, session_auth, storage
from backend.analysis.result_builder import result_payload

router = APIRouter(prefix="/api/results", tags=["结果"])


async def _owned_result(task_id: int, user_id: int):
    task = await database.fetch_one("SELECT * FROM analysis_tasks WHERE id = ?", (task_id,))
    if task is None or task["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")

    row = await database.fetch_one("SELECT * FROM analysis_results WHERE task_id = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="该任务尚未生成结构化结果")
    return row


@router.get("/{task_id}", summary="查询结构化分析结果")
async def get_result(task_id: int, user: session_auth.CurrentUser) -> dict:
    row = await _owned_result(task_id, user["id"])
    return result_payload(row)


@router.get("/{task_id}/download", summary="下载结果文件（扩展接口）")
async def download_result(task_id: int, user: session_auth.CurrentUser) -> FileResponse:
    row = await _owned_result(task_id, user["id"])

    file_path = row["result_file_path"]
    if not file_path:
        raise HTTPException(status_code=404, detail="该结果没有生成导出文件")
    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="导出文件已丢失，可重新运行分析生成")
    if not storage.is_within(path, storage.data_dir()):
        raise HTTPException(status_code=403, detail="非法的文件路径")

    return FileResponse(
        path=path,
        filename=path.name,
        media_type="text/markdown; charset=utf-8",
    )
