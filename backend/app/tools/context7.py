# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from typing import Optional

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

CONTEXT7_MCP_URL = "https://mcp.context7.com/mcp"


def check_context7_requirements() -> bool:
    return config.web_search_enabled


async def context7_resolve_library_id(args: dict, **kwargs) -> str:
    library_name = args.get("libraryName", "")
    query = args.get("query", library_name)
    if not library_name and not query:
        return json.dumps({"error": "libraryName or query is required"}, ensure_ascii=False)

    from app.tools.mcp_client import call_mcp_tool

    api_key = config.context7_api_key
    arguments = {"query": query}
    if library_name:
        arguments["libraryName"] = library_name

    try:
        result = await call_mcp_tool(CONTEXT7_MCP_URL, "resolve-library-id", arguments, api_key=api_key)
        return result
    except Exception as e:
        logger.exception("Context7 resolve-library-id failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def context7_query_docs(args: dict, **kwargs) -> str:
    library_id = args.get("libraryId", "")
    query = args.get("query", "")
    tokens = args.get("tokens", 10000)
    if not library_id:
        return json.dumps({"error": "libraryId is required"}, ensure_ascii=False)
    if not query:
        return json.dumps({"error": "query is required"}, ensure_ascii=False)

    from app.tools.mcp_client import call_mcp_tool

    api_key = config.context7_api_key
    arguments = {
        "libraryId": library_id,
        "query": query,
        "tokens": tokens,
    }

    try:
        result = await call_mcp_tool(CONTEXT7_MCP_URL, "query-docs", arguments, api_key=api_key)
        return result
    except Exception as e:
        logger.exception("Context7 query-docs failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


registry.register(
    name="context7_resolve_library_id",
    toolset="docs",
    schema={
        "name": "context7_resolve_library_id",
        "description": "Resolve a library/framework name to a Context7-compatible library ID. Use this FIRST before calling context7_query_docs. Returns matching libraries with their IDs, descriptions, and available versions.",
        "parameters": {
            "type": "object",
            "properties": {
                "libraryName": {
                    "type": "string",
                    "description": "The name of the library or framework to look up (e.g., 'FastAPI', 'React', 'LangChain').",
                },
                "query": {
                    "type": "string",
                    "description": "Optional: Additional context about what you need from the library, used to rank results by relevance.",
                },
            },
            "required": ["libraryName"],
        },
    },
    handler=context7_resolve_library_id,
    check_fn=check_context7_requirements,
    is_async=True,
    description="Context7: resolve library ID",
    emoji="",
)

registry.register(
    name="context7_query_docs",
    toolset="docs",
    schema={
        "name": "context7_query_docs",
        "description": "Query up-to-date documentation and code examples for a specific library. Requires a libraryId obtained from context7_resolve_library_id. Returns relevant documentation snippets, API references, and code examples.",
        "parameters": {
            "type": "object",
            "properties": {
                "libraryId": {
                    "type": "string",
                    "description": "Context7-compatible library ID (e.g., '/fastapi/fastapi', '/vercel/next.js'). Obtain this from context7_resolve_library_id first.",
                },
                "query": {
                    "type": "string",
                    "description": "Specific topic or question about the library (e.g., 'routing', 'authentication middleware', 'database connection').",
                },
                "tokens": {
                    "type": "integer",
                    "description": "Maximum number of tokens to return. Default: 10000.",
                },
            },
            "required": ["libraryId", "query"],
        },
    },
    handler=context7_query_docs,
    check_fn=check_context7_requirements,
    is_async=True,
    description="Context7: query library docs",
    emoji="",
)
