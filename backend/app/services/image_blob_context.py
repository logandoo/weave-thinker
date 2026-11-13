# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""图片 blob 的运行上下文（2026-11-09 图片信封零损失内存改造 · spec §4.1/§4.3）。

两个 ContextVar，由 `AgentLoop.run()` 在每次 run 起点装配（chat / worker /
scheduler / delegate_task 四条构造路径都带 `user_id`，因此这里是唯一必经装配
点；voice 直连 `stream_chat_structured` 的历史重建把 tool 消息截到 1500 字符
且无 `name`，永不进入展开路径，见 spec §4.4 语音行）：

- `IMAGE_OWNER_UID8`：本轮消息列表归属用户的 8 位十六进制命名空间。blob 物化
  前必须校验 `ref.uid8 == IMAGE_OWNER_UID8`，否则拒绝（spec §4.1 授权面——
  `MessagePayload` 禁令第 ② 条：伪造 ref 越权读他人内容）。
- `IMAGE_SESSION_KEY`：告警去重键（默认取 conversation id）。软阈值告警要求
  「一次/会话」，没有会话键就只能一次/进程，会漏掉后续会话。

ContextVar 是任务作用域的：`asyncio.create_task` 继承创建时的上下文，因此
run 内派生的工具任务读到的是同一个 owner，不会串会话。
"""
from __future__ import annotations

import hashlib
from contextvars import ContextVar
from typing import Optional

IMAGE_OWNER_UID8: ContextVar[Optional[str]] = ContextVar("image_owner_uid8", default=None)
IMAGE_SESSION_KEY: ContextVar[Optional[str]] = ContextVar("image_session_key", default=None)


def uid8_for_user(user_id: object) -> str:
    """用户 id → 8 位小写十六进制命名空间（spec §4.1 布局与 ref 语法）。

    用 sha256 前 8 位而不是 `user_id[:8]`：仓内 user id 既有 `user-xxxx` 也有
    裸 uuid，前缀切片会把命名空间压成少数几种前缀，且 32 bit 空间下 51 用户的
    生日碰撞概率 ≈3e-7（碰撞只意味着两个用户共用一个 blob 目录，仍需猜中对方
    的 64 hex 才能读到内容；记录在 tests/decisions.md 备查）。
    """
    raw = str(user_id or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def set_run_context(user_id: object, session_key: object) -> tuple:
    """装配本轮上下文，返回 (owner_token, session_token) 供 finally 复位。"""
    owner = uid8_for_user(user_id)
    key = str(session_key or "").strip() or None
    return IMAGE_OWNER_UID8.set(owner or None), IMAGE_SESSION_KEY.set(key)


def clear_run_context(tokens: tuple) -> None:
    owner_token, session_token = tokens
    IMAGE_OWNER_UID8.reset(owner_token)
    IMAGE_SESSION_KEY.reset(session_token)


def current_owner_uid8() -> Optional[str]:
    return IMAGE_OWNER_UID8.get()


def current_session_key() -> Optional[str]:
    return IMAGE_SESSION_KEY.get() or "global"
