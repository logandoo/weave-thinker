# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""工具侧统一作用域解析（2026-11-10 波 · T5）。

设计契约 §7.1 / §7.2 / §14：

* **所有本地文件/执行工具用同一个解析入口**，不允许各自「找不到就回用户根」；
* `scope=current_task|project|task|legacy`（缺省 current_task）；
* enforce 下缺 `WorkspaceContext` → `workspace_scope_missing`（fail closed，绝不返回用户根）；
* 跨域（project/task/legacy）默认只读，且必须持服务端签发的 grant；
* shadow 档保持旧行为（回用户根）但记录「旧根 vs 提案 Task 根」比较，供 Phase 1 观测；
* off 档与 legacy 会话完全走旧路径（现网零行为变化）。

签名说明：`access="read"|"write"` 表示读写意图；`mode` 是作用域档位
（`off|shadow|enforce`，缺省读配置），两者刻意分开，避免与 grant 的 `mode=read` 混淆。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from app.core.config import get_config
from app.services import cross_scope_grant_service as gs
from app.services import workspace_scope as ws

logger = logging.getLogger(__name__)

SCOPES = ("current_task", "project", "task", "legacy", "scratch")


def scoping_mode() -> str:
    return get_config().workspace_task_scoping_mode


def supported_modes() -> tuple[str, ...]:
    return tuple(get_config().workspace_task_scoping_supported_modes)


def _shadow_log(**payload: Any) -> None:
    """Phase 1 观测面：旧根 vs 提案 Task 根。结构化一行，便于后续统计误读率。"""
    logger.info("workspace-scope shadow: %s", {k: v for k, v in payload.items() if v is not None})


def grant_id_from(kwargs: Mapping[str, Any] | None) -> str | None:
    """grant id 只从服务端注入的 kwargs / 工具参数里取；agent 不能自铸（只能引用）。"""
    data = kwargs or {}
    args = data.get("args") if isinstance(data.get("args"), Mapping) else {}
    for source in (data, args or {}):
        raw = source.get("grant_id")
        if raw and str(raw).strip():
            return str(raw).strip()
    return None


async def _legacy_root(kwargs: Mapping[str, Any]) -> Path:
    """旧路径：kwargs 里的 workspace_path 优先，否则按用户查 workspace 行。"""
    raw = str((kwargs or {}).get("workspace_path") or "").strip()
    if raw:
        return Path(raw).resolve()
    user = (kwargs or {}).get("user")
    db = (kwargs or {}).get("db")
    if user is not None and db is not None:
        from app.services.workspace_service import ensure_user_workspace

        workspace = await ensure_user_workspace(db, getattr(user, "id", None),
                                                getattr(user, "username", None))
        if workspace is not None and getattr(workspace, "root_path", None):
            return Path(workspace.root_path).resolve()
    raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING,
                                 "no workspace root available for legacy scope", "")


async def resolve_tool_root(kwargs: Mapping[str, Any] | None, *, scope: str = "current_task",
                            task_id: str | None = None, access: str = "read",
                            mode: str | None = None,
                            allow_legacy: bool = True,
                            count_audit: bool = True) -> ws.ScopeRoot:
    """工具的唯一取根入口。返回 `ScopeRoot`；任何越界意图都 raise。"""
    data = dict(kwargs or {})
    setting = str(mode or scoping_mode() or "off").strip().lower()
    if setting not in ("off", "shadow", "enforce"):
        setting = "off"
    if scope not in SCOPES:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "unknown scope", scope)
    ctx = data.get("workspace_ctx")

    # ---- off：完全旧行为 ---------------------------------------------------
    if setting == "off":
        if not allow_legacy:
            raise ws.WorkspaceScopeError(ws.CODE_MODE_UNSUPPORTED,
                                         "legacy root access is not allowed here", scope)
        root = await _legacy_root(data)
        return ws.ScopeRoot(root=root, scope="legacy", source="legacy",
                            read_only=(access != "write"))

    # ---- shadow：行为不变，只记录比较 --------------------------------------
    if setting == "shadow":
        root = await _legacy_root(data)
        if isinstance(ctx, ws.WorkspaceContext):
            _shadow_log(scope=scope, access=access, legacy=str(root),
                        proposed=str(ctx.write_root), task_id=ctx.task_id, run_id=ctx.run_id,
                        attempt_id=ctx.attempt_id, binding_version=ctx.binding_version,
                        lease_epoch=ctx.lease_epoch, differs=str(root) != str(ctx.write_root))
        return ws.ScopeRoot(root=root, scope="legacy", source="legacy",
                            read_only=(access != "write"))

    # ---- enforce -----------------------------------------------------------
    if not isinstance(ctx, ws.WorkspaceContext):
        raise ws.WorkspaceScopeError(
            ws.CODE_SCOPE_MISSING,
            "scoped execution requires a frozen WorkspaceContext; refusing the user root", scope)

    if scope == "current_task":
        return ws.ScopeRoot(root=ctx.write_root, scope="current_task", task_id=ctx.task_id,
                            source="当前任务", read_only=False)

    if scope == "scratch":
        # §7.3：terminal / execute_code / process / skill_run_script 的默认 cwd 是
        # 当前 attempt 的 scratch（稳定 Task 写入必须走 controlled_publish）。
        scratch = ctx.attempt_root / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        return ws.ScopeRoot(root=scratch, scope="scratch", task_id=ctx.task_id,
                            source="当前执行", read_only=False)

    grant_id = grant_id_from(data)
    db = data.get("db")
    if db is None:
        raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING, "db session required for cross scope",
                                     scope)
    # 跨域读取必须逐次带 grant，且 grant 要绑定到**本次请求的资源**（见 verify_grant）；
    # 列表参数逐项过检，任何一项不在 allowed_paths 即整体拒绝（fail-closed）。
    requested_rels = _requested_rels(data, None) or [None]

    if scope == "project":
        kind = "memory" if str(task_id or "") == "memory" else "files"
        if not grant_id:
            # §7.1：项目 shared 是「显式 scope=project 才读」，仍然要 grant 记账与可撤销
            raise ws.WorkspaceScopeError(ws.CODE_CROSS_SCOPE_DENIED,
                                         "project shared read needs a server-issued grant", scope)
        for rel in requested_rels:
            check = await gs.verify_grant(db, grant_id, ctx=ctx, kind=kind, mode="read",
                                          path=rel, count_audit=count_audit,
                                          requested_project_id=str(ctx.project_id))
            if not check.ok:
                raise ws.WorkspaceScopeError(check.code or ws.CODE_CROSS_SCOPE_DENIED,
                                             check.message, scope)
        if ctx.project_root is None:
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "no project in context", scope)
        root = (ctx.project_root / "shared").resolve()
        if not ws.is_inside(ctx.user_root, root):
            raise ws.WorkspaceScopeError(ws.CODE_ROOT_INVALID, "project shared escapes user root",
                                         str(root))
        return ws.ScopeRoot(root=root, scope="project", task_id=ctx.task_id, source="项目共享",
                            read_only=True, grant_id=grant_id)

    if scope == "task":
        if not task_id:
            raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "scope=task needs task_id", scope)
        if str(task_id) == str(ctx.task_id):
            return ws.ScopeRoot(root=ctx.write_root, scope="current_task", task_id=ctx.task_id,
                                source="当前任务", read_only=False)
        if not grant_id:
            raise ws.WorkspaceScopeError(ws.CODE_CROSS_SCOPE_DENIED,
                                         "cross-task read needs a server-issued grant", str(task_id))
        # 目录级放行：每个请求路径（含 files/folders 列表项）都过 allowed_paths 前缀校验
        for rel in requested_rels:
            check = await gs.verify_grant(db, grant_id, ctx=ctx, kind="files", mode="read",
                                          path=rel, count_audit=count_audit,
                                          requested_task_id=str(task_id))
            if not check.ok:
                raise ws.WorkspaceScopeError(check.code or ws.CODE_CROSS_SCOPE_DENIED,
                                             check.message, str(task_id))
        from app.services import workspace_task_service as svc

        task = await svc.get_task(db, user_id=ctx.user_id, task_id=str(task_id))
        if task is None:
            raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "source task not found", str(task_id))
        root = (ctx.user_root / Path(*task.root_rel.split("/")) / "workspace").resolve()
        if not ws.is_inside(ctx.user_root, root):
            raise ws.WorkspaceScopeError(ws.CODE_ROOT_INVALID, "source task root escapes user root",
                                         str(root))
        return ws.ScopeRoot(root=root, scope="task", task_id=task.id,
                            source=f"任务:{task.title or task.id[:8]}", read_only=True,
                            grant_id=grant_id)

    # scope == "legacy"
    if not grant_id:
        raise ws.WorkspaceScopeError(ws.CODE_CROSS_SCOPE_DENIED,
                                     "legacy root access needs an explicit grant", scope)
    for rel in requested_rels:
        check = await gs.verify_grant(db, grant_id, ctx=ctx, kind="legacy", mode="read",
                                      path=rel, count_audit=count_audit)
        if not check.ok:
            raise ws.WorkspaceScopeError(check.code or ws.CODE_CROSS_SCOPE_DENIED,
                                         check.message, "legacy")
    root = await _legacy_root(data)
    return ws.ScopeRoot(root=root, scope="legacy", source="legacy", read_only=True,
                        grant_id=grant_id)


async def resolve_write_root(kwargs: Mapping[str, Any] | None, *,
                             mode: str | None = None) -> ws.ScopeRoot:
    """写入只允许当前 Task（§7.2：写入工具不开放任意 scope 写）。"""
    return await resolve_tool_root(kwargs, scope="current_task", access="write", mode=mode)


_REQUESTED_PATH_KEYS = ("rel_path", "path", "file_path", "folder_path", "audio_path", "image_path")


def _requested_rels(data: Mapping[str, Any], args: Mapping[str, Any] | None) -> list:
    """本次请求的**全部**目标相对路径（标量键 + files/folders 列表逐项）。

    grant 的 allowed_paths 必须覆盖每一项——否则 `file_path=允许项 + files=[越界项]`
    会拿前者的放行读后者（评审 r2 #5）。
    """
    out: list[str] = []
    single = _requested_rel(data, args)
    if single:
        out.append(single)
    for source in (args or {}, data or {}):
        for key in ("files", "folders"):
            raw = source.get(key)
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    val = str(item).strip().strip("'\"")
                    if val and val not in out:
                        out.append(val)
    return out


def _requested_rel(data: Mapping[str, Any], args: Mapping[str, Any] | None) -> str | None:
    """本次请求的目标相对路径（grant 的 allowed_paths 前缀按它校验）。"""
    sources = [args or {}, data or {}]
    for key in _REQUESTED_PATH_KEYS:
        for source in sources:
            raw = source.get(key)
            if raw and str(raw).strip():
                return str(raw).strip().strip("'\"")
    return None


async def apply_scope(kwargs: Mapping[str, Any] | None, *, default_root: Any,
                      args: Mapping[str, Any] | None = None, access: str = "read",
                      mode: str | None = None, default_scope: str = "current_task") -> str:
    """工具侧一行收口：把「工具自己算出来的根」换成作用域根。

    * `off` → 原样返回 `default_root`（**现网零行为变化**，包括 separate_v1 的窄 root）；
    * `shadow` → 原样返回，但记录「旧根 vs 提案 Task 根」比较；
    * `enforce` → 返回当前 Task 根（或经 grant 的只读跨域根）；缺 context 直接 raise。

    `args` 里的 `scope` / `task_id` 是模型请求参数，服务端仍会校验归属与 grant。
    """
    setting = str(mode or scoping_mode() or "off").strip().lower()
    if setting not in ("shadow", "enforce"):
        return str(default_root)
    data = dict(kwargs or {})
    if args:
        for key in ("scope", "task_id", "grant_id", *_REQUESTED_PATH_KEYS):
            if key in args and key not in data:
                data[key] = args.get(key)
        for key in ("files", "folders"):
            if isinstance(args.get(key), (list, tuple)) and key not in data:
                data[key] = list(args[key])
    # 写入/执行永远不接受模型给的 scope：只允许当前 Task（或执行类的 attempt scratch）。
    # §7.2「写入工具不开放任意 scope 写」+ §11.2「跨域读取不授予写权限」。
    if access == "write":
        scope = default_scope if default_scope == "scratch" else "current_task"
    else:
        scope = str(data.get("scope") or default_scope)
    if setting == "shadow":
        root = await resolve_tool_root(data, scope="current_task", access=access, mode="shadow")
        return str(root.root)
    root = await resolve_tool_root(data, scope=scope, task_id=data.get("task_id"),
                                   access=access, mode="enforce")
    return str(root.root)


async def apply_scope_write(kwargs: Mapping[str, Any] | None, *, default_root: Any,
                            mode: str | None = None) -> str:
    """写入路径的作用域收口：enforce 下只允许当前 Task 文件域。"""
    return await apply_scope(kwargs, default_root=default_root, access="write", mode=mode)


def scope_error_payload(exc: ws.WorkspaceScopeError) -> str:
    """把作用域拒绝翻译成工具返回（模型可见，含下一步指引，但不泄漏他人信息）。"""
    return json.dumps({"error": str(exc), "code": exc.code, "success": False},
                      ensure_ascii=False)


async def apply_scope_or_error(kwargs: Mapping[str, Any] | None, *, default_root: Any,
                               args: Mapping[str, Any] | None = None, access: str = "read",
                               mode: str | None = None,
                               default_scope: str = "current_task") -> tuple[str, dict | None]:
    """工具统一收口：返回 `(root, error_payload)`，`error_payload` 非空即应直接回给模型。

    每个文件/执行工具都只加这三行，保证「不允许悄悄退回用户根」在结构上可审计。
    """
    try:
        root = await apply_scope(kwargs, default_root=default_root, args=args, access=access,
                                 mode=mode, default_scope=default_scope)
    except ws.WorkspaceScopeError as exc:
        hint = {
            ws.CODE_SCOPE_MISSING: "本轮缺少任务作用域上下文，请在新会话中重试或让用户显式选择任务。",
            ws.CODE_CROSS_SCOPE_DENIED: "跨任务/项目/legacy 读取需要用户在界面上显式授权（grant），"
                                        "不能用参数自行放行。",
            ws.CODE_ROOT_INVALID: "目标路径不在当前任务文件域内。",
        }.get(exc.code, "")
        return str(default_root), {"error": f"任务作用域拒绝：{exc}", "code": exc.code,
                                   "hint": hint, "success": False}
    return root, None


async def resolve_scope_meta(kwargs: Mapping[str, Any] | None, *, args: Mapping[str, Any] | None = None,
                             access: str = "read", mode: str | None = None,
                             default_scope: str = "current_task") -> dict | None:
    """当前请求解析出的作用域展示元数据 `{scope, source, task_id, grant_id}`。

    off/shadow 与 current_task/scratch/legacy 返回 None（现网零变化、不给本域附件
    加噪声标签）；只有经 grant 的跨域读取才返回标签，供交付卡片显示来源 badge。
    """
    setting = str(mode or scoping_mode() or "off").strip().lower()
    if setting != "enforce":
        return None
    data = dict(kwargs or {})
    if args:
        for key in ("scope", "task_id", "grant_id", *_REQUESTED_PATH_KEYS):
            if key in args and key not in data:
                data[key] = args.get(key)
    scope = str(data.get("scope") or default_scope)
    if scope in ("current_task", "scratch", "legacy"):
        return None
    try:
        root = await resolve_tool_root(data, scope=scope, task_id=data.get("task_id"),
                                       access=access, mode="enforce",
                                       count_audit=False)
    except ws.WorkspaceScopeError:
        return None
    if root.scope in ("current_task", "scratch", "legacy"):
        return None
    return {"scope": root.scope, "source": root.source,
            "task_id": str(root.task_id) if root.task_id else None,
            "grant_id": root.grant_id}


def capability_guard(mode_name: str) -> None:
    """分阶段能力门：未支持的模式明确拒绝。"""
    ws.capability_guard(mode_name, supported_modes())


__all__ = ["SCOPES", "apply_scope", "apply_scope_or_error", "apply_scope_write",
           "resolve_scope_meta",
           "capability_guard", "grant_id_from", "resolve_tool_root", "resolve_write_root",
           "scoping_mode", "supported_modes"]
