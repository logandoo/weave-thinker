# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P4: 2D history window (messages + tokens) with tool-pair integrity.

The 40-message cut is independent of the model window.  Keep the NEWEST
messages (D-112); in ``2d`` mode additionally:

* estimate **all** fields that round-trip through the provider (content,
  multi-part text, ``reasoning_content``, tool-call arguments, tool results,
  names) — not just ``content``;
* respect an optional token budget and a minimum message floor;
* never split a tool-call/tool-result pair.  A cut that would leave an orphan
  ``tool`` message drops the orphan instead of sending an invalid request;
* optionally cap a single tool result by tokens (cheap, prefix-stable) before
  summarization is ever considered.

``legacy`` keeps the historical content-only behaviour exactly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.services.tool_image_results import (
    IMAGE_TOOL_ALLOWLIST,
    image_envelope_token_count,
    inline_images_enabled,
    parse_image_envelope,
)


def _content_of(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("content") or "")
    return str(getattr(msg, "content", "") or "")


def _field(msg: Any, name: str, default: Any = None) -> Any:
    if isinstance(msg, dict):
        return msg.get(name, default)
    return getattr(msg, name, default)


def _role_of(msg: Any) -> str:
    return str(_field(msg, "role", "") or "")


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("input_text") or ""))
            else:
                parts.append(str(part or ""))
        return "".join(parts)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    return str(value)


def _estimate_text(text: str) -> int:
    try:
        from app.services.context_compressor import estimate_text_tokens_rough
        return max(0, int(estimate_text_tokens_rough(text)))
    except Exception:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk + max(0, len(text) - cjk) // 4


def _is_inline_image_message(msg: Any) -> bool:
    from app.services.tool_image_results import IMAGE_TOOL_ALLOWLIST

    return str(_field(msg, "name") or "") in IMAGE_TOOL_ALLOWLIST


def _content_cost(msg: Any) -> int:
    """P0.5：allowlist 图片信封按尺寸计 token；其余内容按文本计。"""
    content = _content_text(_field(msg, "content"))
    if _is_inline_image_message(msg):
        payload = parse_image_envelope(content)
        if payload is not None:
            text_tokens = _estimate_text(str(payload.get("text") or ""))
            if not inline_images_enabled():
                return max(1, text_tokens)
            return max(1, text_tokens + image_envelope_token_count(payload))
    return _estimate_text(content)


def _tool_name_map(msg: Any) -> dict:
    out: dict = {}
    calls = _tool_calls_of(msg)
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except (json.JSONDecodeError, TypeError):
            calls = []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            cid = call.get("id")
            fn = call.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else ""
            if cid and name:
                out[str(cid)] = str(name)
    return out


def _step_text(step: dict) -> str:
    value = step.get("content")
    if value is None:
        value = step.get("result")
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _tool_results_cost(msg: Any, results: Any) -> int:
    """Cost the persisted legacy ``tool_results`` blob.

    P0.5（C1 review）：ORM ``Message`` rows have no ``name`` column; the
    screenshot envelope lives inside ``tool_results.agent_steps[].content``.
    Without this branch a 2.5 MB base64 blob is charged ~640k text tokens and
    evicts the whole history window after one screenshot.
    """
    if results in (None, "", [], {}):
        return 0
    if isinstance(results, str):
        try:
            data = json.loads(results)
        except (json.JSONDecodeError, TypeError):
            return _estimate_text(results)
    elif isinstance(results, (dict, list)):
        data = results
    else:
        return _estimate_text(str(results))

    steps = data.get("agent_steps") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        try:
            return _estimate_text(json.dumps(results, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return _estimate_text(str(results))

    name_map = _tool_name_map(msg)
    total = 0
    for step in steps:
        if not isinstance(step, dict):
            total += _estimate_text(str(step))
            continue
        raw = _step_text(step)
        tool_name = name_map.get(str(step.get("name") or step.get("call_id") or ""), "")
        if not tool_name:
            title = str(step.get("title") or "")
            tool_name = next((n for n in IMAGE_TOOL_ALLOWLIST if n in title), "")
        if tool_name in IMAGE_TOOL_ALLOWLIST:
            payload = parse_image_envelope(raw)
            if payload is not None:
                text_tokens = _estimate_text(str(payload.get("text") or ""))
                if inline_images_enabled():
                    total += text_tokens + image_envelope_token_count(payload)
                else:
                    total += text_tokens
                continue
        total += _estimate_text(raw)
    return total


def _tool_calls_of(msg: Any) -> Any:
    return _field(msg, "tool_calls")


def estimate_message_tokens(msg: Any) -> int:
    """Full-field token estimate for one history message."""
    total = 8
    total += _content_cost(msg)
    reasoning = _field(msg, "reasoning_content")
    if reasoning:
        total += _estimate_text(_content_text(reasoning))
    calls = _tool_calls_of(msg)
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict):
                function = call.get("function") or {}
                total += _estimate_text(str(function.get("arguments") or ""))
                total += _estimate_text(str(function.get("name") or ""))
            else:
                total += _estimate_text(str(call))
    elif isinstance(calls, str) and calls.strip():
        total += _estimate_text(calls)
    results = _field(msg, "tool_results")
    if results not in (None, "", [], {}):
        total += _tool_results_cost(msg, results)
    name = _field(msg, "name")
    if name:
        total += _estimate_text(str(name))
    return max(1, total)


def _repair_tool_pairs(messages: list[Any]) -> list[Any]:
    """Drop orphan ``tool`` results left at the front by the cut.

    The window is always a suffix, so the cut can only orphan a tool result at
    the FRONT.  Never pop the tail: the newest message must survive even when
    it is an assistant turn carrying ``tool_calls`` (deathmatch/interrupted
    saves emit that shape).
    """
    out = list(messages)
    while out and _role_of(out[0]) == "tool":
        out.pop(0)
    return out


def cap_tool_results(
    messages: list[dict],
    max_tokens: int,
) -> tuple[list[dict], int]:
    """Head/tail cap a single tool result; ``0`` keeps every message intact."""
    if not max_tokens or int(max_tokens) <= 0:
        return messages, 0
    budget_chars = max(200, int(max_tokens) * 2)
    out: list[dict] = []
    capped = 0
    for msg in messages:
        if isinstance(msg, dict) and _is_inline_image_message(msg):
            from app.services.tool_image_results import parse_image_envelope

            if parse_image_envelope(msg.get("content")) is not None:
                # P0.5：截断 base64 会破坏 JSON 并丢图；图片帧由 compressor
                # 的旧帧占位机制处理，这里原样保留。
                out.append(msg)
                continue
        if (
            isinstance(msg, dict)
            and str(msg.get("role") or "") == "tool"
            and isinstance(msg.get("content"), str)
            and _estimate_text(msg["content"]) > int(max_tokens)
        ):
            content = msg["content"]
            omitted = max(0, len(content) - budget_chars * 2)
            marker = (
                f"\n…[tool output truncated: {omitted} chars omitted; "
                "middle not retained — re-run the tool with narrower "
                "arguments if the full output is needed]…\n"
            )
            out.append({
                **msg,
                "content": content[:budget_chars] + marker + content[-budget_chars:],
            })
            capped += 1
        else:
            out.append(msg)
    return out, capped


@dataclass
class WindowSelection:
    messages: list[Any]
    dropped: list[Any]
    used_tokens: int
    capped_tool_results: int = 0
    mode: str = "legacy"
    floor_applied: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def resolve_history_token_budget(config: Any, window: int | None = None) -> int:
    """P4: auto history budget = ratio × resolved model window (capped).

    ``[agent] conversation_context_max_tokens > 0`` remains an explicit manual
    override (useful for testing); ``0`` now means "model-relative" instead of
    "no budget".  ``window`` comes from the endpoint's declared
    ``extra.context_window`` (see ``agent_loop_context._resolve_context_length``)
    or the ``[agent.compression] context_length`` fallback.
    """
    try:
        explicit = int(getattr(config, "agent_conversation_max_tokens", 0) or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    if not window or int(window) <= 0:
        try:
            window = int(getattr(config, "agent_compression_context_length", 262144))
        except (TypeError, ValueError):
            window = 262144
    try:
        ratio = float(getattr(config, "agent_conversation_context_budget_ratio", 0.45))
    except (TypeError, ValueError):
        ratio = 0.45
    try:
        cap = int(getattr(config, "agent_conversation_context_budget_cap_tokens", 300000))
    except (TypeError, ValueError):
        cap = 300000
    budget = int(max(0, int(window)) * min(1.0, max(0.0, ratio)))
    if cap > 0:
        budget = min(budget, cap)
    return max(8000, budget)


def select_history_window_meta(
    messages: Sequence[Any],
    max_messages: int,
    max_tokens: int = 0,
    *,
    mode: str = "legacy",
    min_messages: int = 0,
) -> WindowSelection:
    msgs = list(messages or [])
    if not msgs:
        return WindowSelection(messages=[], dropped=[], used_tokens=0, mode=mode)
    if mode == "2d":
        return _select_2d(msgs, max_messages, max_tokens, min_messages)
    kept = msgs
    if max_messages and max_messages > 0:
        kept = msgs[-int(max_messages):]
    if not kept or not max_tokens or int(max_tokens) <= 0:
        return WindowSelection(
            messages=kept,
            dropped=msgs[: len(msgs) - len(kept)],
            used_tokens=sum(_content_cost(m) for m in kept),
            mode="legacy",
        )
    budget = int(max_tokens)
    kept = []
    used = 0
    for msg in reversed(msgs[-int(max_messages):] if max_messages else msgs):
        cost = max(1, _content_cost(msg))
        if kept and used + cost > budget:
            break
        kept.append(msg)
        used += cost
    kept.reverse()
    return WindowSelection(
        messages=kept,
        dropped=msgs[: len(msgs) - len(kept)],
        used_tokens=used,
        mode="legacy",
    )


def _select_2d(
    msgs: list[Any],
    max_messages: int,
    max_tokens: int,
    min_messages: int,
) -> WindowSelection:
    floor_n = max(0, int(min_messages or 0))
    window = msgs
    if max_messages and int(max_messages) > 0:
        window = msgs[-int(max_messages):]
    if not max_tokens or int(max_tokens) <= 0:
        kept = _repair_tool_pairs(window)
        return WindowSelection(
            messages=kept,
            dropped=msgs[: len(msgs) - len(kept)],
            # Full-field token counting over restored payloads is expensive and
            # this value has no consumer without a budget; keep it lazy.
            used_tokens=0,
            mode="2d",
        )
    budget = int(max_tokens)
    kept: list[Any] = []
    used = 0
    floor_applied = False
    for index, msg in enumerate(reversed(window)):
        cost = estimate_message_tokens(msg)
        at_floor = len(kept) < floor_n
        if kept and not at_floor and used + cost > budget:
            break
        if at_floor and used + cost > budget:
            floor_applied = True
        kept.append(msg)
        used += cost
    kept.reverse()
    kept = _repair_tool_pairs(kept)
    return WindowSelection(
        messages=kept,
        dropped=msgs[: len(msgs) - len(kept)],
        used_tokens=sum(estimate_message_tokens(m) for m in kept),
        mode="2d",
        floor_applied=floor_applied,
    )


def select_history_window(
    messages: Sequence[Any],
    max_messages: int,
    max_tokens: int = 0,
    *,
    mode: str = "legacy",
    min_messages: int = 0,
) -> list[Any]:
    """Backward-compatible list API used by chat/worker/scheduler."""
    return select_history_window_meta(
        messages, max_messages, max_tokens,
        mode=mode, min_messages=min_messages,
    ).messages


def build_deathmatch_disclosure(dropped_count: int) -> str:
    """Deathmatch cut disclosure (its cross-round summary remains in force)."""
    count = max(0, int(dropped_count or 0))
    return (
        "<context_continuity>\n"

        "已省略 " + str(count) + " 条更早消息（未展示 ≠ 不存在）。"
        "死磕模式的跨轮摘要由死磕管理器注入；本恢复包含计划、已结算、"
        "撤回记录与产物指针；需要精确原文时使用 session_search 或按产物路径重读。\n"
        "</context_continuity>"
    )
