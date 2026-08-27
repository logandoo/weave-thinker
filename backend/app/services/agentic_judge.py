# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Shared agentic-judgment helpers.

The ONLY sanctioned way to make semantic judgments in this codebase: an LLM
call with JSON output. Deterministic code may handle protocol parsing, safety
filters, dedup and documented policy fallbacks — never intent/completion/
quality classification by regex or keyword matching (user principle
2026-07-20: 禁止正则/硬编码分类器，语义判断留给 LLM).

Every helper is LLM-first, bounded-input, JSON-schema driven, and returns a
safe default on any LLM failure so callers implement their documented
fallback policy (never silent wrong judgments).
"""
import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional

from app.services.auxiliary_client import AuxiliaryClient

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Structurally extract the first JSON object from an LLM reply.

    Tolerates exact JSON, fenced code blocks and prose wrapping. Pure
    structure parsing — not a judgment.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        nl = t.find("\n")
        if nl != -1:
            head = t[:nl].strip().lower()
            if head in ("", "json"):
                t = t[nl + 1:].strip()
        else:
            t = t
    try:
        parsed = json.loads(t)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    for m in _JSON_OBJECT_RE.finditer(t):
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return None


async def judge_json(
    system_prompt: str,
    user_prompt: str,
    *,
    task: str = "default",
    default: Optional[Dict[str, Any]] = None,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    timeout: float = 30.0,
) -> Optional[Dict[str, Any]]:
    """One LLM judgment call with JSON output.

    Returns ``default`` (or None) on any LLM failure — never raises.
    Callers map the failure to their documented fallback policy. The
    ``timeout`` is ENFORCED via asyncio.wait_for — a hung classifier model
    must never freeze a user-visible stream beyond the configured bound
    (A4.9 review finding: the SDK default alone caps at ~600s).
    """
    try:
        client = AuxiliaryClient(task=task)

        async def _call() -> str:
            _kw: Dict[str, Any] = {"temperature": temperature}
            if max_tokens is not None:
                _kw["max_tokens"] = max_tokens
            content, _ = await client.complete_parts(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **_kw,
            )
            return content or ""

        content = await asyncio.wait_for(_call(), timeout=timeout)
        parsed = extract_json_object(content)
        if parsed is None:
            return default
        return parsed
    except asyncio.TimeoutError:
        logger.warning("judge_json task=%s timed out after %.0fs", task, timeout)
        return default
    except Exception as exc:
        logger.warning("judge_json task=%s failed: %s", task, type(exc).__name__)
        return default
