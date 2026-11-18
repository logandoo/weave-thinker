# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`workspace_task_files` — 列任务文件 / 本会话产出（只读，§4.10 / §6）。

* 任务文件 = 任务根 `workspace/{inputs,artifacts}` 的目录列表；
* 「本会话产出」= `.vibeweaver/workspace-change-manifest.jsonl` 中
  run→conversation 关联的 best-effort 过滤（不是另一份存储）。
* 目标解析：`task_id` 直取 / 缺省取当前会话绑定。

安全定位（评审 J2d 裁定）：本工具是**只读元数据列表**面（文件名/大小/mtime，非文件内容），
且严格 owner-scoped（`get_task(user_id=...)` 过滤 + 拒 deleted/foreign）。它是 Piece 1「跨任务
看我自己的产出」列表能力的一部分，**不经 `apply_scope_or_error`、不受 `read_root` 门控是刻意设计**
（列自己的任务文件不是跨租户读，也不是把 foreign 内容灌进上下文）。故不纳入免 grant 跨任务读
taint 降级元组；真正的 foreign **内容**读围栏在 `resolve_tool_root`。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
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


async def workspace_task_files(args: dict, **kwargs) -> str:
    db = kwargs.get("db")
    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    if db is None or user is None:
        return _error("缺少会话上下文", "workspace_scope_missing")
    from app.services import task_binding_service as tbs
    from app.services import workspace_files_service as wfs
    from app.services import workspace_task_service as svc
    from app.services.workspace_service import ensure_user_workspace

    task_id = str(args.get("task_id") or "").strip()
    conv_id = str(getattr(conversation, "id", "") or "")
    try:
        if not task_id and conv_id:
            binding = await tbs.get_binding(db, user_id=user.id, conversation_id=conv_id)
            task_id = str(binding.task_id) if binding is not None else ""
        if not task_id:
            return _error("没有可用任务（请提供 task_id 或先绑定会话）",
                          "workspace_scope_missing")
        task = await svc.get_task(db, user_id=user.id, task_id=task_id)
        if task is None:
            return _error("任务不存在或不属于当前用户", "workspace_foreign_scope")
        if str(task.status) == "deleted":
            return _error("任务已删除（在回收站）", "workspace_relation_invalid")
        workspace = await ensure_user_workspace(db, user.id, user.username)
        if workspace is None:
            return _error("workspace 未初始化", "workspace_scope_missing")
        root = Path(workspace.root_path).resolve()
        produced_only = bool(args.get("produced_only"))
        if produced_only and conv_id:
            # 归属视图走**跨任务**函数 + `only_task_id`（2026-11-18 用户裁定
            # D-20261117-waveJ2-9「用户要 Agent 在自己任务间自由翻文件」+ 60 事故 conv 389a2496）。
            # 为什么这样两边都对：跨任务函数给每条条目打上**文件真实落点**的 task 归属——
            # ① 改绑后被 `migrate_orphaned_task_files` 迁进本任务的历史产出 → 归属=本任务 → 列得出
            #    （此前单任务 join 查不到，agent 与用户面板看到的不一样的那个洞）；
            # ② 仍留在别的任务里的产出 → 归属=那个任务 → 被 `only_task_id` 挡掉
            #    （CA-2 钉的「查 B 不见 A」继续成立，`test_task_files_tool_stays_single_task_after_rebind` 不动）。
            # 过滤必须在函数**内部、截断之前**做（评审 I1）：先 `out[:limit]` 后过滤会把被询问
            # 任务的行整批挤掉 = 静默空列表，正是本波要修的 bug 换个阈值复发。
            produced = await wfs.async_conversation_produced_files_across_tasks(
                db, user_id=user.id, user_root=root, conversation_id=conv_id,
                only_task_id=str(task.id))
            return json.dumps({
                "ok": True, "success": True, "task_id": task.id,
                "display_root": f"{task.root_rel}/workspace",
                "produced_only": True, "conversation_id": conv_id,
                "produced": produced,
                # 命中上限就给信号：此前两条路径都不报截断，agent 会把半份清单当全部。
                "truncated": len(produced) >= 500,
            }, ensure_ascii=False)
        task_root = ws._resolve_no_redirect(root, task.root_rel, what="task root")
        try:
            listing = wfs.list_directory(task_root, "workspace")
        except wfs.WorkspacePathError as exc:
            return _error(str(exc), getattr(exc, "code", "workspace_root_invalid"))
        return json.dumps({
            "ok": True, "success": True, "task_id": task.id,
            "display_root": f"{task.root_rel}/workspace",
            "produced_only": False, "conversation_id": conv_id or None,
            "listing": listing,
        }, ensure_ascii=False)
    except ws.WorkspaceScopeError as exc:
        return _error(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败不掩盖原始错误
            pass
        logger.exception("workspace_task_files failed")
        return _error(f"列任务文件失败（{type(exc).__name__}）", "workspace_task_files_failed")


registry.register(
    name="workspace_task_files",
    toolset="files",
    schema={
        "name": "workspace_task_files",
        "description": (
            "列出任务文件（任务根 workspace/{inputs,artifacts}）。"
            "produced_only=true 时只看**本会话在该任务的产出**（change-manifest best-effort 过滤；"
            "含改绑后被迁移到该任务的历史产出——仅当该任务是本会话**当前绑定**任务时可见）。"
            "task_id 缺省取当前会话绑定。只读。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 id（缺省=当前会话绑定）"},
                "produced_only": {"type": "boolean",
                                  "description": "只看本会话产出（默认 false）"},
            },
            "required": [],
        },
    },
    handler=workspace_task_files,
    is_async=True,
    check_fn=_available,
    description="List workspace task files",
    emoji="",
)

__all__ = ["workspace_task_files"]
