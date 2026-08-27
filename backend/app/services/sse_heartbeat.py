# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""SSE setup-heartbeat helper.

The chat stream's setup phase (coordinator pre-pass + memory retrieval +
workspace + skills gather in ``_run_tool_loop``) performs slow LLM/DB work
BEFORE the stream buffer, agent registry entry, and agent keepalive exist.
Without heartbeats the client sees total silence; the frontend's 30s stall
watchdog then aborts a perfectly healthy setup and the whole run dies before
the detached agent task is even created (permanent "正在思考" then nothing).

``aiter_with_heartbeat`` wraps one awaitable: it yields ``HEARTBEAT``
sentinels while the awaitable is still running and the final result once it
completes. Closing the generator cancels the underlying future so a client
disconnect aborts the setup exactly like a direct ``await`` would.
"""

import asyncio
from typing import Any, AsyncIterator, Awaitable

HEARTBEAT: Any = object()


async def aiter_with_heartbeat(awaitable: Awaitable[Any], interval: float = 10.0) -> AsyncIterator[Any]:
    fut = asyncio.ensure_future(awaitable)
    try:
        while True:
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), timeout=interval)
            except asyncio.TimeoutError:
                yield HEARTBEAT
                continue
            yield result
            return
    finally:
        if not fut.done():
            fut.cancel()
            try:
                await fut
            except (asyncio.CancelledError, Exception):
                pass
