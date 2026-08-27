# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from app.core.config import get_config

if sys.platform != "win32":
    import resource

config = get_config()
logger = logging.getLogger(__name__)


_DANGEROUS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\bos\s*\.\s*system\b'), "os.system is not allowed"),
    (re.compile(r'\bos\s*\.\s*popen\b'), "os.popen is not allowed"),
    (re.compile(r'\bos\s*\.\s*exec\w*\b'), "os.exec* is not allowed"),
    (re.compile(r'\bos\s*\.\s*spawn\w*\b'), "os.spawn* is not allowed"),
    (re.compile(r'\bos\s*\.\s*remove\b'), "os.remove is not allowed"),
    (re.compile(r'\bos\s*\.\s*unlink\b'), "os.unlink is not allowed"),
    (re.compile(r'\bos\s*\.\s*rmdir\b'), "os.rmdir is not allowed"),
    (re.compile(r'\bos\s*\.\s*(makedirs|removedirs|rename|replace|chmod|chown|link|symlink|truncate)\b'), "os path/filesystem mutation is not allowed"),
    (re.compile(r'\bshutil\b'), "shutil module is not allowed"),
    (re.compile(r'\bsubprocess\b'), "subprocess module is not allowed"),
    (re.compile(r'\b__import__\s*\('), "__import__() is not allowed"),
    (re.compile(r'\bimportlib\b'), "importlib is not allowed"),
    (re.compile(r'\beval\s*\('), "eval() is not allowed"),
    (re.compile(r'\bexec\s*\('), "exec() is not allowed"),
    (re.compile(r'\bcompile\s*\('), "compile() is not allowed"),
    (re.compile(r'\bctypes\b'), "ctypes module is not allowed"),
    (re.compile(r'\bsocket\b'), "socket module is not allowed — no network access"),
    (re.compile(r'\brequests\b'), "requests module is not allowed — no network access"),
    (re.compile(r'\burllib\b'), "urllib module is not allowed — no network access"),
    (re.compile(r'\bhttpx\b'), "httpx module is not allowed — no network access"),
    (re.compile(r'\baiohttp\b'), "aiohttp module is not allowed — no network access"),
    (re.compile(r'\b\w*Path\s*\([^)]*\)\s*\.\s*(unlink|rmdir|chmod|chown|rename|replace|symlink_to|hardlink_to)\b'), "pathlib destructive operations are not allowed"),
    (re.compile(r"""\bopen\s*\(\s*['"][/\\]"""), "Absolute paths in open() are not allowed unless inside the user's workspace"),
]

_ALLOWED_WRITE_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".xlsx", ".xls", ".xlsm", ".csv",
    ".pptx", ".ppt",
    ".docx", ".doc",
    ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".ico",
    ".html", ".htm", ".css", ".js",
    ".py", ".sh", ".bat",
    ".zip", ".tar", ".gz",
    ".wav", ".mp3",
})


def _load_omml_helper_source() -> str:
    """Load the injected OMML helper source written into .exec_tmp/omml_helper.py.

    The helper (backend/app/services/omml_helper.py) gives execute_code a
    guaranteed-correct LaTeX -> Word equation-editor (OMML) conversion
    (official MML2OMML.XSL + nary normalization), so the agent never has to
    re-implement the fragile conversion code itself.
    """
    helper_file = config.backend_root / "app" / "services" / "omml_helper.py"
    try:
        return helper_file.read_text(encoding="utf-8")
    except Exception:
        logger.warning("failed to read omml_helper.py", exc_info=True)
        return ""


def _check_paths(code: str, cwd: str, workspace_root: str) -> Optional[str]:
    """Validate that all open() calls target paths inside the workspace,
    and that write-mode calls use allowed file extensions."""
    open_pattern = re.compile(
        r'\bopen\s*\(\s*["\']([^"\']+)["\']\s*(?:,\s*["\']([^"\']*)["\']\s*)?(?:,|\))',
    )
    workspace_root_resolved = str(Path(workspace_root).resolve())
    cwd_resolved = str(Path(cwd).resolve())
    for match in open_pattern.finditer(code):
        filepath = match.group(1)
        mode = (match.group(2) or "").lower()
        if filepath.startswith("/") or filepath.startswith("\\"):
            resolved = str(Path(filepath).resolve())
        else:
            resolved = str((Path(cwd) / filepath).resolve())
        if not resolved.startswith(workspace_root_resolved + os.sep) and resolved != workspace_root_resolved:
            return f"Path outside workspace in open(): {filepath}"
        if not resolved.startswith(cwd_resolved + os.sep) and resolved != cwd_resolved:
            if any(m in mode for m in ('w', 'a', 'x')):
                return f"Write outside current working directory in open(): {filepath}"
        ext = Path(filepath).suffix.lower()
        if ext and any(m in mode for m in ('w', 'a', 'x')) and ext not in _ALLOWED_WRITE_EXTENSIONS:
            return f"File extension '{ext}' is not allowed for write. Allowed: {', '.join(sorted(_ALLOWED_WRITE_EXTENSIONS))}"
    return None


def check_code_safety(
    code: str, cwd: str = "", workspace_root: str = "",
    allow_patterns: Optional[List[str]] = None,
) -> Optional[str]:
    """Run static checks against *code*.  Returns an error message string
    if any dangerous pattern is detected, otherwise ``None``.

    ``allow_patterns`` (opt-in, deathmatch verification gate only) exempts
    specific danger keys from the check — the gate runs sandboxed shell
    verification commands whose generated wrapper needs ``subprocess``."""
    if config.super_admin_bypass:
        return None
    allowed = set(allow_patterns or [])
    for pattern, message in _DANGEROUS_PATTERNS:
        if message in allowed:
            continue
        if pattern.search(code):
            return message
    if cwd and workspace_root:
        path_error = _check_paths(code, cwd, workspace_root)
        if path_error:
            return path_error
    return None


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    return_code: int
    error: Optional[str] = None
    timed_out: bool = False


def _set_memory_limit() -> None:
    if sys.platform == "win32":
        return
    try:
        limit = 1024 * 1024 * 1024
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (min(limit, hard), hard))
    except Exception:
        pass


class CodeExecutionService:
    """Execute user-generated Python code in a sandboxed subprocess."""

    async def execute_python(
        self,
        code: str,
        *,
        cwd: str,
        timeout: Optional[float] = None,
        max_output: Optional[int] = None,
        extra_env: Optional[dict] = None,
        allow_patterns: Optional[List[str]] = None,
    ) -> ExecutionResult:
        """Execute *code* as a Python script inside *cwd*.

        ``allow_patterns`` (opt-in, deathmatch verification gate only)
        exempts specific danger keys from the static check.

        Security measures:
        - Static code analysis for dangerous constructs
        - Working directory confined to workspace root
        - Subprocess timeout
        - Output truncation
        - Restricted environment variables
        """
        if timeout is None:
            timeout = config.code_execution_timeout
        if max_output is None:
            max_output = config.code_execution_max_output

        workspace_root = config.workspace_root.resolve()

        # 1. Static safety check (with cwd and workspace root for path validation)
        safety_error = check_code_safety(code, cwd=cwd, workspace_root=str(workspace_root), allow_patterns=allow_patterns)
        if safety_error:
            return ExecutionResult(
                stdout="",
                stderr="",
                return_code=-1,
                error=f"Security check failed: {safety_error}",
            )

        # 2. Validate cwd is inside the allowed workspace root
        cwd_path = Path(cwd).resolve()
        if not config.super_admin_bypass and not str(cwd_path).startswith(str(workspace_root)):
            return ExecutionResult(
                stdout="",
                stderr="",
                return_code=-1,
                error="Working directory is outside the allowed workspace",
            )
        cwd_path.mkdir(parents=True, exist_ok=True)

        # 3. Determine Python executable
        python_exec = self._resolve_python()

        venv_bin = str(config.project_root / ".venv" / "bin")
        _ws_venv_bin = ""
        _ws_venv = cwd_path / ".venv" / "bin"
        if _ws_venv.exists():
            _ws_venv_bin = str(_ws_venv)
        _path_parts = [p for p in (_ws_venv_bin, venv_bin, "/usr/local/bin", "/usr/bin", "/bin") if p]
        _exec_path = ":".join(_path_parts)

        tmp_dir = cwd_path / ".exec_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        helper_src = _load_omml_helper_source()
        if helper_src:
            helper_path = tmp_dir / "omml_helper.py"
            try:
                helper_path.write_text(helper_src, encoding="utf-8")
            except Exception:
                logger.warning("failed to write omml_helper.py", exc_info=True)

        env = {
            "PATH": _exec_path,
            "HOME": str(cwd_path),
            "TMPDIR": str(tmp_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "TZ": "Asia/Shanghai",
            "WAVETHINKER_MML2OMML_XSL": str(
                config.backend_root / "skills" / "docx_manipulation" / "MML2OMML.XSL"
            ),
            **(extra_env or {}),
        }

        proc_kwargs = {
            "cwd": str(cwd_path),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env,
            "start_new_session": sys.platform != "win32",
        }
        if sys.platform != "win32" and not config.super_admin_bypass:
            proc_kwargs["preexec_fn"] = _set_memory_limit

        try:
            proc = await asyncio.create_subprocess_exec(
                python_exec,
                "-c",
                code,
                **proc_kwargs,
            )

            # Read stdout/stderr via background tasks so partial output is
            # preserved on timeout. The previous code used
            # `asyncio.wait_for(proc.communicate(), timeout)` which, on
            # timeout, cancels communicate() — losing ALL accumulated
            # output. The agent then receives empty stdout and can't tell
            # how far the script got, causing spin loops (conv 01d08b67).
            # With separate reading tasks, on timeout we kill the process
            # (closing the pipes → reading tasks finish) and collect
            # whatever they accumulated.
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

            # Wait for process to exit (with timeout). The reading tasks
            # run concurrently and drain the pipes, preventing deadlock.
            # On timeout, kill the process → pipes close → reading tasks
            # finish naturally with whatever they accumulated.
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                return_code = proc.returncode or 0
                _timed_out = False
            except asyncio.TimeoutError:
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
                _p_stdout = stdout_bytes.decode("utf-8", errors="replace")[:max_output]
                _p_stderr = stderr_bytes.decode("utf-8", errors="replace")[:max_output]
                if len(stdout_bytes) > max_output:
                    _p_stdout += f"\n...[truncated, total {len(stdout_bytes)} chars]"
                return ExecutionResult(
                    stdout=_p_stdout,
                    stderr=_p_stderr,
                    return_code=-1,
                    error=f"Execution timed out after {timeout}s",
                    timed_out=True,
                )

            raw_stdout = stdout_bytes.decode("utf-8", errors="replace")
            raw_stderr = stderr_bytes.decode("utf-8", errors="replace")
            stdout = raw_stdout[:max_output]
            stderr = raw_stderr[:max_output]
            if len(raw_stdout) > max_output:
                stdout += f"\n...[truncated, total {len(raw_stdout)} chars]"
            if len(raw_stderr) > max_output:
                stderr += f"\n...[truncated, total {len(raw_stderr)} chars]"

            return ExecutionResult(
                stdout=stdout,
                stderr=stderr,
                return_code=return_code,
            )
        except Exception as e:
            logger.exception("Code execution failed")
            return ExecutionResult(
                stdout="",
                stderr="",
                return_code=-1,
                error=str(e),
            )

    @staticmethod
    def _resolve_python() -> str:
        """Resolve the Python executable path."""
        venv_python = config.project_root / ".venv" / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
        return "python3"
