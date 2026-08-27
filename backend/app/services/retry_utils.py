# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import random
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 1.0
_DEFAULT_MAX_DELAY = 30.0


def jittered_backoff(
    attempt: int,
    base_delay: float = _DEFAULT_BASE_DELAY,
    max_delay: float = _DEFAULT_MAX_DELAY,
) -> float:
    delay = min(base_delay * (2 ** attempt), max_delay)
    jitter = delay * 0.25 * (random.random() * 2 - 1)
    return max(0, delay + jitter)


def sanitize_surrogates(text: str) -> str:
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub('\ufffd', text)
    return text


def sanitize_messages_surrogates(messages: List[Dict[str, Any]]) -> bool:
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and _SURROGATE_RE.search(content):
            msg["content"] = _SURROGATE_RE.sub('\ufffd', content)
            found = True
        name = msg.get("name")
        if isinstance(name, str) and _SURROGATE_RE.search(name):
            msg["name"] = _SURROGATE_RE.sub('\ufffd', name)
            found = True
    return found


def coerce_tool_args(tool_name: str, args: Dict[str, Any], schema: dict = None) -> Dict[str, Any]:
    if not args or not isinstance(args, dict):
        return args

    props = (schema or {}).get("parameters", {}).get("properties", {})
    if not props:
        return args

    # tool_name is reserved for future per-tool coercion rules
    _ = tool_name

    for key, value in args.items():
        if not isinstance(value, str):
            continue
        prop_schema = props.get(key)
        if not prop_schema:
            continue
        expected = prop_schema.get("type")
        if isinstance(expected, list):
            for t in expected:
                coerced = _coerce_single(value, t)
                if coerced is not value:
                    args[key] = coerced
                    break
        else:
            coerced = _coerce_single(value, expected)
            if coerced is not value:
                args[key] = coerced

    return args


def _coerce_single(value: str, expected_type):
    if expected_type in ("integer", "number"):
        try:
            f = float(value)
        except (ValueError, OverflowError):
            return value
        if f != f or f == float("inf") or f == float("-inf"):
            return f
        if f == int(f):
            return int(f)
        if expected_type == "integer":
            return value
        return f
    if expected_type == "boolean":
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
        return value
    return value


def repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    raw = raw_args.strip() if isinstance(raw_args, str) else ""
    if not raw:
        return "{}"
    if raw == "None":
        return "{}"

    fixed = raw
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)

    open_curly = fixed.count('{') - fixed.count('}')
    open_bracket = fixed.count('[') - fixed.count(']')
    if open_curly > 0:
        fixed += '}' * open_curly
    if open_bracket > 0:
        fixed += ']' * open_bracket

    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith('}') and fixed.count('}') > fixed.count('{'):
                fixed = fixed[:-1]
            elif fixed.endswith(']') and fixed.count(']') > fixed.count('['):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        logger.warning("Repaired tool_call args for %s: %s → %s", tool_name, raw[:60], fixed[:60])
        return fixed
    except json.JSONDecodeError:
        logger.warning("Unrepairable tool_call args for %s, replaced with {}", tool_name)
        return "{}"


async def retry_async(
    coro_factory: Callable[[], Any],
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay: float = _DEFAULT_BASE_DELAY,
    retryable_exceptions: Tuple = (Exception,),
):
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except retryable_exceptions as e:
            last_error = e
            if attempt < max_retries:
                delay = jittered_backoff(attempt, base_delay)
                logger.warning("Retry %d/%d after %.1fs: %s", attempt + 1, max_retries, delay, e)
                await asyncio.sleep(delay)
    raise last_error
