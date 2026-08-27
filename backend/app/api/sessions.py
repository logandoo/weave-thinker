# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List
from datetime import datetime

from app.db.database import get_db, User, UserSession, ChatSession
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class UserSessionResponse:
    def __init__(self, session):
        self.id = session.id
        self.user_id = session.user_id
        self.session_token = session.session_token[:20] + "..." if len(session.session_token) > 20 else session.session_token
        self.ip_address = session.ip_address
        self.user_agent = session.user_agent
        self.last_active_at = session.last_active_at.isoformat() if session.last_active_at else None
        self.expires_at = session.expires_at.isoformat() if session.expires_at else None
        self.created_at = session.created_at.isoformat()


class ChatSessionResponse:
    def __init__(self, session):
        self.id = session.id
        self.user_id = session.user_id
        self.conversation_id = session.conversation_id
        self.assistant_id = session.assistant_id
        self.started_at = session.started_at.isoformat() if session.started_at else None
        self.ended_at = session.ended_at.isoformat() if session.ended_at else None
        self.message_count = session.message_count
        self.total_tokens = session.total_tokens
        self.created_at = session.created_at.isoformat()


@router.get("/user-sessions", response_model=List[dict])
async def list_user_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(UserSession).where(UserSession.user_id == current_user.id).order_by(desc(UserSession.created_at))
    )
    sessions = result.scalars().all()
    return [
        {
            "id": s.id,
            "session_token": s.session_token[:20] + "..." if len(s.session_token) > 20 else s.session_token,
            "ip_address": s.ip_address,
            "user_agent": s.user_agent,
            "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "created_at": s.created_at.isoformat()
        }
        for s in sessions
    ]


@router.delete("/user-sessions/{session_id}")
async def delete_user_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.user_id == current_user.id
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await db.delete(session)
    await db.commit()
    return {"status": "ok"}


@router.get("/chat-sessions", response_model=List[dict])
async def list_chat_sessions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(ChatSession).where(ChatSession.user_id == current_user.id).order_by(desc(ChatSession.started_at))
    )
    sessions = result.scalars().all()
    return [
        {
            "id": s.id,
            "conversation_id": s.conversation_id,
            "assistant_id": s.assistant_id,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "message_count": s.message_count,
            "total_tokens": s.total_tokens,
            "created_at": s.created_at.isoformat()
        }
        for s in sessions
    ]


@router.get("/chat-sessions/{session_id}", response_model=dict)
async def get_chat_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == current_user.id
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "id": session.id,
        "conversation_id": session.conversation_id,
        "assistant_id": session.assistant_id,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "message_count": session.message_count,
        "total_tokens": session.total_tokens,
        "created_at": session.created_at.isoformat()
    }


@router.delete("/chat-sessions/{session_id}")
async def delete_chat_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == current_user.id
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    await db.delete(session)
    await db.commit()
    return {"status": "ok"}