# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""用户级模型供应商覆盖 — 运行时 ContextVar 覆盖层（2026-09-13）。

背景：用户可在「系统设置 → 模型供应商」为自己的账号覆盖系统各模型类型的
URL / API Key / model_name / 采样参数（design/FLOW_DESIGN_user_settings_20260913.html）。
覆盖仅在该用户的任务上下文内生效：请求入口（HTTP/WS 鉴权）与后台任务入口
（worker/scheduler/memory_scheduler per-user）调用 `set_active_user_overrides`，
`ModelRegistry` 的 get/resolve/endpoint_for_assistant 三个出口统一经过
`apply_user_override`。

语义（用户 Gate A 决策）：
- 覆盖所有模型类型（llm/vlm/embedding/rerank/asr/tts）；VLM 端点在注册表中
  kind=llm，按别名（vlm/deathmatch.vlm）路由到 "vlm" 覆盖键，与 llm 隔离。
- 自定义 base_url 时 API Key 一律按用户填写发送；缺省则发空（消费层
  "no-key" 哨兵），**绝不回落系统 Key**。
- 仅覆盖 model/params（无自定义 URL）时沿用系统 URL/Key。
- 留空 / enabled=false / 无覆盖 → 原端点原样返回（零行为差异）。
"""
import logging
from contextvars import ContextVar
from typing import Any, Dict, Optional

from app.model_gateway.schemas import KIND_ASR, KIND_LLM, ModelEndpoint

logger = logging.getLogger(__name__)

_VLM_ALIASES = ("vlm", "deathmatch.vlm")

_ACTIVE: ContextVar[Optional[Dict[str, Dict[str, Any]]]] = ContextVar(
    "active_user_model_overrides", default=None,
)


def set_active_user_overrides(overrides: Optional[Dict[str, Dict[str, Any]]]) -> None:
    _ACTIVE.set(overrides or None)


def reset_active_user_overrides() -> None:
    _ACTIVE.set(None)


def get_active_user_overrides() -> Optional[Dict[str, Dict[str, Any]]]:
    return _ACTIVE.get()


def override_key_for(ep: ModelEndpoint) -> str:
    """端点 → 覆盖键。VLM 在注册表中 kind=llm（purpose 端点），按别名归 "vlm"。"""
    if ep.kind == KIND_LLM and ep.alias in _VLM_ALIASES:
        return "vlm"
    return ep.kind


def apply_user_override(ep: ModelEndpoint) -> ModelEndpoint:
    """按当前任务上下文的用户覆盖改写端点；无覆盖/空覆盖 → 原样返回。"""
    active = _ACTIVE.get()
    if not active:
        return ep
    ov = active.get(override_key_for(ep))
    if not ov or not ov.get("enabled", True):
        return ep

    base_url = str(ov.get("base_url") or "")
    model_name = str(ov.get("model_name") or "")
    params = {k: v for k, v in (ov.get("params") or {}).items() if v is not None}
    if not base_url and not model_name and not params:
        return ep

    # 自定义 URL → 用户 Key 或空（消费层转 "no-key"，绝不发系统 Key）；
    # 无自定义 URL → 沿用系统 Key。
    api_key = str(ov.get("api_key") or "") if base_url else ep.api_key
    merged_params = {**ep.params, **params}
    # 用户显式参数单列 extra.user_params：llm_service._build_params 将其注入
    # kwargs（最高优先级）——否则主回复等调用点硬编码的 temperature 会吞掉用户设置。
    extra = {**ep.extra, "user_params": dict(params)} if params else ep.extra
    provider_type = ep.provider_type
    if base_url and ep.kind == KIND_ASR:
        # A4.9 Critical/Important 修复（2026-09-13）：自定义 ASR URL = 通用 HTTP
        # 引擎语义。dashscope/mimo 引擎忽略 base_url 且经 extra.dashscope_api_key
        # 旁路取系统 Key（转写 + 热词词表同步）→ 强制 general 并清空 dashscope
        # 专属键，保证系统 Key 绝不随自定义 URL 发出。
        provider_type = "general"
        extra = {
            **extra,
            "is_dashscope": False,
            "is_mimo": False,
            "dashscope_api_key": "",
            "dashscope_model": "",
            "dashscope_vocabulary_url": "",
        }
    return ep.with_overrides(
        base_url=base_url or ep.base_url,
        api_key=api_key,
        model_name=model_name or ep.model_name,
        params=merged_params,
        extra=extra,
        provider_type=provider_type,
        # 覆盖后的端点必须走"显式端点"构造语义，params 才会在 wire 生效
        # （llm_service 仅对 is_custom_provider 应用端点 params）。
        is_custom=True if (base_url or params) else ep.is_custom,
    )
