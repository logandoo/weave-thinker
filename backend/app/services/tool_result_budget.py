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


_UNLIMITED_TOOLS = frozenset({"memory", "memory_search", "session_search", "workspace_read", "word_count", "workspace_glob", "provide_file", "grep", "diff", "workspace_write", "workspace_edit", "workspace_snapshot"})


def _is_inline_image_message(msg: Dict) -> bool:
    """P0.5：只有产图白名单工具的**可解析**信封才豁免字符预算。

    shape-only 的 substring 命中不够——伪造/畸形 `_image_result` 的
    terminal/mcp 结果仍按普通文本处理（防绕过）。
    """
    from app.services.tool_image_results import (
        IMAGE_TOOL_ALLOWLIST,
        parse_image_envelope_for_message,
    )

    name = str(msg.get("name") or "")
    return name in IMAGE_TOOL_ALLOWLIST and parse_image_envelope_for_message(msg) is not None


async def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    config: BudgetConfig = DEFAULT_BUDGET,
    workspace_path: str = "",
    force: bool = False,
    msg: Optional[Dict] = None,
) -> str:
    if not content:
        return content
    # P0.5：base64 图片信封不是"大文本"——外置成 .txt 会丢图且破坏 JSON。
    # 只豁免可解析的真信封；构造期已受 MAX_INLINE_BYTES 限制，请求侧旧帧
    # 由 expand_tool_images（wire 前）与 context_compressor 双重降级。
    # P0（2026-11-09）：调用方传了 msg 就走按消息身份的记忆化解析，避免同一
    # 轮里对同一封反复 json.loads 全量 base64。
    from app.services.tool_image_results import (
        IMAGE_TOOL_ALLOWLIST,
        parse_image_envelope_memoized,
        parse_image_envelope_for_message,
    )

    if tool_name in IMAGE_TOOL_ALLOWLIST:
        # 没有 msg 也要走记忆化：`agent_loop._apply_tool_result_budget` 这个**每个
        # 工具结果都要过**的调用点就不传 msg（A4.9 二轮评审 A·MISSED-CONSUMERS）。
        # 键是载荷字符串本身的对象身份，而这里传进来的正是长期存活的那个 result
        # 对象，命中率与按消息缓存等价。（对比：history_window 的多 part 分支是
        # 现拼的临时串，记忆化只会往里塞垃圾条目，所以那里保持裸解析。）
        payload = (
            parse_image_envelope_for_message(msg)
            if msg is not None
            else parse_image_envelope_memoized(content)
        )
        if payload is not None:
            return content
    if len(content) <= config.max_result_size_chars:
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

    # M5：引用台账 manifest 是 system 消息，结构化地不进入本函数（本函数只
    # 处理 role=tool 消息）；真实折叠保护在 context_compressor 摘出/挂回 +
    # agent_loop._refresh_citation_manifest 重注入（见 test_citation_manifest_*）。

    # P0.5：图片信封不计入字符预算——base64 是二进制载体，不是可外置文本。
    def _budget_chars(msg: Dict) -> int:
        if _is_inline_image_message(msg):
            return 0
        return len(msg.get("content", ""))

    total_chars = sum(_budget_chars(m) for m in tool_messages)
    if total_chars <= config.turn_budget_chars:
        return tool_messages

    indexed = [(i, _budget_chars(m), m) for i, m in enumerate(tool_messages)]
    non_persisted = [
        (i, size, m)
        for i, size, m in indexed
        if size > 0 and "<persisted-output>" not in m.get("content", "")
    ]
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
            msg=msg,
        )
        reduction = len(content) - len(persisted)
        current_total -= reduction
        msg["content"] = persisted

    if current_total > config.turn_budget_chars:
        for msg in tool_messages:
            content = msg.get("content", "")
            # P0.5：图片信封永不走兜底外置（force 也不行）——外置即丢图。
            if _is_inline_image_message(msg):
                continue
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
                    msg=msg,
                )
                msg["content"] = persisted if persisted != content else (
                    content + "\n\n[...output 超预算且存档未成功；已保留全文...]")

    return tool_messages
