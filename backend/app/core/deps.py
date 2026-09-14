# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db, User
from app.services.auth_service import decode_access_token

logger = logging.getLogger(__name__)

security = HTTPBearer()


async def get_user_from_token(token: str, db: AsyncSession) -> User:
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is inactive",
        )

    # 用户级模型供应商覆盖（2026-09-13）：鉴权即激活任务上下文覆盖。
    # 覆盖全部走 get_current_user / get_user_from_token 的入口（HTTP + WS），
    # 后台任务入口另行显式激活。load 侧有 30s TTL 缓存，无覆盖用户零额外查询。
    # R4（2026-09-13 审计 F-G）：此处**刻意不做 finally reset**——本函数在
    # endpoint 之前返回，在此 reset 会先于业务执行清空覆盖。隔离不变量由
    # asyncio 任务上下文副本保证：每个请求/WS 连接运行在独立 task，set 只写
    # 该 task 的 ContextVar 副本，task 结束即回收。后台长驻循环
    # （agent_scheduler / memory_scheduler / agent_worker）复用 task，因此
    # 它们必须显式 try/finally reset（见各调度器实现）。该不变量由
    # tests/test_user_model_overrides.py::TestContextIsolation 锁死。
    from app.services.user_model_provider_service import activate_user_overrides
    try:
        await activate_user_overrides(db, user.id)
    except Exception:
        logger.exception("activate_user_overrides failed for user %s — using system endpoints", user.id)

    return user


def extract_websocket_token(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("token")
    if token:
        return token

    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:]

    return None


async def get_current_user_from_websocket(websocket: WebSocket, db: AsyncSession) -> User:
    token = extract_websocket_token(websocket)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await get_user_from_token(token, db)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    return await get_user_from_token(credentials.credentials, db)