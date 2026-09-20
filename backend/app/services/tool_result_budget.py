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


_UNLIMITED_TOOLS = frozenset({"memory", "session_search", "workspace_read", "word_count", "workspace_glob", "provide_file", "grep", "diff", "workspace_write", "workspace_edit", "workspace_snapshot"})


async def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    config: BudgetConfig = DEFAULT_BUDGET,
    workspace_path: str = "",
    force: bool = False,
) -> str:
    if not content or len(content) <= config.max_result_size_chars:
        return content
    if tool_name in _UNLIMITED_TOOLS and not force:
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
    except Exception as exc:
        logger.exception("Failed to persist tool result for %s", tool_name)
        # 2026-09-20 审计完整性（A4.9 r2 Important-2）：存档失败绝不丢弃内容。
        # 旧实现按 max_result_size_chars 截断——调用方传 0 时整段内容被换成一
        # 行提示（静默数据丢失，比截断更糟）。原则：保留全文 + 显式警示。
        return content + (
            f"\n\n[存档失败：无法写入 {fpath}（{exc}）；已保留全文，注意上下文体积]"
        )

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
            # A4.9 residual：兜底不再裸字符截断——先强制存档（含 _UNLIMITED_TOOLS），
            # 有指针可回读；存档失败时保留全文 + 标记（完整性优先，绝不静默丢内容）。
            if len(content) > config.max_result_size_chars and config.max_result_size_chars > 0:
                persisted = await maybe_persist_tool_result(
                    content,
                    tool_name=msg.get("name", "unknown_tool"),
                    tool_use_id=msg.get("tool_call_id", _uuid.uuid4().hex[:12]),
                    config=config,
                    workspace_path=workspace_path,
                    force=True,
                )
                msg["content"] = persisted if persisted != content else (
                    content + "\n\n[...output 超预算且存档未成功；已保留全文...]")

    return tool_messages
