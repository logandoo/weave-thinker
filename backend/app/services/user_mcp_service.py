# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""用户级 MCP server — 校验 / CRUD / 掩码 / 运行期注册（2026-11-01）。

设计要点：
- 每用户至多 ``MAX_SERVERS_PER_USER`` 个 server；name 为 slug，用户内唯一。
- 凭据（api_key / env value）只写不读：GET 只回 `***`；更新时 `***`/None
  表示保留原值，空串表示清除。
- 保存后立即 ``ensure_user_mcp_tools(force=True)``：当前 worker 的下一轮
  AgentLoop 即可见；其他 worker 在 run 入口 revalidate 时收敛。
- 连接失败不清掉已保存配置：返回 ``ok: false`` + error，工具不注册（诚实）。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from sqlalchemy import select

from app.core.config import get_config

from app.db.database import AsyncSessionLocal, UserMcpServer
from app.tools import mcp_client

logger = logging.getLogger(__name__)

MAX_SERVERS_PER_USER = 20
MAX_ARGS = 64
MAX_ENV = 64
MAX_ARG_CHARS = 1000
MAX_ENV_VALUE_CHARS = 4000
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
MASK = "***"

# 进程内只读状态缓存（可选；当前 ensure 每次直查 DB，保证 CRUD 即时生效）。
_USER_CACHE: Dict[str, Any] = {}


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return str(value)


def _decode_json(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(str(raw or ""))
    except (ValueError, TypeError):
        return default
    return value


def _mask_secret(value: str) -> str:
    return MASK if value else ""


def serialize_server(row) -> dict:
    api_key = str(getattr(row, "api_key", None) or "")
    env = _decode_json(getattr(row, "env_json", None), {})
    if not isinstance(env, dict):
        env = {}
    args = _decode_json(getattr(row, "args_json", None), [])
    if not isinstance(args, list):
        args = []
    return {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "url": row.url or "",
        "api_key": _mask_secret(api_key),
        "has_api_key": bool(api_key),
        "command": row.command or "",
        "args": [str(x) for x in args],
        "env": {str(k): (_mask_secret(str(v)) if v != "" else "") for k, v in env.items()},
        "enabled": bool(row.enabled),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _string_list(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ARGS:
        raise ValueError(f"{field} must be a list of at most {MAX_ARGS} strings")
    out = []
    for item in value:
        if not isinstance(item, str) or len(item) > MAX_ARG_CHARS:
            raise ValueError(f"{field} entries must be strings <= {MAX_ARG_CHARS} chars")
        out.append(item)
    return out


def _env_dict(value: Any, existing: dict) -> Dict[str, str]:
    if value is None:
        return dict(existing)
    if not isinstance(value, dict) or len(value) > MAX_ENV:
        raise ValueError(f"env must be an object with at most {MAX_ENV} keys")
    out = dict(existing)
    for key, raw in value.items():
        key_s = str(key)
        if not key_s or len(key_s) > 128:
            raise ValueError("env key must be 1..128 chars")
        if raw == MASK:
            continue
        if not isinstance(raw, str) or len(raw) > MAX_ENV_VALUE_CHARS:
            raise ValueError(f"env[{key_s}] must be a string <= {MAX_ENV_VALUE_CHARS} chars")
        out[key_s] = raw
    return out


def _validate_enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    return value


def _validate_common(name: str, transport: str) -> None:
    if not NAME_RE.fullmatch(str(name or "")):
        raise ValueError("name must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    if transport not in ("http", "stdio"):
        raise ValueError("transport must be 'http' or 'stdio'")


def _is_private_host(host: str) -> bool:
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


async def _host_is_private(host: str) -> bool:
    """Literal + DNS check without blocking the event loop (2s budget)."""
    if _is_private_host(host):
        return True
    if not host:
        return False
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None), timeout=2.0)
    except Exception:
        return False
    for info in infos or []:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


async def _validate_transport(transport: str, *, url: str, command: str) -> None:
    cfg = get_config()
    if transport == "http":
        if not url or len(url) > 1000 or not url.lower().startswith(("http://", "https://")):
            raise ValueError("http transport requires an http(s) url (<=1000 chars)")
        if command:
            raise ValueError("http transport must not set command")
        # SSRF guard: user-level HTTP MCP must not reach loopback/private/
        # link-local metadata by default. Operators running trusted local MCPs
        # can opt in with [security].user_mcp_allow_private_hosts = true.
        if await _host_is_private(urlsplit(url).hostname or "") and not cfg.user_mcp_allow_private_hosts:
            raise ValueError(
                "user MCP http url points at a private/loopback host; "
                "set [security].user_mcp_allow_private_hosts=true only on trusted deployments"
            )
    else:
        if not command or len(command) > 1000:
            raise ValueError("stdio transport requires a command (<=1000 chars)")
        if url:
            raise ValueError("stdio transport must not set url")
        # stdio = server-side process execution; opt-in on trusted deployments.
        if not cfg.user_mcp_stdio_enabled:
            raise ValueError(
                "user MCP stdio transport is disabled; "
                "enable [security].user_mcp_stdio_enabled only on trusted deployments"
            )


async def _rows_for_user(db, user_id: str) -> List[UserMcpServer]:
    result = await db.execute(
        select(UserMcpServer).where(UserMcpServer.user_id == str(user_id))
    )
    return list(result.scalars().all())


def _runtime_by_name(user_id: str) -> Dict[str, dict]:
    try:
        return {item["name"]: item for item in mcp_client.user_mcp_runtime_status(user_id)}
    except Exception:
        return {}


def _with_runtime(row, runtime: Dict[str, dict]) -> dict:
    data = serialize_server(row)
    status = runtime.get(data["name"]) or {}
    data["runtime"] = {
        "registered": bool(status),
        "tools_count": int(status.get("tools_count") or 0),
        "tools": status.get("tools") or [],
    }
    return data


async def list_servers(db, user_id: str) -> List[dict]:
    rows = await _rows_for_user(db, user_id)
    runtime = _runtime_by_name(user_id)
    return [_with_runtime(row, runtime) for row in rows]


async def get_server(db, user_id: str, server_id: str) -> Optional[dict]:
    for row in await _rows_for_user(db, user_id):
        if str(row.id) == str(server_id):
            return _with_runtime(row, _runtime_by_name(user_id))
    return None


async def _ensure_capacity(db, user_id: str) -> None:
    rows = await _rows_for_user(db, user_id)
    if len(rows) >= MAX_SERVERS_PER_USER:
        raise ValueError(f"at most {MAX_SERVERS_PER_USER} MCP servers per user")


async def create_server(db, user_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    name = str(payload.get("name") or "").strip()
    transport = str(payload.get("transport") or "http").strip().lower()
    _validate_common(name, transport)
    url = str(payload.get("url") or "").strip()
    command = str(payload.get("command") or "").strip()
    await _validate_transport(transport, url=url, command=command)
    args = _string_list(payload.get("args"), "args")
    env = _env_dict(payload.get("env"), {})
    api_key = str(payload.get("api_key") or "").strip()
    if len(api_key) > 1000:
        raise ValueError("api_key must be <= 1000 chars")

    await _ensure_capacity(db, user_id)
    for row in await _rows_for_user(db, user_id):
        if str(row.name) == name:
            raise ValueError(f"MCP server '{name}' already exists")

    row = UserMcpServer(
        user_id=str(user_id),
        name=name,
        transport=transport,
        url=url or None,
        api_key=api_key or None,
        command=command or None,
        args_json=json.dumps(args, ensure_ascii=False),
        env_json=json.dumps(env, ensure_ascii=False),
        enabled=_validate_enabled(payload.get("enabled", True)),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    runtime = await ensure_user_mcp_tools(db, user_id, force=True)
    data = _with_runtime(row, _runtime_by_name(user_id))
    data["runtime"]["error"] = "; ".join(runtime.get("errors") or []) or None
    data["ok"] = bool(runtime.get("ok"))
    if not runtime.get("ok"):
        data["error"] = data["runtime"]["error"]
    return data


async def update_server(db, user_id: str, server_id: str, payload: dict) -> Optional[dict]:
    row = None
    for candidate in await _rows_for_user(db, user_id):
        if str(candidate.id) == str(server_id):
            row = candidate
            break
    if row is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    name = str(payload.get("name", row.name) or "").strip()
    transport = str(payload.get("transport", row.transport) or "http").strip().lower()
    _validate_common(name, transport)
    url = str(payload.get("url", row.url or "") or "").strip()
    command = str(payload.get("command", row.command or "") or "").strip()
    await _validate_transport(transport, url=url, command=command)

    if name != row.name:
        for other in await _rows_for_user(db, user_id):
            if other.id != row.id and str(other.name) == name:
                raise ValueError(f"MCP server '{name}' already exists")

    args = _string_list(payload.get("args"), "args") if "args" in payload else _decode_json(row.args_json, [])
    if not isinstance(args, list):
        args = []
    env = _env_dict(payload.get("env"), _decode_json(row.env_json, {}) if isinstance(_decode_json(row.env_json, {}), dict) else {})
    raw_key = payload.get("api_key", None)
    if raw_key is None or raw_key == MASK:
        api_key = str(row.api_key or "")
    else:
        api_key = str(raw_key).strip()
        if len(api_key) > 1000:
            raise ValueError("api_key must be <= 1000 chars")

    row.name = name
    row.transport = transport
    row.url = url or None
    row.command = command or None
    row.api_key = api_key or None
    row.args_json = json.dumps([str(x) for x in args], ensure_ascii=False)
    row.env_json = json.dumps(env, ensure_ascii=False)
    row.enabled = _validate_enabled(payload.get("enabled", row.enabled))
    row.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(row)
    runtime = await ensure_user_mcp_tools(db, user_id, force=True)
    data = _with_runtime(row, _runtime_by_name(user_id))
    data["runtime"]["error"] = "; ".join(runtime.get("errors") or []) or None
    data["ok"] = bool(runtime.get("ok"))
    if not runtime.get("ok"):
        data["error"] = data["runtime"]["error"]
    return data


async def delete_server(db, user_id: str, server_id: str) -> bool:
    row = None
    for candidate in await _rows_for_user(db, user_id):
        if str(candidate.id) == str(server_id):
            row = candidate
            break
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    await ensure_user_mcp_tools(db, user_id, force=True)
    return True


async def prepare_user_mcp_for_run(user_id: str) -> dict:
    """Run-entry revalidation with its own short-lived DB session."""
    async with AsyncSessionLocal() as db:
        return await ensure_user_mcp_tools(db, str(user_id))


async def ensure_user_mcp_tools(db, user_id: str, force: bool = False) -> dict:
    """Revalidate this user's enabled rows and apply the diff in-process."""
    rows = await _rows_for_user(db, user_id)
    try:
        return await mcp_client.sync_user_mcp_servers(str(user_id), rows, force=force)
    except Exception as exc:
        logger.exception("User MCP sync failed for user %s", user_id)
        return {"ok": False, "errors": [str(exc)], "registered": 0, "removed": 0, "tools": []}


def runtime_status(user_id: str) -> list:
    return mcp_client.user_mcp_runtime_status(str(user_id))
