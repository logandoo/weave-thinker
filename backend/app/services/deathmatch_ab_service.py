# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W4-2 四开关 A/B shadow 指标（记忆/长程 v2，2026-10-23，判据 #19）。

四个开关（读 ``config.deathmatch`` dict；memory_injection 的读法同
``app/services/memory_task_context.deathmatch_memory_enabled``）：
- evidence_units     ← deathmatch.evidence_units_enabled
- consensus          ← deathmatch.consensus_challenger_enabled
- reflection_retract ← deathmatch.reflection_retract_enabled
- memory_injection   ← deathmatch.memory_injection_enabled

默认全部 False = shadow/关（P3 默认关）。

指标面（reopen 率/重复工具率/时长/轮次）落 ``memory_eval_runs``
（suite='deathmatch_ab'，arm=开关状态稳定字符串），``summarize_ab`` 按 arm
聚合均值。

**服务只提供数据**：负向结果写回 ``memory/`` ❌（决策记录）是人工流程，
不在本服务内自动执行。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.db.database import MemoryEvalRun

logger = logging.getLogger(__name__)

#: 四开关的固定顺序 → arm 字符串稳定排序（与 dict 插入顺序无关）
SWITCH_KEYS = ("evidence_units", "consensus", "reflection_retract", "memory_injection")

_ARM_PREFIX = {
    "evidence_units": "eu",
    "consensus": "co",
    "reflection_retract": "rr",
    "memory_injection": "mi",
}

#: arm 列宽 String(64)——用短前缀保证不越界（完整开关态存 metrics_json.switch_state）
_ARM_SOURCE_KEYS = {
    "evidence_units": "evidence_units_enabled",
    "consensus": "consensus_challenger_enabled",
    "reflection_retract": "reflection_retract_enabled",
    "memory_injection": "memory_injection_enabled",
}

METRIC_KEYS = ("reopen_rate", "duplicate_tool_rate", "duration_seconds", "turns")


def current_switch_state(config_dict: Optional[dict] = None) -> Dict[str, bool]:
    """读四开关状态；``config_dict`` 缺省 = ``get_config().deathmatch``。

    传入的 dict 既可以是 [deathmatch] 段本身，也可以是含 "deathmatch" 子
    dict 的外层 config dict。缺省/缺键 = False（shadow/关，P3 默认关）。
    """
    src = config_dict
    if src is None:
        try:
            from app.core.config import get_config
            src = get_config().deathmatch or {}
        except Exception:
            logger.debug("switch state config read failed (default off)", exc_info=True)
            src = {}
    if not isinstance(src, dict):
        src = {}
    if isinstance(src.get("deathmatch"), dict):
        src = src["deathmatch"]
    return {k: bool(src.get(_ARM_SOURCE_KEYS[k], False)) for k in SWITCH_KEYS}


def arm_label(switch_state: Optional[dict] = None) -> str:
    """开关状态 → 稳定 arm 字符串（固定键序；≤64 字符）。

    形如 "eu=on,co=off,rr=off,mi=off"。完整开关态由调用方随 metrics_json
    落库（record_ab_metrics 会带上 switch_state）。
    """
    st = switch_state if isinstance(switch_state, dict) else {}
    return ",".join(
        f"{_ARM_PREFIX[k]}={'on' if st.get(k) else 'off'}" for k in SWITCH_KEYS
    )


def _mean(rows: List[dict], key: str) -> float:
    vals = []
    for m in rows:
        try:
            vals.append(float(m.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return (sum(vals) / len(vals)) if vals else 0.0


async def record_ab_metrics(db: Any, conversation_id: str, metrics: dict) -> None:
    """把一次 A/B shadow 采集写入 memory_eval_runs（suite='deathmatch_ab'）。

    ``metrics`` 形如 {"switch_state": {...}, "reopen_rate", "duplicate_tool_rate",
    "duration_seconds", "turns", ...}；switch_state 缺省 = current_switch_state()。
    conversation_id 一并存入 metrics_json（可追溯；arm 只做分组键）。
    本 helper 拥有 commit（同 deathmatch_archive.record_harness_run 惯例）。
    """
    metrics = dict(metrics or {})
    state = metrics.get("switch_state")
    if not isinstance(state, dict):
        state = current_switch_state()
    payload = dict(metrics)
    payload["switch_state"] = state
    payload.setdefault("conversation_id", str(conversation_id))
    row = MemoryEvalRun(
        run_id=f"deathmatch-ab-{uuid.uuid4().hex[:16]}",
        suite="deathmatch_ab",
        arm=arm_label(state),
        metrics_json=payload,
    )
    db.add(row)
    await db.commit()


async def summarize_ab(db: Any, days: int = 14) -> Dict[str, Any]:
    """按 arm 聚合近 N 天 shadow 指标均值（reopen 率/重复工具率/时长/轮次）。

    返回 {"days", "total", "arms": [{"arm", "count", reopen_rate,
    duplicate_tool_rate, duration_seconds, turns}, ...]}，arm 按字典序稳定排序。
    """
    cutoff = datetime.utcnow() - timedelta(days=max(0, int(days)))
    rows = (await db.execute(
        select(MemoryEvalRun)
        .where(MemoryEvalRun.suite == "deathmatch_ab",
               MemoryEvalRun.created_at >= cutoff)
    )).scalars().all()
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        m = r.metrics_json if isinstance(r.metrics_json, dict) else {}
        groups.setdefault(str(r.arm or ""), []).append(m)
    arms: List[Dict[str, Any]] = []
    for arm in sorted(groups):
        ms = groups[arm]
        entry: Dict[str, Any] = {"arm": arm, "count": len(ms)}
        for k in METRIC_KEYS:
            entry[k] = _mean(ms, k)
        arms.append(entry)
    return {"days": int(days), "total": len(rows), "arms": arms}
