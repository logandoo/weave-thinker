# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""workspace_glob tool — find files in user workspace by glob pattern.

Returns file paths with metadata (size, type) for cross-step verification
and to help the agent discover what files already exist in the workspace.
"""
import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from app.tools.registry import registry
from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_MAX_RESULTS = 200

_EXT_TYPE_MAP = {
    ".pdf": "pdf", ".docx": "word", ".doc": "word",
    ".pptx": "ppt", ".ppt": "ppt",
    ".xlsx": "excel", ".xls": "excel", ".csv": "csv",
    ".txt": "text", ".md": "markdown", ".json": "json",
    ".py": "python", ".js": "javascript", ".ts": "javascript",
    ".html": "html", ".css": "css",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".gif": "image", ".webp": "image", ".bmp": "image", ".svg": "image",
    ".zip": "archive", ".gz": "archive", ".tar": "archive",
    ".mp3": "audio", ".wav": "audio", ".mp4": "video", ".mov": "video",
}


def _guess_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _EXT_TYPE_MAP.get(ext, "file")


async def workspace_glob(args: Dict[str, Any], **kwargs) -> str:
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

    pattern = str(args.get("pattern") or "*").strip()
    if not pattern:
        pattern = "*"

    matches: List[Dict[str, Any]] = []
    skipped_dirs = {".git", "__pycache__", "node_modules", ".venv", "skill_scripts"}

    try:
        for dirpath, dirnames, filenames in os.walk(workspace_root):
            dirnames[:] = [d for d in dirnames if d not in skipped_dirs and not d.startswith(".")]
            for fn in filenames:
                if fnmatch.fnmatch(fn, pattern) or fnmatch.fnmatch(os.path.relpath(
                    os.path.join(dirpath, fn), workspace_root
                ), pattern):
                    fp = os.path.join(dirpath, fn)
                    rel = os.path.relpath(fp, workspace_root)
                    try:
                        st = os.stat(fp)
                        matches.append({
                            "name": fn,
                            "path": fp,
                            "relative_path": rel,
                            "size": st.st_size,
                            "type": _guess_type(fn),
                            "mtime": int(st.st_mtime),
                        })
                    except OSError:
                        matches.append({
                            "name": fn,
                            "path": fp,
                            "relative_path": rel,
                            "size": 0,
                            "type": _guess_type(fn),
                            "mtime": 0,
                        })
                    if len(matches) >= _MAX_RESULTS:
                        break
            if len(matches) >= _MAX_RESULTS:
                break
    except Exception as e:
        logger.warning("workspace_glob walk failed: %s", e)
        return json.dumps({"error": f"工作区扫描失败: {e}", "success": False}, ensure_ascii=False)

    matches.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    truncated = len(matches) >= _MAX_RESULTS

    return json.dumps({
        "success": True,
        "count": len(matches),
        "truncated": truncated,
        "pattern": pattern,
        "files": matches,
    }, ensure_ascii=False)


registry.register(
    name="workspace_glob",
    toolset="files",
    schema={
        "name": "workspace_glob",
        "description": (
            "在工作区中按文件名模式搜索文件。支持通配符（* 和 ?）。"
            "例如 pattern='*.docx' 查找所有 Word 文档，pattern='chapter*.txt' 查找各章节文件。"
            "返回文件路径、大小、类型和最后修改时间。最多返回 {_MAX_RESULTS} 条结果。"
            "用于死磕模式中检查工作区中已有哪些文件、避免重复生成或操作不存在的文件。"
        ).format(_MAX_RESULTS=_MAX_RESULTS),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "文件名匹配模式，支持通配符 * 和 ?。例如 '*.docx'、'chapter*.txt'。默认 '*'。",
                },
            },
        },
    },
    handler=workspace_glob,
    is_async=True,
    description="Find files in workspace by glob pattern",
    emoji="",
)
