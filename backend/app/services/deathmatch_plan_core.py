# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from deathmatch_service.py (DeathmatchPlanCore, 2026-10-20).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``deathmatch_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (
    '_JUDGE_EVIDENCE_CHARS',
    '_format_criteria_block',
    '_head_tail_truncate',
    '_os',
    '_read_deliverable_excerpt',
    '_truncate',
    'config',
    'json',
    'logger',
    'select',
)


def _sync_deathmatchplancoremixin_globals() -> None:
    from app.services import deathmatch_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class DeathmatchPlanCoreMixin:
    def _goal_with_subgoals(self) -> str:
        """D3: the goal text plus any user-appended acceptance criteria
        (subgoals) — the judge must check every criterion; the continuation
        prompt surfaces them so the agent works toward them."""
        _sync_deathmatchplancoremixin_globals()
        goal = self._conv.deathmatch_goal or ""
        subgoals = list(getattr(self._conv, "deathmatch_subgoals", None) or [])
        if not subgoals:
            return goal
        return (
            goal
            + "\n\n附加验收标准（全部必须满足才算完成）：\n"
            + "\n".join(f"- {str(s)[:300]}" for s in subgoals)
        )
    def _build_judge_evidence(self, workspace_path: str, tool_results: Optional[List[Any]] = None) -> str:
        """B1 (AJ-Bench 2604.18240): environment evidence pack for the judge.

        Shares the verifier's deterministic evidence with the judge so the
        completion verdict is grounded in the environment, not only in the
        agent's narration. Contents: settled (completed) plan steps with their
        recorded output — the P1-5 light form, the judge must not re-litigate
        them without new evidence — the workspace snapshot, the previous
        verification summary, and this turn's tool trace. Hard-capped at
        _JUDGE_EVIDENCE_CHARS (explicit truncation marker per the
        information-integrity rule).
        """
        _sync_deathmatchplancoremixin_globals()
        parts: List[str] = []
        plan = self._conv.deathmatch_plan or {}
        done_steps = [s for s in (plan.get("steps") or []) if s.get("status") == "done"]
        if done_steps:
            parts.append(
                "已完成步骤（已定案——除非出现新证据，不得据此推翻）:\n"
                + "\n".join(
                    # A4.9 r2 Minor: clamp the id — an LLM-authored 400-char id
                    # would otherwise eat the evidence budget and push the
                    # placeholder scan out of the pack.
                    f"- {_truncate(str(s.get('id') or ''), 40)}: {_truncate(str(s.get('description') or ''), 80)}"
                    f" → {_truncate(str(s.get('output_summary') or '(无摘要)'), 120)}"
                    for s in done_steps[:8]
                )
            )
            if len(done_steps) > 8:
                parts.append(
                    f"（已完成步骤共 {len(done_steps)} 个，按计划序仅列前 8 个；"
                    f"其余未展示 ≠ 不存在）"
                )
        # 2026-09-20 placeholder gate (conv a104fbc5), r7: the judge must see
        # the actual deliverable content (not just filenames/summaries) to
        # rule on unexecuted placeholders; feed it the bounded full-text
        # excerpt BEFORE the (potentially huge) workspace listing — the pack
        # is tail-truncated at _JUDGE_EVIDENCE_CHARS, so a late excerpt would
        # silently vanish on large workspaces.
        if workspace_path:
            try:
                _outputs: List[str] = []
                for s in (plan.get("steps") or []):
                    for fp in (s.get("output_files") or s.get("writes") or []):
                        p = str(fp or "").strip()
                        if p and p not in _outputs:
                            _outputs.append(p)
                _excerpt = _read_deliverable_excerpt(
                    workspace_path, _outputs, max_chars=2400
                )
                if _excerpt:
                    parts.append(
                        _excerpt
                        + "\n（评审侧为有界摘录；完整判定由完成闸门的 agentic 占位检查执行）"
                    )
            except Exception as exc:
                logger.debug("judge evidence: deliverable excerpt failed: %s", exc)
        if workspace_path:
            try:
                files = self._workspace_file_snapshot(workspace_path)
                parts.append("工作区文件快照:\n" + self._format_workspace_listing(
                    files, total=getattr(self, "_last_snapshot_total", len(files))))
            except Exception as exc:
                logger.debug("judge evidence: workspace snapshot failed: %s", exc)
        prev = self._conv.deathmatch_last_verification_result or {}
        if prev:
            parts.append(
                f"上轮验证: status={prev.get('status')}, "
                f"issues={_truncate(str(prev.get('issues') or []), 200)}"
            )
        # P1-5: settled verdicts (step completions + reconcile overturns) —
        # the judge must not flip them without new evidence.
        _settled = self._build_settled_block()
        if _settled:
            parts.append(_settled)
        # AEWM B1: 证伪台账——judge 不得把被推翻的断言当事实（anti-contamination）。
        _retracted = self._retracted_claims_block()
        if _retracted:
            parts.append(_retracted)
        if tool_results:
            trace = "\n".join(
                f"[{getattr(tr, 'name', '?')}] {_truncate(str(getattr(tr, 'result', '') or ''), 200)}"
                for tr in tool_results[-6:]
            )
            if trace.strip():
                parts.append("本轮工具调用:\n" + trace)
        ev = "\n\n".join(p for p in parts if p.strip())
        if len(ev) > _JUDGE_EVIDENCE_CHARS:
            # 2026-09-20：头+尾（旧头截断会丢工具轨迹/上轮验证等尾部证据）
            ev = _head_tail_truncate(ev, _JUDGE_EVIDENCE_CHARS)
        # W2 D1（ACG）：证据单元块尾部注入（块级预算准入——整块入或整体弃）。
        # 注入点在 head-tail 截断之后：块不被 3000 字符预算半截吞掉（C5 前置
        # 依赖：上下文计划 W1 保真三修管续跑链 serializer，此处管本包）。
        _pack = ev
        _units_block = self._build_evidence_units_block("judge")
        if _units_block:
            ev = f"{ev}\n\n{_units_block}" if ev.strip() else _units_block
        # judge 链归档点：产出后归档本包证据摘要（截 300）。跨步骤证据包无
        # 单一需求声明 → 歧义保持未归因（ACG §3.3：不用语义相似补归属）。
        self._record_evidence_unit(
            "OBSERVED", "judge", f"judge证据包摘要: {_truncate(_pack, 260)}",
            step_id="", attributed=False,
        )
        return ev
    def _build_continuity_anchor(self) -> str:
        """Build the <deathmatch_continuity> anchor block from the last
        verifier's distilled continuity_brief.

        Falls back to the most recent plan step that carries a brief, so
        turns where the verifier had nothing new to distill (empty brief)
        do not lose the anchor. Returns an empty string when anchoring is
        disabled or no brief exists anywhere.
        """
        _sync_deathmatchplancoremixin_globals()
        if not config.deathmatch_continuity_anchor_enabled:
            return ""
        prev = self._conv.deathmatch_last_verification_result or {}
        brief = str(prev.get("continuity_brief") or "").strip()
        if not brief:
            plan = self._conv.deathmatch_plan or {}
            for s in reversed((plan.get("steps") or [])):
                sb = str(s.get("continuity_brief") or "").strip()
                if sb:
                    brief = sb
                    break
        if not brief:
            return ""
        return (
            "<deathmatch_continuity>\n"
            "上一轮验证器提炼的内容连续性锚点——本轮产出必须与这些保持一致，"
            "严禁偏离目标要求的风格/人物/设定/情节/格式/事实；"
            "锚点只是内容事实总结，如与目标冲突，一律以目标为准：\n"
            f"{brief}\n"
            "</deathmatch_continuity>"
        )
    def _read_recent_step_tail(self, steps: List[Dict[str, Any]], max_tail_chars: int = 400) -> str:
        """Read the ENDING of the most recently completed step's first file.

        For sequential long-form content the previous output's ending is the
        primary continuity point — the executor sees where the story left off
        without an extra workspace_read round-trip.
        """
        _sync_deathmatchplancoremixin_globals()
        ws = getattr(self, "_workspace_path", "") or ""
        if not ws:
            return ""
        done_steps = [s for s in steps if s.get("status") == "done" and s.get("output_files")]
        if not done_steps:
            return ""
        latest = done_steps[-1]
        for fp in (latest.get("output_files") or []):
            abs_path = _os.path.join(ws, fp) if not _os.path.isabs(fp) else fp
            if not _os.path.isfile(abs_path) or not self._is_text_file(fp):
                continue
            try:
                size = _os.path.getsize(abs_path)
                if size <= 0:
                    continue
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                # Char-based slicing; byte seek would land mid-CJK-character.
                if len(content) > max_tail_chars:
                    return content[-max_tail_chars:].lstrip("\n")
                return content
            except Exception:
                continue
        return ""
    async def compress_messages(
        self,
        messages: List[Dict[str, Any]],
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Compress a message list using ContextCompressor."""
        _sync_deathmatchplancoremixin_globals()
        try:
            from app.services.context_compressor import ContextCompressor
            compressor = ContextCompressor(quiet=True)
            # D4 统一投递：死亡模式直接构造的 compressor 也走同一 working-note
            # compact/resume 语义，不能绕过 vibeweaver delivery（普通 loop 已接）。
            compressor.delivery_root = getattr(self, "_workspace_path", "") or ""
            compressor.delivery_conv_id = str(getattr(getattr(self, "_conv", None), "id", "") or "")
            active = getattr(self, "vibeweaver_active", None)
            if active is None:
                active = getattr(self._conv, "vibeweaver_active", None)
            if active is None:
                # D9: `auto` needs a per-turn activation decision; absence of
                # that decision must NOT be treated as active. Only `always`
                # is unconditionally active. Conservative default prevents a
                # non-coding deathmatch turn from injecting a stale project's
                # working note.
                from app.core.config import get_config
                active = get_config().vibeweaver_mode == "always"
            compressor.delivery_active = bool(active)
            compressed = await compressor.compress_async(messages, focus_topic=focus_topic)
            return compressed
        except Exception as exc:
            logger.warning("Deathmatch context compression failed: %s", exc)
            return messages
    def build_context_messages(
        self,
        base_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Inject compressed context summary from previous rounds as background."""
        _sync_deathmatchplancoremixin_globals()
        summary = self._conv.deathmatch_context_summary
        if not summary:
            return base_messages
        system_injection = (
            "[跨轮死磕上下文摘要 — 仅作背景参考]\n"
            f"{summary}\n\n"
            "以上摘要是之前死磕任务的背景，请结合当前用户的新请求继续工作。"
        )
        # Insert after the first system message if present, else prepend as user.
        result = []
        inserted = False
        for i, m in enumerate(base_messages):
            result.append(m)
            if not inserted and m.get("role") == "system":
                result.append({"role": "user", "content": system_injection})
                inserted = True
        if not inserted:
            result.insert(0, {"role": "user", "content": system_injection})
        return result
    async def compress_conversation_context(
        self,
        db: AsyncSession,
    ) -> str:
        """Compress the full conversation message history and store summary."""
        _sync_deathmatchplancoremixin_globals()
        from app.db.database import Message

        try:
            if not getattr(self, "_workspace_path", ""):
                from app.services.workspace_service import ensure_user_workspace
                workspace = await ensure_user_workspace(db, self._conv.user_id)
                self._workspace_path = str(workspace.root_path or "")
            result = await db.execute(
                select(Message)
                .where(Message.conversation_id == self._conv.id,
                       Message.delivery_status != "streaming")
                .order_by(Message.created_at)
            )
            msgs = result.scalars().all()
            # 评审 I3：外置桩还原全文（否则 provide_file 收割读到 stub JSON）
            from app.services.message_payload_service import resolve_message_fields
            msgs = await resolve_message_fields(list(msgs), db)
            messages = []
            # conv a040c24e (D-12): neutralize historical [N] citation markers
            # on assistant rows before they re-enter the goal-loop context.
            from app.services.tool_history import neutralize_historical_citations
            for m in msgs:
                content = self.strip_markers(m.content or "")
                if not content.strip():
                    continue
                if m.role == "assistant":
                    content = neutralize_historical_citations(content)
                messages.append({"role": m.role, "content": content})
            if len(messages) <= 3:
                summary = "\n\n".join(f"[{m['role']}] {m['content']}" for m in messages)
            else:
                compressed = await self.compress_messages(messages, focus_topic=self._conv.deathmatch_goal or "")
                summary = "\n\n".join(f"[{m['role']}] {m['content']}" for m in compressed)
            self._conv.deathmatch_context_summary = summary
            # B1: append the structured handoff document so the audit state
            # (plan progress / continuity / reflections / telemetry) survives
            # compression even if the lossy summary drops details.
            handoff = self.generate_handoff_document()
            if handoff:
                self._conv.deathmatch_context_summary = (
                    summary
                    + "\n\n[死磕模式交接文档 — 压缩后必须保留的审计状态]\n"
                    + handoff
                )
            self._conv.deathmatch_compressed_context = json.dumps(messages, ensure_ascii=False)
            await db.flush()
            return summary
        except Exception as exc:
            logger.warning("Failed to compress conversation context: %s", exc)
            return ""
    def generate_handoff_document(self) -> str:
        """B1: structured handoff document — the audit-bearing state that
        must survive compression / context resets: goal, plan progress,
        continuity anchor, recent reflections and telemetry. Appended to the
        compressed summary so the post-compression agent never loses the
        acceptance criteria (the #1 long-horizon failure mode: compaction
        drops the audit requirement and the agent completes on local
        evidence alone)."""
        _sync_deathmatchplancoremixin_globals()
        parts = []
        goal = self._conv.deathmatch_goal or ""
        if goal:
            parts.append(f"目标:\n{_truncate(goal, 2000)}")
        # W1a: the acceptance criteria are audit state — they must survive
        # compression exactly like the goal (append-only contract).
        _crit_block = _format_criteria_block(self._all_criteria())
        if _crit_block:
            parts.append(_crit_block)
        plan = self._conv.deathmatch_plan or {"steps": []}
        steps = plan.get("steps") or []
        if steps:
            lines = ["计划进度:"]
            for s in steps:
                st = s.get("status", "pending")
                mark = "✅" if st == "done" else "⬜"
                lines.append(
                    f"- {mark} [{s.get('id', '?')}] {_truncate(s.get('description', ''), 120)}"
                    f"{( '— ' + s.get('output_summary', '')[:100]) if s.get('output_summary') else ''}"
                )
            parts.append("\n".join(lines))
        continuity = self._build_continuity_anchor()
        if continuity:
            parts.append(continuity)
        reflections = self._conv.deathmatch_reflections or []
        if reflections:
            recent = reflections[-3:]
            lines = ["近期反思:"]
            for r in recent:
                lines.append(f"- {str(r.get('summary', r))[:200]}")
            parts.append("\n".join(lines))
        telemetry = self._build_telemetry_block()
        if telemetry:
            parts.append(telemetry)
        # Hard cap: the handoff is injected on every deathmatch chat request
        # via build_context_messages, so bound its size (A4.9 Important 6).
        return _truncate("\n\n".join(parts), 4000)
