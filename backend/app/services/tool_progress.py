# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tool-call liveness/progress channel (2026-09-03, conv 827a6f78 wave).

A hung tool call previously sat silent for the full hard cap (1800s in both
deployments — two vision_interpret calls burned 30 minutes each). This module
gives tools a way to report LIVENESS and gives the agent loop a watchdog that
distinguishes the two failure modes:

- real long task   → keeps reporting progress (streaming chunks / subprocess
  output) → the stall watchdog never fires; only the hard wall-clock cap
  bounds it
- stuck call       → no progress for ``stall_timeout`` seconds → killed early
  with :class:`ToolStalledError`

Design (ADR D-2): stall detection is OPT-IN per tool via
``[agent.tool_loop] tool_call_stall_timeout_overrides`` — uninstrumented
tools have no honest liveness signal, so a global stall threshold would
false-kill them. The channel itself (contextvar) is safe to call from
anywhere: ``report_tool_progress`` is a no-op when no tracker is installed
(subagents, deathmatch, scheduler).
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Awaitable, Callable, Dict, Optional, Tuple

__all__ = [
    "ToolStalledError",
    "ToolProgressTracker",
    "report_tool_progress",
    "run_with_liveness",
    "resolve_tool_deadlines",
]


class ToolStalledError(RuntimeError):
    """Raised when a guarded tool call made no progress for the stall window."""


class ToolProgressTracker:
    """Monotonic liveness clock: ``report()`` refreshes it, the watchdog reads it."""

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last = time.monotonic()

    def report(self, note: str = "") -> None:
        self._last = time.monotonic()

    def seconds_since_progress(self) -> float:
        return time.monotonic() - self._last


_progress_reporter: contextvars.ContextVar[Optional[Callable[[str], None]]] = (
    contextvars.ContextVar("tool_progress_reporter", default=None)
)


def report_tool_progress(note: str = "") -> None:
    """Report tool liveness. No-op when no guarded call is watching (safe
    from any code path — the tracker is installed per-tool-call via contextvar)."""
    cb = _progress_reporter.get()
    if cb is not None:
        try:
            cb(note)
        except Exception:
            pass


async def run_with_liveness(
    coro: Awaitable,
    *,
    stall_timeout: float,
    check_interval: float = 10.0,
) -> object:
    """Await ``coro`` under a progress-aware watchdog.

    - ``stall_timeout <= 0`` → plain await, watchdog disabled (D-2 default).
    - Otherwise poll every ``check_interval``: kill + raise
      :class:`ToolStalledError` when the tracker went silent for
      ``stall_timeout`` seconds. Progress reports (contextvar installed here)
      keep pushing the deadline out — "只要还有实时输出就重置超时".
    - Cancellation of the OUTER task still propagates to ``coro``.
    """
    if stall_timeout <= 0:
        return await coro

    tracker = ToolProgressTracker()
    token = _progress_reporter.set(tracker.report)
    task: Optional[asyncio.Task] = None
    try:
        task = asyncio.ensure_future(coro)
        while True:
            done, _ = await asyncio.wait({task}, timeout=max(0.01, check_interval))
            if done:
                return task.result()
            idle = tracker.seconds_since_progress()
            if idle >= stall_timeout:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                raise ToolStalledError(
                    f"Tool stalled: no progress for {idle:.0f}s (stall_timeout={stall_timeout:.0f}s)。"
                    f"若任务确需长时间计算，请重试并在执行中周期性 print 进度（输出即存活信号）。"
                )
    except asyncio.CancelledError:
        if task is not None and not task.done():
            task.cancel()
        raise
    finally:
        _progress_reporter.reset(token)


def resolve_tool_deadlines(
    tool_name: str,
    *,
    hard_default: float,
    hard_overrides: Dict[str, float],
    stall_overrides: Dict[str, float],
) -> Tuple[float, float]:
    """Return ``(hard_cap, stall_timeout)`` for a tool. Unknown tools get the
    global default with the watchdog disabled (stall=0)."""
    hard = float(hard_overrides.get(tool_name, hard_default))
    stall = float(stall_overrides.get(tool_name, 0.0))
    return hard, stall
