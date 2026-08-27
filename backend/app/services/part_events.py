# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Part event protocol (F1-1, agent_improve.md §五).

Translates the legacy single-key AgentLoop event dicts (content /
reasoning_content / tool_call / tool_result / agent_step / ...) into the
opencode-aligned three-class part protocol:

  part_started  {part_id, part_type, ...snapshot fields}   -> push
  part_delta    {part_id, part_type, field, delta}         -> append (not persisted)
  part_updated  {part_id, ...mutated fields}               -> mutate / correct

Part ids are ULID-style lexicographically sortable strings
(`prt_<ms:013d>_<seq:05d>`) generated with stdlib only — monotonic within a
stream so frontends can binary-search / map-lookup by id.

The translator also stamps the incoming legacy event with
`_part_stamped`/`_part_id` so downstream accumulators (StreamBuffer) can skip
their own display_sequence mutation for events the part channel now covers,
while old clients keep working unchanged (part events are purely additive).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class PartIdGen:
    """Monotonic, lexicographically sortable part id generator.

    Format: ``prt_<13-digit-ms>_<5-digit-seq>``. Fixed-width fields make plain
    string sorting equivalent to chronological ordering, including multiple
    ids issued inside the same millisecond (seq increments).
    """

    __slots__ = ("_last_ms", "_seq")

    def __init__(self) -> None:
        self._last_ms = 0
        self._seq = 0

    def new_id(self) -> str:
        ms = int(time.time() * 1000)
        if ms > self._last_ms:
            self._last_ms = ms
            self._seq = 0
        self._seq += 1
        return f"prt_{self._last_ms:013d}_{self._seq:05d}"


class PartTranslator:
    """Per-stream state machine converting legacy events to part events.

    One instance per conversation stream (created in chat.py `_put` scope).
    A single "stream part" (text or reasoning) is open at a time; a different
    part starting implicitly closes the previous one — matching the existing
    last-item-type heuristic but with explicit, stable identities.
    """

    def __init__(self) -> None:
        self._gen = PartIdGen()
        # Currently open streaming part: (part_id, part_type) where part_type
        # is "text" or "reasoning".
        self._open_stream_part: Optional[tuple[str, str]] = None
        # Most recent reasoning part id — a reasoning_segment arriving at a
        # tool-call boundary must finalize THIS part when the open stream
        # part is no longer reasoning (a text part opened in between), never
        # push a duplicate block carrying the full accumulated thinking
        # (user report 2026-08-20: thinking displayed twice after tool call).
        self._last_reasoning_part: Optional[str] = None
        # call_id -> part_id for tool parts (pending/running/completed/error
        # lifecycle lives in one array slot, opencode-style).
        self._tool_parts: Dict[str, str] = {}

    def _close_stream_part(self) -> None:
        self._open_stream_part = None

    def _open_or_continue(self, part_type: str) -> tuple[List[Dict[str, Any]], str]:
        """Return (events, part_id) for a streaming text/reasoning channel."""
        events: List[Dict[str, Any]] = []
        if self._open_stream_part and self._open_stream_part[1] == part_type:
            return events, self._open_stream_part[0]
        part_id = self._gen.new_id()
        self._open_stream_part = (part_id, part_type)
        events.append({"part_started": {"part_id": part_id, "part_type": part_type}})
        return events, part_id

    def translate(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Translate one legacy event dict into 0+ part events.

        Mutates the legacy event in place with `_part_stamped`/`_part_id` when
        it produced part events, so StreamBuffer can skip its legacy
        display_sequence mutation for exactly these events.
        """
        out: List[Dict[str, Any]] = []

        if "content" in event:
            events, part_id = self._open_or_continue("text")
            events.append({"part_delta": {
                "part_id": part_id, "part_type": "text",
                "field": "content", "delta": event["content"],
            }})
            event["_part_id"] = part_id
            out.extend(events)
        elif "reasoning_content" in event:
            events, part_id = self._open_or_continue("reasoning")
            self._last_reasoning_part = part_id
            events.append({"part_delta": {
                "part_id": part_id, "part_type": "reasoning",
                "field": "content", "delta": event["reasoning_content"],
            }})
            event["_part_id"] = part_id
            out.extend(events)
        elif "tool_call" in event:
            self._close_stream_part()
            tc = event["tool_call"]
            part_id = self._gen.new_id()
            call_id = tc.get("call_id")
            if call_id:
                self._tool_parts[call_id] = part_id
            out.append({"part_started": {
                "part_id": part_id,
                "part_type": "tool_call",
                "call_id": call_id,
                "name": tc.get("name"),
                "arguments": tc.get("arguments") or {},
                "status": "running",
            }})
            event["_part_id"] = part_id
        elif "tool_result" in event:
            tr = event["tool_result"]
            call_id = tr.get("call_id")
            part_id = self._tool_parts.get(call_id) if call_id else None
            updated: Dict[str, Any] = {
                "part_id": part_id,
                "call_id": call_id,
                "name": tr.get("name"),
                "status": "error" if tr.get("error") else "completed",
                "result": tr.get("result"),
                "error": bool(tr.get("error")),
            }
            out.append({"part_updated": updated})
            if part_id:
                event["_part_id"] = part_id
        elif "agent_step" in event:
            self._close_stream_part()
            step = event["agent_step"]
            part_id = self._gen.new_id()
            out.append({"part_started": {
                "part_id": part_id,
                "part_type": step.get("step_type") or "agent_step",
                **step,
            }})
            event["_part_id"] = part_id
        elif "reasoning_segment" in event:
            seg = event["reasoning_segment"] or {}
            title = seg.get("title") or "💭 思考过程"
            if self._open_stream_part and self._open_stream_part[1] == "reasoning":
                # The segment content was already streamed live as deltas into
                # the open reasoning part — finalize THAT slot instead of
                # pushing a duplicate part (double-render). The content field
                # is deliberately OMITTED: the segment carries the reasoning
                # accumulated since the last tool-call boundary, which can
                # span multiple reasoning runs (r1 → text → r2 → tool) and
                # would re-print r1 inside r2's block.
                part_id = self._open_stream_part[0]
                self._close_stream_part()
                out.append({"part_updated": {
                    "part_id": part_id,
                    "title": title,
                }})
                event["_part_id"] = part_id
            elif self._last_reasoning_part:
                # A text part (pre-tool content released by the hold cap, or
                # the coordinator did not expect tools) opened between the
                # reasoning deltas and this tool-call boundary. The reasoning
                # was ALREADY streamed live into a reasoning part — finalize
                # that part instead of starting a NEW reasoning_step part
                # carrying the full accumulated thinking (that duplicate block
                # is the user-visible bug). No content field for the same
                # multi-run accumulation reason as above.
                part_id = self._last_reasoning_part
                out.append({"part_updated": {
                    "part_id": part_id,
                    "title": title,
                }})
                event["_part_id"] = part_id
            else:
                # No reasoning deltas were ever streamed live (legacy /
                # defensive): emit the self-contained reasoning_step part.
                self._close_stream_part()
                part_id = self._gen.new_id()
                self._last_reasoning_part = part_id
                out.append({"part_started": {
                    "part_id": part_id,
                    "part_type": "reasoning_step",
                    "title": title,
                    "content": seg.get("content") or "",
                }})
                event["_part_id"] = part_id
        elif "content_segment" in event:
            seg = event["content_segment"] or ""
            if self._open_stream_part and self._open_stream_part[1] == "text":
                # Same double-render guard as reasoning_segment: finalize the
                # open text part in place rather than pushing a second copy.
                part_id = self._open_stream_part[0]
                self._close_stream_part()
                out.append({"part_updated": {"part_id": part_id, "content": seg}})
                event["_part_id"] = part_id
            else:
                self._close_stream_part()
                part_id = self._gen.new_id()
                out.append({"part_started": {
                    "part_id": part_id, "part_type": "text", "content": seg,
                }})
                event["_part_id"] = part_id
        else:
            # ping / done / error / iteration / deathmatch_verdict /
            # permission_request / compression / attachments — channel-level,
            # not timeline parts.
            return out

        if out:
            event["_part_stamped"] = True
        return out
