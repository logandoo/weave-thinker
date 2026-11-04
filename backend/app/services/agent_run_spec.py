# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Shared execution spec for chat and background agent runs (voice delegation §8).

This module is the single source of truth for:
- strict-highest reasoning resolution from the *effective* endpoint capabilities
- non-secret endpoint fingerprints (never contain URLs or API keys)
- canonical request fingerprints used for delegation idempotency
- the versioned Execution Manifest frozen at submission time

No credentials may leave this module: the manifest carries only non-secret
configuration, and credentials are re-read from the live registry at run time.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from app.model_gateway.schemas import ModelEndpoint

REASONING_POLICY_STRICT = "strict_highest"
REASONING_POLICY_DISABLED = "disabled"

_HIGHEST_FIRST_FALLBACK = ("max", "xhigh", "high", "medium", "low")
_EFFORT_DEPTH = {"max": 5, "xhigh": 4, "high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class ReasoningResolution:
    """Resolved reasoning contract for the *main agent* model."""

    policy: str
    supported: bool
    enabled: bool
    effort: Optional[str]
    thinking_budget: Optional[int] = None
    scope: str = "main_agent"

    def as_manifest(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "enabled": bool(self.enabled),
            "effort": self.effort,
            "thinking_budget": self.thinking_budget,
            "scope": self.scope,
            "supported": bool(self.supported),
        }


def _capability_efforts(endpoint: ModelEndpoint) -> list[str]:
    caps = endpoint.capabilities or {}
    raw = caps.get("reasoning_efforts")
    if isinstance(raw, (list, tuple)) and raw:
        return [str(e) for e in raw if e]
    # Fall back to the provider's thinking profile (wire-legal set). The
    # registry normally materialises capabilities already; this keeps custom
    # inline endpoints (legacy:*) honest too.
    try:
        from app.model_gateway.profiles import get_thinking_profile

        efforts = get_thinking_profile(endpoint.provider_type or "").efforts
        if efforts:
            return [str(e) for e in efforts]
    except Exception:
        pass
    return []


def resolve_highest_reasoning(endpoint: ModelEndpoint) -> ReasoningResolution:
    """Resolve the highest legal reasoning level for *this* endpoint.

    Ordering contract: `capabilities.reasoning_efforts` is the UI/wire order
    (DeepSeek max>high>low · Qwen3.8 xhigh>medium>low · Doubao high>medium>low),
    so element 0 is the highest. A toggle-only endpoint resolves to
    enabled=True/effort=None. An endpoint with no reasoning capability resolves
    to supported=False — strict submission must reject it.
    """
    caps = endpoint.capabilities or {}
    efforts = _capability_efforts(endpoint)
    supports = bool(caps.get("supports_reasoning"))
    if not supports:
        # A provider profile with a legal effort set is evidence of support even
        # when a hand-written toml endpoint omitted the capability block.
        supports = bool(efforts)

    if not supports:
        return ReasoningResolution(
            policy=REASONING_POLICY_DISABLED,
            supported=False,
            enabled=False,
            effort=None,
        )

    effort = None
    if efforts:
        # Depth order, never tuple order: DeepSeekProfile.efforts is ascending
        # ("low","high","max") while Qwen/Doubao are descending. Taking
        # efforts[0] would silently pick "low" on a capability-less DeepSeek
        # endpoint and still label it strict-highest.
        legal = [e for e in efforts if e in _EFFORT_DEPTH]
        effort = max(legal, key=lambda e: _EFFORT_DEPTH[e]) if legal else None
    # Guard against a stale config that lists nothing recognisable: still a
    # legal "thinking on" request, never an invented numeric budget.
    return ReasoningResolution(
        policy=REASONING_POLICY_STRICT,
        supported=True,
        enabled=True,
        effort=effort,
    )


def endpoint_fingerprint(endpoint: ModelEndpoint) -> str:
    """Stable non-secret fingerprint of the effective endpoint.

    Hashed inputs include provider/model/params/capabilities and the URL *host*
    only — never the full URL, path, query or API key.
    """
    host = ""
    try:
        host = urlsplit(endpoint.base_url or "").netloc.lower()
    except Exception:
        host = ""
    caps = endpoint.capabilities or {}
    payload = {
        "alias": endpoint.alias,
        "provider_type": endpoint.provider_type,
        "model_name": endpoint.model_name,
        "host": host,
        "params": endpoint.params or {},
        "user_params": (endpoint.extra or {}).get("user_params") or {},
        "supports_reasoning": bool(caps.get("supports_reasoning")),
        "reasoning_efforts": list(caps.get("reasoning_efforts") or []),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalise_goal(goal: str) -> str:
    return " ".join(str(goal or "").split())


def canonical_request_fingerprint(
    *,
    goal: str,
    target_assistant_id: str,
    reasoning_policy: str,
    reasoning_effort: Optional[str],
    source_conversation_id: Optional[str],
    source_message_id: Optional[str],
) -> str:
    """Fingerprint of the normalised request + stable execution policy.

    Explicitly excludes T/C2 ids, timestamps and server-derived workspace
    paths, so retrying the same request yields the same fingerprint.
    """
    payload = {
        "goal": _normalise_goal(goal),
        "target_assistant_id": str(target_assistant_id or ""),
        "reasoning_policy": str(reasoning_policy or ""),
        "reasoning_effort": str(reasoning_effort or ""),
        "source_conversation_id": str(source_conversation_id or ""),
        "source_message_id": str(source_message_id or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_execution_manifest(
    *,
    assistant: Any,
    endpoint: ModelEndpoint,
    reasoning: ReasoningResolution,
    source_conversation_id: Optional[str],
    source_message_id: Optional[str],
    context_cutoff: str,
    workspace_task_root: str,
    iterations_max: int,
    wall_time_seconds: int,
) -> Dict[str, Any]:
    """Freeze a non-secret, versioned execution configuration (§6.2)."""
    sampling = {}
    for key in ("temperature", "top_p", "top_k", "presence_penalty",
                "frequency_penalty", "max_tokens", "min_p", "repetition_penalty"):
        value = getattr(assistant, key, None)
        if value is not None:
            sampling[key] = value
    return {
        "version": 1,
        "assistant": {
            "id": str(getattr(assistant, "id", "") or ""),
            "name": str(getattr(assistant, "name", "") or ""),
            "system_prompt": str(getattr(assistant, "system_prompt", "") or ""),
            "model_alias": str(getattr(assistant, "model_alias", "") or "") or None,
            "subtask_model_alias": str(getattr(assistant, "subtask_model_alias", "") or "") or None,
            "sampling": sampling,
            "citation_style": str(getattr(assistant, "citation_style", "") or "") or None,
        },
        "endpoint": {
            "alias": endpoint.alias,
            "provider_type": endpoint.provider_type or "",
            "model_name": endpoint.model_name or "",
            "nonsecret_fingerprint": endpoint_fingerprint(endpoint),
        },
        "reasoning": reasoning.as_manifest(),
        "limits": {
            "iterations_max": int(iterations_max),
            "wall_time_seconds": int(wall_time_seconds),
            "queue_policy": "per_user_bounded",
        },
        "source": {
            "conversation_id": source_conversation_id,
            "message_id": source_message_id,
            "context_cutoff": context_cutoff,
        },
        "workspace": {
            "scratch": f"{workspace_task_root}/scratch",
            "artifacts": f"{workspace_task_root}/artifacts",
        },
    }


def manifest_hash(manifest: Dict[str, Any]) -> str:
    raw = json.dumps(manifest or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def should_mark_reasoning_degraded(
    *,
    enabled: bool,
    use_thinking: bool,
    retry_thinking_off: bool,
    revision_thinking_off: bool,
    main_serves_iterations: bool,
) -> bool:
    """True only when the MAIN model's user-visible answer ran without thinking.

    A dedicated subtask client is allowed to stay non-thinking (§8.1); its
    retry/revision must not fail a strict main-model task.
    """
    return bool(
        enabled
        and not use_thinking
        and (retry_thinking_off or revision_thinking_off)
        and main_serves_iterations
    )
