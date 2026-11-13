# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""chat / voice / 后台链路的作用域装配（2026-11-10 波 · T8）。

设计契约 §6.1 / §10 / §14：

* `off` → 完全不解析（现网零变化）；
* `shadow` → 解析并记录「旧根 vs 提案 Task 根」，不改变行为、不建 run；
* `enforce` → 解析绑定 → 开启 run/attempt → 把 frozen `WorkspaceContext` 交给 AgentLoop；
  未支持的运行模式返回 `workspace_scope_mode_unsupported`（保留当前绑定），
  **绝不静默回退用户根**。

同一会话的上一条 run 在新请求进入时先 park（= 设计 §6.3「先 stop/park 再操作」的
运行时形态）：保证「同一 Task 默认单写 run」，也让改绑不会被历史 run 卡死。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from app.services import task_binding_service as tbs
from app.services import workspace_run_service as yrs
from app.services import workspace_scope as ws
from app.services import workspace_scope_service as svs

logger = logging.getLogger(__name__)


@dataclass
class ScopedRunResult:
    mode: str
    action: str = ""
    ctx: Any = None
    run_id: str | None = None
    attempt_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    binding_version: int | None = None
    system_memory_ingest: str | None = None
    question: str | None = None
    candidates: list = field(default_factory=list)
    legacy_root: str | None = None

    @property
    def enforced(self) -> bool:
        return self.ctx is not None


async def attach_chat_scope(db, *, current_user, conversation, workspace, agent_loop=None,
                            continuation_hint: str | None = None,
                            explicit_task_id: str | None = None,
                            execution_target: str | None = None,
                            current_message_id: str | None = None,
                            mode_name: str = "chat") -> ScopedRunResult:
    """解析绑定并（enforce 档）开启 run。任何失败都 raise，由调用方决定回包。"""
    mode = svs.scoping_mode()
    legacy_root = str(getattr(workspace, "root_path", "") or "") or None
    if mode == "off":
        return ScopedRunResult(mode=mode, action="legacy", legacy_root=legacy_root)

    if mode == "shadow":
        resolution = await tbs.TaskBindingService.resolve(
            db, user_id=current_user.id, user_root=legacy_root, conversation=conversation,
            explicit_task_id=explicit_task_id, continuation_hint=continuation_hint)
        proposed = str(resolution.view.task_root / "workspace") if resolution.view else None
        svs._shadow_log(chain=mode_name, action=resolution.action, legacy=legacy_root,
                        proposed=proposed,
                        task_id=getattr(resolution.binding, "task_id", None),
                        binding_version=getattr(resolution.binding, "binding_version", None))
        await db.commit()
        return ScopedRunResult(mode=mode, action=resolution.action, legacy_root=legacy_root,
                               task_id=resolution.binding.task_id,
                               project_id=resolution.binding.project_id,
                               binding_version=resolution.binding.binding_version,
                               question=resolution.question,
                               candidates=[c.__dict__ for c in resolution.candidates])

    # enforce
    if legacy_root is None:
        raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING, "user workspace root unavailable", "")

    from sqlalchemy import text as _sql_text

    # §14「旧 Conversation 继续 legacy」：已有历史消息且从未绑定的会话**不换房**——
    # 否则一开启 enforce 就把老会话的文件域从用户根挪走，违反不变量 10。
    existing = await tbs.get_binding(db, user_id=current_user.id,
                                     conversation_id=conversation.id)
    if existing is None:
        # 排除**本轮刚持久化的用户消息**：流式链路先落 user 行再做作用域装配，
        # 若不排除，新会话首回合永远被误判为「先于 scoping 存在」而永远 legacy。
        # 真·旧会话排除最新一条后仍有历史 → 继续 legacy（不变量 10 不变）。
        if current_message_id:
            # 精确锚点：只排除本轮消息（uuid 排序在同秒批量数据下不可靠）。
            count = await db.execute(
                _sql_text("SELECT count(*) FROM messages WHERE conversation_id = :c "
                          "AND id <> :mid"),
                {"c": conversation.id, "mid": str(current_message_id)})
        else:
            count = await db.execute(
                _sql_text(
                    "SELECT count(*) FROM messages WHERE conversation_id = :c "
                    "AND id <> COALESCE((SELECT id FROM messages WHERE conversation_id = :c "
                    "AND role = 'user' ORDER BY created_at DESC, id DESC LIMIT 1), '')"),
                {"c": conversation.id})
        if int(count.scalar() or 0) > 0 and not explicit_task_id:
            logger.info("workspace scope: conversation %s predates scoping — staying legacy",
                        conversation.id)
            return ScopedRunResult(mode=mode, action="legacy", legacy_root=legacy_root)

    # 能力门在身份判定之后（与 attach_execution_scope 同序，acceptance #31）：
    # 只约束已作用域化/将被作用域化的执行；未绑定的 legacy 会话在任意链路保持旧根。
    svs.capability_guard(mode_name)

    previous = await yrs.active_run_for(db, conversation_id=conversation.id)
    if previous is not None:
        # 终态化上一条 run（写只读 handoff + 封存 attempt），否则 run 永远 running，
        # 改绑会被 workspace_run_active 永久挡住。
        await yrs.finish_run(db, user_root=Path(legacy_root), run_id=previous.id,
                             status="parked")
        logger.info("workspace scope: settled superseded run %s (conversation=%s)",
                    previous.id, conversation.id)

    resolution = await tbs.TaskBindingService.resolve(
        db, user_id=current_user.id, user_root=legacy_root, conversation=conversation,
        explicit_task_id=explicit_task_id, continuation_hint=continuation_hint)
    if resolution.action == "ambiguous":
        await db.rollback()
        return ScopedRunResult(mode=mode, action="ambiguous",
                               task_id=None, question=resolution.question,
                               candidates=[c.__dict__ for c in resolution.candidates],
                               legacy_root=legacy_root)
    await db.flush()

    bundle = await yrs.start_run(
        db, user_id=current_user.id, user_root=legacy_root, conversation_id=conversation.id,
        binding=resolution.binding, execution_target=execution_target,
        run_spec={"mode_name": mode_name, "conversation_id": conversation.id})
    await db.commit()

    if agent_loop is not None:
        agent_loop.workspace_ctx = bundle.ctx
        # 稳定 Task 文件域成为本轮工具默认根；workspace_path 保留给用户根展示/交付
        agent_loop.workspace_path = str(bundle.ctx.write_root)

    return ScopedRunResult(
        mode=mode, action=resolution.action, ctx=bundle.ctx, run_id=bundle.run.id,
        attempt_id=bundle.attempt.id, task_id=bundle.ctx.task_id, project_id=bundle.ctx.project_id,
        binding_version=bundle.ctx.binding_version,
        system_memory_ingest=bundle.ctx.system_memory_ingest, legacy_root=legacy_root)


async def record_user_message_scope(db, *, user_id: str, message_id: str,
                                    result: ScopedRunResult) -> None:
    """逐消息作用域快照：改绑/策略修改都不能追溯改写这条消息的来源。"""
    if not result.enforced or not message_id:
        return
    await tbs.record_message_context(
        db, message_id=message_id, user_id=user_id, task_id=str(result.task_id),
        run_id=result.run_id, attempt_id=result.attempt_id,
        binding_version=int(result.binding_version or 1),
        system_memory_ingest=result.system_memory_ingest)


async def finalize_scope_run(db, *, run_id: str | None, status: str = "completed") -> None:
    if not run_id:
        return
    try:
        root = await _root_for(db, run_id)
        await yrs.finish_run(db, user_root=root, run_id=run_id, status=status)
    except ws.WorkspaceScopeError as exc:
        logger.info("workspace run finalize skipped: %s (run=%s)", exc.code, run_id)


async def _root_for(db, run_id: str) -> Path:
    from app.services.workspace_service import ensure_user_workspace
    from app.db.database import WorkspaceRun

    run = await db.get(WorkspaceRun, run_id)
    if run is None:
        raise ws.WorkspaceScopeError(ws.CODE_ATTEMPT_STALE, "run not found", run_id)
    workspace = await ensure_user_workspace(db, run.user_id)
    if workspace is None:
        raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING, "user workspace not found",
                                     run.user_id)
    return Path(workspace.root_path).resolve()


def entry_guard(mode_name: str) -> None:
    """§14 分阶段能力门：enforce 档下未列入 `supported_modes` 的链路必须在
    **创建/启动前**明确拒绝（`workspace_scope_mode_unsupported`），不得静默用 legacy 根。

    off / shadow 档不拦截（现网零变化 + shadow 只观测）。
    """
    if svs.scoping_mode() != "enforce":
        return
    svs.capability_guard(mode_name)


async def attach_execution_scope(db, *, user_id, workspace_root, agent_loop=None,
                                 owner_kind: str | None = None, owner_id: str | None = None,
                                 conversation=None, mode_name: str = "background") -> ScopedRunResult:
    """后台/定时/独立委派/设备链路的作用域装配（§10）。

    优先用 `workspace_execution_bindings` 里持久化的 task/run 身份恢复同一个 run；
    没有绑定行时按会话绑定新建 run 并补写执行绑定（recurring 每次 run 独立成行）。
    """
    mode = svs.scoping_mode()
    legacy_root = str(workspace_root or "") or None
    if mode != "enforce":
        return ScopedRunResult(mode=mode, action="legacy", legacy_root=legacy_root)
    if not legacy_root:
        raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING, "workspace root unavailable", "")

    # 身份先行、门控其次（acceptance #31）：能力门只约束**已被作用域化**的执行
    # （有 execution binding、或会话已有 Task 绑定）；从未绑定的 legacy 执行继续
    # 用旧根——enforce 不得把 legacy 会话的语音/后台/设备链路一刀切拒掉。
    task_id = None
    run_id = None
    if owner_kind and owner_id:
        eb = await yrs.execution_binding_for(db, owner_kind=owner_kind, owner_id=str(owner_id),
                                             user_id=str(user_id))
        if eb is not None:
            task_id, run_id = str(eb.task_id), (str(eb.run_id) if eb.run_id else None)
    if run_id:
        from app.db.database import WorkspaceRun as _Run

        run_row = await db.get(_Run, run_id)
        if run_row is not None and run_row.status in yrs.SETTLED_RUN_STATUSES:
            # recurring/重跑：上一 run 已终态 → 本次执行开**新 run**（新绑定行），
            # 不复用终态 run（manifest/handoff 语义会失真，且改绑会被永久卡死）。
            run_id = None
            task_id = str(run_row.task_id)
    if task_id is None and conversation is not None:
        conv_binding = await tbs.get_binding(db, user_id=user_id,
                                             conversation_id=str(getattr(conversation, "id", "") or ""))
        if conv_binding is not None:
            task_id = str(conv_binding.task_id)
    if task_id is None and run_id is None:
        return ScopedRunResult(mode=mode, action="legacy", legacy_root=legacy_root)
    entry_guard(mode_name)
    if run_id:
        ctx = await yrs.load_run_context(db, user_id=user_id, user_root=legacy_root, run_id=run_id)
    else:
        from types import SimpleNamespace as _NS

        from app.services import workspace_task_service as svc

        if not task_id:
            raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING,
                                         "no task identity for this execution", mode_name)
        task = await svc.get_task(db, user_id=user_id, task_id=task_id)
        if task is None:
            raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
        _bv = 1
        if conversation is not None:
            _cb = await tbs.get_binding(db, user_id=user_id,
                                        conversation_id=str(getattr(conversation, "id", "") or ""))
            if _cb is not None:
                _bv = int(_cb.binding_version)
        binding = _NS(conversation_id=None, user_id=user_id, task_id=task.id,
                      project_id=task.project_id, binding_version=_bv,
                      system_memory_ingest=task.system_memory_ingest)
        bundle = await yrs.start_run(db, user_id=user_id, user_root=legacy_root,
                                     conversation_id=None, binding=binding,
                                     run_spec={"mode_name": mode_name, "owner_kind": owner_kind,
                                               "owner_id": owner_id},
                                     task=task)
        ctx = bundle.ctx
        run_id = bundle.run.id
        if owner_kind and owner_id:
            await yrs.bind_execution(db, owner_kind=owner_kind, owner_id=str(owner_id),
                                     user_id=user_id, task_id=task.id, run_id=run_id,
                                     binding_version=ctx.binding_version)
    await db.commit()
    if agent_loop is not None:
        agent_loop.workspace_ctx = ctx
        agent_loop.workspace_path = str(ctx.write_root)
    return ScopedRunResult(mode=mode, action="attached", ctx=ctx, run_id=run_id,
                           attempt_id=ctx.attempt_id, task_id=ctx.task_id,
                           project_id=ctx.project_id, binding_version=ctx.binding_version,
                           system_memory_ingest=ctx.system_memory_ingest, legacy_root=legacy_root)


async def finish_execution_scope(run_id: str | None, *, status: str = "completed") -> None:
    """执行链 run 的收尾终态（worker/scheduler 完成面，best-effort）。

    没有这一步 run 永远 running：recurring 执行会复用终态语义失真的旧 run，
    且 Task 改绑被 `workspace_run_active` 永久卡死（评审 r2 #4）。
    """
    if not run_id:
        return
    try:
        from app.db.database import AsyncSessionLocal, WorkspaceRun
        from app.services import workspace_run_service as _yrs
        from app.services.workspace_service import ensure_user_workspace

        async with AsyncSessionLocal() as db:
            run = await db.get(WorkspaceRun, str(run_id))
            if run is None or run.status in _yrs.SETTLED_RUN_STATUSES:
                return
            ws_row = await ensure_user_workspace(db, run.user_id)
            await _yrs.finish_run(db, user_root=ws_row.root_path, run_id=str(run_id),
                                  status=status)
            await db.commit()
    except Exception:  # noqa: BLE001 — 收尾失败不反噬业务终态；下次 attach 有兜底
        logger.warning("workspace run finish failed (run=%s status=%s)", run_id, status,
                       exc_info=True)


async def scope_snapshot(db, *, current_user, conversation) -> dict[str, Any]:
    """给前端/诊断用的当前绑定概况（只读，不建 run）。"""
    mode = svs.scoping_mode()
    binding = await tbs.get_binding(db, user_id=current_user.id,
                                   conversation_id=str(getattr(conversation, "id", "") or ""))
    if binding is None:
        return {"mode": mode, "bound": False, "task": None, "project": None,
                "binding_version": None, "note": "legacy（未绑定任务）"}
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=current_user.id, task_id=binding.task_id)
    project = await svc.get_project(db, user_id=current_user.id,
                                    project_id=binding.project_id) if binding.project_id else None
    return {
        "mode": mode,
        "bound": True,
        "task": {"id": task.id, "title": task.title, "status": task.status,
                 "display_root": f"{task.root_rel}/workspace"} if task else None,
        "project": {"id": project.id, "name": project.name} if project else None,
        "binding_version": binding.binding_version,
        "system_memory_ingest": binding.system_memory_ingest,
        "supported_modes": list(svs.supported_modes()),
    }


def unsupported_payload(exc: ws.WorkspaceScopeError) -> Mapping[str, Any]:
    return {"error": str(exc), "code": exc.code, "success": False}
