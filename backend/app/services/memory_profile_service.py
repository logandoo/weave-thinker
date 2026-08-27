# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""用户画像事实写路径（2026-08-09）：把 v1 每日摘要中的 profile 事实
提炼为 memory_type='profile' 的概念入库，使画像信息走正常 v2 检索管线。

背景（A4.6 根因 4）：迁移未跑时，家庭/孩子/年龄等 profile 事实只存在于
`user_agent_states.memory_summary` 与 `agent_memories.daily-summary`，v2
检索管线从不查询——用户问"给孩子选书"时孩子信息永不出现。上一版修复在
读路径做了运行时关键字扫描 + 行级 embedding（魔法数字 + 每请求 ≤8 次
embed 调用）。本模块把该逻辑移到**写路径**：调度器每日一次用 LLM 从
近 14 天 daily-summary 提炼结构化 profile 事实，upsert 为概念，读路径
退化为查概念 + 恒定基底注入（见 `_get_profile_summary` 简化版）。
"""
import json
import logging
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
    """近 14 天 daily-summary 文本（最新在前）。"""
    result = await db.execute(
        text("""
            SELECT content FROM agent_memories
            WHERE agent_state_id = (SELECT id FROM user_agent_states WHERE user_id = :uid)
              AND source_type = 'daily-summary' AND content IS NOT NULL
              AND created_at >= now() - interval '14 days'
            ORDER BY created_at DESC LIMIT :lim
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
            keys.update(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            if isinstance(raw, str) and raw:
                keys.add(raw)
    return keys


async def _extract_profile_facts(llm, summaries: list[str]) -> list[dict]:
    """LLM 提炼 profile 事实。返回 [{"name", "desc"}]，desc ≤ 80 字符。"""
    if not summaries:
        return []
    now_str = __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).isoformat()
    prompt = (
        "你是用户画像事实提炼助手。从用户近期的每日记忆摘要中，提取**稳定的个人画像事实**："
        "家庭情况（配偶/孩子/父母）、年龄、职业身份、居住地、长期偏好、重要个人事件。\n"
        "规则：\n"
        "1. 只提取与用户个人生活相关的稳定事实，不要提取工作项目细节、技术方案、一次性的对话内容\n"
        "2. 每条事实要自包含（如'用户有两个孩子，老大5岁，喜欢恐龙和乐高绘本'）\n"
        "3. 概念名（name）用短语（≤20字，如'用户的孩子（老大5岁）'）；描述（desc）≤80字\n"
        "4. 输出 JSON 数组：[{\"name\": \"...\", \"desc\": \"...\"}]\n"
        "5. 如果摘要中没有稳定个人画像事实，输出 []\n"
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

    existing_keys = await _load_existing_profile_keys(db, user_id)

    llm = _memory_llm("concept_extraction")
    try:
        facts = await _extract_profile_facts(llm, summaries)
    except Exception:
        logger.exception("profile fact extraction failed for user=%s", user_id)
        return {"error": "extraction_failed"}

    created, updated, skipped = 0, 0, 0
    for fact in facts:
        key = _PROFILE_KEY_PREFIX + fact["name"]
        if not force and key in existing_keys:
            skipped += 1
            continue
        # 已有同名 profile 概念则改写描述；否则新建
        existing = await db.execute(
            text("SELECT id FROM memory_concepts WHERE user_id = :uid AND source_type = :st AND canonical_name = :nm"),
            {"uid": user_id, "st": _PROFILE_SOURCE, "nm": fact["name"]},
        )
        row = existing.fetchone()
        if row:
            await db.execute(
                text("""
                    UPDATE memory_concepts
                    SET description_short = :d, updated_at = NOW()
                    WHERE id = :cid
                """),
                {"d": fact["desc"], "cid": row[0]},
            )
            # 描述变化后需重生成 embedding 与 BM25 索引
            from app.services.memory_concept_service import update_concept_description
            await update_concept_description(db, row[0], fact["desc"], fact["desc"])
            # 修改分支也持久化 key（缺 key 的旧行补写，
            # 否则每次扫描都走修改分支重复 re-embed）
            await db.execute(
                text("UPDATE memory_concepts SET source_raw_ids = :keys WHERE id = :cid AND (source_raw_ids IS NULL OR source_raw_ids = '')"),
                {"keys": json.dumps([key], ensure_ascii=False), "cid": row[0]},
            )
            updated += 1
        else:
            cid = await create_concept(
                db, user_id, fact["name"], fact["desc"], fact["desc"],
                source_trust="user_stated",
                memory_type="profile",
                source_type=_PROFILE_SOURCE,
            )
            if cid:
                await db.execute(
                    text("UPDATE memory_concepts SET source_raw_ids = :keys WHERE id = :cid"),
                    {"keys": json.dumps([key], ensure_ascii=False), "cid": cid},
                )
                created += 1
        existing_keys.add(key)
    await _mark_synced(db, user_id)
    return {"created": created, "updated": updated, "skipped": skipped}


async def get_profile_concepts(db: AsyncSession, user_id: str, limit: int = 6) -> list[dict]:
    """读路径：profile 概念（active，权重降序）。"""
    result = await db.execute(
        text("""
            SELECT canonical_name, description_short FROM memory_concepts
            WHERE user_id = :uid AND memory_type = 'profile'
              AND status = 'active' AND valid_to IS NULL
            ORDER BY weight DESC, created_at DESC LIMIT :lim
        """),
        {"uid": user_id, "lim": limit},
    )
    return [
        {"name": r[0], "desc": r[1]}
        for r in result.fetchall()
    ]
