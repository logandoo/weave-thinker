# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Shared generated-file detection for tool results.

``execute_code`` and ``terminal`` both need to report which files a script or
command created/modified, so the frontend can render download cards. The
original flat ``os.listdir`` scan only saw TOP-LEVEL files of the scan
directories — files the agent saved into subdirectories (e.g.
``charts/chart1_*.png``) were never detected, so no download cards appeared
even though the agent's reply claimed the files were downloadable
(conv 3dbedcd5, 2026-08-08).

The scan here is recursive but bounded:
- recursion depth capped at ``_MAX_DEPTH``;
- heavy / internal directories (uploads, scratch, Library, node_modules,
  .venv, __pycache__, .git, noteimg, tool_digests, tool_results) are skipped
  — they are either user input stores, task temp dirs (already filtered
  downstream by ``_is_scratch_path``), or internal metadata, never agent
  deliverables;
- the pre/post mtime+size diff semantics are unchanged from the original
  flat scan.
"""
import os
from typing import Callable, Dict, List, Set, Tuple

_SKIP_DIRS = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".git",
    ".pytest_cache",
    "Library",
    "uploads",
    "scratch",
    "noteimg",
    "tool_digests",
    "tool_results",
}
_MAX_DEPTH = 4

_PathState = Tuple[float, int]


def snapshot_files(scan_dirs: Set[str]) -> Dict[str, _PathState]:
    """Recursive pre-execution snapshot of all files under *scan_dirs*."""
    out: Dict[str, _PathState] = {}
    for scan_dir in scan_dirs:
        _walk(scan_dir, 0, out, _collect)
    return out


def detect_generated_files(
    pre_files: Dict[str, _PathState],
    scan_dirs: Set[str],
    exec_start: float,
    type_fn: Callable[[str], str],
) -> List[Dict]:
    """Diff the post-execution tree against *pre_files*.

    A file is reported as generated when it is new, or its mtime/size
    changed after execution started (same rules as the original flat scan,
    just recursive now).
    """
    generated: List[Dict] = []
    known_paths: Set[str] = set()
    for scan_dir in scan_dirs:
        _walk(scan_dir, 0, generated, _diff, pre_files, exec_start, type_fn, known_paths)
    return generated


def _walk(root: str, depth: int, out, fn, *fn_args) -> None:
    if depth > _MAX_DEPTH:
        return
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return
    for name in entries:
        fp = os.path.join(root, name)
        if os.path.isdir(fp):
            if name in _SKIP_DIRS:
                continue
            _walk(fp, depth + 1, out, fn, *fn_args)
        elif os.path.isfile(fp):
            fn(fp, name, out, *fn_args)


def _collect(fp: str, name: str, out: Dict[str, _PathState]) -> None:
    try:
        out[fp] = (os.path.getmtime(fp), os.path.getsize(fp))
    except OSError:
        out[fp] = (0.0, 0)


def _diff(
    fp: str,
    name: str,
    generated: List[Dict],
    pre_files: Dict[str, _PathState],
    exec_start: float,
    type_fn: Callable[[str], str],
    known_paths: Set[str],
) -> None:
    if fp in known_paths:
        return
    try:
        mtime = os.path.getmtime(fp)
        size = os.path.getsize(fp)
    except OSError:
        return
    prev = pre_files.get(fp)
    added = False
    if prev is None:
        added = True
    else:
        prev_mtime, prev_size = prev
        if mtime >= max(exec_start - 1.0, prev_mtime + 0.001) or size != prev_size:
            added = True
    if added:
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        generated.append({
            "name": name,
            "path": os.path.abspath(fp),
            "size": size,
            "type": type_fn(ext),
        })
        known_paths.add(fp)
