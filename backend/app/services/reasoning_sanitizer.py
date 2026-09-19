# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic display hygiene for user-visible reasoning.

The thinking panel is user-visible. Prompt rules forbid narrating internal
machinery ("系统提示词/system prompt/内部规则/协调器注记/identity rules"), but
models still do it — especially while resolving conflicts (conv 3583d840
follow-up: "Per identity rules, I should…", "the coordinator note says…",
"注意不要提及系统提示词"). This module is the same class of mechanical guard
as canary stripping (`strip_canary_streaming`): matched phrases are replaced
with a neutral placeholder before the reasoning chunk is streamed/persisted.

`scrub_reasoning_streaming` holds back a small tail so a phrase split across
deltas can never leak; flush the returned tail through
`scrub_reasoning_meta` at the end of the stream / segment.
"""
import re

_PLACEHOLDER = "（内部信息）"

# Longer alternatives first (leftmost-first matching): 内部规则编号 before
# 内部规则, 系统提示词 before 系统提示. A4.9 R1 review: also the paraphrased
# attributions ("Per rules", "The rule says", 按规则/根据规则/规则说) — the
# harm class is "narrating the rulebook", not one literal phrase.
_META_RE = re.compile(
    r"(?i)("
    r"coordinator\s*note|identity\s*rules?|system\s*prompt|"
    r"per\s+(?:the\s+)?rules?|the\s+rules?\s+says?|rules?\s+say|"
    r"内部规则编号|内部规则|系统提示词|系统提示|提示词要求|"
    r"规则\s*4b|按规则|根据规则|据规则|规则说|规则要求|协调器(?:注记|判断)"
    r")"
)

# Boundary hold-back forms. Comparison normalizes whitespace runs to one
# space AND ignores spaces entirely, mirroring the regex `\s*` semantics
# (A4.9 R1 NEW-1: newline / zero-space / multi-space chunk splits must not
# leak). Keep in sync with _META_RE alternatives.
_PATTERN_FORMS = (
    "coordinator note", "identity rule", "identity rules", "system prompt",
    "per rule", "per rules", "per the rule", "per the rules",
    "the rule says", "rule says", "rules say",
    "内部规则编号", "内部规则", "系统提示词", "系统提示", "提示词要求",
    "规则4b", "按规则", "根据规则", "据规则", "规则说", "规则要求",
    "协调器注记", "协调器判断",
)


def _matches_any_form(collapsed: str) -> bool:
    squeezed = collapsed.replace(" ", "")
    for form in _PATTERN_FORMS:
        if form.startswith(collapsed) or form.replace(" ", "").startswith(squeezed):
            return True
    return False


def _hold_len(buf: str) -> int:
    """Longest suffix of buf that could still grow into a pattern match.

    Works in whitespace-collapsed space so arbitrary whitespace runs (the
    regex `\\s*`, including across chunk boundaries: zero, one, newlines, or
    long runs) are handled. For each collapsed-suffix length the original
    suffix is recovered by walking backward run-by-run; a suffix qualifies
    when its collapsed form is a prefix of any pattern form, with or without
    spaces (A4.9 R2: no fixed hold cap — a long gap must not flush the
    phrase's start).
    """
    low = buf.lower()
    max_norm = max(len(f) for f in _PATTERN_FORMS)
    hold = 0
    for c_len in range(1, min(len(low), max_norm) + 1):
        i = len(low)
        seen = 0
        while i > 0 and seen < c_len:
            i -= 1
            if low[i].isspace():
                while i > 0 and low[i - 1].isspace():
                    i -= 1
            seen += 1
        if seen < c_len:
            break
        if _matches_any_form(re.sub(r"\s+", " ", low[i:])):
            hold = max(hold, len(low) - i)
    return hold


def scrub_reasoning_meta(text: str) -> str:
    """Replace internal-scaffolding references in reasoning text."""
    if not text:
        return text
    return _META_RE.sub(_PLACEHOLDER, text)


def scrub_reasoning_streaming(text: str, tail: str = "") -> tuple[str, str]:
    """Streaming-safe scrub: returns (safe_prefix, held_tail).

    The held tail is the longest boundary suffix that could still be the
    start of a match; flush it through `scrub_reasoning_meta` at the end of
    the stream or when the segment is reset.
    """
    buf = (tail or "") + (text or "")
    hold = _hold_len(buf)
    if hold == 0:
        return scrub_reasoning_meta(buf), ""
    safe, held = buf[:-hold], buf[-hold:]
    return scrub_reasoning_meta(safe), held
