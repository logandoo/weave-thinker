# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deathmatch parsers (P5.4 move-only, 2026-10-18).

Grilling readiness, question normalization, action triage and judge taxonomy
moved verbatim from ``deathmatch_service.py``; the service re-exports them.
"""
from __future__ import annotations

import json
import re as _re
from typing import Any, Dict, List, Optional

from app.services.eval_metrics import TAXONOMY_VALUES as JUDGE_TAXONOMY_VALUES
from app.services.deathmatch_judge import _JSON_OBJECT_RE


def _sanitize_research_field(text: Any, limit: int) -> str:
    """Bounded, single-line rendering of untrusted search metadata."""
    s = str(text or "")
    s = _re.sub(r"[\x00-\x1f\x7f]+", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


def _parse_grilling_readiness(raw: str) -> Dict[str, Any]:
    """Parse the round-synthesis readiness report. Fail-open → keep rounds."""
    empty = {
        "should_continue": True, "reason": "", "open_decisions": [],
        "vague_dimensions": [], "assumed_defaults": [],
        "diminishing_returns": False,
    }
    if not raw or not raw.strip():
        return empty
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    data = None
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
        return empty

    def _str_list(key: str) -> List[str]:
        raw_list = data.get(key, [])
        if not isinstance(raw_list, list):
            return []
        out = []
        for item in raw_list:
            s = str(item).strip() if item is not None else ""
            if s:
                out.append(s)
        return out

    val = str(data.get("should_continue", "true")).strip().lower()
    return {
        "should_continue": val not in ("false", "no", "0"),
        "reason": str(data.get("reason", "") or ""),
        "open_decisions": _str_list("open_decisions"),
        "vague_dimensions": _str_list("vague_dimensions"),
        "assumed_defaults": _str_list("assumed_defaults"),
        "diminishing_returns": str(
            data.get("diminishing_returns", "false")
        ).strip().lower() in ("true", "yes", "1"),
    }

def _normalize_question_text(text: str) -> str:
    return _re.sub(r"[\s\W_]+", "", (text or "")).lower()

_TRIAGE_ROUTES = ("state_revision", "replan", "explore_credit")

_TRIAGE_LABELS = ("critical", "exploratory", "noisy")

def _parse_action_triage(raw: str) -> Optional[Dict[str, Any]]:
    """Parse the stall action-triage verdict (AEWM Action Judge 三分法).
    Strict: unknown route/label → None → fail-open to the legacy path."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    data = None
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
        return None
    route = str(data.get("route") or "").strip()
    label = str(data.get("label") or "").strip()
    if route not in _TRIAGE_ROUTES or label not in _TRIAGE_LABELS:
        return None
    # A4.9 M2：route/label 配对校验（label 不是装饰）——不配对即 fail-open。
    pairing_ok = (
        (route == "explore_credit" and label == "exploratory")
        or (route == "state_revision" and label == "noisy")
        or (route == "replan" and label in ("critical", "noisy"))
    )
    if not pairing_ok:
        return None
    claims_raw = data.get("noisy_claims") or []
    if not isinstance(claims_raw, list):
        claims_raw = []
    # A4.9 I5：claims 是 LLM 派生文本（可能间接携带网络内容）——单行化、
    # 剥尖括号、截断后再进入提示词链。
    claims = [
        _sanitize_research_field(str(c), 150).replace("<", "").replace(">", "")
        for c in claims_raw
    ]
    claims = [c for c in claims if c]
    return {
        "route": route,
        "label": label,
        "noisy_claims": claims[:5],
        "note": _sanitize_research_field(data.get("note") or "", 200)
                .replace("<", "").replace(">", ""),
    }

def _extract_judge_taxonomy(raw: str) -> Optional[str]:
    """从判词 JSON 提取可选 taxonomy（非法/缺失 → None，绝不影响 verdict 解析）。"""
    if not raw:
        return None
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
        return None
    val = str(data.get("taxonomy") or "").strip().lower()
    return val if val in JUDGE_TAXONOMY_VALUES else None
