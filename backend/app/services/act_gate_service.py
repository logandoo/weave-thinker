# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P1a TMA-NM: deterministic act gate (M3), shadow by default.

The gate never asks an LLM.  It permits a consequential action only when the
conversation taint is not ``untrusted``, or when at least two **independent**
trusted principals corroborate it.  Duplicated untrusted sources never count.

``shadow`` records the policy verdict without changing execution; ``enforce``
returns a recoverable tool error for a denied action.  ``off`` bypasses the
gate (rollback).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.services import taint_service, verdict_log_service

logger = logging.getLogger(__name__)

ACT_TOOLS = frozenset({
    "terminal",
    "execute_code",
    "process",
    "job_submit",
    "skill_run_script",
    "provide_file",
    "provide_folder",
    "pdf_export",
    "workspace_write",
    "workspace_edit",
    "git",
    "schedule",
    "background_task",
    "delegate_task",
    "delegate_agent_task",
})
ACT_PREFIXES = ("browser_", "mcp_")
# 2026-11-01: per-user MCP management writes config; gate it like mcp_* even
# though its name intentionally avoids the mcp_ prefix (progressive-discovery
# tests treat mcp_* schema names as MCP server tools).
ACT_EXTRA = frozenset({"manage_mcp_server"})
MODES = ("off", "shadow", "enforce")


@dataclass
class ActVerdict:
    decision: str                       # allow | deny | ask | elevate
    reason: str
    taint: str
    corroborators: list[str] = field(default_factory=list)
    fingerprint: str = ""
    blocked: bool = False
    mode: str = "shadow"


def is_act_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    return name in ACT_TOOLS or name in ACT_EXTRA or any(name.startswith(p) for p in ACT_PREFIXES)


def action_fingerprint(
    tool_name: str,
    args: dict[str, Any] | None,
    user_id: str = "",
    conversation_id: str = "",
) -> str:
    clean = {
        str(key): value
        for key, value in (args or {}).items()
        if not str(key).startswith("_")
    }
    payload = json.dumps(
        {
            "tool": str(tool_name or ""),
            "args": clean,
            "user_id": str(user_id or ""),
            "conversation_id": str(conversation_id or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate(
    ledger: taint_service.TaintLedger,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    user_id: str = "",
    conversation_id: str = "",
    mode: str | None = None,
) -> ActVerdict:
    from app.core.config import get_config
    current_mode = str(mode or get_config().tma_mode or "shadow").strip().lower()
    if current_mode not in MODES:
        current_mode = "shadow"
    taint = ledger.max_taint

    if not is_act_tool(tool_name):
        return ActVerdict(
            "allow", "not_consequential", taint, [], "", False, current_mode,
        )
    if current_mode == "off":
        return ActVerdict(
            "allow", "gate_off", taint, [], "", False, current_mode,
        )
    fingerprint = action_fingerprint(tool_name, args, user_id, conversation_id)
    if taint != taint_service.TAINT_UNTRUSTED:
        return ActVerdict(
            "allow", f"taint_{taint}", taint, [], fingerprint, False, current_mode,
        )

    corroborators = ledger.trusted_principals()
    if len(corroborators) >= 2:
        return ActVerdict(
            "elevate",
            "untrusted source corroborated by independent trusted principals",
            taint,
            corroborators,
            fingerprint,
            False,
            current_mode,
        )

    reason = (
        "untrusted conversation taint; no independent trusted corroborators "
        f"({len(corroborators)}/2)"
    )
    return ActVerdict(
        "deny",
        reason,
        taint,
        corroborators,
        fingerprint,
        blocked=(current_mode == "enforce"),
        mode=current_mode,
    )


def tool_error(verdict: ActVerdict) -> str:
    return json.dumps(
        {
            "error": "act_gate_denied",
            "recoverable": True,
            "decision": verdict.decision,
            "taint": verdict.taint,
            "corroborators": verdict.corroborators,
            "action_fingerprint": verdict.fingerprint,
            "reason": verdict.reason,
            "guidance": (
                "该动作依赖不可信来源，已被确定性 act gate 阻断。用户已拒绝/无法"
                "审批，或当前通道（后台/语音）没有审批对话框；不要重复相同调用。"
                "请说明风险并让用户改用文字聊天、拆分/缩短动作，或先提供独立可信"
                "佐证。enforce 的文字聊天授权是一次性、action-bound 的。"
            ),
        },
        ensure_ascii=False,
    )


async def record_user_authorization(
    verdict: ActVerdict,
    *,
    token_id: str,
    tool_name: str = "",
    user_id: str = "",
    conversation_id: str = "",
) -> bool:
    """Append the one-time user override for an already-denied action.

    The token is intentionally not stored: it authorises exactly the dispatch
    in flight.  A retry asks the user again (no replay window).  Returns False
    when the audit append failed; enforce callers must then NOT dispatch.
    """
    try:
        from app.services.tenant_service import tenant_segment
        return await verdict_log_service.append_verdict(
            tenant_id=tenant_segment(),
            user_id=user_id,
            conversation_id=conversation_id,
            action_fingerprint=verdict.fingerprint,
            tool_name=tool_name,
            decision="elevate",
            reason=f"user_authorized:{token_id}",
            taint=verdict.taint,
            corroborators=[],
            mode=verdict.mode,
            blocked=False,
        )
    except Exception:
        logger.warning("user authorization verdict log failed", exc_info=True)
        return False


async def deny_taint_unavailable(
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    user_id: str = "",
    conversation_id: str = "",
    mode: str = "enforce",
) -> ActVerdict:
    """Fail closed (and audit) when the taint store could not be read.

    A DB failure must never silently downgrade enforce to trusted.  Used by
    both the live loop and background crash recovery.
    """
    verdict = ActVerdict(
        decision="deny",
        reason="taint state unavailable; enforce fails closed",
        taint=taint_service.TAINT_UNTRUSTED,
        fingerprint=action_fingerprint(
            tool_name, args, user_id, conversation_id,
        ),
        blocked=True,
        mode=mode,
    )
    try:
        from app.services.tenant_service import tenant_segment
        await verdict_log_service.append_verdict(
            tenant_id=tenant_segment(),
            user_id=user_id,
            conversation_id=conversation_id,
            action_fingerprint=verdict.fingerprint,
            tool_name=tool_name,
            decision="deny",
            reason=verdict.reason,
            taint=verdict.taint,
            corroborators=[],
            mode=mode,
            blocked=True,
        )
    except Exception:
        logger.warning(
            "failed to log taint-unavailable deny (non-blocking)",
            exc_info=True,
        )
    return verdict


async def evaluate_and_log(
    ledger: taint_service.TaintLedger,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    user_id: str = "",
    conversation_id: str = "",
    mode: str | None = None,
) -> ActVerdict:
    verdict = evaluate(
        ledger, tool_name, args,
        user_id=user_id, conversation_id=conversation_id, mode=mode,
    )
    if verdict.mode != "off" and is_act_tool(tool_name):
        try:
            from app.services.tenant_service import tenant_segment
            await verdict_log_service.append_verdict(
                tenant_id=tenant_segment(),
                user_id=user_id,
                conversation_id=conversation_id,
                action_fingerprint=verdict.fingerprint,
                tool_name=tool_name,
                decision=verdict.decision,
                reason=verdict.reason,
                taint=verdict.taint,
                corroborators=verdict.corroborators,
                mode=verdict.mode,
                blocked=verdict.blocked,
            )
        except Exception:
            logger.debug("act gate verdict log failed (non-blocking)", exc_info=True)
    return verdict
