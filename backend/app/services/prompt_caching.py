# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


class PromptCache:
    def __init__(self):
        self._cache: Dict[str, tuple[float, str]] = {}
        self._ttl = config.agent_cache_ttl_minutes * 60
        self.enabled = config.agent_cache_enabled
        self._lock = asyncio.Lock()

    def _hash_content(self, content: str) -> str:
        return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]

    async def _prune_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (ts, _) in self._cache.items() if now - ts > self._ttl]
        for k in expired:
            del self._cache[k]

    async def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        async with self._lock:
            await self._prune_expired()
            entry = self._cache.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self._ttl:
                del self._cache[key]
                return None
            return value

    async def set(self, key: str, value: str) -> None:
        if not self.enabled:
            return
        async with self._lock:
            self._cache[key] = (time.monotonic(), value)

    async def invalidate(self, key: Optional[str] = None) -> None:
        async with self._lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()


_cache = PromptCache()


async def partition_system_prompt(sections: List[str]) -> tuple[List[str], List[str]]:
    static: List[str] = []
    dynamic: List[str] = []

    for section in sections:
        if any(marker in section for marker in [
            "当前时间:",
            "联网检索结果",
            "网页浏览结果",
            "代码执行结果",
            "可参考的记忆条目",
        ]):
            dynamic.append(section)
        else:
            static.append(section)

    return static, dynamic


async def build_cached_system_prompt(
    assistant_name: str,
    static_sections: List[str],
    dynamic_sections: List[str],
) -> str:
    static_key = f"sys:{assistant_name}:{_cache._hash_content('\n\n'.join(static_sections))}"

    cached_static = await _cache.get(static_key)
    if cached_static is None:
        cached_static = "\n\n".join(static_sections)
        await _cache.set(static_key, cached_static)

    return cached_static + "\n\n" + "\n\n".join(dynamic_sections)


async def apply_prompt_cache_control(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not _cache.enabled:
        return messages

    result = []
    for i, msg in enumerate(messages):
        m = dict(msg)
        if i == 0 and m.get("role") == "system":
            m["cache_control"] = {"type": "ephemeral"}
        result.append(m)

    return result


async def clear_cache() -> None:
    await _cache.invalidate()
    logger.info("Prompt cache cleared")
