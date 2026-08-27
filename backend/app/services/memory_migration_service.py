# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import hashlib
import json
import zlib
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import AsyncSessionLocal
from app.services.memory_embedding_service import embed_text, embed_texts, _emb_to_pgvector

config = get_config()
logger = logging.getLogger(__name__)

_STEPS = ["memory_summary", "agent_memories", "file_memory", "dream", "watermarks"]
_BACKOFF_SECONDS = [60, 300, 900]  # §8.5.3 指数退避：1min → 5min → 15min

# 系统上线日期——所有迁移数据不得早于此日期
_SYSTEM_LAUNCH_DATE = datetime(2026, 1, 1)


def _build_migration_prompt() -> str:
    """构建迁移提取 prompt，每次调用注入当前日期以防止 LLM 编造历史时间。"""
    now_str = datetime.utcnow().strftime("%Y-%m-%d")
    return (
        "你是记忆迁移助手。从以下来源文本中提取值得持久化的概念，并产出一条事件叙事。\n\n"
        "【严格规则——违反即失败】\n"
        f"1. 当前日期：{now_str}。系统于 2026 年 1 月上线，所有数据均产生于 2026 年及以后。\n"
        "2. valid_from：如果源文本中包含明确日期，使用该日期的 ISO8601 格式；否则必须填 null。"
        " 禁止编造、推测、回填任何日期。宁可 null 也不要错误日期。\n"
        "3. narrative：只描述源文本中实际包含的信息。禁止添加源文本中不存在的细节、背景、动机分析。\n"
        "4. description_full：只基于源文本事实，禁止补充训练数据中的知识。\n\n"
        "一个概念 = 一个可独立验证的事实性陈述；优先粗粒度。procedural 概念是带触发条件的工作方法。\n"
        "输出 JSON：{\"episodic\": {\"narrative\": \"≤500字事件叙事\", \"valid_from\": \"ISO8601或null\"}, "
        "\"concepts\": [{\"canonical_name\": \"...\", \"description_short\": \"≤80中文字\", "
        "\"description_full\": \"≤1000 token详版\", \"aliases\": [...], "
        "\"memory_type\": \"semantic|episodic|procedural\", "
        "\"source_trust\": \"user_authored|agent_inferred\"}]}"
    )


def _lock_id(user_id: str) -> int:
    return zlib.crc32(f"migrate_{user_id}".encode("utf-8")) % (2**31)


def _parse_metadata(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _new_progress() -> dict:
    return {
        "status": "pending",
        "current_step": 0,
        "steps": {s: {"status": "pending"} for s in _STEPS},
        "attempts": 0,
        "last_error": None,
        "next_retry_at": None,
    }


async def _load_progress(db: AsyncSession, user_id: str) -> tuple[dict, dict]:
    # FOR UPDATE：与 cost_governance _save_level 串行化 metadata_json 读-改-写（防互丢写入）
    result = await db.execute(
        text("SELECT metadata_json FROM user_agent_states WHERE user_id = :uid FOR UPDATE"),
        {"uid": user_id},
    )
    meta = _parse_metadata(result.scalar())
    progress = meta.get("migration") or _new_progress()
    return meta, progress


async def _save_progress(db: AsyncSession, user_id: str, meta: dict, progress: dict) -> None:
    meta["migration"] = progress
    await db.execute(
        text("UPDATE user_agent_states SET metadata_json = :meta WHERE user_id = :uid"),
        {"meta": json.dumps(meta, ensure_ascii=False), "uid": user_id},
    )


def _backoff_seconds(attempts: int) -> int:
    idx = min(max(attempts - 1, 0), len(_BACKOFF_SECONDS) - 1)
    return _BACKOFF_SECONDS[idx]


async def _record_failure(db: AsyncSession, user_id: str, meta: dict, progress: dict, err: str) -> str:
    progress["attempts"] = int(progress.get("attempts", 0)) + 1
    progress["last_error"] = str(err)[:500]
    max_retries = int(config.memory.get("migration_max_retries", 5))
    if progress["attempts"] > max_retries:
        progress["status"] = "failed"
        progress["next_retry_at"] = None
        logger.error("Migration user %s failed permanently after %d attempts: %s",
                     user_id, progress["attempts"], err)
        outcome = "failed"
    else:
        progress["status"] = "pending"
        progress["next_retry_at"] = (datetime.utcnow() + timedelta(
            seconds=_backoff_seconds(progress["attempts"]))).isoformat()
        logger.warning("Migration user %s attempt %d failed: %s", user_id, progress["attempts"], err)
        outcome = "retry_scheduled"
    await _save_progress(db, user_id, meta, progress)
    await db.commit()
    return outcome


async def _llm_extract(db: AsyncSession, user_id: str, source_text: str) -> dict | None:
    from app.services.memory_llm_factory import _memory_llm
    llm = _memory_llm("migration")
    timeout = int(config.memory.get("migration_llm_timeout_seconds", 60))
    prompt = _build_migration_prompt()
    resp = await asyncio.wait_for(
        llm.complete_chat(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": source_text[:8000]},
            ],
            temperature=0.1,
            max_tokens=config.memory.get("concept_extraction_max_tokens") or None,
        ),
        timeout=timeout,
    )
    resp = (resp or "").strip()
    if resp.startswith("```"):
        resp = "\n".join(l for l in resp.split("\n") if not l.startswith("```"))
    try:
        from app.services.memory_cost_governance_service import record_llm_call
        await record_llm_call(db, user_id, "migration")
    except Exception:
        pass
    return _parse_llm_json(resp)


def _parse_llm_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        if "Invalid \\escape" in str(e) or "Invalid \\u" in str(e):
            fixed = _fix_json_escapes(raw)
            return json.loads(fixed)
        raise


def _fix_json_escapes(s: str) -> str:
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in '"\\/bfnrtu':
                if nxt == 'u':
                    if i + 5 < len(s) and all(c in '0123456789abcdefABCDEF' for c in s[i + 2:i + 6]):
                        result.append(s[i:i + 6])
                        i += 6
                        continue
                else:
                    result.append('\\')
                    result.append(nxt)
                    i += 2
                    continue
            result.append('\\\\')
            i += 1
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def _sanitize_llm_output(llm_output: dict) -> dict:
    """后置校验：修正 LLM 输出中的虚构日期和超范围内容。"""
    epic = llm_output.get("episodic") or {}
    vf_str = epic.get("valid_from")
    if vf_str:
        try:
            vf_dt = datetime.fromisoformat(str(vf_str).replace("Z", "+00:00")).replace(tzinfo=None)
            if vf_dt < _SYSTEM_LAUNCH_DATE:
                logger.warning("Migration: valid_from %s predates system launch, forcing null", vf_str)
                epic["valid_from"] = None
        except (ValueError, TypeError):
            epic["valid_from"] = None
    return llm_output


async def _concept_key_exists(db: AsyncSession, user_id: str, source_key: str) -> bool:
    result = await db.execute(
        text("SELECT 1 FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration' AND source_raw_ids LIKE :key LIMIT 1"),
        {"uid": user_id, "key": f"%{source_key}%"},
    )
    return result.fetchone() is not None


async def _insert_migration_concept(
    db: AsyncSession, user_id: str, cdata: dict, source_keys: list[str],
    emb: list[float] | None = None,
) -> str | None:
    name = (cdata.get("canonical_name") or "").strip()
    if not name:
        return None
    short = (cdata.get("description_short") or "")[:80]
    full = (cdata.get("description_full") or "")[:5000]
    aliases = json.dumps(cdata.get("aliases", []), ensure_ascii=False)
    source_trust = cdata.get("source_trust", "user_authored")
    memory_type = cdata.get("memory_type", "semantic")
    activation = 1.0 if source_trust in ("user_stated", "user_authored") else 0.0
    status = "active" if source_trust in ("user_stated", "user_authored") else "silent"

    cid = str(uuid.uuid4())
    if emb is None:
        emb = await embed_text(f"{name} {short}")
    from app.services.memory_concept_service import _sanitize_importance
    await db.execute(
        text("""INSERT INTO memory_concepts
            (id, user_id, canonical_name, description_short, description_full, aliases,
             weight, importance, importance_evaluated, stability, source_trust, memory_type,
             activation_strength, status, source_type, source_raw_ids, embedding, embedding_updated_at)
            VALUES (:id, :uid, :name, :short, :full, :aliases,
                    :w, :imp, :evald, :stab, :trust, :mtype, :act, :status,
                    'migration', :skeys, CAST(:emb AS vector), :eat)"""),
        {
            "id": cid, "uid": user_id, "name": name, "short": short, "full": full,
            "aliases": aliases, "w": 0.5, "imp": _sanitize_importance(cdata),
            "evald": "importance" in cdata, "stab": 14,
            "trust": source_trust,
            "mtype": memory_type, "act": activation, "status": status,
            "skeys": json.dumps(source_keys, ensure_ascii=False),
            "emb": _emb_to_pgvector(emb) if emb else None,
            "eat": datetime.utcnow() if emb else None,
        },
    )
    return cid


async def _insert_migration_episode(
    db: AsyncSession, user_id: str, narrative: str, source_keys: list[str], concept_ids: list[str],
    valid_from: datetime | None = None, emb: list[float] | None = None,
) -> str | None:
    narrative = (narrative or "").strip()[:5000]
    if not narrative:
        return None
    eid = str(uuid.uuid4())
    if emb is None:
        emb = await embed_text(narrative)
    await db.execute(
        text("""INSERT INTO memory_episodes
            (id, user_id, narrative, source_unit_ids, source_concept_ids, valid_from, embedding, source_type)
            VALUES (:id, :uid, :n, '[]', :cids, :vf, CAST(:emb AS vector), 'migration')"""),
        {"id": eid, "uid": user_id, "n": narrative,
         "cids": json.dumps(concept_ids, ensure_ascii=False),
         "vf": valid_from or datetime.utcnow(),
         "emb": _emb_to_pgvector(emb) if emb else None},
    )
    return eid


async def _apply_llm_output(
    db: AsyncSession, user_id: str, llm_output: dict, source_keys: list[str],
) -> list[str]:
    llm_output = _sanitize_llm_output(llm_output)
    concepts = llm_output.get("concepts") or []
    epic = llm_output.get("episodic") or {}
    has_narrative = bool(epic.get("narrative"))

    # 预收集所有待 embedding 的文本，一次性批量调用
    texts_to_embed: list[str] = []
    for cdata in concepts:
        name = (cdata.get("canonical_name") or "").strip()
        short = (cdata.get("description_short") or "")[:80]
        if name:
            texts_to_embed.append(f"{name} {short}")
    if has_narrative:
        texts_to_embed.append(epic["narrative"][:5000])

    embeddings = await embed_texts(texts_to_embed) if texts_to_embed else []

    # 分配 embedding 给各概念和事件
    emb_idx = 0
    concept_ids: list[str] = []
    for cdata in concepts:
        name = (cdata.get("canonical_name") or "").strip()
        short = (cdata.get("description_short") or "")[:80]
        if not name:
            continue
        emb = embeddings[emb_idx] if emb_idx < len(embeddings) else None
        emb_idx += 1
        try:
            cid = await _insert_migration_concept(db, user_id, cdata, source_keys, emb=emb)
            if cid:
                concept_ids.append(cid)
        except Exception:
            logger.debug("migration concept insert failed", exc_info=True)

    if has_narrative:
        ep_vf = None
        vf_str = epic.get("valid_from")
        if vf_str:
            try:
                ep_vf = datetime.fromisoformat(str(vf_str).replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, TypeError):
                ep_vf = None
        epi_emb = embeddings[emb_idx] if emb_idx < len(embeddings) else None
        try:
            await _insert_migration_episode(db, user_id, epic["narrative"], source_keys, concept_ids,
                                            valid_from=ep_vf, emb=epi_emb)
        except Exception:
            logger.debug("migration episode insert failed", exc_info=True)
    return concept_ids


# ---------- 各 Step ----------

async def _migrate_memory_summary(db: AsyncSession, user_id: str, state: dict, progress: dict) -> None:
    summary = state.get("memory_summary")
    if not summary:
        progress["steps"]["memory_summary"] = {"status": "skipped", "at": datetime.utcnow().isoformat()}
        return
    source_key = "legacy:memory_summary"
    if await _concept_key_exists(db, user_id, source_key):
        progress["steps"]["memory_summary"] = {"status": "done", "at": datetime.utcnow().isoformat()}
        return
    llm_output = await _llm_extract(db, user_id, summary)
    if llm_output is None:
        raise RuntimeError("memory_summary LLM extraction returned None")
    await _apply_llm_output(db, user_id, llm_output, [source_key])
    progress["steps"]["memory_summary"] = {"status": "done", "at": datetime.utcnow().isoformat()}


async def _migrate_agent_memories(db: AsyncSession, user_id: str, state: dict, progress: dict) -> None:
    state_id = state.get("id")
    if not state_id:
        progress["steps"]["agent_memories"] = {"status": "skipped", "at": datetime.utcnow().isoformat()}
        return

    batch_size = int(config.memory.get("migration_batch_size", 5))
    step = progress["steps"].setdefault("agent_memories", {"status": "pending"})
    cursor = step.get("cursor")  # [created_at_iso, id]
    done = int(step.get("done", 0))
    skipped = int(step.get("skipped", 0))

    total_r = await db.execute(
        text("SELECT COUNT(*) FROM agent_memories WHERE agent_state_id = :sid"),
        {"sid": state_id},
    )
    total = total_r.scalar() or 0

    while True:
        if cursor:
            cts_val = datetime.fromisoformat(cursor[0]) if isinstance(cursor[0], str) else cursor[0]
            result = await db.execute(
                text("""SELECT id, content, created_at FROM agent_memories
                        WHERE agent_state_id = :sid AND (created_at, id) > (CAST(:cts AS TIMESTAMP), CAST(:cid AS VARCHAR))
                        ORDER BY created_at ASC, id ASC LIMIT :lim"""),
                {"sid": state_id, "cts": cts_val, "cid": cursor[1], "lim": batch_size},
            )
        else:
            result = await db.execute(
                text("""SELECT id, content, created_at FROM agent_memories
                        WHERE agent_state_id = :sid ORDER BY created_at ASC, id ASC LIMIT :lim"""),
                {"sid": state_id, "lim": batch_size},
            )
        rows = result.fetchall()
        if not rows:
            break

        # §8.5.2：一批中已迁移的子集先剔除再送 LLM
        pending = []
        for row in rows:
            mem_id, content = row[0], row[1]
            source_key = f"legacy:agent_memories:{mem_id}"
            if await _concept_key_exists(db, user_id, source_key):
                skipped += 1
                continue
            if not content or len(content.strip()) < 5:
                skipped += 1
                continue
            pending.append((mem_id, content, source_key))

        if pending:
            batch_text = "\n\n---\n\n".join(f"[{i}] {c[:2000]}" for i, (_, c, _) in enumerate(pending))
            llm_output = await _llm_extract(db, user_id, batch_text)
            if llm_output is None:
                raise RuntimeError("agent_memories LLM extraction returned None")
            keys = [k for _, _, k in pending]
            await _apply_llm_output(db, user_id, llm_output, keys)
            done += len(pending)

        last = rows[-1]
        cursor = [last[2].isoformat() if last[2] else datetime.utcnow().isoformat(), last[0]]
        # §8.5.3：每批事务提交后立即推进 cursor
        step.update({"status": "running", "cursor": cursor, "done": done,
                     "total": total, "skipped": skipped})
        await _save_progress(db, user_id, (await _load_progress(db, user_id))[0], progress)
        await db.commit()

    step.update({"status": "done", "at": datetime.utcnow().isoformat(),
                 "done": done, "total": total, "skipped": skipped})


async def _migrate_file_memories(db: AsyncSession, user_id: str, progress: dict) -> None:
    from app.tools.memory import _get_memory_dir
    memory_dir = _get_memory_dir() / str(user_id)
    if not memory_dir.exists():
        progress["steps"]["file_memory"] = {"status": "skipped", "at": datetime.utcnow().isoformat()}
        return

    for target in ["USER.md", "AGENT.md"]:
        fpath = memory_dir / target
        if not fpath.exists():
            continue
        try:
            content = await asyncio.to_thread(fpath.read_text, encoding="utf-8")
        except Exception:
            continue
        entries = [e.strip() for e in content.split("\n§\n") if e.strip()]
        source_trust = "user_authored" if target == "USER.md" else "agent_inferred"
        target_tag = target.replace(".md", "")

        batch_keys: list[str] = []
        batch_texts: list[str] = []
        for entry in entries[:200]:
            entry_hash = hashlib.sha1(entry.encode("utf-8")).hexdigest()[:16]
            source_key = f"legacy:file_memory:{target_tag}:{entry_hash}"
            if await _concept_key_exists(db, user_id, source_key):
                continue
            batch_keys.append(source_key)
            batch_texts.append(entry)
        if not batch_texts:
            continue
        llm_output = await _llm_extract(db, user_id, "\n\n---\n\n".join(batch_texts)[:8000])
        if llm_output is None:
            raise RuntimeError(f"file_memory {target} LLM extraction returned None")
        for cdata in (llm_output.get("concepts") or []):
            cdata.setdefault("source_trust", source_trust)
        await _apply_llm_output(db, user_id, llm_output, batch_keys)

    progress["steps"]["file_memory"] = {"status": "done", "at": datetime.utcnow().isoformat()}


async def _migrate_dream(db: AsyncSession, user_id: str, state: dict, progress: dict) -> None:
    if not state.get("id"):
        progress["steps"]["dream"] = {"status": "skipped", "at": datetime.utcnow().isoformat()}
        return
    existing = await db.execute(
        text("SELECT COUNT(*) FROM agent_dreams WHERE agent_state_id = :sid AND dream_type = 'legacy'"),
        {"sid": state["id"]},
    )
    if existing.scalar():
        progress["steps"]["dream"] = {"status": "done", "at": datetime.utcnow().isoformat()}
        return
    dream_summary = state.get("dream_summary")
    if not dream_summary:
        progress["steps"]["dream"] = {"status": "skipped", "at": datetime.utcnow().isoformat()}
        return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    await db.execute(
        text("INSERT INTO agent_dreams (id, agent_state_id, generated_for_date, summary, source_note_count, source_message_count, created_at, dream_type) VALUES (:id, :sid, :dt, :sum, 0, 0, :created, 'legacy')"),
        {"id": str(uuid.uuid4()), "sid": state["id"], "dt": today, "sum": dream_summary[:4000], "created": datetime.utcnow()},
    )
    progress["steps"]["dream"] = {"status": "done", "at": datetime.utcnow().isoformat()}


async def _init_watermarks(db: AsyncSession, user_id: str, state: dict, progress: dict) -> None:
    if not state.get("id"):
        return
    now = datetime.utcnow()
    await db.execute(
        text("UPDATE user_agent_states SET last_note_processed_at = :now, last_message_processed_at = :now, last_file_memory_processed_at = :now, last_subconscious_scan_at = :now, last_consolidation_at = :now WHERE id = :sid"),
        {"now": now, "sid": state["id"]},
    )
    progress["steps"]["watermarks"] = {"status": "done", "at": now.isoformat()}


# ---------- 对账 / dry-run / 回滚 ----------

async def reconcile_migration(db: AsyncSession, user_id: str) -> dict:
    """§8.5.4 自动对账：任一不达标即 failed，不写 migration_completed_at。"""
    failures: list[str] = []

    state_r = await db.execute(
        text("SELECT memory_summary FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    memory_summary = state_r.scalar()

    r = await db.execute(
        text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration' AND valid_to IS NULL"),
        {"uid": user_id},
    )
    concept_count = r.scalar() or 0
    if memory_summary and concept_count == 0:
        failures.append("concept_count: memory_summary 非空但迁移概念数为 0")

    # §9.4：embedding NULL 先补生成再校验
    null_r = await db.execute(
        text("SELECT id, canonical_name, description_short FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration' AND embedding IS NULL AND valid_to IS NULL"),
        {"uid": user_id},
    )
    null_rows = null_r.fetchall()
    if null_rows:
        backfill_texts = [f"{row[1]} {row[2] or ''}" for row in null_rows]
        backfill_embs = await embed_texts(backfill_texts)
        for row, emb in zip(null_rows, backfill_embs):
            try:
                if emb:
                    await db.execute(
                        text("UPDATE memory_concepts SET embedding = CAST(:emb AS vector), embedding_updated_at = NOW() WHERE id = :id"),
                        {"emb": _emb_to_pgvector(emb), "id": row[0]},
                    )
            except Exception:
                pass
    await db.commit()
    null_r2 = await db.execute(
        text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration' AND embedding IS NULL AND valid_to IS NULL"),
        {"uid": user_id},
    )
    null_count = null_r2.scalar() or 0
    if null_count > 0:
        failures.append(f"embedding_null: {null_count} 条迁移概念 embedding 为 NULL")

    # 覆盖率：已迁移键数 / 应迁移条目数 ≥ 99%
    state_r2 = await db.execute(
        text("SELECT id FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    sid = state_r2.scalar()
    coverage = 1.0
    if sid:
        # §8.5.4：分母排除被 §5.1.a 预筛规则跳过的条目（len<5 等，已记入 steps.skipped）
        total_r = await db.execute(
            text("SELECT COUNT(*) FROM agent_memories WHERE agent_state_id = :sid AND length(trim(coalesce(content, ''))) >= 5"),
            {"sid": sid},
        )
        total = total_r.scalar() or 0
        if total > 0:
            covered_r = await db.execute(
                text("""SELECT COUNT(DISTINCT am.id) FROM agent_memories am
                        WHERE am.agent_state_id = :sid AND length(trim(coalesce(am.content, ''))) >= 5 AND EXISTS (
                            SELECT 1 FROM memory_concepts mc
                            WHERE mc.user_id = :uid AND mc.source_type = 'migration'
                              AND mc.source_raw_ids LIKE '%' || 'legacy:agent_memories:' || am.id || '%'
                        )"""),
                {"sid": sid, "uid": user_id},
            )
            covered = covered_r.scalar() or 0
            coverage = covered / total
            if coverage < 0.99:
                failures.append(f"coverage: agent_memories 覆盖率 {coverage:.2%} < 99%")

    return {"passed": not failures, "failures": failures,
            "concept_count": concept_count, "coverage": coverage}


async def migrate_user_dry_run(db: AsyncSession, user_id: str) -> dict:
    """§8.5.4 dry-run：只读统计不写库（各来源条目数/待迁移数/预计 LLM 调用数）。"""
    state_r = await db.execute(
        text("SELECT id, memory_summary, dream_summary FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    row = state_r.fetchone()
    state = {"id": row[0], "memory_summary": row[1], "dream_summary": row[2]} if row else {}
    batch_size = int(config.memory.get("migration_batch_size", 5))

    summary_present = bool(state.get("memory_summary"))
    summary_migrated = summary_present and await _concept_key_exists(db, user_id, "legacy:memory_summary")

    mem_total = mem_pending = 0
    if state.get("id"):
        total_r = await db.execute(
            text("SELECT id, content FROM agent_memories WHERE agent_state_id = :sid"),
            {"sid": state["id"]},
        )
        for mid, content in total_r.fetchall():
            mem_total += 1
            if not content or len(content.strip()) < 5:
                continue
            if not await _concept_key_exists(db, user_id, f"legacy:agent_memories:{mid}"):
                mem_pending += 1

    file_entries = 0
    try:
        from app.tools.memory import _get_memory_dir
        memory_dir = _get_memory_dir() / str(user_id)
        for target in ("USER.md", "AGENT.md"):
            fpath = memory_dir / target
            if fpath.exists():
                content = fpath.read_text(encoding="utf-8")
                file_entries += len([e for e in content.split("\n§\n") if e.strip()])
    except Exception:
        pass

    estimated_calls = (0 if (not summary_present or summary_migrated) else 1) \
        + (mem_pending + batch_size - 1) // batch_size \
        + (1 if file_entries else 0)
    return {
        "user_id": user_id,
        "memory_summary_present": summary_present,
        "memory_summary_migrated": bool(summary_migrated),
        "agent_memories_total": mem_total,
        "agent_memories_pending": mem_pending,
        "file_memory_entries": file_entries,
        "dream_summary_present": bool(state.get("dream_summary")),
        "estimated_llm_calls": estimated_calls,
        "estimated_tokens": estimated_calls * 3000,
    }


async def rollback_user(user_id: str) -> dict:
    """§8.5.5 单用户回滚：删迁移产出 + 清 migration 元数据 + 水位线置 NULL；旧数据只读不动。"""
    async with AsyncSessionLocal() as db:
        acquired = await db.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": _lock_id(user_id)})
        if not acquired.scalar():
            return {"status": "locked"}
        try:
            await db.execute(
                text("""DELETE FROM concept_relations WHERE user_id = :uid AND (
                        source_id IN (SELECT id FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration')
                        OR target_id IN (SELECT id FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration'))"""),
                {"uid": user_id},
            )
            await db.execute(
                text("DELETE FROM concept_cluster_members WHERE concept_id IN (SELECT id FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration')"),
                {"uid": user_id},
            )
            await db.execute(
                text("DELETE FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration'"),
                {"uid": user_id},
            )
            await db.execute(
                text("DELETE FROM memory_episodes WHERE user_id = :uid AND source_type = 'migration'"),
                {"uid": user_id},
            )
            await db.execute(
                text("DELETE FROM memory_clusters WHERE user_id = :uid AND member_count = 0"),
                {"uid": user_id},
            )
            await db.execute(
                text("DELETE FROM agent_dreams WHERE agent_state_id IN (SELECT id FROM user_agent_states WHERE user_id = :uid) AND dream_type = 'legacy'"),
                {"uid": user_id},
            )
            meta, _ = await _load_progress(db, user_id)
            meta.pop("migration", None)
            meta.pop("migration_completed_at", None)
            meta.pop("migrated_concept_count", None)
            meta.pop("migrated_episode_count", None)
            await db.execute(
                text("""UPDATE user_agent_states SET metadata_json = :meta,
                        last_note_processed_at = NULL, last_message_processed_at = NULL,
                        last_file_memory_processed_at = NULL, last_subconscious_scan_at = NULL,
                        last_consolidation_at = NULL, total_concept_count = 0, total_episode_count = 0
                        WHERE user_id = :uid"""),
                {"meta": json.dumps(meta, ensure_ascii=False), "uid": user_id},
            )
            await db.commit()
            return {"status": "rolled_back", "user_id": user_id}
        finally:
            await db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": _lock_id(user_id)})
            await db.commit()


# ---------- 主入口 ----------

async def migrate_user(user_id: str, dry_run: bool = False) -> str:
    async with AsyncSessionLocal() as db:
        if dry_run:
            stats = await migrate_user_dry_run(db, user_id)
            return json.dumps(stats, ensure_ascii=False)

        acquired = await db.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": _lock_id(user_id)})
        if not acquired.scalar():
            return "locked"
        try:
            state_r = await db.execute(
                text("SELECT id, memory_summary, dream_summary, metadata_json FROM user_agent_states WHERE user_id = :uid"),
                {"uid": user_id},
            )
            row = state_r.fetchone()
            if not row:
                return "no_state"
            state = {"id": row[0], "memory_summary": row[1], "dream_summary": row[2]}
            meta = _parse_metadata(row[3])
            if meta.get("migration_completed_at"):
                return "already_migrated"

            progress = meta.get("migration") or _new_progress()
            if progress.get("status") == "failed":
                return "failed"
            next_retry = progress.get("next_retry_at")
            if next_retry:
                try:
                    if datetime.fromisoformat(next_retry) > datetime.utcnow():
                        return "backoff"
                except (ValueError, TypeError):
                    pass

            progress["status"] = "running"
            await _save_progress(db, user_id, meta, progress)
            await db.commit()

            try:
                step_fns = {
                    "memory_summary": lambda: _migrate_memory_summary(db, user_id, state, progress),
                    "agent_memories": lambda: _migrate_agent_memories(db, user_id, state, progress),
                    "file_memory": lambda: _migrate_file_memories(db, user_id, progress),
                    "dream": lambda: _migrate_dream(db, user_id, state, progress),
                    "watermarks": lambda: _init_watermarks(db, user_id, state, progress),
                }
                for idx, step_name in enumerate(_STEPS):
                    progress["current_step"] = idx + 1
                    step_state = progress["steps"].get(step_name, {})
                    if step_state.get("status") in ("done", "skipped"):
                        continue
                    await step_fns[step_name]()
                    await _save_progress(db, user_id, meta, progress)
                    await db.commit()

                recon = await reconcile_migration(db, user_id)
                if not recon["passed"]:
                    return await _record_failure(
                        db, user_id, meta, progress,
                        "reconciliation failed: " + "; ".join(recon["failures"]))

                concept_r = await db.execute(
                    text("SELECT COUNT(*) FROM memory_concepts WHERE user_id = :uid AND source_type = 'migration'"),
                    {"uid": user_id},
                )
                episode_r = await db.execute(
                    text("SELECT COUNT(*) FROM memory_episodes WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                meta["migration_completed_at"] = datetime.utcnow().isoformat()
                meta["migrated_concept_count"] = concept_r.scalar() or 0
                meta["migrated_episode_count"] = episode_r.scalar() or 0
                progress["status"] = "completed"
                await _save_progress(db, user_id, meta, progress)
                await db.commit()
                return "completed"
            except Exception as e:
                await db.rollback()
                return await _record_failure(db, user_id, meta, progress, str(e) or repr(e))
        finally:
            try:
                await db.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": _lock_id(user_id)})
                await db.commit()
            except Exception:
                pass


async def enqueue_pending_migrations() -> dict:
    """§8.3 启动自动排队：对无 migration_completed_at 的用户排队迁移（含崩溃恢复续传）。"""
    if not config.memory.get("migration_enabled", False):
        return {"status": "disabled"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("SELECT user_id, metadata_json FROM user_agent_states")
        )
        candidates: list[str] = []
        for uid, raw in result.fetchall():
            meta = _parse_metadata(raw)
            if meta.get("migration_completed_at"):
                continue
            progress = meta.get("migration") or {}
            if progress.get("status") == "failed":
                continue
            next_retry = progress.get("next_retry_at")
            if next_retry:
                try:
                    if datetime.fromisoformat(next_retry) > datetime.utcnow():
                        continue
                except (ValueError, TypeError):
                    pass
            candidates.append(uid)

    results = {"queued": len(candidates), "completed": 0, "failed": 0, "skipped": 0}
    max_concurrent = int(config.memory.get("migration_max_concurrent", 2))
    sem = asyncio.Semaphore(max_concurrent)

    async with AsyncSessionLocal() as db:
        from app.services.memory_cost_governance_service import _load_level
        degraded = {uid for uid in candidates if await _load_level(db, uid) > 0}
    # §8.5.6：已触发计费降级的用户迁移自动暂停（留 pending 不入队）
    if degraded:
        results["queued"] -= len(degraded)
        results["skipped"] += len(degraded)
    candidates = [uid for uid in candidates if uid not in degraded]

    async def migrate_one(uid: str):
        async with sem:
            try:
                status = await migrate_user(uid)
                if status == "completed":
                    results["completed"] += 1
                elif status in ("already_migrated", "backoff", "locked"):
                    results["skipped"] += 1
                else:
                    results["failed"] += 1
            except Exception:
                logger.exception("Migration failed for user %s", uid)
                results["failed"] += 1

    await asyncio.gather(*[migrate_one(uid) for uid in candidates])
    return results


async def migrate_all_users() -> dict:
    """向后兼容入口：等价于 enqueue_pending_migrations。"""
    return await enqueue_pending_migrations()


async def get_migration_status(user_id: str | None = None) -> list[dict]:
    async with AsyncSessionLocal() as db:
        if user_id:
            result = await db.execute(
                text("SELECT user_id, metadata_json FROM user_agent_states WHERE user_id = :uid"),
                {"uid": user_id},
            )
        else:
            result = await db.execute(text("SELECT user_id, metadata_json FROM user_agent_states"))
        out = []
        for uid, raw in result.fetchall():
            meta = _parse_metadata(raw)
            progress = meta.get("migration") or {}
            out.append({
                "user_id": uid,
                "migration_completed_at": meta.get("migration_completed_at"),
                "migrated_concept_count": meta.get("migrated_concept_count", 0),
                "migrated_episode_count": meta.get("migrated_episode_count", 0),
                "status": progress.get("status", "pending"),
                "current_step": progress.get("current_step", 0),
                "steps": progress.get("steps", {}),
                "attempts": progress.get("attempts", 0),
                "last_error": progress.get("last_error"),
            })
        return out
