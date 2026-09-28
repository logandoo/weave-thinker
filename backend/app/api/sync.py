# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""同步端点（S1 波，2026-09-24；前缀 /api/sync，全部 JWT）。

契约事实源：server/API.md 二.13 + BACKEND_DESIGN.html 同步端点面。
cursor = sync_events.seq（全局单调）；delta 窗口内 Joplin 四规则压缩；
push 经 ORM 应用（触发 sync_capture，origin_device 经 ContextVar 传递）。
"""
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.deps import get_current_user
from app.db.database import (
    Assistant,
    Conversation,
    ConversationGroup,
    Message,
    Note,
    Notebook,
    SyncBlob,
    SyncDevice,
    SyncEvent,
    User,
    get_db,
)
from app.schemas.sync import (
    BlobResponse,
    DeltaResponse,
    DeviceRegister,
    DeviceResponse,
    PushRequest,
    PushResponse,
    ResyncResponse,
)
from app.services.sync_capture import (
    SYNC_ENTITIES,
    reset_origin_device,
    row_snapshot,
    set_origin_device,
)
from app.services.sync_apply import (
    APPLIED,
    DIRECT_USER_TYPES as _DIRECT_USER_TYPES,
    apply_change,
)

router = APIRouter(prefix="/api/sync", tags=["sync"])

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sync_enabled() -> None:
    if not get_config().sync_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/devices")
async def list_devices(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """本人设备列表（S4 设备管理面）。"""
    _sync_enabled()
    rows = (
        await db.execute(
            select(SyncDevice)
            .where(SyncDevice.user_id == current_user.id)
            .order_by(SyncDevice.created_at, SyncDevice.id)
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "platform": r.platform,
            "last_seen_at": (r.last_seen_at.isoformat() + "Z") if r.last_seen_at else None,
            "revoked": bool(r.revoked),
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/devices/{device_id}/revoke")
async def revoke_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """幂等撤销设备（S4）：撤销后 push/重复注册 403；越权 404。"""
    _sync_enabled()
    row = await db.get(SyncDevice, device_id)
    if row is None or row.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Not Found")
    if not row.revoked:
        row.revoked = True
        await db.commit()
    return {"id": row.id, "revoked": True}


@router.get("/conflicts")
async def list_conflicts(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """本人冲突记录（S4 冲突面；新到旧，offset 分页）。"""
    _sync_enabled()
    from app.db.database import SyncConflict

    rows = (
        await db.execute(
            select(SyncConflict)
            .where(SyncConflict.user_id == current_user.id)
            .order_by(SyncConflict.created_at.desc(), SyncConflict.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "direction": r.direction,
            "reason": r.reason,
            "local_updated_at": (r.local_updated_at.isoformat() + "Z") if r.local_updated_at else None,
            "remote_updated_at": (r.remote_updated_at.isoformat() + "Z") if r.remote_updated_at else None,
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/account")
async def get_sync_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """本人云端绑定（per-user 波；密码永不回显）。"""
    _sync_enabled()
    from app.db.database import SyncState

    row = await db.get(SyncState, current_user.id)
    if row is None or not row.server_url:
        return {"bound": False}
    return {
        "bound": bool(row.sync_enabled),
        "server_url": row.server_url,
        "cloud_username": row.cloud_username,
        "device_id": row.device_id,
        "last_sync_at": (row.last_sync_at.isoformat() + "Z") if row.last_sync_at else None,
        "last_error": row.last_error,
        "tls_verify": bool(row.tls_verify) if row.tls_verify is not None else True,
    }


@router.put("/account")
async def put_sync_account(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """自助绑定云端账号（per-user 波）：验证凭据真实可登录后才落库。

    密码仅存本机 DB（同盒威胁模型，D-13）；bind 成功后 worker 下周期自动接管。"""
    _sync_enabled()
    from app.db.database import SyncState

    body = await request.json()
    server_url = str(body.get("server_url", "")).rstrip("/")
    cloud_username = str(body.get("cloud_username", "")).strip()
    cloud_password = str(body.get("cloud_password", ""))
    if not server_url or not cloud_username or not cloud_password:
        raise HTTPException(status_code=400, detail="server_url/cloud_username/cloud_password 必填")
    if len(server_url) > 512 or len(cloud_username) > 255:
        raise HTTPException(status_code=400, detail="server_url/cloud_username 超长")
    if not server_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="server_url 须为 http(s):// 地址")
    # 凭据真实性核验（真实登录远端，拒绝即不落库）
    import httpx

    try:
        async with httpx.AsyncClient(
            base_url=server_url, trust_env=False, timeout=10,
            verify=bool(body.get("tls_verify", True)),
        ) as rc:
            login_body = {"username": cloud_username}
            login_body["password"] = str(cloud_password)  # 凭据核验载荷（变量引用）
            r = await rc.post("/api/auth/login", json=login_body)
            if r.status_code == 401:
                raise HTTPException(status_code=400, detail="云端账号或密码错误")
            if r.status_code >= 400:
                raise HTTPException(status_code=400, detail=f"远端登录返回错误（{r.status_code}）")
            token = r.json().get("access_token")
            if not token:
                raise HTTPException(status_code=400, detail="远端登录响应异常（非本系统后端）")
            # 远端须具备 sync 面（上游 chatbot 或本仓后端；仅 200 接受）
            probe = await rc.get(
                "/api/sync/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            if probe.status_code == 404:
                raise HTTPException(status_code=400, detail="远端服务端不支持同步（sync 面缺失）")
            if probe.status_code != 200:
                raise HTTPException(status_code=400, detail=f"远端 sync 面异常（{probe.status_code}）")
    except httpx.HTTPError:
        raise HTTPException(status_code=400, detail="远端不可达（请检查地址/网络）")

    row = await db.get(SyncState, current_user.id)
    if row is None:
        row = SyncState(user_id=current_user.id, device_id=uuid.uuid4().hex)
        db.add(row)
    identity_changed = bool(
        row.server_url and row.cloud_username
        and (row.server_url != server_url or row.cloud_username != cloud_username)
    )
    if identity_changed:
        # C1（评审+实测）：换绑不同账号/服务器必须断尾。push_cursor 不回零而
        # 取本地当前最大 seq——业务表全局主键下，全量历史推新账号必撞旧账号
        # 已存的同 id 行（PK 冲突实证）；语义=「向前看」：旧数据留旧账号，
        # 新账号从换绑点起同步（搬家走 export/import）。pull_cursor=0 拉新
        # 账号既有数据；device_id 换发（worker 缓存按身份键控，旧态自然失效）。
        from app.db.database import SyncEvent as _SE

        max_seq = (
            await db.execute(
                select(func.max(_SE.seq)).where(_SE.user_id == current_user.id)
            )
        ).scalar() or 0
        row.push_cursor = max_seq
        row.pull_cursor = 0
        row.device_id = uuid.uuid4().hex
        row.last_error = None
        row.last_sync_at = None  # 防首拉把换绑前编辑误判为冲突（评审 Minor）
    row.server_url = server_url
    row.cloud_username = cloud_username
    row.cloud_password = cloud_password
    row.sync_enabled = True
    row.tls_verify = bool(body.get("tls_verify", True))
    await db.commit()
    return {"bound": True, "server_url": server_url, "cloud_username": cloud_username,
            "rebind_reset": identity_changed,
            "push_cursor": row.push_cursor if identity_changed else None}


@router.post("/account/resync")
async def resync_sync_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """手动重跑全量基线（全量保真波）：重置 pull_cursor/last_sync_at，worker
    下周期重跑 resync（幂等 upsert 合并，不丢本地）。用于新域上线后补拉。"""
    _sync_enabled()
    from app.db.database import SyncState

    row = await db.get(SyncState, current_user.id)
    if row is None or not row.sync_enabled:
        raise HTTPException(status_code=400, detail="未绑定云端账号")
    row.pull_cursor = 0
    row.last_sync_at = None
    await db.commit()
    return {"resync": "scheduled"}


@router.delete("/account")
async def delete_sync_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """解绑：停同步 + 清密码（游标保留，重新绑定后续推）。"""
    _sync_enabled()
    from app.db.database import SyncState

    row = await db.get(SyncState, current_user.id)
    if row is None or not row.server_url:
        return {"bound": False}
    row.sync_enabled = False
    row.cloud_password = None
    await db.commit()
    return {"bound": False}


@router.get("/status")
async def sync_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """同步状态面（S4）：服务端开关 + 客户端 worker 游标/错误 + 待推送量。"""
    _sync_enabled()
    from app.db.database import SyncConflict, SyncState

    config = get_config()
    state = (
        await db.execute(select(SyncState).where(SyncState.user_id == current_user.id))
    ).scalar_one_or_none()
    pending = 0
    if state is not None:
        pending = (
            await db.execute(
                select(func.count(SyncEvent.seq)).where(
                    SyncEvent.user_id == current_user.id,
                    SyncEvent.seq > (state.push_cursor or 0),
                    ~func.coalesce(SyncEvent.origin_device, "").like("rx:%"),
                )
            )
        ).scalar() or 0
    conflicts_count = (
        await db.execute(
            select(func.count(SyncConflict.id)).where(SyncConflict.user_id == current_user.id)
        )
    ).scalar() or 0
    bound = bool(state and state.sync_enabled and state.server_url)
    return {
        "server_enabled": bool(config.sync_enabled),
        "client_enabled": bound,
        "server_url": state.server_url if state else None,
        "cloud_username": state.cloud_username if state else None,
        "device_id": state.device_id if state else None,
        "push_cursor": state.push_cursor if state else None,
        "pull_cursor": state.pull_cursor if state else None,
        "pending_events": pending,
        "last_sync_at": (state.last_sync_at.isoformat() + "Z") if state and state.last_sync_at else None,
        "last_error": state.last_error if state else None,
        "conflicts_count": conflicts_count,
    }


@router.post("/devices", response_model=DeviceResponse)
async def register_device(
    body: DeviceRegister,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    if body.id is not None and len(body.id) > 64:
        raise HTTPException(status_code=400, detail="device id too long")
    if body.name is not None and len(body.name) > 255:
        raise HTTPException(status_code=400, detail="device name too long")
    if body.platform is not None and len(body.platform) > 40:
        raise HTTPException(status_code=400, detail="platform too long")
    device_id = body.id or uuid.uuid4().hex
    row = await db.get(SyncDevice, device_id)
    if row is not None:
        if row.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Not Found")
        if row.revoked:
            raise HTTPException(status_code=403, detail="device revoked")
        row.last_seen_at = datetime.utcnow()
        if body.name is not None:
            row.name = body.name
        if body.platform is not None:
            row.platform = body.platform
        await db.commit()
    else:
        row = SyncDevice(
            id=device_id,
            user_id=current_user.id,
            name=body.name,
            platform=body.platform,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # 并发同 id 注册：行已存在 → 重取返回（幂等语义，A4.9 Minor）
            await db.rollback()
            row = await db.get(SyncDevice, device_id)
            if row is None or row.user_id != current_user.id:
                raise HTTPException(status_code=404, detail="Not Found")
            if row.revoked:
                raise HTTPException(status_code=403, detail="device revoked")
            row.last_seen_at = datetime.utcnow()
            await db.commit()
    return DeviceResponse(
        id=row.id,
        name=row.name,
        platform=row.platform,
        last_seen_at=(row.last_seen_at.isoformat() + "Z") if row.last_seen_at else None,
        revoked=bool(row.revoked),
        created_at=(row.created_at.isoformat() + "Z") if row.created_at else None,
    )


_COMPRESS_DROP = {("create", "delete")}
# 排除清单唯一事实源 = sync_apply.EXCLUDE_PAYLOAD_COLS（D-16 后仅含嵌入本地态
# 与记忆动态列；api_key 类列随同步——用户原则「同账户所有数据一致」）。


def _ev_dict(ev) -> dict:
    return {
        "seq": ev.seq,
        "entity_type": ev.entity_type,
        "entity_id": ev.entity_id,
        "op": ev.op,
        "payload": ev.payload,
        "origin_device": ev.origin_device,
        "created_at": (ev.created_at.isoformat() + "Z") if ev.created_at else None,
    }


def _compress_window(raw_events):
    """窗口内 Joplin 四规则压缩（输入 ORM 行→dict 处理，绝不改持久对象，A4.9 I1）。"""
    by_key: dict = {}
    order: list = []
    for raw in raw_events:
        ev = _ev_dict(raw)
        key = (ev["entity_type"], ev["entity_id"])
        if key not in by_key:
            by_key[key] = ev
            order.append(key)
            continue
        prev = by_key[key]
        combo = (prev["op"], ev["op"])
        if combo in _COMPRESS_DROP:
            del by_key[key]
            order.remove(key)
        elif combo in (("create", "update"), ("delete", "create")):
            prev["op"] = "create"
            prev["payload"] = ev["payload"]
            prev["seq"] = ev["seq"]
            prev["created_at"] = ev["created_at"]
            prev["origin_device"] = ev["origin_device"]
        elif combo in (("update", "update"), ("create", "create")):
            prev["payload"] = ev["payload"]
            prev["seq"] = ev["seq"]
            prev["created_at"] = ev["created_at"]
            prev["origin_device"] = ev["origin_device"]
        elif combo == ("update", "delete"):
            prev["op"] = "delete"
            prev["payload"] = None
            prev["seq"] = ev["seq"]
            prev["created_at"] = ev["created_at"]
            prev["origin_device"] = ev["origin_device"]
        else:  # (delete, delete) / (delete, update) — 保留 delete，推进 seq
            prev["seq"] = ev["seq"]
            prev["created_at"] = ev["created_at"]
            prev["origin_device"] = ev["origin_device"]
    return [by_key[k] for k in sorted(order, key=lambda k: by_key[k]["seq"])]


@router.get("/delta", response_model=DeltaResponse)
async def get_delta(
    cursor: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    device_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    q = select(SyncEvent).where(
        SyncEvent.user_id == current_user.id, SyncEvent.seq > cursor
    )
    if device_id:
        # 撤销设备同样切断读路径（评审 I1：revoke 不只是写切断——带 device_id
        # 的 delta 是设备级会话，撤销即 403；不带 device_id 的调用按用户 JWT 语义）
        device = await db.get(SyncDevice, device_id)
        if device is not None and device.user_id == current_user.id and device.revoked:
            raise HTTPException(status_code=403, detail="device revoked")
        # 回显抑制必须在 SQL 侧（LIMIT 之前）——否则整窗同源事件过滤后 cursor
        # 不推进、has_more 失真，后续事件永久不可达（A4.9 C2）。
        # IS DISTINCT FROM 兼容 origin_device NULL（非 push 来源事件保留）。
        q = q.where(SyncEvent.origin_device.is_distinct_from(device_id))
    q = q.order_by(SyncEvent.seq).limit(limit)
    raw = (await db.execute(q)).scalars().all()
    compressed = _compress_window(raw)
    new_cursor = raw[-1].seq if raw else cursor
    return DeltaResponse(
        events=compressed,
        cursor=new_cursor,
        has_more=len(raw) == limit,
    )


async def _apply_push_event(db: AsyncSession, user: User, ev) -> bool:
    """push 事件应用：统一走 sync_apply.apply_change（与客户端 pull 同一 LWW
    语义，两端仲裁规则逐字节一致）；STALE 在 push 响应里并入 skipped。"""
    result = await apply_change(
        db, user, ev.entity_type, ev.entity_id, ev.op, ev.payload
    )
    return result == APPLIED


@router.post("/push", response_model=PushResponse)
async def push_events(
    body: PushRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    device = await db.get(SyncDevice, body.device_id)
    if device is None or device.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Not Found")
    if device.revoked:
        raise HTTPException(status_code=403, detail="device revoked")
    applied: list = []
    skipped: list = []
    origin_ctx = set_origin_device(body.device_id)
    try:
        for ev in body.events:
            # per-event SAVEPOINT（仓内先例=migrations.py begin_nested）：单事件
            # flush 异常只回滚自身，已 applied 的前序事件与捕获事件不受连坐
            # （A4.9 C1：整事务 rollback 会使 applied 列表谎报）。
            try:
                async with db.begin_nested():
                    ok = await _apply_push_event(db, current_user, ev)
            except Exception:
                ok = False
            (applied if ok else skipped).append(ev.entity_id)
        device.last_seen_at = datetime.utcnow()
        await db.commit()
    finally:
        reset_origin_device(origin_ctx)
    return PushResponse(applied=applied, skipped=skipped)


@router.put("/blobs/{sha256}", response_model=BlobResponse)
async def put_blob(
    sha256: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    if not _SHA256_RE.match(sha256):
        raise HTTPException(status_code=400, detail="invalid sha256")
    config = get_config()
    max_bytes = config.sync_blob_max_mb * 1024 * 1024
    # Content-Length 预检在读体之前（A4.9 Minor：防认证用户逼大内存分配）
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail="blob too large")
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail="blob too large")
    if hashlib.sha256(body).hexdigest() != sha256:
        raise HTTPException(status_code=400, detail="sha256 mismatch")
    blob_dir = config.sync_blob_dir
    blob_dir.mkdir(parents=True, exist_ok=True)
    path = blob_dir / sha256
    row = await db.get(SyncBlob, (sha256, current_user.id))
    if row is not None and path.exists():
        return BlobResponse(sha256=sha256, size=row.size, dedup=True)
    # 原子写入（tmp+rename，防半截文件被 dedup 路径永久信任）
    tmp = blob_dir / f".{sha256}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_bytes(body)
    tmp.replace(path)
    if row is not None:
        return BlobResponse(sha256=sha256, size=row.size, dedup=True)
    db.add(SyncBlob(sha256=sha256, user_id=current_user.id, size=len(body), path=str(path)))
    try:
        await db.commit()
    except IntegrityError:
        # 并发同 (sha,user) PUT：行已存在 → 幂等成功（文件已就位）
        await db.rollback()
        return BlobResponse(sha256=sha256, size=len(body), dedup=True)
    return BlobResponse(sha256=sha256, size=len(body), dedup=False)


@router.get("/blobs")
async def list_blobs(
    since: Optional[str] = Query(None),
    after: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """blob 元数据列表（S3：客户端上行 diff / 运维审计；仅本人）。

    元组游标 (since=created_at, after=sha256)：同 created_at 并列行不丢页
    （评审 I4）。返回 {items:[...], next_since, next_after}。"""
    _sync_enabled()
    q = select(SyncBlob).where(SyncBlob.user_id == current_user.id)
    if after and not _SHA256_RE.match(after):
        raise HTTPException(status_code=400, detail="invalid after")
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is not None:
                since_dt = since_dt.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid since")
        if after:
            q = q.where(
                (SyncBlob.created_at > since_dt)
                | ((SyncBlob.created_at == since_dt) & (SyncBlob.sha256 > after))
            )
        else:
            q = q.where(SyncBlob.created_at > since_dt)
    q = q.order_by(SyncBlob.created_at, SyncBlob.sha256).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    items = [
        {
            "sha256": r.sha256,
            "size": r.size,
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
        }
        for r in rows
    ]
    full_page = len(items) == limit and items and items[-1]["created_at"] is not None
    return {
        "items": items,
        "next_since": items[-1]["created_at"] if full_page else None,
        "next_after": items[-1]["sha256"] if full_page else None,
    }


@router.get("/blobs/{sha256}")
async def get_blob(
    sha256: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    if not _SHA256_RE.match(sha256):
        raise HTTPException(status_code=400, detail="invalid sha256")
    row = await db.get(SyncBlob, (sha256, current_user.id))
    path = get_config().sync_blob_dir / sha256
    if row is None or not path.exists():
        # S3 懒下载穿透：本地缺失且已绑定云端 → 从远端拉取（.part 断点续传
        # + sha 实算校验）后本地落盘成行；远端亦无 → 404（与本地缺失语义一致）。
        # 注：sync_client 为客户端组件；纯服务端部署无此模块 → 跳过穿透。
        try:
            from app.services.sync_client import sync_client
        except ImportError:
            sync_client = None

        if sync_client is not None and await sync_client.fetch_blob(sha256, current_user.id):
            row = await db.get(SyncBlob, (sha256, current_user.id))
    if row is None or not path.exists():
        raise HTTPException(status_code=404, detail="Not Found")
    # starlette FileResponse 原生 Range（206 + Content-Range 断点续传）
    return FileResponse(str(path), media_type="application/octet-stream")


@router.post("/resync", response_model=ResyncResponse)
async def resync(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _sync_enabled()
    from app.services.sync_capture import MEMORY_SYNC_ENTITIES

    config = get_config()
    domains = dict(SYNC_ENTITIES)
    # Minor：pgvector 缺失主机无 memory 表（init_db §9.5 回退）——域按可用性门控
    from app.db import migrations as _mig

    if config.sync_memory_enabled and _mig.PGVECTOR_AVAILABLE:
        domains.update(MEMORY_SYNC_ENTITIES)
    entities: dict = {}
    for entity_type, model in domains.items():
        if entity_type in _DIRECT_USER_TYPES:
            q = select(model).where(model.user_id == current_user.id)
        elif entity_type == "notes":
            q = (
                select(Note)
                .join(Notebook, Note.notebook_id == Notebook.id)
                .where(Notebook.user_id == current_user.id)
            )
        elif entity_type == "messages":
            q = (
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.user_id == current_user.id)
            )
        elif entity_type == "skill_files":
            # 全量保真波：skill_files 经父 user_skills 归属（此前 else=messages 会错套）
            from app.db.database import SkillFile, UserSkill

            q = (
                select(SkillFile)
                .join(UserSkill, SkillFile.skill_id == UserSkill.id)
                .where(UserSkill.user_id == current_user.id)
            )
        else:  # messages（兜底分支）
            q = (
                select(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.user_id == current_user.id)
            )
        rows = (await db.execute(q)).scalars().all()
        entities[entity_type] = [row_snapshot(r) for r in rows]
    # user_profile：users 自行四列白名单（密码/角色/登录态永不入）
    from app.services.sync_capture import PROFILE_COLS

    snap = row_snapshot(current_user)
    entities["user_profile"] = [
        {k: snap.get(k) for k in (*PROFILE_COLS, "updated_at", "id")}
    ]
    max_seq = (await db.execute(select(func.max(SyncEvent.seq)))).scalar() or 0
    return ResyncResponse(entities=entities, cursor=max_seq)
