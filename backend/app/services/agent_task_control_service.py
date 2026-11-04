# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Task control-plane service shared by REST and voice tools (Wave 2 P1).

One implementation for reference resolution, steer, cancel, waiting permits and
worktree merge checks. Voice is a thin adapter: it must never re-implement
ownership, state transitions or 409 semantics.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.db.database import AgentTask, AgentTaskPermit, Message

logger = logging.getLogger(__name__)

MAX_STEER_QUEUE = 10


class TaskReferenceError(Exception):
    def __init__(self, code: str, message: str, *, candidates: Optional[list] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []

    def as_dict(self) -> dict:
        return {
            "error": self.code,
            "message": self.message,
            "candidates": self.candidates,
        }


def _norm(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _candidate(task: AgentTask) -> dict:
    return {
        "task_id": task.id,
        "title": task.title or "",
        "status": task.status,
        "assistant_id": task.assistant_id,
        "execution_conversation_id": task.execution_conversation_id,
    }


async def resolve_task_reference(
    db, *, user_id: str, ref: Optional[str] = None,
    statuses: Optional[Iterable[str]] = None,
) -> AgentTask:
    """Resolve exactly one user-owned task, or raise an explicit error.

    Order: exact id → exact normalised title → substring of title/goal. An
    ambiguous match NEVER picks the first row; the caller must ask the user.
    With no ref and an active-status filter, exactly one active task is
    acceptable; multiple active tasks require clarification.
    """
    value = str(ref or "").strip()
    if value:
        direct = await db.get(AgentTask, value)
        if direct is not None and str(direct.user_id) == str(user_id):
            return direct

    stmt = select(AgentTask).where(
        AgentTask.user_id == user_id,
        AgentTask.task_type != "grilling",
    )
    if statuses is not None:
        stmt = stmt.where(AgentTask.status.in_(tuple(statuses)))
    stmt = stmt.order_by(AgentTask.created_at.desc()).limit(100)
    tasks = list((await db.execute(stmt)).scalars().all())

    if not value:
        if not tasks:
            raise TaskReferenceError("task_not_found", "没有找到可用的委派任务")
        if len(tasks) == 1:
            return tasks[0]
        active = [t for t in tasks if str(t.status) in (
            "pending", "claimed", "running", "resumable",
            "waiting_input", "waiting_auth", "waiting_budget", "cancelling")]
        if statuses is not None and len(active) == 1:
            return active[0]
        raise TaskReferenceError(
            "task_ambiguous", "有多个任务匹配，请说明是哪一项",
            candidates=[_candidate(t) for t in (active or tasks)[:5]],
        )

    key = _norm(value)
    exact = [t for t in tasks if _norm(t.title) == key]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise TaskReferenceError(
            "task_ambiguous", "有多个同名任务，请说明是哪一项",
            candidates=[_candidate(t) for t in exact[:5]],
        )
    partial = [
        t for t in tasks
        if key and (key in _norm(t.title) or key in _norm(t.goal))
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise TaskReferenceError(
            "task_ambiguous", "有多个任务匹配这个描述，请说明是哪一项",
            candidates=[_candidate(t) for t in partial[:5]],
        )
    raise TaskReferenceError("task_not_found", "没有找到匹配的任务")


async def _lock_task(db, task_id: str) -> Optional[AgentTask]:
    try:
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        result = await db.execute(
            select(AgentTask)
            .where(AgentTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()
    except DBAPIError as exc:
        await db.rollback()
        if "lock timeout" in str(exc).lower() or "locknotavailable" in str(exc).lower():
            raise TaskReferenceError("task_busy", "任务正忙，请稍后重试")
        raise


async def steer_task(
    db, *, task_id: str, user_id: str, content: str,
) -> dict:
    """Shared steer implementation (§7.2). Waiting tasks wake on steer."""
    from app.services.agent_task_finalize import can_steer

    text_value = str(content or "").strip()
    if not text_value:
        raise TaskReferenceError("content_required", "补充内容不能为空")
    task = await _lock_task(db, task_id)
    if task is None or str(task.user_id) != str(user_id):
        raise TaskReferenceError("task_not_found", "没有找到该任务")
    if str(task.execution_mode or "") != "separate_v1":
        raise TaskReferenceError("not_separate_task", "该任务不是独立执行任务")
    if not can_steer(task):
        raise TaskReferenceError(
            "task_not_steerable", f"任务当前状态（{task.status}）不接受补充要求")
    if not task.execution_conversation_id:
        raise TaskReferenceError(
            "execution_conversation_missing", "执行会话缺失；不会回退到来源会话")

    try:
        items = json.loads(task.pending_messages) if task.pending_messages else []
        if not isinstance(items, list):
            items = []
    except (json.JSONDecodeError, TypeError):
        items = []
    if len(items) >= MAX_STEER_QUEUE:
        raise TaskReferenceError("steer_queue_full", "待处理补充要求已满")

    message = Message(
        id=str(uuid.uuid4()),
        conversation_id=task.execution_conversation_id,
        role="user",
        content=text_value,
    )
    db.add(message)
    await db.flush()
    items.append({
        "id": message.id,
        "content": text_value,
        "created_at": datetime.utcnow().isoformat(),
    })
    task.pending_messages = json.dumps(items, ensure_ascii=False)
    woke = False
    if str(task.status or "") in (
        "waiting_input", "waiting_auth", "waiting_budget",
    ):
        # A steer to a parked task is the user's reply: wake a new attempt.
        task.status = "resumable"
        task.waiting_reason = None
        task.attempt_id = None
        task.lease_until = None
        task.heartbeat_at = None
        task.status_version = int(task.status_version or 0) + 1
        woke = True
    task.updated_at = datetime.utcnow()
    await db.commit()
    return {
        "ok": True,
        "accepted": True,
        "task_id": task.id,
        "message_id": message.id,
        "queued": len(items),
        "status": "queued" if not woke else "resumable",
        "woke": woke,
        "status_version": int(task.status_version or 0),
    }


async def cancel_task_control(db, *, task_id: str, user_id: str) -> tuple[int, dict]:
    """Shared cancel implementation. Returns (http_status, payload)."""
    from app.services.agent_task_finalize import (
        cancel_decision,
        finalize_separate_task,
    )

    task = await db.get(AgentTask, task_id)
    if task is None or str(task.user_id) != str(user_id):
        raise TaskReferenceError("task_not_found", "没有找到该任务")
    if str(task.execution_mode or "") != "separate_v1":
        if str(task.status or "") not in ("pending", "claimed", "running"):
            raise TaskReferenceError(
                "cancel_too_late", f"任务已处于 {task.status}，无法取消")
        task.status = "cancelled"
        task.completed_at = datetime.utcnow()
        await db.commit()
        return 200, {"ok": True, "task_id": task_id, "status": "cancelled"}

    for _attempt in range(2):
        decision = cancel_decision(task)
        if decision == "cancelled_now":
            outcome = await finalize_separate_task(
                db, task_id=task_id, status="cancelled",
                require_statuses=(
                    "pending", "resumable", "waiting_input",
                    "waiting_auth", "waiting_budget",
                ),
                error="用户请求取消（尚未开始执行）",
            )
            if outcome is not None:
                return 200, {
                    "ok": True, "task_id": task_id, "status": "cancelled",
                    "status_version": outcome.get("status_version"),
                }
            # The state moved between read and finalize (e.g. steer woke it).
            # Re-read and retry the correct transition instead of reporting a
            # false too_late to an active task.
            await db.refresh(task)
            continue
        if decision in ("cancelling", "already_cancelling"):
            if decision == "cancelling":
                res = await db.execute(
                    update(AgentTask)
                    .where(
                        AgentTask.id == task_id,
                        AgentTask.user_id == user_id,
                        AgentTask.status.in_(("claimed", "running")),
                        AgentTask.cancel_requested_at.is_(None),
                    )
                    .values(
                        status="cancelling",
                        cancel_requested_at=datetime.utcnow(),
                        status_version=AgentTask.status_version + 1,
                        updated_at=datetime.utcnow(),
                    )
                    .returning(AgentTask.status_version)
                )
                new_version = res.scalar_one_or_none()
                await db.commit()
                if new_version is None:
                    await db.refresh(task)
                    if task.status in ("pending", "resumable", "waiting_input",
                                       "waiting_auth", "waiting_budget"):
                        continue
                    if task.status in ("claimed", "running"):
                        return 409, {
                            "ok": False, "task_id": task_id,
                            "status": "changed_state",
                            "message": "任务状态正在变化，请重试取消",
                        }
                    if task.status != "cancelling":
                        return 200, {"ok": False, "task_id": task_id,
                                      "status": "too_late",
                                      "message": f"Task already {task.status}"}
                    new_version = int(task.status_version or 0)
                return 202, {
                    "ok": True, "task_id": task_id, "status": "cancelling",
                    "status_version": int(new_version or 0),
                    "message": "已请求取消，正在等待执行安全停下",
                }
            await db.refresh(task)
            return 202, {
                "ok": True, "task_id": task_id, "status": "cancelling",
                "status_version": int(task.status_version or 0),
                "message": "已请求取消，正在等待执行安全停下",
            }
        break

    await db.refresh(task)
    if task.status in ("pending", "resumable", "waiting_input",
                       "waiting_auth", "waiting_budget", "claimed", "running"):
        return 409, {
            "ok": False, "task_id": task_id, "status": "changed_state",
            "message": "任务状态正在变化，请重试取消",
        }
    return 200, {"ok": False, "task_id": task_id, "status": "too_late",
                 "message": f"Task already {task.status}"}


async def grant_task_permit(
    db, *, task_id: str, user_id: str, tool_name: str,
    action_fingerprint: str, ttl_seconds: int = 300,
) -> dict:
    """One-time authorization for a waiting_auth task (§13.1)."""
    try:
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
    except DBAPIError:
        await db.rollback()
        raise TaskReferenceError("task_busy", "任务正忙，请稍后重试")
    task = (await db.execute(
        select(AgentTask)
        .where(AgentTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if task is None or str(task.user_id) != str(user_id):
        raise TaskReferenceError("task_not_found", "没有找到该任务")
    if str(task.status or "") != "waiting_auth":
        raise TaskReferenceError(
            "task_not_waiting_auth", f"任务当前状态（{task.status}）不需要授权")
    waiting = {}
    try:
        waiting = json.loads(task.waiting_reason or "{}")
    except (TypeError, json.JSONDecodeError):
        waiting = {}
    expected_tool = str(waiting.get("tool_name") or "")
    expected_fp = str(waiting.get("action_fingerprint") or "")
    if not expected_tool or not expected_fp:
        raise TaskReferenceError(
            "permit_binding_missing",
            "等待原因没有绑定具体工具与动作指纹；不能用补充要求代替授权，请让任务重新请求",
        )
    if not tool_name or not action_fingerprint:
        raise TaskReferenceError("permit_binding_required", "授权必须绑定工具与动作指纹")
    if expected_tool != tool_name:
        raise TaskReferenceError("permit_tool_mismatch", "授权工具与等待中的动作不一致")
    if expected_fp != action_fingerprint:
        raise TaskReferenceError("permit_fingerprint_mismatch", "授权动作与等待中的动作不一致")

    permit = AgentTaskPermit(
        task_id=task.id,
        user_id=user_id,
        tool_name=tool_name,
        action_fingerprint=action_fingerprint,
        expires_at=datetime.utcnow() + timedelta(seconds=max(30, min(3600, int(ttl_seconds)))),
    )
    db.add(permit)
    await db.flush()
    # The model must RETRY the blocked action: a permit alone is not visible in
    # the resumed context. Ride the durable inbox (not a forged user message)
    # so the next attempt sees the authorization and re-dispatches the call.
    try:
        items = json.loads(task.pending_messages) if task.pending_messages else []
        if not isinstance(items, list):
            items = []
    except (TypeError, json.JSONDecodeError):
        items = []
    items.append({
        "id": f"permit:{permit.id}",
        "content": (
            f"[系统授权] 用户已授权执行工具 {tool_name}"
            f"（动作指纹 {str(action_fingerprint)[:12]}）。请重试该动作；"
            "不要把它扩展到其他操作。"
        ),
        "created_at": datetime.utcnow().isoformat(),
        "source": "task_permit",
    })
    task.pending_messages = json.dumps(items, ensure_ascii=False)
    task.status = "resumable"
    task.waiting_reason = None
    task.attempt_id = None
    task.lease_until = None
    task.heartbeat_at = None
    task.status_version = int(task.status_version or 0) + 1
    task.updated_at = datetime.utcnow()
    await db.flush()
    await db.commit()
    return {
        "permit_id": permit.id,
        "task_id": task.id,
        "expires_at": permit.expires_at.isoformat(),
        "status": task.status,
        "status_version": int(task.status_version or 0),
    }


async def has_task_permit(
    task_id: str, *, user_id: str, tool_name: str, action_fingerprint: str,
) -> bool:
    """Read-only check used by crash recovery before skipping a recorded call."""
    from sqlalchemy import select
    from app.db.database import AgentTaskPermit, AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(AgentTaskPermit.id).where(
                    AgentTaskPermit.task_id == task_id,
                    AgentTaskPermit.user_id == user_id,
                    AgentTaskPermit.tool_name == tool_name,
                    AgentTaskPermit.action_fingerprint == action_fingerprint,
                    AgentTaskPermit.consumed_at.is_(None),
                    AgentTaskPermit.expires_at > datetime.utcnow(),
                ).limit(1)
            )).scalar_one_or_none()
            return row is not None
    except Exception:
        logger.warning("task permit lookup failed; fail closed", exc_info=True)
        return False


async def consume_task_permit(
    db, *, task_id: str, user_id: str, tool_name: str, action_fingerprint: str,
) -> bool:
    result = await db.execute(
        update(AgentTaskPermit)
        .where(
            AgentTaskPermit.task_id == task_id,
            AgentTaskPermit.user_id == user_id,
            AgentTaskPermit.tool_name == tool_name,
            AgentTaskPermit.action_fingerprint == action_fingerprint,
            AgentTaskPermit.consumed_at.is_(None),
            AgentTaskPermit.expires_at > datetime.utcnow(),
        )
        .values(consumed_at=datetime.utcnow())
    )
    await db.commit()
    return (result.rowcount or 0) == 1
