# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tool: schedule — unified scheduled-task management.

Inspired by hermes-agent's ``cronjob`` tool: a single tool dispatched by
``action`` so creating, listing, cancelling and immediately running a
scheduled task all share one entry point.

IMPORTANT — semantics:
    ``action="create"`` ONLY records the job in the database. It does NOT
    execute the job body in the current chat. The body runs at
    ``next_run_at`` in a fresh scheduled session that has no current-chat
    context, so ``prompt`` must be self-contained. To run the body now,
    the user must explicitly request a separate action.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

from sqlalchemy import select, and_

from app.tools.registry import registry
from app.services.schedule_parser import parse_schedule_agentic
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()
_TZ_CN = timezone(timedelta(hours=8))

_VALID_ACTIONS = {"create", "cancel", "list", "run_now"}
_VALID_DELIVER = {"origin", "silent"}


def _err(msg: str, *, action: str | None = None) -> str:
    payload: Dict[str, Any] = {"success": False, "error": msg}
    if action:
        payload["action"] = action
    return json.dumps(payload, ensure_ascii=False)


def _ok(action: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"success": True, "action": action}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


async def _action_create(args: dict, **kwargs) -> str:
    schedule_text = (args.get("schedule_text") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    duration_text = (args.get("duration_text") or "").strip()
    name = (args.get("name") or "").strip() or None
    deliver = (args.get("deliver") or "origin").strip().lower()
    if deliver not in _VALID_DELIVER:
        deliver = "origin"

    if not schedule_text:
        return _err("缺少 schedule_text 参数", action="create")
    if not prompt:
        return _err("缺少 prompt 参数", action="create")

    parsed = await parse_schedule_agentic(schedule_text, duration_text)
    if not parsed:
        return _err(
            f"无法解析调度表达式: {schedule_text}，请使用如 '每10秒'、'每1分钟'、'每天上午9点' 等格式",
            action="create",
        )

    schedule_type, schedule_expr, next_run_at, repeat_count = parsed
    if not next_run_at:
        next_run_at = datetime.now(_TZ_CN).replace(tzinfo=None)
    if next_run_at.tzinfo:
        next_run_at = next_run_at.replace(tzinfo=None)

    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    assistant = kwargs.get("assistant")
    if not user:
        return _err("无法获取用户信息", action="create")

    from app.db.database import AsyncSessionLocal, ScheduledTask

    task_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as task_db:
        st = ScheduledTask(
            id=task_id,
            user_id=user.id,
            assistant_id=assistant.id if assistant else None,
            conversation_id=conversation.id if conversation else None,
            name=(name or prompt)[:60],
            prompt=prompt,
            schedule_type=schedule_type,
            schedule_expr=schedule_expr,
            next_run_at=next_run_at,
            repeat_count=repeat_count,
            status="active",
        )
        task_db.add(st)
        await task_db.commit()

    logger.info(
        "schedule.create task=%s type=%s expr=%s repeat=%s deliver=%s conv=%s",
        task_id, schedule_type, schedule_expr, repeat_count, deliver,
        conversation.id if conversation else None,
    )

    msg = f"已创建定时任务，将在 {next_run_at.strftime('%Y-%m-%d %H:%M:%S')} 首次执行。"
    if repeat_count:
        msg += f" 共执行 {repeat_count} 次。"
    msg += " 任务届时会在新会话中自动执行，无需现在处理。"

    return _ok(
        "create",
        task_id=task_id,
        schedule_type=schedule_type,
        schedule_expr=schedule_expr,
        next_run_at=next_run_at.isoformat(),
        repeat_count=repeat_count,
        deliver=deliver,
        message=msg,
    )


async def _action_cancel(args: dict, **kwargs) -> str:
    task_id = (args.get("task_id") or "").strip()
    cancel_all = bool(args.get("cancel_all"))
    conversation_scope = bool(args.get("conversation_scope"))

    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    if not user:
        return _err("无法获取用户信息", action="cancel")

    from app.db.database import AsyncSessionLocal, ScheduledTask

    cancelled_ids = []
    async with AsyncSessionLocal() as db:
        if task_id:
            stmt = select(ScheduledTask).where(
                and_(
                    ScheduledTask.id == task_id,
                    ScheduledTask.user_id == user.id,
                    ScheduledTask.status == "active",
                )
            )
            result = await db.execute(stmt)
            task = result.scalar_one_or_none()
            if not task:
                return _err(f"未找到活跃的定时任务 {task_id}，可能已取消或已完成", action="cancel")
            task.status = "cancelled"
            task.next_run_at = None
            cancelled_ids.append(task.id)
        elif cancel_all:
            stmt = select(ScheduledTask).where(
                and_(ScheduledTask.user_id == user.id, ScheduledTask.status == "active")
            )
            result = await db.execute(stmt)
            for t in result.scalars().all():
                t.status = "cancelled"
                t.next_run_at = None
                cancelled_ids.append(t.id)
        elif conversation_scope and conversation:
            stmt = select(ScheduledTask).where(
                and_(
                    ScheduledTask.user_id == user.id,
                    ScheduledTask.conversation_id == conversation.id,
                    ScheduledTask.status == "active",
                )
            )
            result = await db.execute(stmt)
            for t in result.scalars().all():
                t.status = "cancelled"
                t.next_run_at = None
                cancelled_ids.append(t.id)
        else:
            return _err(
                "请指定要取消的 task_id，或设置 cancel_all=true / conversation_scope=true",
                action="cancel",
            )
        await db.commit()

    logger.info(
        "schedule.cancel user=%s cancelled=%d ids=%s",
        user.id, len(cancelled_ids), cancelled_ids,
    )
    return _ok(
        "cancel",
        cancelled_task_ids=cancelled_ids,
        cancelled_count=len(cancelled_ids),
        message=f"已取消 {len(cancelled_ids)} 个定时任务。",
    )


async def _action_list(args: dict, **kwargs) -> str:
    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    conversation_scope = bool(args.get("conversation_scope"))
    if not user:
        return _err("无法获取用户信息", action="list")

    from app.db.database import AsyncSessionLocal, ScheduledTask

    async with AsyncSessionLocal() as db:
        conds = [ScheduledTask.user_id == user.id, ScheduledTask.status == "active"]
        if conversation_scope and conversation:
            conds.append(ScheduledTask.conversation_id == conversation.id)
        stmt = select(ScheduledTask).where(and_(*conds)).order_by(ScheduledTask.next_run_at.asc())
        result = await db.execute(stmt)
        tasks = result.scalars().all()

    rows = [
        {
            "task_id": t.id,
            "name": t.name,
            "schedule_type": t.schedule_type,
            "schedule_expr": t.schedule_expr,
            "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
            "repeat_count": t.repeat_count,
            "prompt_preview": (t.prompt or "")[:100],
        }
        for t in tasks
    ]
    return _ok("list", count=len(rows), tasks=rows)


async def _action_run_now(args: dict, **kwargs) -> str:
    """Mark a task to run on next scheduler tick by setting next_run_at=now."""
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return _err("run_now 需要 task_id", action="run_now")

    user = kwargs.get("user")
    if not user:
        return _err("无法获取用户信息", action="run_now")

    from app.db.database import AsyncSessionLocal, ScheduledTask

    async with AsyncSessionLocal() as db:
        stmt = select(ScheduledTask).where(
            and_(
                ScheduledTask.id == task_id,
                ScheduledTask.user_id == user.id,
                ScheduledTask.status == "active",
            )
        )
        result = await db.execute(stmt)
        task = result.scalar_one_or_none()
        if not task:
            return _err(f"未找到活跃的定时任务 {task_id}", action="run_now")
        task.next_run_at = datetime.now(_TZ_CN).replace(tzinfo=None)
        await db.commit()
        next_run = task.next_run_at.isoformat()

    logger.info("schedule.run_now task=%s user=%s", task_id, user.id)
    return _ok(
        "run_now",
        task_id=task_id,
        next_run_at=next_run,
        message=f"定时任务 {task_id} 已标记为立即执行，将在下一轮调度器 tick 时启动。",
    )


_ACTIONS = {
    "create": _action_create,
    "cancel": _action_cancel,
    "list": _action_list,
    "run_now": _action_run_now,
}


async def schedule(args: dict, **kwargs) -> str:
    """Unified schedule tool entry point. Dispatches on ``args['action']``."""
    action = (args.get("action") or "").strip().lower()
    # Back-compat: older payloads without action but with schedule_text mean create.
    if not action and (args.get("schedule_text") or args.get("prompt")):
        action = "create"
    if action not in _VALID_ACTIONS:
        return _err(
            f"不支持的 action: {action!r}。允许值: create / cancel / list / run_now",
        )
    handler = _ACTIONS[action]
    return await handler(args, **kwargs)


registry.register(
    name="schedule",
    toolset="system",
    schema={
        "name": "schedule",
        "description": (
            "管理定时任务（创建/取消/列出/立即触发）。\n"
            "- action='create': 创建定时任务。**仅记录调度，不会在本轮立即执行任务体**——"
            "任务将在 next_run_at 由调度器在独立的新会话运行，无当前对话上下文，prompt 必须自包含。"
            "创建成功后立即结束本轮回复，不要继续调用 web_search/browser/execute_code 去现场完成任务体。"
            "调度表达式示例：'每20秒' / '每天早上7点' / '每周一上午9点' / '3分钟后' / '2026/05/13 07:00'。\n"
            "- action='cancel': 取消任务（task_id 精确取消，或 cancel_all=true / conversation_scope=true 批量）。\n"
            "- action='list': 列出活跃定时任务（conversation_scope=true 仅当前对话关联）。\n"
            "- action='run_now': 立即触发已创建任务（需 task_id）。仅当用户明确要求'马上跑一次已有的定时任务'时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "cancel", "list", "run_now"],
                    "description": "操作类型",
                },
                "schedule_text": {
                    "type": "string",
                    "description": "[create] 调度表达式，如 '每天早上7点'、'每20秒'、'每周一上午9点'、'3分钟后'。",
                },
                "prompt": {
                    "type": "string",
                    "description": "[create] 任务体内容。必须自包含（新会话里没有当前对话上下文）。例如：'搜索近24小时的科技新闻并整理成摘要'。",
                },
                "duration_text": {
                    "type": "string",
                    "description": "[create] 可选持续时间，如 '2分钟'、'1小时'，会自动计算重复次数。",
                },
                "name": {
                    "type": "string",
                    "description": "[create] 可选任务名（不填用 prompt 前 60 字）。",
                },
                "deliver": {
                    "type": "string",
                    "enum": ["origin", "silent"],
                    "description": "[create] 任务结果投递方式：origin=写回原对话（默认）；silent=仅保存不通知。",
                },
                "task_id": {
                    "type": "string",
                    "description": "[cancel/run_now] 任务 ID。",
                },
                "cancel_all": {
                    "type": "boolean",
                    "description": "[cancel] 取消当前用户所有任务。",
                },
                "conversation_scope": {
                    "type": "boolean",
                    "description": "[cancel/list] 仅作用于当前对话关联的任务。",
                },
            },
            "required": ["action"],
        },
    },
    handler=schedule,
    is_async=True,
    description="管理定时任务",
    emoji="⏰",
)
