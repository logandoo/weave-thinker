# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""workspace_snapshot 工具 — 快照 create/list/restore。

restore 会覆盖工作区当前文件内容，默认权限拒绝（workspace_restore=False）：
未授权时返回 `_permission_needed`，由既有权限管道询问用户后以
`_permission_granted` 重入；create/list 免门控。
"""
import json
import logging
from typing import Any, Dict

from app.tools.registry import registry
from app.tools.workspace_write import _resolve_workspace

logger = logging.getLogger(__name__)

_ACTIONS = ("create", "list", "restore")


def _error(message: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"error": message, "success": False}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


async def workspace_snapshot(args: Dict[str, Any], **kwargs) -> str:
    action = str(args.get("action") or "list").lower()
    if action not in _ACTIONS:
        return _error(f"action 必须是 {list(_ACTIONS)} 之一。")

    workspace_root, err = await _resolve_workspace(kwargs)
    if err:
        return _error(err)
    user = kwargs.get("user")
    user_id = str(getattr(user, "id", ""))

    from app.services.workspace_snapshot_service import (
        create_snapshot,
        list_snapshots,
        restore_snapshot,
    )

    if action == "create":
        reason = str(args.get("label") or args.get("reason") or "manual snapshot")
        result = await create_snapshot(user_id, workspace_root, reason=reason)
    elif action == "list":
        result = await list_snapshots(user_id, workspace_root)
    else:
        from app.services.agent_permissions import is_permission_allowed, permission_description

        if not args.get("_permission_granted") and not is_permission_allowed(user, "workspace_restore"):
            return json.dumps(
                {
                    "success": False,
                    "_permission_needed": True,
                    "_permission_key": "workspace_restore",
                    "_permission_description": permission_description("workspace_restore"),
                },
                ensure_ascii=False,
            )
        snapshot_id = str(args.get("snapshot_id") or "").strip()
        if not snapshot_id:
            return _error("restore 需要 snapshot_id（可先用 action='list' 查看）。")
        result = await restore_snapshot(user_id, workspace_root, snapshot_id)

    if not result.get("ok"):
        return _error(result.get("error") or "snapshot operation failed", **{k: v for k, v in result.items() if k not in ("ok", "error")})
    payload = {"success": True}
    payload.update({k: v for k, v in result.items() if k != "ok"})
    logger.info("workspace_snapshot: action=%s user=%s", action, user_id)
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="workspace_snapshot",
    toolset="files",
    schema={
        "name": "workspace_snapshot",
        "description": (
            "管理工作区快照（影子 git）：create 创建快照；list 列出最近快照；"
            "restore 用指定 snapshot_id 恢复文件内容（需要用户授权；恢复前会自动"
            "为当前状态再建一次快照，因此恢复本身可再撤销；快照之后新增的文件不会"
            "被删除）。workspace_write/workspace_edit 在每次写入前会自动创建快照，"
            "误改后可用本工具回滚。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "create（创建）/ list（列出）/ restore（恢复）。",
                },
                "snapshot_id": {
                    "type": "string",
                    "description": "restore 使用的快照 ID（由 create 返回或 list 获得，如 1758...-a1b2c3d4e5）。",
                },
                "label": {
                    "type": "string",
                    "description": "create 时可选的快照标签（用于 list 辨识）。",
                },
            },
            "required": ["action"],
        },
    },
    handler=workspace_snapshot,
    is_async=True,
    description="Create/list/restore workspace snapshots",
    emoji="",
)
