# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice clock helpers (P5.5 move-only, 2026-10-18)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _local_time_line() -> str:
    """Explicit UTC+8 clock line — single format source for the system-prompt
    injection and the per-query marker. The timezone is DELIBERATE: a bare
    now() drifts on non-CST deployments and a "北京时间" label would lie
    (2026-08-25 review I1). Reads the clock per call so long sessions never
    serve a stale timestamp."""
    # Keep the historical patch point: tests patch ``voice_service.datetime``;
    # resolving it lazily preserves that contract after the move.
    from app.services import voice_service as _vs

    now = _vs.datetime.now(_vs.timezone(_vs.timedelta(hours=8)))
    return f"当前时间: {now.strftime('%Y年%m月%d日 %H:%M:%S')} (北京时间, {now.strftime('%A')})"


def _stamp_query_time(messages: list[dict]) -> list[dict]:
    """Append the current-time marker to the LAST user message (the query the
    model is about to answer) so every voice turn carries its own local time
    and the model can perceive time passing across turns (2026-09-06).

    The messages list may share dicts with ``_history`` (``_trimmed_history``
    is a shallow copy) — the stamped entry is a NEW dict; the original is
    never mutated, so persistence/UI semantics are untouched."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].strip():
            stamped = dict(m)
            stamped["content"] = f"{m['content']}\n[{_local_time_line()}]"
            messages[i] = stamped
            break
    return messages
