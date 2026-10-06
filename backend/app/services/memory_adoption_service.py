# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P1-① 采纳反馈闭环（2026-10-04，MLSys'26 论文评估 §3.3 落地）。

把「注入的记忆证据被最终回答引用」变成权重信号：回答文本出现注入概念的
名/别名（归一化、len≥2）→ `memory_weight_service.apply_reinforcement_signal(id,
"answer_cited")` + 该概念 `concept_relations` 边权 +0.02（LEAST 1.0 封顶）。

设计口径（论文 Phase 3 async weight update 的低成本近似）：
- 纯函数 `match_adopted_concepts` 负责判定（hermetic 单测）；写路径全 try/except
  fail-open，永不阻塞回答主流程；
- 只对本轮**注入过的** id 判定（不扫全库），成本 O(注入条数×别名数)；
- 名/别名匹配是保守代理（真「采纳」需用户反馈通道时再升级，见 memory 主题）。
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_MIN_NAME_LEN = 2
_MIN_ALIAS_LEN = 3  # 别名更短者噪声大（"bb" 之类），不作采纳证据
_MAX_ADOPTED_PER_TURN = 3  # 每轮回写上限（回显放大器限流，A4.9 wave2）
_EDGE_BUMP = 0.02
_EDGE_CAP = 1.0
# 进程内「近期被采纳概念」缓存（P1-② 一致性加权的 verified_ids 源）；
# 上限封顶防内存漂移，失败路径永不写入。
_ADOPTED_RECENT: dict[str, float] = {}
_ADOPTED_RECENT_MAX = 2048


def match_adopted_concepts(concepts: list[dict], answer_text: str) -> list[str]:
    """回答文本提及注入概念的名/别名 → 采纳 id 列表（保序、去重）。

    概念 dict：{"id", "canonical_name", "aliases"(JSON 数组串或 list)}。
    匹配为大小写不敏感子串；名/别名短于 2 字符忽略；aliases 损坏 fail-open。
    """
    if not concepts or not answer_text:
        return []
    haystack = answer_text.lower()
    out: list[str] = []
    for c in concepts:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        names: list[tuple[str, int]] = [(str(c.get("canonical_name") or ""), _MIN_NAME_LEN)]
        raw = c.get("aliases")
        try:
            aliases = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
        except (ValueError, TypeError):
            aliases = []
        if isinstance(aliases, list):
            names.extend((str(a), _MIN_ALIAS_LEN) for a in aliases)
        for n, floor in names:
            n = n.strip().lower()
            if len(n) >= floor and n in haystack:
                out.append(cid)
                break
    return out


def adopted_concepts_recent(limit: int = 512) -> set[str]:
    """近期被采纳概念 id（P1-② verified_ids 源；纯内存、按时间序取尾）。"""
    try:
        keys = list(_ADOPTED_RECENT.keys())
        return set(keys[-limit:])
    except Exception:
        return set()


async def record_answer_adoption(
    db: AsyncSession,
    user_id: str,
    injected_ids: list[str],
    answer_text: str,
) -> dict:
    """本轮注入证据被回答引用 → 权重/边权回写。fail-open，返回摘要。"""
    summary: dict = {"adopted": [], "skipped": 0}
    try:
        if not injected_ids or not (answer_text or "").strip():
            return summary
        rows = (await db.execute(
            text("""
                SELECT id, canonical_name, aliases FROM memory_concepts
                WHERE user_id = :u AND id = ANY(:ids) AND valid_to IS NULL
            """),
            {"u": user_id, "ids": list(injected_ids)},
        )).mappings().all()
        if not rows:
            return summary
        adopted = match_adopted_concepts([dict(r) for r in rows], answer_text)
        if not adopted:
            return summary
        adopted = adopted[:_MAX_ADOPTED_PER_TURN]
        from app.services.memory_weight_service import apply_reinforcement_signal
        for cid in adopted:
            await apply_reinforcement_signal(db, cid, "answer_cited")
            await db.execute(
                text("""
                    UPDATE concept_relations SET
                      weight = LEAST(CAST(:cap AS double precision), COALESCE(weight, 0) + :bump)
                    WHERE source_id = :id OR target_id = :id
                """),
                {"id": cid, "bump": _EDGE_BUMP, "cap": _EDGE_CAP},
            )
        summary["adopted"] = adopted
        now = __import__("time").time()
        for cid in adopted:
            # move-to-end：重采纳=热概念，不得被当作最旧优先逐出（A4.9 wave2）
            _ADOPTED_RECENT.pop(cid, None)
            _ADOPTED_RECENT[cid] = now
        if len(_ADOPTED_RECENT) > _ADOPTED_RECENT_MAX:
            for k in list(_ADOPTED_RECENT.keys())[: len(_ADOPTED_RECENT) - _ADOPTED_RECENT_MAX]:
                _ADOPTED_RECENT.pop(k, None)
    except Exception:
        logger.debug("record_answer_adoption failed (fail-open)", exc_info=True)
        summary["skipped"] = 1
    return summary


async def record_answer_adoption_bg(
    user_id: str,
    injected_ids: list[str],
    answer_text: str,
) -> None:
    """fire-and-forget 包装：独立 DB 会话，失败静默（对齐 recall boost 口径）。"""
    try:
        from app.db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await record_answer_adoption(db, user_id, injected_ids, answer_text)
            await db.commit()
    except Exception:
        logger.debug("record_answer_adoption_bg failed (fail-open)", exc_info=True)


def spawn_answer_adoption(user_id: str, injected_ids: list[str], answer_text: str):
    """在事件循环上调度采纳回写任务（调用方持有任务集防 GC）。"""
    return asyncio.create_task(
        record_answer_adoption_bg(user_id, injected_ids, answer_text))
