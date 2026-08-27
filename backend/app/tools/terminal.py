# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os
import re
import signal
from pathlib import Path
from typing import Optional

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

_BLOCKED_COMMAND_PATTERNS = [
    re.compile(r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/(\s|$)', re.IGNORECASE),
    re.compile(r'\bmkfs\b', re.IGNORECASE),
    re.compile(r'\bdd\s+if=', re.IGNORECASE),
    re.compile(r'\bshutdown\b', re.IGNORECASE),
    re.compile(r'\breboot\b', re.IGNORECASE),
    re.compile(r'\bhalt\b', re.IGNORECASE),
    re.compile(r'\binit\s+[06](\s|$)', re.IGNORECASE),
    re.compile(r'\bsystemctl\s+(reboot|poweroff|halt)', re.IGNORECASE),
    re.compile(r'\b(nc|ncat|socat)\s+.*-[el]', re.IGNORECASE),
    re.compile(r'\bssh\s+.*-[RD]\s', re.IGNORECASE),
    re.compile(r'\bchmod\s+(-[a-zA-Z]+\s+)*0?7777?\s+/', re.IGNORECASE),
    re.compile(r'\bchown\b.*\broot\b', re.IGNORECASE),
    re.compile(r'\b(crontab|at)\s+-[er]', re.IGNORECASE),
    re.compile(r'\bservice\s+\w+\s+(start|stop|restart)', re.IGNORECASE),
    re.compile(r':\(\)\{\s*:\|:\s*&\s*\}', re.IGNORECASE),
    re.compile(r'\biptables\b', re.IGNORECASE),
    re.compile(r'\bmount\b', re.IGNORECASE),
    re.compile(r'\bumount\b', re.IGNORECASE),
    re.compile(r'\b fdisk\b', re.IGNORECASE),
    re.compile(r'\bparted\b', re.IGNORECASE),
]

_MV_PATTERN = re.compile(r'\bmv\s+', re.IGNORECASE)
_RM_PATTERN = re.compile(r'\brm\s+', re.IGNORECASE)

_SUDO_PATTERN = re.compile(r'\bsudo\b', re.IGNORECASE)

_REDIRECT_DANGER_PATTERN = re.compile(r'>\s*/(etc|proc|sys|dev|boot|root)\b', re.IGNORECASE)

_PIPE_DANGER_PATTERN = re.compile(r'\|\s*(bash|sh|zsh|csh|fish)\b', re.IGNORECASE)

_EVAL_DANGER_PATTERN = re.compile(r'\beval\s+', re.IGNORECASE)

_INTERPRETER_INLINE_PATTERN = re.compile(
    r'\b(python[23]?|perl|ruby|node)\s+-[ce]\s', re.IGNORECASE
)

_PIP_UNINSTALL_PATTERN = re.compile(r'\bpip\s+uninstall\b', re.IGNORECASE)
_PIP_FORCE_PATTERN = re.compile(r'\bpip\s+install\s+.*--break-system-packages', re.IGNORECASE)
_NPM_UNINSTALL_PATTERN = re.compile(r'\bnpm\s+(uninstall|remove|rm|prune)\b', re.IGNORECASE)
_NPM_FORCE_PATTERN = re.compile(r'\bnpm\s+install\s+.*(--force|-f)\b', re.IGNORECASE)

_SOURCE_PATTERN = re.compile(r'(?:\bsource\s+|\.\s+)/?(tmp|var|dev|shm)', re.IGNORECASE)

_MAX_OUTPUT_BYTES = 5 * 1024 * 1024


def check_terminal_requirements() -> bool:
    return getattr(config, "terminal_enabled", False)


def _validate_command(command: str) -> Optional[dict]:
    """Validate command safety.

    Returns:
        dict with key "error" for commands that must be hard-blocked.
        dict with key "_permission_needed" for dangerous actions that should
        prompt the user for confirmation instead of being rejected outright.
        None when the command passes static safety checks.
    """
    if config.super_admin_bypass:
        return None
    if _SUDO_PATTERN.search(command):
        return {"error": "sudo is not allowed"}
    if _INTERPRETER_INLINE_PATTERN.search(command):
        return {"error": "Inline interpreter execution (python -c, perl -e, node -e, etc.) is not allowed"}
    if _EVAL_DANGER_PATTERN.search(command):
        return {"error": "eval is not allowed"}
    if _SOURCE_PATTERN.search(command):
        return {"error": "Sourcing scripts from temp/system directories is not allowed"}
    if _REDIRECT_DANGER_PATTERN.search(command):
        return {
            "_permission_needed": True,
            "_permission_description": "命令将写入系统目录（如 /etc、/proc、/sys 等），属于危险操作",
            "_command": command,
        }
    if _PIPE_DANGER_PATTERN.search(command):
        return {
            "_permission_needed": True,
            "_permission_description": "命令包含管道到 shell 的操作，属于危险操作",
            "_command": command,
        }
    if _PIP_UNINSTALL_PATTERN.search(command):
        return {"error": "pip uninstall/freeze is not allowed — only pip install"}
    if _PIP_FORCE_PATTERN.search(command):
        return {"error": "pip install --break-system-packages is not allowed"}
    if _NPM_UNINSTALL_PATTERN.search(command):
        return {"error": "npm uninstall/remove is not allowed — only npm install"}
    if _NPM_FORCE_PATTERN.search(command):
        return {"error": "npm install --force is not allowed"}
    for pattern in _BLOCKED_COMMAND_PATTERNS:
        if pattern.search(command):
            return {"error": "Blocked command pattern detected"}
    if _MV_PATTERN.search(command):
        return {
            "_permission_needed": True,
            "_permission_description": "mv（移动/重命名文件）属于危险操作，可能影响无关文件。请直接将文件生成到目标位置，不要移动已有文件。",
            "_command": command,
        }
    if _RM_PATTERN.search(command):
        return {
            "_permission_needed": True,
            "_permission_description": "rm（删除文件）属于危险操作，可能删除无关文件。严禁删除与当前任务无关的文件。",
            "_command": command,
        }
    return None


_EXT_TYPE_MAP = {
    "pdf": "pdf", "docx": "word", "doc": "word",
    "pptx": "ppt", "ppt": "ppt",
    "xlsx": "excel", "xls": "excel", "csv": "csv",
    "txt": "text", "md": "markdown", "json": "json",
    "py": "python", "js": "javascript",
    "html": "html", "css": "css",
    "png": "image", "jpg": "image", "jpeg": "image",
    "gif": "image", "webp": "image", "bmp": "image", "svg": "image",
    "zip": "archive", "gz": "archive", "tar": "archive",
    "mp3": "audio", "wav": "audio", "mp4": "video", "mov": "video",
}


def _guess_file_type(ext: str) -> str:
    return _EXT_TYPE_MAP.get(ext, "file")


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        resolved = path.resolve()
        root = workspace_root.resolve()
        return resolved == root or str(resolved).startswith(str(root) + os.sep)
    except Exception:
        return False


def _resolve_working_dir(args: dict, kwargs: dict) -> tuple[str, Optional[str]]:
    workspace_root = config.workspace_root.resolve()

    wd = args.get("working_dir", "").strip()
    if wd:
        resolved = Path(wd)
        if _is_within_workspace(resolved, workspace_root) or config.super_admin_bypass:
            return str(resolved.resolve()), None
        if args.get("_permission_granted"):
            return str(resolved.resolve()), None
        return "", f"PERMISSION_NEEDED:{wd}"

    workspace_path = kwargs.get("workspace_path", "")
    if workspace_path:
        resolved = Path(workspace_path)
        if _is_within_workspace(resolved, workspace_root) or config.super_admin_bypass:
            return str(resolved.resolve()), None
        return "", f"Workspace path '{workspace_path}' is outside the allowed workspace"

    return str(workspace_root), None


def _command_accesses_outside_workspace(command: str, cwd: str, workspace_path: str) -> Optional[str]:
    if config.super_admin_bypass:
        return None
    workspace_root = config.workspace_root.resolve()
    cwd_path = Path(cwd).resolve()
    if not _is_within_workspace(cwd_path, workspace_root):
        return cwd
    path_patterns = [
        re.compile(r'\b(cd|ls|cat|head|tail|less|more|cp|mv)\s+(/[^\s;|&]+)', re.IGNORECASE),
        re.compile(r'\b(open|read|write)\s*\(\s*["\'](/[^"\']+)["\']'),
    ]
    for pat in path_patterns:
        for m in pat.finditer(command):
            target = m.group(2) if m.lastindex >= 2 else ""
            if target:
                target_path = Path(target).resolve()
                if not _is_within_workspace(target_path, workspace_root):
                    return target
    return None


async def terminal(args: dict, **kwargs) -> str:
    command = args.get("command", "").strip()
    if not command:
        return json.dumps({"error": "No command provided"}, ensure_ascii=False)

    safety_result = _validate_command(command)
    if safety_result:
        if safety_result.get("_permission_needed"):
            return json.dumps({
                "error": f"Permission needed: {safety_result.get('_permission_description', 'Dangerous command')}",
                "_permission_needed": True,
                "_command": safety_result.get("_command", command),
                "_target_path": safety_result.get("_target_path", ""),
                "_permission_description": safety_result.get("_permission_description", ""),
            }, ensure_ascii=False)
        return json.dumps({"error": f"Command blocked: {safety_result.get('error', 'Unknown safety error')}"}, ensure_ascii=False)

    cwd, dir_error = _resolve_working_dir(args, kwargs)
    if dir_error:
        if dir_error.startswith("PERMISSION_NEEDED:"):
            target_path = dir_error.split(":", 1)[1]
            return json.dumps({
                "error": f"Working directory '{target_path}' is outside the allowed workspace",
                "_permission_needed": True,
                "_target_path": target_path,
                "_command": command,
                "_permission_description": f"命令请求访问工作区外路径: {target_path}",
            }, ensure_ascii=False)
        return json.dumps({"error": dir_error}, ensure_ascii=False)

    # Check if the command itself references paths outside the allowed workspace.
    outside_path = _command_accesses_outside_workspace(command, cwd, str(config.workspace_root))
    if outside_path and not args.get("_permission_granted") and not config.super_admin_bypass:
        return json.dumps({
            "error": f"Command references path outside workspace: {outside_path}",
            "_permission_needed": True,
            "_target_path": outside_path,
            "_command": command,
            "_permission_description": f"命令请求访问工作区外路径: {outside_path}",
        }, ensure_ascii=False)

    Path(cwd).mkdir(parents=True, exist_ok=True)

    # Pre-execution workspace snapshot for generated-file detection.
    import time as _time
    from app.services.workspace_scan import snapshot_files, detect_generated_files
    _scan_dirs = {cwd}
    ws_path = kwargs.get("workspace_path", "")
    if ws_path and str(Path(ws_path).resolve()) != str(Path(cwd).resolve()):
        _scan_dirs.add(str(Path(ws_path).resolve()))
    _pre_files = await asyncio.to_thread(snapshot_files, _scan_dirs)
    _exec_start = _time.time()

    timeout_val = args.get("timeout", None)
    max_timeout = getattr(config, "terminal_max_timeout", 120)
    default_timeout = getattr(config, "terminal_timeout", 30)
    try:
        timeout = min(float(timeout_val), float(max_timeout)) if timeout_val else float(default_timeout)
    except (ValueError, TypeError):
        timeout = float(default_timeout)
    timeout = max(5.0, min(timeout, float(max_timeout)))

    max_output = getattr(config, "terminal_max_output", 10000)

    venv_bin = str(config.project_root / ".venv" / "bin")
    workspace_path = kwargs.get("workspace_path", "")
    _workspace_venv_bin = ""
    if workspace_path:
        _ws_venv = Path(workspace_path) / ".venv" / "bin"
        if _ws_venv.exists():
            _workspace_venv_bin = str(_ws_venv)
    _node_paths = "/usr/local/bin"
    _path_parts = [p for p in (_workspace_venv_bin, venv_bin, _node_paths, "/usr/bin", "/bin") if p]
    _terminal_path = ":".join(_path_parts)

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={
                "PATH": _terminal_path,
                "HOME": cwd,
                "LANG": "C.UTF-8",
                "TERM": "dumb",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TZ": "Asia/Shanghai",
            },
        )

        # Background reading tasks preserve partial output on timeout
        # (same rationale as code_execution_service.py — conv 01d08b67).
        async def _read_all(stream):
            if stream is None:
                return b""
            chunks = []
            while True:
                try:
                    chunk = await stream.read(65536)
                except Exception:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

        stdout_task = asyncio.ensure_future(_read_all(proc.stdout))
        stderr_task = asyncio.ensure_future(_read_all(proc.stderr))

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            _timed_out = False
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            await proc.wait()
            _timed_out = True

        # Collect output from reading tasks (they finish after pipe close).
        stdout_bytes = b""
        stderr_bytes = b""
        try:
            stdout_bytes = await asyncio.wait_for(stdout_task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            stdout_task.cancel()
        try:
            stderr_bytes = await asyncio.wait_for(stderr_task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            stderr_task.cancel()

        if _timed_out:
            if len(stdout_bytes) > _MAX_OUTPUT_BYTES:
                stdout_bytes = stdout_bytes[:_MAX_OUTPUT_BYTES]
            if len(stderr_bytes) > _MAX_OUTPUT_BYTES:
                stderr_bytes = stderr_bytes[:_MAX_OUTPUT_BYTES]
            return json.dumps({
                "stdout": stdout_bytes.decode("utf-8", errors="replace")[:max_output],
                "stderr": stderr_bytes.decode("utf-8", errors="replace")[:max_output],
                "return_code": -1,
                "error": f"Command timed out after {timeout}s",
                "timed_out": True,
                "guidance": (
                    "命令执行超过超时限制被终止。命令在被终止前已产生上述部分 stdout 输出——"
                    "请先查看部分输出判断进度，然后调整方案："
                    "(1) 减少单次处理的数据量；"
                    "(2) 分批执行，每批保存中间结果；"
                    "(3) 将长任务拆分为多个小步骤；"
                    "(4) 检查是否存在死循环或阻塞等待。"
                    "对于确实需要长时间运行的任务，使用 background_task 工具提交到后台执行。"
                    "不要用相同命令重复调用——会再次超时。"
                ),
                "working_dir": cwd,
            }, ensure_ascii=False)

        if len(stdout_bytes) > _MAX_OUTPUT_BYTES:
            stdout_bytes = stdout_bytes[:_MAX_OUTPUT_BYTES]
        if len(stderr_bytes) > _MAX_OUTPUT_BYTES:
            stderr_bytes = stderr_bytes[:_MAX_OUTPUT_BYTES]

        stdout = stdout_bytes.decode("utf-8", errors="replace")[:max_output]
        stderr = stderr_bytes.decode("utf-8", errors="replace")[:max_output]

        # Post-execution workspace snapshot: detect new/modified files.
        generated_files = await asyncio.to_thread(
            detect_generated_files, _pre_files, _scan_dirs, _exec_start, _guess_file_type,
        )

        result_dict = {
            "stdout": stdout,
            "stderr": stderr,
            "return_code": proc.returncode if proc.returncode is not None else 0,
            "command": command[:500],
            "working_dir": cwd,
        }
        if generated_files:
            result_dict["generated_files"] = generated_files
        return json.dumps(result_dict, ensure_ascii=False)

    except Exception as e:
        logger.exception("Terminal command execution failed")
        return json.dumps({
            "error": "Execution failed",
            "command": command[:500],
        }, ensure_ascii=False)


registry.register(
    name="terminal",
    toolset="system",
    schema={
        "name": "terminal",
        "description": (
            "Execute shell commands in a controlled terminal. "
            "Only use when the user explicitly asks to run external CLI tools "
            "(xelatex, pandoc, ffmpeg, gcc), install packages (pip install, npm install), "
            "or perform system operations other tools cannot handle. "
            "Do NOT use for information gathering (use web_search/browser), file generation, "
            "diagrams, or plain text responses.\n"
            "Runs in the user's workspace. Blocked: sudo, eval, inline interpreters "
            "(python -c, perl -e, node -e), pip/npm uninstall, --force. "
            "Dangerous operations (system dirs, shell pipes, paths outside workspace) "
            "prompt the user for confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute (e.g., 'curl -s https://api.example.com/data', 'ls -la', 'pip install requests', 'npm install axios')",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (default: 30, max: 120)",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command (must be within workspace). Default: workspace root.",
                },
            },
            "required": ["command"],
        },
    },
    handler=terminal,
    check_fn=check_terminal_requirements,
    is_async=True,
    description="Execute shell commands in controlled terminal",
    emoji="",
    permission_key="terminal_execution",
)
