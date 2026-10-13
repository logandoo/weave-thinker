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
from app.services import vibeweaver_state
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
# D1 盲区波（wave9-10 / T24l）：字符级 gate、counter 墙、per-turn 上限、标点墙
LOOP_CHAR_GATE_LINE = 256
LOOP_SHAPE_MIN = 16
LOOP_TURN_CEILING = 65536
LOOP_PUNCT_WALL_MIN = 2048

_BASH_META_RE = re.compile(r"[|&;<>`$(){}!\n\r]")
_DIGITS_RE = re.compile(r"\d+")
_LETTERS_RE = re.compile(r"[a-z]+")
# JS \p{L}\p{N} 的 Python 等价：unicode 字母/数字（不含下划线）
_ALNUM_U_RE = re.compile(r"[^\W_]", re.UNICODE)
# 尾部噪声归一（流式 message/part 拼接留下的 \n/空格/NBSP/零宽字符）
_WS_TAIL_RE = re.compile(r"[\s\u00a0\u200B-\u200D]+$")
# 纯标点后缀（JS [^\p{L}\p{N}]+$ 的等价：非字母数字，含下划线）
_TRAILING_PUNCT_RE = re.compile(r"[\W_]+$", re.UNICODE)
_GATE_TOKEN_RE = re.compile(r"HARD-GATE-[12]:\s*[A-Z0-9-]+=(?:pass|na)")
_MEDIA_CITE_RE = re.compile(r"tests/(\S+\.(?:png|mp4|webm|wav))")
_TABLE_HEADER = "| # |"

STATE_DIR = ".vibeweaver"
AUDIT_STATE_FILE = "audit-state.json"
AUDIT_JOURNAL = "gate_audit.md"

# D7/H14：C8 禁止裸命令（audit-core.js DEFAULTS.forbiddenRaw 同款；每 root 去重）
_FORBIDDEN_RAW_RES: Tuple[Any, ...] = (
    re.compile(r"(^|[;&|]\s*)npm run (build|dev|start|preview)\b"),
    re.compile(r"(^|[;&|]\s*)vite\b"),
    re.compile(r"(^|[;&|]\s*)npm start\b"),
    re.compile(r"(^|[;&|]\s*)uvicorn\b"),
    re.compile(r"\bpkill -f\b"),
)
C8_FLAGGED_CAP = 100


def is_forbidden_command(cmd: str) -> bool:
    """COV-2：裸命令（script/ 之外的构建/启动/杀进程）判定。"""
    if not isinstance(cmd, str):
        return False
    return any(re_.search(cmd) for re_ in _FORBIDDEN_RAW_RES)


def hash_command(cmd: str) -> str:
    """稳定短哈希（C8 去重键：同命令每 root 只记一次）。"""
    import hashlib
    return hashlib.sha1(str(cmd).encode("utf-8", "replace")).hexdigest()[:12]


def _c8_mark_fresh(root: Path, cmd: str) -> bool:
    """命中 → 记入 state.json `c8Fresh`（未在完成声明中入账的哈希；首次 → True）。

    与 `c8Flagged`（已入账）分离：完成声明第一次见到 fresh → BAD 并把它们移入
    flagged（自清可恢复——JS «已记录即不再标红» 同语义）。走共享锁读改写。
    """
    h = hash_command(cmd)
    flag = {"added": False}

    def _m(st: Dict[str, Any]) -> None:
        flagged = st.get("c8Flagged") if isinstance(st.get("c8Flagged"), list) else []
        fresh = st.get("c8Fresh") if isinstance(st.get("c8Fresh"), list) else []
        if h in flagged or h in fresh:
            return
        fresh.append(h)
        st["c8Fresh"] = fresh[-C8_FLAGGED_CAP:]
        flag["added"] = True

    try:
        vibeweaver_state.mutate_state(root, _m)
        return flag["added"]
    except Exception:
        return False


_installed = False


# ---------------- 观察状态 ----------------

@dataclass
class SessionObservation:
    session_id: str
    text_tail: str = ""
    reasoning_tail: str = ""
    since_check: Dict[str, int] = field(default_factory=dict)  # channel -> chars since last detector run
    tool_observations: List[Dict[str, Any]] = field(default_factory=list)
    interventions: Dict[str, int] = field(default_factory=dict)  # channel -> count
    last_finding: str = ""
    # D1：turn 上限 / message 边界重武装（JS noteMsg + turnChars + armed/fireMsgID）
    turn_chars: int = 0
    turn_ceiling_at: int = 0
    last_msg_id: str = ""
    fired_msg_ids: Dict[str, str] = field(default_factory=dict)
    armed: Dict[str, bool] = field(default_factory=dict)


_observations: Dict[str, SessionObservation] = {}
_active_runs: Dict[str, Any] = {}  # conversation_id -> AgentLoopState（中断用）
_MAX_OBSERVATIONS = 200
# W12 P1#7/#8：观察性跟踪（不阻断）——技能激活 / C17 / terminal 写
_skill_activations: Dict[str, str] = {}
_c17_warned: set = set()
_terminal_write_warned: set = set()
_TRACKING_CAP = 500
# 保守写模式（含项目路径/名才告警；避免全局噪音）
_TERMINAL_WRITE_RE = re.compile(r"(>>?|\btee\b|\bcp\b|\bmv\b|\bdd\b|\btruncate\b|\binstall\b)")


def _bounded_add(target: set, key: str) -> None:
    if len(target) >= _TRACKING_CAP and key not in target:
        for k in list(target)[:100]:
            target.discard(k)
    target.add(key)


def skill_activation_for(session_id: str) -> Optional[str]:
    return _skill_activations.get(session_id or "")


def _remember_skill_activation(session_id: str, name: str) -> None:
    if not session_id or not name:
        return
    if len(_skill_activations) >= _TRACKING_CAP and session_id not in _skill_activations:
        for k in list(_skill_activations.keys())[:100]:
            _skill_activations.pop(k, None)
    _skill_activations[session_id] = name


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


def _char_period_repeat(tail_in: str) -> Optional[Dict[str, str]]:
    """字符级后缀周期（无换行尾部）。D1 升级：尾噪归一 + 标点墙豁免。

    - 尾噪归一（D1.5）：流式/part 拼接的尾随空白（\\n/空格/NBSP/零宽字符）会让
      端锚种子成为自身唯一出现 → 先归一再来判（JS charPeriodRepeat 同款）。
    - 标点墙（D1.4）：纯标点后缀仅当墙 ≥ LOOP_PUNCT_WALL_MIN（按**墙自身**长度，
      不是 tail 窗口——大文档末尾短横线/横幅必须保持干净）才豁免字母数字门槛。
    """
    min_p = LOOP_CHAR_MIN_PERIOD
    tail = _WS_TAIL_RE.sub("", tail_in)
    if len(tail) < min_p * 4:
        return None
    seed = tail[-LOOP_CHAR_SEED:]
    punct_m = _TRAILING_PUNCT_RE.search(tail)
    punct_wall = bool(punct_m) and len(punct_m.group(0)) >= LOOP_PUNCT_WALL_MIN
    if not _ALNUM_U_RE.search(seed) and not punct_wall:
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
            if len(flat) < 16:
                continue
            if not _ALNUM_U_RE.search(flat) and not punct_wall:
                continue
            return {"kind": "repeat", "pattern": re.sub(r"\s+", " ", flat)[:80]}
    return None


def _counter_shape_wall(tokens: List[str]) -> Optional[Dict[str, str]]:
    """counter 墙（D1.2 / T24l）：尾 ≥16 行同 shape（数字段归一 #）且**每个**
    数字段相对上一行严格 +1。

    严格 +1 是 FP 关键：时间戳日志只有末段 tick、`page N of M` 的 M 恒定 →
    自然排除；短同形列表（<16）与精确重复（行路径职责）保持干净。
    """
    if len(tokens) < LOOP_SHAPE_MIN:
        return None
    shape = lambda l: _DIGITS_RE.sub("#", l)  # noqa: E731
    # 锚最后 3 行：墙后跟一行状态（"…×200\\nDone."）仍算墙（尾容差）
    for a in range(len(tokens) - 1, max(-1, len(tokens) - 4), -1):
        s_last = shape(tokens[a])
        if s_last == tokens[a]:
            continue
        n = 0
        prev: Optional[List[int]] = None
        for i in range(a, -1, -1):
            t = tokens[i]
            if shape(t) != s_last:
                break
            r = [int(x) for x in _DIGITS_RE.findall(t)]
            if prev is not None:
                if len(r) != len(prev):
                    break
                if not all(prev[k] - r[k] == 1 for k in range(len(r))):
                    break
            prev = r
            n += 1
        if n >= LOOP_SHAPE_MIN:
            return {"kind": "repeat", "pattern": s_last[:80] + f" ×{n} (counter run)"}
    return None


def loop_finding(text: str) -> Optional[Dict[str, str]]:
    """文本退化纯检测（audit.js loopFinding 移植；D1 全形态）。"""
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
                if not _ALNUM_U_RE.search(last):
                    continue
                if len(lines) < 4 or lines[-4] != last:
                    continue
            return {"kind": "repeat", "pattern": re.sub(r"\s+", " ", flat)[:80]}
    # B) 字符级周期（D1.6 gate256）：换行稀疏（近窗 <4 个换行）或最后内容行
    #    ≥256 字符才进字符级扫描——newline-rich 上下文里的中短行（表格行/minified
    #    行）不误报；行尾空白行不能遮蔽行内 runaway（读最后**内容**行）。
    recent = tail[-2048:]
    last_content_len = 0
    for ln in reversed(lines):
        if ln.strip():
            last_content_len = len(ln)
            break
    if recent.count("\n") < 4 or last_content_len >= LOOP_CHAR_GATE_LINE:
        hit = _char_period_repeat(tail)
        if hit:
            return hit
    # C) 裸递增序列
    seq = _stepping_sequence(tail)
    if seq:
        return seq
    # D) counter 墙（增量变化型结构重复，SpecRA 46% 类）
    tokens = [t.strip() for t in lines if t.strip()]
    return _counter_shape_wall(tokens)


# ---------------- claim↔产物交叉校验（Tier 1） ----------------

def _current_log_block(log_text: str) -> str:
    """当前任务块（最后 `## ` 块；B12 final-run 锚点的作用域）。"""
    if not log_text or not isinstance(log_text, str):
        return ""
    parts = re.split(r"^(?=## )", log_text, flags=re.MULTILINE)
    return parts[-1] if parts else log_text


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
    # D6/B12（wave17/audit-core.js:570-577）：完成声明必须携带 final-run 证据——
    # 当前任务块含 `- final-run: --final …` 行（物理门不可能替跑 --final，此日志行
    # 是「完成时机组 19 真的机检执行过」的唯一机器锚点）。
    if not re.search(r"-[ \t]*final-run:[ \t]*--final\b", _current_log_block(log), re.IGNORECASE):
        reasons.append(
            "claim check: B12 完成锚点缺失——当前任务块无 `- final-run: --final …` 行（组 19 未机检执行）"
        )
    # D7/H14 C8：未入账的禁止裸命令（COV-2 script-only）→ 首次完成声明 BAD；
    # 随即入账（自清可恢复——同 root 已记录不再标红，JS c8Flagged 去重同语义）。
    try:
        _c8 = {"n": 0}

        def _c8_m(st: Dict[str, Any]) -> None:
            _fresh = st.get("c8Fresh") if isinstance(st.get("c8Fresh"), list) else []
            if not _fresh:
                return
            _c8["n"] = len(_fresh)
            _flagged = st.get("c8Flagged") if isinstance(st.get("c8Flagged"), list) else []
            st["c8Flagged"] = (_flagged + [h for h in _fresh if h not in _flagged])[-C8_FLAGGED_CAP:]
            st["c8Fresh"] = []

        vibeweaver_state.mutate_state(root, _c8_m)
        if _c8["n"]:
            reasons.append(
                f"claim check: C8 禁止裸命令（COV-2 script-only）——{_c8['n']} 条原始命令未入账；"
                "改走 script/ 包装后重试（命令本轮已入账，二次声明不再标红）"
            )
    except Exception:
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
    if finding.get("kind") == "runaway":
        return (
            f"[loop-guard] RUNAWAY OUTPUT: {finding.get('pattern')} without a verified stop "
            "condition (the per-turn ceiling is the absolute backstop bound). STOP emitting. "
            "Do exactly ONE of: (1) if every acceptance criterion is verified passing, emit the "
            "final completion output ([Verification Gate] + completion table) now — the evidence, "
            "not a re-narration; or (2) if work remains, emit ONE short status line naming the "
            "single next concrete step and continue with bounded output. Never re-narrate "
            "completed work."
        )
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


def _guard_root(session_id: str) -> Optional[Path]:
    """遥测根解析（注册表命中才落盘；未知 = 跳过，遥测绝不外溢）。"""
    try:
        from app.services.vibeweaver_gate_service import recent_project_root
        return recent_project_root(session_id or "")
    except Exception:
        return None


def _note_msg(o: SessionObservation, msg_id: str) -> None:
    """message 边界 = 新 turn：turnChars/turnCeilingAt 归零（JS noteMsg）。"""
    if isinstance(msg_id, str) and msg_id and o.last_msg_id != msg_id:
        o.turn_chars = 0
        o.turn_ceiling_at = 0
        o.last_msg_id = msg_id


def _guard_check(
    o: SessionObservation, channel: str, finding: Optional[Dict[str, str]], session_id: str = ""
) -> Optional[Dict[str, str]]:
    """armed/ceiling/re-arm 语义（JS maybeGuardCheck；预算在 request_interrupt 消费）。

    - 干净观察 → 重武装；新 message 边界（fireMsgID≠lastMsgID）→ 重武装（新 episode）。
    - runaway（per-turn 上限）绕过同 turn no-thrash，且可救援已闩锁的有机 finding
      （增长窗再次越过 64KB）。
    - 闩锁期间的重复命中直接抑制（no-thrash），不重复请求干预。
    """
    def _ceiling_finding() -> Dict[str, str]:
        return {
            "kind": "runaway",
            "pattern": f"turn output exceeded {LOOP_TURN_CEILING} bytes",
        }

    if (
        finding is None
        and channel != "tools"
        and o.turn_chars > LOOP_TURN_CEILING
        and o.turn_chars >= o.turn_ceiling_at + LOOP_TURN_CEILING
    ):
        # 每个 64KB 增长窗合成一次（blocked/预算吞掉不烧窗）
        o.turn_ceiling_at = o.turn_chars
        finding = _ceiling_finding()
    armed = o.armed.get(channel, True)
    if finding is None:
        if not armed:
            o.armed[channel] = True
        return None
    if not armed:
        fire_id = o.fired_msg_ids.get(channel)
        new_turn = bool(fire_id) and bool(o.last_msg_id) and o.last_msg_id != fire_id
        if not new_turn and finding.get("kind") != "runaway":
            if channel != "tools" and o.turn_chars >= o.turn_ceiling_at + LOOP_TURN_CEILING:
                o.turn_ceiling_at = o.turn_chars
                finding = _ceiling_finding()
            else:
                return None
    o.last_finding = f"{finding.get('kind')}:{finding.get('pattern')}"
    return finding


def request_interrupt(session_id: str, conversation_id: str, feedback: str, channel: str) -> bool:
    """请求中断当前 run（迭代边界注入纠正指令）。预算耗尽返回 False（log-only）。"""
    o = _obs(session_id)
    # episode 闩锁（JS firedKey/armed）：触发即失能——同一 episode 不再重复干预；
    # 干净观察或新 message 边界重武装（_guard_check / _note_msg）。
    o.armed[channel] = False
    o.fired_msg_ids[channel] = o.last_msg_id
    state = _active_runs.get(conversation_id or session_id)
    if state is None:
        # 无活跃 run（会话键缺失/已收尾）→ 不消费预算、不记干预
        # （JS 同语义：onDegenerate 不存在 = 守卫不激活，无幻影计数）。
        logger.warning("loop-guard intervention requested but no active run (conv=%s)", conversation_id)
        return False
    used = o.interventions.get(channel, 0)
    root = _guard_root(conversation_id or session_id)
    if used >= LOOP_MAX_INTERVENTIONS:
        logger.warning(
            "degenerate generation detected (%s) — intervention budget exhausted (%d/session/%s), log-only",
            o.last_finding, used, channel,
        )
        vibeweaver_state.record_loop_guard_event(root, channel, suppressed=True)
        return False
    o.interventions[channel] = used + 1
    try:
        state.loop_guard_feedback = feedback
        logger.info(
            "loop-guard intervention armed (%d/%d/%s) conv=%s",
            used + 1, LOOP_MAX_INTERVENTIONS, channel, conversation_id,
        )
        kind, _, pattern = (o.last_finding or "").partition(":")
        vibeweaver_state.record_loop_guard_event(root, channel, kind=kind, pattern=pattern)
        return True
    except Exception:
        return False


# ---------------- 事件观察入口 ----------------

def observe_text_delta(
    session_id: str, delta: str, channel: str = "content", msg_id: str = ""
) -> Optional[Dict[str, str]]:
    """文本增量观察（D1.1 双通道：content / reasoning 独立缓冲与预算）。

    channel: "content"（默认，兼容旧调用）/ "reasoning"。msg_id 变化 = 新 turn
    （turnChars 清零 + 新 episode 重武装来源）。
    """
    if not delta:
        return None
    try:
        if not get_config().vibeweaver_loop_guard_enabled:
            return None
    except Exception:
        return None
    o = _obs(session_id)
    _note_msg(o, msg_id)
    o.turn_chars += len(delta)
    key = "reasoning" if channel == "reasoning" else "text"
    if key == "reasoning":
        o.reasoning_tail = (o.reasoning_tail + delta)[-LOOP_TAIL_SCAN:]
        tail = o.reasoning_tail
    else:
        o.text_tail = (o.text_tail + delta)[-LOOP_TAIL_SCAN:]
        tail = o.text_tail
    o.since_check[key] = o.since_check.get(key, 0) + len(delta)
    if o.since_check[key] < _DELTA_CHECK_CHARS:
        return None
    o.since_check[key] = 0
    return _guard_check(o, key, loop_finding(tail), session_id)


def observe_tool_call(
    session_id: str, tool_name: str, command: str, truncated: bool = False, msg_id: str = ""
) -> Optional[Dict[str, str]]:
    """tool 通道观察（bash 等价物 = terminal 工具）。msg_id 变化 = 新 turn
    （JS noteMsg 同语义：工具通道也可在新消息边界重武装）。"""
    try:
        if not get_config().vibeweaver_loop_guard_enabled:
            return None
    except Exception:
        return None
    o = _obs(session_id)
    _note_msg(o, msg_id)
    o.tool_observations.append({
        "tool": "bash" if tool_name == "terminal" else tool_name,
        "command": command if isinstance(command, str) else "",
        "truncated": bool(truncated),
    })
    if len(o.tool_observations) > 200:
        o.tool_observations = o.tool_observations[-200:]
    return _guard_check(o, "tools", noop_bash_finding(o.tool_observations), session_id)


# ---------------- hook 安装 ----------------

async def _audit_tool_before(ctx: hook_service.ToolHookContext) -> Optional[hook_service.HookDecision]:
    """RED latch 封写（证据修复路径除外）。A4.9 I1：仅写类工具设门——
    终端/搜索/只读/脚本执行不封（修复路径需要它们跑 assert_artifacts）。"""
    if not ctx.vibeweaver_active:
        return None  # W17 轮次门控：非激活轮不封写（RED latch 对新轮不生效）
    if ctx.tool_name not in ("workspace_write", "workspace_edit", "git"):
        return None
    from app.services.vibeweaver_gate_service import resolve_session_root
    root = resolve_session_root(
        ctx.workspace_path,
        ctx.conversation_id or ctx.session_id,
        starts=[(ctx.args or {}).get("file_path"), (ctx.args or {}).get("path")],
        prefer_hints=True,
    )
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
    # D7/H14：C8 禁止裸命令（note 不拦截 + 每 root 首见去重；终局 claim 面 BAD）。
    # W17 stand-down：仅激活轮记录（非激活轮的裸命令不得在后续激活轮的 claim 上
    # 伪造 C8 BAD——评审 A-I2）。loop-guard 观察不 stand-down（纯观察面）。
    try:
        if ctx.vibeweaver_active and get_config().vibeweaver_c8_enabled and is_forbidden_command(command):
            from app.services.vibeweaver_gate_service import find_project_root, recent_project_root
            _c8_root = recent_project_root(obs_key)
            if _c8_root is None and ctx.workspace_path:
                _c8_root = find_project_root([ctx.workspace_path], bound=ctx.workspace_path)
            if _c8_root is not None and _c8_mark_fresh(_c8_root, command):
                logger.warning(
                    "vibeweaver C8: forbidden raw command (COV-2 script-only) conv=%s cmd=%s",
                    obs_key, command[:100],
                )
                return hook_service.HookDecision(notes=[
                    f"[GATE-WARNING (vibeweaver-c8)] COV-2 script-only lifecycle: forbidden raw "
                    f"command `{command[:80]}` — use the project's script/ wrappers "
                    f"(project_build.sh/start.sh/stop.sh); a completion claim counts it BAD until "
                    f"the command is recorded (it now is — fix the evidence flow and re-claim)."
                ])
    except Exception:
        pass
    finding = observe_tool_call(obs_key, "terminal", command, msg_id=ctx.turn_key or "")
    if finding:
        fb = _intervention_text(finding, "tools")
        request_interrupt(obs_key, ctx.conversation_id, fb, "tools")
    # W12 P1#8：terminal 写观察（设计边界：bash 写不设门）——保守模式且会话有
    # active 项目且命令提及项目路径/名 → 每会话一次告警日志（不阻断、不影响结果）。
    try:
        from app.services.vibeweaver_gate_service import find_project_root, recent_project_root
        root = recent_project_root(obs_key)
        if root is None and ctx.workspace_path:
            # 兜底：工作区根本身即 active 项目（terminal 首次触达场景）
            root = find_project_root([ctx.workspace_path], bound=ctx.workspace_path)
        if root is not None and obs_key not in _terminal_write_warned:
            if _TERMINAL_WRITE_RE.search(command) and (
                root.as_posix() in command or f"{root.name}/" in command
            ):
                _bounded_add(_terminal_write_warned, obs_key)
                logger.info(
                    "terminal write into vibeweaver-active project bypasses the gate "
                    "(documented boundary; prefer workspace_write) conv=%s cmd=%s",
                    obs_key, command[:80],
                )
    except Exception:
        pass
    return None


async def _audit_stop_hook(ctx: hook_service.StopContext) -> Optional[hook_service.StopDecision]:
    """终局 claim 校验：BAD → RED latch（不拦本 stop，封锁后续写）。"""
    if not ctx.vibeweaver_active:
        return None  # W17 轮次门控：非激活轮不做 claim 校验（不产新 latch）
    from app.services.vibeweaver_gate_service import resolve_session_root, recent_project_root
    key = ctx.conversation_id or ctx.session_id
    root = resolve_session_root(ctx.workspace_path, key)
    if root is None:
        return None
    # W12 P1#7：C17 观察——vibeweaver-active 项目被写（注册表有记录）但技能未激活。
    if (
        recent_project_root(key) is not None
        and skill_activation_for(key) != "vibeweaver"
        and key not in _c17_warned
    ):
        _bounded_add(_c17_warned, key)
        logger.warning(
            "vibeweaver-active project touched without loading the skill (C17) conv=%s root=%s",
            key, root,
        )
    verdict = audit_final_output(ctx.assistant_content, root)
    if verdict.get("verdict") == "bad":
        latch_red(root, ctx.conversation_id or ctx.session_id, verdict["reasons"])
        logger.warning("vibeweaver audit BAD (conv=%s): %s", ctx.conversation_id, verdict["reasons"][:3])
    return None  # audit 不拦 stop（latch 才是执法面）


async def _event_listener(event: AgentEvent) -> None:
    try:
        # W12 P1#7：技能激活信号（skill_view 发布）——C17 判定与审计会话筛选信号。
        if event.type == "skill.activated":
            name = str((getattr(event, "data", None) or {}).get("name") or "")
            _remember_skill_activation(str(getattr(event, "session_id", "") or ""), name)
            return
        if event.type == "message.delta":
            data = event.data if isinstance(event.data, dict) else {}
            raw_channel = str(data.get("channel") or "content")
            finding = observe_text_delta(
                event.session_id,
                str(data.get("delta") or ""),
                channel=raw_channel,
                msg_id=str(data.get("msg_id") or ""),
            )
            if finding:
                ch = "reasoning" if raw_channel == "reasoning" else "text"
                fb = _intervention_text(finding, ch)
                request_interrupt(event.session_id, event.session_id, fb, ch)
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
