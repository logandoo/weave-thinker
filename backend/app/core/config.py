# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import os
import toml
from pathlib import Path
from typing import Optional
from functools import lru_cache


def _parse_int(value) -> Optional[int]:
    if value == "" or value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


class Config:
    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "config.toml"
            )

        self.config_path = Path(config_path).resolve()
        self._config = toml.load(config_path)
        # Model config split: every model-related setting (LLM / ASR / TTS /
        # embedding / rerank / judge / verifier / subagent / validator /
        # memory / title / providers) lives in config_model.toml, merged
        # OVER the main file so config.toml stays a pure infra file. When the
        # model file is absent (legacy deployments) the main file's sections
        # remain authoritative — every property below is unchanged.
        self.model_config_path = self._resolve_model_config_path(config_path)
        self._config = self._merge_model_config(self._config)

    @staticmethod
    def _resolve_model_config_path(config_path: str) -> Optional[Path]:
        env_override = os.environ.get("CONFIG_MODEL_PATH")
        if env_override:
            return Path(env_override).resolve()
        main = Path(config_path).resolve()
        candidate = main.parent / "config_model.toml"
        return candidate if candidate.exists() else None

    # Sections that belong to config_model.toml. Whole-section moves:
    _MODEL_SECTIONS = {
        "api",               # legacy main LLM endpoint
        "defaults",          # default LLM sampling params
        "default_assistant", # assistant-scoped sampling params
        "asr",               # speech recognition models
        "voice",             # voice LLM / TTS / ASR tuning
        "providers",         # multi-provider LLM routing
        "deathmatch",        # judge / verifier models + goal-loop budgets
        "sub_agent",         # subagent LLM params
        "title_generation",  # title LLM params
        "memory",            # memory LLM / embedding / rerank / cost models
    }
    # [agent] stays in the main file for harness tuning, but its MODEL
    # sub-sections move. Only these keys are taken from the model file.
    _MODEL_AGENT_SUBSECTIONS = {
        "auxiliary",      # per-task auxiliary models (coordinator/classifier/title/…)
        "compression",    # context-compression model params
        "moa",            # mixture-of-agents models
        "memory",         # daily summary / dream models
        "tool_digest",    # subagent tool-result digest model
        "sub_agent",      # subagent model params
    }

    def _merge_model_config(self, base: dict) -> dict:
        if self.model_config_path is None:
            return base
        import logging
        logger = logging.getLogger(__name__)
        try:
            model_cfg = toml.load(self.model_config_path)
        except Exception:
            logger.exception(
                "Failed to load %s — falling back to main config sections. "
                "If this deployment was already split, the server is now "
                "running with EMPTY/default model config and will fail on "
                "the first LLM call.",
                self.model_config_path,
            )
            return base
        merged = dict(base)
        for section in self._MODEL_SECTIONS:
            if section in model_cfg:
                if section in merged:
                    # Section-granular replacement: a partial model file
                    # REPLACES the whole main-file section. Warn so ops can
                    # spot missing keys (e.g. a hand-crafted model file with
                    # only [api].model_name would silently drop base_url/key).
                    logger.warning(
                        "config_model.toml section [%s] REPLACES the main "
                        "config.toml section of the same name (whole-section "
                        "override, not per-key merge)", section,
                    )
                merged[section] = model_cfg[section]
        if "agent" in model_cfg:
            agent = dict(merged.get("agent") or {})
            for key, value in (model_cfg.get("agent") or {}).items():
                if key in self._MODEL_AGENT_SUBSECTIONS:
                    agent[key] = value
            merged["agent"] = agent
        return merged

    def _resolve_project_path(self, value: str, *, default: str) -> Path:
        raw_value = value or default
        path = Path(raw_value)
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()

    @property
    def database_url(self) -> str:
        db = self._config.get("database", {})
        host = db.get("host", "localhost")
        port = db.get("port", 5432)
        username = db.get("username", "postgres")
        password = db.get("password", "")
        name = db.get("name", "weavethinker")
        return f"postgresql+asyncpg://{username}:{password}@{host}:{port}/{name}"

    @property
    def database_pool_size(self) -> int:
        return int(self._config.get("database", {}).get("pool_size", 20))

    @property
    def database_max_overflow(self) -> int:
        return int(self._config.get("database", {}).get("max_overflow", 30))

    @property
    def database_pool_recycle(self) -> int:
        return int(self._config.get("database", {}).get("pool_recycle", 1800))

    @property
    def database_pool_timeout(self) -> int:
        return int(self._config.get("database", {}).get("pool_timeout", 30))

    @property
    def api_base_url(self) -> str:
        return self._config.get("api", {}).get("base_url", "https://api.openai.com/v1")

    @property
    def api_key(self) -> Optional[str]:
        key = self._config.get("api", {}).get("api_key", "")
        return key if key else None

    @property
    def security(self) -> dict:
        return self._config.get("security", {})

    @property
    def security_jwt_secret_key(self) -> Optional[str]:
        key = self.security.get("jwt_secret_key", "")
        if key:
            return key
        return os.environ.get("JWT_SECRET_KEY") or None

    @property
    def security_token_expire_days(self) -> int:
        """JWT lifetime in days (sliding-session window). Was a hardcoded
        constant (7) in auth_service; configurable since 2026-08-25 wave-3.
        The frontend auto-refreshes on app start, so users who come back
        within this window are never re-asked for a password.
        Clamped to >= 1: a 0/negative window would issue immediately-dead
        tokens (login succeeds, then a 401 loop with no error surface);
        non-numeric values fall back to 7."""
        try:
            return max(1, int(self.security.get("token_expire_days", 7)))
        except (TypeError, ValueError, OverflowError):
            return 7

    @property
    def security_cors_allow_origins(self) -> list:
        origins = self.security.get("cors_allow_origins", ["*"])
        if isinstance(origins, str):
            return [origins]
        return list(origins)

    @property
    def security_cors_allow_credentials(self) -> bool:
        return bool(self.security.get("cors_allow_credentials", True))

    @property
    def super_admin_bypass(self) -> bool:
        return bool(self.security.get("super_admin_bypass", False))

    @property
    def model_name(self) -> Optional[str]:
        return self._config.get("api", {}).get("model_name") or None

    @property
    def server_host(self) -> str:
        return self._config.get("server", {}).get("host", "0.0.0.0")

    @property
    def server_port(self) -> int:
        return self._config.get("server", {}).get("port", 8158)

    @property
    def server_scheme(self) -> str:
        """Match the SSL auto-detection in scripts/start.sh: when key.pem and
        cert.pem exist in the backend directory, uvicorn is launched with
        --ssl-keyfile/--ssl-certfile, so the API is https."""
        key_pem = self.backend_root / "key.pem"
        cert_pem = self.backend_root / "cert.pem"
        return "https" if (key_pem.exists() and cert_pem.exists()) else "http"

    @property
    def deathmatch_self_eval_username(self) -> str:
        return str(self.deathmatch.get("self_eval_username", "") or "")

    @property
    def deathmatch_self_eval_password(self) -> str:
        return str(self.deathmatch.get("self_eval_password", "") or "")

    @property
    def project_root(self) -> Path:
        return self.config_path.parent.parent

    @property
    def backend_root(self) -> Path:
        return self.config_path.parent

    @property
    def defaults(self) -> dict:
        return self._config.get("defaults", {})

    @property
    def default_temperature(self) -> float:
        return self.defaults.get("temperature", 0.7)

    @property
    def default_top_p(self) -> float:
        return self.defaults.get("top_p", 1.0)

    @property
    def default_top_k(self) -> Optional[int]:
        val = self.defaults.get("top_k")
        return _parse_int(val)

    @property
    def default_presence_penalty(self) -> float:
        return self.defaults.get("presence_penalty", 0.0)

    @property
    def default_frequency_penalty(self) -> float:
        return self.defaults.get("frequency_penalty", 0.0)

    @property
    def default_max_tokens(self) -> Optional[int]:
        val = self.defaults.get("max_tokens")
        return _parse_int(val)

    @property
    def default_assistant(self) -> dict:
        return self._config.get("default_assistant", {})

    @property
    def default_assistant_name(self) -> str:
        return self.default_assistant.get("name", "默认助手")

    @property
    def default_assistant_system_prompt(self) -> str:
        return self.default_assistant.get("system_prompt", "")

    @property
    def default_assistant_temperature(self) -> float:
        return self.default_assistant.get("temperature", 0.7)

    @property
    def default_assistant_top_p(self) -> float:
        return self.default_assistant.get("top_p", 1.0)

    @property
    def default_assistant_top_k(self) -> Optional[int]:
        val = self.default_assistant.get("top_k")
        return _parse_int(val)

    @property
    def default_assistant_presence_penalty(self) -> float:
        return self.default_assistant.get("presence_penalty", 0.0)

    @property
    def default_assistant_frequency_penalty(self) -> float:
        return self.default_assistant.get("frequency_penalty", 0.0)

    @property
    def default_assistant_max_tokens(self) -> Optional[int]:
        val = self.default_assistant.get("max_tokens")
        return _parse_int(val)

    @property
    def asr(self) -> dict:
        return self._config.get("asr", {})

    @property
    def asr_base_url(self) -> str:
        return self.asr.get("base_url", "")

    @property
    def asr_model(self) -> str:
        return self.asr.get("model", "paraformer-zh")

    @property
    def asr_is_dashscope(self) -> bool:
        return bool(self.asr.get("is_dashscope", False))

    @property
    def asr_is_mimo(self) -> bool:
        return bool(self.asr.get("is_mimo", False))

    @property
    def asr_api_key(self) -> str:
        return self.asr.get("api_key", "")

    @property
    def asr_dashscope_api_key(self) -> str:
        return self.asr.get("dashscope_api_key", "")

    @property
    def asr_dashscope_model(self) -> str:
        return self.asr.get("dashscope_model", "qwen3-asr-flash-realtime-2026-02-10")

    @property
    def voice(self) -> dict:
        return self._config.get("voice", {})

    @property
    def voice_enabled(self) -> bool:
        return bool(self.voice.get("enabled", False))

    @property
    def voice_provider(self) -> str:
        return str(self.voice.get("provider", "default"))

    @property
    def voice_model_name(self) -> str:
        return str(self.voice.get("model_name", ""))

    @property
    def voice_system_prompt(self) -> str:
        return str(self.voice.get("system_prompt", ""))

    @property
    def voice_temperature(self) -> float:
        try:
            return float(self.voice.get("temperature", 0.7))
        except (TypeError, ValueError):
            return 0.7

    @property
    def voice_max_tokens(self) -> Optional[int]:
        return _parse_int(self.voice.get("max_tokens", 1024))

    @property
    def voice_duplex_model(self) -> str:
        return str(self.voice.get("duplex_model", ""))

    @property
    def voice_intent_model(self) -> str:
        return str(self.voice.get("intent_model", ""))

    @property
    def voice_barge_in_enabled(self) -> bool:
        return bool(self.voice.get("barge_in_enabled", True))

    @property
    def voice_bg_task_notify_enabled(self) -> bool:
        """When a background task (submitted from a voice conversation) reaches
        a terminal state, proactively announce it in the live voice session and
        let the assistant offer follow-up actions (read result / export / save)."""
        return bool(self.voice.get("bg_task_notify_enabled", True))

    @property
    def voice_barge_in_onset_min_chars(self) -> int:
        """Minimum utterance length (chars, after stripping punctuation) for
        the acoustic ONSET barge-in pause. Utterances shorter than this
        (e.g. "对"/"嗯是"/"好") are NOT paused — they are overwhelmingly
        backchannels or mic false positives (TTS-echo / environment audio
        misrecognized as a syllable; the echo gate only catches transcripts
        matching the spoken text, so misrecognitions slip through). They
        flow through the normal EoT+classify path instead — a backchannel
        then never interrupts the playback at all. Real interrupts are
        almost always >= this length and still pause immediately."""
        try:
            return int(self.voice.get("barge_in_onset_min_chars", 3))
        except (TypeError, ValueError):
            return 3

    @property
    def voice_barge_in_no_interrupt_seconds(self) -> float:
        """No-interrupt window (s) after a TTS playback burst starts. Utterances
        flushed inside the window are deferred (queued + prefetched) instead of
        cutting playback — the opening instants of a burst are where own-voice
        echo/reverb most often fools ASR. Explicit stop words bypass this."""
        try:
            return float(self.voice.get("barge_in_no_interrupt_seconds", 1.2))
        except (TypeError, ValueError):
            return 1.2

    @property
    def voice_barge_in_cooldown_seconds(self) -> float:
        """Cooldown (s) after each confirmed interrupt. Prevents a single noisy
        stretch (echo tail, reverb, the user's own continuing speech) from
        re-interrupting the resumed playback over and over."""
        try:
            return float(self.voice.get("barge_in_cooldown_seconds", 2.0))
        except (TypeError, ValueError):
            return 2.0

    @property
    def voice_barge_in_onset_enabled(self) -> bool:
        """Acoustic-layer onset barge-in (FireRedChat pVAD pattern): when speech
        is detected during TTS playback, pause the audio IMMEDIATELY on the
        first ASR partial (instead of waiting for EoT + LLM classification,
        ~2s). The barge-in classifier then decides: interrupt -> answer the new
        turn; backchannel/defer -> resume playback from the breakpoint."""
        return bool(self.voice.get("barge_in_onset_enabled", True))

    @property
    def voice_barge_in_proximity_gate(self) -> bool:
        """Acoustic near-field gate for barge-in: the browser classifies its
        mic input as near-field (user close to the phone — almost certainly
        the user's own voice) vs far-field (environment speech — TV/room
        conversation) and reports it via the audio_proximity WS event. When
        enabled, far-field speech never pauses playback (onset) and the
        barge-in classifier receives the near/far evidence. Without the
        signal (unknown clients) the gate defaults to near — old behavior."""
        return bool(self.voice.get("barge_in_proximity_gate", True))

    @property
    def voice_barge_in_proximity_stale_seconds(self) -> float:
        """Freshness window (s) for the near-field signal: a near report older
        than this is treated as far (the client stopped reporting — be
        conservative and never pause on stale evidence)."""
        try:
            return float(self.voice.get("barge_in_proximity_stale_seconds", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def voice_backchannel_recall_seconds(self) -> float:
        """Window (s) during which a backchannel-classified utterance can
        recall (skip) a queued copy of itself — the window/cooldown defer
        path enqueues turns before the barge-in classifier verdict lands, and
        a late backchannel verdict must prevent the filler from being answered
        as a real turn (ghost-message fix, conv 689f06ec)."""
        try:
            return float(self.voice.get("backchannel_recall_seconds", 30.0))
        except (TypeError, ValueError):
            return 30.0

    @property
    def voice_eot_semantic_enabled(self) -> bool:
        """Semantic end-of-turn: for utterances WITHOUT terminal punctuation,
        probe semantic completeness with an LLM judge once silence passes
        voice_eot_semantic_probe_seconds. "Complete" flushes early (unpunctuated
        finished speech answers faster than the hard silence threshold);
        "incomplete"/error waits for the hard threshold (fail-open, no added
        dead time beyond the current behavior)."""
        return bool(self.voice.get("eot_semantic_enabled", True))

    @property
    def voice_eot_semantic_probe_seconds(self) -> float:
        """Silence (s) after which the semantic EoT judge is consulted for
        unpunctuated text. Must be below voice_eot_silence_incomplete_seconds
        (the hard flush threshold) so the judge can beat it."""
        try:
            return float(self.voice.get("eot_semantic_probe_seconds", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def voice_eot_paused_probe_seconds(self) -> float:
        """Silence (s) after which the semantic EoT judge is consulted while
        playback is PAUSED (acoustic onset barge-in). The user's reaction is
        the only speech in that state, so the endpoint can be faster without
        fragment risk — the flush is still gated by the judge's verdict.
        Keeps the pause→resume silence inside the user's patience (the
        classify now runs in parallel via the onset pre-classify)."""
        try:
            return float(self.voice.get("eot_paused_probe_seconds", 0.6))
        except (TypeError, ValueError):
            return 0.6

    @property
    def voice_eot_semantic_timeout_seconds(self) -> float:
        """Hard bound (s) on the semantic EoT judge call (gate wait + LLM call).
        On timeout the watchdog falls back to the hard silence threshold —
        never blocks the EoT critical path beyond this bound."""
        try:
            return float(self.voice.get("eot_semantic_timeout_seconds", 1.5))
        except (TypeError, ValueError):
            return 1.5

    @property
    def voice_disable_thinking(self) -> bool:
        """Voice turns force provider reasoning/thinking OFF by default (True),
        for every provider — qwen/DashScope gets enable_thinking=False, others
        get thinking.type=disabled (see voice_service._thinking_off_body).
        Voice latency cannot afford reasoning time; set False only to
        deliberately experiment with reasoning-capable voice turns."""
        return bool(self.voice.get("disable_thinking", True))

    @property
    def voice_tts_enabled(self) -> bool:
        return bool(self.voice.get("tts_enabled", True))

    @property
    def voice_tts_base_url(self) -> str:
        return str(self.voice.get("tts_base_url", ""))

    @property
    def voice_tts_api_key(self) -> str:
        return str(self.voice.get("tts_api_key", ""))

    @property
    def voice_tts_model(self) -> str:
        return str(self.voice.get("tts_model", "mimo-v2.5-tts"))

    @property
    def voice_tts_voice(self) -> str:
        return str(self.voice.get("tts_voice", "冰糖"))

    @property
    def voice_tts_style_instruction(self) -> str:
        return str(self.voice.get("tts_style_instruction", ""))

    @property
    def voice_context_turns(self) -> int:
        try:
            return int(self.voice.get("context_turns", 8))
        except (TypeError, ValueError):
            return 8

    @property
    def voice_eot_silence_seconds(self) -> float:
        """Silence (s) before flushing a turn whose text ends with terminal
        punctuation (a complete-looking utterance). Shorter = more responsive."""
        try:
            return float(self.voice.get("eot_silence_seconds", 0.6))
        except (TypeError, ValueError):
            return 0.6

    @property
    def voice_eot_silence_incomplete_seconds(self) -> float:
        """Silence (s) before flushing a turn whose text does NOT end with
        terminal punctuation (likely a mid-sentence pause). Longer = less
        truncation of long questions spoken with natural pauses. Must cover
        FunASR's long-sentence finalization latency — observed upstream-result
        gaps of 1.6s at sentence boundaries (2026-07-21), so values <= 1.5s
        chop continuous speech at sentence boundaries."""
        try:
            return float(self.voice.get("eot_silence_incomplete_seconds", 2.0))
        except (TypeError, ValueError):
            return 2.0

    @property
    def voice_eot_complete_grace_seconds(self) -> float:
        """Grace period (s) of NO ASR activity after an utterance whose text
        ends with terminal punctuation before the turn flushes. fun-asr-realtime
        emits the first partial of a follow-on sentence ~0.5-1.3s after a
        sentence_end when the user keeps speaking (inter-sentence pause +
        recognition latency), so ~1.2s distinguishes a real stop from a
        sentence boundary mid-speech (0.3s faster per turn than 1.5s; a
        boundary-gap flush is now recovered by the onset barge-in + fragment
        coalescing)."""
        try:
            return float(self.voice.get("eot_complete_grace_seconds", 1.2))
        except (TypeError, ValueError):
            return 1.2

    @property
    def voice_eot_complete_max_seconds(self) -> float:
        """Hard cap (s) on how long a COMPLETE utterance (ends with terminal
        punctuation) may wait for the activity-grace before flushing anyway.
        Bounds the wait in noisy environments where background speech keeps
        feeding ASR activity (which would otherwise postpone the flush
        indefinitely). When the user genuinely continues, the next partial
        removes the terminal punctuation from the tail and resets this timer,
        so it only fires when the text has sat complete for the whole cap."""
        try:
            return float(self.voice.get("eot_complete_max_seconds", 3.0))
        except (TypeError, ValueError):
            return 3.0

    @property
    def voice_fragment_merge_seconds(self) -> float:
        """Probe window (s) for coalescing chopped ASR turns in the responder.
        Queued backlog fragments always merge immediately; while the merged
        text does NOT end with terminal punctuation (evidence the EoT cut a
        continuous utterance mid-speech) the responder waits up to this long
        for the next fragment (re-arming on each arrival). Kept short: a
        single chopped head should be answered quickly and its tail answered
        as a follow-up turn, not stall the head."""
        try:
            return float(self.voice.get("fragment_merge_seconds", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def voice_fragment_merge_max_seconds(self) -> float:
        """Total cap (s) on fragment coalescing for a single turn — bounds the
        added latency when an utterance is chopped into many fragments."""
        try:
            return float(self.voice.get("fragment_merge_max_seconds", 10.0))
        except (TypeError, ValueError):
            return 10.0

    @property
    def voice_noise_gate_max_chars(self) -> int:
        """Only run the agentic ASR-noise (should_respond) gate for utterances
        of at most this many characters. Longer utterances are always answered
        so a real (long) question is never swallowed as 'noise'."""
        try:
            return int(self.voice.get("noise_gate_max_chars", 3))
        except (TypeError, ValueError):
            return 3

    @property
    def voice_asr_speech_noise_threshold(self) -> float:
        """Fun-ASR VAD speech/noise decision threshold (range [-1.0, 1.0],
        adjust in 0.1 steps per upstream docs). Higher values filter MORE
        audio as noise (less background-voice pickup); values >= 0.5
        misclassify real user speech as noise (verified 2026-07-21),
        producing long upstream-result gaps mid-utterance that the EoT then
        flushes as truncated turns. 0.3 stays the default — the 0.4 bump was
        reverted for lack of real-audio verification (A4.9 C4); background
        speech is gated acoustically by the proximity gate instead. Browser-
        side RNNoise handles denoising."""
        try:
            v = float(self.voice.get("asr_speech_noise_threshold", 0.3))
        except (TypeError, ValueError):
            return 0.3
        return max(-1.0, min(1.0, v))

    @property
    def voice_asr_context_enabled(self) -> bool:
        """Whether to pass recent conversation turns to fun-asr-realtime as
        context (raw_input.context) to bias recognition toward the dialogue
        topic and suppress off-topic background speech."""
        return bool(self.voice.get("asr_context_enabled", True))

    @property
    def voice_asr_context_turns(self) -> int:
        """Number of recent user/assistant turns to include in the fun-asr
        realtime context (DashScope caps each role at 5 messages; per-turn
        text is capped at 400 chars by the service)."""
        try:
            return max(0, min(5, int(self.voice.get("asr_context_turns", 3))))
        except (TypeError, ValueError):
            return 3

    @property
    def voice_intent_context_turns(self) -> int:
        """Number of recent turns the intent subagent sees when judging
        should_respond. More context = better off-topic detection at a small
        prompt-size cost."""
        try:
            return max(0, min(8, int(self.voice.get("intent_context_turns", 4))))
        except (TypeError, ValueError):
            return 4

    @property
    def voice_subagent_timeout_seconds(self) -> float:
        """Timeout for each voice subagent classifier call (barge-in, intent,
        interjection). These run on the user's critical path (barge-in blocks
        turn queuing; a slow interjection check blocks EoT flush), so a long
        bound can make the assistant appear dead for tens of seconds. 6s
        covers the normal 1-2s call plus provider hiccups while bounding the
        worst case; every classifier fail-safes on timeout."""
        try:
            return max(2.0, float(self.voice.get("subagent_timeout_seconds", 6.0)))
        except (TypeError, ValueError):
            return 6.0

    @property
    def voice_llm_retry_attempts(self) -> int:
        """Extra attempts for the voice main LLM stream on provider
        rate-limit errors (429). xiaomimimo hard-limits when a voice session
        fires several LLM calls concurrently (interjection + intent + main);
        without retries the whole turn dies with an error event and the user
        gets no answer. Retries only while nothing has been generated yet."""
        try:
            return max(0, min(4, int(self.voice.get("llm_retry_attempts", 2))))
        except (TypeError, ValueError):
            return 2

    # ---- Interjection (插话) mechanism ----

    @property
    def voice_interjection_enabled(self) -> bool:
        """Whether the agent can interject brief remarks while the user is
        still speaking. When enabled, each completed ASR sentence is sent to
        an interjection subagent that decides whether to make a quick comment."""
        return bool(self.voice.get("interjection_enabled", True))

    @property
    def voice_interjection_model(self) -> str:
        """Model for the interjection subagent. Falls back to the intent model
        if not specified."""
        return str(self.voice.get("interjection_model", ""))

    @property
    def voice_interjection_cooldown_seconds(self) -> float:
        """Minimum seconds between interjections. Prevents the agent from
        interjecting on every single sentence."""
        try:
            return max(0.5, float(self.voice.get("interjection_cooldown_seconds", 3.0)))
        except (TypeError, ValueError):
            return 3.0

    @property
    def voice_interjection_max_per_turn(self) -> int:
        """Maximum interjections during a single user speech turn."""
        try:
            return max(0, int(self.voice.get("interjection_max_per_turn", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def voice_emotion_enabled(self) -> bool:
        """Whether the agent has an emotional state that affects interjection
        frequency and answer tone. When excited or upset, the agent interjects
        more actively."""
        return bool(self.voice.get("emotion_enabled", True))

    # ---- Auxiliary speech: filler prefix (填充词) + backchannel (应和) ----

    @property
    def voice_filler_enabled(self) -> bool:
        """Whether a random filler phrase ("我来想想啊"…) is spoken at the start
        of an answer turn to cover LLM generation time. The phrase is an aux
        TTS item: the consumer cuts it when the real answer's first audio
        chunk is ready, so fast answers are not delayed."""
        return bool(self.voice.get("filler_enabled", True))

    @property
    def voice_filler_phrases(self) -> list:
        """Candidate filler phrases; one is picked at random per turn (never
        the same as the previous pick when alternatives exist)."""
        default = ["我来看看啊", "我来想想啊", "等下啊，我琢磨一下", "嗯，我想想"]
        val = self.voice.get("filler_phrases")
        if isinstance(val, list) and any(isinstance(p, str) and p.strip() for p in val):
            return [p for p in val if isinstance(p, str) and p.strip()] or default
        return default

    @property
    def voice_filler_min_gap_seconds(self) -> float:
        """Minimum gap (s) between two filler utterances. The filler now fires
        at the EoT flush via a speculative prefetch (arm → convert); this gap
        keeps a fragment cascade (watchdog chopping one utterance) from
        speaking two fillers back-to-back, and keeps the `_handle_user_turn`
        fallback from double-firing behind a flush-converted filler. 3.0s is
        comfortably above the cascade window (fragment_merge_seconds probe +
        typical chop gaps) without eating the filler of a fast natural
        back-and-forth (short answer + quick follow-up)."""
        try:
            return float(self.voice.get("filler_min_gap_seconds", 3.0))
        except (TypeError, ValueError):
            return 3.0

    @property
    def voice_backchannel_enabled(self) -> bool:
        """Whether the agent utters short listening acks (嗯/哦…) during the
        user's mid-utterance pauses. Kept intentionally short so the phrase
        always finishes before the earliest EoT flush of the same utterance."""
        return bool(self.voice.get("backchannel_enabled", True))

    @property
    def voice_backchannel_phrases(self) -> list:
        """Candidate backchannel phrases (must stay ~1-2 chars: a long phrase
        would stretch its audible window into the flush region and its
        mic-echo could pass the short-noise gate; runtime enforces <=4 chars)."""
        default = ["嗯", "嗯嗯", "哦", "对"]
        val = self.voice.get("backchannel_phrases")
        if isinstance(val, list) and any(isinstance(p, str) and p.strip() for p in val):
            return [p for p in val if isinstance(p, str) and p.strip()] or default
        return default

    @property
    def voice_backchannel_pause_seconds(self) -> float:
        """Minimum user-speech pause (s) before a backchannel may fire. Must be
        below voice_eot_semantic_probe_seconds so the ack lands in the window
        where the turn is neither flushing nor being semantically judged.
        Clamped below the probe so a misconfigured value degrades to a
        narrower window instead of silently disabling the feature (A4.9 M4)."""
        try:
            value = float(self.voice.get("backchannel_pause_seconds", 0.35))
        except (TypeError, ValueError):
            value = 0.35
        probe = self.voice_eot_semantic_probe_seconds
        return max(0.1, min(value, max(0.1, probe - 0.05)))

    @property
    def voice_backchannel_cooldown_seconds(self) -> float:
        """Minimum seconds between backchannels. Human backchannels land every
        ~5-15s; a shorter cooldown starts to sound mechanical."""
        try:
            return max(1.0, float(self.voice.get("backchannel_cooldown_seconds", 8.0)))
        except (TypeError, ValueError):
            return 8.0

    @property
    def voice_backchannel_max_per_turn(self) -> int:
        """Maximum backchannels within a single user speech turn."""
        try:
            return max(0, int(self.voice.get("backchannel_max_per_turn", 2)))
        except (TypeError, ValueError):
            return 2

    # ---- 每轮异步记忆召回 + 记忆插话（memory recall & interjection）----
    @property
    def voice_memory_recall_enabled(self) -> bool:
        """Per-turn fire-and-forget v2 memory recall + identity-prompt block
        injection. Off = legacy behavior (session-start frozen memory)."""
        return bool(self.voice.get("memory_recall_enabled", True))

    @property
    def voice_memory_interjection_enabled(self) -> bool:
        """Master switch for the memory interjection (judge + speak)."""
        return bool(self.voice.get("memory_interjection_enabled", True))

    @property
    def voice_memory_correct_enabled(self) -> bool:
        """Self-correction (correct) memory interjection, incl. draining the
        in-flight answer tail. Gray-release default: off."""
        return bool(self.voice.get("memory_correct_enabled", False))

    @property
    def voice_memory_interjection_max_append(self) -> int:
        """Per-session budget for append-style memory interjections."""
        try:
            return max(0, int(self.voice.get("memory_interjection_max_append", 3)))
        except (TypeError, ValueError):
            return 3

    @property
    def voice_memory_interjection_max_correct(self) -> int:
        """Per-session budget for correct-style memory interjections."""
        try:
            return max(0, int(self.voice.get("memory_interjection_max_correct", 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def voice_memory_interjection_cooldown_seconds(self) -> float:
        """Minimum seconds between ANY two memory interjections."""
        try:
            return max(1.0, float(self.voice.get("memory_interjection_cooldown_seconds", 20.0)))
        except (TypeError, ValueError):
            return 20.0

    @property
    def voice_memory_interjection_min_score(self) -> float:
        """Top absolute relevance score required before the judge is even
        asked (aligns with [memory.retrieval] boost_min_relevance)."""
        try:
            return max(0.0, float(self.voice.get("memory_interjection_min_score", 0.5)))
        except (TypeError, ValueError):
            return 0.5

    @property
    def voice_memory_interjection_model(self) -> str:
        """Model override for the memory-interjection judge. Empty string
        falls back to voice_interjection_model (then the main voice model)."""
        return str(self.voice.get("memory_interjection_model", ""))

    @property
    def agent(self) -> dict:
        return self._config.get("agent", {})

    @property
    def agent_name(self) -> str:
        return self.agent.get("name", "共享智能体")

    @property
    def agent_memory_max_items(self) -> int:
        return int(self.agent.get("memory_max_items", 12))

    @property
    def agent_note_context_limit(self) -> int:
        return int(self.agent.get("note_context_limit", 12))

    @property
    def agent_conversation_context_limit(self) -> int:
        return int(self.agent.get("conversation_context_limit", 40))

    @property
    def agent_memory_refresh_note_limit(self) -> int:
        return int(self.agent.get("memory_refresh_note_limit", 20))

    @property
    def agent_memory_refresh_message_limit(self) -> int:
        return int(self.agent.get("memory_refresh_message_limit", 60))

    @property
    def sub_agent(self) -> dict:
        return self._config.get("sub_agent", {})

    @property
    def sub_agent_structured_output_attempts(self) -> int:
        return max(1, int(self.sub_agent.get("structured_output_attempts", 3)))

    @property
    def sub_agent_retry_delay_seconds(self) -> float:
        return float(self.sub_agent.get("retry_delay_seconds", 2.0))

    @property
    def sub_agent_llm_call_timeout_seconds(self) -> float:
        """Per-attempt hard timeout for sub-agent structured JSON LLM calls.
        Prevents the SSE stream from hanging indefinitely when the model
        stalls. 0 or negative disables the timeout."""
        return float(self.sub_agent.get("llm_call_timeout_seconds", 90.0))

    @property
    def sub_agent_search_decision_max_tokens(self) -> int:
        return int(self.sub_agent.get("search_decision_max_tokens", 800))

    @property
    def sub_agent_search_decision_repair_max_tokens(self) -> int:
        return int(self.sub_agent.get("search_decision_repair_max_tokens", 1200))

    @property
    def sub_agent_keyword_generation_max_tokens(self) -> int:
        return int(self.sub_agent.get("keyword_generation_max_tokens", 2000))

    @property
    def sub_agent_keyword_generation_repair_max_tokens(self) -> int:
        return int(self.sub_agent.get("keyword_generation_repair_max_tokens", 2600))

    @property
    def title_generation(self) -> dict:
        return self._config.get("title_generation", {})

    @property
    def title_generation_structured_output_attempts(self) -> int:
        return max(
            1,
            int(
                self.title_generation.get(
                    "structured_output_attempts",
                    self.sub_agent_structured_output_attempts,
                )
            ),
        )

    @property
    def title_generation_retry_delay_seconds(self) -> float:
        return float(
            self.title_generation.get(
                "retry_delay_seconds",
                self.sub_agent_retry_delay_seconds,
            )
        )

    @property
    def title_generation_max_tokens(self) -> Optional[int]:
        v = self.title_generation.get("max_tokens")
        return int(v) if v not in (None, "", 0) else None

    @property
    def title_generation_repair_max_tokens(self) -> Optional[int]:
        v = self.title_generation.get("repair_max_tokens")
        return int(v) if v not in (None, "", 0) else None

    @property
    def web_search(self) -> dict:
        return self._config.get("web_search", {})

    @property
    def web_search_enabled(self) -> bool:
        return bool(self.web_search.get("enabled", False))

    @property
    def web_search_provider(self) -> str:
        return str(self.web_search.get("provider", "tavily")).strip().lower()

    @property
    def web_search_fallback_providers(self) -> list:
        """Ordered list of providers tried when the primary fails (config key `fallback_providers`)."""
        value = self.web_search.get("fallback_providers")
        if value is None:
            # DuckDuckGo was removed (2026-08-16, unstable); old configs
            # without fallback_providers get no fallback rather than a
            # provider that no longer exists.
            return []
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        return [str(v).strip().lower() for v in value if str(v).strip()]

    @property
    def web_search_api_url(self) -> Optional[str]:
        value = self.web_search.get("api_url", "")
        return value or None

    @property
    def web_search_api_key(self) -> Optional[str]:
        value = self.web_search.get("api_key", "")
        return value or None

    @property
    def web_search_bocha_api_key(self) -> Optional[str]:
        value = self.web_search.get("bocha_api_key", "")
        return value or None

    @property
    def web_search_firecrawl_api_key(self) -> Optional[str]:
        value = self.web_search.get("firecrawl_api_key", "")
        return value or None

    @property
    def web_search_exa_api_key(self) -> Optional[str]:
        """Exa's own key slot — distinct from the shared `api_key` (Tavily/Serper)
        so a Tavily key is never forwarded to mcp.exa.ai."""
        value = self.web_search.get("exa_api_key", "")
        return value or None

    @property
    def web_search_max_results(self) -> int:
        return int(self.web_search.get("max_results", 5))

    @property
    def web_search_timeout_seconds(self) -> float:
        return float(self.web_search.get("timeout_seconds", 12.0))

    @property
    def web_search_max_rounds(self) -> int:
        return int(self.web_search.get("max_search_rounds", 5))

    @property
    def web_search_min_qualified_rounds(self) -> int:
        return int(self.web_search.get("min_qualified_rounds", 2))

    @property
    def web_search_blocked_domains(self) -> list:
        return list(self.web_search.get("blocked_domains", []))

    @property
    def context7_api_key(self) -> Optional[str]:
        value = self._config.get("context7", {}).get("api_key", "")
        return value or None

    @property
    def scheduler(self) -> dict:
        return self._config.get("scheduler", {})

    @property
    def scheduler_enabled(self) -> bool:
        return bool(self.scheduler.get("enabled", True))

    @property
    def scheduler_poll_interval_minutes(self) -> int:
        return int(self.scheduler.get("poll_interval_minutes", 15))

    @property
    def scheduler_run_on_startup(self) -> bool:
        return bool(self.scheduler.get("run_on_startup", True))

    @property
    def workspace(self) -> dict:
        return self._config.get("workspace", {})

    @property
    def workspace_root(self) -> Path:
        return self._resolve_project_path(
            self.workspace.get("root_dir", "user_workspaces"),
            default="user_workspaces",
        )

    @property
    def workspace_use_project_venv(self) -> bool:
        return bool(self.workspace.get("use_project_venv", True))

    @property
    def workspace_create_readme(self) -> bool:
        return bool(self.workspace.get("create_readme", True))

    # ---- Browser skill ----

    @property
    def browser(self) -> dict:
        return self._config.get("browser", {})

    @property
    def browser_enabled(self) -> bool:
        return bool(self.browser.get("enabled", True))

    @property
    def browser_max_content_length(self) -> int:
        return int(self.browser.get("max_content_length", 30000))

    @property
    def browser_max_pages(self) -> int:
        return int(self.browser.get("max_pages", 5))

    @property
    def browser_interaction(self) -> dict:
        return self.browser.get("interaction", {})

    @property
    def browser_interaction_enabled(self) -> bool:
        return bool(self.browser_interaction.get("enabled", False))

    @property
    def browser_interaction_session_timeout(self) -> int:
        return int(self.browser_interaction.get("session_timeout_seconds", 300))

    @property
    def browser_interaction_max_concurrent(self) -> int:
        return int(self.browser_interaction.get("max_concurrent_sessions", 5))

    @property
    def terminal(self) -> dict:
        return self._config.get("terminal", {})

    @property
    def terminal_enabled(self) -> bool:
        return bool(self.terminal.get("enabled", False))

    @property
    def terminal_timeout(self) -> float:
        return float(self.terminal.get("timeout_seconds", 30))

    @property
    def terminal_max_timeout(self) -> float:
        return float(self.terminal.get("max_timeout_seconds", 120))

    @property
    def terminal_max_output(self) -> int:
        return int(self.terminal.get("max_output_chars", 10000))

    # ---- Code execution skill ----

    @property
    def code_execution(self) -> dict:
        return self._config.get("code_execution", {})

    @property
    def code_execution_enabled(self) -> bool:
        return bool(self.code_execution.get("enabled", True))

    @property
    def code_execution_timeout(self) -> float:
        return float(self.code_execution.get("timeout_seconds", 30.0))

    @property
    def code_execution_max_output(self) -> int:
        return int(self.code_execution.get("max_output_chars", 10000))

    @property
    def code_execution_gen_max_tokens(self) -> int:
        return int(self.code_execution.get("gen_max_tokens", 2000))

    @property
    def code_execution_max_retries(self) -> int:
        """Max repair-and-retry attempts when generated code fails at runtime."""
        return int(self.code_execution.get("max_retries", 3))

    @property
    def code_execution_total_timeout_seconds(self) -> float:
        """Overall ceiling for the whole code-execution skill (generation +
        subprocess exec + repair loop). Prevents SSE stream hangs. 0 or
        negative disables the ceiling."""
        return float(self.code_execution.get("total_timeout_seconds", 240.0))

    # ---- Agent tool loop ----

    @property
    def agent_tool_loop(self) -> dict:
        return self.agent.get("tool_loop", {})

    @property
    def agent_background_tasks(self) -> dict:
        return self.agent.get("background_tasks", {})

    @property
    def agent_background_tasks_enabled(self) -> bool:
        return bool(self.agent_background_tasks.get("enabled", True))

    @property
    def agent_background_tasks_max_concurrent(self) -> int:
        return int(self.agent_background_tasks.get("max_concurrent_tasks", 3))

    @property
    def agent_background_tasks_poll_interval(self) -> int:
        return int(self.agent_background_tasks.get("poll_interval_seconds", 3))

    @property
    def agent_background_tasks_total_timeout(self) -> float:
        return float(self.agent_background_tasks.get("total_timeout_seconds", 3600))

    @property
    def agent_background_tasks_progress_update_interval(self) -> int:
        return int(self.agent_background_tasks.get("progress_update_interval_seconds", 2))

    @property
    def agent_tool_loop_conversation_timeout(self) -> float:
        """Maximum seconds without activity (content/tool output) before an
        online conversation turn is considered stuck and terminated gracefully.
        Continuously outputting (but slow) text keeps resetting the timer.
        0 or negative disables.  Default: 300 (5 min)."""
        return float(self.agent_tool_loop.get("inactivity_timeout_seconds", 300))

    @property
    def agent_tool_loop_max_iterations(self) -> int:
        return int(self.agent_tool_loop.get("max_iterations", 50))

    @property
    def agent_tool_loop_max_consecutive_iterations(self) -> int:
        return int(self.agent_tool_loop.get("max_consecutive_iterations", 999))

    @property
    def agent_tool_loop_max_empty_answer_retries(self) -> int:
        """Max consecutive EMPTY final answers the loop retries before
        persisting a visible failure message. A silent empty LLM response
        (no content, no tools, no error — e.g. reasoning budget burn on a
        huge context, conv 517140ca 2026-08-08) previously fell through the
        non-deathmatch else-branch and yielded done-empty with ZERO retries,
        producing "回答生成失败" bubbles after successful tool work. Each
        retry injects a directive + runs with thinking disabled (smaller,
        safer request). Default: 2 (so up to 3 total attempts)."""
        return int(self.agent_tool_loop.get("max_empty_answer_retries", 2))

    @property
    def agent_tool_loop_max_force_stage_rounds(self) -> int:
        """Hard cap on tool-calling rounds AFTER the force-final-answer guard
        fires (A4.9 I1): execute_code rounds refund the iteration budget, so
        without this         cap the forced phase could theoretically run for
        ~max_consecutive_iterations×N rounds (net-zero budget consumption).
        Once the cap is reached, execute_code is no longer offered and the
        model MUST answer in text (empty answers then hit the bounded
        empty-answer retry). Default: 50 — must exceed legit long force
        phases (conv 517140ca novel needed ~25 force rounds to finish
        chapter 5; a 20 default re-stranded the same task)."""
        return int(self.agent_tool_loop.get("max_force_stage_rounds", 50))

    @property
    def agent_tool_loop_parallel_tool_calls(self) -> bool:
        return bool(self.agent_tool_loop.get("parallel_tool_calls", True))

    @property
    def agent_tool_loop_memory_read_dedup(self) -> bool:
        """Per-turn memory-read dedup (conv dfc40619 2026-08-09): the
        coordinator turn-focus directive persists across all iterations and
        the mandatory_tool_use system rule forces fresh tool calls, so the
        model re-issues the SAME ``memory read`` every iteration (35KB func.md
        re-injected per read). When enabled, a second identical read of the
        same target within one turn returns a short "already read" note
        instead of re-reading the file. Default: True."""
        return bool(self.agent_tool_loop.get("memory_read_dedup", True))

    @property
    def agent_tool_loop_tool_call_timeout(self) -> float:
        return float(self.agent_tool_loop.get("tool_call_timeout_seconds", 60))

    @property
    def agent_tool_loop_judge_timeout(self) -> float:
        """Outer deathmatch judge+verifier+replan evaluation budget.
        Prevents the judge from hanging the entire agent loop. 0 = disabled.
        Default 300s: judge ≤30s + verifier ≤120s + replan ≤120s worst case —
        must exceed the sum or the wrapper cancels mid-replan and the stall
        counter climbs on LLM slowness rather than failure (conv 6b0faf81)."""
        return float(self.agent_tool_loop.get("judge_timeout_seconds", 300))

    @property
    def agent_tool_loop_grace_timeout(self) -> float:
        """Timeout for grace call and final-thinking LLM calls.
        Prevents the summary phase from hanging. 0 = disabled."""
        return float(self.agent_tool_loop.get("grace_timeout_seconds", 120))

    @property
    def agent_tool_loop_max_tool_retry_attempts(self) -> int:
        return int(self.agent_tool_loop.get("max_tool_retry_attempts", 2))

    @property
    def agent_subtask_reasoning_char_cap(self) -> int:
        """PHASE 4: maximum cumulative reasoning_content characters tolerated
        during a single tool-calling iteration. When exceeded the iteration
        aborts and falls through, preventing thinking-by-default models
        (qwen3-thinking, deepseek-reasoner) from blowing up the sub-task
        context window. ``0`` disables the cap.
        """
        return int(self.agent_tool_loop.get("subtask_reasoning_char_cap", 8000))

    @property
    def agent_tool_loop_live_thinking(self) -> bool:
        """Stream iteration reasoning+content live (opencode-style) instead of
        suppressing middle iterations and regenerating the answer in a second
        full pass. Eliminates the draft+synthesis double generation (TTFT was
        76-92s on search turns) and gives users immediate visible feedback.

        Tradeoffs (A4.9 review I2/I3, accepted): every tool-calling iteration
        now pays thinking tokens (total token usage and per-iteration latency
        rise vs the legacy thinking-free iterations; TTFT and perceived
        responsiveness improve dramatically). The subtask reasoning char cap
        is bypassed in this mode — cutting the reasoning stream mid-answer
        would truncate the final response itself; runaway CoT is instead
        bounded by the per-iteration timeout and the provider thinking budget.

        Default True; set False to restore the legacy suppress+regenerate flow.
        """
        return bool(self.agent_tool_loop.get("live_thinking", True))

    @property
    def agent_subtask_iteration_timeout(self) -> float:
        """PHASE 4: per-iteration wall-clock timeout (seconds). Iterations
        exceeding this abort and let the outer loop continue to the next
        round (or grace-call). ``0`` disables the timeout.
        """
        return float(self.agent_tool_loop.get("subtask_iteration_timeout_seconds", 600))

    # ──────────────────────────────────────────────────────────────────
    # Deathmatch (死磕) mode — typed properties for [deathmatch] section.
    # Previously all reads went through config._config.get("deathmatch", {})
    # which is error-prone. See loop_improve.md Phase 3.2.
    # ──────────────────────────────────────────────────────────────────
    @property
    def deathmatch(self) -> dict:
        return self._config.get("deathmatch", {})

    @property
    def deathmatch_enabled(self) -> bool:
        return bool(self.deathmatch.get("enabled", True))

    @property
    def deathmatch_max_turns(self) -> int:
        return int(self.deathmatch.get("max_turns", 30))

    @property
    def deathmatch_max_consecutive_failures(self) -> int:
        return int(self.deathmatch.get("max_consecutive_failures", 5))

    @property
    def deathmatch_tool_loop_max_iterations(self) -> int:
        """Independent AgentLoop iteration budget for deathmatch goal loop.
        Overrides the normal tool-loop cap so deep loops aren't truncated."""
        return int(self.deathmatch.get("tool_loop_max_iterations", 999))

    @property
    def deathmatch_max_wall_time_seconds(self) -> int:
        return int(self.deathmatch.get("max_wall_time_seconds", 3600))

    @property
    def deathmatch_verify_enabled(self) -> bool:
        return bool(self.deathmatch.get("verify_enabled", False))

    @property
    def deathmatch_verify_max_retries(self) -> int:
        return int(self.deathmatch.get("verify_max_retries", 20))

    @property
    def deathmatch_stall_partial_threshold(self) -> int:
        """After N consecutive stalls, offer partial completion with deliverables.

        Raised 3→8 for long-horizon autonomy (conv 6b0faf81): a 100k-char
        novel goal hit 3 stalls in 4 turns and stopped. Deathmatch should
        keep working through direction changes like opencode goal mode;
        partial_complete is now reserved for genuinely persistent no-progress."""
        return int(self.deathmatch.get("stall_partial_threshold", 8))

    @property
    def deathmatch_stall_hard_threshold(self) -> int:
        """After N consecutive stalls, force human gate (absolute backstop).

        Raised 6→16 alongside partial threshold — with "plan complete but
        goal unmet" no longer counting as a stall, the remaining stall paths
        (verifier blocked / no-progress partial) need room to self-correct
        via replanning before interrupting the user."""
        return int(self.deathmatch.get("stall_hard_threshold", 16))

    @property
    def deathmatch_verify_interval(self) -> int:
        """Run the verifier every N outer turns (not every turn)."""
        return int(self.deathmatch.get("verify_interval", 3))

    @property
    def deathmatch_verify_model(self) -> str:
        return str(self.deathmatch.get("verify_model", "") or "")

    @property
    def deathmatch_bible_enabled(self) -> bool:
        """Story-bible spec files for creative goals: on goal-loop start,
        generate workspace/bible/{characters,relationships,world,outline,
        style}.md so the agent works against a file-based spec (user-editable,
        compression-proof) instead of free-text goal only."""
        return bool(self.deathmatch.get("bible_enabled", True))

    @property
    def deathmatch_verify_moa_enabled(self) -> bool:
        """A5: route the verifier through MoA (multi-model aggregation) when
        [agent.moa] is enabled and reference providers are configured. The
        aggregate verdict reduces single-model bias at extra cost."""
        return bool(self.deathmatch.get("verify_moa_enabled", False))

    @property
    def deathmatch_verify_command_gate_enabled(self) -> bool:
        """A1b: deterministic verification commands — a step's
        verification_method starting with 'gate: <shell command>' runs inside
        the code-execution sandbox BEFORE the LLM verifier; non-zero exit
        short-circuits to partial with the gate output as the issue.
        OFF by default (shell execution in the sandbox is opt-in)."""
        return bool(self.deathmatch.get("verify_command_gate_enabled", False))

    @property
    def deathmatch_plan_exploration_enabled(self) -> bool:
        return bool(self.deathmatch.get("plan_exploration_enabled", True))

    @property
    def deathmatch_plan_exploration_max_steps(self) -> int:
        return int(self.deathmatch.get("plan_exploration_max_steps", 5))

    @property
    def deathmatch_reflection_memory_max_items(self) -> int:
        return int(self.deathmatch.get("reflection_memory_max_items", 10))

    @property
    def deathmatch_continuity_anchor_enabled(self) -> bool:
        """Continuity anchoring: the verifier emits a distilled per-turn
        continuity_brief injected into the next continuation prompt, and the
        verifier reads head+tail of this-turn/prior files to catch long-content
        drift (style/plot/setting deviation). Default on."""
        return bool(self.deathmatch.get("continuity_anchor_enabled", True))

    @property
    def deathmatch_judge(self) -> dict:
        return self.deathmatch.get("judge", {})

    @property
    def agent_auxiliary(self) -> dict:
        return self.agent.get("auxiliary", {})

    @property
    def agent_auxiliary_compression_model(self) -> str:
        return str(self.agent_auxiliary.get("compression_model", "") or "")

    @property
    def agent_auxiliary_search_decision_model(self) -> str:
        return str(self.agent_auxiliary.get("search_decision_model", "") or "")

    @property
    def agent_auxiliary_title_model(self) -> str:
        return str(self.agent_auxiliary.get("title_model", "") or "")

    @property
    def agent_auxiliary_coordinator_model(self) -> str:
        """P2-1: Separate model for coordinator routing/classification.
        When non-empty, coordinator calls use this lighter model instead of
        the main LLM. Reduces coordinator TTFT from 1-3s → 0.3-0.8s (Flash-class)."""
        return str(self.agent_auxiliary.get("coordinator_model", "") or "")

    @property
    def agent_auxiliary_classifier_model(self) -> str:
        """Model for agentic judgment calls (error classification, triviality,
        interest extraction, skill assessment, identity facts, citation
        disambiguation, schedule parsing, creative-goal detection, completion
        reconciliation). Empty → main LLM."""
        return str(self.agent_auxiliary.get("classifier_model", "") or "")

    @property
    def agent_cache(self) -> dict:
        return self.agent.get("cache", {})

    @property
    def agent_cache_enabled(self) -> bool:
        return bool(self.agent_cache.get("enabled", False))

    @property
    def agent_cache_ttl_minutes(self) -> int:
        return int(self.agent_cache.get("cache_ttl_minutes", 5))

    @property
    def agent_compression(self) -> dict:
        return self.agent.get("compression", {})

    @property
    def agent_compression_enabled(self) -> bool:
        return bool(self.agent_compression.get("enabled", False))

    @property
    def agent_compression_context_length(self) -> int:
        """The model's actual context window size in tokens.
        Used by ContextCompressor to calculate compression thresholds."""
        return int(self.agent_compression.get("context_length", 65536))

    # ---- Canary marker (遵循词) ----

    @property
    def agent_canary(self) -> dict:
        return self.agent.get("canary", {})

    @property
    def agent_canary_enabled(self) -> bool:
        """遵循词 (canary marker) context-rot detection. When enabled the
        system prompt asks the model to end every final reply with a
        per-conversation marker; two consecutive misses trigger one context
        compression (see app/services/canary_marker.py)."""
        return bool(self.agent_canary.get("enabled", False))

    @property
    def agent_canary_miss_threshold(self) -> int:
        return int(self.agent_canary.get("miss_threshold", 2))

    @property
    def agent_canary_auto_disable_after(self) -> int:
        return int(self.agent_canary.get("auto_disable_after", 3))

    @property
    def agent_canary_compress_min_ratio(self) -> float:
        """P0/A3 (2026-08-21): canary trip compresses only when the estimated
        context occupies >= this ratio of the model window. Below it, the
        miss is not context rot — re-assert the marker directive and re-answer
        instead. 0 = legacy always-compress behavior."""
        return float(self.agent_canary.get("compress_min_ratio", 0.6))

    # ---- Response auditor (发送前质量审计) ----

    @property
    def agent_audit(self) -> dict:
        return self.agent.get("audit", {})

    @property
    def agent_audit_reject_budget(self) -> int:
        """发送前审计的拒绝预算：一轮内审计员最多打回几稿。打回次数超过预算
        后停止重写，走有界 salvage（去毒上下文再生成一次，再审一次，仍不过
        则 ship 诚实失败文本）。普通轮次默认 2。"""
        return int(self.agent_audit.get("reject_budget", 2))

    @property
    def agent_audit_numeric_gate_enabled(self) -> bool:
        """A1 (2026-08-21): deterministic backstop for numeric audit verdicts.
        A reject whose asserted correction value appears nowhere in the
        evidence ledger is the auditor's own arithmetic (the template's
        mental-math ban) — downgrade to needs_evidence instead of looping."""
        return bool(self.agent_audit.get("numeric_gate_enabled", True))

    @property
    def agent_audit_retry_reasoning_keep_chars(self) -> int:
        """A2 (2026-08-21): char cap of the rejected draft's reasoning_content
        re-attached on audit-retry for preserve-thinking providers.
        0 = disabled."""
        return int(self.agent_audit.get("retry_reasoning_keep_chars", 6000))

    @property
    def agent_audit_revision_thinking_enabled(self) -> bool:
        """用户要求（2026-08-21）：审核打回后的修正轮不开思考。
        False（默认）= 打回后的修正轮 thinking 关闭（更快、上游挂死面更小）；
        True = 显式保持思考（操作者可覆写）。"""
        return bool(self.agent_audit.get("revision_thinking_enabled", False))

    @property
    def agent_audit_call_timeout_seconds(self) -> float:
        """审计 LLM 非流式调用硬超时（防审计挂死阻塞整个轮次）。
        超时走既有 fail-open 契约（审计器故障不得阻塞好答案）。0 = 不设限。"""
        return float(self.agent_audit.get("call_timeout_seconds", 600))

    @property
    def agent_audit_revision_min_similarity(self) -> float:
        """用户要求（2026-08-21）：修正轮仅修改点名部分，不整篇重写。
        修正稿与被拒稿的 difflib 相似度低于该阈值 → 判定整篇重写，
        不送审计、注入重改指令一次。0 = 关闭闸门。"""
        return float(self.agent_audit.get("revision_min_similarity", 0.45))

    @property
    def agent_audit_reject_budget_search(self) -> int:
        """search_required=True（用户明确要求联网检索）轮次的拒绝预算，
        默认 4 —— 检索轮次上下文更大、模型更易跑偏，多给两次修正机会。"""
        return int(self.agent_audit.get("reject_budget_search", 4))

    @property
    def agent_audit_max_evidence_tokens(self) -> int:
        """审计器可用的证据 token 预算（estimate_text_tokens_rough，CJK 感知）。
        用户原则（2026-08-14）：信息完整性永远高于节省 token——预算只做
        「模型上下文装不下」的物理上限，不做省钱上限。grounding 类工具结果
        （memory/workspace_read/web_search/browser）优先读回磁盘全文（digest/
        budget 的【全文存档】指针），超预算才退到压缩信封/头尾片段并显式标注，
        截断导致的"看不到"走 unverifiable 出口，禁止按"凭空编造"误杀
        （conv a67faa04）。2026-08-18 用户决定 60000→128000：证据全量读回
        优先，截断只留给真正的物理极限。"""
        return int(self.agent_audit.get("max_evidence_tokens", 128000))

    @property
    def agent_audit_soft_reject_limit(self) -> int:
        """unverifiable/needs_evidence（非编造类打回）的每轮独立计数上限。
        这类打回不消耗 reject_budget（conv a67faa04：截断导致的误判不应把
        拒绝预算烧光导致失败文本）；达到本上限后同样走有界 salvage。默认 3。"""
        return int(self.agent_audit.get("soft_reject_limit", 3))

    @property
    def agent_audit_draft_selection_enabled(self) -> bool:
        """审计耗尽 best-of 选优兜底开关（conv 7dc7a0d5，2026-08-18）。
        开启时：salvage 仍失败（被拒/空/异常/超时）后，由独立选优调用综合
        全部被拒草稿与各自审计意见产出最终回答；选优也失败时按确定性规则
        （软拒优先→最早草稿）出货带警示前缀的草稿——最终回答不再显示
        「回答生成失败」。关闭时完全回退旧行为（失败文本）。"""
        return bool(self.agent_audit.get("draft_selection_enabled", True))

    @property
    def agent_audit_selection_timeout_seconds(self) -> float:
        """选优调用的生成超时（秒）。conv 7dc7a0d5 的直接死因是 salvage 复用
        120s grace 超时且首个 content 事件未及到达——选优是同类长文再生成，
        默认给更宽的 240s；0 = 复用 [agent.tool_loop] grace_timeout_seconds。
        conv efaf8f9c（2026-08-20 回放实证）：xhigh 思考模式下选优 240s 产出
        <200 字符即被截断，浪费最后一次 LLM 修复机会 → 默认 480s。"""
        return float(self.agent_audit.get("selection_timeout_seconds", 480.0))

    @property
    def agent_audit_salvage_timeout_seconds(self) -> float:
        """审计预算耗尽后 salvage 重整生成的超时（秒）。conv efaf8f9c
        （2026-08-20）：salvage 复用了 120s grace 超时，而开启思考模式的
        迭代 LLM（qwen3.8_27b xhigh）首 content token 延迟可超过 120s →
        salvage 每次零内容必死、白耗 2 分钟才进入选优。默认 240s（与选优
        同级）；0 = 复用 [agent.tool_loop] grace_timeout_seconds。"""
        return float(self.agent_audit.get("salvage_timeout_seconds", 240.0))

    @property
    def agent_audit_selection_draft_budget_tokens(self) -> int:
        """选优 prompt 中「历史草稿块」的 token 预算（CJK 感知估算，A4.9 M7）。
        超预算时先按 selection_draft_window_chars 头尾窗口化每份草稿（显式标注），
        仍超则丢弃最旧草稿（显式标注）。信息完整性优先：预算只防 provider
        上下文物理溢出，不做省钱上限。"""
        return int(self.agent_audit.get("selection_draft_budget_tokens", 60000))

    @property
    def agent_audit_selection_draft_window_chars(self) -> int:
        """选优 prompt 草稿超预算时的每份头尾窗口（字符，A4.9 M7）。"""
        return int(self.agent_audit.get("selection_draft_window_chars", 6000))

    @property
    def agent_audit_evidence_context_ceiling_tokens(self) -> int:
        """审计 prompt 总量（模板+上下文+证据+输出预留）的 provider 上下文
        天花板（A4.9 M8，默认 128000 ≈ DeepSeek 上下文）。运行时证据预算 =
        min(max_evidence_tokens, 本值 − 非证据预留 10000)——防止 128k 证据
        在 128k 上下文 provider 上整体超限 400 → fail-open 绕过
        「截断→unverifiable」优雅路径。provider 上下文更大时可调高。"""
        return int(self.agent_audit.get("evidence_context_ceiling_tokens", 128000))

    # ---- PTC (Programmatic Tool Calling) ----

    @property
    def agent_ptc(self) -> dict:
        return self.agent.get("ptc", {})

    @property
    def agent_ptc_enabled(self) -> bool:
        return bool(self.agent_ptc.get("enabled", False))

    @property
    def agent_ptc_allowed_tools(self) -> list:
        return list(self.agent_ptc.get("allowed_tools", ["web_search", "browser", "memory"]))

    # ---- Tool catalog visibility (P2-2) ----

    @property
    def agent_tools(self) -> dict:
        return self.agent.get("tools", {})

    @property
    def agent_tools_visible(self) -> list:
        """Global visible-tools allowlist (opencode visibleTools analogue).

        Empty list (default) = all registered tools are offered to the model.
        When non-empty, AgentLoop only injects schemas for the listed names and
        registry.dispatch fail-closes on anything outside the set — reducing
        per-turn prefill tokens and tool-catalog attention dilution.
        """
        return list(self.agent_tools.get("visible_tools", []) or [])

    # ---- Synthetic system directives (4.8) ----

    @property
    def agent_synthetic_directives_enabled(self) -> bool:
        """When True (default), internal agent directives (turn focus, audit
        guidance, nudges, deathmatch continuations, grace prompts) are injected
        as role="system" messages flagged synthetic=True instead of polluting
        the user role. Set False to restore the legacy role="user" behavior."""
        return bool(self.agent.get("synthetic_directives_enabled", True))

    # ---- Session search ----

    @property
    def agent_session_search(self) -> dict:
        return self.agent.get("session_search", {})

    @property
    def agent_session_search_enabled(self) -> bool:
        return bool(self.agent_session_search.get("enabled", True))

    # ---- Error recovery ----

    @property
    def agent_error_recovery(self) -> dict:
        return self.agent.get("error_recovery", {})

    @property
    def agent_error_recovery_enabled(self) -> bool:
        return bool(self.agent_error_recovery.get("enabled", True))

    @property
    def agent_error_recovery_max_retries(self) -> int:
        return int(self.agent_error_recovery.get("max_retries", 2))

    # ---- Delegation ----

    @property
    def agent_delegation(self) -> dict:
        return self.agent.get("delegation", {})

    @property
    def agent_delegation_max_depth(self) -> int:
        return int(self.agent_delegation.get("max_depth", 2))

    @property
    def agent_delegation_default_child_max_iterations(self) -> int:
        return int(self.agent_delegation.get("default_child_max_iterations", 999))

    @property
    def agent_delegation_default_child_timeout(self) -> int:
        return int(self.agent_delegation.get("default_child_timeout", 300))

    # ---- Tool digest (subagent near-lossless reduction of large results) ----

    @property
    def agent_tool_digest(self) -> dict:
        return self.agent.get("tool_digest", {})

    @property
    def agent_tool_digest_enabled(self) -> bool:
        # Default OFF in code: upgrades without a [agent.tool_digest] section
        # must not silently start paying for parallel subagent calls. The
        # shipped config.toml enables it explicitly.
        return bool(self.agent_tool_digest.get("enabled", False))

    @property
    def agent_tool_digest_min_chars(self) -> int:
        return int(self.agent_tool_digest.get("min_digest_chars", 8000))

    @property
    def agent_tool_digest_max_chars(self) -> int:
        return int(self.agent_tool_digest.get("max_digest_chars", 6000))

    @property
    def agent_tool_digest_max_concurrent(self) -> int:
        return int(self.agent_tool_digest.get("max_concurrent", 5))

    @property
    def agent_tool_digest_max_tokens(self) -> Optional[int]:
        """不设 == provider 默认最大输出（用户指令 2026-08-18）。"""
        v = self.agent_tool_digest.get("max_tokens")
        return int(v) if v not in (None, "", 0) else None

    @property
    def agent_tool_digest_timeout_seconds(self) -> float:
        return float(self.agent_tool_digest.get("timeout_seconds", 120))

    @property
    def agent_tool_digest_batch_timeout_seconds(self) -> float:
        """Aggregate deadline for one digest batch (parallel path). Kept at or
        below the conversation inactivity timeout (300s) so the loop can never
        stall past its own watchdog. 0 disables."""
        return float(self.agent_tool_digest.get("batch_timeout_seconds", 300))

    @property
    def agent_tool_digest_temperature(self) -> float:
        return float(self.agent_tool_digest.get("temperature", 0.3))

    @property
    def agent_tool_digest_verify(self) -> bool:
        """Fact-check pass: one extra LLM call re-checks the digest against
        the original and appends corrections (hardens the near-lossless
        guarantee)."""
        return bool(self.agent_tool_digest.get("verify", True))

    @property
    def agent_tool_digest_model(self) -> str:
        return str(self.agent_tool_digest.get("model", "") or "")

    @property
    def agent_tool_digest_tools(self) -> list:
        from app.services.tool_result_digest import DEFAULT_DIGEST_TOOLS
        return [str(t) for t in self.agent_tool_digest.get("digest_tools", DEFAULT_DIGEST_TOOLS)]

    # ---- MoA (Mixture of Agents) ----

    @property
    def agent_moa(self) -> dict:
        return self.agent.get("moa", {})

    @property
    def agent_moa_enabled(self) -> bool:
        return bool(self.agent_moa.get("enabled", False))

    @property
    def agent_moa_reference_providers(self) -> list:
        return list(self.agent_moa.get("reference_providers", []))

    @property
    def agent_moa_aggregator_provider(self) -> str:
        return str(self.agent_moa.get("aggregator_provider", "default"))

    @property
    def agent_moa_max_reference_models(self) -> int:
        return int(self.agent_moa.get("max_reference_models", 3))

    @property
    def agent_moa_timeout_seconds(self) -> float:
        return float(self.agent_moa.get("timeout_seconds", 120))

    # ---- Skill Evolution ----

    @property
    def agent_skill_evolution(self) -> dict:
        return self.agent.get("skill_evolution", {})

    @property
    def agent_skill_evolution_enabled(self) -> bool:
        return bool(self.agent_skill_evolution.get("enabled", True))

    @property
    def agent_skill_evolution_auto_suggest_threshold(self) -> int:
        return int(self.agent_skill_evolution.get("auto_suggest_threshold", 5))

    # ---- Proactive Learning ----

    @property
    def agent_proactive_learning(self) -> dict:
        return self.agent.get("proactive_learning", {})

    @property
    def agent_proactive_learning_enabled(self) -> bool:
        return bool(self.agent_proactive_learning.get("enabled", True))

    @property
    def agent_proactive_learning_user_modeling_enabled(self) -> bool:
        return bool(self.agent_proactive_learning.get("user_modeling_enabled", True))

    # ---- Sub-agent ----

    @property
    def agent_sub_agent(self) -> dict:
        return self.agent.get("sub_agent", {})

    # ---- Agent memory ----

    @property
    def agent_memory(self) -> dict:
        return self.agent.get("memory", {})

    # ---- Agent media localization ----

    @property
    def agent_media_localize(self) -> dict:
        """[agent.media_localize] — download remote media the agent displays into
        the user's workspace (content-addressed sha256) and rewrite answer URLs.
        Keys: enabled, max_per_message, timeout_seconds,
        max_image_bytes, max_audio_bytes, max_video_bytes,
        allow_private_hosts, total_timeout_seconds,
        max_dir_bytes, neg_cache_ttl_seconds."""
        return self.agent.get("media_localize", {})

    # ---- Memory & Dreaming v2 ----

    @property
    def memory(self) -> dict:
        return self._config.get("memory", {})

    @property
    def memory_concept(self) -> dict:
        return self.memory.get("concept", {})

    @property
    def memory_retrieval(self) -> dict:
        return self.memory.get("retrieval", {})

    @property
    def memory_subconscious(self) -> dict:
        return self.memory.get("subconscious", {})

    @property
    def memory_episodic(self) -> dict:
        return self.memory.get("episodic", {})

    @property
    def memory_multimodal(self) -> dict:
        return self.memory.get("multimodal", {})

    @property
    def memory_cost_governance(self) -> dict:
        return self.memory.get("cost_governance", {})

    @property
    def memory_fatigue(self) -> dict:
        return self.memory_concept.get("fatigue", {})

    @property
    def memory_timezone(self) -> str:
        return self.memory.get("timezone", "Asia/Shanghai")

    # ---- Providers ----

    @property
    def providers(self) -> dict:
        return self._config.get("providers", {})

    @property
    def provider_configs(self) -> dict:
        """Return provider configs keyed by provider type.
        Example structure from config.toml:
        [providers.deepseek]
        base_url = "https://api.deepseek.com/v1"
        api_key = "sk-..."
        model_name = "deepseek-v4-flash"

        [providers.zhipu]
        base_url = "https://api.zhipu.ai/v1"

        [providers.qwen]
        base_url = "https://api.qwen.ai/v1"
        """
        providers = {}
        raw = self._config.get("providers", {})
        if not isinstance(raw, dict):
            return providers
        for key, value in raw.items():
            if isinstance(value, dict):
                providers[key] = {
                    "base_url": str(value.get("base_url", "")).strip(),
                    "api_key": str(value.get("api_key", "")).strip(),
                    "model_name": str(value.get("model_name", "")).strip(),
                }
        # Always ensure deepseek falls back to [api] section if not explicitly configured
        if "deepseek" not in providers:
            providers["deepseek"] = {
                "base_url": self.api_base_url,
                "api_key": self.api_key or "",
                "model_name": self.model_name or "",
            }
        else:
            pd = providers["deepseek"]
            if not pd["base_url"]:
                pd["base_url"] = self.api_base_url
            if not pd["api_key"]:
                pd["api_key"] = self.api_key or ""
            if not pd["model_name"]:
                pd["model_name"] = self.model_name or ""
        return providers

    # Assistant provider_type → actual [providers.*] config key. The
    # Qwen3.8(Local) UI type maps to the qwen3.8_27b vLLM deployment.
    _PROVIDER_TYPE_ALIASES = {"qwen3.8_vllm": "qwen3.8_27b"}

    def get_provider_config(self, provider_type: str) -> dict:
        """Get config for a specific provider type."""
        cfg = self.provider_configs.get(provider_type, {}) or self.provider_configs.get(
            self._PROVIDER_TYPE_ALIASES.get(provider_type, ""), {}
        )
        return {
            "base_url": cfg.get("base_url", ""),
            "api_key": cfg.get("api_key", ""),
            "model_name": cfg.get("model_name", ""),
        }


@lru_cache()
def get_config() -> Config:
    return Config()


def clear_config_cache() -> None:
    get_config.cache_clear()
