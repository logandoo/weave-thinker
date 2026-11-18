# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""工具侧统一作用域解析（2026-11-10 波 · T5）。

设计契约 §7.1 / §7.2 / §14：

* **所有本地文件/执行工具用同一个解析入口**，不允许各自「找不到就回用户根」；
* `scope=current_task|project|task|legacy`（缺省 current_task）；
* enforce 下缺 `WorkspaceContext` → `workspace_scope_missing`（fail closed），**唯一例外**：
  装配层显式声明 `workspace_scoping_intent="legacy"` 时用用户根（2026-11-13 续波，见
  `workspace_scoping_runtime._stamp_intent`）。intent 只认服务端 kwargs、只认精确值，
  未声明/垃圾值一律仍 fail closed；模型 args 无法伪造（args→kwargs 只合并白名单键）；
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
from typing import Any, Mapping, Sequence

from app.core.config import get_config
from app.services import cross_scope_grant_service as gs
from app.services import workspace_scope as ws

logger = logging.getLogger(__name__)

SCOPES = ("current_task", "project", "task", "legacy", "scratch")

# 服务端装配层注入的「本轮作用域意图」（模型不可见、不可伪造，只认 kwargs）。
# 只有精确值生效：新链路忘了注入 → enforce 照旧 fail-closed，不会被默认放行架空。
INTENT_TASK = "task"        # 本轮已绑定 Task（有 frozen ctx）
INTENT_LEGACY = "legacy"    # 本轮**设计上**就走旧用户根（老会话不换房 / 链路未放开作用域）


def scoping_mode() -> str:
    return get_config().workspace_task_scoping_mode


def supported_modes() -> tuple[str, ...]:
    return tuple(get_config().workspace_task_scoping_supported_modes)


def owning_task_for_rel(rel: str, tasks: Sequence[tuple[str, str]]) -> tuple[str, str] | None:
    """用户根相对路径 → 拥有它的 Task ``(task_id, task_root_rel)``（最长 root_rel 段前缀匹配）。

    无归属返回 None：项目 ``shared/``、用户根直下的 legacy 文件、以及名字段非边界的近似前缀
    （``t_ab`` 不被 ``t_a`` 误吞）。纯函数，不查库（与 workspace_scope 同一 ethos）。
    """
    if not rel:
        return None
    segs = [s for s in str(rel).replace("\\", "/").split("/") if s not in ("", ".")]
    if not segs:
        return None
    best: tuple[str, str] | None = None
    best_len = -1
    for task_id, root_rel in tasks:
        rsegs = [s for s in str(root_rel).replace("\\", "/").split("/") if s not in ("", ".")]
        if not rsegs:
            continue
        if len(rsegs) > best_len and segs[:len(rsegs)] == rsegs:
            best = (str(task_id), "/".join(rsegs))
            best_len = len(rsegs)
    return best


async def owning_task_for_path(db, *, user_id: str, rel: str) -> tuple[str, str] | None:
    """DB 薄壳：取本用户非删除 Task 的 (id, root_rel)，交 `owning_task_for_rel` 反查。

    ⚠ **有意保留的地基**（intentional groundwork, ADR D-20261117-waveJ2-5）：当前无生产调用方——
    徽标实由 `resolve_tool_root` 直接给出 task_id，免 grant 的 `scope=legacy` 读已撤除（延后到工具侧
    遍历过滤硬化波）。该硬化波需要「path→owning-task」做 storage_authority 读围栏，故此处先备好并带
    单测（`test_workspace_read_root_20261117.py`）。硬化波落地时接线，勿当意外死代码删除。
    """
    from sqlalchemy import select

    from app.db.database import WorkspaceTask

    rows = (await db.execute(
        select(WorkspaceTask.id, WorkspaceTask.root_rel).where(
            WorkspaceTask.user_id == user_id,
            WorkspaceTask.status != "deleted")
    )).all()
    return owning_task_for_rel(rel, [(str(t), str(r)) for t, r in rows if r])


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


async def _assert_write_binding_current(db, ctx) -> None:
    """D-3（CE-1）：写当前任务域前确认会话绑定仍指向 ctx 的 task+version。

    回合中被改绑（binding_version 递增 / 换 task）→ 旧 ctx 的写必须 fail-closed，
    不再静默落到已被放弃的旧 Task 文件域（`workspace_ctx` 每回合注入一次、不刷新）。
    非锁读（UNIQUE(conversation_id) 索引），并行写不串行化；与 controlled_publish 的
    `assert_write_epoch` 互补（后者覆盖 publish/接管，本围栏覆盖 workspace_write/edit 直写）。
    无会话身份的 run（执行链 conversation_id=None）此处跳过，交回 `assert_write_epoch` 的
    execution 分支——注意那只覆盖 controlled_publish 路径，执行链 ctx 的 workspace_write/edit
    直写要等这些链进 supported_modes 才由本围栏覆盖（在那之前为已接受边界）。
    """
    conv_id = getattr(ctx, "conversation_id", None)
    if not conv_id:
        return   # 执行链 conversation_id=None：无会话权威，交回 assert_write_epoch 的 execution 分支
    if db is None:
        # CF-2（②）：有会话身份却缺 db = 写围栏无法验证绑定 → fail-closed（不再 fail-open 静默放行）。
        # 生产 dispatch 恒注入 db（agent_loop_tools.py），工具层 _resolve_workspace 也已先挡 db=None；
        # 这里把「未来某个调用点漏传 db」的残留堵死。
        raise ws.WorkspaceScopeError(ws.CODE_SCOPE_MISSING,
                                     "write fence cannot verify binding without a db session",
                                     str(conv_id))
    from sqlalchemy import select

    from app.db.database import WorkspaceBinding

    row = (await db.execute(
        select(WorkspaceBinding.task_id, WorkspaceBinding.binding_version)
        .where(WorkspaceBinding.conversation_id == str(conv_id),
               WorkspaceBinding.user_id == str(ctx.user_id)))).first()   # 用户交叉校验（评审 r2 Minor）
    if row is None:
        raise ws.WorkspaceScopeError(ws.CODE_BINDING_STALE,
                                     "conversation binding no longer exists", str(conv_id))
    if str(row[0]) != str(ctx.task_id) or int(row[1]) != int(ctx.binding_version):
        raise ws.WorkspaceScopeError(
            ws.CODE_BINDING_STALE,
            f"task binding moved (now task={row[0]} v{row[1]}, this turn is "
            f"task={ctx.task_id} v{ctx.binding_version})", str(conv_id))


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
        # 「本轮刻意走 legacy」必须是服务端装配层显式声明的事实（kwargs 里的
        # `workspace_scoping_intent`），不是工具层猜、更不是模型 args 能塞的。
        # 2026-11-13 60 事故续波修的正是这里的矛盾：入口门按 acceptance #31 放行未绑定
        # 会话（老会话不换房、未放开链路保持旧根），而工具层却一律拒绝 → 这些会话
        # 「进得来、什么都干不了」（60 上 1397 条有历史的未绑定会话即此状态）。
        # 只认精确值 INTENT_LEGACY：忘了注入的新链路继续 fail-closed，enforce 不被架空。
        if str((data or {}).get("workspace_scoping_intent") or "") == INTENT_LEGACY:
            root = await _legacy_root(data)
            # 每次工具调用都过这里（一轮最多 max_iterations 次），所以逐次记 DEBUG；
            # 「为什么这轮没进 Task」的权威 INFO 在装配点打（attach_chat_scope 的两条
            # legacy 分支各有一条带原因的 INFO 日志），运维在 INFO 级看装配、DEBUG 级看逐次。
            logger.debug(
                "workspace scope(enforce): legacy intent → user root user=%s conv=%s "
                "source=%s access=%s root=%s",
                str(getattr(data.get("user"), "id", "") or ""),
                str(getattr(data.get("conversation"), "id", "") or ""),
                str(data.get("workspace_legacy_source") or "unspecified"), access, root)
            return ws.ScopeRoot(root=root, scope="legacy", source="legacy(声明式)",
                                read_only=(access != "write"))
        raise ws.WorkspaceScopeError(
            ws.CODE_SCOPE_MISSING,
            "scoped execution requires a frozen WorkspaceContext; refusing the user root", scope)

    if scope == "current_task":
        if access == "write":
            await _assert_write_binding_current(data.get("db"), ctx)
        return ws.ScopeRoot(root=ctx.write_root, scope="current_task", task_id=ctx.task_id,
                            source="当前任务", read_only=False)

    if scope == "scratch":
        # §7.3：terminal / execute_code / process / skill_run_script 的默认 cwd 是
        # 当前 attempt 的 scratch（稳定 Task 写入必须走 controlled_publish）。
        # CF-1（①）：scratch 仍是**被放弃 attempt 子树**内的目录，写型解析同样必须过写围栏——
        # 否则回合中改绑后旧 ctx 仍能经 terminal/process/job 的 default_scope='scratch' 在被放弃
        # 的 attempt 树里 mkdir/写。读型解析（access='read'）不拦截（与 current_task 读同口径）。
        if access == "write":
            await _assert_write_binding_current(data.get("db"), ctx)
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
            if access == "write":
                await _assert_write_binding_current(db, ctx)
            return ws.ScopeRoot(root=ctx.write_root, scope="current_task", task_id=ctx.task_id,
                                source="当前任务", read_only=False)
        # Piece 2：read_root=user_root 且只读 → 跨任务文件读免 grant（root 仍是该任务的
        # workspace 子树，memory/staging/runs 在其外，天然不暴露）；read_root=task（默认）
        # 与既有 grant 路径逐字不变。
        grantfree = (str(getattr(ctx, "read_root", "task")) == "user_root" and access == "read")
        if grant_id:
            # 目录级放行：每个请求路径（含 files/folders 列表项）都过 allowed_paths 前缀校验
            for rel in requested_rels:
                check = await gs.verify_grant(db, grant_id, ctx=ctx, kind="files", mode="read",
                                              path=rel, count_audit=count_audit,
                                              requested_task_id=str(task_id))
                if not check.ok:
                    raise ws.WorkspaceScopeError(check.code or ws.CODE_CROSS_SCOPE_DENIED,
                                                 check.message, str(task_id))
        elif not grantfree:
            raise ws.WorkspaceScopeError(ws.CODE_CROSS_SCOPE_DENIED,
                                         "cross-task read needs a server-issued grant", str(task_id))
        from app.services import workspace_task_service as svc

        task = await svc.get_task(db, user_id=ctx.user_id, task_id=str(task_id))
        if task is None or str(task.status) == "deleted":
            # 免 grant 读不得触及回收站任务（与产出视图/任务级视图一致，grant 路径另有归档失效规则）
            raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "source task not found", str(task_id))
        # 新建读围栏（L9，DESIGN_20261108:294-301）：免 grant 跨任务读时，执行权威必须与
        # 源任务一致——cloud 权威的执行不得读 device 权威任务的产出（文件不在云上）。
        # 带 grant 的读是用户显式签发，已含授权，不在此围栏内。
        if grantfree and str(task.storage_authority or "cloud") != str(ctx.storage_authority):
            raise ws.WorkspaceScopeError(
                ws.CODE_RELATION_INVALID,
                "cross-task read blocked: storage authority mismatch", str(task_id))
        root = (ctx.user_root / Path(*task.root_rel.split("/")) / "workspace").resolve()
        if not ws.is_inside(ctx.user_root, root):
            raise ws.WorkspaceScopeError(ws.CODE_ROOT_INVALID, "source task root escapes user root",
                                         str(root))
        if grantfree and count_audit:
            # 免 grant 绕过了 grant 台账（verify_grant 的 count_audit），此处补一条 INFO 审计：
            # 「哪个任务的执行读了哪个任务的产出」必须可事后追溯（评审 r1 Important）。
            # count_audit 门控：resolve_scope_meta（徽标解析）会以 count_audit=False 再解析一次，不重复落审计（评审 r1 Minor）。
            logger.info(
                "workspace cross-task read (grant-free): user=%s current_task=%s -> source_task=%s "
                "rels=%s", ctx.user_id, ctx.task_id, task.id,
                ",".join(str(r) for r in requested_rels if r)[:200])
        return ws.ScopeRoot(root=root, scope="task", task_id=task.id,
                            source=f"任务:{task.title or task.id[:8]}", read_only=True,
                            grant_id=grant_id)

    # scope == "legacy"
    # Piece 2 决策（评审 r1 Critical 裁决，见 tests/decisions.md D-20261117-waveJ2-5）：
    # scope=legacy（整个用户根）的免 grant 读**本波不启用**——排除集只能校验请求前缀，而
    # grep/provide_folder 等递归工具会遍历用户根下的 memory 注入库（架空 kind=memory gate），
    # 且 `workspace/../memory` 借归一化前骗过 memory 豁免。安全启用需在工具侧对**每个遍历路径**
    # 过排除集（独立硬化波）。跨任务读现由 scope=task&task_id 覆盖（root=task/workspace，
    # memory/staging/runs 天然在其外），已满足「读别的任务的文件」主诉求。
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
    * `enforce` → 返回当前 Task 根（或经 grant 的只读跨域根）；缺 context 直接 raise，
      除非装配层声明了 `workspace_scoping_intent="legacy"`（那条走用户根并记 DEBUG）。

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
            ws.CODE_BINDING_STALE: "本轮任务绑定已变更（会话被改绑到新任务）。旧回合的执行域已封存，"
                                   "请在新回合继续写入——后续消息与文件会进入新任务。",
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
    # 写入永远只落当前任务（apply_scope 对 access=write 强制 scope=current_task/scratch，忽略模型
    # 给的 scope）。写操作没有「跨域读」来源可言——若这里照读 args 里的 scope=task&task_id，会对
    # 自带 stray scope 的写子命令（如 git commit）误触发跨任务解析→抛错→fail-closed 降级标记，
    # 把「自己任务的写结果」错标成 foreign/untrusted（enforce 下误拦后续 act 工具）。故写一律 None。
    if access == "write":
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
        # fail-closed：本工具第一次解析（apply_scope_or_error）已取到内容，第二次（徽标）解析此刻才失败
        # = 窄竞态（源任务被删/权威翻转/grant 撤销）。能走到这里的 scope 只可能是 task/project
        # （current_task/scratch/legacy 已在其上早返回 None）。返回保守标记（grant_id=None）让
        # classifier 降级 untrusted，避免跨任务内容因徽标解析失败而裸奔成 agent。
        return {"scope": scope, "source": "作用域解析失败(保守降级)",
                "task_id": str(data.get("task_id")) if data.get("task_id") else None,
                "grant_id": None}
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
