# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import re as _re
import uuid
import itertools
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from app.services.llm_service import LLMService, PRESERVE_THINKING_PROVIDERS
from app.tools.registry import registry
from app.services.retry_utils import coerce_tool_args, repair_tool_call_arguments, sanitize_messages_surrogates, jittered_backoff
from app.services.tool_result_budget import maybe_persist_tool_result, enforce_turn_budget, BudgetConfig, DEFAULT_BUDGET
from app.services.tool_result_digest import digest_tool_results_batch, DigestConfig, DEFAULT_DIGEST_TOOLS
from app.services.provider_router import build_thinking_extra_body
from app.services.agent_permissions import (
    is_permission_allowed,
    PermissionContext,
    permission_key_for_tool_request,
    permission_description,
)
from app.core.config import get_config
from app.services.deathmatch_service import DeathmatchManager

logger = logging.getLogger(__name__)
config = get_config()

# run(precomputed_coord=...) 的"未提供"哨兵——coordinator 合法返回 None
# （deathmatch/skill 跳过），必须与"调用方未预算"区分
_UNSET = object()

_PARALLEL_SAFE_TOOLS: Set[str] = {
    "web_search",
    "browser",
    "memory",
    "context7_resolve_library_id",
    "context7_query_docs",
    "workspace_read",
    "word_count",
    "workspace_glob",
    "provide_file",
    "grep",
    "diff",
}

_TOOL_CALL_RE = _re.compile(r'<tool_calls>.*?</tool_calls>', _re.DOTALL)
_TOOL_INVOKE_RE = _re.compile(r'<invoke[^>]*>.*?</invoke>', _re.DOTALL)
_TOOL_CALL_XML_RE = _re.compile(r'<tool_call>.*?</tool_call>', _re.DOTALL)
_TOOL_RESULT_RE = _re.compile(r'<tool_result>.*?</tool_result>', _re.DOTALL)

# Invisible deathmatch context markers that the model must echo.
_DM_CTX_RE = _re.compile(r'<!--dm_ctx:round=\d+:hash=[a-f0-9]+:ts=\d+-->')

# 遵循词 (canary marker): visible per-conversation tokens the model must echo
# at the end of every final reply. Stripped before display/persistence. The
# regex lives in canary_marker.py — import, never re-declare.
from app.services.canary_marker import CANARY_RE as _CANARY_RE  # noqa: E402

# DeepSeek-style DSML markup that some models emit inline instead of using
# native function-calling deltas.
_DSML_TOOL_CALLS_RE = _re.compile(r'<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>', _re.DOTALL)
_DSML_INVOKE_RE = _re.compile(
    r'<｜｜DSML｜｜invoke\s+name\s*=\s*"([^"]+)"[^>]*>(.*?)</｜｜DSML｜｜invoke>',
    _re.DOTALL,
)
_DSML_PARAMETER_RE = _re.compile(
    r'<｜｜DSML｜｜parameter\s+name\s*=\s*"([^"]+)"(?:\s+string\s*=\s*"([^"]*)")?\s*>(.*?)</｜｜DSML｜｜parameter>',
    _re.DOTALL,
)
# DSML tool-call IDs must be unique across the entire request. A monotonic
# counter plus a per-process nonce guarantees no collisions with persisted
# history or across assistant turns within the same run.
_DSML_CALL_NONCE = uuid.uuid4().hex[:8]
_DSML_CALL_COUNTER = itertools.count()
# ── Claim-vs-content consistency (conv 41d2790d, 2026-08-10) ──
# The final answer of conv 41d2790d claimed "上方 Mermaid 图…下方附有纯文本
# 文件树和关键信息汇总表" while containing NONE of them. Per the user
# principle (2026-07-20: 禁止正则/硬编码分类器，语义判断留给 LLM), there is
# NO deterministic detector here — the AUDITOR LLM makes the call, with rule
# 6 of _AUDITOR_SYSTEM_TEMPLATE requiring claimed artifacts to physically
# exist in the draft, and the auditor context carrying the ACTUAL tool
# results + the previous answer so the judgment is grounded in evidence.
# The rejection budget guarantees the FINAL draft is always audited.
# Matches ANY individual DSML tag (opening or closing) that may remain after
# stripping complete blocks — e.g. orphaned <｜｜DSML｜｜invoke ...> or
# </｜｜DSML｜｜parameter> fragments left over when a block is split across
# stream chunks.
_DSML_ANY_TAG_RE = _re.compile(r'</?｜｜DSML｜｜[^>]*>', _re.DOTALL)
_DSML_MARKER = '<｜｜DSML｜｜'
_DSML_TOOL_CALLS_CLOSE = '</｜｜DSML｜｜tool_calls>'


def _strip_dsml_all(text: str) -> str:
    """Aggressively strip ALL DSML markup and deathmatch context markers from *text*.

    Removes complete ``<｜｜DSML｜｜tool_calls>`` blocks first, then any
    remaining individual DSML tags (orphaned invoke/parameter fragments).
    Also strips invisible `<!--dm_ctx:...-->` markers so users never see them.
    Used as a safety net on finalised content before it is persisted or
    displayed.
    """
    if not text:
        return text
    if _DSML_MARKER in text:
        text = _DSML_TOOL_CALLS_RE.sub('', text)
        text = _DSML_ANY_TAG_RE.sub('', text)
    text = _DM_CTX_RE.sub('', text)
    text = _CANARY_RE.sub('', text)
    return text


def _dsml_marker_prefix_len(s: str) -> int:
    """Longest suffix of ``s`` that is a prefix of the DSML marker.

    Used to decide how many trailing chars to hold back between stream
    chunks: only a suffix that could begin the marker needs to be buffered
    (unconditional hold-backs would silently drop up to len(marker)-1
    legitimate trailing chars at stream end).
    """
    n = len(_DSML_MARKER)
    for l in range(min(len(s), n - 1), 0, -1):
        if s[-l:] == _DSML_MARKER[:l]:
            return l
    return 0


def _dsml_close_prefix_len(s: str) -> int:
    """Longest suffix of ``s`` that is a prefix of the DSML closing tag.

    Used while discarding an unterminated block: the closing tag
    ``</||DSML||tool_calls>`` can itself be split across stream chunks, so
    its trailing prefix must be buffered between chunks or the close would
    never be recognized and the rest of the turn would be discarded.
    """
    n = len(_DSML_TOOL_CALLS_CLOSE)
    for l in range(min(len(s), n - 1), 0, -1):
        if s[-l:] == _DSML_TOOL_CALLS_CLOSE[:l]:
            return l
    return 0

def _strip_dsml_streaming(
    text: str, dsml_active: bool, dsml_tail: str = ""
) -> tuple[str, bool, str]:
    """Strip DSML markup from a streaming *text* chunk.

    Because DSML blocks can be split across many stream deltas, this helper
    tracks whether we are currently *inside* an unterminated DSML block.

    The DSML marker itself can also be split across chunk boundaries
    (DeepSeek streams 1-3 char deltas; conv 149ce886 reproduced `<||DSML||`
    arriving as ``<`` + ``||`` + ``DSML`` + ``||`` + ``tool`` + ...). A plain
    ``_DSML_MARKER in text`` check then never fires and the raw markup leaks
    into the user-visible answer (and, worse, marks the final synthesis
    pass as "produced content", so the loop ends with empty visible
    output). To close that gap the caller keeps a small trailing buffer
    (``dsml_tail``, at most ``max(len(_DSML_MARKER), len(_DSML_TOOL_CALLS_CLOSE))-1``
    chars) that is prepended to the next chunk so a marker — or the closing
    tag — split across the boundary is detected.

    Returns ``(cleaned_text, new_dsml_active, new_dsml_tail)``.
    """
    if not text and not dsml_tail:
        return text, dsml_active, dsml_tail

    # Prepend the held-back tail so a marker / close tag split across chunk
    # boundaries is detected on this chunk.
    combined = (dsml_tail or "") + (text or "")
    dsml_tail = ""
    text = combined

    if not text:
        return "", dsml_active, ""

    # If we are inside a DSML block from a previous chunk, look for the
    # closing </||DSML||tool_calls> tag.  Everything up to and including
    # it is DSML and must be discarded.
    if dsml_active:
        idx = text.find(_DSML_TOOL_CALLS_CLOSE)
        if idx != -1:
            text = text[idx + len(_DSML_TOOL_CALLS_CLOSE):]
            dsml_active = False
        else:
            # Still inside the DSML block — discard the chunk, but hold
            # back a trailing close-tag prefix so a close tag split across
            # chunks is still detected.
            _hold = _dsml_close_prefix_len(text)
            if _hold:
                return "", True, text[-_hold:]
            return '', True, ""

    # Fast path: no DSML marker at all and not currently inside a block.
    if not dsml_active and _DSML_MARKER not in text:
        # Hold back only the trailing chars that could begin the marker in
        # the NEXT chunk (split-marker detection) — never arbitrary text.
        _hold = _dsml_marker_prefix_len(text)
        if _hold:
            return text[:-_hold], False, text[-_hold:]
        return text, False, ""

    # Not (or no longer) inside a DSML block.
    # 1. Strip any *complete* tool_calls blocks.
    text = _DSML_TOOL_CALLS_RE.sub('', text)

    # 2. Check for a partial (unterminated) DSML opening.
    marker_idx = text.find(_DSML_MARKER)
    if marker_idx != -1:
        # Is there a matching close after the marker in THIS chunk?
        tail = text[marker_idx:]
        if _DSML_TOOL_CALLS_CLOSE in tail:
            # The close is present but _DSML_TOOL_CALLS_RE didn't match,
            # which means the opening tag is not <||DSML||tool_calls>.
            # Strip individual DSML tags from the tail.
            tail_cleaned = _DSML_ANY_TAG_RE.sub('', tail)
            text = text[:marker_idx] + tail_cleaned
        else:
            # Partial DSML — strip from the marker to the end of the chunk
            # and remember that we are inside a block. Hold a trailing
            # close-tag prefix so a close split across chunks is detected.
            _discarded = text[marker_idx:]
            text = text[:marker_idx]
            dsml_active = True
            _hold = _dsml_close_prefix_len(_discarded)
            if _hold:
                return text, True, _discarded[-_hold:]

    # 3. Clean up any remaining individual DSML tags (orphaned fragments
    #    that slipped through because their parent block was incomplete).
    if _DSML_MARKER in text:
        text = _DSML_ANY_TAG_RE.sub('', text)

    # Hold back the trailing chars that could begin the marker in the NEXT
    # chunk (split-marker detection). When we are inside a block there is
    # nothing to hold — everything is being discarded anyway.
    if dsml_active:
        return text, True, ""
    _hold = _dsml_marker_prefix_len(text)
    if _hold:
        return text[:-_hold], False, text[-_hold:]
    return text, False, ""

def _extract_dsml_tool_calls(content: str) -> tuple[str, List[Dict[str, Any]]]:
    """Extract DeepSeek-DSML-style inline tool calls from model content.

    Some models (notably DeepSeek) emit tool calls as XML-like markup inside
    the text stream instead of using native ``delta.tool_calls``. This function
    parses that markup, removes it from the displayed content, and returns the
    equivalent OpenAI-style ``tool_calls`` list so the agent loop can execute
    them.
    """
    if not content or '<｜｜DSML｜｜' not in content:
        return content, []

    cleaned = _DSML_TOOL_CALLS_RE.sub('', content).strip()
    tool_calls: List[Dict[str, Any]] = []
    seen_signatures: Set[tuple[str, str]] = set()

    for idx, invoke_match in enumerate(_DSML_INVOKE_RE.finditer(content)):
        tool_name = invoke_match.group(1).strip()
        invoke_body = invoke_match.group(2)

        args: Dict[str, Any] = {}
        for param_match in _DSML_PARAMETER_RE.finditer(invoke_body):
            param_name = param_match.group(1).strip()
            string_attr = (param_match.group(2) or '').strip().lower()
            param_value = param_match.group(3)

            if string_attr == 'false':
                try:
                    args[param_name] = json.loads(param_value)
                except (json.JSONDecodeError, ValueError):
                    args[param_name] = param_value
            else:
                args[param_name] = param_value

        arguments_json = json.dumps(args, ensure_ascii=False)
        signature = (tool_name, arguments_json)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        tool_calls.append({
            "id": f"dsml_{tool_name}_{_DSML_CALL_NONCE}_{next(_DSML_CALL_COUNTER):05d}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": arguments_json,
            },
        })

    return cleaned, tool_calls


class _SessionContext:
    """Backwards-compatible async context manager wrapping an existing session."""
    def __init__(self, session: Any):
        self._session = session
    async def __aenter__(self):
        return self._session
    async def __aexit__(self, *args):
        pass


class IterationBudget:
    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        async with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    async def refund(self) -> None:
        async with self._lock:
            if self._used > 0:
                self._used -= 1

    async def reset(self) -> None:
        """Restore the full iteration budget (fresh budget for a new turn)."""
        async with self._lock:
            self._used = 0

    async def get_remaining(self) -> int:
        async with self._lock:
            return self.max_total - self._used

    async def get_used(self) -> int:
        async with self._lock:
            return self._used


@dataclass
class ToolCallResult:
    call_id: str
    name: str
    arguments: dict
    result: str
    error: bool = False


def _inject_directive(state: "AgentLoopState", content: str, *, enabled: Optional[bool] = None, **extra: Any) -> None:
    """Inject an internal agent directive into the in-flight message list.

    4.8 synthetic part (opencode analogue): internal instructions (turn focus,
    audit guidance, nudges, deathmatch continuations, grace prompts) are NOT
    user input. When ``agent_synthetic_directives_enabled`` (default True),
    they enter the model context as role="system" messages flagged
    ``synthetic: True`` — keeping the user role for genuine user turns and
    letting history rebuilds skip them naturally. When disabled, falls back
    to the legacy role="user" injection byte-for-byte.
    """
    if enabled is None:
        enabled = bool(config.agent_synthetic_directives_enabled)
    if enabled:
        state.messages.append({"role": "system", "synthetic": True, "content": content, **extra})
    else:
        state.messages.append({"role": "user", "content": content, **extra})


# Shipped when the audit rejection budget is spent AND the bounded salvage
# regeneration also fails its single audit (or errors out). An honest,
# actionable failure notice — never the just-rejected draft (conv 97ff355d).
# Wording (2026-08-14, conv a67faa04): "草稿没有发出" read as "nothing was
# sent" although the notice itself IS the final persisted content; state
# plainly that no publishable answer was produced and rejected drafts are
# never saved.
_AUDIT_SALVAGE_FAILURE_TEXT = (
    "回答生成失败：本轮回答多次未通过内部质量校验，未能生成可发布的回答（被拒的草稿不会被保存）。\n\n"
    "请重新发送消息重试；若多次失败，请简化问题或稍后再试。"
)

# Best-of-N stash cap (conv 7dc7a0d5): the reject budget bounds audits per
# turn (reject_budget + soft_limit, search rounds up to 4+3), so 8 covers the
# worst case with headroom; pop-oldest keeps memory flat if budgets are raised.
_AUDIT_STASH_LIMIT = 8

# Honest caveat prepended to the DETERMINISTIC fallback draft (selector LLM
# also failed — the shipped text is a rejected draft, the user must know its
# verification status; honest, but not a bare "generation failed" wall).
_AUDIT_SELECTION_CAVEAT = (
    "（说明：本回答经过多轮修正仍未完全通过内部质量校验，以下为按可用性排序选出的"
    "最接近可用的版本，请谨慎核对关键数字与事实依据。）\n\n"
)


# Appended to every audit-rejection guidance (conv 7dc7a0d5 evening wave):
# the regenerated draft MUST be complete and standalone — the user never sees
# rejected drafts or audit opinions, so delta-style text ("修正如下"/"维持
# 上一版不变"/"审计提示") shipped from a rewrite loop is meaningless to them.
_AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE = (
    "重写时必须输出完整独立、可直接发布的回答全文——严禁出现“修正”“上一版”"
    "“之前草稿”“审计/质检”等字样或任何对被拒草稿的指代"
    "（用户看不到被拒草稿与审计意见）。"
)

# Follow-up user message for the ONE bounded bad_json retry (conv 7dc7a0d5
# evening): truncation is the dominant bad_json cause, so the retry demands
# strict brevity. Role=user keeps provider-agnostic (no mid-sequence system).
_AUDIT_RETRY_BREVITY_HINT = (
    "你上一次的输出未能解析为完整JSON（疑似超长被截断）。请重新判定并严格控制篇幅："
    "problem 不超过80字；unsupported_claims 最多3条、每条 claim 不超过40字；"
    "只输出完整合法的JSON，不要任何其他内容。"
)

# Per-turn settled-verdict ledger injected into the AUDITOR's context
# (conv efaf8f9c 2026-08-20: the auditor re-litigated the same datum each
# round — VGM 32GB -> 48GB -> ~32GB+GTT — an unsatisfiable loop). The
# auditor must NOT flip-flop settled items: a draft that complied with the
# settled corrections is not rejectable on that item; only contradiction
# with ALL visible evidence entries is rejectable. When evidence entries
# themselves conflict (source A 48GB vs source B ~32GB), a draft that
# lists both sources with the difference is ACCEPTABLE — reject is reserved
# for contradicting every visible source. SOTA: VRR-Stop (arXiv 2607.17641,
# verifier votes contradicting settled verdicts are noise), Pan 2026-05
# ("freeze the acceptance criteria"), DISC (arXiv 2606.21724).
_AUDIT_SETTLED_HEADER = "已定案修正项（本轮前几次审计已判定并要求修正的内容，不得翻案）"
_AUDIT_SETTLED_RULES = (
    "【不得翻案】草稿若已按上述修正项修改，即使你个人对证据有不同解读，也不得因此再 reject；"
    "只有草稿与【所有】可见证据条目均矛盾才可 reject。\n"
    "【新证据推翻】若你发现本轮新出现的证据条目与某已定案修正项直接矛盾"
    "（该定案项的修正值本身被更权威的证据推翻），不得按旧定案项判 reject——"
    "判 unverifiable 并说明证据冲突与建议并列标注来源差异。\n"
    "【证据冲突出口】若台账中不同条目对同一数值/事实给出不同值（如来源A 48GB、来源B ~32GB），"
    "草稿并列标注各来源及其差异属于合格回答，判 accept；若草稿单方面采用其一而未标注其他来源，"
    "判 unverifiable（建议并列标注来源差异），不得判 reject。"
)

# Localized-revision clause appended to every audit-reject guidance
# (conv efaf8f9c 2026-08-20: full regeneration each round destroyed fixed
# content and introduced new errors — whack-a-mole). SOTA: DISC corrector
# leaves unflagged content unchanged; arXiv 2607.13347 target-preservation
# failure under unconstrained regeneration; VeriHarness admissible
# alternatives (arXiv 2607.14167).
_AUDIT_GUIDANCE_LOCALIZED_CLAUSE = (
    "【修正方式】只修改上面点名的内容；草稿中其余未被点名且未被审计判定的内容保持原样，"
    "严禁整篇重写——整篇重写会破坏已修正的内容并引入新的错误。"
    "修正后必须输出完整独立、可直接发布的回答全文。"
)

# Writer-side cumulative settled list (conv efaf8f9c: _prune_guardrail_pairs
# kept only the LATEST reject directive, so the writer forgot earlier fixes
# and re-broke them). Compact prefix; appended before the current problem.
_AUDIT_GUIDANCE_SETTLED_PREFIX = "【已定案修正项（必须遵守）】"


def _trim_to_sentence_boundary(text: str) -> str:
    """Cut a truncated partial at the last sentence/line boundary (A4.9 M6) —
    a caveat-prefixed partial must not end mid-word. Falls back to the full
    text when no boundary exists."""
    _bounds = list(_re.finditer(r"[。！？!?\n]", text))
    if not _bounds:
        return text
    return text[: _bounds[-1].end()].rstrip()


def _strip_generation_leakage(text: str) -> str:
    """Post-generation hygiene shared by salvage / selection regenerations:
    strip <think> blocks, tool-call DSL and DSML from a tools-disabled single
    pass (mirrors the _final_thinking cleanup)."""
    if "<think" in text:
        text = _re.sub(r"<think[^>]*>.*?</think\s*>", "", text, flags=_re.DOTALL)
        if "<think" in text:
            text = _re.sub(r"<think[^>]*>.*", "", text, flags=_re.DOTALL)
    text = _TOOL_CALL_RE.sub("", text)
    text = _TOOL_INVOKE_RE.sub("", text)
    text = _TOOL_CALL_XML_RE.sub("", text)
    text = _TOOL_RESULT_RE.sub("", text)
    return _strip_dsml_all(text).strip()


def _audit_reject_budget_for(state: "AgentLoopState") -> int:
    """本轮的审计拒绝预算（[agent.audit] reject_budget / reject_budget_search）：
    search_required 轮次上下文更大、更易跑偏，默认多给两次修正机会。"""
    if state.search_required:
        return config.agent_audit_reject_budget_search
    return config.agent_audit_reject_budget


_GUARDRAIL_EPHEMERAL = frozenset({"response_audit", "search_demand", "tool_demand"})


def _settled_items_view(state: "AgentLoopState", limit: int = 6, evidence_text: Optional[str] = None) -> str:
    """Compact list of settled rejection problems (conv efaf8f9c 2026-08-20).

    Derives from the existing best-of stash (``audit_rejected_drafts``, which
    holds verdict/problem/unsupported_claims per rejected draft). Injected
    into BOTH the auditor context (so it cannot flip-flop on settled items)
    and the writer guidance (so the writer sees the full cumulative fix list
    even though _prune_guardrail_pairs keeps only the latest directive).
    Cap at ``limit`` entries, newest-first order; empty stash → "".

    2026-08-22 (conv 3b58af5b): two hardening fixes —
    1. Only verdict=="reject" entries are settled corrections. Soft verdicts
       (needs_evidence/unverifiable) are "please verify" notes, NOT settled
       findings; including them made the auditor treat a gate-downgraded
       phantom rejection as 已定案 and repeat it every round.
    2. When ``evidence_text`` is given, a settled reject whose flagged claim
       tokens now appear in the CURRENT ledger is dropped — new evidence
       satisfies the item deterministically instead of relying on the weak
       auditor's "新证据推翻" prompt rule.
    """
    stash = list(getattr(state, "audit_rejected_drafts", []) or [])
    lines: List[str] = []
    _seen: set = set()
    for _e in reversed(stash[-limit:]):
        if not isinstance(_e, dict):
            continue
        if _e.get("verdict") != "reject":
            continue
        _p = (_e.get("problem") or "").strip()
        if not _p:
            continue
        _p = _p[:200]
        _claims = _e.get("unsupported_claims") or []
        # Evidence-satisfied settled items are dropped deterministically:
        # if the flagged claim's distinctive tokens are in the current
        # ledger, the item is grounded now — it cannot stay 已定案.
        if evidence_text:
            _claim_texts = []
            if isinstance(_claims, list):
                for _c in _claims[:3]:
                    if isinstance(_c, dict) and str(_c.get("claim") or "").strip():
                        _claim_texts.append(str(_c["claim"]))
            if _claim_texts:
                _toks = set()
                for _ct in _claim_texts:
                    _toks |= _extract_claim_tokens(_ct)
                _ev_l = evidence_text.lower()
                if _toks and all(_claim_token_hit(t, evidence_text, _ev_l) for t in _toks):
                    continue
        if _p in _seen:
            continue
        _seen.add(_p)
        _cv = ""
        if isinstance(_claims, list) and _claims:
            _parts = []
            for _c in _claims[:3]:
                if isinstance(_c, dict):
                    _ct = str(_c.get("claim") or "")[:120].strip()
                    if _ct:
                        _parts.append(_ct)
            if _parts:
                _cv = "（无依据声称：" + "；".join(_parts) + "）"
        lines.append(f"- {_p}{_cv}")
    return "\n".join(lines)


_ORPHAN_PUNCT = "，；。"


def _strip_leading_orphan_punct(text: str) -> str:
    """Strip exactly ONE leading orphan full-width punctuation (，；。).

    Defense for the qwen3.8_27b@vLLM first-token glitch family (conv
    e7d51dcb 2026-08-19 bare '：' — persistence-layer strip; conv efaf8f9c
    2026-08-21 复盘: drafts opened with '，但属于…' and the auditor rejected
    five regenerations for the same grammar defect). No legitimate prose
    answer starts with one of these three marks, and no pipeline stage can
    introduce one. Guards: chained punctuation (……/。。/，！) is NOT stripped
    (legit in creative continuations); a punctuation-only string is kept
    (never empty out a message); leading whitespace preserved.
    """
    if not text:
        return text
    i = 0
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if not (i < len(text) and text[i] in _ORPHAN_PUNCT):
        return text
    j = i + 1
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    if j < len(text) and text[j] in _ORPHAN_PUNCT + "！？…":
        return text  # chained punctuation — not an orphan glitch
    if not text[i + 1:].strip():
        return text
    return text[:i] + text[i + 1:]


_NUM_ASSERT_RE = _re.compile(r"应为\s*([0-9]+(?:\.[0-9]+)?)")


def _extract_asserted_numbers(text: Optional[str]) -> List[str]:
    """A1: the auditor's asserted correction values (「应为 X」pattern).

    Mechanical containment check, NOT a semantic judgment (user principle
    2026-07-20 stays intact): we never override the auditor's quality
    opinion — we only verify whether the number it asserts as truth is
    actually visible in the evidence ledger it was required to cite.
    """
    if not text:
        return []
    return _NUM_ASSERT_RE.findall(text)


def _apply_numeric_gate(verdict: str, problem: str, evidence_text: Optional[str]) -> tuple[str, str]:
    """A1 (2026-08-21, conv efaf8f9c): deterministic backstop for numeric
    audit verdicts. A reject whose asserted correction value appears nowhere
    in the evidence ledger is the auditor's own mental arithmetic (the
    template's 【数字核对硬性约束】 already forbids it) — an unsatisfiable
    loop (writer was RIGHT at 15.5GiB, auditor hallucinated 12.5GiB, five
    consecutive rejects). Downgrade to needs_evidence + a directive to cite
    ledger evidence or run execute_code.
    """
    if verdict != "reject":
        return verdict, problem
    if not config.agent_audit_numeric_gate_enabled:
        return verdict, problem
    _nums = _extract_asserted_numbers(problem)
    if not _nums:
        return verdict, problem
    _ev = evidence_text or ""
    # Token-boundary containment (A4.9 Minor-1): 「12.5」 must NOT match
    # 「112.5」/「12.56」/「12.5%」… — only the asserted number itself.
    for n in _nums:
        if _re.search(r"(?<![0-9.])" + _re.escape(n) + r"(?![0-9.])", _ev):
            return verdict, problem
    _downgraded = (
        "【数值核对闸门】审计断言值未见于证据台账，无法构成有效驳回依据"
        "（心算不得作为 reject 依据）。请调用 execute_code 实际计算验证该数值，"
        "或在回答中引用证据台账中可见的来源；原审计意见：" + problem
    )
    return "needs_evidence", _downgraded


# A5 (2026-08-22, conv 3b58af5b): claim-grounding gate for the
# 「无依据声称」rejection family — the A1 numeric gate's blind spot. The
# auditor flags a draft claim as "no basis in evidence"; if the claim's
# distinctive tokens (numbers, identifiers) DO appear in the evidence
# ledger the auditor was given, the claim IS grounded and the rejection is
# auditor blindness (the 12.5GiB disease in different clothes). Same
# philosophy as A1: mechanical containment check, never a semantic
# judgment — downgrade only, never upgrade.
# A4.9 wave-2 Important-1: token containment adopts A1's boundary
# discipline — identifiers must match on word boundaries (YaRN must not
# match YaRNScaling…, beta_fast must not match beta_fast2) and numbers
# must be ≥ 2 digits (single digits like "1" appear in virtually every
# ledger; "32GiB" must not match a bare "32" elsewhere). Long numbers are
# kept whole (never split).
_CLAIM_TOKEN_RE = _re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}|\d{2,}(?:\.\d+)?")
# Tokens too generic to be distinctive evidence markers (stopword-level):
# they appear in nearly every ledger and would false-downgrade rejections.
_CLAIM_TOKEN_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "these", "those",
    "were", "was", "are", "has", "have", "had", "its", "their", "your",
    "you", "our", "not", "but", "also", "then", "when", "what",
    "which", "who", "whom", "where", "how", "why", "will", "would", "can",
    "could", "should", "may", "might", "must", "per", "via", "into", "onto",
    "over", "under", "about", "against", "between", "through", "during",
    "before", "after", "above", "below", "used", "using", "use", "data",
    "model", "models", "config", "configuration", "context", "cache",
    "token", "tokens", "value", "values", "number", "numbers", "result",
    "results", "output", "input", "type", "types", "key", "keys", "page",
    "pages", "text", "content", "file", "files", "name", "names", "list",
    "info", "information", "http", "https", "www", "com", "html", "htm",
})


# 盲区①（wave-2 审计 2026-08-23，纯中文声称闸门失效）：主通道为空时的
# 「数字+单位」回退通道。主通道只认 ASCII 标识符与 ≥2 位整数；纯中文声称
# （「每token显存计算」家族）或单数位数字+单位值（8万/5.8亿/3%/3KB）对它
# 完全不可见 → token 集为空 → 闸门 fail-closed。注意：TiB/GiB/MiB/KiB 类
# 3 字节单位会被主通道当作标识符提取（如「显存3GiB」→{GiB}），永远到不了
# 本通道——本通道字节单位只需覆盖 2 字母形（TB/GB/MB/KB）。单位锚定语境使
# 这些 token 足够区分性；裸单数字不可见。仅在主通道集为空时启用。
# 交替序纪律（A4.9 wave-3）：带单位的分支必须在裸小数之前（否则「5.8%」被
# 捕获为裸「5.8」，跨语境裸值会假接地——Important-1）；复合数量词最长优先
# （万亿|千亿|…先于亿/万/千，「3万亿」不得捕获成「3万」——Important-2）。
_CLAIM_NUMUNIT_RE = _re.compile(
    r"\d+(?:\.\d+)?(?:TB|GB|MB|KB)"
    r"|\d+(?:\.\d+)?(?:万亿|千亿|百亿|千万|百万|十万|亿|万|千|百|%|％|元|秒|分钟|小时|天|周|月|年|倍)"
    r"|\d+\.\d+",
    _re.IGNORECASE,
)


def _extract_claim_tokens(text: Optional[str]) -> set:
    """Distinctive tokens from an auditor-flagged claim: identifiers
    (beta_fast, yarn_scaling_rope, fp8) and small numbers (32, 1, 5.8),
    minus stopwords. Used by the A5 claim-grounding gate.

    盲区① fallback: when the primary channel yields NOTHING (pure-Chinese
    claims, single-digit values that only carry meaning via a unit), a
    number+unit pass (8万 / 5.8亿 / 3% / 3KB / 5.8) fills the set so the
    mechanical containment check can still run; claims with no extractable
    tokens at all stay fail-closed.
    """
    if not text:
        return set()
    toks = set()
    for m in _CLAIM_TOKEN_RE.finditer(text):
        t = m.group(0)
        if t.lower() in _CLAIM_TOKEN_STOPWORDS:
            continue
        toks.add(t)
    if not toks:
        for m in _CLAIM_NUMUNIT_RE.finditer(text):
            toks.add(m.group(0))
    return toks


def _claim_token_hit(t: str, evidence_text: str, evidence_lower: Optional[str] = None) -> Optional[object]:
    """Boundary-aware token containment for the A5 gate and the settled-view
    evidence-satisfied drop (shared, A4.9 wave-2 Important-1 + Minor-3):
    - identifiers (beta_fast, YaRN, yarn_scaling_rope): case-insensitive,
      LEFT boundary strict (must not match mid-identifier — "fast" must not
      match "beta_fast"); RIGHT side MAY continue because identifier
      families compound — "YaRN" matches "YaRNScalingRotaryEmbedding".
      Hyphen is EXCLUDED from the left-boundary class (A4.9 wave-2b
      Important-1): flag-form tokens like "rope-scaling" appear in ledgers
      as "--rope-scaling" — a preceding dash is a legitimate anchor, not a
      mid-identifier splice. Keeping [A-Za-z0-9_.] still blocks "fast"→
      "beta_fast".
    - numbers (32, 1, 5.8): non-digit boundaries BOTH sides (A1 rule) —
      "32" must not match "132"/"32GiB"/"32.5". IGNORECASE so number+unit
      fallback tokens (盲区①) match case-variant evidence ("64kb" vs
      "64KB"); pure digits are case-inert — no behavior change for A1.
    """
    if not evidence_text:
        return None
    if t[:1].isdigit():
        return _re.search(
            r"(?<![0-9.])" + _re.escape(t) + r"(?![0-9.])",
            evidence_text,
            _re.IGNORECASE,
        )
    _lower = evidence_lower if evidence_lower is not None else evidence_text.lower()
    return _re.search(
        r"(?<![A-Za-z0-9_.])" + _re.escape(t.lower()),
        _lower,
    )


def _apply_claim_grounding_gate(
    verdict: str, problem: str,
    unsupported_claims: Optional[list], evidence_text: Optional[str],
) -> tuple[str, str]:
    """A5 (2026-08-22, conv 3b58af5b): deterministic backstop for the
    「无依据声称」(unsupported-claims) rejection family.

    conv 3b58af5b: the auditor rejected drafts #2-5 for「无依据声称（YaRN
    默认参数及每token显存计算）」even AFTER the writer fetched the vLLM
    source into the ledger — `beta_fast: int = 32, beta_slow: int = 1`
    (browser digest, 19:46:48). The A1 numeric gate only matched「应为 X」
    assertions, so this grounded claim family looped to budget exhaustion
    and the deterministic fallback shipped a draft with a KNOWN error
    (rotary_embedding.py vs evidence yarn_scaling_rope.py).

    Mechanism: extract distinctive tokens from the flagged claims (or, if
    the auditor gave no claim list, from the problem text's parenthetical);
    if ANY token appears in the evidence ledger the auditor was given, the
    claim is grounded → downgrade to needs_evidence (does NOT consume the
    reject budget). Contradiction-style rejections (「与证据矛盾」) are NOT
    gated — those cite visible evidence and are the auditor's legitimate
    catch (reject #5 file-path error stayed a reject).
    """
    if verdict != "reject":
        return verdict, problem
    if not evidence_text:
        return verdict, problem
    if "矛盾" in problem or "不一致" in problem:
        return verdict, problem
    if not any(k in problem for k in ("无依据", "无证据", "凭空", "编造")):
        return verdict, problem
    _claims_text = []
    if isinstance(unsupported_claims, list):
        for _c in unsupported_claims[:5]:
            if isinstance(_c, dict) and str(_c.get("claim") or "").strip():
                _claims_text.append(str(_c["claim"]))
    if not _claims_text:
        _m = _re.search(r"[（(]([^）)]*)[）)]", problem)
        if _m and _m.group(1).strip():
            _claims_text.append(_m.group(1))
    if not _claims_text:
        return verdict, problem
    _tokens = set()
    for _ct in _claims_text:
        _tokens |= _extract_claim_tokens(_ct)
    if not _tokens:
        return verdict, problem
    _ev = evidence_text or ""
    _ev_lower = _ev.lower()
    # 2026-08-23 (conv 3b58af5b 00:12-00:16): ALL-match, not ANY-match — a
    # claim that genuinely fabricated a PR number (PR#28006) and computed
    # the KV math wrong must NOT be downgraded just because generic domain
    # words (vLLM/rope-scaling) appear in the ledger from OTHER contexts.
    # The specific identifier (28006) is absent → the rejection stands and
    # the writer must actually fix the draft.
    if not all(_claim_token_hit(t, _ev, _ev_lower) for t in _tokens):
        return verdict, problem
    _downgraded = (
        "【声称核对闸门】审计所指“无依据声称”的全部关键标识/数值均已存在于"
        "证据台账（可核对），无法构成有效驳回依据。请直接引用台账中可见的来源"
        "作答，或删除无法引用的细节；原审计意见：" + problem
    )
    return "needs_evidence", _downgraded


# 兜底选稿源优先级（盲区③配套）：copy-edit 链草稿逐稿单调改进 → 新鲜再生
# 源（synthesis/salvage）靠后。
_FALLBACK_SRC_RANK = {"draft": 0, "synthesis": 1, "salvage": 2}
# 矛盾家族标记（与 A5 的矛盾排除同词表）：problem 指出与可见证据冲突 =
# 审计员的合法捕捉，该草稿含已知事实错误。
_DRAFT_CONTRA_MARKS = ("矛盾", "不一致")


def _draft_ground_key(
    e: dict, evidence_text: str, evidence_lower: Optional[str] = None,
) -> tuple:
    """Deterministic last-resort draft ranking key (best-of stash fallback).

    Returns ``(contradiction_flag, absent_count, src_rank)`` — min() wins.

    盲区③（wave-2 审计 2026-08-23）：矛盾类草稿（审计正确拒绝——声称与可见
    证据冲突，如文件路径写错）的 token 常以目录名/子串出现在证据路径中
    （rotary_embedding ⊂ rotary_embedding/yarn_scaling_rope.py）→ 假完美
    ``_absent=0`` 反超无 token 中性稿（floor=1），conv 3b58af5b 因此把含已知
    错误的错误路径稿选为兜底出货。contradiction 标志使其排到全部非矛盾稿
    之后（无论 absent 计数）。非矛盾语义不变：absent 少者胜（最修正稿）→
    source rank（draft < synthesis < salvage）。
    """
    _ct = []
    _cl = e.get("unsupported_claims") or []
    if isinstance(_cl, list):
        for _c in _cl[:3]:
            if isinstance(_c, dict) and str(_c.get("claim") or "").strip():
                _ct.append(str(_c["claim"]))
    if not _ct:
        _m = _re.search(r"[（(]([^）)]*)[）)]", str(e.get("problem") or ""))
        if _m and _m.group(1).strip():
            _ct.append(_m.group(1))
    _toks = set()
    for _c in _ct:
        _toks |= _extract_claim_tokens(_c)
    if not _toks:
        absent = 1
    else:
        _ev_l = evidence_lower if evidence_lower is not None else (evidence_text or "").lower()
        absent = sum(1 for t in _toks if not _claim_token_hit(t, evidence_text or "", _ev_l))
    _prob = str(e.get("problem") or "")
    contra = 1 if any(k in _prob for k in _DRAFT_CONTRA_MARKS) else 0
    _src = str(e.get("source") or "draft")
    return (contra, absent, _FALLBACK_SRC_RANK.get(_src, 1))


_AUDIT_STASH_REASONING_CHARS = 2000


_REVISION_SIMILARITY_MIN_CHARS = 300


def _is_full_rewrite(base: str, revision: str) -> bool:
    """用户要求（2026-08-21）：修正轮仅修改点名部分。机械比对修正稿与
    被拒稿的 difflib 相似度——低于阈值即整篇重写（底稿未保留）。短稿跳过
    （误伤成本高）；阈值 0 关闭。"""
    _thr = config.agent_audit_revision_min_similarity
    if _thr <= 0:
        return False
    if len(base) < _REVISION_SIMILARITY_MIN_CHARS or len(revision) < _REVISION_SIMILARITY_MIN_CHARS:
        return False
    from difflib import SequenceMatcher
    return SequenceMatcher(None, base, revision).ratio() < _thr


def _rejected_append(loop: "AgentLoop", state: "AgentLoopState", content: str, reasoning_content: str = "") -> None:
    """A2: append an audit-rejected draft to the in-flight context. On
    preserve-thinking providers the draft's reasoning_content is re-attached
    (capped) so the next generation continues/corrects its own reasoning
    instead of cold-restarting (Qwen preserve_thinking / DeepSeek
    reasoning_content contract / MiMo docs: missing history reasoning ->
    instruction-following degradation + hallucinations)."""
    _cap = config.agent_audit_retry_reasoning_keep_chars
    _pt = getattr(loop, "provider_type", "") or ""
    if (
        _pt in PRESERVE_THINKING_PROVIDERS
        and _cap > 0
        and reasoning_content
    ):
        state.messages.append({
            "role": "assistant",
            "content": content,
            "_rejected": True,
            "reasoning_content": reasoning_content[-_cap:],
        })
    else:
        state.messages.append({"role": "assistant", "content": content, "_rejected": True})


def _stash_rejected_draft(state: "AgentLoopState", content: str, guidance: "AuditVerdict", source: str, reasoning: str = "") -> None:
    """Append an audit-rejected draft to the best-of stash (conv 7dc7a0d5).

    `_prune_guardrail_pairs` keeps at most ONE rejected draft in
    state.messages (anti self-reinforcement, conv 97ff355d) — the stash is
    the separate, fuller record the last-resort selector reads. Called for
    BOTH the not-spent rewrite branch and the budget-spent branch (the
    draft that spends the budget is a candidate too), on the draft path and
    the synthesis path alike. Pop-oldest at _AUDIT_STASH_LIMIT.
    """
    state.audit_rejected_drafts.append({
        "content": content or "",
        # A2: reasoning excerpt (capped) so the last-resort selector sees the
        # rejected draft's derivation, not just its text.
        "reasoning": (reasoning or "")[-_AUDIT_STASH_REASONING_CHARS:],
        "verdict": getattr(guidance, "verdict", "reject") or "reject",
        # A4.9 (2026-08-20): store the PARSED problem, not the composed
        # guidance — the guidance carries writer directives + the settled
        # prefix (recursive nesting across rounds, cross-role instruction
        # pollution, and ≥3-reject rounds truncating the newest problem
        # past the 200-char view cut).
        "problem": getattr(guidance, "problem", "") or "",
        "unsupported_claims": list(getattr(guidance, "unsupported_claims", []) or []),
        "source": source,
    })
    if len(state.audit_rejected_drafts) > _AUDIT_STASH_LIMIT:
        state.audit_rejected_drafts.pop(0)


def _prune_guardrail_pairs(state: "AgentLoopState") -> None:
    """Keep at most ONE rejected draft + ONE guardrail directive in the
    in-flight context (2026-08-12 blind-spot wave).

    conv 97ff355d: every rejection appended the rejected draft as an
    assistant message plus a guidance directive — after 2+ rejections the
    model's own rejected claims stacked up in context and self-reinforced
    the hallucination ("用户消息是重复发送" ×4) faster than the auditor's
    correction could land. Called right BEFORE a new guardrail pair is
    appended: the model always sees at most the latest rejected draft and
    the latest guidance. turn_focus / length_continuation directives are not
    guardrail pairs and are preserved.
    """
    state.messages = [
        m for m in state.messages
        if not m.get("_rejected") and m.get("_ephemeral") not in _GUARDRAIL_EPHEMERAL
    ]


def _search_class_tool_used(state: "AgentLoopState") -> bool:
    """Whether ANY search-class tool ran this turn (web_search OR browser*).

    The auditor template (rule 4) treats web_search/browser as the same
    retrieval class, but the search-demand directive historically gated on
    ``web_search_count == 0`` only — so a turn that did 20 real browser
    retrievals was still told "你尚未调用 web_search 工具" (conv 97ff355d
    2026-08-12), a factually wrong directive that fed the model's confusion
    spiral. Tool-identity check, not content classification.
    """
    if state.web_search_count > 0:
        return True
    for tr in state.tool_results:
        name = getattr(tr, "name", "") or ""
        if name == "browser" or name.startswith("browser_"):
            return True
    return False


@dataclass
class AgentLoopState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[ToolCallResult] = field(default_factory=list)
    iterations: int = 0
    total_tool_calls: int = 0
    consecutive_tool_iterations: int = 0
    force_final_answer: bool = False
    executed_final_code: bool = False
    # Superseded by force_stage_rounds (A4.9 r1): the old schema gate
    # `force_final_answer and not executed_final_code` dropped tools to NONE
    # after the model's first final-code run, stranding mid-task models
    # (conv 517140ca). Kept as a field for external compatibility only.
    # Tool-calling rounds elapsed AFTER the force-final-answer guard fired
    # (A4.9 I1): execute_code refunds the iteration budget, so without this
    # hard cap the forced phase could run unbounded (net-zero consumption).
    # Once >= max_force_stage_rounds, execute_code is no longer offered and
    # the model must answer in text.
    force_stage_rounds: int = 0
    # Empty-answer retry counter (conv 517140ca 2026-08-08): how many
    # consecutive silent-empty iterations (no content, no tools, no error)
    # have been retried this turn. Bounded by
    # config.agent_tool_loop_max_empty_answer_retries so a truly dead
    # provider cannot loop forever.
    empty_answer_retries: int = 0
    # Retry runs with thinking disabled: a silent-empty response usually
    # means the reasoning pass burned the token budget on a huge context
    # (llm_empty_content family) — the smaller, no-think request recovers.
    retry_thinking_off: bool = False
    # 用户要求（2026-08-21）：审核打回后的修正轮不开思考。任一 verdict
    # （reject/needs_evidence/unverifiable）打回即置位——该轮后续所有
    # 修正迭代与合成 pass 均以 thinking 关闭运行（conv efaf8f9c 复盘：
    # 修正轮 xhigh 再思考在上游停滞时扩大了挂死面）。
    revision_thinking_off: bool = False
    # 差异闸门一次性标记（2026-08-21）：整篇重写的修正稿只拦截一次，
    # 二次修正即放行送审计（合法的大幅修改不被无限拦截）。
    revision_rewrite_flagged: bool = False
    # 2026-08-22（conv efaf8f9c 复盘）：本轮最终稿已被审计 accept（或 salvage
    # 通过）——canary trip 不得再触发重答（会产生措辞不同的重复生成 + 重复
    # 工具轮，用户看到多份正式回答）。SOTA：连贯且审计通过的回答漏标 =
    # 警告记录继续，不重答。
    audit_accepted: bool = False
    budget: Optional[IterationBudget] = None
    budget_grace_call: bool = False
    completed_normally: bool = False
    # Coordinator (LLM) judgments for this turn. ``turn_focus`` restates what
    # the user actually wants NOW (anti topic-anchoring); ``expects_tools`` is
    # the coordinator's judgment on whether answering requires tool calls —
    # used by the tool-nudge logic instead of regex classifiers.
    turn_focus: str = ""
    expects_tools: Optional[bool] = None
    # Coordinator's LLM judgment on whether THIS turn's user message explicitly
    # requests a web search (search_required: true/false in coordinator JSON).
    # Agentic replacement for regex-based user-intent classifiers (feedback
    # 2026-07-20: no regex intent classification — coordinator LLM judges).
    search_required: Optional[bool] = None
    # Coordinator's LLM judgment on whether THIS turn is creative writing
    # (story/novel/poem/essay creation, continuation or rewriting — including
    # plot-point injections into an ongoing story). Creative turns skip the
    # pre-send audit: criteria like "topic anchoring" and "verbosity" do not
    # apply to a story continuation (conv de876c13, 2026-08-06: the audit
    # rejected a legitimate continuation of the poisoning cliffhanger, chat.py
    # wiped the draft from persistence, and the user's long reply shrank to
    # the regenerated tail with a narrative gap).
    creative_turn: bool = False
    # The user's actual question for this turn, captured before any
    # system-generated user messages (guards/nudges) are appended, so the
    # final synthesis pass can anchor on it.
    turn_question: str = ""
    finish_reason: Optional[str] = None
    web_search_count: int = 0  # Track number of web_search tool calls
    max_web_searches: int = 9  # Maximum allowed web_search calls (3 rounds × 3 queries)
    # Deathmatch repetition detection
    recent_responses: List[str] = field(default_factory=list)
    repetition_count: int = 0
    # P2 4.8: Doom loop detection — track consecutive identical (tool_name, args_hash)
    # tool calls. Same tool+args 3x in a row triggers abort with warning.
    _doom_tool_history: List[tuple] = field(default_factory=list)
    # P2 4.8: Structured trace accumulator for OTel-style per-step instrumentation.
    _tool_trace: List[dict] = field(default_factory=list)
    # DSML streaming state: True when we are inside an unterminated
    # <｜｜DSML｜｜tool_calls> block that was split across stream chunks.
    dsml_active: bool = False
    # Trailing chars of the previous chunk that could begin a DSML marker
    # (len(_DSML_MARKER)-1 max). Rejoined with the next chunk so a marker
    # split across chunk boundaries is still detected.
    dsml_tail: str = ""
    # Accumulated assistant text content across all iterations in the
    # current turn. Used by tools (e.g. pdf_export export_conversation)
    # that need the in-progress response before it is persisted to the DB.
    turn_content_segments: List[str] = field(default_factory=list)
    # Activity-based timeout: reset whenever content/tool_call/tool_result
    # is produced. If no activity for inactivity_timeout_seconds, the turn
    # is considered stuck and terminated gracefully. 0 = disabled.
    last_activity_at: float = 0.0
    inactivity_timeout_seconds: float = 0.0
    # Deathmatch: consecutive inactivity-timeout→judge cycles. Each cycle
    # costs judge_timeout + inactivity_timeout of wall time; a permanently
    # dead provider must fail fast instead of spinning for the 7-day wall
    # budget (A4.9 review I5). Reset on any real activity (_mark_activity).
    consecutive_inactivity_cycles: int = 0
    # P1 4.4: Cooperative cancellation — set when the consumer stops reading
    # the event queue. Checked at tool execution boundaries to skip pending
    # tool calls and mark running ones as interrupted.
    cancelled: bool = False
    # 遵循词 canary: True after one miss-triggered compression ran for this
    # request — the per-request budget (cross-request budget is the tracker's
    # auto_disable_after).
    canary_compressed: bool = False
    # search_required=True 且模型未调 web_search 时，是否已注入过一次检索
    # 提醒 directive（conv 86e51bbd follow-up 2026-08-10）：每轮只提醒一次，
    # 防止顽固模型反复触发无限迭代（有界：最多注入一次后交给预算/审计）。
    search_demanded: bool = False
    # expects_tools=True 且模型连续多轮零工具调用时，是否已注入过一次
    # tool-demand directive（conv 41d2790d 2026-08-10）：每轮最多提醒一次，
    # 防止顽固模型反复触发无限迭代（有界，之后交给 rejection budget/审计）。
    tool_demanded: bool = False
    # Response-auditor rejection count for THIS turn (conv 41d2790d
    # 2026-08-10): the audit window was previously an ITERATION cap
    # (audit only iterations 1-2 of non-search turns), so the FINAL draft
    # of a 3-iteration turn shipped un-audited — exactly how the hollow
    # "上方有图/下方有表" summary escaped. Now the window is a REJECTION
    # budget: every no-tool draft is audited until it passes or the budget
    # is spent, so the last draft can never slip past the auditor.
    audit_rejections: int = 0
    # Soft-rejection counter (2026-08-14, conv a67faa04): unverifiable /
    # needs_evidence verdicts do NOT consume `audit_rejections` (truncated-
    # evidence "can't see" must not burn the reject budget into the failure
    # text path); capped separately by [agent.audit] soft_reject_limit.
    audit_soft_rejections: int = 0
    # Best-of-N stash (conv 7dc7a0d5, 2026-08-18): every audit-rejected draft
    # (draft path AND synthesis path, INCLUDING the budget-spending one) is
    # kept here in full — `_prune_guardrail_pairs` wipes the `_rejected`
    # copies from state.messages (anti self-reinforcement, conv 97ff355d)
    # but the stash survives so the last-resort selector can compare all
    # candidates with their rejection reasons. Bounded by _AUDIT_STASH_LIMIT.
    audit_rejected_drafts: List[dict] = field(default_factory=list)
    # Set when finish_reason=length truncates a tool-less answer: the next
    # iteration is the legitimate continuation tail, so the pre-send audit
    # (fabrication guard + LLM auditor) must skip exactly once — auditing the
    # fragment in isolation would wrongly reject it and force a regeneration.
    skip_audit_once: bool = False
    # Set after the audit-budget salvage replaced the shipped content
    # (conv 97ff355d 2026-08-12): the salvage IS already the final synthesis
    # from a de-poisoned context — running _final_thinking on top would
    # regenerate from the still-poisoned state.messages and could resurrect
    # the very hallucination the salvage just escaped.
    salvaged_final: bool = False
    # Turn-level citation ledger (grounded-citations port): assigns GLOBAL
    # ids to fetched URLs across all web_search rounds so [N] markers in the
    # final answer are unambiguous (cross-round collision fix) and verifiable
    # (sanitize removes ids that do not exist in the ledger).
    citation_ledger: Optional["CitationLedger"] = None
    # Per-turn memory-read dedup (conv dfc40619 2026-08-09): (action, target)
    # pairs of SUCCESSFUL memory reads this turn. The coordinator turn-focus
    # directive persists across all iterations and the system prompt's
    # mandatory_tool_use rule 8 ("即使你认为答案已经存在于历史里，仍然必须
    # 发起新的工具调用") force the model to re-read the same 35KB system
    # document every iteration — a second identical read short-circuits with
    # a small note instead of re-injecting the whole file into context.
    memory_read_targets: set = field(default_factory=set)


def _maybe_dedupe_memory_read(state: "AgentLoopState", tool_args: dict) -> Optional[str]:
    """Per-turn memory-read dedup guard (conv dfc40619 2026-08-09).

    The coordinator turn-focus directive ("需要读取 func.md") persists across
    ALL iterations of a turn, and the system prompt's mandatory_tool_use rule
    8 forces fresh tool calls even when the model already has the data — so
    DeepSeek re-issues the SAME ``memory read`` every iteration, re-injecting
    a 35KB document each time (observed: 2 identical reads in one turn).

    Returns the short deduplicated result JSON when this (action, target)
    read already succeeded this turn; None when the call must proceed.
    """
    action = str(tool_args.get("action") or "read").lower()
    target = str(tool_args.get("target") or "agent").lower()
    if action == "read" and ("read", target) in state.memory_read_targets:
        return json.dumps({
            "action": "read",
            "target": target,
            "deduplicated": True,
            "note": (
                f"该记忆目标 ({target}) 已在本轮对话中读取过，"
                "内容已包含在上一次工具结果中，请直接使用已有内容作答，无需重复读取。"
            ),
        }, ensure_ascii=False)
    return None


def _track_memory_read(state: "AgentLoopState", tool_args: dict, result: str) -> None:
    """Record a SUCCESSFUL memory read for dedup, or invalidate the cache on
    a write to the same target (the agent may legitimately re-read its own
    write — content changed). Failed reads are never cached so a retry of
    the same read still dispatches."""
    action = str(tool_args.get("action") or "read").lower()
    target = str(tool_args.get("target") or "agent").lower()
    if action == "read":
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict) and "error" in parsed:
                return
        except (json.JSONDecodeError, TypeError):
            return
        state.memory_read_targets.add(("read", target))
    elif action in ("add", "replace", "remove"):
        state.memory_read_targets.discard(("read", target))


def _clear_memory_read_cache(state: "AgentLoopState") -> None:
    """Conservatively drop the whole per-turn memory-read dedup cache.

    Called after a `delegate_task` returns (A4.9 review finding): the child
    agent runs its own AgentLoop with its own state, so a child write to
    `agent`/`user` memory only invalidates the CHILD's cache — a parent that
    read the target before delegating, then re-reads it after, would get a
    stale dedup note with no escape hatch. Clearing on delegation makes the
    parent's next read fetch fresh data."""
    state.memory_read_targets.clear()


def _tool_evidence_fragment(result: str, limit: int = 800) -> str:
    """Head+tail fragment of a tool result for the auditor context.

    conv a67faa04 (2026-08-14): head-only truncation hid tail-anchored
    evidence — the memory read of func.md is ~10.6k chars and its changelog
    section lives at the END of the file, so grounded drafts quoting that
    changelog were falsely accused of fabrication. Same 800-char budget as
    before (auditor is a repeated hot-path LLM call), but both ends are
    visible: `head … tail`.
    """
    if len(result) <= limit:
        return result
    half = (limit - 1) // 2
    return result[:half] + "…" + result[-half:]


_ARCHIVE_PATH_RE = _re.compile(r"(?:【全文存档】|Full output saved to:)\s*([^\s\]\n]+)")


def _read_text_file_sync(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as _f:
        return _f.read()


async def _load_full_tool_result(result_text: str) -> str:
    """Recover the FULL original tool result text for the auditor.

    The digest layer (tool_result_digest) replaces large results with
    <tool-digest> envelopes whose full text is persisted at 【全文存档】, and
    the budget layer (tool_result_budget) with <persisted-output> envelopes
    (Full output saved to:). Both keep an on-disk pointer — read it back so
    the auditor sees the SAME evidence the model saw (conv a67faa04
    2026-08-14: head-only truncation hid func.md's tail changelog and
    grounded drafts were falsely accused of fabrication).

    User principle (2026-08-14): information integrity > saving tokens —
    read-back is the default, not the exception. On any read failure the
    envelope text is kept as fallback (never empty evidence).
    """
    if not result_text:
        return result_text
    m = _ARCHIVE_PATH_RE.search(result_text)
    if not m:
        return result_text
    fpath = m.group(1)
    try:
        text = await asyncio.to_thread(_read_text_file_sync, fpath)
    except Exception as exc:
        logger.warning("audit evidence read-back failed for %s: %s", fpath, exc)
        return result_text
    return text or result_text


_AUDIT_GROUNDING_TOOLS = frozenset({"memory", "workspace_read", "web_search", "browser", "workspace_glob"})

# Non-evidence share of the audit prompt: system template (~1.6k tok) +
# user msg/turn_focus/tools list (~1k) + prev-answer window (~2.5k) +
# draft head+tail window (~2k) + hard-constraint injections (~0.3k) +
# the provider-default output budget (~4k). The clamp keeps the TOTAL audit prompt
# under the provider context ceiling (A4.9 M8: 128k evidence + the rest
# would 400 on a 128k-context provider, fail-opening past the graceful
# truncation→unverifiable path).
_AUDIT_NON_EVIDENCE_RESERVE_TOKENS = 12000


def _audit_evidence_budget() -> int:
    """Effective evidence token budget for one audit call (A4.9 M8):
    min(configured evidence budget, context ceiling − non-evidence reserve),
    floored at 4000 so a misconfigured tiny ceiling still leaves digest
    envelopes visible. Ceiling configurable via [agent.audit]
    evidence_context_ceiling_tokens (default 128000 ≈ DeepSeek context)."""
    return max(
        4000,
        min(
            config.agent_audit_max_evidence_tokens,
            config.agent_audit_evidence_context_ceiling_tokens - _AUDIT_NON_EVIDENCE_RESERVE_TOKENS,
        ),
    )


async def _build_audit_evidence(
    state: "AgentLoopState",
    budget: Optional[int] = None,
) -> "Tuple[str, str]":
    """(evidence_ledger, evidence_text) for the auditor.

    L1 ledger: deterministic, always given — one line per this-turn tool
    result with tool name, token size (CJK-aware), and visibility status
    (完整/压缩/截断). L2 evidence: grounding-class results get FULL text
    (disk read-back, token-budgeted); over-budget grounding entries fall
    back to their existing digest envelope (full-document compression, not
    amputation) or head+tail fragment, marked 截断/压缩 in the ledger so the
    auditor knows the cut boundary — claims beyond it are unverifiable,
    never fabrication (Galtea rule, conv a67faa04).
    """
    from app.services.context_compressor import estimate_text_tokens_rough

    if budget is None:
        budget = _audit_evidence_budget()
    ledger_lines: List[str] = []
    evidence_blocks: List[str] = []
    used_tokens = 0
    seen: set = set()
    items: List[ToolCallResult] = []
    for tr in state.tool_results[-12:]:
        _key = (tr.name, tr.result[:200])
        if _key in seen:
            continue
        seen.add(_key)
        items.append(("", tr))
    # Previous-turn tool evidence (conv 357c110d: drafts legitimately reuse
    # [N] citation numbers from earlier search rounds — the auditor must see
    # those results to verify "沿用历史编号且内容一致" instead of accusing
    # fabrication). Tool-role messages not already covered this-turn.
    for _m in reversed(state.messages[:-1]):
        if _m.get("role") != "tool" or len(items) >= 18:
            continue
        _content = str(_m.get("content") or "")
        _key = (_m.get("name") or "tool", _content[:200])
        if _key in seen:
            continue
        seen.add(_key)
        items.append(("(历史轮次) ", ToolCallResult(
            call_id=str(_m.get("tool_call_id") or ""),
            name=str(_m.get("name") or "tool"),
            arguments={},
            result=_content,
        )))
    items.sort(key=lambda it: (it[0] == "" and it[1].name not in _AUDIT_GROUNDING_TOOLS, it[0] == "", it[1].error))
    for idx, (label, tr) in enumerate(items, 1):
        raw = tr.result or ""
        _err_mark = " error" if tr.error else ""
        if tr.error or tr.name not in _AUDIT_GROUNDING_TOOLS:
            frag = _tool_evidence_fragment(raw)
            _tk = estimate_text_tokens_rough(frag)
            if used_tokens + _tk <= budget:
                ledger_lines.append(f"[{idx}] {label}{tr.name}{_err_mark} — {_tk} tokens — 片段(非grounding)")
                evidence_blocks.append(f"[{idx}] {label}{tr.name}{_err_mark}:\n{frag}")
                used_tokens += _tk
            else:
                ledger_lines.append(f"[{idx}] {label}{tr.name}{_err_mark} — 截断(超出预算，未展示)")
            continue
        full = await _load_full_tool_result(raw)
        _tk = estimate_text_tokens_rough(full)
        if used_tokens + _tk <= budget:
            ledger_lines.append(f"[{idx}] {label}{tr.name} — {_tk} tokens — 完整")
            evidence_blocks.append(f"[{idx}] {label}{tr.name}:\n{full}")
            used_tokens += _tk
        else:
            frag = _tool_evidence_fragment(raw)
            _fk = estimate_text_tokens_rough(frag)
            if used_tokens + _fk <= budget:
                ledger_lines.append(f"[{idx}] {label}{tr.name} — {_tk} tokens — 截断(仅头尾片段，原文已存档)")
                evidence_blocks.append(f"[{idx}] {label}{tr.name}:\n{frag}")
                used_tokens += _fk
            else:
                ledger_lines.append(f"[{idx}] {label}{tr.name} — {_tk} tokens — 截断(超出预算，未展示)")
    ledger = "<evidence-ledger>\n" + "\n".join(ledger_lines) + "\n</evidence-ledger>"
    return ledger, "\n\n".join(evidence_blocks)


@dataclass
class AuditVerdict:
    """Result of the pre-send audit. verdict ∈ accept | reject | unverifiable
    | needs_evidence. Only `reject` consumes the rejection budget (conv
    a67faa04: truncated-evidence "can't see" is NOT fabrication and must not
    burn the budget into the failure-text path); unverifiable/needs_evidence
    consume the soft-rejection counter instead."""
    verdict: str = "accept"
    guidance: str = ""
    problem: str = ""
    unsupported_claims: list = field(default_factory=list)


def _build_salvage_prompt(state: "AgentLoopState", last_user_msg: str) -> str:
    """Salvage regeneration prompt (module-level for testability).

    The honest-answer escape hatch (2026-08-14, conv a67faa04): the model
    fabricated a changelog four times instead of ever answering "I have no
    verifiable update log" — an answer that WOULD pass the auditor. Explicitly
    license the honest "cannot verify" answer in the salvage pass.
    """
    _q = (state.turn_question or last_user_msg or "").strip()
    prompt = (
        "请基于对话中已有的工具调用结果，直接、完整地回答用户本轮的问题：\n"
        f"「{_q[:600]}」\n"
    )
    if state.turn_focus:
        prompt += f"本轮意图聚焦：{state.turn_focus[:400]}\n"
    prompt += (
        "要求：\n"
        "- 直接输出最终答复正文，不要描述你打算怎么回答；\n"
        "- 不要提及质检、审计、消息重复或任何内部过程；\n"
        "- 工具结果只用于回答这个问题，与问题无关的信息不要写入回答；\n"
        "- 篇幅与问题匹配：事实型问题简短直接，分析型问题才可展开；\n"
        "- 若现有工具结果/上下文不足以如实回答，请直接说明无法获知"
        "（如“我没有可查证的更新记录”），严禁编造具体细节"
        "（日期、数字、版本号、提交记录、功能名等）。"
    )
    return prompt


def _build_selection_prompt(state: "AgentLoopState", last_user_msg: str, stash: List[dict]) -> str:
    """Best-of selection prompt (module-level for testability, conv 7dc7a0d5).

    The selector is a COMPARATIVE repair pass, not another absolute audit:
    it sees every rejected draft WITH its rejection reason and must produce
    the final answer by fixing the named defects on the closest-to-passing
    base. SOTA guardrails (2026-06): recovery must feed the SPECIFIC error,
    and the honest-unverifiable escape hatch must stay licensed — the
    selector's output passes ONE focused audit before shipping (bounded,
    no loop). conv 7dc7a0d5 evening: the output MUST be fully standalone —
    the user never saw any draft, so delta-style text ("修正后"/"维持上一版
    不变") is meaningless to them.

    Token budget (A4.9 I3): the full stash + full history can overflow the
    provider context. Drafts are estimated CJK-aware; when the drafts block
    exceeds the budget each draft is windowed (head+tail, explicitly
    annotated); if STILL over, the OLDEST drafts are dropped (annotated) —
    the newest drafts carry the most accumulated guidance. Truncation is
    always annotated (user principle 2026-08-14: 信息完整性>省token,
    截断必须显式标注).
    """
    from app.services.context_compressor import estimate_text_tokens_rough

    _q = (state.turn_question or last_user_msg or "").strip()

    def _render_drafts(entries: List[dict], per_draft_window: int = 0) -> List[str]:
        blocks = []
        for _i, _entry in enumerate(entries, 1):
            _verdict = _entry.get("verdict") or "reject"
            _problem = (_entry.get("problem") or "").strip() or "（未给出具体理由）"
            _claims = _entry.get("unsupported_claims") or []
            _claims_text = ""
            if _claims:
                _parts = []
                for _c in _claims[:5]:
                    if isinstance(_c, dict):
                        _parts.append(f"{_c.get('claim', '')}（证据状态：{_c.get('evidence_status', '?')}）")
                    else:
                        _parts.append(str(_c))
                _claims_text = "；无依据声称：" + "；".join(_parts)
            _src = _entry.get("source") or "draft"
            _src_label = {"draft": "草稿", "synthesis": "合成稿", "salvage": "重整稿"}.get(_src, "草稿")
            _content = (_entry.get("content") or "").strip()
            if per_draft_window > 0 and len(_content) > per_draft_window:
                _half = per_draft_window // 2
                _content = (
                    _content[:_half]
                    + f"\n【中段省略 {len(_content) - per_draft_window} 字符（上下文预算）】\n"
                    + _content[-_half:]
                )
            _reasoning = _entry.get("reasoning") or ""
            _reasoning_block = f"\n【思考摘录】{_reasoning}" if _reasoning else ""
            blocks.append(
                f"\n【{_src_label} {_i}】（审计判定：{_verdict}；问题：{_problem}{_claims_text}）\n"
                f"{_content}{_reasoning_block}\n"
            )
        return blocks

    _draft_budget = config.agent_audit_selection_draft_budget_tokens
    _entries = list(stash)
    _blocks = _render_drafts(_entries)
    if sum(estimate_text_tokens_rough(b) for b in _blocks) > _draft_budget:
        _window = config.agent_audit_selection_draft_window_chars
        _blocks = _render_drafts(_entries, per_draft_window=_window)
        while _entries and sum(estimate_text_tokens_rough(b) for b in _blocks) > _draft_budget:
            _entries = _entries[1:]
            _blocks = ([f"\n【更早的 {len(stash) - len(_entries)} 份草稿因上下文预算省略】\n"]
                       + _render_drafts(_entries, per_draft_window=_window))

    prompt = (
        "本轮回答在发送前的内部质量审计中被多次打回。以下是全部历史草稿与各自的审计意见，"
        "以及本轮已获取的工具调用结果（对话上文）。\n\n"
        f"用户本轮的问题：「{_q[:600]}」\n"
    )
    if state.turn_focus:
        prompt += f"本轮意图聚焦：{state.turn_focus[:400]}\n"
    prompt += "\n历史草稿与审计意见（按时间顺序）：\n" + "".join(_blocks)
    prompt += (
        "\n你的任务：综合评估以上草稿，以最接近通过审计的一份为基底，修正各审计意见"
        "指出的问题，输出一份最终回答。\n"
        "要求：\n"
        "- 直接输出最终答复正文，不要描述你打算怎么回答；\n"
        "- 输出必须是完整独立自足的回答全文：用户从未看到任何历史草稿，"
        "严禁差量式表述（如“修正后”“修正后的最终结论”“其余参数维持上一版不变”"
        "“如上表/如前所述”指代草稿内容的写法）；\n"
        "- 修正审计指出的具体问题：编号引用必须是上下文工具结果中真实存在的编号，"
        "数字与事实必须能在工具结果中核实；\n"
        "- 工具结果/上下文中无法核实的数字或事实，必须在回答中如实说明无法核实，"
        "严禁猜测或沿用审计已判定无依据的数值；\n"
        "- 若历史审计意见互相矛盾，或工具结果中不同来源对同一数值给出不同值，"
        "如实并列各来源及数值并说明差异——这是合格回答，不得单方面取舍或杜撰统一值；\n"
        "- 严禁提及质检、审计、草稿或任何内部过程。"
    )
    return prompt

class AgentLoop:
    #: Default reasoning effort for the iteration LLM when the user or
    #: assistant config does not specify one (kept low to control latency).
    def __init__(
        self,
        llm: LLMService,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
        max_iterations: int = None,
        workspace_path: str = "",
        enabled_toolsets: Optional[List[str]] = None,
        enable_compression: bool = False,
        blocked_tools: Optional[Set[str]] = None,
        delegation_depth: int = 0,
        provider_type: str = "deepseek",
        enable_reasoning: bool = False,
        reasoning_effort: str | None = None,
        thinking_budget: int | None = None,
        thinking_sampling: Optional[dict] = None,
        non_thinking_sampling: Optional[dict] = None,
        preserve_thinking: Optional[bool] = None,
        iteration_llm: Optional[LLMService] = None,
        iteration_provider_type: Optional[str] = None,
        iteration_extra_body: Optional[dict] = None,
        deathmatch_manager: Any = None,
        permission_callback: Any = None,
        session_factory: Any = None,
        identity_context: Optional[str] = None,
        coordinator_llm: Optional[LLMService] = None,
        canary_marker: Optional[str] = None,
        lazy_tools: Optional[List[str]] = None,
    ):
        self.llm = llm
        self.coordinator_llm = coordinator_llm or llm
        # JSON-mode (response_format={"type": "json_object"}) for structured
        # coordinator/auditor calls. DeepSeek, DashScope (OpenAI-compatible
        # mode) and MiMo all accept the identical parameter; providers that
        # reject it are detected at runtime and it is disabled for the rest
        # of this loop's lifetime.
        self._json_mode_supported = True
        # PHASE 3: iteration_llm is the (potentially cheaper / non-thinking)
        # client used for tool-calling iterations. _final_thinking always
        # uses self.llm so the user-facing synthesis still goes through the
        # main reasoner. Falls back to self.llm when no override is set.
        self.iteration_llm = iteration_llm or llm
        self.iteration_provider_type = iteration_provider_type or provider_type
        self.iteration_extra_body_override = iteration_extra_body
        self.provider_type = provider_type
        self.enable_reasoning = enable_reasoning
        self.reasoning_effort = reasoning_effort
        self.thinking_budget = thinking_budget
        # Qwen3.8(Local): per-mode sampling param sets (temperature/top_p/top_k/
        # min_p/presence_penalty/repetition_penalty) + preserve_thinking. None
        # for every other provider — sampling keeps existing behavior.
        self.thinking_sampling = dict(thinking_sampling or {})
        self.non_thinking_sampling = dict(non_thinking_sampling or {})
        self.preserve_thinking = preserve_thinking
        self.enabled_toolsets = enabled_toolsets
        if config.super_admin_bypass:
            self.blocked_tools = set()
        else:
            self.blocked_tools = blocked_tools or set()
        self.deathmatch_manager = deathmatch_manager
        self._permission_callback = permission_callback
        # Auditor-facing assistant name; run() overwrites it with the actual
        # assistant's name each turn (A4.9 M6: init here so non-run callers
        # never hit a missing attribute).
        self._audit_assistant_name = None
        if tool_schemas is not None:
            # Explicitly-provided schemas (sub-agent loops, PTC bridges) are
            # already curated by the caller — the global visible filter must
            # NOT silently strip tools the caller deliberately requested.
            self.tool_schemas = tool_schemas
        elif enabled_toolsets:
            allowed = set()
            for ts in enabled_toolsets:
                allowed.update(registry.get_tool_names_for_toolset(ts))
            self.tool_schemas = registry.get_definitions(allowed - self.blocked_tools)
        elif self.blocked_tools:
            all_names = set(registry.get_all_tool_names()) - self.blocked_tools
            self.tool_schemas = registry.get_definitions(all_names)
        else:
            self.tool_schemas = registry.get_definitions()
        # P2-2: visibleTools — global allowlist from [agent.tools] visible_tools.
        # Empty = all tools (default). When set, the model only ever sees the
        # listed schemas (prefill reduction + attention focus); dispatch
        # fail-closes on anything outside the set. Applies only to
        # registry-derived schema resolution (see note above).
        _visible = set(config.agent_tools_visible or [])
        self._visible_tool_names: Optional[Set[str]] = _visible or None
        if self._visible_tool_names is not None and tool_schemas is None:
            self.tool_schemas = [
                schema for schema in self.tool_schemas
                if (schema.get("function") or {}).get("name") in self._visible_tool_names
            ]
        # Names of tools the model can actually see/dispatch this run — used by
        # guardrails to avoid synthesizing calls to unavailable tools.
        self._active_tool_names: Set[str] = {
            (s.get("function") or {}).get("name", "")
            for s in self.tool_schemas
            if (s.get("function") or {}).get("name")
        }
        # Lazy tool loading (二期工具集按需发送): schemas for rarely-needed
        # tools are withheld on the first turn and appended the moment a
        # trigger tool (e.g. the browser entry) is actually called. The set
        # only grows, so the provider prefix cache is broken at most once per
        # session (the append turn); afterwards the schema is stable again.
        # Default None = legacy behavior (all schemas from the start).
        self._lazy_tool_names: Set[str] = set(lazy_tools or [])
        self._lazy_loaded = False
        self._lazy_schemas: List[Dict[str, Any]] = []
        if self._lazy_tool_names:
            self._lazy_schemas = [
                s for s in self.tool_schemas
                if (s.get("function") or {}).get("name") in self._lazy_tool_names
            ]
            self.tool_schemas = [
                s for s in self.tool_schemas
                if (s.get("function") or {}).get("name") not in self._lazy_tool_names
            ]
            self._active_tool_names = {
                (s.get("function") or {}).get("name", "")
                for s in self.tool_schemas
                if (s.get("function") or {}).get("name")
            }
        self.max_iterations = max_iterations or config.agent_tool_loop_max_iterations
        # PEVR: deathmatch goal loop gets an independent (larger) iteration
        # budget so deep loops aren't truncated by the normal 30-iter cap.
        # See loop_improve.md Phase 3.5 / §2.8.
        if self.deathmatch_manager is not None and self.deathmatch_manager.is_goal_active:
            self.max_iterations = config.deathmatch_tool_loop_max_iterations
        self.max_consecutive_iterations = config.agent_tool_loop_max_consecutive_iterations
        self.max_empty_answer_retries = config.agent_tool_loop_max_empty_answer_retries
        self.max_force_stage_rounds = config.agent_tool_loop_max_force_stage_rounds
        self.workspace_path = workspace_path
        # C3: first judge evaluation of a run counts as the user-initiated
        # turn (does not consume the deathmatch max_turns budget). chat.py
        # constructs one AgentLoop per user message and run() is called once,
        # so __init__ placement is equivalent to a per-run reset.
        self._dm_first_eval_is_user = True
        self.enable_compression = enable_compression
        # 遵循词 canary: ONLY enabled when the caller injected the marker into
        # the system prompt (chat.py main path). Worker/scheduler/delegate
        # loops never pass a marker and must never check for one.
        self._canary_marker = canary_marker
        self._canary_enabled = bool(canary_marker) and config.agent_canary_enabled
        if self._canary_enabled:
            from app.services.canary_marker import canary_tracker
            canary_tracker.auto_disable_after = config.agent_canary_auto_disable_after
        self._tool_call_timeout = config.agent_tool_loop_tool_call_timeout
        self._compressor = None
        self.delegation_depth = delegation_depth
        self.session_factory = session_factory
        self._identity_context = (identity_context or "").strip()
        self._budget_config = BudgetConfig(
            max_result_size_chars=int(config.agent_tool_loop.get("max_result_size_chars", 100_000)),
            turn_budget_chars=int(config.agent_tool_loop.get("turn_budget_chars", 200_000)),
            preview_chars=int(config.agent_tool_loop.get("preview_chars", 1_500)),
            persist_dir=None,
        )
        # Subagent digest layer: large content-heavy tool results (file reads,
        # web searches, browser snapshots) are reduced by parallel subagent
        # summarization into near-lossless <tool-digest> envelopes, with the
        # full text persisted to a file (lossless by pointer).
        self._digest_config = DigestConfig(
            enabled=config.agent_tool_digest_enabled,
            min_digest_chars=config.agent_tool_digest_min_chars,
            max_digest_chars=config.agent_tool_digest_max_chars,
            max_concurrent=config.agent_tool_digest_max_concurrent,
            max_tokens=config.agent_tool_digest_max_tokens,
            timeout_seconds=config.agent_tool_digest_timeout_seconds,
            batch_timeout_seconds=config.agent_tool_digest_batch_timeout_seconds,
            temperature=config.agent_tool_digest_temperature,
            model=config.agent_tool_digest_model,
            verify=config.agent_tool_digest_verify,
            digest_tools=frozenset(config.agent_tool_digest_tools) or DEFAULT_DIGEST_TOOLS,
        )

    def _skip_guardrails(self) -> bool:
        """In deathmatch mode, skip force_final_answer, max_consecutive_iterations,
        and web_search limit guards. The judge alone decides when to stop."""
        return (
            self.deathmatch_manager is not None
            and self.deathmatch_manager.is_goal_active
        )

    def _live_thinking_enabled(self) -> bool:
        """opencode-style live streaming: iterations run with thinking enabled
        and stream reasoning+content to the user immediately, instead of
        suppressing everything and regenerating the answer in a second full
        pass (_final_thinking). Kills the draft+synthesis double generation
        (TTFT was 76-92s on search turns, conv daa19eac investigation).

        Disabled when a DEDICATED subtask client serves iterations
        (``iteration_llm is not llm``): that client is configured by the
        operator as the cheap/non-thinking model (agent_service
        create_iteration_llm_service) — sending ``thinking: enabled`` to a
        non-thinking model would 400 every iteration (A4.9 review I1).
        """
        return (
            self.enable_reasoning
            and config.agent_tool_loop_live_thinking
            and self.iteration_llm is self.llm
        )

    def _sampling_kwargs(self, thinking: bool, provider_type: str | None = None) -> dict:
        """Sampling kwargs for the current mode. Empty unless the effective
        provider is qwen3.8_vllm (A4.9 I1: sampling sets must never leak to
        subtask/fallback providers) — callers fall back to existing defaults."""
        pt = provider_type or self.provider_type
        if pt != "qwen3.8_vllm":
            return {}
        return dict(self.thinking_sampling if thinking else self.non_thinking_sampling)

    def _detect_repetition(self, state: AgentLoopState, new_content: str) -> bool:
        """Check if the agent is repeating itself in deathmatch mode.
        Returns True if the new content is substantially similar to any recent response."""
        if not new_content or not new_content.strip():
            return False
        # Normalize: strip whitespace, take first 500 chars for comparison
        normalized = new_content.strip()[:500]
        for prev in state.recent_responses:
            prev_normalized = prev.strip()[:500]
            # Simple similarity: check if 70%+ of content overlaps
            if len(normalized) < 50 or len(prev_normalized) < 50:
                if normalized == prev_normalized:
                    return True
            else:
                # Check prefix similarity (first 300 chars)
                prefix_len = min(300, len(normalized), len(prev_normalized))
                if normalized[:prefix_len] == prev_normalized[:prefix_len]:
                    return True
        return False

    def _track_response(self, state: AgentLoopState, content: str) -> None:
        """Track a response for repetition detection. Keep last 3 responses."""
        if content and content.strip():
            state.recent_responses.append(content.strip())
            if len(state.recent_responses) > 3:
                state.recent_responses.pop(0)

    @staticmethod
    def _mark_activity(state: AgentLoopState) -> None:
        """Reset inactivity timer — call after any productive output."""
        if state.inactivity_timeout_seconds > 0:
            state.last_activity_at = asyncio.get_event_loop().time()
        # Real work happened — the consecutive inactivity-cycle counter is
        # only meaningful across genuinely dead stretches (A4.9 review I5).
        state.consecutive_inactivity_cycles = 0

    @staticmethod
    def _apply_citation_ledger(state: AgentLoopState, results: List[ToolCallResult]) -> None:
        """Register web_search hits into the turn-level citation ledger and
        RENUMBER the result's `formatted` text with GLOBAL ids.

        Grounded-citations port (hermes-agent): the system owns the
        url → [N] mapping, so the model only ever emits integers it was
        handed. Per-round numbering (each web_search restarts at 1) makes
        [N] ambiguous across rounds once results are flattened for the
        frontend — a [1] meant for round 2's first hit resolves to round
        1's first hit. The ledger assigns one monotonic id per normalized
        URL for the whole turn; the rewritten `formatted` is what the model
        sees (and what the digest envelope / persistence embeds), keeping
        the numbering the model cites consistent with the flattened
        results[] the UI maps [N] against.
        """
        if not results:
            return
        from app.services.citation_ledger import CitationLedger
        if state.citation_ledger is None:
            state.citation_ledger = CitationLedger()
        for r in results:
            if r.name != "web_search" or r.error:
                continue
            try:
                payload = json.loads(r.result or "")
            except (json.JSONDecodeError, TypeError):
                continue
            hits = payload.get("results")
            if not isinstance(hits, list) or not hits:
                continue
            queries = (r.arguments or {}).get("queries") if isinstance(r.arguments, dict) else None
            query = queries[0] if isinstance(queries, list) and queries else None
            ids = state.citation_ledger.register_hits(hits, query=query)
            payload["formatted"] = state.citation_ledger.format_hits(ids)
            r.result = json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _check_inactivity(state: AgentLoopState) -> bool:
        """Return True if the turn has been inactive (no content/tool output)
        longer than the configured threshold."""
        if state.inactivity_timeout_seconds <= 0:
            return False
        elapsed = asyncio.get_event_loop().time() - state.last_activity_at
        return elapsed > state.inactivity_timeout_seconds

    async def _safe_judge(self, dm, content, user_initiated, workspace_path, state=None):
        """Wrap evaluate_after_turn with a timeout to prevent judge LLM hangs.

        When ``state`` is given, the tool results produced since the previous
        judge call are forwarded so the verifier's progress detection can
        distinguish genuine research (new information gathered) from polling
        loops that only re-run the same commands.
        """
        tool_results = None
        # C3: the first judge evaluation of a run is the user-initiated turn
        # (the message that kicked off this run); every subsequent evaluation
        # inside this run is an autonomous continuation turn.
        if self._dm_first_eval_is_user:
            user_initiated = True
            self._dm_first_eval_is_user = False
        if state is not None:
            _start = getattr(state, "_dm_judged_tool_results", 0)
            tool_results = state.tool_results[_start:]
            state._dm_judged_tool_results = len(state.tool_results)
            logger.debug(
                "deathmatch judge tool_results slice: start=%d, new=%d, names=%s",
                _start, len(tool_results),
                [getattr(tr, "name", "?") for tr in tool_results][:6],
            )
        timeout = config.agent_tool_loop_judge_timeout
        if timeout <= 0:
            try:
                return await dm.evaluate_after_turn(
                    content,
                    user_initiated=user_initiated,
                    workspace_path=workspace_path,
                    tool_results=tool_results,
                )
            except Exception as exc:
                # Fail open even on the unbounded branch (A4.9 review I4):
                # an exception here must never propagate and kill the goal loop.
                logger.warning(
                    "Deathmatch judge raised %s (unbounded): %s — continuing via default",
                    type(exc).__name__, str(exc)[:200],
                )
                return {
                    "verdict": "continue",
                    "reason": f"judge_error: {type(exc).__name__}",
                    "should_continue": True,
                    "message": "评估异常，继续推进",
                    "continuation_prompt": (
                        "[死磕模式 — 评估异常，自主推进]\n"
                        "上一轮的完成度评估发生异常，请继续自主推进目标："
                        "检查已生成的产出文件是否齐全、字数是否达标，"
                        "如有遗漏或不足请直接补充完成；"
                        "若你认为所有产出均已交付完成，请明确输出最终交付汇总"
                        "（列出全部产出文件与字数），并声明全部步骤已完成。"
                        "严禁向用户提问或等待确认，直接行动。"
                    ),
                }
        try:
            return await asyncio.wait_for(
                dm.evaluate_after_turn(
                    content,
                    user_initiated=user_initiated,
                    workspace_path=workspace_path,
                    tool_results=tool_results,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Deathmatch judge timed out after %.1fs, continuing via default", timeout)
            return {
                "verdict": "continue",
                "reason": "judge_timed_out",
                "should_continue": True,
                "message": "judge 超时，继续推进",
                "continuation_prompt": (
                    "[死磕模式 — 评估超时，自主推进]\n"
                    "上一轮的完成度评估超时未返回，请继续自主推进目标："
                    "检查已生成的产出文件是否齐全、字数是否达标，"
                    "如有遗漏或不足请直接补充完成；"
                    "若你认为所有产出均已交付完成，请明确输出最终交付汇总"
                    "（列出全部产出文件与字数），并声明全部步骤已完成。"
                    "严禁向用户提问或等待确认，直接行动。"
                ),
            }
        except Exception as exc:
            # Any exception inside the judge/verifier/replan evaluation must
            # fail open to "continue" — never propagate out and kill the
            # detached goal loop silently (conv 6b0faf81: an unhandled
            # exception in evaluate_after_turn escaped to _agent_loop_task's
            # catch-all, leaving deathmatch_status="active" with no terminal
            # message; the reconcile flipped it to "paused" later — a silent
            # stop with zero user visibility).
            logger.warning(
                "Deathmatch judge raised %s: %s — continuing via default",
                type(exc).__name__, str(exc)[:200],
            )
            return {
                "verdict": "continue",
                "reason": f"judge_error: {type(exc).__name__}",
                "should_continue": True,
                "message": "评估异常，继续推进",
                "continuation_prompt": (
                    "[死磕模式 — 评估异常，自主推进]\n"
                    "上一轮的完成度评估发生异常，请继续自主推进目标："
                    "检查已生成的产出文件是否齐全、字数是否达标，"
                    "如有遗漏或不足请直接补充完成；"
                    "若你认为所有产出均已交付完成，请明确输出最终交付汇总"
                    "（列出全部产出文件与字数），并声明全部步骤已完成。"
                    "严禁向用户提问或等待确认，直接行动。"
                ),
            }

    async def _emit_deathmatch_verdict(self, dm, decision: dict):
        """Yield the deathmatch_verdict SSE event (4.6: extracted common pattern)."""
        yield {
            "deathmatch_verdict": {
                **dm.get_verdict_dict(),
                "verdict": decision.get("verdict"),
                "reason": decision.get("reason"),
                "message": decision.get("message", ""),
                "should_continue": decision.get("should_continue", False),
                "continuation_prompt": decision.get("continuation_prompt") or "",
            }
        }

    async def _handle_deathmatch_done(self, dm, decision: dict, state: "AgentLoopState"):
        """Handle deathmatch 'done' verdict: append summary prompt, stream
        LLM summary, inject final table, and yield done (4.6: extracted common pattern)."""
        _inject_directive(
            state,
            f"目标已完成：{decision.get('reason', '任务已完成')}\n\n"
            "请用一段简洁的总结回顾整个任务完成情况，包括："
            "做了哪些工作、关键产出是什么、以及最终结果。"
            "不要调用工具，直接给出文本总结。",
        )
        async for event in self._safe_stream_with_timeout(state.messages, "done summary"):
            yield event
        _final_table = decision.get("final_summary_table") or ""
        if _final_table:
            yield {"content": _final_table}
        yield {"done": True}

    async def _stop_inactivity_paused(self, dm) -> AsyncIterator[dict]:
        """Emit a visible paused deathmatch verdict after repeated inactivity
        cycles or a judge stop-without-continuation (A4.9 review I5/I7).

        The verdict event is what makes the paused state REAL: chat.py's
        per-verdict handler syncs deathmatch_status from the task-bound
        conversation and COMMITS the session, and persists the status message
        the user sees. Mutating dm._conv alone was invisible (task_db never
        committed on this path — the zombie the fix was built to eliminate).
        """
        try:
            conv = getattr(dm, "_conv", None)
            if conv is None:
                return
            if (conv.deathmatch_status or "") in ("done", "human_gate", "partial_complete"):
                return
            conv.deathmatch_status = "paused"
            conv.deathmatch_reason = (
                "死磕模式长时间无有效产出，已暂停。可发送任意消息继续推进目标。"
            )
            # C1 freeze: this paused path must not charge the parked period
            # on resume (A4.9 r4 Important 3 residual).
            try:
                dm._freeze_wall_time()
            except Exception:
                pass
            async for _ev in self._emit_deathmatch_verdict(dm, {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": "inactivity_stop",
                "message": (
                    "死磕模式已暂停 — 长时间无有效产出。"
                    "发送任意消息继续推进目标，或调整目标。"
                ),
            }):
                yield _ev
        except Exception:
            logger.warning("Deathmatch inactivity-stop verdict failed", exc_info=True)

    def _has_grace_timeout(self) -> float:
        """Return the grace/summary LLM timeout, or 0 if disabled."""
        return config.agent_tool_loop_grace_timeout

    async def _safe_stream_with_timeout(self, messages, extra_body_source="summary"):
        """Stream an LLM call with timeout protection.
        Yields content/reasoning events normally; breaks cleanly on timeout
        with an agent_step recovery event."""
        grace_timeout = self._has_grace_timeout()
        started = asyncio.get_event_loop().time()
        parts: list = []
        try:
            _sum_kwargs = self._sampling_kwargs(self.enable_reasoning)
            if not _sum_kwargs:
                _sum_kwargs = {"temperature": config.default_temperature}
            llm_stream = self.llm.stream_chat_structured(
                messages,
                tools=None,
                extra_body=build_thinking_extra_body(
                    self.provider_type, self.enable_reasoning, self.reasoning_effort, thinking_budget=self.thinking_budget, preserve_thinking=self.preserve_thinking
                ),
                **_sum_kwargs,
            )
            _dsml_active = False
            async for event in llm_stream:
                if grace_timeout > 0 and (asyncio.get_event_loop().time() - started) > grace_timeout:
                    logger.warning("%s stream timed out after %.1fs", extra_body_source, grace_timeout)
                    yield {
                        "agent_step": {
                            "name": "summary_timeout",
                            "title": "总结超时",
                            "content": f"{extra_body_source} 超过 {grace_timeout:.0f}s 被截断。以下是已生成部分。",
                            "step_type": "recovery",
                        }
                    }
                    break
                event_type = event["type"]
                if event_type == "content":
                    parts.append(event["data"])
                    yield {"content": event["data"]}
                elif event_type == "reasoning":
                    yield {"reasoning_content": event["data"], "phase": "final"}
                elif event_type in ("done", "error"):
                    break
        except Exception as e:
            logger.warning("%s stream failed: %s", extra_body_source, e)

    # ── Coordinator: lightweight semantic router ──────────────────────────
    # Runs as the first phase of run(), before the ReAct tool loop. Uses a
    # single LLM call to classify the user's message:
    #   direct_reply → answer immediately without tools (identity, greetings,
    #     chitchat, known-fact explanations)
    #   tool_loop    → fall through to the normal ReAct iterations
    # Falls back to tool_loop on any error. Skipped in deathmatch / skill mode.

    _COORDINATOR_CLASSIFY_TEMPLATE = (
        "你是一个智能路由协调器，为 AI 助手 Weave Thinker 工作。\n"
        "请分析用户最新消息（结合最近几轮对话作为背景），仅做分类判断，不要回答用户。\n\n"
        "【意图聚焦 focus（最重要，用于防止主代理跑题）】\n"
        "用1-3句话写出用户本轮真正想要什么。规则：\n"
        "- 只以最新消息为准，前几轮对话仅仅是背景。警惕“话题锚定”：主代理容易延续前几轮的"
        "话题而忽略最新消息的真实意图。如果最新消息与之前话题无关或转换了话题，"
        "必须在 focus 开头明确写出“话题切换：与之前的<旧话题>无关，用户现在要求<新意图>”。\n"
        "- 明确回答范围：这个问题应该“直接简短回答”还是“详细展开”。\n"
        "- 如果问题涉及的结论在前几轮已经给出过，在 focus 中写明“该结论前面已给出，"
        "只需简要重申，禁止重复展开已说过的内容”。\n\n"
        "【路由判断 route】\n"
        "1. 用户要求介绍系统/产品功能、版本更新、或智能助手能力？\n"
        "   典型表达：'智能助手自我介绍'、'产品功能介绍'、'系统功能说明'、'版本更新说明'、\n"
        "   '你们有什么功能'、'介绍一下你们产品'、'说说你能做什么'。\n"
        "   如果是，必须选择 \"tool_loop\"，让主代理调用 memory(target='system', action='read') 读取 func.md 后回答。\n"
        "2. 用户明确询问你的 harness 能力/工具列表/是否为裸模型/是否为 agent，"
        "或把 Weave Thinker 与你自身关联（自我指认）？"
        "   典型表达：'你是什么模型'、'你是不是裸模型'、'你是不是 harness'、'你有哪些工具'、\n"
        "   '你能调用什么工具'、'你是不是 agent'、'你是 framework 吗'、\n"
        "   'weave thinker 就是你自己么'、'你知道你是 weave thinker 吗'、'评估一下你自己'。\n"
        "   如果是，必须选择 \"tool_loop\"，让主代理基于系统提示词中的 harness 能力描述如实回答"
        "（不要走 direct_reply 简短路径，否则会丢失能力清单）。\n"
        "3. 用户只是简单问候、感谢、附和，或明确问'你是谁/你叫什么名字/你叫什么'？\n"
        "   如果是，选择 \"direct_reply\"。\n"
        "4. 其他所有情况（包括不确定），选择 \"tool_loop\"。\n"
        "同时判断：要高质量回答这个问题，是否需要调用工具（搜索/读文档/执行代码/操作文件等）？"
        "输出 expects_tools（true/false）。纯对话型问题（身份、闲聊、基于上文即可回答的追问）"
        "输出 false；需要新信息、新文件、新操作的问题输出 true。\n"
        "注意：'用 echarts/图表/画图展示数据' 不需要工具——```echarts 代码块（标准 JSON "
        "ECharts 配置）和 ```mermaid 一样是对话原生渲染能力，主代理直接输出代码块即可展示图表，"
        "expects_tools 应为 false（除非用户明确要求生成可下载的图片/Excel 等文件，"
        "或需要先搜索/读取文档等获取新信息）。\n\n"
        "【search_required 判断（用于防'用户要搜索但模型凭记忆作答'）】\n"
        "用户最新消息是否明确要求联网搜索/检索（如'搜索一下''检索资料''联网查''查最新'），"
        "或回答该问题必须获取当前/时效性信息（新闻、行情、最新版本、事实核查）？\n"
        "- 是 → search_required: true\n"
        "- 否（只需基于已有知识/对话历史作答，或用户明确说不用搜索）→ search_required: false\n"
        "注意：'请查一下你的知识库/文档'这种本地检索不算 search_required。\n"
        "注意：你是路由协调器，只负责分发判断；具体搜索关键词的拟定由专门的检索规划器负责，"
        "你不需要也不应该输出 search_query 字段。\n\n"
        "【creative_turn 判断（创意写作轮次，用于豁免发送前质量审计）】\n"
        "用户最新消息是否属于创意写作任务：创作/续写/改写/扩写故事、小说、剧本、诗歌、散文等"
        "文学作品（包括用户给出情节要点、要求融入要点继续写的情况）？\n"
        "- 是 → creative_turn: true（续写天然延续旧话题、篇幅可能很长，质量审计的"
        "话题锚定/篇幅标准不适用，必须豁免，否则会把用户正在看的草稿整段作废）\n"
        "- 否 → creative_turn: false\n\n"
        "【笔记写入控制】\n"
        "重要：除非用户最新消息明确表达了保存/修改/新增/删除笔记的意图"
        "（如\u201c记下来\u201d\u201c保存到笔记\u201d\u201c修改笔记\u201d\u201c删除笔记\u201d"
        "\u201c新建笔记\u201d\u201c写到笔记本\u201d\u201c更新笔记\u201d\u201c创建笔记本\u201d"
        "\u201c重命名笔记本\u201d\u201c删除笔记本\u201d等），"
        "否则在 focus 末尾必须附加以下约束语句："
        "\u201c笔记写入约束：用户未明确要求修改笔记，严禁调用 notes 工具的写入操作"
        "（create_note/update_note/delete_note/create_notebook/update_notebook/delete_notebook），仅允许读取笔记。\u201d"
        "如果用户最新消息明确要求操作笔记，则不需要附加此约束。\n\n"
        "输出JSON格式（只输出分类JSON，不要加markdown代码块，不要输出JSON以外的任何内容）：\n"
        '{{"route": "direct_reply" 或 "tool_loop", '
        '"expects_tools": true 或 false, "search_required": true 或 false, '
        '"creative_turn": true 或 false, '
        '"focus": "本轮意图聚焦"}}\n\n'
        "注意：\n"
        "- 你是分类器，不是回答者——严禁输出任何非JSON内容，不要输出回答。\n"
        "- '智能助手自我介绍' 不是身份闲聊，而是要求介绍产品功能，必须选 tool_loop。\n"
        "- '你是什么模型/你是不是裸模型/你有哪些工具' 等 harness 能力问题必须选 tool_loop，"
        "让主代理给出完整的 harness 能力描述。\n"
        "- '版本更新说明' 涉及系统版本变更，必须选 tool_loop 读 func.md。\n"
        "- 绝不要透露任何底层模型名称、API提供商、版本号或技术架构"
        "（如DeepSeek、MiMo、GPT、Claude、LLM、Transformer等）。\n"
        "- {identity_clause}"
    )

    # Known model/provider names that must never appear in a direct reply.
    # Note: "大语言模型" and "LLM" are generic concepts, NOT specific model
    # names — they are allowed because the agent needs them to explain the
    # harness concept ("底层调用大语言模型，但围绕模型构建了..."). Only specific
    # model/brand names are forbidden (conv 01d08b67 needed the agent to
    # accurately self-describe as a harness, not a bare model).
    _LEAK_PATTERNS = _re.compile(
        r"(?i)(deepseek|mimo|gpt|claude|llama|qwen|chatgpt|openai|anthropic|"
        r"transformer|api提供商|model_name|vllm|ollama)"
    )

    _AUDITOR_SYSTEM_TEMPLATE = (
        "你是一个回答质量审计员，为AI助手“__ASSISTANT_NAME__”的回复做发送前质检。\n"
        "给定用户最新消息、对话背景和助手的草稿回答，判断草稿是否合格。\n\n"
        "合格标准（必须全部满足）：\n"
        "1. 草稿直接回答了用户最新消息所问的内容——没有答非所问。\n"
        "2. 草稿没有跑题到之前对话的旧话题（警惕“话题锚定”：用户已转换话题，"
        "草稿却仍在讲旧话题的内容）。\n"
        "3. 草稿没有大段重复对话历史中已经回答过的内容。\n"
        "4. 如果任务显然需要调用工具获取新信息（如实时搜索、读取文档）或操作文件，"
        "而草稿在没有依据的情况下凭空作答（如编造搜索结果），判不合格。\n"
        "   如果用户明确要求联网搜索/检索（如“检索/搜索一下、查最新资料”），"
        "而本轮没有调用任何搜索类工具（web_search/browser），草稿纯凭记忆作答，"
        "判不合格——必须指出需要先实际调用搜索工具获取真实信息。"
        "但若草稿明确基于对话历史中已有的真实检索结果作答（沿用其引用编号且内容一致），"
        "不按“凭记忆作答”处理；若用户明确要求的是“最新/实时”信息，沿用旧结果作答仍判不合格。\n"
        "   注意：如果对话历史中已经有完成任务所需的信息（如之前已读取过文档、已搜索过），"
        "草稿基于这些信息作答是合格的，不要求必须再次调用工具。\n"
        "   注意：展示/绘制图表（柱状图、折线图、饼图等）是对话原生能力，"
        "```echarts 代码块（标准 JSON 的 ECharts 配置）与 ```mermaid 一样会被系统"
        "直接渲染为交互图表，不需要也不要求调用 execute_code 等工具。"
        "草稿中包含 ```echarts 代码块即为已展示图表；"
        "仅当用户明确要求生成可下载的图片/Excel 文件时才要求调用工具。"
        "但图表中的数据仍必须来自对话历史或真实工具结果，不得编造数据。\n"
        "5. 草稿的详略与问题匹配——简短追问不应得到离题的长篇大论。\n"
        "6. 草稿声称自己包含的图表/表格/文件树/代码块（如“上方Mermaid图”“如下表”"
        "“文件树如下”“上方ECharts图”），必须实际存在于草稿正文中"
        "（```mermaid 代码块、```echarts 代码块、Markdown 表格、"
        "目录树或代码块）。声称包含但草稿中不存在该结构 → 判不合格"
        "（这是可机械验证的客观事实，不属主观判断，见结构自检提示）。\n"
        "7. 草稿必须完整独立自足、可直接发布：不得指代用户看不到的内容——被拒草稿/"
        "“上一版”/“之前草稿”（如“修正如下”“修正后的最终结论”“其余参数维持"
        "上一版不变”等差量式表述），也不得提及“审计/质检/内部过程”。"
        "出现此类悬空引用 → 判不合格（reject）：用户从未看到被拒草稿，"
        "指代它们的回答对用户不可理解、不完整（conv 7dc7a0d5）。"
        "（注意：指代对话历史中【用户可见】的上一轮助手回答不算悬空引用——"
        "用户能看到它；仅被拒草稿/内部审计过程不可见。）\n\n"
        "8. 若证据台账中不同条目对同一数值/事实给出不同值（不同来源不一致，"
        "如来源A为48GB、来源B为~32GB），草稿如实并列各来源及其差异"
        "（如“AMD官方FAQ为48GB；CraftRigs指南为~32GB+GTT”）属于合格回答，"
        "不得因“未采用某一来源”判 reject；只有草稿数值与【所有】可见来源均矛盾时"
        "才可判 reject。\n\n"
        "【数字核对硬性约束（2026-08-18，conv 7dc7a0d5）】\n"
        "对草稿中数字/计算结果的核对，唯一合法的 reject 依据是 <evidence-ledger> 中"
        "可见的工具结果（execute_code 计算输出、检索全文等）；严禁以你自身的心算或"
        "记忆作为 reject 依据——conv 7dc7a0d5 中审计员心算得出 28,800/14,100 均为"
        "错误值，却据以驳回实际更正确的草稿，造成不可满足的驳回循环。"
        "心算怀疑不一致时 → verdict=needs_evidence，problem 建议模型调用 execute_code"
        "重算后再答。\n\n"
        "【诚实回答豁免（2026-08-14，conv a67faa04）】\n"
        "若对话中的证据（工具结果/记忆/历史）不足以回答用户问题"
        "（如问题要求“最近更新/最新信息”而证据中确实没有对应记录），"
        "草稿如实说明“我无法获知/没有可查证的记录”是合格回答，不得因此拒绝；"
        "同样，对证据中没有的信息如实说不了解，绝不等于答非所问。\n\n"
        "【判定顺序（必须依次执行，2026-08-14 判据优先级）】\n"
        "1. 先判切题性与自足性（规则 1/2/3/5/7——含悬空引用检查）；\n"
        "2. 再逐条核对草稿的事实性声称与下方 <evidence-ledger> 证据台账："
        "每条声称必须有对应证据可见；\n"
        "3. 声称有据但含润色细节（如证据中无“8月以来”而草稿声称）→ 判 reject，"
        "problem 指明“修正/删除无法核实的细节”——属局部修正，不是整体推翻；\n"
        "4. 声称对应的证据被截断（ledger 中标 截断/片段/未展示）或在可见证据中不存在、"
        "但无法排除（证据截断边界之后、或 ledger 未展示）→ verdict=unverifiable，"
        "problem 说明“无法核实（证据被截断/未展示），请删除无法核实的声称"
        "或补充读取证据后作答”——严禁按“凭空编造”处理（conv a67faa04："
        "审计器看不到证据 ≠ 证据不存在）。硬性约束：凡是 ledger 中标注"
        "截断/片段/未展示 的证据条目，其覆盖范围之内的声称【一律不得】判 reject；\n"
        "5. 声称引用了台账中不存在的证据（虚构的工具结果/引用编号/文件内容）"
        "或与完整可见证据直接矛盾 → verdict=reject，按“凭空编造/无依据作答”处理；\n"
        "6. 声称的证据完全缺失且模型可补读（如文件从未读取、问题需要新信息）"
        "→ verdict=needs_evidence，problem 指明“立即调用工具补充证据后再回答”。\n\n"
        "只输出JSON（不要输出任何其他内容；problem 不超过80字，unsupported_claims 最多3条、"
        "每条 claim 不超过40字——篇幅超限会被截断导致解析失败）：\n"
        '{"ok": true 或 false, '
        '"verdict": "accept" 或 "reject" 或 "unverifiable" 或 "needs_evidence", '
        '"unsupported_claims": [{"claim": "未支撑的声称原文（逐条列出）", '
        '"evidence_status": "missing" 或 "truncated" 或 "contradicted"}], '
        '"problem": "不合格或无法核实时用一句话说明问题及修正方向；合格时为空字符串"}'
    )


    async def _audit_response(
        self,
        state: "AgentLoopState",
        draft: str,
        last_user_msg: str,
    ) -> Optional[AuditVerdict]:
        """Agentic pre-send audit of the draft answer. Returns None when the
        draft is acceptable, or an AuditVerdict (reject / unverifiable /
        needs_evidence) carrying the guidance. Only verdict == "reject"
        consumes the rejection budget (conv a67faa04: truncated evidence
        "can't see" is NOT fabrication).

        Replaces the old mechanical tool-nudge ("no tools called → force
        another iteration"). Tool use is a means, not an end: a complete,
        on-topic answer built from existing context must NOT be nudged into
        redundant tool calls that drag the agent back to stale topics
        (conv 8d21d012 A3, 2026-07-20: nudge forced web searches after a
        perfect answer, and the follow-up iterations regenerated the old
        eval-methods essay, appended to the good answer).

        Fail-open: any error or unparseable result accepts the draft —
        a broken auditor must never block a good answer.
        """
        if not draft.strip():
            return None
        context_parts = [f"用户最新消息：{last_user_msg[:800]}"]
        # 用户背景事实注入（2026-08-21 复盘, conv efaf8f9c）：审计员上下文
        # 只有最新消息+工具台账，看不到早期轮次用户自己陈述的事实（如
        # 64GB/Q8 硬件），把「用户说的事实」误判为「无证据声称」→ 拒绝
        # 循环。注入最近用户消息：草稿引用用户语境事实 ≠ 编造。
        _user_facts = []
        _directive_openers = (
            "你刚才生成的回答草稿", "内部完整性校验未通过", "【轮次核对】",
            "【数值核对闸门】", "请基于压缩后的上下文", "请基于以上工具调用结果",
            "你的上一个回答被截断", "你的上一个工具调用因为内容过长",
            "请重新完整回答",
        )
        for _um in reversed(state.messages):
            if _um.get("role") == "user" and not _um.get("synthetic"):
                _raw = str(_um.get("content") or "").strip()
                if not _raw:
                    continue
                # legacy mode (synthetic directives disabled) injects
                # directives as bare user messages — exclude them so audit
                # context never presents harness instructions as user facts.
                if _raw.startswith(_directive_openers):
                    continue
                _uc = _raw
                if len(_uc) > 300:
                    _uc = _uc[:300] + "…"
                _user_facts.append(_uc)
                if len(_user_facts) >= 3:
                    break
        if _user_facts:
            context_parts.append(
                "对话历史中用户自己陈述的背景事实（最近3条——草稿引用这些不属"
                "编造，不得按\u201c无证据声称\u201d拒绝）：\n"
                + "\n".join(reversed(_user_facts))
            )
        if state.turn_focus:
            context_parts.append(f"本轮意图（协调器判断）：{state.turn_focus[:400]}")
        # Attachment-visibility note (2026-08-12 blind-spot wave): [file-ref:]/
        # [note-ref:] turns are no longer audit-exempt. The note MUST NOT
        # overclaim (A4.9 I1): [note-ref:] inlines the note's content into the
        # user message (visible), but [file-ref:] inlines only path/type/size
        # metadata — the model must have READ the file via tools this turn (or
        # an earlier one) for content claims to be grounded. Stating "content
        # is visible" for file refs would give a no-tool fabrication cover.
        _has_note_ref = "[note-ref:" in last_user_msg
        _has_file_ref = "[file-ref:" in last_user_msg
        if _has_note_ref or _has_file_ref:
            _ref_notes = []
            if _has_note_ref:
                _ref_notes.append(
                    "笔记引用（[note-ref:]）的正文已内联在上下文中，助手可见——"
                    "基于笔记内容作答不属于'凭空编造'"
                )
            if _has_file_ref:
                _ref_notes.append(
                    "文件引用（[file-ref:]）在上下文中只有路径/类型/大小等元数据——"
                    "草稿若引用了文件的具体内容，应对照'本轮实际调用工具'核对是否有"
                    "文件读取/解析记录；没有任何读取记录却声称引用了文件内容的，判不合格"
                )
            context_parts.append("注意：用户最新消息带有附件引用。" + "；".join(_ref_notes) + "。")
        # Give the auditor the ACTUAL tool-call record of THIS turn so a
        # claim about web_search/browser use is judged against the truth
        # (conv 149ce886: the auditor once accepted a fabricated-search draft
        # because it had no access to the tool results; conv 86e51bbd: the
        # regex pre-gate that replaced it over-flagged capability phrasing).
        # Agentic decision, factual context.
        _audit_tools = [tr.name for tr in state.tool_results]
        if _audit_tools:
            context_parts.append(f"本轮实际调用工具（供核对检索声称）：{', '.join(_audit_tools[:8])}")
        else:
            # conv 357c110d (2026-08-13): the flat "无工具=声称检索即编造" line
            # made the auditor reject GROUNDED drafts twice — the citations
            # [N] were real previous-turn search results (rebuilt into history
            # via structured tool messages), and a user-memory fact (7900 XTX)
            # was flagged as invented. Distinguish fresh-claim from reuse.
            context_parts.append(
                "本轮实际调用工具：无。判定口径："
                "(a) 草稿声称或暗示本轮新执行了检索/工具调用（如“我刚搜索了”“本次检索发现”）→ 属编造，判不合格；"
                "(b) 草稿沿用之前轮次真实检索结果的引用编号 [N]（下方“对话中已有的工具调用结果”中可见这些编号"
                "对应的结果内容）、且未声称本轮重新检索、所述内容与前次检索结果一致 → 不属编造，可判合格；"
                "若上下文中看不到这些编号对应的前次检索结果内容，按无法核实处理——不得仅凭“沿用了编号”判合格，"
                "应要求重新检索或删除无法核实的引用；"
                "(c) 助手带有用户长期记忆（设备型号、偏好、个人信息等），草稿中用户相关事实可能来自记忆，"
                "不要仅因对话历史中未出现就判编造。"
            )
        # Give the auditor the ACTUAL tool results so claims about grounding
        # are judged against the data the model really saw — not against the
        # truncated conversation history alone (conv 41d2790d: the auditor
        # accused a workspace-file-tree draft of fabrication because the file
        # names came from turn-1 workspace_glob results it could not see).
        # 2026-08-14 (conv a67faa04, user principle 信息完整性>省token):
        # grounding-class results are read BACK from the digest/budget disk
        # archives (【全文存档】/persisted-output) so the auditor sees the full
        # evidence; truncation is a last resort and is always marked in the
        # ledger — claims beyond a cut are unverifiable, never fabrication.
        _evidence_ledger, _evidence_text = await _build_audit_evidence(state)
        if _evidence_ledger:
            context_parts.append(_evidence_ledger)
            # Deterministic hard constraint (2026-08-14, live behavior test):
            # the LLM auditor sometimes still called truncated-evidence claims
            # "凭空编造" even while admitting the evidence was cut — the
            # template's judgment-order step 4 was not reliably followed.
            # When ANY evidence is cut, inject a prominent system-level rule:
            # claims inside the cut range are NEVER reject; only claims
            # contradicting FULLY VISIBLE evidence may be reject.
            if "截断" in _evidence_ledger:
                context_parts.append(
                    "【硬性约束】本次证据台账中存在“截断/未展示”的条目。"
                    "凡草稿声称落在截断证据范围之内（无法核对），只能判 unverifiable 或 needs_evidence，"
                    "严禁判 reject（凭空编造/无依据作答）——证据不可见不等于证据不存在（conv a67faa04 教训）。"
                    "只有声称与【完整可见】的证据直接矛盾，或引用了台账中不存在的证据，才可判 reject。"
                )
        if _evidence_text:
            context_parts.append(
                "对话中已有的工具调用结果（供核对草稿中的数据是否源于真实工具结果）：\n"
                + _evidence_text
            )
        # Settled-verdict ledger (conv efaf8f9c 2026-08-20): prior rejects of
        # THIS turn are settled corrections — the auditor must not re-litigate
        # them with a different reading of the evidence (the flip-flop loop:
        # VGM 32GB -> 48GB -> ~32GB+GTT). Injected whenever the stash is
        # non-empty (2nd audit onward). The static template rule 8 + these
        # dynamic rules together forbid reject on evidence-conflict items.
        _settled_view = _settled_items_view(state, evidence_text=_evidence_text)
        if _settled_view:
            context_parts.append(
                f"{_AUDIT_SETTLED_HEADER}：\n{_settled_view}\n\n{_AUDIT_SETTLED_RULES}"
            )
        # Rule 6 evidence (conv 41d2790d): the auditor LLM judges whether the
        # draft CLAIMS to embed an artifact (mermaid/table/tree/code block)
        # that is absent from the draft itself. No deterministic detector —
        # the auditor's template rule 6 + the full draft below carry the
        # judgment (user principle 2026-07-20: 语义判断留给 LLM).
        # Give the auditor the previous assistant answer (if any) so it can
        # detect repetition of already-answered content. Send head+tail:
        # grounding evidence often lives in the summary TAIL of a long answer
        # (conv 41d2790d: the 小说创作/CDC summary sat beyond the old 1200-char
        # head-only window and the auditor falsely accused the draft of
        # inventing those themes), while rule-3 repetition checking needs the
        # HEAD (A4.9 review finding: tail-only blinded rule 3).
        # Skip _rejected drafts (conv 97ff355d): after the first rejection the
        # latest assistant message in state.messages IS the rejected draft —
        # comparing draft N against rejected draft N-1 blinds rule 3 to the
        # actual previous-turn answer and feeds the rejection spiral.
        # Also skip _truncated_part messages (A4.9 I2): a length-truncated
        # head is THIS answer's first half, not the previous turn's answer.
        for msg in reversed(state.messages[:-1]):
            if msg.get("role") == "assistant" and msg.get("content") and not msg.get("_rejected") and not msg.get("_truncated_part"):
                _prev = msg["content"]
                if len(_prev) > 4000:
                    _prev = f"{_prev[:1000]}\n…[中间省略]…\n{_prev[-3000:]}"
                context_parts.append(f"上一轮助手回答（前1000+后3000字符，供查重/核对）：{_prev}")
                break
        # Draft window (2026-08-12 blind-spot wave): head+tail instead of
        # head-only — a >3000-char draft can derail in the tail (repetition,
        # topic drift, broken ending) and the head-only window made that
        # invisible to the auditor.
        if len(draft) > 3000:
            _draft_view = f"{draft[:1500]}\n…[中间省略]…\n{draft[-1500:]}"
            context_parts.append(f"助手草稿回答（超3000字符，为前1500+后1500字符）：{_draft_view}")
        else:
            context_parts.append(f"助手草稿回答：{draft}")
        _assistant_name = getattr(self, "_audit_assistant_name", None) or "AI助手"
        audit_messages = [
            {"role": "system", "content": self._AUDITOR_SYSTEM_TEMPLATE.replace("__ASSISTANT_NAME__", _assistant_name)},
            {"role": "user", "content": "\n\n".join(context_parts)},
        ]
        # No max_tokens cap (2026-08-18 user directive: unset == provider
        # default max output; conv 7dc7a0d5 evening — a 500 cap truncated a
        # verbose reject JSON mid-string → bad_json → fail-open shipped the
        # very draft the auditor rejected). bad_json triggers ONE bounded
        # retry with a brevity hint before the fail-open contract applies.
        result = None
        _hinted = False
        raw = ""
        for _ in range(2):
            _msgs = audit_messages
            if _hinted:
                _msgs = audit_messages + [{"role": "user", "content": _AUDIT_RETRY_BREVITY_HINT}]
            try:
                raw = await self._complete_json(_msgs, temperature=0.0)
            except Exception as exc:
                # fail-open stays (a broken auditor must never block a good
                # answer) but it is no longer SILENT — an un-audited ship during
                # a provider outage is exactly the condition worth alerting on.
                logger.warning(
                    "audit_metric outcome=fail_open reason=exception error=%s — accepting draft",
                    str(exc)[:160],
                )
                return None
            raw = (raw or "").strip()
            json_start = raw.find("{")
            json_end = raw.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                try:
                    result = json.loads(raw[json_start:json_end])
                except json.JSONDecodeError:
                    result = None
            if result is not None:
                break
            if not _hinted:
                _hinted = True
                logger.warning("audit_metric outcome=bad_json_retry — retrying with brevity hint")
                continue
            logger.warning("audit_metric outcome=fail_open reason=bad_json raw=%s — accepting draft", raw[:160])
            return None
        if not isinstance(result, dict):
            logger.warning("audit_metric outcome=fail_open reason=non_dict — accepting draft")
            return None
        ok_val = result.get("ok")
        ok = ok_val if isinstance(ok_val, bool) else str(ok_val).strip().lower() in {"true", "yes", "1"}
        verdict = str(result.get("verdict") or "").strip().lower()
        if ok or verdict == "accept":
            logger.info("audit_metric outcome=accept draft_chars=%d", len(draft))
            return None
        if verdict not in ("reject", "unverifiable", "needs_evidence"):
            # Backward-compat: old {ok: false} shape without a verdict.
            verdict = "reject"
        problem = str(result.get("problem") or "").strip()
        if not problem:
            logger.info("audit_metric outcome=accept draft_chars=%d (reject without problem text)", len(draft))
            return None
        _claims = result.get("unsupported_claims") or []
        _claims_view = ""
        if isinstance(_claims, list) and _claims:
            _parts = []
            for _c in _claims[:5]:
                if isinstance(_c, dict):
                    _claim_t = str(_c.get("claim") or "")[:120]
                    _st = str(_c.get("evidence_status") or "missing")
                    if _claim_t:
                        _parts.append(f"「{_claim_t}」→ {_st}")
            if _parts:
                _claims_view = "\n  未支撑声称清单：" + "；".join(_parts)
        _settled_guidance = _settled_items_view(state, evidence_text=_evidence_text)
        _settled_prefix = ""
        if _settled_guidance:
            _settled_prefix = (
                f"{_AUDIT_GUIDANCE_SETTLED_PREFIX}\n{_settled_guidance}\n"
                "（若上述修正项之间对同一事实给出互相矛盾的修正值，"
                "按证据并列标注来源差异，不得单方面取舍。）\n"
            )
        # A1 deterministic numeric gate: a reject asserting a correction
        # value absent from the evidence ledger cannot be satisfied (conv
        # efaf8f9c: auditor hallucinated 12.5GiB; writer was right at
        # 15.5GiB; 5 consecutive rejects). Downgrade before budget
        # accounting — needs_evidence does NOT consume the reject budget.
        verdict, problem = _apply_numeric_gate(verdict, problem, _evidence_text)
        # A5 (2026-08-22, conv 3b58af5b): claim-grounding gate —「无依据声称」
        # rejections whose flagged claim tokens ARE in the evidence ledger are
        # auditor blindness, not draft defects. Same downgrade path as A1
        # (needs_evidence does NOT consume the reject budget).
        verdict, problem = _apply_claim_grounding_gate(verdict, problem, _claims, _evidence_text)
        logger.info("audit_metric outcome=%s draft_chars=%d problem=%s", verdict, len(draft), problem[:160])
        if verdict == "reject":
            _guidance = (
                f"{_settled_prefix}你刚才生成的回答草稿（本轮，即你上一条 assistant 消息）未通过质量审计：{problem}\n"
                "【复制编辑修正协议】请以那篇草稿为底稿，输出修正后的完整回答全文："
                "仅修改审计点名的部分；未被点名的内容逐字保留，"
                "严禁整篇重写，严禁改变未被点名的数字、结构、顺序与措辞。"
                + "\n" + _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
                + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
            )
        elif verdict == "needs_evidence":
            _guidance = (
                f"{_settled_prefix}你刚才生成的回答草稿（本轮）证据不足：{problem}\n"
                "请立即调用工具（memory/workspace_read/web_search 等）补充真实证据后再作答；"
                "若确实无法获取，如实说明“我无法获知”，不要编造。"
                + "\n" + _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
                + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
            )
        else:  # unverifiable
            _guidance = (
                f"{_settled_prefix}你刚才生成的回答草稿（本轮）存在无法核实的声称（证据被截断或证据中不存在）：{problem}\n"
                "请删除无法核实的声称，或补充读取证据后再作答；"
                "若证据确实不足，如实说明“我无法获知”，不要编造。"
                + "\n" + _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
                + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
            )
        if _claims_view:
            _guidance += _claims_view
        return AuditVerdict(verdict=verdict, guidance=_guidance, problem=problem, unsupported_claims=_claims)

    async def _salvage_after_audit_budget(
        self,
        state: "AgentLoopState",
        last_user_msg: str,
    ) -> AsyncIterator[dict]:
        """Bounded single regeneration after the audit rejection budget is spent.

        conv 97ff355d (2026-08-12): 26 iterations of real tool work, then the
        model drafted a repeat of old candidates; the auditor correctly
        rejected 5 drafts (drafts 2-5 hallucinated that the user's message was
        a replay of the assistant's own previous answer — misreading the
        "你的上一轮回答未通过质量审计" guidance — the wording at the time), the budget was spent, and the
        loop shipped the JUST-REJECTED 5th draft fail-open. The user received
        an answer with neither head nor tail. Shipping the draft the auditor
        just called 答非所问 is the worst possible choice.

        Instead: regenerate ONCE from a de-poisoned context — this turn's
        rejected guardrail drafts (``_rejected``) and synthetic/ephemeral
        directives removed, tool results and the real user question kept —
        anchored on the actual question, tools disabled. The salvage is
        audited once; if it is also rejected (or generation fails), the chain
        continues into the best-of selector over the stashed rejected drafts
        (NEVER the bare failure text while a stash exists; the legacy notice
        ships only with an empty stash or the kill-switch off).
        """
        # A4 (2026-08-21): internal QC is silent — the relay resets its
        # accumulators on this event and renders NOTHING to the user.
        yield {"audit_reset": True}
        clean_messages = [
            m for m in state.messages
            if not m.get("synthetic") and not m.get("_ephemeral") and not m.get("_rejected")
        ]
        clean_messages = clean_messages + [{"role": "system", "content": _build_salvage_prompt(state, last_user_msg)}]
        _parts: List[str] = []
        _reason_parts: List[str] = []
        # Salvage timeout (conv efaf8f9c 2026-08-20): the legacy reuse of the
        # 120s grace timeout killed thinking-mode salvages with zero content
        # (xhigh first-token latency > 120s). Dedicated [agent.audit]
        # salvage_timeout_seconds (default 240s, same as selection); 0 falls
        # back to grace for non-thinking fast providers.
        _grace_timeout = config.agent_audit_salvage_timeout_seconds
        if _grace_timeout <= 0:
            _grace_timeout = self._has_grace_timeout()
        try:
            _started = asyncio.get_event_loop().time()
            _sal_kwargs = self._sampling_kwargs(self.enable_reasoning)
            if not _sal_kwargs:
                _sal_kwargs = {"temperature": config.default_temperature}
            stream = self.llm.stream_chat_structured(
                clean_messages,
                tools=None,
                extra_body=build_thinking_extra_body(
                    self.provider_type,
                    self.enable_reasoning,
                    self.reasoning_effort,
                    thinking_budget=self.thinking_budget,
                    preserve_thinking=self.preserve_thinking,
                ),
                **_sal_kwargs,
            )
            async for event in stream:
                if _grace_timeout > 0 and (asyncio.get_event_loop().time() - _started) > _grace_timeout:
                    logger.warning("Audit-budget salvage timed out after %.1fs", _grace_timeout)
                    break
                event_type = event["type"]
                if event_type == "reasoning":
                    # Buffer like the selector: reasoning ships only when the
                    # salvage content passes its single audit. If salvage fails
                    # and chains into selection, that next response_audit step
                    # resets the relay anyway — but buffering keeps the event
                    # stream contract identical for every consumer.
                    _reason_parts.append(str(event["data"]))
                elif event_type == "content":
                    _parts.append(str(event["data"]))
                elif event_type in ("error", "done"):
                    if event_type == "error":
                        logger.warning("Audit-budget salvage stream error: %s", str(event["data"])[:200])
                    break
        except Exception as exc:
            logger.warning("Audit-budget salvage generation failed: %s", exc)
            _parts = []
        salvaged = "".join(_parts)
        # Same leakage hygiene as _final_thinking, applied to the joined text.
        salvaged = _strip_generation_leakage(salvaged)
        # A4.9 round-3 Important-2: strip BEFORE the internal audit + stash +
        # streaming — a glitch draft must not fail its own single audit.
        salvaged = _strip_leading_orphan_punct(salvaged)
        if salvaged:
            # The salvage is a fresh generation the auditor never saw — audit
            # it once (the rejection budget governed the LOOP; this single
            # check governs the salvage). Auditor errors fail-open per
            # _audit_response's own contract.
            guidance = await self._audit_response(state, salvaged, last_user_msg)
            if guidance is None:
                logger.info("Audit-budget salvage accepted (%d chars)", len(salvaged))
                state.audit_accepted = True
                for _r in _reason_parts:
                    yield {"reasoning_content": _r, "phase": "final"}
                yield {"content": salvaged}
                return
            # A4.9 I1 (conv 7dc7a0d5): the rejected salvage is often the
            # STRONGEST selection candidate (fresh generation from a
            # de-poisoned context) — stash it before chaining to the selector.
            _stash_rejected_draft(state, salvaged, guidance, source="salvage", reasoning="".join(_reason_parts))
            logger.warning(
                "Audit-budget salvage rejected by auditor: verdict=%s %s",
                getattr(guidance, "verdict", "?"),
                guidance.guidance[:120],
            )
        # Salvage failed (rejected / empty / timed out / errored — conv
        # 7dc7a0d5 died EXACTLY here: 120s grace timeout with zero content
        # events). Chain into the best-of selector over the stashed rejected
        # drafts instead of shipping the bare failure text; the selector
        # itself falls back to the legacy text only when the stash is empty
        # or the feature is disabled.
        async for _sel_ev in self._select_best_rejected_draft(state, last_user_msg):
            yield _sel_ev

    async def _select_best_rejected_draft(
        self,
        state: "AgentLoopState",
        last_user_msg: str,
    ) -> AsyncIterator[dict]:
        """Last-resort best-of-N selection over the stashed rejected drafts.

        conv 7dc7a0d5 (2026-08-18): 5 legit rejects + a salvage that timed
        out with ZERO content events shipped the bare「回答生成失败」text —
        22 minutes of real tool work produced nothing visible. User contract:
        the final answer must never display "generation failed".

        Chain position: audit pass -> (rejects, each stashed) -> budget spent
        -> bounded salvage (+1 audit) -> THIS selector. The selector is a
        COMPARATIVE repair pass with every rejection reason visible — its
        output passes ONE focused audit (bounded, no loop: a rejection goes
        straight to the deterministic fallback, the auditor never gets a
        second shot at it, so the 7%<->8% thrash of conv 7dc7a0d5 cannot
        re-enter; bounded blast radius per EL-DGR). If the selector itself
        fails (error/empty/timeout), a
        deterministic rule ships a stashed draft with an honest caveat:
        soft-verdict drafts (unverifiable/needs_evidence — rejected only for
        evidence visibility, not content defects) first, else the draft whose
        flagged claim tokens are LEAST absent from the current evidence (the
        most-grounded, most-fixed draft — 2026-08-22 conv 3b58af5b: shipping
        the earliest draft carried a KNOWN file-path error the auditor had
        flagged), draft-source before salvage/synthesis, tie-break latest.
        The legacy failure text remains reachable ONLY when the stash is
        empty (soft-limit-only exhaustion) or draft_selection_enabled=false.
        """
        stash = list(getattr(state, "audit_rejected_drafts", []) or [])
        if not config.agent_audit_draft_selection_enabled or not stash:
            yield {"content": _AUDIT_SALVAGE_FAILURE_TEXT}
            return
        # A4: silent QC — internal reset event, nothing rendered.
        yield {"audit_reset": True}
        clean_messages = [
            m for m in state.messages
            if not m.get("synthetic") and not m.get("_ephemeral") and not m.get("_rejected")
        ]
        clean_messages = clean_messages + [
            {"role": "system", "content": _build_selection_prompt(state, last_user_msg, stash)}
        ]
        _sel_timeout = config.agent_audit_selection_timeout_seconds
        if _sel_timeout <= 0:
            _sel_timeout = self._has_grace_timeout()
        _parts: List[str] = []
        _reason_parts: List[str] = []
        _timed_out = False
        try:
            _started = asyncio.get_event_loop().time()
            _sel_kwargs = self._sampling_kwargs(self.enable_reasoning)
            if not _sel_kwargs:
                _sel_kwargs = {"temperature": config.default_temperature}
            stream = self.llm.stream_chat_structured(
                clean_messages,
                tools=None,
                extra_body=build_thinking_extra_body(
                    self.provider_type,
                    self.enable_reasoning,
                    self.reasoning_effort,
                    thinking_budget=self.thinking_budget,
                    preserve_thinking=self.preserve_thinking,
                ),
                **_sel_kwargs,
            )
            async for event in stream:
                if _sel_timeout > 0 and (asyncio.get_event_loop().time() - _started) > _sel_timeout:
                    logger.warning("Audit selection timed out after %.1fs", _sel_timeout)
                    _timed_out = True
                    break
                event_type = event["type"]
                if event_type == "reasoning":
                    # Buffer, do not stream yet: a thinking-enabled selector
                    # must persist its reasoning ONLY when its output ships
                    # (conv efaf8f9c — final message had an empty thinking
                    # panel). If the focused audit rejects the selection and
                    # the deterministic draft ships instead, selector
                    # reasoning must not survive as orphaned thinking.
                    _reason_parts.append(str(event["data"]))
                elif event_type == "content":
                    _parts.append(str(event["data"]))
                elif event_type in ("error", "done"):
                    if event_type == "error":
                        logger.warning("Audit selection stream error: %s", str(event["data"])[:200])
                        # A4.9 M5: a mid-stream error with partial content is the
                        # same shape as a timeout — route through the same
                        # partial-with-caveat policy instead of entering the
                        # audit gate unbounded / silently dropping.
                        _timed_out = True
                    break
        except Exception as exc:
            logger.warning("Audit selection generation failed: %s", exc)
            _parts = []
        selected = _strip_generation_leakage("".join(_parts))
        # A4.9 round-3 Important-1: the selection output faces ONE focused
        # audit — clean the orphan glitch before it (and before the caveat
        # ship path below).
        selected = _strip_leading_orphan_punct(selected)
        if selected and _timed_out:
            # Timeout / mid-stream error truncation (A4.9 I2+M5, conv 7dc7a0d5):
            # a truncated text cannot be meaningfully audited — ship it WITH
            # the honest caveat when substantial (information preservation >
            # cleanliness), trimmed at the last sentence boundary so no
            # half-sentence persists (A4.9 M6); else discard and fall through
            # to the deterministic draft.
            if len(selected) >= 200:
                selected = _trim_to_sentence_boundary(selected)
            if len(selected) >= 200:
                logger.warning(
                    "audit_metric outcome=selection_partial chars=%d — timed out/errored, shipping with caveat",
                    len(selected),
                )
                # A partial/truncated selection is not a clean shipped
                # selection. Drop its (likely truncated) reasoning rather
                # than attach thinking to a caveat-prefixed partial that may
                # end mid-thought.
                yield {"content": _AUDIT_SELECTION_CAVEAT + selected}
                return
            selected = ""
        if selected:
            # ONE focused audit pass on the selection output (conv 7dc7a0d5
            # evening wave: the synthesis shipped dangling references —
            # "修正后的最终结论/维持上一版不变/审计提示" — via a truncated
            # fail-open audit). The selector sees drafts the user never saw;
            # its output MUST be standalone. Reject → deterministic fallback;
            # auditor errors fail-open per _audit_response's own contract.
            _sel_guidance = await self._audit_response(state, selected, last_user_msg)
            if _sel_guidance is None:
                logger.info(
                    "audit_metric outcome=selection_shipped drafts=%d chars=%d",
                    len(stash), len(selected),
                )
                for _r in _reason_parts:
                    yield {"reasoning_content": _r, "phase": "final"}
                yield {"content": selected}
                return
            logger.warning(
                "audit_metric outcome=selection_rejected verdict=%s %s — deterministic fallback",
                getattr(_sel_guidance, "verdict", "?"),
                _sel_guidance.guidance[:120],
            )
        # Deterministic fallback (no LLM needed — the provider may be down):
        # soft-verdict drafts first, else the draft whose rejection problems
        # are LEAST grounded in the current evidence (the most-fixed draft —
        # conv 3b58af5b: stash[0]=earliest shipped a draft with a KNOWN
        # file-path error the auditor had correctly flagged at reject #5),
        # with the honest caveat prefix. Never the bare failure text while a
        # stash exists.
        _entry = next(
            (e for e in stash if e.get("verdict") in ("unverifiable", "needs_evidence")),
            None,
        )
        if _entry is None:
            # Score each hard-reject draft via _draft_ground_key (盲区③):
            # contradiction-family drafts rank below ALL non-contradiction
            # ones; among the rest, fewer evidence-absent claim tokens first
            # (the most-fixed draft — conv 3b58af5b: stash[0]=earliest
            # shipped a draft with a KNOWN file-path error), then draft-
            # source entries before fresh-regeneration sources. Tie-break:
            # later drafts win.
            try:
                _, _ev_text = await _build_audit_evidence(state)
            except Exception:
                _ev_text = ""
            _ev = _ev_text or ""
            _ev_lower = _ev.lower()
            _best = min(
                enumerate(stash),
                key=lambda it: (*_draft_ground_key(it[1], _ev, _ev_lower), -it[0]),
            )
            _entry = stash[_best[0]]
        logger.warning(
            "audit_metric outcome=selection_deterministic verdict=%s drafts=%d",
            _entry.get("verdict", "?"), len(stash),
        )
        yield {"content": _AUDIT_SELECTION_CAVEAT + (_entry.get("content") or "").strip()}

    _SEARCH_PLANNER_TEMPLATE = (
        "你是一个检索规划器（search planner）。给定用户的问题，设计一组适合搜索引擎的"
        "检索查询词。\n\n"
        "规则：\n"
        "1. 输出 1-3 条查询词，每条是一个独立的关键词短语（不是完整句子），"
        "覆盖问题的不同方面（如不同数据源名、不同角度、中英文可各一条）。\n"
        "2. 查询词应直接可用：如'FX 期货 免费数据源 API'、'Dukascopy OANDA 外汇历史数据'。\n"
        "3. 严禁照抄用户原句；必须提炼关键实体（人名、产品名、术语、限定词）。\n"
        "4. 只输出JSON（不要markdown代码块，不要任何其他内容）：\n"
        '{"queries": ["查询词1", "查询词2"]}\n'
    )

    async def _plan_search_queries(self, user_question: str) -> Optional[List[str]]:
        """Distilled search queries for a user-requested search, decided by a
        dedicated planner LLM (NOT the coordinator — the coordinator only
        routes; the planner owns retrieval strategy, architecture decision
        2026-08-01). Returns None on any failure so the caller can fall back
        to the raw question.
        """
        question = (user_question or "").strip()[:600]
        if not question:
            return None
        raw = None
        try:
            raw = await self._complete_json(
                [
                    {"role": "system", "content": self._SEARCH_PLANNER_TEMPLATE},
                    {"role": "user", "content": f"用户问题：{question}"},
                ],
                temperature=0.0,
            )
            raw = (raw or "").strip()
        except Exception:
            logger.warning("Search planner call failed — falling back to raw question")
            return None
        if not isinstance(raw, str) or not raw:
            logger.warning("Search planner returned empty — falling back to raw question")
            return None
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start < 0 or json_end <= json_start:
            logger.warning("Search planner returned non-JSON — falling back to raw question")
            return None
        try:
            result = json.loads(raw[json_start:json_end])
        except json.JSONDecodeError:
            logger.warning("Search planner JSON unparseable — falling back to raw question")
            return None
        queries = result.get("queries") if isinstance(result, dict) else None
        if not isinstance(queries, list):
            return None
        cleaned = [str(q).strip()[:200] for q in queries if isinstance(q, str) and str(q).strip()]
        return cleaned[:3] or None

    async def _complete_json(
        self,
        messages: List[Dict[str, Any]],
        *,
        temperature: float,
        max_tokens: Optional[int] = None,
    ) -> str:
        """complete_chat with JSON Output mode (response_format json_object).

        DeepSeek, DashScope (OpenAI-compatible) and MiMo all accept the same
        parameter (verified against vendor docs 2026-07-20), so it is sent
        unconditionally; a provider that rejects it (400 mentioning
        response_format) disables the mode for this AgentLoop instance and
        the call is retried without it. max_tokens=None (2026-08-18 user
        directive): unset == provider default max output — no cap by default.
        """
        kwargs: Dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        kwargs["extra_body"] = build_thinking_extra_body(self.provider_type, False, thinking_budget=self.thinking_budget)
        if self._json_mode_supported:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            _audit_to = config.agent_audit_call_timeout_seconds
            if _audit_to and _audit_to > 0:
                return await asyncio.wait_for(
                    self.coordinator_llm.complete_chat(messages=messages, **kwargs),
                    timeout=_audit_to,
                )
            return await self.coordinator_llm.complete_chat(messages=messages, **kwargs)
        except Exception as exc:
            if "response_format" in kwargs and "response_format" in str(exc):
                logger.info(
                    "Provider rejects response_format — disabling JSON mode for this loop"
                )
                self._json_mode_supported = False
                kwargs.pop("response_format")
                return await self.coordinator_llm.complete_chat(messages=messages, **kwargs)
            raise

    async def _coordinate(
        self,
        messages: List[Dict[str, Any]],
    ) -> Optional[dict]:
        """Classify the user's latest message and optionally generate a direct
        reply.  Returns ``{"route": "direct_reply", "response": "..."}`` when
        the message can be answered without tools, ``{"route": "tool_loop",
        "response": ""}`` when tools are needed, or ``None`` when coordination
        should be skipped entirely (deathmatch, skill execution, errors).

        All intent judgment (routing, whether tools are needed, what the user
        actually wants this turn) is done by the coordinator LLM — no regex
        pre-classifiers. The LLM generalizes to arbitrary phrasings; regexes
        only cover the patterns someone happened to enumerate (user feedback
        2026-07-20: "无限多可能性的垃圾实现").
        """
        if self.deathmatch_manager is not None:
            return None

        last_user_msg = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break
        if not last_user_msg:
            return None

        stripped = last_user_msg.strip()
        if stripped.startswith("[skill:") or stripped.startswith("[使用技能:"):
            return None

        # Build compact context: coordinator system prompt + last 5 conversation
        # messages (skip the full agent system prompt to save tokens).
        # Only user/assistant text messages are forwarded — tool-role messages
        # require tool_call_id and assistant messages with tool_calls need the
        # matching tool responses; sending either verbatim makes strict APIs
        # (DeepSeek) reject the call with HTTP 400, silently disabling the
        # coordinator for the whole conversation (conv f75236d2, 2026-07-20).
        recent_msgs: List[Dict[str, Any]] = []
        for msg in messages[-6:]:
            role = msg.get("role", "user")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                recent_msgs.append({"role": role, "content": content[:800]})

        identity_clause = ""
        if self._identity_context:
            identity_clause = (
                "如果下方提供了用户为你设置的身份/昵称信息，优先使用那个名称；"
                "没有时就以'Weave Thinker'自称。严禁编造不存在的名称。\n"
            )
        else:
            identity_clause = "没有用户自定义名称时就以'Weave Thinker'自称。严禁编造不存在的名称。\n"

        system_prompt = self._COORDINATOR_CLASSIFY_TEMPLATE.format(
            identity_clause=identity_clause
        )
        if self._identity_context:
            system_prompt += "\n\n用户记忆提供的身份/昵称信息（回答身份问题时参考）：\n" + self._identity_context

        coord_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ] + recent_msgs

        self._last_coord_messages = coord_messages

        raw = ""
        for _attempt in range(2):
            try:
                raw = await self._complete_json(
                    coord_messages, temperature=0.3
                )
            except Exception:
                logger.exception("Coordinator LLM call failed, falling through to tool_loop")
                return None
            raw = (raw or "").strip()
            if raw:
                # Non-empty: check whether it is usable routing JSON. A plain
                # markdown answer (topic drift, e.g. conv 149ce886 2026-08-02:
                # coordinator answered the PREVIOUS question with [3]-citations
                # instead of routing) is NOT usable — retry once with a format
                # reminder instead of dropping the routing decision entirely.
                # Schema-validated: parseable JSON with a valid route field
                # counts as a routing decision; a drift answer that merely
                # EMBEDS some JSON literal (e.g. quoting an API response) does
                # NOT — it must also retry (review finding 2026-08-02).
                _js = raw.find("{")
                _je = raw.rfind("}") + 1
                _usable_routing = False
                if _js >= 0 and _je > _js:
                    try:
                        _probe = json.loads(raw[_js:_je])
                        if isinstance(_probe, dict):
                            _r = _probe.get("route")
                            _usable_routing = isinstance(_r, str) and _r in ("direct_reply", "tool_loop")
                    except json.JSONDecodeError:
                        pass
                if _usable_routing:
                    break
                if _attempt == 0:
                    coord_messages = coord_messages + [{
                        "role": "user",
                        "content": "提醒：你是路由器，不是在回答用户。只输出JSON，不要输出任何其他内容。",
                    }]
                    logger.warning("Coordinator returned non-JSON, retrying with format reminder: %s", raw[:80])
                    continue
                break
            # Empty completion — retry once with an explicit format reminder
            # (transient reasoning-budget exhaustion must not silently disable
            # the coordinator, conv 3bc79c4c 11:33).
            if _attempt == 0:
                coord_messages = coord_messages + [{
                    "role": "user",
                    "content": "提醒：你是路由器，不是在回答用户。只输出JSON，不要输出任何其他内容。",
                }]
                logger.info("Coordinator returned empty, retrying with format reminder")
        if not raw:
            return None

        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()

        # Extract JSON object from the response.
        result: Optional[Dict[str, Any]] = None
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            try:
                result = json.loads(raw[json_start:json_end])
            except json.JSONDecodeError:
                result = None
        if not isinstance(result, dict):
            # Salvage: the model answered the user DIRECTLY instead of routing
            # (conv 3bc79c4c, 2026-07-20 — coordinator produced a correct answer
            # to "你觉得你和其他 agent 最大的区别是什么" but no JSON; it was
            # discarded, the main agent ran unfocused and drifted back to the
            # stale eval-methods topic). A conversational direct answer is a
            # usable direct_reply — far better than no coordination at all.
            #
            # BUT never salvage output that looks like a (truncated) routing
            # object: a JSON fragment is a FAILED routing decision, not a
            # conversational answer (conv 1c959c11, 2026-07-24 — coordinator
            # retry returned 43 chars of truncated JSON after reasoning-budget
            # exhaustion; the fragment was salvaged as direct_reply and saved
            # as the user-visible reply).
            salvaged = raw.strip()
            looks_like_route_fragment = (
                salvaged.startswith("{")
                or salvaged.startswith("[")
                or '"route"' in salvaged[:120]
            )
            if (
                salvaged
                and len(salvaged) <= 500
                and not looks_like_route_fragment
                and not self._LEAK_PATTERNS.search(salvaged)
            ):
                logger.info("Coordinator answered directly (non-JSON), using as direct_reply")
                return {
                    "route": "direct_reply",
                    "response": salvaged,
                    "expects_tools": False,
                    "search_required": False,
                    "creative_turn": False,
                    "focus": "",
                }
            logger.warning("Coordinator returned non-JSON: %s", raw[:200])
            # Truncated routing JSON (max_tokens cut the focus mid-word, e.g.
            # conv 149ce886 19:41: the JSON was valid until "…最新信息，exp").
            # A truncated routing object is still a routing DECISION — salvage
            # route + expects_tools so the fabrication guard and focus survive
            # instead of degrading to an unfocused run.
            if raw.strip().startswith("{") and '"route"' in raw[:120]:
                _route_m = _re.search(r'"route"\s*:\s*"([a-z_]+)"', raw)
                _exp_m = _re.search(r'"expects_tools"\s*:\s*(true|false)', raw)
                _search_m = _re.search(r'"search_required"\s*:\s*(true|false)', raw)
                _creative_m = _re.search(r'"creative_turn"\s*:\s*(true|false)', raw)
                if _route_m:
                    logger.warning(
                        "Salvaging truncated coordinator JSON: route=%s expects_tools=%s search_required=%s",
                        _route_m.group(1), _exp_m.group(1) if _exp_m else None,
                        _search_m.group(1) if _search_m else None,
                    )
                    return {
                        "route": _route_m.group(1),
                        "response": "",
                        # tool_loop routing implies tools may be needed;
                        # default True so the fabrication guards stay
                        # armed even when truncation cut the flag.
                        "expects_tools": (
                            _exp_m.group(1) == "true"
                            if _exp_m
                            else (_route_m.group(1) == "tool_loop")
                        ),
                        "search_required": (
                            _search_m.group(1) == "true" if _search_m else None
                        ),
                        "creative_turn": (
                            _creative_m.group(1) == "true" if _creative_m else False
                        ),
                        "focus": "",
                    }
            return None

        route = result.get("route", "tool_loop")
        expects_tools = result.get("expects_tools")
        if not isinstance(expects_tools, bool):
            expects_tools = None
        search_required = result.get("search_required")
        if not isinstance(search_required, bool):
            search_required = None
        creative_turn = result.get("creative_turn")
        if not isinstance(creative_turn, bool):
            creative_turn = False
        focus = str(result.get("focus", "") or "").strip()[:600]

        logger.info(
            "Coordinator route=%s expects_tools=%s search_required=%s creative_turn=%s focus=%s",
            route, expects_tools, search_required, creative_turn, focus[:80],
        )
        return {
            "route": route,
            "response": "",
            "expects_tools": expects_tools,
            "search_required": search_required,
            "creative_turn": creative_turn,
            "focus": focus,
        }

    async def _stream_direct_reply(
        self, state: "AgentLoopState"
    ) -> AsyncIterator[Dict[str, Any]]:
        """Phase 2 of two-phase streaming: produce a streamed direct reply.

        Uses the coordinator's classification context (same system prompt)
        but instructs the model to answer conversationally instead of
        outputting JSON.  This replaces the old single-shot
        ``await event_queue.put({"content": coord_result["response"]})``
        so the user sees tokens arrive incrementally.
        """
        if not hasattr(self, "_last_coord_messages") or not self._last_coord_messages:
            return

        messages = list(self._last_coord_messages)

        identity_clause = ""
        if self._identity_context:
            identity_clause = (
                "如果下方提供了用户为你设置的身份/昵称信息，优先使用那个名称；"
                "没有时就以'Weave Thinker'自称。严禁编造不存在的名称。\n"
            )
        else:
            identity_clause = "没有用户自定义名称时就以'Weave Thinker'自称。严禁编造不存在的名称。\n"

        # The direct reply must NOT reuse the coordinator's routing template:
        # it is full of "output JSON only" instructions, and thinking models
        # (qwen) then echo the routing JSON as the visible answer instead of
        # replying conversationally (conv ba2be2dd, 2026-07-31). Use a clean
        # conversational prompt plus the coordinator's intent focus.
        answer_system = "你是 AI 助手 Weave Thinker。\n" + identity_clause
        if self._identity_context:
            answer_system += "\n\n用户记忆提供的身份/昵称信息（回答身份问题时参考）：\n" + self._identity_context
        _focus = (getattr(state, "turn_focus", "") or "").strip()
        if _focus:
            answer_system += f"\n\n本轮用户意图（仅供把握回答方向，不要在回答中提及它）：{_focus}"
        answer_system += "\n\n请直接自然、口语化地回答用户的最新消息（不要输出JSON，就像正常对话一样）。"

        messages[0] = {"role": "system", "content": answer_system}

        # Thinking models (qwen on vLLM) easily burn thousands of tokens on
        # reasoning alone; an 800-token cap then yields an EMPTY reply —
        # nothing is saved and the user sees the answer vanish (conv d65f5eee).
        # Per the thinking_budget contract: unset budget means UNLIMITED
        # thinking, so when reasoning is enabled we do NOT cap max_tokens at
        # all (same as every other stream call in this class — they all rely
        # on the provider/config default). A user-set thinking_budget still
        # flows through extra_body (chat_template_kwargs on vLLM).
        # Safety net: if reasoning was produced but no content, retry once
        # with thinking disabled so the conversation never ends unsavable.
        produced_content = False
        produced_reasoning = False
        _kwargs: Dict[str, Any] = self._sampling_kwargs(self.enable_reasoning)
        if not _kwargs:
            _kwargs = {"temperature": 0.7}
        async for chunk in self.llm.stream_chat_structured(
            messages,
            extra_body=build_thinking_extra_body(self.provider_type, self.enable_reasoning, self.reasoning_effort, thinking_budget=self.thinking_budget, preserve_thinking=self.preserve_thinking),
            **_kwargs,
        ):
            ctype = chunk.get("type")
            data = chunk.get("data")
            if ctype == "content" and data:
                produced_content = True
                yield {"content": data}
            elif ctype == "reasoning" and data:
                produced_reasoning = True
                yield {"reasoning_content": data}

        if not produced_content and produced_reasoning:
            logger.warning(
                "Direct reply produced reasoning but no content "
                "(provider=%s, enable_reasoning=%s) — retrying with thinking disabled",
                self.provider_type, self.enable_reasoning,
            )
            async for chunk in self.llm.stream_chat_structured(
                messages, temperature=0.7,
                extra_body=build_thinking_extra_body(self.provider_type, False),
            ):
                ctype = chunk.get("type")
                data = chunk.get("data")
                if ctype == "content" and data:
                    yield {"content": data}

    def _maybe_load_lazy_tools(self, state: Any) -> None:
        """Append lazy tool schemas once a trigger tool has been called.

        Called at the top of each tool-loop iteration. The trigger for the
        browser interaction sub-tools is a call to the ``browser`` entry tool:
        the model has signaled web capability is needed, so the interaction
        sub-tools become visible from the next iteration on.
        """
        if self._lazy_loaded or not self._lazy_tool_names:
            return
        tool_results = getattr(state, "tool_results", None) or []
        if any(getattr(tr, "name", None) == "browser" for tr in tool_results):
            self.tool_schemas = list(self.tool_schemas) + list(self._lazy_schemas)
            self._active_tool_names.update(self._lazy_tool_names)
            self._lazy_loaded = True
            logger.info(
                "Lazy tools loaded after browser call: %s",
                sorted(self._lazy_tool_names),
            )

    async def run(
        self,
        messages: List[Dict[str, Any]],
        *,
        session_factory: Any = None,
        db: Any = None,
        user: Any = None,
        conversation: Any = None,
        assistant: Any = None,
        precomputed_coord: Any = _UNSET,
    ) -> AsyncIterator[dict]:
        state = AgentLoopState(messages=list(messages))
        # Introspection hook for tests/diagnostics (stash, counters, flags).
        self._last_state = state
        state.max_web_searches = config.web_search_max_rounds * 3
        state.budget = IterationBudget(self.max_iterations)
        # The auditor's system prompt names the assistant being audited
        # (2026-08-12: was hardcoded "Weave Thinker").
        self._audit_assistant_name = (getattr(assistant, "name", None) or "AI助手")
        inactivity = config.agent_tool_loop_conversation_timeout
        if inactivity > 0:
            state.last_activity_at = asyncio.get_event_loop().time()
            state.inactivity_timeout_seconds = inactivity
        event_queue: asyncio.Queue = asyncio.Queue(maxsize=256)

        # Backwards compatibility: callers used to pass an open session as `db`.
        # If `db` is provided and no explicit factory is given, wrap it in a
        # factory that yields the existing session. New callers should pass
        # `session_factory=AsyncSessionLocal` instead.
        _factory = session_factory or self.session_factory
        if _factory is None and db is not None:
            _factory = lambda: _SessionContext(db)

        async def _keepalive(task_cancelled: asyncio.Event):
            interval = config.agent_tool_loop.get("keepalive_interval_seconds", 10)
            while not task_cancelled.is_set():
                await asyncio.sleep(interval)
                if not task_cancelled.is_set():
                    try:
                        event_queue.put_nowait({"ping": True})
                    except asyncio.QueueFull:
                        pass

        async def _produce():
            try:
                # ── Coordinator phase ────────────────────────────────────
                # Lightweight semantic router: classify the user's message
                # before entering the ReAct tool loop.  If the message is a
                # simple conversational question (identity, greeting, etc.),
                # answer directly and skip the tool loop entirely.
                # §5.2 工程要求 1：调用方可提前并发启动 coordinator（precomputed_coord），
                # 与记忆召回/gather 准备工作重叠（取 max 非 sum）；未提供则此处串行调用。
                if precomputed_coord is not _UNSET:
                    coord_result = precomputed_coord
                else:
                    coord_result = await self._coordinate(state.messages)
                if coord_result:
                    state.turn_focus = coord_result.get("focus") or ""
                    state.expects_tools = coord_result.get("expects_tools")
                    state.search_required = coord_result.get("search_required")
                    state.creative_turn = bool(coord_result.get("creative_turn"))
                await event_queue.put({"coord_done": True})
                # Route signal for the SSE layer: chat.py uses it to gate
                # pre-tool text streaming on tool-requiring turns (a fabricated
                # "I searched" claim must never flash before the auto-search).
                await event_queue.put({
                    "coordinator": {
                        "route": (coord_result or {}).get("route"),
                        "expects_tools": state.expects_tools,
                    },
                })
                # Capture the user's actual question for this turn BEFORE any
                # system-generated user messages (tool guards, nudges, synthesis
                # prompts) are appended — _final_thinking anchors on it.
                for _m in reversed(state.messages):
                    if _m.get("role") == "user" and not _m.get("_ephemeral"):
                        state.turn_question = str(_m.get("content") or "").strip()
                        break
                # Skill turns return None from _coordinate BY DESIGN
                # (agent_loop.py:1023 — "[使用技能:"/"skill:" prefix skips
                # coordination) — never treat them as coordinator failure
                # (review finding 2026-08-02: fallback would fire a spurious
                # planner call on every skill turn).
                _is_skill_turn = state.turn_question.startswith(("[skill:", "[使用技能:"))
                if not coord_result and not _is_skill_turn and self.deathmatch_manager is None:
                    # Coordinator failed entirely (topic-drift non-JSON that
                    # survived the retry, LLM failure, or unsalvageable output —
                    # conv 149ce886 2026-08-02: coordinator answered the PREVIOUS
                    # question instead of routing, search_required was never set,
                    # auto-search gate stayed disarmed, user got a memory-only
                    # answer). The search planner subagent makes the fallback
                    # judgment: it decides whether the question needs retrieval
                    # and how to query it — agentic, no regex. When it returns
                    # queries, treat the turn as a search-required turn.
                    _planner_q = await self._plan_search_queries(state.turn_question)
                    if _planner_q:
                        state.search_required = True
                        logger.info(
                            "Coordinator unavailable — planner fallback judged search_required=True queries=%s",
                            _planner_q[:3],
                        )
                    else:
                        logger.warning(
                            "Coordinator unavailable and planner found no search queries — no fallback search"
                        )
                if coord_result and coord_result.get("route") == "direct_reply":
                    # Contradictory coordinator output (direct_reply but
                    # search_required=true) would bypass the search-request
                    # enforcement gate below — a user who asked to search must
                    # get a real search even if the coordinator mis-routed.
                    if (
                        state.search_required is True
                        and "web_search" in self._active_tool_names
                        and config.web_search_enabled
                    ):
                        logger.warning(
                            "Coordinator direct_reply overridden to tool_loop (search_required=True)"
                        )
                    else:
                        async for event in self._stream_direct_reply(state):
                            try:
                                await event_queue.put(event)
                            except asyncio.QueueFull:
                                pass
                        return
                # Inject the coordinator's intent focus so the main agent does
                # not get anchored on the previous conversation topic (short
                # follow-ups were getting long off-topic repeats of earlier
                # answers — conv 38df82bd, 2026-07-20). Ephemeral: never saved.
                # When the coordinator is unavailable (LLM failure, empty, or
                # unsalvageable non-JSON), inject a GENERIC turn-discipline
                # reminder instead — it encodes no intent, only the universal
                # rule "answer the LATEST message, don't drift back to stale
                # topics" (conv 3bc79c4c: coordinator non-JSON → no focus →
                # main agent re-answered the eval-methods question again).
                # Never injected in deathmatch mode (the goal loop drives its
                # own continuation prompts).
                _focus_text = state.turn_focus
                if not _focus_text and self.deathmatch_manager is None:
                    _focus_text = (
                        "只针对用户的最新消息作答：先判断它与上文是否属于同一话题；"
                        "如果是话题切换，严禁延续之前的话题或重复已经回答过的内容；"
                        "回答篇幅与问题匹配——简短的问题直接、简短地回答，"
                        "不要重新展开之前已经讲过的内容。"
                        "笔记写入约束：用户未明确要求修改笔记，严禁调用 notes 工具的写入操作"
                        "（create_note/update_note/delete_note/create_notebook/update_notebook/delete_notebook），仅允许读取笔记。"
                    )
                if _focus_text:
                    _inject_directive(
                        state,
                        "[本轮任务聚焦 — 协调器注入，严禁在回答中复述此段]\n" + _focus_text,
                        _ephemeral="turn_focus",
                    )

                # ── Normal ReAct tool loop ───────────────────────────────
                async for event in self._run_loop(state, _factory, user, conversation, assistant):
                    try:
                        await event_queue.put(event)
                    except asyncio.QueueFull:
                        logger.warning("Agent loop event queue full, dropping event type=%s", event.get("type", "?"))
            except asyncio.CancelledError:
                await event_queue.put({"error": "Agent loop cancelled"})
            except Exception:
                logger.exception("Agent loop failed")
                await event_queue.put({"error": "Agent loop encountered an error"})
            finally:
                await event_queue.put(None)

        cancelled = asyncio.Event()
        keepalive_task = asyncio.create_task(_keepalive(cancelled))
        producer_task = asyncio.create_task(_produce())

        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event
        finally:
            cancelled.set()
            state.cancelled = True
            keepalive_task.cancel()
            if not producer_task.done():
                producer_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
            try:
                await producer_task
            except asyncio.CancelledError:
                pass

    def _ensure_compressor(self) -> None:
        """Lazily build the ContextCompressor with the same config defaults
        used at loop top (threshold, protected margins, context length, and
        deathmatch's more aggressive profile). Shared by the token-threshold
        trigger and the 遵循词 (canary) trigger."""
        if self._compressor is not None:
            return
        from app.services.context_compressor import ContextCompressor
        # A4.9 Important-2: llm=None — the compressor's AuxiliaryClient then
        # resolves the P0 task-local override (custom-model assistants) or
        # the operator's global [agent.auxiliary] compression_model key
        # (non-custom assistants). Passing coordinator_llm unconditionally
        # bypassed the global key for every non-custom deployment.
        self._compressor = ContextCompressor(quiet=True)
        compression_cfg = config.agent_compression
        if compression_cfg:
            self._compressor.threshold_percent = float(compression_cfg.get("threshold_percent", 0.65))
            self._compressor.protect_last_tokens = int(compression_cfg.get("protect_last_tokens", 20000))
            self._compressor.protect_first_n = int(compression_cfg.get("protect_first_n", 3))
        # Set the compressor's context_length from config so the
        # compression threshold matches the actual model context window.
        _ctx_len = config.agent_compression_context_length
        if _ctx_len > 0:
            self._compressor.update_context_length(_ctx_len)
        # Deathmatch mode: use more aggressive compression settings to
        # handle high iteration counts (up to 9999 turns). Lower the
        # threshold so compression triggers sooner, and protect more
        # recent context so the agent doesn't lose the plan/goal.
        if self.deathmatch_manager and self.deathmatch_manager.is_goal_active:
            self._compressor.threshold_percent = min(self._compressor.threshold_percent, 0.5)
            self._compressor.protect_last_tokens = max(self._compressor.protect_last_tokens, 30000)

    async def _run_loop(
        self,
        state: AgentLoopState,
        session_factory: Any,
        user: Any,
        conversation: Any,
        assistant: Any,
    ) -> AsyncIterator[dict]:
        # In deathmatch goal-loop mode the judge alone decides when to stop, so
        # the loop must stay alive even at 0 budget: the consume() failure then
        # routes into the exhaustion handler below, which evaluates the goal and
        # resets the budget for the next turn instead of terminating the run.
        while (await state.budget.get_remaining()) > 0 or state.budget_grace_call or self._skip_guardrails():
            if state.budget_grace_call:
                state.budget_grace_call = False
                async for event in self._grace_call(state):
                    yield event
                return

            if not await state.budget.consume():
                # Deathmatch mode: when the tool-loop budget is exhausted,
                # run the deathmatch evaluation BEFORE breaking. If the judge
                # says "continue", refund the budget and start the next
                # deathmatch turn. This prevents the tool-loop budget from
                # prematurely terminating the deathmatch goal loop.
                if self._skip_guardrails() and self.deathmatch_manager:
                    dm = self.deathmatch_manager
                    logger.info(
                        "Deathmatch budget exhausted: running evaluation (turns=%d, iterations=%d)",
                        dm._conv.deathmatch_turns, state.iterations,
                    )
                    # Build evaluation content from whatever was produced
                    _budget_eval_content = assistant_content.strip() if assistant_content else ""
                    if not _budget_eval_content and state.tool_results:
                        _parts = [f"[{tr.name}] {tr.result[:200]}" for tr in state.tool_results[-3:]]
                        _budget_eval_content = "\n".join(_parts)
                    if not _budget_eval_content:
                        _budget_eval_content = "(tool loop budget exhausted, no content produced)"

                    _budget_decision = await self._safe_judge(
                        dm, _budget_eval_content,
                        user_initiated=False,
                        workspace_path=self.workspace_path or "",
                        state=state,
                    )
                    _budget_should_continue = _budget_decision.get("should_continue", False)
                    async for _ev in self._emit_deathmatch_verdict(dm, _budget_decision):
                        yield _ev
                    self._mark_activity(state)
                    if _budget_should_continue and _budget_decision.get("continuation_prompt"):
                        _inject_directive(state, _budget_decision["continuation_prompt"])
                        await state.budget.reset()
                        logger.info("Deathmatch: budget reset for next turn (turns=%d)", dm._conv.deathmatch_turns)
                        continue
                    elif _budget_decision.get("verdict") == "done":
                        async for _ev in self._handle_deathmatch_done(dm, _budget_decision, state):
                            yield _ev
                        return
                    else:
                        yield {"done": True}
                        return
                break

            state.iterations += 1
            logger.debug("Agent loop iteration %d/%d (budget remaining: %d)",
                         state.iterations, self.max_iterations, await state.budget.get_remaining())

            if self._check_inactivity(state):
                elapsed = asyncio.get_event_loop().time() - state.last_activity_at
                logger.warning(
                    "AgentLoop inactivity timeout: no activity for %.1fs > %.1fs (iterations=%d)",
                    elapsed, state.inactivity_timeout_seconds, state.iterations,
                )
                yield {
                    "agent_step": {
                        "name": "conversation_timeout",
                        "title": "对话无响应",
                        "content": (
                            f"对话已卡住超过 {state.inactivity_timeout_seconds:.0f} 秒"
                            f"（无新内容产出 {elapsed:.0f} 秒），系统自动终止剩余步骤。"
                        ),
                        "step_type": "recovery",
                    }
                }
                if self._skip_guardrails() and self.deathmatch_manager:
                    # DEATHMATCH: inactivity must NEVER silently kill the goal
                    # loop (conv 6b0faf81: a slow LLM call on a multi-hundred-
                    # thousand-token context exceeds the 300s inactivity
                    # window → the old code ran _grace_call and returned,
                    # leaving deathmatch_status="active" with no terminal
                    # message; the reconcile flipped it to "paused" 18 min
                    # later — a silent stop on a healthy long task). Route
                    # through the judge like the budget-exhaustion path: a
                    # "continue" verdict refreshes activity and keeps going;
                    # a done/stop verdict persists the terminal state.
                    # Bounded (A4.9 review I5): consecutive no-activity cycles
                    # with no real work escalate to a visible stop instead of
                    # spinning for the 7-day wall budget on a dead provider.
                    dm = self.deathmatch_manager
                    state.consecutive_inactivity_cycles += 1
                    _max_inact_cycles = max(
                        int(config.agent_tool_loop.get("max_inactivity_judge_cycles", 4)),
                        1,
                    )
                    if state.consecutive_inactivity_cycles >= _max_inact_cycles:
                        logger.warning(
                            "Deathmatch: %d consecutive inactivity cycles (turns=%d) — "
                            "stopping with visible state instead of spinning",
                            state.consecutive_inactivity_cycles,
                            dm._conv.deathmatch_turns,
                        )
                        # I7: the paused state must actually LAND in the DB.
                        # Mutating dm._conv alone is invisible — task_db is
                        # only committed by chat.py's per-verdict handler.
                        # Emit the verdict event so the handler syncs +
                        # commits status and persists a visible message.
                        async for _ev in self._stop_inactivity_paused(dm):
                            yield _ev
                        yield {"done": True}
                        return
                    _inact_content = assistant_content.strip() if assistant_content else (
                        "(inactivity timeout — no content produced)"
                    )
                    _inact_decision = await self._safe_judge(
                        dm, _inact_content,
                        user_initiated=False,
                        workspace_path=self.workspace_path or "",
                        state=state,
                    )
                    _inact_should_continue = _inact_decision.get("should_continue", False)
                    async for _ev in self._emit_deathmatch_verdict(dm, _inact_decision):
                        yield _ev
                    if _inact_decision.get("verdict") == "done":
                        async for _ev in self._handle_deathmatch_done(dm, _inact_decision, state):
                            yield _ev
                        return
                    if _inact_should_continue and _inact_decision.get("continuation_prompt"):
                        _inject_directive(state, _inact_decision["continuation_prompt"])
                        await state.budget.reset()
                        # Refresh the inactivity timer WITHOUT resetting the
                        # consecutive-cycle counter (A4.9 review I5): resetting
                        # via _mark_activity on judge-continue made the bound
                        # unreachable — a dead provider would spin forever
                        # because each continue reset the counter. Only REAL
                        # output (content/tool events) resets it via
                        # _mark_activity.
                        state.last_activity_at = asyncio.get_event_loop().time()
                        logger.info(
                            "Deathmatch: inactivity routed through judge — continue (turns=%d)",
                            dm._conv.deathmatch_turns,
                        )
                        continue
                    # Judge said continue WITHOUT a continuation prompt, or
                    # ordered a full stop: persist a visible paused state
                    # instead of a silent done (A4.9 review I7 — never leave
                    # the zombie outcome the fix was built to eliminate).
                    async for _ev in self._stop_inactivity_paused(dm):
                        yield _ev
                    yield {"done": True}
                    return
                state.budget_grace_call = True
                async for event in self._grace_call(state):
                    yield event
                return

            yield {"ping": True}

            if self.enable_compression and self._compressor is None:
                self._ensure_compressor()

            # Deathmatch mode: force compression when the message list grows
            # too large, even if token count is below the threshold. This
            # prevents unbounded growth with high max_turns (e.g. 9999).
            if (self._compressor and self.deathmatch_manager
                    and self.deathmatch_manager.is_goal_active
                    and len(state.messages) > 80):
                from app.services.context_compressor import estimate_request_tokens_rough
                before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                state.messages = await self._compressor.compress_async(state.messages)
                after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                logger.info("Deathmatch forced compression (msg count=%d): %d->%d tokens",
                            len(state.messages), before, after)
                yield {"compression": {"before": before, "after": after}}
            elif self._compressor and self._compressor.should_compress(state.messages):
                from app.services.context_compressor import estimate_request_tokens_rough
                before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                state.messages = await self._compressor.compress_async(state.messages)
                after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                logger.info("Compressed %d->%d tokens", before, after)
                yield {"compression": {"before": before, "after": after}}

            sanitize_messages_surrogates(state.messages)
            self._repair_message_sequence(state.messages)

            self._maybe_load_lazy_tools(state)

            active_tool_schemas = None
            if self.tool_schemas:
                if state.force_final_answer:
                    # The force-final-answer guard restricts the tools OFFERED
                    # to the model to execute_code only (final answer must be
                    # text or a code-generated file; conv aadb26a3 2026-07-18
                    # ran 41 browser_snapshot calls via DSML after the guard
                    # fired 4 times). NOTE: previously the execute_code
                    # offer was gated on `not state.executed_final_code`,
                    # which dropped tools to NONE after the model's first
                    # final code run — stranding a mid-task model (conv
                    # 517140ca 2026-08-08: chapter 5 at 979/3500 chars) with
                    # no way to continue → silent empty answer. Keep
                    # execute_code available for the forced phase, BUT
                    # bounded by max_force_stage_rounds (A4.9 I1): execute_code
                    # rounds refund the budget, so without this hard cap the
                    # forced phase could run unbounded. Past the cap the model
                    # must answer in text (empty answers then hit the bounded
                    # empty-answer retry).
                    if state.force_stage_rounds < self.max_force_stage_rounds:
                        active_tool_schemas = [ts for ts in self.tool_schemas
                                               if ts.get("function", {}).get("name") in ("execute_code",)]
                    else:
                        logger.warning(
                            "Force-stage round cap reached (%d/%d) — execute_code no longer offered",
                            state.force_stage_rounds, self.max_force_stage_rounds,
                        )
                else:
                    active_tool_schemas = self.tool_schemas

            tool_calls_collected: List[Dict[str, Any]] = []
            assistant_content = ""
            assistant_raw_content = ""
            reasoning_content = ""
            finish_reason: Optional[str] = None
            state.dsml_active = False
            state.dsml_tail = ""

            # Sub-agents (tool-calling iterations) historically NEVER used
            # thinking — the answer was suppressed and regenerated later by
            # _final_thinking (a second full pass: TTFT 76-92s, conv daa19eac).
            # Live-thinking mode: iterations think+stream immediately (one
            # pass, opencode-style); _final_thinking becomes fallback-only.
            # Empty-answer retries (conv 517140ca) disable thinking: a
            # silent-empty response usually means the reasoning pass burned
            # the token budget on a huge context — the smaller, no-think
            # request recovers (llm_empty_content family).
            _use_thinking = (
                self._live_thinking_enabled()
                and not state.retry_thinking_off
                and not state.revision_thinking_off
            )
            logger.info(
                "AgentLoop iteration %d: enable_reasoning=%s, total_tool_calls=%d, iterations=%d, _use_thinking=%s, reasoning_effort=%s",
                state.iterations, self.enable_reasoning, state.total_tool_calls, state.iterations, _use_thinking, self.reasoning_effort
            )

            try:
                # PHASE 3: route iteration calls through iteration_llm with
                # its own provider_type so a separate (cheaper, no-think)
                # client can serve the loop while _final_thinking still uses
                # the main reasoner.
                _iter_extra_body = (
                    self.iteration_extra_body_override
                    if self.iteration_extra_body_override is not None
                    else build_thinking_extra_body(
                        self.iteration_provider_type, _use_thinking, self.reasoning_effort, thinking_budget=self.thinking_budget, preserve_thinking=self.preserve_thinking
                    )
                )
                # A4.9 I1: sampling sets apply only to the qwen3.8_vllm
                # iteration provider AND only when the operator did not take
                # explicit extra_body control (iteration_extra_body_override
                # bypasses build_thinking_extra_body — A4.9 I2).
                _iter_kwargs = {}
                if self.iteration_extra_body_override is None:
                    _iter_kwargs = self._sampling_kwargs(_use_thinking, self.iteration_provider_type)
                if not _iter_kwargs:
                    _iter_kwargs = {"temperature": config.default_temperature}
                llm_stream = self.iteration_llm.stream_chat_structured(
                    state.messages,
                    tools=active_tool_schemas,
                    extra_body=_iter_extra_body,
                    **_iter_kwargs,
                )
                # PHASE 4: track cumulative reasoning chars and per-iteration
                # wall-clock so a thinking-by-default sub-task model can't
                # blow up the context window or hang the round.
                _iter_started_at = asyncio.get_event_loop().time()
                _reasoning_char_cap = config.agent_subtask_reasoning_char_cap
                _iter_timeout = config.agent_subtask_iteration_timeout
                _reasoning_truncated = False
                async for event in llm_stream:
                    event_type = event["type"]
                    event_data = event["data"]

                    # PHASE 4: enforce per-iteration timeout AND conversation
                    # inactivity timeout. The inactivity check catches the
                    # case where the agent is stuck in a loop producing no
                    # actual content (e.g. deathmatch judge keeps saying
                    # "continue" but the model outputs nothing).
                    # BOTH checks MUST run on heartbeat wake-ups too: a
                    # stalled upstream stream yields no real chunks (conv
                    # efaf8f9c 2026-08-21 hung for 60+ min, conversation slot
                    # reserved forever), so without evaluating on heartbeats
                    # these guards are unreachable dead code.
                    if _iter_timeout and (
                        asyncio.get_event_loop().time() - _iter_started_at
                    ) > _iter_timeout:
                        logger.warning(
                            "AgentLoop iteration %d: timeout after %.1fs, aborting stream",
                            state.iterations, _iter_timeout,
                        )
                        yield {
                            "agent_step": {
                                "name": "iteration_timeout",
                                "title": "迭代超时",
                                "content": f"子任务超过 {_iter_timeout:.0f}s 未完成，已中止本轮以继续后续步骤。",
                                "step_type": "recovery",
                            }
                        }
                        break

                    # Inactivity check: if the streaming loop produces zero
                    # events for too long (model connection hung), bail out.
                    if self._check_inactivity(state):
                        elapsed = asyncio.get_event_loop().time() - state.last_activity_at
                        logger.warning(
                            "AgentLoop inactivity in stream: no activity for %.1fs (iteration=%d)",
                            elapsed, state.iterations,
                        )
                        yield {
                            "agent_step": {
                                "name": "conversation_timeout",
                                "title": "对话无响应",
                                "content": (
                                    f"对话已卡住超过 {state.inactivity_timeout_seconds:.0f} 秒"
                                    f"（无新内容产出 {elapsed:.0f} 秒），系统自动终止。"
                                ),
                                "step_type": "recovery",
                            }
                        }
                        break

                    if event_type == "reasoning":
                        reasoning_content += event_data
                        self._mark_activity(state)
                        # PHASE 4: cap runaway reasoning streams from thinking-
                        # by-default sub-task models. Once we hit the cap we
                        # stop accepting more reasoning chars and break out so
                        # the outer loop moves on. The model's tool_calls /
                        # content collected so far are preserved.
                        # LIVE-THINKING: the cap is bypassed — reasoning is now
                        # the user-visible thinking stream, and cutting it
                        # mid-answer would truncate the final response itself.
                        if (
                            _reasoning_char_cap
                            and not _use_thinking
                            and len(reasoning_content) >= _reasoning_char_cap
                            and not _reasoning_truncated
                        ):
                            _reasoning_truncated = True
                            logger.warning(
                                "AgentLoop iteration %d: reasoning exceeded cap (%d chars), aborting stream",
                                state.iterations, _reasoning_char_cap,
                            )
                            yield {
                                "agent_step": {
                                    "name": "reasoning_cap",
                                    "title": "思维链截断",
                                    "content": (
                                        f"子任务思维链已达上限 {_reasoning_char_cap} 字符，"
                                        "为避免上下文溢出已提前结束本轮思考。"
                                    ),
                                    "step_type": "recovery",
                                }
                            }
                            break
                        # PHASE 1C: middle-iteration reasoning is not the user-visible
                        # "thinking". The final synthesis pass (_final_thinking) is the
                        # only place that surfaces reasoning to the user. Drop here
                        # both to avoid mixing iteration noise with final reasoning
                        # and to prevent forced-reasoning models from leaking long CoT
                        # into the UI before the final phase. When reasoning is
                        # disabled altogether, drop unconditionally.
                        # LIVE-THINKING: stream it — the thinking panel fills live
                        # from the very first iteration (opencode-style TTFT).
                        if not self.enable_reasoning:
                            continue
                        if _use_thinking:
                            yield {"reasoning_content": event_data, "phase": "final"}
                            continue
                        # Even when enabled: middle-iteration reasoning is dropped;
                        # _final_thinking provides the visible reasoning stream.
                        continue

                    elif event_type == "content":
                        content_text = event_data
                        if '<think' in content_text:
                            content_text = _re.sub(r'<think[^>]*>.*?</think\s*>', '', content_text, flags=_re.DOTALL)
                            if '<think' in content_text:
                                content_text = _re.sub(r'<think[^>]*>.*', '', content_text, flags=_re.DOTALL)
                        # Strip tool_call XML from content — some providers leak
                        # tool call markup inline instead of using delta.tool_calls.
                        content_text = _TOOL_CALL_RE.sub('', content_text)
                        content_text = _TOOL_INVOKE_RE.sub('', content_text)
                        content_text = _TOOL_CALL_XML_RE.sub('', content_text)
                        content_text = _TOOL_RESULT_RE.sub('', content_text)
                        # Stateful DSML stripping: handles blocks that are split
                        # across multiple stream chunks.  The previous approach
                        # only removed *complete* blocks per-chunk, so partial
                        # DSML tags leaked into the user-visible stream and were
                        # subsequently persisted to the database.
                        content_text, state.dsml_active, state.dsml_tail = _strip_dsml_streaming(
                            content_text, state.dsml_active, state.dsml_tail
                        )
                        assistant_raw_content += event_data
                        if content_text:
                            assistant_content += content_text
                            # When reasoning is enabled, suppress all middle-iteration
                            # content; the single final answer is produced by
                            # _final_thinking after all tools finish (or directly when
                            # no tools are called). When reasoning is disabled, yield
                            # content normally.
                            # EXCEPTION: In deathmatch mode, always yield intermediate
                            # content so the user can see progress and the judge has
                            # fresh context to evaluate.
                            # LIVE-THINKING: content streams live in every iteration;
                            # the relay resets its accumulator at each tool-call
                            # boundary, so pre-tool prose never pollutes the
                            # persisted answer (same guarantee as before).
                            # EMPTY-ANSWER RETRY (conv 517140ca): the retry runs
                            # with thinking disabled (retry_thinking_off) but
                            # enable_reasoning stays True — without this clause
                            # the retried content would be suppressed AND
                            # _final_thinking would be skipped (live thinking
                            # still "on" per _live_thinking_enabled), so the
                            # recovered answer would never reach the user.
                            if (
                                not self.enable_reasoning
                                or self._skip_guardrails()
                                or _use_thinking
                                or state.retry_thinking_off
                                # LIVE 模式修正轮（thinking 关闭）直接流式出稿；
                                # 非 live 模式的答案仍由 _final_thinking 合成
                                # pass 产出（修正轮合成 pass 已按同一标志关思考）。
                                or (state.revision_thinking_off and self._live_thinking_enabled())
                            ):
                                yield {"content": content_text}
                                self._mark_activity(state)

                    elif event_type == "tool_calls":
                        tool_calls_collected = event_data
                        self._mark_activity(state)

                    elif event_type == "error":
                        should_retry, recovery_action, retry_after = await self._classify_llm_error(event_data)
                        if should_retry and (await state.budget.get_remaining()) > 0:
                            yield {
                                "agent_step": {
                                    "name": "error_recovery",
                                    "title": "LLM 错误恢复",
                                    "content": f"LLM 错误: {str(event_data)[:200]}, 策略: {recovery_action}",
                                    "step_type": "recovery",
                                }
                            }
                            if recovery_action == "compress_context" and self._compressor:
                                from app.services.context_compressor import estimate_request_tokens_rough
                                before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                state.messages = await self._compressor.compress_async(state.messages)
                                after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                logger.info("Emergency compression %d->%d tokens after LLM error", before, after)
                                yield {"context_info": {
                                    "tokens": after,
                                    "context_length": self._compressor.context_length,
                                    "compressed": True,
                                }}
                            elif recovery_action == "fallback_provider":
                                fallback_msg = "主供应商不可用，正在切换备用供应商..."
                                if self._try_fallback_provider():
                                    fallback_msg += " 已切换到备用供应商。"
                                else:
                                    fallback_msg += " 未找到可用备用供应商。"
                                yield {
                                    "agent_step": {
                                        "name": "provider_fallback",
                                        "title": "尝试备用供应商",
                                        "content": fallback_msg,
                                        "step_type": "recovery",
                                    }
                                }
                            delay = retry_after if retry_after is not None else 2.0
                            await asyncio.sleep(delay)
                            continue
                        yield {"error": event_data}
                        return

                    elif event_type == "done":
                        if isinstance(event_data, dict):
                            finish_reason = event_data.get("finish_reason")
                        state.finish_reason = finish_reason

            except asyncio.TimeoutError:
                logger.warning("Main LLM stream timed out")
                yield {"error": "LLM响应超时, 请简化问题后重试。"}
                if self.deathmatch_manager and self.deathmatch_manager.is_active:
                    yield {"content": "死磕模式: LLM 响应超时，请简化问题后重试。"}
                    yield {"done": True}
                return
            except Exception as e:
                should_retry, recovery_action, retry_after = await self._classify_llm_error(e)
                if should_retry and (await state.budget.get_remaining()) > 0:
                    logger.warning("LLM stream error (recoverable): %s -- %s", e, recovery_action)
                    yield {
                        "agent_step": {
                            "name": "error_recovery",
                            "title": "LLM 错误恢复",
                            "content": f"LLM 错误: {str(e)[:200]}, 策略: {recovery_action}",
                            "step_type": "recovery",
                        }
                    }
                    if recovery_action == "compress_context" and self._compressor:
                        from app.services.context_compressor import estimate_request_tokens_rough
                        before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                        state.messages = await self._compressor.compress_async(state.messages)
                        after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                        logger.info("Emergency compression %d->%d tokens after LLM error", before, after)
                        yield {"context_info": {
                            "tokens": after,
                            "context_length": self._compressor.context_length,
                            "compressed": True,
                        }}
                    elif recovery_action == "fallback_provider":
                        yield {
                            "agent_step": {
                                "name": "provider_fallback",
                                "title": "尝试备用供应商",
                                "content": "主供应商不可用，正在切换备用供应商...",
                                "step_type": "recovery",
                            }
                        }
                    delay = retry_after if retry_after is not None else jittered_backoff(state.iterations)
                    await asyncio.sleep(delay)
                    continue
                yield {"error": str(e)}
                if self.deathmatch_manager and self.deathmatch_manager.is_active:
                    yield {"content": f"死磕模式: LLM 调用失败，请检查 API 配置后重试。错误: {str(e)[:200]}"}
                    yield {"done": True}
                return

            if finish_reason == "length":
                logger.warning("LLM response truncated (finish_reason=length), content=%d chars",
                               len(assistant_content))
                if tool_calls_collected:
                    valid_tc = []
                    for tc in tool_calls_collected:
                        args_raw = tc.get("function", {}).get("arguments", "")
                        if args_raw:
                            try:
                                json.loads(args_raw)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "Dropping truncated tool call %s: JSON args incomplete",
                                    tc.get("function", {}).get("name", "?"),
                                )
                                continue
                        valid_tc.append(tc)
                    tool_calls_collected = valid_tc
                    # FIX: If all tool calls were dropped due to truncation, recover by
                    # asking the LLM to break the task into smaller chunks.
                    if not tool_calls_collected:
                        logger.warning("All tool calls dropped due to truncation, requesting smaller chunks")
                        if assistant_content:
                            state.messages.append({"role": "assistant", "content": assistant_content})
                        _inject_directive(
                            state,
                            "你的上一个工具调用因为内容过长被截断了。请将任务拆分成更小的步骤：\n"
                            "1. 不要在一次工具调用中生成全部内容\n"
                            "2. 对于文件生成任务：先创建文件框架，再分批追加内容\n"
                            "3. 对于代码执行任务：将大段代码拆分成多个小的 execute_code 调用\n"
                            "请重新调用工具，使用更小的参数。",
                        )
                        continue
                else:
                    if assistant_content:
                        # Tag the truncated head: it stays in the model's
                        # context (the continuation must see it), but the
                        # auditor's previous-answer window must NOT pick it up
                        # — it is THIS answer's first half, not the previous
                        # turn's answer (A4.9 I2, conv 97ff355d wave-3).
                        state.messages.append({"role": "assistant", "content": assistant_content, "_truncated_part": True})
                        # A4.9 C1: this branch `continue`s BEFORE the segment
                        # append below — without this line the "stitched"
                        # audit only ever judged the tail fragment.
                        state.turn_content_segments.append(assistant_content)
                    _inject_directive(
                        state,
                        "你的上一个回答被截断了。请继续完成你的回答。"
                        "如果需要生成大量内容，请分多次调用工具，每次只处理一部分。",
                    )
                    # The next iteration's content is the TAIL of this truncated
                    # answer — the pre-send audit judges the STITCHED whole
                    # (segments above), not the isolated fragment.
                    state.skip_audit_once = True
                    # No-tool truncation boundary: release any held pre-tool
                    # text so the continuation streams instead of parking the
                    # whole answer until `done` (2026-08-06 user report).
                    yield {
                        "iteration_done": {
                            "iteration": state.iterations,
                            "tool_calls": 0,
                        }
                    }
                    continue

            # Extract any DeepSeek-DSML-style tool calls that the model emitted
            # inline as content instead of using native function-calling deltas.
            # This removes the markup from the final message and lets the loop
            # execute the tools.
            if assistant_raw_content:
                cleaned_raw, dsml_tool_calls = _extract_dsml_tool_calls(assistant_raw_content)
                if dsml_tool_calls and state.force_final_answer:
                    # The force-final-answer guard restricts the tools OFFERED
                    # to the model, but inline DSML markup bypasses that
                    # restriction — observed 2026-07-18 (conv aadb26a3): the
                    # model ran 41 browser_snapshot calls via DSML after the
                    # guard fired 4 times. Apply the same restriction here:
                    # only execute_code (allowed by the guard) may pass.
                    dsml_tool_calls = [
                        tc for tc in dsml_tool_calls
                        if tc.get("function", {}).get("name") == "execute_code"
                    ]
                if dsml_tool_calls:
                    existing_signatures = {
                        (tc.get("function", {}).get("name"), tc.get("function", {}).get("arguments"))
                        for tc in tool_calls_collected
                    }
                    for tc in dsml_tool_calls:
                        sig = (tc.get("function", {}).get("name"), tc.get("function", {}).get("arguments"))
                        if sig in existing_signatures:
                            continue
                        existing_signatures.add(sig)
                        tool_calls_collected.append(tc)
                        logger.info(
                            "Extracted DSML tool call: name=%s id=%s",
                            tc["function"]["name"], tc["id"],
                        )
                # Prefer the fully cleaned content (entire DSML blocks removed)
                # over the stream tag-stripped version for storage/context.
                # Also strip any remaining individual DSML tags that may have
                # survived due to partial-block streaming edge cases.
                assistant_content = _strip_dsml_all(cleaned_raw)
                # Leading-orphan punctuation defense (conv efaf8f9c 2026-08-21:
                # qwen3.8 first-token glitch produced drafts opening with '，'
                # and the auditor rejected five regenerations for the same
                # grammar defect — clean BEFORE the audit sees the draft).
                assistant_content = _strip_leading_orphan_punct(assistant_content)

            # Accumulate the cleaned assistant text for this iteration so
            # tools invoked later in the same turn (e.g. pdf_export
            # export_conversation) can access the in-progress response
            # before it is persisted to the database.
            # When tool calls are present, the pre-tool content is transient
            # and should not be treated as part of the authoritative answer.
            if assistant_content and not tool_calls_collected:
                state.turn_content_segments.append(assistant_content)

            # ── User-requested search: agentic, never synthesized ──
            # conv 149ce886 原实现：coordinator search_required=True 且模型零
            # 工具调用时确定性合成 web_search。按用户原则（2026-07-20：禁止
            # 硬编码/机械执行，判断留给 LLM）与 conv 86e51bbd 实证，此处不再
            # 合成工具调用（模型拥有执行工具的自主权）。
            #
            # 反向缺口（用户问题 2026-08-10："会不会导致用户不说联网搜索就
            # 绝对不搜索"）：coordinator 已判定用户明确要求检索
            # (search_required=True，LLM 判定、非正则)，模型却静默凭记忆回答
            # ——这是对用户意图的违背，必须确定性提醒。Agentic 语义：注入
            # directive 要求模型执行检索（工具调用由模型自主发出，不合成），
            # 而非静默放行。auditor LLM 的模板第 4 条同时把关"用户要求检索
            # 却未调用搜索工具 → 草稿不合格"。
            # conv 97ff355d: the gate used web_search_count == 0 only, so a
            # turn that did 20+ real BROWSER retrievals was still told
            # "你尚未调用 web_search 工具" — a factually wrong directive that
            # fed the model's confusion spiral. Search-class = web_search OR
            # browser* (mirrors auditor template rule 4).
            if (
                not tool_calls_collected
                and not _search_class_tool_used(state)
                and state.search_required is True
                and not self._skip_guardrails()
                and "web_search" in self._active_tool_names
                and config.web_search_enabled
            ):
                if assistant_content and not state.search_demanded:
                    logger.info(
                        "search_required=True but model produced no web_search "
                        "(iterations=%d) — injecting search-demand directive "
                        "(agentic execution, no synthesis)",
                        state.iterations,
                    )
                    state.search_demanded = True
                    yield {
                        "agent_step": {
                            "name": "search_demand",
                            "title": "需要联网检索",
                            "content": "协调器判定本轮需要联网检索，但助手尚未调用搜索工具。",
                            "step_type": "system",
                        }
                    }
                    _prune_guardrail_pairs(state)
                    _rejected_append(self, state, assistant_content, reasoning_content)
                    _inject_directive(
                        state,
                        "【轮次核对】本轮（用户最新一条消息之后）你尚未调用任何搜索工具——"
                        "上下文中可见的 web_search/检索结果全部来自之前的消息轮次，不属于本轮调用。\n"
                        "协调器判定本轮需要联网检索最新信息，请立即调用 web_search 执行真实检索，"
                        "基于检索结果回答并在正文中使用 [N] 引用标号。\n"
                        "如果你判断之前轮次的检索结果已足以准确回答本轮问题，可以直接基于这些已有结果作答，"
                        "但必须在回答中明确说明依据的是此前已获取的检索结果；"
                        "严禁声称或暗示本轮重新执行了检索，严禁编造来源。",
                        _ephemeral="search_demand",
                    )
                    continue
            # ── Tool-demand: coordinator judged the turn needs tools, model
            # made zero tool calls ──
            # conv 41d2790d 2026-08-10: coordinator said expects_tools=True
            # ("需要调用工具读取工作区文件列表并生成mermaid树状图") but the
            # model answered from memory for 3 iterations without calling ANY
            # tool — the file tree it produced was grounded in turn-1 tool
            # results, but the auditor (blind to those results) accused it of
            # fabrication, and the final hollow draft shipped un-audited.
            # Agentic nudge, never synthesized: a directive reminding the
            # model to call the appropriate tool when it has dodged every
            # tool call on a coordinator-judged tool-requiring turn. Bounded:
            # fires at most once per turn (tool_demanded), then the
            # auditor/rejection budget governs. Requires at least ONE audit
            # rejection first (a draft that passed the auditor is never
            # nudged — A4.9 finding: firing before the auditor wasted a
            # regeneration on grounded rewrites), and is skipped on
            # length-continuation/canary-retry iterations.
            if (
                not tool_calls_collected
                and state.total_tool_calls == 0
                and state.expects_tools is True
                and state.audit_rejections >= 1
                and not state.tool_demanded
                and not self._skip_guardrails()
                and not state.creative_turn
            ):
                state.tool_demanded = True
                logger.info(
                    "expects_tools=True but zero tool calls (iterations=%d) — "
                    "injecting tool-demand directive (agentic execution, no synthesis)",
                    state.iterations,
                )
                yield {
                    "agent_step": {
                        "name": "tool_demand",
                        "title": "需要调用工具",
                        "content": "协调器判定本轮回答需要调用工具获取数据，但助手尚未调用任何工具。",
                        "step_type": "system",
                    }
                }
                if assistant_content:
                    _prune_guardrail_pairs(state)
                    _rejected_append(self, state, assistant_content, reasoning_content)
                _inject_directive(
                    state,
                    "【轮次核对】本轮（用户最新一条消息之后）你尚未调用任何工具——"
                    "上下文中可见的工具调用与结果全部来自之前的消息轮次。\n"
                    "协调器判定本轮任务需要调用工具获取数据或操作文件（如读取工作区文件列表、"
                    "联网检索、执行代码），但你连续多轮没有调用任何工具。"
                    "请立即调用合适的工具获取真实数据后再回答。\n"
                    "如果之前轮次的工具结果已足以准确回答本轮问题，可以直接基于已有结果作答并如实说明依据；"
                    "严禁声称本轮调用了工具，也严禁声称回答中包含实际不存在的图表或文件树。",
                    _ephemeral="tool_demand",
                )
                continue

            if tool_calls_collected:
                state.consecutive_tool_iterations += 1
                if state.force_final_answer:
                    state.force_stage_rounds += 1

                has_budget_refund_tool = any(
                    tc.get("function", {}).get("name") in {"execute_code",}
                    for tc in tool_calls_collected
                )

                if state.consecutive_tool_iterations >= self.max_consecutive_iterations and not self._skip_guardrails():
                    logger.warning(
                        "Forcing final answer after %d consecutive tool iterations",
                        state.consecutive_tool_iterations,
                    )
                    yield {
                        "agent_step": {
                            "name": "tool_loop_guard",
                            "title": "工具调用限制",
                            "content": f"已连续调用工具 {state.consecutive_tool_iterations} 轮, 正在基于已有结果生成最终回答。",
                            "step_type": "tool",
                        }
                    }
                    tool_calls_collected = []
                    state.force_final_answer = True
                    state.consecutive_tool_iterations = 0
                    if assistant_content:
                        state.messages.append({"role": "assistant", "content": assistant_content, "reasoning_content": reasoning_content or ""})
                    else:
                        state.messages.append({"role": "assistant", "content": "[工具调用中...]", "reasoning_content": ""})
                    _inject_directive(
                        state,
                        "请基于以上工具调用结果,直接给出完整回答。"
                        "如果任务需要生成文件(Excel、PPT、Word等),请使用 execute_code 工具生成。"
                        "注意Markdown格式：标题前留空行，#后加空格，表格行之间必须有换行。",
                    )
                    continue
                else:
                    valid_tool_calls = []
                    for tc in tool_calls_collected:
                        if tc.get("function", {}).get("name") and tc.get("id"):
                            if "type" not in tc:
                                tc["type"] = "function"
                            valid_tool_calls.append(tc)

                    assistant_msg = {
                        "role": "assistant",
                        # When tool calls are present, the content generated
                        # before the tool calls is a transient pre-tool utterance.
                        # Persist the message without content so the final answer
                        # produced after tool results is authoritative and not
                        # duplicated by the model's own pre-tool prose.
                        "content": None if valid_tool_calls else (assistant_content or None),
                        # DeepSeek API requires reasoning_content to always be present
                        # in assistant messages when thinking mode is enabled in the
                        # conversation. Use empty string when no reasoning was generated.
                        "reasoning_content": reasoning_content or "",
                    }

                    if valid_tool_calls:
                        # 上线前参数归一化（2026-08-21 dflash2 事故）：模型
                        # 可能发出截断 JSON 参数的 tool_call——执行侧早已
                        # 修复，但 assistant 消息里若保留截断串，vLLM 服务端
                        # 解析 tool_calls.arguments 会 400
                        # （"Expecting value: line 1 column 10"）→ 空答案
                        # 重试再 400 → Agent loop error。所有 outgoing
                        # tool_calls 参数必须是合法 JSON（修复或 {}）。
                        for _tc in valid_tool_calls:
                            _raw_args = _tc.get("function", {}).get("arguments")
                            if not isinstance(_raw_args, str):
                                _tc.setdefault("function", {})["arguments"] = "{}"
                                continue
                            try:
                                json.loads(_raw_args)
                            except json.JSONDecodeError:
                                _tc["function"]["arguments"] = repair_tool_call_arguments(
                                    _raw_args, _tc.get("function", {}).get("name", "?")
                                )
                        assistant_msg["tool_calls"] = valid_tool_calls

                        can_parallel = (
                            config.agent_tool_loop_parallel_tool_calls
                            and len(valid_tool_calls) > 1
                            and all(
                                tc["function"]["name"] in _PARALLEL_SAFE_TOOLS
                                for tc in valid_tool_calls
                            )
                        )

                        if can_parallel:
                            _parallel_cancelled = False
                            if state.cancelled:
                                for tc in valid_tool_calls:
                                    tool_name = tc["function"]["name"]
                                    interrupted_result = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments={},
                                        result=json.dumps({"error": "Agent cancelled", "interrupted": True}, ensure_ascii=False),
                                        error=True,
                                    )
                                    state.tool_results.append(interrupted_result)
                                    state.messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "name": tool_name,
                                        "content": interrupted_result.result,
                                    })
                                _parallel_cancelled = True

                            if not _parallel_cancelled:
                                coros = []
                                for tc in valid_tool_calls:
                                    tool_name = tc["function"]["name"]
                                    try:
                                        tool_args = json.loads(tc["function"]["arguments"])
                                    except json.JSONDecodeError:
                                        repaired = repair_tool_call_arguments(tc["function"]["arguments"], tool_name)
                                        try:
                                            tool_args = json.loads(repaired)
                                        except json.JSONDecodeError:
                                            tool_args = {}

                                    yield {
                                        "tool_call": {
                                            "call_id": tc["id"],
                                            "name": tool_name,
                                            "arguments": tool_args,
                                        }
                                    }
                                    _turn_text, _turn_tr = self._current_turn_context(state)
                                    coros.append((tc, tool_args, asyncio.wait_for(
                                        self._execute_single_tool(
                                            tc["id"], tool_name, tool_args, session_factory, user, conversation, assistant,
                                            state,
                                            current_turn_content=_turn_text,
                                            current_turn_tool_results=_turn_tr,
                                        ),
                                        timeout=self._tool_call_timeout,
                                    )))

                                par_task = asyncio.gather(*(c[2] for c in coros), return_exceptions=True)
                                try:
                                    while not par_task.done():
                                        done, _ = await asyncio.wait({par_task}, timeout=8.0)
                                        if not done:
                                            yield {"ping": True}
                                    raw_results = par_task.result()
                                    results = []
                                    for (tc, tool_args, _), r in zip(coros, raw_results):
                                        if isinstance(r, asyncio.TimeoutError):
                                            results.append(ToolCallResult(
                                                call_id=tc["id"],
                                                name=tc["function"]["name"],
                                                arguments=tool_args,
                                                result=json.dumps({"error": f"Tool '{tc['function']['name']}' timed out"}),
                                                error=True,
                                            ))
                                        elif isinstance(r, Exception):
                                            results.append(ToolCallResult(
                                                call_id=tc["id"],
                                                name=tc["function"]["name"],
                                                arguments=tool_args,
                                                result=json.dumps({"error": f"Tool '{tc['function']['name']}' failed: {str(r)}"}),
                                                error=True,
                                            ))
                                        else:
                                            results.append(r)
                                except asyncio.CancelledError:
                                    par_task.cancel()
                                    try:
                                        await par_task
                                    except asyncio.CancelledError:
                                        pass
                                    raise

                                # Citation ledger: renumber web_search results
                                # with turn-global ids BEFORE the digest layer
                                # so the numbering the model sees (and the
                                # digest envelope embeds) is unambiguous.
                                self._apply_citation_ledger(state, results)

                                # Fix JSON arguments for parallel path (same as serial)
                                for i, (tc, tool_args, _) in enumerate(coros):
                                    if isinstance(tool_args, str):
                                        try:
                                            json.loads(tool_args)
                                        except json.JSONDecodeError:
                                            repaired = repair_tool_call_arguments(tool_args, tc["function"]["name"])
                                            try:
                                                coros[i] = (tc, json.loads(repaired), coros[i][2])
                                            except json.JSONDecodeError:
                                                coros[i] = (tc, {}, coros[i][2])

                                _digest_out = []
                                async for _digest_ev in self._digest_with_ping(results):
                                    if "_digest_done" in _digest_ev:
                                        _digest_out = _digest_ev["_digest_done"]
                                    else:
                                        yield _digest_ev
                                results = _digest_out
                                tool_messages = []
                                for (tc, tool_args, _), result in zip(coros, results):
                                    state.tool_results.append(result)
                                    tool_messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "name": tc["function"]["name"],
                                        "content": result.result,
                                    })
                                    state.total_tool_calls += 1
                                    # Count web_search calls (mirror the serial
                                    # path below). Without this the search_required
                                    # auto-invoke gate re-fires after a PARALLEL
                                    # search round (web_search_count stays 0) and
                                    # synthesizes a redundant call_user_search_*
                                    # third search (conv a3cfb421 2026-08-09:
                                    # msg[5]/[7]/[9] each ran 2 parallel model
                                    # searches + 1 guardrail-synthesized round).
                                    if tc["function"]["name"] == "web_search":
                                        state.web_search_count += 1
                                    yield {
                                        "tool_result": {
                                            "call_id": tc["id"],
                                            "name": tc["function"]["name"],
                                            "result": result.result,
                                            "error": result.error,
                                        }
                                    }
                                    self._mark_activity(state)

                                tool_messages = await enforce_turn_budget(
                                    tool_messages, self._budget_config, self.workspace_path
                                )
                                ordered_tool_messages = []
                                for (tc, _, _), _ in zip(coros, results):
                                    tc_id = tc["id"]
                                    matched = None
                                    for tm in tool_messages:
                                        if tm.get("tool_call_id") == tc_id:
                                            matched = tm
                                            break
                                    if matched:
                                        ordered_tool_messages.append(matched)
                                    else:
                                        ordered_tool_messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_id,
                                            "name": tc["function"]["name"],
                                            "content": "[tool result missing]",
                                        })
                                state.messages.append(assistant_msg)
                                state.messages.extend(ordered_tool_messages)
                        else:
                            tool_results_map: Dict[str, ToolCallResult] = {}
                            tool_messages = []
                            for tc in valid_tool_calls:
                                tool_name = tc["function"]["name"]

                                if state.cancelled:
                                    interrupted_result = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments={},
                                        result=json.dumps({"error": "Agent cancelled", "interrupted": True}, ensure_ascii=False),
                                        error=True,
                                    )
                                    tool_results_map[tc["id"]] = interrupted_result
                                    tool_messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "name": tool_name,
                                        "content": interrupted_result.result,
                                    })
                                    continue

                                try:
                                    tool_args = json.loads(tc["function"]["arguments"])
                                except json.JSONDecodeError:
                                    repaired = repair_tool_call_arguments(tc["function"]["arguments"], tool_name)
                                    try:
                                        tool_args = json.loads(repaired)
                                    except json.JSONDecodeError:
                                        tool_args = {}

                                yield {
                                    "tool_call": {
                                        "call_id": tc["id"],
                                        "name": tool_name,
                                        "arguments": tool_args,
                                    }
                                }

                                _turn_text, _turn_tr = self._current_turn_context(state)
                                tool_task = asyncio.ensure_future(
                                    asyncio.wait_for(
                                        self._execute_single_tool(
                                            tc["id"], tool_name, tool_args, session_factory, user, conversation, assistant,
                                            state,
                                            current_turn_content=_turn_text,
                                            current_turn_tool_results=_turn_tr,
                                        ),
                                        timeout=self._tool_call_timeout,
                                    )
                                )
                                try:
                                    while not tool_task.done():
                                        done, _ = await asyncio.wait({tool_task}, timeout=8.0)
                                        if not done:
                                            yield {"ping": True}
                                    result = tool_task.result()
                                except asyncio.TimeoutError:
                                    result = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments=tool_args,
                                        result=json.dumps({"error": f"Tool '{tool_name}' timed out"}),
                                        error=True,
                                    )
                                except asyncio.CancelledError:
                                    tool_task.cancel()
                                    try:
                                        await tool_task
                                    except asyncio.CancelledError:
                                        pass
                                    raise

                                if result.error and await self._is_recoverable_tool_error(result):
                                    retry_result = await self._retry_tool_with_recovery(
                                        tc["id"], tool_name, tool_args,
                                        session_factory, user, conversation, assistant, state,
                                        failed_result=result,
                                    )
                                    if retry_result:
                                        result = retry_result

                                tool_results_map[tc["id"]] = result

                            # Citation ledger: renumber web_search results
                            # with turn-global ids BEFORE the digest layer
                            # (same as the parallel path above).
                            self._apply_citation_ledger(state, list(tool_results_map.values()))

                            _digest_out = []
                            async for _digest_ev in self._digest_with_ping(list(tool_results_map.values())):
                                if "_digest_done" in _digest_ev:
                                    _digest_out = _digest_ev["_digest_done"]
                                else:
                                    yield _digest_ev
                            results_digested = _digest_out
                            tool_results_map = {
                                r.call_id: r for r in results_digested
                            }
                            tool_messages = [
                                {
                                    "role": "tool",
                                    "tool_call_id": r.call_id,
                                    "name": r.name,
                                    "content": r.result,
                                }
                                for r in results_digested
                            ]

                            tool_messages = await enforce_turn_budget(
                                tool_messages, self._budget_config, self.workspace_path
                            )
                            ordered_tool_messages = []
                            for tc in valid_tool_calls:
                                tc_id = tc["id"]
                                matched = None
                                for tm in tool_messages:
                                    if tm.get("tool_call_id") == tc_id:
                                        matched = tm
                                        break
                                if matched:
                                    ordered_tool_messages.append(matched)
                                else:
                                    ordered_tool_messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc_id,
                                        "name": tc["function"]["name"],
                                        "content": "[tool result missing]",
                                    })
                            state.messages.append(assistant_msg)
                            state.messages.extend(ordered_tool_messages)
                            for tc in valid_tool_calls:
                                result = tool_results_map[tc["id"]]
                                state.tool_results.append(result)
                                state.total_tool_calls += 1
                                # Count web_search calls
                                if tc["function"]["name"] == "web_search":
                                    state.web_search_count += 1
                                yield {
                                    "tool_result": {
                                        "call_id": tc["id"],
                                        "name": tc["function"]["name"],
                                        "result": result.result,
                                        "error": result.error,
                                    }
                                }
                                self._mark_activity(state)

                        # Post-tool guardrail — sync, pure-function. Short-circuits
                        # the loop after specific success conditions (currently:
                        # schedule(action='create') success). Lets the LLM stop
                        # mid-turn without another costly round-trip.
                        from app.services.tool_guardrails import evaluate_post_tool as _guard
                        _halted = False
                        for _r in state.tool_results[-len(valid_tool_calls):]:
                            _decision = _guard(_r.name, _r.result)
                            if _decision.action == "halt":
                                _halted = True
                                _halt_message = _decision.final_message or ""
                                logger.info(
                                    "AgentLoop halted by tool_guardrails after tool=%s",
                                    _r.name,
                                )
                                break
                        if _halted:
                            if _halt_message:
                                yield {"content": _halt_message}
                                state.messages.append(
                                    {"role": "assistant", "content": _halt_message}
                                )
                            state.finish_reason = "stop"
                            state.completed_normally = True
                            break

                        # Check if web_search limit reached (skip in deathmatch mode)
                        if state.web_search_count >= state.max_web_searches and not self._skip_guardrails():
                            logger.warning(
                                "Web search limit reached (%d/%d), forcing final answer",
                                state.web_search_count, state.max_web_searches,
                            )
                            yield {
                                "agent_step": {
                                    "name": "search_limit_guard",
                                    "title": "搜索次数限制",
                                    "content": f"已达到最大搜索次数 ({state.web_search_count} 次)，正在基于已有结果生成最终回答。",
                                    "step_type": "tool",
                                }
                            }
                            state.force_final_answer = True
                            state.consecutive_tool_iterations = 0
                            if assistant_content:
                                state.messages.append({"role": "assistant", "content": assistant_content})
                            else:
                                state.messages.append({"role": "assistant", "content": "[搜索完成...]"})
                            _inject_directive(
                                state,
                                "已达到最大搜索次数，请基于已收集的信息直接给出完整回答。"
                                "如果任务需要生成文件，请使用 execute_code 工具生成。"
                                "注意Markdown格式。",
                            )
                            # Tool-round boundary: a search round ended (its
                            # tool_call event may have been lost to a slow
                            # client's queue overflow) — the relay must drop
                            # any held pre-tool prose before the answer
                            # iteration streams (A4.9 M1 symmetry).
                            yield {
                                "iteration_done": {
                                    "iteration": state.iterations,
                                    "tool_calls": 1,
                                }
                            }
                            continue

                        if has_budget_refund_tool:
                            await state.budget.refund()
                            logger.debug("Refunded iteration budget for execute_code tool call")

                        yield {
                            "iteration": {
                                "current": state.iterations,
                                "max": self.max_iterations,
                                "tool_calls": state.total_tool_calls,
                            }
                        }

                        # Deathmatch: evaluate judge after tool-calling iterations
                        # so the turns counter increments and goal progress is tracked.
                        if self._skip_guardrails() and state.total_tool_calls > 0:
                            dm = self.deathmatch_manager
                            logger.info(
                                "Deathmatch mid-loop judge: iterations=%d, tool_calls=%d, turns=%d",
                                state.iterations, state.total_tool_calls, dm._conv.deathmatch_turns,
                            )
                            # Build a summary of what tools were used
                            _tool_summary_parts = []
                            for _tr in state.tool_results[-len(valid_tool_calls):]:
                                _tool_summary_parts.append(f"[{_tr.name}] {_tr.result[:200]}")
                            _tool_summary = "\n".join(_tool_summary_parts) if _tool_summary_parts else "(tool calls in progress)"
                            _eval_content = assistant_content.strip() if assistant_content else _tool_summary

                            decision = await self._safe_judge(
                                dm, _eval_content,
                                user_initiated=False,
                                workspace_path=self.workspace_path or "",
                                state=state,
                            )
                            _dm_should_continue = decision.get("should_continue", False)
                            async for _ev in self._emit_deathmatch_verdict(dm, decision):
                                yield _ev
                            self._mark_activity(state)
                            if decision.get("verdict") == "done":
                                async for _ev in self._handle_deathmatch_done(dm, decision, state):
                                    yield _ev
                                return
                            if not _dm_should_continue:
                                yield {"done": True}
                                return
                    # Iteration boundary signal: chat.py's relay uses it to
                    # disarm the pre-tool text gate deterministically. When
                    # the tool_call event itself was dropped (slow client
                    # subscriber-queue overflow), this event is the only way
                    # the relay learns a tool round ended. `tool_calls` is
                    # THIS iteration's count: 0 → the held text is the answer
                    # and must stream; >0 → the held text is stale pre-tool
                    # prose and must be dropped.
                    yield {
                        "iteration_done": {
                            "iteration": state.iterations,
                            "tool_calls": len(tool_calls_collected),
                        }
                    }
                    continue

            state.consecutive_tool_iterations = 0
            if assistant_content:
                _last_user_msg = ""
                for msg in reversed(state.messages):
                    if msg.get("role") == "user":
                        _last_user_msg = msg.get("content", "")
                        break
                # Deathmatch mode: skip the tool_nudge logic since the
                # deathmatch loop handles continuation itself.
                if not self._skip_guardrails():
                    # Audit window history (conv 86e51bbd follow-up 2026-08-10;
                    # conv 41d2790d 2026-08-10; widened 2026-08-12): originally
                    # an ITERATION cap (audit only iterations 1-2), so the final
                    # draft of a 3-iteration turn shipped UN-AUDITED (the hollow
                    # "上方 Mermaid 图…" summary). Then a rejection budget keyed
                    # on web_search_count == 0 — which let search turns ship
                    # un-audited in live-thinking mode (blind spot closed
                    # 2026-08-12: every final draft is now audited).
                    # warning. A draft can therefore never ship without having
                    # been audited at least once.
                    # Audit window (2026-08-12 blind-spot wave): EVERY final
                    # draft is audited — no exemptions for search turns or
                    # attachment turns. The old gate (web_search_count == 0)
                    # let any turn that called web_search once ship
                    # un-audited in live-thinking mode (the synthesis-path
                    # audit is unreachable there) — "has tool results" does
                    # NOT imply "answers the question" (conv 97ff355d did 25
                    # real tool calls and still drafted nonsense). The
                    # [file-ref:] exemption is gone too; the auditor context
                    # now carries an attachment-visibility note instead so it
                    # doesn't false-accuse grounded drafts of fabrication.
                    # Creative turns (coordinator-judged) and deathmatch mode
                    # (own judge) remain exempt.
                    _audit_reject_budget = _audit_reject_budget_for(state)
                    if not state.creative_turn:
                        # Length-continuation tail (2026-08-12 blind-spot wave):
                        # no longer SKIPS the audit — the auditor judges the
                        # STITCHED whole answer (all no-tool segments of this
                        # attempt), not the isolated tail fragment (auditing
                        # the fragment alone was why the skip existed).
                        _stitched_audit = state.skip_audit_once
                        state.skip_audit_once = False
                        # Agentic pre-send audit: an LLM judges whether the
                        # draft actually answers the LATEST user message
                        # (on-topic, non-repetitive, adequately grounded).
                        # Only a failed audit triggers another iteration —
                        # never a mechanical "no tools were used" heuristic.
                        # The old deterministic regex gate
                        # (detect_fabricated_search_claim, conv 149ce886)
                        # was removed per user principle (2026-07-20: agentic
                        # over regex; conv 86e51bbd 2026-08-10: 自评草稿
                        # "我是'能动手'的：搜索、浏览…" 被正则误判为检索声称 →
                        # 强制合成检索 + 整段原文 query + 第二轮回答). The
                        # auditor LLM decides — its template distinguishes
                        # "用户要求检索却未调用工具"（不合格）from "历史已有
                        # 信息凭记忆回答"（合格），and its context now states
                        # the ACTUAL tool-call record so a claim about
                        # web_search/browser use is judged against the truth.
                        # LLM auditor is budget-bounded (audit_rejections <=
                        # budget); when the budget is spent a bounded salvage
                        # regeneration replaces the rejected draft (never
                        # shipped fail-open, conv 97ff355d). The
                        # claim-vs-content check (rule 6) is
                        # the auditor LLM's job — no deterministic detector
                        # (user principle 2026-07-20: 语义判断留给 LLM).
                        guidance = None
                        _soft_limit = config.agent_audit_soft_reject_limit
                        if (
                            state.audit_rejections <= _audit_reject_budget
                            and state.audit_soft_rejections <= _soft_limit
                        ):
                            if _stitched_audit and state.turn_content_segments:
                                _audit_target = "\n\n".join(state.turn_content_segments)
                            else:
                                _audit_target = assistant_content
                            # 差异闸门（用户要求 2026-08-21）：修正轮仅修改
                            # 点名部分。上一稿是 reject 打回时，比对修正稿与
                            # 被拒稿的相似度——整篇重写不送审计，静默注入
                            # 重改指令一次（一次性标记防循环；短稿跳过；
                            # config revision_min_similarity=0 关闭）。
                            _prev_stash = state.audit_rejected_drafts[-1] if state.audit_rejected_drafts else None
                            if (
                                not state.revision_rewrite_flagged
                                and _prev_stash is not None
                                and (_prev_stash.get("verdict") == "reject")
                                and _is_full_rewrite(str(_prev_stash.get("content") or ""), _audit_target)
                            ):
                                state.revision_rewrite_flagged = True
                                logger.warning(
                                    "Revision similarity gate: full-rewrite detected (threshold %.2f) — "
                                    "re-injecting copy-edit directive instead of auditing",
                                    config.agent_audit_revision_min_similarity,
                                )
                                yield {"audit_reset": True}
                                _prune_guardrail_pairs(state)
                                _rejected_append(self, state, assistant_content, reasoning_content)
                                _inject_directive(
                                    state,
                                    "【改动过大】你刚才的修正稿把原稿整篇重写了。"
                                    "请以上一条 assistant 消息中的被拒草稿为底稿，"
                                    "仅修改审计点名的部分，其余内容逐字保留，"
                                    "重新输出完整回答。",
                                    _ephemeral="response_audit",
                                )
                                continue
                            guidance = await self._audit_response(state, _audit_target, _last_user_msg)
                        if guidance is None and assistant_content.strip() and self._live_thinking_enabled():
                            # 审计 accept（guidance None = 通过/软放行）——标记
                            # 本轮最终稿可信，canary trip 不得重答。仅 live 模式
                            # 草稿即最终稿；非 live 模式的最终稿是合成 pass 产出
                            # （其 accept 在合成审计点置位），草稿 accept 是中间态。
                            state.audit_accepted = True
                        if guidance:
                            # 2026-08-14 (conv a67faa04): ONLY verdict==reject
                            # consumes the rejection budget; unverifiable /
                            # needs_evidence ("can't see" is not fabrication)
                            # consume the separate soft counter so truncated
                            # evidence cannot burn the budget into the
                            # failure-text path.
                            if guidance.verdict == "reject":
                                state.audit_rejections += 1
                            else:
                                state.audit_soft_rejections += 1
                            if not config.agent_audit_revision_thinking_enabled:
                                state.revision_thinking_off = True
                            # Best-of stash (conv 7dc7a0d5): record BEFORE the
                            # budget branch — the budget-spending draft is a
                            # selection candidate too.
                            _stash_rejected_draft(state, _audit_target, guidance, source="draft", reasoning=reasoning_content)
                            _budget_spent = (
                                state.audit_rejections > _audit_reject_budget
                                or state.audit_soft_rejections > _soft_limit
                            )
                            if _budget_spent:
                                # Budget spent: NEVER ship the just-rejected
                                # draft (conv 97ff355d 2026-08-12: 5 correct
                                # rejections — the model hallucinated that the
                                # user message was a replay of its own previous
                                # answer — then the 答非所问 5th draft shipped
                                # fail-open and the user got an answer with
                                # neither head nor tail). Run ONE bounded
                                # salvage regeneration from a de-poisoned
                                # context; if that also fails, an honest
                                # failure notice ships instead of the draft.
                                logger.warning(
                                    "Response auditor budget spent (reject=%d soft=%d) — salvaging from cleaned context instead of shipping the rejected draft",
                                    state.audit_rejections,
                                    state.audit_soft_rejections,
                                )
                                _salvage_parts: List[str] = []
                                async for _salvage_ev in self._salvage_after_audit_budget(state, _last_user_msg):
                                    if "content" in _salvage_ev:
                                        _salvage_parts.append(_salvage_ev["content"])
                                    yield _salvage_ev
                                assistant_content = _strip_leading_orphan_punct(
                                    "".join(_salvage_parts).strip()
                                ) or _AUDIT_SALVAGE_FAILURE_TEXT
                                state.salvaged_final = True
                            else:
                                # A4: silent QC — internal reset event
                                # (relay wipes the streamed draft; nothing
                                # rendered to the user).
                                yield {"audit_reset": True}
                                _prune_guardrail_pairs(state)
                                _rejected_append(self, state, assistant_content, reasoning_content)
                                # The rejected draft is dead: drop it from the
                                # turn's content segments too (mirrors the
                                # relay's accumulator reset) so the stitched
                                # continuation audit and _current_turn_context
                                # never see superseded text.
                                state.turn_content_segments.clear()
                                _inject_directive(state, guidance.guidance, _ephemeral="response_audit")
                                continue
                # No-tool iteration boundary (see the tool branch above): the
                # model answered without calling a tool — the relay must
                # release any held text and stream the answer live instead of
                # holding it until `done` (whole-answer pop, 2026-08-06 user
                # report). Placed AFTER the deterministic fabrication guard so
                # a rejected fake-claim draft is never flushed to the client
                # (the gate's original purpose, A4.9 I1); both reject paths
                # above `continue` past this yield.
                yield {
                    "iteration_done": {
                        "iteration": state.iterations,
                        "tool_calls": 0,
                    }
                }
                if state.total_tool_calls >= config.agent_skill_evolution_auto_suggest_threshold and config.agent_skill_evolution_enabled:
                    yield {
                        "agent_step": {
                            "name": "skill_evolution_suggestion",
                            "title": "技能建议",
                            "content": f"本次任务使用了 {state.total_tool_calls} 次工具调用。如果这是一个常见任务模式,可以考虑保存为技能以便复用。调用 skill_manage(action='create') 即可创建。",
                            "step_type": "suggestion",
                        }
                    }
                # Final thinking phase: when reasoning is enabled, always run a
                # dedicated deep-thinking synthesis pass before yielding the final
                # answer — even when no tools were called. Previously this was
                # gated on `total_tool_calls > 0`, which silently bypassed the
                # thinking toggle for pure-chat turns and confused users.
                # LIVE-THINKING MODE (agent.tool_loop.live_thinking, default on):
                # iterations already streamed thinking+content live, so the
                # draft IS the final answer — the regeneration pass only runs as
                # a fallback when the loop produced no visible content.
                # PHASE 1A: never write reasoning_content back to state.messages.
                # Reasoning must not round-trip — see llm_service._build_params.
                _final_content = assistant_content
                if self.enable_reasoning:
                    # EMPTY-ANSWER RETRY consistency (A4.9 I2): a retried
                    # iteration already streamed its content as the final
                    # answer (retry_thinking_off yield bypasses the
                    # suppression). When content IS present after a retry,
                    # skip the synthesis pass — otherwise the retried content
                    # AND the _final_thinking regeneration would BOTH be
                    # persisted (double answer) when live-thinking is off
                    # (config live_thinking=false or dedicated subtask
                    # iteration_llm). Empty-after-retry still synthesizes
                    # (the recovery path remains).
                    _need_synthesis = (
                        not self._live_thinking_enabled()
                        or not assistant_content.strip()
                    ) and not (
                        (state.retry_thinking_off
                         or (state.revision_thinking_off and self._live_thinking_enabled()))
                        and assistant_content.strip()
                    ) and not state.salvaged_final
                    if _need_synthesis:
                        if assistant_content:
                            state.messages.append({
                                "role": "assistant",
                                "content": assistant_content,
                            })
                        async for event in self._final_thinking(state, draft_content=assistant_content):
                            if "content" in event:
                                _final_content = _strip_leading_orphan_punct(event["content"])
                            yield event
                        # AGENTIC AUDIT of the synthesized final content
                        # (A4.9 review I1, conv 41d2790d follow-up): the
                        # synthesis pass is a FRESH LLM generation that the
                        # draft-path audit above never saw — a
                        # claim-without-content summary can slip through here.
                        # Re-run the SAME auditor LLM (rule 6 judges
                        # claim-vs-content) on the synthesized text — no
                        # deterministic detector (user principle: 语义判断
                        # 留给 LLM). Bounded by the rejection budget; on
                        # budget exhaustion a bounded salvage regeneration
                        # replaces the rejected text (never ship it fail-open,
                        # conv 97ff355d), never an infinite loop.
                        if (
                            not self._skip_guardrails()
                            and not state.creative_turn
                            and state.audit_rejections <= _audit_reject_budget_for(state)
                            and state.audit_soft_rejections <= config.agent_audit_soft_reject_limit
                            and _final_content and _final_content.strip()
                        ):
                            _synth_guidance = await self._audit_response(state, _final_content, _last_user_msg)
                            if _synth_guidance is None and _final_content.strip():
                                state.audit_accepted = True
                            if _synth_guidance:
                                if _synth_guidance.verdict == "reject":
                                    state.audit_rejections += 1
                                else:
                                    state.audit_soft_rejections += 1
                                # Best-of stash (conv 7dc7a0d5): synthesis rejects
                                # are selection candidates too (both branches).
                                _stash_rejected_draft(state, _final_content, _synth_guidance, source="synthesis", reasoning=reasoning_content)
                                if (
                                    state.audit_rejections <= _audit_reject_budget_for(state)
                                    and state.audit_soft_rejections <= config.agent_audit_soft_reject_limit
                                ):
                                    logger.warning(
                                        "Synthesized final rejected by auditor: verdict=%s %s",
                                        _synth_guidance.verdict,
                                        _synth_guidance.guidance[:120],
                                    )
                                    # A4: silent QC — internal reset event.
                                    yield {"audit_reset": True}
                                    _prune_guardrail_pairs(state)
                                    _rejected_append(self, state, _final_content or "", reasoning_content)
                                    # A4.9 M5: mirror the draft-reject branch —
                                    # the rejected synthesis must not linger in
                                    # the turn segments (_current_turn_context /
                                    # stitched audit would see superseded text).
                                    state.turn_content_segments.clear()
                                    _inject_directive(state, _synth_guidance.guidance, _ephemeral="response_audit")
                                    continue
                                logger.warning(
                                    "Synthesized final rejected after budget (reject=%d soft=%d) — salvaging from cleaned context",
                                    state.audit_rejections,
                                    state.audit_soft_rejections,
                                )
                                _salvage_parts = []
                                async for _salvage_ev in self._salvage_after_audit_budget(state, _last_user_msg):
                                    if "content" in _salvage_ev:
                                        _salvage_parts.append(_salvage_ev["content"])
                                    yield _salvage_ev
                                _final_content = _strip_leading_orphan_punct(
                                    "".join(_salvage_parts).strip()
                                ) or _AUDIT_SALVAGE_FAILURE_TEXT
                                state.salvaged_final = True

                # 遵循词 (canary) check — SOTA-corrected context-rot detector.
                # The model must echo the per-conversation marker at the end of
                # every final reply. Two CONSECUTIVE misses (across turns, one
                # final answer per turn) = the early-context instruction stopped
                # landing: compress once and re-answer. Never restart the loop —
                # the legacy deathmatch HTML-comment marker caused infinite
                # restarts because DeepSeek does not echo HTML comments (see
                # canary_marker.py header; fix_deathmatch_loop_stuck_at_turn0).
                _canary_retry = False
                if self._canary_enabled and conversation is not None and not state.canary_compressed:
                    try:
                        from app.services.canary_marker import (
                            response_has_canary, canary_tracker,
                        )
                        _conv_key = str(getattr(conversation, "id", "") or "")
                        _marker = self._canary_marker
                        if _marker:
                            # The main-stream content (`assistant_content`) was
                            # already stripped by _strip_dsml_all above, so the
                            # check MUST read the raw stream text. With
                            # reasoning enabled, the thinking pass regenerates
                            # the final content (marker intact there) — a hit
                            # on EITHER pass counts.
                            _hit = response_has_canary(_final_content, _marker) or (
                                assistant_raw_content and response_has_canary(assistant_raw_content, _marker)
                            )
                            if _hit:
                                canary_tracker.record_hit(_conv_key)
                            else:
                                _misses = canary_tracker.record_miss(_conv_key)
                                _threshold = config.agent_canary_miss_threshold
                                _disabled = canary_tracker.auto_disabled(_conv_key)
                                if (_misses >= _threshold
                                        and canary_tracker.can_compress(_conv_key)
                                        and not _disabled):
                                    # A4.9 I2 (conv 97ff355d fix review): the
                                    # retry RE-ANSWERS the turn, so the salvage
                                    # flag must not suppress the synthesis pass
                                    # of that retry — otherwise non-live mode
                                    # ships an empty answer (the relay already
                                    # wiped its copy of the salvage).
                                    state.salvaged_final = False
                                    from app.services.context_compressor import estimate_request_tokens_rough
                                    _c_before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                    _ctx_len = config.agent_compression_context_length
                                    _min_ratio = config.agent_canary_compress_min_ratio
                                    _ratio = (_c_before / _ctx_len) if _ctx_len > 0 else 1.0
                                    if state.audit_accepted:
                                        # 2026-08-22（conv efaf8f9c 复盘）：最终稿
                                        # 已被审计接受——canary 漏标只是警告信号
                                        # （SOTA：连贯且通过质检的回答漏标=记录继续），
                                        # 重答会产生措辞不同的重复生成 + 重复工具轮
                                        # （iter7/8 重新浏览 GitHub）+ 回答后卡片。
                                        # 直接出货，不注入重答指令。
                                        logger.warning(
                                            "Canary trip (conv=%s): %d misses but draft was "
                                            "audit-accepted — recording miss, shipping as-is "
                                            "(no re-answer)",
                                            _conv_key, _misses,
                                        )
                                    elif _min_ratio > 0 and _ratio < _min_ratio:
                                        # A3 (2026-08-21, conv efaf8f9c): the
                                        # context is far from the window
                                        # (incident: ~9%) — a canary miss here
                                        # is NOT context rot. Compressing is
                                        # the wrong lever (it misfired twice
                                        # with zero hits). Re-assert the marker
                                        # directive and re-answer only; nothing
                                        # rendered to the user (A4).
                                        # A4.9 Important-1: the per-request
                                        # trip guard must cover BOTH branches
                                        # (the outer gate reads
                                        # state.canary_compressed) — otherwise
                                        # a model that keeps missing the marker
                                        # re-trips up to max_iterations.
                                        state.canary_compressed = True
                                        logger.warning(
                                            "Canary trip (conv=%s): %d consecutive misses but "
                                            "context at %.0f%% < %.0f%% ratio gate — "
                                            "re-asserting marker directive only",
                                            _conv_key, _misses, _ratio * 100, _min_ratio * 100,
                                        )
                                        yield {"audit_reset": True}
                                        _inject_directive(
                                            state,
                                            "内部完整性校验未通过：你刚才的回答末尾缺少约定的校验标记。"
                                            "请重新完整回答用户最近的一条消息，"
                                            "并记得在回答最后一行原样输出校验标记。",
                                        )
                                        _canary_retry = True
                                    else:
                                        # Per-request guard BEFORE the await: if
                                        # compression fails, the retry directive
                                        # below must not re-trigger within this
                                        # request (budget bounded re-generation).
                                        state.canary_compressed = True
                                        self._ensure_compressor()
                                        state.messages = await self._compressor.compress_async(state.messages)
                                        _c_after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                        canary_tracker.record_compression(_conv_key)
                                        logger.warning(
                                            "Canary trip (conv=%s): %d consecutive misses -> "
                                            "compressed %d->%d tokens, re-answering",
                                            _conv_key, _misses, _c_before, _c_after,
                                        )
                                        # A4: silent compression — the relay
                                        # resets its accumulators but renders
                                        # no step to the user (token badge
                                        # refresh stays).
                                        yield {"compression": {"before": _c_before, "after": _c_after, "silent": True}}
                                        _inject_directive(
                                            state,
                                            "内部完整性校验未通过：你刚才的回答末尾缺少约定的校验标记，"
                                            "说明上下文已经开始失效。系统已对历史上下文执行压缩。"
                                            "请基于压缩后的上下文，重新完整回答用户最近的一条消息，"
                                            "并记得在回答最后一行输出校验标记。",
                                        )
                                        _canary_retry = True
                                else:
                                    _log_miss = logger.debug if _disabled else logger.info
                                    _log_miss(
                                        "Canary miss (conv=%s): %d/%d misses, %s",
                                        _conv_key, _misses, _threshold,
                                        "auto-disabled" if _disabled else "no action",
                                    )
                    except Exception:
                        logger.exception("Canary check failed")
                # The marker is internal: strip it from everything the judge
                # and the persistence path see, AFTER the raw check above.
                if _final_content:
                    _final_content = _CANARY_RE.sub('', _final_content)
                if _canary_retry:
                    continue

                # Deathmatch verdict evaluation after final thinking
                if self._skip_guardrails():
                    # Ensure the assistant message is in state.messages
                    if _final_content and not any(
                        m.get("content") == _final_content and m.get("role") == "assistant"
                        for m in reversed(state.messages)
                    ):
                        state.messages.append({"role": "assistant", "content": _final_content})
                    dm = self.deathmatch_manager

                    # Repetition detection: track responses and detect loops
                    _is_repeating = self._detect_repetition(state, _final_content)
                    self._track_response(state, _final_content)
                    if _is_repeating:
                        state.repetition_count += 1
                        logger.warning(
                            "Deathmatch repetition detected (count=%d, turns=%d)",
                            state.repetition_count, dm._conv.deathmatch_turns,
                        )
                    else:
                        state.repetition_count = 0

                    logger.info("AgentLoop judge running: status=%s turns=%d",
                                dm._conv.deathmatch_status, dm._conv.deathmatch_turns)
                    decision = await self._safe_judge(
                        dm, _final_content,
                        user_initiated=False,
                        workspace_path=self.workspace_path or "",
                        state=state,
                    )
                    _dm_should_continue = decision.get("should_continue", False)
                    async for _ev in self._emit_deathmatch_verdict(dm, decision):
                        yield _ev
                    self._mark_activity(state)
                    if _dm_should_continue and decision.get("continuation_prompt"):
                        # Use repetition-detected prompt if agent is repeating itself
                        if state.repetition_count >= 2:
                            rep_prompt = dm.get_repetition_prompt()
                            if rep_prompt:
                                _inject_directive(state, rep_prompt)
                                continue
                        _inject_directive(state, decision["continuation_prompt"])
                        # Deathmatch escalation: after 3+ turns of only searching,
                        # force the agent to generate the output file.
                        _turns = dm._conv.deathmatch_turns or 0
                        if _turns >= 3:
                            _search_only = all(
                                tr.name == "web_search"
                                for tr in state.tool_results[-6:]
                            ) if len(state.tool_results) >= 3 else False
                            if _search_only:
                                logger.info(
                                    "Deathmatch escalation: turns=%d, forcing execute_code",
                                    _turns,
                                )
                                _inject_directive(
                                    state,
                                    "[强制指令] 你已经搜索了多轮但没有生成最终文件。"
                                    "现在必须停止搜索，立即使用 execute_code 工具生成'清单.xlsx'文件。"
                                    "基于你已收集到的所有信息，直接生成Excel文件。"
                                    "不要再调用 web_search。这是强制要求。",
                                )
                        continue
                    elif decision.get("needs_compression_retry") or decision.get("needs_restart"):
                        # Context marker was lost: compress history up to the last
                        # preserved checkpoint and retry the same turn with a fresh
                        # continuation prompt/marker. If too many consecutive misses
                        # triggered a deathmatch restart, reset turn counters too.
                        _restart = decision.get("needs_restart", False)
                        logger.info(
                            "Deathmatch %s: turns=%d messages=%d",
                            "restart" if _restart else "compression retry",
                            dm._conv.deathmatch_turns or 0, len(state.messages),
                        )
                        compressed = await dm.compress_messages(state.messages)
                        state.messages = compressed
                        # Re-inject continuation prompt with a fresh marker.
                        retry_prompt = dm.get_continuation_prompt(_final_content)
                        if retry_prompt:
                                _inject_directive(state, retry_prompt)
                        continue
                    elif decision.get("verdict") == "done":
                        async for _ev in self._handle_deathmatch_done(dm, decision, state):
                            yield _ev
                        return
                    else:
                        yield {"done": True}
                        return

                state.completed_normally = True
                break
            else:
                if self.deathmatch_manager and self.deathmatch_manager.is_active:
                    logger.info("AgentLoop recovery path (no tool calls): status=%s turns=%d",
                                self.deathmatch_manager._conv.deathmatch_status,
                                self.deathmatch_manager._conv.deathmatch_turns)
                    # Run the judge even when no tool calls were made so the
                    # turns counter increments and the DB status stays accurate.
                    dm = self.deathmatch_manager
                    _dm_content = assistant_content.strip() or "(no content produced)"

                    # Repetition detection
                    _is_repeating = self._detect_repetition(state, _dm_content)
                    self._track_response(state, _dm_content)
                    if _is_repeating:
                        state.repetition_count += 1
                        logger.warning(
                            "Deathmatch repetition detected in no-content path (count=%d, turns=%d)",
                            state.repetition_count, dm._conv.deathmatch_turns,
                        )
                    else:
                        state.repetition_count = 0

                    state.messages.append({"role": "assistant", "content": _dm_content})
                    decision = await self._safe_judge(
                        dm, _dm_content,
                        user_initiated=False,
                        workspace_path=self.workspace_path or "",
                        state=state,
                    )
                    _dm_should_continue = decision.get("should_continue", False)
                    async for _ev in self._emit_deathmatch_verdict(dm, decision):
                        yield _ev
                    self._mark_activity(state)
                    if _dm_should_continue and decision.get("continuation_prompt"):
                        # Use repetition-detected prompt if agent is repeating itself
                        if state.repetition_count >= 2:
                            rep_prompt = dm.get_repetition_prompt()
                            if rep_prompt:
                                _inject_directive(state, rep_prompt)
                                if (await state.budget.get_remaining()) <= 0:
                                    await state.budget.refund()
                                continue
                        _inject_directive(state, decision["continuation_prompt"])
                        if (await state.budget.get_remaining()) <= 0:
                            await state.budget.refund()
                        continue
                    elif decision.get("needs_compression_retry") or decision.get("needs_restart"):
                        _restart = decision.get("needs_restart", False)
                        logger.info(
                            "Deathmatch %s (no-content): turns=%d messages=%d",
                            "restart" if _restart else "compression retry",
                            dm._conv.deathmatch_turns or 0, len(state.messages),
                        )
                        compressed = await dm.compress_messages(state.messages)
                        state.messages = compressed
                        retry_prompt = dm.get_continuation_prompt(_dm_content)
                        if retry_prompt:
                                _inject_directive(state, retry_prompt)
                        if (await state.budget.get_remaining()) <= 0:
                            await state.budget.refund()
                        continue
                    elif decision.get("verdict") == "done":
                        async for _ev in self._handle_deathmatch_done(dm, decision, state):
                            yield _ev
                        return
                    elif not _dm_should_continue:
                        # human_gate / paused / partial_complete / inactive:
                        # the judge ordered a full stop — do NOT inject the
                        # generic "continue" nudge below, which would resurrect
                        # a loop the user was told had stopped for human review.
                        yield {"done": True}
                        return
                    # Fallthrough: inject generic nudge and continue
                    _inject_directive(
                        state,
                        "请继续推进目标。你的上一轮回复没有生成有效内容，请直接给出有实质内容的回复。如果任务需要搜索或执行操作，请调用工具。",
                    )
                    if (await state.budget.get_remaining()) <= 0:
                        await state.budget.refund()
                    continue
                # ── EMPTY-ANSWER RETRY (conv 517140ca, 2026-08-08) ──
                # The iteration LLM returned NO content, NO tool calls and NO
                # error — a silent empty response (reasoning budget burn on a
                # huge context is the known family, llm_empty_content). The
                # deathmatch path above retries via the judge; NORMAL mode
                # previously fell straight through to done-empty here with
                # ZERO retry, and chat.py then persisted
                # "回答生成失败：模型在工具调用后未能产出有效回答。" even
                # after the turn had done real work (conv 517140ca: 17 tool
                # steps wrote the bible + chapters 1-5, then the final
                # answer was empty). NEVER accept an empty final answer
                # without retrying: inject a directive and re-run the
                # iteration, bounded by max_empty_answer_retries.
                if state.empty_answer_retries < self.max_empty_answer_retries:
                    state.empty_answer_retries += 1
                    logger.warning(
                        "Empty final answer (iterations=%d, tool_calls=%d) — "
                        "auto-retry %d/%d",
                        state.iterations, state.total_tool_calls,
                        state.empty_answer_retries, self.max_empty_answer_retries,
                    )
                    yield {
                        "agent_step": {
                            "name": "empty_answer_retry",
                            "title": "回答重试",
                            "content": (
                                f"模型未产出有效回答，正在自动重试"
                                f"（{state.empty_answer_retries}/{self.max_empty_answer_retries}）…"
                            ),
                            "step_type": "recovery",
                        }
                    }
                    # Retry with thinking disabled — a silent-empty response
                    # usually means the reasoning pass burned the token
                    # budget on a huge context; the smaller no-think request
                    # recovers (mirrors llm_service empty-content retry).
                    # 2026-08-22: 先清空 relay 累加器——前序迭代（含 thinking
                    # 开的工具轮）已流出的正文若不清理，重试的新正文会与之
                    # 叠加显示（conv efaf8f9c 用户看到多份正式回答）。
                    yield {"audit_reset": True}
                    state.retry_thinking_off = True
                    _inject_directive(
                        state,
                        "你上一轮没有产出任何有效回答。请直接回答用户的问题；"
                        "如果任务尚未完成（文件、代码、正文等内容未生成完毕），"
                        "请继续调用工具完成它，然后给出最终回答。",
                    )
                    if (await state.budget.get_remaining()) <= 0:
                        await state.budget.refund()
                    continue
                yield {"done": True, "empty": True}
            return

        if state.completed_normally:
            # Normal break (final answer produced): NOT an exhaustion — the
            # legacy unconditional warning below made every healthy run look
            # like a budget failure in the logs (misleading during the conv
            # daa19eac truncation investigation).
            yield {"done": True}
            return
        logger.warning("Agent loop budget exhausted (iterations=%d, tool_calls=%d)",
                       state.iterations, state.total_tool_calls)
        # Deathmatch mode: budget exhaustion is not a stop condition.
        # Evaluate the goal, inject continuation if needed, and keep going.
        if self._skip_guardrails():
            dm = self.deathmatch_manager
            try:
                last_content = assistant_content
            except NameError:
                last_content = ""

            # Repetition detection for budget exhaustion path
            _is_repeating = self._detect_repetition(state, last_content)
            self._track_response(state, last_content)
            if _is_repeating:
                state.repetition_count += 1

            if last_content:
                state.messages.append({"role": "assistant", "content": last_content})
            decision = await self._safe_judge(
                dm, last_content,
                user_initiated=False,
                workspace_path=self.workspace_path or "",
                state=state,
            )
            async for _ev in self._emit_deathmatch_verdict(dm, decision):
                yield _ev
            self._mark_activity(state)
            if decision.get("should_continue") and decision.get("continuation_prompt"):
                # Use repetition-detected prompt if agent is repeating itself
                if state.repetition_count >= 2:
                    rep_prompt = dm.get_repetition_prompt()
                    if rep_prompt:
                        _inject_directive(state, rep_prompt)
                        if (await state.budget.get_remaining()) <= 0:
                            await state.budget.refund()
                        state.budget_grace_call = True
                        async for event in self._grace_call(state):
                            yield event
                        return
                _inject_directive(state, decision["continuation_prompt"])
                if (await state.budget.get_remaining()) <= 0:
                    await state.budget.refund()
                state.budget_grace_call = True
                async for event in self._grace_call(state):
                    yield event
                return
            elif decision.get("needs_compression_retry") or decision.get("needs_restart"):
                _restart = decision.get("needs_restart", False)
                logger.info(
                    "Deathmatch %s (budget): turns=%d messages=%d",
                    "restart" if _restart else "compression retry",
                    dm._conv.deathmatch_turns or 0, len(state.messages),
                )
                compressed = await dm.compress_messages(state.messages)
                state.messages = compressed
                retry_prompt = dm.get_continuation_prompt(last_content)
                if retry_prompt:
                    _inject_directive(state, retry_prompt)
                if (await state.budget.get_remaining()) <= 0:
                    await state.budget.refund()
                state.budget_grace_call = True
                async for event in self._grace_call(state):
                    yield event
                return
            elif decision.get("verdict") == "done":
                async for _ev in self._handle_deathmatch_done(dm, decision, state):
                    yield _ev
                return
            else:
                yield {"done": True}
                return
        # If the loop completed normally (produced a full answer without
        # exhausting the budget), do NOT run the grace call — it would
        # generate a duplicate response concatenated with the existing one.
        # (Handled by the early return above; kept as a defensive re-check.)
        if state.completed_normally:
            yield {"done": True}
            return
        # FIX: Always attempt grace call when budget is exhausted, even if tool_results is empty
        # This ensures we get some response rather than abrupt termination
        if state.messages:
            state.budget_grace_call = True
            async for event in self._grace_call(state):
                yield event
        else:
            yield {"done": True, "max_iterations_reached": True}

    async def _grace_call(self, state: AgentLoopState) -> AsyncIterator[dict]:
        logger.info("Grace call: making one final LLM call without tools to summarize")

        content_parts: list = []
        _grace_timeout = self._has_grace_timeout()

        async def _stream_one_call(prompt: str) -> None:
            nonlocal content_parts
            _started = asyncio.get_event_loop().time()
            finish_holder = {"finish": None}
            try:
                _grace_kwargs = self._sampling_kwargs(False)
                if not _grace_kwargs:
                    _grace_kwargs = {"temperature": config.default_temperature}
                llm_stream = self.llm.stream_chat_structured(
                    state.messages,
                    tools=None,
                    extra_body=build_thinking_extra_body(self.provider_type, False, thinking_budget=self.thinking_budget, preserve_thinking=self.preserve_thinking),
                    **_grace_kwargs,
                )
                _dsml_active = False
                _dsml_tail = ""
                async for event in llm_stream:
                    if _grace_timeout > 0 and (asyncio.get_event_loop().time() - _started) > _grace_timeout:
                        logger.warning("Grace call timed out after %.1fs", _grace_timeout)
                        break
                    event_type = event["type"]
                    event_data = event["data"]
                    if event_type == "content":
                        content_text = event_data
                        if '<think' in content_text:
                            content_text = _re.sub(r'<think[^>]*>.*?</think\s*>', '', content_text, flags=_re.DOTALL)
                            if '<think' in content_text:
                                content_text = _re.sub(r'<think[^>]*>.*', '', content_text, flags=_re.DOTALL)
                        content_text = _TOOL_CALL_RE.sub('', content_text)
                        content_text = _TOOL_INVOKE_RE.sub('', content_text)
                        content_text = _TOOL_CALL_XML_RE.sub('', content_text)
                        content_text = _TOOL_RESULT_RE.sub('', content_text)
                        content_text, _dsml_active, _dsml_tail = _strip_dsml_streaming(
                            content_text, _dsml_active, _dsml_tail
                        )
                        if content_text:
                            content_parts.append(content_text)
                            yield {"content": content_text}
                    elif event_type == "reasoning":
                        yield {"reasoning_content": event_data, "phase": "final"}
                    elif event_type == "done":
                        if isinstance(event_data, dict):
                            finish_holder["finish"] = event_data.get("finish_reason")
                        break
                    elif event_type == "error":
                        break
            except Exception as e:
                logger.warning("Grace call failed: %s", e)
            yield {"_finish": finish_holder["finish"]}

        _inject_directive(
            state,
            "你已达到最大迭代次数,请基于已收集的信息立即给出最终回答,不要再调用任何工具。注意Markdown格式：标题前留空行，#后加空格，表格行之间必须有换行。",
        )

        _grace_finish = None
        async for event in _stream_one_call(""):
            if isinstance(event, dict) and "_finish" in event:
                _grace_finish = event["_finish"]
            else:
                yield event

        # finish_reason=length: the summary itself was cut off at the token
        # ceiling — continue from the cut point (bounded) so even the
        # last-resort answer is complete, never a truncated fragment.
        _grace_continuations = 0
        _grace_partial_sent = None
        while _grace_finish == "length" and _grace_continuations < 2 and any(cp.strip() for cp in content_parts):
            _grace_continuations += 1
            logger.warning(
                "Grace call truncated (finish_reason=length) — continuation %d/2",
                _grace_continuations,
            )
            # Remove the previous partial pair so the context carries a single
            # up-to-date partial answer (same hygiene as _final_thinking).
            for _i in range(len(state.messages) - 1, -1, -1):
                _m = state.messages[_i]
                if _m.get("role") in ("system", "user") and _m.get("_ephemeral") == "length_continuation":
                    del state.messages[_i]
                elif (_grace_partial_sent and _m.get("role") == "assistant"
                        and _m.get("content") == _grace_partial_sent):
                    del state.messages[_i]
                    break
            _grace_partial_sent = "".join(content_parts)
            state.messages.append({"role": "assistant", "content": _grace_partial_sent})
            _inject_directive(
                state,
                "你的上一个回答因输出长度限制被截断。请从截断处无缝继续输出剩余内容："
                "不要重复任何已输出的文字，不要加过渡语，直接接着写。",
                _ephemeral="length_continuation",
            )
            async for event in _stream_one_call(""):
                if isinstance(event, dict) and "_finish" in event:
                    _grace_finish = event["_finish"]
                else:
                    yield event

        if not any(cp.strip() for cp in content_parts):
            logger.info("Grace call: no content on first attempt, retrying with fallback prompt")
            state.messages.pop()
            _inject_directive(
                state,
                "请用简洁的语言总结以上对话中你已完成的工作和发现的结果。直接给出回答，不要调用任何工具。",
            )
            async for event in _stream_one_call(""):
                if isinstance(event, dict) and "_finish" in event:
                    _grace_finish = event["_finish"]
                else:
                    yield event
            # The fallback prompt is also subject to the same completeness
            # guarantee: continue once more if it too was cut off.
            while _grace_finish == "length" and _grace_continuations < 2 and any(cp.strip() for cp in content_parts):
                _grace_continuations += 1
                logger.warning(
                    "Grace call fallback truncated (finish_reason=length) — continuation %d/2",
                    _grace_continuations,
                )
                for _i in range(len(state.messages) - 1, -1, -1):
                    _m = state.messages[_i]
                    if _m.get("role") in ("system", "user") and _m.get("_ephemeral") == "length_continuation":
                        del state.messages[_i]
                    elif (_grace_partial_sent and _m.get("role") == "assistant"
                            and _m.get("content") == _grace_partial_sent):
                        del state.messages[_i]
                        break
                _grace_partial_sent = "".join(content_parts)
                state.messages.append({"role": "assistant", "content": _grace_partial_sent})
                _inject_directive(
                    state,
                    "你的上一个回答因输出长度限制被截断。请从截断处无缝继续输出剩余内容："
                    "不要重复任何已输出的文字，不要加过渡语，直接接着写。",
                    _ephemeral="length_continuation",
                )
                async for event in _stream_one_call(""):
                    if isinstance(event, dict) and "_finish" in event:
                        _grace_finish = event["_finish"]
                    else:
                        yield event

        yield {"done": True, "max_iterations_reached": True}

    async def _final_thinking(self, state: AgentLoopState, draft_content: str = "") -> AsyncIterator[dict]:
        """Dedicated deep-thinking synthesis pass before yielding the final answer.

        Runs whenever ``enable_reasoning`` is True, regardless of whether any
        tools were called. Middle iterations suppress all content/reasoning when
        reasoning is enabled, so this phase produces the single, unified final
        answer with the user-visible reasoning stream.

        ``draft_content``: the (suppressed) draft the loop already produced this
        turn. Used ONLY as a last-resort fallback when every synthesis attempt
        fails — a previously audit-approved draft is a far better answer than a
        bare error bubble (conv daa19eac, 2026-08-03: synthesis failure left a
        51-char preamble persisted as the "answer")."""
        _grace_timeout = self._has_grace_timeout()
        logger.info(
            "Final thinking: making a dedicated reasoning pass (tool_calls=%d)",
            state.total_tool_calls,
        )
        # Resolve the question anchor for BOTH branches below. Every synthesis
        # prompt must restate the user's actual question: this prompt is the
        # LAST user message the model sees, so an unanchored "summarize /
        # think deeply" instruction overrides the coordinator focus and the
        # draft, and the model drifts to whatever thread it finds most
        # salient in the conversation (conv 3bc79c4c, 2026-07-20 — twice:
        # tool branch produced a 5727-char full recap; no-tool branch dropped
        # an audit-approved GPT-5.6-sol draft and wrote a business-model essay
        # resurrecting a 10-hour-old topic).
        _latest_q = state.turn_question
        if not _latest_q:
            for _m in reversed(state.messages):
                if _m.get("role") == "user" and not _m.get("_ephemeral"):
                    _latest_q = str(_m.get("content") or "").strip()
                    break
        if state.total_tool_calls > 0:
            synth_prompt = (
                "所有工具调用已完成。现在请回答用户本轮提出的这个问题：\n"
                f"「{_latest_q[:600]}」\n"
            )
            if state.turn_focus:
                synth_prompt += f"本轮意图聚焦：{state.turn_focus[:400]}\n"
            synth_prompt += (
                "要求：\n"
                "- 先直接回答这个问题本身，再按需补充支撑依据；\n"
                "- 工具结果只用于回答这个问题，与问题无关的信息不要写入回答；\n"
                "- 严禁把本场对话之前轮次的内容整理成总结或综述；"
                "之前轮次已给出的结论不要重复展开；\n"
                "- 篇幅与问题匹配：事实型问题简短直接，分析型问题才可展开。"
            )
        else:
            synth_prompt = (
                "请进行深度思考后，给出对用户这个问题的最终回答：\n"
                f"「{_latest_q[:600]}」\n"
            )
            if state.turn_focus:
                synth_prompt += f"本轮意图聚焦：{state.turn_focus[:400]}\n"
            synth_prompt += (
                "要求：\n"
                "- 上面的草稿已经回答了这个问题——以草稿的核心内容为基础，"
                "直接输出完善后的最终答复，不要描述你打算怎么回答；\n"
                "- 严禁另起炉灶切换话题，严禁复述本场对话之前轮次的内容；\n"
                "- 篇幅与问题匹配：事实型问题简短直接，分析型问题才可展开。"
            )
        _inject_directive(state, synth_prompt)

        # Failure-hardened synthesis (conv 149ce886, 2026-07-31): previously a
        # single attempt whose error event hit `break` and whose exception was
        # logged-and-swallowed — reasoning mode suppresses ALL other content,
        # so one failed synthesis = an empty, invisible turn saved to the DB.
        # Now: retry once (classified recovery: compress on context overflow,
        # thinking disabled on retry for a smaller/safer request), and if the
        # answer still never materializes, yield a terminal error event so the
        # caller persists a VISIBLE failure message instead of an empty bubble.
        _max_attempts = 2
        _max_continuations = 2
        _last_error = None
        for _attempt in range(_max_attempts):
            # Per-attempt deadline: the grace timeout governs ONE LLM call, so
            # a retry gets its own full window instead of instantly re-tripping
            # the previous attempt's expired deadline.
            _started = asyncio.get_event_loop().time()
            _produced = False
            _produced_parts: list = []
            _finish_reason = None
            _attempt_error = None
            _eb = build_thinking_extra_body(
                self.provider_type,
                (self.enable_reasoning and not state.revision_thinking_off) if _attempt == 0 else False,
                self.reasoning_effort if _attempt == 0 else None,
                thinking_budget=self.thinking_budget,
                preserve_thinking=self.preserve_thinking,
            )
            try:
                _final_kwargs = self._sampling_kwargs(self.enable_reasoning if _attempt == 0 else False)
                if not _final_kwargs:
                    _final_kwargs = {"temperature": config.default_temperature}
                llm_stream = self.llm.stream_chat_structured(
                    state.messages,
                    tools=None,
                    extra_body=_eb,
                    **_final_kwargs,
                )
                _dsml_active = False
                _dsml_tail = ""
                async for event in llm_stream:
                    if _grace_timeout > 0 and (asyncio.get_event_loop().time() - _started) > _grace_timeout:
                        logger.warning("Final thinking timed out after %.1fs", _grace_timeout)
                        yield {
                            "agent_step": {
                                "name": "final_thinking_timeout",
                                "title": "思考超时",
                                "content": f"最终思考阶段超过 {_grace_timeout:.0f} 秒，已截断。以下是已生成的回答。",
                                "step_type": "recovery",
                            }
                        }
                        break
                    event_type = event["type"]
                    event_data = event["data"]
                    if event_type == "reasoning":
                        # PHASE 5: final_thinking is THE user-visible reasoning
                        # phase. Tag the event so frontend can render it under
                        # the 💭 "思考过程" panel exclusively.
                        yield {"reasoning_content": event_data, "phase": "final"}
                    elif event_type == "content":
                        content_text = event_data
                        if '<think' in content_text:
                            content_text = _re.sub(r'<think[^>]*>.*?</think\s*>', '', content_text, flags=_re.DOTALL)
                            if '<think' in content_text:
                                content_text = _re.sub(r'<think[^>]*>.*', '', content_text, flags=_re.DOTALL)
                        content_text = _TOOL_CALL_RE.sub('', content_text)
                        content_text = _TOOL_INVOKE_RE.sub('', content_text)
                        content_text = _TOOL_CALL_XML_RE.sub('', content_text)
                        content_text = _TOOL_RESULT_RE.sub('', content_text)
                        content_text, _dsml_active, _dsml_tail = _strip_dsml_streaming(
                            content_text, _dsml_active, _dsml_tail
                        )
                        if content_text:
                            _produced = True
                            _produced_parts.append(content_text)
                            yield {"content": content_text}
                    elif event_type == "error":
                        _attempt_error = event_data
                        break
                    elif event_type == "done":
                        if isinstance(event_data, dict):
                            _finish_reason = event_data.get("finish_reason")
                        break
            except Exception as e:
                _attempt_error = e
                logger.warning("Final thinking attempt %d failed: %s", _attempt + 1, e)

            if _produced:
                # finish_reason=length means the provider cut the answer off at
                # the token ceiling — the collected text is GUARANTEED
                # incomplete (the model did not choose to stop). Persisting it
                # as-is is the classic "answer truncated mid-sentence" bug.
                # Continue generation from the cut point (bounded): append the
                # partial answer + a continue directive, stream the remainder.
                _continuations = 0
                while _finish_reason == "length" and _continuations < _max_continuations:
                    _continuations += 1
                    logger.warning(
                        "Final thinking truncated (finish_reason=length) — continuation %d/%d",
                        _continuations, _max_continuations,
                    )
                    _partial = "".join(_produced_parts)
                    state.messages.append({"role": "assistant", "content": _partial})
                    _inject_directive(
                        state,
                        "你的上一个回答因输出长度限制被截断。请从截断处无缝继续输出剩余内容："
                        "不要重复任何已输出的文字，不要加“好的/继续”等过渡语，直接接着写。"
                        "如果内容已基本完整，只需补全结尾。",
                        _ephemeral="length_continuation",
                    )
                    _cont_started = asyncio.get_event_loop().time()
                    _cont_error = None
                    _cont_finish = None
                    try:
                        _cont_kwargs = self._sampling_kwargs(False)
                        if not _cont_kwargs:
                            _cont_kwargs = {"temperature": config.default_temperature}
                        _cont_stream = self.llm.stream_chat_structured(
                            state.messages,
                            tools=None,
                            extra_body=build_thinking_extra_body(
                                self.provider_type, False, None,
                                thinking_budget=self.thinking_budget,
                            ),
                            **_cont_kwargs,
                        )
                        _dsml_active = False
                        _dsml_tail = ""
                        async for event in _cont_stream:
                            if _grace_timeout > 0 and (asyncio.get_event_loop().time() - _cont_started) > _grace_timeout:
                                logger.warning("Final thinking continuation timed out after %.1fs", _grace_timeout)
                                break
                            event_type = event["type"]
                            event_data = event["data"]
                            if event_type == "content":
                                content_text = event_data
                                if '<think' in content_text:
                                    content_text = _re.sub(r'<think[^>]*>.*?</think\s*>', '', content_text, flags=_re.DOTALL)
                                    if '<think' in content_text:
                                        content_text = _re.sub(r'<think[^>]*>.*', '', content_text, flags=_re.DOTALL)
                                content_text = _TOOL_CALL_RE.sub('', content_text)
                                content_text = _TOOL_INVOKE_RE.sub('', content_text)
                                content_text = _TOOL_CALL_XML_RE.sub('', content_text)
                                content_text = _TOOL_RESULT_RE.sub('', content_text)
                                content_text, _dsml_active, _dsml_tail = _strip_dsml_streaming(
                                    content_text, _dsml_active, _dsml_tail
                                )
                                if content_text:
                                    _produced_parts.append(content_text)
                                    yield {"content": content_text}
                            elif event_type == "error":
                                _cont_error = event_data
                                break
                            elif event_type == "done":
                                if isinstance(event_data, dict):
                                    _cont_finish = event_data.get("finish_reason")
                                break
                    except Exception as e:
                        _cont_error = e
                        logger.warning("Final thinking continuation failed: %s", e)
                    # Remove the ephemeral partial pair before the next round so
                    # the context carries a single up-to-date partial answer.
                    for _i in range(len(state.messages) - 1, -1, -1):
                        _m = state.messages[_i]
                        if _m.get("role") in ("system", "user") and _m.get("_ephemeral") == "length_continuation":
                            del state.messages[_i]
                        elif _m.get("role") == "assistant" and _m.get("content") == _partial:
                            del state.messages[_i]
                            break
                    _finish_reason = _cont_finish
                    if _cont_error is not None:
                        # Continuation failed: keep the partial answer we have —
                        # a 90%-complete visible answer beats an error bubble.
                        logger.warning(
                            "Final thinking continuation error (%s); keeping partial answer",
                            _cont_error,
                        )
                        break
                if _finish_reason == "length":
                    logger.warning(
                        "Final thinking still truncated after %d continuations; keeping stitched answer",
                        _max_continuations,
                    )
                return
            _last_error = _attempt_error
            if _attempt + 1 >= _max_attempts:
                break
            # Recovery before the retry: compress on context overflow (the
            # dominant production cause — heavy tool rounds bloat the request
            # past the context window and the provider rejects it instantly).
            # Abort-class errors (auth/billing/unknown) are NOT retried — the
            # terminal error below surfaces them instead of burning a request.
            if _last_error is not None:
                _should_retry, _action, _delay = await self._classify_llm_error(_last_error)
            else:
                _should_retry, _action, _delay = True, "retry", None
            if not _should_retry:
                logger.warning(
                    "Final thinking error is not recoverable (error=%s); surfacing terminal failure",
                    _last_error,
                )
                break
            logger.warning(
                "Final thinking attempt %d produced nothing (error=%s); recovery=%s, retrying",
                _attempt + 1, _last_error, _action,
            )
            if _action == "compress_context":
                try:
                    if self._compressor is None:
                        from app.services.context_compressor import ContextCompressor
                        self._compressor = ContextCompressor(quiet=True)
                        _ctx_len = config.agent_compression_context_length
                        if _ctx_len > 0:
                            self._compressor.update_context_length(_ctx_len)
                    from app.services.context_compressor import estimate_request_tokens_rough
                    _before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                    state.messages = await self._compressor.compress_async(state.messages)
                    _after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                    logger.info("Final thinking emergency compression %d->%d tokens", _before, _after)
                    yield {"context_info": {
                        "tokens": _after,
                        "context_length": self._compressor.context_length,
                        "compressed": True,
                    }}
                except Exception:
                    logger.exception("Final thinking compression failed")
            elif _action == "fallback_provider":
                # Mirror the main loop's recovery (agent_loop.py:1491): switch
                # to the configured fallback provider before retrying. With no
                # fallback configured there is nothing to retry with — surface
                # the terminal error instead of burning an identical request.
                if not self._try_fallback_provider():
                    logger.warning("Final thinking: no fallback provider available, surfacing failure")
                    break
            await asyncio.sleep(_delay if _delay is not None else 1.0)

        # Last resort: fall back to the loop's (audit-approved) draft when every
        # synthesis attempt failed. The draft was already generated against the
        # full tool context — a coherent answer, unlike an error bubble or the
        # empty/preamble-only saves that produced the conv daa19eac truncation.
        if draft_content and draft_content.strip():
            logger.warning(
                "Final thinking failed after %d attempts; falling back to loop draft (%d chars)",
                _max_attempts, len(draft_content),
            )
            yield {
                "agent_step": {
                    "name": "final_thinking_draft_fallback",
                    "title": "回答恢复",
                    "content": "深度思考阶段失败，已回退到基于工具结果生成的回答。",
                    "step_type": "recovery",
                }
            }
            yield {"content": draft_content}
            return

        yield {
            "error": (
                "最终回答生成失败"
                f"（已尝试 {_max_attempts} 次）：{_last_error or '模型未返回有效内容'}"
            )
        }

    def _repair_message_sequence(self, messages: List[Dict[str, Any]]) -> None:
        repaired = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            repaired.append(msg)
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                called_ids = {tc["id"] for tc in msg["tool_calls"] if tc.get("id")}
                j = i + 1
                responded_ids = set()
                while j < len(messages) and messages[j].get("role") == "tool":
                    responded_ids.add(messages[j].get("tool_call_id", ""))
                    repaired.append(messages[j])
                    j += 1
                missing = called_ids - responded_ids
                for missing_id in missing:
                    for tc in msg["tool_calls"]:
                        if tc.get("id") == missing_id:
                            logger.warning("Repairing missing tool response for call_id=%s", missing_id)
                            repaired.append({
                                "role": "tool",
                                "tool_call_id": missing_id,
                                "name": tc.get("function", {}).get("name", "unknown"),
                                "content": "[tool response was lost due to error recovery]",
                            })
                            break
                i = j
            else:
                i += 1
        messages[:] = repaired

    async def _apply_tool_result_budget(self, result: ToolCallResult) -> ToolCallResult:
        if result.error:
            return result
        budgetted_content = await maybe_persist_tool_result(
            content=result.result,
            tool_name=result.name,
            tool_use_id=result.call_id,
            config=self._budget_config,
            workspace_path=self.workspace_path,
        )
        if budgetted_content is not result.result:
            result = ToolCallResult(
                call_id=result.call_id,
                name=result.name,
                arguments=result.arguments,
                result=budgetted_content,
                error=result.error,
            )
        return result

    async def _digest_tool_results_batch(self, results: List[ToolCallResult]) -> List[ToolCallResult]:
        """Near-lossless subagent reduction of large content-heavy tool results.

        Eligible results (tool in the digest set, content above the threshold)
        are summarized by parallel subagent completions into <tool-digest>
        envelopes; the full text is persisted to a file (lossless by pointer).
        Ineligible/error results pass through; the budget persist applies to
        whatever remains (digested envelopes are already small). Order kept.
        """
        if not results:
            return results
        if not self._digest_config.enabled:
            return [await self._apply_tool_result_budget(r) if not r.error else r for r in results]
        digested = await digest_tool_results_batch(
            results, self._digest_config, parent_llm=self.llm,
            provider_type=self.iteration_provider_type, workspace_path=self.workspace_path,
        )
        out: List[ToolCallResult] = []
        for r in digested:
            if r.error:
                out.append(r)
                continue
            out.append(await self._apply_tool_result_budget(r))
        return out

    async def _digest_with_ping(self, results: List[ToolCallResult]):
        """Run the digest batch while emitting keepalive pings.

        The digest phase (subagent summarization + fact-check, up to the
        batch deadline of 300s) produces no events on its own; a bare await
        would leave the SSE stream silent past the frontend's 30s stall
        watchdog and kill the live view (agent would still finish detached).
        Mirrors the tool-execution 8s ping pattern.

        Yields ``{"ping": True}`` during the wait, then finally
        ``{"_digest_done": batch_result}``.
        """
        fut = asyncio.ensure_future(self._digest_tool_results_batch(results))
        try:
            while not fut.done():
                _done, _ = await asyncio.wait({fut}, timeout=8.0)
                if not _done:
                    yield {"ping": True}
            yield {"_digest_done": fut.result()}
        except asyncio.CancelledError:
            fut.cancel()
            try:
                await fut
            except asyncio.CancelledError:
                pass
            raise
        finally:
            # GeneratorExit (outer generator closed mid-yield) also orphans the
            # digest future — cancel it so no LLM calls burn until the deadline.
            if not fut.done():
                fut.cancel()

    async def _classify_llm_error(self, error) -> tuple:
        """Classify LLM error and return (recoverable, action, retry_after_seconds).

        P1 4.3: Extracts provider-supplied Retry-After delay from headers or
        error message. When available, this overrides the default backoff.
        Free-text classification is LLM-judged (agentic principle).
        """
        from app.services.error_classifier import ErrorClassifier, ErrorCategory
        if isinstance(error, str):
            category = await ErrorClassifier.classify_message(error)
            retry_after = None
        else:
            category, retry_after = await ErrorClassifier.classify(error)

        strategy = ErrorClassifier.get_recovery_strategy(category)
        recoverable = strategy.action != "abort"

        if not config.agent_error_recovery_enabled:
            return False, strategy.action, retry_after

        if strategy.action == "compress_context":
            return recoverable, "compress_context", retry_after
        if strategy.action == "retry":
            return recoverable, "retry", retry_after
        if strategy.action == "fallback_provider":
            return recoverable, "fallback_provider", retry_after

        return False, strategy.action, retry_after

    def _try_fallback_provider(self) -> bool:
        """Switch to next available provider when primary fails.

        Returns True if a fallback provider was found and switched to.
        """
        try:
            from app.services.provider_router import get_provider_router
            router = get_provider_router()
            available = router.list_available()
            if len(available) <= 1:
                return False
            for name in available:
                if name == "default":
                    continue
                kwargs = router.get_client_kwargs(name)
                model_name = router.get_model_name(name)
                if not kwargs.get("base_url"):
                    continue
                from app.services.llm_service import LLMService
                new_llm = LLMService(
                    custom_api_url=kwargs.get("base_url", ""),
                    custom_api_key=kwargs.get("api_key", ""),
                    custom_model_name=model_name,
                )
                self.iteration_llm = new_llm
                self.llm = new_llm
                self.iteration_provider_type = name
                logger.info("Switched to fallback provider: %s (%s)", name, model_name)
                return True
            return False
        except Exception as e:
            logger.warning("Fallback provider switch failed: %s", e)
            return False

        if strategy.action == "compress_context":
            return recoverable, "compress_context", retry_after
        if strategy.action == "retry":
            return recoverable, "retry", retry_after
        if strategy.action == "fallback_provider":
            return recoverable, "fallback_provider", retry_after

        return False, strategy.action, retry_after

    async def _is_recoverable_tool_error(self, result: ToolCallResult) -> bool:
        if not config.agent_error_recovery_enabled:
            return False
        try:
            parsed = json.loads(result.result)
            # parsed.get("error", "") returns None when the key exists with
            # a JSON null value (not the same as missing key). Coerce to
            # empty string so classify_message never receives None.
            error_msg = parsed.get("error") or ""
            if not error_msg:
                return False
            from app.services.error_classifier import ErrorClassifier
            category = await ErrorClassifier.classify_message(error_msg)
            return ErrorClassifier.is_recoverable(category)
        except (json.JSONDecodeError, Exception):
            return False

    async def _retry_tool_with_recovery(
        self,
        call_id: str,
        tool_name: str,
        tool_args: dict,
        session_factory: Any,
        user: Any,
        conversation: Any,
        assistant: Any,
        state: AgentLoopState,
        failed_result: Optional[ToolCallResult] = None,
    ) -> Optional[ToolCallResult]:
        error_msg = "unknown error"
        if failed_result is not None and failed_result.error:
            try:
                error_data = json.loads(failed_result.result) if failed_result.result else {}
                error_msg = error_data.get("error") or failed_result.result or f"Tool '{tool_name}' failed"
            except (json.JSONDecodeError, TypeError):
                error_msg = str(failed_result.result or f"Tool '{tool_name}' failed")
        elif state.tool_results:
            try:
                error_result = json.loads(state.tool_results[-1].result) if state.tool_results else {}
                error_msg = error_result.get("error") or str(state.tool_results[-1].result)
            except (json.JSONDecodeError, IndexError):
                pass

        from app.services.error_classifier import ErrorClassifier, ErrorCategory
        category = await ErrorClassifier.classify_message(error_msg)
        strategy = ErrorClassifier.get_recovery_strategy(category)

        if strategy.action == "abort":
            return None

        max_retries = config.agent_tool_loop_max_tool_retry_attempts
        for attempt in range(1, max_retries + 1):
            delay = jittered_backoff(attempt, base_delay=strategy.delay_seconds)
            logger.info(
                "Tool %s error recovery: attempt %d/%d, delay %.1fs, category=%s",
                tool_name, attempt, max_retries, delay, category.value,
            )
            await asyncio.sleep(delay)

            _retry_text, _retry_tr = self._current_turn_context(state)
            result = await self._execute_single_tool(
                call_id, tool_name, tool_args, session_factory, user, conversation, assistant,
                state,
                current_turn_content=_retry_text,
                current_turn_tool_results=_retry_tr,
            )
            if not result.error:
                return result

        return None

    def _current_turn_context(self, state: AgentLoopState) -> tuple:
        """Build (content, tool_results_json) for the in-progress turn.

        Returns a tuple of (joined_text, web_search_results_json) so tools
        like pdf_export can include the current turn's analysis and
        citations before the assistant message is persisted to the DB.
        """
        content = "\n\n".join(state.turn_content_segments)
        web_search_results: list = []
        for tr in state.tool_results:
            if tr.name == "web_search" and not tr.error and not tr.result.startswith("<tool-digest>"):
                try:
                    data = json.loads(tr.result)
                    results = data if isinstance(data, list) else data.get("results", [])
                    if isinstance(results, list):
                        web_search_results.extend(results)
                except (json.JSONDecodeError, TypeError):
                    pass
        tool_results_json = json.dumps({"results": web_search_results}) if web_search_results else ""
        return content, tool_results_json

    async def _execute_single_tool(
        self,
        call_id: str,
        tool_name: str,
        tool_args: dict,
        session_factory: Any,
        user: Any,
        conversation: Any,
        assistant: Any,
        state: AgentLoopState,
        current_turn_content: str = "",
        current_turn_tool_results: str = "",
    ) -> ToolCallResult:
        if tool_name in self.blocked_tools:
            logger.warning("Blocked tool call '%s' (depth=%d)", tool_name, self.delegation_depth)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=json.dumps({"error": f"Tool '{tool_name}' is blocked at this delegation depth"}),
                error=True,
            )

        factory = session_factory or self.session_factory
        if factory is None:
            from app.db.database import AsyncSessionLocal
            factory = AsyncSessionLocal

        # Per-turn memory-read dedup (conv dfc40619 2026-08-09): the
        # coordinator turn-focus directive + mandatory_tool_use rule 8 force
        # the model to re-read the same memory target every iteration — a
        # second identical read of the same target within ONE turn returns a
        # short note instead of re-injecting the whole 35KB document into
        # context. Placed BEFORE doom-loop tracking: a deduped call is
        # handled gracefully, not a loop to abort.
        if config.agent_tool_loop_memory_read_dedup and tool_name == "memory":
            _dedup_result = _maybe_dedupe_memory_read(state, tool_args)
            if _dedup_result is not None:
                logger.info(
                    "Memory read dedup: call=%s target=%s already read this turn",
                    call_id, tool_args.get("target") or "agent",
                )
                return ToolCallResult(
                    call_id=call_id,
                    name=tool_name,
                    arguments=tool_args,
                    result=_dedup_result,
                    error=False,
                )

        import hashlib, time as _time
        _t_start = _time.monotonic()
        _args_signature = hashlib.sha256(
            json.dumps(tool_args, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        state._doom_tool_history.append((tool_name, _args_signature))
        if len(state._doom_tool_history) > 6:
            state._doom_tool_history = state._doom_tool_history[-6:]

        recent_doom = state._doom_tool_history[-3:]
        if len(recent_doom) == 3 and len(set(recent_doom)) == 1:
            doom_name = recent_doom[0][0]
            logger.warning("Doom loop detected: %s called 3x with same args — aborting", doom_name)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=json.dumps({
                    "error": f"Doom loop detected: {doom_name} called 3 consecutive times with identical arguments. Aborting to prevent infinite loop. Try a different tool or approach."
                }, ensure_ascii=False),
                error=True,
            )

        try:
            schema = registry.get_schema(tool_name)
            tool_args = coerce_tool_args(tool_name, tool_args, schema)

            async with factory() as db:
                dispatch_kwargs = dict(
                    db=db,
                    user=user,
                    conversation=conversation,
                    assistant=assistant,
                    workspace_path=self.workspace_path,
                )
                if tool_name == "delegate_task" and self.delegation_depth > 0:
                    dispatch_kwargs["_delegation_depth"] = self.delegation_depth
                if current_turn_content:
                    dispatch_kwargs["current_turn_content"] = current_turn_content
                if current_turn_tool_results:
                    dispatch_kwargs["current_turn_tool_results"] = current_turn_tool_results

                perm_ctx = PermissionContext(
                    super_admin_bypass=config.super_admin_bypass,
                    deathmatch_active=(
                        self.deathmatch_manager is not None
                        and self.deathmatch_manager.is_goal_active
                    ),
                    user=user,
                )
                dispatch_kwargs["permission_context"] = perm_ctx
                if self._visible_tool_names is not None:
                    dispatch_kwargs["allowed_tools"] = self._visible_tool_names

                result = await registry.dispatch(
                    tool_name,
                    tool_args,
                    **dispatch_kwargs,
                )

                try:
                    result_parsed = json.loads(result)
                    if isinstance(result_parsed, dict) and result_parsed.get("_permission_needed"):
                        conversation_id = getattr(conversation, "id", "") if conversation else ""
                        target_path = result_parsed.get("_target_path", "")
                        command = result_parsed.get("_command", "")
                        action = result_parsed.get("_action", "")
                        perm_key = result_parsed.get("_permission_key") or permission_key_for_tool_request(tool_name, result_parsed)
                        description = (
                            result_parsed.get("_permission_description")
                            or (permission_description(perm_key) if perm_key else "请求执行风险操作")
                        )
                        details = {
                            "command": command,
                            "target_path": target_path,
                            "action": action,
                            "permission_key": perm_key,
                        }

                        auto_allowed = False
                        if config.super_admin_bypass:
                            auto_allowed = True
                        elif (
                            self.deathmatch_manager is not None
                            and self.deathmatch_manager.is_goal_active
                        ):
                            auto_allowed = True
                        elif perm_key and user is not None:
                            auto_allowed = is_permission_allowed(user, perm_key)

                        if auto_allowed:
                            tool_args["_permission_granted"] = True
                            result = await registry.dispatch(
                                tool_name,
                                tool_args,
                                **dispatch_kwargs,
                            )
                        elif self._permission_callback:
                            approved = await self._permission_callback(
                                conversation_id=conversation_id,
                                tool_name=tool_name,
                                description=description,
                                details=details,
                            )
                            if approved:
                                tool_args["_permission_granted"] = True
                                result = await registry.dispatch(
                                    tool_name,
                                    tool_args,
                                    **dispatch_kwargs,
                                )
                            else:
                                return ToolCallResult(
                                    call_id=call_id,
                                    name=tool_name,
                                    arguments=tool_args,
                                    result=json.dumps({"error": "用户拒绝了执行请求"}, ensure_ascii=False),
                                    error=True,
                                )
                        else:
                            return ToolCallResult(
                                call_id=call_id,
                                name=tool_name,
                                arguments=tool_args,
                                result=json.dumps({"error": f"Permission denied: {description}"}, ensure_ascii=False),
                                error=True,
                            )
                except (json.JSONDecodeError, TypeError):
                    pass

            _latency_ms = int((_time.monotonic() - _t_start) * 1000)
            logger.info("tool_trace: name=%s call_id=%s latency_ms=%d error=0",
                        tool_name, call_id, _latency_ms)
            # Record successful memory reads (dedup) / invalidate on writes;
            # a returned delegate_task clears the whole cache (its child may
            # have written to any memory target — A4.9 review finding).
            if config.agent_tool_loop_memory_read_dedup:
                if tool_name == "memory":
                    _track_memory_read(state, tool_args, result)
                elif tool_name == "delegate_task":
                    _clear_memory_read_cache(state)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=result,
                error=False,
            )
        except Exception as e:
            _latency_ms = int((_time.monotonic() - _t_start) * 1000)
            logger.exception("Error executing tool '%s' (latency_ms=%d)", tool_name, _latency_ms)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                arguments=tool_args,
                result=json.dumps({"error": f"Tool execution failed: {str(e)}"}),
                error=True,
            )

    def _get_state_summary(self, state: AgentLoopState) -> dict:
        return {
            "iterations": state.iterations,
            "total_tool_calls": state.total_tool_calls,
            "message_count": len(state.messages),
        }
