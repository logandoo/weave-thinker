# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Transcript-grounded memory verification (Anthropic dreaming adaptation, 2026-11-08).

Anthropic 的 dreaming 核心是“拿 transcript 当地面真值纠错 memory notes”。
本服务实现最小闭环：

  memory_concepts.source_unit_ids
    → subconscious_log.raw_text / source_ids
    → messages ids（证据）
    → LLM judge（只读 excerpts，不塞全量对话）
    → memory_concepts.verification_state / last_verified_at / verification_evidence

- confirmed：片段支持笔记；
- contradicted：片段与笔记冲突（不写 valid_to、不删概念；常规检索排除，待核实）；
- insufficient：无来源或片段不足（不调用 LLM 的 no_source 路径也会标 insufficient）。

有界：默认每晚最多 5 个概念、importance ≥ 0.7、14 天复核间隔；失败逐条跳过，
不毒化 consolidation 事务（调用方用 savepoint 包裹）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

VERDICTS = {"confirmed", "contradicted", "insufficient"}
DEFAULT_TOTAL_BUDGET_SECONDS = 240.0


def parse_json_list(raw: Any) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


def extract_message_ids(units: list[dict]) -> list[str]:
    """subconscious_log.source_ids（JSON message id 数组）→ 去重 message ids。"""
    out: list[str] = []
    for u in units or []:
        for mid in parse_json_list(u.get("source_ids")):
            if isinstance(mid, str) and mid and mid not in out:
                out.append(mid)
    return out[:20]


def build_excerpts(units: list[dict], max_units: int = 3, max_chars: int = 500) -> str:
    lines = []
    for i, u in enumerate((units or [])[:max_units], 1):
        txt = str(u.get("raw_text") or "").strip()[:max_chars]
        if txt:
            lines.append(f"[{i}] {txt}")
    return "\n".join(lines)


def build_user_excerpts(units: list[dict], max_units: int = 3, max_chars: int = 500) -> str:
    """只取 user-authored 证据：message 单元剥掉助手段；note 单元保留；
    file_memory（AGENT.md，agent 可写）不进入 confirmed 证据。"""
    lines = []
    for u in (units or []):
        kind = str(u.get("unit_kind") or "")
        txt = str(u.get("raw_text") or "")
        if kind == "message":
            txt = txt.split("\n助手:", 1)[0]
        elif kind != "note":
            continue
        txt = txt.strip()[:max_chars]
        if txt:
            lines.append(f"[{len(lines) + 1}] {txt}")
        if len(lines) >= max_units:
            break
    return "\n".join(lines)


def _norm_text(s: Any) -> str:
    return "".join(str(s or "").split())


def quote_grounded(quote: str, excerpts: str) -> bool:
    """Quote 必须逐字（除空白）出现在 excerpts 中；防止 judge 编造/注入。"""
    q = _norm_text(quote)
    return bool(q) and q in _norm_text(excerpts)


def normalize_verdict(parsed: Any, excerpts: str | None = None) -> tuple[str, str, str]:
    """LLM 输出 → (state, quote, reason)。非法/缺字段/quote 不落地一律 insufficient。"""
    if not isinstance(parsed, dict):
        return "insufficient", "", "invalid_json"
    state = str(parsed.get("verdict") or "").strip().lower()
    if state not in VERDICTS:
        state = "insufficient"
    quote = str(parsed.get("quote") or "").strip()[:200]
    reason = str(parsed.get("reason") or "").strip()[:120]
    if state in ("confirmed", "contradicted"):
        if not quote:
            return "insufficient", "", f"{state}_without_quote"
        if excerpts is not None and not quote_grounded(quote, excerpts):
            return "insufficient", "", "quote_not_grounded"
    return state, quote, reason


def normalize_correction(parsed: Any) -> str | None:
    """B6：judge 可选修正草案；只在 contradicted 时保留，长度受限。"""
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("verdict") or "").strip().lower() != "contradicted":
        return None
    raw = str(parsed.get("suggested_correction") or "").strip()
    return raw[:2000] or None


def build_verify_payload(name: str, description: str, excerpts: list[dict]) -> str:
    """J5：把 note 与片段编码成一个 JSON 数据对象，避免 `</transcript>` 越界。

    Excerpt order is preserved; each item is explicitly data (`text`), never a
    role/instruction channel.
    """
    return json.dumps({
        "note": {"name": str(name or "")[:200], "description": str(description or "")[:500]},
        "excerpts": [
            {"n": i, "text": str(e.get("text") or "")[:500]}
            for i, e in enumerate(excerpts[:3], 1)
        ],
    }, ensure_ascii=False).replace("</", "<\\/")


def build_evidence(source_ids: list[str], quote: str, reason: str,
                   unit_ids: list[str] | None = None,
                   source_kind: str | None = None,
                   suggested_correction: str | None = None) -> str:
    payload = {
        "source_ids": list(source_ids or [])[:10],
        "unit_ids": list(unit_ids or [])[:10],
        "source_kind": source_kind,
        "quote": (quote or "")[:200],
        "reason": (reason or "")[:120],
        "checked_at": datetime.utcnow().isoformat(),
    }
    if suggested_correction:
        payload["suggested_correction"] = suggested_correction[:2000]
    return json.dumps(payload, ensure_ascii=False)


async def _load_units(db: AsyncSession, user_id: str, raw_unit_ids: Any) -> list[dict]:
    ids = [str(x) for x in parse_json_list(raw_unit_ids) if isinstance(x, str) and x]
    if not ids:
        return []
    result = await db.execute(
        text("""SELECT id, raw_text, source_ids, unit_kind FROM subconscious_log
                WHERE user_id = :uid AND id = ANY(:ids) LIMIT 10"""),
        {"uid": user_id, "ids": ids[:10]},
    )
    return [{"id": r[0], "raw_text": r[1], "source_ids": r[2],
             "unit_kind": r[3]} for r in result.fetchall()]


async def _load_units_from_messages(db: AsyncSession, user_id: str,
                                    raw_message_ids: Any) -> list[dict]:
    """B4：没有 subconscious unit 时，直接回读该用户自己的 messages。"""
    ids = [str(x) for x in parse_json_list(raw_message_ids) if isinstance(x, str) and x][:10]
    if not ids:
        return []
    try:
        result = await db.execute(
            text("""SELECT m.id, m.content, m.role FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE m.id = ANY(:ids) AND c.user_id = :uid"""),
            {"uid": user_id, "ids": ids},
        )
        rows = result.fetchall()
    except Exception:
        logger.debug("message evidence lookup failed", exc_info=True)
        return []
    out = []
    for mid, content, role in rows:
        text_val = str(content or "")
        if role and str(role) != "user":
            text_val = f"助手: {text_val}" if role == "assistant" else f"{role}: {text_val}"
        else:
            text_val = f"用户: {text_val}"
        out.append({"id": mid, "raw_text": text_val,
                    "source_ids": json.dumps([mid]), "unit_kind": "message"})
    return out


async def _load_concept_rows(db: AsyncSession, user_id: str, limit: int,
                             min_importance: float, interval_days: int) -> list[dict]:
    # J3：COALESCE(source_type,'') 让 NULL 行也参与候选；J4：畸形 source_unit_ids
    # 在 Python 侧再过滤（SQL 只做粗筛，避免占满 5 个名额）。
    overfetch = max(1, min(int(limit) * 3, 60))
    # C7/P1-09（2026-11-08）：目标化 cadence——高重要（≥ dream_verify_high_importance，
    # 默认 0.85）用半间隔快档复验（杜绝长期未验证的高价值记忆）；同档内
    # 从未验证 → 最早验证 → 近 7 日召回 → profile/extracted 来源优先。
    high_imp = float(config.memory.get("dream_verify_high_importance", 0.85))
    fast_days = max(1, int(config.memory.get(
        "dream_verify_fast_interval_days", max(1, max(1, int(interval_days)) // 2))))
    result = await db.execute(
        text("""
            SELECT id, canonical_name, description_short, description_full,
                   source_unit_ids, source_message_ids, source_kind, source_type
            FROM memory_concepts
            WHERE user_id = :uid AND valid_to IS NULL AND status = 'active'
              AND COALESCE(source_type, '') != 'dreaming'
              AND importance >= :min_imp
              AND (last_verified_at IS NULL
                   OR last_verified_at < NOW() - ((CASE
                            WHEN importance >= CAST(:high_imp AS double precision)
                            THEN CAST(:fast_days AS int) ELSE CAST(:days AS int) END)
                        * INTERVAL '1 day'))
            ORDER BY importance DESC,
                     (last_verified_at IS NOT NULL) ASC,
                     last_verified_at ASC NULLS FIRST,
                     CASE WHEN last_recalled_at IS NOT NULL
                            AND last_recalled_at >= NOW() - INTERVAL '7 days'
                          THEN 1 ELSE 0 END DESC,
                     CASE WHEN COALESCE(source_type, '') IN ('profile_sync', 'extracted')
                          THEN 1 ELSE 0 END DESC,
                     updated_at DESC NULLS LAST
            LIMIT :lim
        """),
        {"uid": user_id, "lim": overfetch, "min_imp": min_importance,
         "high_imp": high_imp, "fast_days": fast_days,
         "days": max(1, int(interval_days))},
    )
    out: list[dict] = []
    for r in result.fetchall():
        raw_units = r[4]
        raw_msgs = r[5] if len(r) > 5 else None
        unit_ids = parse_json_list(raw_units)
        msg_ids = parse_json_list(raw_msgs)
        # J4：非空的畸形 JSON 直接跳过（不占名额）；真正的「无来源」仍进入
        # 候选并标 insufficient（保留审计面），与既有 no_source 语义一致。
        if (raw_units and not unit_ids) or (raw_msgs and not msg_ids):
            continue
        out.append({
            "id": r[0], "name": r[1], "short": r[2], "full": r[3],
            "source_unit_ids": raw_units,
            "source_message_ids": raw_msgs,
            "source_kind": r[6] if len(r) > 6 else None,
            "source_type": r[7] if len(r) > 7 else None,
        })
        if len(out) >= limit:
            break
    return out


async def select_verify_candidates(db: AsyncSession, user_id: str, *,
                                   limit: int | None = None,
                                   min_importance: float | None = None,
                                   interval_days: int | None = None) -> list[dict]:
    """Public selection seam for the backfill script (J9/B2)."""
    lim = max(1, min(int(limit or config.memory.get("dream_verify_max_concepts", 5)), 50))
    min_imp = float(min_importance if min_importance is not None
                    else config.memory.get("dream_verify_min_importance", 0.7))
    days = int(interval_days if interval_days is not None
               else config.memory.get("dream_verify_interval_days", 14))
    return await _load_concept_rows(db, user_id, lim, min_imp, days)


async def _load_episode_rows(db: AsyncSession, user_id: str, limit: int,
                             interval_days: int) -> list[dict]:
    result = await db.execute(
        text("""
            SELECT id, narrative, source_unit_ids
            FROM memory_episodes
            WHERE user_id = :uid AND valid_to IS NULL
              AND source_unit_ids IS NOT NULL
              AND (verification_state IS NULL OR verification_state != 'contradicted')
              AND (last_verified_at IS NULL
                   OR last_verified_at < NOW() - (:days * INTERVAL '1 day'))
            ORDER BY created_at DESC NULLS LAST
            LIMIT :lim
        """),
        {"uid": user_id, "lim": max(1, int(limit)), "days": max(1, int(interval_days))},
    )
    return [{"id": r[0], "narrative": r[1], "source_unit_ids": r[2]}
            for r in result.fetchall()]


def build_user_excerpt_items(units: list[dict], max_units: int = 3,
                             max_chars: int = 500) -> list[dict]:
    """只取 user-authored 证据，返回结构化片段（J5 JSON 编码用）。"""
    items: list[dict] = []
    for u in (units or []):
        kind = str(u.get("unit_kind") or "")
        txt = str(u.get("raw_text") or "")
        if kind == "message":
            txt = txt.split("\n助手:", 1)[0]
        elif kind != "note":
            continue
        txt = txt.strip()[:max_chars]
        if txt:
            items.append({"text": txt})
        if len(items) >= max_units:
            break
    return items


def _source_kind(units: list[dict], fallback: str | None = None) -> str | None:
    kinds = {str(u.get("unit_kind") or "") for u in (units or []) if u.get("unit_kind")}
    if not kinds:
        return fallback
    if kinds <= {"message"}:
        return "message"
    if kinds <= {"note"}:
        return "note"
    return "mixed"


async def _bill_verify(db: AsyncSession, user_id: str) -> None:
    """B11：独立短会话记账，业务事务回滚不会吞掉成本行。"""
    try:
        from sqlalchemy.ext.asyncio import AsyncSession as _RealSession
        if not isinstance(db, _RealSession):
            return
        from app.services.memory_cost_governance_service import record_llm_call_bg
        await record_llm_call_bg(user_id, "dream_verify_llm", billing_class="write")
    except Exception:
        logger.debug("dream verify billing failed (silent)", exc_info=True)


async def record_verify_metrics(stats: dict) -> None:
    """B10：把一次 verify 的统计写入 memory_eval_runs（独立会话，失败静默）。"""
    try:
        from sqlalchemy.ext.asyncio import AsyncSession as _RealSession
        if not isinstance(stats.get("_db"), _RealSession):
            return
        user_id = stats.pop("_user_id", None)
        stats.pop("_db", None)
        from app.db.database import AsyncSessionLocal, MemoryEvalRun

        async with AsyncSessionLocal() as session:
            session.add(MemoryEvalRun(
                run_id=(f"dreamverify-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
                        f"-{uuid.uuid4().hex[:6]}"),
                suite="dream_verify", arm="enforce", user_id=user_id,
                metrics_json={k: v for k, v in stats.items()
                              if not k.startswith("_") and k != "selected_ids"},
            ))
            await session.commit()
    except Exception:
        logger.debug("dream verify metrics failed (silent)", exc_info=True)


async def _judge_note(db: AsyncSession, user_id: str, name: str, description: str,
                      units: list[dict], timeout_seconds: float,
                      stats: dict | None = None) -> tuple[str, str, str, str | None]:
    from app.prompts.memory import MEMORY_DREAM_VERIFY_PROMPT
    from app.services.memory_llm_factory import _memory_llm
    from app.services.memory_subconscious_service import _extract_json_object

    items = build_user_excerpt_items(units)
    if not items:
        return "insufficient", "", "agent_only_evidence", None
    excerpts_text = "\n".join(f"[{i}] {it['text']}" for i, it in enumerate(items, 1))
    payload = build_verify_payload(name, description, items)
    llm = _memory_llm("dream_verify")
    resp = await asyncio.wait_for(
        llm.complete_chat(
            [
                {"role": "system", "content": MEMORY_DREAM_VERIFY_PROMPT},
                {"role": "user", "content": payload},
            ],
            temperature=0.0,
        ),
        timeout=max(1.0, float(timeout_seconds)),
    )
    parsed = _extract_json_object(resp)
    state, quote, reason = normalize_verdict(parsed, excerpts_text)
    correction = normalize_correction(parsed)
    await _bill_verify(db, user_id)
    if stats is not None:
        # B10（2026-11-08）：tokens 记账（CJK 粗估 1 字≈1 token，与 spike guard
        # 同一口径的保守上界；周报展示为估算值）。
        stats["est_llm_tokens"] = int(stats.get("est_llm_tokens") or 0) \
            + len(payload) + len(str(resp or ""))
    return state, quote, reason, correction


async def _run_verify_episodes(db: AsyncSession, user_id: str, *,
                               limit: int, interval_days: int,
                               deadline: float, stats: dict) -> None:
    from app.db.database import MemoryEpisode

    rows = await _load_episode_rows(db, user_id, limit, interval_days)
    stats["episodes_selected"] = len(rows)
    for row in rows:
        if time.monotonic() >= deadline:
            stats["episodes_skipped"] = stats.get("episodes_skipped", 0) + 1
            continue
        try:
            units = await _load_units(db, user_id, row["source_unit_ids"])
            source_ids = extract_message_ids(units)
            unit_ids = [str(u.get("id")) for u in units if u.get("id")]
            timeout = min(
                float(config.memory.get("dream_verify_timeout_seconds", 30)),
                max(1.0, deadline - time.monotonic()),
            )
            state, quote, reason, correction = await _judge_note(
                db, user_id, "情景记忆", str(row.get("narrative") or ""),
                units, timeout, stats=stats)
            episode = await db.get(MemoryEpisode, row["id"])
            if episode is None or episode.user_id != user_id:
                stats["episodes_skipped"] = stats.get("episodes_skipped", 0) + 1
                continue
            episode.verification_state = state
            episode.last_verified_at = datetime.utcnow()
            episode.verification_evidence = build_evidence(
                source_ids, quote, reason, unit_ids,
                source_kind=_source_kind(units, "message"),
                suggested_correction=correction)
            episode.verification_correction = correction
            stats[f"episodes_{state}"] = stats.get(f"episodes_{state}", 0) + 1
        except Exception:
            logger.warning("dream verify episode failed id=%s", row.get("id"), exc_info=True)
            stats["episodes_skipped"] = stats.get("episodes_skipped", 0) + 1


async def run_verify(db: AsyncSession, user_id: str, *, max_concepts: int | None = None) -> dict:
    if not bool(config.memory.get("dream_verify_enabled", True)):
        return {"skipped": "disabled"}
    limit = max(1, min(int(max_concepts or config.memory.get("dream_verify_max_concepts", 5)), 20))
    min_importance = float(config.memory.get("dream_verify_min_importance", 0.7))
    interval_days = int(config.memory.get("dream_verify_interval_days", 14))
    budget = float(config.memory.get("dream_verify_total_budget_seconds",
                                     DEFAULT_TOTAL_BUDGET_SECONDS))
    started = time.monotonic()
    deadline = started + max(10.0, budget)
    rows = await _load_concept_rows(db, user_id, limit, min_importance, interval_days)
    stats = {"selected": len(rows), "confirmed": 0, "contradicted": 0,
             "insufficient": 0, "skipped": 0, "quote_grounded_fail": 0,
             "est_llm_tokens": 0,
             "episodes_selected": 0, "episodes_confirmed": 0,
             "episodes_contradicted": 0, "episodes_insufficient": 0,
             "episodes_skipped": 0, "budget_exhausted": False,
             # P1-16：批内 candidate ids（脚本审计用；metrics 落库前剔除）
             "selected_ids": [str(r["id"]) for r in rows][:20],
             "_db": db, "_user_id": user_id}
    if not rows:
        stats.pop("_db", None)
        stats.pop("_user_id", None)
        return stats

    from app.db.database import MemoryConcept

    for row in rows:
        if time.monotonic() >= deadline:
            stats["budget_exhausted"] = True
            stats["skipped"] += 1
            continue
        try:
            units = await _load_units(db, user_id, row.get("source_unit_ids"))
            if not units:
                units = await _load_units_from_messages(
                    db, user_id, row.get("source_message_ids"))
            source_ids = extract_message_ids(units)
            unit_ids = [str(u.get("id")) for u in units if u.get("id")]
            if not units:
                state, quote, reason, correction = "insufficient", "", "no_source", None
            else:
                timeout = min(
                    float(config.memory.get("dream_verify_timeout_seconds", 30)),
                    max(1.0, deadline - time.monotonic()),
                )
                state, quote, reason, correction = await _judge_note(
                    db, user_id, row["name"],
                    row.get("short") or row.get("full") or "",
                    units, timeout, stats=stats)
            if reason == "quote_not_grounded":
                stats["quote_grounded_fail"] += 1
            concept = await db.get(MemoryConcept, row["id"])
            if not concept or concept.user_id != user_id:
                stats["skipped"] += 1
                continue
            # 2026-11-08：contradicted 不写 valid_to、不删概念；只标待核实。
            concept.verification_state = state
            concept.last_verified_at = datetime.utcnow()
            concept.verification_evidence = build_evidence(
                source_ids, quote, reason, unit_ids,
                source_kind=_source_kind(units, row.get("source_kind")),
                suggested_correction=correction)
            concept.verification_correction = correction
            if row.get("source_kind") is None and units:
                concept.source_kind = _source_kind(units)
            stats[state] += 1
        except Exception:
            logger.warning("dream verify failed for concept=%s", row.get("id"), exc_info=True)
            stats["skipped"] += 1

    try:
        await _run_verify_episodes(
            db, user_id,
            limit=int(config.memory.get("dream_verify_max_episodes", 2)),
            interval_days=interval_days, deadline=deadline, stats=stats)
    except Exception:
        logger.warning("dream verify episodes failed", exc_info=True)

    await db.flush()
    stats["latency_ms"] = int((time.monotonic() - started) * 1000)
    await record_verify_metrics(dict(stats))
    stats.pop("_db", None)
    stats.pop("_user_id", None)
    return stats
