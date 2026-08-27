# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
AgentWorker — Background asyncio-based job executor for long-running agent tasks.

Polls the agent_tasks table for pending tasks and runs the AgentLoop
independently of any HTTP request/SSE lifecycle. Tasks continue even
when the user disconnects their browser.

Improvements aligned with hermes-agent:
- Inactivity timeout per task (default 600s)
- Cron-style system prompt hints for background tasks
- Better attachment/result extraction from tool results
"""
import asyncio
import json
import logging
import time as _time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.config import get_config
from app.db.database import AsyncSessionLocal, AgentTask, Conversation, Message, User, Assistant, Notebook, Note
from app.services.agent_loop import AgentLoop
from app.services.llm_service import LLMService
from app.services.agent_service import should_use_custom_model
from app.services.title_generator import TitleGeneratorService
from app.api.chat import _is_scratch_path
from app.services.workspace_service import ensure_user_workspace
from app.api.chat import _transform_tool_loop_results
from app.services.markdown_sanitizer import sanitize_markdown
from app.services.shared_state import shared_state

config = get_config()
logger = logging.getLogger(__name__)

_BACKGROUND_TASK_HINT = (
    "[SYSTEM: You are running as a background research task. "
    "Your final response will be automatically saved as a conversation and note "
    "— do NOT try to deliver the output yourself. Just produce your report/output "
    "as your final response and the system handles the rest. "
    "If you need to generate files (Excel, PPT, Word, etc.), use execute_code. "
    "When uncertain about facts, always search rather than guess. "
    "MARKDOWN: Blank line before headings. Space after #. "
    "Each table row on its own line. No inline bold after heading text.]"
)

_BG_DENIED_OPS = {}


def _make_background_permission_callback():
    def _bg_permission(conversation_id: str, tool_name: str, description: str, details: dict) -> bool:
        from app.services.agent_permissions import permission_key_for_tool_request
        perm_key = permission_key_for_tool_request(tool_name, details) or ""
        if perm_key in ("terminal_execution", "note_delete", "notebook_delete"):
            logger.warning("Background task: denied %s operation (%s)", perm_key, tool_name)
            _BG_DENIED_OPS[conversation_id] = f"后台任务拒绝了 {description}"
            return False
        return True
    return _bg_permission


class AgentWorker:
    """Polls agent_tasks for pending jobs and executes them in background."""

    def __init__(self):
        self._poll_task: asyncio.Task | None = None
        self._running_task_ids: set[str] = set()
        self._worker_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        if not config.agent_background_tasks_enabled:
            logger.info("Background tasks disabled in config")
            return
        # On startup, immediately recover ALL running/claimed tasks —
        # we know no worker is active, so there's no point waiting the
        # stale_recovery_minutes grace period. (For a 5-hour background
        # task, a 10-min delay + "failed" status means lost work that
        # the agent must re-submit from scratch.)
        await self._recover_orphaned_tasks(force=True)
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._run_poll(), name="agent-worker-poll")
            logger.info("AgentWorker started (max_concurrent=%d, poll_interval=%ds)",
                        config.agent_background_tasks_max_concurrent,
                        config.agent_background_tasks_poll_interval)

    async def _recover_orphaned_tasks(self, *, force: bool = False) -> None:
        """Recover tasks stuck in claimed/running.

        On startup (force=True), recovers ALL running/claimed tasks
        immediately — no grace period, because we know no worker is
        active. During normal operation (force=False), only recovers
        tasks older than stale_recovery_minutes (worker might be
        legitimately busy).
        """
        stale_minutes = int(config.agent_background_tasks.get("stale_recovery_minutes", 10))
        recovered = 0
        try:
            async with AsyncSessionLocal() as db:
                for stuck_status in ("claimed", "running"):
                    conditions = [AgentTask.status == stuck_status]
                    if not force:
                        # Normal poll: only recover tasks stale for > N min
                        cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
                        conditions.append(AgentTask.updated_at < cutoff)
                    stmt = (
                        update(AgentTask)
                        .where(*conditions)
                        .values(
                            status="failed",
                            error=(
                                f"Task recovered: backend restarted (was in '{stuck_status}')"
                                if force else
                                f"Task recovered: was stuck in '{stuck_status}' for over {stale_minutes} minutes (worker likely crashed)"
                            ),
                            completed_at=datetime.utcnow(),
                            updated_at=datetime.utcnow(),
                        )
                    )
                    result = await db.execute(stmt)
                    recovered += result.rowcount
                if recovered > 0:
                    await db.commit()
                    logger.warning(
                        "Recovered %d orphaned agent tasks (%s)",
                        recovered,
                        "startup force" if force else f"stale > {stale_minutes} min",
                    )
        except Exception:
            logger.exception("Failed to recover orphaned agent tasks")

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        for task in list(self._worker_tasks):
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()
        logger.info("AgentWorker stopped")

    async def _run_poll(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self._poll_pending_tasks()
            except Exception:
                logger.exception("Error in agent worker poll")
            interval = config.agent_background_tasks_poll_interval
            await asyncio.sleep(interval)

    async def _poll_pending_tasks(self) -> None:
        max_concurrent = config.agent_background_tasks_max_concurrent
        available_slots = max_concurrent - len(self._worker_tasks)
        if available_slots <= 0:
            return

        async with AsyncSessionLocal() as db:
            stmt = (
                select(AgentTask)
                .where(
                    AgentTask.status == "pending",
                    AgentTask.task_type != "grilling",
                )
                .order_by(AgentTask.created_at.asc())
                .limit(available_slots)
                .with_for_update(skip_locked=True)
            )
            result = await db.execute(stmt)
            pending_tasks = result.scalars().all()

            claimed_ids = []
            for task in pending_tasks:
                if task.id in self._running_task_ids:
                    continue
                task.status = "claimed"
                task.worker_id = shared_state.worker_id
                claimed_ids.append(task.id)

            if claimed_ids:
                await db.commit()

            for task_id in claimed_ids:
                self._running_task_ids.add(task_id)
                worker = asyncio.create_task(self._execute_task(task_id), name=f"agent-worker-{task_id[:8]}")
                self._worker_tasks.add(worker)
                worker.add_done_callback(self._on_worker_done)

    def _on_worker_done(self, worker_task: asyncio.Task) -> None:
        self._worker_tasks.discard(worker_task)
        if not worker_task.cancelled():
            try:
                exc = worker_task.exception()
                if exc:
                    logger.error("Agent worker failed: %s", exc)
            except asyncio.InvalidStateError:
                pass

    async def _execute_task(self, task_id: str) -> None:
        try:
            await self._run_task(task_id)
        except Exception:
            logger.exception("Failed to execute background task %s", task_id)
        finally:
            self._running_task_ids.discard(task_id)

    async def _run_task(self, task_id: str) -> None:
        now = datetime.utcnow()
        timeout = config.agent_background_tasks_total_timeout
        inactivity_timeout = float(config.agent_background_tasks.get("inactivity_timeout_seconds", 600))

        async with AsyncSessionLocal() as db:
            task = await db.get(AgentTask, task_id)
            if task is None or task.status not in ("pending", "claimed"):
                return
            task.status = "running"
            task.started_at = now
            await db.commit()

            user = await db.get(User, task.user_id)
            assistant = await db.get(Assistant, task.assistant_id) if task.assistant_id else None
            conversation = None
            if task.conversation_id:
                conversation = await db.get(Conversation, task.conversation_id)

            workspace = await ensure_user_workspace(db, task.user_id, user.username if user else None)

        logger.info("Background task started: %s (goal: %.80s)", task_id, task.goal or "")

        try:
            system_prompt = config.agent.get(
                "background_task_system_prompt",
                "你是一个专业的研究助手。请根据用户的任务，使用搜索和浏览器工具收集信息，最后给出完整的总结。",
            )
            if assistant and assistant.system_prompt:
                system_prompt = assistant.system_prompt
            system_prompt = f"{system_prompt}\n\n{_BACKGROUND_TASK_HINT}"

            # P0: canonical client builder — provider-type routing (incl.
            # qwen3.8_vllm's server-side provider fallback) + process cache.
            from app.services.agent_service import AgentService as _AgentService
            llm = _AgentService().create_llm_service(assistant)
            # P0: aux LLM behavior follows the assistant's model setting.
            if should_use_custom_model(assistant):
                from app.services.auxiliary_client import set_aux_llm_override
                set_aux_llm_override(llm)

            messages = [
                {"role": "system", "content": system_prompt},
            ]

            if task.conversation_id:
                try:
                    async with AsyncSessionLocal() as hist_db:
                        result = await hist_db.execute(
                            select(Message)
                            .where(Message.conversation_id == task.conversation_id)
                            .order_by(Message.created_at)
                        )
                        hist_msgs = result.scalars().all()
                        context_limit = config.agent_conversation_context_limit
                        recent = hist_msgs[-context_limit:] if len(hist_msgs) > context_limit else hist_msgs
                        for m in recent:
                            if m.role in ("user", "assistant") and m.content:
                                _h = {"role": m.role, "content": m.content}
                                if m.role == "assistant":
                                    _rc = getattr(m, "reasoning_content", None)
                                    if _rc:
                                        _h["reasoning_content"] = _rc
                                messages.append(_h)
                except Exception:
                    logger.warning("Failed to load conversation history for background task %s", task_id)

            messages.append({"role": "user", "content": task.goal or ""})

            max_iterations = task.iterations_max or config.agent_tool_loop_max_iterations

            agent_loop = AgentLoop(
                llm=llm,
                max_iterations=max_iterations,
                workspace_path=str(workspace.root_path),
                provider_type=getattr(assistant, "provider_type", "deepseek") or "deepseek",
                enable_reasoning=False,
                enable_compression=config.agent_compression_enabled,
                permission_callback=_make_background_permission_callback(),
            )

            accumulated_response = ""
            accumulated_reasoning = ""
            tool_results_accumulated: list[dict] = []
            search_results_accumulated: list[dict] = []
            browser_results_accumulated: list[dict] = []
            search_queries_used: list[str] = []
            search_queries_by_call: dict[str, list[str]] = {}
            last_progress_update = 0.0
            started_at_ts = asyncio.get_event_loop().time()
            last_activity_ts = _time.monotonic()

            deadline = asyncio.get_event_loop().time() + timeout if timeout > 0 else None
            _last_cancel_check = 0.0
            _timed_out = False
            _cancelled = False

            async for event in agent_loop.run(
                messages,
                user=user,
                conversation=conversation,
                assistant=assistant,
            ):
                if deadline and asyncio.get_event_loop().time() > deadline:
                    logger.warning("Background task %s timed out after %ds", task_id, timeout)
                    _timed_out = True
                    accumulated_response += "\n\n[任务执行时间超出限制，已被截断]"
                    break

                now_mono = _time.monotonic()
                if now_mono - _last_cancel_check > 5.0:
                    _last_cancel_check = now_mono
                    async with AsyncSessionLocal() as _ck_db:
                        try:
                            _ck_task = await _ck_db.get(AgentTask, task_id)
                            if _ck_task and _ck_task.status == "cancelled":
                                logger.warning("Background task %s was cancelled, stopping", task_id)
                                _cancelled = True
                                break
                        except Exception:
                            pass
                if deadline and asyncio.get_event_loop().time() > deadline:
                    logger.warning("Background task %s timed out after %ds", task_id, timeout)
                    _timed_out = True
                    break

                now_mono = _time.monotonic()
                if now_mono - last_activity_ts > inactivity_timeout:
                    logger.warning("Background task %s inactive for %ds, terminating",
                                   task_id, inactivity_timeout)
                    _timed_out = True
                    accumulated_response += "\n\n[任务因长时间无活动被自动终止]"
                    break

                if "content" in event and event["content"]:
                    accumulated_response += event["content"]
                    last_activity_ts = now_mono

                # Audit-rejected draft / audit-budget salvage (conv 97ff355d
                # fix, A4.9 I1): the rejected draft must never concatenate into
                # the persisted result, and the salvage/failure text that
                # follows the reset is the only authoritative content.
                # conv 3b58af5b (2026-08-23, R1/R2): mirror chat.py — discard
                # ONLY the draft text. Reasoning and tool/search accumulators
                # are the turn's real work: post-reject revisions run
                # thinking-off (user decision), so wiping reasoning erased the
                # whole thinking panel from background-task results too.
                if "audit_reset" in event:
                    accumulated_response = ""

                if "reasoning_content" in event and event["reasoning_content"]:
                    accumulated_reasoning += event["reasoning_content"]
                    last_activity_ts = now_mono

                if "tool_call" in event:
                    tc = event["tool_call"]
                    last_activity_ts = now_mono
                    tc_call_id = tc.get("call_id", "")
                    tc_name = tc.get("name", "")
                    tc_args = tc.get("arguments", {})
                    if tc_name == "web_search":
                        queries = tc_args.get("queries", [])
                        if isinstance(queries, list):
                            search_queries_used = list(queries)
                            # Per-call queries for round-level display (conv
                            # a3cfb421 2026-08-09) — mirror chat.py so worker-
                            # persisted conversations show each round's own
                            # queries, not the last call's for every round.
                            search_queries_by_call[tc_call_id or tc.get("id") or ""] = list(queries)
                    tool_results_accumulated.append({
                        "call_id": tc_call_id,
                        "name": tc_name,
                        "arguments": tc_args,
                    })

                if "tool_result" in event:
                    tr = event["tool_result"]
                    last_activity_ts = now_mono
                    tr_call_id = tr.get("call_id", "")
                    tr_name = tr.get("name", "")
                    tr_result = tr.get("result", "")
                    tr_error = tr.get("error", False)
                    for tentry in tool_results_accumulated:
                        if tentry.get("call_id") == tr_call_id:
                            tentry["result"] = tr_result
                            tentry["error"] = tr_error
                            break
                    else:
                        tool_results_accumulated.append({
                            "call_id": tr_call_id,
                            "name": tr_name,
                            "result": tr_result,
                            "error": tr_error,
                        })
                    if tr_name == "web_search" and tr_result and not tr_result.startswith("<tool-digest>"):
                        search_results_accumulated.append({"name": tr_name, "result": tr_result})
                    elif tr_name == "browser" and tr_result:
                        browser_results_accumulated.append({"name": tr_name, "result": tr_result})

                if "iteration" in event:
                    iter_data = event["iteration"]
                    current = iter_data.get("current", 0)
                    max_iter = iter_data.get("max", max_iterations)
                    progress = min(current / max(max_iter, 1), 1.0)
                    elapsed = asyncio.get_event_loop().time() - started_at_ts
                    if elapsed - last_progress_update >= config.agent_background_tasks_progress_update_interval:
                        await self._update_progress(task_id, progress, current, max_iter, elapsed)
                        last_progress_update = elapsed

                if "done" in event or event.get("done"):
                    break

                if "error" in event:
                    accumulated_response += f"\n\n[Error: {event.get('error', '')}]"
                    break

            total_elapsed = asyncio.get_event_loop().time() - started_at_ts

            if _cancelled:
                logger.info("Background task %s cancelled, skipping result save", task_id)
                return

            transformed_tool_results = (
                _transform_tool_loop_results(
                    tool_results_accumulated,
                    search_queries_used=search_queries_used if search_queries_used else None,
                    queries_by_call=search_queries_by_call or None,
                )
                if tool_results_accumulated else None
            )

            all_attachments = self._extract_attachments(tool_results_accumulated)

            async with AsyncSessionLocal() as db:
                task = await db.get(AgentTask, task_id)
                if task is None:
                    return

                if task.status == "cancelled":
                    return

                if _timed_out:
                    task.status = "timed_out"
                else:
                    task.status = "completed"
                raw_result = accumulated_response.strip() or "任务已完成,但未生成文本输出。"
                task.result = sanitize_markdown(raw_result)
                # Citation ledger verify step (grounded-citations port): strip
                # out-of-range [N] before persistence, same as chat_stream.
                # Sanitizes content AND tool_results text surfaces together.
                if transformed_tool_results:
                    from app.api.chat import _sanitize_cited_content
                    task.result, transformed_tool_results = await _sanitize_cited_content(
                        task.result, transformed_tool_results,
                    )
                task.progress = 1.0
                task.iterations_done = max_iterations
                task.elapsed_seconds = total_elapsed
                task.completed_at = datetime.utcnow()
                task.search_results = json.dumps(search_results_accumulated, ensure_ascii=False) if search_results_accumulated else None
                task.browser_results = json.dumps(browser_results_accumulated, ensure_ascii=False) if browser_results_accumulated else None
                task.intermediate_steps = json.dumps(tool_results_accumulated, ensure_ascii=False) if tool_results_accumulated else None

                generated_title = None
                try:
                    # P0: title LLM follows the assistant's model setting.
                    from app.services.agent_service import AgentService as _AgentService
                    title_gen = TitleGeneratorService(
                        **_AgentService().title_generator_kwargs(assistant, llm),
                        provider_type=getattr(assistant, "provider_type", "deepseek") or "deepseek",
                    )
                    generated_title = await title_gen.generate_title(
                        user_query=task.goal or "",
                        assistant_response=task.result or "",
                    )
                except Exception:
                    logger.exception("Title generation failed for task %s", task_id)

                if not generated_title or generated_title == "新对话":
                    from app.services.title_generator import TitleGeneratorService as TGS
                    gen = TGS()
                    generated_title = await gen.get_fallback_title(task.goal or task.result or "")
                if not generated_title:
                    generated_title = (task.goal or "")[:20]

                import uuid

                # If the task originated from a specific conversation, write
                # results back to that conversation instead of creating a new
                # one. This keeps deathmatch and other inline tasks within the
                # same session.
                existing_conv = None
                if task.conversation_id:
                    try:
                        existing_conv = await db.get(Conversation, task.conversation_id)
                    except Exception:
                        pass

                if existing_conv:
                    conv_id = existing_conv.id
                    conv = existing_conv
                    existing_conv.updated_at = datetime.utcnow()
                    # Update title if the conversation still has the default title
                    if existing_conv.title == "新对话" and generated_title and generated_title != "新对话":
                        existing_conv.title = generated_title
                else:
                    conv_id = str(uuid.uuid4())
                    conv = Conversation(
                        id=conv_id,
                        user_id=task.user_id,
                        assistant_id=task.assistant_id,
                        title=generated_title,
                    )
                    db.add(conv)

                # Only add a user message if this is a new conversation.
                # Existing conversations already have the user's original query.
                if not existing_conv:
                    user_msg = Message(
                        id=str(uuid.uuid4()),
                        conversation_id=conv_id,
                        role="user",
                        content=task.goal or "",
                    )
                    db.add(user_msg)

                if all_attachments and transformed_tool_results:
                    try:
                        tr_obj = json.loads(transformed_tool_results)
                        existing = tr_obj.get("attachments", [])
                        seen_names = {a.get("name") or a.get("filename") for a in existing if isinstance(a, dict)}
                        for att in all_attachments:
                            att_name = att.get("name") or att.get("filename") or ""
                            if att_name not in seen_names:
                                existing.append(att)
                                seen_names.add(att_name)
                        tr_obj["attachments"] = existing
                        transformed_tool_results = json.dumps(tr_obj, ensure_ascii=False)
                    except Exception:
                        pass

                # Localize remote media the agent decided to display into the
                # user's workspace (content-addressed sha256) BEFORE building
                # ORM objects, and on a SEPARATE session: ensure_user_workspace
                # commits internally, which must not split the atomic persist
                # below. Best-effort: failures keep the original URLs.
                try:
                    from app.services.media_localizer import localize_message_payload
                    async with AsyncSessionLocal() as ml_db:
                        _ml_user = await ml_db.get(User, task.user_id)
                        _ml_ws = await ensure_user_workspace(
                            ml_db, task.user_id, getattr(_ml_user, "username", None))
                        task.result, transformed_tool_results = await localize_message_payload(
                            task.result, transformed_tool_results, _ml_ws.root_path,
                        )
                except Exception:
                    logger.exception("media localization failed for task %s; keeping original URLs", task_id)

                assistant_msg = Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conv_id,
                    role="assistant",
                    content=task.result,
                    reasoning_content=accumulated_reasoning if accumulated_reasoning else None,
                    tool_results=transformed_tool_results,
                )
                db.add(assistant_msg)

                task.output_conversation_id = conv_id

                try:
                    default_nb = (await db.execute(
                        select(Notebook).where(
                            Notebook.user_id == task.user_id,
                            Notebook.is_default == True,
                        )
                    )).scalar_one_or_none()

                    if default_nb:
                        note_content = f"# {generated_title}\n\n**任务**: {task.goal}\n\n**完成时间**: {task.completed_at}\n\n**结果**:\n\n{task.result}"
                        if all_attachments:
                            note_content += "\n\n**生成文件**:\n"
                            for att in all_attachments:
                                att_name = att.get("name") or att.get("filename") or "unknown"
                                note_content += f"- {att_name}\n"
                        note = Note(
                            id=str(uuid.uuid4()),
                            notebook_id=default_nb.id,
                            title=generated_title,
                            content=note_content,
                        )
                        db.add(note)
                        await db.flush()
                        task.output_note_id = note.id
                        logger.info("Background task %s: note created %s", task_id, note.id)
                except Exception:
                    logger.exception("Failed to create output note for task %s", task_id)

                await db.commit()
                logger.info("Background task completed: %s (%.1fs, %d chars, conv=%s, attachments=%d)",
                            task_id, total_elapsed, len(task.result or ""), conv_id, len(all_attachments))

            # If this task came from 语音助理 (voice) and the user still has a
            # live voice session, hand it to the session so the assistant
            # proactively announces the result and offers follow-up actions
            # (read aloud / export PDF / save to notes).
            try:
                from app.services.voice_service import notify_voice_task_finished
                await notify_voice_task_finished(task_id)
            except Exception:
                logger.debug("voice notify on task completion failed", exc_info=True)

        except asyncio.CancelledError:
            await self._mark_cancelled(task_id)
            raise
        except Exception as exc:
            logger.exception("Background task %s failed", task_id)
            await self._mark_failed(task_id, str(exc))

    def _extract_attachments(self, tool_results_accumulated: list[dict]) -> list[dict]:
        """Same policy as chat._transform_tool_loop_results:

        - execute_code/code_execution auto-collection excludes scratch/task_XXXX
          temp files (intermediates are never deliverables).
        - provide_file entries are the agent's explicit set: kept as-is (no
          scratch filter) and win over auto-collection when present.
        """
        auto_attachments = []
        provided_attachments = []
        seen_paths: set[str] = set()
        for tr in tool_results_accumulated:
            name = tr.get("name", "")
            raw_result = tr.get("result", "")
            if name in ("execute_code", "code_execution", "provide_file") and raw_result:
                try:
                    parsed = json.loads(raw_result)
                    gen_files = parsed.get("generated_files", [])
                    for f in gen_files:
                        if not isinstance(f, dict):
                            continue
                        fpath = f.get("path", "")
                        if not fpath or fpath in seen_paths:
                            continue
                        seen_paths.add(fpath)
                        if name == "provide_file":
                            provided_attachments.append(f)
                        elif not _is_scratch_path(fpath):
                            auto_attachments.append(f)
                except (json.JSONDecodeError, TypeError):
                    pass
        return provided_attachments if provided_attachments else auto_attachments

    def _get_start_time(self, started_at: datetime) -> float:
        return started_at.timestamp()

    async def _update_progress(self, task_id: str, progress: float, iterations_done: int, iterations_max: int, elapsed: float) -> None:
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    update(AgentTask)
                    .where(AgentTask.id == task_id, AgentTask.status == "running")
                    .values(
                        progress=min(progress, 0.99),
                        iterations_done=iterations_done,
                        iterations_max=iterations_max,
                        elapsed_seconds=elapsed,
                        updated_at=datetime.utcnow(),
                    )
                )
                await db.execute(stmt)
                await db.commit()
        except Exception:
            pass

    async def _mark_failed(self, task_id: str, error: str) -> None:
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    update(AgentTask)
                    .where(AgentTask.id == task_id)
                    .values(
                        status="failed",
                        error=error[:5000],
                        completed_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                )
                await db.execute(stmt)
                await db.commit()
        except Exception:
            logger.exception("Failed to mark task %s as failed", task_id)
        try:
            from app.services.voice_service import notify_voice_task_finished
            await notify_voice_task_finished(task_id)
        except Exception:
            logger.debug("voice notify on task failure failed", exc_info=True)

    async def _mark_cancelled(self, task_id: str) -> None:
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    update(AgentTask)
                    .where(AgentTask.id == task_id)
                    .values(
                        status="cancelled",
                        completed_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                )
                await db.execute(stmt)
                await db.commit()
        except Exception:
            logger.exception("Failed to mark task %s as cancelled", task_id)


agent_worker = AgentWorker()
