# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`workspace_note_bind` — 把当前用户的笔记绑定/解绑到任务（§4.9 / §6）。

约束：只能操作**当前用户**的笔记（owner 校验走 notebook.user_id）；
`task_id=null` = 解绑（不删笔记）；绑定是关系，不复制内容。
2026-11-16 Wave B：绑定是**多对多**——传 `task_id` 是「加绑」（幂等，不影响该笔记
在别的任务/项目上的绑定），要摘掉单个任务请用 HTTP 面
（`POST /api/notes/{id}/task-binding` + `action=unbind`）；本工具的 `task_id` 为空
仍是「清空该笔记全部绑定」的旧语义。
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


async def workspace_note_bind(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    if db is None or user is None:
        return _error("缺少会话上下文", "workspace_scope_missing")
    from app.services import workspace_note_binding_service as nbs

    note_id = str(args.get("note_id") or "").strip()
    if not note_id:
        return _error("需要 note_id", "workspace_invalid_args")
    raw_task = args.get("task_id")
    task_id = str(raw_task).strip() if raw_task not in (None, "") else None
    try:
        note = await nbs.bind_note(db, user_id=user.id, note_id=note_id, task_id=task_id)
        bound = await nbs.tasks_for_note(db, user_id=user.id, note_id=note_id)
        await db.commit()
    except ws.WorkspaceScopeError as exc:
        return _error(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败不掩盖原始错误
            pass
        logger.exception("workspace_note_bind failed")
        return _error(f"笔记绑定失败（{type(exc).__name__}）", "workspace_note_bind_failed")
    return json.dumps({
        "ok": True, "success": True, "note_id": note.id, "task_id": note.task_id,
        "task_ids": [b["task_id"] for b in bound],
        "note": "解绑不删除笔记；绑定不复制内容；一条笔记可同时绑多个任务/项目。",
    }, ensure_ascii=False)


registry.register(
    name="workspace_note_bind",
    toolset="files",
    schema={
        "name": "workspace_note_bind",
        "description": (
            "把当前用户的笔记绑定到任务，或解绑（task_id 为空/不传 = 解绑）。"
            "只能操作自己的笔记；解绑不删除笔记。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note_id": {"type": "string", "description": "笔记 id"},
                "task_id": {"type": "string", "description": "目标任务 id；空 = 解绑"},
            },
            "required": ["note_id"],
        },
    },
    handler=workspace_note_bind,
    is_async=True,
    check_fn=_available,
    description="Bind or unbind a note to a workspace task",
    emoji="",
    permission_key="workspace_write",
)

__all__ = ["workspace_note_bind"]
