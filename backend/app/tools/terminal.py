# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os
import re
import signal
import uuid
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

# A4.9 r1 Important-2（2026-09-20）：取消 10k 静默截断后，10k–100k 区间的输出
# 会原样进入消息历史——预算层只在 >100k 时存档，128k 上下文下有溢出风险。
# 单流超过该阈值即全文存档（tool_results/，无损可回读）+ 预览指针；
# 这是「保存而非丢弃」，与用户完整性原则一致（不是省 token 的截断）。
_STREAM_PERSIST_CHARS = 30_000


def _decode_output_capped(raw: bytes) -> str:
    """Decode captured process output WITHOUT silent character amputation.

    用户原则（2026-08-14 / 2026-09-20 审计要求）：信息完整性永远高于节省 token。
    旧实现按 `terminal_max_output`（默认 1 万字符）静默 `[:n]` 截断——terminal 是
    审计 grounding 工具，截断后的结果没有任何存档指针，审计回读也无从恢复，直接
    制造"看不到证据=编造"的误杀面。现在：
    - 正常情况下返回**全量**输出；体积治理交给 tool_result_digest / budget 层
      （>8k 摘要、>100k 全文存档 + 回读指针）。
    - 仅保留 5MB 字节硬顶作为 OOM 保护；触顶时显式标注截断并提供恢复路径提示。
    """
    if len(raw) <= _MAX_OUTPUT_BYTES:
        return raw.decode("utf-8", errors="replace")
    text = raw[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return text + (
        f"\n…[输出超过单次捕获上限 {_MAX_OUTPUT_BYTES} 字节，已截断；"
        "完整输出请分页读取或重定向到文件]…\n"
    )


async def _persist_stream_if_large(text: str, workspace_path: str, tag: str) -> str:
    """单流 >30k：全文存档 + 预览指针（无损，模型/审计可回读）。

    与 `_decode_output_capped` 配合：正常路径不留静默字符截断；体积治理用
    「存档 + 指针」而非丢弃。存档写入 tool_results/（allow-list 根），
    审计 grounding 回读可恢复全文。
    """
    if len(text) <= _STREAM_PERSIST_CHARS:
        return text
    try:
        from app.services.tool_result_budget import BudgetConfig, maybe_persist_tool_result
        cfg = BudgetConfig(max_result_size_chars=0, preview_chars=1500)
        return await maybe_persist_tool_result(
            text,
            tool_name="terminal",
            tool_use_id=tag,
            config=cfg,
            workspace_path=workspace_path,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("terminal stream persist failed: %s", exc)
        return text + (
            f"\n…[输出超长（{len(text)} 字符）且存档失败，已保留全文]…\n"
        )


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


def _tenant_workspace_root():
    from app.services.tenant_service import scoped_root
    return scoped_root(config.workspace_root).resolve()


def _resolve_working_dir(args: dict, kwargs: dict) -> tuple[str, Optional[str]]:
    """同步解析 cwd。任务作用域由调用方（async handler）先行收口写入
    `kwargs["workspace_path"]`，这里保持纯函数语义（既有租户/穿越测试直接调它）。"""
    workspace_root = _tenant_workspace_root()
    wd = args.get("working_dir", "").strip()
    if wd:
        resolved = Path(wd)
        # 任务作用域 enforce：working_dir 必须落在本轮收口后的根里（scratch/task 域），
        # 不再接受「租户根内任意绝对路径」——租户根是所有用户工作区的父目录。
        _scoped_root = str(kwargs.get("workspace_path") or "").strip()
        try:
            from app.services.workspace_scope_service import scoping_mode as _scope_mode

            _scoped_enforce = _scope_mode() == "enforce" and bool(_scoped_root)
        except Exception:
            _scoped_enforce = False
        if _scoped_enforce and not _is_within_workspace(resolved, Path(_scoped_root).resolve()):
            return "", (f"PERMISSION_NEEDED:{wd}") if config.super_admin_bypass else (
                f"working_dir '{wd}' 不在当前任务作用域内，已拒绝")
        # A caller may pass a persisted (pre-tenant) workspace path; accept it
        # only when it is inside the configured global root, never `/etc`.
        _caller_root = Path(kwargs.get("workspace_path") or workspace_root).resolve()
        if (
            _is_within_workspace(resolved, _caller_root)
            or _is_within_workspace(resolved, workspace_root)
            or config.super_admin_bypass
        ):
            return str(resolved.resolve()), None
        if args.get("_permission_granted"):
            return str(resolved.resolve()), None
        return "", f"PERMISSION_NEEDED:{wd}"

    workspace_path = kwargs.get("workspace_path", "")
    if workspace_path:
        resolved = Path(workspace_path)
        # Accept the caller's persisted workspace even when it predates a
        # tenant namespace (enabling WT_TENANT_ID must not brick terminal for
        # existing users).  The path is server-resolved from the user's
        # workspace row, not user input.
        if (
            _is_within_workspace(resolved, workspace_root)
            or (
                _is_within_workspace(resolved, config.workspace_root)
                and resolved.resolve() != config.workspace_root.resolve()
            )
            or config.super_admin_bypass
        ):
            return str(resolved.resolve()), None
        return "", f"Workspace path '{workspace_path}' is outside the allowed workspace"

    return str(workspace_root), None


def _command_accesses_outside_workspace(command: str, cwd: str, workspace_path: str) -> Optional[str]:
    if config.super_admin_bypass:
        return None
    # A4.9 fix-round 2: honor the caller-provided workspace (process passes the
    # user workspace explicitly; terminal passes the global root) instead of
    # silently ignoring the argument.
    if workspace_path:
        workspace_root = Path(workspace_path).resolve()
    else:
        workspace_root = _tenant_workspace_root()
    cwd_path = Path(cwd).resolve()
    if not _is_within_workspace(cwd_path, workspace_root):
        return cwd
    path_patterns = [
        re.compile(r'\b(cd|ls|cat|head|tail|less|more|cp|mv)\s+(/[^\s;|&]+)', re.IGNORECASE),
        re.compile(r'\b(open|read|write)\s*\(\s*["\'](/[^"\']+)["\']'),
        # Relative traversal (`cat ../other/x`, `cd ..`) resolved against cwd.
        re.compile(r'\b(cat|head|tail|less|more|cp|mv)\s+(\.\.?/[^\s;|&]+)', re.IGNORECASE),
        re.compile(r'\bcd\s+([^\s;|&]+)', re.IGNORECASE),
    ]
    for pat in path_patterns:
        for m in pat.finditer(command):
            target = m.group(2) if (m.lastindex or 0) >= 2 else (m.group(1) if m.lastindex else "")
            if target:
                target = target.strip("'\"")
                target_path = (cwd_path / target).resolve() if not target.startswith("/") else Path(target).resolve()
                if not _is_within_workspace(target_path, workspace_root):
                    return target
    return None


def _timeout_guidance(timeout: float, has_output: bool) -> str:
    """超时分支的改写文案（F3，2026-11-01 审计）。

    旧文案无条件断言「已产生上述部分 stdout 输出」，而 Python 子进程默认块缓冲
    被 SIGKILL 后缓冲丢失（现场 conv a4d54ec7 即空输出），把模型引向错误前提；
    且逃生口指向研究型的 background_task，而不是同工具的 durable=true。
    实现与 execute_code 共用 `app/tools/timeout_guidance.py`。
    """
    from app.tools.timeout_guidance import build_timeout_guidance
    return build_timeout_guidance(timeout, has_output, tool_name="terminal")


def _sandbox_env(path: str, cwd: str, bundled_git_env: dict) -> dict:
    """终端沙箱 env（F1 2026-10-26）：HOME 隔离进工作区的同时，把 playwright
    浏览器注册表钉到服务端共享目录（避免每工作区复制 641M 浏览器）。"""
    from app.services.sandbox_env import playwright_browsers_env
    return {
        "PATH": path,
        "HOME": cwd,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "PYTHONDONTWRITEBYTECODE": "1",
        # F1（2026-11-01 审计）：python 子进程接管道时默认块缓冲，被 SIGKILL
        # 时缓冲丢失 → 超时"部分输出"恒为空。强制不缓冲。
        "PYTHONUNBUFFERED": "1",
        "TZ": "Asia/Shanghai",
        **playwright_browsers_env(),
        **(bundled_git_env or {}),
    }


async def terminal(args: dict, **kwargs) -> str:
    command = args.get("command", "").strip()
    if not command:
        return json.dumps({"error": "No command provided"}, ensure_ascii=False)

    safety_result = _validate_command(command)
    if safety_result:
        if safety_result.get("_permission_needed"):
            # 2026-11-12（A4.9 评审 Critical）：用户批准后的重发必须真正执行——否则
            # rm/mv/重定向/管道等"可批准"类命令批准后仍会再次弹窗。注意：只认
            # `_user_approved`（由 agent_loop 批准重发路径以 **kwargs 注入，模型无法
            # 伪造、voice/deathmatch/PTC 的批量 `_permission_granted` 不携带），
            # 不得用 args 内的 `_permission_granted`（voice 对每个调用无条件置真）。
            # 硬拦截（sudo/eval/系统目录等 error 类）不受此影响，仍在下面原样返回。
            if not kwargs.get("_user_approved"):
                return json.dumps({
                    "error": f"Permission needed: {safety_result.get('_permission_description', 'Dangerous command')}",
                    "_permission_needed": True,
                    "_command": safety_result.get("_command", command),
                    "_target_path": safety_result.get("_target_path", ""),
                    "_permission_description": safety_result.get("_permission_description", ""),
                }, ensure_ascii=False)
        else:
            return json.dumps({"error": f"Command blocked: {safety_result.get('error', 'Unknown safety error')}"}, ensure_ascii=False)

    # 任务作用域收口（2026-11-10 §7.3）：enforce 下默认 cwd 是当前 attempt 的 scratch，
    # 稳定 Task 文件的写入由 controlled_publish 完成；off/shadow 保持原行为。
    from app.services.workspace_scope_service import apply_scope_or_error

    _legacy_default = str(kwargs.get("workspace_path") or _tenant_workspace_root())
    _scoped, _scope_err = await apply_scope_or_error(kwargs, default_root=_legacy_default,
                                                     args=args, access="write",
                                                     default_scope="scratch")
    if _scope_err:
        return json.dumps(_scope_err, ensure_ascii=False)
    kwargs = {**kwargs, "workspace_path": _scoped}

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
    _allowed_root = str(kwargs.get("workspace_path") or _tenant_workspace_root())
    outside_path = _command_accesses_outside_workspace(command, cwd, _allowed_root)
    if outside_path and not args.get("_permission_granted") and not config.super_admin_bypass:
        return json.dumps({
            "error": f"Command references path outside workspace: {outside_path}",
            "_permission_needed": True,
            "_target_path": outside_path,
            "_command": command,
            "_permission_description": f"命令请求访问工作区外路径: {outside_path}",
        }, ensure_ascii=False)

    # durable=true → 提交持久作业立即返回句柄（脱出 120s 内联顶）。
    if args.get("durable"):
        _uid = str(getattr(kwargs.get("user"), "id", "") or "")
        if not _uid:
            return json.dumps({"error": "durable terminal job requires an authenticated user"},
                              ensure_ascii=False)
        if not config.agent_durable_jobs_enabled:
            return json.dumps({"error": "持久作业已被配置禁用（[agent.durable_jobs] enabled=false）"},
                              ensure_ascii=False)
        from app.services.job_runner_service import get_job_runner
        job = await get_job_runner().submit(
            user_id=_uid,
            source="terminal",
            spec={"kind": "shell", "command": command, "workdir": str(cwd)},
            idempotency_key=args.get("idempotency_key") or None,
        )
        return json.dumps({
            "durable": True,
            "job_id": job["id"],
            "state": job["state"],
            "handle": "命令已提交持久作业（detached，后端重启不中断）。job_status 轮询、job_logs(offset) 读日志、job_artifacts 取产物、job_cancel 取消。",
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

    # 2026-09-20：不再读取 terminal_max_output 做静默字符截断（见
    # _decode_output_capped 的完整性说明）。体积治理由 digest/budget 层负责。
    venv_bin = str(config.project_root / ".venv" / "bin")
    workspace_path = kwargs.get("workspace_path", "")
    _workspace_venv_bin = ""
    if workspace_path:
        _ws_venv = Path(workspace_path) / ".venv" / "bin"
        if _ws_venv.exists():
            _workspace_venv_bin = str(_ws_venv)
    _node_paths = "/usr/local/bin"
    _bundled_bin = ""
    _bundled_git_env = {}
    try:
        from app.services.git_binary import bundled_bin_dir
        _bundled_bin = bundled_bin_dir()
        if _bundled_bin:
            from app.services.git_binary import _bundled_git_env as _bge
            _bundled_git_env = _bge() or {}
    except Exception:
        _bundled_bin = ""
        _bundled_git_env = {}
    _path_parts = [p for p in (_workspace_venv_bin, venv_bin, _bundled_bin, _node_paths, "/usr/bin", "/bin") if p]
    _terminal_path = ":".join(_path_parts)

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=_sandbox_env(_terminal_path, cwd, _bundled_git_env),
        )
        # F2（2026-10-26）：登记子进程组，后台子孙（cmd &）在关停/重启清扫时可回收
        # A4.9 I1：登记已知命令串（leader 可能秒退，不赌 /proc 回读）；pgid 兜底 proc.pid
        try:
            from app.services.spawn_registry import (
                register as _spawn_register,
                unregister as _spawn_unregister,
                unregister_if_empty as _spawn_unreg_if_empty,
            )
            try:
                _pgid = os.getpgid(proc.pid)
            except Exception:
                _pgid = proc.pid
            _spawn_register(_pgid, command, cwd, "terminal")
        except Exception:
            _pgid = proc.pid

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
            # A4.9 I2：自然结束且组内无幸存后台子孙 → 销记（防 manifest 无界增长）
            try:
                _spawn_unreg_if_empty(_pgid)
            except Exception:
                pass
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            await proc.wait()
            _timed_out = True
            try:
                _spawn_unregister(_pgid)
            except Exception:
                pass

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

        _ws_archive = kwargs.get("workspace_path", "")
        _tag = str(kwargs.get("tool_call_id") or uuid.uuid4().hex[:12])
        if _timed_out:
            _out = await _persist_stream_if_large(
                _decode_output_capped(stdout_bytes), _ws_archive, f"{_tag}-timeout-stdout")
            _err = await _persist_stream_if_large(
                _decode_output_capped(stderr_bytes), _ws_archive, f"{_tag}-timeout-stderr")
            return json.dumps({
                "stdout": _out,
                "stderr": _err,
                "return_code": -1,
                "error": f"Command timed out after {timeout}s",
                "timed_out": True,
                # F5（2026-11-01 审计）：-1 只表示"未正常结束"，补上可判读的信号
                # 与真正生效的超时值（timeout 参数会被 clamp 到 [5, max]）。
                "killed_by": "SIGKILL",
                "timeout_effective": timeout,
                "guidance": _timeout_guidance(timeout, bool(_out.strip() or _err.strip())),
                "working_dir": cwd,
            }, ensure_ascii=False)

        stdout = await _persist_stream_if_large(
            _decode_output_capped(stdout_bytes), _ws_archive, f"{_tag}-stdout")
        stderr = await _persist_stream_if_large(
            _decode_output_capped(stderr_bytes), _ws_archive, f"{_tag}-stderr")

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
                "durable": {
                    "type": "boolean",
                    "description": "If true, submit to the durable job runner and return a handle immediately (bypasses the 120s inline cap; detached, survives backend restarts; track with job_status/job_logs/job_artifacts). Use for long builds/training/ETL.",
                    "default": False,
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Durable-job idempotency key (only used when durable=true); same key returns the existing job.",
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
