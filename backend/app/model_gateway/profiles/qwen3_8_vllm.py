# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Qwen3.8 vLLM thinking profile：一切走 chat_template_kwargs
（enable_thinking / reasoning_effort(xhigh,medium,low) / thinking_budget /
preserve_thinking）；含模型卡思考/非思考采样预设（modelscope Qwen3.8-27B-FP8）。
"""
from typing import Any, Dict, Optional

from app.model_gateway.profiles.base import ThinkingProfile


class Qwen38VllmProfile(ThinkingProfile):
    name = "qwen3.8_vllm"
    efforts = ("xhigh", "medium", "low")
    preserve_thinking = True
    # Qwen3.8-27B-FP8 model-card defaults (modelscope.cn/models/Qwen/Qwen3.8-27B-FP8)
    SAMPLING_THINKING = {
        "temperature": 1.0, "top_p": 0.95, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
    }
    SAMPLING_NON_THINKING = {
        "temperature": 0.7, "top_p": 0.80, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0,
    }

    def enable(self, effort: Optional[str] = None, budget: Optional[int] = None,
               preserve: Optional[bool] = None) -> dict:
        ctk: Dict[str, Any] = {"enable_thinking": True}
        if effort in self.efforts:
            ctk["reasoning_effort"] = effort
        if preserve is not None:
            ctk["preserve_thinking"] = preserve
        if budget is not None:
            ctk["thinking_budget"] = budget
        return {"chat_template_kwargs": ctk}

    def disable(self, preserve: Optional[bool] = None) -> dict:
        # preserve_thinking 不门控 enable（旧 provider_router 行为，A4.9 wave-7
        # 复审 Critical）：关思考也携带——vLLM 模板据此决定是否保留历史
        # reasoning_content，grace/retry/thinking-off 路径依赖该保真。
        ctk: Dict[str, Any] = {"enable_thinking": False}
        if preserve is not None:
            ctk["preserve_thinking"] = preserve
        return {"chat_template_kwargs": ctk}

    def normalize_user_extra_body(self, extra_body: dict) -> dict:
        """vLLM Qwen 的用户 extra_body 归一化（原 llm_service custom 翻译）。

        顶层 enable_thinking 一律迁入 ctk；OpenAI 风格 thinking{type} 翻译为
        ctk.enable_thinking（setdefault，不覆盖显式 enable_thinking）；
        thinking_budget 迁入 ctk。vLLM 对顶层三个参数全部无视——不翻译
        "disable thinking" 会静默失效并白烧 reasoning token（title 生成事故）。
        """
        body = dict(extra_body)
        ctk = body.get("chat_template_kwargs")
        if not isinstance(ctk, dict):
            ctk = {}
        else:
            ctk = dict(ctk)
        if "enable_thinking" in body:
            ctk["enable_thinking"] = body.pop("enable_thinking")
        thinking_cfg = body.get("thinking")
        if isinstance(thinking_cfg, dict) and "type" in thinking_cfg:
            ctk.setdefault("enable_thinking", thinking_cfg["type"] == "enabled")
            body.pop("thinking", None)
        if "thinking_budget" in body:
            ctk["thinking_budget"] = body.pop("thinking_budget")
        if ctk:
            body["chat_template_kwargs"] = ctk
        return body
