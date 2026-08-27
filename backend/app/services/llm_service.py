# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from openai import AsyncOpenAI
from typing import Optional, AsyncIterator, Dict, List, Any
import asyncio
import json
import re
import logging
from app.core.config import get_config

logger = logging.getLogger(__name__)

_THINK_OPEN = re.compile(r'<think>')
_THINK_CLOSE = re.compile(r'</think>')
_STREAM_HEARTBEAT_INTERVAL = 30.0


async def _heartbeat_wrapped(anext, interval: float = _STREAM_HEARTBEAT_INTERVAL):
    """Wrap an SDK stream's ``__anext__`` with a heartbeat.

    A stalled upstream (server hung mid-generation, conv efaf8f9c
    2026-08-21: zero chunks for 60+ min — the turn never ended and the
    conversation slot stayed reserved forever) blocks ``async for``
    indefinitely, so the CONSUMER's wall-clock guards (agent_loop's
    per-iteration timeout / inactivity checks, which run per event) never
    execute. Yield a heartbeat sentinel whenever no real chunk arrives
    within ``interval`` seconds — the consumer skips sentinels and then
    evaluates its own timeout logic. Real chunks pass through unchanged.

    2026-08-22 (conv efaf8f9c root cause): the previous implementation used
    ``asyncio.wait_for(anext(), timeout=interval)`` — on timeout wait_for
    CANCELS the in-flight coroutine. The openai SDK's ``AsyncStream``
    drives its ``__stream__`` async generator whose ``finally`` calls
    ``await response.aclose()``: the cancellation propagates into the
    generator, closes the HTTP connection, and the NEXT ``__anext__``
    raises StopAsyncIteration — the stream "ends" cleanly with ZERO chunks.
    Any iteration whose TTFT exceeds the interval then dies as an empty
    answer (qwen3.8-27b private deployment TTFT ≈ 33s > 30s interval).
    Fix: keep a persistent task and wait with ``asyncio.wait`` (which never
    cancels on timeout) — the pending read survives heartbeats and the
    real chunk still arrives. The task is only cancelled when the consumer
    closes the generator (stop button / abort), which is the intended
    close-the-connection path.
    """
    pending: Optional[asyncio.Task] = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield {"type": "heartbeat", "data": None}
                continue
            pending = None
            try:
                chunk = done.pop().result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, Exception):
                pass


PRESERVE_THINKING_PROVIDERS = ("qwen3.8_vllm", "deepseek", "mimo")


class LLMService:
    def __init__(self, custom_api_url: str = None, custom_api_key: str = None, custom_model_name: str = None, preserve_reasoning: bool = False):
        self.config = get_config()
        self.is_custom_provider = bool(custom_api_url)
        # P0/PHASE 1B (2026-08-21): providers with preserve-thinking semantics
        # (qwen3.8_vllm chat_template_kwargs.preserve_thinking / deepseek
        # reasoning_content round-trip / mimo 思考链) keep the CURRENT turn's
        # assistant reasoning_content on the wire; see _build_params.
        self.preserve_reasoning = preserve_reasoning
        base_url = custom_api_url or self.config.api_base_url
        api_key = custom_api_key or self.config.api_key
        self.custom_model_name = custom_model_name
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key if api_key else "dummy-key-for-header"
        )
        logger.info("LLMService created: base_url=%s, model=%s, is_custom=%s",
                     base_url, custom_model_name or self.config.model_name, self.is_custom_provider)

    def _build_params(self, messages: list, **kwargs) -> dict:
        model = kwargs.get("model") or self.custom_model_name or self.config.model_name or "gpt-3.5-turbo"
        # PHASE 1B (A2, 2026-08-21): preserve-thinking providers keep the
        # CURRENT turn's assistant reasoning_content (everything after the
        # last real user message — audit-retry drafts, tool-chain turns) so
        # the model continues its own reasoning instead of cold-restarting;
        # reasoning from EARLIER turns is stripped (DeepSeek ignores it
        # across user boundaries anyway; bounds context growth).
        _last_user_idx = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user" and not m.get("synthetic"):
                _last_user_idx = i
        # A4.9 Important-4 (round-2 fix): aggregate cap on current-turn
        # reasoning kept on the wire. Per-draft reasoning is already capped at
        # append time, but long tool chains (max_iterations=50) could still
        # pile up unbounded xhigh reasoning. Keep the MOST RECENT reasoning
        # up to the budget (continuation value concentrates at the tail): two
        # passes — total first, then strip from the FRONT (oldest) until the
        # total fits. Tool-call turns keep an empty marker so the DeepSeek 400
        # contract holds regardless.
        _turn_reasoning_budget = (
            4 * self.config.agent_audit_retry_reasoning_keep_chars
            if self.preserve_reasoning and self.config.agent_audit_retry_reasoning_keep_chars > 0
            else 0
        )
        _turn_total_rc = 0
        if _turn_reasoning_budget > 0:
            for i, m in enumerate(messages):
                if i > _last_user_idx and m.get("role") == "assistant" and not m.get("tool_calls"):
                    _turn_total_rc += len(m.get("reasoning_content") or "")
        validated = []
        for i, msg in enumerate(messages):
            # DeepSeek thinking mode + tools REQUIRE the assistant message's
            # reasoning_content to be passed back in every subsequent request
            # (docs: https://api-docs.deepseek.com/zh-cn/guides/thinking_mode);
            # omitting it on a tool-call turn returns 400
            # "The `reasoning_content` in the thinking mode must be passed back".
            # Non-tool reasoning: stripped unless preserve_reasoning is on AND
            # the message belongs to the current turn AND the aggregate budget
            # still covers it (front-to-back stripping keeps the newest).
            if "reasoning_content" in msg and not msg.get("tool_calls"):
                _keep = self.preserve_reasoning and i > _last_user_idx
                if _keep and _turn_reasoning_budget > 0:
                    if _turn_total_rc > _turn_reasoning_budget:
                        _turn_total_rc -= len(msg.get("reasoning_content") or "")
                        _keep = False
                if not _keep:
                    msg = {k: v for k, v in msg.items() if k != "reasoning_content"}
            if any(k.startswith("_") for k in msg.keys()):
                msg = {k: v for k, v in msg.items() if not k.startswith("_")}
            if "synthetic" in msg:
                # Internal marker for harness-injected directives (4.8) —
                # never leak bookkeeping keys into provider payloads.
                msg = {k: v for k, v in msg.items() if k != "synthetic"}
            if msg.get("role") == "system" and validated:
                # Strict chat templates (Qwen on vLLM) 400 on any system
                # message that is not the FIRST message: "System message must
                # be at the beginning." (conv 692deb04, 2026-08-14). Synthetic
                # directives (turn focus, guardrails, continuations) are
                # appended mid-conversation by _inject_directive, and the
                # audit-salvage prompt is appended at the end — demote them
                # to role="user" on the wire so content/position survive
                # without violating the template. DeepSeek (default) is
                # unaffected: it accepts mid-array system messages.
                msg = {**msg, "role": "user"}
            if msg.get("role") == "assistant" and not msg.get("content") and not msg.get("tool_calls"):
                msg = {**msg, "content": "[...]"}
            validated.append(msg)
        params = {"model": model, "messages": validated}

        response_format = kwargs.get("response_format")
        if response_format is not None:
            params["response_format"] = response_format

        use_defaults = not self.is_custom_provider

        temperature = kwargs.get("temperature")
        if temperature is not None:
            params["temperature"] = temperature
        elif use_defaults and self.config.default_temperature is not None:
            params["temperature"] = self.config.default_temperature

        top_p = kwargs.get("top_p")
        if top_p is not None:
            params["top_p"] = top_p
        elif use_defaults and self.config.default_top_p is not None:
            params["top_p"] = self.config.default_top_p

        presence_penalty = kwargs.get("presence_penalty")
        if presence_penalty is not None:
            params["presence_penalty"] = presence_penalty
        elif use_defaults and self.config.default_presence_penalty is not None:
            params["presence_penalty"] = self.config.default_presence_penalty

        frequency_penalty = kwargs.get("frequency_penalty")
        if frequency_penalty is not None:
            params["frequency_penalty"] = frequency_penalty
        elif use_defaults and self.config.default_frequency_penalty is not None:
            params["frequency_penalty"] = self.config.default_frequency_penalty

        # top_k / min_p / repetition_penalty are OpenAI-compatible EXTENSION
        # params (accepted by vLLM etc.) but the openai SDK rejects them as
        # create() kwargs — they must travel inside extra_body, which the SDK
        # merges into the JSON body verbatim.
        _sdk_extra: Dict[str, Any] = {}
        top_k = kwargs.get("top_k")
        if top_k is not None:
            _sdk_extra["top_k"] = top_k
        min_p = kwargs.get("min_p")
        if min_p is not None:
            _sdk_extra["min_p"] = min_p
        repetition_penalty = kwargs.get("repetition_penalty")
        if repetition_penalty is not None:
            _sdk_extra["repetition_penalty"] = repetition_penalty

        max_tokens = kwargs.get("max_tokens")
        # 用户原则（2026-08-18）：不设置 == 默认最大输出长度。""/0/None 一律
        # 视为不设置——只有显式正整数才会下发 max_tokens。
        if max_tokens in (None, "", 0):
            max_tokens = None
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        elif use_defaults and self.config.default_max_tokens is not None:
            params["max_tokens"] = self.config.default_max_tokens

        response_format = kwargs.get("response_format")
        if response_format is not None:
            params["response_format"] = response_format

        tools = kwargs.get("tools")
        if tools is not None:
            params["tools"] = tools
            if tools:
                params["tool_choice"] = "auto"

        extra_body = kwargs.get("extra_body")
        if extra_body is not None:
            if self.is_custom_provider:
                extra_body = dict(extra_body)
                base_url = str(self.client.base_url).lower() if self.client.base_url else ""
                is_dashscope = "dashscope" in base_url or "aliyuncs" in base_url
                if "enable_thinking" in extra_body and not is_dashscope:
                    et_val = extra_body.pop("enable_thinking")
                    existing_ctk = extra_body.get("chat_template_kwargs", {})
                    if isinstance(existing_ctk, dict):
                        existing_ctk["enable_thinking"] = et_val
                        extra_body["chat_template_kwargs"] = existing_ctk
                    else:
                        extra_body["chat_template_kwargs"] = {"enable_thinking": et_val}
                # Qwen models on OpenAI-compatible custom endpoints (vLLM)
                # ignore the OpenAI-style `thinking` param entirely — translate
                # it (and thinking_budget) into chat_template_kwargs, otherwise
                # "disable thinking" silently does nothing and the model burns
                # the token budget on hidden reasoning (title generation broke
                # this way: empty content, 200 tokens of reasoning).
                if not is_dashscope and "qwen" in (self.custom_model_name or "").lower():
                    thinking_cfg = extra_body.get("thinking")
                    if isinstance(thinking_cfg, dict) and "type" in thinking_cfg:
                        existing_ctk = extra_body.get("chat_template_kwargs", {})
                        if not isinstance(existing_ctk, dict):
                            existing_ctk = {}
                        existing_ctk.setdefault(
                            "enable_thinking", thinking_cfg["type"] == "enabled"
                        )
                        extra_body["chat_template_kwargs"] = existing_ctk
                        extra_body.pop("thinking", None)
                    if "thinking_budget" in extra_body:
                        existing_ctk = extra_body.get("chat_template_kwargs", {})
                        if not isinstance(existing_ctk, dict):
                            existing_ctk = {}
                        existing_ctk["thinking_budget"] = extra_body.pop("thinking_budget")
                        extra_body["chat_template_kwargs"] = existing_ctk
                extra_body.update(_sdk_extra)
                params["extra_body"] = extra_body
            else:
                params["extra_body"] = {**extra_body, **_sdk_extra}
        elif _sdk_extra:
            params["extra_body"] = _sdk_extra

        return params

    async def complete_chat(self, messages: list, **kwargs) -> str:
        content, _ = await self.complete_chat_parts(messages, **kwargs)
        return content

    async def complete_chat_parts(self, messages: list, **kwargs) -> tuple[str, str]:
        params = self._build_params(messages, **kwargs)
        logger.info("complete_chat called: model=%s, base_url=%s, is_custom=%s",
                     params.get("model"), self.client.base_url, self.is_custom_provider)
        response = await self.client.chat.completions.create(**params)
        if not response.choices:
            return "", ""

        message = response.choices[0].message
        raw_rc = getattr(message, "reasoning_content", None)
        if not raw_rc:
            raw_rc = getattr(message, "reasoning", None)
        content = message.content or ""
        if not content.strip() and raw_rc and not (getattr(message, "tool_calls", None) or []):
            # Thinking model (e.g. qwen3.6_27b on vLLM) burned the whole
            # max_tokens budget on reasoning -> content is None with
            # finish_reason="length". HTTP 200 but nothing usable; retry once
            # with thinking OFF so structured-output callers (memory
            # extraction/dream/clarification/...) get real content.
            content = await self._retry_without_thinking(messages, kwargs, response, raw_rc)
        return content, raw_rc or ""

    async def _retry_without_thinking(
        self, messages: list, kwargs: dict, first_response, reasoning_snippet: str,
    ) -> str:
        extra_body = kwargs.get("extra_body")
        if self._extra_body_disables_thinking(extra_body):
            finish_reason = None
            try:
                finish_reason = getattr(first_response.choices[0], "finish_reason", None)
            except Exception:
                pass
            logger.warning(
                "Empty content with reasoning (finish_reason=%s, reasoning_len=%d) and "
                "thinking already disabled — giving up, no retry",
                finish_reason, len(reasoning_snippet or ""),
            )
            return ""

        retry_kwargs = dict(kwargs)
        retry_kwargs["extra_body"] = self._thinking_off_extra_body()
        if kwargs.get("max_tokens"):
            retry_kwargs["max_tokens"] = int(kwargs["max_tokens"]) * 2
        finish_reason = None
        try:
            finish_reason = getattr(first_response.choices[0], "finish_reason", None)
        except Exception:
            pass
        logger.warning(
            "Empty content with reasoning (finish_reason=%s, reasoning_len=%d) — "
            "retrying once with thinking disabled, extra_body=%s",
            finish_reason, len(reasoning_snippet or ""), retry_kwargs["extra_body"],
        )
        try:
            retry_response = await self.client.chat.completions.create(
                **self._build_params(messages, **retry_kwargs)
            )
        except Exception as e:
            logger.warning("Retry-with-thinking-off failed: %s", e)
            return ""
        if not retry_response.choices:
            return ""
        return retry_response.choices[0].message.content or ""

    @staticmethod
    def _extra_body_disables_thinking(extra_body) -> bool:
        if not isinstance(extra_body, dict):
            return False
        ctk = extra_body.get("chat_template_kwargs")
        if isinstance(ctk, dict) and ctk.get("enable_thinking") is False:
            return True
        if extra_body.get("enable_thinking") is False:
            return True
        thinking = extra_body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "disabled":
            return True
        return False

    def _thinking_off_extra_body(self) -> dict:
        model = self.custom_model_name or self.config.model_name or ""
        base_url = str(self.client.base_url or "").lower()
        if "dashscope" in base_url or "aliyuncs" in base_url:
            return {"enable_thinking": False}
        if "qwen" in model.lower():
            # vLLM Qwen3 ignores top-level enable_thinking — only
            # chat_template_kwargs works (verified live 2026-08-04).
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {"thinking": {"type": "disabled"}}

    async def stream_chat(self, messages: list, **kwargs) -> AsyncIterator[str]:
        logger.info("stream_chat called: model=%s, base_url=%s, is_custom=%s",
                     kwargs.get("model") or self.custom_model_name or self.config.model_name,
                     self.client.base_url, self.is_custom_provider)

        async for event in self.stream_chat_structured(messages, **kwargs):
            event_type = event["type"]
            event_data = event["data"]
            if event_type == "reasoning":
                yield f"data: {json.dumps({'reasoning_content': event_data})}\n\n"
            elif event_type == "content":
                yield f"data: {json.dumps({'content': event_data})}\n\n"
            elif event_type == "done":
                yield "data: [DONE]\n\n"
                break
            elif event_type == "error":
                yield f"data: [ERROR] {event_data}\n\n"
                break

    async def stream_chat_structured(
        self, messages: list, **kwargs
    ) -> AsyncIterator[Dict[str, Any]]:
        params = self._build_params(messages, **kwargs)
        params["stream"] = True
        tools = kwargs.get("tools")
        if tools is not None:
            params["tools"] = tools

        try:
            response = await self.client.chat.completions.create(**params)
            inside_think = False
            think_buf = ""
            tool_calls_accumulated: List[Dict[str, Any]] = []
            chunk = None

            async for chunk in _heartbeat_wrapped(response.__anext__):
                if isinstance(chunk, dict) and chunk.get("type") == "heartbeat":
                    # Consumer-side wall-clock guards (iteration timeout,
                    # inactivity) need a wake-up even when the upstream is
                    # silent — pass the sentinel through.
                    yield chunk
                    continue
                if not chunk.choices or not chunk.choices[0].delta:
                    continue

                delta = chunk.choices[0].delta

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        while len(tool_calls_accumulated) <= idx:
                            tool_calls_accumulated.append({
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                        if tc.id:
                            tool_calls_accumulated[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_accumulated[idx]["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_accumulated[idx]["function"]["arguments"] += tc.function.arguments
                    continue

                reasoning = getattr(delta, 'reasoning_content', None)
                if not reasoning:
                    reasoning = getattr(delta, 'reasoning', None)
                if reasoning:
                    yield {"type": "reasoning", "data": reasoning}
                    continue

                if delta.content:
                    text = delta.content

                    if _THINK_OPEN.search(text) and not inside_think:
                        pre, _, post = text.partition('<think>')
                        if pre:
                            yield {"type": "content", "data": pre}
                        inside_think = True
                        think_buf = post
                        if _THINK_CLOSE.search(think_buf):
                            inside_think = False
                            parts = think_buf.split('</think>', 1)
                            if len(parts) > 1 and parts[1]:
                                yield {"type": "content", "data": parts[1]}
                            think_buf = ""
                        continue

                    if inside_think:
                        think_buf += text
                        if _THINK_CLOSE.search(think_buf):
                            inside_think = False
                            parts = think_buf.split('</think>', 1)
                            if len(parts) > 1 and parts[1]:
                                yield {"type": "content", "data": parts[1]}
                            think_buf = ""
                        continue

                    yield {"type": "content", "data": text}

            finish_reason = None
            if chunk is not None and hasattr(chunk, 'choices') and chunk.choices:
                fr = getattr(chunk.choices[0], 'finish_reason', None)
                if fr is not None:
                    finish_reason = fr

            if tool_calls_accumulated and any(
                tc.get("function", {}).get("name") for tc in tool_calls_accumulated
            ):
                valid = [
                    tc for tc in tool_calls_accumulated
                    if tc.get("function", {}).get("name") and tc.get("id")
                ]
                if valid:
                    for tc in valid:
                        args_raw = tc.get("function", {}).get("arguments", "")
                        if args_raw and not args_raw.rstrip().endswith("}"):
                            logger.warning(
                                "Tool call %s has truncated JSON arguments (doesn't end with })",
                                tc.get("function", {}).get("name", "?"),
                            )
                    yield {"type": "tool_calls", "data": valid}

            yield {"type": "done", "data": {"finish_reason": finish_reason}}

        except Exception as e:
            yield {"type": "error", "data": str(e)}
