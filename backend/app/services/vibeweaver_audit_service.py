# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""vibeweaver 机械审计 + loop-guard — vibeweaver-audit.js / audit-core.js 的
Python 移植（P2 / A4-A5）。

Tier 1（机械，OK/BAD/UNCERTAIN）：
- 终局 claim↔产物交叉校验：最终输出带 ``[Verification Gate]`` 行时，逐条
  核对其字面声明与盘上证据（HARD-GATE 双 token、Tests executed with
  artifacts、assert_artifacts 声明、引用媒体文件非空、完成表头）。
- BAD → RED latch（``.vibeweaver/audit-state.json``，会话作用域 + TTL），
  封锁该会话后续非证据写入（tool.before 门），释放条件：换会话 / TTL 过期，
  每次释放记入 ``tests/gate_audit.md``（绝不静默丢弃）。

loop-guard（双通道，独立预算）：
- 文本通道：尾部行组重复（周期 p 由数据推导）、裸递增序列（整数 / 双射
  base-26 字母 ≥12 个）、无换行尾部的字符级周期重复（≥32 字符 ×4）。
- 工具通道：连续 ≥4 个 no-op bash（echo/printf 字面量 / true / :）——
  「宣告工具却只 echo」的意图-动作脱节。截断命令打断连续（截断可伪造字面量）。
- 触发 → 请求中断（AgentLoop 迭代边界注入纠正指令；进程内语义）+ 预算
  每会话每通道 ≤2 次，超出 log-only。``VIBEWEAVER_LOOPGUARD=off`` 关闭。

全部观察 fail-open：观察者任何异常不得影响执行。MULTI-WORKER：进程内，
RED latch 落盘兜底（同 hook_service 说明）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config
from app.services import hook_service
from app.services.agent_event_bus import AgentEvent, agent_event_bus

logger = logging.getLogger(__name__)

# ---- loop-guard 常量（对齐 audit.js）----
LOOP_SEQ_MIN = 12
LOOP_TAIL_SCAN = 98304
LOOP_MAX_INTERVENTIONS = 2
LOOP_CHAR_SEED = 32
LOOP_CHAR_MIN_PERIOD = 32
LOOP_CHAR_MAX_CANDIDATES = 16
LOOP_MAX_LINE_PERIOD = 640
NOOP_RUN_MIN = 4
_DELTA_CHECK_CHARS = 256

_BASH_META_RE = re.compile(r"[|&;<>`$(){}!\n\r]")
_DIGITS_RE = re.compile(r"\d+")
_LETTERS_RE = re.compile(r"[a-z]+")
_WORDY_RE = re.compile(r"[A-Za-z0-9]")
_GATE_TOKEN_RE = re.compile(r"HARD-GATE-[12]:\s*[A-Z0-9-]+=(?:pass|na)")
_MEDIA_CITE_RE = re.compile(r"tests/(\S+\.(?:png|mp4|webm|wav))")
_TABLE_HEADER = "| # |"

STATE_DIR = ".vibeweaver"
AUDIT_STATE_FILE = "audit-state.json"
AUDIT_JOURNAL = "gate_audit.md"

_installed = False


# ---------------- 观察状态 ----------------

@dataclass
class SessionObservation:
    session_id: str
    text_tail: str = ""
    since_check: int = 0
    tool_observations: List[Dict[str, Any]] = field(default_factory=list)
    interventions: Dict[str, int] = field(default_factory=dict)  # channel -> count
    last_finding: str = ""


_observations: Dict[str, SessionObservation] = {}
_active_runs: Dict[str, Any] = {}  # conversation_id -> AgentLoopState（中断用）
_MAX_OBSERVATIONS = 200


def _obs(session_id: str) -> SessionObservation:
    o = _observations.get(session_id)
    if o is None:
        if len(_observations) >= _MAX_OBSERVATIONS:
            for key in list(_observations.keys())[:50]:
                _observations.pop(key, None)
        o = SessionObservation(session_id=session_id)
        _observations[session_id] = o
    return o


def register_run(conversation_id: str, state: Any) -> None:
    if conversation_id:
        if len(_active_runs) >= _MAX_OBSERVATIONS and conversation_id not in _active_runs:
            for key in list(_active_runs.keys())[:50]:
                _active_runs.pop(key, None)
        _active_runs[conversation_id] = state


def unregister_run(conversation_id: str) -> None:
    _active_runs.pop(conversation_id or "", None)


# ---------------- 检测器（纯函数） ----------------

def _tokenize_bash_literal(cmd: str) -> Optional[List[str]]:
    tokens: List[str] = []
    cur = ""
    saw = False
    i = 0
    n = len(cmd)

    def push() -> None:
        nonlocal cur, saw
        if saw:
            tokens.append(cur)
            cur = ""
            saw = False

    while i < n:
        ch = cmd[i]
        if ch == "\\" and i + 1 < n:
            cur += cmd[i + 1]
            saw = True
            i += 2
            continue
        if ch == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                return None
            cur += cmd[i + 1:j]
            saw = True
            i = j + 1
            continue
        if ch == '"':
            j = cmd.find('"', i + 1)
            if j < 0:
                return None
            inner = cmd[i + 1:j]
            if re.search(r"[`$]", inner):
                return None
            cur += inner
            saw = True
            i = j + 1
            continue
        if ch == "#" and not saw:
            break
        if ch.isspace():
            push()
            i += 1
            continue
        if _BASH_META_RE.match(ch):
            return None
        cur += ch
        saw = True
        i += 1
    push()
    return tokens


def is_noop_bash_command(cmd: str) -> bool:
    """纯叙事/no-op 命令判定。保守：存疑一律视作真命令。"""
    if not isinstance(cmd, str):
        return False
    c = cmd.strip()
    if not c:
        return False
    if re.search(r"[\r\n]", c):
        return False
    c = re.sub(r";\s*$", "", c)
    if c in ("true", ":"):
        return True
    tokens = _tokenize_bash_literal(c)
    if not tokens:
        return False
    head = tokens[0].split("/")[-1]
    return head in ("echo", "printf")


def noop_bash_finding(tools: List[Dict[str, Any]], min_run: int = NOOP_RUN_MIN) -> Optional[Dict[str, str]]:
    """尾部连续 no-op bash 判定。同 pid 重观测去重；未知命令跳过；
    截断命令打断连续。"""
    if not tools:
        return None
    seen_pid = set()
    run = 0
    sample = ""
    for t in reversed(tools):
        if not isinstance(t, dict):
            break
        pid = t.get("pid")
        if pid is not None and pid in seen_pid:
            continue
        if t.get("tool") != "bash":
            break
        cmd = t.get("command") if isinstance(t.get("command"), str) else ""
        if not cmd.strip():
            continue
        if pid is not None:
            seen_pid.add(pid)
        if t.get("truncated") or not is_noop_bash_command(cmd):
            break
        run += 1
        if not sample:
            sample = cmd.strip()[:40]
        if run >= min_run:
            return {"kind": "noop-bash", "pattern": f"{run} consecutive no-op bash calls (e.g. {sample})"}
    return None


def _letters_to_index(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 96)
    return n


def _stepping_sequence(tail: str) -> Optional[Dict[str, str]]:
    tokens = [t for t in re.split(r"\s+", tail.strip()) if t]
    if len(tokens) < LOOP_SEQ_MIN:
        return None
    seq = tokens[-LOOP_SEQ_MIN:]
    if all(_DIGITS_RE.fullmatch(t) for t in seq):
        nums = [int(t) for t in seq]
        if all(nums[i + 1] - nums[i] == 1 for i in range(len(nums) - 1)):
            return {"kind": "sequence", "pattern": " ".join(seq)}
    if all(_LETTERS_RE.fullmatch(t) for t in seq):
        idxs = [_letters_to_index(t) for t in seq]
        if all(idxs[i + 1] - idxs[i] == 1 for i in range(len(idxs) - 1)):
            return {"kind": "sequence", "pattern": " ".join(seq)}
    return None


def _char_period_repeat(tail: str) -> Optional[Dict[str, str]]:
    min_p = LOOP_CHAR_MIN_PERIOD
    if len(tail) < min_p * 4:
        return None
    seed = tail[-LOOP_CHAR_SEED:]
    if not _WORDY_RE.search(seed):
        return None
    max_p = min(1024, len(tail) // 4)
    idx = len(tail) - LOOP_CHAR_SEED
    for _n in range(LOOP_CHAR_MAX_CANDIDATES):
        j = tail.rfind(seed, 0, idx)
        if j < 0:
            return None
        base = idx - j
        idx = j
        starts: List[int] = []
        if base >= min_p:
            starts.append(base)
        else:
            m = -(-min_p // base)  # ceil
            while m * base <= max_p and len(starts) < 4:
                starts.append(m * base)
                m += 1
        for p in starts:
            if p > max_p:
                continue
            last = tail[-p:]
            if tail[-2 * p:-p] != last or tail[-3 * p:-2 * p] != last or tail[-4 * p:-3 * p] != last:
                continue
            flat = last.strip()
            if len(flat) < 16 or not _WORDY_RE.search(flat):
                continue
            return {"kind": "repeat", "pattern": re.sub(r"\s+", " ", flat)[:80]}
    return None


def loop_finding(text: str) -> Optional[Dict[str, str]]:
    """文本退化纯检测（audit.js loopFinding 移植）。"""
    if not text or len(text) < 48:
        return None
    tail = text[-LOOP_TAIL_SCAN:]
    lines = tail.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # A) 尾部行组 ×3 周期重复（p 由数据推导）
    if len(lines) >= 3:
        last = lines[-1]
        p_max = min(LOOP_MAX_LINE_PERIOD, len(lines) // 3)
        for p in range(1, p_max + 1):
            if len(lines) - 1 - 2 * p < 0:
                break
            if lines[-1 - p] != last or lines[-1 - 2 * p] != last:
                continue
            ok = True
            for i in range(len(lines) - p, len(lines)):
                if lines[i] != lines[i - p] or lines[i] != lines[i - 2 * p]:
                    ok = False
                    break
            if not ok:
                continue
            group = lines[-p:]
            flat = "\n".join(group).strip()
            if len(flat) < 16:
                continue
            if p == 1:
                if not _WORDY_RE.search(last):
                    continue
                if len(lines) < 4 or lines[-4] != last:
                    continue
            return {"kind": "repeat", "pattern": re.sub(r"\s+", " ", flat)[:80]}
    # B) 裸递增序列
    seq = _stepping_sequence(tail)
    if seq:
        return seq
    # C) 无换行尾部字符级周期
    return _char_period_repeat(tail)


# ---------------- claim↔产物交叉校验（Tier 1） ----------------

def audit_final_output(content: str, root: Path) -> Dict[str, Any]:
    """终局声明核对。verdict: ok | bad | skip（无 [Verification Gate] = skip）。"""
    if not content or "[Verification Gate]" not in content:
        return {"verdict": "skip", "reasons": []}
    reasons: List[str] = []
    if "HARD-GATE-1" not in content or "HARD-GATE-2" not in content:
        reasons.append("claim check: [Verification Gate] 缺 HARD-GATE-1/HARD-GATE-2 字面 token")
    log = ""
    try:
        log = (root / "tests" / "verification_log.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    if re.search(r"Tests executed with artifacts:\s*yes", content):
        if not re.search(r"^- iter \d+ (PASS|FAIL):", log, re.M):
            reasons.append("claim check: 声明 Tests executed with artifacts: yes 但 tests/verification_log.md 无 iter 条目")
    if re.search(r"assert_artifacts\.py:\s*pass=\d+/fail=0", content):
        if not (root / "tests" / "assert_artifacts.py").exists():
            reasons.append("claim check: 声明 assert_artifacts.py: fail=0 但 tests/assert_artifacts.py 不在盘上")
    for m in _MEDIA_CITE_RE.finditer(content):
        p = root / "tests" / m.group(1)
        try:
            if p.stat().st_size <= 0:
                reasons.append(f"claim check: 引用媒体缺失/空 tests/{m.group(1)}")
        except OSError:
            reasons.append(f"claim check: 引用媒体缺失/空 tests/{m.group(1)}")
    if _TABLE_HEADER not in content:
        reasons.append("claim check: 声明 [Verification Gate] 但无 8 列完成表头（Class CODE 应有）")
    if reasons:
        return {"verdict": "bad", "reasons": reasons}
    return {"verdict": "ok", "reasons": []}


# ---------------- RED latch ----------------

def _audit_state_path(root: Path) -> Path:
    return root / STATE_DIR / AUDIT_STATE_FILE


def _load_audit_state(root: Path) -> Dict[str, Any]:
    p = _audit_state_path(root)
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_audit_state(root: Path, st: Dict[str, Any]) -> None:
    p = _audit_state_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as exc:
        logger.warning("audit state save failed: %s", exc)


def _journal(root: Path, line: str) -> None:
    p = root / "tests" / AUDIT_JOURNAL
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def latch_red(root: Path, session_id: str, reasons: List[str]) -> None:
    _save_audit_state(root, {
        "red": True,
        "session_id": session_id,
        "ts": time.time(),
        "ttl_hours": _red_ttl_hours(),
        "reasons": reasons[:10],
    })
    _journal(root, f"RED latch set (session={session_id}): " + "; ".join(reasons[:5]))


def _red_ttl_hours() -> float:
    try:
        return float(get_config().vibeweaver_red_ttl_hours)
    except Exception:
        return 24.0


def red_state(root: Path, session_id: str) -> Tuple[bool, List[str]]:
    """返回 (是否红灯封锁本次会话, 释放说明)。换会话/TTL 过期即自动释放。"""
    st = _load_audit_state(root)
    if not st.get("red"):
        return False, []
    reasons = list(st.get("reasons") or [])
    ts = float(st.get("ts") or 0)
    ttl = float(st.get("ttl_hours") or _red_ttl_hours())
    if time.time() - ts > ttl * 3600:
        _save_audit_state(root, {**st, "red": False, "released": "ttl-expired"})
        _journal(root, f"Stale RED release: ttl expired (was session={st.get('session_id')})")
        return False, []
    if (st.get("session_id") or "") != (session_id or ""):
        _save_audit_state(root, {**st, "red": False, "released": "session-changed"})
        _journal(root, f"Stale RED release: session changed (was session={st.get('session_id')}, now={session_id})")
        return False, []
    return True, reasons


# ---------------- 干预（loop-guard → AgentLoop） ----------------

def _intervention_text(finding: Dict[str, str], channel: str) -> str:
    if channel == "tools":
        return (
            f"[loop-guard] Your last tool calls degenerated into no-op echo/printf narration "
            f"({finding.get('pattern')}) — echoing intent is NOT a tool call. STOP the echo loop. "
            "Do exactly ONE of: (1) emit the REAL tool call your narration named as an actual tool "
            "invocation; or (2) only if every acceptance criterion is verified passing, emit the "
            "final completion output. BEFORE any completion output, append "
            "`- stall: noop-bash echo narration — <what you were trying to do>` to "
            "tests/verification_log.md and count it in your [Convergence] stalls "
            "(this interrupt IS the stall declaration)."
        )
    return (
        f"[loop-guard] Your output degenerated into meaningless repetition "
        f"({finding.get('kind')}: {finding.get('pattern')}). STOP repeating. Reply with "
        "(1) the task you are solving in ONE line, (2) the single next concrete step, then "
        "continue normally — no sequences, no filler."
    )


def request_interrupt(session_id: str, conversation_id: str, feedback: str, channel: str) -> bool:
    """请求中断当前 run（迭代边界注入纠正指令）。预算耗尽返回 False（log-only）。"""
    o = _obs(session_id)
    used = o.interventions.get(channel, 0)
    if used >= LOOP_MAX_INTERVENTIONS:
        logger.warning(
            "degenerate generation detected (%s) — intervention budget exhausted (%d/session/%s), log-only",
            o.last_finding, used, channel,
        )
        return False
    o.interventions[channel] = used + 1
    state = _active_runs.get(conversation_id or session_id)
    if state is None:
        logger.warning("loop-guard intervention requested but no active run (conv=%s)", conversation_id)
        return False
    try:
        state.loop_guard_feedback = feedback
        logger.info(
            "loop-guard intervention armed (%d/%d/%s) conv=%s",
            used + 1, LOOP_MAX_INTERVENTIONS, channel, conversation_id,
        )
        return True
    except Exception:
        return False


# ---------------- 事件观察入口 ----------------

def observe_text_delta(session_id: str, delta: str) -> Optional[Dict[str, str]]:
    if not delta:
        return None
    try:
        if not get_config().vibeweaver_loop_guard_enabled:
            return None
    except Exception:
        return None
    o = _obs(session_id)
    o.text_tail = (o.text_tail + delta)[-LOOP_TAIL_SCAN:]
    o.since_check += len(delta)
    if o.since_check < _DELTA_CHECK_CHARS:
        return None
    o.since_check = 0
    finding = loop_finding(o.text_tail)
    if finding:
        o.last_finding = f"{finding.get('kind')}:{finding.get('pattern')}"
        return finding
    return None


def observe_tool_call(session_id: str, tool_name: str, command: str, truncated: bool = False) -> Optional[Dict[str, str]]:
    """tool 通道观察（bash 等价物 = terminal 工具）。"""
    try:
        if not get_config().vibeweaver_loop_guard_enabled:
            return None
    except Exception:
        return None
    o = _obs(session_id)
    o.tool_observations.append({
        "tool": "bash" if tool_name == "terminal" else tool_name,
        "command": command if isinstance(command, str) else "",
        "truncated": bool(truncated),
    })
    if len(o.tool_observations) > 200:
        o.tool_observations = o.tool_observations[-200:]
    finding = noop_bash_finding(o.tool_observations)
    if finding:
        o.last_finding = finding.get("pattern", "")
    return finding


# ---------------- hook 安装 ----------------

async def _audit_tool_before(ctx: hook_service.ToolHookContext) -> Optional[hook_service.HookDecision]:
    """RED latch 封写（证据修复路径除外）。A4.9 I1：仅写类工具设门——
    终端/搜索/只读/脚本执行不封（修复路径需要它们跑 assert_artifacts）。"""
    if ctx.tool_name not in ("workspace_write", "workspace_edit", "git"):
        return None
    root = None
    from app.services.vibeweaver_gate_service import find_project_root
    root = find_project_root([ctx.workspace_path], bound=ctx.workspace_path)
    if root is None:
        return None
    from app.services.vibeweaver_gate_service import is_evidence_path
    file_path = (ctx.args or {}).get("file_path") or (ctx.args or {}).get("path")
    if is_evidence_path(root, file_path):
        return None
    red, reasons = red_state(root, ctx.conversation_id or ctx.session_id)
    if not red:
        return None
    msg = (
        "RED-LATCHED (vibeweaver-audit): the previous final audit was BAD and the session latch "
        "is still red — non-evidence writes are blocked until the evidence is fixed and a clean "
        "audit passes (writes under tests/ and memory/ are always allowed as the repair path). "
        "Reasons: " + "; ".join(reasons[:5])
    )
    return hook_service.HookDecision(block=msg)


async def _audit_tool_after(ctx: hook_service.ToolHookContext) -> Optional[hook_service.HookDecision]:
    """tool 通道观察（noop-bash）。观察键统一用 conversation_id（与
    message.delta 事件一致）。"""
    if ctx.tool_name != "terminal":
        return None
    command = str((ctx.args or {}).get("command") or "")
    obs_key = ctx.conversation_id or ctx.session_id
    finding = observe_tool_call(obs_key, "terminal", command)
    if finding:
        fb = _intervention_text(finding, "tools")
        request_interrupt(obs_key, ctx.conversation_id, fb, "tools")
    return None


async def _audit_stop_hook(ctx: hook_service.StopContext) -> Optional[hook_service.StopDecision]:
    """终局 claim 校验：BAD → RED latch（不拦本 stop，封锁后续写）。"""
    root = None
    from app.services.vibeweaver_gate_service import find_project_root
    root = find_project_root([ctx.workspace_path], bound=ctx.workspace_path)
    if root is None:
        return None
    verdict = audit_final_output(ctx.assistant_content, root)
    if verdict.get("verdict") == "bad":
        latch_red(root, ctx.conversation_id or ctx.session_id, verdict["reasons"])
        logger.warning("vibeweaver audit BAD (conv=%s): %s", ctx.conversation_id, verdict["reasons"][:3])
    return None  # audit 不拦 stop（latch 才是执法面）


async def _event_listener(event: AgentEvent) -> None:
    try:
        if event.type == "message.delta":
            finding = observe_text_delta(event.session_id, str(event.data.get("delta") or ""))
            if finding:
                fb = _intervention_text(finding, "text")
                request_interrupt(event.session_id, event.session_id, fb, "text")
    except Exception as exc:
        logger.debug("audit event listener failed-open: %s", exc)


def install_hooks() -> Dict[str, Any]:
    global _installed
    summary = {"audit_tool_hooks": 0, "audit_stop_hooks": 0, "event_listeners": 0}
    if _installed:
        return summary
    try:
        if not (get_config().vibeweaver_enabled and get_config().vibeweaver_audit_enabled):
            return summary
    except Exception:
        return summary
    hook_service.register_tool_hook("before", _audit_tool_before, name="vibeweaver_audit")
    hook_service.register_tool_hook("after", _audit_tool_after, name="vibeweaver_audit")
    hook_service.register_stop_hook(_audit_stop_hook, name="vibeweaver_audit")
    agent_event_bus.subscribe(_event_listener, name="vibeweaver_audit")
    _installed = True
    summary["audit_tool_hooks"] = 2
    summary["audit_stop_hooks"] = 1
    summary["event_listeners"] = 1
    logger.info("vibeweaver audit hooks installed")
    return summary
