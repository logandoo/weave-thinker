# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Post-tool guardrails — synchronous, pure-function evaluators.

These run inside the AgentLoop right after a tool returns. They MUST stay
sync and IO-free so they never introduce asyncio contagion into the loop.

Currently only one rule:
  - When ``schedule(action="create")`` succeeds, halt the loop and surface
    the success message as the final assistant content. This prevents the
    LLM from continuing to ``web_search`` / ``browser`` and effectively
    pre-executing the scheduled job body in the current turn.

Hermes-agent reference: ``hermes-agent/agent/tool_guardrails.py``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal, Optional

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    action: Literal["continue", "halt"]
    final_message: Optional[str] = None


_CONTINUE = Decision(action="continue")


def evaluate_post_tool(tool_name: str, raw_result: str) -> Decision:
    """Inspect a tool result. Return halt+message to short-circuit the loop.

    Pure function. Never raises. Unknown tools / unparseable results / any
    unexpected shape fall through to ``continue`` (i.e. preserves existing
    behaviour).
    """
    if tool_name != "schedule":
        return _CONTINUE

    try:
        parsed = json.loads(raw_result)
    except (TypeError, ValueError):
        return _CONTINUE

    if not isinstance(parsed, dict):
        return _CONTINUE
    if not parsed.get("success"):
        return _CONTINUE
    if parsed.get("action") != "create":
        return _CONTINUE

    message = parsed.get("message") or "已创建定时任务。"
    logger.info(
        "tool_guardrails: halting loop after schedule.create success task_id=%s",
        parsed.get("task_id"),
    )
    return Decision(action="halt", final_message=str(message))
