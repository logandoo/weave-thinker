# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openai import OpenAI

from app.core.config import get_config
from app.services.provider_router import get_provider_router

logger = logging.getLogger(__name__)
config = get_config()

_DEFAULT_AGGREGATION_PROMPT = (
    "你是一个答案聚合器。你将收到多个AI模型对同一问题的独立回答。\n"
    "你的任务是综合这些回答，取各家之长，生成一个更全面、更准确、更完整的最终答案。\n"
    "要求：\n"
    "1. 综合所有参考回答中的关键信息\n"
    "2. 如果参考回答之间有矛盾，优先采信更详细、更有依据的回答\n"
    "3. 补充任何参考回答中有价值但其他回答遗漏的细节\n"
    "4. 用清晰、结构化的方式呈现最终答案\n"
    "5. 不要简单复制某一个参考回答，而要真正融合\n"
)

def _get_aggregation_prompt() -> str:
    return config.agent_moa.get("aggregation_system_prompt") or _DEFAULT_AGGREGATION_PROMPT


@dataclass
class MoAResponse:
    reference_responses: List[Dict[str, str]]
    aggregated_response: str
    model_used: List[str]


class MoAService:
    def __init__(self):
        self._router = get_provider_router()

    async def run_moa(
        self,
        prompt: str,
        context: str = "",
        reference_providers: Optional[List[str]] = None,
        max_references: int = 3,
        timeout_seconds: float = 120.0,
    ) -> MoAResponse:
        moa_cfg = config.agent_moa
        if not moa_cfg.get("enabled", False):
            raise RuntimeError("MoA is not enabled in config.toml")

        if reference_providers is None:
            reference_providers = moa_cfg.get("reference_providers", [])

        if not reference_providers:
            available = self._router.list_available()
            reference_providers = [p for p in available if p != "default"][:max_references]
            if not reference_providers:
                reference_providers = ["default"]

        full_prompt = prompt
        if context:
            full_prompt = f"{context}\n\n---\n\n{prompt}"

        reference_responses = await self._call_references(
            full_prompt, reference_providers, timeout_seconds
        )

        if not reference_responses:
            raise RuntimeError("All reference models failed to produce a response")

        aggregated = await self._aggregate(prompt, reference_responses, timeout_seconds)

        models_used = [r.get("provider", "unknown") for r in reference_responses]

        return MoAResponse(
            reference_responses=reference_responses,
            aggregated_response=aggregated,
            model_used=models_used,
        )

    async def _call_references(
        self,
        prompt: str,
        providers: List[str],
        timeout_seconds: float,
    ) -> List[Dict[str, str]]:
        coros = []
        for provider_name in providers:
            coros.append(self._call_single_reference(prompt, provider_name, timeout_seconds))

        results = await asyncio.gather(*coros, return_exceptions=True)

        valid = []
        for provider_name, result in zip(providers, results):
            if isinstance(result, Exception):
                logger.warning("MoA reference model '%s' failed: %s", provider_name, result)
                continue
            if result:
                valid.append({"provider": provider_name, "response": result})
        return valid

    async def _call_single_reference(
        self,
        prompt: str,
        provider_name: str,
        timeout_seconds: float,
    ) -> Optional[str]:
        adapter = self._router.get_provider(provider_name)
        if not adapter:
            logger.warning("MoA: provider '%s' not found", provider_name)
            return None

        try:
            kwargs = adapter.get_client_kwargs()
            model_name = adapter.get_model_name()
            client = OpenAI(**kwargs)

            loop = asyncio.get_running_loop()

            def _sync_call():
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=moa_cfg.get("reference_max_tokens") or None,
                )
                if response.choices and response.choices[0].message.content:
                    return response.choices[0].message.content
                return None

            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_call),
                timeout=timeout_seconds,
            )
            return result

        except asyncio.TimeoutError:
            logger.warning("MoA reference model '%s' timed out after %ds", provider_name, timeout_seconds)
            return None
        except Exception as e:
            logger.warning("MoA reference model '%s' error: %s", provider_name, e)
            return None

    async def _aggregate(
        self,
        original_prompt: str,
        reference_responses: List[Dict[str, str]],
        timeout_seconds: float,
    ) -> str:
        moa_cfg = config.agent_moa
        aggregator_provider = moa_cfg.get("aggregator_provider", "default")

        ref_text = ""
        for i, ref in enumerate(reference_responses, 1):
            ref_text += f"\n--- 参考回答 {i} (来自 {ref.get('provider', 'unknown')}) ---\n{ref['response']}\n"

        aggregation_prompt = (
            f"原始问题:\n{original_prompt}\n\n"
            f"以下是多个AI模型的独立回答:\n{ref_text}\n\n"
            f"请综合以上回答，生成一个更完善的最终答案。"
        )

        adapter = self._router.get_provider(aggregator_provider)
        if not adapter:
            adapter = self._router.get_provider("default")
        if not adapter:
            return reference_responses[0]["response"] if reference_responses else ""

        try:
            kwargs = adapter.get_client_kwargs()
            model_name = adapter.get_model_name()
            client = OpenAI(**kwargs)

            loop = asyncio.get_running_loop()

            def _sync_aggregate():
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": _get_aggregation_prompt()},
                        {"role": "user", "content": aggregation_prompt},
                    ],
                    temperature=0.5,
                    max_tokens=moa_cfg.get("aggregator_max_tokens") or None,
                )
                if response.choices and response.choices[0].message.content:
                    return response.choices[0].message.content
                return ""

            result = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_aggregate),
                timeout=timeout_seconds,
            )
            return result or ""

        except Exception as e:
            logger.warning("MoA aggregation failed: %s, falling back to first reference", e)
            return reference_responses[0]["response"] if reference_responses else ""
