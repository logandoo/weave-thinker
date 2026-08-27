# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

# Interest extraction is LLM-judged (agentic principle 2026-07-20 — the
# former regex topic/intent classifiers cannot generalize to arbitrary
# phrasings). Interaction COUNTS below are deterministic facts computed by
# code; topics/preferences are the LLM's judgment.
_INTEREST_EXTRACT_PROMPT = (
    "你是用户画像分析师。根据以下用户最近的消息，提取用户的关注领域与偏好。\n"
    "输出JSON：\n"
    '{"topics": {"<领域标签>": <出现次数>, ...}, '
    '"preferences": {"response_style": "concise|balanced|detailed", '
    '"frequently_uses_code": true|false, "frequently_uses_search": true|false}}\n'
    "要求：\n"
    "1. topics 用简洁的中文或英文领域标签（如 technology/编程/金融/写作），次数为消息中出现的近似计数（至少1）\n"
    "2. response_style：用户消息普遍简短 → concise；中等长度 → balanced；长而详细的诉求 → detailed\n"
    "3. frequently_uses_code：是否经常涉及写代码/调试/程序问题\n"
    "4. frequently_uses_search：是否经常要求搜索/查找/查最新信息\n"
    "5. 只输出JSON，不要输出其他内容"
)


class ProactiveLearningService:
    async def extract_interests(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """LLM-judged interest extraction from recent messages.

        Interaction pattern COUNTS are structural facts computed here;
        topics and preferences are the LLM's judgment. On LLM failure the
        structurally-known counts are returned with empty semantic fields
        (documented fallback — no semantic claims without a judge).
        """
        user_msgs = [m for m in messages if m.get("role") == "user"]
        structural = {
            "interaction_patterns": {
                "total_user_messages": len(user_msgs),
                "total_messages": len(messages),
                "user_ratio": round(len(user_msgs) / max(len(messages), 1), 2),
            }
        }
        if not user_msgs:
            return {
                "topics": {},
                "preferences": {
                    "response_style": "balanced",
                    "avg_query_length": 0.0,
                    "frequently_uses_code": False,
                    "frequently_uses_search": False,
                },
                **structural,
            }

        from app.services.agentic_judge import judge_json

        transcript = "\n".join(
            f"用户: {(m.get('content') or '')[:300]}"
            for m in user_msgs[-40:]
        )
        avg_length = sum(len(str(m.get("content", ""))) for m in user_msgs) / len(user_msgs)

        parsed = await judge_json(
            _INTEREST_EXTRACT_PROMPT,
            f"用户最近消息（最多40条）：\n{transcript[:6000]}\n\n只输出JSON。",
            task="interest_extract",
            default=None,

            timeout=25.0,
        )
        if not isinstance(parsed, dict):
            logger.info("interest extraction LLM unavailable — structural counts only")
            return {
                "topics": {},
                "preferences": {
                    "response_style": "balanced",
                    "avg_query_length": round(avg_length, 1),
                    "frequently_uses_code": False,
                    "frequently_uses_search": False,
                },
                **structural,
            }

        topics = parsed.get("topics")
        if not isinstance(topics, dict):
            topics = {}
        prefs = parsed.get("preferences")
        if not isinstance(prefs, dict):
            prefs = {}
        style = str(prefs.get("response_style") or "balanced")
        if style not in ("concise", "balanced", "detailed"):
            style = "balanced"

        return {
            "topics": {str(k): max(1, int(v)) for k, v in topics.items() if isinstance(v, (int, float))},
            "preferences": {
                "response_style": style,
                "avg_query_length": round(avg_length, 1),
                "frequently_uses_code": bool(prefs.get("frequently_uses_code")),
                "frequently_uses_search": bool(prefs.get("frequently_uses_search")),
            },
            **structural,
        }

    def build_user_context(self, interests: Dict[str, Any]) -> str:
        topics = interests.get("topics", {})
        prefs = interests.get("preferences", {})

        parts = []
        if topics:
            top_topics = sorted(topics.items(), key=lambda x: x[1], reverse=True)[:3]
            topic_str = ", ".join(f"{t}({c}次)" for t, c in top_topics)
            parts.append(f"用户关注领域: {topic_str}")

        style = prefs.get("response_style", "balanced")
        parts.append(f"回答风格偏好: {style}")

        if prefs.get("frequently_uses_search"):
            parts.append("用户常需要联网搜索")
        if prefs.get("frequently_uses_code"):
            parts.append("用户常需要代码执行")

        return "；".join(parts) if parts else ""

    async def update_user_model(self, db, user_id: str) -> Optional[Dict[str, Any]]:
        cfg = config.agent_proactive_learning
        if not cfg.get("enabled", True) or not cfg.get("user_modeling_enabled", True):
            return None

        try:
            from sqlalchemy import select
            from app.db.database import Message, UserAgentState

            result = await db.execute(
                select(Message)
                .where(Message.conversation_id.in_(
                    select(Message.conversation_id).limit(200)
                ))
                .order_by(Message.created_at.desc())
                .limit(100)
            )
            recent_messages = [{"role": m.role, "content": m.content} for m in result.scalars().all()]

            interests = await self.extract_interests(recent_messages)

            state_result = await db.execute(
                select(UserAgentState).where(UserAgentState.user_id == user_id)
            )
            state = state_result.scalar_one_or_none()

            if state and hasattr(state, 'metadata_json') and state.metadata_json:
                try:
                    existing = json.loads(state.metadata_json) if isinstance(state.metadata_json, str) else state.metadata_json
                except (json.JSONDecodeError, TypeError):
                    existing = {}
            else:
                existing = {}

            existing["user_model"] = interests
            existing["user_model_updated_at"] = datetime.utcnow().isoformat()

            if state:
                if hasattr(state, 'metadata_json'):
                    state.metadata_json = json.dumps(existing, ensure_ascii=False)
                await db.commit()

            return interests

        except Exception as e:
            logger.warning("Failed to update user model: %s", e)
            return None
