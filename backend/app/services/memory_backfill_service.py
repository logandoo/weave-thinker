# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""存量概念 importance 回填服务（2026-08-10）。

背景：importance 维度上线前创建的概念（迁移/提取）全为默认 0.5，需要按真实语义
重新评估。本服务对每个用户的概念（active+silent）分批发给 LLM，判定持久重要性，
写回 memory_concepts.importance。

- 幂等：重跑只覆盖 importance 列
- 容错：单个用户/单批 LLM 失败不中断（failed 计数）
- 评估 prompt 与提取 prompt 的 importance 语义一致：
  用户身份/偏好/昵称/关系/长期稳定事实 0.8-1.0；知识/技能/经验 0.5-0.8；
  一次性任务/临时话题/时事 0.2-0.5；不值得记 0-0.2；与提及频率无关。
"""
import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.services.memory_llm_factory import _memory_llm

config = get_config()
logger = logging.getLogger(__name__)

_BATCH_SIZE = 35


async def _evaluate_batch_llm(payload: str) -> str:
    """对一批概念发起 LLM 重要性评估，返回原始文本（供 _parse_verdicts 解析）。"""
    llm = _memory_llm("clarification")
    resp = await llm.complete_chat(
        [
            {"role": "system", "content": (
                "你是记忆重要性评估助手。对每个概念输出持久重要性分数。\n"
                "importance 定义（0-1）：\n"
                "- 0.8-1.0：用户身份/偏好/昵称/称呼/关系/长期稳定事实（一次陈述终身生效）\n"
                "- 0.5-0.8：知识/技能/经验/工作方法（有长期参考价值但非用户个人属性）\n"
                "- 0.2-0.5：一次性任务/临时话题/时事（当前有用，事后价值低）\n"
                "- 0-0.2：不值得记的琐碎内容\n"
                "importance 与提及频率无关：用户只说过一次但终身生效的偏好必须高分；"
                "频繁提及的一次性工作话题不能高分。\n"
                "输出 JSON 对象：{\"verdicts\": [{\"id\": \"<concept_id>\", \"importance\": 0.85}]}，"
                "每一条已给概念都必须有 verdict。"
            )},
            {"role": "user", "content": payload},
        ],
        temperature=0.1,
    )
    return (resp or "").strip()


def _parse_verdicts(resp: str) -> dict:
    """容错解析 LLM 输出为 {concept_id: importance}（容忍围栏/散文包裹/坏 JSON；
    字符串数字 verdict 会被拒绝，不强制转换）。"""
    verdicts: dict = {}
    t = (resp or "").strip()
    candidates = []
    if t:
        candidates.append(t)
    lines = [l for l in t.split("\n") if not l.lstrip().startswith("```")]
    joined = "\n".join(lines).strip()
    if joined != t:
        candidates.append(joined)
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        items = parsed.get("verdicts") or parsed.get("concepts") or []
        if isinstance(items, list) and items:
            for it in items:
                if not isinstance(it, dict):
                    continue
                cid = str(it.get("id") or "").strip()
                imp = it.get("importance")
                if not cid or not isinstance(imp, (int, float)) or isinstance(imp, bool):
                    continue
                try:
                    val = float(imp)
                except (TypeError, ValueError):
                    continue
                if 0.0 <= val <= 1.0:
                    verdicts[cid] = val
            if verdicts:
                break
        if not verdicts and all(
            isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
            for k, v in parsed.items()
        ):
            # 兜底：直接 {id: importance} 平铺格式
            for cid, val in parsed.items():
                if 0.0 <= float(val) <= 1.0:
                    verdicts[cid] = float(val)
            if verdicts:
                break
    return verdicts


async def evaluate_user_concepts(db: AsyncSession, user_id: str, force: bool = False) -> dict:
    """对单个用户的概念执行重要性评估并写回（默认仅处理默认值 0.5 的行）。

    force=True 时重评全部 active+silent 概念（线上合并路径 importance 只升不降，
    无条件覆盖会打回真实偏好——A4.9 审查 I2）。

    返回 {"evaluated": n, "updated": n, "failed": n}。
    """
    where_extra = "" if force else "AND importance_evaluated = FALSE"
    result = await db.execute(
        text(f"SELECT id, canonical_name, description_short, importance "
             f"FROM memory_concepts WHERE user_id = :uid AND status IN ('active','silent') "
             f"AND valid_to IS NULL {where_extra} ORDER BY created_at"),
        {"uid": user_id},
    )
    rows = result.fetchall()
    if not rows:
        return {"evaluated": 0, "updated": 0, "failed": 0}

    stats = {"evaluated": 0, "updated": 0, "failed": 0}
    for start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[start : start + _BATCH_SIZE]
        payload = "\n".join(
            f"- id={r[0]} | 概念: {r[1]} | 描述: {(r[2] or '')[:100]}"
            for r in batch
        )
        try:
            resp = await _evaluate_batch_llm(payload)
            verdicts = _parse_verdicts(resp)
            if not verdicts and len(batch) > 1:
                # 整批无有效 verdict：拆半两半分别重试一次（防 token 截断/LLM 飘，
                # F2：两半都覆盖，不丢后半）
                logger.warning("backfill: empty verdicts for user=%s batch=%d, retrying halves", user_id, start)
                half1, half2 = batch[: len(batch) // 2], batch[len(batch) // 2:]
                for half in (half1, half2):
                    try:
                        resp2 = await _evaluate_batch_llm(
                            "\n".join(f"- id={r[0]} | 概念: {r[1]} | 描述: {(r[2] or '')[:100]}" for r in half)
                        )
                        verdicts2 = _parse_verdicts(resp2)
                    except Exception:
                        verdicts2 = None
                    if verdicts2:
                        for cid, imp in verdicts2.items():
                            if cid in {r[0] for r in half}:
                                await db.execute(
                                    text("UPDATE memory_concepts SET importance = :imp, importance_evaluated = TRUE, "
                                         "updated_at = NOW() WHERE id = :cid AND user_id = :uid"),
                                    {"imp": imp, "cid": cid, "uid": user_id},
                                )
                                stats["updated"] += 1
                        stats["evaluated"] += len(half)
                    else:
                        stats["failed"] += 1
                stats["failed"] += 1
                continue
            if not verdicts:
                stats["failed"] += 1
                logger.warning("backfill: empty verdicts for user=%s batch=%d", user_id, start)
                continue
            batch_ids = {r[0] for r in batch}
            missing = 0
            for cid, imp in verdicts.items():
                if cid not in batch_ids:
                    continue
                await db.execute(
                    text("UPDATE memory_concepts SET importance = :imp, importance_evaluated = TRUE, "
                         "updated_at = NOW() WHERE id = :cid AND user_id = :uid"),
                    {"imp": imp, "cid": cid, "uid": user_id},
                )
                stats["updated"] += 1
            missing = len(batch_ids) - sum(1 for cid in verdicts if cid in batch_ids)
            if missing:
                logger.warning("backfill: user=%s batch=%d missing %d verdicts", user_id, start, missing)
            stats["evaluated"] += len(batch)
        except Exception:
            logger.exception("backfill: user %s batch %d failed", user_id, start)
            stats["failed"] += 1
    return stats


async def evaluate_all_users(db: AsyncSession, user_ids: Optional[list[str]] = None, force: bool = False) -> dict:
    """遍历全部活跃用户（或指定用户）执行重要性评估。

    返回 {"users": n, "evaluated": n, "updated": n, "failed": n}。
    """
    if user_ids is None:
        result = await db.execute(
            text("SELECT id FROM users WHERE is_active = TRUE ORDER BY last_login_at DESC NULLS LAST")
        )
        user_ids = [r[0] for r in result.fetchall()]
    total = {"users": len(user_ids), "evaluated": 0, "updated": 0, "failed": 0}
    for uid in user_ids:
        try:
            st = await evaluate_user_concepts(db, uid, force=force)
        except Exception:
            logger.exception("backfill: user %s failed", uid)
            total["failed"] += 1
            continue
        for k in ("evaluated", "updated", "failed"):
            total[k] += st[k]
        logger.info("backfill: user=%s evaluated=%d updated=%d", uid[:8], st["evaluated"], st["updated"])
        await db.commit()
    return total
