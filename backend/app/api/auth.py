# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db, User, UserSession
from app.schemas.chat import UserCreate, UserResponse, LoginRequest, TokenResponse
from app.services.auth_service import hash_password, verify_password, create_access_token
from app.services.assistant_service import create_default_assistant_if_needed
from app.services.memory_service import ensure_user_agent_state
from app.services.workspace_service import ensure_user_workspace
from app.services.agent_permissions import (
    get_default_permissions,
    parse_permissions,
    PERMISSION_KEYS,
)
from datetime import datetime
import json
from typing import Dict, Any

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_response(user) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        created_at=user.created_at.isoformat(),
        agent_permissions=parse_permissions(user),
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    if not request.username or not request.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名和密码不能为空"
        )
    if len(request.username) < 2 or len(request.username) > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名长度必须在2-50个字符之间"
        )
    if len(request.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="密码长度不能少于6个字符"
        )

    result = await db.execute(select(User).where(User.username == request.username))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在"
        )

    user = User(
        username=request.username,
        password_hash=await hash_password(request.password),
        agent_permissions=json.dumps(get_default_permissions()),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await ensure_user_agent_state(db, user.id)
    await ensure_user_workspace(db, user.id, user.username)

    return _user_response(user)


@router.post("/login", response_model=TokenResponse)
async def login(request: Request, login_req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == login_req.username))
    user = result.scalar_one_or_none()

    if not user or not await verify_password(login_req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled"
        )

    user.last_login_at = datetime.utcnow()
    user.last_login_ip = request.client.host if request.client else None
    await db.commit()

    await create_default_assistant_if_needed(db, user.id)
    await ensure_user_agent_state(db, user.id)
    await ensure_user_workspace(db, user.id, user.username)

    access_token = create_access_token(user.id, user.username)

    user_agent = request.headers.get("user-agent", "")

    user_session = UserSession(
        user_id=user.id,
        session_token=access_token,
        ip_address=request.client.host if request.client else None,
        user_agent=user_agent,
        last_active_at=datetime.utcnow()
    )
    db.add(user_session)
    await db.commit()

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user=_user_response(user),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(__import__("app.core.deps", fromlist=["get_current_user"]).get_current_user)
):
    """Sliding-session refresh (2026-08-25 wave-3): a still-valid token mints
    a fresh token whose window starts now — users who open the app within
    ``[security] token_expire_days`` are never re-asked for a password.
    ``get_current_user`` already rejects expired/invalid tokens (401/403), so
    nothing stale can roll forward.

    Security (A4.9 I1): the rotation is a single atomic UPDATE ... WHERE
    session_token = <old> (CAS). A missing row — user already logged out, or
    a concurrent tab already rotated this token — yields 401 WITHOUT issuing
    a token: a logout stays terminal for that login and never resurrects,
    and concurrent app-start refreshes can mint at most one live token. The
    loser tab is harmless: '/auth/refresh' is on the frontend 401-skip list
    and storage-sync already gave it the rotated token."""
    from sqlalchemy import update as sa_update

    old_header = request.headers.get("authorization", "")
    old_token = old_header[7:] if old_header.startswith("Bearer ") else ""
    if not old_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(current_user.id, current_user.username)
    result = await db.execute(
        sa_update(UserSession)
        .where(
            UserSession.user_id == current_user.id,
            UserSession.session_token == old_token,
        )
        .values(session_token=access_token, last_active_at=datetime.utcnow())
    )
    if result.rowcount != 1:
        # No live session row for this token (logged out / already rotated).
        # The freshly minted token above is simply never returned.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session not found (logged out or already rotated)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await db.commit()
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user=_user_response(current_user),
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(__import__("app.core.deps", fromlist=["get_current_user"]).get_current_user)
):
    return _user_response(current_user)


@router.put("/me/permissions", response_model=UserResponse)
async def update_agent_permissions(
    request_data: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(__import__("app.core.deps", fromlist=["get_current_user"]).get_current_user)
):
    current = parse_permissions(current_user)
    for key in PERMISSION_KEYS:
        if key in request_data and isinstance(request_data[key], bool):
            current[key] = request_data[key]
    current_user.agent_permissions = json.dumps(current)
    await db.commit()
    await db.refresh(current_user)
    return _user_response(current_user)


@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(__import__("app.core.deps", fromlist=["get_current_user"]).get_current_user)
):
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == current_user.id,
                UserSession.session_token == token
            )
        )
        session = result.scalar_one_or_none()
        if session:
            await db.delete(session)
            await db.commit()

    return {"message": "Logged out successfully"}