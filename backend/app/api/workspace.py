# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Workspace 管理 API（2026-11-10 波 · T13 · 设计 §13.1）。

全部端点沿用登录 + owner 校验；**管理操作不产生任何系统记忆写入**。
`fork` 只复制用户选择的输入；`merge` 同名绝不静默覆盖，返回冲突清单。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.database import User, get_db
from app.services import workspace_scope as ws
from app.services import workspace_scope_service as svs

logger = logging.getLogger(__name__)
router = APIRouter()


def _raise(exc: ws.WorkspaceScopeError) -> None:
    status = 409 if exc.code in (ws.CODE_BINDING_STALE, ws.CODE_RUN_ACTIVE,
                                 ws.CODE_ATTEMPT_STALE, ws.CODE_MODE_UNSUPPORTED) else 403
    if exc.code in (ws.CODE_ROOT_INVALID, ws.CODE_RELATION_INVALID, ws.CODE_SCOPE_MISSING):
        status = 400
    raise HTTPException(status_code=status, detail={"error": str(exc), "code": exc.code})


async def _user_root(db: AsyncSession, user: User) -> Path:
    from app.services.workspace_service import ensure_user_workspace

    workspace = await ensure_user_workspace(db, user.id, user.username)
    if workspace is None:
        raise HTTPException(status_code=400, detail={"error": "workspace not initialized",
                                                     "code": ws.CODE_SCOPE_MISSING})
    return Path(workspace.root_path).resolve()


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    storage_authority: str = "cloud"
    system_memory_ingest: Optional[str] = None


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    goal: str = ""
    storage_authority: Optional[str] = None
    system_memory_ingest: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class BindingUpdate(BaseModel):
    task_id: str
    expected_binding_version: int
    reason: str = ""


class ForkRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    input_paths: List[str] = Field(default_factory=list)
    copy_memory: bool = False


class MergeRequest(BaseModel):
    source_task_id: str
    paths: List[str] = Field(default_factory=list)
    copy_memory: bool = False


class GrantCreate(BaseModel):
    current_task_id: str
    kind: str
    # 可选：把 grant 绑到具体 attempt/epoch/binding（默认自动取当前活跃 run，
    # 使改绑与接管立即失效——§7.5）
    run_id: Optional[str] = None
    attempt_id: Optional[str] = None
    lease_epoch: Optional[int] = None
    binding_version: Optional[int] = None
    source_project_id: Optional[str] = None
    source_task_id: Optional[str] = None
    allowed_paths: List[str] = Field(default_factory=list)
    server_tool: Optional[str] = None
    allowed_actions: List[str] = Field(default_factory=list)
    ttl_seconds: int = 3600


def _task_dict(task, *, root_rel: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": task.id, "project_id": task.project_id, "title": task.title, "goal": task.goal,
        "status": task.status, "root_rel": task.root_rel, "summary": task.summary,
        "storage_authority": task.storage_authority,
        "system_memory_ingest": task.system_memory_ingest,
        "display_root": f"{task.root_rel}/workspace",
        "last_active_at": task.last_active_at.isoformat() if task.last_active_at else None,
        "artifact_manifest": _manifest(task),
    }


def _manifest(task) -> list[dict[str, Any]]:
    try:
        data = json.loads(task.artifact_manifest_json or "[]")
        return data if isinstance(data, list) else []
    except ValueError:
        return []


# --- 能力矩阵 -----------------------------------------------------------------

class StagingCommit(BaseModel):
    upload_id: str
    sha256: str
    run_id: str


@router.post("/api/workspace/tasks/{task_id}/staging/commit")
async def commit_staged_upload(task_id: str, body: StagingCommit,
                               db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    """首 run 激活时把 staging 附件按 hash + CAS 原子交接为 inputs + artifact refs。"""
    from app.services import workspace_artifact_service as arts
    from app.services import workspace_run_service as yrs
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    root = await _user_root(db, user)
    try:
        ctx = await yrs.load_run_context(db, user_id=user.id, user_root=root, run_id=body.run_id)
        if str(ctx.task_id) != str(task_id):
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                         "run does not belong to this task", body.run_id)
        outcome = await arts.commit_staging(db, ctx, upload_id=body.upload_id, sha256=body.sha256)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    if not outcome.get("ok"):
        raise HTTPException(status_code=409, detail=outcome)
    return outcome


@router.get("/api/workspace/capabilities")
async def get_capabilities(user: User = Depends(get_current_user)):
    """§14：每个阶段发布 supported_modes；未列出的模式在 scoped 会话里被明确拒绝。"""
    from app.services.workspace_scoping_runtime import scope_snapshot  # noqa: F401 触发导入检查

    return {"mode": svs.scoping_mode(), "supported_modes": list(svs.supported_modes()),
            "all_modes": ["chat", "voice", "deathmatch", "background", "scheduler", "delegation",
                          "separate_task", "device", "durable", "browser", "media"],
            "enforcement": {"off": "全部走旧用户根", "shadow": "解析+记录，不拦截",
                            "enforce": "仅 supported_modes 内强制"}}


# --- Project / Task -----------------------------------------------------------

@router.get("/api/workspace/projects")
async def list_projects(db: AsyncSession = Depends(get_db),
                        user: User = Depends(get_current_user)):
    from app.services import workspace_task_service as svc

    projects = await svc.list_projects(db, user_id=user.id, statuses=("active", "archived"))
    return {"projects": [{"id": p.id, "name": p.name, "root_rel": p.root_rel,
                          "status": p.status, "storage_authority": p.storage_authority,
                          "system_memory_ingest": p.system_memory_ingest,
                          "last_active_at": p.last_active_at.isoformat() if p.last_active_at else None}
                         for p in projects]}


@router.post("/api/workspace/projects")
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    from app.services import workspace_task_service as svc

    root = await _user_root(db, user)
    try:
        project = await svc.create_project(
            db, user_id=user.id, user_root=root, name=body.name,
            storage_authority=body.storage_authority,
            system_memory_ingest=body.system_memory_ingest)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return {"id": project.id, "name": project.name, "root_rel": project.root_rel,
            "storage_authority": project.storage_authority,
            "system_memory_ingest": project.system_memory_ingest}


@router.get("/api/workspace/projects/{project_id}/tasks")
async def list_project_tasks(project_id: str, db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    from app.services import workspace_task_service as svc

    project = await svc.get_project(db, user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"error": "project not found"})
    tasks = await svc.list_tasks(db, user_id=user.id, project_id=project_id,
                                 statuses=("active", "completed", "archived"), limit=100)
    return {"tasks": [_task_dict(t) for t in tasks]}


@router.post("/api/workspace/projects/{project_id}/tasks")
async def create_task_in_project(project_id: str, body: TaskCreate,
                                 db: AsyncSession = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    from app.services import workspace_task_service as svc

    root = await _user_root(db, user)
    try:
        task = await svc.create_task(
            db, user_id=user.id, user_root=root, project_id=project_id, title=body.title,
            goal=body.goal, storage_authority=body.storage_authority,
            system_memory_ingest=body.system_memory_ingest, tags=body.tags)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return _task_dict(task)


@router.get("/api/workspace/tasks/{task_id}")
async def get_task_detail(task_id: str, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    from app.services import task_binding_service as tbs
    from app.services import workspace_memory_service as wmem
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    root = await _user_root(db, user)
    task_root = root / Path(*task.root_rel.split("/"))
    from sqlalchemy import select

    from app.db.database import WorkspaceBinding

    from sqlalchemy import select as _select

    from app.db.database import WorkspaceBinding as _WB

    bindings = await db.execute(_select(_WB).where(_WB.task_id == task_id,
                                                   _WB.user_id == user.id))
    conversations = [b.conversation_id for b in bindings.scalars().all()]
    return {
        "task": _task_dict(task),
        "bound_conversations": conversations,
        "memory": {"index": wmem.read_index(task_root / "memory"),
                   "topics": [t.as_dict() for t in wmem.list_topics(task_root / "memory")]},
        "runs": await _run_overview(db, task_id=task_id),
    }


async def _run_overview(db: AsyncSession, *, task_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.database import WorkspaceAttempt, WorkspaceRun

    res = await db.execute(select(WorkspaceRun).where(WorkspaceRun.task_id == task_id)
                            .order_by(WorkspaceRun.created_at.desc()).limit(20))
    out = []
    for run in res.scalars().all():
        attempts = await db.execute(
            select(WorkspaceAttempt).where(WorkspaceAttempt.run_id == run.id)
            .order_by(WorkspaceAttempt.lease_epoch.desc()))
        out.append({"id": run.id, "status": run.status, "binding_version": run.binding_version,
                    "lease_epoch": run.lease_epoch, "current_attempt_id": run.current_attempt_id,
                    "attempts": [{"id": a.id, "epoch": a.lease_epoch, "status": a.status,
                                  "started_at": a.started_at.isoformat() if a.started_at else None}
                                 for a in attempts.scalars().all()]})
    return out


@router.post("/api/workspace/tasks/{task_id}/archive")
async def archive_task(task_id: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """归档不物理删除文件；系统记忆也不会因归档被删除（§12）。"""
    from app.services import workspace_task_service as svc

    try:
        task = await svc.set_task_status(db, user_id=user.id, task_id=task_id, status="archived")
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return {"id": task.id, "status": task.status, "files_moved": False}


# --- 绑定 ---------------------------------------------------------------------

@router.get("/api/conversations/{conversation_id}/workspace-binding")
async def get_conversation_binding(conversation_id: str, db: AsyncSession = Depends(get_db),
                                   user: User = Depends(get_current_user)):
    from app.services import task_binding_service as tbs
    from app.services.workspace_scoping_runtime import scope_snapshot

    from app.db.database import Conversation

    conv = await db.get(Conversation, conversation_id)
    if conv is None or str(conv.user_id) != str(user.id):
        raise HTTPException(status_code=404, detail={"error": "conversation not found"})
    snapshot = await scope_snapshot(db, current_user=user, conversation=conv)
    history = await tbs.binding_history(db, user_id=user.id, conversation_id=conversation_id)
    snapshot["history"] = [{"from_task_id": h.from_task_id, "to_task_id": h.to_task_id,
                            "from_version": h.from_version, "to_version": h.to_version,
                            "actor": h.actor, "reason": h.reason,
                            "created_at": h.created_at.isoformat() if h.created_at else None}
                           for h in history]
    return snapshot


class NewTaskBind(BaseModel):
    title: str = Field(default="", max_length=300)


@router.post("/api/conversations/{conversation_id}/workspace-binding/new-task")
async def bind_new_task_conversation(conversation_id: str, body: NewTaskBind,
                                     db: AsyncSession = Depends(get_db),
                                     user: User = Depends(get_current_user)):
    """歧义选择器「开新任务」：建收件箱空 Task 并绑定（已有绑定则原样返回）。"""
    from app.db.database import Conversation
    from app.services import task_binding_service as tbs

    conv = await db.get(Conversation, conversation_id)
    if conv is None or str(conv.user_id) != str(user.id):
        raise HTTPException(status_code=404, detail={"error": "conversation not found"})
    root = await _user_root(db, user)
    try:
        binding = await tbs.bind_new_task(db, user_id=user.id, user_root=root,
                                          conversation_id=conversation_id, title=body.title)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return {"conversation_id": conversation_id, "task_id": binding.task_id,
            "binding_version": binding.binding_version,
            "system_memory_ingest": binding.system_memory_ingest}


@router.post("/api/conversations/{conversation_id}/workspace-binding")
async def rebind_conversation(conversation_id: str, body: BindingUpdate,
                              db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """显式改绑（CAS）。不移动旧文件；历史与逐消息来源保留。"""
    from app.db.database import Conversation
    from app.services import task_binding_service as tbs

    conv = await db.get(Conversation, conversation_id)
    if conv is None or str(conv.user_id) != str(user.id):
        raise HTTPException(status_code=404, detail={"error": "conversation not found"})

    try:
        binding = await tbs.rebind(db, user_id=user.id, conversation_id=conversation_id,
                                   task_id=body.task_id,
                                   expected_binding_version=body.expected_binding_version,
                                   actor="user", reason=body.reason)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return {"conversation_id": conversation_id, "task_id": binding.task_id,
            "binding_version": binding.binding_version,
            "system_memory_ingest": binding.system_memory_ingest}


# --- fork / merge -------------------------------------------------------------

@router.post("/api/workspace/tasks/{task_id}/fork")
async def fork_task(task_id: str, body: ForkRequest, db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """选择性复制 inputs/记忆引用，不默认复制整个旧 Task。"""
    from app.services import workspace_artifact_service as arts
    from app.services import workspace_task_service as svc

    source = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if source is None:
        raise HTTPException(status_code=404, detail={"error": "source task not found"})
    root = await _user_root(db, user)
    try:
        target = await svc.create_task(db, user_id=user.id, user_root=root,
                                       project_id=source.project_id, title=body.title,
                                       goal=f"fork of {source.title}")
        await db.flush()
        src_root = root / Path(*source.root_rel.split("/"))
        dst_root = root / Path(*target.root_rel.split("/"))
        copied, refused = [], []
        for rel in body.input_paths:
            outcome = arts.copy_into_task(root, src_root=src_root, dst_root=dst_root,
                                          rel_path=str(rel), source_task_id=source.id,
                                          user_id=user.id)
            (copied if outcome.get("ok") else refused).append(outcome)
        memory_copied = []
        if body.copy_memory:
            from app.services import workspace_memory_service as wmem

            for topic in wmem.list_topics(src_root / "memory"):
                if topic.status != wmem.STATUS_VERIFIED:
                    continue
                wmem.write_candidate(task_id=target.id, run_id="", attempt_id="",
                                     topic=topic.name, content=topic.body,
                                     evidence=topic.evidence,
                                     attempt_memory_dir=dst_root / "memory",
                                     origin=topic.origin, taint=topic.taint)
                memory_copied.append(topic.name)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return {"task": _task_dict(target), "copied": copied, "refused": refused,
            "memory_copied": memory_copied}


@router.post("/api/workspace/tasks/{task_id}/merge")
async def merge_task(task_id: str, body: MergeRequest, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """用户驱动合并：同名文件绝不静默覆盖，返回冲突清单。"""
    from app.services import workspace_artifact_service as arts
    from app.services import workspace_task_service as svc

    target = await svc.get_task(db, user_id=user.id, task_id=task_id)
    source = await svc.get_task(db, user_id=user.id, task_id=body.source_task_id)
    if target is None or source is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    root = await _user_root(db, user)
    src_root = root / Path(*source.root_rel.split("/"))
    dst_root = root / Path(*target.root_rel.split("/"))
    merged, conflicts = [], []
    for rel in body.paths:
        outcome = arts.merge_into_task(root, src_root=src_root, dst_root=dst_root,
                                       rel_path=str(rel), source_task_id=source.id,
                                       user_id=user.id)
        (merged if outcome.get("ok") else conflicts).append(outcome)
    return {"merged": merged, "conflicts": conflicts,
            "note": "冲突项需用户显式改名/替换后重发；同名文件不会被静默覆盖。"}


# --- 跨域 grant ---------------------------------------------------------------

@router.post("/api/workspace/cross-scope-grants")
async def create_grant(body: GrantCreate, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """只有登录用户能签发（agent 无此入口）。"""
    from app.services import cross_scope_grant_service as gs
    from app.services import workspace_run_service as yrs
    from app.services import workspace_task_service as svc

    # owner 校验：current/source 任务都必须属于当前用户
    for tid in filter(None, (body.current_task_id, body.source_task_id)):
        if await svc.get_task(db, user_id=user.id, task_id=str(tid)) is None:
            raise HTTPException(status_code=404, detail={"error": "task not found"})
    run_id, attempt_id, epoch, version = (body.run_id, body.attempt_id, body.lease_epoch,
                                          body.binding_version)
    if run_id is None:
        active = await yrs.active_run_for(db, task_id=body.current_task_id)
        if active is not None:
            run_id, attempt_id = active.id, active.current_attempt_id
            epoch, version = active.lease_epoch, active.binding_version

    try:
        grant = await gs.mint_grant(
            db, user_id=user.id, current_task_id=body.current_task_id, kind=body.kind,
            source_project_id=body.source_project_id, source_task_id=body.source_task_id,
            allowed_paths=body.allowed_paths, server_tool=body.server_tool,
            allowed_actions=body.allowed_actions, ttl_seconds=body.ttl_seconds, issuer="user",
            run_id=run_id, attempt_id=attempt_id, lease_epoch=epoch, binding_version=version)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return {"id": grant.id, "kind": grant.kind, "mode": grant.mode,
            "not_after": grant.not_after.isoformat() if grant.not_after else None,
            "claims": await gs.claims_of(grant)}


@router.get("/api/workspace/cross-scope-grants")
async def list_grants(task_id: Optional[str] = None, db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    from app.services import cross_scope_grant_service as gs

    rows = await gs.list_grants(db, user_id=user.id, task_id=task_id, include_inactive=True)
    return {"grants": [{"id": g.id, "kind": g.kind, "status": g.status,
                        "source_task_id": g.source_task_id, "current_task_id": g.current_task_id,
                        "not_after": g.not_after.isoformat() if g.not_after else None,
                        "audit_count": g.audit_count} for g in rows]}


@router.delete("/api/workspace/cross-scope-grants/{grant_id}")
async def revoke_grant(grant_id: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    from app.services import cross_scope_grant_service as gs

    try:
        grant = await gs.revoke_grant(db, user_id=user.id, grant_id=grant_id)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return {"id": grant.id, "status": grant.status}
