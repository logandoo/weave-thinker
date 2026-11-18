# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`workspace_bind_task` — 用户显式要求「放入 xx 项目 / 归类到 xx 任务」时，
把**当前会话**改绑到目标任务（2026-11-14 v2）。

硬约束：
* 只能改绑当前会话（conversation 来自 dispatch kwargs，模型不可指定别的会话）；
* 目标解析：`task_id` 直取 / `task_title` 在用户任务中**唯一**匹配 /
  `project_name` → 该项目下最近活跃任务；歧义一律结构化拒绝（不猜）；
* owner 校验 + 归档拒绝 + CAS（当前 binding_version）+ 活跃 run 守卫
  （与面板改绑同一 `task_binding_service.rebind`）；
* 不追溯改写历史消息来源（§6.1 不变量）；**若改绑后源任务再无其他会话，源任务 workspace
  文件自动迁入目标任务**（2026-11-18 issue4；源任务仍被共享时文件不动）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.services import workspace_scope as ws
from app.services import workspace_scope_service as svs
from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _error(message: str, code: str | None = None) -> str:
    payload: dict[str, Any] = {"ok": False, "error": message, "success": False}
    if code:
        payload["code"] = code
    return json.dumps(payload, ensure_ascii=False)


def _bind_available() -> bool:
    """off 档没有绑定身份：不把注定失败的工具塞给模型。"""
    try:
        return svs.scoping_mode() in ("shadow", "enforce")
    except Exception:  # noqa: BLE001 - 配置不可读时不暴露
        return False


async def _resolve_target(db, *, user_id: str, args: dict):
    from app.services import workspace_task_service as svc

    task_id = str(args.get("task_id") or "").strip()
    task_title = str(args.get("task_title") or "").strip()
    project_name = str(args.get("project_name") or "").strip()

    if task_id:
        return await svc.get_task(db, user_id=user_id, task_id=task_id), None
    if task_title:
        # 先查可用任务（评审 r2 Minor：同名 active+archived 并存时不得制造歧义）
        tasks = await svc.list_tasks(db, user_id=user_id,
                                     statuses=("active", "completed"), limit=200)
        matches = [t for t in tasks if str(t.title or "").strip() == task_title]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, _error(
                f"有 {len(matches)} 个同名任务「{task_title}」，请提供 task_id",
                "workspace_task_ambiguous")
        archived = await svc.list_tasks(db, user_id=user_id,
                                        statuses=("archived", "deleted"), limit=200)
        if any(str(t.title or "").strip() == task_title for t in archived):
            return None, _error("目标任务已归档/已删除，不能改绑", "workspace_relation_invalid")
        return None, _error(f"没有名为「{task_title}」的任务", "workspace_relation_invalid")
    if project_name:
        projects = await svc.list_projects(db, user_id=user_id, statuses=("active",), limit=200)
        matches = [p for p in projects if str(p.name or "").strip() == project_name]
        if len(matches) != 1:
            return None, _error(
                f"项目「{project_name}」不存在或不唯一，请提供 task_id / task_title",
                "workspace_relation_invalid")
        tasks = await svc.list_tasks(db, user_id=user_id, project_id=matches[0].id,
                                     statuses=("active", "completed"), limit=50)
        if not tasks:
            return None, _error(f"项目「{project_name}」下没有可用任务",
                                "workspace_relation_invalid")
        return tasks[0], None
    return None, _error("需要 task_id / task_title / project_name 之一", "workspace_invalid_args")


async def workspace_bind_task(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    if db is None or user is None or conversation is None:
        return _error("缺少会话上下文", "workspace_scope_missing")
    conv_id = str(getattr(conversation, "id", "") or "")
    if not conv_id:
        return _error("缺少会话 id", "workspace_scope_missing")

    # 2026-11-14 v2（评审 C3）：本工具是第四个「身份制造面」——设备链尚未放开时
    # 绝不能为它创建绑定（绑定=作用域身份，之后每次工具调用都会被链门拒成死会话，
    # 2026-11-13 事故同类）。与 attach_chat_scope / file_upload 共用同一判定。
    try:
        from app.services.workspace_scoping_runtime import mints_identity_for_unsupported_chain

        if mints_identity_for_unsupported_chain(getattr(conversation, "execution_target", None)):
            return _error(
                "该会话在本地设备上运行，暂不支持任务作用域改绑（设备链未放开）",
                "workspace_scope_mode_unsupported")
    except Exception:  # noqa: BLE001 - 判定不可读时 fail closed
        return _error("作用域判定不可用，已拒绝改绑", "workspace_scope_mode_unsupported")

    from app.services import task_binding_service as tbs

    try:
        task, err = await _resolve_target(db, user_id=user.id, args=args)
        if err is not None:
            return err
        if task is None:
            return _error("任务不存在或不属于当前用户", "workspace_foreign_scope")
        if str(task.status or "active") in ("archived", "deleted"):
            return _error("目标任务已归档，不能改绑", "workspace_relation_invalid")

        current = await tbs.get_binding(db, user_id=user.id, conversation_id=conv_id)
        expected = int(current.binding_version) if current is not None else 0
        from_task_id = str(current.task_id) if current is not None else None
        binding = await tbs.rebind(db, user_id=user.id, conversation_id=conv_id,
                                   task_id=task.id, expected_binding_version=expected,
                                   actor="agent", reason="agent_instruction")
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        return _error(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001 - 结构化失败，不外漏内部栈
        logger.exception("workspace_bind_task failed (conversation=%s)", conv_id)
        return _error(f"改绑失败（{type(exc).__name__}），请稍后重试", "workspace_bind_failed")

    # 改绑成功后：源任务若变空（无其他会话共享），把它的 workspace 文件迁到目标任务
    # （2026-11-18 issue4）。best-effort，失败不影响改绑结果。
    migrated = 0
    try:
        from app.services import workspace_task_lifecycle_service as life
        from app.services.workspace_service import ensure_user_workspace

        workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
        if workspace is not None:
            mig = await life.migrate_orphaned_task_files(
                db, user_id=user.id, user_root=Path(workspace.root_path),
                from_task_id=from_task_id, to_task_id=task.id)
            migrated = int(mig.get("migrated", 0) or 0)
    except Exception:  # noqa: BLE001 - 文件迁移是改绑成功后的尽力而为副作用
        logger.warning("bind_tool orphan-file migration failed (conv=%s)", conv_id, exc_info=True)

    parked = int(getattr(binding, "_parked_active_runs", 0) or 0)
    if migrated:
        note = (f"后续消息与文件进入该任务；源任务已无其他会话，已把它的 {migrated} 个文件迁入本任务。"
                "历史消息来源不变。")
    else:
        note = "后续消息与文件进入该任务；旧任务若仍有其他会话则文件不动，历史消息来源不变。"
    if parked:
        note = (f"已自动停泊 {parked} 个进行中的执行（本轮执行域已封存，本轮不应再向旧任务写文件）。"
                + note)
    return json.dumps({
        "ok": True,
        "success": True,
        "task_id": binding.task_id,
        "project_id": binding.project_id,
        "binding_version": binding.binding_version,
        "parked_runs": parked,
        "migrated_files": migrated,
        "title": task.title,
        "note": note,
    }, ensure_ascii=False)


registry.register(
    name="workspace_bind_task",
    toolset="files",
    schema={
        "name": "workspace_bind_task",
        "description": (
            "把**当前会话**改绑到指定的项目任务（用户说「放入 xx 项目」「归类到 xx 任务」时调用）。"
            "目标三选一：task_id（最精确）/ task_title（同名任务必须唯一）/ project_name"
            "（取该项目最近活跃任务）。只切后续消息与文件的归属，不追溯改写历史。"
            "**若改绑后旧任务再无其他会话，旧任务的 workspace 文件会自动迁入新任务**；"
            "旧任务仍被其他会话共享时文件不动。活跃 run 会被自动停泊（不会因此失败）；归档任务不可作为目标。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "目标任务 id（优先）。"},
                "task_title": {"type": "string", "description": "目标任务标题（须唯一）。"},
                "project_name": {"type": "string", "description": "目标项目名（取最近活跃任务）。"},
            },
            "required": [],
        },
    },
    handler=workspace_bind_task,
    is_async=True,
    check_fn=_bind_available,
    description="Bind the current conversation to a project task",
    emoji="",
    permission_key="workspace_write",
)


__all__ = ["workspace_bind_task"]
