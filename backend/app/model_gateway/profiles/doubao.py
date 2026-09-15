# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Doubao（火山方舟 Ark）thinking profile（provider_type "doubao"）。

Ark OpenAI 兼容端点（https://ark.cn-beijing.volces.com/api/v3）的豆包
Seed 系列 wire（2026-09-15 真实 API 实测，doubao-seed-2-0-pro-260215）：
- thinking{type: enabled|disabled}：开 → reasoning_content；关 → 无
  （auto 该模型不支持，实测 400 → 不下发）
- 顶层 reasoning_effort ∈ {minimal, low, medium, high}（minimal=不思考）
- 无 thinking_budget 变量——所有 wire 路径剥除（模板静默忽略≠契约允许）
"""
from typing import Any, Dict, Optional

from app.model_gateway.profiles.base import ThinkingProfile


class DoubaoProfile(ThinkingProfile):
    name = "doubao"
    efforts = ("high", "medium", "low", "minimal")
    preserve_thinking = False

    def enable(self, effort: Optional[str] = None, budget: Optional[int] = None,
               preserve: Optional[bool] = None) -> dict:
        body: Dict[str, Any] = {"thinking": {"type": "enabled"}}
        if effort in self.efforts:
            body["reasoning_effort"] = effort
        # budget 显式忽略（Ark 无该参数）。
        return body

    @staticmethod
    def _strip_budget(body: dict) -> dict:
        # 顶层与嵌套 ctk 双路径剥除（A4.9 R1 Minor-3；空壳 ctk 清掉）。
        body.pop("thinking_budget", None)
        ctk = body.get("chat_template_kwargs")
        if isinstance(ctk, dict):
            ctk.pop("thinking_budget", None)
            if not ctk:
                body.pop("chat_template_kwargs")
        return body

    def merge_extra_params(self, body: dict, params: dict) -> dict:
        # effort_meta.params 逃生舱合并后剥除 budget（防重新注入）。
        return self._strip_budget(super().merge_extra_params(body, params))

    def normalize_user_extra_body(self, extra_body: dict) -> dict:
        # 用户显式 thinking_budget 同样剥除。
        return self._strip_budget(super().normalize_user_extra_body(extra_body))
