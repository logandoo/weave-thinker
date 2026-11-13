# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)


def tool_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _outcome_ok(result: Any) -> bool:
    """Tool handlers signal failure with {"error": ...} without raising."""
    if result is None:
        return False
    if isinstance(result, dict):
        return not result.get("error")
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return not parsed.get("error")
        except (json.JSONDecodeError, TypeError):
            pass
    return True


def _record_reliability(name: str, result: Any, started: float, *,
                        error_class: str = "") -> None:
    try:
        from app.services.tool_reliability_service import record
        record(
            name,
            _outcome_ok(result) and not error_class,
            error_class=error_class,
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except Exception:
        logger.debug("tool reliability record failed", exc_info=True)


def _tool_timeout_error(tool_name: str, timeout_seconds: float) -> str:
    """Structured timeout error with actionable guidance.

    Returns a JSON result that tells the agent:
    1. WHAT happened (tool timed out at N seconds — not a code bug)
    2. WHY it likely happened (task too large for a synchronous tool call)
    3. WHAT to do (reduce scope / batch / checkpoint / check for infinite loop)

    This is general (works for any tool) and agentic (gives the agent
    information to self-correct, doesn't force a specific behavior).
    Without this, the agent sees "Tool 'X' timed out" and retries the
    same approach — causing a spin loop (conv 01d08b67: 20+ retries of
    the same 2.5h GAIA script against a 600s timeout).

    For tools with their own timeout handling (execute_code, terminal),
    this is a backstop — their internal timeout fires first and returns
    partial output. This handles tools WITHOUT internal timeout handling.
    """
    return json.dumps({
        "error": "tool_timeout",
        "tool": tool_name,
        "timeout_seconds": timeout_seconds,
        "guidance": (
            f"工具 {tool_name} 执行超过 {int(timeout_seconds)} 秒被终止。"
            "这通常意味着任务量过大或任务设计需要调整，不是代码 bug。建议："
            "(1) 减少单次任务范围（如只处理部分数据，而不是全部）；"
            "(2) 分批执行，每批在超时限制内完成，并将中间结果保存到文件；"
            "(3) 如果任务确实需要长时间运行，将其拆分为多个小步骤，每步单独调用工具；"
            "    对于确实需要长时间运行的任务，使用 background_task 工具提交到后台执行（5 小时超时）。"
            "(4) 检查是否存在死循环、无限等待或不必要的重复计算。"
            "请根据上述建议调整方案后重试，不要用相同的参数重复调用。"
        ),
    }, ensure_ascii=False)


@dataclass
class ToolEntry:
    name: str
    toolset: str
    schema: dict
    handler: Callable
    check_fn: Optional[Callable[[], bool]] = None
    requires_env: List[str] = field(default_factory=list)
    is_async: bool = False
    description: str = ""
    emoji: str = ""
    permission_key: Optional[str] = None
    # F3（2026-09-14）：产物声明——带该元数据的工具结果会被附件收集器收集
    # （不再按工具名硬编码；新工具自动被收集）
    produces_files: bool = False
    # 2026-11-01 用户级 MCP：None = 全局工具（所有用户可见）；否则仅属主可见/
    # 可 dispatch（fail-closed，用户上下文缺失时也不可见）。
    owner_user_id: Optional[str] = None


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._toolset_checks: Dict[str, Callable] = {}
        self._lock = asyncio.Lock()
        # P2-2: in-memory usage counter for tool-frequency analysis (drives
        # future catalog slimming decisions; exposed via /api/system/status).
        self._usage_counter: Counter = Counter()

    def get_tool_usage_stats(self) -> Dict[str, int]:
        """Return {tool_name: dispatch_count} for successfully-dispatched calls."""
        return dict(self._usage_counter)

    def tool_produces_files(self, name: str) -> bool:
        """F3：工具是否声明产物（附件收集器的元数据判据）。"""
        entry = self._tools.get(name)
        return bool(entry and entry.produces_files)

    def _snapshot_entries(self) -> List[ToolEntry]:
        return list(self._tools.values())

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        return self._tools.get(name)

    async def _a_get_entry(self, name: str) -> Optional[ToolEntry]:
        async with self._lock:
            return self._tools.get(name)

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable[[], bool]] = None,
        requires_env: Optional[List[str]] = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        permission_key: Optional[str] = None,
        produces_files: bool = False,
        owner_user_id: Optional[str] = None,
    ):
        existing = self._tools.get(name)
        if existing:
            logger.warning(
                "Tool '%s' already registered in toolset '%s', overwriting with '%s'",
                name, existing.toolset, toolset,
            )
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=requires_env or [],
            is_async=is_async,
            description=description,
            emoji=emoji,
            permission_key=permission_key,
            produces_files=produces_files,
            owner_user_id=str(owner_user_id) if owner_user_id else None,
        )
        logger.debug("Registered tool '%s' in toolset '%s'", name, toolset)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    @staticmethod
    def _visible_for(entry: ToolEntry, user_id: Optional[str]) -> bool:
        """Owner gate: global tools are public; user tools need the exact owner.

        ``user_id=None`` is the fail-closed context (internal/tests/no auth) —
        it sees global tools only, never another user's MCP tools.
        """
        owner = getattr(entry, "owner_user_id", None)
        if owner is None:
            return True
        return bool(user_id) and str(owner) == str(user_id)

    def get_tool_names_for_toolset(
        self, toolset: str, user_id: Optional[str] = None,
    ) -> List[str]:
        return sorted(
            entry.name for entry in self._snapshot_entries()
            if entry.toolset == toolset and self._visible_for(entry, user_id)
        )

    def get_toolset_for_tool(
        self, name: str, user_id: Optional[str] = None,
    ) -> Optional[str]:
        entry = self.get_entry(name)
        if entry is None or not self._visible_for(entry, user_id):
            return None
        return entry.toolset

    def get_all_tool_names(self, user_id: Optional[str] = None) -> List[str]:
        return sorted(
            entry.name for entry in self._snapshot_entries()
            if self._visible_for(entry, user_id)
        )

    def get_all_registered_names(self) -> List[str]:
        """Collision/management surface: intentionally ignores ownership."""
        return sorted(self._tools.keys())

    def get_definitions(
        self,
        tool_names: Optional[Set[str]] = None,
        quiet: bool = False,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        definitions = []
        entries = self._snapshot_entries()

        for entry in entries:
            if not self._visible_for(entry, user_id):
                continue
            if tool_names is not None and entry.name not in tool_names:
                continue
            if entry.check_fn:
                try:
                    if not entry.check_fn():
                        if not quiet:
                            logger.info("Tool '%s' skipped: check_fn returned False", entry.name)
                        continue
                except Exception:
                    logger.debug("Tool '%s' check_fn raised; marking unavailable", entry.name)
                    continue
            definitions.append({
                "type": "function",
                "function": entry.schema,
            })

        return definitions

    def get_schema(self, name: str) -> Optional[dict]:
        entry = self.get_entry(name)
        return entry.schema if entry else None

    async def dispatch(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch a tool call.

        P1 4.5: Supports ``permission_context`` in kwargs for pre-execution
        permission checks. When a tool has ``permission_key`` set and
        ``permission_context`` is provided without ``_permission_granted``
        in args, returns ``_permission_needed`` before execution instead
        of relying on each tool's self-check.
        """
        entry = await self._a_get_entry(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}")

        # 2026-11-01 user-owned MCP: fail-closed owner check BEFORE any routing/
        # permission seam. A hallucinated/guessed name from another user must
        # look exactly like an unknown tool (no existence leak).
        if not self._visible_for(entry, str(getattr(kwargs.get("user"), "id", "") or "") or None):
            return tool_error(f"Unknown tool: {name}")

        started = time.monotonic()

        # P2-2: fail-closed visibility guard. When the caller (AgentLoop)
        # restricts the visible tool set, a model that still emits a hidden
        # tool name (stale schema in history / hallucination) gets a clear
        # rejection instead of silent execution.
        allowed_tools = kwargs.pop("allowed_tools", None)
        if allowed_tools is not None and name not in allowed_tools:
            return tool_error(
                f"工具 {name} 当前不可用（不在本场景的可见工具集内）。"
                "请从本轮可见的工具列表中选择替代工具，或直接基于已有信息回答。"
            )

        permission_context = kwargs.pop("permission_context", None)

        if entry.permission_key and not args.get("_permission_granted") and permission_context is not None:
            super_admin = getattr(permission_context, "super_admin_bypass", False)
            deathmatch_active = getattr(permission_context, "deathmatch_active", False)
            user = getattr(permission_context, "user", None)
            auto_allowed = super_admin or deathmatch_active
            # 2026-11-12：调用点新鲜权限快照优先（进行中会话改设置立即生效）。
            # None = 旧语义（回落 user.agent_permissions 请求期快照）。
            # user is None 仍 fail-closed（快照不得让无用户上下文放行）。
            fresh_perms = getattr(permission_context, "permissions", None)
            if (
                not auto_allowed
                and fresh_perms is not None
                and user is not None
                and entry.permission_key
            ):
                from app.services.agent_permissions import DEFAULT_AGENT_PERMISSIONS

                auto_allowed = bool(
                    fresh_perms.get(
                        entry.permission_key,
                        DEFAULT_AGENT_PERMISSIONS.get(entry.permission_key, False),
                    )
                )
            elif not auto_allowed and user is not None and entry.permission_key:
                try:
                    from app.services.agent_permissions import is_permission_allowed
                    auto_allowed = is_permission_allowed(user, entry.permission_key)
                except Exception:
                    pass

            if not auto_allowed:
                callback = getattr(permission_context, "callback", None)
                target_path = str(args.get("target_path", args.get("path", "")))
                command = str(args.get("command", ""))
                return json.dumps({
                    "_permission_needed": True,
                    "_permission_key": entry.permission_key,
                    "_target_path": target_path,
                    "_command": command,
                    "_permission_description": f"工具 {name} 需要确认执行",
                }, ensure_ascii=False)

        # W16 设备执行模式路由缝（docs/PLAN_device_execution_mode.md §6.4）：
        # 唯一接入点=本 dispatch 外壳（覆盖 agent_loop 初发/权限重发、voice、worker、
        # PTC、scheduler、delegate 全部调用面）。execution_target=device 且工具在
        # 远端白名单 → 经 WS 在设备执行并还原结果；路由层永不静默落回本地执行。
        remote_result = None
        try:
            from app.services.remote_dispatch_router import try_remote_dispatch
            remote_result = await try_remote_dispatch(name, args, kwargs)
        except Exception:
            logger.exception("device-exec routing seam failed for tool '%s'", name)
            _target = None
            try:
                _target = kwargs.get("execution_target") or getattr(
                    kwargs.get("conversation"), "execution_target", None
                )
            except Exception:
                _target = None
            if isinstance(_target, str) and _target.startswith("device:"):
                remote_result = tool_error("设备执行路由失败（fail-closed，不回退云端执行）")
        if remote_result is not None:
            self._usage_counter[name] += 1
            _record_reliability(name, remote_result, started)
            return remote_result

        try:
            self._usage_counter[name] += 1
            if entry.is_async:
                handler = entry.handler
                if not asyncio.iscoroutinefunction(handler):
                    # Async callable instance (async __call__): iscoroutinefunction
                    # on the instance is False; inspect the bound __call__ instead.
                    call = getattr(handler, "__call__", None)
                    if call is not None and asyncio.iscoroutinefunction(call):
                        handler = call
                if asyncio.iscoroutinefunction(handler):
                    result = await asyncio.wait_for(
                        handler(args, **kwargs),
                        timeout=config.agent_tool_loop_tool_call_timeout,
                    )
                    _record_reliability(name, result, started)
                    return result
            result = entry.handler(args, **kwargs)
            _record_reliability(name, result, started)
            return result
        except asyncio.TimeoutError:
            logger.exception("Tool '%s' timed out", name)
            _record_reliability(name, None, started, error_class="timeout")
            return _tool_timeout_error(name, config.agent_tool_loop_tool_call_timeout)
        except Exception as e:
            logger.exception("Error executing tool '%s'", name)
            _record_reliability(name, None, started, error_class=type(e).__name__)
            return tool_error(f"Tool '{name}' failed: {str(e)}")

    def get_available_toolsets(self, user_id: Optional[str] = None) -> Dict[str, dict]:
        from collections import defaultdict
        toolsets: Dict[str, dict] = defaultdict(lambda: {"tools": [], "available": True})
        for entry in self._snapshot_entries():
            if not self._visible_for(entry, user_id):
                continue
            info = toolsets[entry.toolset]
            info["tools"].append(entry.name)
            if entry.check_fn:
                try:
                    if not entry.check_fn():
                        info["available"] = False
                except Exception:
                    info["available"] = False
        return dict(toolsets)

    def get_tool_to_toolset_map(self, user_id: Optional[str] = None) -> Dict[str, str]:
        return {
            entry.name: entry.toolset
            for entry in self._snapshot_entries()
            if self._visible_for(entry, user_id)
        }


registry = ToolRegistry()
