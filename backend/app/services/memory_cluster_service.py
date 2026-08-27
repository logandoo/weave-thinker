# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import MemoryCluster, ConceptClusterMember, ConceptRelation
from app.services.memory_embedding_service import embed_text, _emb_to_pgvector, _emb_from_db, cosine_similarity

config = get_config()
logger = logging.getLogger(__name__)


async def create_cluster(db: AsyncSession, user_id: str, name: str, summary: str = "") -> str:
    cid = str(uuid.uuid4())
    cluster = MemoryCluster(
        id=cid, user_id=user_id, name=name, summary=summary,
        weight=0.5, member_count=0,
    )
    db.add(cluster)
    await db.flush()
    return cid


async def add_concept_to_cluster(db: AsyncSession, concept_id: str, cluster_id: str) -> bool:
    await db.execute(
        text("INSERT INTO concept_cluster_members (concept_id, cluster_id) VALUES (:cid, :clid) ON CONFLICT DO NOTHING"),
        {"cid": concept_id, "clid": cluster_id},
    )
    await db.execute(
        text("UPDATE memory_clusters SET member_count = (SELECT COUNT(*) FROM concept_cluster_members WHERE cluster_id = :clid), updated_at = NOW() WHERE id = :clid"),
        {"clid": cluster_id},
    )
    await _update_cluster_embedding(db, cluster_id)
    return True


async def remove_concept_from_cluster(db: AsyncSession, concept_id: str, cluster_id: str) -> bool:
    await db.execute(
        text("DELETE FROM concept_cluster_members WHERE concept_id = :cid AND cluster_id = :clid"),
        {"cid": concept_id, "clid": cluster_id},
    )
    await db.execute(
        text("UPDATE memory_clusters SET member_count = (SELECT COUNT(*) FROM concept_cluster_members WHERE cluster_id = :clid), updated_at = NOW() WHERE id = :clid"),
        {"clid": cluster_id},
    )
    await _update_cluster_embedding(db, cluster_id)
    return True


async def _update_cluster_embedding(db: AsyncSession, cluster_id: str) -> None:
    cluster = await db.get(MemoryCluster, cluster_id)
    if not cluster:
        return
    result = await db.execute(
        text("SELECT mc.embedding FROM memory_concepts mc JOIN concept_cluster_members ccm ON mc.id = ccm.concept_id WHERE ccm.cluster_id = :clid AND mc.embedding IS NOT NULL"),
        {"clid": cluster_id},
    )
    rows = result.fetchall()
    if not rows:
        cluster.embedding = None
        return

    dim = int(config.memory.get("embedding_dim", 1536))
    mean = [0.0] * dim
    count = 0
    for row in rows:
        emb = _emb_from_db(row[0])
        if emb and len(emb) == dim:
            for i in range(dim):
                mean[i] += emb[i]
            count += 1
    if count > 0:
        mean = [v / count for v in mean]
        cluster.embedding = mean
        cluster.member_count = count


async def get_clusters_for_concepts(db: AsyncSession, concept_ids: list[str]) -> list[dict]:
    if not concept_ids:
        return []
    result = await db.execute(
        text("""
            SELECT DISTINCT mc.id, mc.name, mc.summary, mc.weight
            FROM memory_clusters mc
            JOIN concept_cluster_members ccm ON mc.id = ccm.cluster_id
            WHERE ccm.concept_id = ANY(:ids)
        """),
        {"ids": concept_ids},
    )
    return [
        {"id": r[0], "name": r[1], "summary": r[2], "weight": r[3]}
        for r in result.fetchall()
    ]


async def create_relation(
    db: AsyncSession, user_id: str, source_id: str, target_id: str,
    relation_type: str, description: str = "", weight: float = 0.5,
) -> str:
    rid = str(uuid.uuid4())
    relation = ConceptRelation(
        id=rid, user_id=user_id, source_id=source_id, target_id=target_id,
        relation_type=relation_type, description=description, weight=weight,
    )
    db.add(relation)
    await db.flush()
    return rid


async def get_neighbors(db: AsyncSession, concept_id: str, min_weight: float = 0.3) -> list[dict]:
    result = await db.execute(
        text("SELECT target_id, relation_type, weight FROM concept_relations WHERE source_id = :id AND weight >= :mw UNION ALL SELECT source_id, relation_type, weight FROM concept_relations WHERE target_id = :id AND weight >= :mw"),
        {"id": concept_id, "mw": min_weight},
    )
    return [{"id": r[0], "relation_type": r[1], "weight": r[2]} for r in result.fetchall()]
