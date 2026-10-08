"""分析工具集：数据库查询、文件读写、文本检索、命令执行、库结构查看。

安全边界：
- db_query 只能打开 analysis.scenario_db_dir 下的库（system.db 在结构上不可达），
  且一律以 mode=ro + PRAGMA query_only 打开，仅允许单条 SELECT/WITH/EXPLAIN。
- file_read / text_search 限定在 本会话的 uploads 与 workspace 目录内。
- file_write 只允许写 workspace 目录，文件名经过清洗。
- run_command 仅放行白名单可执行文件，工作目录固定为 workspace，环境变量白名单化。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend import config, database, storage
from backend.utils import truncate

# ---------------------------------------------------------------- 工具声明

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_databases",
        "description": "列出可查询的情景数据库及其表结构与行数。开始分析前应先调用它了解有哪些数据可用。",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "db_query",
        "description": (
            "对情景数据库执行一条只读 SQL（仅支持单条 SELECT / WITH / EXPLAIN）。"
            "返回 JSON：{columns, rows, row_count, truncated}。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "数据库名，如 catalog、customer_behavior"},
                "sql": {"type": "string", "description": "单条只读 SQL 语句"},
            },
            "required": ["database", "sql"],
        },
    },
    {
        "name": "file_read",
        "description": "读取本会话 uploads 或 workspace 目录下的文本文件内容（可传相对文件名）。",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径或文件名"}},
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": "把中间分析笔记写入本会话 workspace 目录，返回相对路径。",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "文件名，如 notes.md"},
                "content": {"type": "string", "description": "写入内容"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "text_search",
        "description": "在本会话 uploads 与 workspace 的文本文件中检索关键词，返回命中行与上下文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词；以 re: 开头表示正则"},
                "path": {"type": "string", "description": "可选，限定检索的文件"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_command",
        "description": (
            f"在会话 workspace 目录下执行受限命令。仅允许：python（当前解释器）"
            f"{'、sqlite3' if shutil.which('sqlite3') else ''}。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "完整命令行"}},
            "required": ["command"],
        },
    },
]

SUBMIT_RESULT_TOOL: dict[str, Any] = {
    "name": "submit_result",
    "description": (
        "提交最终的结构化归因分析结论。这是分析的最后一步，必须调用且只调用一次。"
        "六个部分都要填写完整，结论与建议使用中文。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "problem_definition": {"type": "string", "description": "本次分析回答的业务问题（一句话）"},
            "key_metrics": {
                "type": "array",
                "description": "关键指标列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric_name": {"type": "string"},
                        "metric_value": {"type": "string"},
                        "metric_unit": {"type": "string"},
                        "metric_period": {"type": "string"},
                    },
                    "required": ["metric_name", "metric_value"],
                },
            },
            "evidence_list": {
                "type": "array",
                "description": "证据列表，每条证据须标注来源与置信度",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_type": {"type": "string", "description": "如 数据库查询 / 附件 / 计算结果"},
                        "source_name": {"type": "string", "description": "如 catalog.db / 文件名 / SQL 摘要"},
                        "evidence_text": {"type": "string"},
                        "related_metric": {"type": "string"},
                        "confidence": {"type": "string", "description": "high / medium / low"},
                    },
                    "required": ["source_type", "source_name", "evidence_text"],
                },
            },
            "conclusion_text": {"type": "string", "description": "归因结论：主要原因与影响范围"},
            "missing_data_text": {"type": "string", "description": "仍缺失、需要补充的数据项"},
            "next_action_text": {"type": "string", "description": "下一步建议，至少两条，用换行或分号分隔"},
        },
        "required": [
            "problem_definition",
            "key_metrics",
            "evidence_list",
            "conclusion_text",
            "missing_data_text",
            "next_action_text",
        ],
    },
}


@dataclass
class ToolContext:
    """工具执行所需的会话级目录与开关。"""

    workspace_dir: Path
    upload_dir: Path
    enabled: list[str]


class ToolError(Exception):
    """工具执行失败（返回给模型的可见错误信息）。"""


# ---------------------------------------------------------------- 情景库


_db_cache: list[dict[str, Any]] | None = None


def scenario_databases() -> list[Path]:
    directory = config.scenario_db_dir()
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.db") if p.is_file())


def describe_databases(refresh: bool = False) -> list[dict[str, Any]]:
    """列出情景库的表结构（带缓存，配置重载时刷新）。"""
    global _db_cache
    if _db_cache is not None and not refresh:
        return _db_cache

    described: list[dict[str, Any]] = []
    for path in scenario_databases():
        info: dict[str, Any] = {"name": path.stem, "file": path.name, "tables": []}
        try:
            conn = _sync_connect_readonly(path)
            try:
                names = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                for table in names:
                    columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                    count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    info["tables"].append({"name": table, "columns": columns, "row_count": count})
            finally:
                conn.close()
        except Exception as exc:  # 库损坏也不应让整个工具失败
            info["error"] = str(exc)
        described.append(info)

    _db_cache = described
    return described


def invalidate_db_cache() -> None:
    global _db_cache
    _db_cache = None


def _sync_connect_readonly(path: Path):
    import sqlite3

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _resolve_db(name: str) -> Path | None:
    if not name:
        return None
    target = name.strip()
    for path in scenario_databases():
        if path.name == target or path.stem == target or path.stem == Path(target).stem:
            return path
    return None


def schema_text() -> str:
    """把库结构渲染成提示词片段。"""
    lines: list[str] = []
    for info in describe_databases():
        if not info["tables"]:
            continue
        lines.append(f"【{info['name']}】{info['file']}")
        for table in info["tables"]:
            lines.append(
                f"  - {table['name']}({', '.join(table['columns'])})  约 {table['row_count']} 行"
            )
    return "\n".join(lines) if lines else "（未发现可查询的情景数据库）"


# ---------------------------------------------------------------- 单条语句校验

_ALLOWED_PREFIXES = ("select", "with", "explain")
_FORBIDDEN_KEYWORDS = ("insert", "update", "delete", "drop", "alter", "attach", "detach", "create", "replace", "vacuum")


def _validate_sql(sql: str) -> str:
    body = sql.strip().rstrip(";").strip()
    if not body:
        raise ToolError("SQL 不能为空")
    if ";" in body:
        raise ToolError("只允许单条语句（检测到分号）")
    lowered = body.lower()
    if not lowered.startswith(_ALLOWED_PREFIXES):
        raise ToolError("只允许 SELECT / WITH / EXPLAIN 只读查询")
    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", lowered):
            raise ToolError(f"SQL 中包含被禁止的关键字：{keyword}")
    return body


# ---------------------------------------------------------------- 路径解析


def _resolve_readable(ctx: ToolContext, raw_path: str) -> Path:
    """把用户/模型给出的路径解析到 uploads 或 workspace 内的真实文件。"""
    if not raw_path:
        raise ToolError("路径不能为空")

    candidate = Path(raw_path)
    roots = [ctx.upload_dir, ctx.workspace_dir]
    attempts: list[Path] = []
    if candidate.is_absolute():
        attempts.append(candidate)
    else:
        attempts.extend(root / candidate for root in roots)

    for path in attempts:
        if not path.exists():
            continue
        if not any(storage.is_within(path, root) for root in roots):
            raise ToolError("路径越界：只允许访问本会话的上传目录与工作目录")
        if path.is_dir():
            raise ToolError("目标是目录，不是文件")
        return path
    raise ToolError(f"文件不存在：{raw_path}")


def _safe_workspace_path(ctx: ToolContext, filename: str) -> Path:
    clean = storage.sanitize_filename(filename, fallback="note.txt")
    target = storage.safe_join(ctx.workspace_dir, clean)
    ctx.workspace_dir.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------- 工具实现


async def _tool_list_databases(ctx: ToolContext) -> str:
    infos = describe_databases()
    if not infos:
        return "当前没有可查询的情景数据库。"
    return json.dumps(
        [
            {
                "database": info["name"],
                "file": info["file"],
                "tables": [
                    {"table": t["name"], "columns": t["columns"], "row_count": t["row_count"]}
                    for t in info["tables"]
                ],
            }
            for info in infos
        ],
        ensure_ascii=False,
    )


async def _tool_db_query(ctx: ToolContext, args: dict[str, Any]) -> str:
    db_name = str(args.get("database") or "").strip()
    sql = str(args.get("sql") or "")

    path = _resolve_db(db_name)
    if path is None:
        available = ", ".join(p.stem for p in scenario_databases()) or "无"
        raise ToolError(f"数据库不存在：{db_name}（可用：{available}）")

    body = _validate_sql(sql)
    max_rows = config.get_int("analysis.db_query_max_rows", 200)
    timeout = config.get_int("analysis.db_query_timeout", 30)

    async def _run() -> dict[str, Any]:
        async with database.connect(path, readonly=True) as conn:
            cursor = await conn.execute(body)
            columns = [d[0] for d in (cursor.description or [])]
            rows = await cursor.fetchmany(max_rows + 1)
            await cursor.close()
        truncated = len(rows) > max_rows
        return {
            "database": path.stem,
            "columns": columns,
            "rows": [list(row) for row in rows[:max_rows]],
            "row_count": min(len(rows), max_rows),
            "truncated": truncated,
        }

    try:
        payload = await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ToolError(f"查询超时（>{timeout}s）") from exc
    except Exception as exc:
        raise ToolError(f"SQL 执行失败：{exc}") from exc

    return json.dumps(payload, ensure_ascii=False, default=str)


async def _tool_file_read(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = _resolve_readable(ctx, str(args.get("path") or ""))
    max_bytes = config.get_int("analysis.file_read_max_bytes", 1048576)
    size = path.stat().st_size
    if size > max_bytes:
        raise ToolError(f"文件过大（{size} 字节，上限 {max_bytes} 字节）")

    from backend.attachments.parser import decode_text

    raw = await asyncio.to_thread(path.read_bytes)
    try:
        text, encoding = decode_text(raw)
    except Exception:
        return f"[二进制文件，{size} 字节，无法按文本解析]"

    limit = config.get_int("analysis.max_attachment_chars", 4000) * 3
    return json.dumps(
        {
            "file_name": path.name,
            "encoding": encoding,
            "char_count": len(text),
            "content": truncate(text, limit),
        },
        ensure_ascii=False,
    )


async def _tool_file_write(ctx: ToolContext, args: dict[str, Any]) -> str:
    filename = str(args.get("filename") or "")
    content = str(args.get("content") or "")
    if not filename:
        raise ToolError("filename 不能为空")

    data = content.encode("utf-8")
    max_bytes = config.get_int("analysis.file_write_max_bytes", 204800)
    if len(data) > max_bytes:
        raise ToolError(f"内容过大（{len(data)} 字节，上限 {max_bytes} 字节）")

    target = _safe_workspace_path(ctx, filename)
    await asyncio.to_thread(target.write_bytes, data)
    relative = target.relative_to(ctx.workspace_dir).as_posix()
    return json.dumps(
        {"written": True, "relative_path": relative, "bytes": len(data)}, ensure_ascii=False
    )


async def _tool_text_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    query = str(args.get("query") or "")
    if not query:
        raise ToolError("query 不能为空")

    raw_path = str(args.get("path") or "").strip()
    if raw_path:
        files = [_resolve_readable(ctx, raw_path)]
    else:
        files = []
        for root in (ctx.upload_dir, ctx.workspace_dir):
            if root.exists():
                files.extend(
                    p for p in sorted(root.rglob("*"))
                    if p.is_file() and not p.name.endswith(".meta.json")
                )

    use_regex = query.startswith("re:")
    pattern = None
    if use_regex:
        try:
            pattern = re.compile(query[3:])
        except re.error as exc:
            raise ToolError(f"正则表达式无效：{exc}") from exc

    from backend.attachments.parser import decode_text

    max_hits = config.get_int("analysis.text_search_max_hits", 50)
    hits: list[dict[str, Any]] = []
    searched: list[str] = []

    for path in files:
        if len(hits) >= max_hits:
            break
        try:
            if path.stat().st_size > config.get_int("analysis.file_read_max_bytes", 1048576):
                continue
            text, _ = decode_text(await asyncio.to_thread(path.read_bytes))
        except Exception:
            continue
        searched.append(path.name)
        for lineno, line in enumerate(text.splitlines(), start=1):
            matched = bool(pattern.search(line)) if pattern else (query in line)
            if not matched:
                continue
            hits.append(
                {
                    "file_name": path.name,
                    "line_no": lineno,
                    "line": truncate(line.strip(), 200),
                }
            )
            if len(hits) >= max_hits:
                break

    return json.dumps(
        {"query": query, "searched_files": searched, "hit_count": len(hits), "hits": hits},
        ensure_ascii=False,
    )


_COMMAND_ENV_WHITELIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "HOME", "NUMBER_OF_PROCESSORS", "LANG",
)


async def _tool_run_command(ctx: ToolContext, args: dict[str, Any]) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ToolError("command 不能为空")

    try:
        parts = shlex_split(command)
    except ValueError as exc:
        raise ToolError(f"命令解析失败：{exc}") from exc
    if not parts:
        raise ToolError("command 不能为空")

    allowed = _allowed_executables()
    head = Path(parts[0]).name.lower()
    head = re.sub(r"\.(exe|cmd|bat)$", "", head)
    if head not in allowed:
        raise ToolError(
            f"命令不在白名单内：{parts[0]}（允许：{', '.join(sorted(allowed))}）"
        )
    parts[0] = allowed[head]

    ctx.workspace_dir.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in _COMMAND_ENV_WHITELIST if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    timeout = config.get_int("analysis.command_timeout", 30)
    output_cap = config.get_int("analysis.command_output_cap", 65536)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    try:
        process = await asyncio.create_subprocess_exec(
            *parts,
            cwd=str(ctx.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            creationflags=creationflags,
        )
    except (OSError, NotImplementedError) as exc:
        raise ToolError(f"无法启动进程：{exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise ToolError(f"命令执行超时（>{timeout}s）")

    def _decode(raw: bytes) -> str:
        from backend.attachments.parser import decode_text

        text, _ = decode_text(raw)
        return truncate(text, output_cap)

    return json.dumps(
        {
            "exit_code": process.returncode,
            "stdout": _decode(stdout),
            "stderr": _decode(stderr),
        },
        ensure_ascii=False,
    )


def _allowed_executables() -> dict[str, str]:
    mapping = {"python": sys.executable, "python3": sys.executable}
    sqlite_cli = shutil.which("sqlite3")
    if sqlite_cli:
        mapping["sqlite3"] = sqlite_cli
    return mapping


def shlex_split(command: str) -> list[str]:
    """按 shell 规则切分，但 Windows 路径中的反斜杠不做转义处理。"""
    import shlex

    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return [token.strip('"').strip("'") for token in lexer]


# ---------------------------------------------------------------- 调度


_HANDLERS = {
    "list_databases": lambda ctx, args: _tool_list_databases(ctx),
    "db_query": _tool_db_query,
    "file_read": _tool_file_read,
    "file_write": _tool_file_write,
    "text_search": _tool_text_search,
    "run_command": _tool_run_command,
}


def enabled_tool_names() -> list[str]:
    configured = config.get_list("analysis.tools_enabled", [])
    if configured == ["none"]:
        return []
    return [name for name in _HANDLERS if name in configured]


def available_tool_schemas() -> list[dict[str, Any]]:
    enabled = set(enabled_tool_names())
    return [schema for schema in TOOL_SCHEMAS if schema["name"] in enabled]


async def execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
    """执行工具，返回文本结果（错误也以文本返回，便于模型自我修正）。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"错误：未知工具 {name}"
    if name not in enabled_tool_names():
        return f"错误：工具 {name} 已被管理员停用"

    try:
        return await handler(ctx, arguments or {})
    except ToolError as exc:
        return f"错误：{exc}"
    except Exception as exc:  # 工具异常不应中断任务
        return f"错误：{name} 执行异常：{exc}"
