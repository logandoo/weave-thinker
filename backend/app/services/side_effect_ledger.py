# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""side_effect_ledger — 副作用台账：重放不重复执行。

协议：幂等键 make_key(principal_type, principal_id, cursor, tool_name, call_id)，
keyed on cursor 不是 seq（重放同一步得同 key；dev.to 2026-08 生产教训）。

语义（write-before-invoke / result-before-advance）：
- begin：执行前记 pending（重复 key → 返回 None = 已记录，调用方跳过执行）
- finish：执行后记 done/failed + 结果摘要（result_ref）
- should_skip：命中已有记录（pending/done/failed/unknown 均算）→ 不重放
  （pending=崩溃于执行中：宁可跳过也不双写，人工裁定；unknown 同理）

Store 缝：测试=MemoryLedgerStore（测试文件内）；生产=SqlLedgerStore。
"""
from typing import Any, Optional, Protocol

from app.services.durable_types import make_key

__all__ = ["make_key", "begin", "finish", "should_skip", "get_result", "get_ledger_store"]


class LedgerStore(Protocol):
    async def get(self, key: str) -> Optional[dict]: ...
    async def insert(self, record: dict) -> bool: ...
    async def update(self, key: str, status: str, result_ref: Optional[str] = None) -> None: ...


async def begin(store: LedgerStore, principal_type: str, principal_id: str,
                cursor: int, tool_name: str, call_id: str) -> Optional[str]:
    """写前登记 pending；重复返回 None（=已记录 → 调用方跳过执行）。

    既有记录为 failed 时允许重试（原子失败=外部效果未完成，
    可安全重放）——把行改回 pending 并返回 key；done/pending/unknown 不重放。
    """
    key = make_key(principal_type, principal_id, int(cursor), tool_name, call_id)
    ok = await store.insert({
        "idempotency_key": key,
        "principal_type": principal_type,
        "principal_id": str(principal_id),
        "cursor": int(cursor),
        "tool_name": tool_name,
        "status": "pending",
        "result_ref": None,
    })
    if ok:
        return key
    row = await store.get(key)
    if row is not None and row.get("status") == "failed":
        fn = getattr(store, "update_if", None)
        if fn is None:
            await store.update(key, "pending", None)
            return key
        # 原子 failed→pending（条件更新）——并发下不覆盖刚写入的 done
        return key if await fn(key, "failed", "pending") else None
    return None


async def finish(store: LedgerStore, key: str, status: str, result_ref: Optional[str] = None) -> None:
    """写后结算（done/failed/unknown）+ 结果摘要。"""
    if not key:
        return
    ref = None
    if result_ref is not None:
        ref = str(result_ref)
        if len(ref) > 4000:
            ref = ref[:4000] + "…[truncated]"
    await store.update(key, status if status in ("done", "failed", "unknown") else "failed", ref)


async def should_skip(store: LedgerStore, principal_type: str, principal_id: str,
                      cursor: int, tool_name: str, call_id: str) -> bool:
    key = make_key(principal_type, principal_id, int(cursor), tool_name, call_id)
    row = await store.get(key)
    if row is None:
        return False
    # failed 允许重试（不得把失败的 attempt 当成功吞掉）；
    # pending/done/unknown 一律不重放（write-before-invoke：宁可不双写）。
    return row.get("status") != "failed"


async def get_result(store: LedgerStore, principal_type: str, principal_id: str,
                     cursor: int, tool_name: str, call_id: str) -> Optional[str]:
    key = make_key(principal_type, principal_id, int(cursor), tool_name, call_id)
    row = await store.get(key)
    return (row or {}).get("result_ref")


class SqlLedgerStore:
    """生产存储：side_effect_ledger 表。"""

    async def get(self, key):
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal, SideEffectLedger
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SideEffectLedger).where(SideEffectLedger.idempotency_key == key))
            row = result.scalars().first()
            if row is None:
                return None
            return {"idempotency_key": row.idempotency_key, "status": row.status,
                    "result_ref": row.result_ref, "cursor": row.cursor, "tool_name": row.tool_name}

    async def insert(self, record):
        from sqlalchemy.exc import IntegrityError
        from app.db.database import AsyncSessionLocal, SideEffectLedger
        async with AsyncSessionLocal() as db:
            db.add(SideEffectLedger(**record))
            try:
                await db.commit()
                return True
            except IntegrityError:
                await db.rollback()
                return False

    async def update(self, key, status, result_ref=None):
        from sqlalchemy import update as sa_update
        from app.db.database import AsyncSessionLocal, SideEffectLedger
        async with AsyncSessionLocal() as db:
            await db.execute(
                sa_update(SideEffectLedger)
                .where(SideEffectLedger.idempotency_key == key)
                .values(status=status, result_ref=result_ref))
            await db.commit()

    async def update_if(self, key, expected_status, status):
        """条件更新（CAS）——status==expected 才迁移；返回是否生效。"""
        from sqlalchemy import update as sa_update
        from app.db.database import AsyncSessionLocal, SideEffectLedger
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sa_update(SideEffectLedger)
                .where(SideEffectLedger.idempotency_key == key,
                       SideEffectLedger.status == expected_status)
                .values(status=status, result_ref=None))
            await db.commit()
            return bool(result.rowcount)


_store_singleton: Optional[LedgerStore] = None


def get_ledger_store() -> LedgerStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = SqlLedgerStore()
    return _store_singleton
