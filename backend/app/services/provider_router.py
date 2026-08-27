# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str = ""
    api_mode: str = "chat_completions"
    model_name: str = ""
    default_headers: Dict[str, str] = field(default_factory=dict)
    priority: int = 0


class ProviderAdapter(Protocol):
    name: str

    def is_available(self) -> bool: ...
    def get_client_kwargs(self) -> Dict[str, Any]: ...
    def get_model_name(self) -> str: ...


class OpenAIAdapter:
    name = "openai"

    def __init__(self, cfg: ProviderConfig):
        self.config = cfg

    def is_available(self) -> bool:
        return bool(self.config.base_url)

    def get_client_kwargs(self) -> Dict[str, Any]:
        return {
            "base_url": self.config.base_url,
            "api_key": self.config.api_key or config.api_key or "dummy-key",
        }

    def get_model_name(self) -> str:
        return self.config.model_name or config.model_name


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, cfg: ProviderConfig):
        self.config = cfg

    def is_available(self) -> bool:
        return bool(self.config.base_url and self.config.api_key)

    def get_client_kwargs(self) -> Dict[str, Any]:
        return {
            "base_url": self.config.base_url,
            "api_key": self.config.api_key,
        }

    def get_model_name(self) -> str:
        return self.config.model_name or "claude-sonnet-4-20250514"


class MiMoAdapter:
    name = "mimo"

    def __init__(self, cfg: ProviderConfig):
        self.config = cfg

    def is_available(self) -> bool:
        return bool(self.config.base_url)

    def get_client_kwargs(self) -> Dict[str, Any]:
        return {
            "base_url": self.config.base_url,
            "api_key": self.config.api_key or "dummy-key",
        }

    def get_model_name(self) -> str:
        return self.config.model_name or "mimo-v2.5-pro"


class OpenRouterAdapter:
    name = "openrouter"

    def __init__(self, cfg: ProviderConfig):
        self.config = cfg

    def is_available(self) -> bool:
        return bool(self.config.base_url)

    def get_client_kwargs(self) -> Dict[str, Any]:
        return {
            "base_url": self.config.base_url,
            "api_key": self.config.api_key or "placeholder",
        }

    def get_model_name(self) -> str:
        return self.config.model_name or "anthropic/claude-opus-4-20250514"


_ADAPTER_REGISTRY = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "openrouter": OpenRouterAdapter,
    "mimo": MiMoAdapter,
}

# Qwen3.8-27B-FP8 model-card defaults (modelscope.cn/models/Qwen/Qwen3.8-27B-FP8).
# Used when the assistant leaves a sampling field NULL.
QWEN38_VLLM_THINKING_DEFAULTS = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repetition_penalty": 1.0,
}
QWEN38_VLLM_NON_THINKING_DEFAULTS = {
    "temperature": 0.7,
    "top_p": 0.80,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
}
QWEN38_VLLM_REASONING_EFFORTS = ("xhigh", "medium", "low")


def build_thinking_extra_body(
    provider_type: str,
    enable_reasoning: bool,
    reasoning_effort: str | None = None,
    custom_extra_body: str | None = None,
    thinking_budget: int | None = None,
    preserve_thinking: bool | None = None,
) -> dict:
    """Build the extra_body dict for enabling/disabling thinking mode"""

    thinking_params: dict = {}
    if provider_type in ("zhipu", "deepseek"):
        thinking_params = {"thinking": {"type": "enabled" if enable_reasoning else "disabled"}}
        if provider_type == "deepseek" and enable_reasoning and reasoning_effort in ("low", "high", "max"):
            thinking_params["reasoning_effort"] = reasoning_effort
    elif provider_type == "qwen":
        thinking_params = {"enable_thinking": enable_reasoning}
    elif provider_type == "qwen3.8_vllm":
        # Qwen3.8 on vLLM: thinking toggles, reasoning depth and
        # preserve_thinking all live in chat_template_kwargs (modelscope
        # model card / Qwen docs). llm_service passes extra_body verbatim
        # for custom providers, so build the final shape here.
        ctk: dict = {"enable_thinking": enable_reasoning}
        if enable_reasoning and reasoning_effort in QWEN38_VLLM_REASONING_EFFORTS:
            ctk["reasoning_effort"] = reasoning_effort
        if preserve_thinking is not None:
            ctk["preserve_thinking"] = preserve_thinking
        thinking_params = {"chat_template_kwargs": ctk}
    else:
        thinking_params = {"thinking": {"type": "enabled" if enable_reasoning else "disabled"}}

    if thinking_budget is not None and enable_reasoning:
        if provider_type == "qwen3.8_vllm":
            thinking_params["chat_template_kwargs"]["thinking_budget"] = thinking_budget
        else:
            thinking_params["thinking_budget"] = thinking_budget

    if provider_type == "custom" and custom_extra_body:
        try:
            parsed = json.loads(custom_extra_body)
            if isinstance(parsed, dict):
                # Merge thinking params with custom extra_body (thinking params take precedence)
                merged = {**parsed, **thinking_params}
                return merged
        except json.JSONDecodeError:
            pass
        return thinking_params

    if provider_type in ("zhipu", "deepseek"):
        return thinking_params

    if provider_type == "qwen":
        return thinking_params

    return thinking_params


class ProviderRouter:
    def __init__(self):
        self._providers: List[ProviderConfig] = []
        self._adapters: Dict[str, ProviderAdapter] = {}
        self._load_providers()

    def _load_providers(self):
        # Default provider from config.toml
        default = ProviderConfig(
            name="default",
            base_url=config.api_base_url,
            api_key=config.api_key or "",
            model_name=config.model_name or "",
            api_mode="chat_completions",
            priority=0,
        )
        self._providers.append(default)
        adapter_cls = _ADAPTER_REGISTRY.get("openai", OpenAIAdapter)
        self._adapters["default"] = adapter_cls(default)

        # Additional providers from config
        additional = config.providers
        if not isinstance(additional, dict):
            return
        for name, pdata in additional.items():
            pc = ProviderConfig(
                name=str(name),
                base_url=str(pdata.get("base_url", "")),
                api_key=str(pdata.get("api_key", "")),
                model_name=str(pdata.get("model_name", "")),
                api_mode=str(pdata.get("api_mode", "chat_completions")),
                priority=int(pdata.get("priority", 0)),
            )
            if pc.base_url:
                self._providers.append(pc)
                adapter_type = str(pdata.get("type", "openai")).lower()
                adapter_cls = _ADAPTER_REGISTRY.get(adapter_type, OpenAIAdapter)
                self._adapters[name] = adapter_cls(pc)

        self._providers.sort(key=lambda p: p.priority)

    def get_provider(self, name: str = None) -> Optional[ProviderAdapter]:
        if name:
            return self._adapters.get(name)
        return self._adapters.get("default")

    def list_available(self) -> List[str]:
        return [p.name for p in self._providers]

    def get_client_kwargs(self, provider_name: str = None) -> Dict[str, Any]:
        adapter = self.get_provider(provider_name)
        if adapter is None:
            return {}
        return adapter.get_client_kwargs()

    def get_model_name(self, provider_name: str = None) -> str:
        adapter = self.get_provider(provider_name)
        if adapter is None:
            return config.model_name or "gpt-3.5-turbo"
        return adapter.get_model_name()

    async def embedding_available(self, provider_name: str = None) -> tuple[bool, Optional[int]]:
        """§9.11 启动探测：主 provider 是否暴露 /embeddings 端点，返回 (available, dim)。

        404/网络异常/非法响应 → (False, None)；成功 → (True, embedding 维度)。
        """
        import httpx
        kwargs = self.get_client_kwargs(provider_name)
        base_url = str(kwargs.get("base_url", "") or "").rstrip("/")
        api_key = str(kwargs.get("api_key", "") or "")
        if not base_url:
            return False, None
        model = config.memory.get("embedding_model", "text-embedding-3-small")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"input": "probe", "model": model},
                )
                resp.raise_for_status()
                data = resp.json()
                emb = data["data"][0]["embedding"]
                return True, len(emb)
        except Exception:
            return False, None


_provider_router: Optional[ProviderRouter] = None


def get_provider_router() -> ProviderRouter:
    global _provider_router
    if _provider_router is None:
        _provider_router = ProviderRouter()
    return _provider_router
