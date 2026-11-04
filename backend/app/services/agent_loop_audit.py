# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Audit helpers (P5.2 move-only, 2026-10-18).

Chunk splitting, chunk-claim JSON parsing and claim/evidence overlap scoring
moved verbatim from ``agent_loop.py``; the loop re-exports the same names.
"""
from __future__ import annotations

import json
import logging
import re as _re
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_config
from app.prompts.audit import AUDIT_SETTLED_RULES

config = get_config()
logger = logging.getLogger("app.services.agent_loop")

def _split_audit_chunks(
    draft: str,
    target_tokens: int = 3000,
    overlap_chars: int = 150,
    max_chunks: int = 16,
) -> "Tuple[List[str], int]":
    """段落语义优先的 token 目标分块 → (chunks, omitted_chunks)。

    - 段落聚合到 ~target_tokens（同一 CJK 感知估算器，非字符拍脑袋）；
    - 单段超目标 → 句末切分，仍超 → 字符等切，保证块有界；
    - 相邻块带 overlap_chars 重叠，减少边界声称被切碎；
    - 超过 max_chunks → 保留前 max_chunks，omitted 显式计数。
    """
    from app.services.context_compressor import estimate_text_tokens_rough as _est
    text = draft or ""
    if not text:
        return [], 0
    chunks: List[str] = []
    buf = ""
    for seg in text.split("\n\n"):
        cand = (buf + "\n\n" + seg) if buf else seg
        if _est(cand) <= target_tokens:
            buf = cand
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if _est(seg) <= target_tokens:
            buf = seg
            continue
        sbuf = ""
        for s_ in _re.split(r"(?<=[。！？!?；;.\n])", seg):
            sc = (sbuf + s_) if sbuf else s_
            if _est(sc) <= target_tokens:
                sbuf = sc
                continue
            if sbuf:
                chunks.append(sbuf)
                sbuf = ""
            if _est(s_) <= target_tokens:
                sbuf = s_
            else:
                _chars = max(200, int(len(s_) * target_tokens / max(1, _est(s_))))
                for i in range(0, len(s_), _chars):
                    chunks.append(s_[i:i + _chars])
        if sbuf:
            buf = sbuf
    if buf:
        chunks.append(buf)
    if not chunks:
        return [], 0
    overlapped: List[str] = []
    for i, ch in enumerate(chunks):
        if i > 0 and overlap_chars > 0:
            overlapped.append(chunks[i - 1][-overlap_chars:] + ch)
        else:
            overlapped.append(ch)
    omitted = max(0, len(overlapped) - max(1, int(max_chunks)))
    return overlapped[: max(1, int(max_chunks))], omitted

def _parse_chunk_claim_json(raw: str) -> "Optional[dict]":
    """宽容解析分块 JSON：fence 包裹/前后杂讯均可。失败返回 None（该块记未覆盖）。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                return data if isinstance(data, dict) else None
            except Exception:
                return None
        return None

_CJK_RUN_RE = _re.compile(r"[\u4e00-\u9fff]+")

_ASCII_TOKEN_RE = _re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.\-]{2,}")

def _claim_overlap_score(draft_text: str, result_text: str) -> float:
    """草稿声称与工具结果的重叠度（确定性 0..1，用于审计预算分配）。

    ASCII 词（≥3 字符，含 ._- 连缀，覆盖 verify.yml / python 等标识符）+
    CJK 二元组。2026-09-13 conv 5abef2bf：让草稿真正引用的证据优先进入
    审计窗口，而不是按时间先后被预算截断。
    """
    if not draft_text or not result_text:
        return 0.0
    draft = draft_text.lower()
    result = result_text.lower()
    toks: set = set(_ASCII_TOKEN_RE.findall(draft))
    for run in _CJK_RUN_RE.findall(draft):
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    if not toks:
        return 0.0
    res_toks: set = set(_ASCII_TOKEN_RE.findall(result))
    for run in _CJK_RUN_RE.findall(result):
        for i in range(len(run) - 1):
            res_toks.add(run[i:i + 2])
    if not res_toks:
        return 0.0
    return len(toks & res_toks) / len(toks)

_AUDIT_DIRECTIVE_OPENERS = (
    "你刚才生成的回答草稿", "内部完整性校验未通过", "【轮次核对】",
    "【数值核对闸门】", "请基于压缩后的上下文", "请基于以上工具调用结果",
    "你的上一个回答被截断", "你的上一个工具调用因为内容过长",
    "请重新完整回答",
)

_AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE = (
    "重写时必须输出完整独立、可直接发布的回答全文——严禁出现“修正”“上一版”"
    "“之前草稿”“审计/质检”等字样或任何对被拒草稿的指代"
    "（用户看不到被拒草稿与审计意见）。"
)

_AUDIT_RETRY_BREVITY_HINT = (
    "你上一次的输出未能解析为完整JSON（疑似超长被截断）。请重新判定并严格控制篇幅："
    "problem 不超过80字；unsupported_claims 最多3条、每条 claim 不超过40字；"
    "只输出完整合法的JSON，不要任何其他内容。"
)

_AUDIT_SETTLED_HEADER = "已定案修正项（本轮前几次审计已判定并要求修正的内容，不得翻案）"

_AUDIT_SETTLED_RULES = AUDIT_SETTLED_RULES

_AUDIT_GUIDANCE_LOCALIZED_CLAUSE = (
    "【修正方式】只修改上面点名的内容；草稿中其余未被点名且未被审计判定的内容保持原样——"
    "包括全部 [N] 引用标记及其位置，严禁删除、改动或重排引用编号；"
    "严禁整篇重写——整篇重写会破坏已修正的内容并引入新的错误。"
    "修正后必须输出完整独立、可直接发布的回答全文。"
)

_AUDIT_GUIDANCE_SETTLED_PREFIX = "【已定案修正项（必须遵守）】"

_AUDIT_GUIDANCE_EVIDENCE_RESCUE_MARKERS = (
    "无对应", "无法核实", "无法核对", "无证据支撑", "缺乏证据",
)

_AUDIT_GUIDANCE_EVIDENCE_RESCUE_CLAUSE = (
    "\n若被点名的细节/声称确实存在于来源但不在本轮证据中：优先用 browser 打开对应"
    "原文页面取得本轮证据（随后按本轮编号引用），无法取得时再删除/弱化。"
)

_AUDIT_TRUNCATION_MARKERS = (
    "截断", "未展示", "无法核对", "无法核实", "片段", "无对应", "无相应",
)


def _wrap(name, *a, **k):
    from app.services import agent_loop as _al
    return getattr(_al, name)(*a, **k)


def AuditVerdict(*a, **k):
    from app.services.agent_loop import AuditVerdict as _C
    return _C(*a, **k)


def _apply_claim_grounding_gate(*a, **k):
    return _wrap("_apply_claim_grounding_gate", *a, **k)


def _apply_numeric_gate(*a, **k):
    return _wrap("_apply_numeric_gate", *a, **k)


def _audit_problem_family(*a, **k):
    return _wrap("_audit_problem_family", *a, **k)


def _audit_user_facts_text(*a, **k):
    return _wrap("_audit_user_facts_text", *a, **k)


def _build_audit_evidence(*a, **k):
    return _wrap("_build_audit_evidence", *a, **k)


def _build_citation_mapping_view(*a, **k):
    return _wrap("_build_citation_mapping_view", *a, **k)


def _citation_map_false_premise(*a, **k):
    return _wrap("_citation_map_false_premise", *a, **k)


def _citation_remand_verdict(*a, **k):
    return _wrap("_citation_remand_verdict", *a, **k)


def _est_guard(*a, **k):
    return _wrap("_est_guard", *a, **k)


def _extract_audit_taxonomy(*a, **k):
    return _wrap("_extract_audit_taxonomy", *a, **k)


def _map_gate_has_other_issues(*a, **k):
    return _wrap("_map_gate_has_other_issues", *a, **k)


def _settled_items_view(*a, **k):
    return _wrap("_settled_items_view", *a, **k)


def _strip_map_absent_clauses(*a, **k):
    return _wrap("_strip_map_absent_clauses", *a, **k)


def _truncate_marked(*a, **k):
    return _wrap("_truncate_marked", *a, **k)


def _visibility_accept_gate(*a, **k):
    return _wrap("_visibility_accept_gate", *a, **k)


def evaluate_numeric_provenance(*a, **k):
    from app.services.numeric_provenance_gate import evaluate_numeric_provenance as f
    return f(*a, **k)



class AuditMixin:
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
        # 低置信标记入口复位（2026-09-16）：每次审计只携带本轮判定结果——
        # degraded/uncovered 接收置位，出货点消费一次后清零，杜绝陈旧标记串稿。
        state.audit_low_confidence = False
        state.audit_chunk_coverage_incomplete = False
        # F7：每次审计入口复位「未审计」标记——它只描述**本次**审计结果；
        # 仅当本次超预算且调用失败（fail-open）时重新置位。
        state.audit_budget_unaudited = False
        # 2026-09-21（conv a104fbc5，用户指令）：最新用户消息逐字全文（零截断）——
        # head-only 800 字符曾使审计员看不到长粘贴消息尾部的真实诉求
        # （「…受限估计是为什么？」），把「解释原因」的正确答案误判为
        # 「答非所问」；head+tail 亦被否决（中间仍可能切断诉求）。SOTA 对齐
        # 见 `_coordinator_recent_messages` 上方注释。
        context_parts = [f"用户最新消息：{last_user_msg}"]
        # 用户背景事实注入（2026-08-21 复盘, conv efaf8f9c）：审计员上下文
        # 只有最新消息+工具台账，看不到早期轮次用户自己陈述的事实（如
        # 64GB/Q8 硬件），把「用户说的事实」误判为「无证据声称」→ 拒绝
        # 循环。注入此前用户消息（逐字整条；超预算整条跳过并披露）：
        # 草稿引用用户语境事实 ≠ 编造。
        _user_facts_text = _audit_user_facts_text(state.messages)
        if _user_facts_text:
            context_parts.append(
                "对话历史中用户自己陈述的背景事实（此前用户消息，逐字；"
                "草稿引用这些不属编造，不得按\u201c无证据声称\u201d拒绝）：\n"
                + _user_facts_text
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
            # A4.9 r5 Important-2：全量列名曾无上界——40 名 + 明示余量（不静默）。
            _names_view = ", ".join(_audit_tools[:40])
            if len(_audit_tools) > 40:
                _names_view += f" …（另有 {len(_audit_tools) - 40} 次调用未列名；未展示 ≠ 未调用）"
            context_parts.append(
                f"本轮实际调用工具（共 {len(_audit_tools)} 次，供核对检索声称）：{_names_view}"
            )
            # conv 2d6ff3a7 (2026-09-10): when citation numbers are the issue,
            # the auditor must be able to name the VALID id range so the writer
            # fixes citations instead of abandoning them after repeated rejects.
            _ledger = getattr(state, "citation_ledger", None)
            _ledger_size = _ledger.size if _ledger is not None else 0
            if _ledger_size > 0:
                context_parts.append(
                    f"本轮检索结果的有效引用编号范围：[1]-[{_ledger_size}]（共 {_ledger_size} 条）。"
                    "草稿的 [N] 引用编号以下方【引用编号对照表】为准；"
                    "因引用编号问题打回时，problem 中须写明该范围，引导写手改用有效编号修正引用，"
                    "而非放弃引用。"
                )
        else:
            # conv 357c110d (2026-08-13): the flat "无工具=声称检索即编造" line
            # made the auditor reject GROUNDED drafts twice — the citations
            # [N] were real previous-turn search results (rebuilt into history
            # via structured tool messages), and a user-memory fact (7900 XTX)
            # was flagged as invented. Distinguish fresh-claim from reuse.
            context_parts.append(
                "本轮实际调用工具：无。判定口径："
                "(a) 草稿声称或暗示本轮新执行了检索/工具调用（如“我刚搜索了”“本次检索发现”）→ 属编造，判不合格；"
                "(b) 草稿基于之前轮次真实检索结果的内容作答、未声称本轮重新检索、"
                "所述内容与前次检索结果一致 → 不属编造，可判合格；"
                "但历史检索结果的引用编号仅在其所属轮次有效——零工具轮的草稿不得带 [N] "
                "引用标记（历史编号对本轮读者不可解析、会指向错误来源），带 [N] 的按无法核实处理，"
                "应要求重新检索获得本轮编号或删除引用标记（conv a040c24e：模型复用两轮前的旧编号，"
                "读者点开引用卡看到无关链接）；"
            "(c) 助手带有用户长期记忆（设备型号、偏好、个人信息等），草稿中用户相关事实可能来自记忆，"
            "不要仅因对话历史中未出现就判编造——台账 [M] 中可见的记忆是权威证据，"
            "与其一致的声称视为有据；"
                "(d) 草稿中的冷僻具体事实声称（地方典故、地名由来、小众人物事件、"
                "具体日期/数字、实体的存在性/发布/上市状态等）若在下方证据台账（含历史轮次工具结果）中没有对应证据"
                "且超出常识通识范围，按 needs_evidence 处理——"
                "要求调用 web_search 核实后再答，或删除/弱化无法核实的具体声称；"
                "常识通识性陈述不受此限。"
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
        _evidence_ledger, _evidence_text, _evidence_full = await _build_audit_evidence(
            state, draft_text=draft, workspace_path=getattr(self, "workspace_path", "") or "")
        _audit_ceiling = int(config.agent_audit_evidence_context_ceiling_tokens)
        _audit_prompt_budget = max(8000, _audit_ceiling - 8000)
        # NPG (2026-09-02, conv 83d97ede): writer-side numeric provenance gate
        # — B-series design (memory/research_math_bi_agent_numeric_guarantees_
        # 20260902.md). Deterministic, zero LLM: high-risk derived numbers in
        # the draft must be quoted (ledger/user messages, unit-family aware)
        # or receipt-backed (CURRENT-turn execute_code/terminal output + B1
        # param provenance — `print(40000)` launders nothing). Pure mental
        # arithmetic on a fresh computation question previously shipped
        # unchecked (40ml vs 186.5ml, 4.66× off) because the ledger never
        # contains computed values and the auditor is forbidden to mental-
        # math reject. Modes (B10 gray rollout): shadow=observe only
        # (default), enforce=needs_evidence BEFORE the LLM audit, off=skip.
        _npg_mode = config.agent_audit_numeric_provenance_mode
        _npg_report = None
        if _npg_mode != "off":
            # I5 (A4.9): fail-open — a pathological NPG input must degrade to
            # the pre-NPG behavior, never kill the turn (auditor contract).
            try:
                _npg_user_msgs = [
                    str(m.get("content") or "")
                    for m in state.messages
                    if m.get("role") == "user" and not m.get("synthetic")
                    and not str(m.get("content") or "").startswith(_AUDIT_DIRECTIVE_OPENERS)
                ]
                _npg_tools = []
                for _tr in state.tool_results:
                    if _tr.error:
                        continue
                    if _tr.name in ("execute_code", "terminal"):
                        _npg_tools.append({
                            "name": _tr.name,
                            "code": str((_tr.arguments or {}).get("code")
                                        or (_tr.arguments or {}).get("command") or ""),
                            "output": _tr.result or "",
                        })
                    elif _tr.name == "calculate":
                        # C1 (A4.9): calculate receipts must reach the receipt
                        # domain — the expression is the B1 "code" (a bare
                        # literal expression is still a hardcode).
                        _npg_tools.append({
                            "name": _tr.name,
                            "code": str((_tr.arguments or {}).get("expression") or ""),
                            "output": _tr.result or "",
                        })
                _npg_report = evaluate_numeric_provenance(
                    draft=draft,
                    evidence_text=_evidence_full or "",
                    user_messages=_npg_user_msgs,
                    current_tool_calls=_npg_tools,
                    tolerance=config.agent_audit_numeric_gate_tolerance,
                )
            except Exception:
                logger.exception("npg evaluation failed — fail-open, skipping numeric gate")
                _npg_report = None
            if _npg_report is not None and _npg_report.flagged:
                _npg_flags = _npg_report.flags
                _flag_view = "、".join(f"「{f.raw}」" for f in _npg_flags[:6])
                if len(_npg_flags) > 6:
                    _flag_view += f" 等{len(_npg_flags)}处"
                if _npg_mode == "enforce":
                    # no-progress 守卫输入（conv 3a216a51）：记录本次 enforce
                    # 打回的 flags 数、raw 集合与当时的工具调用数。
                    state.npg_reject_flags.append(len(_npg_flags))
                    state.npg_reject_raws.append(frozenset(f.raw for f in _npg_flags))
                    state.npg_reject_tool_calls.append(state.total_tool_calls)
                    logger.info(
                        "audit_metric outcome=needs_evidence npg=enforce flags=%d draft_chars=%d",
                        len(_npg_flags), len(draft),
                    )
                    # I1 (A4.9): settled-ledger context — re-flagged numeric
                    # corrections keep the anti flip-flop protection.
                    _npg_settled = _settled_items_view(state, evidence_text=_evidence_full)
                    _npg_prefix = (
                        f"{_AUDIT_GUIDANCE_SETTLED_PREFIX}\n{_npg_settled}\n"
                    ) if _npg_settled else ""
                    return AuditVerdict(
                        verdict="needs_evidence",
                        source="npg",
                        problem=f"草稿含未经计算回执的推导数值：{_flag_view}",
                        guidance=(
                            _npg_prefix
                            + f"你刚才生成的回答草稿（本轮）包含未经计算验证的推导数值：{_flag_view}。\n"
                            "【数值溯源闸门】上述数值未出现在用户提供的资料或任何工具输出中——"
                            "严禁以心算得出数值。请立即调用 calculate（或 execute_code，多步计算时"
                            "打印公式、输入参数与中间量）实际计算这些数值，以计算输出为准修正草稿；"
                            "已有 calculate/execute_code 回执的数值及其单位换算（mL/cm³/L、"
                            "mm³/cm³ 等同族换算）无需重复计算，可直接引用台账输出；"
                            "非计算所得的行业经验/常识范围数值（如「150~250 mL 常见」）"
                            "请改写为定性表述或标注「经验值」，不要因此删除有效分析内容；"
                            "若该数值本应来自资料引用，请改写为与原文一致的表述并标注来源。"
                            + "\n" + _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
                            + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
                        ),
                    )
                logger.info(
                    "audit_metric npg_shadow flags=%d values=%s draft_chars=%d",
                    len(_npg_flags), _flag_view, len(draft),
                )
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
        _evidence_text_idx = None
        if _evidence_text:
            _evidence_text_idx = len(context_parts)
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
        _settled_view = _settled_items_view(state, evidence_text=_evidence_full)
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
        # Citation mapping table (conv 4e95acc0, 2026-09-12): placed
        # immediately BEFORE the draft so the auditor's citation check runs
        # against the citation namespace, not the evidence-ledger's tool-item
        # indexes. See _build_citation_mapping_view.
        _citation_map = _build_citation_mapping_view(state, draft)
        if _citation_map:
            context_parts.append(_citation_map)
        # 草稿全文纪律（2026-10-06 用户红线，conv ae9aa092）：审计 prompt 中的
        # 草稿永远逐字全文——头+尾省略中段 = 断章取义（引用落省略段时审计员
        # 会睁眼误判「没有引用」）。效率手段只允许分块全覆盖审计
        # （`_chunked_full_audit`，零省略），绝不允许截断。
        _draft_idx = len(context_parts)
        context_parts.append(f"助手草稿回答（逐字全文）：{draft}")
        # A4.9 r5 Important-2：发送前硬守卫——预留金是估算，若组装后的审计提示
        # 仍逼近上下文上限，按优先级显式降级（2026-10-06 起：先收起证据正文，
        # 草稿永不缩；草稿本身超巨型 → 分块全覆盖审计），
        # 绝不把 400 → fail-open 未审计出货留给 provider。
        from app.services.context_compressor import estimate_text_tokens_rough as _est_guard
        if _est_guard("\n\n".join(context_parts)) > _audit_prompt_budget:
            _evidence_dropped = False
            if _evidence_text_idx is not None:
                context_parts[_evidence_text_idx] = (
                    "（证据正文因上下文上限未展开——证据台账仍完整列出各条目的"
                    "可见性/截断状态；对未展开证据只能判 unverifiable/needs_evidence，"
                    "严禁判 reject/编造。）"
                )
                _evidence_dropped = True
            if _est_guard("\n\n".join(context_parts)) > _audit_prompt_budget:
                # 草稿本身超巨型（证据已收完仍超预算）→ 分块全覆盖审计。
                _chunked = await self._chunked_full_audit(
                    state, draft, context_parts, _draft_idx, _audit_prompt_budget)
                if _chunked is not None:
                    # A4.9 r1 C1：分块全覆盖 accept（哨兵 verdict="accept"）=
                    # 审计已判合格——直接放行（None），绝不落回超预算单调用
                    # （重复审计/分块 accept 被单调用 reject 的 flip-flop）。
                    if _chunked.verdict == "accept":
                        return None
                    return _chunked
                if getattr(state, "audit_chunk_coverage_incomplete", False):
                    # 分块覆盖未达成（块失败/解析失败）——低置信出货底线：
                    # 不再重复单调用（必然再超预算），按 fail-open 契约放行。
                    state.audit_budget_unaudited = True
                    logger.warning(
                        "audit_metric outcome=fail_open reason=chunked_coverage_incomplete — accepting draft")
                    return None
            logger.warning(
                "audit_prompt_guard: prompt tokens ~%d > budget %d — degraded (evidence_text_dropped=%s, draft_never_cut=True)",
                _est_guard("\n\n".join(context_parts)), _audit_prompt_budget,
                _evidence_dropped,
            )
        # F7（A4.9）：用户消息 headline 逐字全文后不再设上界——全部降级后仍超预算
        # 时，审计调用可能被 provider 拒绝并走 fail-open（未审计出货）。fail-open
        # 路径据此置位 `audit_budget_unaudited`，出货时附加「未能完成自动复核」说明
        # （未审计出货不得静默）。
        _audit_over_budget = (
            _est_guard("\n\n".join(context_parts)) > _audit_prompt_budget
        )
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
                # `getattr` keeps __new__-constructed loops (test harnesses,
                # legacy callers) on the pre-routing coordinator behavior.
                _audit_llm = (
                    getattr(self, "audit_llm", None)
                    or getattr(self, "coordinator_llm", None)
                )
                raw = await self._complete_json(_msgs, temperature=0.0, llm=_audit_llm)
            except Exception as exc:
                # Deterministic zero-citation gate runs on accept-class exits
                # too (A4.9 R1 Important-1): the guard is independent of the
                # auditor's availability — a broken/weak auditor must not
                # reopen the hole this fix closes.
                _remand = _citation_remand_verdict(state, draft)
                if _remand is not None:
                    return _remand
                # fail-open stays (a broken auditor must never block a good
                # answer) but it is no longer SILENT — an un-audited ship during
                # a provider outage is exactly the condition worth alerting on.
                if _audit_over_budget:
                    state.audit_budget_unaudited = True
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
            _remand = _citation_remand_verdict(state, draft)
            if _remand is not None:
                return _remand
            if _audit_over_budget:
                state.audit_budget_unaudited = True
            logger.warning("audit_metric outcome=fail_open reason=bad_json raw=%s — accepting draft", raw[:160])
            return None
        if not isinstance(result, dict):
            _remand = _citation_remand_verdict(state, draft)
            if _remand is not None:
                return _remand
            if _audit_over_budget:
                state.audit_budget_unaudited = True
            logger.warning("audit_metric outcome=fail_open reason=non_dict — accepting draft")
            return None
        ok_val = result.get("ok")
        ok = ok_val if isinstance(ok_val, bool) else str(ok_val).strip().lower() in {"true", "yes", "1"}
        verdict = str(result.get("verdict") or "").strip().lower()
        if ok or verdict == "accept":
            # Deterministic zero-citation gate (2026-09-15, conv 174182bc):
            # the LLM clause proved unreliable (same weak model wrote and
            # audited) — a mechanical remand overrides the accept ONCE.
            _citation_remand = _citation_remand_verdict(state, draft)
            if _citation_remand is not None:
                return _citation_remand
            logger.info("audit_metric outcome=accept draft_chars=%d", len(draft))
            return None
        if verdict not in ("reject", "unverifiable", "needs_evidence"):
            # Backward-compat: old {ok: false} shape without a verdict.
            verdict = "reject"
        problem = str(result.get("problem") or "").strip()
        if not problem:
            _remand = _citation_remand_verdict(state, draft)
            if _remand is not None:
                return _remand
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
        # ── 分段声称核对（2026-09-15，用户拍板；替代可见性闸门主路径）──
        # 判决为可见性族且台账存在截断 → 确定性 claim→span 对齐 + 并行轻量
        # 验证（独立于写手/单窗审计）：任一 fail（span 直接矛盾）→ 软拒点名；
        # 无 fail → 接收。不可判定/未启用/异常 → 回退存在性闸门（下一段）。
        # A4.9 R1 Important-2：触发口沿用存在性闸门的精度护栏——矛盾族/
        # 悬空指代族/contradicted 状态一律不进入分段接收（弱审计员的
        # unverifiable 不得覆盖这些更硬的信号）。
        _seg_family = _audit_problem_family("llm", problem)
        _vis_family = (
            (
                verdict == "unverifiable"
                or (
                    verdict == "needs_evidence"
                    and any(k in problem for k in _AUDIT_TRUNCATION_MARKERS)
                )
            )
            and _seg_family not in ("contradiction", "self_contained")
            and not any(
                isinstance(_c, dict)
                and str(_c.get("evidence_status") or "").strip().lower() == "contradicted"
                for _c in _claims
            )
        )
        # veto 组合（A4.9 R1 Critical-1/Important-2 修复）：**先按存在性闸门的
        # 旧契约判定**（审计点名 token 全在场——缺席 token 绝不被静默原谅），
        # 通过后再跑分段核对做质量否决（任一已验证声称与证据矛盾 → reject）。
        # 闸门不通过 → 分段核对不跑（不引入比旧路径更宽的接收面）。
        _seg = None
        _gate_accept = _visibility_accept_gate(
            verdict, "llm", problem, _claims, _evidence_full, _evidence_ledger,
        )
        if _gate_accept and _vis_family and "截断" in (_evidence_ledger or ""):
            _seg = await self._segmented_claim_verify(
                draft, _evidence_text, _evidence_full)
            if _seg is not None:
                if _seg.failed:
                    _seg_view = "；".join(
                        f"「{c[:60]}」→ {r[:80]}" for c, r in _seg.failed[:4])
                    _seg_settled = _settled_items_view(state, evidence_text=_evidence_full)
                    _seg_prefix = (
                        f"{_AUDIT_GUIDANCE_SETTLED_PREFIX}\n{_seg_settled}\n"
                    ) if _seg_settled else ""
                    logger.info(
                        "audit_metric outcome=reject claim_verify=fail packs=%d failed=%d draft_chars=%d",
                        _seg.packs, len(_seg.failed), len(draft),
                    )
                    return AuditVerdict(
                        # reject 而非 needs_evidence（A4.9 R1 Important-3）：
                        # 验证器亲眼见到证据矛盾——软判决会被 deterministic
                        # 兜底选稿的「soft 优先」规则选中出货；reject 走
                        # contradiction 感知的 _draft_ground_key 排名，且
                        # 消耗硬拒预算（family=claim_verify 仍受 stall cut 界住）。
                        verdict="reject",
                        source="claim_verify",
                        problem=f"分段声称核对发现与证据直接矛盾的声称：{_seg_view}",
                        contradicted_claims=[c for c, _r in _seg.failed[:5]],
                        guidance=(
                            f"{_seg_prefix}你刚才生成的回答草稿（本轮）存在与证据直接矛盾的声称"
                            "（已按证据切片逐条核对）：\n"
                            + "\n".join(
                                f"- 「{c[:120]}」→ {r[:160]}" for c, r in _seg.failed[:5])
                            + "\n请以被拒稿为底稿，仅修正上述被点名部分（数值/结论以证据为准），"
                            "其余内容逐字保留，严禁整篇重写。"
                            + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
                        ),
                    )
        if _gate_accept:
            _remand = _citation_remand_verdict(state, draft)
            if _remand is not None:
                return _remand
            if _seg is not None and not _seg.degraded:
                logger.info(
                    "audit_metric outcome=accept_claim_verify verdict=%s packs=%d unverifiable=%d uncovered=%d draft_chars=%d",
                    verdict, _seg.packs, _seg.unverifiable, _seg.uncovered, len(draft),
                )
            else:
                logger.info(
                    "audit_metric outcome=accept_visibility verdict=%s degraded=%s uncovered=%d draft_chars=%d problem=%s",
                    verdict,
                    _seg is not None and _seg.degraded,
                    _seg.uncovered if _seg is not None else 0,
                    len(draft), problem[:120],
                )
            # 低置信标记（2026-09-16 C4 评估残余风险①②，用户拍板处理）：
            # degraded（核对基础设施失败回退存在性闸门）或 uncovered（声称超出
            # 逐条核对覆盖上限）的接收不再静默——出货点在回答末尾追加说明
            # （_AUDIT_LOW_CONFIDENCE_NOTE）；开关 low_confidence_note_enabled。
            if (
                _seg is not None
                and (_seg.degraded or _seg.uncovered > 0)
                and config.agent_audit_low_confidence_note_enabled
            ):
                state.audit_low_confidence = True
            return None
        _settled_guidance = _settled_items_view(state, evidence_text=_evidence_full)
        _settled_prefix = ""
        if _settled_guidance:
            _settled_prefix = (
                f"{_AUDIT_GUIDANCE_SETTLED_PREFIX}\n{_settled_guidance}\n"
                "（若上述修正项之间对同一事实给出互相矛盾的修正值，"
                "按证据并列标注来源差异，不得单方面取舍。）\n"
            )
        _verdict_before_gates = verdict
        # A1 deterministic numeric gate: a reject asserting a correction
        # value absent from the evidence ledger cannot be satisfied (conv
        # efaf8f9c: auditor hallucinated 12.5GiB; writer was right at
        # 15.5GiB; 5 consecutive rejects). Downgrade before budget
        # accounting — needs_evidence does NOT consume the reject budget.
        verdict, problem = _apply_numeric_gate(verdict, problem, _evidence_full)
        # A5 (2026-08-22, conv 3b58af5b): claim-grounding gate —「无依据声称」
        # rejections whose flagged claim tokens ARE in the evidence ledger are
        # auditor blindness, not draft defects. Same downgrade path as A1
        # (needs_evidence does NOT consume the reject budget).
        verdict, problem = _apply_claim_grounding_gate(verdict, problem, _claims, _evidence_full)
        # E2（A4.9 复评 Minor）：taxonomy 与最终 verdict 一致——降级门改变了
        # verdict（reject→needs_evidence）时清空，避免把「审计员声称的幻觉」
        # 归因到「证据不可见」的最终判决上
        _taxonomy = (_extract_audit_taxonomy(result)
                     if verdict == _verdict_before_gates else "")
        logger.info("audit_metric outcome=%s taxonomy=%s draft_chars=%d problem=%s",
                    verdict, _taxonomy or "-", len(draft), problem[:160])
        # 对照表假判词确定性闸门（2026-09-16，conv f2553c58 生产，B 件套）：
        # 审计声称「无对照表/对照表未列出」而台账非空且草稿编号全部合法 →
        # 前提可证伪（对照表已渲染进审计上下文，audit_citation_map 为证；
        # 大上下文稀释是审计盲区，非草稿缺陷）。纯假判词 → 直接接收；
        # 混合判词 → 剔除「修编号」无效指令（写手不再空转），保留其余真问题。
        # 开关 citation_map_gate_enabled（默认 true）。
        if config.agent_audit_citation_map_gate_enabled:
            _map_fp, _map_size = _citation_map_false_premise(problem, state, draft)
            if _map_fp:
                _map_remaining = _strip_map_absent_clauses(problem)
                if not _map_gate_has_other_issues(_map_remaining):
                    # A4.9 R1 Important-1：接收路径不得绕过零引用 remand 闸
                    # （与另两条接收路径同契约）。
                    _map_remand = _citation_remand_verdict(state, draft)
                    if _map_remand is not None:
                        return _map_remand
                    logger.info(
                        "audit_metric outcome=accept_map_gate verdict=%s ledger=%d draft_chars=%d",
                        verdict, _map_size, len(draft),
                    )
                    return None
                problem = (
                    f"【对照表核对闸门】草稿的 [N] 引用编号经确定性核对全部合法"
                    f"「对照表共 {_map_size} 条，全部命中、无越界」，编号本身无需改动；"
                    "请仅针对以下其余问题修正：" + _map_remaining
                )
                logger.info(
                    "audit_metric outcome=map_gate_corrected verdict=%s ledger=%d draft_chars=%d",
                    verdict, _map_size, len(draft),
                )
        # 证据侦察（2026-09-13 conv 5abef2bf）：审计失败即触发一次有界侦察，
        # 把草稿声称对应的证据切片补进后续审计窗口（每轮 ≤1；失败无操作）。
        if not state.scout_used:
            await self._scout_missing_evidence(state, draft, problem)
        if verdict == "reject":
            _guidance = (
                f"{_settled_prefix}你刚才生成的回答草稿（本轮，即你上一条 assistant 消息）未通过质量审计：{_truncate_marked(problem, 600)}\n"
                "【复制编辑修正协议】请以那篇草稿为底稿，输出修正后的完整回答全文："
                "仅修改审计点名的部分；未被点名的内容逐字保留，"
                "严禁整篇重写，严禁改变未被点名的数字、结构、顺序与措辞。"
                + "\n" + _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
                + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
            )
        elif verdict == "needs_evidence":
            _guidance = (
                f"{_settled_prefix}你刚才生成的回答草稿（本轮）证据不足：{_truncate_marked(problem, 600)}\n"
                "请立即调用工具（memory/workspace_read/web_search 等）补充真实证据后再作答；"
                "若确实无法获取，如实说明“我无法获知”，不要编造。"
                + (
                    _AUDIT_GUIDANCE_EVIDENCE_RESCUE_CLAUSE
                    if any(k in problem for k in _AUDIT_GUIDANCE_EVIDENCE_RESCUE_MARKERS)
                    else ""
                )
                + "\n" + _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
                + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
            )
        else:  # unverifiable
            _guidance = (
                f"{_settled_prefix}你刚才生成的回答草稿（本轮）存在无法核实的声称（证据被截断或证据中不存在）：{_truncate_marked(problem, 600)}\n"
                "请删除无法核实的声称，或补充读取证据后再作答；"
                "若证据确实不足，如实说明“我无法获知”，不要编造。"
                + (
                    _AUDIT_GUIDANCE_EVIDENCE_RESCUE_CLAUSE
                    if any(k in problem for k in _AUDIT_GUIDANCE_EVIDENCE_RESCUE_MARKERS)
                    else ""
                )
                + "\n" + _AUDIT_GUIDANCE_LOCALIZED_CLAUSE
                + "\n" + _AUDIT_GUIDANCE_INDEPENDENCE_CLAUSE
            )
        if _claims_view:
            _guidance += _claims_view
        # C-10: record only the FINAL reject (after numeric / claim-grounding /
        # citation-map gates).  Recording at parse time queued drafts that
        # production later accepted or downgraded (A4.9 review).
        if verdict == "reject":
            try:
                import asyncio

                from app.services.prompt_feedback_intake import record_feedback_event

                await asyncio.to_thread(
                    record_feedback_event,
                    prompt_id="audit.response",
                    source="audit_reject",
                    input_text=draft,
                    expected="reject",
                    note=problem,
                )
            except Exception:
                logger.debug("prompt feedback intake skipped", exc_info=True)
        return AuditVerdict(verdict=verdict, guidance=_guidance, problem=problem,
                            taxonomy=_taxonomy, unsupported_claims=_claims)
