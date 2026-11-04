# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Authoritative task-result reader (Wave 2 §10.5).

C2 / output_message is the long-lived authority; AgentTask is only the runtime
state record and may be removed by the 7-day retention sweep. When the task row
is gone, the same-user outbox row (logical_task_id) still points at C2 and the
output message. This module never fabricates a result and never falls back to
the source voice conversation.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import desc, or_, select

from app.db.database import AgentTask, Conversation, Message, VoiceNotificationOutbox

logger = logging.getLogger(__name__)


def result_hash(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class TaskResult:
    logical_task_id: str
    task_id: Optional[str]
    status: str
    status_version: int
    assistant_name: str
    title: str
    execution_conversation_id: Optional[str]
    output_message_id: Optional[str]
    result_text: Optional[str]
    result_hash: str
    result_missing: bool = False
    conversation_deleted: bool = False
    task_record_present: bool = False
    reasoning_status: Optional[str] = None
    origin_kind: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "logical_task_id": self.logical_task_id,
            "task_id": self.task_id,
            "status": self.status,
            "status_version": self.status_version,
            "assistant_name": self.assistant_name,
            "title": self.title,
            "execution_conversation_id": self.execution_conversation_id,
            "output_message_id": self.output_message_id,
            "result_hash": self.result_hash,
            "result_missing": self.result_missing,
            "conversation_deleted": self.conversation_deleted,
            "task_record_present": self.task_record_present,
            "reasoning_status": self.reasoning_status,
            "result_preview": (self.result_text or "")[:200],
        }


def _manifest(task: AgentTask) -> dict:
    try:
        value = json.loads(task.execution_config or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _payload(row: VoiceNotificationOutbox) -> dict:
    try:
        value = json.loads(row.payload or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


async def _find_outbox(db, user_id: str, ref: str) -> Optional[VoiceNotificationOutbox]:
    """Same-user fallback after the AgentTask row was cleaned up."""
    stmt = (
        select(VoiceNotificationOutbox)
        .where(
            VoiceNotificationOutbox.recipient_user_id == user_id,
            or_(
                VoiceNotificationOutbox.logical_task_id == ref,
                VoiceNotificationOutbox.id == ref,
            ),
        )
        .order_by(desc(VoiceNotificationOutbox.created_at))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def resolve_task_result(
    db, *, user_id: str, task_ref: str,
) -> Optional[TaskResult]:
    """Resolve a user-owned task (or its retained outbox reference)."""
    ref = str(task_ref or "").strip()
    if not ref:
        return None
    task = await db.get(AgentTask, ref)
    if task is not None and str(task.user_id) != str(user_id):
        task = None

    outbox = None
    if task is None:
        outbox = await _find_outbox(db, user_id, ref)
        if outbox is None:
            return None

    payload = _payload(outbox) if outbox is not None else {}
    manifest = _manifest(task) if task is not None else {}
    assistant = manifest.get("assistant") or {}
    reasoning = manifest.get("reasoning") or {}

    logical_id = (
        str(task.id) if task is not None
        else str(payload.get("logical_task_id") or payload.get("task_id") or ref)
    )
    execution_id = (
        getattr(task, "execution_conversation_id", None) if task is not None
        else None
    )
    # `execution_conversation_id` is an FK with ON DELETE SET NULL, so a
    # deleted C2 leaves it NULL; `output_conversation_id` is a plain string
    # projection and survives, which is how deletion is detected.
    conversation_id = execution_id or (
        getattr(task, "output_conversation_id", None) if task is not None
        else payload.get("execution_conversation_id")
    )
    output_message_id = (
        getattr(task, "output_message_id", None) if task is not None
        else payload.get("output_message_id")
    )
    status = str(
        getattr(task, "status", None) if task is not None
        else payload.get("task_status") or payload.get("status") or ""
    )
    status_version = int(
        (getattr(task, "status_version", 0) if task is not None
         else payload.get("status_version") or 0) or 0
    )
    assistant_name = str(
        (assistant.get("name") if task is not None else payload.get("assistant_name"))
        or payload.get("assistant_name") or "执行助手"
    )
    title = str(
        (getattr(task, "title", None) if task is not None else payload.get("title"))
        or payload.get("title") or ""
    )

    conversation = None
    if conversation_id:
        conversation = await db.get(Conversation, conversation_id)
    conversation_deleted = bool(conversation_id) and conversation is None

    result_text: Optional[str] = None
    if conversation_deleted:
        result_text = None
    elif output_message_id:
        message = await db.get(Message, output_message_id)
        if message is not None and str(message.conversation_id) == str(conversation_id):
            result_text = message.content
    if result_text is None and not conversation_deleted and task is not None:
        # Finalize writes task.result and the C2 message in the same
        # transaction; the task copy is acceptable only while C2 still exists.
        result_text = getattr(task, "result", None)

    result_missing = not (result_text or "").strip()
    return TaskResult(
        logical_task_id=logical_id,
        task_id=str(getattr(task, "id", "") or "") or None,
        status=status,
        status_version=status_version,
        assistant_name=assistant_name,
        title=title,
        execution_conversation_id=conversation_id,
        output_message_id=output_message_id,
        result_text=result_text if not result_missing else None,
        result_hash=result_hash(result_text if not result_missing else ""),
        result_missing=result_missing,
        conversation_deleted=conversation_deleted,
        task_record_present=task is not None,
        reasoning_status=(
            getattr(task, "reasoning_status", None) if task is not None
            else payload.get("reasoning_status")
        ),
        origin_kind=(
            getattr(task, "origin_kind", None) if task is not None
            else payload.get("origin_kind")
        ),
        extra={
            "reasoning_policy": reasoning.get("policy"),
            "requested_reasoning_effort": reasoning.get("effort"),
        },
    )
