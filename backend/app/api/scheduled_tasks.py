# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Scheduled tasks API: CRUD + manual trigger."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone, timedelta

from app.db.database import get_db, User, ScheduledTask
from app.core.deps import get_current_user
from app.services.schedule_parser import parse_schedule_agentic

_TZ_CN = timezone(timedelta(hours=8))

router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled-tasks"])


# --- Schemas ---

class ScheduledTaskCreate(BaseModel):
    name: str
    prompt: str
    schedule_text: str  # Natural language schedule, e.g. "每天上午9点"
    assistant_id: Optional[str] = None
    repeat_count: Optional[int] = None


class ScheduledTaskUpdate(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    schedule_text: Optional[str] = None
    status: Optional[str] = None
    repeat_count: Optional[int] = None


class ScheduledTaskResponse(BaseModel):
    id: str
    name: str
    prompt: str
    schedule_type: str
    schedule_expr: str
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    status: str
    repeat_count: Optional[int] = None
    run_count: int
    assistant_id: Optional[str] = None
    conversation_id: Optional[str] = None
    created_at: str


# --- Endpoints ---

@router.get("", response_model=List[ScheduledTaskResponse])
async def list_scheduled_tasks(
    status: Optional[str] = Query(None, description="Filter by status: active, paused, cancelled, completed, failed"),
    include_completed: bool = Query(False, description="Include completed/failed/cancelled tasks"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = select(ScheduledTask).where(ScheduledTask.user_id == current_user.id)
    if status:
        stmt = stmt.where(ScheduledTask.status == status)
    elif not include_completed:
        stmt = stmt.where(ScheduledTask.status.notin_(("completed", "failed", "cancelled")))
    stmt = stmt.order_by(desc(ScheduledTask.created_at))
    result = await db.execute(stmt)
    tasks = result.scalars().all()
    return [_to_response(t) for t in tasks]


@router.post("", response_model=ScheduledTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_scheduled_task(
    body: ScheduledTaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    parsed = await parse_schedule_agentic(body.schedule_text)
    if not parsed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='无法解析调度表达式，请使用如"每天上午9点"、"每2小时"等格式',
        )
    schedule_type, schedule_expr, next_run_at, _repeat = parsed

    if next_run_at and hasattr(next_run_at, 'tzinfo') and next_run_at.tzinfo is not None:
        next_run_at = next_run_at.replace(tzinfo=None)

    task = ScheduledTask(
        user_id=current_user.id,
        assistant_id=body.assistant_id,
        name=body.name,
        prompt=body.prompt,
        schedule_type=schedule_type,
        schedule_expr=schedule_expr,
        next_run_at=next_run_at,
        repeat_count=body.repeat_count,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return _to_response(task)


@router.put("/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    task_id: str,
    body: ScheduledTaskUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _get_user_task(db, task_id, current_user.id)

    if body.name is not None:
        task.name = body.name
    if body.prompt is not None:
        task.prompt = body.prompt
    if body.status is not None:
        if body.status not in ("active", "paused", "cancelled"):
            raise HTTPException(status_code=400, detail="status must be 'active' or 'paused'")
        task.status = body.status
    if body.repeat_count is not None:
        task.repeat_count = body.repeat_count
    if body.schedule_text is not None:
        parsed = await parse_schedule_agentic(body.schedule_text)
        if not parsed:
            raise HTTPException(status_code=400, detail="无法解析调度表达式")
        schedule_type, schedule_expr, next_run_at, _repeat = parsed
        if next_run_at is not None and next_run_at.tzinfo is not None:
            next_run_at = next_run_at.replace(tzinfo=None)
        task.schedule_type = schedule_type
        task.schedule_expr = schedule_expr
        task.next_run_at = next_run_at

    await db.commit()
    await db.refresh(task)
    return _to_response(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheduled_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _get_user_task(db, task_id, current_user.id)
    await db.delete(task)
    await db.commit()


@router.post("/{task_id}/trigger", response_model=ScheduledTaskResponse)
async def trigger_scheduled_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually trigger a scheduled task immediately."""
    task = await _get_user_task(db, task_id, current_user.id)
    from app.services.agent_scheduler import agent_scheduler
    now = datetime.now(_TZ_CN).replace(tzinfo=None)
    await agent_scheduler._execute_scheduled_task(db, task, now)
    await db.refresh(task)
    return _to_response(task)


# --- Helpers ---

async def _get_user_task(db: AsyncSession, task_id: str, user_id: str) -> ScheduledTask:
    task = await db.get(ScheduledTask, task_id)
    if not task or task.user_id != user_id:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return task


def _to_response(t: ScheduledTask) -> ScheduledTaskResponse:
    return ScheduledTaskResponse(
        id=t.id,
        name=t.name,
        prompt=t.prompt,
        schedule_type=t.schedule_type,
        schedule_expr=t.schedule_expr,
        next_run_at=t.next_run_at.isoformat() if t.next_run_at else None,
        last_run_at=t.last_run_at.isoformat() if t.last_run_at else None,
        status=t.status,
        repeat_count=t.repeat_count,
        run_count=t.run_count or 0,
        assistant_id=t.assistant_id,
        conversation_id=t.conversation_id,
        created_at=t.created_at.isoformat() if t.created_at else "",
    )
