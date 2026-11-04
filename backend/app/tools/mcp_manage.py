# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""mcp_manage — conversational per-user MCP management (2026-11-01).

Users ask the agent to install/update/remove an MCP server; this tool writes the
owner-scoped row and applies it in-process immediately. The next iteration of
the same run sees the new tool schemas (AgentLoop expands on the next request),
so no service restart is needed. Every action is owner-bound via the dispatch
`user` kwarg and is act-gated by the `mcp_` prefix.
"""
import json
from typing import Any, Dict

from app.tools.registry import registry, tool_error
from app.services import user_mcp_service

_MCP_MANAGE_SCHEMA = {
    "name": "manage_mcp_server",
    "description": (
        "管理当前用户的 MCP server（无需重启服务）。"
        "action=list 查看；add 安装（stdio 需 command/args/env，HTTP 需 url/api_key）；"
        "update 修改（同名或指定 name，凭据传 *** 表示保留原值）；remove 删除；"
        "reload 重新应用。安装后当前 run 的下一轮即可检索/调用其工具。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "update", "remove", "reload"],
                "description": "操作类型",
            },
            "name": {"type": "string", "description": "server 名称（slug，用户内唯一）"},
            "transport": {"type": "string", "enum": ["http", "stdio"]},
            "url": {"type": "string", "description": "HTTP MCP endpoint"},
            "api_key": {"type": "string", "description": "HTTP Bearer（*** 表示保留）"},
            "command": {"type": "string", "description": "stdio 可执行文件"},
            "args": {"type": "array", "items": {"type": "string"}},
            "env": {"type": "object", "description": "stdio 环境变量（*** 表示保留）"},
            "enabled": {"type": "boolean"},
        },
        "required": ["action"],
    },
}


def _payload_from_args(args: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in (
        "name", "transport", "url", "api_key", "command", "args", "env", "enabled",
    ):
        if key in args:
            payload[key] = args.get(key)
    return payload


async def mcp_manage_handler(args: Dict[str, Any], **kwargs) -> str:
    user = kwargs.get("user")
    db = kwargs.get("db")
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return tool_error("manage_mcp_server requires an authenticated user")
    if db is None:
        return tool_error("manage_mcp_server requires a database session")

    action = str(args.get("action") or "").strip().lower()
    try:
        if action == "list":
            return json.dumps(
                {"servers": await user_mcp_service.list_servers(db, user_id)},
                ensure_ascii=False,
            )
        if action == "add":
            server = await user_mcp_service.create_server(
                db, user_id, _payload_from_args(args),
            )
            return json.dumps({
                "ok": bool(server.get("ok")),
                "server": server,
                "error": server.get("error"),
            }, ensure_ascii=False)
        if action in ("update", "remove"):
            name = str(args.get("name") or "").strip()
            if not name:
                return tool_error("name is required")
            match = None
            for row in await user_mcp_service.list_servers(db, user_id):
                if row.get("name") == name:
                    match = row
                    break
            if match is None:
                return tool_error(f"MCP server '{name}' not found")
            if action == "remove":
                ok = await user_mcp_service.delete_server(db, user_id, match["id"])
                return json.dumps({"ok": bool(ok)}, ensure_ascii=False)
            updated = await user_mcp_service.update_server(
                db, user_id, match["id"], _payload_from_args(args),
            )
            return json.dumps(updated or {"ok": False}, ensure_ascii=False)
        if action == "reload":
            result = await user_mcp_service.ensure_user_mcp_tools(
                db, user_id, force=True,
            )
            return json.dumps(result, ensure_ascii=False)
        return tool_error(f"unsupported action: {action}")
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"manage_mcp_server failed: {exc}")


registry.register(
    name="manage_mcp_server",
    toolset="core",
    schema=_MCP_MANAGE_SCHEMA,
    handler=mcp_manage_handler,
    is_async=True,
    description="Manage the current user's MCP servers without restarting",
    emoji="🔌",
)
