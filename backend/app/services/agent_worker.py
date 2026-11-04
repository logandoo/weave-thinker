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
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update, text, or_, func

from app.core.config import get_config
from app.db.database import AsyncSessionLocal, AgentTask, Conversation, Message, User, Assistant, Notebook, Note
from app.services.agent_loop import AgentLoop
from app.services.llm_service import LLMService
from app.services.agent_service import should_use_custom_model
from app.services.title_generator import TitleGeneratorService
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

# Tools that control OTHER delegated tasks. A running background/delegated task
# must never call them: it could forge a user-role steer into another C2 or
# cancel unrelated work (review W2-I3). `request_user_input` stays allowed
# because it only parks the caller's own task.
_TASK_CONTROL_BLOCKED_TOOLS = frozenset({
    "list_agent_tasks",
    "get_agent_task",
    "cancel_agent_task",
    "steer_agent_task",
    "summarize_agent_task",
    "open_agent_task_result",
})


def _task_messages(system_prompt: str) -> list[dict]:
    """Single system-message construction site for legacy + separate workers."""
    return [{"role": "system", "content": system_prompt}]


def detect_task_pause(payload: Any, tool_name: str = "") -> dict | None:
    """Classify a tool result that must park a separate task (Wave 2).

    - `_task_pause` from `request_user_input` → waiting_input/auth/budget.
    - act-gate denial in a background task → waiting_auth (no approval dialog).
    """
    if not isinstance(payload, dict):
        return None
    pause = payload.get("_task_pause")
    if isinstance(pause, dict):
        kind = str(pause.get("kind") or "input").lower()
        if kind not in ("input", "auth", "budget"):
            kind = "input"
        return {
            "kind": kind,
            "question": str(pause.get("question") or ""),
            "tool_name": None,
            "action_fingerprint": None,
        }
    if payload.get("error") == "act_gate_denied":
        return {
            "kind": "auth",
            "question": f"任务需要授权动作 {tool_name or ''} 才能继续",
            "tool_name": tool_name or None,
            "action_fingerprint": str(payload.get("action_fingerprint") or "") or None,
        }
    return None


def compute_remaining_budget(
    wall_time_seconds: int | float,
    budget_extension_seconds: int | float,
    previous_elapsed_seconds: int | float,
) -> float:
    """Remaining wall-clock budget; <=0 means exhausted (never "unlimited")."""
    return (
        float(wall_time_seconds or 0)
        + float(budget_extension_seconds or 0)
        - float(previous_elapsed_seconds or 0)
    )


def build_cumulative_usage(
    previous: dict, *, last_elapsed: float, iterations: int,
    tool_calls: int, budget_extension: float,
    reasoning_effort: str | None,
) -> dict:
    """Accumulate usage across attempts (§14.1); never fabricate USD cost."""
    previous = previous if isinstance(previous, dict) else {}
    prev_attempts = int(previous.get("attempts") or 0)
    prev_elapsed = float(previous.get("elapsed_seconds") or 0.0)
    prev_iterations = int(previous.get("iterations") or 0)
    prev_tool_calls = int(previous.get("tool_calls") or 0)
    return {
        "attempts": prev_attempts + 1,
        "elapsed_seconds": round(prev_elapsed + float(last_elapsed), 3),
        "last_attempt_elapsed_seconds": round(float(last_elapsed), 3),
        "iterations": max(prev_iterations, int(iterations or 0)),
        "tool_calls": prev_tool_calls + int(tool_calls or 0),
        "budget_extension_seconds": float(budget_extension or 0.0),
        "stages": {
            "main_model_attempts": prev_attempts + 1,
            "tool_iterations": max(prev_iterations, int(iterations or 0)),
            "final_synthesis": 0,
            "title_generation": 0,
            "audit": 0,
            "subtask": 0,
        },
        "requested_reasoning_effort": reasoning_effort,
        "note": "token counts not reported by every provider; cost in USD is not fabricated",
    }


def _enforce_hard_blocked_tools(agent_loop, names: set[str]) -> None:
    """Recursion guard that survives `super_admin_bypass` (§9.3).

    AgentLoop clears `blocked_tools` for super-admin bypass; background
    delegation must still never recursively submit another background/delegated
    task. Re-apply after construction and drop the schemas too.
    """
    agent_loop.blocked_tools = set(agent_loop.blocked_tools or set()) | set(names)
    agent_loop.tool_schemas = [
        schema for schema in (agent_loop.tool_schemas or [])
        if ((schema.get("function") or {}).get("name") not in names)
    ]


async def _recovery_act_verdict(tool_name, args, user, conversation, *, task_id=None):
    """P1b: crash recovery must go through the same act gate (no user prompt).

    Background recovery has no human approval channel, so enforce denies an
    untrusted consequential call instead of replaying it ungated. Wave 2: a
    one-time task permit issued for a `waiting_auth` action is consumed here
    (the recovery path never sees `AgentLoop`'s live approval callback).
    """
    from app.services import act_gate_service, taint_service
    from app.db.database import AsyncSessionLocal

    uid = str(getattr(user, "id", "") or "")
    cid = (
        str(getattr(conversation, "id", "") or "")
        or taint_service.background_conversation_key(uid)
    )
    if task_id:
        from app.services.agent_task_control_service import consume_task_permit

        fingerprint = act_gate_service.action_fingerprint(
            tool_name, args, user_id=uid, conversation_id=cid)
        try:
            async with AsyncSessionLocal() as permit_db:
                if await consume_task_permit(
                    permit_db, task_id=str(task_id), user_id=uid,
                    tool_name=tool_name, action_fingerprint=fingerprint,
                ):
                    try:
                        from app.services import verdict_log_service
                        from app.services.tenant_service import tenant_segment

                        audited = await verdict_log_service.append_verdict(
                            tenant_id=tenant_segment(),
                            user_id=uid,
                            conversation_id=cid,
                            action_fingerprint=fingerprint,
                            tool_name=tool_name,
                            decision="elevate",
                            reason=f"task_permit:{task_id}",
                            taint="untrusted",
                            corroborators=[],
                            mode=config.tma_mode,
                            blocked=False,
                        )
                    except Exception:
                        logger.warning("recovery permit audit failed", exc_info=True)
                        audited = False
                    if not audited:
                        return act_gate_service.ActVerdict(
                            "deny", "permit audit unavailable", "untrusted",
                            [], fingerprint, True, config.tma_mode,
                        )
                    return act_gate_service.ActVerdict(
                        "allow", "task_permit", "agent", [], fingerprint,
                        False, config.tma_mode,
                    )
        except Exception:
            logger.warning("recovery permit consume failed; denying", exc_info=True)
    async with AsyncSessionLocal() as db:
        ledger = await taint_service.load_ledger(db, cid, uid)
    if config.tma_mode == "enforce" and not getattr(ledger, "loaded", False):
        return await act_gate_service.deny_taint_unavailable(
            tool_name, args,
            user_id=uid, conversation_id=cid, mode="enforce",
        )
    return await act_gate_service.evaluate_and_log(
        ledger, tool_name, args,
        user_id=uid, conversation_id=cid, mode=config.tma_mode,
    )


def _make_background_permission_callback():
    def _bg_permission(conversation_id: str, tool_name: str, description: str, details: dict) -> bool:
        from app.services.agent_permissions import permission_key_for_tool_request
        perm_key = details.get("permission_key") or permission_key_for_tool_request(tool_name, details) or ""
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
        # 优雅停机标记——drain 中断的任务落 resumable 非 cancelled
        self._draining = False

    def running_count(self) -> int:
        """部署排空计数（D-12）：在途后台任务数（GET /api/server/active-runs 三源之一）。"""
        return len(self._running_task_ids)

    async def start(self) -> None:
        if not config.agent_background_tasks_enabled:
            logger.info("Background tasks disabled in config")
            return
        self._draining = False  # R3 Minor-1：进程内 stop→start 不留陈旧 drain 标记
        # On startup, immediately recover ALL running/claimed tasks —
        # we know no worker is active, so there's no point waiting the
        # stale_recovery_minutes grace period. (For a 5-hour background
        # task, a 10-min delay + "failed" status means lost work that
        # the agent must re-submit from scratch.)
        await self._recover_orphaned_tasks(force=True)
        await self._recover_orphaned_separate_tasks(force=True)
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
                # 多实例下不踩踏对端活任务——心跳 120s 内的 active
                # worker 视为存活，其名下任务不由本实例恢复（单机模式 alive 为空=旧行为）
                alive_others: list = []
                if shared_state.is_db_enabled:
                    try:
                        _hb_cutoff = _time.time() - 120
                        _r = await db.execute(
                            text("SELECT id FROM worker_instances WHERE status='active' "
                                 "AND last_heartbeat > :cutoff AND id != :me"),
                            {"cutoff": _hb_cutoff, "me": shared_state.worker_id})
                        alive_others = [row[0] for row in _r.fetchall()]
                    except Exception:
                        alive_others = []
                for stuck_status in ("claimed", "running"):
                    conditions = [AgentTask.status == stuck_status]
                    if alive_others:
                        conditions.append(or_(AgentTask.worker_id.is_(None),
                                              AgentTask.worker_id.notin_(alive_others)))
                    if not force:
                        # Normal poll: only recover tasks stale for > N min
                        cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
                        conditions.append(AgentTask.updated_at < cutoff)
                    err_msg = (
                        f"Task recovered: backend restarted (was in '{stuck_status}')"
                        if force else
                        f"Task recovered: was stuck in '{stuck_status}' for over {stale_minutes} minutes (worker likely crashed)"
                    )
                    stmt = (
                        update(AgentTask)
                        .where(*conditions, AgentTask.checkpoint.is_(None),
                               AgentTask.execution_mode.is_(None))
                        .values(
                            status="failed",
                            error=err_msg,
                            completed_at=datetime.utcnow(),
                            updated_at=datetime.utcnow(),
                        )
                    )
                    result = await db.execute(stmt)
                    recovered += result.rowcount
                    # 有 checkpoint → resumable（sweeper 领取续跑），
                    # 无 → failed（旧行为）；completed_at 置空（任务未终结）。
                    stmt_res = (
                        update(AgentTask)
                        .where(*conditions, AgentTask.checkpoint.isnot(None),
                               AgentTask.execution_mode.is_(None))
                        .values(
                            status="resumable",
                            error=f"Task interrupted: checkpoint preserved (was in '{stuck_status}')",
                            completed_at=None,
                            updated_at=datetime.utcnow(),
                        )
                    )
                    result = await db.execute(stmt_res)
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

    async def _recover_orphaned_separate_tasks(self, *, force: bool = False) -> None:
        """Lease-based recovery for separate_v1 tasks (§7.4).

        The legacy ActiveAgentRegistry/updated_at recovery must never touch
        separate tasks; only an expired task-level lease (or startup force)
        makes an attempt recoverable.
        """
        from app.services.agent_task_finalize import finalize_separate_task

        now = datetime.utcnow()
        candidates: list[tuple[str, str, str, str]] = []
        try:
            async with AsyncSessionLocal() as db:
                # Lease expiry is the ONLY normal recovery trigger. `force`
                # means "scan now" but still never steals a valid lease held by
                # a live peer; a crashed process's lease expires in <=180s.
                conditions = [
                    AgentTask.execution_mode == "separate_v1",
                    AgentTask.status.in_(("claimed", "running", "cancelling")),
                    or_(
                        AgentTask.lease_until.is_(None),
                        AgentTask.lease_until < now,
                    ),
                ]
                rows = (await db.execute(
                    select(AgentTask).where(*conditions).with_for_update(skip_locked=True)
                )).scalars().all()
                for row in rows:
                    candidates.append((
                        row.id, row.status, row.attempt_id or "", row.checkpoint or ""))
                await db.commit()
        except Exception:
            logger.exception("separate recovery scan failed")
            return

        for task_id, status, attempt_id, checkpoint in candidates:
            try:
                async with AsyncSessionLocal() as db:
                    # Re-validate under the row lock: the scan snapshot may be
                    # stale (heartbeat renewed or a new attempt claimed).
                    row = (await db.execute(
                        select(AgentTask)
                        .where(AgentTask.id == task_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )).scalar_one_or_none()
                    if row is None:
                        continue
                    if str(row.attempt_id or "") != str(attempt_id or ""):
                        continue
                    if row.status not in ("claimed", "running", "cancelling"):
                        continue
                    if row.lease_until is not None and row.lease_until >= datetime.utcnow():
                        continue
                    if row.status == "cancelling":
                        await finalize_separate_task(
                            db, task_id=task_id, status="cancelled",
                            attempt_id=attempt_id or None, require_attempt=True,
                            require_statuses=("cancelling",),
                            error="取消请求已由控制面收尾（原执行者失联；在途工具结果未知）",
                        )
                        continue
                    if checkpoint:
                        await db.execute(
                            update(AgentTask)
                            .where(
                                AgentTask.id == task_id,
                                AgentTask.status.in_(("claimed", "running")),
                                AgentTask.attempt_id == attempt_id,
                                or_(AgentTask.lease_until.is_(None),
                                    AgentTask.lease_until < datetime.utcnow()),
                            )
                            .values(
                                status="resumable",
                                error="任务中断：检查点已保留，等待新 attempt 恢复",
                                completed_at=None,
                                attempt_id=None,
                                lease_until=None,
                                heartbeat_at=None,
                                status_version=AgentTask.status_version + 1,
                                updated_at=datetime.utcnow(),
                            )
                        )
                        await db.commit()
                        logger.warning("separate task %s recovered → resumable", task_id)
                    else:
                        await finalize_separate_task(
                            db, task_id=task_id, status="failed",
                            attempt_id=attempt_id or None, require_attempt=True,
                            require_statuses=("claimed", "running"),
                            error="任务中断且没有有效检查点；为避免重复副作用，不从头执行",
                        )
                        logger.warning("separate task %s recovered → failed (no checkpoint)", task_id)
            except Exception:
                logger.exception("separate recovery failed for task %s", task_id)

    async def stop(self) -> None:
        # 先置 drain 标记——CancelledError 分支据此落 resumable
        # （此前 _draining 从未置 True，_mark_drained 为死代码）
        self._draining = True
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
        # 停机打断的任务落 resumable（有快照）而非 failed
        try:
            await self._recover_orphaned_tasks(force=True)
            await self._recover_orphaned_separate_tasks(force=True)
        except Exception:
            logger.exception("drain recovery failed")
        logger.info("AgentWorker stopped")

    async def _run_poll(self) -> None:
        await asyncio.sleep(5)
        _recovery_tick = 0
        while True:
            try:
                await self._poll_pending_tasks()
                # 周期陈旧恢复——硬杀对端 worker 的心跳窗口（120s）
                # 过期后，其 running/claimed 任务被 force=False 扫回（stale>N 分钟），
                # 否则仅启动一次的对账会让这些任务永久搁浅。
                _recovery_tick += 1
                if _recovery_tick % 20 == 0:
                    await self._recover_orphaned_tasks(force=False)
                    await self._recover_orphaned_separate_tasks(force=False)
                    await self._reconcile_separate_outbox()
            except Exception:
                logger.exception("Error in agent worker poll")
            interval = config.agent_background_tasks_poll_interval
            await asyncio.sleep(interval)

    async def _reconcile_separate_outbox(self) -> int:
        """Backfill terminal events missed at a transaction edge (§10.1)."""
        try:
            from app.services.voice_notification_service import (
                reconcile_terminal_outbox,
            )

            async with AsyncSessionLocal() as db:
                created = await reconcile_terminal_outbox(db, limit=50)
            if created:
                logger.warning(
                    "separate outbox reconciliation backfilled %d terminal event(s)",
                    created,
                )
            return created
        except Exception:
            logger.exception("separate outbox reconciliation failed")
            return 0

    async def _poll_pending_tasks(self) -> None:
        # 部署排空扩面（D-12，2026-10-06）：drain 中停止领取新任务——在途任务
        # 照常完成（计入排空计数），未领任务留给重启后的新进程（或 TTL 复位后
        # 下轮领取）。进程内优雅停机 _draining 同义（stop() 置位后不再领）。
        from app.services.active_agent_registry import ActiveAgentRegistry
        if self._draining or ActiveAgentRegistry.get_instance().is_draining:
            return

        max_concurrent = config.agent_background_tasks_max_concurrent
        available_slots = max_concurrent - len(self._worker_tasks)
        if available_slots <= 0:
            return

        async with AsyncSessionLocal() as db:
            stmt = (
                select(AgentTask)
                .where(
                    # resumable（有 checkpoint 的被中断任务）与 pending 同等可领
                    AgentTask.status.in_(("pending", "resumable")),
                    AgentTask.task_type != "grilling",
                )
                .order_by(AgentTask.created_at.asc())
                .limit(available_slots)
                .with_for_update(skip_locked=True)
            )
            if not config.voice_delegation_enabled:
                # §15.3 rollback: parked separate tasks must not occupy the
                # claim window and starve legacy background tasks. Filter in
                # SQL, not after the LIMIT.
                stmt = stmt.where(or_(
                    AgentTask.execution_mode.is_(None),
                    AgentTask.execution_mode != "separate_v1",
                ))
            result = await db.execute(stmt)
            pending_tasks = result.scalars().all()

            claimed: list[tuple[str, str | None]] = []
            now = datetime.utcnow()
            for task in pending_tasks:
                if task.id in self._running_task_ids:
                    continue
                # Staged rollout (§15.3): a compatible worker must NOT claim a
                # separate task while the feature is off.
                if getattr(task, "execution_mode", None) == "separate_v1" \
                        and not config.voice_delegation_enabled:
                    continue
                task.status = "claimed"
                task.worker_id = shared_state.worker_id
                if getattr(task, "execution_mode", None) == "separate_v1":
                    task.attempt_id = str(uuid.uuid4())
                    task.lease_until = now + timedelta(
                        seconds=config.voice_delegation_lease_seconds)
                    task.heartbeat_at = now
                    task.status_version = int(task.status_version or 0) + 1
                claimed.append((task.id, getattr(task, "attempt_id", None)))

            if claimed:
                await db.commit()

            for task_id, attempt_id in claimed:
                self._running_task_ids.add(task_id)
                worker = asyncio.create_task(
                    self._execute_task(task_id, attempt_id),
                    name=f"agent-worker-{task_id[:8]}")
                self._worker_tasks.add(worker)
                worker.add_done_callback(self._on_worker_done)

    async def _preflight_pending_tool_calls(self, messages, *, task_id, cursor, user,
                                            conversation, assistant, workspace_path,
                                            permit_fingerprints=None,
                                            blocked_tools=None):
        """恢复时重派 checkpoint 中未回填的 tool_calls。

        语义：done/pending/unknown → 合成已记录结果或跳过（宁可 不双写）；
        无记录 → 执行并回填台账；failed → 允许重试。随后补齐 tool 结果
        消息，主循环从该边界继续（at-least-once）。
        `permit_fingerprints` is precomputed by the worker (task_id -> set of
        (tool, fingerprint)) so this recovery path never opens its own DB
        session; a bare/direct call is fail-closed (no permits).
        """
        import json as _json
        permit_fingerprints = permit_fingerprints or set()
        answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        pending_tc = None
        pending_msg = None
        for m in reversed(messages):
            if m.get("role") == "tool":
                continue
            if m.get("role") == "assistant" and m.get("tool_calls"):
                pending_msg = m
                pending_tc = [tc for tc in m["tool_calls"] if tc.get("id") not in answered]
            break
        if not pending_tc:
            return messages
        from app.services import side_effect_ledger as sel
        from app.services.retry_utils import decide_tool_dispatch, plan_tool_calls_for_round
        from app.tools.registry import registry as _registry
        _perm_cb = _make_background_permission_callback()
        _conv_id = str(getattr(conversation, "id", "") or "")
        store = sel.get_ledger_store()
        out = list(messages)
        # R4 NEW-5/NEW-6（R5 修正）：与活路径同一套解析/修复（plan 先于归一化）——
        # 可修复参数照常派发，仅 plan 判定的截断才合成反馈；并只对**待续的那条**
        # assistant 消息整体出网合法化（含其已回答的旧调用，避免污染更早轮次）。
        _plan, _safe_calls = plan_tool_calls_for_round(pending_tc)
        if pending_msg is not None:
            _, _msg_safe = plan_tool_calls_for_round(list(pending_msg.get("tool_calls") or []))
            if isinstance(_msg_safe, list) and _msg_safe:
                pending_msg["tool_calls"] = _msg_safe
        for tc in pending_tc:
            fn = tc.get("function") or {}
            name = fn.get("name") or "unknown"
            call_id = str(tc.get("id") or "")
            if not call_id:
                continue
            if blocked_tools and name in blocked_tools:
                out.append({
                    "role": "tool", "tool_call_id": call_id, "name": name,
                    "content": _json.dumps({
                        "error": f"工具 {name} 在后台任务中被禁用（恢复重放）",
                        "replay_blocked": True,
                    }, ensure_ascii=False),
                })
                continue
            _args, _args_error = decide_tool_dispatch(_plan, tc)
            if _args_error is not None:
                # 截断调用——合成反馈，绝不派发、不记台账（后续可重试）
                out.append({"role": "tool", "tool_call_id": call_id, "name": name,
                            "content": _args_error})
                continue
            _args = _args if isinstance(_args, dict) else {}
            permit_available = False
            try:
                from app.services import act_gate_service as _gate0
                if _gate0.is_act_tool(name):
                    _fp = _gate0.action_fingerprint(
                        name, _args,
                        user_id=str(getattr(user, "id", "") or ""),
                        conversation_id=_conv_id,
                    )
                    permit_available = (name, _fp) in permit_fingerprints
            except Exception:
                permit_available = False
            if await sel.should_skip(store, "agent_task", task_id, cursor, name, call_id) and not permit_available:
                recorded = await sel.get_result(store, "agent_task", task_id, cursor, name, call_id)
                result_text = recorded or _json.dumps(
                    {"replayed": True, "skipped": "side effect already recorded (crash recovery)"},
                    ensure_ascii=False)
            else:
                # Recorded results above win.  Both permission gates apply
                # only to a call that is actually about to be dispatched.
                try:
                    if not _perm_cb(_conv_id, name, "", dict(_args)):
                        result_text = _json.dumps(
                            {"error": f"Background task: denied {name} operation (recovery gate)"},
                            ensure_ascii=False)
                        out.append({"role": "tool", "tool_call_id": call_id, "name": name,
                                    "content": result_text})
                        continue
                except Exception:
                    pass
                # P1b: only a call that is actually about to be dispatched goes
                # through the act gate.  A recorded (done/pending/unknown)
                # result above must win and is never re-gated.
                try:
                    from app.services import act_gate_service as _ag
                    if _ag.is_act_tool(name):
                        _recovery_verdict = await _recovery_act_verdict(
                            name, _args, user, conversation, task_id=task_id,
                        )
                        if _recovery_verdict.blocked:
                            out.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "name": name,
                                "content": _json.dumps(
                                    _ag.tool_error(_recovery_verdict),
                                    ensure_ascii=False,
                                ),
                            })
                            continue
                except Exception:
                    if config.tma_mode == "enforce":
                        out.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": name,
                            "content": _json.dumps({
                                "error": "act_gate_denied",
                                "reason": "recovery act gate unavailable; fail closed",
                            }, ensure_ascii=False),
                        })
                        continue
                key = await sel.begin(store, "agent_task", task_id, cursor, name, call_id)
                if key is None:
                    result_text = _json.dumps({"replayed": True, "skipped": "ledger key present"},
                                              ensure_ascii=False)
                else:
                    try:
                        result_text = str(await _registry.dispatch(
                            name, _args, user=user, conversation=conversation,
                            assistant=assistant, workspace_path=workspace_path))
                    except Exception as exc:
                        result_text = _json.dumps({"error": f"recovery dispatch failed: {exc}"},
                                                  ensure_ascii=False)
                        await sel.finish(store, key, "failed", result_text)
                    else:
                        await sel.finish(store, key, "done", result_text)
            out.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": result_text})
        logger.info("Background task %s preflight: re-dispatched %d in-flight tool call(s)",
                    task_id, len(pending_tc))
        return out

    def _on_worker_done(self, worker_task: asyncio.Task) -> None:
        self._worker_tasks.discard(worker_task)
        if not worker_task.cancelled():
            try:
                exc = worker_task.exception()
                if exc:
                    logger.error("Agent worker failed: %s", exc)
            except asyncio.InvalidStateError:
                pass

    async def _execute_task(self, task_id: str, attempt_id: str | None = None) -> None:
        try:
            await self._run_task(task_id, attempt_id)
        except Exception:
            logger.exception("Failed to execute background task %s", task_id)
        finally:
            self._running_task_ids.discard(task_id)
            # 用户级模型供应商覆盖：任务结束必须清上下文（worker 任务复用风险）
            from app.model_gateway.user_overrides import reset_active_user_overrides
            reset_active_user_overrides()

    async def _run_task(self, task_id: str, attempt_id: str | None = None) -> None:
        now = datetime.utcnow()
        timeout = config.agent_background_tasks_total_timeout
        inactivity_timeout = float(config.agent_background_tasks.get("inactivity_timeout_seconds", 600))

        async with AsyncSessionLocal() as db:
            task = await db.get(AgentTask, task_id)
            if task is None:
                return
            if getattr(task, "execution_mode", None) == "separate_v1":
                if task.status == "cancelling":
                    await self._finalize_separate_new_session(
                        task_id=task_id, status="cancelled",
                        attempt_id=getattr(task, "attempt_id", None),
                        require_attempt=bool(getattr(task, "attempt_id", None)),
                        require_statuses=("cancelling",),
                        error="取消请求已确认（在启动前到达）",
                    )
                    return
                if task.status not in ("pending", "claimed", "resumable"):
                    return
                await self._run_separate_task(task_id, attempt_id)
                return
            if task.status not in ("pending", "claimed", "resumable"):
                return
            task.status = "running"
            task.started_at = now
            await db.commit()

            user = await db.get(User, task.user_id)
            assistant = await db.get(Assistant, task.assistant_id) if task.assistant_id else None
            conversation = None
            if task.conversation_id:
                conversation = await db.get(Conversation, task.conversation_id)

            # 用户级模型供应商覆盖：后台任务与交互请求同等适用（2026-09-13）
            try:
                from app.services.user_model_provider_service import activate_user_overrides
                await activate_user_overrides(db, task.user_id)
            except Exception:
                logger.exception("activate_user_overrides failed for background task %s", task_id)

            # 2026-11-01 per-user MCP: apply this user's servers before the loop.
            try:
                from app.services.user_mcp_service import ensure_user_mcp_tools
                await ensure_user_mcp_tools(db, task.user_id)
            except Exception:
                logger.exception("ensure_user_mcp_tools failed for background task %s", task_id)

            workspace = await ensure_user_workspace(db, task.user_id, user.username if user else None)

        logger.info("Background task started: %s (goal: %.80s)", task_id, task.goal or "")

        # F1/H2（2026-09-14）：运行中补充消息轮询器（异常路径可安全取消）
        _pending_poller: asyncio.Task | None = None

        try:
            system_prompt = config.agent.get(
                "background_task_system_prompt",
                "你是一个专业的研究助手。请根据用户的任务，使用搜索和浏览器工具收集信息，最后给出完整的总结。",
            )
            if assistant and assistant.system_prompt:
                system_prompt = assistant.system_prompt
            system_prompt = f"{system_prompt}\n\n{_BACKGROUND_TASK_HINT}"

            # E4（2026-09-14，默认关）：后台任务注入小预算用户记忆（fail-open）
            _mem_block = ""
            try:
                from app.services import memory_task_context as _mtc
                if _mtc.background_task_memory_enabled():
                    async with AsyncSessionLocal() as _mem_db:
                        _mem_block = await _mtc.build_task_memory_context(
                            _mem_db, task.user_id, task.goal or "",
                            budget_chars=int(config.agent.get(
                                "background_task_memory_budget_chars", 600)))
                    if _mem_block:
                        system_prompt = f"{system_prompt}\n\n{_mem_block}"
            except Exception:
                logger.debug("background task memory injection failed (fail-open)", exc_info=True)

            # P0: canonical client builder — provider-type routing (incl.
            # qwen3.8_vllm's server-side provider fallback) + process cache.
            from app.services.agent_service import AgentService as _AgentService
            llm = _AgentService().create_llm_service(assistant)
            # P0: aux LLM behavior follows the assistant's model setting.
            if should_use_custom_model(assistant):
                from app.services.auxiliary_client import set_aux_llm_override
                set_aux_llm_override(llm)

            messages = _task_messages(system_prompt)

            if task.conversation_id:
                try:
                    async with AsyncSessionLocal() as hist_db:
                        result = await hist_db.execute(
                            select(Message)
                            .where(Message.conversation_id == task.conversation_id,
                                   Message.delivery_status != "streaming")
                            .order_by(Message.created_at)
                        )
                        hist_msgs = result.scalars().all()
                        # 评审 I3：外置桩还原全文后再进后台任务上下文
                        from app.services.message_payload_service import resolve_message_fields
                        hist_msgs = await resolve_message_fields(list(hist_msgs), hist_db)
                        context_limit = config.agent_conversation_context_limit
                        from app.services.history_window_service import (
                            resolve_history_token_budget,
                            select_history_window,
                        )
                        recent = select_history_window(
                            hist_msgs, context_limit,
                            resolve_history_token_budget(config),
                            mode=getattr(config, "agent_context_window_mode", "legacy"),
                            min_messages=getattr(
                                config, "agent_conversation_min_messages", 0,
                            ),
                        )
                        for m in recent:
                            if m.role in ("user", "assistant") and m.content:
                                # build_history_message: conv-a040c24e citation
                                # neutralization for assistant rows only.
                                from app.services.tool_history import build_history_message
                                messages.append(
                                    build_history_message(
                                        m.role,
                                        m.content,
                                        reasoning_content=getattr(m, "reasoning_content", None),
                                    )
                                )
                except Exception:
                    logger.warning("Failed to load conversation history for background task %s", task_id)

            messages.append({"role": "user", "content": task.goal or ""})

            # 有 checkpoint → 全量回放快照 messages + 消费计数
            # 续算（State-Aware 恢复：重建下一次上下文视图，不做有损摘要）。
            resume_preused = 0
            if getattr(task, "checkpoint", None):
                try:
                    from app.services.task_checkpoint import parse_checkpoint, plan_resume
                    _cp = parse_checkpoint(task.checkpoint)
                    messages, resume_preused = plan_resume(_cp)
                    # 重派在飞机 tool_calls（台账去重）后再续跑；
                    # cursor=checkpoint.budget_used（即该迭代 consume 后的计数）。
                    messages = await self._preflight_pending_tool_calls(
                        messages, task_id=task_id, cursor=int(resume_preused),
                        user=user, conversation=conversation, assistant=assistant,
                        workspace_path=str(workspace.root_path),
                        blocked_tools=(
                            {"background_task", "delegate_agent_task"}
                            | _TASK_CONTROL_BLOCKED_TOOLS),
                    )
                    logger.info("Background task %s resuming from checkpoint (budget_used=%d)",
                                task_id, resume_preused)
                except ValueError:
                    logger.exception("checkpoint unusable (loud error honored) — starting fresh")

            # checkpoint 回调（1s 节流；fail-open）
            _cp_last = 0.0

            async def _checkpoint_cb(cp, _task_id=task_id):
                nonlocal _cp_last
                import json as _json
                import time as _t
                _now = _t.monotonic()
                if _now - _cp_last < 1.0:
                    return
                _cp_last = _now
                try:
                    async with AsyncSessionLocal() as cp_db:
                        row = await cp_db.get(AgentTask, _task_id)
                        if row is not None:
                            row.checkpoint = _json.dumps(cp, ensure_ascii=False)
                            await cp_db.commit()
                except Exception:
                    logger.exception("checkpoint persist failed (fail-open)")

            max_iterations = task.iterations_max or config.agent_tool_loop_max_iterations

            from app.model_gateway.factory import wire_provider_type as _wire_pt
            agent_loop = AgentLoop(
                llm=llm,
                max_iterations=max_iterations,
                workspace_path=str(workspace.root_path),
                provider_type=_wire_pt(llm, getattr(assistant, "provider_type", "deepseek") or "deepseek"),
                enable_reasoning=False,
                enable_compression=config.agent_compression_enabled,
                permission_callback=_make_background_permission_callback(),
                blocked_tools={"delegate_agent_task"} | _TASK_CONTROL_BLOCKED_TOOLS,
                user_id=str(task.user_id) if task.user_id else None,
            )
            _enforce_hard_blocked_tools(
                agent_loop, {"delegate_agent_task"} | _TASK_CONTROL_BLOCKED_TOOLS)
            # W17-P1（审计 I-1）：后台任务同口径轮次门控——非代码任务不启
            # vibeweaver 执法（gate/audit stand-down），消除「执法无指引」面。
            # 复审 MINOR-1：整块 fail-open（异常保持默认 True=旧行为，不拖挂任务）。
            try:
                from app.services.vibeweaver_turn_gate import (
                    explicit_trigger_hit as _vw_explicit,
                    extract_recent_user_text as _vw_extract,
                    resolve_effective_active as _vw_resolve_eff,
                )
                _vw_last, _vw_ctx = _vw_extract(messages)
                try:
                    _vw_gating = bool(config.vibeweaver_task_gating_enabled)
                except Exception:
                    _vw_gating = True
                try:
                    _vw_off = str(getattr(config, "vibeweaver_mode", "auto") or "auto") == "off"
                except Exception:
                    _vw_off = False
                agent_loop.vibeweaver_active = _vw_resolve_eff(_vw_last, None, _vw_gating, context_text=_vw_ctx)
                if _vw_off:
                    agent_loop.vibeweaver_active = False  # D9：mode=off 全关（含后台/定时面）
                logger.info(
                    "vibeweaver turn gate active=%s explicit=%s coding=None gating=%s task=%s",
                    agent_loop.vibeweaver_active, _vw_explicit(_vw_last), _vw_gating, task_id,
                )
            except Exception:
                logger.debug(
                    "vibeweaver turn gate wiring failed (fail-open to active=True)",
                    exc_info=True,
                )
            # 记忆优先级最高（与 chat.py 同）：后台任务注入的记忆进入审计
            # 证据域 [M]，不得被判「凭空编造」（2026-09-18 conv 3583d840）。
            agent_loop.injected_memory_context = _mem_block or ""

            accumulated_response = ""
            # 输出单元契约（2026-10-14）：停车单元 + 截断续写链状态（镜像 chat.py）。
            parked_units: list = []
            _stitch_active = False
            _stitch_base = ""
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

            # F1/H2：interjection_queue + 5s 轮询 pending_messages（投递后清空列）
            interjection_queue: asyncio.Queue = asyncio.Queue()
            _pending_poller = asyncio.create_task(
                self._poll_pending_messages(task_id, interjection_queue))

            async for event in agent_loop.run(
                messages,
                user=user,
                conversation=conversation,
                assistant=assistant,
                interjection_queue=interjection_queue,
                checkpoint_cb=_checkpoint_cb,
                budget_preused=resume_preused,
                principal=("agent_task", task_id),
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
                # 输出单元契约（2026-10-14）：keep_stitch 型 reset（可恢复重试）
                # 只回退失败残稿到链锚——与 chat.py 同口径。
                if "audit_reset" in event:
                    if event.get("keep_stitch") and _stitch_active:
                        accumulated_response = _stitch_base
                    else:
                        accumulated_response = ""
                        _stitch_active = False
                        _stitch_base = ""

                # 输出单元契约（2026-10-14）：工具边界预工具散文停车——不拼进
                # 终局结果（后台任务此前与 chat.py 同病：过程旁白并入终稿）。
                # parked 单元按边界序回插持久化 display_sequence（工具卡之前）。
                if "tool_call" in event or "iteration_done" in event:
                    _id_info = (event.get("iteration_done") or {}) if "iteration_done" in event else {}
                    if _id_info.get("stitch"):
                        _stitch_active = True
                        _stitch_base = accumulated_response
                    if "tool_call" in event or _id_info.get("tool_calls"):
                        if not _stitch_active and accumulated_response.strip():
                            parked_units.append(accumulated_response)
                        elif _stitch_active:
                            _stitch_base = accumulated_response
                        accumulated_response = "" if not _stitch_active else accumulated_response

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

            if _pending_poller:
                _pending_poller.cancel()
                with suppress(asyncio.CancelledError):
                    await _pending_poller

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

            # 输出单元契约（2026-10-14 I2 / A4.9 C2 修订）：以 agent_steps 为基底
            # 构建完整 display_sequence（镜像 chat.py 装配口径：工具卡+停车文本+
            # 终答项），再按边界序把停车单元回插到第 i 张工具卡**之前**（位序契约
            # A4.9 C1）。此前直接往不存在的 display_sequence 键写 text-only 列表，
            # 会把工具卡/终答挤出渲染面（scoped 复审 Critical-2）。
            _final_text = (accumulated_response or "").strip()
            if not _final_text and parked_units:
                # M4 镜像：仅预工具散文、无终稿——提升末停车单元为答案。
                _final_text = parked_units.pop().strip()
            if parked_units or _final_text:
                try:
                    _tr_obj = json.loads(transformed_tool_results) if transformed_tool_results else {}
                    _a_steps = _tr_obj.get("agent_steps") or []
                    _seq = []
                    _pu = 0
                    for _st in _a_steps:
                        if _pu < len(parked_units):
                            _seq.append({"type": "text", "content": parked_units[_pu], "superseded": True})
                            _pu += 1
                        _item = dict(_st)
                        _item["type"] = _st.get("step_type", "tool")
                        _seq.append(_item)
                    while _pu < len(parked_units):
                        _seq.append({"type": "text", "content": parked_units[_pu], "superseded": True})
                        _pu += 1
                    if _final_text:
                        _seq.append({"type": "text", "content": _final_text})
                    _tr_obj["display_sequence"] = _seq
                    _tr_obj["content_segments"] = [_final_text] if _final_text else []
                    transformed_tool_results = json.dumps(_tr_obj, ensure_ascii=False)
                except Exception:
                    logger.debug("parked-unit interleave failed (fail-open)", exc_info=True)

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
                        provider_type=_wire_pt(llm, getattr(assistant, "provider_type", "deepseek") or "deepseek"),
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
            if _pending_poller:
                _pending_poller.cancel()
                with suppress(asyncio.CancelledError):
                    await _pending_poller
            # 优雅停机打断 → resumable（保留快照）而非
            # cancelled；用户显式取消（_cancelled 正常返回路径）仍 cancelled。
            if self._draining:
                await self._mark_drained(task_id)
            else:
                await self._mark_cancelled(task_id)
            raise
        except Exception as exc:
            if _pending_poller:
                _pending_poller.cancel()
                with suppress(asyncio.CancelledError):
                    await _pending_poller
            logger.exception("Background task %s failed", task_id)
            await self._mark_failed(task_id, str(exc))

    @staticmethod
    def _validate_separate_checkpoint_binding(cp: dict, task: AgentTask) -> None:
        """Refuse a checkpoint from another task/C2/manifest (§7.5).

        Wave 1 checkpoints predate the binding; they are accepted with a
        warning because the task row still carries the frozen manifest and C2.
        Any checkpoint that DOES carry a binding must match exactly.
        """
        import hashlib

        binding = cp.get("execution_binding")
        if not isinstance(binding, dict):
            logger.warning(
                "separate task %s: legacy checkpoint without execution binding",
                task.id,
            )
            return
        expected_hash = hashlib.sha256(
            (task.execution_config or "").encode("utf-8")).hexdigest()
        checks = {
            "task_id": str(task.id),
            "execution_conversation_id": str(task.execution_conversation_id or ""),
            "execution_config_hash": expected_hash,
        }
        for key, expected in checks.items():
            actual = str(binding.get(key) or "")
            if actual != expected:
                raise ValueError(
                    f"checkpoint binding mismatch: {key} {actual!r} != {expected!r}")
        # attempt_id is write-time provenance: a resume intentionally gets a
        # new attempt, so equality with the current attempt is not required,
        # but a Wave-2 checkpoint must carry the writer's attempt id.
        if not str(binding.get("attempt_id") or ""):
            raise ValueError("checkpoint binding missing attempt_id")
        bound_version = int(binding.get("status_version") or 0)
        if bound_version > int(task.status_version or 0):
            raise ValueError(
                "checkpoint status_version is newer than the task row")

    async def _load_task_permit_fingerprints(self, task_id: str, user_id: str) -> set[tuple[str, str]]:
        from app.db.database import AgentTaskPermit
        from sqlalchemy import select as _select

        try:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    _select(AgentTaskPermit.tool_name, AgentTaskPermit.action_fingerprint)
                    .where(
                        AgentTaskPermit.task_id == task_id,
                        AgentTaskPermit.user_id == user_id,
                        AgentTaskPermit.consumed_at.is_(None),
                        AgentTaskPermit.expires_at > datetime.utcnow(),
                    )
                )).all()
                return {(str(r[0]), str(r[1])) for r in rows}
        except Exception:
            logger.warning("task permit preload failed; recovery fails closed", exc_info=True)
            return set()

    async def _run_separate_task(self, task_id: str, attempt_id: str | None = None) -> None:
        """Run a `separate_v1` task in its own C2, with fenced writes.

        Contract (§5–§10): never reads/writes C1, never appends the goal twice,
        never creates C3, and finalizes through the shared finalize service.
        """
        from app.services.agent_task_finalize import finalize_separate_task
        from app.services.agent_run_spec import endpoint_fingerprint

        pending_poller: asyncio.Task | None = None
        heartbeat_task: asyncio.Task | None = None
        in_flight: dict[str, str] = {}

        async with AsyncSessionLocal() as db:
            task = await db.get(AgentTask, task_id)
            if task is None or getattr(task, "execution_mode", None) != "separate_v1":
                return
            if attempt_id and str(task.attempt_id or "") != str(attempt_id):
                logger.warning("separate task %s: stale attempt %s (current=%s)",
                               task_id, attempt_id, task.attempt_id)
                return
            if task.status in ("completed", "failed", "cancelled", "timed_out"):
                return
            if task.cancel_requested_at is not None or task.status in ("cancelling", "cancelled"):
                if task.status == "cancelling":
                    await self._finalize_separate_new_session(
                        task_id=task_id, status="cancelled",
                        attempt_id=attempt_id, require_attempt=bool(attempt_id),
                        require_statuses=("cancelling",),
                        error="取消请求已在运行前确认",
                    )
                elif task.status in ("pending", "claimed", "resumable"):
                    await self._finalize_separate_new_session(
                        task_id=task_id, status="cancelled",
                        attempt_id=attempt_id, require_attempt=bool(attempt_id),
                        require_statuses=("pending", "claimed", "resumable"),
                        error="取消请求已在启动前到达",
                    )
                return
            now = datetime.utcnow()
            # CAS the claimed→running transition: a cancel that landed between
            # claim and start must NOT be overwritten by a stale read.
            res = await db.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == task_id,
                    AgentTask.attempt_id == attempt_id,
                    AgentTask.status.in_(("pending", "claimed", "resumable")),
                    AgentTask.cancel_requested_at.is_(None),
                )
                .values(
                    status="running",
                    started_at=func.coalesce(AgentTask.started_at, now),
                    last_attempt_started_at=now,
                    heartbeat_at=now,
                    lease_until=now + timedelta(seconds=config.voice_delegation_lease_seconds),
                    status_version=AgentTask.status_version + 1,
                    updated_at=now,
                )
            )
            if res.rowcount != 1:
                await db.rollback()
                logger.info("separate task %s start fenced (cancel/claim race)", task_id)
                return
            await db.commit()
            await db.refresh(task)

            try:
                previous_usage = json.loads(task.usage_meta or "{}")
                if not isinstance(previous_usage, dict):
                    previous_usage = {}
            except (TypeError, json.JSONDecodeError):
                previous_usage = {}
            previous_elapsed = float(previous_usage.get("elapsed_seconds") or 0.0)
            budget_extension = float(previous_usage.get("budget_extension_seconds") or 0.0)

            user = await db.get(User, task.user_id)
            conversation = (
                await db.get(Conversation, task.execution_conversation_id)
                if task.execution_conversation_id else None
            )
            assistant = await db.get(Assistant, task.assistant_id) if task.assistant_id else None
            manifest = {}
            try:
                manifest = json.loads(task.execution_config or "{}")
            except (TypeError, json.JSONDecodeError):
                manifest = {}
            goal = task.goal or ""

        if user is None or conversation is None or assistant is None:
            await self._finalize_separate_failure(
                task_id, attempt_id,
                "执行上下文缺失（目标助手、执行会话或用户不可用）；不会回退到来源会话，也不会创建新会话",
            )
            return

        wall_time_configured = int(
            (manifest.get("limits") or {}).get("wall_time_seconds")
            or config.agent_background_tasks_total_timeout or 0
        )
        remaining_budget = compute_remaining_budget(
            wall_time_configured, budget_extension, previous_elapsed)
        if wall_time_configured > 0 and remaining_budget <= 0:
            # §14.1: remaining <= 0 is EXHAUSTED, never "unlimited". Park or
            # time out before making any further model/tool call.
            exhausted_usage = build_cumulative_usage(
                previous_usage,
                last_elapsed=0.0,
                iterations=int(previous_usage.get("iterations") or 0),
                tool_calls=0,
                budget_extension=budget_extension,
                reasoning_effort=(manifest.get("reasoning") or {}).get("effort"),
            )
            # No new attempt ran in this pre-check; keep the counters truthful.
            exhausted_usage["attempts"] = int(previous_usage.get("attempts") or 0)
            exhausted_usage["stages"]["main_model_attempts"] = int(
                previous_usage.get("attempts") or 0)
            if config.voice_delegation_budget_pause_enabled:
                exhausted_usage["budget_extension_seconds"] = (
                    budget_extension
                    + config.voice_delegation_budget_resume_seconds
                )
                await self._finalize_separate_waiting(
                    task_id=task_id, kind="budget",
                    question=(
                        "已达到本任务的累计执行预算。回复“继续”可再获得 "
                        f"{config.voice_delegation_budget_resume_seconds} 秒执行预算。"
                    ),
                    attempt_id=attempt_id, require_attempt=True,
                    result=task.result, usage_meta=exhausted_usage,
                    runtime_meta={"budget_exhausted": True, "elapsed_seconds": previous_elapsed},
                )
            else:
                await self._finalize_separate_new_session(
                    task_id=task_id, status="timed_out",
                    attempt_id=attempt_id, require_attempt=True,
                    result=task.result, usage_meta=exhausted_usage,
                    error="已达到累计执行预算",
                )
            await self._notify_voice_terminal(task_id)
            return

        worktree_meta = None
        try:
            async with AsyncSessionLocal() as ws_db:
                workspace = await ensure_user_workspace(
                    ws_db, task.user_id, getattr(user, "username", None))
            # §9.4: task-scoped scratch/artifacts; the shared user workspace is
            # never the execution root for a separate delegation.
            task_root = Path(workspace.root_path) / "agent_tasks" / task_id
            scratch_path = task_root / "scratch"
            artifacts_path = task_root / "artifacts"
            scratch_path.mkdir(parents=True, exist_ok=True)
            artifacts_path.mkdir(parents=True, exist_ok=True)
            workspace_path = str(scratch_path)
            if str(getattr(task, "workspace_mode", "") or "") == "worktree":
                from app.services.git_worktree_service import (
                    WorktreeError,
                )

                try:
                    meta = json.loads(task.workspace_meta or "{}")
                    project = Path(str(meta.get("project_path") or ""))
                    from app.services.git_worktree_service import (
                        ensure_task_worktree_async,
                    )

                    worktree_meta = await ensure_task_worktree_async(
                        project, task_root=task_root, task_id=task_id,
                        stored_meta=meta)
                    workspace_path = worktree_meta.worktree_path
                    # Persist branch/worktree/base at CREATION time, not only at
                    # finalize: a deploy/crash before completion must be able to
                    # resume the SAME worktree (review W2-I5).
                    async with AsyncSessionLocal() as ws_meta_db:
                        await ws_meta_db.execute(
                            update(AgentTask)
                            .where(AgentTask.id == task_id)
                            .values(workspace_meta=json.dumps(
                                worktree_meta.as_dict(), ensure_ascii=False))
                        )
                        await ws_meta_db.commit()
                except WorktreeError as exc:
                    await self._finalize_separate_failure(
                        task_id, attempt_id,
                        f"worktree 创建失败：{exc}；任务未开始执行",
                    )
                    return
        except Exception:
            logger.exception("separate task %s: workspace setup failed", task_id)
            await self._finalize_separate_failure(
                task_id, attempt_id,
                "任务工作区初始化失败（磁盘/权限/数据库）；任务未开始执行",
            )
            return

        # User-level model overrides + MCP servers must match submission.
        try:
            from app.services.user_model_provider_service import activate_user_overrides
            from app.services.user_mcp_service import ensure_user_mcp_tools

            async with AsyncSessionLocal() as ctx_db:
                await activate_user_overrides(ctx_db, task.user_id)
                await ensure_user_mcp_tools(ctx_db, task.user_id)
        except Exception as exc:
            logger.exception("separate task %s: user override/MCP activation failed", task_id)
            await self._finalize_separate_failure(
                task_id, attempt_id,
                "用户模型/工具配置加载失败；为避免把任务发往其他供应商或丢失工具权限，任务停止",
            )
            return

        try:
            from app.services.agent_service import AgentService as _AgentService
            from app.model_gateway import factory as _factory
            from app.model_gateway.profiles import get_thinking_profile

            llm = _AgentService().create_llm_service(assistant)
            endpoint = getattr(llm, "endpoint", None)
            expected_fp = str((manifest.get("endpoint") or {}).get("nonsecret_fingerprint") or "")
            actual_fp = endpoint_fingerprint(endpoint) if endpoint is not None else ""
            expected_ep_alias = str((manifest.get("endpoint") or {}).get("alias") or "")
            actual_ep_alias = str(getattr(endpoint, "alias", "") or "") if endpoint else ""
            expected_alias = (manifest.get("assistant") or {}).get("model_alias") or None
            current_alias = getattr(assistant, "model_alias", None) or None
            expected_sub = (manifest.get("assistant") or {}).get("subtask_model_alias") or None
            current_sub = getattr(assistant, "subtask_model_alias", None) or None
            if expected_alias != current_alias or expected_sub != current_sub:
                await self._finalize_separate_failure(
                    task_id, attempt_id,
                    "目标助手的模型配置在任务提交后发生了变化；严格模式不会静默回落或换模型",
                )
                return
            if expected_ep_alias and actual_ep_alias != expected_ep_alias:
                await self._finalize_separate_failure(
                    task_id, attempt_id,
                    "有效模型端点别名与提交快照不一致（别名失效/回落 main）；严格模式停止",
                )
                return
            if not expected_fp or actual_fp != expected_fp:
                await self._finalize_separate_failure(
                    task_id, attempt_id,
                    "有效模型端点与提交时的快照不一致（可能被用户覆盖或配置变更）；严格模式停止，不回退 main",
                )
                return
            reasoning = manifest.get("reasoning") or {}
            policy = str(reasoning.get("policy") or "strict_highest")
            reasoning_enabled = bool(reasoning.get("enabled"))
            reasoning_effort = reasoning.get("effort")
            thinking_budget = reasoning.get("thinking_budget")

            provider_type = _factory.wire_provider_type(
                llm, getattr(assistant, "provider_type", "deepseek") or "deepseek")
            iteration_llm, iteration_provider = _AgentService().create_iteration_llm_service(
                assistant, llm)
            thinking_profile = get_thinking_profile(provider_type)

            system_prompt = str((manifest.get("assistant") or {}).get("system_prompt") or "")
            if not system_prompt:
                system_prompt = config.agent.get(
                    "background_task_system_prompt",
                    "你是一个专业的研究助手。请根据用户的任务，使用搜索和浏览器工具收集信息，最后给出完整的总结。",
                )
            system_prompt = f"{system_prompt}\n\n{_BACKGROUND_TASK_HINT}"

            _mem_block = ""
            try:
                from app.services import memory_task_context as _mtc
                if _mtc.background_task_memory_enabled():
                    async with AsyncSessionLocal() as _mem_db:
                        _mem_block = await _mtc.build_task_memory_context(
                            _mem_db, task.user_id, goal,
                            budget_chars=int(config.agent.get(
                                "background_task_memory_budget_chars", 600)))
                    if _mem_block:
                        system_prompt = f"{system_prompt}\n\n{_mem_block}"
            except Exception:
                logger.debug("separate task memory injection failed (fail-open)", exc_info=True)

            messages = _task_messages(system_prompt)
            resume_preused = 0
            if getattr(task, "checkpoint", None):
                try:
                    from app.services.task_checkpoint import parse_checkpoint, plan_resume

                    _cp = parse_checkpoint(task.checkpoint)
                    self._validate_separate_checkpoint_binding(_cp, task)
                    _cp_messages, resume_preused = plan_resume(_cp)
                    # Frozen contract must survive checkpoint trimming: the
                    # system prompt and goal are rebuilt from the manifest/task,
                    # never taken from the (trimmed) message prefix.
                    _cp_body = [
                        m for m in _cp_messages if m.get("role") != "system"
                    ]
                    messages = _task_messages(system_prompt) + _cp_body
                    if not any(
                        m.get("role") == "user" and str(m.get("content") or "") == goal
                        for m in messages
                    ):
                        messages.insert(1, {"role": "user", "content": goal})
                    messages = await self._preflight_pending_tool_calls(
                        messages, task_id=task_id, cursor=int(resume_preused),
                        user=user, conversation=conversation, assistant=assistant,
                        workspace_path=workspace_path,
                        permit_fingerprints=await self._load_task_permit_fingerprints(
                            task_id, str(task.user_id)),
                        blocked_tools=(
                            {"background_task", "delegate_agent_task"}
                            | _TASK_CONTROL_BLOCKED_TOOLS),
                    )
                except ValueError:
                    logger.exception("separate checkpoint unusable — refusing to restart side effects")
                    await self._finalize_separate_failure(
                        task_id, attempt_id,
                        "断点快照无效或版本过新；为避免重复副作用，任务停止并保留现场",
                    )
                    return
            else:
                # The goal already exists as C2's single initial user message;
                # it is injected once here and never appended again.
                messages.append({"role": "user", "content": goal})

            async def _heartbeat() -> None:
                interval = config.voice_delegation_heartbeat_seconds
                while True:
                    await asyncio.sleep(interval)
                    try:
                        async with AsyncSessionLocal() as hb_db:
                            res = await hb_db.execute(
                                update(AgentTask)
                                .where(
                                    AgentTask.id == task_id,
                                    AgentTask.attempt_id == attempt_id,
                                    AgentTask.status.in_(("running", "claimed")),
                                )
                                .values(
                                    heartbeat_at=datetime.utcnow(),
                                    lease_until=datetime.utcnow() + timedelta(
                                        seconds=config.voice_delegation_lease_seconds),
                                    updated_at=datetime.utcnow(),
                                )
                            )
                            await hb_db.commit()
                            if res.rowcount == 0:
                                return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug("separate heartbeat failed", exc_info=True)

            async def _poll_steers() -> None:
                while True:
                    try:
                        async with AsyncSessionLocal() as sdb:
                            row = (await sdb.execute(
                                text(
                                    "SELECT pending_messages, status FROM agent_tasks "
                                    "WHERE id = :id AND attempt_id = :aid FOR UPDATE"
                                ),
                                {"id": task_id, "aid": attempt_id},
                            )).fetchone()
                            if row is None:
                                return
                            if row[1] in ("cancelling", "cancelled", "completed",
                                          "failed", "timed_out"):
                                await sdb.commit()
                                return
                            raw = row[0]
                            items = []
                            if raw:
                                try:
                                    items = json.loads(raw) or []
                                except (TypeError, json.JSONDecodeError):
                                    items = []
                            changed = False
                            for item in list(items):
                                if not isinstance(item, dict):
                                    continue
                                mid = str(item.get("id") or "")
                                content = str(item.get("content") or "").strip()
                                if item.get("source") == "task_permit" and mid.startswith("permit:"):
                                    pid = mid.split(":", 1)[1]
                                    consumed = (await sdb.execute(
                                        text("SELECT consumed_at FROM agent_task_permits WHERE id = :pid"),
                                        {"pid": pid},
                                    )).fetchone()
                                    if consumed and consumed[0] is not None:
                                        # The permit was already consumed by the
                                        # recovery path — do not inject a second
                                        # "please retry" that would ask again.
                                        items.remove(item)
                                        changed = True
                                        continue
                                if mid and content and mid not in in_flight:
                                    in_flight[mid] = content
                                    interjection_queue.put_nowait(content)
                            if changed:
                                await sdb.execute(
                                    text(
                                        "UPDATE agent_tasks SET pending_messages = :pm "
                                        "WHERE id = :id AND attempt_id = :aid"
                                    ),
                                    {
                                        "pm": json.dumps(items, ensure_ascii=False) if items else None,
                                        "id": task_id,
                                        "aid": attempt_id,
                                    },
                                )
                            await sdb.commit()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug("separate steer poll failed", exc_info=True)
                    await asyncio.sleep(2)

            async def _checkpoint_cb(cp) -> None:
                applied: list[str] = []
                import hashlib as _hashlib

                # §7.5: bind the checkpoint to the execution contract so a
                # restart cannot resume another attempt's snapshot.
                cp["execution_binding"] = {
                    "task_id": str(task_id),
                    "attempt_id": str(attempt_id or ""),
                    "execution_conversation_id": str(
                        task.execution_conversation_id or ""),
                    "execution_config_hash": _hashlib.sha256(
                        (task.execution_config or "").encode("utf-8")).hexdigest(),
                    "status_version": int(task.status_version or 0),
                }
                iteration_state["used"] = max(
                    int(iteration_state["used"]),
                    int(cp.get("budget_used") or 0),
                )
                checkpoint_messages = cp.get("messages") or []
                # Match only interjection messages and consume at most one
                # inbox item per occurrence. Content matching alone would ack
                # two identical "继续" steers from a single checkpoint entry.
                remaining_counts: dict[str, int] = {}
                for m in checkpoint_messages:
                    if m.get("role") == "user" and m.get("_interjection"):
                        content = str(m.get("content") or "")
                        remaining_counts[content] = remaining_counts.get(content, 0) + 1
                for mid, content in list(in_flight.items()):
                    if remaining_counts.get(content, 0) > 0:
                        applied.append(mid)
                        remaining_counts[content] -= 1
                async with AsyncSessionLocal() as cp_db:
                    row = (await cp_db.execute(
                        text(
                            "SELECT attempt_id, status, pending_messages FROM agent_tasks "
                            "WHERE id = :id FOR UPDATE"
                        ),
                        {"id": task_id},
                    )).fetchone()
                    if row is None or str(row[0] or "") != str(attempt_id or ""):
                        raise RuntimeError("stale attempt: checkpoint write fenced")
                    if row[1] not in ("running", "claimed"):
                        raise RuntimeError("task left running state")
                    items = []
                    if row[2]:
                        try:
                            items = json.loads(row[2]) or []
                        except (TypeError, json.JSONDecodeError):
                            items = []
                    remaining = [
                        item for item in items
                        if not (isinstance(item, dict) and str(item.get("id") or "") in applied)
                    ]
                    live_usage = build_cumulative_usage(
                        previous_usage,
                        last_elapsed=(
                            asyncio.get_event_loop().time() - started_at_ts),
                        iterations=int(iteration_state["used"] or 0),
                        tool_calls=len(tool_results_accumulated),
                        budget_extension=budget_extension,
                        reasoning_effort=reasoning_effort,
                    )
                    await cp_db.execute(
                        text(
                            "UPDATE agent_tasks SET checkpoint = :cp, pending_messages = :pm, "
                            "iterations_done = GREATEST(COALESCE(iterations_done, 0), :iters), "
                            "usage_meta = :usage, "
                            "heartbeat_at = :hb, lease_until = :lease, updated_at = :hb "
                            "WHERE id = :id AND attempt_id = :aid"
                        ),
                        {
                            "cp": json.dumps(cp, ensure_ascii=False),
                            "pm": json.dumps(remaining, ensure_ascii=False) if remaining else None,
                            "iters": int(cp.get("budget_used") or 0),
                            "usage": json.dumps(live_usage, ensure_ascii=False),
                            "hb": datetime.utcnow(),
                            "lease": datetime.utcnow() + timedelta(
                                seconds=config.voice_delegation_lease_seconds),
                            "id": task_id,
                            "aid": attempt_id,
                        },
                    )
                    await cp_db.commit()
                for mid in applied:
                    in_flight.pop(mid, None)

            interjection_queue: asyncio.Queue = asyncio.Queue()
            iteration_state = {"used": int(resume_preused or 0)}

            async def _task_act_gate_callback(*, tool_name: str, details: dict | None = None, **_kw) -> bool:
                """One-time permit check for a waiting_auth resume (§13.1)."""
                fingerprint = str((details or {}).get("fingerprint") or "")
                if not fingerprint:
                    return False
                from app.services.agent_task_control_service import consume_task_permit

                try:
                    async with AsyncSessionLocal() as permit_db:
                        return await consume_task_permit(
                            permit_db, task_id=task_id, user_id=str(task.user_id),
                            tool_name=tool_name, action_fingerprint=fingerprint,
                        )
                except Exception:
                    logger.warning(
                        "task permit consume failed; denying action", exc_info=True)
                    return False

            heartbeat_task = asyncio.create_task(_heartbeat(), name=f"agent-task-hb-{task_id[:8]}")
            pending_poller = asyncio.create_task(
                _poll_steers(), name=f"agent-task-steer-{task_id[:8]}")

            agent_loop = AgentLoop(
                llm=llm,
                max_iterations=min(
                    int(task.iterations_max or config.agent_tool_loop_max_iterations),
                    config.voice_delegation_iterations_cap,
                ),
                workspace_path=workspace_path,
                provider_type=provider_type,
                enable_reasoning=reasoning_enabled,
                reasoning_effort=reasoning_effort,
                thinking_budget=thinking_budget,
                preserve_thinking=thinking_profile.preserve_thinking,
                iteration_llm=iteration_llm,
                iteration_provider_type=iteration_provider,
                enable_compression=config.agent_compression_enabled,
                permission_callback=_make_background_permission_callback(),
                act_gate_approval_callback=_task_act_gate_callback,
                blocked_tools={"background_task", "delegate_agent_task"} | _TASK_CONTROL_BLOCKED_TOOLS,
                strict_reasoning=(policy == "strict_highest"),
                user_id=str(task.user_id) if task.user_id else None,
            )
            _enforce_hard_blocked_tools(
                agent_loop,
                {"background_task", "delegate_agent_task"} | _TASK_CONTROL_BLOCKED_TOOLS)
            agent_loop.injected_memory_context = _mem_block or ""
            agent_loop.task_principal = f"agent_task:{task_id}"

            accumulated_response = ""
            accumulated_reasoning = ""
            tool_results_accumulated: list[dict] = []
            started_at_ts = asyncio.get_event_loop().time()
            last_activity_ts = _time.monotonic()
            deadline_seconds = 0
            if wall_time_configured > 0:
                # §14.1: cumulative wall time across attempts — a restart must
                # not reset the budget. A non-positive remainder was already
                # parked/timed out above; here it is necessarily > 0.
                deadline_seconds = max(1, int(remaining_budget))
            deadline = (started_at_ts + deadline_seconds) if deadline_seconds > 0 else None
            inactivity_timeout = float(
                config.agent_background_tasks.get("inactivity_timeout_seconds", 600))
            _last_cancel_check = 0.0
            _timed_out = False
            _budget_exhausted = False
            _cancel_requested = False
            _loop_error: str | None = None
            _pause_kind: str | None = None
            _pause_question: str | None = None
            _pause_tool: str | None = None
            _pause_fingerprint: str | None = None

            async for event in agent_loop.run(
                messages,
                user=user,
                conversation=conversation,
                assistant=assistant,
                interjection_queue=interjection_queue,
                checkpoint_cb=_checkpoint_cb,
                budget_preused=resume_preused,
                principal=("agent_task", task_id),
            ):
                if deadline and asyncio.get_event_loop().time() > deadline:
                    _timed_out = True
                    _budget_exhausted = True
                    break
                now_mono = _time.monotonic()
                if now_mono - _last_cancel_check > 5.0:
                    _last_cancel_check = now_mono
                    async with AsyncSessionLocal() as ck_db:
                        status_row = (await ck_db.execute(
                            text("SELECT status, cancel_requested_at FROM agent_tasks WHERE id = :id"),
                            {"id": task_id},
                        )).fetchone()
                    current_status = status_row[0] if status_row else ""
                    cancel_requested = bool(status_row[1]) if status_row else False
                    if current_status == "cancelling" or cancel_requested:
                        _cancel_requested = True
                        break
                    if current_status == "cancelled":
                        logger.info("separate task %s already cancelled by control plane", task_id)
                        return
                if now_mono - last_activity_ts > inactivity_timeout:
                    _timed_out = True
                    break
                if event.get("content"):
                    accumulated_response += event["content"]
                    last_activity_ts = now_mono
                if event.get("reasoning_content"):
                    accumulated_reasoning += event["reasoning_content"]
                    last_activity_ts = now_mono
                if "audit_reset" in event:
                    accumulated_response = ""
                if event.get("tool_call"):
                    tc = event["tool_call"]
                    tool_results_accumulated.append({
                        "call_id": tc.get("call_id", ""),
                        "name": tc.get("name", ""),
                        "arguments": tc.get("arguments", {}),
                    })
                    last_activity_ts = now_mono
                if event.get("tool_result"):
                    tr = event["tool_result"]
                    tool_name = ""
                    for entry in tool_results_accumulated:
                        if entry.get("call_id") == tr.get("call_id"):
                            entry["result"] = tr.get("result", "")
                            entry["error"] = tr.get("error", False)
                            tool_name = str(entry.get("name") or "")
                            break
                    last_activity_ts = now_mono
                    payload = None
                    try:
                        payload = json.loads(tr.get("result") or "")
                    except (TypeError, json.JSONDecodeError):
                        payload = None
                    if isinstance(payload, dict):
                        pause = detect_task_pause(payload, tool_name)
                        if pause is not None:
                            _pause_kind = pause["kind"]
                            _pause_question = pause["question"]
                            _pause_tool = pause["tool_name"]
                            _pause_fingerprint = pause["action_fingerprint"]
                            break
                if event.get("error"):
                    _loop_error = str(event.get("error") or "agent loop error")
                    break
                if event.get("done"):
                    break

            if pending_poller:
                pending_poller.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_poller
            if heartbeat_task:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

            total_elapsed = asyncio.get_event_loop().time() - started_at_ts
            cumulative_usage = build_cumulative_usage(
                previous_usage,
                last_elapsed=total_elapsed,
                iterations=int(iteration_state["used"] or 0),
                tool_calls=len(tool_results_accumulated),
                budget_extension=budget_extension,
                reasoning_effort=reasoning_effort,
            )

            workspace_result = None
            workspace_meta_payload = None
            if worktree_meta is not None:
                try:
                    from app.services.git_worktree_service import (
                        commit_task_worktree_async,
                    )

                    workspace_result = await commit_task_worktree_async(
                        worktree_meta.worktree_path,
                        task_id=task_id,
                        base_commit=worktree_meta.base_commit,
                    )
                except Exception as exc:
                    logger.exception("separate task %s: worktree commit failed", task_id)
                    workspace_result = {"error": str(exc)[:500], "changed": False}
                workspace_meta_payload = {
                    **worktree_meta.as_dict(),
                    **(workspace_result or {}),
                }

            if _cancel_requested:
                await self._finalize_separate_new_session(
                    task_id=task_id, status="cancelled",
                    attempt_id=attempt_id, require_attempt=True,
                    error="用户请求取消（在途工具可能已产生副作用）",
                    usage_meta=cumulative_usage,
                    runtime_meta={"policy": policy, "effort": reasoning_effort,
                                  "elapsed_seconds": total_elapsed},
                )
                await self._notify_voice_terminal(task_id)
                return

            if _pause_kind:
                await self._finalize_separate_waiting(
                    task_id=task_id,
                    kind=_pause_kind,
                    question=_pause_question,
                    attempt_id=attempt_id,
                    require_attempt=True,
                    tool_name=_pause_tool,
                    action_fingerprint=_pause_fingerprint,
                    usage_meta=cumulative_usage,
                    workspace_meta=workspace_meta_payload,
                    runtime_meta={
                        "policy": policy,
                        "requested_effort": reasoning_effort,
                        "wire_provider_type": provider_type,
                        "elapsed_seconds": cumulative_usage["elapsed_seconds"],
                        "tool_calls": len(tool_results_accumulated),
                    },
                )
                await self._notify_voice_terminal(task_id)
                return

            if _timed_out and _budget_exhausted and config.voice_delegation_budget_pause_enabled:
                from app.services.markdown_sanitizer import sanitize_markdown

                partial = sanitize_markdown((accumulated_response or "").strip()) or None
                await self._finalize_separate_waiting(
                    task_id=task_id,
                    kind="budget",
                    question=(
                        "已达到本任务的执行预算。回复“继续”可再获得 "
                        f"{config.voice_delegation_budget_resume_seconds} 秒执行预算。"
                    ),
                    attempt_id=attempt_id,
                    require_attempt=True,
                    result=partial,
                    usage_meta={
                        **cumulative_usage,
                        "budget_extension_seconds": (
                            budget_extension
                            + config.voice_delegation_budget_resume_seconds
                        ),
                    },
                    runtime_meta={
                        "policy": policy,
                        "requested_effort": reasoning_effort,
                        "wire_provider_type": provider_type,
                        "elapsed_seconds": cumulative_usage["elapsed_seconds"],
                        "tool_calls": len(tool_results_accumulated),
                        "budget_exhausted": True,
                    },
                    workspace_meta=workspace_meta_payload,
                )
                await self._notify_voice_terminal(task_id)
                return

            final_text = (accumulated_response or "").strip()
            if _loop_error:
                await self._finalize_separate_failure(
                    task_id, attempt_id,
                    f"执行失败：{_loop_error}（严格策略下不会静默改用普通模式）",
                )
                return
            if not final_text:
                await self._finalize_separate_failure(
                    task_id, attempt_id,
                    "任务执行结束但没有生成文本输出；不视为成功完成",
                )
                return

            if workspace_meta_payload:
                _ws = workspace_meta_payload
                _ws_lines = ["", "—", f"变更分支：{_ws.get('branch') or ''}",
                             f"基准分支：{_ws.get('base_branch') or ''}",
                             f"提交：{_ws.get('commit') or ''}"]
                if _ws.get("files"):
                    _ws_lines.append(
                        "变更文件：" + "、".join(_ws["files"][:20])
                        + ("…" if len(_ws["files"]) > 20 else ""))
                if _ws.get("error"):
                    _ws_lines.append(f"工作树提交失败：{_ws['error']}")
                final_text += "\n" + "\n".join(_ws_lines)

            reasoning_degraded = bool(getattr(agent_loop, "reasoning_degraded", False))
            if policy == "strict_highest" and reasoning_degraded:
                await self._finalize_separate_failure(
                    task_id, attempt_id,
                    "严格最高思考档在运行时发生降级或供应商切换；按严格策略停止，"
                    "不静默改用普通模式或其它供应商",
                )
                return

            # Unconsumed steer must block completion (§7.2).
            async with AsyncSessionLocal() as pend_db:
                row = (await pend_db.execute(
                    text("SELECT pending_messages FROM agent_tasks WHERE id = :id"),
                    {"id": task_id},
                )).fetchone()
            if row and row[0]:
                try:
                    remaining = json.loads(row[0]) or []
                except (TypeError, json.JSONDecodeError):
                    remaining = []
                if remaining:
                    await self._mark_separate_resumable(
                        task_id, attempt_id,
                        "有尚未消费的补充要求；保留检查点，下一轮继续执行",
                        usage_meta=cumulative_usage)
                    return

            from app.services.markdown_sanitizer import sanitize_markdown

            transformed = None
            if tool_results_accumulated:
                try:
                    transformed = _transform_tool_loop_results(
                        tool_results_accumulated,
                    )
                except Exception:
                    transformed = None

            if reasoning_enabled and reasoning_degraded:
                reasoning_status = "degraded"
            elif reasoning_enabled:
                reasoning_status = "fulfilled"
            elif policy != "strict_highest":
                reasoning_status = "disabled"
            else:
                reasoning_status = "unavailable"
            final = await self._finalize_separate_new_session(
                task_id=task_id,
                status="timed_out" if _timed_out else "completed",
                attempt_id=attempt_id,
                require_attempt=True,
                result=sanitize_markdown(final_text),
                tool_results=transformed,
                reasoning_status=reasoning_status,
                runtime_meta={
                    "policy": policy,
                    "requested_effort": reasoning_effort,
                    "effective_reasoning_effort": (
                        None if reasoning_degraded else reasoning_effort),
                    "degradation_reason": (
                        "thinking_off_retry" if reasoning_degraded else None),
                    "wire_provider_type": provider_type,
                    "elapsed_seconds": total_elapsed,
                    "tool_calls": len(tool_results_accumulated),
                },
                usage_meta=cumulative_usage,
                workspace_meta=workspace_meta_payload,
            )
            if final is None:
                logger.warning("separate task %s finalize fenced (stale attempt)", task_id)
                return
            if final.get("blocked") == "pending_steer":
                await self._mark_separate_resumable(
                    task_id, attempt_id,
                    "完成前收到未消费的补充要求；保留检查点，下一轮继续执行")
                return
            await self._notify_voice_terminal(task_id)
        except asyncio.CancelledError:
            if pending_poller:
                pending_poller.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_poller
            if heartbeat_task:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            current_status = ""
            try:
                async with AsyncSessionLocal() as st_db:
                    row = (await st_db.execute(
                        text("SELECT status FROM agent_tasks WHERE id = :id"),
                        {"id": task_id},
                    )).fetchone()
                current_status = row[0] if row else ""
            except Exception:
                current_status = ""
            from app.services.agent_task_finalize import finalize_separate_task

            if current_status == "cancelling":
                await self._finalize_separate_new_session(
                    task_id=task_id, status="cancelled",
                    attempt_id=attempt_id, require_attempt=True,
                    error="取消请求已确认（在途工具结果未知）",
                )
            elif self._draining:
                await self._mark_separate_resumable(
                    task_id, attempt_id, "服务优雅停机；检查点已保留")
            else:
                await self._finalize_separate_new_session(
                    task_id=task_id, status="cancelled",
                    attempt_id=attempt_id, require_attempt=True,
                    error="worker 被取消",
                )
            raise
        except Exception as exc:
            if pending_poller:
                pending_poller.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_poller
            if heartbeat_task:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            logger.exception("separate task %s failed", task_id)
            await self._finalize_separate_failure(task_id, attempt_id, str(exc))

    async def _finalize_separate_failure(
        self, task_id: str, attempt_id: str | None, error: str,
    ) -> None:
        try:
            await self._finalize_separate_new_session(
                task_id=task_id, status="failed",
                attempt_id=attempt_id, require_attempt=bool(attempt_id),
                error=error,
            )
            await self._notify_voice_terminal(task_id)
        except Exception:
            logger.exception("separate task %s failure finalize failed", task_id)

    async def _finalize_separate_new_session(self, **kwargs):
        """Finalize on an owned session (never leak a row lock / connection)."""
        from app.services.agent_task_finalize import finalize_separate_task

        async with AsyncSessionLocal() as fdb:
            return await finalize_separate_task(fdb, **kwargs)

    async def _finalize_separate_waiting(self, **kwargs):
        from app.services.agent_task_finalize import finalize_separate_waiting

        async with AsyncSessionLocal() as fdb:
            return await finalize_separate_waiting(fdb, **kwargs)

    async def _mark_separate_resumable(
        self, task_id: str, attempt_id: str | None, reason: str,
        usage_meta: dict | None = None,
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                values = {
                    "status": "resumable",
                    "error": reason,
                    "completed_at": None,
                    "attempt_id": None,
                    "lease_until": None,
                    "heartbeat_at": None,
                    "status_version": AgentTask.status_version + 1,
                    "updated_at": datetime.utcnow(),
                }
                if usage_meta is not None:
                    values["usage_meta"] = json.dumps(usage_meta, ensure_ascii=False)
                await db.execute(
                    update(AgentTask)
                    .where(
                        AgentTask.id == task_id,
                        AgentTask.status.in_(("claimed", "running")),
                        AgentTask.attempt_id == attempt_id,
                    )
                    .values(**values)
                )
                await db.commit()
        except Exception:
            logger.exception("separate task %s resumable mark failed", task_id)

    async def _notify_voice_terminal(self, task_id: str) -> None:
        try:
            from app.services.voice_service import notify_voice_task_finished

            await notify_voice_task_finished(task_id)
        except Exception:
            logger.debug("voice notify on separate task completion failed", exc_info=True)

    async def _poll_pending_messages(self, task_id: str, queue: asyncio.Queue) -> None:
        """F1/H2：每 5s 读取 agent_tasks.pending_messages 并推入 interjection_queue。

        投递成功后清空列（已投递消息不再重复推送）。失败静默（steer 是增强，
        绝不影响任务主流程）。
        """
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    # A4.9 I1：行锁（FOR UPDATE）串行化「读-投递-清空」与 API 追加，
                    # 防止 SELECT 与 UPDATE 之间新追加的 steer 被 NULL 覆盖丢失
                    row = (await db.execute(
                        text("SELECT pending_messages FROM agent_tasks WHERE id = :id FOR UPDATE"),
                        {"id": task_id},
                    )).fetchone()
                    if row and row[0]:
                        try:
                            messages = json.loads(row[0]) or []
                        except (json.JSONDecodeError, TypeError):
                            messages = []
                        delivered = 0
                        for m in messages:
                            if isinstance(m, dict) and str(m.get("content") or "").strip():
                                queue.put_nowait(str(m["content"]))
                                delivered += 1
                        if delivered:
                            await db.execute(
                                text("UPDATE agent_tasks SET pending_messages = NULL WHERE id = :id"),
                                {"id": task_id},
                            )
                            await db.commit()
                            logger.info("Background task %s: delivered %d steering message(s)",
                                        task_id, delivered)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("pending message poll failed (silent)", exc_info=True)
            await asyncio.sleep(5)

    def _extract_attachments(self, tool_results_accumulated: list[dict]) -> list[dict]:
        """Explicit-delivery policy (user directive 2026-09-18, prod conv 8b382ad8;
        revised 2026-09-27 for pdf_export, user ruling D-302, prod conv b8dcedc8).

        Download cards come from the agent's explicit delivery tools —
        ``provide_file`` / ``provide_folder`` (2026-09-19) and ``pdf_export``
        (2026-09-27: a successful export IS the user-facing deliverable).
        Byproducts of execute_code / terminal / other file-producing
        tools are never auto-attached: an analysis background task must not
        offer the files it analyzed. Dedup key: ``rel_path`` (or legacy
        absolute ``path``), so both card generations work.
        """
        provided_attachments = []
        seen_paths: set[str] = set()

        def _add(entry: dict, *, folder: bool) -> None:
            if folder and entry.get("type") != "folder":
                entry = {**entry, "type": "folder"}
            fpath = entry.get("rel_path") or entry.get("path") or ""
            if not fpath or fpath in seen_paths:
                return
            seen_paths.add(fpath)
            provided_attachments.append(entry)

        for tr in tool_results_accumulated:
            if tr.get("name") not in ("provide_file", "provide_folder", "pdf_export"):
                continue
            raw_result = tr.get("result", "")
            if not raw_result:
                continue
            try:
                parsed = json.loads(raw_result)
            except (json.JSONDecodeError, TypeError):
                continue
            if tr.get("name") == "pdf_export" and not parsed.get("success"):
                continue
            for f in parsed.get("generated_files") or []:
                if isinstance(f, dict):
                    _add(f, folder=False)
            for f in parsed.get("generated_folders") or []:
                if isinstance(f, dict):
                    _add(f, folder=True)
        return provided_attachments

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

    async def _mark_drained(self, task_id: str) -> None:
        """优雅停机中断 → resumable（有快照）/failed（无）。

        状态守卫：仅 running/claimed 可被 drain 改写——绝不把已 completed /
        cancelled / 等终态复活为 resumable（违反「显式取消仍 cancelled」）。
        """
        try:
            from app.services.task_checkpoint import drain_status_values
            async with AsyncSessionLocal() as db:
                task = await db.get(AgentTask, task_id)
                if task is None:
                    return
                has_cp = bool(getattr(task, "checkpoint", None))
                vals = drain_status_values(has_cp)
                stmt = (
                    update(AgentTask)
                    .where(AgentTask.id == task_id,
                           AgentTask.status.in_(("running", "claimed")))
                    .values(
                        status=vals["status"],
                        error=("Task interrupted by graceful shutdown; checkpoint preserved (resumable)"
                               if has_cp else "Task interrupted by graceful shutdown (no checkpoint)"),
                        completed_at=vals["completed_at"],
                        updated_at=datetime.utcnow(),
                    )
                )
                result = await db.execute(stmt)
                await db.commit()
                if result.rowcount == 0:
                    logger.info("drain skip: task %s already in terminal state", task_id)
        except Exception:
            logger.exception("Failed to mark task %s as drained", task_id)


agent_worker = AgentWorker()
