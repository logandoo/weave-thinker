# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Project / Task 身份与目录服务（2026-11-10 波 · T3）。

设计契约 `docs/DESIGN_20261108_task_workspace_and_memory.md` §4.2 / §5：

* **DB 是身份与权限的唯一权威**；`project.json` / `task.json` 只是只读投影
  （灾难恢复与人类可读），永不授予权限；
* 用户根保持不变，其下 `projects/p_<pid>/tasks/t_<tid>/` 是默认文件与记忆边界；
* 新 Task 目录：`workspace/{inputs,artifacts}` + `staging/` + `memory/MEMORY.md` + `runs/`；
* 每次查询直接带 owner 校验；Task 的 `storage_authority` 必须与 Project 一致（§6.5）；
* 不搬迁、不复制 legacy 根内容。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import select

from app.core.config import get_config
from app.db.database import WorkspaceProject, WorkspaceTask
from app.services import workspace_scope as ws

logger = logging.getLogger(__name__)

TASK_STATUSES = ("active", "completed", "archived")
PROJECT_STATUSES = ("active", "archived")

MEMORY_INDEX_HEADER = "# 记忆索引\n\n> 有界索引（≤200 行 / 25KB）。topic 文件按需读取，不整体预注入。\n"

_PROJECTION_AUTHORITY = "database"


# --- 目录与投影 ---------------------------------------------------------------

def _mkdir(root: Path, *rel: str) -> Path:
    path = root.joinpath(*rel)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_projection(path: Path, payload: dict[str, Any]) -> None:
    data = dict(payload)
    data["authority"] = _PROJECTION_AUTHORITY
    data["writable_by_client"] = False
    data.setdefault("generated_at", datetime.utcnow().isoformat(timespec="seconds") + "Z")
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    except OSError as exc:  # 投影失败不影响 DB 权威（设计 §16 风险 6）
        logger.warning("workspace projection write failed (%s): %s", path, exc)


def ensure_project_directories(project_root: Path) -> None:
    _mkdir(project_root, "shared")
    mem = _mkdir(project_root, "memory")
    index = mem / "MEMORY.md"
    if not index.exists():
        index.write_text(MEMORY_INDEX_HEADER, encoding="utf-8")


def ensure_task_directories(task_root: Path) -> None:
    _mkdir(task_root, "workspace", "inputs")
    _mkdir(task_root, "workspace", "artifacts")
    _mkdir(task_root, "staging")
    mem = _mkdir(task_root, "memory")
    index = mem / "MEMORY.md"
    if not index.exists():
        index.write_text(MEMORY_INDEX_HEADER, encoding="utf-8")
    _mkdir(task_root, "runs")


def read_projection(directory: Path | str) -> dict[str, Any]:
    """只读回读投影（灾难恢复/诊断用）；**不能**据此建立作用域或授权。"""
    base = Path(directory)
    for name in ("task.json", "project.json"):
        candidate = base / name
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"authority": "unreadable", "path": str(candidate)}
            if isinstance(data, dict):
                data.setdefault("authority", _PROJECTION_AUTHORITY)
                return data
    return {"authority": "none", "path": str(base)}


# --- 路径展示 -----------------------------------------------------------------

def user_rel_path(user_root: Path | str, target: Path | str) -> str:
    """用户根相对 POSIX 路径（对外统一形式，§3 不变量 9）。"""
    return ws.os_relpath(target, user_root)


def task_display_path(user_root: Path | str, target: Path | str, task_root: Path | str) -> str:
    """Task 内展示路径；不在 Task 内则退回用户根相对路径。"""
    try:
        return ws.os_relpath(target, task_root)
    except ws.WorkspaceScopeError:
        return user_rel_path(user_root, target)


def classify_path(user_root: Path | str, target: Path | str, *,
                  task_root: Path | str | None = None,
                  project_root: Path | str | None = None) -> tuple[str, str]:
    """返回 (scope, display_path)：current_task / project / legacy。"""
    if task_root is not None and ws.is_inside(Path(task_root), Path(target)):
        return "current_task", ws.os_relpath(target, task_root)
    if project_root is not None and ws.is_inside(Path(project_root), Path(target)):
        return "project", ws.os_relpath(target, project_root)
    return "legacy", user_rel_path(user_root, target)


# --- Project CRUD -------------------------------------------------------------

async def get_project(db, *, user_id: str, project_id: str) -> WorkspaceProject | None:
    res = await db.execute(
        select(WorkspaceProject).where(WorkspaceProject.id == project_id,
                                       WorkspaceProject.user_id == user_id)
    )
    return res.scalar_one_or_none()


async def list_projects(db, *, user_id: str, statuses: Sequence[str] = ("active",),
                        limit: int = 50) -> list[WorkspaceProject]:
    res = await db.execute(
        select(WorkspaceProject)
        .where(WorkspaceProject.user_id == user_id, WorkspaceProject.status.in_(tuple(statuses)))
        .order_by(WorkspaceProject.last_active_at.desc().nullslast(), WorkspaceProject.created_at.desc())
        .limit(max(1, int(limit)))
    )
    return list(res.scalars().all())


async def create_project(db, *, user_id: str, user_root: Path | str, name: str,
                         storage_authority: str = "cloud",
                         system_memory_ingest: str | None = None) -> WorkspaceProject:
    """创建项目：先落 DB（身份权威），再建目录与只读投影。"""
    base = Path(user_root).resolve()
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ws.WorkspaceScopeError(ws.CODE_ROOT_INVALID, "project name is required", "")
    if str(storage_authority or "cloud") != "cloud" and not str(storage_authority).startswith("device:"):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "storage_authority must be cloud or device:<id>",
                                     str(storage_authority))
    ingest = system_memory_ingest or get_config().workspace_task_default_ingest
    if ingest not in ("normal", "user_facts", "exclude"):
        ingest = "user_facts"

    project = WorkspaceProject(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=clean_name[:200],
        root_rel="",  # 先占位，拿到 id 后写 canonical（root_rel 含自身 id）
        status="active",
        storage_authority=str(storage_authority or "cloud"),
        system_memory_ingest=ingest,
        last_active_at=datetime.utcnow(),
    )
    project.root_rel = ws.project_rel(project.id)
    ws.normalize_rel(project.root_rel, what="project root")
    db.add(project)
    await db.flush()

    root = _resolve_project_root(base, project)
    ensure_project_directories(root)
    _write_projection(root / "project.json", {
        "id": project.id,
        "user_id": project.user_id,
        "name": project.name,
        "root_rel": project.root_rel,
        "status": project.status,
        "storage_authority": project.storage_authority,
        "system_memory_ingest": project.system_memory_ingest,
    })
    return project


async def ensure_inbox_project(db, *, user_id: str, user_root: Path | str) -> WorkspaceProject:
    """未选择项目的新会话落入的 inbox Project（每用户一个，幂等）。"""
    name = get_config().workspace_inbox_project_name
    res = await db.execute(
        select(WorkspaceProject)
        .where(WorkspaceProject.user_id == user_id, WorkspaceProject.name == name,
               WorkspaceProject.status == "active")
        .order_by(WorkspaceProject.created_at.asc())
    )
    existing = res.scalars().first()
    if existing is not None:
        return existing
    return await create_project(db, user_id=user_id, user_root=user_root, name=name)


def _resolve_project_root(user_root: Path, project: WorkspaceProject) -> Path:
    return ws._resolve_no_redirect(user_root, project.root_rel, what="project root")


# --- Task CRUD ----------------------------------------------------------------

async def get_task(db, *, user_id: str, task_id: str) -> WorkspaceTask | None:
    res = await db.execute(
        select(WorkspaceTask).where(WorkspaceTask.id == task_id, WorkspaceTask.user_id == user_id)
    )
    return res.scalar_one_or_none()


async def existing_task_roots(db, *, user_id: str, exclude_task_id: str | None = None,
                              statuses: Sequence[str] = TASK_STATUSES) -> tuple[str, ...]:
    """本用户其他 Task 的 root（重叠校验输入；**必须**排除自己）。"""
    stmt = select(WorkspaceTask.root_rel).where(WorkspaceTask.user_id == user_id,
                                                WorkspaceTask.status.in_(tuple(statuses)))
    if exclude_task_id:
        stmt = stmt.where(WorkspaceTask.id != exclude_task_id)
    res = await db.execute(stmt)
    return tuple(r for r in res.scalars().all() if r)


async def list_tasks(db, *, user_id: str, project_id: str | None = None,
                     statuses: Sequence[str] = ("active",), limit: int = 50,
                     search: str | None = None) -> list[WorkspaceTask]:
    stmt = select(WorkspaceTask).where(WorkspaceTask.user_id == user_id,
                                       WorkspaceTask.status.in_(tuple(statuses)))
    if project_id:
        stmt = stmt.where(WorkspaceTask.project_id == project_id)
    needle = str(search or "").strip()
    if needle:
        like = f"%{needle}%"
        stmt = stmt.where(WorkspaceTask.title.ilike(like) | WorkspaceTask.summary.ilike(like)
                          | WorkspaceTask.goal.ilike(like))
    stmt = stmt.order_by(WorkspaceTask.last_active_at.desc().nullslast(),
                         WorkspaceTask.created_at.desc()).limit(max(1, int(limit)))
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def create_task(db, *, user_id: str, user_root: Path | str, project_id: str,
                      title: str, goal: str = "", storage_authority: str | None = None,
                      system_memory_ingest: str | None = None,
                      tags: Iterable[str] = ()) -> WorkspaceTask:
    base = Path(user_root).resolve()
    project = await get_project(db, user_id=user_id, project_id=project_id)
    if project is None:
        # 归属校验优先于「找不到」：不给跨用户 id 探测信号
        owner = await db.execute(select(WorkspaceProject.user_id)
                                 .where(WorkspaceProject.id == project_id))
        if owner.scalar_one_or_none() is not None:
            raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "project belongs to another user",
                                         project_id)
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "project not found", project_id)
    if str(project.status or "active") not in PROJECT_STATUSES:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "project is not usable",
                                     str(project.status))

    authority = str(storage_authority or project.storage_authority or "cloud")
    if authority != str(project.storage_authority or "cloud"):
        raise ws.WorkspaceScopeError(
            ws.CODE_RELATION_INVALID,
            "task storage_authority must equal project storage_authority", authority)

    ingest = system_memory_ingest or project.system_memory_ingest or "user_facts"
    task = WorkspaceTask(
        id=str(uuid.uuid4()),
        user_id=user_id,
        project_id=project.id,
        title=str(title or "").strip()[:300] or "未命名任务",
        goal=str(goal or "")[:4000] or None,
        root_rel="",
        status="active",
        tags_json=json.dumps(list(tags), ensure_ascii=False) if tags else None,
        storage_authority=authority,
        system_memory_ingest=ingest,
        last_active_at=datetime.utcnow(),
    )
    task.root_rel = ws.task_rel(project.id, task.id)
    ws.validate_task_root(base, task.root_rel, project_id=project.id, task_id=task.id,
                          existing=await existing_task_roots(db, user_id=user_id))
    db.add(task)
    await db.flush()

    task_root = ws._resolve_no_redirect(base, task.root_rel, what="task root")
    ensure_task_directories(task_root)
    _write_projection(task_root / "task.json", {
        "id": task.id,
        "user_id": task.user_id,
        "project_id": task.project_id,
        "title": task.title,
        "goal": task.goal,
        "root_rel": task.root_rel,
        "status": task.status,
        "storage_authority": task.storage_authority,
        "system_memory_ingest": task.system_memory_ingest,
    })
    await touch_project(db, project_id=project.id)
    return task


async def set_task_status(db, *, user_id: str, task_id: str, status: str) -> WorkspaceTask:
    if status not in TASK_STATUSES:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "unknown task status", status)
    task = await get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    task.status = status
    task.updated_at = datetime.utcnow()
    await db.flush()
    await _refresh_task_projection(db, task)
    return task


async def touch_task(db, *, task_id: str) -> None:
    res = await db.execute(select(WorkspaceTask).where(WorkspaceTask.id == task_id))
    task = res.scalar_one_or_none()
    if task is not None:
        task.last_active_at = datetime.utcnow()
        await db.flush()


async def touch_project(db, *, project_id: str) -> None:
    res = await db.execute(select(WorkspaceProject).where(WorkspaceProject.id == project_id))
    project = res.scalar_one_or_none()
    if project is not None:
        project.last_active_at = datetime.utcnow()
        await db.flush()


async def update_task_summary(db, *, user_id: str, task_id: str, summary: str) -> WorkspaceTask:
    task = await get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    task.summary = str(summary or "")[:4000] or None
    await db.flush()
    await _refresh_task_projection(db, task)
    return task


async def update_artifact_manifest(db, *, user_id: str, task_id: str,
                                   add: Sequence[dict[str, Any]] = (),
                                   remove: Sequence[str] = ()) -> list[dict[str, Any]]:
    """稳定交付清单（阶段 A 目录检索的输入之一）。按 path 去重更新。"""
    task = await get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    try:
        current = json.loads(task.artifact_manifest_json or "[]")
    except ValueError:
        current = []
    if not isinstance(current, list):
        current = []
    by_path = {str(item.get("path")): item for item in current if isinstance(item, dict)}
    for drop in remove:
        by_path.pop(str(drop), None)
    for item in add:
        if isinstance(item, dict) and item.get("path"):
            entry = dict(item)
            entry["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            by_path[str(entry["path"])] = entry
    merged = sorted(by_path.values(), key=lambda x: str(x.get("path")))
    task.artifact_manifest_json = json.dumps(merged, ensure_ascii=False)
    await db.flush()
    return merged


async def _refresh_task_projection(db, task: WorkspaceTask) -> None:
    try:
        from app.services.workspace_service import ensure_user_workspace

        workspace = await ensure_user_workspace(db, task.user_id)
        if workspace is None:
            return
        task_root = ws._resolve_no_redirect(Path(workspace.root_path), task.root_rel,
                                            what="task root")
        _write_projection(task_root / "task.json", {
            "id": task.id, "user_id": task.user_id, "project_id": task.project_id,
            "title": task.title, "goal": task.goal, "root_rel": task.root_rel,
            "status": task.status, "storage_authority": task.storage_authority,
            "system_memory_ingest": task.system_memory_ingest,
            "summary": task.summary,
        })
    except Exception:  # 投影刷新是尽力而为，不阻塞 DB 状态变更
        logger.debug("task projection refresh skipped", exc_info=True)


__all__ = [
    "classify_path", "create_project", "create_task", "ensure_inbox_project",
    "ensure_project_directories", "ensure_task_directories", "existing_task_roots",
    "get_project", "get_task", "list_projects", "list_tasks", "read_projection",
    "set_task_status", "task_display_path", "touch_project", "touch_task",
    "update_artifact_manifest", "update_task_summary", "user_rel_path",
]
