# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W0-1/W2-3/W2-4 度量与安全网（记忆/长程 v2，2026-10-23，判据 #2/#13/#14）。

从既有落库数据（memory_recall_log / memory_llm_calls / messages /
deathmatch_harness_runs）计算线上基线六指标，按模型分档：

- tokens/session（memory_llm_calls 的 prompt+completion tokens / 会话数）
- cache_hit_ratio · p95_latency_ms · injected_chars_avg（memory_recall_log）
- duplicate_tool_rate（messages.tool_calls 序列内重复派发占比）
- done→reopen 率（deathmatch_harness_runs 终态后同会话再来用户轮的占比，
  定义见 reopen_rate docstring——既有落库上的确定性代理口径）

W2-3：injection_min_relevance 拒绝面（false-quarantine）shadow 可观测——
`quarantine_stats` 汇总 rejects_json 台账；W2-4：cache_hit/p95 入报告，
缓存布局回归见 tests/test_memory_v2_regression_pack.py（记忆恒在 dynamic 尾部）。
"""
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def p95(values: Iterable[float]) -> float:
    """线性插值 p95（口径钉死：pos=0.95*(n-1)，邻值插值）。"""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return 0.0
    if len(vals) == 1:
        return float(vals[0])
    pos = 0.95 * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return float(vals[lo] + (vals[hi] - vals[lo]) * frac)


def aggregate_recall_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows or [])
    n = len(rows)
    if not n:
        return {"rows": 0, "cache_hit_ratio": 0.0, "p95_latency_ms": 0.0,
                "injected_chars_avg": 0.0}
    hits = sum(1 for r in rows if r.get("cache_hit"))
    return {
        "rows": n,
        "cache_hit_ratio": hits / n,
        "p95_latency_ms": p95([r.get("elapsed_ms") or 0 for r in rows]),
        "injected_chars_avg": sum(int(r.get("injected_chars") or 0)
                                  for r in rows) / n,
    }


def duplicate_tool_rate(sequences: Iterable[List[str]]) -> Dict[str, Any]:
    """重复工具率 = 同一调用序列内重复派发（同签名再次出现）的次数占比。"""
    total, dup = 0, 0
    for seq in sequences or []:
        seen = set()
        for call in seq or []:
            total += 1
            if call in seen:
                dup += 1
            seen.add(call)
    return {"tool_calls": total,
            "duplicate_rate": (dup / total) if total else 0.0}


def reopen_rate(runs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """done→reopen 率（确定性代理口径）：deathmatch 终态（done/partial）落
    deathmatch_harness_runs 后，同会话在 run.created_at 之后再来 ≥1 条用户轮
    即计一次 reopen。样本不足时报告本身标注 LOW confidence（§9.1）。"""
    runs = list(runs or [])
    done = [r for r in runs if r.get("done")]
    reopened = sum(1 for r in done if int(r.get("later_user_turns") or 0) > 0)
    return {"done_runs": len(done),
            "reopen_rate": (reopened / len(done)) if done else 0.0}


def quarantine_stats(rejects: Iterable[Dict[str, Any]],
                     injected_count: int = 0) -> Dict[str, Any]:
    """W2-3 误杀审计面：injection_min_relevance 拒绝的候选（shadow 记录）。"""
    rejects = list(rejects or [])
    by_tier: Dict[str, int] = {}
    for r in rejects:
        t = str(r.get("tier") or "unknown")
        by_tier[t] = by_tier.get(t, 0) + 1
    total = len(rejects) + int(injected_count or 0)
    return {"rejected": len(rejects), "by_tier": by_tier,
            "inject_rate_denominator": total,
            "reject_rate": (len(rejects) / total) if total else 0.0}


def group_by_model(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        m = str(r.get("model") or "unknown")
        slot = out.setdefault(m, {"tokens": 0, "calls": 0, "sessions": set()})
        slot["tokens"] += int(r.get("prompt_tokens") or 0) + int(
            r.get("completion_tokens") or 0)
        slot["calls"] += 1
        if r.get("session_id"):
            slot["sessions"].add(str(r["session_id"]))
    for slot in out.values():
        slot["sessions"] = len(slot["sessions"])
    return out


def render_baseline_report(data: Dict[str, Any]) -> str:
    """7 天基线报告 markdown（含 W5-4 告警段：残余/误杀/成本）。"""
    lines = [f"# 记忆/长程 v2 基线报告（{data.get('window_days', 7)} 天窗口）", ""]
    by_model = data.get("by_model") or {}
    lines.append("## 按模型分档（tokens/session）")
    lines.append("| model | tokens | calls | sessions |")
    lines.append("|---|---|---|---|")
    for m, s in sorted(by_model.items()):
        lines.append(f"| {m} | {s.get('tokens', 0)} | {s.get('calls', 0)} | {s.get('sessions', 0)} |")
    rec = data.get("recall") or {}
    lines.append("")
    lines.append("## 召回/延迟/注入")
    lines.append(f"- cache_hit_ratio: {rec.get('cache_hit_ratio', 0):.3f}")
    lines.append(f"- p95_latency_ms: {rec.get('p95_latency_ms', 0):.1f}")
    lines.append(f"- injected_chars_avg: {rec.get('injected_chars_avg', 0):.0f}")
    tools = data.get("tools") or {}
    lines.append(f"- duplicate_tool_rate: {tools.get('duplicate_rate', 0):.3f}"
                 f"（{tools.get('tool_calls', 0)} calls）")
    ro = data.get("reopen") or {}
    lines.append(f"- done→reopen rate: {ro.get('reopen_rate', 0):.3f}"
                 f"（{ro.get('done_runs', 0)} done runs）")
    q = data.get("quarantine") or {}
    lines.append(f"- 读时门槛误杀面（W2-3 shadow）: rejected={q.get('rejected', 0)}"
                 f" reject_rate={q.get('reject_rate', 0):.3f}")
    lines.append("")
    lines.append("## 告警（残余/误杀/成本，W5-4）")
    alerts = data.get("alerts") or []
    if alerts:
        for a in alerts:
            lines.append(f"- ⚠ {a}")
    else:
        lines.append("- 无")
    if int(ro.get("done_runs") or 0) < 5:
        lines.append("")
        lines.append("> LOW confidence：done 样本 < 5（§9.1），方向性参考。")
    return "\n".join(lines)


async def collect_baseline_metrics(db: Any, *, days: int = 7,
                                   user_id: Optional[str] = None) -> Dict[str, Any]:
    """从既有落库采集六指标（savepoint 隔离，单表缺列不毒化事务）。"""
    from sqlalchemy import text

    async def _rows(stmt: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            async with db.begin_nested():
                res = await db.execute(text(stmt), params)
                return [dict(r) for r in res.mappings().all()]
        except Exception:
            logger.debug("baseline collect miss: %s", stmt[:60], exc_info=True)
            return []

    uid = str(user_id or "")
    user_sql = "AND user_id = :uid" if uid else ""
    params: Dict[str, Any] = {"days": int(days)}
    if uid:
        params["uid"] = uid

    recall_rows = await _rows(
        "SELECT cache_hit, elapsed_ms, injected_chars, rejects_json"
        f" FROM memory_recall_log WHERE created_at >= NOW() - (:days * INTERVAL '1 day') {user_sql}",
        params)
    llm_rows = await _rows(
        "SELECT model, prompt_tokens, completion_tokens,"
        " NULL AS session_id"
        f" FROM memory_llm_calls WHERE created_at >= NOW() - (:days * INTERVAL '1 day') {user_sql}",
        params)
    tool_rows = await _rows(
        "SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL"
        " AND created_at >= NOW() - (:days * INTERVAL '1 day')"
        " AND conversation_id IN (SELECT id FROM conversations"
        + (" WHERE user_id = :uid)" if uid else ")"),
        params)
    done_rows = await _rows(
        "SELECT h.id AS run_id, h.conversation_id, h.final_status, h.created_at,"
        " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = h.conversation_id"
        "  AND m.role = 'user' AND m.created_at > h.created_at) AS later_user_turns"
        " FROM deathmatch_harness_runs h WHERE h.created_at >= NOW() - (:days * INTERVAL '1 day')"
        + (" AND h.conversation_id IN (SELECT id FROM conversations WHERE user_id = :uid)" if uid else ""),
        params)
    residual_rows = await _rows(
        "SELECT COUNT(*) AS n FROM deletion_manifests WHERE status = 'residual'"
        f" AND created_at >= NOW() - (:days * INTERVAL '1 day') {user_sql}",
        params)

    def _parse_tool_calls(raw: Any) -> List[str]:
        import json
        try:
            calls = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (TypeError, ValueError):
            return []
        out = []
        for c in calls or []:
            if isinstance(c, dict):
                out.append(f"{c.get('name') or c.get('function') or '?'}:"
                           f"{str(c.get('arguments') or c.get('args') or '')[:60]}")
            else:
                out.append(str(c)[:80])
        return out

    recalls = aggregate_recall_rows(recall_rows)
    tools = duplicate_tool_rate([_parse_tool_calls(r.get("tool_calls"))
                                 for r in tool_rows])
    reopen = reopen_rate([{
        "done": str(r.get("final_status") or "").lower() in ("done", "complete",
                                                             "partial_complete"),
        "later_user_turns": r.get("later_user_turns") or 0,
    } for r in done_rows])
    all_rejects: List[Dict[str, Any]] = []
    for r in recall_rows:
        raw = r.get("rejects_json")
        if not raw:
            continue
        import json
        try:
            all_rejects.extend(json.loads(raw) or [])
        except (TypeError, ValueError):
            continue
    quar = quarantine_stats(all_rejects, injected_count=recalls.get("rows", 0))

    alerts: List[str] = []
    if quar.get("reject_rate", 0) > 0.10:
        alerts.append(f"读时门槛误杀率 {quar['reject_rate']:.2f} 超过 0.10 阈值（W2-3）")
    if int((residual_rows[0]["n"] if residual_rows else 0) or 0) > 0:
        alerts.append(f"遗忘残余清单 {residual_rows[0]['n']} 条未清（W5-4）")
    # 评审 B I9：成本告警（PLAN §8 硬预算 20k tokens/用户/日）
    model_groups = group_by_model(llm_rows)
    total_tokens = sum(int(s.get("tokens") or 0) for s in model_groups.values())
    budget = 20000
    try:
        from app.core.config import get_config
        budget = int(get_config().memory.get("cost", {}).get("daily_budget_tokens", 20000))
    except Exception:
        pass
    if total_tokens > budget:
        alerts.append(f"记忆侧 LLM 成本 {total_tokens} tokens 超日预算 {budget}（W5-4）")

    return {
        "window_days": int(days),
        "by_model": group_by_model(llm_rows),
        "recall": recalls,
        "tools": tools,
        "reopen": reopen,
        "quarantine": quar,
        "alerts": alerts,
    }
