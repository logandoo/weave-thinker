# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""工作区快照/回滚 — P1a（影子 git，opencode 模式）。

影子 git 目录位于 `{workspace_root}/.snapshots/{user}-{hash8(workspace)}/`，
`--work-tree` 指向用户工作区（不污染工作区）。快照 id = `<unix_ts>-<tree[:10]>`，
ref 为 `refs/snapshots/<id>`。恢复采用 `read-tree + checkout-index -a -f`
（恢复快照内文件；快照后新增的文件保留并在结果中列出）。恢复前自动 capture
一次当前状态（恢复可再撤销）。git 缺失/超时不抛异常，返回 ok=False + error，
调用方（写/编辑）据此降级为 snapshot_error 而不阻断写入。
"""
import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30.0
_REF_PREFIX = "refs/snapshots/"
_MAX_LIST = 100


def _snapshots_base_dir() -> Path:
    return Path(get_config().workspace_root) / ".snapshots"


def _max_file_bytes() -> int:
    try:
        return max(1, int(get_config().workspace_snapshots_max_file_mb)) * 1024 * 1024
    except Exception:
        return 10 * 1024 * 1024


def _shadow_dir(user_id: str, workspace_path: str) -> Path:
    digest = hashlib.sha1(str(Path(workspace_path).resolve()).encode("utf-8")).hexdigest()[:8]
    safe_user = "".join(ch for ch in str(user_id) if ch.isalnum() or ch in "-_")[:64] or "user"
    return _snapshots_base_dir() / f"{safe_user}-{digest}"


async def _run_git(git_dir: Path, work_tree: str, args: List[str]) -> Tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={work_tree}",
            *args,
            # cwd=work_tree: pathspecs/paths resolve against the workspace root,
            # never the server process cwd (A4.9 I-1 fix).
            cwd=work_tree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except ValueError as exc:
        # e.g. NUL byte in a path/ref argument (A4.9 M-4)
        return 125, "", f"invalid git argument: {exc}"
    except OSError as exc:
        return 126, "", f"git spawn failed: {exc}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "git timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _ensure_repo(git_dir: Path, work_tree: str) -> Optional[str]:
    try:
        git_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"snapshot dir not writable: {exc}"
    if (git_dir / "HEAD").exists():
        return None
    code, _out, err = await _run_git(git_dir, work_tree, ["init", "--quiet"])
    if code != 0:
        return err.strip() or f"git init failed ({code})"
    return None


def _large_files(work_tree: str, limit_bytes: int) -> List[str]:
    found: List[str] = []
    for root, dirs, files in os.walk(work_tree):
        dirs[:] = [d for d in dirs if d not in (".git",)]
        for name in files:
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > limit_bytes:
                    found.append(os.path.relpath(path, work_tree))
            except OSError:
                continue
    return found


def _exclude_pathspecs(large: List[str]) -> List[str]:
    """Literal exclude pathspecs (argv-safe: newlines/globs in names cannot
    inject gitignore patterns — A4.9 I-1)."""
    return [":(exclude,literal)" + rel.replace(os.sep, "/") for rel in large]


async def _prune(git_dir: Path, work_tree: str) -> None:
    try:
        retention = max(1, int(get_config().workspace_snapshots_retention))
    except Exception:
        retention = 50
    code, out, _err = await _run_git(
        git_dir,
        work_tree,
        # git 语义：最后一个 --sort 为主键。refname 主序（id 前缀为时间戳，等价
        # 时间倒序），committerdate 次序——A4.9 Minor 修正为与注释一致。
        ["for-each-ref", "--sort=-committerdate", "--sort=-refname", "--format=%(refname)", _REF_PREFIX],
    )
    if code != 0:
        return
    refs = [line for line in out.splitlines() if line.strip()]
    for ref in refs[retention:]:
        await _run_git(git_dir, work_tree, ["update-ref", "-d", ref])


async def create_snapshot(
    user_id: str,
    workspace_path: str,
    reason: str = "",
    snapshot_root: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        enabled = bool(get_config().workspace_snapshots_enabled)
    except Exception:
        enabled = True
    if not enabled:
        return {"ok": False, "disabled": True, "error": "工作区快照已禁用（workspace.snapshots_enabled=false）"}

    work_tree = str(Path(workspace_path).resolve())
    if not Path(work_tree).is_dir():
        return {"ok": False, "error": f"工作区目录不存在: {work_tree}"}
    git_dir = Path(snapshot_root) if snapshot_root else _shadow_dir(user_id, work_tree)

    err = await _ensure_repo(git_dir, work_tree)
    if err:
        return {"ok": False, "error": err}

    skipped = _large_files(work_tree, _max_file_bytes())

    add_args = ["add", "-A", "--", "."] + _exclude_pathspecs(skipped)
    code, out, err = await _run_git(git_dir, work_tree, add_args)
    if code != 0:
        return {"ok": False, "error": f"git add failed: {err.strip() or code}"}
    code, out, err = await _run_git(git_dir, work_tree, ["write-tree"])
    tree = out.strip()
    if code != 0 or not tree:
        return {"ok": False, "error": f"git write-tree failed: {err.strip() or code}"}

    created_at = int(time.time())
    code, out, err = await _run_git(
        git_dir,
        work_tree,
        [
            "-c", "user.name=Weave Thinker",
            "-c", "user.email=agent@weave.local",
            "commit-tree", tree, "-m", reason or "workspace snapshot",
        ],
    )
    commit = out.strip()
    if code != 0 or not commit:
        return {"ok": False, "error": f"git commit-tree failed: {err.strip() or code}"}

    snapshot_id = f"{created_at}-{tree[:10]}"
    code, _out, err = await _run_git(
        git_dir, work_tree, ["update-ref", f"{_REF_PREFIX}{snapshot_id}", commit]
    )
    if code != 0:
        return {"ok": False, "error": f"git update-ref failed: {err.strip() or code}"}
    await _prune(git_dir, work_tree)
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "reason": reason,
        "skipped_large_files": skipped,
    }


async def list_snapshots(
    user_id: str,
    workspace_path: str,
    limit: int = 20,
    snapshot_root: Optional[str] = None,
) -> Dict[str, Any]:
    work_tree = str(Path(workspace_path).resolve())
    git_dir = Path(snapshot_root) if snapshot_root else _shadow_dir(user_id, work_tree)
    if not (git_dir / "HEAD").exists():
        return {"ok": True, "snapshots": []}
    code, out, err = await _run_git(
        git_dir,
        work_tree,
        ["for-each-ref", "--sort=-committerdate", "--format=%(refname)|%(committerdate:unix)|%(subject)", _REF_PREFIX],
    )
    if code != 0:
        return {"ok": False, "error": f"git for-each-ref failed: {err.strip() or code}"}
    snapshots: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3 or not parts[0].startswith(_REF_PREFIX):
            continue
        try:
            created_at = int(parts[1] or 0)
        except (TypeError, ValueError):
            created_at = 0
        snapshots.append(
            {
                "snapshot_id": parts[0][len(_REF_PREFIX):],
                "created_at": created_at,
                "reason": parts[2],
            }
        )
        if len(snapshots) >= max(1, min(int(limit), _MAX_LIST)):
            break
    return {"ok": True, "snapshots": snapshots}


async def restore_snapshot(
    user_id: str,
    workspace_path: str,
    snapshot_id: str,
    snapshot_root: Optional[str] = None,
) -> Dict[str, Any]:
    work_tree = str(Path(workspace_path).resolve())
    if not Path(work_tree).is_dir():
        return {"ok": False, "error": f"工作区目录不存在: {work_tree}"}
    git_dir = Path(snapshot_root) if snapshot_root else _shadow_dir(user_id, work_tree)
    ref = f"{_REF_PREFIX}{snapshot_id}"
    code, out, _err = await _run_git(git_dir, work_tree, ["rev-parse", f"{ref}^{{tree}}"])
    tree = out.strip()
    if code != 0 or not tree:
        return {"ok": False, "error": f"快照不存在或不可用: {snapshot_id}"}

    # 恢复前 capture 当前状态（恢复可再撤销）；失败不阻断恢复本身。
    await create_snapshot(user_id, work_tree, reason=f"before restore {snapshot_id}", snapshot_root=snapshot_root)

    code, _out, err = await _run_git(git_dir, work_tree, ["read-tree", tree])
    if code != 0:
        return {"ok": False, "error": f"git read-tree failed: {err.strip() or code}"}
    code, _out, err = await _run_git(git_dir, work_tree, ["checkout-index", "-a", "-f"])
    if code != 0:
        return {"ok": False, "error": f"git checkout-index failed: {err.strip() or code}"}

    code, out, _err = await _run_git(git_dir, work_tree, ["ls-tree", "-r", "--name-only", tree])
    restored_paths = [line for line in out.splitlines() if line.strip()] if code == 0 else []
    code, out, _err = await _run_git(git_dir, work_tree, ["ls-files", "--others", "--exclude-standard"])
    newer = [line for line in out.splitlines() if line.strip()] if code == 0 else []
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "restored_files": len(restored_paths),
        "restored_paths": restored_paths[:50],
        "newer_files_kept": newer[:50],
        "newer_files_count": len(newer),
    }
