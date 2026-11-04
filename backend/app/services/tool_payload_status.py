# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""工具载荷级失败判定（2026-11-01 审计 F4）。

背景：工具自报的超时（`{"timed_out": true, ...}`）走的是"工具调用成功返回"
这条路径，于是 `ToolCallResult.error=False`，导致三处口径与事实相反：
UI 卡片 ✓ 完成、`tool_trace … error=0`、副作用台账记 done。

本模块只做一件纯函数的事：判断工具返回的 JSON 载荷是否自报超时。
副作用台账的语义**不跟**这个标志走（超时命令可能已产生副作用，记 done 是
"宁可不双写"的保守选择），它只用于展示/日志口径与前端 error 态。
"""
from __future__ import annotations

import json

__all__ = ["payload_is_timeout", "timeout_envelope", "timeout_capable_tool"]

# 只有这两个工具会产出 timed_out 载荷（F4 复评 M4：不按工具限定的载荷嗅探会把
# 用户读到的普通 JSON 文件误判成失败）。
TIMEOUT_TOOLS = frozenset({"terminal", "execute_code", "code_execution"})


def timeout_capable_tool(tool_name: str) -> bool:
    return str(tool_name or "") in TIMEOUT_TOOLS

_MARKER = '"timed_out"'


def payload_is_timeout(result_text: str, tool_name: str = "") -> bool:
    """True 当且仅当载荷是一个命中 `timed_out: true` 的 JSON 对象。

    先做廉价的子串预筛（每个工具调用都会走这里），命中后再解析校验，
    避免误伤正文里出现该字样的普通结果。``tool_name`` 非空时再做工具白名单
    校验（评审复评 M4：`workspace_read` 读到同形状 JSON 不得被判失败）。
    """
    if tool_name and not timeout_capable_tool(tool_name):
        return False
    if not result_text or not isinstance(result_text, str):
        return False
    if _MARKER not in result_text:
        return False
    text = result_text.strip()
    if not text.startswith("{"):
        return False
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return False
    # 形状收窄到真实超时回执：timed_out=true 之外还要求 return_code=-1
    # （两个工具的超时分支都写 -1）。避免把用户读到的普通 JSON 误判成失败。
    return (
        isinstance(obj, dict)
        and obj.get("timed_out") is True
        and obj.get("return_code") == -1
    )


_KEEP_KEYS = ("error", "return_code", "killed_by", "timeout_effective", "guidance")


def timeout_envelope(result_text: str) -> str:
    """超时回执的关键字段摘要（大结果被预算折叠成桩时挂到桩尾用）。

    `maybe_persist_tool_result` 只保留内容头部做 preview，而 `timed_out` /
    `guidance` 通常在 JSON 尾部 —— 不补这一段，模型与 UI 都会以为工具"成功
    返回了一大坨文本"（评审 I1）。非超时载荷返回空串。
    """
    if not payload_is_timeout(result_text):
        return ""
    try:
        obj = json.loads(result_text)
    except (ValueError, TypeError):
        return ""
    kept = {"timed_out": True}
    for k in _KEEP_KEYS:
        if k in obj:
            kept[k] = obj[k]
    return ("\n\n<timeout-envelope>\n"
            + json.dumps(kept, ensure_ascii=False)
            + "\n</timeout-envelope>")
