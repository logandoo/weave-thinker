# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W3-3/W3-4 梦境 probe/repair（记忆/长程 v2，2026-10-23，判据 #17，D2=b）。

MemDream 式（2609.34545）：探测查询 → hit/miss → 只修 miss → 修复报告
（hit@k before/after + cost_tokens）。**默认关**（enabled=False，P3）；
14 天无正收益自动回退方案 B（check_stop_rule 落 stop 标记，恢复需人工删行）。
0 LLM：探测词取活跃概念名，修复=建轻量 agent_inferred 概念。
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STOP_SUITE = "dream_repair_stop"
RUN_SUITE = "dream_repair"


def _extract_probe_terms(text: str) -> List[str]:
    """确定性词条抽取（0 LLM）：引号内容优先；否则 2-12 字的句段。"""
    import re

    quoted = [q.strip() for q in
              re.findall(r"[「『“\"']([^」』”\"'\n]{2,24})[」』”\"']", str(text or ""))
              if q.strip()]
    if quoted:
        return quoted[:6]
    segs = [s.strip() for s in re.split(r"[。！？!?\n，,；;]", str(text or ""))]
    return [s[:24] for s in segs if 2 <= len(s.strip()) <= 24][:6]


async def run_probe_repair(db: Any, user_id: str, *, enabled: bool = False,
                           probe_count: int = 6) -> Optional[Dict[str, Any]]:
    if not enabled:
        return None
    if await _stopped(db, user_id):
        logger.info("dream probe/repair stopped (no positive gain in 14d)")
        return None
    from sqlalchemy import select

    from app.db.database import DreamRepairReport, MemoryConcept, MemoryEvalRun

    # 评审 A I5/B C2：探测词取自**近用户消息抽取的词条**（可不命中——
    # 自问自答恒命中、hit@k 无信息量）；hit = 词条命中活跃概念 name/desc。
    msg_r = await db.execute(
        __import__("sqlalchemy").text(
            "SELECT content FROM messages WHERE role = 'user'"
            " AND conversation_id IN (SELECT id FROM conversations WHERE user_id = :uid)"
            " ORDER BY created_at DESC LIMIT :n"),
        {"uid": user_id, "n": int(probe_count) * 5})
    probes: List[str] = []
    for row in msg_r.fetchall():
        for term in _extract_probe_terms(str(row[0] or "")):
            if term not in probes:
                probes.append(term)
            if len(probes) >= int(probe_count):
                break
        if len(probes) >= int(probe_count):
            break
    if not probes:
        return None

    hits: List[str] = []
    misses: List[str] = []
    for name in probes:
        hit = await db.scalar(
            select(MemoryConcept.id).where(
                MemoryConcept.user_id == user_id,
                MemoryConcept.status == "active",
                MemoryConcept.canonical_name == name).limit(1))
        (hits if hit else misses).append(name)
    before = len(hits) / len(probes) if probes else 0.0

    for name in misses:
        db.add(MemoryConcept(
            user_id=user_id, canonical_name=name,
            description_short="dream-repair",
            description_full=f"dream probe repair: {name}",
            source_trust="agent_inferred", memory_type="semantic",
            weight=0.3))
    await db.flush()
    # 评审 A I5：after 必须复测而非硬编码——修复后重查命中。
    after_hits = 0
    for name in probes:
        hit = await db.scalar(
            select(MemoryConcept.id).where(
                MemoryConcept.user_id == user_id,
                MemoryConcept.status == "active",
                MemoryConcept.canonical_name == name).limit(1))
        if hit:
            after_hits += 1
    after = after_hits / len(probes) if probes else 0.0

    report = DreamRepairReport(
        user_id=user_id,
        run_date=datetime.utcnow().strftime("%Y-%m-%d"),
        probe_count=len(probes), hit_count=len(hits), miss_count=len(misses),
        repairs_json=misses,
        hit_at_k_before=before, hit_at_k_after=after, cost_tokens=0)
    db.add(report)
    db.add(MemoryEvalRun(
        run_id=f"dream-repair-{datetime.utcnow().strftime('%Y%m%d%H%M')}",
        suite=RUN_SUITE, arm=f"probes={len(probes)}",
        metrics_json={"hit_at_k_before": before, "hit_at_k_after": after,
                      "user_id": user_id, "repairs": misses}))
    await db.flush()
    return {"probe_count": len(probes), "hit_count": len(hits),
            "miss_count": len(misses), "hit_at_k_before": before,
            "hit_at_k_after": after, "repairs": misses, "cost_tokens": 0}


async def check_stop_rule(db: Any, user_id: str, *,
                          window_days: int = 14,
                          min_reports: int = 1) -> bool:
    """W3-4：窗口内修复增益均值 ≤ 0 → 落 stop 标记（回退方案 B）。

    `min_reports`（评审修批 A#2）：窗口内报告数下限——不足不判停，
    防「单份零增益报告即永久停」的第 1 天误杀（运行时接线传 3；
    默认 1=既有语义，服务级测试契约不变）。"""
    from sqlalchemy import select

    from app.db.database import DreamRepairReport, MemoryEvalRun

    res = await db.execute(
        select(DreamRepairReport).where(
            DreamRepairReport.user_id == user_id,
            # 评审 A I5：窗口=近 14 天（原 `>= 今天零点` 是错窗）
            DreamRepairReport.created_at >= datetime.utcnow() - timedelta(days=int(window_days)),
        ).order_by(DreamRepairReport.created_at.desc()).limit(50))
    rows = list(res.scalars().all())
    if not rows:
        return False
    # 评审修批（A#2/B#1 blocking）：min_reports 必须真正执行——窗口内
    # 报告数不足下限不判停（防单份零增益报告第 1 天永久停）。
    if len(rows) < max(int(min_reports), 1):
        return False
    gains = [(float(r.hit_at_k_after or 0) - float(r.hit_at_k_before or 0))
             for r in rows]
    if gains and (sum(gains) / len(gains)) <= 0:
        db.add(MemoryEvalRun(
            run_id=f"dream-repair-stop-{datetime.utcnow().strftime('%Y%m%d')}",
            suite=STOP_SUITE, arm=str(user_id),
            metrics_json={"reason": f"no positive gain in {window_days}d",
                          "rows": len(rows)}))
        await db.flush()
        return True
    return False


async def _stopped(db: Any, user_id: str) -> bool:
    from sqlalchemy import select

    from app.db.database import MemoryEvalRun

    row = await db.scalar(
        select(MemoryEvalRun.id).where(
            MemoryEvalRun.suite == STOP_SUITE,
            MemoryEvalRun.arm == str(user_id)).limit(1))
    return row is not None


async def list_repair_reports(db: Any, user_id: str,
                              limit: int = 20) -> List[Dict[str, Any]]:
    from sqlalchemy import select

    from app.db.database import DreamRepairReport

    res = await db.execute(
        select(DreamRepairReport).where(
            DreamRepairReport.user_id == user_id
        ).order_by(DreamRepairReport.created_at.desc()).limit(int(limit)))
    return [{
        "id": r.id,
        "run_date": r.run_date,
        "probe_count": r.probe_count,
        "hit_count": r.hit_count,
        "miss_count": r.miss_count,
        "repairs": list(r.repairs_json or []),
        "hit_at_k_before": r.hit_at_k_before,
        "hit_at_k_after": r.hit_at_k_after,
        "cost_tokens": r.cost_tokens,
    } for r in res.scalars().all()]
