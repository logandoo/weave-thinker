# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESULT_CHARS = 100_000
_DEFAULT_TURN_BUDGET_CHARS = 200_000
_DEFAULT_PREVIEW_CHARS = 1_500
_PERSIST_DIR_NAME = "tool_results"


@dataclass
class BudgetConfig:
    max_result_size_chars: int = _DEFAULT_MAX_RESULT_CHARS
    turn_budget_chars: int = _DEFAULT_TURN_BUDGET_CHARS
    preview_chars: int = _DEFAULT_PREVIEW_CHARS
    persist_dir: Optional[str] = None


DEFAULT_BUDGET = BudgetConfig()


_UNLIMITED_TOOLS = frozenset({"memory", "session_search", "workspace_read", "word_count", "workspace_glob", "provide_file", "grep", "diff"})


async def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    config: BudgetConfig = DEFAULT_BUDGET,
    workspace_path: str = "",
) -> str:
    if not content or len(content) <= config.max_result_size_chars:
        return content
    if tool_name in _UNLIMITED_TOOLS:
        return content

    persist_dir = config.persist_dir
    if not persist_dir and workspace_path:
        persist_dir = os.path.join(workspace_path, _PERSIST_DIR_NAME)
    if not persist_dir:
        from app.core.config import get_config as _get_config
        _cfg = _get_config()
        persist_dir = os.path.join(str(_cfg.project_root), "backend", "output_files", _PERSIST_DIR_NAME)

    await asyncio.to_thread(os.makedirs, persist_dir, exist_ok=True)

    fname = f"{tool_use_id or _uuid.uuid4().hex[:12]}.txt"
    fpath = os.path.join(persist_dir, fname)
    try:
        await asyncio.to_thread(_write_persisted_file, fpath, content)
    except Exception:
        logger.exception("Failed to persist tool result for %s", tool_name)
        if len(content) > config.max_result_size_chars:
            return content[:config.max_result_size_chars] + "\n\n[...output truncated due to size...]"
        return content

    size_str = f"{len(content):,} characters"
    if len(content) > 1024:
        size_str = f"{len(content) / 1024:.1f} KB"

    preview = content[:config.preview_chars]
    if len(content) > config.preview_chars:
        preview += "\n..."

    return (
        f"<persisted-output>\n"
        f"This tool result was too large ({size_str}).\n"
        f"Full output saved to: {fpath}\n"
        f"Use the file reading capabilities to access specific sections if needed.\n\n"
        f"Preview (first {config.preview_chars} chars):\n"
        f"{preview}\n"
        f"</persisted-output>"
    )


def _write_persisted_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


async def enforce_turn_budget(
    tool_messages: List[Dict],
    config: BudgetConfig = DEFAULT_BUDGET,
    workspace_path: str = "",
) -> List[Dict]:
    if not tool_messages:
        return tool_messages

    total_chars = sum(len(m.get("content", "")) for m in tool_messages)
    if total_chars <= config.turn_budget_chars:
        return tool_messages

    indexed = [(i, len(m.get("content", "")), m) for i, m in enumerate(tool_messages)]
    non_persisted = [(i, size, m) for i, size, m in indexed if "<persisted-output>" not in m.get("content", "")]
    non_persisted.sort(key=lambda x: x[1], reverse=True)

    current_total = total_chars
    for idx, size, msg in non_persisted:
        if current_total <= config.turn_budget_chars:
            break
        content = msg.get("content", "")
        persisted = await maybe_persist_tool_result(
            content,
            tool_name=msg.get("name", "unknown_tool"),
            tool_use_id=msg.get("tool_call_id", _uuid.uuid4().hex[:12]),
            config=config,
            workspace_path=workspace_path,
        )
        reduction = len(content) - len(persisted)
        current_total -= reduction
        msg["content"] = persisted

    if current_total > config.turn_budget_chars:
        for msg in tool_messages:
            content = msg.get("content", "")
            if len(content) > config.max_result_size_chars:
                msg["content"] = content[:config.max_result_size_chars] + "\n\n[...output truncated to fit context...]"

    return tool_messages
