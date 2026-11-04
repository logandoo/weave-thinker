# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""循环检测纯函数库 — Governor v2 共享底座（设计 §4.4「一份实现，两处武装」）。

从 `vibeweaver_audit_service.py` 逐字迁移（2026-10-25 P1/T6，行为零变化由
`test_vibeweaver_audit.py` 既有断言机检），并新增 D1 **rotation-invariant
token-cycle** 检测器（vllm-mlx PR #723 参数蓝本：period 1–128、≥3 连续完整
副本、跨度 ≥48 token、512 token 窗口、从尾部比对 ⇒ 周期进行中任意位置可触发）。
专治字符级/行级周期规则的结构性盲区：**多行循环**（换行是 DRY/字符周期
的 canonical breaker）与**尾部截断在周期中间**的流式形态。

调用方：`vibeweaver_audit_service.text_degeneration_finding`（Tier A/B 共享的
观察入口——`loop_governor_service.watch_stream` 经它武装全流量）与其兼容
re-export。纯函数、无状态、无 I/O——任何异常都不得来自本模块的副作用（本就
没有）。

口径限定（语法级承诺，设计 D-20261025-loopsplit2）：`token_cycle_finding`
按**空白分词**（vllm-mlx 用真 BPE token 流的文本近似）。无空格连写文本的
精确周期由规则 A（行组）/B（字符周期，含周期整数倍种子）兜底，本检测器
主要覆盖带空格语言的 token 周期；语义改写型绕圈是 D3（P2）的靶面。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ---- loop-guard 常量（对齐 audit.js；迁自 vibeweaver_audit_service）----
LOOP_SEQ_MIN = 12
LOOP_TAIL_SCAN = 98304
LOOP_CHAR_SEED = 32
LOOP_CHAR_MIN_PERIOD = 32
LOOP_CHAR_MAX_CANDIDATES = 16
LOOP_MAX_LINE_PERIOD = 640
NOOP_RUN_MIN = 4
# D1 盲区波（wave9-10 / T24l）：字符级 gate、counter 墙、标点墙
LOOP_CHAR_GATE_LINE = 256
LOOP_SHAPE_MIN = 16
LOOP_PUNCT_WALL_MIN = 2048
# D1 token-cycle（vllm-mlx 蓝本）
CYCLE_PERIOD_MAX = 128
CYCLE_MIN_COPIES = 3
CYCLE_MIN_SPAN_TOKENS = 48
CYCLE_WINDOW_TOKENS = 512

_BASH_META_RE = re.compile(r"[|&;<>`$(){}!\n\r]")
_DIGITS_RE = re.compile(r"\d+")
_LETTERS_RE = re.compile(r"[a-z]+")
# JS \p{L}\p{N} 的 Python 等价：unicode 字母/数字（不含下划线）
_ALNUM_U_RE = re.compile(r"[^\W_]", re.UNICODE)
# 尾部噪声归一（流式 message/part 拼接留下的 \n/空格/NBSP/零宽字符）
_WS_TAIL_RE = re.compile(r"[\s\u00a0\u200B-\u200D]+$")
# 纯标点后缀（JS [^\p{L}\p{N}]+$ 的等价：非字母数字，含下划线）
_TRAILING_PUNCT_RE = re.compile(r"[\W_]+$", re.UNICODE)


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


def token_cycle_finding(
    text: str,
    *,
    period_max: int = CYCLE_PERIOD_MAX,
    min_copies: int = CYCLE_MIN_COPIES,
    min_span_tokens: int = CYCLE_MIN_SPAN_TOKENS,
    window_tokens: int = CYCLE_WINDOW_TOKENS,
) -> Optional[Dict[str, str]]:
    """D1 扩展：rotation-invariant token 周期检测（vllm-mlx 蓝本）。

    对尾部 ≤``window_tokens`` 个空白分词，找周期 p≤``period_max`` 使尾部含
    ≥``min_copies`` 个**连续完整副本**且总跨度 ≥``min_span_tokens`` token。
    从尾部比对 + 容忍最后一个 token 为流式截断的词前缀（offset∈{0,1} 取长），
    因此**周期进行中任意位置可触发**（含第 N+1 副本只吐了一半的形态）——
    这是行组规则 A（要求尾部恰为整副本）与字符级规则 B（newline 断裂）的
    联合盲区。返回 kind="cycle"。
    """
    if not text:
        return None
    toks = text.split()
    if len(toks) > window_tokens:
        toks = toks[-window_tokens:]
    n = len(toks)
    if n < min_span_tokens:
        return None
    for p in range(1, min(period_max, n // min_copies) + 1):

        def _run(start: int) -> int:
            m = 0
            while start - m - p >= 0 and toks[start - m] == toks[start - m - p]:
                m += 1
            return m

        # offset=1：末 token 视作可能的流式词前缀，跳过它再比
        m = max(_run(n - 1), 1 + _run(n - 2) if n >= 2 else 0)
        copies = m // p + 1
        if copies >= min_copies and (m + p) >= min_span_tokens:
            block = " ".join(toks[-p:])
            return {"kind": "cycle", "pattern": (block[:80] + f" ×{copies}")[:160]}
    return None
