# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W4-1 死磕步骤对齐回退（记忆/长程 v2，2026-10-23，判据 #18）。

三元组 ``(step_id, context_cursor, snapshot_id)`` 落 ``step_checkpoints`` 表
（AgentRewind 2608.14380：上下文+环境对齐检查点，可回退续跑）。

语义（判据 #18「不重复已完成步」）：
- ``record_step_checkpoint`` 同 ``(deathmatch_id, step_id)`` 可重复记录，取最新；
- ``rewind_to_step`` 只读报告——返回 ``steps_discarded`` = 目标步之后的步（=需要
  重跑的步），目标步本身已完成、不重跑；调用方据此从 ``context_cursor`` 续跑。
  本函数不删除/不改写任何 checkpoint 行（重跑步会再 record 一条新记录）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.db.database import StepCheckpoint

logger = logging.getLogger(__name__)


async def record_step_checkpoint(
    db: Any,
    deathmatch_id: str,
    step_id: str,
    context_cursor: int,
    snapshot_id: Optional[str] = None,
) -> StepCheckpoint:
    """落一条 (step_id, context_cursor, snapshot_id) 三元组检查点并 commit。

    同 (deathmatch_id, step_id) 允许多条记录，读取端取最新（created_at）。
    """
    row = StepCheckpoint(
        deathmatch_id=str(deathmatch_id),
        step_id=str(step_id),
        context_cursor=int(context_cursor or 0),
        snapshot_id=(str(snapshot_id) if snapshot_id else None),
    )
    db.add(row)
    await db.commit()
    try:
        await db.refresh(row)
    except Exception:  # refresh 失败不阻塞（部分 detach 场景）
        logger.debug("checkpoint refresh skipped", exc_info=True)
    return row


async def rewind_to_step(db: Any, deathmatch_id: str, step_id: str) -> Dict[str, Any]:
    """回退到某一步的最新 checkpoint。

    返回：
    - 命中：{"restored": True, "step_id", "context_cursor", "snapshot_id",
      "steps_discarded": [其后所有 step_id（去重、保序）]}
      —— steps_discarded 是需要重跑的步；目标步不重复执行。
    - 目标步无 checkpoint：{"restored": False, "reason": "no checkpoint"}

    排序口径：**步序 = 各步首次 checkpoint 的记录序**（= 执行顺序）；目标步的
    (context_cursor, snapshot_id) 取该步**最新**一条 checkpoint。
    """
    target = str(step_id)
    rows: List[StepCheckpoint] = (await db.execute(
        select(StepCheckpoint)
        .where(StepCheckpoint.deathmatch_id == str(deathmatch_id))
        .order_by(StepCheckpoint.created_at.asc(), StepCheckpoint.id.asc())
    )).scalars().all()

    first_seen: Dict[str, int] = {}
    latest_of: Dict[str, StepCheckpoint] = {}
    for i, r in enumerate(rows):
        if r.step_id not in first_seen:
            first_seen[r.step_id] = i
        latest_of[r.step_id] = r  # 取最新

    if target not in latest_of:
        return {"restored": False, "reason": "no checkpoint"}

    cur = latest_of[target]
    discarded: List[str] = [
        sid for sid, idx in sorted(first_seen.items(), key=lambda kv: kv[1])
        if idx > first_seen[target] and sid != target
    ]
    return {
        "restored": True,
        "step_id": cur.step_id,
        "context_cursor": int(cur.context_cursor or 0),
        "snapshot_id": cur.snapshot_id,
        "steps_discarded": discarded,
    }
