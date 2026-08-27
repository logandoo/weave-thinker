# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import MemoryClarification, MemoryConcept

config = get_config()
logger = logging.getLogger(__name__)

_SIGNAL_WORDS = [
    "不是", "不对", "错了", "我说的是", "其实是", "我的意思是",
    "不是这个意思", "你理解错了", "纠正一下", "相反", "忘掉", "别记住",
]


def detect_signal(user_message: str) -> bool:
    for word in _SIGNAL_WORDS:
        if word in user_message:
            if word == "不是" and "是不是" in user_message:
                continue
            return True
    return False


async def process_clarification(
    db: AsyncSession, user_id: str, user_message: str,
    conversation_id: str | None = None, message_id: str | None = None,
) -> dict | None:
    from app.services.memory_llm_factory import _memory_llm

    result = await db.execute(
        text("SELECT id, canonical_name, description_short, aliases FROM memory_concepts WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 AND valid_to IS NULL ORDER BY weight DESC LIMIT 5"),
        {"uid": user_id},
    )
    concepts = [{"id": r[0], "name": r[1], "short": r[2], "aliases": r[3]} for r in result.fetchall()]

    concepts_text = "\n".join(
        f"- {c['name']} (id={c['id']}): {c['short'] or ''}" for c in concepts
    ) or "无"

    prompt = {
        "role": "system",
        "content": (
            "你是记忆修正检测助手。判断用户消息是否在修正/否定之前的记忆。\n"
            "输出 JSON：{\"is_correction\": bool, \"correction_type\": \"negate|refine|add_constraint|forget\", "
            "\"affected_concept_ids\": [...], \"new_description\": \"...\", \"confidence\": 0.0-1.0}"
        ),
    }

    try:
        llm = _memory_llm("clarification")
        response = await llm.complete_chat(
            [
                prompt,
                {"role": "user", "content": f"已知概念：\n{concepts_text}\n\n用户消息：{user_message}"},
            ],
            temperature=float(config.memory.get("clarification_temperature", 0.1)),

        )
        response = (response or "").strip()
        if response.startswith("```"):
            lines = response.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            response = "\n".join(lines)
        parsed = json.loads(response)
    except Exception as e:
        logger.warning("Clarification LLM call failed: %s", e)
        return None

    if not parsed.get("is_correction"):
        return None

    confidence = float(parsed.get("confidence", 0))
    auto_threshold = float(config.memory.get("clarification_auto_apply_threshold", 0.8))

    clar_id = str(uuid.uuid4())
    clarification = MemoryClarification(
        id=clar_id,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        original_text=user_message,
        correction_type=parsed.get("correction_type", "negate"),
        affected_concept_ids=json.dumps(parsed.get("affected_concept_ids", [])),
        new_description=parsed.get("new_description", ""),
        confidence=confidence,
        applied=confidence >= auto_threshold,
        applied_at=datetime.utcnow() if confidence >= auto_threshold else None,
    )
    db.add(clarification)

    if confidence >= auto_threshold:
        await _apply_clarification(db, parsed, user_id)
        await db.flush()

    await db.commit()
    return parsed


async def _apply_clarification(db: AsyncSession, parsed: dict, user_id: str) -> None:
    ctype = parsed.get("correction_type", "negate")
    affected_ids = parsed.get("affected_concept_ids", [])
    if not isinstance(affected_ids, list):
        affected_ids = []

    if ctype == "negate":
        for cid in affected_ids:
            await db.execute(
                text("UPDATE memory_concepts SET valid_to = NOW(), weight = 0, updated_at = NOW() WHERE id = :id AND user_id = :uid"),
                {"id": cid, "uid": user_id},
            )

    elif ctype == "refine":
        new_desc = parsed.get("new_description", "")
        for cid in affected_ids:
            concept = await db.get(MemoryConcept, cid)
            if not concept or concept.user_id != user_id:
                continue
            old_full = concept.description_full
            concept.metadata_json = json.dumps({
                "audit_old_description": old_full,
                "audit_old_description_short": concept.description_short,
            }, ensure_ascii=False)
            concept.description_short = new_desc[:80]
            concept.description_full = new_desc[:5000]
            if concept.status == "silent" and concept.source_trust == "agent_inferred":
                concept.activation_strength = 1.0
                concept.status = "active"
            if concept.needs_review:
                concept.needs_review = False
            concept.updated_at = datetime.utcnow()

    elif ctype == "add_constraint":
        for cid in affected_ids:
            concept = await db.get(MemoryConcept, cid)
            if not concept or concept.user_id != user_id:
                continue
            if concept.description_full and parsed.get("new_description"):
                concept.metadata_json = json.dumps({
                    "audit_old_description": concept.description_full,
                }, ensure_ascii=False)
                concept.description_full = (concept.description_full + "\n\n约束: " + parsed["new_description"])[:5000]
            concept.updated_at = datetime.utcnow()

    elif ctype == "forget":
        for cid in affected_ids:
            concept = await db.get(MemoryConcept, cid)
            if not concept or concept.user_id != user_id:
                continue
            await db.execute(text("DELETE FROM concept_cluster_members WHERE concept_id = :id"), {"id": cid})
            await db.execute(text("DELETE FROM concept_relations WHERE source_id = :id OR target_id = :id"), {"id": cid})
            await db.execute(text("DELETE FROM memory_concepts WHERE id = :id AND user_id = :uid"), {"id": cid, "uid": user_id})


async def get_recent_clarifications(db: AsyncSession, user_id: str, days: int = 3) -> list[dict]:
    result = await db.execute(
        text("SELECT original_text, correction_type, affected_concept_ids, new_description FROM memory_clarifications WHERE user_id = :uid AND applied = TRUE AND applied_at >= now() - make_interval(days => :d) ORDER BY applied_at DESC LIMIT 10"),
        {"uid": user_id, "d": days},
    )
    return [
        {"original_text": r[0], "correction_type": r[1],
         "affected_concept_ids": r[2], "new_description": r[3]}
        for r in result.fetchall()
    ]


async def revert_clarification(db: AsyncSession, user_id: str, clarification_id: str) -> bool:
    """§9.6/§10.4：撤销已应用的澄清。

    negate → 清 valid_to 恢复有效（weight 回初始值）；refine/add_constraint →
    回滚 metadata_json 中的旧版本并重生成 embedding；forget 物理删除不可撤销。
    """
    clar = await db.get(MemoryClarification, clarification_id)
    if not clar or clar.user_id != user_id or not clar.applied:
        return False
    if clar.correction_type == "forget":
        return False

    try:
        affected = json.loads(clar.affected_concept_ids or "[]")
    except (json.JSONDecodeError, TypeError):
        affected = []

    for cid in affected:
        concept = await db.get(MemoryConcept, cid)
        if not concept or concept.user_id != user_id:
            continue
        if clar.correction_type == "negate":
            concept.valid_to = None
            if (concept.weight or 0) <= 0:
                concept.weight = float(config.memory_concept.get("weight_init", 0.5))
            concept.updated_at = datetime.utcnow()
        elif clar.correction_type in ("refine", "add_constraint"):
            try:
                meta = json.loads(concept.metadata_json or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            old_full = meta.get("audit_old_description")
            old_short = meta.get("audit_old_description_short")
            if old_full is not None:
                concept.description_full = old_full
            if old_short is not None:
                concept.description_short = old_short
            # 合并而非清空：仅移除审计键，保留 metadata_json 其他扩展字段
            meta.pop("audit_old_description", None)
            meta.pop("audit_old_description_short", None)
            concept.metadata_json = json.dumps(meta, ensure_ascii=False) if meta else None
            try:
                from app.services.memory_concept_service import _generate_embedding
                emb = await _generate_embedding(
                    concept.canonical_name,
                    json.loads(concept.aliases or "[]"),
                    concept.description_short or "",
                )
                if emb:
                    concept.embedding = emb
                    concept.embedding_updated_at = datetime.utcnow()
            except Exception:
                logger.debug("revert embedding regen failed", exc_info=True)
            concept.updated_at = datetime.utcnow()

    clar.applied = False
    clar.applied_at = None
    await db.flush()
    return True
