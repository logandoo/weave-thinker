# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unified finalize contract for `separate_v1` agent tasks (§10.1).

Two legitimate entries:
- Worker fenced finalize (attempt_id matches, status non-terminal)
- Control-plane finalize (row lock + status_version CAS; no live lease)

Both write the C2 outcome Message, the task terminal state and the durable
outbox event in ONE transaction.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

from app.db.database import AgentTask, Message
from app.services.agent_task_status import (
    ACTIVE_STATUSES,
    STEERABLE_STATUSES,
    TERMINAL_STATUSES,
    WAITING_STATUSES,
)

logger = logging.getLogger(__name__)

SEPARATE_MODE = "separate_v1"
WAITING_KIND_TO_STATUS = {
    "input": "waiting_input",
    "auth": "waiting_auth",
    "budget": "waiting_budget",
}
WAITING_EVENT_TYPES = {
    "waiting_input": "input_required",
    "waiting_auth": "auth_required",
    "waiting_budget": "budget_required",
}


def is_separate(task: Any) -> bool:
    return str(getattr(task, "execution_mode", "") or "") == SEPARATE_MODE


def is_terminal(task: Any) -> bool:
    return str(getattr(task, "status", "") or "") in TERMINAL_STATUSES


def can_steer(task: Any) -> bool:
    return str(getattr(task, "status", "") or "") in STEERABLE_STATUSES


def cancel_decision(task: Any) -> str:
    """Control-plane cancel classification (§7.3)."""
    status = str(getattr(task, "status", "") or "")
    if status in ("pending", "resumable", "waiting_input", "waiting_auth",
                  "waiting_budget"):
        return "cancelled_now"
    if status in ("claimed", "running"):
        return "cancelling"
    if status == "cancelling":
        return "already_cancelling"
    return "too_late"


def terminal_message_text(
    *, status: str, result: Optional[str], error: Optional[str], assistant_name: str,
) -> str:
    name = assistant_name or "执行助手"
    if status == "completed":
        return result or "任务执行结束，但没有生成文本结果。"
    if status == "timed_out":
        body = "任务已达到执行时限，已保留部分结果。"
        if result:
            return f"{body}\n\n{result}"
        return body
    if status == "cancelled":
        detail = f"原因：{error}" if error else "任务在执行安全边界停止。"
        return f"{name} 的委派任务已取消。{detail}"
    detail = f"原因：{error}" if error else "未知错误。"
    return f"{name} 的委派任务执行失败。{detail}"


async def finalize_separate_task(
    db,
    *,
    task_id: str,
    status: str,
    attempt_id: Optional[str] = None,
    require_attempt: bool = False,
    require_statuses: Optional[tuple] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
    tool_results: Optional[str] = None,
    reasoning_status: Optional[str] = None,
    runtime_meta: Optional[dict] = None,
    usage_meta: Optional[dict] = None,
    workspace_meta: Optional[dict] = None,
) -> Optional[dict]:
    """Finalize once. Returns None when fenced/stale/already terminal."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"not a terminal status: {status}")

    row = await db.execute(
        select(AgentTask).where(AgentTask.id == task_id).with_for_update()
    )
    task = row.scalar_one_or_none()
    if task is None or not is_separate(task):
        await db.rollback()
        return None
    if is_terminal(task):
        await db.rollback()
        return None
    if require_statuses is not None and str(task.status or "") not in require_statuses:
        await db.rollback()
        return None
    if require_attempt and str(task.attempt_id or "") != str(attempt_id or ""):
        logger.warning(
            "fenced finalize rejected: task=%s attempt=%s current=%s",
            task_id, attempt_id, task.attempt_id,
        )
        await db.rollback()
        return None

    if status == "completed" and str(task.pending_messages or "").strip():
        # A steer accepted before finalize must not be silently dropped (§7.2).
        await db.rollback()
        return {"blocked": "pending_steer"}

    manifest = {}
    try:
        manifest = json.loads(task.execution_config or "{}")
    except (TypeError, json.JSONDecodeError):
        manifest = {}
    assistant_name = str((manifest.get("assistant") or {}).get("name") or "执行助手")

    conversation_id = task.execution_conversation_id
    output_message_id = None
    if conversation_id:
        content = terminal_message_text(
            status=status, result=result, error=error, assistant_name=assistant_name,
        )
        message = Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            tool_results=tool_results,
        )
        db.add(message)
        await db.flush()
        output_message_id = message.id

    task.status = status
    task.completed_at = datetime.utcnow()
    task.status_version = int(task.status_version or 0) + 1
    task.lease_until = None
    task.heartbeat_at = None
    task.attempt_id = None
    task.waiting_reason = None
    task.output_conversation_id = conversation_id
    task.output_message_id = output_message_id
    if status in ("completed", "timed_out"):
        task.result = result
        task.progress = 1.0 if status == "completed" else (task.progress or 0.0)
        task.error = None
    else:
        task.error = (error or "")[:5000] or None
    if reasoning_status:
        task.reasoning_status = reasoning_status
    if runtime_meta is not None:
        task.runtime_meta = json.dumps(runtime_meta, ensure_ascii=False)
    if usage_meta is not None:
        task.usage_meta = json.dumps(usage_meta, ensure_ascii=False)
    if workspace_meta is not None:
        task.workspace_meta = json.dumps(workspace_meta, ensure_ascii=False)

    from app.services.voice_notification_service import record_separate_notification

    # Every terminal separate task gets a durable event (§10.1); voice-origin
    # events are claimable by the voice session, chat-origin events stay web.
    await record_separate_notification(db, task, assistant_name=assistant_name)

    await db.commit()
    logger.info(
        "Separate task finalized: task=%s status=%s version=%s C2=%s msg=%s",
        task_id, status, task.status_version, conversation_id, output_message_id,
    )
    return {
        "task_id": task_id,
        "status": status,
        "status_version": task.status_version,
        "output_message_id": output_message_id,
        "execution_conversation_id": conversation_id,
    }


def waiting_status_for_kind(kind: str) -> str:
    status = WAITING_KIND_TO_STATUS.get(str(kind or ""))
    if not status:
        raise ValueError(f"unknown waiting kind: {kind}")
    return status


def waiting_notice_text(*, kind: str, question: Optional[str], assistant_name: str) -> str:
    name = assistant_name or "执行助手"
    prompt = (question or "").strip()
    if kind == "auth":
        base = f"{name} 的委派任务需要你授权一个高风险动作后才能继续。"
    elif kind == "budget":
        base = f"{name} 的委派任务已达到执行预算，需要你确认是否继续。"
    else:
        base = f"{name} 的委派任务需要你补充信息后才能继续。"
    if prompt:
        return f"{base}\n\n{prompt}"
    return base


async def finalize_separate_waiting(
    db,
    *,
    task_id: str,
    kind: str,
    question: Optional[str] = None,
    attempt_id: Optional[str] = None,
    require_attempt: bool = False,
    tool_name: Optional[str] = None,
    action_fingerprint: Optional[str] = None,
    expires_at: Optional[str] = None,
    result: Optional[str] = None,
    usage_meta: Optional[dict] = None,
    runtime_meta: Optional[dict] = None,
    workspace_meta: Optional[dict] = None,
) -> Optional[dict]:
    """Park a separate task in a non-terminal waiting state.

    Saves the checkpoint (already on the row), writes a C2 assistant notice and
    a durable waiting event in one transaction. `completed_at` stays NULL and
    the task still owns C2 until a steer/cancel resolves it.
    """
    status = waiting_status_for_kind(kind)
    row = await db.execute(
        select(AgentTask).where(AgentTask.id == task_id).with_for_update()
    )
    task = row.scalar_one_or_none()
    if task is None or not is_separate(task) or is_terminal(task):
        await db.rollback()
        return None
    if str(task.status or "") not in ("claimed", "running"):
        await db.rollback()
        return None
    if require_attempt and str(task.attempt_id or "") != str(attempt_id or ""):
        logger.warning(
            "fenced waiting finalize rejected: task=%s attempt=%s current=%s",
            task_id, attempt_id, task.attempt_id,
        )
        await db.rollback()
        return None

    manifest = {}
    try:
        manifest = json.loads(task.execution_config or "{}")
    except (TypeError, json.JSONDecodeError):
        manifest = {}
    assistant_name = str((manifest.get("assistant") or {}).get("name") or "执行助手")

    conversation_id = task.execution_conversation_id
    output_message_id = None
    if conversation_id:
        message = Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="assistant",
            content=waiting_notice_text(
                kind=kind, question=question, assistant_name=assistant_name),
        )
        db.add(message)
        await db.flush()
        waiting_message_id = message.id
    else:
        waiting_message_id = None

    task.status = status
    task.completed_at = None
    task.status_version = int(task.status_version or 0) + 1
    task.lease_until = None
    task.heartbeat_at = None
    task.last_attempt_started_at = task.last_attempt_started_at or task.started_at
    task.attempt_id = None
    task.waiting_reason = json.dumps({
        "kind": kind,
        "question": (question or "")[:4000],
        "tool_name": tool_name,
        "action_fingerprint": action_fingerprint,
        "expires_at": expires_at,
    }, ensure_ascii=False)
    task.error = None
    task.output_conversation_id = conversation_id
    # A waiting notice must NOT become the authoritative output message:
    # task.result (partial) stays summarizable and a later terminal finalize
    # owns output_message_id.
    if result is not None:
        task.result = result
    if usage_meta is not None:
        task.usage_meta = json.dumps(usage_meta, ensure_ascii=False)
    if workspace_meta is not None:
        task.workspace_meta = json.dumps(workspace_meta, ensure_ascii=False)
    if runtime_meta is not None:
        task.runtime_meta = json.dumps(runtime_meta, ensure_ascii=False)
    task.updated_at = datetime.utcnow()

    from app.services.voice_notification_service import record_task_event

    await record_task_event(
        db, task, event_type=WAITING_EVENT_TYPES[status],
        assistant_name=assistant_name,
        extra_payload={
            "waiting_kind": kind,
            "question_preview": (question or "")[:200],
            "tool_name": tool_name,
        },
    )

    await db.commit()
    logger.info(
        "Separate task waiting: task=%s status=%s version=%s C2=%s msg=%s",
        task_id, status, task.status_version, conversation_id, waiting_message_id,
    )
    return {
        "task_id": task_id,
        "status": status,
        "status_version": task.status_version,
        "waiting_message_id": waiting_message_id,
        "output_message_id": task.output_message_id,
        "execution_conversation_id": conversation_id,
        "waiting_reason": task.waiting_reason,
    }
