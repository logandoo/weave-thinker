# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""run 持久化面的进程内计数（P2 · 2026-11-09 · spec §4.8 第 2 项）。

事故里的形态是「有 user 行、没有任何 assistant 行」，而当时渐进落库的失败路径是
`logger.debug("progressive flush failed (fail-open)")` —— 生产日志级别是 INFO，
**这条 debug 根本不会出现**。"fail-open 只记日志"在没有日志级别保证的前提下
等于静默失败：渐进落库是"进程被杀后用户还能看到已完成轮次"的唯一保险，
它坏了没人知道。

本模块把这些失败变成**可计数、ERROR 级**的事件。它不改变 fail-open 语义
（落库失败仍然不该把用户的流式回答打死），改变的是"失败必须看得见"。

Usage::

    from app.services import persistence_metrics as pm
    pm.note_failure("progressive_flush", exc)

日志单标签：`grep 'PERSIST-ALERT' chatllm.log`
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict

logger = logging.getLogger(__name__)

ALERT_TAG = "PERSIST-ALERT"

_counters: Dict[str, int] = {}
_lock = threading.Lock()


def incr(name: str, by: int = 1) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + by


def get(name: str) -> int:
    with _lock:
        return int(_counters.get(name, 0))


def snapshot() -> Dict[str, int]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    """测试隔离用。"""
    with _lock:
        _counters.clear()


def note_failure(where: str, exc: BaseException | None = None, **ctx: Any) -> None:
    """一次持久化失败：计数 + ERROR（不是 debug/warning）。

    `where` 是稳定枚举（streaming_row_create / progressive_flush /
    progressive_serialize / progressive_schedule / progressive_revert /
    conversation_id_reminted），直接进计数名
    `<where>_failed_total`，便于运维按点位定位。
    """
    incr(f"{where}_failed_total")
    detail = " ".join(f"{k}={v}" for k, v in ctx.items() if v not in (None, ""))
    logger.error(
        "%s %s failed（fail-open：不影响本次流式回答，但用户重启后可能看不到已完成轮次）"
        " %s%s",
        ALERT_TAG, where, f"{detail} " if detail else "",
        f"error={type(exc).__name__}: {exc}" if exc is not None else "",
    )


def note_conversation_id_remint(client_id: str, server_id: str) -> None:
    """服务端不认识前端传来的 conversation id ⇒ 另铸新 id（spec §4.8 (c)）。

    这本身不是错误（跨设备删除/库重置都会遇到），但**必须可见**：前端如果没
    收到新 id，就会一直轮询一个服务端从不存在的会话，回答落在别的会话下而用户
    永远看不到（60 事故里 08:58 与 13:56 的 `GET /api/conversations/<id>` 全 404）。
    """
    incr("conversation_id_reminted_total")
    logger.warning(
        "%s conversation_id reminted client=%r server=%r —— 已随流首个 meta 事件回传新 id",
        ALERT_TAG, (client_id or "")[:16], (server_id or "")[:16],
    )
