# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import MemoryConcept
from app.services.memory_embedding_service import embed_text, _emb_to_pgvector

config = get_config()
logger = logging.getLogger(__name__)


def effective_weight_sql(weight_expr: str = "weight") -> str:
    """有效权重 SQL 片段：weight × Ebbinghaus 残差衰减。

    与 run_weight_decay 的 anchor 语义一致（A4.9 审查 C2）：
      anchor = max(last_recalled_at, weight_decayed_at, created_at)
    衰减只作用于"距上次写回/召回"的增量。run_weight_decay 写回 weight 时
    同步把 weight_decayed_at 前移到 NOW()——因此同一事务内（weight_decay →
    dream → active_dreaming）再次计算时 exp(-(now - weight_decayed_at)/s) ≈ 1，
    不会对已写回权重重复乘全量衰减（旧实现 w·exp(-2Δ/s) 二次衰减）。
    weight_expr 允许调用方传 ROUND 包装等表达式。
    """
    return (
        f"({weight_expr} * exp(-GREATEST("
        "EXTRACT(EPOCH FROM (NOW() - GREATEST("
        "COALESCE(last_recalled_at, created_at), COALESCE(weight_decayed_at, created_at)"
        ")))/86400.0, 0) / GREATEST(COALESCE(stability, 14), 1)))"
    )


def _get_trust_cap(source_trust: str) -> float:
    caps = {"user_stated": 1.0, "user_authored": 1.0,
            "agent_inferred": float(config.memory_concept.get("trust_cap_agent_inferred", 0.7)),
            "external": float(config.memory_concept.get("trust_cap_external", 0.5))}
    return caps.get(source_trust, 1.0)


async def apply_recall_boost(db: AsyncSession, concept_ids: list[str], user_id: str | None = None) -> None:
    if not concept_ids:
        return
    boost = float(config.memory_concept.get("weight_recall_boost", 0.03))
    await _bulk_update_concepts(db, concept_ids, boost, user_id)


async def _bulk_update_concepts(db: AsyncSession, concept_ids: list[str], boost: float, user_id: str | None = None) -> None:
    for cid in concept_ids:
        concept = await db.get(MemoryConcept, cid)
        if not concept:
            continue
        if user_id is not None and concept.user_id != user_id:
            continue
        # as-of 历史召回不修改已失效概念的权重/召回记录（历史只读）
        if concept.valid_to is not None:
            continue
        cap = _get_trust_cap(concept.source_trust)
        concept.weight = min(concept.weight + boost, cap)
        concept.last_recalled_at = datetime.utcnow()
        concept.hot_forget_count = 0

        attr = config.memory_fatigue
        if concept.memory_type == "semantic":
            growth = float(attr.get("semantic_stability_growth_days", 7))
            max_stab = float(attr.get("semantic_stability_max_days", 90))
        elif concept.memory_type == "episodic":
            growth = float(attr.get("episodic_stability_growth_days", 4))
            max_stab = float(attr.get("episodic_stability_max_days", 45))
        else:
            growth = float(attr.get("semantic_stability_growth_days", 7))
            max_stab = float(attr.get("semantic_stability_max_days", 90))

        concept.stability = min(concept.stability + growth, max_stab)
        concept.updated_at = datetime.utcnow()


async def apply_episode_recall_boost(db: AsyncSession, episode_ids: list[str], user_id: str | None = None) -> None:
    if not episode_ids:
        return
    await db.execute(
        text("UPDATE memory_episodes SET last_recalled_at = NOW() WHERE id = ANY(:ids) AND valid_to IS NULL"),
        {"ids": episode_ids},
    )
    # §5.3.1a-2 cross-boost：episode 命中时其 source_concept_ids 内概念顺带 weight += 0.02
    if user_id is None or not config.memory_episodic.get("cross_boost_concept_weight", True):
        return
    result = await db.execute(
        text("SELECT source_concept_ids FROM memory_episodes WHERE id = ANY(:ids) AND user_id = :uid"),
        {"ids": episode_ids, "uid": user_id},
    )
    import json as _json
    concept_ids: list[str] = []
    for row in result.fetchall():
        try:
            ids = _json.loads(row[0] or "[]")
            if isinstance(ids, list):
                concept_ids.extend(str(i) for i in ids)
        except (_json.JSONDecodeError, TypeError):
            continue
    for cid in set(concept_ids):
        concept = await db.get(MemoryConcept, cid)
        if not concept or concept.user_id != user_id:
            continue
        if concept.valid_to is not None:
            continue
        cap = _get_trust_cap(concept.source_trust)
        concept.weight = min((concept.weight or 0) + 0.02, cap)
        concept.updated_at = datetime.utcnow()


async def apply_subconscious_recall_boost(db: AsyncSession, unit_ids: list[str]) -> None:
    if not unit_ids:
        return
    await db.execute(
        text("UPDATE subconscious_log SET recurrence_count = recurrence_count + 1, last_recurrence_at = NOW() WHERE id = ANY(:ids)"),
        {"ids": unit_ids},
    )


async def apply_reinforcement_signal(db: AsyncSession, concept_id: str, signal_type: str) -> None:
    concept = await db.get(MemoryConcept, concept_id)
    if not concept or concept.valid_to is not None:
        return
    cap = _get_trust_cap(concept.source_trust)

    signal_map = {
        "recall_reference": 0.03,
        "multi_turn_recurrence": 0.05,
        "new_evidence": 0.03,
        "clarification_correction": -0.15,
        "clarification_constraint": -0.05,
        "dreaming_contradiction": -0.10,
        "dreaming_confirmation": 0.05,
    }
    delta = signal_map.get(signal_type, 0)
    if delta > 0:
        concept.weight = min(concept.weight + delta, cap)
    elif delta < 0:
        concept.weight = max(concept.weight + delta, 0)
    concept.updated_at = datetime.utcnow()


async def run_weight_decay(db: AsyncSession, user_id: str) -> dict:
    attr = config.memory_fatigue
    hot_threshold = float(config.memory_concept.get("hot_forget_threshold", 0.15))
    avg_reset = config.memory_concept.get("avg_weight_reset", True)
    now = datetime.utcnow()
    # A4.9 审查 Minor：配置一次性读取，移出 per-row 循环
    weight_writeback = bool(config.memory_concept.get("weight_decay_writeback", True))
    floor_w = float(config.memory_concept.get("weight_decay_floor", 0.05))

    result = await db.execute(
        text("SELECT id, weight, stability, last_recalled_at, memory_type, hot_forget_count, source_trust, status, created_at, importance, weight_decayed_at FROM memory_concepts WHERE user_id = :uid AND status IN ('active','silent') AND valid_to IS NULL"),
        {"uid": user_id},
    )
    rows = result.fetchall()

    changes = {"cold_forgotten": 0, "hot_count_inc": 0, "silent_dropped": 0}
    active_weights = [r[1] or 0 for r in rows if r[7] == "active"]
    avg_weight = sum(active_weights) / max(len(active_weights), 1)

    epi_hot = int(attr.get("cold_forget_threshold_episodic", 2))
    sem_hot = int(attr.get("cold_forget_threshold_semantic", 8))
    skip_procedural = attr.get("procedural_skip_decay", True)
    maturation_window = int(config.memory_concept.get("maturation_window_days", 21))
    silent_max = int(config.memory_concept.get("silent_max_windows", 2))
    cold_start_exempt_days = maturation_window

    import math

    for row in rows:
        cid, weight, stability, last_recalled, mem_type, hot_count, trust, status, created_at, importance, weight_decayed_at = row

        if mem_type == "procedural" and skip_procedural:
            continue

        # silent 超期降级：以 created_at 计时（§5.3.5，超 silent_max_windows 个窗口未升 active → 降级）
        if status == "silent":
            base = created_at or now
            if (now - base).days > maturation_window * silent_max:
                await db.execute(
                    text("UPDATE memory_concepts SET status = 'cold_forgotten', updated_at = NOW() WHERE id = :id"),
                    {"id": cid},
                )
                changes["silent_dropped"] += 1
            continue

        # 2026-08-10 importance 豁免：高重要性用户偏好（user_stated/user_authored）免衰减
        # ——一次陈述终身生效的记忆（如助手昵称）不得因长期不被召回而被遗忘。
        # 放在 silent 降级之后：silent 概念仍走成熟/降级流程，只有 active 偏好才豁免。
        if (importance or 0.5) >= 0.8 and trust in ("user_stated", "user_authored"):
            continue

        # 冷启动豁免（§5.3.3）：创建后 21d 内不进入 hot_forget 计数
        if created_at and (now - created_at).days < cold_start_exempt_days:
            continue

        # 2026-08-16 修复（FadeMem/Ebbinghaus 衰减写回）：旧实现只计算 effective
        # 用于 hot_forget 计数，weight 列从不衰减 → weight=1.0 概念在无召回下
        # 仍占据 top10（dream "持续占据高权重" 根因之一）。现在把衰减后的
        # 权重写回 weight 列（下限 weight_decay_floor），未召回概念的权重
        # 真正逐日下降，召回 boost 才能重新拉起——weight 成为可访问性而非
        # 永久分数。
        # A4.9 审查 C1 修复：衰减 anchor 取 max(last_recalled_at, weight_decayed_at)
        # 并在写回时把 anchor 前移到 NOW()——避免旧实现"从 last_recalled 起全量
        # 衰减每轮重复相乘"造成的二次衰减复合（w_k = w0·Πexp(-Δ_i/s)，Δ_i 不递减，
        # 冷遗忘加速 2.8×-6.6×）。现在每轮只衰减"距上次写回/召回"的增量（线性）。
        anchor = last_recalled or created_at or now
        if weight_decayed_at is not None and weight_decayed_at > anchor:
            anchor = weight_decayed_at
        delta_days = max((now - anchor).days, 0)
        strength = math.exp(-delta_days / max(stability or 14, 1))
        effective = (weight or 0) * strength

        if weight_writeback:
            new_weight = max(effective, floor_w)
            if abs(new_weight - (weight or 0)) > 1e-9:
                await db.execute(
                    text("UPDATE memory_concepts SET weight = :w, weight_decayed_at = NOW() WHERE id = :id"),
                    {"w": new_weight, "id": cid},
                )
            effective = new_weight

        hot_thresh = epi_hot if mem_type == "episodic" else sem_hot

        if effective < hot_threshold:
            await db.execute(
                text("UPDATE memory_concepts SET hot_forget_count = hot_forget_count + 1 WHERE id = :id"),
                {"id": cid},
            )
            changes["hot_count_inc"] += 1
            new_hot = (hot_count or 0) + 1
            if new_hot >= hot_thresh:
                await db.execute(
                    text("UPDATE memory_concepts SET status = 'cold_forgotten', updated_at = NOW() WHERE id = :id"),
                    {"id": cid},
                )
                changes["cold_forgotten"] += 1
            elif not weight_writeback and avg_reset and avg_weight > 0:
                await db.execute(
                    text("UPDATE memory_concepts SET weight = :w WHERE id = :id"),
                    {"w": avg_weight, "id": cid},
                )
            elif not weight_writeback and not avg_reset:
                await db.execute(
                    text("UPDATE memory_concepts SET weight = :w WHERE id = :id"),
                    {"w": hot_threshold, "id": cid},
                )

    return changes


async def try_cold_resurrect(db: AsyncSession, user_message: str, user_id: str) -> list[str]:
    msg = (user_message or "").lower()
    if not msg:
        return []
    # 候选扫描有界（默认 500，config [memory.concept] cold_resurrect_scan_limit 可调）
    scan_limit = int(config.memory_concept.get("cold_resurrect_scan_limit", 500))
    result = await db.execute(
        text("SELECT id, canonical_name, aliases, status FROM memory_concepts "
             "WHERE user_id = :uid AND status IN ('cold_forgotten','silent') "
             "ORDER BY updated_at DESC LIMIT :lim"),
        {"uid": user_id, "lim": scan_limit},
    )
    resurrected = []
    for row in result.fetchall():
        cid, name, aliases_raw, status = row
        needles = [name]
        if aliases_raw:
            try:
                import json
                aliases = json.loads(aliases_raw)
                if isinstance(aliases, list):
                    needles.extend(str(a) for a in aliases)
            except Exception:
                pass
        hit = any(n and n.lower() in msg for n in needles)
        if hit:
            if status == "cold_forgotten":
                weight = float(config.memory_concept.get("cold_resurrect_weight", 0.3))
                await db.execute(
                    text("UPDATE memory_concepts SET status = 'active', activation_strength = 1.0, weight = :w, hot_forget_count = 0, updated_at = NOW() WHERE id = :id"),
                    {"w": weight, "id": cid},
                )
            else:
                await db.execute(
                    text("UPDATE memory_concepts SET status = 'active', activation_strength = 1.0, updated_at = NOW() WHERE id = :id"),
                    {"id": cid},
                )
            resurrected.append(cid)
            # §5.3.4：embedding 缺失/过期（NULL 或从未生成）时复活即重生成，
            # 否则复活后 Stage 3 embedding 检索仍不可达
            await _regenerate_embedding_if_stale(db, cid)
    return resurrected


async def _regenerate_embedding_if_stale(db: AsyncSession, concept_id: str) -> None:
    r = await db.execute(
        text("SELECT canonical_name, aliases, description_short FROM memory_concepts "
             "WHERE id = :id AND (embedding IS NULL OR embedding_updated_at IS NULL)"),
        {"id": concept_id},
    )
    row = r.fetchone()
    if not row:
        return
    try:
        aliases = []
        if row[1]:
            import json
            parsed = json.loads(row[1])
            if isinstance(parsed, list):
                aliases = [str(a) for a in parsed]
        text_in = f"{row[0]} {' '.join(aliases)} {row[2] or ''}".strip()
        vec = await embed_text(text_in)
        if vec:
            # savepoint 隔离：regen UPDATE 失败（如维度不匹配）只回滚 regen，
            # 不毒化调用方（召回路径）事务中已完成的 resurrect 写入
            async with db.begin_nested():
                await db.execute(
                    text("UPDATE memory_concepts SET embedding = CAST(:emb AS vector), "
                         "embedding_updated_at = NOW() WHERE id = :id"),
                    {"emb": _emb_to_pgvector(vec), "id": concept_id},
                )
    except Exception:
        # §9.4：embedding 生成失败不阻塞复活，consolidation 批量补生成兜底
        logger.debug("resurrect embedding regen failed for %s", concept_id, exc_info=True)
