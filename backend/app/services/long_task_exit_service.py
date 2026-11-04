# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W4-4 长任务出口（记忆/长程 v2，2026-10-23，判据 #21）。

检测「多步 + 交付物 + 持续 ≥N 轮」的长任务 → 产出**升级死磕模式的建议文案**。

默认 shadow：本服务只产出建议文本（detect_long_task + format_upgrade_suggestion），
**不自动升级**——升级必须用户确认（计划 W4-4 验收面）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

#: 交付物信号词（命中其一即认为有交付物面）
DELIVERABLE_SIGNALS = ("报告", "文档", "表格", "部署", "脚本", "清单", "方案")


def _msg_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: List[str] = []
        for p in c:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
        return "\n".join(parts)
    return ""


def _tool_names(msg: dict) -> Set[str]:
    names: Set[str] = set()
    tc = msg.get("tool_calls")
    if isinstance(tc, list):
        for item in tc:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict):
                n = item.get("name")
                fn = item.get("function")
                if not n and isinstance(fn, dict):
                    n = fn.get("name")
                if n:
                    names.add(str(n))
    if str(msg.get("role") or "") == "tool":
        for key in ("tool_name", "name"):
            if msg.get(key):
                names.add(str(msg[key]))
                break
    return names


def detect_long_task(messages: Optional[List[dict]], *,
                     min_turns: int = 8, min_steps: int = 3) -> Optional[dict]:
    """检测多步长任务（纯函数）。

    命中条件：有效轮数 ≥ min_turns **且**（工具调用种类数 ≥ min_steps
    **或** 文本含交付物信号词：报告/文档/表格/部署/脚本/清单/方案）。

    有效轮数 = role ∈ {user, assistant} 且带实质文本内容的消息数
    （纯 tool_call 载体消息不算轮）。

    命中返回 {"suggested": True, "reason", "turn_count", "signals": [...]}，
    否则 None（shadow：调用方只记录/建议，不自动升级）。
    """
    if not isinstance(messages, list) or not messages:
        return None
    turns = 0
    tools: Set[str] = set()
    texts: List[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        text = _msg_text(m)
        if role in ("user", "assistant") and text.strip():
            turns += 1
        tools |= _tool_names(m)
        texts.append(text)
    if turns < int(min_turns):
        return None

    joined = "\n".join(texts)
    hits = [w for w in DELIVERABLE_SIGNALS if w in joined]
    tool_ok = len(tools) >= int(min_steps)
    if not tool_ok and not hits:
        return None

    signals = [f"turns={turns}"]
    reason = f"多步长任务：轮次 {turns}≥{int(min_turns)}"
    if tool_ok:
        signals.append(f"tool_kinds={len(tools)}")
        reason += f"、工具种类 {len(tools)}≥{int(min_steps)}"
    if hits:
        signals.append("deliverable=" + "/".join(hits))
        reason += f"、交付物信号：{'/'.join(hits)}"
    return {"suggested": True, "reason": reason, "turn_count": turns,
            "signals": signals}


def format_upgrade_suggestion(result: Dict[str, Any]) -> str:
    """建议文案（shadow：只建议，升级需用户确认）。"""
    reason = str((result or {}).get("reason") or "检测到多步长任务")
    return (
        f"{reason}。建议升级为死磕模式（需你确认）——回复确认后才会升级；"
        "当前为 shadow 模式，仅记录建议，不会自动升级。"
    )
