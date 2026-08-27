# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Stateful scrubber for reasoning/thinking blocks in streamed assistant text.

Ported (with adaptations) from ``hermes-agent/agent/think_scrubber.py``
(NousResearch/hermes-agent, licensed under the terms of that project) to
solve the same class of bug here: regex-based ``<think>`` stripping that
runs per-delta destroys cross-delta state and lets reasoning leak into
the visible content stream.

The original was used in run_agent's _fire_stream_delta callback.  Here it
replaces the per-delta ``_THINK_RE.sub`` calls in
``backend/app/api/chat.py`` and ``backend/app/services/agent_loop.py``.

Usage::

    scrubber = StreamingThinkScrubber()
    for delta in stream:
        visible = scrubber.feed(delta)
        if visible:
            emit(visible)
    tail = scrubber.flush()  # at end of stream
    if tail:
        emit(tail)

Call ``reset()`` at the top of each new turn so a hung block from an
interrupted prior stream cannot taint the next turn's output.

Tag variants handled (case-insensitive):
  ``<think>``, ``<thinking>``, ``<reasoning>``, ``<thought>``,
  ``<REASONING_SCRATCHPAD>``.

Closed pairs (``<think>X</think>``) are always suppressed regardless of
boundary; a closed pair is an intentional, bounded construct. Unterminated
opens are only treated as block openers when they appear at a block
boundary (start of stream, after newline, or after only whitespace on
the current line) so prose that mentions the tag name isn't suppressed.
"""

from __future__ import annotations

from typing import Tuple

__all__ = ["StreamingThinkScrubber"]


class StreamingThinkScrubber:
    """Stateful scrubber for streaming reasoning/thinking blocks."""

    _OPEN_TAG_NAMES: Tuple[str, ...] = (
        "think",
        "thinking",
        "reasoning",
        "thought",
        "REASONING_SCRATCHPAD",
    )

    _OPEN_TAGS: Tuple[str, ...] = tuple(f"<{name}>" for name in _OPEN_TAG_NAMES)
    _CLOSE_TAGS: Tuple[str, ...] = tuple(f"</{name}>" for name in _OPEN_TAG_NAMES)

    _MAX_TAG_LEN: int = max(len(tag) for tag in _OPEN_TAGS + _CLOSE_TAGS)

    def __init__(self) -> None:
        self._in_block: bool = False
        self._buf: str = ""
        self._last_emitted_ended_newline: bool = True

    def reset(self) -> None:
        self._in_block = False
        self._buf = ""
        self._last_emitted_ended_newline = True

    @property
    def in_block(self) -> bool:
        """True iff currently inside an unterminated reasoning block."""
        return self._in_block

    def feed(self, text: str) -> str:
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_block:
                close_idx, close_len = self._find_first_tag(buf, self._CLOSE_TAGS)
                if close_idx == -1:
                    held = self._max_partial_suffix(buf, self._CLOSE_TAGS)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                buf = buf[close_idx + close_len:]
                self._in_block = False
            else:
                pair = self._find_earliest_closed_pair(buf)
                open_idx, open_len = self._find_open_at_boundary(buf, out)

                if pair is not None and (open_idx == -1 or pair[0] <= open_idx):
                    start_idx, end_idx = pair
                    preceding = buf[:start_idx]
                    if preceding:
                        preceding = self._strip_orphan_close_tags(preceding)
                        if preceding:
                            out.append(preceding)
                            self._last_emitted_ended_newline = preceding.endswith("\n")
                    buf = buf[end_idx:]
                    continue

                if open_idx != -1:
                    preceding = buf[:open_idx]
                    if preceding:
                        preceding = self._strip_orphan_close_tags(preceding)
                        if preceding:
                            out.append(preceding)
                            self._last_emitted_ended_newline = preceding.endswith("\n")
                    self._in_block = True
                    buf = buf[open_idx + open_len:]
                    continue

                held = self._max_partial_suffix(buf, self._OPEN_TAGS)
                held_close = self._max_partial_suffix(buf, self._CLOSE_TAGS)
                held = max(held, held_close)
                if held:
                    emit_text = buf[:-held]
                    self._buf = buf[-held:]
                else:
                    emit_text = buf
                    self._buf = ""
                if emit_text:
                    emit_text = self._strip_orphan_close_tags(emit_text)
                    if emit_text:
                        out.append(emit_text)
                        self._last_emitted_ended_newline = emit_text.endswith("\n")
                return "".join(out)

        return "".join(out)

    def flush(self) -> str:
        if self._in_block:
            self._buf = ""
            self._in_block = False
            return ""
        tail = self._buf
        self._buf = ""
        if not tail:
            return ""
        tail = self._strip_orphan_close_tags(tail)
        if tail:
            self._last_emitted_ended_newline = tail.endswith("\n")
        return tail

    # ── internal helpers ───────────────────────────────────────────────

    @staticmethod
    def _find_first_tag(buf: str, tags: Tuple[str, ...]) -> Tuple[int, int]:
        buf_lower = buf.lower()
        best_idx = -1
        best_len = 0
        for tag in tags:
            idx = buf_lower.find(tag.lower())
            if idx != -1 and (best_idx == -1 or idx < best_idx):
                best_idx = idx
                best_len = len(tag)
        return best_idx, best_len

    def _find_earliest_closed_pair(self, buf: str):
        buf_lower = buf.lower()
        best: "tuple[int, int] | None" = None
        for open_tag, close_tag in zip(self._OPEN_TAGS, self._CLOSE_TAGS):
            open_lower = open_tag.lower()
            close_lower = close_tag.lower()
            open_idx = buf_lower.find(open_lower)
            if open_idx == -1:
                continue
            close_idx = buf_lower.find(close_lower, open_idx + len(open_lower))
            if close_idx == -1:
                continue
            end_idx = close_idx + len(close_lower)
            if best is None or open_idx < best[0]:
                best = (open_idx, end_idx)
        return best

    def _find_open_at_boundary(
        self, buf: str, already_emitted: list[str],
    ) -> Tuple[int, int]:
        buf_lower = buf.lower()
        best_idx = -1
        best_len = 0
        for tag in self._OPEN_TAGS:
            tag_lower = tag.lower()
            search_start = 0
            while True:
                idx = buf_lower.find(tag_lower, search_start)
                if idx == -1:
                    break
                if self._is_block_boundary(buf, idx, already_emitted):
                    if best_idx == -1 or idx < best_idx:
                        best_idx = idx
                        best_len = len(tag)
                    break
                search_start = idx + 1
        return best_idx, best_len

    def _is_block_boundary(
        self, buf: str, idx: int, already_emitted: list[str],
    ) -> bool:
        if idx == 0:
            if already_emitted:
                return already_emitted[-1].endswith("\n")
            return self._last_emitted_ended_newline
        preceding = buf[:idx]
        last_nl = preceding.rfind("\n")
        if last_nl == -1:
            if already_emitted:
                prior_newline = already_emitted[-1].endswith("\n")
            else:
                prior_newline = self._last_emitted_ended_newline
            return prior_newline and preceding.strip() == ""
        return preceding[last_nl + 1:].strip() == ""

    @classmethod
    def _max_partial_suffix(cls, buf: str, tags: Tuple[str, ...]) -> int:
        if not buf:
            return 0
        buf_lower = buf.lower()
        max_check = min(len(buf_lower), cls._MAX_TAG_LEN - 1)
        for i in range(max_check, 0, -1):
            suffix = buf_lower[-i:]
            for tag in tags:
                tag_lower = tag.lower()
                if len(tag_lower) > i and tag_lower.startswith(suffix):
                    return i
        return 0

    @classmethod
    def _strip_orphan_close_tags(cls, text: str) -> str:
        if "</" not in text:
            return text
        text_lower = text.lower()
        out: list[str] = []
        i = 0
        while i < len(text):
            matched = False
            if text_lower[i:i + 2] == "</":
                for tag in cls._CLOSE_TAGS:
                    tag_lower = tag.lower()
                    tag_len = len(tag_lower)
                    if text_lower[i:i + tag_len] == tag_lower:
                        j = i + tag_len
                        while j < len(text) and text[j] in " \t\n\r":
                            j += 1
                        i = j
                        matched = True
                        break
            if not matched:
                out.append(text[i])
                i += 1
        return "".join(out)
