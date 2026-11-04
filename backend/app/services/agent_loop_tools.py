# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent-loop tool helpers (P5.2 move-only, 2026-10-18).

Tool signature/redundancy/evidence helpers moved verbatim from
``agent_loop.py``; the loop re-exports the same names.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time as _time
from typing import Any, Dict, List

from app.core.config import get_config
from app.db.database import AsyncSessionLocal
from app.services.agent_permissions import (
    PermissionContext,
    is_permission_allowed,
    permission_description,
    permission_key_for_tool_request,
)
from app.services.retry_utils import coerce_tool_args

from app.tools.registry import registry

config = get_config()
logger = logging.getLogger(__name__)


def _normalize_fetch_urls(tool_args: Dict[str, Any]) -> List[str]:
    """Extract the browser tool's `urls` argument as a clean list."""
    urls = tool_args.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    return [u.strip() for u in urls if isinstance(u, str) and u.strip()]

def _call_signature(tool_name: str, tool_args: Dict[str, Any]) -> tuple:
    """Stable (name, args-hash) signature — same construction for doom
    history, redundancy records and redundancy checks so all three agree on
    'identical'. Args are schema-coerced first (A4.9 R1 M3): the model's raw
    ``{"max_pages": "5"}`` and the coerced ``{"max_pages": 5}`` must hash
    identically or the exact-duplicate rule silently never matches."""
    args = tool_args
    try:
        schema = registry.get_schema(tool_name)
        if schema is not None:
            args = coerce_tool_args(tool_name, dict(tool_args or {}), schema)
    except Exception:
        args = tool_args
    return (
        tool_name,
        hashlib.sha256(
            json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest(),
    )

def _record_tool_success(
    state: "AgentLoopState", tool_name: str, tool_args: Dict[str, Any], result: str
) -> None:
    """Record a successful tool call for per-turn redundancy arbitration and
    result-aware doom detection.

    Error payloads ({"error": ...}) record nothing — an identical retry of a
    FAILED call must never be treated as redundant. The browser tool almost
    never returns a top-level error key (A4.9 R1 I1): page-level failures
    arrive as HTTP-200-shaped payloads whose pages all lack text — those
    record nothing either, so a retry after a transient 404/rate-limit is
    not suppressed. Only pages that actually returned text count as fetched.
    """
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("error"):
        return
    if tool_name == "browser":
        if not isinstance(parsed, dict):
            return
        pages_with_text = 0
        for page in parsed.get("pages") or []:
            if not isinstance(page, dict):
                continue
            url = (page.get("url") or "").strip()
            if url and page.get("text"):
                state._fetched_urls.add(url)
                pages_with_text += 1
        if pages_with_text == 0:
            return
    sig = _call_signature(tool_name, tool_args)
    state._succeeded_call_sigs.add(sig)
    hashes = state._doom_result_hashes.setdefault(sig, [])
    hashes.append(hashlib.sha256(result.encode()).hexdigest())
    state._doom_result_hashes[sig] = hashes[-6:]

def _is_redundant_call(state: "AgentLoopState", tool_call: Dict[str, Any]) -> bool:
    """Objective per-turn redundancy (never a content-quality guess):

    1. exact duplicate of a call that already SUCCEEDED this turn; or
    2. a non-paginated browser fetch whose URLs were all fetched this turn.
    ``paginated=true`` requests deeper pages of a URL — always new intent.
    Unparseable args are treated as new intent (never dropped).
    """
    fn = tool_call.get("function") or {}
    name = fn.get("name") or ""
    args_raw = fn.get("arguments")
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(args, dict):
        return False
    if _call_signature(name, args) in state._succeeded_call_sigs:
        return True
    if name == "browser" and not args.get("paginated"):
        urls = _normalize_fetch_urls(args)
        if urls and all(u in state._fetched_urls for u in urls):
            return True
    return False

def _calls_all_redundant(state: "AgentLoopState", tool_calls: List[Dict[str, Any]]) -> bool:
    return bool(tool_calls) and all(_is_redundant_call(state, tc) for tc in tool_calls)

def _tool_evidence_fragment(result: str, limit: int = 800) -> str:
    """Head+tail fragment of a tool result for the auditor context.

    conv a67faa04 (2026-08-14): head-only truncation hid tail-anchored
    evidence — the memory read of changelog.md is ~10.6k chars and its 版本记录
    section lives at the END of the file, so grounded drafts quoting that
    changelog were falsely accused of fabrication. Same 800-char budget as
    before (auditor is a repeated hot-path LLM call), but both ends are
    visible: `head … tail`.
    """
    if len(result) <= limit:
        return result
    half = (limit - 1) // 2
    return result[:half] + "…" + result[-half:]

def _wrap(name, *a, **k):
    from app.services import agent_loop as _al
    return getattr(_al, name)(*a, **k)


def ToolCallResult(*a, **k):
    from app.services.agent_loop import ToolCallResult as _C
    return _C(*a, **k)


def _clear_memory_read_cache(*a, **k):
    return _wrap("_clear_memory_read_cache", *a, **k)


def _detect_doom_loop(*a, **k):
    return _wrap("_detect_doom_loop", *a, **k)


def _maybe_dedupe_memory_read(*a, **k):
    return _wrap("_maybe_dedupe_memory_read", *a, **k)


def _track_memory_read(*a, **k):
    return _wrap("_track_memory_read", *a, **k)



class AgentToolMixin:
    async def _execute_single_tool_inner(
        self,
        call_id: str,
        tool_name: str,
        tool_args: dict,
        session_factory: Any,
        user: Any,
        conversation: Any,
        assistant: Any,
        state: AgentLoopState,
        current_turn_content: str = "",
        current_turn_tool_results: str = "",
    ) -> ToolCallResult:
        if tool_name in self.blocked_tools:
            logger.warning("Blocked tool call '%s' (depth=%d)", tool_name, self.delegation_depth)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=json.dumps({"error": f"Tool '{tool_name}' is blocked at this delegation depth"}),
                error=True,
            )

        factory = session_factory or self.session_factory
        if factory is None:
            from app.db.database import AsyncSessionLocal
            factory = AsyncSessionLocal

        # Per-turn memory-read dedup (conv dfc40619 2026-08-09): the
        # coordinator turn-focus directive + mandatory_tool_use rule 8 force
        # the model to re-read the same memory target every iteration — a
        # second identical read of the same target within ONE turn returns a
        # short note instead of re-injecting the whole 35KB document into
        # context. Placed BEFORE doom-loop tracking: a deduped call is
        # handled gracefully, not a loop to abort.
        if config.agent_tool_loop_memory_read_dedup and tool_name == "memory":
            _dedup_result = _maybe_dedupe_memory_read(state, tool_args)
            if _dedup_result is not None:
                logger.info(
                    "Memory read dedup: call=%s target=%s already read this turn",
                    call_id, tool_args.get("target") or "agent",
                )
                return ToolCallResult(
                    call_id=call_id,
                    name=tool_name,
                    arguments=tool_args,
                    result=_dedup_result,
                    error=False,
                )

        import time as _time
        _t_start = _time.monotonic()
        state._doom_tool_history.append(_call_signature(tool_name, tool_args))
        if len(state._doom_tool_history) > 6:
            state._doom_tool_history = state._doom_tool_history[-6:]

        doom_hit = _detect_doom_loop(state._doom_tool_history, state._doom_result_hashes)
        if doom_hit is not None:
            doom_name = doom_hit[0]
            # conv a8290d37: an abort-and-continue doom verdict is toothless —
            # the model re-issued the identical call 4 more times after the
            # error. Trigger = circuit breaker: force the final-answer stage
            # (tools shrink to execute_code only next iteration). Deathmatch
            # keeps its own judge (skip_guardrails) and only gets the abort.
            _force = (
                config.agent_tool_loop_doom_force_final
                and not self._skip_guardrails()
            )
            logger.warning(
                "Doom loop detected: %s called >=3x in last 6 with identical args and no new result — aborting%s",
                doom_name, " + forcing final answer" if _force else "",
            )
            if _force:
                state.force_final_answer = True
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=json.dumps({
                    "error": f"Doom loop detected: {doom_name} called >=3 times within the last 6 tool calls with identical arguments and no new information returned. Aborting to prevent infinite loop. 工具使用已受限——你已拥有该调用能提供的全部信息，请立即基于已有信息给出最终回答，不要再发起任何重复调用。"
                }, ensure_ascii=False),
                error=True,
            )

        try:
            schema = registry.get_schema(tool_name)
            tool_args = coerce_tool_args(tool_name, tool_args, schema)
            # A4.9 C-1: 剥离模型伪造的内部下划线控制键（权限门 fail-closed）。
            tool_args = self._strip_internal_tool_args(tool_args)

            async with factory() as db:
                dispatch_kwargs = dict(
                    db=db,
                    user=user,
                    conversation=conversation,
                    assistant=assistant,
                    workspace_path=self.workspace_path,
                )
                if getattr(self, "task_principal", ""):
                    dispatch_kwargs["_task_principal"] = self.task_principal
                if tool_name == "delegate_task" and self.delegation_depth > 0:
                    dispatch_kwargs["_delegation_depth"] = self.delegation_depth
                if tool_name == "delegate_task":
                    # W17：子代理继承父轮门控（非激活轮经委派路径不触发 gate/audit）
                    dispatch_kwargs["_vibeweaver_active"] = self.vibeweaver_active
                if current_turn_content:
                    dispatch_kwargs["current_turn_content"] = current_turn_content
                if current_turn_tool_results:
                    dispatch_kwargs["current_turn_tool_results"] = current_turn_tool_results

                perm_ctx = PermissionContext(
                    super_admin_bypass=config.super_admin_bypass,
                    deathmatch_active=(
                        self.deathmatch_manager is not None
                        and self.deathmatch_manager.is_goal_active
                    ),
                    user=user,
                )
                dispatch_kwargs["permission_context"] = perm_ctx
                if self._visible_tool_names is not None:
                    dispatch_kwargs["allowed_tools"] = self._visible_tool_names
                if tool_name == "search_tools" and not config.agent_tools_mcp_progressive_enabled \
                        and not bool(config.agent.get("tools_catalog_discovery_enabled", False)):
                    # A4.9 R1 M4：回滚模式下 search_tools 不在 schema（M1），
                    # 幻觉调用直接拒绝，恢复特性前"未知工具"语义。
                    return ToolCallResult(
                        call_id=call_id,
                        name=tool_name,
                        arguments=tool_args,
                        result=json.dumps({
                            "error": "工具 search_tools 当前不可用（扩展工具检索未启用）。"
                            "请直接基于已有信息回答或使用其他可见工具。"
                        }, ensure_ascii=False),
                        error=True,
                    )
                if tool_name == "search_tools" and self.blocked_tools:
                    # A4.9 R1 Minor-4（M4 收口）：搜索目录排除本 loop 显式
                    # blocked 的工具——不广告一个必然被 fail-close 的能力。
                    dispatch_kwargs["_blocked_tool_names"] = frozenset(self.blocked_tools)
                if tool_name == "search_tools":
                    # F8: exclude schemas already visible from the catalog
                    # results; expansion is therefore duplicate-free.
                    dispatch_kwargs["_active_tool_names"] = frozenset(self._active_tool_names)
                # T8 fail-closed (A4.9 r2 M5): a deathmatch step tool subset
                # constrains DISPATCH too, not just the offered schemas — a
                # hallucinated/DSML call outside the subset must not execute.
                _dm_dispatch_names = self._dm_step_tool_names()
                if _dm_dispatch_names is not None:
                    _allowed = dispatch_kwargs.get("allowed_tools")
                    _effective = (
                        _dm_dispatch_names if _allowed is None
                        else (set(_allowed) & _dm_dispatch_names)
                    )
                    if not _effective:
                        # A4.9 r3 M4: visibleTools ∩ subset = ∅ would fail-close
                        # EVERY tool (operator misconfiguration) — keep the dm
                        # subset and warn loudly instead of stalling the loop.
                        logger.warning(
                            "deathmatch tool subset ∩ visibleTools is empty — "
                            "using the step subset alone (check [agent.tools] visible_tools)"
                        )
                        _effective = _dm_dispatch_names
                    dispatch_kwargs["allowed_tools"] = _effective

                # P1a TMA-NM: deterministic act gate before any consequential
                # dispatch. shadow (default) only records; enforce blocks.
                from app.services import act_gate_service as _act_gate
                from app.services import taint_service as _taint
                _tma_mode = config.tma_mode
                _uid = str(getattr(user, "id", "") or "")
                _cid = str(
                    getattr(conversation, "id", "")
                    or getattr(state, "session_id", "")
                    or _taint.background_conversation_key(_uid)
                )
                _ledger = None
                if _tma_mode != "off":
                    _ledger = await _taint.load_ledger(db, _cid, _uid)
                    if (
                        _tma_mode == "enforce"
                        and _act_gate.is_act_tool(tool_name)
                        and not _ledger.loaded
                    ):
                        # A DB failure must never silently downgrade enforce
                        # to trusted for a consequential action; log and deny
                        # like recovery does.  Read-only tools still run.
                        _gate_verdict = await _act_gate.deny_taint_unavailable(
                            tool_name, tool_args,
                            user_id=_uid, conversation_id=_cid, mode=_tma_mode,
                        )
                    else:
                        if current_turn_content:
                            _ledger.mark(
                                f"user_turn:{_cid}", _taint.TAINT_TRUSTED,
                                origin="user", principal="user",
                                label="current user turn",
                            )
                        _gate_verdict = await _act_gate.evaluate_and_log(
                            _ledger, tool_name, tool_args,
                            user_id=_uid, conversation_id=_cid, mode=_tma_mode,
                        )
                    if _gate_verdict.blocked:
                        # No authoritative taint state = no approval path; the
                        # user cannot meaningfully consent to an unverifiable
                        # action.  Fail closed and keep the audit row.
                        if (
                            _tma_mode == "enforce"
                            and _ledger is not None
                            and not _ledger.loaded
                        ):
                            return ToolCallResult(
                                call_id=call_id,
                                name=tool_name,
                                arguments=tool_args,
                                result=_act_gate.tool_error(_gate_verdict),
                                error=True,
                            )
                        # P1b: chat-only, one-time action-bound user approval.
                        # Background/delegate have no callback and stay denied.
                        _approval_cb = getattr(self, "_act_gate_approval_callback", None)
                        _approved = False
                        if _approval_cb is not None:
                            try:
                                import inspect as _inspect

                                def _redact_value(key: Any, value: Any) -> Any:
                                    lowered = str(key).lower()
                                    if any(marker in lowered for marker in (
                                        "token", "secret", "password", "passwd",
                                        "authorization", "cookie", "api_key",
                                        "apikey", "api-key", "access-key",
                                        "private-key", "client-secret",
                                        "accesskey", "privatekey",
                                        "clientsecret",
                                    )):
                                        if isinstance(value, str):
                                            return f"[redacted, {len(value)} chars]"
                                        try:
                                            size = len(json.dumps(
                                                value, ensure_ascii=False,
                                                default=str,
                                            ))
                                        except Exception:
                                            size = 0
                                        return f"[redacted, {size} chars]"
                                    if isinstance(value, dict):
                                        return {
                                            str(k): _redact_value(k, v)
                                            for k, v in value.items()
                                        }
                                    if isinstance(value, list):
                                        return [_redact_value("", v) for v in value]
                                    return value

                                def _clean_mapping(mapping: dict) -> Any:
                                    pairs = [
                                        (_clean_display(str(k)), _clean_display(v))
                                        for k, v in mapping.items()
                                    ]
                                    if len({k for k, _ in pairs}) == len(pairs):
                                        return dict(pairs)
                                    # Never let two distinct submitted keys
                                    # collapse into one preview entry.
                                    return [
                                        {"key": k, "value": v} for k, v in pairs
                                    ]

                                def _clean_display(value: Any) -> Any:
                                    """Escape bidi/control chars so consent text
                                    cannot be visually reordered."""
                                    if isinstance(value, str):
                                        chars: list[str] = []
                                        for ch in value:
                                            code = ord(ch)
                                            if (
                                                0x202A <= code <= 0x202E
                                                or 0x2066 <= code <= 0x2069
                                                or 0x200B <= code <= 0x200F
                                                or code in (0x2060, 0xFEFF)
                                                or 0x80 <= code <= 0x9F
                                            ):
                                                chars.append(f"\\u{code:04x}")
                                            elif code < 0x20 and ch not in "\n\t\r":
                                                chars.append(f"\\x{code:02x}")
                                            else:
                                                chars.append(ch)
                                        return "".join(chars)
                                    if isinstance(value, dict):
                                        return _clean_mapping(value)
                                    if isinstance(value, list):
                                        return [_clean_display(v) for v in value]
                                    return value

                                # Review the ACTUAL submitted arguments, not a
                                # whitelist.  mcp/skill/terminal actions can put
                                # the real payload in any key; a whitelist
                                # silently hid it (and made the size guard
                                # measure only the preview).
                                _submitted = {
                                    str(key): value
                                    for key, value in (tool_args or {}).items()
                                    if not str(key).startswith("_")
                                }
                                _raw_review = json.dumps(
                                    _submitted, ensure_ascii=False, default=str,
                                )
                                if len(_raw_review) > 20000:
                                    return ToolCallResult(
                                        call_id=call_id,
                                        name=tool_name,
                                        arguments=tool_args,
                                        result=json.dumps({
                                            "error": "act_gate_review_too_large",
                                            "recoverable": True,
                                            "reason": (
                                                "act gate 动作参数超过 20000 字符，"
                                                "无法让用户完整审阅；请拆分/缩短动作"
                                                "或改用文件分块写入"
                                            ),
                                        }, ensure_ascii=False),
                                        error=True,
                                    )
                                _redacted_obj = {
                                    str(key): _redact_value(key, value)
                                    for key, value in _submitted.items()
                                }
                                _preview_obj = _clean_mapping(_redacted_obj)
                                _display = json.dumps(
                                    _preview_obj, ensure_ascii=False,
                                    default=str, indent=2,
                                )
                                if len(_display) > 20000:
                                    # Indentation pushed the preview over the
                                    # display budget; compact form keeps every
                                    # submitted arg visible.
                                    _display = json.dumps(
                                        _preview_obj, ensure_ascii=False,
                                        default=str, separators=(",", ":"),
                                    )
                                if len(_display) > 60000:
                                    # Escaping/redaction can expand the view
                                    # (e.g. thousands of zero-width chars).
                                    return ToolCallResult(
                                        call_id=call_id,
                                        name=tool_name,
                                        arguments=tool_args,
                                        result=json.dumps({
                                            "error": "act_gate_review_too_large",
                                            "recoverable": True,
                                            "reason": (
                                                "act gate 渲染预览超过 60000 字符，"
                                                "无法让用户完整审阅；请拆分/缩短动作"
                                            ),
                                        }, ensure_ascii=False),
                                        error=True,
                                    )
                                _approval_result = _approval_cb(
                                    conversation_id=_cid,
                                    tool_name=tool_name,
                                    description=(
                                        "Act gate：该动作依赖不可信来源，"
                                        "是否授权本次执行？"
                                    ),
                                    details={
                                        "act_gate": True,
                                        "fingerprint": _gate_verdict.fingerprint,
                                        "taint": _gate_verdict.taint,
                                        "reason": _gate_verdict.reason,
                                        "tool": tool_name,
                                        "user_id": _uid,
                                        # Fields the existing PermissionDialog
                                        # already renders (no frontend change):
                                        "command": _display,
                                        "target_path": _clean_display(
                                            (tool_args or {}).get("path")
                                            or (tool_args or {}).get("target_path")
                                            or ""
                                        ),
                                        "action": tool_name,
                                        "title": f"Act gate: {tool_name}",
                                        "_permission_description": (
                                            f"不可信来源动作 {tool_name}："
                                            f"{_gate_verdict.reason}"
                                        ),
                                    },
                                )
                                if _inspect.isawaitable(_approval_result):
                                    _approval_result = await _approval_result
                                _approved = bool(_approval_result)
                            except Exception:
                                logger.warning(
                                    "act gate approval callback failed; denying",
                                    exc_info=True,
                                )
                                _approved = False
                        if _approved:
                            from app.services import action_token_service as _tokens
                            from app.services.tenant_service import tenant_segment as _tenant
                            _token_id = await _tokens.issue_action_token(
                                tenant_id=_tenant(),
                                user_id=_uid,
                                conversation_id=_cid,
                                action_fingerprint=_gate_verdict.fingerprint,
                                tool_name=tool_name,
                            )
                            _token_ok = bool(_token_id) and await _tokens.consume_action_token(
                                _token_id,
                                action_fingerprint=_gate_verdict.fingerprint,
                                user_id=_uid,
                                conversation_id=_cid,
                                tenant_id=_tenant(),
                                tool_name=tool_name,
                            )
                            _audit_ok = _token_ok and await _act_gate.record_user_authorization(
                                _gate_verdict,
                                token_id=_token_id or "",
                                tool_name=tool_name,
                                user_id=_uid,
                                conversation_id=_cid,
                            )
                            if not _audit_ok:
                                return ToolCallResult(
                                    call_id=call_id,
                                    name=tool_name,
                                    arguments=tool_args,
                                    result=json.dumps({
                                        "error": "act_gate_denied",
                                        "recoverable": True,
                                        "reason": (
                                            "authorization audit unavailable; "
                                            "refusing to execute an unlogged elevation"
                                        ),
                                    }, ensure_ascii=False),
                                    error=True,
                                )
                        else:
                            return ToolCallResult(
                                call_id=call_id,
                                name=tool_name,
                                arguments=tool_args,
                                result=_act_gate.tool_error(_gate_verdict),
                                error=True,
                            )

                result = await registry.dispatch(
                    tool_name,
                    tool_args,
                    **dispatch_kwargs,
                )

                try:
                    result_parsed = json.loads(result)
                    if isinstance(result_parsed, dict) and result_parsed.get("_permission_needed"):
                        remote_permission = bool(getattr(result, "_remote_execution", False))
                        from app.services.remote_dispatch_router import (
                            _remote_allowed_tools,
                            resolve_execution_target,
                            strip_internal_args,
                        )

                        target = resolve_execution_target(dispatch_kwargs) or ""
                        remote_retry = (
                            target.startswith("device:")
                            and tool_name in _remote_allowed_tools()
                        )
                        conversation_id = getattr(conversation, "id", "") if conversation else ""
                        if remote_permission:
                            # Consent and signing both use the submitted args, never
                            # device-provided benign-looking display fields.
                            command = json.dumps(
                                strip_internal_args(tool_args), ensure_ascii=False, default=str,
                            )
                            target_path = (
                                tool_args.get("path") or tool_args.get("target_path") or ""
                            )
                            action = tool_name
                            # Device metadata is display-only and cannot choose
                            # the authorization policy. Derive from cloud args.
                            perm_key = permission_key_for_tool_request(tool_name, tool_args)
                            description = (
                                permission_description(perm_key)
                                if perm_key else "请求在设备上执行风险操作"
                            )
                        else:
                            target_path = result_parsed.get("_target_path", "")
                            command = result_parsed.get("_command", "")
                            action = result_parsed.get("_action", "")
                            perm_key = (
                                result_parsed.get("_permission_key")
                                or permission_key_for_tool_request(tool_name, result_parsed)
                            )
                            description = (
                                result_parsed.get("_permission_description")
                                or (permission_description(perm_key) if perm_key else "请求执行风险操作")
                            )
                        details = {
                            "command": command,
                            "target_path": target_path,
                            "action": action,
                            "permission_key": perm_key,
                            "user_id": _uid,
                        }

                        auto_allowed = False
                        if config.super_admin_bypass:
                            auto_allowed = True
                        elif (
                            self.deathmatch_manager is not None
                            and self.deathmatch_manager.is_goal_active
                        ):
                            auto_allowed = True
                        elif perm_key and user is not None:
                            auto_allowed = is_permission_allowed(user, perm_key)

                        async def dispatch_approved_retry():
                            nonlocal result
                            retry_kwargs = dispatch_kwargs
                            if remote_retry:
                                if tool_name == "skill_run_script":
                                    return ToolCallResult(
                                        call_id=call_id,
                                        name=tool_name,
                                        arguments=tool_args,
                                        result=json.dumps({
                                            "error": "设备授权通道暂不支持技能脚本调用（签名未覆盖脚本内容）",
                                        }, ensure_ascii=False),
                                        error=True,
                                    )
                                try:
                                    from app.services.device_grant_service import issue_device_grant
                                    from app.services.device_session_service import device_session_manager

                                    device_id = target.split(":", 1)[1]
                                    pairing_secret = device_session_manager.pairing_secret_for(device_id)
                                    if not pairing_secret:
                                        raise RuntimeError("live device grant session unavailable")
                                    grant = issue_device_grant(
                                        pairing_secret,
                                        device_id,
                                        tool_name,
                                        strip_internal_args(tool_args),
                                    )
                                    retry_kwargs = {
                                        **dispatch_kwargs,
                                        "_device_grant": grant,
                                    }
                                except Exception:
                                    logger.warning(
                                        "device grant issuance failed; refusing remote retry",
                                        exc_info=True,
                                    )
                                    return ToolCallResult(
                                        call_id=call_id,
                                        name=tool_name,
                                        arguments=tool_args,
                                        result=json.dumps({
                                            "error": "设备授权通道不可用：无法签发本次设备授权",
                                        }, ensure_ascii=False),
                                        error=True,
                                    )
                            tool_args["_permission_granted"] = True
                            result = await registry.dispatch(
                                tool_name,
                                tool_args,
                                **retry_kwargs,
                            )
                            return None

                        if auto_allowed and not remote_permission:
                            failure = await dispatch_approved_retry()
                            if failure is not None:
                                return failure
                        elif self._permission_callback:
                            approved = await self._permission_callback(
                                conversation_id=conversation_id,
                                tool_name=tool_name,
                                description=description,
                                details=details,
                            )
                            if approved:
                                failure = await dispatch_approved_retry()
                                if failure is not None:
                                    return failure
                            else:
                                return ToolCallResult(
                                    call_id=call_id,
                                    name=tool_name,
                                    arguments=tool_args,
                                    result=json.dumps({"error": "用户拒绝了执行请求"}, ensure_ascii=False),
                                    error=True,
                                )
                        else:
                            error_message = (
                                "设备授权通道不可用：客户端未提供用户批准通道"
                                if remote_permission
                                else f"Permission denied: {description}"
                            )
                            return ToolCallResult(
                                call_id=call_id,
                                name=tool_name,
                                arguments=tool_args,
                                result=json.dumps({"error": error_message}, ensure_ascii=False),
                                error=True,
                            )
                except (json.JSONDecodeError, TypeError):
                    pass

                # P1a TMA-NM M2: the tool result inherits max(input taint,
                # tool default). memory reads derive taint from entries_meta.
                if _ledger is not None:
                    try:
                        _out_taint = _taint.classify_tool_result(
                            tool_name, result, input_taint=_ledger.max_taint,
                            tool_args=tool_args,
                        )
                        _ledger.mark(
                            f"tool:{call_id}", _out_taint,
                            origin=(
                                "untrusted_external"
                                if _out_taint == _taint.TAINT_UNTRUSTED else "agent"
                            ),
                            principal=f"tool:{tool_name}",
                            label=tool_name[:80],
                        )
                        await _taint.persist_ledger(db, _ledger)
                    except Exception:
                        logger.debug("taint record failed (non-blocking)", exc_info=True)

            _latency_ms = int((_time.monotonic() - _t_start) * 1000)
            from app.services.tool_payload_status import payload_is_timeout as _is_timeout
            _payload_error = _is_timeout(result, tool_name)
            logger.info("tool_trace: name=%s call_id=%s latency_ms=%d error=%d",
                        tool_name, call_id, _latency_ms, 1 if _payload_error else 0)
            # Record successful memory reads (dedup) / invalidate on writes;
            # a returned delegate_task clears the whole cache (its child may
            # have written to any memory target — A4.9 review finding).
            if config.agent_tool_loop_memory_read_dedup:
                if tool_name == "memory":
                    _track_memory_read(state, tool_args, result)
                elif tool_name == "delegate_task":
                    _clear_memory_read_cache(state)
            # Per-turn redundancy records for the answer/tool conflict
            # arbitration (conv a8290d37): only the success path reaches
            # here — error payloads are filtered inside the helper.
            _record_tool_success(state, tool_name, tool_args, result)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=result,
                error=False,
                payload_error=_payload_error,
            )
        except Exception as e:
            _latency_ms = int((_time.monotonic() - _t_start) * 1000)
            logger.exception("Error executing tool '%s' (latency_ms=%d)", tool_name, _latency_ms)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=json.dumps({"error": f"Tool execution failed: {str(e)}"}),
                error=True,
            )
