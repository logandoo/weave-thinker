# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent loop orchestrator (C-01 move-only, 2026-10-19).

``_run_loop`` was moved byte-for-byte out of ``agent_loop.py`` so the
orchestrator has its own module boundary and an explicit dependency list.
Module-level names owned by ``agent_loop.py`` are synced at call time (not
copied at import time), keeping monkeypatches of ``agent_loop.<name>`` alive.
The next wave can decompose the body into pure helpers without touching the
1.9k-line host file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

_AGENT_GLOBALS = (   'AgentLoopState',
    'Any',
    'AsyncIterator',
    'Dict',
    'List',
    'Optional',
    'ToolCallResult',
    'ToolStalledError',
    '_AUDIT_SALVAGE_FAILURE_TEXT',
    '_CANARY_RE',
    '_PARALLEL_SAFE_TOOLS',
    '_TOOL_CALL_RE',
    '_TOOL_CALL_XML_RE',
    '_TOOL_INVOKE_RE',
    '_TOOL_RESULT_RE',
    '_audit_family_stalled',
    '_audit_problem_family',
    '_audit_reject_budget_for',
    '_consume_low_confidence_note',
    '_context_info_from_usage',
    '_draft_ships_as_final',
    '_emit_draft_rejected',
    '_extract_dsml_tool_calls',
    '_filter_force_stage_calls',
    '_inject_directive',
    '_is_full_rewrite',
    '_npg_soft_reject_no_progress',
    '_prune_guardrail_pairs',
    '_re',
    '_rejected_append',
    '_resolve_context_length',
    '_search_class_tool_used',
    '_search_demand_directive',
    '_should_promote_answer_over_tools',
    '_stash_rejected_draft',
    '_strip_dsml_all',
    '_strip_dsml_streaming',
    '_strip_leading_orphan_punct',
    '_tool_demand_directive',
    'asyncio',
    'config',
    'decide_tool_dispatch',
    'enforce_turn_budget',
    'jittered_backoff',
    'json',
    'logger',
    'plan_tool_calls_for_round',
    'repair_tool_call_arguments',
    'sanitize_messages_surrogates')


def _sync_agent_globals() -> None:
    from app.services import agent_loop as _agent_loop

    g = globals()
    for name in _AGENT_GLOBALS:
        if hasattr(_agent_loop, name):
            g[name] = getattr(_agent_loop, name)




def tool_result_event(call_id: str, name: str, result) -> dict:
    """SSE `tool_result` 载荷（2026-11-01 审计 F4）。

    工具"成功返回"了自报失败的结果（`timed_out: true`）时，`error` 必须为真，
    否则 UI 卡片显示「✓ 完成」、`tool_trace` 记 error=0（评审 I5 要求可测）。
    """
    return {
        "tool_result": {
            "call_id": call_id,
            "name": name,
            "result": result.result,
            "error": bool(result.error or getattr(result, "payload_error", False)),
        }
    }


class _LoopContinueSignal(BaseException):
    pass


class _LoopBreakSignal(BaseException):
    pass


class _LoopReturnSignal(BaseException):
    pass


@dataclass
class _LoopResult:
    _iter_extra_body: Any = None
    _iter_kwargs: Any = None
    _iter_started_at: Any = None
    _iter_timeout: Any = None
    _measured: Any = None
    _ratio: Any = None
    _reasoning_char_cap: Any = None
    _reasoning_truncated: Any = None
    after: Any = None
    assistant_content: Any = None
    assistant_raw_content: Any = None
    before: Any = None
    content_text: Any = None
    delay: Any = None
    elapsed: Any = None
    event: Any = None
    event_data: Any = None
    event_type: Any = None
    fallback_msg: Any = None
    finish_reason: Any = None
    llm_stream: Any = None
    reasoning_content: Any = None
    recovery_action: Any = None
    retry_after: Any = None
    should_retry: Any = None
    tool_calls_collected: Any = None
    _adv_ev: Any = None
    _adv_events: Any = None
    _adv_text: Any = None
    _args_error: Any = None
    _audit_family: Any = None
    _audit_reject_budget: Any = None
    _audit_target: Any = None
    _base_text: Any = None
    _budget_spent: Any = None
    _c: Any = None
    _c_after: Any = None
    _c_before: Any = None
    _canary_retry: Any = None
    _conv_key: Any = None
    _cov_note: Any = None
    _cp_assistant: Any = None
    _ctx_len: Any = None
    _decision: Any = None
    _digest_ev: Any = None
    _digest_out: Any = None
    _disabled: Any = None
    _dispatch_plan: Any = None
    _dm_content: Any = None
    _dm_should_continue: Any = None
    _ev: Any = None
    _eval_content: Any = None
    _family_stall: Any = None
    _final_content: Any = None
    _g_blocked: Any = None
    _g_ev: Any = None
    _g_events: Any = None
    _guarded: Any = None
    _halt_message: Any = None
    _halted: Any = None
    _hard: Any = None
    _hit: Any = None
    _is_repeating: Any = None
    _last_user_msg: Any = None
    _lc_note: Any = None
    _log_miss: Any = None
    _marker: Any = None
    _min_ratio: Any = None
    _misses: Any = None
    _need_synthesis: Any = None
    _npg_no_progress: Any = None
    _parallel_cancelled: Any = None
    _prev_stash: Any = None
    _r: Any = None
    _res: Any = None
    _reset_ev: Any = None
    _restart: Any = None
    _results_by_id: Any = None
    _safe_tool_calls: Any = None
    _salvage_ev: Any = None
    _salvage_parts: Any = None
    _search_only: Any = None
    _shipped_guidance: Any = None
    _shipped_text: Any = None
    _ships_as_final: Any = None
    _soft_limit: Any = None
    _stitched_audit: Any = None
    _synth_guidance: Any = None
    _synth_shipped_guidance: Any = None
    _threshold: Any = None
    _tool_summary: Any = None
    _tool_summary_parts: Any = None
    _tr: Any = None
    _truncated_result: Any = None
    _turn_text: Any = None
    _turn_tr: Any = None
    _turns: Any = None
    _wave: Any = None
    _wave_ids: Any = None
    _waves: Any = None
    assistant_msg: Any = None
    can_parallel: Any = None
    compressed: Any = None
    coros: Any = None
    decision: Any = None
    dm: Any = None
    done: Any = None
    guidance: Any = None
    has_budget_refund_tool: Any = None
    i: Any = None
    interrupted_result: Any = None
    matched: Any = None
    msg: Any = None
    ordered_tool_messages: Any = None
    par_task: Any = None
    r: Any = None
    raw_results: Any = None
    rep_prompt: Any = None
    repaired: Any = None
    result: Any = None
    results: Any = None
    results_digested: Any = None
    retry_prompt: Any = None
    retry_result: Any = None
    tc: Any = None
    tc_id: Any = None
    tm: Any = None
    tool_args: Any = None
    tool_messages: Any = None
    tool_name: Any = None
    tool_results_map: Any = None
    tool_task: Any = None
    valid_tool_calls: Any = None
    wave_coros: Any = None

class AgentLoopOrchestratorMixin:
    async def _stream_iteration(self, state, _loop_result, **kw):
        _lg_conv_id = kw.get('_lg_conv_id')
        _use_thinking = kw.get('_use_thinking')
        active_tool_schemas = kw.get('active_tool_schemas')
        _iter_extra_body = _loop_result._iter_extra_body
        _iter_kwargs = _loop_result._iter_kwargs
        _iter_started_at = _loop_result._iter_started_at
        _iter_timeout = _loop_result._iter_timeout
        _measured = _loop_result._measured
        _ratio = _loop_result._ratio
        _reasoning_char_cap = _loop_result._reasoning_char_cap
        _reasoning_truncated = _loop_result._reasoning_truncated
        after = _loop_result.after
        assistant_content = _loop_result.assistant_content
        assistant_raw_content = _loop_result.assistant_raw_content
        before = _loop_result.before
        content_text = _loop_result.content_text
        delay = _loop_result.delay
        elapsed = _loop_result.elapsed
        event = _loop_result.event
        event_data = _loop_result.event_data
        event_type = _loop_result.event_type
        fallback_msg = _loop_result.fallback_msg
        finish_reason = _loop_result.finish_reason
        llm_stream = _loop_result.llm_stream
        reasoning_content = _loop_result.reasoning_content
        recovery_action = _loop_result.recovery_action
        retry_after = _loop_result.retry_after
        should_retry = _loop_result.should_retry
        tool_calls_collected = _loop_result.tool_calls_collected
        try:
            try:
                # PHASE 3: route iteration calls through iteration_llm with
                # its own provider_type so a separate (cheaper, no-think)
                # client can serve the loop while _final_thinking still uses
                # the main reasoner.
                _iter_extra_body = (
                    self.iteration_extra_body_override
                    if self.iteration_extra_body_override is not None
                    else self._thinking_extra_body(
                        self.iteration_provider_type, _use_thinking, llm=self.iteration_llm, thinking_budget=self.thinking_budget, preserve_thinking=self.preserve_thinking
                    )
                )
                # A4.9 I1: sampling sets apply only to the qwen3.8_vllm
                # iteration provider AND only when the operator did not take
                # explicit extra_body control (iteration_extra_body_override
                # bypasses build_thinking_extra_body — A4.9 I2).
                _iter_kwargs = {}
                if self.iteration_extra_body_override is None:
                    _iter_kwargs = self._sampling_kwargs(_use_thinking, self.iteration_provider_type)
                if not _iter_kwargs:
                    _iter_kwargs = {"temperature": config.default_temperature}
                llm_stream = self.iteration_llm.stream_chat_structured(
                    state.messages,
                    tools=active_tool_schemas,
                    prompt_id="agent.system",
                    extra_body=_iter_extra_body,
                    **_iter_kwargs,
                )
                # Governor v2 · A1 StreamWatch：迭代流经 watch_stream 包裹——
                # 观察移到消费侧 + cancel 每 chunk 检查（设计 §4.2-A1）。
                from app.services.loop_governor_service import watch_stream as _lgov_watch
                llm_stream = _lgov_watch(
                    llm_stream,
                    conv_id=str(_lg_conv_id or ""),
                    msg_id=f"iter-{state.iterations}",
                )
                # PHASE 4: track cumulative reasoning chars and per-iteration
                # wall-clock so a thinking-by-default sub-task model can't
                # blow up the context window or hang the round.
                _iter_started_at = asyncio.get_event_loop().time()
                _reasoning_char_cap = config.agent_subtask_reasoning_char_cap
                _iter_timeout = config.agent_subtask_iteration_timeout
                _reasoning_truncated = False
                async for event in llm_stream:
                    event_type = event["type"]
                    event_data = event["data"]

                    # PHASE 4: enforce per-iteration timeout AND conversation
                    # inactivity timeout. The inactivity check catches the
                    # case where the agent is stuck in a loop producing no
                    # actual content (e.g. deathmatch judge keeps saying
                    # "continue" but the model outputs nothing).
                    # BOTH checks MUST run on heartbeat wake-ups too: a
                    # stalled upstream stream yields no real chunks (conv
                    # efaf8f9c 2026-08-21 hung for 60+ min, conversation slot
                    # reserved forever), so without evaluating on heartbeats
                    # these guards are unreachable dead code.
                    if _iter_timeout and (
                        asyncio.get_event_loop().time() - _iter_started_at
                    ) > _iter_timeout:
                        logger.warning(
                            "AgentLoop iteration %d: timeout after %.1fs, aborting stream",
                            state.iterations, _iter_timeout,
                        )
                        yield {
                            "agent_step": {
                                "name": "iteration_timeout",
                                "title": "迭代超时",
                                "content": f"子任务超过 {_iter_timeout:.0f}s 未完成，已中止本轮以继续后续步骤。",
                                "step_type": "recovery",
                            }
                        }
                        break

                    # Inactivity check: if the streaming loop produces zero
                    # events for too long (model connection hung), bail out.
                    if self._check_inactivity(state):
                        elapsed = asyncio.get_event_loop().time() - state.last_activity_at
                        logger.warning(
                            "AgentLoop inactivity in stream: no activity for %.1fs (iteration=%d)",
                            elapsed, state.iterations,
                        )
                        yield {
                            "agent_step": {
                                "name": "conversation_timeout",
                                "title": "对话无响应",
                                "content": (
                                    f"对话已卡住超过 {state.inactivity_timeout_seconds:.0f} 秒"
                                    f"（无新内容产出 {elapsed:.0f} 秒），系统自动终止。"
                                ),
                                "step_type": "recovery",
                            }
                        }
                        break

                    if event_type == "reasoning":
                        reasoning_content += event_data
                        self._mark_activity(state)
                        # vibeweaver loop-guard 观察通道（D1.1）：reasoning 增量单独
                        # 发布（channel=reasoning）——仅 content 观测不住 reasoning
                        # 独有的墙（hermes #97630 形态：reasoning 里 "!!!!" 无 content）。
                        try:
                            from app.services.agent_event_bus import (
                                AgentEvent as _AebEvent,
                                agent_event_bus as _aeb,
                            )
                            if event_data:
                                _aeb.publish_soon(_AebEvent(
                                    type="message.delta",
                                    session_id=_lg_conv_id,
                                    data={
                                        "delta": event_data,
                                        "channel": "reasoning",
                                        "msg_id": f"iter-{state.iterations}",
                                    },
                                ))
                        except Exception:
                            pass
                        # PHASE 4: cap runaway reasoning streams from thinking-
                        # by-default sub-task models. Once we hit the cap we
                        # stop accepting more reasoning chars and break out so
                        # the outer loop moves on. The model's tool_calls /
                        # content collected so far are preserved.
                        # LIVE-THINKING: the cap is bypassed — reasoning is now
                        # the user-visible thinking stream, and cutting it
                        # mid-answer would truncate the final response itself.
                        if (
                            _reasoning_char_cap
                            and not _use_thinking
                            and len(reasoning_content) >= _reasoning_char_cap
                            and not _reasoning_truncated
                        ):
                            _reasoning_truncated = True
                            logger.warning(
                                "AgentLoop iteration %d: reasoning exceeded cap (%d chars), aborting stream",
                                state.iterations, _reasoning_char_cap,
                            )
                            yield {
                                "agent_step": {
                                    "name": "reasoning_cap",
                                    "title": "思维链截断",
                                    "content": (
                                        f"子任务思维链已达上限 {_reasoning_char_cap} 字符，"
                                        "为避免上下文溢出已提前结束本轮思考。"
                                    ),
                                    "step_type": "recovery",
                                }
                            }
                            break
                        # PHASE 1C: middle-iteration reasoning is not the user-visible
                        # "thinking". The final synthesis pass (_final_thinking) is the
                        # only place that surfaces reasoning to the user. Drop here
                        # both to avoid mixing iteration noise with final reasoning
                        # and to prevent forced-reasoning models from leaking long CoT
                        # into the UI before the final phase. When reasoning is
                        # disabled altogether, drop unconditionally.
                        # LIVE-THINKING: stream it — the thinking panel fills live
                        # from the very first iteration (opencode-style TTFT).
                        if not self.enable_reasoning:
                            continue
                        if _use_thinking:
                            yield {"reasoning_content": event_data, "phase": "final"}
                            continue
                        # Even when enabled: middle-iteration reasoning is dropped;
                        # _final_thinking provides the visible reasoning stream.
                        continue

                    elif event_type == "content":
                        content_text = event_data
                        if '<think' in content_text:
                            content_text = _re.sub(r'<think[^>]*>.*?</think\s*>', '', content_text, flags=_re.DOTALL)
                            if '<think' in content_text:
                                content_text = _re.sub(r'<think[^>]*>.*', '', content_text, flags=_re.DOTALL)
                        # Strip tool_call XML from content — some providers leak
                        # tool call markup inline instead of using delta.tool_calls.
                        content_text = _TOOL_CALL_RE.sub('', content_text)
                        content_text = _TOOL_INVOKE_RE.sub('', content_text)
                        content_text = _TOOL_CALL_XML_RE.sub('', content_text)
                        content_text = _TOOL_RESULT_RE.sub('', content_text)
                        # Stateful DSML stripping: handles blocks that are split
                        # across multiple stream chunks.  The previous approach
                        # only removed *complete* blocks per-chunk, so partial
                        # DSML tags leaked into the user-visible stream and were
                        # subsequently persisted to the database.
                        content_text, state.dsml_active, state.dsml_tail = _strip_dsml_streaming(
                            content_text, state.dsml_active, state.dsml_tail
                        )
                        assistant_raw_content += event_data
                        if content_text:
                            assistant_content += content_text
                            # vibeweaver loop-guard 观察通道（A4）：文本增量
                            # 发布（fire-and-forget；观察者 fail-open）。
                            try:
                                from app.services.agent_event_bus import (
                                    AgentEvent as _AebEvent,
                                    agent_event_bus as _aeb,
                                )
                                _aeb.publish_soon(_AebEvent(
                                    type="message.delta",
                                    session_id=_lg_conv_id,
                                    data={
                                        "delta": content_text,
                                        "channel": "content",
                                        "msg_id": f"iter-{state.iterations}",
                                    },
                                ))
                            except Exception:
                                pass
                            # When reasoning is enabled, suppress all middle-iteration
                            # content; the single final answer is produced by
                            # _final_thinking after all tools finish (or directly when
                            # no tools are called). When reasoning is disabled, yield
                            # content normally.
                            # EXCEPTION: In deathmatch mode, always yield intermediate
                            # content so the user can see progress and the judge has
                            # fresh context to evaluate.
                            # LIVE-THINKING: content streams live in every iteration;
                            # the relay resets its accumulator at each tool-call
                            # boundary, so pre-tool prose never pollutes the
                            # persisted answer (same guarantee as before).
                            # EMPTY-ANSWER RETRY (conv 517140ca): the retry runs
                            # with thinking disabled (retry_thinking_off) but
                            # enable_reasoning stays True — without this clause
                            # the retried content would be suppressed AND
                            # _final_thinking would be skipped (live thinking
                            # still "on" per _live_thinking_enabled), so the
                            # recovered answer would never reach the user.
                            if (
                                not self.enable_reasoning
                                or self._skip_guardrails()
                                or _use_thinking
                                or state.retry_thinking_off
                                # LIVE 模式修正轮（thinking 关闭）直接流式出稿；
                                # 非 live 模式的答案仍由 _final_thinking 合成
                                # pass 产出（修正轮合成 pass 已按同一标志关思考）。
                                or (state.revision_thinking_off and self._live_thinking_enabled())
                            ):
                                yield {"content": content_text}
                                self._mark_activity(state)

                    elif event_type == "usage":
                        # P4 真值徽章：主链路实测 prompt_tokens → 刷新前端
                        state.last_usage = event_data
                        _measured = int(event_data.get("prompt_tokens") or 0)
                        if _measured > 0:
                            state.measured_prompt_tokens = _measured
                            if state.last_request_estimate > 0:
                                _ratio = _measured / float(state.last_request_estimate)
                                # Clamp: a provider-reported 0/tiny value must
                                # not disable the guard; a huge ratio must not
                                # make it fire immediately.
                                self._estimate_calibration = max(0.05, min(2.0, _ratio))
                                logger.info(
                                    "Context calibration: measured=%d rough=%d ratio=%.3f",
                                    _measured, state.last_request_estimate,
                                    self._estimate_calibration,
                                )
                        logger.info(
                            "LLM usage (measured): prompt=%s cache_hit=%s completion=%s",
                            event_data.get("prompt_tokens"),
                            event_data.get("prompt_cache_hit_tokens"),
                            event_data.get("completion_tokens"),
                        )
                        yield {"context_info": _context_info_from_usage(
                            event_data, _resolve_context_length(self.llm))}
                    elif event_type == "tool_calls":
                        tool_calls_collected = event_data
                        self._mark_activity(state)

                    elif event_type == "error":
                        should_retry, recovery_action, retry_after = await self._classify_llm_error(event_data)
                        if should_retry and (await state.budget.get_remaining()) > 0:
                            yield {
                                "agent_step": {
                                    "name": "error_recovery",
                                    "title": "LLM 错误恢复",
                                    "content": f"LLM 错误: {str(event_data)[:200]}, 策略: {recovery_action}",
                                    "step_type": "recovery",
                                }
                            }
                            if recovery_action == "compress_context" and self._compressor:
                                from app.services.context_compressor import estimate_request_tokens_rough
                                before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                state.messages = await self._compressor.compress_async(state.messages)
                                after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                state.last_request_estimate = after
                                logger.info("Emergency compression %d->%d tokens after LLM error", before, after)
                                yield {"context_info": {
                                    "tokens": after,
                                    "context_length": self._compressor.context_length,
                                    "compressed": True,
                                }}
                            elif recovery_action == "fallback_provider":
                                fallback_msg = "主供应商不可用，正在切换备用供应商..."
                                if self._try_fallback_provider():
                                    fallback_msg += " 已切换到备用供应商。"
                                else:
                                    fallback_msg += " 未找到可用备用供应商。"
                                yield {
                                    "agent_step": {
                                        "name": "provider_fallback",
                                        "title": "尝试备用供应商",
                                        "content": fallback_msg,
                                        "step_type": "recovery",
                                    }
                                }
                            delay = retry_after if retry_after is not None else 2.0
                            await asyncio.sleep(delay)
                            # 输出单元契约（2026-10-14）：可恢复重试=代际边界——
                            # 失败残稿不得与重试稿并存（双倍输出源）；keep_stitch
                            # 保护截断续写链的 head（残稿只是本次续写尾巴）。
                            yield {"audit_reset": True, "keep_stitch": True}
                            continue
                        yield {"error": event_data}
                        raise _LoopReturnSignal()

                    elif event_type == "done":
                        if isinstance(event_data, dict):
                            finish_reason = event_data.get("finish_reason")
                        state.finish_reason = finish_reason

            except asyncio.TimeoutError:
                logger.warning("Main LLM stream timed out")
                yield {"error": "LLM响应超时, 请简化问题后重试。"}
                if self.deathmatch_manager and self.deathmatch_manager.is_active:
                    yield {"content": "死磕模式: LLM 响应超时，请简化问题后重试。"}
                    yield {"done": True}
                raise _LoopReturnSignal()
            except Exception as e:
                should_retry, recovery_action, retry_after = await self._classify_llm_error(e)
                if should_retry and (await state.budget.get_remaining()) > 0:
                    logger.warning("LLM stream error (recoverable): %s -- %s", e, recovery_action)
                    yield {
                        "agent_step": {
                            "name": "error_recovery",
                            "title": "LLM 错误恢复",
                            "content": f"LLM 错误: {str(e)[:200]}, 策略: {recovery_action}",
                            "step_type": "recovery",
                        }
                    }
                    if recovery_action == "compress_context" and self._compressor:
                        from app.services.context_compressor import estimate_request_tokens_rough
                        before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                        state.messages = await self._compressor.compress_async(state.messages)
                        after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                        state.last_request_estimate = after
                        logger.info("Emergency compression %d->%d tokens after LLM error", before, after)
                        yield {"context_info": {
                            "tokens": after,
                            "context_length": self._compressor.context_length,
                            "compressed": True,
                        }}
                    elif recovery_action == "fallback_provider":
                        yield {
                            "agent_step": {
                                "name": "provider_fallback",
                                "title": "尝试备用供应商",
                                "content": "主供应商不可用，正在切换备用供应商...",
                                "step_type": "recovery",
                            }
                        }
                    delay = retry_after if retry_after is not None else jittered_backoff(state.iterations)
                    await asyncio.sleep(delay)
                    # 输出单元契约（2026-10-14）：异常重试同代际边界口径。
                    yield {"audit_reset": True, "keep_stitch": True}
                    raise _LoopContinueSignal()
                yield {"error": str(e)}
                if self.deathmatch_manager and self.deathmatch_manager.is_active:
                    yield {"content": f"死磕模式: LLM 调用失败，请检查 API 配置后重试。错误: {str(e)[:200]}"}
                    yield {"done": True}
                raise _LoopReturnSignal()
        finally:
            _loop_result._iter_extra_body = _iter_extra_body
            _loop_result._iter_kwargs = _iter_kwargs
            _loop_result._iter_started_at = _iter_started_at
            _loop_result._iter_timeout = _iter_timeout
            _loop_result._measured = _measured
            _loop_result._ratio = _ratio
            _loop_result._reasoning_char_cap = _reasoning_char_cap
            _loop_result._reasoning_truncated = _reasoning_truncated
            _loop_result.after = after
            _loop_result.assistant_content = assistant_content
            _loop_result.assistant_raw_content = assistant_raw_content
            _loop_result.before = before
            _loop_result.content_text = content_text
            _loop_result.delay = delay
            _loop_result.elapsed = elapsed
            _loop_result.event = event
            _loop_result.event_data = event_data
            _loop_result.event_type = event_type
            _loop_result.fallback_msg = fallback_msg
            _loop_result.finish_reason = finish_reason
            _loop_result.llm_stream = llm_stream
            _loop_result.reasoning_content = reasoning_content
            _loop_result.recovery_action = recovery_action
            _loop_result.retry_after = retry_after
            _loop_result.should_retry = should_retry
            _loop_result.tool_calls_collected = tool_calls_collected

    async def _execute_tool_block(self, state, _loop_result, **kw):
        _name = kw.get('_name')
        assistant = kw.get('assistant')
        assistant_content = kw.get('assistant_content')
        conversation = kw.get('conversation')
        reasoning_content = kw.get('reasoning_content')
        session_factory = kw.get('session_factory')
        user = kw.get('user')
        _args_error = _loop_result._args_error
        _c = _loop_result._c
        _cp_assistant = _loop_result._cp_assistant
        _decision = _loop_result._decision
        _digest_ev = _loop_result._digest_ev
        _digest_out = _loop_result._digest_out
        _dispatch_plan = _loop_result._dispatch_plan
        _dm_should_continue = _loop_result._dm_should_continue
        _ev = _loop_result._ev
        _eval_content = _loop_result._eval_content
        _guarded = _loop_result._guarded
        _halt_message = _loop_result._halt_message
        _halted = _loop_result._halted
        _hard = _loop_result._hard
        _parallel_cancelled = _loop_result._parallel_cancelled
        _r = _loop_result._r
        _res = _loop_result._res
        _results_by_id = _loop_result._results_by_id
        _safe_tool_calls = _loop_result._safe_tool_calls
        _tool_summary = _loop_result._tool_summary
        _tool_summary_parts = _loop_result._tool_summary_parts
        _tr = _loop_result._tr
        _truncated_result = _loop_result._truncated_result
        _turn_text = _loop_result._turn_text
        _turn_tr = _loop_result._turn_tr
        _wave = _loop_result._wave
        _wave_ids = _loop_result._wave_ids
        _waves = _loop_result._waves
        assistant_msg = _loop_result.assistant_msg
        can_parallel = _loop_result.can_parallel
        coros = _loop_result.coros
        decision = _loop_result.decision
        dm = _loop_result.dm
        done = _loop_result.done
        has_budget_refund_tool = _loop_result.has_budget_refund_tool
        i = _loop_result.i
        interrupted_result = _loop_result.interrupted_result
        matched = _loop_result.matched
        ordered_tool_messages = _loop_result.ordered_tool_messages
        par_task = _loop_result.par_task
        r = _loop_result.r
        raw_results = _loop_result.raw_results
        repaired = _loop_result.repaired
        result = _loop_result.result
        results = _loop_result.results
        results_digested = _loop_result.results_digested
        retry_result = _loop_result.retry_result
        tc = _loop_result.tc
        tc_id = _loop_result.tc_id
        tm = _loop_result.tm
        tool_args = _loop_result.tool_args
        tool_calls_collected = _loop_result.tool_calls_collected
        tool_messages = _loop_result.tool_messages
        tool_name = _loop_result.tool_name
        tool_results_map = _loop_result.tool_results_map
        tool_task = _loop_result.tool_task
        valid_tool_calls = _loop_result.valid_tool_calls
        wave_coros = _loop_result.wave_coros
        try:
            if tool_calls_collected:
                state.consecutive_tool_iterations += 1
                if state.force_final_answer:
                    state.force_stage_rounds += 1

                has_budget_refund_tool = any(
                    tc.get("function", {}).get("name") in {"execute_code",}
                    for tc in tool_calls_collected
                )

                _gov_hit = ""
                if not self._skip_guardrails():
                    from app.services.loop_governor_service import (
                        turn_hard_limits_hit as _lgov_hit,
                    )
                    # 墙钟在迭代边界判（T10）；此处判总迭代/工具调用硬顶。
                    _gov_hit = _lgov_hit(state, wall_seconds_eff=0.0, now=0.0)
                if (
                    state.consecutive_tool_iterations >= self.max_consecutive_iterations
                    or _gov_hit in ("total_iterations", "tool_calls")
                ) and not self._skip_guardrails():
                    logger.warning(
                        "Forcing final answer after %d consecutive tool iterations (gov_hit=%s)",
                        state.consecutive_tool_iterations, _gov_hit,
                    )
                    yield {
                        "agent_step": {
                            "name": "tool_loop_guard",
                            "title": "工具调用限制",
                            "content": f"已连续调用工具 {state.consecutive_tool_iterations} 轮, 正在基于已有结果生成最终回答。",
                            "step_type": "tool",
                        }
                    }
                    tool_calls_collected = []
                    state.force_final_answer = True
                    state.consecutive_tool_iterations = 0
                    if assistant_content:
                        state.messages.append({"role": "assistant", "content": assistant_content, "reasoning_content": reasoning_content or ""})
                    else:
                        state.messages.append({"role": "assistant", "content": "[工具调用中...]", "reasoning_content": ""})
                    _inject_directive(
                        state,
                        "请基于以上工具调用结果,直接给出完整回答。"
                        "如果任务需要生成文件(Excel、PPT、Word等),请使用 execute_code 工具生成。"
                        "注意Markdown格式：标题前留空行，#后加空格，表格行之间必须有换行。",
                    )
                    # 输出单元契约（2026-10-14 I2）：强制终答=工具轮边界——
                    # 发 iteration_done(tool_calls>0) 让 relay 把本迭代预工具
                    # 散文停车进时间线（与「最大搜索次数」边界同口径），否则
                    # 散文拼进强制终答（过程输出并入终局原病灶）。
                    yield {
                        "iteration_done": {
                            "iteration": state.iterations,
                            "tool_calls": 1,
                        }
                    }
                    raise _LoopContinueSignal()
                else:
                    valid_tool_calls = []
                    for tc in tool_calls_collected:
                        if tc.get("function", {}).get("name") and tc.get("id"):
                            if "type" not in tc:
                                tc["type"] = "function"
                            valid_tool_calls.append(tc)

                    assistant_msg = {
                        "role": "assistant",
                        # When tool calls are present, the content generated
                        # before the tool calls is a transient pre-tool utterance.
                        # Persist the message without content so the final answer
                        # produced after tool results is authoritative and not
                        # duplicated by the model's own pre-tool prose.
                        "content": None if valid_tool_calls else (assistant_content or None),
                        # DeepSeek API requires reasoning_content to always be present
                        # in assistant messages when thinking mode is enabled in the
                        # conversation. Use empty string when no reasoning was generated.
                        "reasoning_content": reasoning_content or "",
                    }

                    if valid_tool_calls:
                        # 截断守卫 + 供应商安全归一化（顺序关键，A4.9 C1）：
                        # 必须对**原始**参数先做派发计划（截断 → 不执行 + 回
                        # 可行动错误），再生成给上游的合法 JSON 副本。绝不就地
                        # 改写 valid_tool_calls——旧顺序（先 repair→"{}"）会让
                        # 守卫永远看不到截断载荷（conv fcb2e60a 同类复发）。
                        # 上游约束（2026-08-21 dflash2）：outgoing tool_calls
                        # 参数必须是合法 JSON（修复或 {}），否则 vLLM 400。
                        _dispatch_plan, _safe_tool_calls = plan_tool_calls_for_round(valid_tool_calls)
                        assistant_msg["tool_calls"] = _safe_tool_calls
                        # 派发前落盘在飞迭代，存
                        # **原始** tool_calls（含截断参数原样）——恢复时 preflight 对
                        # 不可解析参数合成截断反馈、绝不以 {} 派发（同 R1 C1 红线）。
                        _cp_assistant = dict(assistant_msg)
                        _cp_assistant["tool_calls"] = valid_tool_calls
                        await self._maybe_checkpoint(
                            state, extra_messages=list(state.messages) + [_cp_assistant])

                        can_parallel = (
                            config.agent_tool_loop_parallel_tool_calls
                            and len(valid_tool_calls) > 1
                            and all(
                                tc["function"]["name"] in _PARALLEL_SAFE_TOOLS
                                for tc in valid_tool_calls
                            )
                        )

                        if can_parallel:
                            _parallel_cancelled = False
                            if state.cancelled:
                                for tc in valid_tool_calls:
                                    tool_name = tc["function"]["name"]
                                    interrupted_result = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments={},
                                        result=json.dumps({"error": "Agent cancelled", "interrupted": True}, ensure_ascii=False),
                                        error=True,
                                    )
                                    state.tool_results.append(interrupted_result)
                                    state.messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "name": tool_name,
                                        "content": interrupted_result.result,
                                    })
                                _parallel_cancelled = True

                            if not _parallel_cancelled:
                                coros = []
                                for tc in valid_tool_calls:
                                    tool_name = tc["function"]["name"]
                                    # 截断守卫（conv fcb2e60a 同类）：计划在
                                    # 归一化之前由原始参数产生；不可解析参数
                                    # 绝不以 {} 派发——回可行动截断错误，不执行。
                                    tool_args, _args_error = decide_tool_dispatch(_dispatch_plan, tc)
                                    yield {
                                        "tool_call": {
                                            "call_id": tc["id"],
                                            "name": tool_name,
                                            "arguments": tool_args if tool_args is not None else {},
                                        }
                                    }
                                    if _args_error is not None:
                                        async def _truncated_result(_tc=tc, _name=tool_name, _msg=_args_error):
                                            return ToolCallResult(
                                                call_id=_tc["id"],
                                                name=_name,
                                                arguments={},
                                                result=_msg,
                                                error=True,
                                            )
                                        coros.append((tc, {}, asyncio.wait_for(
                                            _truncated_result(), timeout=5.0,
                                        )))
                                        continue
                                    _turn_text, _turn_tr = self._current_turn_context(state)
                                    _guarded, _hard = self._tool_liveness_coro(tool_name, self._execute_single_tool(
                                        tc["id"], tool_name, tool_args, session_factory, user, conversation, assistant,
                                        state,
                                        current_turn_content=_turn_text,
                                        current_turn_tool_results=_turn_tr,
                                    ))
                                    coros.append((tc, tool_args, asyncio.wait_for(
                                        _guarded,
                                        timeout=_hard,
                                    )))

                                # 同路径分波——波内并行、波间
                                # 串行；结果按模型顺序回填（下游 zip(coros,…) 不变）。
                                from app.services.workspace_lock_service import (
                                    partition_write_waves as _partition_write_waves,
                                )
                                _waves = _partition_write_waves(
                                    valid_tool_calls, getattr(self, "workspace_path", None))
                                _results_by_id: Dict[str, ToolCallResult] = {}
                                for _wave in _waves:
                                    _wave_ids = {t["id"] for t in _wave}
                                    wave_coros = [c for c in coros if c[0]["id"] in _wave_ids]
                                    if not wave_coros:
                                        continue
                                    par_task = asyncio.gather(*(c[2] for c in wave_coros), return_exceptions=True)
                                    try:
                                        while not par_task.done():
                                            done, _ = await asyncio.wait({par_task}, timeout=8.0)
                                            if not done:
                                                yield {"ping": True}
                                        raw_results = par_task.result()
                                        results = []
                                        for (tc, tool_args, _), r in zip(wave_coros, raw_results):
                                            if isinstance(r, asyncio.TimeoutError):
                                                results.append(ToolCallResult(
                                                    call_id=tc["id"],
                                                    name=tc["function"]["name"],
                                                    arguments=tool_args,
                                                    result=json.dumps({"error": f"Tool '{tc['function']['name']}' timed out"}),
                                                    error=True,
                                                ))
                                            elif isinstance(r, ToolStalledError):
                                                results.append(ToolCallResult(
                                                    call_id=tc["id"],
                                                    name=tc["function"]["name"],
                                                    arguments=tool_args,
                                                    result=json.dumps({"error": str(r)}),
                                                    error=True,
                                                ))
                                            elif isinstance(r, Exception):
                                                results.append(ToolCallResult(
                                                    call_id=tc["id"],
                                                    name=tc["function"]["name"],
                                                    arguments=tool_args,
                                                    result=json.dumps({"error": f"Tool '{tc['function']['name']}' failed: {str(r)}"}),
                                                    error=True,
                                                ))
                                            else:
                                                results.append(r)
                                        for _c, _res in zip(wave_coros, results):
                                            _results_by_id[_c[0]["id"]] = _res
                                    except asyncio.CancelledError:
                                        par_task.cancel()
                                        try:
                                            await par_task
                                        except asyncio.CancelledError:
                                            pass
                                        state.memory_read_pending.clear()
                                        raise
                                results = [_results_by_id[tc["id"]] for tc, _, _ in coros]
                                # P0 R3: the parallel wave is complete; failed reads
                                # must not leave a stale in-flight reservation.
                                state.memory_read_pending.clear()

                                # Citation ledger: renumber web_search results
                                # with turn-global ids BEFORE the digest layer
                                # so the numbering the model sees (and the
                                # digest envelope embeds) is unambiguous.
                                self._apply_citation_ledger(state, results)

                                # Fix JSON arguments for parallel path (same as serial)
                                for i, (tc, tool_args, _) in enumerate(coros):
                                    if isinstance(tool_args, str):
                                        try:
                                            json.loads(tool_args)
                                        except json.JSONDecodeError:
                                            repaired = repair_tool_call_arguments(tool_args, tc["function"]["name"])
                                            try:
                                                coros[i] = (tc, json.loads(repaired), coros[i][2])
                                            except json.JSONDecodeError:
                                                coros[i] = (tc, {}, coros[i][2])

                                _digest_out = []
                                async for _digest_ev in self._digest_with_ping(results):
                                    if "_digest_done" in _digest_ev:
                                        _digest_out = _digest_ev["_digest_done"]
                                    else:
                                        yield _digest_ev
                                results = _digest_out
                                # M2 入账定号：digest 后把编号烙进 browser 来源行
                                # （信封 [N]/JSON citation_id），与持久化同序编号。
                                self._stamp_browser_citation_ids(state, results)
                                # M5 层 2：刷新台账 manifest（折叠保护，编号与检索深度解耦）
                                self._refresh_citation_manifest(state)
                                tool_messages = []
                                for (tc, tool_args, _), result in zip(coros, results):
                                    state.tool_results.append(result)
                                    tool_messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "name": tc["function"]["name"],
                                        "content": result.result,
                                    })
                                    state.total_tool_calls += 1
                                    # Count web_search calls (mirror the serial
                                    # path below). Without this the search_required
                                    # auto-invoke gate re-fires after a PARALLEL
                                    # search round (web_search_count stays 0) and
                                    # synthesizes a redundant call_user_search_*
                                    # third search (conv a3cfb421 2026-08-09:
                                    # msg[5]/[7]/[9] each ran 2 parallel model
                                    # searches + 1 guardrail-synthesized round).
                                    if tc["function"]["name"] == "web_search":
                                        state.web_search_count += 1
                                    yield tool_result_event(
                                        tc["id"], tc["function"]["name"], result)
                                    self._mark_activity(state)

                                tool_messages = await enforce_turn_budget(
                                    tool_messages, self._budget_config, self.workspace_path
                                )
                                ordered_tool_messages = []
                                for (tc, _, _), _ in zip(coros, results):
                                    tc_id = tc["id"]
                                    matched = None
                                    for tm in tool_messages:
                                        if tm.get("tool_call_id") == tc_id:
                                            matched = tm
                                            break
                                    if matched:
                                        ordered_tool_messages.append(matched)
                                    else:
                                        ordered_tool_messages.append({
                                            "role": "tool",
                                            "tool_call_id": tc_id,
                                            "name": tc["function"]["name"],
                                            "content": "[tool result missing]",
                                        })
                                state.messages.append(assistant_msg)
                                state.messages.extend(ordered_tool_messages)
                        else:
                            tool_results_map: Dict[str, ToolCallResult] = {}
                            tool_messages = []
                            for tc in valid_tool_calls:
                                tool_name = tc["function"]["name"]

                                if state.cancelled:
                                    interrupted_result = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments={},
                                        result=json.dumps({"error": "Agent cancelled", "interrupted": True}, ensure_ascii=False),
                                        error=True,
                                    )
                                    tool_results_map[tc["id"]] = interrupted_result
                                    tool_messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "name": tool_name,
                                        "content": interrupted_result.result,
                                    })
                                    continue

                                # 截断守卫（conv fcb2e60a 同类）：不可解析参数
                                # 绝不以 {} 派发；回可行动截断错误并继续本轮。
                                tool_args, _args_error = decide_tool_dispatch(_dispatch_plan, tc)
                                if _args_error is not None:
                                    yield {
                                        "tool_call": {
                                            "call_id": tc["id"],
                                            "name": tool_name,
                                            "arguments": {},
                                        }
                                    }
                                    tool_results_map[tc["id"]] = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments={},
                                        result=_args_error,
                                        error=True,
                                    )
                                    continue

                                yield {
                                    "tool_call": {
                                        "call_id": tc["id"],
                                        "name": tool_name,
                                        "arguments": tool_args,
                                    }
                                }

                                _turn_text, _turn_tr = self._current_turn_context(state)
                                _guarded, _hard = self._tool_liveness_coro(tool_name, self._execute_single_tool(
                                    tc["id"], tool_name, tool_args, session_factory, user, conversation, assistant,
                                    state,
                                    current_turn_content=_turn_text,
                                    current_turn_tool_results=_turn_tr,
                                ))
                                tool_task = asyncio.ensure_future(
                                    asyncio.wait_for(
                                        _guarded,
                                        timeout=_hard,
                                    )
                                )
                                try:
                                    while not tool_task.done():
                                        done, _ = await asyncio.wait({tool_task}, timeout=8.0)
                                        if not done:
                                            yield {"ping": True}
                                    result = tool_task.result()
                                except asyncio.TimeoutError:
                                    result = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments=tool_args,
                                        result=json.dumps({"error": f"Tool '{tool_name}' timed out"}),
                                        error=True,
                                    )
                                except ToolStalledError as stalled:
                                    result = ToolCallResult(
                                        call_id=tc["id"],
                                        name=tool_name,
                                        arguments=tool_args,
                                        result=json.dumps({"error": str(stalled)}),
                                        error=True,
                                    )
                                except asyncio.CancelledError:
                                    tool_task.cancel()
                                    try:
                                        await tool_task
                                    except asyncio.CancelledError:
                                        pass
                                    state.memory_read_pending.clear()
                                    raise

                                if result.error and await self._is_recoverable_tool_error(result):
                                    retry_result = await self._retry_tool_with_recovery(
                                        tc["id"], tool_name, tool_args,
                                        session_factory, user, conversation, assistant, state,
                                        failed_result=result,
                                    )
                                    if retry_result:
                                        result = retry_result

                                tool_results_map[tc["id"]] = result

                            # P0 R3: serial round complete; drop failed-read
                            # reservations so a retry can dispatch next iteration.
                            state.memory_read_pending.clear()

                            # Citation ledger: renumber web_search results
                            # with turn-global ids BEFORE the digest layer
                            # (same as the parallel path above).
                            self._apply_citation_ledger(state, list(tool_results_map.values()))

                            _digest_out = []
                            async for _digest_ev in self._digest_with_ping(list(tool_results_map.values())):
                                if "_digest_done" in _digest_ev:
                                    _digest_out = _digest_ev["_digest_done"]
                                else:
                                    yield _digest_ev
                            results_digested = _digest_out
                            # M2 入账定号（串行路径同款）：digest 后烙编号。
                            self._stamp_browser_citation_ids(state, results_digested)
                            # M5 层 2：刷新台账 manifest（串行路径同款）
                            self._refresh_citation_manifest(state)
                            tool_results_map = {
                                r.call_id: r for r in results_digested
                            }
                            tool_messages = [
                                {
                                    "role": "tool",
                                    "tool_call_id": r.call_id,
                                    "name": r.name,
                                    "content": r.result,
                                }
                                for r in results_digested
                            ]

                            tool_messages = await enforce_turn_budget(
                                tool_messages, self._budget_config, self.workspace_path
                            )
                            ordered_tool_messages = []
                            for tc in valid_tool_calls:
                                tc_id = tc["id"]
                                matched = None
                                for tm in tool_messages:
                                    if tm.get("tool_call_id") == tc_id:
                                        matched = tm
                                        break
                                if matched:
                                    ordered_tool_messages.append(matched)
                                else:
                                    ordered_tool_messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc_id,
                                        "name": tc["function"]["name"],
                                        "content": "[tool result missing]",
                                    })
                            state.messages.append(assistant_msg)
                            state.messages.extend(ordered_tool_messages)
                            for tc in valid_tool_calls:
                                result = tool_results_map[tc["id"]]
                                state.tool_results.append(result)
                                state.total_tool_calls += 1
                                # Count web_search calls
                                if tc["function"]["name"] == "web_search":
                                    state.web_search_count += 1
                                yield tool_result_event(
                                    tc["id"], tc["function"]["name"], result)
                                self._mark_activity(state)

                        # Post-tool guardrail — sync, pure-function. Short-circuits
                        # the loop after specific success conditions (currently:
                        # schedule(action='create') success). Lets the LLM stop
                        # mid-turn without another costly round-trip.
                        from app.services.tool_guardrails import evaluate_post_tool as _guard
                        _halted = False
                        for _r in state.tool_results[-len(valid_tool_calls):]:
                            _decision = _guard(_r.name, _r.result)
                            if _decision.action == "halt":
                                _halted = True
                                _halt_message = _decision.final_message or ""
                                logger.info(
                                    "AgentLoop halted by tool_guardrails after tool=%s",
                                    _r.name,
                                )
                                break
                        if _halted:
                            if _halt_message:
                                yield {"content": _halt_message}
                                state.messages.append(
                                    {"role": "assistant", "content": _halt_message}
                                )
                            state.finish_reason = "stop"
                            state.completed_normally = True
                            raise _LoopBreakSignal()

                        # Check if web_search limit reached (skip in deathmatch mode)
                        if state.web_search_count >= state.max_web_searches and not self._skip_guardrails():
                            logger.warning(
                                "Web search limit reached (%d/%d), forcing final answer",
                                state.web_search_count, state.max_web_searches,
                            )
                            yield {
                                "agent_step": {
                                    "name": "search_limit_guard",
                                    "title": "搜索次数限制",
                                    "content": f"已达到最大搜索次数 ({state.web_search_count} 次)，正在基于已有结果生成最终回答。",
                                    "step_type": "tool",
                                }
                            }
                            state.force_final_answer = True
                            state.consecutive_tool_iterations = 0
                            if assistant_content:
                                state.messages.append({"role": "assistant", "content": assistant_content})
                            else:
                                state.messages.append({"role": "assistant", "content": "[搜索完成...]"})
                            _inject_directive(
                                state,
                                "已达到最大搜索次数，请基于已收集的信息直接给出完整回答。"
                                "如果任务需要生成文件，请使用 execute_code 工具生成。"
                                "注意Markdown格式。",
                            )
                            # Tool-round boundary: a search round ended (its
                            # tool_call event may have been lost to a slow
                            # client's queue overflow) — the relay must drop
                            # any held pre-tool prose before the answer
                            # iteration streams (A4.9 M1 symmetry).
                            yield {
                                "iteration_done": {
                                    "iteration": state.iterations,
                                    "tool_calls": 1,
                                }
                            }
                            raise _LoopContinueSignal()

                        if has_budget_refund_tool:
                            # F4（Governor v2）：execute_code 退款封顶——变参循环
                            # 不再净零消耗（每回合 ≤refund_max_per_turn，超限
                            # 预算照常递减直至 force-final/grace 收口）。
                            from app.services.loop_governor_service import (
                                allow_execute_code_refund as _lgov_refund_ok,
                            )
                            if _lgov_refund_ok(state):
                                await state.budget.refund()
                                logger.debug(
                                    "Refunded iteration budget for execute_code tool call (refund %d this turn)",
                                    state.loop_refunds,
                                )

                        yield {
                            "iteration": {
                                "current": state.iterations,
                                "max": self.max_iterations,
                                "tool_calls": state.total_tool_calls,
                            }
                        }

                        # Deathmatch: evaluate judge after tool-calling iterations
                        # so the turns counter increments and goal progress is tracked.
                        if self._skip_guardrails() and state.total_tool_calls > 0:
                            # R2-#123（A4.9-B-1）：Terminate 置位时截断稿不得进
                            # judge——走 verdict/park 出口收口（落档 + 披露恰一次）。
                            if self._gov_terminated_now():
                                async for _gov_ev in self._gov_terminate_paused(
                                    self.deathmatch_manager
                                ):
                                    yield _gov_ev
                                raise _LoopReturnSignal()
                            dm = self.deathmatch_manager
                            logger.info(
                                "Deathmatch mid-loop judge: iterations=%d, tool_calls=%d, turns=%d",
                                state.iterations, state.total_tool_calls, dm._conv.deathmatch_turns,
                            )
                            # Build a summary of what tools were used
                            _tool_summary_parts = []
                            for _tr in state.tool_results[-len(valid_tool_calls):]:
                                _tool_summary_parts.append(f"[{_tr.name}] {_tr.result[:200]}")
                            _tool_summary = "\n".join(_tool_summary_parts) if _tool_summary_parts else "(tool calls in progress)"
                            _eval_content = assistant_content.strip() if assistant_content else _tool_summary

                            decision = await self._safe_judge(
                                dm, _eval_content,
                                user_initiated=False,
                                workspace_path=self.workspace_path or "",
                                state=state,
                            )
                            _dm_should_continue = decision.get("should_continue", False)
                            async for _ev in self._emit_deathmatch_verdict(dm, decision):
                                yield _ev
                            self._mark_activity(state)
                            if decision.get("verdict") == "done":
                                async for _ev in self._handle_deathmatch_done(dm, decision, state):
                                    yield _ev
                                raise _LoopReturnSignal()
                            if not _dm_should_continue:
                                yield {"done": True}
                                raise _LoopReturnSignal()
                            # Autonomy WAIT: honor the bounded in-loop backoff
                            # before hot-continuing the tool loop (A4.9 W1-I1).
                            await self._dm_decision_backoff(decision)
                    # Iteration boundary signal: chat.py's relay uses it to
                    # disarm the pre-tool text gate deterministically. When
                    # the tool_call event itself was dropped (slow client
                    # subscriber-queue overflow), this event is the only way
                    # the relay learns a tool round ended. `tool_calls` is
                    # THIS iteration's count: 0 → the held text is the answer
                    # and must stream; >0 → the held text is stale pre-tool
                    # prose and must be dropped.
                    yield {
                        "iteration_done": {
                            "iteration": state.iterations,
                            "tool_calls": len(tool_calls_collected),
                        }
                    }
                    raise _LoopContinueSignal()
        finally:
            _loop_result._args_error = _args_error
            _loop_result._c = _c
            _loop_result._cp_assistant = _cp_assistant
            _loop_result._decision = _decision
            _loop_result._digest_ev = _digest_ev
            _loop_result._digest_out = _digest_out
            _loop_result._dispatch_plan = _dispatch_plan
            _loop_result._dm_should_continue = _dm_should_continue
            _loop_result._ev = _ev
            _loop_result._eval_content = _eval_content
            _loop_result._guarded = _guarded
            _loop_result._halt_message = _halt_message
            _loop_result._halted = _halted
            _loop_result._hard = _hard
            _loop_result._parallel_cancelled = _parallel_cancelled
            _loop_result._r = _r
            _loop_result._res = _res
            _loop_result._results_by_id = _results_by_id
            _loop_result._safe_tool_calls = _safe_tool_calls
            _loop_result._tool_summary = _tool_summary
            _loop_result._tool_summary_parts = _tool_summary_parts
            _loop_result._tr = _tr
            _loop_result._truncated_result = _truncated_result
            _loop_result._turn_text = _turn_text
            _loop_result._turn_tr = _turn_tr
            _loop_result._wave = _wave
            _loop_result._wave_ids = _wave_ids
            _loop_result._waves = _waves
            _loop_result.assistant_msg = assistant_msg
            _loop_result.can_parallel = can_parallel
            _loop_result.coros = coros
            _loop_result.decision = decision
            _loop_result.dm = dm
            _loop_result.done = done
            _loop_result.has_budget_refund_tool = has_budget_refund_tool
            _loop_result.i = i
            _loop_result.interrupted_result = interrupted_result
            _loop_result.matched = matched
            _loop_result.ordered_tool_messages = ordered_tool_messages
            _loop_result.par_task = par_task
            _loop_result.r = r
            _loop_result.raw_results = raw_results
            _loop_result.repaired = repaired
            _loop_result.result = result
            _loop_result.results = results
            _loop_result.results_digested = results_digested
            _loop_result.retry_result = retry_result
            _loop_result.tc = tc
            _loop_result.tc_id = tc_id
            _loop_result.tm = tm
            _loop_result.tool_args = tool_args
            _loop_result.tool_calls_collected = tool_calls_collected
            _loop_result.tool_messages = tool_messages
            _loop_result.tool_name = tool_name
            _loop_result.tool_results_map = tool_results_map
            _loop_result.tool_task = tool_task
            _loop_result.valid_tool_calls = valid_tool_calls
            _loop_result.wave_coros = wave_coros

    def _gov_terminated_now(self) -> bool:
        """R2-#122/123：当前回合是否已被升级阶梯 Terminate（收口信号）。
        fail-open——governor 不可用时返回 False（生成主链路不受防线拖累）。"""
        try:
            from app.services.loop_governor_service import get_run_ctx as _lgov_get
            _c = _lgov_get(str(getattr(self, "_delivery_conv_id", "") or ""))
            return bool(_c and _c.terminated)
        except Exception:
            return False

    async def _gov_turn_convergence(self, state: Any, conv_id: str, lim: str):
        """回合收口的披露 + 位牌收尾（从 _run_loop 边界块抽取——行数契约
        ≤1000）。披露恰一次（consume_terminate_disclosure 去重）；置
        force_final/budget_grace_call 后由调用方**落入**下方 grace 消费块
        （⚠ A4.9-C2：绝不 continue——判定是 state 纯函数，continue 即自旋）。"""
        _lgov_reason = {
            "wall": "回合墙钟", "total_iterations": "总迭代", "tool_calls": "工具调用",
        }.get(lim, "重复退化（升级阶梯 Terminate）")
        logger.warning(
            "loop-governor: turn terminate conv=%s reason=%s", conv_id, lim or "ladder",
        )
        _lgov_text = None
        try:
            from app.services.loop_governor_service import (
                consume_terminate_disclosure as _lgov_disc,
            )
            _lgov_text = _lgov_disc(conv_id)
        except Exception:
            pass
        if lim or _lgov_text:
            yield {
                "agent_step": {
                    "name": "loop_governor_terminate",
                    "title": "回合收口",
                    "content": _lgov_text
                    or (
                        f"检测到{_lgov_reason}达到回合上界，已中断继续生成；"
                        "以下基于已有结果收尾（loop governor v2，非错误）。"
                    ),
                    "step_type": "recovery",
                }
            }
        state.force_final_answer = True
        state.budget_grace_call = True

    async def _finalize_deathmatch_branch(self, state, _loop_result, **kw):
        # R2-#123（A4.9-B-1）：Terminate 置位时截断稿**不得作为 judge 依据**——
        # 跳过 judge，走 verdict/park 出口收口（落档 + commit + 披露恰一次）。
        # ⚠ R3-1（复审 Critical）：仅死磕 active 才拦——本函数在非死磕内容回合
        # 也先行于 tail 调用，无条件拦截会跳过 G1 打回/披露，把截断稿静默
        # 终稿化（复刻裸 return 族）。非死磕回退 tail→边界收口路径。
        if (
            self.deathmatch_manager
            and self.deathmatch_manager.is_active
            and self._gov_terminated_now()
        ):
            async for _gov_ev in self._gov_terminate_paused(self.deathmatch_manager):
                yield _gov_ev
            raise _LoopReturnSignal()
        _final_content = kw.get('_final_content', _loop_result._final_content)
        _dm_should_continue = _loop_result._dm_should_continue
        _ev = _loop_result._ev
        _is_repeating = _loop_result._is_repeating
        _reset_ev = _loop_result._reset_ev
        _restart = _loop_result._restart
        _search_only = _loop_result._search_only
        _turns = _loop_result._turns
        compressed = _loop_result.compressed
        decision = _loop_result.decision
        dm = _loop_result.dm
        rep_prompt = _loop_result.rep_prompt
        retry_prompt = _loop_result.retry_prompt
        try:
                if self._skip_guardrails():
                    # Ensure the assistant message is in state.messages
                    if _final_content and not any(
                        m.get("content") == _final_content and m.get("role") == "assistant"
                        for m in reversed(state.messages)
                    ):
                        state.messages.append({"role": "assistant", "content": _final_content})
                    dm = self.deathmatch_manager

                    # Repetition detection: track responses and detect loops
                    _is_repeating = self._detect_repetition(state, _final_content)
                    self._track_response(state, _final_content)
                    if _is_repeating:
                        state.repetition_count += 1
                        logger.warning(
                            "Deathmatch repetition detected (count=%d, turns=%d)",
                            state.repetition_count, dm._conv.deathmatch_turns,
                        )
                    else:
                        state.repetition_count = 0

                    logger.info("AgentLoop judge running: status=%s turns=%d",
                                dm._conv.deathmatch_status, dm._conv.deathmatch_turns)
                    decision = await self._safe_judge(
                        dm, _final_content,
                        user_initiated=False,
                        workspace_path=self.workspace_path or "",
                        state=state,
                    )
                    _dm_should_continue = decision.get("should_continue", False)
                    async for _ev in self._emit_deathmatch_verdict(dm, decision):
                        yield _ev
                    self._mark_activity(state)
                    if _dm_should_continue and decision.get("continuation_prompt"):
                        await self._dm_decision_backoff(decision)
                        # Use repetition-detected prompt if agent is repeating itself
                        if state.repetition_count >= 2:
                            rep_prompt = dm.get_repetition_prompt()
                            if rep_prompt:
                                _inject_directive(state, rep_prompt)
                                raise _LoopContinueSignal()
                        _reset_ev = await self._maybe_dm_context_reset(dm, state)
                        if _reset_ev is not None:
                            yield _reset_ev
                        _inject_directive(state, decision["continuation_prompt"])
                        # Deathmatch escalation: after 3+ turns of only searching,
                        # force the agent to generate the output file.
                        _turns = dm._conv.deathmatch_turns or 0
                        if _turns >= 3:
                            _search_only = all(
                                tr.name == "web_search"
                                for tr in state.tool_results[-6:]
                            ) if len(state.tool_results) >= 3 else False
                            if _search_only:
                                logger.info(
                                    "Deathmatch escalation: turns=%d, forcing execute_code",
                                    _turns,
                                )
                                _inject_directive(
                                    state,
                                    "[强制指令] 你已经搜索了多轮但没有生成最终文件。"
                                    "现在必须停止搜索，立即使用 execute_code 工具生成'清单.xlsx'文件。"
                                    "基于你已收集到的所有信息，直接生成Excel文件。"
                                    "不要再调用 web_search。这是强制要求。",
                                )
                        raise _LoopContinueSignal()
                    elif decision.get("needs_compression_retry") or decision.get("needs_restart"):
                        # Context marker was lost: compress history up to the last
                        # preserved checkpoint and retry the same turn with a fresh
                        # continuation prompt/marker. If too many consecutive misses
                        # triggered a deathmatch restart, reset turn counters too.
                        _restart = decision.get("needs_restart", False)
                        logger.info(
                            "Deathmatch %s: turns=%d messages=%d",
                            "restart" if _restart else "compression retry",
                            dm._conv.deathmatch_turns or 0, len(state.messages),
                        )
                        compressed = await dm.compress_messages(state.messages)
                        state.messages = compressed
                        # Re-inject continuation prompt with a fresh marker.
                        retry_prompt = dm.get_continuation_prompt(_final_content)
                        if retry_prompt:
                                _inject_directive(state, retry_prompt)
                        raise _LoopContinueSignal()
                    elif decision.get("verdict") == "done":
                        async for _ev in self._handle_deathmatch_done(dm, decision, state):
                            yield _ev
                        raise _LoopReturnSignal()
                    else:
                        yield {"done": True}
                        raise _LoopReturnSignal()
        finally:
            _loop_result._dm_should_continue = _dm_should_continue
            _loop_result._ev = _ev
            _loop_result._is_repeating = _is_repeating
            _loop_result._reset_ev = _reset_ev
            _loop_result._restart = _restart
            _loop_result._search_only = _search_only
            _loop_result._turns = _turns
            _loop_result.compressed = compressed
            _loop_result.decision = decision
            _loop_result.dm = dm
            _loop_result.rep_prompt = rep_prompt
            _loop_result.retry_prompt = retry_prompt

    async def _finalize_canary_branch(self, state, _loop_result, **kw):
        _final_content = kw.get('_final_content', _loop_result._final_content)
        assistant_raw_content = kw.get('assistant_raw_content')
        conversation = kw.get('conversation')
        _c_after = _loop_result._c_after
        _c_before = _loop_result._c_before
        _canary_retry = _loop_result._canary_retry
        _conv_key = _loop_result._conv_key
        _ctx_len = _loop_result._ctx_len
        _disabled = _loop_result._disabled
        _hit = _loop_result._hit
        _log_miss = _loop_result._log_miss
        _marker = _loop_result._marker
        _min_ratio = _loop_result._min_ratio
        _misses = _loop_result._misses
        _ratio = _loop_result._ratio
        _threshold = _loop_result._threshold
        try:
                if self._canary_enabled and conversation is not None and not state.canary_compressed:
                    try:
                        from app.services.canary_marker import (
                            response_has_canary, canary_tracker,
                        )
                        _conv_key = str(getattr(conversation, "id", "") or "")
                        _marker = self._canary_marker
                        if _marker:
                            # The main-stream content (`assistant_content`) was
                            # already stripped by _strip_dsml_all above, so the
                            # check MUST read the raw stream text. With
                            # reasoning enabled, the thinking pass regenerates
                            # the final content (marker intact there) — a hit
                            # on EITHER pass counts.
                            _hit = response_has_canary(_final_content, _marker) or (
                                assistant_raw_content and response_has_canary(assistant_raw_content, _marker)
                            )
                            if _hit:
                                canary_tracker.record_hit(_conv_key)
                            else:
                                _misses = canary_tracker.record_miss(_conv_key)
                                _threshold = config.agent_canary_miss_threshold
                                _disabled = canary_tracker.auto_disabled(_conv_key)
                                if (_misses >= _threshold
                                        and canary_tracker.can_compress(_conv_key)
                                        and not _disabled):
                                    # A4.9 I2 (conv 97ff355d fix review): the
                                    # retry RE-ANSWERS the turn, so the salvage
                                    # flag must not suppress the synthesis pass
                                    # of that retry — otherwise non-live mode
                                    # ships an empty answer (the relay already
                                    # wiped its copy of the salvage).
                                    state.salvaged_final = False
                                    from app.services.context_compressor import estimate_request_tokens_rough
                                    _c_before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                    _ctx_len = _resolve_context_length(self.llm)
                                    _min_ratio = config.agent_canary_compress_min_ratio
                                    _ratio = (_c_before / _ctx_len) if _ctx_len > 0 else 1.0
                                    if state.audit_accepted:
                                        # 2026-08-22（conv efaf8f9c 复盘）：最终稿
                                        # 已被审计接受——canary 漏标只是警告信号
                                        # （SOTA：连贯且通过质检的回答漏标=记录继续），
                                        # 重答会产生措辞不同的重复生成 + 重复工具轮
                                        # （iter7/8 重新浏览 GitHub）+ 回答后卡片。
                                        # 直接出货，不注入重答指令。advisory 出货
                                        # （A4.9 Minors-1）也走此出口——结构性保证
                                        # 下不再有审计驱动的重生成，漏标只记录。
                                        logger.warning(
                                            "Canary trip (conv=%s): %d misses but draft was "
                                            "shipped as final (accept/advisory) — recording "
                                            "miss, shipping as-is (no re-answer)",
                                            _conv_key, _misses,
                                        )
                                    elif _min_ratio > 0 and _ratio < _min_ratio:
                                        # A3 (2026-08-21, conv efaf8f9c): the
                                        # context is far from the window
                                        # (incident: ~9%) — a canary miss here
                                        # is NOT context rot. Compressing is
                                        # the wrong lever (it misfired twice
                                        # with zero hits). Re-assert the marker
                                        # directive and re-answer only; nothing
                                        # rendered to the user (A4).
                                        # A4.9 Important-1: the per-request
                                        # trip guard must cover BOTH branches
                                        # (the outer gate reads
                                        # state.canary_compressed) — otherwise
                                        # a model that keeps missing the marker
                                        # re-trips up to max_iterations.
                                        state.canary_compressed = True
                                        logger.warning(
                                            "Canary trip (conv=%s): %d consecutive misses but "
                                            "context at %.0f%% < %.0f%% ratio gate — "
                                            "re-asserting marker directive only",
                                            _conv_key, _misses, _ratio * 100, _min_ratio * 100,
                                        )
                                        yield {"audit_reset": True}
                                        _inject_directive(
                                            state,
                                            "内部完整性校验未通过：你刚才的回答末尾缺少约定的校验标记。"
                                            "请重新完整回答用户最近的一条消息，"
                                            "并记得在回答最后一行原样输出校验标记。",
                                        )
                                        _canary_retry = True
                                    else:
                                        # Per-request guard BEFORE the await: if
                                        # compression fails, the retry directive
                                        # below must not re-trigger within this
                                        # request (budget bounded re-generation).
                                        state.canary_compressed = True
                                        self._ensure_compressor()
                                        state.messages = await self._compressor.compress_async(state.messages)
                                        _c_after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                                        state.last_request_estimate = _c_after
                                        canary_tracker.record_compression(_conv_key)
                                        logger.warning(
                                            "Canary trip (conv=%s): %d consecutive misses -> "
                                            "compressed %d->%d tokens, re-answering",
                                            _conv_key, _misses, _c_before, _c_after,
                                        )
                                        # A4: silent compression — the relay
                                        # resets its accumulators but renders
                                        # no step to the user (token badge
                                        # refresh stays).
                                        yield {"compression": {"before": _c_before, "after": _c_after, "silent": True}}
                                        _inject_directive(
                                            state,
                                            "内部完整性校验未通过：你刚才的回答末尾缺少约定的校验标记，"
                                            "说明上下文已经开始失效。系统已对历史上下文执行压缩。"
                                            "请基于压缩后的上下文，重新完整回答用户最近的一条消息，"
                                            "并记得在回答最后一行输出校验标记。",
                                        )
                                        _canary_retry = True
                                else:
                                    _log_miss = logger.debug if _disabled else logger.info
                                    _log_miss(
                                        "Canary miss (conv=%s): %d/%d misses, %s",
                                        _conv_key, _misses, _threshold,
                                        "auto-disabled" if _disabled else "no action",
                                    )
                    except Exception:
                        logger.exception("Canary check failed")
        finally:
            _loop_result._c_after = _c_after
            _loop_result._c_before = _c_before
            _loop_result._canary_retry = _canary_retry
            _loop_result._conv_key = _conv_key
            _loop_result._ctx_len = _ctx_len
            _loop_result._disabled = _disabled
            _loop_result._hit = _hit
            _loop_result._log_miss = _log_miss
            _loop_result._marker = _marker
            _loop_result._min_ratio = _min_ratio
            _loop_result._misses = _misses
            _loop_result._ratio = _ratio
            _loop_result._threshold = _threshold

    async def _finalize_synthesis_branch(self, state, _loop_result, **kw):
        _last_user_msg = kw.get('_last_user_msg')
        assistant_content = kw.get('assistant_content')
        reasoning_content = kw.get('reasoning_content')
        _adv_ev = _loop_result._adv_ev
        _adv_events = _loop_result._adv_events
        _adv_text = _loop_result._adv_text
        _cov_note = _loop_result._cov_note
        _final_content = kw.get('_final_content', _loop_result._final_content)
        _lc_note = _loop_result._lc_note
        _need_synthesis = _loop_result._need_synthesis
        _salvage_ev = _loop_result._salvage_ev
        _salvage_parts = _loop_result._salvage_parts
        _synth_guidance = _loop_result._synth_guidance
        _synth_shipped_guidance = _loop_result._synth_shipped_guidance
        event = _loop_result.event
        try:
                if self.enable_reasoning:
                    # EMPTY-ANSWER RETRY consistency (A4.9 I2): a retried
                    # iteration already streamed its content as the final
                    # answer (retry_thinking_off yield bypasses the
                    # suppression). When content IS present after a retry,
                    # skip the synthesis pass — otherwise the retried content
                    # AND the _final_thinking regeneration would BOTH be
                    # persisted (double answer) when live-thinking is off
                    # (config live_thinking=false or dedicated subtask
                    # iteration_llm). Empty-after-retry still synthesizes
                    # (the recovery path remains).
                    _need_synthesis = (
                        not self._live_thinking_enabled()
                        or not assistant_content.strip()
                    ) and not (
                        (state.retry_thinking_off
                         or (state.revision_thinking_off and self._live_thinking_enabled()))
                        and assistant_content.strip()
                    ) and not state.salvaged_final
                    if _need_synthesis:
                        if assistant_content:
                            state.messages.append({
                                "role": "assistant",
                                "content": assistant_content,
                            })
                        async for event in self._final_thinking(state, draft_content=assistant_content):
                            if "content" in event:
                                _final_content = _strip_leading_orphan_punct(event["content"])
                            yield event
                        # AGENTIC AUDIT of the synthesized final content
                        # (A4.9 review I1, conv 41d2790d follow-up): the
                        # synthesis pass is a FRESH LLM generation that the
                        # draft-path audit above never saw — a
                        # claim-without-content summary can slip through here.
                        # Re-run the SAME auditor LLM (rule 6 judges
                        # claim-vs-content) on the synthesized text — no
                        # deterministic detector (user principle: 语义判断
                        # 留给 LLM). Bounded by the rejection budget; on
                        # budget exhaustion a bounded salvage regeneration
                        # replaces the rejected text (never ship it fail-open,
                        # conv 97ff355d), never an infinite loop.
                        _synth_shipped_guidance = None
                        if (
                            not self._skip_guardrails()
                            and not state.creative_turn
                            and _final_content and _final_content.strip()
                            and (
                                config.agent_audit_policy == "advisory"
                                or (
                                    state.audit_rejections <= _audit_reject_budget_for(state)
                                    and state.audit_soft_rejections <= config.agent_audit_soft_reject_limit
                                )
                            )
                        ):
                            _synth_guidance = await self._audit_response(state, _final_content, _last_user_msg)
                            if config.agent_audit_policy == "advisory":
                                # advisory（同 draft 路径）：软判决只作建议，
                                # 确定性/硬拒至多一次定向修复，之后一律出货
                                # （软判决静默）。
                                if self._advisory_audit_gate(
                                    state, _synth_guidance, _final_content,
                                    reasoning_content, source="synthesis",
                                ) == "repair":
                                    yield {"audit_reset": True}
                                    raise _LoopContinueSignal()
                                if _synth_guidance is not None:
                                    _synth_shipped_guidance = _synth_guidance
                                    _synth_guidance = None
                            if _synth_guidance is None and _final_content.strip():
                                state.audit_accepted = True
                                if _synth_shipped_guidance is not None:
                                    # 合成稿自身非 accept → 结算（矛盾切除/具体说明）
                                    _adv_events, _adv_text = self._advisory_ship_events(
                                        state, _synth_shipped_guidance, _final_content,
                                    )
                                    _final_content = _adv_text
                                    for _adv_ev in _adv_events:
                                        yield _adv_ev
                                    state.audit_advisory_shipped = False
                                    # A4.9 r3 Important：合成 advisory 出货同样必须
                                    # 披露覆盖不完整（软判静默仅限存在性低置信文案）。
                                    _cov_note = _consume_low_confidence_note(
                                        state, coverage_only=True)
                                    if _cov_note:
                                        yield {"content": _cov_note}
                                else:
                                    # 合成稿自身通过审计：草稿轮的旧标记不适用
                                    state.audit_advisory_shipped = False
                                    _lc_note = _consume_low_confidence_note(state)
                                    if _lc_note:
                                        yield {"content": _lc_note}
                            if _synth_guidance:
                                if _synth_guidance.verdict == "reject":
                                    state.audit_rejections += 1
                                else:
                                    state.audit_soft_rejections += 1
                                # Best-of stash (conv 7dc7a0d5): synthesis rejects
                                # are selection candidates too (both branches).
                                _stash_rejected_draft(state, _final_content, _synth_guidance, source="synthesis", reasoning=reasoning_content)
                                await _emit_draft_rejected(self, state, _final_content, _synth_guidance, "synthesis", attempt_no=state.draft_attempt_seq)
                                if (
                                    state.audit_rejections <= _audit_reject_budget_for(state)
                                    and state.audit_soft_rejections <= config.agent_audit_soft_reject_limit
                                ):
                                    logger.warning(
                                        "Synthesized final rejected by auditor: verdict=%s %s",
                                        _synth_guidance.verdict,
                                        _synth_guidance.guidance[:120],
                                    )
                                    # A4: silent QC — internal reset event.
                                    yield {"audit_reset": True}
                                    _prune_guardrail_pairs(state)
                                    _rejected_append(self, state, _final_content or "", reasoning_content)
                                    # A4.9 M5: mirror the draft-reject branch —
                                    # the rejected synthesis must not linger in
                                    # the turn segments (_current_turn_context /
                                    # stitched audit would see superseded text).
                                    state.turn_content_segments.clear()
                                    _inject_directive(state, _synth_guidance.guidance, _ephemeral="response_audit")
                                    raise _LoopContinueSignal()
                                logger.warning(
                                    "Synthesized final rejected after budget (reject=%d soft=%d) — salvaging from cleaned context",
                                    state.audit_rejections,
                                    state.audit_soft_rejections,
                                )
                                _salvage_parts = []
                                async for _salvage_ev in self._salvage_after_audit_budget(state, _last_user_msg):
                                    if "content" in _salvage_ev:
                                        _salvage_parts.append(_salvage_ev["content"])
                                    yield _salvage_ev
                                _final_content = _strip_leading_orphan_punct(
                                    "".join(_salvage_parts).strip()
                                ) or _AUDIT_SALVAGE_FAILURE_TEXT
                                state.salvaged_final = True
        finally:
            _loop_result._adv_ev = _adv_ev
            _loop_result._adv_events = _adv_events
            _loop_result._adv_text = _adv_text
            _loop_result._cov_note = _cov_note
            _loop_result._final_content = _final_content
            _loop_result._lc_note = _lc_note
            _loop_result._need_synthesis = _need_synthesis
            _loop_result._salvage_ev = _salvage_ev
            _loop_result._salvage_parts = _salvage_parts
            _loop_result._synth_guidance = _synth_guidance
            _loop_result._synth_shipped_guidance = _synth_shipped_guidance
            _loop_result.event = event

    async def _finalize_primary_audit_branch(self, state, _loop_result, **kw):
        _last_user_msg = kw.get('_last_user_msg')
        reasoning_content = kw.get('reasoning_content')
        if not self._skip_guardrails():
            # Audit window history (conv 86e51bbd follow-up 2026-08-10;
            # conv 41d2790d 2026-08-10; widened 2026-08-12): originally
            # an ITERATION cap (audit only iterations 1-2), so the final
            # draft of a 3-iteration turn shipped UN-AUDITED (the hollow
            # "上方 Mermaid 图…" summary). Then a rejection budget keyed
            # on web_search_count == 0 — which let search turns ship
            # un-audited in live-thinking mode (blind spot closed
            # 2026-08-12: every final draft is now audited).
            # warning. A draft can therefore never ship without having
            # been audited at least once.
            # Audit window (2026-08-12 blind-spot wave): EVERY final
            # draft is audited — no exemptions for search turns or
            # attachment turns. The old gate (web_search_count == 0)
            # let any turn that called web_search once ship
            # un-audited in live-thinking mode (the synthesis-path
            # audit is unreachable there) — "has tool results" does
            # NOT imply "answers the question" (conv 97ff355d did 25
            # real tool calls and still drafted nonsense). The
            # [file-ref:] exemption is gone too; the auditor context
            # now carries an attachment-visibility note instead so it
            # doesn't false-accuse grounded drafts of fabrication.
            # Creative turns (coordinator-judged) and deathmatch mode
            # (own judge) remain exempt.
            _loop_result._audit_reject_budget = _audit_reject_budget_for(state)
            if not state.creative_turn:
                # Length-continuation tail (2026-08-12 blind-spot wave):
                # no longer SKIPS the audit — the auditor judges the
                # STITCHED whole answer (all no-tool segments of this
                # attempt), not the isolated tail fragment (auditing
                # the fragment alone was why the skip existed).
                _loop_result._stitched_audit = state.skip_audit_once
                state.skip_audit_once = False
                # Agentic pre-send audit: an LLM judges whether the
                # draft actually answers the LATEST user message
                # (on-topic, non-repetitive, adequately grounded).
                # Only a failed audit triggers another iteration —
                # never a mechanical "no tools were used" heuristic.
                # The old deterministic regex gate
                # (detect_fabricated_search_claim, conv 149ce886)
                # was removed per user principle (2026-07-20: agentic
                # over regex; conv 86e51bbd 2026-08-10: 自评草稿
                # "我是'能动手'的：搜索、浏览…" 被正则误判为检索声称 →
                # 强制合成检索 + 整段原文 query + 第二轮回答). The
                # auditor LLM decides — its template distinguishes
                # "用户要求检索却未调用工具"（不合格）from "历史已有
                # 信息凭记忆回答"（合格），and its context now states
                # the ACTUAL tool-call record so a claim about
                # web_search/browser use is judged against the truth.
                # LLM auditor is budget-bounded (audit_rejections <=
                # budget); when the budget is spent a bounded salvage
                # regeneration replaces the rejected draft (never
                # shipped fail-open, conv 97ff355d). The
                # claim-vs-content check (rule 6) is
                # the auditor LLM's job — no deterministic detector
                # (user principle 2026-07-20: 语义判断留给 LLM).
                _loop_result.guidance = None
                _loop_result._shipped_guidance = None
                _loop_result._shipped_text = None
                _loop_result._soft_limit = config.agent_audit_soft_reject_limit
                # NOTE (C-01c): the extracted segments do NOT keep locals —
                # every shared field is read/written on `_loop_result` in place,
                # which is what makes the dropped finally write-back safe.
                async for _ev in self._primary_audit_guidance(
                    state, _loop_result, _last_user_msg, reasoning_content):
                    yield _ev
                if _loop_result.guidance is None and _loop_result.assistant_content.strip():
                    # 出口一次（A4.9 R1 Important-1）：advisory 结算说明
                    # （矛盾/二次硬拒红线专用；软判决已静默）优先于低置信
                    # 接收说明；「草稿即最终稿」的三种模式
                    # （live/非 reasoning/retry_thinking_off）没有后续合成
                    # 审计消费点——必须在此消费，否则该类轮次静默出货
                    # （agent_worker/scheduler 硬编码非 reasoning）。
                    _loop_result._ships_as_final = _draft_ships_as_final(
                        self._live_thinking_enabled(),
                        self.enable_reasoning,
                        state.retry_thinking_off,
                    )
                    if state.audit_advisory_shipped and _loop_result._ships_as_final:
                        # A4.9 C1：结算基于被审计文本（拼接审计=完整拼接稿），
                        # audit_reset 后重发结算稿，用户可见==落库。
                        _loop_result._base_text = (
                            _loop_result._shipped_text if _loop_result._shipped_text is not None
                            else _loop_result.assistant_content
                        )
                        _loop_result._adv_events, _loop_result._adv_text = self._advisory_ship_events(
                            state, _loop_result._shipped_guidance, _loop_result._base_text,
                        )
                        if _loop_result._adv_text != _loop_result.assistant_content:
                            _loop_result.assistant_content = _loop_result._adv_text
                            state.turn_content_segments = [_loop_result.assistant_content]
                        for _loop_result._adv_ev in _loop_result._adv_events:
                            yield _loop_result._adv_ev
                        # A4.9 residual：advisory 软判保持静默（2026-09-18 用户
                        # 指令），但「分块覆盖不完整」是内容未核验披露而非审核
                        # 残留文案——仍须告知用户。
                        _loop_result._cov_note = _consume_low_confidence_note(
                            state, coverage_only=True)
                        if _loop_result._cov_note:
                            yield {"content": _loop_result._cov_note}
                    elif _loop_result._ships_as_final:
                        _loop_result._lc_note = _consume_low_confidence_note(state)
                        if _loop_result._lc_note:
                            yield {"content": _loop_result._lc_note}
                if _loop_result.guidance is None and _loop_result.assistant_content.strip() and self._live_thinking_enabled():
                    # 审计 accept（guidance None = 通过/软放行）——标记
                    # 本轮最终稿可信，canary trip 不得重答。仅 live 模式
                    # 草稿即最终稿；非 live 模式的最终稿是合成 pass 产出
                    # （其 accept 在合成审计点置位），草稿 accept 是中间态。
                    state.audit_accepted = True
                async for _ev in self._primary_audit_verdict(
                    state, _loop_result, _last_user_msg, reasoning_content):
                    yield _ev
    async def _primary_audit_guidance(self, state, _loop_result, _last_user_msg, reasoning_content):
        """Audit-policy segment (advisory / legacy) — produces `_loop_result.guidance`.

        C-01c: extracted verbatim from `_finalize_primary_audit_branch`."""
        if config.agent_audit_policy == "advisory":
            # advisory（2026-09-18 结构性重构，conv f831486f）：
            # judge 去阻塞化——软判决只作建议（不再重生成/消耗
            # 预算/进兜底链）；确定性可行动判决（npg/citation）
            # 与硬 reject 至多一次定向修复；修复后一律出货
            # （软判决静默；矛盾/硬拒红线说明见结算方法）。
            # 恢复旧行为：policy="legacy"。
            if _loop_result._stitched_audit and state.turn_content_segments:
                _loop_result._audit_target = "\n\n".join(state.turn_content_segments)
            else:
                _loop_result._audit_target = _loop_result.assistant_content
            _loop_result.guidance = await self._audit_response(
                state, _loop_result._audit_target, _last_user_msg)
            if self._advisory_audit_gate(
                state, _loop_result.guidance, _loop_result._audit_target, reasoning_content,
                source="draft",
            ) == "repair":
                yield {"audit_reset": True}
                raise _LoopContinueSignal()
            if _loop_result.guidance is not None:
                _loop_result._shipped_guidance = _loop_result.guidance
                _loop_result._shipped_text = _loop_result._audit_target
                _loop_result.guidance = None
        elif (
            state.audit_rejections <= _loop_result._audit_reject_budget
            and state.audit_soft_rejections <= _loop_result._soft_limit
        ):
            if _loop_result._stitched_audit and state.turn_content_segments:
                _loop_result._audit_target = "\n\n".join(state.turn_content_segments)
            else:
                _loop_result._audit_target = _loop_result.assistant_content
            # 差异闸门（用户要求 2026-08-21）：修正轮仅修改
            # 点名部分。上一稿是 reject 打回时，比对修正稿与
            # 被拒稿的相似度——整篇重写不送审计，静默注入
            # 重改指令一次（一次性标记防循环；短稿跳过；
            # config revision_min_similarity=0 关闭）。
            _loop_result._prev_stash = state.audit_rejected_drafts[-1] if state.audit_rejected_drafts else None
            if (
                not state.revision_rewrite_flagged
                and _loop_result._prev_stash is not None
                and (_loop_result._prev_stash.get("verdict") == "reject")
                and _is_full_rewrite(str(_loop_result._prev_stash.get("content") or ""), _loop_result._audit_target)
            ):
                state.revision_rewrite_flagged = True
                logger.warning(
                    "Revision similarity gate: full-rewrite detected (threshold %.2f) — "
                    "re-injecting copy-edit directive instead of auditing",
                    config.agent_audit_revision_min_similarity,
                )
                yield {"audit_reset": True}
                _prune_guardrail_pairs(state)
                _rejected_append(self, state, _loop_result.assistant_content, reasoning_content)
                _inject_directive(
                    state,
                    "【改动过大】你刚才的修正稿把原稿整篇重写了。"
                    "请以上一条 assistant 消息中的被拒草稿为底稿，"
                    "仅修改审计点名的部分，其余内容逐字保留，"
                    "重新输出完整回答。",
                    _ephemeral="response_audit",
                )
                raise _LoopContinueSignal()
            _loop_result.guidance = await self._audit_response(state, _loop_result._audit_target, _last_user_msg)

    async def _primary_audit_verdict(self, state, _loop_result, _last_user_msg, reasoning_content):
        """Verdict segment — rejection counters, NPG/family stall cut, then salvage
        (budget spent) or audit_reset + directive (reject).

        C-01c: extracted verbatim from `_finalize_primary_audit_branch`."""
        if _loop_result.guidance:
            # 2026-08-14 (conv a67faa04): ONLY verdict==reject
            # consumes the rejection budget; unverifiable /
            # needs_evidence ("can't see" is not fabrication)
            # consume the separate soft counter so truncated
            # evidence cannot burn the budget into the
            # failure-text path.
            if _loop_result.guidance.verdict == "reject":
                state.audit_rejections += 1
            else:
                state.audit_soft_rejections += 1
            if not config.agent_audit_revision_thinking_enabled:
                state.revision_thinking_off = True
            # Best-of stash (conv 7dc7a0d5): record BEFORE the
            # budget branch — the budget-spending draft is a
            # selection candidate too.
            _stash_rejected_draft(state, _loop_result._audit_target, _loop_result.guidance, source="draft", reasoning=reasoning_content)
            await _emit_draft_rejected(self, state, _loop_result._audit_target, _loop_result.guidance, "draft", attempt_no=state.draft_attempt_seq)
            # NPG no-progress 守卫（conv 3a216a51, 2026-09-02）：
            # 连续两次 NPG enforce 打回且 flags 不降、期间无新
            # 工具调用 → guidance 的两条自救路径（calculate
            # 回执 / 改写定性表述）均已失败，整稿重发无收敛
            # 指望（生产实证 flags 3→6→6→8），视同软预算耗尽
            # 直接进入 salvage/selection 链。
            _loop_result._npg_no_progress = (
                _loop_result.guidance.verdict == "needs_evidence"
                and getattr(_loop_result.guidance, "source", "") == "npg"
                and _npg_soft_reject_no_progress(
                    state.npg_reject_flags,
                    state.npg_reject_tool_calls,
                    state.npg_reject_raws,
                )
            )
            # 同族 stall cut（2026-09-15，conv f2553c58）：连续 N
            # 次同族审计失败（截断/矛盾/无依据/悬空/…）→ 重发
            # 无收敛指望（生产实证：截断族 4 连拒 + salvage +
            # selection 共 18 分钟 7 次生成仍兜底发货），视同
            # 预算耗尽直接进入 salvage/selection 链。
            _loop_result._audit_family = _audit_problem_family(
                getattr(_loop_result.guidance, "source", "llm"),
                _loop_result.guidance.problem,
            )
            state.audit_fail_families.append(_loop_result._audit_family)
            _loop_result._family_stall = _audit_family_stalled(
                state.audit_fail_families,
                config.agent_audit_stall_cut_family_repeats,
            )
            _loop_result._budget_spent = (
                state.audit_rejections > _loop_result._audit_reject_budget
                or state.audit_soft_rejections > _loop_result._soft_limit
                or _loop_result._npg_no_progress
                or _loop_result._family_stall
            )
            if _loop_result._budget_spent:
                if _loop_result._npg_no_progress:
                    logger.warning(
                        "NPG soft-reject no-progress (flags=%s tool_calls=%s) — cutting to salvage instead of another full regeneration",
                        state.npg_reject_flags[-2:],
                        state.npg_reject_tool_calls[-2:],
                    )
                if _loop_result._family_stall:
                    logger.warning(
                        "Audit family stall cut (family=%s repeats=%d history=%s) — cutting to salvage instead of another regeneration",
                        _loop_result._audit_family,
                        config.agent_audit_stall_cut_family_repeats,
                        state.audit_fail_families[-4:],
                    )
                # Budget spent: NEVER ship the just-rejected
                # draft (conv 97ff355d 2026-08-12: 5 correct
                # rejections — the model hallucinated that the
                # user message was a replay of its own previous
                # answer — then the 答非所问 5th draft shipped
                # fail-open and the user got an answer with
                # neither head nor tail). Run ONE bounded
                # salvage regeneration from a de-poisoned
                # context; if that also fails, an honest
                # failure notice ships instead of the draft.
                logger.warning(
                    "Response auditor budget spent (reject=%d soft=%d) — salvaging from cleaned context instead of shipping the rejected draft",
                    state.audit_rejections,
                    state.audit_soft_rejections,
                )
                _loop_result._salvage_parts: List[str] = []
                async for _salvage_ev in self._salvage_after_audit_budget(state, _last_user_msg):
                    if "content" in _salvage_ev:
                        _loop_result._salvage_parts.append(_salvage_ev["content"])
                    yield _salvage_ev
                _loop_result.assistant_content = _strip_leading_orphan_punct(
                    "".join(_loop_result._salvage_parts).strip()
                ) or _AUDIT_SALVAGE_FAILURE_TEXT
                state.salvaged_final = True
            else:
                # A4: silent QC — internal reset event
                # (relay wipes the streamed draft; nothing
                # rendered to the user).
                yield {"audit_reset": True}
                _prune_guardrail_pairs(state)
                _rejected_append(self, state, _loop_result.assistant_content, reasoning_content)
                # The rejected draft is dead: drop it from the
                # turn's content segments too (mirrors the
                # relay's accumulator reset) so the stitched
                # continuation audit and _current_turn_context
                # never see superseded text.
                state.turn_content_segments.clear()
                _inject_directive(state, _loop_result.guidance.guidance, _ephemeral="response_audit")
                raise _LoopContinueSignal()


    async def _finalize_answer_block(self, state, _loop_result, **kw):
        assistant_raw_content = kw.get('assistant_raw_content')
        conversation = kw.get('conversation')
        reasoning_content = kw.get('reasoning_content')
        user = kw.get('user')
        _adv_ev = _loop_result._adv_ev
        _adv_events = _loop_result._adv_events
        _adv_text = _loop_result._adv_text
        _audit_family = _loop_result._audit_family
        _audit_reject_budget = _loop_result._audit_reject_budget
        _audit_target = _loop_result._audit_target
        _base_text = _loop_result._base_text
        _budget_spent = _loop_result._budget_spent
        _c_after = _loop_result._c_after
        _c_before = _loop_result._c_before
        _canary_retry = _loop_result._canary_retry
        _conv_key = _loop_result._conv_key
        _cov_note = _loop_result._cov_note
        _ctx_len = _loop_result._ctx_len
        _disabled = _loop_result._disabled
        _dm_content = _loop_result._dm_content
        _dm_should_continue = _loop_result._dm_should_continue
        _ev = _loop_result._ev
        _family_stall = _loop_result._family_stall
        _final_content = _loop_result._final_content
        _g_blocked = _loop_result._g_blocked
        _g_ev = _loop_result._g_ev
        _g_events = _loop_result._g_events
        _hit = _loop_result._hit
        _is_repeating = _loop_result._is_repeating
        _last_user_msg = _loop_result._last_user_msg
        _lc_note = _loop_result._lc_note
        _log_miss = _loop_result._log_miss
        _marker = _loop_result._marker
        _min_ratio = _loop_result._min_ratio
        _misses = _loop_result._misses
        _need_synthesis = _loop_result._need_synthesis
        _npg_no_progress = _loop_result._npg_no_progress
        _prev_stash = _loop_result._prev_stash
        _ratio = _loop_result._ratio
        _reset_ev = _loop_result._reset_ev
        _restart = _loop_result._restart
        _salvage_ev = _loop_result._salvage_ev
        _salvage_parts = _loop_result._salvage_parts
        _search_only = _loop_result._search_only
        _shipped_guidance = _loop_result._shipped_guidance
        _shipped_text = _loop_result._shipped_text
        _ships_as_final = _loop_result._ships_as_final
        _soft_limit = _loop_result._soft_limit
        _stitched_audit = _loop_result._stitched_audit
        _synth_guidance = _loop_result._synth_guidance
        _synth_shipped_guidance = _loop_result._synth_shipped_guidance
        _threshold = _loop_result._threshold
        _turns = _loop_result._turns
        assistant_content = _loop_result.assistant_content
        compressed = _loop_result.compressed
        decision = _loop_result.decision
        dm = _loop_result.dm
        event = _loop_result.event
        guidance = _loop_result.guidance
        msg = _loop_result.msg
        rep_prompt = _loop_result.rep_prompt
        retry_prompt = _loop_result.retry_prompt
        try:
            if assistant_content:
                _last_user_msg = ""
                for msg in reversed(state.messages):
                    if msg.get("role") == "user":
                        _last_user_msg = msg.get("content", "")
                        break
                # Deathmatch mode: skip the tool_nudge logic since the
                # deathmatch loop handles continuation itself.
                _kw = locals()
                try:
                    async for _ev in self._finalize_primary_audit_branch(
                        state, _loop_result, **{k: _kw[k] for k in ('_last_user_msg', 'reasoning_content') if k in _kw},
                    ):
                        yield _ev
                finally:
                    _adv_ev = _loop_result._adv_ev
                    _adv_events = _loop_result._adv_events
                    _adv_text = _loop_result._adv_text
                    _audit_family = _loop_result._audit_family
                    _audit_reject_budget = _loop_result._audit_reject_budget
                    _audit_target = _loop_result._audit_target
                    _base_text = _loop_result._base_text
                    _budget_spent = _loop_result._budget_spent
                    _cov_note = _loop_result._cov_note
                    _family_stall = _loop_result._family_stall
                    _lc_note = _loop_result._lc_note
                    _npg_no_progress = _loop_result._npg_no_progress
                    _prev_stash = _loop_result._prev_stash
                    _salvage_ev = _loop_result._salvage_ev
                    _salvage_parts = _loop_result._salvage_parts
                    _shipped_guidance = _loop_result._shipped_guidance
                    _shipped_text = _loop_result._shipped_text
                    _ships_as_final = _loop_result._ships_as_final
                    _soft_limit = _loop_result._soft_limit
                    _stitched_audit = _loop_result._stitched_audit
                    assistant_content = _loop_result.assistant_content
                    guidance = _loop_result.guidance
                # No-tool iteration boundary (see the tool branch above): the
                # model answered without calling a tool — the relay must
                # release any held text and stream the answer live instead of
                # holding it until `done` (whole-answer pop, 2026-08-06 user
                # report). Placed AFTER the deterministic fabrication guard so
                # a rejected fake-claim draft is never flushed to the client
                # (the gate's original purpose, A4.9 I1); both reject paths
                # above `continue` past this yield.
                yield {
                    "iteration_done": {
                        "iteration": state.iterations,
                        "tool_calls": 0,
                    }
                }
                if state.total_tool_calls >= config.agent_skill_evolution_auto_suggest_threshold and config.agent_skill_evolution_enabled:
                    yield {
                        "agent_step": {
                            "name": "skill_evolution_suggestion",
                            "title": "技能建议",
                            "content": f"本次任务使用了 {state.total_tool_calls} 次工具调用。如果这是一个常见任务模式,可以考虑保存为技能以便复用。调用 skill_manage(action='create') 即可创建。",
                            "step_type": "suggestion",
                        }
                    }
                # Final thinking phase: when reasoning is enabled, always run a
                # dedicated deep-thinking synthesis pass before yielding the final
                # answer — even when no tools were called. Previously this was
                # gated on `total_tool_calls > 0`, which silently bypassed the
                # thinking toggle for pure-chat turns and confused users.
                # LIVE-THINKING MODE (agent.tool_loop.live_thinking, default on):
                # iterations already streamed thinking+content live, so the
                # draft IS the final answer — the regeneration pass only runs as
                # a fallback when the loop produced no visible content.
                # PHASE 1A: never write reasoning_content back to state.messages.
                # Reasoning must not round-trip — see llm_service._build_params.
                _final_content = assistant_content
                _kw = locals()
                try:
                    async for _ev in self._finalize_synthesis_branch(
                        state, _loop_result, **{k: _kw[k] for k in ('_last_user_msg', '_final_content', 'assistant_content', 'reasoning_content') if k in _kw},
                    ):
                        yield _ev
                finally:
                    _adv_ev = _loop_result._adv_ev
                    _adv_events = _loop_result._adv_events
                    _adv_text = _loop_result._adv_text
                    _cov_note = _loop_result._cov_note
                    _final_content = _loop_result._final_content
                    _lc_note = _loop_result._lc_note
                    _need_synthesis = _loop_result._need_synthesis
                    _salvage_ev = _loop_result._salvage_ev
                    _salvage_parts = _loop_result._salvage_parts
                    _synth_guidance = _loop_result._synth_guidance
                    _synth_shipped_guidance = _loop_result._synth_shipped_guidance
                    event = _loop_result.event

                # 遵循词 (canary) check — SOTA-corrected context-rot detector.
                # The model must echo the per-conversation marker at the end of
                # every final reply. Two CONSECUTIVE misses (across turns, one
                # final answer per turn) = the early-context instruction stopped
                # landing: compress once and re-answer. Never restart the loop —
                # the legacy deathmatch HTML-comment marker caused infinite
                # restarts because DeepSeek does not echo HTML comments (see
                # canary_marker.py header; fix_deathmatch_loop_stuck_at_turn0).
                _canary_retry = False
                _kw = locals()
                try:
                    async for _ev in self._finalize_canary_branch(
                        state, _loop_result, **{k: _kw[k] for k in ('_final_content', 'assistant_raw_content', 'conversation') if k in _kw},
                    ):
                        yield _ev
                finally:
                    _c_after = _loop_result._c_after
                    _c_before = _loop_result._c_before
                    _canary_retry = _loop_result._canary_retry
                    _conv_key = _loop_result._conv_key
                    _ctx_len = _loop_result._ctx_len
                    _disabled = _loop_result._disabled
                    _hit = _loop_result._hit
                    _log_miss = _loop_result._log_miss
                    _marker = _loop_result._marker
                    _min_ratio = _loop_result._min_ratio
                    _misses = _loop_result._misses
                    _ratio = _loop_result._ratio
                    _threshold = _loop_result._threshold
                # The marker is internal: strip it from everything the judge
                # and the persistence path see, AFTER the raw check above.
                if _final_content:
                    _final_content = _CANARY_RE.sub('', _final_content)
                if _canary_retry:
                    raise _LoopContinueSignal()

                # Deathmatch verdict evaluation after final thinking
                _kw = locals()
                try:
                    async for _ev in self._finalize_deathmatch_branch(
                        state, _loop_result, **{k: _kw[k] for k in ('_final_content',) if k in _kw},
                    ):
                        yield _ev
                finally:
                    _dm_should_continue = _loop_result._dm_should_continue
                    _ev = _loop_result._ev
                    _is_repeating = _loop_result._is_repeating
                    _reset_ev = _loop_result._reset_ev
                    _restart = _loop_result._restart
                    _search_only = _loop_result._search_only
                    _turns = _loop_result._turns
                    compressed = _loop_result.compressed
                    decision = _loop_result.decision
                    dm = _loop_result.dm
                    rep_prompt = _loop_result.rep_prompt
                    retry_prompt = _loop_result.retry_prompt

                # ── vibeweaver stop hook（A3/D2）：turn 收敛门禁 ──
                # 证据红 → 拦下收尾、注入纠正反馈续跑（预算内）；死亡磕模式
                # 由自己的 judge 收尾，不重复设门。fail-open。
                # A4.9 I4：迭代中途武装但未被消费的 loop_guard 纠正指令在此
                # 同样生效（退化终稿不得静默收尾）。
                # W12 P1#9：收尾门禁（与 coord direct_reply 快捷路径共用同一实现）
                _g_events, _g_blocked = await self._completion_guardrails_tail(
                    state, conversation, user, assistant_content, reasoning_content
                )
                for _g_ev in _g_events:
                    yield _g_ev
                if _g_blocked:
                    raise _LoopContinueSignal()

                state.completed_normally = True
                raise _LoopBreakSignal()
            else:
                async for _ev in self._finalize_empty_answer_branch(state, assistant_content):
                    yield _ev
        finally:
            _loop_result._adv_ev = _adv_ev
            _loop_result._adv_events = _adv_events
            _loop_result._adv_text = _adv_text
            _loop_result._audit_family = _audit_family
            _loop_result._audit_reject_budget = _audit_reject_budget
            _loop_result._audit_target = _audit_target
            _loop_result._base_text = _base_text
            _loop_result._budget_spent = _budget_spent
            _loop_result._c_after = _c_after
            _loop_result._c_before = _c_before
            _loop_result._canary_retry = _canary_retry
            _loop_result._conv_key = _conv_key
            _loop_result._cov_note = _cov_note
            _loop_result._ctx_len = _ctx_len
            _loop_result._disabled = _disabled
            _loop_result._dm_content = _dm_content
            _loop_result._dm_should_continue = _dm_should_continue
            _loop_result._ev = _ev
            _loop_result._family_stall = _family_stall
            _loop_result._final_content = _final_content
            _loop_result._g_blocked = _g_blocked
            _loop_result._g_ev = _g_ev
            _loop_result._g_events = _g_events
            _loop_result._hit = _hit
            _loop_result._is_repeating = _is_repeating
            _loop_result._last_user_msg = _last_user_msg
            _loop_result._lc_note = _lc_note
            _loop_result._log_miss = _log_miss
            _loop_result._marker = _marker
            _loop_result._min_ratio = _min_ratio
            _loop_result._misses = _misses
            _loop_result._need_synthesis = _need_synthesis
            _loop_result._npg_no_progress = _npg_no_progress
            _loop_result._prev_stash = _prev_stash
            _loop_result._ratio = _ratio
            _loop_result._reset_ev = _reset_ev
            _loop_result._restart = _restart
            _loop_result._salvage_ev = _salvage_ev
            _loop_result._salvage_parts = _salvage_parts
            _loop_result._search_only = _search_only
            _loop_result._shipped_guidance = _shipped_guidance
            _loop_result._shipped_text = _shipped_text
            _loop_result._ships_as_final = _ships_as_final
            _loop_result._soft_limit = _soft_limit
            _loop_result._stitched_audit = _stitched_audit
            _loop_result._synth_guidance = _synth_guidance
            _loop_result._synth_shipped_guidance = _synth_shipped_guidance
            _loop_result._threshold = _threshold
            _loop_result._turns = _turns
            _loop_result.assistant_content = assistant_content
            _loop_result.compressed = compressed
            _loop_result.decision = decision
            _loop_result.dm = dm
            _loop_result.event = event
            _loop_result.guidance = guidance
            _loop_result.msg = msg
            _loop_result.rep_prompt = rep_prompt
            _loop_result.retry_prompt = retry_prompt
    async def _finalize_empty_answer_branch(self, state, assistant_content):
        """No-content finalize path: deathmatch judgement for an empty turn
        and the bounded empty-answer retry (never ship a silent empty answer).

        C-01c: extracted verbatim from `_finalize_answer_block`."""
        # R2-#123（A4.9-B-2）：Terminate 置位时本分支不得静默 done/retry——
        # 空 partial 同样交收口点（死磕走 verdict 出口，其余回边界收口+grace）。
        if self._gov_terminated_now():
            if self.deathmatch_manager and self.deathmatch_manager.is_active:
                async for _gov_ev in self._gov_terminate_paused(self.deathmatch_manager):
                    yield _gov_ev
                raise _LoopReturnSignal()
            raise _LoopContinueSignal()
        if self.deathmatch_manager and self.deathmatch_manager.is_active:
            logger.info("AgentLoop recovery path (no tool calls): status=%s turns=%d",
                        self.deathmatch_manager._conv.deathmatch_status,
                        self.deathmatch_manager._conv.deathmatch_turns)
            # Run the judge even when no tool calls were made so the
            # turns counter increments and the DB status stays accurate.
            dm = self.deathmatch_manager
            _dm_content = assistant_content.strip() or "(no content produced)"

            # Repetition detection
            _is_repeating = self._detect_repetition(state, _dm_content)
            self._track_response(state, _dm_content)
            if _is_repeating:
                state.repetition_count += 1
                logger.warning(
                    "Deathmatch repetition detected in no-content path (count=%d, turns=%d)",
                    state.repetition_count, dm._conv.deathmatch_turns,
                )
            else:
                state.repetition_count = 0

            state.messages.append({"role": "assistant", "content": _dm_content})
            decision = await self._safe_judge(
                dm, _dm_content,
                user_initiated=False,
                workspace_path=self.workspace_path or "",
                state=state,
            )
            _dm_should_continue = decision.get("should_continue", False)
            async for _ev in self._emit_deathmatch_verdict(dm, decision):
                yield _ev
            self._mark_activity(state)
            if _dm_should_continue and decision.get("continuation_prompt"):
                await self._dm_decision_backoff(decision)
                # Use repetition-detected prompt if agent is repeating itself
                if state.repetition_count >= 2:
                    rep_prompt = dm.get_repetition_prompt()
                    if rep_prompt:
                        _inject_directive(state, rep_prompt)
                        if (await state.budget.get_remaining()) <= 0:
                            await state.budget.refund()
                        raise _LoopContinueSignal()
                _reset_ev = await self._maybe_dm_context_reset(dm, state)
                if _reset_ev is not None:
                    yield _reset_ev
                _inject_directive(state, decision["continuation_prompt"])
                if (await state.budget.get_remaining()) <= 0:
                    await state.budget.refund()
                raise _LoopContinueSignal()
            elif decision.get("needs_compression_retry") or decision.get("needs_restart"):
                _restart = decision.get("needs_restart", False)
                logger.info(
                    "Deathmatch %s (no-content): turns=%d messages=%d",
                    "restart" if _restart else "compression retry",
                    dm._conv.deathmatch_turns or 0, len(state.messages),
                )
                compressed = await dm.compress_messages(state.messages)
                state.messages = compressed
                retry_prompt = dm.get_continuation_prompt(_dm_content)
                if retry_prompt:
                        _inject_directive(state, retry_prompt)
                if (await state.budget.get_remaining()) <= 0:
                    await state.budget.refund()
                raise _LoopContinueSignal()
            elif decision.get("verdict") == "done":
                async for _ev in self._handle_deathmatch_done(dm, decision, state):
                    yield _ev
                raise _LoopReturnSignal()
            elif not _dm_should_continue:
                # human_gate / paused / partial_complete / inactive:
                # the judge ordered a full stop — do NOT inject the
                # generic "continue" nudge below, which would resurrect
                # a loop the user was told had stopped for human review.
                yield {"done": True}
                raise _LoopReturnSignal()
            # Fallthrough: inject generic nudge and continue
            _inject_directive(
                state,
                "请继续推进目标。你的上一轮回复没有生成有效内容，请直接给出有实质内容的回复。如果任务需要搜索或执行操作，请调用工具。",
            )
            if (await state.budget.get_remaining()) <= 0:
                await state.budget.refund()
            raise _LoopContinueSignal()
        # ── EMPTY-ANSWER RETRY (conv 517140ca, 2026-08-08) ──
        # The iteration LLM returned NO content, NO tool calls and NO
        # error — a silent empty response (reasoning budget burn on a
        # huge context is the known family, llm_empty_content). The
        # deathmatch path above retries via the judge; NORMAL mode
        # previously fell straight through to done-empty here with
        # ZERO retry, and chat.py then persisted
        # "回答生成失败：模型在工具调用后未能产出有效回答。" even
        # after the turn had done real work (conv 517140ca: 17 tool
        # steps wrote the bible + chapters 1-5, then the final
        # answer was empty). NEVER accept an empty final answer
        # without retrying: inject a directive and re-run the
        # iteration, bounded by max_empty_answer_retries.
        if state.empty_answer_retries < self.max_empty_answer_retries:
            state.empty_answer_retries += 1
            logger.warning(
                "Empty final answer (iterations=%d, tool_calls=%d) — "
                "auto-retry %d/%d",
                state.iterations, state.total_tool_calls,
                state.empty_answer_retries, self.max_empty_answer_retries,
            )
            yield {
                "agent_step": {
                    "name": "empty_answer_retry",
                    "title": "回答重试",
                    "content": (
                        f"模型未产出有效回答，正在自动重试"
                        f"（{state.empty_answer_retries}/{self.max_empty_answer_retries}）…"
                    ),
                    "step_type": "recovery",
                }
            }
            # Retry with thinking disabled — a silent-empty response
            # usually means the reasoning pass burned the token
            # budget on a huge context; the smaller no-think request
            # recovers (mirrors llm_service empty-content retry).
            # 2026-08-22: 先清空 relay 累加器——前序迭代（含 thinking
            # 开的工具轮）已流出的正文若不清理，重试的新正文会与之
            # 叠加显示（conv efaf8f9c 用户看到多份正式回答）。
            yield {"audit_reset": True}
            state.retry_thinking_off = True
            _inject_directive(
                state,
                "你上一轮没有产出任何有效回答。请直接回答用户的问题；"
                "如果任务尚未完成（文件、代码、正文等内容未生成完毕），"
                "请继续调用工具完成它，然后给出最终回答。",
            )
            if (await state.budget.get_remaining()) <= 0:
                await state.budget.refund()
            raise _LoopContinueSignal()
        yield {"done": True, "empty": True}


    async def _run_loop(
        self,
        state: AgentLoopState,
        session_factory: Any,
        user: Any,
        conversation: Any,
        assistant: Any,
    ) -> AsyncIterator[dict]:
        _sync_agent_globals()
        # In deathmatch goal-loop mode the judge alone decides when to stop, so
        # the loop must stay alive even at 0 budget: the consume() failure then
        # routes into the exhaustion handler below, which evaluates the goal and
        # resets the budget for the next turn instead of terminating the run.
        assistant_content = ""
        # vibeweaver loop-guard：注册活跃 run（观察器请求中断的落点）。
        _lg_conv_id = str(getattr(conversation, "id", "") or "")
        self._delivery_conv_id = _lg_conv_id
        # D3 stop-guard 会话锚：在任何回合写入之前建立 first_seen（白名单 mtime 比较）。
        try:
            from app.services.vibeweaver_stop_guard import touch_session as _sg_touch
            _sg_touch(_lg_conv_id)
        except Exception:
            pass
        try:
            # Governor v2：回合注册（R2-#126 幂等——direct_reply 阶段已注册则
            # 复用同代 ctx/台账，不丢 terminated、不翻预算；A4.9-I2 自噬修复）。
            from app.services.loop_governor_service import register_turn as _lgov_turn
            _lgov_turn(state, _lg_conv_id)
        except Exception:
            logger.debug("loop-governor turn register failed-open")
        while (await state.budget.get_remaining()) > 0 or state.budget_grace_call or self._skip_guardrails():
            # checkpoint 位于 consume 之前——budget.used
            # 仅代表「已完成」的迭代数，重放中断迭代时 consume 后回到同一 cursor，
            # 台账键跨重放稳定（write-before-invoke）。
            await self._maybe_checkpoint(state)
            # ── vibeweaver loop-guard（A5）：迭代边界消费纠正指令 ──
            # 观察器（文本退化 / noop-bash）请求的中断在下一迭代边界落地：
            # 丢弃退化草稿、注入纠正指令继续（预算由观察器侧控制）。
            if state.loop_guard_feedback:
                _lg_feedback = state.loop_guard_feedback
                state.loop_guard_feedback = ""
                yield {
                    "agent_step": {
                        "name": "loop_guard",
                        "title": "生成退化中断",
                        "content": "检测到生成退化（重复/无意义序列或 echo 空转），已中断并注入纠正指令。",
                        "step_type": "system",
                    }
                }
                if assistant_content:
                    # A4.9 I3：草稿若已被提交为 live assistant 消息（force-final
                    # 等路径），不得再 _rejected_append 一份重复；仅未提交时走
                    # 拒绝收编。segments 汇聚无论如何清空（避免外泄到终稿）。
                    _already_committed = any(
                        m.get("role") == "assistant" and m.get("content") == assistant_content
                        for m in state.messages[-3:]
                    )
                    if not _already_committed:
                        _prune_guardrail_pairs(state)
                        _rejected_append(self, state, assistant_content, reasoning_content)
                    state.turn_content_segments.clear()
                    assistant_content = ""
                    # 输出单元契约（2026-10-14）：退化草稿打回即代际边界——
                    # relay 累积同步清零（audit_reset），杜绝旧稿+纠正稿并存。
                    yield {"audit_reset": True}
                _inject_directive(state, _lg_feedback, _ephemeral="loop_guard")
                continue
            # ── Governor v2 · A4/A3：回合硬顶（墙钟/总迭代/工具调用）与阶梯
            # Terminate 的收口点。语义=部分结果+诚实披露（grace 收口），非 error。
            # 硬顶判定豁免死磕（其自身 judge + max_wall_time 管辖）；G2：
            # ctx.terminated（升级阶梯收口信号）**无条件消费**——设计 §4.1 覆盖
            # 图把 deathmatch 列在 Tier A 内，且该信号若不消费会粘性泄漏。──
            _lgov_lim = ""
            _lgov_ctx_obj = None
            try:
                from app.services.loop_governor_service import get_run_ctx as _lgov_get_ctx
                _lgov_ctx_obj = _lgov_get_ctx(_lg_conv_id)
            except Exception:
                pass
            if not self._skip_guardrails():
                try:
                    import time as _lgov_clock
                    from app.services.loop_governor_service import (
                        effective_turn_wall as _lgov_wall,
                        turn_hard_limits_hit as _lgov_hit,
                    )
                    _lgov_lim = _lgov_hit(
                        state,
                        wall_seconds_eff=_lgov_wall(config.agent_loop_governor_turn_wall_time_seconds),
                        now=_lgov_clock.monotonic(),
                    )
                except Exception:
                    pass
            if _lgov_lim or (_lgov_ctx_obj is not None and _lgov_ctx_obj.terminated):
                # R2-#122（A4.9-C1）：死磕收口走 verdict/park 出口（status=paused +
                # 落档 + commit），不裸 return——绕过 judge/落档 = conv 6b0faf81
                # 静默停摆家族 bug。披露/位牌收尾在 _gov_turn_convergence。
                if self._skip_guardrails():
                    async for _gov_ev in self._gov_terminate_paused(self.deathmatch_manager):
                        yield _gov_ev
                    return
                async for _gov_ev in self._gov_turn_convergence(state, _lg_conv_id, _lgov_lim):
                    yield _gov_ev
            if state.budget_grace_call:
                state.budget_grace_call = False
                async for event in self._grace_call(state):
                    yield event
                return

            if not await state.budget.consume():
                # Deathmatch mode: when the tool-loop budget is exhausted,
                # run the deathmatch evaluation BEFORE breaking. If the judge
                # says "continue", refund the budget and start the next
                # deathmatch turn. This prevents the tool-loop budget from
                # prematurely terminating the deathmatch goal loop.
                if self._skip_guardrails() and self.deathmatch_manager:
                    dm = self.deathmatch_manager
                    logger.info(
                        "Deathmatch budget exhausted: running evaluation (turns=%d, iterations=%d)",
                        dm._conv.deathmatch_turns, state.iterations,
                    )
                    # Build evaluation content from whatever was produced
                    _budget_eval_content = assistant_content.strip() if assistant_content else ""
                    if not _budget_eval_content and state.tool_results:
                        _parts = [f"[{tr.name}] {tr.result[:200]}" for tr in state.tool_results[-3:]]
                        _budget_eval_content = "\n".join(_parts)
                    if not _budget_eval_content:
                        _budget_eval_content = "(tool loop budget exhausted, no content produced)"

                    _budget_decision = await self._safe_judge(
                        dm, _budget_eval_content,
                        user_initiated=False,
                        workspace_path=self.workspace_path or "",
                        state=state,
                    )
                    _budget_should_continue = _budget_decision.get("should_continue", False)
                    async for _ev in self._emit_deathmatch_verdict(dm, _budget_decision):
                        yield _ev
                    self._mark_activity(state)
                    if _budget_should_continue and _budget_decision.get("continuation_prompt"):
                        await self._dm_decision_backoff(_budget_decision)
                        _reset_ev = await self._maybe_dm_context_reset(dm, state)
                        if _reset_ev is not None:
                            yield _reset_ev
                        _inject_directive(state, _budget_decision["continuation_prompt"])
                        await state.budget.reset()
                        logger.info("Deathmatch: budget reset for next turn (turns=%d)", dm._conv.deathmatch_turns)
                        continue
                    elif _budget_decision.get("verdict") == "done":
                        async for _ev in self._handle_deathmatch_done(dm, _budget_decision, state):
                            yield _ev
                        return
                    else:
                        yield {"done": True}
                        return
                break

            state.iterations += 1
            logger.debug("Agent loop iteration %d/%d (budget remaining: %d)",
                         state.iterations, self.max_iterations, await state.budget.get_remaining())

            # 插话注入（迭代边界）：用户在本轮生成期间发送的插话在此成为
            # 真实 user 消息，本轮下一次 LLM 调用即在上下文中可见。
            for _interjection_event in self._drain_interjections(state):
                yield _interjection_event

            if self._check_inactivity(state):
                elapsed = asyncio.get_event_loop().time() - state.last_activity_at
                logger.warning(
                    "AgentLoop inactivity timeout: no activity for %.1fs > %.1fs (iterations=%d)",
                    elapsed, state.inactivity_timeout_seconds, state.iterations,
                )
                yield {
                    "agent_step": {
                        "name": "conversation_timeout",
                        "title": "对话无响应",
                        "content": (
                            f"对话已卡住超过 {state.inactivity_timeout_seconds:.0f} 秒"
                            f"（无新内容产出 {elapsed:.0f} 秒），系统自动终止剩余步骤。"
                        ),
                        "step_type": "recovery",
                    }
                }
                if self._skip_guardrails() and self.deathmatch_manager:
                    # DEATHMATCH: inactivity must NEVER silently kill the goal
                    # loop (conv 6b0faf81: a slow LLM call on a multi-hundred-
                    # thousand-token context exceeds the 300s inactivity
                    # window → the old code ran _grace_call and returned,
                    # leaving deathmatch_status="active" with no terminal
                    # message; the reconcile flipped it to "paused" 18 min
                    # later — a silent stop on a healthy long task). Route
                    # through the judge like the budget-exhaustion path: a
                    # "continue" verdict refreshes activity and keeps going;
                    # a done/stop verdict persists the terminal state.
                    # Bounded (A4.9 review I5): consecutive no-activity cycles
                    # with no real work escalate to a visible stop instead of
                    # spinning for the 7-day wall budget on a dead provider.
                    dm = self.deathmatch_manager
                    state.consecutive_inactivity_cycles += 1
                    _max_inact_cycles = max(
                        int(config.agent_tool_loop.get("max_inactivity_judge_cycles", 4)),
                        1,
                    )
                    if state.consecutive_inactivity_cycles >= _max_inact_cycles and not config.deathmatch_autonomy_enabled:
                        logger.warning(
                            "Deathmatch: %d consecutive inactivity cycles (turns=%d) — "
                            "stopping with visible state instead of spinning",
                            state.consecutive_inactivity_cycles,
                            dm._conv.deathmatch_turns,
                        )
                        # I7: the paused state must actually LAND in the DB.
                        # Mutating dm._conv alone is invisible — task_db is
                        # only committed by chat.py's per-verdict handler.
                        # Emit the verdict event so the handler syncs +
                        # commits status and persists a visible message.
                        async for _ev in self._stop_inactivity_paused(dm):
                            yield _ev
                        yield {"done": True}
                        return
                    _inact_content = assistant_content.strip() if assistant_content else (
                        "(inactivity timeout — no content produced)"
                    )
                    _inact_decision = await self._safe_judge(
                        dm, _inact_content,
                        user_initiated=False,
                        workspace_path=self.workspace_path or "",
                        state=state,
                    )
                    _inact_should_continue = _inact_decision.get("should_continue", False)
                    async for _ev in self._emit_deathmatch_verdict(dm, _inact_decision):
                        yield _ev
                    if _inact_decision.get("verdict") == "done":
                        async for _ev in self._handle_deathmatch_done(dm, _inact_decision, state):
                            yield _ev
                        return
                    if _inact_should_continue and _inact_decision.get("continuation_prompt"):
                        await self._dm_decision_backoff(_inact_decision)
                        _reset_ev = await self._maybe_dm_context_reset(dm, state)
                        if _reset_ev is not None:
                            yield _reset_ev
                        _inject_directive(state, _inact_decision["continuation_prompt"])
                        await state.budget.reset()
                        # Refresh the inactivity timer WITHOUT resetting the
                        # consecutive-cycle counter (A4.9 review I5): resetting
                        # via _mark_activity on judge-continue made the bound
                        # unreachable — a dead provider would spin forever
                        # because each continue reset the counter. Only REAL
                        # output (content/tool events) resets it via
                        # _mark_activity.
                        state.last_activity_at = asyncio.get_event_loop().time()
                        logger.info(
                            "Deathmatch: inactivity routed through judge — continue (turns=%d)",
                            dm._conv.deathmatch_turns,
                        )
                        continue
                    # Judge said continue WITHOUT a continuation prompt, or
                    # ordered a full stop: persist a visible paused state
                    # instead of a silent done (A4.9 review I7 — never leave
                    # the zombie outcome the fix was built to eliminate).
                    async for _ev in self._stop_inactivity_paused(dm):
                        yield _ev
                    yield {"done": True}
                    return
                state.budget_grace_call = True
                async for event in self._grace_call(state):
                    yield event
                return

            yield {"ping": True}

            if self.enable_compression and self._compressor is None:
                self._ensure_compressor()

            # P0-2: pre-iteration spike guard — estimate first, compress
            # before the call instead of after the 400 (round-4 eval).
            _spike_ev = await self._maybe_spike_compress(state)
            if _spike_ev is not None:
                yield _spike_ev

            # Deathmatch mode: force compression when the message list grows
            # too large, even if token count is below the threshold. This
            # prevents unbounded growth with high max_turns (e.g. 9999).
            # (Skipped when the spike guard already compressed THIS
            # iteration — one compression per iteration, A4.9 r2 M4.)
            # Gate logic: _dm_forced_compression_needed (P2-11 rubric aware).
            if (_spike_ev is None and self._dm_forced_compression_needed(len(state.messages))):
                from app.services.context_compressor import estimate_request_tokens_rough
                before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                state.messages = await self._compressor.compress_async(state.messages)
                after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                state.last_request_estimate = after
                logger.info("Deathmatch forced compression (msg count=%d): %d->%d tokens",
                            len(state.messages), before, after)
                yield {"compression": {"before": before, "after": after}}
            elif _spike_ev is None and self._compressor and self._compressor.should_compress(
                state.messages, prompt_tokens=self._effective_context_tokens(state)
            ):
                from app.services.context_compressor import estimate_request_tokens_rough
                before = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                state.messages = await self._compressor.compress_async(state.messages)
                after = estimate_request_tokens_rough(state.messages, tools=self.tool_schemas)
                state.last_request_estimate = after
                logger.info("Compressed %d->%d tokens", before, after)
                yield {"compression": {"before": before, "after": after}}

            sanitize_messages_surrogates(state.messages)
            self._repair_message_sequence(state.messages)

            self._maybe_load_lazy_tools(state)
            self._maybe_expand_searched_tools(state)

            active_tool_schemas = None
            if self.tool_schemas:
                if state.force_final_answer:
                    # The force-final-answer guard restricts the tools OFFERED
                    # to the model to execute_code only (final answer must be
                    # text or a code-generated file; conv aadb26a3 2026-07-18
                    # ran 41 browser_snapshot calls via DSML after the guard
                    # fired 4 times). NOTE: previously the execute_code
                    # offer was gated on `not state.executed_final_code`,
                    # which dropped tools to NONE after the model's first
                    # final code run — stranding a mid-task model (conv
                    # 517140ca 2026-08-08: chapter 5 at 979/3500 chars) with
                    # no way to continue → silent empty answer. Keep
                    # execute_code available for the forced phase, BUT
                    # bounded by max_force_stage_rounds (A4.9 I1): execute_code
                    # rounds refund the budget, so without this hard cap the
                    # forced phase could run unbounded. Past the cap the model
                    # must answer in text (empty answers then hit the bounded
                    # empty-answer retry).
                    if state.force_stage_rounds < self.max_force_stage_rounds:
                        active_tool_schemas = [ts for ts in self.tool_schemas
                                               if ts.get("function", {}).get("name") in ("execute_code",)]
                    else:
                        logger.warning(
                            "Force-stage round cap reached (%d/%d) — execute_code no longer offered",
                            state.force_stage_rounds, self.max_force_stage_rounds,
                        )
                else:
                    active_tool_schemas = self.tool_schemas
                    # T8: deathmatch step-level capability orchestration —
                    # when the current plan step declares its tools, offer
                    # only that subset (schema tokens↓ + tool misuse↓). Log
                    # on subset CHANGE (per-turn noise must stay low).
                    _dm_names = self._dm_step_tool_names()
                    if _dm_names is not None:
                        active_tool_schemas = [
                            ts for ts in active_tool_schemas
                            if ts.get("function", {}).get("name") in _dm_names
                        ]
                        if getattr(state, "_dm_last_tool_subset", None) != _dm_names:
                            state._dm_last_tool_subset = _dm_names
                            logger.info(
                                "deathmatch step tool subset active: %d/%d schemas offered",
                                len(active_tool_schemas), len(self.tool_schemas),
                            )

            tool_calls_collected: List[Dict[str, Any]] = []
            assistant_content = ""
            assistant_raw_content = ""
            reasoning_content = ""
            finish_reason: Optional[str] = None
            state.dsml_active = False
            state.dsml_tail = ""

            # Sub-agents (tool-calling iterations) historically NEVER used
            # thinking — the answer was suppressed and regenerated later by
            # _final_thinking (a second full pass: TTFT 76-92s, conv daa19eac).
            # Live-thinking mode: iterations think+stream immediately (one
            # pass, opencode-style); _final_thinking becomes fallback-only.
            # Empty-answer retries (conv 517140ca) disable thinking: a
            # silent-empty response usually means the reasoning pass burned
            # the token budget on a huge context — the smaller, no-think
            # request recovers (llm_empty_content family).
            _use_thinking = (
                self._live_thinking_enabled()
                and (self.strict_reasoning
                     or (not state.retry_thinking_off
                         and not state.revision_thinking_off))
            )
            from app.services.agent_run_spec import should_mark_reasoning_degraded

            if should_mark_reasoning_degraded(
                enabled=bool(self.enable_reasoning),
                use_thinking=bool(_use_thinking),
                retry_thinking_off=bool(state.retry_thinking_off),
                revision_thinking_off=bool(state.revision_thinking_off),
                main_serves_iterations=(self.iteration_llm is self.llm),
            ):
                self.reasoning_degraded = True
            logger.info(
                "AgentLoop iteration %d: enable_reasoning=%s, total_tool_calls=%d, iterations=%d, _use_thinking=%s, reasoning_effort=%s",
                state.iterations, self.enable_reasoning, state.total_tool_calls, state.iterations, _use_thinking, self.reasoning_effort
            )

            _loop_result = _LoopResult()
            for _name in ('_iter_extra_body', '_iter_kwargs', '_iter_started_at', '_iter_timeout', '_measured', '_ratio', '_reasoning_char_cap', '_reasoning_truncated', 'after', 'assistant_content', 'assistant_raw_content', 'before', 'content_text', 'delay', 'elapsed', 'event', 'event_data', 'event_type', 'fallback_msg', 'finish_reason', 'llm_stream', 'reasoning_content', 'recovery_action', 'retry_after', 'should_retry', 'tool_calls_collected'):
                if _name in locals():
                    setattr(_loop_result, _name, locals()[_name])
            _kw = locals()
            try:
                try:
                    async for _ev in self._stream_iteration(
                        state, _loop_result, **{k: _kw[k] for k in ('_lg_conv_id', '_use_thinking', 'active_tool_schemas') if k in _kw},
                    ):
                        yield _ev
                finally:
                    _iter_extra_body = _loop_result._iter_extra_body
                    _iter_kwargs = _loop_result._iter_kwargs
                    _iter_started_at = _loop_result._iter_started_at
                    _iter_timeout = _loop_result._iter_timeout
                    _measured = _loop_result._measured
                    _ratio = _loop_result._ratio
                    _reasoning_char_cap = _loop_result._reasoning_char_cap
                    _reasoning_truncated = _loop_result._reasoning_truncated
                    after = _loop_result.after
                    assistant_content = _loop_result.assistant_content
                    assistant_raw_content = _loop_result.assistant_raw_content
                    before = _loop_result.before
                    content_text = _loop_result.content_text
                    delay = _loop_result.delay
                    elapsed = _loop_result.elapsed
                    event = _loop_result.event
                    event_data = _loop_result.event_data
                    event_type = _loop_result.event_type
                    fallback_msg = _loop_result.fallback_msg
                    finish_reason = _loop_result.finish_reason
                    llm_stream = _loop_result.llm_stream
                    reasoning_content = _loop_result.reasoning_content
                    recovery_action = _loop_result.recovery_action
                    retry_after = _loop_result.retry_after
                    should_retry = _loop_result.should_retry
                    tool_calls_collected = _loop_result.tool_calls_collected
            except _LoopContinueSignal:
                continue
            except _LoopBreakSignal:
                break
            except _LoopReturnSignal:
                return

            if finish_reason == "length":
                logger.warning("LLM response truncated (finish_reason=length), content=%d chars",
                               len(assistant_content))
                if tool_calls_collected:
                    valid_tc = []
                    for tc in tool_calls_collected:
                        args_raw = tc.get("function", {}).get("arguments", "")
                        if args_raw:
                            try:
                                json.loads(args_raw)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "Dropping truncated tool call %s: JSON args incomplete",
                                    tc.get("function", {}).get("name", "?"),
                                )
                                continue
                        valid_tc.append(tc)
                    tool_calls_collected = valid_tc
                    # FIX: If all tool calls were dropped due to truncation, recover by
                    # asking the LLM to break the task into smaller chunks.
                    if not tool_calls_collected:
                        logger.warning("All tool calls dropped due to truncation, requesting smaller chunks")
                        if assistant_content:
                            state.messages.append({"role": "assistant", "content": assistant_content})
                        # 输出单元契约（2026-10-14）：重问即代际边界——relay
                        # 累积同步清零，否则残稿+重调用后的正文并存=双倍输出。
                        yield {"audit_reset": True}
                        _inject_directive(
                            state,
                            "你的上一个工具调用因为内容过长被截断了。请将任务拆分成更小的步骤：\n"
                            "1. 不要在一次工具调用中生成全部内容\n"
                            "2. 对于文件生成任务：先创建文件框架，再分批追加内容\n"
                            "3. 对于代码执行任务：将大段代码拆分成多个小的 execute_code 调用\n"
                            "请重新调用工具，使用更小的参数。",
                        )
                        continue
                else:
                    if assistant_content:
                        # Tag the truncated head: it stays in the model's
                        # context (the continuation must see it), but the
                        # auditor's previous-answer window must NOT pick it up
                        # — it is THIS answer's first half, not the previous
                        # turn's answer (A4.9 I2, conv 97ff355d wave-3).
                        state.messages.append({"role": "assistant", "content": assistant_content, "_truncated_part": True})
                        # A4.9 C1: this branch `continue`s BEFORE the segment
                        # append below — without this line the "stitched"
                        # audit only ever judged the tail fragment.
                        state.turn_content_segments.append(assistant_content)
                    _inject_directive(
                        state,
                        "你的上一个回答被截断了。请继续完成你的回答。"
                        "如果需要生成大量内容，请分多次调用工具，每次只处理一部分。",
                    )
                    # The next iteration's content is the TAIL of this truncated
                    # answer — the pre-send audit judges the STITCHED whole
                    # (segments above), not the isolated fragment.
                    state.skip_audit_once = True
                    # No-tool truncation boundary: release any held pre-tool
                    # text so the continuation streams instead of parking the
                    # whole answer until `done` (2026-08-06 user report).
                    # stitch=True：唯一合法拼接边界（finish_reason=length 续写）
                    # ——chat.py 据此放行链上文本跨工具边界拼接（2026-10-14）。
                    yield {
                        "iteration_done": {
                            "iteration": state.iterations,
                            "tool_calls": 0,
                            "stitch": True,
                        }
                    }
                    continue

            # Extract any DeepSeek-DSML-style tool calls that the model emitted
            # inline as content instead of using native function-calling deltas.
            # This removes the markup from the final message and lets the loop
            # execute the tools.
            if assistant_raw_content:
                cleaned_raw, dsml_tool_calls = _extract_dsml_tool_calls(assistant_raw_content)
                if dsml_tool_calls and state.force_final_answer:
                    # The force-final-answer guard restricts the tools OFFERED
                    # to the model, but inline DSML markup bypasses that
                    # restriction — observed 2026-07-18 (conv aadb26a3): the
                    # model ran 41 browser_snapshot calls via DSML after the
                    # guard fired 4 times. Apply the same restriction here:
                    # only execute_code (allowed by the guard) may pass.
                    dsml_tool_calls = [
                        tc for tc in dsml_tool_calls
                        if tc.get("function", {}).get("name") == "execute_code"
                    ]
                if dsml_tool_calls:
                    existing_signatures = {
                        (tc.get("function", {}).get("name"), tc.get("function", {}).get("arguments"))
                        for tc in tool_calls_collected
                    }
                    for tc in dsml_tool_calls:
                        sig = (tc.get("function", {}).get("name"), tc.get("function", {}).get("arguments"))
                        if sig in existing_signatures:
                            continue
                        existing_signatures.add(sig)
                        tool_calls_collected.append(tc)
                        logger.info(
                            "Extracted DSML tool call: name=%s id=%s",
                            tc["function"]["name"], tc["id"],
                        )
                # Prefer the fully cleaned content (entire DSML blocks removed)
                # over the stream tag-stripped version for storage/context.
                # Also strip any remaining individual DSML tags that may have
                # survived due to partial-block streaming edge cases.
                assistant_content = _strip_dsml_all(cleaned_raw)
                # Leading-orphan punctuation defense (conv efaf8f9c 2026-08-21:
                # qwen3.8 first-token glitch produced drafts opening with '，'
                # and the auditor rejected five regenerations for the same
                # grammar defect — clean BEFORE the audit sees the draft).
                assistant_content = _strip_leading_orphan_punct(assistant_content)

            # ── 行动层/决策层冲突仲裁 (conv a8290d37 2026-08-28) ──
            # 响应同时携带「完整正式回答 + 工具调用」时，旧规则把 content 当
            # 临时过渡文本丢弃、无条件服从行动层 → 模型 11 份完整答案被弃、
            # 发送前审计结构性失明、循环 16 轮。仲裁：content 达实质长度且
            # 所有调用客观冗余（本轮已成功同参 / 非分页 browser 且 URL 全部
            # 已抓取）→ 决策层优先：丢弃冗余调用，content 落入下方常规终答
            # 路径（turn_content_segments + 发送前审计）。任一调用含新意图
            # （新 URL / paginated 深抓 / 新参数）则照常执行工具。
            if (
                tool_calls_collected
                and not self._skip_guardrails()
                and config.agent_tool_loop_answer_conflict_arbitration
                and _should_promote_answer_over_tools(
                    state, assistant_content, tool_calls_collected,
                    min_chars=config.agent_tool_loop_answer_conflict_min_chars,
                )
            ):
                logger.warning(
                    "Answer/tool conflict arbitration: dropping %d redundant tool "
                    "call(s), promoting %d-char co-emitted answer to the audit "
                    "path (iteration=%d, total_tool_calls=%d)",
                    len(tool_calls_collected), len(assistant_content),
                    state.iterations, state.total_tool_calls,
                )
                yield {
                    "agent_step": {
                        "name": "answer_tool_conflict",
                        "title": "采纳完整回答",
                        "content": "助手已产出完整回答，其附带的工具调用与本轮已成功的调用完全重复（冗余），已采纳回答并交由审计判定。",
                        "step_type": "system",
                    }
                }
                tool_calls_collected = []

            # Force-final stage hard stop (A4.9 R1 M6): only execute_code is
            # offered during the forced phase, but a non-compliant model can
            # still emit NATIVE calls to other tools — conv a8290d37 kept
            # issuing native browser calls through 4 doom aborts. Mirror the
            # DSML inline filter: drop them before execution.
            if (
                tool_calls_collected
                and state.force_final_answer
                and not self._skip_guardrails()
            ):
                tool_calls_collected, _force_dropped = _filter_force_stage_calls(
                    state, tool_calls_collected
                )
                if _force_dropped:
                    logger.warning(
                        "Force-final stage: dropped %d native non-execute_code "
                        "call(s) (%s) before execution",
                        len(_force_dropped),
                        ",".join(sorted({(tc.get("function") or {}).get("name", "?") for tc in _force_dropped})),
                    )

            # Accumulate the cleaned assistant text for this iteration so
            # tools invoked later in the same turn (e.g. pdf_export
            # export_conversation) can access the in-progress response
            # before it is persisted to the database.
            # When tool calls are present, the pre-tool content is transient
            # and should not be treated as part of the authoritative answer.
            if assistant_content and not tool_calls_collected:
                state.turn_content_segments.append(assistant_content)

            # ── User-requested search: agentic, never synthesized ──
            # conv 149ce886 原实现：coordinator search_required=True 且模型零
            # 工具调用时确定性合成 web_search。按用户原则（2026-07-20：禁止
            # 硬编码/机械执行，判断留给 LLM）与 conv 86e51bbd 实证，此处不再
            # 合成工具调用（模型拥有执行工具的自主权）。
            #
            # 反向缺口（用户问题 2026-08-10："会不会导致用户不说联网搜索就
            # 绝对不搜索"）：coordinator 已判定用户明确要求检索
            # (search_required=True，LLM 判定、非正则)，模型却静默凭记忆回答
            # ——这是对用户意图的违背，必须确定性提醒。Agentic 语义：注入
            # directive 要求模型执行检索（工具调用由模型自主发出，不合成），
            # 而非静默放行。auditor LLM 的模板第 4 条同时把关"用户要求检索
            # 却未调用搜索工具 → 草稿不合格"。
            # conv 97ff355d: the gate used web_search_count == 0 only, so a
            # turn that did 20+ real BROWSER retrievals was still told
            # "你尚未调用 web_search 工具" — a factually wrong directive that
            # fed the model's confusion spiral. Search-class = web_search OR
            # browser* (mirrors auditor template rule 4).
            if (
                not tool_calls_collected
                and not _search_class_tool_used(state)
                and state.search_required is True
                and not self._skip_guardrails()
                and "web_search" in self._active_tool_names
                and config.web_search_enabled
            ):
                if assistant_content and not state.search_demanded:
                    logger.info(
                        "search_required=True but model produced no web_search "
                        "(iterations=%d) — injecting search-demand directive "
                        "(agentic execution, no synthesis)",
                        state.iterations,
                    )
                    state.search_demanded = True
                    yield {
                        "agent_step": {
                            "name": "search_demand",
                            "title": "需要联网检索",
                            "content": "协调器判定本轮需要联网检索，但助手尚未调用搜索工具。",
                            "step_type": "system",
                        }
                    }
                    _prune_guardrail_pairs(state)
                    _rejected_append(self, state, assistant_content, reasoning_content)
                    # A4.9 R1 M5: mirror the audit-reject path — the rejected
                    # draft must not linger in turn_content_segments and leak
                    # into _current_turn_context / a later stitched audit.
                    state.turn_content_segments.clear()
                    # 输出单元契约（2026-10-14）：打回即代际边界——relay 侧
                    # 累积同步清零（audit_reset），否则旧稿+重答并存=双倍输出。
                    yield {"audit_reset": True}
                    _inject_directive(
                        state,
                        _search_demand_directive(state.turn_question),
                        _ephemeral="search_demand",
                    )
                    continue
            # ── Tool-demand: coordinator judged the turn needs tools, model
            # made zero tool calls ──
            # conv 41d2790d 2026-08-10: coordinator said expects_tools=True
            # ("需要调用工具读取工作区文件列表并生成mermaid树状图") but the
            # model answered from memory for 3 iterations without calling ANY
            # tool — the file tree it produced was grounded in turn-1 tool
            # results, but the auditor (blind to those results) accused it of
            # fabrication, and the final hollow draft shipped un-audited.
            # Agentic nudge, never synthesized: a directive reminding the
            # model to call the appropriate tool when it has dodged every
            # tool call on a coordinator-judged tool-requiring turn. Bounded:
            # fires at most once per turn (tool_demanded), then the
            # auditor/rejection budget governs. Requires at least ONE audit
            # rejection first (a draft that passed the auditor is never
            # nudged — A4.9 finding: firing before the auditor wasted a
            # regeneration on grounded rewrites), and is skipped on
            # length-continuation/canary-retry iterations.
            if (
                not tool_calls_collected
                and state.total_tool_calls == 0
                and state.expects_tools is True
                and state.audit_rejections >= 1
                and not state.tool_demanded
                and not self._skip_guardrails()
                and not state.creative_turn
            ):
                state.tool_demanded = True
                logger.info(
                    "expects_tools=True but zero tool calls (iterations=%d) — "
                    "injecting tool-demand directive (agentic execution, no synthesis)",
                    state.iterations,
                )
                yield {
                    "agent_step": {
                        "name": "tool_demand",
                        "title": "需要调用工具",
                        "content": "协调器判定本轮回答需要调用工具获取数据，但助手尚未调用任何工具。",
                        "step_type": "system",
                    }
                }
                if assistant_content:
                    _prune_guardrail_pairs(state)
                    _rejected_append(self, state, assistant_content, reasoning_content)
                    # A4.9 R1 M5 (same class as search_demand): keep rejected
                    # drafts out of turn_content_segments.
                    state.turn_content_segments.clear()
                    # 输出单元契约（2026-10-14）：同 search_demand——打回即
                    # 代际边界，relay 累积同步清零，杜绝双倍输出。
                    yield {"audit_reset": True}
                _inject_directive(
                    state,
                    _tool_demand_directive(state.turn_question),
                    _ephemeral="tool_demand",
                )
                continue

            _loop_result = _LoopResult()
            for _name in ('_', '_args_error', '_c', '_cp_assistant', '_decision', '_digest_ev', '_digest_out', '_dispatch_plan', '_dm_should_continue', '_ev', '_eval_content', '_guarded', '_halt_message', '_halted', '_hard', '_parallel_cancelled', '_r', '_res', '_results_by_id', '_safe_tool_calls', '_tool_summary', '_tool_summary_parts', '_tr', '_truncated_result', '_turn_text', '_turn_tr', '_wave', '_wave_ids', '_waves', 'assistant_msg', 'can_parallel', 'coros', 'decision', 'dm', 'done', 'has_budget_refund_tool', 'i', 'interrupted_result', 'matched', 'ordered_tool_messages', 'par_task', 'r', 'raw_results', 'repaired', 'result', 'results', 'results_digested', 'retry_result', 'tc', 'tc_id', 'tm', 'tool_args', 'tool_calls_collected', 'tool_messages', 'tool_name', 'tool_results_map', 'tool_task', 'valid_tool_calls', 'wave_coros'):
                if _name in locals():
                    setattr(_loop_result, _name, locals()[_name])
            _kw = locals()
            try:
                try:
                    async for _ev in self._execute_tool_block(
                        state, _loop_result, **{k: _kw[k] for k in ('_name', 'assistant', 'assistant_content', 'conversation', 'reasoning_content', 'session_factory', 'user') if k in _kw},
                    ):
                        yield _ev
                finally:
                    _args_error = _loop_result._args_error
                    _c = _loop_result._c
                    _cp_assistant = _loop_result._cp_assistant
                    _decision = _loop_result._decision
                    _digest_ev = _loop_result._digest_ev
                    _digest_out = _loop_result._digest_out
                    _dispatch_plan = _loop_result._dispatch_plan
                    _dm_should_continue = _loop_result._dm_should_continue
                    _ev = _loop_result._ev
                    _eval_content = _loop_result._eval_content
                    _guarded = _loop_result._guarded
                    _halt_message = _loop_result._halt_message
                    _halted = _loop_result._halted
                    _hard = _loop_result._hard
                    _parallel_cancelled = _loop_result._parallel_cancelled
                    _r = _loop_result._r
                    _res = _loop_result._res
                    _results_by_id = _loop_result._results_by_id
                    _safe_tool_calls = _loop_result._safe_tool_calls
                    _tool_summary = _loop_result._tool_summary
                    _tool_summary_parts = _loop_result._tool_summary_parts
                    _tr = _loop_result._tr
                    _truncated_result = _loop_result._truncated_result
                    _turn_text = _loop_result._turn_text
                    _turn_tr = _loop_result._turn_tr
                    _wave = _loop_result._wave
                    _wave_ids = _loop_result._wave_ids
                    _waves = _loop_result._waves
                    assistant_msg = _loop_result.assistant_msg
                    can_parallel = _loop_result.can_parallel
                    coros = _loop_result.coros
                    decision = _loop_result.decision
                    dm = _loop_result.dm
                    done = _loop_result.done
                    has_budget_refund_tool = _loop_result.has_budget_refund_tool
                    i = _loop_result.i
                    interrupted_result = _loop_result.interrupted_result
                    matched = _loop_result.matched
                    ordered_tool_messages = _loop_result.ordered_tool_messages
                    par_task = _loop_result.par_task
                    r = _loop_result.r
                    raw_results = _loop_result.raw_results
                    repaired = _loop_result.repaired
                    result = _loop_result.result
                    results = _loop_result.results
                    results_digested = _loop_result.results_digested
                    retry_result = _loop_result.retry_result
                    tc = _loop_result.tc
                    tc_id = _loop_result.tc_id
                    tm = _loop_result.tm
                    tool_args = _loop_result.tool_args
                    tool_calls_collected = _loop_result.tool_calls_collected
                    tool_messages = _loop_result.tool_messages
                    tool_name = _loop_result.tool_name
                    tool_results_map = _loop_result.tool_results_map
                    tool_task = _loop_result.tool_task
                    valid_tool_calls = _loop_result.valid_tool_calls
                    wave_coros = _loop_result.wave_coros
            except _LoopContinueSignal:
                continue
            except _LoopBreakSignal:
                break
            except _LoopReturnSignal:
                return

            state.consecutive_tool_iterations = 0
            _loop_result = _LoopResult()
            for _name in ('_adv_ev', '_adv_events', '_adv_text', '_audit_family', '_audit_reject_budget', '_audit_target', '_base_text', '_budget_spent', '_c_after', '_c_before', '_canary_retry', '_conv_key', '_cov_note', '_ctx_len', '_disabled', '_dm_content', '_dm_should_continue', '_ev', '_family_stall', '_final_content', '_g_blocked', '_g_ev', '_g_events', '_hit', '_is_repeating', '_last_user_msg', '_lc_note', '_log_miss', '_marker', '_min_ratio', '_misses', '_need_synthesis', '_npg_no_progress', '_prev_stash', '_ratio', '_reset_ev', '_restart', '_salvage_ev', '_salvage_parts', '_search_only', '_shipped_guidance', '_shipped_text', '_ships_as_final', '_soft_limit', '_stitched_audit', '_synth_guidance', '_synth_shipped_guidance', '_threshold', '_turns', 'assistant_content', 'compressed', 'decision', 'dm', 'event', 'guidance', 'msg', 'rep_prompt', 'retry_prompt'):
                if _name in locals():
                    setattr(_loop_result, _name, locals()[_name])
            _kw = locals()
            try:
                try:
                    async for _ev in self._finalize_answer_block(
                        state, _loop_result, **{k: _kw[k] for k in ('assistant_raw_content', 'conversation', 'reasoning_content', 'user') if k in _kw},
                    ):
                        yield _ev
                finally:
                    _adv_ev = _loop_result._adv_ev
                    _adv_events = _loop_result._adv_events
                    _adv_text = _loop_result._adv_text
                    _audit_family = _loop_result._audit_family
                    _audit_reject_budget = _loop_result._audit_reject_budget
                    _audit_target = _loop_result._audit_target
                    _base_text = _loop_result._base_text
                    _budget_spent = _loop_result._budget_spent
                    _c_after = _loop_result._c_after
                    _c_before = _loop_result._c_before
                    _canary_retry = _loop_result._canary_retry
                    _conv_key = _loop_result._conv_key
                    _cov_note = _loop_result._cov_note
                    _ctx_len = _loop_result._ctx_len
                    _disabled = _loop_result._disabled
                    _dm_content = _loop_result._dm_content
                    _dm_should_continue = _loop_result._dm_should_continue
                    _ev = _loop_result._ev
                    _family_stall = _loop_result._family_stall
                    _final_content = _loop_result._final_content
                    _g_blocked = _loop_result._g_blocked
                    _g_ev = _loop_result._g_ev
                    _g_events = _loop_result._g_events
                    _hit = _loop_result._hit
                    _is_repeating = _loop_result._is_repeating
                    _last_user_msg = _loop_result._last_user_msg
                    _lc_note = _loop_result._lc_note
                    _log_miss = _loop_result._log_miss
                    _marker = _loop_result._marker
                    _min_ratio = _loop_result._min_ratio
                    _misses = _loop_result._misses
                    _need_synthesis = _loop_result._need_synthesis
                    _npg_no_progress = _loop_result._npg_no_progress
                    _prev_stash = _loop_result._prev_stash
                    _ratio = _loop_result._ratio
                    _reset_ev = _loop_result._reset_ev
                    _restart = _loop_result._restart
                    _salvage_ev = _loop_result._salvage_ev
                    _salvage_parts = _loop_result._salvage_parts
                    _search_only = _loop_result._search_only
                    _shipped_guidance = _loop_result._shipped_guidance
                    _shipped_text = _loop_result._shipped_text
                    _ships_as_final = _loop_result._ships_as_final
                    _soft_limit = _loop_result._soft_limit
                    _stitched_audit = _loop_result._stitched_audit
                    _synth_guidance = _loop_result._synth_guidance
                    _synth_shipped_guidance = _loop_result._synth_shipped_guidance
                    _threshold = _loop_result._threshold
                    _turns = _loop_result._turns
                    assistant_content = _loop_result.assistant_content
                    compressed = _loop_result.compressed
                    decision = _loop_result.decision
                    dm = _loop_result.dm
                    event = _loop_result.event
                    guidance = _loop_result.guidance
                    msg = _loop_result.msg
                    rep_prompt = _loop_result.rep_prompt
                    retry_prompt = _loop_result.retry_prompt
            except _LoopContinueSignal:
                continue
            except _LoopBreakSignal:
                break
            except _LoopReturnSignal:
                return
            return

        if state.completed_normally:
            # Normal break (final answer produced): NOT an exhaustion — the
            # legacy unconditional warning below made every healthy run look
            # like a budget failure in the logs (misleading during the conv
            # daa19eac truncation investigation).
            yield {"done": True}
            return
        logger.warning("Agent loop budget exhausted (iterations=%d, tool_calls=%d)",
                       state.iterations, state.total_tool_calls)
        # Deathmatch mode: budget exhaustion is not a stop condition.
        # Evaluate the goal, inject continuation if needed, and keep going.
        if self._skip_guardrails():
            dm = self.deathmatch_manager
            try:
                last_content = assistant_content
            except NameError:
                last_content = ""

            # Repetition detection for budget exhaustion path
            _is_repeating = self._detect_repetition(state, last_content)
            self._track_response(state, last_content)
            if _is_repeating:
                state.repetition_count += 1

            if last_content:
                state.messages.append({"role": "assistant", "content": last_content})
            decision = await self._safe_judge(
                dm, last_content,
                user_initiated=False,
                workspace_path=self.workspace_path or "",
                state=state,
            )
            async for _ev in self._emit_deathmatch_verdict(dm, decision):
                yield _ev
            self._mark_activity(state)
            if decision.get("should_continue") and decision.get("continuation_prompt"):
                # Use repetition-detected prompt if agent is repeating itself
                if state.repetition_count >= 2:
                    rep_prompt = dm.get_repetition_prompt()
                    if rep_prompt:
                        _inject_directive(state, rep_prompt)
                        if (await state.budget.get_remaining()) <= 0:
                            await state.budget.refund()
                        state.budget_grace_call = True
                        async for event in self._grace_call(state):
                            yield event
                        return
                _reset_ev = await self._maybe_dm_context_reset(dm, state)
                if _reset_ev is not None:
                    yield _reset_ev
                _inject_directive(state, decision["continuation_prompt"])
                if (await state.budget.get_remaining()) <= 0:
                    await state.budget.refund()
                state.budget_grace_call = True
                async for event in self._grace_call(state):
                    yield event
                return
            elif decision.get("needs_compression_retry") or decision.get("needs_restart"):
                _restart = decision.get("needs_restart", False)
                logger.info(
                    "Deathmatch %s (budget): turns=%d messages=%d",
                    "restart" if _restart else "compression retry",
                    dm._conv.deathmatch_turns or 0, len(state.messages),
                )
                compressed = await dm.compress_messages(state.messages)
                state.messages = compressed
                retry_prompt = dm.get_continuation_prompt(last_content)
                if retry_prompt:
                    _inject_directive(state, retry_prompt)
                if (await state.budget.get_remaining()) <= 0:
                    await state.budget.refund()
                state.budget_grace_call = True
                async for event in self._grace_call(state):
                    yield event
                return
            elif decision.get("verdict") == "done":
                async for _ev in self._handle_deathmatch_done(dm, decision, state):
                    yield _ev
                return
            else:
                yield {"done": True}
                return
        # If the loop completed normally (produced a full answer without
        # exhausting the budget), do NOT run the grace call — it would
        # generate a duplicate response concatenated with the existing one.
        # (Handled by the early return above; kept as a defensive re-check.)
        if state.completed_normally:
            yield {"done": True}
            return
        # FIX: Always attempt grace call when budget is exhausted, even if tool_results is empty
        # This ensures we get some response rather than abrupt termination
        if state.messages:
            state.budget_grace_call = True
            async for event in self._grace_call(state):
                yield event
        else:
            yield {"done": True, "max_iterations_reached": True}
