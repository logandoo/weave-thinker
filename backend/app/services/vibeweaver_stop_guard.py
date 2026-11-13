# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""vibeweaver stop-guard（§V12 / D3）— 伪停检测与纠正投递。

背景（JS audit-core.js stop-guard 移植）：对「上下文耗尽/先停一下」类**语义伪停**
的字节级退化检测器（loop-guard）天然失明——它没有重复形态，只有停发词。本模块
做**语法级**检测（保守：漏报可接受，误报不可），命中的触发经 hook_service stop
通道投递纠正指令（第三只 stop hook；gate-red 在前，只有 when-would-allow 才轮到
本钩子）。

语义分级（方案 §十 B 级）：触发与投递是硬的（stop 通道 allow=False + feedback），
**遵从是软的**（模型可忽略纠正）；预算 2/会话；episode 去重（同尾只拦一次）；
预算耗尽/无投递通道 → abandon debt 落盘（`.vibeweaver/stop-debt.json` cap5）并
汇入 tests/gate_audit.md，绝不静默。`VIBEWEAVER_STOPGUARD=off` 或
`[vibeweaver] stop_guard_enabled=false` 关闭。

白名单五路（每条作用域化，防历史免疫）：本会话写入的 paused_state.md · 本会话
写入的 Class-E 决策行 · 同一消息内的 [Verification Gate] 标记 · 用户停令
（指令形匹配 + 否定排除 + 插件提示剥离）· 末句问句。产品无 token 遥测 →
「无 fill 版」文案（绝不编造填充率）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.config import get_config
from app.services import hook_service
from app.services import vibeweaver_state

logger = logging.getLogger(__name__)

STOP_GUARD_MAX = 2  # 干预预算/会话（对齐 JS stopGuardMaxInterventions 默认）
_TAIL_SCAN = 4096
_MAX_SESSIONS = 200

# ---- 停发词检测（audit-core.js STOP_EUPHEMISM_RES 移植）----
# 每个模式都要求「耗尽词 × 上下文词近邻」；“out of context”“full of”等习语与
# 裸提“context limit”保持干净。遗漏同义改写可接受，误报不可。
_STOP_EUPHEMISM_RES = (
    re.compile(r"上下文[^。\n]{0,20}(耗尽|用尽|不足|不够|快满|已满|达到?上限|满了|撑不住)"),
    re.compile(r"(耗尽|用尽)[^。\n]{0,10}上下文"),
    re.compile(r"running out of (the )?context", re.IGNORECASE),
    re.compile(r"out of context (window|tokens?|space|budget)", re.IGNORECASE),
    re.compile(
        r"context (window )?(is |was |gets? |getting )?(almost |nearly |about to be )?"
        r"\d{0,3}\s?%?\s?(exhausted|full)(?!\s+of)",
        re.IGNORECASE,
    ),
    re.compile(r"context (window )?(is |was )?running low", re.IGNORECASE),
    re.compile(r"(hit|hits|hitting|reached?|reaching) (the )?context (window )?limit", re.IGNORECASE),
    re.compile(r"context limit (reached|hit|exceeded)", re.IGNORECASE),
)

# 真正的停发声明携带「终止意图」；纯引用/正文中段提及是元讨论（测试/文档/契约）。
# 终止形态面向 agent 自身：「wrap up」仅裸用/句末才算（“wrap up the component”是工作）。
_STOP_CESSATION_RES = (
    re.compile(r"先到这里|先停在这|先停一下|到此为止|无法继续|不能再继续"),
    re.compile(
        r"stopping here|stop here|pausing here|let me stop|i('ll| will) stop|i have to stop",
        re.IGNORECASE,
    ),
    re.compile(r"wrap(ing)? up(?=\s*[。.!]?\s*$|\s+(here|now|today|for now))", re.IGNORECASE),
    re.compile(r"(?:^|[\s,.，。])(?:stopping|wrapping up|pausing|giving up)\s*[.!…]?\s*$", re.IGNORECASE),
)

# 引用跨度（代码/测试/竞态说明）绝不构成声明。直单引号不剥（会吃掉缩写：
# "We've hit the context limit, so I'll stop"）。
_QUOTED_RES = (
    re.compile(r"```[\s\S]*?```"),
    re.compile(r"`[^`\n]*`"),
    re.compile(r'"[^"\n]*"'),
    re.compile(r"“[^”\n]*”"),
    re.compile(r"「[^」\n]*」"),
)

_TRAILING_DECAY_RE = re.compile(r"\s*[了啦吧呢啊嘛]?[。.!…~]*\s*$")
_LABEL_COLON_RE = re.compile(r"[:：]\s*$")

# 用户停令（指令形匹配 + 否定排除）
_USER_STOP_RE = re.compile(
    r"(^\s*(?:please\s+)?(?:stop|cancel|quit)\b(?!\s+(?:the|a|an)\s)"
    r"|\bplease\s+(?:stop|cancel|quit)\b"
    r"|\b(?:stop|cancel|quit)\s+(?:here|now|it|this|coding|working|for today)\b"
    r"|停一下|停下来|停止吧|别做了|别再做|取消吧|到此为止|今天到这)",
    re.IGNORECASE,
)
_USER_STOP_NEG_RE = re.compile(
    r"((?:don't|do not|never)\s+(?:stop|cancel|quit)|不要停|别停|不能停)", re.IGNORECASE
)
# 插件自产提示必须剥离（守卫不得给自己发白名单）
_PLUGIN_PROMPT_STRIP_RE = re.compile(r"^\[(stop-guard|loop-guard)\][^\n]*", re.MULTILINE)

# Positive silent-stop exemptions (upstream stopAwaitingUser / read-only scope).
_AWAITING_RE = re.compile(
    r"(please\s+(confirm|choose|decide|let me know)|waiting\s+(on|for)\s+your|"
    r"awaiting\s+your|should\s+i\s+(proceed|continue)|would\s+you\s+like|"
    r"请(你)?确认|等你(确认|决定)|需要你(确认|决定)|是否继续|要我继续吗|待你确认)",
    re.IGNORECASE,
)
_READONLY_RE = re.compile(
    r"(仅(做|是|需|要)?(评估|审计|评审|分析|检查)|只读|不要修改|请勿修改|别改代码|"
    r"read[- ]?only|\bonly\s+(evaluate|review|audit|analy[sz]e|inspect)\b|"
    r"\b(review|audit|evaluation|analysis)\s+only\b|"
    r"(do not|don'?t)\s+(modify|change|edit)\s+(any\s+|the\s+)?(code|implementation|source|files?))",
    re.IGNORECASE,
)
_PROSE_WRITE_RE = re.compile(
    r"\.(md|txt|rst|log|png|jpe?g|webm|wav|mp4|mp3|docx?|xlsx?|pptx?|pdf|odt|ods|odp|rtf|odg|epub|mht|csv|tsv|ya?ml)$",
    re.IGNORECASE,
)
_PROSE_NAME_RE = re.compile(r"(^|/)(LICENSE|NOTICE|COPYING|README|CHANGELOG|AUTHORS)(\.[A-Za-z0-9]+)?$", re.IGNORECASE)
_REPORT_PATH_RE = re.compile(
    r"(^|/)(reports?|output|outputs|artifacts?|findings?|results?|summaries|summary|docs?)(/|$)",
    re.IGNORECASE,
)
_CODE_WRITE_RE = re.compile(
    r"\.(py|js|mjs|ts|tsx|jsx|go|rs|java|c|cc|cpp|h|hpp|rb|php|cs|kt|swift|m|scala|pl|sh|bash|ps1|vue|svelte|sql)$",
    re.IGNORECASE,
)
_FINAL_RUN_PASS_RE = re.compile(
    r"-[ \t]*final-run:[^\n]*--final[^\n]*pass=[1-9]\d*[ \t]*/[ \t]*fail=0(?![\d.])",
    re.IGNORECASE,
)
_ASSERT_RUN_RE = re.compile(
    r"-[ \t]*assert-run:[ \t]*assert_artifacts\.py:[ \t]*(?:all\s+[1-9]\d*\s+checks\s+pass|pass=[1-9]\d*/fail=0)",
    re.IGNORECASE,
)

def _strip_quoted_spans(s: str) -> str:
    for re_ in _QUOTED_RES:
        s = re_.sub(" ", s)
    return s


def stop_euphemism(text: str) -> Optional[Dict[str, str]]:
    """返回 {kind:"euphemism", match} | None。只扫尾部。

    元讨论守卫：先剥引用跨度；≥2 个不同模式命中 = 在讨论该模式而非声明；
    单命中必须携带终止意图或命中后 ≤120 字符内到句尾。
    """
    if not text or not isinstance(text, str):
        return None
    tail = _strip_quoted_spans(text[-_TAIL_SCAN:])
    hit: Optional[re.Match] = None
    hits = 0
    for re_ in _STOP_EUPHEMISM_RES:
        m = re_.search(tail)
        if m:
            hits += 1
            if hit is None:
                hit = m
    if hits != 1 or hit is None:
        return None
    matched = hit.group(0)
    start = tail.find(matched)
    if start < 0:
        return None
    after = tail[start + len(matched):]
    before = tail[:start]
    near = after[:80]
    cessation = any(re_.search(near) for re_ in _STOP_CESSATION_RES) or (
        _TRAILING_DECAY_RE.fullmatch(after) is not None and not _LABEL_COLON_RE.search(before)
    )
    if not cessation:
        return None
    return {"kind": "euphemism", "match": matched[:80]}


def stop_guard_decision(
    *,
    trigger: Optional[Dict[str, str]],
    task_active: bool,
    paused_state: bool = False,
    class_e: bool = False,
    gate_marker: bool = False,
    user_stop: bool = False,
    tail_question: bool = False,
    telemetry: bool = True,
) -> Dict[str, str]:
    """纯裁决（audit-core.js stopGuardDecision 移植）。"""
    if trigger is None:
        return {"action": "ignore", "why": "no-trigger"}
    if not task_active:
        return {"action": "ignore", "why": "no-active-task"}
    if paused_state:
        return {"action": "ignore", "why": "whitelisted:paused-state"}
    if class_e:
        return {"action": "ignore", "why": "whitelisted:class-e"}
    if gate_marker:
        return {"action": "ignore", "why": "whitelisted:gate-marker"}
    if user_stop:
        return {"action": "ignore", "why": "whitelisted:user-stop"}
    if tail_question:
        return {"action": "ignore", "why": "whitelisted:tail-question"}
    if not telemetry:
        return {"action": "debt", "why": "no-telemetry", "kind": str(trigger.get("kind") or "")}
    return {"action": "prompt", "kind": str(trigger.get("kind") or "")}


def stop_guard_prompt(kind: str = "euphemism") -> str:
    """纠正指令（§V12 契约；无 fill 版——产品无 token 遥测，绝不编造填充率）。"""
    if kind == "truncation":
        what = (
            'Your last turn ended with finish:"length" (output-limit truncation) — '
            "a truncated turn is NOT a completed task."
        )
    elif kind == "silent-stop":
        what = "This is a silent stop after a non-evidence task write without a trusted final verification run."
    else:
        what = "Your last message declared context exhaustion and stopped mid-task."
    return (
        f"[stop-guard] {what} That is NOT a legal stop. Legal stops are exactly four: "
        "(1) ALL-PASS on every acceptance criterion in tests/acceptance.md; "
        "(2) cap/stall with a recorded failure entry in memory/ (COV-7); "
        "(3) a PAUSED packet in tests/paused_state.md (§3.4); "
        "(4) a [Coverage]-honest partial completion naming every unchecked item (§V9). "
        "Resume now: read tests/working_note.md (if present) + tests/acceptance.md + "
        "the last entries of tests/verification_log.md, then continue the loop. "
        "If the budget is genuinely nearly exhausted, consolidate (ship-order) or write "
        "the PAUSED packet — never abandon silently."
    )


# ---------------- episode / budget 状态（进程内，有界） ----------------

_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _session_state(conv_id: str) -> Dict[str, Any]:
    st = _SESSIONS.get(conv_id)
    if st is None:
        if len(_SESSIONS) >= _MAX_SESSIONS and conv_id not in _SESSIONS:
            for k in list(_SESSIONS.keys())[:50]:
                _SESSIONS.pop(k, None)
        st = {
            "interventions": 0,
            "armed": True,
            "disarmed_key": "",
            "first_seen": time.time(),  # 会话作用域白名单锚（本进程首见时刻）
            "writes": [],  # 本 run 的非证据写入（positive silent-stop 腿）
        }
        _SESSIONS[conv_id] = st
    return st


def reset_state() -> None:
    """测试隔离用。"""
    _SESSIONS.clear()


def touch_session(conv_id: str) -> None:
    """会话开始锚（装配/run 起点调用）：白名单 mtime 比较的 `first_seen` 必须
    在**本会话任何写入之前**建立——否则本回合写入的 paused_state/Class-E 会因
    mtime < first_seen 而被排除，恰是白名单存在的场景（评审 B-C1）。"""
    if conv_id:
        st = _session_state(conv_id)
        st["writes"] = []  # run 边界：上一 run 的非证据写入不得触发本 run silent-stop


def note_task_write(conv_id: str, rel_path: str) -> None:
    """记录本 run 的非证据、非 prose/report 写入；tests/test/memory 只算修复路径。"""
    normalized = str(rel_path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if not normalized or normalized.split("/", 1)[0].lower() in ("tests", "test", "memory"):
        return
    if _PROSE_WRITE_RE.search(normalized) or _PROSE_NAME_RE.search(normalized):
        return
    if _REPORT_PATH_RE.search(normalized) and not _CODE_WRITE_RE.search(normalized):
        return
    st = _session_state(conv_id or "unknown")
    writes = st.setdefault("writes", [])
    if normalized not in writes:
        writes.append(normalized)
        del writes[:-50]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def current_log_block(log_text: str) -> str:
    """Current task block, not merely the last heading (see audit service)."""
    if not log_text or not isinstance(log_text, str):
        return ""
    parts = [p for p in re.split(r"^(?=## )", log_text, flags=re.MULTILINE) if p.strip()]
    marker = re.compile(
        r"(?m)^- (?:iter \d+ (?:PASS|FAIL)|final-run:|assert-run:)|\[Verification Gate\]"
    )
    for part in reversed(parts):
        if marker.search(part):
            return part
    return parts[-1] if parts else log_text


def _journal_debt(root: Path, rec: Dict[str, Any]) -> None:
    """汇入 tests/gate_audit.md（advisory，绝不静默）。"""
    try:
        p = root / "tests" / "gate_audit.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(
                f"- {time.strftime('%Y-%m-%dT%H:%M:%S')} stop-debt: {rec.get('kind')} "
                f"\"{rec.get('match')}\" — {rec.get('why')} (session {rec.get('sessionID')}) "
                "— §V12 false-stop; adjudicate in Tier-2 review\n"
            )
    except OSError:
        pass


# ---------------- stop hook ----------------

async def _stop_guard_hook(ctx: hook_service.StopContext) -> Optional[hook_service.StopDecision]:
    try:
        if not ctx.vibeweaver_active:
            return None  # W17 轮次门控：非激活轮不判伪停
        try:
            if not get_config().vibeweaver_stop_guard_enabled:
                return None
        except Exception:
            return None
        if str(os.environ.get("VIBEWEAVER_STOPGUARD", "")).strip().lower() == "off":
            return None
        from app.services.vibeweaver_gate_service import resolve_session_root

        key = ctx.conversation_id or ctx.session_id or ""
        root = resolve_session_root(ctx.workspace_path, key,
                                    scope_ctx=getattr(ctx, "workspace_ctx", None))
        if root is None:
            return None
        a_tail = (ctx.assistant_content or "")[-_TAIL_SCAN:]
        trigger = stop_euphemism(a_tail)
        st = _session_state(key or "unknown")
        arm_key = hashlib.sha1(a_tail[-256:].encode("utf-8", "replace")).hexdigest()[:16]
        if st["disarmed_key"] and arm_key != st["disarmed_key"]:
            st["armed"] = True
            st["disarmed_key"] = ""
        session_start = float(st.get("first_seen") or 0)
        paused_state = _mtime(root / "tests" / "paused_state.md") >= session_start > 0
        decisions_path = root / "tests" / "decisions.md"
        class_e = (
            _mtime(decisions_path) >= session_start > 0
            and "class-e" in _read(decisions_path).lower()
        )
        gate_marker = "[Verification Gate]" in a_tail
        awaiting_user = bool(_AWAITING_RE.search(a_tail))
        tail_question = awaiting_user or bool(re.search(r"[?？]\s*$", a_tail.strip()))
        user_text = _PLUGIN_PROMPT_STRIP_RE.sub("", ctx.last_user_text or "")
        readonly_scope = bool(_READONLY_RE.search(user_text))
        user_stop = bool(_USER_STOP_RE.search(user_text)) and not bool(
            _USER_STOP_NEG_RE.search(user_text)
        )
        block = current_log_block(_read(root / "tests" / "verification_log.md"))
        gate_marker = gate_marker or "[Verification Gate]" in block
        wave_complete = bool(_FINAL_RUN_PASS_RE.search(block)) and bool(_ASSERT_RUN_RE.search(block))
        task_active = (
            (root / "tests" / "acceptance.md").exists()
            and bool(ctx.had_tool_calls)
            and not wave_complete
        )
        if trigger is None and task_active and not gate_marker and not readonly_scope and not awaiting_user:
            is_doc = bool(re.search(r"-[ \t]*class:[ \t]*DOC\b", block, re.IGNORECASE))
            forged_doc_code = is_doc and any(_CODE_WRITE_RE.search(w) for w in (st.get("writes") or []))
            if st.get("writes") and (not is_doc or forged_doc_code):
                trigger = {"kind": "silent-stop", "match": "non-evidence code write without trusted final-run"}
        if trigger is None:
            return None  # 无 finish 遥测 → truncation 腿不可得（宿主差异，文档化）
        decision = stop_guard_decision(
            trigger=trigger,
            task_active=task_active,
            paused_state=paused_state,
            class_e=class_e,
            gate_marker=gate_marker,
            user_stop=user_stop,
            tail_question=tail_question,
            telemetry=True,
        )
        if decision.get("action") == "ignore":
            return None
        if not st["armed"]:
            return None  # 同 episode 已处理（no-thrash）
        st["armed"] = False
        st["disarmed_key"] = arm_key
        kind = str(trigger.get("kind") or "euphemism")
        match = str(trigger.get("match") or "")
        if decision.get("action") == "debt" or st["interventions"] >= STOP_GUARD_MAX:
            why = (
                str(decision.get("why") or "budget-exhausted")
                if decision.get("action") == "debt"
                else "budget-exhausted"
            )
            vibeweaver_state.append_stop_debt(root, key, kind, match, why)
            _journal_debt(
                root,
                {"kind": kind, "match": match[:80], "why": why, "sessionID": key},
            )
            vibeweaver_state.record_stop_guard_event(root, prompted=False, kind=kind, match=match)
            logger.info(
                "stop-guard false-stop episode (%s) — budget exhausted (%d/%d) — journaled as stop-debt (%s)",
                kind, st["interventions"], STOP_GUARD_MAX, why,
            )
            return None
        st["interventions"] += 1
        vibeweaver_state.record_stop_guard_event(root, prompted=True, kind=kind, match=match)
        logger.warning(
            "stop-guard false-stop detected (%s: %s) — blocking completion with corrective feedback (%d/%d)",
            kind, match[:60], st["interventions"], STOP_GUARD_MAX,
        )
        return hook_service.StopDecision(
            allow=False,
            feedback=stop_guard_prompt(kind),
            reason="stop-guard: false stop detected (§V12)",
        )
    except Exception:
        return None  # fail-open：守卫绝不掀翻收尾


async def _stop_guard_tool_after(ctx: hook_service.ToolHookContext) -> Optional[hook_service.HookDecision]:
    """只观察成功的 write/edit，供 positive silent-stop 判定；不改变工具结果。"""
    if not ctx.vibeweaver_active or ctx.is_error or ctx.tool_name not in ("workspace_write", "workspace_edit"):
        return None
    from app.services.vibeweaver_gate_service import resolve_session_root

    key = ctx.conversation_id or ctx.session_id or ""
    file_path = (ctx.args or {}).get("file_path") or (ctx.args or {}).get("path")
    root = resolve_session_root(
        ctx.workspace_path, key, starts=[file_path], prefer_hints=True
    )
    if root is None:
        return None
    if not file_path:
        return None
    try:
        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        rel = candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    note_task_write(key, rel)
    return None


def install_hooks() -> Dict[str, Any]:
    """注册 stop-guard stop 钩子（gate/audit 之后 = when-would-allow 语义）。"""
    summary = {"stop_guard_hooks": 0}
    try:
        if not (
            get_config().vibeweaver_enabled
            and get_config().vibeweaver_audit_enabled
            and get_config().vibeweaver_stop_guard_enabled
        ):
            return summary
    except Exception:
        return summary
    hook_service.register_stop_hook(_stop_guard_hook, name="vibeweaver_stop_guard")
    hook_service.register_tool_hook("after", _stop_guard_tool_after, name="vibeweaver_stop_guard_writes")
    summary["stop_guard_hooks"] = 1
    summary["stop_guard_tool_hooks"] = 1
    logger.info("vibeweaver stop-guard hook installed")
    return summary
