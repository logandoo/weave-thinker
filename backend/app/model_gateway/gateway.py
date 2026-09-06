# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""ModelGateway — 系统层统一模型访问门面。

调用方只需给一个字段指定模型：
- `model="main"`（逻辑别名），或
- `purpose="judge"`（系统内部用途，经 routing 映射到别名）。

网关按端点 kind 分派到对应适配器；配置（url/key/model/参数）全部来自
ModelRegistry，调用方不再接触任何模型配置键。
"""
import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from app.model_gateway import factory
from app.model_gateway.registry import ModelRegistry, get_model_registry
from app.model_gateway.schemas import (
    KIND_ASR,
    KIND_EMBEDDING,
    KIND_LLM,
    KIND_RERANK,
    KIND_TTS,
    ModelEndpoint,
)

logger = logging.getLogger(__name__)


class ModelGateway:
    def __init__(self, registry: Optional[ModelRegistry] = None):
        self.registry = registry or get_model_registry()

    # ---------------- 端点解析 ----------------

    def endpoint_for(self, model: Optional[str] = None, purpose: Optional[str] = None,
                     expect_kind: Optional[str] = None) -> ModelEndpoint:
        if purpose:
            ep = self.registry.resolve(purpose)
        elif model:
            ep = self.registry.get(model)
        else:
            # facade 缺省：llm → "main" 主端点；其余 kind → 同名缺省端点
            default = "main" if expect_kind in (None, KIND_LLM) else expect_kind
            ep = self.registry.get(default)
        if expect_kind is not None and ep.kind != expect_kind:
            raise TypeError(
                f"model {ep.alias!r} is kind={ep.kind}, expected {expect_kind}"
            )
        return ep

    # ---------------- LLM ----------------

    def llm_service(self, model: Optional[str] = None, purpose: Optional[str] = None,
                    preserve_reasoning: bool = False):
        ep = self.endpoint_for(model=model, purpose=purpose, expect_kind=KIND_LLM)
        return factory.build_llm_service(ep, preserve_reasoning=preserve_reasoning)

    async def chat(self, messages: list, model: Optional[str] = None,
                   purpose: Optional[str] = None, **kw) -> str:
        return await self.llm_service(model=model, purpose=purpose).complete_chat(messages, **kw)

    async def chat_parts(self, messages: list, model: Optional[str] = None,
                         purpose: Optional[str] = None, **kw) -> tuple:
        return await self.llm_service(model=model, purpose=purpose).complete_chat_parts(messages, **kw)

    def stream_chat(self, messages: list, model: Optional[str] = None,
                    purpose: Optional[str] = None, **kw) -> AsyncIterator[Dict[str, Any]]:
        return self.llm_service(model=model, purpose=purpose).stream_chat_structured(messages, **kw)

    # ---------------- embedding / rerank ----------------

    async def embed(self, texts: List[str], model: Optional[str] = None,
                    purpose: Optional[str] = None):
        ep = self.endpoint_for(model=model, purpose=purpose, expect_kind=KIND_EMBEDDING)
        return await self._call_embed(ep, texts)

    async def rerank(self, query: str, documents: List[str], model: Optional[str] = None,
                     purpose: Optional[str] = None, top_n: Optional[int] = None,
                     timeout: float = 10.0):
        ep = self.endpoint_for(model=model, purpose=purpose, expect_kind=KIND_RERANK)
        return await self._call_rerank(ep, query, documents, top_n, timeout)

    # ---------------- ASR / TTS ----------------

    async def transcribe(self, audio_data: bytes, filename: str = "audio.wav",
                         model: Optional[str] = None, purpose: Optional[str] = None, **kw):
        ep = self.endpoint_for(model=model, purpose=purpose, expect_kind=KIND_ASR)
        return await self._call_transcribe(ep, audio_data, filename, **kw)

    def synthesize(self, text: str, model: Optional[str] = None,
                   purpose: Optional[str] = None, **kw):
        ep = self.endpoint_for(model=model, purpose=purpose, expect_kind=KIND_TTS)
        return self._call_synthesize(ep, text, **kw)

    # ---------------- 同步包装（非 async 上下文专用，如 executor/CLI/测试） ----------------

    def chat_sync(self, messages: list, model: Optional[str] = None,
                  purpose: Optional[str] = None, **kw) -> str:
        return asyncio.run(self.chat(messages, model=model, purpose=purpose, **kw))

    def embed_sync(self, texts: List[str], model: Optional[str] = None,
                   purpose: Optional[str] = None):
        return asyncio.run(self.embed(texts, model=model, purpose=purpose))

    def rerank_sync(self, query: str, documents: List[str], model: Optional[str] = None,
                    purpose: Optional[str] = None, top_n: Optional[int] = None,
                    timeout: float = 10.0):
        return asyncio.run(self.rerank(query, documents, model=model, purpose=purpose,
                                       top_n=top_n, timeout=timeout))

    def transcribe_sync(self, audio_data: bytes, filename: str = "audio.wav",
                        model: Optional[str] = None, purpose: Optional[str] = None, **kw):
        return asyncio.run(self.transcribe(audio_data, filename, model=model, purpose=purpose, **kw))

    def synthesize_sync(self, text: str, model: Optional[str] = None,
                        purpose: Optional[str] = None, **kw):
        return self.synthesize(text, model=model, purpose=purpose, **kw)

    # ---------------- kind 适配器（私有；W2 起各服务内部改由端点驱动） ----------------

    async def _call_embed(self, ep: ModelEndpoint, texts: List[str]):
        from app.services import memory_embedding_service
        return await memory_embedding_service.embed_texts(texts)

    async def _call_rerank(self, ep: ModelEndpoint, query: str, documents: List[str],
                           top_n: Optional[int], timeout: float):
        import httpx
        if not ep.base_url:
            raise RuntimeError(f"rerank endpoint {ep.alias!r} has no base_url configured")
        payload: Dict[str, Any] = {
            "model": ep.model_name,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        headers = {"Content-Type": "application/json"}
        if ep.api_key:
            headers["Authorization"] = f"Bearer {ep.api_key}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(f"{ep.base_url.rstrip('/')}/rerank", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        # TEI 返回裸 list；Xinference/Jina 返回 {"results": [...]}
        if isinstance(data, list):
            return data
        return data.get("results") or data.get("data") or []

    async def _call_transcribe(self, ep: ModelEndpoint, audio_data: bytes, filename: str, **kw):
        from app.services.asr_service import ASRService
        return await ASRService().transcribe(audio_data, filename=filename, **kw)

    def _call_synthesize(self, ep: ModelEndpoint, text: str, **kw):
        from app.services.tts_service import get_tts_service
        return get_tts_service().stream_tts(text, **kw)


_gateway: Optional[ModelGateway] = None


def get_model_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
