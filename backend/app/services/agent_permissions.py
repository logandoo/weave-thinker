# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
Agent permission management.

MULTI-WORKER NOTE: permission_manager (permission_manager.py) is a process-local
dictionary. In a multi-worker deployment, permission approval requests sent to
one worker will not be found by another worker (the request_id lookup fails, and
the approval times out silently after 120s). For multi-worker deployments, migrate
permission state to Redis or the database.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class PermissionContext:
    """P1 4.5: Context passed to registry.dispatch() for pre-execution permission checks.

    When a tool has a ``permission_key`` on its ToolEntry registration, the
    registry's dispatch method checks this context BEFORE executing the tool
    (rather than relying on each tool's self-check of ``_permission_granted``)."""

    super_admin_bypass: bool = False
    deathmatch_active: bool = False
    user: Any = None
    callback: Optional[Callable] = None

DEFAULT_AGENT_PERMISSIONS = {
    "terminal_execution": False,
    "code_execution": True,
    "note_create": True,
    "note_edit": True,
    "note_delete": False,
    "notebook_create": True,
    "notebook_edit": True,
    "notebook_delete": False,
}

PERMISSION_KEYS = set(DEFAULT_AGENT_PERMISSIONS.keys())


def get_default_permissions() -> Dict[str, bool]:
    return dict(DEFAULT_AGENT_PERMISSIONS)


def parse_permissions(user: Any) -> Dict[str, bool]:
    perms = get_default_permissions()
    if user is None:
        return perms
    raw = getattr(user, "agent_permissions", None) or {}
    if isinstance(raw, str):
        try:
            stored = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            stored = {}
    elif isinstance(raw, dict):
        stored = raw
    else:
        stored = {}
    for key in PERMISSION_KEYS:
        if key in stored and isinstance(stored[key], bool):
            perms[key] = stored[key]
    return perms


def is_permission_allowed(user: Any, key: str) -> bool:
    return parse_permissions(user).get(key, DEFAULT_AGENT_PERMISSIONS.get(key, False))


def permission_description(key: str) -> str:
    descriptions = {
        "terminal_execution": "终端命令执行（高风险操作）",
        "note_create": "新增笔记",
        "note_edit": "编辑笔记",
        "note_delete": "删除笔记",
        "notebook_create": "创建笔记本",
        "notebook_edit": "修改笔记本",
        "notebook_delete": "删除笔记本",
    }
    return descriptions.get(key, key)


def permission_key_for_tool_request(tool_name: str, details: Optional[Dict[str, Any]] = None) -> Optional[str]:
    details = details or {}
    if tool_name == "terminal":
        return "terminal_execution"
    if tool_name == "notes":
        action = (details.get("action") or "").lower()
        if action == "create_note":
            return "note_create"
        if action == "update_note":
            return "note_edit"
        if action == "delete_note":
            return "note_delete"
        if action == "create_notebook":
            return "notebook_create"
        if action == "update_notebook":
            return "notebook_edit"
        if action == "delete_notebook":
            return "notebook_delete"
    return None
