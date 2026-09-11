# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Qwen3.8-Flash-Next thinking profile（provider_type "qwen3.8_next"）。

模型卡（modelscope.cn/models/Qwen/Qwen3.8-Flash-Next，架构 Qwen4Exp
125B-A6B，262k ctx）与 Qwen3.8-27B 的 chat_template 同构：
enable_thinking / preserve_thinking（默认 true）+ reasoning_effort
{xhigh(默认), medium, low}；采样推荐两档与 27B 卡逐字相同。

与 qwen3.8_vllm 的唯一 wire 差异：模板没有 thinking_budget 变量——
「绝不下发 thinking_budget」在本 profile 的所有 wire 路径上是不变式：
enable() 忽略 budget、merge_extra_params/normalize_user_extra_body
剥除 budget（A4.9 修复轮 Imp-1）。
"""
from typing import Optional

from app.model_gateway.profiles.qwen3_8_vllm import Qwen38VllmProfile


class Qwen38NextProfile(Qwen38VllmProfile):
    name = "qwen3.8_next"
    preserve_thinking = True

    def enable(self, effort: Optional[str] = None, budget: Optional[int] = None,
               preserve: Optional[bool] = None) -> dict:
        # Flash-Next 无 thinking_budget：显式忽略 budget 参数。
        return super().enable(effort=effort, budget=None, preserve=preserve)

    @staticmethod
    def _strip_budget(body: dict) -> dict:
        ctk = body.get("chat_template_kwargs")
        if isinstance(ctk, dict):
            ctk.pop("thinking_budget", None)
            if not ctk:
                body.pop("chat_template_kwargs")
        else:
            body.pop("thinking_budget", None)
        return body

    def merge_extra_params(self, body: dict, params: dict) -> dict:
        # effort_meta.params 逃生舱合并后剥除 budget（防重新注入 ctk）。
        return self._strip_budget(super().merge_extra_params(body, params))

    def normalize_user_extra_body(self, extra_body: dict) -> dict:
        # 用户显式 thinking_budget 同样剥除（模板静默忽略≠契约允许下发）。
        return self._strip_budget(super().normalize_user_extra_body(extra_body))
