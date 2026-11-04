# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Prompt-as-contract primitives (L0/L1, plan 2026-10-18).

A PromptSpec is immutable metadata + ordered sections. The compiler validates
structure and emits a hash so every LLM call can be traced to a prompt version.
This package deliberately contains no runtime IO.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Instruction hierarchy (must match app.prompts.assembly.TIER_PRIORITY).
_TIER_RANK = {
    "safety_core": 0,
    "platform": 100,
    "mode": 200,
    "task": 300,
    "persona": 400,
    "dynamic": 900,
}


@dataclass(frozen=True)
class PromptSection:
    id: str
    text: str
    priority: int = 100
    stability: str = "stable"  # stable | context | volatile
    rule_ids: Tuple[str, ...] = ()
    tier: str = "platform"
    locale: str = ""
    budget_tokens: Optional[int] = None
    untrusted: bool = False

    def __post_init__(self) -> None:
        if self.tier not in _TIER_RANK:
            raise ValueError(f"unknown prompt tier: {self.tier}")
        if self.stability not in ("stable", "context", "volatile"):
            raise ValueError(f"unknown stability: {self.stability}")


@dataclass(frozen=True)
class PromptSpec:
    id: str
    version: str
    owner: str
    sections: Tuple[PromptSection, ...] = ()
    scope: Dict[str, Any] = field(default_factory=dict)
    output_schema: Optional[str] = None
    flags: Dict[str, Any] = field(default_factory=dict)
    deprecated: Optional[Dict[str, Any]] = None
    locale: str = "zh"
    used_by: Tuple[str, ...] = ()
    changelog: Tuple[str, ...] = ()

    def ordered_sections(self) -> List[PromptSection]:
        return sorted(
            self.sections,
            key=lambda s: (_TIER_RANK[s.tier], s.priority, s.id),
        )

    def render(self, separator: str = "\n\n") -> str:
        return separator.join(s.text for s in self.ordered_sections() if s.text)

    def stable_text(self, separator: str = "\n\n") -> str:
        return separator.join(
            s.text for s in self.ordered_sections()
            if s.text and s.stability != "volatile"
        )

    def volatile_text(self, separator: str = "\n\n") -> str:
        return separator.join(
            s.text for s in self.ordered_sections()
            if s.text and s.stability == "volatile"
        )

    def content_hash(self, separator: str = "\n\n") -> str:
        raw = self.render(separator).encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    def is_deprecated_past_due(self, today: str) -> bool:
        if not self.deprecated:
            return False
        remove_after = str(self.deprecated.get("remove_after") or "").strip()
        return bool(remove_after) and remove_after <= str(today)
