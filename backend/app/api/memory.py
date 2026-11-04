# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db
from app.db.database import User, MemoryConcept, MemoryEpisode

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
    concept = await db.get(MemoryConcept, concept_id)
    if not concept or concept.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Concept not found")
    # 同步发射侧契约：ORM 删除触发 after_delete → capture 事件+tombstone
    # （memory_concepts 属同步域；text() 物理删除零捕获是已修缺口）。
    await db.delete(concept)
    # 关联清理（junction 表非同步域，无捕获语义）。
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
    # 同步域实体（memory_concepts/memory_episodes）走 ORM 逐行删除：
    # after_delete → capture 事件+tombstone，客户端镜像擦除（text() 物理删除零捕获为已修缺口）。
    # GDPR 罕见操作，行数=用户全部 concepts/episodes，可接受；先 flush 定界避免 UOW 表序歧义。
    concepts = (await db.execute(
        select(MemoryConcept).where(MemoryConcept.user_id == uid)
    )).scalars().all()
    for obj in concepts:
        await db.delete(obj)
    await db.flush()
    await db.execute(
        text("DELETE FROM memory_clusters WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM subconscious_log WHERE user_id = :uid"),
        {"uid": uid},
    )
    episodes = (await db.execute(
        select(MemoryEpisode).where(MemoryEpisode.user_id == uid)
    )).scalars().all()
    for obj in episodes:
        await db.delete(obj)
    await db.flush()
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
    # P1b/P1d TMA-NM: runtime taint/artifact/token state is user-scoped.
    # Verdict rows are pseudonymous audit (no message content); their
    # retention/redaction policy is P2.
    await db.execute(text("DELETE FROM taint_states WHERE user_id = :uid"), {"uid": uid})
    await db.execute(text("DELETE FROM artifact_taint WHERE user_id = :uid"), {"uid": uid})
    await db.execute(text("DELETE FROM memory_action_tokens WHERE user_id = :uid"), {"uid": uid})
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
    from app.tools.memory import _get_memory_base
    from app.services.tenant_service import scoped_root

    def _remove_file_layer() -> None:
        # Erasure must not be blocked by the tenant/provenance authority guard
        # (that would wipe DB rows and leave the file layer behind), but it is
        # still tenant-scoped so one tenant's erase cannot touch another's.
        user_dir = scoped_root(_get_memory_base()) / str(uid)
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
    try:
        from app.services.taint_service import forget_user
        forget_user(str(uid))
    except Exception:
        pass
    # W5-1（记忆/长程 v2，2026-10-23）擦除面扩展：会话派生物（artifacts/
    # rolling summaries/resume snapshots/evidence units）+ skills + pinned
    # constraints + dream reports——全部按 user_id 作用域（B20 无越权）
    try:
        from app.services.memory_forget_service import erase_extended_surfaces
        erase_counts = await erase_extended_surfaces(db, current_user.id)
        # 评审 A C1：erase 在 get_db 关闭时若不 commit 会被隐式回滚
        # （GDPR 擦除面静默丢失）——显式落库。
        await db.commit()
    except Exception:
        logger.warning("erase_extended_surfaces failed", exc_info=True)
        erase_counts = {}
    return {"deleted": "all", "extended_surfaces": erase_counts}


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
    concept = await db.get(MemoryConcept, concept_id)
    if not concept or concept.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Concept not found")
    # 同步发射侧契约：ORM 变更触发捕获事件（memory_concepts 同步域）；
    # status/weight/valid_to 为真实变更，显式 bump 保 ts 单调。
    concept.valid_to = datetime.utcnow()
    concept.weight = 0
    concept.status = "forgotten"
    concept.updated_at = datetime.utcnow()
    await db.commit()
    return {"forgotten": concept_id}


def _decode_recall_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


@router.get("/recall_log")
async def list_recall_log(
    limit: int = 50,
    before_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """C1：逐轮召回台账（元数据；不存记忆内容）。cursor=(created_at,id)。"""
    limit = max(1, min(int(limit or 50), 200))
    params = {"uid": current_user.id, "limit": limit}
    cursor_clause = ""
    if before_id:
        cursor_clause = (
            "AND (created_at, id) < (SELECT created_at, id FROM memory_recall_log "
            "WHERE id = :before_id AND user_id = :uid)")
        params["before_id"] = before_id
    result = await db.execute(
        text(f"""
            SELECT id, conversation_id, query_hash, candidate_ids, tier_scores,
                   gate_score, budget_chars, injected_chars, truncated,
                   elapsed_ms, cache_hit, created_at
            FROM memory_recall_log
            WHERE user_id = :uid {cursor_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
        """),
        params,
    )
    rows = result.mappings().all()
    total = (await db.execute(
        text("SELECT COUNT(*) FROM memory_recall_log WHERE user_id = :uid"),
        {"uid": current_user.id},
    )).scalar() or 0
    items = [{
        "id": r["id"],
        "conversation_id": r["conversation_id"],
        "query_hash": r["query_hash"],
        "candidate_ids": _decode_recall_json(r["candidate_ids"]),
        "tier_scores": _decode_recall_json(r["tier_scores"]),
        "gate_score": r["gate_score"],
        "budget_chars": r["budget_chars"],
        "injected_chars": r["injected_chars"],
        "truncated": r["truncated"],
        "elapsed_ms": r["elapsed_ms"],
        "cache_hit": r["cache_hit"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]
    return {"items": items, "total": int(total)}


@router.get("/constraints")
async def list_constraints(
    conversation_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W1-2（记忆/长程 v2）：固定约束面板——含已撤销（可审计，P5 用户可见）。"""
    from app.services import memory_constraint_service as mcs

    rows = await mcs.list_all_pins(db, current_user.id, conversation_id or None)
    return {"items": [{
        "id": r.id,
        "conversation_id": r.conversation_id,
        "text": r.text,
        "source_role": r.source_role,
        "provenance": r.provenance,
        "source_message_id": r.source_message_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
    } for r in rows]}


@router.post("/constraints/{pin_id}/revoke")
async def revoke_constraint(
    pin_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W1-2：撤销单条固定约束（立即不再重放；不可恢复性=无，可逆优先 P2）。"""
    from app.services import memory_constraint_service as mcs

    ok = await mcs.revoke_pin(db, current_user.id, pin_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="约束不存在或已撤销")
    await db.commit()
    return {"revoked": pin_id}


@router.post("/forget")
async def forget_memory(
    request: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W0-4（记忆/长程 v2，D1=a memory-only）：遗忘请求 → 清单 + 执行 + 残余报告。

    语义明示：删除记忆层（概念/事件/潜意识/agent 记忆/dream/文件层/约束），
    **保留对话与笔记**（retained_surfaces_declared 列明；如需 full forget 另行申请）。
    请求文本用「」括起要遗忘的词条，或以『删除记忆：』开头。
    """
    from app.services.memory_forget_service import (
        create_deletion_manifest, execute_forget, parse_forget_needles)

    text = str(request.get("text") or "")
    needles = parse_forget_needles(text)
    if not needles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未能解析要遗忘的词条——请用「」括起，或以『删除记忆：』开头")
    man = await create_deletion_manifest(
        db, current_user.id,
        str(request.get("request_id") or "req-" + man_id_hint(request)),
        {"needles": needles})
    await db.commit()
    rep = await execute_forget(db, current_user.id, man.id)
    await db.commit()
    rep["manifest_id"] = man.id
    rep["status"] = man.status
    return rep


def man_id_hint(request: dict) -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex[:8]


@router.get("/skill_candidates")
async def list_skill_candidates(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W4-3（记忆/长程 v2，判据 #20）：程序性记忆候选技能（人工门）。

    从死磕反思确定性拼装候选草稿——**不落库**；approve 通过才写 user_skills。
    返回 {"items": [{id,name,description,content,source_reflections}], "pool": {used,cap}}。
    """
    from app.services import memory_procedural_service as mps

    items = await mps.harvest_skill_candidates(
        db, current_user.id, limit=max(1, min(int(limit or 10), 50)))
    return {"items": items, "pool": await mps.skill_pool_usage(db, current_user.id)}


@router.post("/skill_candidates/{candidate_id}/approve")
async def approve_skill_candidate(
    candidate_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W4-3：人工门通过 → 候选落 user_skills（判据 #20）。

    返回 {"skill_id", "reason"}；reason ∈ created/exists（幂等）。
    409 = 技能池已满（上限 30）；404 = 候选不存在（须来自 harvest）。
    """
    from app.services import memory_procedural_service as mps

    skill, reason = await mps.approve_skill_candidate(
        db, current_user.id, candidate_id)
    if skill is None and reason == "pool_full":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"技能池已满（上限 {mps.SKILL_POOL_CAP}），请先清理后再审批")
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="候选不存在或已过期（候选必须来自 skill_candidates 列表）")
    return {"skill_id": skill.id, "reason": reason}


@router.get("/retention_preview")
async def retention_preview(
    capacity: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W5-3：保留/淘汰预览（shadow 只记录不删；可撤销，D5=a）。"""
    from app.services.memory_retention_service import retention_preview_report

    cap = int(capacity) if capacity and capacity > 0 else 500
    return await retention_preview_report(db, current_user.id, cap)


@router.post("/retention/undo")
async def retention_undo(
    request: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W5-3：撤销淘汰（fidelity_state 恢复 full，P2 可逆优先）。"""
    from app.services.memory_retention_service import undo_eviction

    ids = list(request.get("concept_ids") or [])
    n = await undo_eviction(db, current_user.id, ids)
    await db.commit()
    return {"restored": int(n)}


@router.get("/dream_repair_reports")
async def dream_repair_reports(
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """W3-3：梦境 probe/repair 报告（默认关；14 天无正收益自动停，W3-4）。"""
    from app.services.memory_dream_repair_service import list_repair_reports

    items = await list_repair_reports(db, current_user.id, min(int(limit or 20), 100))
    return {"items": items}
