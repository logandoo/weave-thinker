# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""workspace_read tool — read workspace file contents, with offset/limit support.

Essential for cross-step consistency in PEVR mode: allows the agent (and verifier)
to re-read files generated in prior steps to ensure continuity. Supports .docx
via python-docx and all text-based files.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from app.tools.registry import registry
from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_MAX_READ_CHARS = 50000
_DEFAULT_LIMIT_LINES = 200


def _resolve_workspace_path(target: str, workspace_root: str) -> str | None:
    """Resolve target to an existing file inside workspace_root. Same logic as provide_file."""
    ws = str(Path(workspace_root).resolve())
    if os.path.isabs(target):
        resolved = str(Path(target).resolve())
        if (resolved == ws or resolved.startswith(ws + os.sep)) and os.path.isfile(resolved):
            return resolved
    rel = Path(ws) / target
    rel_resolved = str(rel.resolve())
    if (rel_resolved == ws or rel_resolved.startswith(ws + os.sep)) and os.path.isfile(rel_resolved):
        return rel_resolved
    return None


def _read_text_file(filepath: str, offset: int = 0, limit: int = _DEFAULT_LIMIT_LINES) -> Dict[str, Any]:
    """Read a text file with offset/limit and return content with line numbers."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except UnicodeDecodeError:
        try:
            import chardet
            with open(filepath, "rb") as f:
                raw = f.read()
            encoding = chardet.detect(raw).get("encoding", "utf-8")
            all_lines = raw.decode(encoding, errors="replace").splitlines(keepends=True)
        except ImportError:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
    except Exception as e:
        return {"error": f"读取文件失败: {e}", "success": False}

    total_lines = len(all_lines)
    total_chars = sum(len(l) for l in all_lines)

    if offset < 0:
        offset = max(0, total_lines + offset)
    sliced = all_lines[offset:offset + limit]

    content = "".join(sliced)
    if len(content) > _MAX_READ_CHARS:
        content = content[:_MAX_READ_CHARS] + "\n\n[... 内容过长，已截断 ...]"

    return {
        "success": True,
        "content": content,
        "total_lines": total_lines,
        "total_chars": total_chars,
        "offset": offset,
        "limit": limit,
        "returned_lines": len(sliced),
        "returned_chars": len(content),
        "truncated": total_chars > _MAX_READ_CHARS + 100,
    }


def _read_docx_file(filepath: str) -> Dict[str, Any]:
    """Read a .docx file and return text content with paragraph markers."""
    try:
        from docx import Document
        doc = Document(filepath)
        paragraphs = [p.text for p in doc.paragraphs]
        full_text = "\n".join(paragraphs)
        if len(full_text) > _MAX_READ_CHARS:
            full_text = full_text[:_MAX_READ_CHARS] + "\n\n[... 内容过长，已截断 ...]"
        return {
            "success": True,
            "content": full_text,
            "total_lines": len(paragraphs),
            "total_chars": len("\n".join(paragraphs)),
            "truncated": len("\n".join(paragraphs)) > _MAX_READ_CHARS + 100,
        }
    except Exception as e:
        return {"error": f"读取 .docx 文件失败: {e}", "success": False}


async def workspace_read(args: Dict[str, Any], **kwargs) -> str:
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

    target = str(args.get("path") or args.get("file_path") or "").strip().strip("'\"")
    if not target:
        return json.dumps(
            {"error": "未提供文件路径。请通过 path 参数指定文件。", "success": False},
            ensure_ascii=False,
        )

    resolved = _resolve_workspace_path(target, workspace_root)
    if resolved is None:
        return json.dumps(
            {"error": f"未找到文件: {target}。请确认文件路径正确且文件位于用户工作区内。",
             "not_found": target, "success": False},
            ensure_ascii=False,
        )

    ext = os.path.splitext(resolved)[1].lower()
    name = os.path.basename(resolved)
    offset_raw = args.get("offset", 0)
    limit_raw = args.get("limit", _DEFAULT_LIMIT_LINES)
    try:
        offset = int(offset_raw) if offset_raw else 0
    except (ValueError, TypeError):
        offset = 0
    try:
        limit = int(limit_raw) if limit_raw else _DEFAULT_LIMIT_LINES
    except (ValueError, TypeError):
        limit = _DEFAULT_LIMIT_LINES

    if ext in (".docx", ".doc"):
        result = _read_docx_file(resolved)
    else:
        result = _read_text_file(resolved, offset=offset, limit=limit)

    result["name"] = name
    result["path"] = resolved
    result["type"] = ext

    if result.get("success"):
        logger.info("workspace_read: read %s (%d chars, %d lines)", name,
                     result.get("total_chars", 0), result.get("total_lines", 0))

    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="workspace_read",
    toolset="files",
    schema={
        "name": "workspace_read",
        "description": (
            "读取用户工作区中文件的内容。支持 .docx（Word 文档）、.txt、.md、.py、.json 等文本文件。"
            "在死磕模式的 PEVR 计划中，用于读取前序步骤已生成的文件内容，确保当前步骤产出"
            "与已有内容衔接一致，避免重复、矛盾或风格断裂。"
            "通过 offset/limit 参数分页读取大文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对路径、工作区相对路径）。",
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几行开始读取（0-based）。负值表示从文件末尾倒数。默认 0。",
                },
                "limit": {
                    "type": "integer",
                    "description": f"最多返回多少行。默认 {_DEFAULT_LIMIT_LINES}。",
                },
            },
            "required": ["path"],
        },
    },
    handler=workspace_read,
    is_async=True,
    description="Read workspace file contents with offset/limit",
    emoji="",
)
