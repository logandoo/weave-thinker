# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
API routes for background agent tasks.
"""
import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, desc, text
from sqlalchemy.exc import DBAPIError
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime

from app.db.database import get_db, AgentTask, Message, User
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/agent-tasks", tags=["agent-tasks"])


class AgentTaskCreate(BaseModel):
    goal: str
    title: Optional[str] = None
    assistant_id: Optional[str] = None
    conversation_id: Optional[str] = None
    task_type: str = "general"
    iterations_max: int = 30


class TaskSummaryRequest(BaseModel):
    part: int = 0
    max_chars: Optional[int] = None


class TaskPermitRequest(BaseModel):
    tool_name: str
    action_fingerprint: str
    ttl_seconds: int = 300


class AgentTaskResponse(BaseModel):
    id: str
    title: Optional[str] = None
    goal: str
    status: str
    progress: float
    iterations_done: int
    iterations_max: int
    elapsed_seconds: Optional[float] = None
    task_type: str
    result: Optional[str] = None
    error: Optional[str] = None
    output_conversation_id: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    # Voice delegation / separate_v1 additive fields (2026-11-04)
    source_conversation_id: Optional[str] = None
    execution_conversation_id: Optional[str] = None
    execution_mode: Optional[str] = None
    origin_kind: Optional[str] = None
    assistant_id: Optional[str] = None
    assistant_name: Optional[str] = None
    status_version: int = 0
    reasoning_status: Optional[str] = None
    requested_reasoning_effort: Optional[str] = None
    effective_reasoning_effort: Optional[str] = None
    degradation_reason: Optional[str] = None
    output_message_id: Optional[str] = None
    usage_meta: Optional[Dict] = None
    # Wave 2 (2026-11-04)
    waiting_reason: Optional[Dict] = None
    workspace_mode: Optional[str] = None
    workspace_meta: Optional[Dict] = None
    merge_status: Optional[str] = None


def _parse_json(text_value: Optional[str]) -> dict:
    try:
        value = json.loads(text_value or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _task_response(task: AgentTask, *, include_result: bool = True) -> AgentTaskResponse:
    manifest = _parse_json(task.execution_config)
    runtime = _parse_json(task.runtime_meta)
    usage = _parse_json(task.usage_meta)
    assistant = manifest.get("assistant") or {}
    reasoning = manifest.get("reasoning") or {}
    return AgentTaskResponse(
        id=task.id,
        title=task.title,
        goal=task.goal,
        status=task.status,
        progress=task.progress or 0.0,
        iterations_done=task.iterations_done or 0,
        iterations_max=task.iterations_max or 30,
        elapsed_seconds=task.elapsed_seconds,
        task_type=task.task_type,
        result=(task.result if include_result else (task.result[:5000] if task.result else None)),
        error=task.error,
        output_conversation_id=task.output_conversation_id,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        source_conversation_id=task.source_conversation_id,
        execution_conversation_id=task.execution_conversation_id,
        execution_mode=task.execution_mode,
        origin_kind=task.origin_kind,
        assistant_id=task.assistant_id,
        assistant_name=assistant.get("name"),
        status_version=int(task.status_version or 0),
        reasoning_status=task.reasoning_status,
        requested_reasoning_effort=reasoning.get("effort"),
        effective_reasoning_effort=(
            runtime.get("effective_reasoning_effort")
            if task.status in ("completed", "failed", "cancelled", "timed_out")
            else None
        ),
        degradation_reason=runtime.get("degradation_reason"),
        output_message_id=task.output_message_id,
        usage_meta=usage or None,
        waiting_reason=_parse_json(task.waiting_reason) if task.waiting_reason else None,
        workspace_mode=task.workspace_mode,
        workspace_meta=_parse_json(task.workspace_meta) if task.workspace_meta else None,
        merge_status=task.merge_status,
    )


@router.get("", response_model=List[AgentTaskResponse])
async def list_tasks(
    status: Optional[str] = Query(None, description="Filter by status: pending, claimed, running, completed, failed, cancelled"),
    include_completed: bool = Query(False, description="Include completed/failed/cancelled tasks"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = select(AgentTask).where(AgentTask.user_id == current_user.id)
    stmt = stmt.where(AgentTask.task_type != "grilling")
    if status:
        stmt = stmt.where(AgentTask.status == status)
    elif not include_completed:
        stmt = stmt.where(AgentTask.status.notin_(("completed", "failed", "cancelled", "timed_out")))
    stmt = stmt.order_by(desc(AgentTask.created_at)).limit(limit)
    result = await db.execute(stmt)
    tasks = result.scalars().all()
    return [_task_response(t, include_result=False) for t in tasks]


@router.get("/notifications")
async def list_task_notifications(
    include_resolved: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Same-user unread/history center for delegated-task events."""
    from app.services.voice_notification_service import list_notifications

    return await list_notifications(
        db, current_user.id, include_resolved=include_resolved, limit=limit)


@router.post("/notifications/{event_id}/dismiss")
async def dismiss_task_notification(
    event_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.voice_notification_service import dismiss_notification

    if not await dismiss_notification(db, current_user.id, event_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"ok": True, "event_id": event_id}


@router.get("/active")
async def active_task_for_conversation(
    conversation_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """C2 ownership lookup for the frontend steer composer (§7.1)."""
    from app.services.agent_task_submission_service import (
        active_separate_task_for_conversation,
    )

    task = await active_separate_task_for_conversation(db, conversation_id)
    if task is None or str(task.user_id) != str(current_user.id):
        return None
    return {
        "task_id": task.id,
        "status": task.status,
        "status_version": int(task.status_version or 0),
        "waiting_reason": _parse_json(task.waiting_reason) if task.waiting_reason else None,
        "assistant_name": (_parse_json(task.execution_config).get("assistant") or {}).get("name"),
        "execution_conversation_id": task.execution_conversation_id,
    }


@router.get("/{task_id}", response_model=AgentTaskResponse)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(AgentTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(task)


@router.get("/{task_id}/result")
async def task_result_link(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Result link that survives the 7-day AgentTask cleanup (§10.5/§11.4)."""
    from app.db.database import Conversation
    from app.services.task_result_reader import resolve_task_result

    resolved = await resolve_task_result(
        db, user_id=current_user.id, task_ref=task_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Task not found")
    assistant_id = None
    if resolved.execution_conversation_id:
        conv = await db.get(Conversation, resolved.execution_conversation_id)
        assistant_id = getattr(conv, "assistant_id", None) if conv else None
    return {
        "task_id": resolved.task_id,
        "logical_task_id": resolved.logical_task_id,
        "status": resolved.status,
        "status_version": resolved.status_version,
        "assistant_name": resolved.assistant_name,
        "assistant_id": assistant_id,
        "execution_conversation_id": resolved.execution_conversation_id,
        "output_message_id": resolved.output_message_id,
        "result_missing": resolved.result_missing,
        "conversation_deleted": resolved.conversation_deleted,
        "task_record_present": resolved.task_record_present,
    }


@router.post("/{task_id}/summary")
async def summarize_task(
    task_id: str,
    payload: TaskSummaryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Tool-less summary of the authoritative C2 result (§10.5).

    The result is read from C2/output_message (or the same-user outbox when
    the AgentTask row was cleaned up); never from the notification payload.
    """
    from app.services.task_summary_service import summarize_task_result

    outcome = await summarize_task_result(
        db, user_id=current_user.id, task_ref=task_id,
        part=payload.part, max_chars=payload.max_chars,
    )
    if outcome.get("error") == "task_not_found":
        raise HTTPException(status_code=404, detail=outcome.get("message"))
    if outcome.get("error") == "summary_disabled":
        raise HTTPException(status_code=409, detail=outcome.get("message"))
    if outcome.get("error") == "summary_unavailable":
        raise HTTPException(status_code=502, detail=outcome.get("message"))
    return outcome


@router.post("", response_model=AgentTaskResponse)
async def create_task(
    payload: AgentTaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import uuid
    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        title=payload.title,
        goal=payload.goal,
        assistant_id=payload.assistant_id,
        conversation_id=payload.conversation_id,
        task_type=payload.task_type,
        iterations_max=payload.iterations_max,
        status="pending",
        progress=0.0,
        iterations_done=0,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return AgentTaskResponse(
        id=task.id,
        title=task.title,
        goal=task.goal,
        status=task.status,
        progress=0.0,
        iterations_done=0,
        iterations_max=task.iterations_max,
        elapsed_seconds=0.0,
        task_type=task.task_type,
        result=None,
        error=None,
        output_conversation_id=None,
        created_at=task.created_at,
        started_at=None,
        completed_at=None,
    )


@router.post("/{task_id}/messages")
async def append_task_message(
    task_id: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """F1/H2（2026-09-14）：向运行中的后台任务追加补充消息（steer）。

    worker 每 5s 轮询 pending_messages 并推入 AgentLoop 的 interjection_queue，
    在下一迭代边界作为真实 user 消息进入模型。仅 pending/claimed/running 可追加；
    队列上限 10（与 chat interject 一致）。
    """
    # A4.9 I1：行锁串行化追加（与 worker poller 的 FOR UPDATE 互斥），
    # 防并发读-改-写丢消息；A4.9 残余：lock_timeout 防止长完成事务（媒体本地化
    # 等）把本请求无限阻塞——超时返回 409，消息未写入需重试。
    pre = await db.get(AgentTask, task_id)
    if pre is None or pre.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    if pre.execution_mode == "separate_v1":
        from app.services.agent_task_control_service import (
            TaskReferenceError,
            steer_task,
        )

        try:
            return await steer_task(
                db, task_id=task_id, user_id=current_user.id,
                content=str(payload.get("content") or ""),
            )
        except TaskReferenceError as exc:
            status = {
                "task_not_found": 404,
                "content_required": 422,
                "steer_queue_full": 429,
            }.get(exc.code, 409)
            raise HTTPException(status_code=status, detail=exc.message) from exc

    if pre.status not in ("pending", "claimed", "running"):
        raise HTTPException(status_code=409, detail=f"Task not running (status '{pre.status}')")
    try:
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        result = await db.execute(
            select(AgentTask)
            .where(AgentTask.id == task_id)
            .with_for_update()
            # A4.9 复评 Important：pre=db.get 已把实例放进 identity map，
            # 锁定 SELECT 默认不重填未过期属性 → 必须 populate_existing 强制刷新
            .execution_options(populate_existing=True)
        )
        task = result.scalar_one_or_none()
    except DBAPIError as exc:
        await db.rollback()
        if "lock timeout" in str(exc).lower() or "locknotavailable" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Task busy; retry shortly") from exc
        raise
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("pending", "claimed", "running"):
        raise HTTPException(status_code=409, detail=f"Task not running (status '{task.status}')")
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="content is required")
    try:
        messages = json.loads(task.pending_messages) if task.pending_messages else []
        if not isinstance(messages, list):
            messages = []
    except (json.JSONDecodeError, TypeError):
        messages = []
    if len(messages) >= 10:
        raise HTTPException(status_code=429, detail="Too many pending messages")
    item = {"id": str(uuid.uuid4()), "content": content, "created_at": datetime.utcnow().isoformat()}
    messages.append(item)
    task.pending_messages = json.dumps(messages, ensure_ascii=False)
    await db.commit()
    return {"ok": True, "task_id": task_id, "queued": len(messages), "message_id": item["id"]}


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(AgentTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.execution_mode == "separate_v1":
        from app.services.agent_task_control_service import (
            TaskReferenceError,
            cancel_task_control,
        )

        try:
            status_code, body = await cancel_task_control(
                db, task_id=task_id, user_id=current_user.id)
        except TaskReferenceError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        if status_code == 202:
            return JSONResponse(status_code=202, content=body)
        if status_code == 409:
            raise HTTPException(status_code=409, detail=body)
        return body

    if task.status not in ("pending", "claimed", "running"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel task in status '{task.status}'")

    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "task_id": task_id, "status": "cancelled"}


@router.post("/{task_id}/merge")
async def merge_task_worktree_endpoint(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Explicitly merge a completed worktree task back into its base branch."""
    from app.services.git_worktree_service import (
        WorktreeConflict,
        WorktreeError,
        merge_task_worktree,
    )

    task = await db.get(AgentTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.execution_mode != "separate_v1":
        raise HTTPException(status_code=409, detail="Not a delegated task")
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="Only completed tasks can be merged")
    if task.workspace_mode != "worktree":
        raise HTTPException(status_code=409, detail="Task did not run in a worktree")
    try:
        meta = json.loads(task.workspace_meta or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    project_path = str(meta.get("project_path") or "")
    branch = str(meta.get("branch") or "")
    base_branch = str(meta.get("base_branch") or "")
    if not (project_path and branch and base_branch):
        raise HTTPException(status_code=409, detail="Worktree metadata is incomplete")
    try:
        result = await asyncio.to_thread(
            merge_task_worktree, project_path,
            branch=branch, base_branch=base_branch,
        )
    except WorktreeConflict as exc:
        task.merge_status = "conflict"
        task.workspace_meta = json.dumps(
            {**meta, "merge_conflict_files": exc.files}, ensure_ascii=False)
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail={"code": "merge_conflict", "message": str(exc),
                    "files": exc.files},
        ) from exc
    except WorktreeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task.merge_status = "merged"
    task.workspace_meta = json.dumps({**meta, "merge": result}, ensure_ascii=False)
    await db.commit()
    return result


@router.post("/{task_id}/permit")
async def permit_task_action(
    task_id: str,
    payload: TaskPermitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One-time act authorization for a waiting_auth delegated task (§13.1)."""
    from app.services.agent_task_control_service import (
        TaskReferenceError,
        grant_task_permit,
    )

    try:
        return await grant_task_permit(
            db, task_id=task_id, user_id=current_user.id,
            tool_name=payload.tool_name,
            action_fingerprint=payload.action_fingerprint,
            ttl_seconds=payload.ttl_seconds,
        )
    except TaskReferenceError as exc:
        status = 404 if exc.code == "task_not_found" else 409
        raise HTTPException(status_code=status, detail=exc.message) from exc


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(AgentTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.execution_mode == "separate_v1" and task.status in (
        "pending", "claimed", "running", "resumable",
        "waiting_input", "waiting_auth", "waiting_budget", "cancelling",
    ):
        raise HTTPException(
            status_code=409,
            detail="Active task cannot be deleted; cancel it first",
        )
    await db.delete(task)
    await db.commit()
    return {"ok": True, "task_id": task_id}


class GrillingAnswerRequest(BaseModel):
    answer: str


class GrillingRoundAnswerRequest(BaseModel):
    answers: List[Dict[str, str]]


@router.get("/grilling/{conversation_id}")
async def get_grilling_questions(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.db.database import Conversation
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    stmt = (
        select(AgentTask)
        .where(
            AgentTask.conversation_id == conversation_id,
            AgentTask.task_type == "grilling",
        )
        .order_by(AgentTask.created_at)
    )
    result = await db.execute(stmt)
    tasks = result.scalars().all()

    questions = []
    for t in tasks:
        ctx = {}
        if t.context:
            try:
                import json
                ctx = json.loads(t.context)
            except Exception:
                pass
        questions.append({
            "task_id": t.id,
            "question_id": ctx.get("question_id", ""),
            "question": ctx.get("question", t.goal or ""),
            "recommendation": ctx.get("recommendation", ""),
            "options": ctx.get("options", []),
            "round": ctx.get("grilling_round", 1),
            "status": t.status,
            "answer": t.result if t.status == "completed" else None,
        })

    return {
        "conversation_id": conversation_id,
        "deathmatch_status": conv.deathmatch_status,
        "grilling_total": conv.deathmatch_grilling_total or 0,
        "grilling_completed": conv.deathmatch_grilling_completed or 0,
        "grilling_round": conv.deathmatch_grilling_round or 0,
        "grilling_round_total": conv.deathmatch_grilling_round_total or 3,
        "questions": questions,
    }


@router.post("/grilling/{task_id}/answer")
async def answer_grilling_question(
    task_id: str,
    payload: GrillingAnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json
    from app.db.database import Conversation
    from app.services.deathmatch_service import DeathmatchManager

    task = await db.get(AgentTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Grilling task not found")
    if task.task_type != "grilling":
        raise HTTPException(status_code=400, detail="Not a grilling task")
    if task.status == "completed":
        raise HTTPException(status_code=400, detail="Question already answered")

    conv = await db.get(Conversation, task.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    dm = DeathmatchManager(conv)
    result = await dm.complete_grilling_question(task_id, payload.answer, db)
    await db.commit()

    return {
        "ok": True,
        "task_id": task_id,
        "result": result,
        "grilling_completed": conv.deathmatch_grilling_completed,
        "grilling_total": conv.deathmatch_grilling_total,
        "grilling_round": conv.deathmatch_grilling_round,
        "grilling_round_total": conv.deathmatch_grilling_round_total,
        "deathmatch_status": conv.deathmatch_status,
    }


@router.post("/grilling/{conversation_id}/round-answer")
async def answer_grilling_round(
    conversation_id: str,
    payload: GrillingRoundAnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.db.database import Conversation
    from app.services.deathmatch_service import DeathmatchManager

    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    dm = DeathmatchManager(conv)
    result = await dm.submit_grilling_round(payload.answers, db)
    await db.commit()

    return {
        "ok": True,
        "result": result,
        "grilling_completed": conv.deathmatch_grilling_completed,
        "grilling_total": conv.deathmatch_grilling_total,
        "grilling_round": conv.deathmatch_grilling_round,
        "grilling_round_total": conv.deathmatch_grilling_round_total,
        "deathmatch_status": conv.deathmatch_status,
    }
