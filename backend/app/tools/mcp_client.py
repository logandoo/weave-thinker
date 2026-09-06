# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os
import re
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
        merged_env = dict(os.environ)
        merged_env.update(self.env)
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
    (conv 6dcae019, 2026-09-03 — the RemPilot switch to Streamable HTTP
    re-exposed the dotted-name 400 because cleaning existed on the stdio path
    only)."""
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
) -> int:
    _STDIO_SERVER_SPECS[server_name] = (list(command), list(args or []), dict(env or {}))
    try:
        session = await _get_or_create_stdio_session(server_name)
        tools_result = await session._request("tools/list", {}, timeout=15.0)
    except Exception as e:
        logger.warning("MCP stdio server '%s' registration failed: %s", server_name, e)
        return 0

    tools = []
    if isinstance(tools_result, dict) and "result" in tools_result:
        tools = tools_result["result"].get("tools", [])

    # OpenAI/DeepSeek 工具名契约 ^[a-zA-Z0-9_-]+$（wave3 2026-09-02）：RemPilot
    # 等 MCP 服务器的工具名带命名空间点号（rempilot.local_shell.open），拼接后
    # 必须清洗，否则整份 tools 数组被上游 400 拒绝（conv 7618b2c5）。dispatch
    # 走 handler 内部的原始 tool_name，清洗只影响对外注册名，调用不受影响。
    used_names = set(registry.get_all_tool_names())
    registered = 0
    for tool in tools:
        tool_name = tool.get("name", "")
        if not tool_name:
            continue
        mcp_tool_name = _sanitize_mcp_tool_name(server_name, tool_name, used_names)
        used_names.add(mcp_tool_name)
        schema = {
            "name": mcp_tool_name,
            "description": tool.get("description", f"MCP tool {tool_name} from {server_name}"),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        }
        handler = _MCPToolHandler(
            "", tool_name, transport="stdio", server_name=server_name
        )
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
    """Parse MCP response. Handles both plain JSON and SSE (data:) framing.
    RemPilot's Streamable HTTP returns plain JSON; other servers may use SSE."""
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
    headers.update(await _effective_headers(server_url, extra_headers))

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
    headers.update(await _effective_headers(server_url, extra_headers))

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
        headers.update(await _effective_headers(server_url, extra_headers))
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
                        logger.warning(
                            "MCP call received %d — session cache invalidated for %s",
                            resp.status_code, key,
                        )
                    provider = _OAUTH_BY_URL.get(server_url)
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
# Some HTTP MCP endpoints (e.g. RemPilot Streamable HTTP) require an OAuth
# Authorization Code + PKCE flow whose access token expires (~1h). The harness
# persists the refresh token in a local state file (config key
# `oauth_state_file` under [mcp.servers.<name>]) and refreshes lazily: token()
# refreshes when the cached access token is expired, and a 401/403 on a call
# invalidates + force-refreshes once before retrying.
_OAUTH_BY_URL: Dict[str, "_OAuthProvider"] = {}


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
    server_url: str, extra_headers: Dict[str, str] = None
) -> Dict[str, str]:
    """Static extra headers plus a live OAuth Bearer when available."""
    hdrs = dict(extra_headers or {})
    provider = _OAUTH_BY_URL.get(server_url)
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
    ):
        self.server_url = server_url
        self.tool_name = tool_name
        self.session_id = session_id
        self.extra_headers = extra_headers
        self.transport = transport
        self.server_name = server_name

    async def __call__(self, args: dict, **kwargs) -> str:
        if self.transport == "stdio":
            return await _call_stdio_tool(self.server_name, self.tool_name, args)
        return await _mcp_call_tool(
            self.server_url,
            self.tool_name,
            args,
            self.session_id,
            self.extra_headers,
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
) -> int:
    if command:
        return await _register_stdio_server(server_name, command, args, env)
    return await _register_http_server(server_name, server_url, api_key)


async def _register_http_server(
    server_name: str,
    server_url: str,
    api_key: str = "",
) -> int:
    extra_headers = {}
    # OAuth provider（[mcp.servers.<name>].oauth_state_file）存在时
    # Authorization 由 _effective_headers 动态注入（token 会过期并自动刷新），
    # 静态 api_key 仅用于无 OAuth 的 HTTP MCP 服务器。
    if api_key and server_url not in _OAUTH_BY_URL:
        extra_headers["Authorization"] = f"Bearer {api_key}"

    session_id = await _mcp_initialize(server_url, extra_headers)
    tools = await _mcp_list_tools(server_url, session_id, extra_headers)

    if not tools:
        logger.warning("No tools found from MCP server '%s'", server_name)
        return 0

    used_names = set(registry.get_all_tool_names())
    registered = 0
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

    used_names = set(registry.get_all_tool_names())
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
    mcp_config = config.mcp
    if not isinstance(mcp_config, dict):
        return 0
    servers = mcp_config.get("servers", {})
    if not isinstance(servers, dict):
        return 0
    total = 0

    for name, sdata in servers.items():
        if not isinstance(sdata, dict):
            continue
        url = sdata.get("url", "")
        api_key = sdata.get("api_key", "")
        command = sdata.get("command")
        cmd_args = sdata.get("args") or []
        cmd_env = sdata.get("env") or {}
        if command:
            if isinstance(command, str):
                command = [command]
            try:
                count = await register_mcp_server_async(
                    name, "", "", command=command, args=cmd_args, env=cmd_env
                )
                total += count
            except Exception as e:
                logger.warning("Failed to register stdio MCP server '%s': %s", name, e)
            continue
        if not url:
            continue
        provider = _load_oauth_provider(sdata)
        if provider is not None:
            _OAUTH_BY_URL[url] = provider
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
