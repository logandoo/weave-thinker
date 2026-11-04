# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P3 (2026-10-18): prompt/context budget model.

Budgets are estimates, not tokenizer truth. They are intentionally generous
and deterministic so CI can catch runaway prompt growth without a provider
call. Section names mirror the system prompt tiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping

DEFAULT_BUDGETS: Dict[str, int] = {
    "safety_core": 1200,
    "platform_rules": 6000,
    "mode_rules": 8000,
    "task_rules": 8000,
    "persona": 4000,
    "memory": 4000,
    "skills": 20000,
    "history": 60000,
    "tools_schema": 12000,
    "total_system": 24000,
}


@dataclass(frozen=True)
class BudgetReport:
    used: Dict[str, int]
    limits: Dict[str, int]
    violations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


def check_budget(used: Mapping[str, int],
                 limits: Mapping[str, int] | None = None) -> BudgetReport:
    limits = dict(limits or DEFAULT_BUDGETS)
    violations = []
    for name, value in used.items():
        limit = limits.get(name)
        if limit is not None and int(value) > int(limit):
            violations.append(f"{name}: {value} > {limit}")
    return BudgetReport(used=dict(used), limits=limits, violations=tuple(violations))


def total_system_tokens(used: Mapping[str, int]) -> int:
    return sum(int(v) for k, v in used.items() if k != "tools_schema")
