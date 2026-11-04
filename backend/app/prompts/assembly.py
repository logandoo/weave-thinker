# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Prompt assembly & precedence (P1.5, plan 2026-10-18).

Instruction hierarchy is explicit, not an accident of concatenation order:

    SAFETY_CORE (0) > PLATFORM (100) > MODE (200) > TASK (300) >
    PERSONA (400) > DYNAMIC (900)

Only PERSONA may be overridden by an assistant/config system prompt.  Safety,
platform and tool-permission rules are owned by the product and cannot be
replaced by user-editable text.  This module is pure (no IO, no provider
calls) so the assembler can be unit-tested and used by the compiler.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

from app.prompts.spec import PromptSection

TIER_SAFETY = 0
TIER_PLATFORM = 100
TIER_MODE = 200
TIER_TASK = 300
TIER_PERSONA = 400
TIER_DYNAMIC = 900

TIER_ORDER: Tuple[str, ...] = (
    "safety_core",
    "platform",
    "mode",
    "task",
    "persona",
    "dynamic",
)

TIER_PRIORITY = {
    "safety_core": TIER_SAFETY,
    "platform": TIER_PLATFORM,
    "mode": TIER_MODE,
    "task": TIER_TASK,
    "persona": TIER_PERSONA,
    "dynamic": TIER_DYNAMIC,
}

# A safety/platform rule may not be overridden by persona text.  The patterns
# deliberately cover direct override attempts, not normal user preferences.
_OVERRIDE_PATTERNS = (
    re.compile(r"(?i)\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)"),
    re.compile(r"(?i)\bdisregard\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)"),
    re.compile(r"(?i)\bdisregard\s+(your|all|any)\s+(instructions|rules|guidelines)"),
    re.compile(r"(?i)\bignore\s+(your|all|any)\s+(instructions|rules|guidelines)"),
    re.compile(r"忽略(以上|之前|前面|所有)(的)?(所有)?(指令|规则|要求)"),
    re.compile(r"无视(以上|之前|前面|所有)(的)?(指令|规则|要求)"),
    re.compile(r"(?i)you\s+are\s+now\s+(the\s+)?(system|developer|root)"),
    re.compile(r"(?i)reveal\s+(the\s+)?(system\s+)?prompt"),
    re.compile(r"输出(你的)?(系统|内部)(提示词|规则)"),
    re.compile(r"(?i)bypass\s+(the\s+)?(safety|security|tool)\s+(rules|policy)"),
    re.compile(r"(?i)tool\s+permission(s)?\s*[:=]\s*all"),
)


def tier_of(section: PromptSection) -> str:
    tier = getattr(section, "tier", "") or ""
    return tier if tier in TIER_PRIORITY else "platform"


def _sort_key(section: PromptSection) -> Tuple[int, int, str]:
    return (TIER_PRIORITY[tier_of(section)], int(section.priority), section.id)


def ordered(sections: Iterable[PromptSection]) -> List[PromptSection]:
    return sorted(sections, key=_sort_key)


def render(sections: Iterable[PromptSection], separator: str = "\n\n") -> str:
    return separator.join(s.text for s in ordered(sections) if s.text)


@dataclass(frozen=True)
class OverrideDecision:
    accepted: bool
    issues: Tuple[str, ...] = ()
    text: str = ""


def validate_override(text: str) -> OverrideDecision:
    """Return whether a persona override is safe to accept.

    The override is never allowed to replace safety/platform rules; callers
    either drop it (``accepted=False``) or keep it as a PERSONA section.
    """
    raw = str(text or "")
    issues = [p.pattern for p in _OVERRIDE_PATTERNS if p.search(raw)]
    if issues:
        return OverrideDecision(accepted=False, issues=tuple(issues), text="")
    return OverrideDecision(accepted=True, text=raw)


def persona_section(text: str, *, section_id: str = "persona.assistant") -> PromptSection:
    decision = validate_override(text)
    if not decision.accepted:
        raise ValueError(
            "persona override attempts to replace safety/platform rules: "
            + ", ".join(decision.issues)
        )
    return PromptSection(
        id=section_id,
        text=decision.text.strip(),
        priority=TIER_PERSONA,
        tier="persona",
        stability="stable",
        rule_ids=(),
    )


@dataclass(frozen=True)
class AssembledPrompt:
    text: str
    sections: Tuple[PromptSection, ...]
    stable_text: str
    volatile_text: str

    @property
    def stable_hash(self) -> str:
        import hashlib

        return hashlib.sha256(
            self.stable_text.encode("utf-8", errors="replace")
        ).hexdigest()


@dataclass
class PromptAssembler:
    """Collect sections by tier, keep stable/volatile separate, render once."""

    sections: List[PromptSection] = field(default_factory=list)

    def add(self, section: PromptSection) -> "PromptAssembler":
        self.sections.append(section)
        return self

    def extend(self, sections: Sequence[PromptSection]) -> "PromptAssembler":
        self.sections.extend(sections)
        return self

    def add_persona(self, text: str) -> "PromptAssembler":
        if str(text or "").strip():
            decision = validate_override(text)
            if decision.accepted:
                self.sections.append(persona_section(text))
        return self

    def assemble(self) -> AssembledPrompt:
        ordered_sections = ordered(self.sections)
        stable = [s for s in ordered_sections if s.stability != "volatile"]
        volatile = [s for s in ordered_sections if s.stability == "volatile"]
        return AssembledPrompt(
            text=render(ordered_sections),
            sections=tuple(ordered_sections),
            stable_text=render(stable),
            volatile_text=render(volatile),
        )
