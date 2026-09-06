# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
from contextvars import ContextVar
from typing import Optional

from app.services.llm_service import LLMService
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

# P0 (2026-08-21, user requirement): per-assistant aux LLM override. When an
# assistant uses a custom model, ALL in-request LLM behavior (classifiers,
# judges, error classifier, compression summaries, code-gen helpers) must
# follow the assistant's model unless explicitly configured per-assistant.
# ContextVar is task-local: each HTTP request / worker task sets it once;
# no cross-request leakage. Unset (None) = legacy global-aux-key behavior.
_aux_llm_override: ContextVar = ContextVar("aux_llm_override", default=None)


def set_aux_llm_override(llm) -> None:
    """Route all AuxiliaryClient calls in this task through ``llm``."""
    _aux_llm_override.set(llm)


def get_aux_llm_override() -> Optional[LLMService]:
    return _aux_llm_override.get()


_CLASSIFIER_TASKS = {
    "error_classify", "triviality", "interest_extract", "skill_assess",
    "identity_facts", "citation_disambiguate", "schedule_parse",
    "creative_goal", "completion_reconcile", "title_fallback",
}

_TASK_PURPOSE = {
    "compression": "aux.compression",
    "search_decision": "aux.search_decision",
    "title": "aux.title",
}


class AuxiliaryClient:
    def __init__(self, task: str = "default", llm: Optional[LLMService] = None):
        self.task = task
        _override = llm or get_aux_llm_override()
        if _override is not None:
            # P0: an explicit per-assistant client wins over global aux
            # task-model keys (error_classify/compression/title/...).
            self.llm = _override
        else:
            # model_gateway 收口（2026-08-30）：task → purpose → registry 端点。
            # 裸构造语义（is_custom=False + model_name 覆盖）与 legacy 一致。
            from app.model_gateway import factory
            from app.model_gateway.registry import get_model_registry
            purpose = _TASK_PURPOSE.get(task)
            if purpose is None:
                purpose = "aux.classifier" if task in _CLASSIFIER_TASKS else "main"
            self.llm = factory.build_llm_service(get_model_registry().resolve(purpose))

    async def complete(self, messages: list, **kwargs) -> str:
        content, _ = await self.complete_parts(messages, **kwargs)
        return content

    async def complete_parts(self, messages: list, **kwargs) -> tuple[str, str]:
        model = kwargs.pop("model", self.llm.custom_model_name or config.model_name)
        return await self.llm.complete_chat_parts(
            messages,
            model=model,
            **kwargs,
        )

    async def stream(self, messages: list, **kwargs):
        model = kwargs.pop("model", self.llm.custom_model_name or config.model_name)
        async for chunk in self.llm.stream_chat(messages, model=model, **kwargs):
            yield chunk
