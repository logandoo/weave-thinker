# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice ASR helpers (P5.5 move-only, 2026-10-18)."""
from __future__ import annotations

import re


def _build_funasr_context_payload(
    history: list[dict], max_turns: int = 3, max_chars_per_turn: int = 400
) -> list[dict]:
    """Convert voice conversation history into fun-asr-realtime ``context``.

    DashScope requires the input list to interleave ``user`` (input_text) and
    ``assistant`` (text) messages, with user before assistant in each turn, a
    per-role cap of 5 messages, and a per-turn text cap of 400 chars. History
    messages with tool roles or empty content are skipped so the payload
    reflects only the user/assistant dialogue — exactly what the model uses
    to bias recognition toward the current topic and away from background
    speech.
    """
    if max_turns <= 0 or not history:
        return []
    # Keep only plain user/assistant turns (skip tool/system messages).
    plain: list[dict] = [
        m for m in history
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and m["content"].strip()
    ]
    # Take the trailing window (most recent first), then reverse for chrono order.
    window = list(reversed(plain))[: max_turns * 2]
    window.reverse()
    # Group into turns: a user message followed by its assistant reply (if any).
    # DashScope wants user BEFORE assistant within a turn.
    out: list[dict] = []
    i = 0
    while i < len(window):
        m = window[i]
        if m["role"] == "user":
            text = m["content"].strip()[:max_chars_per_turn]
            out.append({
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            })
            # Look ahead for the matching assistant reply.
            if i + 1 < len(window) and window[i + 1]["role"] == "assistant":
                atext = window[i + 1]["content"].strip()[:max_chars_per_turn]
                out.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": atext}],
                })
                i += 2
            else:
                i += 1
        elif m["role"] == "assistant":
            # Orphaned assistant message (history starts mid-turn) — still emit.
            atext = m["content"].strip()[:max_chars_per_turn]
            out.append({
                "role": "assistant",
                "content": [{"type": "text", "text": atext}],
            })
            i += 1
        else:
            i += 1
    # Enforce the per-role cap of 5 (keep the most recent).
    users = [m for m in out if m["role"] == "user"]
    assts = [m for m in out if m["role"] == "assistant"]
    if len(users) > 5 or len(assts) > 5:
        # Rebuild preserving order while trimming older entries.
        keep_u = set(id(m) for m in users[-5:])
        keep_a = set(id(m) for m in assts[-5:])
        out = [
            m for m in out
            if (m["role"] == "user" and id(m) in keep_u)
            or (m["role"] == "assistant" and id(m) in keep_a)
        ]
    return out

_PUNCT_WS_RE = re.compile(r"[，。！？；、…—～~,.!?;:、\"'“”‘’()（）【】\[\]·\s]+")

def _char_bigrams(s: str) -> set:
    s = _PUNCT_WS_RE.sub("", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}

def _is_likely_echo(text: str, spoken: str) -> bool:
    """Deterministic acoustic-echo guard for full-duplex playback.

    While the agent speaks, the mic picks up its OWN TTS voice and ASR
    transcribes that echo as text. Such an interjection is not a real user
    turn and must never interrupt playback (the #1 cause of mid-playback
    truncation). Echo text substantially overlaps what was just spoken, so we
    detect it by character-bigram overlap with the currently-spoken text. This
    is an acoustic-echo-cancellation-equivalent heuristic (like the existing
    ``_ASR_NOISE_RE`` pre-filter), NOT a semantic routing classifier — it only
    ever suppresses the agent's own echoed voice, never a genuine user message.
    """
    t = _PUNCT_WS_RE.sub("", text or "")
    s = _PUNCT_WS_RE.sub("", spoken or "")
    if not t or not s:
        return False
    # Clean containment (most echo is an almost-verbatim slice of the speech).
    if len(t) >= 2 and t in s:
        return True
    tb = _char_bigrams(t)
    if not tb:
        return False
    sb = _char_bigrams(s)
    overlap = len(tb & sb) / len(tb)
    return overlap >= 0.6
