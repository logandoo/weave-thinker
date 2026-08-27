#!/usr/bin/env python3
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
重置迁移状态，保留 concepts，清除 migration_completed_at 和 progress。
重跑时会跳过已有 concepts，只为它们重新生成 episodes。

用法:
    CLEANUP_DB_URL="postgresql+asyncpg://..." python -m scripts.reset_migration [--execute]
"""

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

sys.path.insert(0, ".")

_DATABASE_URL = os.environ.get("CLEANUP_DB_URL")
_DRY_RUN = "--execute" not in sys.argv


def _get_session_factory():
    if _DATABASE_URL:
        engine = create_async_engine(_DATABASE_URL, pool_size=2, max_overflow=0)
        return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    from app.db.database import AsyncSessionLocal
    return AsyncSessionLocal


async def main():
    print(f"模式: {'DRY-RUN' if _DRY_RUN else 'EXECUTE'}")
    print("=" * 60)

    SessionLocal = _get_session_factory()
    async with SessionLocal() as db:
        # 统计当前状态
        result = await db.execute(text("""
            SELECT COUNT(*) FROM user_agent_states 
            WHERE metadata_json::text LIKE '%migration_completed_at%'
        """))
        completed = result.scalar()

        result2 = await db.execute(text("""
            SELECT COUNT(*) FROM memory_concepts WHERE source_type = 'migration' AND valid_to IS NULL
        """))
        concepts = result2.scalar()

        result3 = await db.execute(text("""
            SELECT COUNT(*) FROM memory_episodes WHERE source_type = 'migration'
        """))
        episodes = result3.scalar()

        print(f"当前状态:")
        print(f"  已完成迁移的用户: {completed}")
        print(f"  迁移 concepts: {concepts}")
        print(f"  迁移 episodes: {episodes}")
        print()

        if not _DRY_RUN:
            # 清除 migration_completed_at 和 migration progress
            await db.execute(text("""
                UPDATE user_agent_states 
                SET metadata_json = (metadata_json::jsonb 
                                  - 'migration_completed_at' 
                                  - 'migrated_concept_count' 
                                  - 'migrated_episode_count'
                                  - 'migration')::text
                WHERE metadata_json::text LIKE '%migration_completed_at%'
            """))
            print(f"  已清除 {completed} 个用户的迁移标记")

            # 删除剩余的迁移 episodes（让重跑时重新生成）
            await db.execute(text("""
                DELETE FROM memory_episodes WHERE source_type = 'migration'
            """))
            print(f"  已删除 {episodes} 个迁移 episodes")

            await db.commit()
            print("\n重置完成。重启后端将自动重跑迁移。")
            print("重跑时会跳过已有 concepts，只为它们重新生成 episodes。")
        else:
            print("[DRY-RUN] 加 --execute 执行重置")
            print(f"  将清除 {completed} 个用户的迁移标记")
            print(f"  将删除 {episodes} 个迁移 episodes")
            print(f"  保留 {concepts} 个迁移 concepts")


if __name__ == "__main__":
    asyncio.run(main())
