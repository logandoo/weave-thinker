# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import MemoryEpisode
from app.services.memory_embedding_service import embed_text, cosine_similarity, _emb_from_db

config = get_config()
logger = logging.getLogger(__name__)


async def create_episode(
    db: AsyncSession, user_id: str, narrative: str, valid_from: datetime,
    source_unit_ids: list[str], source_concept_ids: list[str] | None = None,
) -> str:
    eid = str(uuid.uuid4())
    narr = narrative[:5000]
    emb = await embed_text(narr)

    episode = MemoryEpisode(
        id=eid,
        user_id=user_id,
        narrative=narr,
        source_unit_ids=json.dumps(source_unit_ids, ensure_ascii=False),
        source_concept_ids=json.dumps(source_concept_ids or [], ensure_ascii=False),
        valid_from=valid_from or datetime.utcnow(),
        embedding=emb,
    )
    db.add(episode)
    await db.flush()
    return eid


async def merge_episode(
    db: AsyncSession, episode_id: str, new_narrative: str, new_source_unit_ids: list[str],
) -> bool:
    episode = await db.get(MemoryEpisode, episode_id)
    if not episode:
        return False

    old_ids = _parse_json_array(episode.source_unit_ids)
    merged_ids = list(dict.fromkeys(old_ids + new_source_unit_ids))
    episode.source_unit_ids = json.dumps(merged_ids, ensure_ascii=False)

    if new_narrative:
        episode.narrative = new_narrative[:5000]
        emb = await embed_text(new_narrative)
        if emb:
            episode.embedding = emb

    episode.updated_at = datetime.utcnow()
    await db.flush()
    return True


async def merge_first(
    db: AsyncSession, user_id: str, narrative: str,
    source_unit_ids: list[str],
) -> Optional[str]:
    threshold = float(config.memory_episodic.get("merge_first_threshold", 0.85))
    emb = await embed_text(narrative)
    if not emb:
        return None

    from app.services.memory_embedding_service import find_similar_episodes
    candidates = await find_similar_episodes(db, user_id, emb, top_k=1)
    if candidates and candidates[0]["similarity"] >= threshold:
        existing_id = candidates[0]["id"]
        await merge_episode(db, existing_id, narrative, source_unit_ids)
        return existing_id

    return None


async def invalidate_episode(db: AsyncSession, episode_id: str, superseded_by: str | None = None) -> bool:
    episode = await db.get(MemoryEpisode, episode_id)
    if not episode:
        return False
    episode.valid_to = datetime.utcnow()
    if superseded_by:
        episode.superseded_by = superseded_by
    episode.updated_at = datetime.utcnow()
    await db.flush()
    return True


def _parse_json_array(raw: str | None) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
