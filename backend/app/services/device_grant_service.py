# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Single-use device tool-call grants (PLAN T6).

The cloud signs with the current authenticated WS session's pairing secret.
The device must verify signature/binding/expiry and atomically consume the
nonce before execution. Stateless verification here deliberately accepts a
``consumed_nonces`` view so replay checks do not pretend to be a ledger.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Container, Mapping
from typing import Any

GRANT_VERSION = 1
DEFAULT_GRANT_TTL_SECONDS = 120


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Cross-language canonical JSON: sorted keys, compact, ASCII, no NaN."""
    return json.dumps(
        args,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def tool_args_sha256(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_tool_args(args).encode("utf-8")).hexdigest()


def _scope_segment(scope: Mapping[str, Any] | None) -> str:
    """作用域段（2026-11-10 §11.2）。缺省空串 = 旧客户端/legacy 会话的签名形状。

    用 canonical JSON（固定键序）而不是 "|" 拼接：storage_authority 后缀用户可控，
    裸拼接允许 `a|b` 与 `a`+`b` 撞同一签名域（评审 r2 #9）。
    """
    if not scope:
        return ""
    keys = ("storage_authority", "task_id", "run_id", "attempt_id", "binding_version",
            "lease_epoch", "scope")
    canonical = {k: ("" if scope.get(k) is None else str(scope.get(k))) for k in keys}
    return "|" + json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"))


def _message(device_id: str, tool: str, args_digest: str, nonce: str, exp: int,
             scope: Mapping[str, Any] | None = None) -> bytes:
    return f"{device_id}|{tool}|{args_digest}|{nonce}|{exp}{_scope_segment(scope)}".encode("utf-8")


def issue_device_grant(
    pairing_secret: str,
    device_id: str,
    tool: str,
    args: Mapping[str, Any],
    *,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    nonce: str | None = None,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not pairing_secret or not device_id or not tool:
        raise ValueError("pairing_secret, device_id and tool are required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued_at = int(time.time() if now is None else now)
    exp = issued_at + int(ttl_seconds)
    grant_nonce = nonce or secrets.token_urlsafe(24)
    if not grant_nonce:
        raise ValueError("nonce must not be empty")
    args_digest = tool_args_sha256(args)
    signature = hmac.new(
        pairing_secret.encode("utf-8"),
        _message(device_id, tool, args_digest, grant_nonce, exp, scope),
        hashlib.sha256,
    ).hexdigest()
    payload = {
        "version": GRANT_VERSION,
        "nonce": grant_nonce,
        "exp": exp,
        "signature": signature,
    }
    if scope:
        # 作用域字段随 grant 一起下发，设备侧可校验自己拿到的就是签的那份
        payload["scope"] = dict(scope)
    return payload


def verify_device_grant(
    pairing_secret: str,
    device_id: str,
    tool: str,
    args: Mapping[str, Any],
    grant: Mapping[str, Any] | None,
    *,
    now: int | None = None,
    consumed_nonces: Container[str] = (),
) -> bool:
    """Verify without consuming; the client ledger owns atomic single-use."""
    if not pairing_secret or not isinstance(grant, Mapping):
        return False
    try:
        version = grant.get("version")
        nonce = grant.get("nonce")
        exp = grant.get("exp")
        signature = grant.get("signature")
        if version != GRANT_VERSION or not isinstance(nonce, str) or not nonce:
            return False
        if isinstance(exp, bool) or not isinstance(exp, int):
            return False
        if not isinstance(signature, str) or len(signature) != 64:
            return False
        current = int(time.time() if now is None else now)
        if current >= exp or nonce in consumed_nonces:
            return False
        args_digest = tool_args_sha256(args)
        # 带作用域的 grant 必须连作用域一起验（缺字段/被改写的旧版签名一律不过）
        scope = grant.get("scope") if isinstance(grant.get("scope"), Mapping) else None
        expected = hmac.new(
            pairing_secret.encode("utf-8"),
            _message(device_id, tool, args_digest, nonce, exp, scope),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
