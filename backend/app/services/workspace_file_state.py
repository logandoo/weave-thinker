# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""workspace 读后写追踪（P0 workspace 原语配套）。

记录每次成功 ``workspace_read`` 的 (mtime_ns, size)，供 ``workspace_edit``
拒绝「未读」或「读后被外部修改」的编辑（stale-edit guard）。进程内、有界
（FIFO 淘汰），多用户键隔离。写入/编辑成功后由工具刷新记录。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional, Tuple

_MAX_ENTRIES = 1024

_Key = Tuple[str, str]
_Stat = Tuple[int, int]


class WorkspaceReadTracker:
    def __init__(self, max_entries: int = _MAX_ENTRIES):
        self._max_entries = max(1, int(max_entries))
        self._entries: "OrderedDict[_Key, _Stat]" = OrderedDict()
        self._lock = threading.Lock()

    def record_read(self, user_id, path: str, mtime_ns: int, size: int) -> None:
        key: _Key = (str(user_id or ""), str(path))
        with self._lock:
            self._entries[key] = (int(mtime_ns), int(size))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def check(self, user_id, path: str, mtime_ns: int, size: int) -> Optional[str]:
        """Return None when the edit is allowed, else a user-facing reason."""
        key: _Key = (str(user_id or ""), str(path))
        with self._lock:
            record = self._entries.get(key)
            if record is not None:
                self._entries.move_to_end(key)
        if record is None:
            return (
                "该文件尚未通过 workspace_read 读取。编辑已有文件前必须先用 "
                "workspace_read 读取其当前内容，确认后再编辑。"
            )
        if record != (int(mtime_ns), int(size)):
            return (
                "文件在读取后已被修改（mtime/size 变化），为避免覆盖外部改动，"
                "请先重新 workspace_read 读取最新内容，再执行编辑。"
            )
        return None

    def forget(self, user_id, path: str) -> None:
        key: _Key = (str(user_id or ""), str(path))
        with self._lock:
            self._entries.pop(key, None)


read_tracker = WorkspaceReadTracker()
