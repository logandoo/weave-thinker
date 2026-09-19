# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""process 工具 — 持久进程会话（P1b）。

start/write/kill 属于命令执行动作：未获 `terminal_execution` 权限时返回
`_permission_needed`（由既有权限管道询问后以 `_permission_granted` 重入）；
read/list 免门控。命令静态校验复用 terminal 的 `_validate_command`。
"""
import json
import logging
from typing import Any, Dict

from app.tools.registry import registry
from app.tools.terminal import check_terminal_requirements
from app.tools.workspace_write import _resolve_workspace

logger = logging.getLogger(__name__)

_ACTIONS = ("start", "write", "read", "kill", "list")
_GATED_ACTIONS = ("start", "write", "kill")


def _error(message: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"error": message, "success": False}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


async def process(args: Dict[str, Any], **kwargs) -> str:
    action = str(args.get("action") or "").lower()
    if action not in _ACTIONS:
        return _error(f"action 必须是 {list(_ACTIONS)} 之一。")

    user = kwargs.get("user")
    if user is None or kwargs.get("db") is None:
        return _error("用户上下文缺失，无法执行进程操作。")

    workspace_root, err = await _resolve_workspace(kwargs)
    if err:
        return _error(err)
    user_id = str(getattr(user, "id", ""))

    if action in _GATED_ACTIONS and not args.get("_permission_granted"):
        from app.services.agent_permissions import is_permission_allowed, permission_description

        if not is_permission_allowed(user, "terminal_execution"):
            return json.dumps(
                {
                    "success": False,
                    "_permission_needed": True,
                    "_permission_key": "terminal_execution",
                    "_permission_description": permission_description("terminal_execution"),
                    "_action": action,
                    "_command": str(args.get("command") or ""),
                },
                ensure_ascii=False,
            )

    from app.services.process_session_service import process_manager

    if action == "start":
        command = str(args.get("command") or "").strip()
        if not command:
            return _error("start 需要 command。")
        try:
            wait_ms = int(args.get("wait_ms") or 300)
        except (TypeError, ValueError):
            return _error("wait_ms 必须是整数。")
        result = await process_manager.start(
            user_id,
            command,
            cwd=str(args.get("cwd") or workspace_root),
            workspace_root=workspace_root,
            wait_ms=wait_ms,
        )
    elif action == "write":
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return _error("write 需要 session_id。")
        result = await process_manager.write(
            user_id,
            session_id,
            str(args.get("data") or ""),
            newline=bool(args.get("newline", True)),
        )
    elif action == "read":
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return _error("read 需要 session_id。")
        try:
            wait_ms = int(args.get("wait_ms") or 0)
        except (TypeError, ValueError):
            return _error("wait_ms 必须是整数。")
        max_chars = args.get("max_chars")
        if max_chars is not None:
            try:
                max_chars = int(max_chars)
            except (TypeError, ValueError):
                return _error("max_chars 必须是整数。")
        result = await process_manager.read(
            user_id,
            session_id,
            wait_ms=wait_ms,
            max_chars=max_chars,
        )
    elif action == "kill":
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return _error("kill 需要 session_id。")
        result = await process_manager.kill(user_id, session_id)
    else:
        result = process_manager.list(user_id)

    if not result.get("ok"):
        extras = {k: v for k, v in result.items() if k not in ("ok", "error")}
        return _error(result.get("error") or "process 操作失败", **extras)
    payload = {"success": True}
    payload.update({k: v for k, v in result.items() if k != "ok"})
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="process",
    toolset="system",
    schema={
        "name": "process",
        "description": (
            "持久进程会话：适合长任务与交互式操作——启动开发服务器、跑长时间测试/构建、"
            "需要持续 stdin（REPL、调试器）或需要跨多次调用读取增量输出的场景。"
            "start 启动后台命令并返回 session_id；write 向 stdin 写入；read 增量读取"
            "新输出（wait_ms 等待，默认自上次读取处继续，避免重复输出）；kill 终止；"
            "list 列出本用户会话。一次性命令（安装包、短命令）继续用 terminal。"
            "start/write/kill 需要终端执行权限；会话空闲 30 分钟自动回收，服务重启后不保留。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "start / write / read / kill / list。",
                },
                "command": {"type": "string", "description": "start 时要执行的 shell 命令。"},
                "cwd": {
                    "type": "string",
                    "description": "工作目录（须在工作区内），默认工作区根目录。",
                },
                "session_id": {"type": "string", "description": "write/read/kill 使用的会话 ID。"},
                "data": {"type": "string", "description": "write 写入 stdin 的内容。"},
                "newline": {
                    "type": "boolean",
                    "description": "write 时是否自动追加换行（默认 true）。",
                    "default": True,
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "read 等待新输出的毫秒数（0-30000，默认 0 立即返回）；start 时等待初始输出的毫秒数。",
                },
                "max_chars": {"type": "integer", "description": "read 单次返回的最大字符数。"},
            },
            "required": ["action"],
        },
    },
    handler=process,
    check_fn=check_terminal_requirements,
    is_async=True,
    description="Persistent process sessions (start/write/read/kill/list)",
    emoji="",
)
