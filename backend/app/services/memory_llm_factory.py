# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
Per-feature model routing for memory subsystem (M&D §6.2).

Each memory feature (extraction, dreaming, clarification, query_expansion)
can be routed to a different LLM provider via config keys like:

  [memory]
  concept_extraction_model = "default"   # → main [api] provider
  dream_model = "mimo"                   # → [providers.mimo] provider

"default" (or empty) uses the main [api] provider (bare LLMService()).
Any other value is looked up in [providers.<name>] and passed as
custom_api_url / custom_api_key / custom_model_name to LLMService.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_KEY_MAP = {
    "concept_extraction": "concept_extraction_model",
    "dream": "dream_model",
    "clarification": "clarification_model",
    "query_expansion": "query_expansion_model",
    "migration": "concept_extraction_model",
    "consolidation": "dream_model",
}


def _memory_llm(kind: str):
    """Return an LLMService instance routed to the provider configured for *kind*.

    model_gateway 收口（2026-08-30）：kind → purpose "memory.<kind>" →
    registry routing（[memory] *_model 键 → provider 别名，"default"/空 → main）。
    与 legacy 的 provider_router 解析语义一致。

    Args:
        kind: One of "concept_extraction", "dream", "clarification",
              "query_expansion", "migration", "consolidation".
    """
    from app.model_gateway import factory
    from app.model_gateway.registry import get_model_registry

    if kind not in _CONFIG_KEY_MAP:
        logger.warning("Unknown memory LLM kind %r, using default provider", kind)
        return factory.build_llm_service(get_model_registry().get("main"))
    return factory.build_llm_service(get_model_registry().resolve(f"memory.{kind}"))
