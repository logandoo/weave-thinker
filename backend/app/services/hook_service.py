# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent hook 运行时 — vibeweaver 执法层的宿主接口（P1 / A1-A3）。

三类钩子（对齐 OpenCode 插件面语义）：

- ``tool.before``：工具执行前；返回 block 消息 → 该次调用以红错误结果返回
  模型（等价 ``tool.execute.before`` throw）。
- ``tool.after``：工具执行后；block → 结果替换为红错误（GATE-BLOCKED），
  notes → 追加到结果文本尾部（模型可见，等价 result.content 注记）。
- ``stop``：turn 收敛（本轮无待发工具调用）时；返回 block+feedback →
  阻止收尾并把 feedback 注入指令续跑（等价 session.idle 门禁 + 续跑）。

设计约束：
- **fail-open**：任何钩子抛异常只记日志不影响工具执行（观察者不越权）。
- **防递归预算**：stop 拦截每轮 ≤ ``stop_hook_max_per_turn``（默认 2）、
  每会话 ≤ ``stop_hook_max_per_conversation``（默认 3）；超出即放行并留痕。
- 会话预算为进程内字典（多 worker 部署与 permission_manager 同款限制，
  RED latch 等跨进程状态由 vibeweaver_gate_service 落盘 .vibeweaver/ 兜底）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

ToolHookPhase = Literal["before", "after"]


@dataclass
class ToolHookContext:
    tool_name: str
    args: Dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    workspace_path: str = ""
    # after 阶段填充
    result: str = ""
    is_error: bool = False


@dataclass
class HookDecision:
    block: Optional[str] = None
    notes: List[str] = field(default_factory=list)


_ALLOW = HookDecision()


@dataclass
class StopContext:
    session_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    workspace_path: str = ""
    assistant_content: str = ""
    turn_tool_names: List[str] = field(default_factory=list)
    iterations: int = 0
    # 防递归：本次收尾已因 stop hook 续跑过（同 stop 序列只拦一次语义由
    # 预算与本标志共同承担）
    stop_hook_active: bool = False
    stop_hook_blocks_this_turn: int = 0


@dataclass
class StopDecision:
    allow: bool = True
    feedback: str = ""
    reason: str = ""


_ALLOW_STOP = StopDecision()

ToolHookFn = Callable[[ToolHookContext], Awaitable[Optional[HookDecision]]]
StopHookFn = Callable[[StopContext], Awaitable[Optional[StopDecision]]]

_tool_hooks: Dict[ToolHookPhase, List[Tuple[str, ToolHookFn]]] = {"before": [], "after": []}
_stop_hooks: List[Tuple[str, StopHookFn]] = []
_stop_blocks_by_conv: Dict[str, int] = {}
_stop_lock = asyncio.Lock()


def register_tool_hook(phase: ToolHookPhase, fn: ToolHookFn, name: str = "") -> Callable[[], None]:
    if phase not in _tool_hooks:
        raise ValueError(f"unknown tool hook phase: {phase}")
    entry = (name or getattr(fn, "__name__", "hook"), fn)
    _tool_hooks[phase].append(entry)

    def _unregister() -> None:
        try:
            _tool_hooks[phase].remove(entry)
        except ValueError:
            pass

    return _unregister


def register_stop_hook(fn: StopHookFn, name: str = "") -> Callable[[], None]:
    entry = (name or getattr(fn, "__name__", "stop_hook"), fn)
    _stop_hooks.append(entry)

    def _unregister() -> None:
        try:
            _stop_hooks.remove(entry)
        except ValueError:
            pass

    return _unregister


def clear_hooks() -> None:
    """测试隔离用。"""
    for phase in _tool_hooks:
        _tool_hooks[phase].clear()
    _stop_hooks.clear()
    _stop_blocks_by_conv.clear()


async def run_tool_hooks(phase: ToolHookPhase, ctx: ToolHookContext) -> HookDecision:
    """聚合执行某阶段全部钩子。首个 block 短路；notes 累计。fail-open。"""
    notes: List[str] = []
    for hook_name, fn in list(_tool_hooks.get(phase, [])):
        try:
            decision = await fn(ctx)
        except Exception as exc:
            logger.warning("tool hook '%s' (%s) failed-open: %s", hook_name, phase, exc)
            continue
        if decision is None:
            continue
        if decision.block:
            return HookDecision(block=decision.block, notes=notes + list(decision.notes))
        notes.extend(decision.notes)
    return HookDecision(notes=notes) if notes else _ALLOW


def _stop_budget_exceeded(conv_id: str, ctx: StopContext, max_per_turn: int, max_per_conv: int) -> Optional[str]:
    if ctx.stop_hook_blocks_this_turn >= max_per_turn:
        return f"stop-hook per-turn budget exhausted ({ctx.stop_hook_blocks_this_turn}/{max_per_turn})"
    used = _stop_blocks_by_conv.get(conv_id, 0)
    if used >= max_per_conv:
        return f"stop-hook per-conversation budget exhausted ({used}/{max_per_conv})"
    return None


async def run_stop_hooks(ctx: StopContext) -> StopDecision:
    """聚合执行 stop 钩子。预算内首个 block 生效并计数；fail-open 放行。"""
    from app.core.config import get_config

    try:
        max_per_turn = max(0, int(get_config().hooks_stop_max_per_turn))
        max_per_conv = max(0, int(get_config().hooks_stop_max_per_conversation))
    except Exception:
        max_per_turn, max_per_conv = 2, 3

    conv_id = ctx.conversation_id or f"conv-{ctx.session_id or 'unknown'}"

    if not _stop_hooks:
        return _ALLOW_STOP

    over = _stop_budget_exceeded(conv_id, ctx, max_per_turn, max_per_conv)
    if over:
        logger.info("stop hooks skipped: %s", over)
        return StopDecision(allow=True, reason=over)

    for hook_name, fn in list(_stop_hooks):
        try:
            decision = await fn(ctx)
        except Exception as exc:
            logger.warning("stop hook '%s' failed-open: %s", hook_name, exc)
            continue
        if decision is None or decision.allow:
            continue
        if not (decision.feedback or "").strip():
            # 无反馈文本的 block 无法续跑，视作 allow（防止空转）
            logger.warning("stop hook '%s' blocked without feedback — treated as allow", hook_name)
            continue
        async with _stop_lock:
            _stop_blocks_by_conv[conv_id] = _stop_blocks_by_conv.get(conv_id, 0) + 1
        logger.info(
            "stop hook '%s' blocks completion (conv=%s blocks=%d): %s",
            hook_name, conv_id, _stop_blocks_by_conv[conv_id], decision.reason or decision.feedback[:120],
        )
        return decision
    return _ALLOW_STOP


def stop_block_count(conversation_id: str) -> int:
    return _stop_blocks_by_conv.get(conversation_id or "", 0)
