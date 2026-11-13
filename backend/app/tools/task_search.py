# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`task_catalog_search` / `task_memory_search` — 任务层只读检索（2026-11-10 波 · T10）。

设计契约 §7.4 / §7.5 / §13.2：

* `task_catalog_search` = 跨 Task **阶段 A**：只返回标题/目标/摘要/状态/时间/artifact
  manifest 与匹配原因，**不读文件正文**；
* `task_memory_search` = **阶段 B** 的任务工程记忆读取：只返回 vibeweaver 层级
  （status/origin/taint/run/task/path/excerpt），**不返回系统 concept 字段**；
* 跨 Task（`scope=task`）与跨 Project 必须持服务端签发的 grant；
* 检索不是授权：命中不提升 taint、不写系统记忆、不改变当前绑定。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from app.services import workspace_memory_service as wmem
from app.services import workspace_scope as ws
from app.services import workspace_scope_service as svs

logger = logging.getLogger(__name__)


def _err(message: str, code: str = "") -> str:
    return json.dumps({"error": message, "code": code, "success": False}, ensure_ascii=False)


def _ctx_or_error(kwargs: Dict[str, Any]) -> tuple[Any, str | None]:
    ctx = kwargs.get("workspace_ctx")
    if not isinstance(ctx, ws.WorkspaceContext):
        if svs.scoping_mode() == "enforce":
            return None, _err("任务作用域未初始化（本轮无 Task 上下文）。", ws.CODE_SCOPE_MISSING)
        return None, _err("当前会话未绑定任务，无法检索任务目录。", ws.CODE_SCOPE_MISSING)
    return ctx, None


async def task_catalog_search(args: Dict[str, Any], **kwargs) -> str:
    """阶段 A：任务目录检索（只元数据）。"""
    ctx, err = _ctx_or_error(kwargs)
    if err:
        return err
    db = kwargs.get("db")
    if db is None:
        return _err("缺少数据库上下文。")
    from app.services import workspace_task_service as svc

    scope = str(args.get("scope") or "current_project").strip()
    if scope not in ("current_project", "project"):
        return _err("scope 只能是 current_project 或 project。", ws.CODE_RELATION_INVALID)
    project_id = args.get("project_id") or (ctx.project_id if scope == "current_project" else None)
    if scope == "project" and project_id and str(project_id) != str(ctx.project_id):
        # 跨 Project 目录检索需要 catalog grant
        grant_id = svs.grant_id_from(kwargs) or str(args.get("grant_id") or "").strip()
        if not grant_id:
            return _err("跨 Project 目录检索需要用户显式授权（kind=catalog grant）。",
                        ws.CODE_CROSS_SCOPE_DENIED)
        from app.services.cross_scope_grant_service import verify_grant

        check = await verify_grant(db, grant_id, ctx=ctx, kind="catalog", mode="read",
                                   requested_project_id=str(project_id))
        if not check.ok:
            return _err(check.message or "grant 不适用。", check.code or ws.CODE_CROSS_SCOPE_DENIED)
    try:
        limit = max(1, min(int(args.get("limit") or 6), 20))
    except (TypeError, ValueError):
        limit = 6

    tasks = await svc.list_tasks(db, user_id=ctx.user_id, project_id=str(project_id or ""),
                                 statuses=("active", "completed"), limit=limit * 4)
    query = str(args.get("query") or "").strip()
    from app.services.task_binding_service import _score

    out: list[dict[str, Any]] = []
    for task in tasks:
        score, reason = _score(query, title=task.title, goal=task.goal, summary=task.summary,
                               tags=task.tags_json)
        if query and score <= 0:
            continue
        try:
            manifest = json.loads(task.artifact_manifest_json or "[]")
        except ValueError:
            manifest = []
        out.append({
            "task_id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "goal": (task.goal or "")[:200],
            "summary": (task.summary or "")[:200],
            "status": task.status,
            "last_active_at": task.last_active_at.isoformat() if task.last_active_at else None,
            "match_reason": reason,
            "score": round(score, 3),
            "artifacts": [str(m.get("path")) for m in manifest[:5] if isinstance(m, dict)],
            "read_only": True,
            "note": "目录检索只含元数据；需要文件内容请选定后用文件工具或 task_memory_search。",
        })
        if len(out) >= limit:
            break
    return json.dumps({"success": True, "count": len(out), "results": out,
                       "current_task_id": ctx.task_id,
                       "binding_unchanged": True}, ensure_ascii=False)


async def task_memory_search(args: Dict[str, Any], **kwargs) -> str:
    """阶段 B：任务/项目工程记忆检索（vibeweaver 层级，不返回系统 concept 字段）。"""
    ctx, err = _ctx_or_error(kwargs)
    if err:
        return err
    db = kwargs.get("db")
    if db is None:
        return _err("缺少数据库上下文。")
    scope = str(args.get("scope") or "current_task").strip()
    if scope not in ("current_task", "current_project", "task"):
        return _err("scope 只能是 current_task / current_project / task。",
                    ws.CODE_RELATION_INVALID)
    include_history = bool(args.get("include_history"))
    try:
        limit = max(1, min(int(args.get("limit") or 6), 20))
    except (TypeError, ValueError):
        limit = 6
    query = str(args.get("query") or "").strip()

    if scope == "current_project":
        results = wmem.search(memory_dir=ctx.project_memory_root, query=query, limit=limit,
                              include_history=include_history)
        level = "project"
        ref_prefix = wmem.project_ref(ctx.project_id, "")
    else:
        task_id = ctx.task_id if scope == "current_task" else str(args.get("task_id") or "")
        if not task_id:
            return _err("scope=task 需要 task_id。", ws.CODE_RELATION_INVALID)
        if str(task_id) != str(ctx.task_id):
            grant_id = svs.grant_id_from(kwargs) or str(args.get("grant_id") or "").strip()
            if not grant_id:
                return _err("读取其他任务的记忆需要用户显式授权（kind=memory grant）。",
                            ws.CODE_CROSS_SCOPE_DENIED)
            from app.services.cross_scope_grant_service import verify_grant

            check = await verify_grant(db, grant_id, ctx=ctx, kind="memory", mode="read",
                                       requested_task_id=str(task_id))
            if not check.ok:
                return _err(check.message or "grant 不适用。",
                            check.code or ws.CODE_CROSS_SCOPE_DENIED)
            from app.services import workspace_task_service as svc

            task = await svc.get_task(db, user_id=ctx.user_id, task_id=task_id)
            if task is None:
                return _err("来源任务不存在。", ws.CODE_FOREIGN_SCOPE)
            memory_dir = ctx.user_root / Path(*task.root_rel.split("/")) / "memory"
            level = "task"
            ref_prefix = wmem.task_ref(task_id, "")
        else:
            memory_dir = ctx.task_memory_root
            level = "task"
            ref_prefix = wmem.task_ref(ctx.task_id, "")
        results = wmem.search(memory_dir=memory_dir, query=query, limit=limit,
                              include_history=include_history)

    for item in results:
        item["ref"] = ref_prefix + str(item.get("name"))
        item["level"] = level
        item["read_only"] = True
        # 明确声明：这些状态不是系统记忆的 verification_state / invalid_reason
        item["layer"] = "workspace_task_memory"
    return json.dumps({
        "success": True, "count": len(results), "results": results,
        "current_task_id": ctx.task_id, "binding_unchanged": True,
        "note": "任务/项目工程记忆；与系统长期记忆（memory_search）是不同层，不互相认证。",
    }, ensure_ascii=False)


def _enabled() -> bool:
    return svs.scoping_mode() != "off"


from app.tools.registry import registry  # noqa: E402

registry.register(
    name="task_catalog_search",
    toolset="core",
    is_async=True,
    check_fn=_enabled,
    schema={
        "name": "task_catalog_search",
        "description": (
            "Read-only search over the task catalog (titles, goals, summaries, status, "
            "artifact manifest). It never reads file bodies and never changes the current "
            "task binding. Use it first to find candidate tasks, then read specific files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to match task metadata."},
                "scope": {"type": "string", "enum": ["current_project", "project"],
                          "description": "Default current_project."},
                "project_id": {"type": "string",
                               "description": "Required for other projects (needs a catalog grant)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    handler=task_catalog_search,
)

registry.register(
    name="task_memory_search",
    toolset="core",
    is_async=True,
    check_fn=_enabled,
    schema={
        "name": "task_memory_search",
        "description": (
            "Read-only search over vibeweaver task/project engineering memory "
            "(status ⛔/❌/⏳/✅, origin, taint, source run/attempt, excerpt). These are NOT "
            "system long-term-memory facts: results cannot certify 'remembered'/'forgotten' "
            "claims. Reading another task's memory needs a user-issued grant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords for topic names/bodies."},
                "scope": {"type": "string",
                          "enum": ["current_task", "current_project", "task"],
                          "description": "Default current_task."},
                "task_id": {"type": "string",
                            "description": "Required when scope=task (needs a memory grant)."},
                "include_history": {"type": "boolean",
                                    "description": "Include quarantined/retracted topics."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
    handler=task_memory_search,
)
