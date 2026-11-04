# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P1d: one-time, action-bound cryptographic user-authorisation tokens.

The chat approval callback remains the human channel.  A successful approval
issues a signed token bound to user+conversation+action fingerprint+tool+nonce+
expiry; the act gate consumes it atomically.  Replay, expiry, a wrong action or
a tampered row all fail closed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

from app.services import verdict_log_service

logger = logging.getLogger(__name__)

_TTL_SECONDS = 120


def _default_ttl() -> int:
    from app.core.config import get_config
    try:
        value = int(get_config().security.get("tma_action_token_ttl", _TTL_SECONDS))
    except (TypeError, ValueError):
        value = _TTL_SECONDS
    return max(1, value)


def _secret() -> bytes:
    material = verdict_log_service._key_material()
    if not material:
        material = "weave-tma-action-dev"
    return ("tma-action\x00" + material).encode("utf-8")


def _payload(
    token_id: str,
    nonce: str,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    action_fingerprint: str,
    tool_name: str,
    issued_at: int,
    expires_at: int,
) -> dict[str, Any]:
    return {
        "token_id": token_id,
        "nonce": nonce,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "action_fingerprint": action_fingerprint,
        "tool_name": tool_name,
        "issued_at_epoch": int(issued_at),
        "expires_at_epoch": int(expires_at),
    }


def _sign(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_token_payload(
    payload: dict[str, Any], *, now: int | None = None,
) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "malformed"
    sig = str(payload.get("sig") or "")
    body = {key: value for key, value in payload.items() if key != "sig"}
    expected = _sign(body)
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        return False, "signature"
    if int(payload.get("expires_at_epoch") or 0) <= int(
        now if now is not None else time.time()
    ):
        return False, "expired"
    return True, ""


async def issue_action_token(
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    action_fingerprint: str,
    tool_name: str,
    ttl_seconds: int | None = None,
) -> str | None:
    from sqlalchemy import text
    from app.db.database import AsyncSessionLocal

    ttl = _default_ttl() if ttl_seconds is None else max(1, int(ttl_seconds))
    now = int(time.time())
    token_id = str(uuid.uuid4())
    nonce = uuid.uuid4().hex
    payload = _payload(
        token_id, nonce, str(tenant_id or ""), str(user_id or ""),
        str(conversation_id or ""), str(action_fingerprint or ""),
        str(tool_name or ""), now, now + ttl,
    )
    payload["sig"] = _sign(payload)
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO memory_action_tokens
                        (token_id, tenant_id, user_id, conversation_id,
                         action_fingerprint, tool_name, nonce,
                         issued_at_epoch, expires_at_epoch, used_at_epoch, sig)
                    VALUES
                        (:token_id, :tenant_id, :user_id, :conversation_id,
                         :action_fingerprint, :tool_name, :nonce,
                         :issued_at_epoch, :expires_at_epoch, 0, :sig)
                    """
                ),
                payload,
            )
            await db.commit()
        return token_id
    except Exception:
        logger.warning("action token issue failed", exc_info=True)
        return None


async def consume_action_token(
    token_id: str,
    *,
    action_fingerprint: str,
    user_id: str,
    conversation_id: str,
    tenant_id: str = "",
    tool_name: str = "",
) -> bool:
    from sqlalchemy import text
    from app.db.database import AsyncSessionLocal

    now = int(time.time())
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                text(
                    "SELECT token_id, nonce, tenant_id, user_id, conversation_id, "
                    "action_fingerprint, tool_name, issued_at_epoch, "
                    "expires_at_epoch, used_at_epoch, sig "
                    "FROM memory_action_tokens WHERE token_id = :tid"
                ),
                {"tid": str(token_id or "")},
            )).fetchone()
            if not row:
                return False
            values = list(row)
            payload = {
                "token_id": values[0],
                "nonce": values[1],
                "tenant_id": values[2],
                "user_id": values[3],
                "conversation_id": values[4],
                "action_fingerprint": values[5],
                "tool_name": values[6],
                "issued_at_epoch": int(values[7] or 0),
                "expires_at_epoch": int(values[8] or 0),
                "sig": values[10],
            }
            ok, reason = verify_token_payload(payload, now=now)
            if not ok:
                logger.warning("action token rejected: %s", reason)
                return False
            if (
                str(payload["action_fingerprint"]) != str(action_fingerprint or "")
                or str(payload["user_id"]) != str(user_id or "")
                or str(payload["conversation_id"]) != str(conversation_id or "")
                or (tenant_id and str(payload["tenant_id"]) != str(tenant_id))
                or (tool_name and str(payload["tool_name"]) != str(tool_name))
            ):
                logger.warning("action token binding mismatch")
                return False
            result = await db.execute(
                text(
                    "UPDATE memory_action_tokens SET used_at_epoch = :now "
                    "WHERE token_id = :tid AND used_at_epoch = 0 "
                    "AND expires_at_epoch > :now"
                ),
                {"now": now, "tid": str(token_id)},
            )
            if (result.rowcount or 0) != 1:
                return False
            await db.commit()
            return True
    except Exception:
        logger.warning("action token consume failed", exc_info=True)
        return False
