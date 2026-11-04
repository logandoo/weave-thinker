# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Inline interjection boundaries for one assistant turn (2026-11-01).

The agent steers at iteration boundaries: a user interjection becomes a real
user message in the model context, but the transport keeps streaming one
assistant row. To render the interjection where it actually entered the turn,
the transport flushes the text produced so far into ``display_sequence`` /
``content_segments`` and appends an ``interjection`` item. The item is also
copied into ``Message.interjection_items`` (a small sidecar column) so the
frontend can reconstruct the position even when the heavy ``tool_results``
payload is externalized.

Pure helpers live here so the persistence contract is unit-testable without a
live SSE run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# The canonical terminal join separator (chat_stream_transport._finalize_agent_run).
SEGMENT_SEPARATOR = "\n\n<!-- segment_split -->\n\n"
MAX_INTERJECTION_ITEMS = 50


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — matches JavaScript ``String#slice``.

    Python ``len`` counts code points; an emoji/astral char is two JS units.
    The frontend fallback slices ``message.content`` by ``after_char``, so the
    backend must record the same unit the consumer uses.
    """
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    return str(value)


def flush_pending_text(run) -> str:
    """Flush trailing reasoning + assistant text into the display timeline.

    Order is reasoning → text → caller's interjection item, matching the
    live SSE order (thinking streams before the answer). Returns the flushed
    text (empty when there was nothing but whitespace).
    """
    reason = str(getattr(run, "current_reasoning_segment", "") or "").strip()
    if reason:
        run.display_sequence.append({
            "type": "reasoning_step",
            "title": "💭 思考过程",
            "content": reason,
        })
        run.current_reasoning_segment = ""

    text = str(getattr(run, "assistant_content", "") or "")
    # Clear the accumulator even for whitespace: it belongs to the closed
    # segment, and leaving it would duplicate it after the interjection.
    run.assistant_content = ""
    stripped = text.strip()
    if stripped:
        run.content_segments.append(stripped)
        run.display_sequence.append({"type": "text", "content": stripped})
        run.stitch_active = False
        run.stitch_base = ""
        pf = getattr(run, "pf", None)
        if pf is not None and hasattr(pf, "reset"):
            pf.reset()
    return stripped


def prepare_interjection_item(
    run,
    *,
    message_id: str,
    content: str,
    created_at: Any = None,
) -> Dict[str, Any]:
    """Flush pending text and build the position record (does not append)."""
    flush_pending_text(run)
    after_index = len(getattr(run, "display_sequence", []) or [])
    segments = list(getattr(run, "content_segments", []) or [])
    after_char = _utf16_len(SEGMENT_SEPARATOR.join(segments)) if segments else 0
    return {
        "message_id": str(message_id or ""),
        "content": str(content or ""),
        "created_at": _iso(created_at),
        "after_index": after_index,
        "after_char": after_char,
    }


def append_interjection_item(
    run,
    *,
    message_id: str,
    content: str,
    created_at: Any = None,
) -> Dict[str, Any]:
    """Prepare + append the item to the timeline and the run sidecar list."""
    item = prepare_interjection_item(
        run, message_id=message_id, content=content, created_at=created_at,
    )
    _append_prepared(run, item)
    return item


def _append_prepared(run, item: Dict[str, Any]) -> None:
    display_item = {"type": "interjection", **item}
    run.display_sequence.append(display_item)
    items = getattr(run, "interjection_items", None)
    if items is None:
        items = []
        run.interjection_items = items
    items.append(item)
    if len(items) > MAX_INTERJECTION_ITEMS:
        del items[: len(items) - MAX_INTERJECTION_ITEMS]


def commit_prepared_item(run, item: Dict[str, Any]) -> None:
    """Append a prepared item after its DB transaction succeeded."""
    _append_prepared(run, item)


def resolve_persisted_display_sequence(
    sequence: List[dict], agent_steps: List[dict]
) -> List[dict]:
    """Resolve tool_placeholder markers while preserving interjection items.

    Mirrors the terminal persistence resolver (previously inline in
    ``_finalize_agent_run``); interjection items must survive verbatim and in
    position, otherwise a reload loses the inline boundary.
    """
    resolved: List[dict] = []
    step_idx = 0
    for item in list(sequence or []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("text", "reasoning_step", "interjection"):
            resolved.append(item)
        elif item_type == "tool_placeholder" and step_idx < len(agent_steps):
            step = dict(agent_steps[step_idx])
            step["type"] = step.get("step_type", "tool")
            resolved.append(step)
            step_idx += 1
    while step_idx < len(agent_steps):
        step = dict(agent_steps[step_idx])
        step["type"] = step.get("step_type", "tool")
        resolved.append(step)
        step_idx += 1
    return resolved
