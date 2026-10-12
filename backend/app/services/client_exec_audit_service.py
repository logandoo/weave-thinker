# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W19 P3 — 远端执行审计服务（client_exec_audit）。

record_exec：默认开独立 AsyncSessionLocal 写入（不干扰调用方事务——
agent_loop 的 db 会话可能持有未提交状态）；测试可注入 db。
recent_execs：用户维度倒序查询（GET /api/devices/executions）。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import ClientExecAudit

logger = logging.getLogger(__name__)


async def record_exec(
    db: Optional[AsyncSession],
    *,
    user_id: str,
    device_id: str,
    conversation_id: Optional[str],
    tool: str,
    ok: bool,
    error: Optional[str],
    duration_ms: int,
) -> None:
    row = ClientExecAudit(
        user_id=user_id,
        device_id=device_id,
        conversation_id=conversation_id,
        tool=tool,
        ok=bool(ok),
        error=(error or None),
        duration_ms=int(duration_ms or 0),
    )
    if db is not None:
        db.add(row)
        await db.flush()
        return
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        session.add(row)
        await session.commit()


async def recent_execs(db: AsyncSession, *, user_id: str, limit: int = 20) -> List[Any]:
    from sqlalchemy import desc

    result = await db.execute(
        select(ClientExecAudit)
        .where(ClientExecAudit.user_id == user_id)
        .order_by(ClientExecAudit.created_at.desc(), desc(ClientExecAudit.id))
        .limit(max(1, min(int(limit or 20), 100)))
    )
    return list(result.scalars().all())
