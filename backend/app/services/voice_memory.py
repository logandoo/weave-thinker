# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from app/services/voice_service.py (C-03/C-04, 2026-10-19).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = ('_VMEM_END', '_VMEM_START', '_now', '_scan_threats', '_vmem_budget', '_vmem_wrap', 'asyncio', 'exc', 'logger', 'memory_retrieval_service', 'memory_runtime_enabled', 're', 'strip_voice_tags')


def _sync_voicememory_globals() -> None:
    from app.services import voice_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class VoiceMemoryMixin:
    async def _set_state(self, state: str) -> None:
        _sync_voicememory_globals()
        self.state = state
        await self._send_json({"event": "state", "state": state})
    async def _speak_interjection(self, text: str, emotion: str) -> None:
        """Speak a brief interjection while the user is still speaking.

        The interjection is a short TTS burst that plays over the user's
        ongoing speech. It does NOT trigger the full turn/speak state machine —
        the agent remains in 'listen' state so ASR continues accumulating.
        After the interjection audio finishes, the flag clears automatically.
        """
        _sync_voicememory_globals()
        origin_epoch = self._prepare_direct_speech()
        self._interjecting = True
        old_emotion = self._emotion
        self._emotion = emotion
        self._turn_interjections.append({
            "sentence": self._user_speech_sentences[-1] if self._user_speech_sentences else "",
            "text": text,
            "emotion": emotion,
        })
        # Record the interjection into the conversation context: it used to
        # exist ONLY as a one-shot system note for the same turn, so a later
        # follow-up question about the interjection ("你刚才说X是什么意思")
        # had no record of it at all. Appending at speak time (before the
        # full user turn is added) keeps real conversation order: the model
        # sees the interjection in _history for this turn and every turn after.
        self._history.append({"role": "assistant", "content": strip_voice_tags(text)})
        self._interjection_count_this_turn += 1
        self._last_interjection_time = _now()

        clean = strip_voice_tags(text)
        await self._send_json({
            "event": "interjection",
            "text": clean,
            "emotion": emotion,
            "raw_text": text,
        })
        if emotion != old_emotion:
            await self._send_json({"event": "emotion", "emotion": emotion})

        logger.info(
            "voice interjection: text=%r emotion=%s sentence=%r",
            text, emotion,
            self._user_speech_sentences[-1] if self._user_speech_sentences else "",
        )

        # Speak via TTS — this sets _speaking=True (enabling barge-in echo
        # detection) but the user's ASR continues. If the user pauses (EoT)
        # during the interjection, _on_user_turn will fire and the interjection
        # is cut off (the user's turn takes priority).
        try:
            await self._speak_direct_text(text, wait=True, origin_epoch=origin_epoch)
        except Exception as exc:
            logger.debug("voice interjection TTS error: %s", exc)
        finally:
            self._interjecting = False
    def _reset_interjection_state(self) -> None:
        """Reset per-turn interjection tracking at the start of a new turn."""
        _sync_voicememory_globals()
        self._user_speech_sentences = []
        self._interjection_offset = len(self._latest_full)
        self._turn_interjections = []
        self._interjection_count_this_turn = 0
        self._interjecting = False
        self._interjection_checking = False
    def _apply_vmem_block(self, ctx: str | None) -> None:
        """Replace the identity-prompt memory block with the latest recall
        context. Idempotent while the sentinels exist; appends a fresh block
        when the session-start memory section was empty.

        ``ctx=None`` restores the session-start baseline (``_vmem_baseline``):
        an empty recall must not leave a previous topic's block in place.
        When the baseline is empty (e.g. brand-new session) the current block
        is KEPT — wiping a real recall because of a noise turn ("嗯嗯") is
        worse than carrying a stale block (E2E-verified 2026-08-19)."""
        _sync_voicememory_globals()
        remove = False
        if ctx is None:
            if not self._vmem_baseline:
                return  # 基线空：保留当前区块（宁存勿清）
            ctx = self._vmem_baseline
        block = _vmem_wrap(ctx[:4000])
        if not block:
            return
        if _VMEM_START in self._identity_prompt:
            self._identity_prompt = re.sub(
                re.escape(_VMEM_START) + r".*?" + re.escape(_VMEM_END),
                lambda _m: block,
                self._identity_prompt,
                count=1,
                flags=re.DOTALL,
            )
        elif self._identity_prompt:
            self._identity_prompt = self._identity_prompt + "\n\n" + block
        else:
            self._identity_prompt = block
    def _start_memory_recall(self, turn_epoch: int, text: str, history_snapshot: list[dict]) -> None:
        """Fire-and-forget per-turn v2 memory recall (never awaited on the
        turn's critical path). Gates: feature flag, v2 runtime, live db.
        All failures are silent — recall is an enhancement, not a dependency."""
        _sync_voicememory_globals()
        if not self.config.voice_memory_recall_enabled:
            return
        try:
            from app.services.memory_runtime_state import memory_runtime_enabled
            if not (memory_runtime_enabled(self.config)
                    and self.config.memory.get("retrieval_enabled")):
                return
        except Exception:
            return
        uid = getattr(self.user, "id", None)
        if self.db is None or not uid or self._closed:
            return
        task = asyncio.create_task(
            self._vmem_recall_run(turn_epoch, text, history_snapshot, uid))
        self._vmem_recall_inflight.add(task)
        task.add_done_callback(self._vmem_recall_inflight.discard)
    async def _vmem_recall_run(self, turn_epoch: int, text: str, history_snapshot: list[dict],
                               uid: str) -> None:
        _sync_voicememory_globals()
        from app.services import memory_retrieval_service
        recent = [m for m in (history_snapshot or []) if m.get("role") == "user"][-3:]
        if not recent:
            recent = [{"role": "user", "content": text}]
        try:
            ctx, ids, top = await asyncio.wait_for(
                memory_retrieval_service.retrieve_with_meta(self.db, uid, recent),
                timeout=10.0)
        except Exception as exc:
            logger.debug("voice per-turn memory recall failed (silent): %s", exc)
            return
        if self._closed:
            return
        if ctx and ctx.strip():
            self._vmem_block_ctx = ctx
            self._apply_vmem_block(ctx)
        elif ids == []:
            # 空召回（无候选）：还原会话基线，不残留上一主题的区块。
            self._apply_vmem_block(None)
        logger.info(
            "voice memory recall done: turn=%s ids=%d top=%.2f chars=%d",
            turn_epoch, len(ids), top, len(ctx or ""))
        # 后续插话仲裁（Task: _vmem_maybe_judge）由该方法链外的钩子唤起——
        # 若方法存在则 create_task（存在性检查让 T3 可在 T5 前独立落地）。
        if ids:
            judge = getattr(self, "_vmem_maybe_judge", None)
            if judge is not None:
                judge_task = asyncio.create_task(judge(turn_epoch, ctx or "", ids, top))
                self._vmem_recall_inflight.add(judge_task)
                judge_task.add_done_callback(self._vmem_recall_inflight.discard)
    def _vmem_queue_has_answer_epoch(self, epoch: int) -> bool:
        """Read-only scan: is this turn's answer audio still pending in the TTS
        queue? (correct 窗口判定：回答还在播，才有可纠正的余段。只读，不动队列。）"""
        _sync_voicememory_globals()
        for item in list(self._tts_queue._queue):
            if (isinstance(item, dict) and "text" in item
                    and not item.get("aux") and item.get("epoch") == epoch):
                return True
        return False
    def _vmem_pick_window(self, turn_epoch: int) -> str:
        """窗口路由（优先级 correct > w1 > postscript）：
        - correct：纠正开关开 且 本轮回答还有余段在播（可自我打断）
        - w1_user_speaking：用户正在说下一句（插话会打断 ta，judge 端从严）
        - postscript：其余（回答播完、还没下一条——顺接最自然）
        """
        _sync_voicememory_globals()
        gen_epoch = turn_epoch + 1
        if (self.config.voice_memory_correct_enabled
                and self._vmem_queue_has_answer_epoch(gen_epoch)):
            return "correct"
        if (self._pending_turn_text.strip() and self._asr_ready
                and not self._speaking and not self._turn_active):
            return "w1_user_speaking"
        return "postscript"
    async def _vmem_maybe_judge(self, turn_epoch: int, ctx: str, ids: list,
                                top: float) -> None:
        """记忆插话仲裁总入口（由 _vmem_recall_run 派生，fire-and-forget）。

        门控表（全过才出声，任一不过则静默——宁可不说错）：
        特性开关 → 有召回结果 → 相关性下限 → id 未消费过 → 间隔 ≥ cooldown
        → 双预算未同时耗尽 → 等本轮生成结束（≤25s，防挂死）→ 跨轮过期防护
        → 窗口路由 → 仲裁 LLM → 单动作预算 → 出声。被判 none 的批次也消耗
        id（同一批记忆不会换个窗口再仲裁一遍）。
        """
        _sync_voicememory_globals()
        cfg = self.config
        if self._closed:
            return
        if not cfg.voice_memory_interjection_enabled:
            return
        if not ctx or not ids:
            return
        if top is not None and top < cfg.voice_memory_interjection_min_score:
            return
        new_ids = {i for i in ids if i not in self._vmem_last_ids}
        if not new_ids:
            return
        budget = _vmem_budget(self.user.id)
        if (budget.last_time
                and _now() - budget.last_time < cfg.voice_memory_interjection_cooldown_seconds):
            return
        if (budget.append >= cfg.voice_memory_interjection_max_append
                and budget.correct >= cfg.voice_memory_interjection_max_correct):
            return
        # 等本轮生成结束：取值单调递增（每轮 +1），无竞态清除问题；每 0.5s
        # 轮询一次即可，整体封顶 25s（生成比召回还慢的极端场景直接弃）。
        gen_epoch = turn_epoch + 1
        deadline = _now() + 25.0
        while self._vmem_gen_done_epoch < gen_epoch:
            if self._closed:
                return
            remaining = deadline - _now()
            if remaining <= 0:
                return
            step = min(0.5, remaining)
            if self._vmem_gen_done.is_set():
                # 首轮之后 Event 恒 set（不 clear，靠单调 epoch 判归属）——
                # wait() 会立即返回，必须真实 sleep 保持 0.5s 轮询节律，
                # 否则忙自旋夺事件循环（实时音频 pacing 抖动）。
                await asyncio.sleep(step)
            else:
                try:
                    await asyncio.wait_for(self._vmem_gen_done.wait(), timeout=step)
                except asyncio.TimeoutError:
                    pass
        # 过期防护：等期间若已有更新一轮开始，本批召回/回答已错位 → 弃。
        if self._turn_epoch != gen_epoch:
            return
        if self._vmem_gen_done_epoch > gen_epoch:
            return
        if self._vmem_answer_epoch != gen_epoch:
            return
        window = self._vmem_pick_window(turn_epoch)
        user_speech = self._pending_turn_text if window == "w1_user_speaking" else ""
        try:
            verdict = await self._classify_memory_interjection(
                ctx, self._last_answer_text, user_speech, window)
        finally:
            self._vmem_last_ids |= set(ids)
        action = verdict.get("action")
        if action == "none":
            return
        line = verdict.get("line") or ""
        # 注入消毒（记忆→仲裁→插话链）：威胁模式/不可见 Unicode 命中即弃，
        # 不让污染文本经 _history 进入后续轮次上下文。
        # 用 _scan_threats（不受 super_admin_bypass 影响——仲裁输出不可信，
        # 管理员绕过语义只适用于管理员写记忆）。
        if line:
            from app.tools.memory import _scan_threats
            if _scan_threats(line):
                logger.debug("voice memory remark blocked (injection scan)")
                return
        if action == "append" and budget.append >= cfg.voice_memory_interjection_max_append:
            return
        if action == "correct" and budget.correct >= cfg.voice_memory_interjection_max_correct:
            return
        # 出声前复检（仲裁 LLM 0.5-3s 内世界可能已变）：新一轮已开始 →
        # 本轮语境失效，宁漏勿错。
        if self._closed or self._turn_epoch != gen_epoch:
            return
        kind = "memory_append" if action == "append" else "memory_correct"
        try:
            await self._speak_memory_remark(verdict["line"], kind, verdict["emotion"], window)
        except Exception as exc:
            logger.debug("voice memory remark speak failed: %s", exc)
    async def _speak_memory_remark(self, line: str, kind: str, emotion: str,
                                   window: str = "") -> None:
        """出声：把仲裁通过的插话以普通 TTS 项说出去（非 aux——aux 有回答
        就绪即截断语义，插话必须完整播完）。correct 先 drain 本轮余段。
        台账/历史/回声守卫/WS 事件与既有 _speak_interjection 同构。

        出声前对预算/冷却做二次复检（TOCTOU 防线）：judge 门控读的是任务
        启动时的 budget 快照，多个 fire-and-forget judge 可并发通过门控，
        必须在此处用最新状态拦截超支/击穿。"""
        _sync_voicememory_globals()
        cfg = self.config
        budget = _vmem_budget(self.user.id)
        if kind == "memory_append":
            if budget.append >= cfg.voice_memory_interjection_max_append:
                return
        else:
            if budget.correct >= cfg.voice_memory_interjection_max_correct:
                return
        if (budget.last_time
                and _now() - budget.last_time < cfg.voice_memory_interjection_cooldown_seconds):
            return
        origin_epoch = self._prepare_direct_speech()
        clean = strip_voice_tags(line)
        now = _now()
        self._aux_echo_guard.append((clean, now))
        del self._aux_echo_guard[:-4]
        self._interjecting = True
        old_emotion = self._emotion
        self._emotion = emotion
        try:
            if kind == "memory_correct":
                self._drain_tts_queue()
                self._pending_interruption_note = (
                    "（上一轮回答的余下部分因我想起一条更准确的长期记忆被中断；"
                    "我随后说了自我纠正，最新说法以纠正后的为准。）"
                )
            self._history.append({"role": "assistant", "content": clean})
            self._turn_interjections.append({
                "sentence": "", "text": line, "emotion": emotion, "kind": kind,
            })
            # 落库：插话是真实说出口的内容，须进会话记录（Web UI 可见、
            # 断线重连后 _history 从 DB 重建仍保留，追问答不丢链）。
            await self._persist_message("assistant", clean)
            await self._send_json({
                "event": "interjection", "text": clean, "emotion": emotion,
                "raw_text": line, "kind": kind,
            })
            if emotion != old_emotion:
                await self._send_json({"event": "emotion", "emotion": emotion})
            # 预算/冷却在出声前更新：并发 judge 的复检立即可见；
            # 即使 TTS 后续失败也不放行第二个（宁可少说）。
            if kind == "memory_append":
                budget.append += 1
            else:
                budget.correct += 1
            budget.last_time = _now()
            await self._speak_direct_text(line, wait=True, origin_epoch=origin_epoch)
        finally:
            self._interjecting = False
    def _decay_emotion(self) -> None:
        """Decay emotion one level toward calm after a turn completes."""
        _sync_voicememory_globals()
        new_emotion = self._EMOTION_DECAY.get(self._emotion, "calm")
        if new_emotion != self._emotion:
            self._emotion = new_emotion
    def _build_interjection_context_note(self) -> str:
        """Build a system-message note describing interjections made during the
        user's speech, so the main agent's full answer is coherent and does not
        repeat what was already said in interjections."""
        _sync_voicememory_globals()
        if not self._interjection_snapshot:
            return ""
        lines = ["（系统提示，不要朗读）：在用户说话的过程中，你做了以下简短插话："]
        has_memory_remark = False
        for i, inj in enumerate(self._interjection_snapshot, 1):
            text_clean = strip_voice_tags(inj.get("text", ""))
            kind = inj.get("kind", "")
            if kind in ("memory_append", "memory_correct"):
                has_memory_remark = True
                label = "补充" if kind == "memory_append" else "更正"
                lines.append(f"  {i}. （记忆插话·{label}）你主动说了「{text_clean}」")
            else:
                lines.append(f"  {i}. 用户说「{inj.get('sentence', '')[:30]}」时你说了「{text_clean}」")
        lines.append("请在回答时自然衔接，不要重复这些已说过的内容。如果用户的叙述已经包含了你插话时追问的信息，直接基于完整内容回答。")
        if has_memory_remark:
            lines.append("其中标注（记忆插话）的是你基于长期记忆主动补充/更正的内容；后续轮次一律以更正/补充后的信息为准，不要再沿用被更正前的旧说法。")
        return "\n".join(lines)
