# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""E3（2026-09-14，默认关）：listwise 记忆验证器试点（M9 / 前案 R8）。

对一组候选记忆做一次 LLM listwise 调用：标注每个节点的效用与角色
（relevant / redundant / conflicting / irrelevant），返回保留 id 列表。
**仅试点路径使用**（死磕 / 后台等延迟不敏感装配点）；热路径零改动。

失败语义（fail-open）：超时 / 异常 / 解析失败 / 开关关闭 → 返回 None，
调用方回退现有注入。调用必须入台账（read 计费类）且带超时。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from app.core.config import get_config

logger = logging.getLogger(__name__)

VALID_ROLES = ("relevant", "redundant", "conflicting", "irrelevant")


def _parse_keep_ids(raw: str, candidate_ids: list[str]) -> Optional[list[str]]:
    """解析 LLM listwise 输出 → 保留 id 列表（仅接受候选内 id）。"""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    try:
        data = json.loads(text)
    except Exception:
        return None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    allowed = set(candidate_ids)
    keep: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cid = str(it.get("id") or "")
        role = str(it.get("role") or "").strip().lower()
        if cid in allowed and role in VALID_ROLES and role != "irrelevant" and cid not in keep:
            keep.append(cid)
    return keep or None


async def verify_listwise(
    user_id: str, query_text: str, candidates: list[dict],
    timeout_ms: Optional[int] = None,
) -> Optional[list[str]]:
    """listwise 保留 id 列表；None = 失败/关闭（调用方回退现有注入）。

    candidates: [{"id": str, "text": str}, ...]（text 已截断，仅试点路径）。
    """
    cfg = get_config().memory_retrieval
    if not cfg.get("listwise_verifier_enabled", False):
        return None
    if not candidates:
        return None
    timeout_s = float(timeout_ms or cfg.get("listwise_verifier_timeout_ms", 8000)) / 1000.0
    ids = [str(c.get("id")) for c in candidates if c.get("id")]
    listing = "\n".join(
        f"- id={c.get('id')}: {str(c.get('text') or '')[:200]}" for c in candidates)
    try:
        from app.services.memory_llm_factory import _memory_llm
        llm = _memory_llm("listwise_verifier")
        resp = await asyncio.wait_for(
            llm.complete_chat(
                [
                    {"role": "system", "content": (
                        "你是记忆装配验证器。对每个候选标注角色并只保留有独立效用的条目："
                        "role ∈ relevant|redundant|conflicting|irrelevant。"
                        '输出 JSON: {"items": [{"id": "...", "role": "..."}]}'
                    )},
                    {"role": "user", "content": f"查询：{query_text}\n候选：\n{listing}"},
                ],
                temperature=0.0,
            ),
            timeout=timeout_s,
        )
        # A4a：试点调用入账（read 遥测）
        try:
            from app.services.memory_cost_governance_service import record_llm_call_bg
            await record_llm_call_bg(user_id, "listwise_verifier", billing_class="read")
        except Exception:
            logger.debug("listwise record failed", exc_info=True)
        return _parse_keep_ids(resp or "", ids)
    except asyncio.TimeoutError:
        logger.info("listwise verifier timed out after %.1fs — fallback", timeout_s)
        return None
    except Exception:
        logger.debug("listwise verifier failed — fallback", exc_info=True)
        return None
