# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Read-only long-term memory search tool (TODO 2026-11-08 C2/C3).

The `memory` tool only reads AGENT.md/USER.md. v2 concepts/episodes/pages were
previously available only through passive injection. This tool lets the agent
look them up on demand, including validity/verification state and source
message ids, without granting any write authority.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.memory_validity_service import derive_invalid_reason, validity_label
from app.tools.registry import registry, tool_error

logger = logging.getLogger(__name__)
REGISTERED = False

MAX_LIMIT = 20
DEFAULT_LIMIT = 6


def normalize_limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _json_list(raw: Any) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return [str(x) for x in val] if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []


def _evidence(raw: Any) -> dict | None:
    if not raw:
        return None
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(val, dict):
        return None
    return {
        "quote": str(val.get("quote") or "")[:200],
        "reason": str(val.get("reason") or "")[:120],
        "source_kind": val.get("source_kind"),
        "checked_at": val.get("checked_at"),
        "suggested_correction": str(val.get("suggested_correction") or "")[:500] or None,
    }


async def search_memory(
    db: AsyncSession, user_id: str, query: str, *,
    kind: str = "all", limit: int = DEFAULT_LIMIT, history: bool = False,
    include_evidence: bool = False,
) -> dict:
    q = str(query or "").strip()
    if not q:
        return {"error": "query is required"}
    if not user_id:
        return {"error": "user context required"}
    lim = normalize_limit(limit)
    escaped = q[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    out: dict[str, Any] = {"query": q, "history": bool(history),
                           "concepts": [], "episodes": [], "pages": []}
    filters = "" if history else (
        " AND valid_to IS NULL AND status = 'active'"
        " AND (verification_state IS NULL OR verification_state != 'contradicted')"
    )
    if kind in ("all", "concept"):
        rows = (await db.execute(
            text(f"""SELECT id, canonical_name, description_short, aliases,
                            source_message_ids, verification_state, last_verified_at,
                            created_at, valid_to, invalid_reason, superseded_by,
                            verification_evidence, status, needs_review
                     FROM memory_concepts
                     WHERE user_id = :uid
                       AND (canonical_name ILIKE :q ESCAPE '\\' OR description_full ILIKE :q ESCAPE '\\'
                            OR aliases ILIKE :q ESCAPE '\\'){filters}
                     ORDER BY importance DESC, weight DESC, created_at DESC
                     LIMIT :lim"""),
            {"uid": user_id, "q": like, "lim": lim},
        )).fetchall()
        if not rows:
            from app.services.memory_validity_service import (
                phonetic_cache_get, phonetic_cache_put, phonetic_pinyin)
            target = phonetic_pinyin(q)
            if target:
                cache_key = f"c:{user_id}:{target}:{'h' if history else 'a'}"
                cached_ids = phonetic_cache_get(cache_key)
                if cached_ids is not None:
                    # P1-17：缓存命中 → 按 id 直查，不再拉 500 行扫描面。
                    # 评审修复：必须重新套 {filters}——缓存只保证「写入时同口径」，
                    # TTL 内概念被失效/contradict 后，active 口径不得再吐出它。
                    if cached_ids:
                        rows = (await db.execute(
                            text(f"""SELECT id, canonical_name, description_short, aliases,
                                            source_message_ids, verification_state,
                                            last_verified_at, created_at, valid_to,
                                            invalid_reason, superseded_by,
                                            verification_evidence, status, needs_review
                                     FROM memory_concepts
                                     WHERE user_id = :uid AND id = ANY(:ids){filters}
                                     ORDER BY importance DESC, weight DESC, created_at DESC
                                     LIMIT :lim"""),
                            {"uid": user_id, "ids": cached_ids, "lim": lim},
                        )).fetchall()
                    else:
                        rows = []
                else:
                    all_rows = (await db.execute(
                        text(f"""SELECT id, canonical_name, description_short, aliases,
                                        source_message_ids, verification_state, last_verified_at,
                                        created_at, valid_to, invalid_reason, superseded_by,
                                        verification_evidence, status, needs_review
                                 FROM memory_concepts
                                 WHERE user_id = :uid{filters}
                                 ORDER BY importance DESC, weight DESC LIMIT 500"""),
                        {"uid": user_id},
                    )).fetchall()
                    matched = [
                        r for r in all_rows
                        if target in {phonetic_pinyin(r[1]),
                                      *[phonetic_pinyin(a) for a in _json_list(r[3])]}
                    ]
                    phonetic_cache_put(cache_key, [str(r[0]) for r in matched])
                    rows = matched[:lim]
        for r in rows:
            reason = r[9] or (derive_invalid_reason(
                superseded_by=r[10], status=(r[12] if len(r) > 12 else "active"),
                needs_review=bool(r[13]) if len(r) > 13 else False)
                if r[8] else None)
            out["concepts"].append({
                "id": r[0], "name": r[1], "description": r[2],
                "aliases": _json_list(r[3]),
                "source_message_ids": _json_list(r[4]),
                "verification": r[5], "last_verified_at": _iso(r[6]),
                "created_at": _iso(r[7]),
                "validity": validity_label(reason, valid_to=r[8]) if reason else "现行",
                "invalid_reason": reason,
                "superseded_by": r[10],
                **({"evidence": _evidence(r[11])} if include_evidence else {}),
            })
    if kind in ("all", "episode"):
        ep_filters = "" if history else (
            " AND valid_to IS NULL AND superseded_by IS NULL"
            " AND (verification_state IS NULL OR verification_state != 'contradicted')"
        )
        rows = (await db.execute(
            text(f"""SELECT id, narrative, valid_from, verification_state,
                            last_verified_at, invalid_reason, superseded_by,
                            verification_evidence
                     FROM memory_episodes
                     WHERE user_id = :uid AND narrative ILIKE :q{ep_filters}
                     ORDER BY valid_from DESC NULLS LAST LIMIT :lim"""),
            {"uid": user_id, "q": like, "lim": lim},
        )).fetchall()
        for r in rows:
            reason = r[5] or (derive_invalid_reason(superseded_by=r[6])
                              if r[6] else None)
            out["episodes"].append({
                "id": r[0], "narrative": r[1][:300] if r[1] else "",
                "valid_from": _iso(r[2]), "verification": r[3],
                "last_verified_at": _iso(r[4]),
                "invalid_reason": reason,
                **({"evidence": _evidence(r[7])} if include_evidence else {}),
            })
    if kind in ("all", "page"):
        rows = (await db.execute(
            text("""SELECT id, entity_key, title, aliases, summary, importance,
                           concept_ids, last_built_at
                    FROM memory_pages
                    WHERE user_id = :uid
                      AND (title ILIKE :q OR aliases ILIKE :q OR summary ILIKE :q)
                    ORDER BY importance DESC, title ASC LIMIT :lim"""),
            {"uid": user_id, "q": like, "lim": lim},
        )).fetchall()
        for r in rows:
            out["pages"].append({
                "id": r[0], "entity_key": r[1], "title": r[2],
                "aliases": _json_list(r[3]), "summary": r[4],
                "importance": float(r[5] or 0),
                "concept_ids": _json_list(r[6]),
                "last_built_at": _iso(r[7]),
            })
    out["count"] = len(out["concepts"]) + len(out["episodes"]) + len(out["pages"])
    return out


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


async def memory_search(args: dict, **kwargs) -> str:
    user = kwargs.get("user")
    db = kwargs.get("db")
    user_id = getattr(user, "id", None) if user else kwargs.get("user_id")
    if not db or not user_id:
        return tool_error("memory_search requires an authenticated user and DB session")
    kind = str(args.get("kind") or "all").strip().lower()
    if kind not in ("all", "concept", "episode", "page"):
        kind = "all"
    try:
        result = await search_memory(
            db, str(user_id), str(args.get("query") or ""),
            kind=kind, limit=normalize_limit(args.get("limit")),
            history=bool(args.get("history")),
            include_evidence=bool(args.get("include_evidence")),
        )
    except Exception:
        logger.warning("memory_search failed", exc_info=True)
        return tool_error("memory_search failed; retry or use session_search")
    if result.get("error"):
        return tool_error(result["error"])
    # 结果预算：条目已按 limit 限制；超 6000 字时丢弃整条结果而不是截断
    # JSON 字符串（截断会产生非法 JSON，模型会误读）。
    for key in ("pages", "episodes"):
        while len(json.dumps(result, ensure_ascii=False)) > 6000 and result[key]:
            result[key].pop()
    while len(json.dumps(result, ensure_ascii=False)) > 6000 and result["concepts"]:
        result["concepts"].pop()
    result["count"] = sum(len(result[k]) for k in ("concepts", "episodes", "pages"))
    return json.dumps(result, ensure_ascii=False)


def _enabled() -> bool:
    from app.core.config import get_config
    return bool(get_config().memory.get("memory_search_enabled", True))


registry.register(
    name="memory_search",
    toolset="core",
    is_async=True,
    check_fn=_enabled,
    schema={
        "name": "memory_search",
        "description": (
            "Read-only search over long-term memory: concepts (facts/profile), "
            "episodes (narrative events) and entity pages (aliases/wiki index). "
            "Use it when the user asks 'do you remember', when injected memory is "
            "empty or conflicting, or before claiming a memory state. Returns "
            "name/description, validity label, verification state, and source "
            "message ids. Defaults to current (active) memories; history=true "
            "includes merged/retracted/forgotten/expired rows. It never writes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword/entity/name to search."},
                "kind": {
                    "type": "string",
                    "enum": ["all", "concept", "episode", "page"],
                    "description": "Restrict the result type (default all).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Max items per kind (default 6).",
                },
                "history": {
                    "type": "boolean",
                    "description": "Include merged/retracted/forgotten/expired rows.",
                },
                "include_evidence": {
                    "type": "boolean",
                    "description": "Include verify evidence quote/reason (default false).",
                },
            },
            "required": ["query"],
        },
    },
    handler=memory_search,
)
REGISTERED = True
