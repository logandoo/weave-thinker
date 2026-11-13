# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W5-3 保留/淘汰（记忆/长程 v2，2026-10-23，判据 #24，D5=a 用户决策）。

容量 + 用户可见预览；默认 **shadow 只记录不删**。淘汰 = 归档降级
（fidelity_state: full → archived，P2 降级 > 删除），可一键恢复（撤销）。
"""
import json
import logging
from datetime import datetime
from math import exp
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CAP_DEFAULT = 500


def retention_score_for(weight, stability, recurrence_count,
                        last_recurrence_at, created_at, now=None) -> float:
    """与 scripts/backfill_retention_score.plan_backfill 同一公式（口径一致）：
    0.5*weight + 0.3*exp(-age/stability) + 0.2*min(rec/10, 1)。"""
    now = now or datetime.utcnow()
    w = min(max(float(weight or 0.5), 0.0), 1.0)
    ref = last_recurrence_at or created_at
    age = max((now - ref).total_seconds() / 86400.0, 0.0) if ref else 0.0
    stab = max(float(stability or 14.0), 1.0)
    recency = exp(-age / stab)
    recurrence = min(float(recurrence_count or 0) / 10.0, 1.0)
    return round(0.5 * w + 0.3 * recency + 0.2 * recurrence, 6)


async def preview_evictions(db: Any, user_id: str,
                            capacity: int = CAP_DEFAULT) -> List[Dict[str, Any]]:
    """超容量淘汰候选——只预览不删（shadow）。排序：retention_score 降序
    （NULLS FIRST 保护未回填行）取尾部 = 最低分先淘汰。"""
    from sqlalchemy import select

    from app.db.database import MemoryConcept

    res = await db.execute(
        select(MemoryConcept).where(
            MemoryConcept.user_id == user_id,
            MemoryConcept.status == "active",
            # J2/B9（2026-11-08）：contradicted 不参与保留排序/淘汰候选。
            (MemoryConcept.verification_state.is_(None))
            | (MemoryConcept.verification_state != "contradicted"),
        ).order_by(
            # 评审修复：显式 NULLS FIRST——未回填 retention_score 的 legacy 行
            # 不得因 NULL 排序方言差异被当成最低分先淘汰（保护方向必须写死）。
            MemoryConcept.retention_score.desc().nulls_first(),
            MemoryConcept.weight.desc().nulls_first()))
    rows = list(res.scalars().all())
    # 评审 A C3：按分数降序取尾部 = 最低分先淘汰（原升序取尾部=反向误删高价值）
    over = rows[int(capacity):] if len(rows) > int(capacity) else []
    return [{
        "id": c.id,
        "canonical_name": c.canonical_name,
        "retention_score": float(c.retention_score or 0.0),
        "fidelity_state": c.fidelity_state or "full",
        "action": "archive",
    } for c in over]


async def apply_eviction(db: Any, user_id: str, capacity: int = CAP_DEFAULT,
                         *, mode: str = "shadow") -> Dict[str, Any]:
    """shadow（默认）：只记录不删（memory_eval_runs suite='retention_shadow'）；
    enforce：超容候选 fidelity_state='archived'（可 undo 恢复 full）。"""
    from app.db.database import MemoryConcept, MemoryEvalRun

    candidates = await preview_evictions(db, user_id, capacity)
    db.add(MemoryEvalRun(
        run_id=f"retention-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        suite="retention_shadow",
        arm=str(mode or "shadow"),
        user_id=user_id,
        metrics_json={"would_evict": len(candidates), "user_id": user_id},
    ))
    if str(mode).strip().lower() != "enforce":
        return {"applied": False, "mode": str(mode or "shadow"),
                "candidates": len(candidates)}
    from sqlalchemy import select
    for c in candidates:
        row = await db.get(MemoryConcept, c["id"])
        if row is not None and row.user_id == user_id:
            # 评审修复：J1 要求归档态不得显示验证态，但 undo 必须可恢复——
            # 抹除前把验证四字段快照进 metadata_json，undo_eviction 还原。
            snap = {
                "verification_state": row.verification_state,
                "last_verified_at": (row.last_verified_at.isoformat()
                                     if row.last_verified_at else None),
                "verification_evidence": row.verification_evidence,
                "verification_correction": getattr(row, "verification_correction", None),
            }
            if any(v is not None for v in snap.values()):
                try:
                    meta = json.loads(row.metadata_json) if row.metadata_json else {}
                except (TypeError, ValueError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta["retention_undo_snapshot"] = snap
                row.metadata_json = json.dumps(meta, ensure_ascii=False)
            row.fidelity_state = "archived"
            # J1：归档/淘汰后不得同时显示“已归档 + 已验证/待核实”。
            row.verification_state = None
            row.last_verified_at = None
            row.verification_evidence = None
            if hasattr(row, "verification_correction"):
                row.verification_correction = None
    await db.flush()
    return {"applied": True, "mode": "enforce", "candidates": len(candidates)}


async def retention_preview_report(db: Any, user_id: str,
                                   capacity: int = CAP_DEFAULT) -> Dict[str, Any]:
    from sqlalchemy import func, select

    from app.db.database import MemoryConcept

    active = int(await db.scalar(
        select(func.count()).select_from(MemoryConcept).where(
            MemoryConcept.user_id == user_id,
            MemoryConcept.status == "active",
            (MemoryConcept.verification_state.is_(None))
            | (MemoryConcept.verification_state != "contradicted"))) or 0)
    contradicted = int(await db.scalar(
        select(func.count()).select_from(MemoryConcept).where(
            MemoryConcept.user_id == user_id,
            MemoryConcept.status == "active",
            MemoryConcept.verification_state == "contradicted")) or 0)
    candidates = await preview_evictions(db, user_id, capacity)
    return {"capacity": int(capacity), "active": active,
            "contradicted_excluded": contradicted,
            "mode": "shadow", "candidates": candidates}


async def undo_eviction(db: Any, user_id: str, concept_ids: List[str]) -> int:
    """撤销淘汰：fidelity_state 恢复 full（P2 可逆优先）+ 还原归档时快照的验证态。"""
    from sqlalchemy import select

    from app.db.database import MemoryConcept

    n = 0
    for cid in list(concept_ids or []):
        row = await db.get(MemoryConcept, str(cid))
        if row is not None and row.user_id == user_id and row.fidelity_state != "full":
            row.fidelity_state = "full"
            # 评审修复：enforce 归档抹掉的验证四字段（J1 显示约束）从快照恢复。
            try:
                meta = json.loads(row.metadata_json) if row.metadata_json else {}
            except (TypeError, ValueError):
                meta = {}
            snap = meta.pop("retention_undo_snapshot", None) if isinstance(meta, dict) else None
            if isinstance(snap, dict) and row.verification_state is not None:
                # 评审复审钉：守卫跳过还原时必须把快照放回——否则快照被弹出
                # 却不生效，元数据里既没了句柄也丢了可恢复性。
                meta["retention_undo_snapshot"] = snap
                row.metadata_json = json.dumps(meta, ensure_ascii=False)
                snap = None
            if isinstance(snap, dict):
                # 评审修复：仅当归档期间验证态未被新写入（仍为空）才还原——
                # 若归档后又有新的 verify 结果，不得用陈旧快照覆盖新态。
                row.verification_state = snap.get("verification_state")
                lva = snap.get("last_verified_at")
                try:
                    row.last_verified_at = datetime.fromisoformat(lva) if lva else None
                except (TypeError, ValueError):
                    row.last_verified_at = None
                row.verification_evidence = snap.get("verification_evidence")
                if hasattr(row, "verification_correction"):
                    row.verification_correction = snap.get("verification_correction")
                row.metadata_json = json.dumps(meta, ensure_ascii=False)
            n += 1
    await db.flush()
    return n
