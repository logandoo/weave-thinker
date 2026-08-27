# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""遵循词 (canary marker) — context-rot detection for the agent tool loop.

SOTA-corrected follow-word mechanism (2026-08-03). The user's original design —
inject a follow-word the model must echo, strip it from display, and compress
when the echo stops — was implemented once in deathmatch mode as an invisible
HTML comment (``<!--dm_ctx:...-->``) and FAILED in production: DeepSeek does
not echo HTML comments, every turn counted as a miss, and the loop restarted
infinitely (memory/fix_deathmatch_loop_stuck_at_turn0.md).

This module is the corrected version, following the community "context canary"
pattern and its documented pitfalls:

1. **Visible token** — ``[遵循词:xxxxxx]`` is plain text, reliably echoed.
2. **System-prompt placement** — the instruction lives in early context (the
   system block), not the last user message, so a miss genuinely tests
   long-context retention instead of self-refreshing from recent context.
3. **One-sided test** — presence is weak evidence, absence is strong evidence.
   A single miss is a warning; TWO CONSECUTIVE misses (across turns) is the
   confirmed trip that triggers compression.
4. **Compress, never restart** — on a trip we compact the conversation and
   ask the model to re-answer. We never reset counters or restart loops
   (the deathmatch bug).
5. **Cost guards** — one compression per run, and auto-disable after 3
   miss-triggered compressions with no intervening hit (a model that never
   echoes the marker must not cause unbounded compression churn).

The marker is deterministic per conversation (sha256 of conversation id), so
no DB column or cross-request state is needed, and the strip regex never
collides with user content.
"""
import hashlib
import re
from typing import Dict, Optional

CANARY_PATTERN = r"\[遵循词:([0-9a-fA-F]{6})\]"
CANARY_RE = re.compile(CANARY_PATTERN)
# A trailing suffix that could still become a complete marker once more
# chars arrive (marker is 12 chars: `[` + 遵循词 + `:` + 6 hex + `]`; the
# longest holdable prefix is the full marker minus its final `]` = 11 chars).
# Matches ANY partial prefix of the marker, including splits inside the
# literal 遵循词 itself (`[遵`, `[遵循`, `[遵循词`, `[遵循词:`).
# Note: hex is accepted in either case — the generator always emits
# lowercase, but a case-variant echo must still be stripped from display.
CANARY_PREFIX_RE = re.compile(r"\[(?:遵循词:[0-9a-fA-F]{0,6}|遵循词|遵循|遵)?$")
_CANARY_MAX_PREFIX = len("[遵循词:") + 6  # 11 — full marker minus closing ]


def make_canary(conversation_id: str) -> str:
    """Return the per-conversation canary marker token.

    Deterministic: the same conversation always produces the same marker,
    so it survives request boundaries without any persisted state, and the
    system prompt stays byte-stable across turns.
    """
    h = hashlib.sha256(str(conversation_id).encode("utf-8")).hexdigest()[:6]
    return f"[遵循词:{h}]"


def canary_prompt_section(marker: str) -> str:
    """System-prompt section instructing the model to echo the marker.

    Placed at the END of the dynamic system block (closest to the first user
    message) so it holds maximal instruction weight while remaining part of
    early context.
    """
    return (
        "内部校验约定：每一条最终回复（不含工具调用过程的过渡文字）的最后一行，"
        f"必须原样输出标记 {marker}，不得省略、不得加空格或改写。"
        "该标记是系统完整性校验，用户不可见，不要向用户解释它，也不要写入工具调用中。"
    )


def response_has_canary(text: Optional[str], marker: str) -> bool:
    """True when the raw model output contains the expected canary marker."""
    if not text:
        return False
    return marker in text


def strip_canary(text: str) -> str:
    """Remove ALL canary markers from text (display/persistence safety net)."""
    if not text:
        return text
    return CANARY_RE.sub("", text)


def strip_canary_streaming(text: str, tail: str = "") -> tuple[str, str]:
    """Strip canary markers from a streaming chunk without leaking split
    markers.

    LLM streams arrive as tiny deltas (DeepSeek: 1-3 chars), so a complete
    marker ``[遵循词:xxxxxx]`` is routinely split across chunk boundaries and
    per-chunk ``CANARY_RE.sub`` never matches (conv 6227fb26, 2026-08-14: the
    marker leaked into the live stream / persisted tool_results reasoning
    panel).

    The caller keeps a held-back ``tail`` (at most 11 chars) containing the
    suffix that could be the beginning of a marker; it is prepended to the
    next chunk, the combined text is stripped, and the longest trailing
    partial-marker prefix is held again. Returns ``(emit_now, new_tail)``.
    An incomplete marker at stream end is never emitted — cost note: this
    also silently swallows up to 11 trailing chars that merely LOOK like a
    marker prefix (e.g. a truncated ``[`` at stream end); acceptable because
    ``遵循词`` is a reserved term the prompt forbids the model from writing,
    and an uncompleted marker would trip the canary miss path anyway.
    """
    combined = (tail or "") + (text or "")
    if not combined:
        return "", ""
    cleaned = CANARY_RE.sub("", combined)
    hold = 0
    for l in range(min(len(cleaned), _CANARY_MAX_PREFIX), 0, -1):
        if CANARY_PREFIX_RE.match(cleaned[-l:]):
            hold = l
            break
    if hold:
        return cleaned[:-hold], cleaned[-hold:]
    return cleaned, ""


class CanaryTracker:
    """Cross-turn miss bookkeeping for one process.

    The agent loop is per-request, so a miss counter cannot live on the loop
    object. This tracker keys on conversation id (single-process uvicorn).

    Per-request compression budget is enforced by the loop itself
    (``state.canary_compressed``); this tracker only gates ACROSS requests:
    after ``auto_disable_after`` miss-triggered compressions without an
    intervening hit, the conversation stops compressing.
    """

    _AUTO_DISABLE_COMPRESSIONS = 3
    _ENTRY_TTL_SECONDS = 24 * 3600

    def __init__(self, auto_disable_after: int = 3) -> None:
        self.auto_disable_after = auto_disable_after
        self._misses: Dict[str, int] = {}
        self._compressions: Dict[str, int] = {}
        self._ts: Dict[str, float] = {}
        self._last_purge: float = 0.0

    def _purge_stale(self) -> None:
        import time
        now = time.time()
        if now - self._last_purge < 3600:
            return
        self._last_purge = now
        stale = {k for k, t in self._ts.items() if now - t > self._ENTRY_TTL_SECONDS}
        for k in stale:
            self._misses.pop(k, None)
            self._compressions.pop(k, None)
            self._ts.pop(k, None)

    def misses(self, conversation_id: str) -> int:
        return self._misses.get(conversation_id, 0)

    def record_miss(self, conversation_id: str) -> int:
        import time
        self._purge_stale()
        n = self._misses.get(conversation_id, 0) + 1
        self._misses[conversation_id] = n
        self._ts[conversation_id] = time.time()
        return n

    def record_hit(self, conversation_id: str) -> None:
        self._misses.pop(conversation_id, None)
        self._compressions.pop(conversation_id, None)
        self._ts.pop(conversation_id, None)

    def record_compression(self, conversation_id: str) -> int:
        import time
        self._purge_stale()
        n = self._compressions.get(conversation_id, 0) + 1
        self._compressions[conversation_id] = n
        self._ts[conversation_id] = time.time()
        return n

    def compression_count(self, conversation_id: str) -> int:
        return self._compressions.get(conversation_id, 0)

    def can_compress(self, conversation_id: str) -> bool:
        """True while the conversation is not auto-disabled. The per-request
        budget lives in the loop (``state.canary_compressed``)."""
        return not self.auto_disabled(conversation_id)

    def auto_disabled(self, conversation_id: str) -> bool:
        """True when a model repeatedly misses after compression — further
        miss-triggered compressions for this conversation would be pure cost.
        Any hit re-arms the conversation."""
        return self.compression_count(conversation_id) >= self.auto_disable_after


canary_tracker = CanaryTracker()
