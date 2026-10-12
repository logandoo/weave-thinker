# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""同步历史保留核心（UPSTREAM_TODO_20261007 项8，W7 评审 A/B；W8 项8a/8d 收口）。

背景：message 事件随项1 携带全文后，`sync_events` 无裁剪会双份存储（payload
表 + 事件日志，两端均然）。本模块提供**按龄保留**核心；配套缺口信号（delta 的
`min_available_seq` / `resync_required`）让越窗客户端改走全量重同步——
对齐 Joplin（180d 保留 + `resyncRequired`）与 smrt（floor + `resyncRequired`）
范式：保留策略必须配缺口信号，「绝不静默返回不完整数据」。

W8（UPSTREAM_TODO_20261007 项 8a/8d）：
- **per-user 裁剪水位**（D-w8-1）：每次真删前把各用户被删的最大 seq 落
  `sync_prune_watermarks`（max 合并，只增不减）——「日志被全部裁剪」时
  `min_seq=None`，仅靠保留沿判不出缺口；水位让 delta 仍能提示全量重同步。
- **push 侧保护**（D-w8-4 + 复审修正 D-w8-6）：本地实例的 `sync_events` 同时是
  出站队列，裁剪不得删「仍可能被 `_push` 发送」的事件——保护条件与
  `sync_client._push` 的选择面同源：绑定在场（`server_url`+`cloud_username`；
  解绑可续推故不因 enabled/密码解除）且 `seq > push_cursor` 且 `origin` 非 `rx:`。

⚠ 复审裁定（D-w4）：**墓碑不随龄裁剪**——resync 为 upsert 合并、不做本地删除
对账；删墓碑只会加重越窗设备的删除分歧（被删实体可能复活）。墓碑行极小，
保留成本可忽略。仅 `sync_events` 按龄裁剪。

操作入口：`backend/scripts/prune_sync_events.py`（dry-run 默认，`--execute` 真删）。
"""
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select

from app.db.database import SyncEvent, SyncPruneWatermark, SyncState


async def _bound_push_bindings(db) -> list:
    """「仍可能推送」的绑定（user_id, push_cursor）。

    绑定判定（D-w8-6 复审修正）：`server_url`+`cloud_username` 在场即绑定——
    解绑（DELETE /account 清密码/停用）语义为「游标保留，重新绑定后续推」，
    仅凭 enabled/password 解除保护会让同身份重绑后的未推送事件永久丢失。
    身份变更重绑时 push_cursor 被端点重置为本地 max seq（向前看），旧事件
    天然落在保护面外，语义自洽。
    """
    return (await db.execute(
        select(SyncState.user_id, SyncState.push_cursor).where(
            SyncState.server_url.is_not(None),
            SyncState.cloud_username.is_not(None),
        )
    )).all()


def _age_cond(cutoff: datetime):
    """年龄谓词：created_at NULL 遗留行按「年龄未知=最老」计入（防永久泄漏）。"""
    return or_(SyncEvent.created_at.is_(None), SyncEvent.created_at < cutoff)


def _protect_cond(bound) -> "object | None":
    """未推送保护谓词（与 sync_client._push 同源：seq>push_cursor ∧ origin 非 rx:）。"""
    if not bound:
        return None
    return or_(*[
        and_(SyncEvent.user_id == uid, SyncEvent.seq > (pc or 0),
             ~func.coalesce(SyncEvent.origin_device, "").like("rx:%"))
        for uid, pc in bound
    ])


async def prune_sync_preview(db, days: int) -> dict:
    """dry-run 预览（不写库）：候选/受保护/可删计数（操作员删除前可核对保护面）。"""
    if days <= 0:
        return {"skipped": "days<=0（保留关闭/dry 语义）", "candidates": 0,
                "protected_unpushed": 0, "deletable": 0}
    cutoff = datetime.utcnow() - timedelta(days=days)
    base = _age_cond(cutoff)
    protect = _protect_cond(await _bound_push_bindings(db))
    candidates = (await db.execute(
        select(func.count()).select_from(SyncEvent).where(base)
    )).scalar() or 0
    protected = 0
    if protect is not None:
        protected = (await db.execute(
            select(func.count()).select_from(SyncEvent).where(base, protect)
        )).scalar() or 0
    return {
        "cutoff": cutoff.isoformat() + "Z",
        "candidates": int(candidates),
        "protected_unpushed": int(protected),
        "deletable": int(candidates) - int(protected),
    }


async def prune_sync_history(db, days: int) -> dict:
    """按龄删除 sync_events（days<=0 = no-op/关闭语义）；墓碑不裁剪（D-w4）。

    已绑定用户的未推送事件（`seq > push_cursor` 且非 rx:）受保护不删
    （D-w8-4/D-w8-6）；被删事件按用户记录水位（max 合并）。返回
    {deleted_events, cutoff, protected_unpushed, watermark_users}。
    """
    if days <= 0:
        return {"skipped": "days<=0（保留关闭/dry 语义）", "deleted_events": 0}
    cutoff = datetime.utcnow() - timedelta(days=days)
    base = _age_cond(cutoff)
    bound = await _bound_push_bindings(db)
    protect = _protect_cond(bound)
    protected_unpushed = 0
    if protect is not None:
        protected_unpushed = (await db.execute(
            select(func.count()).select_from(SyncEvent).where(base, protect)
        )).scalar() or 0
    delete_cond = and_(base, ~protect) if protect is not None else base
    # 水位：先记「将被删」的 per-user 最大 seq（与删除同事务提交）
    rows = (await db.execute(
        select(SyncEvent.user_id, func.max(SyncEvent.seq))
        .where(delete_cond)
        .group_by(SyncEvent.user_id)
    )).all()
    watermark_users = 0
    if rows:
        ids = [uid for uid, _mx in rows]
        existing = {w.user_id: w for w in (await db.execute(
            select(SyncPruneWatermark).where(SyncPruneWatermark.user_id.in_(ids))
        )).scalars().all()}
        for uid, mx in rows:
            mx = int(mx or 0)
            w = existing.get(uid)
            if w is None:
                db.add(SyncPruneWatermark(
                    user_id=uid, max_deleted_seq=mx, updated_at=datetime.utcnow()))
                watermark_users += 1
            elif mx > (w.max_deleted_seq or 0):
                w.max_deleted_seq = mx
                w.updated_at = datetime.utcnow()
                watermark_users += 1
    ev = await db.execute(delete(SyncEvent).where(delete_cond))
    await db.commit()
    return {
        "deleted_events": ev.rowcount or 0,
        "cutoff": cutoff.isoformat() + "Z",
        "protected_unpushed": int(protected_unpushed),
        "watermark_users": watermark_users,
    }


async def sync_history_stats(db) -> dict:
    """保留沿/水位（供脚本打印与越窗告警文案）。"""
    min_seq = (await db.execute(select(func.min(SyncEvent.seq)))).scalar()
    max_seq = (await db.execute(select(func.max(SyncEvent.seq)))).scalar()
    return {"min_available_seq": min_seq, "max_seq": max_seq}
