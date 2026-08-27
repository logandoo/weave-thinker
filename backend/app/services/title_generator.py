# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import re
from typing import Optional

from app.core.config import get_config
from app.services.llm_service import LLMService
from app.services.provider_router import build_thinking_extra_body

logger = logging.getLogger(__name__)
config = get_config()

_TITLE_SYSTEM_PROMPT = """你是一个对话标题生成器。

请根据用户最新问题和助手最终回答，生成一个简洁准确的中文标题。
你的回复必须严格满足 JSON schema，只输出一个 JSON 对象：{"title": "..."}。

要求：
1. `title` 必须是简洁标题，只保留标题文字本身
2. 不要输出任何 reasoning、解释、用户原始问句、Markdown、代码块、XML 标签或额外字段
3. 不要照抄完整用户 query，不要输出疑问句，不要包含“什么/如何/为什么/请问/？”等问句成分
4. 标题尽量控制在 6 到 16 个字之内
5. 标题应该是名词短语或主题短语，而不是用户提问原文
6. title 不能是“...”“……”这类省略号、纯标点或空泛占位词

错误示例：{"title": "Python 是什么编程语言？"}
正确示例：{"title": "Python 语言简介"}"""

_TITLE_REPAIR_PROMPT = """你上一条标题回复未通过系统校验，原因是：{error}

请基于同一个任务重新回答，并严格遵守以下要求：
1. 只输出一个合法 JSON 对象
2. JSON 对象必须只有一个字段：title
3. 不要输出 markdown 代码块、解释、分析、标签或额外文字
4. title 必须是最终标题文字，不能包含用户原始问句或 reasoning 内容
5. title 不能是“...”“……”这类省略号、纯标点或占位词"""

_TITLE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "conversation_title",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "A concise Chinese conversation title.",
                }
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
}

_RESPONSE_FORMAT_ERROR_HINTS = (
    "response_format",
    "json_schema",
    "guided_json",
    "unsupported",
    "not support",
    "not supported",
    "unexpected keyword",
)

_FALLBACK_DEFAULT_TITLE = "新对话"


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def _has_meaningful_title_text(title: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", title))


def _truncate_query(source: str) -> str:
    """Mechanical text truncation for the final no-LLM fallback (structural,
    not a judgment)."""
    src = (source or "").strip()
    if not src:
        return _FALLBACK_DEFAULT_TITLE
    snippet = src[:16]
    for boundary in ("。", "！", "!", "？", "?", "，", ",", "、", " "):
        idx = snippet.find(boundary)
        if 2 <= idx < len(snippet):
            snippet = snippet[:idx]
            break
    snippet = snippet.strip(" 。！!？?，,、:：")
    return snippet or src[:16]


_FALLBACK_TITLE_PROMPT = (
    "你是对话标题生成器。根据用户的提问，生成一个简洁准确的中文标题。\n"
    "要求：\n"
    "1. 标题是名词短语或主题短语（如「Python 语言简介」），不是提问原文，"
    "不是疑问句，不含「什么/如何/为什么/请问/？」等问句成分\n"
    "2. 6 到 16 个字之间，只保留标题文字本身\n"
    "3. 不能是省略号、纯标点或空泛占位词\n"
    '只输出JSON：{"title": "..."}'
)


def _validate_title_payload(parsed: dict) -> str:
    """Structural validation of a title payload (no semantic judgment)."""
    raw_title = parsed.get("title")
    if not isinstance(raw_title, str):
        raise ValueError("title must be a string")

    title = _normalize_title(raw_title)
    if not title:
        raise ValueError("title was empty")
    if not _has_meaningful_title_text(title):
        raise ValueError("title contained no meaningful text")
    if len(title) > 30:
        raise ValueError("title was too long")
    return title


def _strip_code_fences(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        if len(parts) >= 2:
            cleaned = parts[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
    return cleaned


def _extract_json_object(raw: str) -> dict:
    cleaned = _strip_code_fences(raw)
    if not cleaned:
        raise ValueError("empty title response content")

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"No JSON object found in title response: {cleaned[:200]}")


def _extract_json_from_response(content: str, reasoning: str) -> dict:
    candidates: list[str] = []
    for candidate in (content, reasoning, f"{content}\n{reasoning}" if content and reasoning else ""):
        text = (candidate or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    if not candidates:
        raise ValueError("empty title response content")

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return _extract_json_object(candidate)
        except Exception as exc:
            last_error = exc

    assert last_error is not None
    raise last_error


def _looks_like_response_format_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _RESPONSE_FORMAT_ERROR_HINTS)


class TitleGeneratorService:
    def __init__(self, custom_api_url: str = None, custom_api_key: str = None, custom_model_name: str = None, provider_type: str = "deepseek"):
        self.llm_service = LLMService(
            custom_api_url=custom_api_url,
            custom_api_key=custom_api_key,
            custom_model_name=custom_model_name
        )
        self.provider_type = provider_type
        self.custom_model_name = custom_model_name

    async def _request_title_json(self, messages: list[dict]) -> Optional[dict]:
        attempts = config.title_generation_structured_output_attempts
        # 用户指令（2026-08-18）：不设 max_tokens == 默认最大输出长度；
        # 重试结构（repair prompt）保留，token 升级不再需要。
        use_response_format = True
        last_error: Exception | None = None
        last_error_text = "empty title response"
        attempt = 0

        while attempt < attempts:
            attempt += 1
            current_messages = list(messages)
            if attempt > 1:
                current_messages.append(
                    {
                        "role": "user",
                        "content": _TITLE_REPAIR_PROMPT.format(error=last_error_text),
                    }
                )
            kwargs = {
                "temperature": 0.0,
                "extra_body": build_thinking_extra_body(self.provider_type, False),
            }
            if use_response_format:
                kwargs["response_format"] = _TITLE_RESPONSE_FORMAT

            try:
                content, reasoning = await self.llm_service.complete_chat_parts(
                    current_messages,
                    **kwargs,
                )
                parsed = _extract_json_from_response(content or "", reasoning or "")
                title = _validate_title_payload(parsed)
                return {"title": title}
            except Exception as exc:
                if use_response_format and _looks_like_response_format_error(exc):
                    logger.info("Title generation provider rejected response_format, retrying without it: %s", exc)
                    use_response_format = False
                    attempt = 0
                    continue

                last_error = exc
                last_error_text = str(exc)
                logger.warning("Title generation attempt %d failed: %s", attempt, exc)
                if attempt < attempts:
                    await asyncio.sleep(config.title_generation_retry_delay_seconds)

        if last_error is not None:
            logger.warning("Title generation failed after %d attempts: %s", attempts, last_error)
        return None

    async def generate_title(
        self,
        user_query: str,
        assistant_response: str
    ) -> Optional[str]:
        """
        Generate a title for the conversation using LLM.

        Args:
            user_query: The user's question/message
            assistant_response: The assistant's response

        Returns:
            Generated title or None if failed
        """
        try:
            messages = [
                {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"用户最新问题：\n{(user_query or '').strip() or '无'}\n\n"
                        f"助手最终回答：\n{(assistant_response or '').strip()[:400] or '无'}"
                    ),
                },
            ]

            payload = await self._request_title_json(messages)
            if not payload:
                return None
            return payload["title"]
        except Exception as e:
            logger.error(f"Failed to generate title: {e}")
            return None

    async def get_fallback_title(self, user_query: str = "") -> str:
        """LLM-judged fallback title (agentic principle — the former
        regex-based derivation _derive_fallback_title was removed). On LLM
        failure, a structural truncation of the query is used so the UI
        always has some label."""
        from app.services.agentic_judge import judge_json

        query = (user_query or "").strip()
        if not query:
            return _FALLBACK_DEFAULT_TITLE
        parsed = await judge_json(
            _FALLBACK_TITLE_PROMPT,
            f"用户提问：\n{query[:400]}\n\n只输出JSON。",
            task="title_fallback",
            default=None,
            timeout=15.0,
        )
        if isinstance(parsed, dict):
            try:
                title = _validate_title_payload(parsed)
                return title
            except ValueError:
                pass
        logger.info("fallback title LLM unavailable/invalid — structural truncation")
        return _truncate_query(query)
