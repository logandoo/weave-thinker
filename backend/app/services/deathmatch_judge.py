# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deathmatch judge reply parser (P5.4 move-only, 2026-10-18)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)

def _parse_judge_response(raw: str) -> Tuple[str, str, bool, bool]:
    """Parse the judge reply into (verdict, reason, parse_failed, compact).

    ``compact`` is the judge's rubric signal (P2-11, SELFCOMPACT light):
    true when the judge considers the subtask solved / trajectory converged
    so the system may compress context early.


    verdict ∈ {"done", "continue", "wait", "blocked", "ask"} — "wait" means
    the goal loop should park (progress gated on an async task / backoff)
    without burning turns (D2 wait barrier); "blocked"/"ask" are the MEA
    control decisions (2608.01964): structurally unadvanceable → resumable
    stop with a report; user information/authorization required → park with
    the question surfaced.
    """
    from app.services.deathmatch_service import _truncate

    if not raw:
        return "continue", "judge returned empty response", True, False
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    data: Optional[Dict[str, Any]] = None
    try:
        data = json.loads(text)
    except Exception:
        match = _JSON_OBJECT_RE.search(text)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return "continue", f"judge reply was not JSON: {_truncate(raw, 200)!r}", True, False
    # P1.4: typed contract for the modern {"verdict": ...} shape.  Legacy
    # {"done": bool} replies keep the historical parser below.
    if "verdict" in data:
        import json as _json
        from app.prompts.contracts import JudgeVerdict, parse_contract

        contract, failure = parse_contract(
            JudgeVerdict, _json.dumps(data, ensure_ascii=False))
        if failure is None and contract is not None:
            verdict = str(contract.verdict or "").strip().lower()
            if verdict in ("done", "continue", "wait", "blocked", "ask"):
                reason = str(contract.reason or "").strip() or "no reason provided"
                if verdict == "wait":
                    ws = contract.wait_seconds if contract.wait_seconds is not None else 30
                    reason = f"{reason} (wait_seconds={max(5, min(ws, 3600))})"
                return verdict, reason, False, bool(contract.compact)
    # New shape: {"verdict": "done|continue|wait|blocked|ask", "reason": ...}.
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict in ("done", "continue", "wait", "blocked", "ask"):
        reason = str(data.get("reason") or "").strip()
        if not reason:
            reason = "no reason provided"
        if verdict == "wait":
            ws = data.get("wait_seconds")
            try:
                ws = int(ws) if ws is not None else 30
            except (TypeError, ValueError):
                ws = 30
            reason = f"{reason} (wait_seconds={max(5, min(ws, 3600))})"
        return verdict, reason, False, bool(data.get("compact"))
    # Legacy shape: {"done": bool, "reason": ...}.
    done_val = data.get("done")
    if isinstance(done_val, str):
        done = done_val.strip().lower() in {"true", "yes", "1", "done"}
    else:
        done = bool(done_val)
    reason = str(data.get("reason") or "").strip()
    if not reason:
        reason = "no reason provided"
    return ("done" if done else "continue"), reason, False, bool(data.get("compact"))
