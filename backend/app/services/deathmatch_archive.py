# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""C3 (JIT-Agent 2608.25593 harness bank): deathmatch harness archive.

Persists one row per TERMINAL goal-loop state (done / partial_complete /
human_gate) with the harness configuration snapshot and outcome metrics.
This is the data facility every later harness change is evaluated against
(recurrent-failure aggregation + train/dev acceptance gate, round-5 B4).

Pure builder + one INSERT helper — the agent loop calls this fail-open.
"""
import json
import uuid
from typing import Any, Dict, List

from sqlalchemy import text

from app.core.config import get_config

config = get_config()


def build_harness_record(conv: Any, decision: Dict[str, Any]) -> Dict[str, Any]:
    """Build the archive row (pure — no I/O). Keys match the
    deathmatch_harness_runs columns one-to-one."""
    plan = getattr(conv, "deathmatch_plan", None) or {}
    steps = plan.get("steps") or [] if isinstance(plan, dict) else []
    try:
        verify_model = config.deathmatch_verify_model or ""
    except Exception:
        verify_model = ""
    judge_model = ""
    try:
        judge_model = str(config.deathmatch_judge.get("model_name") or "")
    except Exception:
        pass
    if not judge_model:
        try:
            from app.model_gateway.registry import get_model_registry
            judge_model = str(get_model_registry().resolve("deathmatch.judge").model_name or "")
        except Exception:
            judge_model = ""
    try:
        config_snapshot = json.dumps({
            "max_turns": config.deathmatch_max_turns,
            "stall_partial_threshold": config.deathmatch_stall_partial_threshold,
            "stall_hard_threshold": config.deathmatch_stall_hard_threshold,
            "verify_enabled": config.deathmatch_verify_enabled,
            "verify_interval": config.deathmatch_verify_interval,
            "verify_moa_enabled": config.deathmatch_verify_moa_enabled,
            "judge_evidence_enabled": config.deathmatch_judge_evidence_enabled,
            "continuity_anchor_enabled": config.deathmatch_continuity_anchor_enabled,
            "bible_enabled": config.deathmatch_bible_enabled,
            "compression_context_length": config.agent_compression_context_length,
            "spike_guard_ratio": config.agent_compression_spike_guard_ratio,
        }, ensure_ascii=False)
    except Exception:
        config_snapshot = "{}"
    # B4 recurrence axis (A4.9 W2-I1): last verifier issues, capped.
    _issues: List[str] = []
    try:
        _vr = (decision or {}).get("verify_result") or {}
        _raw_issues = _vr.get("issues")
        if not _raw_issues:
            _last = getattr(conv, "deathmatch_last_verification_result", None) or {}
            _raw_issues = _last.get("issues")
        _issues = [str(i)[:200] for i in (_raw_issues or [])][:20]
    except Exception:
        _issues = []
    return {
        "id": str(uuid.uuid4()),
        "conversation_id": str(getattr(conv, "id", "") or ""),
        "final_status": str((decision or {}).get("status") or ""),
        "goal": str(getattr(conv, "deathmatch_goal", "") or "")[:2000],
        "turns": int(getattr(conv, "deathmatch_turns", 0) or 0),
        "wall_time_used_seconds": int(getattr(conv, "deathmatch_wall_time_used_seconds", 0) or 0),
        "verify_failures": int(getattr(conv, "deathmatch_verify_failures", 0) or 0),
        "plan_steps": len(steps),
        "plan_done": sum(1 for s in steps if s.get("status") == "done"),
        "judge_model": judge_model[:200],
        "verify_model": (verify_model or "")[:200],
        "config_json": config_snapshot,
        "issues_json": json.dumps(_issues, ensure_ascii=False),
    }


async def record_harness_run(db: Any, conv: Any, decision: Dict[str, Any]) -> None:
    """INSERT one archive row and commit (this helper owns the commit — it
    always runs on a dedicated session from the agent loop)."""
    rec = build_harness_record(conv, decision)
    await db.execute(
        text(
            "INSERT INTO deathmatch_harness_runs ("
            "id, conversation_id, final_status, goal, turns, "
            "wall_time_used_seconds, verify_failures, plan_steps, plan_done, "
            "judge_model, verify_model, config_json, issues_json"
            ") VALUES ("
            ":id, :conversation_id, :final_status, :goal, :turns, "
            ":wall_time_used_seconds, :verify_failures, :plan_steps, :plan_done, "
            ":judge_model, :verify_model, :config_json, :issues_json)"
        ),
        rec,
    )
    await db.commit()
