# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from app/services/voice_service.py (baseline 883ac883, C-04, 2026-10-19).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (   '_ASR_NOISE_RE',
    '_MIC_ENERGY_ACTIVITY_FRESH_SECONDS',
    '_MIC_ENERGY_FLOOR_MAX',
    '_MIC_ENERGY_FLOOR_MIN',
    '_MIC_ENERGY_NEAR_MIN',
    '_MIC_ENERGY_NEAR_RATIO',
    '_is_likely_echo',
    '_now',
    '_perf',
    '_prox_is_near_signal',
    '_strip_aux_echo',
    '_strip_lead_punct',
    '_utterance_complete',
    'asyncio',
    'logger')


def _sync_voiceasrsession_globals() -> None:
    from app.services import voice_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class VoiceASRSessionMixin:
    def _prox_is_near(self) -> bool:
        """Session view of the acoustic near-field gate: True when the client
        reports the current mic input as near-field (user close to the phone).
        Unknown clients default to near (old behavior preserved); the config
        knob disables the gate entirely (always near)."""
        _sync_voiceasrsession_globals()
        if not self.config.voice_barge_in_proximity_gate:
            return True
        return _prox_is_near_signal(
            self._prox_seen,
            self._prox_near,
            self._prox_updated,
            self.config.voice_barge_in_proximity_stale_seconds,
            _now(),
        )
    def _prox_evidence_near(self) -> bool:
        """Proximity evidence for the barge-in classifier: prefer the
        utterance-time snapshot (refreshed on each ASR partial), falling back
        to the live gate. The live signal expires during EoT flush (1-3s of
        silence after the last partial), so using it directly would inject a
        stale far-field verdict for a real near-field interrupt (A4.9 C1)."""
        _sync_voiceasrsession_globals()
        if self._prox_utterance is not None:
            return self._prox_utterance
        return self._prox_is_near()
    def _track_mic_energy(self, pcm16: bytes) -> None:
        """Track local mic activity from the incoming PCM (ASR gap bridge).

        FunASR's result stream can stall for seconds at long-sentence
        boundaries while the user keeps talking; the raw mic audio is the only
        activity signal then. Adaptive noise floor (falls fast, rises slowly)
        plus a near-field ratio gate, mirroring the browser-side classifier.
        """
        _sync_voiceasrsession_globals()
        try:
            import numpy as np

            samples = np.frombuffer(pcm16, dtype=np.int16)
            if samples.size == 0:
                return
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        except Exception:
            return
        if rms < self._mic_energy_floor:
            self._mic_energy_floor = max(
                _MIC_ENERGY_FLOOR_MIN, rms * 0.9 + self._mic_energy_floor * 0.1
            )
        else:
            self._mic_energy_floor = min(
                _MIC_ENERGY_FLOOR_MAX,
                self._mic_energy_floor + self._mic_energy_floor * 0.002 + 1e-5,
            )
        near_thr = max(
            self._mic_energy_floor * _MIC_ENERGY_NEAR_RATIO, _MIC_ENERGY_NEAR_MIN
        )
        if rms >= near_thr:
            self._last_mic_activity = _now()
    def _hold_flush_for_mic_activity(self) -> bool:
        """True when a would-be EoT flush must be postponed because the local
        mic still carries near-field speech while ASR results are stalled.

        Bounded by ``voice_eot_activity_extension_seconds`` from the moment
        the flush first became due; released as soon as local speech stops or
        the cap elapses. Applies in every playback state — an onset pause or
        an aux (backchannel/filler) must not fragment a live user monologue;
        the mic-energy + proximity gates below are the only switches. Pure
        state machine — unit-tested."""
        _sync_voiceasrsession_globals()
        if not self.config.voice_eot_activity_bridge:
            self._flush_hold_since = 0.0
            return False
        now = _now()
        # A hold anchor belongs to ONE continuous flush-due window: any new
        # recognizer result/text supersedes the earlier would-be flush, so a
        # fresh bounded window starts (without this, a hold from a previous
        # gap kept its old anchor and the extension cap released the next hold
        # immediately — observed 2026-09-20 E2E).
        if (
            self._flush_hold_since > 0
            and max(self._last_text_change, self._last_asr_activity)
            > self._flush_hold_since
        ):
            # A new recognizer result superseded the pending would-be flush —
            # a fresh bounded hold window starts, including its per-decision
            # budget (otherwise an old anchor makes the cap fire instantly on
            # the next decision; observed live 2026-09-20 E2E).
            self._flush_hold_since = 0.0
            self._flush_hold_turn_started = 0.0
        # Per-pending-turn total cap (A4.9 review Minor-5): window resets must
        # not let repeated ASR stall/resume cycles postpone the flush across
        # many windows — the turn as a whole is bounded.
        if self._flush_hold_turn_started <= 0:
            self._flush_hold_turn_started = now
        if now - self._flush_hold_turn_started >= self.config.voice_eot_activity_extension_seconds:
            self._flush_hold_since = 0.0
            self._flush_hold_turn_started = 0.0
            return False
        if not self._prox_is_near():
            self._flush_hold_since = 0.0
            return False
        if now - self._last_mic_activity > _MIC_ENERGY_ACTIVITY_FRESH_SECONDS:
            self._flush_hold_since = 0.0
            return False
        if self._flush_hold_since <= 0:
            self._flush_hold_since = now
        if now - self._flush_hold_log_at >= 1.0:
            self._flush_hold_log_at = now
            logger.info(
                "voice EoT flush held (local mic activity, ASR gap bridge, %.1fs total)",
                now - self._flush_hold_turn_started,
            )
        return True
    async def _run_eot_semantic_check(self, text: str) -> None:
        """Fire-and-forget wrapper for the semantic EoT judge. Bounded by
        voice_eot_semantic_timeout_seconds (covers the _llm_gate wait + call);
        on timeout/error the verdict stays "not complete" and the watchdog's
        hard silence threshold flushes as before (fail-open)."""
        _sync_voiceasrsession_globals()
        try:
            result = await asyncio.wait_for(
                self._classify_eot(text, history=list(self._history)),
                timeout=self.config.voice_eot_semantic_timeout_seconds,
            )
            if result.get("complete"):
                self._eot_semantic_complete = True
            logger.info(
                "voice semantic EoT result: complete=%s reason=%r text=%r",
                result.get("complete"), result.get("reason", ""), text[-60:],
            )
        except asyncio.TimeoutError:
            logger.debug("voice semantic EoT check timed out (fail-open wait)")
        except Exception as exc:
            logger.debug("voice semantic EoT check failed (fail-open wait): %s", exc)
        finally:
            self._eot_semantic_checking = False
    async def _asr_pipeline(self) -> None:
        _sync_voiceasrsession_globals()
        while not self._closed:
            ev = await self._asr_events.get()
            etype = ev.get("type")
            if etype == "ready":
                self._asr_ready = True
                self._last_asr_activity = _now()
                await self._set_state("listen")
            elif etype == "reconnecting":
                # Upstream dropped and is reconnecting — freeze endpointing so
                # the watchdog can't flush a mid-utterance turn on stale text.
                self._asr_ready = False
            elif etype == "activity":
                self._last_asr_activity = _now()
            elif etype in ("partial", "segment"):
                self._last_asr_activity = _now()
                full = ev.get("text", "")
                self._latest_full = full
                # Refresh the utterance-time proximity snapshot (A4.9 C1): the
                # evidence for the classifier must reflect the acoustic
                # condition while this utterance was heard, not the (possibly
                # stale-by-flush-time) live signal.
                self._prox_utterance = self._prox_is_near()
                # The accumulated ASR text is retroactively finalised (sentence
                # punctuation appended at sentence_end), so the naive offset
                # slice can begin with the previous sentence's terminator.
                # Strip leading punctuation — a real turn never starts with it.
                turn_text = _strip_lead_punct(full[self._consumed_offset:])
                # Aux-speech echo guard: the agent's own filler/backchannel
                # phrases heard back by the mic land at the TAIL of the
                # accumulation (e.g. "帮我查一下天气嗯"). Strip them from the
                # pending text so they never pollute the EoT judgment or
                # become a phantom user turn. Only strips a suffix of a
                # LONGER text — a standalone "嗯" is a real confirmation.
                if self._aux_echo_guard:
                    stripped = _strip_aux_echo(turn_text, self._aux_echo_guard)
                    if stripped != turn_text:
                        logger.debug("voice aux-echo stripped: %r -> %r", turn_text, stripped)
                        turn_text = stripped
                # Acoustic onset barge-in: the user started speaking during
                # playback — pause TTS IMMEDIATELY on the first partial (the
                # earliest text signal upstream provides), instead of waiting
                # for EoT + LLM classification (~2s). The barge-in classifier
                # (at EoT flush) then decides resume-from-breakpoint or switch.
                # Pre-filters mirror the barge-in path: echo of our own voice,
                # ASR-noise text, and the no-interrupt window / cooldown never
                # pause — they defer to the normal flush-time path.
                if (
                    self._speaking
                    and not self._playback_paused
                    and not self._interjecting
                    and (self._speaking_epoch == self._turn_epoch or self._playing_aux)
                    and self.config.voice_barge_in_onset_enabled
                    and self.config.voice_barge_in_enabled
                    and turn_text.strip()
                    and not _ASR_NOISE_RE.match(turn_text)
                    and not _is_likely_echo(turn_text, self._spoken_text_recent)
                    and self._prox_is_near()
                ):
                    # Do NOT pause on VERY SHORT utterances (≤2 chars, e.g.
                    # "对"/"嗯是"/"好"): these are overwhelmingly backchannels
                    # or mic false positives (the TTS echo or environment
                    # audio misrecognized as a syllable — the echo gate only
                    # catches transcripts that MATCH the spoken text, so a
                    # misrecognition slips through; observed live: the user
                    # reported "绝对没有插话" while the ASR produced "对" and
                    # paused the answer). Let them flow through the normal
                    # EoT+classify path instead: a backchannel never pauses
                    # the playback at all, and a genuinely growing utterance
                    # pauses on its next, longer partial. Real interrupts are
                    # almost always ≥3 chars and still pause immediately.
                    if len(_strip_lead_punct(turn_text).strip()) < self.config.voice_barge_in_onset_min_chars:
                        logger.debug(
                            "voice onset skipped (short utterance %r) — deferring to EoT+classify",
                            turn_text,
                        )
                    else:
                        _now_p = _now()
                        if not (
                            _now_p - self._speaking_started_at
                            < self.config.voice_barge_in_no_interrupt_seconds
                            or _now_p - self._last_barge_in_time
                            < self.config.voice_barge_in_cooldown_seconds
                        ):
                            await self._pause_playback()
                            # Launch the barge-in classify in PARALLEL with the EoT
                            # watchdog, on the CURRENT partial. Serial EoT (probe
                            # 1.0s + judge 0.8s + flush) THEN classify (~1-1.5s)
                            # left 4-5s of dead silence after the pause — users
                            # gave up and refreshed, permanently truncating the
                            # answer (observed 15:33: pause 36.8s, EoT result
                            # 40.1s, connection closed before the resume). The
                            # flush reuses this verdict when the final text
                            # matches the classified partial (see
                            # _on_user_turn_locked), so a short reaction
                            # ("对"/"嗯") resumes as soon as the EoT flushes.
                            self._pre_classify = {
                                "text": turn_text,
                                "action": None,
                                "task": asyncio.create_task(
                                    self._run_pre_classify(turn_text)
                                ),
                            }
                if turn_text != self._pending_turn_text:
                    self._pending_turn_text = turn_text
                    self._last_text_change = _now()
                    self._complete_since = (
                        _now() if _utterance_complete(turn_text) else 0.0
                    )
                    await self._send_json({"event": "asr_partial", "text": turn_text})
                    if self._speaking and turn_text.strip():
                        await self._set_state("dual")
                # Interjection check: when a sentence completes (segment event)
                # and the agent is NOT speaking, run the interjection subagent
                # to decide whether to make a brief remark. This implements the
                # "插话" mechanism — the agent reacts while the user is still
                # speaking, without waiting for the full turn to finish.
                if (
                    etype == "segment"
                    and not self._speaking
                    and not self._playback_paused
                    and not self._interjecting
                    and not self._turn_active
                    and self.config.voice_interjection_enabled
                ):
                    await self._check_interjection(full)
            elif etype == "error":
                await self._send_json({"event": "error", "error": ev.get("error", "语音识别错误")})
    async def _eot_watchdog(self) -> None:
        _sync_voiceasrsession_globals()
        while not self._closed:
            await asyncio.sleep(0.1)
            # Safety fallback: if the client's playback_drained never arrives
            # (raw/headless client, network drop), clear _speaking once the
            # estimated playback window elapses so barge-in state can't stick.
            # Never while the consumer is inside a play loop OR the turn is
            # still generating: the deadline is only refreshed on flush items
            # (the _speak_text path), so a stale deadline would otherwise
            # clear _speaking mid-burst (a provider stall >2s between chunks
            # included) and the resulting mid-turn speaking_end/start pair
            # restarts the client's burst clock — under-counting the next
            # pause breakpoint (observed live conv 964462a8: the
            # audible-replay failure family).
            if (
                self._speaking
                and self._speaking_deadline
                and not self._consumer_playing
                and not self._turn_active
                and _now() > self._speaking_deadline
            ):
                await self._mark_speaking_end()
            text = self._pending_turn_text
            if not text.strip():
                continue
            # Never endpoint while the recognizer is down/reconnecting — frozen
            # text would otherwise look like "user stopped speaking" and flush
            # a mid-utterance turn.
            if not self._asr_ready:
                continue
            # An in-flight interjection check does NOT block the flush: the
            # check is a best-effort "插话" for a sentence the user may or may
            # not be continuing. Waiting for it (a non-streaming LLM call,
            # 1-2s typical, up to the subagent timeout) puts it on the
            # critical path to the user's answer. The check itself already
            # double-guards before speaking (not _turn_active / not
            # _interjecting), so a flush that wins the race simply skips the
            # interjection — the full answer starts immediately.
            # Endpoint idleness is measured from the last RECOGNIZER activity
            # (any upstream result), not the last text change. FunASR's partial
            # cadence during continuous speech is ~0.3-1.1s and can briefly
            # repeat identical text, so text-change time alone flushes turns
            # while the user is still talking.
            silence = _now() - max(self._last_text_change, self._last_asr_activity)
            # Backchannel (应和): a short listening ack during a mid-utterance
            # pause window — before the semantic-EoT probe can arm, so the
            # turn-taking pipeline is never racing an in-flight aux phrase.
            await self._maybe_backchannel(silence, text)
            # Filler prefetch (填充词预取): the utterance MAY end at the
            # earliest flush threshold — start the filler's TTS synthesis
            # even earlier (half of min(semantic_probe, complete_grace),
            # floored at 0.2s) so its first audio chunk is ready BEFORE the
            # flush: the filler overlaps the flush+generation pipeline and
            # starts playing right after ASR end (measured E2E: filler PCM
            # ~0.5s after the filler event, 10s+ before the answer finishes).
            # ARM ONCE PER LIVE FILLER — a mid-utterance pause never re-arms:
            # the phrase is content-independent (picked from the previous
            # PHRASE, not the user text), and a re-arm on every FunASR partial
            # (0.3-1.1s cadence) would burn one canceled provider synthesis
            # per partial of a long monologue (2026-08-25 review I1). The
            # synthesis simply stays queued; a convert whose text no longer
            # matches is re-gated (noise/stop-word) on the FLUSHED text.
            arm_threshold = max(
                0.2,
                0.5
                * min(
                    self.config.voice_eot_semantic_probe_seconds,
                    self.config.voice_eot_complete_grace_seconds,
                ),
            )
            if silence >= arm_threshold and self._filler_prefetch.get("q") is None:
                self._arm_filler_prefetch(text)
            # Adaptive endpointing. A complete-looking utterance (ends with
            # sentence-final punctuation) flushes after a short grace of no ASR
            # activity — when the user continues into the next sentence FunASR
            # emits its first partial within ~0.5-1.3s of the sentence_end, so
            # the grace distinguishes a real stop from a sentence boundary.
            # An incomplete utterance (mid-sentence pause, no terminal
            # punctuation) waits much longer so a long question spoken with
            # natural pauses is not chopped into fragments.
            # NOTE: the same thresholds apply while the agent is speaking.
            # A previous special-case (0.6s during playback) truncated barge-in
            # speech mid-utterance — FunASR's partial cadence (0.5-1.3s) beat
            # the threshold whenever the user talked over the TTS answer,
            # shattering one utterance into a cascade of fragment turns.
            # Barge-in responsiveness comes from the downstream classifier,
            # not from aggressive endpointing.
            # Whether this flush was approved by the semantic-EoT judge as a
            # complete turn — carried into the turn queue so the responder can
            # skip the fragment-merge wait for it (2026-09-20 latency fix).
            semantic_complete_flush = False
            if _utterance_complete(text):
                # Flush when the recognizer has been idle for the grace period
                # — OR when the text has sat complete for the hard cap even
                # though ASR activity continues (noisy environment: background
                # speech must not postpone the answer indefinitely). The cap
                # timer resets the moment new text arrives, so a user who
                # genuinely keeps talking is never truncated by it.
                idle_flush = silence >= self.config.voice_eot_complete_grace_seconds
                capped_flush = (
                    self._complete_since
                    and (_now() - self._complete_since)
                    >= self.config.voice_eot_complete_max_seconds
                )
                if not (idle_flush or capped_flush):
                    continue
            else:
                # Semantic EoT: unpunctuated text is ambiguous — the user may
                # have finished ("好的就这样" often carries no period) or be
                # mid-thought ("我想说…"). Once silence passes the probe
                # threshold, an LLM judge (fire-and-forget, bounded) decides
                # semantic completeness: "complete" flushes EARLY (faster than
                # the hard threshold); "incomplete" or a failed/timeout check
                # waits for the hard threshold below (fail-open — never more
                # dead time than the pre-semantic behavior). A text change
                # while the check is in flight invalidates its verdict.
                # While playback is PAUSED (onset barge-in) the probe is
                # shortened: the user's reaction is the only speech, so the
                # endpoint can be faster without fragment risk — the flush is
                # still gated by the semantic judge's verdict.
                probe_sec = self.config.voice_eot_semantic_probe_seconds
                if self._playback_paused:
                    probe_sec = min(probe_sec, self.config.voice_eot_paused_probe_seconds)
                if (
                    self.config.voice_eot_semantic_enabled
                    and not self._eot_semantic_checking
                    and silence >= probe_sec
                    and self._eot_semantic_checked_text != text.strip()
                ):
                    self._eot_semantic_checking = True
                    self._eot_semantic_complete = False
                    self._eot_semantic_checked_text = text.strip()
                    asyncio.create_task(self._run_eot_semantic_check(text))
                    logger.info("voice semantic EoT probe fired: %r", text[-60:])
                semantic_ok = (
                    self._eot_semantic_complete
                    and text.strip() == self._eot_semantic_checked_text
                )
                if not semantic_ok:
                    if silence < self.config.voice_eot_silence_incomplete_seconds:
                        continue
                elif silence < probe_sec:
                    # The user resumed talking (identical re-emitted text reset
                    # the silence clock) while the judge was in flight — do NOT
                    # flush mid-speech; wait for the hard threshold (I4).
                    continue
                if semantic_ok:
                    semantic_complete_flush = True
                    _perf("eot_semantic_flush",
                          (_now() - max(self._last_text_change, self._last_asr_activity)) * 1000,
                          text_len=len(text))
            # ASR result-gap bridge (2026-09-20): FunASR can stall for seconds
            # at long-sentence boundaries while the user keeps talking. The
            # local mic energy is the only activity signal in that window —
            # hold the flush (bounded) while near-field speech is locally
            # present so the pending text keeps accumulating instead of
            # flushing a mid-utterance turn.
            if self._hold_flush_for_mic_activity():
                continue
            self._flush_hold_since = 0.0
            self._flush_hold_turn_started = 0.0
            text = text.strip()
            self._pending_turn_text = ""
            self._complete_since = 0.0
            self._eot_semantic_complete = False
            self._eot_semantic_checked_text = ""
            # Per-turn backchannel budget + aux-echo guards reset at the turn
            # boundary (the echo of the previous turn's filler is consumed).
            self._backchannel_count_this_turn = 0
            self._aux_echo_guard = []
            self._consumed_offset = len(self._latest_full)
            _perf("eot_flush", (_now() - max(self._last_text_change, self._last_asr_activity)) * 1000,
                  text_len=len(text), utter_complete=_utterance_complete(text))
            # Keep interjection offset in sync with consumed offset so the next
            # turn's sentence extraction starts from the right position.
            self._interjection_offset = len(self._latest_full)
            # Snapshot interjection data for the upcoming turn's LLM context,
            # then reset for the next speech. The snapshot lets _system_messages
            # inject "you already said X during their speech" so the full answer
            # is coherent and doesn't repeat interjection content.
            self._interjection_snapshot = list(self._turn_interjections)
            self._reset_interjection_state()
            if self._speaking:
                # during speech, route through barge-in logic
                asyncio.create_task(self._on_user_turn(text))
            else:
                await self._send_json({"event": "asr_segment", "text": text})
                if (self._turn_active or self._playback_paused) and not self._closed:
                    # Think phase (generation in flight, no audio yet) or
                    # PLAYBACK PAUSED (audio stopped on speech onset, generation
                    # may already be done): route through the preemption /
                    # pause-resolution judgment in _on_user_turn so a clear
                    # interrupt cuts the in-flight answer (or switches the
                    # paused turn) instead of making the user wait for the
                    # whole answer to stream and play out — and a backchannel
                    # resumes the paused playback from its breakpoint.
                    asyncio.create_task(self._on_user_turn(text))
                else:
                    # Idle flush: convert the speculative filler synthesis
                    # FIRST (before enqueueing the turn) — the phrase enters
                    # the TTS queue as a pre-synthesized aux item here, so it
                    # plays ~immediately after ASR end and covers the
                    # generation+TTS window. `_handle_user_turn`'s fallback
                    # stays cooldown-suppressed behind this (2026-08-25).
                    await self._convert_filler_prefetch(text)
                    self._enqueue_turn(text, semantic_complete=semantic_complete_flush)
