# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from voice_service.py (VoiceWS, 2026-10-20).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (
    'ASRService',
    '_ACTIVE_VOICE_SESSIONS',
    '_VoiceASR',
    '_now',
    'asyncio',
    'json',
    'logger',
)


def _sync_voicewsmixin_globals() -> None:
    from app.services import voice_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class VoiceWSMixin:
    async def run(self) -> None:
        _sync_voicewsmixin_globals()
        # Ensure a conversation exists and announce the session FIRST, then
        # send `ready` IMMEDIATELY. `_load_identity` below can block for tens
        # of seconds (the shared-memory retrieval runs an AWAITED query-
        # expansion LLM call on the user's custom provider — user custom provider —
        # which is slow or unreachable; observed 15:12: sessions hung there
        # and the client timed out at 12s: "连接语音服务超时", completely
        # unusable). The client starts mic capture on `ready`; audio buffers
        # in `_audio_q` until the ASR upstream connects, so nothing is lost.
        try:
            await self._ensure_conversation()
        except Exception as exc:
            logger.debug("voice conversation setup failed (non-fatal): %s", exc)

        await self._send_json({"event": "ready", "tts": self.tts.is_configured()})

        # Identity/memory loading runs CONCURRENTLY with ASR startup. It can
        # block for tens of seconds (shared-memory retrieval awaits a
        # query-expansion LLM call on the user's provider — slow/unreachable
        # observed), while the user's first utterance must be transcribed and
        # answered immediately (2026-09-20 first-query TTFT fix). The first
        # answer waits at most `_IDENTITY_WAIT_SECONDS` for it (see
        # _handle_user_turn); loaded content applies from the next turn.
        self._identity_task = asyncio.create_task(self._load_identity())
        self._identity_task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )

        try:
            await self._load_history()
        except Exception as exc:
            logger.debug("voice conversation setup failed (non-fatal): %s", exc)

        asr_service = ASRService()
        if not asr_service.enabled:
            await self._send_json({"event": "error", "error": "语音识别服务未配置"})
            return

        hotwords = []
        vocabulary_id: Optional[str] = None
        try:
            from sqlalchemy import select
            from app.db.database import UserAsrHotword

            res = await self.db.execute(
                select(UserAsrHotword).where(UserAsrHotword.user_id == self.user.id)
            )
            items = res.scalars().all()
            hotwords = [{"text": i.text, "weight": i.weight, "lang": i.lang} for i in items]
            for item in items:
                if item.dashscope_vocabulary_id:
                    vocabulary_id = item.dashscope_vocabulary_id
                    break
        except Exception:
            hotwords = []
            vocabulary_id = None

        try:
            self._asr = _VoiceASR(
                asr_service,
                self._asr_events,
                hotwords,
                vocabulary_id,
                context=list(self._history),
            )
            await self._asr.start()
        except Exception as exc:
            logger.error("voice ASR start failed: %s", exc)
            await self._send_json({"event": "error", "error": f"无法启动语音识别: {exc}"})
            return

        # Register so out-of-band events (background task completion) can
        # find this session and have the assistant proactively announce them.
        _ACTIVE_VOICE_SESSIONS[self.user.id] = self

        self._tasks = [
            asyncio.create_task(self._asr_pipeline()),
            asyncio.create_task(self._eot_watchdog()),
            asyncio.create_task(self._tts_consumer()),
            asyncio.create_task(self._responder()),
            asyncio.create_task(self._notification_replay_loop()),
        ]

        try:
            await self._receive_loop()
        finally:
            await self._shutdown()
    async def _receive_loop(self) -> None:
        _sync_voicewsmixin_globals()
        while not self._closed:
            try:
                message = await self.websocket.receive()
            except Exception:
                break
            mtype = message.get("type")
            if mtype == "websocket.disconnect":
                break
            data = message.get("bytes")
            text = message.get("text")
            if data is not None:
                if self._asr:
                    pcm16 = await asyncio.to_thread(ASRService._float32_to_pcm16, data)
                    if pcm16:
                        self._track_mic_energy(pcm16)
                        await self._asr.send_audio(pcm16)
            elif text is not None:
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                await self._handle_client_event(msg)
    async def _handle_client_event(self, msg: dict) -> None:
        _sync_voicewsmixin_globals()
        event = msg.get("event")
        if event == "text":
            content = (msg.get("text") or "").strip()
            if content:
                await self._on_user_turn(content)
        elif event == "playback_progress":
            # Client-authoritative playback position. Ties the spoken audio to
            # the answer text so truncation is attributed to playback, not text.
            try:
                self._playback_played_sec = float(msg.get("played_sec", 0) or 0)
                total = float(msg.get("total_sec", 0) or 0)
                if total > 0:
                    self._playback_total_sec = total
            except (TypeError, ValueError):
                pass
        elif event == "playback_drained":
            ack_notification_id = msg.get("notification_id")
            notice_state = self._notification_states.get(ack_notification_id)
            if notice_state is None and ack_notification_id == self._active_notification_id:
                notice_state = {
                    "audio_sent": self._active_notification_audio_sent,
                    "generation_complete": self._active_notification_generation_complete,
                    "tts_failed": False,
                    "delivered": False,
                }
                self._notification_states[ack_notification_id] = notice_state
            if not ack_notification_id:
                notice_state = {}
            if ack_notification_id and notice_state and notice_state.get("delivered"):
                return
            if ack_notification_id and notice_state:
                try:
                    ack_burst_seq = int(msg.get("notice_burst_seq", 0) or 0)
                    current_burst_seq = int(notice_state.get("burst_seq", 0) or 0)
                except (TypeError, ValueError):
                    ack_burst_seq = -1
                    current_burst_seq = int(notice_state.get("burst_seq", 0) or 0)
                if ack_burst_seq != current_burst_seq:
                    # A pre-pause drain may arrive after a newer resumed burst
                    # started. It proves nothing about the newer audible tail.
                    logger.debug(
                        "voice playback_drained ignored (burst mismatch): %s ack=%s current=%s",
                        ack_notification_id, ack_burst_seq, current_burst_seq,
                    )
                    return
            if (
                ack_notification_id
                and ack_notification_id == self._active_notification_id
                and notice_state
                and notice_state.get("tts_failed")
            ):
                self._queued_notice_ids.discard(ack_notification_id)
                self._active_notification_id = None
                self._active_notification_audio_sent = False
                self._active_notification_generation_complete = False
                await self._mark_speaking_end()
                await self._send_json({
                    "event": "notification_failed",
                    "notification_id": ack_notification_id,
                })
                return
            if ack_notification_id and (
                not notice_state
                or not notice_state.get("audio_sent")
                or not notice_state.get("generation_complete")
                or notice_state.get("tts_failed")
            ):
                if ack_notification_id == self._active_notification_id:
                    await self._send_json({
                        "event": "notification_ack_pending",
                        "notification_id": ack_notification_id,
                    })
                else:
                    # This older audible attempt can never complete. Let a
                    # later replay queue it instead of deduping it forever.
                    self._queued_notice_ids.discard(ack_notification_id)
                logger.debug("voice playback_drained ignored (stale/incomplete notice): %s", ack_notification_id)
                return
            if ack_notification_id and ack_notification_id != self._active_notification_id:
                # A valid late ACK belongs to an older notice. Settle only that
                # row; never end or clear the newer notification's playback.
                notice_state["delivered"] = True
                try:
                    from app.db.database import AsyncSessionLocal
                    from app.services.voice_notification_service import mark_voice_notification_delivered

                    async with AsyncSessionLocal() as db:
                        members = notice_state.get("members") or []
                        if members:
                            for member in members:
                                await mark_voice_notification_delivered(
                                    db,
                                    str(member.get("notification_id") or ""),
                                    member.get("delivery_attempt_id"),
                                )
                        else:
                            await mark_voice_notification_delivered(
                                db, ack_notification_id,
                                notice_state.get("delivery_attempt_id"),
                            )
                except Exception:
                    logger.debug("voice late notice ACK failed: %s", ack_notification_id, exc_info=True)
                self._queued_notice_ids.discard(ack_notification_id)
                for member in (notice_state.get("members") or []):
                    member_id = str(member.get("notification_id") or "")
                    if member_id:
                        self._queued_notice_ids.discard(member_id)
                return
            # Client finished playing every scheduled chunk — the audible tail
            # is over, so speaking is truly done (clears barge-in state).
            #
            # Advance the burst anchors to the HEARD position first: after a
            # drain the next item (e.g. an interjection aux after a gap) begins
            # a NEW client burst (speaking_start resets the client's clock), so
            # its played_sec restarts at 0. Without this anchor a later onset
            # pause would compute the breakpoint as base(0) + tiny played_sec
            # and a backchannel/defer resume would REPLAY the whole answer the
            # user had already heard (生产事故 conv 19b63be9 2026-09-04:
            # 270 字答案播完后插话期间被打断，defer 恢复重播了 263 字旧答案，
            # 新问题的回答音频排队直到连接关闭都未播出）。
            # 只计入已完成答案段（aux 从不入 _turn_segments，口径一致）；
            # 在飞片段未录段不计——低估方向安全（最多重播一小段已听尾部，
            # 绝不跳过未听内容）。_playback_paused 期间客户端不会发 drained。
            _heard_chars = sum(len(s["text"]) for s in self._turn_segments)
            _full_len = len(self._turn_reply_text or "")
            if _full_len:
                # 插话（interjection）走 _speak_text 无 aux 标记、会被录段，
                # 但其文本不属于答案正文——计入会越过答案末尾（A4.9 R1 M3）。
                # 钳制到答案长度：答案段文本恒为 _turn_reply_text 的前缀子集，
                # 超限只可能来自非答案污染。
                _heard_chars = min(_heard_chars, _full_len)
            self._burst_base_chars = _heard_chars
            self._burst_base_sec = self._turn_audio_sec_total
            # 客户端对新 burst 从 0 重计 played_sec；清掉上一 burst 的残值，
            # 否则 drained→首个 progress 事件（≤250ms）窗口内的暂停会把
            # 过期 played_sec 叠进断点（A4.9 R1 M2）。
            self._playback_played_sec = 0.0
            await self._mark_speaking_end()
            if not ack_notification_id and self._active_notification_id:
                # A plain drain cannot prove which notice was heard, so never
                # mark it delivered. It does, however, release active ownership
                # for the next notice and for a later explicit retry.
                self._queued_notice_ids.discard(self._active_notification_id)
                self._active_notification_id = None
                self._active_notification_audio_sent = False
                self._active_notification_generation_complete = False
            notice_id = msg.get("notification_id")
            notice_audio_sent = notice_state.get("audio_sent", False)
            notice_generation_complete = notice_state.get("generation_complete", False)
            if (
                notice_id
                and notice_audio_sent
                and notice_generation_complete
                and not notice_state.get("tts_failed")
            ):
                notice_state["delivered"] = True
                self._active_notification_id = None
                self._active_notification_audio_sent = False
                self._active_notification_generation_complete = False
                try:
                    from app.db.database import AsyncSessionLocal
                    from app.services.voice_notification_service import mark_voice_notification_delivered

                    async with AsyncSessionLocal() as db:
                        members = notice_state.get("members") or []
                        if members:
                            for member in members:
                                await mark_voice_notification_delivered(
                                    db,
                                    str(member.get("notification_id") or ""),
                                    member.get("delivery_attempt_id"),
                                )
                        else:
                            await mark_voice_notification_delivered(
                                db, notice_id,
                                notice_state.get("delivery_attempt_id"),
                            )
                except Exception:
                    self._queued_notice_ids.discard(notice_id)
                    logger.debug("voice notice delivered ACK failed: %s", notice_id, exc_info=True)
                for member in (notice_state.get("members") or []):
                    member_id = str(member.get("notification_id") or "")
                    if member_id:
                        self._queued_notice_ids.discard(member_id)
        elif event == "audio_proximity":
            # Browser-side acoustic near-field signal: the client classifies
            # its own mic input as near-field (user close to the mic) vs
            # far-field (environment speech). Far-field speech must never
            # pause playback or interrupt the current answer. Only a literal
            # boolean counts — anything else keeps the previous state.
            near = msg.get("near")
            if isinstance(near, bool):
                self._prox_seen = True
                self._prox_near = near
                self._prox_updated = _now()
        elif event == "interrupt":
            await self._request_interrupt()
        elif event == "_test_llm_stall":
            # Test-only: force the next main LLM stream to stall (heartbeats
            # only) so the stall guard is verifiable over a real WS. Clamped
            # and OverflowError-safe (A4.9 r2 Minor-2).
            try:
                self._test_llm_stall = max(1, min(10, int(msg.get("count", 1) or 1)))
            except (TypeError, ValueError, OverflowError):
                self._test_llm_stall = 1
        elif event == "_test_inject_asr":
            # Test-only: inject ASR text to test interjection without real
            # audio. Simulates FunASR emitting partial/segment events.
            inj_text = msg.get("text", "")
            is_segment = bool(msg.get("segment", False))
            if is_segment:
                # Segment (sentence_end): emit with the current accumulated text
                await self._asr_events.put({"type": "segment", "text": self._latest_full})
            elif inj_text:
                # Partial: accumulate text and emit
                full = (self._latest_full or "") + inj_text
                self._latest_full = full
                await self._asr_events.put({"type": "partial", "text": full})
        elif event == "stop":
            self._closed = True
    async def _shutdown(self) -> None:
        _sync_voicewsmixin_globals()
        self._closed = True
        if _ACTIVE_VOICE_SESSIONS.get(self.user.id) is self:
            _ACTIVE_VOICE_SESSIONS.pop(self.user.id, None)
        self._interrupt.set()
        self._drop_filler_prefetch()
        # stop queues
        try:
            self._turn_queue.put_nowait(None)
        except Exception:
            pass
        try:
            self._tts_queue.put_nowait(None)
        except Exception:
            pass
        if self._prefetch.get("task") and not self._prefetch["task"].done():
            self._prefetch["task"].cancel()
        if self._identity_task and not self._identity_task.done():
            self._identity_task.cancel()
            try:
                await self._identity_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in self._vmem_recall_inflight:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._asr:
            await self._asr.close()
    async def _send_json(self, payload: dict) -> None:
        _sync_voicewsmixin_globals()
        if self._closed:
            return
        try:
            async with self._send_lock:
                await self.websocket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            logger.debug("voice send_json failed: %s", exc)
    async def _send_bytes(self, data: bytes) -> None:
        _sync_voicewsmixin_globals()
        if self._closed:
            return
        try:
            async with self._send_lock:
                await self.websocket.send_bytes(data)
        except Exception as exc:
            logger.debug("voice send_bytes failed: %s", exc)
