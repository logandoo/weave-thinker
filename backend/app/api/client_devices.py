# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W16 设备执行模式 — 设备配对/管理/文件代理 HTTP 面（PLAN §6.2）。

安全（PLAN §8 + A4.9 修订）：配对码一次性短 TTL（用户 JWT 签发、失败限次/在途限数）；
设备 token 仅存 hash；全部端点逐条属主校验（IDOR fail-closed）；revoked → 403/404；
fs 路径 Windows/POSIX 双分隔符硬化 + 头部净化 + RFC 5987 文件名；
fs 鉴权对齐 files.py `_authenticate`（Bearer 或 ?token=，供 <img>/fetch 预览用——
注意这是**用户 JWT** 的既有仓库惯例；设备 token 只走 WS header，见 device_ws.py）。
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.deps import get_current_user, get_db, get_user_from_token
from app.db.database import User
from app.services import client_device_service as cds
from app.services.device_session_service import (
    DeviceCallError,
    DeviceCallTimeoutError,
    DeviceOfflineError,
    device_session_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["client-devices"])

_bearer = HTTPBearer(auto_error=False)


class PairingCodeResponse(BaseModel):
    code: str
    expires_in_seconds: int


class DevicePairRequest(BaseModel):
    code: str
    name: str | None = None
    platform: str | None = None
    device_id: str | None = None


class DevicePairResponse(BaseModel):
    device_id: str
    token: str
    name: str | None = None


class DeviceInfo(BaseModel):
    id: str
    device_id: str
    name: str | None = None
    platform: str | None = None
    online: bool = False
    revoked: bool = False
    last_seen_at: str | None = None
    created_at: str | None = None


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


async def _authenticate(
    credentials: HTTPAuthorizationCredentials | None,
    token: str | None,
    db: AsyncSession,
) -> User:
    """Bearer 头（标准）或 ?token=（<img>/fetch 预览用）——与 files.py 同款。"""
    user: User | None = None
    if credentials:
        try:
            user = await get_user_from_token(credentials.credentials, db)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            user = None
    if user is None and token:
        try:
            user = await get_user_from_token(token, db)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            user = None
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@router.post("/pairing-code", response_model=PairingCodeResponse)
async def create_pairing_code(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用户 JWT 签发一次性短 TTL 配对码（PLAN §8.1 ①）。"""
    if not get_config().client_exec_enabled:
        raise HTTPException(status_code=404, detail="device execution disabled")
    rec = cds.issue_pairing_code(current_user.id)
    return PairingCodeResponse(code=rec["code"], expires_in_seconds=rec["expires_in_seconds"])


@router.post("/pair", response_model=DevicePairResponse)
async def pair_device(
    payload: DevicePairRequest,
    db: AsyncSession = Depends(get_db),
):
    """设备凭配对码注册（PLAN §8.1 ②③）：token 仅此一次明文回传。"""
    if not get_config().client_exec_enabled:
        raise HTTPException(status_code=404, detail="device execution disabled")
    try:
        row, token = await cds.register_device(
            db,
            code=payload.code,
            name=payload.name,
            platform=payload.platform,
            device_id=payload.device_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return DevicePairResponse(device_id=row.device_id, token=token, name=row.name)


@router.get("", response_model=list[DeviceInfo])
async def list_devices(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = await cds.list_devices(db, current_user.id)
    online = device_session_manager.online_device_ids()
    return [
        DeviceInfo(
            id=r.id,
            device_id=r.device_id,
            name=r.name,
            platform=r.platform,
            online=(r.device_id in online) and not r.revoked,
            revoked=bool(r.revoked),
            last_seen_at=_iso(r.last_seen_at),
            created_at=_iso(r.created_at),
        )
        for r in rows
    ]


@router.post("/{device_id}/revoke")
async def revoke_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import select

    from app.db.database import ClientExecutorDevice

    result = await db.execute(
        select(ClientExecutorDevice).where(
            ClientExecutorDevice.device_id == device_id,
            ClientExecutorDevice.user_id == current_user.id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    row.revoked = True
    await db.commit()
    # revoke 即踢线（PLAN §8.3）：unregister 已按会话身份 fail pending
    device_session_manager.unregister(device_id)
    return {"ok": True, "revoked": True}


async def _require_owned_device(db: AsyncSession, user_id: str, device_id: str):
    from sqlalchemy import select

    from app.db.database import ClientExecutorDevice

    result = await db.execute(
        select(ClientExecutorDevice).where(
            ClientExecutorDevice.device_id == device_id,
            ClientExecutorDevice.user_id == user_id,
            ClientExecutorDevice.revoked.is_(False),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="设备不存在或已解绑")
    return row


async def _device_rpc(device_id: str, method: str, params: dict) -> dict:
    timeout = float(get_config().client_exec_call_timeout_seconds)
    try:
        return await device_session_manager.rpc_call(device_id, method, params, timeout=timeout)
    except DeviceOfflineError:
        raise HTTPException(status_code=502, detail="设备已离线")
    except DeviceCallTimeoutError:
        raise HTTPException(status_code=504, detail="设备执行超时")
    except DeviceCallError:
        raise HTTPException(status_code=502, detail="设备执行失败")


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _media_type_for(rel: str) -> str:
    """W18 G6：按扩展名 typing（对齐 files.py 语义）——video/audio 预览依赖
    Content-Type 做资源选择（Safari 拒收 octet-stream 媒体）。"""
    import mimetypes

    mt, _ = mimetypes.guess_type(rel)
    return mt or "application/octet-stream"


def _check_rel_path(path: str) -> str:
    """workspace 相对路径硬化（A4.9 I-3）——校验本体在 client_device_service.validate_rel_path。"""
    from app.services.client_device_service import validate_rel_path

    try:
        return validate_rel_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _safe_filename(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return _CTRL_RE.sub("_", name).replace('"', "_") or "download"


def _content_disposition(rel: str) -> str:
    name = _safe_filename(rel)
    try:
        name.encode("ascii")
        return f"attachment; filename=\"{name}\""
    except UnicodeEncodeError:
        from urllib.parse import quote
        return f"attachment; filename=\"download\"; filename*=UTF-8''{quote(name)}"


@router.get("/{device_id}/fs/list")
async def fs_list(
    device_id: str,
    path: str = Query(""),
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None),
):
    current_user = await _authenticate(credentials, token, db)
    await _require_owned_device(db, current_user.id, device_id)
    rel = _check_rel_path(path) if path else ""
    result = await _device_rpc(device_id, "fs/list", {"path": rel})
    return result


@router.get("/{device_id}/fs/read")
async def fs_read(
    device_id: str,
    path: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(20000, ge=1, le=100000),
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None),
):
    current_user = await _authenticate(credentials, token, db)
    await _require_owned_device(db, current_user.id, device_id)
    rel = _check_rel_path(path)
    return await _device_rpc(
        device_id, "fs/read", {"path": rel, "offset": offset, "limit": limit}
    )


async def _device_fetch_bytes(device_id: str, method: str, params: dict) -> bytes:
    """W19：经通用字节通道取字节（fs/download|fs/zip|fs/office-* 共用）。"""
    from app.services.device_session_service import DeviceOversizeError

    max_bytes = get_config().client_exec_max_payload_mb * 1024 * 1024
    try:
        return await device_session_manager.fetch_file_bytes(
            device_id, method, params, max_bytes=max_bytes
        )
    except DeviceOfflineError:
        raise HTTPException(status_code=502, detail="设备已离线")
    except DeviceCallTimeoutError:
        raise HTTPException(status_code=504, detail="设备执行超时")
    except DeviceOversizeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except DeviceCallError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/{device_id}/fs/download")
async def fs_download(
    device_id: str,
    path: str = Query(...),
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None),
):
    """设备文件字节代理（下载卡/预览用）。data_b64 单包或 transfer_id+chunk 分块。"""
    current_user = await _authenticate(credentials, token, db)
    await _require_owned_device(db, current_user.id, device_id)
    rel = _check_rel_path(path)
    data = await _device_fetch_bytes(device_id, "fs/download", {"path": rel})
    return Response(
        content=data,
        media_type=_media_type_for(rel),
        headers={"Content-Disposition": _content_disposition(rel)},
    )


@router.get("/{device_id}/fs/zip")
async def fs_zip(
    device_id: str,
    path: str = Query(...),
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None),
):
    """W19 P1：设备端目录打包（fs/zip RPC 在设备生成 zip 后回传字节）。"""
    current_user = await _authenticate(credentials, token, db)
    await _require_owned_device(db, current_user.id, device_id)
    rel = _check_rel_path(path)
    data = await _device_fetch_bytes(device_id, "fs/zip", {"path": rel})
    name = rel.rsplit("/", 1)[-1] or "folder"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{name}.zip")},
    )


@router.get("/{device_id}/fs/office-html")
async def fs_office_html(
    device_id: str,
    path: str = Query(...),
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None),
):
    """W19 P1：设备端 Office→HTML 转换（设备侧 LibreOffice，字节回传）。"""
    current_user = await _authenticate(credentials, token, db)
    await _require_owned_device(db, current_user.id, device_id)
    rel = _check_rel_path(path)
    data = await _device_fetch_bytes(device_id, "fs/office-html", {"path": rel})
    return Response(content=data, media_type="text/html; charset=utf-8")


@router.get("/{device_id}/fs/office-pdf")
async def fs_office_pdf(
    device_id: str,
    path: str = Query(...),
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None),
):
    """W19 P1：设备端 Office→PDF 转换（设备侧 LibreOffice，字节回传）。"""
    current_user = await _authenticate(credentials, token, db)
    await _require_owned_device(db, current_user.id, device_id)
    rel = _check_rel_path(path)
    data = await _device_fetch_bytes(device_id, "fs/office-pdf", {"path": rel})
    return Response(content=data, media_type="application/pdf")


class ExecAuditItem(BaseModel):
    id: str
    device_id: str
    conversation_id: str | None = None
    tool: str
    ok: bool
    error: str | None = None
    duration_ms: int = 0
    created_at: str | None = None


@router.get("/executions", response_model=list[ExecAuditItem])
async def list_executions(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """W19 P3：远端执行审计（用户维度倒序）。"""
    from app.services.client_exec_audit_service import recent_execs

    rows = await recent_execs(db, user_id=current_user.id, limit=limit)
    return [
        ExecAuditItem(
            id=r.id,
            device_id=r.device_id,
            conversation_id=r.conversation_id,
            tool=r.tool,
            ok=bool(r.ok),
            error=r.error,
            duration_ms=r.duration_ms or 0,
            created_at=_iso(r.created_at),
        )
        for r in rows
    ]
