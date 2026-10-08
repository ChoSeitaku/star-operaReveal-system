"""配置中心。

优先级（由低到高）：内置默认值 → .env 文件 → system_configs 表 → 进程环境变量。
即：进程环境变量优先级最高，其次数据库配置，再是 .env，最后是内置默认值。
某一层取到空值时视为「该层未设置」，继续向下层回退（否则播种进数据库的空默认值
会把 .env 层永久遮蔽）。被进程环境变量强制覆盖的键会在管理台标注为不可编辑。

可热更新的配置键统一定义在 CONFIG_DEFAULTS，分 4 组：
    llm / auth / analysis / upload（与 system_configs.config_group 一致）。
服务端口、数据目录等进程级设置不可热更新，仅从环境变量读取。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Iterable

from dotenv import dotenv_values

# ---------------------------------------------------------------- 路径常量

BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "db"
FRONTEND_DIR = BASE_DIR / "frontend"

# ---------------------------------------------------------------- 默认配置

CONFIG_DEFAULTS: dict[str, str] = {
    # ---- llm ----
    "llm.base_url": "https://api.deepseek.com/anthropic",
    "llm.api_key": "",
    "llm.model": "deepseek-v4-pro",
    "llm.max_tokens": "4096",
    "llm.max_agent_steps": "14",
    "llm.timeout_seconds": "180",
    "llm.temperature": "0.2",
    # ---- auth ----
    "auth.auth_center_url": "http://localhost:8001",
    "auth.client_id": "star-demo",
    "auth.client_secret": "star-demo-secret",
    "auth.session_ttl_seconds": "86400",
    "auth.ws_token_ttl_seconds": "60",
    # ---- analysis ----
    "analysis.engine_mode": "demo",  # demo | llm
    "analysis.max_concurrent_tasks": "2",
    "analysis.summarize_after_messages": "12",
    "analysis.summarize_after_chars": "24000",
    "analysis.context_recent_messages": "10",
    "analysis.max_attachment_chars": "4000",
    "analysis.max_attachments_in_context": "5",
    "analysis.scenario_db_dir": "db/demo",
    "analysis.tools_enabled": (
        "db_query,file_read,file_write,text_search,run_command,list_databases"
    ),
    "analysis.command_timeout": "30",
    "analysis.command_output_cap": "65536",
    "analysis.db_query_max_rows": "200",
    "analysis.db_query_timeout": "30",
    "analysis.file_read_max_bytes": "1048576",
    "analysis.file_write_max_bytes": "204800",
    "analysis.text_search_max_hits": "50",
    # ---- upload ----
    "upload.max_file_size_mb": "20",
    "upload.allowed_extensions": "csv,txt,md,json,log,xlsx",
    "upload.preview_chars": "5000",
}

# 环境变量 → 配置键映射（用于 .env 与进程环境变量两个层级）
ENV_TO_CONFIG: dict[str, str] = {
    "ANTHROPIC_BASE_URL": "llm.base_url",
    "ANTHROPIC_AUTH_TOKEN": "llm.api_key",
    "ANTHROPIC_API_KEY": "llm.api_key",
    "ANTHROPIC_MODEL": "llm.model",
    "AUTH_CENTER_URL": "auth.auth_center_url",
    "OAUTH_CLIENT_ID": "auth.client_id",
    "OAUTH_CLIENT_SECRET": "auth.client_secret",
    "SESSION_TTL_SECONDS": "auth.session_ttl_seconds",
    "WS_TOKEN_TTL_SECONDS": "auth.ws_token_ttl_seconds",
    "ANALYSIS_ENGINE_MODE": "analysis.engine_mode",
    "ANALYSIS_MAX_CONCURRENT_TASKS": "analysis.max_concurrent_tasks",
    "UPLOAD_MAX_FILE_SIZE_MB": "upload.max_file_size_mb",
}

# ---------------------------------------------------------------- 内部状态

_lock = threading.RLock()
CONFIG: dict[str, str] = {}


def _load_dotenv_file() -> dict[str, str]:
    """读取 .env（不注入 os.environ，以便与进程环境变量区分优先级）。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return {}
    try:
        return {k: v for k, v in dotenv_values(env_path).items() if v is not None}
    except Exception:
        return {}


def _build(db_rows: dict[str, str] | None = None) -> dict[str, str]:
    """按优先级合成配置字典。"""
    env_file = _load_dotenv_file()
    merged = dict(CONFIG_DEFAULTS)

    # 1) .env 覆盖默认值
    for env_key, cfg_key in ENV_TO_CONFIG.items():
        val = env_file.get(env_key)
        if val:
            merged[cfg_key] = val

    # 2) 数据库配置覆盖 .env（仅接受已声明的键）
    #    空值视为「该层未设置」而直接跳过：默认配置会以空值播种进 system_configs，
    #    若把空值当作有效覆盖，.env 层就会被永久遮蔽而失效。
    for key, val in (db_rows or {}).items():
        if key in CONFIG_DEFAULTS and val is not None and str(val).strip():
            merged[key] = str(val)

    # 3) 进程环境变量优先级最高
    for env_key, cfg_key in ENV_TO_CONFIG.items():
        val = os.environ.get(env_key)
        if val:
            merged[cfg_key] = val

    return merged


def load_initial() -> dict[str, str]:
    """进程启动时同步加载（此时还未读数据库）。"""
    global CONFIG
    with _lock:
        CONFIG = _build(None)
    return CONFIG


async def reload_config() -> dict[str, str]:
    """重新读取 system_configs 并重建配置（配置热更新入口）。"""
    from backend import database  # 延迟导入避免循环依赖

    try:
        rows = await database.fetch_all("SELECT config_key, config_value FROM system_configs")
        db_rows = {r["config_key"]: r["config_value"] for r in rows}
    except Exception:
        db_rows = {}

    global CONFIG
    with _lock:
        CONFIG = _build(db_rows)
    return CONFIG


# ---------------------------------------------------------------- 读取接口


def get(key: str, default: Any = None) -> str:
    with _lock:
        if key in CONFIG:
            return CONFIG[key]
    if key in CONFIG_DEFAULTS:
        return CONFIG_DEFAULTS[key]
    return default


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(str(get(key)).strip())
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(str(get(key)).strip())
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = str(get(key, "")).strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off", ""):
        return False
    return default


def get_list(key: str, default: Iterable[str] = ()) -> list[str]:
    raw = str(get(key, "") or "")
    items = [p.strip() for p in raw.split(",") if p.strip()]
    return items or list(default)


def config_group_of(key: str) -> str:
    """由配置键前缀推导分组（与 system_configs.config_group 对应）。"""
    prefix = key.split(".", 1)[0]
    return prefix if prefix in ("llm", "auth", "analysis", "upload") else "analysis"


def env_overridden_keys() -> set[str]:
    """返回当前被进程环境变量强制覆盖的配置键。

    这些键在管理台可以修改并写入数据库，但不会生效（环境变量优先级最高），
    因此需要在界面上明确标注，避免「改了却没反应」的误解。
    """
    forced: set[str] = set()
    for env_key, cfg_key in ENV_TO_CONFIG.items():
        if os.environ.get(env_key):
            forced.add(cfg_key)
    return forced


def snapshot(mask_secrets: bool = True) -> list[dict[str, str]]:
    """导出当前生效配置（供管理端查看）。"""
    with _lock:
        current = dict(CONFIG)
    forced = env_overridden_keys()

    out: list[dict[str, str]] = []
    for key in CONFIG_DEFAULTS:
        val = current.get(key, CONFIG_DEFAULTS[key])
        masked = bool(mask_secrets and _is_secret(key) and val)
        out.append(
            {
                "config_key": key,
                "config_value": "******" if masked else val,
                "config_group": config_group_of(key),
                "is_default": current.get(key, CONFIG_DEFAULTS[key]) == CONFIG_DEFAULTS[key],
                "overridden_by_env": key in forced,
                "editable": key not in forced,
            }
        )
    return out


def _is_secret(key: str) -> bool:
    return any(tok in key for tok in ("api_key", "client_secret", "secret", "token"))


# ---------------------------------------------------------------- 进程级设置
# 以下设置需重启进程才能生效，不做热更新，仅从 .env / 环境变量读取。

_env_file_cache: dict[str, str] | None = None


def _server_setting(name: str, default: str) -> str:
    global _env_file_cache
    val = os.environ.get(name)
    if val:
        return val
    if _env_file_cache is None:
        _env_file_cache = _load_dotenv_file()
    val = _env_file_cache.get(name)
    return val if val else default


def server_setting(name: str, default: str = "") -> str:
    """读取进程级设置（.env → 环境变量，优先级：环境变量 > .env > 默认值）。"""
    return _server_setting(name, default)


def backend_port() -> int:
    return int(_server_setting("BACKEND_PORT", "8000"))


def auth_port() -> int:
    return int(_server_setting("AUTH_PORT", "8001"))


def public_base_url() -> str:
    """浏览器访问后端的基地址，用于拼接 OAuth redirect_uri。

    必须与认证中心白名单一致：localhost 与 127.0.0.1 不可混用。
    """
    return _server_setting("PUBLIC_BASE_URL", f"http://localhost:{backend_port()}").rstrip("/")


def data_dir() -> Path:
    raw = _server_setting("DATA_DIR", "data")
    p = Path(raw)
    return p if p.is_absolute() else (BASE_DIR / p)


def scenario_db_dir() -> Path:
    """情景演示库目录（db_query 工具唯一可访问的数据库来源）。"""
    raw = get("analysis.scenario_db_dir", "db/demo")
    p = Path(raw)
    return p if p.is_absolute() else (BASE_DIR / p)


load_initial()
