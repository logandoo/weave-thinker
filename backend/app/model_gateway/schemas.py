# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""ModelEndpoint — 模型端点数据类（模型网关的唯一配置载体）。

一个端点 = 一个逻辑别名 → 一类模型（llm/embedding/rerank/asr/tts）的
供应商 url、api key、模型名、默认参数与能力描述。系统内任何模型调用
只持有 ModelEndpoint，不再直接读取 config 的模型配置键。
"""
from dataclasses import dataclass, field, replace
from typing import Any, Dict

KIND_LLM = "llm"
KIND_EMBEDDING = "embedding"
KIND_RERANK = "rerank"
KIND_ASR = "asr"
KIND_TTS = "tts"
KINDS = (KIND_LLM, KIND_EMBEDDING, KIND_RERANK, KIND_ASR, KIND_TTS)


@dataclass(frozen=True)
class ModelEndpoint:
    alias: str                       # 逻辑别名（注册表唯一键），如 "main" / "qwen3.8"
    kind: str = KIND_LLM             # KIND_* 之一
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    provider_type: str = ""          # vendor 特判用（deepseek/qwen3.8_vllm/mimo/custom/...）
    display_name: str = ""
    # is_custom=False → llm_service 应用 [defaults] 全局采样（裸 LLMService 语义，
    # 供 main/内部 purpose 使用）；True → 语义同 legacy custom provider。
    is_custom: bool = True
    params: Dict[str, Any] = field(default_factory=dict)        # 默认采样/调用参数
    capabilities: Dict[str, Any] = field(default_factory=dict)  # supports_reasoning / reasoning_efforts / ...
    extra: Dict[str, Any] = field(default_factory=dict)         # kind 专属（asr ws 配置、tts voice、embedding dim ...）

    def with_overrides(self, **kw) -> "ModelEndpoint":
        return replace(self, **kw)

    def public_dict(self) -> dict:
        """前端可见视图——绝不包含 base_url / api_key / 真实 model_name。"""
        return {
            "alias": self.alias,
            "kind": self.kind,
            "display_name": self.display_name or self.alias,
            "capabilities": dict(self.capabilities),
        }
