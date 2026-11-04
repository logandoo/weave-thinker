# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from deathmatch_service.py (DeathmatchGrillingFlow, 2026-10-20).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``deathmatch_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (
    'DEFAULT_MAX_GRILLING_ROUNDS',
    'DEFAULT_QUESTIONS_PER_ROUND',
    'GRILLING_FACT_RESEARCH_PROMPT',
    'GRILLING_QUESTION_GENERATION_PROMPT',
    'GRILLING_ROUND_SYNTHESIS_PROMPT',
    'GRILLING_SYNTHESIS_PROMPT',
    '_HARNESS_REPAIR_COUNTS',
    '_JSON_OBJECT_RE',
    '_assumed_defaults_block',
    '_filter_grilling_questions',
    '_parse_grilling_readiness',
    '_readiness_block_for_questions',
    '_sanitize_research_field',
    '_truncate',
    'config',
    'datetime',
    'json',
    'logger',
    'select',
    'update',
    'uuid',
)


def _sync_deathmatchgrillingflowmixin_globals() -> None:
    from app.services import deathmatch_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class DeathmatchGrillingFlowMixin:
    def _max_grilling_rounds(self) -> int:
        _sync_deathmatchgrillingflowmixin_globals()
        return int(config.deathmatch.get("max_grilling_rounds", DEFAULT_MAX_GRILLING_ROUNDS))
    def _questions_per_round(self) -> int:
        _sync_deathmatchgrillingflowmixin_globals()
        return int(config.deathmatch.get("questions_per_round", DEFAULT_QUESTIONS_PER_ROUND))
    def _current_qa_history(self) -> List[Dict[str, Any]]:
        _sync_deathmatchgrillingflowmixin_globals()
        raw = self._conv.deathmatch_grilling_qa_history
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        try:
            return json.loads(raw) if isinstance(raw, str) else []
        except Exception:
            return []
    def _add_to_qa_history(self, round_number: int, qa_pairs: List[Dict[str, str]]) -> None:
        _sync_deathmatchgrillingflowmixin_globals()
        history = self._current_qa_history()
        history.append({"round": round_number, "qa_pairs": qa_pairs})
        self._conv.deathmatch_grilling_qa_history = history
    def _format_history_for_prompt(self, newest_first: bool = False) -> str:
        _sync_deathmatchgrillingflowmixin_globals()
        history = self._current_qa_history()
        if not history:
            return "此前尚未回答问题。"
        parts = ["## 已回答的所有问题（严禁重复以下任何问题或角度！）"]
        for h in (reversed(history) if newest_first else history):
            if isinstance(h, dict) and (
                "goal_contract_pending" in h
                or (
                    "resume_correction" in h
                    and not h.get("qa_pairs")
                    and not isinstance(h.get("readiness"), dict)
                )
            ):
                continue  # internal correction/epoch metadata, not a Q&A row
            round_num = h.get("round", "?")
            qa_pairs = list(h.get("qa_pairs", []))
            if newest_first:
                qa_pairs.reverse()
            if not qa_pairs and isinstance(h.get("readiness"), dict):
                # A4.9 Minor：readiness 兄弟条目不渲染成空的"第N轮回答"。
                rep = h["readiness"]
                summary_bits = []
                if rep.get("open_decisions"):
                    summary_bits.append("未决: " + "、".join(rep["open_decisions"][:5]))
                if rep.get("assumed_defaults"):
                    summary_bits.append("默认假设: " + "、".join(rep["assumed_defaults"][:5]))
                parts.append(
                    f"\n--- 第{round_num}轮判读 ---"
                    + ("；".join(summary_bits) if summary_bits else "（无未决决策）")
                )
                continue
            parts.append(f"\n--- 第{round_num}轮回答（本轮已覆盖的角度）---")
            for pair in qa_pairs:
                dim = pair.get("dimension") or ""
                dim_part = f"（维度: {dim}）" if dim else ""
                parts.append(f"  问题{dim_part}: {pair.get('question', '')}")
                parts.append(f"  回答: {pair.get('answer', '')}")
            # Hint at what dimensions this round covered to help progressive deepening
            covered = ", ".join(
                (pair.get("dimension") or pair.get("question", "")[:40])
                for pair in qa_pairs
            )
            parts.append(f"  → 本轮已触及: {covered}")
        parts.append(
            "\n请确保本轮生成的问题：\n"
            "1) 与以上所有已问问题在角度上有本质区别\n"
            "2) 基于以上回答中暴露的未明确之处深入追问\n"
            "3) 如果以上回答已经非常完整，可以精简问题数量"
        )
        return "\n".join(parts)
    def _record_grilling_readiness(self, round_number: int, report: Dict[str, Any]) -> None:
        """Persist the round readiness report into the QA history (no schema
        change: a sibling entry without ``qa_pairs``)."""
        _sync_deathmatchgrillingflowmixin_globals()
        history = self._current_qa_history()
        history.append({"round": round_number, "readiness": report})
        self._conv.deathmatch_grilling_qa_history = history
    def _latest_readiness(self) -> Dict[str, Any]:
        _sync_deathmatchgrillingflowmixin_globals()
        for entry in reversed(self._current_qa_history()):
            if isinstance(entry, dict) and isinstance(entry.get("readiness"), dict):
                return entry["readiness"]
        return {}

    async def _ensure_grilling_readiness_for_contract(self, db: AsyncSession) -> bool:
        """Backfill the current round's readiness after active is visible.

        Early-stop rounds already have this report. Final-round and zombie
        paths defer it so their LLM call cannot hide the active transition.
        """
        _sync_deathmatchgrillingflowmixin_globals()
        current_round = self._conv.deathmatch_grilling_round or 1
        for entry in reversed(self._current_qa_history()):
            if isinstance(entry, dict) and isinstance(entry.get("readiness"), dict):
                if entry.get("round") == current_round:
                    return False
                break
        from app.db.database import AgentTask
        result = await db.execute(
            select(AgentTask)
            .where(
                AgentTask.conversation_id == self._conv.id,
                AgentTask.task_type == "grilling",
                AgentTask.status == "completed",
            )
            .order_by(AgentTask.created_at)
        )
        completed_tasks = result.scalars().all()
        answered = [
            pair for pair in (self._qa_pair_from_task(task) for task in completed_tasks)
            if pair.get("answer")
        ]
        if len(answered) < 2:
            return False

        # Zombie recovery can start with empty QA history even though the
        # completed task rows contain the real answers. Seed missing pairs
        # before judging so readiness is grounded in recovered decisions.
        history = self._current_qa_history()
        existing = {
            (
                entry.get("round"), pair.get("question_id"),
                pair.get("question"), pair.get("answer"),
            )
            for entry in history if isinstance(entry, dict)
            for pair in (entry.get("qa_pairs") or [])
            if isinstance(pair, dict)
        }
        missing_by_round: Dict[int, List[Dict[str, Any]]] = {}
        for pair in answered:
            round_number = int(pair.get("round") or current_round)
            key = (
                round_number, pair.get("question_id"),
                pair.get("question"), pair.get("answer"),
            )
            if key not in existing:
                missing_by_round.setdefault(round_number, []).append(pair)
                existing.add(key)
        for round_number in sorted(missing_by_round):
            history.append({
                "round": round_number,
                "qa_pairs": missing_by_round[round_number],
            })
        if missing_by_round:
            self._conv.deathmatch_grilling_qa_history = history

        report = await self._grilling_readiness_report(db)
        if report.get("_failed"):
            return False
        self._record_grilling_readiness(current_round, report)
        return True

    async def _refine_goal_with_readiness_if_needed(self, db: AsyncSession) -> bool:
        """Retry goal refinement until all current readiness defaults persist."""
        _sync_deathmatchgrillingflowmixin_globals()
        defaults = self._latest_readiness().get("assumed_defaults") or []
        required = [str(item) for item in defaults if str(item).strip()]
        current_goal = str(self._conv.deathmatch_goal or "")
        if not required:
            return False
        refined_goal = await self._synthesize_goal_from_answers(db)
        if not refined_goal or refined_goal == current_goal:
            # Deterministic equality is the convergence condition. Requiring
            # every full default string would loop forever when the bounded
            # goal legitimately truncates the tail.
            return False
        return self._replace_goal_and_invalidate_derived(refined_goal)

    def _replace_goal_and_invalidate_derived(
        self, refined_goal: str, *, force: bool = False,
    ) -> bool:
        """Atomically retire every artifact derived from the previous goal."""
        _sync_deathmatchgrillingflowmixin_globals()
        refined_goal = str(refined_goal or "").strip()
        if not refined_goal:
            return False
        if not force and refined_goal == str(self._conv.deathmatch_goal or ""):
            return False
        self._conv.deathmatch_goal = refined_goal
        self._conv.deathmatch_acceptance_criteria = None
        self._conv.deathmatch_plan = None
        self._conv.deathmatch_bible_draft = None
        self._conv.deathmatch_last_verification_result = None
        self._conv.deathmatch_settled_ledger = None
        self._conv.deathmatch_plan_version = (
            getattr(self._conv, "deathmatch_plan_version", 0) or 0
        ) + 1
        # Pending epoch lives outside write-through columns: plan and its
        # consumption therefore commit atomically on the surrounding session.
        history = list(self._current_qa_history())
        history.append({
            "goal_contract_pending": True,
            "plan_version": self._conv.deathmatch_plan_version,
        })
        self._conv.deathmatch_grilling_qa_history = history
        self._record_event(
            "goal_invalidated",
            plan_version=self._conv.deathmatch_plan_version,
        )
        return True

    async def incorporate_resume_correction(self, db: AsyncSession, correction: str) -> bool:
        """Persist the latest substantive resume input before contract rebuild."""
        _sync_deathmatchgrillingflowmixin_globals()
        text = str(correction or "").strip()
        if not text:
            return False
        # Copy before append: SQLAlchemy plain JSON cannot track same-list mutation.
        history = list(self._current_qa_history())
        latest = next((
            str(entry.get("resume_correction") or "")
            for entry in reversed(history)
            if isinstance(entry, dict) and entry.get("resume_correction")
        ), "")
        added = latest != text
        if added:
            history.append({
                "round": self._conv.deathmatch_grilling_round or 1,
                "resume_correction": text,
            })
            self._conv.deathmatch_grilling_qa_history = history
        refined_goal = await self._synthesize_goal_from_answers(db)
        return self._replace_goal_and_invalidate_derived(
            refined_goal, force=added,
        )

    def complete_grilling(self, goal: str) -> None:
        _sync_deathmatchgrillingflowmixin_globals()
        self._conv.deathmatch_status = "active"
        self._conv.deathmatch_grilling_complete = True
        self._conv.deathmatch_goal = goal
        self._conv.deathmatch_turns = 0
        self._conv.deathmatch_consecutive_failures = 0
        self._conv.deathmatch_max_turns = config.deathmatch_max_turns
        # PEVR: start wall-clock budget and reset plan/reflection state.
        from datetime import datetime
        self._conv.deathmatch_wall_time_started_at = datetime.utcnow()
        self._conv.deathmatch_max_wall_time_seconds = config.deathmatch_max_wall_time_seconds
        self._conv.deathmatch_plan = None
        self._conv.deathmatch_plan_version = 0
        self._conv.deathmatch_reflections = []
        self._conv.deathmatch_verify_failures = 0
        _HARNESS_REPAIR_COUNTS.pop(self._conv.id, None)  # C2: progress resets repair budget
        self._conv.deathmatch_last_verification_result = None
        self._conv.deathmatch_human_gate = None
        # P1-5: a new goal starts with a clean settled ledger.
        self._conv.deathmatch_settled_ledger = None
        # W1a/W2b/W2c: a new goal = a new contract — criteria, failed
        # directions, pause packet and the no-progress breaker all reset.
        self._conv.deathmatch_acceptance_criteria = None
        self._conv.deathmatch_failed_directions = None
        self._conv.deathmatch_pause_state = None
        self._conv.deathmatch_no_progress_replans = 0
        self._conv.deathmatch_events = None
    async def try_recover_stalled_grilling(self, db: AsyncSession) -> bool:
        """Detect and recover zombie grilling state.

        When the grilling phase is interrupted during or after the final
        round (all tasks answered but goal synthesis never completed),
        the status stays ``"grilling"`` with no running agent and no
        pending tasks — a zombie.  This method detects that state,
        synthesizes the goal, completes grilling, and transitions to
        ``"active"`` so the goal loop can start.

        Returns True if recovery was performed (grilling is now complete).
        """
        _sync_deathmatchgrillingflowmixin_globals()
        from app.db.database import AgentTask

        if not self.is_grilling:
            return False

        current_round = self._conv.deathmatch_grilling_round or 1
        max_rounds = self._conv.deathmatch_grilling_round_total or self._max_grilling_rounds()

        # Only trigger recovery when ALL grilling rounds are done.
        # If current_round < max_rounds, completed tasks from earlier
        # rounds are normal — we should generate next-round questions,
        # not synthesize the goal.
        if current_round < max_rounds:
            return False

        stmt = (
            select(AgentTask)
            .where(
                AgentTask.conversation_id == self._conv.id,
                AgentTask.task_type == "grilling",
                AgentTask.status == "pending",
            )
        )
        result = await db.execute(stmt)
        pending_tasks = result.scalars().all()
        if pending_tasks:
            return False

        stmt = (
            select(AgentTask)
            .where(
                AgentTask.conversation_id == self._conv.id,
                AgentTask.task_type == "grilling",
                AgentTask.status == "completed",
            )
            .order_by(AgentTask.created_at)
        )
        result = await db.execute(stmt)
        completed_tasks = result.scalars().all()
        if not completed_tasks:
            return False

        # Readiness is backfilled by the kickoff lazy contract after the
        # recovered active frame; criteria/plan still consume its defaults.
        goal = await self._synthesize_goal_from_answers(db)
        # Bible drafting (including creative detection) is also lazy work and
        # must not hide the completed grilling stage behind another LLM call.
        self.complete_grilling(goal)
        # Criteria/plan are lazy-generated by the kickoff stream after the
        # initial active frame, keeping zombie recovery visible immediately.
        await db.flush()
        logger.info(
            "Deathmatch: zombie grilling recovered for conversation %s "
            "(round %d/%d, %d completed tasks) → goal loop active",
            self._conv.id, current_round, max_rounds, len(completed_tasks),
        )
        return True
    async def generate_grilling_questions(
        self,
        query: str,
        db: AsyncSession,
        user_id: str,
        assistant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Generate one round of clarification questions (2-3) based on the
        current round and previously collected answers."""
        _sync_deathmatchgrillingflowmixin_globals()
        from app.services.llm_service import LLMService
        from app.db.database import AgentTask

        current_round = self._conv.deathmatch_grilling_round or 1
        max_rounds = self._conv.deathmatch_grilling_round_total or self._max_grilling_rounds()
        questions_per_round = self._questions_per_round()

        summary = (self._conv.deathmatch_context_summary or "").strip()
        if summary:
            # Cap the summary so the grilling prompt stays within a reasonable
            # size while still giving the question generator access to prior context.
            if len(summary) > 6000:
                summary = summary[:6000] + "\n...[截断]"
            previous_context = f"此前对话的上下文摘要（供参考）：\n{summary}\n\n"
        else:
            previous_context = ""

        prompt = GRILLING_QUESTION_GENERATION_PROMPT.format(
            query=query,
            round=current_round,
            max_rounds=max_rounds,
            questions_per_round=questions_per_round,
            history_text=self._format_history_for_prompt(),
            previous_context=previous_context,
            research_block=await self._grilling_research_block(query),
            readiness_block=_readiness_block_for_questions(self._latest_readiness()),
        )

        # E4（2026-09-14，默认关）：grilling 前注入小预算用户记忆（fail-open）
        try:
            from app.services import memory_task_context as _mtc
            if _mtc.deathmatch_memory_enabled():
                _mem_block = await _mtc.build_task_memory_context(
                    db, user_id, query,
                    budget_chars=int(config.deathmatch.get("memory_injection_budget_chars", 600)))
                if _mem_block:
                    prompt = f"{_mem_block}\n\n{prompt}"
        except Exception:
            logger.debug("deathmatch grilling memory injection failed (fail-open)", exc_info=True)

        MAX_RETRIES = 3
        questions = []
        last_raw = ""

        for attempt in range(MAX_RETRIES):
            temperature = 0.3 + (attempt * 0.2)

            llm = self._make_llm()
            messages = [{"role": "user", "content": prompt}]

            raw = ""
            try:
                stream = llm.stream_chat_structured(
                    messages, temperature=temperature, tools=None,
                    extra_body={},
                )
                async for event in stream:
                    if event["type"] == "content":
                        raw += event["data"]
                    elif event["type"] == "error":
                        logger.warning(
                            "deathmatch grilling question generation error (attempt %d/%d): %s",
                            attempt + 1, MAX_RETRIES, event["data"],
                        )
                        break
            except Exception as exc:
                logger.exception(
                    "deathmatch grilling question generation failed (attempt %d/%d): %s",
                    attempt + 1, MAX_RETRIES, exc,
                )
                raw = ""

            questions = self._parse_grilling_questions(raw)
            if questions:
                questions = _filter_grilling_questions(
                    questions, self._current_qa_history(),
                )
            if questions:
                last_raw = raw
                break
            last_raw = raw

        if not questions:
            logger.error(
                "deathmatch grilling question generation: all %d retries failed for conv=%s query=%r last_raw=%r",
                MAX_RETRIES, self._conv.id, query[:200], last_raw[:500],
            )
            raise ValueError(
                f"无法为当前目标生成盘问问题（{MAX_RETRIES}次重试均失败），请关闭死磕模式后直接提问"
            )
        questions = questions[:questions_per_round]

        # Clear previous pending grilling tasks for this conversation so only
        # the current round questions are active.
        await db.execute(
            update(AgentTask)
            .where(
                AgentTask.conversation_id == self._conv.id,
                AgentTask.task_type == "grilling",
                AgentTask.status == "pending",
            )
            .values(status="cancelled")
        )

        created_tasks = []
        for q in questions:
            task_id = str(uuid.uuid4())
            task = AgentTask(
                id=task_id,
                user_id=user_id,
                conversation_id=self._conv.id,
                assistant_id=assistant_id,
                title=f"盘问: {q['question'][:50]}",
                task_type="grilling",
                goal=q["question"],
                context=json.dumps({
                    "question_id": q["id"],
                    "question": q["question"],
                    "recommendation": q.get("recommendation", ""),
                    "options": q.get("options", []),
                    "dimension": q.get("dimension", ""),
                    "depends_on": q.get("depends_on", []),
                    "re_ask_of": q.get("re_ask_of", ""),
                    "deepens": q.get("deepens", ""),
                    "original_query": query,
                    "grilling_round": current_round,
                }, ensure_ascii=False),
                status="pending",
                progress=0.0,
                iterations_done=0,
                iterations_max=1,
            )
            db.add(task)
            created_tasks.append({
                "task_id": task_id,
                "question_id": q["id"],
                "question": q["question"],
                "recommendation": q.get("recommendation", ""),
                "options": q.get("options", []),
                "round": current_round,
            })

        self._conv.deathmatch_grilling_total = len(questions)
        self._conv.deathmatch_grilling_completed = 0
        await db.flush()

        logger.info(
            "Deathmatch: generated %d grilling questions (round %d/%d) for conversation %s",
            len(questions), current_round, max_rounds, self._conv.id,
        )
        return created_tasks
    def _parse_grilling_questions(self, raw: str) -> List[Dict[str, str]]:
        _sync_deathmatchgrillingflowmixin_globals()
        if not raw or not raw.strip():
            return []
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1:]
        data = None
        try:
            data = json.loads(text)
        except Exception:
            match = _JSON_OBJECT_RE.search(text)
            if match:
                try:
                    data = json.loads(match.group(0))
                except Exception:
                    data = None
        if not isinstance(data, dict):
            return []
        from app.prompts.contracts import GrillingQuestions, parse_contract

        _gq, _gq_failure = parse_contract(GrillingQuestions, json.dumps(data))
        if _gq_failure is not None or _gq is None:
            logger.warning("grilling contract failed: %s",
                           _gq_failure.kind if _gq_failure else "schema")
            return []
        questions = data.get("questions", [])
        if not isinstance(questions, list):
            return []
        result = []
        for i, q in enumerate(questions):
            if isinstance(q, dict) and q.get("question"):
                opts = q.get("options", [])
                if not isinstance(opts, list):
                    opts = []
                opts = [str(o) for o in opts if o]
                deps = q.get("depends_on", [])
                if not isinstance(deps, list):
                    deps = []
                result.append({
                    "id": q.get("id", f"q{i+1}"),
                    "question": str(q["question"]),
                    "recommendation": str(q.get("recommendation", "")),
                    "options": opts,
                    "dimension": str(q.get("dimension", "") or ""),
                    "depends_on": [str(d) for d in deps if str(d).strip()],
                    "re_ask_of": str(q.get("re_ask_of", "") or ""),
                    "deepens": str(q.get("deepens", "") or ""),
                })
        return result
    async def complete_grilling_question(
        self,
        task_id: str,
        answer: str,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """Mark a single grilling subagent as completed with the user's answer.
        This method is kept for backward compatibility; the new round-based UI
        uses submit_grilling_round instead."""
        _sync_deathmatchgrillingflowmixin_globals()
        from app.db.database import AgentTask

        task = await db.get(AgentTask, task_id)
        if task is None or task.task_type != "grilling":
            return {"status": "error", "message": "grilling task not found"}
        if task.status == "completed":
            return {"status": "error", "message": "question already answered"}

        task.status = "completed"
        task.result = answer
        task.progress = 1.0
        task.completed_at = datetime.utcnow()

        self._conv.deathmatch_grilling_completed = (
            (self._conv.deathmatch_grilling_completed or 0) + 1
        )
        await db.flush()

        completed_count = self._conv.deathmatch_grilling_completed
        total_count = self._conv.deathmatch_grilling_total or 0

        logger.info(
            "Deathmatch: grilling question completed (%d/%d) for conversation %s",
            completed_count, total_count, self._conv.id,
        )

        if completed_count >= total_count and total_count > 0:
            # Auto-advance: this single-question API now drives the round flow.
            return await self._finish_grilling_round(db)

        return {
            "status": "grilling_in_progress",
            "completed": completed_count,
            "total": total_count,
        }
    def _qa_pair_from_task(self, task: Any) -> Dict[str, Any]:
        _sync_deathmatchgrillingflowmixin_globals()
        ctx = {}
        if task.context:
            try:
                ctx = json.loads(task.context)
            except Exception:
                pass
        return {
            "question_id": ctx.get("question_id", ""),
            "question": ctx.get("question", task.goal or ""),
            "recommendation": ctx.get("recommendation", ""),
            "answer": task.result or "",
            "dimension": ctx.get("dimension", ""),
            "re_ask_of": ctx.get("re_ask_of", ""),
            # 与旧实现一致：缺失 grilling_round 键的遗留任务按第 1 轮归档
            #（否则每轮 _finish 都会重复收集历史任务）。
            "round": ctx.get("grilling_round", 1),
        }
    async def _finish_grilling_round(self, db: AsyncSession) -> Dict[str, Any]:
        """Advance to the next grilling round or synthesize the final goal.

        Shared between per-question and round-based submission flows.
        """
        _sync_deathmatchgrillingflowmixin_globals()
        from app.db.database import AgentTask

        if not self.is_grilling:
            return {"status": "error", "message": "not in grilling phase"}

        current_round = self._conv.deathmatch_grilling_round or 1

        # Collect completed tasks for the current round.
        stmt = (
            select(AgentTask)
            .where(
                AgentTask.conversation_id == self._conv.id,
                AgentTask.task_type == "grilling",
                AgentTask.status == "completed",
            )
            .order_by(AgentTask.created_at)
        )
        result = await db.execute(stmt)
        completed_tasks = result.scalars().all()

        qa_pairs = []
        for task in completed_tasks:
            pair = self._qa_pair_from_task(task)
            if pair.get("round") != current_round:
                continue
            qa_pairs.append(pair)

        self._add_to_qa_history(current_round, qa_pairs)
        await db.flush()
        return await self._advance_or_finish_round(db, qa_pairs)
    async def _advance_or_finish_round(
        self, db: AsyncSession, qa_pairs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Shared round tail: readiness-gated advance vs goal synthesis.

        A1: both the per-question path and the round-based path consult the
        same readiness report (previously only the per-question path did, and
        ``submit_grilling_round`` carried a TODO and always ran out the
        configured rounds). Minimum-round floor (A4.9 Important 5): never
        consult the LLM before round 2 or with zero answers.
        """
        _sync_deathmatchgrillingflowmixin_globals()
        current_round = self._conv.deathmatch_grilling_round or 1
        max_rounds = self._conv.deathmatch_grilling_round_total or self._max_grilling_rounds()

        should_continue = current_round < max_rounds
        report: Optional[Dict[str, Any]] = None
        # Early-stop decisions still need readiness now. At the round ceiling
        # it cannot change the transition, so the lazy contract backfills it
        # after active instead of hiding completion behind another LLM call.
        if current_round >= 2 and qa_pairs and should_continue:
            report = await self._grilling_readiness_report(db)
            if not report.get("_failed"):
                self._record_grilling_readiness(current_round, report)
            if should_continue:
                should_continue = bool(report.get("should_continue", True))
                if report.get("diminishing_returns") and not report.get("open_decisions"):
                    should_continue = False

        if should_continue:
            self._conv.deathmatch_grilling_round = current_round + 1
            self._conv.deathmatch_grilling_total = 0
            self._conv.deathmatch_grilling_completed = 0
            await db.flush()

            original_query = await self._extract_original_query(db)
            # A4.9 Important 1：出题失败（含机械过滤清空）不得 500 主 UI 提交
            # 路径（会回滚用户已提交的答案）——降级为按现有回答收束。
            try:
                questions = await self.generate_grilling_questions(
                    query=original_query,
                    db=db,
                    user_id=self._conv.user_id,
                    assistant_id=self._conv.assistant_id,
                )
            except Exception as exc:
                logger.warning(
                    "grilling question generation failed (%s) — degrading to "
                    "goal synthesis with the answers collected so far", exc,
                )
                self._record_event(
                    "grilling_questions_failed",
                    reason=str(exc)[:200], round=current_round,
                )
                questions = None
                # 恢复被预增的轮次/计数——收束状态不留虚高轮次（r2 cosmetic）。
                self._conv.deathmatch_grilling_round = current_round
                self._conv.deathmatch_grilling_total = 0
                self._conv.deathmatch_grilling_completed = 0
            if questions:
                await db.commit()
                return {
                    "status": "next_round",
                    "round": self._conv.deathmatch_grilling_round,
                    "max_rounds": max_rounds,
                    "grilling_completed": 0,
                    "grilling_total": len(questions),
                    "questions": questions,
                }
            # Degrade: fall through to synthesis with what we have.

        try:
            goal = await self._synthesize_goal_from_answers(db)
        except Exception as exc:
            logger.exception(
                "Goal synthesis failed for conversation %s, using fallback", self._conv.id
            )
            goal = await self._extract_original_query(db) or ""
        self.complete_grilling(goal)
        # Acceptance criteria and the structured plan are generated lazily by
        # the goal-kickoff stream AFTER its initial active frame. Blocking the
        # answer response on more LLM calls hid the completed stage for 90-160s.
        return {
            "status": "grilling_complete",
            "completed": len(qa_pairs),
            "total": len(qa_pairs),
            "goal": goal,
        }
    async def _grilling_readiness_report(self, db: AsyncSession) -> Dict[str, Any]:
        """A2/S0（readiness gate，Stellar Colosseum 2609.15983 启发）：轮末结构化
        判读。失败 fail-open → 继续固定轮次，永不卡死盘问。"""
        _sync_deathmatchgrillingflowmixin_globals()
        try:
            original = await self._extract_original_query(db)
            history = self._format_history_for_prompt()
            llm = self._make_llm()
            raw = await self._llm_generate(
                llm,
                GRILLING_ROUND_SYNTHESIS_PROMPT.format(
                    round=self._conv.deathmatch_grilling_round or 1,
                    query=original or self._conv.deathmatch_goal or "",
                    previous_context="",
                    qa_pairs=history or "(尚无问答)",
                ),
                f"原始目标:\n{original or ''}\n\n盘问历史:\n{history or '(无)'}",
                temperature=0.0,
            )
            report = _parse_grilling_readiness(raw)
            logger.info(
                "PEVR grilling readiness report: should_continue=%s diminishing=%s "
                "open=%d vague=%d defaults=%d (round %d)",
                report["should_continue"], report["diminishing_returns"],
                len(report["open_decisions"]), len(report["vague_dimensions"]),
                len(report["assumed_defaults"]), self._conv.deathmatch_grilling_round,
            )
            return report
        except Exception as exc:
            logger.warning(
                "Grilling readiness judgment failed (%s) — keep fixed rounds", exc
            )
            return {
                "should_continue": True,
                "reason": f"readiness judgment failed: {exc}",
                "open_decisions": [], "vague_dimensions": [],
                "assumed_defaults": [], "diminishing_returns": False,
                # A4.9 Minor：失败兜底不落档（否则会遮蔽上一轮真实报告的
                # assumed_defaults）。
                "_failed": True,
            }
    async def _should_continue_grilling(self, db: AsyncSession) -> bool:
        """E1: bool view of the readiness report (kept for compatibility)."""
        _sync_deathmatchgrillingflowmixin_globals()
        report = await self._grilling_readiness_report(db)
        return bool(report.get("should_continue", True))
    async def _grilling_search(self, query: str) -> List[Dict[str, str]]:
        """Search seam for the pre-grill fact research (overridable in tests)."""
        _sync_deathmatchgrillingflowmixin_globals()
        from app.services.search_service import WebSearchService
        service = WebSearchService()
        if not service.is_available:
            return []
        hits = await service.search(query)
        out = []
        for hit in hits[:2]:
            out.append({
                "title": str(getattr(hit, "title", "") or ""),
                "snippet": str(getattr(hit, "snippet", "") or getattr(hit, "content", "") or ""),
                "url": str(getattr(hit, "url", "") or ""),
            })
        return out
    async def _grilling_research_block(self, query: str) -> str:
        """A4（facts-before-ask，grill-me「先自查再提问」纪律）：出题前有界联网
        核实事实类问题，结论注入出题提示词；任何失败 fail-open 返回空串。"""
        _sync_deathmatchgrillingflowmixin_globals()
        if not config.deathmatch_grilling_research_enabled:
            return ""
        max_queries = max(1, config.deathmatch_grilling_research_max_queries)
        try:
            llm = self._make_llm()
            raw = await self._llm_generate(
                llm,
                GRILLING_FACT_RESEARCH_PROMPT.format(
                    query=query or "",
                    history_text=self._format_history_for_prompt(),
                    max_queries=max_queries,
                ),
                f"原始目标:\n{query or ''}",
                temperature=0.0,
            )
            data = self._parse_json_object(raw) or {}
            fact_questions = data.get("fact_questions") or []
            if not isinstance(fact_questions, list):
                return ""
            fact_questions = [str(q).strip() for q in fact_questions if str(q).strip()]
            if not fact_questions:
                return ""
        except Exception:
            logger.debug("grilling fact-question extraction failed (fail-open)", exc_info=True)
            return ""

        # A4.9 Important 3：检索摘录是不可信内容（COV-11）——标题/摘要/URL
        # 全部有界、过注入扫描，且标注不得冒充"已核实事实"。
        try:
            from app.services.memory_security import scan_injection as _scan
        except Exception:  # pragma: no cover - scanner always importable
            _scan = None
        lines = [
            "【检索到的公开资料摘录（未经独立核实，仅供背景参考）】"
            "禁止就以下内容向用户提问；与用户回答冲突时一律以用户回答为准。"
        ]
        found = False
        for fq in fact_questions[:max_queries]:
            try:
                hits = await self._grilling_search(fq)
            except Exception:
                logger.debug("grilling fact search failed (fail-open)", exc_info=True)
                hits = []
            if not hits:
                continue
            kept_hits = []
            for h in hits:
                title = _sanitize_research_field(h.get("title", ""), 80)
                snippet = _sanitize_research_field(h.get("snippet", ""), 200)
                url = _sanitize_research_field(h.get("url", ""), 120)
                blob = f"{title}\n{snippet}\n{url}"
                if _scan is not None:
                    try:
                        hit_pattern = _scan(blob)
                    except Exception:
                        hit_pattern = None
                    if hit_pattern:
                        logger.warning(
                            "grilling research hit dropped (injection scan): %s",
                            hit_pattern,
                        )
                        continue
                kept_hits.append((title, snippet, url))
            if not kept_hits:
                continue
            found = True
            fq_label = _sanitize_research_field(fq, 120)
            lines.append(f"- 问题「{fq_label}」：")
            for title, snippet, url in kept_hits:
                lines.append(f"  · {title}：{snippet}（{url}）")
        if not found:
            return ""
        return "\n".join(lines) + "\n\n"
    async def submit_grilling_round(
        self,
        answers: List[Dict[str, str]],
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """Submit all answers for the current grilling round.

        answers: list of {"task_id": str, "answer": str}
        Returns dict with status:
          - "next_round": generated new questions for next round
          - "grilling_complete": all rounds done, goal synthesized
          - "grilling_in_progress": not all current-round questions answered
        """
        _sync_deathmatchgrillingflowmixin_globals()
        from app.db.database import AgentTask

        if not self.is_grilling:
            return {"status": "error", "message": "not in grilling phase"}

        current_round = self._conv.deathmatch_grilling_round or 1

        stmt = (
            select(AgentTask)
            .where(
                AgentTask.conversation_id == self._conv.id,
                AgentTask.task_type == "grilling",
                AgentTask.status == "pending",
            )
        )
        result = await db.execute(stmt)
        pending_tasks = {t.id: t for t in result.scalars().all()}

        if not pending_tasks:
            return {"status": "error", "message": "no pending questions for this round"}

        # Validate all pending tasks are answered.
        answered_task_ids = set()
        for a in answers:
            task_id = a.get("task_id")
            answer = (a.get("answer") or "").strip()
            if task_id in pending_tasks and answer:
                answered_task_ids.add(task_id)

        if len(answered_task_ids) < len(pending_tasks):
            return {
                "status": "incomplete",
                "completed": len(answered_task_ids),
                "total": len(pending_tasks),
                "message": f"还有 {len(pending_tasks) - len(answered_task_ids)} 个问题未回答",
            }

        # Mark tasks completed and build QA pairs for history.
        qa_pairs = []
        for a in answers:
            task_id = a.get("task_id")
            answer = (a.get("answer") or "").strip()
            task = pending_tasks.get(task_id)
            if not task:
                continue
            task.status = "completed"
            task.result = answer
            task.progress = 1.0
            task.completed_at = datetime.utcnow()
            pair = self._qa_pair_from_task(task)
            pair["answer"] = answer
            qa_pairs.append(pair)

        self._add_to_qa_history(current_round, qa_pairs)
        self._conv.deathmatch_grilling_completed = (
            (self._conv.deathmatch_grilling_completed or 0) + len(qa_pairs)
        )
        await db.flush()

        # A1: readiness-gated advance vs goal synthesis — shared with the
        # per-question path (was: always advance through configured rounds).
        return await self._advance_or_finish_round(db, qa_pairs)
    async def _extract_original_query(self, db: AsyncSession) -> str:
        """Recover the user's original task query from messages or context."""
        _sync_deathmatchgrillingflowmixin_globals()
        from app.db.database import Message

        # Prefer first user message in conversation.
        try:
            msg_result = await db.execute(
                select(Message)
                .where(Message.conversation_id == self._conv.id, Message.role == "user")
                .order_by(Message.created_at)
                .limit(1)
            )
            first_msg = msg_result.scalar_one_or_none()
            if first_msg and (first_msg.content or "").strip():
                return first_msg.content.strip()
        except Exception:
            pass
        return self._conv.deathmatch_context_summary or ""
    async def _synthesize_goal_from_answers(self, db: AsyncSession) -> str:
        """Build the authoritative goal from the original request and actual Q&A.

        No free-form LLM rewrite is allowed to change hard requirements.
        """
        _sync_deathmatchgrillingflowmixin_globals()
        from app.db.database import AgentTask
        from app.services.llm_service import LLMService

        stmt = (
            select(AgentTask)
            .where(
                AgentTask.conversation_id == self._conv.id,
                AgentTask.task_type == "grilling",
                AgentTask.status == "completed",
            )
            .order_by(AgentTask.created_at)
        )
        result = await db.execute(stmt)
        tasks = result.scalars().all()

        qa_pairs = []
        original_query = ""
        for t in tasks:
            ctx = {}
            if t.context:
                try:
                    ctx = json.loads(t.context)
                except Exception:
                    pass
            question = ctx.get("question", t.goal or "")
            recommendation = ctx.get("recommendation", "")
            answer = t.result or ""
            if ctx.get("original_query"):
                original_query = ctx["original_query"]
            qa_pairs.insert(0, (
                f"Q: {str(question)[:180]}\n"
                f"A（用户实际回答，最终决策）: {str(answer)[:600]}\n"
                f"推荐背景（仅解释回答；被修改或拒绝时不生效）: {str(recommendation)[:240]}"
            ))

        if not original_query:
            from app.db.database import Message
            msg_result = await db.execute(
                select(Message)
                .where(Message.conversation_id == self._conv.id, Message.role == "user")
                .order_by(Message.created_at)
                .limit(1)
            )
            first_msg = msg_result.scalar_one_or_none()
            if first_msg:
                original_query = first_msg.content or ""

        # Build the goal deterministically. A free-form synthesis can invent
        # constraints (17B→16B), while an unconditional original-first rule can
        # also erase an explicit amendment. Actual answers are final decisions;
        # recommendations are context only, and newer decisions come first so
        # downstream bounded readers do not silently lose them.
        sections: List[str] = []
        corrections = [
            str(entry.get("resume_correction") or "").strip()
            for entry in reversed(self._current_qa_history())
            if isinstance(entry, dict) and str(entry.get("resume_correction") or "").strip()
        ]
        if corrections:
            # Preserve every non-superseded instruction newest-first. Explicit
            # conflicts resolve toward the newer (earlier) entry; additive
            # corrections such as language + cost analysis cannot disappear.
            sections.append(
                "【最新恢复纠正（全部按新到旧保留；明确冲突时以较前/较新条目为准）】\n"
                + _truncate("\n\n".join(corrections), 3600)
            )
        if original_query.strip():
            sections.append(
                "【原始用户要求（基线；若与实际回答明确变更冲突，以实际回答为准）】\n"
                + _truncate(original_query.strip(), 1800)
            )
        if qa_pairs:
            sections.append(
                "【实际盘问决策（用户实际回答为最终决策；明确修改原始要求时以最新回答为准）】\n"
                + "\n\n".join(qa_pairs)[:2400]
            )
        defaults_block = _assumed_defaults_block(self._latest_readiness())
        if defaults_block:
            sections.append(defaults_block + "（若被实际回答推翻，则以实际回答为准）")
        summary = (self._conv.deathmatch_context_summary or "").strip()
        if summary:
            sections.append("【此前对话上下文（仅供参考，不得覆盖上述决策）】\n" + summary[:800])
        if sections:
            assembled = "\n\n".join(sections).strip()
            if len(assembled) > 6400:
                return assembled[:6400] + "\n…[目标组装有界截断：优先保留原始基线与最新决策]"
            return assembled
        return (self._conv.deathmatch_goal or "").strip()
