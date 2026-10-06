# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent 事件总线 — 进程内观察通道（P2 / A4）。

供 vibeweaver 观察器（loop-guard / audit）与后续 UI/遥测订阅。事件即事实
流，观察者不得反向影响执行（订阅回调异常 fail-open，只记日志）。

事件类型（约定）：
- ``message.delta``    {session_id, delta, message_id?}      助手文本增量
- ``message.updated``  {session_id, content}                 助手整段文本
- ``tool.started``     {session_id, tool_name, args}
- ``tool.completed``   {session_id, tool_name, result, is_error}
- ``skill.activated``  {session_id, name}
- ``turn.ended``       {session_id}

MULTI-WORKER NOTE: 进程内广播，与 permission_manager 同款限制——多 worker
部署只覆盖单进程观察；跨进程一致性由落盘状态（.vibeweaver/）兜底。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    type: str
    session_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


EventListener = Callable[[AgentEvent], Optional[Awaitable[None]]]


class AgentEventBus:
    def __init__(self) -> None:
        self._listeners: List[Tuple[str, EventListener]] = []
        self._lock = asyncio.Lock()

    def subscribe(self, fn: EventListener, name: str = "") -> Callable[[], None]:
        entry = (name or getattr(fn, "__name__", "listener"), fn)
        self._listeners.append(entry)

        def _unregister() -> None:
            try:
                self._listeners.remove(entry)
            except ValueError:
                pass

        return _unregister

    def clear(self) -> None:
        self._listeners.clear()

    async def publish(self, event: AgentEvent) -> None:
        for name, fn in list(self._listeners):
            try:
                result = fn(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("event listener '%s' failed-open: %s", name, exc)

    def publish_soon(self, event: AgentEvent) -> None:
        """fire-and-forget 发布（同步上下文；无运行中循环时丢弃并告警）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("event dropped (no running loop): %s", event.type)
            return
        loop.create_task(self.publish(event))


agent_event_bus = AgentEventBus()
