# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""MiMo TTS (mimo-v2.5-tts) streaming client.

Streams PCM16 (24kHz, mono, little-endian) audio chunks from the MiMo
OpenAI-compatible `/chat/completions` endpoint. Each call synthesizes a single
text segment; the caller is responsible for endpoint segmentation and ordering.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import AsyncIterator, Optional

import httpx

from app.core.config import get_config
from app.services.http_client import get_shared_async_client

logger = logging.getLogger(__name__)


class TTSService:
    def __init__(self):
        self.config = get_config()

    # --- config resolution -------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.voice_tts_enabled)

    def _resolve_base_url(self) -> str:
        if self.config.voice_tts_base_url:
            return self.config.voice_tts_base_url.rstrip("/")
        # Fall back to the configured mimo provider base_url.
        try:
            from app.services.provider_router import get_provider_router

            router = get_provider_router()
            adapter = router.get_provider("mimo")
            if adapter and getattr(adapter, "config", None) and adapter.config.base_url:
                return adapter.config.base_url.rstrip("/")
        except Exception:
            pass
        return self.config.api_base_url.rstrip("/")

    def _resolve_api_key(self) -> str:
        if self.config.voice_tts_api_key:
            return self.config.voice_tts_api_key
        try:
            from app.services.provider_router import get_provider_router

            router = get_provider_router()
            adapter = router.get_provider("mimo")
            if adapter and getattr(adapter, "config", None) and adapter.config.api_key:
                return adapter.config.api_key
        except Exception:
            pass
        return self.config.api_key or ""

    @property
    def model(self) -> str:
        return self.config.voice_tts_model or "mimo-v2.5-tts"

    @property
    def voice(self) -> str:
        return self.config.voice_tts_voice or "冰糖"

    def is_configured(self) -> bool:
        return self.enabled and bool(self._resolve_base_url()) and bool(self._resolve_api_key())

    # --- streaming ---------------------------------------------------------
    async def stream_tts(
        self,
        text: str,
        style_instruction: Optional[str] = None,
        timeout: float = 60.0,
    ) -> AsyncIterator[bytes]:
        """Stream PCM16 audio chunks for a single text segment.

        ``style_instruction`` is a natural-language style directive placed in the
        ``user`` message (consistent across segments). ``text`` is placed in the
        ``assistant`` message and may carry inline ``(风格)`` / ``[音频标签]``.
        """
        if not text or not text.strip():
            return

        base_url = self._resolve_base_url()
        api_key = self._resolve_api_key()
        if not base_url or not api_key:
            raise RuntimeError("TTS is not configured (missing base_url or api_key)")

        user_content = style_instruction or self.config.voice_tts_style_instruction or ""
        messages = [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": text},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "audio": {"format": "pcm16", "voice": self.voice},
            "stream": True,
        }

        client = get_shared_async_client()
        try:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise RuntimeError(
                        f"TTS request failed: HTTP {response.status_code}: {body.decode('utf-8', 'ignore')[:400]}"
                    )
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    audio = delta.get("audio")
                    if isinstance(audio, dict) and audio.get("data"):
                        try:
                            pcm = base64.b64decode(audio["data"])
                        except Exception:
                            continue
                        if pcm:
                            yield pcm
        except httpx.HTTPError as exc:
            logger.error("TTS streaming HTTP error: %s", exc)
            raise


_tts_service: Optional[TTSService] = None


def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
