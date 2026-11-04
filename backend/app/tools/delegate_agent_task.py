# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tool: delegate_agent_task — voice Manager hands a complex task to an assistant.

Thin adapter only: all business rules (target resolution, strict-highest
reasoning, idempotency, atomic C2+goal+task commit) live in
`agent_task_submission_service`. The tool never waits for execution.

Server-injected fields (user, C1, source message, origin, mode, policy) are
never accepted from model arguments — underscore-prefixed keys are stripped.
"""
import json
import logging

from app.core.config import get_config
from app.tools.registry import registry

logger = logging.getLogger(__name__)
config = get_config()


def _strip_internal(args: dict) -> dict:
    return {k: v for k, v in (args or {}).items() if not str(k).startswith("_")}


async def delegate_agent_task(args: dict, **kwargs) -> str:
    clean = _strip_internal(args)
    goal = str(clean.get("goal") or "").strip()
    if not goal:
        return json.dumps({"error": "缺少 goal 参数（完整、自包含的任务描述）"},
                          ensure_ascii=False)

    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    db = kwargs.get("db")
    if not user or db is None:
        return json.dumps({"error": "无法获取用户或数据库上下文"}, ensure_ascii=False)
    if not config.voice_delegation_enabled:
        return json.dumps({
            "error": "voice_delegation_disabled",
            "reason": "语音委派功能未开启（[voice_delegation] enabled=false）",
            "recoverable": False,
        }, ensure_ascii=False)

    source_message_id = (
        str(kwargs.get("_source_message_id") or "")
        or str(getattr(conversation, "last_user_message_id", "") or "")
        or None
    )
    if not source_message_id:
        # The tool is the voice Manager's delegation adapter. A text-chat turn
        # does not pass a durable source-message id, so provenance/idempotency
        # cannot be guaranteed; fail explicitly instead of re-binding to the
        # newest message (which could be a later "别委派了" interjection).
        return json.dumps({
            "error": "source_message_required",
            "message": "委派需要真实用户消息来源；请在语音轮中提交，或使用普通后台任务",
            "recoverable": False,
        }, ensure_ascii=False)

    origin_kind = "chat"
    try:
        if conversation is not None and getattr(conversation, "assistant_id", None):
            from app.db.database import Assistant
            from app.services.assistant_service import (
                LEGACY_VOICE_ASSISTANT_NAME,
                VOICE_ASSISTANT_NAME,
            )

            conv_assistant = await db.get(Assistant, conversation.assistant_id)
            if conv_assistant is not None and conv_assistant.name in (
                VOICE_ASSISTANT_NAME, LEGACY_VOICE_ASSISTANT_NAME,
            ):
                origin_kind = "voice"
    except Exception:
        origin_kind = "chat"
    try:
        from app.services.agent_task_submission_service import (
            SubmissionError,
            submit_voice_delegation,
        )

        result = await submit_voice_delegation(
            db,
            user=user,
            goal=goal,
            assistant_name=clean.get("assistant_name") or None,
            title=clean.get("title") or None,
            source_conversation_id=str(getattr(conversation, "id", "") or "") or None,
            source_message_id=source_message_id,
            origin_kind=origin_kind,
            project_path=clean.get("project_path") or None,
        )
    except SubmissionError as exc:
        return json.dumps({
            "error": exc.code,
            "message": exc.message,
            "recoverable": exc.status_code < 500,
        }, ensure_ascii=False)
    except Exception as exc:
        # Target-resolution errors carry explicit codes; anything unexpected is
        # logged loudly but never turned into a fake "submitted" receipt.
        try:
            from app.services.agent_task_submission_service import AssistantResolutionError

            if isinstance(exc, AssistantResolutionError):
                return json.dumps({
                    "error": exc.code,
                    "message": exc.message,
                    "recoverable": True,
                }, ensure_ascii=False)
        except Exception:
            pass
        logger.exception("delegate_agent_task failed")
        return json.dumps({
            "error": "delegation_failed",
            "message": "委派提交失败，请稍后重试",
            "recoverable": True,
        }, ensure_ascii=False)

    logger.info(
        "delegate_agent_task submitted task=%s C2=%s assistant=%s",
        result.get("task_id"), result.get("execution_conversation_id"),
        result.get("assistant_name"),
    )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="delegate_agent_task",
    toolset="system",
    schema={
        "name": "delegate_agent_task",
        "description": (
            "把复杂、耗时的任务异步交给一个执行助手，在全新的独立会话中执行。"
            "提交后立即返回任务 ID 和新会话 ID，不等待执行结果；用户可以继续当前对话。"
            "仅在用户明确要求把任务交给助手/后台执行时使用。goal 必须完整、自包含"
            "（原始需求、范围、必要上下文、期望输出、完成标准），因为执行助手看不到"
            "当前语音对话。未点名助手时交给系统默认助手。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "完整、自包含的任务描述（执行助手看不到当前对话）",
                },
                "assistant_name": {
                    "type": "string",
                    "description": "可选，用户点名的执行助手名称；必须与用户助手列表唯一匹配",
                },
                "title": {
                    "type": "string",
                    "description": "可选，短标题，用于任务列表与结果会话标题",
                },
                "project_path": {
                    "type": "string",
                    "description": (
                        "可选。用户工作区内既有 git 项目的相对/绝对路径；提供时任务在"
                        "独立 worktree/分支执行，不会改动基准工作树。"
                    ),
                },
            },
            "required": ["goal"],
        },
    },
    handler=delegate_agent_task,
    is_async=True,
    description="异步委派任务给执行助手",
    emoji="🧭",
)
