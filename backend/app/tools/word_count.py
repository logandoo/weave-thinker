# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""word_count tool — count words, characters, lines in workspace files.

Supports .docx (via python-docx), .txt, .md, and other text files.
Reports CJK-aware word count, character count, and line count.
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

_CJK_RE = None


def _get_cjk_re():
    global _CJK_RE
    if _CJK_RE is None:
        import re
        _CJK_RE = re.compile(
            r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff'
            r'\u3000-\u303f\uff00-\uffef'
            r'\u3040-\u309f\u30a0-\u30ff'
            r'\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]'
        )
    return _CJK_RE


def _count_docx(filepath: str) -> Dict[str, Any]:
    """Count words in a .docx file using python-docx."""
    try:
        from docx import Document
        doc = Document(filepath)
        paragraphs = []
        char_count = 0
        for p in doc.paragraphs:
            paragraphs.append(p.text)
            char_count += len(p.text)
        full_text = "\n".join(paragraphs)
    except Exception as e:
        return {"error": f"读取 .docx 文件失败: {e}", "success": False}

    return _count_text(full_text)


def _count_text(text: str) -> Dict[str, Any]:
    """Count words, chars, lines in text content with CJK awareness."""
    lines = text.split("\n")
    line_count = len(lines)
    if line_count == 1 and not text.strip():
        line_count = 0

    char_count = len(text)
    char_count_no_spaces = len(text.replace(" ", "").replace("\t", "").replace("\n", ""))

    cjk_re = _get_cjk_re()
    cjk_chars = len(cjk_re.findall(text))

    non_cjk_text = cjk_re.sub(" ", text)
    latin_words = [w for w in non_cjk_text.split() if w.strip()]
    latin_word_count = len(latin_words)
    word_count = cjk_chars + latin_word_count

    return {
        "word_count": word_count,
        "char_count": char_count,
        "char_count_no_spaces": char_count_no_spaces,
        "line_count": line_count,
        "cjk_chars": cjk_chars,
        "latin_words": latin_word_count,
        "paragraph_count": len([l for l in lines if l.strip()]),
        "success": True,
    }


def _count_file(filepath: str, ext: str) -> Dict[str, Any]:
    """Count words in any supported file."""
    ext = ext.lower()
    if ext == ".docx" or ext == ".doc":
        return _count_docx(filepath)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            import chardet
            with open(filepath, "rb") as f:
                raw = f.read()
            encoding = chardet.detect(raw).get("encoding", "utf-8")
            content = raw.decode(encoding, errors="replace")
        except ImportError:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
    except Exception as e:
        return {"error": f"读取文件失败: {e}", "success": False}

    return _count_text(content)


def _resolve_path(target: str, workspace_root: str) -> str | None:
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


async def word_count(args: Dict[str, Any], **kwargs) -> str:
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

    paths: List[str] = []
    single = args.get("path") or args.get("file_path")
    if single:
        paths.append(str(single).strip().strip("'\""))
    files = args.get("files")
    if isinstance(files, list):
        paths.extend(str(f).strip().strip("'\"") for f in files)

    if not paths:
        return json.dumps(
            {"error": "未提供文件路径。请通过 path 或 files 参数指定文件。", "success": False},
            ensure_ascii=False,
        )

    results: List[Dict[str, Any]] = []
    not_found: List[str] = []
    for target in paths:
        if not target:
            continue
        resolved = _resolve_path(target, workspace_root)
        if resolved is None:
            not_found.append(target)
            continue
        ext = Path(resolved).suffix
        counts = _count_file(resolved, ext)
        if counts.get("success") is False:
            results.append({
                "file": target,
                "name": os.path.basename(resolved),
                "error": counts.get("error", "统计失败"),
            })
            continue
        results.append({
            "name": os.path.basename(resolved),
            "path": resolved,
            **counts,
        })

    total_words = sum(r.get("word_count", 0) for r in results)
    total_chars = sum(r.get("char_count", 0) for r in results)
    total_lines = sum(r.get("line_count", 0) for r in results)

    resp = {
        "success": len(results) > 0,
        "total": {
            "files": len(results),
            "word_count": total_words,
            "char_count": total_chars,
            "line_count": total_lines,
        },
        "files": results,
    }
    if not_found:
        resp["not_found"] = not_found

    return json.dumps(resp, ensure_ascii=False)


registry.register(
    name="word_count",
    toolset="files",
    schema={
        "name": "word_count",
        "description": (
            "统计工作区中文件的字数、字符数、行数。支持 .docx（Word 文档）、.txt、.md 等文本文件。"
            "中文按字数统计（CJK 字符），英文按词数统计，两者相加得到总字数。"
            "用于在生成文档后验证实际产出是否满足用户要求的篇幅（如'每章不低于2000字'）。"
            "接受 path（单个文件）或 files（多个文件列表）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "单个文件的路径（绝对路径、工作区相对路径）。与 files 二选一。",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "多个文件路径列表，用于批量统计时使用。",
                },
            },
        },
    },
    handler=word_count,
    is_async=True,
    description="Count words, chars, lines in workspace files",
    emoji="",
)
