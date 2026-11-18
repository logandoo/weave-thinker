# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""citation_render — 按学术引用样式渲染文本/工作区文件中的 [N] 并生成标准参考文献节。

死磕论文/报告类文件产物的完稿工具（设计 §5.4）：正文保持 [N] 撰写语法，完稿时
调用本工具一次，输出标准文内引用 + 参考文献节。来源条目=本会话 web_search 检索
结果（current_turn_tool_results），杜绝模型手写参考文献（反幻觉：文献表由台账
机械生成）。

用法（agent 工具）：
- file_path: 工作区文件路径（md/txt）——渲染后原地覆写
- content: 直接传文本（与 file_path 二选一）——只返回渲染结果不落盘
- style_id: 可选样式 id（见 GET /api/citation-styles）；缺省=助手/全局默认
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _entries_from_turn_tool_results(raw: str) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    results = data if isinstance(data, list) else (data.get("results") or []) if isinstance(data, dict) else []
    entries = []
    for i, r in enumerate(results):
        if not isinstance(r, dict) or not r.get("url"):
            continue
        entries.append({
            "id": r.get("id") if isinstance(r.get("id"), int) else i + 1,
            "url": r.get("url"),
            "title": r.get("title") or "",
            "snippet": r.get("snippet") or "",
            "published_date": r.get("published_date") or "",
            "author": r.get("author") or "",
            "site_name": r.get("site_name") or "",
            "type": r.get("type") or "",
        })
    return entries


async def citation_render(args: dict, **kwargs) -> str:
    from app.services.citation_style_service import render_citations, resolve_style_id

    file_path = str(args.get("file_path") or "").strip()
    content = args.get("content")
    style_id = str(args.get("style_id") or "").strip() or None
    if not style_id:
        # A4.9 ①：缺省回落助手 citation_style > 全局默认
        style_id = getattr(kwargs.get("assistant"), "citation_style", None) or None

    entries = _entries_from_turn_tool_results(kwargs.get("current_turn_tool_results", ""))
    _src_meta = None  # 跨域读来源标记（仅 file_path 分支会解析；content-only 路径恒 None）

    try:
        if file_path:
            from app.core.config import get_config
            from app.services.workspace_service import ensure_user_workspace
            from app.services.workspace_paths import WorkspacePathError, resolve_workspace_file

            user = kwargs.get("user")
            db = kwargs.get("db")
            close_db = False
            if db is None:
                from app.db.database import AsyncSessionLocal
                db = AsyncSessionLocal()
                close_db = True
            if user is None:
                return json.dumps({"error": "citation_render: 缺少 user 上下文"}, ensure_ascii=False)
            try:
                workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
                workspace_root = str(Path(workspace.root_path).resolve())
                from app.services.workspace_scope_service import apply_scope_or_error, resolve_scope_meta
                workspace_root, _scope_err = await apply_scope_or_error(kwargs, default_root=workspace_root, args=args, access='read')
                if _scope_err:
                    return json.dumps(_scope_err, ensure_ascii=False)
                # Piece 2 J2d：跨域读要带来源标记（含 grant_id），供 taint 重判免 grant 跨任务读不携带 agent 权威
                _src_meta = await resolve_scope_meta(kwargs, args=args, access="read")

                try:
                    resolved = resolve_workspace_file(file_path, workspace_root)
                except WorkspacePathError as exc:
                    return json.dumps({"error": f"citation_render: {exc}"}, ensure_ascii=False)
                if resolved is None:
                    return json.dumps({"error": f"citation_render: 文件不存在 {file_path}"}, ensure_ascii=False)
                raw = await asyncio.to_thread(Path(resolved).read_text, "utf-8")
            finally:
                if close_db:
                    await db.close()
        elif content is not None and str(content).strip():
            raw = str(content)
            resolved = None
        else:
            return json.dumps({"error": "citation_render: 需要 file_path 或 content"}, ensure_ascii=False)

        rendered = await asyncio.to_thread(render_citations, raw, entries, style_id=style_id)
        if not rendered.bibliography_section:
            _resp = {
                "success": True,
                "style_id": rendered.style_id,
                "family": rendered.family,
                "used_ids": rendered.used_ids,
                "message": "正文无 [N] 引用标记，未生成参考文献节（未引用来源不会入表）",
                "content": rendered.content if resolved is None else None,
            }
            if _src_meta:
                _resp["source_scope"] = _src_meta["scope"]
                _resp["source_grant_id"] = _src_meta.get("grant_id")
            return json.dumps(_resp, ensure_ascii=False)

        final_text = rendered.content + rendered.bibliography_section
        wrote_back = False
        if resolved is not None:
            # §7.2 任务作用域内：read-scope 工具绝不得覆写别的任务的文件（user_root 免 grant 或
            # grant 跨任务读时 resolved 可能指向别的任务根）。无 ctx 的链路（off/shadow/刻意 legacy-intent）
            # 本就以**用户根**为写域——同回合 workspace_write 也能写该处，citation_render 不另造权限；
            # 见 AGENTS.md「enforce 对未绑定会话不隔离用户根」既有裁定，不在此围栏内。
            # ctx 在却缺 write_root = 异常态 → fail-closed 不写。
            _ctx = kwargs.get("workspace_ctx")
            if _ctx is None:
                _foreign = False
            elif not getattr(_ctx, "write_root", None):
                _foreign = True
            else:
                try:
                    Path(resolved).resolve().relative_to(Path(_ctx.write_root).resolve())
                    _foreign = False
                except (ValueError, OSError, RuntimeError):
                    _foreign = True
            if _foreign:
                logger.info("citation_render: skipped cross-task write-back to %s (read scope)", resolved)
            else:
                await asyncio.to_thread(Path(resolved).write_text, final_text, "utf-8")
                wrote_back = True

        _resp = {
            "success": True,
            "style_id": rendered.style_id,
            "family": rendered.family,
            "used_ids": rendered.used_ids,
            "file_path": str(resolved) if resolved is not None else None,
            "message": (
                f"已按 {rendered.style_id} 渲染 {len(rendered.used_ids)} 条引用"
                + (f"并覆写 {file_path}" if wrote_back else
                   ("（跨任务只读，未覆写，渲染结果见 content）" if resolved is not None else ""))
            ),
            "content": final_text if not wrote_back else None,
        }
        if _src_meta:
            _resp["source_scope"] = _src_meta["scope"]
            _resp["source_grant_id"] = _src_meta.get("grant_id")
        return json.dumps(_resp, ensure_ascii=False)
    except Exception as exc:
        logger.exception("citation_render failed")
        return json.dumps({"error": f"citation_render failed: {exc}"}, ensure_ascii=False)


registry.register(
    name="citation_render",
    toolset="web",
    schema={
        "name": "citation_render",
        "description": (
            "按学术引用格式（APA/GB/T 7714/Chicago/IEEE/Vancouver 等）渲染正文中的 "
            "[N] 引用角标并生成标准参考文献节。论文/报告类文件产物完稿时必须调用"
            "（不要手写参考文献）。来源=本会话 web_search 结果，未引用的来源不会"
            "出现在参考文献表中。file_path 渲染后原地覆写；content 只返回渲染结果。"
            "style_id 可选：gb-t-7714-numeric（默认）/apa/mla/chicago-notes/"
            "chicago-author-date/ieee/vancouver/ama/harvard/gb-t-7714-author-date 等"
            "（完整目录见 GET /api/citation-styles）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "工作区文件路径（如 report.md）。与 content 二选一。",
                },
                "content": {
                    "type": "string",
                    "description": "直接传入正文文本。与 file_path 二选一。",
                },
                "style_id": {
                    "type": "string",
                    "description": "引用样式 id，缺省=助手/全局默认（gb-t-7714-numeric）。",
                },
            },
        },
    },
    handler=citation_render,
    is_async=True,
    description="学术引用渲染（[N] → 标准文内引用 + 参考文献节）",
    emoji="",
)
