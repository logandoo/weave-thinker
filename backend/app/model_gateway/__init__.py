# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""model_gateway — 模型访问统一网关。

系统内所有模型（llm / embedding / rerank / asr / tts）的访问经此包：
- schemas.ModelEndpoint：端点配置载体
- registry.ModelRegistry / get_model_registry()：端点注册与解析
- gateway.ModelGateway / get_model_gateway()：统一调用 facade
- factory：端点 → 具体服务客户端（llm_service/asr_service/tts_service）
"""
from app.model_gateway.schemas import (
    KIND_ASR,
    KIND_EMBEDDING,
    KIND_LLM,
    KIND_RERANK,
    KIND_TTS,
    KINDS,
    ModelEndpoint,
)
from app.model_gateway.registry import (
    ModelRegistry,
    get_model_registry,
    reload_model_registry,
)

__all__ = [
    "KIND_ASR",
    "KIND_EMBEDDING",
    "KIND_LLM",
    "KIND_RERANK",
    "KIND_TTS",
    "KINDS",
    "ModelEndpoint",
    "ModelRegistry",
    "get_model_registry",
    "reload_model_registry",
]
