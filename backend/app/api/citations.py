# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Citation style rendering API — 引用样式目录 + 渲染服务（设计 §5.2/§7）。

GET  /api/citation-styles        可用样式目录（id/名称/学科建议）
POST /api/citations/render       [N] → 样式化文内引用 + 参考文献节
"""
import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.deps import get_current_user
from app.db.database import User
from app.services.citation_style_service import list_styles, render_citations

logger = logging.getLogger(__name__)
router = APIRouter(tags=["citations"])


class CitationRenderRequest(BaseModel):
    content: str = Field(..., description="含 [N] 角标的正文（Markdown）")
    style_id: Optional[str] = Field(None, description="样式逻辑 id；缺省走存档汇点默认")
    tool_results: Optional[Union[str, List[Dict[str, Any]]]] = Field(
        None, description="消息 tool_results JSON（web_search results 数组）或直接条目列表"
    )
    entries: Optional[List[Dict[str, Any]]] = Field(
        None, description="直接条目列表 [{id,url,title,published_date,author,site_name}]"
    )
    accessed: Optional[str] = Field(None, description="引用日期 YYYY-MM-DD；缺省当天")


def _entries_from_tool_results(tool_results: Any) -> List[Dict[str, Any]]:
    """tool_results JSON → 条目列表（位置序号=台账 id，与 chips 映射同约定）。"""
    data = tool_results
    if isinstance(tool_results, str):
        try:
            data = json.loads(tool_results)
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


@router.get("/api/citation-styles")
async def get_citation_styles(user: User = Depends(get_current_user)):
    """可用引用样式目录（供助手配置 UI 与死磕规划）。"""
    return {"styles": list_styles()}


@router.post("/api/citations/render")
async def post_citation_render(req: CitationRenderRequest, user: User = Depends(get_current_user)):
    """把正文 [N] 角标按目标样式渲染为文内引用 + 参考文献节（含 F1 剥节）。"""
    entries = list(req.entries or [])
    if req.tool_results is not None:
        entries = entries + _entries_from_tool_results(req.tool_results)
    accessed = None
    if req.accessed:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", req.accessed)
        if m:
            accessed = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    r = await asyncio.to_thread(render_citations, req.content, entries, style_id=req.style_id, accessed=accessed)
    return {
        "content": r.content,
        "bibliography_section": r.bibliography_section,
        "notes_section": r.notes_section,
        "style_id": r.style_id,
        "family": r.family,
        "used_ids": r.used_ids,
    }
