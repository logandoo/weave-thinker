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
from typing import Any, List, Optional, Sequence

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
                                 ws.CODE_ATTEMPT_STALE, ws.CODE_MODE_UNSUPPORTED,
                                 ws.CODE_PROJECT_STALE, ws.CODE_TASK_STALE,
                                 ws.CODE_PROJECT_RESERVED) else 403
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


async def _refresh_projections(db: AsyncSession, *, user_id: str,
                               project_id: Optional[str] = None,
                               task_ids: Sequence[str] = ()) -> None:
    """commit 之后刷新只读投影（best-effort；投影不授予权限）。

    `_refresh_*_projection` 用独立短会话读**已提交**状态，所以必须在 commit 之后调用
    （commit 之前读不到本事务的未提交值）。
    """
    from sqlalchemy import select

    from app.db.database import WorkspaceTask
    from app.services import workspace_task_service as svc

    ids = [str(t) for t in (task_ids or ())]
    if project_id:
        project = await svc.get_project(db, user_id=user_id, project_id=str(project_id))
        if project is not None:
            await svc._refresh_project_projection(project)
        res = await db.execute(
            select(WorkspaceTask.id).where(WorkspaceTask.user_id == user_id,
                                           WorkspaceTask.project_id == str(project_id)))
        ids.extend(r[0] for r in res.all())
    for tid in ids:
        task = await svc.get_task(db, user_id=user_id, task_id=tid)
        if task is not None:
            await svc._refresh_task_projection(task)


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    storage_authority: str = "cloud"
    system_memory_ingest: Optional[str] = None
    description: Optional[str] = None


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    goal: str = ""
    storage_authority: Optional[str] = None
    system_memory_ingest: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    description: Optional[str] = None


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
        "description": task.description, "origin": task.origin,
        "row_version": task.row_version,
        "deleted_at": task.deleted_at.isoformat() if task.deleted_at else None,
        "display_root": f"{task.root_rel}/workspace",
        "last_active_at": task.last_active_at.isoformat() if task.last_active_at else None,
        "artifact_manifest": _manifest(task),
    }


def _project_dict(project) -> dict[str, Any]:
    from app.core.config import get_config as _get_cfg

    reserved = _get_cfg().workspace_inbox_project_name
    is_default = str(project.kind or "user") == "inbox"
    return {
        "id": project.id, "name": project.name, "root_rel": project.root_rel,
        "status": project.status, "storage_authority": project.storage_authority,
        "system_memory_ingest": project.system_memory_ingest,
        "description": project.description, "row_version": project.row_version,
        "kind": project.kind, "is_default": is_default,
        "reserved_name": reserved,
        "deleted_at": project.deleted_at.isoformat() if project.deleted_at else None,
        "last_active_at": project.last_active_at.isoformat() if project.last_active_at else None,
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
    return {"projects": [_project_dict(p) for p in projects]}


@router.get("/api/workspace/projects/overview")
async def projects_overview(db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    """工作台「项目列表」：projects→tasks→bound conversations 一次返回。

    只读聚合（不建 run、不改绑定、零系统记忆写入），不依赖 `[workspace.task_scoping]`
    mode——off 档也能展示历史项目。owner 过滤全部在查询层（binding.user_id 与
    conversation.user_id 双条件）。

    预算与截断显式化（评审 r1 I3：UI 不得拿截断数据声称「没有」）：项目 ≤200；
    任务全局取 ≤1000 行，每项目实际总数经 count 对账，超出即 `tasks_truncated=true`；
    会话行全局取 ≤5000（updated_at DESC, id 平序），每任务展示 ≤50，实际绑定数经
    count 对账，超出即 `conversations_truncated=true`。
    """
    from sqlalchemy import func, select

    from app.db.database import Conversation, WorkspaceBinding, WorkspaceTask
    from app.services import workspace_task_service as svc

    projects = await svc.list_projects(db, user_id=user.id,
                                       statuses=("active", "archived"), limit=200)
    if not projects:
        return {"projects": []}
    project_ids = [p.id for p in projects]
    tasks_res = await db.execute(
        select(WorkspaceTask)
        .where(WorkspaceTask.user_id == user.id,
               WorkspaceTask.project_id.in_(project_ids),
               WorkspaceTask.status.in_(("active", "completed", "archived")))
        .order_by(WorkspaceTask.last_active_at.desc().nullslast(),
                  WorkspaceTask.created_at.desc())
        .limit(1000)
    )
    tasks = list(tasks_res.scalars().all())

    # 每项目实际任务数（对账全局 limit，截断不静默）
    task_totals = dict((await db.execute(
        select(WorkspaceTask.project_id, func.count(WorkspaceTask.id))
        .where(WorkspaceTask.user_id == user.id,
               WorkspaceTask.project_id.in_(project_ids),
               WorkspaceTask.status.in_(("active", "completed", "archived")))
        .group_by(WorkspaceTask.project_id)
    )).all())
    fetched_per_project: dict[str, int] = {}
    for t in tasks:
        fetched_per_project[str(t.project_id)] = fetched_per_project.get(str(t.project_id), 0) + 1

    convs_by_task: dict[str, list] = {t.id: [] for t in tasks}
    task_ids = [t.id for t in tasks]
    conv_totals: dict[str, int] = {}
    if task_ids:
        rows = await db.execute(
            select(WorkspaceBinding.task_id, Conversation.id, Conversation.title,
                   Conversation.updated_at)
            .join(Conversation, Conversation.id == WorkspaceBinding.conversation_id)
            .where(WorkspaceBinding.user_id == user.id,
                   Conversation.user_id == user.id,
                   WorkspaceBinding.task_id.in_(task_ids))
            .order_by(Conversation.updated_at.desc().nullslast(), Conversation.id)
            .limit(5000)
        )
        for tid, cid, ctitle, cupd in rows.all():
            bucket = convs_by_task.get(tid)
            if bucket is None or len(bucket) >= 50:
                continue
            bucket.append({"id": cid, "title": ctitle or "新对话",
                           "updated_at": cupd.isoformat() if cupd else None})
        conv_totals = dict((await db.execute(
            select(WorkspaceBinding.task_id, func.count(WorkspaceBinding.id))
            .join(Conversation, Conversation.id == WorkspaceBinding.conversation_id)
            .where(WorkspaceBinding.user_id == user.id,
                   Conversation.user_id == user.id,
                   WorkspaceBinding.task_id.in_(task_ids))
            .group_by(WorkspaceBinding.task_id)
        )).all())

    # 每任务笔记数（§5 overview 任务行加 notes_count；2026-11-16 Wave B 走多对多关系表）
    notes_totals: dict[str, int] = {}
    if task_ids:
        from app.services import workspace_note_binding_service as nbs

        notes_totals = await nbs.task_note_counts(db, user_id=user.id, task_ids=list(task_ids))

    tasks_by_project: dict[str, list] = {}
    for t in tasks:
        total_conv = int(conv_totals.get(t.id, 0))
        tasks_by_project.setdefault(str(t.project_id), []).append({
            "id": t.id, "title": t.title, "status": t.status, "root_rel": t.root_rel,
            "display_root": f"{t.root_rel}/workspace",
            "description": t.description, "origin": t.origin, "row_version": t.row_version,
            # 2026-11-17：任务设置页展示真实继承值（此前硬编码 cloud）。
            "storage_authority": t.storage_authority,
            "system_memory_ingest": t.system_memory_ingest,
            "notes_count": int(notes_totals.get(t.id, 0)),
            "last_active_at": t.last_active_at.isoformat() if t.last_active_at else None,
            "conversations": convs_by_task.get(t.id, []),
            "conversations_truncated": total_conv > len(convs_by_task.get(t.id, [])),
        })
    return {"projects": [{
        "id": p.id, "name": p.name, "status": p.status, "root_rel": p.root_rel,
        "description": p.description, "row_version": p.row_version, "kind": p.kind,
        # 2026-11-17：设置面板要展示/回写真实值——此前前端硬编码 user_facts/cloud，
        # 任何一次「改名保存」都会把既有 exclude/normal 策略悄悄改回 user_facts。
        "storage_authority": p.storage_authority,
        "system_memory_ingest": p.system_memory_ingest,
        "last_active_at": p.last_active_at.isoformat() if p.last_active_at else None,
        "tasks": tasks_by_project.get(str(p.id), []),
        "tasks_count": int(task_totals.get(p.id, 0)),
        "tasks_truncated": int(task_totals.get(p.id, 0)) > fetched_per_project.get(str(p.id), 0),
        # 默认项目以 kind='inbox' 稳定标识（A1 修复：不再按名字键控）
        "is_default": str(p.kind or "user") == "inbox",
    } for p in projects]}


@router.post("/api/workspace/projects")
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    from app.services import workspace_task_service as svc

    root = await _user_root(db, user)
    try:
        project = await svc.create_project(
            db, user_id=user.id, user_root=root, name=body.name,
            storage_authority=body.storage_authority,
            system_memory_ingest=body.system_memory_ingest,
            description=body.description)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return _project_dict(project)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    system_memory_ingest: Optional[str] = None
    expected_row_version: Optional[int] = None


@router.patch("/api/workspace/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """项目编辑（2026-11-15 任务管理中心）：名称/备注/记忆策略 + CAS。

    默认项目（kind='inbox'）允许改名/写备注（身份按 kind 不按名字）。
    `storage_authority` 不在可编辑面（单向继承）。
    """
    from app.services import workspace_task_service as svc

    try:
        project = await svc.update_project(
            db, user_id=user.id, project_id=project_id, name=body.name,
            description=body.description, system_memory_ingest=body.system_memory_ingest,
            expected_row_version=body.expected_row_version)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return _project_dict(project)


class DeleteRequest(BaseModel):
    mode: str = Field(default="archive")  # archive | purge
    confirm_name: Optional[str] = None
    confirm_title: Optional[str] = None
    expected_row_version: Optional[int] = None
    conversations: str = Field(default="unbind")  # unbind | delete


@router.delete("/api/workspace/projects/{project_id}")
async def delete_project(project_id: str, body: Optional[DeleteRequest] = None,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """项目删除（两段式）：archive（可恢复）/ purge（需输入项目名 + 级联计数）。"""
    from app.services import workspace_task_lifecycle_service as life

    body = body or DeleteRequest()
    root = await _user_root(db, user)
    try:
        if body.mode == "archive":
            project = await life.archive_project(
                db, user_id=user.id, project_id=project_id,
                expected_row_version=body.expected_row_version)
            await db.commit()
            await _refresh_projections(db, user_id=user.id, project_id=project_id)
            return {"id": project.id, "status": project.status, "mode": "archive",
                    "files_moved": False}
        if body.mode == "purge":
            outcome = await life.purge_project(
                db, user_id=user.id, project_id=project_id,
                confirm_name=str(body.confirm_name or ""),
                expected_row_version=body.expected_row_version,
                conversations=body.conversations)
            plan = outcome.pop("file_plan", [])
            await db.commit()
            moved = life.apply_file_plan(root, plan)
            # 项目目录固定落在 _trash/project_<pid>（评审 #7：不能取 moved[0]）
            project_trash = (f"_trash/project_{project_id}"
                             if (root / "_trash" / f"project_{project_id}").is_dir() else None)
            outcome["trash_rel"] = project_trash
            outcome["files_moved_to_trash"] = bool(moved)
            return outcome
        raise HTTPException(status_code=422, detail={
            "error": "mode must be archive|purge", "code": ws.CODE_RELATION_INVALID})
    except ws.WorkspaceScopeError as exc:
        _raise(exc)


@router.post("/api/workspace/projects/{project_id}/restore")
async def restore_project(project_id: str, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    from app.services import workspace_task_lifecycle_service as life

    root = await _user_root(db, user)
    try:
        project, plan = await life.restore_project(db, user_id=user.id, project_id=project_id)
        await db.commit()
        life.apply_file_plan(root, plan)
        await _refresh_projections(db, user_id=user.id, project_id=project_id)
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return _project_dict(project)


@router.get("/api/workspace/projects/{project_id}/delete-preview")
async def preview_project_delete(project_id: str, db: AsyncSession = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    """删除前级联计数预览（评审 I7）：UI 在输入 confirm 前展示。"""
    from app.services import workspace_task_lifecycle_service as life

    try:
        return await life.preview_project_delete(db, user_id=user.id, project_id=project_id)
    except ws.WorkspaceScopeError as exc:
        _raise(exc)


@router.get("/api/workspace/trash")
async def list_trash(db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """回收站（评审 I6）：purge 后刷新仍能拿到 id 去 restore。"""
    from app.services import workspace_task_lifecycle_service as life

    return await life.list_trash(db, user_id=user.id)


# 兼容别名：2026-11-14 的 `rename_project` 调用点（含测试）直接引用该函数名。
async def rename_project(project_id: str, body: Optional[ProjectUpdate] = None,
                         db: AsyncSession = Depends(get_db),
                         user: User = Depends(get_current_user), **kwargs):
    """兼容包装：旧签名 `rename_project(pid, name=...)` 与 `(pid, ProjectUpdate(...))` 都可用。"""
    if body is None:
        body = ProjectUpdate(name=kwargs.get("name"))
    return await update_project(project_id, body, db=db, user=user)


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
    # §4.11：任务存储权威从项目继承；传不一致值一律 422（表单不暴露该字段）
    if body.storage_authority is not None:
        project = await svc.get_project(db, user_id=user.id, project_id=project_id)
        if project is not None and str(body.storage_authority) != str(project.storage_authority or "cloud"):
            raise HTTPException(status_code=422, detail={
                "error": "task storage_authority must equal project storage_authority",
                "code": ws.CODE_RELATION_INVALID})
    try:
        task = await svc.create_task(
            db, user_id=user.id, user_root=root, project_id=project_id, title=body.title,
            goal=body.goal, storage_authority=body.storage_authority,
            system_memory_ingest=body.system_memory_ingest, tags=body.tags,
            description=body.description, origin="user")
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return _task_dict(task)


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    description: Optional[str] = None
    status: Optional[str] = None
    expected_row_version: Optional[int] = None


class MoveTasksRequest(BaseModel):
    task_ids: List[str] = Field(default_factory=list)
    target_project_id: str = Field(..., min_length=1)


@router.post("/api/workspace/tasks/move")
async def move_tasks(body: MoveTasksRequest, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """跨项目移动 1 个或多个任务（2026-11-18 issue1）。改挂 + 绑定 + 目录物理搬移（commit 后）。"""
    from app.services import workspace_task_lifecycle_service as life
    from app.services import workspace_task_service as svc

    ids = [str(t) for t in (body.task_ids or []) if str(t).strip()]
    if not ids:
        raise HTTPException(status_code=422, detail={
            "error": "task_ids is required", "code": ws.CODE_RELATION_INVALID})
    root = await _user_root(db, user)
    try:
        out = await svc.move_tasks(db, user_id=user.id, user_root=root,
                                   task_ids=ids, target_project_id=body.target_project_id)
        plan = out.get("file_moves", [])
        await db.commit()
        try:
            moved_dirs = life.apply_move_plan(root, plan)
        except Exception:  # noqa: BLE001 - 目录搬移异常不得 500（DB 已权威改挂）
            logger.warning("apply_move_plan failed after commit", exc_info=True)
            moved_dirs = []
        await _refresh_projections(db, user_id=user.id, task_ids=out.get("task_ids", []))
    except ws.WorkspaceScopeError as exc:
        await db.rollback()
        _raise(exc)
    return {"moved": out["moved"], "task_ids": out["task_ids"],
            "dirs_moved": len(moved_dirs)}


@router.patch("/api/workspace/tasks/{task_id}")
async def update_task(task_id: str, body: TaskUpdate,
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """任务编辑（2026-11-15）：标题/备注/状态 + CAS。"""
    from app.services import workspace_task_service as svc

    try:
        task = await svc.update_task(
            db, user_id=user.id, task_id=task_id, title=body.title,
            description=body.description, status=body.status,
            expected_row_version=body.expected_row_version)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return _task_dict(task)


@router.delete("/api/workspace/tasks/{task_id}")
async def delete_task(task_id: str, body: Optional[DeleteRequest] = None,
                      db: AsyncSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """任务删除（两段式）：archive（可恢复）/ purge（需输入任务名 + 会话处理选择）。"""
    from app.services import workspace_task_lifecycle_service as life

    body = body or DeleteRequest()
    root = await _user_root(db, user)
    try:
        if body.mode == "archive":
            task = await life.archive_task(db, user_id=user.id, task_id=task_id,
                                           expected_row_version=body.expected_row_version)
            await db.commit()
            await _refresh_projections(db, user_id=user.id, task_ids=[task_id])
            return {"id": task.id, "status": task.status, "mode": "archive",
                    "files_moved": False}
        if body.mode == "purge":
            outcome = await life.purge_task(
                db, user_id=user.id, task_id=task_id,
                confirm_title=str(body.confirm_title or ""),
                expected_row_version=body.expected_row_version,
                conversations=body.conversations)
            plan = outcome.pop("file_plan", [])
            await db.commit()
            moved = life.apply_file_plan(root, plan)
            outcome["trash_rel"] = moved[0] if moved else None
            outcome["files_moved_to_trash"] = bool(moved)
            return outcome
        raise HTTPException(status_code=422, detail={
            "error": "mode must be archive|purge", "code": ws.CODE_RELATION_INVALID})
    except ws.WorkspaceScopeError as exc:
        _raise(exc)


@router.post("/api/workspace/tasks/{task_id}/restore")
async def restore_task(task_id: str, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    from app.services import workspace_task_lifecycle_service as life

    root = await _user_root(db, user)
    try:
        task, plan = await life.restore_task(db, user_id=user.id, task_id=task_id)
        await db.commit()
        life.apply_file_plan(root, plan)
        await _refresh_projections(db, user_id=user.id, task_ids=[task_id])
    except ws.WorkspaceScopeError as exc:
        _raise(exc)
    return _task_dict(task)


@router.get("/api/workspace/tasks/{task_id}/delete-preview")
async def preview_task_delete(task_id: str, db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    from app.services import workspace_task_lifecycle_service as life

    try:
        return await life.preview_task_delete(db, user_id=user.id, task_id=task_id)
    except ws.WorkspaceScopeError as exc:
        _raise(exc)


@router.get("/api/workspace/tasks/{task_id}/conversations")
async def list_task_conversations(task_id: str, db: AsyncSession = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    """任务内会话列表（含未读/最近活跃）。"""
    from sqlalchemy import select

    from app.db.database import Conversation, WorkspaceBinding
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    rows = await db.execute(
        select(Conversation.id, Conversation.title, Conversation.updated_at)
        .join(WorkspaceBinding, WorkspaceBinding.conversation_id == Conversation.id)
        .where(WorkspaceBinding.user_id == user.id, WorkspaceBinding.task_id == task_id,
               Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc().nullslast(), Conversation.id)
        .limit(200)
    )
    return {"task_id": task_id, "conversations": [
        {"id": cid, "title": title or "新对话",
         "updated_at": upd.isoformat() if upd else None}
        for cid, title, upd in rows.all()]}


class NewConversationInTask(BaseModel):
    title: str = Field(default="", max_length=300)


@router.post("/api/workspace/tasks/{task_id}/conversations")
async def create_conversation_in_task(task_id: str, body: NewConversationInTask,
                                      db: AsyncSession = Depends(get_db),
                                      user: User = Depends(get_current_user)):
    """在任务内新建会话（直接写绑定，expected=0）。"""
    from app.db.database import Conversation
    from app.services import task_binding_service as tbs
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    if str(task.status) == "deleted":
        raise HTTPException(status_code=422, detail={
            "error": "cannot create conversation in a deleted task",
            "code": ws.CODE_RELATION_INVALID})
    project = await svc.get_project(db, user_id=user.id, project_id=task.project_id)
    if project is None or str(project.status) != "active":
        raise HTTPException(status_code=422, detail={
            "error": "project is not active", "code": ws.CODE_RELATION_INVALID})
    conv = Conversation(user_id=user.id, title=(body.title or "新对话")[:255])
    db.add(conv)
    await db.flush()
    try:
        binding = await tbs.rebind(db, user_id=user.id, conversation_id=conv.id,
                                   task_id=task_id, expected_binding_version=0,
                                   actor="user", reason="created_in_task")
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        await db.rollback()
        _raise(exc)
    return {"conversation_id": conv.id, "task_id": binding.task_id,
            "binding_version": binding.binding_version}


@router.get("/api/workspace/tasks/{task_id}/notes")
async def list_task_notes(task_id: str, db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    from app.services import workspace_note_binding_service as nbs
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    # 2026-11-16 Wave B：一条笔记可同时绑多个任务 → 行里带 bound_task_ids / bound_task_count
    rows = await nbs.list_task_note_rows(db, user_id=user.id, task_id=task_id)
    return {"task_id": task_id, "notes": rows}


class NoteBatchBindRequest(BaseModel):
    note_ids: List[str] = Field(default_factory=list)


@router.post("/api/workspace/tasks/{task_id}/notes/bind")
async def bind_notes_to_task(task_id: str, body: NoteBatchBindRequest,
                             db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """批量把笔记绑到任务（多选绑定 · 幂等：已绑的记为 already_bound，不产生重复行）。"""
    from app.services import workspace_note_binding_service as nbs
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    if str(task.status) == "deleted":
        raise HTTPException(status_code=422, detail={
            "error": "cannot bind note to a deleted task", "code": ws.CODE_RELATION_INVALID})
    try:
        requested = list(body.note_ids or [])
        picked = requested[:200]
        out = await nbs.batch_bind(db, user_id=user.id, task_id=task_id, note_ids=picked)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        await db.rollback()
        _raise(exc)
    out["bound_task_count"] = len(out["bound"]) + len(out["already_bound"])
    out["truncated"] = max(0, len(requested) - len(picked))   # 评审 m1：静默截断要可见
    return out


@router.post("/api/workspace/tasks/{task_id}/notes/unbind")
async def unbind_notes_from_task(task_id: str, body: NoteBatchBindRequest,
                                 db: AsyncSession = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    """从任务上批量解绑笔记（只解关系，笔记不删；该笔记在其他任务上的绑定保留）。"""
    from app.services import workspace_note_binding_service as nbs
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    requested = list(body.note_ids or [])
    picked = requested[:200]
    out = await nbs.batch_unbind(db, user_id=user.id, task_id=task_id, note_ids=picked)
    await db.commit()
    out["truncated"] = max(0, len(requested) - len(picked))
    return out


@router.get("/api/workspace/notes/bindings")
async def list_note_bindings(note_ids: str = "", db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """按笔记反查绑定（选择器徽标：这条笔记已经绑了哪些任务/项目）。

    `note_ids` 逗号分隔；上限 200 条，超出截断（选择器分页用不到这么多）。
    """
    from app.services import workspace_note_binding_service as nbs

    ids = [s.strip() for s in (note_ids or "").split(",") if s.strip()][:200]
    counts = await nbs.binding_counts_for_notes(db, user_id=user.id, note_ids=ids)
    return {"bindings": [{"note_id": nid, "task_count": int(counts.get(nid, 0))} for nid in ids]}


@router.get("/api/workspace/notes/{note_id}/bindings")
async def note_binding_detail(note_id: str, db: AsyncSession = Depends(get_db),
                              user: User = Depends(get_current_user)):
    """单条笔记的绑定明细（任务标题 + 所属项目，供卡片徽标 tooltip）。"""
    from app.services import workspace_note_binding_service as nbs

    if await nbs.get_note(db, user_id=user.id, note_id=note_id) is None:
        raise HTTPException(status_code=404, detail={"error": "note not found"})
    return {"note_id": note_id, "tasks": await nbs.tasks_for_note(db, user_id=user.id,
                                                                  note_id=note_id)}


@router.get("/api/workspace/tasks/{task_id}/files")
async def list_task_files(task_id: str, conversation_id: Optional[str] = None,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """任务文件（只读任务根 `workspace/{inputs,artifacts}`）。

    `conversation_id` → 返回「本会话产出」扁平清单（manifest ∪ run→conversation
    关联，best-effort；不是另一份存储）。无 `conversation_id` → 目录列表。
    """
    from app.services import workspace_files_service as wfs
    from app.services import workspace_task_service as svc

    task = await svc.get_task(db, user_id=user.id, task_id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={"error": "task not found"})
    if str(task.status) == "deleted":
        raise HTTPException(status_code=410, detail={
            "error": "task is deleted (in trash)", "code": ws.CODE_RELATION_INVALID})
    root = await _user_root(db, user)
    task_root = None
    try:
        task_root = ws._resolve_no_redirect(root, task.root_rel, what="task root")
    except ws.WorkspaceScopeError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "code": exc.code})
    if conversation_id:
        # 方案A Piece 1：会话「我产出的文件」按 conversation 的全部 runs 跨任务扫，
        # 改绑到别的任务后旧任务产出仍可见（读/视图侧，不搬文件、不改 owner）。
        produced = await wfs.async_conversation_produced_files_across_tasks(
            db, user_id=user.id, user_root=root, conversation_id=conversation_id)
        return {"task_id": task_id, "display_root": f"{task.root_rel}/workspace",
                "conversation_id": conversation_id, "produced": produced}
    try:
        listing = wfs.list_directory(task_root, "workspace")
    except wfs.WorkspacePathError as exc:
        raise HTTPException(status_code=404, detail={
            "error": str(exc), "code": getattr(exc, "code", "workspace_root_invalid")})
    return {"task_id": task_id, "display_root": f"{task.root_rel}/workspace",
            "conversation_id": None, "listing": listing}


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
async def archive_task(task_id: str, expected_row_version: Optional[int] = None,
                       db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    """归档不物理删除文件；系统记忆也不会因归档被删除（§12）。

    兼容旧入口：与 `DELETE mode=archive` 走同一 lifecycle 服务（评审 M11：CAS 不再被绕过）。
    """
    from app.services import workspace_task_lifecycle_service as life

    try:
        task = await life.archive_task(db, user_id=user.id, task_id=task_id,
                                       expected_row_version=expected_row_version)
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


def _guard_unsupported_chain(conv) -> None:
    """2026-11-14 v2（评审 r2 Important）：设备链未放开时，HTTP 改绑/开新任务也不得
    为它制造绑定身份（2026-11-13 死会话同类）。与自动铸造面/agent 工具共用同一判定。
    显式改绑的「用户负责」只适用于已支持链路内部的任务选择，不适用于未放开的执行链。"""
    from app.services.workspace_scoping_runtime import mints_identity_for_unsupported_chain

    if mints_identity_for_unsupported_chain(getattr(conv, "execution_target", None)):
        raise HTTPException(status_code=409, detail={
            "error": "该会话在本地设备上运行，暂不支持任务作用域改绑（设备链未放开）",
            "code": ws.CODE_MODE_UNSUPPORTED,
        })


@router.post("/api/conversations/{conversation_id}/workspace-binding/new-task")
async def bind_new_task_conversation(conversation_id: str, body: NewTaskBind,
                                     db: AsyncSession = Depends(get_db),
                                     user: User = Depends(get_current_user)):
    """歧义选择器「开新任务」：建默认项目空 Task 并绑定（已有绑定则原样返回）。"""
    from app.db.database import Conversation
    from app.services import task_binding_service as tbs

    conv = await db.get(Conversation, conversation_id)
    if conv is None or str(conv.user_id) != str(user.id):
        raise HTTPException(status_code=404, detail={"error": "conversation not found"})
    root = await _user_root(db, user)
    try:
        _guard_unsupported_chain(conv)
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
    """显式改绑（CAS）。改绑本身不移动文件；但**改绑后源任务变空**（无其他会话共享）时，
    把源任务的 workspace 文件迁到目标任务（2026-11-18 issue4）。共享任务域不动。"""
    from app.db.database import Conversation
    from app.services import task_binding_service as tbs
    from app.services import workspace_task_lifecycle_service as life

    conv = await db.get(Conversation, conversation_id)
    if conv is None or str(conv.user_id) != str(user.id):
        raise HTTPException(status_code=404, detail={"error": "conversation not found"})

    try:
        _guard_unsupported_chain(conv)
        # 改绑前的源任务（用于改绑后判断是否变空 → 迁移孤儿文件）。for_update 锁住绑定行，
        # 与下面的 rebind 同一行锁，杜绝 TOCTOU 把无关已抽干任务的源文件误迁（评审 I3）。
        prior = await tbs.get_binding(db, user_id=user.id, conversation_id=conversation_id,
                                      for_update=True)
        from_task_id = str(prior.task_id) if prior is not None else None
        root = await _user_root(db, user)
        binding = await tbs.rebind(db, user_id=user.id, conversation_id=conversation_id,
                                   task_id=body.task_id,
                                   expected_binding_version=body.expected_binding_version,
                                   actor="user", reason=body.reason)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        await db.rollback()   # 行锁 CAS 失败/停泊异常：立即释放 binding 行锁，不等 get_db 关会话
        _raise(exc)
    # commit 之后迁移（best-effort）：仅当源任务改绑后剩余会话 == 0
    migrated = 0
    try:
        mig = await life.migrate_orphaned_task_files(
            db, user_id=user.id, user_root=root, from_task_id=from_task_id,
            to_task_id=body.task_id)
        migrated = int(mig.get("migrated", 0) or 0)
    except Exception:  # noqa: BLE001 - 文件迁移是改绑成功后的尽力而为副作用
        logger.warning("rebind orphan-file migration failed (conv=%s)", conversation_id,
                       exc_info=True)
    return {"conversation_id": conversation_id, "task_id": binding.task_id,
            "binding_version": binding.binding_version,
            "parked_runs": getattr(binding, "_parked_active_runs", 0),
            "migrated_files": migrated,
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
