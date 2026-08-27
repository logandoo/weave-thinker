# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Natural-language schedule expression parser and next-run calculator.

Agentic design (principle 2026-07-20: 禁止正则/硬编码分类器，语义判断留给 LLM):
- Semantic interpretation of arbitrary schedule phrasings ("每隔3天" / "每周一
  晚上8点" / "下周三下午3点") is done by an LLM (``parse_schedule_agentic``),
  which outputs a structured schedule object.
- Compilation of that structure into cron expressions / next-run datetimes is
  DETERMINISTIC and structural (interval_to_seconds, _next_cron_*, the unit
  maps) — parsing the LLM's structured output is not a judgment.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_TZ_CN = timezone(timedelta(hours=8))


def _strip_tz(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


_SCHEDULE_EXTRACT_PROMPT = (
    "你是定时调度解析器。将用户的自然语言调度描述解析为结构化 JSON。\n"
    "输出格式（只输出JSON）：\n"
    '{"schedule_type": "interval|daily|weekly|once_delay|once_datetime", ...}\n'
    "各类型字段：\n"
    '- interval（每隔N秒/分钟/小时/天/周）: {"amount": N, "unit": "s|m|h|d|w"}\n'
    '- daily（每天X点/每天上午X点/每天下午X点）: {"hour": 0-23, "minute": 0-59}\n'
    '  注意：下午/晚上 3 点 → hour=15；凌晨 0-5 点 → hour 不变\n'
    '- weekly（每周X X点）: {"dow": 0-6, "hour": 0-23, "minute": 0-59}（0=周日，1=周一...6=周六）\n'
    '- once_delay（N分钟后/N小时后/N天后执行一次）: {"delay_amount": N, "delay_unit": "m|h|d"}\n'
    '- once_datetime（明确日期时间，如 2026/05/13 07:00、明天下午3点）: {"year": Y, "month": M, "day": D, "hour": 0-23, "minute": 0-59}\n'
    "附加字段：\n"
    "- duration_seconds（可选）：若同时提供了持续时间（如持续2分钟、2分钟内），输出秒数；否则省略\n"
    "无法解析为任何类型时输出 {\"schedule_type\": \"unknown\"}。\n"
    "只输出JSON，不要输出其他内容。"
)

_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DELAY_UNITS = {"m": 60, "h": 3600, "d": 86400}


async def parse_schedule_agentic(
    schedule_text: str,
    duration_text: str = "",
) -> Optional[Tuple[str, str, Optional[datetime], Optional[int]]]:
    """LLM-judged schedule parsing + deterministic compile.

    Returns ``(schedule_type, schedule_expr, next_run_at, repeat_count)`` —
    schedule_type ∈ {"interval", "cron", "once"}; repeat_count is None when
    no duration window was given. Returns None on LLM failure or unknown
    schedule — the schedule tool reports an error so the agent adjusts.
    """
    from app.services.agentic_judge import judge_json

    text = (schedule_text or "").strip()
    if not text:
        return None
    user_prompt = f"调度描述：\n{text[:300]}"
    if duration_text:
        user_prompt += f"\n持续时间描述：\n{duration_text[:120]}"
    user_prompt += "\n\n只输出JSON。"

    parsed = await judge_json(
        _SCHEDULE_EXTRACT_PROMPT,
        user_prompt,
        task="schedule_parse",
        default=None,
        timeout=20.0,
    )
    if not isinstance(parsed, dict):
        logger.info("schedule parse LLM unavailable — parse failed")
        return None

    stype = str(parsed.get("schedule_type") or "").strip().lower()
    now = datetime.now(_TZ_CN)
    repeat_count: Optional[int] = None

    try:
        if stype == "interval":
            amount = int(parsed.get("amount"))
            unit = str(parsed.get("unit") or "").strip().lower()
            if unit not in _INTERVAL_UNITS or amount <= 0:
                return None
            expr = f"{amount}{unit}"
            next_run = now + timedelta(seconds=_INTERVAL_UNITS[unit] * amount)
            interval_sec = _INTERVAL_UNITS[unit] * amount
        elif stype == "daily":
            hour = int(parsed.get("hour"))
            minute = int(parsed.get("minute") or 0)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            expr = f"{minute} {hour} * * *"
            next_run = _next_cron_daily(hour, minute)
            interval_sec = 86400
            stype = "cron"
        elif stype == "weekly":
            dow = int(parsed.get("dow"))
            hour = int(parsed.get("hour"))
            minute = int(parsed.get("minute") or 0)
            if not (0 <= dow <= 6 and 0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            expr = f"{minute} {hour} * * {dow}"
            next_run = _next_cron_weekly(dow, hour, minute)
            interval_sec = 604800
            stype = "cron"
        elif stype == "once_delay":
            amount = int(parsed.get("delay_amount"))
            unit = str(parsed.get("delay_unit") or "").strip().lower()
            if unit not in _DELAY_UNITS or amount <= 0:
                return None
            delay_sec = _DELAY_UNITS[unit] * amount
            next_run = now + timedelta(seconds=delay_sec)
            return ("once", _strip_tz(next_run).isoformat(), _strip_tz(next_run), None)
        elif stype == "once_datetime":
            year = int(parsed.get("year"))
            month = int(parsed.get("month"))
            day = int(parsed.get("day"))
            hour = int(parsed.get("hour") or 0)
            minute = int(parsed.get("minute") or 0)
            next_run = datetime(year, month, day, hour, minute, tzinfo=_TZ_CN)
            return ("once", _strip_tz(next_run).isoformat(), _strip_tz(next_run), None)
        else:
            logger.info("schedule parse: LLM returned unknown schedule_type=%r", stype)
            return None
    except (TypeError, ValueError) as exc:
        logger.warning("schedule parse compile failed: %s", exc)
        return None

    duration_sec = parsed.get("duration_seconds")
    if isinstance(duration_sec, (int, float)) and duration_sec > 0:
        repeat_count = int(duration_sec) // interval_sec
        if repeat_count <= 0:
            repeat_count = 1

    return (stype, expr, _strip_tz(next_run), repeat_count)


def interval_to_seconds(expr: str) -> int:
    """Convert interval expression like '30m', '2h', '10s' to seconds.

    Parses SYSTEM-GENERATED schedule expressions (structural), not user text.
    """
    import re
    m = re.match(r"(\d+)([smhdw])", expr)
    if not m:
        return 3600
    amount, unit = int(m.group(1)), m.group(2)
    return amount * _INTERVAL_UNITS[unit]


def compute_next_cron_run(expr: str) -> Optional[datetime]:
    """Very simple cron-like next-run: supports 'MIN HOUR * * [DOW]'."""
    parts = expr.split()
    if len(parts) < 5:
        return None
    mi, h = int(parts[0]), int(parts[1])
    dow = parts[4]
    if dow == "*":
        return _next_cron_daily(h, mi)
    return _next_cron_weekly(int(dow), h, mi)


def _next_cron_daily(hour: int, minute: int) -> datetime:
    now = datetime.now(_TZ_CN).replace(tzinfo=None)
    today_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if today_run <= now:
        today_run += timedelta(days=1)
    return today_run


def _next_cron_weekly(dow: int, hour: int, minute: int) -> datetime:
    now = datetime.now(_TZ_CN).replace(tzinfo=None)
    py_dow = (dow - 1) % 7 if dow > 0 else 6
    days_ahead = (py_dow - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(weeks=1)
    return candidate
