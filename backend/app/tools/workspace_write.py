# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""workspace_write / workspace_edit — P0 写与编辑原语。

设计（docs/AUDIT_2026-09-19_coding-agent-primitives.md §6）：
- 模型无关的 string-replace 编辑（非 apply_patch freeform）；
- 编辑强制「先读后写」：workspace_read 记录 (mtime_ns, size)，编辑前校验未变；
- 覆盖/编辑返回 unified diff；写前自动创建影子 git 快照（失败不阻断，结果带
  snapshot_error）；成功后刷新读追踪，便于连续编辑。
- 路径严格限定用户工作区内；禁止写 .git 元数据；内容上限 2MB。
"""
import difflib
import json
import logging
import os
import stat as _stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.tools.registry import registry
from app.services.workspace_file_state import read_tracker

logger = logging.getLogger(__name__)

MAX_CONTENT_BYTES = 2 * 1024 * 1024
DIFF_MAX_LINES = 400

_MODES = ("overwrite", "append", "create_only")


def _error(message: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"error": message, "success": False}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _resolve_target(target: str, workspace_root: str) -> Tuple[Optional[str], Optional[str]]:
    stripped = str(target or "").strip().strip("'\"")
    if not stripped:
        return None, "未提供文件路径（path）。"
    ws = str(Path(workspace_root).resolve())
    candidate = Path(stripped)
    resolved = str(candidate.resolve()) if candidate.is_absolute() else str((Path(ws) / candidate).resolve())
    if not (resolved == ws or resolved.startswith(ws + os.sep)):
        return None, f"路径超出用户工作区，禁止写入: {stripped}"
    rel_parts = Path(os.path.relpath(resolved, ws)).parts
    # A4.9 M-1: 大小写不敏感文件系统上 `.GIT` 亦须拒绝。
    if any(part.lower() == ".git" for part in rel_parts):
        return None, "禁止写入 .git 目录（版本库元数据受保护）。"
    return resolved, None


async def _resolve_workspace(kwargs: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    user = kwargs.get("user")
    db = kwargs.get("db")
    if user is None or db is None:
        return None, "用户上下文缺失，无法定位工作区。"
    from app.services.workspace_service import ensure_user_workspace

    workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
    return str(Path(workspace.root_path).resolve()), None


def _unified_diff(old: str, new: str, name: str) -> Tuple[str, str]:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=name, tofile=name, n=3))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    summary = f"+{added}/-{removed} 行"
    if not diff_lines:
        return "", "无内容变化"
    truncated = len(diff_lines) > DIFF_MAX_LINES
    text = "".join(diff_lines[:DIFF_MAX_LINES])
    if truncated:
        text += f"\n... (diff 截断，仅显示前 {DIFF_MAX_LINES} 行)"
        summary += "（截断）"
    return text, summary


def _atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".wt_write_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        if mode is not None:
            # A4.9 M-2: 保留原文件权限位（mkstemp 默认 0600 会丢掉 +x）。
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


async def _capture_snapshot(user: Any, workspace_root: str, reason: str) -> Dict[str, Any]:
    try:
        from app.services.workspace_snapshot_service import create_snapshot
    except Exception as exc:  # pragma: no cover - only before P1a lands
        return {"snapshot_error": f"snapshot service unavailable: {exc}"}
    try:
        result = await create_snapshot(str(getattr(user, "id", "")), workspace_root, reason=reason)
    except Exception as exc:
        logger.warning("workspace snapshot failed: %s", exc)
        return {"snapshot_error": f"snapshot failed: {exc}"}
    if result.get("ok"):
        return {"snapshot_id": result.get("snapshot_id")}
    return {"snapshot_error": result.get("error") or "snapshot failed"}


async def workspace_write(args: Dict[str, Any], **kwargs) -> str:
    user = kwargs.get("user")
    path_arg = args.get("path") or args.get("file_path") or ""
    content = args.get("content")
    mode = str(args.get("mode") or "overwrite").lower()

    if content is None or not isinstance(content, str):
        return _error("必须提供字符串 content。")
    if mode not in _MODES:
        return _error(f"mode 必须是 {list(_MODES)} 之一。")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return _error(f"内容超过上限 {MAX_CONTENT_BYTES // (1024 * 1024)}MB。")

    workspace_root, err = await _resolve_workspace(kwargs)
    if err:
        return _error(err)
    resolved, err = _resolve_target(path_arg, workspace_root)
    if err:
        return _error(err)
    assert resolved is not None

    path = Path(resolved)
    if path.exists() and path.is_dir():
        return _error(f"目标路径是目录，不是文件: {path_arg}")
    existed = path.exists()
    if mode == "create_only" and existed:
        return _error("文件已存在（mode=create_only 不覆盖）。改用 overwrite 或 workspace_edit。")

    old = _read_text(path) if existed else ""
    new = old + content if mode == "append" else content
    display = os.path.relpath(resolved, workspace_root)
    diff_text, diff_summary = _unified_diff(old, new, display)
    existing_mode = _stat.S_IMODE(path.stat().st_mode) if existed else None

    snapshot_info = await _capture_snapshot(user, workspace_root, f"before workspace_write {display}")
    try:
        _atomic_write(path, new, existing_mode)
    except OSError as exc:
        return _error(f"写入失败: {exc}")

    stat = path.stat()
    read_tracker.record_read(getattr(user, "id", ""), resolved, stat.st_mtime_ns, stat.st_size)

    result: Dict[str, Any] = {
        "success": True,
        "path": resolved,
        "relative_path": display,
        "created": not existed,
        "mode": mode,
        "bytes_written": len(new.encode("utf-8")),
        "lines": new.count("\n") + (0 if new.endswith("\n") or not new else 1),
        "diff_summary": diff_summary,
        "diff": diff_text,
    }
    result.update(snapshot_info)
    logger.info("workspace_write: %s (%s, %s)", display, mode, diff_summary)
    return json.dumps(result, ensure_ascii=False)


async def workspace_edit(args: Dict[str, Any], **kwargs) -> str:
    user = kwargs.get("user")
    path_arg = args.get("path") or args.get("file_path") or ""
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))

    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return _error("必须提供字符串 old_string 与 new_string。")
    if old_string == "":
        return _error("old_string 不能为空；新建文件请用 workspace_write。")

    workspace_root, err = await _resolve_workspace(kwargs)
    if err:
        return _error(err)
    resolved, err = _resolve_target(path_arg, workspace_root)
    if err:
        return _error(err)
    assert resolved is not None

    path = Path(resolved)
    if not path.is_file():
        return _error(f"文件不存在或不是文件: {path_arg}。新建文件请用 workspace_write。")

    stat = path.stat()
    guard = read_tracker.check(getattr(user, "id", ""), resolved, stat.st_mtime_ns, stat.st_size)
    if guard:
        return _error(guard, needs_read=True)

    old = _read_text(path)
    count = old.count(old_string)
    if count == 0:
        return _error(
            "old_string 在文件中未找到（0 处匹配）。请基于 workspace_read 的内容"
            "核对空白/换行后重试；不要让 old_string 跨越未读取的内容。",
            count=0,
        )
    if count > 1 and not replace_all:
        return _error(
            f"old_string 在文件中出现 {count} 次，无法确定替换目标。"
            "请提供更长的上下文使匹配唯一，或设置 replace_all=true 全部替换。",
            count=count,
        )

    if replace_all:
        new = old.replace(old_string, new_string)
        replacements = count
    else:
        new = old.replace(old_string, new_string, 1)
        replacements = 1

    display = os.path.relpath(resolved, workspace_root)
    diff_text, diff_summary = _unified_diff(old, new, display)

    snapshot_info = await _capture_snapshot(user, workspace_root, f"before workspace_edit {display}")
    try:
        _atomic_write(path, new, _stat.S_IMODE(stat.st_mode))
    except OSError as exc:
        return _error(f"写入失败: {exc}")

    new_stat = path.stat()
    read_tracker.record_read(getattr(user, "id", ""), resolved, new_stat.st_mtime_ns, new_stat.st_size)

    result: Dict[str, Any] = {
        "success": True,
        "path": resolved,
        "relative_path": display,
        "replacements": replacements,
        "diff_summary": diff_summary,
        "diff": diff_text,
    }
    result.update(snapshot_info)
    logger.info("workspace_edit: %s (%d replacements, %s)", display, replacements, diff_summary)
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="workspace_write",
    toolset="files",
    schema={
        "name": "workspace_write",
        "description": (
            "创建/覆盖/追加用户工作区内的文本文件，返回 unified diff。"
            "修改已有文件的内容时优先使用 workspace_edit（精确替换），不要整文件重写；"
            "本工具适合新建文件或确需整体覆盖的场景。写前自动创建快照（可用 "
            "workspace_snapshot 回滚）。路径必须位于用户工作区内。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（工作区相对或绝对，须在工作区内）。"},
                "content": {"type": "string", "description": "要写入的完整文本内容（UTF-8）。"},
                "mode": {
                    "type": "string",
                    "enum": list(_MODES),
                    "description": "overwrite（默认，整体覆盖）/ append（追加）/ create_only（仅新建，已存在则报错）。",
                },
            },
            "required": ["path", "content"],
        },
    },
    handler=workspace_write,
    is_async=True,
    description="Create/overwrite/append a workspace file with diff",
    emoji="",
    permission_key="workspace_write",
)


registry.register(
    name="workspace_edit",
    toolset="files",
    schema={
        "name": "workspace_edit",
        "description": (
            "对工作区内已有文件做精确字符串替换（string-replace）。使用前必须先用 "
            "workspace_read 读取该文件（读取后文件被外部修改会拒绝，要求重新读取）。"
            "old_string 必须唯一匹配（否则报错并给出出现次数）；确需全部替换时设置 "
            "replace_all=true。返回 unified diff；写前自动创建快照。这是修改代码/文档"
            "首选工具，禁止用整文件重写代替小改动。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（工作区相对或绝对，须在工作区内）。"},
                "old_string": {"type": "string", "description": "要替换的原文（需与文件内容精确一致，且唯一）。"},
                "new_string": {"type": "string", "description": "替换后的新文本；传空字符串表示删除。"},
                "replace_all": {
                    "type": "boolean",
                    "description": "为 true 时替换全部匹配（默认 false，要求唯一匹配）。",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    handler=workspace_edit,
    is_async=True,
    description="Exact string-replace edit of a workspace file",
    emoji="",
    permission_key="workspace_write",
)
