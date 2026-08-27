# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""PreToolGate — bounded hold for pre-tool prose on tool-requiring turns.

Why it exists: on turns the coordinator classifies as ``route=tool_loop`` +
``expects_tools=True``, content the model emits BEFORE its first tool call is
transient — it is dropped from the persisted answer (chat.py resets its
accumulators at the tool_call boundary), and streaming it live made users see
a "first answer" that later vanished or duplicated the real one (conv
149ce886: the model claimed "我进行了真实检索" before the auto-invoked
web_search).

What it holds: content chunks go into ``held`` instead of the SSE stream.
The hold is released by one of:
  * ``on_tool_call`` — a real tool call arrived: pre-tool prose is stale,
    drop it and stream everything after.
  * ``on_iteration_done(tool_calls>0)`` — a tool iteration ended (safety net
    for tool_call events dropped by a slow client's subscriber-queue
    overflow): same drop semantics.
  * ``on_iteration_done(tool_calls=0)`` — an iteration ended WITHOUT a tool
    call: the held text is the actual answer, return it to be streamed.
  * ``on_content`` hold-cap — no tool call arrived within
    ``HOLD_MAX_SECONDS`` of the first held chunk: return everything held and
    disarm, so the rest of the answer streams live (bounded pop instead of a
    whole-turn silent hold).
  * ``flush`` — terminal fallback at stream end (``done``).

Regression it fixes (2026-08-06, user report): a turn where the coordinator
over-predicted tools but the model answered directly (common on multi-turn
follow-ups) previously held the WHOLE answer with no disarm path other than
the tool_call event — thinking streamed, then a long silence (pre-send
audit), then the entire answer popped in ONE SSE event at ``done``.
"""
from __future__ import annotations


class PreToolGate:
    """State machine for the relay's pre-tool text hold (see module doc)."""

    #: Max seconds a content chunk may sit in the hold without a tool call.
    #: Tool calls arrive 1-3s after pre-tool prose in the same provider
    #: stream, so 6s never fires for genuine tool turns, while a no-tool
    #: answer is released (and the rest streams live) well before the turn
    #: ends.
    HOLD_MAX_SECONDS = 6.0

    def __init__(self) -> None:
        self.armed = False
        self.held = ""
        self._hold_started = 0.0
        # True when the hold cap released the gate (held prose was streamed).
        # chat.py uses it to keep the already-streamed prose in the persisted
        # answer if a tool_call arrives afterwards — the user saw that text,
        # so it must not vanish from the saved message (A4.9 I2).
        self.cap_released = False

    @property
    def holding(self) -> bool:
        return self.armed

    def arm(self, route: str, expects_tools: bool) -> None:
        """Arm the gate when the coordinator predicts a tool round."""
        self.armed = bool(route == "tool_loop" and expects_tools is True)
        if not self.armed:
            self.held = ""
            self._hold_started = 0.0
        self.cap_released = False

    def on_content(self, chunk: str, now: float) -> str:
        """Absorb a content chunk while armed.

        Returns the text the caller must STREAM instead (empty when the
        chunk was absorbed into the hold). When the hold cap expires, the
        entire held text is returned and the gate disarms — from here on
        every chunk streams live.
        """
        if not self.armed:
            return chunk
        if not self._hold_started:
            self._hold_started = now
        self.held += chunk
        if now - self._hold_started >= self.HOLD_MAX_SECONDS:
            # Inline disarm: `_clear` would reset cap_released.
            text = self.held
            self.armed = False
            self.held = ""
            self._hold_started = 0.0
            self.cap_released = True
            return text
        return ""

    def on_tool_call(self) -> None:
        """A real tool call arrived — pre-tool prose is stale, drop it."""
        self._clear()

    def on_iteration_done(self, tool_calls: int, now: float) -> str:
        """Iteration boundary with no tool_call event in between.

        Returns held text to stream when the iteration had NO tool calls
        (the held text is the answer); drops the hold when tools ran (the
        held text is stale pre-tool prose). Disarms in both cases.
        """
        if not self.armed:
            return ""
        was_held = self.held
        self._clear()
        if tool_calls:
            return ""
        return was_held

    def flush(self) -> str:
        """Terminal fallback (stream end): return everything held, disarm."""
        if not self.armed:
            return ""
        return self._disarm_and_flush()

    def reset(self) -> None:
        """Hard reset (compression / response-audit regeneration)."""
        self._clear()

    def _clear(self) -> None:
        self.armed = False
        self.held = ""
        self._hold_started = 0.0
        self.cap_released = False

    def _disarm_and_flush(self) -> str:
        text = self.held
        self._clear()
        return text
