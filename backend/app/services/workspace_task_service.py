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

from sqlalchemy import select, update

from app.core.config import get_config
from app.db.database import WorkspaceProject, WorkspaceTask
from app.services import workspace_scope as ws

logger = logging.getLogger(__name__)

# 2026-11-13 更名前的 inbox Project 名（仅启动迁移回填用；运行时按 kind 查找）
_LEGACY_INBOX_NAMES = ("收件箱",)  # noqa: F401 - 保留供迁移/诊断引用

TASK_STATUSES = ("active", "completed", "archived", "deleted")
# 可经 PATCH/update 编辑的状态（`deleted` 只能走 lifecycle purge，评审 C1）
EDITABLE_TASK_STATUSES = ("active", "completed", "archived")
PROJECT_STATUSES = ("active", "archived", "deleted")
# 可创建任务/会话的项目状态（评审 C2：不能用全量词表当 allowlist）
USABLE_PROJECT_STATUSES = ("active",)

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

async def get_project(db, *, user_id: str, project_id: str,
                      for_update: bool = False) -> WorkspaceProject | None:
    stmt = select(WorkspaceProject).where(WorkspaceProject.id == project_id,
                                          WorkspaceProject.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    res = await db.execute(stmt)
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
                         system_memory_ingest: str | None = None,
                         kind: str = "user",
                         description: str | None = None) -> WorkspaceProject:
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
        kind=str(kind or "user") if str(kind or "user") in ("inbox", "user") else "user",
        description=(str(description).strip() or None) if description else None,
        row_version=1,
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
        "kind": project.kind,
        "description": project.description,
        "row_version": project.row_version,
    })
    return project


async def ensure_inbox_project(db, *, user_id: str, user_root: Path | str) -> WorkspaceProject:
    """未选择项目的新会话落入的 inbox Project（每用户一个，幂等）。

    2026-11-15（DESIGN_20261115 §4.2 / A1 修复）：默认项目以 **`kind='inbox'`
    稳定标识**查找，**不再按名字**——默认项目允许改名，按名查找会在改名后
    再建一个默认项目。

    评审 I8：不再「按名字认领任意项目」（否则用户先建一个叫「默认项目」的普通
    项目会被静默转成不可删的系统默认）。历史行由启动迁移一次性回填 kind。
    若 inbox 行处于 deleted（防御：正常路径禁止 purge 默认项目），原地复活。
    """
    name = get_config().workspace_inbox_project_name
    res = await db.execute(
        select(WorkspaceProject)
        .where(WorkspaceProject.user_id == user_id, WorkspaceProject.kind == "inbox")
        .order_by(WorkspaceProject.created_at.asc())
    )
    existing = res.scalars().first()
    if existing is not None:
        if str(existing.status) == "deleted":
            # 防御性复活（唯一索引仍覆盖该行，不能另建）
            existing.status = "active"
            existing.deleted_at = None
            await db.flush()
        return existing
    return await create_project(db, user_id=user_id, user_root=user_root, name=name, kind="inbox")


def _resolve_project_root(user_root: Path, project: WorkspaceProject) -> Path:
    return ws._resolve_no_redirect(user_root, project.root_rel, what="project root")


# --- Task CRUD ----------------------------------------------------------------

async def get_task(db, *, user_id: str, task_id: str,
                   for_update: bool = False) -> WorkspaceTask | None:
    stmt = select(WorkspaceTask).where(WorkspaceTask.id == task_id,
                                       WorkspaceTask.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    res = await db.execute(stmt)
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
                      tags: Iterable[str] = (), description: str | None = None,
                      origin: str | None = None) -> WorkspaceTask:
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
    if str(project.status or "active") not in USABLE_PROJECT_STATUSES:
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
        description=(str(description).strip() or None) if description else None,
        origin=(str(origin) if str(origin or "") in ("user", "auto", "fork", "agent") else None),
        row_version=1,
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
        "description": task.description,
        "origin": task.origin,
        "row_version": task.row_version,
    })
    await touch_project(db, project_id=project.id)
    return task


def _check_row_version(row, expected: int | None, *, code: str, what: str) -> None:
    """编辑 CAS：`expected_row_version` 必须等于当前 `row_version`（None = 不校验）。"""
    if expected is None:
        return
    current = int(getattr(row, "row_version", 1) or 1)
    if int(expected) != current:
        raise ws.WorkspaceScopeError(
            code, f"{what} row_version {current} != expected {expected}", str(getattr(row, "id", "")))


def _bump_row_version(row) -> None:
    row.row_version = int(getattr(row, "row_version", 1) or 1) + 1


async def update_task(db, *, user_id: str, task_id: str, title: str | None = None,
                      description: str | None = None, status: str | None = None,
                      expected_row_version: int | None = None) -> WorkspaceTask:
    """任务编辑（2026-11-15 任务管理中心）：标题/备注/状态 + CAS。

    与 `rename_task` 同构但覆盖全部可编辑字段；`row_version` 每次成功编辑 +1。
    """
    task = await get_task(db, user_id=user_id, task_id=task_id, for_update=True)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    if str(task.status) == "deleted":
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "deleted task cannot be edited; restore it first", task_id)
    _check_row_version(task, expected_row_version, code=ws.CODE_TASK_STALE, what="task")
    if title is not None:
        clean = str(title).strip()
        if not clean:
            raise ws.WorkspaceScopeError(ws.CODE_ROOT_INVALID, "task title is required", task_id)
        task.title = clean[:300]
    if description is not None:
        task.description = str(description).strip()[:8000] or None
    if status is not None:
        # 评审 C1：`deleted` 只能经 lifecycle purge（confirm + 活跃 run + 会话处理 + 回收站）
        if status not in EDITABLE_TASK_STATUSES:
            raise ws.WorkspaceScopeError(
                ws.CODE_RELATION_INVALID,
                "status must be active|completed|archived; use purge to delete", status)
        task.status = status
    task.updated_at = datetime.utcnow()
    _bump_row_version(task)
    await db.flush()
    await _refresh_task_projection(task)
    return task


async def rename_task(db, *, user_id: str, task_id: str, title: str) -> WorkspaceTask:
    """任务重命名（兼容包装）：DB 权威 + `task.json` 投影同步 + owner 校验。

    Task 标题不是展示用的装饰：`search_tasks`（延续/归类）、`workspace_bind_task`
    工具与任务记忆都以它为键，所以标题必须与真实目标一致。
    """
    return await update_task(db, user_id=user_id, task_id=task_id, title=title)


async def set_task_status(db, *, user_id: str, task_id: str, status: str) -> WorkspaceTask:
    return await update_task(db, user_id=user_id, task_id=task_id, status=status)


async def update_project(db, *, user_id: str, project_id: str, name: str | None = None,
                         description: str | None = None,
                         system_memory_ingest: str | None = None,
                         expected_row_version: int | None = None) -> WorkspaceProject:
    """项目编辑（2026-11-15 任务管理中心）：名称/备注/记忆策略 + CAS。

    默认项目（kind='inbox'）**允许改名/写备注**——身份按 kind 而非名字（A1 修复）。
    `storage_authority` 不在可编辑面（存储权威单向继承，§4.11）。
    """
    project = await get_project(db, user_id=user_id, project_id=project_id, for_update=True)
    if project is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE,
                                     "project not found for user", project_id)
    if str(project.status) == "deleted":
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "deleted project cannot be edited; restore it first", project_id)
    _check_row_version(project, expected_row_version, code=ws.CODE_PROJECT_STALE, what="project")
    if name is not None:
        clean = str(name).strip()
        if not clean:
            raise ws.WorkspaceScopeError(ws.CODE_ROOT_INVALID, "project name is required", "")
        project.name = clean[:200]
    if description is not None:
        project.description = str(description).strip()[:8000] or None
    if system_memory_ingest is not None:
        if system_memory_ingest not in ("normal", "user_facts", "exclude"):
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                         "unknown system_memory_ingest", system_memory_ingest)
        project.system_memory_ingest = system_memory_ingest
    project.updated_at = datetime.utcnow()
    _bump_row_version(project)
    await db.flush()
    await _refresh_project_projection(project)
    return project


async def rename_project(db, *, user_id: str, project_id: str, name: str) -> WorkspaceProject:
    """项目重命名（兼容包装）：DB 权威，`project.json` 投影同步，owner 校验。"""
    return await update_project(db, user_id=user_id, project_id=project_id, name=name)


async def move_tasks(db, *, user_id: str, user_root: Path | str, task_ids: Sequence[str],
                     target_project_id: str) -> dict[str, Any]:
    """跨项目移动任务（2026-11-18 六缺陷波 issue1，DESIGN_20261115 §11 non-goal 现落地）。

    以 Task 为单位改挂到目标项目：更新 `project_id` + canonical `root_rel` + 该任务全部
    绑定的 `project_id`，并返回 **目录物理搬移计划**（`file_moves` = [(old_root_rel,
    new_root_rel)]，调用方 commit 后经 `workspace_task_lifecycle_service.apply_move_plan`
    执行，与 purge/restore 的「commit 后搬目录」同一顺序保证）。

    硬约束（与创建同口径）：
    * 目标项目 owner 校验 + 必须 active（USABLE_PROJECT_STATUSES）；
    * 任务 `storage_authority` 必须等于目标项目（单向继承，§4.11），否则整批拒绝；
    * 新 `root_rel` 不得与本人其他任务目录重叠；
    * **有活跃 run 的任务禁止移动**（评审 C1：搬走运行中任务的目录会让冻结的 ctx 把
      写入落回旧树 → 静默丢交付物，与 purge 同一守卫口径）；
    * 已在目标项目的任务幂等跳过（不 bump 版本、不搬目录）。
    """
    from app.db.database import WorkspaceBinding, WorkspaceRun
    from app.services.workspace_run_service import ACTIVE_RUN_STATUSES

    ids = list(dict.fromkeys(str(t) for t in (task_ids or []) if str(t).strip()))
    if not ids:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "no task ids", "")
    if len(ids) > 200:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "too many task ids (max 200)", "")
    target = await get_project(db, user_id=user_id, project_id=target_project_id, for_update=True)
    if target is None:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "target project not found",
                                     target_project_id)
    if str(target.status or "active") not in USABLE_PROJECT_STATUSES:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "target project is not usable",
                                     str(target.status))

    # 活跃 run 守卫（评审 C1）：一次性查出这批任务里有没有活跃 run，命中即整批拒绝。
    active_run = (await db.execute(
        select(WorkspaceRun.id)
        .where(WorkspaceRun.user_id == user_id, WorkspaceRun.task_id.in_(ids),
               WorkspaceRun.status.in_(ACTIVE_RUN_STATUSES))
        .limit(1)
    )).scalar_one_or_none()
    if active_run is not None:
        raise ws.WorkspaceScopeError(ws.CODE_RUN_ACTIVE,
                                     "park or stop the active run before moving the task", "")

    existing = set(await existing_task_roots(db, user_id=user_id))
    moved: list[WorkspaceTask] = []
    file_moves: list[tuple[str, str]] = []
    for tid in ids:
        task = await get_task(db, user_id=user_id, task_id=tid, for_update=True)
        if task is None:
            raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", tid)
        if str(task.status) == "deleted":
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                         "deleted task cannot be moved; restore it first", tid)
        if str(task.project_id) == str(target.id):
            continue  # 幂等：已在目标项目
        if str(task.storage_authority or "cloud") != str(target.storage_authority or "cloud"):
            raise ws.WorkspaceScopeError(
                ws.CODE_RELATION_INVALID,
                "task storage_authority differs from target project", tid)
        new_root_rel = ws.task_rel(target.id, task.id)
        if new_root_rel in existing:
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                         "target task path already exists", new_root_rel)
        old_root_rel = task.root_rel
        task.project_id = target.id
        task.root_rel = new_root_rel
        task.updated_at = datetime.utcnow()
        _bump_row_version(task)
        existing.discard(old_root_rel)
        existing.add(new_root_rel)
        moved.append(task)
        file_moves.append((old_root_rel, new_root_rel))
        # 绑定行的 project_id 同步（workspace 表非 SYNC_ENTITIES，Core UPDATE 安全）
        await db.execute(
            update(WorkspaceBinding)
            .where(WorkspaceBinding.user_id == user_id, WorkspaceBinding.task_id == task.id)
            .values(project_id=target.id))
    await db.flush()
    # 投影刷新交给调用方在 apply_move_plan **之后** 做（API 的 _refresh_projections）——
    # 此处目录尚未物理搬移，提前写 task.json 会因新目录不存在而失败（投影是 best-effort）。
    if moved:
        await touch_project(db, project_id=target.id)
    return {"moved": len(moved), "task_ids": [t.id for t in moved], "file_moves": file_moves}


async def _refresh_project_projection(project: WorkspaceProject) -> None:
    """项目投影刷新（尽力而为，**既不阻塞也不提交**调用方的 DB 变更；投影不授予权限）。

    与 `_refresh_task_projection` 同一条约束：用独立短会话取用户根，绝不借调用方会话
    （`ensure_user_workspace` 内部有 `commit()`，借会话就会把调用方未写完的持久化提前落盘）。
    """
    try:
        from app.db.database import AsyncSessionLocal, User
        from app.services.workspace_service import ensure_user_workspace

        async with AsyncSessionLocal() as own:
            user_row = await own.get(User, project.user_id)
            workspace = await ensure_user_workspace(
                own, project.user_id, getattr(user_row, "username", None))
        if workspace is None:
            return
        root = ws._resolve_no_redirect(Path(workspace.root_path), project.root_rel,
                                       what="project root")
        _write_projection(root / "project.json", {
            "id": project.id, "user_id": project.user_id, "name": project.name,
            "root_rel": project.root_rel, "status": project.status,
            "storage_authority": project.storage_authority,
            "system_memory_ingest": project.system_memory_ingest,
            "kind": project.kind, "description": project.description,
            "row_version": project.row_version,
        })
    except Exception:  # noqa: BLE001 - 投影刷新是尽力而为
        logger.debug("project projection refresh skipped", exc_info=True)


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
    await _refresh_task_projection(task)
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


async def _workspace_root_for(user_id: str) -> Path | None:
    """用户根——**独立短会话**取。

    `ensure_user_workspace` 内部会 `commit()`（workspace_service.py:71/92）。借调用方会话
    就会把「调用方还没写完的持久化」提前落盘：agent_worker 的原子 persist 注释早已点出这个面
    （评审 r1 Critical-2 在标题同步路径上把它抓了回来，conversation.py 的 PUT 同理）。
    投影只是尽力而为的文件副作用，**不该持有提交权**。
    """
    from app.db.database import AsyncSessionLocal
    from app.services.workspace_service import ensure_user_workspace

    async with AsyncSessionLocal() as own:
        workspace = await ensure_user_workspace(own, user_id)
    return Path(workspace.root_path) if workspace is not None else None


async def _refresh_task_projection(task: WorkspaceTask) -> None:
    """任务投影刷新：尽力而为，**既不阻塞也不提交**调用方的 DB 变更；投影不授予权限。

    失败语义是「DB 已改、`task.json` 仍是旧的」（不是「什么都没发生」），所以记 WARNING
    让漂移可见，而不是 DEBUG 吞掉。
    """
    try:
        root = await _workspace_root_for(task.user_id)
        if root is None:
            return
        task_root = ws._resolve_no_redirect(root, task.root_rel, what="task root")
        _write_projection(task_root / "task.json", {
            "id": task.id, "user_id": task.user_id, "project_id": task.project_id,
            "title": task.title, "goal": task.goal, "root_rel": task.root_rel,
            "status": task.status, "storage_authority": task.storage_authority,
            "system_memory_ingest": task.system_memory_ingest,
            "summary": task.summary, "description": task.description,
            "origin": task.origin, "row_version": task.row_version,
        })
    except Exception:  # noqa: BLE001
        logger.warning("task projection refresh skipped (DB change already flushed)",
                       exc_info=True)


__all__ = [
    "classify_path", "create_project", "create_task", "ensure_inbox_project",
    "ensure_project_directories", "ensure_task_directories", "existing_task_roots",
    "get_project", "get_task", "list_projects", "list_tasks", "move_tasks", "read_projection",
    "rename_project", "rename_task", "set_task_status", "task_display_path",
    "touch_project", "touch_task", "update_artifact_manifest", "update_project",
    "update_task", "update_task_summary", "user_rel_path",
]
