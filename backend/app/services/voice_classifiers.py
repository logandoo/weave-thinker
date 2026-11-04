# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Voice classifier mixin (P5.5 move-only, 2026-10-18)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from app.prompts.voice import (
    VOICE_ASR_CORRECTION_HINT,
    VOICE_BARGE_IN_SYSTEM,
    VOICE_EOT_SYSTEM,
    VOICE_INTENT_SYSTEM,
    VOICE_INTERJECTION_SYSTEM,
)

logger = logging.getLogger(__name__)

_ASR_CORRECTION_HINT = VOICE_ASR_CORRECTION_HINT

_EMOTION_ENUM = ["calm", "interested", "excited", "upset", "broken"]


def _now() -> float:
    from app.services.voice_service import _now as _impl
    return _impl()


def _perf(phase: str, ms: float, **extra) -> None:
    from app.services.voice_service import _perf as _impl
    return _impl(phase, ms, **extra)


def _prox_evidence_line(near: bool) -> str:
    from app.services.voice_service import _prox_evidence_line as _impl
    return _impl(near)


def _json_schema_format(name: str, properties: dict, required: list) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }

_INTENT_RESPONSE_FORMAT = _json_schema_format(
    "voice_intent_verdict",
    {
        "should_respond": {"type": "boolean", "description": "是否值得助手开口回应"},
        "needs_tools": {"type": "boolean", "description": "是否需要调用工具"},
        "reason": {"type": "string", "description": "简短原因"},
    },
    ["should_respond", "needs_tools", "reason"],
)

_EOT_RESPONSE_FORMAT = _json_schema_format(
    "voice_eot_verdict",
    {
        "complete": {"type": "boolean", "description": "用户语义上是否已说完"},
        "reason": {"type": "string", "description": "简短原因"},
    },
    ["complete", "reason"],
)

_BARGE_IN_RESPONSE_FORMAT = _json_schema_format(
    "voice_barge_in_verdict",
    {
        "verdict": {
            "type": "string",
            "enum": ["interrupt", "defer", "backchannel"],
            "description": "interrupt=立刻停下回应；defer=不急可等；backchannel=附和/噪音",
        },
    },
    ["verdict"],
)

_INTERJECTION_RESPONSE_FORMAT = _json_schema_format(
    "voice_interjection_verdict",
    {
        "should_interject": {"type": "boolean", "description": "是否值得插一句话"},
        "emotion": {"type": "string", "enum": _EMOTION_ENUM, "description": "更新后的情绪"},
        "interjection_text": {"type": "string", "description": "插话文本（不插话时空串）"},
        "reason": {"type": "string", "description": "简短原因"},
    },
    ["should_interject", "emotion", "interjection_text", "reason"],
)

_MEMORY_INTERJECTION_RESPONSE_FORMAT = _json_schema_format(
    "voice_memory_interjection_verdict",
    {
        "action": {
            "type": "string",
            "enum": ["none", "append", "correct"],
            "description": "none=不插话；append=补充；correct=自我纠正",
        },
        "line": {"type": "string", "description": "插话文本（none 时空串）"},
        "emotion": {"type": "string", "enum": _EMOTION_ENUM, "description": "插话情绪"},
        "reason": {"type": "string", "description": "简短原因"},
    },
    ["action", "line", "emotion", "reason"],
)


class VoiceClassifyMixin:
    async def _classify_barge_in(self, text: str, history: Optional[list[dict]] = None) -> str:
        """Return interrupt | defer | backchannel.

        Echo-aware AND context-aware: the mic picks up the assistant's OWN TTS
        voice during playback, and ASR also transcribes fragments of the user's
        own speech (mid-sentence pauses, muttering, background conversation)
        that look like real interjections but are not. We pass BOTH the text
        the assistant is currently speaking AND the recent conversation history
        so the classifier can judge contextual relevance: a fragment that is
        completely unrelated to the ongoing exchange is almost certainly noise
        or an accidental pickup, not a real user turn. This stays an agentic
        LLM judgment, never a hardcoded pattern match.

        Core discriminator (2026-08-02): is the user DIRECTLY ADDRESSING the
        assistant with something that needs a reply, or not? Topic relevance is
        NOT the noise criterion — a user who changes the topic is by definition
        saying something unrelated to the current answer, and that is a real
        intentional interrupt (redirecting the conversation), not background
        noise. What must be suppressed is speech NOT directed at the assistant
        (someone else in the room, phone/TV audio, self-talk).

        Acoustic evidence (2026-08-07): the browser's near-field gate tells us
        whether the utterance was picked up close to the mic (near-field —
        almost certainly the user's own voice) or from the environment
        (far-field — background speech/TV). This is injected into the prompt so
        the LLM can weigh "对助手说的" vs "房间里的声音" with real evidence
        instead of guessing from text alone."""
        system = VOICE_BARGE_IN_SYSTEM
        # Build context: recent conversation history + currently spoken text.
        ctx_parts: list[str] = []
        if history:
            recent: list[dict] = [
                m for m in history
                if m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
                and m["content"].strip()
            ]
            if recent:
                lines: list[str] = []
                for m in recent[-4:]:
                    role = "用户" if m["role"] == "user" else "助手"
                    content = m["content"].strip()[:120]
                    lines.append(f"{role}：{content}")
                ctx_parts.append("最近对话上下文：\n" + "\n".join(lines))
        spoken_ctx = (self._spoken_text_recent or "")[-120:]
        if spoken_ctx:
            ctx_parts.append(f"助手此刻正在播报的内容：「{spoken_ctx}」")
        ctx_parts.append(f"用户插话：{text}")
        ctx_parts.append(_prox_evidence_line(self._prox_evidence_near()))
        user_msg = "\n\n".join(ctx_parts)
        raw = await self._quick_classify(
            system, user_msg, "voice.duplex",
            response_format=_BARGE_IN_RESPONSE_FORMAT,
        )
        _perf("barge_in_classify", (_now() - self._barge_classify_start) * 1000,
              text_len=len(text), raw_len=len(raw or ""))
        raw = (raw or "").strip()
        verdict = ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                v = str(data.get("verdict", "")).strip().lower()
                if v in ("interrupt", "defer", "backchannel"):
                    verdict = v
            except Exception:
                pass
        if not verdict:
            # Backward compat: a degraded rung (no response_format) may return
            # the legacy bare word.
            low = raw.lower()
            if "interrupt" in low:
                verdict = "interrupt"
            elif "backchannel" in low:
                verdict = "backchannel"
            elif "defer" in low:
                verdict = "defer"
        if verdict:
            return verdict
        # LLM returned nothing usable (subagent timeout / malformed output).
        # Fail-safe = defer, NOT backchannel: the deterministic noise/echo
        # prefilters already ran before this classifier, so a real user turn
        # that reaches here must never be silently dropped — defer QUEUES it
        # (answered after the current playback) and the client shows the
        # deferred notice. Short acknowledgements still resolve to
        # backchannel (they are filler, not turns). A real user can always
        # press the stop button to interrupt explicitly.
        if len(text) <= 4 and re.fullmatch(r"[嗯对哦好是的啊呃]+", text):
            return "backchannel"
        logger.warning("voice barge-in classify unavailable — deferring turn: %r", text)
        return "defer"

    async def _classify_intent(self, text: str, history: Optional[list[dict]] = None) -> dict:
        """Return {should_respond, needs_tools, reason}.

        The subagent first judges whether the utterance is worth answering at
        all (``should_respond``), because streaming ASR often emits obvious
        misrecognitions — isolated punctuation, filler sounds, gibberish — from
        background noise. Those must be ignored instead of eagerly answered.
        This is an agentic LLM judgment, never a hardcoded pattern match.

        ``history`` is the recent dialogue (list of {role, content}). Passing
        the live conversation context lets the subagent recognise utterances
        that are entirely off-topic relative to the ongoing exchange — e.g.
        someone else in the room talking, a phone conversation, or audio bleed
        from a video — and treat them as background noise even when they are
        syntactically coherent (the length gate alone cannot catch these).
        """
        system = VOICE_INTENT_SYSTEM
        # Build the user message with the recent dialogue as context.
        ctx_block = ""
        recent: list[dict] = []
        if history:
            recent = [
                m for m in history
                if m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
                and m["content"].strip()
            ]
            if recent:
                lines: list[str] = []
                for m in recent:
                    role = "用户" if m["role"] == "user" else "助手"
                    lines.append(f"{role}：{m['content'].strip()}")
                ctx_block = (
                    "以下是最近的对话上下文（从旧到新）：\n"
                    + "\n".join(lines)
                    + "\n\n现在用户又说了一句话（可能是 ASR 误识别的背景噪音，"
                    "也可能是真实的下一句话），请结合上述上下文判断它是否值得助手回应：\n"
                    f"用户：{text}"
                )
        user_msg = ctx_block if ctx_block else f"用户：{text}"
        raw = await self._quick_classify(
            system, user_msg, "voice.intent",
            response_format=_INTENT_RESPONSE_FORMAT,
        )
        _perf("intent_classify", (_now() - self._intent_classify_start) * 1000,
              text_len=len(text), ctx_msgs=len(recent))
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            sr = data.get("should_respond", True)
            # Accept bools and common truthy/falsy strings; fail-safe = respond.
            if isinstance(sr, str):
                should_respond = sr.strip().lower() not in ("false", "0", "no", "否", "不")
            else:
                should_respond = bool(sr)
            return {
                "should_respond": should_respond,
                "needs_tools": bool(data.get("needs_tools", False)),
                "reason": str(data.get("reason", "")),
            }
        except Exception:
            # Fail-safe: never silently swallow a possibly-real user message.
            return {"should_respond": True, "needs_tools": False, "reason": ""}

    async def _classify_eot(self, text: str, history: Optional[list[dict]] = None) -> dict:
        """Semantic end-of-turn judge: return {"complete": bool, "reason": str}.

        Acoustic silence does not imply end-of-turn (the survey consensus):
        an utterance WITHOUT terminal punctuation is ambiguous — the user may
        have finished ("好的就这样" often carries no period) or be mid-thought
        ("我想说…"). This LLM judge decides semantic completeness so the EoT
        watchdog can flush unpunctuated finished speech EARLY instead of
        waiting out the hard silence threshold. Agentic judgment, never a
        punctuation/keyword rule.
        """
        system = VOICE_EOT_SYSTEM
        user_msg = text
        raw = await self._quick_classify(
            system, user_msg, "voice.intent",
            response_format=_EOT_RESPONSE_FORMAT,
        )
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            complete = bool(data.get("complete", False))
            return {"complete": complete, "reason": str(data.get("reason", ""))}
        except Exception:
            # Fail-safe: verdict "not complete" — the watchdog's hard threshold
            # still flushes, so an errored check only adds bounded wait.
            return {"complete": False, "reason": "check failed"}

    async def _classify_interjection(
        self, sentence: str, accumulated_sentences: list[str]
    ) -> dict:
        """Judge whether to interject a brief remark while the user is speaking.

        Returns ``{should_interject, emotion, interjection_text, reason}``.

        The subagent sees the current completed sentence plus previously
        completed sentences in this turn, and the agent's current emotional
        state. When the agent is excited or upset, it interjects more actively.
        The interjection text is SHORT (2-20 chars) — a quick reaction, not a
        full answer. The emotion is updated based on the content so the agent's
        tone shifts naturally (e.g. surprising news → excited, unpleasant news
        → upset, occasionally losing composure → broken/破防).
        """
        emotion_desc = {
            "calm": "平静稳重",
            "interested": "感兴趣",
            "excited": "激动",
            "upset": "不高兴",
            "broken": "破防（情绪失控）",
        }.get(self._emotion, "平静稳重")

        prior = ""
        if accumulated_sentences:
            prior = "之前用户说的内容：\n" + "\n".join(accumulated_sentences) + "\n\n"

        system = VOICE_INTERJECTION_SYSTEM.replace("__EMOTION__", str(emotion_desc))
        user_msg = f"{_ASR_CORRECTION_HINT}\n\n{prior}用户新说完的一句话：{sentence}"
        raw = await self._quick_classify(
            system, user_msg, "voice.interjection",
            response_format=_INTERJECTION_RESPONSE_FORMAT,
        )
        _perf("interjection_classify", (_now() - self._interjection_classify_start) * 1000,
              sentence_len=len(sentence))
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            should_interject = bool(data.get("should_interject", False))
            emotion = str(data.get("emotion", self._emotion)).strip().lower()
            if emotion not in ("calm", "interested", "excited", "upset", "broken"):
                emotion = self._emotion
            interjection_text = str(data.get("interjection_text", "")).strip()
            # Validate interjection text length
            if should_interject and (not interjection_text or len(interjection_text) > 60):
                should_interject = False
            return {
                "should_interject": should_interject,
                "emotion": emotion,
                "interjection_text": interjection_text,
                "reason": str(data.get("reason", "")),
            }
        except Exception:
            return {
                "should_interject": False,
                "emotion": self._emotion,
                "interjection_text": "",
                "reason": "",
            }

    async def _classify_memory_interjection(
        self, memory_ctx: str, answer_text: str, user_speech: str, window: str) -> dict:
        """仲裁：召回的记忆是否值得自己插一句（append 补充 / correct 纠正）。

        LLM-only 判断（项目红线：无正则/关键词意图分类）；代码层只做非意图
        校验（JSON 解析、长度、开关、emotion 白名单）。任何失败 → none 静默。
        """
        fail = {"action": "none", "line": "", "emotion": self._emotion, "reason": ""}
        # 2026-08-31 池化：子用途 purpose 传递（legacy 覆盖链已由 registry
        # 升格为显式 routing），不再读 config 模型名。
        model = "voice.memory_interjection"
        window_desc = {
            "w1_user_speaking": "用户现在正在说话（你插话会打断 ta 的叙述，必须极强相关且极简短）",
            "postscript": "你刚说完/回答刚播完，用户还没开口（适合句尾顺口补一句）",
            "correct": "你的回答还在播放中（你可以自我打断纠正）",
        }.get(window, "时机未明（从严：不插话）")
        system = (
            "你是全双工语音助手。你刚完成一轮对话，随后想起了一段长期记忆。"
            "判断是否值得自己插一句话把它说出来——像真人聊天时忽然想起来一样自然，而不是机械播报。\n"
            f"【当前时机】{window_desc}。\n"
            "只有以下两种情况才插话：\n"
            "- append（补充）：记忆直接补充/澄清了当前话题，且用户现在就需要这个信息。"
            "必须以承接词开头（如「对了，」「而且，」「顺便说一句，」），6-30 字口语，"
            "只给增量信息，不重复回答里已有的内容。\n"
            "- correct（自我纠正）：你的回答里有一个断言与记忆中的**稳定个人事实**"
            "（用户的偏好/家人/习惯/长期身份信息等，而非某次一次性工作事项）直接矛盾。"
            "必须以认错结构开头（如「不对，我搞错了，」「等等，我记差了，」），6-40 字，"
            "先认错再给出正确说法。\n"
            "其他一切情况 → none（沉默）。宁可漏说，不可错说：说错了比没想起来更伤信任；"
            "记忆与当前话题只是沾边、用户在陈述新故事、或你不确定 → none。\n"
            "插话文本要求：口语化短句；不写完整回答或建议；不出现「记忆」「数据库」「系统」"
            "这类词（可以说「我刚才记的是…」「我记得你说过…」）；可以带一个白名单风格词"
            "如 (欣慰)、(抱歉)。\n"
            '只输出 JSON：{"action":"none|append|correct","line":"插话文本（none 时空串）",'
            '"emotion":"calm|interested|excited|upset|broken","reason":"一句话原因"}\n'
            "emotion 按插话内容更新（correct 常配 calm/克制，不必 upset）。"
        )
        parts = [f"【你想起的记忆】\n{memory_ctx.strip()[:1200]}"]
        if answer_text and answer_text.strip():
            parts.append(f"【你刚给出的回答】\n{answer_text.strip()[:800]}")
        else:
            parts.append("【你刚给出的回答】\n（本轮没有实质性回答文字——用户问题可能由工具处理，"
                         "此时尤其要克制：没有断言可纠正，只能考虑 append）")
        if user_speech and user_speech.strip():
            parts.append(f"【用户正在说的话】\n{user_speech.strip()[:300]}")
        parts.append(f"【时机】{window}")
        try:
            raw = await self._quick_classify(
                system, "\n\n".join(parts), model,
                response_format=_MEMORY_INTERJECTION_RESPONSE_FORMAT,
            )
            m = re.search(r"\{.*\}", raw or "", re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            action = str(data.get("action", "none")).strip().lower()
            if action not in ("append", "correct"):
                return fail
            line = str(data.get("line", "")).strip()
            if not line or len(line) > 40 or len(line) < 6:
                return {**fail, "reason": "line 长度/空校验（6-40 字）"}
            if action == "correct" and not self.config.voice_memory_correct_enabled:
                return {**fail, "reason": "correct 开关关闭（灰度）"}
            emotion = str(data.get("emotion", "")).strip().lower()
            if emotion not in ("calm", "interested", "excited", "upset", "broken"):
                emotion = self._emotion
            return {"action": action, "line": line, "emotion": emotion,
                    "reason": str(data.get("reason", ""))[:120]}
        except Exception:
            logger.debug("voice memory interjection judge failed (silent)")
            return fail
    async def _quick_classify(
        self,
        system: str,
        user: str,
        purpose: str = "voice",
        response_format: Optional[dict] = None,
    ) -> str:
        """One-shot subagent call (classifier / judge), thinking forced OFF.

        Structured output (2026-09-13): when the caller passes a JSON-schema
        `response_format`, the call walks the provider-support ladder
        ``schema → json_object → off``; a provider rejection of one rung is
        learned per purpose (`_json_modes`) so later calls skip it. Parsing
        callers stay tolerant either way (regex JSON extraction)."""
        from app.services.voice_service import (
            _StructuredOutputRejected,
            _looks_like_response_format_error,
        )
        svc, model = self._build_llm(purpose)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def _call_subagent(rf: Optional[dict]) -> str:
            # Subagents are classifiers outputting one word / a small JSON —
            # reasoning MUST be off for them regardless of the main model's
            # thinking setting: with thinking on, a 160-token budget is burned
            # entirely by reasoning and the content comes back EMPTY, which
            # silently degrades every classifier to its fail-safe default
            # (observed 2026-07-22: all barge-in judgments -> backchannel).
            kwargs: dict = {
                "temperature": 0.1,
                "max_tokens": 160,
                "extra_body": self._thinking_off_body(svc),
            }
            if rf is not None:
                kwargs["response_format"] = rf
            try:
                return await svc.complete_chat(messages, **kwargs)
            except Exception as exc:
                if rf is not None and _looks_like_response_format_error(exc):
                    # Provider rejected this structured-output rung — signal the
                    # ladder to downgrade instead of masking it as a generic
                    # provider failure.
                    raise _StructuredOutputRejected() from exc
                # Thinking/reasoning rejection (or any other provider error):
                # retry once through the legacy config-gated path without
                # structured output. NOTE: `_extra_body` returns the same
                # thinking-off body while `voice_disable_thinking` is true, so
                # a provider that rejects the thinking knob itself still
                # fail-safes (pre-existing behavior, not a regression).
                kwargs["extra_body"] = self._extra_body(svc)
                kwargs.pop("response_format", None)
                return await svc.complete_chat(messages, **kwargs)

        def _rung(mode: str) -> Optional[dict]:
            if mode == "schema":
                return response_format
            if mode == "object":
                return {"type": "json_object"}
            return None

        # The 6s bound covers the WHOLE subagent operation (gate queue wait +
        # LLM call): the gate is per-session and a long main stream holds one
        # slot, so a plain `async with` could wait behind it indefinitely —
        # for barge-in classify that would be dead air on the user's critical
        # path. On timeout every classifier fail-safes.
        async def _classify() -> str:
            async with self._llm_gate:
                if response_format is None:
                    return await _call_subagent(None)
                learned = self._json_modes.get(purpose)
                if learned is None:
                    ladder = ["schema", "object", "off"]
                elif learned == "off":
                    ladder = ["off"]
                else:
                    ladder = [learned, "off"]
                for mode in ladder:
                    try:
                        return await _call_subagent(_rung(mode))
                    except _StructuredOutputRejected:
                        # The rejected rung is unusable for this provider; cache
                        # the next rung so later calls skip straight to it.
                        nxt = "object" if mode == "schema" else "off"
                        self._json_modes[purpose] = nxt
                        logger.info(
                            "voice subagent structured output downgraded: purpose=%s %s -> %s",
                            purpose, mode, nxt,
                        )
                        continue
                return ""

        try:
            return await asyncio.wait_for(
                _classify(), timeout=self.config.voice_subagent_timeout_seconds
            )
        except Exception as exc:
            logger.debug("voice subagent call failed: %s", exc)
            return ""
