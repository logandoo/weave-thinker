# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Task-scoped git worktree isolation for delegated code work (Wave 2 P1).

Safety rules:
- the project must live INSIDE the user's workspace (path traversal / absolute
  escapes are rejected);
- the project must be a clean git repository (tracked files unmodified);
- execution happens in a NEW worktree + branch; the base checkout is never
  mutated by the worker;
- merging is an explicit user action and aborts cleanly on conflict.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorktreeError(Exception):
    pass


class WorktreeConflict(WorktreeError):
    def __init__(self, message: str, *, files: Optional[list[str]] = None):
        super().__init__(message)
        self.files = files or []


@dataclass
class WorktreeMeta:
    project_path: str
    worktree_path: str
    branch: str
    base_branch: str
    base_commit: str

    def as_dict(self) -> dict:
        return {
            "project_path": self.project_path,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "base_commit": self.base_commit,
        }


def _run_git(cwd: str | Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {args[0]} timed out") from exc


def _git_ok(cwd: str | Path, *args: str, timeout: int = 60) -> str:
    result = _run_git(cwd, *args, timeout=timeout)
    if result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:500]}")
    return (result.stdout or "").strip()


def validate_project_path(workspace_root: str, project_path: str) -> Path:
    """Resolve a project path inside the user workspace; reject escapes."""
    if not str(project_path or "").strip():
        raise WorktreeError("project_path is required")
    root = Path(workspace_root).resolve()
    raw = Path(str(project_path)).expanduser()
    target = (raw if raw.is_absolute() else root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorktreeError("project_path must stay inside the user workspace") from exc
    if not target.is_dir():
        raise WorktreeError("project_path is not a directory")
    top = _git_ok(target, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != target:
        raise WorktreeError("project_path must be the git repository root")
    status = _git_ok(target, "status", "--porcelain", "--untracked-files=no")
    if status:
        raise WorktreeError("project has uncommitted tracked changes; commit or stash first")
    return target


def create_task_worktree(
    project: Path, *, task_root: Path, task_id: str,
) -> WorktreeMeta:
    base_branch = _git_ok(project, "rev-parse", "--abbrev-ref", "HEAD")
    if not base_branch or base_branch == "HEAD":
        raise WorktreeError("project is in detached HEAD state")
    base_commit = _git_ok(project, "rev-parse", "HEAD")
    branch = f"delegation/{task_id[:8]}"
    worktree_path = (task_root / "worktree").resolve()
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        raise WorktreeError("task worktree path already exists")
    _git_ok(
        project, "worktree", "add", "-b", branch, str(worktree_path), base_commit,
        timeout=180,
    )
    return WorktreeMeta(
        project_path=str(project),
        worktree_path=str(worktree_path),
        branch=branch,
        base_branch=base_branch,
        base_commit=base_commit,
    )


def commit_task_worktree(
    worktree_path: str, *, task_id: str, base_commit: str,
) -> dict:
    """Commit every change in the task worktree; never touch the base checkout."""
    _git_ok(worktree_path, "add", "-A")
    staged = _run_git(worktree_path, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        changed = False
        commit = _git_ok(worktree_path, "rev-parse", "HEAD")
    else:
        changed = True
        _git_ok(
            worktree_path, "commit", "-m",
            f"delegation({task_id[:8]}): task result", "--no-verify",
        )
        commit = _git_ok(worktree_path, "rev-parse", "HEAD")
    diff_stat = _git_ok(worktree_path, "diff", "--stat", f"{base_commit}..HEAD")
    diff_names = _git_ok(worktree_path, "diff", "--name-only", f"{base_commit}..HEAD")
    return {
        "commit": commit,
        "changed": changed,
        "diff_stat": diff_stat[:4000],
        "files": [line for line in diff_names.splitlines() if line][:200],
    }


def merge_task_worktree(
    project_path: str, *, branch: str, base_branch: str,
) -> dict:
    current = _git_ok(project_path, "rev-parse", "--abbrev-ref", "HEAD")
    if current != base_branch:
        raise WorktreeError(
            f"project is on '{current}', expected '{base_branch}'; checkout the base branch first")
    status = _git_ok(project_path, "status", "--porcelain", "--untracked-files=no")
    if status:
        raise WorktreeError("project has uncommitted tracked changes; refusing to merge")
    result = _run_git(
        project_path, "merge", "--no-ff", "--no-edit",
        "-m", f"merge {branch} (delegated task)", branch, timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        try:
            _run_git(project_path, "merge", "--abort")
        except WorktreeError:
            pass
        files = []
        for line in detail.splitlines():
            if line.strip().startswith("CONFLICT"):
                files.append(line.strip()[:300])
        raise WorktreeConflict(
            "merge conflict; merge aborted and the task worktree is preserved",
            files=files,
        )
    commit = _git_ok(project_path, "rev-parse", "HEAD")
    return {"merged": True, "branch": branch, "base_branch": base_branch, "commit": commit}


def remove_task_worktree(project_path: str, worktree_path: str, branch: str) -> None:
    try:
        _run_git(project_path, "worktree", "remove", "--force", worktree_path, timeout=180)
    except WorktreeError:
        logger.warning("worktree remove failed: %s", worktree_path, exc_info=True)
    try:
        _run_git(project_path, "branch", "-D", branch)
    except WorktreeError:
        pass


async def create_task_worktree_async(
    project: Path, *, task_root: Path, task_id: str,
) -> WorktreeMeta:
    return await asyncio.to_thread(
        create_task_worktree, project, task_root=task_root, task_id=task_id)


async def ensure_task_worktree_async(
    project: Path, *, task_root: Path, task_id: str, stored_meta: dict | None,
) -> WorktreeMeta:
    """Reuse the persisted task worktree after a restart, else create it."""
    meta = stored_meta or {}
    worktree_path = Path(str(meta.get("worktree_path") or ""))
    if str(meta.get("branch") or "") and worktree_path.is_dir():
        return WorktreeMeta(
            project_path=str(meta.get("project_path") or project),
            worktree_path=str(worktree_path),
            branch=str(meta.get("branch") or ""),
            base_branch=str(meta.get("base_branch") or ""),
            base_commit=str(meta.get("base_commit") or ""),
        )
    return await create_task_worktree_async(
        project, task_root=task_root, task_id=task_id)


async def commit_task_worktree_async(
    worktree_path: str, *, task_id: str, base_commit: str,
) -> dict:
    return await asyncio.to_thread(
        commit_task_worktree, worktree_path, task_id=task_id, base_commit=base_commit)


async def validate_project_path_async(root: str, project_path: str) -> Path:
    return await asyncio.to_thread(validate_project_path, root, project_path)
