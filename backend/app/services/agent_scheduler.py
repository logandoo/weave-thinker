# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
AgentScheduler — Scheduled task executor with cron tick loop.

Improvements aligned with hermes-agent:
- Cron-specific system prompt hint
- Inactivity timeout per scheduled task execution
- Stale task fast-forward (skip overdue recurring tasks beyond grace window)
- At-most-once semantics: tasks are claimed (status='running') in a short
  transaction BEFORE execution, so a restart never re-fires a claimed task
  (interrupted 'running' tasks are reconciled to 'failed' on startup).
- No row locks are held during agent execution, so cancels never block;
  a running task observes cancellation within ~5s and aborts.
"""
import asyncio
import json
import logging
import time as _time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_, func, update

from app.core.config import get_config
from app.db.database import AsyncSessionLocal, User, ScheduledTask, Conversation, Message, Assistant
from app.services.memory_service import generate_user_agent_memory
from app.services.schedule_parser import interval_to_seconds, compute_next_cron_run
from app.services.workspace_service import ensure_user_workspace
from app.services.markdown_sanitizer import sanitize_markdown

config = get_config()
logger = logging.getLogger(__name__)

_TZ_CN = timezone(timedelta(hours=8))

_CRON_HINT = (
    "[SYSTEM: You are running as a scheduled cron task. "
    "DELIVERY: Your final response will be automatically delivered "
    "to the user — do NOT try to deliver the output yourself. "
    "Just produce your report/output as your final response and the system handles the rest. "
    "SILENT: If there is genuinely nothing new to report, respond "
    "with exactly \"[SILENT]\" (nothing else) to suppress delivery. "
    "When uncertain about facts, always search rather than guess. "
    "If you need to generate files, use execute_code. "
    "MARKDOWN: Blank line before headings. Space after #. "
    "Each table row on its own line. No inline bold after heading text.]"
)

_STALE_GRACE_MINUTES = 30
_MAX_SCHEDULED_FAIL_COUNT = 3


class AgentScheduler:
    def __init__(self):
        self._memory_task: asyncio.Task | None = None
        self._cron_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        if not config.scheduler_enabled:
            return
        await self._reconcile_interrupted_tasks()
        if self._memory_task is None:
            self._memory_task = asyncio.create_task(self._run_memory(), name="agent-memory-scheduler")
        if self._cron_task is None:
            self._cron_task = asyncio.create_task(self._run_cron(), name="agent-cron-scheduler")
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._run_cleanup(), name="agent-cleanup-scheduler")

    async def _reconcile_interrupted_tasks(self) -> None:
        """Mark tasks stuck in 'running' (interrupted by a restart) as failed.

        Guarantees at-most-once: a claimed task never re-fires after a restart.
        """
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    update(ScheduledTask)
                    .where(ScheduledTask.status == "running")
                    .values(status="failed", next_run_at=None)
                )
                if result.rowcount:
                    logger.warning(
                        "Reconciled %d scheduled task(s) interrupted by restart (running -> failed)",
                        result.rowcount,
                    )
                await db.commit()
        except Exception:
            logger.exception("Failed to reconcile interrupted scheduled tasks")

    async def stop(self) -> None:
        for task in (self._memory_task, self._cron_task, self._cleanup_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._memory_task = None
        self._cron_task = None
        self._cleanup_task = None

    _memory_semaphore: asyncio.Semaphore | None = None
    _MAX_MEMORY_CONCURRENCY = 5

    async def _refresh_user_memory(self, user_id: str) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await generate_user_agent_memory(db, user_id, force=False)
        except Exception:
            logger.exception("Failed to refresh memory for user %s", user_id)

    async def run_once(self) -> None:
        if self._memory_semaphore is None:
            self._memory_semaphore = asyncio.Semaphore(self._MAX_MEMORY_CONCURRENCY)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User))
            users = result.scalars().all()

        if not users:
            return

        async def _with_semaphore(uid: str) -> None:
            async with self._memory_semaphore:
                await self._refresh_user_memory(uid)

        await asyncio.gather(*(_with_semaphore(u.id) for u in users), return_exceptions=True)

    async def _run_memory(self) -> None:
        from app.services.memory_runtime_state import memory_runtime_enabled
        if memory_runtime_enabled(config):
            logger.info("Memory Scheduler: new memory system enabled, skipping legacy memory refresh")
            return
        if config.scheduler_run_on_startup:
            await self.run_once()
        while True:
            await asyncio.sleep(max(config.scheduler_poll_interval_minutes, 1) * 60)
            await self.run_once()

    async def _run_cron(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self._tick_scheduled_tasks()
            except Exception:
                logger.exception("Error in scheduled task tick")
            min_interval_sec = await self._get_min_active_interval()
            sleep_sec = max(10, min(min_interval_sec, 60))
            await asyncio.sleep(sleep_sec)

    async def _get_min_active_interval(self) -> int:
        try:
            async with AsyncSessionLocal() as db:
                stmt = select(ScheduledTask).where(
                    ScheduledTask.status == "active",
                    ScheduledTask.schedule_type == "interval",
                )
                result = await db.execute(stmt)
                tasks = result.scalars().all()
                if not tasks:
                    return 15
                min_sec = 60
                for t in tasks:
                    secs = interval_to_seconds(t.schedule_expr)
                    if secs < min_sec:
                        min_sec = secs
                return min_sec
        except Exception:
            return 15

    async def _is_first_execution(self, db, conv_id: str, task: ScheduledTask) -> bool:
        if not conv_id:
            return True
        from sqlalchemy import func
        stmt = select(func.count()).select_from(Message).where(
            Message.conversation_id == conv_id,
            Message.role == "assistant",
        )
        result = await db.execute(stmt)
        count = result.scalar() or 0
        return count == 0

    async def _tick_scheduled_tasks(self) -> None:
        now = datetime.now(_TZ_CN).replace(tzinfo=None)
        grace = timedelta(minutes=2)
        max_per_tick = int(get_config().scheduler.get("max_tasks_per_tick", 10))

        # Phase 1 (short tx): claim due tasks by flipping status to 'running'.
        # The row lock is released on commit — the agent execution below holds
        # NO database lock, so concurrent cancels/updates never block.
        claimed: list[ScheduledTask] = []
        async with AsyncSessionLocal() as db:
            stmt = (
                select(ScheduledTask)
                .where(
                    and_(
                        ScheduledTask.status == "active",
                        ScheduledTask.next_run_at != None,
                        ScheduledTask.next_run_at <= now + grace,
                    )
                )
                .order_by(ScheduledTask.next_run_at)
                .limit(max_per_tick)
                .with_for_update(skip_locked=True)
            )
            result = await db.execute(stmt)
            due_tasks = result.scalars().all()

            for task in due_tasks:
                if task.schedule_type != "once":
                    stale_threshold = now - timedelta(minutes=_STALE_GRACE_MINUTES)
                    if task.next_run_at < stale_threshold:
                        logger.warning(
                            "Skipping stale scheduled task '%s' (id=%s, scheduled=%s, now=%s)",
                            task.name, task.id, task.next_run_at, now,
                        )
                        self._advance_next_run(task, now)
                        continue
                task.status = "running"
                claimed.append(task)
            await db.commit()
            for task in claimed:
                await db.refresh(task)
                db.expunge(task)

        # Phase 2: execute claimed tasks concurrently (respecting max_tasks_per_tick).
        async def _execute_one(task):
            try:
                await self._execute_scheduled_task(task, now)
            except Exception:
                logger.exception("Failed to execute scheduled task %s", task.id)
                try:
                    async with AsyncSessionLocal() as fin_db:
                        trow = await fin_db.get(ScheduledTask, task.id)
                        if trow is not None and trow.status == "running":
                            trow.fail_count = (trow.fail_count or 0) + 1
                            if trow.fail_count >= _MAX_SCHEDULED_FAIL_COUNT:
                                trow.status = "failed"
                                trow.next_run_at = None
                                logger.error(
                                    "Scheduled task %s permanently failed after %d retries",
                                    trow.id, trow.fail_count,
                                )
                            else:
                                trow.status = "active"
                            await fin_db.commit()
                except Exception:
                    logger.exception("Also failed to finalize scheduled task %s", task.id)

        if claimed:
            await asyncio.gather(*(_execute_one(t) for t in claimed), return_exceptions=True)

    def _advance_next_run(self, task: ScheduledTask, now: datetime) -> None:
        if task.schedule_type == "once":
            return
        if task.schedule_type == "interval":
            secs = interval_to_seconds(task.schedule_expr)
            task.next_run_at = now + timedelta(seconds=secs)
        elif task.schedule_type == "cron":
            task.next_run_at = compute_next_cron_run(task.schedule_expr)

    async def _is_task_cancelled(self, task_id: str) -> bool:
        """Cheap status probe: anything other than 'running' means the task was
        cancelled/failed externally while executing."""
        try:
            async with AsyncSessionLocal() as chk_db:
                result = await chk_db.execute(
                    select(ScheduledTask.status).where(ScheduledTask.id == task_id)
                )
                return result.scalar_one_or_none() != "running"
        except Exception:
            return False

    async def _execute_scheduled_task(self, task: ScheduledTask, now: datetime) -> None:
        logger.info("Executing scheduled task '%s' (id=%s)", task.name, task.id)

        import uuid

        # Phase 1: Setup — load/create conversation and assistant, add user message.
        # Use a short session and commit/close before the long agent loop.
        setup_scalars: dict = {}
        async with AsyncSessionLocal() as setup_db:
            assistant = None
            if task.assistant_id:
                assistant = await setup_db.get(Assistant, task.assistant_id)

            conv_id = task.conversation_id
            conv = None
            if conv_id:
                conv = await setup_db.get(Conversation, conv_id)
            if conv is None:
                conv_id = str(uuid.uuid4())
                conv = Conversation(
                    id=conv_id,
                    user_id=task.user_id,
                    assistant_id=task.assistant_id,
                    title=f"[定时任务] {task.name}",
                )
                setup_db.add(conv)
                await setup_db.flush()
                task.conversation_id = conv_id

            conv.updated_at = datetime.utcnow()

            run_num = (task.run_count or 0) + 1
            total = task.repeat_count
            now_cn = datetime.now(_TZ_CN)

            user_prompt = task.prompt
            if total:
                user_prompt = (
                    f"[定时任务执行 — 第 {run_num}/{total} 次] "
                    f"当前北京时间: {now_cn.strftime('%Y年%m月%d日 %H:%M:%S')}\n\n"
                    f"{task.prompt}"
                )
            else:
                user_prompt = (
                    f"[定时任务执行 — 第 {run_num} 次] "
                    f"当前北京时间: {now_cn.strftime('%Y年%m月%d日 %H:%M:%S')}\n\n"
                    f"{task.prompt}"
                )

            history_messages = []
            if conv_id:
                result = await setup_db.execute(
                    select(Message)
                    .where(Message.conversation_id == conv_id)
                    .order_by(Message.created_at)
                )
                all_msgs = result.scalars().all()
                cfg = get_config()
                context_limit = cfg.agent_conversation_context_limit
                recent = all_msgs[-context_limit:] if len(all_msgs) > context_limit else all_msgs
                for m in recent:
                    if m.role in ("user", "assistant") and m.content:
                        _h = {"role": m.role, "content": m.content}
                        if m.role == "assistant":
                            _rc = getattr(m, "reasoning_content", None)
                            if _rc:
                                _h["reasoning_content"] = _rc
                        history_messages.append(_h)

            user_msg = Message(
                id=str(uuid.uuid4()),
                conversation_id=conv_id,
                role="user",
                content=user_prompt,
            )
            setup_db.add(user_msg)
            await setup_db.commit()

            setup_scalars = {
                "task_id": task.id,
                "user_id": task.user_id,
                "conv_id": conv_id,
                "assistant_id": task.assistant_id,
                "assistant_custom_api_url": getattr(assistant, "custom_api_url", None),
                "assistant_custom_api_key": getattr(assistant, "custom_api_key", None),
                "assistant_custom_model_name": getattr(assistant, "custom_model_name", None),
                "assistant_provider_type": getattr(assistant, "provider_type", "deepseek") or "deepseek",
                "assistant_system_prompt": getattr(assistant, "system_prompt", None),
                "user_prompt": user_prompt,
                "history_messages": history_messages,
                "run_num": run_num,
                "total": total,
            }

        # Phase 2: Run the agent loop without holding a database connection.
        execution_failed = False
        execution_cancelled = False
        response_text = ""
        full_reasoning = ""
        tool_results_json = None
        content_segments: list[str] = []
        display_sequence: list[dict] = []

        try:
            from app.services.llm_service import LLMService
            from app.services.agent_service import should_use_custom_model
            from app.core.config import get_config as _get_config

            cfg = _get_config()
            use_tools = cfg.agent_tool_loop_max_iterations > 0

            # P0 / A4.9 Important-3: canonical client builder — provider-type
            # routing (qwen3.8_vllm server-side provider fallback) + process
            # cache + preserve_reasoning wiring, aligned with chat.py/worker.
            from app.services.agent_service import AgentService as _AgentService
            if assistant is None:
                # assistant_id is nullable + FK CASCADE — a deleted assistant
                # falls back to the global provider (pre-existing semantics).
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "Scheduled task assistant missing — falling back to global provider")
            llm = _AgentService().create_llm_service(assistant)
            # P0: aux LLM behavior follows the assistant's model setting.
            if should_use_custom_model(assistant):
                from app.services.auxiliary_client import set_aux_llm_override
                set_aux_llm_override(llm)

            _default_scheduled_prompt = cfg.agent.get(
                "scheduled_task_system_prompt",
                "你是一个智能助手，正在执行用户设定的定时任务。请根据任务要求完成操作并给出结果。",
            )
            system_prompt = setup_scalars["assistant_system_prompt"] or _default_scheduled_prompt
            system_prompt = f"{system_prompt}\n\n{_CRON_HINT}"

            inactivity_timeout = float(cfg.agent.get("scheduled_task_inactivity_timeout_seconds", 600))

            if use_tools:
                from app.services.agent_loop import AgentLoop, _strip_dsml_all
                from app.db.database import User as UserModel

                # Minimal user object: load scalars in a short session.
                user_obj = None
                async with AsyncSessionLocal() as user_db:
                    user_obj = await user_db.get(UserModel, setup_scalars["user_id"])

                workspace = None
                async with AsyncSessionLocal() as ws_db:
                    workspace = await ensure_user_workspace(ws_db, setup_scalars["user_id"], user_obj.username if user_obj else None)

                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(setup_scalars["history_messages"])
                messages.append({"role": "user", "content": setup_scalars["user_prompt"]})

                loop = AgentLoop(
                    llm=llm,
                    max_iterations=cfg.agent_tool_loop_max_iterations,
                    workspace_path=str(workspace.root_path),
                    provider_type=setup_scalars["assistant_provider_type"],
                    enable_reasoning=False,
                    enable_compression=cfg.agent_compression_enabled,
                    session_factory=AsyncSessionLocal,
                )

                tool_results_list = []
                last_activity_ts = _time.monotonic()
                last_cancel_check_ts = _time.monotonic()

                assistant_content = ""
                current_reasoning_segment = ""

                async for event in loop.run(
                    messages,
                    user=user_obj,
                    conversation=conv,
                    assistant=assistant,
                ):
                    now_mono = _time.monotonic()

                    if now_mono - last_cancel_check_ts > 5:
                        last_cancel_check_ts = now_mono
                        if await self._is_task_cancelled(setup_scalars["task_id"]):
                            logger.info(
                                "Scheduled task %s cancelled mid-run, aborting",
                                setup_scalars["task_id"],
                            )
                            execution_cancelled = True
                            break

                    if now_mono - last_activity_ts > inactivity_timeout:
                        logger.warning("Scheduled task %s inactive for %ds, terminating",
                                       setup_scalars["task_id"], inactivity_timeout)
                        response_text += "\n\n[任务因长时间无活动被自动终止]"
                        assistant_content += "\n\n[任务因长时间无活动被自动终止]"
                        break

                    if "content" in event and event["content"]:
                        response_text += event["content"]
                        assistant_content += event["content"]
                        last_activity_ts = now_mono
                    elif "audit_reset" in event:
                        # Audit-rejected draft / salvage / best-of selection
                        # (conv 97ff355d + conv 7dc7a0d5): mirror the
                        # chat.py/agent_worker accumulator reset — a rejected
                        # draft that streamed live (live_thinking) must NEVER
                        # concatenate into the persisted result; the content
                        # that follows the reset is the only authoritative text.
                        # (Pre-existing blind spot: the scheduler had no such
                        # branch while chat.py:2296 and agent_worker.py:345 did.)
                        #
                        # A4.9 round-2 finding: this check previously sat
                        # NESTED under `elif "agent_step" in event:` while
                        # every audit_reset emission is a standalone
                        # {"audit_reset": True} dict — dead code since it was
                        # written. Hoisted to the top-level chain.
                        response_text = ""
                        assistant_content = ""
                        _rseg = current_reasoning_segment.strip()
                        if _rseg:
                            display_sequence.append({
                                "type": "reasoning_step",
                                "title": "💭 思考过程",
                                "content": _rseg,
                            })
                            current_reasoning_segment = ""
                        # preserved: full_reasoning / display_sequence /
                        # tool_results_list
                    elif "reasoning_content" in event:
                        full_reasoning += event["reasoning_content"]
                        current_reasoning_segment += event["reasoning_content"]
                        last_activity_ts = now_mono
                    elif "tool_call" in event:
                        rseg = current_reasoning_segment.strip()
                        if rseg:
                            display_sequence.append({
                                "type": "reasoning_step",
                                "title": "💭 思考过程",
                                "content": rseg,
                            })
                            current_reasoning_segment = ""
                        seg_text = _strip_dsml_all(assistant_content).strip()
                        if seg_text:
                            content_segments.append(seg_text)
                            display_sequence.append({"type": "text", "content": seg_text})
                        display_sequence.append({"type": "tool_placeholder"})
                        assistant_content = ""
                        last_activity_ts = now_mono
                    elif "tool_result" in event:
                        tool_results_list.append(event["tool_result"])
                        last_activity_ts = now_mono
                    elif "done" in event or event.get("done"):
                        break
                    elif "error" in event:
                        response_text += f"\n\n[Error: {event.get('error', '')}]"
                        assistant_content += f"\n\n[Error: {event.get('error', '')}]"
                        break

                final_text = _strip_dsml_all(assistant_content).strip()
                if final_text:
                    content_segments.append(final_text)
                    display_sequence.append({"type": "text", "content": final_text})
                rseg = current_reasoning_segment.strip()
                if rseg:
                    display_sequence.append({
                        "type": "reasoning_step",
                        "title": "💭 思考过程",
                        "content": rseg,
                    })

                from app.api.chat import _transform_tool_loop_results
                if tool_results_list:
                    tool_results_json = _transform_tool_loop_results(tool_results_list)

                if tool_results_json and display_sequence:
                    try:
                        tr_obj = json.loads(tool_results_json)
                        a_steps = tr_obj.get("agent_steps", []) or []
                        resolved: list[dict] = []
                        step_idx = 0
                        for item in display_sequence:
                            if item.get("type") in ("text", "reasoning_step"):
                                resolved.append(item)
                            elif item.get("type") == "tool_placeholder":
                                if step_idx < len(a_steps):
                                    step = dict(a_steps[step_idx])
                                    step["type"] = step.get("step_type", "tool")
                                    resolved.append(step)
                                    step_idx += 1
                        while step_idx < len(a_steps):
                            step = dict(a_steps[step_idx])
                            step["type"] = step.get("step_type", "tool")
                            resolved.append(step)
                            step_idx += 1
                        tr_obj["display_sequence"] = resolved
                        tr_obj["content_segments"] = content_segments
                        tool_results_json = json.dumps(tr_obj, ensure_ascii=False)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        logger.exception(
                            "Failed to attach display_sequence for scheduled task %s",
                            setup_scalars["task_id"],
                        )
            else:
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(setup_scalars["history_messages"])
                messages.append({"role": "user", "content": setup_scalars["user_prompt"]})

                response_text, _ = await llm.complete_chat_parts(messages)
                full_reasoning = ""
                tool_results_json = None
        except Exception:
            logger.exception("Agent execution failed for scheduled task %s", setup_scalars.get("task_id", task.id))
            execution_failed = True

        # Phase 3: Save results in a fresh short session.
        if execution_cancelled:
            logger.info(
                "Scheduled task %s was cancelled; discarding output",
                setup_scalars["task_id"],
            )
            return

        async with AsyncSessionLocal() as save_db:
            task_row = await save_db.get(ScheduledTask, setup_scalars["task_id"])
            if task_row is None:
                logger.error("Scheduled task %s not found during save", setup_scalars["task_id"])
                return
            if task_row.status != "running":
                logger.info(
                    "Scheduled task %s no longer running (status=%s) at save time; discarding output",
                    task_row.id, task_row.status,
                )
                return

            if response_text and response_text.strip() == "[SILENT]":
                logger.info("Scheduled task %s produced [SILENT], skipping save", task_row.id)
            elif response_text:
                segs = content_segments or []
                if segs:
                    joined = "\n\n<!-- segment_split -->\n\n".join(segs)
                else:
                    joined = response_text
                sanitized_response = sanitize_markdown(joined)
                # Citation ledger verify step (grounded-citations port): strip
                # out-of-range [N] before persistence, same as chat_stream.
                # Sanitizes content AND tool_results text surfaces together.
                if tool_results_json:
                    from app.api.chat import _sanitize_cited_content
                    sanitized_response, tool_results_json = await _sanitize_cited_content(
                        sanitized_response, tool_results_json,
                    )
                # Localize remote media the agent decided to display into the
                # user's workspace (content-addressed sha256). Best-effort.
                # Separate session: ensure_user_workspace commits internally.
                try:
                    from app.services.media_localizer import localize_message_payload
                    async with AsyncSessionLocal() as ml_db:
                        _ml_user = await ml_db.get(User, setup_scalars["user_id"])
                        _ml_ws = await ensure_user_workspace(
                            ml_db, setup_scalars["user_id"], getattr(_ml_user, "username", None))
                        sanitized_response, tool_results_json = await localize_message_payload(
                            sanitized_response, tool_results_json, _ml_ws.root_path,
                        )
                except Exception:
                    logger.exception("media localization failed for scheduled task %s; keeping original URLs", task_row.id)
                assistant_msg = Message(
                    id=str(uuid.uuid4()),
                    conversation_id=setup_scalars["conv_id"],
                    role="assistant",
                    content=sanitized_response,
                    reasoning_content=full_reasoning or None,
                    tool_results=tool_results_json,
                )
                save_db.add(assistant_msg)

            task_row.conversation_id = setup_scalars["conv_id"]
            task_row.last_run_at = now
            task_row.run_count = (task_row.run_count or 0) + 1
            task_row.fail_count = 0

            if execution_failed:
                task_row.status = "failed"
                if task_row.schedule_type == "once":
                    task_row.next_run_at = None
            elif task_row.schedule_type == "once":
                task_row.status = "completed"
                task_row.next_run_at = None
            elif task_row.repeat_count and task_row.run_count >= task_row.repeat_count:
                task_row.status = "completed"
                task_row.next_run_at = None
            else:
                task_row.status = "active"
                self._advance_next_run(task_row, now)

            await save_db.commit()

    async def _run_cleanup(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self._cleanup_old_tasks()
            except Exception:
                logger.exception("Error in task cleanup")
            try:
                await self._cleanup_tool_result_files()
            except Exception:
                logger.exception("Error in tool result file cleanup")
            cleanup_interval_hours = int(config.scheduler.get("cleanup_interval_hours", 6))
            await asyncio.sleep(cleanup_interval_hours * 3600)

    async def _cleanup_old_tasks(self) -> None:
        agent_task_retention_days = int(config.scheduler.get("agent_task_retention_days", 7))
        scheduled_task_retention_days = int(config.scheduler.get("scheduled_task_retention_days", 30))
        now = datetime.utcnow()

        try:
            async with AsyncSessionLocal() as db:
                from app.db.database import AgentTask

                agent_cutoff = now - timedelta(days=agent_task_retention_days)
                stmt = (
                    select(AgentTask)
                    .where(
                        AgentTask.status.in_(("completed", "failed", "cancelled")),
                        AgentTask.completed_at < agent_cutoff,
                    )
                )
                result = await db.execute(stmt)
                old_agent_tasks = result.scalars().all()
                for t in old_agent_tasks:
                    await db.delete(t)
                if old_agent_tasks:
                    await db.commit()
                    logger.info("Cleaned up %d old agent tasks (completed > %d days)", len(old_agent_tasks), agent_task_retention_days)

                scheduled_cutoff = now - timedelta(days=scheduled_task_retention_days)
                stmt2 = (
                    select(ScheduledTask)
                    .where(
                        ScheduledTask.status.in_(("completed", "failed", "cancelled")),
                        ScheduledTask.updated_at < scheduled_cutoff,
                    )
                )
                result2 = await db.execute(stmt2)
                old_scheduled_tasks = result2.scalars().all()
                for t in old_scheduled_tasks:
                    await db.delete(t)
                if old_scheduled_tasks:
                    await db.commit()
                    logger.info("Cleaned up %d old scheduled tasks (ended > %d days)", len(old_scheduled_tasks), scheduled_task_retention_days)
        except Exception:
            logger.exception("Failed to cleanup old tasks")

    async def _cleanup_tool_result_files(self) -> None:
        """Delete tool result files older than configurable TTL (P1 item 4.1).

        Scans user_workspaces/*/tool_results/ and backend/output_files/tool_results/.
        Default TTL: 168 hours (7 days). Runs as part of the existing cleanup loop.
        """
        import os as _os

        ttl_hours = int(config.scheduler.get("tool_result_ttl_hours", 168))
        now = _time.time()
        cutoff = now - (ttl_hours * 3600)
        total_deleted = 0
        total_bytes = 0

        scan_roots: list[str] = []

        ws_root = _os.path.join(str(config.project_root), "user_workspaces")
        if _os.path.isdir(ws_root):
            for user_dir in _os.listdir(ws_root):
                tr_dir = _os.path.join(ws_root, user_dir, "tool_results")
                if _os.path.isdir(tr_dir):
                    scan_roots.append(tr_dir)

        output_tr = _os.path.join(str(config.project_root), "backend", "output_files", "tool_results")
        if _os.path.isdir(output_tr):
            scan_roots.append(output_tr)

        for scan_dir in scan_roots:
            try:
                for fname in _os.listdir(scan_dir):
                    fpath = _os.path.join(scan_dir, fname)
                    if not _os.path.isfile(fpath):
                        continue
                    try:
                        mtime = _os.path.getmtime(fpath)
                    except OSError:
                        continue
                    if mtime < cutoff:
                        try:
                            fsize = _os.path.getsize(fpath)
                            _os.remove(fpath)
                            total_deleted += 1
                            total_bytes += fsize
                        except OSError:
                            logger.warning("Failed to delete old tool result: %s", fpath)
            except OSError:
                logger.warning("Failed to scan tool result directory: %s", scan_dir)

        if total_deleted > 0:
            size_mb = total_bytes / (1024 * 1024)
            logger.info(
                "Tool result cleanup: deleted %d files (%.1f MB) older than %d hours",
                total_deleted, size_mb, ttl_hours,
            )


agent_scheduler = AgentScheduler()
