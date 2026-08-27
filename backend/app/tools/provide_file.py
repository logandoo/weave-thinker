# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""provide_file tool — attach existing workspace files as download cards.

This solves the recurring problem where download cards only appeared when a
tool *generated* a file in the current turn.  When the agent (or the user)
references a file that already exists on disk (created in an earlier turn,
or pre-existing), no ``generated_files`` was reported and no download card
rendered.

The tool is deliberately *explicit* — the agent chooses which files to
attach.  There is no automatic filesystem scanning (which the user
explicitly rejected).  The security model mirrors ``api/files.py``: only
files inside the requesting user's workspace can be attached.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from app.tools.registry import registry
from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_EXT_TYPE_MAP = {
    ".pdf": "pdf", ".docx": "word", ".doc": "word",
    ".pptx": "ppt", ".ppt": "ppt",
    ".xlsx": "excel", ".xls": "excel", ".csv": "csv",
    ".txt": "text", ".md": "markdown", ".json": "json",
    ".py": "python", ".js": "javascript",
    ".html": "html", ".css": "css",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".webp": "image", ".bmp": "image", ".svg": "image",
    ".zip": "archive", ".gz": "archive", ".tar": "archive",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".ogg": "audio",
    ".flac": "audio", ".aac": "audio",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".m4v": "video", ".avi": "video",
}


def _guess_file_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _EXT_TYPE_MAP.get(ext, "file")


def _resolve_within_workspace(target: str, workspace_root: str) -> str | None:
    """Resolve *target* to an existing file inside *workspace_root*.

    ``target`` may be:
      * an absolute path (must be inside the workspace)
      * a workspace-relative path
      * a bare filename (searched recursively, first match wins)
    Returns the resolved absolute path or ``None``.
    """
    ws = str(Path(workspace_root).resolve())

    # Absolute path: must be inside the workspace.
    if os.path.isabs(target):
        resolved = str(Path(target).resolve())
        if (resolved == ws or resolved.startswith(ws + os.sep)) and os.path.isfile(resolved):
            return resolved
        # Fall through to filename search below for a bare-name fallback.

    # Workspace-relative path.
    rel = Path(ws) / target
    rel_resolved = str(rel.resolve())
    if (rel_resolved == ws or rel_resolved.startswith(ws + os.sep)) and os.path.isfile(rel_resolved):
        return rel_resolved

    # Bare-filename recursive search (mirrors api/files.py download fallback).
    base = os.path.basename(target)
    if base and base != target:
        candidate = _find_by_name(base, ws)
        if candidate:
            return candidate
    elif base == target and base:
        candidate = _find_by_name(base, ws)
        if candidate:
            return candidate

    return None


def _find_by_name(name: str, workspace_root: str) -> str | None:
    for dirpath, _dirs, filenames in os.walk(workspace_root):
        for fn in filenames:
            if fn == name:
                candidate = os.path.join(dirpath, fn)
                if os.path.isfile(candidate):
                    return candidate
    return None


async def provide_file(args: Dict[str, Any], **kwargs) -> str:
    """Attach one or more existing workspace files as download cards."""
    user = kwargs.get("user")
    db = kwargs.get("db")
    if user is None or db is None:
        return json.dumps(
            {"error": "用户上下文缺失，无法定位工作区", "generated_files": []},
            ensure_ascii=False,
        )

    from app.services.workspace_service import ensure_user_workspace
    workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
    workspace_root = str(Path(workspace.root_path).resolve())

    # Accept both a single file_path string and a files list.
    raw_files: List[str] = []
    single = args.get("file_path") or args.get("path")
    if single:
        raw_files.append(single)
    files_list = args.get("files")
    if isinstance(files_list, list):
        raw_files.extend(str(f) for f in files_list)

    if not raw_files:
        return json.dumps(
            {"error": "未提供文件路径。请通过 file_path 或 files 参数指定要提供的文件。", "generated_files": []},
            ensure_ascii=False,
        )

    generated: List[Dict[str, Any]] = []
    not_found: List[str] = []
    for target in raw_files:
        target = str(target).strip().strip("'\"")
        if not target:
            continue
        resolved = _resolve_within_workspace(target, workspace_root)
        if resolved is None:
            not_found.append(target)
            logger.info("provide_file: file not found in workspace: %s", target)
            continue
        name = os.path.basename(resolved)
        try:
            size = os.path.getsize(resolved)
        except OSError:
            size = 0
        generated.append({
            "name": name,
            "path": resolved,
            "size": size,
            "type": _guess_file_type(name),
        })
        logger.info("provide_file: attached %s (%d bytes) from %s", name, size, resolved)

    summary_parts: List[str] = []
    if generated:
        names = ", ".join(g["name"] for g in generated)
        summary_parts.append(f"已提供 {len(generated)} 个文件作为下载卡片：{names}")
    if not_found:
        summary_parts.append(f"未找到的文件：{', '.join(not_found)}")
    if not generated:
        summary_parts.append("没有可提供的文件。请确认文件路径正确且文件位于用户工作区内。")

    return json.dumps(
        {
            "success": len(generated) > 0,
            "message": "；".join(summary_parts),
            "generated_files": generated,
            "not_found": not_found,
        },
        ensure_ascii=False,
    )


registry.register(
    name="provide_file",
    toolset="files",
    schema={
        "name": "provide_file",
        "description": (
            "将用户工作区中已存在的文件作为下载卡片提供给用户。"
            "当用户明确要求'把文件给我'、'下载文件'、'提供文件'，"
            "或当你需要将之前生成的文件以下载卡片形式展示给用户时，调用此工具。"
            "支持绝对路径、工作区相对路径、或纯文件名（会自动在工作区内搜索）。"
            "可同时提供多个文件。这不是文件扫描——只附加你明确指定的文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "单个文件的路径（绝对路径、工作区相对路径或文件名）。与 files 二选一。",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "多个文件路径列表。当需要同时提供多个文件时使用。",
                },
            },
        },
    },
    handler=provide_file,
    is_async=True,
    description="Attach existing workspace files as download cards",
    emoji="",
)
