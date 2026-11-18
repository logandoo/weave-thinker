# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Project / Task 生命周期服务（2026-11-15 任务管理中心 · DESIGN_20261115 §4.5–§4.7）。

与 `workspace_task_service`（身份/目录/编辑）分离：本模块只负责**状态机**与
**级联副作用**，避免把删除语义塞进 CRUD 文件。

状态机（两级同构）：

* `active` → `archived`（archive：文件与绑定全部保留，可 restore）
* `active`/`archived` → `deleted`（purge：目录移入 `_trash/` 保留 7 天，
  `deleted_at` 落档，可 restore）
* `deleted` → `active`（restore：目录从 `_trash/` 移回）

删除时的会话处理（§4.6，ADR D-20261115-design-3 已拍板）：

* `conversations="unbind"`（**默认**）：只删绑定行，会话保留进「未归类」；
  历史消息/附件/逐消息来源快照全部不变；下一回合按默认路由重新铸任务。
* `conversations="delete"`：复用 `DELETE /api/conversations/{id}` 语义
  （活跃委派任务 409、取消定时任务、消息级联），并显式清理
  `workspace_message_contexts` 与 `workspace_runs.conversation_id`（两者无物理 FK）。
* **笔记永远只解绑**（不提供级联删除）。

文件移动与 DB 事务的顺序（评审 I2）：服务只做 DB 变更并返回 `file_plan`；
调用方 **commit 之后** 再调 `apply_file_plan()` 搬目录。这样 commit 失败时
文件仍在原位（不会出现「DB 说 active、文件已在回收站」的不可恢复态）。
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update

from app.db.database import (
    AgentTask,
    ChatSession,
    Conversation,
    NoteTaskBinding,
    ScheduledTask,
    WorkspaceBinding,
    WorkspaceMessageContext,
    WorkspaceProject,
    WorkspaceRun,
    WorkspaceTask,
)
from app.services import workspace_scope as ws
from app.services import workspace_task_service as svc

logger = logging.getLogger(__name__)

TRASH_DIR = "_trash"
CONVERSATION_MODES = ("unbind", "delete")
TRASH_RETENTION_DAYS = 7


# --- 文件计划（commit 后执行）------------------------------------------------

@dataclass(frozen=True)
class FilePlan:
    action: str          # trash | restore
    root_rel: str
    kind: str            # project | task
    object_id: str


def _trash_path(user_root: Path, kind: str, object_id: str) -> Path:
    return Path(user_root) / TRASH_DIR / f"{kind}_{object_id}"


def _move_to_trash(user_root: Path, root_rel: str, kind: str, object_id: str) -> str | None:
    try:
        src = ws._resolve_no_redirect(Path(user_root), root_rel, what="trash source")
    except ws.WorkspaceScopeError:
        return None
    if not src.is_dir():
        return None
    dest = _trash_path(Path(user_root), kind, object_id)
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return str(dest.relative_to(Path(user_root)).as_posix())
    except OSError as exc:  # noqa: BLE001 - 目录移动是尽力而为
        logger.warning("workspace trash move failed (%s → %s): %s", src, dest, exc)
        return None


def _restore_from_trash(user_root: Path, root_rel: str, kind: str, object_id: str) -> bool:
    try:
        dest = ws._resolve_no_redirect(Path(user_root), root_rel, what="restore dest")
    except ws.WorkspaceScopeError:
        return False
    src = _trash_path(Path(user_root), kind, object_id)
    if dest.exists() or not src.is_dir():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return True
    except OSError as exc:  # noqa: BLE001
        logger.warning("workspace restore move failed (%s → %s): %s", src, dest, exc)
        return False


def apply_file_plan(user_root: Path | str, plans: list[FilePlan]) -> list[str]:
    """在 DB commit **之后** 执行文件搬移；返回实际移动的回收站相对路径。"""
    moved: list[str] = []
    for plan in plans or []:
        if plan.action == "trash":
            rel = _move_to_trash(Path(user_root), plan.root_rel, plan.kind, plan.object_id)
            if rel:
                moved.append(rel)
        elif plan.action == "restore":
            _restore_from_trash(Path(user_root), plan.root_rel, plan.kind, plan.object_id)
    return moved


def apply_move_plan(user_root: Path | str, moves: list[tuple[str, str]]) -> list[str]:
    """跨项目移动任务后的目录物理搬移（commit 之后执行，与 apply_file_plan 同一顺序保证）。

    `moves` = [(old_root_rel, new_root_rel)]。源目录不存在 → 跳过。目标已存在（罕见：历史
    失败残留）→ **合并** 而非跳过（评审 I1：绝不静默把文件留在原地却报成功）。返回实际搬/
    并的新相对路径。
    """
    root = Path(user_root)
    moved: list[str] = []
    for old_rel, new_rel in moves or []:
        try:
            src = ws._resolve_no_redirect(root, old_rel, what="move source")
            dst = ws._resolve_no_redirect(root, new_rel, what="move dest")
        except ws.WorkspaceScopeError:
            continue
        if not src.is_dir():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                # 目标已存在 → 逐条目并入（绝不覆盖同名），避免文件滞留旧路径（评审 I1）
                if _merge_move_dir(src, dst) or not any(src.iterdir()):
                    moved.append(new_rel)
            else:
                shutil.move(str(src), str(dst))
                moved.append(new_rel)
        except OSError as exc:  # noqa: BLE001 - 目录移动尽力而为（DB 已权威改挂）
            logger.warning("workspace move dir failed (%s → %s): %s", src, dst, exc)
    return moved


def _merge_move_dir(src_dir: Path, dst_dir: Path) -> int:
    """把 src_dir 的内容逐个并入 dst_dir（目录递归合并，文件**绝不覆盖**同名目标）。

    返回移动的条目数。用于改绑时把孤儿任务的 workspace 文件迁到目标任务。
    符号链接安全（评审 I2）：链接本身整体 rename，**绝不跟随链接递归**（否则一个指向
    别的任务 workspace 的符号链接会把别人的真实文件搬走 = 跨任务逃逸）。
    """
    moved = 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        entries = list(src_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        target = dst_dir / entry.name
        try:
            if entry.is_symlink():
                # 只搬链接本身（不解析目标）；目标同名则保留源，不动
                if target.exists() or target.is_symlink():
                    continue
                entry.rename(target)
                moved += 1
            elif entry.is_dir():
                if target.is_dir():
                    moved += _merge_move_dir(entry, target)
                    try:
                        entry.rmdir()
                    except OSError:
                        pass  # 非空（同名文件留下）→ 不动
                elif target.exists():
                    continue  # 文件 vs 目录名冲突 → 不覆盖，保留源
                else:
                    entry.rename(target)
                    moved += 1
            else:
                if target.exists():
                    continue  # 绝不覆盖
                entry.rename(target)
                moved += 1
        except OSError as exc:  # noqa: BLE001
            logger.warning("workspace merge move failed (%s → %s): %s", entry, target, exc)
    return moved


async def remaining_conversation_count(db, *, user_id: str, task_id: str) -> int:
    res = await db.execute(
        select(func.count(WorkspaceBinding.id))
        .where(WorkspaceBinding.user_id == user_id, WorkspaceBinding.task_id == task_id))
    return int(res.scalar_one() or 0)


async def migrate_orphaned_task_files(db, *, user_id: str, user_root: Path | str,
                                      from_task_id: str | None,
                                      to_task_id: str | None) -> dict[str, Any]:
    """改绑后把「变空的源任务」的 workspace 文件迁到目标任务（2026-11-18 issue4）。

    仅在**独占场景**触发：源任务改绑后剩余会话数 == 0 时才搬文件；源任务仍被其他会话
    共享（>0）时**绝不动**——保留「同任务会话共享文件域」的血缘保证。同任务 / 缺任务 /
    存储权威不一致 / 路径异常 → 无操作。调用方必须在 rebind commit **之后** 调用，使剩余
    会话计数反映改绑后的真实状态。

    并发安全（评审 I4）：先 `for_update` 锁住源任务行，**锁后再数剩余会话**，再搬——两条
    并发改绑不会都把同一源任务的 workspace 拆着并进两个不同目标。返回 `{migrated, reason}`。
    """
    if not from_task_id or not to_task_id or str(from_task_id) == str(to_task_id):
        return {"migrated": 0, "reason": "noop"}
    src = await svc.get_task(db, user_id=user_id, task_id=from_task_id, for_update=True)
    dst = await svc.get_task(db, user_id=user_id, task_id=to_task_id)
    if src is None or dst is None:
        return {"migrated": 0, "reason": "missing"}
    if str(src.storage_authority or "cloud") != str(dst.storage_authority or "cloud"):
        return {"migrated": 0, "reason": "authority"}
    if await remaining_conversation_count(db, user_id=user_id, task_id=from_task_id) > 0:
        return {"migrated": 0, "reason": "shared"}
    root = Path(user_root).resolve()
    try:
        src_ws = ws._resolve_no_redirect(root, f"{src.root_rel}/workspace", what="src workspace")
        dst_ws = ws._resolve_no_redirect(root, f"{dst.root_rel}/workspace", what="dst workspace")
    except ws.WorkspaceScopeError:
        return {"migrated": 0, "reason": "badpath"}
    if not src_ws.is_dir():
        return {"migrated": 0, "reason": "nosrc"}
    dst_ws.mkdir(parents=True, exist_ok=True)
    moved = _merge_move_dir(src_ws, dst_ws)
    if moved:
        logger.info("rebind orphan-file migration: task %s → %s, %d entry(ies)",
                    from_task_id, to_task_id, moved)
    return {"migrated": moved, "reason": "moved", "from": str(from_task_id), "to": str(to_task_id)}


# --- 守卫 ---------------------------------------------------------------------

async def _active_run_for_tasks(db, *, user_id: str, task_ids: list[str]) -> str | None:
    from app.services.workspace_run_service import ACTIVE_RUN_STATUSES

    if not task_ids:
        return None
    res = await db.execute(
        select(WorkspaceRun.id)
        .where(WorkspaceRun.user_id == user_id,
               WorkspaceRun.task_id.in_(task_ids),
               WorkspaceRun.status.in_(ACTIVE_RUN_STATUSES))
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _active_delegation_for_conversations(db, *, user_id: str,
                                               task_ids: list[str]) -> str | None:
    """评审 I4：pending/resumable 的 separate_v1 委派任务还没有 run，但 worker 会
    在稍后写进该 Task——purge 前必须一并阻断。"""
    from app.services.agent_task_status import ACTIVE_STATUSES

    if not task_ids:
        return None
    conv_ids = select(WorkspaceBinding.conversation_id).where(
        WorkspaceBinding.user_id == user_id, WorkspaceBinding.task_id.in_(task_ids))
    res = await db.execute(
        select(AgentTask.id)
        .where(AgentTask.execution_conversation_id.in_(conv_ids),
               AgentTask.execution_mode == "separate_v1",
               AgentTask.status.in_(ACTIVE_STATUSES))
        .limit(1)
    )
    return res.scalar_one_or_none()


async def _task_ids_of_project(db, *, user_id: str, project_id: str) -> list[str]:
    res = await db.execute(
        select(WorkspaceTask.id).where(WorkspaceTask.user_id == user_id,
                                       WorkspaceTask.project_id == project_id,
                                       WorkspaceTask.status != "deleted")
    )
    return [r for r in res.scalars().all() if r]


# --- 会话处理 -----------------------------------------------------------------

async def _delete_conversation(db, *, user_id: str, conversation_id: str) -> bool:
    """删除单个会话（复用 DELETE /api/conversations/{id} 语义）。"""
    from app.services.agent_task_status import ACTIVE_STATUSES

    active_task = (await db.execute(
        select(AgentTask.id).where(
            AgentTask.execution_conversation_id == conversation_id,
            AgentTask.execution_mode == "separate_v1",
            AgentTask.status.in_(ACTIVE_STATUSES),
        ).limit(1)
    )).scalar_one_or_none()
    if active_task:
        return False

    await db.execute(
        update(ScheduledTask)
        .where(ScheduledTask.conversation_id == conversation_id,
               ScheduledTask.status == "active")
        .values(status="cancelled", next_run_at=None)
    )
    # 逐消息来源快照无物理 FK：显式清理（否则悬空行）
    run_ids = select(WorkspaceRun.id).where(WorkspaceRun.conversation_id == conversation_id)
    await db.execute(
        delete(WorkspaceMessageContext)
        .where(WorkspaceMessageContext.user_id == user_id,
               WorkspaceMessageContext.run_id.in_(run_ids))
    )
    # runs.conversation_id 无物理 FK：置空（保留 run 审计行）
    await db.execute(
        update(WorkspaceRun)
        .where(WorkspaceRun.conversation_id == conversation_id)
        .values(conversation_id=None)
    )
    # ChatSession 无级联（评审 M1）：显式删，避免留下 conversation_id=NULL 的孤儿
    await db.execute(delete(ChatSession).where(ChatSession.conversation_id == conversation_id))
    conv = await db.get(Conversation, conversation_id)
    if conv is not None and str(conv.user_id) == str(user_id):
        await db.delete(conv)  # 消息级联（messages.conversation_id FK ON DELETE CASCADE）
    return True


async def _handle_conversations(db, *, user_id: str, task_ids: list[str],
                                mode: str) -> dict[str, Any]:
    if mode not in CONVERSATION_MODES:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "conversations must be unbind|delete", str(mode))
    if not task_ids:
        return {"conversations_unbound": 0, "conversations_deleted": 0}
    rows = (await db.execute(
        select(WorkspaceBinding.id, WorkspaceBinding.conversation_id)
        .where(WorkspaceBinding.user_id == user_id,
               WorkspaceBinding.task_id.in_(task_ids))
    )).all()
    conv_ids = [r[1] for r in rows]
    if mode == "delete":
        blocked: list[str] = []
        for cid in conv_ids:
            if not await _delete_conversation(db, user_id=user_id, conversation_id=cid):
                blocked.append(cid)
        if blocked:
            raise ws.WorkspaceScopeError(
                ws.CODE_RUN_ACTIVE,
                "some conversations are owned by active delegated tasks; cancel them first",
                ",".join(blocked))
        await db.execute(delete(WorkspaceBinding)
                         .where(WorkspaceBinding.user_id == user_id,
                                WorkspaceBinding.task_id.in_(task_ids)))
        return {"conversations_unbound": 0, "conversations_deleted": len(conv_ids)}
    await db.execute(delete(WorkspaceBinding)
                     .where(WorkspaceBinding.user_id == user_id,
                            WorkspaceBinding.task_id.in_(task_ids)))
    return {"conversations_unbound": len(conv_ids), "conversations_deleted": 0}


async def _unbind_notes(db, *, user_id: str, task_ids: list[str]) -> int:
    """笔记永远只解绑（多对多：只解这些任务上的关系，其他任务的绑定保留）。

    评审 #3：`notes` 是 SYNC_ENTITIES 成员，Core bulk UPDATE 不触发 mapper 事件 =
    同步泄漏（其他设备仍显示绑定）。必须逐行 ORM 赋值——这条约束由
    `workspace_note_binding_service.unbind_tasks_notes` 统一满足（它删关系行并回写
    `notes.task_id` 镜像）。
    """
    from app.services import workspace_note_binding_service as nbs

    return await nbs.unbind_tasks_notes(db, user_id=user_id, task_ids=task_ids)


# --- 级联计数 / 预览 ----------------------------------------------------------

async def cascade_counts(db, *, user_id: str, task_ids: list[str]) -> dict[str, int]:
    if not task_ids:
        return {"tasks": 0, "conversations": 0, "notes": 0}
    conv = (await db.execute(
        select(func.count(WorkspaceBinding.id))
        .where(WorkspaceBinding.user_id == user_id,
               WorkspaceBinding.task_id.in_(task_ids))
    )).scalar_one()
    notes = (await db.execute(
        select(func.count(func.distinct(NoteTaskBinding.note_id)))
        .where(NoteTaskBinding.user_id == user_id,
               NoteTaskBinding.task_id.in_(task_ids))
    )).scalar_one()
    return {"tasks": len(task_ids), "conversations": int(conv or 0), "notes": int(notes or 0)}


async def preview_task_delete(db, *, user_id: str, task_id: str) -> dict[str, Any]:
    """删除前预览（评审 I7）：UI 在输入 confirm 之前展示级联计数。"""
    task = await svc.get_task(db, user_id=user_id, task_id=task_id)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    counts = await cascade_counts(db, user_id=user_id, task_ids=[task.id])
    return {"task_id": task.id, "title": task.title, "row_version": task.row_version,
            "counts": counts}


async def preview_project_delete(db, *, user_id: str, project_id: str) -> dict[str, Any]:
    project = await svc.get_project(db, user_id=user_id, project_id=project_id)
    if project is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "project not found for user", project_id)
    task_ids = await _task_ids_of_project(db, user_id=user_id, project_id=project.id)
    counts = await cascade_counts(db, user_id=user_id, task_ids=task_ids)
    return {"project_id": project.id, "name": project.name,
            "row_version": project.row_version, "is_default": str(project.kind) == "inbox",
            "counts": counts}


# --- archive / restore --------------------------------------------------------

async def archive_task(db, *, user_id: str, task_id: str,
                       expected_row_version: int | None = None) -> WorkspaceTask:
    task = await svc.get_task(db, user_id=user_id, task_id=task_id, for_update=True)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    if str(task.status) == "deleted":
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "task is deleted", task_id)
    if str(task.status) == "archived":
        # 幂等（2026-11-18 issue5）：重复归档是 no-op，**不 bump 版本、不校验 CAS**——
        # 否则前端旧版本号 + 重复点击会撞上 `workspace_task_stale`（row_version 20 != 19）。
        return task
    return await svc.update_task(db, user_id=user_id, task_id=task_id, status="archived",
                                 expected_row_version=expected_row_version)


async def archive_project(db, *, user_id: str, project_id: str,
                          expected_row_version: int | None = None) -> WorkspaceProject:
    project = await svc.get_project(db, user_id=user_id, project_id=project_id, for_update=True)
    if project is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "project not found for user", project_id)
    if str(project.kind or "user") == "inbox":
        raise ws.WorkspaceScopeError(ws.CODE_PROJECT_RESERVED, "默认项目不可归档", project_id)
    if str(project.status) == "deleted":
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "project is deleted", project_id)
    if str(project.status) == "archived":
        return project  # 幂等（同 archive_task）：重复归档不再 bump 版本
    svc._check_row_version(project, expected_row_version,
                           code=ws.CODE_PROJECT_STALE, what="project")
    project.status = "archived"
    project.updated_at = datetime.utcnow()
    svc._bump_row_version(project)
    # §4.7：archive 项目 = 项目与其任务一起移出活跃列表（评审 I5）
    res = await db.execute(
        select(WorkspaceTask).where(WorkspaceTask.user_id == user_id,
                                    WorkspaceTask.project_id == project.id,
                                    WorkspaceTask.status.in_(("active", "completed")))
    )
    for task in res.scalars().all():
        task.status = "archived"
        task.updated_at = datetime.utcnow()
        svc._bump_row_version(task)
    await db.flush()
    await svc._refresh_project_projection(project)
    return project


async def restore_task(db, *, user_id: str, task_id: str) -> tuple[WorkspaceTask, list[FilePlan]]:
    task = await svc.get_task(db, user_id=user_id, task_id=task_id, for_update=True)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    if str(task.status) == "active":
        return task, []   # 幂等（与 archive 对称）：重复「取消归档」不再 bump、不再报错
    if str(task.status) not in ("archived", "deleted"):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "only archived/deleted tasks can be restored", str(task.status))
    # 评审 #2：项目仍处于 deleted 时不得单独复活任务（否则 active 任务挂在已删项目下）
    project = await svc.get_project(db, user_id=user_id, project_id=task.project_id)
    if project is not None and str(project.status) == "deleted":
        raise ws.WorkspaceScopeError(
            ws.CODE_RELATION_INVALID,
            "project is deleted; restore the project first", str(project.id))
    plan: list[FilePlan] = []
    if str(task.status) == "deleted":
        plan.append(FilePlan("restore", task.root_rel, "task", task.id))
    task.status = "active"
    task.deleted_at = None
    task.deleted_reason = None
    task.updated_at = datetime.utcnow()
    svc._bump_row_version(task)
    await db.flush()
    return task, plan


async def restore_project(db, *, user_id: str,
                          project_id: str) -> tuple[WorkspaceProject, list[FilePlan]]:
    project = await svc.get_project(db, user_id=user_id, project_id=project_id, for_update=True)
    if project is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "project not found for user", project_id)
    if str(project.status) == "active":
        return project, []   # 幂等（与 archive_project 对称）
    if str(project.status) not in ("archived", "deleted"):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "only archived/deleted projects can be restored", str(project.status))
    plan: list[FilePlan] = []
    if str(project.status) == "deleted":
        plan.append(FilePlan("restore", project.root_rel, "project", project.id))
    project.status = "active"
    project.deleted_at = None
    project.updated_at = datetime.utcnow()
    svc._bump_row_version(project)
    # 只复活与项目同批删除的任务（评审 I3）：deleted_reason='project' 或 archived
    res = await db.execute(
        select(WorkspaceTask).where(WorkspaceTask.user_id == user_id,
                                    WorkspaceTask.project_id == project.id,
                                    WorkspaceTask.status.in_(("archived", "deleted")))
    )
    for task in res.scalars().all():
        if str(task.status) == "deleted" and str(task.deleted_reason) != "project":
            continue  # 用户单独 purge 的任务不复活
        if str(task.status) == "deleted":
            plan.append(FilePlan("restore", task.root_rel, "task", task.id))
        task.status = "active"
        task.deleted_at = None
        task.deleted_reason = None
        task.updated_at = datetime.utcnow()
        svc._bump_row_version(task)
    await db.flush()
    await svc._refresh_project_projection(project)
    return project, plan


# --- purge --------------------------------------------------------------------

async def purge_task(db, *, user_id: str, task_id: str,
                     confirm_title: str, expected_row_version: int | None = None,
                     conversations: str = "unbind") -> dict[str, Any]:
    """彻底删除任务（两段式第二段）。文件搬移由调用方 commit 后 `apply_file_plan` 执行。"""
    task = await svc.get_task(db, user_id=user_id, task_id=task_id, for_update=True)
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "task not found for user", task_id)
    if str(task.status) == "deleted":
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "task already deleted", task_id)
    if str(confirm_title or "") != str(task.title or ""):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "confirm_title does not match task title", task_id)
    svc._check_row_version(task, expected_row_version, code=ws.CODE_TASK_STALE, what="task")
    if await _active_run_for_tasks(db, user_id=user_id, task_ids=[task.id]):
        raise ws.WorkspaceScopeError(ws.CODE_RUN_ACTIVE,
                                     "park the active run before purging", task_id)
    if await _active_delegation_for_conversations(db, user_id=user_id, task_ids=[task.id]):
        raise ws.WorkspaceScopeError(ws.CODE_RUN_ACTIVE,
                                     "cancel active delegated tasks before purging", task_id)

    counts = await cascade_counts(db, user_id=user_id, task_ids=[task.id])
    conv_counts = await _handle_conversations(db, user_id=user_id,
                                              task_ids=[task.id], mode=conversations)
    notes_unbound = await _unbind_notes(db, user_id=user_id, task_ids=[task.id])

    task.status = "deleted"
    task.deleted_at = datetime.utcnow()
    task.deleted_reason = "user"
    task.updated_at = datetime.utcnow()
    svc._bump_row_version(task)
    await db.flush()
    await svc._refresh_task_projection(task)
    return {
        "id": task.id, "status": task.status, "mode": "purge",
        "tasks": 1,
        "conversations_unbound": conv_counts["conversations_unbound"],
        "conversations_deleted": conv_counts["conversations_deleted"],
        "notes_unbound": notes_unbound,
        "counts": counts,
        "file_plan": [FilePlan("trash", task.root_rel, "task", task.id)],
    }


async def purge_project(db, *, user_id: str, project_id: str,
                        confirm_name: str, expected_row_version: int | None = None,
                        conversations: str = "unbind") -> dict[str, Any]:
    """彻底删除项目：其下任务一并 purge、会话按 §4.6 处理、笔记解绑（§4.7）。"""
    project = await svc.get_project(db, user_id=user_id, project_id=project_id, for_update=True)
    if project is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "project not found for user", project_id)
    if str(project.kind or "user") == "inbox":
        raise ws.WorkspaceScopeError(ws.CODE_PROJECT_RESERVED,
                                     "默认项目不可删除", project_id)
    if str(project.status) == "deleted":
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "project already deleted", project_id)
    if str(confirm_name or "") != str(project.name or ""):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "confirm_name does not match project name", project_id)
    svc._check_row_version(project, expected_row_version,
                           code=ws.CODE_PROJECT_STALE, what="project")

    task_ids = await _task_ids_of_project(db, user_id=user_id, project_id=project.id)
    if await _active_run_for_tasks(db, user_id=user_id, task_ids=task_ids):
        raise ws.WorkspaceScopeError(ws.CODE_RUN_ACTIVE,
                                     "park the active run before purging", project_id)
    if await _active_delegation_for_conversations(db, user_id=user_id, task_ids=task_ids):
        raise ws.WorkspaceScopeError(ws.CODE_RUN_ACTIVE,
                                     "cancel active delegated tasks before purging", project_id)

    counts = await cascade_counts(db, user_id=user_id, task_ids=task_ids)
    conv_counts = await _handle_conversations(db, user_id=user_id,
                                              task_ids=task_ids, mode=conversations)
    notes_unbound = await _unbind_notes(db, user_id=user_id, task_ids=task_ids)

    plan: list[FilePlan] = []
    for tid in task_ids:
        task = await svc.get_task(db, user_id=user_id, task_id=tid, for_update=True)
        if task is not None:
            plan.append(FilePlan("trash", task.root_rel, "task", task.id))
            task.status = "deleted"
            task.deleted_at = datetime.utcnow()
            task.deleted_reason = "project"
            task.updated_at = datetime.utcnow()
            svc._bump_row_version(task)
    await db.flush()
    plan.append(FilePlan("trash", project.root_rel, "project", project.id))

    project.status = "deleted"
    project.deleted_at = datetime.utcnow()
    project.updated_at = datetime.utcnow()
    svc._bump_row_version(project)
    await db.flush()
    await svc._refresh_project_projection(project)
    return {
        "id": project.id, "status": project.status, "mode": "purge",
        "tasks": len(task_ids),
        "conversations_unbound": conv_counts["conversations_unbound"],
        "conversations_deleted": conv_counts["conversations_deleted"],
        "notes_unbound": notes_unbound,
        "counts": counts,
        "file_plan": plan,
    }


# --- 回收站 -------------------------------------------------------------------

async def list_trash(db, *, user_id: str, limit: int = 200) -> dict[str, Any]:
    """回收站列表（评审 I6）：purge 后客户端刷新仍能拿到 id 去 restore。"""
    projects = (await db.execute(
        select(WorkspaceProject)
        .where(WorkspaceProject.user_id == user_id, WorkspaceProject.status == "deleted")
        .order_by(WorkspaceProject.deleted_at.desc().nullslast())
        .limit(limit)
    )).scalars().all()
    tasks = (await db.execute(
        select(WorkspaceTask)
        .where(WorkspaceTask.user_id == user_id, WorkspaceTask.status == "deleted")
        .order_by(WorkspaceTask.deleted_at.desc().nullslast())
        .limit(limit)
    )).scalars().all()
    now = datetime.utcnow()

    def _row(row, kind: str) -> dict[str, Any]:
        expires = (row.deleted_at + timedelta(days=TRASH_RETENTION_DAYS)
                   if row.deleted_at else None)
        return {"kind": kind, "id": row.id,
                "name": getattr(row, "name", None) or getattr(row, "title", None),
                "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
                "expires_at": expires.isoformat() if expires else None,
                "expired": bool(expires and expires <= now)}

    return {"projects": [_row(p, "project") for p in projects],
            "tasks": [_row(t, "task") for t in tasks]}


async def purge_expired_trash(db, *, user_id: str | None = None,
                              now: datetime | None = None) -> list[dict[str, Any]]:
    """物理删除超过保留期的回收站条目（7 天）。返回被清理的对象清单。

    由 `scripts/purge_expired_trash.py`（dry-run 默认）或运维定时任务调用；
    服务层不自行调度（避免与请求事务竞争）。
    """
    cutoff = (now or datetime.utcnow()) - timedelta(days=TRASH_RETENTION_DAYS)
    stmt = select(WorkspaceTask).where(WorkspaceTask.status == "deleted",
                                       WorkspaceTask.deleted_at.isnot(None),
                                       WorkspaceTask.deleted_at <= cutoff)
    if user_id:
        stmt = stmt.where(WorkspaceTask.user_id == user_id)
    tasks = (await db.execute(stmt)).scalars().all()
    out: list[dict[str, Any]] = []
    for task in tasks:
        out.append({"kind": "task", "id": task.id, "user_id": task.user_id,
                    "root_rel": task.root_rel})
        await db.delete(task)
    pstmt = select(WorkspaceProject).where(WorkspaceProject.status == "deleted",
                                           WorkspaceProject.deleted_at.isnot(None),
                                           WorkspaceProject.deleted_at <= cutoff)
    if user_id:
        pstmt = pstmt.where(WorkspaceProject.user_id == user_id)
    for project in (await db.execute(pstmt)).scalars().all():
        out.append({"kind": "project", "id": project.id, "user_id": project.user_id,
                    "root_rel": project.root_rel})
        await db.delete(project)
    await db.flush()
    return out


__all__ = [
    "CONVERSATION_MODES", "FilePlan", "TRASH_DIR", "TRASH_RETENTION_DAYS",
    "apply_file_plan", "apply_move_plan", "archive_project", "archive_task", "cascade_counts",
    "list_trash", "migrate_orphaned_task_files", "preview_project_delete", "preview_task_delete",
    "purge_expired_trash", "purge_project", "purge_task", "remaining_conversation_count",
    "restore_project", "restore_task",
]
