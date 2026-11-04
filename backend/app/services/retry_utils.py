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


def parse_tool_arguments(raw: Optional[str]) -> Optional[dict]:
    """Parse accumulated tool-call arguments, fail-closed.

    Returns the parsed dict for complete payloads (including ones repaired by
    adding missing closing braces after a COMPLETE value), `{}` for missing
    arguments (no-param calls), and **None** when the payload is genuinely
    truncated (cut mid-string) or is valid JSON that is not an object.

    Callers MUST NOT dispatch a call whose parse returns None with empty
    arguments: production incident conv fcb2e60a (voice notes.create_note)
    showed the silent `{}` fallback executing a *different* call, which the
    tool answered with "Unknown action" and the model retried the identical
    overlong call until the round budget ran out.
    """
    if raw is None:
        return {}
    if not isinstance(raw, str):
        # a non-string payload (dict/list/…) is not a provider-shaped argument
        # string — fail closed instead of silently dispatching `{}`.
        return None
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if parsed is not None:
        # valid JSON but not an object — invalid tool arguments (all tool
        # schemas declare type=object); fail closed rather than dispatch.
        return None
    repaired = repair_tool_call_arguments(raw)
    if repaired.strip() == "{}" and raw.strip() not in ("{}", "None"):
        # repair_tool_call_arguments uses "{}" as its unrepairable sentinel
        return None
    try:
        parsed = json.loads(repaired)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def truncated_tool_args_feedback(tool_name: str) -> str:
    """Actionable tool result for a call whose arguments were cut off by the
    model's output budget (or are otherwise unparseable). The model must
    shrink/chunk the payload instead of repeating the same overlong call."""
    return json.dumps(
        {
            "error": (
                f"工具 {tool_name} 的参数被截断或不是合法 JSON（输出长度超出上限），调用未执行。"
                "请把长内容分块写入（如笔记：先 create_note 写开头，再用 append_note "
                "分批追加，每次约 800 字以内）；查询类操作可直接重试；"
                "不要原样重复上一次的超长调用。"
            ),
            "_truncated": True,
        },
        ensure_ascii=False,
    )


def plan_tool_call_for_dispatch(tool_call: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Decide how to handle one tool call's arguments.

    Returns ``(args, None)`` to dispatch normally, or ``(None, feedback_json)``
    when the arguments are unusable — the caller must NOT execute the call and
    should feed ``feedback_json`` back to the model as the tool result.
    """
    fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    name = fn.get("name", "?") if isinstance(fn, dict) else "?"
    raw = fn.get("arguments") if isinstance(fn, dict) else None
    args = parse_tool_arguments(raw)
    if args is None:
        return None, truncated_tool_args_feedback(name)
    return args, None


def decide_tool_dispatch(
    plan: Dict[str, Tuple[Optional[dict], Optional[str]]],
    tool_call: dict,
) -> Tuple[Optional[dict], Optional[str]]:
    """Look up one call's pre-computed dispatch decision (see
    ``plan_tool_calls_for_round``); defensively re-plans if the call id is
    missing from the plan (never dispatch an unmapped call with empty args)."""
    tc_id = tool_call.get("id", "") if isinstance(tool_call, dict) else ""
    args, error = plan.get(tc_id, (None, None))
    if args is None and error is None:
        return plan_tool_call_for_dispatch(tool_call)
    return args, error


def _batch_dedup_signature(tool_name: str, args: Optional[dict]) -> Optional[tuple]:
    """Canonical signature for same-round duplicate detection.

    Scope is deliberately narrow (P0): duplicate ``web_search`` calls with the
    same query SET + time_sensitive flag, and duplicate ``memory(read)``
    targets.  Write actions and every other tool are untouched.  This is a
    structural comparison, not semantic dedup (D-13/D-936).
    """
    if not isinstance(args, dict):
        return None
    name = str(tool_name or "")
    if name == "web_search":
        raw = args.get("queries")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return None
        queries = tuple(sorted({
            str(q).strip().casefold() for q in raw if str(q).strip()
        }))
        if not queries:
            return None
        return ("web_search", queries, bool(args.get("time_sensitive", False)))
    if name == "memory" and str(args.get("action") or "read").lower() == "read":
        # Respect the per-turn dedup kill switch.  Import lazily: retry_utils
        # is also imported by offline scripts without a loaded config.
        try:
            from app.core.config import get_config
            if not get_config().agent_tool_loop_memory_read_dedup:
                return None
        except Exception:
            pass
        return ("memory", "read", str(args.get("target") or "agent").lower())
    return None


def plan_tool_calls_for_round(
    tool_calls: List[dict],
) -> Tuple[Dict[str, Tuple[Optional[dict], Optional[str]]], List[dict]]:
    """Plan dispatch for one assistant round AND build provider-safe copies.

    MUST be called on the RAW tool_calls, BEFORE any provider-safety
    normalization rewrites ``arguments`` (the 2026-08-21 dflash2 fix used to
    rewrite unparseable args in place to the ``"{}"`` sentinel — planning
    first is exactly what keeps a truncated call from being dispatched as
    ``{}`` and answered "Unknown action").

    Returns ``(plan, safe_calls)``:
      * ``plan``       — ``{call_id: (args, None)}`` → dispatch, or
                         ``(None, feedback_json)`` → fail closed with a
                         synthesized actionable tool result;
      * ``safe_calls`` — NEW dicts for the assistant message sent upstream /
                         persisted: every ``arguments`` is valid JSON (parsed
                         as-is, repaired, or ``"{}"`` when unusable). The
                         input dicts are never mutated.
    """
    plan: Dict[str, Tuple[Optional[dict], Optional[str]]] = {}
    safe_calls: List[dict] = []
    seen_dedup: set[tuple] = set()
    for tc in tool_calls:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        raw = fn.get("arguments") if isinstance(fn, dict) else None
        args, error = plan_tool_call_for_dispatch(tc)
        call_id = tc.get("id", "") if isinstance(tc, dict) else ""
        duplicate_note: Optional[str] = None
        if error is None and isinstance(args, dict):
            sig = _batch_dedup_signature(fn.get("name", ""), args)
            if sig is not None:
                if sig in seen_dedup:
                    duplicate_note = json.dumps({
                        "duplicate_call": True,
                        "note": (
                            "同一轮已计划过一次相同调用（web_search 查询集合 / "
                            "memory read 目标相同），本次不重复执行；请使用已有结果继续。"
                        ),
                    }, ensure_ascii=False)
                else:
                    seen_dedup.add(sig)
        if duplicate_note is not None:
            plan[call_id] = (None, duplicate_note)
        else:
            plan[call_id] = (args, error)
        if error is not None or not isinstance(raw, str):
            safe_args = "{}"
        else:
            safe_args = raw
            try:
                json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                safe_args = repair_tool_call_arguments(raw, fn.get("name", "?"))
        safe_calls.append({**tc, "function": {**fn, "arguments": safe_args}})
    return plan, safe_calls


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
