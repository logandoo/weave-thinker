# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Qwen (DashScope) thinking profile：顶层 enable_thinking；budget 顶层。
"""
from typing import Any, Dict, Optional

from app.model_gateway.profiles.base import ThinkingProfile


class QwenDashscopeProfile(ThinkingProfile):
    name = "qwen"

    def enable(self, effort: Optional[str] = None, budget: Optional[int] = None,
               preserve: Optional[bool] = None) -> dict:
        body: Dict[str, Any] = {"enable_thinking": True}
        if budget is not None:
            body["thinking_budget"] = budget
        return body

    def disable(self, preserve: Optional[bool] = None) -> dict:
        return {"enable_thinking": False}
