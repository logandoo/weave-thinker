# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""profiles 装配：provider_type → ThinkingProfile；自定义端点 sniff。

- get_thinking_profile(provider_type)：声明式路由（未知→GenericProfile）
- sniff_thinking_profile(base_url, model_name)：legacy custom 端点的
  启发式路由（dashscope/aliyuncs URL→qwen；qwen 模型名→qwen3.8_vllm；
  其他→custom_compat）——替代 llm_service 两处的 URL/模型名子串判断。
"""
from typing import Dict

from app.model_gateway.profiles.base import ThinkingProfile
from app.model_gateway.profiles.deepseek import DeepSeekProfile
from app.model_gateway.profiles.qwen3_8_next import Qwen38NextProfile
from app.model_gateway.profiles.qwen3_8_vllm import Qwen38VllmProfile
from app.model_gateway.profiles.qwen_dashscope import QwenDashscopeProfile
from app.model_gateway.profiles.vendors import (
    CustomCompatProfile,
    MiMoProfile,
    ZhipuProfile,
)

_GENERIC = ThinkingProfile()
_CUSTOM_COMPAT = CustomCompatProfile()

_PROFILES: Dict[str, ThinkingProfile] = {
    p.name: p
    for p in (
        DeepSeekProfile(),
        Qwen38VllmProfile(),
        Qwen38NextProfile(),
        QwenDashscopeProfile(),
        ZhipuProfile(),
        MiMoProfile(),
    )
}


def get_thinking_profile(provider_type: str) -> ThinkingProfile:
    return _PROFILES.get(provider_type or "", _GENERIC)


def sniff_thinking_profile(base_url: str, model_name: str) -> ThinkingProfile:
    url = (base_url or "").lower()
    if "dashscope" in url or "aliyuncs" in url:
        return _PROFILES["qwen"]
    _model = (model_name or "").lower()
    if "flash-next" in _model:
        # Qwen3.8-Flash-Next：无 thinking_budget 变量——llm_service 的
        # extra_body 归一化按模型名 sniff，budget 剥离必须在此命中。
        return _PROFILES["qwen3.8_next"]
    if "qwen" in _model:
        return _PROFILES["qwen3.8_vllm"]
    return _CUSTOM_COMPAT


def preserve_thinking_provider_types() -> tuple:
    """PRESERVE_THINKING_PROVIDERS 的派生形式（单一事实源在 profile）。"""
    return tuple(name for name, p in _PROFILES.items() if p.preserve_thinking)
