# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for delegated-task status sets (Wave 2).

Every delete guard, claim query and UI must use these tuples; drift between
copies is how a parked `waiting_*` task's C2 gets deleted (review W2-C1).
"""
from __future__ import annotations

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "timed_out")
ACTIVE_STATUSES = (
    "pending", "claimed", "running", "resumable",
    "waiting_input", "waiting_auth", "waiting_budget", "cancelling",
)
STEERABLE_STATUSES = (
    "pending", "claimed", "running", "resumable",
    "waiting_input", "waiting_auth", "waiting_budget",
)
WAITING_STATUSES = ("waiting_input", "waiting_auth", "waiting_budget")
