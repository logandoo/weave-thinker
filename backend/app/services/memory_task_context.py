# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""E4/E3（2026-09-14，默认关）：任务型记忆注入（死磕 / 后台任务）。

为「死磕 grilling 前 + 步骤 handoff」「后台任务 messages 构建」提供小预算记忆段。
调用点按开关门控：
  - `[deathmatch] memory_injection_enabled` / `memory_injection_budget_chars`
  - `[agent] background_task_memory_injection_enabled` / `background_task_memory_budget_chars`

E3（`[memory.retrieval] listwise_verifier_enabled`，默认关）：开启时对候选做一次
listwise 效用筛选（超时/异常/关闭 → None → 回退原始注入，fail-open）。
失败 fail-open（返回 ""），永不阻塞任务主流程；注入段带显式截断标注（零数字）。
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config

logger = logging.getLogger(__name__)

TASK_MEMORY_TRUNCATION_NOTE = "\n（任务记忆因预算未完整展示）"


async def _fetch_candidate_texts(
    db: AsyncSession, user_id: str, ids: list[str],
) -> list[dict]:
    """按召回 id 取候选展示文本（概念/事件/原文，一条 UNION 查询）。"""
    if not ids:
        return []
    rows = (await db.execute(
        text("""
            SELECT id, canonical_name || '：' || COALESCE(description_short, '')
            FROM memory_concepts WHERE user_id = :u AND id = ANY(:ids)
            UNION ALL
            SELECT id, narrative FROM memory_episodes
            WHERE user_id = :u AND id = ANY(:ids)
            UNION ALL
            SELECT id, raw_text FROM subconscious_log
            WHERE user_id = :u AND id = ANY(:ids)
        """),
        {"u": user_id, "ids": list(ids)},
    )).fetchall()
    by_id = {r[0]: (r[1] or "")[:300] for r in rows}
    return [{"id": i, "text": by_id[i]} for i in ids if i in by_id]


async def build_task_memory_context(
    db: AsyncSession, user_id: str, query_text: str, budget_chars: int = 600,
) -> str:
    """小预算任务记忆段（复用检索管线；失败 fail-open 返回空串）。

    E3 门控开启时：对召回候选做一次 listwise 过滤（保留独立效用条目）；
    返回 None（超时/异常）→ 回退原始注入。
    """
    try:
        from app.services import memory_retrieval_service as mrs
        ctx, ids, _top = await mrs.retrieve_with_meta(
            db, user_id, [{"role": "user", "content": query_text or ""}])
        if not ctx:
            return ""
        # E3（默认关）：listwise 验证器试点接入（失败回退原始 ctx）
        if get_config().memory_retrieval.get("listwise_verifier_enabled", False) and ids:
            try:
                from app.services.memory_listwise_verifier import verify_listwise
                cands = await _fetch_candidate_texts(db, user_id, ids[:8])
                keep = await verify_listwise(user_id, query_text or "", cands)
                if keep is not None:
                    kept = [c for c in cands if c["id"] in set(keep)]
                    if kept:
                        ctx = "\n".join(f"- {c['text']}" for c in kept)
            except Exception:
                logger.debug("task memory listwise filter failed (fallback)", exc_info=True)
        budget = max(int(budget_chars or 600), 100)
        if len(ctx) > budget:
            ctx = ctx[:budget] + TASK_MEMORY_TRUNCATION_NOTE
        return f"[任务记忆]\n{ctx}"
    except Exception:
        logger.debug("task memory context failed (fail-open)", exc_info=True)
        return ""


def deathmatch_memory_enabled() -> bool:
    return bool(get_config().deathmatch.get("memory_injection_enabled", False))


def background_task_memory_enabled() -> bool:
    return bool(get_config().agent.get("background_task_memory_injection_enabled", False))
