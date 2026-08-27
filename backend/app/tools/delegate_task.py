# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from app.tools.registry import registry
from app.services.llm_service import LLMService
from app.core.config import get_config
from app.db.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
config = get_config()

DELEGATE_BLOCKED_TOOLS = frozenset([
    "delegate_task",
    "clarify",
    "memory",
])

MAX_DELEGATION_DEPTH = 2


def _get_default_child_max_iterations():
    from app.core.config import get_config
    return get_config().agent_delegation_default_child_max_iterations

def _get_default_child_timeout():
    from app.core.config import get_config
    return get_config().agent_subtask_iteration_timeout


def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    workspace_path: str = "",
    role: str = "leaf",
) -> str:
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    if workspace_path:
        parts.append(
            f"\nWORKSPACE PATH:\n{workspace_path}\n"
            "Use this exact path for local repository/workdir operations unless the task explicitly says otherwise."
        )
    if role == "orchestrator":
        parts.append(
            "\nROLE: You are an orchestrator. You may delegate subtasks to other agents "
            "using the delegate_task tool when it benefits the overall goal."
        )
    else:
        parts.append(
            "\nROLE: You are a leaf agent. You cannot delegate tasks — complete the work directly."
        )
    parts.append(
        "\nComplete this task using the tools available to you. "
        "You have access to web_search (for searching the web), browser (for browsing web pages), "
        "and execute_code (for running Python code). "
        "IMPORTANT: When you are uncertain about facts, data, or need up-to-date information, "
        "you MUST use web_search to find the answer rather than guessing. "
        "Never fabricate information — search first.\n"
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Be thorough but concise — your response is returned to the "
        "parent agent as a summary."
    )
    return "\n".join(parts)


def _get_child_blocked_tools(depth: int, role: str) -> set:
    blocked = set(DELEGATE_BLOCKED_TOOLS)
    if role == "leaf" or depth >= MAX_DELEGATION_DEPTH - 1:
        blocked.add("delegate_task")
    return blocked


async def _run_child_agent(
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    workspace_path: str,
    parent_llm: LLMService,
    role: str = "leaf",
    depth: int = 0,
    model: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    child_start = asyncio.get_running_loop().time()
    child_id = f"subagent-{uuid.uuid4().hex[:8]}"

    if model:
        # P0: even an explicitly named delegation model stays on the parent
        # provider when the parent is a custom provider (never default to
        # the [api] deepseek endpoint under a custom-model assistant).
        child_llm = LLMService(
            custom_api_url=(parent_llm.client.base_url if parent_llm.is_custom_provider else None),
            custom_api_key=(parent_llm.client.api_key if parent_llm.is_custom_provider else None),
            custom_model_name=model,
        )
    else:
        child_llm = LLMService(
            custom_api_url=(parent_llm.client.base_url if parent_llm.is_custom_provider else None),
            custom_model_name=parent_llm.custom_model_name,
        )

    child_system = _build_child_system_prompt(goal, context, workspace_path, role)
    blocked = _get_child_blocked_tools(depth, role)

    child_tool_names = set(
        name for name in registry.get_all_tool_names()
        if name not in blocked
    )
    if toolsets:
        toolset_names = set()
        for ts in toolsets:
            toolset_names.update(registry.get_tool_names_for_toolset(ts))
        child_tool_names &= toolset_names

    child_tool_schemas = registry.get_definitions(child_tool_names)

    try:
        from app.services.agent_loop import AgentLoop
        _cfg = get_config()

        messages = [
            {"role": "system", "content": child_system},
            {"role": "user", "content": goal},
        ]

        loop = AgentLoop(
            llm=child_llm,
            tool_schemas=child_tool_schemas,
            max_iterations=_cfg.agent_tool_loop_max_iterations,
            workspace_path=workspace_path,
            blocked_tools=blocked,
            delegation_depth=depth + 1,
            provider_type=getattr(kwargs.get("assistant"), "provider_type", "deepseek") or "deepseek",
            enable_reasoning=False,
            session_factory=AsyncSessionLocal,
        )

        assistant_content = ""
        async for event in loop.run(
            messages,
            user=kwargs.get("user"),
            conversation=kwargs.get("conversation"),
            assistant=kwargs.get("assistant"),
        ):
            # A4.9 Minor-4: mirror the parent relays — a rejected draft must
            # never concatenate into the child summary (audit_reset wipes).
            if "audit_reset" in event:
                assistant_content = ""
            if event.get("content"):
                assistant_content += event.get("content", "")

        duration = asyncio.get_running_loop().time() - child_start
        return {
            "child_id": child_id,
            "status": "completed",
            "summary": assistant_content.strip() or "(no output)",
            "duration_seconds": round(duration, 2),
        }
    except Exception as e:
        logger.debug("AgentLoop failed, using simple call: %s", e)

    messages = [
        {"role": "system", "content": child_system},
        {"role": "user", "content": goal},
    ]

    try:
        content, reasoning = await asyncio.to_thread(
            child_llm.complete_chat_parts, messages,
            temperature=config.default_temperature,
            max_tokens=2048,
            tools=child_tool_schemas or None,
        )
        duration = asyncio.get_running_loop().time() - child_start

        return {
            "child_id": child_id,
            "status": "completed",
            "summary": content or "",
            "reasoning": reasoning or "",
            "duration_seconds": round(duration, 2),
        }
    except Exception as e:
        logger.exception("Child agent %s failed", child_id)
        duration = asyncio.get_running_loop().time() - child_start
        return {
            "child_id": child_id,
            "status": "error",
            "error": str(e),
            "duration_seconds": round(duration, 2),
        }


async def delegate_task(args: dict, **kwargs) -> str:
    goal = args.get("goal", "") or ""
    context = args.get("context", "") or ""
    tasks = args.get("tasks", None)
    toolsets = args.get("toolsets", None)
    role = args.get("role", "leaf")
    model = args.get("model", None)
    depth = kwargs.get("_delegation_depth", 0)

    workspace_path = kwargs.get("workspace_path", "")
    parent_llm = kwargs.get("_parent_llm")

    if not parent_llm:
        from app.services.auxiliary_client import get_aux_llm_override
        parent_llm = get_aux_llm_override() or LLMService()

    if depth >= MAX_DELEGATION_DEPTH:
        return json.dumps({
            "error": f"Maximum delegation depth ({MAX_DELEGATION_DEPTH}) reached. Cannot delegate further.",
        }, ensure_ascii=False)

    child_kwargs = {
        "user": kwargs.get("user"),
        "conversation": kwargs.get("conversation"),
        "assistant": kwargs.get("assistant"),
    }

    child_timeout = _get_default_child_timeout()

    if goal.strip():
        try:
            if child_timeout and child_timeout > 0:
                result = await asyncio.wait_for(
                    _run_child_agent(
                        goal=goal,
                        context=context if context.strip() else None,
                        toolsets=toolsets,
                        workspace_path=workspace_path,
                        parent_llm=parent_llm,
                        role=role,
                        depth=depth,
                        model=model,
                        **child_kwargs,
                    ),
                    timeout=child_timeout,
                )
            else:
                result = await _run_child_agent(
                    goal=goal,
                    context=context if context.strip() else None,
                    toolsets=toolsets,
                    workspace_path=workspace_path,
                    parent_llm=parent_llm,
                    role=role,
                    depth=depth,
                    model=model,
                    **child_kwargs,
                )
        except asyncio.TimeoutError:
            return json.dumps(
                {"error": f"Delegated task timed out after {child_timeout}s"},
                ensure_ascii=False,
            )
        return json.dumps({"results": [result]}, ensure_ascii=False)

    if tasks and isinstance(tasks, list):
        max_concurrent = min(len(tasks), 5)
        sem = asyncio.Semaphore(max_concurrent)

        async def _run_with_sem(task_dict: dict):
            async with sem:
                t_goal = task_dict.get("goal", "")
                t_context = task_dict.get("context", "")
                t_toolsets = task_dict.get("toolsets", toolsets)
                t_role = task_dict.get("role", role)
                t_model = task_dict.get("model", model)
                return await _run_child_agent(
                    goal=t_goal,
                    context=t_context if t_context and t_context.strip() else None,
                    toolsets=t_toolsets,
                    workspace_path=workspace_path,
                    parent_llm=parent_llm,
                    role=t_role,
                    depth=depth,
                    model=t_model,
                    **child_kwargs,
                )

        try:
            if child_timeout and child_timeout > 0:
                results = await asyncio.wait_for(
                    asyncio.gather(*[_run_with_sem(t) for t in tasks]),
                    timeout=child_timeout,
                )
            else:
                results = await asyncio.gather(*[_run_with_sem(t) for t in tasks])
        except asyncio.TimeoutError:
            return json.dumps(
                {"error": f"Batch delegation timed out after {child_timeout}s"},
                ensure_ascii=False,
            )
        return json.dumps({"results": results}, ensure_ascii=False)

    return json.dumps({"error": "Provide either 'goal' (single task) or 'tasks' (batch)."}, ensure_ascii=False)


registry.register(
    name="delegate_task",
    toolset="core",
    schema={
        "name": "delegate_task",
        "description": (
            "Spawn child subagents to handle tasks in parallel. Each child has isolated "
            "context and restricted tools. Use for: 2+ independent subtasks that can run "
            "in parallel, or reasoning-heavy subtasks that would flood your context.\n\n"
            "Modes: Single = 'goal' (+ optional 'context', 'toolsets'); "
            "Batch = 'tasks' array [{goal, context, toolsets, role, model}, ...].\n\n"
            "Roles: 'leaf' (default, cannot further delegate); "
            "'orchestrator' (can delegate subtasks, up to max depth).\n\n"
            "Do NOT delegate: single-step mechanical work, trivial tasks you can do "
            "in one or two tool calls, or re-delegating your entire goal to one worker."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "The task goal for a single subagent.",
                },
                "context": {
                    "type": "string",
                    "description": "Additional context for the subagent.",
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "context": {"type": "string"},
                            "toolsets": {"type": "array", "items": {"type": "string"}},
                            "role": {"type": "string", "enum": ["leaf", "orchestrator"]},
                            "model": {"type": "string"},
                        },
                        "required": ["goal"],
                    },
                    "description": "Array of tasks for batch parallel delegation.",
                },
                "toolsets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Toolset names to enable for the subagent(s). Default: all non-blocked tools.",
                },
                "role": {
                    "type": "string",
                    "enum": ["leaf", "orchestrator"],
                    "description": "Role for the child agent: 'leaf' cannot further delegate, 'orchestrator' can delegate subtasks. Default: leaf.",
                },
                "model": {
                    "type": "string",
                    "description": "Model name for the child agent. Use a cheaper/faster model for simple subtasks. Default: same as parent.",
                },
            },
        },
    },
    handler=delegate_task,
    is_async=True,
    description="Delegate tasks to parallel subagents",
    emoji="",
)
