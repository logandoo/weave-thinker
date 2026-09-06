# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""NPG — Numeric Provenance Gate (数值溯源闸门, 2026-09-02, conv 83d97ede).

Incident: conv 83d97ede (strawberry plug-tray frustum volume) — the writer
model (deepseek-v4-flash) answered ≈40000mm³=40ml by pure mental arithmetic
(tool_calls=none); the true value is π·(110/3)·(32.5²+32.5·12.5+12.5²)
≈ 186466.6mm³ ≈ 186.5ml (4.66× off, advisory conclusion inverted). The
pre-send audit could not catch it BY DESIGN: its template forbids the
auditor from rejecting numbers on its own mental arithmetic, and for pure
computation questions the evidence ledger never contains a computed value —
a structural verification vacuum. Design + SOTA research:
memory/research_math_bi_agent_numeric_guarantees_20260902.md (PAL/PoT,
VerifiAgent, FinTrustRAG numeric grounding, "narrated number" receipts).

Mechanism (deterministic, zero LLM calls, runs inside
AgentLoop._audit_response before the auditor LLM):

  Every risk-gated number in the draft is classified three ways:
    1. QUOTED  — value(+unit family) appears in the user's own messages or
       the evidence ledger (tool outputs, any turn). Unit-family-aware:
       「40 天」 never grounds 「40 ml」 (B2). Assistant-message text is
       NEVER a grounding source (hallucination propagation, T5a).
    2. RECEIPT — value appears in a CURRENT-TURN execute_code/terminal
       output AND the tool's code survives the B1 anti-hardcode checks:
       the value is not a bare literal print (`print(40000)` launders
       nothing) and the code references at least one ≥2-digit number from
       the user's messages (input params in code). A bare tool output
       (print(V)) grounds a unit'd draft value if the value is consistent
       with the draft under ANY unit factor of the same family (the tool
       printed the quantity in an unknown unit of that family).
    3. UNGROUNDED — neither; the caller (agent_loop) decides by mode:
       shadow logs only, enforce returns needs_evidence (soft counter,
       never the reject budget).

  Risk grading (B8) keeps chitchat fast (PAL: small numbers are the model's
  strength; big numbers are where it collapses):
    measurement units (ml/GB/%/元…)  → ints ≥2 digits, or decimals whose
                                        digit string has ≥3 digits (12.5 ✓, 1.5 ✗)
    time/count units (分钟/天/次…)    → ints ≥3 digits, or decimals ≥3 digits
    bare numbers                      → ints ≥3 digits, or decimals ≥3 digits

  B3: Chinese numerals (一百八十六毫升 / 三万五千) are extracted and
  normalized when followed by a unit — 「十分」「一下」 never match.

  Exemptions: years/dates, fenced code blocks, inline code, URLs.

  Unit families (volume/length/mass/data/percent/temp/currency) normalize
  to SI so a receipt printed in mm³ grounds a draft written in ml within a
  relative tolerance (rounding: 186466.6mm³ ≈ "186.5 ml" at 1%).

Mode (B10 gray rollout) lives in config [agent.audit]
numeric_provenance_gate_mode: "shadow" (default — log only) → "enforce" →
"off". This module is pure evaluation: it never blocks, it only reports.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── extraction helpers ──────────────────────────────────────────────────────

_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"https?://\S+")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_YEAR_RE = re.compile(r"(?<![\d.])(19|20)\d{2}(?![\d.])")
# LaTeX unit normalization: \text{mm}^3 → mm³, \text{ml} → ml, \ → space
_LATEX_TEXT_POW_RE = re.compile(r"\\text\s*\{([^}]*)\}\s*\^\s*\{?([0-9])\}?")
_LATEX_TEXT_RE = re.compile(r"\\text\s*\{([^}]*)\}")
_LATEX_SPACE_RE = re.compile(r"\\(?=\s)")
_SUPERS_MAP = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵",
               "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"}

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM_BODY = r"[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二三四五六七八九]+)?"
_CN_NUM_RE = re.compile(_CN_NUM_BODY)
# 中文百分比（B3 补充）：百分之九十八 → 98%（A4.9 M3：最高频中文派生百分比形态）
_CN_PERCENT_RE = re.compile(r"百分之(" + _CN_NUM_BODY + r")")

# unit → (family, SI factor). Key = normalized lowercase token.
_UNIT_TABLE: Dict[str, Tuple[str, float]] = {}
for _u, _f, _m in [
    # volume → m³
    ("mm³", "volume", 1e-9), ("mm3", "volume", 1e-9), ("立方毫米", "volume", 1e-9),
    ("cm³", "volume", 1e-6), ("cm3", "volume", 1e-6), ("cc", "volume", 1e-6),
    ("ml", "volume", 1e-6), ("毫升", "volume", 1e-6), ("立方厘米", "volume", 1e-6),
    ("m³", "volume", 1.0), ("m3", "volume", 1.0), ("立方米", "volume", 1.0),
    ("l", "volume", 1e-3), ("升", "volume", 1e-3),
    # area → m²
    ("mm²", "area", 1e-6), ("mm2", "area", 1e-6), ("平方毫米", "area", 1e-6),
    ("cm²", "area", 1e-4), ("cm2", "area", 1e-4), ("平方厘米", "area", 1e-4),
    ("m²", "area", 1.0), ("m2", "area", 1.0), ("平方米", "area", 1.0),
    ("km²", "area", 1e6), ("平方公里", "area", 1e6), ("平方千米", "area", 1e6),
    # length → m
    ("mm", "length", 1e-3), ("毫米", "length", 1e-3),
    ("cm", "length", 1e-2), ("厘米", "length", 1e-2),
    ("m", "length", 1.0), ("米", "length", 1.0),
    ("km", "length", 1e3), ("千米", "length", 1e3), ("公里", "length", 1e3),
    # mass → kg
    ("mg", "mass", 1e-6), ("毫克", "mass", 1e-6),
    ("g", "mass", 1e-3), ("克", "mass", 1e-3),
    ("kg", "mass", 1.0), ("千克", "mass", 1.0), ("公斤", "mass", 1.0),
    ("t", "mass", 1000.0), ("吨", "mass", 1000.0),
    # data → byte
    ("kib", "data", 1024.0), ("mib", "data", 1024.0 ** 2),
    ("gib", "data", 1024.0 ** 3), ("tib", "data", 1024.0 ** 4),
    ("kb", "data", 1e3), ("mb", "data", 1e6), ("gb", "data", 1e9), ("tb", "data", 1e12),
    # percent / temp / currency (identity families)
    ("%", "percent", 1.0), ("％", "percent", 1.0), ("百分点", "percent", 1.0),
    ("℃", "temp", 1.0), ("°c", "temp", 1.0),
    ("元", "currency:元", 1.0), ("人民币", "currency:元", 1.0),
    ("美元", "currency:美元", 1.0),
    # time → seconds (低风险族)
    ("s", "time", 1.0), ("秒", "time", 1.0),
    ("min", "time", 60.0), ("分钟", "time", 60.0),
    ("hour", "time", 3600.0), ("小时", "time", 3600.0),
    ("day", "time", 86400.0), ("天", "time", 86400.0), ("日", "time", 86400.0),
    ("week", "time", 604800.0), ("周", "time", 604800.0), ("星期", "time", 604800.0),
    ("month", "time", 2592000.0), ("月", "time", 2592000.0),
    ("year", "time", 31536000.0), ("年", "time", 31536000.0),
    # count (低风险族, identity)
    ("个", "count:个", 1.0), ("次", "count:次", 1.0), ("人", "count:人", 1.0),
    ("倍", "count:倍", 1.0), ("只", "count:只", 1.0), ("条", "count:条", 1.0),
    ("张", "count:张", 1.0), ("页", "count:页", 1.0), ("字", "count:字", 1.0),
    ("名", "count:名", 1.0), ("项", "count:项", 1.0), ("家", "count:家", 1.0),
    ("款", "count:款", 1.0), ("层", "count:层", 1.0), ("杯", "count:杯", 1.0),
    ("份", "count:份", 1.0), ("台", "count:台", 1.0), ("辆", "count:辆", 1.0),
    ("间", "count:间", 1.0),
]:
    _UNIT_TABLE[_u.lower()] = (_f, _m)

_MEASURE_FAMILIES = {"volume", "length", "mass", "data", "percent", "temp", "area"}
# reporting canonicalization (factor-identical synonyms → ASCII token)
_UNIT_CANON = {"毫升": "ml", "立方毫米": "mm³", "立方厘米": "cm³", "立方米": "m³",
               "毫米": "mm", "厘米": "cm", "千米": "km", "米": "m",
               "毫克": "mg", "克": "g", "千克": "kg", "吨": "t", "人民币": "元"}
_UNIT_ALT = "|".join(sorted({re.escape(k) for k in _UNIT_TABLE}, key=len, reverse=True))
# unit'd candidate: Chinese numeral OR arabic number (+万/亿) + unit.
# IGNORECASE (GB/gb) + right letter-boundary guard (「12 long」≠ 12 liters).
_UNITED_RE = re.compile(
    r"(?<![\d.])((?:" + _CN_NUM_BODY + r")|(-?\d[\d,]*(?:\.\d+)?))(万|亿)?\s*("
    + _UNIT_ALT + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_BARE_RE = re.compile(r"(?<![\d.])(-?\d[\d,]*(?:\.\d+)?)(万|亿)?")


def _unit_info(unit: str) -> Optional[Tuple[str, float]]:
    if not unit:
        return None
    return _UNIT_TABLE.get(unit.strip().lower())


def cn_to_value(cn: str) -> Optional[float]:
    """Chinese numeral → float (一百八十六→186, 三万五千→35000, 三点五→3.5)."""
    int_part, _, dec_part = cn.partition("点")
    mul_map = {"万": 10000.0, "亿": 100000000.0}
    total, section, digit, cur_mag = 0.0, 0.0, 0.0, 1.0
    for ch in int_part:
        if ch in _CN_DIGITS:
            digit = float(_CN_DIGITS[ch])
        elif ch == "十":
            section += (digit if digit else 1.0) * 10.0
            digit = 0.0
        elif ch == "百":
            section += (digit if digit else 1.0) * 100.0
            digit = 0.0
        elif ch == "千":
            section += (digit if digit else 1.0) * 1000.0
            digit = 0.0
        elif ch in mul_map:
            section = (section + digit) * mul_map[ch]
            total += section
            section, digit = 0.0, 0.0
    total += section + digit
    if not dec_part:
        return total if total > 0 else None
    frac, scale = 0.0, 0.1
    for ch in dec_part:
        if ch in _CN_DIGITS:
            frac += _CN_DIGITS[ch] * scale
            scale *= 0.1
    return (total + frac) if (total + frac) > 0 else None


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def _eff_tol(value: float, tol: float) -> float:
    """Digit-aware tolerance: a 2-significant-digit number IS a coarse
    rounding —「0.19 L」 from receipt 186.4666 L is 1.9% off and must ground
    (the model cannot fix it by calculating more — enforcing 1% there creates
    an unsatisfiable loop). ≥3 digits keep the tight tolerance."""
    if _digit_count(value) <= 2:
        return max(tol, 0.05)
    return tol


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class NumericFlag:
    raw: str            # matched draft text, e.g. 「40000 mm³」
    value: float        # normalized value (万/亿 multiplier applied)
    unit: str           # normalized unit token ("" for bare numbers)
    family: str         # unit family ("" for bare)
    reason: str = "无回执推导值"


@dataclass
class NumericProvenanceReport:
    flags: List[NumericFlag] = field(default_factory=list)
    checked: int = 0

    @property
    def flagged(self) -> bool:
        return bool(self.flags)


# ── corpus extraction (quoted domain) ────────────────────────────────────────

def _strip_exemptions(text: str) -> str:
    text = _FENCED_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _ISO_DATE_RE.sub(" ", text)
    text = _YEAR_RE.sub(" ", text)
    text = _LATEX_TEXT_POW_RE.sub(lambda m: m.group(1) + _SUPERS_MAP.get(m.group(2), m.group(2)), text)
    text = _LATEX_TEXT_RE.sub(r"\1", text)
    text = _LATEX_SPACE_RE.sub(" ", text)
    # LaTeX {,}/{.} thousand-separator braces: 186{,}400 → 186,400
    text = text.replace("{,}", ",").replace("{.}", ".")
    return text


def _extract_corpus_tuples(text: str) -> List[Tuple[float, str, str]]:
    """All (value, unit, family) tuples from a grounding text (quoted domain)."""
    if not text:
        return []
    cleaned = _strip_exemptions(text)
    out: List[Tuple[float, str, str]] = []
    for m in _UNITED_RE.finditer(cleaned):
        cn, ar, mult, unit_raw = m.groups()
        if cn and not ar:
            val = cn_to_value(cn)
            if val is None:
                continue
        else:
            val = float(ar.replace(",", ""))
            if mult:
                val *= 10000.0 if mult == "万" else 100000000.0
        unit = unit_raw.strip().lower()
        info = _UNIT_TABLE.get(unit)
        if info:
            out.append((val, unit, info[0]))
    for m in _BARE_RE.finditer(cleaned):
        val = float(m.group(1).replace(",", ""))
        if m.group(2):
            val *= 10000.0 if m.group(2) == "万" else 100000000.0
        out.append((val, "", ""))
    # 中文百分比（B3 补充）：百分之九十八 → 98%
    for m in _CN_PERCENT_RE.finditer(cleaned):
        val = cn_to_value(m.group(1))
        if val is not None:
            out.append((val, "%", "percent"))
    return out


def _matches_quoted(value: float, unit: str, family: str,
                    corpus: Sequence[Tuple[float, str, str]], tol: float) -> bool:
    """B2: unit'd draft numbers ground ONLY against same-family corpus
    entries (SI-converted, tolerance); bare draft numbers ground against
    any corpus value by raw value. Bare corpus values never ground unit'd
    drafts in the QUOTED domain (family of a bare mention is unknown)."""
    if unit:
        si_a = value * _UNIT_TABLE[unit][1]
        t = _eff_tol(value, tol)
        for cv, cu, cf in corpus:
            if cf != family or not cu:
                continue
            if _close(si_a, cv * _UNIT_TABLE[cu][1], t):
                return True
        return False
    t = _eff_tol(value, tol)
    for cv, _cu, _cf in corpus:
        if _close(value, cv, t):
            return True
    return False


# ── draft-side gated candidates ──────────────────────────────────────────────

# friction-1 (2026-09-02 enforce rollout): knowledge values overwhelmingly
# appear as RANGES or COMPARATIVES; computed results as ≈/=/point values.
_RANGE_SPAN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[~～\-—–至到]\s*(?:-?\d[\d,]*(?:\.\d+)?)")
_PREFIX_COMP_RE = re.compile(r"(?:超过|超(?!级)|逾|近(?!期|日|年|代|似)|大于|小于|不足|不到|高于|低于)\s*$")
_SUFFIX_COMP_RE = re.compile(r"^\s*(?:以上|以下|以内|左右|上下|有余|出头|余)")
_MATH_SPAN_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+?\$", re.DOTALL)
# 单位定义式豁免：「1 cm³ = 1000 mm³」「1 L = 1000 mL」——普适常数不是推导值
_UNIT_DEF_RE = re.compile(
    r"1\s*(" + _UNIT_ALT + r")\s*=\s*(?=-?\d)",
    re.IGNORECASE,
)
# 数学块内：运算符邻接=中间量豁免；≈/=/~(关系符)邻接或词/块边界=结果位，仍 gated。
# 注意 x/X 不是乘号（\approx 以 x 结尾——事故数 40000 的逃逸通道），乘号只有 ×*。
# 「=」不在 next 集合：≈A=B 形态中 A 是结果位（40000 mm³ = 40 ml 必须双双 gated），
# 等号右侧的值由 prev 关系符管制。
_PREV_OP_SYMBOLS = set("×*+−-－＋()（）/／÷^_")
_NEXT_OP_SYMBOLS = set("×*+−-－＋()（）/／÷^_≈~～")
_LATEX_RELATION_CMDS = {"approx", "ne", "neq", "sim", "simeq", "text", "mathrm", "begin", "end"}


def _latex_prev_class(text: str, pos: int) -> str:
    """Class of what precedes a candidate: op / relation / word / symbol / start."""
    i = pos - 1
    while i >= 0 and text[i] in " \t\\{}":
        i -= 1
    if i < 0:
        return "start"
    ch = text[i]
    if ch.isalpha():
        j = i
        while j >= 0 and text[j].isalpha():
            j -= 1
        if j >= 0 and text[j] == "\\":
            cmd = text[j + 1:i + 1]
            return "relation" if cmd in _LATEX_RELATION_CMDS else "op"
        return "word"
    return ch


def _latex_next_class(text: str, pos: int) -> str:
    i = pos
    while i < len(text) and text[i] in " \t\\{}":
        i += 1
    if i >= len(text):
        return "end"
    ch = text[i]
    if ch.isalpha():
        return "word"
    return ch


def _classify_candidate(cleaned: str, start: int, end: int,
                        math_spans: Sequence[Tuple[int, int]]) -> str:
    """'exempt'（知识/区间/单位定义/数学块中间量）| 'math_result'（数学块内
    结果位——数学式天然是计算上下文，无视标记/闭包面强制 gated）| 'normal'."""
    # (0) unit-definition statement: 「1 cm³ = 1000 mm³」 right-hand number
    for m in _UNIT_DEF_RE.finditer(cleaned):
        if abs(m.end() - start) <= 1:
            return "exempt"
    # (a) range membership: number on either side of a ~/-/至 range
    pre = cleaned[max(0, start - 24):start]
    post = cleaned[end:end + 24]
    if re.search(r"\d[\d,]*(?:\.\d+)?\s*[~～\-—–至到]\s*$", pre):
        return "exempt"
    if re.match(r"\s*[~～\-—–至到]\s*\d", post):
        return "exempt"
    # (b) comparative markers hugging the number
    if _PREFIX_COMP_RE.search(pre):
        return "exempt"
    if _SUFFIX_COMP_RE.match(post):
        return "exempt"
    # (c) inside a math block: operator-adjacent = formula intermediate;
    #     relation/word/end = result position → math_result
    for ms, me in math_spans:
        if start >= ms and end <= me:
            prev = _latex_prev_class(cleaned, start)
            nxt = _latex_next_class(cleaned, end)
            if prev in _PREV_OP_SYMBOLS or prev == "op":
                return "exempt"
            if nxt in _NEXT_OP_SYMBOLS:
                return "exempt"
            return "math_result"
    return "normal"


def _digit_count(value: float) -> int:
    """Significant digits (leading zeros stripped — 0.19 has TWO)."""
    s = re.sub(r"[-.,]", "", repr(value)).lstrip("0")
    return len(s) or 1


def _is_decimal(value: float) -> bool:
    return "." in repr(value) and not float(value).is_integer()


def _risk_gate(value: float, family: str, has_unit: bool) -> bool:
    """True → gated (needs grounding). B8 risk grading."""
    dec = _is_decimal(value)
    digits = _digit_count(value)
    if not has_unit:
        return abs(value) >= 100 or (dec and digits >= 3)
    if family in _MEASURE_FAMILIES or family.startswith("currency"):
        # measurement: ≥2-digit ints (40ml ✓), decimals with ≥3 digit-string (12.5 ✓, 1.5 ✗)
        return (not dec and abs(value) >= 10) or (dec and digits >= 3)
    # time/count: ≥3-digit ints, decimals with ≥3 digit-string
    return (not dec and abs(value) >= 100) or (dec and digits >= 3)


# 计算标记（friction-1 深修）：标记窗口内的数值才默认需要回执——纯知识点值
# （168 mL 对比规格/10%/35℃/128 孔）交还 LLM 审计的知识规则（模板明许可凭
# 知识作答）。数学块结果位天然含 ≈/= 标记。
_COMPUTATION_MARKERS = (
    "≈", "＝", "=", "计算得", "计算结果", "结果为", "代入", "换算", "折算",
    "经计算", "容积", "体积", "面积", "容量", "占比", "总数", "总量", "合计",
    "平均", "差值", "比值", "单价", "总价", "利率",
)
_MARKER_WINDOW = 20


def _extract_gated_candidates(draft: str):
    """(value, unit, family, raw, start, end, math_result) for gated numbers."""
    cleaned = _strip_exemptions(draft)
    math_spans = [(m.start(), m.end()) for m in _MATH_SPAN_RE.finditer(cleaned)]
    cands: List[Tuple[float, str, str, str, int, int, bool]] = []
    spans: List[Tuple[int, int]] = []

    def _in_spans(start: int, end: int) -> bool:
        return any(start >= s and start < e for s, e in spans)

    for m in _UNITED_RE.finditer(cleaned):
        cn, ar, mult, unit_raw = m.groups()
        if cn and not ar:
            val = cn_to_value(cn)
            if val is None:
                continue
        else:
            val = float(ar.replace(",", ""))
            if mult:
                val *= 10000.0 if mult == "万" else 100000000.0
        unit = unit_raw.strip().lower()
        info = _UNIT_TABLE.get(unit)
        if not info:
            continue
        if _in_spans(m.start(), m.end()):
            continue
        spans.append((m.start(), m.end()))
        if not _risk_gate(val, info[0], True):
            continue
        cls = _classify_candidate(cleaned, m.start(), m.end(), math_spans)
        if cls == "exempt":
            continue
        cands.append((val, _UNIT_CANON.get(unit, unit), info[0], m.group(0).strip(),
                      m.start(), m.end(), cls == "math_result"))
    for m in _BARE_RE.finditer(cleaned):
        if _in_spans(m.start(), m.end()):
            continue
        val = float(m.group(1).replace(",", ""))
        if m.group(2):
            val *= 10000.0 if m.group(2) == "万" else 100000000.0
        if not _risk_gate(val, "", False):
            continue
        cls = _classify_candidate(cleaned, m.start(), m.end(), math_spans)
        if cls == "exempt":
            continue
        cands.append((val, "", "", m.group(0), m.start(), m.end(), cls == "math_result"))
    # 中文百分比（B3 补充）：百分之九十八 → 98%
    for m in _CN_PERCENT_RE.finditer(cleaned):
        val = cn_to_value(m.group(1))
        if val is None or not _risk_gate(val, "percent", True):
            continue
        cls = _classify_candidate(cleaned, m.start(), m.end(), math_spans)
        if cls == "exempt":
            continue
        cands.append((val, "%", "percent", m.group(0), m.start(), m.end(), cls == "math_result"))
    return cands, cleaned


# ── B1 receipt checks ────────────────────────────────────────────────────────

_BARE_PRINT_RE = re.compile(
    r"print\s*\(\s*(['\"])?(-?\d[\d,]*(?:\.\d+)?)(['\"])?\s*\)"
    r"|print\s*\(\s*f?['\"][^'\"]*\{(-?\d[\d,]*(?:\.\d+)?)\}[^'\"]*['\"]\s*\)"
)
# the whole expression/code body is just a literal (calculate hardcode shape)
_BARE_EXPR_RE = re.compile(r"^\s*-?\d[\d,]*(?:\.\d+)?\s*$")
_CODE_NUM_TOKEN_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _code_value_literals(code: str) -> List[float]:
    """Every numeric literal appearing anywhere in the tool code/command —
    the anti-hardcode universe (C2: `V=40000\\nprint(V)` and `echo 40000`
    both carry the value as a literal, not just bare `print(40000)`)."""
    return [float(t.replace(",", "")) for t in _CODE_NUM_TOKEN_RE.findall(code or "")]


def _value_literal_in_code(code: str, value: float, unit: str, family: str, tol: float) -> bool:
    """True when the flagged value itself appears as a literal in the code
    (value-equal, or family-factor-equal for unit'd values — a literal
    186466.6 in code is the hardcode form of a draft 186.5 ml)."""
    si_a = value * _UNIT_TABLE[unit][1] if unit else None
    family_factors = [m for f, m in _UNIT_TABLE.values() if f == family] if unit else []
    for tok in _code_value_literals(code):
        if _close(tok, value, tol):
            return True
        if si_a is not None and any(_close(tok * fac, si_a, tol) for fac in family_factors):
            return True
    return False


def _num_in_code(user_num: float, code: str) -> bool:
    """C2: user number matches code NUMERIC TOKENS by value — never raw
    substring (「40」 must not match inside the hardcoded literal 40000)."""
    for tok in _code_value_literals(code):
        if _close(tok, user_num, 1e-9):
            return True
    return False


def _receipt_grounds(value: float, unit: str, family: str,
                     tool_calls: Sequence[dict], user_nums: Sequence[float],
                     tol: float) -> bool:
    """B1 anti-hardcode receipt:
      ① value must appear in the tool output — same-family unit'd output
         (SI compare), OR a bare output number consistent with the draft
         value under SOME unit factor of the draft's family;
      ② the printed value must not be a bare literal (`print(40000)`);
      ③ the code must reference ≥1 ≥2-digit number from the user's
         messages (input params in code)."""
    user_nums = list(user_nums or [])
    t = _eff_tol(value, tol)
    for tc in tool_calls or []:
        out = str(tc.get("output") or "")
        code = str(tc.get("code") or "")
        if not out:
            continue
        out_tuples = _extract_corpus_tuples(out)
        hit = False
        if unit:
            si_a = value * _UNIT_TABLE[unit][1]
            family_factors = [m for f, m in _UNIT_TABLE.values() if f == family]
            for cv, cu, cf in out_tuples:
                if cu and cf == family and _close(si_a, cv * _UNIT_TABLE[cu][1], t):
                    hit = True
                    break
                if not cu:
                    # bare output number — try every unit factor of the family
                    if any(_close(si_a, cv * fac, t) for fac in family_factors):
                        hit = True
                        break
            if not hit:
                # also: unit'd output of a DIFFERENT unit of same family already
                # covered above; bare handled — no match → next tool call
                pass
        else:
            for cv, _cu, _cf in out_tuples:
                if _close(value, cv, t):
                    hit = True
                    break
        if not hit:
            continue
        # B1a: the flagged value itself as a literal in the code → hardcode,
        # not a receipt (C2: covers print(40000), `V=40000; print(V)`,
        # `echo 40000`, and the whole-expression literal "186.5" on calculate)
        if _BARE_EXPR_RE.match(code or "") or _value_literal_in_code(code, value, unit, family, tol):
            continue
        # B1b: param provenance — code numeric tokens reference ≥1 user number
        if not any(_num_in_code(b, code) for b in user_nums):
            continue
        return True
    return False


# ── public API ───────────────────────────────────────────────────────────────

def _derivation_closure(user_nums: Sequence[float], receipt_values: Sequence[float]) -> set:
    """Depth-2 arithmetic closure of user-input numbers ∪ receipt values —
    the set of values a model could plausibly have derived IN ITS HEAD from
    given data (the 40ml incident: 40 = 65-25). Anything outside the closure
    that carries no computation marker is a knowledge claim, not a
    computation (168 mL comparable-tray spec, 10%, 128 孔)."""
    s0 = [float(x) for x in list(user_nums)[:16] + list(receipt_values)[:16]]
    vals = set(s0)
    for a in s0:
        vals.update({a * 2, a / 2 if a != 0 else 0.0, a * a})
        for b in s0:
            vals.add(a + b)
            vals.add(abs(a - b))
            vals.add(a * b)
            if b != 0:
                vals.add(a / b)
    return vals


def _in_closure(value: float, closure: set, tol: float = 5e-3) -> bool:
    return any(_close(value, v, tol) for v in closure)


def _has_computation_marker(cleaned: str, start: int, end: int) -> bool:
    window = cleaned[max(0, start - _MARKER_WINDOW):min(len(cleaned), end + _MARKER_WINDOW)]
    return any(m in window for m in _COMPUTATION_MARKERS)


def evaluate_numeric_provenance(
    draft: str,
    evidence_text: str,
    user_messages: Sequence[str],
    current_tool_calls: Sequence[dict],
    tolerance: float = 0.01,
) -> NumericProvenanceReport:
    """Pure evaluation (never blocks). Flags high-risk draft numbers that are
    neither quoted (ledger/user messages) nor receipt-backed (current-turn
    execute_code/calculate/terminal outputs with B1 param provenance).

    Gate trigger面 (2026-09-02 晚, conv 43dd9841 death-loop fix): a candidate
    is flagged ONLY when it carries a computation marker (≈/=/计算得/换算/
    容积…) OR is derivable from user inputs (family-exportable AND inside
    the arithmetic closure) OR is a bare number inside the closure. Knowledge
    point values (168 mL specs, 10%, 35℃, 128 孔) are NOT flaggable — they
    are not computations and the model cannot ground them with calculate;
    blocking them created an unsatisfiable loop that burned the audit budget
    into the deterministic fallback."""
    report = NumericProvenanceReport()
    if not draft or not draft.strip():
        return report
    quoted: List[Tuple[float, str, str]] = list(_extract_corpus_tuples(evidence_text or ""))
    user_families: set = set()
    user_nums: List[float] = []
    for um in user_messages or []:
        um_clean = _strip_exemptions(str(um or ""))
        for v, u, f in _extract_corpus_tuples(um_clean):
            quoted.append((v, u, f))
            if u and f:
                user_families.add(f.split(":")[0])
        for m in re.finditer(r"(?<![\d.])\d[\d,]*(?:\.\d+)?", um_clean):
            # 单数字也作锚点（A4.9 R2 finding-1：半径9cm 场景）
            user_nums.append(float(m.group(0).replace(",", "")))
    receipt_values: List[float] = []
    for tc in current_tool_calls or []:
        for cv, cu, cf in _extract_corpus_tuples(str(tc.get("output") or "")):
            if not cu:
                receipt_values.append(cv)
    closure = _derivation_closure(user_nums, receipt_values)
    cands, cleaned = _extract_gated_candidates(draft)
    for value, unit, family, raw, start, end, math_result in cands:
        report.checked += 1
        if _matches_quoted(value, unit, family, quoted, tolerance):
            continue
        if _receipt_grounds(value, unit, family, current_tool_calls or [], user_nums, tolerance):
            continue
        # B8 low-risk exemption: single-step derivations from user numbers
        if any(_close(value, t, 1e-6) for un in user_nums for t in (
            un / 2, un * 10, un / 10, un * 100, un / 100, un * 1000, un / 1000,
        )):
            continue
        # Gate trigger面收缩：数学块结果位强制 gated（数学式=计算上下文）；
        # 否则需计算标记或可导出闭包——纯知识点值交还审计员知识规则
        base_family = family.split(":")[0]
        derivable = (
            base_family in ("volume", "area") and "length" in user_families
        ) or (family in user_families and base_family not in ("length", "time", "temp", "currency", "percent", "count"))
        marker = math_result or _has_computation_marker(cleaned, start, end)
        if unit:
            if not marker and not (derivable and _in_closure(value, closure)):
                continue
        else:
            if not marker and not _in_closure(value, closure):
                continue
        report.flags.append(NumericFlag(raw=raw, value=value, unit=unit, family=family))
    return report
