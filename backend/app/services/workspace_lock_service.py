# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""workspace_lock_service — P1（2026-09-22 durable execution 波）。

两件事：
1. `path_lock(user_id, path)`：per-(user, path) asyncio.Lock——跨任务/跨并发写
   同一文件互斥（stale-edit guard 与写前影子 git 快照在其下层兜底）。
2. `partition_write_waves(tool_calls)`：同轮工具调用分波——同波内目标路径两两
   不同（波内并行），同路径调用挤到后续波（波间串行、模型顺序保序）。这是
   「write 全串行」到「异文件并行、同文件有序」的语义升级；无 path 参数的调用
   视为无冲突（grep/只读类）。

锁表有界（LRU 1024）：只淘汰未锁定条目，持锁条目永不淘汰（否则互斥失效）。
"""
import asyncio
import json
import os
from typing import Any, Dict, List, Optional

_WRITE_TOOLS_WITH_PATH = frozenset({"workspace_write", "workspace_edit"})

_MAX_LOCKS = 1024
_locks: Dict[tuple, "asyncio.Lock"] = {}


def _norm_path(path: str) -> str:
    return os.path.normpath(str(path).strip())


def canonical_key_path(workspace_path: Any, raw: str) -> str:
    """A4.9 R2 I8：把 path/file_path 统一为绝对规范键——相对路径 join 工作区，
    realpath 消解 ../ 与符号链接（目标不存在时也安全）。"""
    if not raw:
        return ""
    p = str(raw)
    if not os.path.isabs(p):
        p = os.path.join(str(workspace_path or ""), p)
    try:
        return os.path.realpath(p) if p else ""
    except Exception:
        return os.path.normpath(p)


def path_lock(user_id: Any, path: str) -> "asyncio.Lock":
    """返回 (user_id, 规范化 path) 维度的互斥锁；同键同锁。"""
    key = (str(user_id or ""), _norm_path(path))
    lock = _locks.get(key)
    if lock is None:
        if len(_locks) >= _MAX_LOCKS:
            for k in list(_locks):
                if len(_locks) < _MAX_LOCKS:
                    break
                if not _locks[k].locked():
                    del _locks[k]
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _tc_path(tc: Dict[str, Any], workspace_path: Any = None) -> Optional[str]:
    """工具调用的目标路径（写工具）；无 path 或解析失败 = 无冲突 → None。"""
    fn = tc.get("function") or {}
    if fn.get("name") not in _WRITE_TOOLS_WITH_PATH:
        return None
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return None
    if not isinstance(args, dict):
        return None
    p = args.get("path") or args.get("file_path")  # A4.9 R1 I8：别名同键
    if not isinstance(p, str) or not p:
        return None
    return canonical_key_path(workspace_path, p) if workspace_path else _norm_path(p)


def partition_write_waves(tool_calls: List[Dict[str, Any]],
                          workspace_path: Any = None) -> List[List[Dict[str, Any]]]:
    """贪心最早波放置：同波内路径两两不同；同路径按模型顺序先后入波。

    A4.9 R2 I8：带 workspace_path 时路径规范化为绝对键（相对/绝对/符号链接同键）。
    """
    if not tool_calls:
        return []
    waves: List[List[Dict[str, Any]]] = []
    wave_paths: List[set] = []
    for tc in tool_calls:
        p = _tc_path(tc, workspace_path)
        target = 0
        if p is not None:
            target = None
            for i, paths in enumerate(wave_paths):
                if p not in paths:
                    target = i
                    break
            if target is None:
                target = len(waves)
        while len(waves) <= target:
            waves.append([])
            wave_paths.append(set())
        waves[target].append(tc)
        if p is not None:
            wave_paths[target].add(p)
    return waves
