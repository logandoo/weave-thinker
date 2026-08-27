# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.services.memory_embedding_service import _emb_from_db, embed_text, _emb_to_pgvector

config = get_config()
logger = logging.getLogger(__name__)


def _sanitize_dream_date(candidate, today: str) -> str:
    """dream 的 generated_for_date 校验：仅接受今日 ±3 天内的 %Y-%m-%d，否则回退今天。

    2026-08-10 线上数据发现 LLM 返回 "2023-10-27" 被直接落库；consolidation 每天
    午夜为当天生成 dream，合法日期只可能是今天或昨天。
    """
    if not candidate or not isinstance(candidate, str):
        return today
    candidate = candidate.strip()
    try:
        dt = datetime.strptime(candidate, "%Y-%m-%d")
        if abs((datetime.strptime(today, "%Y-%m-%d") - dt).days) <= 3:
            return candidate
    except (ValueError, TypeError):
        pass
    return today


async def run_consolidation(db: AsyncSession, user_id: str) -> dict:
    state = await _get_agent_state(db, user_id)
    if not state:
        return {"status": "no_state"}

    if not await _should_consolidate(db, user_id, state):
        return {"status": "skipped"}

    result = {
        "dedup_merged": 0,
        "episodes_merged": 0,
        "clusters_rebalanced": 0,
        "relations_created": 0,
        "cold_forgotten": 0,
        "dream_generated": False,
    }
    _initial = dict(result)

    had_failure = False
    for step_name, step_fn in (
        ("dedup", _dedup_concepts_and_episodes),
        ("rebalance", _rebalance_clusters),
        ("relations", _update_relations),
        ("weight_decay", _run_weight_decay),
        ("cluster_weights", _update_cluster_weights),
        ("dream", _generate_dream),
    ):
        try:
            await step_fn(db, user_id, result)
        except Exception:
            logger.exception("consolidation %s failed for user=%s", step_name, user_id)
            await db.rollback()
            # rollback 撤销整个未提交事务（含此前步骤的写入）——
            # result 必须与已提交工作一致，全部计数归零重计
            result.update(_initial)
            had_failure = True
    if not had_failure:
        # 任一步骤失败即不在部分回滚状态上跑主动 Dreaming（§9.3 隔离原则）
        await _try_active_dreaming(db, user_id)

    await db.execute(
        text("UPDATE user_agent_states SET last_consolidation_at = NOW() WHERE user_id = :uid"),
        {"uid": user_id},
    )
    await db.commit()
    return result


async def _get_agent_state(db: AsyncSession, user_id: str) -> dict | None:
    result = await db.execute(
        text("SELECT last_consolidation_at, total_concept_count FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    row = result.fetchone()
    return {"last_consolidation_at": row[0], "total_concept_count": row[1]} if row else None


async def _should_consolidate(db: AsyncSession, user_id: str, state: dict) -> bool:
    """§5.5 事件驱动 + 时间兜底：概念变更数 ≥ threshold 或距上次 ≥ 48h。"""
    threshold = int(config.memory.get("consolidation_trigger_threshold", 20))
    max_hours = int(config.memory.get("consolidation_max_interval_hours", 48))
    last = state["last_consolidation_at"]
    if last is None:
        return True

    # “新增/修改”按创建或新证据（last_recurrence_at）计——召回 boost 会 bump updated_at，
    # 用 updated_at 会被召回路径污染导致恒触发（A4.9 审查 #8）
    result = await db.execute(
        text("""SELECT COUNT(*) FROM memory_concepts
                WHERE user_id = :uid AND (created_at > :since OR last_recurrence_at > :since)"""),
        {"uid": user_id, "since": last},
    )
    changed = result.scalar() or 0
    if changed >= threshold:
        return True

    elapsed = (datetime.utcnow() - last).total_seconds() / 3600
    return elapsed >= max_hours


async def _dedup_concepts_and_episodes(db: AsyncSession, user_id: str, result: dict) -> None:
    """§5.5 Step 1：ANN 预筛（pgvector LATERAL）+ LLM 合并判断，概念与 episodic 合批。"""
    dedup_thresh = float(config.memory.get("consolidation_dedup_threshold", 0.08))

    # 概念对（ANN 预筛，附录 B；dist < dedup_thresh 即 sim > 0.92）
    pairs_r = await db.execute(
        text("""
            SELECT a.id AS a_id, a.canonical_name AS a_name, a.description_short AS a_short,
                   a.source_trust AS a_trust, a.weight AS a_weight,
                   b.id AS b_id, b.canonical_name AS b_name, b.description_short AS b_short,
                   b.source_trust AS b_trust, b.weight AS b_weight
            FROM memory_concepts a
            CROSS JOIN LATERAL (
                SELECT id, canonical_name, description_short, source_trust, weight, embedding
                FROM memory_concepts
                WHERE user_id = a.user_id
                  AND status IN ('active','silent')
                  AND valid_to IS NULL
                  AND id != a.id
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> a.embedding
                LIMIT 5
            ) b
            WHERE a.user_id = :uid
              AND a.status IN ('active','silent')
              AND a.valid_to IS NULL
              AND a.embedding IS NOT NULL
              AND (a.embedding <=> b.embedding) < :thresh
              AND a.id < b.id
        """),
        {"uid": user_id, "thresh": dedup_thresh},
    )
    concept_pairs = pairs_r.fetchall()

    # episodic 对（同机制，§5.5 Step 1.5）
    epi_pairs_r = await db.execute(
        text("""
            SELECT a.id AS a_id, a.narrative AS a_narr, a.valid_from AS a_vf,
                   b.id AS b_id, b.narrative AS b_narr, b.valid_from AS b_vf
            FROM memory_episodes a
            CROSS JOIN LATERAL (
                SELECT id, narrative, valid_from, embedding
                FROM memory_episodes
                WHERE user_id = a.user_id
                  AND valid_to IS NULL
                  AND id != a.id
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> a.embedding
                LIMIT 5
            ) b
            WHERE a.user_id = :uid
              AND a.valid_to IS NULL
              AND a.embedding IS NOT NULL
              AND (a.embedding <=> b.embedding) < :thresh
              AND a.id < b.id
        """),
        {"uid": user_id, "thresh": dedup_thresh},
    )
    epi_pairs = epi_pairs_r.fetchall()

    if not concept_pairs and not epi_pairs:
        return

    from app.services.memory_llm_factory import _memory_llm
    llm = _memory_llm("consolidation")

    # 概念合并判断（批量，每批 ≤10 对一次调用）
    for batch_start in range(0, len(concept_pairs), 10):
        batch = concept_pairs[batch_start:batch_start + 10]
        pairs_text = "\n".join(
            f"对{i+1}: A={r[1]}({r[2] or ''}) | B={r[6]}({r[7] or ''})"
            for i, r in enumerate(batch)
        )
        try:
            resp = await llm.complete_chat(
                [
                    {"role": "system", "content": (
                        "你是记忆合并助手。逐对判断两个概念是否应该合并。"
                        "输出JSON: {\"merges\": [{\"pair\": 1, \"should_merge\": true, "
                        "\"merged_name\": \"...\", \"merged_description_short\": \"≤80字\", "
                        "\"merged_description_full\": \"...\"}]}"
                    )},
                    {"role": "user", "content": pairs_text},
                ],
                temperature=0.1,
            )
            resp = (resp or "").strip()
            if resp.lstrip().startswith("```"):
                resp = "\n".join(l for l in resp.split("\n") if not l.lstrip().startswith("```"))
            parsed = json.loads(resp)
            merges = {m.get("pair"): m for m in parsed.get("merges", []) if isinstance(m, dict)}
        except Exception:
            logger.warning("concept dedup LLM batch failed", exc_info=True)
            continue

        from app.services.memory_concept_service import merge_concepts
        for i, r in enumerate(batch):
            m = merges.get(i + 1)
            if not m or not m.get("should_merge"):
                continue
            a_trust, a_w, b_trust, b_w = r[3], r[4], r[8], r[9]
            keep_a = a_trust in ("user_stated", "user_authored") or (a_w or 0) >= (b_w or 0)
            kept_id, merged_id = (r[0], r[5]) if keep_a else (r[5], r[0])
            try:
                if await merge_concepts(db, kept_id, merged_id):
                    result["dedup_merged"] += 1
            except Exception:
                logger.debug("merge_concepts failed %s<-%s", kept_id, merged_id, exc_info=True)

    # episodic 合并判断（同批机制）
    for batch_start in range(0, len(epi_pairs), 10):
        batch = epi_pairs[batch_start:batch_start + 10]
        pairs_text = "\n".join(
            f"对{i+1}: A={r[1][:200]} | B={r[4][:200]}"
            for i, r in enumerate(batch)
        )
        try:
            resp = await llm.complete_chat(
                [
                    {"role": "system", "content": (
                        "判断每对事件叙事是否描述同一事件（应合并）。"
                        "输出JSON: {\"merges\": [{\"pair\": 1, \"should_merge\": true, "
                        "\"merged_narrative\": \"融合后的事件叙事（≤500字）\"}]}"
                    )},
                    {"role": "user", "content": pairs_text},
                ],
                temperature=0.1,
            )
            resp = (resp or "").strip()
            if resp.lstrip().startswith("```"):
                resp = "\n".join(l for l in resp.split("\n") if not l.lstrip().startswith("```"))
            parsed = json.loads(resp)
            merges = {m.get("pair"): m for m in parsed.get("merges", []) if isinstance(m, dict)}
        except Exception:
            logger.warning("episode dedup LLM batch failed", exc_info=True)
            continue

        for i, r in enumerate(batch):
            m = merges.get(i + 1)
            if not m or not m.get("should_merge"):
                continue
            a_id, a_vf, b_id, b_vf = r[0], r[2], r[3], r[5]
            # 保留 valid_from 较早者为幸存方
            keep_a = (a_vf or datetime.max) <= (b_vf or datetime.max)
            kept_id, merged_id = (a_id, b_id) if keep_a else (b_id, a_id)
            merged_narr = (m.get("merged_narrative") or "")[:5000]
            try:
                if merged_narr:
                    emb = await embed_text(merged_narr)
                    await db.execute(
                        text("""UPDATE memory_episodes SET narrative = :n, embedding = CAST(:emb AS vector),
                                source_unit_ids = (
                                    SELECT jsonb_agg(DISTINCT x) FROM (
                                        SELECT jsonb_array_elements_text(source_unit_ids::jsonb) AS x
                                        FROM memory_episodes WHERE id IN (:kept, :merged)
                                    ) t
                                )::text,
                                updated_at = NOW() WHERE id = :kept"""),
                        {"n": merged_narr, "emb": _emb_to_pgvector(emb) if emb else None,
                         "kept": kept_id, "merged": merged_id},
                    )
                await db.execute(
                    text("UPDATE memory_episodes SET valid_to = NOW(), superseded_by = :kept, updated_at = NOW() WHERE id = :merged"),
                    {"kept": kept_id, "merged": merged_id},
                )
                await db.execute(
                    text("UPDATE memory_episodes SET merged_from = :merged WHERE id = :kept"),
                    {"merged": merged_id, "kept": kept_id},
                )
                result["episodes_merged"] += 1
            except Exception:
                logger.debug("episode merge failed %s<-%s", kept_id, merged_id, exc_info=True)


async def _rebalance_clusters(db: AsyncSession, user_id: str, result: dict) -> None:
    await db.execute(
        text("DELETE FROM memory_clusters WHERE user_id = :uid AND member_count = 0"),
        {"uid": user_id},
    )

    # 待复核概念（needs_review，§5.5 Step 2）：无来源证据则置 valid_to 失效，有证据则清除标记
    review_r = await db.execute(
        text("SELECT id, source_raw_ids, source_unit_ids FROM memory_concepts WHERE user_id = :uid AND needs_review = TRUE AND valid_to IS NULL"),
        {"uid": user_id},
    )
    from app.services.memory_concept_service import _parse_json_array
    for row in review_r.fetchall():
        cid, raw_ids_raw, unit_ids_raw = row[0], row[1], row[2]
        raw_ids = _parse_json_array(raw_ids_raw)
        unit_ids = _parse_json_array(unit_ids_raw)
        unit_alive = 0
        if unit_ids:
            r = await db.execute(
                text("SELECT COUNT(*) FROM subconscious_log WHERE id = ANY(:ids)"),
                {"ids": unit_ids},
            )
            unit_alive = r.scalar() or 0
        raw_alive = 0
        for rid in raw_ids:
            if isinstance(rid, str) and rid.startswith("legacy:"):
                raw_alive += 1
        if unit_alive == 0 and raw_alive == 0:
            await db.execute(
                text("UPDATE memory_concepts SET valid_to = NOW(), updated_at = NOW() WHERE id = :id"),
                {"id": cid},
            )
        else:
            await db.execute(
                text("UPDATE memory_concepts SET needs_review = FALSE, updated_at = NOW() WHERE id = :id"),
                {"id": cid},
            )

    clusters = await db.execute(
        text("SELECT id, name, member_count FROM memory_clusters WHERE user_id = :uid ORDER BY member_count DESC"),
        {"uid": user_id},
    )
    for row in clusters.fetchall():
        cid, name, count = row[0], row[1], row[2]
        if count < 3 and count > 0:
            nearest = await _find_nearest_cluster(db, user_id, cid)
            if nearest:
                await db.execute(
                    text("UPDATE concept_cluster_members SET cluster_id = :nc WHERE cluster_id = :oc"),
                    {"nc": nearest, "oc": cid},
                )
                await db.execute(
                    text("UPDATE memory_clusters SET member_count = (SELECT COUNT(*) FROM concept_cluster_members WHERE cluster_id = :nc) WHERE id = :nc"),
                    {"nc": nearest},
                )
                await db.execute(text("DELETE FROM memory_clusters WHERE id = :id"), {"id": cid})
                result["clusters_rebalanced"] += 1


async def _find_nearest_cluster(db: AsyncSession, user_id: str, cluster_id: str) -> str | None:
    r = await db.execute(
        text("SELECT id FROM memory_clusters WHERE user_id = :uid AND id != :cid ORDER BY embedding <=> (SELECT embedding FROM memory_clusters WHERE id = :cid) LIMIT 1"),
        {"uid": user_id, "cid": cluster_id},
    )
    row = r.fetchone()
    return row[0] if row else None


async def _update_relations(db: AsyncSession, user_id: str, result: dict) -> None:
    """§5.5 Step 3：今日新增概念与同集合已有概念批量 LLM 提取关系；清理失效关系。"""
    await db.execute(
        text("DELETE FROM concept_relations WHERE user_id = :uid AND (source_id IN (SELECT id FROM memory_concepts WHERE user_id = :uid AND status = 'cold_forgotten') OR target_id IN (SELECT id FROM memory_concepts WHERE user_id = :uid AND status = 'cold_forgotten'))"),
        {"uid": user_id},
    )

    since = datetime.utcnow() - timedelta(days=1)
    new_r = await db.execute(
        text("""SELECT id, canonical_name, description_short FROM memory_concepts
                WHERE user_id = :uid AND created_at >= :since AND valid_to IS NULL LIMIT 30"""),
        {"uid": user_id, "since": since},
    )
    new_concepts = [{"id": r[0], "name": r[1], "short": r[2]} for r in new_r.fetchall()]
    if not new_concepts:
        return

    existing_r = await db.execute(
        text("""SELECT id, canonical_name, description_short FROM memory_concepts
                WHERE user_id = :uid AND valid_to IS NULL AND status IN ('active','silent')
                ORDER BY weight DESC LIMIT 20"""),
        {"uid": user_id},
    )
    existing = [{"id": r[0], "name": r[1], "short": r[2]} for r in existing_r.fetchall()]
    if not existing:
        return

    new_text = "\n".join(f"- {c['name']} (id={c['id']}): {c['short'] or ''}" for c in new_concepts)
    existing_text = "\n".join(f"- {c['name']} (id={c['id']}): {c['short'] or ''}" for c in existing)

    from app.services.memory_llm_factory import _memory_llm
    try:
        llm = _memory_llm("consolidation")
        resp = await llm.complete_chat(
            [
                {"role": "system", "content": (
                    "检测新增概念与已有概念之间的关系。输出 JSON array（空数组合法）："
                    "[{\"source\": \"id\", \"target\": \"id\", "
                    "\"type\": \"causal|temporal|contradicts|supports|part_of\", "
                    "\"description\": \"...\"}]"
                )},
                {"role": "user", "content": f"新增概念：\n{new_text}\n\n已有概念：\n{existing_text}"},
            ],
            temperature=0.1,
        )
        resp = (resp or "").strip()
        if resp.lstrip().startswith("```"):
            resp = "\n".join(l for l in resp.split("\n") if not l.lstrip().startswith("```"))
        relations = json.loads(resp)
        if not isinstance(relations, list):
            return
    except Exception:
        logger.warning("relation extraction LLM failed", exc_info=True)
        return

    valid_ids = {c["id"] for c in new_concepts} | {c["id"] for c in existing}
    from app.services.memory_cluster_service import create_relation
    for rel in relations[:20]:
        if not isinstance(rel, dict):
            continue
        sid, tid = rel.get("source"), rel.get("target")
        rtype = rel.get("type", "")
        if sid not in valid_ids or tid not in valid_ids or sid == tid:
            continue
        if rtype not in ("causal", "temporal", "contradicts", "supports", "part_of"):
            continue
        dup = await db.execute(
            text("SELECT 1 FROM concept_relations WHERE source_id = :s AND target_id = :t AND relation_type = :rt LIMIT 1"),
            {"s": sid, "t": tid, "rt": rtype},
        )
        if dup.fetchone():
            continue
        try:
            await create_relation(db, user_id, sid, tid, rtype, rel.get("description", "")[:500])
            result["relations_created"] += 1
            if rtype == "contradicts":
                # contradicts 关系同步触发矛盾降权（§5.3.1a）
                from app.services.memory_weight_service import apply_reinforcement_signal
                await apply_reinforcement_signal(db, sid, "dreaming_contradiction")
                await apply_reinforcement_signal(db, tid, "dreaming_contradiction")
        except Exception:
            logger.debug("create_relation failed", exc_info=True)


async def _run_weight_decay(db: AsyncSession, user_id: str, result: dict) -> None:
    from app.services.memory_weight_service import run_weight_decay
    changes = await run_weight_decay(db, user_id)
    result["cold_forgotten"] = changes.get("cold_forgotten", 0)


async def _update_cluster_weights(db: AsyncSession, user_id: str, result: dict | None = None) -> None:
    """§5.5 Step 4：cluster weight = avg(成员 weight)。"""
    await db.execute(
        text("""UPDATE memory_clusters mc SET weight = COALESCE((
                    SELECT AVG(c.weight) FROM memory_concepts c
                    JOIN concept_cluster_members ccm ON c.id = ccm.concept_id
                    WHERE ccm.cluster_id = mc.id AND c.valid_to IS NULL
                ), mc.weight), updated_at = NOW()
                WHERE mc.user_id = :uid"""),
        {"uid": user_id},
    )


async def _generate_dream(db: AsyncSession, user_id: str, result: dict) -> None:
    """§5.5 Step 5：结构化 dream（今日新增/合并/澄清/权重分布 → JSON + narrative）。"""
    from app.services.memory_llm_factory import _memory_llm
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(config.memory_timezone)).strftime("%Y-%m-%d")
    day_ago = datetime.utcnow() - timedelta(days=1)

    new_r = await db.execute(
        text("SELECT canonical_name FROM memory_concepts WHERE user_id = :uid AND created_at >= :since AND valid_to IS NULL LIMIT 30"),
        {"uid": user_id, "since": day_ago},
    )
    new_concepts = [r[0] for r in new_r.fetchall()]

    clar_r = await db.execute(
        text("SELECT original_text, correction_type FROM memory_clarifications WHERE user_id = :uid AND created_at >= :since LIMIT 10"),
        {"uid": user_id, "since": day_ago},
    )
    clarifications = [{"text": r[0][:100], "type": r[1]} for r in clar_r.fetchall()]

    # 2026-08-16 修复：top/bottom 按"有效权重"（weight × Ebbinghaus 残差衰减，
    # 与 run_weight_decay 同 anchor：max(last_recalled_at, weight_decayed_at)）
    # 排序——旧实现按原始 weight 恒序，未召回概念因 weight 不再衰减而永久
    # 霸榜（dream "持续占据高权重" 的直接来源）。payload 中 weight 展示有效值
    # （float，避免 Decimal 使 json.dumps 崩溃——2026-08-16 收尾实证），
    # LLM 叙述即基于真实热度。
    from app.services.memory_weight_service import effective_weight_sql
    eff_expr = effective_weight_sql()
    # 2026-08-25 修复：top/bottom10 原先全历史无时间窗，prompt 却框定"今日记忆"，
    # 导致 LLM 把数月前迁移导入、早已不再出现的旧项目概念叙述成"当前边缘主题"
    # （生产环境实证：低频主题导入后未被召回）。
    # 拆成三组语义明确的名单：近窗高频（当前焦点）/ 近窗低频 / 待遗忘候选
    # （超过窗口未再出现，提示 LLM 只可一句带过）。并加 last_recalled_at
    # 平局次级排序（权重齐 1.0 时旧纯 ORDER BY eff 顺序未定义）。
    window_days = int(config.memory.get("dream_concept_window_days", 14))
    window_since = datetime.utcnow() - timedelta(days=window_days)
    base_filter = ("status = 'active' AND activation_strength > 0.05 "
                   "AND valid_to IS NULL AND memory_type != 'profile'")
    recent_filter = f"({base_filter} AND (last_recalled_at >= :since OR created_at >= :since))"

    # A4.9 wave2/3 审查 M4：与 overview 同构补 profile 排除（profile 概念经
    # 有效权重排序后自然下沉，但不应进入 top/bottom 名单误导 LLM 叙述）
    top_r = await db.execute(
        text(f"SELECT canonical_name, ROUND(CAST({eff_expr} AS numeric), 2) "
             f"FROM memory_concepts WHERE user_id = :uid AND {recent_filter} "
             "ORDER BY " + eff_expr + " DESC, last_recalled_at DESC NULLS LAST LIMIT 10"),
        {"uid": user_id, "since": window_since},
    )
    top_concepts = [{"name": r[0], "weight": float(r[1] or 0)} for r in top_r.fetchall()]

    bottom_r = await db.execute(
        text(f"SELECT canonical_name, ROUND(CAST({eff_expr} AS numeric), 2) "
             f"FROM memory_concepts WHERE user_id = :uid AND {recent_filter} "
             "ORDER BY " + eff_expr + " ASC, last_recalled_at DESC NULLS LAST LIMIT 10"),
        {"uid": user_id, "since": window_since},
    )
    bottom_concepts = [{"name": r[0], "weight": float(r[1] or 0)} for r in bottom_r.fetchall()]

    forget_r = await db.execute(
        text(f"SELECT canonical_name, ROUND(CAST({eff_expr} AS numeric), 2) "
             f"FROM memory_concepts WHERE user_id = :uid AND {base_filter} "
             "AND COALESCE(last_recalled_at, created_at) < :since "
             "ORDER BY " + eff_expr + " ASC, COALESCE(last_recalled_at, created_at) ASC LIMIT 10"),
        {"uid": user_id, "since": window_since},
    )
    forget_candidates = [{"name": r[0], "weight": float(r[1] or 0)} for r in forget_r.fetchall()]

    cluster_r = await db.execute(
        text("SELECT name, member_count FROM memory_clusters WHERE user_id = :uid ORDER BY member_count DESC LIMIT 10"),
        {"uid": user_id},
    )
    clusters = [{"name": r[0], "members": r[1]} for r in cluster_r.fetchall()]

    cc_r = await db.execute(
        text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND status = 'active' AND valid_to IS NULL"),
        {"uid": user_id},
    )
    concept_count = cc_r.scalar() or 0
    cluster_count = len(clusters)

    input_payload = {
        "今日新增概念": new_concepts,
        "今日合并概念对数": result["dedup_merged"],
        "今日澄清": clarifications,
        f"近{window_days}天高频概念_当前焦点": top_concepts,
        f"近{window_days}天低频概念": bottom_concepts,
        f"待遗忘候选_近{window_days}天未再出现": forget_candidates,
        "概念集合": clusters,
        "冷遗忘数": result["cold_forgotten"],
    }

    fallback_meta = {
        "date": today,
        "new_concepts": new_concepts,
        "merged_concepts": result["dedup_merged"],
        "clarifications_applied": len(clarifications),
        "patterns_observed": "",
        "attention_needed": [],
        "narrative": f"今日记忆整理：活跃概念 {concept_count} 个，集合 {cluster_count} 个。",
    }
    narrative = fallback_meta["narrative"]
    metadata = fallback_meta

    try:
        llm = _memory_llm("dream")
        resp = await llm.complete_chat(
            [
                {"role": "system", "content": (
                    "生成今日记忆整理 dream。输出 JSON：{\"date\": \"...\", \"new_concepts\": [...], "
                    "\"merged_concepts\": [{\"kept\": \"...\", \"merged\": \"...\"}], "
                    "\"clarifications_applied\": 0, \"patterns_observed\": \"...\", "
                    "\"attention_needed\": [...], \"narrative\": \"3-5句中文摘要\"}。"
                    f"语义约束：{window_days}天高频概念是当前工作焦点，narrative 以此为中心；"
                    f"{window_days}天低频概念是近期内出现过但热度较低的主题；"
                    f"待遗忘候选是超过 {window_days} 天未再出现的旧主题——narrative 至多用一句话说明它们正在渐淡，"
                    "绝不能把它们描述为近期活动、当前边缘工作或本周主题。"
                    f"若近 {window_days} 天高频/低频名单为空（近期记忆活动少），narrative 如实说明平静状态，"
                    "不得虚构焦点，更不得拿待遗忘候选充当焦点。"
                )},
                {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)},
            ],
            temperature=float(config.memory.get("dream_temperature", 0.2)),
            max_tokens=config.memory.get("dream_max_tokens") or None,
        )
        try:
            from app.services.memory_cost_governance_service import record_llm_call
            await record_llm_call(db, user_id, "dream")
        except Exception:
            logger.debug("record_llm_call failed", exc_info=True)
        resp = (resp or "").strip()
        # 2026-08-16 收尾修复：复用 subconscious 的 _extract_json_object（三段式：
        # 直解析 → lstrip 剥围栏 → {} 区间提取），兼容缩进围栏与散文包裹 JSON
        # （A4.9 wave2/3 审查 I3——旧实现只剥 ``` 行且无 {} 提取，LLM 输出
        # 缩进围栏/散文包裹时 json.loads 失败 → 整段 dream 落入 fallback 模板）。
        from app.services.memory_subconscious_service import _extract_json_object
        parsed = _extract_json_object(resp)
        if isinstance(parsed, dict):
            # 2026-08-10：LLM 曾返回幻觉日期 "2023-10-27" 并被直接落库 -> 强制校验回退
            metadata = {**fallback_meta, **parsed, "date": _sanitize_dream_date(parsed.get("date"), today)}
            narrative = parsed.get("narrative") or narrative
    except Exception:
        logger.warning("dream LLM failed, using fallback", exc_info=True)

    state_r = await db.execute(
        text("SELECT id FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    state_row = state_r.fetchone()
    if not state_row:
        return

    # 同日 dream 去重（agent_dreams 无唯一约束，重复 consolidation 不重复建行）
    dup_r = await db.execute(
        text("SELECT 1 FROM agent_dreams WHERE agent_state_id = :sid AND generated_for_date = :dt AND dream_type = 'consolidation' LIMIT 1"),
        {"sid": state_row[0], "dt": today},
    )
    if dup_r.fetchone():
        result["dream_generated"] = False
        return

    dream_id = str(uuid.uuid4())
    await db.execute(
        text("INSERT INTO agent_dreams (id, agent_state_id, generated_for_date, summary, source_note_count, source_message_count, source_concept_count, source_cluster_count, metadata_json, created_at, dream_type) VALUES (:id, :sid, :dt, :sum, 0, 0, :cc, :clc, :meta, :created, 'consolidation')"),
        {"id": dream_id, "sid": state_row[0], "dt": today, "sum": (narrative or "")[:4000],
         "cc": concept_count, "clc": cluster_count, "created": datetime.utcnow(),
         "meta": json.dumps(metadata, ensure_ascii=False)},
    )
    await db.execute(
        text("UPDATE user_agent_states SET latest_dream_id = :did WHERE id = :sid"),
        {"did": dream_id, "sid": state_row[0]},
    )
    result["dream_generated"] = True


async def _try_active_dreaming(db: AsyncSession, user_id: str) -> None:
    if not config.memory.get("dreaming_enabled"):
        return
    from app.services.memory_cost_governance_service import is_step_enabled
    if not await is_step_enabled(user_id, "dreaming_step6_off", db):
        return
    from app.services.memory_dreaming_service import run_active_dreaming
    try:
        await run_active_dreaming(db, user_id)
    except Exception:
        logger.exception("Active dreaming failed for user=%s", user_id)
