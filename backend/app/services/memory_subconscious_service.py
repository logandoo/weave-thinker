# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import SubconsciousLog, MemoryEpisode
from app.services.memory_embedding_service import embed_text, _emb_to_pgvector
from app.services.memory_security import scrub_pii

config = get_config()
logger = logging.getLogger(__name__)

# §5.1.a 提取输入来源与信任级映射：unit → 隐式 source_trust 标签，升级时传入 LLM
_UNIT_SOURCE_TRUST = {
    "message": "user_stated",
    "note": "user_authored",
    "file_memory": "user_authored",      # USER.md 默认；AGENT.md 经 source_id 覆盖为 agent_inferred
}

_GREETING_TRIVIAL_PROMPT = (
    "你是消息琐碎性判断器。判断以下用户消息中哪些是寒暄/无实质内容的消息"
    "（如：你好/谢谢/好的/嗯/再见/辛苦啦 这类不携带任何可学习信息的短消息）。\n"
    "只输出JSON：{\"trivial_indices\": [0, 3]} —— 输出属于寒暄/无实质内容的消息的下标列表，"
    "下标对应消息列表的顺序。若都没有，输出 {\"trivial_indices\": []}。\n"
    "只输出JSON，不要输出其他内容。"
)


async def _filter_trivial_texts(texts: list[str]) -> set[int]:
    """LLM-judged triviality filter (agentic principle — the former greeting
    regexes could not generalize to arbitrary small-talk phrasings).

    Batched: one LLM call per ≤200-text chunk (bounded prompt size — a large
    post-absence backlog must not yield an unbounded prompt). LLM failure →
    empty set (ingest everything — permissive and lossless; noise units
    carry low weight).
    """
    if not texts:
        return set()
    from app.services.agentic_judge import judge_json

    # Structural pre-strip: conversation units carry "用户: " prefix and a
    # paired assistant reply — triviality is judged on the USER part only.
    check_texts = [
        (t.split("\n助手:", 1)[0].removeprefix("用户: ").strip() if isinstance(t, str) else "")
        for t in texts
    ]
    trivial: set[int] = set()
    for i, ct in enumerate(check_texts):
        if not ct or len(ct) < 5:
            # Structural length rule (a fact, not a judgment): too short to
            # carry any learnable information.
            trivial.add(i)
    pending = [i for i, ct in enumerate(check_texts) if i not in trivial]
    if not pending:
        return trivial
    try:
        for start in range(0, len(pending), 200):
            chunk = pending[start:start + 200]
            numbered = "\n".join(f"{i}. {check_texts[i][:120]}" for i in chunk)
            parsed = await judge_json(
                _GREETING_TRIVIAL_PROMPT,
                f"消息列表：\n{numbered}\n\n只输出JSON。",
                task="triviality",
                default=None,
                timeout=25.0,
            )
            if isinstance(parsed, dict):
                for idx in parsed.get("trivial_indices") or []:
                    try:
                        trivial.add(int(idx))
                    except (TypeError, ValueError):
                        continue
    except Exception as exc:
        logger.warning("triviality LLM judgment failed: %s", exc)
    return trivial


def _extract_json_object(text: str):
    """Parse the first JSON object from an LLM response.

    Tolerates: exact JSON, fenced code blocks (```json ... ```), and prose
    wrapping (the model narrating before/after the JSON). Returns None when
    no JSON object can be recovered.
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        parsed = json.loads(t)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    lines = [l for l in t.split("\n") if not l.lstrip().startswith("```")]
    joined = "\n".join(lines).strip()
    try:
        parsed = json.loads(joined)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = joined.find("{"), joined.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(joined[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _unit_source_trust(unit_kind: str, source_ids: list[str] | None = None) -> str:
    trust = _UNIT_SOURCE_TRUST.get(unit_kind, "user_stated")
    if unit_kind == "file_memory" and source_ids:
        joined = " ".join(source_ids).upper()
        if "AGENT" in joined:
            return "agent_inferred"
    return trust


async def ingest_raw_unit(
    db: AsyncSession, user_id: str, unit_kind: str, raw_text: str, source_ids: list[str],
) -> Optional[str]:
    if not raw_text or len(raw_text) < 5:
        return None
    # 琐碎/寒暄预筛在调用方批量完成（LLM 判断，_filter_trivial_texts）
    clean_text, _ = scrub_pii(raw_text[:1000])

    emb = await embed_text(clean_text)
    if not emb:
        return None

    unit_id = str(uuid.uuid4())
    unit = SubconsciousLog(
        id=unit_id,
        user_id=user_id,
        unit_kind=unit_kind,
        raw_text=clean_text,
        source_ids=json.dumps(source_ids, ensure_ascii=False),
        embedding=emb,
    )
    db.add(unit)
    await db.flush()
    return unit_id


async def ingest_pending_raw_units(db: AsyncSession, user_id: str) -> int:
    from app.db.database import UserAgentState
    result = await db.execute(
        text("SELECT id, last_message_processed_at, last_note_processed_at, last_file_memory_processed_at FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    row = result.fetchone()
    if not row:
        return 0

    state_id = row[0]
    t0 = datetime.utcnow()
    count = 0

    for kind, col_name in [
        ("message", "last_message_processed_at"),
        ("note", "last_note_processed_at"),
        ("file_memory", "last_file_memory_processed_at"),
    ]:
        old_watermark = getattr(row, col_name) if hasattr(row, col_name) else None
        if old_watermark is None:
            old_watermark = datetime(2020, 1, 1)

        raw_units = await _load_raw_by_kind(db, user_id, kind, old_watermark, t0)
        # 琐碎/寒暄批量预筛（§5.1.a step 2，LLM 判断）：跳过但不阻塞水位线
        trivial_idx = await _filter_trivial_texts([rt for rt, _ in raw_units])
        kind_ok = True
        for unit_idx, (raw_text, source_ids) in enumerate(raw_units):
            if unit_idx in trivial_idx:
                continue
            try:
                uid = await ingest_raw_unit(db, user_id, kind, raw_text, source_ids)
                if uid:
                    count += 1
                else:
                    kind_ok = False
            except Exception:
                kind_ok = False
                logger.exception("ingest_raw_unit failed for user=%s kind=%s", user_id, kind)

        if kind_ok:
            await db.execute(
                text(f"UPDATE user_agent_states SET {col_name} = :t0 WHERE id = :sid"),
                {"t0": t0, "sid": state_id},
            )
        else:
            logger.warning(
                "ingest incomplete for user=%s kind=%s; watermark kept for retry",
                user_id, kind,
            )
    return count


async def _load_raw_by_kind(
    db: AsyncSession, user_id: str, kind: str, old_watermark: datetime, t0: datetime,
) -> list[tuple[str, list[str]]]:
    results = []
    if kind == "message":
        # §5.1.a：user+assistant 消息配对为一个单元（各截 500 字符）
        # LATERAL 单次取配对 assistant（上界=下一条 user 消息，子查询仅一份）
        result = await db.execute(
            text("""
                SELECT m.id, m.content, pa.assistant_id, pa.assistant_content
                FROM messages m
                JOIN conversations c ON m.conversation_id = c.id
                LEFT JOIN LATERAL (
                    SELECT a.id AS assistant_id, a.content AS assistant_content
                    FROM messages a
                    WHERE a.conversation_id = m.conversation_id AND a.role = 'assistant'
                      AND a.created_at >= m.created_at
                      AND a.created_at < COALESCE(
                          (SELECT m2.created_at FROM messages m2
                           WHERE m2.conversation_id = m.conversation_id AND m2.role = 'user'
                             AND m2.created_at > m.created_at
                           ORDER BY m2.created_at ASC LIMIT 1),
                          m.created_at + interval '1 day')
                    ORDER BY a.created_at ASC LIMIT 1
                ) pa ON TRUE
                WHERE c.user_id = :uid AND m.role = 'user'
                  AND m.created_at > :ow AND m.created_at <= :t0
                ORDER BY m.created_at ASC LIMIT 50
            """),
            {"uid": user_id, "ow": old_watermark, "t0": t0},
        )
        for row in result.fetchall():
            user_part = (row[1] or "")[:500]
            if not user_part.strip():
                continue
            # 琐碎/寒暄预筛由调用方批量 LLM 判断（_filter_trivial_texts）
            source_ids = [row[0]]
            raw = f"用户: {user_part}"
            if row[2] and row[3]:
                source_ids.append(row[2])
                raw += f"\n助手: {(row[3] or '')[:500]}"
            results.append((raw, source_ids))

    elif kind == "note":
        result = await db.execute(
            text("""
                SELECT n.content, n.id FROM notes n
                JOIN notebooks nb ON n.notebook_id = nb.id
                WHERE nb.user_id = :uid
                  AND n.updated_at > :ow AND n.updated_at <= :t0
                ORDER BY n.updated_at ASC LIMIT 20
            """),
            {"uid": user_id, "ow": old_watermark, "t0": t0},
        )
        for row in result.fetchall():
            results.append((row[0] or "", [row[1]]))

    elif kind == "file_memory":
        # §5.1.a 文件记忆水位线拉取：AGENT.md/USER.md 条目按内容哈希去重入库
        results.extend(await _load_file_memory_units(db, user_id))

    return results


async def _load_file_memory_units(db: AsyncSession, user_id: str) -> list[tuple[str, list[str]]]:
    import hashlib
    from app.tools.memory import _get_memory_dir

    memory_dir = _get_memory_dir() / str(user_id)
    if not memory_dir.exists():
        return []

    units: list[tuple[str, list[str]]] = []
    for target in ("USER.md", "AGENT.md"):
        fpath = memory_dir / target
        if not fpath.exists():
            continue
        try:
            content = await asyncio.to_thread(fpath.read_text, encoding="utf-8")
        except Exception:
            continue
        entries = [e.strip() for e in content.split("\n§\n") if e.strip()]
        for entry in entries[:200]:
            entry_hash = hashlib.sha1(entry.encode("utf-8")).hexdigest()[:16]
            source_id = f"file_memory:{target.replace('.md', '')}:{entry_hash}"
            exists = await db.execute(
                text("SELECT 1 FROM subconscious_log WHERE user_id = :uid AND source_ids LIKE :sid LIMIT 1"),
                {"uid": user_id, "sid": f"%{entry_hash}%"},
            )
            if exists.fetchone():
                continue
            units.append((entry[:1000], [source_id]))
    return units


async def scan_recurrence(db: AsyncSession, user_id: str) -> int:
    sub_cfg = config.memory_subconscious
    sim_threshold = float(sub_cfg.get("recurrence_sim_threshold", 0.6))
    count_threshold = int(sub_cfg.get("recurrence_count_threshold", 3))
    top_k = int(sub_cfg.get("top_k_neighbors", 5))
    batch_size = int(sub_cfg.get("scan_batch_size", 50))
    # 2026-08-10 队头阻塞修复：每单元最多作为批次头扫描 N 次；N 次未晋升即让位，
    # 批次得以推进到新单元（旧单元仍可被新单元匹配为邻居证据）。
    max_scans = int(sub_cfg.get("recurrence_max_scans", 3))
    promote_count = 0

    result = await db.execute(
        text("SELECT id, raw_text, embedding, created_at, recurrence_count, recurrence_scan_count FROM subconscious_log WHERE user_id = :uid AND promoted = FALSE AND embedding IS NOT NULL AND recurrence_scan_count < :max_scans ORDER BY created_at ASC LIMIT :lim"),
        {"uid": user_id, "lim": batch_size, "max_scans": max_scans},
    )
    rows = result.fetchall()
    if not rows:
        return 0

    from app.services.memory_embedding_service import find_neighbors_for_unit, _emb_from_db

    for row in rows:
        unit_id, raw_text = row[0], row[1]
        emb_data = _emb_from_db(row[2]) if row[2] else None
        created_at = row[3]
        rec_count = row[4] or 0

        if emb_data is None:
            # SQL 已滤 embedding IS NOT NULL，此处仅 _emb_from_db 反序列化失败时命中；
            # 同样消耗扫描预算，避免该单元永久占用批次槽位。
            await db.execute(
                text("UPDATE subconscious_log SET recurrence_scan_count = recurrence_scan_count + 1 WHERE id = :id"),
                {"id": unit_id},
            )
            continue

        accel_threshold = int(sub_cfg.get("accelerate_recurrence_threshold", 2))
        effective_count = count_threshold
        if rec_count >= accel_threshold:
            effective_count = 2

        neighbors = await find_neighbors_for_unit(db, user_id, emb_data, unit_id, created_at, top_k)
        relevant = [n for n in neighbors if n["similarity"] >= sim_threshold]
        if len(relevant) < effective_count:
            await db.execute(
                text("UPDATE subconscious_log SET recurrence_scan_count = recurrence_scan_count + 1 WHERE id = :id"),
                {"id": unit_id},
            )
            continue

        cluster_units = relevant + [{"id": unit_id, "raw_text": raw_text, "similarity": 1.0}]
        for n in relevant:
            await db.execute(
                text("UPDATE subconscious_log SET recurrence_count = recurrence_count + 1, last_recurrence_at = NOW() WHERE id = :id"),
                {"id": n["id"]},
            )
        await db.execute(
            text("UPDATE subconscious_log SET recurrence_count = recurrence_count + 1, last_recurrence_at = NOW() WHERE id = :id"),
            {"id": unit_id},
        )

        # §5.1.a：为每个单元打隐式 source_trust 标签（升级时传入 LLM）
        cluster_ids = [u["id"] for u in cluster_units]
        kind_rows = await db.execute(
            text("SELECT id, unit_kind, source_ids FROM subconscious_log WHERE id = ANY(:ids)"),
            {"ids": cluster_ids},
        )
        kind_map = {r[0]: (r[1], r[2]) for r in kind_rows.fetchall()}
        for u in cluster_units:
            kinfo = kind_map.get(u["id"])
            if kinfo:
                try:
                    sids = json.loads(kinfo[1]) if kinfo[1] else []
                except (json.JSONDecodeError, TypeError):
                    sids = []
                u["source_trust"] = _unit_source_trust(kinfo[0], sids)
            else:
                u["source_trust"] = "user_stated"

        llm_output = await _call_extraction_llm(cluster_units, db, user_id)
        if not llm_output:
            await db.execute(
                text("UPDATE subconscious_log SET recurrence_scan_count = recurrence_scan_count + 1 WHERE id = :id"),
                {"id": unit_id},
            )
            continue

        await _execute_promotion(db, user_id, cluster_units, llm_output)
        promote_count += 1

    return promote_count


async def _call_extraction_llm(
    cluster_units: list[dict], db: AsyncSession, user_id: str,
) -> dict | None:
    from app.services.memory_llm_factory import _memory_llm
    from app.services.memory_concept_service import get_concepts_for_extraction, get_clusters_for_extraction

    existing_concepts = await get_concepts_for_extraction(db, user_id)
    existing_clusters = await get_clusters_for_extraction(db, user_id)

    # §5.2 冷启动兜底配套：建库初期放宽写入门控
    gate_hint = ""
    try:
        active_count_result = await db.execute(
            text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND status = 'active' AND activation_strength > 0.05 AND valid_to IS NULL"),
            {"uid": user_id},
        )
        bootstrap = int(config.memory_retrieval.get("bootstrap_threshold", 10)) if hasattr(config, "memory_retrieval") else 10
        if (active_count_result.scalar() or 0) < bootstrap:
            gate_hint = "\n建库初期，宁宽勿漏。"
    except Exception:
        gate_hint = ""

    units_text = "\n\n---\n\n".join(
        f"[unit {i} | 信任级: {u.get('source_trust', 'user_stated')}] {u['raw_text'][:800]}"
        for i, u in enumerate(cluster_units)
    )

    concepts_summary = "\n".join(
        f"- {c['canonical_name']} (id={c['id']}): {c['description_short'] or ''}" for c in existing_concepts[:200]
    ) or "无"

    clusters_summary = "\n".join(
        f"- {c['name']} (id={c['id']})" for c in existing_clusters[:50]
    ) or "无"

    prompt = {
        "role": "system",
        "content": (
            "你是一个记忆提取助手。请从以下对话/笔记片段中提取值得持久化的概念和事件叙事。\n\n"
            "一个概念 = 一个可独立验证的事实性陈述。\n"
            '"用户喜欢用 Vim 编辑器" 是一个概念；"用户偏好"（太宽泛）和 "Vim 配置文件路径"（太细碎）都不是好的概念粒度。\n\n'
            "优先匹配已有概念，避免重复创建。当不确定时，倾向更粗的粒度。\n\n"
            "并非所有输入都值得形成概念。问候、寒暄、一次性任务指令、纯情绪表达不具有跨会话持久价值。若本批次中没有任何值得持久化的事实，输出空 concepts: []、保留 episodic.narrative 即可。\n\n"
            'importance 是持久重要性（0-1）：用户身份/偏好/昵称/关系/长期稳定事实 = 0.8-1.0；'
            "知识/技能/经验/工作方法 = 0.5-0.8；一次性任务/临时话题/时事 = 0.2-0.5；不值得记 = 0-0.2。"
            "importance 与提及频率无关：用户只说过一次但终身生效的偏好必须高 importance。\n\n"
            "procedural 概念是'做某类任务的有效方法'，必须带触发条件。来源于 agent 自我观察/工作方法。procedural 免衰减。\n\n"
            + gate_hint +
            "输出 JSON 格式：\n"
            '{"episodic": {"narrative": "<500 token 事件叙事>", "valid_from": "ISO8601或null", "merge_with_episode_id": "已有episode ID或null"}, '
            '"concepts": [{"canonical_name": "...", "description_short": "≤80中文字", "description_full": "≤1000 token详版", '
            '"aliases": [...], "match_existing_id": "已有概念ID或null", "cluster_suggestion": "集合名", '
            '"source_trust": "user_stated|user_authored|agent_inferred", "memory_type": "semantic|episodic|procedural", '
            '"importance": 0-1数字, "source_unit_ids": [...], "event_time": "ISO8601或null"}]}'
        ),
    }

    user_prompt = (
        f"已有概念：\n{concepts_summary}\n\n"
        f"已有集合：\n{clusters_summary}\n\n"
        f"待提取单元：\n{units_text}"
    )

    try:
        llm = _memory_llm("concept_extraction")
        response = await asyncio.wait_for(
            llm.complete_chat(
                [prompt, {"role": "user", "content": user_prompt}],
                temperature=float(config.memory.get("concept_extraction_temperature", 0.1)),
                max_tokens=config.memory.get("concept_extraction_max_tokens") or None,
            ),
            timeout=60,
        )
        try:
            from app.services.memory_cost_governance_service import record_llm_call
            await record_llm_call(db, user_id, "extract")
        except Exception:
            logger.debug("record_llm_call failed", exc_info=True)
        response = response or ""
        response = response.strip()
        if response.startswith("```"):
            lines = response.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            response = "\n".join(lines)
        parsed = _extract_json_object(response)
        if parsed is None:
            logger.warning(
                "Extraction LLM returned empty/non-JSON content for user=%s (len=%d, snippet=%r)",
                user_id, len(response), response[:200],
            )
        return parsed
    except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
        logger.warning("Extraction LLM call failed for user=%s: %s", user_id, e)
        return None


async def _execute_promotion(
    db: AsyncSession, user_id: str, cluster_units: list[dict], llm_output: dict,
) -> None:
    from app.services.memory_concept_service import extract_concepts_from_recurrence, reconcile_concept_sources
    source_unit_ids = [u["id"] for u in cluster_units]
    raw_texts = [u["raw_text"] for u in cluster_units]
    concept_ids, episode_id = await extract_concepts_from_recurrence(
        db, user_id, llm_output, source_unit_ids, raw_texts,
    )
    for u in cluster_units:
        await db.execute(
            text("UPDATE subconscious_log SET promoted = TRUE, promoted_at = NOW() WHERE id = :id"),
            {"id": u["id"]},
        )
    # §5.1.d step 3：对账 pass——来源全部失效的概念标 needs_review
    try:
        await reconcile_concept_sources(db, user_id)
    except Exception:
        logger.debug("reconcile_concept_sources failed for user=%s", user_id, exc_info=True)


async def archive_soft_deprecated(db: AsyncSession, user_id: str) -> int:
    ret_cfg = config.memory_subconscious
    retention = int(ret_cfg.get("subconscious_retention_days", 30))
    archive_ret = int(ret_cfg.get("subconscious_archive_retention_days", 90))
    now = datetime.utcnow()

    result = await db.execute(
        text("UPDATE subconscious_log SET promoted = TRUE, embedding = NULL WHERE user_id = :uid AND promoted = FALSE AND created_at < :cutoff AND embedding IS NOT NULL"),
        {"uid": user_id, "cutoff": now - timedelta(days=retention)},
    )
    soft_count = result.rowcount

    result = await db.execute(
        text("DELETE FROM subconscious_log WHERE user_id = :uid AND embedding IS NULL AND created_at < :cutoff"),
        {"uid": user_id, "cutoff": now - timedelta(days=max(retention, archive_ret) + 30)},
    )
    hard_count = result.rowcount
    if soft_count > 0 or hard_count > 0:
        logger.info("subconscious archive: user=%s soft=%d hard=%d", user_id, soft_count, hard_count)
    return soft_count
