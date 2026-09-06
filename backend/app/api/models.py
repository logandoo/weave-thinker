# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""GET /api/models — 模型逻辑别名列表（模型配置解耦，2026-08-30）。

前端能看到的只有别名与能力描述；供应商 base_url / api_key / 真实
model_name 统一由后端 config_model.toml 管理，永不进入响应。
"""
from fastapi import APIRouter, Depends

from app.core.deps import get_current_user
from app.db.database import User
from app.model_gateway.registry import get_model_registry

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
async def list_models(current_user: User = Depends(get_current_user)):
    registry = get_model_registry()
    return {
        "aliases": registry.public_aliases(),
        "default_alias": registry.default_alias(),
    }
