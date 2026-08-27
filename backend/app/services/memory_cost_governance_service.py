# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import MemoryLLMCall

config = get_config()
logger = logging.getLogger(__name__)

# 进程内缓存（DB 为权威；缓存仅作 is_step_enabled 无 db 场景兜底）
_user_degrade_state: dict[str, int] = {}


async def record_llm_call(
    db: AsyncSession, user_id: str, kind: str, model: str = "",
    prompt_tokens: int = 0, completion_tokens: int = 0,
) -> None:
    if not config.memory.get("cost_governance_enabled", True):
        return
    call = MemoryLLMCall(
        id=str(uuid.uuid4()),
        user_id=user_id,
        kind=kind,
        model=model or "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    db.add(call)
    await db.flush()


async def _load_cg_meta(db: AsyncSession, user_id: str) -> dict:
    result = await db.execute(
        text("SELECT metadata_json FROM user_agent_states WHERE user_id = :uid"),
        {"uid": user_id},
    )
    raw = result.scalar()
    if not raw:
        return {}
    try:
        meta = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    cg = meta.get("cost_governance")
    return cg if isinstance(cg, dict) else {}


async def _load_level(db: AsyncSession, user_id: str) -> int:
    try:
        return int((await _load_cg_meta(db, user_id)).get("level", 0))
    except (TypeError, ValueError):
        return 0


async def _save_level(db: AsyncSession, user_id: str, level: int,
                     reason: str = "", last_change: str = "") -> None:
    result = await db.execute(
        text("SELECT metadata_json FROM user_agent_states WHERE user_id = :uid FOR UPDATE"),
        {"uid": user_id},
    )
    raw = result.scalar()
    try:
        meta = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    meta["cost_governance"] = {
        "level": level,
        "reason": reason,
        "updated_at": datetime.utcnow().isoformat(),
    }
    if last_change:
        meta["cost_governance"]["last_change"] = last_change
    await db.execute(
        text("UPDATE user_agent_states SET metadata_json = :meta WHERE user_id = :uid"),
        {"meta": json.dumps(meta, ensure_ascii=False), "uid": user_id},
    )
    _user_degrade_state[user_id] = level


async def check_user_threshold_and_degrade(db: AsyncSession, user_id: str) -> int:
    cg = config.memory_cost_governance
    if not config.memory.get("cost_governance_enabled", True):
        return 0
    if not cg.get("per_user_independent", True):
        return 0

    rolling_days = max(int(cg.get("rolling_window_days", 7)), 1)
    warn_mult = float(cg.get("warn_multiplier", 1.5))
    # 2026-08-25 修复：绝对下限。产品正常节奏每天就有 1-4 次记忆 LLM 调用
    # （午夜 dream + 每会话 subconscious extract），当 7 日均值本身很低（~1）时，
    # 纯相对阈值 avg×warn_mult≈1.5 使普通天也触发升级，且降级条件（见下）
    # 永远无法满足 → level 长期卡在 L1/L2（60 服务器实证：正常 4 调用/天
    # 触发 L1，rerank 被降级数周）。有下限后只有真失控 burst
    # （迁移 41 / 回填 37）才会升级。
    min_today_calls = float(cg.get("min_today_calls", 8))
    recovery_ratio = float(cg.get("recovery_ratio", 1.0))
    degrade_steps = cg.get("degrade_steps", [])

    result = await db.execute(
        text("SELECT COUNT(*) FROM memory_llm_calls WHERE user_id = :uid AND created_at >= :since"),
        {"uid": user_id, "since": datetime.utcnow() - timedelta(days=rolling_days)},
    )
    total_calls = result.scalar() or 0
    daily_avg = total_calls / rolling_days

    result = await db.execute(
        text("SELECT COUNT(*) FROM memory_llm_calls WHERE user_id = :uid AND created_at >= :since"),
        {"uid": user_id, "since": datetime.utcnow() - timedelta(days=1)},
    )
    today_calls = result.scalar() or 0

    current_level = await _load_level(db, user_id)
    _user_degrade_state[user_id] = current_level
    max_level = max(len(degrade_steps), 1)

    escalate_floor = max(daily_avg * warn_mult, min_today_calls)
    if today_calls > escalate_floor and current_level < max_level:
        new_level = current_level + 1
        reason = (f"today {today_calls} > 7d avg {daily_avg:.1f} x {warn_mult} "
                  f"(floor {min_today_calls:.0f})")
        await _save_level(db, user_id, new_level, reason,
                          last_change=f"L{current_level}->L{new_level} {reason}")
        logger.warning("Cost governance: user %s degraded to level %d (%s)",
                       user_id, new_level, reason)
        return new_level

    # 2026-08-25 修复（A4.9 I1 终版）：恢复条件 = today ≤ avg × recovery_ratio
    # （ratio 默认 1.0："今天回到不超 7 日均值即视为恢复正常节奏"）。
    # 旧条件 today < avg×0.5 在稳态非零使用日不可达（R 次/天用户 avg→R、
    # today≈R 恒 ≥ R×0.5——burst 后每日常规 1-2 次的用户永久卡死）；
    # 完全静默（avg=0）被 today=0 ≤ 0 自然蕴含（24h 窗 ⊂ 7 日窗 ⇒ avg=0 时
    # today 必为 0），无需特判分支。
    # ②降级时保留原 reason（它解释的是"为何有降级"），把降级动作记到
    # last_change——旧实现把 reason 覆写成 "usage recovered"，
    # UI"触发原因"行显示自相矛盾。
    if current_level > 0 and today_calls <= daily_avg * recovery_ratio:
        new_level = current_level - 1
        keep_reason = (await _load_cg_meta(db, user_id)).get("reason") \
            or "历史降级（无触发记录）"
        await _save_level(db, user_id, new_level, keep_reason,
                          last_change=(f"L{current_level}->L{new_level} usage recovered "
                                       f"({datetime.utcnow().isoformat()})"))
        logger.info("Cost governance: user %s restored to level %d", user_id, new_level)
        return new_level

    return current_level


async def is_step_enabled(user_id: str, step_name: str, db: AsyncSession | None = None) -> bool:
    if not config.memory.get("cost_governance_enabled", True):
        return True
    cg = config.memory_cost_governance
    degrade_steps = cg.get("degrade_steps", [])
    if db is not None:
        level = await _load_level(db, user_id)
        _user_degrade_state[user_id] = level
    else:
        level = _user_degrade_state.get(user_id, 0)
    disabled = set(degrade_steps[:level])
    return step_name not in disabled


async def reset_user_degrade(user_id: str, db: AsyncSession | None = None) -> None:
    if db is not None:
        current = await _load_level(db, user_id)
        await _save_level(db, user_id, 0, "manual reset",
                          last_change=f"L{current}->L0 manual reset "
                                      f"({datetime.utcnow().isoformat()})")
    _user_degrade_state[user_id] = 0
    logger.info("Cost governance: user %s reset to level 0", user_id)


async def get_user_degrade_status(db: AsyncSession, user_id: str) -> dict:
    cg = config.memory_cost_governance
    rolling_days = max(int(cg.get("rolling_window_days", 7)), 1)
    degrade_steps = cg.get("degrade_steps", [])
    cg_meta = await _load_cg_meta(db, user_id)
    try:
        level = int(cg_meta.get("level", 0))
    except (TypeError, ValueError):
        level = 0
    _user_degrade_state[user_id] = level
    reason = cg_meta.get("reason", "")
    last_change = cg_meta.get("last_change", "")

    result = await db.execute(
        text("SELECT COUNT(*) FROM memory_llm_calls WHERE user_id = :uid AND created_at >= :since"),
        {"uid": user_id, "since": datetime.utcnow() - timedelta(days=rolling_days)},
    )
    total_calls = result.scalar() or 0
    result = await db.execute(
        text("SELECT COUNT(*) FROM memory_llm_calls WHERE user_id = :uid AND created_at >= :since"),
        {"uid": user_id, "since": datetime.utcnow() - timedelta(days=1)},
    )
    today_calls = result.scalar() or 0

    return {
        "level": level,
        "disabled_steps": list(degrade_steps[:level]),
        "reason": reason,
        "last_change": last_change,
        "today_calls": today_calls,
        "daily_avg_7d": round(total_calls / rolling_days, 2),
        "warn_multiplier": float(cg.get("warn_multiplier", 1.5)),
    }
