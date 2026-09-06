# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""factory — ModelEndpoint → 具体服务客户端。

唯一知道"端点 → LLMService/ASRService/TTSService"构造规则的地方；
服务实现（协议/重试/thinking 语义）保留在原服务模块，此处只做显式注入。
"""
from typing import Optional

from app.model_gateway.schemas import KIND_LLM, ModelEndpoint
from app.services.llm_service import LLMService


def wire_provider_type(llm, legacy_provider_type: str = "deepseek") -> str:
    """wire 层 provider_type（thinking 参数格式/preserve/采样预设的判定依据）。

    以解析端点为准（模型网关 2026-08-30）：model_alias 助手的 DB 旧列
    provider_type 与新模型无关，必须用端点的 provider_type；legacy inline
    端点携带旧列值，行为不变。
    """
    ep = getattr(llm, "endpoint", None)
    return (getattr(ep, "provider_type", "") or "") or legacy_provider_type or "deepseek"


def fallback_wire_type(ep: ModelEndpoint) -> str:
    """provider 热切换等场景的 wire provider_type（A4.9 R3 Minor②修复）。

    端点有 provider_type 用之；没有时给中性缺省 "custom"（通用 OpenAI 兼容
    格式），绝不把别名串（如 "qwen3.8"）当作类型下发——别名不是类型。
    """
    return (ep.provider_type or "") or "custom"


def derive_child_endpoint(parent_llm, model_override: Optional[str] = None) -> ModelEndpoint:
    """子代理/摘要等"继承父客户端"场景的端点推导（2026-08-30 解耦重构）。

    语义同 legacy 的 `parent.client.base_url if parent.is_custom_provider else None`
    模式：custom 父客户端 → 子端点继承其 url/key/model；裸父客户端 → main 端点
    （全局默认采样语义）+ 父 model 透传。model_override 再覆盖 model_name。
    """
    from app.model_gateway.registry import get_model_registry

    ep = getattr(parent_llm, "endpoint", None)
    if ep is None:
        # 过渡期为 legacy 构造的服务（无端点元数据）兜底
        if getattr(parent_llm, "is_custom_provider", False):
            ep = ModelEndpoint(
                alias="derived:parent",
                kind=KIND_LLM,
                base_url=str(getattr(parent_llm.client, "base_url", "") or ""),
                api_key=str(getattr(parent_llm.client, "api_key", "") or ""),
                model_name=getattr(parent_llm, "custom_model_name", None) or "",
                is_custom=True,
            )
        else:
            ep = get_model_registry().get("main")
            parent_model = getattr(parent_llm, "custom_model_name", None)
            if parent_model and parent_model != ep.model_name:
                ep = ep.with_overrides(model_name=parent_model)
    if model_override and model_override != ep.model_name:
        ep = ep.with_overrides(model_name=model_override)
    return ep


def build_llm_service(ep: ModelEndpoint, preserve_reasoning: bool = False) -> LLMService:
    """按端点构造 LLMService。

    is_custom=False 的端点（如 "main"）走裸构造（[api] fallback + 全局默认采样）；
    is_custom=True 的端点显式注入 url/key/model（legacy custom provider 语义）。
    """
    if ep.kind != KIND_LLM:
        raise TypeError(f"endpoint {ep.alias!r} is kind={ep.kind}, not llm")
    if ep.is_custom:
        # 空键守卫（2026-09-01 no-key 哨兵从配置字面量改为消费层）：显式 base_url
        # 的端点空键 = 该服务不需要鉴权 → wire "no-key"，绝不传 None（None 会在
        # llm_service 回落全局 main key，把 DeepSeek key 发进无鉴权本地服务器）。
        return LLMService(
            custom_api_url=ep.base_url or None,
            custom_api_key=ep.api_key or ("no-key" if ep.base_url else None),
            custom_model_name=ep.model_name or None,
            preserve_reasoning=preserve_reasoning,
            endpoint=ep,
        )
    # 裸构造语义（legacy LLMService()）：url 回落 [api]，全局默认采样生效；
    # model_name 透传（legacy AuxiliaryClient 的 custom_model_name 覆盖语义，
    # 例如 aux.compression → 主端点 + 指定模型）；api_key 透传（legacy
    # custom 行空 url 时行级 key 照传 llm_service 的语义，A4.9 复审 Important-2）。
    return LLMService(
        custom_api_key=ep.api_key or ("no-key" if ep.base_url else None),
        custom_model_name=ep.model_name or None,
        preserve_reasoning=preserve_reasoning,
        endpoint=ep,
    )
