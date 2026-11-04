# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""B-8（D-15 落实，2026-10-14 遗留清偿波）：死亡模式数据列写-through。

运行面 conv 在 task_db.close() 后 detached，落库历史上只靠每-verdict 的
merge 通道——merge 异常/stop/断线/进程中断即丢步骤状态（诊断机制 B 残留）。
本模块提供 fire-and-forget 的**目标列 UPDATE 写-through**：只写数据列
（plan/version/events/settled/reflections/evidence_units），绝不触碰控制字段
（status/human_gate/pause_state 等——用户意图面，由 merge 冻结语义管辖）。

fix-round I1（2026-10-14）：
- per-conv FIFO 任务链（dict conv_id→尾任务）——同 conv 写-through 严格串行，
  后写不得乱序抢先；
- **执行期取值**——任务闭包持 conv 引用，执行时读列值（天然取最新；排队时的
  旧快照不得回灌 merge 之后的新状态）；
- 冻结白名单 8 列（Security Minor）：白名单外 raise ValueError，控制字段永
  不写。

非 ORM 对象（单测 SimpleNamespace 伪会话）跳过；失败静默（merge 通道仍在）。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import inspect as sa_inspect, select

logger = logging.getLogger(__name__)

# I1 冻结白名单（Security Minor）：只有数据列可写-through。
# 控制字段（deathmatch_status/human_gate/pause_state/…）由 merge 冻结语义
# 管辖——任何白名单外列在入口就 raise，绝不落到 UPDATE。
WRITE_THROUGH_COLUMNS = frozenset({
    "deathmatch_plan",
    "deathmatch_plan_version",
    "deathmatch_events",
    "deathmatch_settled_ledger",
    "deathmatch_reflections",
    "deathmatch_evidence_units",
    # R16: contract data committed with fresh-goal replan in one UPDATE.
    "deathmatch_acceptance_criteria",
    "deathmatch_grilling_qa_history",
})

_pending: set = set()
# I1：per-conv FIFO 任务链（conv_id → 尾任务）。
_tails: Dict[str, asyncio.Task] = {}

_GOAL_GUARDED_COLUMNS = frozenset({
    "deathmatch_plan",
    "deathmatch_acceptance_criteria",
    "deathmatch_grilling_qa_history",
    "deathmatch_settled_ledger",
})
_CONTRACT_FIELDS = (
    "deathmatch_goal",
    "deathmatch_plan_version",
    "deathmatch_plan",
    "deathmatch_acceptance_criteria",
    "deathmatch_grilling_qa_history",
    "deathmatch_settled_ledger",
)


def _contract_snapshot(conv: Any) -> Dict[str, Any]:
    """Deep detached-safe snapshot used as the optimistic-write baseline."""
    return {
        field: copy.deepcopy(conv.__dict__.get(field))
        for field in _CONTRACT_FIELDS
    }


def _snapshot_matches(row: Any, expected: Dict[str, Any]) -> bool:
    return all(
        getattr(row, field, None) == expected.get(field)
        for field in _CONTRACT_FIELDS
    )


async def _await_shared_task(task: asyncio.Task) -> None:
    """Wait without propagating waiter cancellation into shared persistence.

    A predecessor cancelled independently is treated as a failed chain link and
    does not block its successor. Cancellation of the current waiter still
    propagates to that waiter, while ``shield`` keeps the shared task running.
    """
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        # The shared task itself was cancelled; the caller may continue.
    except Exception:
        # Write-through is fail-open; a failed predecessor must not block the
        # remaining FIFO candidates.
        pass


async def _write_columns(
    conv_id: str,
    columns: Dict[str, Any],
    *,
    expected_contract: Optional[Dict[str, Any]] = None,
    **_: Any,
) -> bool:
    """Compare-and-swap a complete contract under a database row lock."""
    try:
        from app.db.database import AsyncSessionLocal, Conversation

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Conversation)
                .where(Conversation.id == conv_id)
                .with_for_update()
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            if expected_contract is not None and not _snapshot_matches(row, expected_contract):
                # Any changed goal/version/plan/criteria/QA/settled component
                # means another writer advanced the contract. This stale writer
                # must not restore any part of its older candidate.
                return False
            if "deathmatch_grilling_qa_history" in columns:
                qa = columns.get("deathmatch_grilling_qa_history") or []
                pending = [
                    entry.get("goal_contract_pending")
                    for entry in qa if isinstance(entry, dict)
                    and "goal_contract_pending" in entry
                ]
                plan = columns.get("deathmatch_plan") or {}
                logger.info(
                    "deathmatch QA write-through conv=%s version=%s pending=%s steps=%s qa_len=%s",
                    conv_id, columns.get("deathmatch_plan_version"), pending,
                    [step.get("status") for step in (plan.get("steps") or [])],
                    len(qa),
                )
            for key, value in columns.items():
                setattr(row, key, value)
            await db.commit()
            return True
    except Exception as exc:
        logger.debug("deathmatch write-through failed (merge channel remains): %s", exc)
    return False


async def _write_columns_chained(
    prev: Optional[asyncio.Task], conv: Any, conv_id: str, keys: Tuple[str, ...],
    snapshot: Dict[str, Any], expected_contract: Optional[Dict[str, Any]],
) -> None:
    """I1：FIFO 链节——先等前序写完成，再**执行期取值**（读 conv 当前列值，
    天然取最新）。属性不可读（detached 过期等罕见边角）才回落调用期快照。"""
    if prev is not None:
        await _await_shared_task(prev)
    if getattr(conv, "_dm_write_stale", False):
        return
    values: Dict[str, Any] = {}
    for k in keys:
        try:
            # Freeze the candidate before the first await. Otherwise a later
            # step transition can mutate the same JSON object while this DB
            # write is in flight, and the post-commit baseline would describe
            # a plan/version combination that was never actually committed.
            values[k] = copy.deepcopy(getattr(conv, k))
        except Exception:
            values[k] = copy.deepcopy(snapshot.get(k))
    if expected_contract is not None:
        # A queued successor must use the baseline produced by its predecessor,
        # not the older baseline captured when it was first enqueued.
        expected_contract = getattr(
            conv, "_dm_write_contract_baseline", expected_contract,
        )
    written = await _write_columns(
        conv_id, values, expected_contract=expected_contract,
    )
    if written and expected_contract is not None:
        # Only a successful guarded write advances the baseline. Unguarded
        # event writes and rejected stale writes never advance it.
        try:
            baseline = dict(expected_contract)
            for field in _CONTRACT_FIELDS:
                if field in values:
                    baseline[field] = copy.deepcopy(values[field])
            conv._dm_write_contract_baseline = baseline
        except Exception:
            pass


def write_through_bg(conv: Any, _expected_previous_plan_version: Any = None, **columns: Any) -> None:
    """fire-and-forget 目标列 UPDATE。列名必须在冻结白名单内（否则 ValueError）；
    值在**执行期**从 conv 读取（调用参数只是列名声明+读取失败兜底快照）。

    ``_expected_previous_plan_version`` is accepted for call compatibility but
    intentionally ignored: independently incremented counters are not proof of
    freshness. The complete contract snapshot is the only CAS baseline.
    """
    if "deathmatch_plan" in columns:
        # Every plan write carries the same contract/epoch. A queued older
        # plan/version write can no longer publish them with stale criteria
        # or a pending fresh-goal marker while reading live attributes.
        columns.setdefault(
            "deathmatch_acceptance_criteria",
            conv.__dict__.get("deathmatch_acceptance_criteria"),
        )
        columns.setdefault(
            "deathmatch_grilling_qa_history",
            conv.__dict__.get("deathmatch_grilling_qa_history"),
        )
    unknown = [k for k in columns if k not in WRITE_THROUGH_COLUMNS]
    if unknown:
        raise ValueError(
            f"deathmatch write-through 拒绝写入非白名单列（控制字段永不写）: {unknown}"
        )
    if conv is None or sa_inspect(conv, raiseerr=False) is None:
        return
    if getattr(conv, "_dm_write_stale", False):
        return
    try:
        from app.core.config import get_config

        if not bool(get_config().deathmatch_write_through_enabled):
            return
    except Exception:
        pass
    conv_id = conv.__dict__.get("id")
    if not conv_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    keys = tuple(columns)
    snapshot = {k: copy.deepcopy(v) for k, v in columns.items()}
    guarded = bool(_GOAL_GUARDED_COLUMNS.intersection(columns))
    if guarded:
        expected_contract = getattr(conv, "_dm_write_contract_baseline", None)
        if expected_contract is None:
            expected_contract = _contract_snapshot(conv)
    else:
        expected_contract = None
    prev = _tails.get(conv_id)
    task = loop.create_task(
        _write_columns_chained(
            prev, conv, conv_id, keys, snapshot, expected_contract,
        )
    )
    _tails[conv_id] = task
    _pending.add(task)

    def _done(t: asyncio.Task) -> None:
        _pending.discard(t)
        if _tails.get(conv_id) is t:
            _tails.pop(conv_id, None)

    task.add_done_callback(_done)


async def drain_write_through(conv_id: Optional[str] = None) -> None:
    """等待在飞写-through 完成（测试/优雅停机/verdict CAS 用）。

    指定 ``conv_id`` 时只排空该会话的 FIFO 尾链，避免一个会话的 verdict
    merge 等待无关会话；尾链之后新排入的同会话写也必须一并完成。
    """
    if conv_id is None:
        while _pending:
            # Shield every shared write from cancellation of this waiter.
            await asyncio.gather(
                *(asyncio.shield(task) for task in list(_pending)),
                return_exceptions=True,
            )
        return
    conv_key = str(conv_id)
    while True:
        tail = _tails.get(conv_key)
        if tail is None:
            return
        await _await_shared_task(tail)
        if _tails.get(conv_key) is tail:
            # The done callback normally removes this entry. Keep the scoped
            # drain deterministic even if it has not run yet; a newer tail is
            # never popped because identity is checked without an intervening
            # await.
            _tails.pop(conv_key, None)
