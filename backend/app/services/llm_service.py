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


# PRESERVE_THINKING_PROVIDERS —— 派生自 model_gateway.profiles（单一事实源在
# 各 profile 的 preserve_thinking 标志；保留此常量仅为向后兼容的导入面）。
from app.model_gateway.profiles import preserve_thinking_provider_types as _pt_types

PRESERVE_THINKING_PROVIDERS = _pt_types()


class LLMService:
    def __init__(self, custom_api_url: str = None, custom_api_key: str = None, custom_model_name: str = None, preserve_reasoning: bool = False, endpoint=None):
        self.config = get_config()
        self.is_custom_provider = bool(custom_api_url)
        # model_gateway 注入的端点元数据（alias/provider_type/capabilities）；
        # 仅作描述，不改变任何构造行为（解耦重构 W2）。
        self.endpoint = endpoint
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
        # 模型网关：custom 端点的 toml params 作为每端点默认采样（kwargs 优先；
        # 非 custom 端点走下方 [defaults] 全局默认，行为与 legacy 一致）。
        _ep = getattr(self, "endpoint", None)
        _ep_params = dict((_ep.params if (_ep is not None and self.is_custom_provider) else {}) or {})
        # 用户级模型供应商覆盖（2026-09-13）：用户在「系统设置 → 模型供应商」
        # 显式设置的采样参数优先级最高——注入 kwargs 覆盖 per-call 硬编码
        # （主回复 temperature=0.7、审计 0.0 等）。仅非 None 值生效。
        if _ep is not None:
            for _uk, _uv in dict(_ep.extra.get("user_params") or {}).items():
                if _uv is not None:
                    kwargs[_uk] = _uv
        # A4.9 R3 Minor①修复：max_tokens 前置归一化——0/""/"0" 与 kwargs 同义
        # （视为未设置），从端点参数中剔除使其自然回落 [defaults]。
        # 2026-08-31 W-2 Imp-3：同样的归一化扩展到 extra_body 扩展参数——
        # main 自包含化（is_custom=true）后 params.top_k="" 会在每次主链路
        # 调用的 wire 上携带 extra_body={"top_k": ""}，类型严格的 backend
        # （vLLM）会 400。
        for _norm_key in ("max_tokens", "top_k", "min_p", "repetition_penalty"):
            # min_p=0.0 是真实数据不是哨兵（Qwen3.8 模型卡预设；R5，2026-09-13
            # 审计 F-E）——与下方 kwargs 侧（`min_p in (None, "", "0")`）统一：
            # 端点 params 里的显式 0/0.0 必须送达 wire（llama.cpp 未设 min_p
            # 时用服务端非零默认）。其余键维持 0 视为未设置。
            _sentinel = (None, "", "0") if _norm_key == "min_p" else (None, "", 0, "0")
            if _ep_params.get(_norm_key) in _sentinel:
                _ep_params.pop(_norm_key, None)
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
        elif _ep_params.get("temperature") is not None:
            params["temperature"] = _ep_params["temperature"]
        elif use_defaults and self.config.default_temperature is not None:
            params["temperature"] = self.config.default_temperature

        top_p = kwargs.get("top_p")
        if top_p is not None:
            params["top_p"] = top_p
        elif _ep_params.get("top_p") is not None:
            params["top_p"] = _ep_params["top_p"]
        elif use_defaults and self.config.default_top_p is not None:
            params["top_p"] = self.config.default_top_p

        presence_penalty = kwargs.get("presence_penalty")
        if presence_penalty is not None:
            params["presence_penalty"] = presence_penalty
        elif _ep_params.get("presence_penalty") is not None:
            params["presence_penalty"] = _ep_params["presence_penalty"]
        elif use_defaults and self.config.default_presence_penalty is not None:
            params["presence_penalty"] = self.config.default_presence_penalty

        frequency_penalty = kwargs.get("frequency_penalty")
        if frequency_penalty is not None:
            params["frequency_penalty"] = frequency_penalty
        elif _ep_params.get("frequency_penalty") is not None:
            params["frequency_penalty"] = _ep_params["frequency_penalty"]
        elif use_defaults and self.config.default_frequency_penalty is not None:
            params["frequency_penalty"] = self.config.default_frequency_penalty

        # top_k / min_p / repetition_penalty are OpenAI-compatible EXTENSION
        # params (accepted by vLLM etc.) but the openai SDK rejects them as
        # create() kwargs — they must travel inside extra_body, which the SDK
        # merges into the JSON body verbatim.
        _sdk_extra: Dict[str, Any] = {}
        # 2026-08-31 Wave D（A4.9 W-2 deferred）：kwargs 侧与端点侧同款归一化——
        # 空串/0 视为未设置，防止 kwargs top_k="" 之类把空值送上 wire。
        # 注意元组成员用 == 判定：0/0.0/False 都会被吞（语义上均为 no-op/非法），
        # 真中性值 repetition_penalty=1.0 不受影响。
        top_k = kwargs.get("top_k")
        if top_k in (None, "", 0, "0"):
            top_k = _ep_params.get("top_k")
        if top_k not in (None, "", 0, "0"):
            _sdk_extra["top_k"] = top_k
        # min_p=0.0 是真实数据不是哨兵（Qwen3.8 模型卡采样预设；llama.cpp
        # 未设 min_p 时用服务端非零默认——0.0 被吞=偏离卡预设）。None/""/"0"
        # 仍视为未设置；显式数字 0/0.0 送达 wire（A4.9 qwen3.8_next 评审 Imp-2）。
        min_p = kwargs.get("min_p")
        if min_p in (None, "", "0"):
            min_p = _ep_params.get("min_p")
        if min_p not in (None, "", "0"):
            _sdk_extra["min_p"] = min_p
        repetition_penalty = kwargs.get("repetition_penalty")
        if repetition_penalty in (None, "", 0, "0"):
            repetition_penalty = _ep_params.get("repetition_penalty")
        if repetition_penalty not in (None, "", 0, "0"):
            _sdk_extra["repetition_penalty"] = repetition_penalty

        max_tokens = kwargs.get("max_tokens")
        # 用户原则（2026-08-18）：不设置 == 默认最大输出长度。""/0/"0"/None 一律
        # 视为不设置——只有显式正整数才会下发 max_tokens。
        if max_tokens in (None, "", 0, "0"):
            max_tokens = None
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        elif _ep_params.get("max_tokens") is not None:
            params["max_tokens"] = _ep_params["max_tokens"]
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
                # model_gateway.profiles 收口（wave-7）：vendor 归一化单点化为
                # sniff_thinking_profile（dashscope URL→qwen 透传 · qwen 模型名
                # →chat_template_kwargs 翻译 · 其他→enable_thinking 迁 ctk）。
                from app.model_gateway.profiles import sniff_thinking_profile
                _profile = sniff_thinking_profile(
                    str(self.client.base_url or ""), self.custom_model_name or ""
                )
                extra_body = _profile.normalize_user_extra_body(dict(extra_body))
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
        # model_gateway.profiles 收口（wave-7）：关思考的 wire 形状由 sniff 的
        # profile 统一给出（dashscope→enable_thinking False · qwen→ctk ·
        # 其他→thinking{type:disabled}）。
        from app.model_gateway.profiles import sniff_thinking_profile
        model = self.custom_model_name or self.config.model_name or ""
        base_url = str(self.client.base_url or "")
        return sniff_thinking_profile(base_url, model).disable()

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
        # P4 徽章真值（2026-09-02）：请求 usage 回传（OpenAI 兼容 stream_options），
        # 供应商不识别时在下方 create 处自动降级重试。flag=[llm] stream_include_usage。
        _include_usage = bool(getattr(self.config, "llm_stream_include_usage", True))
        if _include_usage:
            params["stream_options"] = {"include_usage": True}
        tools = kwargs.get("tools")
        if tools is not None:
            params["tools"] = tools

        try:
            try:
                response = await self.client.chat.completions.create(**params)
            except Exception as _first_err:
                _err_text = str(_first_err).lower()
                if _include_usage and (
                    "stream_options" in _err_text
                    or "unrecognized" in _err_text and "argument" in _err_text
                    or "unknown" in _err_text and "parameter" in _err_text
                    or "unexpected keyword" in _err_text
                ):
                    logger.warning(
                        "Provider rejected stream_options (%s); retrying without include_usage",
                        str(_first_err)[:160],
                    )
                    params.pop("stream_options", None)
                    try:
                        response = await self.client.chat.completions.create(**params)
                    except Exception as _retry_err:
                        # 遗留④：重试失败不掩蔽首错——两个错误都进错误信息。
                        raise Exception(
                            f"{_first_err}; (stream_options 降级重试亦失败: {_retry_err})"
                        ) from _retry_err
                else:
                    raise
            inside_think = False
            think_buf = ""
            tool_calls_accumulated: List[Dict[str, Any]] = []
            chunk = None
            _usage_sent = False
            _last_finish_reason = None

            async for chunk in _heartbeat_wrapped(response.__anext__):
                if isinstance(chunk, dict) and chunk.get("type") == "heartbeat":
                    # Consumer-side wall-clock guards (iteration timeout,
                    # inactivity) need a wake-up even when the upstream is
                    # silent — pass the sentinel through.
                    yield chunk
                    continue
                _usage = getattr(chunk, "usage", None)
                _pt = getattr(_usage, "prompt_tokens", 0) if _usage is not None else 0
                if _usage is not None and _pt and not _usage_sent:
                    _usage_sent = True  # A4.9 R1 M2：每 stream 只发一次
                    # 遗留①：prompt_tokens 缺失/0（null-usage 供应商）不发事件，
                    # 前端保留估算徽章——绝不出 measured tokens:0。
                    # DeepSeek: usage 随最后一个 content chunk；OpenAI: 独立空 choices 块。
                    yield {"type": "usage", "data": {
                        "prompt_tokens": _pt,
                        "completion_tokens": getattr(_usage, "completion_tokens", 0) or 0,
                        "prompt_cache_hit_tokens": getattr(_usage, "prompt_cache_hit_tokens", 0) or 0,
                        "prompt_cache_miss_tokens": getattr(_usage, "prompt_cache_miss_tokens", 0) or 0,
                    }}
                if chunk.choices and getattr(chunk.choices[0], "finish_reason", None) is not None:
                    _last_finish_reason = chunk.choices[0].finish_reason
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

            # A4.9 R1 M3：include_usage 的空 choices 末块会覆盖 chunk 变量，
            # finish_reason 取流中最后见到的非空值，不被 usage 块冲掉。
            finish_reason = _last_finish_reason

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
