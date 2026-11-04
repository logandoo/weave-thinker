# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from app/services/voice_service.py (baseline 883ac883, C-04, 2026-10-19).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (   'Optional',
    '_ASR_NOISE_RE',
    '_IDENTITY_WAIT_SECONDS',
    '_SAFE_RESUME_BOUNDARIES',
    '_STOP_TASK_EXACT_RE',
    '_STOP_TASK_RE',
    '_TERMINAL_PUNCT',
    '_is_likely_echo',
    '_is_voice_noise',
    '_now',
    '_perf',
    '_pre_classify_reusable',
    '_strip_lead_punct',
    '_utterance_complete',
    'asyncio',
    'get_config',
    'logger',
    're')


def _sync_voiceturn_globals() -> None:
    from app.services import voice_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class VoiceTurnMixin:
    async def _on_user_turn(self, text: str) -> None:
        # Serialized: EoT flushes spawn concurrent _on_user_turn tasks, and
        # the pause/resume + interrupt state machine must never interleave (a
        # backchannel resume must not clear/race a concurrent interrupt).
        _sync_voiceturn_globals()
        async with self._turn_handling_lock:
            await self._on_user_turn_locked(text)
    async def _on_user_turn_locked(self, text: str) -> None:
        # 部署排空扩面（D-12，scoped 复审新发现）：drain 中整轮停摆——barge
        # 分类/预取/ack 语音/入队全部不发生（它们是新 LLM run 或对永不作答之轮
        # 的承诺）；错误事件给用户可感知反馈。_enqueue_turn 门为二道防线。
        _sync_voiceturn_globals()
        from app.services.active_agent_registry import ActiveAgentRegistry
        if ActiveAgentRegistry.get_instance().is_draining:
            logger.info("voice user turn dropped (server draining): %r", text)
            try:
                await self._send_json({"event": "error", "error": "server_draining"})
            except Exception:
                pass
            return
        # Normalise: a genuine user utterance never starts with punctuation.
        # Streaming ASR can emit a slice beginning with the previous sentence's
        # terminator (e.g. "，快点"); strip it so turns are clean.
        text = _strip_lead_punct((text or "").strip())
        if not text:
            return

        # Explicit task-cancellation: if a task is running OR audio is still
        # being played (incl. the audible tail after a finished answer — the
        # user hears speech, so "停下" must work then too), a clear stop
        # command cancels it. The task loop checks _task_cancelled before each
        # tool round / LLM call and breaks out. Short commands (停下/等下) must
        # be the whole utterance; longer unambiguous phrases match as substrings.
        if (self._turn_active or self._speaking) and (
            _STOP_TASK_RE.search(text) or _STOP_TASK_EXACT_RE.match(text)
        ):
            self._task_cancelled = True
            # A stop-word also supersedes any onset pause (its breakpoint is
            # meaningless once the answer is abandoned).
            self._playback_paused = False
            self._paused_spoken_chars = 0
            self._interrupt.set()
            self._last_barge_in_time = _now()
            self._drain_tts_queue()
            await self._mark_speaking_end()
            await self._send_json({"event": "interrupted"})
            await self._send_json({"event": "task_cancelled", "text": text})
            logger.info("voice task cancelled by user: %r", text)
            return

        # Barge-in handling while speaking, OR after an acoustic onset-pause
        # (_playback_paused — speech already stopped the audio; the classifier
        # now decides resume-from-breakpoint vs switch to the new turn).
        if (self._speaking or self._playback_paused) and self.config.voice_barge_in_enabled:
            # Pre-filter: obvious ASR noise never interrupts TTS playback. In
            # the paused state the noise/echo verdict means "not a real turn"
            # → resume the paused answer from its breakpoint.
            if _ASR_NOISE_RE.match(text):
                logger.info("voice barge-in skipped (ASR noise): %r", text)
                if self._playback_paused:
                    await self._resume_playback()
                else:
                    await self._send_json({"event": "backchannel", "text": text})
                await self._set_state("speak")
                return
            # Deterministic echo guard: the agent's OWN TTS voice echoing back
            # through the mic is the #1 cause of mid-playback truncation. If the
            # interjection overlaps what is currently being spoken, it is echo,
            # not a real turn — keep playing (or resume).
            if _is_likely_echo(text, self._spoken_text_recent):
                logger.info("voice barge-in skipped (own-voice echo): %r", text)
                if self._playback_paused:
                    await self._resume_playback()
                else:
                    await self._send_json({"event": "backchannel", "text": text})
                await self._set_state("speak")
                return
            # If the agent is interjecting (a brief remark during the user's
            # speech), the user's completed turn always takes priority — cut
            # off the interjection immediately and process the turn. This check
            # runs AFTER the noise/echo filters so a stray noise fragment can't
            # kill an in-flight interjection.
            if self._interjecting:
                self._playback_paused = False
                self._paused_spoken_chars = 0
                self._interrupt.set()
                self._last_barge_in_time = _now()
                self._drain_tts_queue()
                await self._mark_speaking_end()
                self._interjecting = False
                logger.info("voice interjection cut off by user turn: %r", text)
                self._enqueue_turn(text)
                return
            # No-interrupt window + cooldown (only while actively PLAYING — a
            # paused playback already passed these gates at the onset pause).
            # In the first instants of a playback burst (and right after a
            # previous interrupt) the mic is most likely to pick up echo/reverb,
            # so a real utterance there is deferred instead of cutting playback:
            # it is still queued and will be answered as soon as the burst ends.
            if not self._playback_paused:
                now = _now()
                if (
                    now - self._speaking_started_at
                    < self.config.voice_barge_in_no_interrupt_seconds
                ) or (
                    now - self._last_barge_in_time
                    < self.config.voice_barge_in_cooldown_seconds
                ):
                    logger.info(
                        "voice barge-in deferred (window/cooldown): %r", text
                    )
                    self._enqueue_turn(text)
                    self._start_prefetch(text)
                    await self._send_json({"event": "deferred", "text": text})
                    await self._set_state("speak")
                    return
            self._barge_classify_start = _now()
            # Reuse the pre-classify verdict (fired at the onset pause, in
            # parallel with the EoT watchdog) when the flushed text matches
            # the classified partial — saves the ~1-1.5s classify on the
            # critical path, keeping the pause→resume silence within the
            # user's patience (previously 4-5s: users refreshed and the
            # answer stayed truncated).
            pre = self._pre_classify
            self._pre_classify = None
            if _pre_classify_reusable(pre, text):
                action = pre["action"]
                logger.info("voice barge-in classify (pre): action=%s text=%r", action, text)
            else:
                action = await self._classify_barge_in(text, history=list(self._history))
                logger.info("voice barge-in classify: action=%s text=%r", action, text)
            if action == "backchannel":
                self._record_backchannel(text)
                if self._playback_paused:
                    await self._resume_playback()
                else:
                    await self._send_json({"event": "backchannel", "text": text})
                await self._set_state("speak")
                return
            if action == "interrupt":
                await self._request_interrupt()
                self._enqueue_turn(text)
                return
            # defer: resume the paused answer (or keep playing), pre-generate
            # the reply for when the current answer ends.
            if self._playback_paused:
                await self._resume_playback()
            else:
                self._start_prefetch(text)
            self._enqueue_turn(text)
            await self._send_json({"event": "deferred", "text": text})
            await self._set_state("speak")
            return

        # Think-phase preemption: the assistant is GENERATING an answer but no
        # audio has reached the user yet (no barge-in path above). Without a
        # judgment here every non-stop-word utterance is queued and the user
        # waits for the whole in-flight answer to stream AND play out before
        # their interruption is handled — "can't interrupt". Run the SAME
        # barge-in classifier (LLM-based, never hardcoded): a clear interrupt
        # (new question, topic change, correction, stop) aborts the in-flight
        # generation NOW. Fail-safe (timeout/error) = defer: never abort a
        # stream on uncertainty. Tool execution is never aborted here (the
        # queued turn preempts the tool loop at the next round boundary).
        # NOTE: "think" here is the session state-machine state (waiting for
        # first audio), NOT the LLM thinking/reasoning mode — voice LLM calls
        # (main stream AND these classifier subagents) always run with
        # thinking disabled (voice.disable_thinking / _thinking_off_body).
        # The classify contends for _llm_gate with the in-flight main stream
        # and its intent subagent; a 6s gate wait degrades this to the
        # baseline defer path (the turn still gets answered afterwards), which
        # is the accepted trade-off vs. tripping provider rate limits.
        if (
            self._turn_active
            and not self._speaking
            and not self._tools_running
            and self.config.voice_barge_in_enabled
        ):
            self._barge_classify_start = _now()
            action = await self._classify_barge_in(text, history=list(self._history))
            logger.info(
                "voice think-phase barge-in classify: action=%s text=%r", action, text,
            )
            # Re-check state after the await: the in-flight turn may have
            # completed while the classifier ran — aborting then is pointless
            # and would set a spurious barge-in cooldown. (Audio may have
            # started during the classify — that is fine: cutting an answer
            # after audio began is exactly the speaking-path barge-in
            # semantic, the decision was already approved by the classifier.)
            if action == "interrupt" and self._turn_active and not self._closed:
                await self._request_interrupt()
                logger.info("voice think-phase preempted by user turn: %r", text)
                self._enqueue_turn(text)
                return

        self._enqueue_turn(text)
        if self._turn_active and not self._closed:
            # The user spoke while a turn (e.g. a long tool loop) is still
            # running. Their utterance is queued — acknowledge IMMEDIATELY so
            # they are never met with total silence. Substantive utterances
            # also get a brief spoken ack (cooldown-gated); the queued turn
            # itself preempts the tool loop at the next round boundary.
            await self._send_json({"event": "turn_queued", "text": text})
            if (
                self._tools_running
                and len(text) > self.config.voice_noise_gate_max_chars
                and _now() - self._last_queue_ack > 15.0
            ):
                self._last_queue_ack = _now()
                asyncio.create_task(self._speak_text(
                    "(平静)收到，你先等我把手头这步做完，马上就来回答你。"
                ))
    def _start_prefetch(self, text: str) -> None:
        """Pre-generate reply text for a deferred turn while speech continues."""
        _sync_voiceturn_globals()
        if self._prefetch.get("task") and not self._prefetch["task"].done():
            return
        self._prefetch = {
            "turn": text,
            "text": None,
            "task": asyncio.create_task(self._generate_reply_text(text)),
            "summary_note": self._pending_tool_summary_note,
        }
    async def _generate_reply_text(self, text: str) -> str:
        """Generate the full reply text without speaking (used for prefetch)."""
        _sync_voiceturn_globals()
        svc, _ = self._build_llm()
        messages = self._generation_messages(text)
        out = []
        # Runs through the same provider gate as the main stream — the defer
        # path fires while the main stream and intent classify may still hold
        # gate slots; an ungated third call was the exact xiaomimimo 429
        # trigger the gate exists to prevent (A4.9 review finding #2).
        try:
            async with self._llm_gate:
                async for ev in svc.stream_chat_structured(
                    messages,
                    temperature=self.config.voice_temperature,
                    max_tokens=self.config.voice_max_tokens,
                    extra_body=self._extra_body(svc),
                ):
                    if ev["type"] == "content":
                        out.append(ev["data"])
        except Exception as exc:
            logger.debug("voice prefetch error: %s", exc)
        result = "".join(out)
        self._prefetch["text"] = result
        return result
    async def _request_interrupt(self) -> None:
        # Any confirmed interrupt SUPERSEDES an acoustic onset pause: the
        # answer is being abandoned, so the pause state (and its breakpoint)
        # must not linger — a later backchannel would otherwise "resume" a
        # stale breakpoint against the NEW turn's text (A4.9 finding C1).
        _sync_voiceturn_globals()
        self._playback_paused = False
        self._paused_spoken_chars = 0
        self._interrupt.set()
        self._last_barge_in_time = _now()
        # Interrupt cancels active/current audio and older notices retained by
        # the client's drain map. Future turn-queue entries were never started;
        # keep their latch so the surviving original cannot be duplicated.
        cancelled_notice_ids = {self._active_notification_id, self._current_notice_id}
        for notice_id in list(self._queued_notice_ids):
            notice_state = self._notification_states.get(notice_id, {})
            if (
                not notice_state.get("delivered")
                and (notice_id in cancelled_notice_ids or notice_state.get("audio_sent"))
            ):
                self._queued_notice_ids.discard(notice_id)
        self._active_notification_id = None
        self._active_notification_audio_sent = False
        self._active_notification_generation_complete = False
        self._drain_tts_queue()
        # Attribute the truncation to AUDIO PLAYBACK, never to the text. The
        # answer text is complete; only its spoken playback was cut. Compute how
        # much had actually been voiced when playback stopped, and queue a note
        # so the next turn's LLM KNOWS this (instead of confabulating a reason
        # like "the system thought I talked too much").
        # The client's played_sec is BURST-relative (restarted at each
        # pause→resume — see the frontend playback_resumed handler), so the
        # turn-absolute heard position is _burst_base_sec + played_sec;
        # mapping the raw value against the turn-relative segment timeline
        # would report "~2% played" right after a resume (A4.9 finding).
        played = self._burst_base_sec + self._playback_played_sec
        total = self._turn_audio_sec_total
        # The interruption-note source: _turn_reply_text covers the WHOLE turn
        # (generated text, incl. before any audio); _turn_segments only covers
        # text whose audio was actually synthesized. Fall back to segments so
        # the prefetch path (no _turn_reply_text) keeps working.
        full = self._turn_reply_text or "".join(s["text"] for s in self._turn_segments)
        # Truncation happened if a meaningful tail of audio never played.
        if full.strip() and total > 0 and played < total - 0.3:
            spoken = self._spoken_text_so_far(played)
            tail = spoken[-24:] if spoken else "（开头不久）"
            pct = int(100 * played / total) if total else 0
            self._pending_interruption_note = (
                "（系统提示，不要朗读）：你上一段语音播报只播出了约 "
                f"{pct}% ，在「" + tail + "…」附近就被用户打断了，后面的内容没有播给用户听。用户很可能没听全。"
                "如果用户追问为什么中断/被截断/没说完，请如实说明是【语音播报被打断】而不是回答本身"
                "有问题，并主动用简短的话把没播完的内容补全或询问是否继续，不要编造其他原因。）"
            )
            logger.info(
                "voice playback interrupted (audio cut): played=%.2fs/%.2fs (%d%%) spoken=%d/%d chars",
                played, total, pct, len(spoken), len(full),
            )
        elif full.strip() and total <= 0:
            # Think-phase abort: an answer was being GENERATED (text shown in
            # the transcript) but its audio never started — the user heard
            # nothing. The next turn must know the previous answer was never
            # delivered so it can offer to re-answer instead of treating the
            # partial text as delivered (A4.9 finding).
            self._pending_interruption_note = (
                "（系统提示，不要朗读）：你上一轮生成的内容还没开始播报就被用户打断了，"
                "用户没有听到这段内容。如果用户追问或要求继续，请如实说明这段回答没有播出来，"
                "并简要重述要点或询问是否需要重讲，不要编造其他原因。"
            )
            logger.info(
                "voice think-phase answer aborted before playback (spoken=%d chars)",
                len(full),
            )
        await self._mark_speaking_end()
        await self._send_json({"event": "interrupted"})
    async def _pause_playback(self) -> None:
        """Acoustic onset barge-in: speech detected during playback — stop TTS
        IMMEDIATELY (before EoT + LLM classification would decide, ~2s). The
        barge-in classifier later decides: interrupt → answer the new turn;
        backchannel/defer → `_resume_playback()` from the breakpoint. The
        pause itself is NOT an intent decision — it is the acoustic-layer
        equivalent of FireRedChat's pVAD onset stop; echo/noise pre-filters
        and the window/cooldown gates run at the CALLER (see _asr_pipeline).
        Deliberately does NOT set `_interrupt`: the in-flight GENERATION keeps
        running while the audio is paused, so a defer outcome can resume the
        FULL answer (generated tail included) instead of a truncated one
        (A4.9 finding I2). The TTS consumer checks `_playback_paused`."""
        _sync_voiceturn_globals()
        if self._playback_paused or self._closed:
            return
        self._playback_paused = True
        # Only active/current notice audio can be resumed by this breakpoint.
        # Older retained drains are cancelled by the client's clearPlayback;
        # release their latch without touching the resumable notice.
        resumable_notice_ids = {self._active_notification_id, self._current_notice_id}
        for notice_id in list(self._queued_notice_ids):
            notice_state = self._notification_states.get(notice_id, {})
            if (
                notice_id not in resumable_notice_ids
                and notice_state.get("audio_sent")
                and not notice_state.get("delivered")
            ):
                self._queued_notice_ids.discard(notice_id)
        completed = "".join(s["text"] for s in self._turn_segments)
        full_txt = self._turn_reply_text or ""
        # Breakpoint = how much the user ACTUALLY HEARD, not how much was
        # SENT. The pipelined TTS prefetch races the consumer ahead of the
        # client by seconds of buffered audio, so `_turn_segments` (segments
        # whose audio was handed to the websocket) can say "everything" while
        # the user has heard a fraction — a pause then computed a full-text
        # breakpoint and the resume had "no remaining content", truncating
        # the answer (observed live: heard "欢迎回来！我在这儿呢…还" of a
        # 33-char answer, resume was empty).
        # PRIMARY: the client's AUDIBLE position (played_sec) mapped to chars
        # with the chars-per-second rate of the COMPLETED segments — both
        # measured from the same audio, so the mapping is self-consistent.
        # NOT played/total_sec: mid-stream the client's total is still
        # growing (only part of the answer has been received), so that
        # fraction overestimates progress and SKIPS unheard content
        # (observed live 13:47: paused at 5.48s of an ~40s answer, the
        # played/total mapping landed at char 80 of 164 — 40+ chars of
        # unheard speech were skipped).
        if full_txt and self._playback_played_sec > 0:
            # PRIMARY: the client's AUDIBLE position (played_sec, relative to
            # the CURRENT burst) mapped to chars with the turn's chars-per-
            # second rate (completed segments only — the in-flight segment is
            # covered by played_sec itself). The burst base anchors resumed
            # bursts: after a pause→resume the client restarts its clock at 0
            # from the resume point, so the full-text position is
            # burst_base_chars + played_sec * rate. WITHOUT the base, a
            # post-resume pause would estimate a position inside the resume
            # segment and `full_txt[est:]` replays already-heard text
            # (observed live conv 964462a8: pause#2 est=175 vs correct ~325
            # → an ~18s audible repeat of chars 175-354).
            comp_audio = sum(s["audio_sec"] for s in self._turn_segments)
            if len(completed) > 0 and comp_audio > 0:
                rate = len(completed) / comp_audio
                est = self._burst_base_chars + int(self._playback_played_sec * rate)
            elif self._burst_is_first and self._current_seg_text:
                # No completed segments — the FIRST segment of the turn's
                # ORIGINAL burst is still in flight: estimate its played
                # fraction. Safe only here (the segment is short and the snap
                # below guarantees a word boundary). NEVER on a resumed burst
                # (_burst_is_first False): the resumed remainder is one large
                # segment and _current_seg_audio_sec (sent so far) is far
                # below its total, so a fraction of it would SKIP unheard
                # content — replay from the resume point instead.
                frac = 0.0
                if self._current_seg_audio_sec > 0:
                    frac = min(
                        1.0,
                        max(0.0, self._playback_played_sec)
                        / self._current_seg_audio_sec,
                    )
                est = int(len(self._current_seg_text) * frac)
            else:
                # Pause in the first ~second of a resumed burst (or no client
                # position): the burst base plus what completed IN this burst
                # — never skip unheard content.
                est = self._burst_base_chars + self._completed_chars_in_burst()
        else:
            # FALLBACK (no client position — headless clients): the burst base
            # plus segments completed WITHIN this burst only (the epoch-wide
            # completed prefix already lies BEFORE the burst and is counted by
            # the base — adding it again would overshoot past the text end and
            # truncate the resume).
            est = self._burst_base_chars + self._completed_chars_in_burst()
        # SNAP to a safe clause boundary: the raw estimate is an exact
        # character count that can land MID-WORD (observed in the wild:
        # "...刚才可能是信|号..." and "...吃过|饭..."), and the resume
        # re-synthesizes from that character — a mid-word cut sounds broken.
        # Scan the last 8 chars for a boundary (punctuation/space/closing
        # style-tag paren); if none, scan the whole prefix (a natural
        # restart point); last resort: the very start (repeat, never cut).
        if full_txt and 0 < est < len(full_txt):
            lo = max(0, est - 8)
            window = full_txt[lo:est]
            safe = -1
            for i, ch in enumerate(window):
                if ch in _SAFE_RESUME_BOUNDARIES:
                    safe = i
            if safe >= 0:
                est = lo + safe + 1
            else:
                safe2 = -1
                for i, ch in enumerate(full_txt[:est]):
                    if ch in _SAFE_RESUME_BOUNDARIES:
                        safe2 = i
                est = (safe2 + 1) if safe2 >= 0 else 0
        self._paused_spoken_chars = est
        self._last_barge_in_time = _now()
        self._drain_tts_queue()
        # Pre-synthesize the unspoken remainder WHILE the barge-in classifier
        # runs (~1s) so a backchannel resume plays almost immediately instead
        # of paying a fresh provider round-trip (~0.9s) — the audible
        # pause→resume gap drops by roughly half. ignore_paused: this synth
        # must run DURING the pause (that is its purpose); it is still gated
        # by interrupt/close and dropped on any non-resume outcome.
        remaining = _strip_lead_punct(full_txt[self._paused_spoken_chars:]).strip()
        self._resume_epoch = 0
        self._resume_q = None
        self._resume_text = ""
        if remaining:
            self._resume_epoch = self._turn_epoch
            self._resume_q = asyncio.Queue()
            self._resume_text = remaining
            resume_kwargs = {"ignore_paused": True}
            resume_notification_id = self._active_notification_id or self._current_notice_id
            if resume_notification_id:
                resume_kwargs["notification_id"] = resume_notification_id
            self._spawn_synth(
                remaining,
                self._turn_epoch,
                self._resume_q,
                **resume_kwargs,
            )
        await self._mark_speaking_end()
        await self._send_json({
            "event": "playback_paused",
            "paused_at_chars": self._paused_spoken_chars,
        })
        logger.info(
            "voice playback paused (onset): spoken_chars=%d (client played %.2fs, sent %.2fs)",
            self._paused_spoken_chars, self._playback_played_sec, self._turn_audio_sec_total,
        )
    async def _run_pre_classify(self, text: str) -> None:
        """Background barge-in classify fired at the acoustic onset pause
        (parallel with the EoT watchdog). The verdict is only reused by
        _on_user_turn_locked when the flushed text matches this partial."""
        _sync_voiceturn_globals()
        # Snapshot BEFORE the await (A4.9 I1): the flush path may consume
        # `self._pre_classify` (set it to None) while the LLM call is in
        # flight. The identity check `self._pre_classify is pre` below only
        # means anything if `pre` was captured here — a post-await read makes
        # the guard a no-op (both refer to the same current value).
        pre = self._pre_classify
        try:
            action = await self._classify_barge_in(text, history=list(self._history))
            if pre and pre.get("text") == text:
                pre["action"] = action
                logger.info("voice pre-classify: action=%s text=%r", action, text)
            if action == "backchannel" and pre is not None and self._pre_classify is pre:
                # Record so the responder can skip a queued copy of this
                # utterance that was already enqueued by the window/cooldown
                # defer path (ghost-message fix, conv 689f06ec). Only when the
                # verdict is still authoritative: if the flush consumed it
                # (re-classified the SAME sentence, possibly as interrupt) or
                # a newer onset replaced it, recording a stale backchannel
                # would wrongly recall a REAL interrupt turn into silence.
                self._record_backchannel(text)
        except Exception as exc:
            logger.debug("voice pre-classify failed: %s", exc)
    async def _resume_playback(self) -> None:
        """Resume a paused answer from the breakpoint (backchannel/defer
        outcome of the barge-in classifier). Re-synthesizes the unspoken
        remainder of the generated text — the already-played prefix is not
        repeated. No-op when nothing was paused. Does NOT touch `_interrupt`:
        under the _turn_handling_lock no concurrent interrupt can be in flight,
        and the pause itself never set it (see _pause_playback)."""
        _sync_voiceturn_globals()
        if not self._playback_paused or self._closed:
            return
        self._playback_paused = False
        full = self._turn_reply_text or ""
        # Never resume from a leading punctuation: the breakpoint snap can
        # land right before a clause boundary, making the remainder start
        # with "。"/"，" — such text defeats _split_segments (no split → one
        # giant segment) and a leading "。(风格)…" input hung the MiMo
        # provider, stalling the consumer forever (observed live).
        remaining = _strip_lead_punct(full[self._paused_spoken_chars:]).strip() if full else ""
        resumed_notification_id = self._active_notification_id or self._current_notice_id
        notice_state = self._notification_states.get(resumed_notification_id, {}) if resumed_notification_id else {}
        notice_burst_seq = int(notice_state.get("burst_seq", 0) or 0) + 1
        if resumed_notification_id:
            notice_state["burst_seq"] = notice_burst_seq
        await self._send_json({
            "event": "playback_resumed",
            "notification_id": resumed_notification_id,
            "has_remaining": bool(remaining),
            "notice_burst_seq": notice_burst_seq,
        })
        # The resumed audio is a NEW audible burst: the client restarts its
        # playback clock at 0 from here, so anchor the pause-breakpoint math
        # to this full-text position and audio offset until the next pause
        # (see _pause_playback). The burst is no longer the turn's first one,
        # so the in-flight played-fraction estimate must never apply (the
        # resumed remainder is one large segment — see _pause_playback).
        self._burst_base_chars = self._paused_spoken_chars
        self._burst_base_sec = self._turn_audio_sec_total
        self._burst_is_first = False
        self._paused_spoken_chars = 0
        if remaining:
            # Prefer the pre-synthesized remainder (spawned at pause time so
            # the provider round-trip overlaps the barge-in classifier) — the
            # resumed audio then starts almost immediately instead of after a
            # fresh ~0.9s synthesis. Guards: same turn epoch, no interrupt,
            # and the queue must not have been dropped by a drain.
            # The pre_q item carries the SNAPSHOT text synthesized at pause
            # time (`_resume_text`) — the answer may have KEPT STREAMING
            # during the pause, so the current `remaining` can be longer than
            # what the queue's audio covers; the extra tail (delta) is
            # enqueued as fresh segments after the pre_q item instead of
            # being silently swallowed (observed live 13:47: pre_q text said
            # 106 chars but its audio covered only 84 — the last 22 chars
            # were never heard).
            if (
                self._resume_q is not None
                and self._resume_epoch == self._turn_epoch
                and not self._interrupt.is_set()
                and not self._closed
            ):
                q = self._resume_q
                snap = self._resume_text or ""
                self._resume_q = None
                self._resume_text = ""
                if snap and remaining.startswith(snap):
                    # The delta is the text that streamed AFTER the pause —
                    # it can begin with a punctuation that the snapshot did
                    # not cover (e.g. the LLM emitted "。…" as the first token
                    # of a new chunk); strip it so the delta splits normally
                    # and the provider never receives a leading "。" text.
                    delta = _strip_lead_punct(remaining[len(snap):]).strip()
                    logger.info(
                        "voice playback resumed via pre-synthesized remainder: %d chars (+%d streamed during pause)",
                        len(snap), len(delta),
                    )
                    await self._tts_queue.put({
                        "text": snap,
                        "epoch": self._turn_epoch,
                        "pre_q": q,
                        "notification_id": self._active_notification_id or self._current_notice_id,
                    })
                    if delta:
                        await self._speak_text(
                            delta,
                            wait=False,
                            notification_id=self._active_notification_id or self._current_notice_id,
                        )
                else:
                    # Snapshot diverged from the current text (defensive) —
                    # fresh synthesis covers everything.
                    logger.info(
                        "voice playback resumed from breakpoint: remaining=%d chars",
                        len(remaining),
                    )
                    await self._speak_text(
                        remaining,
                        notification_id=self._active_notification_id or self._current_notice_id,
                    )
            else:
                logger.info(
                    "voice playback resumed from breakpoint: remaining=%d chars",
                    len(remaining),
                )
                await self._speak_text(
                    remaining,
                    notification_id=self._active_notification_id or self._current_notice_id,
                )
        else:
            logger.info("voice playback resumed: no remaining content")
        if resumed_notification_id:
            notice_state = self._notification_states.get(resumed_notification_id, {})
            if notice_state.get("llm_succeeded") and not notice_state.get("tts_failed"):
                await self._tts_queue.put({
                    "flush": None,
                    "notification_id": resumed_notification_id,
                    "notification_complete": True,
                })
                if not remaining:
                    # All text was already heard. Settle directly instead of
                    # racing a client ACK against the restored completion flush.
                    notice_state["delivered"] = True
                    self._active_notification_id = None
                    self._active_notification_audio_sent = False
                    self._active_notification_generation_complete = False
                    try:
                        from app.db.database import AsyncSessionLocal
                        from app.services.voice_notification_service import mark_voice_notification_delivered

                        async with AsyncSessionLocal() as db:
                            await mark_voice_notification_delivered(db, resumed_notification_id)
                    except Exception:
                        self._queued_notice_ids.discard(resumed_notification_id)
                        logger.debug("voice zero-remaining notice ACK failed: %s", resumed_notification_id, exc_info=True)
    async def _responder(self) -> None:
        _sync_voiceturn_globals()
        while not self._closed:
            item = await self._turn_queue.get()
            if item is None:
                return
            notice_batch = item.get("notice_batch")
            if notice_batch:
                retry_at = float(item.get("retry_at", 0.0) or 0.0)
                if retry_at > _now():
                    self._turn_queue.put_nowait(item)
                    await asyncio.sleep(min(0.25, retry_at - _now()))
                    continue
                try:
                    await self._deliver_notice_batch(notice_batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    for info in (notice_batch.get("items") or []):
                        nid = info.get("notification_id") if isinstance(info, dict) else None
                        if nid:
                            self._queued_notice_ids.discard(nid)
                    logger.error("voice bg-task notice batch error: %s", exc, exc_info=True)
                continue
            notice = item.get("notice")
            if notice:
                retry_at = float(item.get("retry_at", 0.0) or 0.0)
                if retry_at > _now():
                    # A notice whose predecessor is still audible goes to the
                    # back of the queue, so newer user turns are never blocked.
                    self._turn_queue.put_nowait(item)
                    await asyncio.sleep(min(0.25, retry_at - _now()))
                    continue
                # Out-of-band background-task completion notice. Riding the
                # turn queue keeps it serialized with user turns: it waits
                # for the in-flight turn instead of interrupting it.
                try:
                    await self._deliver_bg_task_notice(
                        notice, notification_id=item.get("notification_id")
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("voice bg-task notice error: %s", exc, exc_info=True)
                continue
            text = item.get("text", "")
            item_input_seq = int(item.get("input_seq", 0) or 0)
            try:
                text = await self._coalesce_fragments(
                    text,
                    skip_wait=bool(item.get("semantic_complete")),
                    input_seq=item_input_seq,
                )
                if not text.strip():
                    self._clear_pending_filler()
                    continue
                # A queued turn whose utterance was already judged backchannel
                # (window/cooldown defer + late pre-classify verdict, or a
                # flush-time backchannel) must NOT be answered — it is filler,
                # and answering it leaves a ghost unanswered message (conv
                # 689f06ec: "嗯，没啥，我再跟你聊聊。" persisted after the
                # real answer, orphaned).
                if await self._queued_turn_is_backchannel(text):
                    ignored_seqs = set(self._coalesced_input_seqs)
                    while self._latest_input_seq > 0 and self._latest_input_seq in ignored_seqs:
                        self._latest_input_seq -= 1
                    self._clear_pending_filler()
                    await self._send_json({"event": "backchannel", "text": text})
                    await self._set_state("speak")
                    continue
                await self._handle_user_turn(text, input_seq=self._coalesced_input_seq)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("voice handle turn error: %s", exc, exc_info=True)
                await self._send_json({"event": "error", "error": f"处理出错: {exc}"})
                await self._set_state("listen")
    async def _coalesce_fragments(self, text: str, skip_wait: bool = False, input_seq: int = 0) -> str:
        """Reassemble one continuous utterance that the EoT chopped into
        several queued fragments. When upstream ASR results stall (noise
        threshold, reconnect, echo mix), the watchdog can flush mid-speech;
        without coalescing every fragment becomes its own answered turn and
        the conversation shatters (observed 2026-07-21: one utterance → 11
        fragment turns, each interrupting the previous answer).

        Strategy: fragments already queued (backlog — the cascade signature)
        merge immediately, and while the accumulated text lacks terminal
        punctuation (the signature of a cut) a short probe window waits for
        the next fragment, re-arming on each arrival, bounded overall by
        ``voice_fragment_merge_max_seconds``. A complete turn with an empty
        queue returns instantly — normal short queries see zero added
        latency. Deliberately NO waiting on live unflushed partials: a single
        chopped head is answered quickly and its tail becomes a follow-up
        turn with full context, which beats stalling the head answer."""
        _sync_voiceturn_globals()
        self._coalesced_input_seq = input_seq
        self._coalesced_input_seqs = [input_seq]
        merged = text
        deadline = _now() + self.config.voice_fragment_merge_max_seconds
        while not self._closed:
            nxt_item = None
            got = False
            if not self._turn_queue.empty():
                try:
                    nxt_item = self._turn_queue.get_nowait()
                    got = True
                except asyncio.QueueEmpty:
                    pass
            if not got:
                if skip_wait or _utterance_complete(merged) or _now() >= deadline:
                    break
                wait = min(self.config.voice_fragment_merge_seconds, deadline - _now())
                try:
                    nxt_item = await asyncio.wait_for(self._turn_queue.get(), timeout=wait)
                    got = True
                except asyncio.TimeoutError:
                    break
            if nxt_item is None:
                # Poison pill (shutdown) arrived mid-merge — hand it back to
                # the main responder loop so it can terminate cleanly.
                self._turn_queue.put_nowait(None)
                break
            if nxt_item.get("notice"):
                # Not a speech fragment — a background-task notice. Put it
                # back for the responder and process the merged text first.
                self._turn_queue.put_nowait(nxt_item)
                break
            next_input_seq = int(nxt_item.get("input_seq", 0) or 0)
            self._coalesced_input_seqs.append(next_input_seq)
            self._coalesced_input_seq = max(
                self._coalesced_input_seq,
                next_input_seq,
            )
            nxt = (nxt_item.get("text", "") or "").strip()
            if not nxt:
                continue
            merged = f"{merged}{nxt}" if merged else nxt
            logger.info("voice fragment merged: %d chars -> %r", len(nxt), merged[-40:])
        if merged != text:
            logger.info("voice turn coalesced: %r -> %r", text[:40], merged[:60])
        return merged
    async def _handle_user_turn(self, text: str, input_seq: int = 0) -> None:
        _sync_voiceturn_globals()
        self._interrupt.clear()
        self._style_tag = ""
        self._turn_active = True
        await self._set_state("think")

        # Deterministic noise pre-filter: fast check for transcribed ambient
        # sounds (coughs, sighs, throat clearing) before any LLM call. This
        # catches obvious noise that streaming ASR routinely emits, avoids
        # burning API calls, and never needs the fail-safe default.
        if _is_voice_noise(text):
            logger.info("voice turn ignored (deterministic noise): %r", text)
            self._turn_active = False
            if not self._closed:
                await self._set_state("listen")
            return

        # Send user_turn IMMEDIATELY so the frontend shows the user message
        # BEFORE the assistant's streaming response. If the intent classifier
        # later rejects this turn (noise/fragment), we send user_turn_cancelled
        # to remove it. This fixes the display-order bug where the assistant's
        # partial response appeared before the user's message.

        await self._send_json({"event": "user_turn", "text": text})

        # Use prefetched text if this turn was pre-generated during a defer.
        prefetched = None
        prefetched_summary_note = self._prefetch.get("summary_note", "")
        if self._prefetch.get("turn") == text:
            task = self._prefetch.get("task")
            if task:
                try:
                    prefetched = await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
                except Exception:
                    prefetched = None
            self._prefetch = {"turn": None, "text": None, "task": None}
        else:
            # Turn text no longer matches the prefetch (e.g. fragments were
            # coalesced into a longer turn) — cancel the stale generation so
            # it doesn't keep burning LLM tokens for a discarded fragment.
            stale = self._prefetch.get("task")
            if stale and not stale.done():
                stale.cancel()
            if self._prefetch.get("turn") is not None:
                self._prefetch = {"turn": None, "text": None, "task": None}

        # Filler prefix (TTFT-gated, 2026-09-13): no longer spoken here. The
        # decision lives in `_filler_ttft_watch`, scheduled by the generation
        # path: it plays only if the model has produced no first content token
        # within `voice_filler_ttft_threshold_seconds`. The old immediate
        # `_speak_filler()` call here made every turn open with a filler.
        self._last_persisted_user_message_id = None
        await self._persist_message("user", text)
        # Voice delegation needs the real source user message for the
        # idempotency key (voice:<source_message_id>).
        self._current_user_message_id = self._last_persisted_user_message_id
        # Snapshot of the recent dialogue (before this turn) for the subagent.
        # We intentionally exclude the very message being judged. Bound the
        # snapshot to voice_intent_context_turns (the config knob was defined
        # but never applied — the full history was formatted into the intent
        # prompt on every turn, growing its prefill without bound over a long
        # session and slowing the (non-blocking but wasteful) classifier).
        turns = self.config.voice_intent_context_turns
        if turns > 0:
            tagged = [
                m for m in self._history
                if m.get("role") in ("user", "assistant")
            ]
            history_snapshot = list(tagged[-turns * 2:])
        else:
            history_snapshot = list(self._history)

        # 每轮 fire-and-forget 记忆召回（vmem）：不进入关键路径。快照传拷贝
        # （后续 _history 会 mutate）。轮 epoch 用自增前值：本轮生成的实际
        # epoch = _turn_epoch + 1（_generate_and_speak 内自增），watcher 以
        # "> 钩子时点 epoch" 判定本轮生成结束。
        self._start_memory_recall(self._turn_epoch, text, list(history_snapshot))

        _perf("turn_start", (_now() - self._turn_enqueued_at) * 1000 if self._turn_enqueued_at else 0,
              text_len=len(text), queued=(self._turn_enqueued_at is not None))
        self._turn_started = _now()

        # Bound the wait for the concurrent identity/shared-memory load. A
        # slow provider must never stall the answer; the fallback prompt
        # applies for this turn and the loaded identity lands from the next
        # turn (2026-09-20 first-query TTFT fix).
        identity_task = self._identity_task
        if (
            identity_task is not None
            and not identity_task.done()
            and not self._identity_wait_done
        ):
            self._identity_wait_done = True
            try:
                await asyncio.wait_for(
                    asyncio.shield(identity_task), timeout=_IDENTITY_WAIT_SECONDS
                )
            except Exception:
                pass

        # Staggered parallel subagent + main-agent pattern: the main LLM starts
        # generating IMMEDIATELY while the intent subagent runs in parallel with
        # a short stagger. The stagger prevents simultaneous API requests to the
        # same provider from hitting rate limits (429 Too Many Requests observed
        # with xiaomimimo), which would otherwise trigger OpenAI-client retries
        # that add 0.5-1.5s of latency. The subagent is tiny (max_tokens=160)
        # so the stagger adds negligible time-to-first-audio overhead.
        generation_kwargs = {}
        if prefetched is not None:
            generation_kwargs["prefetched"] = prefetched
            generation_kwargs["prefetched_summary_note"] = prefetched_summary_note
        if input_seq:
            generation_kwargs["input_seq"] = input_seq
        gen_task = asyncio.create_task(
            self._generate_and_speak_cancellable(text, **generation_kwargs)
        )
        await asyncio.sleep(0.5)
        self._intent_classify_start = _now()
        intent_task = asyncio.create_task(
            self._classify_intent(text, history=history_snapshot)
        )

        # Wait for the subagent (it's the faster of the two).
        intent = None
        try:
            intent = await asyncio.wait_for(intent_task, timeout=10.0)
        except asyncio.TimeoutError:
            intent = {"should_respond": True, "needs_tools": False, "reason": ""}
        except Exception:
            intent = {"should_respond": True, "needs_tools": False, "reason": ""}

        if not intent.get("should_respond", True):
            # Off-topic/background noise — cancel the in-flight generation immediately.
            logger.info(
                "voice turn ignored (not respondable): %r reason=%s",
                text, intent.get("reason", ""),
            )
            gen_task.cancel()
            try:
                await gen_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            stale = self._prefetch.get("task")
            if stale and not stale.done():
                stale.cancel()
            self._prefetch = {"turn": None, "text": None, "task": None}
            # Remove the user message from the frontend (it was noise/fragment).
            await self._send_json({"event": "user_turn_cancelled"})
            await self._send_json({"event": "ignored", "text": text, "reason": intent.get("reason", "")})
            # Roll back the persisted user message so noise doesn't pollute the DB.
            await self._rollback_last_user_message(text)
            self._turn_active = False
            self._interjection_snapshot = []
            self._decay_emotion()
            if not self._closed:
                await self._set_state("listen")
            return

        # Subagent approved — the user_turn was already sent at the start of
        # this method. Just wait for generation to complete (it has already
        # been streaming while the subagent ran).

        # Wait for the (already-running) generation to finish. Tools stay
        # enabled for every turn on purpose: whether to call a tool is the main
        # LLM's own agentic decision (it sees the tool list and chooses), and
        # the intent subagent's needs_tools is only advisory — its fail-safe on
        # timeout/error is needs_tools=False, which would wrongly strip tools
        # from real task requests whenever the subagent is slow.
        try:
            await gen_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("voice generation error: %s", exc)

        self._turn_active = False
        self._tools_running = False
        self._interjection_snapshot = []
        self._decay_emotion()
        if not self._closed:
            await self._set_state("listen")
    async def _generate_and_speak(self, text: str, allow_tools: bool = False, prefetched: Optional[str] = None,
                                  allow_filler: bool = True, user_turn: bool = True,
                                  input_seq: Optional[int] = None,
                                  prefetched_summary_note: str = "") -> None:
        _sync_voiceturn_globals()
        try:
            generation_seq = self._latest_input_seq if input_seq is None else input_seq
            await self._generate_and_speak_impl(text, allow_tools=allow_tools, prefetched=prefetched,
                                                allow_filler=allow_filler, user_turn=user_turn,
                                                input_seq=generation_seq,
                                                prefetched_summary_note=prefetched_summary_note)
        finally:
            # vmem：本轮生成结束信号（正常/取消/异常全部到达；记忆插话仲裁
            # 以 "epoch > 钩子时点" 判本轮，超时封顶，等待永远不挂死）。
            self._vmem_gen_done_epoch = self._turn_epoch
            self._vmem_gen_done.set()
            # TTFT watch lifecycle (A4.9 review Important #1): a generation
            # that ends WITHOUT content (provider error / empty reply) must not
            # leave a watch that fires a filler into the silence or over the
            # user's next utterance. Generations are serialized per session, so
            # this cannot cancel a newer turn's watch; a watch that already
            # fired is done and unaffected.
            self._clear_pending_filler()
    async def _clarification_shadow(self, text: str) -> None:
        """B11（2026-09-14）：语音纠正候选 shadow 记录（applied=False，不应用；
        独立会话，失败静默）。

        2026-09-20 A4.9 r1 Important-3：必须遵守记忆运行时总开关
        （`memory_runtime_enabled`，与 chat 及本文件其他记忆路径一致）——
        运行时被禁用时不得再调用澄清 LLM。"""
        _sync_voiceturn_globals()
        try:
            from app.core.config import get_config
            from app.services.memory_runtime_state import memory_runtime_enabled
            if not memory_runtime_enabled(get_config()):
                return
            from app.db.database import AsyncSessionLocal
            from app.services.memory_clarification_service import process_clarification
            uid = getattr(self.user, "id", None)
            if not uid:
                return
            async with AsyncSessionLocal() as db:
                await process_clarification(db, uid, text, shadow=True)
        except Exception:
            logger.debug("voice clarification shadow failed", exc_info=True)
    async def _generate_and_speak_cancellable(self, text: str, prefetched: Optional[str] = None,
                                              input_seq: int = 0,
                                              prefetched_summary_note: str = "") -> None:
        """Generation entry-point used by the parallel subagent+main-agent
        pattern for short utterances. Identical to ``_generate_and_speak``
        but handles ``asyncio.CancelledError`` gracefully: when the noise-gate
        subagent cancels this task, partial history is rolled back and no
        incomplete message is persisted."""
        _sync_voiceturn_globals()
        history_before = len(self._history)
        try:
            generation_kwargs = {"allow_tools": True}
            if prefetched is not None:
                generation_kwargs["prefetched"] = prefetched
                generation_kwargs["prefetched_summary_note"] = prefetched_summary_note
            if input_seq:
                generation_kwargs["input_seq"] = input_seq
            await self._generate_and_speak(text, **generation_kwargs)
        except asyncio.CancelledError:
            # Noise gate cancelled this generation — roll back history and
            # notify the client so any partial display text is cleared. Also
            # cut any in-flight aux speech (filler prefix spoken at turn
            # start): the turn is abandoned, so "我来想想啊…" must not keep
            # playing into the void. `_interrupt` stays set until the next
            # _handle_user_turn clears it — the established interrupt pattern
            # (A4.9 I2).
            del self._history[history_before:]
            self._last_answer_text = ""
            self._vmem_answer_epoch = 0
            self._interrupt.set()
            self._drain_tts_queue()
            await self._mark_speaking_end()
            await self._send_json({"event": "generation_cancelled"})
            raise
    async def _check_interjection(self, full_text: str) -> None:
        """Extract the latest completed sentence and run the interjection
        subagent. Non-blocking: fires the check as a background task so ASR
        pipeline processing is not delayed."""
        _sync_voiceturn_globals()
        if self._interjection_checking:
            return
        if _now() - self._last_interjection_time < self.config.voice_interjection_cooldown_seconds:
            # Still update the offset so we don't reprocess old text later
            self._interjection_offset = len(full_text)
            return
        if self._interjection_count_this_turn >= self.config.voice_interjection_max_per_turn:
            self._interjection_offset = len(full_text)
            return
        # Extract new text since last interjection check
        new_text = _strip_lead_punct(full_text[self._interjection_offset:]).strip()
        self._interjection_offset = len(full_text)
        if not new_text or len(new_text) < 4:
            return
        # Split by terminal punctuation — take the last completed sentence
        sentences = [s.strip() for s in re.split(f"[{_TERMINAL_PUNCT}]", new_text) if s.strip()]
        if not sentences:
            return
        latest_sentence = sentences[-1]
        if len(latest_sentence) < 4:
            return
        self._user_speech_sentences.append(latest_sentence)
        self._interjection_checking = True
        asyncio.create_task(self._run_interjection_check(latest_sentence))
    async def _run_interjection_check(self, sentence: str) -> None:
        """Run the interjection subagent and speak the interjection if approved."""
        _sync_voiceturn_globals()
        self._interjection_classify_start = _now()
        try:
            result = await self._classify_interjection(
                sentence, list(self._user_speech_sentences)
            )
            if result["should_interject"] and not self._speaking and not self._closed:
                # Double-check conditions before speaking — the user might have
                # stopped speaking (EoT) while the subagent was running.
                if not self._turn_active and not self._interjecting:
                    await self._speak_interjection(
                        result["interjection_text"], result["emotion"]
                    )
                else:
                    # Update emotion even if we don't interject
                    self._emotion = result["emotion"]
            else:
                self._emotion = result["emotion"]
        except Exception as exc:
            logger.debug("voice interjection check error: %s", exc)
        finally:
            self._interjection_checking = False
