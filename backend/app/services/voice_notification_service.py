# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Durable outbox operations for voice background-task completion notices."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import desc, or_, select, update

from app.db.database import VoiceNotificationOutbox

EVENT_TYPES = ("terminal", "input_required", "auth_required", "budget_required")
WAITING_EVENT_TYPES = {
    "waiting_input": "input_required",
    "waiting_auth": "auth_required",
    "waiting_budget": "budget_required",
}


def build_separate_notification_payload(
    task, *, assistant_name: str, event_type: str = "terminal",
    extra_payload: dict | None = None,
) -> dict:
    """Small trusted metadata only — never goal/result/error full text (§10.4)."""
    payload = {
        "logical_task_id": task.id,
        "task_id": task.id,
        "recipient_user_id": task.user_id,
        "origin_conversation_id": getattr(task, "source_conversation_id", None),
        "execution_conversation_id": getattr(task, "execution_conversation_id", None),
        "output_message_id": getattr(task, "output_message_id", None),
        "event_type": event_type,
        "task_status": task.status,
        "status_version": int(getattr(task, "status_version", 0) or 0),
        "assistant_name": str(assistant_name or ""),
        "title": str(getattr(task, "title", "") or "")[:80],
        "reasoning_status": getattr(task, "reasoning_status", None),
        "notification_mode": "deterministic_v1",
        "delivery_channel": (
            "voice" if str(getattr(task, "origin_kind", "") or "") == "voice" else "web"
        ),
    }
    for key, value in (extra_payload or {}).items():
        if key in payload:
            continue
        payload[key] = value
    return payload


async def record_task_event(
    db, task, *, event_type: str, assistant_name: str,
    extra_payload: dict | None = None,
):
    """Add exactly one durable event per (logical task, status_version, type).

    No commit: the caller's finalize transaction owns atomicity. Returns None
    when an identical event already exists.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    existing = await db.execute(
        select(VoiceNotificationOutbox).where(
            VoiceNotificationOutbox.recipient_user_id == task.user_id,
            VoiceNotificationOutbox.logical_task_id == task.id,
            VoiceNotificationOutbox.status_version == int(task.status_version or 0),
            VoiceNotificationOutbox.event_type == event_type,
        ).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return None
    payload = build_separate_notification_payload(
        task, assistant_name=assistant_name, event_type=event_type,
        extra_payload=extra_payload,
    )
    row = VoiceNotificationOutbox(
        id=str(uuid.uuid4()),
        task_id=task.id,
        logical_task_id=task.id,
        recipient_user_id=task.user_id,
        user_id=task.user_id,
        conversation_id=None,
        origin_conversation_id=getattr(task, "source_conversation_id", None),
        execution_conversation_id=getattr(task, "execution_conversation_id", None),
        output_message_id=getattr(task, "output_message_id", None),
        event_type=event_type,
        status_version=int(task.status_version or 0),
        delivery_channel=(
            "voice" if str(getattr(task, "origin_kind", "") or "") == "voice" else "web"
        ),
        status="pending",
        task_status=task.status,
        payload=json.dumps(payload, ensure_ascii=False),
        attempt_count=0,
    )
    db.add(row)
    await db.flush()
    # Latest-wins per logical task: an unclaimed older pending event (e.g.
    # input_required superseded by terminal) must not be announced later.
    await db.execute(
        update(VoiceNotificationOutbox)
        .where(
            VoiceNotificationOutbox.recipient_user_id == task.user_id,
            VoiceNotificationOutbox.logical_task_id == task.id,
            VoiceNotificationOutbox.status == "pending",
            VoiceNotificationOutbox.status_version < int(task.status_version or 0),
            VoiceNotificationOutbox.delivery_attempt_id.is_(None),
            VoiceNotificationOutbox.id != row.id,
        )
        .values(status="dismissed", dismissed_at=datetime.utcnow())
    )
    return row


async def record_separate_notification(db, task, *, assistant_name: str):
    """Terminal event (kept as the Wave 1 entry point)."""
    if str(getattr(task, "status", "") or "") not in (
        "completed", "failed", "cancelled", "timed_out",
    ):
        # Waiting states already wrote their own event in finalize; a caller
        # hook must never fabricate a terminal event for a parked task.
        return None
    return await record_task_event(
        db, task, event_type="terminal", assistant_name=assistant_name)


def task_notification_info(task) -> dict:
    return {
        "task_id": task.id,
        "title": task.title or (task.goal or "")[:20],
        "goal": task.goal or "",
        "status": task.status,
        "result": (task.result or "")[:4000],
        "error": (task.error or "")[:1000],
        "output_note_id": task.output_note_id,
        "output_conversation_id": task.output_conversation_id,
    }


def notification_info(row: VoiceNotificationOutbox) -> dict:
    try:
        return json.loads(row.payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


async def record_voice_task_notification(db, task) -> VoiceNotificationOutbox:
    """Persist exactly one pending notice for a terminal voice task."""
    existing = await db.get(VoiceNotificationOutbox, task.id)
    if existing is not None:
        return existing
    row = VoiceNotificationOutbox(
        id=task.id,
        task_id=task.id,
        user_id=task.user_id,
        conversation_id=task.conversation_id,
        status="pending",
        task_status=task.status,
        payload=json.dumps(task_notification_info(task), ensure_ascii=False),
        attempt_count=0,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def touch_voice_notification(db, notification_id: str) -> None:
    row = await db.get(VoiceNotificationOutbox, notification_id)
    if row is None or row.status != "pending":
        return
    row.attempt_count = (row.attempt_count or 0) + 1
    row.last_attempt_at = datetime.utcnow()
    await db.commit()


async def claim_voice_notifications(db, user_id: str, conversation_id: str | None = None,
                                    stale_after_seconds: int = 300,
                                    notification_id: str | None = None,
                                    follow_user: bool = True,
                                    limit: int = 50) -> list[dict]:
    """Return pending notices and record an audible-delivery attempt.

    Rows remain pending until playback_drained. A short stale window prevents
    hot replay loops while still allowing recovery after a crashed attempt.
    At-least-once delivery is intentional; the client/consumer dedupes IDs.

    ``follow_user=True`` (default) claims across the user's voice sessions, the
    §10.3 product contract: switching voice conversations must not strand a
    notice. ``conversation_id`` is kept for legacy call sites and is only used
    when ``follow_user=False``.
    """
    stale_before = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
    query = select(VoiceNotificationOutbox).where(
        VoiceNotificationOutbox.user_id == user_id,
        VoiceNotificationOutbox.status == "pending",
        or_(
            VoiceNotificationOutbox.delivery_channel.is_(None),
            VoiceNotificationOutbox.delivery_channel == "voice",
        ),
        or_(
            VoiceNotificationOutbox.last_attempt_at.is_(None),
            VoiceNotificationOutbox.last_attempt_at < stale_before,
        ),
    )
    if not follow_user and conversation_id is not None:
        query = query.where(VoiceNotificationOutbox.conversation_id == conversation_id)
    if notification_id is not None:
        query = query.where(VoiceNotificationOutbox.id == notification_id)
    result = await db.execute(query.order_by(VoiceNotificationOutbox.created_at).limit(
        max(1, min(200, int(limit)))))
    rows = list(result.scalars().all())
    notices = []
    for row in rows:
        attempt_id = str(uuid.uuid4())
        # Atomic compare-and-claim: a concurrent replay that already refreshed
        # last_attempt_at loses the conditional UPDATE and cannot double-enqueue.
        claimed = await db.execute(
            update(VoiceNotificationOutbox)
            .where(
                VoiceNotificationOutbox.id == row.id,
                VoiceNotificationOutbox.status == "pending",
                or_(
                    VoiceNotificationOutbox.last_attempt_at.is_(None),
                    VoiceNotificationOutbox.last_attempt_at < stale_before,
                ),
            )
            .values(
                attempt_count=(row.attempt_count or 0) + 1,
                last_attempt_at=datetime.utcnow(),
                delivery_attempt_id=attempt_id,
                delivery_lease_owner="voice-session",
                delivery_lease_expires_at=datetime.utcnow() + timedelta(seconds=stale_after_seconds),
            )
        )
        if claimed.rowcount != 1:
            continue
        await db.commit()
        notices.append({
            "notification_id": row.id,
            "delivery_attempt_id": attempt_id,
            "info": notification_info(row),
        })
    return notices


async def mark_voice_notification_delivered(
    db, notification_id: str, delivery_attempt_id: str | None = None,
) -> bool:
    row = await db.get(VoiceNotificationOutbox, notification_id)
    if row is None or row.status != "pending":
        return False
    if delivery_attempt_id and row.delivery_attempt_id and \
            row.delivery_attempt_id != delivery_attempt_id:
        return False
    row.status = "delivered"
    row.delivered_at = datetime.utcnow()
    row.delivery_lease_owner = None
    row.delivery_lease_expires_at = None
    await db.commit()
    return True


async def list_notifications(
    db, user_id: str, *, include_resolved: bool = False, limit: int = 50,
) -> list[dict]:
    """Web unread/history center (same-user isolation by recipient)."""
    from sqlalchemy import and_

    stmt = select(VoiceNotificationOutbox).where(
        or_(
            VoiceNotificationOutbox.recipient_user_id == user_id,
            and_(
                VoiceNotificationOutbox.recipient_user_id.is_(None),
                VoiceNotificationOutbox.user_id == user_id,
            ),
        ),
    )
    if not include_resolved:
        stmt = stmt.where(VoiceNotificationOutbox.status == "pending")
    stmt = stmt.order_by(desc(VoiceNotificationOutbox.created_at)).limit(
        max(1, min(200, int(limit))))
    rows = list((await db.execute(stmt)).scalars().all())
    out = []
    for row in rows:
        payload = notification_info(row)
        out.append({
            "event_id": row.id,
            "logical_task_id": row.logical_task_id or payload.get("logical_task_id"),
            "task_id": row.task_id,
            "event_type": row.event_type,
            "status": row.task_status,
            "status_version": int(row.status_version or 0),
            "assistant_name": payload.get("assistant_name"),
            "title": payload.get("title"),
            "execution_conversation_id": row.execution_conversation_id,
            "output_message_id": row.output_message_id,
            "delivery_channel": row.delivery_channel,
            "outbox_status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    return out


async def dismiss_notification(db, user_id: str, event_id: str) -> bool:
    from sqlalchemy import and_

    row = await db.get(VoiceNotificationOutbox, event_id)
    if row is None:
        return False
    owner = row.recipient_user_id or row.user_id
    if str(owner) != str(user_id):
        return False
    if row.status == "dismissed":
        return True
    row.status = "dismissed"
    row.dismissed_at = datetime.utcnow()
    await db.commit()
    return True


async def reconcile_terminal_outbox(db, *, limit: int = 50) -> int:
    """Backfill terminal events for tasks that finalized without one (§10.1).

    Only events are created; C2 messages/results are never rewritten. Runs are
    idempotent thanks to the `(recipient, logical, version, type)` unique key.
    """
    from sqlalchemy import exists

    from app.db.database import AgentTask

    # Only tasks WITHOUT a terminal event are candidates. Filtering in SQL is
    # essential: an order-by+limit scan that post-filters in Python would be
    # permanently starved by the oldest already-reconciled 50 tasks.
    missing_event = ~exists().where(
        VoiceNotificationOutbox.recipient_user_id == AgentTask.user_id,
        VoiceNotificationOutbox.logical_task_id == AgentTask.id,
        VoiceNotificationOutbox.status_version == AgentTask.status_version,
        VoiceNotificationOutbox.event_type == "terminal",
    )
    rows = list((await db.execute(
        select(AgentTask).where(
            AgentTask.execution_mode == "separate_v1",
            AgentTask.status.in_(("completed", "failed", "cancelled", "timed_out")),
            AgentTask.completed_at.is_not(None),
            missing_event,
        ).order_by(AgentTask.completed_at.asc()).limit(max(1, int(limit)))
    )).scalars().all())
    created = 0
    for task in rows:
        try:
            manifest = json.loads(task.execution_config or "{}")
        except (TypeError, json.JSONDecodeError):
            manifest = {}
        assistant_name = str(
            (manifest.get("assistant") or {}).get("name") or "执行助手")
        row = await record_task_event(
            db, task, event_type="terminal", assistant_name=assistant_name)
        if row is not None:
            created += 1
    if created:
        await db.commit()
    return created
