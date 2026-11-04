# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Runtime prompt observability (L4, plan 2026-10-18).

Every wire request gets ``prompt_id``, ``prompt_version``, ``prompt_hash`` and
stable/volatile token estimates.  The values travel on the existing ``usage``
event (no provider parameters), so traces/messages can answer "which prompt
version produced this answer?" without a new table.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from app.prompts.compiler import estimate_tokens
from app.prompts.registry import get

# Ordered, specific → generic.  Only used when the caller did not stamp the
# message explicitly; this is a routing aid, not a semantic classifier.
_MARKERS = (
    ("audit.response", "回答质量审计员"),
    ("audit.claim_verify", "事实核对员"),
    ("audit.scout", "证据侦察员"),
    ("coordinator.route", "智能路由协调器"),
    ("search.plan", "检索规划器"),
    ("deathmatch.judge", "严格的评判者"),
    ("deathmatch.verifier", "独立验证器"),
    ("deathmatch.plan", "任务规划器"),
    ("deathmatch.grilling", "盘问阶段"),
    ("title.generation", "对话标题生成器"),
    ("schedule.parse", "定时调度解析器"),
    ("error.classify", "错误分类器"),
    ("voice.duplex", "全双工语音"),
    ("memory.summary", "长期记忆"),
    ("memory.dream", "nightly dream"),
)


def _infer_with_source(messages: Iterable[Dict[str, Any]]) -> tuple[str, str]:
    """Return ``(prompt_id, source)`` with explicit > message > marker > fallback.

    ``unclassified`` is deliberate: silently attributing an unknown system
    prompt to ``agent.system`` makes traces lie about the release.
    """
    explicit = ""
    system_text = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system":
            system_text.append(str(message.get("content") or ""))
            explicit = str(message.get("prompt_id") or explicit)
    if explicit:
        return explicit, "message"
    text = "\n".join(system_text)
    for prompt_id, marker in _MARKERS:
        if marker in text:
            return prompt_id, "inferred"
    return "unclassified", "fallback"


def infer_prompt_id(messages: Iterable[Dict[str, Any]]) -> str:
    """Backward-compatible id-only projection (kept for callers/tests)."""
    return _infer_with_source(messages)[0]


def prompt_meta(
    messages: List[Dict[str, Any]],
    tools: Any,
    model: str,
    *,
    prompt_id: str = "",
    params: Optional[Dict[str, Any]] = None,
    model_alias: str = "",
    provider_type: str = "",
) -> Dict[str, Any]:
    system_text = "\n\n".join(
        str(m.get("content") or "") for m in messages
        if isinstance(m, dict) and m.get("role") == "system"
    )
    inferred_id, source = _infer_with_source(messages)
    resolved_id = str(prompt_id or "").strip()
    if resolved_id:
        source = "explicit"
    else:
        resolved_id = inferred_id
    spec = get(resolved_id)
    version = spec.version if spec is not None else "1.0.0"
    digest = hashlib.sha256(
        f"{resolved_id}@{version}\0{system_text}".encode("utf-8", errors="replace")
    ).hexdigest()
    tools_text = json.dumps(tools or [], ensure_ascii=False, sort_keys=True) if tools else ""
    # C-13: the release hash must cover every input that can change the
    # wire prompt: system text, tools schema, model identity, provider
    # identity, sampling params and the C-06 transform flag.  ``prompt_hash``
    # stays system-only for dashboards that compare prompt text across
    # releases; ``model_alias`` is the logical alias, ``model_name`` the wire
    # model (several aliases can share a model name).
    release_payload = json.dumps(
        {
            "prompt_id": resolved_id,
            "prompt_version": version,
            "system": system_text,
            "tools": tools or [],
            "model_alias": str(model_alias or model or ""),
            "model_name": model,
            "provider_type": str(provider_type or ""),
            "params": params or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    release_digest = hashlib.sha256(
        release_payload.encode("utf-8", errors="replace")
    ).hexdigest()
    variant = "compact" if "工具选择（参数以工具 schema 为准）" in system_text else "full"
    return {
        "prompt_id": resolved_id,
        "prompt_id_source": source,
        "prompt_variant": variant,
        "prompt_version": version,
        "prompt_hash": digest[:16],
        "prompt_release_hash": release_digest,
        "system_tokens": estimate_tokens(system_text),
        "tools_tokens": estimate_tokens(tools_text) if tools_text else 0,
        "model_alias": str(model_alias or model or ""),
        "model_name": model,
    }


_SECRET_RE = None


def redact_secrets(text: str) -> str:
    """Redact credential-looking tokens before persisting/printing evidence."""
    import re

    global _SECRET_RE
    if _SECRET_RE is None:
        _SECRET_RE = re.compile(
            r"(?i)(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}"
            r"|api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}"
            r"|access[_-]?token\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,})"
        )
    return _SECRET_RE.sub("[REDACTED]", str(text or ""))


def usage_cache_metrics(usage: Any) -> Dict[str, int]:
    """Normalise provider cache counters (OpenAI/DeepSeek/Anthropic shapes)."""
    hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    miss = int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    read = hit or cached or int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    out: Dict[str, int] = {}
    if read:
        out["cache_read_input_tokens"] = read
    if creation:
        out["cache_creation_input_tokens"] = creation
    if hit or miss:
        out["prompt_cache_hit_tokens"] = hit
        out["prompt_cache_miss_tokens"] = miss
    return out
