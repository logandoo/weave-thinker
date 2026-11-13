# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""用户画像事实写路径（2026-08-09，2026-09-13 输入源改为 v2 产物）：把用户
近期的 v2 情景记忆（memory_episodes.narrative，冷启动兜底 extracted 概念
描述）中的 profile 事实提炼为 memory_type='profile' 的概念入库，使画像信息
走正常 v2 检索管线。

历史：原实现读 v1 `agent_memories.daily-summary`——v2 运行时 v1 生成已停摆
（60 实证：2026-07-29 后零新增 → profile_sync 自 2026-08-12 永久空转）。
2026-09-13 方案 A 退休 v1：输入源改为 v2 自身产物（v1 生成/回落注入在 v2
运行时一并停用；v1 代码保留为 v2 关闭时的回滚网）。
"""
import json
import logging
import re
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.services.memory_concept_service import create_concept
from app.services.memory_llm_factory import _memory_llm

config = get_config()
logger = logging.getLogger(__name__)


def _parse_json_array_lenient(text: str):
    """容错解析 LLM 返回的 JSON 数组（容忍 ```json 围栏与前后散文），
    解析失败返回 None 而非抛异常（2026-08-10：原 json.loads 直解在
    "Unterminated string"/"Extra data" 时抛错，导致当日 profile 提取整体失败）。"""
    t = (text or "").strip()
    if not t:
        return None
    try:
        parsed = json.loads(t)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    lines = [l for l in t.split("\n") if not l.lstrip().startswith("```")]
    joined = "\n".join(lines).strip()
    start, end = joined.find("["), joined.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(joined[start : end + 1])
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            return None
    return None

_PROFILE_SOURCE = "profile_sync"
_PROFILE_KEY_PREFIX = "profile_fact:"


def _profile_key(name: str) -> str:
    """C5：稳定键（归一化名称），不再依赖每日变体名。"""
    from app.services.memory_validity_service import normalize_entity_key
    return _PROFILE_KEY_PREFIX + (normalize_entity_key(name) or name.strip()[:60])
# 每日门控（A4.9 复审 Important #1）：调度器 15 分钟一轮，无门控则每轮触发
# 800-token LLM 提炼（~96× 文档化成本）。last_profile_sync_at 存于
# user_agent_states.metadata_json，距上次同步 < _SYNC_INTERVAL_HOURS 跳过。
_SYNC_INTERVAL_HOURS = 20


async def _last_sync_at(db: AsyncSession, user_id: str) -> float:
    result = await db.execute(
        text("SELECT metadata_json FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    raw = result.scalar()
    if not raw:
        return 0.0
    try:
        return float(json.loads(raw).get("last_profile_sync_at") or 0)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return 0.0


async def _mark_synced(db: AsyncSession, user_id: str) -> None:
    import time as _t
    result = await db.execute(
        text("SELECT metadata_json FROM user_agent_states WHERE user_id = :uid FOR UPDATE"),
        {"uid": user_id},
    )
    raw = result.scalar()
    try:
        meta = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    meta["last_profile_sync_at"] = _t.time()
    await db.execute(
        text("UPDATE user_agent_states SET metadata_json = :meta WHERE user_id = :uid"),
        {"meta": json.dumps(meta, ensure_ascii=False), "uid": user_id},
    )


async def _load_recent_summaries(db: AsyncSession, user_id: str, limit: int = 10) -> list[str]:
    """近 14 天 v2 情景记忆原文（最新在前）——v1 daily-summary 退休后的输入源。

    2026-09-13（v1 退休，方案 A）：v2 运行时 v1 daily-summary 已停摆
    （60 实证 2026-07-29 后零新增 → profile_sync 自 2026-08-12 空转）。
    profile 提炼改读 v2 自身产物；冷启动（无情景记忆）兜底近 14 天
    extracted 概念描述。
    """
    result = await db.execute(
        text("""
            SELECT narrative FROM memory_episodes
            WHERE user_id = :uid AND narrative IS NOT NULL AND valid_to IS NULL
              AND created_at >= now() - interval '14 days'
            ORDER BY created_at DESC LIMIT :lim
        """),
        {"uid": user_id, "lim": limit},
    )
    rows = [r[0].strip() for r in result.fetchall() if r[0] and r[0].strip()]
    if rows:
        return rows
    result = await db.execute(
        text("""
            SELECT description_full FROM memory_concepts
            WHERE user_id = :uid AND source_type = 'extracted' AND valid_to IS NULL
              AND status = 'active' AND description_full IS NOT NULL
              AND (verification_state IS NULL OR verification_state != 'contradicted')
              AND created_at >= now() - interval '14 days'
            ORDER BY importance DESC LIMIT :lim
        """),
        {"uid": user_id, "lim": limit},
    )
    return [r[0].strip() for r in result.fetchall() if r[0] and r[0].strip()]


async def _load_existing_profile_keys(db: AsyncSession, user_id: str) -> set[str]:
    # A4.9 复审 Important #2：只统计未失效概念——soft-delete（valid_to 置位 /
    # 合并）后 key 不再阻塞重建，否则画像事实永久消失无恢复路径。
    result = await db.execute(
        text("SELECT source_raw_ids FROM memory_concepts WHERE user_id = :uid AND source_type = :st AND valid_to IS NULL"),
        {"uid": user_id, "st": _PROFILE_SOURCE},
    )
    keys: set[str] = set()
    for (raw,) in result.fetchall():
        if not raw:
            continue
        try:
            vals = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            vals = [raw] if isinstance(raw, str) and raw else []
        for val in vals if isinstance(vals, list) else []:
            if not isinstance(val, str) or not val:
                continue
            if val.startswith(_PROFILE_KEY_PREFIX):
                keys.add(_profile_key(val[len(_PROFILE_KEY_PREFIX):]))
            else:
                keys.add(val)
    return keys


async def _load_profile_rows(db: AsyncSession, user_id: str) -> list[dict]:
    rows = (await db.execute(
        text("""SELECT id, source_raw_ids, canonical_name, description_short,
                       status, valid_to, source_unit_ids, superseded_by, invalid_reason
                FROM memory_concepts
                WHERE user_id = :uid AND source_type = :st"""),
        {"uid": user_id, "st": _PROFILE_SOURCE},
    )).fetchall()
    return [{
        "id": r[0], "raw_keys": r[1],
        "name": r[2] if len(r) > 2 else "",
        "desc": r[3] if len(r) > 3 else "",
        "status": r[4] if len(r) > 4 else "active",
        "valid_to": r[5] if len(r) > 5 else None,
        "source_unit_ids": r[6] if len(r) > 6 else None,
        "superseded_by": r[7] if len(r) > 7 else None,
        "invalid_reason": r[8] if len(r) > 8 else None,
    } for r in rows]


async def _find_profile_row_by_name(db: AsyncSession, user_id: str,
                                    name: str) -> dict | None:
    """Legacy fallback: match by exact canonical name when no stable key exists."""
    row = (await db.execute(
        text("""SELECT id, source_raw_ids, canonical_name, description_short,
                       status, valid_to, source_unit_ids, superseded_by, invalid_reason
                FROM memory_concepts
                WHERE user_id = :uid AND source_type = :st
                  AND canonical_name = :nm AND status = 'active'
                LIMIT 1"""),
        {"uid": user_id, "st": _PROFILE_SOURCE, "nm": name},
    )).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "raw_keys": row[1] if len(row) > 1 else None,
        "name": row[2] if len(row) > 2 else name,
        "desc": row[3] if len(row) > 3 else "",
        "status": row[4] if len(row) > 4 else "active",
        "valid_to": row[5] if len(row) > 5 else None,
        "source_unit_ids": row[6] if len(row) > 6 else None,
        "superseded_by": row[7] if len(row) > 7 else None,
        "invalid_reason": row[8] if len(row) > 8 else None,
    }


async def _recent_evidence_unit_ids(db: AsyncSession, user_id: str,
                                    limit: int = 10) -> list[str]:
    """B4：给 profile 事实挂最近情景记忆的来源单元，便于 transcript verify。"""
    rows = (await db.execute(
        text("""SELECT source_unit_ids FROM memory_episodes
                WHERE user_id = :uid AND source_unit_ids IS NOT NULL
                  AND valid_to IS NULL
                ORDER BY created_at DESC NULLS LAST LIMIT :lim"""),
        {"uid": user_id, "lim": max(1, int(limit))},
    )).fetchall()
    out: list[str] = []
    for (raw,) in rows:
        try:
            vals = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            vals = []
        for v in vals if isinstance(vals, list) else []:
            if isinstance(v, str) and v and v not in out:
                out.append(v)
    return out[:30]


async def _evidence_units_for_fact(db: AsyncSession, user_id: str,
                                   name: str, desc: str) -> list[str]:
    """I5：只挂与这条画像事实有词面交集的 episode 来源，避免拿无关对话
    给 judge 当 ground truth（否则 confirmed/contradicted 都是偶然）。"""
    tokens = [t for t in re.split(r"[^\w\u4e00-\u9fff]+", f"{name} {desc}")
              if len(t) >= 2][:10]
    if not tokens:
        return []
    rows = (await db.execute(
        text("""SELECT narrative, source_unit_ids FROM memory_episodes
                WHERE user_id = :uid AND source_unit_ids IS NOT NULL
                  AND valid_to IS NULL
                  AND (verification_state IS NULL OR verification_state != 'contradicted')
                ORDER BY created_at DESC NULLS LAST LIMIT 50"""),
        {"uid": user_id},
    )).fetchall()
    out: list[str] = []
    for row in rows:
        narrative = row[0] if row else ""
        raw = row[1] if len(row) > 1 else None
        text_val = str(narrative or "")
        if not any(tok in text_val for tok in tokens):
            continue
        try:
            vals = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            vals = []
        for v in vals if isinstance(vals, list) else []:
            if isinstance(v, str) and v and v not in out:
                out.append(v)
        if len(out) >= 20:
            break
    return out[:20]


async def _extract_profile_facts(llm, summaries: list[str]) -> list[dict]:
    """LLM 提炼 profile 事实。返回 [{"name", "desc"}]，desc ≤ 80 字符。"""
    if not summaries:
        return []
    now_str = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).isoformat()
    prompt = (
        "你是用户画像事实提炼助手。从用户近期的情景记忆与概念摘要中，提取**稳定的个人画像事实**："
        "家庭情况（配偶/孩子/父母）、年龄、职业身份、居住地、长期偏好、重要个人事件。\n"
        "规则：\n"
        "1. 只提取与用户个人生活相关的稳定事实，不要提取工作项目细节、技术方案、一次性的对话内容\n"
        "2. 每条事实要自包含（如'用户有两个孩子，老大5岁，喜欢恐龙和乐高绘本'）\n"
        "3. 概念名（name）用短语（≤20字，如'用户的孩子（老大5岁）'）；描述（desc）≤80字\n"
        "4. 输出 JSON 数组：[{\"name\": \"...\", \"desc\": \"...\"}]\n"
        "5. 如果摘要中没有稳定个人画像事实，输出 []\n"
        "6. 用户自身的角色/性别/家庭关系必须严格按用户陈述保真：用户说'我是妈妈'就写"
        "'用户是…的妈妈'，严禁改写成'爸爸'或性别中性表述；关系保持方向性——"
        "'蛮蛮是弟弟'（年幼男性）不得写成哥哥，'年年是姐姐'（年长女性）不得写成妹妹，"
        "兄弟姐妹的长幼与性别不可互换。\n"
        "7. 主体归属必须精确：'用户'指当前对话的人类本人；AI 助手/产品自身的名字"
        "（如'Weave Thinker'、'小航'、Weave Thinker）指 AI 不是用户——严禁把用户的观点、经历或"
        "身份记到产品名下（如'Weave Thinker认为…'当主体是用户时必须写'用户认为…'），"
        "也严禁把产品名当作用户姓名（除非用户明确自称该名字）。\n"
        f"当前时间: {now_str}\n\n摘要：\n" + "\n\n---\n\n".join(s[:800] for s in summaries[:5])
    )
    resp = await llm.complete_chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请提取用户画像事实。"},
        ],
        temperature=0.1,
    )
    resp = (resp or "").strip()
    if resp.startswith("```"):
        resp = "\n".join(l for l in resp.split("\n") if not l.startswith("```"))
    parsed = _parse_json_array_lenient(resp)
    if parsed is None:
        logger.warning("profile LLM returned unparseable content, skipping this run (len=%d)", len(resp))
        return []
    if not isinstance(parsed, list):
        return []
    facts = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        desc = str(item.get("desc") or "").strip()
        if name and desc:
            facts.append({"name": name[:60], "desc": desc[:80]})
    return facts


async def sync_profile_concepts(db: AsyncSession, user_id: str, force: bool = False) -> dict:
    """每日一次的画像事实同步：LLM 提炼 → upsert 概念（幂等）。

    每日门控（A4.9 复审 Important #1）：调度器 15 分钟一轮，无门控则每轮
    触发 800-token LLM 提炼（~96× 文档化成本）——非 force 且距上次同步
    < 20h 直接跳过。
    """
    import time as _t
    from app.services.memory_cost_governance_service import is_step_enabled
    if not await is_step_enabled(user_id, "profile_sync_off", db):
        return {"skipped": "profile_sync_off"}
    if not force:
        last = await _last_sync_at(db, user_id)
        if last and (_t.time() - last) < _SYNC_INTERVAL_HOURS * 3600:
            return {"skipped": "daily_gate"}

    summaries = await _load_recent_summaries(db, user_id)
    if not summaries:
        await _mark_synced(db, user_id)
        return {"skipped": "no_summaries"}

    profile_rows = await _load_profile_rows(db, user_id)
    existing_by_key: dict[str, dict] = {}
    for row in profile_rows:
        keys: list[str] = []
        try:
            raw = json.loads(row["raw_keys"]) if row["raw_keys"] else []
            keys = [str(k) for k in raw] if isinstance(raw, list) else []
        except (json.JSONDecodeError, TypeError):
            keys = []
        keys.append(_profile_key(row["name"]))
        for key in keys:
            current = existing_by_key.get(key)
            row_is_live = not (row.get("superseded_by") or row.get("status") == "forgotten")
            current_is_live = current is not None and not (
                current.get("superseded_by") or current.get("status") == "forgotten")
            if current is None or (row_is_live and not current_is_live):
                existing_by_key[key] = row
    cap = max(1, int(config.memory.get("profile_concept_cap", 200)))
    profile_count = sum(
        1 for r in profile_rows
        if r.get("valid_to") is None and (r.get("status") or "active") == "active"
        and not r.get("superseded_by")
    )
    processed_keys: set[str] = set()

    llm = _memory_llm("concept_extraction")
    try:
        facts = await _extract_profile_facts(llm, summaries)
        # A4a（2026-09-14）：写路径 LLM 调用入账（计费类）
        # A4.9 修复 F2：savepoint 隔离，入账失败不毒化画像事务
        try:
            from app.services.memory_cost_governance_service import record_llm_call
            async with db.begin_nested():
                await record_llm_call(db, user_id, "profile_sync", billing_class="write")
        except Exception:
            logger.debug("record profile_sync llm call failed", exc_info=True)
    except Exception:
        logger.exception("profile fact extraction failed for user=%s", user_id)
        return {"error": "extraction_failed"}

    created, updated, skipped, capped = 0, 0, 0, 0
    for fact in facts:
        key = _profile_key(fact["name"])
        if key in processed_keys:
            skipped += 1
            continue
        processed_keys.add(key)
        row = existing_by_key.get(key)
        if row is None:
            row = await _find_profile_row_by_name(db, user_id, fact["name"])
        # I2：merged/forgotten/retracted 历史行不得被复活成新的现行版本。
        if row and (row.get("superseded_by") or row.get("status") == "forgotten"
                    or row.get("invalid_reason") == "retracted"):
            skipped += 1
            continue
        fact_units = await _evidence_units_for_fact(
            db, user_id, fact["name"], fact["desc"])
        if row:
            old_desc = row["desc"] or ""
            changed = old_desc != fact["desc"]
            units = []
            try:
                units = json.loads(row["source_unit_ids"]) if row["source_unit_ids"] else []
            except (json.JSONDecodeError, TypeError):
                units = []
            units = list(dict.fromkeys([str(u) for u in units if u] + fact_units))[:30]
            await db.execute(
                text("""
                    UPDATE memory_concepts
                    SET valid_to = NULL, invalid_reason = NULL, needs_review = FALSE,
                        superseded_by = NULL, status = 'active', source_kind = 'profile',
                        validity_updated_at = NOW(),
                        source_unit_ids = :units, updated_at = NOW()
                    WHERE id = :cid
                """),
                {"units": json.dumps(units, ensure_ascii=False), "cid": row["id"]},
            )
            if changed:
                from app.services.memory_concept_service import update_concept_description
                await update_concept_description(db, row["id"], fact["desc"], fact["desc"])
            # A-M4（W12 复审）：profile_fact 键 append 合并。
            try:
                _keys = json.loads(row["raw_keys"]) if row["raw_keys"] else []
                if not isinstance(_keys, list):
                    _keys = [row["raw_keys"]] if row["raw_keys"] else []
            except (json.JSONDecodeError, TypeError):
                _keys = [row["raw_keys"]] if row["raw_keys"] else []
            if key not in _keys:
                _keys.append(key)
            await db.execute(
                text("UPDATE memory_concepts SET source_raw_ids = :keys WHERE id = :cid"),
                {"keys": json.dumps(_keys, ensure_ascii=False), "cid": row["id"]},
            )
            updated += 1
        else:
            if profile_count >= cap:
                # P1-22（2026-11-08）：cap 触顶不再静默 skip——逐条 WARNING +
                # 汇总落 memory_eval_runs(suite="profile_cap")，边界可解释。
                capped += 1
                logger.warning(
                    "profile cap reached user=%s key=%s profile_count=%s cap=%s "
                    "(new fact skipped; existing facts still update)",
                    user_id, key, profile_count, cap)
                continue
            cid = await create_concept(
                db, user_id, fact["name"], fact["desc"], fact["desc"],
                source_trust="user_stated",
                memory_type="profile",
                source_type=_PROFILE_SOURCE,
            )
            if cid:
                await db.execute(
                    text("""UPDATE memory_concepts
                            SET source_raw_ids = :keys, source_kind = 'profile',
                                source_unit_ids = :units
                            WHERE id = :cid"""),
                    {"keys": json.dumps([key], ensure_ascii=False),
                     "units": json.dumps(fact_units, ensure_ascii=False),
                     "cid": cid},
                )
                created += 1
                profile_count += 1
    await _mark_synced(db, user_id)
    if capped:
        # P1-22：触顶指标（health/周报可查；只记计数不记事实内容）。
        try:
            from datetime import datetime as _dt

            from app.db.database import MemoryEvalRun
            db.add(MemoryEvalRun(
                run_id=f"profilecap-{_dt.utcnow().strftime('%Y%m%d%H%M%S')}",
                suite="profile_cap", arm="info", user_id=user_id,
                metrics_json={"capped": capped, "cap": cap,
                              "profile_count": profile_count},
            ))
        except Exception:
            logger.debug("profile cap metrics record failed", exc_info=True)
    return {"created": created, "updated": updated, "skipped": skipped, "capped": capped}


async def get_profile_concepts(db: AsyncSession, user_id: str, limit: int = 6) -> list[dict]:
    """读路径：profile 概念（active，权重降序）。"""
    result = await db.execute(
        text("""
            SELECT canonical_name, description_short FROM memory_concepts
            WHERE user_id = :uid AND memory_type = 'profile'
              AND status = 'active' AND valid_to IS NULL
              AND (verification_state IS NULL OR verification_state != 'contradicted')
            ORDER BY weight DESC, created_at DESC LIMIT :lim
        """),
        {"uid": user_id, "lim": limit},
    )
    return [
        {"name": r[0], "desc": r[1]}
        for r in result.fetchall()
    ]
