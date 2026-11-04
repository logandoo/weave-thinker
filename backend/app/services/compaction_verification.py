# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P4: deterministic verification of a compaction summary.

Compaction is a lossy rewrite.  Before the summary replaces the middle of the
history, extract checkable **key facts** from the compressed span and verify
they survived.  When recall is below the configured floor, append the missing
facts verbatim (bounded) instead of silently dropping them.

This is a deterministic oracle (no LLM): numbers, paths, identifiers and
constraint sentences.  It cannot prove semantic fidelity — it proves that
specific high-value tokens were not lost.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

_MAX_FACT_CHARS = 180
_MAX_REPAIR_CHARS = 2400

_NUMBER = re.compile(r"(?<![\w.])(\d{4,}|\d+\.\d+)(?![\w.])")
# Bounded repetition: an unbounded `[\w./\-]+` backtracks O(n^2) on a long
# run with no extension (a 3M-char tool dump).  240 chars is enough for a path.
_PATH = re.compile(
    r"(?<![\w./\\-])[\w./\\-]{1,240}\.(?:py|js|ts|tsx|jsx|vue|md|txt|json|toml|"
    r"ya?ml|csv|pdf|docx|xlsx|pptx|html|css|sh|sql|png|jpe?g|zip|log)\b"
)
_IDENTIFIER_TOKEN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{5,}\b")
_CAMEL = re.compile(r"[a-z][A-Z]")
_CONSTANT = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b")
_QUOTED = re.compile(r"[“\"']([^“”\"'\n]{8,120})[”\"']")
_CONSTRAINT = re.compile(
    r"(?i)(?:必须|不得|不要|只能|禁止|务必|一定要|do\s+not|don't|must\s+(?:not\s+)?|"
    r"never\s+|only\s+|required\s+to)"
)


def _text_of(msg: Any) -> str:
    if isinstance(msg, dict):
        parts = [str(msg.get("content") or "")]
        if msg.get("reasoning_content"):
            parts.append(str(msg["reasoning_content"]))
        results = msg.get("tool_results")
        if results:
            try:
                parts.append(
                    results if isinstance(results, str)
                    else json.dumps(results, ensure_ascii=False)
                )
            except (TypeError, ValueError):
                parts.append(str(results))
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict):
                parts.append(str((call.get("function") or {}).get("arguments") or ""))
        return "\n".join(parts)
    parts = [str(getattr(msg, "content", "") or "")]
    if getattr(msg, "reasoning_content", None):
        parts.append(str(msg.reasoning_content))
    results = getattr(msg, "tool_results", None)
    if results:
        parts.append(results if isinstance(results, str) else str(results))
    return "\n".join(parts)


def _constraint_sentences(text: str) -> list[str]:
    out: list[str] = []
    for sentence in re.split(r"(?<=[。.!?！？\n])\s*", text):
        sentence = re.sub(r"\s+", " ", sentence).strip()
        if 8 <= len(sentence) <= _MAX_FACT_CHARS and _CONSTRAINT.search(sentence):
            out.append(sentence)
    return out


def extract_key_facts(messages: Iterable[Any], *, max_facts: int = 20) -> list[str]:
    """Deterministic high-value facts from the span being compressed."""
    facts: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        if not value or len(value) > _MAX_FACT_CHARS:
            return
        # Never re-surface an instruction-bearing string as "verbatim facts":
        # the compressor sanitizes its summary input one line earlier.
        try:
            from app.services.memory_security import scan_injection
            if scan_injection(value):
                return
        except Exception:
            pass
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        facts.append(value)

    for msg in messages or []:
        text = _text_of(msg)
        if not text:
            continue
        # Bound the scan: a 3M-char tool dump only needs its head/tail for
        # key facts; the regexes must stay linear on adversarial input.
        if len(text) > 40000:
            text = text[:20000] + "\n…\n" + text[-20000:]
        for match in _NUMBER.finditer(text):
            add(match.group(0))
        for match in _PATH.finditer(text):
            add(match.group(0))
        for match in _IDENTIFIER_TOKEN.finditer(text):
            token = match.group(0)
            # Two-step filter: a single long lowercase run ("xxxx...") is not
            # an identifier; only snake_case (2+ underscores) or CamelCase.
            if token.count("_") >= 2 or _CAMEL.search(token):
                add(token)
        for match in _CONSTANT.finditer(text):
            add(match.group(0))
        for match in _QUOTED.finditer(text):
            add(match.group(1))
        for sentence in _constraint_sentences(text):
            add(sentence)
        if len(facts) >= int(max_facts):
            break
    return facts[: int(max_facts)]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def fact_recall(facts: Iterable[str], summary: str) -> tuple[float, list[str]]:
    values = [str(f) for f in facts if str(f or "").strip()]
    if not values:
        return 1.0, []
    haystack = _normalise(summary)
    missing = [fact for fact in values if _normalise(fact) not in haystack]
    return round(1.0 - len(missing) / len(values), 4), missing


def repair_summary(
    messages: Iterable[Any],
    summary: str,
    *,
    floor: float = 0.8,
    max_facts: int = 20,
    max_repair_chars: int = _MAX_REPAIR_CHARS,
) -> tuple[str, dict[str, Any]]:
    """Verify recall; append missing facts verbatim when below the floor."""
    facts = extract_key_facts(messages, max_facts=max_facts)
    recall, missing = fact_recall(facts, summary)
    stats = {
        "facts": len(facts),
        "recall": recall,
        "missing": len(missing),
        "repaired": False,
        "floor": float(floor),
    }
    if not missing or recall >= float(floor):
        return summary, stats
    lines: list[str] = []
    used = 0
    for fact in missing:
        line = f"- {fact}"
        if used + len(line) > int(max_repair_chars):
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return summary, stats
    repaired = (
        str(summary).rstrip()
        + "\n\n【逐字关键事实（压缩校验补充）】\n"
        + "\n".join(lines)
    )
    stats["repaired"] = True
    stats["repaired_facts"] = len(lines)
    return repaired, stats
