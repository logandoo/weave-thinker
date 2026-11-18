# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""笔记 ↔ 任务绑定服务（2026-11-15 任务管理中心 · DESIGN_20261115 §4.9；
2026-11-16 Wave B 改为**多对多**）。

绑定是**关系**：一条笔记可以同时属于多个任务（进而属于多个项目）；绑定不复制内容，
解绑不删除笔记；任务删除时笔记只解绑（由 `workspace_task_lifecycle_service` 执行）。

权威面 = `note_task_bindings`（唯一键 `(note_id, task_id)`）。
`notes.task_id` 自 Wave B 起是**镜像列**：始终等于「最早那条绑定」（`bound_at` 升序，
同值按 `id` 升序，无绑定则 NULL），`notes.task_bound_at` 同步为该绑定的 `bound_at`。
镜像保留的原因：既有读者（overview 计数 / 同步面 / agent 工具出参 / Wave A 契约）
都读这一列，且 Wave A 锁定验收要求「任务 purge 后 `notes.task_id` 置 NULL」——
单绑场景下镜像回落天然就是 NULL，语义不变。

`note_task_bindings` 与 `notes.task_id` 都是逻辑 FK（无物理 FK，避免同步冲突）；
owner 校验一律走 `notes.notebook_id → notebooks.user_id`。
镜像写入必须逐行 ORM 赋值（`notes` 是 SYNC_ENTITIES 成员，Core bulk UPDATE 不触发
mapper 事件 = 同步泄漏，见 `workspace_task_lifecycle_service._unbind_notes` 注释）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.db.database import (
    Note,
    Notebook,
    NoteTaskBinding,
    WorkspaceProject,
    WorkspaceTask,
)
from app.services import workspace_scope as ws
from app.services import workspace_task_service as svc

logger = logging.getLogger(__name__)

__all__ = [
    "get_note",
    "bind_note",
    "add_binding",
    "remove_binding",
    "clear_bindings",
    "batch_bind",
    "batch_unbind",
    "list_task_notes",
    "list_task_note_rows",
    "binding_counts_for_notes",
    "tasks_for_note",
    "unbind_tasks_notes",
    "task_note_counts",
]


# --- 基础查询 ----------------------------------------------------------------

async def get_note(db, *, user_id: str, note_id: str) -> Note | None:
    """按 owner 取笔记（join notebook）。"""
    res = await db.execute(
        select(Note)
        .join(Notebook, Notebook.id == Note.notebook_id)
        .where(Note.id == note_id, Notebook.user_id == user_id)
    )
    return res.scalar_one_or_none()


def _earliest(rows: list[NoteTaskBinding]) -> NoteTaskBinding | None:
    """镜像口径：bound_at 最早优先，同值按 id 升序（可判定、与插入顺序无关）。"""
    if not rows:
        return None
    return min(rows, key=lambda r: (r.bound_at is None, r.bound_at or datetime.min, str(r.id)))


async def _note_bindings(db, *, user_id: str, note_id: str) -> list[NoteTaskBinding]:
    res = await db.execute(
        select(NoteTaskBinding)
        .where(NoteTaskBinding.user_id == user_id, NoteTaskBinding.note_id == note_id)
        .order_by(NoteTaskBinding.bound_at.asc().nullslast(), NoteTaskBinding.id.asc())
    )
    return list(res.scalars().all())


async def _sync_mirror(db, *, user_id: str, note: Note) -> list[NoteTaskBinding]:
    """把 `notes.task_id/task_bound_at` 对齐到权威关系表（逐行 ORM 赋值）。"""
    rows = await _note_bindings(db, user_id=user_id, note_id=note.id)
    primary = _earliest(rows)
    note.task_id = str(primary.task_id) if primary else None
    note.task_bound_at = primary.bound_at if primary else None
    note.updated_at = datetime.utcnow()
    await db.flush()
    return rows


async def _assert_bindable_task(db, *, user_id: str, task_id: str) -> WorkspaceTask:
    task = await svc.get_task(db, user_id=user_id, task_id=str(task_id))
    if task is None:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "task not found for this user", str(task_id))
    if str(task.status) == "deleted":
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "cannot bind note to a deleted task", str(task_id))
    return task


# --- 写面 --------------------------------------------------------------------

async def add_binding(db, *, user_id: str, note_id: str,
                      task_id: str) -> tuple[NoteTaskBinding, bool]:
    """加一条绑定（幂等）。返回 `(绑定行, 是否新建)`。"""
    note = await get_note(db, user_id=user_id, note_id=note_id)
    if note is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "note not found for user", note_id)
    task = await _assert_bindable_task(db, user_id=user_id, task_id=task_id)

    res = await db.execute(
        select(NoteTaskBinding)
        .where(NoteTaskBinding.note_id == note.id, NoteTaskBinding.task_id == task.id)
        .limit(1)
    )
    row = res.scalar_one_or_none()
    created = row is None
    if row is None:
        try:
            # 并发双绑撞唯一键：只回滚到 savepoint（批量绑定里不能把整笔事务掀掉），
            # 然后把冲突当作幂等命中重查（评审 m10）。
            async with db.begin_nested():
                db.add(NoteTaskBinding(note_id=note.id, task_id=task.id, user_id=user_id,
                                       bound_at=datetime.utcnow()))
                await db.flush()
        except IntegrityError:
            created = False
        row = (await db.execute(
            select(NoteTaskBinding)
            .where(NoteTaskBinding.note_id == note.id, NoteTaskBinding.task_id == task.id)
            .limit(1)
        )).scalar_one_or_none()
        if row is None:
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                         "note binding failed unexpectedly", note_id)
    await _sync_mirror(db, user_id=user_id, note=note)
    return row, created


async def remove_binding(db, *, user_id: str, note_id: str, task_id: str) -> int:
    """解一条绑定（笔记不删）。返回删除行数。"""
    note = await get_note(db, user_id=user_id, note_id=note_id)
    if note is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "note not found for user", note_id)
    res = await db.execute(
        delete(NoteTaskBinding)
        .where(NoteTaskBinding.note_id == note.id,
               NoteTaskBinding.task_id == str(task_id),
               NoteTaskBinding.user_id == user_id)
    )
    await _sync_mirror(db, user_id=user_id, note=note)
    return int(res.rowcount or 0)


async def clear_bindings(db, *, user_id: str, note_id: str) -> int:
    """清空一条笔记的全部绑定（= 旧「解绑」语义）。返回删除行数。"""
    note = await get_note(db, user_id=user_id, note_id=note_id)
    if note is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "note not found for user", note_id)
    res = await db.execute(
        delete(NoteTaskBinding).where(NoteTaskBinding.note_id == note.id,
                                      NoteTaskBinding.user_id == user_id)
    )
    await _sync_mirror(db, user_id=user_id, note=note)
    return int(res.rowcount or 0)


async def bind_note(db, *, user_id: str, note_id: str,
                    task_id: str | None) -> Note:
    """兼容门面（Wave A 契约不变）：`task_id=None` = 清空全部绑定；非空 = **加绑**（幂等，
    不影响该笔记在其他任务上的绑定）。多绑/批量请用 `batch_bind` / `add_binding`。"""
    if task_id is None:
        note = await get_note(db, user_id=user_id, note_id=note_id)
        if note is None:
            raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "note not found for user", note_id)
        await clear_bindings(db, user_id=user_id, note_id=note_id)
        return await get_note(db, user_id=user_id, note_id=note_id)
    await add_binding(db, user_id=user_id, note_id=note_id, task_id=task_id)
    return await get_note(db, user_id=user_id, note_id=note_id)


def _dedupe_ids(raw_ids) -> list[str]:
    """去重且保序（同一 note_id 在一次批量里只处理一次，否则返回计数会虚高）。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_ids or []:
        nid = str(raw or "").strip()
        if not nid or nid in seen:
            continue
        seen.add(nid)
        out.append(nid)
    return out


async def batch_bind(db, *, user_id: str, task_id: str,
                     note_ids: list[str]) -> dict:
    """把多条笔记一次绑到同一任务（任务管理中心多选批量）。逐条校验 owner。"""
    bound: list[str] = []
    skipped: list[str] = []
    failed: list[dict] = []
    for nid in _dedupe_ids(note_ids):
        try:
            _, created = await add_binding(db, user_id=user_id, note_id=nid, task_id=task_id)
        except ws.WorkspaceScopeError as exc:
            if exc.code == ws.CODE_FOREIGN_SCOPE:
                failed.append({"note_id": nid, "code": exc.code, "error": str(exc)})
                continue
            raise
        (bound if created else skipped).append(nid)
    return {"task_id": str(task_id), "bound": bound, "already_bound": skipped, "failed": failed}


async def batch_unbind(db, *, user_id: str, task_id: str,
                       note_ids: list[str]) -> dict:
    """从任务上解绑多条笔记（笔记不删）。

    与 `batch_bind` 对称（评审 M3）：他人/不存在的 note_id 记进 `failed`，
    不让一条坏 id 把整批打成 500。
    """
    unbound: list[str] = []
    failed: list[dict] = []
    for nid in _dedupe_ids(note_ids):
        try:
            if await remove_binding(db, user_id=user_id, note_id=nid, task_id=task_id):
                unbound.append(nid)
        except ws.WorkspaceScopeError as exc:
            if exc.code == ws.CODE_FOREIGN_SCOPE:
                failed.append({"note_id": nid, "code": exc.code, "error": str(exc)})
                continue
            raise
    return {"task_id": str(task_id), "unbound": unbound, "failed": failed}


async def unbind_tasks_notes(db, *, user_id: str, task_ids: list[str]) -> int:
    """任务/项目删除：解掉这些任务上的全部笔记绑定并回写镜像。返回受影响笔记数。

    逐行 ORM 赋值镜像（同步面要求，见模块 docstring）。
    """
    if not task_ids:
        return 0
    rows = (await db.execute(
        select(NoteTaskBinding)
        .where(NoteTaskBinding.user_id == user_id, NoteTaskBinding.task_id.in_(task_ids))
    )).scalars().all()
    note_ids = sorted({str(r.note_id) for r in rows})
    if not note_ids:
        return 0
    await db.execute(
        delete(NoteTaskBinding)
        .where(NoteTaskBinding.user_id == user_id, NoteTaskBinding.task_id.in_(task_ids))
    )
    notes = (await db.execute(select(Note).where(Note.id.in_(note_ids)))).scalars().all()
    for note in notes:
        await _sync_mirror(db, user_id=user_id, note=note)
    return len(notes)


# --- 读面 --------------------------------------------------------------------

async def list_task_notes(db, *, user_id: str, task_id: str,
                          limit: int = 200) -> list[Note]:
    res = await db.execute(
        select(Note)
        .join(NoteTaskBinding, NoteTaskBinding.note_id == Note.id)
        .join(Notebook, Notebook.id == Note.notebook_id)
        .where(NoteTaskBinding.task_id == task_id,
               NoteTaskBinding.user_id == user_id,
               Notebook.user_id == user_id)
        .order_by(Note.updated_at.desc().nullslast(), Note.created_at.desc())
        .limit(max(1, int(limit)))
    )
    return list(res.scalars().all())


async def list_task_note_rows(db, *, user_id: str, task_id: str,
                              limit: int = 200) -> list[dict]:
    """任务详情「笔记」tab 的行数据：笔记 + 该笔记的全部绑定（多绑可见）。

    绑定口径统一排除已删除任务（与 `tasks_for_note` 一致，评审 m4）：
    徽标说「共绑 3 个任务」而明细只有 2 条，是会在 UI 上自相矛盾的。
    """
    notes = await list_task_notes(db, user_id=user_id, task_id=task_id, limit=limit)
    if not notes:
        return []
    note_ids = [n.id for n in notes]
    live = _live_task_join()
    binds = (await db.execute(
        select(NoteTaskBinding.note_id, NoteTaskBinding.task_id, NoteTaskBinding.bound_at)
        .join(WorkspaceTask, WorkspaceTask.id == NoteTaskBinding.task_id)
        .where(NoteTaskBinding.user_id == user_id, NoteTaskBinding.note_id.in_(note_ids),
               live)
        .order_by(NoteTaskBinding.bound_at.asc().nullslast(), NoteTaskBinding.id.asc())
    )).all()
    by_note: dict[str, list] = {}
    for nid, tid, bound_at in binds:
        by_note.setdefault(str(nid), []).append((str(tid), bound_at))
    rows = []
    for n in notes:
        items = by_note.get(n.id, [])
        rows.append({
            "id": n.id,
            "title": n.title,
            "notebook_id": n.notebook_id,
            "task_id": n.task_id,                       # 镜像（= 最早绑定）
            "task_bound_at": n.task_bound_at.isoformat() if n.task_bound_at else None,
            "updated_at": n.updated_at.isoformat() if n.updated_at else None,
            "excerpt": (n.content or "")[:200],
            "bound_task_ids": [t for t, _ in items],
            "bound_task_count": len(items),
        })
    return rows


def _live_task_join():
    """绑定计数/明细的统一口径：已 purge（status='deleted'）的任务不算一次绑定。"""
    return WorkspaceTask.status != "deleted"


async def binding_counts_for_notes(db, *, user_id: str,
                                   note_ids: list[str]) -> dict[str, int]:
    if not note_ids:
        return {}
    res = await db.execute(
        select(NoteTaskBinding.note_id, func.count(NoteTaskBinding.id))
        .join(WorkspaceTask, WorkspaceTask.id == NoteTaskBinding.task_id)
        .where(NoteTaskBinding.user_id == user_id, NoteTaskBinding.note_id.in_(note_ids),
               _live_task_join())
        .group_by(NoteTaskBinding.note_id)
    )
    return {str(nid): int(cnt) for nid, cnt in res.all()}


async def tasks_for_note(db, *, user_id: str, note_id: str) -> list[dict]:
    """一条笔记绑了哪些任务/项目（笔记卡片与选择器徽标用）。"""
    res = await db.execute(
        select(WorkspaceTask, WorkspaceProject.name, NoteTaskBinding.bound_at)
        .join(WorkspaceProject, WorkspaceProject.id == WorkspaceTask.project_id)
        .join(NoteTaskBinding, NoteTaskBinding.task_id == WorkspaceTask.id)
        .where(NoteTaskBinding.user_id == user_id, NoteTaskBinding.note_id == note_id,
               WorkspaceTask.status != "deleted")
        .order_by(NoteTaskBinding.bound_at.asc().nullslast(), NoteTaskBinding.id.asc())
    )
    out = []
    for task, project_name, bound_at in res.all():
        out.append({
            "task_id": str(task.id),
            "title": task.title,
            "status": str(task.status),
            "project_id": str(task.project_id),
            "project_name": project_name,
            "bound_at": bound_at.isoformat() if bound_at else None,
        })
    return out


async def task_note_counts(db, *, user_id: str, task_ids: list[str]) -> dict[str, int]:
    """每任务笔记数（overview 用；多对多口径 = 去重后的笔记数）。

    归属直接看关系行上的 `user_id`（写入时由 owner 校验保证），不再绕 `notebooks`。
    """
    if not task_ids:
        return {}
    res = await db.execute(
        select(NoteTaskBinding.task_id, func.count(func.distinct(NoteTaskBinding.note_id)))
        .where(NoteTaskBinding.user_id == user_id, NoteTaskBinding.task_id.in_(task_ids))
        .group_by(NoteTaskBinding.task_id)
    )
    return {str(tid): int(cnt) for tid, cnt in res.all()}
