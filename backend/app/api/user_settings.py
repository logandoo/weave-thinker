# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""用户设置 API — 用户资料（昵称/头像）+ 用户级模型供应商覆盖（2026-09-13）。

设计：design/BACKEND_DESIGN_user_settings.html；计划 docs/PLAN.md Task 5。
- GET/PUT /api/users/me/profile · POST/DELETE /api/users/me/avatar
- GET/PUT/DELETE /api/users/me/model-provider
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.database import User, UserModelProvider, get_db
from app.schemas.chat import UserResponse
from app.services import user_model_provider_service as provider_svc
from app.services import user_profile_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["user-settings"])


def _profile_response(user: User) -> UserResponse:
    from app.services.agent_permissions import parse_permissions
    return UserResponse(
        id=user.id,
        username=user.username,
        created_at=user.created_at.isoformat(),
        agent_permissions=parse_permissions(user),
        nickname=getattr(user, "nickname", None) or None,
        avatar_data=getattr(user, "avatar_data", None) or None,
    )


class ProfileUpdate(BaseModel):
    nickname: Optional[str] = Field(default=None, max_length=50)


# ---------------- 用户资料 ----------------

@router.get("/api/users/me/profile", response_model=UserResponse)
async def get_profile(user: User = Depends(get_current_user)):
    return _profile_response(user)


@router.put("/api/users/me/profile", response_model=UserResponse)
async def update_profile(
    payload: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    nickname = (payload.nickname or "").strip()
    user.nickname = nickname or None
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _profile_response(user)


@router.post("/api/users/me/avatar", response_model=UserResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > user_profile_service.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"avatar too large (max {user_profile_service.MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
            )
        chunks.append(chunk)
    try:
        data_url = user_profile_service.process_avatar(b"".join(chunks))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    user.avatar_data = data_url
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _profile_response(user)


@router.delete("/api/users/me/avatar", response_model=UserResponse)
async def delete_avatar(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user.avatar_data = None
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _profile_response(user)


# ---------------- 模型供应商覆盖 ----------------

@router.get("/api/users/me/model-provider")
async def get_model_provider(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await provider_svc.list_user_provider_rows(db, user.id)
    return provider_svc.serialize_status(rows)


@router.put("/api/users/me/model-provider")
async def update_model_provider(
    payload: dict = Body(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        parsed = provider_svc.parse_overrides_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    result = await db.execute(
        select(UserModelProvider).where(UserModelProvider.user_id == user.id)
    )
    existing = {row.kind: row for row in result.scalars().all()}
    # A4.9 Important 修复：改 pg upsert（ON CONFLICT (user_id, kind)）——
    # 唯一索引下并发双 PUT 不再产生重复行/IntegrityError。
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    for kind, cfg in parsed.items():
        row = existing.get(kind)
        if cfg is None:
            if row is not None:
                await db.delete(row)
            continue
        # A4.9 Minor 修复（2026-09-13）：全空覆盖无意义（无 URL/模型/参数/Key）
        # → 视同删除，避免留下 GET 可见但无 "已覆盖" 标记的空行。例外：既有行
        # 且 api_key 保持原值（None）时保留——UI 承诺"留空保持不变"。
        _has_effective = bool(
            cfg["base_url"] or cfg["model_name"]
            or any(v is not None for v in cfg["params"].values())
            or (cfg["api_key"] not in (None, ""))
            or (row is not None and cfg["api_key"] is None)
        )
        if not _has_effective:
            if row is not None:
                await db.delete(row)
            continue
        values = {
            "id": row.id if row is not None else str(uuid.uuid4()),
            "user_id": user.id,
            "kind": kind,
            "enabled": cfg["enabled"],
            "base_url": cfg["base_url"],
            "model_name": cfg["model_name"],
            "params_json": json.dumps(cfg["params"], ensure_ascii=False),
            "created_at": row.created_at if row is not None else datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if cfg["api_key"] is not None:
            values["api_key"] = cfg["api_key"]
        stmt = pg_insert(UserModelProvider).values(**values)
        update_cols = {
            "enabled": stmt.excluded["enabled"],
            "base_url": stmt.excluded["base_url"],
            "model_name": stmt.excluded["model_name"],
            "params_json": stmt.excluded["params_json"],
            "updated_at": stmt.excluded["updated_at"],
        }
        if cfg["api_key"] is not None:
            update_cols["api_key"] = stmt.excluded["api_key"]
        await db.execute(
            stmt.on_conflict_do_update(index_elements=["user_id", "kind"], set_=update_cols)
        )
    await db.commit()
    # Core upsert 不经 ORM identity map；session expire_on_commit=False 时
    # 再查询会命中旧实例（A4.9 修复轮实测：DB 已更新而响应回旧值）→ 显式过期。
    # 注意：expire_all 会连同 user 一起过期，uid 必须提前捕获（否则后续
    # user.id 触发 async 上下文中的同步懒加载 → MissingGreenlet 500）。
    uid = user.id
    provider_svc.invalidate_user_overrides(uid)
    db.expire_all()
    rows = await provider_svc.list_user_provider_rows(db, uid)
    return provider_svc.serialize_status(rows)


@router.delete("/api/users/me/model-provider")
async def delete_model_provider(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserModelProvider).where(UserModelProvider.user_id == user.id)
    )
    rows = result.scalars().all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    provider_svc.invalidate_user_overrides(user.id)
    return {"deleted": len(rows)}
