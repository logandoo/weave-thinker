# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Workspace file service — directory listing + server-side folder zip.

Used by ``/api/files/list`` and ``/api/files/zip`` (2026-09-19). All path
resolution goes through :mod:`app.services.workspace_paths`, so callers can
only ever address the requesting user's workspace. Symlinked entries are
excluded from listings and archives (defense in depth — an escaping link
never becomes downloadable), and every archived member is re-checked for
workspace containment before it is written into the zip.
"""
from __future__ import annotations

import logging
import os
import stat as stat_module
import zipfile
from pathlib import Path
from typing import Any, Dict

from app.services.workspace_paths import (
    CODE_NOT_FOUND,
    WorkspacePathError,
    is_within,
    resolve_workspace_dir,
    to_rel_path,
)

logger = logging.getLogger(__name__)

# Upper bound for one directory listing; larger dirs are reported truncated.
MAX_DIRECTORY_ENTRIES = 1000


def _safe_arc_component(name: str) -> str:
    """Sanitize one archive path component (A4.9: extraction-side zip-slip).

    Replaces backslashes (valid POSIX filename chars that Windows extractors
    treat as separators) and NULs; normalizes dot components.
    """
    clean = name.replace("\\", "_").replace("\x00", "_")
    if clean in ("", ".", ".."):
        return "_"
    return clean


def _guess_type(name: str) -> str:
    # Local import keeps the tools package optional for pure service callers.
    from app.tools.provide_file import _guess_file_type

    return _guess_file_type(name)


def list_directory(root: str | Path, target: str) -> Dict[str, Any]:
    """List one directory inside *root*.

    Returns ``{"path": rel, "entries": [...], "truncated": bool}`` with
    dir-first then case-insensitive name ordering.
    """
    root_path = Path(root).resolve()
    directory = resolve_workspace_dir(target, root_path)
    rel_path = to_rel_path(directory, root_path)

    try:
        children = list(directory.iterdir())
    except OSError as exc:
        raise WorkspacePathError(CODE_NOT_FOUND, f"cannot read directory: {exc}", str(target)) from exc

    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))

    entries: list[Dict[str, Any]] = []
    truncated = False
    for child in children:
        if child.is_symlink():
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        if child.is_dir():
            entry_type, is_dir = "folder", True
        elif child.is_file():
            entry_type, is_dir = _guess_type(child.name), False
        else:
            continue  # sockets/fifos/devices are not part of the user-visible tree
        if len(entries) >= MAX_DIRECTORY_ENTRIES:
            truncated = True
            break
        try:
            entry_rel = to_rel_path(child, root_path)
        except WorkspacePathError:
            continue
        entries.append({
            "name": child.name,
            "rel_path": entry_rel,
            "is_dir": is_dir,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "type": entry_type,
        })

    return {"path": rel_path, "entries": entries, "truncated": truncated}


def build_folder_zip(root: str | Path, target: str, dest: str | Path) -> Dict[str, Any]:
    """Zip the folder at *target* into *dest* (created/overwritten).

    The archive root is the folder's own name, so extracting it reproduces
    the folder. Symlinks and non-regular files are skipped; every member is
    re-validated for workspace containment before being written.
    Returns ``{"name", "size", "file_count"}``.
    """
    root_path = Path(root).resolve()
    directory = resolve_workspace_dir(target, root_path)
    root_name = _safe_arc_component(directory.name) or "workspace"
    dest_path = Path(dest)

    file_count = 0
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
            )
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                if full.is_symlink():
                    continue
                try:
                    resolved = full.resolve()
                except (OSError, RuntimeError):
                    continue
                if not is_within(root_path, resolved) or not resolved.is_file():
                    logger.warning("zip: skipped out-of-workspace member %s", full)
                    continue
                try:
                    relative = resolved.relative_to(directory)
                except ValueError:  # pragma: no cover - is_within guarantees this
                    continue
                safe_parts = [_safe_arc_component(part) for part in relative.parts]
                arcname = "/".join([root_name, *safe_parts])
                # Stream member bytes through a no-follow fd: the path was
                # validated above, but opening it again by name would leave a
                # check→open symlink race (A4.9 TOCTOU).
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    member_fd = os.open(str(resolved), flags)
                except OSError:
                    continue
                try:
                    if not stat_module.S_ISREG(os.fstat(member_fd).st_mode):
                        continue
                    with archive.open(arcname, "w") as member_out:
                        while True:
                            chunk = os.read(member_fd, 1024 * 1024)
                            if not chunk:
                                break
                            member_out.write(chunk)
                finally:
                    os.close(member_fd)
                file_count += 1

    return {
        "name": f"{root_name}.zip",
        "size": dest_path.stat().st_size if dest_path.exists() else 0,
        "file_count": file_count,
    }
