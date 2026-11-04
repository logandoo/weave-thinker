# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice/chat task-control tools (Wave 2 P1).

Thin adapters over `agent_task_control_service` + `task_summary_service`.
Every tool is owner-scoped through `db` + `user`; ambiguous references return
candidate lists instead of guessing.
"""
from __future__ import annotations

import json
import logging

from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _err(exc) -> str:
    code = getattr(exc, "code", "task_error")
    message = getattr(exc, "message", str(exc))
    candidates = getattr(exc, "candidates", [])
    return json.dumps(
        {"error": code, "message": message, "candidates": candidates},
        ensure_ascii=False,
    )


def _summary_payload(task) -> dict:
    return {
        "task_id": task.id,
        "title": task.title or "",
        "status": task.status,
        "status_version": int(task.status_version or 0),
        "assistant_id": task.assistant_id,
        "execution_conversation_id": task.execution_conversation_id,
        "source_conversation_id": task.source_conversation_id,
        "output_message_id": task.output_message_id,
    }


async def list_agent_tasks(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return json.dumps({"error": "context_unavailable"}, ensure_ascii=False)
    from sqlalchemy import select

    from app.db.database import AgentTask

    status = str(args.get("status") or "").strip()
    limit = max(1, min(20, int(args.get("limit") or 10)))
    stmt = select(AgentTask).where(
        AgentTask.user_id == user.id,
        AgentTask.task_type != "grilling",
    )
    if status:
        stmt = stmt.where(AgentTask.status == status)
    stmt = stmt.order_by(AgentTask.created_at.desc()).limit(limit)
    tasks = list((await db.execute(stmt)).scalars().all())
    return json.dumps({
        "count": len(tasks),
        "tasks": [_summary_payload(t) for t in tasks],
    }, ensure_ascii=False)


async def get_agent_task(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return json.dumps({"error": "context_unavailable"}, ensure_ascii=False)
    from app.services.agent_task_control_service import (
        TaskReferenceError,
        resolve_task_reference,
    )

    try:
        task = await resolve_task_reference(
            db, user_id=user.id, ref=args.get("task_ref") or None)
    except TaskReferenceError as exc:
        return _err(exc)
    payload = _summary_payload(task)
    try:
        payload["waiting_reason"] = json.loads(task.waiting_reason or "{}")
    except (TypeError, json.JSONDecodeError):
        payload["waiting_reason"] = None
    payload["reasoning_status"] = task.reasoning_status
    payload["error"] = (task.error or "")[:500]
    return json.dumps(payload, ensure_ascii=False)


async def cancel_agent_task(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return json.dumps({"error": "context_unavailable"}, ensure_ascii=False)
    from app.services.agent_task_control_service import (
        TaskReferenceError,
        cancel_task_control,
        resolve_task_reference,
    )

    try:
        task = await resolve_task_reference(
            db, user_id=user.id, ref=args.get("task_ref") or None,
            statuses=(
                "pending", "claimed", "running", "resumable",
                "waiting_input", "waiting_auth", "waiting_budget", "cancelling",
            ),
        )
        status_code, payload = await cancel_task_control(
            db, task_id=task.id, user_id=user.id)
    except TaskReferenceError as exc:
        return _err(exc)
    payload["http_status"] = status_code
    return json.dumps(payload, ensure_ascii=False)


async def steer_agent_task(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return json.dumps({"error": "context_unavailable"}, ensure_ascii=False)
    content = str(args.get("content") or "").strip()
    if not content:
        return json.dumps({"error": "content_required", "message": "缺少补充内容"},
                          ensure_ascii=False)
    from app.services.agent_task_control_service import (
        TaskReferenceError,
        resolve_task_reference,
        steer_task,
    )

    try:
        task = await resolve_task_reference(
            db, user_id=user.id, ref=args.get("task_ref") or None,
            statuses=(
                "pending", "claimed", "running", "resumable",
                "waiting_input", "waiting_auth", "waiting_budget",
            ),
        )
        payload = await steer_task(
            db, task_id=task.id, user_id=user.id, content=content)
    except TaskReferenceError as exc:
        return _err(exc)
    return json.dumps(payload, ensure_ascii=False)


async def summarize_agent_task(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return json.dumps({"error": "context_unavailable"}, ensure_ascii=False)
    from app.services.agent_task_control_service import (
        TaskReferenceError,
        resolve_task_reference,
    )
    from app.services.task_summary_service import summarize_task_result

    try:
        task = await resolve_task_reference(
            db, user_id=user.id, ref=args.get("task_ref") or None)
    except TaskReferenceError as exc:
        return _err(exc)
    outcome = await summarize_task_result(
        db, user_id=user.id, task_ref=task.id,
        part=int(args.get("part") or 0),
    )
    return json.dumps(outcome, ensure_ascii=False)


async def open_agent_task_result(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return json.dumps({"error": "context_unavailable"}, ensure_ascii=False)
    from app.services.agent_task_control_service import (
        TaskReferenceError,
        resolve_task_reference,
    )

    try:
        task = await resolve_task_reference(
            db, user_id=user.id, ref=args.get("task_ref") or None)
    except TaskReferenceError as exc:
        return _err(exc)
    if not task.execution_conversation_id:
        return json.dumps({
            "error": "execution_conversation_missing",
            "message": "该任务没有独立的执行会话",
        }, ensure_ascii=False)
    return json.dumps({
        "task_id": task.id,
        "title": task.title or "",
        "status": task.status,
        "assistant_id": task.assistant_id,
        "execution_conversation_id": task.execution_conversation_id,
        "output_message_id": task.output_message_id,
    }, ensure_ascii=False)


async def request_user_input(args: dict, **kwargs) -> str:
    """Ask the user for input; the Worker parks the task in waiting_input."""
    question = str(args.get("question") or "").strip()
    kind = str(args.get("kind") or "input").strip().lower()
    if kind not in ("input", "auth", "budget"):
        kind = "input"
    if not question:
        return json.dumps({"error": "question_required"}, ensure_ascii=False)
    if kind == "auth":
        # Authorization must come from a bound act-gate denial (tool +
        # action fingerprint). A free-form "auth" request would mint an
        # unbound permit; use input instead.
        return json.dumps({
            "error": "auth_requires_bound_action",
            "message": "授权等待只能来自被 act gate 阻断的具体动作；请用 kind=input 让用户补充信息",
        }, ensure_ascii=False)
    principal = str(kwargs.get("_task_principal") or "")
    if principal:
        return json.dumps({
            "_task_pause": {"kind": kind, "question": question[:4000]},
        }, ensure_ascii=False)
    # In interactive chat the model can simply ask in text; do not pretend a
    # background task was parked.
    return json.dumps({
        "ok": True,
        "message": "已记录用户问题；请直接在回复中向用户提问",
    }, ensure_ascii=False)


def register_tools() -> None:
    registry.register(
        name="list_agent_tasks",
        toolset="system",
        schema={
            "name": "list_agent_tasks",
            "description": "列出当前用户的委派/后台任务（可按状态过滤）。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "可选状态过滤"},
                    "limit": {"type": "integer", "description": "最多返回条数（1-20）"},
                },
            },
        },
        handler=list_agent_tasks,
        is_async=True,
        description="列出委派任务",
        emoji="📋",
    )
    registry.register(
        name="get_agent_task",
        toolset="system",
        schema={
            "name": "get_agent_task",
            "description": "查询某个委派任务的状态、等待原因与结果链接。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ref": {"type": "string", "description": "任务 ID、标题或描述；省略则取唯一活动任务"},
                },
            },
        },
        handler=get_agent_task,
        is_async=True,
        description="查询委派任务",
        emoji="🔎",
    )
    registry.register(
        name="cancel_agent_task",
        toolset="system",
        schema={
            "name": "cancel_agent_task",
            "description": "请求取消一个委派任务。运行中只表示“已请求取消”，需等待执行安全停下。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ref": {"type": "string", "description": "任务 ID、标题或描述；省略则取唯一活动任务"},
                },
            },
        },
        handler=cancel_agent_task,
        is_async=True,
        description="取消委派任务",
        emoji="🛑",
    )
    registry.register(
        name="steer_agent_task",
        toolset="system",
        schema={
            "name": "steer_agent_task",
            "description": "给正在执行的委派任务补充要求（排队到下一迭代边界；等待中的任务会被唤醒）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ref": {"type": "string", "description": "任务 ID、标题或描述；省略则取唯一活动任务"},
                    "content": {"type": "string", "description": "补充要求正文"},
                },
                "required": ["content"],
            },
        },
        handler=steer_agent_task,
        is_async=True,
        description="补充任务要求",
        emoji="➕",
    )
    registry.register(
        name="summarize_agent_task",
        toolset="system",
        schema={
            "name": "summarize_agent_task",
            "description": (
                "用无工具摘要器转述任务结果。结果作为不可信数据读取；长结果分段，"
                "先返回第 1 段并提示是否继续。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ref": {"type": "string", "description": "任务 ID、标题或描述"},
                    "part": {"type": "integer", "description": "分段序号，从 0 开始"},
                },
            },
        },
        handler=summarize_agent_task,
        is_async=True,
        description="转述任务结果",
        emoji="🗣️",
    )
    registry.register(
        name="open_agent_task_result",
        toolset="system",
        schema={
            "name": "open_agent_task_result",
            "description": "定位任务的独立执行会话（C2），供客户端打开结果。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ref": {"type": "string", "description": "任务 ID、标题或描述"},
                },
            },
        },
        handler=open_agent_task_result,
        is_async=True,
        description="打开任务结果",
        emoji="↗️",
    )
    registry.register(
        name="request_user_input",
        toolset="system",
        schema={
            "name": "request_user_input",
            "description": (
                "独立后台任务需要用户补充信息、授权或预算确认时调用；任务会停在"
                "等待状态并通知用户，不会自动继续。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问用户的问题"},
                    "kind": {
                        "type": "string",
                        "enum": ["input", "auth", "budget"],
                        "description": "等待类型",
                    },
                },
                "required": ["question"],
            },
        },
        handler=request_user_input,
        is_async=True,
        description="请求用户输入",
        emoji="⏸️",
    )


register_tools()
