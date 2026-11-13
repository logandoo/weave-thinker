# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.deps import get_current_user, get_db, require_admin
from app.db.database import (
    User, MemoryConcept, MemoryEpisode, MemoryEvalRun,
)
from app.schemas.memory import (
    MemoryConceptItem,
    MemoryConceptList,
    MemoryEvidence,
    MemoryHealth,
    MemoryPageItem,
    MemoryPageLinkItem,
    MemoryPageList,
    MemoryVerifyRequest,
    MemoryVerifyResponse,
)

router = APIRouter(prefix="/api/memory", tags=["memory"])
logger = logging.getLogger(__name__)

_VERIFY_STATES = {"confirmed", "contradicted", "insufficient"}


def _json_list(raw):
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return [str(x) for x in val] if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []


def _parse_evidence(raw) -> MemoryEvidence | None:
    if not raw:
        return None
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(val, dict):
        return None
    return MemoryEvidence(
        source_ids=[str(x) for x in (val.get("source_ids") or [])][:10],
        unit_ids=[str(x) for x in (val.get("unit_ids") or [])][:10],
        source_kind=(str(val.get("source_kind"))[:20] if val.get("source_kind") else None),
        quote=str(val.get("quote") or "")[:200],
        reason=str(val.get("reason") or "")[:120],
        checked_at=(str(val.get("checked_at"))[:40] if val.get("checked_at") else None),
        manual=(bool(val.get("manual")) if val.get("manual") is not None else None),
    )


def _iso(value):
    return str(value) if value else None


def _concept_item(row) -> MemoryConceptItem:
    r = row._mapping if hasattr(row, "_mapping") else row
    return MemoryConceptItem(
        id=r["id"],
        canonical_name=r["canonical_name"],
        description_short=r["description_short"] or "",
        description_full=r["description_full"],
        weight=float(r["weight"] or 0),
        importance=float(r["importance"] or 0),
        source_trust=r["source_trust"] or "",
        memory_type=r["memory_type"] or "",
        activation_strength=float(r["activation_strength"] or 0),
        status=r["status"] or "",
        valid_from=_iso(r["valid_from"]),
        valid_to=_iso(r["valid_to"]),
        created_at=_iso(r["created_at"]),
        verification_state=r["verification_state"],
        last_verified_at=_iso(r["last_verified_at"]),
        invalid_reason=r["invalid_reason"],
        superseded_by=r["superseded_by"],
        superseded_by_name=r["superseded_by_name"],
        needs_review=bool(r["needs_review"]),
        aliases=_json_list(r["aliases"]),
        recurrence_count=int(r["recurrence_count"] or 0),
        source_type=r["source_type"],
        source_kind=r["source_kind"],
        source_message_ids=_json_list(r["source_message_ids"]),
        evidence=_parse_evidence(r["verification_evidence"]),
        verification_correction=r["verification_correction"],
        is_history=bool(r["valid_to"]),
    )


@router.get("/concepts", response_model=MemoryConceptList)
async def list_concepts(
    limit: int = 50,
    offset: int = 0,
    history_offset: int = 0,
    include_history: bool = False,
    verification_state: str | None = None,
    q: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """概念列表。默认只返回现行（valid_to IS NULL）；历史需显式 include_history。

    每条返回 validity 原因、后继名称、needs_review、验证状态/证据与来源消息
    （A1/A2/B7/C3/E1）。

    分页（评审修复 2026-11-08）：`offset` 只偏移现行段，`history_offset` 只偏移
    历史段——两段共享游标会让合并列表翻页时现行/历史同时跳步产生空洞。
    响应附 active_total/history_total 供前端分别推进游标。
    """
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    hoff = max(0, int(history_offset or 0))
    clauses = ["c.user_id = :uid", "c.valid_to IS NULL"]
    params: dict = {"uid": current_user.id, "lim": lim, "off": off, "hoff": hoff}
    vs = (verification_state or "").strip().lower()
    if vs and vs not in ("any", "all"):
        if vs == "unverified":
            clauses.append("c.verification_state IS NULL")
        elif vs in _VERIFY_STATES:
            clauses.append("c.verification_state = :vs")
            params["vs"] = vs
    if q and q.strip():
        clauses.append("(c.canonical_name ILIKE :q OR c.description_full ILIKE :q "
                       "OR c.aliases ILIKE :q)")
        params["q"] = f"%{q.strip()[:100]}%"
    where = " AND ".join(clauses)
    total_where = " AND ".join(c for c in clauses if c != "c.valid_to IS NULL")
    # c.id 决胜（评审修复）：importance/weight/created_at 全同的批量数据下
    # 无决胜列时 PG 行序不稳定，offset 翻页可能重复/漏行。
    order_clause = "c.importance DESC, c.weight DESC, c.created_at DESC, c.id"
    rows = (await db.execute(
        text(f"""
            SELECT c.id, c.canonical_name, c.description_short, c.description_full,
                   c.weight, c.importance, c.source_trust, c.memory_type,
                   c.activation_strength, c.status, c.valid_from, c.valid_to,
                   c.created_at, c.verification_state, c.last_verified_at,
                   c.invalid_reason, c.superseded_by,
                   s.canonical_name AS superseded_by_name, c.needs_review,
                   c.aliases, c.recurrence_count, c.source_type, c.source_kind,
                   c.source_message_ids, c.verification_evidence,
                   c.verification_correction
            FROM memory_concepts c
            LEFT JOIN memory_concepts s
              ON s.id = c.superseded_by AND s.user_id = c.user_id
            WHERE {where}
            ORDER BY {order_clause}
            LIMIT :lim OFFSET :off
        """),
        params,
    )).fetchall()
    if include_history:
        # A2/E1：现行段与历史段分开取，避免 700+ 现行概念把历史挤出 limit；
        # 历史只在显式 include_history 时追加（最多再一页）。
        history_rows = (await db.execute(
            text(f"""
                SELECT c.id, c.canonical_name, c.description_short, c.description_full,
                       c.weight, c.importance, c.source_trust, c.memory_type,
                       c.activation_strength, c.status, c.valid_from, c.valid_to,
                       c.created_at, c.verification_state, c.last_verified_at,
                       c.invalid_reason, c.superseded_by,
                       s.canonical_name AS superseded_by_name, c.needs_review,
                       c.aliases, c.recurrence_count, c.source_type, c.source_kind,
                       c.source_message_ids, c.verification_evidence,
                       c.verification_correction
                FROM memory_concepts c
                LEFT JOIN memory_concepts s
                  ON s.id = c.superseded_by AND s.user_id = c.user_id
                WHERE {total_where} AND c.valid_to IS NOT NULL
                ORDER BY c.importance DESC, c.weight DESC, c.created_at DESC, c.id
                LIMIT :lim OFFSET :hoff
            """),
            params,
        )).fetchall()
        seen = {r[0] for r in rows}
        rows = list(rows) + [r for r in history_rows if r[0] not in seen]
    # 分段 total：前端按「已加载的本类条数」分别推进 offset / history_offset。
    active_total = int((await db.execute(
        text(f"SELECT COUNT(*) FROM memory_concepts c WHERE {where}"),
        {k: v for k, v in params.items() if k in ("uid", "vs", "q")},
    )).scalar() or 0)
    history_total = 0
    if include_history:
        history_total = int((await db.execute(
            text(f"SELECT COUNT(*) FROM memory_concepts c "
                 f"WHERE {total_where} AND c.valid_to IS NOT NULL"),
            {k: v for k, v in params.items() if k in ("uid", "vs", "q")},
        )).scalar() or 0)
    total = active_total + history_total
    items = [_concept_item(r) for r in rows]
    return MemoryConceptList(
        concepts=items, count=len(items), total=total,
        active_total=active_total, history_total=history_total,
        include_history=bool(include_history))


@router.post("/concepts/{concept_id}/verify", response_model=MemoryVerifyResponse)
async def verify_concept(
    concept_id: str,
    body: MemoryVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用户手动确认/否认/重验/应用修正草案（B8/E5）。

    与 valid_to 语义分离：reject 只标 contradicted（待核实），不写失效。
    """
    concept = await db.get(MemoryConcept, concept_id)
    if not concept or concept.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Concept not found")
    action = body.action
    now = datetime.utcnow()
    applied = False
    # G5 评审修复：记录操作前的验证态——false_contradiction_rate 只数
    # 「把 contradicted 确认回来」的 confirm（对 insufficient/未验证的确认不是误判翻案）。
    prev_state = concept.verification_state
    if action == "confirm":
        concept.verification_state = "confirmed"
        concept.last_verified_at = now
        concept.needs_review = False
        evidence = _parse_evidence(concept.verification_evidence) or MemoryEvidence()
        concept.verification_evidence = json.dumps({
            "source_ids": evidence.source_ids,
            "unit_ids": evidence.unit_ids,
            "source_kind": evidence.source_kind,
            "quote": evidence.quote,
            "reason": (body.note or "user_confirmed")[:120],
            "checked_at": now.isoformat(),
            "manual": True,
        }, ensure_ascii=False)
    elif action == "reject":
        concept.verification_state = "contradicted"
        concept.last_verified_at = now
        # 用户否认 ≠ 来源丢失：不进入 needs_review 的 nightly source_lost 链。
        concept.needs_review = False
        evidence = _parse_evidence(concept.verification_evidence) or MemoryEvidence()
        concept.verification_evidence = json.dumps({
            "source_ids": evidence.source_ids,
            "unit_ids": evidence.unit_ids,
            "source_kind": evidence.source_kind,
            "quote": evidence.quote,
            "reason": (body.note or "user_rejected")[:120],
            "checked_at": now.isoformat(),
            "manual": True,
        }, ensure_ascii=False)
    elif action == "retry":
        concept.verification_state = None
        concept.last_verified_at = None
        concept.verification_evidence = None
        concept.verification_correction = None
    else:  # apply_correction
        correction = (concept.verification_correction or "").strip()
        if not correction:
            raise HTTPException(status_code=400, detail="No correction proposal to apply")
        try:
            _meta = json.loads(concept.metadata_json) if concept.metadata_json else {}
        except (TypeError, ValueError):
            _meta = {}
        if not isinstance(_meta, dict):
            _meta = {}
        concept.metadata_json = json.dumps({
            **_meta,
            "audit_old_description": concept.description_full,
            "audit_old_description_short": concept.description_short,
            "audit_correction_source": "dream_verify",
        }, ensure_ascii=False)
        from app.services.memory_concept_service import update_concept_description
        await update_concept_description(db, concept_id, correction[:80], correction[:5000])
        concept.verification_correction = None
        concept.needs_review = False
        applied = True
    concept.updated_at = now
    audit = MemoryEvalRun(
        run_id=f"manual-{now.strftime('%Y%m%d%H%M%S')}",
        suite="manual_verify",
        arm=action,
        user_id=current_user.id,
        metrics_json={"concept_id": concept_id, "note": (body.note or "")[:200],
                      "prev_state": prev_state or "unverified"},
    )
    db.add(audit)
    await db.commit()
    await db.refresh(concept)
    return MemoryVerifyResponse(
        id=concept_id, action=action,
        verification_state=concept.verification_state,
        description_short=concept.description_short,
        description_full=concept.description_full,
        applied=applied, audit_id=audit.id,
    )


@router.get("/pages", response_model=MemoryPageList)
async def list_pages(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """C4：实体页索引（短 wiki）；详情用 GET /pages/{id} 展开。"""
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    rows = (await db.execute(
        text("""SELECT id, entity_key, title, aliases, summary, concept_ids,
                       importance, last_built_at
                FROM memory_pages WHERE user_id = :uid
                ORDER BY importance DESC, title ASC LIMIT :lim OFFSET :off"""),
        {"uid": current_user.id, "lim": lim, "off": off},
    )).fetchall()
    links = await _links_for_pages(db, current_user.id, [r[0] for r in rows])
    items = [_page_item(r, links.get(r[0], [])) for r in rows]
    return MemoryPageList(pages=items, count=len(items))


async def _links_for_pages(db: AsyncSession, user_id: str, page_ids: list[str]) -> dict:
    if not page_ids:
        return {}
    rows = (await db.execute(
        text("""SELECT l.source_page_id, l.target_page_id, t.title,
                       l.relation_type, l.weight
                FROM memory_page_links l
                LEFT JOIN memory_pages t ON t.id = l.target_page_id AND t.user_id = l.user_id
                WHERE l.user_id = :uid AND l.source_page_id = ANY(:ids)"""),
        {"uid": user_id, "ids": page_ids},
    )).fetchall()
    out: dict = {}
    for src, tid, title, rtype, weight in rows:
        out.setdefault(src, []).append(MemoryPageLinkItem(
            target_page_id=tid, target_title=title,
            relation_type=rtype or "related", weight=float(weight or 0.5)))
    return out


def _page_item(row, links: list) -> MemoryPageItem:
    r = row._mapping if hasattr(row, "_mapping") else row
    return MemoryPageItem(
        id=r["id"], entity_key=r["entity_key"], title=r["title"],
        aliases=_json_list(r["aliases"]), summary=r["summary"],
        concept_ids=_json_list(r["concept_ids"]),
        importance=float(r["importance"] or 0),
        last_built_at=_iso(r["last_built_at"]),
        links=links,
    )


@router.get("/pages/{page_id}", response_model=MemoryPageItem)
async def get_page(
    page_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        text("""SELECT id, entity_key, title, aliases, summary, concept_ids,
                       importance, last_built_at
                FROM memory_pages WHERE id = :id AND user_id = :uid"""),
        {"id": page_id, "uid": current_user.id},
    )).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Page not found")
    links = (await _links_for_pages(db, current_user.id, [page_id])).get(page_id, [])
    return _page_item(row, links)


@router.post("/pages/rebuild")
async def rebuild_pages(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """C4：手动重建实体页（夜间也会由 consolidation 调用）。"""
    from app.services.memory_page_service import build_pages
    stats = await build_pages(db, current_user.id)
    await db.commit()
    return stats


@router.get("/health", response_model=MemoryHealth)
async def memory_health(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """E6/G5：记忆健康度（失效原因、验证覆盖、dream 可注入率、注入字符）。"""
    row = (await db.execute(
        text("""SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE valid_to IS NULL) AS active,
            COUNT(*) FILTER (WHERE invalid_reason = 'merged') AS merged,
            COUNT(*) FILTER (WHERE invalid_reason = 'retracted') AS retracted,
            COUNT(*) FILTER (WHERE invalid_reason = 'source_lost') AS source_lost,
            COUNT(*) FILTER (WHERE invalid_reason = 'forgotten') AS forgotten,
            COUNT(*) FILTER (WHERE invalid_reason = 'expired') AS expired,
            COUNT(*) FILTER (WHERE needs_review) AS needs_review,
            COUNT(*) FILTER (WHERE verification_state = 'confirmed' AND valid_to IS NULL) AS verified,
            COUNT(*) FILTER (WHERE verification_state = 'contradicted' AND valid_to IS NULL) AS contradicted,
            COUNT(*) FILTER (WHERE verification_state = 'insufficient' AND valid_to IS NULL) AS insufficient
            FROM memory_concepts WHERE user_id = :uid"""),
        {"uid": current_user.id},
    )).mappings().first() or {}
    active = int(row.get("active") or 0)
    verified = int(row.get("verified") or 0)
    # P1-15：legacy 记账行（user_id 不可归属）只出全局卫生计数，不泄漏内容。
    eval_legacy = (await db.execute(
        text("""SELECT COUNT(*) FROM memory_eval_runs
                WHERE user_id IS NULL AND jsonb_typeof(metrics_json) = 'object'
                  AND metrics_json->>'user_id_unknown' = 'true'"""),
    )).scalar() or 0
    # G5/P1-11（2026-11-08）：告警阈值（[memory.alerts]；crit = 2× warn 阈值）。
    al_cfg = (get_config().memory.get("alerts") or {})
    th_fail = float(al_cfg.get("verify_fail_rate", 0.5))
    th_zero_days = int(al_cfg.get("zero_inject_days", 7))
    th_false = float(al_cfg.get("false_contradiction_rate", 0.1))
    th_cost = int(al_cfg.get("daily_llm_cost_units", 50))

    def _level(value: float, threshold: float) -> str:
        if threshold <= 0 or value < threshold:
            return "ok"
        return "crit" if value >= threshold * 2 else "warn"

    vsum = (await db.execute(
        text("""SELECT COALESCE(SUM((metrics_json->>'selected')::int),0) AS sel,
                       COALESCE(SUM((metrics_json->>'skipped')::int),0) AS sk,
                       COUNT(*) FILTER (
                         WHERE created_at >= date_trunc('day', (NOW() AT TIME ZONE 'UTC'))
                       ) AS runs_today
                FROM memory_eval_runs
                WHERE suite = 'dream_verify' AND user_id = :uid
                  AND created_at >= (NOW() AT TIME ZONE 'UTC') - INTERVAL '7 days'"""),
        {"uid": current_user.id},
    )).mappings().first() or {}
    sel7 = int(vsum.get("sel") or 0)
    fail_rate = round(int(vsum.get("sk") or 0) / sel7, 4) if sel7 else 0.0
    inj_age = (await db.execute(
        text("""SELECT EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'UTC') - created_at)) / 86400.0 AS age_days,
                       (metrics_json->>'injectable')::int AS injectable
                FROM memory_eval_runs
                WHERE suite = 'dream_injection' AND user_id = :uid
                ORDER BY created_at DESC LIMIT 1"""),
        {"uid": current_user.id},
    )).mappings().first()
    zero_days = float(inj_age["age_days"] or 0.0) if (inj_age and int(inj_age.get("injectable") or 0) == 0) else 0.0
    # 评审修复：分母不用「当前 contradicted 存量」——用户把积压确认后存量=0，
    # 告警恰在要它工作的场景自熄。改窗口内审计口径：分母 = 7 天内对
    # contradicted 概念的所有人工判定（confirm/reject/retry），分子 = 其中
    # confirm（=「判矛盾但用户说没错」= 误判率）。
    fc = (await db.execute(
        text("""SELECT COUNT(*) FILTER (WHERE arm = 'confirm') AS confirms,
                       COUNT(*) AS judged
                FROM memory_eval_runs
                WHERE suite = 'manual_verify' AND user_id = :uid
                  AND metrics_json->>'prev_state' = 'contradicted'
                  AND created_at >= (NOW() AT TIME ZONE 'UTC') - INTERVAL '7 days'"""),
        {"uid": current_user.id},
    )).mappings().first() or {}
    fc_confirms = int(fc.get("confirms") or 0)
    fc_judged = int(fc.get("judged") or 0)
    # 评审复审钉：最小样本守卫——n<3 时单条 confirm 就能把率推到 1.0 触发
    # crit（统计噪音）；样本不足按 0 处理（宁缺毋误报）。
    false_rate = round(fc_confirms / fc_judged, 4) if fc_judged >= 3 else 0.0
    runs_today = int(vsum.get("runs_today") or 0)
    alerts = [
        {"key": "verify_fail_rate", "value": fail_rate, "threshold": th_fail,
         "level": _level(fail_rate, th_fail)},
        {"key": "zero_injection", "value": round(zero_days, 2), "threshold": th_zero_days,
         "level": _level(zero_days, th_zero_days)},
        {"key": "false_contradiction_rate", "value": false_rate, "threshold": th_false,
         "level": _level(false_rate, th_false)},
        {"key": "verify_cost_units_today", "value": runs_today, "threshold": th_cost,
         "level": _level(runs_today, th_cost)},
    ]
    last_verify = (await db.execute(
        text("""SELECT metrics_json, created_at FROM memory_eval_runs
                WHERE suite = 'dream_verify' AND user_id = :uid
                ORDER BY created_at DESC LIMIT 1"""),
        {"uid": current_user.id},
    )).mappings().first()
    dream_metric = (await db.execute(
        text("""SELECT metrics_json FROM memory_eval_runs
                WHERE suite = 'dream_injection' AND user_id = :uid
                ORDER BY created_at DESC LIMIT 1"""),
        {"uid": current_user.id},
    )).scalar()
    recall_avg = (await db.execute(
        text("""SELECT AVG(injected_chars) FROM memory_recall_log
                WHERE user_id = :uid
                  AND created_at >= (NOW() AT TIME ZONE 'UTC') - INTERVAL '7 days'"""),
        {"uid": current_user.id},
    )).scalar()
    injectable_rate = None
    if dream_metric:
        try:
            dm = json.loads(dream_metric) if isinstance(dream_metric, str) else dream_metric
            scanned = int(dm.get("scanned") or 0)
            injectable = int(dm.get("injectable") or 0)
            injectable_rate = round(injectable / scanned, 4) if scanned else None
        except (TypeError, ValueError, AttributeError):
            injectable_rate = None
    return MemoryHealth(
        total=int(row.get("total") or 0), active=active,
        merged=int(row.get("merged") or 0),
        retracted=int(row.get("retracted") or 0),
        source_lost=int(row.get("source_lost") or 0),
        forgotten=int(row.get("forgotten") or 0),
        expired=int(row.get("expired") or 0),
        needs_review=int(row.get("needs_review") or 0),
        verified=verified,
        contradicted=int(row.get("contradicted") or 0),
        insufficient=int(row.get("insufficient") or 0),
        verify_coverage=round(verified / active, 4) if active else 0.0,
        dream_injectable_rate=injectable_rate,
        last_verify=(dict(last_verify["metrics_json"]) if isinstance(last_verify.get("metrics_json"), dict) else None) if last_verify else None,
        recall_7d_avg_injected_chars=round(float(recall_avg), 1) if recall_avg is not None else None,
        eval_runs_legacy=int(eval_legacy),
        alerts=alerts,
    )


@router.get("/dreams")
async def list_dreams(
    limit: int = 10,
    include_legacy: bool = False,
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
              AND (:include_legacy OR d.dream_type = 'consolidation')
            ORDER BY d.created_at DESC NULLS LAST, d.generated_for_date DESC LIMIT :lim
        """),
        {"uid": current_user.id, "lim": max(1, min(limit, 50)),
         "include_legacy": bool(include_legacy)},
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
    try:
        from app.services.memory_page_service import build_pages
        async with db.begin_nested():
            await build_pages(db, current_user.id)
    except Exception:
        logger.warning("page rebuild after concept delete failed", exc_info=True)
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
        text("DELETE FROM memory_page_links WHERE user_id = :uid"),
        {"uid": uid},
    )
    await db.execute(
        text("DELETE FROM memory_pages WHERE user_id = :uid"),
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
        erase_counts = {"_all": -1}
    # 评审复审钉（Critical）：任一模糊面失败（-1）都不得声称 deleted=all——
    # GDPR 擦除 fail-closed：partial + failed_surfaces 显式上报。
    return _erase_response(erase_counts)


def _erase_response(erase_counts: dict) -> dict:
    """擦除响应构造（纯函数，可测）：任一模糊面失败（-1）都不得声称
    deleted=all——GDPR 擦除 fail-closed，partial + failed_surfaces 显式上报。"""
    failed_surfaces = sorted(k for k, v in (erase_counts or {}).items() if v < 0)
    deleted = "all" if not failed_surfaces else "partial"
    return {"deleted": deleted, "extended_surfaces": erase_counts,
            "failed_surfaces": failed_surfaces}


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
    from app.services.memory_validity_service import apply_invalidation
    apply_invalidation(concept, "forgotten", status="forgotten")
    # 发射侧契约（同步域）：显式 ORM 写点 + ts bump。
    concept.status = "forgotten"
    concept.updated_at = datetime.utcnow()
    try:
        from app.services.memory_page_service import build_pages
        async with db.begin_nested():
            await build_pages(db, current_user.id)
    except Exception:
        logger.warning("page rebuild after forget failed", exc_info=True)
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
        ForgetManifestNotFound, create_deletion_manifest, execute_forget,
        parse_forget_needles)

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
    try:
        rep = await execute_forget(db, current_user.id, man.id)
    except ForgetManifestNotFound:
        # P0-08/D4：不存在/非本人 manifest = 404 语义，不再 500。
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="遗忘清单不存在或无权访问") from None
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
