# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.services.memory_weight_service import apply_reinforcement_signal

config = get_config()
logger = logging.getLogger(__name__)


async def run_active_dreaming(db: AsyncSession, user_id: str) -> list[dict]:
    # 2026-08-16 修复：演练对象按"有效权重"（weight × Ebbinghaus 残差衰减，
    # anchor = max(last_recalled_at, weight_decayed_at)，与 run_weight_decay/
    # consolidation dream 共用同一 helper）排序——旧实现按原始 weight 恒序，
    # 未召回概念永远在演练名单内并被 nightly dreaming_confirmation +0.05 续命，
    # 形成"高权重→被演练→确认→更高权重"的自我强化循环（A4.9 审查 C2 同源）。
    from app.services.memory_weight_service import effective_weight_sql
    eff_expr = effective_weight_sql()
    top_result = await db.execute(
        text(f"SELECT id, canonical_name, description_short, ROUND(CAST({eff_expr} AS numeric), 2) "
             "FROM memory_concepts WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 "
             "AND valid_to IS NULL ORDER BY " + eff_expr + " DESC LIMIT 10"),
        {"uid": user_id},
    )
    top_concepts = [{"id": r[0], "name": r[1], "short": r[2], "weight": float(r[3] or 0)} for r in top_result.fetchall()]
    # 附录 A 优化：概念数 <10 时跳过（记忆太少无需演练）
    if len(top_concepts) < 10:
        return []

    scenarios = await _generate_scenarios(top_concepts)
    if not scenarios:
        return []

    evaluations = await _rehearse_scenarios(scenarios, top_concepts)
    return await _apply_dreaming_feedback(db, user_id, evaluations)


async def _generate_scenarios(concepts: list[dict]) -> list[str]:
    from app.services.memory_llm_factory import _memory_llm
    concepts_text = "\n".join(f"- {c['name']}: {c['short']}" for c in concepts)
    try:
        llm = _memory_llm("dream")
        resp = await llm.complete_chat(
            [
                {"role": "system", "content": "生成 2-3 个假设性用户问题/任务场景，用于测试概念间的一致性。输出 JSON array of strings。"},
                {"role": "user", "content": f"概念：\n{concepts_text}"},
            ],
            temperature=0.3,
        )
        resp = (resp or "").strip()
        if resp.startswith("```"):
            resp = resp.split("\n", 1)[1].rsplit("\n", 1)[0]
        return json.loads(resp) if resp.startswith("[") else []
    except Exception:
        return []


async def _rehearse_scenarios(scenarios: list[str], concepts: list[dict]) -> list[dict]:
    from app.services.memory_llm_factory import _memory_llm
    concepts_text = "\n".join(f"- {c['name']} (id={c['id']}): {c['short']}" for c in concepts)
    all_evals = []
    for scenario in scenarios:
        try:
            llm = _memory_llm("dream")
            resp = await llm.complete_chat(
                [
                    {"role": "system", "content": "根据以下概念回答用户问题，然后自评每个概念是否被正确使用。输出JSON：{\"concept_evaluations\": [{\"concept_id\": \"...\", \"verdict\": \"confirmed|contradicted|gap_found\", \"note\": \"...\"}], \"new_insights\": [...], \"contradictions_found\": [...]}"},
                    {"role": "user", "content": f"已知概念：\n{concepts_text}\n\n场景：{scenario}"},
                ],
                temperature=0.2,
            )
            resp = (resp or "").strip()
            if resp.startswith("```"):
                lines = resp.split("\n")
                resp = "\n".join(lines[1:-1])
            all_evals.append(json.loads(resp))
        except Exception:
            continue
    return all_evals


async def _apply_dreaming_feedback(db: AsyncSession, user_id: str, evaluations: list[dict]) -> list[dict]:
    """§5.5 Step 6c 记忆反馈：confirmed/contradicted/gap_found/new_insights/contradictions_found 全落地。"""
    results = []
    for ev in evaluations:
        evals_list = ev.get("concept_evaluations", [])
        for ce in evals_list:
            cid = ce.get("concept_id")
            verdict = ce.get("verdict", "")
            note = (ce.get("note") or "").strip()
            if verdict == "confirmed" and cid:
                await apply_reinforcement_signal(db, cid, "dreaming_confirmation")
                await _maybe_promote_silent(db, cid)
            elif verdict == "contradicted" and cid:
                await apply_reinforcement_signal(db, cid, "dreaming_contradiction")
                await db.execute(
                    text("UPDATE memory_concepts SET needs_review = TRUE WHERE id = :id"),
                    {"id": cid},
                )
            elif verdict == "gap_found" and note:
                # §6c：gap_found → 创建新概念（dreaming / agent_inferred，weight=0.3，silent）
                await _create_dreaming_concept(db, user_id, note)

        for insight in ev.get("new_insights", []):
            # §6c：new_insights → 尝试匹配已有概念，失败则创建（agent_inferred，受 0.7 封顶）
            if isinstance(insight, str) and insight.strip():
                await _match_or_create_insight(db, user_id, insight.strip())

        for contra in ev.get("contradictions_found", []):
            # §6c：contradictions_found → 创建 concept_relations(type='contradicts')
            await _create_contradiction_relation(db, user_id, contra)

        results.append({"concept_evaluations": evals_list})
    await db.commit()
    return results


async def _create_dreaming_concept(db: AsyncSession, user_id: str, text_note: str) -> None:
    from app.services.memory_concept_service import create_concept
    name = text_note[:60]
    dup = await db.execute(
        text("SELECT 1 FROM memory_concepts WHERE user_id = :uid AND canonical_name = :nm AND valid_to IS NULL LIMIT 1"),
        {"uid": user_id, "nm": name},
    )
    if dup.fetchone():
        return
    cid = await create_concept(
        db, user_id, canonical_name=name,
        description_short=text_note[:80], description_full=text_note,
        source_trust="agent_inferred", memory_type="semantic", source_type="dreaming",
    )
    if cid:
        await db.execute(
            text("UPDATE memory_concepts SET weight = 0.3 WHERE id = :id"),
            {"id": cid},
        )


async def _match_or_create_insight(db: AsyncSession, user_id: str, insight: str) -> None:
    from app.services.memory_concept_service import create_concept
    dup = await db.execute(
        text("SELECT 1 FROM memory_concepts WHERE user_id = :uid AND canonical_name = :nm AND valid_to IS NULL LIMIT 1"),
        {"uid": user_id, "nm": insight[:60]},
    )
    if dup.fetchone():
        return
    try:
        from app.services.memory_embedding_service import embed_text, find_similar_concepts
        emb = await embed_text(insight)
        if emb:
            similar = await find_similar_concepts(db, user_id, emb, top_k=1)
            if similar and similar[0]["similarity"] >= 0.9:
                return
    except Exception:
        pass
    await create_concept(
        db, user_id, canonical_name=insight[:60],
        description_short=insight[:80], description_full=insight,
        source_trust="agent_inferred", memory_type="semantic", source_type="dreaming",
    )


async def _create_contradiction_relation(db: AsyncSession, user_id: str, contra) -> None:
    """contradictions_found 项 → contradicts 关系。兼容 dict({source,target}) 与自由文本两种形态。"""
    src_name = tgt_name = None
    if isinstance(contra, dict):
        src_name = contra.get("source")
        tgt_name = contra.get("target")
    elif isinstance(contra, str):
        # 自由文本：提取其中提到的概念名，取前两个配对
        r = await db.execute(
            text("SELECT canonical_name FROM memory_concepts WHERE user_id = :uid AND valid_to IS NULL"),
            {"uid": user_id},
        )
        mentioned = [row[0] for row in r.fetchall() if row[0] and row[0] in contra]
        if len(mentioned) >= 2:
            src_name, tgt_name = mentioned[0], mentioned[1]
    if not src_name or not tgt_name or src_name == tgt_name:
        return

    r = await db.execute(
        text("SELECT id, canonical_name FROM memory_concepts WHERE user_id = :uid AND canonical_name = ANY(:names) AND valid_to IS NULL"),
        {"uid": user_id, "names": [src_name, tgt_name]},
    )
    id_map = {row[1]: row[0] for row in r.fetchall()}
    sid, tid = id_map.get(src_name), id_map.get(tgt_name)
    if not sid or not tid:
        return
    dup = await db.execute(
        text("SELECT 1 FROM concept_relations WHERE source_id = :s AND target_id = :t AND relation_type = 'contradicts' LIMIT 1"),
        {"s": sid, "t": tid},
    )
    if dup.fetchone():
        return
    from app.services.memory_cluster_service import create_relation
    await create_relation(db, user_id, sid, tid, "contradicts",
                          "Dreaming 演练发现矛盾", weight=0.6)
    from app.services.memory_weight_service import apply_reinforcement_signal
    await apply_reinforcement_signal(db, sid, "dreaming_contradiction")
    await apply_reinforcement_signal(db, tid, "dreaming_contradiction")


async def _maybe_promote_silent(db: AsyncSession, concept_id: str) -> None:
    result = await db.execute(
        text("SELECT status FROM memory_concepts WHERE id = :id"),
        {"id": concept_id},
    )
    row = result.fetchone()
    if row and row[0] == "silent":
        await db.execute(
            text("UPDATE memory_concepts SET activation_strength = 1.0, status = 'active', updated_at = NOW() WHERE id = :id"),
            {"id": concept_id},
        )
