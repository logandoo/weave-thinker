# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""DeepSeek thinking profile：thinking{type} + reasoning_effort(low/high/max)；
reasoning_content 回传语义（tools 携带时必须回传）→ preserve_thinking=True。

与 deepseek-harness `llm-deepseek` 对齐（2026-10-18）：
- effort 只有 low/high/max；默认 high；不发送 `thinking_budget`（官方 API
  没有该旋钮，harness 也只发 effort / output_config.effort）。
- 思考模式忽略 temperature / presence_penalty / frequency_penalty；top_p 对
  deepseek-flash 仅在 [0.95, 1.0] 生效。sanitize_request 把这些“文档写明
  不生效”的参数从 wire 上剔除，避免误导值与无谓的缓存键差异。
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
        # 2026-10-18: no thinking_budget — DeepSeek documents effort only.
        return body

    def sanitize_request(self, body: dict, thinking: bool) -> dict:
        body = dict(body)
        body.pop("thinking_budget", None)
        extra = body.get("extra_body")
        if isinstance(extra, dict):
            extra = dict(extra)
            extra.pop("thinking_budget", None)
            body["extra_body"] = extra
        if not thinking:
            return body
        # Thinking mode ignores these three (official thinking_mode guide).
        for key in ("temperature", "presence_penalty", "frequency_penalty"):
            body.pop(key, None)
        # deepseek-flash: thinking-mode top_p is clamped to [0.95, 1.0];
        # below 0.95 is treated as 0.95. Send the effective value.
        top_p = body.get("top_p")
        if isinstance(top_p, (int, float)):
            body["top_p"] = min(1.0, max(0.95, float(top_p)))
        return body
