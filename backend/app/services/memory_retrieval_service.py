# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import UserAgentState
from app.services.memory_embedding_service import embed_text, embed_text_cached, find_similar_concepts, find_similar_episodes, find_similar_subconscious_units, _emb_from_db

config = get_config()
logger = logging.getLogger(__name__)

# BM25 词法来源标记（stage1 name/epi/sub + stage2 desc）——_cand_gate_score
# 区分"BM25-only 未确认"与"hybrid 确认"候选（盲区 D，2026-08-16 校准实证）
_BM25_SOURCES = ("stage1_name", "stage1_epi", "stage1_sub", "stage2_desc")

try:
    import jieba
    _JIEBA = True
except ImportError:
    _JIEBA = False


def _time_pattern_re() -> re.Pattern:
    patterns = config.memory.get("stage0_time_regex_patterns") or [
        "上周", "最近", "三天前", "前几天", "前天", "昨天", "去年", "以前", "以来",
        "之前", "过去", "曾经", "上次", "上个月",
    ]
    return re.compile("(" + "|".join(re.escape(p) for p in patterns) + ")")


@dataclass
class Stage0Result:
    keywords: list[str]
    time_range: Optional[tuple[datetime, datetime]] = None
    query_type: str = "semantic"
    include_expired: bool = False


@dataclass
class RetrievalCandidate:
    id: str
    tier: str
    content: str
    score: float
    metadata: dict


async def retrieve_with_meta(
    db: AsyncSession, user_id: str, conversation_messages: list[dict],
) -> tuple[str, list[str], float]:
    """召回管线 + meta 读出（语音每轮召回用）。

    返回 (ctx, memory_ids, top_gate_score)：ctx = 与旧入口逐字节相同的注入
    上下文；memory_ids = 本轮注入的全部候选 id（跨轮去重用）；top_gate_score
    = 注入候选中的最高绝对相关分（记忆插话预筛门槛，0.0 = 无候选）。
    """
    if not config.memory.get("retrieval_enabled", False):
        return await _fallback_context(db, user_id), [], 0.0

    user_queries = _extract_recent_user_queries(conversation_messages)
    if not user_queries:
        # 无用户查询仍注入恒定基底（用户长期记忆总览），避免零记忆
        return (
            await _build_injection_context(
                db, user_id, [], Stage0Result(keywords=[], query_type="general", include_expired=False),
                query_text=""),
            [], 0.0)

    # §5.2 工程要求 4：会话级缓存（最近 3 轮消息哈希，TTL 5 分钟）。
    # 缓存值携带完整 meta（ctx, ids, top）：语音 vmem 的跨轮 id 去重与
    # 插话仲裁预筛依赖 meta，只存 ctx 会让命中轮次的仲裁候选丢失。
    cache_key = _session_cache_key(user_id, user_queries)
    cached = _session_cache_get(cache_key)
    if cached is not None:
        return cached

    query_text = " ".join(user_queries)
    stage0 = await _stage0_query_expansion(query_text, user_id=user_id, db=db)

    # temporal_list 捷径（§5.2 Stage 0）：episodic-first，跳过 Stage 2-4
    if stage0.query_type == "temporal_list":
        tctx, tids, ttop = await _temporal_list_shortcut(db, user_id, stage0)
        _session_cache_put(cache_key, tctx, (tids, ttop))
        return tctx, tids, ttop

    cold_start = await _is_cold_start(db, user_id)
    if cold_start:
        ctx = await _try_cold_start_fallback(db, user_id, conversation_messages, cache_key)
        if ctx is not None:
            return ctx, [], 0.0
    candidates = await _stage1_bm25_search(db, user_id, stage0, cold_start)

    concept_candidates = [c for c in candidates if c.tier == "concept"]
    epi_candidates = [c for c in candidates if c.tier == "episodic"]
    sub_candidates = [c for c in candidates if c.tier == "subconscious"]

    if concept_candidates:
        concept_candidates = await _stage2_description_expansion(
            db, user_id, concept_candidates, stage0, query_text,
        )

    try:
        candidates = await _stage3_embedding_rerank(
            db, user_id, query_text, concept_candidates, epi_candidates, sub_candidates, stage0,
        )
    except Exception:
        logger.exception("Stage 3 failed, falling back to BM25")
        candidates = concept_candidates + epi_candidates + sub_candidates

    try:
        candidates = await _stage4_rerank(candidates, query_text, user_id=user_id, db=db, cold_start=cold_start)
    except Exception:
        logger.exception("Stage 4 failed")

    final_candidates = _composite_score_by_tier(candidates, stage0)

    # 召回后处理（M&D §5.2 / §5.3.1a）：fire-and-forget 写库，不阻塞响应。
    # 2026-08-16 修复（Oblivion 写路径解耦）：仅强命中概念（绝对相关分
    # ≥ boost_min_relevance）触发 weight/recency/stability 强化——弱相关
    # 注入不再回血，打断"注入即 boost"自我强化循环（高权重旧话题在无关
    # 查询中被卷入注入后不再续命）。
    injected: dict[str, list[RetrievalCandidate]] = {}
    memory_ids: list[str] = []
    top_gate_score = 0.0
    try:
        injected = _select_injected(final_candidates)
        memory_ids = [c.id for tier_list in injected.values() for c in tier_list]
        top_gate_score = max(
            (_cand_gate_score(c) for tier_list in injected.values() for c in tier_list),
            default=0.0)
        concept_ids = [c.id for c in injected["concept"]
                       if _cand_gate_score(c) >= float(config.memory_retrieval.get("boost_min_relevance", 0.5))]
        episode_ids = [c.id for c in injected["episodic"]
                       if _cand_gate_score(c) >= float(config.memory_retrieval.get("boost_min_relevance_episodic", 0.40))]
        unit_ids = [c.id for c in injected["subconscious"]]
        # §5.3.1a-2：多轮复现滑窗记录（同步、纯内存）
        if concept_ids:
            _record_window_hit(user_id, "concept", concept_ids)
        if episode_ids:
            _record_window_hit(user_id, "episodic", episode_ids)
        if unit_ids:
            _record_window_hit(user_id, "subconscious", unit_ids)
        if concept_ids or episode_ids or unit_ids:
            task = asyncio.create_task(_apply_recall_boosts_bg(user_id, concept_ids, episode_ids, unit_ids))
            _boost_tasks.add(task)
            task.add_done_callback(_boost_tasks.discard)
    except Exception:
        logger.debug("recall boost scheduling failed", exc_info=True)

    ctx = await _build_injection_context(db, user_id, final_candidates, stage0, query_text=query_text)
    _session_cache_put(cache_key, ctx, (memory_ids, top_gate_score))
    return ctx, memory_ids, top_gate_score


async def retrieve_and_build_context(
    db: AsyncSession, user_id: str, conversation_messages: list[dict],
) -> str:
    """文本通道入口——行为与重构前逐字节一致（仅委托 meta 版取 ctx）。"""
    ctx, _ids, _top = await retrieve_with_meta(db, user_id, conversation_messages)
    return ctx


async def _fallback_context(db: AsyncSession, user_id: str) -> str:
    """旧摘要式注入（§5.2 兜底 / retrieval_enabled=false 双轨）。

    不在请求路径同步生成摘要（§1.2 #13：force 生成 = 2 次串行 LLM + 共享会话 commit，
    5-30s TTFT 阻塞）。摘要缺失时调度后台生成（独立会话），本轮返回已有内容。
    """
    try:
        result = await db.execute(
            text("SELECT memory_summary, dream_summary FROM user_agent_states WHERE user_id = :uid"),
            {"uid": user_id},
        )
        row = result.fetchone()
        memory_summary, dream_summary = (row[0], row[1]) if row else (None, None)
        sections = []
        if memory_summary:
            sections.append("共享长期记忆:\n" + memory_summary.strip()[:2000])
        if dream_summary:
            sections.append("近期 dream:\n" + dream_summary.strip()[:2000])
        if not sections:
            _schedule_summary_generation(user_id)
        return "\n\n".join(sections)
    except Exception:
        return ""


_summary_gen_tasks: set = set()
_summary_gen_inflight: set = set()


def _schedule_summary_generation(user_id: str) -> None:
    """后台补齐摘要（独立会话提交，fire-and-forget 强引用防 GC；per-user 去重防任务风暴）。"""
    if user_id in _summary_gen_inflight:
        return

    async def _gen() -> None:
        from app.db.database import AsyncSessionLocal
        from app.services.memory_service import generate_user_agent_memory
        try:
            async with AsyncSessionLocal() as session:
                await generate_user_agent_memory(session, user_id, force=False)
        except Exception:
            logger.debug("background summary generation failed for user=%s", user_id, exc_info=True)
        finally:
            _summary_gen_inflight.discard(user_id)

    try:
        _summary_gen_inflight.add(user_id)
        task = asyncio.create_task(_gen())
        _summary_gen_tasks.add(task)
        task.add_done_callback(_summary_gen_tasks.discard)
    except RuntimeError:
        _summary_gen_inflight.discard(user_id)


def _msg_get(m, key, default=""):
    if isinstance(m, dict):
        return m.get(key, default)
    return getattr(m, key, default)


def _extract_recent_user_queries(messages: list) -> list[str]:
    user_msgs = [_msg_get(m, "content", "") for m in messages[-6:] if _msg_get(m, "role") == "user"]
    return [q for q in user_msgs[-3:] if q]


async def _is_cold_start(db: AsyncSession, user_id: str) -> bool:
    threshold = int(config.memory_retrieval.get("bootstrap_threshold", 10))
    result = await db.execute(
        text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 AND valid_to IS NULL"),
        {"uid": user_id},
    )
    count = result.scalar() or 0
    return count < threshold


async def _count_recallable_concepts(db: AsyncSession, user_id: str) -> int:
    """active + silent（valid_to IS NULL）概念总数——冷启动 Stage 1 放宽后的可召回池大小。"""
    result = await db.execute(
        text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND status IN ('active','silent') AND valid_to IS NULL"),
        {"uid": user_id},
    )
    return result.scalar() or 0


async def _has_recallable_subconscious(db: AsyncSession, user_id: str) -> bool:
    """近 30 天内有带 embedding 的 subconscious 单元（低数据用户原文检索兜底）。"""
    result = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM subconscious_log
                WHERE user_id = :uid AND embedding IS NOT NULL
                  AND created_at >= now() - interval '30 days'
                LIMIT 1
            )
        """),
        {"uid": user_id},
    )
    return bool(result.scalar())


async def _is_migration_completed(db: AsyncSession, user_id: str) -> bool:
    result = await db.execute(
        text("SELECT metadata_json FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    raw = result.scalar()
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("migration_completed_at"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False


def _is_first_user_message(conversation_messages: list) -> bool:
    user_msgs = [m for m in conversation_messages if _msg_get(m, "role") == "user"]
    return len(user_msgs) <= 1


async def _try_cold_start_fallback(
    db: AsyncSession, user_id: str, conversation_messages: list, cache_key: str,
) -> str | None:
    """§5.2 冷启动回退链。返回 None 表示继续走正常召回管线（Stage 1 已放宽 silent）。

    1. active+silent > 5 → None（种子足够，无需 fallback）
    2. 有 subconscious 内容（近 30 天含 embedding）→ None（原文检索兜底低数据
       用户，2026-08-09 盲区1修复：0 概念但 subconscious 有内容时必须走 v2
       管线，否则 subconscious/embedding/CE 全被 v1 摘要 fallback 截断）
    3. 迁移完成老用户 → None（跳过冷启动）
    4. [memory.multimodal] opt-in 且（fallback_first_msg_only → 仅用户首条消息）
       → 多模态 PNG+SoM fallback（失败回退第 5 步）
    5. 兜底：旧摘要式注入（_fallback_context 直读摘要，缺失时后台生成）
    """
    recallable = await _count_recallable_concepts(db, user_id)
    if recallable > 5:
        return None
    if await _has_recallable_subconscious(db, user_id):
        return None
    if await _is_migration_completed(db, user_id):
        return None

    mm_cfg = config.memory_multimodal
    if mm_cfg.get("enabled") and mm_cfg.get("fallback_on_cold_start", True):
        if not mm_cfg.get("fallback_first_msg_only", True) or _is_first_user_message(conversation_messages):
            from app.services import memory_multimodal_service
            try:
                ctx = await memory_multimodal_service.fallback_cold_start_context(
                    db, user_id, conversation_messages,
                )
            except Exception:
                logger.warning("multimodal cold-start fallback error for user=%s", user_id, exc_info=True)
                ctx = None
            if ctx:
                _session_cache_put(cache_key, ctx)
                return ctx
            # §9.9：多模态渲染/LLM 失败 → 回退 Stage 1-2-3 RRF（继续正常管线），不走旧摘要
            return None

    ctx = await _fallback_context(db, user_id)
    _session_cache_put(cache_key, ctx)
    return ctx


async def _stage0_query_expansion(
    query_text: str, user_id: str | None = None, db: AsyncSession | None = None,
) -> Stage0Result:
    """§5.2 Stage 0：jieba 分词零 LLM；语义判断（query_type / include_expired /
    时间归一化）全部由 LLM 完成（agentic 原则 2026-07-20——原 _ASOF_RE /
    _TEMPORAL_LIST_RE 正则分类器无法泛化任意表达，已删除）。`has_time` 只是
    成本门（决定是否值得花一次 LLM 调用），不是语义判断。"""
    from app.services.memory_cost_governance_service import is_step_enabled

    time_re = _time_pattern_re()
    has_time = bool(time_re.search(query_text))
    stage0_llm = bool(config.memory.get("stage0_llm_enabled", False))

    stage0_off = False
    if user_id is not None:
        try:
            stage0_off = not await is_step_enabled(user_id, "stage0_off", db)
        except Exception:
            stage0_off = False

    # LLM 触发门：时间短语（任意长度，覆盖"以前聊过什么"等短查询）或长查询
    # 或 stage0_llm_enabled 放宽模式。未触发 → 语义中立（semantic，不含
    # as-of/temporal 特殊处理），不产生任何语义判断。
    should_llm = not stage0_off and (
        has_time or stage0_llm or len(query_text) >= 20
    )
    if should_llm:
        try:
            keywords, time_range, llm_query_type, llm_include_expired = await _llm_time_normalization(query_text)
            return Stage0Result(
                keywords=keywords or _jieba_keywords(query_text),
                time_range=time_range,
                query_type=llm_query_type or "semantic",
                include_expired=llm_include_expired,
            )
        except Exception:
            logger.debug("stage0 LLM time normalization failed, fallback jieba", exc_info=True)

    return Stage0Result(keywords=_jieba_keywords(query_text), include_expired=False)


def _jieba_keywords(text: str) -> list[str]:
    if _JIEBA:
        words = jieba.lcut(text)
        words = [w for w in words if len(w) >= 2 and not w.isspace()]
        words = [w for w in words if not re.fullmatch(r'[\u3000-\u303f\uff00-\uffef]+', w)]
        return words[:5] if words else [text[:20]]
    return [w for w in text.split()[:5] if len(w) >= 2] or [text[:20]]


async def _llm_time_normalization(text: str) -> tuple[list[str], Optional[tuple[datetime, datetime]], str, bool]:
    from app.services.memory_llm_factory import _memory_llm
    llm = _memory_llm("query_expansion")
    now_str = datetime.now(timezone.utc).isoformat()
    resp = await llm.complete_chat(
        [
            {"role": "system", "content": (
                "你是时间归一化助手。从用户消息中提取关键词和时间范围。\n"
                '输出 JSON: {"keywords": [...], "time_range": {"start": "ISO8601", "end": "ISO8601"} | null, '
                '"query_type": "semantic|temporal_list", "include_expired": bool}\n'
                f"当前 UTC 时间: {now_str}"
            )},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
    )
    resp = (resp or "").strip()
    if resp.startswith("```"):
        resp = resp.split("\n", 1)[1].rsplit("\n", 1)[0]
    parsed = json.loads(resp)
    keywords = parsed.get("keywords", [])
    time_data = parsed.get("time_range")
    time_range = None
    if time_data and isinstance(time_data, dict):
        start = time_data.get("start")
        end = time_data.get("end")
        if start and end:
            try:
                time_range = (
                    datetime.fromisoformat(start.replace("Z", "+00:00")),
                    datetime.fromisoformat(end.replace("Z", "+00:00")),
                )
            except (ValueError, TypeError):
                pass
    return keywords[:5], time_range, parsed.get("query_type") or "semantic", bool(parsed.get("include_expired"))


async def _stage1_bm25_search(
    db: AsyncSession, user_id: str, stage0: Stage0Result, cold_start: bool,
) -> list[RetrievalCandidate]:
    from app.services.memory_bm25 import get_name_index, get_epi_index, get_sub_index
    from app.services.memory_weight_service import try_cold_resurrect
    candidates = []

    resurrected = await try_cold_resurrect(db, " ".join(stage0.keywords), user_id)

    query_str = " ".join(stage0.keywords)

    try:
        name_idx = await get_name_index(db, user_id)
        for doc_id, score in name_idx.search(query_str, k=int(config.memory_retrieval.get("stage1_concept_top_k", 30))):
            detail = await _get_concept_detail(db, doc_id, user_id, include_expired=stage0.include_expired)
            if not detail:
                continue
            # §5.2：as-of 不限制 status；冷启动放行 active+silent（不放行 cold_forgotten）；
            # 常规仅 active + activation_strength > 0.05
            if stage0.include_expired:
                pass
            elif cold_start:
                if detail.get("status") not in ("active", "silent"):
                    continue
            elif not _concept_status_ok(detail, False):
                continue
            regex_boost = _regex_match_boost(stage0.keywords, detail.get("canonical_name", ""), detail.get("aliases", ""))
            total_score = score + regex_boost
            candidates.append(RetrievalCandidate(
                id=doc_id, tier="concept", score=total_score,
                content=detail.get("description_short", ""),
                metadata={"canonical_name": detail.get("canonical_name", ""),
                          "description_full": detail.get("description_full", ""),
                          "weight": detail.get("weight", 0.5),
                          "importance": detail.get("importance", 0.5),
                          "source_trust": detail.get("source_trust", ""),
                          "memory_type": detail.get("memory_type", ""),
                          "aliases": detail.get("aliases", ""),
                          "last_recalled_at": detail.get("last_recalled_at"),
                          "stability": detail.get("stability"),
                          "created_at": detail.get("created_at"),
                          "valid_to": detail.get("valid_to"),
                          "source": "stage1_name"},
            ))
    except Exception:
        logger.exception("Stage 1 concept BM25 failed")

    try:
        epi_idx = await get_epi_index(db, user_id)
        for doc_id, score in epi_idx.search(query_str, k=int(config.memory_retrieval.get("stage1_episodic_top_k", 5))):
            detail = await _get_episode_detail(db, doc_id, include_expired=stage0.include_expired)
            if detail:
                candidates.append(RetrievalCandidate(
                    id=doc_id, tier="episodic", score=score,
                    content=detail.get("narrative", "")[:200],
                    metadata={"narrative": detail.get("narrative", ""),
                              "valid_from": detail.get("valid_from"),
                              "source_concept_ids": detail.get("source_concept_ids", ""),
                              "source": "stage1_epi"},
                ))
    except Exception:
        logger.exception("Stage 1 episodic BM25 failed")

    try:
        sub_idx = await get_sub_index(db, user_id)
        for doc_id, score in sub_idx.search(query_str, k=int(config.memory_retrieval.get("stage1_subconscious_top_k", 10))):
            detail = await _get_subconscious_detail(db, doc_id)
            if detail:
                candidates.append(RetrievalCandidate(
                    id=doc_id, tier="subconscious", score=score,
                    content=detail.get("raw_text", "")[:200],
                    metadata={"raw_text": detail.get("raw_text", ""),
                              "created_at": detail.get("created_at"),
                              "recurrence_count": detail.get("recurrence_count", 0),
                              "source": "stage1_sub"},
                ))
    except Exception:
        logger.exception("Stage 1 subconscious BM25 failed")

    return candidates


def _regex_match_boost(keywords: list[str], name: str, aliases_raw: str) -> float:
    boost = 0.0
    for kw in keywords:
        if kw.lower() in name.lower():
            boost += 2.0
        if aliases_raw:
            try:
                aliases = json.loads(aliases_raw) if isinstance(aliases_raw, str) else aliases_raw
                for alias in aliases:
                    if kw.lower() in str(alias).lower():
                        boost += 1.5
            except (json.JSONDecodeError, TypeError):
                pass
    return boost


# ---- §5.2 会话级缓存（最近 3 轮消息哈希，TTL 5min） ----

# 值 = (写入时刻, (ctx, memory_ids, top_gate_score))
_session_cache: dict[str, tuple[float, tuple[str, list, float]]] = {}
_SESSION_CACHE_TTL = 300.0
_EMPTY_META: tuple[list, float] = ([], 0.0)


def _session_cache_key(user_id: str, user_queries: list[str]) -> str:
    import hashlib
    h = hashlib.sha1(("|".join(user_queries[-3:])).encode("utf-8")).hexdigest()[:16]
    return f"{user_id}:{h}"


def _session_cache_get(key: str) -> Optional[tuple[str, list, float]]:
    entry = _session_cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _SESSION_CACHE_TTL:
        _session_cache.pop(key, None)
        return None
    return value


def _session_cache_put(key: str, value: str, meta: Optional[tuple[list, float]] = None) -> None:
    if len(_session_cache) > 500:
        oldest = sorted(_session_cache.items(), key=lambda kv: kv[1][0])[:100]
        for k, _ in oldest:
            _session_cache.pop(k, None)
    _session_cache[key] = (time.time(), (value, *(meta or _EMPTY_META)))


# ---- §5.3.1a-2 多轮复现滑窗（per-user 5 轮） ----

from collections import defaultdict, deque

_concept_window: dict[str, deque] = defaultdict(lambda: deque(maxlen=5))
_episodic_window: dict[str, deque] = defaultdict(lambda: deque(maxlen=5))
_subconscious_window: dict[str, deque] = defaultdict(lambda: deque(maxlen=5))


def _record_window_hit(user_id: str, tier: str, ids: list[str]) -> None:
    window = {"concept": _concept_window, "episodic": _episodic_window,
              "subconscious": _subconscious_window}.get(tier)
    if window is None or not ids:
        return
    window[user_id].append(tuple(ids))


def _window_recurrent_ids(user_id: str, tier: str) -> list[str]:
    window = {"concept": _concept_window, "episodic": _episodic_window,
              "subconscious": _subconscious_window}.get(tier)
    if window is None:
        return []
    counts: dict[str, int] = {}
    for turn_ids in window.get(user_id, ()):
        for iid in turn_ids:
            counts[iid] = counts.get(iid, 0) + 1
    return [iid for iid, n in counts.items() if n >= 3]


async def _apply_window_boosts(db: AsyncSession, user_id: str) -> None:
    """§5.3.1a-2：窗口内 ≥3 次命中 → concept +0.05；episode 仅时间戳；unit recurrence_count++。"""
    from app.services.memory_weight_service import (
        apply_reinforcement_signal, apply_episode_recall_boost, apply_subconscious_recall_boost,
    )
    for cid in _window_recurrent_ids(user_id, "concept"):
        await apply_reinforcement_signal(db, cid, "multi_turn_recurrence")
    epi_ids = _window_recurrent_ids(user_id, "episodic")
    if epi_ids:
        await apply_episode_recall_boost(db, epi_ids)
    unit_ids = _window_recurrent_ids(user_id, "subconscious")
    if unit_ids:
        await apply_subconscious_recall_boost(db, unit_ids)


async def _temporal_list_shortcut(
    db: AsyncSession, user_id: str, stage0: "Stage0Result | None" = None,
) -> tuple[str, list[str], float]:
    """§5.2 temporal_list 捷径：episodic-first（10）→ 不足补 concept（8）→
    不足补 subconscious（10）。

    返回 (ctx, ids, top)：ids = 全部选中候选 id（语音 vmem 仲裁/去重需要）；
    top = 1.0（时间型查询按构造即强相关，只要选中了候选就够仲裁门槛）。"""
    sections: list[str] = []
    ids: list[str] = []

    epi_r = await db.execute(
        text("""SELECT id, narrative, valid_from FROM memory_episodes
                WHERE user_id = :uid AND valid_to IS NULL
                ORDER BY valid_from DESC NULLS LAST LIMIT 10"""),
        {"uid": user_id},
    )
    episodes = epi_r.fetchall()
    if episodes:
        lines = ["[相关事件 Episodic]"]
        for r in episodes:
            ids.append(r[0])
            lines.append(f"- [{r[2]}] {(r[1] or '')[:200]}")
        sections.append("\n".join(lines))

    if len(episodes) < 5:
        con_r = await db.execute(
            text("""SELECT id, canonical_name, description_short, aliases, weight, source_trust
                    FROM memory_concepts
                    WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 AND valid_to IS NULL
                    ORDER BY last_recalled_at DESC NULLS LAST LIMIT 8"""),
            {"uid": user_id},
        )
        concepts = con_r.fetchall()
        if concepts:
            lines = ["[相关概念 Concept]"]
            for r in concepts:
                ids.append(r[0])
                tag = " （推断，未经用户确认）" if r[5] == "agent_inferred" else ""
                lines.append(f"- {r[1]}: {(r[2] or '')[:80]}{tag} [权重: {(r[4] or 0):.2f}]")
            sections.append("\n".join(lines))
        concept_count = len(concepts)
    else:
        concept_count = 99

    if len(episodes) + (0 if concept_count == 99 else concept_count) < 8:
        sub_r = await db.execute(
            text("""SELECT id, raw_text, created_at FROM subconscious_log
                    WHERE user_id = :uid AND created_at >= now() - interval '30 days' AND embedding IS NOT NULL
                    ORDER BY created_at DESC LIMIT 10"""),
            {"uid": user_id},
        )
        subs = sub_r.fetchall()
        if subs:
            lines = ["[近期原文片段 Subconscious]"]
            for r in subs:
                ids.append(r[0])
                lines.append(f"- [{r[2]}] {(r[1] or '')[:200]} (未经整理)")
            sections.append("\n".join(lines))

    return "\n\n".join(sections), ids, 1.0 if ids else 0.0


def _concept_status_ok(detail: dict, include_expired: bool) -> bool:
    """§5.2：常规查询仅 active + activation_strength > 0.05；
    as-of 历史查询不限制 status / activation_strength（含 silent + cold_forgotten）。"""
    if include_expired:
        return True
    return detail.get("status") == "active" and (detail.get("activation_strength") or 0) > 0.05


async def _get_concept_detail(
    db: AsyncSession, concept_id: str, user_id: str, include_expired: bool = False,
) -> dict | None:    # §5.2 as-of 历史查询：include_expired 时不过滤 valid_to（失效概念可查，注入时标注）
    validity = "valid_from <= NOW()" if include_expired else "valid_to IS NULL"
    result = await db.execute(text(f"SELECT canonical_name, description_short, description_full, weight, source_trust, memory_type, aliases, activation_strength, status, last_recalled_at, stability, created_at, valid_to, importance FROM memory_concepts WHERE id = :id AND user_id = :uid AND {validity}"), {"id": concept_id, "uid": user_id})
    row = result.fetchone()
    if not row:
        return None
    return {
        "canonical_name": row[0], "description_short": row[1], "description_full": row[2],
        "weight": row[3], "source_trust": row[4], "memory_type": row[5],
        "aliases": row[6], "activation_strength": row[7], "status": row[8],
        "last_recalled_at": row[9], "stability": row[10], "created_at": row[11],
        "valid_to": row[12], "importance": row[13],
    }


async def _get_episode_detail(db: AsyncSession, episode_id: str, include_expired: bool = False) -> dict | None:
    # §5.2 as-of：include_expired 时不过滤 valid_to/superseded_by
    validity = "valid_from <= NOW()" if include_expired else "(valid_to IS NULL OR superseded_by IS NULL)"
    result = await db.execute(text(f"SELECT narrative, valid_from, source_concept_ids FROM memory_episodes WHERE id = :id AND {validity}"), {"id": episode_id})
    row = result.fetchone()
    if not row:
        return None
    return {"narrative": row[0], "valid_from": row[1], "source_concept_ids": row[2]}


async def _get_subconscious_detail(db: AsyncSession, unit_id: str) -> dict | None:
    result = await db.execute(text("SELECT raw_text, created_at, recurrence_count FROM subconscious_log WHERE id = :id"), {"id": unit_id})
    row = result.fetchone()
    if not row:
        return None
    return {"raw_text": row[0], "created_at": row[1], "recurrence_count": row[2]}


async def _stage2_description_expansion(
    db: AsyncSession, user_id: str, candidates: list[RetrievalCandidate],
    stage0: Stage0Result, query_text: str,
) -> list[RetrievalCandidate]:
    from app.services.memory_bm25 import get_desc_index
    from app.services.memory_cluster_service import get_clusters_for_concepts, get_neighbors

    ret_cfg = config.memory_retrieval
    query_str = " ".join(stage0.keywords)

    try:
        desc_idx = await get_desc_index(db, user_id)
        for doc_id, score in desc_idx.search(query_str, k=int(ret_cfg.get("stage2_concept_top_k", 15))):
            existing = [c for c in candidates if c.id == doc_id]
            if not existing:
                detail = await _get_concept_detail(db, doc_id, user_id, include_expired=stage0.include_expired)
                if detail and _concept_status_ok(detail, stage0.include_expired):
                    candidates.append(RetrievalCandidate(
                        id=doc_id, tier="concept", score=score,
                        content=detail.get("description_short", ""),
                        metadata={"canonical_name": detail.get("canonical_name", ""),
                                  "description_full": detail.get("description_full", ""),
                                  "weight": detail.get("weight", 0.5),
                          "importance": detail.get("importance", 0.5),
                                  "source_trust": detail.get("source_trust", ""),
                                  "memory_type": detail.get("memory_type", ""),
                                  "aliases": detail.get("aliases", ""),
                                  "last_recalled_at": detail.get("last_recalled_at"),
                                  "stability": detail.get("stability"),
                                  "created_at": detail.get("created_at"),
                                  "source": "stage2_desc"},
                    ))
    except Exception:
        logger.exception("Stage 2 desc BM25 failed")

    if ret_cfg.get("stage2_relation_expansion_enabled", True):
        concept_ids = [c.id for c in candidates if c.tier == "concept"]
        try:
            clusters = await get_clusters_for_concepts(db, concept_ids[:10])
            for cluster in clusters:
                result = await db.execute(
                    text("SELECT mc.id FROM memory_concepts mc JOIN concept_cluster_members ccm ON mc.id = ccm.concept_id WHERE ccm.cluster_id = :clid AND mc.id != ALL(:existing) LIMIT 10"),
                    {"clid": cluster["id"], "existing": [c.id for c in candidates if c.tier == "concept"]},
                )
                for row in result.fetchall():
                    detail = await _get_concept_detail(db, row[0], user_id, include_expired=stage0.include_expired)
                    if detail and _concept_status_ok(detail, stage0.include_expired):
                        candidates.append(RetrievalCandidate(
                            id=row[0], tier="concept", score=0.5,
                            content=detail.get("description_short", ""),
                            metadata={"canonical_name": detail.get("canonical_name", ""),
                                      "description_full": detail.get("description_full", ""),
                                      "weight": detail.get("weight", 0.5),
                          "importance": detail.get("importance", 0.5),
                                      "source_trust": detail.get("source_trust", ""),
                                      "memory_type": detail.get("memory_type", ""),
                                      "aliases": detail.get("aliases", ""),
                                      "last_recalled_at": detail.get("last_recalled_at"),
                                      "stability": detail.get("stability"),
                                      "created_at": detail.get("created_at"),
                                      "source": "cluster_expansion"},
                        ))
        except Exception:
            pass

        # §5.2 Stage 2 step 4：关系扩展（1-hop PPR-lite）——种子 top-5 沿 concept_relations 扩邻居
        try:
            seed_top_k = int(ret_cfg.get("stage2_relation_seed_top_k", 5))
            max_neighbors = int(ret_cfg.get("stage2_relation_max_neighbors", 5))
            max_new = int(ret_cfg.get("stage2_relation_max_new", 10))
            score_decay = float(ret_cfg.get("stage2_relation_score_decay", 0.6))
            min_edge_w = float(ret_cfg.get("stage2_relation_min_edge_weight", 0.3))

            seeds = sorted([c for c in candidates if c.tier == "concept"],
                           key=lambda c: c.score, reverse=True)[:seed_top_k]
            existing_ids = {c.id for c in candidates if c.tier == "concept"}
            added = 0
            for seed in seeds:
                if added >= max_new:
                    break
                neighbors = await get_neighbors(db, seed.id, min_weight=min_edge_w)
                for nb in neighbors[:max_neighbors]:
                    if added >= max_new:
                        break
                    nb_id = nb["id"]
                    if nb_id in existing_ids:
                        continue
                    detail = await _get_concept_detail(db, nb_id, user_id, include_expired=stage0.include_expired)
                    if not detail or not _concept_status_ok(detail, stage0.include_expired):
                        continue
                    edge_w = float(nb.get("weight") or 0.5)
                    candidates.append(RetrievalCandidate(
                        id=nb_id, tier="concept", score=seed.score * edge_w * score_decay,
                        content=detail.get("description_short", ""),
                        metadata={"canonical_name": detail.get("canonical_name", ""),
                                  "description_full": detail.get("description_full", ""),
                                  "weight": detail.get("weight", 0.5),
                          "importance": detail.get("importance", 0.5),
                                  "source_trust": detail.get("source_trust", ""),
                                  "memory_type": detail.get("memory_type", ""),
                                  "aliases": detail.get("aliases", ""),
                                  "last_recalled_at": detail.get("last_recalled_at"),
                                  "stability": detail.get("stability"),
                                  "created_at": detail.get("created_at"),
                                  "source": "relation_expansion"},
                    ))
                    existing_ids.add(nb_id)
                    added += 1
        except Exception:
            logger.debug("relation expansion failed", exc_info=True)

    return sorted(candidates, key=lambda c: c.score, reverse=True)[:50]


async def _stage3_embedding_rerank(
    db: AsyncSession, user_id: str, query_text: str,
    concept_candidates: list[RetrievalCandidate],
    epi_candidates: list[RetrievalCandidate],
    sub_candidates: list[RetrievalCandidate],
    stage0: Stage0Result,
) -> list[RetrievalCandidate]:
    import hashlib
    cache_key = f"query_{user_id}_{hashlib.sha1(query_text.encode('utf-8')).hexdigest()[:16]}"
    query_emb = await embed_text_cached(query_text, cache_key)
    if not query_emb:
        return concept_candidates + epi_candidates + sub_candidates

    ret_cfg = config.memory_retrieval
    bootstrap = int(ret_cfg.get("bootstrap_threshold", 10))
    active_count = await _count_active_concepts(db, user_id)

    try:
        concept_results = await find_similar_concepts(
            db, user_id, query_emb,
            top_k=int(ret_cfg.get("retrieval_k_concept", 10)),
            include_expired=stage0.include_expired,
            active_concept_count=active_count, bootstrap_threshold=bootstrap,
        )
    except Exception:
        concept_results = []

    try:
        epi_results = await find_similar_episodes(
            db, user_id, query_emb,
            top_k=int(ret_cfg.get("retrieval_k_epi", 5)),
            include_expired=stage0.include_expired,
            time_start=stage0.time_range[0] if stage0.time_range else None,
            time_end=stage0.time_range[1] if stage0.time_range else None,
        )
    except Exception:
        epi_results = []

    try:
        sub_results = await find_similar_subconscious_units(
            db, user_id, query_emb,
            top_k=10,
            time_start=stage0.time_range[0] if stage0.time_range else None,
            time_end=stage0.time_range[1] if stage0.time_range else None,
        )
    except Exception:
        sub_results = []

    emb_weight = float(ret_cfg.get("embedding_score_weight", 0.6))
    bm25_weight = float(ret_cfg.get("bm25_score_weight", 0.4))

    bm25_max = max((c.score for c in concept_candidates), default=0.0)

    for cr in concept_results:
        existing = [c for c in concept_candidates if c.id == cr["id"]]
        if existing:
            bm25_norm = (existing[0].score / bm25_max) if bm25_max > 0 else 0.0
            concept_final = cr["similarity"] * emb_weight + bm25_norm * bm25_weight
            existing[0].score = concept_final
            existing[0].metadata["embedding_sim"] = cr["similarity"]
            existing[0].metadata["calibrated_score"] = concept_final
        else:
            detail = await _get_concept_detail(db, cr["id"], user_id, include_expired=stage0.include_expired)
            if detail:
                concept_candidates.append(RetrievalCandidate(
                    id=cr["id"], tier="concept", score=cr["similarity"],
                    content=detail.get("description_short", ""),
                    metadata={"canonical_name": detail.get("canonical_name", ""),
                              "description_full": detail.get("description_full", ""),
                              "weight": detail.get("weight", 0.5),
                          "importance": detail.get("importance", 0.5),
                              "source_trust": detail.get("source_trust", ""),
                              "memory_type": detail.get("memory_type", ""),
                              "aliases": detail.get("aliases", ""),
                              "last_recalled_at": detail.get("last_recalled_at"),
                              "stability": detail.get("stability"),
                              "created_at": detail.get("created_at"),
                              "valid_to": detail.get("valid_to"),
                              "embedding_sim": cr["similarity"],
                              "calibrated_score": cr["similarity"],
                              "source": "stage3_emb"},
                ))

    epi_bm25_max = max((c.score for c in epi_candidates), default=0.0)
    for er in epi_results:
        existing = [c for c in epi_candidates if c.id == er["id"]]
        if existing:
            bm25_norm = (existing[0].score / epi_bm25_max) if epi_bm25_max > 0 else 0.0
            epi_final = er["similarity"] * emb_weight + bm25_norm * bm25_weight
            existing[0].score = epi_final
            existing[0].metadata["embedding_sim"] = er["similarity"]
            existing[0].metadata["calibrated_score"] = epi_final
        else:
            epi_candidates.append(RetrievalCandidate(
                id=er["id"], tier="episodic", score=er["similarity"],
                content=er.get("narrative", "")[:200],
                metadata={"narrative": er.get("narrative", ""),
                          "valid_from": er.get("valid_from"),
                          "embedding_sim": er["similarity"],
                          "calibrated_score": er["similarity"],
                          "source": "stage3_emb"},
            ))

    sub_bm25_max = max((c.score for c in sub_candidates), default=0.0)
    for sr in sub_results:
        existing = [c for c in sub_candidates if c.id == sr["id"]]
        if existing:
            bm25_norm = (existing[0].score / sub_bm25_max) if sub_bm25_max > 0 else 0.0
            sub_final = sr["similarity"] * emb_weight + bm25_norm * bm25_weight
            existing[0].score = sub_final
            existing[0].metadata["embedding_sim"] = sr["similarity"]
            existing[0].metadata["calibrated_score"] = sub_final
        else:
            sub_candidates.append(RetrievalCandidate(
                id=sr["id"], tier="subconscious", score=sr["similarity"],
                content=sr.get("raw_text", "")[:200],
                metadata={"raw_text": sr.get("raw_text", ""),
                          "created_at": sr.get("created_at"),
                          "embedding_sim": sr["similarity"],
                          "calibrated_score": sr["similarity"],
                          "source": "stage3_emb"},
            ))

    # 分数尺度一致化：未命中 embedding 的 BM25-only 候选保留原始分（可高达 20+），
    # 会淹没 embedding 相似度（[0,1]）导致 RRF 排序失真——同 tier 内归一化到
    # [0, bm25_weight] 并与 hybrid 分可比（A4.6 根因 1：7900 XTX 单元 BM25=24 被覆盖后
    # 反而跌出 RRF top-4，纯 BM25 干扰项 7-9 分却存活）。
    # A4.9 复审 Minor：仅关系/聚类扩展候选（固定 0.5 分、非 BM25 命中）的 tier，
    # tier_max=0.5 → 全部归一化为 bm25_weight=0.4 虚高——只归一化 BM25 来源候选；
    # 非 BM25 来源（cluster/relation 扩展，raw 尺度可能 7+）钳制到 bm25_weight
    # 上限，防其越权超过归一化后的真 BM25 命中（A4.9 r2 Minor #1）。
    for tier_cands in (concept_candidates, epi_candidates, sub_candidates):
        bm25_src = [c for c in tier_cands if c.metadata.get("source") in _BM25_SOURCES
                    and c.metadata.get("embedding_sim") is None]
        tier_max = max((c.score for c in bm25_src), default=0.0)
        if tier_max > 0:
            for c in bm25_src:
                c.score = (c.score / tier_max) * bm25_weight
                c.metadata["calibrated_score"] = c.score
        for c in tier_cands:
            if c.metadata.get("source") not in _BM25_SOURCES \
                    and c.metadata.get("embedding_sim") is None \
                    and float(c.score) > bm25_weight:
                c.score = bm25_weight
                c.metadata["calibrated_score"] = bm25_weight

    return _apply_weighted_rrf(concept_candidates, epi_candidates, sub_candidates)


def _apply_weighted_rrf(
    concept: list[RetrievalCandidate],
    episodic: list[RetrievalCandidate],
    subconscious: list[RetrievalCandidate],
) -> list[RetrievalCandidate]:
    ret_cfg = config.memory_retrieval
    rrf_k = int(ret_cfg.get("rrf_k", 60))
    w_c = float(ret_cfg.get("rrf_weight_concept", 1.0))
    w_e = float(ret_cfg.get("rrf_weight_episodic", 1.5))
    w_s = float(ret_cfg.get("rrf_weight_subconscious", 0.5))

    concept_max = int(ret_cfg.get("injection_max_concept", 4)) * 2
    epi_max = int(ret_cfg.get("injection_max_episodic", 2)) * 2
    sub_max = int(ret_cfg.get("injection_max_subconscious", 2)) * 2

    concept_sorted = sorted(concept, key=lambda c: c.score, reverse=True)[:concept_max]
    epi_sorted = sorted(episodic, key=lambda c: c.score, reverse=True)[:epi_max]
    sub_sorted = sorted(subconscious, key=lambda c: c.score, reverse=True)[:sub_max]

    rrf_scores = {}
    all_candidates = {}
    for rank, c in enumerate(concept_sorted):
        rrf = w_c / (rrf_k + rank + 1)
        rrf_scores[c.id] = rrf_scores.get(c.id, 0) + rrf
        all_candidates[c.id] = c
    for rank, c in enumerate(epi_sorted):
        rrf = w_e / (rrf_k + rank + 1)
        rrf_scores[c.id] = rrf_scores.get(c.id, 0) + rrf
        all_candidates[c.id] = c
    for rank, c in enumerate(sub_sorted):
        rrf = w_s / (rrf_k + rank + 1)
        rrf_scores[c.id] = rrf_scores.get(c.id, 0) + rrf
        all_candidates[c.id] = c

    for cid, c in all_candidates.items():
        c.metadata.setdefault("calibrated_score", c.score)
        c.metadata["rrf_score"] = rrf_scores.get(cid, 0.0)
        c.score = rrf_scores.get(cid, c.score)

    return sorted(all_candidates.values(), key=lambda c: c.score, reverse=True)


def _cand_abs_score(c) -> float:
    """候选的绝对相关分（跨查询可比，用于优先级/gap 判断）。

    优先 CE raw rerank_score（Qwen3-Reranker 类输出 [0,1]）；负数（bge 类
    logits 可全负）降级到 embedding_sim——负分会把命中段排到恒定基底之后
    （A4.9 复审 Important #2）。与 min-max 归一化后的 calibrated_score
    （top 恒 1.0）不同，本分可直接跨查询比较。
    """
    rk = c.metadata.get("rerank_score")
    if rk is not None and float(rk) > 0:
        return float(rk)
    sim = c.metadata.get("embedding_sim")
    if sim is not None:
        return float(sim)
    cal = c.metadata.get("calibrated_score")
    if cal is not None:
        return float(cal)
    return float(c.score) if c.score <= 1.0 else 0.5


def _cand_gate_score(c) -> float:
    """门槛专用绝对相关分（A4.9 审查 I2 修复 + 2026-08-16 盲区 A 修正）。

    与 _cand_abs_score 的区别：
    - CE raw rerank_score 钳制到 [0,1]——bge-reranker 类原始 logit ∈ [-10,10]，
      正 logit（如 +2.5）原样通过会绕过 0.35/0.5 门槛；
    - rerank_score 缺失（CE/LLM rerank 未跑或候选不在 rerank pool）时，
      calibrated_score 必为 stage3 的 hybrid 绝对分（embedding×0.6+bm25×0.4，
      或 BM25-only 归一化 ≤0.4，均在 [0,1]）→ 可信，与 embedding_sim 取 max
      ——BM25 强词法命中但 embedding 弱相关的候选（如查询含概念全名）不因
      门槛被误杀（盲区 A）；
    - rerank_score 存在但 ≤0（CE 输出非正 → min-max calibrated 不可信）且无
      embedding_sim 时返回 0.0（BM25-only 被 CE 否定的路径不注入）。
    """
    rk = c.metadata.get("rerank_score")
    if rk is not None and float(rk) > 0:
        return min(max(float(rk), 0.0), 1.0)
    if rk is not None:
        # rerank 已跑且输出非正：分模式判定（A4.9 wave2/3 审查 M2 修正）
        # - CE 模式（cross_encoder，bge raw logits 可全负 [-10,10]）：非正是噪声
        #   信号，min-max calibrated 不可信 → embedding_sim 存在则以其为准，否则拒绝
        # - LLM 模式（rerank_mode=llm，输出显式 [0,1]）：0.0 是真判定（最差）→ 直接拒绝
        sim = c.metadata.get("embedding_sim")
        if sim is not None and config.memory.get("rerank_mode", "score_only") == "cross_encoder":
            return float(sim)
        return 0.0
    sim = c.metadata.get("embedding_sim")
    cal = c.metadata.get("calibrated_score")
    try:
        cal_f = float(cal) if cal is not None else 0.0
    except (TypeError, ValueError):
        cal_f = 0.0
    if sim is not None:
        return max(float(sim), min(cal_f, 1.0))
    # 无 embedding 确认（sim 缺失）的候选，按来源分派（盲区 D，2026-08-16 校准实证）：
    # - BM25 来源：stage3 归一化后 score 必 ≤ bm25_score_weight（默认 0.4）——压线分
    #   不可信 → 拒绝；score>1 ⟹ 归一化未执行（embedding 故障降级路径的原始 BM25
    #   分）→ 保守 0.5 保留词法注入（盲区 A2）
    # - 扩展候选（cluster/relation）：正常模式 score 被钳到 ≤0.4，非绝对相关分
    #   （"南京古落马涧"查询曾注入 3 个完全无关的扩展候选 gate=0.400 压线）→ 拒绝；
    #   降级模式下扩展分 = seed.score×edge×decay 可 >1 → 与 BM25 同构保留 0.5 兜底
    #   （A4.9 wave2/3 审查 I2：防降级模式下扩展召回整层消失）
    src = c.metadata.get("source")
    if src in _BM25_SOURCES or src in ("cluster_expansion", "relation_expansion"):
        s = c.score
        try:
            s = float(s)
        except (TypeError, ValueError):
            return 0.0
        return 0.5 if s > 1.0 else 0.0
    # 其他来源（理论未覆盖）：保守回落 calibrated
    return min(cal_f, 1.0)


def _should_skip_ce(ordered: list["RetrievalCandidate"], gap_trigger: float) -> bool:
    """CE 早退判断（2026-08-09 改进）：用绝对相关分而非 RRF 秩次分。

    旧实现用 rrf_score（~0.008-0.016）比 gap 0.1 → 永不触发早退 → CE 每查询
    必跑。绝对相关分（CE raw / embedding_sim）跨查询可比：top1-top3 差距
    ≥ gap 说明排序已确定，跳过 CE 省调用；top1 强命中（sim ≥ 0.7）同理。
    """
    if len(ordered) < 3:
        return True
    top1 = _cand_abs_score(ordered[0])
    top3 = _cand_abs_score(ordered[2])
    if top1 - top3 >= gap_trigger:
        return True
    # 强命中保险丝：top1 embedding sim 已足够高 → CE 大概率不改序
    strong_sim = float(config.memory.get("stage4_ce_skip_strong_top_sim", 0.7) or 0)
    if strong_sim > 0:
        top1_sim = ordered[0].metadata.get("embedding_sim")
        if top1_sim is not None and float(top1_sim) >= strong_sim:
            return True
    return False


async def _stage4_rerank(
    candidates: list[RetrievalCandidate], query_text: str,
    user_id: str | None = None, db: AsyncSession | None = None, cold_start: bool = False,
) -> list[RetrievalCandidate]:
    """§5.2 Stage 4：默认 score_only（零 LLM）；候选区分度不足且显式开启时触发 rerank。

    触发主开关 = stage4_llm_enabled（§5.2 触发条件）；rerank_mode 选择触发后
    用哪种 reranker：
      llm / score_only → 模式 A，复用主模型（零新部署依赖）
      cross_encoder    → 模式 B，外部署 CE 服务（rerank_api_base，TEI/Xinference 兼容）
    """
    ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
    if not ordered:
        return ordered

    if not config.memory.get("rerank_enabled", True):
        return ordered

    mode = config.memory.get("rerank_mode", "score_only")
    if mode == "cross_encoder":
        # §5.2 模式 B：cross-encoder 是独立部署的 CE 服务（rerank_api_base），
        # 与主 LLM 无关——只受 rerank_enabled 门控，不受 stage4_llm_enabled
        # 误伤（stage4_llm_enabled 是 LLM 模式 A 的触发开关）。
        # cold_start 不跳过：CE 是廉价专用服务（~百 ms），冷启动用户更需要
        # 相关性纠偏（A4.6 根因 2：低活跃用户 6 active < 10 阈值，旧代码永远跳过）。
        # rerank_off 降级（§9.10）：cost governance 命中即强制 score_only
        if user_id is not None:
            try:
                from app.services.memory_cost_governance_service import is_step_enabled
                if not await is_step_enabled(user_id, "rerank_off", db):
                    return ordered
            except Exception:
                pass
        # 2026-08-09：早退改用绝对相关分（旧 rrf_score 永不触发 → CE 必跑）
        gap_trigger = float(config.memory.get("stage4_llm_trigger_score_gap", 0.1))
        if _should_skip_ce(ordered, gap_trigger):
            return ordered
        return await _stage4_cross_encoder(ordered, query_text)

    # 模式 A：LLM/score_only 由 stage4_llm_enabled 触发
    if not config.memory.get("stage4_llm_enabled", False):
        return ordered
    if cold_start:
        return ordered

    # rerank_off 降级（§9.10）：cost governance 命中即强制 score_only
    if user_id is not None:
        try:
            from app.services.memory_cost_governance_service import is_step_enabled
            if not await is_step_enabled(user_id, "rerank_off", db):
                return ordered
        except Exception:
            pass

    # 2026-08-09：早退改用绝对相关分（旧 rrf_score 永不触发）
    gap_trigger = float(config.memory.get("stage4_llm_trigger_score_gap", 0.1))
    if _should_skip_ce(ordered, gap_trigger):
        return ordered

    if config.memory.get("rerank_mode", "score_only") == "cross_encoder":
        return await _stage4_cross_encoder(ordered, query_text)

    # 模式 A：LLM reranker（复用主模型，零新部署依赖）
    try:
        from app.services.memory_llm_factory import _memory_llm
        lines = []
        for i, c in enumerate(ordered[:12]):
            tier_label = {"concept": "概念", "episodic": "事件", "subconscious": "原文"}.get(c.tier, c.tier)
            text_preview = c.content[:100] if c.tier != "concept" else f"{c.metadata.get('canonical_name', '')} {c.content[:80]}"
            lines.append(f"[{i}] [{tier_label}] {text_preview}")
        llm = _memory_llm("query_expansion")
        resp = await asyncio.wait_for(
            llm.complete_chat(
                [
                    {"role": "system", "content": (
                        "你是记忆重排序助手。按与用户问题的相关性给候选打分。"
                        "概念是抽象事实，事件是多轮叙事，原文是低信任级 verbatim。"
                        "输出 JSON: {\"ranked\": [{\"index\": 0, \"score\": 0.0-1.0}]}"
                    )},
                    {"role": "user", "content": f"用户问题：{query_text}\n\n候选：\n" + "\n".join(lines)},
                ],
                temperature=0.1,
            ),
            timeout=float(config.memory_retrieval.get("stage4_llm_timeout_ms", 500)) / 1000.0 + 1.0,
        )
        resp = (resp or "").strip()
        if resp.startswith("```"):
            resp = "\n".join(l for l in resp.split("\n") if not l.startswith("```"))
        parsed = json.loads(resp)
        ranked = parsed.get("ranked", [])
        for item in ranked:
            idx = item.get("index")
            score = item.get("score")
            if isinstance(idx, int) and 0 <= idx < min(len(ordered), 12) and isinstance(score, (int, float)):
                ordered[idx].metadata["rerank_score"] = float(score)
                ordered[idx].metadata["calibrated_score"] = float(score)
        reranked = [c for c in ordered[:12] if "rerank_score" in c.metadata]
        reranked.sort(key=lambda c: c.metadata["rerank_score"], reverse=True)
        reranked_ids = {id(c) for c in reranked}
        rest = [c for c in ordered if id(c) not in reranked_ids]
        return reranked + rest
    except Exception:
        logger.warning("stage4 LLM rerank failed, fallback score_only", exc_info=True)
        return ordered


async def _stage4_cross_encoder(
    ordered: list[RetrievalCandidate], query_text: str,
) -> list[RetrievalCandidate]:
    """§5.2 Stage 4 模式 B：cross-encoder reranker（需额外部署，默认关）。

    rerank_api_base 指向 TEI / Xinference 兼容端点（POST /rerank）：
      请求 {model, query, documents, top_n}
      TEI 响应 [{"index": i, "score": s}]；
      Xinference/Jina 响应 {"results": [{"index": i, "relevance_score": s}]}
    document 构成（§5.2）：Concept = canonical_name + description_short；
    Episodic/Subconscious = narrative/raw_text 前 256 字符。
    失败/超时回退 score_only 序（§9.1 降级链）。

    2026-08-09：候选池改为 tier 均衡（concept 8/episodic 4/subconscious 4，
    复用 RRF 各 tier 上限），不再 flat top-12——subconscious RRF 权重 0.5
    恒垫底，flat 切片把最相关原文排除在 CE 之外（A4.6 根因 2 延伸）。
    """
    api_base = (config.memory.get("rerank_api_base") or "").rstrip("/")
    if not api_base:
        logger.warning("rerank_mode=cross_encoder 但 rerank_api_base 未配置，回退 score_only")
        return ordered
    ret_cfg = config.memory_retrieval
    caps = {
        "concept": int(ret_cfg.get("injection_max_concept", 4)) * 2,
        "episodic": int(ret_cfg.get("injection_max_episodic", 2)) * 2,
        "subconscious": int(ret_cfg.get("injection_max_subconscious", 2)) * 2,
    }
    pool: list[RetrievalCandidate] = []
    seen: set = set()
    for tier, cap in caps.items():
        for c in ordered:
            if c.tier == tier and c.id not in seen and len(pool) < 16:
                pool.append(c)
                seen.add(c.id)
            if len([p for p in pool if p.tier == tier]) >= cap:
                break
    if not pool:
        return ordered
    docs = []
    for c in pool:
        if c.tier == "concept":
            docs.append(f"{c.metadata.get('canonical_name', '')} {c.content[:200]}".strip())
        else:
            docs.append(c.content[:256])
    if not docs:
        return ordered
    headers = {"Content-Type": "application/json"}
    api_key = config.memory.get("rerank_api_key") or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout_s = float(config.memory_retrieval.get("stage4_cross_encoder_timeout_ms", 800)) / 1000.0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s + 0.5)) as client:
            resp = await client.post(
                f"{api_base}/rerank",
                json={
                    "model": config.memory.get("rerank_model", "bge-reranker-v2-m3"),
                    "query": query_text,
                    "documents": docs,
                    "top_n": len(docs),
                },
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        items = data if isinstance(data, list) else (data.get("results") or [])
        # TEI/Xinference 返回 raw logits（可负/超 1，bge ≈ [-10,10]）——
        # min-max 归一化到 [0,1] 再写 calibrated_score，否则复合终分
        # max-normalize 时被全负/混合符号毒化（A4.9 round5 复审 Important #1）；
        # rerank_score 保留原始分仅用于本阶段排序（min-max 保序）
        raw_scored: list[tuple[int, float]] = []
        seen_idx: set[int] = set()
        for it in items:
            idx = it.get("index")
            score = it.get("score", it.get("relevance_score"))
            if isinstance(idx, int) and 0 <= idx < len(docs) and isinstance(score, (int, float)) \
                    and math.isfinite(score) and idx not in seen_idx:
                seen_idx.add(idx)
                raw_scored.append((idx, float(score)))
        if not raw_scored:
            return ordered
        lo = min(s for _, s in raw_scored)
        hi = max(s for _, s in raw_scored)
        span = hi - lo
        scored: list[RetrievalCandidate] = []
        for idx, raw in raw_scored:
            norm = (raw - lo) / span if span > 0 else 1.0
            pool[idx].metadata["rerank_score"] = raw
            pool[idx].metadata["calibrated_score"] = norm
            scored.append(pool[idx])
        scored.sort(key=lambda c: c.metadata["rerank_score"], reverse=True)
        scored_ids = {id(c) for c in scored}
        return scored + [c for c in ordered if id(c) not in scored_ids]
    except Exception:
        logger.warning("stage4 cross-encoder rerank failed, fallback score_only", exc_info=True)
        return ordered


def _to_dt(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None


def _composite_score_by_tier(
    candidates: list[RetrievalCandidate], stage0: Stage0Result,
) -> list[RetrievalCandidate]:
    ret_cfg = config.memory_retrieval
    now = datetime.utcnow()

    # relevance 信号取各 tier 内校准分（M&D §5.2 复合终分：不用 RRF 秩次分），
    # 三信号线性组合。2026-08-09 盲区3修复：relevance 用**绝对校准分**而非
    # tier 内 max-normalize——旧实现每个 tier 的 top 候选 relevance 恒 = 1.0，
    # 跨 tier 不可比（0.3 sim 的 concept 与 0.7 sim 的 subconscious 被抹平），
    # 使高相关原文可能输给低相关概念。绝对分（embedding_sim/calibrated ∈ [0,1]）
    # 跨 tier 直接可比。
    for c in candidates:
        cal = c.metadata.get("calibrated_score")
        if cal is None:
            cal = c.metadata.get("embedding_sim")
        if cal is None:
            cal = c.score if c.score <= 1.0 else (c.score / 10.0)
        c.metadata["calibrated_score"] = cal

    for c in candidates:
        cal = float(c.metadata.get("calibrated_score") or 0.0)
        # 绝对 relevance（跨 tier 可比）；BM25-only 候选经 stage3 归一化后
        # calibrated ∈ [0, bm25_weight]，天然低于 embedding 命中——符合预期
        relevance = min(cal, 1.0)

        if c.tier == "concept":
            alpha = float(ret_cfg.get("fusion_concept_relevance", 0.5))
            beta = float(ret_cfg.get("fusion_concept_recency", 0.2))
            gamma = float(ret_cfg.get("fusion_concept_weight", 0.3))

            # §5.2 as-of 语义：include_expired 查询中，已失效候选按"其失效时刻"评估
            # recency/strength（bi-temporal 历史视角），避免被 now 衰减埋没
            ref_now = now
            if stage0.include_expired:
                vt = _to_dt(c.metadata.get("valid_to"))
                if vt:
                    ref_now = vt

            recency_delta = 0.0
            lr = _to_dt(c.metadata.get("last_recalled_at")) or _to_dt(c.metadata.get("created_at"))
            if lr:
                tau = float(ret_cfg.get("recency_tau_days_concept", 14))
                recency_delta = math.exp(-max((ref_now - lr).days, 0) / tau)

            weight = min(c.metadata.get("weight", 0.5), 1.0)
            # 2026-08-10 importance 融合：持久重要性调节 weight 项（γ 有界 0.3，
            # 不压过 relevance；importance 高=偏好类记忆在检索中更受青睐）
            imp_val = c.metadata.get("importance")
            importance = min(float(imp_val if imp_val is not None else 0.5), 1.0)
            if c.metadata.get("memory_type") == "procedural":
                strength = 1.0
            else:
                stability = max(float(c.metadata.get("stability") or 14.0), 1.0)
                s_lr = _to_dt(c.metadata.get("last_recalled_at")) or _to_dt(c.metadata.get("created_at"))
                s_days = max((ref_now - s_lr).days, 0) if s_lr else 0
                strength = math.exp(-s_days / stability)
            effective_weight = weight * strength * importance
            c.score = alpha * relevance + beta * recency_delta + gamma * effective_weight

        elif c.tier == "episodic":
            alpha = float(ret_cfg.get("fusion_episodic_relevance", 0.6))
            beta = float(ret_cfg.get("fusion_episodic_recency", 0.4))
            recency_delta = 0.0
            vf = _to_dt(c.metadata.get("valid_from"))
            if vf:
                tau = float(ret_cfg.get("recency_tau_days_episodic", 21))
                recency_delta = math.exp(-max((now - vf).days, 0) / tau)
            c.score = alpha * relevance + beta * recency_delta

        elif c.tier == "subconscious":
            alpha = float(ret_cfg.get("fusion_subconscious_relevance", 0.4))
            beta = float(ret_cfg.get("fusion_subconscious_recency", 0.6))
            recency_delta = 0.0
            ca = _to_dt(c.metadata.get("created_at"))
            if ca:
                tau = float(ret_cfg.get("recency_tau_days_subconscious", 7))
                recency_delta = math.exp(-max((now - ca).days, 0) / tau)
            c.score = alpha * relevance + beta * recency_delta

    return sorted(candidates, key=lambda c: c.score, reverse=True)


def _select_injected(
    candidates: list[RetrievalCandidate],
) -> dict[str, list[RetrievalCandidate]]:
    """注入切片选择（SOTA 硬门槛：ScoreGate/Databricks relevance floor）。

    2026-08-16 修复：旧实现无绝对相关分门槛——top-N 恒有候选注入，高权重
    概念即使与查询几乎无关也会挤进注入位并被 boost，形成"权重高→必注入→
    注入即boost→权重更高"的自我强化死循环（实测：科技新闻查询在 03:30:07
    把《火焰守望者》概念卷入注入）。现在低于 injection_min_relevance 的
    概念级候选直接丢弃，零命中时宁可空段也不注入噪声。
    """
    ret_cfg = config.memory_retrieval
    concept_floor = float(ret_cfg.get("injection_min_relevance", 0.35))
    episodic_floor = float(ret_cfg.get("injection_min_relevance_episodic", 0.30))
    sub_floor = float(ret_cfg.get("injection_min_relevance_subconscious", 0.30))
    floors = {"concept": concept_floor, "episodic": episodic_floor,
              "subconscious": sub_floor}

    def _slice(tier: str, max_n: int) -> list[RetrievalCandidate]:
        floor = floors.get(tier, 0.0)
        eligible = [c for c in candidates if c.tier == tier and _cand_gate_score(c) >= floor]
        return eligible[:max_n]

    return {
        "concept": _slice("concept", int(ret_cfg.get("injection_max_concept", 4))),
        "episodic": _slice("episodic", int(ret_cfg.get("injection_max_episodic", 2))),
        "subconscious": _slice("subconscious", int(ret_cfg.get("injection_max_subconscious", 2))),
    }


_boost_tasks: set = set()


async def _apply_recall_boosts_bg(
    user_id: str, concept_ids: list[str], episode_ids: list[str], unit_ids: list[str],
) -> None:
    from app.db.database import AsyncSessionLocal
    from app.services.memory_weight_service import (
        apply_episode_recall_boost,
        apply_recall_boost,
        apply_subconscious_recall_boost,
    )
    # 三腿独立会话提交：单腿失败不拖垮其他腿（事务 aborted 会级联回滚）
    for leg, ids in (
        ("concept", concept_ids),
        ("episode", episode_ids),
        ("subconscious", unit_ids),
    ):
        if not ids:
            continue
        try:
            async with AsyncSessionLocal() as session:
                if leg == "concept":
                    await apply_recall_boost(session, ids, user_id)
                elif leg == "episode":
                    await apply_episode_recall_boost(session, ids, user_id=user_id)
                else:
                    await apply_subconscious_recall_boost(session, ids)
                await session.commit()
        except Exception:
            logger.warning("recall boost failed for user=%s leg=%s", user_id, leg, exc_info=True)

    # §5.3.1a-2：多轮复现窗口 boost（≥3 次/5 轮）
    try:
        async with AsyncSessionLocal() as session:
            await _apply_window_boosts(session, user_id)
            await session.commit()
    except Exception:
        logger.debug("window boost failed for user=%s", user_id, exc_info=True)


async def _build_memory_overview(
    db: AsyncSession, user_id: str, max_chars: int | None = None,
) -> tuple[str, set, set]:
    """用户长期记忆总览基底段（方案 A''，2026-08-03）。

    从 memory_concepts / memory_episodes 纯 SQL 聚合（请求路径无 LLM /
    embedding 调用，仅 ~2 次索引点查，ms 级）。恒定注入，保证检索零命中
    时 prompt 仍携带用户长期画像——恢复 v1 无条件注入优势，同时保持单一
    v2 数据源。返回 (text, concept_ids, episode_ids) 供注入侧去重，避免
    与检索命中段重复消耗预算。

    2026-08-09：max_chars 可调用方注入——强命中时收缩基底，让位给相关
    内容（A4.6 根因 3：固定 800 字符总览挤占预算，最相关原文被截断）。
    """
    max_concepts = int(config.memory_retrieval.get("overview_max_concepts", 12))
    max_episodes = int(config.memory_retrieval.get("overview_max_episodes", 5))
    max_chars = max_chars if max_chars is not None else int(config.memory_retrieval.get("overview_max_chars", 800))
    lines = ["[用户长期记忆总览]"]
    cids: set = set()
    eids: set = set()

    def _over_budget() -> bool:
        return sum(len(l) for l in lines) > max_chars

    try:
        # 2026-08-16 盲区 B 修复：与 dream/decay 同一有效权重排序——旧实现按原始
        # weight DESC，衰减写回生效前高权重旧概念仍恒定出现在每个请求的基底里。
        # A4.9 wave2/3 审查 M5/M7：SELECT 展示有效权重（与排序依据一致，口径与
        # dream payload 统一）；weight > 0.05 粗筛缩小行集（触底概念无展示价值）。
        from app.services.memory_weight_service import effective_weight_sql
        eff_expr = effective_weight_sql()
        result = await db.execute(
            text(
                "SELECT canonical_name, description_short, weight, memory_type, aliases, id, source_trust, "
                "ROUND(CAST(" + eff_expr + " AS numeric), 2) "
                "FROM memory_concepts "
                "WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 "
                "AND valid_to IS NULL AND (valid_from IS NULL OR valid_from <= NOW()) "
                "AND memory_type != 'profile' AND weight > 0.05 "
                "ORDER BY " + eff_expr + " DESC, created_at DESC LIMIT :limit"
            ),
            {"uid": user_id, "limit": max_concepts},
        )
        rows = result.fetchall()
    except Exception:
        logger.exception("memory overview concepts query failed")
        rows = []
    for row in rows:
        name = (row[0] or "")[:60]
        desc = (row[1] or "").strip().replace("\n", " ")
        if len(desc) > 60:
            desc = desc[:60] + "..."
        weight = float(row[7]) if row[7] is not None else 0
        mtype = (row[3] or "").strip()
        aliases_raw = row[4] or ""
        try:
            aliases_parsed = json.loads(aliases_raw) if isinstance(aliases_raw, str) and aliases_raw.strip() else []
            aliases = ", ".join(aliases_parsed) if isinstance(aliases_parsed, list) else ""
        except (json.JSONDecodeError, TypeError):
            aliases = ""
        if len(aliases) > 60:
            aliases = aliases[:60] + "..."
        trust = (row[6] or "").strip()
        trust_tag = "（推断，未经用户确认）" if trust == "agent_inferred" else ""
        mtype_tag = f"（{mtype}）" if mtype else ""
        alias_part = f"（{aliases}）" if aliases else ""
        lines.append(f"- {name}{alias_part}{mtype_tag}: {desc}{trust_tag} [权重: {weight:.2f}]")
        cids.add(row[5])
        if _over_budget():
            break

    try:
        result = await db.execute(
            text(
                "SELECT narrative, valid_from, id FROM memory_episodes "
                "WHERE user_id = :uid AND valid_to IS NULL AND superseded_by IS NULL "
                "AND (valid_from IS NULL OR valid_from <= NOW()) "
                "ORDER BY valid_from DESC NULLS LAST, created_at DESC LIMIT :limit"
            ),
            {"uid": user_id, "limit": max_episodes},
        )
        epi_rows = result.fetchall()
    except Exception:
        logger.exception("memory overview episodes query failed")
        epi_rows = []
    for row in epi_rows:
        narrative = (row[0] or "").strip().replace("\n", " ")
        if len(narrative) > 80:
            narrative = narrative[:80] + "..."
        vf = row[1]
        date_part = vf.strftime("%Y-%m-%d") if hasattr(vf, "strftime") else str(vf or "")
        lines.append(f"- 事件 [{date_part}] {narrative}")
        eids.add(row[2])
        if _over_budget():
            break

    if len(lines) == 1:
        return "", cids, eids
    return "\n".join(lines), cids, eids


async def _build_injection_context(
    db: AsyncSession, user_id: str,
    candidates: list[RetrievalCandidate], stage0: Stage0Result,
    query_text: str = "",
) -> str:
    ret_cfg = config.memory_retrieval
    # (priority, text)：按相关性降序注入，预算截断从最低优先段开始，
    # 保证高相关内容（常为 subconscious 原文）不被恒定基底挤掉（A4.6 根因 3）。
    # 段优先级用绝对相关分（CE raw rerank_score > embedding_sim > calibrated）
    # 而非 min-max 归一化后的 calibrated（top 恒 1.0，跨查询不可比）——
    # 否则无关但高权重概念段被抬到 1.0，画像基底被预算截断
    # （A4.6 根因 4：YearDoo 选书查询，长干里工作概念 cal=1.0 vs 画像）。
    sections: list[tuple[float, str]] = []

    def _section_score(tier: str) -> float:
        # A4.9 复审 Minor：只对实际注入切片（去重 + top-k）取分，
        # 避免被总览已展示/未注入的高分候选虚高优先级。
        # A4.9 wave2/3 审查 I1：切片内候选均过注入门槛，统一用 gate 分
        # 与 strong_hit 判定口径一致（abs 分可能因 min-max calibrated 虚高）。
        if tier == "episodic":
            slice_c = epi_slice
        elif tier == "concept":
            slice_c = concept_slice
        elif tier == "subconscious":
            slice_c = sub_slice
        else:
            slice_c = [c for c in candidates if c.tier == tier]
        if not slice_c:
            return 0.0
        return max(_cand_gate_score(c) for c in slice_c)

    # 总览基底：恒定注入优先级固定（零命中时仍有用户画像）。
    # 强命中（top 候选绝对相关分 ≥ 0.5）时收缩基底，让位给相关内容——
    # 收缩到 1/3 而非 1/2：命中段（~1500 字符）后仍须塞进 2000 预算
    # （A4.6 根因 3：固定 800 字符总览 + 命中段 > 预算 → 总览被丢弃）。
    # A4.9 复审修正：用绝对相关分而非 composite——composite 改绝对分后
    # 概念 sim 0.4-0.6 只得 ~0.3-0.45，旧阈值（≥0.5 composite）不再触发
    # 收缩 → 800 字符总览挤压画像/dream 段（A4.6 根因 3 缓解部分回退）。
    # A4.9 wave2/3 审查 I1：改用 _cand_gate_score——被注入门槛拒绝的候选
    # （rk≤0 无 sim 的 BM25-only/扩展候选）不应触发总览收缩（否则总览被
    # 收缩、让位预算，却没有任何强命中段填充）。
    strong_hit = any(_cand_gate_score(c) >= 0.5 for c in candidates)
    overview_base_chars = int(ret_cfg.get("overview_max_chars", 800))
    overview_chars = max(overview_base_chars // 3, 200) if strong_hit else overview_base_chars
    overview_cids: set = set()
    overview_eids: set = set()
    try:
        overview, overview_cids, overview_eids = await _build_memory_overview(
            db, user_id, max_chars=overview_chars,
        )
        if overview:
            sections.append((0.35, overview))
    except Exception:
        logger.exception("memory overview build failed")

    injected = _select_injected(candidates)

    # 去重前置到切片前：overview 已显示的概念/事件不再重复注入，
    # 且非重叠的后续候选仍可补满段（避免段欠填）。
    # 2026-08-16：注入切片与 _select_injected 同一门槛（injection_min_relevance*），
    # 低于绝对相关分门槛的候选不注入——防止高权重低相关概念占位并触发 boost 死循环。
    _floors = {
        "episodic": float(ret_cfg.get("injection_min_relevance_episodic", 0.30)),
        "concept": float(ret_cfg.get("injection_min_relevance", 0.35)),
        "subconscious": float(ret_cfg.get("injection_min_relevance_subconscious", 0.30)),
    }
    epi_slice = [
        c for c in candidates
        if c.tier == "episodic" and c.id not in overview_eids
        and _cand_gate_score(c) >= _floors["episodic"]
    ][:int(ret_cfg.get("injection_max_episodic", 2))]
    concept_slice = [
        c for c in candidates
        if c.tier == "concept" and c.id not in overview_cids
        and _cand_gate_score(c) >= _floors["concept"]
    ][:int(ret_cfg.get("injection_max_concept", 4))]
    sub_slice = injected["subconscious"]

    if epi_slice:
        lines = ["[相关事件 Episodic]"]
        for c in epi_slice:
            vf = c.metadata.get("valid_from", "")
            lines.append(f"- [{vf}] {c.content[:200]}")
        sections.append((_section_score("episodic"), "\n".join(lines)))

    if concept_slice:
        lines = ["[相关概念 Concept]"]
        for c in concept_slice:
            name = c.metadata.get("canonical_name", "")
            aliases_raw = c.metadata.get("aliases", "")
            try:
                aliases = ", ".join(json.loads(aliases_raw)) if isinstance(aliases_raw, str) and aliases_raw else ""
            except (json.JSONDecodeError, TypeError):
                aliases = ""
            weight = c.metadata.get("weight", 0)
            trust = c.metadata.get("source_trust", "")
            tag = " （推断，未经用户确认）" if trust == "agent_inferred" else ""
            valid_to = _to_dt(c.metadata.get("valid_to"))
            expired_tag = f" （已于 {valid_to.strftime('%Y-%m-%d')} 失效）" if valid_to else ""
            alias_part = f"（{aliases}）" if aliases else ""
            lines.append(f"- {name}{alias_part}: {c.content}{tag}{expired_tag} [权重: {weight:.2f}]")
        sections.append((_section_score("concept"), "\n".join(lines)))

    dream_text = await _get_latest_dream(db, user_id)
    if dream_text:
        sections.append((0.25, f"[近期 Dream]\n{dream_text[:500]}"))

    # 用户画像基底（A4.6 根因 4；2026-08-09 写路径版）：profile 概念由
    # 调度器每日 LLM 提炼入库（memory_profile_service.sync_profile_concepts），
    # 读路径恒定注入小体积（≤600 字符），优先级固定 0.30——
    # 不再做请求时关键字扫描 / 行级 embedding / 相似度魔法数字（A4.9 复审
    # Important #1/#2 的根治：写路径结构化 + 恒定低优先，而非运行时启发式）。
    # profile 概念带 embedding，相关查询会被 stage1-4 正常命中提升。
    try:
        profile = await _get_profile_summary(db, user_id, max_chars=600)
        if profile:
            sections.append((0.30, f"[用户画像 Profile]\n{profile}"))
    except Exception:
        logger.debug("profile summary load failed", exc_info=True)

    concept_ids = [c.id for c in concept_slice]
    if concept_ids:
        from app.services.memory_cluster_service import get_clusters_for_concepts
        clusters = await get_clusters_for_concepts(db, concept_ids)
        if clusters:
            lines = ["[相关概念集合 Clusters]"]
            for cl in clusters[:int(ret_cfg.get("injection_max_cluster", 3))]:
                lines.append(f"- {cl['name']}: {cl.get('summary', '') or ''}")
            sections.append((0.2, "\n".join(lines)))

    if sub_slice:
        lines = ["[近期原文片段 Subconscious]"]
        for c in sub_slice:
            ca = c.metadata.get("created_at", "")
            lines.append(f"- [{ca}] {c.content[:200]} (未经整理)")
        sections.append((_section_score("subconscious"), "\n".join(lines)))

    try:
        from app.services.memory_clarification_service import get_recent_clarifications
        clarifications = await get_recent_clarifications(db, user_id, days=3)
    except Exception:
        logger.debug("load clarifications failed", exc_info=True)
        clarifications = []
    if clarifications:
        lines = ["[澄清提示]"]
        for cl in clarifications[:int(ret_cfg.get("injection_max_clarification", 3))]:
            lines.append(f"- 用户澄清\"{cl['original_text'][:50]}\" → 已修正")
        sections.append((0.1, "\n".join(lines)))

    # §5.6 注入总预算硬上限（injection_total_token_budget，默认 2000 token；
    # 中文按 1 字≈1 token 保守估算，超预算从最低优先级段开始丢弃）
    sections.sort(key=lambda s: s[0], reverse=True)
    budget = int(ret_cfg.get("injection_total_token_budget", 2000))
    return _apply_token_budget([s[1] for s in sections], budget)


def _apply_token_budget(sections: list[str], budget: int) -> str:
    """按段优先级保留，累计估算 token ≤ budget；单段超长时截断该段。"""
    kept: list[str] = []
    used = 0
    for section in sections:
        est = len(section)
        if used + est <= budget:
            kept.append(section)
            used += est
        elif not kept:
            kept.append(section[:budget])
            used = budget
            break
        else:
            break
    return "\n\n".join(kept)


async def _get_latest_dream(db: AsyncSession, user_id: str) -> str | None:
    # 2026-08-09 修复：① 限定 consolidation 类型（passive dream 每天由 consolidation
    # 管线写入，不受 dreaming_enabled 门控；该开关只门控主动 dream 第 6 步）；
    # ② `ORDER BY created_at DESC` 必须显式 NULLS LAST——PostgreSQL 降序默认
    # NULLS FIRST，而 agent_dreams 有 134 条历史行 created_at 为 NULL（约 5%），
    # 旧代码总是选中 NULL 行 → [近期 Dream] 注入过时内容（实测低活跃用户返回
    # 08-02 的 workspace_read 而非 08-09 的新 dream）。
    # A4.9 复审 Minor：runtime 禁用（v1 模式）时 consolidation 调度停止、v1 行被
    # nightly 过滤 → 本函数返回空——退回任意类型最新行（v1 模式无注入歧义）。
    result = await db.execute(
        text("SELECT summary FROM agent_dreams WHERE agent_state_id = (SELECT id FROM user_agent_states WHERE user_id = :uid) AND dream_type = 'consolidation' ORDER BY created_at DESC NULLS LAST, generated_for_date DESC LIMIT 1"),
        {"uid": user_id},
    )
    row = result.fetchone()
    if row:
        return row[0]
    result = await db.execute(
        text("SELECT summary FROM agent_dreams WHERE agent_state_id = (SELECT id FROM user_agent_states WHERE user_id = :uid) ORDER BY created_at DESC NULLS LAST, generated_for_date DESC LIMIT 1"),
        {"uid": user_id},
    )
    row = result.fetchone()
    return row[0] if row else None


_PROFILE_STABLE_CACHE: dict[str, tuple[float, list[str]]] = {}

_PROFILE_STABLE_PROMPT = (
    "你是用户画像清洗员。下面的文本是用户的记忆摘要，可能包含'偏好变更叙述'"
    "（如'从 VSCode 换成 Vim'、'改用 XX'、'不再用 YY'）——这类行描述的是过去的"
    "偏好变化过程，不代表当前稳定偏好，恒定注入会污染语义。\n"
    "请只输出描述**稳定画像事实**的行（职业、家庭、居住、长期爱好等），"
    "删除所有变化叙述行。\n"
    '输出JSON：{"stable_lines": ["保留的行原文1", ...]}\n只输出JSON，不要输出其他内容。'
)


async def _filter_stable_profile_lines(summary: str, user_id: str) -> list[str]:
    """LLM-judged stable-profile filtering (agentic principle — the former
    _CHANGE_RE regex could not generalize to arbitrary change narrations).

    Cached 10 minutes per user. LLM failure → keep ALL lines (permissive:
    contamination is a quality issue, dropping real profile facts is worse).
    """
    lines = [ln.strip() for ln in (summary or "").splitlines() if ln.strip()]
    if not lines:
        return []
    now = time.monotonic()
    cached = _PROFILE_STABLE_CACHE.get(user_id)
    if cached and now - cached[0] < 600:
        return cached[1]
    try:
        from app.services.agentic_judge import judge_json

        numbered = "\n".join(f"{i}. {ln[:150]}" for i, ln in enumerate(lines))
        parsed = await judge_json(
            _PROFILE_STABLE_PROMPT,
            f"摘要文本：\n{numbered[:5000]}\n\n只输出JSON。",
            task="identity_facts",
            default=None,
            timeout=25.0,
        )
        stable = lines
        if isinstance(parsed, dict) and isinstance(parsed.get("stable_lines"), list):
            chosen = [str(e) for e in parsed["stable_lines"]]
            stable = [ln for ln in lines if ln in chosen]
        if len(_PROFILE_STABLE_CACHE) > 512:
            _PROFILE_STABLE_CACHE.clear()
        _PROFILE_STABLE_CACHE[user_id] = (now, stable)
        return stable
    except Exception as exc:
        logger.warning("stable-profile LLM filter failed: %s", exc)
        return lines


async def _get_profile_summary(db: AsyncSession, user_id: str, max_chars: int = 600) -> str | None:
    """用户画像基底（2026-08-09 写路径版）。

    v2 检索管线此前完全不读 v1 摘要——profile 事实（家庭成员、年龄、偏好等）
    只存在于 v1 摘要时（迁移未跑 / 概念未抽取），v2 命中为零，用户问
    "给孩子选书" 时孩子信息永不出现（A4.6 根因 4：选书案例）。

    读路径改为：优先取**写路径**提炼的 profile 概念
    （memory_profile_service.sync_profile_concepts，调度器每日 LLM 提炼入库，
    带 embedding，走正常 v2 召回）；profile 概念尚未生成时兜底合并最新
    memory_summary。恒定基底，按注入优先级排在强命中之后。
    """
    lines: list[str] = []
    try:
        from app.services.memory_profile_service import get_profile_concepts
        facts = await get_profile_concepts(db, user_id, limit=6)
        for f in facts:
            name = (f.get("name") or "").strip()
            desc = (f.get("desc") or "").strip()
            if name and desc:
                lines.append(f"- {name}: {desc}")
    except Exception:
        logger.debug("profile concepts load failed", exc_info=True)

    # profile 概念未生成时兜底：最新 memory_summary（v1 每日生成）。
    # 2026-08-09 A4.9 实测回归：v1 摘要含偏好变更叙述（"从 VSCode 换成 Vim"），
    # 恒定注入会污染当前偏好语义（as-of 违背：当前查询 must_not 旧偏好）。
    # 兜底时裁剪变化叙述行——只保留稳定画像（职业/家庭/居住等）。
    # 裁剪判断由 LLM 完成（agentic 原则，原 _CHANGE_RE 正则已删除）。
    if not lines:
        result = await db.execute(
            text("SELECT memory_summary FROM user_agent_states WHERE user_id = :uid AND memory_summary IS NOT NULL"),
            {"uid": user_id},
        )
        row = result.fetchone()
        if row and (row[0] or "").strip():
            stable_lines = await _filter_stable_profile_lines(row[0], user_id)
            if not stable_lines:
                # 全是变化叙述（罕见）→ 不注入，避免污染
                return None
            lines.append("\n".join(stable_lines)[:max_chars])

    merged = "\n".join(lines).strip()
    if not merged:
        return None
    if len(merged) > max_chars:
        merged = merged[:max_chars] + "..."
    return merged


async def _count_active_concepts(db: AsyncSession, user_id: str) -> int:
    result = await db.execute(
        text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 AND valid_to IS NULL"),
        {"uid": user_id},
    )
    return result.scalar() or 0
