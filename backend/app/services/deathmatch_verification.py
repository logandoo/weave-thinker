# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deathmatch verifier helpers (P5.4 move-only, 2026-10-18)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os as _os
import re as _re
import shlex as _shlex
from datetime import datetime
from typing import Any, Dict, Optional

from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)


_INFO_GATHERING_TOOLS = frozenset({
    "workspace_read", "workspace_glob", "web_search", "browser",
    "browser_navigate", "browser_snapshot", "browser_extract",
    "session_search", "context7", "memory", "notes",
})

_SHORT_VERIFICATION_TOOLS = frozenset({"word_count", "grep"})

GATE_BLOCK_OVERRIDE_AFTER = 2

GATE_BLOCK_STUCK_AFTER = 3


def _receipt_command_segments(command: Any) -> list[list[str]]:
    """Split only on unquoted mandatory &&/; operators, preserving quote state."""
    text = str(command or "")
    raw_segments: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            current.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "|":
            return []
        if ch == "&":
            if i + 1 < len(text) and text[i + 1] == "&":
                raw_segments.append("".join(current))
                current = []
                i += 2
                continue
            return []
        if ch == ";":
            # A semicolon can mask a failed expected command with a later
            # successful echo/read. Only mandatory && chains are accepted.
            return []
        current.append(ch)
        i += 1
    if escaped or quote:
        return []
    raw_segments.append("".join(current))
    segments: list[list[str]] = []
    for raw in raw_segments:
        if not raw.strip():
            continue
        try:
            segments.append(_shlex.split(raw, posix=True))
        except ValueError:
            return []
    return segments


def _receipt_commands_cover(actual: Any, expected_commands: Any) -> bool:
    """Require every expected command, in order, with only cd/echo wrappers."""
    actual_segments = _receipt_command_segments(actual)
    expected: list[list[str]] = []
    for command in expected_commands or []:
        segments = _receipt_command_segments(command)
        if len(segments) != 1:
            return False
        expected.append(segments[0])
    if not actual_segments or not expected:
        return False
    next_expected = 0
    for segment in actual_segments:
        if next_expected < len(expected) and segment == expected[next_expected]:
            next_expected += 1
            continue
        if next_expected >= len(expected):
            break  # additional read/inspection commands after the receipts
        if segment and segment[0] in ("cd", "echo"):
            continue
        # Any other pre-receipt command (including false/test) can short-
        # circuit later commands, so it cannot prove they actually ran.
        return False
    return next_expected == len(expected)


def _declared_output_paths(*a, **k):
    from app.services.deathmatch_service import _declared_output_paths as f
    return f(*a, **k)


def _head_tail_truncate(*a, **k):
    from app.services.deathmatch_service import _head_tail_truncate as f
    return f(*a, **k)


def _is_bible_file(*a, **k):
    from app.services.deathmatch_service import _is_bible_file as f
    return f(*a, **k)


def _is_noise_path(*a, **k):
    from app.services.deathmatch_service import _is_noise_path as f
    return f(*a, **k)


def _normalize_rel_path(*a, **k):
    from app.services.deathmatch_service import _normalize_rel_path as f
    return f(*a, **k)


def _protected_deliverables(*a, **k):
    from app.services.deathmatch_service import _protected_deliverables as f
    return f(*a, **k)


def _read_deliverable_excerpt(*a, **k):
    from app.services.deathmatch_service import _read_deliverable_excerpt as f
    return f(*a, **k)


def _safe_workspace_path(*a, **k):
    from app.services.deathmatch_service import _safe_workspace_path as f
    return f(*a, **k)


def _touch_json(*a, **k):
    from app.services.deathmatch_service import _touch_json as f
    return f(*a, **k)


def _truncate(*a, **k):
    from app.services.deathmatch_service import _truncate as f
    return f(*a, **k)


def critique_visual_artifacts(*a, **k):
    from app.services.deathmatch_service import critique_visual_artifacts as f
    return f(*a, **k)


def _call_judge_llm(*a, **k):
    from app.services.deathmatch_service import _call_judge_llm as f
    return f(*a, **k)


def _ensure_creative_judged(*a, **k):
    from app.services.deathmatch_service import _ensure_creative_judged as f
    return f(*a, **k)


def _format_criteria_block(*a, **k):
    from app.services.deathmatch_plan import _format_criteria_block as f
    return f(*a, **k)


_SPIN_COUNTS: dict[str, int] = {}

_JUDGE_INFRA_FAILURES: dict[str, int] = {}

_JUDGE_INFRA_STALL_THRESHOLD = 2

_HARNESS_REPAIR_COUNTS: dict[str, int] = {}


class DeathmatchVerificationMixin:
    @staticmethod
    def _is_judge_verifier_conflict(stall_reason: str) -> bool:
        """Detect whether this stall was caused by the judge saying 'done'
        while the verifier found unfinished work. In that case the judge's
        reason is suspect (the judge only sees the agent's text, not the
        workspace files — Phantom Action Completion anti-pattern), and the
        verifier's findings are authoritative."""
        return (
            stall_reason.startswith("judge-done but unfinished steps")
            or stall_reason.startswith("judge-done but verifier=")
        )

    def _archive_verifier_evidence_units(self, result: Dict[str, Any]) -> None:
        """W2 D1：verifier 链归档点（verify_step_outputs 结束时）——
        issues → FAIL 单元、completed → FACT 单元。归因只认已校验来源声明
        （本链验证的当前步骤）；无当前步骤 = 歧义 → 未归因。"""
        try:
            step_id = str(result.get("current_step") or "")
        except Exception:
            step_id = ""
        issues = [str(i) for i in (result.get("issues") or []) if str(i).strip()]
        if issues:
            self._record_evidence_unit(
                "FAIL", "verifier",
                "issues: " + "; ".join(issues),
                step_id=step_id,
            )
        completed = [str(c) for c in (result.get("completed_steps") or []) if str(c).strip()]
        if completed:
            self._record_evidence_unit(
                "FACT", "verifier",
                "completed: " + ", ".join(completed),
                step_id=step_id,
            )

    async def _run_verification_gate(self, workspace_path: str, current_step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """A1b + W1b: deterministic verification gate — a step's
        verification_method may start with ``gate: <shell command>`` (legacy)
        or carry a done_check {mode: gate}. The command runs inside the
        code-execution sandbox (cwd = workspace) BEFORE the LLM verifier.
        Exit 0 → pass (returns None); non-zero → the gate output becomes
        the issue (partial, short-circuits the LLM call). Opt-in via config."""
        if not config.deathmatch_verify_command_gate_enabled:
            return None
        method = str(current_step.get("verification_method") or "").strip()
        command = ""
        if method.startswith("gate:"):
            command = method[len("gate:"):].strip()
        else:
            check = current_step.get("done_check") or {}
            if isinstance(check, dict) and str(check.get("mode")) == "gate":
                command = str(check.get("cmd") or "")
        if not command or len(command) > 500:
            return None
        issues = await self._run_gate_command(command, workspace_path)
        if not issues:
            logger.info(
                "deathmatch verification gate PASSED for step %s (conv %s)",
                current_step.get("id"), self._conv.id,
            )
            return None
        issue = issues[0]
        logger.info(
            "deathmatch verification gate BLOCKED step %s: %s",
            current_step.get("id"), issue[:200],
        )
        return {
            "status": "partial",
            "issues": [issue],
            "retry_instruction": f"修复后重跑验证命令：{command[:200]}",
        }

    async def verify_step_outputs(
        self,
        last_response: str,
        workspace_path: str,
        tool_results: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Independent verifier: LLM-based step completion assessment.

        The verifier evaluates the current step using an LLM that considers
        the agent's response, the workspace file snapshot, prior step outputs,
        AND actual content snippets from previously generated files. Only the
        LLM verifier can mark a step as done — no heuristic shortcuts.
        """
        if workspace_path:
            self._workspace_path = workspace_path
        files = self._workspace_file_snapshot(workspace_path)
        plan = self._conv.deathmatch_plan or {"steps": []}
        steps = plan.get("steps") or []

        issues: List[str] = []
        completed: List[str] = []

        # Find the step currently being worked on (in_progress or first pending).
        current_step = None
        for s in steps:
            if s.get("status") == "in_progress":
                current_step = s
                break
        if current_step is None:
            current_step = self._get_next_pending_step()
            if current_step is not None:
                # R2 兜底重拾：gate_blocks 清零在 _set_step_status 统一处理
                # （I1/B-1 评审：prompt/verify 两个拾起点同权）。
                self._set_step_status(current_step, "in_progress", "verify_pick")

        # Collect already-completed step IDs.
        for s in steps:
            if s.get("status") == "done" and s.get("id"):
                completed.append(s.get("id"))

        result: Dict[str, Any] = {
            "status": "partial",
            "completed_steps": [c for c in completed if c],
            "issues": issues,
            "retry_instruction": "",
            "confidence": 0.5,
            "workspace_files": files,
            "current_step": current_step.get("id") if current_step else None,
            "continuity_brief": "",
            "integrity": "clean",  # G3: flips to "violation" on protected-deletion
        }

        # Baseline for progress/new-file detection: the previous verification's
        # snapshot. Computed up-front so the verifier can see THIS turn's
        # new/changed files and the progress detector below can reuse it.
        prev_result = self._conv.deathmatch_last_verification_result or {}
        if not prev_result:
            # A4.9 r4: the FIRST verification of a goal loop is a BASELINE
            # round — capture the workspace snapshot without judging, so
            # legacy files from previous goals / uploads do not count as
            # this-turn evidence on turn 1 (user workspaces are persistent).
            # Turn 2 onward, prev_files contains the baseline and only real
            # mtime changes satisfy the evidence gate.
            # Story bible: write the creative-task spec files on this
            # baseline round (goal-loop start) so the agent works against a
            # file-based spec from turn 2 on.
            try:
                await self._ensure_bible_files(workspace_path)
            except Exception as exc:
                logger.warning("deathmatch bible ensure failed (non-blocking): %s", exc)
            logger.info(
                "PEVR verifier: baseline round for conv=%s (no previous snapshot) — "
                "capturing workspace, %d files",
                self._conv.id, len(files),
            )
            baseline = {
                "status": "partial",
                "completed_steps": [],
                "issues": ["基线轮：捕获工作区快照，下一轮开始验证产出"],
                "retry_instruction": "",
                "confidence": 0.5,
                "workspace_files": files,
                "current_step": current_step.get("id") if current_step else None,
                "continuity_brief": "",
                # Contract keys expected by downstream consumers (progress
                # detection / spin detection / stall handling) — the baseline
                # must behave like a first-round partial (A4.9 r5).
                "progress": True,
                "spin_detected": False,
                "no_content_turns": 1 if not (last_response or "").strip() else 0,
                "tool_result_hashes": {},
                "integrity": "clean",  # G3 contract symmetry (A4.9 R3 Minor-1)
            }
            self._conv.deathmatch_last_verification_result = baseline
            # A-N1（fix-round C1/C2）：兜底捕获 = 全量 os.walk + late 标记。
            # 计划创建时已捕获（含合法空基线 []）则不覆盖；late 名单**不启用
            # 豁免**（只记录观测——计划创建时未捕获=未知口径，严格 mtime）。
            if isinstance(plan, dict) and "baseline_paths" not in plan:
                _late = self._capture_baseline_paths(workspace_path)
                if _late is not None:
                    plan["baseline_paths"] = _late[: self._BASELINE_PATHS_CAP]
                    if len(_late) > self._BASELINE_PATHS_CAP:
                        plan["baseline_truncated"] = True
                    plan["baseline_captured_late"] = True
                    _touch_json(self._conv, "deathmatch_plan")
            return baseline
        prev_files: Dict[str, int] = {}
        try:
            for pf in (prev_result.get("workspace_files") or []):
                prev_files[str(pf.get("path"))] = int(pf.get("mtime") or 0)
        except Exception:
            prev_files = {}
        prev_completed = set(prev_result.get("completed_steps") or [])

        def _is_noise_path(p: str) -> bool:
            return (
                "__pycache__" in p
                or p.startswith("tool_results/")
                or "/tool_results/" in p
                # Bible spec files are SETTINGS, not deliverables — excluded
                # from evidence/progress. Precise match on the 5 generated
                # file names so a REAL deliverable inside a directory named
                # "bible" is never falsely excluded (A4.9 review d/b).
                or _is_bible_file(p)
            )

        # Files new or changed since the previous verification = THIS turn's
        # actual output. The verifier MUST see their content (head+tail) —
        # otherwise it can only trust the agent's narration about what was
        # written (Phantom Action Completion / drift blindness).
        new_files: List[Dict[str, Any]] = []
        for f in files:
            p = str(f.get("path") or "")
            if _is_noise_path(p):
                continue
            m = int(f.get("mtime") or 0)
            if p not in prev_files or m > int(prev_files.get(p) or 0):
                new_files.append(f)

        # G3 (LongHorizon-Harness 2608.01964 integrity axis): deliverables of
        # already-completed steps are protected artifacts. One vanishing from
        # the workspace since the previous verification is an integrity
        # violation — the round can never certify completion on top of it.
        # Only protected (recorded) deliverables are watched, so legitimate
        # temp-file cleanup never false-fires; a delete+rewrite inside one
        # turn shows up as an mtime change, not a deletion.
        # Membership in the previous snapshot proves "was there"; the current
        # check goes straight to the filesystem (A4.9 M1: the 400-file mtime
        # window could evict an old deliverable and read as a deletion).
        deleted_protected = sorted(
            p for p in _protected_deliverables(steps)
            if p in prev_files
            and not _os.path.exists(_os.path.join(workspace_path, p))
        )
        integrity_issue = (
            "完整性违规：已完成步骤的交付物已从工作区消失："
            + ", ".join(deleted_protected[:5])
            + " —— 需先恢复或重新产出，目标不得据此判完成"
        ) if deleted_protected else ""

        if deleted_protected:
            # Bookkeeping lands IMMEDIATELY (A4.9 R2 new-1): the A1b gate
            # short-circuit early-returns below — a gate-failing round must
            # still carry the violation, reopen the owning steps and strip
            # them from completed_steps, or the redirect is silently dropped.
            result["integrity"] = "violation"
            result["issues"] = list(result.get("issues") or []) + [integrity_issue]
            # A4.9 I1: deterministically re-open the steps that own the
            # vanished deliverables — the plan itself redirects the loop to
            # re-produce them (plan order) instead of relying on the agent
            # noticing the issue text. "pending" preserves the single
            # in_progress invariant: the current step settles first, then
            # the violated steps are re-picked in order.
            reopened: List[str] = []
            for s in steps:
                if s.get("status") == "done" and any(
                    p in {str(x) for x in (s.get("output_files") or [])}
                    for p in deleted_protected
                ):
                    self._set_step_status(s, "pending", "integrity_reopen")
                    # M2（评审）：重开 = 新一轮尝试周期，计数/卡住标记清零
                    s["gate_blocks"] = 0
                    s.pop("stuck", None)
                    if s.get("id"):
                        reopened.append(str(s.get("id")))
            if reopened:
                _reopened = set(reopened)
                result["completed_steps"] = [
                    c for c in (result.get("completed_steps") or [])
                    if c not in _reopened
                ]
                result["issues"] = list(result.get("issues") or []) + [
                    "已重开受影响步骤：" + ", ".join(reopened[:5])
                ]
                # AEWM B1：重开=其完成/交付声称被证伪，撤回以防污染后续判定。
                self._retract_reopened_step_claims(
                    reopened, deleted_protected, integrity_issue,
                )

        # LLM verification: evaluate the current step's completion.
        # This is the ONLY mechanism that can mark a step as done.
        llm_status: Optional[str] = None
        if config.deathmatch_verify_enabled and current_step:
            # A1b: deterministic gate short-circuits the LLM verifier.
            gate_result = await self._run_verification_gate(workspace_path, current_step)
            if gate_result is not None:
                result["issues"] = list(result["issues"]) + gate_result.get("issues", [])
                result["retry_instruction"] = gate_result.get("retry_instruction", "")
                result["status"] = "partial"
                return result
            try:
                llm = self._make_llm(
                    model_override=config.deathmatch_verify_model or ""
                )
                files_desc = self._format_workspace_listing(
                    files, total=getattr(self, "_last_snapshot_total", len(files)))
                prior_outputs = ""
                for s in steps:
                    if s.get("status") == "done" and s.get("id") != current_step.get("id"):
                        prior_outputs += (
                            f"步骤 {s.get('id')}: "
                            f"{_truncate(str(s.get('output_summary') or ''), 200)}\n"
                        )
                prior_file_snippets = self._read_prior_file_snippets(
                    steps, workspace_path, new_files=new_files
                )
                # 2026-09-20 placeholder gate: deterministic marker scan of THIS
                # step's expected outputs so the verifier can flag design-only
                # scaffolds ("受限估计：待执行") instead of accepting them.
                # A4.9 r2 Important-1: `output_files` is only written back at
                # step COMPLETION, so on a step's first verification pass the
                # declared outputs may be empty — always include this turn's
                # new/changed files (new_files), or the scan would be inert
                # exactly when it matters.
                _cur_outputs = [
                    str(x) for x in (
                        current_step.get("output_files")
                        or current_step.get("writes")
                        or []
                    )
                ]
                for _f in (new_files or []):
                    _p = str(_f.get("path") or "").strip()
                    if _p and _p not in _cur_outputs:
                        _cur_outputs.append(_p)
                # r7: 评审者需要看到交付物**内容**（而非仅文件名/片段）才能对
                # 未执行占位作语义判断——注入有界全文摘录（agentic 判定输入）。
                _deliverable_excerpt = _read_deliverable_excerpt(
                    workspace_path, _cur_outputs
                )
                _step_json = json.dumps(current_step, ensure_ascii=False)
                if len(_step_json) > 1000:
                    # 2026-09-20 审计完整性：静默 [:1000] 会砍掉步骤契约后段
                    # （expected_output/output_files 位于 JSON key 顺序后部）。
                    _step_json = _step_json[:1000] + "\n…[步骤 JSON 截断]…"
                # W2 D1（ACG）：需求锚定证据单元块注入 verifier 用户提示词
                # （块级预算准入；B2 对侧链种子交叉注入；开关默认关）。
                _units_block = self._build_evidence_units_block(
                    "verifier", str(current_step.get("id") or "")
                )
                user_prompt = (
                    f"目标:\n{_truncate(self._conv.deathmatch_goal or '', 800)}\n\n"
                    + (
                        f"{_format_criteria_block(self._all_criteria())}\n\n"
                        if _format_criteria_block(self._all_criteria()) else ""
                    )
                    + f"当前正在执行的步骤:\n{_step_json}\n\n"
                    # 步骤契约必须显式在场（不依赖 JSON dump 的 key 顺序/长度）：
                    # expected_output 是 verifier 判 complete 的第一依据。
                    + (
                        f"当前步骤预期产出（expected_output）:\n"
                        f"{_truncate(str(current_step.get('expected_output') or ''), 400)}\n\n"
                        if current_step.get("expected_output") else ""
                    )
                    # A4.9 M2: boundary 位于 JSON key 顺序后部，显式呈现，
                    # 保证检查项 9 永不静默失效。
                    + (
                        f"当前步骤边界约束（boundary）:\n"
                        f"{_truncate(str(current_step.get('boundary') or ''), 300)}\n\n"
                        if current_step.get("boundary") else ""
                    )
                    + f"此前已完成步骤的产出:\n{prior_outputs or '(无)'}\n\n"
                    f"文件内容片段（前序步骤产物 + 本轮新产出/变更文件，均为开头+结尾）:\n{prior_file_snippets}\n\n"
                    + (f"{_deliverable_excerpt}\n\n" if _deliverable_excerpt else "")
                    + f"Agent最近回复:\n{_head_tail_truncate(last_response, 2000)}\n\n"
                    f"workspace文件快照:\n{files_desc}\n\n"
                    + (
                        f"<bible>\n{self._build_bible_context_block()}\n</bible>\n\n"
                        if await _ensure_creative_judged(self._conv.deathmatch_goal or "")
                        else ""
                    )
                    + (
                        f"{self._build_settled_block()}\n\n"
                        if self._build_settled_block()
                        else ""
                    )
                    + (
                        f"{self._retracted_claims_block()}\n\n"
                        if self._retracted_claims_block()
                        else ""
                    )
                    + (
                        f"{_units_block}\n\n"
                        if _units_block
                        else ""
                    )
                    + f"请评估当前步骤是否已完成：\n"
                    f"1) 当前步骤产出是否满足预期（内容完整性、质量）——重点检查'本轮新产出文件的内容片段'是否与步骤要求相符\n"
                    f"2) 当前步骤产出与此前步骤产出是否衔接一致（无矛盾、无断裂）\n"
                    f"   重点：对比'本轮新产出文件的内容片段'与'前序文件内容片段'——风格、设定、情节、人物必须一致\n"
                    f"3) 是否有重复或冗余内容（当前步骤是否重复了已有的章节/段落）\n"
                    f"4) 如果步骤要求生成文件，文件是否实际存在（在下方按目录分组的完整快照中查找，不要漏看子目录）\n"
                    f"5) 产出中声称的实测/基准测试数据是否真实可执行——若该测试在本环境客观上无法执行，"
                    f"标记 blocked 并指出数据系编造\n"
                    f"6) 内容偏离检查：本轮新产出是否偏离目标要求（风格/人物/设定/情节/格式/事实）？"
                    f"若有偏离，标记 partial 并在 issues 中指出偏离点\n"
                    f"7) 最后输出 requires_file：当前步骤的预期产出是否要求生成实际文件"
                    f"（创作/文档/报告/导出步骤=true，纯信息分析判断步骤=false）\n"
                    f"8) spec 保真三元组（mattpocock code-review 移植）：对每个步骤给出结论前，"
                    f"对照该步骤的预期原文逐条判定——requirements missing/partial（要求缺失或只完成一部分）、"
                    f"scope creep（产出超出步骤要求范围）、looks-implemented-but-wrong"
                    f"（看起来完成了实际不符合——表面有产出但内容答非所问）。"
                    f"命中任何一类时，在 issues 中引用被违反的步骤预期原文。\n"
                    f"9) 边界完整性：若当前步骤声明了 boundary 边界约束，检查本轮产出/变更是否违反"
                    f"（越界修改、覆盖或引用被禁止的对象）。违反→标记 partial，"
                    f"并在 issues 中引用被违反的边界原文。\n"
                    f"10) 未执行占位检查（agentic）：若上方『交付物内容摘录』或『未执行占位』"
                    f"判定显示本步骤产出以留白（待执行/受限估计/待填报/不填报数值）"
                    f"代替实际结果，而步骤预期要求实际结果，"
                    f"不得判 complete——判 partial 并在 issues/diagnosis 中列出未执行交付项。\n"
                    f"只有当步骤产出确实满足预期时才标记为 complete。"
                )
                # A5: MoA aggregation path (optional) — multiple reference
                # models weigh the verdict, reducing single-model bias at
                # extra cost; falls back to the single-model path on error
                # OR when the aggregate does not honor the verifier JSON
                # schema (I4: the generic fusion aggregator may produce
                # prose — a dict without "status" must not silently degrade
                # to perpetual partial).
                if config.deathmatch_verify_moa_enabled:
                    try:
                        from app.services.moa_service import MoAService
                        moa = MoAService()
                        moa_resp = await moa.run_moa(
                            prompt=(
                                f"{self.VERIFIER_SYSTEM_PROMPT}\n\n{user_prompt}\n\n"
                                "（聚合输出必须保持 verifier 的 JSON 结构："
                                '{"status": "complete|partial|blocked", "issues": [...], '
                                '"retry_instruction": "...", "confidence": 0.8, '
                                '"continuity_brief": "..."}）'
                            ),
                            context="",
                            timeout_seconds=60.0,
                        )
                        raw = moa_resp.aggregated_response or ""
                        if not raw.strip():
                            raise RuntimeError("MoA returned empty aggregate")
                        _parsed_probe = self._parse_json_object(raw)
                        if not isinstance(_parsed_probe, dict) or "status" not in _parsed_probe:
                            raise RuntimeError("MoA aggregate lost the verifier schema")
                    except Exception as exc:
                        logger.warning("MoA verifier failed (%s) — single-model fallback", exc)
                        raw = await self._llm_generate(
                            llm, self.VERIFIER_SYSTEM_PROMPT, user_prompt, temperature=0
                        )
                else:
                    raw = await self._llm_generate(
                        llm, self.VERIFIER_SYSTEM_PROMPT, user_prompt, temperature=0
                    )
                parsed = self._parse_json_object(raw)
                if parsed:
                    from app.prompts.contracts import VerifierVerdict, parse_contract

                    _vv, _vv_failure = parse_contract(
                        VerifierVerdict, json.dumps(parsed, ensure_ascii=False))
                    if _vv_failure is not None:
                        logger.warning("verifier contract failed: %s", _vv_failure.kind)
                    llm_status = parsed.get("status", "partial")
                    # Merge, don't replace: pre-LLM issues (G3 integrity
                    # violation landed at detection time) must survive the
                    # LLM's issue list.
                    result["issues"] = list(result.get("issues") or []) + list(parsed.get("issues") or [])
                    result["retry_instruction"] = str(parsed.get("retry_instruction", ""))
                    result["confidence"] = float(parsed.get("confidence", 0.5))
                    # Continuity anchor: the verifier distills what the NEXT
                    # step must keep consistent. Persisted with the result and
                    # injected into the next continuation prompt (survives
                    # context compression because it is re-read from the DB).
                    result["continuity_brief"] = _truncate(str(
                        parsed.get("continuity_brief") or ""
                    ).strip(), 800)
                    # W2a: the verifier's falsifiable diagnosis for this round
                    # (feeds the node-recovery ladder / failed directions).
                    result["diagnosis"] = _truncate(str(parsed.get("diagnosis") or "").strip(), 300)

                    # Only mark the current step as done when the LLM verifier
                    # explicitly says "complete". Do NOT use heuristic shortcuts.
                    if llm_status == "complete" and current_step.get("status") != "done":
                        # A1a evidence gate (default-FAIL hardening): an
                        # output-type step must hold a real non-empty artifact
                        # produced THIS turn (new_files by mtime since the
                        # last verification), excluding noise paths and the
                        # agent's own PROGRESS.md handoff file. The LLM
                        # "complete" verdict alone is not evidence — weak
                        # models hallucinate completion and the agent can
                        # claim victory without artifacts (conv 2fa87be4
                        # phantom-completion class). Full-workspace snapshots
                        # are bypassable (legacy files / PROGRESS.md itself
                        # would always pass — A4.9 Critical 1).
                        # Whether the step requires a file artifact is the
                        # verifier LLM's ``requires_file`` judgment (agentic
                        # principle); absent field → True (fail-secure).
                        try:
                            _requires_file = bool(parsed.get("requires_file", True))
                        except Exception:
                            _requires_file = True
                        _declared = _declared_output_paths(current_step)
                        _declared_norm = {
                            _normalize_rel_path(_safe_workspace_path(p))
                            for p in _declared if _safe_workspace_path(p)
                        }
                        _evidence_files = [
                            f for f in new_files
                            if f.get("size", 0) > 0
                            and not _is_noise_path(str(f.get("path") or ""))
                            and str(f.get("path") or "").lower() != "progress.md"
                            and not str(f.get("path") or "").endswith("/progress.md")
                        ]
                        if _declared:
                            # An unrelated non-empty file is never evidence for
                            # this step. Declared outputs are checked as a set
                            # below, so one of many required files is not enough.
                            _evidence_files = [
                                f for f in _evidence_files
                                if _normalize_rel_path(str(f.get("path") or "")) in _declared_norm
                            ]
                        # R1（诊断机制 A）：证据闸三车道分诊——verifier 判
                        # complete 但当轮无新文件时不再一律拦截（早轮产出/
                        # 合成型步骤会被永久卡死并垄断步骤机）。
                        # C1（评审）：lane 1 记账 = 声明产出【全部】在场且
                        # >0B 且有出处（mtime ≥ 目标环计划创建时刻——同步骤
                        # 早轮产出过闸；目标启动前的同名遗留文件不过闸，封
                        # phantom-completion 旁路 A4.9 Critical 1 同类）。
                        _plan_created_at = 0.0
                        try:
                            _plan_created_at = float((plan or {}).get("created_at") or 0.0)
                        except Exception:
                            _plan_created_at = 0.0
                        _baseline_known = (
                            "baseline_paths" in (plan or {})
                            and not (plan or {}).get("baseline_truncated")
                            and not (plan or {}).get("baseline_captured_late")
                        )
                        _baseline_set = set(
                            _normalize_rel_path(x)
                            for x in ((plan or {}).get("baseline_paths") or [])
                        )
                        # I4（评审）：存在性/大小/mtime 走文件系统直查——工作区
                        # 快照 snap[:400] 截断会把窗口外的声明产出误判缺失
                        # （deleted_protected 同类修复 A4.9 M1）。ALL-or-nothing：
                        # 声明产出必须全部在场且有出处（同步骤早轮产出）。
                        _existing_artifact: List[str] = []
                        if _declared:
                            for _p in _declared:
                                # N1（复审）：声明路径必须过 _safe_workspace_path
                                # 包含性守卫（拒绝对路径/..）——os.path.join 对
                                # 绝对路径会直接落到工作区外。
                                _p = _safe_workspace_path(_p)
                                if not _p:
                                    break
                                # A-N1（fix-round）：比较前归一化（`./x` ≡ `x`——
                                # 声明 ./deliverable.md 必须命中基线成员
                                # deliverable.md，形态洞不得骗豁免）。
                                _p = _normalize_rel_path(_p)
                                try:
                                    _st = _os.stat(_os.path.join(workspace_path, _p))
                                except Exception:
                                    break
                                if (
                                    _st.st_size > 0
                                    and not _is_noise_path(_p)
                                    and _p.lower() != "progress.md"
                                    and not _p.lower().endswith("/progress.md")
                                    # C1 出处：mtime ≥ 目标环计划创建时刻（同步骤
                                    # 早轮产出过闸，目标启动前的遗留文件不过闸）。
                                    # N3（复审）：存量计划无 created_at 锚（锚为
                                    # 本次修复引入）——过渡口径放宽为不要求出处
                                    # （ALL-match 仍全量），下次 replan/local_patch
                                    # 会补锚（D-14）。
                                    and (
                                        _plan_created_at <= 0
                                        or int(_st.st_mtime) >= int(_plan_created_at)
                                        # A-N1（fix-round C1/C2）：豁免收窄——
                                        # 「名单已捕获（含合法空基线 []）且未
                                        # truncated 且非 late 且归一化后非成员」
                                        # 才可豁免 mtime（目标环内保 mtime 产出
                                        # 不误拒；遗留/未知/截断名单一律严格）。
                                        or (_baseline_known and _p not in _baseline_set)
                                    )
                                ):
                                    _existing_artifact.append(_p)
                                else:
                                    break
                        _lane1_ok = bool(_declared) and len(_existing_artifact) == len(_declared)
                        _gate_blocks = int(current_step.get("gate_blocks") or 0)
                        _credit: Optional[str] = None  # 完成记账理由（None=不放行）
                        _settle = True  # 放行是否入 settled 台账
                        _current_es = current_step.get("evidence_spec") if isinstance(current_step.get("evidence_spec"), dict) else {}
                        _tool_output_required = (
                            str(_current_es.get("kind") or "") == "tool_output"
                            and bool(_current_es.get("must_run", True))
                        )
                        _receipt_cmds = (_current_es.get("receipt") or {}).get("cmds") or []
                        def _receipt_tool_match(tr: Any) -> bool:
                            if str(getattr(tr, "name", "") or "") != "terminal":
                                return False
                            if bool(getattr(tr, "error", False)):
                                return False
                            if not str(getattr(tr, "result", "") or "").strip():
                                return False
                            try:
                                receipt_payload = json.loads(str(getattr(tr, "result", "")))
                            except (TypeError, ValueError, json.JSONDecodeError):
                                return False
                            if not isinstance(receipt_payload, dict):
                                return False
                            return_code = receipt_payload.get("return_code")
                            if type(return_code) is not int or return_code != 0:
                                return False
                            args = getattr(tr, "arguments", {}) or {}
                            return _receipt_commands_cover(
                                args.get("command"), _receipt_cmds,
                            )
                        _tool_output_ok = not _tool_output_required or (
                            bool(_receipt_cmds) and any(_receipt_tool_match(tr) for tr in (tool_results or []))
                        )
                        if deleted_protected:
                            # G3 integrity gate: a completion claim can never
                            # stand while a protected deliverable is missing.
                            # (The issue itself was appended at detection time.)
                            self._set_step_status(current_step, "in_progress", "integrity_gate_block")
                            logger.info(
                                "PEVR integrity gate blocked complete for step %s "
                                "(deleted protected deliverables: %s)",
                                current_step.get("id"), deleted_protected[:3],
                            )
                        elif _tool_output_required and not _tool_output_ok:
                            # tool_output is not a receipt-less bypass: a real
                            # read/compute result must exist in THIS turn.
                            result["issues"] = list(result["issues"]) + [
                                "tool_output 证据缺失：需当轮真实工具（terminal）回读/计算；"
                                "合并命令时按 receipt.cmds 顺序使用 && 精确执行，"
                                "不要用分号、||、管道或 echo 冒充（分号会掩盖前序失败），"
                                "且结果必须含整数 return_code=0；请按此形式重试"
                            ]
                            self._set_step_status(current_step, "in_progress", "tool_output_evidence_block")
                            self._record_event(
                                "tool_output_evidence_block",
                                step_id=str(current_step.get("id") or ""),
                            )
                        elif (_evidence_files and (not _declared or _lane1_ok)) or (not _requires_file and not _declared):
                            _credit = "verifier_complete"
                        elif _lane1_ok:
                            # lane 1（精确化）：声明产出全部在场（同步骤早轮
                            # 产出，本轮无新建）——记账放行 + 留痕。
                            _credit = "existing_artifact"
                        elif not _declared and (
                            not current_step.get("evidence_spec")
                            or str((current_step.get("evidence_spec") or {}).get("kind") or "") == "tool_output"
                        ):
                            # lane 2（合成/分析/tool-output 步骤：无文件义务）——
                            # 连拦 GATE_BLOCK_OVERRIDE_AFTER 次后放行需记录
                            #（软完成，不入 settled——新证据可再审）。
                            _gate_blocks += 1
                            current_step["gate_blocks"] = _gate_blocks
                            _touch_json(self._conv, "deathmatch_plan")
                            # fix-round I2：自增写-through（同态 _set_step_status
                            # 早退不带写-through——自增不得只靠 merge 通道）。
                            from app.services.deathmatch_write_through import write_through_bg
                            write_through_bg(
                                self._conv,
                                deathmatch_plan=getattr(self._conv, "deathmatch_plan", None),
                                deathmatch_plan_version=getattr(self._conv, "deathmatch_plan_version", 0),
                            )
                            self._record_event(
                                "evidence_gate_block",
                                step_id=str(current_step.get("id") or ""),
                                gate_blocks=_gate_blocks, lane="synthetic",
                            )
                            if _gate_blocks >= GATE_BLOCK_OVERRIDE_AFTER:
                                _credit = "evidence_gate_override"
                                _settle = False
                            else:
                                result["issues"] = list(result["issues"]) + [
                                    f"证据门拦截（合成步重试 {_gate_blocks}/{GATE_BLOCK_OVERRIDE_AFTER}）："
                                    f"步骤 {current_step.get('id', '?')} 判定完成但本轮无新产出"
                                ]
                                self._set_step_status(current_step, "in_progress", "evidence_gate_block")
                        else:
                            # lane 3（critical：声明交付物/evidence_spec 持续
                            # 缺失）——重试至 GATE_BLOCK_STUCK_AFTER，随后 R2
                            # 降级 pending+stuck 并前移步骤机。
                            _gate_blocks += 1
                            current_step["gate_blocks"] = _gate_blocks
                            _touch_json(self._conv, "deathmatch_plan")
                            # fix-round I2：自增写-through（同态 _set_step_status
                            # 早退不带写-through——自增不得只靠 merge 通道）。
                            from app.services.deathmatch_write_through import write_through_bg
                            write_through_bg(
                                self._conv,
                                deathmatch_plan=getattr(self._conv, "deathmatch_plan", None),
                                deathmatch_plan_version=getattr(self._conv, "deathmatch_plan_version", 0),
                            )
                            self._record_event(
                                "evidence_gate_block",
                                step_id=str(current_step.get("id") or ""),
                                gate_blocks=_gate_blocks, lane="critical",
                            )
                            if _gate_blocks >= GATE_BLOCK_STUCK_AFTER:
                                current_step["stuck"] = True
                                self._set_step_status(current_step, "pending", "stuck_bypass")
                                self._record_event(
                                    "step_stuck_bypass",
                                    step_id=str(current_step.get("id") or ""),
                                    gate_blocks=_gate_blocks,
                                )
                                result["issues"] = list(result["issues"]) + [
                                    f"步骤 {current_step.get('id', '?')} 连续 {_gate_blocks} 次未产出声明交付物"
                                    f"（{', '.join(_declared[:3])}）——已标记 stuck 并前移后续步骤，稍后重试"
                                ]
                            else:
                                result["issues"] = list(result["issues"]) + [
                                    f"证据门拦截：步骤 {current_step.get('id', '?')} 声明完成但"
                                    f"声明交付物（{', '.join(_declared[:3]) or '产出文件'}）缺失或早于本目标启动（过期）— 需产出实际文件"
                                ]
                                self._set_step_status(current_step, "in_progress", "evidence_gate_block")
                            logger.info(
                                "PEVR evidence gate blocked complete for step %s "
                                "(declared outputs missing: %s)",
                                current_step.get("id"), _declared[:3],
                            )
                        if _credit:
                            self._set_step_status(current_step, "done", _credit)
                            # M5（评审）：成功记账不写 result["issues"]（该通道
                            # 是失败诊断输入，被 stall/续跑当问题消费）——留痕
                            # 只走 deathmatch_events 结构化事件。
                            if _credit == "existing_artifact":
                                self._record_event(
                                    "step_done_on_existing_artifact",
                                    step_id=str(current_step.get("id") or ""),
                                    files=_existing_artifact[:5],
                                )
                            elif _credit == "evidence_gate_override":
                                self._record_event(
                                    "evidence_gate_override",
                                    step_id=str(current_step.get("id") or ""),
                                    gate_blocks=_gate_blocks,
                                )
                            if _settle:
                                # P1-5: step completion is a settled verdict.
                                self._record_settled({
                                    "type": "step_complete",
                                    "summary": (
                                        f"步骤 {current_step.get('id')} 完成："
                                        f"{str(current_step.get('description') or '')[:80]}"
                                    ),
                                    # W1c: freeze the statement + hash of the
                                    # settled step (replan must preserve it).
                                    "step": dict(current_step),
                                })
                            # Bible evolution: extract canon facts from this
                            # completed step (creative goals) — background
                            # task so the verify latency stays inside the
                            # judge budget (I5: a synchronous 120s evolution
                            # LLM call on top of MoA would exceed the 300s
                            # outer wait_for and cancel the verdict).
                            try:
                                asyncio.create_task(self._evolve_bible(
                                    workspace_path,
                                    current_step.get("id", "?"),
                                    last_response,
                                ))
                            except Exception as exc:
                                logger.warning("bible evolution spawn error: %s", exc)
                            # Prefer the distilled brief over a raw 300-char
                            # truncation of the agent's commentary — for file-writing
                            # turns the response is narration, not content.
                            _brief = result.get("continuity_brief") or ""
                            current_step["output_summary"] = (
                                _truncate(_brief, 300) if _brief else _truncate(last_response, 300)
                            )
                            if _brief:
                                current_step["continuity_brief"] = _truncate(_brief, 400)
                            # Collect output files from workspace snapshot.
                            # C1（评审）：声明清单是契约——不得覆写/重排；仅在
                            # 步骤从未声明时按实存回填。
                            if _evidence_files and not current_step.get("output_files"):
                                current_step["output_files"] = [
                                    f["path"] for f in _evidence_files[:10]
                                ]
                            if current_step.get("id"):
                                completed.append(current_step["id"])
                                result["completed_steps"] = list(set(
                                    result["completed_steps"] + [current_step["id"]]
                                ))
                    elif llm_status != "complete":
                        # Verifier says not complete — ensure step stays in_progress.
                        # M2（评审）：gate_blocks 记「连续被拦的 complete 申领」，
                        # 非 complete 轮打断连击（否则累计计数会提前放行）。
                        if current_step.get("gate_blocks"):
                            current_step["gate_blocks"] = 0
                            _touch_json(self._conv, "deathmatch_plan")
                        if current_step.get("status") != "done":
                            self._set_step_status(current_step, "in_progress", "verifier_partial")
            except Exception as exc:
                logger.warning("PEVR verifier LLM failed: %s", exc)

        # If all steps done → complete. Otherwise propagate the verifier's
        # blocked verdict (e.g. fabricated-data detection) so the stall
        # handler can fire; fall back to partial for normal in-progress.
        pending = [s for s in steps if s.get("status") != "done"]
        if not pending and steps:
            result["status"] = "complete"
        elif llm_status == "blocked":
            result["status"] = "blocked"
        else:
            result["status"] = "partial"

        # G3 integrity override: a vanished protected deliverable downgrades
        # even an all-steps-done round — completion is never certified on a
        # violated workspace. (Bookkeeping — issue / reopen / completed-steps
        # strip — landed right after detection above, so the A1b gate
        # short-circuit cannot swallow it; this is the status downgrade only.)
        if deleted_protected and result["status"] == "complete":
            result["status"] = "partial"
            result["retry_instruction"] = (
                result.get("retry_instruction") or "恢复或重新产出消失的交付物后重新交付"
            )

        # Progress detection: compare against the previous verification
        # result so that repeated partial rounds with no new files and no
        # newly completed steps can be treated as a stall upstream.
        progress = False
        if not prev_result:
            progress = True
        elif set(result["completed_steps"]) - prev_completed:
            progress = True
        else:
            for f in files:
                p = str(f.get("path") or "")
                if _is_noise_path(p):
                    continue
                m = int(f.get("mtime") or 0)
                if p not in prev_files or m > int(prev_files.get(p) or 0):
                    progress = True
                    break

        # Information-gathering tool output also counts as progress: reading
        # files / searching / browsing yields NEW information this turn even
        # though no file was written yet. This prevents read-only research
        # turns (e.g. the agent reading evidence logs for several turns in
        # conv 51d74833) from being misclassified as stalls.
        #
        # Execution tools (terminal / execute_code / ...) count as progress
        # ONLY when a result is novel — never seen in a previous evaluation
        # (sha1 of name+result). Repeated polling loops (curl the same status
        # endpoint, identical output) produce identical hashes and still
        # escalate; distinct exploration commands (different arguments, new
        # output) are genuine progress. This closes the gap where an agent
        # doing legitimate setup work via terminal (conv 4d9a5289: probing
        # API endpoints, writing scripts) stalled out after 3 turns because
        # execution-tool output never counted.
        if tool_results:
            prev_hashes = set(prev_result.get("tool_result_hashes") or [])
            merged_hashes = set(prev_hashes)
            for tr in tool_results:
                name = str(getattr(tr, "name", "") or "")
                if getattr(tr, "error", False):
                    continue
                result_text = (getattr(tr, "result", "") or "").strip()
                if name in _INFO_GATHERING_TOOLS:
                    if not progress and len(result_text) >= 200:
                        progress = True
                    continue
                # Short verification tools (word_count/grep) count as progress
                # only when the result is NOVEL (different file, different
                # query). Identical repeats still escalate — same hash.
                if len(result_text) < (1 if name in _SHORT_VERIFICATION_TOOLS else 50):
                    continue
                _h = hashlib.sha1(
                    f"{name}:{result_text[:2000]}".encode("utf-8", "ignore")
                ).hexdigest()
                merged_hashes.add(_h)
                if not progress and _h not in prev_hashes:
                    progress = True
            # Persist a bounded hash history so novelty compares across turns.
            result["tool_result_hashes"] = sorted(merged_hashes)[-200:]
        result["progress"] = progress

        # ── Spin detection (conv 01d08b67) ──────────────────────────────
        # When the agent only calls tools (execute_code that times out,
        # workspace_read, etc.) but never produces a visible text answer,
        # it's spinning — even if tool outputs have novel hashes (different
        # error messages, different partial results). Track consecutive
        # no-content turns; after 3, force progress=False so the existing
        # three-tier stall escalation fires (replan → partial_complete →
        # human_gate). This is general: catches ANY spin loop regardless
        # of root cause (timeout, API error, bad strategy).
        #
        # IMPORTANT: only override when progress came from tool-result
        # novelty alone. If the agent created new files or completed a
        # step, that's REAL progress even without visible text — don't
        # override it (otherwise legitimate tool-heavy work like running
        # a benchmark and saving results would false-trigger spin).
        _prev_nc = int((prev_result or {}).get("no_content_turns", 0))
        if len((last_response or "").strip()) < 50:
            _no_content_turns = _prev_nc + 1
        else:
            _no_content_turns = 0
        result["no_content_turns"] = _no_content_turns
        if _no_content_turns >= 3 and progress:
            # Check if progress came from files/steps (real progress)
            # vs tool-result novelty alone (spin candidate).
            _progress_from_files_or_steps = (
                not prev_result  # baseline round
                or bool(set(result["completed_steps"]) - prev_completed)
            )
            if not _progress_from_files_or_steps:
                for f in files:
                    p = str(f.get("path") or "")
                    if _is_noise_path(p):
                        continue
                    m = int(f.get("mtime") or 0)
                    if p not in prev_files or m > int(prev_files.get(p) or 0):
                        _progress_from_files_or_steps = True
                        break
            if not _progress_from_files_or_steps:
                logger.info(
                    "deathmatch: spin detected — %d consecutive no-content turns, "
                    "overriding tool-result-novelty progress=True → False (turn %d)",
                    _no_content_turns, self._conv.deathmatch_turns,
                )
                progress = False
                result["progress"] = False
                result["spin_detected"] = True

        # B4c: VLM visual critic (opt-in) — when enabled and a multimodal
        # endpoint is configured, layout-check this turn's visual deliverables
        # (.html/.svg) and merge the verdict: issues appended, and a
        # layout failure downgrades complete→partial (never upgrades).
        # Fail-open at every layer (disabled / unconfigured / not visual /
        # screenshot or VLM error).
        if config.deathmatch_visual_critic_enabled and current_step:
            try:
                from app.services.visual_critic import critique_visual_artifacts
                _vc = await critique_visual_artifacts(
                    new_files, current_step, self._conv.deathmatch_goal or "",
                    workspace_path=workspace_path,
                    assistant_llm=getattr(self, "_assistant_llm", None),
                )
                if _vc:
                    _vc_issues = [
                        f"版式问题({_vc.get('file', '?')}): {i}" for i in (_vc.get("issues") or [])
                    ]
                    result["issues"] = list(result.get("issues") or []) + _vc_issues
                    if _vc.get("layout_ok") is False and result.get("status") == "complete":
                        logger.info(
                            "visual_critic: layout issues in %s — downgrading complete→partial",
                            _vc.get("file"),
                        )
                        result["status"] = "partial"
                        result.setdefault("retry_instruction", "修复版式问题后重新交付")
            except Exception as exc:
                logger.info("visual_critic integration failed (non-blocking): %s", exc)

        # W2 D1（ACG 2609.32754）：verifier 链归档点——链结束时归档 issues/
        # completed 摘要（写点在注入之后：本单元进下一轮证据视图）。
        self._archive_verifier_evidence_units(result)
        self._conv.deathmatch_last_verification_result = result
        return result

    async def evaluate_after_turn(
        self,
        last_response: str,
        *,
        user_initiated: bool = False,
        workspace_path: str = "",
        tool_results: Optional[List[Any]] = None,
        ctx_estimate_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Run the judge and return a decision dict.

        Increments the turn counter on every agent response and asks the judge
        whether the goal is satisfied. The legacy invisible context-marker check
        is no longer used to gate continuation because current models do not echo
        HTML comments, which caused an infinite restart cycle.
        """
        # P0-3: store the agent loop's context estimate for the telemetry
        # block (0 = unavailable → the ctx line is omitted).
        self._ctx_estimate_tokens = int(ctx_estimate_tokens or 0)
        if workspace_path:
            self._workspace_path = workspace_path
        if not self.is_goal_active:
            return {
                "status": self._conv.deathmatch_status,
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "inactive",
                "reason": "no active goal",
                "message": "",
            }

        # Creative-goal judgment (LLM, per-goal cached) — resolves before any
        # prompt/verifier build so the sync get_continuation_prompt view is
        # already populated (agentic principle, replaces keyword heuristic).
        try:
            await _ensure_creative_judged(self._conv.deathmatch_goal or "")
        except Exception as exc:
            logger.warning("creative-goal judgment failed (non-blocking): %s", exc)

        # N3: re-arm the wall clock when an active loop re-enters after a
        # WAIT park (the freeze set started_at=None; the wait-parked period
        # is not charged, but the resumed segment must start counting).
        if not self._conv.deathmatch_wall_time_started_at:
            from datetime import datetime
            self._conv.deathmatch_wall_time_started_at = datetime.utcnow()

        # PEVR: wall-clock hard upper bound. Check before doing any LLM work.
        if self.wall_time_exceeded():
            self.trigger_human_gate(
                f"wall time 超限 ({self._conv.deathmatch_max_wall_time_seconds}s)"
            )
            try:
                self._final_attachments = await self.collect_final_deliverables_from_messages()
            except Exception:
                pass
            return {
                "status": "human_gate",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": "wall_time_exceeded",
                "message": (
                    f"死磕模式已进入人工介入 — 已运行超过 "
                    f"{self._conv.deathmatch_max_wall_time_seconds} 秒。"
                    "\n继续方式（PAUSED）：发送任意消息 = 按默认建议继续推进（默认）；"
                    "回复「调整目标」改变目标；回复「放弃」结束死磕。"
                ),
            }

        # Advance turn counter for every AUTONOMOUS agent response in the
        # goal loop. User-initiated turns (the message that kicked off the
        # run) do not consume the max_turns budget — they are not autonomous
        # work (C3: user_initiated was previously a dead parameter).
        if not user_initiated:
            self._conv.deathmatch_turns += 1

        # C4: spin guard (front-loaded) — a turn with no tool output and a
        # very short reply is not progress. Two CONSECUTIVE such turns
        # surface as partial_complete (visible to the user) instead of
        # burning judge/verifier budget on empty content. Conservative:
        # a single no-tool turn can be a legitimate planning turn, and any
        # real activity (tool output or a substantive reply) resets the
        # counter. Process-local — a process restart restarts the count,
        # which is fine (the spin would restart too).
        # Spin = completely empty reply (no content at all) AND no tool
        # output. Short-but-nonempty replies are NOT spins — "working"-style
        # one-liners can accompany real verifier progress (plan-complete +
        # progress scenarios must never be killed; A4.9 r5 regression).
        _spin_short = not (last_response or "").strip()
        _spin_no_tool = not tool_results or all(
            not (getattr(tr, "result", "") or "").strip()
            for tr in (tool_results or [])
        )
        if not user_initiated and _spin_short and _spin_no_tool:
            # Exclude agent_loop's synthetic judge inputs (inactivity /
            # budget-exhausted placeholders) — those are NOT spins, and a
            # slow provider must never be killed by this guard (A4.9
            # Important 4, conv 6b0faf81 class).
            _synth = last_response.startswith("(") and (
                "no content produced" in last_response
                or "budget exhausted" in last_response
                or "inactivity" in last_response
                or "tool calls in progress" in last_response
            )
            if not _synth:
                _spin = _SPIN_COUNTS.get(self._conv.id, 0) + 1
                _SPIN_COUNTS[self._conv.id] = _spin
                if _spin >= 2:
                    _SPIN_COUNTS.pop(self._conv.id, None)
                    if config.deathmatch_autonomy_enabled:
                        # Autonomy (2026-08-31): never gate on empty turns —
                        # inject a substance directive and keep pushing.
                        logger.info(
                            "deathmatch spin guard (autonomy): %d consecutive "
                            "empty no-tool turns — directive continue",
                            _spin,
                        )
                        _directive = (
                            "[系统] 你已连续多轮没有产出任何内容或工具调用。"
                            "下一轮必须二选一：调用工具完成实质工作，"
                            "或直接给出有实质内容的回复。"
                        )
                        return {
                            "status": "active",
                            "should_continue": True,
                            "continuation_prompt": (
                                f"{_directive}\n{self.get_continuation_prompt(last_response)}"
                            ),
                            "verdict": "continue",
                            "reason": "spin_guard_autonomy",
                            "message": (
                                f"[死磕] 连续 {_spin} 轮无产出，已注入推进指令 "
                                f"(第{self._conv.deathmatch_turns}轮)"
                            ),
                        }
                    logger.info(
                        "deathmatch spin guard: %d consecutive empty no-tool turns → "
                        "partial_complete (C4)",
                        _spin,
                    )
                    self._conv.deathmatch_status = "partial_complete"
                    self._conv.deathmatch_reason = (
                        f"连续 {_spin} 轮无产出且无工具调用（spin），已暂停目标循环"
                    )
                    self._freeze_wall_time()
                    return {
                        "status": "partial_complete",
                        "should_continue": False,
                        "continuation_prompt": None,
                        "verdict": "continue",
                        "reason": "spin_guard",
                        "message": (
                            "连续多轮无产出，已暂停目标循环。"
                            "发送消息继续，或关闭死磕模式接受当前结果。"
                        ),
                    }
        else:
            _SPIN_COUNTS.pop(self._conv.id, None)

        # Completion is judged by the judge LLM — the sole completion
        # authority (agentic principle, 2026-07-20: 禁止正则/硬编码分类器).
        # The former regex completion-declaration detector (_agent_done,
        # conv 149ce886..6b0faf81 lineage) was removed: judge LLM timeouts
        # are already recovered agentically by _safe_judge's continuation
        # directive + the verifier's progress detection, and judge/verifier
        # conflicts are resolved by an LLM reconciliation below.
        _goal_with_subgoals = self._goal_with_criteria()
        # B1: build the environment evidence pack for the judge (AJ-Bench:
        # judge+evidence > stronger blind judge). Deterministic, no LLM.
        _judge_evidence = ""
        if config.deathmatch_judge_evidence_enabled:
            try:
                _judge_evidence = self._build_judge_evidence(workspace_path, tool_results)
            except Exception as exc:
                logger.debug("judge evidence build failed (non-blocking): %s", exc)
        _judge_result = await _call_judge_llm(
            _goal_with_subgoals, last_response,
            judge_llm=self._make_llm(),
            evidence=_judge_evidence,
        )
        # E2：新契约为 6 元组（含 taxonomy）；兼容旧 5 元组桩（测试 monkeypatch）
        if len(_judge_result) == 6:
            verdict, reason, parse_failed, infra_failed, compact, taxonomy = _judge_result
        else:
            verdict, reason, parse_failed, infra_failed, compact = _judge_result
            taxonomy = None
        self._last_judge_taxonomy = taxonomy
        # P2-11: rubric compaction signal from the judge (consumed by the
        # agent loop's forced-compression gate).
        self._last_judge_compact = bool(compact)
        self._conv.deathmatch_verdict = verdict
        self._conv.deathmatch_reason = reason

        if parse_failed:
            self._conv.deathmatch_consecutive_failures += 1
        elif not infra_failed:
            # Infra turns leave the parse-failure counter unchanged — a
            # timeout must not erase a parse-failure streak (A4.9 r2 M2).
            self._conv.deathmatch_consecutive_failures = 0

        # P1-7 (round-4 eval): judge infra failures (timeout/LLM error) are
        # fail-open "continue" — track them as a streak; two in a row enter
        # the stall tiers at the end of this evaluation (a dead provider must
        # not burn wall clock invisibly). A successful judge resets the streak.
        if infra_failed:
            _JUDGE_INFRA_FAILURES[self._conv.id] = (
                _JUDGE_INFRA_FAILURES.get(self._conv.id, 0) + 1
            )
        else:
            _JUDGE_INFRA_FAILURES.pop(self._conv.id, None)

        # Snapshot the stall counter BEFORE the verifier branches so the
        # P1-7 infra-stall below can tell whether the verifier already
        # stalled THIS turn (counter increased) — no double-counting.
        _vf_before_verifier = self._conv.deathmatch_verify_failures or 0

        max_consecutive = config.deathmatch_max_consecutive_failures

        # PEVR: ALWAYS run the verifier for step tracking. The verifier marks
        # steps as done when their expected outputs are detected, advancing
        # the plan. The LLM-based inter-step relevance check is gated by
        # verify_enabled for cost control, but file-based step tracking
        # always runs.
        # B2: snapshot the done-set BEFORE the verifier so a step transition
        # (new completion this turn) can trigger a context reset downstream.
        _done_before_verify = {
            s.get("id")
            for s in ((self._conv.deathmatch_plan or {}).get("steps") or [])
            if s.get("status") == "done" and s.get("id")
        }
        verify_result: Optional[Dict[str, Any]] = None
        if workspace_path:
            try:
                verify_result = await self.verify_step_outputs(
                    last_response, workspace_path, tool_results=tool_results
                )
            except Exception as exc:
                logger.warning("PEVR verifier failed: %s", exc)
        # B2 (SKILL.state): a NEW step completion with a next step pending =
        # step-boundary transition. The agent loop consumes this flag by
        # rebuilding the executor context from the handoff document instead
        # of letting it accumulate unbounded (fresh-context executor).
        if verify_result:
            _done_now = set(verify_result.get("completed_steps") or [])
            if (_done_now - _done_before_verify) and self._get_next_pending_step() is not None:
                self._step_transition_pending = True
                logger.info(
                    "deathmatch: step boundary crossed (%s) — context reset armed",
                    sorted(_done_now - _done_before_verify),
                )

        # PEVR: Check plan step completion. Only accept "done" when the
        # verifier confirms ALL plan steps are complete. Do NOT force "done"
        # based on step status alone (unreliable heuristics were removed).
        _plan = self._conv.deathmatch_plan
        _has_plan = isinstance(_plan, dict) and bool((_plan.get("steps") or []))
        _unfinished_steps = []
        if _has_plan:
            _steps = _plan.get("steps") or []
            _unfinished_steps = [s for s in _steps if s.get("status") != "done"]

        # D2: judge says WAIT — progress is gated on async work (background
        # task / backoff / external processing). Park the loop: end this
        # round WITHOUT consuming stall counters or injecting a continuation;
        # the user message or the next judge pass resumes naturally.
        if verdict == "wait":
            logger.info(
                "deathmatch: judge WAIT (turn %d) — parking goal loop: %s",
                self._conv.deathmatch_turns, reason,
            )
            self._conv.deathmatch_verdict = "wait"
            self._conv.deathmatch_reason = reason
            # I1: WAIT is not autonomous work — refund the turn so budget
            # semantics hold ("no turn burn").
            if not user_initiated:
                self._conv.deathmatch_turns = max(0, self._conv.deathmatch_turns - 1)
            # I2: freeze the wall clock while parked (waiting is not
            # working); resume() re-starts the segment.
            self._freeze_wall_time()
            if config.deathmatch_autonomy_enabled:
                # Autonomy (2026-08-31): WAIT becomes a bounded in-loop
                # backoff — the run continues automatically after a short
                # sleep instead of parking until a user message arrives.
                _m = _re.search(r"wait_seconds=(\d+)", reason or "")
                _backoff = min(max(int(_m.group(1)) if _m else 15, 5), 120)
                return {
                    "status": "active",
                    "should_continue": True,
                    "continuation_prompt": self.get_continuation_prompt(last_response),
                    "verdict": "wait",
                    "reason": reason,
                    "backoff_seconds": _backoff,
                    "message": (
                        f"[死磕] 等待异步工作，{_backoff}s 后自动继续 "
                        f"(第{self._conv.deathmatch_turns}轮)"
                    ),
                }
            return {
                "status": "active",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "wait",
                "reason": reason,
                "message": (
                    "[死磕] 目标推进被异步工作阻塞（等待后台任务/退避），"
                    "本轮暂停。发送任意消息或稍后继续。"
                ),
            }

        # MEA (LongHorizon-Harness 2608.01964): blocked/ask are first-class
        # control decisions, distinct from done. blocked = the goal cannot
        # advance by any permitted action — stop with a resumable report
        # instead of burning infinite episode resets (autonomy) or being
        # mislabeled "done" (the legacy judge-prompt mapping). ask = user
        # information/authorization is indispensable — park with the
        # question surfaced. Both reuse human_gate (any user message
        # resumes), in BOTH autonomy and legacy mode.
        if verdict in ("blocked", "ask"):
            # I1 parity with WAIT: not autonomous work — refund the turn.
            # (wall freeze is inside trigger_human_gate.)
            if not user_initiated:
                self._conv.deathmatch_turns = max(0, self._conv.deathmatch_turns - 1)
            gate_reason = (
                f"目标受阻：{reason}" if verdict == "blocked"
                else f"需要用户输入：{reason}"
            )
            self.trigger_human_gate(gate_reason, report={"suggested_actions": [
                "补充信息/授权后发送消息继续", "调整目标", "放弃",
            ]})
            try:
                self._final_attachments = await self.collect_final_deliverables_from_messages()
            except Exception:
                pass
            if verdict == "blocked":
                msg = (
                    f"[死磕] 目标在当前条件下无法自主推进：{reason}\n"
                    "已暂停目标循环（可恢复）。发送任意消息重试推进，"
                    "回复「调整目标」改变目标，或关闭死磕模式接受当前结果。"
                )
            else:
                msg = (
                    f"[死磕] 需要你提供信息或授权才能继续：{reason}\n"
                    "请直接回复所需内容——发送任意消息即恢复推进。"
                )
            logger.info(
                "deathmatch: judge %s (turn %d) — resumable park: %s",
                verdict, self._conv.deathmatch_turns, reason[:120],
            )
            return {
                "status": "human_gate",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": verdict,
                "reason": reason,
                "message": msg,
            }

        # If judge says done, verify against the plan before finalizing.
        if verdict == "done":
            # W1e: deterministic goal comparator — mechanical acceptance
            # criteria run BEFORE any finalize path (fail-closed: a failed
            # mechanical check rejects the done verdict outright).
            _cmp_issues: List[str] = []
            try:
                _cmp_issues = await self._run_goal_comparator(workspace_path)
            except Exception as exc:
                logger.warning("goal comparator failed (fail-open): %s", exc)
            if _cmp_issues:
                self._record_event("comparator", issues=_cmp_issues[:5], phase="block")
                self._record_failed_direction(
                    "机械验收未通过：" + _cmp_issues[0], family="comparator",
                )
                # A4.9 Important: a blocked done must also feed the global
                # convergence breaker — an unsatisfiable mechanical criterion
                # must not loop forever under unlimited autonomy.
                if self._bump_no_progress_replan():
                    self.trigger_human_gate(
                        f"机械验收连续 {self._conv.deathmatch_no_progress_replans} 次未通过，"
                        "已触发全局收敛断路器（可恢复）",
                        report={"suggested_actions": ["继续（发送任意消息）", "调整目标", "放弃"]},
                    )
                    try:
                        self._final_attachments = await self.collect_final_deliverables_from_messages()
                    except Exception:
                        self._final_attachments = []
                    return {
                        "status": "human_gate",
                        "should_continue": False,
                        "continuation_prompt": None,
                        "verdict": "continue",
                        "reason": (
                            f"comparator_blocked_cap "
                            f"({self._conv.deathmatch_no_progress_replans})"
                        ),
                        "message": (
                            f"死磕模式已进入人工介入 — 机械验收连续 "
                            f"{self._conv.deathmatch_no_progress_replans} 次未通过"
                            f"（{_cmp_issues[0][:80]}）。"
                            "\n继续方式（PAUSED）：发送任意消息 = 按默认建议继续推进（默认）；"
                            "回复「调整目标」改变目标；回复「放弃」结束死磕。"
                        ),
                        "verify_result": verify_result,
                    }
                _cont = self.get_continuation_prompt(last_response) or ""
                _cont_block = (
                    "\n\n[系统] 机械验收（goal comparator）未通过，必须修复后才能结束：\n- "
                    + "\n- ".join(_cmp_issues[:5])
                )
                return {
                    "status": "active",
                    "should_continue": True,
                    "continuation_prompt": _cont + _cont_block,
                    "verdict": "continue",
                    "reason": "comparator: " + "; ".join(_cmp_issues[:3]),
                    "message": (
                        f"[死磕] 机械验收未通过（{_cmp_issues[0][:60]}），继续修复 "
                        f"(第{self._conv.deathmatch_turns}轮)"
                    ),
                    "verify_result": verify_result,
                }
            self._record_event("comparator", issues=[], phase="pass")
            if _has_plan:
                _step_issues: List[str] = []
                for s in _unfinished_steps:
                    try:
                        _step_issues += [
                            f"[{s.get('id')}] {i}"
                            for i in await self._run_step_done_checks(s, workspace_path)
                        ]
                    except Exception:
                        continue
                if _step_issues:
                    self._record_event(
                        "step_done_check", issues=_step_issues[:5], phase="block",
                    )
                    # A4.9 Important: same breaker accounting as the goal
                    # comparator block (bounded, resumable).
                    if self._bump_no_progress_replan():
                        self.trigger_human_gate(
                            f"步骤机械验收连续 {self._conv.deathmatch_no_progress_replans} 次未通过，"
                            "已触发全局收敛断路器（可恢复）",
                            report={"suggested_actions": ["继续（发送任意消息）", "调整目标", "放弃"]},
                        )
                        try:
                            self._final_attachments = await self.collect_final_deliverables_from_messages()
                        except Exception:
                            self._final_attachments = []
                        return {
                            "status": "human_gate",
                            "should_continue": False,
                            "continuation_prompt": None,
                            "verdict": "continue",
                            "reason": (
                                f"step_done_check_cap "
                                f"({self._conv.deathmatch_no_progress_replans})"
                            ),
                            "message": (
                                f"死磕模式已进入人工介入 — 步骤机械验收连续 "
                                f"{self._conv.deathmatch_no_progress_replans} 次未通过"
                                f"（{_step_issues[0][:80]}）。"
                                "\n继续方式（PAUSED）：发送任意消息 = 按默认建议继续推进（默认）；"
                                "回复「调整目标」改变目标；回复「放弃」结束死磕。"
                            ),
                            "verify_result": verify_result,
                        }
                    _cont = self.get_continuation_prompt(last_response) or ""
                    _cont_block = (
                        "\n\n[系统] 未完成步骤的机械验收未通过，必须修复后才能结束：\n- "
                        + "\n- ".join(_step_issues[:5])
                    )
                    return {
                        "status": "active",
                        "should_continue": True,
                        "continuation_prompt": _cont + _cont_block,
                        "verdict": "continue",
                        "reason": "step_done_check: " + "; ".join(_step_issues[:3]),
                        "message": (
                            f"[死磕] 步骤机械验收未通过（{_step_issues[0][:60]}），继续修复 "
                            f"(第{self._conv.deathmatch_turns}轮)"
                        ),
                        "verify_result": verify_result,
                    }
            # Both mechanical gates passed (goal comparator + unfinished-step
            # done_check) — genuine progress, clear the breaker (A4.9 r2 N1:
            # the reset must come AFTER the step checks, or a step-level
            # block can never reach the cap).
            self._reset_no_progress_replan()
            # Agentic completion gate: the judge LLM is the completion
            # authority (agentic principle 2026-07-20 — 禁止正则/硬编码分类
            # 器). When the plan still has unfinished steps or the verifier
            # disagrees, an LLM RECONCILIATION call weighs the verifier's
            # evidence against the judge's verdict — no mechanical
            # progress-based overrides (replaces the former _strong_delivery
            # regex + progress override, conv 2fa87be4 / 6b0faf81 lineage).
            _conflict = False
            if _has_plan:
                # With a plan: unfinished steps or a verifier that did not say
                # complete are a conflict to reconcile. With NO plan
                # (planner-degraded mode) the verifier always reports
                # "partial" (no steps to validate) — trust the judge's done
                # verdict and finalize directly (reviewer finding #4: plan=None
                # must not strand a finished loop via conservative
                # reconciliation).
                _conflict = bool(_unfinished_steps) or bool(
                    verify_result and verify_result.get("status") != "complete"
                )
            if _conflict:
                # D2a（VeriHarness）：reconcile 之前跑一次共识挑战者（默认关，
                # fail-open）——覆盖面检查（验收标准×证据）注入仲裁证据。
                _consensus_findings = await self._challenge_completion_consensus(
                    last_response, workspace_path,
                )
                _recon_decision, _recon_reason = await self._reconcile_completion(
                    last_response, reason, verify_result, _unfinished_steps,
                    consensus_findings=_consensus_findings,
                )
                if _recon_decision == "continue":
                    verdict = "continue"
                    self._conv.deathmatch_verdict = verdict
                    # P1-5: the reconcile overturn is settled — the judge must
                    # not re-assert done on the same evidence next turn.
                    self._record_settled({
                        "type": "reconcile_continue",
                        "summary": f"judge 判完成被仲裁驳回：{str(_recon_reason)[:150]}",
                    })
                    # AEWM B1：「目标已完成」声称被仲裁证伪——撤回以防后续
                    # 判定/产出继续建立在该声称上。
                    self._record_retracted_claim(
                        "「目标已完成」的声称",
                        reason=_truncate(str(_recon_reason), 200),
                        source="reconcile",
                    )
                    if verify_result and verify_result.get("progress"):
                        # Genuine progress → reset stall counter (matches the
                        # normal-progress reset in the partial branch below).
                        self._conv.deathmatch_verify_failures = 0
                        self._reset_no_progress_replan()
                        _HARNESS_REPAIR_COUNTS.pop(self._conv.id, None)  # C2: progress resets repair budget
                    logger.info(
                        "deathmatch: judge=done but reconciliation=continue "
                        "(%s, turn %d)",
                        _recon_reason[:120], self._conv.deathmatch_turns,
                    )
                    cont = self.get_continuation_prompt(last_response)
                    return {
                        "status": "active",
                        "should_continue": True,
                        "continuation_prompt": cont,
                        "verdict": "continue",
                        "reason": f"reconciliation=continue: {_recon_reason}; {reason}",
                        "message": (
                            f"[死磕] 完成度评审认为目标尚未最终完成"
                            f"（{_recon_reason[:60]}），继续执行 "
                            f"(第{self._conv.deathmatch_turns}轮)"
                        ),
                        "verify_result": verify_result,
                    }
                if _recon_decision == "stall":
                    # A4.9 M3：stall 裁决同样是「目标已完成」声称被推翻——
                    # 与 continue 裁决一致地撤回（B1 覆盖同类仲裁结果）。
                    self._record_retracted_claim(
                        "「目标已完成」的声称",
                        reason=_truncate(str(_recon_reason), 200),
                        source="reconcile",
                    )
                    _stall = await self._handle_stall(
                        f"reconciliation=stall: {_recon_reason[:150]}",
                        verify_result, last_response,
                        judge_reason=reason,
                    )
                    if _stall is not None:
                        return _stall
                    cont = self.get_continuation_prompt(last_response)
                    return {
                        "status": "active",
                        "should_continue": True,
                        "continuation_prompt": cont,
                        "verdict": "continue",
                        "reason": f"reconciliation=stall: {_recon_reason}; {reason}",
                        "message": (
                            f"[死磕] 完成度评审提示停滞，已重规划并继续 "
                            f"(第{self._conv.deathmatch_turns}轮)"
                        ),
                        "verify_result": verify_result,
                    }
                # decision == finalize → fall through to the done finalize.
            self._conv.deathmatch_status = "done"
            self._conv.deathmatch_verify_failures = 0
            self._reset_no_progress_replan()
            _HARNESS_REPAIR_COUNTS.pop(self._conv.id, None)  # C2: progress resets repair budget
            # The judge's done verdict is accepted (possibly backed by the
            # reconciliation LLM) — mark remaining plan steps done so the UI
            # shows the correct final step count.
            if _has_plan:
                for s in (_plan.get("steps") or []):
                    if s.get("status") != "done":
                        self._set_step_status(s, "done", "finalize_close_out")
                if _unfinished_steps:
                    # W1e + A4.9 Minor: only steps whose mechanical checks
                    # PASSED (or declared none) reach this point — record the
                    # close-out with an accurate reason.
                    self._record_event(
                        "finalized_without_step_evidence",
                        steps=[s.get("id") for s in _unfinished_steps][:10],
                        reason=(
                            "reconcile-finalize：这些步骤未经 verifier 单独定案；"
                            "机械 done_check 未发现失败（gate 类在未启用时跳过、执行异常不阻断；"
                            "失败者已在收口前拦截）"
                        ),
                    )
            if _has_plan:
                # Terminal status, plan steps and verifier evidence must describe
                # the same completed contract. A baseline/partial result left
                # from turn 1 would otherwise contradict status=done and the
                # now-completed plan even when finalization is legitimate.
                _previous_verification = self._conv.deathmatch_last_verification_result or verify_result or {}
                _completed_step_ids = [
                    str(s.get("id") or "") for s in (_plan.get("steps") or []) if s.get("id")
                ]
                self._conv.deathmatch_last_verification_result = {
                    **_previous_verification,
                    "status": "complete",
                    "progress": True,
                    "completed_steps": _completed_step_ids,
                    "issues": [],
                    "current_step": _completed_step_ids[-1] if _completed_step_ids else None,
                    "integrity": _previous_verification.get("integrity", "clean"),
                }
            # Generate the final task summary table.
            final_table = self.generate_final_summary_table()
            # Collect deliverable files from agent tool output (not filesystem
            # scanning). See collect_final_deliverables_from_messages. The
            # agent's final response and the judge's reason are passed as
            # citation texts so tool-generated files they explicitly name
            # (e.g. a merged full-novel docx) are included even when the agent
            # forgot to attach them via provide_file.
            try:
                self._final_attachments = await self.collect_final_deliverables_from_messages(
                    citation_texts=[last_response or "", self._conv.deathmatch_reason or ""],
                )
            except Exception as exc:
                logger.warning("deathmatch final deliverables collection failed: %s", exc)
                self._final_attachments = []
            # ScienceBuddy 持续学习：收束教训回写记忆（fail-open，不阻断收束）。
            await self._persist_completion_memory()
            return {
                "status": "done",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "done",
                "reason": reason,
                "message": f"目标已完成：{reason}",
                "final_summary_table": final_table,
                "final_attachments": list(self._final_attachments),
            }

        if self._conv.deathmatch_consecutive_failures >= int(max_consecutive):
            self._conv.deathmatch_status = "paused"
            self._conv.deathmatch_reason = (
                f"连续 {self._conv.deathmatch_consecutive_failures} 次评判解析失败"
            )
            # C1 freeze: this paused path must not charge the parked period
            # on resume (A4.9 r4 Important 3 residual).
            self._freeze_wall_time()
            # W2c: every stop carries a resume packet (A4.9 Minor).
            self._write_pause_packet(
                gate="judge-parse-failures",
                question=self._conv.deathmatch_reason,
                options=["发送任意消息重试", "调整目标", "放弃"],
                default_if_continue="按默认建议重试评判并继续推进",
            )
            try:
                self._final_attachments = await self.collect_final_deliverables_from_messages()
            except Exception:
                pass
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": reason,
                "message": (
                    f"死磕模式已暂停 — 连续 {self._conv.deathmatch_consecutive_failures} 次评判失败。"
                    "请重新启动或关闭死磕模式。"
                ),
            }

        _max_turns_budget = int(self._conv.deathmatch_max_turns or 0)
        # 0 = unlimited (autonomy default): the turn-budget gate only fires
        # when the operator configured a positive budget.
        if _max_turns_budget > 0 and self._conv.deathmatch_turns >= _max_turns_budget:
            self.trigger_human_gate(
                f"轮次预算耗尽 ({self._conv.deathmatch_turns}轮)",
                report={"suggested_actions": ["继续（发送任意消息）", "调整目标", "放弃"]},
            )
            try:
                self._final_attachments = await self.collect_final_deliverables_from_messages()
            except Exception:
                pass
            return {
                "status": "human_gate",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": reason,
                "message": (
                    f"死磕模式已进入人工介入 — 已使用 {self._conv.deathmatch_turns} 轮。"
                    "\n继续方式（PAUSED）：发送任意消息 = 按默认建议继续推进（默认）；"
                    "回复「调整目标」改变目标；回复「放弃」结束死磕。"
                ),
            }

        # PEVR: record reflection + inject plan/reflection into continuation.
        # Three-tier stall escalation via _handle_stall:
        #   - verifier=blocked          → stall (verifier found real problems)
        #   - verifier=complete + plan done → stall (plan insufficient for goal)
        #   - verifier=partial + no progress → stall (same step, no new files)
        #   - verifier=partial + progress    → normal progress, reset stall counter
        if verify_result and verify_result.get("status") == "blocked":
            _stall = await self._handle_stall(
                f"verifier blocked: {str(verify_result.get('issues', []))[:200]}",
                verify_result, last_response,
                judge_reason=reason,
            )
            if _stall is not None:
                return _stall
        elif (
            verify_result
            and verify_result.get("status") == "complete"
            and _has_plan
            and not _unfinished_steps
        ):
            # All plan steps are done but the judge says the goal isn't met.
            # For long-horizon goals this is a NORMAL mid-task state: the
            # plan simply underestimated the remaining work (conv 6b0faf81:
            # the planner degraded to a single all-encompassing step with
            # generic expected_output, the verifier marked it done after
            # chapter 1 of a 100k-char novel, and 3 rounds of this branch
            # stopped the whole task at partial_complete). Mirror opencode
            # goal mode: keep working — continue WITHOUT counting this as a
            # stall, and WITHOUT burning an expensive replanner call, while
            # the agent is advancing (new/changed files this turn).
            #
            # Replanning is only needed when NOTHING advanced: a new plan
            # adds concrete steps for the remaining goal work and restarts
            # the phase. If the replanner fails to produce actionable steps
            # (None / all-done — e.g. LLM timeout, conv 6b0faf81), the
            # three-tier escalation fires — a genuinely stuck replanner.
            _replanned = None
            _has_actionable = False
            _progress_made_pc = bool(verify_result and verify_result.get("progress"))
            if not _progress_made_pc:
                try:
                    _replanned = await self.replan(verify_result)
                except Exception as exc:
                    logger.warning("PEVR replan failed (plan-complete branch): %s", exc)
                _new_steps = ((_replanned or {}).get("steps") or []) if isinstance(_replanned, dict) else []
                _has_actionable = any(s.get("status") != "done" for s in _new_steps)
            if _has_actionable:
                logger.info(
                    "deathmatch: plan complete but goal unmet → replanned with "
                    "%d actionable steps, continuing WITHOUT stall (turn %d)",
                    len(_new_steps), self._conv.deathmatch_turns,
                )
                # Actionable replan = new phase: reset so no-progress history
                # cannot stop a loop that is demonstrably working again.
                # P1-7: judge infra turns never reset the stall counter —
                # verifier progress does not prove the judge channel
                # recovered (a dead judge must still escalate, P1-7).
                if not infra_failed:
                    self._conv.deathmatch_verify_failures = 0
                    self._reset_no_progress_replan()
                    _HARNESS_REPAIR_COUNTS.pop(self._conv.id, None)  # C2: progress resets repair budget
            elif _progress_made_pc:
                logger.info(
                    "deathmatch: plan complete but goal unmet, agent advancing "
                    "(progress=True) → continuing WITHOUT stall or replan (turn %d)",
                    self._conv.deathmatch_turns,
                )
                # Healthy work (new/changed files): reset the stall counter
                # (skipped on judge infra turns — P1-7).
                if not infra_failed:
                    self._conv.deathmatch_verify_failures = 0
                    self._reset_no_progress_replan()
                    _HARNESS_REPAIR_COUNTS.pop(self._conv.id, None)  # C2: progress resets repair budget
            else:
                # The branch already attempted replan once this turn — do NOT
                # replan again inside _handle_stall (duplicate ~120s LLM call
                # on the same all-done input, likely identical failure). The
                # next stall turn retries via this branch itself.
                _stall = await self._handle_stall(
                    "plan complete but goal unmet (replan produced no actionable steps)",
                    verify_result, last_response,
                    replan=False,
                    judge_reason=reason,
                )
                if _stall is not None:
                    return _stall
        else:
            # Verifier=partial is only "normal progress" when something
            # actually advanced (new/changed files or a newly completed
            # step). Consecutive no-progress partial rounds are stalls —
            # route them through the same three-tier escalation so the loop
            # replans / partial-completes / human-gates instead of spinning.
            if (
                verify_result
                and verify_result.get("status") == "partial"
                and verify_result.get("progress") is False
            ):
                # Distinguish spin (agent only calls tools, no visible text)
                # from genuine no-progress (same files, same steps). Spin
                # needs a different user-facing explanation.
                if verify_result.get("spin_detected"):
                    _stall_reason = (
                        f"spin: {verify_result.get('no_content_turns')} consecutive "
                        f"no-content turns (agent calls tools but never produces "
                        f"a text answer — likely stuck on timeouts/errors)"
                    )
                else:
                    _stall_reason = (
                        f"verifier partial, no progress (step {verify_result.get('current_step')}, "
                        "no new files or completed steps)"
                    )
                # W2a: per-node recovery ladder (local_retry → local_patch)
                # before the legacy stall machinery (attempts carried on the
                # step; a successful local_patch returns a continue decision).
                _node_decision = await self._maybe_node_recovery(
                    verify_result, last_response
                )
                if _node_decision is not None:
                    return _node_decision
                # W2b: global no-progress replan breaker (approved plan §5):
                # counts only rounds that would actually replan; a resumable
                # human_gate bounds runaway replan churn even in autonomy.
                _will_replan = self._conv.deathmatch_verify_failures >= 1
                if _will_replan and self._bump_no_progress_replan():
                    self.trigger_human_gate(
                        f"连续 {self._conv.deathmatch_no_progress_replans} 次无进展重规划，"
                        "已触发全局收敛断路器（可恢复）",
                        report={"suggested_actions": ["继续（发送任意消息）", "调整目标", "放弃"]},
                    )
                    try:
                        self._final_attachments = await self.collect_final_deliverables_from_messages()
                    except Exception:
                        self._final_attachments = []
                    return {
                        "status": "human_gate",
                        "should_continue": False,
                        "continuation_prompt": None,
                        "verdict": "continue",
                        "reason": (
                            f"no_progress_replan_cap "
                            f"({self._conv.deathmatch_no_progress_replans})"
                        ),
                        "message": (
                            f"死磕模式已进入人工介入 — 连续 "
                            f"{self._conv.deathmatch_no_progress_replans} 次无进展重规划。"
                            "\n继续方式（PAUSED）：发送任意消息 = 按默认建议继续推进（默认）；"
                            "回复「调整目标」改变目标；回复「放弃」结束死磕。"
                        ),
                        "verify_result": verify_result,
                    }
                _stall = await self._handle_stall(
                    _stall_reason,
                    verify_result, last_response,
                    # First no-progress round: reflect only, no replan yet.
                    # Replan from the second consecutive no-progress round.
                    replan=self._conv.deathmatch_verify_failures >= 1,
                    judge_reason=reason,
                )
                if _stall is not None:
                    return _stall
            else:
                # Normal progress (or no verifier data) → reset stall.
                # P1-7: skipped on judge infra turns — verifier progress does
                # not prove the judge channel recovered.
                if not infra_failed:
                    self._conv.deathmatch_verify_failures = 0
                    _HARNESS_REPAIR_COUNTS.pop(self._conv.id, None)  # C2: progress resets repair budget
                    # W2b: real progress resets the global no-progress breaker.
                    # A4.9 r2 N6: only on non-infra turns (same P1-7 rule as
                    # the stall counter — a dead judge channel must not clear
                    # the convergence breaker).
                    self._reset_no_progress_replan()

        self._record_reflection(last_response, verdict, verify_result, reason=reason)

        # P1-7: two consecutive judge infra failures → stall escalation. Runs
        # AFTER the verifier branches and skips the replan — the plan is not
        # the problem, the provider is. A4.9 r2 Imp-2: when the verifier
        # already stalled THIS turn (tier-1 returns None and falls through),
        # the infra stall must NOT fire a second time (single-turn +1 max).
        if (
            infra_failed
            and _JUDGE_INFRA_FAILURES.get(self._conv.id, 0) >= _JUDGE_INFRA_STALL_THRESHOLD
        ):
            _infra_n = _JUDGE_INFRA_FAILURES.pop(self._conv.id, 0)
            if config.deathmatch_autonomy_enabled:
                # Autonomy (2026-08-31): a dead judge channel is an
                # ENVIRONMENT failure, not a task stall — pause visibly
                # instead of silently burning LLM calls with no completion
                # authority. Any user message resumes (and re-snapshots).
                self._conv.deathmatch_status = "paused"
                self._conv.deathmatch_reason = (
                    f"judge 连续 {_infra_n} 次超时/错误（infra），评估通道不可用"
                )
                self._freeze_wall_time()
                # W2c: every stop carries a resume packet (A4.9 Minor).
                self._write_pause_packet(
                    gate="judge-infra",
                    question=self._conv.deathmatch_reason,
                    options=["发送任意消息重试", "调整目标", "放弃"],
                    default_if_continue="检查模型服务后发送任意消息重试",
                )
                return {
                    "status": "paused",
                    "should_continue": False,
                    "continuation_prompt": None,
                    "verdict": "continue",
                    "reason": reason,
                    "message": (
                        f"死磕模式已暂停 — 评判通道连续 {_infra_n} 次不可用"
                        "（模型服务异常）。发送任意消息重试，"
                        "或检查模型服务后继续。"
                    ),
                }
            if (self._conv.deathmatch_verify_failures or 0) <= _vf_before_verifier:
                _stall = await self._handle_stall(
                    f"judge 连续 {_infra_n} 次超时/错误（infra），评估通道不可用",
                    verify_result, last_response,
                    replan=False,
                    judge_reason=reason,
                )
                if _stall is not None:
                    return _stall

        cont = self.get_continuation_prompt(last_response)
        return {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": cont,
            "verdict": "continue",
            "reason": reason,
            "message": (
                f"[死磕] 继续推进目标 (第{self._conv.deathmatch_turns}轮): {reason}"
            ),
            "verify_result": verify_result,
        }
