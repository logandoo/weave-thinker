# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Full-duplex voice conversation orchestrator.

A single ``VoiceDuplexSession`` owns one WebSocket connection and runs the
cascaded pipeline: streaming ASR -> turn-taking controller (duplex + intent
subagents) -> fast main agent -> endpoint-segmented MiMo TTS.

Design references: cascaded ASR-LLM-TTS with a turn-taking controller and a
{idle, listen, think, speak, dual} decision state machine (FireRedChat,
DuplexCascade, semantic-VAD dialogue managers).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator, Optional

import websockets
from sqlalchemy import select
from websockets.exceptions import ConnectionClosed

from app.core.config import get_config
from app.services.asr_service import (
    ASRService,
    _recognition_tuning_params,
    _supports_instant_vocabulary,
    apply_hotword_phonetic_correction,
)
from app.services.llm_service import LLMService
from app.services.provider_router import build_thinking_extra_body, get_provider_router
from app.services.title_generator import TitleGeneratorService
from app.services.tts_service import get_tts_service
from app.services.agent_service import _load_identity_memory_context
from app.services.retry_utils import parse_tool_arguments, truncated_tool_args_feedback

logger = logging.getLogger(__name__)

from app.services.voice_asr import (
    _PUNCT_WS_RE,
    _build_funasr_context_payload,
    _char_bigrams,
    _is_likely_echo,
)
from app.services.voice_tts_text import (
    _MD_LINK_RE,
    _TTS_SOUND_TAG_RE,
    _md_link_label,
    _sanitize_tts_tags,
    _strip_markdown_for_tts,
)


from app.services.voice_text import (
    _EMOJI_RE,
    _LEAD_PUNCT,
    _SEGMENT_PUNCT,
    _TERMINAL_PUNCT,
    _TTS_STYLE_SORTED,
    _TTS_STYLE_WHITELIST,
    _norm_barge_compare,
    _normalize_style_tag,
    _strip_emoji,
    _strip_lead_punct,
    _utterance_complete,
    strip_pseudo_tool_tags,
)
from app.services.voice_turn_taking import (
    _is_voice_rate_limit,
    _pre_classify_reusable,
    _prox_evidence_line,
    _prox_is_near_signal,
)


from app.prompts.voice import VOICE_ASR_CORRECTION_HINT as _ASR_CORRECTION_HINT
from app.services.voice_generation import VoiceGenerationMixin, _voice_clarification_tasks
from app.services.voice_tts import VoiceTTSMixin
from app.services.voice_asr_session import VoiceASRSessionMixin
from app.services.voice_turn import VoiceTurnMixin
from app.services.voice_session_setup import VoiceSessionSetupMixin
from app.services.voice_memory import VoiceMemoryMixin
from app.services.voice_filler import VoiceFillerMixin
from app.services.voice_ws import VoiceWSMixin
from app.services.voice_classifiers import (
    VoiceClassifyMixin,
    _BARGE_IN_RESPONSE_FORMAT,
    _EOT_RESPONSE_FORMAT,
    _INTENT_RESPONSE_FORMAT,
    _INTERJECTION_RESPONSE_FORMAT,
    _MEMORY_INTERJECTION_RESPONSE_FORMAT,
)


from app.prompts.voice import (
    VOICE_INTERJECTION_SYSTEM,
    VOICE_BARGE_IN_SYSTEM,
    VOICE_INTENT_SYSTEM,
    VOICE_EOT_SYSTEM,
)


# 部署排空扩面（D-12，2026-10-06）：在途语音会话计数——GET /api/server/active-runs
# 三源之一（registry + 后台 + 语音），stop.sh 排空等待覆盖语音面。
_active_voice_sessions = 0


def begin_session() -> None:
    global _active_voice_sessions
    _active_voice_sessions += 1


def end_session() -> None:
    global _active_voice_sessions
    _active_voice_sessions = max(0, _active_voice_sessions - 1)


def active_session_count() -> int:
    return _active_voice_sessions

# Punctuation that ends a speakable segment (clause / sentence boundary).
# Resume-breakpoint snap boundaries: after a mid-segment pause the breakpoint
# is backed off to the last of these (punctuation / space / closing style-tag
# paren) so the resume never re-synthesizes from mid-word (see _pause_playback).
_SAFE_RESUME_BOUNDARIES = _SEGMENT_PUNCT + ")）"


# ---- Voice subagent structured output (JSON schema) ----
# 用户要求（2026-09-13）：语音子代理（分类/判定）采用 JSON schema 回复。供应商
# 支持不一（DeepSeek 实测 json_schema 400「unavailable」/ json_object OK；其他
# OpenAI 兼容端点可能支持 json_schema），故走三级阶梯：
#   schema → json_object → off（无 response_format，靠 prompt 约束 + 容错解析）
# 被拒档位按 purpose 学习并缓存，同一会话后续调用不再重试被拒档位。
_RESPONSE_FORMAT_ERROR_HINTS = (
    "response_format",
    "json_schema",
    "json_object",
    "guided_json",
)
# A bare RF token is not enough: OpenAI-compatible gateways sometimes echo the
# whole request body in ANY 400 message, so a thinking-knob rejection would
# look like an RF rejection and permanently downgrade the session (A4.9 review
# Important #2). Require a rejection phrase too, and let thinking/reasoning
# rejections win the tie (they get the legacy no-structured-output retry).
_REJECTION_ERROR_HINTS = (
    "not support",
    "unsupported",
    "unavailable",
    "not available",
    "invalid",
    "unknown",
    "unrecognized",
    "unexpected",
    "not allowed",
    "not permitted",
    "does not support",
    "extra inputs",
)
_THINKING_ERROR_HINTS = (
    "thinking",
    "reasoning",
    "enable_thinking",
    "reasoning_effort",
)


class _StructuredOutputRejected(Exception):
    """Provider rejected the response_format payload (signal for the ladder)."""


def _looks_like_thinking_error(msg: str) -> bool:
    return (
        any(h in msg for h in _THINKING_ERROR_HINTS)
        and any(h in msg for h in _REJECTION_ERROR_HINTS)
    )


def _looks_like_response_format_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if not any(h in msg for h in _RESPONSE_FORMAT_ERROR_HINTS):
        return False
    if _looks_like_thinking_error(msg):
        return False
    return any(h in msg for h in _REJECTION_ERROR_HINTS)




_EMOTION_ENUM = ["calm", "interested", "excited", "upset", "broken"]







# Punctuation that marks a *complete* utterance (sentence-final). Used by the
# adaptive endpointing watchdog to decide how long a silence means "done".
# Hard cap (s) on how long one voice turn waits for the concurrent identity /
# shared-memory load before generating with the fallback prompt. The load can
# block for tens of seconds on a slow memory provider; the first answer must
# not (2026-09-20 first-query TTFT fix). Loaded content applies from the next
# turn on.
_IDENTITY_WAIT_SECONDS = 2.5
# Local mic-energy activity tracking (ASR result-gap bridge, 2026-09-20):
# FunASR's realtime result stream can stall for seconds at long sentence
# boundaries while the user keeps speaking (measured 6.7s gap on a real 77.5s
# recording, conv 213f5a55 class). The EoT watchdog then sees "silence" and
# flushes a mid-utterance turn. The uploaded mic PCM is the only activity
# signal in that window; these constants mirror the frontend near-field
# classifier (adaptive noise floor + ratio gate) over 16-bit PCM RMS.
_MIC_ENERGY_FLOOR_INIT = 0.01 * 32768
_MIC_ENERGY_FLOOR_MIN = 0.0005 * 32768
_MIC_ENERGY_FLOOR_MAX = 0.06 * 32768
_MIC_ENERGY_NEAR_RATIO = 6.0
_MIC_ENERGY_NEAR_MIN = 0.04 * 32768
# Freshness of a local-speech sample for the flush hold. Must cover the flush
# decision latency (grace ~1.2s + processing) so speech that resumed just
# before the would-be flush still holds it; a truly silent turn releases the
# hold at the same moment the normal grace expires (no added wait).
_MIC_ENERGY_ACTIVITY_FRESH_SECONDS = 1.2
# Tags to strip from the text shown to the user (kept for TTS only).
_TAG_RE = re.compile(r"[(（\[][^)）\]]{0,24}[)）\]]")
# Voice tool visibility + display names live in a small catalog module so this
# host file stays within its move-only line budget. Aliased to the historical
# private names because other modules/tests import them from here.
from app.services.voice_tool_catalog import (  # noqa: E402
    VOICE_TOOL_DISPLAY_NAMES as _VOICE_TOOL_DISPLAY_NAMES,
    VOICE_TOOLS as _VOICE_TOOLS,
    voice_tool_names as _voice_tool_names,
)


def _voice_tool_display_name(name: str, lang: str) -> str:
    """Human-readable tool name for TTS/UI, in the requested language.

    Known tools resolve from _VOICE_TOOL_DISPLAY_NAMES; MCP tools are split via
    their registry toolset (``mcp-{server}`` — unambiguous even when server or
    tool names contain underscores) and announced as "<server> 的 <tool> 工具"
    / "<server> <tool> tool"; anything else falls back to the prettified name.
    """
    mapped = _VOICE_TOOL_DISPLAY_NAMES.get(name)
    if mapped:
        return mapped[0] if lang == "zh" else mapped[1]
    from app.tools.registry import registry

    toolset = registry.get_toolset_for_tool(name) or ""
    if toolset.startswith("mcp-"):
        server = toolset[len("mcp-"):]
        tool = name[len(f"mcp_{server}_"):] if name.startswith(f"mcp_{server}_") else name
        tool_pretty = tool.replace("_", " ")
        if lang == "zh":
            return f"{server} 的 {tool_pretty} 工具"
        return f"{server} {tool_pretty} tool"
    return name.replace("_", " ")


def _detect_text_language(text: str) -> str:
    """Return 'zh' when *text* is predominantly Chinese, else 'en'.

    Used to pick the tool-name language for TTS announcements: an English
    reply is announced as "web search", a Chinese one as "联网查询".
    Scope: zh/en only — kana/hangul text (isalpha but outside the CJK range)
    is treated as 'en'.
    """
    t = (text or "").strip()
    if not t:
        return "zh"
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in t if ch.isalpha())
    if letters == 0:
        return "zh"
    return "zh" if cjk / letters >= 0.4 else "en"


def _build_tool_notice_names(tool_calls: list[dict], lang: str) -> str:
    """Join the spoken names of a tool_calls list in the given language."""
    names = [
        _voice_tool_display_name(tc.get("function", {}).get("name", "工具"), lang)
        for tc in tool_calls
    ]
    return "、".join(names) if lang == "zh" else ", ".join(names)


def _plan_voice_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Classify accumulated voice tool calls for dispatch (order preserved).

    Production incident conv fcb2e60a (2026-09-21): the voice LLM's output
    budget (max_tokens=1024) truncated a `notes.create_note` arguments JSON
    mid-string; the old code silently degraded the unparseable args to `{}`,
    dispatched the call anyway and the tool answered "Unknown action", so the
    model retried the same overlong call 7 times and gave up.

    A call whose `function.arguments` is non-empty but does not parse is
    therefore NOT dispatchable — the parameters are gone and executing `{}`
    invents a different call. Parsing/repair semantics are shared with the
    agent path (`retry_utils.parse_tool_arguments`). Each entry carries:
      * `call`      — the original tool-call dict (persisted verbatim for
                      forensics; never mutated)
      * `args`      — parsed dict, or None when not dispatchable
      * `truncated` — True for truncated/unparseable arguments
      * `safe_call` — provider-safe copy (arguments normalized to `{}` when
                      truncated) for re-sending in assistant messages
    """
    plan: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        args = parse_tool_arguments(fn.get("arguments"))
        truncated = args is None
        if truncated:
            safe_call = {
                **tc,
                "function": {**fn, "arguments": "{}"},
            }
        else:
            safe_call = tc
        plan.append({
            "call": tc,
            "args": args,
            "truncated": truncated,
            "safe_call": safe_call,
        })
    return plan


def _voice_truncation_feedback(name: str) -> str:
    """Actionable tool result for a call whose args were cut off by the
    model's output budget (shared wording with the agent path)."""
    return truncated_tool_args_feedback(name)


def _escalated_voice_max_tokens(current: Optional[int], ceiling: Optional[int]) -> int:
    """Raise the voice output budget after a truncation (bounded by ceiling).

    Doubling gives a truncated tool call room to land on retry; the ceiling
    (config `[voice] max_tokens_ceiling`) keeps worst-case latency finite and
    the per-turn retry limit bounds the number of escalations.
    """
    if ceiling is None or ceiling <= 0:
        return current if current else 0
    if not current or current <= 0:
        return ceiling
    return min(current * 2, ceiling)
# Explicit user phrases that mean "cancel the current task". Only clear,
# unambiguous stop commands match — the task continues for anything else.
# Two tiers: (1) unambiguous long phrases matched as substrings anywhere in
# the utterance; (2) very short commands (停下/等下/...) that must be the WHOLE
# utterance so they don't false-fire inside normal speech (e.g. "我等下要去吃饭").
_STOP_TASK_RE = re.compile(
    r"(不要继续|终止任务|停止任务|取消任务|别做了|不要做了|别继续|不要做这个|"
    r"停下来|取消这个|别做了吧|不要了.*任务|算了.*任务|先别干|别干了|"
    r"不要讲了|不要再讲|别再讲|不要再说|先不要讲|先别说了|"
    r"stop\s*(the\s*)?task|cancel\s*task|abort\s*task|stop\s*it)",
    re.IGNORECASE,
)
_STOP_TASK_EXACT_RE = re.compile(
    r"^(?:停|停下|停下来|停一停|停停停|停停|打住|别说了|别讲|别讲了|"
    r"等下|等一下|等等|先等等|好了好了|好啦好啦|行了行了|可以了|"
    r"stop|wait|hold\s*on)[，。！？!?,.\s…~啊呀呐呢]*$",
    re.IGNORECASE,
)

# Quick ASR-noise patterns — texts matching these are almost certainly
# misrecognised ambient sound and should never interrupt TTS playback.
# NOTE: deliberately NO length rule here. 1-2 char utterances (停下/等下/停)
# are the most common interrupt/stop commands and must reach the stop-word
# check and the barge-in classifier; short-fragment judgment is the
# classifier's job, not a hardcoded length ban.
_ASR_NOISE_RE = re.compile(
    r"^[，。！？；、…,.!?;~\s]+$"          # only punctuation/whitespace
    r"|^[嗯啊呃哦唉嘿哈咦唔嘿]+$"           # only filler/hesitation sounds
)

# Extended noise-filter patterns for deterministic pre-classification.
# These catch transcribed ambient sounds (coughs, sighs, throat clearing,
# breathing) that streaming ASR can produce as short character strings.
# Applied as a fast pre-filter BEFORE the LLM intent classifier to avoid
# burning API calls on obvious noise.
# Matched after stripping common punctuation/whitespace from both ends.
_VOICE_NOISE_FILLER_RE = re.compile(
    r"^"
    r"[咳嗬嗯啊呃哦唉哎诶嘿哈嘻噫唔呦哟哼呼呵嘁咝噗]+"  # pure filler/noise syllables
    r"$"
)
_VOICE_NOISE_COUGH_RE = re.compile(
    r"^咳+嗽*$"       # 咳 / 咳咳 / 咳嗽
)
_VOICE_NOISE_SIGH_RE = re.compile(
    r"^[唉哎诶嗨嘿呼呵嘁][~…～。！？…、，,.!?;]*$"  # sigh with optional trailing punct
)


def _is_voice_noise(text: str) -> bool:
    """Deterministic pre-filter: return True if *text* is almost certainly an
    ASR misrecognition of ambient sound (cough, sigh, breath, throat clearing)
    that should never trigger a response.

    This runs BEFORE the LLM intent classifier to eliminate wasted API calls on
    obvious noise. Only covers short, unambiguous noise patterns — anything
    longer or semantically ambiguous is left to the LLM classifier.
    """
    t = (text or "").strip()
    if not t:
        return True
    if _ASR_NOISE_RE.match(t):
        return True
    # Strip common punctuation/whitespace and re-check length.
    stripped = t.strip("，。！？；、…,.!?;~ 　\t\n\r")
    if not stripped:
        return True
    if len(stripped) > 2:
        # Over 2 chars after stripping punctuation: could be a real short
        # utterance (e.g. "你好", "帮我", "是的"). Only match unambiguous
        # noise patterns at this length.
        if _VOICE_NOISE_COUGH_RE.match(stripped):
            return True
        if len(stripped) <= 3 and _VOICE_NOISE_FILLER_RE.match(stripped):
            return True
        return False
    return bool(
        _VOICE_NOISE_FILLER_RE.match(stripped)
        or _VOICE_NOISE_COUGH_RE.match(stripped)
        or _VOICE_NOISE_SIGH_RE.match(stripped)
    )


# Freshness window (s) for aux-echo guards: the mic echo of a filler/
# backchannel phrase arrives within ~0.2-2s of playback, so a guard older
# than this can never match a real echo and must not rewrite user speech.
_AUX_ECHO_GUARD_TTL = 4.0


def _strip_aux_echo(full: str, guards: list, now: float | None = None) -> str:
    """Strip an aux-speech echo (filler/backchannel phrase) from the TAIL of a
    longer ASR accumulation, so the agent's own "嗯"/"我来看看啊" heard back by
    the mic never pollutes the EoT pending text.

    Guards are ``(phrase, spoken_at)`` tuples; stale guards (> _AUX_ECHO_GUARD_TTL
    old) are ignored so a real user utterance that happens to end with the
    same words is never rewritten (A4.9 M2). Only strips when the text is
    STRICTLY LONGER than the guard: a standalone "嗯" is a real user utterance
    (confirmation) and is never stripped. Longest guard wins first (the full
    joined phrase before its split segments), so a whole-phrase echo is
    removed cleanly instead of leaving a dangling clause fragment behind."""
    now = _now() if now is None else now
    candidates = []
    for g in (guards or []):
        if isinstance(g, tuple):
            ph, ts = g
            if not ph or not isinstance(ph, str) or now - ts > _AUX_ECHO_GUARD_TTL:
                continue
        else:
            ph = g
        if ph and isinstance(ph, str):
            candidates.append(ph)
    for ph in sorted(candidates, key=len, reverse=True):
        if full.endswith(ph) and len(full) > len(ph):
            return full[: -len(ph)]
    return full


# Hint injected into every voice LLM context: streaming ASR transcripts are
# imperfect. The model should reason over context and phonetic/semantic
# plausibility instead of treating the raw text as ground truth.

# Leading characters that can never legitimately begin a user utterance. The
# streaming ASR accumulated text is retroactively finalised (punctuation is
# appended at sentence_end), so a naive character-offset slice can start the
# next turn with the previous sentence's trailing punctuation (e.g. "，快点").
# A genuine user turn never starts with punctuation, so stripping it is safe.












def _now() -> float:
    return time.monotonic()


from app.services.voice_time import _local_time_line, _stamp_query_time  # P5.5 move-only










def _perf(phase: str, ms: float, **extra) -> None:
    """Per-phase latency log for the voice pipeline. ``phase`` names the node;
    ``ms`` is the wall time spent in it; ``extra`` carries context. Grep
    'voice_perf' in the backend log for a per-turn timing trace."""
    if extra:
        logger.info(
            "voice_perf phase=%s ms=%d %s",
            phase, int(ms), json.dumps(extra, ensure_ascii=False, default=str),
        )
    else:
        logger.info("voice_perf phase=%s ms=%d", phase, int(ms))


def _build_voice_tool_results(tool_results: list[dict]) -> str | None:
    """Convert the flat voice-mode tool_results list into the rich
    ToolResultsData format (agent_steps etc.) that the frontend
    MessageBubble renders as collapsible tool-call blocks."""
    if not tool_results:
        return None

    agent_steps: list[dict] = []
    results_list: list[dict] = []
    rounds_list: list[dict] = []

    search_round_idx = 0
    for tr in tool_results:
        name = tr.get("name", "")
        call_id = tr.get("tool_call_id", "")
        content = tr.get("content", "") or ""
        title = _voice_tool_display_name(name, "zh")

        agent_steps.append({
            "name": call_id or name,
            "title": title,
            "content": content[:6000] if content else "(无内容)",
            "step_type": "tool",
        })

        if name == "web_search":
            try:
                parsed = json.loads(content) if content else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            search_round_idx += 1
            hits = parsed.get("results", [])
            if isinstance(hits, list) and len(hits) > 0:
                rounds_list.append({
                    "round": search_round_idx,
                    "queries": [f"web_search_{search_round_idx}"],
                    "qualified": True,
                    "cn_en_count": len(hits),
                    "total_count": len(hits),
                })
                for h in hits:
                    if isinstance(h, dict) and h.get("url"):
                        results_list.append({
                            "title": (h.get("title") or "")[:200],
                            "url": h.get("url", ""),
                            "snippet": (h.get("snippet") or "")[:300],
                            "published_date": h.get("published_date"),
                        })

    payload: dict = {
        "rounds": rounds_list,
        "results": results_list,
        "search_failed": False,
        "agent_steps": agent_steps,
    }
    return json.dumps(payload, ensure_ascii=False)






def strip_voice_tags(text: str) -> str:
    """Remove (风格)/[音频标签] and emoji so the transcript shows clean text.

    2026-11-07 语音事故防御：同时剥离伪工具标记（<tool_call>… 文本形态），
    不让其出现在前端展示与朗读文本里。"""
    if not text:
        return ""
    cleaned = strip_pseudo_tool_tags(text)
    cleaned = _TAG_RE.sub("", cleaned)
    cleaned = _strip_emoji(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# MiMo TTS (mimo-v2.5-tts) recognized (风格) style words. Only these are kept
# inline in TTS text; any other parenthetical content (descriptive actions like
# "温柔地笑", sound words like "叹气", bare interjections like "嗯") is stripped
# because MiMo may vocalize it as unnatural 哼/啊 sounds. Source: MiMo-V2.5-TTS
# docs "推荐风格" list + common emotion aliases used by the big-sister persona.
# Longest-first so prefix matching prefers the longest style word
# (e.g. "温柔地笑" → "温柔", never a shorter partial).

# [音频标签] content that produces non-speech sounds (laughs/sighs/moans/breaths)
# — these are the source of the unnatural 哼/啊 (娇喘) sounds and must be stripped
# from TTS text. Source: MiMo docs "音频标签" 笑/叹气/喘息/哽咽 etc.






def norm_spoken_segment(text: str) -> str:
    """Normalize a TTS segment for duplicate comparison: strip (风格)/[音频标签]
    tags, emoji, punctuation and whitespace; casefold. So "(平静)好的，我来
    解释。" == "好的我来解释" — a re-utterance with different tags/punct is
    still the same content."""
    t = _TAG_RE.sub("", text or "")
    t = _strip_emoji(t)
    t = re.sub(
        r"[\s，。！？；：、,.!?;:…~·—-—()（）\[\]【】<>《》“”‘’'\"]+", "", t
    )
    return t.casefold()


class _SpokenDupWindow:
    """Two-phase duplicate-segment window for the TTS pipeline.

    Hermes port (tools/tts_tool.py stream_tts_to_speaker): the LLM sometimes
    repeats a sentence verbatim; the second copy must not be spoken twice.
    Adapted from hermes's session-wide exact-match list:

    - Two-phase: ``check()`` at PLAY time (A4.9 review C1: recording at
      enqueue time meant pause-drained segments — never heard — were already
      in the window, so a resume's re-enqueued delta was fully suppressed and
      the answer tail silently vanished); ``record()`` only when a segment
      COMPLETED playing (same guard as the _turn_segments breakpoint pool).
    - Per-TURN window (epoch-keyed reset) — a user asking "再说一遍" must
      hear the legitimately repeated answer of a NEW turn.
    - Window of the last 8 segments (clause-level — our _split_segments cuts
      at every punctuation, so a sentence spans several segments).
    - MIN length 6 normalized chars: short emphatic repeats ("对。对。对。",
      "好的。好的。") are intentional emphasis, never recorded nor suppressed.
    - Exact normalized match only — similar-but-different text is spoken.
    - Only answer-stream segments carry the ``dedup`` flag: notices,
      interjections, queue-acks and resume replays (pre_q) bypass the window
      entirely (review I1 — they cannot be LLM-repetition artifacts).
    """

    def __init__(self, maxlen: int = 8, min_len: int = 6) -> None:
        self._segs: "collections.deque" = collections.deque(maxlen=maxlen)
        self._min_len = min_len
        self._epoch: Optional[int] = None

    def check(self, text: str, epoch: int) -> bool:
        """True when `text` matches a segment that COMPLETED playing in the
        same epoch. Read-only — no recording."""
        if self._epoch != epoch:
            return False
        return norm_spoken_segment(text) in self._segs

    def record(self, text: str, epoch: int) -> None:
        """Record a segment that completed playing (epoch-keyed reset)."""
        if self._epoch != epoch:
            self._epoch = epoch
            self._segs.clear()
        norm = norm_spoken_segment(text)
        if len(norm) >= self._min_len:
            self._segs.append(norm)






def clean_for_tts(text: str) -> str:
    """Prepare text for MiMo TTS: strip emoji, sanitize style/audio tags, and
    strip Markdown syntax so the spoken output is clean prose.

    Only recognized ``(风格)`` style words are kept inline; descriptive phrases
    and sound-producing ``[音频标签]`` are stripped so MiMo does not vocalize
    them as unnatural 哼/啊 (娇喘) sounds. Markdown (bold/italic/links/
    headings/tables/code) is stripped per the hermes tts_text_normalize
    pattern — the same chat markdown reply fed to TTS should be spoken as
    prose, not syntax.
    """
    cleaned = _strip_markdown_for_tts(_strip_emoji(text or ""))
    return _sanitize_tts_tags(cleaned)


def _split_segments(buffer: str) -> tuple[list[str], str]:
    """Cut ``buffer`` at the last segment punctuation, guarding open tags.

    Returns (complete_segments, remainder). Only cuts when bracket/paren
    balance is intact so we never split a (风格) or [音频标签] in half.
    Sentence boundaries preserve clause-level prosody inside one TTS request;
    commas and enumeration punctuation deliberately remain in the buffer.
    """
    boundaries = _TERMINAL_PUNCT + ".\n"

    def boundary_end(text: str, index: int) -> int:
        ch = text[index]
        if ch not in boundaries:
            return -1
        if ch == ".":
            previous_is_digit = index > 0 and text[index - 1].isdigit()
            if index + 1 == len(text):
                # A digit-ending prefix may become a decimal on the next token.
                return -1 if previous_is_digit else index
            next_ch = text[index + 1]
            if not next_ch.isspace() and next_ch not in "\"'”’":
                # Decimal points and dotted hosts are not sentence boundaries.
                return -1
        end = index
        while end + 1 < len(text) and text[end + 1] in "\"'”’":
            end += 1
        return end

    segments: list[str] = []
    rest = buffer
    while True:
        last_idx = -1
        for i, _ch in enumerate(rest):
            end = boundary_end(rest, i)
            if end >= 0:
                last_idx = end
        if last_idx <= 0:
            break
        candidate = rest[: last_idx + 1]
        # Guard: don't cut inside an unclosed tag.
        if (candidate.count("(") != candidate.count(")")) or (
            candidate.count("（") != candidate.count("）")
        ) or (candidate.count("[") != candidate.count("]")):
            # Look for an earlier safe cut.
            safe = -1
            for i in range(last_idx - 1, 0, -1):
                end = boundary_end(rest, i)
                if end >= 0:
                    sub = rest[: end + 1]
                    if (sub.count("(") == sub.count(")")) and (
                        sub.count("（") == sub.count("）")
                    ) and (sub.count("[") == sub.count("]")):
                        safe = end
                        break
            if safe <= 0:
                break
            last_idx = safe
            candidate = rest[: last_idx + 1]
        segments.append(candidate)
        rest = rest[last_idx + 1 :]
        if not rest:
            break
    return segments, rest


class _VoiceASR:
    """Internal streaming ASR client (FunASR realtime primary, MiMo fallback).

    Emits dicts into ``event_queue``: {"type": ready|partial|segment|final|error, "text"}.
    ``partial``/``segment`` carry the full accumulated transcript so the session
    can slice per-turn text with an offset.
    """

    def __init__(self, service: ASRService, event_queue: asyncio.Queue, hotwords: Optional[list] = None, vocabulary_id: Optional[str] = None, context: Optional[list[dict]] = None):
        self.service = service
        self.q = event_queue
        self.hotwords = hotwords or []
        self.vocabulary_id = vocabulary_id
        # Recent conversation turns ({role, content}) used to build the
        # fun-asr-realtime ``raw_input.context`` for improved recognition of
        # in-topic speech and suppression of off-topic background speech.
        self.context: list[dict] = list(context or [])
        self._audio_q: asyncio.Queue = asyncio.Queue()
        self._upstream = None
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._sup_task: Optional[asyncio.Task] = None
        self._closed = False
        self._task_id = uuid.uuid4().hex[:32]
        # Accumulated transcript carried across upstream reconnects so the
        # session's consumed offset stays valid when a new FunASR connection
        # starts accumulating from an empty string.
        self._carry_text = ""
        # First-connect synchronization: start() waits on this so startup
        # failures propagate to the session instead of dying in the supervisor.
        self._start_done: Optional[asyncio.Event] = None
        self._start_error: Optional[Exception] = None

    async def start(self) -> None:
        if self.service.is_mimo:
            await self._start_mimo()
        elif self.service.is_dashscope and self.service._is_funasr_model:
            await self._start_funasr()
        elif self.service.is_dashscope:
            await self._start_funasr()  # generic dashscope realtime shares protocol
        else:
            raise RuntimeError("Voice mode requires DashScope/FunASR or MiMo ASR")
        # Do NOT await the first upstream connect: the FunASR/DashScope
        # connection is network-dependent and can take tens of seconds on
        # slow links. The session sends `ready` before this call, so the
        # client starts capturing immediately; PCM queues in `_audio_q`
        # (unbounded) and flushes once the recognizer connects. Persistent
        # connect failures are surfaced via {"type": "error"} events from
        # the supervisor, which the session forwards to the client.

    async def send_audio(self, pcm16: bytes) -> None:
        if self._closed:
            return
        await self._audio_q.put(pcm16)

    async def _emit(self, payload: dict) -> None:
        await self.q.put(payload)

    # ---- FunASR / DashScope realtime ----
    async def _start_funasr(self) -> None:
        """Start the supervisor that owns the upstream FunASR connection.

        The supervisor (re)connects, runs the send/recv pumps, and reconnects
        automatically when the upstream drops — DashScope closes realtime ASR
        connections server-side (e.g. after ~60s of silence audio when
        ``heartbeat`` is off, or on transient network failures). Without
        reconnection the transcript freezes while the user keeps speaking and
        the EoT watchdog flushes a mid-utterance turn (the "ASR truncation"
        bug)."""
        self._start_done = asyncio.Event()
        self._sup_task = asyncio.create_task(self._funasr_supervisor())

    async def _funasr_supervisor(self) -> None:
        failures = 0
        first = True
        warned = False
        while not self._closed:
            self._task_id = uuid.uuid4().hex[:32]
            try:
                await self._funasr_connect()
            except Exception as exc:
                was_first = first
                if first:
                    # First connect failed — the session is already live
                    # (`ready` was sent before ASR start). Report it, then keep
                    # retrying like any later outage: a transient upstream
                    # failure at session start must not leave the session
                    # permanently deaf (A4.9 review Important-2).
                    self._start_error = exc
                    if self._start_done is not None:
                        self._start_done.set()
                    first = False
                failures += 1
                logger.warning("voice ASR connect failed (%d): %s", failures, exc)
                if not warned and (was_first or failures >= 5):
                    # Surface an outage ONCE: immediately for the session's
                    # first connect (recognition never worked — the user must
                    # know), otherwise only after 5 consecutive failures, so a
                    # transient mid-session blip doesn't flash the error UI
                    # (A4.9 r2 Minor-1). Retrying never stops; recovery via
                    # `task-started` → ready restores listen automatically.
                    warned = True
                    await self._emit({"type": "error", "error": f"语音识别服务暂时不可用，正在重连: {exc}"})
                await asyncio.sleep(min(0.5 * failures, 3.0))
                continue
            if self._start_done is not None:
                self._start_done.set()
            first = False
            failures = 0
            warned = False
            self._send_task = asyncio.create_task(self._funasr_send())
            self._recv_task = asyncio.create_task(self._funasr_recv())
            done, pending = await asyncio.wait(
                {self._send_task, self._recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if self._closed:
                break
            logger.warning("voice ASR upstream connection lost; reconnecting")
            await self._emit({"type": "reconnecting"})
            await asyncio.sleep(0.5)

    async def _funasr_connect(self) -> None:
        url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
        headers = {"Authorization": f"bearer {self.service.dashscope_api_key}"}
        cfg = self.service._asr_config
        self._upstream = await websockets.connect(
            url,
            proxy=None,
            additional_headers=headers,
            max_size=None,
            open_timeout=int(cfg.get("ws_open_timeout", 10)),
            ping_interval=int(cfg.get("ws_ping_interval", 20)),
            ping_timeout=int(cfg.get("ws_ping_timeout", 20)),
            close_timeout=int(cfg.get("ws_close_timeout", 5)),
        )
        params: dict = {
            "sample_rate": 16000,
            "format": "pcm",
            "language_hints": ["zh"],
            # VAD silence threshold (ms): FunASR emits sentence_end after this
            # much silence. The EoT watchdog provides adaptive buffering on top
            # (complete vs incomplete utterances), so this should be aggressive
            # to minimize endpoint delay. 800ms avoids fragmenting natural speech
            # while keeping end-to-end latency <~2s.
            "max_sentence_silence": int(cfg.get("max_sentence_silence", 800)),
            # Multi-threshold mode prevents VAD from cutting long utterances
            # too early by dynamically adjusting the silence budget.
            "multi_threshold_mode_enabled": True,
            # Keep the upstream connection alive across silence stretches:
            # without heartbeat DashScope closes the connection after ~60s even
            # while silence audio is being streamed, freezing recognition
            # mid-conversation.
            "heartbeat": True,
        }
        # Speech/noise decision threshold (fun-asr-realtime). Higher values bias
        # the VAD toward rejecting ambient conversation and other background
        # voices so they aren't transcribed as user turns. Range [-1.0, 1.0].
        # Ref: https://help.aliyun.com/zh/model-studio/fun-asr-realtime-python-sdk
        vcfg = get_config()
        params["speech_noise_threshold"] = vcfg.voice_asr_speech_noise_threshold
        params.update(_recognition_tuning_params(cfg, self.service.dashscope_model))
        if self.vocabulary_id:
            params["vocabulary_id"] = self.vocabulary_id
        else:
            vocabulary = self.service._format_funasr_hotwords(self.hotwords)
            if vocabulary and _supports_instant_vocabulary(self.service.dashscope_model):
                params["vocabulary"] = vocabulary
        # fun-asr-realtime accepts a dialogue context that biases recognition
        # toward in-topic words and suppresses off-topic/background speech.
        # Ref: https://help.aliyun.com/zh/model-studio/improve-asr-accuracy
        asr_input: dict = {}
        if vcfg.voice_asr_context_enabled:
            ctx_payload = _build_funasr_context_payload(
                self.context, max_turns=vcfg.voice_asr_context_turns
            )
            if ctx_payload:
                asr_input["context"] = ctx_payload
        run_task = {
            "header": {"action": "run-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.service.dashscope_model,
                "parameters": params,
                "input": asr_input,
            },
        }
        logger.info(
            "voice ASR start: model=%s speech_noise_threshold=%s context_msgs=%d params=%s",
            self.service.dashscope_model,
            params.get("speech_noise_threshold"),
            len(asr_input.get("context", []) or []),
            json.dumps({k: v for k, v in params.items() if k != "vocabulary_id"}, ensure_ascii=False),
        )
        await self._upstream.send(json.dumps(run_task))

    async def _funasr_send(self) -> None:
        try:
            while not self._closed:
                pcm16 = await self._audio_q.get()
                if pcm16 is None:
                    return
                try:
                    await self._upstream.send(pcm16)
                except ConnectionClosed:
                    logger.warning("voice ASR send: upstream closed")
                    return
        except Exception as exc:
            logger.debug("voice ASR send error: %s", exc)

    async def _funasr_recv(self) -> None:
        # Seed with the carried-over transcript from a previous connection so
        # accumulated text (and the session's consumed offset) stays continuous
        # across reconnects.
        finalized: list[str] = [self._carry_text] if self._carry_text else []
        try:
            while not self._closed:
                try:
                    raw = await self._upstream.recv()
                except ConnectionClosed:
                    logger.warning("voice ASR recv: upstream closed")
                    return
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                header = msg.get("header", {})
                event = header.get("event", "")
                if event == "task-started":
                    await self._emit({"type": "ready"})
                elif event == "result-generated":
                    sentence = msg.get("payload", {}).get("output", {}).get("sentence", {})
                    text_val = sentence.get("text", "")
                    sentence_end = sentence.get("sentence_end", False)
                    if text_val:
                        if sentence_end:
                            finalized.append(text_val)
                            accumulated = "".join(finalized)
                        else:
                            accumulated = "".join(finalized) + text_val
                        if self.service.hotword_phonetic_correction:
                            accumulated = apply_hotword_phonetic_correction(accumulated, self.hotwords)
                        self._carry_text = accumulated
                        await self._emit({"type": "partial", "text": accumulated})
                        if sentence_end:
                            await self._emit({"type": "segment", "text": accumulated})
                    else:
                        # Empty-text result: the recognizer is alive and
                        # processing (sentence boundary / new sentence start).
                        # Surfaced as liveness so the EoT watchdog knows speech
                        # recognition is still progressing.
                        await self._emit({"type": "activity"})
                elif event == "task-finished":
                    final_text = "".join(finalized)
                    if self.service.hotword_phonetic_correction:
                        final_text = apply_hotword_phonetic_correction(final_text, self.hotwords)
                    await self._emit({"type": "final", "text": final_text})
                    return
                elif event == "task-failed":
                    err = header.get("error_message", header.get("message", "ASR task failed"))
                    logger.warning("voice ASR task-failed: %s", err)
                    return
        except Exception as exc:
            logger.debug("voice ASR recv error: %s", exc)

    # ---- MiMo fallback (periodic full-audio transcription, degraded duplex) ----
    async def _start_mimo(self) -> None:
        self._recv_task = asyncio.create_task(self._mimo_loop())
        self._send_task = asyncio.create_task(self._mimo_collect())
        await self._emit({"type": "ready"})

    async def _mimo_collect(self) -> None:
        self._mimo_buf = bytearray()
        while not self._closed:
            pcm16 = await self._audio_q.get()
            if pcm16 is None:
                return
            self._mimo_buf.extend(pcm16)

    async def _mimo_loop(self) -> None:
        interval = float(self.service.mimo_partial_interval_seconds or 1.5)
        last_text = ""
        while not self._closed:
            await asyncio.sleep(interval)
            buf = bytes(getattr(self, "_mimo_buf", b""))
            if len(buf) < 16000:  # < 0.5s of 16k pcm16
                continue
            try:
                text = ""
                # MiMo path expects float32; we have pcm16 — convert.
                import numpy as np

                f32 = (np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0).tobytes()
                async for chunk in self.service._transcribe_mimo_streaming(f32, is_float32=True):
                    text += chunk
                if self.service.hotword_phonetic_correction:
                    text = apply_hotword_phonetic_correction(text, self.hotwords)
                if text and text != last_text:
                    last_text = text
                    await self._emit({"type": "partial", "text": text})
            except Exception as exc:
                logger.debug("voice MiMo ASR error: %s", exc)

    async def update_context(self, history: list[dict]) -> None:
        """Push a refreshed dialogue context to fun-asr-realtime via the
        ``continue-task`` action so later turns are recognised with the live
        conversation as context (biases recognition toward in-topic speech and
        away from background conversation). Safe no-op for non-funasr models
        or when the upstream is not yet connected."""
        if self._closed or self._upstream is None:
            return
        if not (self.service.is_dashscope and self.service._is_funasr_model):
            return
        vcfg = get_config()
        if not vcfg.voice_asr_context_enabled:
            return
        payload = _build_funasr_context_payload(history, max_turns=vcfg.voice_asr_context_turns)
        if not payload:
            return
        msg = {
            "header": {"action": "continue-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {"input": {"context": payload}},
        }
        try:
            await self._upstream.send(json.dumps(msg))
            logger.info("voice ASR context refreshed: %d messages", len(payload))
        except Exception as exc:
            logger.debug("voice ASR continue-task context update failed: %s", exc)

    async def close(self) -> None:
        self._closed = True
        if self._start_done is not None and not self._start_done.is_set():
            self._start_done.set()
        if self._sup_task and not self._sup_task.done():
            self._sup_task.cancel()
        try:
            await self._audio_q.put(None)
        except Exception:
            pass
        for t in (self._send_task, self._recv_task):
            if t and not t.done():
                t.cancel()
        tasks = [t for t in (self._sup_task, self._send_task, self._recv_task) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._upstream is not None:
            try:
                # Politely finish the task then close.
                try:
                    finish = {
                        "header": {"action": "finish-task", "task_id": self._task_id, "streaming": "duplex"},
                        "payload": {"input": {}},
                    }
                    await self._upstream.send(json.dumps(finish))
                except Exception:
                    pass
                await self._upstream.close()
            except Exception:
                pass


# Active voice sessions keyed by user_id. Lets out-of-band events (background
# task completion) reach the user's live voice session so the assistant can
# proactively announce them. Last connection wins (reconnect replaces).
_ACTIVE_VOICE_SESSIONS: dict[str, "VoiceDuplexSession"] = {}


async def notify_voice_task_finished(task_id: str) -> bool:
    """Out-of-band hook invoked by agent_worker when a background task reaches
    a terminal state (completed/failed). If the task originated from this
    user's 语音助理 conversation and a voice session is live, queue a spoken
    notice so the assistant proactively tells the user and offers follow-up
    actions (read the result aloud / export PDF / save to notes). Returns
    True when the notice was handed to a session."""
    try:
        if not get_config().voice_bg_task_notify_enabled:
            return False
        from app.db.database import AsyncSessionLocal, AgentTask, Conversation

        async with AsyncSessionLocal() as db:
            task = await db.get(AgentTask, task_id)
            if task is None or task.status not in (
                "completed", "failed", "cancelled", "timed_out",
            ):
                return False

            from app.services.voice_notification_service import (
                claim_voice_notifications,
                notification_info,
                record_voice_task_notification,
            )

            if getattr(task, "execution_mode", None) == "separate_v1":
                if str(getattr(task, "origin_kind", "") or "") != "voice":
                    return False
                from app.services.voice_notification_service import (
                    record_separate_notification,
                )

                manifest = {}
                try:
                    import json as _json
                    manifest = _json.loads(task.execution_config or "{}")
                except Exception:
                    manifest = {}
                assistant_name = str(
                    (manifest.get("assistant") or {}).get("name") or "执行助手")
                await record_separate_notification(
                    db, task, assistant_name=assistant_name)
                await db.commit()
                # §10.3: follow the user's CURRENT voice session, not only C1.
                session = _ACTIVE_VOICE_SESSIONS.get(task.user_id)
                if session is None or session._closed:
                    return False
                # §10.2: route live completions through the same claim/replay
                # path as reconnects, so a burst is coalesced into one batch
                # instead of N immediate announcements.
                await session._replay_pending_notices()
                return True

            # Only voice-originated tasks are announced: the origin
            # conversation must belong to the user's 语音助理 assistant.
            if not task.conversation_id:
                return False
            conv = await db.get(Conversation, task.conversation_id)
            if conv is None:
                return False
            from app.services.assistant_service import ensure_voice_assistant

            voice_assistant = await ensure_voice_assistant(db, task.user_id)
            if conv.assistant_id != voice_assistant.id:
                return False
            # Durable first: no active WS only defers audible delivery; it must
            # not erase the completion notice. Repeated worker hooks reuse the
            # same task-id row.
            row = await record_voice_task_notification(db, task)
            if row.status != "pending":
                return False
            info = notification_info(row)
            session = _ACTIVE_VOICE_SESSIONS.get(task.user_id)
            if (
                session is None
                or session._closed
                or session.conversation_id != task.conversation_id
            ):
                return False
            claimed = await claim_voice_notifications(
                db,
                task.user_id,
                task.conversation_id,
                notification_id=row.id,
            )
            if not claimed:
                return False
            info["delivery_attempt_id"] = claimed[0].get("delivery_attempt_id")
            await session._enqueue_bg_task_notice(info, notification_id=row.id)
        return True
    except Exception:
        logger.exception("voice bg-task notify failed for task %s", task_id)
        return False


# 每轮回调上下文在 identity prompt 中的哨兵区块标记：区块内容由每轮
# fire-and-forget 召回替换（_apply_vmem_block），system prompt 因此始终
# 携带最新召回而不留双份基底。
_VMEM_START = "__#VOICE_MEMORY_START__"
_VMEM_END = "__#VOICE_MEMORY_END__"


def _vmem_wrap(*parts: str) -> str:
    body = "\n\n".join(p.strip() for p in parts if p and p.strip())
    if not body:
        return ""
    return f"{_VMEM_START}\n{body}\n{_VMEM_END}"


def _vmem_extract_block(prompt: str) -> str:
    """Extract the current sentinel block body ('' when absent) — used to
    capture the session-start baseline so an empty recall can restore it."""
    if _VMEM_START not in (prompt or ""):
        return ""
    m = re.search(re.escape(_VMEM_START) + r"\n?(.*?)\n?" + re.escape(_VMEM_END),
                  prompt, flags=re.DOTALL)
    return m.group(1) if m else ""


class _VmemUserBudget:
    """Per-user memory-interjection budget/cooldown shared ACROSS voice
    sessions (a reconnect must not reset them). Fixed 1h anchored window
    (window_start set on first touch) resets the counters; the cooldown
    clock always persists."""
    __slots__ = ("window_start", "append", "correct", "last_time")

    def __init__(self) -> None:
        self.window_start = 0.0
        self.append = 0
        self.correct = 0
        self.last_time = 0.0


_VMEM_BUDGET_WINDOW = 3600.0
_VMEM_BUDGETS: dict[str, _VmemUserBudget] = {}


def _vmem_budget(uid: str) -> _VmemUserBudget:
    b = _VMEM_BUDGETS.get(uid)
    now = _now()
    if b is None or now - b.window_start >= _VMEM_BUDGET_WINDOW:
        if b is None:
            # Opportunistic eviction of long-dead entries (bounded memory).
            for k, old in list(_VMEM_BUDGETS.items()):
                if now - old.window_start >= _VMEM_BUDGET_WINDOW * 2:
                    _VMEM_BUDGETS.pop(k, None)
        b = _VmemUserBudget()
        b.window_start = now
        _VMEM_BUDGETS[uid] = b
    return b


def _vmem_budget_clear(uid: Optional[str] = None) -> None:
    if uid is None:
        _VMEM_BUDGETS.clear()
    else:
        _VMEM_BUDGETS.pop(uid, None)


# B11（2026-09-14）：语音纠正 shadow 任务强引用（防 asyncio GC）


class VoiceDuplexSession(VoiceWSMixin, VoiceFillerMixin, VoiceClassifyMixin, VoiceGenerationMixin, VoiceTTSMixin, VoiceASRSessionMixin, VoiceTurnMixin, VoiceSessionSetupMixin, VoiceMemoryMixin):
    """One full-duplex voice session bound to a client WebSocket."""

    def __init__(self, websocket, user, db, conversation_id: Optional[str] = None,
                 audio_protocol: Optional[str] = None):
        self.websocket = websocket
        self._audio_protocol = "epoch_v1" if audio_protocol == "epoch_v1" else "legacy"
        self.user = user
        self.db = db
        self.config = get_config()
        self.tts = get_tts_service()
        self.conversation_id = conversation_id

        self.state = "idle"
        self._closed = False
        self._send_lock = asyncio.Lock()

        # ASR
        self._asr: Optional[_VoiceASR] = None
        self._asr_events: asyncio.Queue = asyncio.Queue()
        self._latest_full = ""
        self._consumed_offset = 0
        self._pending_turn_text = ""
        self._last_text_change = 0.0
        # Recognizer liveness: updated on EVERY upstream result (even ones whose
        # text didn't change, incl. empty-text boundary results). FunASR emits
        # results every ~0.3-1.1s while speech is being processed, so this —
        # not text-change time — is the reliable signal that the user is still
        # speaking. False when the upstream is reconnecting: the EoT watchdog
        # must not flush a turn while recognition is (re)starting.
        self._last_asr_activity = 0.0
        self._asr_ready = False
        # Set when the pending turn text last changed WHILE ending with terminal
        # punctuation; reset whenever the text is incomplete. Drives the
        # complete-utterance flush cap (bounded wait in noisy environments).
        self._complete_since = 0.0

        # Conversation
        self._history: list[dict] = []
        self._style_tag = ""

        # Turn / generation control
        self._turn_queue: asyncio.Queue = asyncio.Queue()
        self._tts_queue: asyncio.Queue = asyncio.Queue()
        self._interrupt = asyncio.Event()
        self._speaking = False
        self._turn_active = False
        self._task_cancelled = False
        self._prefetch: dict = {"turn": None, "text": None, "task": None}
        # Per-turn latency instrumentation (voice_perf logs): enqueue time is
        # stamped when a turn lands in the queue, turn start when the
        # responder picks it up.
        self._turn_enqueued_at: float = 0.0
        self._turn_started: float = 0.0
        self._first_chunk_logged = False
        self._barge_classify_start: float = 0.0
        self._intent_classify_start: float = 0.0
        self._interjection_classify_start: float = 0.0
        # Provider concurrency gate: xiaomimimo hard-rate-limits (429) when a
        # voice session fires several LLM calls at once (observed: main stream
        # + intent + interjection → 10-15s of 429s, the "voice is very slow"
        # symptom). A 2-slot semaphore bounds session concurrency: the main
        # stream + one subagent may run together (preserving the intent
        # noise-cancel design), a third call waits instead of tripping the
        # provider limit.
        self._llm_gate = asyncio.Semaphore(2)
        # Barge-in calibration: no-interrupt window after each speaking burst
        # starts (echo/noise is most likely in the first instants of playback)
        # and a cooldown after each confirmed interrupt (prevents a single
        # noisy stretch from re-interrupting the resumed playback).
        self._speaking_started_at = 0.0
        self._last_barge_in_time = 0.0
        # (W38 handoff T3, 2026-11-06: filler prefetch / TTFT-watch / audible
        # backchannel state removed with the unreachable chain — normal answers
        # never open with a synthetic filler; the backchannel CLASSIFIER chain
        # below stays live.)
        # Structured-output mode learned per subagent purpose ("schema" |
        # "object" | "off") — see `_quick_classify`.
        self._json_modes: dict[str, str] = {}
        # True while the consumer is playing an AUX item (filler/backchannel).
        # The onset barge-in epoch gate compares _speaking_epoch vs
        # _turn_epoch; aux items can carry a stale epoch (spoken before the
        # next generation increments it), so the gate must also accept
        # "currently playing aux" — user speech must pause the filler too
        # (A4.9 I3).
        self._playing_aux = False
        # Literal phrases most recently spoken as aux speech — used to strip
        # their own mic-echo from the ASR pending text (suffix-only, and only
        # when the accumulation is longer than the phrase).
        self._aux_echo_guard: list = []
        # Acoustic onset barge-in (FireRedChat pVAD pattern): speech detected
        # during playback pauses TTS immediately (first ASR partial); the LLM
        # classifier later decides resume-from-breakpoint (backchannel/defer)
        # or switch (interrupt). _paused_spoken_chars = text spoken so far at
        # the pause (sum of completed TTS segments) — the resume breakpoint.
        self._playback_paused = False
        self._paused_spoken_chars = 0
        # Serializes _on_user_turn handling: EoT flushes spawn concurrent
        # tasks, and the pause/resume + interrupt state machine must never
        # interleave (a backchannel resume must not stomp a concurrent
        # interrupt's _interrupt flag — A4.9 finding I1).
        self._turn_handling_lock = asyncio.Lock()
        # Generation epoch: incremented per _generate_and_speak; the TTS
        # consumer tags each audio burst with the epoch of the turn it belongs
        # to (_speaking_epoch). The onset-pause hook only fires when the
        # playing burst belongs to the CURRENT generation — a previous turn's
        # audio still draining (deferred-then-answered case) must not pause
        # against the NEW turn's (already-wiped) segment state (A4.9 r2).
        self._turn_epoch = 0
        self._speaking_epoch = 0
        # Latest epoch whose TTS send flush completed. A client underrun before
        # this boundary is temporary and must not end the speaking turn.
        self._completed_speaking_epoch = 0
        # Generation-side final flush marker, used to restore a flush dropped
        # by pause without declaring an in-flight response complete early.
        self._final_flush_enqueued_epoch = 0
        # Monotonic audio sequence within this WS session. Together with the
        # generation epoch it lets the client reject duplicates and stale PCM.
        self._audio_sequence = 0
        # Latest accepted user-turn ownership. Assigned at enqueue time so a
        # newer user turn can supersede a stale low-value assistant reply.
        self._latest_input_seq = 0
        self._generation_input_seq = 0
        self._pending_tool_summary_note = ""
        # Durable background-task notification delivery state. The current ID
        # marks TTS items produced by the notice turn; the active ID is cleared
        # only by playback_drained (delivered) or a confirmed interrupt (not).
        self._current_notice_id: Optional[str] = None
        self._active_notification_id: Optional[str] = None
        self._active_notification_audio_sent = False
        self._active_notification_generation_complete = False
        self._notification_states: dict[str, dict] = {}
        self._queued_notice_ids: set[str] = set()
        # Duplicate-segment suppression window (hermes stream_tts_to_speaker
        # port): the LLM occasionally repeats a sentence verbatim — the second
        # copy must not be spoken twice. Per-turn (epoch-keyed), so a NEW
        # turn may legitimately repeat ("再说一遍") and be spoken.
        self._dup_window = _SpokenDupWindow()
        # TTS pipeline prefetch state (see _tts_consumer/_drain_tts_queue):
        # the NEXT queued segment's synthesis runs while the current plays.
        # _tts_prefetch_valid is cleared by _drain_tts_queue so a gate-truncated
        # prefetch (interrupt/pause) is NEVER replayed against a later item
        # with the same text (A4.9 latency-fix review Important #1).
        self._tts_prefetch_q: Optional[asyncio.Queue] = None
        self._tts_prefetch_text = ""
        self._tts_prefetch_epoch = 0
        self._tts_prefetch_valid = True
        # Resume pre-synthesis state: at pause time the unspoken remainder is
        # synthesized into _resume_q (gated ignore_paused) while the barge-in
        # classifier runs; a backchannel/defer resume plays it directly, and a
        # drain (interrupt/stop) drops it. Epoch-guarded against cross-turn
        # replay (see _pause_playback/_resume_playback/_drain_tts_queue).
        self._resume_q: Optional[asyncio.Queue] = None
        self._resume_epoch = 0
        self._resume_text = ""
        self._send_pace_start = 0.0
        # Acoustic near-field gate state (browser-side RMS proximity signal,
        # see _prox_is_near/_handle_client_event): the client classifies its
        # own mic input as near-field (user close to the phone — almost
        # certainly the user's own voice) vs far-field (environment/background
        # speech picked up from a distance). Far-field speech must never pause
        # playback nor interrupt the current answer. Unknown clients (no
        # signal received) default to near — old behavior preserved.
        self._prox_seen = False
        self._prox_near = True
        self._prox_updated = 0.0
        # Recent backchannel verdicts (normalized text → monotonic time) so
        # the responder can skip a queued copy of a filler utterance that was
        # classified backchannel after the turn was already enqueued (ghost-
        # message fix, conv 689f06ec). See _record_backchannel/
        # _queued_turn_is_backchannel.
        self._recent_backchannel: dict[str, float] = {}
        # Utterance-time proximity snapshot: refreshed on every ASR partial/
        # segment event (A4.9 C1 — the live signal expires ~1s after the last
        # report, but the barge-in classifier runs at EoT flush, 1-3s later;
        # without the snapshot the evidence injected into the classifier would
        # be a stale far-field by then, and a REAL near-field interrupt would
        # be judged backchannel and swallowed). The snapshot reflects the
        # acoustic condition WHILE the utterance was being heard.
        self._prox_utterance: Optional[bool] = None
        # Onset-pause pre-classify: verdict computed in parallel with the EoT
        # watchdog, reused by _on_user_turn_locked when the text matches.
        self._pre_classify: Optional[dict] = None
        # Semantic EoT state: LLM judge on unpunctuated text (see watchdog).
        self._eot_semantic_checking = False
        self._eot_semantic_complete = False
        self._eot_semantic_checked_text = ""
        # Local mic-energy bridge state (ASR result-gap guard, 2026-09-20):
        # adaptive noise floor + last local activity timestamp + the anchor of
        # an in-progress held flush.
        self._mic_energy_floor = _MIC_ENERGY_FLOOR_INIT
        self._last_mic_activity = 0.0
        self._flush_hold_since = 0.0
        self._flush_hold_turn_started = 0.0
        self._flush_hold_log_at = 0.0
        # The first turn waits (bounded) for the concurrent identity load;
        # later turns never block on it again (A4.9 review Minor-8).
        self._identity_wait_done = False
        # Test-only stall injection counter (see _test_llm_stall client event).
        self._test_llm_stall = 0

        # Playback-progress tracking. In 语音助理 mode every answer reaches the user
        # ONLY as audio, so we must tie playback progress to the answer text:
        # any truncation is an AUDIO-playback truncation, never a text one. We
        # accumulate each turn's TTS segments with their audio durations and
        # reconcile them with the client's reported playback position so we can
        # tell exactly how much of the answer was actually spoken aloud.
        self._turn_segments: list[dict] = []   # [{"text","audio_sec","cum_sec","epoch"}]
        self._turn_audio_sec_total = 0.0        # sum of audio seconds sent this turn
        self._current_seg_text = ""
        self._current_seg_audio_sec = 0.0
        self._playback_played_sec = 0.0         # last client-reported played seconds
        self._playback_total_sec = 0.0          # last client-reported total seconds
        # Full-text position where the CURRENT audible burst started: 0 for the
        # turn's first burst, the pause breakpoint for a resumed burst. The
        # client reports played_sec relative to the burst start, so the pause
        # breakpoint estimate is burst_base_chars + played_sec * chars/sec —
        # without the base, a post-resume pause estimates a position INSIDE the
        # resume segment and replays already-heard text (observed live conv
        # 964462a8: pause#2 est=175 vs correct ~325 → an 18s audible repeat).
        self._burst_base_chars = 0
        # Cumulative audio seconds at the burst start (cum_sec of the last
        # segment completed before the burst) — separates segments completed
        # WITHIN the current burst from the pre-burst prefix.
        self._burst_base_sec = 0.0
        # True only for the turn's ORIGINAL first burst (before any pause→
        # resume). The in-flight-segment played-fraction estimate is safe
        # ONLY here (the first segment is short; a resumed remainder is one
        # huge segment whose sent-so-far is far below its total — a fraction
        # of it would skip unheard content).
        self._burst_is_first = True
        # True while the TTS consumer is inside a segment's play loop (even
        # when the provider stalls between chunks). The watchdog's stale
        # deadline must not clear _speaking mid-burst: a mid-turn speaking_end
        # makes the client stop reporting progress and a later speaking_start
        # restarts its burst clock, under-counting the next pause breakpoint
        # (observed live conv 964462a8 — the replay failure family).
        self._consumer_playing = False
        self._speaking_deadline = 0.0           # safety fallback to clear _speaking
        self._spoken_text_recent = ""           # tail of text currently spoken (echo detect)
        self._pending_interruption_note: Optional[str] = None
        # Text GENERATED so far in the current turn (vs. _turn_segments which
        # only records text whose audio was actually synthesized). Used by
        # _request_interrupt to attribute an abort that happened BEFORE any
        # audio was played (think-phase preemption).
        self._turn_reply_text = ""

        # Interjection (插话) mechanism — the agent can interject brief remarks
        # while the user is still speaking. ASR text is processed per-sentence:
        # each completed sentence (FunASR sentence_end) is sent to an
        # interjection subagent that decides whether to make a quick comment.
        # The agent has an emotional state that affects interjection frequency.
        self._emotion = "calm"                   # calm|interested|excited|upset|broken
        self._user_speech_sentences: list[str] = []  # completed sentences this turn
        self._interjection_offset = 0            # ASR text offset for sentence extraction
        self._turn_interjections: list[dict] = []  # [{sentence, text, emotion}]
        self._interjecting = False               # currently speaking an interjection
        self._last_interjection_time = 0.0       # for cooldown enforcement
        self._interjection_count_this_turn = 0
        self._interjection_checking = False      # subagent in-flight guard
        self._interjection_snapshot: list[dict] = []  # snapshot for turn context

        # Identity & memory (loaded once at startup from the user's Agent-mode
        # assistant + shared agent context, so 语音助理 mode inherits the same
        # persona, name, and long-term memory as Agent mode).
        self._identity_prompt = ""
        self._identity_loaded = False
        self._identity_task: Optional[asyncio.Task] = None
        # ---- 每轮异步记忆召回 + 记忆插话（vmem）----
        self._vmem_block_ctx = ""                       # 最新召回上下文（""=尚无成功召回）
        self._vmem_recall_inflight: set = set()         # 进行中的召回任务（强引用防 GC）
        self._vmem_last_ids: set = set()                # 已插话过的记忆 id（全会话去重）
        # 预算/冷却为进程级 per-user registry（_vmem_budget）——重连不重置；
        # 会话级不再持有计数，避免双源漂移。
        self._vmem_baseline = ""                        # 会话启动时哨兵区块的基线内容（空召回还原）
        self._vmem_gen_done = asyncio.Event()           # 本轮生成结束信号（插话仲裁等待，超时封顶）
        self._vmem_gen_done_epoch = 0                   # _generate_and_speak 退出时的 epoch
        self._vmem_answer_epoch = 0                     # _last_answer_text 属于哪一轮（防跨轮错配）
        self._last_answer_text = ""                     # 上一轮完整回答（仲裁输入）
        # The user's 语音助理 assistant row (loaded with the identity). Its
        # own model config, when set, takes precedence over the global
        # [voice] block in _build_llm — voice sessions then behave like
        # agent-mode: per-assistant instead of globally shared.
        self._voice_assistant = None

        # Tool-call dispatch context (lazily initialised on first tool call).
        # Agent mode dispatches tools with the user's workspace + current
        # conversation; 语音助理 mode must do the same so execute_code runs in the
        # user workspace (not the backend cwd) and pdf_export can default to
        # the current voice conversation.
        self._workspace_path: Optional[str] = None
        self._conversation = None

        # Long-task UX (2026-07-22): tool-loop state for immediate user
        # acknowledgment, and the in-progress assistant message row so tool
        # activity survives a mid-loop disconnect (incremental persistence).
        self._tools_running = False
        self._turn_msg_id: Optional[str] = None
        # Voice delegation source anchor: id of the current turn's persisted
        # user Message (used for idempotency key voice:<message_id>).
        self._last_persisted_user_message_id: Optional[str] = None
        self._last_queue_ack = 0.0

        self._tasks: list[asyncio.Task] = []

    # ---- low-level send helpers ----



    # ---- LLM helpers ----















    # ---- subagents ----













    # ---- Interjection (插话) subagent ----



    # ---- 每轮异步记忆召回（vmem）：L0 哨兵区块 + watcher ----








    _EMOTION_DECAY = {
        "broken": "upset",
        "upset": "calm",
        "excited": "interested",
        "interested": "calm",
        "calm": "calm",
    }



    # ---- TTS consumer (pipelined synthesis, ordered playback) ----






















    # ---- turn handling ----




















    # ---- ASR pipeline / EoT watchdog ----




    # ---- lifecycle ----



