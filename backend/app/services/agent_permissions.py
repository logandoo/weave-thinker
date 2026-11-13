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
    # 2026-11-12：调用点新鲜读取的完整权限表（含默认值合并）。非 None 时
    # registry 权限闸以它为准，而不是 user.agent_permissions（请求期快照，
    # 进行中会话改设置后仍是旧值）。None = 旧语义（回落 user）。
    permissions: Optional[Dict[str, bool]] = None

DEFAULT_AGENT_PERMISSIONS = {
    "terminal_execution": False,
    "code_execution": True,
    # P0（2026-09-19）：工作区写/编辑默认允许（有快照回滚兜底）；恢复快照默认
    # 拒绝（会覆盖工作区当前状态，需用户显式授权）。
    "workspace_write": True,
    "workspace_restore": False,
    # vibeweaver 集成 P0（2026-10-01）：工作区内受控 git 变更（add/commit/
    # stash/init）默认允许——提交是可回滚的本地动作；restore 仍走
    # workspace_restore（覆盖文件需显式授权）。
    "git_execution": True,
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
    return parse_permissions_payload(raw, base=perms)


def parse_permissions_payload(raw: Any, base: Optional[Dict[str, bool]] = None) -> Dict[str, bool]:
    """把 users.agent_permissions 的原始值（JSON 串 / dict / None）解析成完整权限表。

    2026-11-12：`fresh_permissions` 与设备 RPC 的 `permissions` 快照共用同一解析，
    避免两处口径漂移（默认值合并 + 仅接受 bool 值）。
    """
    perms = dict(base) if base is not None else get_default_permissions()
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


async def fresh_permissions(db: Any, user: Any) -> Dict[str, bool]:
    """从 DB **新鲜读取**用户权限（进行中会话改设置后立即生效）。

    请求期加载的 `user` ORM 对象在整轮 stream 内不会刷新；若只读它，用户在
    设置里改的开关要等下一轮请求才生效（2026-11-12 现场缺陷 #1）。这里用调用点
    的 db 会话直接 SELECT 该列（不 refresh ORM 对象，避免跨 session 绑定冲突）。
    读失败/无 db → 回落请求期快照（fail-safe，不改变既有语义）。
    """
    base = get_default_permissions()
    try:
        base = parse_permissions(user)
    except Exception:
        # detached/expired ORM 对象：属性访问即抛 DetachedInstanceError——
        # 不得让权限读取把工具调用打死，回落默认值（A4.9 评审 Minor #6）。
        logger.debug("parse_permissions failed; using defaults", exc_info=True)
    if db is None or user is None:
        return base
    try:
        user_id = getattr(user, "id", None)
        if not user_id:
            return base
        from sqlalchemy import select

        from app.db.database import User

        result = await db.execute(select(User.agent_permissions).where(User.id == user_id))
        raw = result.scalar_one_or_none()
        return parse_permissions_payload(raw, base=base)
    except Exception:
        logger.debug("fresh_permissions read failed; using request-time snapshot", exc_info=True)
        return base



def is_permission_allowed(user: Any, key: str) -> bool:
    return parse_permissions(user).get(key, DEFAULT_AGENT_PERMISSIONS.get(key, False))


def permission_allowed_from(kwargs: Any, user: Any, key: str) -> bool:
    """工具内部自检的权限判定（2026-11-12）。

    优先使用调用点新鲜权限快照 `kwargs["permission_snapshot"]`（云端每次工具调用
    新鲜读取 / 设备 RPC 随帧下发）；缺省回落 `user.agent_permissions`（旧语义）。
    这样 process/git/workspace_snapshot 等**无 registry permission_key** 的工具
    也能即时跟随设置变更（A4.9 评审 Important #2）。
    """
    snapshot = (kwargs or {}).get("permission_snapshot") if isinstance(kwargs, dict) else None
    if isinstance(snapshot, dict):
        return bool(snapshot.get(key, DEFAULT_AGENT_PERMISSIONS.get(key, False)))
    return is_permission_allowed(user, key)



def permission_description(key: str) -> str:
    descriptions = {
        "terminal_execution": "终端命令执行（高风险操作）",
        "workspace_write": "工作区文件写入/编辑",
        "workspace_restore": "恢复工作区快照（会覆盖当前文件内容）",
        "git_execution": "工作区 git 变更操作（add/commit/stash/init）",
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
    if tool_name == "process":
        action = (details.get("action") or "").lower()
        if action in ("start", "write", "kill"):
            return "terminal_execution"
        return None
    if tool_name == "git":
        sub = str(details.get("action") or details.get("subcommand") or "").lower()
        if sub == "restore":
            return "workspace_restore"
        if sub in ("add", "commit", "stash", "init"):
            return "git_execution"
        return None
    if tool_name == "notes":
        action = (details.get("action") or "").lower()
        if action == "create_note":
            return "note_create"
        if action in ("update_note", "append_note"):
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
