"""向 system_configs 写入默认配置。

仅在键不存在时插入，不覆盖管理员的修改，可重复执行。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database  # noqa: E402
from backend.utils import utc_now_iso  # noqa: E402


async def seed_system_configs() -> int:
    now = utc_now_iso()
    inserted = 0
    for key, value in config.CONFIG_DEFAULTS.items():
        exists = await database.fetch_value(
            "SELECT 1 FROM system_configs WHERE config_key = ?", (key,)
        )
        if exists:
            continue
        await database.execute(
            "INSERT INTO system_configs (config_key, config_value, config_group, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (key, value, config.config_group_of(key), now),
        )
        inserted += 1
    return inserted


async def main() -> None:
    await database.init_db()
    count = await seed_system_configs()
    print(f"[seed_system] 新增默认配置 {count} 项（已存在的不覆盖）")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
