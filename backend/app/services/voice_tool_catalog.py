# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice tool visibility + human-readable tool names.

Extracted from `voice_service` to keep the host module's move-only line budget.
"""
from __future__ import annotations

# Tools the voice agent is allowed to call. Voice mode supports the same
# toolset as Agent (chat) mode so users can accomplish real tasks by voice.
# MCP tools are NOT listed here — they are merged in dynamically at assembly
# time by `_voice_tool_names()` so any registered mcp-* toolset is voice-visible.
VOICE_TOOLS = {
    "web_search",
    "browser",
    "execute_code",
    "memory",
    "notes",
    "pdf_export",
    "context7_resolve_library_id",
    "context7_query_docs",
    "terminal",
    "skill_view",
    "delegate_agent_task",
    "list_agent_tasks",
    "get_agent_task",
    "cancel_agent_task",
    "steer_agent_task",
    "summarize_agent_task",
    "open_agent_task_result",
    "session_search",
}

# Human-readable voice-mode tool names: (zh, en) pairs used for TTS
# announcements ("我来调用联网查询工具…") and the UI tool-call block titles.
# Raw snake_case names must never be spoken to the user. Unknown tools
# (e.g. dynamically registered MCP tools) are prettified on the fly by
# `_voice_tool_display_name()`.
VOICE_TOOL_DISPLAY_NAMES: dict[str, tuple[str, str]] = {
    "web_search": ("联网查询", "web search"),
    "browser": ("浏览网页", "browse the web"),
    "execute_code": ("代码执行", "run code"),
    "memory": ("记忆操作", "memory"),
    "notes": ("笔记操作", "notes"),
    "pdf_export": ("导出 PDF", "export PDF"),
    "terminal": ("终端命令", "terminal"),
    "context7_resolve_library_id": ("查找库文档 ID", "library docs lookup"),
    "context7_query_docs": ("查询库文档", "query library docs"),
    "skill_view": ("查看技能", "view skills"),
    "background_task": ("后台任务", "background task"),
    "delegate_agent_task": ("委派执行助手", "delegate task"),
    "list_agent_tasks": ("任务列表", "list tasks"),
    "get_agent_task": ("查询任务", "check task"),
    "cancel_agent_task": ("取消任务", "cancel task"),
    "steer_agent_task": ("补充任务要求", "steer task"),
    "summarize_agent_task": ("转述任务结果", "summarize task"),
    "open_agent_task_result": ("打开任务结果", "open task result"),
    "session_search": ("历史会话检索", "search conversation history"),
}


def voice_tool_names(user_id: str | None = None) -> set[str]:
    """Static whitelist plus every currently registered MCP tool."""
    from app.tools.registry import registry

    names = set(VOICE_TOOLS)
    for tool_name in registry.get_all_tool_names(user_id=user_id):
        toolset = registry.get_toolset_for_tool(tool_name, user_id=user_id) or ""
        if toolset.startswith("mcp-"):
            names.add(tool_name)
    return names
