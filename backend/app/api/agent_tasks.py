# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
API routes for background agent tasks.
"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, desc, text
from sqlalchemy.exc import DBAPIError
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime

from app.db.database import get_db, AgentTask, User
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/agent-tasks", tags=["agent-tasks"])


class AgentTaskCreate(BaseModel):
    goal: str
    title: Optional[str] = None
    assistant_id: Optional[str] = None
    conversation_id: Optional[str] = None
    task_type: str = "general"
    iterations_max: int = 30


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
        stmt = stmt.where(AgentTask.status.notin_(("completed", "failed", "cancelled")))
    stmt = stmt.order_by(desc(AgentTask.created_at)).limit(limit)
    result = await db.execute(stmt)
    tasks = result.scalars().all()
    return [AgentTaskResponse(
        id=t.id,
        title=t.title,
        goal=t.goal,
        status=t.status,
        progress=t.progress or 0.0,
        iterations_done=t.iterations_done or 0,
        iterations_max=t.iterations_max or 30,
        elapsed_seconds=t.elapsed_seconds,
        task_type=t.task_type,
        result=t.result[:5000] if t.result else None,
        error=t.error,
        output_conversation_id=t.output_conversation_id,
        created_at=t.created_at,
        started_at=t.started_at,
        completed_at=t.completed_at,
    ) for t in tasks]


@router.get("/{task_id}", response_model=AgentTaskResponse)
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(AgentTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
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
        result=task.result if task.result else None,
        error=task.error,
        output_conversation_id=task.output_conversation_id,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


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
    if task.status not in ("pending", "claimed", "running"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel task in status '{task.status}'")

    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "task_id": task_id, "status": "cancelled"}


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(AgentTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
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
