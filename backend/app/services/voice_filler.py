# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from voice_service.py (VoiceFiller, 2026-10-20).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (
    '_ASR_NOISE_RE',
    '_AUX_RECENT_CAP',
    '_STOP_TASK_EXACT_RE',
    '_STOP_TASK_RE',
    '_is_likely_echo',
    '_is_voice_noise',
    '_norm_barge_compare',
    '_now',
    '_pick_aux_phrase',
    '_split_segments',
    '_strip_lead_punct',
    '_utterance_complete',
    'asyncio',
    'logger',
)


def _sync_voicefillermixin_globals() -> None:
    from app.services import voice_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


_STATUS_ZH = {
    "completed": "已完成",
    "failed": "执行失败",
    "cancelled": "已取消",
    "timed_out": "已超时",
    "waiting_input": "需要你补充信息",
    "waiting_auth": "需要你授权",
    "waiting_budget": "需要你确认预算",
}


def build_batch_notice_text(items: list[dict]) -> str:
    """One short, deterministic sentence for a completion burst (§10.2)."""
    parts = []
    for item in (items or [])[:5]:
        assistant = str(item.get("assistant_name") or "执行助手")
        title = str(item.get("title") or "委派任务")[:30]
        status = str(item.get("task_status") or item.get("status") or "")
        parts.append(f"{assistant}的《{title}》{_STATUS_ZH.get(status, status or '有更新')}")
    text = f"有 {len(items or [])} 个委派任务有更新：" + "；".join(parts) + "。"
    if len(items or []) > 5:
        text += f"另外还有 {len(items) - 5} 个任务，可在任务面板查看。"
    return text


class VoiceFillerMixin:
    def _record_filler_pick(self, phrase: str) -> None:
        """填充词选取落账：last + 近期窗口（_pick_aux_phrase 的 recent 源）。"""
        _sync_voicefillermixin_globals()
        self._last_filler_phrase = phrase
        self._recent_filler_phrases = (self._recent_filler_phrases + [phrase])[-_AUX_RECENT_CAP:]
    def _record_backchannel_pick(self, phrase: str) -> None:
        """应和选取落账：last + 近期窗口。"""
        _sync_voicefillermixin_globals()
        self._last_backchannel_phrase = phrase
        self._recent_backchannel_phrases = (self._recent_backchannel_phrases + [phrase])[-_AUX_RECENT_CAP:]
    async def _speak_filler(self) -> None:
        """Filler prefix (填充词): speak a random "让我想想" phrase at the start
        of an answer turn so the user is never met with dead silence while the
        LLM generates. Gates: enabled, not already speaking, not paused, no
        tool loop running, session open. The phrase rides _tts_queue as an aux
        item — the consumer cuts it the moment the real answer's first audio
        chunk is ready, so fast answers pay ~zero added latency.

        This is the FALLBACK trigger (barge-in-deferred turns reach
        `_handle_user_turn` without passing the idle flush block). The primary
        trigger is `_convert_filler_prefetch` at the flush; the shared
        `_last_filler_at` cooldown keeps the two from double-firing in one
        turn and bounds fragment-cascade repeats."""
        _sync_voicefillermixin_globals()
        cfg = self.config
        if not cfg.voice_filler_enabled:
            return
        if _now() - self._last_filler_at < cfg.voice_filler_min_gap_seconds:
            return
        if self._speaking or self._playback_paused or self._tools_running:
            return
        if self._closed:
            return
        phrase = _pick_aux_phrase(cfg.voice_filler_phrases, self._last_filler_phrase,
                                  recent=self._recent_filler_phrases)
        if not phrase:
            return
        if len(phrase) > 24:
            # A pathological over-long configured phrase would play in full
            # before the answer arrives — never (A4.9 M4).
            return
        self._record_filler_pick(phrase)
        self._last_filler_at = _now()
        await self._send_json({"event": "filler", "text": phrase})
        await self._speak_text(phrase, wait=False, aux=True)
    def _filler_idle_gates_ok(self) -> bool:
        """Shared gates for the speculative filler arm/convert: only when the
        session is fully idle (no playback, no paused answer, no active turn,
        no tool loop) may a filler be synthesized/queued — the same conditions
        under which the EoT flush routes to the plain `_enqueue_turn` path."""
        _sync_voicefillermixin_globals()
        cfg = self.config
        if not cfg.voice_filler_enabled or self._closed:
            return False
        if self._speaking or self._playback_paused or self._turn_active or self._tools_running:
            return False
        return True
    def _drop_filler_prefetch(self) -> None:
        """Cancel and clear a live speculative filler synthesis (text changed,
        gates flipped, session draining/shutting down), plus any pending
        TTFT-gated phrase/watch from the previous turn."""
        _sync_voicefillermixin_globals()
        p = self._filler_prefetch
        task = p.get("task")
        if task is not None and not task.done():
            task.cancel()
        self._filler_prefetch = {"text": "", "phrase": "", "q": None, "task": None}
        self._clear_pending_filler()
    def _clear_pending_filler(self) -> None:
        """Drop the converted-but-unplayed filler and cancel its TTFT watch
        (drain / shutdown / generation end / skipped turn). The pre-synthesized
        queue AND its still-running synth task are discarded — one-shot, like
        the prefetch it came from (A4.9 review Minor #2)."""
        _sync_voicefillermixin_globals()
        pending = self._pending_filler
        task = pending.get("task")
        if task is not None and not task.done():
            task.cancel()
        self._pending_filler = {"phrase": "", "q": None, "task": None}
        watch = self._filler_watch_task
        if watch is not None and not watch.done():
            watch.cancel()
        self._filler_watch_task = None
    def _register_aux_echo(self, text: str) -> None:
        """Register an aux utterance (filler/backchannel) for mic-echo tail-
        stripping. Registers the SPLIT segments plus the joined phrase: the
        consumer typically cuts a filler mid-phrase, so the ASR tail of an
        interrupted aux word may only contain one segment. Shared by the
        flush-convert path and the `_speak_text(aux=True)` fallback so the
        two stay symmetric (2026-08-25 review I2)."""
        _sync_voicefillermixin_globals()
        segments, rest = _split_segments(text)
        if rest.strip():
            segments.append(rest)
        spoken = [s for s in segments if s.strip()]
        if not spoken:
            return
        now = _now()
        self._aux_echo_guard.extend((s, now) for s in spoken)
        joined = "".join(spoken)
        if joined not in [p for p, _ in self._aux_echo_guard]:
            self._aux_echo_guard.append((joined, now))
        del self._aux_echo_guard[:-4]
    def _arm_filler_prefetch(self, text: str) -> None:
        """Speculatively start the filler phrase's TTS synthesis while the EoT
        watchdog is still deciding whether the utterance ended (silence has
        reached arm threshold). ARM-ONCE per live prefetch — the caller only
        invokes this when no prefetch is alive, so a long monologue's many
        mid-utterance pauses produce at most ONE synthesis (not one per
        FunASR partial, which 0.3-1.1s cadence would otherwise burn as
        cancelled provider calls — review I1). At the flush the phrase
        converts to a pre-synthesized aux item, so the filler's own TTS
        round-trip overlaps the generation pipeline instead of trailing it.
        Deterministic gates only (noise/stop-word/busy/cooldown): the LLM
        intent classifier runs too late to gate this, and the reject path
        already drains aux speech (A4.9 I2 pattern)."""
        _sync_voicefillermixin_globals()
        if not self._filler_idle_gates_ok():
            return
        cfg = self.config
        if _now() - self._last_filler_at < cfg.voice_filler_min_gap_seconds:
            return
        t = (text or "").strip()
        if not t or _is_voice_noise(t):
            return
        if _STOP_TASK_RE.search(t) or _STOP_TASK_EXACT_RE.match(t):
            return
        phrase = _pick_aux_phrase(cfg.voice_filler_phrases, self._last_filler_phrase,
                                  recent=self._recent_filler_phrases)
        if not phrase or len(phrase) > 24:
            return
        self._record_filler_pick(phrase)
        # Defensive: the watchdog only arms when no prefetch is alive; drop
        # anything stale so we never hold two candidate syntheses.
        self._drop_filler_prefetch()
        q: asyncio.Queue = asyncio.Queue()
        task = self._spawn_synth(phrase, self._turn_epoch, q)
        self._filler_prefetch = {"text": t, "phrase": phrase, "q": q, "task": task}
    async def _convert_filler_prefetch(self, flushed: str) -> None:
        """Convert the armed filler synthesis into a PENDING phrase at the EoT
        flush (idle path only, BEFORE `_enqueue_turn`). Nothing plays yet: the
        pre-synthesized queue is held until the TTFT watch scheduled at
        generation start decides — the filler is audible only when the model
        has not produced its first content token within
        `voice_filler_ttft_threshold_seconds` (2026-09-13; fast turns stay
        silent). The old immediate enqueue made every turn open with a filler.

        NO strict text match against the arm-time text: the filler phrase is
        content-independent, and the flushed text is normally LONGER than the
        tail that armed it (partials kept growing) — matching on equality
        would drop most converts (review I1). Security against the user text
        is re-gated here on the FLUSHED text (noise / stop-word / idle gates);
        a prefetch that fails them is dropped. The prefetch is ALWAYS cleared
        (one-shot)."""
        _sync_voicefillermixin_globals()
        p = self._filler_prefetch
        self._filler_prefetch = {"text": "", "phrase": "", "q": None, "task": None}
        q = p.get("q")
        synth_task = p.get("task")
        phrase = p.get("phrase") or ""
        if q is None or not phrase:
            return
        t = (flushed or "").strip()
        if not (
            bool(t)
            and self._filler_idle_gates_ok()
            and not _is_voice_noise(t)
            and not (_STOP_TASK_RE.search(t) or _STOP_TASK_EXACT_RE.match(t))
        ):
            if synth_task is not None and not synth_task.done():
                synth_task.cancel()
            return
        self._pending_filler = {"phrase": phrase, "q": q, "task": synth_task}
    def _schedule_filler_ttft_watch(self) -> None:
        """Start the TTFT-gated filler decision for THIS turn (called when the
        main generation is about to issue its LLM call).

        Consumes `_pending_filler` (EoT prefetch) if present; otherwise the
        watch's fallback synthesizes on demand (`_speak_filler`) — covers the
        barge-in/defer path that never went through the idle flush convert.
        A previous watch is cancelled: only the newest generation owns the
        decision."""
        _sync_voicefillermixin_globals()
        cfg = self.config
        if not cfg.voice_filler_enabled or self._closed:
            self._clear_pending_filler()
            return
        epoch = self._turn_epoch
        pending = self._pending_filler
        self._pending_filler = {"phrase": "", "q": None, "task": None}
        prev = self._filler_watch_task
        if prev is not None and not prev.done():
            prev.cancel()
        self._filler_watch_task = asyncio.create_task(
            self._filler_ttft_watch(
                epoch, pending.get("phrase") or "", pending.get("q"), pending.get("task")
            )
        )
    async def _filler_ttft_watch(self, epoch: int, phrase: str, q,
                                 synth_task: Optional[asyncio.Task] = None) -> None:
        """AND-gated filler decision: play only when (elapsed >= threshold)
        AND (no first content this turn) AND (not speaking/paused/interrupted)
        AND (cooldown passed). OR semantics would degenerate to the old
        per-turn behavior — elapsed always exceeds the threshold eventually.

        With a pre-synthesized queue the phrase plays instantly (its TTS
        round-trip already overlapped the EoT window); without one (barge-in
        path) `_speak_filler` synthesizes on demand — the model is slow
        anyway, so the extra TTS hop only lands on genuinely slow turns."""
        _sync_voicefillermixin_globals()
        played = False
        try:
            threshold = self.config.voice_filler_ttft_threshold_seconds
            if threshold > 0:
                await asyncio.sleep(threshold)
            if self._closed or self._turn_epoch != epoch:
                return
            if self._answer_content_epoch >= epoch:
                return  # model already produced its first content token
            if self._speaking or self._playback_paused or self._interrupt.is_set():
                return
            if phrase and q is not None:
                if _now() - self._last_filler_at < self.config.voice_filler_min_gap_seconds:
                    return
                self._last_filler_at = _now()
                self._register_aux_echo(phrase)
                await self._send_json({"event": "filler", "text": phrase})
                self._tts_queue.put_nowait({
                    "text": phrase, "epoch": epoch, "aux": True, "pre_q": q,
                })
                played = True
            else:
                await self._speak_filler()
                played = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("voice filler ttft watch failed: %s", exc)
        finally:
            if not played and synth_task is not None and not synth_task.done():
                # Decision was "no filler": stop the speculative synthesis
                # instead of letting it fill a queue nobody reads (A4.9
                # review Minor #2).
                synth_task.cancel()
            if self._filler_watch_task is asyncio.current_task():
                self._filler_watch_task = None
    async def _maybe_backchannel(self, silence: float, text: str) -> None:
        """Backchannel (应和): during the USER's speech, in a mid-utterance
        pause window (silence >= pause_seconds and below the semantic-EoT
        probe), the agent utters a short "嗯" to signal attentive listening —
        the classic human listening behavior that keeps a speaker going.

        Design constraints honored (no TTS conflict, no race, no overwrite):
        - Speech rides the SAME _tts_queue as everything else: the single
          consumer serializes it against answers/interjections/notices.
        - Only fires while the agent is fully idle (not speaking, not
          interjecting, no turn active, not paused, ASR ready) — it can never
          talk over the user's turn-taking machinery.
        - Only fires on INCOMPLETE utterances (no terminal punctuation) in a
          silence window that ENDS BEFORE the semantic-EoT probe: a complete
          utterance is about to flush and get answered, a probe-armed one is
          being judged — both would race the turn pipeline.
        - If a flush of the SAME utterance still lands while the ack plays
          (probe 1.0s + judge ~0.8s, or the 1.6s hard threshold, vs TTS
          round-trip ~0.7-1.3s + short phrase audio), the overlap is benign
          BY CONSTRUCTION: the aux-cut truncates the ack the moment the
          answer audio is ready, the barge-in no-interrupt window defers the
          flushed turn (queued + prefetched, answered immediately after), and
          the ack is excluded from the pause breakpoint pool so a resume
          never replays it (A4.9 M3).
        - Phrase length is bounded (~1-2 chars, ~0.3-0.6s audio) so the ack
          is over quickly and its own mic-echo passes the short-noise gate.
        - Cooldown + per-turn cap + same-text dedup + near-field gate + noise/
          echo pre-filters bound frequency and false positives."""
        _sync_voicefillermixin_globals()
        cfg = self.config
        if not cfg.voice_backchannel_enabled:
            return
        if not self._asr_ready or self._speaking or self._interjecting:
            return
        if self._turn_active or self._playback_paused or self._closed:
            return
        if silence < cfg.voice_backchannel_pause_seconds or silence >= cfg.voice_eot_semantic_probe_seconds:
            return
        t = (text or "").strip()
        if not t or _utterance_complete(t):
            return
        if len(_strip_lead_punct(t)) < 2:
            return
        if _ASR_NOISE_RE.match(t) or _is_voice_noise(t):
            return
        if _is_likely_echo(t, self._spoken_text_recent):
            return
        if not self._prox_is_near():
            return
        now = _now()
        if now - self._last_backchannel_time < cfg.voice_backchannel_cooldown_seconds:
            return
        if t == self._backchannel_acked_text:
            return
        if self._backchannel_count_this_turn >= cfg.voice_backchannel_max_per_turn:
            return
        phrase = _pick_aux_phrase(cfg.voice_backchannel_phrases, self._last_backchannel_phrase,
                                  recent=self._recent_backchannel_phrases)
        if not phrase:
            return
        if len(phrase) > 4:
            # Backchannel phrases MUST stay ~1-2 chars: a long one would (a)
            # stretch its audible window into the flush region and (b) its
            # mic-echo could pass the short-noise gate and become a phantom
            # turn (A4.9 M4).
            return
        self._last_backchannel_time = now
        self._record_backchannel_pick(phrase)
        self._backchannel_acked_text = t
        self._backchannel_count_this_turn += 1
        await self._send_json({"event": "agent_backchannel", "text": phrase})
        await self._speak_text(phrase, wait=False, aux=True)
        logger.info("voice backchannel: %r (pause %.2fs, text %r)", phrase, silence, t[-40:])
    def _record_backchannel(self, text: str) -> None:
        """Remember a recent backchannel verdict for *text* so the responder
        can skip a queued copy of the same utterance (a backchannel-classified
        sentence is filler, not a turn — answering it produces a ghost
        unanswered message, conv 689f06ec)."""
        _sync_voicefillermixin_globals()
        norm = _norm_barge_compare(text)
        if norm:
            self._recent_backchannel[norm] = _now()
    async def _deliver_bg_task_notice(self, info: dict, notification_id: Optional[str] = None) -> None:
        """Proactively announce a finished background task and offer follow-up
        actions. The notice (goal + full result + note linkage) goes into the
        LLM context as a system-marked message, so the user's spoken reply
        ("播报一下" / "导出PDF" / "存到笔记") is handled by the normal agentic
        tool loop with the real result in context — nothing is hardcoded."""
        _sync_voicefillermixin_globals()
        if self._closed:
            return
        if info.get("notification_mode") == "deterministic_v1":
            await self._deliver_deterministic_notice(info, notification_id)
            return
        previous_id = self._active_notification_id
        if previous_id and previous_id != notification_id:
            previous_state = self._notification_states.get(previous_id, {})
            if not previous_state.get("delivered"):
                # Do not block the responder or later user turns. Retry this
                # notice after the previous one's playback/ACK settles.
                await self._turn_queue.put({
                    "notice": info,
                    "notification_id": notification_id,
                    "retry_at": _now() + 0.25,
                })
                return
        self._interrupt.clear()
        self._turn_active = True
        await self._set_state("think")
        await self._send_json({
            "event": "bg_task_notice",
            "task_id": info.get("task_id", ""),
            "title": info.get("title", ""),
            "status": info.get("status", ""),
        })
        title = info.get("title") or "未命名任务"
        goal = info.get("goal") or ""
        if info.get("status") == "completed":
            note_hint = ""
            if info.get("output_note_id"):
                note_hint = f"结果也已自动保存到默认笔记本（笔记ID：{info['output_note_id']}）。"
            prompt_text = (
                f"[系统通知] 你之前根据用户的语音请求提交的后台任务《{title}》已经完成。"
                f"任务目标是：{goal}。\n"
                f"任务结果全文如下：\n{info.get('result') or '（无文本输出）'}\n"
                f"结果已自动保存到当前对话。{note_hint}"
                "现在请你立刻主动开口：用一两句口语告诉用户这个任务已完成、简要说明做成了什么，"
                "然后询问用户接下来想怎么做——要不要现在听你播报一下结果要点，"
                "要不要导出成PDF，要不要整理保存到笔记，还是做点别的。"
                "之后如果用户要求播报，用自然的口语转述结果要点，不要逐字念、不要读出格式符号；"
                "如果用户要导出，调用 pdf_export 工具；如果用户要保存或整理到笔记，调用 notes 工具。"
            )
        else:
            prompt_text = (
                f"[系统通知] 你之前根据用户的语音请求提交的后台任务《{title}》执行失败了。"
                f"任务目标是：{goal}。失败原因：{info.get('error') or '未知'}。"
                "现在请你立刻主动开口：用一两句口语告诉用户这个任务没做成，简单说明原因，"
                "然后询问用户要不要换个方式重试，还是先聊点别的。"
            )
        previous_notice_id = self._current_notice_id
        self._current_notice_id = notification_id
        if notification_id:
            self._notification_states[notification_id] = {
                "audio_sent": False,
                "generation_complete": False,
                "tts_failed": False,
                "delivered": False,
                "delivery_attempt_id": info.get("delivery_attempt_id"),
            }
        try:
            # No filler for proactive notices: the user asked nothing, so a
            # "让我想想" opener would be a non-sequitur (pre-TTFT behavior).
            # user_turn=False: 合成系统通知不参与澄清检测（A4.9 r1 Important-2）。
            await self._generate_and_speak(prompt_text, allow_tools=True, allow_filler=False, user_turn=False)
        finally:
            notice_failed = self._notification_states.get(notification_id, {}).get("tts_failed", False)
            zero_audio = (
                self._active_notification_generation_complete
                and not self._active_notification_audio_sent
            )
            if notification_id and (not self._active_notification_generation_complete or notice_failed or zero_audio):
                self._queued_notice_ids.discard(notification_id)
            self._current_notice_id = previous_notice_id
            self._turn_active = False
            self._tools_running = False
            if not self._closed:
                await self._set_state("listen")
    async def _deliver_deterministic_notice(self, info: dict,
                                            notification_id: Optional[str] = None) -> None:
        """No-LLM, no-tools terminal notice (§10.4).

        The text is derived ONLY from trusted structured status + the frozen
        assistant name + a truncated title. Result/error/goal text never enters
        the prompt or any tool-capable context.
        """
        _sync_voicefillermixin_globals()
        if self._closed:
            return
        previous_id = self._active_notification_id
        if previous_id and previous_id != notification_id:
            previous_state = self._notification_states.get(previous_id, {})
            if not previous_state.get("delivered"):
                await self._turn_queue.put({
                    "notice": info,
                    "notification_id": notification_id,
                    "retry_at": _now() + 0.25,
                })
                return
        self._interrupt.clear()
        self._turn_active = True
        await self._set_state("think")
        status = str(info.get("task_status") or info.get("status") or "")
        assistant = str(info.get("assistant_name") or "执行助手")
        title = str(info.get("title") or "委派任务")[:40]
        await self._send_json({
            "event": "bg_task_notice",
            "task_id": info.get("task_id", ""),
            "event_id": info.get("event_id") or notification_id or "",
            "logical_task_id": info.get("logical_task_id") or info.get("task_id", ""),
            "status_version": info.get("status_version", 0),
            "assistant_name": assistant,
            "title": title,
            "status": status,
            "task_status": status,
            "execution_conversation_id": info.get("execution_conversation_id"),
            "output_message_id": info.get("output_message_id"),
            "reasoning_status": info.get("reasoning_status"),
            "delivery_attempt_id": info.get("delivery_attempt_id"),
        })
        if status == "completed":
            text = f"{assistant}委派的任务《{title}》已经执行结束。你可以打开结果会话查看。"
        elif status == "timed_out":
            text = f"{assistant}委派的任务《{title}》已达到执行时限，目前保留了部分结果。你可以打开结果会话检查。"
        elif status == "cancelled":
            text = f"{assistant}委派的任务《{title}》已取消。"
        elif status == "waiting_input":
            text = f"{assistant}委派的任务《{title}》需要你补充信息。你可以打开结果会话查看问题。"
        elif status == "waiting_auth":
            text = f"{assistant}委派的任务《{title}》需要你授权一个动作。你可以打开结果会话查看详情。"
        elif status == "waiting_budget":
            text = f"{assistant}委派的任务《{title}》已达到执行预算，需要你确认是否继续。"
        else:
            text = f"{assistant}委派的任务《{title}》执行失败。你可以打开结果会话查看原因。"
        previous_notice_id = self._current_notice_id
        self._current_notice_id = notification_id
        if notification_id:
            self._notification_states[notification_id] = {
                "audio_sent": False,
                "generation_complete": False,
                "tts_failed": False,
                "delivered": False,
                "delivery_attempt_id": info.get("delivery_attempt_id"),
            }
        try:
            await self._speak_text(
                text, wait=True, notification_id=notification_id)
            if notification_id:
                self._notice_state(notification_id)["generation_complete"] = True
                if self._active_notification_id == notification_id:
                    self._active_notification_generation_complete = True
        finally:
            self._current_notice_id = previous_notice_id
            self._turn_active = False
            self._tools_running = False
            if not self._closed:
                await self._set_state("listen")

    async def _deliver_notice_batch(self, batch: dict) -> None:
        """Speak several terminal notices as ONE announcement (§10.2/§10.4).

        Members stay pending until the single playback drain ACKs the batch;
        `voice_ws` then settles each member with its own delivery attempt id.
        """
        _sync_voicefillermixin_globals()
        if self._closed:
            return
        batch_id = str(batch.get("batch_id") or "")
        items = [dict(item) for item in (batch.get("items") or []) if item]
        if not batch_id or not items:
            return
        previous_id = self._active_notification_id
        if previous_id and previous_id != batch_id:
            previous_state = self._notification_states.get(previous_id, {})
            if not previous_state.get("delivered"):
                await self._turn_queue.put({
                    "notice_batch": batch,
                    "retry_at": _now() + 0.25,
                })
                return
        self._interrupt.clear()
        self._turn_active = True
        await self._set_state("think")
        members = [{
            "notification_id": str(item.get("notification_id") or ""),
            "delivery_attempt_id": item.get("delivery_attempt_id"),
        } for item in items]
        self._notification_states[batch_id] = {
            "audio_sent": False,
            "generation_complete": False,
            "tts_failed": False,
            "delivered": False,
            "members": members,
        }
        await self._send_json({
            "event": "bg_task_notice_batch",
            "notification_id": batch_id,
            "count": len(items),
            "items": [{
                "event_id": item.get("event_id") or item.get("notification_id") or "",
                "notification_id": item.get("notification_id") or "",
                "task_id": item.get("task_id", ""),
                "logical_task_id": item.get("logical_task_id") or item.get("task_id", ""),
                "status_version": item.get("status_version", 0),
                "assistant_name": item.get("assistant_name", ""),
                "title": str(item.get("title") or "")[:40],
                "status": item.get("task_status") or item.get("status") or "",
                "execution_conversation_id": item.get("execution_conversation_id"),
                "output_message_id": item.get("output_message_id"),
                "reasoning_status": item.get("reasoning_status"),
                "delivery_attempt_id": item.get("delivery_attempt_id"),
            } for item in items],
        })
        text = build_batch_notice_text(items)
        previous_notice_id = self._current_notice_id
        self._current_notice_id = batch_id
        try:
            await self._speak_text(text, wait=True, notification_id=batch_id)
            self._notice_state(batch_id)["generation_complete"] = True
            if self._active_notification_id == batch_id:
                self._active_notification_generation_complete = True
        finally:
            self._current_notice_id = previous_notice_id
            self._turn_active = False
            self._tools_running = False
            if not self._closed:
                await self._set_state("listen")

    async def _enqueue_bg_task_notice(self, info: dict, notification_id: Optional[str] = None) -> None:
        """Queue an out-of-band background-task notice. Riding the normal turn
        queue means the announcement never interrupts an in-flight user turn —
        it is delivered as soon as the session goes idle."""
        _sync_voicefillermixin_globals()
        if self._closed:
            return
        if notification_id and notification_id in self._queued_notice_ids:
            return
        if notification_id:
            self._queued_notice_ids.add(notification_id)
        logger.info(
            "voice bg-task notice queued: %s status=%s title=%r",
            info.get("task_id"), info.get("status"), info.get("title"),
        )
        await self._turn_queue.put({"notice": info, "notification_id": notification_id})

    async def _replay_pending_notices(self) -> None:
        """Claim durable notices for this conversation and queue each once."""
        _sync_voicefillermixin_globals()
        if self._closed or self.user is None or not self.conversation_id:
            return
        try:
            from app.db.database import AsyncSessionLocal
            from app.services.voice_notification_service import claim_voice_notifications

            async with AsyncSessionLocal() as db:
                notices = await claim_voice_notifications(db, self.user.id, self.conversation_id)
            prepared = []
            for item in notices:
                info = dict(item.get("info") or {})
                info["delivery_attempt_id"] = item.get("delivery_attempt_id")
                info["notification_id"] = item.get("notification_id")
                prepared.append((item["notification_id"], info))
            try:
                from app.core.config import get_config as _get_cfg
                batch_max = max(1, int(_get_cfg().voice_delegation_notification_batch_max))
            except Exception:
                batch_max = 5
            if len(prepared) >= 2:
                import uuid as _uuid

                # §10.2: coalesce a completion burst into one announcement.
                while prepared:
                    group = [
                        (nid, info) for nid, info in prepared[:batch_max]
                        if nid not in self._queued_notice_ids
                    ]
                    prepared = prepared[batch_max:]
                    if not group:
                        continue
                    for nid, _info in group:
                        self._queued_notice_ids.add(nid)
                    await self._turn_queue.put({
                        "notice_batch": {
                            "batch_id": f"batch:{_uuid.uuid4().hex[:12]}",
                            "items": [info for _nid, info in group],
                        },
                    })
                return
            for nid, info in prepared:
                if nid in self._queued_notice_ids:
                    continue
                await self._enqueue_bg_task_notice(info, notification_id=nid)
        except Exception:
            logger.debug("voice pending-notice replay failed", exc_info=True)

    async def _notification_replay_loop(self) -> None:
        """Reconnect/restart safety net; immediate first replay, then bounded polling."""
        _sync_voicefillermixin_globals()
        while not self._closed:
            await self._replay_pending_notices()
            try:
                await asyncio.sleep(15.0)
            except asyncio.CancelledError:
                raise
