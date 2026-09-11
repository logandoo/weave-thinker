# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Structured tool-call history persistence and replay (Phase 2B).

Multi-turn conversations were degrading at turn 5-6 because the LLM saw only
opaque narrative text in history (e.g. "我搜索了 X 找到了 Y") and lost the
structured ``tool_calls`` ↔ ``tool`` message pattern. Without that pattern
the model started skipping tool calls — the same failure mode hermes-agent
PR #3528 addressed.

This module:
  - ``build_persisted_tool_calls(events)`` — convert per-iteration tool_call
    events collected during a turn into the OpenAI-compatible JSON blob
    stored on ``Message.tool_calls``.
  - ``rebuild_structured_history(message)`` — given a persisted assistant
    Message row, yield structured ``{role: assistant, tool_calls: [...]}``
    + ``{role: tool, tool_call_id, content}`` + final assistant content
    messages for replay.
  - ``sanitize_api_messages(messages)`` — repair orphan assistant.tool_calls
    (no matching tool reply) and stray tool messages (no preceding
    assistant.tool_calls) so the OpenAI client doesn't reject the request.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional

from app.services.citation_ledger import _CITE_RE, _FENCE_RE

logger = logging.getLogger(__name__)

__all__ = [
    "build_persisted_tool_calls",
    "rebuild_structured_history",
    "sanitize_api_messages",
    "neutralize_historical_citations",
    "build_history_message",
]

_ORPHAN_RESULT_STUB = "[Result unavailable — see context summary above]"


def neutralize_historical_citations(text: Optional[str]) -> str:
    """Remove ``[N]`` citation markers from HISTORICAL assistant prose before
    the text re-enters the model context.

    Why (conv a040c24e, 2026-09-10): each turn's CitationLedger restarts at 1,
    but prior turns' ``[N]`` markers replayed verbatim into the prompt. The
    model then reused two-turns-old numbers — in-range collisions resolve to
    unrelated URLs (frontend maps positionally), out-of-range ones dangle.
    The ids are unresolvable outside their own turn, so the markers carry no
    actionable information for a new turn; deleting them restores the ledger
    invariant ("the model only ever emits small integers it was handed" —
    the current turn's).

    Why REMOVAL and not a replacement glyph (2026-09-10 post-deploy smoke,
    conv b650d7c2): the first version rewrote ``[N]`` → full-width ``（N）``
    to keep enumeration prose readable — the model MIMICKED the full-width
    form in its own new answer ("吹不到手 （6）（10）", persisted without
    badges). Any number-preserving replacement is imitable; removal is the
    only non-imitable transform. Enumeration numbers in replayed history
    ("[3] 个要点") lose the digit but keep the prose — replay-context-only,
    never persisted, never user-visible.

    Code fences / inline code are masked (``a[1]`` inside code survives).
    Replay-time only — persisted rows are never mutated.
    """
    if not text:
        return ""
    spans = list(_FENCE_RE.finditer(text))
    if not spans:
        return _CITE_RE.sub("", text)
    parts: List[str] = []
    last = 0
    for m in spans:
        parts.append(_CITE_RE.sub("", text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_CITE_RE.sub("", text[last:]))
    return "".join(parts)


def build_history_message(
    role: str,
    content: Optional[str],
    reasoning_content: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one replay message for LLM context from a persisted row.

    Single seam for the role-conditional citation neutralization used by the
    chat / background-worker / scheduler replay loops (A4.9 R1 M3): assistant
    rows get ``neutralize_historical_citations``; user/other roles pass
    through byte-identical. ``reasoning_content`` is attached only when
    truthy (DeepSeek thinking round-trip invariant lives on tool-call rows,
    which ``rebuild_structured_history`` handles separately).
    """
    msg: Dict[str, Any] = {"role": role, "content": content or ""}
    if role == "assistant":
        msg["content"] = neutralize_historical_citations(msg["content"])
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
    return msg


# A formatted search-list entry header ("12. <title>") is de-numbered when its
# BLOCK (entries are separated by blank lines per CitationLedger.format_hits)
# contains a "URL: " line — the structural signature of a real search entry.
# Block-level (not next-line) signature so multi-line titles are covered
# (A4.9 R1 M4). Digit-leading lines inside snippets lack the signature and
# stay untouched.
_SEARCH_ENTRY_HEADER_RE = re.compile(r"^\d{1,3}\. ")


def _denumber_search_formatted(content: Optional[str]) -> str:
    """De-number a historical web_search ``formatted`` result list.

    Entries ``"N. title\\nURL: url\\n摘要: snippet"`` become ``"- title\\n…"``
    so replayed history carries its facts (titles/URLs/snippets intact) but
    none of its stale ledger integers (conv a040c24e). Deleting the number
    is safe because historical ids are unresolvable outside their own turn.
    """
    if not content:
        return content or ""
    out_blocks: List[str] = []
    for block in content.split("\n\n"):
        lines = block.split("\n")
        if (
            lines
            and _SEARCH_ENTRY_HEADER_RE.match(lines[0])
            and any(ln.startswith("URL: ") for ln in lines[1:])
        ):
            lines[0] = "- " + lines[0].split(". ", 1)[1]
        out_blocks.append("\n".join(lines))
    return "\n\n".join(out_blocks)


def _unique_tool_call_id() -> str:
    """Generate a unique id for a tool call when deduplicating history."""
    return f"fix_{uuid.uuid4().hex[:8]}"


def build_persisted_tool_calls(events: Iterable[Dict[str, Any]]) -> Optional[str]:
    """Serialize a sequence of agent-loop ``tool_call`` events into the
    JSON blob persisted on ``Message.tool_calls``.

    Each event is ``{call_id, name, arguments(parsed dict)}`` as emitted by
    ``AgentLoop._run_loop``. The output is an OpenAI-style list:

        [{"id", "type":"function", "function":{"name", "arguments": <str>}}, ...]

    Returns ``None`` if the events list is empty.
    """
    items: List[Dict[str, Any]] = []
    for ev in events:
        call_id = ev.get("call_id") or ev.get("id")
        name = ev.get("name") or ev.get("function", {}).get("name")
        if not call_id or not name:
            continue
        args = ev.get("arguments")
        if isinstance(args, dict):
            args_str = json.dumps(args, ensure_ascii=False)
        elif args is None:
            args_str = "{}"
        else:
            args_str = str(args)
        items.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": args_str},
        })
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)


def _extract_tool_result_content_by_id(tool_results_json: Optional[str]) -> Dict[str, str]:
    """Index persisted tool_results JSON by call_id → result content.

    The tool_results blob produced by ``_transform_tool_loop_results`` is a
    legacy frontend-display shape with ``agent_steps[]`` (each having
    ``name`` = call_id where available, ``content`` = result snippet).
    """
    if not tool_results_json:
        return {}
    try:
        data = json.loads(tool_results_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    out: Dict[str, str] = {}
    for step in data.get("agent_steps", []) or []:
        if step.get("step_type") not in ("tool", "tool_call"):
            continue
        cid = step.get("name") or step.get("call_id")
        if not cid:
            continue
        out[cid] = step.get("content") or ""
    return out


def rebuild_structured_history(
    role: str,
    content: Optional[str],
    tool_calls_json: Optional[str],
    tool_results_json: Optional[str],
    reasoning_content: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Rebuild OpenAI-compatible structured messages for a single persisted
    Message row.

    For assistant rows with tool_calls, the result is:

        [
          {"role": "assistant", "content": <stripped content>, "tool_calls": [...]},
          {"role": "tool", "tool_call_id": <id>, "name": <name>, "content": <result>},
          ...
        ]

    For user/system/assistant-without-tools rows, returns a single message.
    Orphan call_ids (no matching tool_result) get the ``_ORPHAN_RESULT_STUB``
    so the assistant.tool_calls / tool message pairing remains intact.
    """
    if role != "assistant" or not tool_calls_json:
        return [build_history_message(role, content, reasoning_content)]

    try:
        tool_calls = json.loads(tool_calls_json)
        if not isinstance(tool_calls, list) or not tool_calls:
            return [build_history_message("assistant", content, reasoning_content)]
    except (json.JSONDecodeError, TypeError):
        return [build_history_message("assistant", content, reasoning_content)]

    result_by_id = _extract_tool_result_content_by_id(tool_results_json)

    # Strip the inter-segment marker that the chat handler embeds between
    # text emitted before vs. after tool calls.
    cleaned_content = (content or "").replace("<!-- segment_split -->", "").strip()
    cleaned_content = neutralize_historical_citations(cleaned_content)

    assistant_msg: Dict[str, Any] = {
        "role": "assistant",
        "content": cleaned_content,
        "tool_calls": tool_calls,
    }
    # DeepSeek thinking mode: the assistant tool-call message MUST carry its
    # reasoning_content in every subsequent request, or the API rejects the
    # payload with a 400. Emit the key unconditionally (empty string when the
    # row predates reasoning persistence) so old conversations stay valid.
    assistant_msg["reasoning_content"] = reasoning_content or ""
    messages: List[Dict[str, Any]] = [assistant_msg]
    for tc in tool_calls:
        cid = tc.get("id", "")
        name = tc.get("function", {}).get("name", "tool")
        result_content = result_by_id.get(cid)
        if result_content is None:
            logger.debug(
                "rebuild_structured_history: no tool_result for call_id=%s, using orphan stub",
                cid,
            )
            result_content = _ORPHAN_RESULT_STUB
        elif name == "web_search":
            # Historical search lists carry their own turn's ledger ids —
            # strip the numbers so the model can't re-cite them (a040c24e).
            result_content = _denumber_search_formatted(result_content)
        messages.append({
            "role": "tool",
            "tool_call_id": cid,
            "name": name,
            "content": result_content,
        })
    return messages


def _deduplicate_tool_call_ids(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite duplicate tool_call_ids so the API does not reject the request.

    Historical messages may contain duplicate ids (e.g. older DSML parsing used a
    per-turn index that reset every assistant message). This pass assigns a
    unique id to every duplicated tool call and updates the matching tool result
    messages so the assistant/tool pairs stay consistent.
    """
    used_ids: set = set()
    result: List[Dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_calls = list(msg.get("tool_calls") or [])
            block_map: Dict[str, List[str]] = {}
            new_tool_calls: List[Dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    new_tool_calls.append(tc)
                    continue
                cid = tc.get("id", "")
                if not cid:
                    new_tool_calls.append(tc)
                    continue
                if cid in used_ids:
                    new_id = _unique_tool_call_id()
                    block_map.setdefault(cid, []).append(new_id)
                    tc = {**tc, "id": new_id}
                    used_ids.add(new_id)
                else:
                    used_ids.add(cid)
                new_tool_calls.append(tc)
            result.append({**msg, "tool_calls": new_tool_calls})
            i += 1
            # Rewrite following tool messages that belong to this block.
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                tcid = tm.get("tool_call_id", "")
                new_tcid = tcid
                if tcid and tcid in block_map and block_map[tcid]:
                    new_tcid = block_map[tcid].pop(0)
                    result.append({**tm, "tool_call_id": new_tcid})
                else:
                    result.append(tm)
                if new_tcid:
                    used_ids.add(new_tcid)
                i += 1
        else:
            result.append(msg)
            i += 1
    return result


def sanitize_api_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Repair orphan assistant.tool_calls / stray tool messages so the
    OpenAI client doesn't 400.

    Rules (matching hermes-agent ``_sanitize_api_messages``):
      - Every assistant message with ``tool_calls`` must be followed by one
        ``tool`` message per call_id. Missing tool responses are stubbed.
      - A ``tool`` message with no preceding assistant.tool_calls in the
        sequence is dropped.
    """
    # First remove any duplicate tool_call_ids that could trigger
    # "Duplicate value for 'tool_call_id'" errors from the API.
    messages = _deduplicate_tool_call_ids(messages)
    out: List[Dict[str, Any]] = []
    pending_call_ids: List[str] = []

    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            cid = msg.get("tool_call_id", "")
            if cid in pending_call_ids:
                pending_call_ids.remove(cid)
                out.append(msg)
            else:
                logger.debug("sanitize_api_messages: dropping orphan tool msg call_id=%s", cid)
                continue
        elif role == "assistant" and msg.get("tool_calls"):
            # Flush remaining unfilled pending pairs from a prior assistant
            # block before starting a new one — fill with orphan stubs.
            for cid in pending_call_ids:
                out.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": "tool",
                    "content": _ORPHAN_RESULT_STUB,
                })
            pending_call_ids = [tc.get("id", "") for tc in msg["tool_calls"] if tc.get("id")]
            out.append(msg)
        else:
            for cid in pending_call_ids:
                out.append({
                    "role": "tool",
                    "tool_call_id": cid,
                    "name": "tool",
                    "content": _ORPHAN_RESULT_STUB,
                })
            pending_call_ids = []
            out.append(msg)

    for cid in pending_call_ids:
        out.append({
            "role": "tool",
            "tool_call_id": cid,
            "name": "tool",
            "content": _ORPHAN_RESULT_STUB,
        })
    return out
