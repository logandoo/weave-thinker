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
from app.services.memory_embedding_service import embed_text, cosine_similarity, _emb_from_db, _get_embedding_model

config = get_config()
logger = logging.getLogger(__name__)


async def create_episode(
    db: AsyncSession, user_id: str, narrative: str, valid_from: datetime,
    source_unit_ids: list[str], source_concept_ids: list[str] | None = None,
    participants: list[str] | None = None, locations: list[str] | None = None,
) -> str:
    eid = str(uuid.uuid4())
    narr = narrative[:5000]
    # B8 shadow（2026-09-14）：episode 叙事过去不扫注入——先记录不拒绝
    try:
        from app.services.memory_security import scan_injection as _scan
        _hit = _scan(narr)
        if _hit:
            logger.warning("memory shadow scan hit (episode) user=%s: %s", user_id, _hit)
    except Exception:
        logger.debug("episode shadow scan failed", exc_info=True)
    emb = await embed_text(narr)

    episode = MemoryEpisode(
        id=eid,
        user_id=user_id,
        narrative=narr,
        source_unit_ids=json.dumps(source_unit_ids, ensure_ascii=False),
        source_concept_ids=json.dumps(source_concept_ids or [], ensure_ascii=False),
        # D1：P/L/T 线索（可空；非列表输入忽略）
        participants=_json_list_or_none(participants),
        locations=_json_list_or_none(locations),
        valid_from=valid_from or datetime.utcnow(),
        embedding=emb,
        embedding_model=_get_embedding_model() if emb else None,
    )
    db.add(episode)
    await db.flush()
    # B1（2026-09-14）：episode BM25 索引增量维护（旧实现首建后冻结，
    # 新 episode 在进程生命周期内对 stage1 词法不可见）
    try:
        from app.services.memory_bm25 import update_episode_index
        update_episode_index(user_id, eid, narr)
    except Exception:
        logger.debug("episode bm25 index update failed", exc_info=True)
    return eid


async def merge_episode(
    db: AsyncSession, episode_id: str, new_narrative: str, new_source_unit_ids: list[str],
    participants: list[str] | None = None, locations: list[str] | None = None,
) -> bool:
    episode = await db.get(MemoryEpisode, episode_id)
    if not episode:
        return False

    old_ids = _parse_json_array(episode.source_unit_ids)
    merged_ids = list(dict.fromkeys(old_ids + new_source_unit_ids))
    episode.source_unit_ids = json.dumps(merged_ids, ensure_ascii=False)

    # D1：P/L/T 线索并集合并（旧行为无该列时为 None，保持 None 不引入空串）
    if participants:
        merged_p = list(dict.fromkeys(_parse_json_array(episode.participants) + list(participants)))
        episode.participants = json.dumps(merged_p, ensure_ascii=False)
    if locations:
        merged_l = list(dict.fromkeys(_parse_json_array(episode.locations) + list(locations)))
        episode.locations = json.dumps(merged_l, ensure_ascii=False)

    if new_narrative:
        episode.narrative = new_narrative[:5000]
        emb = await embed_text(new_narrative)
        if emb:
            episode.embedding = emb
            # B7/DC2：仅在新向量可用时更新溯源
            episode.embedding_model = _get_embedding_model()

    episode.updated_at = datetime.utcnow()
    await db.flush()
    # B1：合并后的叙事更新进 BM25 索引
    try:
        from app.services.memory_bm25 import update_episode_index
        update_episode_index(episode.user_id, episode_id, episode.narrative)
    except Exception:
        logger.debug("episode bm25 index update failed", exc_info=True)
    return True


async def merge_first(
    db: AsyncSession, user_id: str, narrative: str,
    source_unit_ids: list[str],
    participants: list[str] | None = None, locations: list[str] | None = None,
) -> Optional[str]:
    threshold = float(config.memory_episodic.get("merge_first_threshold", 0.85))
    emb = await embed_text(narrative)
    if not emb:
        return None

    from app.services.memory_embedding_service import find_similar_episodes
    candidates = await find_similar_episodes(db, user_id, emb, top_k=1)
    if candidates and candidates[0]["similarity"] >= threshold:
        existing_id = candidates[0]["id"]
        await merge_episode(db, existing_id, narrative, source_unit_ids,
                            participants=participants, locations=locations)
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


def _json_list_or_none(value) -> str | None:
    """D1：列表 → JSON 字符串；非列表/空列表 → None（不写空串）。"""
    if not isinstance(value, list):
        return None
    items = [str(v) for v in value if str(v).strip()]
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)
