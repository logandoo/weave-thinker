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

# 伪工具标记（2026-11-07 语音事故 conv 73875edd）：模型不走原生 function-calling、
# 把工具调用当正文吐出来（"<tool_call>memory</tool_call>"，无参数）。语音链路一旦
# 把它播出去/存进历史，模型会在后续轮次模仿自己的伪格式并谎称已完成——自增强污染
# 环。chat 链路在 agent_loop.py:58-61 有同款防御，语音侧在此对齐。
PSEUDO_TOOL_TAGS_RE = re.compile(
    r"<tool_calls?>.*?</tool_calls?>"
    r"|<tool_result>.*?</tool_result>"
    r"|<invoke[^>]*>.*?</invoke>",
    re.DOTALL,
)
# DeepSeek DSML 内联工具标记（2026-11-07 第二形态泄漏：memory-add 轮整段吐
# "<｜｜DSML｜｜ calls>…"）。注意生产实测存在 `<｜｜DSML｜｜ invoke`（｜｜后带空格）
# 与 `calls`（非 tool_calls）变体，正则一律容忍。
_DSML_BLOCK_RE = re.compile(
    r"<｜｜DSML｜｜\s*(?:tool_)?calls>.*?</｜｜DSML｜｜\s*(?:tool_)?calls>", re.DOTALL
)
_DSML_ANY_TAG_RE = re.compile(r"</?｜｜DSML｜｜[^>]*>")
_DSML_INVOKE_RE = re.compile(
    r"<｜｜DSML｜｜\s*invoke\s+name\s*=\s*\"([^\"]+)\"[^>]*>(.*?)</｜｜DSML｜｜\s*invoke>",
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    r"<｜｜DSML｜｜\s*parameter\s+name\s*=\s*\"([^\"]+)\"(?:\s+string\s*=\s*\"([^\"]*)\")?\s*>"
    r"(.*?)</｜｜DSML｜｜\s*parameter>",
    re.DOTALL,
)
# 流式尾部的半截开标签（"<tool" / "<tool_cal" …）：朗读/落库前一律丢弃，
# 绝不把残缺标记留给 TTS 或历史。只匹配「<[/] + 伪标签名的严格前缀 + 串尾」，
# 普通尖括号文本（"a <- b"、"5 < 6"）不受影响。
def _partial_alt(names: tuple) -> str:
    alts = {re.escape(n[:i]) for n in names for i in range(len(n) + 1)}
    return "|".join(sorted(alts, key=len, reverse=True))


_PSEUDO_TOOL_PARTIAL_RE = re.compile(
    r"</?(?:" + _partial_alt(("tool_call", "tool_calls", "tool_result", "invoke")) + r")$"
)
# 流式尾部的半截 DSML 标记（"<｜" / "<｜｜DS" …）同样丢弃
_DSML_PARTIAL_TAIL_RE = re.compile(
    "<(?:" + _partial_alt(("｜｜DSML｜｜",)) + ")$"
)


def strip_pseudo_tool_tags(text: str) -> str:
    """Remove pseudo tool-call markup (``<tool_call>`` family + DeepSeek DSML
    blocks + a trailing partial open-tag) from voice output. Returns the text
    unchanged when no marker is present (identity fast path keeps callers
    cheap to call per chunk)."""
    if not text or "<" not in text:
        return text
    cleaned = PSEUDO_TOOL_TAGS_RE.sub("", text)
    if "<｜｜DSML" in cleaned:
        cleaned = _DSML_BLOCK_RE.sub("", cleaned)
        cleaned = _DSML_ANY_TAG_RE.sub("", cleaned)
        # 未闭合的 DSML 残段：从首个标记起点整段截断（其后必然全是 markup）
        cut = cleaned.find("<｜｜DSML")
        if cut != -1:
            cleaned = cleaned[:cut]
    cleaned = _PSEUDO_TOOL_PARTIAL_RE.sub("", cleaned)
    cleaned = _DSML_PARTIAL_TAIL_RE.sub("", cleaned)
    if cleaned != text:
        # 剥离后收拾标签留下的悬空空行/行尾空白（正文本身不做折叠——
        # 折叠是 strip_voice_tags 的职责）。
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()
    return cleaned


def extract_dsml_pseudo_tool_calls(content: str) -> tuple[str, list[dict]]:
    """把 DSML 内联工具标记解析成 OpenAI 风格 tool_calls（语音侧对齐
    agent_loop._extract_dsml_tool_calls，但容忍生产实测的带空格/`calls`
    变体）。返回 (剥离后正文, calls)；calls 为空时正文仍已剥离。"""
    if not content or "<｜｜DSML" not in content:
        return content, []
    calls: list[dict] = []
    seen: set[tuple[str, str]] = set()
    import json as _json

    for invoke_match in _DSML_INVOKE_RE.finditer(content):
        tool_name = invoke_match.group(1).strip()
        args: dict = {}
        for param_match in _DSML_PARAMETER_RE.finditer(invoke_match.group(2)):
            param_name = param_match.group(1).strip()
            string_attr = (param_match.group(2) or "").strip().lower()
            param_value = param_match.group(3)
            if string_attr == "false":
                try:
                    args[param_name] = _json.loads(param_value)
                except (ValueError, TypeError):
                    args[param_name] = param_value
            else:
                args[param_name] = param_value
        arguments_json = _json.dumps(args, ensure_ascii=False)
        sig = (tool_name, arguments_json)
        if sig in seen:
            continue
        seen.add(sig)
        calls.append({
            "id": f"voice_dsml_{len(calls)}",
            "type": "function",
            "function": {"name": tool_name, "arguments": arguments_json},
        })
    cleaned = _DSML_BLOCK_RE.sub("", content)
    cleaned = _DSML_ANY_TAG_RE.sub("", cleaned)
    cut = cleaned.find("<｜｜DSML")
    if cut != -1:
        cleaned = cleaned[:cut]
    return cleaned.strip(), calls

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
