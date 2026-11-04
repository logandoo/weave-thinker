# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice turn-taking helpers (P5.5 move-only, 2026-10-18)."""
from __future__ import annotations

from typing import Optional

from app.services.voice_text import _norm_barge_compare, _utterance_complete


def _prox_is_near_signal(seen: bool, near: bool, updated: float, stale_sec: float, now: float) -> bool:
    """Acoustic near-field gate decision (pure, testable).

    The browser classifies its mic input as near-field (user close to the
    mic — almost certainly the user's own voice) vs far-field (environment
    speech picked up at a distance — TV/room conversation). Far-field speech
    must never pause playback nor interrupt the current answer (the "背景有
    点语音就会被打断" bug family, conv 689f06ec).

    * Unknown client (no ``audio_proximity`` signal received yet): True —
      old behavior preserved for older frontends/headless test clients.
    * seen + far → False regardless of freshness.
    * seen + near → True only while fresh; a stale near signal expires to
      False (the client stopped reporting — treat as far, be conservative).
    """
    if not seen:
        return True
    if not near:
        return False
    return (now - updated) < stale_sec

def _pre_classify_reusable(pre: Optional[dict], text: str) -> bool:
    """Whether the onset-pause pre-classify verdict may be reused for the
    EoT-flushed utterance *text*.

    The pre-classify fires on the FIRST ASR partial (often mid-utterance).
    Reusing its verdict on a flushed INCOMPLETE fragment is unsafe: the LLM
    judged a truncated slice (e.g. "做一个" — the start of a real command the
    user was still voicing, or background speech) and the pause path applied
    it as a full interrupt, killing the in-flight answer (conv 689f06ec
    12:48:11: pre-classify=interrupt on the fragment "做一个" aborted the
    real answer). Only a COMPLETE utterance (terminal punctuation) carries
    enough meaning for the verdict to survive the flush — incomplete text
    always re-classifies on the full utterance instead.
    """
    if not pre or not pre.get("action"):
        return False
    if _norm_barge_compare(pre.get("text", "")) != _norm_barge_compare(text):
        return False
    return _utterance_complete(text)

def _prox_evidence_line(near: bool) -> str:
    """Acoustic evidence injected into the barge-in classifier prompt: the
    LLM sees whether the utterance was picked up near-field (user close to
    the mic — likely the user's own voice) or far-field (environment/
    background speech), which helps distinguish "对助手说的" from "房间里
    的声音". The signal is a loudness heuristic, so the wording stays
    suggestive (提示) rather than assertive."""
    if near:
        return ("声学拾音证据：这段语音的声学特征**提示**为近场拾音"
                "（说话人贴近麦克风，很可能是用户本人对着手机说的）。")
    return ("声学拾音证据：这段语音的声学特征**提示**为远场拾音"
            "（麦克风从环境中拾取的声音，很可能是背景人声/电视/他人对话，"
            "而非用户本人对着手机说话）。")

def _is_voice_rate_limit(exc: Exception) -> bool:
    """True when the exception is a provider rate-limit error (openai SDK
    raises RateLimitError for 429s; string-matching keeps the check robust
    across SDK versions)."""
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate_limit" in text
