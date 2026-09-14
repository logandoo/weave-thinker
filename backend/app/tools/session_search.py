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
    mode = str(args.get("mode") or "search").strip().lower()
    user = kwargs.get("user")
    db = kwargs.get("db")

    # F2（2026-09-14）：read 模式——确定性回读窗口外消息（(created_at,id) 游标，
    # 不重不漏）；search 模式行为与旧实现一致。
    if mode == "read":
        conversation_id = str(args.get("conversation_id") or "").strip()
        if not conversation_id:
            return json.dumps({"error": "conversation_id is required for mode=read"},
                              ensure_ascii=False)
        if not user or not db:
            return json.dumps({"error": "User authentication required for read"},
                              ensure_ascii=False)
        try:
            read_limit = max(1, min(int(args.get("read_limit") or 20), 50))
            params: dict = {"uid": user.id, "cid": conversation_id, "lim": read_limit}
            clauses = []
            if args.get("before_id"):
                clauses.append("AND (m.created_at, m.id) < (SELECT created_at, id FROM messages WHERE id = :before_id AND conversation_id = :cid)")
                params["before_id"] = str(args["before_id"])
            if args.get("after_id"):
                clauses.append("AND (m.created_at, m.id) > (SELECT created_at, id FROM messages WHERE id = :after_id AND conversation_id = :cid)")
                params["after_id"] = str(args["after_id"])
            clause = " ".join(clauses)
            # A4.9 Minor：after_id 正向分页必须 ASC（DESC+LIMIT 会返回游标后
            # 最新的 N 条，静默跳过中间消息）；before_id/无游标保持 DESC 取窗后反转
            forward = bool(args.get("after_id"))
            order = "ASC" if forward else "DESC"
            rows = (await db.execute(
                text(f"""
                    SELECT m.id, m.content, m.role, m.created_at
                    FROM messages m
                    JOIN conversations c ON m.conversation_id = c.id
                    WHERE c.user_id = :uid AND m.conversation_id = :cid
                      AND m.role IN ('user', 'assistant') {clause}
                    ORDER BY m.created_at {order}, m.id {order}
                    LIMIT :lim
                """),
                params,
            )).fetchall()
            ordered_rows = list(rows) if forward else list(reversed(rows))
            results = [{
                "message_id": r[0],
                "content": (r[1] or "")[:2000],
                "role": r[2],
                "created_at": str(r[3]),
            } for r in ordered_rows]
            return json.dumps({
                "results": results,
                "mode": "read",
                "conversation_id": conversation_id,
                "count": len(results),
            }, ensure_ascii=False)
        except Exception as e:
            logger.exception("Session read failed")
            return json.dumps({"error": f"Read failed: {str(e)}"}, ensure_ascii=False)

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
            "Search your past conversations for relevant information, or read a "
            "deterministic window of one conversation. Use search mode to find "
            "solutions, answers, or context from previous chats (returns matching "
            "messages with conversation titles and relevance scores). Use read "
            "mode with conversation_id (+ optional before_id/after_id cursors) to "
            "read older messages outside the current context window."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — words to find in past conversations. Use keywords for best results. Required for mode=search; ignored for mode=read.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of search results to return (1-20, default 5).",
                    "default": 5,
                },
                "mode": {
                    "type": "string",
                    "enum": ["search", "read"],
                    "description": "search (default) = keyword search; read = deterministic window read of one conversation.",
                    "default": "search",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Target conversation id (required for mode=read).",
                },
                "before_id": {
                    "type": "string",
                    "description": "mode=read cursor: return messages strictly older than this message id.",
                },
                "after_id": {
                    "type": "string",
                    "description": "mode=read cursor: return messages strictly newer than this message id.",
                },
                "read_limit": {
                    "type": "integer",
                    "description": "mode=read window size (1-50, default 20).",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
    handler=session_search,
    check_fn=check_session_search_requirements,
    is_async=True,
    description="Search past conversations for relevant information",
    emoji="",
)
