# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""grep tool — search workspace files with regex using ripgrep.

Powered by ripgrep (rg) for performance matching opencode. Falls back to
Python re when ripgrep is unavailable.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from app.tools.registry import registry
from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_MAX_RESULTS = 100
_MAX_LINE_LENGTH = 2000
_DEFAULT_TIMEOUT = 30.0

_RG_ARGS_BASE = [
    "--no-config",
    "--json",
    "--hidden",
    "--no-messages",
    "--glob=!**/.git/**",
]


def _contained(resolved: str, workspace_root: str) -> bool:
    return resolved == workspace_root or resolved.startswith(workspace_root + os.sep)


def _find_rg() -> str | None:
    for candidate in ["rg", "/usr/local/bin/rg", "/opt/homebrew/bin/rg"]:
        try:
            subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
            return candidate
        except Exception:
            continue
    return None


def _grep_rg(
    rg_bin: str,
    pattern: str,
    search_root: str,
    workspace_root: str,
    include: str | None,
    ignore_case: bool,
    max_results: int,
) -> Dict[str, Any]:
    args = [rg_bin, *_RG_ARGS_BASE]
    if ignore_case:
        args.append("-i")
    if include:
        args.append(f"--glob={include}")
    args.extend(["--", pattern, search_root])

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"ripgrep 搜索超时 ({_DEFAULT_TIMEOUT}s)", "success": False}
    except Exception as e:
        logger.warning("ripgrep execution failed: %s", e)
        return {"error": f"ripgrep 执行失败: {e}", "success": False}

    stderr = proc.stderr.strip()
    if stderr:
        for line in stderr.splitlines():
            lower = line.lower()
            if "regex parse error" in lower or "error parsing regex" in lower:
                return {"error": f"正则表达式无效: {stderr}", "success": False}

    if proc.returncode not in (0, 1):
        msg = stderr or f"ripgrep exited with code {proc.returncode}"
        return {"error": f"ripgrep 执行失败: {msg}", "success": False}

    results: List[Dict[str, Any]] = []
    truncated = False

    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "match":
            continue
        data = record.get("data", {})
        path_data = data.get("path", {})
        lines_data = data.get("lines", {})
        raw_path = path_data.get("text", "")
        raw_text = lines_data.get("text", "").rstrip("\n").rstrip("\r")
        line_number = data.get("line_number", 0)

        clean_path = raw_path.replace("\\", "/")
        if len(clean_path) > 1 and clean_path[0] == ".":
            clean_path = clean_path[2:] if clean_path[1] in "/\\" else clean_path[1:]

        if len(raw_text) > _MAX_LINE_LENGTH:
            raw_text = raw_text[:_MAX_LINE_LENGTH] + "..."

        results.append({
            "file": clean_path,
            "line_number": line_number,
            "text": raw_text,
        })

        if len(results) >= max_results:
            truncated = True
            break

    return {
        "success": True,
        "pattern": pattern,
        "include": include,
        "matches": len(results),
        "truncated": truncated,
        "results": results,
    }


def _grep_python(
    pattern: re.Pattern,
    search_root: str,
    workspace_root: str,
    include: str | None,
    max_results: int,
) -> Dict[str, Any]:
    import fnmatch

    _SKIPPED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "skill_scripts"}

    results: List[Dict[str, Any]] = []
    files_collected: List[str] = []

    try:
        if os.path.isfile(search_root):
            files_collected = [search_root]
        else:
            for dirpath, dirnames, filenames in os.walk(search_root):
                dirnames[:] = [d for d in dirnames if d not in _SKIPPED_DIRS and not d.startswith(".")]
                for fn in filenames:
                    if not include or fnmatch.fnmatch(fn, include):
                        files_collected.append(os.path.join(dirpath, fn))
    except Exception as e:
        return {"error": f"文件扫描失败: {e}", "success": False}

    for fp in files_collected:
        if len(results) >= max_results:
            break

        try:
            if os.path.getsize(fp) > 10 * 1024 * 1024:
                continue
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if pattern.search(line):
                        rel = os.path.relpath(fp, workspace_root).replace("\\", "/")
                        text = line.rstrip("\n").rstrip("\r")
                        if len(text) > _MAX_LINE_LENGTH:
                            text = text[:_MAX_LINE_LENGTH] + "..."
                        results.append({
                            "file": rel,
                            "line_number": i + 1,
                            "text": text,
                        })
                        if len(results) >= max_results:
                            break
        except Exception:
            continue

    return {
        "success": True,
        "pattern": pattern.pattern,
        "include": include,
        "matches": len(results),
        "truncated": len(results) >= max_results,
        "results": results,
    }


def _format_output(pattern: str, data: Dict[str, Any]) -> str:
    if data.get("error"):
        return json.dumps(data, ensure_ascii=False)

    results = data.get("results", [])
    total = len(results)
    truncated = data.get("truncated", False)

    lines: List[str] = []
    lines.append(f"Found {total} matches for '{pattern}'" + (" (more matches available)" if truncated else ""))

    if not results:
        lines.append("No files found")
        return "\n".join(lines)

    current = ""
    for match in results:
        if current != match["file"]:
            if current:
                lines.append("")
            current = match["file"]
            lines.append(f"{match['file']}:")
        lines.append(f"  Line {match['line_number']}: {match['text']}")

    if truncated:
        lines.append("")
        lines.append("(Results truncated. Consider using a more specific path or pattern.)")

    return "\n".join(lines)


async def grep(args: Dict[str, Any], **kwargs) -> str:
    user = kwargs.get("user")
    db = kwargs.get("db")
    if user is None or db is None:
        return _format_output("", {"error": "用户上下文缺失，无法定位工作区", "success": False})

    from app.services.workspace_service import ensure_user_workspace
    workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
    workspace_root = str(Path(workspace.root_path).resolve())

    pattern_str = str(args.get("pattern") or "").strip()
    if not pattern_str:
        return _format_output("", {"error": "未提供搜索模式。请通过 pattern 参数指定正则表达式。", "success": False})

    include = str(args.get("include") or args.get("file_filter") or "").strip() or None
    ignore_case = bool(args.get("ignore_case", True))
    max_results = int(args.get("max_results", _MAX_RESULTS))

    path_arg = str(args.get("path") or "").strip().strip("'\"")
    if path_arg:
        if os.path.isabs(path_arg):
            search_root = str(Path(path_arg).resolve())
        else:
            search_root = str((Path(workspace_root) / path_arg).resolve())
        if not _contained(search_root, workspace_root):
            return _format_output(pattern_str, {"error": f"搜索路径必须在工作区内: {path_arg}", "success": False})
    else:
        search_root = workspace_root

    if not os.path.exists(search_root):
        return _format_output(pattern_str, {"error": f"路径不存在: {path_arg or workspace_root}", "success": False})

    rg_path = _find_rg()
    if rg_path:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    _grep_rg, rg_path, pattern_str, search_root, workspace_root,
                    include, ignore_case, max_results,
                ),
                timeout=_DEFAULT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            result = {"error": f"ripgrep 搜索超时 ({_DEFAULT_TIMEOUT}s)", "success": False}
        return _format_output(pattern_str, result)

    try:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern_str, flags)
    except re.error as e:
        return _format_output(pattern_str, {"error": f"正则表达式无效: {e}", "success": False})

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                _grep_python, compiled, search_root, workspace_root,
                include, max_results,
            ),
            timeout=_DEFAULT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        result = {"error": f"Python grep 搜索超时 ({_DEFAULT_TIMEOUT}s)", "success": False}

    return _format_output(pattern_str, result)


registry.register(
    name="grep",
    toolset="files",
    schema={
        "name": "grep",
        "description": (
            "Fast content search tool using regex patterns. "
            "Supports full regex syntax, file pattern filtering, and recursive directory search. "
            "Returns file paths with line numbers and matching text (truncated at {} characters per line). "
            "Defaults to searching the entire workspace. Use for finding function definitions, "
            "variable references, configuration values, log patterns, etc."
        ).format(_MAX_LINE_LENGTH),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regex pattern to search for in file contents.",
                },
                "path": {
                    "type": "string",
                    "description": "The directory or file to search in. Defaults to the workspace root.",
                },
                "include": {
                    "type": "string",
                    "description": 'File pattern to filter by, e.g. "*.py", "*.{ts,tsx}".',
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Whether to ignore case. Default true.",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum number of matches to return. Default {_MAX_RESULTS}.",
                },
            },
            "required": ["pattern"],
        },
    },
    handler=grep,
    is_async=True,
    description="Search workspace files with regex pattern matching via ripgrep",
    emoji="",
)
