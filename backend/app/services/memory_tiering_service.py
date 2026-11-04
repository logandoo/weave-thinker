# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W6-1 模型分档（记忆/长程 v2，2026-10-23，判据 #26，D4=a 用户决策）。

按 endpoint 的 context_window 自动选择记忆策略：
- **hybrid**：窗口 ≥ 131072（128k）——设计型记忆 + 原始历史并用（现状形态，
  记忆注入保持 dynamic 尾部，缓存不破）；
- **designed**：小窗口——设计型记忆（预算受限时的浓缩面）；
- **raw**：显式 tier=raw（MemoryTax 2610.29823 的强模型方向：原始历史+检索
  优于设计型记忆）——跳过设计型注入，只留历史/原始检索。

分档只决定「是否注入设计型记忆」，不改历史窗口与检索管线。A/B 验证前
默认路径 = hybrid（≥128k）/ designed（其余），与现状逐字节一致（P3）。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

HYBRID_MIN_CONTEXT_WINDOW = 131072
_VALID_MODES = ("designed", "raw", "hybrid")


def select_memory_mode(context_window: Optional[int], tier: str = "",
                       *, hybrid_min: int = HYBRID_MIN_CONTEXT_WINDOW) -> str:
    """确定性分档（纯函数）。显式 tier 优先；否则按窗口自动。"""
    t = str(tier or "").strip().lower()
    if t in _VALID_MODES:
        return t
    try:
        window = int(context_window or 0)
    except (TypeError, ValueError):
        window = 0
    if window >= int(hybrid_min):
        return "hybrid"
    return "designed"


def injection_enabled(mode: str) -> bool:
    """raw = 不注入设计型记忆（原始历史/检索承担）；其余注入。"""
    return str(mode or "").strip().lower() != "raw"
