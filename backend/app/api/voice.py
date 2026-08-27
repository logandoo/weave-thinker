# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging

from fastapi import APIRouter, WebSocket, Depends, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.deps import get_current_user, get_current_user_from_websocket
from app.db.database import Conversation, Message, get_db
from app.db.database import User
from app.services.assistant_service import ensure_voice_assistant
from app.services.voice_service import VoiceDuplexSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voice", tags=["voice"])


@router.get("/sessions")
async def list_voice_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List voice sessions (conversations belonging to the \u916c assistant)."""
    assistant = await ensure_voice_assistant(db, current_user.id)
    result = await db.execute(
        select(Conversation).where(
            Conversation.assistant_id == assistant.id,
            Conversation.user_id == current_user.id,
        ).order_by(desc(Conversation.updated_at))
    )
    sessions = result.scalars().all()
    return [
        {
            "id": c.id,
            "title": c.title or "\u65b0\u8bed\u97f3\u5bf9\u8bdd",
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in sessions
    ]


@router.post("/sessions")
async def create_voice_session(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new voice session under the \u916c assistant."""
    assistant = await ensure_voice_assistant(db, current_user.id)
    conv = Conversation(
        user_id=current_user.id,
        title="\u65b0\u8bed\u97f3\u5bf9\u8bdd",
        assistant_id=assistant.id,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return {"id": conv.id, "title": conv.title}


@router.get("/sessions/{session_id}/messages")
async def get_voice_session_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get messages for a voice session (for loading history)."""
    assistant = await ensure_voice_assistant(db, current_user.id)
    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == session_id,
            Conversation.user_id == current_user.id,
            Conversation.assistant_id == assistant.id,
        )
    )
    conv = conv_result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="\u4f1a\u8bdd\u4e0d\u5b58\u5728")
    msg_result = await db.execute(
        select(Message).where(Message.conversation_id == session_id)
        .order_by(Message.created_at)
    )
    messages = msg_result.scalars().all()
    return [
        {
            "role": m.role,
            "content": m.content or "",
            "tool_calls": m.tool_calls,
            "tool_results": m.tool_results,
        }
        for m in messages
        if m.role in ("user", "assistant") and m.content
    ]


@router.websocket("/ws")
async def voice_duplex_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    config = get_config()
    if not config.voice_enabled:
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="Voice mode disabled")
        return

    try:
        current_user = await get_current_user_from_websocket(websocket, db)
    except HTTPException as exc:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(exc.detail))
        return

    await websocket.accept()
    conversation_id = websocket.query_params.get("conversation_id") or None
    session = VoiceDuplexSession(websocket, current_user, db, conversation_id=conversation_id)
    try:
        await session.run()
    except Exception as exc:
        logger.error("voice session error: %s", exc, exc_info=True)
        try:
            await websocket.send_text(
                '{"event": "error", "error": "\u8bed\u97f3\u4f1a\u8bdd\u5f02\u5e38\u7ed3\u675f"}'
            )
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
