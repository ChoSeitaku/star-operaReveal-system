"""系统管理接口：配置查看与热更新、运行日志、数据源连接、工具自检。

全部接口要求 role=admin。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend import config, database, session_auth, storage
from backend.analysis import tools as analysis_tools
from backend.tasks import runner as task_runner
from backend.tasks.state import cancel_registry
from backend.utils import truncate, utc_now_iso
from backend.ws.manager import manager as ws_manager

router = APIRouter(prefix="/api/admin", tags=["系统管理"])


class ConfigUpdateRequest(BaseModel):
    configs: dict[str, str] = Field(..., description="要写入的配置键值对，键必须在已知配置项内")
    auto_reload: bool = Field(default=True, description="写入后是否立即重载生效")


def _reload_caches() -> None:
    """配置热更新的副作用：失效数据库结构缓存。"""
    analysis_tools.invalidate_db_cache()


@router.post("/reload", summary="重载系统配置")
async def reload_config(user: session_auth.AdminUser) -> dict:
    before = config.get("analysis.engine_mode")
    await config.reload_config()
    _reload_caches()
    after = config.get("analysis.engine_mode")

    await database.execute(
        "INSERT INTO task_logs (task_id, log_level, log_type, log_content, created_at)"
        " VALUES (NULL, 'info', 'system', ?, ?)",
        (f"管理员 {user['username']} 重载配置：engine_mode {before} → {after}", utc_now_iso()),
    )
    return {
        "status": "ok",
        "message": f"已重载 {len(config.CONFIG_DEFAULTS)} 项配置，分析引擎：{after}",
        "engine_mode": after,
        "config_count": len(config.CONFIG_DEFAULTS),
    }


@router.get("/config", summary="查看当前生效配置（扩展接口）")
async def get_config(user: session_auth.AdminUser) -> dict:
    items = config.snapshot(mask_secrets=True)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item["config_group"], []).append(item)
    return {
        "total": len(items),
        "groups": groups,
        "items": items,
        "engine_mode": config.get("analysis.engine_mode"),
    }


@router.post("/config", summary="修改配置（扩展接口）")
async def set_config(payload: ConfigUpdateRequest, user: session_auth.AdminUser) -> dict:
    if not payload.configs:
        raise HTTPException(status_code=400, detail="configs 不能为空")

    unknown = [k for k in payload.configs if k not in config.CONFIG_DEFAULTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知配置项：{', '.join(unknown)}")

    now = utc_now_iso()
    for key, value in payload.configs.items():
        await database.execute(
            "INSERT INTO system_configs (config_key, config_value, config_group, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(config_key) DO UPDATE SET config_value = excluded.config_value,"
            " updated_at = excluded.updated_at",
            (key, str(value), config.config_group_of(key), now),
        )

    reloaded = False
    if payload.auto_reload:
        await config.reload_config()
        _reload_caches()
        reloaded = True

    # 环境变量优先级高于数据库配置，被覆盖的键写入后不会生效，必须如实告知
    forced = sorted(set(payload.configs) & config.env_overridden_keys())

    await database.execute(
        "INSERT INTO task_logs (task_id, log_level, log_type, log_content, created_at)"
        " VALUES (NULL, 'info', 'system', ?, ?)",
        (f"管理员 {user['username']} 修改配置 {list(payload.configs)}（自动重载={reloaded}）", now),
    )
    message = f"已更新 {len(payload.configs)} 项配置" + ("并即时生效" if reloaded else "")
    if forced:
        message += (
            f"；注意：{', '.join(forced)} 当前被系统环境变量覆盖，"
            f"数据库中的值不会生效，请调整环境变量后重启服务"
        )
    return {
        "status": "ok",
        "message": message,
        "updated": list(payload.configs),
        "reloaded": reloaded,
        "overridden_by_env": forced,
    }


@router.get("/logs", summary="查看运行日志（扩展接口）")
async def get_logs(
    user: session_auth.AdminUser,
    limit: int = Query(default=200, ge=1, le=2000),
    log_level: str = Query(default="", description="可选，按级别过滤"),
    task_id: int | None = Query(default=None, description="可选，按任务过滤"),
) -> dict:
    clauses, params = [], []
    if log_level:
        clauses.append("log_level = ?")
        params.append(log_level)
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = await database.fetch_all(
        f"SELECT id, task_id, log_level, log_type, log_content, created_at FROM task_logs"
        f" {where} ORDER BY id DESC LIMIT ?",
        params,
    )
    return {
        "total": len(rows),
        "logs": [
            {
                "log_id": r["id"],
                "task_id": r["task_id"],
                "log_level": r["log_level"],
                "log_type": r["log_type"],
                "log_content": r["log_content"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@router.get("/datasources", summary="数据源连接列表（扩展接口）")
async def list_datasources(user: session_auth.AdminUser) -> dict:
    infos = await asyncio.to_thread(analysis_tools.describe_databases, True)
    directory = config.scenario_db_dir()
    return {
        "scenario_db_dir": str(directory),
        "directory_exists": directory.exists(),
        "total": len(infos),
        "datasources": [
            {
                "name": info["name"],
                "file": info["file"],
                "table_count": len(info["tables"]),
                "row_total": sum(t["row_count"] for t in info["tables"]),
                "tables": info["tables"],
                "error": info.get("error"),
            }
            for info in infos
        ],
    }


@router.get("/stats", summary="运行状态概览（扩展接口）")
async def stats(user: session_auth.AdminUser) -> dict:
    now = utc_now_iso()

    async def count(sql: str, params: tuple = ()) -> int:
        value = await database.fetch_value(sql, params)
        return int(value or 0)

    return {
        "server_time": now,
        "users": await count("SELECT COUNT(*) FROM users"),
        "conversations_total": await count("SELECT COUNT(*) FROM conversations"),
        "conversations_active": await count(
            "SELECT COUNT(*) FROM conversations WHERE status = 'active'"
        ),
        "messages": await count("SELECT COUNT(*) FROM messages"),
        "attachments": await count("SELECT COUNT(*) FROM attachments"),
        "tasks_total": await count("SELECT COUNT(*) FROM analysis_tasks"),
        "tasks_running": await count(
            "SELECT COUNT(*) FROM analysis_tasks WHERE task_status IN ('queued','running')"
        ),
        "tasks_failed": await count(
            "SELECT COUNT(*) FROM analysis_tasks WHERE task_status = 'failed'"
        ),
        "results": await count("SELECT COUNT(*) FROM analysis_results"),
        "summaries": await count("SELECT COUNT(*) FROM context_summaries"),
        "cleanup_interrupted": task_runner.LAST_CLEANUP_COUNT,
        "cancelling_tasks": cancel_registry.snapshot(),
        "websocket": ws_manager.stats(),
        "engine_mode": config.get("analysis.engine_mode"),
        "data_dir": str(config.data_dir()),
    }


# ---------------------------------------------------------------- 工具自检

SELFTEST_DB_QUERY_FALLBACK = ("catalog", "SELECT COUNT(*) AS 行数 FROM products")


@router.post("/selftest", summary="分析工具自检（扩展接口）")
async def selftest(user: session_auth.AdminUser) -> dict:
    """逐个执行六个分析工具，验证能力可用。会在 data/selftest/ 下产生临时文件。"""
    base = config.data_dir() / "selftest"
    (base / "workspace").mkdir(parents=True, exist_ok=True)
    (base / "uploads").mkdir(parents=True, exist_ok=True)

    tool_ctx = analysis_tools.ToolContext(
        workspace_dir=base / "workspace",
        upload_dir=base / "uploads",
        enabled=analysis_tools.enabled_tool_names(),
    )

    # 构造一个上传样例，供 file_read / text_search 使用
    sample = base / "uploads" / "selftest_sample.csv"
    sample.write_text("指标,数值\n曝光量,12000\n转化量,722\n", encoding="utf-8")

    databases = analysis_tools.describe_databases()
    db_name, sql = SELFTEST_DB_QUERY_FALLBACK
    if databases:
        db_name = databases[0]["name"]
        if databases[0]["tables"]:
            sql = f'SELECT COUNT(*) AS 行数 FROM "{databases[0]["tables"][0]["name"]}"'

    cases: list[tuple[str, dict[str, Any]]] = [
        ("list_databases", {}),
        ("db_query", {"database": db_name, "sql": sql}),
        ("file_write", {"filename": "selftest_note.md", "content": "# 自检笔记\n工具链路正常。"}),
        ("file_read", {"path": "selftest_note.md"}),
        ("text_search", {"query": "曝光量"}),
        ("run_command", {"command": "python -c \"print('工具自检 OK')\""}),
    ]

    results: list[dict[str, Any]] = []
    for name, arguments in cases:
        try:
            output = await analysis_tools.execute_tool(name, arguments, tool_ctx)
        except Exception as exc:  # 自检不应抛出，转为失败项
            output = f"错误：{exc}"

        ok = not output.startswith("错误：")
        results.append(
            {
                "tool": name,
                "enabled": name in analysis_tools.enabled_tool_names(),
                "ok": ok,
                "summary": truncate(output.replace("\n", " "), 220),
            }
        )

    passed = sum(1 for r in results if r["ok"])
    await database.execute(
        "INSERT INTO task_logs (task_id, log_level, log_type, log_content, created_at)"
        " VALUES (NULL, ?, 'system', ?, ?)",
        (
            "info" if passed == len(results) else "warning",
            f"管理员 {user['username']} 执行工具自检：{passed}/{len(results)} 通过",
            utc_now_iso(),
        ),
    )
    return {
        "status": "ok" if passed == len(results) else "partial",
        "message": f"工具自检完成：{passed}/{len(results)} 项通过",
        "passed": passed,
        "total": len(results),
        "results": results,
    }


@router.get("/files", summary="查看存储目录占用（扩展接口）")
async def storage_overview(user: session_auth.AdminUser) -> dict:
    def scan(kind: str) -> dict[str, Any]:
        root = storage.kind_dir(kind)
        files = 0
        size = 0
        if root.exists():
            for path in root.rglob("*"):
                if path.is_file():
                    files += 1
                    try:
                        size += path.stat().st_size
                    except OSError:
                        pass
        return {"kind": kind, "path": str(root), "files": files, "bytes": size}

    return {"directories": [scan(kind) for kind in storage.KINDS]}
