# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete, or_, func, and_, update
from sqlalchemy.orm import selectinload
from typing import List, Optional
import os
import csv
import re
import io
import zipfile
import logging
from datetime import datetime
from urllib.parse import quote

from app.db.database import get_db, Conversation, Message, ChatSession, User, Assistant, ScheduledTask, ConversationGroup
from app.schemas.chat import (
    ConversationCreate,
    ConversationUpdate,
    ConversationResponse,
    MessageResponse,
    ConversationWithMessages,
    ConversationSearchResult,
    MatchedMessage,
    ConversationGroupCreate,
    ConversationGroupUpdate,
    ConversationGroupResponse,
    ConversationMoveRequest,
    GroupMoveRequest,
    ConversationReorderRequest,
    ConversationGroupReorderRequest,
)
from app.core.deps import get_current_user
from app.api.notes import (
    _markdown_to_html_with_mermaid,
    _ensure_heading_ids,
    _PDF_CSS,
    _materialize_note_images,
    _get_note_workspace_root,
    _strip_media_tags,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


async def _reconcile_deathmatch_status(db: AsyncSession, conversation: Conversation) -> None:
    """Self-heal zombie deathmatch state.

    When a deathmatch goal-loop or grilling phase is interrupted (SSE
    cancellation, agent-task cancel, server crash, network disconnect, or
    unhandled agent-loop exception), the ``deathmatch_status`` column may be
    left at ``"active"`` or ``"grilling"`` even though no agent is actually
    running. The frontend then polls forever showing a stuck status with no
    progress — the zombie-conversation symptom.

    This reconciliation runs on every conversation read:
    - For ``"active"``: if no agent/buffer running → set to ``"paused"``
    - For ``"grilling"``: only pause if there are NO pending grilling tasks
      AND completed tasks exist (zombie: all answered, no synthesis).
      If pending tasks still exist, the user is normally answering questions
      and the "grilling" status is correct.
    """
    if not conversation.deathmatch_mode:
        return
    current_status = conversation.deathmatch_status or "inactive"
    if current_status not in ("active", "grilling"):
        return
    try:
        from app.services.active_agent_registry import ActiveAgentRegistry
        from app.services.stream_buffer import stream_buffer_manager
        registry = ActiveAgentRegistry.get_instance()
        state = await registry.get(conversation.id)
        if state is not None and state.is_running:
            return
        buf = await stream_buffer_manager.get_buffer_no_auth(conversation.id)
        if buf is not None and getattr(buf, "is_running", False):
            return
    except Exception:
        logger.warning(
            "deathmatch status reconcile check failed for %s",
            conversation.id, exc_info=True,
        )
        return

    # Race guard: a just-accepted /chat/stream request bumps `updated_at`
    # (user message save + deathmatch lifecycle commit) BEFORE the agent
    # registers in ActiveAgentRegistry (the registration happens inside the
    # streaming generator, after LLM setup). A poll landing in that window
    # would see status="active" with no registered agent and wrongly flip it
    # to "paused" — observed 2026-07-18 (conv aadb26a3): reconcile fired 8ms
    # before agent registration, silently downgrading the run to a plain
    # agent loop. Skip reconciliation when the conversation was written to
    # very recently; a genuine zombie stays stale and is caught on the next
    # poll once the grace window passes.
    from datetime import datetime, timezone
    updated_at = getattr(conversation, "updated_at", None)
    if updated_at is not None:
        ua = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ua).total_seconds()
        if age < 60:
            return

    # For "grilling": only pause if this is a genuine zombie (all tasks
    # completed but goal not synthesized).  If there are still pending
    # tasks, the user is answering questions — keep status as-is.
    if current_status == "grilling":
        from app.db.database import AgentTask
        from sqlalchemy import select
        stmt = select(AgentTask).where(
            AgentTask.conversation_id == conversation.id,
            AgentTask.task_type == "grilling",
            AgentTask.status == "pending",
        )
        result = await db.execute(stmt)
        pending = result.scalars().all()
        if pending:
            return
        stmt = select(AgentTask).where(
            AgentTask.conversation_id == conversation.id,
            AgentTask.task_type == "grilling",
            AgentTask.status == "completed",
        )
        result = await db.execute(stmt)
        completed = result.scalars().all()
        if not completed:
            return

    prev = conversation.deathmatch_status
    if current_status == "active":
        # I2/N4: a WAIT-parked goal loop (judge said wait) is intentionally
        # inactive — do not flip it to paused while the park is fresh
        # (6h TTL); an OLD wait park is a zombie → fall through to the
        # normal paused/human_gate reconciliation.
        if getattr(conversation, "deathmatch_verdict", None) == "wait":
            from datetime import datetime, timezone
            ua = getattr(conversation, "updated_at", None)
            if ua is not None:
                ua = ua if ua.tzinfo else ua.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - ua).total_seconds() < 6 * 3600:
                    return
        # D1: wall-clock aware zombie recovery — an active goal loop whose
        # wall time has elapsed (process died mid-run, nobody reconciled)
        # should surface as human_gate with a visible reason, not a silent
        # paused flip the user cannot explain (conv 6b0faf81 class failure).
        try:
            from app.services.deathmatch_service import DeathmatchManager
            if DeathmatchManager(conversation).wall_time_exceeded():
                conversation.deathmatch_status = "human_gate"
                conversation.deathmatch_reason = (
                    "墙钟超限（进程中断后自愈）— 发送任意消息继续或调整目标"
                )
                await db.commit()
                return
        except Exception:
            logger.warning(
                "deathmatch wall-time reconcile check failed for %s",
                conversation.id, exc_info=True,
            )
    conversation.deathmatch_status = "paused"
    # conv 6b0faf81: a silent active→paused flip gives the user no idea why
    # the goal loop stopped. Record an explicit, user-visible reason so the
    # status message explains it and resume is a one-tap "继续".
    if not conversation.deathmatch_reason:
        conversation.deathmatch_reason = (
            "死磕模式执行中断（未检测到正在运行的 agent）。发送任意消息可继续推进目标。"
        )
    await db.commit()
    logger.info(
        "Reconciled stuck deathmatch_status %r->'paused' for conversation %s",
        prev, conversation.id,
    )


class BulkDeleteRequest(BaseModel):
    conversation_ids: List[str]


class BulkDeleteResponse(BaseModel):
    status: str
    deleted_count: int


class BulkDeleteGroupsRequest(BaseModel):
    group_ids: List[str]
    delete_conversations: bool = False


class ExportMessagesPDFRequest(BaseModel):
    title: str = ""
    content: str = ""
    role: str = "assistant"


class ExportMessagesPDFBulkRequest(BaseModel):
    items: List[ExportMessagesPDFRequest]
    action: str = "single"  # "single" or "bulk"


def _chunked_ids(values: List[str], size: int = 200):
    for index in range(0, len(values), size):
        yield values[index:index + size]


async def _get_last_user_message_times(
    db: AsyncSession, conversation_ids: List[str]
) -> dict[str, Optional[datetime]]:
    if not conversation_ids:
        return {}
    result = await db.execute(
        select(Message.conversation_id, func.max(Message.created_at).label("last_user_message_at"))
        .where(Message.conversation_id.in_(conversation_ids), Message.role == "user")
        .group_by(Message.conversation_id)
    )
    return {row[0]: row[1] for row in result.all()}


def _utc_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat() + 'Z'


def _conversation_response(
    conversation: Conversation, last_user_message_at: Optional[datetime] = None
) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        title=conversation.title,
        group_id=conversation.group_id,
        assistant_id=conversation.assistant_id,
        sort_order=conversation.sort_order or 0,
        created_at=_utc_iso(conversation.created_at),
        updated_at=_utc_iso(conversation.updated_at),
        last_user_message_at=_utc_iso(last_user_message_at),
        deathmatch_mode=conversation.deathmatch_mode or False,
        deathmatch_status=conversation.deathmatch_status or "inactive",
        deathmatch_reason=conversation.deathmatch_reason,
        deathmatch_goal=conversation.deathmatch_goal,
        deathmatch_turns=conversation.deathmatch_turns or 0,
        deathmatch_max_turns=conversation.deathmatch_max_turns or 30,
        deathmatch_grilling_total=conversation.deathmatch_grilling_total or 0,
        deathmatch_grilling_completed=conversation.deathmatch_grilling_completed or 0,
        deathmatch_grilling_round=conversation.deathmatch_grilling_round or 0,
        deathmatch_grilling_round_total=conversation.deathmatch_grilling_round_total or 3,
        deathmatch_context_summary=conversation.deathmatch_context_summary,
        deathmatch_expected_marker=conversation.deathmatch_expected_marker,
        deathmatch_marker_miss_count=conversation.deathmatch_marker_miss_count or 0,
        deathmatch_compressed_context=conversation.deathmatch_compressed_context,
        deathmatch_plan=conversation.deathmatch_plan,
        deathmatch_plan_version=conversation.deathmatch_plan_version or 0,
    )


@router.get("", response_model=List[ConversationResponse])
async def list_conversations(
    assistant_id: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    last_msg_subq = (
        select(Message.conversation_id, func.max(Message.created_at).label("last_user_message_at"))
        .where(Message.role == "user")
        .group_by(Message.conversation_id)
        .subquery()
    )
    query = (
        select(Conversation, last_msg_subq.c.last_user_message_at)
        .outerjoin(last_msg_subq, Conversation.id == last_msg_subq.c.conversation_id)
        .where(Conversation.user_id == current_user.id)
    )
    if assistant_id:
        query = query.where(Conversation.assistant_id == assistant_id)
    query = query.order_by(
        Conversation.sort_order.asc(),
        desc(func.coalesce(last_msg_subq.c.last_user_message_at, Conversation.updated_at))
    )
    result = await db.execute(query)
    rows = result.all()
    return [
        _conversation_response(conversation, last_user_message_at)
        for conversation, last_user_message_at in rows
    ]


@router.post("", response_model=ConversationResponse)
async def create_conversation(
    conversation_data: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = Conversation(
        user_id=current_user.id,
        title=conversation_data.title or "新对话",
        assistant_id=conversation_data.assistant_id
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return _conversation_response(conversation)


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_conversations(
    delete_data: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation_ids = list(dict.fromkeys(delete_data.conversation_ids))
    if not conversation_ids:
        raise HTTPException(status_code=400, detail="No conversations selected")

    result = await db.execute(
        select(Conversation.id).where(
            Conversation.user_id == current_user.id,
            Conversation.id.in_(conversation_ids),
        )
    )
    valid_ids = [row[0] for row in result.all()]
    if not valid_ids:
        raise HTTPException(status_code=404, detail="Conversation not found")

    for batch_ids in _chunked_ids(valid_ids):
        await db.execute(
            update(ScheduledTask)
            .where(and_(
                ScheduledTask.conversation_id.in_(batch_ids),
                ScheduledTask.status == "active",
            ))
            .values(status="cancelled", next_run_at=None)
        )
        await db.execute(
            delete(Message).where(Message.conversation_id.in_(batch_ids))
        )
        await db.execute(
            delete(ChatSession).where(ChatSession.conversation_id.in_(batch_ids))
        )
        await db.execute(
            delete(Conversation).where(Conversation.id.in_(batch_ids))
        )

    await db.commit()
    return BulkDeleteResponse(status="ok", deleted_count=len(valid_ids))


@router.get("/search", response_model=List[ConversationSearchResult])
async def search_conversations(
    q: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not q or not q.strip():
        return []

    keyword = f"%{q.strip()}%"
    query_terms = q.strip()

    # Use PostgreSQL full-text search (tsvector/ts_rank_cd) for BM25-like ranking
    # ts_rank_cd uses cover density ranking, similar to BM25 in principle
    ts_query = ' & '.join(query_terms.split())

    # Search conversations and messages using full-text search, ranked by relevance.
    # Message matching reads the precomputed `messages.search_vector` column
    # (kept fresh by a trigger) instead of computing to_tsvector() per row at
    # query time — the dynamic computation forced a full-table scan that made
    # short queries take 19-43s ("s"=297 hits on 2026-08-07), letting a slow
    # in-flight request overwrite a later correct result in the spotlight UI.
    # The outer ORDER BY rank_score DESC makes the [:100] cap below
    # relevance-aware: DISTINCT ON (c.id) forces the inner ORDER BY to start
    # with c.id, so without the wrap rows would come back in UUID order and
    # the slice would keep arbitrary conversations (review finding 2026-08-07).
    from sqlalchemy import text, func

    # Find conversations with ranked full-text search on messages
    ranked_result = await db.execute(
        text("""
            SELECT * FROM (
                SELECT DISTINCT ON (c.id)
                    c.id as conv_id,
                    c.title as conv_title,
                    c.updated_at as conv_updated_at,
                    GREATEST(
                        COALESCE(ts_rank_cd(to_tsvector('simple', c.title), plainto_tsquery('simple', :q)), 0),
                        COALESCE(ts_rank_cd(m.search_vector, plainto_tsquery('simple', :q)), 0)
                    ) as rank_score
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user_id = :user_id
                  AND (
                    to_tsvector('simple', c.title) @@ plainto_tsquery('simple', :q)
                    OR m.search_vector @@ plainto_tsquery('simple', :q)
                  )
                ORDER BY c.id, rank_score DESC
            ) ranked
            ORDER BY rank_score DESC
            LIMIT 100
        """),
        {"q": ts_query, "user_id": current_user.id}
    )
    ranked_rows = ranked_result.all()
    
    # Collect ranked conversation IDs
    ts_matched_conv_ids = [row.conv_id for row in ranked_rows]

    # Also keep ILIKE fallback for conversations not caught by tsvector.
    # Single-character queries ("s") match nearly every message via %s% and
    # would scan the whole table; message-level ILIKE is bounded by the
    # tsvector GIN matches instead (a single char is rarely a meaningful
    # search term, and the ranked tsvector path above already covers it).
    ilike_title_result = await db.execute(
        select(Conversation).where(
            Conversation.user_id == current_user.id,
            Conversation.title.ilike(keyword),
            ~Conversation.id.in_(ts_matched_conv_ids) if ts_matched_conv_ids else True
        ).order_by(desc(Conversation.updated_at)).limit(100)
    )
    ilike_title_convs = ilike_title_result.scalars().all()

    ilike_msg_conv_ids: list = []
    if len(query_terms) >= 2:
        # Bounded message-level ILIKE fallback: skip the pathological %s%-style
        # full scans; cap rows so a broad two-char match cannot scan the table.
        ilike_msg_result = await db.execute(
            select(Message.conversation_id).where(
                Message.content.ilike(keyword),
                Message.conversation_id.in_(
                    select(Conversation.id).where(Conversation.user_id == current_user.id)
                ),
                ~Message.conversation_id.in_(ts_matched_conv_ids) if ts_matched_conv_ids else True
            ).distinct().limit(100)
        )
        ilike_msg_conv_ids = [row[0] for row in ilike_msg_result.all()]
    
    # Merge all IDs: tsvector-ranked first, then ILIKE matches. Cap the total
    # so a broad/short query cannot return hundreds of loosely-related
    # conversations (ranking quality collapses; the frontend renders a bounded
    # list anyway).
    all_conv_ids = list(dict.fromkeys(ts_matched_conv_ids + [c.id for c in ilike_title_convs] + ilike_msg_conv_ids))[:100]
    
    if not all_conv_ids:
        return []
    
    # Load all matched conversations
    conv_result = await db.execute(
        select(Conversation).where(Conversation.id.in_(all_conv_ids))
    )
    conv_map = {c.id: c for c in conv_result.scalars().all()}
    
    # Order by the merged priority
    ordered_convs = [conv_map[cid] for cid in all_conv_ids if cid in conv_map]
    
    results = []
    for conv in ordered_convs:
        # Get matched messages
        matched_msgs_result = await db.execute(
            select(Message).where(
                Message.conversation_id == conv.id,
                Message.content.ilike(keyword)
            ).order_by(Message.created_at).limit(3)
        )
        matched_msgs = matched_msgs_result.scalars().all()
        
        # If no ILIKE messages, try tsvector on messages for snippets.
        # Uses the precomputed search_vector column (GIN index) instead of
        # computing to_tsvector() per row (see /search above).
        if not matched_msgs:
            matched_msgs_ts = await db.execute(
                text("""
                    SELECT m.id, m.role, m.content, m.created_at
                    FROM messages m
                    WHERE m.conversation_id = :conv_id
                      AND m.search_vector @@ plainto_tsquery('simple', :q)
                    ORDER BY ts_rank_cd(m.search_vector, plainto_tsquery('simple', :q)) DESC
                    LIMIT 3
                """),
                {"conv_id": conv.id, "q": ts_query}
            )
            matched_msgs = [SimpleMessage(id=row.id, role=row.role, content=row.content, created_at=row.created_at) for row in matched_msgs_ts.all()]
        
        snippets = []
        for m in matched_msgs:
            content = m.content or ""
            lower_content = content.lower()
            lower_q = q.strip().lower()
            idx = lower_content.find(lower_q)
            if idx >= 0:
                start = max(0, idx - 30)
                end = min(len(content), idx + len(q.strip()) + 30)
                snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
            else:
                snippet = content[:80] + ("..." if len(content) > 80 else "")
            snippets.append(MatchedMessage(id=m.id, role=m.role, content_snippet=snippet))
        
        results.append(ConversationSearchResult(
            conversation_id=conv.id,
            title=conv.title,
            updated_at=_utc_iso(conv.updated_at),
            matched_messages=snippets
        ))
    
    return results


class SimpleMessage:
    """Lightweight message class for tsvector results"""
    def __init__(self, id, role, content, created_at=None):
        self.id = id
        self.role = role
        self.content = content
        self.created_at = created_at


# ─── Conversation Groups (MUST be before /{conversation_id} routes) ───

@router.get("/groups", response_model=List[ConversationGroupResponse])
async def list_conversation_groups(
    assistant_id: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = select(ConversationGroup).where(ConversationGroup.user_id == current_user.id)
    if assistant_id:
        query = query.where(ConversationGroup.assistant_id == assistant_id)
    query = query.order_by(ConversationGroup.sort_order.asc(), desc(ConversationGroup.updated_at))
    result = await db.execute(query)
    groups = result.scalars().all()

    # Count conversations per group
    group_counts = {}
    for g in groups:
        count_result = await db.execute(
            select(func.count(Conversation.id)).where(Conversation.group_id == g.id)
        )
        group_counts[g.id] = count_result.scalar() or 0

    return [
        ConversationGroupResponse(
            id=g.id,
            name=g.name,
            color=g.color,
            assistant_id=g.assistant_id,
            sort_order=g.sort_order or 0,
            created_at=_utc_iso(g.created_at),
            updated_at=_utc_iso(g.updated_at),
            conversation_count=group_counts.get(g.id, 0)
        )
        for g in groups
    ]


@router.post("/groups", response_model=ConversationGroupResponse)
async def create_conversation_group(
    group_data: ConversationGroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    group = ConversationGroup(
        user_id=current_user.id,
        assistant_id=group_data.assistant_id,
        name=group_data.name or "新分组",
        color=group_data.color or "#3b82f6"
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return ConversationGroupResponse(
        id=group.id,
        name=group.name,
        color=group.color,
        assistant_id=group.assistant_id,
            sort_order=group.sort_order or 0,
        created_at=_utc_iso(group.created_at),
        updated_at=_utc_iso(group.updated_at),
        conversation_count=0
    )


@router.put("/groups/{group_id}", response_model=ConversationGroupResponse)
async def update_conversation_group(
    group_id: str,
    group_data: ConversationGroupUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(ConversationGroup).where(
            ConversationGroup.id == group_id,
            ConversationGroup.user_id == current_user.id
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    if group_data.name is not None:
        group.name = group_data.name
    if group_data.color is not None:
        group.color = group_data.color

    await db.commit()
    await db.refresh(group)

    count_result = await db.execute(
        select(func.count(Conversation.id)).where(Conversation.group_id == group.id)
    )
    conversation_count = count_result.scalar() or 0

    return ConversationGroupResponse(
        id=group.id,
        name=group.name,
        color=group.color,
        assistant_id=group.assistant_id,
            sort_order=group.sort_order or 0,
        created_at=_utc_iso(group.created_at),
        updated_at=_utc_iso(group.updated_at),
        conversation_count=conversation_count
    )


@router.put("/groups/{group_id}/move", response_model=ConversationGroupResponse)
async def move_conversation_group(
    group_id: str,
    move_data: GroupMoveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(ConversationGroup).where(
            ConversationGroup.id == group_id,
            ConversationGroup.user_id == current_user.id
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    assistant_result = await db.execute(
        select(Assistant).where(
            Assistant.id == move_data.assistant_id,
            Assistant.user_id == current_user.id
        )
    )
    if assistant_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Assistant not found")

    group.assistant_id = move_data.assistant_id
    # Conversations inside the group move with it (groups are assistant-scoped).
    await db.execute(
        update(Conversation)
        .where(Conversation.group_id == group.id)
        .values(assistant_id=move_data.assistant_id)
    )
    await db.commit()
    await db.refresh(group)

    count_result = await db.execute(
        select(func.count(Conversation.id)).where(Conversation.group_id == group.id)
    )
    conversation_count = count_result.scalar() or 0

    return ConversationGroupResponse(
        id=group.id,
        name=group.name,
        color=group.color,
        assistant_id=group.assistant_id,
            sort_order=group.sort_order or 0,
        created_at=_utc_iso(group.created_at),
        updated_at=_utc_iso(group.updated_at),
        conversation_count=conversation_count
    )


@router.delete("/groups/{group_id}")
async def delete_conversation_group(
    group_id: str,
    delete_conversations: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(ConversationGroup).where(
            ConversationGroup.id == group_id,
            ConversationGroup.user_id == current_user.id
        )
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    if delete_conversations:
        # Delete all conversations in the group
        conv_result = await db.execute(
            select(Conversation.id).where(Conversation.group_id == group_id)
        )
        conv_ids = [row[0] for row in conv_result.all()]
        for batch_ids in _chunked_ids(conv_ids):
            await db.execute(
                update(ScheduledTask)
                .where(and_(
                    ScheduledTask.conversation_id.in_(batch_ids),
                    ScheduledTask.status == "active",
                ))
                .values(status="cancelled", next_run_at=None)
            )
            await db.execute(
                delete(Message).where(Message.conversation_id.in_(batch_ids))
            )
            await db.execute(
                delete(ChatSession).where(ChatSession.conversation_id.in_(batch_ids))
            )
            await db.execute(
                delete(Conversation).where(Conversation.id.in_(batch_ids))
            )
    else:
        # Move conversations out of the group
        await db.execute(
            update(Conversation)
            .where(Conversation.group_id == group_id)
            .values(group_id=None)
        )

    await db.delete(group)
    await db.commit()
    return {"status": "ok"}


@router.post("/groups/bulk-delete")
async def bulk_delete_groups(
    delete_data: BulkDeleteGroupsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    group_ids = list(dict.fromkeys(delete_data.group_ids))
    if not group_ids:
        raise HTTPException(status_code=400, detail="No groups selected")

    result = await db.execute(
        select(ConversationGroup).where(
            ConversationGroup.user_id == current_user.id,
            ConversationGroup.id.in_(group_ids),
        )
    )
    valid_groups = result.scalars().all()
    if not valid_groups:
        raise HTTPException(status_code=404, detail="Groups not found")

    valid_group_ids = [g.id for g in valid_groups]

    if delete_data.delete_conversations:
        conv_result = await db.execute(
            select(Conversation.id).where(Conversation.group_id.in_(valid_group_ids))
        )
        conv_ids = [row[0] for row in conv_result.all()]
        for batch_ids in _chunked_ids(conv_ids):
            await db.execute(
                update(ScheduledTask)
                .where(and_(
                    ScheduledTask.conversation_id.in_(batch_ids),
                    ScheduledTask.status == "active",
                ))
                .values(status="cancelled", next_run_at=None)
            )
            await db.execute(
                delete(Message).where(Message.conversation_id.in_(batch_ids))
            )
            await db.execute(
                delete(ChatSession).where(ChatSession.conversation_id.in_(batch_ids))
            )
            await db.execute(
                delete(Conversation).where(Conversation.id.in_(batch_ids))
            )
    else:
        await db.execute(
            update(Conversation)
            .where(Conversation.group_id.in_(valid_group_ids))
            .values(group_id=None)
        )

    for batch_ids in _chunked_ids(valid_group_ids):
        await db.execute(
            delete(ConversationGroup).where(ConversationGroup.id.in_(batch_ids))
        )

    await db.commit()
    return {"status": "ok", "deleted_count": len(valid_group_ids), "deleted_conversations": delete_data.delete_conversations}


@router.put("/reorder")
async def reorder_conversations(
    reorder_data: ConversationReorderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    for item in reorder_data.items:
        await db.execute(
            update(Conversation)
            .where(
                Conversation.id == item.id,
                Conversation.user_id == current_user.id
            )
            .values(sort_order=item.sort_order, group_id=item.group_id)
        )
    await db.commit()
    return {"status": "ok"}


@router.put("/groups/reorder")
async def reorder_groups(
    reorder_data: ConversationGroupReorderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    for item in reorder_data.items:
        await db.execute(
            update(ConversationGroup)
            .where(
                ConversationGroup.id == item.id,
                ConversationGroup.user_id == current_user.id
            )
            .values(sort_order=item.sort_order)
        )
    await db.commit()
    return {"status": "ok"}


# ─── Conversation CRUD (parameterized routes) ──────────────────────────

@router.get("/{conversation_id}", response_model=ConversationWithMessages)
async def get_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await _reconcile_deathmatch_status(db, conversation)

    msg_result = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    )
    messages = msg_result.scalars().all()

    return ConversationWithMessages(
        id=conversation.id,
        title=conversation.title,
        group_id=conversation.group_id,
        assistant_id=conversation.assistant_id,
        sort_order=conversation.sort_order or 0,
        created_at=_utc_iso(conversation.created_at),
        updated_at=_utc_iso(conversation.updated_at),
        deathmatch_mode=conversation.deathmatch_mode or False,
        deathmatch_status=conversation.deathmatch_status or "inactive",
        deathmatch_reason=conversation.deathmatch_reason,
        deathmatch_goal=conversation.deathmatch_goal,
        deathmatch_turns=conversation.deathmatch_turns or 0,
        deathmatch_max_turns=conversation.deathmatch_max_turns or 30,
        deathmatch_grilling_total=conversation.deathmatch_grilling_total or 0,
        deathmatch_grilling_completed=conversation.deathmatch_grilling_completed or 0,
        deathmatch_grilling_round=conversation.deathmatch_grilling_round or 0,
        deathmatch_grilling_round_total=conversation.deathmatch_grilling_round_total or 3,
        deathmatch_context_summary=conversation.deathmatch_context_summary,
        deathmatch_expected_marker=conversation.deathmatch_expected_marker,
        deathmatch_marker_miss_count=conversation.deathmatch_marker_miss_count or 0,
        deathmatch_compressed_context=conversation.deathmatch_compressed_context,
        deathmatch_plan=conversation.deathmatch_plan,
        deathmatch_plan_version=conversation.deathmatch_plan_version or 0,
        messages=[
            MessageResponse(
                id=m.id,
                conversation_id=m.conversation_id,
                role=m.role,
                content=m.content,
                reasoning_content=m.reasoning_content,
                tool_calls=m.tool_calls,
                tool_results=m.tool_results,
                created_at=_utc_iso(m.created_at)
            )
            for m in messages
        ]
    )


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await db.execute(
        update(ScheduledTask)
        .where(and_(
            ScheduledTask.conversation_id == conversation_id,
            ScheduledTask.status == "active",
        ))
        .values(status="cancelled", next_run_at=None)
    )
    await db.delete(conversation)
    await db.commit()
    return {"status": "ok"}


@router.put("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: str,
    conversation_data: ConversationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation_data.title is not None:
        conversation.title = conversation_data.title
    if 'group_id' in conversation_data.model_fields_set:
        conversation.group_id = conversation_data.group_id

    await db.commit()
    await db.refresh(conversation)
    last_user_times = await _get_last_user_message_times(db, [conversation.id])
    return _conversation_response(
        conversation, last_user_times.get(conversation.id)
    )



@router.put("/{conversation_id}/move", response_model=ConversationResponse)
async def move_conversation(
    conversation_id: str,
    move_data: ConversationMoveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    resulting_assistant_id = move_data.assistant_id or conversation.assistant_id
    if move_data.group_id is not None:
        # Verify group exists and belongs to user. Groups are assistant-scoped:
        # the target group must belong to the assistant the conversation ends
        # up in, or the conversation would vanish from both assistants' group
        # listings.
        group_result = await db.execute(
            select(ConversationGroup).where(
                ConversationGroup.id == move_data.group_id,
                ConversationGroup.user_id == current_user.id,
            )
        )
        group = group_result.scalar_one_or_none()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        if group.assistant_id != resulting_assistant_id:
            raise HTTPException(
                status_code=400,
                detail="Group does not belong to the target assistant",
            )

    if move_data.assistant_id is not None and move_data.assistant_id != conversation.assistant_id:
        # Cross-assistant move: verify the target assistant exists and belongs
        # to the user. Without an explicit target group, the conversation
        # leaves its group (groups are assistant-scoped).
        assistant_result = await db.execute(
            select(Assistant).where(
                Assistant.id == move_data.assistant_id,
                Assistant.user_id == current_user.id
            )
        )
        target = assistant_result.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Assistant not found")
        if move_data.group_id is None:
            conversation.group_id = None
        conversation.assistant_id = move_data.assistant_id

    conversation.group_id = move_data.group_id
    await db.commit()
    await db.refresh(conversation)
    last_user_times = await _get_last_user_message_times(db, [conversation.id])
    return _conversation_response(
        conversation, last_user_times.get(conversation.id)
    )


class ExportRequest(BaseModel):
    assistant_id: str
    conversation_ids: List[str]


class ExportResponse(BaseModel):
    status: str
    exported_count: int
    files: List[str]


@router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
async def get_messages(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msg_result = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    )
    messages = msg_result.scalars().all()

    return [
        MessageResponse(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            reasoning_content=m.reasoning_content,
            tool_calls=m.tool_calls,
            tool_results=m.tool_results,
            created_at=_utc_iso(m.created_at)
        )
        for m in messages
    ]


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip()
    if not name:
        name = 'untitled'
    return name[:100]


@router.post("/export")
async def export_conversations(
    export_data: ExportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Assistant).where(
            Assistant.id == export_data.assistant_id,
            Assistant.user_id == current_user.id
        )
    )
    assistant = result.scalar_one_or_none()
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "output_files", sanitize_filename(assistant.name))
    os.makedirs(output_dir, exist_ok=True)

    conversations_result = await db.execute(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(
            Conversation.id.in_(export_data.conversation_ids),
            Conversation.user_id == current_user.id,
            Conversation.assistant_id == export_data.assistant_id
        )
        .order_by(Conversation.created_at)
    )
    conversations = conversations_result.unique().scalars().all()

    conv_data = []
    for conv in conversations:
        messages = sorted(conv.messages, key=lambda m: m.created_at)
        rows = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.role == 'user':
                query = _strip_media_tags(msg.content or "")
                answer = ''
                if i + 1 < len(messages) and messages[i + 1].role == 'assistant':
                    answer = _strip_media_tags(messages[i + 1].content or "")
                    i += 1
                rows.append({'query': query, 'answer': answer})
            i += 1
        conv_data.append({
            'title': conv.title,
            'id': conv.id,
            'rows': rows,
        })

    def _build_zip_and_files() -> tuple[bytes, list[str]]:
        exported_files = []
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for cd in conv_data:
                filename = f"{sanitize_filename(cd['title'])}_{cd['id']}.csv"
                filepath = os.path.join(output_dir, filename)
                with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.DictWriter(f, fieldnames=['query', 'answer'])
                    writer.writeheader()
                    writer.writerows(cd['rows'])
                csv_buffer = io.StringIO()
                writer = csv.DictWriter(csv_buffer, fieldnames=['query', 'answer'])
                writer.writeheader()
                writer.writerows(cd['rows'])
                zf.writestr(filename, csv_buffer.getvalue().encode('utf-8-sig'))
                exported_files.append(filename)
        return zip_buffer.getvalue(), exported_files

    zip_data, _ = await asyncio.to_thread(_build_zip_and_files)

    zip_filename = f"{sanitize_filename(assistant.name)}_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    encoded_filename = quote(zip_filename, safe='')

    return StreamingResponse(
        io.BytesIO(zip_data),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"export.zip\"; filename*=UTF-8''{encoded_filename}"
        }
    )


def _render_messages_pdf(title: str, content: str, created_at: str = "", workspace_root: str | None = None) -> bytes:
    """Render messages as PDF using the same pipeline as note PDF export.

    When *workspace_root* is provided, local image references (relative
    workspace paths such as ``charts/chart1.png``) are inlined as base64
    data URIs before rendering — WeasyPrint cannot resolve relative paths
    (it has no base URL for ``HTML(string=...)``) and would silently drop
    every image. Mirrors ``_render_note_pdf``.
    """
    from weasyprint import HTML, CSS
    from app.services.pdf_fonts import get_font_config_and_css

    content = content or ""
    # Exports never embed media resources (product decision): strip them.
    content = _strip_media_tags(content)
    if workspace_root:
        content = _materialize_note_images(content, workspace_root)
    html_body = _markdown_to_html_with_mermaid(content)
    html_body = _ensure_heading_ids(html_body)
    title_html = f'<h1 id="_note_title">{title}</h1>' if title else ""
    meta = f'<div class="meta">{created_at}</div>' if created_at else ""

    full_html = (
        f"<!DOCTYPE html><html><head>"
        f"<meta charset='utf-8'>"
        f"</head><body>{title_html}{meta}{html_body}</body></html>"
    )
    font_config, font_css = get_font_config_and_css()
    return HTML(string=full_html).write_pdf(
        stylesheets=[font_css, CSS(string=_PDF_CSS, font_config=font_config)],
        font_config=font_config,
    )


@router.post("/export-pdf")
async def export_messages_pdf(
    export_data: ExportMessagesPDFBulkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export selected messages as PDF (single combined or per-message zip)."""
    import asyncio as _asyncio

    workspace_root = await _get_note_workspace_root(db, current_user.id)

    if export_data.action == "single":
        combined_content = ""
        combined_title = ""
        combined_created = ""
        for idx, item in enumerate(export_data.items):
            if idx > 0:
                combined_content += "\n\n---\n\n"
            role = item.role if item.role in ("user", "assistant") else ("user" if "user" in item.title.lower() or "用户" in item.title else "assistant")
            role_label = "**用户**" if role == "user" else "**助手**"
            combined_content += f"{role_label}\n\n{item.content}"
            if not combined_title:
                combined_title = item.title
            if not combined_created:
                combined_created = item.title[:50]
        safe_name = sanitize_filename(combined_title or "对话记录")
        try:
            pdf_bytes = await _asyncio.to_thread(_render_messages_pdf, combined_title, combined_content, "", workspace_root)
        except Exception:
            logger.exception("PDF rendering failed for messages export")
            raise HTTPException(status_code=500, detail="PDF rendering failed")
        encoded = quote(f"{safe_name}.pdf", safe='')
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=\"messages.pdf\"; filename*=UTF-8''{encoded}"
            },
        )
    else:
        def _build_zip() -> tuple[bytes, list[str]]:
            exported_files: list[str] = []
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for idx, item in enumerate(export_data.items):
                    role = item.role if item.role in ("user", "assistant") else ("user" if "user" in item.title.lower() or "用户" in item.title else "assistant")
                    safe_name = sanitize_filename(f"{role}_{idx+1}")
                    try:
                        pdf_bytes = _render_messages_pdf(item.title, item.content, "", workspace_root)
                    except Exception:
                        logger.exception("PDF rendering failed for message %d", idx)
                        pdf_bytes = b""
                    zf.writestr(f"{safe_name}.pdf", pdf_bytes)
                    exported_files.append(f"{safe_name}.pdf")
            return zip_buffer.getvalue(), exported_files

        zip_data, _ = await _asyncio.to_thread(_build_zip)
        zip_buffer = io.BytesIO(zip_data)
        safe_name = sanitize_filename("对话记录")
        encoded = quote(f"{safe_name}.zip", safe='')
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=\"messages.zip\"; filename*=UTF-8''{encoded}"
            }
        )