"""后端应用装配：生命周期、路由注册、静态资源托管。

注意：静态资源必须最后挂载，否则会抢占 /api/* 与 WebSocket 路由。
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from backend import config, database, storage  # noqa: E402
from backend.routers import (  # noqa: E402
    admin_router,
    attachment_router,
    auth_router,
    chat_router,
    result_router,
    task_router,
)
from backend.utils import utc_now_iso  # noqa: E402
from backend.ws import endpoint as ws_endpoint  # noqa: E402

STALE_TASK_MESSAGE = "服务重启导致任务中断"


async def cleanup_stale_tasks() -> int:
    """进程启动时把上次遗留的 queued/running 任务标记为失败。"""
    now = utc_now_iso()
    rows = await database.fetch_all(
        "SELECT id FROM analysis_tasks WHERE task_status IN ('queued', 'running')"
    )
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    await database.execute(
        f"UPDATE analysis_tasks SET task_status = 'failed', error_message = ?, finished_at = ?"
        f" WHERE id IN ({placeholders})",
        (STALE_TASK_MESSAGE, now, *ids),
    )
    for task_id in ids:
        await database.execute(
            "INSERT INTO task_logs (task_id, log_level, log_type, log_content, created_at)"
            " VALUES (?, 'error', 'system', ?, ?)",
            (task_id, STALE_TASK_MESSAGE, now),
        )
    return len(ids)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from db import seed_system

    storage.ensure_all()
    await database.init_db()
    await seed_system.seed_system_configs()
    await config.reload_config()
    stale = await cleanup_stale_tasks()
    from backend.tasks import runner as task_runner

    task_runner.LAST_CLEANUP_COUNT = stale

    print(
        f"[backend] 启动完成 | 数据目录={config.data_dir()} | "
        f"情景库目录={config.scenario_db_dir()} | 引擎={config.get('analysis.engine_mode')} | "
        f"中断任务清理={stale}",
        flush=True,
    )
    yield
    print("[backend] 已停止", flush=True)


app = FastAPI(
    title="经营归因分析系统",
    description="多轮归因分析工作台后端：会话、附件、实时任务、结构化结果与配置热更新。",
    version="0.1.0",
    lifespan=lifespan,
)

# ---- 路由注册（静态资源挂载之前） ----
app.include_router(auth_router.router)
app.include_router(chat_router.router)
app.include_router(attachment_router.router)
app.include_router(task_router.router)
app.include_router(result_router.router)
app.include_router(admin_router.router)
app.include_router(ws_endpoint.router)  # WebSocket 路由


@app.get("/api/health", tags=["系统"], summary="健康检查（扩展接口）")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "backend",
        "server_time": utc_now_iso(),
        "engine_mode": config.get("analysis.engine_mode"),
        "scenario_db_dir": str(config.scenario_db_dir()),
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    print(f"[backend] 未处理异常 {request.method} {request.url.path}: {exc!r}", flush=True)
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {exc}"})


def _page(filename: str) -> FileResponse:
    path = config.FRONTEND_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在: {filename}")
    return FileResponse(path, media_type="text/html; charset=utf-8")


# StaticFiles(html=True) 只把「目录」映射到 index.html，不会把 /app 解析成 app.html，
# 因此这里为无扩展名路径显式注册页面路由（必须位于静态挂载之前）。
@app.get("/login", include_in_schema=False)
@app.get("/app", include_in_schema=False)
async def workspace_page() -> FileResponse:
    return _page("app.html")


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return _page("admin.html")


# ---- 静态资源（必须最后挂载） ----
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
