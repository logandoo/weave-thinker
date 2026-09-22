# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""job_runner_service — 持久作业（durable job runner）。

职责：
- execute_code/process/terminal 的 5h+ 重作业 → detached 子进程持久执行：
  后端重启不杀作业（start_new_session），退出回执 = 作业目录 exit_code 文件。
- 状态机 queued→leased→running→{succeeded,failed,cancelled,unknown}
  （迁移白名单 durable_types.can_transition；unknown 仅 reconcile/收尸可出）。
- 幂等：同 idempotency_key 重复 submit 返回既有作业（keyed on cursor 教训的作业态版本）。
- append-only 事件 WAL（durable_job_events, seq per job）+ 产物登记（sha256）。
- cancel 回执四态：requested|acknowledged|too_late|failed。

架构：JobRunner core 可注入 JobStore（测试=MemoryJobStore；生产=SqlJobStore），
文件系统与子进程走真实执行（tmp 目录可测）。
"""
import asyncio
import hashlib
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from app.core.config import get_config
from app.services.durable_types import (
    JOB_STATE_FAILED,
    JOB_STATE_LEASED,
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_SUCCEEDED,
    JOB_STATE_UNKNOWN,
    JOB_TERMINAL_STATES,
    JOB_STATE_CANCELLED,
    can_transition,
)

logger = logging.getLogger(__name__)
config = get_config()

_SCAFFOLD_NAMES = frozenset({"run.sh", "run.log", "exit_code", "script.py"})
_MAX_ARTIFACTS = 50


class JobStore(Protocol):
    """作业存储缝（恢复凭据而非仅远端状态）。"""

    async def get(self, job_id: str) -> Optional[Dict[str, Any]]: ...
    async def get_by_key(self, key: str) -> Optional[Dict[str, Any]]: ...
    async def save(self, job: Dict[str, Any]) -> None: ...
    async def next_seq(self, job_id: str) -> int: ...
    async def append_event(self, job_id: str, seq: int, event_type: str, payload: Any) -> None: ...
    async def list_events(self, job_id: str) -> List[Dict[str, Any]]: ...
    async def add_artifact(self, job_id: str, artifact: Dict[str, Any]) -> None: ...
    async def list_artifacts(self, job_id: str) -> List[Dict[str, Any]]: ...
    async def list_jobs(self, user_id: str, limit: int) -> List[Dict[str, Any]]: ...
    async def lease_next(self, owner: str, now: datetime, lease_seconds: float) -> Optional[Dict[str, Any]]: ...


def validate_spec(spec: Any) -> None:
    """spec 契约校验（Hub 形状）；不合法抛 ValueError。"""
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    kind = spec.get("kind")
    if kind == "shell":
        if not isinstance(spec.get("command"), str) or not spec["command"].strip():
            raise ValueError("kind=shell requires non-empty 'command'")
    elif kind == "python":
        if not isinstance(spec.get("code"), str) or not spec["code"].strip():
            raise ValueError("kind=python requires non-empty 'code'")
    else:
        raise ValueError(f"kind must be 'shell' or 'python', got {kind!r}")
    if spec.get("workdir") is not None and not isinstance(spec.get("workdir"), str):
        raise ValueError("workdir must be a string path")
    ttl = spec.get("timeout_seconds")
    if ttl is not None:
        try:
            if float(ttl) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("timeout_seconds must be a number >= 0 (0=unlimited)")


def read_exit_code(job_dir: Path) -> Optional[int]:
    """退出回执：exit_code 文件存在 = 终态可收（写于 run.sh 尾句）。"""
    p = Path(job_dir) / "exit_code"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip() or "-1")
    except (OSError, ValueError):
        return -1


def read_log_chunk(log_path: Any, offset: Any, max_chunk: int = 262_144) -> Tuple[str, int]:
    """日志游标增量读取（不重复、不丢尾；有界读：单次至多 max_chunk 字节）。"""
    off = max(0, int(offset or 0))
    if not log_path:
        return "", off
    p = Path(log_path)
    if not p.exists():
        return "", off
    try:
        size = p.stat().st_size
    except OSError:
        return "", off
    if off >= size:
        return "", size
    with open(p, "rb") as f:
        f.seek(off)
        data = f.read(max(1, int(max_chunk)))
    # 分片边界安全——回退尾部不完整 UTF-8 序列（下一读从完整码点开始），
    # 避免 CJK 密集日志在每次分片处产生替换符且字节永不返回。
    if off + len(data) < size:
        import codecs
        _dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        _dec.decode(data, final=False)
        _tail = _dec.getstate()[0]
        if _tail and len(_tail) < len(data):
            data = data[: len(data) - len(_tail)]
    return data.decode("utf-8", "replace"), off + len(data)


_DT_FIELDS = ("created_at", "started_at", "finished_at", "lease_until", "cancel_requested_at")


def _norm_dt(value: Any) -> Any:
    """ISO 字符串 → datetime（DateTime 列写入前显式规范化，不赌驱动强转）。"""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    return value


def scan_artifacts(workdir: Path) -> List[Dict[str, Any]]:
    """产物登记扫描（path+sha256+bytes）；脚手架文件排除、数量与体积有界。"""
    out: List[Dict[str, Any]] = []
    root = Path(workdir)
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if len(out) >= _MAX_ARTIFACTS:
            break
        if not p.is_file() or p.name in _SCAFFOLD_NAMES:
            continue
        try:
            if p.stat().st_size > 64 * 1024 * 1024:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        out.append({
            "path": str(p.relative_to(root)),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
    return out


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def _kill_process_group(pid: Any) -> None:
    """SIGTERM 作业进程组（先验 pgid==pid——作业经 start_new_session
    自任组长；不匹配=pid 疑似被回收复用，拒绝误杀）。"""
    try:
        pid = int(pid)
        pgid = os.getpgid(pid)
        if pgid != pid:
            return
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        pass


class JobRunner:
    """作业运行器 core（store 注入；文件系统/子进程真实执行）。"""

    def __init__(self, store: JobStore, jobs_root: str, owner_id: str = "worker-1",
                 max_concurrent: Optional[int] = None):
        self.store = store
        self.jobs_root = Path(jobs_root)
        self.owner_id = owner_id
        self.max_concurrent = max_concurrent or config.agent_durable_jobs_max_concurrent

    # ---------- paths / fs ----------

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def _spawn_detached(self, job: Dict[str, Any]) -> Tuple[int, str]:
        """启动 detached 子进程（start_new_session：后端重启不连带杀死）。

        返回 (pid, log_path)。退出回执由 run.sh 尾句写 exit_code 文件。
        """
        spec = job["spec"]
        d = self.job_dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        workdir = spec.get("workdir") or str(d / "work")
        Path(workdir).mkdir(parents=True, exist_ok=True)
        log = d / "run.log"
        exit_file = d / "exit_code"
        if spec.get("kind") == "python":
            script = d / "script.py"
            script.write_text(spec["code"])
            inner = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        else:
            inner = spec["command"]
        run_sh = d / "run.sh"
        # inner 用子壳 ( … ) 包裹——命令含 exit/exec/set -e 只终止
        # 子壳，包装壳仍能写 exit_code 回执；cd 失败也落 126 回执（不再跳过）。
        run_sh.write_text(
            "#!/bin/sh\n"
            f"cd {shlex.quote(workdir)} || {{ echo 126 > {shlex.quote(str(exit_file))}; exit 126; }}\n"
            f"( {inner} ) > {shlex.quote(str(log))} 2>&1\n"
            f"echo $? > {shlex.quote(str(exit_file))}\n"
        )
        os.chmod(run_sh, 0o755)
        proc = subprocess.Popen(
            ["/bin/sh", str(run_sh)],
            cwd=workdir,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid, str(log)

    # ---------- events ----------

    async def _event(self, job_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        seq = await self.store.next_seq(job_id)
        await self.store.append_event(job_id, seq, event_type,
                                      json.dumps(payload or {}, ensure_ascii=False))

    # ---------- public API ----------

    async def submit(self, *, user_id: str, source: str, spec: Dict[str, Any],
                     idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if not config.agent_durable_jobs_enabled:
            # 兜底：禁用态下绝不静默排队（execute_code/terminal durable 亦经此）
            raise ValueError("durable jobs disabled by config ([agent.durable_jobs] enabled=false)")
        validate_spec(spec)
        if idempotency_key:
            existing = await self.store.get_by_key(idempotency_key)
            if existing:
                # 全局键不跨用户放行（防键抢占/键嗅探取 job_id）
                if str(existing.get("user_id")) != str(user_id):
                    raise ValueError("idempotency_key belongs to another user")
                return dict(existing)
        ttl = spec.get("timeout_seconds")
        job = {
            "id": str(uuid.uuid4()),
            "user_id": str(user_id),
            "source": str(source or "tool"),
            "spec": dict(spec),
            "idempotency_key": idempotency_key,
            "state": JOB_STATE_QUEUED,
            "attempts": 0,
            "lease_owner": None,
            "lease_until": None,
            "pid": None,
            "log_path": None,
            "exit_code": None,
            "result_digest": None,
            "cancel_requested_at": None,
            "cancel_state": None,
            "error": None,
            "timeout_seconds": float(ttl) if ttl is not None else float(config.agent_durable_jobs_total_timeout),
            "created_at": datetime.utcnow().isoformat(),
            "started_at": None,
            "finished_at": None,
        }
        await self.store.save(job)
        await self._event(job["id"], "JobSubmitted", {"source": source, "idempotency_key": idempotency_key})
        return dict(job)

    async def _owned_row(self, job_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """所有权门（IDOR 防护）：非属主一律 not found（不泄露存在性）。"""
        row = await self.store.get(job_id)
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        if user_id is not None and str(row.get("user_id")) != str(user_id):
            raise KeyError(f"job not found: {job_id}")
        return row

    async def poll(self, job_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        return dict(await self._owned_row(job_id, user_id))

    async def logs(self, job_id: str, offset: int, user_id: Optional[str] = None) -> Tuple[str, int]:
        row = await self._owned_row(job_id, user_id)
        chunk, next_offset = read_log_chunk(row.get("log_path") or (self.job_dir(job_id) / "run.log"), offset)
        # 日志消费落 WAL（JobLogOffset）——有实际新字节才记，fail-open
        if chunk:
            try:
                await self._event(job_id, "JobLogOffset", {"from": int(offset or 0), "to": int(next_offset)})
            except Exception:
                logger.debug("JobLogOffset event failed (fail-open)", exc_info=True)
        return chunk, next_offset

    async def list(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return [dict(j) for j in await self.store.list_jobs(user_id, max(1, min(int(limit), 100)))]

    async def artifacts(self, job_id: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        await self._owned_row(job_id, user_id)
        return [dict(a) for a in await self.store.list_artifacts(job_id)]

    async def cancel(self, job_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """cancel 回执四态（Hub）：requested|acknowledged|too_late|failed。

        CAS 冲突时重读新鲜行并**重应用**取消意图（不再丢取消）；
        至多两轮，过期即 failed（可重试）。
        """
        try:
            row = await self._owned_row(job_id, user_id)
            for _attempt in range(2):
                state = row.get("state")
                if state in JOB_TERMINAL_STATES or state == JOB_STATE_UNKNOWN:
                    receipt = {"job_id": job_id, "cancel_state": "too_late", "state": state}
                    await self._event(job_id, "JobCancelReceipt", receipt)
                    return receipt
                now = datetime.utcnow().isoformat()
                row["cancel_requested_at"] = now
                if state == JOB_STATE_QUEUED:
                    if not can_transition(state, JOB_STATE_CANCELLED):
                        return {"job_id": job_id, "cancel_state": "failed", "error": "illegal transition"}
                    row["state"] = JOB_STATE_CANCELLED
                    row["cancel_state"] = "acknowledged"
                    row["finished_at"] = now
                    if await self._save_cas(row, JOB_STATE_QUEUED):
                        await self._event(job_id, "JobCancelRequested", {"from": state})
                        receipt = {"job_id": job_id, "cancel_state": "acknowledged", "state": row["state"]}
                        await self._event(job_id, "JobCancelReceipt", receipt)
                        return receipt
                else:
                    # leased / running：登记请求 + 尽力终止进程组，回执=requested
                    row["cancel_state"] = "requested"
                    _pid = row.get("pid")
                    if _pid:
                        _kill_process_group(_pid)
                    if await self._save_cas(row, state):
                        await self._event(job_id, "JobCancelRequested", {"from": state, "pid": _pid})
                        return {"job_id": job_id, "cancel_state": "requested", "state": state}
                # CAS 冲突：重读新鲜行后再试一轮
                fresh = await self.store.get(job_id)
                if fresh is None:
                    return {"job_id": job_id, "cancel_state": "failed", "error": "job vanished"}
                row = fresh
            return {"job_id": job_id, "cancel_state": "failed", "error": "cancel conflict retries exhausted"}
        except Exception as exc:
            logger.exception("cancel_job failed")
            return {"job_id": job_id, "cancel_state": "failed", "error": str(exc)}

    async def _save_cas(self, row: Dict[str, Any], expected_state: str) -> bool:
        """状态条件写（compare-and-set）——其他写者已推进时不覆盖。"""
        fn = getattr(self.store, "save_cas", None)
        if fn is None:
            await self.store.save(row)
            return True
        return bool(await fn(row, expected_state))

    async def _finalize(self, row: Dict[str, Any], new_state: str, *, exit_code: Optional[int] = None,
                        error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """终态结算（含 unknown→failed 的人工裁定路径）。产物扫描在成功/失败均执行。

        CAS 写入——与 cancel/并发 step 竞态时重读新鲜行重验迁移，
        绝不把已终态盖回 running、也不覆盖他人的 cancel 标记。
        """
        for _ in range(3):
            old = row.get("state")
            if not can_transition(old, new_state):
                logger.warning("job %s illegal transition %s->%s (ignored)", row.get("id"), old, new_state)
                return None
            now = datetime.utcnow().isoformat()
            row["state"] = new_state
            row["pid"] = None  # 终态后清 pid（JobStarted 事件里已存档；防后续误杀）
            if exit_code is not None:
                row["exit_code"] = int(exit_code)
            if error is not None:
                row["error"] = error
            row["finished_at"] = now
            row["result_digest"] = json.dumps(
                {"state": new_state, "exit_code": row.get("exit_code"), "error": row.get("error")},
                ensure_ascii=False)
            if await self._save_cas(row, old):
                break
            fresh = await self.store.get(row["id"])
            if fresh is None:
                return None
            if fresh.get("state") in JOB_TERMINAL_STATES or fresh.get("state") == JOB_STATE_UNKNOWN:
                return fresh  # 他人已结算
            row = fresh
        else:
            logger.warning("job %s finalize gave up after CAS retry", row.get("id"))
            return None
        event = {
            JOB_STATE_SUCCEEDED: "JobFinished",
            JOB_STATE_FAILED: "JobFailed",
            JOB_STATE_CANCELLED: "JobCancelReceipt",
            JOB_STATE_UNKNOWN: "JobOutcomeUnknown",
        }.get(new_state, "JobFinished")
        await self._event(row["id"], event, {"exit_code": row.get("exit_code"), "error": row.get("error"),
                                             "from": old})
        workdir = (row.get("spec") or {}).get("workdir") or str(self.job_dir(row["id"]) / "work")
        for art in await asyncio.to_thread(scan_artifacts, Path(workdir)):
            art["job_id"] = row["id"]
            await self.store.add_artifact(row["id"], art)
        return row

    async def _running_rows(self) -> List[Dict[str, Any]]:
        # 运行面很小（max_concurrent ≤8）：lease_next/list_jobs 已够用；此处按
        # list_jobs 全扫过滤 running（单实例 worker 轮询拓扑，够用且无额外 store 面）。
        out: List[Dict[str, Any]] = []
        for uid in {self._last_user_ids()}:
            for j in await self.store.list_jobs(uid, 200):
                if j.get("state") in (JOB_STATE_RUNNING, JOB_STATE_LEASED):
                    out.append(dict(j))
        return out

    def _last_user_ids(self) -> str:
        # list_jobs 需要 user 过滤；运行面扫描用哨兵 "*"（SqlJobStore 支持全扫）
        return "*"

    async def step(self) -> Optional[str]:
        """一个工作单元：收尸/超时 → 有容量则领新活启动。"""
        # phase A: 推进在途作业
        for row in await self._running_rows():
            d = self.job_dir(row["id"])
            ec = read_exit_code(d)
            if ec is not None:
                if row.get("cancel_requested_at"):
                    await self._finalize(row, JOB_STATE_CANCELLED, exit_code=ec,
                                         error="cancelled by request")
                elif ec == 0:
                    await self._finalize(row, JOB_STATE_SUCCEEDED, exit_code=ec)
                else:
                    await self._finalize(row, JOB_STATE_FAILED, exit_code=ec, error=f"exit {ec}")
                continue
            if row.get("pid") and not _pid_alive(row["pid"]):
                await self._finalize(row, JOB_STATE_UNKNOWN, error="process died without exit receipt")
                continue
            ttl = float(row.get("timeout_seconds") or 0)
            if ttl > 0 and row.get("started_at"):
                try:
                    started = datetime.fromisoformat(row["started_at"])
                    if (datetime.utcnow() - started).total_seconds() > ttl:
                        if row.get("pid"):
                            _kill_process_group(row["pid"])
                        await self._finalize(row, JOB_STATE_FAILED, error=f"timeout after {int(ttl)}s")
                        continue
                except (ValueError, TypeError):
                    pass
        # phase B: 容量内领新活
        running = [r for r in await self._running_rows() if r.get("state") == JOB_STATE_RUNNING]
        if len(running) >= self.max_concurrent:
            return None
        row = await self.store.lease_next(self.owner_id, datetime.utcnow(),
                                          config.agent_durable_jobs_lease_seconds)
        if row is None:
            return None
        await self._event(row["id"], "JobLeased", {"owner": self.owner_id})
        try:
            pid, log_path = await asyncio.to_thread(self._spawn_detached, row)
        except Exception as exc:
            row["state"] = JOB_STATE_FAILED
            row["error"] = f"spawn failed: {exc}"
            row["finished_at"] = datetime.utcnow().isoformat()
            await self.store.save(row)
            await self._event(row["id"], "JobFailed", {"error": str(exc)})
            return "spawn-failed"
        if not can_transition(row.get("state"), JOB_STATE_RUNNING):
            # lease_next 已把 queued→leased；此处仅防御
            logger.warning("job %s unexpected state %s at spawn", row["id"], row.get("state"))
        row["state"] = JOB_STATE_RUNNING
        row["pid"] = pid
        row["log_path"] = log_path
        row["started_at"] = datetime.utcnow().isoformat()
        row["attempts"] = int(row.get("attempts") or 0) + 1
        await self.store.save(row)
        await self._event(row["id"], "JobStarted", {"pid": pid})
        return "spawned"

    async def reconcile_on_startup(self) -> Dict[str, int]:
        """启动对账：exit_code 落盘→finalize；pid 活→接管；否则 unknown（显式凭据）。"""
        summary = {"finalized": 0, "adopted": 0, "unknown": 0}
        for row in await self._running_rows():
            d = self.job_dir(row["id"])
            ec = read_exit_code(d)
            if ec is not None:
                if row.get("cancel_requested_at"):
                    await self._finalize(row, JOB_STATE_CANCELLED, exit_code=ec, error="cancelled by request")
                elif ec == 0:
                    await self._finalize(row, JOB_STATE_SUCCEEDED, exit_code=ec)
                else:
                    await self._finalize(row, JOB_STATE_FAILED, exit_code=ec, error=f"exit {ec}")
                summary["finalized"] += 1
            elif row.get("pid") and _pid_alive(row["pid"]):
                await self._event(row["id"], "JobReconciled", {"pid": row["pid"], "adopted": True})
                summary["adopted"] += 1
            else:
                if can_transition(row.get("state"), JOB_STATE_UNKNOWN):
                    row["state"] = JOB_STATE_UNKNOWN
                    row["error"] = "outcome unknown after restart (no exit receipt)"
                    await self.store.save(row)
                    await self._event(row["id"], "JobOutcomeUnknown", {"pid": row.get("pid")})
                    summary["unknown"] += 1
        return summary


class SqlJobStore:
    """生产存储：durable_jobs / durable_job_events / durable_job_artifacts。"""

    async def get(self, job_id):
        from app.db.database import AsyncSessionLocal, DurableJob
        async with AsyncSessionLocal() as db:
            row = await db.get(DurableJob, job_id)
            return self._to_dict(row) if row else None

    async def get_by_key(self, key):
        if not key:
            return None
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal, DurableJob
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(DurableJob).where(DurableJob.idempotency_key == key))
            row = result.scalars().first()
            return self._to_dict(row) if row else None

    async def save(self, job):
        from app.db.database import AsyncSessionLocal, DurableJob
        async with AsyncSessionLocal() as db:
            row = await db.get(DurableJob, job["id"])
            fields = {k: v for k, v in job.items() if k != "spec"}
            spec = job.get("spec") or {}
            if row is None:
                _ins = {k: (_norm_dt(v) if k in _DT_FIELDS else v) for k, v in fields.items()}
                row = DurableJob(id=job["id"], spec=json.dumps(spec, ensure_ascii=False), **_ins)
                db.add(row)
            else:
                row.spec = json.dumps(spec, ensure_ascii=False)
                for k, v in fields.items():
                    setattr(row, k, _norm_dt(v) if k in _DT_FIELDS else v)
            await db.commit()

    async def save_cas(self, job, expected_state):
        """SELECT ... FOR UPDATE 事务内比较 state 后整体回写。"""
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal, DurableJob
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DurableJob).where(DurableJob.id == job["id"]).with_for_update())
            row = result.scalars().first()
            if row is None or row.state != expected_state:
                return False
            row.spec = json.dumps(job.get("spec") or {}, ensure_ascii=False)
            for k, v in {k: v for k, v in job.items() if k != "spec"}.items():
                setattr(row, k, _norm_dt(v) if k in _DT_FIELDS else v)
            await db.commit()
            return True

    async def next_seq(self, job_id):
        from sqlalchemy import func, select
        from app.db.database import AsyncSessionLocal, DurableJobEvent
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.coalesce(func.max(DurableJobEvent.seq), 0)).where(DurableJobEvent.job_id == job_id))
            return int(result.scalar_one()) + 1

    async def append_event(self, job_id, seq, event_type, payload):
        from sqlalchemy.exc import IntegrityError
        from app.db.database import AsyncSessionLocal, DurableJobEvent
        # max+1 非原子 → UNIQUE 撞车重取 seq 重试一次
        for attempt in range(2):
            use_seq = seq if attempt == 0 else await self.next_seq(job_id)
            try:
                async with AsyncSessionLocal() as db:
                    db.add(DurableJobEvent(job_id=job_id, seq=use_seq, event_type=event_type, payload=str(payload)))
                    await db.commit()
                return
            except IntegrityError:
                if attempt == 1:
                    raise

    async def list_events(self, job_id):
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal, DurableJobEvent
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DurableJobEvent).where(DurableJobEvent.job_id == job_id).order_by(DurableJobEvent.seq))
            return [{"seq": e.seq, "event_type": e.event_type, "payload": e.payload}
                    for e in result.scalars().all()]

    async def add_artifact(self, job_id, artifact):
        from app.db.database import AsyncSessionLocal, DurableJobArtifact
        async with AsyncSessionLocal() as db:
            db.add(DurableJobArtifact(job_id=job_id, path=artifact["path"],
                                      sha256=artifact["sha256"], bytes=artifact.get("bytes", 0)))
            await db.commit()

    async def list_artifacts(self, job_id):
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal, DurableJobArtifact
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(DurableJobArtifact).where(DurableJobArtifact.job_id == job_id))
            return [{"path": a.path, "sha256": a.sha256, "bytes": a.bytes}
                    for a in result.scalars().all()]

    async def list_jobs(self, user_id, limit):
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal, DurableJob
        async with AsyncSessionLocal() as db:
            stmt = select(DurableJob).order_by(DurableJob.created_at.desc()).limit(limit)
            if user_id != "*":
                stmt = stmt.where(DurableJob.user_id == user_id)
            result = await db.execute(stmt)
            return [self._to_dict(r) for r in result.scalars().all()]

    async def lease_next(self, owner, now, lease_seconds):
        from datetime import timedelta
        from sqlalchemy import select
        from app.db.database import AsyncSessionLocal, DurableJob
        async with AsyncSessionLocal() as db:
            # FOR UPDATE SKIP LOCKED 保证原子领取；lease_until 落库
            result = await db.execute(
                select(DurableJob).where(DurableJob.state == "queued")
                .order_by(DurableJob.created_at).limit(1)
                .with_for_update(skip_locked=True))
            row = result.scalars().first()
            if row is None:
                return None
            row.state = "leased"
            row.lease_owner = owner
            row.lease_until = now + timedelta(seconds=float(lease_seconds or 120))
            await db.commit()
            return self._to_dict(row)

    def _to_dict(self, row):
        spec = row.spec
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except Exception:
                spec = {}
        d = {
            "id": row.id, "user_id": row.user_id, "source": row.source, "spec": spec,
            "idempotency_key": row.idempotency_key, "state": row.state, "attempts": row.attempts,
            "lease_owner": row.lease_owner, "lease_until": row.lease_until, "pid": row.pid,
            "log_path": row.log_path, "exit_code": row.exit_code, "result_digest": row.result_digest,
            "cancel_requested_at": row.cancel_requested_at, "cancel_state": row.cancel_state,
            "error": row.error, "timeout_seconds": row.timeout_seconds,
            "created_at": row.created_at, "started_at": row.started_at, "finished_at": row.finished_at,
        }
        for k in ("created_at", "started_at", "finished_at", "lease_until", "cancel_requested_at"):
            v = d[k]
            d[k] = v.isoformat() if hasattr(v, "isoformat") else v
        return d


class JobRunnerWorker:
    """轮询循环（main 启停）：step() 周期执行 + 启动对账。"""

    def __init__(self, runner: Optional[JobRunner] = None):
        self.runner = runner  # 延迟到 start() 解析（避免 import 期建目录）
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if not config.agent_durable_jobs_enabled:
            logger.info("durable jobs disabled by config")
            return
        self.runner = self.runner or get_job_runner()
        try:
            summary = await self.runner.reconcile_on_startup()
            logger.info("durable job reconcile: %s", summary)
        except Exception:
            logger.exception("durable job reconcile failed (fail-open)")
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="durable-job-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = max(0.2, float(config.agent_durable_jobs_poll_interval))
        while not self._stopping.is_set():
            try:
                await self.runner.step()
            except Exception:
                logger.exception("durable job step failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


_runner_singleton: Optional[JobRunner] = None


def get_job_runner() -> JobRunner:
    global _runner_singleton
    if _runner_singleton is None:
        root = Path(config.agent_durable_jobs_root)
        if not root.is_absolute():
            root = Path(config.project_root) / root
        root.mkdir(parents=True, exist_ok=True)
        _runner_singleton = JobRunner(store=SqlJobStore(), jobs_root=str(root),
                                      owner_id=f"worker-{os.getpid()}")
    return _runner_singleton
