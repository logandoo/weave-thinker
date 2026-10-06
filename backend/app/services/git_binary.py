# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""git 可执行定位 + 受控调用 — 影子 git（workspace_snapshot_service）与
agent `git` 工具（app/tools/git.py）的单一真源。

定位顺序（resolve_git_path）：
1. config `[git_tool].path`（含路径分隔符 → 必须是可执行文件；否则按名字走 which）
2. ``shutil.which``
3. ``$WEAVE_BUNDLED_BIN/git``（Electron 打包把 git 放进 Resources/bin，
   desktop/local-stack.cjs 将该目录注入 sidecar 环境）
4. 裸 ``"git"``（交给子进程 PATH；缺失时由调用方收到 127 语义，不抛异常）

调用约定与 workspace_snapshot_service._run_git 一致：argv 直调（不经 shell）、
超时 kill、返回 `(exit_code, stdout, stderr)` 三元组，任何 spawn 问题映射为
124/125/126/127 退出码而非异常。
"""
import asyncio
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.config import get_config


def resolve_git_path() -> str:
    """解析 git 可执行路径；找不到时返回裸 "git"（由调用方处理 127）。"""
    try:
        preferred = get_config().git_tool_path
    except Exception:
        preferred = "git"
    preferred = (preferred or "git").strip()
    if os.sep in preferred or (os.altsep and os.altsep in preferred):
        if Path(preferred).is_file() and os.access(preferred, os.X_OK):
            return preferred
    else:
        found = shutil.which(preferred)
        if found:
            return found
    bundled = os.environ.get("WEAVE_BUNDLED_BIN", "")
    if bundled:
        candidate = Path(bundled) / "git"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return preferred or "git"


def bundled_bin_dir() -> str:
    """打包环境的内置工具目录（Resources/bin）；非打包部署返回空串。"""
    return os.environ.get("WEAVE_BUNDLED_BIN", "")


def _bundled_git_env() -> Optional[Dict[str, str]]:
    """打包环境为 git 补齐 exec-path/template 定位。

    bottle 构建的 git 把 exec-path/templates 编译进 /opt/homebrew 前缀，
    目标机不存在该路径；显式 GIT_EXEC_PATH / GIT_TEMPLATE_DIR 指向随包
    libexec/ 与 share/（bin/、libexec/、share/ 三者相邻于 Resources/）。
    """
    bundled = bundled_bin_dir()
    if not bundled:
        return None
    resources = os.path.dirname(bundled)
    env: Dict[str, str] = {}
    exec_path = os.path.join(resources, "libexec", "git-core")
    if os.path.isdir(exec_path):
        env["GIT_EXEC_PATH"] = exec_path
    templates = os.path.join(resources, "share", "git-core", "templates")
    if os.path.isdir(templates):
        env["GIT_TEMPLATE_DIR"] = templates
    return env or None


async def run_git_command(
    args: List[str],
    *,
    cwd: str,
    timeout: float = 30.0,
    git_dir: Optional[str] = None,
    work_tree: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """执行 git 子命令。永不抛异常：

    - 127：git 可执行缺失
    - 126：spawn 失败
    - 125：参数非法（如含 NUL）
    - 124：超时（已 kill）
    """
    argv: List[str] = [resolve_git_path()]
    if git_dir:
        argv.append(f"--git-dir={git_dir}")
    if work_tree:
        argv.append(f"--work-tree={work_tree}")
    argv.extend(args)
    if env is None:
        bundled = _bundled_git_env()
        if bundled:
            env = {**os.environ, **bundled}
    else:
        bundled = _bundled_git_env()
        if bundled:
            env = {**env, **bundled}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except ValueError as exc:
        return 125, "", f"invalid git argument: {exc}"
    except OSError as exc:
        return 126, "", f"git spawn failed: {exc}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "git timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
