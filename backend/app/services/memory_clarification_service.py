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

# 2026-09-20（用户指令）：原 `_SIGNAL_WORDS` 关键词表 + `detect_signal()` 请求路径
# 预分类闸门已删除——纠正检测必须 agentic（项目红线：路由/意图判断禁止关键词枚举，
# 见 memory/feedback_no_hardcoded_classifiers.md）。旧闸门漏检=纠正永不处理；
# 现在每条消息无条件由下方 LLM 判定 `is_correction`（成本用户已确认不考虑）。


async def process_clarification(
    db: AsyncSession, user_id: str, user_message: str,
    conversation_id: str | None = None, message_id: str | None = None,
    shadow: bool = False,
) -> dict | None:
    """B11（2026-09-14）：`shadow=True` 时只记录候选不应用（语音纠正先观察
    准确率；文字路径不受影响）。"""
    from app.services.memory_llm_factory import _memory_llm

    result = await db.execute(
        text("SELECT id, canonical_name, description_short, aliases FROM memory_concepts WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 AND valid_to IS NULL ORDER BY weight DESC LIMIT 5"),
        {"uid": user_id},
    )
    concepts = [{"id": r[0], "name": r[1], "short": r[2], "aliases": r[3]} for r in result.fetchall()]

    concepts_text = "\n".join(
        f"- {c['name']} (id={c['id']}): {c['short'] or ''}" for c in concepts
    ) or "无"

    # 2026-09-20（A4.9 r1 Minor）：LLM 往返前先结束当前事务、归还连接池槽位——
    # 全 agentic 后每条消息都会走到这里，若把池化连接压在 LLM 往返上，突发流量
    # 可能耗尽连接池。此处无业务写入（仅读概念），commit 安全。
    try:
        await db.commit()
    except Exception:
        logger.debug("clarify pre-LLM connection release failed", exc_info=True)

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
    except Exception as e:
        logger.warning("Clarification LLM call failed: %s", e)
        return None

    parsed = None
    try:
        cleaned = response
        if cleaned.startswith("```"):
            lines = [l for l in cleaned.split("\n") if not l.startswith("```")]
            cleaned = "\n".join(lines)
        parsed = json.loads(cleaned)
    except Exception as e:
        logger.warning("Clarification response parse failed: %s", e)

    _is_correction = bool(parsed and parsed.get("is_correction"))
    confidence = float((parsed or {}).get("confidence", 0) or 0)
    auto_threshold = float(config.memory.get("clarification_auto_apply_threshold", 0.8))
    # B11 shadow：记录 applied=False 候选（不应用、不写 applied_at）
    will_apply = bool(_is_correction and (not shadow) and confidence >= auto_threshold)

    # A4a（2026-09-14）：记忆 LLM 调用入账（计费类；DC1 隔离读路径遥测）。
    # 2026-09-20 全 agentic 化（每条消息都检测）后的两项修正：
    # ① 计费口径：只有真正进入写路径（will_apply）的调用算 'write'；未命中纠正、
    #    置信度不足未应用、解析失败一律遥测 ('read')——否则每条消息都会推高成本
    #    治理降级梯子（DC1 本意，同 shadow 的既有理由）。
    # ② 落账持久化：非纠正路径原先提前 return 未 commit，flush 的行随会话回滚，
    #    全部账目丢失（agentic 后是绝大多数调用）。
    try:
        from app.services.memory_cost_governance_service import record_llm_call
        async with db.begin_nested():
            await record_llm_call(
                db, user_id, "clarify",
                billing_class="write" if will_apply else "read",
            )
    except Exception:
        logger.debug("record clarify llm call failed", exc_info=True)

    if not _is_correction:
        # 非纠正（含解析失败）零业务写入，但账目必须落库。
        try:
            await db.commit()
        except Exception:
            logger.debug("clarify telemetry commit failed", exc_info=True)
        return None

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
        applied=will_apply,
        applied_at=datetime.utcnow() if will_apply else None,
    )
    db.add(clarification)

    if will_apply:
        await _apply_clarification(db, parsed, user_id)
        await db.flush()
    elif shadow:
        logger.info(
            "clarification shadow candidate (not applied): user=%s type=%s confidence=%.2f",
            user_id, parsed.get("correction_type", "negate"), confidence,
        )

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
                    from app.services.memory_embedding_service import _get_embedding_model
                    concept.embedding_model = _get_embedding_model()
            except Exception:
                logger.debug("revert embedding regen failed", exc_info=True)
            concept.updated_at = datetime.utcnow()

    clar.applied = False
    clar.applied_at = None
    await db.flush()
    return True
