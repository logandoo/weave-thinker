# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice text/turn-taking helpers (P5.5 move-only, 2026-10-18)."""
from __future__ import annotations

import re
from typing import Optional


_SEGMENT_PUNCT = "，。！？；、…,.!?;~\n"

def _norm_barge_compare(text: str) -> str:
    """Normalize a barge-in utterance for comparing the onset-pause partial
    with the EoT-flushed final text (strip leading/trailing punctuation and
    whitespace; "嗯是。" == "嗯是")."""
    return _strip_lead_punct(text).strip().strip(_SEGMENT_PUNCT)

_TERMINAL_PUNCT = "。！？!?…～~"

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

_LEAD_PUNCT = "，。！？；、…—～~,.!?;:、\"'“”‘’()（）【】[]· \t\n\r"

def _strip_lead_punct(text: str) -> str:
    """Strip leading punctuation/whitespace — a real user utterance never
    begins with punctuation; any such prefix is an ASR segmentation artifact
    (the previous sentence's terminator bleeding into the next slice)."""
    return (text or "").lstrip(_LEAD_PUNCT)

def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text or "")

def _utterance_complete(text: str) -> bool:
    """True when the accumulated ASR text ends with sentence-final punctuation,
    i.e. it looks like a finished utterance rather than a mid-sentence pause."""
    t = (text or "").rstrip()
    return bool(t) and t[-1] in _TERMINAL_PUNCT

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

_TTS_STYLE_SORTED = sorted(_TTS_STYLE_WHITELIST, key=len, reverse=True)

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
