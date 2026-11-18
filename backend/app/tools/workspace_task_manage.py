# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`workspace_task_manage` — agent 侧任务 CRUD（与用户 UI 同权同限，§4.12）。

动作：`create` / `update`（title/description/status）/ `archive` / `list`。
约束：owner 校验 + CAS（`expected_row_version`）；`purge` 不在工具面。
"""
from __future__ import annotations

import json
import logging
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


def _available() -> bool:
    try:
        return svs.scoping_mode() in ("shadow", "enforce")
    except Exception:  # noqa: BLE001
        return False


async def _user_root(db, user):
    from pathlib import Path

    from app.services.workspace_service import ensure_user_workspace

    workspace = await ensure_user_workspace(db, user.id, user.username)
    if workspace is None:
        raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING, "workspace not initialized", "")
    return Path(workspace.root_path).resolve()


async def workspace_task_manage(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return _error("缺少会话上下文", "workspace_scope_missing")
    from app.services import workspace_task_lifecycle_service as life
    from app.services import workspace_task_service as svc

    action = str(args.get("action") or "").strip().lower()
    try:
        if action == "list":
            tasks = await svc.list_tasks(
                db, user_id=user.id, project_id=args.get("project_id"),
                statuses=tuple(args.get("statuses") or ("active", "completed", "archived")),
                limit=int(args.get("limit") or 50), search=args.get("search"))
            return json.dumps({"ok": True, "success": True, "tasks": [
                {"id": t.id, "project_id": t.project_id, "title": t.title,
                 "description": t.description, "origin": t.origin, "status": t.status,
                 "row_version": t.row_version}
                for t in tasks]}, ensure_ascii=False)
        if action == "create":
            root = await _user_root(db, user)
            project_id = str(args.get("project_id") or "").strip()
            if not project_id:
                project = await svc.ensure_inbox_project(db, user_id=user.id, user_root=root)
                project_id = project.id
            task = await svc.create_task(
                db, user_id=user.id, user_root=root, project_id=project_id,
                title=str(args.get("title") or ""), goal=str(args.get("goal") or ""),
                description=args.get("description"), origin="agent")
            await db.commit()
            return json.dumps({"ok": True, "success": True, "task": {
                "id": task.id, "project_id": task.project_id, "title": task.title,
                "description": task.description, "origin": task.origin,
                "row_version": task.row_version}}, ensure_ascii=False)
        if action == "update":
            task = await svc.update_task(
                db, user_id=user.id, task_id=str(args.get("task_id") or ""),
                title=args.get("title"), description=args.get("description"),
                status=args.get("status"),
                expected_row_version=args.get("expected_row_version"))
            await db.commit()
            return json.dumps({"ok": True, "success": True, "task": {
                "id": task.id, "title": task.title, "description": task.description,
                "status": task.status, "row_version": task.row_version}}, ensure_ascii=False)
        if action == "archive":
            task = await life.archive_task(
                db, user_id=user.id, task_id=str(args.get("task_id") or ""),
                expected_row_version=args.get("expected_row_version"))
            await db.commit()
            return json.dumps({"ok": True, "success": True, "task": {
                "id": task.id, "status": task.status}}, ensure_ascii=False)
        return _error("action 必须是 create/update/archive/list（purge 仅用户 UI）",
                      "workspace_invalid_args")
    except ws.WorkspaceScopeError as exc:
        return _error(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败不掩盖原始错误
            pass
        logger.exception("workspace_task_manage failed")
        return _error(f"任务操作失败（{type(exc).__name__}）", "workspace_task_failed")


registry.register(
    name="workspace_task_manage",
    toolset="files",
    schema={
        "name": "workspace_task_manage",
        "description": (
            "管理任务（Task）：list / create / update / archive。"
            "update 可改 title/description/status，需带 expected_row_version（CAS）。"
            "create 不传 project_id 时落入默认项目。彻底删除（purge）只能由用户在 UI 触发。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "archive"]},
                "task_id": {"type": "string"},
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "goal": {"type": "string"},
                "description": {"type": "string", "description": "任务备注（Markdown 纯文本）"},
                "status": {"type": "string",
                           "enum": ["active", "completed", "archived"]},
                "expected_row_version": {"type": "integer", "description": "CAS 版本"},
                "statuses": {"type": "array", "items": {"type": "string"}},
                "search": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
    handler=workspace_task_manage,
    is_async=True,
    check_fn=_available,
    description="Manage workspace tasks",
    emoji="",
    permission_key="workspace_write",
)

__all__ = ["workspace_task_manage"]
