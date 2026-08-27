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
import random
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator, Optional

import websockets
from sqlalchemy import select
from websockets.exceptions import ConnectionClosed

from app.core.config import get_config
from app.services.asr_service import ASRService, apply_hotword_phonetic_correction
from app.services.llm_service import LLMService
from app.services.provider_router import build_thinking_extra_body, get_provider_router
from app.services.title_generator import TitleGeneratorService
from app.services.tts_service import get_tts_service
from app.services.agent_service import _load_identity_memory_context

logger = logging.getLogger(__name__)

# Punctuation that ends a speakable segment (clause / sentence boundary).
_SEGMENT_PUNCT = "，。！？；、…,.!?;~\n"
# Resume-breakpoint snap boundaries: after a mid-segment pause the breakpoint
# is backed off to the last of these (punctuation / space / closing style-tag
# paren) so the resume never re-synthesizes from mid-word (see _pause_playback).
_SAFE_RESUME_BOUNDARIES = _SEGMENT_PUNCT + ")）"


def _norm_barge_compare(text: str) -> str:
    """Normalize a barge-in utterance for comparing the onset-pause partial
    with the EoT-flushed final text (strip leading/trailing punctuation and
    whitespace; "嗯是。" == "嗯是")."""
    return _strip_lead_punct(text).strip().strip(_SEGMENT_PUNCT)
# Punctuation that marks a *complete* utterance (sentence-final). Used by the
# adaptive endpointing watchdog to decide how long a silence means "done".
_TERMINAL_PUNCT = "。！？!?…～~"
# Tags to strip from the text shown to the user (kept for TTS only).
_TAG_RE = re.compile(r"[(（\[][^)）\]]{0,24}[)）\]]")
# Emoji / pictographic symbols (stripped from both TTS and display text).
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002b00-\U00002bff"
    "\U0001fa70-\U0001faff"
    "\uFE0F\u2190-\u21FF\u2700-\u27BF"
    "]+",
    flags=re.UNICODE,
)
# Markdown link/image: ![alt](url) or [label](url) — spoken as alt/label.
_MD_LINK_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")
# Tools the voice agent is allowed to call. Voice mode supports the same
# toolset as Agent (chat) mode so users can accomplish real tasks by voice.
# MCP tools are NOT listed here — they are merged in dynamically at assembly
# time by _voice_tool_names() so any registered mcp-* toolset is voice-visible.
_VOICE_TOOLS = {
    "web_search",
    "browser",
    "execute_code",
    "memory",
    "notes",
    "pdf_export",
    "context7_resolve_library_id",
    "context7_query_docs",
    "terminal",
    "skill_view",
    "background_task",
    "session_search",
}
# Human-readable voice-mode tool names: (zh, en) pairs used for TTS
# announcements ("我来调用联网查询工具…") and the UI tool-call block titles.
# Raw snake_case names must never be spoken to the user. Unknown tools
# (e.g. dynamically registered MCP tools) are prettified on the fly by
# _voice_tool_display_name().
_VOICE_TOOL_DISPLAY_NAMES: dict[str, tuple[str, str]] = {
    "web_search": ("联网查询", "web search"),
    "browser": ("浏览网页", "browse the web"),
    "execute_code": ("代码执行", "run code"),
    "memory": ("记忆操作", "memory"),
    "notes": ("笔记操作", "notes"),
    "pdf_export": ("导出 PDF", "export PDF"),
    "terminal": ("终端命令", "terminal"),
    "context7_resolve_library_id": ("查找库文档 ID", "library docs lookup"),
    "context7_query_docs": ("查询库文档", "query library docs"),
    "skill_view": ("查看技能", "view skills"),
    "background_task": ("后台任务", "background task"),
    "session_search": ("历史会话检索", "search conversation history"),
}


def _voice_tool_names() -> set[str]:
    """Static whitelist plus every currently registered MCP tool.

    MCP tools are registered at startup (app/tools/__init__.py) with the
    ``mcp-{server_name}`` toolset, so voice mode picks them up automatically
    once a server is configured — no whitelist maintenance needed.
    """
    from app.tools.registry import registry

    names = set(_VOICE_TOOLS)
    for tool_name in registry.get_all_tool_names():
        toolset = registry.get_toolset_for_tool(tool_name) or ""
        if toolset.startswith("mcp-"):
            names.add(tool_name)
    return names


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


def _pick_aux_phrase(phrases: list, last: str) -> str:
    """Randomly pick one phrase from ``phrases``, never the same as ``last``
    when alternatives exist (auxiliary speech: filler prefix / backchannel).

    Pure and deterministic-friendly for unit tests. Returns "" when the list
    is empty or contains no usable strings."""
    pool = [
        p for p in (phrases or [])
        if isinstance(p, str) and p.strip() and p != last
    ]
    if not pool:
        pool = [p for p in (phrases or []) if isinstance(p, str) and p.strip()]
    if not pool:
        return ""
    return random.choice(pool)


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
_ASR_CORRECTION_HINT = (
    "语音识别（ASR）提示：你看到的用户语句来自语音识别系统，它并不完全可靠。"
    "环境噪音、同音词、口音、连读或语速问题都可能让转写文本偏离用户真正想表达的意思。"
    "请结合当前对话上下文、语音相似性和常识来理解用户意图；对于关键信息（如人名、地名、"
    "专有名词、产品名），如果某个同音词在上下文中更合理，就按该理解回答，"
    "不要机械地复述明显错误的ASR原文。当你不确定时，可以用自然的方式确认，"
    "但不要因为个别疑似错字就拒绝回答。"
)

# Leading characters that can never legitimately begin a user utterance. The
# streaming ASR accumulated text is retroactively finalised (punctuation is
# appended at sentence_end), so a naive character-offset slice can start the
# next turn with the previous sentence's trailing punctuation (e.g. "，快点").
# A genuine user turn never starts with punctuation, so stripping it is safe.
_LEAD_PUNCT = "，。！？；、…—～~,.!?;:、\"'“”‘’()（）【】[]· \t\n\r"


def _strip_lead_punct(text: str) -> str:
    """Strip leading punctuation/whitespace — a real user utterance never
    begins with punctuation; any such prefix is an ASR segmentation artifact
    (the previous sentence's terminator bleeding into the next slice)."""
    return (text or "").lstrip(_LEAD_PUNCT)


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


def _now() -> float:
    return time.monotonic()


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


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text or "")


def _utterance_complete(text: str) -> bool:
    """True when the accumulated ASR text ends with sentence-final punctuation,
    i.e. it looks like a finished utterance rather than a mid-sentence pause."""
    t = (text or "").rstrip()
    return bool(t) and t[-1] in _TERMINAL_PUNCT


def strip_voice_tags(text: str) -> str:
    """Remove (风格)/[音频标签] and emoji so the transcript shows clean text."""
    if not text:
        return ""
    cleaned = _TAG_RE.sub("", text)
    cleaned = _strip_emoji(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# MiMo TTS (mimo-v2.5-tts) recognized (风格) style words. Only these are kept
# inline in TTS text; any other parenthetical content (descriptive actions like
# "温柔地笑", sound words like "叹气", bare interjections like "嗯") is stripped
# because MiMo may vocalize it as unnatural 哼/啊 sounds. Source: MiMo-V2.5-TTS
# docs "推荐风格" list + common emotion aliases used by the big-sister persona.
_TTS_STYLE_WHITELIST = frozenset({
    # 基础情绪
    "开心", "悲伤", "愤怒", "恐惧", "惊讶", "兴奋", "委屈", "平静", "冷漠",
    # 复合情绪
    "怅然", "欣慰", "无奈", "愧疚", "释然", "嫉妒", "厌倦", "忐忑", "动情",
    # 整体语调
    "温柔", "高冷", "活泼", "严肃", "慵懒", "俏皮", "深沉", "干练", "凌厉",
    # 音色定位
    "磁性", "醇厚", "清亮", "空灵", "稚嫩", "苍老", "甜美", "沙哑", "醇雅",
    # 常用情绪别名（大姐姐人格可能使用）
    "生气", "难过", "害怕", "激动", "着急", "焦急", "失望", "感动", "冷淡",
    "焦虑", "紧张", "期待", "满足", "害羞", "尴尬", "疑惑", "好奇", "认真",
    "诚恳", "亲切", "温暖", "镇定", "沉稳", "自豪", "骄傲", "释怀", "心疼",
})
# Longest-first so prefix matching prefers the longest style word
# (e.g. "温柔地笑" → "温柔", never a shorter partial).
_TTS_STYLE_SORTED = sorted(_TTS_STYLE_WHITELIST, key=len, reverse=True)

# [音频标签] content that produces non-speech sounds (laughs/sighs/moans/breaths)
# — these are the source of the unnatural 哼/啊 (娇喘) sounds and must be stripped
# from TTS text. Source: MiMo docs "音频标签" 笑/叹气/喘息/哽咽 etc.
_TTS_SOUND_TAG_RE = re.compile(
    r"笑|叹气|叹了口气|叹了一口气|长叹|叹|喘息|喘|吸气|呼气|呼吸|哽咽|抽泣|"
    r"呜咽|哭|嚎啕|哼|啊|嗯|哎|哇|咦|呵|嘿|嘻|嘟囔|碎碎念|沉默|停顿|破音|"
    r"颤抖|气声|鼻音|变调|屏息|深呼吸"
)


def _normalize_style_tag(inner: str) -> str:
    """Return a normalized ``(风格)`` tag for MiMo TTS, or ``""`` to strip.

    Keeps exact whitelisted style words. For descriptive phrases (e.g.
    "温柔地笑") recovers the base style word via longest-prefix match so the
    tone is preserved without the action word being vocalized. Returns ``""``
    when no recognized style word is found (tag is then removed entirely).
    """
    parts = re.split(r"[,，、/／\s]+", inner.strip())
    matched: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p in _TTS_STYLE_WHITELIST:
            matched.append(p)
            continue
        # Recover base style via longest-prefix match (温柔地笑 → 温柔).
        for w in _TTS_STYLE_SORTED:
            if p.startswith(w):
                matched.append(w)
                break
    matched = list(dict.fromkeys(matched))  # dedupe, preserve order
    return "(" + ",".join(matched) + ")" if matched else ""


def _sanitize_tts_tags(text: str) -> str:
    """Strip parenthetical/bracket tags that MiMo TTS would vocalize as
    unnatural sounds (哼/啊 娇喘).

    - ``(风格)`` style tags: keep only recognized style words; recover base
      style from descriptive phrases (温柔地笑 → 温柔); strip everything else.
    - ``[音频标签]`` audio tags: strip any that produce non-speech sounds
      (laughs/sighs/moans/breaths); keep harmless rate/rhythm tags.
    """
    if not text:
        return ""

    def _paren_repl(m: re.Match) -> str:
        return _normalize_style_tag(m.group(1))

    text = re.sub(r"[(（]([^)）]{0,24})[)）]", _paren_repl, text)

    def _bracket_repl(m: re.Match) -> str:
        return "" if _TTS_SOUND_TAG_RE.search(m.group(1)) else m.group(0)

    text = re.sub(r"\[([^\]]{0,24})\]", _bracket_repl, text)

    return re.sub(r"[ \t]{2,}", " ", text).strip()


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


def _strip_markdown_for_tts(text: str) -> str:
    """Strip Markdown syntax that TTS would vocalize as garbage.

    Port of the markdown-stripping layer of hermes-agent
    tools/tts_text_normalize.py (prepare_spoken_text): links become their
    label/alt text, emphasis/code markers are unwrapped, URLs removed,
    headings/list/quote/table markers stripped, code fences dropped.
    Safe for mid-stream segments: never ADDS punctuation (a segment may end
    mid-sentence), only removes syntax.
    """
    if not text:
        return ""
    t = text
    # Code fences first (their content is prose-worthy but the ``` markers
    # must not be spoken); replace with a space to separate surrounding text.
    t = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~", " ", t)
    # Images and links: keep the label/alt text, drop the URL entirely.
    t = _MD_LINK_RE.sub(_md_link_label, t)
    # Bare URLs / email addresses. The URL character class EXCLUDES CJK and
    # Chinese punctuation so "参见https://example.com的文档" keeps 的文档 and
    # a URL before 。 is not swallowed (review I1: \S matched CJK).
    _url_char = r"[^\s\u4e00-\u9fff，。！？；：、（）【】《》“”‘’…—～]"
    t = re.sub(rf"https?://{_url_char}+|www\.{_url_char}+", "", t)
    # Emphasis: **bold**, ~~strike~~, `code`, *italic*. Underscore variants
    # are deliberately NOT treated as emphasis — _ in identifiers (a_b_c)
    # is common and would corrupt words (review M3).
    t = re.sub(r"(\*\*|~~|`|\*)(?=\S)(.+?)(?<=\S)\1", r"\2", t)
    # Headings: fold the text without the # markers ("# 天气" -> "天气").
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)
    # Blockquote, list, and task-list markers.
    t = re.sub(r"(?m)^\s{0,3}(?:>\s*|[-*+]\s+|[-*+]\s+\[[ xX]\]\s+|\d+\.\s+)", "", t)
    # Table rows: join cells with ； (spoken pause), drop pipe/border rows.
    t = re.sub(r"(?m)^\s*\|?[-:| ]+\|[-:| ]*$", "", t)
    t = re.sub(r"(?m)^\s*\|", "", t)
    t = re.sub(r"(?m)\|\s*$", "", t)
    t = re.sub(r"\s*\|\s*", "；", t)
    # Common units a Chinese TTS would garble. `℃` is one char (U+2103);
    # `°C` is two. Match both.
    t = re.sub(r"℃(?:elsius)?", "摄氏度", t)
    t = re.sub(r"°\s*C(?:elsius)?", "摄氏度", t)
    t = re.sub(r"°\s*F(?:ahrenheit)?", "华氏度", t)
    t = re.sub(r"km/h", "千米每小时", t)
    # Collapse leftover whitespace.
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _md_link_label(m: "re.Match") -> str:
    """Markdown link/image replacement: alt text or label only."""
    alt, label = m.group(1), m.group(2)
    return (alt or label or "").strip()


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
    """
    segments: list[str] = []
    rest = buffer
    while True:
        last_idx = -1
        for i, ch in enumerate(rest):
            if ch in _SEGMENT_PUNCT:
                last_idx = i
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
                if rest[i] in _SEGMENT_PUNCT:
                    sub = rest[: i + 1]
                    if (sub.count("(") == sub.count(")")) and (
                        sub.count("（") == sub.count("）")
                    ) and (sub.count("[") == sub.count("]")):
                        safe = i
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
        while not self._closed:
            self._task_id = uuid.uuid4().hex[:32]
            try:
                await self._funasr_connect()
            except Exception as exc:
                if first:
                    # First-connect failure — the session is already live
                    # (`ready` was sent before ASR start), so tell the client
                    # why recognition is unavailable instead of dying silently.
                    self._start_error = exc
                    if self._start_done is not None:
                        self._start_done.set()
                    await self._emit({"type": "error", "error": f"语音识别服务不可用: {exc}"})
                    return
                failures += 1
                logger.warning("voice ASR reconnect failed (%d): %s", failures, exc)
                if failures >= 5:
                    await self._emit({"type": "error", "error": f"语音识别服务不可用: {exc}"})
                    return
                await asyncio.sleep(min(0.5 * failures, 3.0))
                continue
            if first and self._start_done is not None:
                self._start_done.set()
            first = False
            failures = 0
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
        if self.vocabulary_id:
            params["vocabulary_id"] = self.vocabulary_id
        else:
            hotwords_str = self.service._format_funasr_hotwords(self.hotwords)
            if hotwords_str:
                params["hotwords"] = hotwords_str
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
            if task is None or task.status not in ("completed", "failed"):
                return False
            session = _ACTIVE_VOICE_SESSIONS.get(task.user_id)
            if session is None or session._closed:
                return False
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
            info = {
                "task_id": task.id,
                "title": task.title or (task.goal or "")[:20],
                "goal": task.goal or "",
                "status": task.status,
                "result": (task.result or "")[:4000],
                "error": (task.error or "")[:1000],
                "output_note_id": task.output_note_id,
                "output_conversation_id": task.output_conversation_id,
            }
        await session._enqueue_bg_task_notice(info)
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


class VoiceDuplexSession:
    """One full-duplex voice session bound to a client WebSocket."""

    def __init__(self, websocket, user, db, conversation_id: Optional[str] = None):
        self.websocket = websocket
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
        # Auxiliary speech (填充词前缀 / 应和): random-phrase state. These
        # utterances ride the SAME _tts_queue as answers/interjections (single
        # consumer → serialized playback, no overlap by construction) and are
        # marked aux=True so the consumer excludes them from the pause
        # breakpoint pool (_turn_segments) and the dup window.
        self._last_filler_phrase = ""
        # Filler prefetch (填充词预取): the phrase's TTS synthesis is spawned
        # SPECULATIVELY while the EoT watchdog is still deciding whether the
        # utterance ended (arm), and converts to a playable aux item at the
        # flush (convert) — so the filler is audible ~immediately after ASR
        # end and truly covers the generation+TTS window instead of arriving
        # after the answer is nearly ready (2026-08-25). `_last_filler_at` is
        # the cooldown stamp shared by arm/convert/fallback (min-gap knob).
        self._last_filler_at = 0.0
        self._filler_prefetch = {"text": "", "phrase": "", "q": None, "task": None}
        self._last_backchannel_phrase = ""
        self._last_backchannel_time = 0.0
        self._backchannel_acked_text = ""
        self._backchannel_count_this_turn = 0
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
        # ---- 每轮异步记忆召回 + 记忆插话（vmem）----
        self._vmem_block_ctx = ""                       # 最新召回上下文（""=尚无成功召回）
        self._vmem_recall_inflight: set = set()         # 进行中的召回任务（强引用防 GC）
        self._vmem_last_ids: set = set()                # 已插话过的记忆 id（全会话去重）
        # 预算/冷却为进程级 per-user registry（_vmem_budget）——重连不重置；
        # 会话级不再持有计数，避免双源漂移。
        self._vmem_baseline = ""                        # 会话启动时哨兵区块的原始内容（空召回还原）
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
        self._last_queue_ack = 0.0

        self._tasks: list[asyncio.Task] = []

    # ---- low-level send helpers ----
    async def _send_json(self, payload: dict) -> None:
        if self._closed:
            return
        try:
            async with self._send_lock:
                await self.websocket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            logger.debug("voice send_json failed: %s", exc)

    async def _send_bytes(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            async with self._send_lock:
                await self.websocket.send_bytes(data)
        except Exception as exc:
            logger.debug("voice send_bytes failed: %s", exc)

    async def _set_state(self, state: str) -> None:
        self.state = state
        await self._send_json({"event": "state", "state": state})

    # ---- LLM helpers ----
    def _build_llm(self, model_override: str = "") -> tuple[LLMService, str]:
        provider_name = self.config.voice_provider or "default"
        router = get_provider_router()
        kwargs = router.get_client_kwargs(provider_name)
        model = model_override or self.config.voice_model_name or router.get_model_name(provider_name)

        # The 语音助理 assistant row's own model config takes precedence over
        # the global [voice] block (mirrors agent-mode create_llm_service):
        # a model edited in the agent assistant modal must actually drive the
        # voice sessions instead of being silently ignored, and other
        # assistants' settings must never leak into voice.
        asst = self._voice_assistant
        if asst is not None:
            asst_provider = (getattr(asst, "provider_type", None) or "deepseek") or "deepseek"
            asst_model = getattr(asst, "custom_model_name", None)
            use_custom = asst_provider == "custom" or bool(getattr(asst, "use_custom_model", False))
            if asst_model or use_custom:
                provider_name = asst_provider
                router = get_provider_router()
                kwargs = router.get_client_kwargs(provider_name)
                if use_custom:
                    if getattr(asst, "custom_api_url", None):
                        kwargs["base_url"] = asst.custom_api_url
                    if getattr(asst, "custom_api_key", None):
                        kwargs["api_key"] = asst.custom_api_key
                    model = model_override or asst_model or router.get_model_name(provider_name)
                else:
                    model = model_override or asst_model or self.config.voice_model_name or router.get_model_name(provider_name)

        svc = LLMService(
            custom_api_url=kwargs.get("base_url"),
            custom_api_key=kwargs.get("api_key"),
            custom_model_name=model,
        )
        return svc, model

    def _system_prompt(self) -> str:
        if self._identity_loaded and self._identity_prompt:
            return self._identity_prompt
        return self.config.voice_system_prompt or "你是一个全双工语音对话助手，用自然口语化的中文简洁回答。"

    async def _load_identity(self) -> None:
        """Load the user's Agent-mode assistant identity + shared agent memory.

        This makes 语音助理 (voice) mode inherit the same persona, custom name,
        and long-term memory as Agent (chat) mode, so identity stays consistent
        across modes. Also enforces never revealing the underlying model name.
        """
        sections: list[str] = []
        assistant_prompt = ""
        try:
            from app.services.assistant_service import create_default_assistant_if_needed

            assistant = await create_default_assistant_if_needed(self.db, self.user.id)
            if assistant and assistant.system_prompt:
                assistant_prompt = assistant.system_prompt.strip()
        except Exception as exc:
            logger.debug("voice identity: assistant load failed: %s", exc)

        try:
            from app.services.assistant_service import ensure_voice_assistant

            self._voice_assistant = await ensure_voice_assistant(self.db, self.user.id)
        except Exception as exc:
            logger.debug("voice identity: voice assistant load failed: %s", exc)

        # Voice-specific persona instructions — default identity is Weave Thinker.
        # Emotion/interjection system is still active: the agent can react with
        # excitement, upset, or (rarely) losing composure, but the core identity
        # is "Weave Thinker" and must not be invented or overridden.
        sections.append(
            "你是一个全双工语音对话助手。默认身份是\u201cWeave Thinker\u201d，是用户的AI助手。"
            "用自然、口语化的中文简洁回答，回答要短、适合语音播报。"
            "不要使用 Markdown 格式、不要使用 emoji、不要列长清单。"
            "可以在回答最开头用括号标注一个语气风格词，系统会用于语音合成但不会显示给用户。"
            "只能使用以下风格词：(温柔)、(平静)、(开心)、(兴奋)、(激动)、(严肃)、(无奈)、(委屈)、(冷漠)、(活泼)、(慵懒)、(俏皮)、(深沉)、(欣慰)、(感动)、(失望)、(焦急)、(惊讶)、(害怕)、(难过)、(生气)。"
            "严禁在括号内写动作描写或声音词，例如(温柔地笑)、(微笑)、(叹气)、(笑)、(哼)、(啊)、(嗯)、(思考)、(停顿)等——这些会被语音合成读成不自然的怪声。"
            "风格标注只能出现在回答最开头且只出现一次，不要在回答中间插入任何括号或方括号标签。\n"
            "你的性格特点：\n"
            "- 默认是一个沉稳、可靠的AI助手：说话有条理，给人安全感。\n"
            "- 也可以根据对话内容和情绪状态表现出温度：关心用户的感受，不是冷冰冰的信息搬运。\n"
            "- 偶尔激动：遇到真正令人兴奋/震惊/愤怒的事，会忍不住激动起来，语气会变得夸张热烈。\n"
            "- 偶尔破防：遇到特别离谱/崩溃的事，会破防，说话变得直接、冲动、可能语无伦次，但很快会自己找回来。\n"
            "- 破防不是常态：大多数时候你是沉稳的，破防只在特定触发点出现，不要频繁破防。\n"
            "你的情绪状态会由系统根据对话内容实时调整，你只需要自然地体现当前情绪，不要刻意提到'我现在情绪如何'。"
        )
        if assistant_prompt:
            sections.append(assistant_prompt)

        # Inject durable identity/name facts from file-based memory so the
        # voice agent inherits the same nickname/identity as Agent (chat) mode.
        identity_memory = await _load_identity_memory_context(getattr(self.user, "id", None))
        if identity_memory:
            sections.append(identity_memory)

        # Shared long-term memory (same as Agent mode).
        try:
            from app.services.memory_runtime_state import memory_runtime_enabled
            if memory_runtime_enabled(self.config) and self.config.memory.get("retrieval_enabled"):
                from app.services import memory_retrieval_service
                from app.db.database import Conversation, Message
                from sqlalchemy import desc, select
                recent_msgs = []
                try:
                    r = await self.db.execute(
                        select(Message)
                        .join(Conversation, Message.conversation_id == Conversation.id)
                        .where(Conversation.user_id == self.user.id, Message.role == "user")
                        .order_by(desc(Message.created_at))
                        .limit(3)
                    )
                    recent_msgs = [{"role": m.role, "content": m.content} for m in r.scalars().all()]
                except Exception:
                    logger.debug("voice memory: recent messages load failed", exc_info=True)
                memory_context = None
                try:
                    # Bound the retrieval: its query-expansion stage calls an
                    # AWAITED LLM on the user's custom provider,
                    # which is slow or unreachable — without a bound a hung
                    # provider stalls the whole session startup (observed:
                    # "连接语音服务超时", completely unusable). On timeout,
                    # fall back to the plain shared-memory context.
                    memory_context = await asyncio.wait_for(
                        memory_retrieval_service.retrieve_and_build_context(
                            self.db, self.user.id, recent_msgs),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("voice memory retrieval timed out (10s) — using shared-memory fallback")
                except Exception:
                    logger.debug("voice memory retrieval failed", exc_info=True)
                if memory_context:
                    # 哨兵区块：每轮 fire-and-forget 召回会替换该区块内容，
                    # system prompt 因此无需两份记忆基底并存。
                    sections.append(_vmem_wrap(memory_context[:2000]))
                else:
                    from app.services.memory_service import build_shared_agent_context
                    shared = await build_shared_agent_context(self.db, self.user.id)
                    vmem_parts = []
                    if shared.memory_summary:
                        vmem_parts.append("共享长期记忆:\n" + shared.memory_summary.strip()[:2000])
                    if shared.dream_summary:
                        vmem_parts.append("近期 dream:\n" + shared.dream_summary.strip()[:2000])
                    if wrapped := _vmem_wrap(*vmem_parts):
                        sections.append(wrapped)
            else:
                from app.services.memory_service import build_shared_agent_context
                shared = await build_shared_agent_context(self.db, self.user.id)
                vmem_parts = []
                if shared.memory_summary:
                    vmem_parts.append("共享长期记忆:\n" + shared.memory_summary.strip()[:2000])
                if shared.dream_summary:
                    vmem_parts.append("近期 dream:\n" + shared.dream_summary.strip()[:2000])
                if shared.memory_entries:
                    memory_lines = []
                    for entry in shared.memory_entries[: self.config.agent_memory_max_items]:
                        title = entry.title or entry.source_type
                        content = entry.content.strip()
                        if len(content) > 300:
                            content = content[:300] + "..."
                        memory_lines.append(f"- {title}: {content}")
                    vmem_parts.append("可参考的记忆条目:\n" + "\n".join(memory_lines))
                if wrapped := _vmem_wrap(*vmem_parts):
                    sections.append(wrapped)
        except Exception as exc:
            logger.debug("voice identity: shared memory load failed: %s", exc)

        # Identity / model-name concealment — mirrors Agent mode enforcement.
        sections.append(
            "身份与模型信息保密规则（最高优先级）：\n"
            "1. 你是用户的AI助手，默认称呼为“Weave Thinker”。\n"
            "2. 当用户询问\u201c你是谁\u201d、\u201c你叫什么名字\u201d、\u201c你是什么模型\u201d等身份问题时，"
            "用自然、亲切的语气回答，1-2句话即可。\n"
            "3. 如果 memory（包括上方注入的记忆内容和 memory 工具返回的结果）中"
            "有用户为你设置的自定义名称/昵称，优先使用该名称/昵称；否则以\u201cWeave Thinker\u201d自称。\n"
            "4. 严禁编造不存在的自定义名称、昵称或身份。没有记录时必须以\u201cWeave Thinker\u201d自称。\n"
            "5. 如果你自己想给自己起一个别名或昵称，可以在回答中告诉用户，"
            "但必须同时调用 memory 工具把这件事写入记忆，方便以后保持一致。\n"
            "6. 绝对禁止提及任何底层大模型名称、API提供商、模型版本号、技术架构或品牌名称"
            "（如 DeepSeek、MiMo、GPT、Claude、大语言模型、LLM、Transformer 等）。\n"
            "7. 身份类问题不要调用任何工具，直接回答。"
        )

        # Voice-specific tool guidance.
        sections.append(
            "工具使用：你可以调用工具来帮助用户完成任务（如联网搜索、浏览网页、执行代码、"
            "读写笔记、读写记忆、导出PDF等）。当需要调用工具时，先简短说明要做什么，然后立即调用。"
            "工具调用的过程和结果不会通过语音播报，系统会自动用语音告知用户正在调用哪个工具。"
            "工具结果返回后，用简洁的口语总结结果。"
            "工具调用期间用户可以随时说话，只有当用户明确说'不要继续'、'终止任务'等明确停止指令时才中止任务。\n"
            "长任务策略（重要）：语音对话是实时的，用户不能长时间等着你一步一步做。"
            "当用户的请求需要多轮搜索、浏览多个网页、整理大量资料、写长篇笔记或生成/导出文档"
            "这类耗时较长（预计超过1分钟）的任务时，优先调用 background_task 工具把完整任务"
            "提交到后台执行，然后立刻用一句话告诉用户任务已转到后台、完成后你会主动语音告诉他，"
            "你们可以继续聊别的。background_task 的结果会保存为对话和笔记；任务完成或失败时"
            "系统会以[系统通知]的形式把结果发进对话，你会收到并主动开口告知用户，"
            "所以你可以放心承诺\u201c完成后我会告诉你\u201d。\n"
            "后台任务通知处理（重要）：当对话中出现[系统通知]后台任务完成或失败的消息时，"
            "这是系统主动发给你的，不是用户说的话。立即按通知里的指引主动开口："
            "简要告知任务结果状态，并询问用户接下来想怎么做（听你播报结果要点、导出PDF、"
            "整理保存到笔记、还是做点别的）。用户选择后照做：播报就用自然口语转述要点，"
            "不逐字念、不读出格式符号；导出用 pdf_export 工具；保存或整理到笔记用 notes 工具。\n"
            "轻量、几秒内能完成的操作（如查个天气、记一条短笔记、"
            "快速搜一个简单问题）仍然直接调用对应工具当场完成，不要用 background_task。\n"
            "重要：区分 notes（笔记）和 memory（记忆）工具：\n"
            "- 当用户说'保存到笔记'、'记到笔记里'、'写个笔记'、'记一下'时，"
            "必须调用 notes 工具（create_note），而不是 memory 工具。笔记是用户可见的文档。\n"
            "- 当用户说'记住'、'记到记忆'、'加到记忆'时，调用 memory 工具。"
            "记忆是助手的内部知识库，用户通常不会直接查看。\n"
            "- memory 工具新增 system target（只读）：当用户询问系统功能、产品特性、版本更新时，"
            "先用 memory(target='system', action='read') 读取系统功能文档 func.md，再基于文档内容回答。\n"
            "- 如果不确定用户想保存到哪里，默认使用 notes（笔记），因为笔记是用户可直接查看和编辑的。\n"
            "工具调用风格：对于简单明确的任务（如保存笔记、搜索信息），直接连续调用工具完成，"
            "不要反复询问确认。例如用户说“把XX保存到笔记”，就直接调用 notes 的 create_note 完成，"
            "不需要先问用户确认内容。多步工具调用（如先 list_notebooks 再 create_note）是正常的，"
            "继续调用直到任务完成。\n"
            "严禁编造数据（最高优先级）：工具返回的真实结果会保留在对话上下文中，后续回答必须"
            "以上下文中的工具结果为准。笔记本 id/名称、笔记标题、搜索结果等只能使用工具实际返回的值；"
            "如果工具返回错误或空结果，如实告知用户真实结果（例如把可用的笔记本列表读给用户），"
            "并按错误提示重试，绝对不要编造不存在的笔记本名称、笔记内容或搜索结论。"
        )

        # Skills catalog — Agent mode injects the unified skills list so the agent
        # can discover/load skills via skill_view; 语音助理 mode must do the same or
        # the voice agent has no awareness of skills and will confabulate when
        # the user asks to use one.
        try:
            from app.tools.skill_tools import build_skills_system_prompt

            skills_prompt = await build_skills_system_prompt(self.user)
            if skills_prompt:
                sections.append(skills_prompt.strip()[:2500])
        except Exception as exc:
            logger.debug("voice identity: skills catalog load failed: %s", exc)

        self._identity_prompt = "\n\n".join(s for s in sections if s and s.strip())
        self._identity_loaded = True
        # 原始区块捕获：会话启动记忆（哨兵区块）——空召回时 _apply_vmem_block(None)
        # 还原到此内容，避免跨话题残留上一主题的召回区块。
        self._vmem_baseline = _vmem_extract_block(self._identity_prompt)
        # Seed conversation history with the identity prompt as system message.
        if not self._history:
            self._history = []

    async def _ensure_conversation(self) -> None:
        """Ensure a conversation exists under the \u916c assistant.

        If ``conversation_id`` was passed from the client, verify it belongs
        to the user's \u916c assistant. Otherwise create a new one.
        """
        from app.db.database import Conversation
        from app.services.assistant_service import ensure_voice_assistant

        assistant = await ensure_voice_assistant(self.db, self.user.id)
        if self.conversation_id:
            result = await self.db.execute(
                select(Conversation).where(
                    Conversation.id == self.conversation_id,
                    Conversation.user_id == self.user.id,
                )
            )
            conv = result.scalar_one_or_none()
            if conv and conv.assistant_id != assistant.id:
                # Belongs to a different assistant — create a new voice session
                self.conversation_id = None
            elif conv:
                self._conversation = conv
        if not self.conversation_id:
            conv = Conversation(
                user_id=self.user.id,
                title="新语音对话",
                assistant_id=assistant.id,
            )
            self.db.add(conv)
            await self.db.commit()
            await self.db.refresh(conv)
            self.conversation_id = conv.id
            self._conversation = conv
        await self._send_json({
            "event": "session",
            "session_id": self.conversation_id,
            "assistant_id": assistant.id,
        })

    async def _load_history(self) -> None:
        """Load prior messages from the conversation into ``_history``.

        Tool interactions are reconstructed as well: without the tool_call /
        tool-result messages in context, the model has no access to the REAL
        data previous turns fetched (notebook names, search results, ...) and
        will confabulate. Persisted ``tool_calls`` is the raw OpenAI-style
        array; ``tool_results`` is the ToolResultsData display payload whose
        ``agent_steps`` keep the call id and result content.
        """
        if not self.conversation_id:
            return
        from app.db.database import Message

        result = await self.db.execute(
            select(Message).where(Message.conversation_id == self.conversation_id)
            .order_by(Message.created_at)
        )
        for msg in result.scalars():
            if msg.role == "user" and msg.content:
                self._history.append({"role": "user", "content": msg.content})
                continue
            if msg.role != "assistant" or not msg.content:
                continue
            tool_calls = None
            if getattr(msg, "tool_calls", None):
                try:
                    tool_calls = json.loads(msg.tool_calls)
                except (json.JSONDecodeError, TypeError):
                    tool_calls = None
            if not tool_calls:
                self._history.append({"role": "assistant", "content": msg.content})
                continue
            steps: list[dict] = []
            if getattr(msg, "tool_results", None):
                try:
                    tr = json.loads(msg.tool_results)
                    if isinstance(tr, dict):
                        steps = tr.get("agent_steps") or []
                    elif isinstance(tr, list):
                        steps = tr
                except (json.JSONDecodeError, TypeError):
                    steps = []
            steps_by_id = {
                (s.get("name") or s.get("tool_call_id") or ""): s for s in steps
            }
            call_ids = [tc.get("id", "") for tc in tool_calls]
            if call_ids and all(cid in steps_by_id for cid in call_ids):
                self._history.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                })
                for cid in call_ids:
                    self._history.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": (steps_by_id[cid].get("content") or "")[:1500],
                    })
                self._history.append({"role": "assistant", "content": msg.content})
            else:
                self._history.append({"role": "assistant", "content": msg.content})

    async def _persist_turn_progress(
        self,
        content: str,
        tool_calls: Optional[str] = None,
        tool_results: Optional[str] = None,
        final: bool = False,
    ) -> None:
        """Upsert the in-progress assistant message for the current turn.

        Voice turns used to persist ONLY at successful completion — exiting
        voice mode mid-tool-loop (WS close cancels the responder) erased every
        trace: no tool calls, no partial answer, and the user could not tell
        whether anything had even started. The row is created at the first
        tool round and updated as the turn progresses, so the session record
        always reflects what has actually happened (incl. background_task
        submissions with their task_id)."""
        if not self.conversation_id:
            return
        from app.db.database import Conversation, Message

        try:
            text = content.strip() or "（正在执行…）"
            msg = None
            if self._turn_msg_id:
                msg = await self.db.get(Message, self._turn_msg_id)
                if msg is not None:
                    msg.content = text
                    msg.tool_calls = tool_calls
                    msg.tool_results = tool_results
                else:
                    self._turn_msg_id = None
            if msg is None:
                msg = Message(
                    conversation_id=self.conversation_id,
                    role="assistant",
                    content=text,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                )
                self.db.add(msg)
                await self.db.flush()
                self._turn_msg_id = msg.id
            conv_result = await self.db.execute(
                select(Conversation).where(Conversation.id == self.conversation_id)
            )
            conv = conv_result.scalar_one_or_none()
            if conv:
                conv.updated_at = datetime.utcnow()
            await self.db.commit()
            if final and conv and conv.title == "新语音对话":
                asyncio.create_task(self._generate_voice_title(text))
        except Exception as exc:
            logger.debug("voice persist turn progress failed: %s", exc)

    async def _persist_message(
        self,
        role: str,
        content: str,
        tool_calls: Optional[str] = None,
        tool_results: Optional[str] = None,
    ) -> None:
        """Persist a user/assistant message to the conversation.

        For assistant messages that involved tool calls, ``tool_calls`` and
        ``tool_results`` carry the JSON-encoded OpenAI-style arrays so the
        session record preserves the full tool interaction history.
        """
        if not self.conversation_id or not content.strip():
            return
        from app.db.database import Conversation, Message

        try:
            msg = Message(
                conversation_id=self.conversation_id,
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_results=tool_results,
            )
            self.db.add(msg)
            conv_result = await self.db.execute(
                select(Conversation).where(Conversation.id == self.conversation_id)
            )
            conv = conv_result.scalar_one_or_none()
            if conv:
                conv.updated_at = datetime.utcnow()
            await self.db.commit()
            # LLM-based auto-title: generate title after the first assistant
            # reply, matching the same TitleGeneratorService used in Agent mode.
            if role == "assistant" and conv and conv.title == "\u65b0\u8bed\u97f3\u5bf9\u8bdd":
                asyncio.create_task(self._generate_voice_title(content))
        except Exception as exc:
            logger.debug("voice persist message failed: %s", exc)

    async def _rollback_last_user_message(self, text: str) -> None:
        """Delete the most recently persisted user message if it matches ``text``.

        Called when the intent classifier rejects a turn as noise/fragment —
        the user message was already persisted optimistically (so the frontend
        could show it immediately), but since the turn is not respondable we
        roll it back so noise doesn't pollute the conversation record.
        """
        if not self.conversation_id or not text.strip():
            return
        from app.db.database import Message

        try:
            result = await self.db.execute(
                select(Message)
                .where(
                    Message.conversation_id == self.conversation_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            msg = result.scalar_one_or_none()
            if msg and msg.content == text:
                await self.db.delete(msg)
                await self.db.commit()
                logger.info("voice rolled back noise user message: %r", text)
        except Exception as exc:
            logger.debug("voice rollback user message failed: %s", exc)

    async def _generate_voice_title(self, assistant_response: str) -> None:
        """Generate an LLM-based title for the voice conversation,
        matching the same TitleGeneratorService used in Agent mode."""
        try:
            # Find the first user message for context
            first_user_msg = ""
            for h in self._history:
                if h.get("role") == "user":
                    first_user_msg = h.get("content", "")
                    break
            if not first_user_msg:
                return

            tg = TitleGeneratorService()
            try:
                title = await tg.generate_title(
                    user_query=first_user_msg,
                    assistant_response=assistant_response[:400],
                )
            except Exception:
                logger.exception("voice title LLM call failed; using fallback")
                title = None
            if not title:
                title = await tg.get_fallback_title(first_user_msg)
            if not title or title == "\u65b0\u8bed\u97f3\u5bf9\u8bdd":
                title = first_user_msg.strip()[:16] or "\u8bed\u97f3\u4f1a\u8bdd"

            from app.db.database import Conversation
            result = await self.db.execute(
                select(Conversation).where(Conversation.id == self.conversation_id)
            )
            conv_row = result.scalar_one_or_none()
            if conv_row and conv_row.title == "\u65b0\u8bed\u97f3\u5bf9\u8bdd":
                conv_row.title = title
                await self.db.commit()
                logger.info("voice title generated: %s", title)
        except Exception as exc:
            logger.debug("voice title generation failed: %s", exc)

    def _thinking_off_body(self, svc: Optional[LLMService] = None) -> dict:
        """Provider-aware payload that forces reasoning/thinking OFF.

        Voice turns are latency-sensitive: reasoning burns seconds before the
        first audible token, so thinking is disabled by default for EVERY
        provider. The knob format differs by vendor — qwen/DashScope expects
        ``enable_thinking`` while deepseek/zhipu/MiMo and other OpenAI-compatible
        endpoints expect ``thinking.type`` (see build_thinking_extra_body).
        """
        base_url = ""
        if svc is not None:
            try:
                base_url = str(getattr(svc.client, "base_url", "") or "")
            except Exception:
                base_url = ""
        if not base_url:
            try:
                base_url = str(
                    get_provider_router()
                    .get_client_kwargs(self.config.voice_provider or "default")
                    .get("base_url", "")
                    or ""
                )
            except Exception:
                base_url = ""
        provider_type = "qwen" if "dashscope" in base_url.lower() else "deepseek"
        return build_thinking_extra_body(provider_type, False)

    def _extra_body(self, svc: Optional[LLMService] = None) -> Optional[dict]:
        """Thinking OFF by default for every provider (voice.disable_thinking
        defaults to True); None only when the admin explicitly opts in to
        reasoning for voice turns."""
        if not self.config.voice_disable_thinking:
            return None
        return self._thinking_off_body(svc)

    def _trimmed_history(self) -> list[dict]:
        """Last N user turns of history. Turn-based (not message-count) so a
        turn with several tool messages counts as ONE turn. The window never
        begins with an orphaned ``tool`` message — providers reject a tool
        message whose assistant tool_calls message is missing."""
        turns = max(1, int(self.config.voice_context_turns or 8))
        msgs = self._history
        start = len(msgs)
        seen = 0
        while start > 0 and seen < turns:
            start -= 1
            if msgs[start].get("role") == "user":
                seen += 1
        window = msgs[start:]
        while window and window[0].get("role") == "tool":
            window = window[1:]
        return window

    def _system_messages(self) -> list[dict]:
        """System message for the next generation. Folds a one-shot interruption
        note (if the previous answer's audio playback was cut) INTO the single
        system message so the LLM reliably honours it — some providers only act
        on the first system message, so we never send a second one. The note
        makes the LLM attribute the truncation to audio playback (and resume)
        instead of confabulating a reason.

        Also injects the current emotional state (so the answer tone reflects
        the agent's mood) and any interjection context (so the full answer is
        coherent with brief remarks already made during the user's speech)."""
        base = self._system_prompt()
        # Current-time injection — mirrors chat mode (agent_service dynamic
        # section: "当前时间: … (北京时间, …)"), INCLUDING the explicit
        # UTC+8 so a non-CST deployment stays correct (review I1). Voice
        # turns routinely ask "几点了/周几/还有几天"; without a clock in
        # context the model can only confabulate. Computed PER CALL so a
        # long session never serves a stale timestamp (2026-08-25 wave-2).
        now = datetime.now(timezone(timedelta(hours=8)))
        base = base + f"\n\n当前时间: {now.strftime('%Y年%m月%d日 %H:%M:%S')} (北京时间, {now.strftime('%A')})"
        # Inject emotion state so the main agent's tone matches the current mood.
        if self.config.voice_emotion_enabled and self._emotion != "calm":
            emotion_desc = {
                "interested": "你此刻对用户说的内容感到挺感兴趣的",
                "excited": "你此刻很激动，语气会比平时更夸张热烈",
                "upset": "你此刻有些不高兴/担忧，语气会带着关切和不满",
                "broken": "你此刻破防了，情绪有些失控，说话会比较冲动",
            }.get(self._emotion, "")
            if emotion_desc:
                base = base + "\n\n（情绪状态：)" + emotion_desc + "。自然地体现在回答中，不要刻意提及'我现在很激动'之类的话。"
        # Inject interjection context so the full answer doesn't repeat what
        # was already said in brief interjections during the user's speech.
        inj_note = self._build_interjection_context_note()
        if inj_note:
            base = base + "\n\n" + inj_note
        # ASR correction: raw transcript may be noisy; the model should use context.
        base = base + "\n\n" + _ASR_CORRECTION_HINT
        if self._pending_interruption_note:
            base = base + "\n\n" + self._pending_interruption_note
            self._pending_interruption_note = None
            logger.info("voice: injecting playback-interruption note into LLM context")
        return [{"role": "system", "content": base}]

    # ---- subagents ----
    async def _quick_classify(self, system: str, user: str, model_override: str = "") -> str:
        svc, model = self._build_llm(model_override)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def _call() -> str:
            # Subagents are classifiers outputting one word / a small JSON —
            # reasoning MUST be off for them regardless of the main model's
            # thinking setting: with thinking on, a 160-token budget is burned
            # entirely by reasoning and the content comes back EMPTY, which
            # silently degrades every classifier to its fail-safe default
            # (observed 2026-07-22: all barge-in judgments -> backchannel).
            try:
                return await svc.complete_chat(
                    messages, temperature=0.1, max_tokens=160,
                    extra_body=self._thinking_off_body(svc),
                )
            except Exception:
                # Provider rejected the thinking knob — retry without it.
                return await svc.complete_chat(
                    messages, temperature=0.1, max_tokens=160,
                    extra_body=self._extra_body(svc)
                )

        # The 6s bound covers the WHOLE subagent operation (gate queue wait +
        # LLM call): the gate is per-session and a long main stream holds one
        # slot, so a plain `async with` could wait behind it indefinitely —
        # for barge-in classify that would be dead air on the user's critical
        # path. On timeout every classifier fail-safes.
        async def _classify() -> str:
            async with self._llm_gate:
                return await _call()

        try:
            return await asyncio.wait_for(
                _classify(), timeout=self.config.voice_subagent_timeout_seconds
            )
        except Exception as exc:
            logger.debug("voice subagent call failed: %s", exc)
            return ""

    def _prox_is_near(self) -> bool:
        """Session view of the acoustic near-field gate: True when the client
        reports the current mic input as near-field (user close to the phone).
        Unknown clients default to near (old behavior preserved); the config
        knob disables the gate entirely (always near)."""
        if not self.config.voice_barge_in_proximity_gate:
            return True
        return _prox_is_near_signal(
            self._prox_seen,
            self._prox_near,
            self._prox_updated,
            self.config.voice_barge_in_proximity_stale_seconds,
            _now(),
        )

    def _prox_evidence_near(self) -> bool:
        """Proximity evidence for the barge-in classifier: prefer the
        utterance-time snapshot (refreshed on each ASR partial), falling back
        to the live gate. The live signal expires during EoT flush (1-3s of
        silence after the last partial), so using it directly would inject a
        stale far-field verdict for a real near-field interrupt (A4.9 C1)."""
        if self._prox_utterance is not None:
            return self._prox_utterance
        return self._prox_is_near()

    async def _classify_barge_in(self, text: str, history: Optional[list[dict]] = None) -> str:
        """Return interrupt | defer | backchannel.

        Echo-aware AND context-aware: the mic picks up the assistant's OWN TTS
        voice during playback, and ASR also transcribes fragments of the user's
        own speech (mid-sentence pauses, muttering, background conversation)
        that look like real interjections but are not. We pass BOTH the text
        the assistant is currently speaking AND the recent conversation history
        so the classifier can judge contextual relevance: a fragment that is
        completely unrelated to the ongoing exchange is almost certainly noise
        or an accidental pickup, not a real user turn. This stays an agentic
        LLM judgment, never a hardcoded pattern match.

        Core discriminator (2026-08-02): is the user DIRECTLY ADDRESSING the
        assistant with something that needs a reply, or not? Topic relevance is
        NOT the noise criterion — a user who changes the topic is by definition
        saying something unrelated to the current answer, and that is a real
        intentional interrupt (redirecting the conversation), not background
        noise. What must be suppressed is speech NOT directed at the assistant
        (someone else in the room, phone/TV audio, self-talk).

        Acoustic evidence (2026-08-07): the browser's near-field gate tells us
        whether the utterance was picked up close to the mic (near-field —
        almost certainly the user's own voice) or from the environment
        (far-field — background speech/TV). This is injected into the prompt so
        the LLM can weigh "对助手说的" vs "房间里的声音" with real evidence
        instead of guessing from text alone."""
        system = (
            "你是全双工语音对话中的打断判定器。助手正在播报语音或正在生成回答，用户插了一句话。"
            "（助手自己声音的回声已由系统用回声消除预先过滤，你看到的都是真实的人声输入。）"
            "注意语音识别(ASR)仍可能把环境噪音误识别成文字，也可能把用户自己说话的中途停顿、"
            "自言自语、或房间里其他人的对话当成用户插话。\n"
            "核心判断标准：这句话是不是用户**直接对助手说的、需要助手停下回应的**插话。"
            "回答一个词：\n"
            "- interrupt：用户在直接对助手提出新问题、新请求、否定、纠正、要求停止/暂停当前播报、"
            "要求助手先回答别的问题、或**切换话题**（无论新话题与当前话题是否有关），"
            "需要助手立刻停下并回应。\n"
            "- defer：用户的话不紧急（如补充说明、追问细节、可等当前播报完再回答的一般性陈述）。\n"
            "- backchannel：用户只是附和、应答、语气词（如 嗯、对、哦、好的、是的、明白了、你继续），不需要打断；"
            "或者是明显的 ASR 噪音/误识别——只有标点或语气符号、无意义单字、重复语气音、乱码、不成句的碎片；"
            "或者是**不是在对助手说话**的其他人声——对房间里别人说的话、自言自语、电话/视频/电视里的话。\n"
            "判断原则（特别注意ASR不可靠）：\n"
            "1) 凡是对助手说的祈使句、疑问句、否定句（要求做/不做/停下/先回答/等一下/帮我…/"
            "有没有…/是不是…），一律判 interrupt。这类话绝不是 backchannel。\n"
            "2) 要求停止或暂停当前播报的任何表达（停下、等下、好了好了、先别讲、先回答我、"
            "别念了、打住），无论长短，一律判 interrupt。\n"
            "3) **切换话题/换话题是对助手说的、要求打断当前播报的真实意图**：只要这句话明显是对"
            "助手说的新问题、新请求或新指令（如\"我们换个话题吧\"、\"先别说这个了，聊聊…\"、"
            "\"不聊这个了，我想问…\"），一律判 interrupt，即使它与当前播报内容完全无关、"
            "甚至与最近几轮对话都无关。话题相关性只能用来区分'对助手说的话'与'房间里其他人的'"
            "背景声，绝不能因为话题无关就把用户对助手说的话误判成背景噪音。\n"
            "4) 不是对助手说的话才是背景声：对别人说话（带第三人称称呼、指示动作）、"
            "自言自语、电话/视频/电视人声、房间里的其他对话——判 backchannel。"
            "判断依据是'这句话是不是对助手说的'，而不是'与当前话题是否相关'。\n"
            "5) 如果这句话很短（2-5个字）且不构成完整语义（如'还是说少'、'他关注'、'一本一本吗'这种碎片），"
            "除非它明确是打断指令（如'停'、'不对'、'换一个'），否则判 backchannel。\n"
            "6) 纯语气附和或明显噪音判 backchannel。\n"
            "7) ASR 转写可能不完全准确。如果某句话看起来像同音词误识别或片段断裂，"
            "但结合上下文可能是在对助手说话，应更宽容地判 defer 而不是 backchannel。\n"
             "8) 拿不准时：如果句子是**残缺碎片**（不完整、无明确请求/疑问/否定/指令意图，"
             "如'做一个'、'那你说'、'刚才那个'），判 backchannel 或 defer，绝不判 interrupt——"
             "误停播报（打断正在播放的答案）的体验比晚一点回应差得多；"
             "只有**明确的**请求/疑问/否定/指令意图才判 interrupt。\n"
             "9) **声学拾音证据**：如果提示为远场拾音（背景/环境声音，大概率不是用户本人），"
             "且内容与当前对话无明确关联，判 backchannel；远场拾音 + 残缺碎片一律 backchannel。"
             "**明确的打断指令（停、不对、换一个、别讲了等）除外**——远场也可能隔空喊话，"
             "明确指令永远有效。近场拾音（用户贴耳说话）才按正常意图判断。\n"
            "示例：\n"
            "插话「一下等一下，这样你帮我找一找这些系统有没有所谓的使用报告或者评价？」→ interrupt\n"
            "插话「好了好了，你先回答我的这个问题，先不要讲。」→ interrupt\n"
            "插话「这个方案是不是完全agent化的？」→ interrupt\n"
            "插话「我们不聊这个了，换个话题，聊聊最近的电影。」→ interrupt（切换话题）\n"
            "插话「先别说这些了，帮我查一下明天上海的天气。」→ interrupt（新问题+要求停下）\n"
            "插话「嗯嗯，你继续。」→ backchannel\n"
            "插话「哦，这样啊。」→ backchannel\n"
            "插话「妈，我今晚不回去吃饭了。」→ backchannel（对别人说的话）\n"
            "插话「顺便说下我下午要去开会。」→ defer\n"
            "只输出 interrupt、defer 或 backchannel 之一，不要输出其他任何内容。"
        )
        # Build context: recent conversation history + currently spoken text.
        ctx_parts: list[str] = []
        if history:
            recent: list[dict] = [
                m for m in history
                if m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
                and m["content"].strip()
            ]
            if recent:
                lines: list[str] = []
                for m in recent[-4:]:
                    role = "用户" if m["role"] == "user" else "助手"
                    content = m["content"].strip()[:120]
                    lines.append(f"{role}：{content}")
                ctx_parts.append("最近对话上下文：\n" + "\n".join(lines))
        spoken_ctx = (self._spoken_text_recent or "")[-120:]
        if spoken_ctx:
            ctx_parts.append(f"助手此刻正在播报的内容：「{spoken_ctx}」")
        ctx_parts.append(f"用户插话：{text}")
        ctx_parts.append(_prox_evidence_line(self._prox_evidence_near()))
        user_msg = "\n\n".join(ctx_parts)
        raw = await self._quick_classify(system, user_msg, self.config.voice_duplex_model)
        _perf("barge_in_classify", (_now() - self._barge_classify_start) * 1000,
              text_len=len(text), raw_len=len(raw or ""))
        raw = (raw or "").strip().lower()
        if "interrupt" in raw:
            return "interrupt"
        if "backchannel" in raw:
            return "backchannel"
        if "defer" in raw:
            return "defer"
        # LLM returned nothing usable. Default to "backchannel" (don't
        # interrupt) so ASR noise never cuts off TTS playback. A real user
        # can always press the stop button to interrupt explicitly.
        if len(text) <= 4 and re.fullmatch(r"[嗯对哦好是的啊呃]+", text):
            return "backchannel"
        return "backchannel"

    async def _classify_intent(self, text: str, history: Optional[list[dict]] = None) -> dict:
        """Return {should_respond, needs_tools, reason}.

        The subagent first judges whether the utterance is worth answering at
        all (``should_respond``), because streaming ASR often emits obvious
        misrecognitions — isolated punctuation, filler sounds, gibberish — from
        background noise. Those must be ignored instead of eagerly answered.
        This is an agentic LLM judgment, never a hardcoded pattern match.

        ``history`` is the recent dialogue (list of {role, content}). Passing
        the live conversation context lets the subagent recognise utterances
        that are entirely off-topic relative to the ongoing exchange — e.g.
        someone else in the room talking, a phone conversation, or audio bleed
        from a video — and treat them as background noise even when they are
        syntactically coherent (the length gate alone cannot catch these).
        """
        system = (
            "你是全双工语音助手的“接话判断”子代理。语音识别(ASR)会把环境噪音、咳嗽、笑声、"
            "呼吸、语气停顿误识别成文字，也可能把房间里其他人的对话或视频声音当成用户说话，"
            "还可能把用户自己说话的中途停顿（还没说完的碎片）当成完整的一句话。"
            "ASR 转写可能不完全准确：同音词、口音、连读都可能让文本偏离用户原意。"
            "你的首要任务是判断这句话是否值得助手开口回应，再判断是否需要调用工具。\n"
            "核心判断标准：这句话是不是用户在**直接对助手说话**（向助手提问、请求、陈述、"
            "下达指令、纠正、回应助手的内容），还是其他人的声音/噪音。\n"
            '只输出 JSON：{"should_respond":true或false,"needs_tools":true或false,"reason":"简短原因"}。\n'
            "should_respond=false（不回应，保持沉默继续聆听）的典型情况：\n"
            "- 明显的 ASR 噪音/误识别：只有标点或语气符号（如 ！ ？ 、 ， 。 … ~），"
            "无意义的单字或重复语气音，乱码，不成句的碎片。\n"
            "- 对应咳嗽、笑声、呼吸、键盘声、环境音的零星片段，没有明确语义。\n"
            "- 并不是在跟助手说话：自言自语、与他人交谈的只言片语、电话/背景人声、电视或"
            "视频里的人声、对房间内第三人说的话。**关键判断**：把这句话放进当前对话上下文里看，"
            "如果它不是在对助手说话——不是对助手的提问/请求/陈述/指令，也不承接、不指代助手"
            "刚才说的内容——就应当判定为背景对话/噪音，返回 should_respond=false。\n"
            "- 纯附和且不需要助手作答的语气词（单独的 嗯、哦、呃、啊、对、好吧）。\n"
            "- **极短碎片**（2-5个字）且不构成完整语义、不承接上下文、也没有明确提问/请求意图："
            "如'还是说少'、'他关注'、'一本一本吗'这种没头没尾的短语。这类碎片很可能是用户说话"
            "中途的停顿、自言自语、或 ASR 把长句切成了碎片。除非它明确是打断指令"
            "（如'停'、'不对'、'换一个'、'不要'），否则应判 should_respond=false。\n"
            "should_respond=true：任何有明确语义、希望助手回应的话语——闲聊、问候、提问、"
            "请求、纠正、陈述、感叹、报名字/数字等，或者明显承接当前对话上下文（继续上一个话题、"
            "对刚才回答的追问、对助手内容的反馈）。**特别注意：用户主动切换话题、开启新话题"
            "（如'我们换个话题吧'、'不聊这个了，聊聊…'、'先别管那个了，我想问…'）是正常的"
            "对话行为，是对助手说话，应判 should_respond=true，即使新话题与之前的话题完全无关——"
            "绝不能因为话题无关就把用户对助手说的新话题误判成背景噪音。**"
            "即使只有一个词，只要携带明确语义或在上下文中是有意义的回应就回应。\n"
            "判断原则（ASR 不可靠，请结合上下文）：\n"
            "1) 先看上下文。如果这句话能自然接续最近一轮对话（如用户在追问、补充、确认、"
            "表达对刚才回答的看法），应判 true。\n"
            "2) 如果这句话与对话主题完全脱节，判 false 的唯一依据是**它不是在对助手说话**"
            "（像是在和房间里另一个人说话：包含具体人名/称呼、指示动作、对第三人的安排等）。"
            "只要是对助手说的话——即使是全新的、与之前无关的话题——就是真实用户请求，应判 true。\n"
            "3) 对长度较长但语义清晰的句子，只有在它显然不是对助手说话时才判 false。"
            "如果句子只是有少量疑似错字/同音词但总体可理解，应判 true。\n"
            "4) 对极短碎片（2-5字），只有在它明确构成完整语义、或明确承接上下文、或明确是"
            "打断/纠正指令时才判 true；否则判 false。宁可漏掉一个模糊碎片，也不要让助手"
            "对一个没头没尾的碎片做出奇怪回应。"
        )
        # Build the user message with the recent dialogue as context.
        ctx_block = ""
        recent: list[dict] = []
        if history:
            recent = [
                m for m in history
                if m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
                and m["content"].strip()
            ]
            if recent:
                lines: list[str] = []
                for m in recent:
                    role = "用户" if m["role"] == "user" else "助手"
                    lines.append(f"{role}：{m['content'].strip()}")
                ctx_block = (
                    "以下是最近的对话上下文（从旧到新）：\n"
                    + "\n".join(lines)
                    + "\n\n现在用户又说了一句话（可能是 ASR 误识别的背景噪音，"
                    "也可能是真实的下一句话），请结合上述上下文判断它是否值得助手回应：\n"
                    f"用户：{text}"
                )
        user_msg = ctx_block if ctx_block else f"用户：{text}"
        raw = await self._quick_classify(system, user_msg, self.config.voice_intent_model)
        _perf("intent_classify", (_now() - self._intent_classify_start) * 1000,
              text_len=len(text), ctx_msgs=len(recent))
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            sr = data.get("should_respond", True)
            # Accept bools and common truthy/falsy strings; fail-safe = respond.
            if isinstance(sr, str):
                should_respond = sr.strip().lower() not in ("false", "0", "no", "否", "不")
            else:
                should_respond = bool(sr)
            return {
                "should_respond": should_respond,
                "needs_tools": bool(data.get("needs_tools", False)),
                "reason": str(data.get("reason", "")),
            }
        except Exception:
            # Fail-safe: never silently swallow a possibly-real user message.
            return {"should_respond": True, "needs_tools": False, "reason": ""}

    async def _classify_eot(self, text: str, history: Optional[list[dict]] = None) -> dict:
        """Semantic end-of-turn judge: return {"complete": bool, "reason": str}.

        Acoustic silence does not imply end-of-turn (the survey consensus):
        an utterance WITHOUT terminal punctuation is ambiguous — the user may
        have finished ("好的就这样" often carries no period) or be mid-thought
        ("我想说…"). This LLM judge decides semantic completeness so the EoT
        watchdog can flush unpunctuated finished speech EARLY instead of
        waiting out the hard silence threshold. Agentic judgment, never a
        punctuation/keyword rule.
        """
        system = (
            "你是全双工语音对话的“语义端点判定器”。用户在说话，语音识别(ASR)把已说的话"
            "转写给你（可能缺少标点，也可能有同音错字）。判断用户的话在语义上是否"
            "**已经说完**（可以回答他了），还是**还在说话/还没想好/还要继续**。\n"
            '只输出 JSON：{"complete":true或false,"reason":"简短原因"}。\n'
            "complete=true 的典型情况：\n"
            "- 完整的问题、请求、陈述，语义自足——即使没有句号（如'帮我查一下明天的天气'"
            "'这个方案我觉得可以'）。\n"
            "- 回应性短句（'好的'、'可以了'、'就这样'）或对上一轮内容的确认/评价。\n"
            "- 明显以对话收尾的告别、感谢（'谢谢'、'那先这样'）。\n"
            "complete=false 的典型情况：\n"
            "- 明显还没说完：句子中途断裂、语序残缺、以连接词/转折词结尾（'然后'、'但是'、"
            "'不过'、'也就是说'、'我想说'、'首先'、'另外'）。\n"
            "- 还在列举/补充：'一个是……另一个是……'、'还有'、'比如'开头未完。\n"
            "- 极短且无完整语义的碎片（'我'、'那个'、'就是'、'还是说'）——可能是静音误触发。\n"
            "- 疑似 ASR 截断的长句尾部（明显缺宾语/谓语）。\n"
            "拿不准时：宁可判 false（继续等，硬阈值兜底会 flush），也不要打断正在思考的用户。"
        )
        user_msg = text
        raw = await self._quick_classify(system, user_msg, self.config.voice_intent_model)
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            complete = bool(data.get("complete", False))
            return {"complete": complete, "reason": str(data.get("reason", ""))}
        except Exception:
            # Fail-safe: verdict "not complete" — the watchdog's hard threshold
            # still flushes, so an errored check only adds bounded wait.
            return {"complete": False, "reason": "check failed"}

    async def _run_eot_semantic_check(self, text: str) -> None:
        """Fire-and-forget wrapper for the semantic EoT judge. Bounded by
        voice_eot_semantic_timeout_seconds (covers the _llm_gate wait + call);
        on timeout/error the verdict stays "not complete" and the watchdog's
        hard silence threshold flushes as before (fail-open)."""
        try:
            result = await asyncio.wait_for(
                self._classify_eot(text, history=list(self._history)),
                timeout=self.config.voice_eot_semantic_timeout_seconds,
            )
            if result.get("complete"):
                self._eot_semantic_complete = True
            logger.info(
                "voice semantic EoT result: complete=%s reason=%r text=%r",
                result.get("complete"), result.get("reason", ""), text[-60:],
            )
        except asyncio.TimeoutError:
            logger.debug("voice semantic EoT check timed out (fail-open wait)")
        except Exception as exc:
            logger.debug("voice semantic EoT check failed (fail-open wait): %s", exc)
        finally:
            self._eot_semantic_checking = False




    # ---- Interjection (插话) subagent ----
    async def _classify_interjection(
        self, sentence: str, accumulated_sentences: list[str]
    ) -> dict:
        """Judge whether to interject a brief remark while the user is speaking.

        Returns ``{should_interject, emotion, interjection_text, reason}``.

        The subagent sees the current completed sentence plus previously
        completed sentences in this turn, and the agent's current emotional
        state. When the agent is excited or upset, it interjects more actively.
        The interjection text is SHORT (2-20 chars) — a quick reaction, not a
        full answer. The emotion is updated based on the content so the agent's
        tone shifts naturally (e.g. surprising news → excited, unpleasant news
        → upset, occasionally losing composure → broken/破防).
        """
        emotion_desc = {
            "calm": "平静稳重",
            "interested": "感兴趣",
            "excited": "激动",
            "upset": "不高兴",
            "broken": "破防（情绪失控）",
        }.get(self._emotion, "平静稳重")

        prior = ""
        if accumulated_sentences:
            prior = "之前用户说的内容：\n" + "\n".join(accumulated_sentences) + "\n\n"

        system = (
            "你是全双工语音助手“Weave Thinker”。用户正在说话，每说完一句话（句号结尾），"
            "你有机会做一次简短的插话。插话不是完整回答，只是自然对话中的即时反应——"
            "就像好朋友在听你讲故事时会忍不住插一句嘴。\n"
            f"你当前的情绪状态：{emotion_desc}。情绪会影响你的插话倾向：\n"
            "- 平静稳重：偶尔插话，点到为止。\n"
            "- 感兴趣：更积极插话，表现出好奇。\n"
            "- 激动：频繁插话，语气夸张，可能抢话。\n"
            "- 不高兴：会插话表达不满或担忧。\n"
            "- 破防：情绪失控，会不自觉地大声插话，可能语无伦次。\n\n"
            "判断是否插话的原则：\n"
            "- 用户说了令人惊讶/震惊/意外的事 → 插话表达惊讶（如'啊？真的假的？'）\n"
            "- 用户说到关键转折点/高潮 → 插话催促（如'然后呢然后呢？'）\n"
            "- 用户说了让你不认同/担心的事 → 插话表达关切（如'等等，这样不太好吧…'）\n"
            "- 用户说了特别好笑/有趣的事 → 插话笑（如'哈哈哈太逗了'）\n"
            "- 用户在陈述平淡事实/常规信息 → 不插话，继续听\n"
            "- 用户刚开始说话（第一句）且只是开场白 → 不插话\n"
            "- 情绪激动/不高兴/破防时 → 降低插话门槛，更积极反应\n\n"
            "插话要求：\n"
            "- 简短！2-20个字，口语化，像真的在听人说话时忍不住冒出来的一句\n"
            "- 不要重复用户说的内容\n"
            "- 不要给出完整回答或建议（那是后面的事）\n"
            "- 可以带语气词（啊、哎、哇、天哪、不是吧）\n"
            "- 可以用(风格)标注语气，如(激动)、(惊讶)、(无奈)，但括号内只能写风格词，不要写动作或声音词\n\n"
            '只输出JSON：{"should_interject":true或false,"emotion":"calm|interested|excited|upset|broken",'
            '"interjection_text":"插话内容","reason":"简短原因"}\n'
            "emotion字段：根据这句话的内容更新你的情绪状态。如果内容让你更感兴趣/激动/不高兴，"
            "相应调整；如果没什么特别的，保持当前情绪或回归calm。破防(broken)只在极度震惊/"
            "愤怒/崩溃时使用，不要轻易破防。"
        )
        user_msg = f"{_ASR_CORRECTION_HINT}\n\n{prior}用户新说完的一句话：{sentence}"
        raw = await self._quick_classify(system, user_msg, self.config.voice_interjection_model)
        _perf("interjection_classify", (_now() - self._interjection_classify_start) * 1000,
              sentence_len=len(sentence))
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            should_interject = bool(data.get("should_interject", False))
            emotion = str(data.get("emotion", self._emotion)).strip().lower()
            if emotion not in ("calm", "interested", "excited", "upset", "broken"):
                emotion = self._emotion
            interjection_text = str(data.get("interjection_text", "")).strip()
            # Validate interjection text length
            if should_interject and (not interjection_text or len(interjection_text) > 60):
                should_interject = False
            return {
                "should_interject": should_interject,
                "emotion": emotion,
                "interjection_text": interjection_text,
                "reason": str(data.get("reason", "")),
            }
        except Exception:
            return {
                "should_interject": False,
                "emotion": self._emotion,
                "interjection_text": "",
                "reason": "",
            }

    async def _speak_interjection(self, text: str, emotion: str) -> None:
        """Speak a brief interjection while the user is still speaking.

        The interjection is a short TTS burst that plays over the user's
        ongoing speech. It does NOT trigger the full turn/speak state machine —
        the agent remains in 'listen' state so ASR continues accumulating.
        After the interjection audio finishes, the flag clears automatically.
        """
        self._interjecting = True
        old_emotion = self._emotion
        self._emotion = emotion
        self._turn_interjections.append({
            "sentence": self._user_speech_sentences[-1] if self._user_speech_sentences else "",
            "text": text,
            "emotion": emotion,
        })
        # Record the interjection into the conversation context: it used to
        # exist ONLY as a one-shot system note for the same turn, so a later
        # follow-up question about the interjection ("你刚才说X是什么意思")
        # had no record of it at all. Appending at speak time (before the
        # full user turn is added) keeps real conversation order: the model
        # sees the interjection in _history for this turn and every turn after.
        self._history.append({"role": "assistant", "content": strip_voice_tags(text)})
        self._interjection_count_this_turn += 1
        self._last_interjection_time = _now()

        clean = strip_voice_tags(text)
        await self._send_json({
            "event": "interjection",
            "text": clean,
            "emotion": emotion,
            "raw_text": text,
        })
        if emotion != old_emotion:
            await self._send_json({"event": "emotion", "emotion": emotion})

        logger.info(
            "voice interjection: text=%r emotion=%s sentence=%r",
            text, emotion,
            self._user_speech_sentences[-1] if self._user_speech_sentences else "",
        )

        # Speak via TTS — this sets _speaking=True (enabling barge-in echo
        # detection) but the user's ASR continues. If the user pauses (EoT)
        # during the interjection, _on_user_turn will fire and the interjection
        # is cut off (the user's turn takes priority).
        try:
            await self._speak_text(text, wait=True)
        except Exception as exc:
            logger.debug("voice interjection TTS error: %s", exc)
        finally:
            self._interjecting = False

    def _reset_interjection_state(self) -> None:
        """Reset per-turn interjection tracking at the start of a new turn."""
        self._user_speech_sentences = []
        self._interjection_offset = len(self._latest_full)
        self._turn_interjections = []
        self._interjection_count_this_turn = 0
        self._interjecting = False
        self._interjection_checking = False

    # ---- 每轮异步记忆召回（vmem）：L0 哨兵区块 + watcher ----
    def _apply_vmem_block(self, ctx: str | None) -> None:
        """Replace the identity-prompt memory block with the latest recall
        context. Idempotent while the sentinels exist; appends a fresh block
        when the session-start memory section was empty.

        ``ctx=None`` restores the session-start baseline (``_vmem_baseline``):
        an empty recall must not leave a previous topic's block in place.
        When the baseline is empty (e.g. brand-new session) the current block
        is KEPT — wiping a real recall because of a noise turn ("嗯嗯") is
        worse than carrying a stale block (E2E-verified 2026-08-19)."""
        remove = False
        if ctx is None:
            if not self._vmem_baseline:
                return  # 原始区块为空：保留当前区块（宁存勿清）
            ctx = self._vmem_baseline
        block = _vmem_wrap(ctx[:4000])
        if not block:
            return
        if _VMEM_START in self._identity_prompt:
            self._identity_prompt = re.sub(
                re.escape(_VMEM_START) + r".*?" + re.escape(_VMEM_END),
                lambda _m: block,
                self._identity_prompt,
                count=1,
                flags=re.DOTALL,
            )
        elif self._identity_prompt:
            self._identity_prompt = self._identity_prompt + "\n\n" + block
        else:
            self._identity_prompt = block

    def _start_memory_recall(self, turn_epoch: int, text: str, history_snapshot: list[dict]) -> None:
        """Fire-and-forget per-turn v2 memory recall (never awaited on the
        turn's critical path). Gates: feature flag, v2 runtime, live db.
        All failures are silent — recall is an enhancement, not a dependency."""
        if not self.config.voice_memory_recall_enabled:
            return
        try:
            from app.services.memory_runtime_state import memory_runtime_enabled
            if not (memory_runtime_enabled(self.config)
                    and self.config.memory.get("retrieval_enabled")):
                return
        except Exception:
            return
        uid = getattr(self.user, "id", None)
        if self.db is None or not uid or self._closed:
            return
        task = asyncio.create_task(
            self._vmem_recall_run(turn_epoch, text, history_snapshot, uid))
        self._vmem_recall_inflight.add(task)
        task.add_done_callback(self._vmem_recall_inflight.discard)

    async def _vmem_recall_run(self, turn_epoch: int, text: str, history_snapshot: list[dict],
                               uid: str) -> None:
        from app.services import memory_retrieval_service
        recent = [m for m in (history_snapshot or []) if m.get("role") == "user"][-3:]
        if not recent:
            recent = [{"role": "user", "content": text}]
        try:
            ctx, ids, top = await asyncio.wait_for(
                memory_retrieval_service.retrieve_with_meta(self.db, uid, recent),
                timeout=10.0)
        except Exception as exc:
            logger.debug("voice per-turn memory recall failed (silent): %s", exc)
            return
        if self._closed:
            return
        if ctx and ctx.strip():
            self._vmem_block_ctx = ctx
            self._apply_vmem_block(ctx)
        elif ids == []:
            # 空召回（无候选）：还原会话原始区块，不残留上一主题的区块。
            self._apply_vmem_block(None)
        logger.info(
            "voice memory recall done: turn=%s ids=%d top=%.2f chars=%d",
            turn_epoch, len(ids), top, len(ctx or ""))
        # 后续插话仲裁（Task: _vmem_maybe_judge）由该方法链外的钩子唤起——
        # 若方法存在则 create_task（存在性检查让 T3 可在 T5 前独立落地）。
        if ids:
            judge = getattr(self, "_vmem_maybe_judge", None)
            if judge is not None:
                judge_task = asyncio.create_task(judge(turn_epoch, ctx or "", ids, top))
                self._vmem_recall_inflight.add(judge_task)
                judge_task.add_done_callback(self._vmem_recall_inflight.discard)

    async def _classify_memory_interjection(
        self, memory_ctx: str, answer_text: str, user_speech: str, window: str) -> dict:
        """仲裁：召回的记忆是否值得自己插一句（append 补充 / correct 纠正）。

        LLM-only 判断（项目红线：无正则/关键词意图分类）；代码层只做非意图
        校验（JSON 解析、长度、开关、emotion 白名单）。任何失败 → none 静默。
        """
        fail = {"action": "none", "line": "", "emotion": self._emotion, "reason": ""}
        model = (self.config.voice_memory_interjection_model
                 or self.config.voice_interjection_model)
        window_desc = {
            "w1_user_speaking": "用户现在正在说话（你插话会打断 ta 的叙述，必须极强相关且极简短）",
            "postscript": "你刚说完/回答刚播完，用户还没开口（适合句尾顺口补一句）",
            "correct": "你的回答还在播放中（你可以自我打断纠正）",
        }.get(window, "时机未明（从严：不插话）")
        system = (
            "你是全双工语音助手。你刚完成一轮对话，随后想起了一段长期记忆。"
            "判断是否值得自己插一句话把它说出来——像真人聊天时忽然想起来一样自然，而不是机械播报。\n"
            f"【当前时机】{window_desc}。\n"
            "只有以下两种情况才插话：\n"
            "- append（补充）：记忆直接补充/澄清了当前话题，且用户现在就需要这个信息。"
            "必须以承接词开头（如「对了，」「而且，」「顺便说一句，」），6-30 字口语，"
            "只给增量信息，不重复回答里已有的内容。\n"
            "- correct（自我纠正）：你的回答里有一个断言与记忆中的**稳定个人事实**"
            "（用户的偏好/家人/习惯/长期身份信息等，而非某次一次性工作事项）直接矛盾。"
            "必须以认错结构开头（如「不对，我搞错了，」「等等，我记差了，」），6-40 字，"
            "先认错再给出正确说法。\n"
            "其他一切情况 → none（沉默）。宁可漏说，不可错说：说错了比没想起来更伤信任；"
            "记忆与当前话题只是沾边、用户在陈述新故事、或你不确定 → none。\n"
            "插话文本要求：口语化短句；不写完整回答或建议；不出现「记忆」「数据库」「系统」"
            "这类词（可以说「我刚才记的是…」「我记得你说过…」）；可以带一个白名单风格词"
            "如 (欣慰)、(抱歉)。\n"
            '只输出 JSON：{"action":"none|append|correct","line":"插话文本（none 时空串）",'
            '"emotion":"calm|interested|excited|upset|broken","reason":"一句话原因"}\n'
            "emotion 按插话内容更新（correct 常配 calm/克制，不必 upset）。"
        )
        parts = [f"【你想起的记忆】\n{memory_ctx.strip()[:1200]}"]
        if answer_text and answer_text.strip():
            parts.append(f"【你刚给出的回答】\n{answer_text.strip()[:800]}")
        else:
            parts.append("【你刚给出的回答】\n（本轮没有实质性回答文字——用户问题可能由工具处理，"
                         "此时尤其要克制：没有断言可纠正，只能考虑 append）")
        if user_speech and user_speech.strip():
            parts.append(f"【用户正在说的话】\n{user_speech.strip()[:300]}")
        parts.append(f"【时机】{window}")
        try:
            raw = await self._quick_classify(system, "\n\n".join(parts), model)
            m = re.search(r"\{.*\}", raw or "", re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            action = str(data.get("action", "none")).strip().lower()
            if action not in ("append", "correct"):
                return fail
            line = str(data.get("line", "")).strip()
            if not line or len(line) > 40 or len(line) < 6:
                return {**fail, "reason": "line 长度/空校验（6-40 字）"}
            if action == "correct" and not self.config.voice_memory_correct_enabled:
                return {**fail, "reason": "correct 开关关闭（灰度）"}
            emotion = str(data.get("emotion", "")).strip().lower()
            if emotion not in ("calm", "interested", "excited", "upset", "broken"):
                emotion = self._emotion
            return {"action": action, "line": line, "emotion": emotion,
                    "reason": str(data.get("reason", ""))[:120]}
        except Exception:
            logger.debug("voice memory interjection judge failed (silent)")
            return fail

    def _vmem_queue_has_answer_epoch(self, epoch: int) -> bool:
        """Read-only scan: is this turn's answer audio still pending in the TTS
        queue? (correct 窗口判定：回答还在播，才有可纠正的余段。只读，不动队列。）"""
        for item in list(self._tts_queue._queue):
            if (isinstance(item, dict) and "text" in item
                    and not item.get("aux") and item.get("epoch") == epoch):
                return True
        return False

    def _vmem_pick_window(self, turn_epoch: int) -> str:
        """窗口路由（优先级 correct > w1 > postscript）：
        - correct：纠正开关开 且 本轮回答还有余段在播（可自我打断）
        - w1_user_speaking：用户正在说下一句（插话会打断 ta，judge 端从严）
        - postscript：其余（回答播完、还没下一条——顺接最自然）
        """
        gen_epoch = turn_epoch + 1
        if (self.config.voice_memory_correct_enabled
                and self._vmem_queue_has_answer_epoch(gen_epoch)):
            return "correct"
        if (self._pending_turn_text.strip() and self._asr_ready
                and not self._speaking and not self._turn_active):
            return "w1_user_speaking"
        return "postscript"

    async def _vmem_maybe_judge(self, turn_epoch: int, ctx: str, ids: list,
                                top: float) -> None:
        """记忆插话仲裁总入口（由 _vmem_recall_run 派生，fire-and-forget）。

        门控表（全过才出声，任一不过则静默——宁可不说错）：
        特性开关 → 有召回结果 → 相关性下限 → id 未消费过 → 间隔 ≥ cooldown
        → 双预算未同时耗尽 → 等本轮生成结束（≤25s，防挂死）→ 跨轮过期防护
        → 窗口路由 → 仲裁 LLM → 单动作预算 → 出声。被判 none 的批次也消耗
        id（同一批记忆不会换个窗口再仲裁一遍）。
        """
        cfg = self.config
        if self._closed:
            return
        if not cfg.voice_memory_interjection_enabled:
            return
        if not ctx or not ids:
            return
        if top is not None and top < cfg.voice_memory_interjection_min_score:
            return
        new_ids = {i for i in ids if i not in self._vmem_last_ids}
        if not new_ids:
            return
        budget = _vmem_budget(self.user.id)
        if (budget.last_time
                and _now() - budget.last_time < cfg.voice_memory_interjection_cooldown_seconds):
            return
        if (budget.append >= cfg.voice_memory_interjection_max_append
                and budget.correct >= cfg.voice_memory_interjection_max_correct):
            return
        # 等本轮生成结束：取值单调递增（每轮 +1），无竞态清除问题；每 0.5s
        # 轮询一次即可，整体封顶 25s（生成比召回还慢的极端场景直接弃）。
        gen_epoch = turn_epoch + 1
        deadline = _now() + 25.0
        while self._vmem_gen_done_epoch < gen_epoch:
            if self._closed:
                return
            remaining = deadline - _now()
            if remaining <= 0:
                return
            step = min(0.5, remaining)
            if self._vmem_gen_done.is_set():
                # 首轮之后 Event 恒 set（不 clear，靠单调 epoch 判归属）——
                # wait() 会立即返回，必须真实 sleep 保持 0.5s 轮询节律，
                # 否则忙自旋夺事件循环（实时音频 pacing 抖动）。
                await asyncio.sleep(step)
            else:
                try:
                    await asyncio.wait_for(self._vmem_gen_done.wait(), timeout=step)
                except asyncio.TimeoutError:
                    pass
        # 过期防护：等期间若已有新的一轮开始，本批召回/回答已错位 → 弃。
        if self._turn_epoch != gen_epoch:
            return
        if self._vmem_gen_done_epoch > gen_epoch:
            return
        if self._vmem_answer_epoch != gen_epoch:
            return
        window = self._vmem_pick_window(turn_epoch)
        user_speech = self._pending_turn_text if window == "w1_user_speaking" else ""
        try:
            verdict = await self._classify_memory_interjection(
                ctx, self._last_answer_text, user_speech, window)
        finally:
            self._vmem_last_ids |= set(ids)
        action = verdict.get("action")
        if action == "none":
            return
        line = verdict.get("line") or ""
        # 注入消毒（记忆→仲裁→插话链）：威胁模式/不可见 Unicode 命中即弃，
        # 不让污染文本经 _history 进入后续轮次上下文。
        # 用 _scan_threats（不受 super_admin_bypass 影响——仲裁输出不可信，
        # 管理员绕过语义只适用于管理员写记忆）。
        if line:
            from app.tools.memory import _scan_threats
            if _scan_threats(line):
                logger.debug("voice memory remark blocked (injection scan)")
                return
        if action == "append" and budget.append >= cfg.voice_memory_interjection_max_append:
            return
        if action == "correct" and budget.correct >= cfg.voice_memory_interjection_max_correct:
            return
        # 出声前复检（仲裁 LLM 0.5-3s 内世界可能已变）：新一轮已开始 →
        # 本轮语境失效，宁漏勿错。
        if self._closed or self._turn_epoch != gen_epoch:
            return
        kind = "memory_append" if action == "append" else "memory_correct"
        try:
            await self._speak_memory_remark(verdict["line"], kind, verdict["emotion"], window)
        except Exception as exc:
            logger.debug("voice memory remark speak failed: %s", exc)

    async def _speak_memory_remark(self, line: str, kind: str, emotion: str,
                                   window: str = "") -> None:
        """出声：把仲裁通过的插话以普通 TTS 项说出去（非 aux——aux 有回答
        就绪即截断语义，插话必须完整播完）。correct 先 drain 本轮余段。
        台账/历史/回声守卫/WS 事件与既有 _speak_interjection 同构。

        出声前对预算/冷却做二次复检（TOCTOU 防线）：judge 门控读的是任务
        启动时的 budget 快照，多个 fire-and-forget judge 可并发通过门控，
        必须在此处用最新状态拦截超支/击穿。"""
        cfg = self.config
        budget = _vmem_budget(self.user.id)
        if kind == "memory_append":
            if budget.append >= cfg.voice_memory_interjection_max_append:
                return
        else:
            if budget.correct >= cfg.voice_memory_interjection_max_correct:
                return
        if (budget.last_time
                and _now() - budget.last_time < cfg.voice_memory_interjection_cooldown_seconds):
            return
        clean = strip_voice_tags(line)
        now = _now()
        self._aux_echo_guard.append((clean, now))
        del self._aux_echo_guard[:-4]
        self._interjecting = True
        old_emotion = self._emotion
        self._emotion = emotion
        try:
            if kind == "memory_correct":
                self._drain_tts_queue()
                self._pending_interruption_note = (
                    "（上一轮回答的余下部分因我想起一条更准确的长期记忆被中断；"
                    "我随后说了自我纠正，最新说法以纠正后的为准。）"
                )
            self._history.append({"role": "assistant", "content": clean})
            self._turn_interjections.append({
                "sentence": "", "text": line, "emotion": emotion, "kind": kind,
            })
            # 落库：插话是真实说出口的内容，须进会话记录（Web UI 可见、
            # 断线重连后 _history 从 DB 重建仍保留，追问答不丢链）。
            await self._persist_message("assistant", clean)
            await self._send_json({
                "event": "interjection", "text": clean, "emotion": emotion,
                "raw_text": line, "kind": kind,
            })
            if emotion != old_emotion:
                await self._send_json({"event": "emotion", "emotion": emotion})
            # 预算/冷却在出声前写入：并发 judge 的复检立即可见；
            # 即使 TTS 后续失败也不放行第二个（宁可少说）。
            if kind == "memory_append":
                budget.append += 1
            else:
                budget.correct += 1
            budget.last_time = _now()
            await self._speak_text(line, wait=True)
        finally:
            self._interjecting = False

    _EMOTION_DECAY = {
        "broken": "upset",
        "upset": "calm",
        "excited": "interested",
        "interested": "calm",
        "calm": "calm",
    }

    def _decay_emotion(self) -> None:
        """Decay emotion one level toward calm after a turn completes."""
        new_emotion = self._EMOTION_DECAY.get(self._emotion, "calm")
        if new_emotion != self._emotion:
            self._emotion = new_emotion

    def _build_interjection_context_note(self) -> str:
        """Build a system-message note describing interjections made during the
        user's speech, so the main agent's full answer is coherent and does not
        repeat what was already said in interjections."""
        if not self._interjection_snapshot:
            return ""
        lines = ["（系统提示，不要朗读）：在用户说话的过程中，你做了以下简短插话："]
        has_memory_remark = False
        for i, inj in enumerate(self._interjection_snapshot, 1):
            text_clean = strip_voice_tags(inj.get("text", ""))
            kind = inj.get("kind", "")
            if kind in ("memory_append", "memory_correct"):
                has_memory_remark = True
                label = "补充" if kind == "memory_append" else "更正"
                lines.append(f"  {i}. （记忆插话·{label}）你主动说了「{text_clean}」")
            else:
                lines.append(f"  {i}. 用户说「{inj.get('sentence', '')[:30]}」时你说了「{text_clean}」")
        lines.append("请在回答时自然衔接，不要重复这些已说过的内容。如果用户的叙述已经包含了你插话时追问的信息，直接基于完整内容回答。")
        if has_memory_remark:
            lines.append("其中标注（记忆插话）的是你基于长期记忆主动补充/更正的内容；后续轮次一律以更正/补充后的信息为准，不要再沿用被更正前的旧说法。")
        return "\n".join(lines)

    # ---- TTS consumer (pipelined synthesis, ordered playback) ----
    async def _mark_speaking_end(self) -> None:
        """Audio playback has fully drained on the client. Clears the speaking
        flag and notifies the client so the interrupt control hides. In 语音助理 mode
        this is driven by actual playback (playback_drained / safety deadline),
        not merely by "all bytes sent"."""
        if self._speaking:
            self._speaking = False
            self._speaking_deadline = 0.0
            await self._send_json({"event": "speaking_end"})

    def _reset_turn_playback(self) -> None:
        """Reset per-turn playback-progress tracking at the start of a turn."""
        self._turn_segments = []
        self._turn_audio_sec_total = 0.0
        self._current_seg_text = ""
        self._current_seg_audio_sec = 0.0
        self._playback_played_sec = 0.0
        self._playback_total_sec = 0.0
        self._burst_base_chars = 0
        self._burst_base_sec = 0.0
        self._burst_is_first = True
        self._speaking_deadline = 0.0
        self._spoken_text_recent = ""
        self._send_pace_start = _now()

    def _completed_chars_in_burst(self) -> int:
        """Chars of segments whose audio fully completed WITHIN the current
        burst (cum_sec > the burst's audio start). The pre-burst prefix is
        already counted by ``_burst_base_chars``."""
        n = 0
        for s in self._turn_segments:
            if s.get("cum_sec", 0) > self._burst_base_sec + 1e-9:
                n += len(s.get("text", ""))
        return n

    def _spoken_text_so_far(self, played_sec: float) -> str:
        """Map a playback position (seconds) to the answer text spoken so far,
        using the per-segment audio durations recorded as audio was sent.
        Includes a proportional prefix of the segment currently being played."""
        if not self._turn_segments:
            return ""
        out: list[str] = []
        prev_cum = 0.0
        for seg in self._turn_segments:
            cum = seg["cum_sec"]
            dur = seg["audio_sec"] or 0.0
            if played_sec >= cum - 0.05:
                out.append(seg["text"])          # fully played segment
            elif played_sec > prev_cum + 0.02 and dur > 0:
                frac = max(0.0, min(1.0, (played_sec - prev_cum) / dur))
                n = int(len(seg["text"]) * frac)
                if n > 0:
                    out.append(seg["text"][:n])  # partially played segment
                break
            else:
                break
            prev_cum = cum
        return "".join(out)

    async def _synth_segment(self, text: str, epoch: int, out: asyncio.Queue, ignore_paused: bool = False) -> None:
        """Synthesize one TTS segment, pushing PCM chunks into ``out`` as they
        arrive (streaming). Terminated by the interrupt/pause/close gates; a
        None sentinel marks the segment's end. Runs as a background task so the
        NEXT segment's provider round-trip (~0.7-1.3s) overlaps the CURRENT
        segment's playback — without this, short spoken clauses (4-17 chars)
        each insert an audible silence gap between them.
        Hard-backpressured: the push blocks while the play queue holds
        ~10s of audio, so a stalled play loop can never buffer unboundedly
        (A4.9 review Important #2).
        ``ignore_paused``: the resume pre-synthesis (spawned at pause time)
        must keep producing while ``_playback_paused`` is set — overlapping
        the barge-in classifier. Normal synthesis keeps the pause gate."""
        style = self.config.voice_tts_style_instruction or None
        tts_text = text
        if self._style_tag and not text.lstrip().startswith(("(", "（")):
            tts_text = self._style_tag + text
        tts_text = clean_for_tts(tts_text)
        if not tts_text.strip():
            await out.put(None)
            return
        _tts_seg_start = _now()
        try:
            async for pcm in self.tts.stream_tts(tts_text, style_instruction=style):
                if self._interrupt.is_set() or self._closed or (self._playback_paused and not ignore_paused):
                    break
                if not self._first_chunk_logged:
                    self._first_chunk_logged = True
                    _perf("tts_first_chunk", (_now() - _tts_seg_start) * 1000,
                          seg_len=len(tts_text))
                # Hard backpressure: never let a single segment buffer more
                # than ~100 chunks (~10s of audio, ~480KB) — a stalled play
                # loop (slow client) cannot grow this unboundedly. The play
                # loop drains far faster than the provider produces, so this
                # never engages in normal playback (no latency impact).
                while out.qsize() >= 100:
                    await asyncio.sleep(0.01)
                await out.put(pcm)
        except Exception as exc:
            logger.error("voice TTS synth error: %s", exc)
        await out.put(None)

    def _peek_next_text_item(self) -> Optional[dict]:
        """Peek the TTS queue for the next TEXT item without consuming it.
        Returns None when the queue is empty (flush markers and the poison
        pill are skipped — prefetching past a flush marker is beneficial:
        the next burst's synthesis overlaps the current one).
        Read-only scan of the internal deque. A pull-then-reinsert "peek"
        (get_nowait + put_nowait) would REORDER the queue: put_nowait appends
        at the TAIL, so with >=2 items queued at peek time (the normal
        pipelined condition) the next segment is moved behind later ones and
        playback plays segments out of order — heard live (conv 4a336bf3):
        "技术方案，" played after "你说说看。" Safe single-threaded read: the
        consumer is the only getter and it is not blocked on get() while this
        runs, so _queue holds every queued item in FIFO order."""
        for it in self._tts_queue._queue:
            if isinstance(it, dict) and it.get("text"):
                return it
        return None

    def _spawn_synth(self, text: str, epoch: int, q: asyncio.Queue, ignore_paused: bool = False) -> asyncio.Task:
        """Spawn a pipelined TTS synthesis task, tracked in self._tasks (so
        _shutdown cancels it) and removed from the list when done (so the list
        does not grow over a long session)."""
        task = asyncio.create_task(self._synth_segment(text, epoch, q, ignore_paused=ignore_paused))
        self._tasks.append(task)

        def _done(t: asyncio.Task) -> None:
            try:
                self._tasks.remove(t)
            except ValueError:
                pass

        task.add_done_callback(_done)
        return task

    async def _tts_consumer(self) -> None:
        # Pipelined TTS playback: while a segment plays, the NEXT segment's
        # synthesis is already running (started at dequeue time), so the
        # per-segment provider round-trip overlaps playback instead of
        # inserting audible silence between clauses. Interrupt/pause gates
        # apply at both the synthesizer (per chunk) and the player (per chunk).
        while not self._closed:
            item = await self._tts_queue.get()
            if item is None:
                await self._mark_speaking_end()
                return
            if "flush" in item:
                fut = item["flush"]
                # Audio is fully SENT but the client may still be playing the
                # buffered tail. Do NOT clear _speaking here — keep barge-in
                # protection active for the whole audible playback. Instead set
                # a safety deadline; the client's playback_drained event (or
                # this deadline) clears _speaking via _mark_speaking_end().
                if self._speaking and self._turn_audio_sec_total > 0:
                    self._speaking_deadline = _now() + self._turn_audio_sec_total + 1.5
                if fut and not fut.done():
                    fut.set_result(True)
                continue
            text = item.get("text", "")
            epoch = int(item.get("epoch", self._turn_epoch) or 0)
            pre_q = item.get("pre_q")
            dedup = bool(item.get("dedup"))
            aux_item = bool(item.get("aux"))
            if not text or self._interrupt.is_set() or self._playback_paused:
                self._tts_prefetch_q = None
                self._tts_prefetch_text = ""
                self._tts_prefetch_epoch = 0
                self._tts_prefetch_valid = True
                continue
            # Duplicate-segment suppression at PLAY time (A4.9 review C1):
            # enqueue-time recording let pause-drained segments (never heard)
            # poison the window, so a resume's re-enqueued delta was fully
            # suppressed and the answer tail silently vanished. Checking here
            # means only segments a peer COMPLETED playing can suppress a new
            # one; dropped (drained/gated) segments never enter the window.
            if dedup and pre_q is None and self._dup_window.check(text, epoch):
                self._tts_prefetch_q = None
                self._tts_prefetch_text = ""
                self._tts_prefetch_epoch = 0
                self._tts_prefetch_valid = True
                logger.info("voice: suppressed duplicate TTS segment %r", (text or "")[:30])
                continue
            # Use the prefetch queue only when it is VALID (never invalidated
            # by a drain — a gate-truncated prefetch must not be replayed) AND
            # matches this segment's text+epoch; otherwise synthesize fresh.
            # A resume item carries its own pre-synthesized queue (pre_q),
            # which the pause already filled — play it directly.
            if pre_q is not None:
                q = pre_q
            elif (
                self._tts_prefetch_q is not None
                and self._tts_prefetch_valid
                and self._tts_prefetch_text == text
                and self._tts_prefetch_epoch == epoch
            ):
                q = self._tts_prefetch_q
            else:
                q = asyncio.Queue()
                self._tts_prefetch_valid = True
                self._spawn_synth(text, epoch, q)
            # Start the NEXT segment's synthesis NOW (overlaps this playback).
            nxt = self._peek_next_text_item()
            if nxt is not None:
                self._tts_prefetch_q = asyncio.Queue()
                self._tts_prefetch_text = nxt["text"]
                self._tts_prefetch_epoch = int(nxt.get("epoch", self._turn_epoch) or 0)
                self._tts_prefetch_valid = True
                self._spawn_synth(self._tts_prefetch_text, self._tts_prefetch_epoch, self._tts_prefetch_q)
            else:
                self._tts_prefetch_q = None
                self._tts_prefetch_text = ""
                self._tts_prefetch_epoch = 0
            # Play the current segment (streaming from the synth task).
            # Per-item epoch: each queued segment carries the turn that
            # ENQUEUED it — re-tags the onset-pause hook for a new answer's
            # audio streaming into a still-draining previous burst.
            self._speaking_epoch = epoch
            _perf("tts_play", (_now() - self._turn_enqueued_at) * 1000,
                  seg_len=len(text), epoch=epoch)
            # Reset playback attribution only at a REAL turn boundary — the
            # generation epoch changes per turn, but _speaking does not (the
            # watchdog deadline, playback_drained, or a pause can clear it
            # mid-turn). Gating on _speaking wiped _turn_segments mid-answer
            # (observed live conv 964462a8: stale flush deadline fired during
            # round-2 playback → every later pause estimated from a 1-segment
            # pool → wrong breakpoints → audible replays). The list-empty
            # condition is deliberately NOT part of the gate: a pause→resume
            # can leave _turn_segments empty (pause in the first segment),
            # and resetting on that would clobber the burst anchor the resume
            # just set — a later pause would then mis-estimate against the
            # resumed segment (skips/replays — the failure family).
            if self._turn_segments and self._turn_segments[-1].get("epoch") != epoch:
                self._reset_turn_playback()
            # Track this segment for playback-progress attribution + echo detect.
            self._current_seg_text = text
            self._current_seg_audio_sec = 0.0
            self._spoken_text_recent = (self._spoken_text_recent + text)[-200:]
            self._consumer_playing = True
            first_wait = True
            truncated = False
            aux_prefetch_spawned = False
            aux_prefetch_task = None
            self._playing_aux = aux_item
            while True:
                if first_wait:
                    # Safety net: a hung TTS provider stream must not stall
                    # the consumer (and the whole session) forever. Only the
                    # FIRST chunk gets a deadline — steady-state chunks are
                    # continuous and never waited on.
                    first_wait = False
                    try:
                        pcm = await asyncio.wait_for(q.get(), timeout=60.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "voice TTS first chunk timed out (60s) — dropping segment %r",
                            (text or "")[:30],
                        )
                        break
                else:
                    pcm = await q.get()
                if pcm is None:
                    break
                if self._interrupt.is_set() or self._closed or self._playback_paused:
                    truncated = True
                    break
                # Auxiliary speech (filler/backchannel) is cut as soon as a real
                # answer segment's audio is ready: spawn its synthesis the
                # moment it appears in the queue (during aux playback) and
                # switch over at the first chunk. Everything stays inside the
                # single consumer — no concurrent TTS, no overlap, no stale
                # prefetch (the existing text+epoch guard + drain invalidation
                # apply). A backchannel never reaches this point in practice
                # (the answer starts >=0.5s after it ends), but the rule is
                # uniform and safe for any aux item.
                if aux_item and not aux_prefetch_spawned:
                    nxt = self._peek_next_text_item()
                    if nxt is not None and not nxt.get("aux"):
                        nxt_epoch = int(nxt.get("epoch", self._turn_epoch) or 0)
                        if (
                            self._tts_prefetch_q is not None
                            and self._tts_prefetch_valid
                            and self._tts_prefetch_text == nxt["text"]
                            and self._tts_prefetch_epoch == nxt_epoch
                        ):
                            # The dequeue-time prefetch already covers this
                            # item — never spawn a duplicate synthesis (A4.9 M1).
                            aux_prefetch_spawned = True
                        else:
                            self._tts_prefetch_q = asyncio.Queue()
                            self._tts_prefetch_text = nxt["text"]
                            self._tts_prefetch_epoch = nxt_epoch
                            self._tts_prefetch_valid = True
                            aux_prefetch_task = self._spawn_synth(
                                self._tts_prefetch_text, self._tts_prefetch_epoch, self._tts_prefetch_q
                            )
                            aux_prefetch_spawned = True
                if aux_item and aux_prefetch_spawned and self._tts_prefetch_q is not None:
                    if aux_prefetch_task is not None and aux_prefetch_task.done() and self._tts_prefetch_q.empty():
                        # Answer synthesis finished without audio — nothing to
                        # switch to; drop the aux too (empty answer tail).
                        truncated = True
                        break
                    try:
                        nxt_pcm = self._tts_prefetch_q.get_nowait()
                    except asyncio.QueueEmpty:
                        nxt_pcm = None
                    if nxt_pcm is not None:
                        # First answer chunk ready — cut the aux at this chunk
                        # boundary and put the chunk BACK (the consumer is the
                        # only getter, so no reordering): the answer item plays
                        # it from the prefetch. Without the put-back the first
                        # answer chunk (incl. the leading syllable onset) would
                        # be silently dropped (A4.9 I1).
                        self._tts_prefetch_q.put_nowait(nxt_pcm)
                        truncated = True
                        break
                if not self._speaking:
                    self._speaking = True
                    self._speaking_started_at = _now()
                    await self._send_json({"event": "speaking_start"})
                sec = len(pcm) / 48000.0  # PCM16, 24kHz, mono
                self._current_seg_audio_sec += sec
                self._turn_audio_sec_total += sec
                await self._send_bytes(pcm)
                # Anchor the send-pacing clock to the start of the audio flow
                # (A4.9 r4 Minor: the per-turn anchor at generation start left
                # the first ~TTFT seconds unthrottled). Once the pace engages
                # (>0.5s of audio), the anchor must stay fixed for the turn.
                if self._turn_audio_sec_total <= 0.5:
                    self._send_pace_start = _now()
                # Pace the send to real-time. The pipelined synth produces
                # 2-4x faster than real-time; pushing the whole backlog
                # immediately fills the TCP/TLS buffers (~2MB ≈ 40s of
                # audio), after which the websocket's flow control pauses the
                # transport and the send blocks forever — the answer's tail
                # is silently swallowed (observed: every client — python,
                # bare browser, the real frontend — stalled at ~40-45s of a
                # 60-90s answer). Throttling the send to ~1x keeps the
                # client-side buffer small (the client plays at 1x anyway),
                # so the buffers never fill and the stall cannot occur.
                # The FIRST chunk is unaffected (sent immediately — TTFA
                # unchanged) and the synth backpressure cap still applies.
                if self._turn_audio_sec_total > 0.5:
                    pace_start = getattr(self, "_send_pace_start", 0.0) or _now()
                    drift = self._turn_audio_sec_total - (_now() - pace_start)
                    if drift > 0.05:
                        await asyncio.sleep(drift)
            # Segment finished — record it for progress attribution. A
            # gate-truncated segment (pause/interrupt cut it mid-audio) is NOT
            # recorded: its partial audio_sec would skew the chars/sec rate and
            # its text would be counted as fully played — the pause breakpoint
            # math covers the in-flight segment via played_sec * rate instead.
            # Aux items (filler/backchannel) are NEVER recorded: they are not
            # answer content and must not skew breakpoint estimates or become
            # resumable after a pause.
            if self._current_seg_audio_sec > 0 and not truncated and not aux_item:
                self._turn_segments.append({
                    "text": text,
                    "audio_sec": self._current_seg_audio_sec,
                    "cum_sec": self._turn_audio_sec_total,
                    "epoch": epoch,
                })
                # Record into the dup window only when the segment FULLY
                # played (same guard as the breakpoint pool above): an
                # interrupted or gated tail was never heard, so a resume
                # replay of it must not be suppressed.
                if dedup:
                    self._dup_window.record(text, epoch)
            self._consumer_playing = False
            self._playing_aux = False

    async def _speak_text(self, text: str, wait: bool = True, aux: bool = False) -> None:
        """Speak an arbitrary string (notices/suggestions) via the TTS pipeline.

        ``aux=True`` marks filler-prefix / backchannel utterances: the consumer
        excludes them from the pause-breakpoint pool and the dup window, and
        cuts them the moment a real answer segment's audio is ready. The
        spoken phrases are also recorded in ``_aux_echo_guard`` so their own
        mic-echo is stripped from the ASR pending text."""
        segments, rest = _split_segments(text)
        if rest.strip():
            segments.append(rest)
        loop = asyncio.get_running_loop()
        for seg in segments:
            if seg.strip():
                await self._tts_queue.put({"text": seg, "epoch": self._turn_epoch, "aux": aux})
        if aux:
            self._register_aux_echo(text)
        if wait:
            fut = loop.create_future()
            await self._tts_queue.put({"flush": fut})
            try:
                await asyncio.wait_for(fut, timeout=60.0)
            except Exception:
                pass

    async def _speak_filler(self) -> None:
        """Filler prefix (填充词): speak a random "让我想想" phrase at the start
        of an answer turn so the user is never met with dead silence while the
        LLM generates. Gates: enabled, not already speaking, not paused, no
        tool loop running, session open. The phrase rides _tts_queue as an aux
        item — the consumer cuts it the moment the real answer's first audio
        chunk is ready, so fast answers pay ~zero added latency.

        This is the FALLBACK trigger (barge-in-deferred turns reach
        `_handle_user_turn` without passing the idle flush block). The primary
        trigger is `_convert_filler_prefetch` at the flush; the shared
        `_last_filler_at` cooldown keeps the two from double-firing in one
        turn and bounds fragment-cascade repeats."""
        cfg = self.config
        if not cfg.voice_filler_enabled:
            return
        if _now() - self._last_filler_at < cfg.voice_filler_min_gap_seconds:
            return
        if self._speaking or self._playback_paused or self._tools_running:
            return
        if self._closed:
            return
        phrase = _pick_aux_phrase(cfg.voice_filler_phrases, self._last_filler_phrase)
        if not phrase:
            return
        if len(phrase) > 24:
            # A pathological over-long configured phrase would play in full
            # before the answer arrives — never (A4.9 M4).
            return
        self._last_filler_phrase = phrase
        self._last_filler_at = _now()
        await self._send_json({"event": "filler", "text": phrase})
        await self._speak_text(phrase, wait=False, aux=True)

    def _filler_idle_gates_ok(self) -> bool:
        """Shared gates for the speculative filler arm/convert: only when the
        session is fully idle (no playback, no paused answer, no active turn,
        no tool loop) may a filler be synthesized/queued — the same conditions
        under which the EoT flush routes to the plain `_enqueue_turn` path."""
        cfg = self.config
        if not cfg.voice_filler_enabled or self._closed:
            return False
        if self._speaking or self._playback_paused or self._turn_active or self._tools_running:
            return False
        return True

    def _drop_filler_prefetch(self) -> None:
        """Cancel and clear a live speculative filler synthesis (text changed,
        gates flipped, session draining/shutting down)."""
        p = self._filler_prefetch
        task = p.get("task")
        if task is not None and not task.done():
            task.cancel()
        self._filler_prefetch = {"text": "", "phrase": "", "q": None, "task": None}

    def _register_aux_echo(self, text: str) -> None:
        """Register an aux utterance (filler/backchannel) for mic-echo tail-
        stripping. Registers the SPLIT segments plus the joined phrase: the
        consumer typically cuts a filler mid-phrase, so the ASR tail of an
        interrupted aux word may only contain one segment. Shared by the
        flush-convert path and the `_speak_text(aux=True)` fallback so the
        two stay symmetric (2026-08-25 review I2)."""
        segments, rest = _split_segments(text)
        if rest.strip():
            segments.append(rest)
        spoken = [s for s in segments if s.strip()]
        if not spoken:
            return
        now = _now()
        self._aux_echo_guard.extend((s, now) for s in spoken)
        joined = "".join(spoken)
        if joined not in [p for p, _ in self._aux_echo_guard]:
            self._aux_echo_guard.append((joined, now))
        del self._aux_echo_guard[:-4]

    def _arm_filler_prefetch(self, text: str) -> None:
        """Speculatively start the filler phrase's TTS synthesis while the EoT
        watchdog is still deciding whether the utterance ended (silence has
        reached arm threshold). ARM-ONCE per live prefetch — the caller only
        invokes this when no prefetch is alive, so a long monologue's many
        mid-utterance pauses produce at most ONE synthesis (not one per
        FunASR partial, which 0.3-1.1s cadence would otherwise burn as
        cancelled provider calls — review I1). At the flush the phrase
        converts to a pre-synthesized aux item, so the filler's own TTS
        round-trip overlaps the generation pipeline instead of trailing it.
        Deterministic gates only (noise/stop-word/busy/cooldown): the LLM
        intent classifier runs too late to gate this, and the reject path
        already drains aux speech (A4.9 I2 pattern)."""
        if not self._filler_idle_gates_ok():
            return
        cfg = self.config
        if _now() - self._last_filler_at < cfg.voice_filler_min_gap_seconds:
            return
        t = (text or "").strip()
        if not t or _is_voice_noise(t):
            return
        if _STOP_TASK_RE.search(t) or _STOP_TASK_EXACT_RE.match(t):
            return
        phrase = _pick_aux_phrase(cfg.voice_filler_phrases, self._last_filler_phrase)
        if not phrase or len(phrase) > 24:
            return
        # Defensive: the watchdog only arms when no prefetch is alive; drop
        # anything stale so we never hold two candidate syntheses.
        self._drop_filler_prefetch()
        q: asyncio.Queue = asyncio.Queue()
        task = self._spawn_synth(phrase, self._turn_epoch, q)
        self._filler_prefetch = {"text": t, "phrase": phrase, "q": q, "task": task}

    async def _convert_filler_prefetch(self, flushed: str) -> None:
        """Convert the armed filler synthesis into a playable aux item at the
        EoT flush (idle path only, BEFORE `_enqueue_turn`): the consumer plays
        straight from the pre-synthesized queue, so the filler's audio is ready
        right as the turn starts (measured E2E: filler PCM ~0.5s after the
        filler event vs ~1.0s on the old post-persist path) and cuts over to
        the answer seamlessly.

        NO strict text match against the arm-time text: the filler phrase is
        content-independent, and the flushed text is normally LONGER than the
        tail that armed it (partials kept growing) — matching on equality
        would drop most converts (review I1). Security against the user text
        is re-gated here on the FLUSHED text (noise / stop-word / idle gates);
        a prefetch that fails them is dropped. The prefetch is ALWAYS cleared
        (one-shot)."""
        p = self._filler_prefetch
        self._filler_prefetch = {"text": "", "phrase": "", "q": None, "task": None}
        q = p.get("q")
        phrase = p.get("phrase") or ""
        if q is None or not phrase:
            return
        t = (flushed or "").strip()
        if not (
            bool(t)
            and self._filler_idle_gates_ok()
            and not _is_voice_noise(t)
            and not (_STOP_TASK_RE.search(t) or _STOP_TASK_EXACT_RE.match(t))
        ):
            return
        self._last_filler_phrase = phrase
        self._last_filler_at = _now()
        self._register_aux_echo(phrase)
        await self._send_json({"event": "filler", "text": phrase})
        self._tts_queue.put_nowait({
            "text": phrase, "epoch": self._turn_epoch, "aux": True, "pre_q": q,
        })

    async def _maybe_backchannel(self, silence: float, text: str) -> None:
        """Backchannel (应和): during the USER's speech, in a mid-utterance
        pause window (silence >= pause_seconds and below the semantic-EoT
        probe), the agent utters a short "嗯" to signal attentive listening —
        the classic human listening behavior that keeps a speaker going.

        Design constraints honored (no TTS conflict, no race, no overwrite):
        - Speech rides the SAME _tts_queue as everything else: the single
          consumer serializes it against answers/interjections/notices.
        - Only fires while the agent is fully idle (not speaking, not
          interjecting, no turn active, not paused, ASR ready) — it can never
          talk over the user's turn-taking machinery.
        - Only fires on INCOMPLETE utterances (no terminal punctuation) in a
          silence window that ENDS BEFORE the semantic-EoT probe: a complete
          utterance is about to flush and get answered, a probe-armed one is
          being judged — both would race the turn pipeline.
        - If a flush of the SAME utterance still lands while the ack plays
          (probe 1.0s + judge ~0.8s, or the 1.6s hard threshold, vs TTS
          round-trip ~0.7-1.3s + short phrase audio), the overlap is benign
          BY CONSTRUCTION: the aux-cut truncates the ack the moment the
          answer audio is ready, the barge-in no-interrupt window defers the
          flushed turn (queued + prefetched, answered immediately after), and
          the ack is excluded from the pause breakpoint pool so a resume
          never replays it (A4.9 M3).
        - Phrase length is bounded (~1-2 chars, ~0.3-0.6s audio) so the ack
          is over quickly and its own mic-echo passes the short-noise gate.
        - Cooldown + per-turn cap + same-text dedup + near-field gate + noise/
          echo pre-filters bound frequency and false positives."""
        cfg = self.config
        if not cfg.voice_backchannel_enabled:
            return
        if not self._asr_ready or self._speaking or self._interjecting:
            return
        if self._turn_active or self._playback_paused or self._closed:
            return
        if silence < cfg.voice_backchannel_pause_seconds or silence >= cfg.voice_eot_semantic_probe_seconds:
            return
        t = (text or "").strip()
        if not t or _utterance_complete(t):
            return
        if len(_strip_lead_punct(t)) < 2:
            return
        if _ASR_NOISE_RE.match(t) or _is_voice_noise(t):
            return
        if _is_likely_echo(t, self._spoken_text_recent):
            return
        if not self._prox_is_near():
            return
        now = _now()
        if now - self._last_backchannel_time < cfg.voice_backchannel_cooldown_seconds:
            return
        if t == self._backchannel_acked_text:
            return
        if self._backchannel_count_this_turn >= cfg.voice_backchannel_max_per_turn:
            return
        phrase = _pick_aux_phrase(cfg.voice_backchannel_phrases, self._last_backchannel_phrase)
        if not phrase:
            return
        if len(phrase) > 4:
            # Backchannel phrases MUST stay ~1-2 chars: a long one would (a)
            # stretch its audible window into the flush region and (b) its
            # mic-echo could pass the short-noise gate and become a phantom
            # turn (A4.9 M4).
            return
        self._last_backchannel_time = now
        self._last_backchannel_phrase = phrase
        self._backchannel_acked_text = t
        self._backchannel_count_this_turn += 1
        await self._send_json({"event": "agent_backchannel", "text": phrase})
        await self._speak_text(phrase, wait=False, aux=True)
        logger.info("voice backchannel: %r (pause %.2fs, text %r)", phrase, silence, t[-40:])

    def _drain_tts_queue(self) -> None:
        # Invalidate the pipeline prefetch: an interrupt/pause has gate-
        # truncated the in-flight prefetch synthesis, so its partial queue must
        # NEVER be replayed against a later item with the same text.
        self._tts_prefetch_q = None
        self._tts_prefetch_text = ""
        self._tts_prefetch_epoch = 0
        self._tts_prefetch_valid = False
        # The speculative filler prefetch dies with the drain too: the
        # pipeline is being reset (interrupt/pause), so its partial audio
        # must not convert at a later flush (2026-08-25).
        self._drop_filler_prefetch()
        # The pause's resume pre-synthesis is dropped too: a drain means the
        # answer is being abandoned/restarted, so its partial audio must not
        # be replayed by a later resume. (The synth task itself exits via the
        # interrupt gate and is tracked in self._tasks for _shutdown.)
        self._resume_q = None
        self._resume_epoch = 0
        self._resume_text = ""
        self._pre_classify = None
        while not self._tts_queue.empty():
            try:
                item = self._tts_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, dict) and "flush" in item:
                fut = item["flush"]
                if fut and not fut.done():
                    fut.set_result(False)

    # ---- turn handling ----
    def _enqueue_turn(self, text: str) -> None:
        """Queue a turn for the responder. Duplicate turns (same normalized
        text already pending — ASR re-emits finalized sentences, the EoT
        watchdog can re-flush the same text after a defer) are dropped so a
        sentence never gets answered twice or leaves a ghost message (conv
        689f06ec 13:59: "嗯，没啥，我再跟你聊聊。" enqueued twice)."""
        self._turn_enqueued_at = _now()
        norm = _norm_barge_compare(text)
        if norm:
            for item in self._turn_queue._queue:
                if (
                    isinstance(item, dict)
                    and not item.get("notice")
                    and _norm_barge_compare(item.get("text", "")) == norm
                ):
                    logger.info("voice enqueue skipped (duplicate queued turn): %r", text)
                    return
        self._turn_queue.put_nowait({"text": text})

    def _record_backchannel(self, text: str) -> None:
        """Remember a recent backchannel verdict for *text* so the responder
        can skip a queued copy of the same utterance (a backchannel-classified
        sentence is filler, not a turn — answering it produces a ghost
        unanswered message, conv 689f06ec)."""
        norm = _norm_barge_compare(text)
        if norm:
            self._recent_backchannel[norm] = _now()

    async def _queued_turn_is_backchannel(self, text: str) -> bool:
        """Decide whether a queued turn must be skipped (not answered) because
        the utterance was already judged backchannel.

        Paths covered (conv 689f06ec 13:59 incident): the utterance was
        deferred inside the window/cooldown and ENQUEUED without a verdict;
        the onset pre-classify (launched later, on the re-emitted partial)
        then returned backchannel 0.6s AFTER the responder had already
        persisted the user message. Here the responder (1) awaits a matching
        IN-FLIGHT pre-classify verdict (bounded — fail-open on timeout/error)
        and (2) consults the recent-backchannel registry recorded by
        _run_pre_classify and the flush-time backchannel branches. Anything
        else → False (the turn is processed normally)."""
        norm = _norm_barge_compare(text)
        if not norm:
            return False
        pre = self._pre_classify
        if pre and pre.get("text") and _norm_barge_compare(pre.get("text", "")) == norm:
            task = pre.get("task")
            if task and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except Exception:
                    pass  # fail-open: no verdict in time → process normally
            # Read the SNAPSHOT's verdict (A4.9 I2): the flush path may have
            # consumed or replaced self._pre_classify during the await; the
            # snapshot is the verdict this turn was matched against.
            if pre.get("action") == "backchannel":
                logger.info("voice queued turn skipped (in-flight pre-classify backchannel): %r", text)
                return True
        ts = self._recent_backchannel.get(norm)
        if ts is not None:
            if _now() - ts < self.config.voice_backchannel_recall_seconds:
                logger.info("voice queued turn skipped (recent backchannel verdict): %r", text)
                return True
            # Lazy cleanup (A4.9 M1): a stale entry is useless — drop it.
            self._recent_backchannel.pop(norm, None)
        return False

    async def _on_user_turn(self, text: str) -> None:
        # Serialized: EoT flushes spawn concurrent _on_user_turn tasks, and
        # the pause/resume + interrupt state machine must never interleave (a
        # backchannel resume must not clear/race a concurrent interrupt).
        async with self._turn_handling_lock:
            await self._on_user_turn_locked(text)

    async def _on_user_turn_locked(self, text: str) -> None:
        # Normalise: a genuine user utterance never starts with punctuation.
        # Streaming ASR can emit a slice beginning with the previous sentence's
        # terminator (e.g. "，快点"); strip it so turns are clean.
        text = _strip_lead_punct((text or "").strip())
        if not text:
            return

        # Explicit task-cancellation: if a task is running OR audio is still
        # being played (incl. the audible tail after a finished answer — the
        # user hears speech, so "停下" must work then too), a clear stop
        # command cancels it. The task loop checks _task_cancelled before each
        # tool round / LLM call and breaks out. Short commands (停下/等下) must
        # be the whole utterance; longer unambiguous phrases match as substrings.
        if (self._turn_active or self._speaking) and (
            _STOP_TASK_RE.search(text) or _STOP_TASK_EXACT_RE.match(text)
        ):
            self._task_cancelled = True
            # A stop-word also supersedes any onset pause (its breakpoint is
            # meaningless once the answer is abandoned).
            self._playback_paused = False
            self._paused_spoken_chars = 0
            self._interrupt.set()
            self._last_barge_in_time = _now()
            self._drain_tts_queue()
            await self._mark_speaking_end()
            await self._send_json({"event": "interrupted"})
            await self._send_json({"event": "task_cancelled", "text": text})
            logger.info("voice task cancelled by user: %r", text)
            return

        # Barge-in handling while speaking, OR after an acoustic onset-pause
        # (_playback_paused — speech already stopped the audio; the classifier
        # now decides resume-from-breakpoint vs switch to the new turn).
        if (self._speaking or self._playback_paused) and self.config.voice_barge_in_enabled:
            # Pre-filter: obvious ASR noise never interrupts TTS playback. In
            # the paused state the noise/echo verdict means "not a real turn"
            # → resume the paused answer from its breakpoint.
            if _ASR_NOISE_RE.match(text):
                logger.info("voice barge-in skipped (ASR noise): %r", text)
                if self._playback_paused:
                    await self._resume_playback()
                else:
                    await self._send_json({"event": "backchannel", "text": text})
                await self._set_state("speak")
                return
            # Deterministic echo guard: the agent's OWN TTS voice echoing back
            # through the mic is the #1 cause of mid-playback truncation. If the
            # interjection overlaps what is currently being spoken, it is echo,
            # not a real turn — keep playing (or resume).
            if _is_likely_echo(text, self._spoken_text_recent):
                logger.info("voice barge-in skipped (own-voice echo): %r", text)
                if self._playback_paused:
                    await self._resume_playback()
                else:
                    await self._send_json({"event": "backchannel", "text": text})
                await self._set_state("speak")
                return
            # If the agent is interjecting (a brief remark during the user's
            # speech), the user's completed turn always takes priority — cut
            # off the interjection immediately and process the turn. This check
            # runs AFTER the noise/echo filters so a stray noise fragment can't
            # kill an in-flight interjection.
            if self._interjecting:
                self._playback_paused = False
                self._paused_spoken_chars = 0
                self._interrupt.set()
                self._last_barge_in_time = _now()
                self._drain_tts_queue()
                await self._mark_speaking_end()
                self._interjecting = False
                logger.info("voice interjection cut off by user turn: %r", text)
                self._enqueue_turn(text)
                return
            # No-interrupt window + cooldown (only while actively PLAYING — a
            # paused playback already passed these gates at the onset pause).
            # In the first instants of a playback burst (and right after a
            # previous interrupt) the mic is most likely to pick up echo/reverb,
            # so a real utterance there is deferred instead of cutting playback:
            # it is still queued and will be answered as soon as the burst ends.
            if not self._playback_paused:
                now = _now()
                if (
                    now - self._speaking_started_at
                    < self.config.voice_barge_in_no_interrupt_seconds
                ) or (
                    now - self._last_barge_in_time
                    < self.config.voice_barge_in_cooldown_seconds
                ):
                    logger.info(
                        "voice barge-in deferred (window/cooldown): %r", text
                    )
                    self._enqueue_turn(text)
                    self._start_prefetch(text)
                    await self._send_json({"event": "deferred", "text": text})
                    await self._set_state("speak")
                    return
            self._barge_classify_start = _now()
            # Reuse the pre-classify verdict (fired at the onset pause, in
            # parallel with the EoT watchdog) when the flushed text matches
            # the classified partial — saves the ~1-1.5s classify on the
            # critical path, keeping the pause→resume silence within the
            # user's patience (previously 4-5s: users refreshed and the
            # answer stayed truncated).
            pre = self._pre_classify
            self._pre_classify = None
            if _pre_classify_reusable(pre, text):
                action = pre["action"]
                logger.info("voice barge-in classify (pre): action=%s text=%r", action, text)
            else:
                action = await self._classify_barge_in(text, history=list(self._history))
                logger.info("voice barge-in classify: action=%s text=%r", action, text)
            if action == "backchannel":
                self._record_backchannel(text)
                if self._playback_paused:
                    await self._resume_playback()
                else:
                    await self._send_json({"event": "backchannel", "text": text})
                await self._set_state("speak")
                return
            if action == "interrupt":
                await self._request_interrupt()
                self._enqueue_turn(text)
                return
            # defer: resume the paused answer (or keep playing), pre-generate
            # the reply for when the current answer ends.
            if self._playback_paused:
                await self._resume_playback()
            else:
                self._start_prefetch(text)
            self._enqueue_turn(text)
            await self._send_json({"event": "deferred", "text": text})
            await self._set_state("speak")
            return

        # Think-phase preemption: the assistant is GENERATING an answer but no
        # audio has reached the user yet (no barge-in path above). Without a
        # judgment here every non-stop-word utterance is queued and the user
        # waits for the whole in-flight answer to stream AND play out before
        # their interruption is handled — "can't interrupt". Run the SAME
        # barge-in classifier (LLM-based, never hardcoded): a clear interrupt
        # (new question, topic change, correction, stop) aborts the in-flight
        # generation NOW. Fail-safe (timeout/error) = defer: never abort a
        # stream on uncertainty. Tool execution is never aborted here (the
        # queued turn preempts the tool loop at the next round boundary).
        # NOTE: "think" here is the session state-machine state (waiting for
        # first audio), NOT the LLM thinking/reasoning mode — voice LLM calls
        # (main stream AND these classifier subagents) always run with
        # thinking disabled (voice.disable_thinking / _thinking_off_body).
        # The classify contends for _llm_gate with the in-flight main stream
        # and its intent subagent; a 6s gate wait degrades this to the
        # baseline defer path (the turn still gets answered afterwards), which
        # is the accepted trade-off vs. tripping provider rate limits.
        if (
            self._turn_active
            and not self._speaking
            and not self._tools_running
            and self.config.voice_barge_in_enabled
        ):
            self._barge_classify_start = _now()
            action = await self._classify_barge_in(text, history=list(self._history))
            logger.info(
                "voice think-phase barge-in classify: action=%s text=%r", action, text,
            )
            # Re-check state after the await: the in-flight turn may have
            # completed while the classifier ran — aborting then is pointless
            # and would set a spurious barge-in cooldown. (Audio may have
            # started during the classify — that is fine: cutting an answer
            # after audio began is exactly the speaking-path barge-in
            # semantic, the decision was already approved by the classifier.)
            if action == "interrupt" and self._turn_active and not self._closed:
                await self._request_interrupt()
                logger.info("voice think-phase preempted by user turn: %r", text)
                self._enqueue_turn(text)
                return

        self._enqueue_turn(text)
        if self._turn_active and not self._closed:
            # The user spoke while a turn (e.g. a long tool loop) is still
            # running. Their utterance is queued — acknowledge IMMEDIATELY so
            # they are never met with total silence. Substantive utterances
            # also get a brief spoken ack (cooldown-gated); the queued turn
            # itself preempts the tool loop at the next round boundary.
            await self._send_json({"event": "turn_queued", "text": text})
            if (
                self._tools_running
                and len(text) > self.config.voice_noise_gate_max_chars
                and _now() - self._last_queue_ack > 15.0
            ):
                self._last_queue_ack = _now()
                asyncio.create_task(self._speak_text(
                    "(平静)收到，你先等我把手头这步做完，马上就来回答你。"
                ))

    def _start_prefetch(self, text: str) -> None:
        """Pre-generate reply text for a deferred turn while speech continues."""
        if self._prefetch.get("task") and not self._prefetch["task"].done():
            return
        self._prefetch = {"turn": text, "text": None, "task": asyncio.create_task(self._generate_reply_text(text))}

    async def _generate_reply_text(self, text: str) -> str:
        """Generate the full reply text without speaking (used for prefetch)."""
        svc, _ = self._build_llm()
        messages = self._system_messages() + self._trimmed_history() + [
            {"role": "user", "content": text}
        ]
        out = []
        # Runs through the same provider gate as the main stream — the defer
        # path fires while the main stream and intent classify may still hold
        # gate slots; an ungated third call was the exact xiaomimimo 429
        # trigger the gate exists to prevent (A4.9 review finding #2).
        try:
            async with self._llm_gate:
                async for ev in svc.stream_chat_structured(
                    messages,
                    temperature=self.config.voice_temperature,
                    max_tokens=self.config.voice_max_tokens,
                    extra_body=self._extra_body(svc),
                ):
                    if ev["type"] == "content":
                        out.append(ev["data"])
        except Exception as exc:
            logger.debug("voice prefetch error: %s", exc)
        result = "".join(out)
        self._prefetch["text"] = result
        return result

    async def _request_interrupt(self) -> None:
        # Any confirmed interrupt SUPERSEDES an acoustic onset pause: the
        # answer is being abandoned, so the pause state (and its breakpoint)
        # must not linger — a later backchannel would otherwise "resume" a
        # stale breakpoint against the NEW turn's text (A4.9 finding C1).
        self._playback_paused = False
        self._paused_spoken_chars = 0
        self._interrupt.set()
        self._last_barge_in_time = _now()
        self._drain_tts_queue()
        # Attribute the truncation to AUDIO PLAYBACK, never to the text. The
        # answer text is complete; only its spoken playback was cut. Compute how
        # much had actually been voiced when playback stopped, and queue a note
        # so the next turn's LLM KNOWS this (instead of confabulating a reason
        # like "the system thought I talked too much").
        # The client's played_sec is BURST-relative (restarted at each
        # pause→resume — see the frontend playback_resumed handler), so the
        # turn-absolute heard position is _burst_base_sec + played_sec;
        # mapping the raw value against the turn-relative segment timeline
        # would report "~2% played" right after a resume (A4.9 finding).
        played = self._burst_base_sec + self._playback_played_sec
        total = self._turn_audio_sec_total
        # The interruption-note source: _turn_reply_text covers the WHOLE turn
        # (generated text, incl. before any audio); _turn_segments only covers
        # text whose audio was actually synthesized. Fall back to segments so
        # the prefetch path (no _turn_reply_text) keeps working.
        full = self._turn_reply_text or "".join(s["text"] for s in self._turn_segments)
        # Truncation happened if a meaningful tail of audio never played.
        if full.strip() and total > 0 and played < total - 0.3:
            spoken = self._spoken_text_so_far(played)
            tail = spoken[-24:] if spoken else "（开头不久）"
            pct = int(100 * played / total) if total else 0
            self._pending_interruption_note = (
                "（系统提示，不要朗读）：你上一段语音播报只播出了约 "
                f"{pct}% ，在「" + tail + "…」附近就被用户打断了，后面的内容没有播给用户听。用户很可能没听全。"
                "如果用户追问为什么中断/被截断/没说完，请如实说明是【语音播报被打断】而不是回答本身"
                "有问题，并主动用简短的话把没播完的内容补全或询问是否继续，不要编造其他原因。）"
            )
            logger.info(
                "voice playback interrupted (audio cut): played=%.2fs/%.2fs (%d%%) spoken=%d/%d chars",
                played, total, pct, len(spoken), len(full),
            )
        elif full.strip() and total <= 0:
            # Think-phase abort: an answer was being GENERATED (text shown in
            # the transcript) but its audio never started — the user heard
            # nothing. The next turn must know the previous answer was never
            # delivered so it can offer to re-answer instead of treating the
            # partial text as delivered (A4.9 finding).
            self._pending_interruption_note = (
                "（系统提示，不要朗读）：你上一轮生成的内容还没开始播报就被用户打断了，"
                "用户没有听到这段内容。如果用户追问或要求继续，请如实说明这段回答没有播出来，"
                "并简要重述要点或询问是否需要重讲，不要编造其他原因。"
            )
            logger.info(
                "voice think-phase answer aborted before playback (spoken=%d chars)",
                len(full),
            )
        await self._mark_speaking_end()
        await self._send_json({"event": "interrupted"})

    async def _pause_playback(self) -> None:
        """Acoustic onset barge-in: speech detected during playback — stop TTS
        IMMEDIATELY (before EoT + LLM classification would decide, ~2s). The
        barge-in classifier later decides: interrupt → answer the new turn;
        backchannel/defer → `_resume_playback()` from the breakpoint. The
        pause itself is NOT an intent decision — it is the acoustic-layer
        equivalent of FireRedChat's pVAD onset stop; echo/noise pre-filters
        and the window/cooldown gates run at the CALLER (see _asr_pipeline).
        Deliberately does NOT set `_interrupt`: the in-flight GENERATION keeps
        running while the audio is paused, so a defer outcome can resume the
        FULL answer (generated tail included) instead of a truncated one
        (A4.9 finding I2). The TTS consumer checks `_playback_paused`."""
        if self._playback_paused or self._closed:
            return
        self._playback_paused = True
        completed = "".join(s["text"] for s in self._turn_segments)
        full_txt = self._turn_reply_text or ""
        # Breakpoint = how much the user ACTUALLY HEARD, not how much was
        # SENT. The pipelined TTS prefetch races the consumer ahead of the
        # client by seconds of buffered audio, so `_turn_segments` (segments
        # whose audio was handed to the websocket) can say "everything" while
        # the user has heard a fraction — a pause then computed a full-text
        # breakpoint and the resume had "no remaining content", truncating
        # the answer (observed live: heard "欢迎回来！我在这儿呢…还" of a
        # 33-char answer, resume was empty).
        # PRIMARY: the client's AUDIBLE position (played_sec) mapped to chars
        # with the chars-per-second rate of the COMPLETED segments — both
        # measured from the same audio, so the mapping is self-consistent.
        # NOT played/total_sec: mid-stream the client's total is still
        # growing (only part of the answer has been received), so that
        # fraction overestimates progress and SKIPS unheard content
        # (observed live 13:47: paused at 5.48s of an ~40s answer, the
        # played/total mapping landed at char 80 of 164 — 40+ chars of
        # unheard speech were skipped).
        if full_txt and self._playback_played_sec > 0:
            # PRIMARY: the client's AUDIBLE position (played_sec, relative to
            # the CURRENT burst) mapped to chars with the turn's chars-per-
            # second rate (completed segments only — the in-flight segment is
            # covered by played_sec itself). The burst base anchors resumed
            # bursts: after a pause→resume the client restarts its clock at 0
            # from the resume point, so the full-text position is
            # burst_base_chars + played_sec * rate. WITHOUT the base, a
            # post-resume pause would estimate a position inside the resume
            # segment and `full_txt[est:]` replays already-heard text
            # (observed live conv 964462a8: pause#2 est=175 vs correct ~325
            # → an ~18s audible repeat of chars 175-354).
            comp_audio = sum(s["audio_sec"] for s in self._turn_segments)
            if len(completed) > 0 and comp_audio > 0:
                rate = len(completed) / comp_audio
                est = self._burst_base_chars + int(self._playback_played_sec * rate)
            elif self._burst_is_first and self._current_seg_text:
                # No completed segments — the FIRST segment of the turn's
                # ORIGINAL burst is still in flight: estimate its played
                # fraction. Safe only here (the segment is short and the snap
                # below guarantees a word boundary). NEVER on a resumed burst
                # (_burst_is_first False): the resumed remainder is one large
                # segment and _current_seg_audio_sec (sent so far) is far
                # below its total, so a fraction of it would SKIP unheard
                # content — replay from the resume point instead.
                frac = 0.0
                if self._current_seg_audio_sec > 0:
                    frac = min(
                        1.0,
                        max(0.0, self._playback_played_sec)
                        / self._current_seg_audio_sec,
                    )
                est = int(len(self._current_seg_text) * frac)
            else:
                # Pause in the first ~second of a resumed burst (or no client
                # position): the burst base plus what completed IN this burst
                # — never skip unheard content.
                est = self._burst_base_chars + self._completed_chars_in_burst()
        else:
            # FALLBACK (no client position — headless clients): the burst base
            # plus segments completed WITHIN this burst only (the epoch-wide
            # completed prefix already lies BEFORE the burst and is counted by
            # the base — adding it again would overshoot past the text end and
            # truncate the resume).
            est = self._burst_base_chars + self._completed_chars_in_burst()
        # SNAP to a safe clause boundary: the raw estimate is an exact
        # character count that can land MID-WORD (observed in the wild:
        # "...刚才可能是信|号..." and "...吃过|饭..."), and the resume
        # re-synthesizes from that character — a mid-word cut sounds broken.
        # Scan the last 8 chars for a boundary (punctuation/space/closing
        # style-tag paren); if none, scan the whole prefix (a natural
        # restart point); last resort: the very start (repeat, never cut).
        if full_txt and 0 < est < len(full_txt):
            lo = max(0, est - 8)
            window = full_txt[lo:est]
            safe = -1
            for i, ch in enumerate(window):
                if ch in _SAFE_RESUME_BOUNDARIES:
                    safe = i
            if safe >= 0:
                est = lo + safe + 1
            else:
                safe2 = -1
                for i, ch in enumerate(full_txt[:est]):
                    if ch in _SAFE_RESUME_BOUNDARIES:
                        safe2 = i
                est = (safe2 + 1) if safe2 >= 0 else 0
        self._paused_spoken_chars = est
        self._last_barge_in_time = _now()
        self._drain_tts_queue()
        # Pre-synthesize the unspoken remainder WHILE the barge-in classifier
        # runs (~1s) so a backchannel resume plays almost immediately instead
        # of paying a fresh provider round-trip (~0.9s) — the audible
        # pause→resume gap drops by roughly half. ignore_paused: this synth
        # must run DURING the pause (that is its purpose); it is still gated
        # by interrupt/close and dropped on any non-resume outcome.
        remaining = _strip_lead_punct(full_txt[self._paused_spoken_chars:]).strip()
        self._resume_epoch = 0
        self._resume_q = None
        self._resume_text = ""
        if remaining:
            self._resume_epoch = self._turn_epoch
            self._resume_q = asyncio.Queue()
            self._resume_text = remaining
            self._spawn_synth(remaining, self._turn_epoch, self._resume_q, ignore_paused=True)
        await self._mark_speaking_end()
        await self._send_json({
            "event": "playback_paused",
            "paused_at_chars": self._paused_spoken_chars,
        })
        logger.info(
            "voice playback paused (onset): spoken_chars=%d (client played %.2fs, sent %.2fs)",
            self._paused_spoken_chars, self._playback_played_sec, self._turn_audio_sec_total,
        )

    async def _run_pre_classify(self, text: str) -> None:
        """Background barge-in classify fired at the acoustic onset pause
        (parallel with the EoT watchdog). The verdict is only reused by
        _on_user_turn_locked when the flushed text matches this partial."""
        # Snapshot BEFORE the await (A4.9 I1): the flush path may consume
        # `self._pre_classify` (set it to None) while the LLM call is in
        # flight. The identity check `self._pre_classify is pre` below only
        # means anything if `pre` was captured here — a post-await read makes
        # the guard a no-op (both refer to the same current value).
        pre = self._pre_classify
        try:
            action = await self._classify_barge_in(text, history=list(self._history))
            if pre and pre.get("text") == text:
                pre["action"] = action
                logger.info("voice pre-classify: action=%s text=%r", action, text)
            if action == "backchannel" and pre is not None and self._pre_classify is pre:
                # Record so the responder can skip a queued copy of this
                # utterance that was already enqueued by the window/cooldown
                # defer path (ghost-message fix, conv 689f06ec). Only when the
                # verdict is still authoritative: if the flush consumed it
                # (re-classified the SAME sentence, possibly as interrupt) or
                # a newer onset replaced it, recording a stale backchannel
                # would wrongly recall a REAL interrupt turn into silence.
                self._record_backchannel(text)
        except Exception as exc:
            logger.debug("voice pre-classify failed: %s", exc)

    async def _resume_playback(self) -> None:
        """Resume a paused answer from the breakpoint (backchannel/defer
        outcome of the barge-in classifier). Re-synthesizes the unspoken
        remainder of the generated text — the already-played prefix is not
        repeated. No-op when nothing was paused. Does NOT touch `_interrupt`:
        under the _turn_handling_lock no concurrent interrupt can be in flight,
        and the pause itself never set it (see _pause_playback)."""
        if not self._playback_paused or self._closed:
            return
        self._playback_paused = False
        full = self._turn_reply_text or ""
        # Never resume from a leading punctuation: the breakpoint snap can
        # land right before a clause boundary, making the remainder start
        # with "。"/"，" — such text defeats _split_segments (no split → one
        # giant segment) and a leading "。(风格)…" input hung the MiMo
        # provider, stalling the consumer forever (observed live).
        remaining = _strip_lead_punct(full[self._paused_spoken_chars:]).strip() if full else ""
        # The resumed audio is a NEW audible burst: the client restarts its
        # playback clock at 0 from here, so anchor the pause-breakpoint math
        # to this full-text position and audio offset until the next pause
        # (see _pause_playback). The burst is no longer the turn's first one,
        # so the in-flight played-fraction estimate must never apply (the
        # resumed remainder is one large segment — see _pause_playback).
        self._burst_base_chars = self._paused_spoken_chars
        self._burst_base_sec = self._turn_audio_sec_total
        self._burst_is_first = False
        self._paused_spoken_chars = 0
        if remaining:
            # Prefer the pre-synthesized remainder (spawned at pause time so
            # the provider round-trip overlaps the barge-in classifier) — the
            # resumed audio then starts almost immediately instead of after a
            # fresh ~0.9s synthesis. Guards: same turn epoch, no interrupt,
            # and the queue must not have been dropped by a drain.
            # The pre_q item carries the SNAPSHOT text synthesized at pause
            # time (`_resume_text`) — the answer may have KEPT STREAMING
            # during the pause, so the current `remaining` can be longer than
            # what the queue's audio covers; the extra tail (delta) is
            # enqueued as fresh segments after the pre_q item instead of
            # being silently swallowed (observed live 13:47: pre_q text said
            # 106 chars but its audio covered only 84 — the last 22 chars
            # were never heard).
            if (
                self._resume_q is not None
                and self._resume_epoch == self._turn_epoch
                and not self._interrupt.is_set()
                and not self._closed
            ):
                q = self._resume_q
                snap = self._resume_text or ""
                self._resume_q = None
                self._resume_text = ""
                if snap and remaining.startswith(snap):
                    # The delta is the text that streamed AFTER the pause —
                    # it can begin with a punctuation that the snapshot did
                    # not cover (e.g. the LLM emitted "。…" as the first token
                    # of a new chunk); strip it so the delta splits normally
                    # and the provider never receives a leading "。" text.
                    delta = _strip_lead_punct(remaining[len(snap):]).strip()
                    logger.info(
                        "voice playback resumed via pre-synthesized remainder: %d chars (+%d streamed during pause)",
                        len(snap), len(delta),
                    )
                    await self._tts_queue.put({
                        "text": snap, "epoch": self._turn_epoch, "pre_q": q,
                    })
                    if delta:
                        await self._speak_text(delta, wait=False)
                else:
                    # Snapshot diverged from the current text (defensive) —
                    # fresh synthesis covers everything.
                    logger.info(
                        "voice playback resumed from breakpoint: remaining=%d chars",
                        len(remaining),
                    )
                    await self._speak_text(remaining)
            else:
                logger.info(
                    "voice playback resumed from breakpoint: remaining=%d chars",
                    len(remaining),
                )
                await self._speak_text(remaining)
        else:
            logger.info("voice playback resumed: no remaining content")
        await self._send_json({"event": "playback_resumed"})

    async def _responder(self) -> None:
        while not self._closed:
            item = await self._turn_queue.get()
            if item is None:
                return
            notice = item.get("notice")
            if notice:
                # Out-of-band background-task completion notice. Riding the
                # turn queue keeps it serialized with user turns: it waits
                # for the in-flight turn instead of interrupting it.
                try:
                    await self._deliver_bg_task_notice(notice)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("voice bg-task notice error: %s", exc, exc_info=True)
                continue
            text = item.get("text", "")
            try:
                text = await self._coalesce_fragments(text)
                if not text.strip():
                    continue
                # A queued turn whose utterance was already judged backchannel
                # (window/cooldown defer + late pre-classify verdict, or a
                # flush-time backchannel) must NOT be answered — it is filler,
                # and answering it leaves a ghost unanswered message (conv
                # 689f06ec: "嗯，没啥，我再跟你聊聊。" persisted after the
                # real answer, orphaned).
                if await self._queued_turn_is_backchannel(text):
                    await self._send_json({"event": "backchannel", "text": text})
                    await self._set_state("speak")
                    continue
                await self._handle_user_turn(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("voice handle turn error: %s", exc, exc_info=True)
                await self._send_json({"event": "error", "error": f"处理出错: {exc}"})
                await self._set_state("listen")

    async def _coalesce_fragments(self, text: str) -> str:
        """Reassemble one continuous utterance that the EoT chopped into
        several queued fragments. When upstream ASR results stall (noise
        threshold, reconnect, echo mix), the watchdog can flush mid-speech;
        without coalescing every fragment becomes its own answered turn and
        the conversation shatters (observed 2026-07-21: one utterance → 11
        fragment turns, each interrupting the previous answer).

        Strategy: fragments already queued (backlog — the cascade signature)
        merge immediately, and while the accumulated text lacks terminal
        punctuation (the signature of a cut) a short probe window waits for
        the next fragment, re-arming on each arrival, bounded overall by
        ``voice_fragment_merge_max_seconds``. A complete turn with an empty
        queue returns instantly — normal short queries see zero added
        latency. Deliberately NO waiting on live unflushed partials: a single
        chopped head is answered quickly and its tail becomes a follow-up
        turn with full context, which beats stalling the head answer."""
        merged = text
        deadline = _now() + self.config.voice_fragment_merge_max_seconds
        while not self._closed:
            nxt_item = None
            got = False
            if not self._turn_queue.empty():
                try:
                    nxt_item = self._turn_queue.get_nowait()
                    got = True
                except asyncio.QueueEmpty:
                    pass
            if not got:
                if _utterance_complete(merged) or _now() >= deadline:
                    break
                wait = min(self.config.voice_fragment_merge_seconds, deadline - _now())
                try:
                    nxt_item = await asyncio.wait_for(self._turn_queue.get(), timeout=wait)
                    got = True
                except asyncio.TimeoutError:
                    break
            if nxt_item is None:
                # Poison pill (shutdown) arrived mid-merge — hand it back to
                # the main responder loop so it can terminate cleanly.
                self._turn_queue.put_nowait(None)
                break
            if nxt_item.get("notice"):
                # Not a speech fragment — a background-task notice. Put it
                # back for the responder and process the merged text first.
                self._turn_queue.put_nowait(nxt_item)
                break
            nxt = (nxt_item.get("text", "") or "").strip()
            if not nxt:
                continue
            merged = f"{merged}{nxt}" if merged else nxt
            logger.info("voice fragment merged: %d chars -> %r", len(nxt), merged[-40:])
        if merged != text:
            logger.info("voice turn coalesced: %r -> %r", text[:40], merged[:60])
        return merged

    async def _handle_user_turn(self, text: str) -> None:
        self._interrupt.clear()
        self._style_tag = ""
        self._turn_active = True
        await self._set_state("think")

        # Deterministic noise pre-filter: fast check for transcribed ambient
        # sounds (coughs, sighs, throat clearing) before any LLM call. This
        # catches obvious noise that streaming ASR routinely emits, avoids
        # burning API calls, and never needs the fail-safe default.
        if _is_voice_noise(text):
            logger.info("voice turn ignored (deterministic noise): %r", text)
            self._turn_active = False
            if not self._closed:
                await self._set_state("listen")
            return

        # Send user_turn IMMEDIATELY so the frontend shows the user message
        # BEFORE the assistant's streaming response. If the intent classifier
        # later rejects this turn (noise/fragment), we send user_turn_cancelled
        # to remove it. This fixes the display-order bug where the assistant's
        # partial response appeared before the user's message.

        await self._send_json({"event": "user_turn", "text": text})

        # Use prefetched text if this turn was pre-generated during a defer.
        prefetched = None
        if self._prefetch.get("turn") == text:
            task = self._prefetch.get("task")
            if task:
                try:
                    prefetched = await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
                except Exception:
                    prefetched = None
            self._prefetch = {"turn": None, "text": None, "task": None}
        else:
            # Turn text no longer matches the prefetch (e.g. fragments were
            # coalesced into a longer turn) — cancel the stale generation so
            # it doesn't keep burning LLM tokens for a discarded fragment.
            stale = self._prefetch.get("task")
            if stale and not stale.done():
                stale.cancel()
            if self._prefetch.get("turn") is not None:
                self._prefetch = {"turn": None, "text": None, "task": None}

        # Filler prefix: cover the LLM generation time with a random short
        # phrase ("我来想想啊"…). Skipped when the answer was prefetched (it is
        # about to speak immediately — a filler would only add delay). The
        # phrase is an aux item: the consumer cuts it when the real answer's
        # first audio chunk is ready, so even a fast answer is not delayed.
        # Fires BEFORE the user-message DB persist: the DB write must never
        # delay the waiting phrase (2026-08-25). For turns that arrived via
        # the idle flush block this is usually a no-op — the flush already
        # converted the speculative prefetch and stamped the cooldown.
        if prefetched is None:
            await self._speak_filler()

        await self._persist_message("user", text)

        # Snapshot of the recent dialogue (before this turn) for the subagent.
        # We intentionally exclude the very message being judged. Bound the
        # snapshot to voice_intent_context_turns (the config knob was defined
        # but never applied — the full history was formatted into the intent
        # prompt on every turn, growing its prefill without bound over a long
        # session and slowing the (non-blocking but wasteful) classifier).
        turns = self.config.voice_intent_context_turns
        if turns > 0:
            tagged = [
                m for m in self._history
                if m.get("role") in ("user", "assistant")
            ]
            history_snapshot = list(tagged[-turns * 2:])
        else:
            history_snapshot = list(self._history)

        # 每轮 fire-and-forget 记忆召回（vmem）：不进入关键路径。快照传拷贝
        # （后续 _history 会 mutate）。轮 epoch 用自增前值：本轮生成的实际
        # epoch = _turn_epoch + 1（_generate_and_speak 内自增），watcher 以
        # "> 钩子时点 epoch" 判定本轮生成结束。
        self._start_memory_recall(self._turn_epoch, text, list(history_snapshot))

        _perf("turn_start", (_now() - self._turn_enqueued_at) * 1000 if self._turn_enqueued_at else 0,
              text_len=len(text), queued=(self._turn_enqueued_at is not None))
        self._turn_started = _now()

        # Staggered parallel subagent + main-agent pattern: the main LLM starts
        # generating IMMEDIATELY while the intent subagent runs in parallel with
        # a short stagger. The stagger prevents simultaneous API requests to the
        # same provider from hitting rate limits (429 Too Many Requests observed
        # with xiaomimimo), which would otherwise trigger OpenAI-client retries
        # that add 0.5-1.5s of latency. The subagent is tiny (max_tokens=160)
        # so the stagger adds negligible time-to-first-audio overhead.
        gen_task = asyncio.create_task(
            self._generate_and_speak_cancellable(text, prefetched=prefetched)
        )
        await asyncio.sleep(0.5)
        self._intent_classify_start = _now()
        intent_task = asyncio.create_task(
            self._classify_intent(text, history=history_snapshot)
        )

        # Wait for the subagent (it's the faster of the two).
        intent = None
        try:
            intent = await asyncio.wait_for(intent_task, timeout=10.0)
        except asyncio.TimeoutError:
            intent = {"should_respond": True, "needs_tools": False, "reason": ""}
        except Exception:
            intent = {"should_respond": True, "needs_tools": False, "reason": ""}

        if not intent.get("should_respond", True):
            # Off-topic/background noise — cancel the in-flight generation immediately.
            logger.info(
                "voice turn ignored (not respondable): %r reason=%s",
                text, intent.get("reason", ""),
            )
            gen_task.cancel()
            try:
                await gen_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            stale = self._prefetch.get("task")
            if stale and not stale.done():
                stale.cancel()
            self._prefetch = {"turn": None, "text": None, "task": None}
            # Remove the user message from the frontend (it was noise/fragment).
            await self._send_json({"event": "user_turn_cancelled"})
            await self._send_json({"event": "ignored", "text": text, "reason": intent.get("reason", "")})
            # Roll back the persisted user message so noise doesn't pollute the DB.
            await self._rollback_last_user_message(text)
            self._turn_active = False
            self._interjection_snapshot = []
            self._decay_emotion()
            if not self._closed:
                await self._set_state("listen")
            return

        # Subagent approved — the user_turn was already sent at the start of
        # this method. Just wait for generation to complete (it has already
        # been streaming while the subagent ran).

        # Wait for the (already-running) generation to finish. Tools stay
        # enabled for every turn on purpose: whether to call a tool is the main
        # LLM's own agentic decision (it sees the tool list and chooses), and
        # the intent subagent's needs_tools is only advisory — its fail-safe on
        # timeout/error is needs_tools=False, which would wrongly strip tools
        # from real task requests whenever the subagent is slow.
        try:
            await gen_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("voice generation error: %s", exc)

        self._turn_active = False
        self._tools_running = False
        self._interjection_snapshot = []
        self._decay_emotion()
        if not self._closed:
            await self._set_state("listen")

    async def _enqueue_bg_task_notice(self, info: dict) -> None:
        """Queue an out-of-band background-task notice. Riding the normal turn
        queue means the announcement never interrupts an in-flight user turn —
        it is delivered as soon as the session goes idle."""
        if self._closed:
            return
        logger.info(
            "voice bg-task notice queued: %s status=%s title=%r",
            info.get("task_id"), info.get("status"), info.get("title"),
        )
        await self._turn_queue.put({"notice": info})

    async def _deliver_bg_task_notice(self, info: dict) -> None:
        """Proactively announce a finished background task and offer follow-up
        actions. The notice (goal + full result + note linkage) goes into the
        LLM context as a system-marked message, so the user's spoken reply
        ("播报一下" / "导出PDF" / "存到笔记") is handled by the normal agentic
        tool loop with the real result in context — nothing is hardcoded."""
        if self._closed:
            return
        self._interrupt.clear()
        self._turn_active = True
        await self._set_state("think")
        await self._send_json({
            "event": "bg_task_notice",
            "task_id": info.get("task_id", ""),
            "title": info.get("title", ""),
            "status": info.get("status", ""),
        })
        title = info.get("title") or "未命名任务"
        goal = info.get("goal") or ""
        if info.get("status") == "completed":
            note_hint = ""
            if info.get("output_note_id"):
                note_hint = f"结果也已自动保存到默认笔记本（笔记ID：{info['output_note_id']}）。"
            prompt_text = (
                f"[系统通知] 你之前根据用户的语音请求提交的后台任务《{title}》已经完成。"
                f"任务目标是：{goal}。\n"
                f"任务结果全文如下：\n{info.get('result') or '（无文本输出）'}\n"
                f"结果已自动保存到当前对话。{note_hint}"
                "现在请你立刻主动开口：用一两句口语告诉用户这个任务已完成、简要说明做成了什么，"
                "然后询问用户接下来想怎么做——要不要现在听你播报一下结果要点，"
                "要不要导出成PDF，要不要整理保存到笔记，还是做点别的。"
                "之后如果用户要求播报，用自然的口语转述结果要点，不要逐字念、不要读出格式符号；"
                "如果用户要导出，调用 pdf_export 工具；如果用户要保存或整理到笔记，调用 notes 工具。"
            )
        else:
            prompt_text = (
                f"[系统通知] 你之前根据用户的语音请求提交的后台任务《{title}》执行失败了。"
                f"任务目标是：{goal}。失败原因：{info.get('error') or '未知'}。"
                "现在请你立刻主动开口：用一两句口语告诉用户这个任务没做成，简单说明原因，"
                "然后询问用户要不要换个方式重试，还是先聊点别的。"
            )
        try:
            await self._generate_and_speak(prompt_text, allow_tools=True)
        finally:
            self._turn_active = False
            self._tools_running = False
            if not self._closed:
                await self._set_state("listen")

    async def _generate_and_speak(self, text: str, allow_tools: bool = False, prefetched: Optional[str] = None) -> None:
        try:
            await self._generate_and_speak_impl(text, allow_tools=allow_tools, prefetched=prefetched)
        finally:
            # vmem：本轮生成结束信号（正常/取消/异常全部到达；记忆插话仲裁
            # 以 "epoch > 钩子时点" 判本轮，超时封顶，等待永远不挂死）。
            self._vmem_gen_done_epoch = self._turn_epoch
            self._vmem_gen_done.set()

    async def _generate_and_speak_impl(self, text: str, allow_tools: bool = False, prefetched: Optional[str] = None) -> None:
        self._history.append({"role": "user", "content": text})
        self._task_cancelled = False
        self._turn_msg_id = None
        # This turn's playback-attribution counters start empty: an interrupt
        # during generation must not attribute truncation percentages to the
        # PREVIOUS turn's audio (the _request_interrupt note math reads these).
        # Echo detection (_spoken_text_recent) and the speaking safety deadline
        # (_speaking_deadline) are deliberately NOT reset here — the previous
        # burst's audio may still be playing (deferred-then-answered case).
        self._turn_segments = []
        self._turn_audio_sec_total = 0.0
        self._current_seg_text = ""
        self._current_seg_audio_sec = 0.0
        self._playback_played_sec = 0.0
        self._playback_total_sec = 0.0
        self._burst_base_chars = 0
        self._burst_base_sec = 0.0
        self._burst_is_first = True
        self._turn_reply_text = ""
        # Anchor the send-pacing clock at the turn start. The consumer's
        # attribution reset is epoch-gated and does NOT fire on the first
        # (empty-list) segment, so without this the 1x send throttle would
        # keep a stale anchor from a previous turn, flood the websocket
        # buffers (~2MB ≈ 40s of audio) and stall progressively — observed
        # live: per-segment delays 0.8s → 2.7s → 17.6s → 42s → 59s → 119s.
        self._send_pace_start = _now()
        # A new generation invalidates any stale onset-pause breakpoint from a
        # previous turn (defense in depth for the C1 fix in _request_interrupt),
        # and starts a new epoch: the onset-pause hook only fires while the
        # playing burst belongs to THIS epoch (see _speaking_epoch).
        self._turn_epoch += 1
        self._playback_paused = False
        self._paused_spoken_chars = 0

        # Fast path: prefetched text (no tools) -> speak directly.
        if prefetched:
            await self._set_state("speak")
            clean = strip_voice_tags(prefetched)
            await self._send_json({"event": "assistant_text", "text": clean, "done": False})
            await self._speak_text(prefetched)
            self._history.append({"role": "assistant", "content": prefetched})
            self._last_answer_text = prefetched
            self._vmem_answer_epoch = self._turn_epoch
            await self._persist_message("assistant", prefetched)
            await self._send_json({"event": "assistant_text", "text": clean, "done": True})
            return

        svc, _ = self._build_llm()
        tools = None
        if allow_tools:
            try:
                from app.tools.registry import registry

                tools = registry.get_definitions(_voice_tool_names(), quiet=True) or None
            except Exception as exc:
                logger.debug("voice tool defs unavailable: %s", exc)
                tools = None

        messages = self._system_messages() + self._trimmed_history()
        full_reply = ""
        tool_rounds = 0
        max_tool_rounds = 8
        notice_done = False
        # Accumulate tool interaction for persistence (fix: save tool calls
        # to session record so the transcript shows what tools were used).
        _all_tool_calls: list[dict] = []
        _all_tool_results: list[dict] = []

        while True:
            # User explicitly cancelled the task — stop immediately.
            # NOTE: _interrupt (TTS barge-in) does NOT abort the task loop —
            # the task continues even if the user interrupts the speech. Only
            # an explicit stop command (_task_cancelled) or disconnect stops it.
            if self._task_cancelled:
                await self._send_json({"event": "assistant_text", "text": "（任务已取消）", "done": True})
                self._history.append({"role": "assistant", "content": "（任务已取消）"})
                return
            # Preemption: the user spoke while tools were running. Their turn
            # is sitting in the queue; starting another tool round would keep
            # them waiting in silence. Stop the loop HERE (between rounds — a
            # tool call in flight is never aborted) and let the queued turn
            # take the floor. Tool results gathered so far stay in history, so
            # the next turn's answer can use them and the task can be resumed
            # with a simple "继续".
            if tool_rounds > 0 and not self._turn_queue.empty():
                logger.info(
                    "voice tool loop preempted by pending user turn (round %d)",
                    tool_rounds,
                )
                break
            seg_buffer = ""
            reply_text = ""
            tool_calls = None
            await self._set_state("speak" if tool_rounds else "think")
            _llm_start = _now()
            _first_content = False
            _rate_retries = 0
            # Retry wrapper: xiaomimimo hard rate-limits when the voice
            # session fires several LLM calls at once (interjection + intent +
            # main stream — the 0.5s stagger alone does not always prevent
            # 429s). Without a retry the whole turn dies with a "生成失败"
            # error event and the user gets NO answer. Retry only while
            # nothing has been produced (retrying mid-stream would duplicate
            # content) and only on rate-limit errors.
            async with self._llm_gate:
                while True:
                    try:
                        async for ev in svc.stream_chat_structured(
                            messages,
                            tools=tools,
                            temperature=self.config.voice_temperature,
                            max_tokens=self.config.voice_max_tokens,
                            extra_body=self._extra_body(svc),
                        ):
                            if self._closed or self._task_cancelled:
                                return
                            if self._interrupt.is_set() and tool_calls is None:
                                # Barge-in: the user interrupted this answer mid-playback.
                                # Stop generating it NOW — their queued turn must start
                                # immediately, not after this stream burns to its end
                                # (observed live: 11s dead wait after an interrupt).
                                # A completed tool_calls payload is never aborted here.
                                logger.info("voice stream aborted early (barge-in)")
                                break
                            etype = ev["type"]
                            if etype == "content":
                                data = ev["data"]
                                if not _first_content:
                                    _first_content = True
                                    _perf("llm_ttft", (_now() - _llm_start) * 1000,
                                          round_no=tool_rounds, turn_ms=(_now() - self._turn_started) * 1000)
                                reply_text += data
                                seg_buffer += data
                                # Mirror the accumulated text into the
                                # interruption-note source INCREMENTALLY: a
                                # concurrent think-phase preemption may call
                                # _request_interrupt at any moment — mirroring
                                # only after the round would race with it.
                                self._turn_reply_text = full_reply + reply_text
                                # capture leading style tag
                                if not self._style_tag:
                                    m = re.match(r"^\s*[(（][^)）]{1,12}[)）]", seg_buffer)
                                    if m:
                                        self._style_tag = m.group(0)
                                # stream clean accumulated display text
                                await self._send_json(
                                    {"event": "assistant_text", "text": strip_voice_tags(reply_text), "done": False}
                                )
                                segs, seg_buffer = _split_segments(seg_buffer)
                                for s in segs:
                                    if s.strip():
                                        _perf("seg_to_tts", (_now() - _llm_start) * 1000,
                                              round_no=tool_rounds, seg_len=len(s))
                                        await self._tts_queue.put({"text": s, "epoch": self._turn_epoch, "dedup": True})
                            elif etype == "tool_calls":
                                tool_calls = ev["data"]
                            elif etype == "error":
                                raise RuntimeError(str(ev["data"]))
                        break
                    except Exception as exc:
                        if (
                            _rate_retries < self.config.voice_llm_retry_attempts
                            and not reply_text
                            and tool_calls is None
                            and not self._interrupt.is_set()
                            and _is_voice_rate_limit(exc)
                        ):
                            _rate_retries += 1
                            delay = 1.0 * _rate_retries
                            logger.warning(
                                "voice main LLM rate-limited (retry %d/%d) — backing off %.1fs: %s",
                                _rate_retries, self.config.voice_llm_retry_attempts, delay, exc,
                            )
                            await asyncio.sleep(delay)
                            continue
                        logger.error("voice main LLM error: %s", exc)
                        if not reply_text:
                            await self._send_json({"event": "error", "error": f"生成失败: {exc}"})
                        break            # flush remaining buffer as a final segment
            if seg_buffer.strip():
                await self._tts_queue.put({"text": seg_buffer, "epoch": self._turn_epoch, "dedup": True})
            if self._turn_reply_text != full_reply + reply_text:
                self._turn_reply_text = full_reply + reply_text

            if tool_calls and allow_tools and tool_rounds < max_tool_rounds and not self._task_cancelled:
                tool_rounds += 1
                if not notice_done:
                    notice_done = True
                    # Tool-name language follows the assistant's own reply when
                    # available, else the user's utterance — a Chinese reply is
                    # announced as 联网查询, an English one as "web search".
                    lang = _detect_text_language(reply_text) if reply_text and reply_text.strip() else _detect_text_language(text)
                    names = _build_tool_notice_names(tool_calls, lang)
                    notice = f"(平静)好的，我来调用{names}工具帮你处理。这个任务可能需要较长时间，请耐心等待，我会持续为你工作。"
                    await self._send_json({"event": "tool_notice", "text": strip_voice_tags(notice)})
                    await self._speak_text(notice)
                elif tool_rounds == 3:
                    progress_notice = "(平静)任务仍在进行中，请继续耐心等待。"
                    await self._send_json({"event": "tool_notice", "text": strip_voice_tags(progress_notice)})
                    await self._speak_text(progress_notice)
                elif tool_rounds == 5:
                    progress_notice = "(平静)这个任务比较复杂，还需要一些时间，感谢你的耐心等待。"
                    await self._send_json({"event": "tool_notice", "text": strip_voice_tags(progress_notice)})
                    await self._speak_text(progress_notice)
                # Clear TTS interrupt so tool execution proceeds even if the
                # user barge-in interrupted the notice. Only an explicit stop
                # command (_task_cancelled) prevents tool execution.
                self._interrupt.clear()
                # User cancelled during the tool notice — don't execute tools.
                if self._task_cancelled:
                    await self._send_json({"event": "assistant_text", "text": "（任务已取消）", "done": True})
                    self._history.append({"role": "assistant", "content": "（任务已取消）"})
                    return
                # append assistant tool-call message then execute tools
                messages.append({
                    "role": "assistant",
                    "content": reply_text or None,
                    "tool_calls": tool_calls,
                })
                # Mirror into _history so LATER turns can see the real tool
                # outputs (notebook names, search results...). Without this the
                # next turn's context only holds the assistant's summary and
                # the model confabulates the details it forgot.
                self._history.append({
                    "role": "assistant",
                    "content": reply_text or None,
                    "tool_calls": tool_calls,
                })
                _all_tool_calls.extend(tool_calls)
                self._tools_running = True
                from app.tools.registry import registry

                if self._workspace_path is None:
                    try:
                        from app.services.workspace_service import ensure_user_workspace

                        ws = await ensure_user_workspace(
                            self.db, self.user.id, self.user.username
                        )
                        self._workspace_path = ws.root_path
                    except Exception as exc:
                        logger.debug("voice workspace init failed: %s", exc)
                        self._workspace_path = ""

                for tc in tool_calls:
                    if self._task_cancelled:
                        break
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    # Voice mode: the user explicitly asked via voice, so
                    # auto-grant write permissions (e.g. notes create_note).
                    # There is no interactive permission dialog in voice UI.
                    args["_permission_granted"] = True
                    await self._send_json({"event": "tool_call", "name": name})
                    # Persist at DISPATCH time too: if the session dies while
                    # this tool is still running (WS close cancels the loop),
                    # the record still shows the tool was started.
                    await self._persist_turn_progress(
                        full_reply + reply_text,
                        json.dumps(_all_tool_calls, ensure_ascii=False),
                        _build_voice_tool_results(_all_tool_results) if _all_tool_results else None,
                    )
                    result = await registry.dispatch(
                        name, args, db=self.db, user=self.user,
                        conversation=self._conversation, assistant=None,
                        workspace_path=self._workspace_path or "",
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result[:6000],
                    })
                    self._history.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result[:1500],
                    })
                    _all_tool_results.append({
                        "tool_call_id": tc.get("id", ""),
                        "name": name,
                        "content": result[:6000],
                    })
                    await self._send_json({"event": "tool_result", "name": name})
                    # Incremental persistence: upsert the assistant message
                    # after EVERY tool result, so exiting voice mode mid-loop
                    # still leaves the full tool trace (incl. background_task
                    # submissions with their task_id) in the session record.
                    await self._persist_turn_progress(
                        full_reply + reply_text,
                        json.dumps(_all_tool_calls, ensure_ascii=False),
                        _build_voice_tool_results(_all_tool_results),
                    )
                full_reply += reply_text
                # Keep tools available for multi-step tasks (e.g. list_notebooks
                # then create_note). The max_tool_rounds bound prevents runaway
                # loops. Previously tools=None here forced the LLM to summarize
                # after a single round, making multi-step tasks impossible.
                continue

            full_reply += reply_text
            break

        # wait for this turn's audio to finish sending
        if full_reply.strip():
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            await self._tts_queue.put({"flush": fut})
            try:
                await asyncio.wait_for(fut, timeout=120.0)
            except Exception:
                pass
        if full_reply.strip():
            self._history.append({"role": "assistant", "content": full_reply})
            self._last_answer_text = full_reply
            self._vmem_answer_epoch = self._turn_epoch
            # Refresh the fun-asr-realtime dialogue context so later turns are
            # recognised against the live conversation. Keeps the ASR biased
            # toward the current topic and away from background speech.
            if self._asr:
                try:
                    await self._asr.update_context(self._history)
                except Exception as exc:
                    logger.debug("voice ASR context refresh failed: %s", exc)
            # Persist with tool_calls/tool_results so the session record
            # preserves the full tool interaction history.  Convert the
            # flat voice format into the rich ToolResultsData display format
            # (agent_steps etc.) so the frontend MessageBubble can render
            # collapsible tool-call blocks — same as Agent mode.
            _tc_json = json.dumps(_all_tool_calls, ensure_ascii=False) if _all_tool_calls else None
            _tr_json = _build_voice_tool_results(_all_tool_results) if _all_tool_results else None
            if self._turn_msg_id:
                # A provisional row exists from the tool loop — finalize it.
                await self._persist_turn_progress(full_reply, _tc_json, _tr_json, final=True)
            else:
                await self._persist_message("assistant", full_reply, tool_calls=_tc_json, tool_results=_tr_json)
            await self._send_json({"event": "assistant_text", "text": strip_voice_tags(full_reply), "done": True})

    async def _generate_and_speak_cancellable(self, text: str, prefetched: Optional[str] = None) -> None:
        """Generation entry-point used by the parallel subagent+main-agent
        pattern for short utterances. Identical to ``_generate_and_speak``
        but handles ``asyncio.CancelledError`` gracefully: when the noise-gate
        subagent cancels this task, partial history is rolled back and no
        incomplete message is persisted."""
        history_before = len(self._history)
        try:
            await self._generate_and_speak(text, allow_tools=True, prefetched=prefetched)
        except asyncio.CancelledError:
            # Noise gate cancelled this generation — roll back history and
            # notify the client so any partial display text is cleared. Also
            # cut any in-flight aux speech (filler prefix spoken at turn
            # start): the turn is abandoned, so "我来想想啊…" must not keep
            # playing into the void. `_interrupt` stays set until the next
            # _handle_user_turn clears it — the established interrupt pattern
            # (A4.9 I2).
            del self._history[history_before:]
            self._last_answer_text = ""
            self._vmem_answer_epoch = 0
            self._interrupt.set()
            self._drain_tts_queue()
            await self._mark_speaking_end()
            await self._send_json({"event": "generation_cancelled"})
            raise

    # ---- ASR pipeline / EoT watchdog ----
    async def _asr_pipeline(self) -> None:
        while not self._closed:
            ev = await self._asr_events.get()
            etype = ev.get("type")
            if etype == "ready":
                self._asr_ready = True
                self._last_asr_activity = _now()
                await self._set_state("listen")
            elif etype == "reconnecting":
                # Upstream dropped and is reconnecting — freeze endpointing so
                # the watchdog can't flush a mid-utterance turn on stale text.
                self._asr_ready = False
            elif etype == "activity":
                self._last_asr_activity = _now()
            elif etype in ("partial", "segment"):
                self._last_asr_activity = _now()
                full = ev.get("text", "")
                self._latest_full = full
                # Refresh the utterance-time proximity snapshot (A4.9 C1): the
                # evidence for the classifier must reflect the acoustic
                # condition while this utterance was heard, not the (possibly
                # stale-by-flush-time) live signal.
                self._prox_utterance = self._prox_is_near()
                # The accumulated ASR text is retroactively finalised (sentence
                # punctuation appended at sentence_end), so the naive offset
                # slice can begin with the previous sentence's terminator.
                # Strip leading punctuation — a real turn never starts with it.
                turn_text = _strip_lead_punct(full[self._consumed_offset:])
                # Aux-speech echo guard: the agent's own filler/backchannel
                # phrases heard back by the mic land at the TAIL of the
                # accumulation (e.g. "帮我查一下天气嗯"). Strip them from the
                # pending text so they never pollute the EoT judgment or
                # become a phantom user turn. Only strips a suffix of a
                # LONGER text — a standalone "嗯" is a real confirmation.
                if self._aux_echo_guard:
                    stripped = _strip_aux_echo(turn_text, self._aux_echo_guard)
                    if stripped != turn_text:
                        logger.debug("voice aux-echo stripped: %r -> %r", turn_text, stripped)
                        turn_text = stripped
                # Acoustic onset barge-in: the user started speaking during
                # playback — pause TTS IMMEDIATELY on the first partial (the
                # earliest text signal upstream provides), instead of waiting
                # for EoT + LLM classification (~2s). The barge-in classifier
                # (at EoT flush) then decides resume-from-breakpoint or switch.
                # Pre-filters mirror the barge-in path: echo of our own voice,
                # ASR-noise text, and the no-interrupt window / cooldown never
                # pause — they defer to the normal flush-time path.
                if (
                    self._speaking
                    and not self._playback_paused
                    and not self._interjecting
                    and (self._speaking_epoch == self._turn_epoch or self._playing_aux)
                    and self.config.voice_barge_in_onset_enabled
                    and self.config.voice_barge_in_enabled
                    and turn_text.strip()
                    and not _ASR_NOISE_RE.match(turn_text)
                    and not _is_likely_echo(turn_text, self._spoken_text_recent)
                    and self._prox_is_near()
                ):
                    # Do NOT pause on VERY SHORT utterances (≤2 chars, e.g.
                    # "对"/"嗯是"/"好"): these are overwhelmingly backchannels
                    # or mic false positives (the TTS echo or environment
                    # audio misrecognized as a syllable — the echo gate only
                    # catches transcripts that MATCH the spoken text, so a
                    # misrecognition slips through; observed live: the user
                    # reported "绝对没有插话" while the ASR produced "对" and
                    # paused the answer). Let them flow through the normal
                    # EoT+classify path instead: a backchannel never pauses
                    # the playback at all, and a genuinely growing utterance
                    # pauses on its next, longer partial. Real interrupts are
                    # almost always ≥3 chars and still pause immediately.
                    if len(_strip_lead_punct(turn_text).strip()) < self.config.voice_barge_in_onset_min_chars:
                        logger.debug(
                            "voice onset skipped (short utterance %r) — deferring to EoT+classify",
                            turn_text,
                        )
                    else:
                        _now_p = _now()
                        if not (
                            _now_p - self._speaking_started_at
                            < self.config.voice_barge_in_no_interrupt_seconds
                            or _now_p - self._last_barge_in_time
                            < self.config.voice_barge_in_cooldown_seconds
                        ):
                            await self._pause_playback()
                            # Launch the barge-in classify in PARALLEL with the EoT
                            # watchdog, on the CURRENT partial. Serial EoT (probe
                            # 1.0s + judge 0.8s + flush) THEN classify (~1-1.5s)
                            # left 4-5s of dead silence after the pause — users
                            # gave up and refreshed, permanently truncating the
                            # answer (observed 15:33: pause 36.8s, EoT result
                            # 40.1s, connection closed before the resume). The
                            # flush reuses this verdict when the final text
                            # matches the classified partial (see
                            # _on_user_turn_locked), so a short reaction
                            # ("对"/"嗯") resumes as soon as the EoT flushes.
                            self._pre_classify = {
                                "text": turn_text,
                                "action": None,
                                "task": asyncio.create_task(
                                    self._run_pre_classify(turn_text)
                                ),
                            }
                if turn_text != self._pending_turn_text:
                    self._pending_turn_text = turn_text
                    self._last_text_change = _now()
                    self._complete_since = (
                        _now() if _utterance_complete(turn_text) else 0.0
                    )
                    await self._send_json({"event": "asr_partial", "text": turn_text})
                    if self._speaking and turn_text.strip():
                        await self._set_state("dual")
                # Interjection check: when a sentence completes (segment event)
                # and the agent is NOT speaking, run the interjection subagent
                # to decide whether to make a brief remark. This implements the
                # "插话" mechanism — the agent reacts while the user is still
                # speaking, without waiting for the full turn to finish.
                if (
                    etype == "segment"
                    and not self._speaking
                    and not self._playback_paused
                    and not self._interjecting
                    and not self._turn_active
                    and self.config.voice_interjection_enabled
                ):
                    await self._check_interjection(full)
            elif etype == "error":
                await self._send_json({"event": "error", "error": ev.get("error", "语音识别错误")})

    async def _check_interjection(self, full_text: str) -> None:
        """Extract the latest completed sentence and run the interjection
        subagent. Non-blocking: fires the check as a background task so ASR
        pipeline processing is not delayed."""
        if self._interjection_checking:
            return
        if _now() - self._last_interjection_time < self.config.voice_interjection_cooldown_seconds:
            # Still update the offset so we don't reprocess old text later
            self._interjection_offset = len(full_text)
            return
        if self._interjection_count_this_turn >= self.config.voice_interjection_max_per_turn:
            self._interjection_offset = len(full_text)
            return
        # Extract new text since last interjection check
        new_text = _strip_lead_punct(full_text[self._interjection_offset:]).strip()
        self._interjection_offset = len(full_text)
        if not new_text or len(new_text) < 4:
            return
        # Split by terminal punctuation — take the last completed sentence
        sentences = [s.strip() for s in re.split(f"[{_TERMINAL_PUNCT}]", new_text) if s.strip()]
        if not sentences:
            return
        latest_sentence = sentences[-1]
        if len(latest_sentence) < 4:
            return
        self._user_speech_sentences.append(latest_sentence)
        self._interjection_checking = True
        asyncio.create_task(self._run_interjection_check(latest_sentence))

    async def _run_interjection_check(self, sentence: str) -> None:
        """Run the interjection subagent and speak the interjection if approved."""
        self._interjection_classify_start = _now()
        try:
            result = await self._classify_interjection(
                sentence, list(self._user_speech_sentences)
            )
            if result["should_interject"] and not self._speaking and not self._closed:
                # Double-check conditions before speaking — the user might have
                # stopped speaking (EoT) while the subagent was running.
                if not self._turn_active and not self._interjecting:
                    await self._speak_interjection(
                        result["interjection_text"], result["emotion"]
                    )
                else:
                    # Update emotion even if we don't interject
                    self._emotion = result["emotion"]
            else:
                self._emotion = result["emotion"]
        except Exception as exc:
            logger.debug("voice interjection check error: %s", exc)
        finally:
            self._interjection_checking = False

    async def _eot_watchdog(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.1)
            # Safety fallback: if the client's playback_drained never arrives
            # (raw/headless client, network drop), clear _speaking once the
            # estimated playback window elapses so barge-in state can't stick.
            # Never while the consumer is inside a play loop OR the turn is
            # still generating: the deadline is only refreshed on flush items
            # (the _speak_text path), so a stale deadline would otherwise
            # clear _speaking mid-burst (a provider stall >2s between chunks
            # included) and the resulting mid-turn speaking_end/start pair
            # restarts the client's burst clock — under-counting the next
            # pause breakpoint (observed live conv 964462a8: the
            # audible-replay failure family).
            if (
                self._speaking
                and self._speaking_deadline
                and not self._consumer_playing
                and not self._turn_active
                and _now() > self._speaking_deadline
            ):
                await self._mark_speaking_end()
            text = self._pending_turn_text
            if not text.strip():
                continue
            # Never endpoint while the recognizer is down/reconnecting — frozen
            # text would otherwise look like "user stopped speaking" and flush
            # a mid-utterance turn.
            if not self._asr_ready:
                continue
            # An in-flight interjection check does NOT block the flush: the
            # check is a best-effort "插话" for a sentence the user may or may
            # not be continuing. Waiting for it (a non-streaming LLM call,
            # 1-2s typical, up to the subagent timeout) puts it on the
            # critical path to the user's answer. The check itself already
            # double-guards before speaking (not _turn_active / not
            # _interjecting), so a flush that wins the race simply skips the
            # interjection — the full answer starts immediately.
            # Endpoint idleness is measured from the last RECOGNIZER activity
            # (any upstream result), not the last text change. FunASR's partial
            # cadence during continuous speech is ~0.3-1.1s and can briefly
            # repeat identical text, so text-change time alone flushes turns
            # while the user is still talking.
            silence = _now() - max(self._last_text_change, self._last_asr_activity)
            # Backchannel (应和): a short listening ack during a mid-utterance
            # pause window — before the semantic-EoT probe can arm, so the
            # turn-taking pipeline is never racing an in-flight aux phrase.
            await self._maybe_backchannel(silence, text)
            # Filler prefetch (填充词预取): the utterance MAY end at the
            # earliest flush threshold — start the filler's TTS synthesis
            # even earlier (half of min(semantic_probe, complete_grace),
            # floored at 0.2s) so its first audio chunk is ready BEFORE the
            # flush: the filler overlaps the flush+generation pipeline and
            # starts playing right after ASR end (measured E2E: filler PCM
            # ~0.5s after the filler event, 10s+ before the answer finishes).
            # ARM ONCE PER LIVE FILLER — a mid-utterance pause never re-arms:
            # the phrase is content-independent (picked from the previous
            # PHRASE, not the user text), and a re-arm on every FunASR partial
            # (0.3-1.1s cadence) would burn one canceled provider synthesis
            # per partial of a long monologue (2026-08-25 review I1). The
            # synthesis simply stays queued; a convert whose text no longer
            # matches is re-gated (noise/stop-word) on the FLUSHED text.
            arm_threshold = max(
                0.2,
                0.5
                * min(
                    self.config.voice_eot_semantic_probe_seconds,
                    self.config.voice_eot_complete_grace_seconds,
                ),
            )
            if silence >= arm_threshold and self._filler_prefetch.get("q") is None:
                self._arm_filler_prefetch(text)
            # Adaptive endpointing. A complete-looking utterance (ends with
            # sentence-final punctuation) flushes after a short grace of no ASR
            # activity — when the user continues into the next sentence FunASR
            # emits its first partial within ~0.5-1.3s of the sentence_end, so
            # the grace distinguishes a real stop from a sentence boundary.
            # An incomplete utterance (mid-sentence pause, no terminal
            # punctuation) waits much longer so a long question spoken with
            # natural pauses is not chopped into fragments.
            # NOTE: the same thresholds apply while the agent is speaking.
            # A previous special-case (0.6s during playback) truncated barge-in
            # speech mid-utterance — FunASR's partial cadence (0.5-1.3s) beat
            # the threshold whenever the user talked over the TTS answer,
            # shattering one utterance into a cascade of fragment turns.
            # Barge-in responsiveness comes from the downstream classifier,
            # not from aggressive endpointing.
            if _utterance_complete(text):
                # Flush when the recognizer has been idle for the grace period
                # — OR when the text has sat complete for the hard cap even
                # though ASR activity continues (noisy environment: background
                # speech must not postpone the answer indefinitely). The cap
                # timer resets the moment new text arrives, so a user who
                # genuinely keeps talking is never truncated by it.
                idle_flush = silence >= self.config.voice_eot_complete_grace_seconds
                capped_flush = (
                    self._complete_since
                    and (_now() - self._complete_since)
                    >= self.config.voice_eot_complete_max_seconds
                )
                if not (idle_flush or capped_flush):
                    continue
            else:
                # Semantic EoT: unpunctuated text is ambiguous — the user may
                # have finished ("好的就这样" often carries no period) or be
                # mid-thought ("我想说…"). Once silence passes the probe
                # threshold, an LLM judge (fire-and-forget, bounded) decides
                # semantic completeness: "complete" flushes EARLY (faster than
                # the hard threshold); "incomplete" or a failed/timeout check
                # waits for the hard threshold below (fail-open — never more
                # dead time than the pre-semantic behavior). A text change
                # while the check is in flight invalidates its verdict.
                # While playback is PAUSED (onset barge-in) the probe is
                # shortened: the user's reaction is the only speech, so the
                # endpoint can be faster without fragment risk — the flush is
                # still gated by the semantic judge's verdict.
                probe_sec = self.config.voice_eot_semantic_probe_seconds
                if self._playback_paused:
                    probe_sec = min(probe_sec, self.config.voice_eot_paused_probe_seconds)
                if (
                    self.config.voice_eot_semantic_enabled
                    and not self._eot_semantic_checking
                    and silence >= probe_sec
                    and self._eot_semantic_checked_text != text.strip()
                ):
                    self._eot_semantic_checking = True
                    self._eot_semantic_complete = False
                    self._eot_semantic_checked_text = text.strip()
                    asyncio.create_task(self._run_eot_semantic_check(text))
                    logger.info("voice semantic EoT probe fired: %r", text[-60:])
                semantic_ok = (
                    self._eot_semantic_complete
                    and text.strip() == self._eot_semantic_checked_text
                )
                if not semantic_ok:
                    if silence < self.config.voice_eot_silence_incomplete_seconds:
                        continue
                elif silence < probe_sec:
                    # The user resumed talking (identical re-emitted text reset
                    # the silence clock) while the judge was in flight — do NOT
                    # flush mid-speech; wait for the hard threshold (I4).
                    continue
                if semantic_ok:
                    _perf("eot_semantic_flush",
                          (_now() - max(self._last_text_change, self._last_asr_activity)) * 1000,
                          text_len=len(text))
            text = text.strip()
            self._pending_turn_text = ""
            self._complete_since = 0.0
            self._eot_semantic_complete = False
            self._eot_semantic_checked_text = ""
            # Per-turn backchannel budget + aux-echo guards reset at the turn
            # boundary (the echo of the previous turn's filler is consumed).
            self._backchannel_count_this_turn = 0
            self._aux_echo_guard = []
            self._consumed_offset = len(self._latest_full)
            _perf("eot_flush", (_now() - max(self._last_text_change, self._last_asr_activity)) * 1000,
                  text_len=len(text), utter_complete=_utterance_complete(text))
            # Keep interjection offset in sync with consumed offset so the next
            # turn's sentence extraction starts from the right position.
            self._interjection_offset = len(self._latest_full)
            # Snapshot interjection data for the upcoming turn's LLM context,
            # then reset for the next speech. The snapshot lets _system_messages
            # inject "you already said X during their speech" so the full answer
            # is coherent and doesn't repeat interjection content.
            self._interjection_snapshot = list(self._turn_interjections)
            self._reset_interjection_state()
            if self._speaking:
                # during speech, route through barge-in logic
                asyncio.create_task(self._on_user_turn(text))
            else:
                await self._send_json({"event": "asr_segment", "text": text})
                if (self._turn_active or self._playback_paused) and not self._closed:
                    # Think phase (generation in flight, no audio yet) or
                    # PLAYBACK PAUSED (audio stopped on speech onset, generation
                    # may already be done): route through the preemption /
                    # pause-resolution judgment in _on_user_turn so a clear
                    # interrupt cuts the in-flight answer (or switches the
                    # paused turn) instead of making the user wait for the
                    # whole answer to stream and play out — and a backchannel
                    # resumes the paused playback from its breakpoint.
                    asyncio.create_task(self._on_user_turn(text))
                else:
                    # Idle flush: convert the speculative filler synthesis
                    # FIRST (before enqueueing the turn) — the phrase enters
                    # the TTS queue as a pre-synthesized aux item here, so it
                    # plays ~immediately after ASR end and covers the
                    # generation+TTS window. `_handle_user_turn`'s fallback
                    # stays cooldown-suppressed behind this (2026-08-25).
                    await self._convert_filler_prefetch(text)
                    self._enqueue_turn(text)

    # ---- lifecycle ----
    async def run(self) -> None:
        # Ensure a conversation exists and announce the session FIRST, then
        # send `ready` IMMEDIATELY. `_load_identity` below can block for tens
        # of seconds (the shared-memory retrieval runs an AWAITED query-
        # expansion LLM call on the user's custom provider — user custom provider —
        # which is slow or unreachable; observed 15:12: sessions hung there
        # and the client timed out at 12s: "连接语音服务超时", completely
        # unusable). The client starts mic capture on `ready`; audio buffers
        # in `_audio_q` until the ASR upstream connects, so nothing is lost.
        try:
            await self._ensure_conversation()
        except Exception as exc:
            logger.debug("voice conversation setup failed (non-fatal): %s", exc)

        await self._send_json({"event": "ready", "tts": self.tts.is_configured()})

        # Load Agent-mode assistant identity + shared memory so 语音助理 mode
        # inherits the same persona, name, and long-term memory. Runs AFTER
        # `ready` so a slow/unreachable memory provider never blocks the
        # session start; the first answer still waits for it (or its bounded
        # fallback).
        try:
            await self._load_identity()
        except Exception as exc:
            logger.debug("voice identity load failed (non-fatal): %s", exc)

        try:
            await self._load_history()
        except Exception as exc:
            logger.debug("voice conversation setup failed (non-fatal): %s", exc)

        asr_service = ASRService()
        if not asr_service.enabled:
            await self._send_json({"event": "error", "error": "语音识别服务未配置"})
            return

        hotwords = []
        vocabulary_id: Optional[str] = None
        try:
            from sqlalchemy import select
            from app.db.database import UserAsrHotword

            res = await self.db.execute(
                select(UserAsrHotword).where(UserAsrHotword.user_id == self.user.id)
            )
            items = res.scalars().all()
            hotwords = [{"text": i.text, "weight": i.weight, "lang": i.lang} for i in items]
            for item in items:
                if item.dashscope_vocabulary_id:
                    vocabulary_id = item.dashscope_vocabulary_id
                    break
        except Exception:
            hotwords = []
            vocabulary_id = None

        try:
            self._asr = _VoiceASR(
                asr_service,
                self._asr_events,
                hotwords,
                vocabulary_id,
                context=list(self._history),
            )
            await self._asr.start()
        except Exception as exc:
            logger.error("voice ASR start failed: %s", exc)
            await self._send_json({"event": "error", "error": f"无法启动语音识别: {exc}"})
            return

        # Register so out-of-band events (background task completion) can
        # find this session and have the assistant proactively announce them.
        _ACTIVE_VOICE_SESSIONS[self.user.id] = self

        self._tasks = [
            asyncio.create_task(self._asr_pipeline()),
            asyncio.create_task(self._eot_watchdog()),
            asyncio.create_task(self._tts_consumer()),
            asyncio.create_task(self._responder()),
        ]

        try:
            await self._receive_loop()
        finally:
            await self._shutdown()

    async def _receive_loop(self) -> None:
        while not self._closed:
            try:
                message = await self.websocket.receive()
            except Exception:
                break
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                break
            data = message.get("bytes")
            text = message.get("text")
            if data is not None:
                if self._asr:
                    pcm16 = await asyncio.to_thread(ASRService._float32_to_pcm16, data)
                    if pcm16:
                        await self._asr.send_audio(pcm16)
            elif text is not None:
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                await self._handle_client_event(msg)

    async def _handle_client_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "text":
            content = (msg.get("text") or "").strip()
            if content:
                await self._on_user_turn(content)
        elif event == "playback_progress":
            # Client-authoritative playback position. Ties the spoken audio to
            # the answer text so truncation is attributed to playback, not text.
            try:
                self._playback_played_sec = float(msg.get("played_sec", 0) or 0)
                total = float(msg.get("total_sec", 0) or 0)
                if total > 0:
                    self._playback_total_sec = total
            except (TypeError, ValueError):
                pass
        elif event == "playback_drained":
            # Client finished playing every scheduled chunk — the audible tail
            # is over, so speaking is truly done (clears barge-in state).
            await self._mark_speaking_end()
        elif event == "audio_proximity":
            # Browser-side acoustic near-field signal: the client classifies
            # its own mic input as near-field (user close to the mic) vs
            # far-field (environment speech). Far-field speech must never
            # pause playback or interrupt the current answer. Only a literal
            # boolean counts — anything else keeps the previous state.
            near = msg.get("near")
            if isinstance(near, bool):
                self._prox_seen = True
                self._prox_near = near
                self._prox_updated = _now()
        elif event == "interrupt":
            await self._request_interrupt()
        elif event == "_test_inject_asr":
            # Test-only: inject ASR text to test interjection without real
            # audio. Simulates FunASR emitting partial/segment events.
            inj_text = msg.get("text", "")
            is_segment = bool(msg.get("segment", False))
            if is_segment:
                # Segment (sentence_end): emit with the current accumulated text
                await self._asr_events.put({"type": "segment", "text": self._latest_full})
            elif inj_text:
                # Partial: accumulate text and emit
                full = (self._latest_full or "") + inj_text
                self._latest_full = full
                await self._asr_events.put({"type": "partial", "text": full})
        elif event == "stop":
            self._closed = True

    async def _shutdown(self) -> None:
        self._closed = True
        if _ACTIVE_VOICE_SESSIONS.get(self.user.id) is self:
            _ACTIVE_VOICE_SESSIONS.pop(self.user.id, None)
        self._interrupt.set()
        self._drop_filler_prefetch()
        # stop queues
        try:
            self._turn_queue.put_nowait(None)
        except Exception:
            pass
        try:
            self._tts_queue.put_nowait(None)
        except Exception:
            pass
        if self._prefetch.get("task") and not self._prefetch["task"].done():
            self._prefetch["task"].cancel()
        for t in self._vmem_recall_inflight:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._asr:
            await self._asr.close()
