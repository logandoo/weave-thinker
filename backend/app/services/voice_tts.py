# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from app/services/voice_service.py (C-03/C-04, 2026-10-19).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (   'Optional',
    '_norm_barge_compare',
    '_now',
    '_perf',
    '_split_segments',
    'asyncio',
    'clean_for_tts',
    '_is_low_value_ack_text',
    'logger')


def _sync_voicetts_globals() -> None:
    from app.services import voice_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class VoiceTTSMixin:
    async def _mark_speaking_end(self) -> None:
        """Audio playback has fully drained on the client. Clears the speaking
        flag and notifies the client so the interrupt control hides. In 语音助理 mode
        this is driven by actual playback (playback_drained / safety deadline),
        not merely by "all bytes sent"."""
        _sync_voicetts_globals()
        if self._speaking:
            self._speaking = False
            self._speaking_deadline = 0.0
            await self._send_json({"event": "speaking_end"})
    def _reset_turn_playback(self) -> None:
        """Reset per-turn playback-progress tracking at the start of a turn."""
        _sync_voicetts_globals()
        self._turn_segments = []
        self._turn_audio_sec_total = 0.0
        self._current_seg_text = ""
        self._current_seg_audio_sec = 0.0
        self._playback_played_sec = 0.0
        self._playback_total_sec = 0.0
        self._burst_base_chars = 0
        self._burst_base_sec = 0.0
        self._burst_is_first = True
        self._speaking_deadline = 0.0
        self._spoken_text_recent = ""
        self._send_pace_start = _now()
    def _completed_chars_in_burst(self) -> int:
        """Chars of segments whose audio fully completed WITHIN the current
        burst (cum_sec > the burst's audio start). The pre-burst prefix is
        already counted by ``_burst_base_chars``."""
        _sync_voicetts_globals()
        n = 0
        for s in self._turn_segments:
            if s.get("cum_sec", 0) > self._burst_base_sec + 1e-9:
                n += len(s.get("text", ""))
        return n
    def _spoken_text_so_far(self, played_sec: float) -> str:
        """Map a playback position (seconds) to the answer text spoken so far,
        using the per-segment audio durations recorded as audio was sent.
        Includes a proportional prefix of the segment currently being played."""
        _sync_voicetts_globals()
        if not self._turn_segments:
            return ""
        out: list[str] = []
        prev_cum = 0.0
        for seg in self._turn_segments:
            cum = seg["cum_sec"]
            dur = seg["audio_sec"] or 0.0
            if played_sec >= cum - 0.05:
                out.append(seg["text"])          # fully played segment
            elif played_sec > prev_cum + 0.02 and dur > 0:
                frac = max(0.0, min(1.0, (played_sec - prev_cum) / dur))
                n = int(len(seg["text"]) * frac)
                if n > 0:
                    out.append(seg["text"][:n])  # partially played segment
                break
            else:
                break
            prev_cum = cum
        return "".join(out)
    def _notice_state(self, notification_id: str) -> dict:
        _sync_voicetts_globals()
        return self._notification_states.setdefault(notification_id, {
            "audio_sent": False,
            "generation_complete": False,
            "tts_failed": False,
            "delivered": False,
            "burst_seq": 0,
        })

    async def _synth_segment(self, text: str, epoch: int, out: asyncio.Queue,
                             ignore_paused: bool = False,
                             notification_id: Optional[str] = None) -> None:
        """Synthesize one TTS segment, pushing PCM chunks into ``out`` as they
        arrive (streaming). Terminated by the interrupt/pause/close gates; a
        None sentinel marks the segment's end. Runs as a background task so the
        NEXT segment's provider round-trip (~0.7-1.3s) overlaps the CURRENT
        segment's playback — without this, short spoken clauses (4-17 chars)
        each insert an audible silence gap between them.
        Hard-backpressured: the push blocks while the play queue holds
        ~10s of audio, so a stalled play loop can never buffer unboundedly
        (A4.9 review Important #2).
        ``ignore_paused``: the resume pre-synthesis (spawned at pause time)
        must keep producing while ``_playback_paused`` is set — overlapping
        the barge-in classifier. Normal synthesis keeps the pause gate."""
        _sync_voicetts_globals()
        # 2026-08-31 端点池化：风格指令由 tts_service 解析链决定（显式参数 >
        # 端点 extra.style_instruction > [voice] legacy 键），此处不再用
        # config 显式覆盖（否则端点配置的音色风格永远被配置键压掉）。
        style = None
        tts_text = text
        if self._style_tag and not text.lstrip().startswith(("(", "（")):
            tts_text = self._style_tag + text
        tts_text = clean_for_tts(tts_text)
        if not tts_text.strip():
            await out.put(None)
            return
        _tts_seg_start = _now()
        try:
            async for pcm in self.tts.stream_tts(tts_text, style_instruction=style):
                if self._interrupt.is_set() or self._closed or (self._playback_paused and not ignore_paused):
                    break
                if not self._first_chunk_logged:
                    self._first_chunk_logged = True
                    _perf("tts_first_chunk", (_now() - _tts_seg_start) * 1000,
                          seg_len=len(tts_text))
                # Hard backpressure: never let a single segment buffer more
                # than ~100 chunks (~10s of audio, ~480KB) — a stalled play
                # loop (slow client) cannot grow this unboundedly. The play
                # loop drains far faster than the provider produces, so this
                # never engages in normal playback (no latency impact).
                while out.qsize() >= 100:
                    await asyncio.sleep(0.01)
                await out.put(pcm)
        except Exception as exc:
            if notification_id:
                await self._fail_notification(notification_id)
            logger.error("voice TTS synth error: %s", exc)
        await out.put(None)
    def _peek_next_text_item(self) -> Optional[dict]:
        """Peek the TTS queue for the next TEXT item without consuming it.
        Returns None when the queue is empty (flush markers and the poison
        pill are skipped — prefetching past a flush marker is beneficial:
        the next burst's synthesis overlaps the current one).
        Read-only scan of the internal deque. A pull-then-reinsert "peek"
        (get_nowait + put_nowait) would REORDER the queue: put_nowait appends
        at the TAIL, so with >=2 items queued at peek time (the normal
        pipelined condition) the next segment is moved behind later ones and
        playback plays segments out of order — heard live (conv 4a336bf3):
        "技术方案，" played after "你说说看。" Safe single-threaded read: the
        consumer is the only getter and it is not blocked on get() while this
        runs, so _queue holds every queued item in FIFO order."""
        _sync_voicetts_globals()
        for it in self._tts_queue._queue:
            if isinstance(it, dict) and it.get("text"):
                return it
        return None
    def _spawn_synth(self, text: str, epoch: int, q: asyncio.Queue,
                     ignore_paused: bool = False,
                     notification_id: Optional[str] = None) -> asyncio.Task:
        """Spawn a pipelined TTS synthesis task, tracked in self._tasks (so
        _shutdown cancels it) and removed from the list when done (so the list
        does not grow over a long session)."""
        _sync_voicetts_globals()
        task = asyncio.create_task(
            self._synth_segment(
                text, epoch, q,
                ignore_paused=ignore_paused,
                notification_id=notification_id,
            )
        )
        self._tasks.append(task)

        def _done(t: asyncio.Task) -> None:
            try:
                self._tasks.remove(t)
            except ValueError:
                pass

        task.add_done_callback(_done)
        return task
    async def _fail_notification(self, notification_id: str) -> None:
        _sync_voicetts_globals()
        state = self._notice_state(notification_id)
        state["tts_failed"] = True
        self._queued_notice_ids.discard(notification_id)
        if self._active_notification_id == notification_id and not state.get("audio_sent"):
            # Zero-audio failure has no tail to drain, so release immediately.
            # A partial-audio failure instead keeps playback ownership until the
            # client's plain drain, preventing the next notice from overlapping.
            self._active_notification_id = None
            self._active_notification_audio_sent = False
            self._active_notification_generation_complete = False
            await self._mark_speaking_end()
        if not state.get("failure_notified"):
            state["failure_notified"] = True
            await self._send_json({
                "event": "notification_failed",
                "notification_id": notification_id,
            })

    async def _tts_consumer(self) -> None:
        _sync_voicetts_globals()
        # Pipelined TTS playback: while a segment plays, the NEXT segment's
        # synthesis is already running (started at dequeue time), so the
        # per-segment provider round-trip overlaps playback instead of
        # inserting audible silence between clauses. Interrupt/pause gates
        # apply at both the synthesizer (per chunk) and the player (per chunk).
        while not self._closed:
            item = await self._tts_queue.get()
            if item is None:
                await self._mark_speaking_end()
                return
            if "flush" in item:
                if (
                    item.get("notification_id")
                    and item.get("notification_id") == self._active_notification_id
                    and item.get("notification_complete") is True
                ):
                    self._notice_state(item["notification_id"])["generation_complete"] = True
                    self._active_notification_generation_complete = True
                fut = item["flush"]
                # Audio is fully SENT but the client may still be playing the
                # buffered tail. Do NOT clear _speaking here — keep barge-in
                # protection active for the whole audible playback. Instead set
                # a safety deadline; the client's playback_drained event (or
                # this deadline) clears _speaking via _mark_speaking_end().
                if self._speaking and self._turn_audio_sec_total > 0:
                    self._speaking_deadline = _now() + self._turn_audio_sec_total + 1.5
                if fut and not fut.done():
                    fut.set_result(True)
                continue
            text = item.get("text", "")
            epoch = int(item.get("epoch", self._turn_epoch) or 0)
            pre_q = item.get("pre_q")
            dedup = bool(item.get("dedup"))
            aux_item = bool(item.get("aux"))
            notification_id = item.get("notification_id")
            item_input_seq = int(item.get("input_seq", 0) or 0)
            if (
                item_input_seq
                and item_input_seq < self._latest_input_seq
                and _is_low_value_ack_text(text)
            ):
                logger.info("voice stale ack PCM dropped: input_seq=%d latest=%d", item_input_seq, self._latest_input_seq)
                continue
            if not text or self._interrupt.is_set() or self._playback_paused:
                self._tts_prefetch_q = None
                self._tts_prefetch_text = ""
                self._tts_prefetch_epoch = 0
                self._tts_prefetch_valid = True
                continue
            # Duplicate-segment suppression at PLAY time (A4.9 review C1):
            # enqueue-time recording let pause-drained segments (never heard)
            # poison the window, so a resume's re-enqueued delta was fully
            # suppressed and the answer tail silently vanished. Checking here
            # means only segments a peer COMPLETED playing can suppress a new
            # one; dropped (drained/gated) segments never enter the window.
            if dedup and pre_q is None and self._dup_window.check(text, epoch):
                self._tts_prefetch_q = None
                self._tts_prefetch_text = ""
                self._tts_prefetch_epoch = 0
                self._tts_prefetch_valid = True
                logger.info("voice: suppressed duplicate TTS segment %r", (text or "")[:30])
                continue
            # Use the prefetch queue only when it is VALID (never invalidated
            # by a drain — a gate-truncated prefetch must not be replayed) AND
            # matches this segment's text+epoch; otherwise synthesize fresh.
            # A resume item carries its own pre-synthesized queue (pre_q),
            # which the pause already filled — play it directly.
            if pre_q is not None:
                q = pre_q
            elif (
                self._tts_prefetch_q is not None
                and self._tts_prefetch_valid
                and self._tts_prefetch_text == text
                and self._tts_prefetch_epoch == epoch
            ):
                q = self._tts_prefetch_q
            else:
                q = asyncio.Queue()
                self._tts_prefetch_valid = True
                self._spawn_synth(text, epoch, q, notification_id=notification_id)
            # Start the NEXT segment's synthesis NOW (overlaps this playback).
            nxt = self._peek_next_text_item()
            if nxt is not None:
                self._tts_prefetch_q = asyncio.Queue()
                self._tts_prefetch_text = nxt["text"]
                self._tts_prefetch_epoch = int(nxt.get("epoch", self._turn_epoch) or 0)
                self._tts_prefetch_valid = True
                self._spawn_synth(
                    self._tts_prefetch_text,
                    self._tts_prefetch_epoch,
                    self._tts_prefetch_q,
                    notification_id=nxt.get("notification_id"),
                )
            else:
                self._tts_prefetch_q = None
                self._tts_prefetch_text = ""
                self._tts_prefetch_epoch = 0
            # Play the current segment (streaming from the synth task).
            # Per-item epoch: each queued segment carries the turn that
            # ENQUEUED it — re-tags the onset-pause hook for a new answer's
            # audio streaming into a still-draining previous burst.
            self._speaking_epoch = epoch
            _perf("tts_play", (_now() - self._turn_enqueued_at) * 1000,
                  seg_len=len(text), epoch=epoch)
            # Reset playback attribution only at a REAL turn boundary — the
            # generation epoch changes per turn, but _speaking does not (the
            # watchdog deadline, playback_drained, or a pause can clear it
            # mid-turn). Gating on _speaking wiped _turn_segments mid-answer
            # (observed live conv 964462a8: stale flush deadline fired during
            # round-2 playback → every later pause estimated from a 1-segment
            # pool → wrong breakpoints → audible replays). The list-empty
            # condition is deliberately NOT part of the gate: a pause→resume
            # can leave _turn_segments empty (pause in the first segment),
            # and resetting on that would clobber the burst anchor the resume
            # just set — a later pause would then mis-estimate against the
            # resumed segment (skips/replays — the failure family).
            if self._turn_segments and self._turn_segments[-1].get("epoch") != epoch:
                self._reset_turn_playback()
            # Track this segment for playback-progress attribution + echo detect.
            self._current_seg_text = text
            self._current_seg_audio_sec = 0.0
            self._spoken_text_recent = (self._spoken_text_recent + text)[-200:]
            self._consumer_playing = True
            first_wait = True
            truncated = False
            aux_prefetch_spawned = False
            aux_prefetch_task = None
            self._playing_aux = aux_item
            previous_notification_id = self._active_notification_id
            if notification_id:
                notice_state = self._notice_state(notification_id)
                self._active_notification_id = notification_id
                self._active_notification_audio_sent = notice_state["audio_sent"]
                self._active_notification_generation_complete = notice_state["generation_complete"]
            else:
                # Ordinary speech is not notification playback. Clear only the
                # active slot; per-notification state remains independently intact.
                self._active_notification_id = None
                self._active_notification_audio_sent = False
                self._active_notification_generation_complete = False
            while True:
                if first_wait:
                    # Safety net: a hung TTS provider stream must not stall
                    # the consumer (and the whole session) forever. Only the
                    # FIRST chunk gets a deadline — steady-state chunks are
                    # continuous and never waited on.
                    first_wait = False
                    try:
                        pcm = await asyncio.wait_for(q.get(), timeout=60.0)
                    except asyncio.TimeoutError:
                        if notification_id:
                            await self._fail_notification(notification_id)
                        logger.warning(
                            "voice TTS first chunk timed out (60s) — dropping segment %r",
                            (text or "")[:30],
                        )
                        break
                else:
                    pcm = await q.get()
                if pcm is None:
                    if self._interrupt.is_set() or self._closed or self._playback_paused:
                        truncated = True
                    break
                if self._interrupt.is_set() or self._closed or self._playback_paused:
                    truncated = True
                    break
                # Auxiliary speech (filler/backchannel) is cut as soon as a real
                # answer segment's audio is ready: spawn its synthesis the
                # moment it appears in the queue (during aux playback) and
                # switch over at the first chunk. Everything stays inside the
                # single consumer — no concurrent TTS, no overlap, no stale
                # prefetch (the existing text+epoch guard + drain invalidation
                # apply). A backchannel never reaches this point in practice
                # (the answer starts >=0.5s after it ends), but the rule is
                # uniform and safe for any aux item.
                if aux_item and not aux_prefetch_spawned:
                    nxt = self._peek_next_text_item()
                    if nxt is not None and not nxt.get("aux"):
                        nxt_epoch = int(nxt.get("epoch", self._turn_epoch) or 0)
                        if (
                            self._tts_prefetch_q is not None
                            and self._tts_prefetch_valid
                            and self._tts_prefetch_text == nxt["text"]
                            and self._tts_prefetch_epoch == nxt_epoch
                        ):
                            # The dequeue-time prefetch already covers this
                            # item — never spawn a duplicate synthesis (A4.9 M1).
                            aux_prefetch_spawned = True
                        else:
                            self._tts_prefetch_q = asyncio.Queue()
                            self._tts_prefetch_text = nxt["text"]
                            self._tts_prefetch_epoch = nxt_epoch
                            self._tts_prefetch_valid = True
                            aux_prefetch_task = self._spawn_synth(
                                self._tts_prefetch_text,
                                self._tts_prefetch_epoch,
                                self._tts_prefetch_q,
                                notification_id=nxt.get("notification_id"),
                            )
                            aux_prefetch_spawned = True
                if aux_item and aux_prefetch_spawned and self._tts_prefetch_q is not None:
                    if aux_prefetch_task is not None and aux_prefetch_task.done() and self._tts_prefetch_q.empty():
                        # Answer synthesis finished without audio — nothing to
                        # switch to; drop the aux too (empty answer tail).
                        truncated = True
                        break
                    try:
                        nxt_pcm = self._tts_prefetch_q.get_nowait()
                    except asyncio.QueueEmpty:
                        nxt_pcm = None
                    if nxt_pcm is not None:
                        # First answer chunk ready — cut the aux at this chunk
                        # boundary and put the chunk BACK (the consumer is the
                        # only getter, so no reordering): the answer item plays
                        # it from the prefetch. Without the put-back the first
                        # answer chunk (incl. the leading syllable onset) would
                        # be silently dropped (A4.9 I1).
                        self._tts_prefetch_q.put_nowait(nxt_pcm)
                        truncated = True
                        break
                was_speaking = self._speaking
                if not self._speaking:
                    self._speaking = True
                    self._speaking_started_at = _now()
                if (not was_speaking) or (notification_id != previous_notification_id):
                    await self._send_json({
                        "event": "speaking_start",
                        "speech_kind": "notice" if notification_id else ("aux" if aux_item else "answer"),
                        "notification_id": notification_id,
                        "notice_burst_seq": notice_state.get("burst_seq", 0) if notification_id else 0,
                    })
                sec = len(pcm) / 48000.0  # PCM16, 24kHz, mono
                self._current_seg_audio_sec += sec
                self._turn_audio_sec_total += sec
                await self._send_bytes(pcm)
                if notification_id and pcm:
                    self._notice_state(notification_id)["audio_sent"] = True
                    self._active_notification_audio_sent = True
                # Anchor the send-pacing clock to the start of the audio flow
                # (A4.9 r4 Minor: the per-turn anchor at generation start left
                # the first ~TTFT seconds unthrottled). Once the pace engages
                # (>0.5s of audio), the anchor must stay fixed for the turn.
                if self._turn_audio_sec_total <= 0.5:
                    self._send_pace_start = _now()
                # Pace the send to real-time. The pipelined synth produces
                # 2-4x faster than real-time; pushing the whole backlog
                # immediately fills the TCP/TLS buffers (~2MB ≈ 40s of
                # audio), after which the websocket's flow control pauses the
                # transport and the send blocks forever — the answer's tail
                # is silently swallowed (observed: every client — python,
                # bare browser, the real frontend — stalled at ~40-45s of a
                # 60-90s answer). Throttling the send to ~1x keeps the
                # client-side buffer small (the client plays at 1x anyway),
                # so the buffers never fill and the stall cannot occur.
                # The FIRST chunk is unaffected (sent immediately — TTFA
                # unchanged) and the synth backpressure cap still applies.
                if self._turn_audio_sec_total > 0.5:
                    pace_start = getattr(self, "_send_pace_start", 0.0) or _now()
                    drift = self._turn_audio_sec_total - (_now() - pace_start)
                    if drift > 0.05:
                        await asyncio.sleep(drift)
            if notification_id and not truncated and self._current_seg_audio_sec <= 0:
                await self._fail_notification(notification_id)
            # Segment finished — record it for progress attribution. A
            # gate-truncated segment (pause/interrupt cut it mid-audio) is NOT
            # recorded: its partial audio_sec would skew the chars/sec rate and
            # its text would be counted as fully played — the pause breakpoint
            # math covers the in-flight segment via played_sec * rate instead.
            # Aux items (filler/backchannel) are NEVER recorded: they are not
            # answer content and must not skew breakpoint estimates or become
            # resumable after a pause.
            if self._current_seg_audio_sec > 0 and not truncated and not aux_item:
                self._turn_segments.append({
                    "text": text,
                    "audio_sec": self._current_seg_audio_sec,
                    "cum_sec": self._turn_audio_sec_total,
                    "epoch": epoch,
                })
                # Record into the dup window only when the segment FULLY
                # played (same guard as the breakpoint pool above): an
                # interrupted or gated tail was never heard, so a resume
                # replay of it must not be suppressed.
                if dedup:
                    self._dup_window.record(text, epoch)
            self._consumer_playing = False
            self._playing_aux = False
    async def _speak_text(self, text: str, wait: bool = True, aux: bool = False,
                          notification_id: Optional[str] = None) -> None:
        """Speak an arbitrary string (notices/suggestions) via the TTS pipeline.

        ``aux=True`` marks filler-prefix / backchannel utterances: the consumer
        excludes them from the pause-breakpoint pool and the dup window, and
        cuts them the moment a real answer segment's audio is ready. The
        spoken phrases are also recorded in ``_aux_echo_guard`` so their own
        mic-echo is stripped from the ASR pending text."""
        _sync_voicetts_globals()
        segments, rest = _split_segments(text)
        if rest.strip():
            segments.append(rest)
        loop = asyncio.get_running_loop()
        for seg in segments:
            if seg.strip():
                await self._tts_queue.put({
                    "text": seg, "epoch": self._turn_epoch, "aux": aux,
                    "notification_id": notification_id or self._current_notice_id,
                    "input_seq": self._generation_input_seq,
                })
        if aux:
            self._register_aux_echo(text)
        if wait:
            fut = loop.create_future()
            await self._tts_queue.put({
                "flush": fut,
                "notification_id": notification_id or self._current_notice_id,
            })
            try:
                await asyncio.wait_for(fut, timeout=60.0)
            except Exception:
                pass
    def _drain_tts_queue(self) -> None:
        _sync_voicetts_globals()
        # Invalidate the pipeline prefetch: an interrupt/pause has gate-
        # truncated the in-flight prefetch synthesis, so its partial queue must
        # NEVER be replayed against a later item with the same text.
        self._tts_prefetch_q = None
        self._tts_prefetch_text = ""
        self._tts_prefetch_epoch = 0
        self._tts_prefetch_valid = False
        # The speculative filler prefetch dies with the drain too: the
        # pipeline is being reset (interrupt/pause), so its partial audio
        # must not convert at a later flush (2026-08-25).
        self._drop_filler_prefetch()
        # The pause's resume pre-synthesis is dropped too: a drain means the
        # answer is being abandoned/restarted, so its partial audio must not
        # be replayed by a later resume. (The synth task itself exits via the
        # interrupt gate and is tracked in self._tasks for _shutdown.)
        self._resume_q = None
        self._resume_epoch = 0
        self._resume_text = ""
        self._pre_classify = None
        while not self._tts_queue.empty():
            try:
                item = self._tts_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, dict) and "flush" in item:
                fut = item["flush"]
                if fut and not fut.done():
                    fut.set_result(False)
    def _enqueue_turn(self, text: str, semantic_complete: bool = False) -> None:
        """Queue a turn for the responder. Duplicate turns (same normalized
        text already pending — ASR re-emits finalized sentences, the EoT
        watchdog can re-flush the same text after a defer) are dropped so a
        sentence never gets answered twice or leaves a ghost message (conv
        689f06ec 13:59: "嗯，没啥，我再跟你聊聊。" enqueued twice).

        ``semantic_complete`` carries the semantic-EoT verdict for this flush:
        the responder skips the fragment-merge wait for it (the judge already
        ruled the text complete — no need to wait for a possible continuation
        fragment; 2026-09-20 latency fix)."""
        _sync_voicetts_globals()
        # 部署排空扩面（D-12，A4.9 二波 B-I2）：drain 中新轮次停领——已有会话
        # 不再发起新 LLM run（丢轮+日志，排空窗≤WT_DRAIN_TIMEOUT 秒）；在途轮
        # 照常完成并计入排空计数。notice 通道是后台任务专用（_deliver_bg_task_
        # notice 形状），不复用。
        from app.services.active_agent_registry import ActiveAgentRegistry
        if ActiveAgentRegistry.get_instance().is_draining:
            logger.info("voice enqueue dropped (server draining): %r", text)
            return
        self._turn_enqueued_at = _now()
        norm = _norm_barge_compare(text)
        if norm:
            for item in self._turn_queue._queue:
                if (
                    isinstance(item, dict)
                    and not item.get("notice")
                    and _norm_barge_compare(item.get("text", "")) == norm
                ):
                    logger.info("voice enqueue skipped (duplicate queued turn): %r", text)
                    return
        self._latest_input_seq += 1
        self._turn_queue.put_nowait({
            "text": text,
            "semantic_complete": bool(semantic_complete),
            "input_seq": self._latest_input_seq,
        })
    async def _queued_turn_is_backchannel(self, text: str) -> bool:
        """Decide whether a queued turn must be skipped (not answered) because
        the utterance was already judged backchannel.

        Paths covered (conv 689f06ec 13:59 incident): the utterance was
        deferred inside the window/cooldown and ENQUEUED without a verdict;
        the onset pre-classify (launched later, on the re-emitted partial)
        then returned backchannel 0.6s AFTER the responder had already
        persisted the user message. Here the responder (1) awaits a matching
        IN-FLIGHT pre-classify verdict (bounded — fail-open on timeout/error)
        and (2) consults the recent-backchannel registry recorded by
        _run_pre_classify and the flush-time backchannel branches. Anything
        else → False (the turn is processed normally)."""
        _sync_voicetts_globals()
        norm = _norm_barge_compare(text)
        if not norm:
            return False
        pre = self._pre_classify
        if pre and pre.get("text") and _norm_barge_compare(pre.get("text", "")) == norm:
            task = pre.get("task")
            if task and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except Exception:
                    pass  # fail-open: no verdict in time → process normally
            # Read the SNAPSHOT's verdict (A4.9 I2): the flush path may have
            # consumed or replaced self._pre_classify during the await; the
            # snapshot is the verdict this turn was matched against.
            if pre.get("action") == "backchannel":
                logger.info("voice queued turn skipped (in-flight pre-classify backchannel): %r", text)
                return True
        ts = self._recent_backchannel.get(norm)
        if ts is not None:
            if _now() - ts < self.config.voice_backchannel_recall_seconds:
                logger.info("voice queued turn skipped (recent backchannel verdict): %r", text)
                return True
            # Lazy cleanup (A4.9 M1): a stale entry is useless — drop it.
            self._recent_backchannel.pop(norm, None)
        return False
