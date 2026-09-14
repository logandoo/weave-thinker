# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import MemoryCluster, ConceptClusterMember, ConceptRelation
from app.services.memory_embedding_service import (
    embed_text, _emb_to_pgvector, _emb_from_db, cosine_similarity,
    _get_embedding_dim, _get_embedding_model,
)

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


async def _update_cluster_embedding(db: AsyncSession, cluster_id: str) -> int:
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
        return 0

    # A1/N1（2026-09-14）：维度必须与端点/DB 一致——旧实现读
    # config.memory["embedding_dim"]（键不存在→默认 1536），与 1024 维端点
    # 永不匹配 → count=0 → 集群 embedding 永远 NULL（本地 20/20 实证）。
    # 统一走 _get_embedding_dim()（端点 extra.dim 优先）。
    dim = _get_embedding_dim()
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
        # DC2：记录向量溯源（跨模型混空间防护）
        cluster.embedding_model = _get_embedding_model()
        cluster.member_count = count
    # A4.9 Minor：返回实际写入的成员向量数（0 = 未写入，调用方据此区分 skipped）
    return count


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
    edge_source: str = "llm",
) -> str:
    rid = str(uuid.uuid4())
    relation = ConceptRelation(
        id=rid, user_id=user_id, source_id=source_id, target_id=target_id,
        relation_type=relation_type, description=description, weight=weight,
        edge_source=edge_source,
    )
    db.add(relation)
    await db.flush()
    return rid


# D1（2026-09-14）：确定性边类型 + 读侧白名单基础集（DC5）
DETERMINISTIC_EDGE_TYPE = "co_occurs"
BASE_EDGE_TYPES = ("causal", "temporal", "contradicts", "supports", "part_of")


def edge_read_whitelist() -> list[str] | None:
    """读侧白名单（D1/DC5）：未启用白名单 → None（保持旧的全类型读语义）。

    启用白名单时只放行 LLM 基础类型；确定性边仅在 deterministic_edges_enabled
    开启时加入——保证「开确定性边」是显式决策，不会因存量数据静默改变检索。
    """
    ret_cfg = config.memory_retrieval
    if not ret_cfg.get("edge_read_whitelist_enabled", False):
        return None
    types = list(BASE_EDGE_TYPES)
    if ret_cfg.get("deterministic_edges_enabled", False):
        types.append(DETERMINISTIC_EDGE_TYPE)
    return types


async def build_deterministic_edges(
    db: AsyncSession, user_id: str, concept_ids: list[str],
    episode_id: str | None = None, source_unit_ids: list[str] | None = None,
    max_edges: int = 20,
) -> int:
    """D1：同源共现的确定性构边（无 LLM）。

    幂等：同向或反向已有 `co_occurs` 即跳过（重跑零新增）；有界（≤ max_edges）。
    仅当 deterministic_edges_enabled 开启时由调用方触发（此处再兜底一次）。
    """
    if not config.memory_retrieval.get("deterministic_edges_enabled", False):
        return 0
    ids = sorted({cid for cid in (concept_ids or []) if cid})
    if len(ids) < 2:
        return 0
    created = 0
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if created >= max_edges:
                return created
            exists = await db.execute(
                text("""
                    SELECT 1 FROM concept_relations
                    WHERE user_id = :u AND relation_type = :rt
                      AND ((source_id = :a AND target_id = :b)
                           OR (source_id = :b AND target_id = :a))
                    LIMIT 1
                """),
                {"u": user_id, "rt": DETERMINISTIC_EDGE_TYPE, "a": ids[i], "b": ids[j]},
            )
            if exists.fetchone():
                continue
            await create_relation(
                db, user_id, ids[i], ids[j], DETERMINISTIC_EDGE_TYPE,
                description=f"同源共现（episode {episode_id or 'n/a'}）"[:500],
                weight=0.4, edge_source="deterministic",
            )
            created += 1
    return created


async def get_neighbors(db: AsyncSession, concept_id: str, min_weight: float = 0.3,
                        allowed_types: list[str] | None = None) -> list[dict]:
    type_clause = "AND relation_type = ANY(:types) " if allowed_types else ""
    params = {"id": concept_id, "mw": min_weight}
    if allowed_types:
        params["types"] = list(allowed_types)
    result = await db.execute(
        text(
            "SELECT target_id, relation_type, weight FROM concept_relations "
            f"WHERE source_id = :id AND weight >= :mw {type_clause}"
            "UNION ALL "
            "SELECT source_id, relation_type, weight FROM concept_relations "
            f"WHERE target_id = :id AND weight >= :mw {type_clause}"
        ),
        params,
    )
    return [{"id": r[0], "relation_type": r[1], "weight": r[2]} for r in result.fetchall()]
