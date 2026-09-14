# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""C1/N16（2026-09-14）：逐轮召回台账服务。

只存元数据：query_hash / 候选 ids / 分层分 / 门控分 / 预算字符 / 注入字符 /
是否截断 / 耗时 / 缓存命中；**不存任何记忆内容**（帕累托 D6 隐私约束）。

写路径 fire-and-forget：独立会话、失败静默——台账永不阻塞或影响检索主流程
（帕累托约束 5 fail-open）。保留策略（30 天 + per-user 行数上限）只作用本表，
由 MemoryScheduler 周期执行（DC7）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from typing import Any, Optional

from sqlalchemy import text

from app.core.config import get_config
from app.db.database import AsyncSessionLocal, MemoryRecallLog

logger = logging.getLogger(__name__)

_write_tasks: set[asyncio.Task] = set()


def query_hash_of(user_queries: list[str]) -> str:
    """最近 3 条用户消息的稳定哈希（与检索会话缓存同源口径）。"""
    if not user_queries:
        return ""
    return hashlib.sha1("|".join(user_queries[-3:]).encode("utf-8")).hexdigest()[:16]


def should_sample(sample_rate: float) -> bool:
    """采样判定（纯函数）：≥1 恒真；≤0 恒假；否则随机。"""
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False
    return random.random() < sample_rate


def cleanup_statements(retention_days: int, max_per_user: int) -> list[tuple[str, dict]]:
    """保留策略 SQL（纯函数，便于单测）：只作用 memory_recall_log。"""
    stmts: list[tuple[str, dict]] = []
    if retention_days and retention_days > 0:
        stmts.append((
            "DELETE FROM memory_recall_log WHERE created_at < NOW() - (:days * INTERVAL '1 day')",
            {"days": int(retention_days)},
        ))
    if max_per_user and max_per_user > 0:
        stmts.append((
            "DELETE FROM memory_recall_log WHERE id IN ("
            "SELECT id FROM (SELECT id, row_number() OVER "
            "(PARTITION BY user_id ORDER BY created_at DESC, id DESC) AS rn "
            "FROM memory_recall_log) t WHERE t.rn > :cap)",
            {"cap": int(max_per_user)},
        ))
    return stmts


async def cleanup_recall_log(db, retention_days: Optional[int] = None,
                             max_per_user: Optional[int] = None) -> int:
    cfg = get_config().memory
    if retention_days is None:
        retention_days = int(cfg.get("recall_log_retention_days", 30))
    if max_per_user is None:
        max_per_user = int(cfg.get("recall_log_max_per_user", 20000))
    deleted = 0
    for stmt, params in cleanup_statements(retention_days, max_per_user):
        result = await db.execute(text(stmt), params)
        deleted += int(result.rowcount or 0)
    await db.commit()
    return deleted


def _json_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


async def _write_row(user_id: str, conversation_id: Optional[str], query_hash: str,
                     candidate_ids: Optional[list], tier_scores: Optional[dict],
                     gate_score: float, budget_chars: int, injected_chars: int,
                     truncated: bool, elapsed_ms: int, cache_hit: bool) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(MemoryRecallLog(
                user_id=user_id,
                conversation_id=conversation_id,
                query_hash=query_hash or None,
                candidate_ids=_json_or_none(candidate_ids),
                tier_scores=_json_or_none(tier_scores),
                gate_score=float(gate_score or 0.0),
                budget_chars=int(budget_chars or 0),
                injected_chars=int(injected_chars or 0),
                truncated=bool(truncated),
                elapsed_ms=int(elapsed_ms or 0),
                cache_hit=bool(cache_hit),
            ))
            await session.commit()
    except Exception:
        logger.debug("recall log write failed (silent)", exc_info=True)


def record_recall_log_bg(user_id: str, conversation_id: Optional[str], query_hash: str,
                         candidate_ids: Optional[list], tier_scores: Optional[dict],
                         gate_score: float, budget_chars: int, injected_chars: int,
                         truncated: bool, elapsed_ms: int, cache_hit: bool) -> None:
    """fire-and-forget：调用方永不等待；异常永不外泄。"""
    try:
        cfg = get_config().memory
        if not cfg.get("recall_log_enabled", True):
            return
        if not should_sample(float(cfg.get("recall_log_sample_rate", 1.0))):
            return
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无事件循环（同步上下文）→ 静默跳过，绝不影响主流程
        return
    try:
        task = loop.create_task(_write_row(
            user_id, conversation_id, query_hash, candidate_ids, tier_scores,
            gate_score, budget_chars, injected_chars, truncated, elapsed_ms, cache_hit,
        ))
        _write_tasks.add(task)
        task.add_done_callback(_write_tasks.discard)
    except Exception:
        logger.debug("recall log scheduling failed (silent)", exc_info=True)
