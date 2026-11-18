# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Workspace path security primitives (2026-09-19).

Single hardened implementation of "resolve a target inside one user's
workspace", shared by ``provide_file`` / ``provide_folder`` and the
``/api/files/*`` endpoints. Security contract:

* only paths inside the requesting user's workspace are ever returned;
* absolute paths are accepted only when already inside the workspace;
* ``..`` traversal is rejected by resolution + containment;
* symlinks are judged by their resolved target (a link escaping the root is
  rejected) and recursive name search never follows or returns symlinks;
* null bytes / control characters / empty targets are rejected;
* every failure raises :class:`WorkspacePathError` — callers never fall back
  to unvalidated paths.

``to_rel_path`` produces workspace-relative POSIX paths (no leading ``/``) —
the only path form shown to users.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# C0 controls except tab/newline/carriage-return; DEL + NUL are rejected too.
_INVALID_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CODE_INVALID = "workspace_path_invalid"
CODE_NOT_FOUND = "workspace_path_not_found"
CODE_OUTSIDE = "workspace_path_outside"


def is_scratch_task_path(path: str | None) -> bool:
    """True when *path* lives under a per-call ``scratch/task_XXXX`` temp dir.

    Scratch dirs are by design intermediate per-call work dirs (the
    execute_code prompt tells the agent each call gets a fresh one). Files
    there are drafts/byproducts — never user-facing deliverables, so they
    must not surface as download cards (conv 2b36fb09) nor as verifier
    evidence (2026-11-15 wave).

    Anchored on the exact ``scratch/task_`` layout created by code_execution
    (``workspace/scratch/task_XXXX``) so a user directory that merely
    contains a segment named ``scratch`` — including ``artifacts/scratch/`` —
    is never misclassified. Accepts absolute and workspace-relative forms.
    """
    if not path:
        return False
    rel = str(path).replace("\\", "/")
    return rel.startswith("scratch/task_") or "/scratch/task_" in rel


def is_legacy_excluded_read(rel: str | None) -> bool:
    """Piece 2：``read_root=user_root`` 时 ``scope=legacy`` 免 grant 读**不得**触及的
    执行/记忆内部目录（用户根相对路径）。

    ⚠ **当前为未接线地基**（no production caller）：J2b 评审 r1 撤除了 scope=legacy 免 grant
    读（排除集只能校验请求前缀，防不住 grep/provide_folder 递归遍历 memory），故本谓词暂无调用方，
    仅由单测覆盖，留给后续「工具侧逐遍历路径过滤」硬化波启用（tests/decisions.md D-20261117-waveJ2-5）。

    排除集 = ``*/memory``（任务/项目记忆注入库，免 grant 读会架空 ``kind=memory`` gate）、
    ``*/staging``（上传暂存）、``runs/*/.vibeweaver``（执行态）、``*/scratch``（逐调用草稿）。
    但**不误伤** ``workspace/`` 下用户自建的同名目录（如 ``workspace/artifacts/memory/``）——
    那是用户产出，不是注入库。``memory`` 仅在其之前没有 ``workspace`` 段时（即 task/project/
    attempt 级记忆库）才算内部目录。先 ``posixpath.normpath`` 归一化（防 ``workspace/../memory``
    绕过）+ 段大小写折叠（防 macOS 大小写不敏感 FS 绕过）+ 绝对/越界 fail-closed。
    """
    if not rel:
        return False
    raw = str(rel).replace("\\", "/")
    norm = posixpath.normpath(raw)
    # 绝对路径或归一化后越出用户根（`../..`）：交上层 is_within 兜底，这里一律视为排除（fail-closed）。
    if posixpath.isabs(raw) or norm == ".." or norm.startswith("../") or norm.startswith("/"):
        return True
    segs = [s.lower() for s in norm.split("/") if s not in ("", ".")]
    for i, seg in enumerate(segs):
        if seg in ("staging", "scratch", ".vibeweaver"):
            return True
        if seg == "memory" and "workspace" not in segs[:i]:
            return True
    return False


class WorkspacePathError(ValueError):
    """Rejected workspace path; ``code`` is one of the workspace_path_* values."""

    def __init__(self, code: str, message: str, target: str = "") -> None:
        self.code = code
        self.target = target
        super().__init__(f"{code}: {message}")


def _normalize(target: str) -> str:
    """Strip surrounding whitespace/quotes and reject unusable targets."""
    if target is None:
        raise WorkspacePathError(CODE_INVALID, "empty path", "")
    text = str(target).strip().strip("'\"")
    if not text:
        raise WorkspacePathError(CODE_INVALID, "empty path", str(target))
    if _INVALID_CHARS.search(text):
        raise WorkspacePathError(CODE_INVALID, "path contains control characters", text)
    return text


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspacePathError(CODE_INVALID, f"unresolvable path: {exc}", str(path)) from exc


def is_within(root: Path, candidate: Path) -> bool:
    """True when *candidate* (resolved) equals *root* or lives under it."""
    try:
        resolved_root = Path(root).resolve()
        resolved_candidate = Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if resolved_candidate == resolved_root:
        return True
    try:
        resolved_candidate.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def find_workspace_file_by_name(name: str, root: str | Path) -> Path | None:
    """Recursively find a regular file named *name* inside *root*.

    Symlinked directories are not descended and symlinked files are not
    returned (defense in depth — the final containment check would reject an
    escaping link anyway, but a link never becomes a card path).
    """
    clean = _normalize(name)
    base = os.path.basename(clean.replace("\\", "/"))
    if not base or base in (".", ".."):
        raise WorkspacePathError(CODE_INVALID, "invalid file name", clean)
    root_path = _resolve(Path(root))
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = [
            d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
        ]
        if base not in filenames:
            continue
        candidate = Path(dirpath) / base
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if is_within(root_path, resolved) and resolved.is_file():
            return resolved
    return None


def resolve_workspace_file(target: str, root: str | Path) -> Path:
    """Resolve *target* to an existing regular file inside *root*.

    Accepts an in-workspace absolute path, a workspace-relative path, or a
    bare filename (recursive search). Raises :class:`WorkspacePathError`
    otherwise.
    """
    root_path = _resolve(Path(root))
    raw = _normalize(target)
    candidate = Path(raw)

    if candidate.is_absolute():
        if not is_within(root_path, candidate):
            logger.warning(
                "workspace path rejected (outside): target=%r root=%s", raw, root_path
            )
            raise WorkspacePathError(CODE_OUTSIDE, "path outside workspace", raw)
        if not candidate.is_file():
            raise WorkspacePathError(CODE_NOT_FOUND, "file not found in workspace", raw)
        return _resolve(candidate)

    direct = root_path / candidate
    if is_within(root_path, direct) and direct.is_file():
        return _resolve(direct)
    if direct.is_symlink():
        # In-workspace link whose target lives outside: reject explicitly.
        resolved = _resolve(direct)
        if not is_within(root_path, resolved):
            logger.warning(
                "workspace path rejected (symlink escape): target=%r root=%s",
                raw,
                root_path,
            )
            raise WorkspacePathError(CODE_OUTSIDE, "symlink escapes workspace", raw)

    # Recursive name search is only for pure basenames. A path-like target
    # that did not resolve directly (missing file, traversal attempt) must NOT
    # be silently substituted by a same-named file elsewhere in the workspace —
    # that would return a different file than requested and hide traversal.
    if "/" in raw or "\\" in raw:
        raise WorkspacePathError(CODE_NOT_FOUND, "file not found in workspace", raw)

    found = find_workspace_file_by_name(os.path.basename(raw.replace("\\", "/")), root_path)
    if found is not None:
        return found
    raise WorkspacePathError(CODE_NOT_FOUND, "file not found in workspace", raw)


def resolve_workspace_dir(target: str, root: str | Path) -> Path:
    """Resolve *target* to an existing directory inside *root*.

    Accepts an in-workspace absolute path or a workspace-relative path.
    Raises :class:`WorkspacePathError` otherwise.
    """
    root_path = _resolve(Path(root))
    raw = _normalize(target)
    candidate = Path(raw)

    if candidate.is_absolute():
        if not is_within(root_path, candidate):
            logger.warning(
                "workspace dir rejected (outside): target=%r root=%s", raw, root_path
            )
            raise WorkspacePathError(CODE_OUTSIDE, "path outside workspace", raw)
        if not candidate.is_dir():
            raise WorkspacePathError(CODE_NOT_FOUND, "directory not found in workspace", raw)
        return _resolve(candidate)

    direct = root_path / candidate
    if is_within(root_path, direct) and direct.is_dir():
        return _resolve(direct)
    raise WorkspacePathError(CODE_NOT_FOUND, "directory not found in workspace", raw)


def to_rel_path(path: Path | str, root: str | Path) -> str:
    """Workspace-relative POSIX path for *path*; ``""`` for the root itself.

    Raises :class:`WorkspacePathError` when *path* is outside *root*.
    """
    root_path = _resolve(Path(root))
    resolved = _resolve(Path(path))
    if resolved == root_path:
        return ""
    if not is_within(root_path, resolved):
        raise WorkspacePathError(CODE_OUTSIDE, "path outside workspace", str(path))
    return resolved.relative_to(root_path).as_posix()
