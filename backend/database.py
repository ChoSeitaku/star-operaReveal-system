"""SQLite 访问层（aiosqlite 封装）。

约定：
- 每次操作使用独立短连接（aiosqlite 连接不可跨任务并发使用），用完即关；
- 统一开启 WAL、busy_timeout、外键约束；
- 写操作遇到 "database is locked" 时指数退避重试；
- 绝不在 LLM / 工具调用期间持有事务。
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

from backend import config

BUSY_TIMEOUT_MS = 10000
_WRITE_RETRIES = 5


def system_db_path() -> Path:
    return config.data_dir() / "system.db"


def _as_posix(path: str | Path) -> str:
    """SQLite URI 只接受正斜杠，Windows 反斜杠会破坏 file: URI。"""
    return Path(path).as_posix()


@asynccontextmanager
async def connect(path: str | Path | None = None, *, readonly: bool = False) -> AsyncIterator[aiosqlite.Connection]:
    """打开一个短连接。readonly=True 时以只读模式打开（用于情景库）。"""
    p = Path(path) if path is not None else system_db_path()
    if not readonly:
        p.parent.mkdir(parents=True, exist_ok=True)

    if readonly:
        conn = await aiosqlite.connect(f"file:{_as_posix(p)}?mode=ro", uri=True)
    else:
        conn = await aiosqlite.connect(_as_posix(p))

    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if readonly:
            await conn.execute("PRAGMA query_only = ON")
        else:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        await conn.close()


async def _retry(coro_factory):
    """对 "database is locked" 做指数退避重试。"""
    delay = 0.05
    for attempt in range(_WRITE_RETRIES):
        try:
            return await coro_factory()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" not in msg and "busy" not in msg) or attempt == _WRITE_RETRIES - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


async def fetch_all(sql: str, params: Sequence[Any] = (), *, path: str | Path | None = None) -> list[aiosqlite.Row]:
    async def _run():
        async with connect(path) as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
            return list(rows)

    return await _retry(_run)


async def fetch_one(sql: str, params: Sequence[Any] = (), *, path: str | Path | None = None) -> aiosqlite.Row | None:
    async def _run():
        async with connect(path) as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
            return row

    return await _retry(_run)


async def fetch_value(sql: str, params: Sequence[Any] = (), *, path: str | Path | None = None) -> Any:
    row = await fetch_one(sql, params, path=path)
    if row is None:
        return None
    return row[0]


async def execute(sql: str, params: Sequence[Any] = (), *, path: str | Path | None = None) -> tuple[int, int]:
    """执行单条写语句，返回 (lastrowid, rowcount)。"""

    async def _run():
        async with connect(path) as conn:
            cur = await conn.execute(sql, params)
            await conn.commit()
            rowid = cur.lastrowid or 0
            count = cur.rowcount
            await cur.close()
            return rowid, count

    return await _retry(_run)


async def execute_many(sql: str, seq: Iterable[Sequence[Any]], *, path: str | Path | None = None) -> int:
    rows = list(seq)

    async def _run():
        async with connect(path) as conn:
            cur = await conn.executemany(sql, rows)
            await conn.commit()
            count = cur.rowcount
            await cur.close()
            return count

    return await _retry(_run)


@asynccontextmanager
async def transaction(path: str | Path | None = None) -> AsyncIterator[aiosqlite.Connection]:
    """多语句原子操作：在同一连接上执行，退出时统一提交/回滚。"""
    async with connect(path) as conn:
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def init_db() -> None:
    """执行 db/init.sql 建表（幂等）。"""
    sql_path = config.DB_DIR / "init.sql"
    script = sql_path.read_text(encoding="utf-8")
    async with connect() as conn:
        await conn.executescript(script)
        await conn.commit()
