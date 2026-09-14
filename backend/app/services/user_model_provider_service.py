# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""用户级模型供应商覆盖 — 服务层（DB 读写 / 校验 / 序列化 / 缓存 / 上下文注入）。

设计：design/BACKEND_DESIGN_user_settings.html §2-§4；计划 docs/PLAN.md Task 3。
- `load_user_overrides`：读 user_model_providers → {kind: override dict}，30s TTL 进程缓存。
- `activate_user_overrides`：load + 写入 model_gateway.user_overrides 的 ContextVar。
- `parse_overrides_payload`：PUT 载荷校验/归一化（未知 kind、非法 URL、参数越域 → ValueError）。
- `serialize_status`：GET 响应（Key 仅回掩码尾 4 位，绝不回明文）。
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import UserModelProvider
from app.model_gateway.user_overrides import set_active_user_overrides

logger = logging.getLogger(__name__)

SUPPORTED_KINDS: List[str] = ["llm", "vlm", "embedding", "rerank", "asr", "tts"]
PARAM_KEYS = (
    "temperature", "top_p", "top_k", "max_tokens", "presence_penalty", "frequency_penalty",
)
# (min, max) 闭区间；top_k/max_tokens 额外要求整数
_PARAM_RANGES = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "top_k": (0, 10_000_000),
    "max_tokens": (0, 10_000_000),
    "presence_penalty": (-2.0, 2.0),
    "frequency_penalty": (-2.0, 2.0),
}
_INT_PARAMS = {"top_k", "max_tokens"}

_CACHE_TTL_SECONDS = 30.0
_cache: Dict[str, tuple] = {}  # user_id -> (expires_at, overrides dict)


async def _fetch_rows(db: AsyncSession, user_id: str) -> list:
    result = await db.execute(
        select(UserModelProvider).where(UserModelProvider.user_id == user_id)
    )
    return list(result.scalars().all())


async def list_user_provider_rows(db: AsyncSession, user_id: str) -> list:
    """API 读取用（直查 DB，不走缓存）。"""
    return await _fetch_rows(db, user_id)


def build_overrides_from_rows(rows: list) -> Dict[str, Dict[str, Any]]:
    """DB 行 → {kind: {enabled, base_url, api_key, model_name, params}}（纯函数）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        kind = str(getattr(row, "kind", "") or "")
        if kind not in SUPPORTED_KINDS:
            continue
        try:
            params = json.loads(getattr(row, "params_json", None) or "{}")
            if not isinstance(params, dict):
                params = {}
        except (json.JSONDecodeError, TypeError):
            params = {}
        out[kind] = {
            "enabled": bool(getattr(row, "enabled", True)),
            "base_url": str(getattr(row, "base_url", None) or ""),
            "api_key": str(getattr(row, "api_key", None) or ""),
            "model_name": str(getattr(row, "model_name", None) or ""),
            "params": params,
        }
    return out


def invalidate_user_overrides(user_id: str) -> None:
    _cache.pop(str(user_id), None)


async def load_user_overrides(db: AsyncSession, user_id: str) -> Dict[str, Dict[str, Any]]:
    """读取用户覆盖（TTL 缓存；保存路径必须调用 invalidate）。"""
    key = str(user_id)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    rows = await _fetch_rows(db, user_id)
    overrides = build_overrides_from_rows(rows)
    _cache[key] = (now + _CACHE_TTL_SECONDS, overrides)
    # A4.9 Minor 修复（2026-09-13）：容量上限（防长期运行用户数增长无界）
    while len(_cache) > 256:
        _cache.pop(next(iter(_cache)), None)
    return overrides


async def activate_user_overrides(db: AsyncSession, user_id: str) -> None:
    """加载并写入当前任务上下文的覆盖（请求/后台任务入口调用）。"""
    overrides = await load_user_overrides(db, user_id)
    set_active_user_overrides(overrides or None)


def _validate_params(params: Any) -> Dict[str, Optional[float]]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    out: Dict[str, Optional[float]] = {}
    for k, v in params.items():
        if k not in PARAM_KEYS:
            raise ValueError(f"unknown param: {k}")
        if v is None:
            out[k] = None
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"param {k} must be a number or null")
        lo, hi = _PARAM_RANGES[k]
        if v < lo or v > hi:
            raise ValueError(f"param {k} out of range [{lo}, {hi}]: {v}")
        if k in _INT_PARAMS and float(v) != int(v):
            raise ValueError(f"param {k} must be an integer")
        out[k] = v
    return out


def _validate_common(kind: str, cfg: dict) -> dict:
    base_url = cfg.get("base_url")
    if base_url is None:
        base_url = ""
    if not isinstance(base_url, str) or len(base_url) > 500:
        raise ValueError(f"[{kind}] base_url must be a string <= 500 chars")
    base_url = base_url.strip()
    if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError(f"[{kind}] base_url must start with http:// or https://")

    model_name = cfg.get("model_name")
    if model_name is None:
        model_name = ""
    if not isinstance(model_name, str) or len(model_name) > 200:
        raise ValueError(f"[{kind}] model_name must be a string <= 200 chars")

    api_key = cfg.get("api_key", None)
    if api_key is not None:
        if not isinstance(api_key, str) or len(api_key) > 500:
            raise ValueError(f"[{kind}] api_key must be a string <= 500 chars")

    enabled = cfg.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"[{kind}] enabled must be a boolean")

    return {
        "enabled": enabled,
        "base_url": base_url,
        "api_key": api_key,  # None = 保持原值；"" = 显式清除；str = 设置
        "model_name": model_name.strip(),
        "params": _validate_params(cfg.get("params")),
    }


def parse_overrides_payload(payload: Any) -> Dict[str, Optional[dict]]:
    """PUT 载荷 → {kind: normalized | None}。None = 删除该 kind 的覆盖。"""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    out: Dict[str, Optional[dict]] = {}
    for kind, cfg in payload.items():
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"unknown kind: {kind}")
        if cfg is None:
            out[kind] = None
            continue
        if not isinstance(cfg, dict):
            raise ValueError(f"[{kind}] config must be an object")
        out[kind] = _validate_common(kind, cfg)
    return out


def serialize_status(rows: list) -> dict:
    """GET 响应：Key 仅回掩码尾 4 位；params 补全白名单键（未设置为 null）。"""
    by_kind = {str(getattr(r, "kind", "")): r for r in rows}
    overrides: Dict[str, Optional[dict]] = {}
    for kind in SUPPORTED_KINDS:
        row = by_kind.get(kind)
        if row is None:
            overrides[kind] = None
            continue
        raw_key = str(getattr(row, "api_key", None) or "")
        try:
            params = json.loads(getattr(row, "params_json", None) or "{}")
            if not isinstance(params, dict):
                params = {}
        except (json.JSONDecodeError, TypeError):
            params = {}
        # A4.9 Minor 修复：≤4 字符 Key 不整串回显（掩码 ****）
        if not raw_key:
            tail = ""
        elif len(raw_key) > 4:
            tail = raw_key[-4:]
        else:
            tail = "****"
        overrides[kind] = {
            "enabled": bool(getattr(row, "enabled", True)),
            "base_url": str(getattr(row, "base_url", None) or ""),
            "model_name": str(getattr(row, "model_name", None) or ""),
            "has_api_key": bool(raw_key),
            "api_key_tail": tail,
            "params": {k: params.get(k) for k in PARAM_KEYS},
        }
    return {"kinds": SUPPORTED_KINDS, "overrides": overrides}
