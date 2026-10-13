# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""vibeweaver 落盘状态共享助手 — `.vibeweaver/` 下 JSON 账本的原子读改写。

D8（H13）遥测面 + D2 stall latch + W3 投递计数共用：
- `state.json`：gate 的 ops/stall、audit 的 loopGuard 计数、delivery 的
  memoryDeliveries —— **保留未知字段**（wave21 教训：禁止整体重置；opencode
  宿主插件与产品共用同一文件形态）。
- `cue-stats.json`：cue triggers 命中计数（W3/D5）。

规则：全部 fail-open（遥测/计数失败绝不影响执法与生成）、有界（计数对象带
cap）、原子写（tmp + os.replace）。进程内加锁防同进程并发读改写丢更新；
多 worker 边界沿用既有「原子写 + 有界」约定（方案 §九）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATE_DIR = ".vibeweaver"
STATE_FILE = "state.json"
CUE_STATS_FILE = "cue-stats.json"
STOP_DEBT_FILE = "stop-debt.json"

CUE_STATS_CAP = 100
LOOP_GUARD_PATTERN_CAP = 80
STOP_DEBT_CAP = 5

_lock = threading.Lock()


def state_path(root: Path) -> Path:
    return root / STATE_DIR / STATE_FILE


def cue_stats_path(root: Path) -> Path:
    return root / STATE_DIR / CUE_STATS_FILE


def load_state(root: Path) -> Dict[str, Any]:
    """读取 state.json —— 非 dict / 损坏 → {}（绝不抛）。"""
    try:
        raw = state_path(root).read_text(encoding="utf-8")
        st = json.loads(raw)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(root: Path, st: Dict[str, Any]) -> None:
    """原子写 state.json（fail-open）。"""
    p = state_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        logger.debug("vibeweaver state save failed-open: %s", exc)


def _mutate(root: Path, mutate) -> None:
    """加锁读改写（保留未知字段：mutate 就地修改读到的 dict）。

    无变化跳写（评审 r1-A #1）：空回调不得物化/重写状态文件（未知字段保留，
    避免只读路径产生遥测副作用）。
    """
    try:
        with _lock:
            st = load_state(root)
            before = json.dumps(st, ensure_ascii=False, sort_keys=True)
            mutate(st)
            if json.dumps(st, ensure_ascii=False, sort_keys=True) == before:
                return
            save_state(root, st)
    except Exception as exc:  # 遥测绝不外溢
        logger.debug("vibeweaver state mutate failed-open: %s", exc)


def mutate_state(root: Path, mutate) -> None:
    """公开的加锁读改写入口（audit 的 c8 账本等调用方共用同一把锁，
    防同进程 last-writer-wins 丢更新）。"""
    _mutate(root, mutate)


def record_memory_delivery(root: Path, kind: str) -> None:
    """投递计数：compactContext / compactResume / sessionCue（有界键集）。"""
    if not root or kind not in ("compactContext", "compactResume", "sessionCue"):
        return

    def _m(st: Dict[str, Any]) -> None:
        md = st.get("memoryDeliveries")
        if not isinstance(md, dict):
            md = {}
        md[kind] = int(md.get(kind) or 0) + 1
        st["memoryDeliveries"] = md

    _mutate(root, _m)


def record_loop_guard_event(
    root: Path,
    channel: str,
    kind: str = "",
    pattern: str = "",
    suppressed: bool = False,
) -> None:
    """loop-guard 遥测（有界）：每通道干预计数 + 抑制计数 + 最近一次命中。"""
    if not root or channel not in ("text", "reasoning", "tools"):
        return

    def _m(st: Dict[str, Any]) -> None:
        lg = st.get("loopGuard")
        if not isinstance(lg, dict):
            lg = {}
        counts = lg.get("interventions")
        if not isinstance(counts, dict):
            counts = {}
        if suppressed:
            lg["suppressed"] = int(lg.get("suppressed") or 0) + 1
        else:
            counts[channel] = int(counts.get(channel) or 0) + 1
            lg["last"] = {
                "channel": channel,
                "kind": str(kind or "")[:32],
                "pattern": str(pattern or "")[:LOOP_GUARD_PATTERN_CAP],
                "ts": int(time.time() * 1000),
            }
        lg["interventions"] = counts
        st["loopGuard"] = lg

    _mutate(root, _m)


def record_stop_guard_event(
    root: Path, *, prompted: bool, kind: str = "", match: str = ""
) -> None:
    """stop-guard 遥测（D8）：干预计数 / debt 计数 / 最近命中。"""
    if not root:
        return

    def _m(st: Dict[str, Any]) -> None:
        sg = st.get("stopGuard")
        if not isinstance(sg, dict):
            sg = {}
        key = "interventions" if prompted else "debt"
        sg[key] = int(sg.get(key) or 0) + 1
        sg["last"] = {
            "kind": str(kind or "")[:32],
            "match": str(match or "")[:80],
            "ts": int(time.time() * 1000),
        }
        st["stopGuard"] = sg

    _mutate(root, _m)


def load_stop_debt(root: Path) -> List[Dict[str, Any]]:
    try:
        raw = json.loads((root / STATE_DIR / STOP_DEBT_FILE).read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [d for d in raw if isinstance(d, dict)]
    except (OSError, ValueError):
        pass
    return []


def append_stop_debt(
    root: Path, session_id: str, kind: str, match: str, why: str
) -> None:
    """§V12 abandonment debt（advisory，cap5，同 episode 去重；绝不阻断）。"""
    if not root:
        return
    try:
        with _lock:
            debts = load_stop_debt(root)
            rec = {
                "ts": int(time.time() * 1000),
                "sessionID": str(session_id or "")[:80],
                "kind": str(kind or "")[:32],
                "match": str(match or "")[:80],
                "why": str(why or "")[:80],
            }
            last = debts[-1] if debts else None
            if (
                last
                and last.get("kind") == rec["kind"]
                and last.get("match") == rec["match"]
                and last.get("why") == rec["why"]
            ):
                return  # 同 episode 去重
            debts.append(rec)
            debts = debts[-STOP_DEBT_CAP:]
            p = root / STATE_DIR / STOP_DEBT_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(debts, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
    except OSError as exc:
        logger.debug("stop-debt save failed-open: %s", exc)


def record_cue_fires(root: Path, topic_files: List[str]) -> None:
    """cue 命中计数（每主题 +1，有界 cap）。"""
    if not root or not topic_files:
        return
    p = cue_stats_path(root)
    try:
        with _lock:
            stats: Dict[str, Any] = {}
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    stats = raw
            except (OSError, ValueError):
                stats = {}
            for f in topic_files:
                key = Path(str(f)).name[:200]
                stats[key] = int(stats.get(key) or 0) + 1
            if len(stats) > CUE_STATS_CAP:
                stats = dict(list(stats.items())[-CUE_STATS_CAP:])
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
    except OSError as exc:
        logger.debug("cue stats save failed-open: %s", exc)
