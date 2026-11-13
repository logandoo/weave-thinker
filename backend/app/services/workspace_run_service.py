# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Run / Attempt 生命周期与 lease fencing（2026-11-10 波 · T4）。

设计契约 §4.2 / §5 目录规则 / §11.3：

* 一个 run 一个 `run_spec`；每次 retry/takeover 新增 attempt row 并保存完整冻结上下文；
* `lease_epoch` 单调递增，`workspace_runs.current_attempt_id` 用 PG `FOR UPDATE` + CAS 切换，
  并发接管只有一个成为当前；
* 存储写入前必须 `assert_write_epoch()`：attempt 不是当前、epoch 落后、或绑定已改
  → 拒绝（旧 attempt 只能写自己的隔离目录）；
* 重启后按 `run + current attempt` 重建上下文，**不从 cwd 或最新文件猜**；
* run 结束写 `run-spec.json` 投影与只读 `handoff.md`（新 attempt 只能读 handoff）。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from app.db.database import (
    WorkspaceAttempt,
    WorkspaceExecutionBinding,
    WorkspaceRun,
)
from app.services import task_binding_service as tbs
from app.services import workspace_scope as ws
from app.services import workspace_task_service as svc

logger = logging.getLogger(__name__)

ACTIVE_RUN_STATUSES = ("running", "waiting_input", "waiting_auth", "waiting_budget", "resumable")
SETTLED_RUN_STATUSES = ("completed", "failed", "cancelled", "parked", "sealed")
ATTEMPT_DIR_LEAVES = ("tests", "memory", "scratch", "artifacts", ".vibeweaver")


@dataclass
class RunBundle:
    run: WorkspaceRun
    attempt: WorkspaceAttempt
    ctx: ws.WorkspaceContext


# --- 内部 ---------------------------------------------------------------------

async def _lock_run(db, run_id: str) -> WorkspaceRun | None:
    res = await db.execute(
        select(WorkspaceRun).where(WorkspaceRun.id == run_id).with_for_update()
    )
    return res.scalar_one_or_none()


async def _rows_for_run(db, *, run: WorkspaceRun) -> tuple[Any, Any, Any]:
    """(project, task, binding-view row)；无 conversation 的执行链用 run 行合成绑定视图。"""
    task = await svc.get_task(db, user_id=run.user_id, task_id=run.task_id)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "run's task is gone", run.task_id)
    project = await svc.get_project(db, user_id=run.user_id, project_id=task.project_id)
    if project is None:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "run's project is gone",
                                     task.project_id or "")
    binding = None
    if run.conversation_id:
        binding = await tbs.get_binding(db, user_id=run.user_id, conversation_id=run.conversation_id)
    if binding is None:
        binding = SimpleNamespace(
            conversation_id=run.conversation_id, user_id=run.user_id, task_id=task.id,
            project_id=project.id, binding_version=int(run.binding_version or 1),
            system_memory_ingest=task.system_memory_ingest,
        )
    return project, task, binding


def _ensure_attempt_dirs(ctx: ws.WorkspaceContext) -> None:
    for leaf in ATTEMPT_DIR_LEAVES:
        (ctx.attempt_root / leaf).mkdir(parents=True, exist_ok=True)
    ctx.run_root.mkdir(parents=True, exist_ok=True)
    (ctx.write_root / "inputs").mkdir(parents=True, exist_ok=True)
    (ctx.write_root / "artifacts").mkdir(parents=True, exist_ok=True)


def _write_run_projection(ctx: ws.WorkspaceContext, run: WorkspaceRun,
                          run_spec: Mapping[str, Any] | None) -> None:
    payload = {
        "authority": "database",
        "run_id": run.id,
        "task_id": run.task_id,
        "project_id": run.project_id,
        "binding_version": run.binding_version,
        "status": run.status,
        "run_spec": dict(run_spec or {}),
        "rel": dict(ctx.rel),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    try:
        (ctx.run_root / "run-spec.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("run-spec projection write failed: %s", exc)


async def _new_attempt_row(db, *, run: WorkspaceRun, epoch: int, ctx: ws.WorkspaceContext,
                           reason: str, attempt_id: str) -> WorkspaceAttempt:
    attempt = WorkspaceAttempt(
        id=attempt_id, run_id=run.id, user_id=run.user_id, lease_epoch=epoch,
        status="active",
        frozen_context_json=json.dumps({**ctx.to_json_dict(), "reason": reason},
                                       ensure_ascii=False, sort_keys=True),
        attempt_root=ctx.rel["attempt"], artifact_root=ctx.rel["attempt"] + "/artifacts",
        state_root=ctx.rel["attempt"],
    )
    db.add(attempt)
    await db.flush()
    return attempt


# --- 生命周期 -----------------------------------------------------------------

async def start_run(db, *, user_id: str, user_root: Path | str, conversation_id: str | None,
                    binding, run_spec: Mapping[str, Any] | None = None,
                    execution_target: str | None = None,
                    task=None, project=None) -> RunBundle:
    """为一个绑定开启新 run + 首个 attempt（epoch=1），并物化 frozen WorkspaceContext。"""
    base = Path(user_root).resolve()
    if task is None:
        task = await svc.get_task(db, user_id=user_id, task_id=binding.task_id)
        if task is None:
            raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user",
                                         binding.task_id)
    if project is None:
        project = await svc.get_project(db, user_id=user_id, project_id=task.project_id)
        if project is None:
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "project not found",
                                         task.project_id or "")

    run = WorkspaceRun(
        id=str(uuid.uuid4()), task_id=task.id, project_id=project.id, user_id=user_id,
        conversation_id=conversation_id, binding_version=int(binding.binding_version),
        run_spec_json=json.dumps(dict(run_spec or {}), ensure_ascii=False),
        status="running", lease_epoch=0,
    )
    db.add(run)
    await db.flush()

    attempt_id = str(uuid.uuid4())
    # 先定指针再建上下文：校验要求 attempt 就是 run 的当前 attempt
    run.current_attempt_id = attempt_id
    run.lease_epoch = 1
    await db.flush()

    ctx = ws.build_workspace_context(
        user_id=user_id, user_root=base, project=project, task=task, binding=binding,
        run=run,
        attempt=SimpleNamespace(id=attempt_id, run_id=run.id, user_id=user_id, lease_epoch=1,
                                status="active", frozen_context_json=None),
        existing_task_roots=await svc.existing_task_roots(db, user_id=user_id,
                                                          exclude_task_id=task.id),
        execution_target=execution_target)
    attempt = await _new_attempt_row(db, run=run, epoch=1, ctx=ctx, reason="start",
                                     attempt_id=attempt_id)
    _ensure_attempt_dirs(ctx)
    _write_run_projection(ctx, run, run_spec)
    await svc.touch_task(db, task_id=task.id)
    try:
        # 首 run 激活：staging 附件按服务端实算 hash 交接为 inputs（§10），
        # 再把过期/未交接的 staging 清理掉（不产生不可追踪文件）。
        from app.services.workspace_artifact_service import (
            auto_commit_staged as _auto_commit,
            sweep_staging as _sweep,
        )

        await _auto_commit(db, ctx)
        await _sweep(db, now=datetime.utcnow())
    except Exception:
        logger.debug("staging commit/sweep skipped", exc_info=True)
    return RunBundle(run=run, attempt=attempt, ctx=ctx)


async def new_attempt(db, *, user_root: Path | str, run_id: str,
                   reason: str = "retry") -> RunBundle:
    """接管：单事务内建新 attempt、提升 epoch、CAS 切换当前指针、封存旧 attempt。"""
    run = await _lock_run(db, run_id)
    if run is None:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, "run not found", run_id)
    if run.status in SETTLED_RUN_STATUSES and run.status not in ("parked", "resumable"):
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, f"run is {run.status}", run_id)

    project, task, binding = await _rows_for_run(db, run=run)
    if int(binding.binding_version) != int(run.binding_version):
        raise ws.WorkspaceScopeError(
            ws.CODE_BINDING_STALE,
            f"binding moved to v{binding.binding_version}, run is v{run.binding_version}", run.id)

    epoch = int(run.lease_epoch or 0) + 1
    old_attempt_id = run.current_attempt_id
    old_epoch = int(run.lease_epoch or 0)
    attempt_id = str(uuid.uuid4())
    base = Path(user_root).resolve()

    ctx = ws.build_workspace_context(
        user_id=run.user_id, user_root=base, project=project, task=task, binding=binding,
        run=SimpleNamespace(id=run.id, task_id=run.task_id, project_id=run.project_id,
                            user_id=run.user_id, conversation_id=run.conversation_id,
                            binding_version=run.binding_version, status="running",
                            current_attempt_id=attempt_id, lease_epoch=epoch, run_spec_json=None),
        attempt=SimpleNamespace(id=attempt_id, run_id=run.id, user_id=run.user_id,
                                lease_epoch=epoch, status="active", frozen_context_json=None),
        existing_task_roots=await svc.existing_task_roots(db, user_id=run.user_id,
                                                          exclude_task_id=task.id))
    # CAS：只有仍持有当前指针的行能推进（并发接管在此串行化/落败方显式失败）
    res = await db.execute(
        WorkspaceRun.__table__.update()
        .where(WorkspaceRun.id == run.id,
               WorkspaceRun.current_attempt_id == old_attempt_id,
               WorkspaceRun.lease_epoch == old_epoch)
        .values(current_attempt_id=attempt_id, lease_epoch=epoch, status="running",
                updated_at=datetime.utcnow())
    )
    if res.rowcount != 1:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE,
                                     "concurrent takeover lost the CAS race", run_id)
    attempt = await _new_attempt_row(db, run=run, epoch=epoch, ctx=ctx, reason=reason,
                                     attempt_id=attempt_id)
    if old_attempt_id:
        old = await db.get(WorkspaceAttempt, old_attempt_id)
        if old is not None and old.status == "active":
            old.status = "superseded"
            old.ended_at = datetime.utcnow()
    run.current_attempt_id = attempt_id
    run.lease_epoch = epoch
    run.status = "running"
    await db.flush()

    _ensure_attempt_dirs(ctx)
    return RunBundle(run=run, attempt=attempt, ctx=ctx)


async def assert_write_epoch(db, ctx: ws.WorkspaceContext) -> None:
    """每次稳定 Task 写入前的 fencing 检查（fail closed）。"""
    run = await _lock_run(db, ctx.run_id)
    if run is None:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, "run no longer exists", ctx.run_id)
    if str(run.current_attempt_id or "") != ctx.attempt_id or int(run.lease_epoch or 0) != int(ctx.lease_epoch):
        raise ws.WorkspaceScopeError(
            ws.CODE_ATTEMPT_STALE,
            f"attempt {ctx.attempt_id} epoch {ctx.lease_epoch} is no longer current "
            f"(current={run.current_attempt_id} epoch={run.lease_epoch})", ctx.run_id)
    if run.status in SETTLED_RUN_STATUSES:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, f"run is {run.status}", ctx.run_id)
    if run.conversation_id:
        binding = await tbs.get_binding(db, user_id=ctx.user_id, conversation_id=run.conversation_id)
        if binding is not None and int(binding.binding_version) != int(ctx.binding_version):
            raise ws.WorkspaceScopeError(
                ws.CODE_BINDING_STALE,
                f"binding_version {ctx.binding_version} superseded by v{binding.binding_version}",
                ctx.run_id)


async def finish_run(db, *, user_root: Path | str, run_id: str, status: str = "completed",
                     handoff: str | None = None) -> WorkspaceRun:
    """终态：写只读 handoff、封存 attempt、更新 run 状态。"""
    if status not in SETTLED_RUN_STATUSES:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "unknown run status", status)
    run = await _lock_run(db, run_id)
    if run is None:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, "run not found", run_id)
    project, task, binding = await _rows_for_run(db, run=run)
    attempt = await db.get(WorkspaceAttempt, run.current_attempt_id) if run.current_attempt_id else None
    if attempt is not None:
        ctx = ws.build_workspace_context(
            user_id=run.user_id, user_root=Path(user_root).resolve(), project=project, task=task,
            binding=binding, run=run, attempt=attempt,
            existing_task_roots=await svc.existing_task_roots(db, user_id=run.user_id,
                                                              exclude_task_id=task.id))
        _ensure_attempt_dirs(ctx)
        try:
            body = handoff if handoff is not None else _default_handoff(ctx, run, status)
            (ctx.run_root / "handoff.md").write_text(body, encoding="utf-8")
        except OSError as exc:
            logger.warning("handoff write failed: %s", exc)
        attempt.status = "sealed"
        attempt.ended_at = datetime.utcnow()
    run.status = status
    run.updated_at = datetime.utcnow()
    await db.flush()
    return run


def _default_handoff(ctx: ws.WorkspaceContext, run: WorkspaceRun, status: str) -> str:
    return (
        f"# Run handoff（只读）\n\n"
        f"- run: {run.id}\n- task: {ctx.task_id}\n- project: {ctx.project_id}\n"
        f"- binding_version: {ctx.binding_version}\n- attempt: {ctx.attempt_id} "
        f"(lease_epoch {ctx.lease_epoch})\n- status: {status}\n\n"
        f"> 新 attempt / 新 run 只能读取本文件与本 Task 的 memory/，"
        f"不得继承上一 attempt 的可写工作假设。\n"
    )


async def park_run(db, *, run_id: str) -> WorkspaceRun:
    """停泊保任务：run 转 parked，attempt 封存；之后才允许改绑。"""
    run = await _lock_run(db, run_id)
    if run is None:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, "run not found", run_id)
    if run.current_attempt_id:
        attempt = await db.get(WorkspaceAttempt, run.current_attempt_id)
        if attempt is not None and attempt.status == "active":
            attempt.status = "sealed"
            attempt.ended_at = datetime.utcnow()
    run.status = "parked"
    run.updated_at = datetime.utcnow()
    await db.flush()
    return run


async def active_run_for(db, *, conversation_id: str | None = None,
                         task_id: str | None = None,
                         statuses: Sequence[str] = ACTIVE_RUN_STATUSES) -> WorkspaceRun | None:
    stmt = select(WorkspaceRun).where(WorkspaceRun.status.in_(tuple(statuses)))
    if conversation_id:
        stmt = stmt.where(WorkspaceRun.conversation_id == conversation_id)
    if task_id:
        stmt = stmt.where(WorkspaceRun.task_id == task_id)
    res = await db.execute(stmt.order_by(WorkspaceRun.created_at.desc()).limit(1))
    return res.scalar_one_or_none()


async def load_run_context(db, *, user_id: str, user_root: Path | str, run_id: str,
                           execution_target: str | None = None) -> ws.WorkspaceContext:
    """重启/恢复：按 run + current attempt 重建 frozen context。"""
    run = await db.get(WorkspaceRun, run_id)
    if run is None or str(run.user_id) != str(user_id):
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "run not found for user", run_id)
    if not run.current_attempt_id:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, "run has no current attempt", run_id)
    project, task, binding = await _rows_for_run(db, run=run)
    attempt = await db.get(WorkspaceAttempt, run.current_attempt_id)
    if attempt is None:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, "current attempt missing", run_id)
    return ws.build_workspace_context(
        user_id=user_id, user_root=user_root, project=project, task=task, binding=binding,
        run=run, attempt=attempt,
        existing_task_roots=await svc.existing_task_roots(db, user_id=user_id,
                                                          exclude_task_id=task.id),
        execution_target=execution_target)


# --- 执行链关联（AgentTask / ScheduledTask / durable job / voice）-------------

async def bind_execution(db, *, owner_kind: str, owner_id: str, user_id: str, task_id: str,
                         run_id: str | None, binding_version: int | None) -> WorkspaceExecutionBinding:
    if owner_kind not in ("agent_task", "scheduled_task", "durable_job", "voice"):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "unknown execution owner kind",
                                     owner_kind)
    res = await db.execute(
        select(WorkspaceExecutionBinding)
        .where(WorkspaceExecutionBinding.owner_kind == owner_kind,
               WorkspaceExecutionBinding.owner_id == owner_id,
               WorkspaceExecutionBinding.run_id == run_id)
    )
    existing = res.scalar_one_or_none()
    if existing is not None:
        return existing
    if run_id:
        # 提交期写的占位行（run_id 未知）由首个 run 填实，避免同一执行出现两行
        placeholder = (await db.execute(
            select(WorkspaceExecutionBinding)
            .where(WorkspaceExecutionBinding.owner_kind == owner_kind,
                   WorkspaceExecutionBinding.owner_id == owner_id,
                   WorkspaceExecutionBinding.run_id.is_(None))
            .order_by(WorkspaceExecutionBinding.bound_at.desc()).limit(1)
        )).scalar_one_or_none()
        if placeholder is not None:
            placeholder.run_id = run_id
            placeholder.task_id = task_id
            placeholder.binding_version = binding_version
            await db.flush()
            return placeholder
    row = WorkspaceExecutionBinding(owner_kind=owner_kind, owner_id=str(owner_id), user_id=user_id,
                                    task_id=task_id, run_id=run_id,
                                    binding_version=binding_version)
    db.add(row)
    await db.flush()
    return row


async def execution_binding_for(db, *, owner_kind: str, owner_id: str,
                                run_id: str | None = None,
                                user_id: str | None = None) -> WorkspaceExecutionBinding | None:
    stmt = select(WorkspaceExecutionBinding).where(
        WorkspaceExecutionBinding.owner_kind == owner_kind,
        WorkspaceExecutionBinding.owner_id == str(owner_id))
    if user_id:
        stmt = stmt.where(WorkspaceExecutionBinding.user_id == str(user_id))
    if run_id:
        stmt = stmt.where(WorkspaceExecutionBinding.run_id == run_id)
    res = await db.execute(stmt.order_by(WorkspaceExecutionBinding.bound_at.desc()).limit(1))
    return res.scalar_one_or_none()


async def execution_bindings_for_owner(db, *, owner_kind: str, owner_id: str):
    """recurring 任务的全部 run 关联（每次 run 独立成行，不覆盖旧关联）。"""
    res = await db.execute(
        select(WorkspaceExecutionBinding)
        .where(WorkspaceExecutionBinding.owner_kind == owner_kind,
               WorkspaceExecutionBinding.owner_id == str(owner_id))
        .order_by(WorkspaceExecutionBinding.bound_at.asc()))
    return list(res.scalars().all())


async def execution_bindings_for_task(db, *, user_id: str, task_id: str,
                                      limit: int = 20) -> list[WorkspaceExecutionBinding]:
    res = await db.execute(
        select(WorkspaceExecutionBinding)
        .where(WorkspaceExecutionBinding.user_id == user_id,
               WorkspaceExecutionBinding.task_id == task_id)
        .order_by(WorkspaceExecutionBinding.bound_at.desc())
        .limit(max(1, int(limit)))
    )
    return list(res.scalars().all())


__all__ = [
    "ACTIVE_RUN_STATUSES", "ATTEMPT_DIR_LEAVES", "RunBundle", "SETTLED_RUN_STATUSES",
    "active_run_for", "assert_write_epoch", "bind_execution", "execution_binding_for",
    "execution_bindings_for_task", "finish_run", "load_run_context", "new_attempt", "park_run",
    "start_run",
]
