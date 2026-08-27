# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter
from app.core.config import get_config

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/providers")
async def get_provider_configs():
    """Return provider configurations (without sensitive API keys)."""
    config = get_config()
    providers = {}
    for name, cfg in config.provider_configs.items():
        providers[name] = {
            "base_url": cfg.get("base_url", ""),
            "model_name": cfg.get("model_name", ""),
        }
    return {
        "providers": providers,
        "default_provider": "deepseek",
    }
