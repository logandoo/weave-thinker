# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Workspace file service — directory listing + server-side folder zip.

Used by ``/api/files/list`` and ``/api/files/zip`` (2026-09-19). All path
resolution goes through :mod:`app.services.workspace_paths`, so callers can
only ever address the requesting user's workspace. Symlinked entries are
excluded from listings and archives (defense in depth — an escaping link
never becomes downloadable), and every archived member is re-checked for
workspace containment before it is written into the zip.
"""
from __future__ import annotations

import json
import logging
import os
import stat as stat_module
import zipfile
from pathlib import Path
from typing import Any, Dict

from app.services.workspace_paths import (
    CODE_NOT_FOUND,
    WorkspacePathError,
    is_within,
    resolve_workspace_dir,
    to_rel_path,
)

logger = logging.getLogger(__name__)

# Upper bound for one directory listing; larger dirs are reported truncated.
MAX_DIRECTORY_ENTRIES = 1000


def _safe_arc_component(name: str) -> str:
    """Sanitize one archive path component (A4.9: extraction-side zip-slip).

    Replaces backslashes (valid POSIX filename chars that Windows extractors
    treat as separators) and NULs; normalizes dot components.
    """
    clean = name.replace("\\", "_").replace("\x00", "_")
    if clean in ("", ".", ".."):
        return "_"
    return clean


def _guess_type(name: str) -> str:
    # Local import keeps the tools package optional for pure service callers.
    from app.tools.provide_file import _guess_file_type

    return _guess_file_type(name)


def list_directory(root: str | Path, target: str) -> Dict[str, Any]:
    """List one directory inside *root*.

    Returns ``{"path": rel, "entries": [...], "truncated": bool}`` with
    dir-first then case-insensitive name ordering.
    """
    root_path = Path(root).resolve()
    directory = resolve_workspace_dir(target, root_path)
    rel_path = to_rel_path(directory, root_path)

    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise WorkspacePathError(CODE_NOT_FOUND, f"cannot read directory: {exc}", str(target)) from exc

    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))

    entries: list[Dict[str, Any]] = []
    truncated = False
    for child in children:
        if child.is_symlink():
            continue
        # 回收站是内部目录（评审 M5）：不出现在用户可见列表里
        if child.name == "_trash":
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        if child.is_dir():
            entry_type, is_dir = "folder", True
        elif child.is_file():
            entry_type, is_dir = _guess_type(child.name), False
        else:
            continue  # sockets/fifos/devices are not part of the user-visible tree
        if len(entries) >= MAX_DIRECTORY_ENTRIES:
            truncated = True
            break
        try:
            entry_rel = to_rel_path(child, root_path)
        except WorkspacePathError:
            continue
        entries.append({
            "name": child.name,
            "rel_path": entry_rel,
            "is_dir": is_dir,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "type": entry_type,
        })

    return {"path": rel_path, "entries": entries, "truncated": truncated}


def build_folder_zip(root: str | Path, target: str, dest: str | Path) -> Dict[str, Any]:
    """Zip the folder at *target* into *dest* (created/overwritten).

    The archive root is the folder's own name, so extracting it reproduces
    the folder. Symlinks and non-regular files are skipped; every member is
    re-validated for workspace containment before being written.
    Returns ``{"name", "size", "file_count"}``.
    """
    root_path = Path(root).resolve()
    directory = resolve_workspace_dir(target, root_path)
    root_name = _safe_arc_component(directory.name) or "workspace"
    dest_path = Path(dest)

    file_count = 0
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
            )
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                if full.is_symlink():
                    continue
                try:
                    resolved = full.resolve()
                except (OSError, RuntimeError):
                    continue
                if not is_within(root_path, resolved) or not resolved.is_file():
                    logger.warning("zip: skipped out-of-workspace member %s", full)
                    continue
                try:
                    relative = resolved.relative_to(directory)
                except ValueError:  # pragma: no cover - is_within guarantees this
                    continue
                safe_parts = [_safe_arc_component(part) for part in relative.parts]
                arcname = "/".join([root_name, *safe_parts])
                # Stream member bytes through a no-follow fd: the path was
                # validated above, but opening it again by name would leave a
                # check→open symlink race (A4.9 TOCTOU).
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    member_fd = os.open(str(resolved), flags)
                except OSError:
                    continue
                try:
                    if not stat_module.S_ISREG(os.fstat(member_fd).st_mode):
                        continue
                    with archive.open(arcname, "w") as member_out:
                        while True:
                            chunk = os.read(member_fd, 1024 * 1024)
                            if not chunk:
                                break
                            member_out.write(chunk)
                finally:
                    os.close(member_fd)
                file_count += 1

    return {
        "name": f"{root_name}.zip",
        "size": dest_path.stat().st_size if dest_path.exists() else 0,
        "file_count": file_count,
    }


# --- 本会话产出（2026-11-15 任务管理中心 · 评审 C3 修复）-----------------------
#
# manifest 由 `workspace_artifact_service.append_change` 写在 **attempt root**：
#   <task>/runs/r_<rid>/attempts/a_<aid>/.vibeweaver/workspace-change-manifest.jsonl
# 行字段：path（**用户根相对**）、operation、task_id、run_id、attempt_id…
# **没有 conversation_id**——必须 join `workspace_runs.conversation_id`。
#
# 返回「本会话产出」的扁平文件清单（相对任务 workspace 根），而不是过滤一层目录
# （一层目录只有 inputs/artifacts，过滤后必然为空）。

async def async_conversation_produced_files(db, *, user_id: str, task,
                                            task_root: Path | str,
                                            conversation_id: str,
                                            limit: int = 500) -> list[Dict[str, Any]]:
    """列「本会话产出」：manifest(run_id∈该会话的 runs) ∩ 现存文件。

    ⚠️ **2026-11-18 起无生产调用方**（会话面板与 agent 工具 caller 都改走
    :func:`async_conversation_produced_files_across_tasks`）：单任务 `task_id AND conversation_id`
    join 正是 60 事故的盲点——改绑 + `migrate_orphaned_task_files` 迁移后，产出仍在**旧任务**的
    runs 里，按当前任务 join 查不到。新代码请用跨任务函数（它给每条条目打真实落点归属，
    要按任务筛就自己过滤 `task_id`），别把本函数当规范视图接回去。
    """
    from sqlalchemy import select

    from app.db.database import WorkspaceRun

    run_ids = set((await db.execute(
        select(WorkspaceRun.id).where(WorkspaceRun.user_id == user_id,
                                      WorkspaceRun.task_id == task.id,
                                      WorkspaceRun.conversation_id == conversation_id)
    )).scalars().all())
    if not run_ids:
        return []
    return _collect_produced(Path(task_root), run_ids, limit=limit)


async def async_conversation_produced_files_across_tasks(
    db, *, user_id: str, user_root: Path | str, conversation_id: str,
    limit: int = 500, only_task_id: str | None = None,
) -> list[Dict[str, Any]]:
    """列「本会话产出」的**跨任务**视图（方案A Piece 1 · 会话连续性）。

    与 :func:`async_conversation_produced_files` 的区别：后者只 join 单一 task
    （任务级视图用，语义不变）；本函数按会话的**全部 runs**（不限当前绑定任务）
    分组到各自 task 根扫 manifest——所以会话改绑到别的任务后，它在旧任务里产出的
    文件仍然可见。这正是「改绑后我的文件不见了」痛点的读/视图侧修法。

    不搬文件、不改 owner、无安全边界变化（都是同一用户自己会话自己产出的东西）。
    每条带 task 归属（``task_id`` / ``task_title`` / ``display_root``），供前端与
    Piece 2 跨任务自由读解析到正确 inode；``rel_path`` 仍按**各自任务** workspace
    根相对（读/写同 inode 的前提，绝不 re-root 到用户根）。
    """
    from sqlalchemy import select

    from app.db.database import WorkspaceRun
    from app.services import workspace_scope as ws
    from app.services import workspace_task_service as task_svc

    root_path = Path(user_root)

    # 1. 该会话的所有 runs（跨任务）→ 按 task 分组 run_id
    #    （ORDER BY id 仅为稳定分组输入；输出确定性由下方全排序 + sort-then-slice 保证）
    rows = (await db.execute(
        select(WorkspaceRun.task_id, WorkspaceRun.id)
        .where(WorkspaceRun.user_id == user_id,
               WorkspaceRun.conversation_id == conversation_id)
        .order_by(WorkspaceRun.id)
    )).all()
    if not rows:
        return []
    run_ids_by_task: Dict[str, set] = {}
    for task_id, run_id in rows:
        run_ids_by_task.setdefault(str(task_id), set()).add(str(run_id))

    # 2. 每个 task 解析根 + 扫 manifest。跨任务合并**不**按 rel_path 全局去重——
    #    两任务同名文件是常态，必须各留一条（靠 task 归属区分）。
    # 活绑定任务根 = 改绑迁移后文件的**真实落点**（2026-11-18 60 事故 conv 389a2496）：
    # `migrate_orphaned_task_files` 把源任务 workspace 的文件物理搬进目标任务，但血缘 manifest
    # 仍留在源任务的 runs 里；源任务再被 purge 进 `_trash/` 后连 runs 目录都不在 `root_rel`
    # 上了。三层落空 → 用户看到「本会话产出 · 0」，以为文件全丢。这里把源任务 manifest 只当
    # **血缘证据**读，文件解析允许回落到活绑定根（只读视图，零搬移）。
    live_root, live_meta = await _live_binding_target(db, user_id=user_id, root=root_path,
                                                      conversation_id=conversation_id)
    # 重定向只在「本会话确实从该任务改绑到活任务」时开放——`workspace_binding_events` 是权威
    # 记录，也正是 `migrate_orphaned_task_files` 的触发场景。少了这道闸，活任务里**别人写的**
    # 同名文件会在「源任务文件被删」时被误认成本会话产出（评审 I1 实测复现）。
    migrated_from = await _rebind_sources(db, user_id=user_id, conversation_id=conversation_id,
                                          to_task_id=(live_meta or {}).get("task_id"))
    out: list[Dict[str, Any]] = []
    for task_id in sorted(run_ids_by_task):
        run_ids = run_ids_by_task[task_id]
        task = await task_svc.get_task(db, user_id=user_id, task_id=task_id)
        if task is None or not task.root_rel:
            continue
        task_root = _resolve_task_root(root_path, task)
        if task_root is None:
            logger.warning("produced-files-across-tasks: task %s root not found", task_id)
            continue
        # 回收站里的任务：manifest 可读（血缘证据），但它自己 workspace 里的文件**绝不外显**
        # ——既避免把已删除内容塞回活会话，也避免给出指向 `_trash/` 的下载路径。判据用
        # `status`（不是「目录是否真在 `_trash`」）：purge 移目录失败残留的原地任务同样不外显
        # （`test_conversation_produced_files_skips_deleted_task` 钉的正是这一形态）。
        deleted = str(task.status) == "deleted"
        redirect = None
        if (live_root is not None and live_meta["task_id"] != task_id
                and task_id in migrated_from
                # 与写侧同一把尺（`migrate_orphaned_task_files` 按 storage_authority 一致才搬）：
                # cloud 血缘不得解析到 device 权威的任务根。
                and str(task.storage_authority or "cloud") == live_meta["storage_authority"]):
            redirect = (live_root, live_meta)
        for entry in _collect_produced(task_root, run_ids, limit=limit, redirect=redirect,
                                       own_root_ok=not deleted):
            moved = entry.pop("moved_to", None)
            if moved:      # 身份整体跟着落点走（绝不 task_id 用落点、title 用源任务）
                entry["moved_from"] = {"task_id": task_id, "task_title": task.title}
                out.append({**entry, "task_id": moved["task_id"],
                            "task_title": moved["task_title"],
                            "display_root": f"{moved['root_rel']}/workspace"})
            else:
                out.append({**entry, "task_id": task_id, "task_title": task.title,
                            "display_root": f"{task.root_rel}/workspace"})
    # `only_task_id`（agent 工具按被询问任务问产出）必须在**截断之前**过滤：全局 `out[:limit]`
    # 按 (display_root, rel_path) 排序后切，任务 id 是 uuid、排序位置任意 → 先切后滤会把被询问
    # 任务的行**整批挤掉**，静默返回空/半份 = 本波要修的同一个 bug 换个阈值复发（评审 I1 实测）。
    if only_task_id is not None:
        out = [e for e in out if str(e.get("task_id") or "") == str(only_task_id)]
    # 确定性截断：先按 (display_root, rel_path) 全排序再切 limit，使「哪 limit 条
    # 胜出」与 DB 行序无关（改绑跨多任务时结果稳定）。重定向后同一物理文件可能被两条
    # manifest 命中（源任务血缘 + 目标任务自己的 manifest）→ 按落点身份去重。
    deduped: Dict[tuple, Dict[str, Any]] = {}
    for entry in out:
        deduped[(str(entry["display_root"]), str(entry["rel_path"]), str(entry["task_id"]))] = entry
    out = sorted(deduped.values(), key=lambda e: (str(e["display_root"]), str(e["rel_path"])))
    return out[:limit]


def _resolve_task_root(root: Path, task) -> Path | None:
    """任务根解析。**只有 `status=deleted` 的任务**才回落到 `_trash/task_<id>`（DB `root_rel`
    按设计不变，为的是 restore 能把目录搬回去）。活任务绝不解析到回收站目录——`restore` 的
    目录搬移是尽力而为（`_restore_from_trash` 失败时调用方不感知），此时若回落到 `_trash`
    就会把回收站字节挂在一个 404 的 `display_root` 上（评审 I2）。两个候选都不存在 → None。"""
    from app.services import workspace_scope as ws
    from app.services import workspace_task_lifecycle_service as life

    candidates = [task.root_rel]
    if str(task.status) == "deleted":
        candidates.append(f"{life.TRASH_DIR}/task_{task.id}")
    for rel in candidates:
        try:
            # 与 sibling caller 同一把尺：拒绝任何 symlink 重定向与越界（root_rel 服务端生成
            # + runs 已按 user_id 过滤，正常恒成立；异常即换下一个候选根）。
            p = ws._resolve_no_redirect(root, str(rel), what="task root")
        except ws.WorkspaceScopeError:
            continue
        if p.is_dir():
            return p
    return None


async def _rebind_sources(db, *, user_id: str, conversation_id: str,
                          to_task_id: str | None) -> set[str]:
    """本会话改绑事件链上、所有能**到达**当前活任务的源任务 id（沿 `from → to` 的传递闭包）。
    没有 `to_task_id`（会话无活绑定）→ 空集，调用方因此完全不做重定向。

    为什么必须传递闭包而不是只看直接前驱（2026-11-18 评审 m2 挖出的形状）：用户会反复改绑。
    A→B（迁移发生）再 B→C 后，`migrate_orphaned_task_files` 把文件又从 B 搬到 C，而血缘
    runs/manifest 始终留在 A；事件表里 `to=C` 的直接前驱只有 B ⇒ 只看一跳则 A ∉ 集合
    → 产出列表**再次清零**，正是本波要修的同一个症状。链上任意跳数都连通，环由 seen 守住。
    """
    from sqlalchemy import select

    from app.db.database import WorkspaceBindingEvent

    if not to_task_id:
        return set()
    rows = (await db.execute(
        select(WorkspaceBindingEvent.from_task_id, WorkspaceBindingEvent.to_task_id)
        .where(WorkspaceBindingEvent.user_id == user_id,
               WorkspaceBindingEvent.conversation_id == conversation_id)
    )).all()
    preds: Dict[str, set] = {}
    for from_id, to_id in rows:
        if from_id and to_id:
            preds.setdefault(str(to_id), set()).add(str(from_id))
    live = str(to_task_id)
    seen: set[str] = set()
    stack = [live]
    while stack:
        for src in preds.get(stack.pop(), ()):
            if src not in seen:
                seen.add(src)
                stack.append(src)
    seen.discard(live)          # 自己不算源（调用方另有 task_id != live 的判据，这里再保一层）
    return seen


async def _live_binding_target(db, *, user_id: str, root: Path,
                               conversation_id: str) -> tuple[Path | None, Dict[str, Any] | None]:
    """会话**当前绑定**的活任务根（= 改绑迁移文件的落点）。无绑定 / 绑定已删 / 路径异常 →
    `(None, None)`（调用方因此不会把回收站内容外显）。"""
    from sqlalchemy import select

    from app.db.database import WorkspaceBinding
    from app.services import workspace_scope as ws
    from app.services import workspace_task_service as task_svc

    task_id = (await db.execute(
        select(WorkspaceBinding.task_id)
        .where(WorkspaceBinding.user_id == user_id,
               WorkspaceBinding.conversation_id == conversation_id)
    )).scalar_one_or_none()
    if not task_id:
        return None, None
    task = await task_svc.get_task(db, user_id=user_id, task_id=str(task_id))
    if task is None or not task.root_rel or str(task.status) == "deleted":
        return None, None
    try:
        return ws._resolve_no_redirect(root, task.root_rel, what="binding root"), {
            "task_id": str(task.id), "task_title": task.title, "root_rel": task.root_rel,
            "storage_authority": str(task.storage_authority or "cloud")}
    except ws.WorkspaceScopeError:
        return None, None


def _collect_produced(task_root: Path, run_ids: set[str], *, limit: int = 500,
                      redirect: tuple[Path, Dict[str, Any]] | None = None,
                      own_root_ok: bool = True) -> list[Dict[str, Any]]:
    """从 attempt 目录下的 manifest 收集指定 run 的产出（相对任务 workspace 根）。

    `own_root_ok=False`：本任务在回收站，它自己 workspace 里的字节不外显（只认重定向落点）。
    `redirect=(活任务根, 活任务身份)`：manifest 记的路径在本任务根下已不存在时，允许在**该
    会话当前绑定的活任务**根下按同一 workspace 相对路径再找一次——`migrate_orphaned_task_files`
    只搬文件不搬血缘，这一跳把「文件被迁走 → 产出列表空」接回来。命中时条目带 `moved_to`
    （调用方据此把 task 归属改成真实落点，绝不给出 `_trash` 路径）。
    两条分支都过 `is_within` 包含性检查（manifest 可被 agent 写，`..` 不得借道越出任务 workspace）。
    """
    runs_dir = Path(task_root) / "runs"
    if not runs_dir.is_dir():
        return []
    redirect_root = redirect[0] if redirect else None
    own_ws_root = Path(task_root) / "workspace"
    seen: dict[str, Dict[str, Any]] = {}
    for manifest in runs_dir.glob("r_*/attempts/a_*/.vibeweaver/workspace-change-manifest.jsonl"):
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("run_id") or "") not in run_ids:
                continue
            rel_user = str(row.get("path") or "")
            if not rel_user:
                continue
            # 用户根相对 → 任务 workspace 相对（只保留 workspace/ 下的文件）
            marker = "/workspace/"
            idx = rel_user.find(marker)
            if idx < 0:
                continue
            rel_ws = rel_user[idx + len(marker):]
            tail = Path(*rel_ws.split("/"))
            # manifest 是**磁盘数据**（agent 有终端就能往自己 attempt 的 `.vibeweaver/` 里写行），
            # 所以两条解析分支都要包含性检查：解析结果必须落在各自任务根的 `workspace/` 里，
            # `..` 借道 `_trash/` 或别的任务一律拒（评审 C1：own-root 分支此前无检查，
            # 一条 manifest 就能把已删任务/他人任务的字节配上可用的下载 URL）。
            own = own_ws_root / tail
            abs_path, moved_to = None, None
            if own_root_ok and is_within(own_ws_root, own) and own.is_file():
                abs_path = own
            elif redirect_root is not None:
                cand = redirect_root / "workspace" / tail
                if is_within(redirect_root / "workspace", cand) and cand.is_file():
                    abs_path, moved_to = cand, redirect[1]
            if abs_path is None:
                continue
            try:
                st = abs_path.stat()
            except OSError:
                continue
            entry = {"name": abs_path.name, "rel_path": rel_ws,
                     "is_dir": False, "size": st.st_size, "mtime": st.st_mtime,
                     "type": _guess_type(abs_path.name)}
            if moved_to:
                entry["moved_to"] = moved_to
            seen[rel_ws] = entry
        if len(seen) >= limit:      # 跨 manifest 的硬上限（旧代码只 break 内层，可超 limit）
            break
    return sorted(seen.values(), key=lambda e: str(e["rel_path"]))
