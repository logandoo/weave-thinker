# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""System capabilities endpoint.

Exposes a machine-readable description of the harness's tools, subsystems,
and key configuration parameters so external evaluators and integrations
can query capabilities without interrogating the agent via chat.

This endpoint is read-only and public-ish (requires auth, like /api/config).
It deliberately does NOT expose:
- Underlying model names / API providers / API keys
- Internal file paths
- User-specific data

Conv 01d08b67 showed that an evaluation session misjudged Weave Thinker
as a "bare model" partly because the only capability info available was
the agent's self-description (which is restricted by identity rules) and
the HTTP API docs (which describe routes, not harness features). This
endpoint fills that gap.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.deps import get_current_user
from app.db.database import User
from app.tools.registry import registry

router = APIRouter(prefix="/api/system", tags=["system"])

config = get_config()


@router.get("/capabilities")
async def get_system_capabilities(
    current_user: User = Depends(get_current_user),
):
    """Return a machine-readable manifest of the harness's capabilities.

    Includes: tool list, subsystem flags, key limits/timeouts, and modes.
    Does NOT include: model names, API keys, file paths, user data.
    """
    # Tool list grouped by toolset
    toolsets = registry.get_available_toolsets()
    tools = []
    for toolset_name, info in sorted(toolsets.items()):
        for tool_name in sorted(info.get("tools", [])):
            schema = registry.get_schema(tool_name) or {}
            func_def = schema.get("function", {}) if isinstance(schema, dict) else {}
            tools.append({
                "name": tool_name,
                "toolset": toolset_name,
                "description": (func_def.get("description") or "")[:200],
                "available": info.get("available", True),
            })

    # Harness subsystems — hardcoded feature flags (these rarely change and
    # are not derivable from a single config key; they describe the system's
    # architectural capabilities, not implementation details).
    subsystems = {
        "agent_loop": {
            "enabled": True,
            "max_iterations": config.agent_tool_loop_max_iterations,
            "max_consecutive_iterations": config.agent_tool_loop_max_consecutive_iterations,
            "parallel_tool_calls": config.agent_tool_loop.get("parallel_tool_calls", True),
            "tool_call_timeout_seconds": config.agent_tool_loop_tool_call_timeout,
            "subtask_iteration_timeout_seconds": config.agent_subtask_iteration_timeout,
            "inactivity_timeout_seconds": config.agent_tool_loop_conversation_timeout,
            "context_compression": config.agent_compression.get("enabled", False),
        },
        "deathmatch": {
            "enabled": True,
            "max_turns": config.deathmatch_max_turns,
            "tool_loop_max_iterations": config.deathmatch_tool_loop_max_iterations,
            "max_wall_time_seconds": config.deathmatch_max_wall_time_seconds,
            "stall_partial_threshold": config.deathmatch_stall_partial_threshold,
            "stall_hard_threshold": config.deathmatch_stall_hard_threshold,
            "phases": ["grilling", "goal_loop", "partial_complete", "human_gate"],
        },
        "memory": {
            "enabled": True,
            "layers": ["file_based", "database"],
            "file_targets": ["agent", "user", "system"],
            "db_tables": ["user_agent_states", "agent_memories", "agent_dreams"],
            "max_items_injected": config.agent_memory_max_items,
        },
        "background_tasks": {
            "enabled": config.agent_background_tasks_enabled,
            "max_concurrent": config.agent_background_tasks_max_concurrent,
            "total_timeout_seconds": config.agent_background_tasks_total_timeout,
            "poll_interval_seconds": config.agent_background_tasks_poll_interval,
        },
        "scheduled_tasks": {
            "enabled": True,
            "natural_language_parsing": True,
        },
        "subagent_delegation": {
            "enabled": True,
            "max_depth": config.agent_delegation_max_depth,
        },
        "skills": {
            "enabled": True,
            "system_skills": True,
            "user_skills": True,
            "executable_scripts": True,
        },
        "workspace": {
            "enabled": True,
            "per_user_isolation": True,
        },
        "pdf_export": {
            "enabled": True,
            "supports_mermaid": True,
            "supports_echarts": True,
            "supports_latex": True,
        },
        "context_compressor": {
            "enabled": config.agent_compression.get("enabled", False),
            "threshold_percent": config.agent_compression.get("threshold_percent", 0.65),
        },
        "coordinator": {
            "enabled": True,
            "routes": ["direct_reply", "tool_loop"],
        },
    }

    # Modes the chat API accepts (mirrors ChatRequest fields)
    modes = {
        "normal_chat": True,
        "deathmatch_mode": True,
        "enable_web_search": True,
        "enable_reasoning": True,
        "regenerate": True,
        "edit_message": True,
    }

    # Important: do NOT expose model_name, api_key, base_url, or any
    # provider-specific identifiers here. This endpoint describes the
    # harness's capabilities, not the underlying model implementation.
    return {
        "system": "Weave Thinker",
        "type": "harness",
        "version": "2026.07",
        "description": (
            "完整的智能体框架（harness），围绕大语言模型构建了工具调用循环、"
            "死磕模式、双层记忆、后台任务、定时任务、子代理委派、技能系统、"
            "工作区文件操作等能力。不是裸模型 API。"
        ),
        "tools": tools,
        "tool_count": len(tools),
        # P2-2: usage frequency (drives catalog slimming decisions) + the
        # active visibility allowlist (empty = all tools visible).
        "tool_usage": registry.get_tool_usage_stats(),
        "visible_tools": config.agent_tools_visible,
        "subsystems": subsystems,
        "modes": modes,
        "limits": {
            "execute_code_timeout_seconds": config.agent_tool_loop_tool_call_timeout,
            "subtask_iteration_timeout_seconds": config.agent_subtask_iteration_timeout,
            "inactivity_timeout_seconds": config.agent_tool_loop_conversation_timeout,
            "max_iterations_per_turn": config.agent_tool_loop_max_iterations,
            "deathmatch_max_turns": config.deathmatch_max_turns,
            "background_task_total_timeout_seconds": config.agent_background_tasks_total_timeout,
        },
        "notes": [
            "单次 LLM 迭代上限 10 分钟（subtask_iteration_timeout），单个工具调用上限 30 分钟（tool_call_timeout）——长任务需拆步（见 agent 系统提示词规则 18）",
            "死磕模式 stall 升级：3 次 → partial_complete，6 次 → human_gate",
            "后台任务与 SSE 解耦，客户端断开后任务继续运行；长评测应使用 POST /api/agent-tasks 创建后台任务，而非通过 SSE 同步执行",
            "本端点不暴露底层模型名称/API 提供商/版本号（受身份保密规则限制）",
        ],
    }
