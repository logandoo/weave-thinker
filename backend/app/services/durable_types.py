# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""durable_types — durable execution 波共享类型（Consistency Hub 唯一实现）。

来源：docs/PLAN_durable_execution_wave.md Consistency Hub。
- CHECKPOINT_VERSION：agent_tasks.checkpoint JSON 的 schema 版本（回滚遇新版本 loud error）
- can_transition：durable_jobs 状态机合法迁移白名单（终态不可逆；unknown 仅 reconcile 可出、
  只允许结算为 failed）
- make_key：副作用台账幂等键 `{principal_type}:{principal_id}:{cursor}:{tool_name}:{call_id}`
  （keyed on cursor 不是 seq——重放同一步得到同 key，dev.to 2026-08 生产教训）
- clamp_concurrency：P2 与 durable jobs 并发钳制 [1,8]
"""
from typing import Any

CHECKPOINT_VERSION = 1

JOB_STATE_QUEUED = "queued"
JOB_STATE_LEASED = "leased"
JOB_STATE_RUNNING = "running"
JOB_STATE_SUCCEEDED = "succeeded"
JOB_STATE_FAILED = "failed"
JOB_STATE_CANCELLED = "cancelled"
JOB_STATE_UNKNOWN = "unknown"

JOB_TERMINAL_STATES = frozenset({JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED})

_ALLOWED_TRANSITIONS = {
    JOB_STATE_QUEUED: {JOB_STATE_LEASED, JOB_STATE_CANCELLED, JOB_STATE_FAILED},
    JOB_STATE_LEASED: {JOB_STATE_RUNNING, JOB_STATE_CANCELLED, JOB_STATE_FAILED, JOB_STATE_UNKNOWN},
    JOB_STATE_RUNNING: {JOB_STATE_SUCCEEDED, JOB_STATE_FAILED, JOB_STATE_CANCELLED, JOB_STATE_UNKNOWN},
    # unknown 只能结算为 failed（人工/策略裁定后）；不可复活、不可直接判成功
    JOB_STATE_UNKNOWN: {JOB_STATE_FAILED},
}


def can_transition(from_state: Any, to_state: Any) -> bool:
    """状态机合法迁移判定（fail-closed：未列出=非法）。"""
    if not isinstance(from_state, str) or not isinstance(to_state, str):
        return False
    return to_state in _ALLOWED_TRANSITIONS.get(from_state, set())


def make_key(principal_type: str, principal_id: str, cursor: int, tool_name: str,
             call_id: str) -> str:
    """副作用台账幂等键（Hub 格式 v2，A4.9 R1 C3 修正：含 call_id——

    仅 (cursor, tool_name) 会让同轮两次同类调用（P1 异文件并行）互相碰撞，
    第二次被伪造成「已执行」而实际未写。call_id 是重放身份（checkpoint 的
    tool_calls 保留原 id），cursor 在 checkpoint 先于 consume 的语义下重放稳定。
    """
    return f"{principal_type}:{principal_id}:{cursor}:{tool_name}:{call_id}"


def clamp_concurrency(value: Any, lo: int = 1, hi: int = 8) -> int:
    """并发钳制 [lo,hi]（默认 [1,8]）；非法输入取 lo。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))
