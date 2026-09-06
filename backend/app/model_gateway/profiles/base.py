# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""ThinkingProfile 基类 —— 同 OpenAI 协议内 vendor 思考差异的适配器基类。

LiteLLM 范式（litellm/llms/<vendor>/chat/transformation.py）：继承基类共享
通用形状（thinking{type}），vendor 子类只覆写差异。层级/规则在代码里；
配置文件（config_model.toml）只做 provider_type 声明与同结构参数微调
（capabilities.effort_meta.params 逃生舱）。
"""
from typing import Any, Dict, Optional


class ThinkingProfile:
    """thinking/reasoning 相关 wire 形状与语义的单一事实源。

    - enable/disable：thinking 开关的 wire 形状
    - normalize_user_extra_body：用户自定义 extra_body 的 vendor 归一化
      （llm_service custom provider 路径）
    - merge_extra_params：effort_meta.params 逃生舱的 provider-aware 合并
    - efforts：reasoning_effort 合法档位（None=不上 wire）
    - preserve_thinking：reasoning_content 回传语义是否适用
    - sampling_defaults：vendor 模型卡的思考/非思考采样预设（空=无）
    """

    name: str = "generic"
    efforts: Optional[tuple] = None
    preserve_thinking: bool = False
    SAMPLING_THINKING: Dict[str, Any] = {}
    SAMPLING_NON_THINKING: Dict[str, Any] = {}

    def enable(self, effort: Optional[str] = None, budget: Optional[int] = None,
               preserve: Optional[bool] = None) -> dict:
        body: Dict[str, Any] = {"thinking": {"type": "enabled"}}
        if budget is not None:
            body["thinking_budget"] = budget
        return body

    def disable(self, preserve: Optional[bool] = None) -> dict:
        return {"thinking": {"type": "disabled"}}

    def normalize_user_extra_body(self, extra_body: dict) -> dict:
        return dict(extra_body)

    def merge_extra_params(self, body: dict, params: dict) -> dict:
        """effort_meta.params 逃生舱合并（旧 agent_loop 内联语义，与 provider 无关）：
        body 已含 chat_template_kwargs 时并入嵌套层，否则顶层合并。"""
        if isinstance(body.get("chat_template_kwargs"), dict):
            return {**body, "chat_template_kwargs": {**body["chat_template_kwargs"], **params}}
        return {**body, **params}

    def sampling_defaults(self, thinking: bool) -> dict:
        return dict(self.SAMPLING_THINKING if thinking else self.SAMPLING_NON_THINKING)
