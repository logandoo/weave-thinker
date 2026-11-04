# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W4-3 程序性记忆（记忆/长程 v2，2026-10-23，判据 #20）。

reflections → 候选技能（**人工门**）→ ``user_skills`` 落库；技能池上限 30。

- 候选拼装是**确定性纯文本**（同类 action_summary/verdict/issues 聚合成技能
  name/description/content），不调 LLM、**不落库**；
- 候选 id = ``sha1(user_id + 名称)[:12]``；
- ``approve_skill_candidate`` 重新 harvest 校验候选存在才落库（人工门）；
  池满（user_skills ≥ 30）拒绝；同名 skill 已存在 → 幂等返回已有行。

注：``approve_skill_candidate`` 返回 ``(Optional[UserSkill], reason)`` 两元组
（reason ∈ created/exists/pool_full/no_candidate）——规格要求「返回 None 并
reason」，裸 Optional 无法区分 409 池满与 404 候选不存在，故加 reason 通道。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from app.db.database import Conversation, UserSkill

logger = logging.getLogger(__name__)

#: 技能池上限（N8 2608.14036：池扩张精度会塌，必须设上限）
SKILL_POOL_CAP = 30

#: 家族关键词表（确定性分类；顺序即优先级）
_SKILL_FAMILIES = (
    ("部署", ("部署", "上线", "发布", "回滚", "docker", "deploy", "发布")),
    ("报告", ("报告", "汇报", "周报", "总结", "report")),
    ("文档", ("文档", "readme", "说明", "doc")),
    ("表格", ("表格", "excel", "xlsx", "csv")),
    ("脚本", ("脚本", "script", "shell", "python 脚本")),
    ("测试", ("测试", "用例", "test", "验证脚本")),
    ("数据", ("数据", "分析", "统计", "data")),
    ("图表", ("图表", "可视化", "echarts", "chart")),
    ("配置", ("配置", "调优", "config", "tuning", "bios")),
    ("调试", ("调试", "排错", "排查", "debug", "修复", "fix")),
)

#: 每个候选最多聚合的反思条目数（内容长度上界）
_MAX_ITEMS_PER_CANDIDATE = 8


def family_of(text: str) -> str:
    """确定性家族归类：首个命中的关键词家族，否则 '综合'。"""
    t = (text or "").lower()
    for family, keys in _SKILL_FAMILIES:
        for k in keys:
            if k.lower() in t:
                return family
    return "综合"


def candidate_id_for(user_id: str, name: str) -> str:
    """候选 id = sha1(user_id + 名称)[:12]。"""
    return hashlib.sha1(f"{user_id}{name}".encode("utf-8")).hexdigest()[:12]


def build_skill_candidates(reflections: List[dict], user_id: str,
                           limit: int = 10) -> List[dict]:
    """纯函数：反思条目（retracted=False）→ 候选技能草稿（确定性拼装）。

    同输入（任意顺序）产出完全一致的候选（含 id/内容/来源）。
    每个候选形如 {"id", "name", "description", "content", "source_reflections"}。
    """
    entries: List[dict] = []
    for e in reflections or []:
        if not isinstance(e, dict):
            continue
        if e.get("retracted"):
            continue
        if not str(e.get("action_summary") or "").strip():
            continue
        entries.append(e)

    groups: Dict[str, List[dict]] = {}
    for e in entries:
        groups.setdefault(family_of(str(e.get("action_summary") or "")), []).append(e)

    def _entry_key(e: dict):
        try:
            turn = int(e.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        return (turn, str(e.get("id") or ""))

    cands: List[dict] = []
    # 家族序：条目多者优先，同数按名称字典序（确定性）
    for fam in sorted(groups, key=lambda f: (-len(groups[f]), f)):
        items = sorted(groups[fam], key=_entry_key)[:_MAX_ITEMS_PER_CANDIDATE]
        name = f"{fam}技能"
        lines: List[str] = []
        for e in items:
            summary = str(e.get("action_summary") or "").strip()
            verdict = str(e.get("verdict") or "").strip()
            issues = "；".join(str(i).strip() for i in (e.get("issues") or [])
                               if str(i).strip())
            retry = str(e.get("retry_instruction") or "").strip()
            line = f"- {summary} → {verdict or '(无判定)'}"
            if issues:
                line += f"：{issues}"
            if retry:
                line += f"；下次：{retry}"
            lines.append(line)
        content = (
            f"## 适用场景\n- {fam}类任务\n\n## 经验条目\n" + "\n".join(lines)
        )
        cands.append({
            "id": candidate_id_for(user_id, name),
            "name": name,
            "description": (f"来自 {len(groups[fam])} 条死磕反思的{fam}类程序性经验"
                            "（判据 #20；人工门通过后落库）"),
            "content": content,
            "source_reflections": [str(e.get("id") or f"turn-{e.get('turn')}")
                                   for e in items],
        })
        if len(cands) >= max(1, int(limit or 10)):
            break
    return cands


def _reflection_entries(raw: Any) -> List[dict]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    try:
        import json
        data = json.loads(raw)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        return []


async def harvest_skill_candidates(db: Any, user_id: str,
                                   limit: int = 10) -> List[dict]:
    """从该用户各会话的 deathmatch_reflections（retracted=False）派生候选技能草稿。

    确定性拼装（build_skill_candidates），不调 LLM、不落库——人工门
    approve 通过才写 user_skills。
    """
    rows = (await db.execute(
        select(Conversation.deathmatch_reflections)
        .where(Conversation.user_id == str(user_id))
        .order_by(Conversation.created_at.asc(), Conversation.id.asc())
    )).scalars().all()
    entries: List[dict] = []
    for raw in rows:
        entries.extend(_reflection_entries(raw))
    return build_skill_candidates(entries, str(user_id), limit=limit)


async def skill_pool_usage(db: Any, user_id: str) -> Dict[str, int]:
    """技能池占用：{"used": n, "cap": 30}。"""
    used = (await db.execute(
        select(func.count()).select_from(UserSkill)
        .where(UserSkill.user_id == str(user_id))
    )).scalar() or 0
    return {"used": int(used), "cap": SKILL_POOL_CAP}


async def approve_skill_candidate(
    db: Any, user_id: str, candidate_id: str
) -> Tuple[Optional[UserSkill], str]:
    """人工门：候选 → user_skills 落库。

    返回 ``(skill, reason)``：
    - ("created")  新建 UserSkill(is_active=True)
    - ("exists")   同名 skill 已存在 → 幂等返回已有行
    - ("pool_full") 池满（user_skills ≥ 30）→ (None, ...)，不落库
    - ("no_candidate") 候选 id 不在当前 harvest 结果里 → (None, ...)
    """
    cands = await harvest_skill_candidates(db, user_id, limit=200)
    cand = next((c for c in cands if c.get("id") == str(candidate_id)), None)
    if cand is None:
        return None, "no_candidate"

    existing = (await db.execute(
        select(UserSkill)
        .where(UserSkill.user_id == str(user_id), UserSkill.name == cand["name"])
    )).scalars().first()
    if existing is not None:
        return existing, "exists"

    usage = await skill_pool_usage(db, user_id)
    if usage["used"] >= usage["cap"]:
        return None, "pool_full"

    row = UserSkill(
        user_id=str(user_id),
        name=cand["name"],
        description=cand.get("description") or "",
        content=cand.get("content") or "",
        is_active=True,
    )
    db.add(row)
    await db.commit()
    try:
        await db.refresh(row)
    except Exception:
        logger.debug("skill refresh skipped", exc_info=True)
    return row, "created"
