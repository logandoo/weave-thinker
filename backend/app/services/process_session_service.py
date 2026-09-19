# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""持久进程会话（P1b）：start / write / read / kill / list。

设计（docs/design/BACKEND_DESIGN_workspace_primitives.html）：
- 管道（非 PTY，首版非目标）：stdout/stderr 各由常驻 reader task 追加进有界
  环形缓冲；超限丢头部并在 read 结果中报 dropped_bytes/dropped_total。
- read 默认自上次消费偏移增量返回（避免历史输出反复进入模型上下文），
  wait_ms ≤ 30s；进程退出后可继续读到 EOF 与 exit_code。
- 每用户并发上限；空闲会话惰性回收（start/read/list 触发），不引入后台任务。
- 进程组隔离（start_new_session + killpg）；服务重启后会话不保留。
"""
import asyncio
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config

logger = logging.getLogger(__name__)

_MAX_WAIT_MS = 30_000
_READER_CHUNK = 65536


def _char_prefix_bytes(data: bytes, max_chars: int) -> int:
    """Byte length of the longest prefix with ≤ max_chars complete UTF-8
    characters. An incomplete trailing sequence is left for the next read
    (A4.9 I-5: never split a multi-byte character)."""
    i = 0
    chars = 0
    n = len(data)
    while i < n and chars < max_chars:
        byte = data[i]
        if byte < 0x80:
            need = 1
        elif 0xC2 <= byte <= 0xDF:
            need = 2
        elif 0xE0 <= byte <= 0xEF:
            need = 3
        elif 0xF0 <= byte <= 0xF4:
            need = 4
        else:
            need = 1  # invalid lead byte -> replacement char, consume 1
        if i + need > n:
            break
        i += need
        chars += 1
    return i


def _cfg_int(name: str, default: int) -> int:
    try:
        return int(getattr(get_config(), name))
    except Exception:
        return default


@dataclass
class _Stream:
    buf: bytearray = field(default_factory=bytearray)
    total: int = 0
    consumed: int = 0
    dropped: int = 0

    def append(self, chunk: bytes, cap: int) -> None:
        self.total += len(chunk)
        self.buf += chunk
        if len(self.buf) > cap:
            cut = len(self.buf) - cap
            del self.buf[:cut]
            self.dropped += cut

    def consume_chars(self, max_chars: int) -> Tuple[str, int]:
        start = self.total - len(self.buf)
        gap = 0
        if self.consumed < start:
            gap = start - self.consumed
            self.consumed = start
        if self.consumed >= self.total:
            return "", gap
        available = bytes(self.buf[self.consumed - start :])
        take = _char_prefix_bytes(available, max_chars)
        if take <= 0:
            return "", gap
        text = available[:take].decode("utf-8", "replace")
        self.consumed += take
        return text, gap


@dataclass
class _Session:
    session_id: str
    user_id: str
    command: str
    cwd: str
    started_at: float
    proc: asyncio.subprocess.Process
    out: _Stream = field(default_factory=_Stream)
    err: _Stream = field(default_factory=_Stream)
    readers: List[asyncio.Task] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)


class ProcessSessionManager:
    def __init__(
        self,
        max_sessions_per_user: Optional[int] = None,
        idle_timeout_seconds: Optional[float] = None,
        max_buffer_bytes: Optional[int] = None,
        max_read_chars: Optional[int] = None,
    ):
        self._sessions: Dict[str, _Session] = {}
        self._max_sessions_per_user = max_sessions_per_user
        self._idle_timeout_seconds = idle_timeout_seconds
        self._max_buffer_bytes = max_buffer_bytes
        self._max_read_chars = max_read_chars

    # ------------------------------------------------------------- config
    def _max_sessions(self) -> int:
        value = self._max_sessions_per_user
        return max(1, int(value if value is not None else _cfg_int("agent_process_max_sessions", 4)))

    def _idle_timeout(self) -> float:
        value = self._idle_timeout_seconds
        return float(value if value is not None else _cfg_int("agent_process_idle_timeout_seconds", 1800))

    def _buffer_cap(self) -> int:
        value = self._max_buffer_bytes
        return max(4096, int(value if value is not None else _cfg_int("agent_process_max_buffer_bytes", 262144)))

    def _read_limit(self) -> int:
        value = self._max_read_chars
        return max(200, int(value if value is not None else _cfg_int("agent_process_max_read_chars", 20000)))

    # ------------------------------------------------------------- helpers
    def _get(self, user_id: str, session_id: str) -> Optional[_Session]:
        session = self._sessions.get(str(session_id or ""))
        if session is None or session.user_id != str(user_id):
            return None
        return session

    def _evict_idle(self, keep: Optional[str] = None) -> None:
        timeout = self._idle_timeout()
        now = time.monotonic()
        for session_id, session in list(self._sessions.items()):
            if keep and session_id == keep:
                continue
            if now - session.last_activity <= timeout:
                continue
            self._sessions.pop(session_id, None)
            if session.proc.returncode is None:
                try:
                    os.killpg(os.getpgid(session.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        session.proc.kill()
                    except ProcessLookupError:
                        pass
            for task in session.readers:
                task.cancel()

    async def _terminate(self, session: _Session, grace: float = 3.0) -> None:
        if session.proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(session.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                session.proc.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(session.proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(session.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    session.proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(session.proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                logger.warning("process session %s did not exit after SIGKILL", session.session_id)

    async def _reader(self, session: _Session, stream: Any, target: _Stream) -> None:
        cap = self._buffer_cap()
        try:
            while True:
                chunk = await stream.read(_READER_CHUNK)
                if not chunk:
                    break
                target.append(chunk, cap)
                session.last_activity = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - stream teardown races
            logger.debug("process reader ended: %s", exc)
        finally:
            session.last_activity = time.monotonic()

    def _build_env(self, cwd: str) -> Dict[str, str]:
        try:
            project_bin = str(get_config().project_root / ".venv" / "bin")
        except Exception:
            project_bin = ""
        parts = [p for p in (project_bin, "/usr/local/bin", "/usr/bin", "/bin") if p]
        return {
            "PATH": ":".join(parts),
            "HOME": cwd,
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TZ": "Asia/Shanghai",
        }

    # ------------------------------------------------------------- actions
    async def start(
        self,
        user_id: str,
        command: str,
        cwd: str,
        workspace_root: str,
        wait_ms: int = 300,
    ) -> Dict[str, Any]:
        command = (command or "").strip()
        if not command:
            return {"ok": False, "error": "命令为空。"}

        from app.tools.terminal import _validate_command

        safety = _validate_command(command)
        if safety:
            if safety.get("error"):
                return {"ok": False, "error": f"命令被拒绝: {safety['error']}"}
            if safety.get("_permission_needed"):
                return {"ok": False, "error": safety.get("_permission_description") or "命令需要用户授权。"}

        workspace = str(Path(workspace_root).resolve())
        resolved_cwd = str(Path(cwd or workspace).resolve())
        if not (resolved_cwd == workspace or resolved_cwd.startswith(workspace + os.sep)):
            return {"ok": False, "error": f"工作目录超出工作区: {cwd}"}
        if not Path(resolved_cwd).is_dir():
            return {"ok": False, "error": f"工作目录不存在: {cwd}"}

        # A4.9 I-4: terminal 的运行时路径包含检查同样适用于持久会话
        # （`super_admin_bypass` 时该函数自身放行）。
        from app.tools.terminal import _command_accesses_outside_workspace

        outside = _command_accesses_outside_workspace(command, resolved_cwd, workspace)
        if outside:
            return {"ok": False, "error": f"命令引用了工作区外路径: {outside}"}

        self._evict_idle()
        user_sessions = [s for s in self._sessions.values() if s.user_id == str(user_id)]
        if len(user_sessions) >= self._max_sessions():
            # A4.9 I-3: 上限覆盖残留的已退出会话，先回收它们（否则短命令
            # 连续启动会无界累积缓冲与句柄）。
            for session in user_sessions:
                if session.proc.returncode is not None:
                    self._sessions.pop(session.session_id, None)
                    for task in session.readers:
                        task.cancel()
            user_sessions = [s for s in self._sessions.values() if s.user_id == str(user_id)]
        if len(user_sessions) >= self._max_sessions():
            return {
                "ok": False,
                "error": f"并发会话数已达上限（{self._max_sessions()}）。请先 process action=kill 结束不用的会话。",
            }

        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                command,
                cwd=resolved_cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=self._build_env(resolved_cwd),
            )
        except OSError as exc:
            return {"ok": False, "error": f"进程启动失败: {exc}"}

        session = _Session(
            session_id=uuid.uuid4().hex[:12],
            user_id=str(user_id),
            command=command,
            cwd=resolved_cwd,
            started_at=time.time(),
            proc=proc,
        )
        session.readers = [
            asyncio.create_task(self._reader(session, proc.stdout, session.out)),
            asyncio.create_task(self._reader(session, proc.stderr, session.err)),
        ]
        self._sessions[session.session_id] = session

        initial = await self.read(user_id, session.session_id, wait_ms=max(0, min(int(wait_ms), 2000)))
        return {
            "ok": True,
            "session_id": session.session_id,
            "pid": proc.pid,
            "command": command,
            "cwd": resolved_cwd,
            "output": initial.get("stdout", ""),
            "stderr": initial.get("stderr", ""),
            "cursor": initial.get("cursor", 0),
            "running": initial.get("running", True),
        }

    async def write(self, user_id: str, session_id: str, data: str, newline: bool = True) -> Dict[str, Any]:
        session = self._get(user_id, session_id)
        if session is None:
            return {"ok": False, "error": "会话不存在或不属于当前用户。"}
        if session.proc.returncode is not None:
            return {"ok": False, "error": "进程已退出。", "exit_code": session.proc.returncode}
        payload = (data or "").encode("utf-8")
        if newline:
            payload += b"\n"
        try:
            assert session.proc.stdin is not None
            session.proc.stdin.write(payload)
            await session.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            return {"ok": False, "error": f"写入 stdin 失败（进程可能已退出）: {exc}"}
        session.last_activity = time.monotonic()
        return {"ok": True, "session_id": session.session_id, "bytes_written": len(payload)}

    async def read(
        self,
        user_id: str,
        session_id: str,
        wait_ms: int = 0,
        max_chars: Optional[int] = None,
    ) -> Dict[str, Any]:
        session = self._get(user_id, session_id)
        if session is None:
            return {"ok": False, "error": "会话不存在或不属于当前用户。"}
        # A4.9 M-3: 契约（start/read/list 惰性回收）——read 也触发空闲清理，
        # 但不回收本次读取的目标会话。
        self._evict_idle(keep=session.session_id)
        if session.session_id not in self._sessions:
            return {"ok": False, "error": "会话已空闲超时被回收。"}
        wait_ms = max(0, min(int(wait_ms or 0), _MAX_WAIT_MS))
        deadline = time.monotonic() + wait_ms / 1000.0
        while True:
            if session.out.total > session.out.consumed or session.err.total > session.err.consumed:
                break
            if session.proc.returncode is not None:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)

        if session.proc.returncode is not None:
            # A4.9 Minor: 进程已退出时给 reader 任务最多 0.2s 排空管道尾字节，
            # 避免 start 的初始输出因竞态缺失。
            for _ in range(20):
                if session.out.total > session.out.consumed or session.err.total > session.err.consumed:
                    break
                if all(task.done() for task in session.readers):
                    break
                await asyncio.sleep(0.01)

        limit = int(max_chars) if max_chars else self._read_limit()
        limit = max(200, min(limit, 200_000))
        out_text, out_gap = session.out.consume_chars(limit)
        err_text, err_gap = session.err.consume_chars(limit)
        session.last_activity = time.monotonic()
        return {
            "ok": True,
            "session_id": session.session_id,
            "stdout": out_text,
            "stderr": err_text,
            "cursor": session.out.consumed,
            "running": session.proc.returncode is None,
            "exit_code": session.proc.returncode,
            "dropped_bytes": out_gap + err_gap,
            "dropped_total": session.out.dropped + session.err.dropped,
        }

    async def kill(self, user_id: str, session_id: str) -> Dict[str, Any]:
        session = self._get(user_id, session_id)
        if session is None:
            return {"ok": False, "error": "会话不存在或不属于当前用户。"}
        await self._terminate(session)
        session.last_activity = time.monotonic()
        return {"ok": True, "session_id": session.session_id, "exit_code": session.proc.returncode, "killed": True}

    def list(self, user_id: str) -> Dict[str, Any]:
        self._evict_idle()
        sessions = []
        for session in self._sessions.values():
            if session.user_id != str(user_id):
                continue
            sessions.append(
                {
                    "session_id": session.session_id,
                    "command": session.command,
                    "cwd": session.cwd,
                    "pid": session.proc.pid,
                    "running": session.proc.returncode is None,
                    "exit_code": session.proc.returncode,
                    "started_at": int(session.started_at),
                }
            )
        return {"ok": True, "sessions": sessions}

    async def shutdown_all(self) -> None:
        for session in list(self._sessions.values()):
            for task in session.readers:
                task.cancel()
            try:
                await self._terminate(session, grace=1.0)
            except Exception:
                logger.debug("process shutdown failed for %s", session.session_id, exc_info=True)
            if session.proc.stdin is not None:
                try:
                    session.proc.stdin.close()
                except Exception:
                    pass
        self._sessions.clear()


process_manager = ProcessSessionManager()
