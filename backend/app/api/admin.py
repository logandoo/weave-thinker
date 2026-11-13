# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List, Optional

from app.db.database import get_db, User
from app.schemas.chat import UserResponse
from app.services.auth_service import hash_password
from app.core.deps import require_admin
from datetime import datetime

router = APIRouter(prefix="/api/admin/users", tags=["admin"])


class AdminUserCreate(BaseModel):
    """建号请求体（2026-11-11 补救 #5b）。

    口令此前是 **query 参数**——会明文进 access log / 反代日志 / 浏览器历史。
    移入 body 后不再出现在 URL 里；旧 query 形式会得到 422（body 缺失），
    不会被静默接受。"""

    username: str
    password: str


class AdminUserUpdate(BaseModel):
    """改号请求体（同上）：字段全部可选，只改传入项。"""

    username: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("", response_model=List[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    result = await db.execute(select(User).order_by(desc(User.created_at)))
    users = result.scalars().all()
    return [
        UserResponse(
            id=u.id,
            username=u.username,
            created_at=u.created_at.isoformat()
        )
        for u in users
    ]


@router.post("")
async def create_user(
    payload: AdminUserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    # 遗留清偿 #4：对齐注册路径 2–50（空/超长曾直进 DB——空串更会污染配对身份链）
    if len(payload.username) < 2 or len(payload.username) > 50:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名长度必须在2-50个字符之间"
        )
    result = await db.execute(select(User).where(User.username == payload.username))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists"
        )

    user = User(
        username=payload.username,
        password_hash=await hash_password(payload.password)
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"id": user.id, "username": user.username, "created_at": user.created_at.isoformat()}


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(
        id=user.id,
        username=user.username,
        created_at=user.created_at.isoformat()
    )


@router.put("/{user_id}")
async def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.username:
        # 遗留清偿 #4（r4 M3）：update 同样守 2–50（身份毒值不止 create 一条路）
        if len(payload.username) < 2 or len(payload.username) > 50:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名长度必须在2-50个字符之间"
            )
        check_result = await db.execute(
            select(User).where(User.username == payload.username, User.id != user_id)
        )
        if check_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already exists"
            )
        user.username = payload.username

    if payload.password:
        user.password_hash = await hash_password(payload.password)

    if payload.is_active is not None:
        user.is_active = payload.is_active

    user.updated_at = datetime.utcnow()
    await db.commit()
    return {"status": "ok"}


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete yourself"
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from sqlalchemy import text as _text
    for _table, _col in (
        ("artifact_taint", "user_id"),
        ("memory_action_tokens", "user_id"),
        ("taint_states", "user_id"),
    ):
        await db.execute(_text(
            f"DELETE FROM {_table} WHERE {_col} = :uid"
        ), {"uid": user_id})
    await db.delete(user)
    await db.commit()
    return {"status": "ok"}