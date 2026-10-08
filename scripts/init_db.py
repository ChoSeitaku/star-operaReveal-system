#!/usr/bin/env python3
"""一键初始化：建表 → 写入默认配置 → 生成 6 个情景演示库。可重复执行（幂等）。

用法：
    python scripts/init_db.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, database, storage  # noqa: E402
from db import seed_scenarios, seed_system  # noqa: E402


async def main() -> None:
    print("[init_db] 项目根目录:", config.BASE_DIR)

    storage.ensure_all()
    print("[init_db] 数据目录已就绪:", config.data_dir())

    await database.init_db()
    print("[init_db] 系统库建表完成:", database.system_db_path())

    inserted = await seed_system.seed_system_configs()
    print(f"[init_db] 默认配置写入完成（新增 {inserted} 项）")

    await config.reload_config()
    print("[init_db] 配置已加载，共", len(config.CONFIG_DEFAULTS), "项")

    seed_scenarios.build_all(verbose=True)
    print("[init_db] 情景演示库已生成于:", config.scenario_db_dir())

    print("[init_db] 初始化完成 ✔")


if __name__ == "__main__":
    asyncio.run(main())
