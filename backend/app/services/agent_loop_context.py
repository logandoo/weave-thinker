# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent-loop context helpers (P5.2 move-only, 2026-10-18).

Context window resolution, usage→context_info mapping and synthetic-directive
injection moved verbatim from ``agent_loop.py``; the loop re-exports the same
names so tests/monkeypatches keep working.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.config import get_config

config = get_config()


def _resolve_context_length(llm) -> int:
    """F5（2026-09-14）：端点声明的 context_window 优先；未声明回退全局配置
    （agent_compression_context_length，绝不猜小）。"""
    try:
        ep = getattr(llm, "endpoint", None)
        window = ep.context_window if ep is not None else None
        if window:
            return int(window)
    except Exception:
        pass
    return config.agent_compression_context_length

def _context_info_from_usage(usage: Dict[str, Any], context_window: int) -> Dict[str, Any]:
    """实测 provider usage → 前端 context_info（P4 真值徽章）。

    P1 (2026-10-18): also carries prompt id/version/hash + cache read/write
    counters.  ``context_info`` is persisted on the assistant message, so a
    later diagnosis can answer "which prompt version, how many cache tokens".
    """
    info: Dict[str, Any] = {
        "tokens": int(usage.get("prompt_tokens") or 0),
        "context_length": context_window,
        "measured": True,
    }
    for key in (
        "prompt_id", "prompt_id_source", "prompt_variant", "prompt_version", "prompt_hash",
        "prompt_release_hash", "system_tokens", "tools_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "model_alias",
        "model_name",
    ):
        value = usage.get(key)
        if value not in (None, ""):
            info[key] = value
    return info

def _inject_directive(state: "AgentLoopState", content: str, *, enabled: Optional[bool] = None, **extra: Any) -> None:
    """Inject an internal agent directive into the in-flight message list.

    4.8 synthetic part (opencode analogue): internal instructions (turn focus,
    audit guidance, nudges, deathmatch continuations, grace prompts) are NOT
    user input. When ``agent_synthetic_directives_enabled`` (default True),
    they enter the model context as role="system" messages flagged
    ``synthetic: True`` — keeping the user role for genuine user turns and
    letting history rebuilds skip them naturally. When disabled, falls back
    to the legacy role="user" injection byte-for-byte.
    """
    if enabled is None:
        enabled = bool(config.agent_synthetic_directives_enabled)
    if enabled:
        state.messages.append({"role": "system", "synthetic": True, "content": content, **extra})
    else:
        state.messages.append({"role": "user", "content": content, **extra})

def _truncate_marked(text: str, limit: int) -> str:
    """Explicitly-marked clip for strings entering the main loop context
    (audit problem/guidance): 未标注的静默截断被禁止（用户完整性原则）。"""
    t = text or ""
    if len(t) <= limit:
        return t
    return t[:limit] + "… [截断]"
