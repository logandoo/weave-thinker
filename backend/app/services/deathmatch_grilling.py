# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deathmatch grilling state mixin (P5.4 move-only, 2026-10-18)."""
from __future__ import annotations

from typing import Tuple


class DeathmatchGrillingMixin:
    @property
    def is_grilling(self) -> bool:
        return (
            self._conv.deathmatch_mode
            and self._conv.deathmatch_status == "grilling"
        )

    @property
    def is_goal_active(self) -> bool:
        return (
            self._conv.deathmatch_mode
            and self._conv.deathmatch_status == "active"
        )

    @property
    def grilling_progress(self) -> Tuple[int, int]:
        """Return (completed, total) for current round grilling subagent questions."""
        return (
            self._conv.deathmatch_grilling_completed or 0,
            self._conv.deathmatch_grilling_total or 0,
        )

    @property
    def all_grilling_complete(self) -> bool:
        total = self._conv.deathmatch_grilling_total or 0
        completed = self._conv.deathmatch_grilling_completed or 0
        return total > 0 and completed >= total

    def activate_grilling(self) -> None:
        self._conv.deathmatch_mode = True
        self._conv.deathmatch_status = "grilling"
        self._conv.deathmatch_grilling_complete = False
        self._conv.deathmatch_turns = 0
        self._conv.deathmatch_consecutive_failures = 0
        self._conv.deathmatch_goal = None
        self._conv.deathmatch_verdict = None
        self._conv.deathmatch_reason = None
        self._conv.deathmatch_grilling_total = 0
        self._conv.deathmatch_grilling_completed = 0
        self._conv.deathmatch_grilling_round = 1
        self._conv.deathmatch_grilling_round_total = self._max_grilling_rounds()
        self._conv.deathmatch_grilling_qa_history = []
        self._conv.deathmatch_expected_marker = None
        self._conv.deathmatch_marker_miss_count = 0
        # C1: a fresh goal round gets a fresh cumulative wall-time budget.
        self._conv.deathmatch_wall_time_used_seconds = 0
        self._conv.deathmatch_wall_time_started_at = None
        # A genuinely NEW round starts clean — the park semantics of
        # deactivate() preserve task state for resume, and this is the only
        # place that wipes it (D3, 2026-08-31 autonomy wave).
        self._conv.deathmatch_plan = None
        self._conv.deathmatch_plan_version = 0
        self._conv.deathmatch_reflections = []
        self._conv.deathmatch_verify_failures = 0
        self._conv.deathmatch_bible_draft = None
        self._conv.deathmatch_settled_ledger = None
        self._conv.deathmatch_human_gate = None
        self._conv.deathmatch_last_verification_result = None
