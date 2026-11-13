# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Entity pages / short wiki representation layer (TODO 2026-11-08 C4/C8).

Pages are *derived* from concepts: aliases are unioned so homophone variants with
the same leading character (洛瞳/洛彤/洛童) land on one page; genuinely distinct
entities such as 山西/陕西 or 王伟/汪伟 stay separate. Pages are only an index +
pointer; the full concept text stays in memory_concepts and is read on demand
by `memory_search` or `memory_page_read`.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import MemoryPage, MemoryPageLink
from app.services.memory_validity_service import entity_keys_for, normalize_entity_key

logger = logging.getLogger(__name__)
config = get_config()

PAGE_SUMMARY_MAX = 500


def _parse_list(raw: Any) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
        return val if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []


class _DSU:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def group_entity_concepts(concepts: list[dict]) -> list[dict]:
    """Pure grouping: concepts sharing a normalized name/alias become one page."""
    dedup: dict[str, dict] = {}
    for c in concepts or []:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        if cid not in dedup:
            dedup[cid] = dict(c)
        else:
            # Duplicate rows for the same id: keep the higher importance and
            # union aliases instead of emitting a duplicate concept id.
            if float(c.get("importance") or 0) > float(dedup[cid].get("importance") or 0):
                merged = dict(c)
                merged["aliases"] = list(dict.fromkeys(
                    list(dedup[cid].get("aliases") or []) + list(c.get("aliases") or [])))
                dedup[cid] = merged
            else:
                dedup[cid]["aliases"] = list(dict.fromkeys(
                    list(dedup[cid].get("aliases") or []) + list(c.get("aliases") or [])))

    dsu = _DSU()
    key_to_ids: dict[str, list[str]] = {}
    for cid, c in dedup.items():
        dsu.add(cid)
        keys = entity_keys_for(str(c.get("name") or ""), c.get("aliases") or [])
        for key in keys:
            key_to_ids.setdefault(key, []).append(cid)
    for ids in key_to_ids.values():
        first = ids[0]
        for other in ids[1:]:
            dsu.union(first, other)

    groups: dict[str, list[dict]] = {}
    for cid, c in dedup.items():
        groups.setdefault(dsu.find(cid), []).append(c)
    out: list[dict] = []
    for members in groups.values():
        members.sort(key=lambda c: float(c.get("importance") or 0), reverse=True)
        title = str(members[0].get("name") or "").strip() or "未命名实体"
        aliases: list[str] = []
        descriptions: list[str] = []
        for c in members:
            for raw in [c.get("name"), *(c.get("aliases") or [])]:
                s = str(raw or "").strip()
                if s and s != title and s not in aliases:
                    aliases.append(s)
            desc = str(c.get("desc") or "").strip()
            if desc and desc not in descriptions:
                descriptions.append(desc)
        summary = "；".join(descriptions[:3])[:PAGE_SUMMARY_MAX]
        keys: list[str] = []
        for c in members:
            for key in entity_keys_for(str(c.get("name") or ""), c.get("aliases") or []):
                if key and key not in keys:
                    keys.append(key)
        out.append({
            "entity_key": normalize_entity_key(title) or (keys[0] if keys else ""),
            "title": title,
            "aliases": aliases[:50],
            "summary": summary,
            "concept_ids": [str(c.get("id")) for c in members],
            "importance": float(members[0].get("importance") or 0),
            "keys": keys,
        })
    return out


async def _load_concepts(db: AsyncSession, user_id: str) -> list[dict]:
    rows = (await db.execute(
        text("""SELECT id, canonical_name, aliases, description_short, importance
                FROM memory_concepts
                WHERE user_id = :uid AND valid_to IS NULL AND status = 'active'
                  AND (verification_state IS NULL OR verification_state != 'contradicted')
                ORDER BY importance DESC, weight DESC"""),
        {"uid": user_id},
    )).fetchall()
    return [{
        "id": r[0], "name": r[1], "aliases": _parse_list(r[2]),
        "desc": r[3] or "", "importance": float(r[4] or 0),
    } for r in rows]


async def build_pages(db: AsyncSession, user_id: str) -> dict:
    """Rebuild this user's entity pages and page-level relation edges.

    Upsert by `(user_id, entity_key)` keeps page ids stable across nights;
    pages whose entity disappeared are removed with their edges.
    """
    concepts = await _load_concepts(db, user_id)
    groups = group_entity_concepts(concepts)
    now = datetime.utcnow()
    existing = {
        p.entity_key: p for p in (await db.execute(
            select(MemoryPage).where(MemoryPage.user_id == user_id)
        )).scalars().all()
    }
    seen_keys: list[str] = []
    page_by_key: dict[str, MemoryPage] = {}
    for g in groups:
        key = g["entity_key"]
        if not key:
            continue
        seen_keys.append(key)
        page = existing.get(key)
        if page is None:
            page = MemoryPage(id=str(uuid.uuid4()), user_id=user_id, entity_key=key)
            db.add(page)
        page.title = g["title"]
        page.aliases = json.dumps(g["aliases"], ensure_ascii=False)
        page.summary = g["summary"]
        page.concept_ids = json.dumps(g["concept_ids"], ensure_ascii=False)
        page.importance = g["importance"]
        page.last_built_at = now
        page.updated_at = now
        page_by_key[key] = page
    await db.flush()

    # Remove pages whose concepts vanished (derived data, safe to drop).
    if seen_keys:
        await db.execute(
            text("DELETE FROM memory_pages WHERE user_id = :uid AND entity_key != ALL(:keys)"),
            {"uid": user_id, "keys": seen_keys},
        )
    else:
        await db.execute(text("DELETE FROM memory_pages WHERE user_id = :uid"),
                         {"uid": user_id})
    # 评审修复（P1-17）：重建改变了拼音匹配集，立即失效该用户扫描缓存。
    from app.services.memory_validity_service import phonetic_cache_invalidate
    phonetic_cache_invalidate(user_id)

    concept_to_page: dict[str, str] = {}
    for page in page_by_key.values():
        for cid in _parse_list(page.concept_ids):
            concept_to_page[str(cid)] = page.id

    await db.execute(text("DELETE FROM memory_page_links WHERE user_id = :uid"),
                     {"uid": user_id})
    edges: dict[tuple[str, str, str], float] = {}
    if concept_to_page:
        rels = (await db.execute(
            text("""SELECT source_id, target_id, relation_type, weight
                    FROM concept_relations WHERE user_id = :uid"""),
            {"uid": user_id},
        )).fetchall()
        for src, tgt, rtype, weight in rels:
            sp, tp = concept_to_page.get(str(src)), concept_to_page.get(str(tgt))
            if not sp or not tp or sp == tp:
                continue
            key = (sp, tp, str(rtype or "related"))
            edges[key] = max(edges.get(key, 0.0), float(weight or 0.5))
        for (sp, tp, rtype), weight in edges.items():
            db.add(MemoryPageLink(
                id=str(uuid.uuid4()), user_id=user_id,
                source_page_id=sp, target_page_id=tp,
                relation_type=rtype, weight=weight))
    await db.flush()
    return {
        "concepts": len(concepts),
        "pages": len(page_by_key),
        "links": len(edges) if concept_to_page else 0,
        "built_at": now.isoformat(),
    }


async def get_index_pages(db: AsyncSession, user_id: str, limit: int = 8) -> list[dict]:
    """C6：注入层“记忆索引”数据源（按页重要度，不按查询）。"""
    rows = (await db.execute(
        text("""SELECT id, entity_key, title, aliases, summary, concept_ids,
                       importance, last_built_at
                FROM memory_pages WHERE user_id = :uid
                ORDER BY importance DESC, title ASC LIMIT :lim"""),
        {"uid": user_id, "lim": max(1, min(int(limit or 8), 20))},
    )).fetchall()
    return [{
        "id": r[0], "entity_key": r[1], "title": r[2],
        "aliases": _parse_list(r[3]), "summary": r[4],
        "concept_ids": _parse_list(r[5]),
        "importance": float(r[6] or 0),
        "last_built_at": r[7].isoformat() if r[7] else None,
    } for r in rows]


async def search_pages(db: AsyncSession, user_id: str, query: str, limit: int = 6) -> list[dict]:
    """Read API/tool search over page titles/aliases/summary (C2/C8)."""
    q = f"%{str(query or '').strip()[:100]}%"
    rows = (await db.execute(
        text("""SELECT id, entity_key, title, aliases, summary, concept_ids,
                       importance, last_built_at
                FROM memory_pages WHERE user_id = :uid
                  AND (title ILIKE :q OR aliases ILIKE :q OR summary ILIKE :q)
                ORDER BY importance DESC LIMIT :lim"""),
        {"uid": user_id, "q": q, "lim": max(1, min(int(limit or 6), 20))},
    )).fetchall()
    if not rows:
        # C8：同音别名（洛瞳/洛彤/洛童）回退到 pinyin 键匹配。
        # P1-17（2026-11-08）：扫描结果按 (user, target) TTL 缓存，命中走 id 直查。
        from app.services.memory_validity_service import (
            phonetic_cache_get, phonetic_cache_put, phonetic_pinyin)
        target = phonetic_pinyin(str(query or ""))
        if target:
            lim = max(1, min(int(limit or 6), 20))
            cache_key = f"p:{user_id}:{target}"
            cached_ids = phonetic_cache_get(cache_key)
            if cached_ids is not None:
                rows = (await db.execute(
                    text("""SELECT id, entity_key, title, aliases, summary, concept_ids,
                                   importance, last_built_at
                            FROM memory_pages WHERE user_id = :uid AND id = ANY(:ids)
                            ORDER BY importance DESC LIMIT :lim"""),
                    {"uid": user_id, "ids": cached_ids, "lim": lim},
                )).fetchall() if cached_ids else []
            else:
                all_rows = (await db.execute(
                    text("""SELECT id, entity_key, title, aliases, summary, concept_ids,
                                   importance, last_built_at
                            FROM memory_pages WHERE user_id = :uid
                            ORDER BY importance DESC LIMIT 300"""),
                    {"uid": user_id},
                )).fetchall()
                matched = [
                    r for r in all_rows
                    if target in {phonetic_pinyin(r[2]),
                                  *[phonetic_pinyin(a) for a in _parse_list(r[3])]}
                ]
                phonetic_cache_put(cache_key, [str(r[0]) for r in matched])
                rows = matched[:lim]
    return [{
        "id": r[0], "entity_key": r[1], "title": r[2],
        "aliases": _parse_list(r[3]), "summary": r[4],
        "concept_ids": _parse_list(r[5]),
        "importance": float(r[6] or 0),
        "last_built_at": r[7].isoformat() if r[7] else None,
    } for r in rows]
