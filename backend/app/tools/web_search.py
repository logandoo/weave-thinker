# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from app.tools.registry import registry
from app.services.search_service import WebSearchService, _fetch_dates_for_hits
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


def check_web_search_requirements() -> bool:
    return config.web_search_enabled and WebSearchService().is_available


async def web_search(args: dict, **kwargs) -> str:
    queries = args.get("queries", [])
    if isinstance(queries, str):
        queries = [queries]
    if not queries:
        return json.dumps({"error": "No search queries provided"}, ensure_ascii=False)

    search_service = WebSearchService()
    if not search_service.is_available:
        return json.dumps({"error": "Search provider not configured"}, ensure_ascii=False)

    user = kwargs.get("user")
    conversation = kwargs.get("conversation")
    user_id = getattr(user, "id", None) if user else None
    conversation_id = getattr(conversation, "id", None) if conversation else None

    all_hits = await search_service.search_multiple(queries)
    # 日期富化先于落库：httpx 提取的 published_date 也要进 web_search_results
    await _fetch_dates_for_hits(all_hits)
    # persist_hits 自开独立会话，不触碰调用方 session（voice_service 长生命周期
    # session 共享场景下，失败回滚也不会污染对方的未提交工作）
    await search_service.persist_hits(all_hits, user_id=user_id, conversation_id=conversation_id)

    return json.dumps({
        "count": len(all_hits),
        "results": [h.to_dict() for h in all_hits],
        "formatted": search_service.format_hits(all_hits),
    }, ensure_ascii=False)


registry.register(
    name="web_search",
    toolset="web",
    schema={
        "name": "web_search",
        "description": "Search the web for current information, news, facts, and data. Returns titles, URLs, and snippets from relevant web pages.",
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Search queries (up to 3). Use specific keywords for best results. Both Chinese and English queries are supported.",
                },
            },
            "required": ["queries"],
        },
    },
    handler=web_search,
    check_fn=check_web_search_requirements,
    is_async=True,
    description="Web search (Exa/Bocha/Firecrawl/Tavily/Serper)",
    emoji="",
)
