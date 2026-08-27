# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from app.tools.registry import registry
from app.core.config import get_config
from app.services.http_client import get_shared_async_client

logger = logging.getLogger(__name__)
config = get_config()

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
}

_SESSION_CACHE: OrderedDict[str, Tuple[Optional[str], Dict[str, str], float]] = OrderedDict()
_SESSION_TTL = 300.0
_SESSION_CACHE_MAX_SIZE = 100
_session_cache_lock = asyncio.Lock()
_rate_limiter_lock = asyncio.Lock()
_cleanup_task: Optional[asyncio.Task] = None
_cleanup_task_lock = asyncio.Lock()


class _MCPRateLimiter:
    _instances: Dict[str, "_MCPRateLimiter"] = {}

    def __init__(self, min_interval: float = 1.0):
        self._lock = asyncio.Lock()
        self._min_interval = min_interval
        self._last_call: float = 0.0

    @classmethod
    async def get(cls, server_url: str, min_interval: float = 1.0) -> "_MCPRateLimiter":
        async with _rate_limiter_lock:
            if server_url not in cls._instances:
                cls._instances[server_url] = cls(min_interval)
            return cls._instances[server_url]

    @classmethod
    async def _cleanup_unused(cls, active_server_urls: set) -> None:
        async with _rate_limiter_lock:
            for url in list(cls._instances.keys()):
                if url not in active_server_urls:
                    del cls._instances[url]

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()

    def backoff(self):
        self._min_interval = min(self._min_interval * 1.5, 10.0)

    def reset(self):
        self._min_interval = max(self._min_interval * 0.8, 0.5)


def _cache_key(server_url: str, extra_headers: Dict[str, str] = None) -> str:
    auth = (extra_headers or {}).get("Authorization", "")
    return f"{server_url}::{auth}"


async def _cleanup_expired_sessions() -> None:
    now = time.monotonic()
    async with _session_cache_lock:
        expired = [
            key for key, (_, _, ts) in _SESSION_CACHE.items()
            if now - ts > _SESSION_TTL
        ]
        for key in expired:
            _SESSION_CACHE.pop(key, None)
        active_server_urls = {key.split("::", 1)[0] for key in _SESSION_CACHE}
    await _MCPRateLimiter._cleanup_unused(active_server_urls)


async def _cleanup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(60.0)
            await _cleanup_expired_sessions()
        except Exception:
            logger.exception("MCP session cache cleanup failed")


async def _ensure_cleanup_task() -> None:
    global _cleanup_task
    async with _cleanup_task_lock:
        if _cleanup_task is None:
            _cleanup_task = asyncio.create_task(_cleanup_loop())


async def _get_or_create_session(
    server_url: str, extra_headers: Dict[str, str] = None
) -> Tuple[Optional[str], Dict[str, str]]:
    await _ensure_cleanup_task()
    key = _cache_key(server_url, extra_headers)
    now = time.monotonic()

    async with _session_cache_lock:
        if key in _SESSION_CACHE:
            sid, hdrs, ts = _SESSION_CACHE[key]
            if now - ts < _SESSION_TTL:
                _SESSION_CACHE.move_to_end(key)
                return sid, hdrs
            _SESSION_CACHE.pop(key, None)

    limiter = await _MCPRateLimiter.get(server_url)
    await limiter.acquire()

    sid = await _mcp_initialize(server_url, extra_headers)
    hdrs = dict(extra_headers) if extra_headers else {}

    async with _session_cache_lock:
        while len(_SESSION_CACHE) >= _SESSION_CACHE_MAX_SIZE:
            _SESSION_CACHE.popitem(last=False)
        _SESSION_CACHE[key] = (sid, hdrs, now)

    return sid, hdrs


def _parse_sse_text(text: str) -> List[dict]:
    results = []
    current_data = ""
    for line in text.split("\n"):
        if line.startswith("data:"):
            current_data += line[5:]
        elif line.strip() == "" and current_data:
            try:
                results.append(json.loads(current_data.strip()))
            except (json.JSONDecodeError, ValueError):
                pass
            current_data = ""
    if current_data:
        try:
            results.append(json.loads(current_data.strip()))
        except (json.JSONDecodeError, ValueError):
            pass
    return results


def _extract_content_from_mcp_result(data: dict) -> str:
    result = data.get("result", {})
    if "error" in data:
        err = data["error"]
        return json.dumps({"error": err.get("message", str(err))}, ensure_ascii=False)
    content = result.get("content", [])
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    texts.append(text)
            elif isinstance(item, str):
                texts.append(item)
        return "\n".join(texts)
    return json.dumps(result, ensure_ascii=False)


async def _mcp_initialize(server_url: str, extra_headers: Dict[str, str] = None) -> Optional[str]:
    if not HAS_HTTPX:
        return None

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "weave-thinker-mcp", "version": "1.0"},
        },
    }

    headers = dict(MCP_BASE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    try:
        client = get_shared_async_client()
        resp = await client.post(server_url, json=payload, headers=headers, timeout=15.0)
        if resp.status_code != 200:
            logger.warning("MCP initialize failed for %s: %d", server_url, resp.status_code)
            return None

        session_id = resp.headers.get("mcp-session-id")

        notif_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        notif_headers = dict(headers)
        if session_id:
            notif_headers["Mcp-Session-Id"] = session_id
        await client.post(server_url, json=notif_payload, headers=notif_headers, timeout=15.0)

        return session_id
    except Exception as e:
        logger.warning("MCP initialize error for %s: %s", server_url, e)
        return None


async def _mcp_list_tools(
    server_url: str,
    session_id: Optional[str] = None,
    extra_headers: Dict[str, str] = None,
) -> List[dict]:
    if not HAS_HTTPX:
        return []

    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }

    headers = dict(MCP_BASE_HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if extra_headers:
        headers.update(extra_headers)

    try:
        client = get_shared_async_client()
        resp = await client.post(server_url, json=payload, headers=headers, timeout=15.0)
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                key = _cache_key(server_url, extra_headers)
                async with _session_cache_lock:
                    _SESSION_CACHE.pop(key, None)
                    logger.warning("MCP tools/list received %d — session cache invalidated for %s", resp.status_code, key)
            logger.warning("MCP tools/list failed: %d %s", resp.status_code, resp.text[:200])
            return []

        results = _parse_sse_text(resp.text)
        for r in results:
            if "result" in r:
                return r["result"].get("tools", [])
        return []
    except Exception as e:
        logger.warning("MCP tools/list error: %s", e)
        return []


async def _mcp_call_tool(
    server_url: str,
    tool_name: str,
    arguments: dict,
    session_id: Optional[str] = None,
    extra_headers: Dict[str, str] = None,
) -> str:
    if not HAS_HTTPX:
        return json.dumps({"error": "httpx not installed for MCP transport"}, ensure_ascii=False)

    payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    headers = dict(MCP_BASE_HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if extra_headers:
        headers.update(extra_headers)

    max_retries = 3
    base_delay = 2.0
    limiter = await _MCPRateLimiter.get(server_url)

    client = get_shared_async_client()
    for attempt in range(max_retries + 1):
        await limiter.acquire()
        try:
            resp = await client.post(server_url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code == 429:
                limiter.backoff()
                key = _cache_key(server_url, extra_headers)
                async with _session_cache_lock:
                    _SESSION_CACHE.pop(key, None)
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "MCP call rate-limited (429), backing off %.1fs then retry %d/%d",
                        delay, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                return json.dumps(
                    {"error": f"MCP call rate-limited: 429 after {max_retries} retries"},
                    ensure_ascii=False,
                )
            if resp.status_code != 200:
                if resp.status_code in (401, 403):
                    key = _cache_key(server_url, extra_headers)
                    async with _session_cache_lock:
                        _SESSION_CACHE.pop(key, None)
                        logger.warning("MCP call received %d — session cache invalidated for %s", resp.status_code, key)
                return json.dumps(
                    {"error": f"MCP call failed: {resp.status_code}"}, ensure_ascii=False
                )

            limiter.reset()
            results = _parse_sse_text(resp.text)
            for r in results:
                if "result" in r or "error" in r:
                    return _extract_content_from_mcp_result(r)
            return json.dumps({"error": "No valid response from MCP server"}, ensure_ascii=False)
        except Exception as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning("MCP call error, retrying in %.1fs: %s", delay, e)
                await asyncio.sleep(delay)
                continue
            return json.dumps({"error": f"MCP call error: {e}"}, ensure_ascii=False)


class _MCPToolHandler:
    def __init__(
        self,
        server_url: str,
        tool_name: str,
        session_id: Optional[str] = None,
        extra_headers: Dict[str, str] = None,
    ):
        self.server_url = server_url
        self.tool_name = tool_name
        self.session_id = session_id
        self.extra_headers = extra_headers

    async def __call__(self, args: dict, **kwargs) -> str:
        return await _mcp_call_tool(
            self.server_url,
            self.tool_name,
            args,
            self.session_id,
            self.extra_headers,
        )


async def register_mcp_server_async(
    server_name: str,
    server_url: str,
    api_key: str = "",
) -> int:
    extra_headers = {}
    if api_key:
        extra_headers["Authorization"] = f"Bearer {api_key}"

    session_id = await _mcp_initialize(server_url, extra_headers)
    tools = await _mcp_list_tools(server_url, session_id, extra_headers)

    if not tools:
        logger.warning("No tools found from MCP server '%s'", server_name)
        return 0

    registered = 0
    for tool in tools:
        tool_name = tool.get("name", "")
        if not tool_name:
            continue

        mcp_tool_name = f"mcp_{server_name}_{tool_name}"
        schema = {
            "name": mcp_tool_name,
            "description": tool.get(
                "description", f"MCP tool {tool_name} from {server_name}"
            ),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        }

        handler = _MCPToolHandler(server_url, tool_name, session_id, extra_headers)

        registry.register(
            name=mcp_tool_name,
            toolset=f"mcp-{server_name}",
            schema=schema,
            handler=handler,
            is_async=True,
            description=f"MCP: {tool_name} ({server_name})",
            emoji="",
        )
        registered += 1

    return registered


def register_mcp_server(
    server_name: str,
    server_url: str,
    api_key: str = "",
    tools: Optional[List[dict]] = None,
) -> int:
    registered = 0
    extra_headers = {}
    if api_key:
        extra_headers["Authorization"] = f"Bearer {api_key}"

    if tools is None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run,
                        _register_mcp_server_sync(server_name, server_url, api_key),
                    )
                    return future.result(timeout=30)
            else:
                return asyncio.run(
                    _register_mcp_server_sync(server_name, server_url, api_key)
                )
        except Exception as e:
            logger.warning("Failed to register MCP server '%s': %s", server_name, e)
            return 0

    for tool in tools:
        tool_name = tool.get("name", "")
        if not tool_name:
            continue

        mcp_tool_name = f"mcp_{server_name}_{tool_name}"
        schema = {
            "name": mcp_tool_name,
            "description": tool.get(
                "description", f"MCP tool {tool_name} from {server_name}"
            ),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        }

        handler = _MCPToolHandler(server_url, tool_name, None, extra_headers)

        registry.register(
            name=mcp_tool_name,
            toolset=f"mcp-{server_name}",
            schema=schema,
            handler=handler,
            is_async=True,
            description=f"MCP: {tool_name} ({server_name})",
            emoji="",
        )
        registered += 1

    return registered


async def _register_mcp_server_sync(server_name: str, server_url: str, api_key: str) -> int:
    return await register_mcp_server_async(server_name, server_url, api_key)


async def load_mcp_servers_from_config_async() -> int:
    mcp_config = config._config.get("mcp", {})
    if not isinstance(mcp_config, dict):
        return 0
    servers = mcp_config.get("servers", {})
    if not isinstance(servers, dict):
        return 0
    total = 0

    for name, sdata in servers.items():
        url = sdata.get("url", "")
        api_key = sdata.get("api_key", "")
        if not url:
            continue
        try:
            count = await register_mcp_server_async(name, url, api_key)
            total += count
            logger.info("Registered %d tools from MCP server '%s'", count, name)
        except Exception as e:
            logger.warning("Failed to register MCP server '%s': %s", name, e)

    return total


def load_mcp_servers_from_config() -> int:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run, load_mcp_servers_from_config_async()
                )
                return future.result(timeout=60)
        else:
            return asyncio.run(load_mcp_servers_from_config_async())
    except Exception as e:
        logger.warning("Failed to load MCP servers: %s", e)
        return 0


async def call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict,
    api_key: str = "",
) -> str:
    extra_headers = {}
    if api_key:
        extra_headers["Authorization"] = f"Bearer {api_key}"

    session_id, _ = await _get_or_create_session(server_url, extra_headers)
    return await _mcp_call_tool(server_url, tool_name, arguments, session_id, extra_headers)
