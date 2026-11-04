# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Prompt compiler: validate sections and emit versioned text + hash."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from app.prompts.spec import PromptSection, PromptSpec

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,}"
    r"|api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,})"
)


class PromptCompileError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledPrompt:
    spec_id: str
    version: str
    owner: str
    text: str
    prompt_hash: str
    estimated_tokens: int
    section_tokens: Dict[str, int]
    stable_hash: str = ""
    locale: str = "zh"
    tier_tokens: Dict[str, int] = field(default_factory=dict)


_CJK_RANGES = (
    (0x3000, 0x303F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x3040, 0x30FF),
    (0xAC00, 0xD7AF), (0x20000, 0x2FA1F),
)
_TIKTOKEN_ENCODER = None
_TIKTOKEN_TRIED = False


def _tiktoken_estimate(text: str) -> int:
    """Use tiktoken only when explicitly enabled (optional, no hard dep).

    ``PROMPT_TOKENIZER=tiktoken`` opts in; default is the deterministic CJK
    fallback so CI/manifest numbers do not change when a dev installs tiktoken.
    """
    import os
    global _TIKTOKEN_ENCODER, _TIKTOKEN_TRIED
    if str(os.environ.get("PROMPT_TOKENIZER", "")).lower() != "tiktoken":
        return -1
    if _TIKTOKEN_TRIED and _TIKTOKEN_ENCODER is None:
        return -1
    _TIKTOKEN_TRIED = True
    try:
        if _TIKTOKEN_ENCODER is None:
            import tiktoken  # type: ignore

            _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        return len(_TIKTOKEN_ENCODER.encode(text))
    except Exception:
        return -1


def estimate_tokens(text: str) -> int:
    """Token estimate with a CJK-aware fallback.

    tiktoken is used when installed (real tokenizer for CI/dev); otherwise a
    deterministic calibrated estimate: CJK ≈ 1 token/char, non-CJK ≈ 1/4
    token/char.  The old ``len//4`` undercounted Chinese by ~3-4×, which made
    budget/compact/context-rot claims meaningless.
    """
    s = str(text or "")
    if not s:
        return 1
    exact = _tiktoken_estimate(s)
    if exact >= 0:
        return max(1, exact)
    cjk = sum(
        1 for ch in s
        if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES)
    )
    other = len(s) - cjk
    return max(1, cjk + other // 4)


def _check_sections(sections: Sequence[PromptSection]) -> None:
    seen_sections: set[str] = set()
    seen_rules: dict[str, str] = {}
    for sec in sections:
        if not sec.id:
            raise PromptCompileError("section id is empty")
        if sec.id in seen_sections:
            raise PromptCompileError(f"duplicate section id: {sec.id}")
        seen_sections.add(sec.id)
        for rid in sec.rule_ids:
            if rid in seen_rules:
                raise PromptCompileError(
                    f"duplicate rule id {rid} in {seen_rules[rid]} and {sec.id}"
                )
            seen_rules[rid] = sec.id


_RULE_SUFFIXES = ("-ALWAYS", "-NEVER")
LONG_SECTION_TOKENS = 1200


def _rule_base(rule_id: str) -> str:
    upper = str(rule_id or "").upper()
    for suffix in _RULE_SUFFIXES:
        if upper.endswith(suffix):
            return upper[: -len(suffix)]
    return ""


def check_conflicts(spec: PromptSpec) -> List[str]:
    """Same-base always/never conflicts (P1.2 conflict matrix).

    A fragment may declare ``rule_ids`` ending in ``-ALWAYS``/``-NEVER``.
    Only the SAME base id (``SEC-TOOL-ALWAYS`` vs ``SEC-TOOL-NEVER``) is a
    conflict; unrelated always/never pairs merely describe different rules.
    An explicit ``overrides`` flag records a reviewed exception.
    """
    issues: List[str] = []
    rules = [rid for s in spec.sections for rid in s.rule_ids]
    always = {_rule_base(r) for r in rules if str(r).upper().endswith("-ALWAYS")}
    never = {_rule_base(r) for r in rules if str(r).upper().endswith("-NEVER")}
    always.discard("")
    never.discard("")
    conflicts = sorted(always & never)
    if conflicts and not spec.flags.get("overrides"):
        issues.append(f"conflicting always/never rules: {conflicts}")
    return issues


def lint_spec(spec: PromptSpec, *, offered_tools: Optional[Iterable[str]] = None,
              today: str = "") -> List[str]:
    issues: List[str] = []
    try:
        _check_sections(spec.ordered_sections())
    except PromptCompileError as exc:
        issues.append(str(exc))
    issues.extend(check_conflicts(spec))
    if spec.is_deprecated_past_due(today or "9999-12-31"):
        issues.append(f"deprecated prompt past remove_after: {spec.id}")
    if offered_tools is not None and spec.flags.get("tool_claims"):
        offered = {str(t) for t in offered_tools}
        claimed = {str(t) for t in spec.flags.get("tool_claims") or ()}
        missing = sorted(claimed - offered)
        if missing:
            issues.append(f"declares unavailable tools: {missing}")
    for sec in spec.sections:
        if sec.budget_tokens and estimate_tokens(sec.text) > int(sec.budget_tokens):
            issues.append(
                f"section {sec.id} over budget: "
                f"{estimate_tokens(sec.text)} > {sec.budget_tokens}"
            )
        if not sec.budget_tokens and estimate_tokens(sec.text) > LONG_SECTION_TOKENS:
            issues.append(
                f"section {sec.id} must declare budget_tokens "
                f"({estimate_tokens(sec.text)} tokens > {LONG_SECTION_TOKENS})"
            )
        if not spec.flags.get("allow_placeholders") and re.search(
            r"__[A-Z][A-Z0-9_]{2,}__", sec.text
        ):
            issues.append(f"{sec.id}: unresolved __PLACEHOLDER__")
        if not spec.flags.get("allow_markup") and re.search(
            r"<\/?tool_call>|<｜｜DSML｜｜>", sec.text
        ):
            issues.append(f"{sec.id}: tool-call markup in prompt")
        for issue in lint_text(sec.text):
            issues.append(f"{sec.id}: {issue}")
    return issues


def compile_prompt(
    spec: PromptSpec,
    *,
    variables: Optional[Dict[str, Any]] = None,
    budget_tokens: Optional[int] = None,
) -> CompiledPrompt:
    sections = spec.ordered_sections()
    _check_sections(sections)
    text = spec.render()
    if variables is not None:
        unknown = [
            m.group(1) for m in _PLACEHOLDER_RE.finditer(text)
            if m.group(1) not in variables
        ]
        if unknown:
            raise PromptCompileError(
                "unresolved placeholders: " + ", ".join(sorted(set(unknown))[:8])
            )
        text = text.format(**variables)
    tokens = estimate_tokens(text)
    if budget_tokens is not None and tokens > int(budget_tokens):
        raise PromptCompileError(
            f"prompt {spec.id} exceeds budget: {tokens} > {budget_tokens}"
        )
    section_tokens = {s.id: estimate_tokens(s.text) for s in sections}
    tier_tokens: Dict[str, int] = {}
    for sec in sections:
        tier_tokens[sec.tier] = tier_tokens.get(sec.tier, 0) + estimate_tokens(sec.text)
    release = f"{spec.id}@{spec.version}\0{text}".encode("utf-8", errors="replace")
    return CompiledPrompt(
        spec_id=spec.id,
        version=spec.version,
        owner=spec.owner,
        text=text,
        prompt_hash=hashlib.sha256(release).hexdigest(),
        estimated_tokens=tokens,
        section_tokens=section_tokens,
        stable_hash=hashlib.sha256(
            spec.stable_text().encode("utf-8", errors="replace")
        ).hexdigest(),
        locale=spec.locale,
        tier_tokens=tier_tokens,
    )


def lint_text(text: str) -> List[str]:
    """Deterministic prompt lint.

    Checks that are language/structure based (not semantic judgments):
      * duplicate adjacent numbered rules (the old codegen ``7.`` bug);
      * credential-looking tokens;
      * unresolved ``__NAME__``-style placeholders;
      * tool-call markup in prompt text (models echo it).
    """
    issues: List[str] = []
    seen: set[str] = set()
    seen_prev: set[str] = set()
    block_first: str | None = None
    last_number = 0
    for line in str(text or "").splitlines():
        m = re.match(r"^\s*(\d+)\.\s", line)
        if not m:
            # Sub-numbered items ("3a.", "4b.") are list continuations, not
            # a new list; empty/heading/prose lines start a new list.
            if re.match(r"^\s*\d+[A-Za-z]\.\s", line):
                continue
            if not line.strip() or not line.startswith((" ", "\t")):
                seen.clear()
                block_first = None
                last_number = 0
            continue
        if line.startswith((" ", "\t")):
            # Nested numbered list (1. → "   1." sub-item) is not a sibling.
            continue
        number = m.group(1)
        value = int(number)
        if block_first is None:
            block_first = number
            # A new list normally restarts at 1.  A blank-separated restart
            # at a non-1 number that already appeared is the codegen ``7.``
            # class bug, not a new list.
            if number != "1" and number in seen_prev:
                issues.append(f"duplicate numbered rules: {number}")
        else:
            if number in seen:
                issues.append(f"duplicate numbered rules: {number}")
            elif value <= last_number and number == "1":
                # 1. after a higher number = a new sub-list inside the block.
                seen.clear()
        seen.add(number)
        seen_prev.add(number)
        last_number = value
    if _SECRET_RE.search(text):
        issues.append("credential-looking token")
    return issues
