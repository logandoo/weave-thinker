# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""search_tools — MCP 扩展工具的渐进式发现元工具（2026-09-02）。

MCP server（[mcp.servers.*]，toolset 前缀 "mcp-"）的工具 schema 默认不进入
每轮 LLM 请求（见 agent_loop.AgentLoop 延迟池）。模型通过本元工具按关键词
检索目录；AgentLoop 在工具结果返回后把匹配到的 schema 追加进后续迭代
（只增不减）。server 连接与注册保持常驻，延迟只作用于 schema 注入——
MCP 官方 client best practices 的 progressive discovery 模式。
"""
import json
import re
from typing import Any, Dict, List

from app.tools.registry import registry

_MCP_TOOLSET_PREFIX = "mcp-"

_SEARCH_TOOLS_SCHEMA = {
    "name": "search_tools",
    "description": (
        "按需检索并加载扩展工具（远程运维/MCP 服务器提供的专业工具等）。"
        "这类工具默认不在你的工具列表中：先用本工具按关键词检索（中英文均可），"
        "匹配到的工具会自动加载到本会话后续请求中，然后即可直接调用。"
        "例：需要操作远程服务器时 query='remote shell' 或 '上传文件'。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词，如 'remote shell'、'端口转发'、'sftp 上传'"},
            "server": {"type": "string", "description": "限定 MCP 服务器名（可选，如 'rempilot'）"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 5，上限 10"},
        },
        "required": ["query"],
    },
}

_LOADED_HINT = (
    "以上工具的完整定义已加载到当前会话，下一轮起可直接调用；"
    "未列出的工具可用其他关键词继续检索。"
)


def _mcp_entries(blocked: Any = None) -> List[Any]:
    """目录 = mcp-* toolset 条目；排除 loop 显式 blocked（M4）与 check_fn
    不可用条目（与 registry.get_definitions 口径一致）。"""
    blocked = set(blocked or ())
    out = []
    for name, toolset in registry.get_tool_to_toolset_map().items():
        if not toolset or not toolset.startswith(_MCP_TOOLSET_PREFIX):
            continue
        if name in blocked:
            continue
        entry = registry.get_entry(name)
        if entry is None:
            continue
        if entry.check_fn is not None:
            try:
                if not entry.check_fn():
                    continue
            except Exception:
                continue
        out.append(entry)
    return out


def _score(entry: Any, terms: List[str], raw: str) -> int:
    schema = entry.schema or {}
    name = str(schema.get("name", ""))
    # 描述双通道：schema.description（模型可见）+ entry.description（注册时的
    # 中文摘要，如 "MCP: remote_shell.open (rempilot)" 携带的原始工具语义）。
    desc = " ".join(filter(None, [str(schema.get("description", "")), str(entry.description or "")]))
    name_tokens = set(re.split(r"[_\W]+", name.lower())) - {""}
    desc_lower = desc.lower()
    score = 0
    for t in terms:
        if t in name_tokens:
            score += 3
        if t in name.lower():
            score += 1
        if t in desc_lower:
            score += 1
    if len(raw) >= 2 and raw.lower() in desc_lower:
        score += 2
    return score


async def search_tools_handler(args: Dict[str, Any], **kwargs) -> str:
    query = str(args.get("query", "") or "").strip()
    server_filter = str(args.get("server", "") or "").strip()
    try:
        limit = int(args.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 10))

    if not query:
        return json.dumps({"matches": [], "loaded_hint": "query 不能为空"}, ensure_ascii=False)

    terms = [t for t in re.split(r"[\s,，;；]+", query.lower()) if t]
    scored = []
    for entry in _mcp_entries(kwargs.get("_blocked_tool_names")):
        server = (entry.toolset or "")[len(_MCP_TOOLSET_PREFIX):]
        if server_filter and server != server_filter:
            continue
        s = _score(entry, terms, query)
        if s <= 0:
            continue
        schema = entry.schema or {}
        scored.append((s, {
            "name": schema.get("name", entry.name),
            "description": schema.get("description", entry.description or ""),
            "server": server,
        }))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    matches = [m for _, m in scored[:limit]]
    return json.dumps({"matches": matches, "loaded_hint": _LOADED_HINT}, ensure_ascii=False)


registry.register(
    name="search_tools",
    toolset="core",
    schema=_SEARCH_TOOLS_SCHEMA,
    handler=search_tools_handler,
    is_async=True,
    description="检索并按需加载 MCP 扩展工具",
    emoji="🔍",
)
