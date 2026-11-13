# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W2-1/W2-2 证据视图与隐式冲突（记忆/长程 v2，2026-10-23，判据 #11/#12）。

- W2-1 证据视图（RaMem 2606.22844 四元组）：候选 → {fact, source,
  event_time, mention_time, session_span, participants, validity,
  conflict_group}；**确定性裁决**（同组取 valid 且 event_time 最新，
  旧者标 superseded 不注入）——不调 LLM，同输入幂等。
- W2-2 隐式冲突（STALE 2605.06527 三维度）：实体/时间/否定规则优先；
  LLM 兜底**默认关**（预留键 [memory] implicit_conflict_llm_fallback=false）。

接入为 shadow（裁决结果可观测、默认不改注入顺序——P3）。
W2 续波（2026-10-23，判据 #48-#53）：`shadow_report` + `record_evidence_shadow*`
把裁决/冲突结果接进运行时（`_build_injection_context_ex` 装配处消费），
稀有事件落 `memory_eval_runs`（suite=evidence_view_shadow /
implicit_conflict_shadow），**只记录不改注入集合**。
"""
import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_NEGATION_RE = re.compile(r"(不|别|停止|禁|never|not|don'?t)", re.I)
_POLICY_RE = re.compile(r"(改|换|更新|调整|现在|从现在|switch|update|change)", re.I)
_TIMEISH_RE = re.compile(r"\d{1,4}[-年/.]\d{1,2}|\d{1,2}:\d{2}|周[一二三四五六日]|昨天|今天|明天")
# 实体键规范化：剥数值/时间/标点 + 否定/策略/功能词（确定性启发式；
# 真实体消歧需 NER，超出本波范围——文档字符串声明为已知边界）
_KEY_NOISE_RE = re.compile(
    r"[\d\s:：.．,，;；!！?？、/\\\-—~～()\[\]（）「」『』“”\"']+"
    r"|[不别禁停止改换更新调整要在从把被了的都也还就]"
)


def _parse_iso_dt(value) -> "datetime | None":
    """P0-08/D3：ISO 解析（datetime/date 对象直取；字符串 fromisoformat；
    解析失败返回 None = 调用方保守处理）。"""
    if isinstance(value, datetime):
        return value
    try:
        if hasattr(value, "isoformat") and not isinstance(value, str):
            import datetime as _d
            if isinstance(value, _d.date):
                return datetime(value.year, value.month, value.day)
    except Exception:
        return None
    s = str(value or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 4].strip(), fmt)
        except ValueError:
            continue
    return None


def _conflict_group_key(fact: str) -> str:
    """确定性实体键：引号内容优先；规范化后取前 8 字符 sha1[:8]。"""
    s = str(fact or "")
    m = re.search(r"[「『“\"']([^」』”\"']{2,40})[」』”\"']", s)
    base = m.group(1) if m else s
    base = _KEY_NOISE_RE.sub("", base)[:8]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]


def build_evidence_units(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """候选 → 证据单元（缺字段给确定性默认）。"""
    units: List[Dict[str, Any]] = []
    for c in candidates or []:
        c = dict(c or {})
        fact = str(c.get("fact") or c.get("text") or c.get("content") or "")
        event_time = c.get("event_time") or c.get("valid_from") or c.get("created_at")
        mention_time = c.get("mention_time") or c.get("created_at") or event_time
        validity = str(c.get("validity") or "valid")
        if validity == "valid":
            if c.get("superseded") or c.get("superseded_by"):
                validity = "superseded"
            elif c.get("verification_state") == "contradicted":
                # B9/J2：与原始 transcript 冲突的笔记按 stale 参与裁决，
                # 不得被当成 valid 注入/加权。
                validity = "stale"
            elif c.get("valid_to"):
                # 只有已过期才标 expired（valid_to 在未来 = 仍有效）。
                # P0-08/D3（2026-11-08）：解析成 datetime 比较——旧 `[:10]` 字符串前缀
                # 比较把 '0abc' 这类垃圾串按字典序误判 expired；解析失败保守按 valid。
                vt = c.get("valid_to")
                vt_dt = _parse_iso_dt(vt)
                if vt_dt is not None:
                    now_dt = datetime.now(timezone.utc)
                    if vt_dt.tzinfo is None:
                        vt_dt = vt_dt.replace(tzinfo=timezone.utc)
                    if vt_dt < now_dt:
                        validity = "expired"
        units.append({
            "unit_id": str(c.get("id") or c.get("unit_id") or ""),
            "fact": fact,
            "source": str(c.get("source") or c.get("source_trust") or "unknown"),
            "event_time": event_time,
            "mention_time": mention_time,
            "session_span": c.get("session_span") or c.get("conversation_id") or "",
            "participants": c.get("participants") or "",
            "validity": validity,
            "conflict_group": _conflict_group_key(fact),
        })
    return units


def adjudicate_conflicts(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """确定性裁决：同组取 valid 且 event_time 最新；其余标 superseded；
    全组无 valid → 保留最新一条标 stale。返回全量（幂等）。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for u in units or []:
        groups.setdefault(str(u.get("conflict_group") or ""), []).append(dict(u))
    out: List[Dict[str, Any]] = []
    for members in groups.values():
        valid = [m for m in members if m.get("validity") == "valid"]
        if valid:
            winner = max(valid, key=lambda m: str(m.get("event_time") or ""))
            for m in members:
                if m is not winner and m.get("validity") == "valid":
                    m["validity"] = "superseded"
        else:
            newest = max(members, key=lambda m: str(m.get("event_time") or ""))
            newest["validity"] = "stale"
        out.extend(members)
    return out


def detect_implicit_conflict(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """W2-2 规则优先：同组 event_time 不同且事实形态不同 → 三维度分类。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for u in units or []:
        groups.setdefault(str(u.get("conflict_group") or ""), []).append(u)
    out: List[Dict[str, Any]] = []
    for g, members in groups.items():
        times = {str(m.get("event_time") or "") for m in members}
        facts = [str(m.get("fact") or "") for m in members]
        if len(times) < 2 or len(set(facts)) < 2:
            continue
        joined = " ".join(facts)
        if _NEGATION_RE.search(joined):
            kind = "premise_resistance"
        elif _POLICY_RE.search(joined):
            kind = "policy_adaptation"
        else:
            kind = "stale_state"
        out.append({"group": g, "kind": kind,
                    "detail": f"{len(members)} 条事实时间不一致（{min(times)} vs {max(times)}）"})
    return out


# ---------- A1/A2 shadow 报告与落档（W2 续波 2026-10-23，判据 #48-#53） ----------

def shadow_report(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """候选 → shadow 报告（稀有事件：无 supersede/stale/expired 且无冲突 → None）。

    只含元数据（unit_id/validity/conflict_group/kind/计数），**不含事实正文**
    （D6 隐私口径，对齐 memory_recall_log 台账纪律）。确定性、幂等。
    形状（Consistency Hub）：
    {"unit_count", "validity_counts", "superseded_ids", "stale_ids",
     "expired_ids", "conflicts": [{"group", "kind", "unit_ids"}]}。
    """
    units = build_evidence_units(list(candidates or []))
    if not units:
        return None
    adjudicated = adjudicate_conflicts(units)
    validity_counts: Dict[str, int] = {}
    superseded: List[str] = []
    stale: List[str] = []
    expired: List[str] = []
    group_units: Dict[str, List[str]] = {}
    for u in adjudicated:
        v = str(u.get("validity") or "")
        uid = str(u.get("unit_id") or "")
        validity_counts[v] = validity_counts.get(v, 0) + 1
        if v == "superseded":
            superseded.append(uid)
        elif v == "stale":
            stale.append(uid)
        elif v == "expired":
            expired.append(uid)
        group_units.setdefault(str(u.get("conflict_group") or ""), []).append(uid)
    conflicts = [
        {"group": str(c.get("group") or ""), "kind": str(c.get("kind") or ""),
         "unit_ids": group_units.get(str(c.get("group") or ""), [])}
        for c in detect_implicit_conflict(adjudicated)
    ]
    if not (superseded or stale or expired or conflicts):
        return None
    return {
        "unit_count": len(adjudicated),
        "validity_counts": validity_counts,
        "superseded_ids": superseded,
        "stale_ids": stale,
        "expired_ids": expired,
        "conflicts": conflicts,
    }


_write_tasks: set = set()

# 落档边界允许清单（B 评审 Important：D6 元数据口径在持久化边界强制——
# 不依赖上游 shadow_report 形状；validity/kind 收紧到枚举，冲突只投影
# {group, kind, unit_ids}，其余字段一律不落库）。
_VALIDITY_KEYS = ("valid", "superseded", "expired", "stale")
_CONFLICT_KINDS = ("stale_state", "premise_resistance", "policy_adaptation")


def _project_metrics(report: Dict[str, Any]) -> tuple:
    """report → (裁决 metrics, 冲突 metrics)：持久化边界允许清单投影。"""
    raw_counts = report.get("validity_counts") or {}
    validity_counts = {
        k: int(raw_counts.get(k, 0)) for k in _VALIDITY_KEYS
        if int(raw_counts.get(k, 0) or 0) > 0
    }
    ids = lambda key: [str(x) for x in (report.get(key) or [])]  # noqa: E731
    conflicts = []
    for c in (report.get("conflicts") or []):
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "")
        conflicts.append({
            "group": str(c.get("group") or ""),
            "kind": kind if kind in _CONFLICT_KINDS else "unknown",
            "unit_ids": [str(x) for x in (c.get("unit_ids") or [])],
        })
    metrics_adjudication = {
        "unit_count": int(report.get("unit_count") or 0),
        "validity_counts": validity_counts,
        "superseded_ids": ids("superseded_ids"),
        "stale_ids": ids("stale_ids"),
        "expired_ids": ids("expired_ids"),
        "llm_fallback": bool(report.get("llm_fallback")),
    }
    metrics_conflicts = {
        "unit_count": int(report.get("unit_count") or 0),
        "conflicts": conflicts,
        "llm_fallback": bool(report.get("llm_fallback")),
    }
    return metrics_adjudication, metrics_conflicts


async def record_evidence_shadow(report: Optional[Dict[str, Any]],
                                 *, session_factory: Any = None) -> int:
    """shadow 报告 → `memory_eval_runs` 稀有事件行（独立会话，失败静默返回 0）。

    裁决行 suite=evidence_view_shadow（supersede/stale/expired 非空才写）·
    冲突行 suite=implicit_conflict_shadow（conflicts 非空才写）；arm=shadow。
    落档口径对齐 retention_shadow/ab_metrics 同表先例（memory_eval_runs=
    测试/影子库口径，不进生产判定）。"""
    try:
        if not report:
            return 0
        metrics_adjudication, metrics_conflicts = _project_metrics(report)
        rows: List[tuple] = []
        if (metrics_adjudication["superseded_ids"] or metrics_adjudication["stale_ids"]
                or metrics_adjudication["expired_ids"]):
            rows.append(("evidence_view_shadow", "evshadow", metrics_adjudication))
        if metrics_conflicts["conflicts"]:
            rows.append(("implicit_conflict_shadow", "conflictshadow", metrics_conflicts))
        if not rows:
            return 0
        from app.db.database import AsyncSessionLocal, MemoryEvalRun
        factory = session_factory or AsyncSessionLocal
        day = datetime.utcnow().strftime("%Y%m%d")
        async with factory() as session:
            for suite, prefix, metrics in rows:
                session.add(MemoryEvalRun(
                    run_id=f"{prefix}-{day}", suite=suite, arm="shadow",
                    metrics_json=metrics))
            await session.commit()
        return len(rows)
    except Exception:
        logger.debug("evidence shadow write failed (silent)", exc_info=True)
        return 0


def record_evidence_shadow_bg(report: Optional[Dict[str, Any]]) -> None:
    """fire-and-forget 包装（对齐 record_recall_log_bg 纪律：调用方永不等待、
    异常永不外泄——shadow 台账永不阻塞或影响检索主流程）。"""
    if not report:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        task = loop.create_task(record_evidence_shadow(report))
        _write_tasks.add(task)
        task.add_done_callback(_write_tasks.discard)
    except Exception:
        logger.debug("evidence shadow scheduling failed (silent)", exc_info=True)
