# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Error classification and recovery-strategy mapping.

Design (agentic principle, 2026-07-20: 禁止正则/硬编码分类器，语义判断留给 LLM):
- HTTP status codes are PROTOCOL FACTS (RFC 9110 semantics) — mapped
  deterministically, no judgment involved.
- Retry-After extraction from headers / body is STRUCTURAL parsing — regex
  extraction of a number is fine (documented exception: parsing, not judging).
- Free-text error messages are classified by an LLM (arbitrary provider
  phrasings cannot be enumerated). LLM failure → documented fallback policy:
  treat as rate-limit-like retry (bounded backoff, never infinite), matching
  the previous regexes' most conservative safe action.
"""
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    AUTH = "auth"
    BILLING = "billing"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    NETWORK = "network"
    TOOL_FAILURE = "tool_failure"
    REASONING_CONTENT = "reasoning_content"
    UNKNOWN = "unknown"


@dataclass
class RecoveryStrategy:
    action: str
    delay_seconds: float = 0.0
    max_retries: int = 1
    details: str = ""
    fallback_provider: Optional[str] = None


_RECOVERY_STRATEGIES: Dict[ErrorCategory, RecoveryStrategy] = {
    ErrorCategory.AUTH: RecoveryStrategy(
        action="fallback_provider",
        details="API key invalid or expired, try fallback provider",
    ),
    ErrorCategory.BILLING: RecoveryStrategy(
        action="fallback_provider",
        details="Billing/quota issue, try fallback provider",
    ),
    ErrorCategory.RATE_LIMIT: RecoveryStrategy(
        action="retry",
        delay_seconds=5.0,
        max_retries=3,
        details="Rate limited, retry with exponential backoff",
    ),
    ErrorCategory.TIMEOUT: RecoveryStrategy(
        action="retry",
        delay_seconds=2.0,
        max_retries=2,
        details="Request timed out, retry",
    ),
    ErrorCategory.CONTEXT_OVERFLOW: RecoveryStrategy(
        action="compress_context",
        details="Context window exceeded, compress and retry",
    ),
    ErrorCategory.NETWORK: RecoveryStrategy(
        action="retry",
        delay_seconds=3.0,
        max_retries=2,
        details="Network error, retry",
    ),
    ErrorCategory.TOOL_FAILURE: RecoveryStrategy(
        action="abort",
        details="Tool execution failed, cannot auto-recover",
    ),
    ErrorCategory.REASONING_CONTENT: RecoveryStrategy(
        action="retry",
        delay_seconds=1.0,
        max_retries=2,
        details="reasoning_content must be passed back; retry (typically with thinking disabled on the retry)",
    ),
    ErrorCategory.UNKNOWN: RecoveryStrategy(
        action="abort",
        details="Unknown error, cannot auto-recover",
    ),
}

# HTTP status codes are protocol facts — deterministic, not judgment.
_STATUS_TO_CATEGORY: Dict[int, ErrorCategory] = {
    400: ErrorCategory.UNKNOWN,
    401: ErrorCategory.AUTH,
    402: ErrorCategory.BILLING,
    403: ErrorCategory.AUTH,
    404: ErrorCategory.UNKNOWN,
    408: ErrorCategory.TIMEOUT,
    409: ErrorCategory.UNKNOWN,
    413: ErrorCategory.CONTEXT_OVERFLOW,
    422: ErrorCategory.UNKNOWN,
    429: ErrorCategory.RATE_LIMIT,
    500: ErrorCategory.NETWORK,
    502: ErrorCategory.NETWORK,
    503: ErrorCategory.NETWORK,
    504: ErrorCategory.TIMEOUT,
}

# Documented fallback policy when the classifying LLM itself is unavailable
# (e.g. the primary provider is down): retry with bounded backoff — the
# safest generic action for transient failures. Deliberately NOT a semantic
# judgment; a named policy constant.
_LLM_FAILURE_CATEGORY = ErrorCategory.TIMEOUT

_CLASSIFY_SYSTEM_PROMPT = (
    "你是一个 LLM API 错误分类器。给定一个错误消息文本，将其归类为以下类别之一，"
    "输出JSON。只做分类，不要解释。\n"
    "类别：\n"
    "- auth: API密钥无效/过期/未授权（401 语义）\n"
    "- billing: 余额不足/配额用尽/计费问题（402 语义）\n"
    "- rate_limit: 请求频率超限/并发容量不足（429 语义）\n"
    "- timeout: 请求超时/上游无响应\n"
    "- context_overflow: 上下文长度超限/token 超限\n"
    "- network: 网络连接错误/DNS/SSL/网关错误\n"
    "- tool_failure: 工具执行失败（如 code_execution 沙箱内部错误）\n"
    "- reasoning_content: 需要回传 reasoning_content 的错误\n"
    "- unknown: 无法归入上述任何类别\n"
    '输出格式（只输出JSON）：{"category": "auth|billing|rate_limit|timeout|context_overflow|network|tool_failure|reasoning_content|unknown", "reason": "一句话依据"}'
)

_VALID_CATEGORIES = {c.value for c in ErrorCategory}


class ErrorClassifier:
    _RETRY_AFTER_MS_RE = re.compile(r"retry[-_]after[-_]ms['\"]?\s*[:=]\s*(\d+)", re.IGNORECASE)
    _RETRY_AFTER_SEC_RE = re.compile(r"retry[-_]after['\"]?\s*[:=]\s*(\d+)", re.IGNORECASE)
    _TRY_AGAIN_SEC_RE = re.compile(r"try\s+again\s+in\s+(\d+\.?\d*)\s*seconds", re.IGNORECASE)
    _WAIT_SEC_RE = re.compile(r"please\s+wait\s+(\d+\.?\d*)\s*seconds?", re.IGNORECASE)

    @classmethod
    def extract_retry_after(cls, error: Exception) -> Optional[float]:
        """Extract Retry-After delay from exception or its HTTP response.

        Checks provider HTTP response headers first (most authoritative),
        then falls back to structural number extraction in the error body.
        Returns seconds or None if no hint found.
        """
        try:
            response = getattr(error, "response", None)
            if response is not None:
                headers = getattr(response, "headers", {}) or {}
                ms = headers.get("retry-after-ms")
                if ms is not None:
                    return float(ms) / 1000.0
                sec = headers.get("Retry-After") or headers.get("retry-after")
                if sec is not None:
                    try:
                        return float(sec)
                    except ValueError:
                        pass
                body = getattr(response, "body", None)
                if body is None:
                    try:
                        body = getattr(response, "text", None)
                    except Exception:
                        pass
                if isinstance(body, (str, bytes)):
                    body_str = body if isinstance(body, str) else body.decode("utf-8", errors="replace")
                elif isinstance(body, dict):
                    body_str = json.dumps(body)
                else:
                    body_str = None
                if body_str:
                    m = cls._RETRY_AFTER_MS_RE.search(body_str)
                    if m:
                        return float(m.group(1)) / 1000.0
            return None
        except Exception:
            return None

    @classmethod
    def extract_retry_after_from_message(cls, message: str) -> Optional[float]:
        """Extract Retry-After delay from error message text."""
        if not isinstance(message, str) or not message:
            return None
        m = cls._RETRY_AFTER_MS_RE.search(message)
        if m:
            return float(m.group(1)) / 1000.0
        m = cls._RETRY_AFTER_SEC_RE.search(message)
        if m:
            return float(m.group(1))
        m = cls._TRY_AGAIN_SEC_RE.search(message)
        if m:
            return float(m.group(1))
        m = cls._WAIT_SEC_RE.search(message)
        if m:
            return float(m.group(1))
        return None

    @classmethod
    def _category_from_status(cls, error: Exception) -> Optional[ErrorCategory]:
        try:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            if status is None:
                status = getattr(error, "status_code", None)
            if status is not None:
                return _STATUS_TO_CATEGORY.get(int(status))
        except Exception:
            pass
        return None

    @classmethod
    async def classify(cls, error: Exception) -> Tuple[ErrorCategory, Optional[float]]:
        """Classify error and extract Retry-After delay if available.

        Protocol facts (HTTP status code) short-circuit; free text is judged
        by an LLM. LLM failure → documented fallback policy (bounded retry).
        """
        retry_after = cls.extract_retry_after(error)
        if retry_after is None:
            retry_after = cls.extract_retry_after_from_message(str(error))

        status_category = cls._category_from_status(error)
        if status_category is not None:
            return status_category, retry_after

        return await cls._classify_text_async(str(error)), retry_after

    @classmethod
    async def classify_message(cls, message: Optional[str]) -> ErrorCategory:
        """Null-safe classification of an error message string."""
        if not isinstance(message, str) or not message:
            return ErrorCategory.UNKNOWN
        return await cls._classify_text_async(message)

    @classmethod
    async def _classify_text_async(cls, message: str) -> ErrorCategory:
        from app.services.agentic_judge import judge_json

        if not message or len(message) > 1500:
            return ErrorCategory.UNKNOWN
        user_prompt = (
            f"错误消息：\n{message[:1500]}\n\n"
            "请只输出JSON分类结果。"
        )
        parsed = await judge_json(
            _CLASSIFY_SYSTEM_PROMPT,
            user_prompt,
            task="error_classify",
            default=None,

            timeout=20.0,
        )
        if not isinstance(parsed, dict):
            return _LLM_FAILURE_CATEGORY
        cat = str(parsed.get("category") or "").strip().lower()
        if cat not in _VALID_CATEGORIES:
            return _LLM_FAILURE_CATEGORY
        return ErrorCategory(cat)

    @classmethod
    def get_recovery_strategy(cls, category: ErrorCategory) -> RecoveryStrategy:
        return _RECOVERY_STRATEGIES.get(
            category, _RECOVERY_STRATEGIES[ErrorCategory.UNKNOWN]
        )

    @classmethod
    def is_recoverable(cls, category: ErrorCategory) -> bool:
        strategy = cls.get_recovery_strategy(category)
        return strategy.action != "abort"
