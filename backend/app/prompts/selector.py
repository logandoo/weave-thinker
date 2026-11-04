# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P3.2 deterministic section selection by mode/task.

Safety sections are always included; mode/task only add non-safety sections.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Sequence

SAFETY_SECTIONS = ("safety_core", "memory.instruction", "tool.availability")

MODE_SECTIONS: Mapping[str, Sequence[str]] = {
    "chat": ("platform_rules", "mode_rules", "persona"),
    "voice": ("platform_rules", "mode_rules", "voice_rules"),
    "deathmatch": ("platform_rules", "mode_rules", "deathmatch_rules"),
    "worker": ("platform_rules", "worker_rules"),
    "subagent": ("platform_rules", "subagent_rules"),
}

TASK_SECTIONS: Mapping[str, Sequence[str]] = {
    "research": ("research_rules", "citation_rules"),
    "coding": ("coding_rules", "tool_rules"),
    "creative": ("creative_rules",),
    "office": ("office_rules", "tool_rules"),
}


def select_sections(mode: str, task: str = "",
                    available: Iterable[str] | None = None) -> List[str]:
    """Stable, de-duplicated section ids. Safety always first."""
    chosen: List[str] = list(SAFETY_SECTIONS)
    for name in MODE_SECTIONS.get(str(mode or "chat"), MODE_SECTIONS["chat"]):
        if name not in chosen:
            chosen.append(name)
    for name in TASK_SECTIONS.get(str(task or ""), ()):
        if name not in chosen:
            chosen.append(name)
    if available is not None:
        allowed = set(available)
        chosen = [n for n in chosen if n in allowed or n in SAFETY_SECTIONS]
    return chosen


def filter_sections(sections, mode: str, task: str = ""):
    """Keep selected sections; safety-core sections are never dropped."""
    selected = set(select_sections(mode, task))
    return [
        s for s in sections
        if s.id in selected or getattr(s, "tier", "") == "safety_core"
    ]


def spec_applies(spec, mode: str, task: str = "") -> bool:
    """Scope gate for a PromptSpec (mode '*' matches every mode)."""
    scope = getattr(spec, "scope", {}) or {}
    modes = str(scope.get("mode") or "*")
    if modes not in ("*", "all") and str(mode) not in modes.split("|"):
        return False
    tasks = str(scope.get("task") or "*")
    if tasks not in ("*", "all") and str(task or "") not in tasks.split("|"):
        return False
    return True


def select_specs(specs, mode: str, task: str = ""):
    return [s for s in specs if spec_applies(s, mode, task)]
