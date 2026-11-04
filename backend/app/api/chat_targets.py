# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Chat execution-target helpers (P5.3 move-only, 2026-10-18)."""
from __future__ import annotations

from typing import Any


async def _resolve_execution_target_for_conversation(
    db, current_user, conversation, request_target,
) -> None:
    """Adopt a target only while the conversation has no prior messages."""
    from datetime import datetime as _datetime

    from fastapi import HTTPException as _HTTPException
    from sqlalchemy import select as _select

    from app.db.database import Conversation as _Conversation
    from app.db.database import Message as _Message
    from app.services import client_device_service as _cds

    req_target = None
    if request_target:
        try:
            req_target = _cds.normalize_execution_target(request_target)
        except ValueError as exc:
            raise _HTTPException(status_code=422, detail=str(exc))
    if not req_target:
        return

    result = await db.execute(
        _select(_Conversation)
        .where(
            _Conversation.id == conversation.id,
            _Conversation.user_id == current_user.id,
        )
        .with_for_update()
    )
    conversation = result.scalar_one()
    conv_target = conversation.execution_target
    if conv_target and req_target != conv_target:
        raise _HTTPException(
            status_code=400,
            detail="执行环境已固定：切换云端/客户端执行环境请新建会话",
        )
    if conv_target:
        return

    try:
        await _cds.assert_device_usable(db, current_user.id, req_target)
    except LookupError as exc:
        raise _HTTPException(status_code=400, detail=str(exc))
    has_messages = (await db.execute(
        _select(_Message.id)
        .where(_Message.conversation_id == conversation.id)
        .limit(1)
    )).first() is not None
    if has_messages:
        raise _HTTPException(
            status_code=400,
            detail="执行环境已固定：切换云端/客户端执行环境请新建会话",
        )
    conversation.execution_target = req_target
    conversation.updated_at = _datetime.utcnow()
    await db.commit()
