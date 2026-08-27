# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db
from app.db.database import User, MemoryConcept

router = APIRouter(prefix="/api/memory", tags=["memory"])
logger = logging.getLogger(__name__)


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """§8.5.1：admin 端点鉴权（fail-closed）。users.role == 'admin' 才放行。"""
    if getattr(current_user, "role", "user") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return current_user


@router.get("/concepts")
async def list_concepts(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("""
            SELECT id, canonical_name, description_short, description_full,
                   weight, importance, source_trust, memory_type, activation_strength,
                   status, valid_from, valid_to, created_at
            FROM memory_concepts
            WHERE user_id = :uid
            ORDER BY importance DESC, weight DESC, created_at DESC LIMIT :lim
        """),
        {"uid": current_user.id, "lim": max(1, min(limit, 200))},
    )
    concepts = []
    for row in result.fetchall():
        concepts.append({
            "id": row[0],
            "canonical_name": row[1],
            "description_short": row[2],
            "description_full": row[3],
            "weight": row[4],
            "importance": row[5],
            "source_trust": row[6],
            "memory_type": row[7],
            "activation_strength": row[8],
            "status": row[9],
            "valid_from": str(row[10]) if row[10] else None,
            "valid_to": str(row[11]) if row[11] else None,
            "created_at": str(row[12]) if row[12] else None,
        })
    return {"concepts": concepts, "count": len(concepts)}


@router.get("/dreams")
async def list_dreams(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("""
            SELECT d.id, d.generated_for_date, d.summary, d.source_concept_count,
                   d.source_cluster_count, d.dream_type, d.created_at
            FROM agent_dreams d
            JOIN user_agent_states s ON d.agent_state_id = s.id
            WHERE s.user_id = :uid
            ORDER BY d.created_at DESC NULLS LAST, d.generated_for_date DESC LIMIT :lim
        """),
        {"uid": current_user.id, "lim": max(1, min(limit, 50))},
    )
    dreams = []
    for row in result.fetchall():
        dreams.append({
            "id": row[0],
            "generated_for_date": row[1],
            "summary": row[2],
            "source_concept_count": row[3],
            "source_cluster_count": row[4],
            "dream_type": row[5],
            "created_at": str(row[6]) if row[6] else None,
        })
    return {"dreams": dreams, "count": len(dreams)}


@router.delete("/concepts/{concept_id}")
async def delete_concept(
    concept_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("DELETE FROM memory_concepts WHERE id = :id AND user_id = :uid RETURNING id"),
        {"id": concept_id, "uid": current_user.id},
    )
    deleted = result.fetchone()
    if not deleted:
        raise HTTPException(status_code=404, detail="Concept not found")
    await db.execute(
        text("DELETE FROM concept_cluster_members WHERE concept_id = :id"),
        {"id": concept_id},
    )
    await db.execute(
        text("DELETE FROM concept_relations WHERE source_id = :id OR target_id = :id"),
        {"id": concept_id},
    )
    await db.commit()
    return {"deleted": concept_id}


@router.delete("/all")
async def delete_all_memory(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """§10.4 全量擦除（GDPR Art. 17）：新概念层 + 文件层 + 旧 agent_memories；保留 raw 对话与笔记。"""
    uid = current_user.id
    await db.execute(
        text("DELETE FROM concept_relations WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM concept_cluster_members WHERE concept_id IN (SELECT id FROM memory_concepts WHERE user_id = :uid)"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM memory_concepts WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM memory_clusters WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM subconscious_log WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM memory_episodes WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM memory_clarifications WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM memory_llm_calls WHERE user_id = :uid"),
        {"uid": uid},
    )
    # 旧扁平记忆条目（agent_memories）
    await db.execute(
        text("DELETE FROM agent_memories WHERE agent_state_id IN (SELECT id FROM user_agent_states WHERE user_id = :uid)"),
        {"uid": uid},
    )
    # 共享摘要与 dream（含 legacy）
    await db.execute(
        text("DELETE FROM agent_dreams WHERE agent_state_id IN (SELECT id FROM user_agent_states WHERE user_id = :uid)"),
        {"uid": uid},
    )
    await db.execute(
        text("UPDATE user_agent_states SET memory_summary = NULL, dream_summary = NULL, latest_dream_id = NULL, total_concept_count = 0, total_episode_count = 0 WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.commit()

    # 文件层记忆（AGENT.md / USER.md）
    import asyncio as _asyncio
    import shutil as _shutil
    from app.tools.memory import _get_memory_dir

    def _remove_file_layer() -> None:
        user_dir = _get_memory_dir() / str(uid)
        if user_dir.exists():
            _shutil.rmtree(user_dir, ignore_errors=True)

    await _asyncio.to_thread(_remove_file_layer)

    # 进程内缓存同步失效（GDPR 擦除彻底性：BM25 文档、会话缓存、复现滑窗）
    try:
        from app.services import memory_bm25 as _bm
        for idx_map in (_bm._name_indexes, _bm._desc_indexes, _bm._epi_indexes, _bm._sub_indexes):
            idx_map.pop(str(uid), None)
    except Exception:
        pass
    try:
        from app.services import memory_retrieval_service as _rs
        for key in [k for k in _rs._session_cache if k.startswith(f"{uid}:")]:
            _rs._session_cache.pop(key, None)
        for window in (_rs._concept_window, _rs._episodic_window, _rs._subconscious_window):
            window.pop(str(uid), None)
    except Exception:
        pass
    return {"deleted": "all"}


@router.get("/clarifications")
async def list_clarifications(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """§10.4：澄清记录列表（revert 端点的前置——用户需能看到已应用澄清的 ID）。"""
    result = await db.execute(
        text("""
            SELECT id, original_text, correction_type, affected_concept_ids,
                   new_description, confidence, applied, applied_at, created_at
            FROM memory_clarifications
            WHERE user_id = :uid
            ORDER BY created_at DESC LIMIT :lim
        """),
        {"uid": current_user.id, "lim": max(1, min(limit, 200))},
    )
    clarifications = []
    for row in result.fetchall():
        clarifications.append({
            "id": row[0],
            "original_text": row[1],
            "correction_type": row[2],
            "affected_concept_ids": row[3],
            "new_description": row[4],
            "confidence": row[5],
            "applied": bool(row[6]),
            "applied_at": str(row[7]) if row[7] else None,
            "created_at": str(row[8]) if row[8] else None,
        })
    return {"clarifications": clarifications, "count": len(clarifications)}


@router.post("/clarifications/{clarification_id}/revert")
async def revert_clarification_endpoint(
    clarification_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """§9.6/§10.4：撤销已应用的澄清（negate 恢复有效；refine/add_constraint 回滚旧版本）。"""
    from app.services.memory_clarification_service import revert_clarification
    ok = await revert_clarification(db, current_user.id, clarification_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Clarification not found, not applied, or irreversible (forget)")
    await db.commit()
    return {"reverted": clarification_id}


@router.put("/{user_id}/cost_governance/reset")
async def reset_cost_governance(
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """§9.10：admin 手动 reset 用户降级状态（本人可 reset 自己）。"""
    if current_user.id != user_id and getattr(current_user, "role", "user") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    from app.services.memory_cost_governance_service import reset_user_degrade
    await reset_user_degrade(user_id, db)
    await db.commit()
    return {"reset": user_id}


@router.get("/cost_governance/status")
async def get_cost_governance_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """§9.10：用户在设置页查看自己的降级状态与触发原因。"""
    from app.services.memory_cost_governance_service import get_user_degrade_status
    return await get_user_degrade_status(db, current_user.id)


# ---------- §8.5 迁移管理端点（admin 段） ----------

admin_router = APIRouter(prefix="/api/admin/memory", tags=["memory-admin"])


@admin_router.post("/migration/run")
async def admin_migration_run(
    body: dict | None = None,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """§8.5.1：手动触发迁移。user_id 缺省全量排队；dry_run=true 只读统计。"""
    from app.services import memory_migration_service as mig
    body = body or {}
    user_id = body.get("user_id")
    dry_run = bool(body.get("dry_run"))

    if dry_run:
        if user_id:
            return {"dry_run": [await mig.migrate_user_dry_run(db, user_id)]}
        result = await db.execute(text("SELECT user_id FROM user_agent_states"))
        stats = []
        for (uid,) in result.fetchall():
            stats.append(await mig.migrate_user_dry_run(db, uid))
        return {"dry_run": stats}

    if user_id:
        progress = (body.get("reset_attempts") and True) or False
        if progress:
            meta, prog = await mig._load_progress(db, user_id)
            prog["attempts"] = 0
            prog["status"] = "pending"
            prog["next_retry_at"] = None
            await mig._save_progress(db, user_id, meta, prog)
            await db.commit()
        status = await mig.migrate_user(user_id)
        return {"user_id": user_id, "status": status}

    result = await mig.enqueue_pending_migrations()
    return result


@admin_router.post("/migration/rollback")
async def admin_migration_rollback(
    body: dict,
    current_user: User = Depends(require_admin),
):
    """§8.5.5：单用户回滚。"""
    user_id = (body or {}).get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    from app.services import memory_migration_service as mig
    return await mig.rollback_user(user_id)


@admin_router.get("/migration/status")
async def admin_migration_status(
    user_id: str | None = None,
    current_user: User = Depends(require_admin),
):
    """§8.5.1：各用户迁移进度（读取 metadata_json.migration）。"""
    from app.services import memory_migration_service as mig
    return {"users": await mig.get_migration_status(user_id)}


@router.post("/concepts/{concept_id}/forget")
async def forget_concept(
    concept_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("UPDATE memory_concepts SET valid_to = NOW(), weight = 0, status = 'forgotten', updated_at = NOW() WHERE id = :id AND user_id = :uid RETURNING id"),
        {"id": concept_id, "uid": current_user.id},
    )
    updated = result.fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="Concept not found")
    await db.commit()
    return {"forgotten": concept_id}
