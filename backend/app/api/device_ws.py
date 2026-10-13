# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W16 设备执行模式 — 设备出站 WS 端点（/api/device/ws，PLAN §5/§6.2）。

鉴权（A4.9 I-2 修订）：**仅** `Authorization: Bearer <device token>` 头——
长效设备凭据不进 URL/访问日志（与用户 JWT 的 files.py `?token=` 预留口不同）；
revoked/未知 token → 握手拒绝。会话：register → receive loop
（result/error/chunk/heartbeat/progress）→ 断线按会话身份 unregister
（A4.9 C-1：迟到的旧连接 finally 不拆新连接）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.services import client_device_service as cds
from app.services.device_session_service import device_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/device", tags=["client-device-ws"])


def _bearer_token(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


@router.websocket("/ws")
async def device_exec_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    presented = _bearer_token(websocket)  # 仅 header；query token 一律拒绝
    device = await cds.verify_device_token(db, presented) if presented else None
    if device is None:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="invalid device token"
        )
        return

    await websocket.accept()
    session = device_session_manager.register(device.device_id, device.user_id, websocket)
    # r4 握手建连复核：token 校验(T0)→register(T1) 窗口内发生 revoke+他户重配对时
    # 会话属主与现行行失配——复核不合即拆（会话-行竞态闭合）。
    try:
        again = await cds.verify_device_token(db, presented)
    except Exception:
        again = None
    if again is None or again.device_id != session.device_id or again.user_id != session.user_id:
        device_session_manager.unregister(session.device_id, session)
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="device binding changed"
        )
        return
    try:
        while True:
            msg = await websocket.receive_json()
            device_session_manager.handle_message(session, msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("device ws session error for %s: %s", device.device_id, exc)
    finally:
        device_session_manager.unregister(device.device_id, session)
        try:
            await _touch_last_seen(db, device.device_id)
        except Exception:
            pass


async def _touch_last_seen(db: AsyncSession, device_id: str) -> None:
    from datetime import datetime

    from sqlalchemy import update

    from app.db.database import ClientExecutorDevice

    await db.execute(
        update(ClientExecutorDevice)
        .where(ClientExecutorDevice.device_id == device_id)
        .values(last_seen_at=datetime.utcnow())
    )
    await db.commit()
