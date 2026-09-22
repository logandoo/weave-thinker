# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""task_checkpoint — 断点续跑快照（恢复即重建下一次上下文视图）。

格式：checkpoint JSON {version,cursor,messages,budget_used,elapsed,updated_at,
dropped_messages?}；CHECKPOINT_VERSION 来自 durable_types（回滚遇新版本 loud
error）；恢复=从 checkpoint 全量回放 messages（不做有损摘要——恢复即重建下一次
上下文视图，State-Aware Runtime 2026-07 教训）。
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Tuple

from app.services.durable_types import CHECKPOINT_VERSION

_KEEP_MSG_FIELDS = ("role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content")


def _slim_message(m: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k in _KEEP_MSG_FIELDS:
        if k in m and m[k] is not None:
            out[k] = m[k]
    out.setdefault("role", "user")
    out.setdefault("content", "")
    return out


def build_checkpoint(*, cursor: int, messages: List[Dict[str, Any]], budget_used: int,
                     elapsed: float = 0.0, max_bytes: int = 2_000_000) -> Dict[str, Any]:
    """构建快照；超 max_bytes（**字节**，UTF-8 精确）时从最旧整条丢弃
    并显式披露计数（信息完整性原则）；单次线性扫描，无 O(n²) 重序列化。"""
    slim = [_slim_message(m) for m in messages]
    sizes = [len(json.dumps(m, ensure_ascii=False).encode("utf-8")) for m in slim]
    dropped = 0
    total = sum(sizes) + 2 * len(sizes) + 512  # 数组分隔符 + 字段余量
    while len(slim) > 1 and total > max_bytes:
        total -= sizes.pop(0)
        slim.pop(0)
        dropped += 1
    cp = {
        "version": CHECKPOINT_VERSION,
        "cursor": int(cursor),
        "messages": slim,
        "budget_used": int(budget_used),
        "elapsed": float(elapsed),
        "updated_at": datetime.utcnow().isoformat(),
    }
    if dropped:
        cp["dropped_messages"] = dropped
    return cp


def parse_checkpoint(raw: Any) -> Dict[str, Any]:
    """解析快照；版本高于当前 = 回滚场景 → ValueError（loud error，绝不猜字段）。"""
    if not raw:
        raise ValueError("empty checkpoint")
    cp = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
    version = int(cp.get("version") or 0)
    if version > CHECKPOINT_VERSION:
        raise ValueError(
            f"checkpoint version {version} is newer than supported {CHECKPOINT_VERSION} "
            f"(rollback? refuse to guess fields)")
    for field in ("cursor", "messages", "budget_used"):
        if field not in cp:
            raise ValueError(f"checkpoint missing field: {field}")
    if not isinstance(cp["messages"], list):
        raise ValueError("checkpoint messages must be a list")
    return cp


def plan_resume(cp: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """恢复计划：(messages 全量回放, budget_used 续算)。"""
    return list(cp["messages"]), int(cp.get("budget_used") or 0)


def recovery_status(has_checkpoint: bool) -> str:
    """启动恢复判定：有快照=resumable 续跑；无=failed（维持旧行为）。"""
    return "resumable" if has_checkpoint else "failed"


def drain_status_values(has_checkpoint: bool) -> Dict[str, Any]:
    """优雅停机（drain）落点：有快照=resumable（completed_at 空）；无=failed。"""
    if has_checkpoint:
        return {"status": "resumable", "completed_at": None}
    return {"status": "failed", "completed_at": datetime.utcnow()}
