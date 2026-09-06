# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Zhipu thinking profile：thinking{type}（effort 不上 wire）。
"""
from app.model_gateway.profiles.base import ThinkingProfile


class ZhipuProfile(ThinkingProfile):
    name = "zhipu"


class MiMoProfile(ThinkingProfile):
    """MiMo 思考链 preserve 语义；wire 形状同 generic。"""
    name = "mimo"
    preserve_thinking = True


class CustomCompatProfile(ThinkingProfile):
    """非 DashScope、非 qwen 模型名的 OpenAI 兼容自定义端点（sniff 命中）。

    顶层 enable_thinking 迁入 chat_template_kwargs（vLLM 惯例）；
    thinking{type} 原样透传；关思考走 generic thinking{type:disabled}。
    """

    name = "custom_compat"

    def normalize_user_extra_body(self, extra_body: dict) -> dict:
        body = dict(extra_body)
        if "enable_thinking" in body:
            ctk = body.get("chat_template_kwargs")
            if not isinstance(ctk, dict):
                ctk = {}
            else:
                ctk = dict(ctk)
            ctk["enable_thinking"] = body.pop("enable_thinking")
            body["chat_template_kwargs"] = ctk
        return body
