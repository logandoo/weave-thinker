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

    Args:
        kind: One of "concept_extraction", "dream", "clarification",
              "query_expansion", "migration", "consolidation".
    """
    from app.core.config import get_config
    from app.services.llm_service import LLMService

    config = get_config()
    cfg_key = _CONFIG_KEY_MAP.get(kind)
    if not cfg_key:
        logger.warning("Unknown memory LLM kind %r, using default provider", kind)
        return LLMService()

    provider_name = config.memory.get(cfg_key, "default")
    if not provider_name or provider_name == "default":
        return LLMService()

    try:
        from app.services.provider_router import get_provider_router
        router = get_provider_router()
        kwargs = router.get_client_kwargs(provider_name)
        model_name = router.get_model_name(provider_name)
        return LLMService(
            custom_api_url=kwargs.get("base_url"),
            custom_api_key=kwargs.get("api_key"),
            custom_model_name=model_name,
        )
    except Exception as e:
        logger.warning(
            "Failed to resolve memory provider %r for kind %r, falling back to default: %s",
            provider_name, kind, e,
        )
        return LLMService()
