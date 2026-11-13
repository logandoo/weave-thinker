# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
import tomllib
import uuid
from urllib.parse import urljoin, urlsplit
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from app.tools.registry import registry
from app.core.config import get_config
from app.services.http_client import get_shared_async_client

logger = logging.getLogger(__name__)

# F5/H11（2026-09-14）：stdio 子进程环境白名单（只收紧子进程，不影响主进程）
_SAFE_ENV_KEYS = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "TMPDIR", "TEMP", "TMP", "USER", "LOGNAME", "SHELL",
    "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "NODE_PATH", "VIRTUAL_ENV", "PYTHONUNBUFFERED",
)


def _child_env(extra: dict | None = None) -> dict:
    """白名单环境 + 服务器显式 env（凭据类变量不随 os.environ 外泄）。"""
    base = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    if extra:
        base.update({str(k): str(v) for k, v in extra.items()})
    return base
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

# --- stdio transport (MCP 2024-11-05: newline-delimited JSON-RPC over a
# client-launched subprocess's stdin/stdout; stderr is logging only) ---
# asyncio primitives are loop-bound: sessions and locks are keyed per running
# loop so a session spawned under one loop (e.g. the sync startup wrapper's
# worker loop) is transparently respawned when used from another (the main
# uvicorn loop).
_STDIO_SESSIONS: Dict[str, "_StdioSession"] = {}
_STDIO_SERVER_SPECS: Dict[str, Tuple[List[str], List[str], Dict[str, str]]] = {}
_STDIO_IDLE_TTL = 300.0
_stdio_loop_locks: Dict[int, asyncio.Lock] = {}


def _stdio_lock_for_current_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _stdio_loop_locks.get(id(loop))
    if lock is None:
        lock = asyncio.Lock()
        _stdio_loop_locks[id(loop)] = lock
    return lock

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


def _cache_key(server_url: str, extra_headers: Dict[str, str] = None,
               owner: str = "") -> str:
    """MCP session-cache key.

    2026-11-02（C3）：owner 参与分键。此前只按 ``(url, auth)``——用户级服务器
    与运营方全局服务器同 URL 时 auth 皆为空（OAuth bearer 是动态注入的），
    两者会共享同一个 MCP session，跨用户复用运营方会话。
    """
    auth = (extra_headers or {}).get("Authorization", "")
    return f"{server_url}::{owner or ''}::{auth}"


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
    async with _stdio_lock_for_current_loop():
        idle = [
            name for name, session in _STDIO_SESSIONS.items()
            if now - session.last_used > _STDIO_IDLE_TTL
        ]
        for name in idle:
            session = _STDIO_SESSIONS.pop(name, None)
            if session:
                try:
                    await session.stop()
                except Exception:
                    logger.warning("MCP stdio session '%s' failed to stop cleanly", name)


class _StdioSession:
    """One MCP stdio server subprocess with request-id routed responses."""

    def __init__(self, name: str, command: List[str], args: List[str], env: Dict[str, str] = None):
        self.name = name
        self.command = list(command) + list(args or [])
        self.env = env or {}
        self.loop = asyncio.get_running_loop()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.pending: Dict[int, asyncio.Future] = {}
        self.next_id = 1
        self.last_used = time.monotonic()
        self._tasks: List[asyncio.Task] = []
        self._stderr_tail: List[str] = []

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        # F5/H11（2026-09-14）：子进程 env 白名单——不再继承整个 os.environ
        # （防 API key/凭据泄漏进第三方 MCP 进程）；服务器显式 env 仍合并。
        merged_env = _child_env(self.env)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
                limit=10 * 1024 * 1024,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise RuntimeError(f"MCP stdio server '{self.name}' failed to start ({self.command[:1]}): {e}") from e
        self._tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._stderr_loop()),
        ]
        await self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "weave-thinker-mcp", "version": "1.0"},
        }, timeout=15.0)
        await self._notify("notifications/initialized", {})

    async def _request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        if not self.is_alive:
            raise RuntimeError(f"MCP stdio server '{self.name}' is not running")
        rid = self.next_id
        self.next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            raise
        finally:
            self.pending.pop(rid, None)

    async def _notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError(f"MCP stdio server '{self.name}' has no stdin")
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        self.proc.stdin.write(data.encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("MCP stdio '%s' dropped non-JSON stdout line: %.120s", self.name, line)
                    continue
                rid = msg.get("id")
                if rid is None:
                    continue  # server notification — nothing to route
                fut = self.pending.get(rid)
                if fut and not fut.done():
                    fut.set_result(msg)
                self.pending.pop(rid, None)
        except Exception as e:
            logger.warning("MCP stdio '%s' read loop ended: %s", self.name, e)
        finally:
            self._fail_all_pending(RuntimeError(f"MCP stdio server '{self.name}' exited"))

    async def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            async for raw in self.proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 20:
                        self._stderr_tail.pop(0)
                    logger.debug("MCP stdio '%s' stderr: %s", self.name, line)
        except Exception:
            pass

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()

    async def stop(self) -> None:
        # Sync-signal only: never await loop-bound primitives here — stop() may
        # be called from a different (newer) loop than the one that spawned the
        # subprocess. Reaping is handled by the child watcher thread.
        self._fail_all_pending(RuntimeError(f"MCP stdio server '{self.name}' stopped"))
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            for _ in range(20):
                if self.proc.returncode is not None:
                    break
                await asyncio.sleep(0.05)
            if self.proc.returncode is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass


async def _get_or_create_stdio_session(server_name: str) -> "_StdioSession":
    spec = _STDIO_SERVER_SPECS.get(server_name)
    if spec is None:
        raise RuntimeError(f"MCP stdio server '{server_name}' is not configured")
    # Arm the shared cleanup loop (idle reaping) regardless of transport —
    # without this a stdio-only deployment never reaps idle subprocesses.
    await _ensure_cleanup_task()
    lock = _stdio_lock_for_current_loop()
    async with lock:
        session = _STDIO_SESSIONS.get(server_name)
        if session and session.is_alive and session.loop is asyncio.get_running_loop():
            session.last_used = time.monotonic()
            return session
        if session:
            await session.stop()
        command, args, env = spec
        session = _StdioSession(server_name, command, args, env)
        try:
            await session.start()
        except Exception:
            # Half-started session (spawned but initialize failed): release
            # the subprocess, otherwise every retry orphans one process.
            await session.stop()
            raise
        _STDIO_SESSIONS[server_name] = session
        session.last_used = time.monotonic()
        return session


def _sanitize_mcp_tool_name(server_name: str, tool_name: str, used_names: set) -> str:
    """Clean an MCP tool's exposed registry name to the OpenAI/DeepSeek
    contract ^[a-zA-Z0-9_-]+$ (wave3 2026-09-02, conv 7618b2c5) with a
    deterministic collision suffix. Dispatch routes via the handler's ORIGINAL
    tool_name — only the exposed name changes. Shared by ALL registration
    transports: stdio (2026-09-02), Streamable HTTP and the sync variant
    (conv 6dcae019, 2026-09-03 — a Streamable-HTTP server switch re-exposed
    the dotted-name 400 because cleaning existed on the stdio path only)."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{server_name}_{tool_name}")
    candidate, suffix = sanitized, 2
    while candidate in used_names:
        candidate = f"{sanitized}_{suffix}"
        suffix += 1
    used_names.add(candidate)
    return candidate


async def _register_stdio_server(
    server_name: str,
    command: List[str],
    args: List[str] = None,
    env: Dict[str, str] = None,
    owner_user_id: str | None = None,
    session_key: str | None = None,
    tool_name_server: str | None = None,
) -> int:
    key = session_key or server_name
    name_for_tools = tool_name_server or server_name
    _STDIO_SERVER_SPECS[key] = (list(command), list(args or []), dict(env or {}))
    try:
        session = await _get_or_create_stdio_session(key)
        tools_result = await session._request("tools/list", {}, timeout=15.0)
    except Exception as e:
        logger.warning("MCP stdio server '%s' registration failed: %s", server_name, e)
        return 0

    tools = []
    if isinstance(tools_result, dict) and "result" in tools_result:
        tools = tools_result["result"].get("tools", [])

    # OpenAI/DeepSeek 工具名契约 ^[a-zA-Z0-9_-]+$（wave3 2026-09-02）：部分
    # MCP 服务器的工具名带命名空间点号（如 vendor.tool.open），拼接后
    # 必须清洗，否则整份 tools 数组被上游 400 拒绝（conv 7618b2c5）。dispatch
    # 走 handler 内部的原始 tool_name，清洗只影响对外注册名，调用不受影响。
    used_names = set(registry.get_all_registered_names())
    registered = 0
    for tool in tools:
        tool_name = tool.get("name", "")
        if not tool_name:
            continue
        mcp_tool_name = _sanitize_mcp_tool_name(name_for_tools, tool_name, used_names)
        used_names.add(mcp_tool_name)
        schema = {
            "name": mcp_tool_name,
            "description": tool.get("description", f"MCP tool {tool_name} from {server_name}"),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        }
        handler = _MCPToolHandler(
            "", tool_name, transport="stdio", server_name=key
        )
        registry.register(
            name=mcp_tool_name,
            toolset=f"mcp-{server_name}",
            schema=schema,
            handler=handler,
            is_async=True,
            description=f"MCP: {tool_name} ({server_name})",
            emoji="",
            owner_user_id=owner_user_id,
        )
        registered += 1
    logger.info("Registered %d tools from stdio MCP server '%s'", registered, server_name)
    return registered


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
    server_url: str, extra_headers: Dict[str, str] = None, owner: str = ""
) -> Tuple[Optional[str], Dict[str, str]]:
    await _ensure_cleanup_task()
    key = _cache_key(server_url, extra_headers, owner)
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

    sid = await _mcp_initialize(server_url, extra_headers, owner=owner)
    hdrs = dict(extra_headers) if extra_headers else {}

    async with _session_cache_lock:
        while len(_SESSION_CACHE) >= _SESSION_CACHE_MAX_SIZE:
            _SESSION_CACHE.popitem(last=False)
        _SESSION_CACHE[key] = (sid, hdrs, now)

    return sid, hdrs


def _parse_sse_text(text: str) -> List[dict]:
    """Parse MCP response. Handles both plain JSON and SSE (data:) framing.
    Some Streamable-HTTP servers return plain JSON; others may use SSE."""
    results = []
    stripped = text.strip()
    # Plain JSON (single object or array) — try direct parse first
    if stripped and not stripped.startswith("data:"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                results.extend(parsed)
            else:
                results.append(parsed)
            return results
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to SSE parsing
    # SSE framing
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


async def _mcp_initialize(server_url: str, extra_headers: Dict[str, str] = None,
                          owner: str = "") -> Optional[str]:
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
    headers.update(await _effective_headers(server_url, extra_headers, owner))

    try:
        client = get_shared_async_client()
        resp = await _mcp_post(client, server_url, json=payload, headers=headers, timeout=15.0)
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
        await _mcp_post(client, server_url, json=notif_payload, headers=notif_headers, timeout=15.0)

        return session_id
    except Exception as e:
        logger.warning("MCP initialize error for %s: %s", server_url, e)
        return None


async def _mcp_list_tools(
    server_url: str,
    session_id: Optional[str] = None,
    extra_headers: Dict[str, str] = None,
    owner: str = "",
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
    headers.update(await _effective_headers(server_url, extra_headers, owner))

    try:
        client = get_shared_async_client()
        resp = await _mcp_post(client, server_url, json=payload, headers=headers, timeout=15.0)
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                key = _cache_key(server_url, extra_headers, owner)
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
    owner: str = "",
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

    base_headers = dict(MCP_BASE_HEADERS)
    if session_id:
        base_headers["Mcp-Session-Id"] = session_id

    max_retries = 3
    base_delay = 2.0
    limiter = await _MCPRateLimiter.get(server_url)

    client = get_shared_async_client()
    for attempt in range(max_retries + 1):
        await limiter.acquire()
        # OAuth Bearer 每次重试动态注入：401/403 刷新后重试立即可用新 token
        headers = dict(base_headers)
        headers.update(await _effective_headers(server_url, extra_headers, owner))
        try:
            resp = await _mcp_post(client, server_url, json=payload, headers=headers, timeout=60.0)
            if resp.status_code == 429:
                limiter.backoff()
                key = _cache_key(server_url, extra_headers, owner)
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
                    key = _cache_key(server_url, extra_headers, owner)
                    async with _session_cache_lock:
                        _SESSION_CACHE.pop(key, None)
                        logger.warning(
                            "MCP call received %d — session cache invalidated for %s",
                            resp.status_code, key,
                        )
                    provider = _OAUTH_BY_URL.get(_oauth_key(server_url, owner))
                    if provider is not None:
                        provider.invalidate()
                        refreshed = await provider.token(force=True)
                        if refreshed and attempt < max_retries:
                            logger.info(
                                "MCP OAuth access token refreshed (was %d), retrying",
                                resp.status_code,
                            )
                            continue
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


# --- OAuth refresh-token support for HTTP MCP servers (2026-09-03) ---
# Some HTTP MCP endpoints (e.g. Streamable-HTTP servers) require an OAuth
# Authorization Code + PKCE flow whose access token expires (~1h). The harness
# persists the refresh token in a local state file (config key
# `oauth_state_file` under [mcp.servers.<name>]) and refreshes lazily: token()
# refreshes when the cached access token is expired, and a 401/403 on a call
# invalidates + force-refreshes once before retrying.
# 2026-11-02（C3）：按 ``(url, owner)`` 分键。owner="" = 运营方全局服务器；
# 用户级服务器恒非空 uid —— 用户不得消费全局 OAuth bearer，其自有 api_key
# 也不再因「URL 已在表内」被静默丢弃。
_OAUTH_BY_URL: Dict[Tuple[str, str], "_OAuthProvider"] = {}


def _oauth_key(url: str, owner: str = "") -> Tuple[str, str]:
    return (str(url or ""), str(owner or ""))


class _OAuthProvider:
    def __init__(
        self,
        *,
        token_endpoint: str,
        client_id: str,
        refresh_token: str,
        resource: str = "",
        state_path: str = "",
    ) -> None:
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.resource = resource
        self.state_path = state_path
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def token(self, force: bool = False) -> Optional[str]:
        async with self._lock:
            if (
                not force
                and self._token
                and time.time() < self._expires_at - 60.0
            ):
                return self._token
            try:
                # 独立短生命周期 client：OAuth 刷新频率极低（约 1h/次 + 401
                # 兜底），避免共享 AsyncClient 的 loop 绑定问题（共享 client
                # 若在 import 期被创建会绑定到非运行 loop，刷新即报
                # "Event loop is closed"）。
                data: Dict[str, str] = {
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                }
                if self.resource:
                    data["resource"] = self.resource
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=15.0
                ) as client:
                    resp = await client.post(
                        self.token_endpoint,
                        data=data,
                        headers={"Accept": "application/json"},
                    )
                if resp.status_code != 200:
                    logger.warning(
                        "MCP OAuth refresh failed for %s: %d %s",
                        self.token_endpoint, resp.status_code, resp.text[:200],
                    )
                    return None
                payload = resp.json()
                new_token = payload.get("access_token")
                if not new_token:
                    logger.warning("MCP OAuth refresh returned no access_token")
                    return None
                self._token = new_token
                try:
                    self._expires_at = time.time() + int(
                        payload.get("expires_in", 3600)
                    )
                except (TypeError, ValueError):
                    self._expires_at = time.time() + 3600.0
                if payload.get("refresh_token"):
                    self.refresh_token = payload["refresh_token"]
                self._persist()
                return self._token
            except Exception as e:
                logger.warning("MCP OAuth refresh error: %s", e)
                return None

    def invalidate(self) -> None:
        self._expires_at = 0.0

    def _persist(self) -> None:
        if not self.state_path:
            return
        try:
            directory = os.path.dirname(self.state_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {
                "token_endpoint": self.token_endpoint,
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
                "resource": self.resource,
                "access_token": self._token or "",
                "expires_at": self._expires_at,
            }
            tmp_path = self.state_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.state_path)
        except Exception as e:
            logger.warning("MCP OAuth state persist failed: %s", e)


def _load_oauth_provider(sdata: dict) -> Optional["_OAuthProvider"]:
    state_path = str(sdata.get("oauth_state_file") or "").strip()
    if not state_path:
        return None
    try:
        with open(state_path, "r") as f:
            st = json.load(f)
    except Exception:
        logger.warning("MCP OAuth state file unreadable: %s", state_path)
        return None
    token_endpoint = str(st.get("token_endpoint") or "").strip()
    client_id = str(st.get("client_id") or "").strip()
    refresh_token = str(st.get("refresh_token") or "").strip()
    if not (token_endpoint and client_id and refresh_token):
        logger.warning("MCP OAuth state file incomplete: %s", state_path)
        return None
    provider = _OAuthProvider(
        token_endpoint=token_endpoint,
        client_id=client_id,
        refresh_token=refresh_token,
        resource=str(st.get("resource") or "").strip(),
        state_path=state_path,
    )
    access_token = str(st.get("access_token") or "").strip()
    if access_token:
        provider._token = access_token
    try:
        provider._expires_at = float(st.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        provider._expires_at = 0.0
    return provider


async def _effective_headers(
    server_url: str, extra_headers: Dict[str, str] = None, owner: str = ""
) -> Dict[str, str]:
    """Static extra headers plus a live OAuth Bearer when available.

    2026-11-02（C3）：只取**本 owner 自己**的 OAuth provider。用户级服务器
    （owner=uid）不会命中运营方全局条目的 bearer。
    """
    hdrs = dict(extra_headers or {})
    provider = _OAUTH_BY_URL.get(_oauth_key(server_url, owner))
    if provider is not None:
        token = await provider.token()
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        else:
            hdrs.pop("Authorization", None)
    return hdrs


class _MCPToolHandler:
    def __init__(
        self,
        server_url: str,
        tool_name: str,
        session_id: Optional[str] = None,
        extra_headers: Dict[str, str] = None,
        transport: str = "http",
        server_name: str = "",
        owner: str = "",
    ):
        self.server_url = server_url
        self.tool_name = tool_name
        self.session_id = session_id
        self.extra_headers = extra_headers
        self.transport = transport
        self.server_name = server_name
        # 2026-11-02（C3）：用户级 handler 记住 owner，调用期只解析自己的
        # OAuth / session，不复用运营方全局条目。
        self.owner = owner

    async def __call__(self, args: dict, **kwargs) -> str:
        # 任务作用域（§7.5/§10 MCP）：外部 MCP 不是本地文件沙箱。scoped 会话默认全部
        # 拒绝，只有用户签发的 kind=mcp grant 指明 server/tool/action 才放行；
        # 放行后危险操作仍照旧过 act gate。
        from app.services import workspace_scope_service as _svs

        if _svs.scoping_mode() == "enforce" and kwargs.get("workspace_ctx") is not None:
            import json as _json

            from app.services.cross_scope_grant_service import verify_grant as _verify_grant

            grant_id = _svs.grant_id_from(kwargs) or str((args or {}).get("grant_id") or "").strip()
            if not grant_id:
                return _json.dumps({
                    "error": "scoped 会话默认不开放外部 MCP；请让用户在界面上显式授权该 server/tool/action。",
                    "code": "workspace_cross_scope_denied", "success": False}, ensure_ascii=False)
            db = kwargs.get("db")
            check = await _verify_grant(db, grant_id, ctx=kwargs["workspace_ctx"], kind="mcp",
                                        mode="read", tool=self.server_name or self.server_url,
                                        action=self.tool_name)
            if not check.ok:
                return _json.dumps({"error": check.message, "code": check.code,
                                    "success": False}, ensure_ascii=False)
        if self.transport == "stdio":
            return await _call_stdio_tool(self.server_name, self.tool_name, args)
        return await _mcp_call_tool(
            self.server_url,
            self.tool_name,
            args,
            self.session_id,
            self.extra_headers,
            owner=self.owner,
        )


async def _call_stdio_tool(server_name: str, tool_name: str, arguments: dict) -> str:
    try:
        session = await _get_or_create_stdio_session(server_name)
    except Exception as e:
        return json.dumps({"error": f"MCP stdio server unavailable: {e}"}, ensure_ascii=False)
    try:
        data = await session._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=60.0,
        )
        return _extract_content_from_mcp_result(data)
    except asyncio.TimeoutError:
        return json.dumps({"error": f"MCP stdio call timed out: {tool_name}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"MCP stdio call error: {e}"}, ensure_ascii=False)


async def register_mcp_server_async(
    server_name: str,
    server_url: str = "",
    api_key: str = "",
    command: Optional[List[str]] = None,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    owner_user_id: str | None = None,
    session_key: str | None = None,
    tool_name_server: str | None = None,
) -> int:
    common = {}
    if owner_user_id:
        common["owner_user_id"] = owner_user_id
    if tool_name_server:
        common["tool_name_server"] = tool_name_server
    if command:
        stdio_extra = dict(common)
        if session_key:
            stdio_extra["session_key"] = session_key
        return await _register_stdio_server(
            server_name, command, args, env, **stdio_extra,
        )
    return await _register_http_server(
        server_name, server_url, api_key, **common,
    )


async def _register_http_server(
    server_name: str,
    server_url: str,
    api_key: str = "",
    owner_user_id: str | None = None,
    tool_name_server: str | None = None,
) -> int:
    name_for_tools = tool_name_server or server_name
    extra_headers = {}
    # OAuth provider（[mcp.servers.<name>].oauth_state_file）存在时
    # Authorization 由 _effective_headers 动态注入（token 会过期并自动刷新），
    # 静态 api_key 仅用于无 OAuth 的 HTTP MCP 服务器。
    # 2026-11-02（C3）：按本 owner 自己的 OAuth 条目判断——同 URL 的运营方
    # OAuth 不得让用户级服务器丢掉自己的 api_key。
    if api_key and _oauth_key(server_url, str(owner_user_id or "")) not in _OAUTH_BY_URL:
        extra_headers["Authorization"] = f"Bearer {api_key}"

    _owner = str(owner_user_id or "")
    session_id = await _mcp_initialize(server_url, extra_headers, owner=_owner)
    tools = await _mcp_list_tools(server_url, session_id, extra_headers, owner=_owner)

    if not tools:
        logger.warning("No tools found from MCP server '%s'", server_name)
        return 0

    used_names = set(registry.get_all_registered_names())
    registered = 0
    for tool in tools:
        tool_name = tool.get("name", "")
        if not tool_name:
            continue

        mcp_tool_name = _sanitize_mcp_tool_name(name_for_tools, tool_name, used_names)
        schema = {
            "name": mcp_tool_name,
            "description": tool.get(
                "description", f"MCP tool {tool_name} from {server_name}"
            ),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        }

        handler = _MCPToolHandler(server_url, tool_name, session_id, extra_headers,
                                  owner=_owner)

        registry.register(
            name=mcp_tool_name,
            toolset=f"mcp-{server_name}",
            schema=schema,
            handler=handler,
            is_async=True,
            description=f"MCP: {tool_name} ({server_name})",
            emoji="",
            owner_user_id=owner_user_id,
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

    used_names = set(registry.get_all_registered_names())
    for tool in tools:
        tool_name = tool.get("name", "")
        if not tool_name:
            continue

        mcp_tool_name = _sanitize_mcp_tool_name(server_name, tool_name, used_names)
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
    """Startup load = one full diff. State is tracked so later hot reloads can
    unregister removed/changed servers instead of leaking their tools."""
    mcp_config = config.mcp if isinstance(config.mcp, dict) else {}
    servers = mcp_config.get("servers") if isinstance(mcp_config.get("servers"), dict) else {}
    try:
        mtime = os.stat(str(config.config_path)).st_mtime
    except Exception:
        mtime = _MCP_CONFIG_MTIME
    result = await _apply_global_mcp_servers(servers, mtime)
    if not result.get("ok"):
        logger.warning("MCP startup load reported errors: %s", result.get("errors"))
    return int(result.get("registered") or 0)


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


# ── 2026-11-01: MCP hot reload + per-user MCP state ────────────────────────
# Global [mcp.servers.*] and per-user user_mcp_servers rows are diffed against
# process state. A changed server is probed with a temporary session FIRST; the
# old tools are unregistered only after the probe succeeds (failure keeps the
# previous working set). User-owned tools never appear in the global state and
# vice versa.
_GLOBAL_MCP_STATE: Dict[str, dict] = {}
_USER_MCP_STATE: Dict[Tuple[str, str], dict] = {}
_USER_MCP_FAILED: Dict[Tuple[str, str], tuple] = {}
_MCP_CONFIG_MTIME: float = 0.0
_MCP_RETRY_AT: float = 0.0
_MCP_RETRY_DELAY: float = 30.0
_MCP_WATCH_TASK: Optional[asyncio.Task] = None
_reload_loop_locks: Dict[int, asyncio.Lock] = {}


def _reload_lock_for_current_loop() -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    lock = _reload_loop_locks.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _reload_loop_locks[loop_id] = lock
    return lock


def _spec_from_config(sdata: dict) -> Optional[dict]:
    if not isinstance(sdata, dict):
        return None
    command = sdata.get("command")
    if command:
        cmd = [str(x) for x in command] if isinstance(command, (list, tuple)) else [str(command)]
        args = sdata.get("args") or []
        env = sdata.get("env") or {}
        return {
            "transport": "stdio",
            "command": cmd,
            "args": [str(x) for x in (args if isinstance(args, (list, tuple)) else [args])],
            "env": {str(k): str(v) for k, v in (env.items() if isinstance(env, dict) else {})},
            "raw": sdata,
        }
    url = str(sdata.get("url") or "").strip()
    if not url:
        return None
    return {
        "transport": "http",
        "url": url,
        "api_key": str(sdata.get("api_key") or ""),
        "raw": sdata,
    }


async def _user_mcp_host_blocked(host: str) -> bool:
    """用户级 HTTP MCP 的**运行期**私网判定（2026-11-02，C2）。

    与写入期 ``user_mcp_service._host_is_private`` **同源**：字面量 + DNS 解析。
    旧实现只做字面量，一个解析到 127.0.0.1 / 10.x / 169.254.169.254 的域名
    在运行期永远不被拦——写入期拦、运行期放，是最典型的 SSRF 缺口形状。
    解析失败保持 fail-open（与写入期一致），避免把 DNS 抖动变成确定性拒绝。
    """
    try:
        from app.services.user_mcp_service import _host_is_private
        return await _host_is_private(host)
    except Exception:  # 服务层不可用 → 退回字面量判据（少拦也不误伤）
        return _is_private_mcp_host(host)


def _same_origin(a: str, b: str) -> bool:
    """True when scheme+host+port match (default ports normalised)."""
    try:
        pa, pb = urlsplit(a), urlsplit(b)
    except ValueError:
        return False
    def _norm(p):
        scheme = (p.scheme or "").lower()
        port = p.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
        return (scheme, (p.hostname or "").lower(), port)
    return _norm(pa) == _norm(pb)


def _mcp_redirect_target_blocked(src_url: str, location: str) -> bool:
    """重定向目标的**字面量**预判（快速路径）。

    2026-11-02（C2b；评审 I2/I4 修正）：
    - **同源跳转一律放行**——运营方自己的 loopback MCP 若把 ``/mcp`` 规范化成
      ``/mcp/``，旧实现会把它当「跳私网」拦掉（功能回退）。
    - 跨源才做私网判定；DNS 维度由 ``_mcp_post`` 的异步 ``_mcp_redirect_target_blocked_async``
      补上（本函数是同步的，不能解析 DNS）。
    - 部署已显式放行私网（``user_mcp_allow_private_hosts=true``）时不做本判定，
      与同文件的入口门禁保持同一口径。
    """
    try:
        target = urljoin(str(src_url or ""), str(location or ""))
        host = urlsplit(target).hostname or ""
    except ValueError:
        return True
    if _same_origin(str(src_url or ""), target):
        return False
    try:
        if get_config().user_mcp_allow_private_hosts:
            return False
    except Exception:
        pass
    return _is_private_mcp_host(host)


async def _mcp_redirect_target_blocked_async(src_url: str, location: str) -> bool:
    """重定向目标的**DNS 感知**判定（评审 I2）——与运行期入口门禁同源。"""
    if _mcp_redirect_target_blocked(src_url, location):
        return True
    try:
        if get_config().user_mcp_allow_private_hosts:
            return False
        target = urljoin(str(src_url or ""), str(location or ""))
        host = urlsplit(target).hostname or ""
    except ValueError:
        return True
    return await _user_mcp_host_blocked(host)


async def _mcp_post(client, url: str, *, json=None, headers=None,
                    timeout: float = 15.0, max_hops: int = 3):
    """带逐跳校验的 MCP HTTP POST（2026-11-02，C2b）。

    不自动跟随重定向：每一跳先判目标主机，私网/回环即停（返回 3xx 响应，
    由调用方按失败处理），公网跳转最多 ``max_hops`` 次。
    """
    cur = str(url or "")
    hdrs = dict(headers or {})
    resp = None
    for _hop in range(max_hops + 1):
        resp = await client.post(cur, json=json, headers=hdrs, timeout=timeout,
                                 follow_redirects=False)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        loc = resp.headers.get("location") or resp.headers.get("Location")
        if not loc:
            return resp
        # 2026-11-02（评审 Minor-2）：303 See Other 语义是 GET；重发 JSON-RPC
        # POST 可能重复副作用。MCP 不使用 303 —— 视为终态交给调用方报错。
        if resp.status_code == 303:
            return resp
        target = urljoin(cur, loc)
        if await _mcp_redirect_target_blocked_async(cur, loc):
            logger.warning(
                "MCP redirect blocked: %s -> %s (private/loopback target)", cur, target
            )
            return resp
        # 2026-11-02（评审 Minor-1）：跨源跳转必须剥离凭据与 MCP 会话头
        # （httpx 自带的行为在手动跟随时不会生效）。
        if not _same_origin(cur, target):
            for h in ("Authorization", "Mcp-Session-Id", "mcp-session-id"):
                hdrs.pop(h, None)
        cur = target
    return resp


def _is_private_mcp_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _spec_signature(spec: dict) -> str:
    material = {
        "transport": spec.get("transport"),
        "command": spec.get("command"),
        "args": spec.get("args"),
        "env": spec.get("env"),
        "url": spec.get("url"),
        "api_key": spec.get("api_key"),
        "oauth_state_file": (spec.get("raw") or {}).get("oauth_state_file"),
        "eager": (spec.get("raw") or {}).get("eager"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _read_mcp_config(config_path: str | None = None) -> tuple[dict, float]:
    path = config_path or str(get_config().config_path)
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    mcp_cfg = data.get("mcp") or {}
    servers = mcp_cfg.get("servers") or {}
    if not isinstance(servers, dict):
        servers = {}
    return servers, os.stat(path).st_mtime


def user_mcp_session_key(user_id: str, name: str) -> str:
    return f"u:{user_id}:{name}"


def user_mcp_tool_server(user_id: str, name: str) -> str:
    """Tool-name namespace segment; keeps two users' same-named servers apart."""
    compact = str(user_id or "").replace("-", "")[:8]
    return f"u{compact}_{name}"


async def _stop_mcp_session_key(session_key: str) -> None:
    session = _STDIO_SESSIONS.pop(session_key, None)
    _STDIO_SERVER_SPECS.pop(session_key, None)
    if session is not None:
        try:
            await session.stop()
        except Exception:
            logger.warning("MCP stdio session '%s' failed to stop cleanly", session_key)


async def _unregister_tool_names(names: list) -> None:
    for name in list(names or []):
        registry.unregister(name)


async def _probe_mcp_spec(name: str, spec: dict, owner: str = "") -> list:
    """Connect + tools/list with a disposable session; no registry writes.

    Raises on connection/initialize failure so callers can keep the old tools.
    """
    if spec.get("transport") == "stdio":
        probe_key = f"__probe__:{uuid.uuid4().hex}"
        _STDIO_SERVER_SPECS[probe_key] = (
            list(spec.get("command") or []),
            list(spec.get("args") or []),
            dict(spec.get("env") or {}),
        )
        try:
            session = await _get_or_create_stdio_session(probe_key)
            result = await session._request("tools/list", {}, timeout=15.0)
            tools = (
                result.get("result", {}).get("tools", [])
                if isinstance(result, dict) else []
            )
            if not isinstance(tools, list):
                tools = []
            return tools
        finally:
            await _stop_mcp_session_key(probe_key)
    raw = spec.get("raw") or {}
    url = str(spec.get("url") or "")
    # 2026-11-02（评审 I1）：probe 也必须带 owner。此前它恒用 ("url","") 查
    # OAuth —— 用户级 spec 在同一 URL 上会拿到**运营方** bearer，同时因该键
    # 存在而丢掉自己的 api_key。probe 是 user sync 的必走路径，等于 C3 只关了
    # 注册/调用两扇门、漏了探针这扇。
    provider = _load_oauth_provider(raw)
    if provider is not None:
        _OAUTH_BY_URL[_oauth_key(url, owner)] = provider
    headers: Dict[str, str] = {}
    if spec.get("api_key") and _oauth_key(url, owner) not in _OAUTH_BY_URL:
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    session_id = await _mcp_initialize(url, headers, owner=owner)
    tools = await _mcp_list_tools(url, session_id, headers, owner=owner)
    if not tools:
        raise RuntimeError("MCP HTTP initialize/tools-list returned no tools")
    return tools


async def _register_mcp_spec(
    server_name: str,
    spec: dict,
    *,
    owner_user_id: str | None = None,
    session_key: str | None = None,
    tool_name_server: str | None = None,
) -> list:
    """Register one probed spec; returns the newly registered tool names."""
    before = set(registry.get_all_registered_names())
    count = 0
    extra = {}
    if owner_user_id:
        extra["owner_user_id"] = owner_user_id
    if session_key and spec.get("transport") == "stdio":
        extra["session_key"] = session_key
    if tool_name_server:
        extra["tool_name_server"] = tool_name_server
    try:
        if spec.get("transport") == "stdio":
            count = await register_mcp_server_async(
                server_name, "", "",
                command=list(spec.get("command") or []),
                args=list(spec.get("args") or []),
                env=dict(spec.get("env") or {}),
                **extra,
            )
            # _register_stdio_server catches connection failures and returns 0;
            # for stdio a zero-tool result is indistinguishable from a dead
            # server, so treat it as failure (the caller retries / rolls back).
            if int(count or 0) == 0:
                raise RuntimeError("MCP stdio server returned no tools (start/initialize failed)")
        else:
            raw = spec.get("raw") or {}
            provider = _load_oauth_provider(raw)
            if provider is not None:
                # 2026-11-02（评审 Minor-3）：按本 owner 落键——否则 owner 的
                # handler 查不到自己的 provider（静默丢鉴权），而全局路径反被
                # 同 URL 的 provider 污染。
                _OAUTH_BY_URL[_oauth_key(str(spec.get("url") or ""),
                                        str(owner_user_id or ""))] = provider
            count = await register_mcp_server_async(
                server_name, str(spec.get("url") or ""), str(spec.get("api_key") or ""),
                **extra,
            )
            if int(count or 0) == 0:
                raise RuntimeError("MCP HTTP server returned no tools")
    except Exception:
        # Never leak a partially registered tool set on a failed attempt.
        after = set(registry.get_all_registered_names())
        for _name in after - before:
            registry.unregister(_name)
        raise
    after = set(registry.get_all_registered_names())
    return sorted(after - before), int(count or 0)


async def reload_mcp_servers_from_config(config_path: str | None = None) -> dict:
    """Read config.toml and diff [mcp.servers.*] against process state."""
    try:
        servers, mtime = _read_mcp_config(config_path)
    except Exception as exc:
        logger.warning("MCP config reload: read/parse failed (%s); keeping current tools", exc)
        return {
            "ok": False, "errors": [f"config parse failed: {exc}"],
            "registered": 0, "removed": 0, "kept": len(_GLOBAL_MCP_STATE),
            "servers": sorted(_GLOBAL_MCP_STATE.keys()),
        }
    return await _apply_global_mcp_servers(servers, mtime)


async def _apply_global_mcp_servers(servers: dict, mtime: float) -> dict:
    """Diff an explicit server dict (file loader or test/config-dict loader)."""
    global _MCP_CONFIG_MTIME
    async with _reload_lock_for_current_loop():
        desired: Dict[str, dict] = {}
        invalid_names = set()
        for name, sdata in servers.items():
            spec = _spec_from_config(sdata)
            if spec is not None:
                desired[str(name)] = spec
            elif str(name) in _GLOBAL_MCP_STATE:
                # Malformed entry: keep the previous working registration
                # (treating it as "removed" would silently lose tools).
                invalid_names.add(str(name))

        errors: list = [f"{name}: malformed [mcp.servers.{name}] (kept previous tools)"
                        for name in sorted(invalid_names)]
        decisions: Dict[str, str] = {}
        for name, spec in desired.items():
            old = _GLOBAL_MCP_STATE.get(name)
            if old and old.get("signature") == _spec_signature(spec):
                decisions[name] = "same"
                continue
            if old is None:
                # New server: nothing to lose if it fails; register directly
                # (also keeps the loader's monkeypatch contract testable).
                decisions[name] = "new"
                continue
            try:
                await _probe_mcp_spec(name, spec)
                decisions[name] = "replace"
            except Exception as exc:
                # Fail-safe: keep the previous working registration.
                decisions[name] = "failed"
                errors.append(f"{name}: {exc}")
                logger.warning("MCP hot reload: server '%s' probe failed (%s); keeping old tools", name, exc)

        removed = 0
        replacing: Dict[str, dict] = {}
        for name in list(_GLOBAL_MCP_STATE.keys()):
            if name not in desired:
                if name in invalid_names:
                    continue
                state = _GLOBAL_MCP_STATE.pop(name, None) or {}
                await _unregister_tool_names(state.get("tools"))
                await _stop_mcp_session_key(state.get("session_key") or name)
                removed += 1
            elif decisions.get(name) == "replace":
                state = _GLOBAL_MCP_STATE.pop(name, None) or {}
                replacing[name] = state
                await _unregister_tool_names(state.get("tools"))
                await _stop_mcp_session_key(state.get("session_key") or name)

        registered = 0
        for name, spec in desired.items():
            decision = decisions.get(name)
            if decision in ("same", "failed"):
                continue
            try:
                tools, count = await _register_mcp_spec(name, spec)
                _GLOBAL_MCP_STATE[name] = {
                    "signature": _spec_signature(spec),
                    "tools": tools,
                    "session_key": name,
                    "url": spec.get("url"),
                    "transport": spec.get("transport"),
                    "spec": spec,
                }
                registered += count
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.warning("MCP hot reload: server '%s' registration failed: %s", name, exc)
                old = replacing.get(name)
                if old and old.get("spec"):
                    try:
                        old_tools, _old_count = await _register_mcp_spec(
                            name, old["spec"], session_key=name,
                        )
                        _GLOBAL_MCP_STATE[name] = {**old, "tools": old_tools}
                        logger.info("MCP hot reload: restored previous tools for '%s'", name)
                    except Exception as restore_exc:
                        errors.append(f"{name}: restore previous tools failed: {restore_exc}")
                        logger.warning("MCP hot reload: restore failed for '%s': %s", name, restore_exc)

        global _MCP_RETRY_AT, _MCP_RETRY_DELAY
        if errors:
            _MCP_RETRY_AT = time.monotonic() + _MCP_RETRY_DELAY
            _MCP_RETRY_DELAY = min(_MCP_RETRY_DELAY * 2.0, 300.0)
        else:
            _MCP_RETRY_AT = 0.0
            _MCP_RETRY_DELAY = 30.0
        _MCP_CONFIG_MTIME = mtime
        logger.info(
            "MCP hot reload: registered=%d removed=%d kept=%d errors=%d",
            registered, removed,
            sum(1 for v in decisions.values() if v == "same"),
            len(errors),
        )
        return {
            "ok": not errors,
            "errors": errors,
            "registered": registered,
            "removed": removed,
            "kept": sum(1 for v in decisions.values() if v == "same"),
            "servers": sorted(desired.keys()),
        }


async def sync_user_mcp_servers(user_id: str, rows: list, force: bool = False) -> dict:
    """Diff one user's enabled rows against process state.

    rows: objects/dicts with name, transport, command, args_json, env_json,
    url, api_key, enabled, updated_at. Removed/disabled rows are unregistered
    and their stdio process stopped. A changed row is probed before the old
    tools are dropped (failure keeps the old working set).
    """
    uid = str(user_id)
    # 2026-11-02（评审 I3）：私网判定（含 DNS，单次 ≤2s）必须在**进程级**
    # reload 锁**之外**完成。此前的写法把最多 20×2s 的解析放在锁内，一个用户
    # 用不可达域名的服务器就能阻塞其他所有用户的 run 启动（ensure_user_mcp_tools
    # 在每次 run 入口执行）。
    _cfg0 = get_config()
    _blocked_hosts: set = set()
    for _row in rows or []:
        try:
            _t = _row.get("transport") if isinstance(_row, dict) else getattr(_row, "transport", None)
            _u = _row.get("url") if isinstance(_row, dict) else getattr(_row, "url", None)
        except Exception:
            continue
        if _t == "http" and not _cfg0.user_mcp_allow_private_hosts and _u:
            try:
                _hn = urlsplit(str(_u)).hostname or ""
                if await _user_mcp_host_blocked(_hn):
                    _blocked_hosts.add(_u)
            except Exception:
                pass

    async with _reload_lock_for_current_loop():
        desired: Dict[str, dict] = {}
        signatures: Dict[str, str] = {}
        invalid_names = set()
        for row in rows or []:
            def _get(key, default=None):
                return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)

            if not bool(_get("enabled", True)):
                continue
            name = str(_get("name") or "")
            if not name:
                continue
            transport = str(_get("transport") or "http")
            if transport == "stdio":
                try:
                    args = json.loads(str(_get("args_json") or "[]"))
                except (ValueError, TypeError):
                    args = []
                try:
                    env = json.loads(str(_get("env_json") or "{}"))
                except (ValueError, TypeError):
                    env = {}
                spec = {
                    "transport": "stdio",
                    "command": [str(_get("command") or "")],
                    "args": [str(x) for x in (args if isinstance(args, list) else [])],
                    "env": {str(k): str(v) for k, v in (env.items() if isinstance(env, dict) else {})},
                    "raw": {},
                }
            else:
                spec = {
                    "transport": "http",
                    "url": str(_get("url") or ""),
                    "api_key": str(_get("api_key") or ""),
                    "raw": {},
                }
            if not (spec.get("command") and spec["command"][0]) and not spec.get("url"):
                if (uid, name) in _USER_MCP_STATE:
                    invalid_names.add(name)
                continue
            cfg = get_config()
            # Runtime kill switch: flipping [security] off must stop spawning
            # stdio processes / reaching private hosts for already-saved rows.
            if spec.get("transport") == "stdio" and not cfg.user_mcp_stdio_enabled:
                continue
            if spec.get("transport") == "http" and (spec.get("url") or "") in _blocked_hosts:
                logger.warning(
                    "User MCP '%s' (%s) skipped: host resolves to a private address "
                    "and [security].user_mcp_allow_private_hosts=false", name, uid,
                )
                continue
            desired[name] = spec
            signatures[name] = hashlib.sha256(
                (str(_get("updated_at") or "") + _spec_signature(spec)).encode("utf-8")
            ).hexdigest()

        errors: list = [f"{name}: malformed row (kept previous tools)" for name in sorted(invalid_names)]
        decisions: Dict[str, str] = {}
        now = time.monotonic()
        for name, spec in desired.items():
            old = _USER_MCP_STATE.get((uid, name))
            if old and not force and old.get("signature") == signatures[name]:
                decisions[name] = "same"
                continue
            cached = _USER_MCP_FAILED.get((uid, name))
            if cached and cached[0] > now and not force:
                decisions[name] = "failed"
                errors.append(f"{name}: {cached[1]} (retry after {int(cached[0] - now)}s)")
                continue
            try:
                await _probe_mcp_spec(name, spec, owner=uid)
                _USER_MCP_FAILED.pop((uid, name), None)
                decisions[name] = "replace" if old else "new"
            except Exception as exc:
                decisions[name] = "failed"
                _USER_MCP_FAILED[(uid, name)] = (now + 30.0, str(exc))
                errors.append(f"{name}: {exc}")
                logger.warning("User MCP '%s' (%s) probe failed: %s; keeping old tools", name, uid, exc)

        removed = 0
        replacing: Dict[str, dict] = {}
        for key in list(_USER_MCP_STATE.keys()):
            row_uid, name = key
            if row_uid != uid:
                continue
            if name not in desired:
                if name in invalid_names:
                    continue
                state = _USER_MCP_STATE.pop(key, None) or {}
                await _unregister_tool_names(state.get("tools"))
                await _stop_mcp_session_key(state.get("session_key") or user_mcp_session_key(uid, name))
                removed += 1
            elif decisions.get(name) == "replace":
                state = _USER_MCP_STATE.pop(key, None) or {}
                replacing[name] = state
                await _unregister_tool_names(state.get("tools"))
                await _stop_mcp_session_key(state.get("session_key") or user_mcp_session_key(uid, name))

        registered = 0
        for name, spec in desired.items():
            decision = decisions.get(name)
            if decision in ("same", "failed"):
                continue
            server_name = user_mcp_tool_server(uid, name)
            session_key = user_mcp_session_key(uid, name)
            try:
                tools, count = await _register_mcp_spec(
                    name, spec, owner_user_id=uid, session_key=session_key,
                    tool_name_server=user_mcp_tool_server(uid, name),
                )
                _USER_MCP_STATE[(uid, name)] = {
                    "signature": signatures[name],
                    "tools": tools,
                    "session_key": session_key,
                    "transport": spec.get("transport"),
                    "spec": spec,
                }
                registered += count
                _USER_MCP_FAILED.pop((uid, name), None)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                _USER_MCP_FAILED[(uid, name)] = (time.monotonic() + 30.0, str(exc))
                logger.exception("User MCP '%s' (%s) registration failed: %s", name, uid, exc)
                old = replacing.get(name)
                if old and old.get("spec"):
                    try:
                        old_tools, _old_count = await _register_mcp_spec(
                            name, old["spec"], owner_user_id=uid,
                            session_key=session_key,
                            tool_name_server=user_mcp_tool_server(uid, name),
                        )
                        _USER_MCP_STATE[(uid, name)] = {**old, "tools": old_tools}
                        logger.info("User MCP '%s' (%s): restored previous tools", name, uid)
                    except Exception as restore_exc:
                        errors.append(f"{name}: restore previous tools failed: {restore_exc}")

        return {
            "ok": not errors,
            "errors": errors,
            "registered": registered,
            "removed": removed,
            "tools": sorted(
                n for (row_uid, _name), state in _USER_MCP_STATE.items()
                if row_uid == uid for n in state.get("tools", [])
            ),
        }


def user_mcp_runtime_status(user_id: str) -> list:
    uid = str(user_id)
    return [
        {
            "name": name,
            "tools_count": len(state.get("tools") or []),
            "tools": sorted(state.get("tools") or []),
            "transport": state.get("transport"),
        }
        for (row_uid, name), state in _USER_MCP_STATE.items()
        if row_uid == uid
    ]


async def _mcp_config_watch_loop() -> None:
    global _MCP_CONFIG_MTIME
    while True:
        interval = 5.0
        try:
            interval = float(get_config().mcp_hot_reload_interval_seconds)
        except Exception:
            pass
        await asyncio.sleep(max(1.0, min(interval, 60.0)))
        try:
            path = str(get_config().config_path)
            mtime = os.stat(path).st_mtime
            if mtime != _MCP_CONFIG_MTIME or (_MCP_RETRY_AT and time.monotonic() >= _MCP_RETRY_AT):
                await reload_mcp_servers_from_config(path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("MCP config watcher tick failed", exc_info=True)


def start_mcp_hot_reload() -> None:
    global _MCP_WATCH_TASK, _MCP_CONFIG_MTIME
    if _MCP_WATCH_TASK is not None:
        return
    try:
        if not get_config().mcp_hot_reload_enabled:
            logger.info("MCP hot reload disabled by [mcp].hot_reload_enabled")
            return
    except Exception:
        pass
    try:
        _MCP_CONFIG_MTIME = os.stat(str(get_config().config_path)).st_mtime
    except Exception:
        _MCP_CONFIG_MTIME = 0.0
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("MCP hot reload not started: no running loop")
        return
    _MCP_WATCH_TASK = loop.create_task(_mcp_config_watch_loop())
    logger.info("MCP hot reload watcher started (interval %.0fs)",
                get_config().mcp_hot_reload_interval_seconds)


def stop_mcp_hot_reload() -> None:
    global _MCP_WATCH_TASK
    task = _MCP_WATCH_TASK
    _MCP_WATCH_TASK = None
    if task is not None:
        task.cancel()
