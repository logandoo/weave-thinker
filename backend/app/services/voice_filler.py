# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from voice_service.py (VoiceFiller, 2026-10-20).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``voice_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (
    '_norm_barge_compare',
    '_now',
    '_split_segments',
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
    def _register_aux_echo(self, text: str) -> None:
        """Register an aux utterance (filler/backchannel) for mic-echo tail-
        stripping. Registers the SPLIT segments plus the joined phrase: the
        consumer typically cuts an aux utterance mid-phrase, so the ASR tail
        of an interrupted aux word may only contain one segment
        (`_speak_text(aux=True)` path, 2026-08-25 review I2)."""
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
        origin_epoch = self._prepare_direct_speech()
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
        # Deterministic content success is known before TTS. The direct-speech
        # queue carries its full text and claims resume ownership at first PCM;
        # audio/completion bits still gate the eventual ACK.
        if notification_id:
            self._notification_states[notification_id] = {
                "audio_sent": False,
                "generation_complete": False,
                "llm_succeeded": True,
                "tts_failed": False,
                "delivered": False,
                "delivery_attempt_id": info.get("delivery_attempt_id"),
            }
        try:
            played = await self._speak_direct_text(
                text, wait=True, notification_id=notification_id,
                origin_epoch=origin_epoch)
            if not played:
                self._queued_notice_ids.discard(notification_id)
                return
            if notification_id and not self._notice_state(notification_id).get("audio_sent"):
                # A pause before the first PCM is not delivery. Release the
                # replay latch without marking the unspoken notice complete.
                self._queued_notice_ids.discard(notification_id)
                return
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
        origin_epoch = self._prepare_direct_speech()
        self._turn_active = True
        await self._set_state("think")
        members = [{
            "notification_id": str(item.get("notification_id") or ""),
            "delivery_attempt_id": item.get("delivery_attempt_id"),
        } for item in items]
        self._notification_states[batch_id] = {
            "audio_sent": False,
            "generation_complete": False,
            "llm_succeeded": True,
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
            played = await self._speak_direct_text(
                text, wait=True, notification_id=batch_id,
                origin_epoch=origin_epoch)
            if not played:
                self._queued_notice_ids.discard(batch_id)
                for item in items:
                    self._queued_notice_ids.discard(str(item.get("notification_id") or ""))
                return
            if not self._notice_state(batch_id).get("audio_sent"):
                self._queued_notice_ids.discard(batch_id)
                for item in items:
                    self._queued_notice_ids.discard(str(item.get("notification_id") or ""))
                return
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
