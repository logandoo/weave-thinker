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


def _message(device_id: str, tool: str, args_digest: str, nonce: str, exp: int) -> bytes:
    return f"{device_id}|{tool}|{args_digest}|{nonce}|{exp}".encode("utf-8")


def issue_device_grant(
    pairing_secret: str,
    device_id: str,
    tool: str,
    args: Mapping[str, Any],
    *,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    nonce: str | None = None,
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
        _message(device_id, tool, args_digest, grant_nonce, exp),
        hashlib.sha256,
    ).hexdigest()
    return {
        "version": GRANT_VERSION,
        "nonce": grant_nonce,
        "exp": exp,
        "signature": signature,
    }


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
        expected = hmac.new(
            pairing_secret.encode("utf-8"),
            _message(device_id, tool, args_digest, nonce, exp),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
