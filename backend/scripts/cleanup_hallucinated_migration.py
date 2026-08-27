#!/usr/bin/env python3
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
清洗迁移产生的虚构数据（pre-2026 日期的 episodes 和关联 concepts）。

用法:
    cd backend && python -m scripts.cleanup_hallucinated_migration [--dry-run]

默认 dry-run 模式，只报告不删除。加 --execute 执行删除。
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

# 项目根目录
sys.path.insert(0, ".")

# 优先使用环境变量或命令行指定的数据库 URL
_DATABASE_URL = os.environ.get("CLEANUP_DB_URL")


def _get_session_factory():
    if _DATABASE_URL:
        engine = create_async_engine(_DATABASE_URL, pool_size=2, max_overflow=0)
        return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    from app.db.database import AsyncSessionLocal
    return AsyncSessionLocal

_SYSTEM_LAUNCH = datetime(2026, 1, 1)
# 只匹配 2025 年及以前的虚构日期（不匹配 2026+）
_PRE_2026_RE = re.compile(r"20(?:[01]\d|2[0-5])年")
_DRY_RUN = "--execute" not in sys.argv


async def find_hallucinated_episodes(db: AsyncSession) -> list[dict]:
    """找出所有虚构的迁移 episode：
    1. narrative 文本中包含 pre-2026 年份
    2. valid_from 早于 2026-01-01
    """
    result = await db.execute(text("""
        SELECT id, user_id, valid_from, substring(narrative, 1, 120) as preview, created_at
        FROM memory_episodes
        WHERE source_type = 'migration'
          AND (
            narrative ~ '20[01][0-9]年' OR narrative ~ '202[0-5]年'
            OR narrative ~ '20[01][0-9]-' OR narrative ~ '202[0-5]-'
            OR valid_from < '2026-01-01'
          )
        ORDER BY created_at
    """))
    rows = result.fetchall()
    return [{"id": r[0], "user_id": r[1], "valid_from": r[2], "preview": r[3], "created_at": r[4]} for r in rows]


async def find_orphaned_concepts(
    db: AsyncSession, deleted_episode_ids: list[str],
) -> list[dict]:
    """找出仅被已删除 episode 引用、且无其他引用的迁移 concept。"""
    if not deleted_episode_ids:
        return []

    # 收集所有被删除 episode 引用的 concept id
    result = await db.execute(text("""
        SELECT DISTINCT jsonb_array_elements_text(source_concept_ids::jsonb) as cid
        FROM memory_episodes
        WHERE id = ANY(:ids)
    """), {"ids": deleted_episode_ids})
    candidate_cids = [r[0] for r in result.fetchall()]
    if not candidate_cids:
        return []

    # 对每个 candidate，检查是否还有其他 episode 引用它
    orphans = []
    for cid in candidate_cids:
        ref_count = await db.execute(text("""
            SELECT COUNT(*) FROM memory_episodes
            WHERE source_type = 'migration'
              AND id != ALL(:del_ids)
              AND source_concept_ids::jsonb ? :cid
        """), {"del_ids": deleted_episode_ids, "cid": cid})
        if ref_count.scalar() == 0:
            # 也检查非迁移 episode 是否引用
            ref_count2 = await db.execute(text("""
                SELECT COUNT(*) FROM memory_episodes
                WHERE source_type != 'migration'
                  AND source_concept_ids::jsonb ? :cid
            """), {"cid": cid})
            if ref_count2.scalar() == 0:
                row = await db.execute(text("""
                    SELECT id, user_id, canonical_name, description_short
                    FROM memory_concepts WHERE id::text = :cid
                """), {"cid": cid})
                r = row.fetchone()
                if r:
                    orphans.append({
                        "id": r[0], "user_id": r[1],
                        "name": r[2], "short": r[3],
                    })
    return orphans


async def find_concepts_with_hallucinated_dates(db: AsyncSession) -> list[dict]:
    """找出 description_full 中包含 pre-2026 日期的迁移 concept（供人工审查）。"""
    result = await db.execute(text("""
        SELECT id, user_id, canonical_name,
               substring(description_full, 1, 150) as preview
        FROM memory_concepts
        WHERE source_type = 'migration'
          AND valid_to IS NULL
          AND (description_full ~ '20[01][0-9]年' OR description_full ~ '202[0-5]年'
               OR description_full ~ '20[01][0-9]-' OR description_full ~ '202[0-5]-')
        ORDER BY created_at
    """))
    rows = result.fetchall()
    return [{"id": r[0], "user_id": r[1], "name": r[2], "preview": r[3]} for r in rows]


async def main():
    print(f"模式: {'DRY-RUN (只报告)' if _DRY_RUN else 'EXECUTE (执行删除)'}")
    if _DATABASE_URL:
        print(f"数据库: {_DATABASE_URL.split('@')[1] if '@' in _DATABASE_URL else 'custom'}")
    else:
        print("数据库: config.toml (本地)")
    print("=" * 70)

    SessionLocal = _get_session_factory()
    async with SessionLocal() as db:
        # 1. 找虚构 episodes
        episodes = await find_hallucinated_episodes(db)
        print(f"\n[1] 虚构 episodes (narrative 含 pre-2026 日期 或 valid_from < 2026): {len(episodes)} 条")
        for ep in episodes[:10]:
            vf = ep.get('valid_from', '')
            vf_str = f" [valid_from={vf}]" if vf else ""
            print(f"  - [{ep['user_id'][:8]}]{vf_str} {ep['preview'][:70]}...")
        if len(episodes) > 10:
            print(f"  ... 还有 {len(episodes) - 10} 条")

        ep_ids = [ep["id"] for ep in episodes]

        # 2. 找孤立 concepts
        orphans = await find_orphaned_concepts(db, ep_ids)
        print(f"\n[2] 孤立 concepts (仅被虚构 episode 引用): {len(orphans)} 条")
        for c in orphans:
            print(f"  - [{c['user_id'][:8]}] {c['name']}: {c['short'][:60]}")

        # 3. 找 description_full 含虚构日期的 concepts
        date_concepts = await find_concepts_with_hallucinated_dates(db)
        print(f"\n[3] description_full 含 pre-2026 日期的 concepts (需人工审查): {len(date_concepts)} 条")
        for c in date_concepts[:10]:
            print(f"  - [{c['user_id'][:8]}] {c['name']}: {c['preview'][:80]}...")
        if len(date_concepts) > 10:
            print(f"  ... 还有 {len(date_concepts) - 10} 条")

        # 4. 按用户汇总
        user_counts: dict[str, int] = {}
        for ep in episodes:
            uid = ep["user_id"]
            user_counts[uid] = user_counts.get(uid, 0) + 1
        print(f"\n[4] 按用户汇总虚构 episodes:")
        for uid, cnt in sorted(user_counts.items(), key=lambda x: -x[1]):
            print(f"  - {uid}: {cnt} 条")

        # 5. 执行删除
        if not _DRY_RUN:
            print("\n" + "=" * 70)
            print("开始执行删除...")

            # 删除虚构 episodes（孤立 concepts 保留——它们本身是事实性数据）
            if ep_ids:
                await db.execute(text("""
                    DELETE FROM memory_episodes WHERE id = ANY(:ids)
                """), {"ids": ep_ids})
                print(f"  已删除 {len(ep_ids)} 个虚构 episodes")

            await db.commit()
            print("删除完成。")

            # 清理后验证
            remaining = await db.execute(text("""
                SELECT COUNT(*) FROM memory_episodes
                WHERE source_type = 'migration'
                  AND (
                    narrative ~ '20[01][0-9]年' OR narrative ~ '202[0-5]年'
                    OR narrative ~ '20[01][0-9]-' OR narrative ~ '202[0-5]-'
                    OR valid_from < '2026-01-01'
                  )
            """))
            print(f"\n清理后剩余虚构 episodes: {remaining.scalar()} 条")
        else:
            print("\n[DRY-RUN] 未执行任何删除。加 --execute 参数执行。")
            print(f"  将删除 {len(ep_ids)} 个虚构 episodes")
            print(f"  将保留 {len(orphans)} 个孤立 concepts（它们本身是事实性数据）")

        # 始终报告需要人工审查的 concepts
        if date_concepts:
            print(f"\n[!] 以下 {len(date_concepts)} 个 concepts 的 description_full 含 pre-2026 日期，")
            print("    建议人工审查后决定是否修正或删除：")
            for c in date_concepts:
                print(f"    {c['id']} | {c['name']} | {c['preview'][:60]}...")


if __name__ == "__main__":
    asyncio.run(main())
