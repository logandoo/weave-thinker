# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""diff tool — compare two files in the workspace and return unified diff."""

import difflib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from app.tools.registry import registry
from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_MAX_DIFF_LINES = 500
_MAX_FILE_SIZE = 5 * 1024 * 1024
_CONTEXT_LINES = 3


def _contained(resolved: str, workspace_root: str) -> bool:
    return resolved == workspace_root or resolved.startswith(workspace_root + os.sep)


def _is_binary(filepath: str) -> bool:
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
        return b"\x00" in chunk
    except (IOError, OSError):
        return True


def _read_lines(filepath: str) -> list[str] | None:
    try:
        file_size = os.path.getsize(filepath)
        if file_size > _MAX_FILE_SIZE:
            return None
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()
    except Exception as e:
        logger.warning("diff failed to read %s: %s", filepath, e)
        return None


async def diff(args: Dict[str, Any], **kwargs) -> str:
    user = kwargs.get("user")
    db = kwargs.get("db")
    if user is None or db is None:
        return json.dumps(
            {"error": "用户上下文缺失，无法定位工作区", "success": False},
            ensure_ascii=False,
        )

    from app.services.workspace_service import ensure_user_workspace
    workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
    workspace_root = str(Path(workspace.root_path).resolve())

    def _resolve(target: str) -> str | None:
        stripped = target.strip().strip("'\"")
        if not stripped:
            return None
        if os.path.isabs(stripped):
            resolved = str(Path(stripped).resolve())
        else:
            resolved = str((Path(workspace_root) / stripped).resolve())
        if _contained(resolved, workspace_root) and os.path.isfile(resolved):
            return resolved
        return None

    path_a = _resolve(str(args.get("file_a") or args.get("path_a") or args.get("old") or ""))
    path_b = _resolve(str(args.get("file_b") or args.get("path_b") or args.get("new") or ""))

    if not path_a or not path_b:
        missing = []
        if not path_a:
            missing.append(f"file_a: {args.get('file_a') or args.get('old') or '(missing)'}")
        if not path_b:
            missing.append(f"file_b: {args.get('file_b') or args.get('new') or '(missing)'}")
        return json.dumps(
            {"error": f"文件未找到: {'; '.join(missing)}", "success": False},
            ensure_ascii=False,
        )

    if _is_binary(path_a):
        return json.dumps(
            {"error": f"无法对比二进制文件: {path_a}", "success": False},
            ensure_ascii=False,
        )
    if _is_binary(path_b):
        return json.dumps(
            {"error": f"无法对比二进制文件: {path_b}", "success": False},
            ensure_ascii=False,
        )

    lines_a = _read_lines(path_a)
    lines_b = _read_lines(path_b)

    if lines_a is None:
        return json.dumps(
            {"error": f"无法读取文件 A: {path_a}（文件过大或编码错误）", "success": False},
            ensure_ascii=False,
        )
    if lines_b is None:
        return json.dumps(
            {"error": f"无法读取文件 B: {path_b}（文件过大或编码错误）", "success": False},
            ensure_ascii=False,
        )

    name_a = args.get("label_a") or os.path.relpath(path_a, workspace_root)
    name_b = args.get("label_b") or os.path.relpath(path_b, workspace_root)

    context_lines = int(args.get("context", _CONTEXT_LINES))

    differ = difflib.unified_diff(
        lines_a, lines_b,
        fromfile=name_a, tofile=name_b,
        n=context_lines,
    )
    diff_lines = list(differ)

    if not diff_lines:
        return json.dumps({
            "success": True,
            "identical": True,
            "file_a": name_a,
            "file_b": name_b,
            "diff": "",
            "summary": "两个文件完全相同。",
        }, ensure_ascii=False)

    truncated = len(diff_lines) > _MAX_DIFF_LINES
    if truncated:
        diff_lines = diff_lines[:_MAX_DIFF_LINES]
        diff_lines.append(f"\n... (diff 被截断，仅显示前 {_MAX_DIFF_LINES} 行)")

    diff_text = "".join(diff_lines)

    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

    return json.dumps({
        "success": True,
        "identical": False,
        "file_a": name_a,
        "file_b": name_b,
        "diff": diff_text,
        "truncated": truncated,
        "summary": f"{added} 行新增, {removed} 行删除" + (" (diff 被截断)" if truncated else ""),
        "added_lines": added,
        "removed_lines": removed,
        "context_lines": context_lines,
    }, ensure_ascii=False)


registry.register(
    name="diff",
    toolset="files",
    schema={
        "name": "diff",
        "description": (
            "比较工作区中两个文件的内容差异，返回 unified diff 格式的结果。"
            "适合查看代码变更、配置修改、版本对比等。"
            "上下文行数可通过 context 参数调整（默认 {} 行）。"
        ).format(_CONTEXT_LINES),
        "parameters": {
            "type": "object",
            "properties": {
                "file_a": {
                    "type": "string",
                    "description": "文件 A 的路径（旧文件）。",
                },
                "file_b": {
                    "type": "string",
                    "description": "文件 B 的路径（新文件）。",
                },
                "label_a": {
                    "type": "string",
                    "description": "diff 中文件 A 的显示标签。",
                },
                "label_b": {
                    "type": "string",
                    "description": "diff 中文件 B 的显示标签。",
                },
                "context": {
                    "type": "integer",
                    "description": "统一 diff 的上下文行数。默认 {}。".format(_CONTEXT_LINES),
                },
            },
            "required": ["file_a", "file_b"],
        },
    },
    handler=diff,
    is_async=True,
    description="Compare two files and return unified diff",
    emoji="",
)
