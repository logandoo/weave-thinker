# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deathmatch creative-judge helper (P5.4 move-only, 2026-10-18)."""
from __future__ import annotations

import logging
import re as _re
from typing import Any, Dict

logger = logging.getLogger(__name__)


_CREATIVE_JUDGE_PENDING = object()

_CREATIVE_GOAL_CACHE: Dict[str, Any] = {}

async def _ensure_creative_judged(goal: str) -> bool:
    """LLM-judge whether the goal is a creative-writing task (the bible spec
    applies). One call per goal; callers that arrive while a judgment for the
    same goal is already in flight see False (fail-open) until it is cached;
    on LLM failure → False (bible skipped, the safe fail-open for a
    spec-enhancement layer)."""
    key = _normalize_goal_key(goal)
    if not key:
        return False
    cached = _CREATIVE_GOAL_CACHE.get(key)
    if cached is not None and cached is not _CREATIVE_JUDGE_PENDING:
        return cached is True
    if cached is _CREATIVE_JUDGE_PENDING:
        return False
    if len(_CREATIVE_GOAL_CACHE) > 256:
        _CREATIVE_GOAL_CACHE.clear()
    _CREATIVE_GOAL_CACHE[key] = _CREATIVE_JUDGE_PENDING
    result = False
    try:
        from app.services.agentic_judge import judge_json
        parsed = await judge_json(
            "你是任务分类器。判断给定的用户目标是否属于文学创作任务"
            "（小说/故事/诗歌/剧本/散文等虚构文学作品的创作）。\n"
            '输出JSON：{"is_creative": true|false}',
            f"用户目标：\n{key[:800]}\n\n只输出JSON。",
            task="creative_goal",
            default=None,
            timeout=120.0,
        )
        if isinstance(parsed, dict):
            result = bool(parsed.get("is_creative"))
    except Exception as exc:
        logger.warning("creative-goal LLM judgment failed: %s", exc)
    _CREATIVE_GOAL_CACHE[key] = result
    return result

def _normalize_goal_key(goal: str) -> str:
    return _re.sub(r"\s+", " ", (goal or "")).strip()
