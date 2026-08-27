# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import math
import time
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_query_embedding_lru: dict[str, list[float]] = {}
_embedding_cache_max_size = 100

# 并发控制：全局最多 10 个并发 embedding 请求
_embed_semaphore = asyncio.Semaphore(10)

# 共享 httpx 客户端（连接池复用）
_http_client: httpx.AsyncClient | None = None

# 熔断器
_circuit_failures: int = 0
_circuit_open_until: float = 0.0
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 60


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=15, max_keepalive_connections=10),
        )
    return _http_client


def _get_embedding_api_base() -> str:
    base = config.memory.get("embedding_api_base", "")
    if base:
        return base.rstrip("/")
    return config.api_base_url.rstrip("/")


def _get_embedding_api_key() -> str:
    key = config.memory.get("embedding_api_key", "")
    if key:
        return key
    return config.api_key or ""


def _get_embedding_dim() -> int:
    # 新鲜 get_config()：SIGHUP reload 后模块级 config 是旧实例（A4.9 round5 复审 Minor #5）
    return int(get_config().memory.get("embedding_dim", 1536))


async def _do_embed(text: str) -> Optional[list[float]]:
    """单条 embedding 调用（内部，需在 semaphore 内调用）。"""
    global _circuit_failures, _circuit_open_until

    if _circuit_failures >= _CIRCUIT_FAIL_THRESHOLD:
        if time.monotonic() < _circuit_open_until:
            return None
        _circuit_failures = 0

    model = config.memory.get("embedding_model", "text-embedding-3-small")
    api_key = _get_embedding_api_key()
    base_url = _get_embedding_api_base()
    client = _get_client()

    try:
        resp = await client.post(
            f"{base_url}/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"input": text, "model": model},
        )
        resp.raise_for_status()
        data = resp.json()
        emb = data["data"][0]["embedding"]
        _circuit_failures = 0
        return emb
    except Exception:
        _circuit_failures += 1
        if _circuit_failures >= _CIRCUIT_FAIL_THRESHOLD:
            _circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SECONDS
            logger.warning("embedding circuit open: %d failures, pausing %ds", _circuit_failures, _CIRCUIT_COOLDOWN_SECONDS)
        return None


async def embed_text(text: str) -> Optional[list[float]]:
    if not text or not text.strip():
        return None
    async with _embed_semaphore:
        return await _do_embed(text)


async def embed_texts(texts: list[str]) -> list[Optional[list[float]]]:
    """批量 embedding，每条受全局 semaphore 控制并发。"""
    if not texts:
        return []
    return await asyncio.gather(*[embed_text(t) for t in texts])


async def embed_text_cached(text: str, cache_key: str) -> Optional[list[float]]:
    global _query_embedding_lru
    if cache_key in _query_embedding_lru:
        val = _query_embedding_lru.pop(cache_key)
        _query_embedding_lru[cache_key] = val
        return val

    emb = await embed_text(text)
    if emb is not None:
        if len(_query_embedding_lru) >= _embedding_cache_max_size:
            oldest = next(iter(_query_embedding_lru))
            del _query_embedding_lru[oldest]
        _query_embedding_lru[cache_key] = emb
    return emb


def _emb_to_pgvector(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


def _emb_from_db(raw) -> Optional[list[float]]:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(v) for v in raw]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [float(v) for v in parsed]
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    try:
        inner = raw.strip().lstrip("[").rstrip("]")
        return [float(v) for v in inner.split(",") if v.strip()]
    except (ValueError, TypeError):
        pass
    return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def find_similar_concepts(
    db: AsyncSession, user_id: str, embedding: list[float], top_k: int = 10,
    include_expired: bool = False, active_concept_count: int = 0,
    bootstrap_threshold: int = 10,
) -> list[dict]:
    emb_str = _emb_to_pgvector(embedding)
    dim = _get_embedding_dim()
    sim_thr = float(config.memory_retrieval.get("embedding_sim_threshold", 0.3) or 0)
    query = text(f"""
        SELECT id, canonical_name, description_short, description_full, weight,
               source_trust, memory_type, activation_strength, status,
               1 - (CAST(:emb AS vector({dim})) <=> embedding) AS similarity
        FROM memory_concepts
        WHERE user_id = :uid
          AND embedding IS NOT NULL
          AND 1 - (CAST(:emb AS vector({dim})) <=> embedding) >= :thr
          AND (
            CASE WHEN :incl_expired THEN TRUE
                 ELSE (
                   (status = 'active' AND activation_strength > 0.05)
                   OR
                   (:in_bootstrap AND status IN ('active','silent'))
                 )
            END
          )
          AND (
            CASE WHEN :incl_expired THEN valid_from <= now()
                 ELSE valid_to IS NULL
            END
          )
        ORDER BY CAST(:emb AS vector({dim})) <=> embedding
        LIMIT :lim
    """)
    result = await db.execute(query, {
        "emb": emb_str,
        "uid": user_id,
        "lim": top_k,
        "thr": sim_thr,
        "incl_expired": include_expired,
        "in_bootstrap": active_concept_count < bootstrap_threshold,
    })
    rows = result.fetchall()
    return [
        {
            "id": r[0], "canonical_name": r[1], "description_short": r[2],
            "description_full": r[3], "weight": r[4], "source_trust": r[5],
            "memory_type": r[6], "activation_strength": r[7], "status": r[8],
            "similarity": float(r[9]),
        }
        for r in rows
    ]


async def find_similar_episodes(
    db: AsyncSession, user_id: str, embedding: list[float], top_k: int = 5,
    include_expired: bool = False, time_start=None, time_end=None,
) -> list[dict]:
    emb_str = _emb_to_pgvector(embedding)
    dim = _get_embedding_dim()
    sim_thr = float(config.memory_retrieval.get("embedding_sim_threshold", 0.3) or 0)
    query = text(f"""
        SELECT id, narrative, valid_from, source_concept_ids,
               1 - (CAST(:emb AS vector({dim})) <=> embedding) AS similarity
        FROM memory_episodes
        WHERE user_id = :uid
          AND embedding IS NOT NULL
          AND 1 - (CAST(:emb AS vector({dim})) <=> embedding) >= :thr
          AND (
            CASE WHEN :incl_expired THEN valid_from <= now()
                 ELSE valid_to IS NULL
            END
          )
          AND (CAST(:ts AS TIMESTAMP) IS NULL OR valid_from >= CAST(:ts AS TIMESTAMP))
          AND (CAST(:te AS TIMESTAMP) IS NULL OR valid_from <= CAST(:te AS TIMESTAMP))
        ORDER BY CAST(:emb AS vector({dim})) <=> embedding
        LIMIT :lim
    """)
    result = await db.execute(query, {
        "emb": emb_str,
        "uid": user_id,
        "lim": top_k,
        "thr": sim_thr,
        "incl_expired": include_expired,
        "ts": time_start,
        "te": time_end,
    })
    rows = result.fetchall()
    return [
        {
            "id": r[0], "narrative": r[1], "valid_from": r[2],
            "source_concept_ids": r[3], "similarity": float(r[4]),
        }
        for r in rows
    ]


async def find_similar_subconscious_units(
    db: AsyncSession, user_id: str, embedding: list[float], top_k: int = 10,
    time_start=None, time_end=None,
) -> list[dict]:
    emb_str = _emb_to_pgvector(embedding)
    dim = _get_embedding_dim()
    sim_thr = float(config.memory_retrieval.get("embedding_sim_threshold", 0.3) or 0)
    query = text(f"""
        SELECT id, raw_text, source_ids, created_at, recurrence_count,
               1 - (CAST(:emb AS vector({dim})) <=> embedding) AS similarity
        FROM subconscious_log
        WHERE user_id = :uid
          AND created_at >= now() - interval '30 days'
          AND embedding IS NOT NULL
          AND 1 - (CAST(:emb AS vector({dim})) <=> embedding) >= :thr
          AND (CAST(:ts AS TIMESTAMP) IS NULL OR created_at >= CAST(:ts AS TIMESTAMP))
          AND (CAST(:te AS TIMESTAMP) IS NULL OR created_at <= CAST(:te AS TIMESTAMP))
        ORDER BY CAST(:emb AS vector({dim})) <=> embedding
        LIMIT :lim
    """)
    result = await db.execute(query, {
        "emb": emb_str,
        "uid": user_id,
        "lim": top_k,
        "thr": sim_thr,
        "ts": time_start,
        "te": time_end,
    })
    rows = result.fetchall()
    return [
        {
            "id": r[0], "raw_text": r[1], "source_ids": r[2],
            "created_at": r[3], "recurrence_count": r[4], "similarity": float(r[5]),
        }
        for r in rows
    ]


async def find_neighbors_for_unit(
    db: AsyncSession, user_id: str, unit_embedding: list[float],
    unit_id: str, unit_created_at, top_k: int = 5,
) -> list[dict]:
    emb_str = _emb_to_pgvector(unit_embedding)
    dim = _get_embedding_dim()
    query = text(f"""
        SELECT id, raw_text, embedding,
               1 - (CAST(:emb AS vector({dim})) <=> embedding) AS similarity
        FROM subconscious_log
        WHERE user_id = :uid
          AND id != :eid
          AND promoted = FALSE
          AND created_at < :ts
          AND embedding IS NOT NULL
        ORDER BY CAST(:emb AS vector({dim})) <=> embedding
        LIMIT :lim
    """)
    result = await db.execute(query, {
        "emb": emb_str,
        "uid": user_id,
        "eid": unit_id,
        "ts": unit_created_at,
        "lim": top_k,
    })
    rows = result.fetchall()
    return [
        {"id": r[0], "raw_text": r[1], "embedding_raw": r[2], "similarity": float(r[3])}
        for r in rows
    ]


async def _probe_main_provider() -> tuple[bool, Optional[int]]:
    cfg = get_config()
    base_url = (cfg.memory.get("embedding_api_base") or "").rstrip("/")
    api_key = cfg.memory.get("embedding_api_key") or ""
    model = cfg.memory.get("embedding_model", "text-embedding-3-small")

    if not base_url:
        from app.services.provider_router import get_provider_router
        try:
            return await get_provider_router().embedding_available("default")
        except Exception:
            logger.exception("embedding provider probe failed")
            return False, None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"input": "probe", "model": model},
            )
            resp.raise_for_status()
            data = resp.json()
            emb = data["data"][0]["embedding"]
            return True, len(emb)
    except Exception:
        logger.exception("embedding provider probe failed (base=%s)", base_url)
        return False, None


async def _db_vector_dim_matches(expected_dim: int) -> bool:
    from app.db.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'memory_concepts'::regclass AND attname = 'embedding'"
            ))
            fmt = r.scalar()
            if not fmt:
                return False
            return fmt == f"vector({expected_dim})"
    except Exception:
        logger.exception("vector dim check failed")
        return False


async def probe_memory_embedding_on_startup() -> bool:
    """§9.11 启动探测：主 provider 无 embedding 端点或维度不匹配时禁用 memory 子系统。

    fail-fast 原则：维度静默不匹配会 corrupt cosine sim，宁可禁用也不带病运行。
    禁用状态写入 memory_runtime_state kill-switch（跨 Config 实例一致，
    SIGHUP reload 不丢失）；本函数每次调用取新鲜 get_config() 以支持重探测。
    返回 True = 环境可用（SIGHUP 重探测据此先测后启，无半开窗口）；
    返回 False = 已禁用或配置未开启。
    """
    from app.services.memory_runtime_state import disable_memory
    cfg = get_config()
    if not cfg.memory.get("enabled"):
        return False
    if not cfg.memory.get("embedding_provider_check_on_startup", True):
        return True

    available, dim = await _probe_main_provider()
    if not available:
        disable_memory(
            "主 provider 无 embedding 端点（/embeddings 探测失败）；"
            "请配置 [memory] embedding_api_base/embedding_api_key 指向支持 embedding 的 provider")
        return False

    expected = _get_embedding_dim()
    if dim and dim != expected:
        if not cfg.memory.get("embedding_dim_override"):
            disable_memory(
                f"主 provider embedding 维度 {dim} ≠ 配置 embedding_dim {expected}；"
                "须在 config 显式设置 embedding_dim_override=true 并 ALTER TABLE 重建 vector 列")
            return False
        if not await _db_vector_dim_matches(expected):
            disable_memory(
                f"embedding_dim_override=true 但 DB memory_concepts.embedding 维度与配置 {expected} "
                "不一致（fail-fast）")
            return False
    return True
