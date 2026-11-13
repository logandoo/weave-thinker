# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Memory-state claim guard (TODO 2026-11-08 A5).

The 2026-11-07 voice incident showed the model can say "已失效/补录好了"
without any tool result. This module is a deterministic exit filter:

- **read** claims (我记得/记忆里有) need a memory/session search result OR
  this turn's injected memory context;
- **write** claims (已记住/已补录/已删除/已重新标记) need a real memory write
  tool result (memory add/replace/remove) — passive injection never certifies
  a write.

Unsupported sentences are removed before persistence/TTS, with an audit issue
list. It is intentionally conservative and does not call an LLM.
"""
from __future__ import annotations

import json
import re

WRITE_CLAIM_RE = re.compile(
    r"(?:已|已经|帮你|给你|我(?:已经)?)\s*(?:记住|记下|记好了?)"
    r"|(?:已|已经|帮你|给你|我(?:已经)?)\s*补录(?:好|了|完成)?"
    r"|补录(?:好|了|完成)"
    r"|(?:已|已经)\s*(?:删除|遗忘|标记|更新|修改|失效)(?:了|掉|好)?"
    r"\s*(?:这条|该条|相关)?\s*(?:记忆|概念|记录)"
    r"|(?:标记|重新标记)为(?:已)?(?:有效|失效)"
    r"|(?:保存|写入|记录)到记忆"
    r"|记忆(?:已|已经)(?:保存|更新|删除|失效)"
)
READ_CLAIM_RE = re.compile(
    r"我(还)?记得|我(并)?不记得|记忆(里|中)(有|没有|显示)|"
    r"我的记忆(里|中)|根据(我的)?记忆"
)
# P1-12（2026-11-08）：`标记为有效/失效` 单独命中时必须句内有记忆对象——
# 普通语句（“把这段条款标记为失效”）不是记忆写入声明。
_MARK_ONLY_RE = re.compile(r"(?:标记|重新标记)为(?:已)?(?:有效|失效)")
_MEMORY_OBJECT_RE = re.compile(r"记忆|概念|记录|条目|笔记")
_SEARCH_TOOLS = ("memory_search", "session_search", "memory")
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?", re.UNICODE)


def _write_claim_hit(sentence: str) -> bool:
    m = WRITE_CLAIM_RE.search(sentence)
    if not m:
        return False
    if _MARK_ONLY_RE.fullmatch(m.group(0)) and not _MEMORY_OBJECT_RE.search(sentence):
        return False
    return True


def is_write_claim(text: str) -> bool:
    return any(_write_claim_hit(s) for s in _SENTENCE_RE.findall(str(text or "")))


def _parse_entries(raw) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        # Chat persistence payload: {rounds, results, agent_steps, ...}
        out = []
        for key in ("agent_steps", "results", "tool_calls", "rounds"):
            vals = data.get(key)
            if isinstance(vals, list):
                out.extend(x for x in vals if isinstance(x, dict))
        return out
    return []


def _entry_name_args(entry: dict) -> tuple[str, dict]:
    fn = entry.get("function") if isinstance(entry.get("function"), dict) else {}
    name = str(entry.get("name") or entry.get("tool_name") or fn.get("name") or "")
    args = entry.get("args") or entry.get("arguments") or fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            args = {}
    return name, args if isinstance(args, dict) else {}


def _call_id(entry: dict) -> str:
    return str(entry.get("call_id") or entry.get("tool_call_id") or entry.get("id") or "")


def _is_error_entry(entry: dict) -> bool:
    if entry.get("error"):
        return True
    content = entry.get("result") or entry.get("content") or ""
    if isinstance(content, str):
        # P1-12：写入被安全层拒绝（[BLOCKED: ...]）= 失败证据，即使无 error 字段。
        if "[BLOCKED" in content:
            return True
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return False
        return isinstance(parsed, dict) and bool(parsed.get("error"))
    return False


def has_memory_write_result(tool_results, tool_calls=None) -> bool:
    """True only for a successful memory add/replace/remove call/result."""
    results = _parse_entries(tool_results)
    calls = _parse_entries(tool_calls)
    error_ids = {_call_id(r) for r in results if _call_id(r) and _is_error_entry(r)}
    result_ids = {_call_id(r) for r in results if _call_id(r)}
    any_error = any(_is_error_entry(r) for r in results)
    for call in calls:
        name, args = _entry_name_args(call)
        if name != "memory" or str((args or {}).get("action") or "").lower() not in (
                "add", "replace", "remove"):
            continue
        cid = _call_id(call)
        if cid and cid in error_ids:
            continue
        if not cid:
            # P1-12：id-less 调用仅在「无任何 error 且结果集为空（裸 call 流）」时
            # 放行；结果集非空却无可关联证据 ≠ 成功证明（旧实现此处直接放行=漏放行）。
            if any_error or results:
                continue
            return True
        if results and cid and cid not in result_ids:
            # No correlatable ids (chat agent_steps name=call_id): accept only
            # when the result set carries no error evidence at all.
            if result_ids:
                continue
            if any_error:
                continue
            return True
        return True
    for result in results:
        name, args = _entry_name_args(result)
        if name == "memory" and str((args or {}).get("action") or "").lower() in (
                "add", "replace", "remove") and not _is_error_entry(result):
            return True
    return False


def _has_search_result(tool_results_json: str, tool_calls=None) -> bool:
    entries = _parse_entries(tool_results_json) + _parse_entries(tool_calls)
    if entries:
        return any(_entry_name_args(e)[0] in _SEARCH_TOOLS for e in entries)
    return any(name in str(tool_results_json) for name in _SEARCH_TOOLS)


def sanitize_claim_text(
    text: str, tool_results_json: str = "", injected_context: str = "",
    tool_calls=None,
) -> tuple[str, list[str]]:
    """Remove unsupported memory-state sentences; preserve every other byte."""
    raw = str(text or "")
    if not raw.strip():
        return raw, []
    write_ok = has_memory_write_result(tool_results_json, tool_calls)
    read_ok = bool(injected_context and injected_context.strip()) or _has_search_result(
        tool_results_json, tool_calls)
    issues: list[str] = []
    out: list[str] = []
    last = 0
    for match in _SENTENCE_RE.finditer(raw):
        # Preserve the gap (including newlines) before this sentence.
        out.append(raw[last:match.start()])
        sentence = match.group(0)
        if _write_claim_hit(sentence):
            if not write_ok:
                issues.append("unsupported_write_claim")
                last = match.end()
                continue
        elif READ_CLAIM_RE.search(sentence):
            if not read_ok:
                issues.append("unsupported_read_claim")
                last = match.end()
                continue
        out.append(sentence)
        last = match.end()
    out.append(raw[last:])
    cleaned = "".join(out).strip()
    if not cleaned:
        cleaned = ""
    return cleaned, issues

