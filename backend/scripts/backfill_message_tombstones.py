# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""消息删除同步缺口——存量 backfill（UPSTREAM_TODO_20261031 §5.1 P3，2026-10-31）。

背景：P0 修复前 `_delete_message_slice`（编辑重发/重新生成）走 Core 批量 DELETE，
绕过 mapper 事件——`sync_events` 零 delete、`sync_tombstones` 零墓碑。客户端只
收到过被删消息的 `messages/create`，delta 收不到 delete、resync 只做 upsert+墓碑
不做快照差集删除 → 旧回答在所有设备永久残留。

本脚本用 `sync_events` 残留的 create 事件反查存量孤儿，补发 delete 事件 + 墓碑，
让**所有**客户端（不只是本地）在 delta/resync 后收敛：

    命中条件（四条全满足）：
    1. `sync_events` 存在 entity_type=messages、op=create 的事件（payload 带
       conversation_id——无 cid 的跳过，无法验证会话在场）；
    2. `messages` 中该行已不存在（仍在的跳过）；
    3. `sync_tombstones` 无 (user_id, messages, entity_id) 墓碑（幂等，重复跑
       不产生重复墓碑）；
    4. payload.conversation_id 对应的会话**仍存在**（避免给已删会话刷墓碑——
       会话删除本身已随 conversation delete 事件下发）。

写入面（与 sync_capture `_emit` delete 分支同构）：delete 事件 payload=NULL、
origin_device=NULL（服务端补发，不是设备回显）；墓碑 upsert 冲突即跳过。
分批 500，每批一个提交点，批内**重新检查** messages 是否仍不存在（fail-safe：
find 与 apply 之间同 id 消息复活则跳过）。

⚠ 操作须知：
- **必须在 sync_events 保留期内跑**：`sync_events` 会按龄裁剪，
  create 事件被裁掉后该 id 无法反查，只能退回客户端本地清理（规格 §5.2 L1）。
- `sync_events` 只有 (user_id, seq) 索引；本脚本按 seq 键集分页扫 messages/create，
  大表上是一次有界顺序扫描——低峰期执行，必要时先建临时索引
  `CREATE INDEX CONCURRENTLY ON sync_events (entity_type, op) WHERE op='create'`。
- 半完成态（上次执行只写了墓碑没写 delete 事件，或事件已被保留策略裁掉）：
  本脚本按契约跳过（`t.id IS NULL` 条件），在线客户端靠 resync 墓碑回放收敛；
  如需精确补 event，按规格 §5.1 用 `--since` 限定另行人工核查。
- 一次性脚本：**不挂启动钩子**。先 dry-run 核对计数与样本，再 `--execute`；
  上线生产前先在备份库演练。

用法（from backend/）：
    python -m scripts.backfill_message_tombstones                 # dry-run（默认）
    python -m scripts.backfill_message_tombstones --since 2026-09-01
    python -m scripts.backfill_message_tombstones --execute
    python -m scripts.backfill_message_tombstones --execute --batch 500
"""
import argparse
import asyncio
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.database import (
    AsyncSessionLocal, Conversation, Message, SyncEvent, SyncTombstone,
)

ENTITY = "messages"
DEFAULT_BATCH = 500
SAMPLE_LIMIT = 10


async def find_orphan_created_ids(db, *, since=None, batch: int = DEFAULT_BATCH):
    """扫描 sync_events 的 messages/create，返回存量孤儿列表。

    返回 [{'user_id', 'entity_id', 'conversation_id', 'create_seq'}]，
    按 create seq 升序、(user_id, entity_id) 去重。`since`（datetime）限定
    事件 created_at 下界，用于控制扫描面。
    """
    orphans = []
    seen = set()
    cursor = 0
    while True:
        stmt = (
            select(SyncEvent.seq, SyncEvent.user_id, SyncEvent.entity_id,
                   SyncEvent.payload)
            .where(SyncEvent.entity_type == ENTITY, SyncEvent.op == "create",
                   SyncEvent.seq > cursor)
            .order_by(SyncEvent.seq)
            .limit(batch)
        )
        if since is not None:
            stmt = stmt.where(SyncEvent.created_at >= since)
        rows = (await db.execute(stmt)).fetchall()
        if not rows:
            break
        cursor = rows[-1][0]

        # 候选：payload 带 conversation_id 且未去重
        cand = []
        for seq, user_id, entity_id, payload in rows:
            cid = None
            if isinstance(payload, dict):
                cid = payload.get("conversation_id")
            if not entity_id or not cid:
                continue
            key = (user_id, str(entity_id))
            if key in seen:
                continue
            seen.add(key)
            cand.append((seq, user_id, str(entity_id), str(cid)))
        if not cand:
            continue

        ids = [c[2] for c in cand]
        cids = {c[3] for c in cand}
        users = {c[1] for c in cand}
        alive_ids = set((await db.execute(
            select(Message.id).where(Message.id.in_(ids))
        )).scalars().all())
        tombstoned = {
            (t_user, t_entity)
            for t_user, t_entity in (await db.execute(
                select(SyncTombstone.user_id, SyncTombstone.entity_id).where(
                    SyncTombstone.entity_type == ENTITY,
                    SyncTombstone.user_id.in_(users),
                    SyncTombstone.entity_id.in_(ids),
                )
            )).fetchall()
        }
        alive_convs = set((await db.execute(
            select(Conversation.id).where(Conversation.id.in_(cids))
        )).scalars().all())

        for seq, user_id, entity_id, cid in cand:
            if entity_id in alive_ids:
                continue
            if (user_id, entity_id) in tombstoned:
                continue
            if cid not in alive_convs:
                continue
            orphans.append({
                "user_id": user_id,
                "entity_id": entity_id,
                "conversation_id": cid,
                "create_seq": seq,
            })
    return orphans


async def apply_backfill(db, orphans, *, execute: bool = True,
                         batch: int = DEFAULT_BATCH) -> dict:
    """把孤儿列表补成 delete 事件 + 墓碑。dry-run（execute=False）零写入。

    分批 `batch`，每批一个提交点；批内重新检查 messages 是否仍不存在
    （find 与 apply 之间复活的跳过）。返回 {'written', 'skipped_existing',
    'batches'}。
    """
    out = {"written": 0, "skipped_existing": 0, "batches": 0}
    if not orphans:
        return out
    for i in range(0, len(orphans), batch):
        chunk = orphans[i:i + batch]
        out["batches"] += 1
        if not execute:
            continue
        ids = [o["entity_id"] for o in chunk]
        # fail-safe 批内复查：同 id 复活（极小概率复用/并发重建）则跳过
        revived = set((await db.execute(
            select(Message.id).where(Message.id.in_(ids))
        )).scalars().all())
        for o in chunk:
            if o["entity_id"] in revived:
                out["skipped_existing"] += 1
                continue
            await db.execute(
                SyncEvent.__table__.insert().values(
                    user_id=o["user_id"],
                    entity_type=ENTITY,
                    entity_id=o["entity_id"],
                    op="delete",
                    payload=None,
                    origin_device=None,  # 服务端补发，非设备回显
                    created_at=datetime.utcnow(),
                )
            )
            await db.execute(
                pg_insert(SyncTombstone.__table__).values(
                    id=str(uuid.uuid4()),
                    user_id=o["user_id"],
                    entity_type=ENTITY,
                    entity_id=o["entity_id"],
                    deleted_at=datetime.utcnow(),
                    payload=None,
                ).on_conflict_do_nothing(constraint="uq_sync_tombstone")
            )
            out["written"] += 1
        await db.commit()
    return out


async def _main(since, execute: bool, batch: int) -> None:
    async with AsyncSessionLocal() as db:
        orphans = await find_orphan_created_ids(db, since=since, batch=batch)
        print(f"[backfill_message_tombstones] orphans={len(orphans)} "
              f"(create 事件在 · messages 行不在 · 无墓碑 · 会话仍在)")
        for o in orphans[:SAMPLE_LIMIT]:
            print(f"  - user={o['user_id']} msg={o['entity_id']} "
                  f"conv={o['conversation_id']} create_seq={o['create_seq']}")
        if len(orphans) > SAMPLE_LIMIT:
            print(f"  … 其余 {len(orphans) - SAMPLE_LIMIT} 条省略")
        if not orphans:
            print("nothing to backfill")
            return
        if not execute:
            print("DRY-RUN: nothing written (rerun with --execute to apply)")
            return
        out = await apply_backfill(db, orphans, execute=True, batch=batch)
        print(f"  written: delete events + tombstones = {out['written']} "
              f"(skipped revived={out['skipped_existing']}, "
              f"batches={out['batches']})")
        print("  note: 在线设备将在下一次 /api/sync/delta 收到 delete；"
              "越窗设备经 resync 墓碑回放收敛。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="存量孤儿 create 事件补 messages/delete + 墓碑（一次性，默认 dry-run）")
    ap.add_argument("--execute", action="store_true",
                    help="真写（默认 dry-run 只报数与样本）")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="只处理该日期之后的 create 事件（控制扫描面）")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                    help=f"分批大小（默认 {DEFAULT_BATCH}）")
    args = ap.parse_args()
    _since = None
    if args.since:
        _since = datetime.strptime(args.since, "%Y-%m-%d")
    asyncio.run(_main(_since, args.execute, args.batch))
