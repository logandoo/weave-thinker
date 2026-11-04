# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deathmatch plan helpers (P5.4 move-only, 2026-10-18)."""
from __future__ import annotations

import copy
import json
import logging
import re as _re
import shlex as _shlex
import time as _time
from typing import Any, Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.services.deathmatch_creative import _normalize_goal_key

config = get_config()
_PLAN_MAX_STEPS = 30
_STEP_KINDS = frozenset({"write", "research", "verify", "synthesize"})
_EVIDENCE_SPEC_KINDS = frozenset({"compute", "retrieval", "tool_output"})
logger = logging.getLogger(__name__)


def _merge_criteria(
    system_criteria: List[Dict[str, Any]],
    user_subgoals: List[Any],
) -> List[Dict[str, Any]]:
    """Merge stored system criteria with user-appended subgoals (D3 compat):
    dedupe by normalized text, user entries become judgmental criteria."""
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for c in (system_criteria or []):
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or "").strip()
        key = _re.sub(r"\s+", "", text)
        if not text or key in seen:
            continue
        seen.add(key)
        merged.append(dict(c))
    for sg in (user_subgoals or []):
        text = str(sg or "").strip()
        key = _re.sub(r"\s+", "", text)
        if not text or key in seen:
            continue
        seen.add(key)
        merged.append({
            "id": f"u{len(merged) + 1}",
            "text": text[:500],
            "type": "judgmental",
            "source": "user",
            "check": {"kind": "none"},
        })
    # A4.9 Minor: enforce globally unique ids (system ids come from the LLM
    # and may collide with the u<N> user ids or each other).
    used: set = set()
    for c in merged:
        cid = str(c.get("id") or "")
        if not cid or cid in used:
            n = len(used) + 1
            cid = f"c{n}"
            while cid in used:
                n += 1
                cid = f"c{n}"
        c["id"] = cid
        used.add(cid)
    return merged

def _format_criteria_block(criteria: List[Dict[str, Any]]) -> str:
    items = [
        c for c in (criteria or [])
        if isinstance(c, dict) and str(c.get("text") or "").strip()
    ]
    if not items:
        return ""
    lines = [
        "<acceptance_criteria>",
        "验收标准（必须全部满足才算完成；append-only，不得放松）：",
    ]
    for c in items:
        tag = {"mechanical": "机械可检", "judgmental": "需判断"}.get(str(c.get("type")), "需判断")
        src = "用户追加" if c.get("source") == "user" else "系统生成"
        check = c.get("check") or {}
        extra = ""
        if check.get("kind") == "file":
            extra = f"（检查：文件 {check.get('path')} ≥{check.get('min_bytes') or 0}B）"
        elif check.get("kind") == "gate":
            extra = f"（检查：命令 {str(check.get('cmd'))[:120]}）"
        elif check.get("kind") == "execution_content":
            extra = "（检查：agentic 判定交付物是否含未执行占位）"
        lines.append(f"- [{c.get('id')}][{tag}/{src}] {c.get('text')}{extra}")
    byte_facts = []
    for c in criteria:
        check = c.get("check") or {}
        if check.get("kind") != "exact_file":
            continue
        try:
            size = len(str(check.get("content") or "").encode("utf-8"))
        except UnicodeEncodeError:
            continue
        safe_path = json.dumps(str(check.get("path") or ""), ensure_ascii=True)
        byte_facts.append(f"- {safe_path}：exact_file.content 的实际 UTF-8 字节数为 {size}")
    if byte_facts:
        lines.append("【系统机械核算（权威字节数）】")
        lines.append("以下数值由程序直接计算，优先于目标散文中的任何模型估算：")
        lines.extend(byte_facts)
    lines.append("</acceptance_criteria>")
    return "\n".join(lines)


def _canonicalize_single_exact_file_plan(plan: Any, criteria: List[Dict[str, Any]]) -> Any:
    """Return a deterministic one-step plan for one root-level exact_file.

    The exact byte comparator is authoritative. The generated receipt resolves
    any existing target symlink and checks containment before writing. Nested,
    external, or large contracts keep the normal planner path.
    """
    mechanical = [
        c for c in (criteria or [])
        if isinstance(c, dict) and str(c.get("type") or "").lower() == "mechanical"
        and not c.get("obligation")
        and str((c.get("check") or {}).get("kind") or "") not in ("execution_content", "markers")
    ]
    exact = [
        c for c in mechanical
        if str((c.get("check") or {}).get("kind") or "") == "exact_file"
    ]
    if len(mechanical) != 1 or len(exact) != 1:
        return plan
    check = exact[0].get("check") or {}
    path = str(check.get("path") or "").strip()
    content = check.get("content")
    normalized = path.replace("\\", "/")
    if (
        not path or not isinstance(content, str) or len(content.encode("utf-8")) > 4096
        or path.startswith(("/", "\\", "~"))
        or "/" in normalized or ".." in normalized.split("/")
    ):
        return plan
    judgmental = [
        str(c.get("text") or "").strip() for c in (criteria or [])
        if isinstance(c, dict) and str(c.get("type") or "").lower() == "judgmental"
        and str(c.get("text") or "").strip()
    ]
    canonical_reference = _re.compile(r"(?<![\w.-])" + _re.escape(path) + r"(?![\w.-])")
    independent = []
    response_only = []
    for text in judgmental:
        other_file_text = canonical_reference.sub("", text)
        has_creation_verb = any(marker in text for marker in (
            "创建", "生成", "写入", "保存", "另存", "新建", "产出文件", "输出文件",
        ))
        created_target_response = _re.search(
            r"所创建文件\s*(?:为|是|的)?\s*" + _re.escape(path) + r"(?![\w.-])",
            text,
        )
        response_file_context = (
            (created_target_response or "写入完成" in text)
            and bool(canonical_reference.search(text))
            and "绝对路径" in text
            and not _re.search(r"\.[A-Za-z0-9]{1,8}(?![A-Za-z0-9])", other_file_text)
        )
        if created_target_response and _re.search(r"(?:和|与|及|、|，|,)", text[created_target_response.end():]):
            response_file_context = False
        creation_action = has_creation_verb and not response_file_context
        delivery_action = any(marker in text for marker in ("交付", "提供", "附上")) and "绝对路径" not in text
        if (
            creation_action or delivery_action
            or any(marker in text for marker in ("附件", "独立文件", "单独文件", "额外文件"))
            or _re.search(r"\.[A-Za-z0-9]{1,8}(?![A-Za-z0-9])", other_file_text)
        ):
            independent.append(text)
        else:
            response_only.append(text)
    expected = f"工作区文件 {path} 的内容逐字节等于 exact_file；最终回复含绝对路径与真实回读证据"
    if response_only:
        expected += "；并同时满足：" + "；".join(response_only)
    octal = "".join(f"\\{byte:03o}" for byte in content.encode("utf-8"))
    command = [
        "root=$(pwd -P)",
        f"target=\"$root\"/{_shlex.quote(path)}",
        "test ! -L \"$target\"",
        "[[ \"$target\" == \"$root\"/* ]]",
        f"printf {_shlex.quote(octal)} > \"$target\"",
        "wc -c \"$target\"",
        "od -An -tx1 \"$target\"",
    ]
    step = {
        "id": "s1",
        "kind": "write",
        "status": "pending",
        "description": f"写入 {path} 并立即用工具回读核对精确字节，随后在最终回复中给出绝对路径和核对结果",
        "expected_output": expected,
        "verification_method": "系统 exact_file comparator 逐字节验证；最终回复核对工具回读与绝对路径",
        "boundary": f"仅允许创建或覆盖 {path}；不得创建或修改其他文件",
        "writes": [path],
        "output_files": [path],
        "dependencies": [],
        "delegable": False,
        "parallel_safe": False,
        "recovery": "idle",
        "attempts": 0,
        "done_check": {"mode": "none"},
        "evidence_spec": {
            "kind": "compute",
            "must_run": True,
            "receipt": {
                "cmds": command,
                "outputs": [path],
                "min_rows": 1,
                "min_exit_code": 0,
            },
            "on_failure": "blocked",
        },
    }
    steps = [step]
    if independent:
        steps.append({
            "id": "s2",
            "kind": "synthesize",
            "status": "pending",
            "description": "完成独立交付物及其他 judgmental 验收要求",
            "expected_output": "；".join(independent),
            "verification_method": "由 judge 对照 judgmental criteria 与实际交付物/最终回复逐项核验",
            "boundary": f"不得修改 {path} 已验收内容；仅完成 judgmental criteria 明确要求的交付",
            "writes": [],
            "output_files": [],
            "dependencies": ["s1"],
            "delegable": False,
            "parallel_safe": False,
            "recovery": "idle",
            "attempts": 0,
            "done_check": {"mode": "none"},
            "judgmental": True,
        })
    canonical = dict(plan) if isinstance(plan, dict) else {}
    canonical["steps"] = steps
    return canonical


def _canonical_judgmental_step_id(criteria: List[Dict[str, Any]]) -> Optional[str]:
    """Return the system-generated judgmental step ID, if any.

    The exemption is derived from the contract, never trusted from an LLM step.
    """
    canonical = _canonicalize_single_exact_file_plan(None, criteria)
    if not isinstance(canonical, dict):
        return None
    steps = canonical.get("steps") or []
    if len(steps) == 2 and steps[1].get("judgmental"):
        return str(steps[1].get("id") or "") or None
    return None


def _validate_plan_protocol(
    plan: Any,
    valid_tool_names: Optional[set] = None,
    require_evidence_spec_ids: Optional[set] = None,
) -> List[str]:
    """Validate a parsed plan against the plan protocol. Returns a list of
    issue strings (empty = pass). Pure function — no I/O, no LLM.

    Three layers (mirroring the JIT-Agent validation ladder): JSON shape →
    protocol rules (ids, descriptions, step cap) → verification_method
    format ("gate: <cmd>" must carry a command). When ``valid_tool_names``
    is given, a step's declared ``tools`` subset is additionally checked
    against the registry (T8 capability orchestration).
    """
    issues: List[str] = []
    if not isinstance(plan, dict):
        return ["计划不是 JSON 对象"]
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["steps 为空或不是列表"]
    if len(steps) > _PLAN_MAX_STEPS:
        issues.append(f"步骤数 {len(steps)} 超过上限 {_PLAN_MAX_STEPS}")
    seen_ids: set = set()
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            issues.append(f"第 {i + 1} 步不是对象")
            continue
        sid = str(s.get("id") or "").strip()
        if not sid:
            issues.append(f"第 {i + 1} 步缺少 id")
        elif sid in seen_ids:
            issues.append(f"步骤 id 重复: {sid}")
        else:
            seen_ids.add(sid)
        if not str(s.get("description") or "").strip():
            issues.append(f"步骤 {sid or i + 1} 的 description 为空")
        vm = str(s.get("verification_method") or "").strip()
        if vm.lower().startswith("gate:") and not vm[5:].strip():
            issues.append(f"步骤 {sid or i + 1} 的 verification_method 为 'gate:' 但缺少验证命令")
        _tools = s.get("tools")
        if _tools is not None and not isinstance(_tools, list):
            issues.append(f"步骤 {sid or i + 1} 的 tools 不是列表")
        elif isinstance(_tools, list) and valid_tool_names is not None:
            for t in _tools:
                if not isinstance(t, str) or t not in valid_tool_names:
                    issues.append(f"步骤 {sid or i + 1} 声明了未知工具: {t}")
                    break
        if "delegable" in s and not isinstance(s.get("delegable"), bool):
            issues.append(f"步骤 {sid or i + 1} 的 delegable 不是布尔值")
    # ── v2（死磕 DAG 波次 W1b）：步骤契约 + 依赖图校验 ──────────────────
    issues.extend(_validate_step_graph(steps))
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip() or f"#{i + 1}"
        kind = s.get("kind")
        if kind is not None and str(kind).strip().lower() not in _STEP_KINDS:
            issues.append(f"步骤 {sid} 的 kind 非法: {kind}")
        if "parallel_safe" in s and not isinstance(s.get("parallel_safe"), bool):
            issues.append(f"步骤 {sid} 的 parallel_safe 不是布尔值")
        writes = s.get("writes")
        if writes is not None and (
            not isinstance(writes, list)
            or any(not isinstance(w, str) or not w.strip() for w in writes)
        ):
            issues.append(f"步骤 {sid} 的 writes 必须是字符串列表")
        dc = s.get("done_check")
        if dc is not None:
            _mode = str(dc.get("mode") or "").strip().lower() if isinstance(dc, dict) else ""
            if not isinstance(dc, dict) or _mode not in ("file", "gate", "llm", "none"):
                issues.append(f"步骤 {sid} 的 done_check 非法")
            elif _mode == "gate" and not str(dc.get("cmd") or "").strip():
                issues.append(f"步骤 {sid} 的 done_check(gate) 缺少 cmd")
            elif _mode == "file" and not str(dc.get("path") or "").strip():
                issues.append(f"步骤 {sid} 的 done_check(file) 缺少 path")
        # W1a（2026-09-21 a104）：evidence_spec 协议校验（收据溯源在 W1b 消费）
        es = s.get("evidence_spec")
        if es is not None:
            if not isinstance(es, dict):
                issues.append(f"步骤 {sid} 的 evidence_spec 不是对象")
            elif es:
                _es_kind = str(es.get("kind") or "").strip().lower()
                if _es_kind not in _EVIDENCE_SPEC_KINDS:
                    issues.append(f"步骤 {sid} 的 evidence_spec.kind 非法: {es.get('kind')}")
                _es_norm = _normalize_evidence_spec(es)
                if not _es_norm["receipt"]["cmds"]:
                    issues.append(f"步骤 {sid} 的 evidence_spec.receipt.cmds 为空")
                if _es_kind != "tool_output" and not _es_norm["receipt"]["outputs"]:
                    issues.append(f"步骤 {sid} 的 evidence_spec.receipt.outputs 为空")
                if _es_kind == "tool_output" and _es_norm["receipt"]["outputs"]:
                    issues.append(f"步骤 {sid} 的 tool_output.receipt.outputs 必须为空")
        # M3（r5/r6/r8）：数值结果步骤必须携带 evidence_spec（fail-closed）；
        # 需要哪些步骤由 **agentic 步骤判定**给出（`require_evidence_spec_ids`），
        # 本函数只做结构校验——不含任何关键词分类。
        if (
            not es
            and require_evidence_spec_ids is not None
            and sid in require_evidence_spec_ids
        ):
            issues.append(
                f"步骤 {sid} 的 expected_output 要求数值结果，但缺少 evidence_spec"
            )
    return issues

def _validate_done_steps_preserved(
    old_steps: List[Dict[str, Any]],
    new_steps: List[Dict[str, Any]],
) -> List[str]:
    """W1c: settled (done) steps are immutable across replan — id + statement
    + done status must survive verbatim; anything else is a violation."""
    issues: List[str] = []
    new_by_id = {
        str(s.get("id")): s for s in (new_steps or [])
        if isinstance(s, dict) and s.get("id")
    }
    for s in (old_steps or []):
        if not isinstance(s, dict) or s.get("status") != "done":
            continue
        sid = str(s.get("id") or "")
        new = new_by_id.get(sid)
        if new is None:
            issues.append(f"已完成步骤 {sid} 在重规划后消失")
            continue
        if step_statement_hash(s) != step_statement_hash(new):
            issues.append(f"已完成步骤 {sid} 的陈述被改写（违反不可变契约）")
        if str(new.get("status") or "pending") != "done":
            issues.append(f"已完成步骤 {sid} 的状态被回退")
    return issues


def _validate_step_graph(steps: List[Dict[str, Any]]) -> List[str]:
    """Dependency integrity + cycle detection (a plan must be a DAG)."""
    issues: List[str] = []
    if not isinstance(steps, list):
        return ["steps 不是列表"]
    id_set = {
        str(s.get("id") or "").strip()
        for s in steps if isinstance(s, dict) and str(s.get("id") or "").strip()
    }
    for s in steps:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip() or "?"
        deps = s.get("dependencies") or []
        if not isinstance(deps, list):
            issues.append(f"步骤 {sid} 的 dependencies 不是列表")
            continue
        for d in deps:
            dsid = str(d)
            if dsid == sid:
                issues.append(f"步骤 {sid} 依赖自身")
            elif dsid not in id_set:
                issues.append(f"步骤 {sid} 依赖不存在的步骤: {d}")
    graph = {
        str(s.get("id") or "").strip(): [
            str(d) for d in (s.get("dependencies") or []) if isinstance(d, (str, int))
        ]
        for s in steps
        if isinstance(s, dict) and str(s.get("id") or "").strip()
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def _dfs(n: str) -> bool:
        color[n] = GRAY
        for m in graph.get(n, []):
            if m not in color:
                continue
            if color[m] == GRAY:
                return True
            if color[m] == WHITE and _dfs(m):
                return True
        color[n] = BLACK
        return False

    if any(color[n] == WHITE and _dfs(n) for n in list(graph)):
        issues.append("步骤依赖存在环")
    return issues


async def _ensure_execution_judged(goal: str) -> Optional[bool]:
    """r6/r8（用户红线：语义判断必须 agentic）：判断目标是否要求**真实执行/
    计算/数据结果**（实证、统计、基准、回归等），替代关键词枚举触发。一次判定
    按目标缓存；**失败/超时返回 None 且不缓存**（A4.9 r8 Critical：judge_json
    失败返回 None 不抛异常，若不显式处理会导致瞬时故障永久解武装）；调用方负责
    记录失败事件。"""
    key = _normalize_goal_key(goal)
    if not key:
        return False
    cached = _EXECUTION_NEED_CACHE.get(key)
    if cached is not None and cached is not _EXECUTION_NEED_PENDING:
        return cached is True
    if cached is _EXECUTION_NEED_PENDING:
        return None
    if len(_EXECUTION_NEED_CACHE) > 256:
        _EXECUTION_NEED_CACHE.clear()
    _EXECUTION_NEED_CACHE[key] = _EXECUTION_NEED_PENDING
    parsed: Any = None
    try:
        from app.services.agentic_judge import judge_json
        parsed = await judge_json(
            "你是任务分类器。判断给定的用户目标是否**要求真实执行/计算/数据结果**"
            "（例如实证分析、统计/回归、基准测试、以真实数据支撑的结论）；"
            "纯文学创作、纯方案/设计描述、纯文字整理不需要执行。\n"
            '输出JSON：{"needs_execution": true|false}',
            f"用户目标：\n{key[:800]}\n\n只输出JSON。",
            task="execution_need",
            default=None,
            timeout=120.0,
        )
    except Exception as exc:
        logger.warning("execution-need LLM judgment failed: %s", exc)
        parsed = None
    if not isinstance(parsed, dict):
        _EXECUTION_NEED_CACHE.pop(key, None)
        return None
    result = bool(parsed.get("needs_execution"))
    _EXECUTION_NEED_CACHE[key] = result
    return result

_EXECUTION_NEED_CACHE: Dict[str, Any] = {}

_EXECUTION_NEED_PENDING = object()


def _normalize_evidence_spec(raw):
    from app.services.deathmatch_service import _normalize_evidence_spec as _impl
    return _impl(raw)


def step_statement_hash(step):
    from app.services.deathmatch_service import step_statement_hash as _impl
    return _impl(step)


class DeathmatchPlanMixin:
    def _all_criteria(self) -> List[Dict[str, Any]]:
        """W1a: merged contract criteria — stored system criteria + user
        subgoals (D3 compat). r5：义务在计划定稿时**持久化**进 stored
        （append-only）。r6（#2）：kill switch 关闭时过滤掉义务判据（热关）。"""
        stored = getattr(self._conv, "deathmatch_acceptance_criteria", None) or []
        subgoals = getattr(self._conv, "deathmatch_subgoals", None) or []
        merged = _merge_criteria(
            stored if isinstance(stored, list) else [],
            subgoals if isinstance(subgoals, list) else [],
        )
        if not self._obligation_criteria_enabled():
            merged = [
                c for c in merged
                if not (
                    isinstance(c, dict)
                    and (
                        c.get("obligation")
                        or (c.get("check") or {}).get("kind") == "markers"
                    )
                )
            ]
        return merged

    def _goal_with_criteria(self) -> str:
        """W1a: goal + full acceptance criteria block — the judge sees the
        whole contract (append-only) instead of the prose goal alone."""
        base = self._goal_with_subgoals()
        block = _format_criteria_block(self._all_criteria())
        return (base + "\n\n" + block) if block else base

    def _bump_no_progress_replan(self) -> bool:
        """W2b: count a no-progress replan; True when the cap is reached."""
        self._conv.deathmatch_no_progress_replans = (
            int(getattr(self._conv, "deathmatch_no_progress_replans", 0) or 0) + 1
        )
        cap = int(config.deathmatch_no_progress_replan_cap or 0)
        reached = bool(cap) and self._conv.deathmatch_no_progress_replans >= cap
        self._record_event(
            "no_progress_replan",
            count=self._conv.deathmatch_no_progress_replans, cap=cap, reached=reached,
        )
        return reached

    def _reset_no_progress_replan(self) -> None:
        if int(getattr(self._conv, "deathmatch_no_progress_replans", 0) or 0):
            self._conv.deathmatch_no_progress_replans = 0
            self._record_event("no_progress_replan", count=0, reset=True)

    def _obligation_criteria_enabled(self) -> bool:
        """W1a kill switch (config.toml [deathmatch] obligation_criteria_enabled)."""
        return bool(config.deathmatch_obligation_criteria_enabled)

    def _fresh_goal_obligations_pending(self) -> bool:
        """Read the persisted goal epoch across requests/manager rebuilds."""
        history = self._current_qa_history()
        for entry in reversed(history):
            if isinstance(entry, dict) and "goal_contract_pending" in entry:
                return bool(entry.get("goal_contract_pending"))
        return False

    def _consume_fresh_goal_obligations(self) -> None:
        """Close pending epochs only after a plan was successfully written."""
        # Deep copy before flipping nested epoch flags. A shallow list copy
        # mutates the ORM's original dicts first, so the later assignment looks
        # like no net change and the DB keeps pending=true while the CAS
        # baseline says false.
        updated = copy.deepcopy(self._current_qa_history())
        changed = False
        for entry in updated:
            if (
                isinstance(entry, dict)
                and "goal_contract_pending" in entry
                and entry.get("goal_contract_pending")
            ):
                entry["goal_contract_pending"] = False
                changed = True
        if not changed:
            return
        # QA history is outside write-through: this assignment commits (or
        # rolls back) together with the plan on the surrounding session.
        self._conv.deathmatch_grilling_qa_history = updated

    def _format_plan_progress(self) -> str:
        """Render plan progress as a compact summary for the continuation prompt."""
        plan = self._conv.deathmatch_plan
        if not plan or not isinstance(plan, dict):
            return "(无计划)"
        steps = plan.get("steps") or []
        if not steps:
            return "(无计划步骤)"
        done = sum(1 for s in steps if s.get("status") == "done")
        total = len(steps)
        lines = [f"计划进度: {done}/{total} 步已完成"]
        for s in steps:
            mark = {"done": "[已完成]", "in_progress": "[进行中]", "pending": "[待执行]"}.get(
                s.get("status", "pending"), "[待执行]"
            )
            desc = s.get("description", "")[:80]
            lines.append(f"  {mark} {s.get('id')}: {desc}")
        return "\n".join(lines)

    def build_plan_directive(self) -> str:
        """Build a plan directive message for the first agent turn.

        This is injected into the messages when deathmatch goal loop starts
        so the agent knows the plan from the very beginning — not just from
        continuation prompts on turn 2+.
        """
        plan = self._conv.deathmatch_plan
        if not plan or not isinstance(plan, dict):
            return ""
        steps = plan.get("steps") or []
        if not steps:
            return ""

        next_step = self._get_next_pending_step()
        if next_step is None:
            return ""

        lines = [
            "[死磕模式 — 计划执行指令]",
            f"目标: {self._conv.deathmatch_goal or '(未设定)'}",
            "",
            "你必须严格按照以下计划步骤顺序执行，不要跳过步骤，不要在计划之外自由发挥。",
            "每个步骤完成后才能开始下一个步骤。不要同时做多个步骤的工作。",
            "",
            "执行计划:",
        ]
        for s in steps:
            mark = {"done": "[已完成]", "in_progress": "[进行中]", "pending": "[待执行]"}.get(
                s.get("status", "pending"), "[待执行]"
            )
            lines.append(f"  {mark} {s.get('id')}: {s.get('description', '')[:100]}")
            if s.get("expected_output"):
                lines.append(f"    预期产出: {s['expected_output'][:100]}")

        lines.append("")
        lines.append(f"当前应执行的步骤: {next_step.get('id')} — {next_step.get('description', '')[:100]}")
        lines.append(f"该步骤预期产出: {next_step.get('expected_output', '')[:100]}")
        evidence_spec = next_step.get("evidence_spec")
        if isinstance(evidence_spec, dict):
            lines.append("该步骤机械执行义务（evidence_spec；不得用其他命令或交付说明代替）:")
            lines.append(json.dumps(evidence_spec, ensure_ascii=False))
            lines.append("合并执行 receipt.cmds 时必须按序使用 &&；分号、||、管道不能作为成功回执")
            lines.append("receipt.cmds 已包含写入与回读；全部成功后不要再执行其他 shell 回读，直接给出绝对路径和核对结论；失败时只按原命令修正重试")
        lines.append("")
        lines.append("重要要求:")
        lines.append("1. 严格按照计划步骤顺序执行，先完成当前步骤再做下一个")
        lines.append("2. 不要在计划步骤之外自由发挥（如未到写作步骤就开始写正文）")
        lines.append("3. 如果需要生成大量内容，分多次调用工具，每次只处理一部分（单次写入不超过约1500字），每写完一部分用 workspace_read 回读上一部分结尾确认衔接一致后再继续")
        lines.append("4. 完成当前步骤后明确说明产出内容和文件名")
        lines.append("5. 在生成本步骤内容前，使用 workspace_read 读取前序步骤的文件，确保风格、设定一致")
        lines.append("6. 如果步骤有字数要求，完成后使用 word_count 统计，不满足则需补充")
        lines.append("7. 全部完成后使用 provide_file（交付物是文件夹时用 provide_folder）将最终文件作为下载卡片提供给用户")
        lines.append("8. 严禁移动、删除、重命名、复制任何已有文件。严禁执行 mv、rm、cp 等文件操作命令")
        lines.append("9. 严禁操作、修改、删除与当前任务无关的文件。所有文件应直接生成到目标位置")
        lines.append("10. 严禁规划'清理工作区'、'整理文件'等与用户目标无关的文件管理操作")

        return "\n".join(lines)

    async def generate_goal_plan(
        self, db: AsyncSession, *, fresh_goal: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Generate a structured plan right after grilling completes.

        Persists to ``conversation.deathmatch_plan`` and bumps
        ``deathmatch_plan_version``. On failure leaves ``deathmatch_plan`` as
        None (no step gating — the goal loop runs on the generic continuation
        prompt and plan generation is retried on the next stall tier-1
        replan; conv 6b0faf81: a single-step fallback with generic
        expected_output was marked done by any file and stopped the loop).
        """
        goal = self._conv.deathmatch_goal or ""
        if not goal.strip():
            return None

        user_prompt = self._build_plan_user_prompt()
        llm = self._make_llm()
        system_prompt = self.PLAN_SYSTEM_PROMPT.replace("{self_eval_hint}", self._self_eval_hint())
        raw = await self._llm_generate(
            llm,
            system_prompt,
            user_prompt,
        )

        plan = await self._parse_plan_with_repair(
            raw, user_prompt, llm, system_prompt=system_prompt,
        )
        canonical_plan = _canonicalize_single_exact_file_plan(plan, self._all_criteria())
        if canonical_plan is not plan:
            self._record_event("plan_canonicalized", reason="single_exact_file", steps=1)
            plan = canonical_plan
        canonical_fallback = _canonicalize_single_exact_file_plan(None, self._all_criteria())
        if not plan:
            # Degrade: NO plan (conv 6b0faf81). Previously this built a
            # single all-encompassing step with generic expected_output
            # ("完成目标描述的最终产出"), which the verifier marks done as
            # soon as ANY file exists — for a 50-chapter novel that happened
            # after chapter 1, and the resulting "plan complete but goal
            # unmet" state stopped the loop at partial_complete after 4
            # turns. Without a plan the goal loop runs on the generic
            # continuation prompt (goal + work summary + turn guidance) and
            # plan generation is retried on the first stall tier-1 replan.
            self._conv.deathmatch_plan = None
            logger.info(
                "PEVR planner: JSON parse failed for conv=%s — running goal loop "
                "without step gating; plan retried on next stall replan",
                self._conv.id,
            )
            return None
        # W1d: independent plan-core audit (default ON, fail-open) — one
        # bounded repair + re-audit; remaining issues are recorded but never
        # block the loop.
        try:
            audit_issues = await self._audit_plan_core(plan)
        except Exception as exc:
            logger.warning("plan-core audit error (fail-open): %s", exc)
            audit_issues = []
        if audit_issues:
            self._record_event("plan_audit", issues=audit_issues[:5], repaired=False)
            try:
                repair_prompt = (
                    user_prompt
                    + "\n\n【计划核心审计发现问题】\n- "
                    + "\n- ".join(audit_issues[:8])
                    + "\n请修复上述问题后重新输出完整 JSON 计划（只输出 JSON）。"
                )
                raw2 = await self._llm_generate(llm, system_prompt, repair_prompt)
                plan2 = await self._parse_plan_with_repair(
                    raw2, user_prompt, llm, system_prompt=system_prompt,
                )
                if not plan2:
                    if canonical_fallback is None:
                        self._conv.deathmatch_plan = None
                        self._record_event(
                            "plan_rejected", issues=audit_issues[:5],
                            reason="repair_parse_failed",
                        )
                        return None
                    plan2 = canonical_fallback
                    self._record_event("plan_canonicalized", reason="repair_parse_failed", steps=1)
                plan2 = _canonicalize_single_exact_file_plan(plan2, self._all_criteria())
                remaining = await self._audit_plan_core(plan2)
                if remaining:
                    if canonical_fallback is None:
                        # Never execute a plan whose audit still proves a hard
                        # contradiction (e.g. 18B vs the 17B goal).
                        self._conv.deathmatch_plan = None
                        self._record_event(
                            "plan_rejected", issues=audit_issues[:5],
                            remaining=remaining[:5],
                        )
                        return None
                    plan = canonical_fallback
                    self._record_event(
                        "plan_canonicalized", reason="reaudit_fallback", steps=1,
                    )
                else:
                    plan = plan2
                    self._record_event(
                        "plan_audit", issues=audit_issues[:5],
                        remaining=[], repaired=True,
                    )
            except Exception as exc:
                if canonical_fallback is None:
                    # Initial audit failure is fail-open, but once a hard issue is
                    # known, a failed repair/second audit must not revive that plan.
                    self._conv.deathmatch_plan = None
                    self._record_event(
                        "plan_rejected", issues=audit_issues[:5],
                        reason=f"repair_or_reaudit_failed:{type(exc).__name__}",
                    )
                    logger.warning("plan-core audit repair rejected: %s", exc)
                    return None
                plan = canonical_fallback
                self._record_event(
                    "plan_canonicalized", reason="repair_exception_fallback", steps=1,
                )
        else:
            self._record_event("plan_audit", issues=[], repaired=False)

        _old_version = int(self._conv.deathmatch_plan_version or 0)
        _fresh_goal = fresh_goal or self._fresh_goal_obligations_pending()
        if config.deathmatch_obligation_criteria_enabled:
            _need = await _ensure_execution_judged(str(self._conv.deathmatch_goal or ""))
            if _need is None:
                # r9（A4.9 Gap A）：触发判定不可用 → fail-closed，不写计划
                # （version 保持 0；后续 replan 重试判定并可按 initial 武装）。
                self._record_event(
                    "execution_need_judge_failed", reason="judge_unavailable",
                )
                return None
            # r8（A4.9 #5）：哪些步骤承诺数值/执行结果 → agentic 步骤判定
            # （替代 _NUMERIC_OUTPUT_RE）；缺 evidence_spec → 有界修复，失败
            # fail-closed（plan=None 走通用目标循环，不武装步骤门）。
            canonical_judgmental_id = _canonical_judgmental_step_id(self._all_criteria())
            _required = await self._judge_steps_needing_evidence_spec([
                s for s in (plan.get("steps") or [])
                if str(s.get("id") or "") != canonical_judgmental_id
            ])
            if _required:
                _issues = _validate_plan_protocol(
                    plan, None, require_evidence_spec_ids=_required
                )
                if _issues:
                    logger.info(
                        "PEVR planner: evidence_spec missing (%s) — one repair",
                        "; ".join(_issues[:3]),
                    )
                    _raw2 = await self._llm_generate(
                        llm, system_prompt,
                        user_prompt
                        + "\n\n【必须修复】以下步骤承诺数值结果但缺少 evidence_spec：\n- "
                        + "\n- ".join(_issues[:6])
                        + "\n请为这些步骤补充 evidence_spec 后重新输出完整 JSON 计划"
                        "（只输出 JSON）。",
                    )
                    _plan2 = await self._parse_plan_with_repair(
                        _raw2, user_prompt, llm, system_prompt=system_prompt,
                    )
                    _ok = bool(_plan2) and not _validate_plan_protocol(
                        _plan2, None, require_evidence_spec_ids=_required
                    )
                    plan = _plan2 if _ok else None
                    if plan is None:
                        self._record_event("evidence_spec_repair_failed")
        if plan is None:
            self._conv.deathmatch_plan = None
            return None
        # A-N1（fix-round C1/C2）：基线名单在**计划创建（=目标环启动）**捕获
        # ——os.walk 全量 + 归一 + 排序 + cap 5000（超 cap 记 baseline_truncated
        # = 名单不完整 → 禁用豁免）。turn-1 产出（含 unzip/tar 保 mtime 产物）
        # 在捕获之后才存在 → 不是基线成员 → 豁免成立（修 turn-1 误拒）。
        # workspace 解析（scoped 复审必修）：计划创建时 self._workspace_path
        # 恒空（manager 每请求新建，仅 verify/evaluate 才赋值）——必须经
        # ensure_user_workspace 解析真实工作区，否则捕获死接线 → 永远 late
        # 兜底 → 豁免禁用 → turn-1 保 mtime 误拒不治。解析仍不可得 → 不写
        # 名单（未知=严格 mtime，phantom 侧安全）。
        _ws = self._workspace_path
        if not _ws:
            try:
                from app.services.workspace_service import ensure_user_workspace
                _ws_obj = await ensure_user_workspace(db, self._conv.user_id)
                _ws = str(getattr(_ws_obj, "root_path", "") or "")
            except Exception as exc:
                logger.debug("baseline workspace resolve failed (strict mtime): %s", exc)
                _ws = ""
        _baseline = self._capture_baseline_paths(_ws)
        if _baseline is not None:
            plan["baseline_paths"] = _baseline[: self._BASELINE_PATHS_CAP]
            if len(_baseline) > self._BASELINE_PATHS_CAP:
                plan["baseline_truncated"] = True
        # C1（评审）：目标环启动时刻 = lane 1 证据出处锚（同步骤早轮产出
        # 过闸 / 目标启动前的同名遗留文件不过闸）。
        plan.setdefault("created_at", _time.time())
        self._conv.deathmatch_plan = plan
        self._conv.deathmatch_plan_version = _old_version + 1
        self._apply_obligations(initial=(_old_version == 0 or _fresh_goal))
        self._consume_fresh_goal_obligations()
        return plan

    async def replan(self, verifier_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Revise the plan based on verifier output. Records a reflection."""
        plan = self._conv.deathmatch_plan or {"steps": []}
        steps = plan.get("steps") or []
        _all_done = bool(steps) and all(s.get("status") == "done" for s in steps)
        user_prompt = (
            f"用户目标:\n{(self._conv.deathmatch_goal or '')[:1500]}\n\n"
            f"当前计划:\n{json.dumps(steps, ensure_ascii=False)[:1500]}\n\n"
            f"验证结果:\n状态={verifier_result.get('status')}\n"
            f"问题={verifier_result.get('issues')}\n"
            f"建议={verifier_result.get('retry_instruction')}"
        )
        if _all_done:
            user_prompt += (
                "\n\n注意：当前计划的所有步骤均已完成，但用户目标尚未完全达成。"
                "请根据目标补充新的步骤来完成剩余工作。"
            )
        # W2b: the replanner must see (and not reintroduce) failed directions.
        _failed_block = self._failed_directions_block()
        if _failed_block:
            user_prompt += "\n\n" + _failed_block
        # AEWM B1: 新计划不得重建在被证伪的断言上。
        _retracted_block = self._retracted_claims_block()
        if _retracted_block:
            user_prompt += "\n\n" + _retracted_block
        # C2 harness repair (coarse_replan, one-shot): the last stall judged
        # the plan too fine-grained — ask for a coarser plan this time.
        if getattr(self, "_replan_coarse", False):
            self._replan_coarse = False
            user_prompt += (
                "\n\n【harness 修复】上一份计划粒度过细导致步骤级停滞——"
                "本次请生成更粗粒度的计划（不超过 5 步，聚焦阶段目标而非琐碎操作）。"
            )
        llm = self._make_llm()
        replanner_prompt = self.REPLANNER_SYSTEM_PROMPT.replace("{self_eval_hint}", self._self_eval_hint())
        raw = await self._llm_generate(
            llm,
            replanner_prompt,
            user_prompt,
        )

        new_plan = await self._parse_plan_with_repair(
            raw, user_prompt, llm, system_prompt=replanner_prompt,
        )
        if new_plan:
            settled_by_id = {
                str(s.get("id") or ""): s for s in steps
                if isinstance(s, dict) and s.get("status") == "done"
            }
            for new_step in new_plan.get("steps") or []:
                if str(new_step.get("id") or "") in settled_by_id:
                    # Validate the candidate's statement, not its regenerated
                    # pending status. Real text mutations still fail below.
                    new_step["status"] = "done"
            # Validate the LLM candidate first: canonical restoration must never
            # launder a real statement mutation into a preserved done step.
            _violations = _validate_done_steps_preserved(
                steps, new_plan.get("steps") or []
            )
            if _violations:
                logger.warning(
                    "PEVR replan rejected: settled-statement violations: %s",
                    "; ".join(_violations[:3]),
                )
                self._record_event("statement_violation", violations=_violations[:5])
                self._record_failed_direction(
                    f"重规划试图改写已定案步骤: {_violations[0]}",
                    family="statement-violation",
                )
                return None
            new_plan = _canonicalize_single_exact_file_plan(new_plan, self._all_criteria())
            for index, new_step in enumerate(new_plan.get("steps") or []):
                settled = settled_by_id.get(str(new_step.get("id") or ""))
                if settled is not None:
                    # Canonical regeneration must not reset a settled step to
                    # pending. Preserve its complete object after validation.
                    new_plan["steps"][index] = settled
            # Carry over per-step continuity state from the OLD plan so the
            # anti-drift fallback chain (step continuity_brief / output_files /
            # output_summary) survives replanning — otherwise a replan severs
            # the step-brief fallback and the recent-step tail reads.
            old_by_id = {s.get("id"): s for s in steps if s.get("id")}
            for s in new_plan.get("steps") or []:
                old = old_by_id.get(s.get("id"))
                if not old:
                    continue
                for _k in ("continuity_brief", "output_summary", "output_files"):
                    if old.get(_k) and not s.get(_k):
                        s[_k] = old[_k]
            # A4.9 ②：replan 不丢顶层 citation_style——新计划未回显该字段时
            # 沿用旧计划的值（REPLANNER 提示词未强制回显，静默丢失=引用格式
            # 悄悄回落全局默认）。
            if not new_plan.get("citation_style"):
                _old_cs = (self._conv.deathmatch_plan or {}).get("citation_style")
                if _old_cs:
                    new_plan["citation_style"] = _old_cs
            _old_version = int(self._conv.deathmatch_plan_version or 0)
            _fresh_goal = self._fresh_goal_obligations_pending()
            if config.deathmatch_obligation_criteria_enabled:
                # r8（A4.9 #4）：首次成功计划可能是 replan（初始计划解析失败）
                # ——必须同样做执行判定并允许 initial 武装；瞬时判定失败记录事件。
                _need = await _ensure_execution_judged(
                    str(self._conv.deathmatch_goal or "")
                )
                _armed = any(
                    isinstance(c, dict)
                    and (
                        c.get("obligation")
                        or (c.get("check") or {}).get("kind") == "markers"
                    )
                    for c in (
                        getattr(self._conv, "deathmatch_acceptance_criteria", None) or []
                    )
                )
                if _need is None and not _armed:
                    # r9（A4.9 Gap A）：触发判定故障且契约未武装 → 不写计划，
                    # 等下一次 replan 重试（不得永久解武装）。
                    self._record_event(
                        "execution_need_judge_failed", reason="judge_unavailable",
                    )
                    return None
                canonical_judgmental_id = _canonical_judgmental_step_id(self._all_criteria())
                await self._judge_steps_needing_evidence_spec([
                    s for s in (new_plan.get("steps") or [])
                    if str(s.get("id") or "") != canonical_judgmental_id
                ])
            # C1（评审）：replan 换计划对象也必须继承 created_at 出处锚
            # （锚=目标环启动时刻，不随阶段重写漂移）；N3（复审）：存量计划
            # 无锚则在 replan 补锚（新阶段产出自此有出处可查，D-14）。
            if "created_at" not in new_plan:
                new_plan["created_at"] = (
                    (self._conv.deathmatch_plan or {}).get("created_at") or _time.time()
                )
            # A-N1（fix-round）：基线锚跨 replan 存活（key-presence 语义，
            # 合法空基线 [] 也继承；late 标记随名单——豁免禁用不变量保持）。
            for _k in ("baseline_paths", "baseline_truncated", "baseline_captured_late"):
                if _k in (self._conv.deathmatch_plan or {}) and _k not in new_plan:
                    new_plan[_k] = self._conv.deathmatch_plan[_k]
            self._conv.deathmatch_plan = new_plan
            self._conv.deathmatch_plan_version = _old_version + 1
            self._apply_obligations(initial=(_old_version == 0 or _fresh_goal))
            self._consume_fresh_goal_obligations()
            # One UPDATE after all contract mutations: detached replan cannot
            # persist a plan without its obligations or consumed goal epoch.
            from app.services.deathmatch_write_through import write_through_bg
            write_through_bg(
                self._conv,
                _expected_previous_plan_version=_old_version,
                deathmatch_plan=getattr(self._conv, "deathmatch_plan", None),
                deathmatch_plan_version=getattr(self._conv, "deathmatch_plan_version", 0),
                deathmatch_acceptance_criteria=getattr(self._conv, "deathmatch_acceptance_criteria", None),
                deathmatch_grilling_qa_history=getattr(self._conv, "deathmatch_grilling_qa_history", None),
                deathmatch_events=getattr(self._conv, "deathmatch_events", None),
            )
            return new_plan
        return None
