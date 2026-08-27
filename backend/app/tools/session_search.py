# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from typing import Optional

from sqlalchemy import text

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


def check_session_search_requirements() -> bool:
    return config.agent_session_search_enabled


async def session_search(args: dict, **kwargs) -> str:
    query = args.get("query", "").strip()
    limit = min(int(args.get("limit", 5)), 20)
    user = kwargs.get("user")
    db = kwargs.get("db")

    if not query:
        return json.dumps({"error": "Query is required"}, ensure_ascii=False)

    if not user or not db:
        return json.dumps({"error": "User authentication required for search"}, ensure_ascii=False)

    try:
        words = [w for w in query.split() if len(w) > 0]
        if not words:
            return json.dumps({"results": [], "query": query}, ensure_ascii=False)

        fts_query = " | ".join(words)

        result = await db.execute(
            text("""
                SELECT m.id, m.content, m.role, m.created_at,
                       c.id AS conversation_id, c.title AS conversation_title,
                       ts_rank(m.search_vector, to_tsquery('simple', :q)) AS rank
                FROM messages m
                JOIN conversations c ON m.conversation_id = c.id
                WHERE c.user_id = :uid
                  AND m.role IN ('user', 'assistant')
                  AND m.search_vector @@ to_tsquery('simple', :q)
                ORDER BY rank DESC
                LIMIT :lim
            """),
            {"uid": user.id, "q": fts_query, "lim": limit},
        )
        rows = result.fetchall()

        if not rows:
            result2 = await db.execute(
                text("""
                    SELECT m.id, m.content, m.role, m.created_at,
                           c.id AS conversation_id, c.title AS conversation_title
                    FROM messages m
                    JOIN conversations c ON m.conversation_id = c.id
                    WHERE c.user_id = :uid
                      AND m.role IN ('user', 'assistant')
                      AND m.content ILIKE :pattern
                    ORDER BY m.created_at DESC
                    LIMIT :lim
                """),
                {"uid": user.id, "pattern": f"%{query}%", "lim": limit},
            )
            rows2 = result2.fetchall()
            results = []
            for row in rows2:
                results.append({
                    "message_id": row[0],
                    "content": (row[1] or "")[:500],
                    "role": row[2],
                    "created_at": str(row[3]),
                    "conversation_id": row[4],
                    "conversation_title": row[5] or "",
                })
            return json.dumps({
                "results": results,
                "query": query,
                "search_mode": "fallback_ilike",
            }, ensure_ascii=False)

        results = []
        for row in rows:
            results.append({
                "message_id": row[0],
                "content": (row[1] or "")[:500],
                "role": row[2],
                "created_at": str(row[3]),
                "conversation_id": row[4],
                "conversation_title": row[5] or "",
                "relevance_score": round(float(row[6]), 4),
            })

        return json.dumps({
            "results": results,
            "query": query,
            "search_mode": "fts",
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("Session search failed")
        return json.dumps({"error": f"Search failed: {str(e)}"}, ensure_ascii=False)


registry.register(
    name="session_search",
    toolset="core",
    schema={
        "name": "session_search",
        "description": (
            "Search your past conversations for relevant information. "
            "Use this to find solutions, answers, or context from previous chats. "
            "Returns matching messages with conversation titles and relevance scores."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — words to find in past conversations. Use keywords for best results.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-20, default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    handler=session_search,
    check_fn=check_session_search_requirements,
    is_async=True,
    description="Search past conversations for relevant information",
    emoji="",
)
