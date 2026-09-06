# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""DeepSeek thinking profile：thinking{type} + reasoning_effort(low/high/max)；
reasoning_content 回传语义（tools 携带时必须回传）→ preserve_thinking=True。
"""
from typing import Any, Dict, Optional

from app.model_gateway.profiles.base import ThinkingProfile


class DeepSeekProfile(ThinkingProfile):
    name = "deepseek"
    efforts = ("low", "high", "max")
    preserve_thinking = True

    def enable(self, effort: Optional[str] = None, budget: Optional[int] = None,
               preserve: Optional[bool] = None) -> dict:
        body: Dict[str, Any] = {"thinking": {"type": "enabled"}}
        if effort in self.efforts:
            body["reasoning_effort"] = effort
        if budget is not None:
            body["thinking_budget"] = budget
        return body
