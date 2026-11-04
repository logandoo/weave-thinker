# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice generation mixin (P5.5 move-only, 2026-10-18)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from app.tools.registry import registry
from app.services.workspace_service import ensure_user_workspace

logger = logging.getLogger(__name__)

# Tools that create/control/mutate delegated tasks. Synthetic notification
# turns (user_turn=False) never expose or dispatch them (§5.1(5), §13.2).
TASK_CONTROL_TOOL_NAMES = frozenset({
    "background_task",
    "delegate_agent_task",
    "list_agent_tasks",
    "get_agent_task",
    "cancel_agent_task",
    "steer_agent_task",
    "summarize_agent_task",
    "open_agent_task_result",
    "request_user_input",
})

_voice_clarification_tasks: set = set()

_DELEGATION_CLAIM_MARKERS = (
    "已委派", "已经委派", "已交给", "已经交给", "已把任务交给",
    "已提交给", "已经提交给", "任务已创建", "已创建任务",
    "已创建独立会话", "已安排给", "已经安排给",
    "转到后台", "已转到后台", "后台执行中", "已启动后台", "已放到后台",
    "delegated", "has been delegated", "task has been created",
    "task was created", "i've handed", "i have handed",
)


_DELEGATION_REQUEST_RE = re.compile(
    r"(委派|交给|让[^。！？\n]{0,12}助手|后台(执行|跑|处理)|异步(执行|任务)|"
    r"独立会话|delegate|hand[\s-]*(it[\s-]*)?off|background\s+task)",
    re.IGNORECASE,
)


def user_turn_requests_delegation(text: str) -> bool:
    """True when the real user turn explicitly asks for a delegation."""
    value = text or ""
    # Questions / definitions are not an instruction to delegate.
    if re.search(
        r"(委派|交给|delegate).{0,16}(是什么|什么意思|怎么|如何|吗|？|\?)",
        value, re.IGNORECASE,
    ):
        return False
    return bool(_DELEGATION_REQUEST_RE.search(value))


def claims_delegation_success(text: str) -> bool:
    """True when a TOOL-LESS prefetch claims a delegation already happened.

    Such a claim can only be hallucinated (the prefetch had no tools and no DB
    transaction); it must never be spoken as a receipt (§5.1 / §17.1).
    """
    value = (text or "").casefold()
    return any(marker.casefold() in value for marker in _DELEGATION_CLAIM_MARKERS)


def _split_segments(*a, **k):
    from app.services.voice_service import _split_segments as f
    return f(*a, **k)


def _build_tool_notice_names(*a, **k):
    from app.services.voice_service import _build_tool_notice_names as f
    return f(*a, **k)


def _build_voice_tool_results(*a, **k):
    from app.services.voice_service import _build_voice_tool_results as f
    return f(*a, **k)


def _detect_text_language(*a, **k):
    from app.services.voice_service import _detect_text_language as f
    return f(*a, **k)


def _escalated_voice_max_tokens(*a, **k):
    from app.services.voice_service import _escalated_voice_max_tokens as f
    return f(*a, **k)


def _is_voice_rate_limit(*a, **k):
    from app.services.voice_turn_taking import _is_voice_rate_limit as f
    return f(*a, **k)


def _now(*a, **k):
    from app.services.voice_service import _now as f
    return f(*a, **k)


def _perf(*a, **k):
    from app.services.voice_service import _perf as f
    return f(*a, **k)


def _plan_voice_tool_calls(*a, **k):
    from app.services.voice_service import _plan_voice_tool_calls as f
    return f(*a, **k)


def _voice_tool_names(*a, **k):
    from app.services.voice_service import _voice_tool_names as f
    return f(*a, **k)


def _voice_truncation_feedback(*a, **k):
    from app.services.voice_service import _voice_truncation_feedback as f
    return f(*a, **k)


def strip_voice_tags(*a, **k):
    from app.services.voice_service import strip_voice_tags as f
    return f(*a, **k)


class VoiceGenerationMixin:
    def _should_suppress_stale_low_value(self, text: str, user_turn: bool) -> bool:
        """Suppress only a stale acknowledgement, never a substantive answer.

        Output ownership is deliberately narrow: defer may still finish a real
        answer, but a delayed "你说，我听着" after the user already asked a new
        question has no conversational value and must not be finalized/spoken.
        """
        if not user_turn or self._generation_input_seq >= self._latest_input_seq:
            return False
        from app.services.voice_service import _is_low_value_ack_text
        return _is_low_value_ack_text(text)

    def _generation_is_stale(self, user_turn: bool) -> bool:
        return user_turn and self._generation_input_seq < self._latest_input_seq

    def _has_pending_user_turn(self) -> bool:
        return any(
            isinstance(item, dict) and not item.get("notice")
            for item in self._turn_queue._queue
        )

    async def _generate_and_speak_impl(self, text: str, allow_tools: bool = False, prefetched: Optional[str] = None,
                                       allow_filler: bool = True, user_turn: bool = True,
                                       input_seq: Optional[int] = None,
                                       prefetched_summary_note: str = "") -> None:
        summary_note_at_start = self._pending_tool_summary_note
        if input_seq is None:
            input_seq = self._generation_input_seq or self._latest_input_seq
        self._generation_input_seq = input_seq
        self._history.append({"role": "user", "content": text})
        # B11（2026-09-14）语音纠正 shadow；2026-09-20（用户指令）全 agentic：
        # 删除关键词预分类闸门（detect_signal 已移除），真实用户轮无条件
        # shadow 记录候选（applied=False，LLM 判定 is_correction），不应用。
        # A4.9 r1 Important-2：后台任务系统通知的合成 prompt 不是用户语音
        # （user_turn=False），不得送澄清检测。
        try:
            if user_turn and text and self.user is not None:
                task = asyncio.create_task(self._clarification_shadow(text))
                _voice_clarification_tasks.add(task)
                task.add_done_callback(_voice_clarification_tasks.discard)
        except Exception:
            logger.debug("voice clarification shadow scheduling failed", exc_info=True)
        self._task_cancelled = False
        self._turn_msg_id = None
        # This turn's playback-attribution counters start empty: an interrupt
        # during generation must not attribute truncation percentages to the
        # PREVIOUS turn's audio (the _request_interrupt note math reads these).
        # Echo detection (_spoken_text_recent) and the speaking safety deadline
        # (_speaking_deadline) are deliberately NOT reset here — the previous
        # burst's audio may still be playing (deferred-then-answered case).
        self._turn_segments = []
        self._turn_audio_sec_total = 0.0
        self._current_seg_text = ""
        self._current_seg_audio_sec = 0.0
        self._playback_played_sec = 0.0
        self._playback_total_sec = 0.0
        self._burst_base_chars = 0
        self._burst_base_sec = 0.0
        self._burst_is_first = True
        self._turn_reply_text = ""
        # Anchor the send-pacing clock at the turn start. The consumer's
        # attribution reset is epoch-gated and does NOT fire on the first
        # (empty-list) segment, so without this the 1x send throttle would
        # keep a stale anchor from a previous turn, flood the websocket
        # buffers (~2MB ≈ 40s of audio) and stall progressively — observed
        # live: per-segment delays 0.8s → 2.7s → 17.6s → 42s → 59s → 119s.
        self._send_pace_start = _now()
        # A new generation invalidates any stale onset-pause breakpoint from a
        # previous turn (defense in depth for the C1 fix in _request_interrupt),
        # and starts a new epoch: the onset-pause hook only fires while the
        # playing burst belongs to THIS epoch (see _speaking_epoch).
        self._turn_epoch += 1
        self._playback_paused = False
        self._paused_spoken_chars = 0

        # Fast path: prefetched text (no tools) -> speak directly.
        if prefetched and user_turn and claims_delegation_success(prefetched):
            # The tool-less prefetch cannot have committed a task. Discard the
            # fake "已委派" receipt and regenerate with the real tool loop.
            logger.warning(
                "voice prefetch claimed delegation without a task; discarding prefetch")
            prefetched = None
            prefetched_summary_note = ""
        if prefetched:
            prefetch_note = prefetched_summary_note or getattr(self, "_prefetch_summary_note", "")
            if self._should_suppress_stale_low_value(prefetched, user_turn):
                self._turn_reply_text = ""
                self._interrupt.set()
                self._drain_tts_queue()
                await self._mark_speaking_end()
                await self._send_json({
                    "event": "generation_cancelled",
                    "reason": "superseded_by_newer_user_turn",
                    "input_seq": self._generation_input_seq,
                })
                return
            self._clear_pending_filler()
            self._answer_content_epoch = self._turn_epoch
            await self._set_state("speak")
            clean = strip_voice_tags(prefetched)
            await self._send_json({"event": "assistant_text", "text": clean, "done": False})
            await self._speak_text(prefetched)
            self._history.append({"role": "assistant", "content": prefetched})
            self._last_answer_text = prefetched
            self._vmem_answer_epoch = self._turn_epoch
            await self._persist_message("assistant", prefetched)
            if summary_note_at_start and prefetch_note == summary_note_at_start:
                self._pending_tool_summary_note = ""
            await self._send_json({"event": "assistant_text", "text": clean, "done": True})
            return

        # TTFT-gated filler: schedule the watch BEFORE the first LLM call so
        # its clock matches "query sent"; it consumes the EoT prefetch (or
        # falls back to on-demand synthesis) and plays only if no content
        # token arrives within the threshold (2026-09-13). Proactive notices
        # pass allow_filler=False (no user question to cover).
        if allow_filler:
            self._schedule_filler_ttft_watch()
        else:
            self._clear_pending_filler()
        svc, _ = self._build_llm()
        if self._test_llm_stall > 0:
            # Test-only hook (mirrors the `_test_inject_asr` convention): the
            # next main LLM stream yields only heartbeat sentinels, so the
            # stall guard can be exercised over a real WebSocket without a
            # real provider outage.
            self._test_llm_stall -= 1

            async def _stalled_stream(*_a, **_k):
                while True:
                    await asyncio.sleep(1.0)
                    yield {"type": "heartbeat", "data": None}

            svc.stream_chat_structured = _stalled_stream
        tools = None
        if allow_tools:
            try:
                from app.tools.registry import registry
                from app.services.user_mcp_service import ensure_user_mcp_tools

                # 2026-11-01: voice runs are owner-bound too — apply the user's
                # MCP rows before resolving the tool definitions.
                await ensure_user_mcp_tools(self.db, self.user.id)

                tools = registry.get_definitions(
                    _voice_tool_names(str(getattr(self.user, 'id', '') or '') or None),
                    quiet=True,
                    user_id=str(getattr(self.user, 'id', '') or '') or None,
                ) or None
                # §5.1(5): synthetic notification turns (user_turn=False) never
                # expose delegation or task-control tools, regardless of flags.
                if not user_turn and tools:
                    tools = [
                        t for t in tools
                        if ((t.get("function") or {}).get("name")
                            not in TASK_CONTROL_TOOL_NAMES)
                    ] or None
            except Exception as exc:
                logger.debug("voice tool defs unavailable: %s", exc)
                tools = None

        messages = self._generation_messages()
        full_reply = ""
        held_tts_segments: list[str] = []
        generation_had_error = False
        preempted_tool_preamble = False
        tool_rounds = 0
        max_tool_rounds = 8
        notice_done = False
        # 截断恢复（conv fcb2e60a 事故）：长工具调用被输出预算截断时，不执行
        # 残缺参数，而是回可行动的截断错误并把本轮预算翻倍（有上限/有界次数）。
        turn_max_tokens = self.config.voice_max_tokens
        truncation_retries = 0
        truncation_retry_limit = self.config.voice_truncation_retry_limit
        # Accumulate tool interaction for persistence (fix: save tool calls
        # to session record so the transcript shows what tools were used).
        _all_tool_calls: list[dict] = []
        _all_tool_results: list[dict] = []

        while True:
            # User explicitly cancelled the task — stop immediately.
            # NOTE: _interrupt (TTS barge-in) does NOT abort the task loop —
            # the task continues even if the user interrupts the speech. Only
            # an explicit stop command (_task_cancelled) or disconnect stops it.
            if self._task_cancelled:
                await self._send_json({"event": "assistant_text", "text": "（任务已取消）", "done": True})
                self._history.append({"role": "assistant", "content": "（任务已取消）"})
                return
            # Preemption: the user spoke while tools were running. Their turn
            # is sitting in the queue; starting another tool round would keep
            # them waiting in silence. Stop the loop HERE (between rounds — a
            # tool call in flight is never aborted) and let the queued turn
            # take the floor. Tool results gathered so far stay in history, so
            # the next turn's answer can use them and the task can be resumed
            # with a simple "继续".
            if tool_rounds > 0 and self._has_pending_user_turn():
                preempted_tool_preamble = True
                if _all_tool_results and not self._pending_tool_summary_note:
                    self._pending_tool_summary_note = (
                        "（系统提示，不要朗读）：上一轮用户请求的工具已经返回结果，"
                        "但由于用户插话，尚未向用户交代结果。请先根据对话中的真实工具结果，"
                        "用一句自然口语交代完成或失败情况，再回答当前用户的新问题。"
                        "不要重复“我先做/我来调用”等过渡话，不得编造结果。）"
                    )
                logger.info(
                    "voice tool loop preempted by pending user turn (round %d)",
                    tool_rounds,
                )
                break
            seg_buffer = ""
            reply_text = ""
            tool_calls = None
            await self._set_state("speak" if tool_rounds else "think")
            _llm_start = _now()
            _first_content = False
            _rate_retries = 0
            # Retry wrapper: xiaomimimo hard rate-limits when the voice
            # session fires several LLM calls at once (interjection + intent +
            # main stream — the 0.5s stagger alone does not always prevent
            # 429s). Without a retry the whole turn dies with a "生成失败"
            # error event and the user gets NO answer. Retry only while
            # nothing has been produced (retrying mid-stream would duplicate
            # content) and only on rate-limit errors.
            async with self._llm_gate:
                while True:
                    try:
                        # Stall guard clock (2026-09-20): the upstream can hang
                        # without producing any chunk; `_heartbeat_wrapped`
                        # then only yields heartbeat sentinels, and without a
                        # consumer-side bound this loop never exits — the turn
                        # stays "active" forever and every later user turn is
                        # swallowed (prod conv 213f5a55: 5+ min no reply).
                        # Resets per attempt (a 429 retry re-opens the stream).
                        _stream_open_ts = _now()
                        _last_real_event_ts = _stream_open_ts
                        async for ev in svc.stream_chat_structured(
                            messages,
                            tools=tools,
                            temperature=self.config.voice_temperature,
                            max_tokens=turn_max_tokens,
                            extra_body=self._extra_body(svc),
                        ):
                            if self._closed or self._task_cancelled:
                                return
                            if self._interrupt.is_set() and tool_calls is None:
                                # Barge-in: the user interrupted this answer mid-playback.
                                # Stop generating it NOW — their queued turn must start
                                # immediately, not after this stream burns to its end
                                # (observed live: 11s dead wait after an interrupt).
                                # A completed tool_calls payload is never aborted here.
                                logger.info("voice stream aborted early (barge-in)")
                                break
                            etype = ev["type"]
                            if etype == "heartbeat":
                                # No real chunk for the heartbeat interval —
                                # enforce the two configured bounds.
                                now_hb = _now()
                                ttft_limit = self.config.voice_llm_ttft_timeout_seconds
                                if (
                                    not _first_content
                                    and ttft_limit > 0
                                    and now_hb - _stream_open_ts >= ttft_limit
                                ):
                                    raise TimeoutError(
                                        f"语音模型首字超时（{ttft_limit:.0f}s 未响应）"
                                    )
                                inact_limit = self.config.voice_llm_inactivity_timeout_seconds
                                if (
                                    _first_content
                                    and inact_limit > 0
                                    and now_hb - _last_real_event_ts >= inact_limit
                                ):
                                    raise TimeoutError(
                                        f"语音模型生成停滞（{inact_limit:.0f}s 无新内容）"
                                    )
                                logger.warning(
                                    "voice main LLM heartbeat without content (ttft=%.1fs, idle=%.1fs)",
                                    now_hb - _stream_open_ts,
                                    now_hb - _last_real_event_ts,
                                )
                                continue
                            _last_real_event_ts = _now()
                            if etype == "content":
                                data = ev["data"]
                                if not _first_content:
                                    _first_content = True
                                    # TTFT gate: this turn's first content token
                                    # arrived — the filler watch must suppress.
                                    self._answer_content_epoch = self._turn_epoch
                                    _perf("llm_ttft", (_now() - _llm_start) * 1000,
                                          round_no=tool_rounds, turn_ms=(_now() - self._turn_started) * 1000)
                                reply_text += data
                                seg_buffer += data
                                # Mirror the accumulated text into the
                                # interruption-note source INCREMENTALLY: a
                                # concurrent think-phase preemption may call
                                # _request_interrupt at any moment — mirroring
                                # only after the round would race with it.
                                self._turn_reply_text = full_reply + reply_text
                                # capture leading style tag
                                if not self._style_tag:
                                    m = re.match(r"^\s*[(（][^)）]{1,12}[)）]", seg_buffer)
                                    if m:
                                        self._style_tag = m.group(0)
                                # stream clean accumulated display text
                                await self._send_json(
                                    {"event": "assistant_text", "text": strip_voice_tags(reply_text), "done": False}
                                )
                                segs, seg_buffer = _split_segments(seg_buffer)
                                for s in segs:
                                    if s.strip():
                                        _perf("seg_to_tts", (_now() - _llm_start) * 1000,
                                              round_no=tool_rounds, seg_len=len(s))
                                        if self._generation_is_stale(user_turn):
                                            held_tts_segments.append(s)
                                        else:
                                            await self._tts_queue.put({
                                                "text": s, "epoch": self._turn_epoch, "dedup": True,
                                                "notification_id": self._current_notice_id,
                                                "input_seq": self._generation_input_seq,
                                                "input_seq": self._generation_input_seq,
                                            })
                            elif etype == "tool_calls":
                                tool_calls = ev["data"]
                            elif etype == "error":
                                raise RuntimeError(str(ev["data"]))
                        break
                    except Exception as exc:
                        if (
                            _rate_retries < self.config.voice_llm_retry_attempts
                            and not reply_text
                            and tool_calls is None
                            and not self._interrupt.is_set()
                            and _is_voice_rate_limit(exc)
                        ):
                            _rate_retries += 1
                            delay = 1.0 * _rate_retries
                            logger.warning(
                                "voice main LLM rate-limited (retry %d/%d) — backing off %.1fs: %s",
                                _rate_retries, self.config.voice_llm_retry_attempts, delay, exc,
                            )
                            await asyncio.sleep(delay)
                            continue
                        generation_had_error = True
                        logger.error("voice main LLM error: %s", exc)
                        if not reply_text:
                            await self._send_json({"event": "error", "error": f"生成失败: {exc}"})
                        break            # flush remaining buffer as a final segment
            if seg_buffer.strip():
                if self._generation_is_stale(user_turn):
                    held_tts_segments.append(seg_buffer)
                else:
                    await self._tts_queue.put({
                        "text": seg_buffer, "epoch": self._turn_epoch, "dedup": True,
                        "notification_id": self._current_notice_id,
                                                "input_seq": self._generation_input_seq,
                    })
            if self._turn_reply_text != full_reply + reply_text:
                self._turn_reply_text = full_reply + reply_text

            if tool_calls and allow_tools and tool_rounds < max_tool_rounds and not self._task_cancelled:
                tool_rounds += 1
                if not notice_done:
                    notice_done = True
                    # Tool-name language follows the assistant's own reply when
                    # available, else the user's utterance — a Chinese reply is
                    # announced as 联网查询, an English one as "web search".
                    lang = _detect_text_language(reply_text) if reply_text and reply_text.strip() else _detect_text_language(text)
                    names = _build_tool_notice_names(tool_calls, lang)
                    notice = f"(平静)好的，我来调用{names}工具帮你处理。这个任务可能需要较长时间，请耐心等待，我会持续为你工作。"
                    await self._send_json({"event": "tool_notice", "text": strip_voice_tags(notice)})
                    await self._speak_text(notice)
                elif tool_rounds == 3:
                    progress_notice = "(平静)任务仍在进行中，请继续耐心等待。"
                    await self._send_json({"event": "tool_notice", "text": strip_voice_tags(progress_notice)})
                    await self._speak_text(progress_notice)
                elif tool_rounds == 5:
                    progress_notice = "(平静)这个任务比较复杂，还需要一些时间，感谢你的耐心等待。"
                    await self._send_json({"event": "tool_notice", "text": strip_voice_tags(progress_notice)})
                    await self._speak_text(progress_notice)
                # Clear TTS interrupt so tool execution proceeds even if the
                # user barge-in interrupted the notice. Only an explicit stop
                # command (_task_cancelled) prevents tool execution.
                self._interrupt.clear()
                # User cancelled during the tool notice — don't execute tools.
                if self._task_cancelled:
                    await self._send_json({"event": "assistant_text", "text": "（任务已取消）", "done": True})
                    self._history.append({"role": "assistant", "content": "（任务已取消）"})
                    return
                # 截断调用绝不执行：参数残缺时 safe_call 归一化为 {}（供上游
                # 合法回放），原始 tool_calls 保持原样落档取证。
                tool_plan = _plan_voice_tool_calls(tool_calls)
                safe_tool_calls = [entry["safe_call"] for entry in tool_plan]
                # append assistant tool-call message then execute tools
                messages.append({
                    "role": "assistant",
                    "content": reply_text or None,
                    "tool_calls": safe_tool_calls,
                })
                # Mirror into _history so LATER turns can see the real tool
                # outputs (notebook names, search results...). Without this the
                # next turn's context only holds the assistant's summary and
                # the model confabulates the details it forgot.
                self._history.append({
                    "role": "assistant",
                    "content": reply_text or None,
                    "tool_calls": safe_tool_calls,
                })
                _all_tool_calls.extend(tool_calls)
                self._tools_running = True
                from app.tools.registry import registry

                if self._workspace_path is None:
                    try:
                        from app.services.workspace_service import ensure_user_workspace

                        ws = await ensure_user_workspace(
                            self.db, self.user.id, self.user.username
                        )
                        self._workspace_path = ws.root_path
                    except Exception as exc:
                        logger.debug("voice workspace init failed: %s", exc)
                        self._workspace_path = ""

                for entry in tool_plan:
                    if self._task_cancelled:
                        break
                    tc = entry["call"]
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    if entry["truncated"]:
                        # conv fcb2e60a：旧行为把截断参数静默降级为 {} 并派发，
                        # 工具回 "Unknown action"，模型同构重试 7 次后放弃。
                        logger.warning(
                            "voice tool call truncated — not dispatched: name=%s args_len=%d",
                            name, len(fn.get("arguments") or ""),
                        )
                        result = _voice_truncation_feedback(name)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                        self._history.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                        _all_tool_results.append({
                            "tool_call_id": tc.get("id", ""),
                            "name": name,
                            "content": result,
                        })
                        await self._send_json({"event": "tool_result", "name": name})
                        await self._persist_turn_progress(
                            full_reply + reply_text,
                            json.dumps(_all_tool_calls, ensure_ascii=False),
                            _build_voice_tool_results(_all_tool_results),
                        )
                        continue
                    args = entry["args"] or {}
                    if name in TASK_CONTROL_TOOL_NAMES and not user_turn:
                        result = json.dumps({
                            "error": "agent_task_tool_unavailable",
                            "reason": "通知/合成轮不得创建、控制或转述委派任务",
                        }, ensure_ascii=False)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                        self._history.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                        _all_tool_results.append({
                            "tool_call_id": tc.get("id", ""),
                            "name": name,
                            "content": result,
                        })
                        await self._send_json({"event": "tool_result", "name": name})
                        continue
                    # P1b/P1 (§13.1): voice has no dialog. delegate_agent_task is
                    # permitted ONLY on a real user turn with a one-time scoped
                    # permit bound to user + C1 + tool + canonical args hash.
                    try:
                        from app.core.config import get_config as _get_cfg
                        _tma_enforce = _get_cfg().tma_mode == "enforce"
                    except Exception:
                        _tma_enforce = True  # config failure: fail closed
                    if _tma_enforce:
                        from app.services import act_gate_service as _ag
                        if _ag.is_act_tool(name):
                            _permitted = False
                            _deny_reason = (
                                "语音模式没有 act gate 审批对话框；"
                                "请改用文字聊天执行该动作"
                            )
                            if name == "delegate_agent_task" and user_turn:
                                _source_msg = str(
                                    self._last_persisted_user_message_id or "")
                                if _source_msg:
                                    try:
                                        from app.db.database import Message as _Message

                                        _src_msg = await self.db.get(_Message, _source_msg)
                                        if _src_msg is None or not user_turn_requests_delegation(
                                                _src_msg.content or ""):
                                            raise ValueError("user turn did not request delegation")
                                        from app.services import action_token_service as _tokens
                                        from app.services.tenant_service import (
                                            tenant_segment as _tenant,
                                        )

                                        _permit_args = dict(args)
                                        _permit_args["source_message_id"] = _source_msg
                                        _fp = _ag.action_fingerprint(
                                            name, _permit_args,
                                            user_id=str(self.user.id),
                                            conversation_id=str(
                                                self._conversation.id),
                                        )
                                        _tok = await _tokens.issue_action_token(
                                            tenant_id=_tenant(),
                                            user_id=str(self.user.id),
                                            conversation_id=str(self._conversation.id),
                                            action_fingerprint=_fp,
                                            tool_name=name,
                                        )
                                        _permitted = bool(_tok) and await _tokens.consume_action_token(
                                            _tok,
                                            action_fingerprint=_fp,
                                            user_id=str(self.user.id),
                                            conversation_id=str(self._conversation.id),
                                            tenant_id=_tenant(),
                                            tool_name=name,
                                        )
                                        if not _permitted:
                                            _deny_reason = (
                                                "语音委派许可校验失败；未创建任务")
                                    except Exception:
                                        _permitted = False
                                        _deny_reason = (
                                            "语音委派许可校验失败；未创建任务")
                                else:
                                    _deny_reason = (
                                        "缺少真实用户消息引用；语音轮不得创建委派")
                            if not _permitted:
                                result = json.dumps({
                                    "error": "act_gate_denied",
                                    "recoverable": True,
                                    "reason": _deny_reason,
                                }, ensure_ascii=False)
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc.get("id", ""),
                                    "content": result,
                                })
                                self._history.append({
                                    "role": "tool",
                                    "tool_call_id": tc.get("id", ""),
                                    "content": result,
                                })
                                _all_tool_results.append({
                                    "tool_call_id": tc.get("id", ""),
                                    "name": name,
                                    "content": result,
                                })
                                await self._send_json({"event": "tool_result", "name": name})
                                await self._persist_turn_progress(
                                    full_reply + reply_text,
                                    json.dumps(_all_tool_calls, ensure_ascii=False),
                                    _build_voice_tool_results(_all_tool_results),
                                )
                                continue
                    # Voice mode: the user explicitly asked via voice, so
                    # auto-grant write permissions (e.g. notes create_note).
                    # There is no interactive permission dialog in voice UI.
                    args["_permission_granted"] = True
                    await self._send_json({"event": "tool_call", "name": name})
                    # Persist at DISPATCH time too: if the session dies while
                    # this tool is still running (WS close cancels the loop),
                    # the record still shows the tool was started.
                    await self._persist_turn_progress(
                        full_reply + reply_text,
                        json.dumps(_all_tool_calls, ensure_ascii=False),
                        _build_voice_tool_results(_all_tool_results) if _all_tool_results else None,
                    )
                    result = await registry.dispatch(
                        name, args, db=self.db, user=self.user,
                        conversation=self._conversation, assistant=None,
                        workspace_path=self._workspace_path or "",
                        _source_message_id=self._last_persisted_user_message_id,
                        # Schema filtering alone is not a security boundary: a
                        # stale/hallucinated name must fail dispatch too.
                        allowed_tools=_voice_tool_names(str(getattr(self.user, "id", "") or "") or None),
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result[:6000],
                    })
                    self._history.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result[:1500],
                    })
                    _all_tool_results.append({
                        "tool_call_id": tc.get("id", ""),
                        "name": name,
                        "content": result[:6000],
                    })
                    if name == "delegate_agent_task":
                        try:
                            _submitted = json.loads(result)
                        except (TypeError, json.JSONDecodeError):
                            _submitted = {}
                        if _submitted.get("task_id") and not _submitted.get("error"):
                            await self._send_json({
                                "event": "delegation_submitted",
                                "task_id": _submitted.get("task_id"),
                                "execution_conversation_id": _submitted.get(
                                    "execution_conversation_id"),
                                "assistant_name": _submitted.get("assistant_name"),
                                "status": _submitted.get("status"),
                                "reasoning_policy": _submitted.get("reasoning_policy"),
                                "requested_reasoning_effort": _submitted.get(
                                    "requested_reasoning_effort"),
                                "deduplicated": bool(_submitted.get("deduplicated")),
                            })
                    if name == "open_agent_task_result":
                        try:
                            _opened = json.loads(result)
                        except (TypeError, json.JSONDecodeError):
                            _opened = {}
                        if _opened.get("execution_conversation_id") and not _opened.get("error"):
                            await self._send_json({
                                "event": "open_task_result",
                                "task_id": _opened.get("task_id"),
                                "execution_conversation_id": _opened.get(
                                    "execution_conversation_id"),
                                "assistant_id": _opened.get("assistant_id"),
                                "status": _opened.get("status"),
                            })
                    await self._send_json({"event": "tool_result", "name": name})
                    # Incremental persistence: upsert the assistant message
                    # after EVERY tool result, so exiting voice mode mid-loop
                    # still leaves the full tool trace (incl. background_task
                    # submissions with their task_id) in the session record.
                    await self._persist_turn_progress(
                        full_reply + reply_text,
                        json.dumps(_all_tool_calls, ensure_ascii=False),
                        _build_voice_tool_results(_all_tool_results),
                    )
                if (
                    any(e["truncated"] for e in tool_plan)
                    and truncation_retries < truncation_retry_limit
                ):
                    new_budget = _escalated_voice_max_tokens(
                        turn_max_tokens, self.config.voice_max_tokens_ceiling
                    )
                    if new_budget > (turn_max_tokens or 0):
                        truncation_retries += 1
                        turn_max_tokens = new_budget
                        logger.warning(
                            "voice truncated tool args — output budget raised to %d (retry %d/%d)",
                            turn_max_tokens, truncation_retries, truncation_retry_limit,
                        )
                full_reply += reply_text
                # Keep tools available for multi-step tasks (e.g. list_notebooks
                # then create_note). The max_tool_rounds bound prevents runaway
                # loops. Previously tools=None here forced the LLM to summarize
                # after a single round, making multi-step tasks impossible.
                continue

            full_reply += reply_text
            break

        if preempted_tool_preamble:
            await self._park_preempted_tool_preamble()
            self._turn_reply_text = ""
            self._interrupt.set()
            self._drain_tts_queue()
            await self._mark_speaking_end()
            await self._send_json({
                "event": "generation_cancelled",
                "reason": "tool_summary_pending",
                "input_seq": self._generation_input_seq,
            })
            return

        if self._should_suppress_stale_low_value(full_reply, user_turn) and not _all_tool_results:
            self._interrupt.set()
            logger.info(
                "voice stale low-value reply suppressed: input_seq=%d latest=%d",
                self._generation_input_seq,
                self._latest_input_seq,
            )
            self._turn_reply_text = ""
            self._drain_tts_queue()
            await self._mark_speaking_end()
            await self._send_json({
                "event": "generation_cancelled",
                "reason": "superseded_by_newer_user_turn",
                "input_seq": self._generation_input_seq,
            })
            return

        for segment in held_tts_segments:
            if segment.strip():
                await self._tts_queue.put({
                    "text": segment,
                    "epoch": self._turn_epoch,
                    "dedup": True,
                    "notification_id": self._current_notice_id,
                                                "input_seq": self._generation_input_seq,
                })

        if (
            full_reply.strip()
            and not generation_had_error
            and summary_note_at_start
            and self._pending_tool_summary_note == summary_note_at_start
        ):
            self._pending_tool_summary_note = ""

        if self._current_notice_id and not generation_had_error:
            self._notice_state(self._current_notice_id)["llm_succeeded"] = True

        # wait for this turn's audio to finish sending
        if full_reply.strip():
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            await self._tts_queue.put({
                "flush": fut,
                "notification_id": self._current_notice_id,
                                                "input_seq": self._generation_input_seq,
                "notification_complete": not generation_had_error,
            })
            try:
                await asyncio.wait_for(fut, timeout=120.0)
            except Exception:
                pass
        if full_reply.strip():
            self._history.append({"role": "assistant", "content": full_reply})
            self._last_answer_text = full_reply
            self._vmem_answer_epoch = self._turn_epoch
            # Refresh the fun-asr-realtime dialogue context so later turns are
            # recognised against the live conversation. Keeps the ASR biased
            # toward the current topic and away from background speech.
            if self._asr:
                try:
                    await self._asr.update_context(self._history)
                except Exception as exc:
                    logger.debug("voice ASR context refresh failed: %s", exc)
            # Persist with tool_calls/tool_results so the session record
            # preserves the full tool interaction history.  Convert the
            # flat voice format into the rich ToolResultsData display format
            # (agent_steps etc.) so the frontend MessageBubble can render
            # collapsible tool-call blocks — same as Agent mode.
            _tc_json = json.dumps(_all_tool_calls, ensure_ascii=False) if _all_tool_calls else None
            _tr_json = _build_voice_tool_results(_all_tool_results) if _all_tool_results else None
            if self._turn_msg_id:
                # A provisional row exists from the tool loop — finalize it.
                await self._persist_turn_progress(full_reply, _tc_json, _tr_json, final=True)
            else:
                await self._persist_message("assistant", full_reply, tool_calls=_tc_json, tool_results=_tr_json)
            await self._send_json({"event": "assistant_text", "text": strip_voice_tags(full_reply), "done": True})
