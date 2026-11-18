# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`workspace_project_manage` — agent 侧项目 CRUD（与用户 UI 同权同限，§4.12）。

动作：`create` / `update`（name/description/system_memory_ingest）/ `archive` / `list`。
约束：owner 校验 + CAS（`expected_row_version`）；默认项目（kind=inbox）禁 archive；
**`purge` 不在工具面**（用户 UI 专属，需输入对象名）。
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


async def workspace_project_manage(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return _error("缺少会话上下文", "workspace_scope_missing")
    from app.services import workspace_task_lifecycle_service as life
    from app.services import workspace_task_service as svc

    action = str(args.get("action") or "").strip().lower()
    try:
        if action == "list":
            projects = await svc.list_projects(
                db, user_id=user.id,
                statuses=tuple(args.get("statuses") or ("active", "archived")), limit=200)
            return json.dumps({"ok": True, "success": True, "projects": [
                {"id": p.id, "name": p.name, "description": p.description,
                 "kind": p.kind, "status": p.status, "row_version": p.row_version}
                for p in projects]}, ensure_ascii=False)
        if action == "create":
            root = await _user_root(db, user)
            project = await svc.create_project(
                db, user_id=user.id, user_root=root,
                name=str(args.get("name") or ""),
                system_memory_ingest=args.get("system_memory_ingest"),
                description=args.get("description"))
            await db.commit()
            return json.dumps({"ok": True, "success": True, "project": {
                "id": project.id, "name": project.name, "description": project.description,
                "row_version": project.row_version}}, ensure_ascii=False)
        if action == "update":
            project = await svc.update_project(
                db, user_id=user.id, project_id=str(args.get("project_id") or ""),
                name=args.get("name"), description=args.get("description"),
                system_memory_ingest=args.get("system_memory_ingest"),
                expected_row_version=args.get("expected_row_version"))
            await db.commit()
            return json.dumps({"ok": True, "success": True, "project": {
                "id": project.id, "name": project.name, "description": project.description,
                "row_version": project.row_version}}, ensure_ascii=False)
        if action == "archive":
            project = await life.archive_project(
                db, user_id=user.id, project_id=str(args.get("project_id") or ""),
                expected_row_version=args.get("expected_row_version"))
            await db.commit()
            return json.dumps({"ok": True, "success": True, "project": {
                "id": project.id, "status": project.status}}, ensure_ascii=False)
        return _error("action 必须是 create/update/archive/list（purge 仅用户 UI）",
                      "workspace_invalid_args")
    except ws.WorkspaceScopeError as exc:
        return _error(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败不掩盖原始错误
            pass
        logger.exception("workspace_project_manage failed")
        return _error(f"项目操作失败（{type(exc).__name__}）", "workspace_project_failed")


registry.register(
    name="workspace_project_manage",
    toolset="files",
    schema={
        "name": "workspace_project_manage",
        "description": (
            "管理项目（Project）：list / create / update / archive。"
            "update 可改 name/description/system_memory_ingest，需带 expected_row_version（CAS）。"
            "默认项目（is_default）不可归档。彻底删除（purge）只能由用户在 UI 触发。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "archive"]},
                "project_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string", "description": "项目备注（Markdown 纯文本）"},
                "system_memory_ingest": {"type": "string",
                                         "enum": ["normal", "user_facts", "exclude"]},
                "expected_row_version": {"type": "integer", "description": "CAS 版本"},
                "statuses": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
        },
    },
    handler=workspace_project_manage,
    is_async=True,
    check_fn=_available,
    description="Manage workspace projects",
    emoji="",
    permission_key="workspace_write",
)

__all__ = ["workspace_project_manage"]
