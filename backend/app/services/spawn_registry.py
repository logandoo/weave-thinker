# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""工具子进程登记与回收（F2）—— 登记 pgid+cmdline 签名，关停回收 + 启动清扫。

背景：terminal / code_execution 的后台子孙（``cmd &``）与 SIGKILL（stop.sh 强制
终止）后的会话子进程无人回收 → 孤儿常驻（2026-10-26 在 60 生产实测
``python3 -m http.server 8099``）。process 会话本身已有 killpg 回收，但优雅关停
被跳过（SIGKILL）或一拍即散的后台子孙不在任何回收面内。

机制：三处 spawn 登记 ``{pgid, cmdline, cwd, source, ts}`` 到运行时 manifest
（`backend/output_files/spawn_registry.json`，部署/备份均排除）。
- `sweep_orphans()`：启动时清扫上一代遗留（含 SIGKILL/崩溃场景）。
- `shutdown_all()`：优雅关停时回收仍在跑的登记组。
- **fail-safe（HOST-SAFETY，A4.9 C1 严格化后）**：只有「签名强匹配 + 进程起始
  时间不早于登记时刻」才杀；签名不匹配 / 短 token / pgid 疑似复用一律只 prune
  条目、绝不误杀；**永不登记/回收服务自身进程组**。

签名强匹配（`_signature_match`，MIN_SIG_LEN 地板）：
    归一化相等，或「较长串 ≥16 字符 且 较短串为其 token 级子串」。
    短 token（sh/python3/…）永不命中（A4.9 C1）。

信任边界（A4.9 I3 裁决）：沙箱与服务同 uid（部署形态即 root 单用户），共享浏览器
注册表对沙箱可写属同一信任域的既有事实；隔离（HOME/工作区）是卫生不是安全边界。
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_GRACE_SECONDS = 1.5
MIN_SIG_LEN = 16


def _manifest_path() -> Path:
    from app.core.config import get_config
    root = Path(getattr(get_config(), "backend_root", "."))
    return root / "output_files" / "spawn_registry.json"


def _proc_table() -> List[Tuple[int, int, str]]:
    """[(pid, pgid, cmdline)] —— Linux /proc 优先，其余平台走 ps。"""
    rows: List[Tuple[int, int, str]] = []
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
                rest = stat[stat.rfind(")") + 2:].split()
                pgid = int(rest[2])
            except Exception:
                continue
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
            except Exception:
                cmdline = ""
            rows.append((pid, pgid, cmdline))
        return rows
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,pgid=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
    except Exception:
        logger.debug("spawn_registry: proc table unavailable", exc_info=True)
    return rows


def _proc_start(pid: int) -> Optional[float]:
    """进程起始时刻（epoch，秒）；不可得返回 None（跳过起始时间防线）。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        starttime = int(stat[stat.rfind(")") + 2:].split()[19])
        btime = 0
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if not btime:
            return None
        return btime + starttime / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def _killpg(pgid: int, sig: int) -> None:
    os.killpg(pgid, sig)


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _signature_match(recorded: str, member_cmdline: str) -> bool:
    """A4.9 C1 严格签名：相等，或「被包含的那串 ≥MIN_SIG_LEN」的 token 级包含。
    地板加在**被搜索的子串**上 —— 短 token（sh/python3/…）永不命中，
    否则 pgid 复用即可诱导误杀。"""
    rec, mem = _norm(recorded), _norm(member_cmdline)
    if not rec or not mem:
        return False
    if rec == mem:
        return True
    if len(mem) >= MIN_SIG_LEN and mem in rec:
        return True
    if len(rec) >= MIN_SIG_LEN and rec in mem:
        return True
    return False


def _start_ok(pid: int, entry: dict) -> bool:
    """起始时间防线：进程早于登记时刻 = 不可能是本次 spawn 的后代（复用/伪造）。"""
    st = _proc_start(pid)
    if st is None:
        return True  # 平台不可得时退回签名判定（fail-open 仅此一层）
    try:
        ts = float(entry.get("ts") or 0)
    except Exception:
        ts = 0.0
    return st + 1.0 >= ts


def _entry_key(entry: dict) -> Tuple[int, object]:
    try:
        return (int(entry.get("pgid", -1)), entry.get("ts"))
    except Exception:
        return (-1, entry.get("ts"))


def _load_entries() -> List[dict]:
    try:
        data = json.loads(_manifest_path().read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        return [e for e in entries if isinstance(e, dict) and e.get("pgid") is not None]
    except Exception:
        return []


def _save_entries(entries: List[dict]) -> None:
    try:
        path = _manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.warning("spawn_registry: manifest write failed", exc_info=True)


def _leader_cmdline(pgid: int) -> str:
    for pid, g, cmd in _proc_table():
        if pid == pgid:
            return cmd
    return ""


def group_alive(pgid: int) -> bool:
    """进程组是否仍有成员（killpg(0) 探测）。"""
    try:
        os.killpg(pgid, 0)
        return True
    except Exception:
        return False


def register(pgid: int, cmdline: Optional[str], cwd: str, source: str) -> None:
    """登记一个工具子进程组。cmdline=None 时从 proc 表读 leader 实际 cmdline
    （调用方应传已知命令串——A4.9 I1：leader 可能已退出，别赌回读）。
    **拒绝登记服务自身进程组**（M6：start_new_session 回归时的灾难兜底）。"""
    try:
        pgid = int(pgid)
    except Exception:
        return
    try:
        if pgid == os.getpgid(0):
            logger.warning("spawn_registry: refusing to register own process group %s", pgid)
            return
    except Exception:
        pass
    rec = _norm(cmdline) if cmdline else _norm(_leader_cmdline(pgid))
    with _LOCK:
        entries = []
        for e in _load_entries():
            try:
                if int(e.get("pgid", -1)) == pgid:
                    continue
            except Exception:
                continue  # M3：坏条目不阻断登记
            entries.append(e)
        entries.append({
            "pgid": pgid,
            "cmdline": rec,
            "cwd": cwd or "",
            "source": source or "",
            "ts": time.time(),
        })
        _save_entries(entries)


def unregister(pgid: int) -> None:
    with _LOCK:
        entries = []
        for e in _load_entries():
            try:
                if int(e.get("pgid", -1)) == int(pgid):
                    continue
            except Exception:
                continue
            entries.append(e)
        _save_entries(entries)


def unregister_if_empty(pgid: int) -> None:
    """自然结束的一拍式任务：组内已无成员即销记（防 manifest 无界增长，I2）。"""
    if not group_alive(pgid):
        unregister(pgid)


async def _terminate_group(pgid: int) -> None:
    try:
        _killpg(pgid, signal.SIGTERM)
    except Exception:
        return
    await asyncio.sleep(_GRACE_SECONDS)
    try:
        _killpg(pgid, signal.SIGKILL)
    except Exception:
        pass


async def _resolve_and_clean() -> Dict[str, list]:
    """杀「强签名 + 起始时间」双过 的存活组；prune 其余条目（绝不误杀）。"""
    table = _proc_table()
    killed: List[int] = []
    pruned = 0
    with _LOCK:
        entries = _load_entries()
    processed = set()
    for e in entries:
        key = _entry_key(e)
        try:
            pgid = int(e["pgid"])
        except Exception:
            pruned += 1
            processed.add(key)
            continue
        members = [(pid, cmd) for (pid, g, cmd) in table if g == pgid]
        if not members:
            pruned += 1  # 死组：陈旧条目
            processed.add(key)
            continue
        matched = any(
            _signature_match(e.get("cmdline", ""), cmd) and _start_ok(pid, e)
            for (pid, cmd) in members
        )
        if matched:
            await _terminate_group(pgid)
            killed.append(pgid)
        else:
            # fail-safe：签名不匹配 / 短 token / 起始时间早于登记（pgid 疑似复用）
            # —— 不杀，只清条目（HOST-SAFETY，A4.9 C1）
            logger.warning(
                "spawn_registry: pgid %s signature/start-time mismatch — refusing to kill", pgid
            )
        pruned += 1
        processed.add(key)
    with _LOCK:
        # M2：清扫等待期间新登记的条目必须幸存（按 (pgid, ts) 身份过滤）
        fresh = [e for e in _load_entries() if _entry_key(e) not in processed]
        _save_entries(fresh)
    return {"killed": killed, "pruned": pruned}


async def sweep_orphans() -> Dict[str, list]:
    """启动清扫：上一代（含 SIGKILL/崩溃）遗留的登记组。"""
    res = await _resolve_and_clean()
    if res["killed"] or res["pruned"]:
        logger.info("spawn_registry sweep: killed=%s pruned=%s", res["killed"], res["pruned"])
    return res


async def shutdown_all() -> Dict[str, list]:
    """优雅关停回收：仍在跑的登记组（含本代后台子孙）。"""
    return await _resolve_and_clean()
