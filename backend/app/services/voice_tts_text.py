# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice TTS text helpers (P5.5 move-only, 2026-10-18)."""
from __future__ import annotations

import re

from app.services.voice_text import _normalize_style_tag, strip_pseudo_tool_tags


_MD_LINK_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")

_TTS_SOUND_TAG_RE = re.compile(
    r"笑|叹气|叹了口气|叹了一口气|长叹|叹|喘息|喘|吸气|呼气|呼吸|哽咽|抽泣|"
    r"呜咽|哭|嚎啕|哼|啊|嗯|哎|哇|咦|呵|嘿|嘻|嘟囔|碎碎念|沉默|停顿|破音|"
    r"颤抖|气声|鼻音|变调|屏息|深呼吸"
)

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
    # DSML 从严：一旦出现，其后整段截断不朗读（落库/历史面按块剥离保留
    # 尾随正文；朗读面宁可少读也不念 markup——参数值文本含标点会被切进
    # 独立分段，逐段剥离后仍会漏读内容文本）。必须先于通用剥离：块剥离
    # 会保留 DSML 之后的正文，令此处 find 落空。
    _dsml_cut = text.find("<｜｜DSML")
    if _dsml_cut != -1:
        text = text[:_dsml_cut]
    # 伪工具标记绝不入耳（2026-11-07 语音事故）：完整标签与流式尾部半截
    # 开标签都在此丢弃，先于风格/音频标签处理。
    text = strip_pseudo_tool_tags(text)
    if not text:
        return ""

    def _paren_repl(m: re.Match) -> str:
        return _normalize_style_tag(m.group(1))

    text = re.sub(r"[(（]([^)）]{0,24})[)）]", _paren_repl, text)

    def _bracket_repl(m: re.Match) -> str:
        return "" if _TTS_SOUND_TAG_RE.search(m.group(1)) else m.group(0)

    text = re.sub(r"\[([^\]]{0,24})\]", _bracket_repl, text)

    return re.sub(r"[ \t]{2,}", " ", text).strip()

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
