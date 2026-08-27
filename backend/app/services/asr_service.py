# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import base64
import json
import logging
import os
import struct
import uuid
import time
import httpx
from typing import Optional, AsyncIterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.core.config import get_config
from app.services.http_client import get_shared_async_client

logger = logging.getLogger(__name__)


def _hotword_text(item) -> str:
    if item is None:
        return ""
    if isinstance(item, dict):
        return str(item.get("text", "")).strip()
    return str(getattr(item, "text", "")).strip()


def _hotword_weight(item) -> int:
    if item is None:
        return 4
    if isinstance(item, dict):
        return int(item.get("weight", 4))
    return int(getattr(item, "weight", 4))


def apply_hotword_phonetic_correction(text: str, hotwords: Optional[list]) -> str:
    """Replace phonetically identical ASR outputs with configured hotwords.

    Some ASR models (especially for rare Chinese characters) return homophones
    instead of the exact hotword text. This function uses pinyin to map those
    homophones back to the intended hotword.
    """
    if not text or not hotwords:
        return text

    try:
        from pypinyin import lazy_pinyin
    except Exception:
        return text

    hotword_map: dict[str, tuple[str, int]] = {}
    for item in hotwords:
        word = _hotword_text(item)
        if len(word) < 2:
            continue
        pinyin = " ".join(lazy_pinyin(word))
        if pinyin not in hotword_map or _hotword_weight(item) > hotword_map[pinyin][1]:
            hotword_map[pinyin] = (word, _hotword_weight(item))

    if not hotword_map:
        return text

    # Sort by word length (desc) then weight (desc) so longer / stronger matches win.
    ordered = sorted(hotword_map.items(), key=lambda kv: (-len(kv[1][0]), -kv[1][1]))
    chars = list(text)
    i = 0
    while i < len(chars):
        matched = False
        for pinyin, (word, _) in ordered:
            n = len(word)
            if i + n > len(chars):
                continue
            substr = "".join(chars[i:i + n])
            if substr == word:
                i += n
                matched = True
                break
            substr_pinyin = " ".join(lazy_pinyin(substr))
            if substr_pinyin == pinyin:
                chars[i:i + n] = list(word)
                i += n
                matched = True
                logger.debug("Phonetic hotword correction: %r -> %r", substr, word)
                break
        if not matched:
            i += 1

    return "".join(chars)


class ASRService:
    @property
    def _asr_config(self) -> dict:
        return get_config().asr

    @property
    def base_url(self) -> str:
        return self._asr_config.get("base_url", "")

    @property
    def model(self) -> str:
        return self._asr_config.get("model", "qwen3-asr")

    @property
    def is_dashscope(self) -> bool:
        return bool(self._asr_config.get("is_dashscope", False))

    @property
    def dashscope_api_key(self) -> str:
        return self._asr_config.get("dashscope_api_key", "")

    @property
    def dashscope_model(self) -> str:
        return self._asr_config.get("dashscope_model", "qwen3-asr-flash-realtime-2026-02-10")

    @property
    def is_mimo(self) -> bool:
        return bool(self._asr_config.get("is_mimo", False))

    @property
    def api_key(self) -> str:
        return self._asr_config.get("api_key", "")

    @property
    def enabled(self) -> bool:
        if self.is_mimo:
            return bool(self.base_url and self.api_key)
        if self.is_dashscope:
            return bool(self.dashscope_api_key)
        return bool(self.base_url)

    @property
    def websocket_url(self) -> str:
        if not self.base_url:
            return ""

        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        stream_path = f"{base_path}/ws/transcribe/stream" if base_path else "/ws/transcribe/stream"
        return urlunparse((scheme, parsed.netloc, stream_path, "", "", ""))

    @property
    def mimo_partial_interval_seconds(self) -> float:
        return float(self._asr_config.get("mimo_partial_interval_seconds", 1.5))

    @property
    def mimo_partial_min_seconds(self) -> float:
        return float(self._asr_config.get("mimo_partial_min_seconds", 0.5))

    @property
    def hotword_phonetic_correction(self) -> bool:
        return bool(self._asr_config.get("hotword_phonetic_correction", True))

    def get_audio_files_dir(self) -> Path:
        backend_dir = Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        audio_dir = backend_dir / "audio_files"
        audio_dir.mkdir(parents=True, exist_ok=True)
        return audio_dir

    async def save_audio_file(self, audio_data: bytes, original_filename: str = "audio.wav") -> Path:
        audio_dir = self.get_audio_files_dir()
        timestamp = int(time.time() * 1000)
        unique_id = uuid.uuid4().hex[:8]
        file_ext = Path(original_filename).suffix or ".wav"
        filename = f"{timestamp}_{unique_id}{file_ext}"
        file_path = audio_dir / filename
        await asyncio.to_thread(file_path.write_bytes, audio_data)
        return file_path

    def _detect_audio_type(self, audio_data: bytes) -> tuple[str, str]:
        if audio_data[:4] == b'RIFF' and audio_data[8:12] == b'WAVE':
            return ("audio.wav", "audio/wav")
        elif audio_data[:4] == b'\x1aE\xdf\xa3':
            return ("audio.webm", "audio/webm")
        elif audio_data[:4] == b'fLaC':
            return ("audio.flac", "audio/flac")
        elif audio_data[:6] == b'ID3' or audio_data[:2] in [b'\xff\xfb', b'\xff\xf3', b'\xff\xf9']:
            return ("audio.mp3", "audio/mpeg")
        return ("audio.wav", "audio/wav")

    @staticmethod
    def _float32_to_pcm16(float32_bytes: bytes) -> bytes:
        num_samples = len(float32_bytes) // 4
        if num_samples == 0:
            return b""
        try:
            floats = struct.unpack(f'<{num_samples}f', float32_bytes[:num_samples * 4])
        except struct.error:
            return b""
        pcm16_samples = []
        for f in floats:
            clamped = max(-1.0, min(1.0, f))
            pcm16_samples.append(int(clamped * 32767))
        return struct.pack(f'<{len(pcm16_samples)}h', *pcm16_samples)

    @staticmethod
    def _float32_to_pcm16_base64(float32_bytes: bytes) -> str:
        pcm16_bytes = ASRService._float32_to_pcm16(float32_bytes)
        if not pcm16_bytes:
            return ""
        return base64.b64encode(pcm16_bytes).decode('ascii')

    @staticmethod
    def _pcm16_to_wav(pcm16_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
        """Wrap raw PCM16 bytes in a minimal WAV header."""
        if not pcm16_bytes:
            return b""
        byte_rate = sample_rate * channels * 2
        block_align = channels * 2
        data_chunk_size = len(pcm16_bytes)
        fmt_chunk_size = 16
        audio_format = 1  # PCM
        header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF',
            36 + data_chunk_size,
            b'WAVE',
            b'fmt ',
            fmt_chunk_size,
            audio_format,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            16,
            b'data',
            data_chunk_size,
        )
        return header + pcm16_bytes

    @staticmethod
    def _float32_to_wav(float32_bytes: bytes) -> bytes:
        pcm16 = ASRService._float32_to_pcm16(float32_bytes)
        return ASRService._pcm16_to_wav(pcm16)

    def _build_mimo_asr_payload(self, audio_base64: str, mime_type: str = "audio/wav", language: str = "auto") -> dict:
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:{mime_type};base64,{audio_base64}"
                            }
                        }
                    ]
                }
            ],
            "asr_options": {
                "language": language
            }
        }

    async def _convert_audio_to_wav(self, audio_data: bytes, filename: str = "audio.wav") -> bytes:
        """Convert input audio bytes to a 16 kHz mono WAV, reusing the temp file layout."""
        detected_name, detected_type = self._detect_audio_type(audio_data)
        audio_dir = self.get_audio_files_dir()
        timestamp = int(time.time() * 1000)
        unique_id = uuid.uuid4().hex[:8]
        original_file = audio_dir / f"{timestamp}_{unique_id}_{detected_name}"
        await asyncio.to_thread(original_file.write_bytes, audio_data)

        wav_bytes: bytes
        try:
            need_conversion = detected_type in ("audio/webm", "audio/flac", "audio/mpeg", "audio/mp3")
            if need_conversion:
                wav_file = audio_dir / f"{timestamp}_{unique_id}_converted.wav"
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", str(original_file), "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", str(wav_file),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(),
                            timeout=int(self._asr_config.get("ffmpeg_timeout_seconds", 60))
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        raise RuntimeError("ffmpeg conversion timed out")
                    if proc.returncode != 0:
                        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()}")
                    wav_bytes = await asyncio.to_thread(wav_file.read_bytes)
                finally:
                    if await asyncio.to_thread(wav_file.exists):
                        await asyncio.to_thread(wav_file.unlink)
            else:
                wav_bytes = await asyncio.to_thread(original_file.read_bytes)
        finally:
            if await asyncio.to_thread(original_file.exists):
                await asyncio.to_thread(original_file.unlink)

        return wav_bytes

    async def _transcribe_mimo_streaming(
        self,
        audio_data: bytes,
        filename: str = "audio.wav",
        language: str = "auto",
        is_float32: bool = False,
    ) -> AsyncIterator[str]:
        """Stream MiMo ASR results via the OpenAI-compatible streaming API.

        MiMo requires the full audio to be sent in a single request, but the
        text is returned incrementally when ``stream: true`` is set. This
        generator yields each incoming content delta.
        """
        if not self.base_url or not self.api_key:
            raise RuntimeError("MiMo ASR is not configured")

        if is_float32:
            wav_bytes = await asyncio.to_thread(self._float32_to_wav, audio_data)
        else:
            wav_bytes = await self._convert_audio_to_wav(audio_data, filename)
        if not wav_bytes:
            raise RuntimeError("Audio conversion produced empty WAV")

        audio_base64 = await asyncio.to_thread(base64.b64encode, wav_bytes)
        audio_base64_str = audio_base64.decode('ascii')
        payload = self._build_mimo_asr_payload(audio_base64_str, mime_type="audio/wav", language=language)
        payload["stream"] = True

        client = get_shared_async_client()
        async with client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers={"api-key": self.api_key},
            json=payload,
            timeout=float(self._asr_config.get("http_timeout_seconds", 300.0)),
        ) as response:
            response.raise_for_status()
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
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content

    async def _transcribe_mimo(self, audio_data: bytes, filename: str = "audio.wav", language: str = "auto") -> dict:
        """Non-streaming MiMo ASR: collect all streaming deltas and return the full text."""
        if not self.base_url or not self.api_key:
            raise RuntimeError("MiMo ASR is not configured")

        text_parts: list[str] = []
        async for chunk in self._transcribe_mimo_streaming(audio_data, filename, language):
            text_parts.append(chunk)

        return {
            "text": "".join(text_parts),
            "language": language if language != "auto" else None,
            "timestamps": [],
            "segments": [],
            "hotwords_used": [],
            "speaker_mode": "disabled",
            "duration": None,
        }

    @property
    def _is_funasr_model(self) -> bool:
        model = self.dashscope_model.lower()
        return model.startswith("fun-asr") or model.startswith("paraformer")

    async def transcribe(self, audio_data: bytes, filename: str = "audio.wav", custom_hotwords: Optional[list[dict]] = None) -> dict:
        if not self.enabled:
            raise RuntimeError("ASR service is not configured")

        if self.is_mimo:
            return await self._transcribe_mimo(audio_data, filename)

        detected_name, detected_type = self._detect_audio_type(audio_data)
        audio_dir = self.get_audio_files_dir()
        timestamp = int(time.time() * 1000)
        unique_id = uuid.uuid4().hex[:8]
        original_file = audio_dir / f"{timestamp}_{unique_id}_{detected_name}"
        await asyncio.to_thread(original_file.write_bytes, audio_data)

        need_conversion = detected_type in ("audio/webm", "audio/flac", "audio/mpeg", "audio/mp3")
        wav_file = None
        if need_conversion:
            wav_file = audio_dir / f"{timestamp}_{unique_id}_converted.wav"
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", str(original_file), "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", str(wav_file),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=int(self._asr_config.get("ffmpeg_timeout_seconds", 60))
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError("ffmpeg conversion timed out")
                if proc.returncode != 0:
                    raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode()}")
                send_file = wav_file
                send_name = "audio.wav"
                send_type = "audio/wav"
            except Exception:
                if await asyncio.to_thread(original_file.exists):
                    await asyncio.to_thread(original_file.unlink)
                if wav_file and await asyncio.to_thread(wav_file.exists):
                    await asyncio.to_thread(wav_file.unlink)
                raise
            finally:
                if await asyncio.to_thread(original_file.exists):
                    await asyncio.to_thread(original_file.unlink)
        else:
            send_file = original_file
            send_name = detected_name
            send_type = detected_type

        client = get_shared_async_client()
        file_bytes = await asyncio.to_thread(send_file.read_bytes)
        files = {"audio": (send_name, file_bytes, send_type)}
        data = {
            "return_timestamps": "true",
            "use_hotwords": "true",
        }
        if custom_hotwords:
            data["custom_hotwords"] = json.dumps(custom_hotwords, ensure_ascii=False)
        response = await client.post(
            f"{self.base_url}/transcribe",
            files=files,
            data=data,
            timeout=float(self._asr_config.get("http_timeout_seconds", 300.0)),
        )

        if need_conversion and await asyncio.to_thread(wav_file.exists):
            await asyncio.to_thread(wav_file.unlink)
        if await asyncio.to_thread(original_file.exists):
            await asyncio.to_thread(original_file.unlink)

        response.raise_for_status()
        result = response.json()
        if self.hotword_phonetic_correction and result.get("text"):
            result["text"] = apply_hotword_phonetic_correction(result["text"], custom_hotwords)
        return result

    async def proxy_websocket_stream(self, client_websocket: WebSocket) -> None:
        if not self.enabled:
            raise RuntimeError("ASR service is not configured")

        if not self.websocket_url:
            raise RuntimeError("ASR WebSocket service is not configured")

        async with websockets.connect(
            self.websocket_url,
            proxy=None,
            max_size=None,
            open_timeout=int(self._asr_config.get("ws_open_timeout", 10)),
            ping_interval=int(self._asr_config.get("ws_ping_interval", 20)),
            ping_timeout=int(self._asr_config.get("ws_ping_timeout", 20)),
            close_timeout=int(self._asr_config.get("ws_close_timeout", 5)),
        ) as upstream:
            client_task = asyncio.create_task(client_websocket.receive())
            upstream_task = asyncio.create_task(upstream.recv())

            try:
                while True:
                    done, pending = await asyncio.wait(
                        {client_task, upstream_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if client_task in done:
                        try:
                            message = client_task.result()
                        except WebSocketDisconnect:
                            return

                        message_type = message.get("type")
                        if message_type == "websocket.disconnect":
                            return

                        text = message.get("text")
                        data = message.get("bytes")

                        if text is not None:
                            await upstream.send(text)
                        elif data is not None:
                            await upstream.send(data)

                        client_task = asyncio.create_task(client_websocket.receive())

                    if upstream_task in done:
                        try:
                            message = upstream_task.result()
                        except ConnectionClosed:
                            return

                        if isinstance(message, bytes):
                            await client_websocket.send_bytes(message)
                        else:
                            await client_websocket.send_text(message)

                        upstream_task = asyncio.create_task(upstream.recv())
            finally:
                for task in (client_task, upstream_task):
                    task.cancel()

                await asyncio.gather(client_task, upstream_task, return_exceptions=True)

    async def proxy_mimo_websocket_stream(self, client_websocket: WebSocket, default_hotwords: Optional[list[dict]] = None) -> None:
        if not self.is_mimo:
            raise RuntimeError("MiMo ASR is not enabled")
        if not self.base_url or not self.api_key:
            raise RuntimeError("MiMo ASR is not configured")

        logger.info("Starting MiMo ASR WebSocket proxy")

        accumulated_float32 = bytearray()
        language = "auto"
        session_started = False
        finish_event = asyncio.Event()
        partial_task: Optional[asyncio.Task] = None
        last_partial_text = ""

        async def periodic_partial_asr() -> None:
            nonlocal last_partial_text
            min_bytes = int(16000 * self.mimo_partial_min_seconds * 4)
            while not finish_event.is_set():
                try:
                    await asyncio.wait_for(
                        finish_event.wait(),
                        timeout=self.mimo_partial_interval_seconds,
                    )
                    return
                except asyncio.TimeoutError:
                    pass

                if finish_event.is_set():
                    return

                snapshot = bytes(accumulated_float32)
                if len(snapshot) < min_bytes:
                    continue

                try:
                    text_parts: list[str] = []
                    async for chunk in self._transcribe_mimo_streaming(
                        snapshot, language=language, is_float32=True
                    ):
                        text_parts.append(chunk)
                    text = "".join(text_parts)
                    if self.hotword_phonetic_correction:
                        text = apply_hotword_phonetic_correction(text, default_hotwords)
                    if text and text != last_partial_text:
                        last_partial_text = text
                        await client_websocket.send_json({"event": "partial", "text": text})
                except Exception as e:
                    logger.warning("MiMo ASR partial transcription failed: %s", e)

        try:
            while True:
                try:
                    message = await client_websocket.receive()
                except WebSocketDisconnect:
                    return

                if message.get("type") == "websocket.disconnect":
                    return

                text = message.get("text")
                data = message.get("bytes")

                if text is not None:
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        continue

                    event = msg.get("event")
                    if event == "start":
                        language = msg.get("language", "auto")
                        session_started = True
                        await client_websocket.send_json({"event": "ready"})
                        logger.info("MiMo ASR session started, language=%s", language)
                        partial_task = asyncio.create_task(periodic_partial_asr())
                    elif event == "finish":
                        if not session_started:
                            await client_websocket.send_json({"event": "error", "error": "会话未开始"})
                            return

                        finish_event.set()
                        if partial_task is not None:
                            partial_task.cancel()
                            try:
                                await partial_task
                            except asyncio.CancelledError:
                                pass

                        float32_bytes = bytes(accumulated_float32)
                        logger.info("MiMo ASR finish requested, accumulated %d float32 bytes", len(float32_bytes))

                        try:
                            wav_bytes = await asyncio.to_thread(self._float32_to_wav, float32_bytes)
                            if not wav_bytes:
                                await client_websocket.send_json({"event": "error", "error": "没有收到音频数据"})
                                return

                            full_text = ""
                            async for chunk in self._transcribe_mimo_streaming(
                                wav_bytes, language=language, is_float32=True
                            ):
                                if chunk:
                                    full_text += chunk
                            final_text = full_text or last_partial_text
                            if self.hotword_phonetic_correction:
                                final_text = apply_hotword_phonetic_correction(final_text, default_hotwords)
                            await client_websocket.send_json({"event": "final", "text": final_text})
                            logger.info("MiMo ASR final text length=%d", len(final_text))
                        except Exception as e:
                            logger.exception("MiMo ASR transcription failed")
                            await client_websocket.send_json({"event": "error", "error": f"MiMo 语音识别失败: {str(e)}"})
                        return

                elif data is not None:
                    if not session_started:
                        await client_websocket.send_json({"event": "error", "error": "会话未开始"})
                        return
                    accumulated_float32.extend(data)

        except Exception as e:
            logger.exception("MiMo ASR WebSocket proxy error")
            try:
                await client_websocket.send_json({"event": "error", "error": f"MiMo ASR 服务错误: {str(e)}"})
            except Exception:
                pass
        finally:
            finish_event.set()
            if partial_task is not None:
                partial_task.cancel()
                try:
                    await partial_task
                except asyncio.CancelledError:
                    pass

    async def proxy_dashscope_websocket_stream(self, client_websocket: WebSocket, default_hotwords: Optional[list[dict]] = None) -> None:
        if not self.is_dashscope:
            raise RuntimeError("Dashscope ASR is not enabled")
        if not self.dashscope_api_key:
            raise RuntimeError("Dashscope API key is not configured")

        dashscope_ws_url = f"wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={self.dashscope_model}"
        headers = {
            "Authorization": f"Bearer {self.dashscope_api_key}",
        }

        logger.info(f"Connecting to Dashscope ASR, model={self.dashscope_model}")

        try:
            upstream = await websockets.connect(
                dashscope_ws_url,
                proxy=None,
                additional_headers=headers,
                max_size=None,
                open_timeout=int(self._asr_config.get("ws_open_timeout", 10)),
                ping_interval=int(self._asr_config.get("ws_ping_interval", 20)),
                ping_timeout=int(self._asr_config.get("ws_ping_timeout", 20)),
                close_timeout=int(self._asr_config.get("ws_close_timeout", 5)),
            )
        except Exception as e:
            logger.error(f"Failed to connect to Dashscope ASR: {e}")
            await client_websocket.send_json({"event": "error", "error": f"无法连接语音识别服务: {str(e)}"})
            await client_websocket.close(code=1011, reason="Dashscope connection failed")
            return

        try:
            segment_texts: list[str] = []
            finish_requested = False
            session_ready = asyncio.Event()
            start_received = False

            async def relay_from_client():
                nonlocal finish_requested, start_received
                while True:
                    try:
                        message = await client_websocket.receive()
                    except WebSocketDisconnect:
                        logger.info("Client WebSocket disconnected during relay")
                        return

                    if message.get("type") == "websocket.disconnect":
                        return

                    text = message.get("text")
                    data = message.get("bytes")

                    if text is not None:
                        try:
                            msg = json.loads(text)
                        except json.JSONDecodeError:
                            continue

                        event = msg.get("event")
                        if event == "start":
                            start_received = True
                            language = msg.get("language", "zh")
                            custom_hotwords = msg.get("custom_hotwords") or default_hotwords
                            logger.info("Client sent start, waiting for Dashscope session...")
                            await session_ready.wait()
                            logger.info("Dashscope session ready, sending session.update")
                            input_audio_transcription = {
                                "enabled": True,
                                "model": self.dashscope_model,
                                "language": language,
                            }
                            if custom_hotwords:
                                input_audio_transcription["custom_hotwords"] = custom_hotwords
                            session_update = {
                                "type": "session.update",
                                "session": {
                                    "modalities": ["text"],
                                    "input_audio_format": "pcm16",
                                    "turn_detection": None,
                                    "input_audio_transcription": input_audio_transcription,
                                },
                            }
                            await upstream.send(json.dumps(session_update))
                            logger.info("Sent session.update, waiting for session.updated before signaling ready")

                        elif event == "finish":
                            finish_requested = True
                            logger.info("Client requested finish, sending commit + session.finish")
                            await upstream.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await upstream.send(json.dumps({"type": "session.finish"}))

                    elif data is not None:
                        pcm16_b64 = await asyncio.to_thread(self._float32_to_pcm16_base64, data)
                        if pcm16_b64:
                            try:
                                await upstream.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "audio": pcm16_b64,
                                }))
                            except websockets.exceptions.ConnectionClosed as e:
                                logger.warning(f"Dashscope connection closed while sending audio: {e}")
                                return

            async def relay_from_dashscope():
                nonlocal finish_requested
                while True:
                    try:
                        raw = await upstream.recv()
                    except ConnectionClosed as e:
                        logger.warning(f"Dashscope connection closed: code={e.code}, reason={e.reason}")
                        try:
                            error_detail = e.reason or f"code={e.code}"
                            await client_websocket.send_json({
                                "event": "error",
                                "error": f"语音识别服务连接断开: {error_detail}",
                            })
                        except Exception:
                            pass
                        return

                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    event_type = msg.get("type", "")
                    logger.debug(f"Dashscope event: {event_type}")

                    if event_type == "session.created":
                        sid = msg.get("session", {}).get("id", "")
                        logger.info(f"Dashscope session created: {sid}")
                        session_ready.set()

                    elif event_type == "session.updated":
                        logger.info("Dashscope session updated successfully")
                        await client_websocket.send_json({"event": "ready"})
                        logger.info("Sent ready to client after session.updated confirmation")

                    elif event_type == "conversation.item.input_audio_transcription.text":
                        text_val = msg.get("text", "")
                        stash = msg.get("stash", "")
                        combined = text_val + stash
                        if combined:
                            if self.hotword_phonetic_correction:
                                combined = apply_hotword_phonetic_correction(combined, default_hotwords)
                            await client_websocket.send_json({
                                "event": "partial",
                                "text": combined,
                            })

                    elif event_type == "conversation.item.input_audio_transcription.completed":
                        transcript = msg.get("transcript", "")
                        if transcript:
                            segment_texts.append(transcript)
                            accumulated = "".join(segment_texts)
                            if self.hotword_phonetic_correction:
                                accumulated = apply_hotword_phonetic_correction(accumulated, default_hotwords)
                            await client_websocket.send_json({
                                "event": "segment",
                                "text": accumulated,
                            })

                    elif event_type == "session.finished":
                        final_text = "".join(segment_texts)
                        if self.hotword_phonetic_correction:
                            final_text = apply_hotword_phonetic_correction(final_text, default_hotwords)
                        logger.info(f"Session finished, final text: {final_text}")
                        await client_websocket.send_json({
                            "event": "final",
                            "text": final_text,
                        })
                        return

                    elif event_type == "error":
                        error_msg = msg.get("error", {})
                        if isinstance(error_msg, dict):
                            error_text = error_msg.get("message", str(error_msg))
                        else:
                            error_text = str(error_msg)
                        logger.error(f"Dashscope ASR error: {error_text}, raw: {json.dumps(msg)}")
                        await client_websocket.send_json({
                            "event": "error",
                            "error": error_text,
                        })
                        return

                    elif event_type in (
                        "input_audio_buffer.speech_started",
                        "input_audio_buffer.speech_stopped",
                        "input_audio_buffer.committed",
                        "session.closing",
                    ):
                        pass

            client_task = asyncio.create_task(relay_from_client())
            upstream_task = asyncio.create_task(relay_from_dashscope())

            try:
                done, pending = await asyncio.wait(
                    {client_task, upstream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                for task in (client_task, upstream_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(client_task, upstream_task, return_exceptions=True)

        except Exception as e:
            logger.error(f"Dashscope ASR proxy error: {e}")
            try:
                await client_websocket.send_json({
                    "event": "error",
                    "error": f"语音识别服务错误: {str(e)}",
                })
            except Exception:
                pass
        finally:
            try:
                await upstream.close()
            except Exception:
                pass

    def _format_funasr_hotwords(self, hotwords: Optional[list[dict]]) -> Optional[str]:
        """DashScope hot words are now managed via the Vocabulary REST API.
        This method is kept for backward compatibility with non-DashScope
        FunASR deployments that accept an inline hotwords string."""
        if not hotwords:
            return None
        valid = []
        for item in hotwords:
            text = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", "")
            weight = item.get("weight", 4) if isinstance(item, dict) else getattr(item, "weight", 4)
            if text and str(text).strip():
                valid.append((str(text).strip(), max(1, min(5, int(weight)))))
        if not valid:
            return None
        return json.dumps({text: weight for text, weight in valid}, ensure_ascii=False)

    async def proxy_funasr_websocket_stream(
        self,
        client_websocket: WebSocket,
        default_hotwords: Optional[list[dict]] = None,
        vocabulary_id: Optional[str] = None,
    ) -> None:
        if not self.is_dashscope:
            raise RuntimeError("Dashscope ASR is not enabled")
        if not self.dashscope_api_key:
            raise RuntimeError("Dashscope API key is not configured")

        task_id = uuid.uuid4().hex[:32]
        funasr_ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/"
        headers = {
            "Authorization": f"bearer {self.dashscope_api_key}",
        }

        logger.info(f"Connecting to Fun-ASR, model={self.dashscope_model}, task_id={task_id}")

        try:
            upstream = await websockets.connect(
                funasr_ws_url,
                proxy=None,
                additional_headers=headers,
                max_size=None,
                open_timeout=int(self._asr_config.get("ws_open_timeout", 10)),
                ping_interval=int(self._asr_config.get("ws_ping_interval", 20)),
                ping_timeout=int(self._asr_config.get("ws_ping_timeout", 20)),
                close_timeout=int(self._asr_config.get("ws_close_timeout", 5)),
            )
        except Exception as e:
            logger.error(f"Failed to connect to Fun-ASR: {e}")
            await client_websocket.send_json({"event": "error", "error": f"无法连接语音识别服务: {str(e)}"})
            await client_websocket.close(code=1011, reason="Fun-ASR connection failed")
            return

        try:
            finalized_segments: list[str] = []
            finish_requested = False
            task_started = asyncio.Event()

            async def relay_from_client():
                nonlocal finish_requested
                while True:
                    try:
                        message = await client_websocket.receive()
                    except WebSocketDisconnect:
                        return

                    if message.get("type") == "websocket.disconnect":
                        return

                    text = message.get("text")
                    data = message.get("bytes")

                    if text is not None:
                        try:
                            msg = json.loads(text)
                        except json.JSONDecodeError:
                            continue

                        event = msg.get("event")
                        if event == "start":
                            language = msg.get("language", "zh")
                            logger.info("Client sent start, sending run-task to Fun-ASR")

                            parameters: dict = {
                                "sample_rate": 16000,
                                "format": "pcm",
                            }

                            if language:
                                parameters["language_hints"] = [language]

                            if vocabulary_id:
                                parameters["vocabulary_id"] = vocabulary_id
                                logger.info("Fun-ASR vocabulary_id: %s", vocabulary_id)
                            else:
                                # Fallback: inline hotwords when no DashScope vocabulary is synced
                                custom_hotwords = msg.get("custom_hotwords") or default_hotwords
                                hotwords_str = self._format_funasr_hotwords(custom_hotwords)
                                if hotwords_str:
                                    parameters["hotwords"] = hotwords_str
                                    logger.info("Fun-ASR inline hotwords enabled, count=%d", len(custom_hotwords))

                            # Per-utterance hotwords from the start message are also passed even when
                            # a vocabulary_id is available, so callers can supply extra context.
                            custom_hotwords = msg.get("custom_hotwords")
                            if custom_hotwords:
                                hotwords_str = self._format_funasr_hotwords(custom_hotwords)
                                if hotwords_str:
                                    parameters["hotwords"] = hotwords_str
                                    logger.info("Fun-ASR per-utterance hotwords enabled, count=%d", len(custom_hotwords))

                            run_task_message = {
                                "header": {
                                    "action": "run-task",
                                    "task_id": task_id,
                                    "streaming": "duplex",
                                },
                                "payload": {
                                    "task_group": "audio",
                                    "task": "asr",
                                    "function": "recognition",
                                    "model": self.dashscope_model,
                                    "parameters": parameters,
                                    "input": {},
                                },
                            }
                            logger.info("Fun-ASR run-task parameters: %s", json.dumps(parameters, ensure_ascii=False))
                            await upstream.send(json.dumps(run_task_message))
                            logger.info("Sent run-task, waiting for task-started...")

                        elif event == "finish":
                            finish_requested = True
                            logger.info("Client requested finish, sending finish-task")
                            finish_task_message = {
                                "header": {
                                    "action": "finish-task",
                                    "task_id": task_id,
                                    "streaming": "duplex",
                                },
                                "payload": {
                                    "input": {},
                                },
                            }
                            await upstream.send(json.dumps(finish_task_message))

                    elif data is not None:
                        pcm16_bytes = await asyncio.to_thread(self._float32_to_pcm16, data)
                        if pcm16_bytes:
                            try:
                                await upstream.send(pcm16_bytes)
                            except websockets.exceptions.ConnectionClosed as e:
                                logger.warning(f"Fun-ASR connection closed while sending audio: {e}")
                                return

            async def relay_from_funasr():
                nonlocal finalized_segments
                while True:
                    try:
                        raw = await upstream.recv()
                    except ConnectionClosed as e:
                        logger.warning(f"Fun-ASR connection closed: code={e.code}, reason={e.reason}")
                        try:
                            error_detail = e.reason or f"code={e.code}"
                            await client_websocket.send_json({
                                "event": "error",
                                "error": f"语音识别服务连接断开: {error_detail}",
                            })
                        except Exception:
                            pass
                        return

                    if isinstance(raw, bytes):
                        continue

                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    header = msg.get("header", {})
                    event_type = header.get("event", "")
                    logger.debug(f"Fun-ASR event: {event_type}")

                    if event_type == "task-started":
                        logger.info(f"Fun-ASR task started: {task_id}")
                        task_started.set()
                        await client_websocket.send_json({"event": "ready"})
                        logger.info("Sent ready to client")

                    elif event_type == "result-generated":
                        payload = msg.get("payload", {})
                        output = payload.get("output", {})
                        sentence = output.get("sentence", {})
                        text_val = sentence.get("text", "")
                        sentence_end = sentence.get("sentence_end", False)

                        if text_val:
                            # VAD splits speech into sentences. To prevent the displayed
                            # transcription from disappearing during a pause, we always send
                            # the accumulated text recognized so far.
                            if sentence_end:
                                finalized_segments.append(text_val)
                                accumulated = "".join(finalized_segments)
                            else:
                                accumulated = "".join(finalized_segments) + text_val

                            if self.hotword_phonetic_correction:
                                accumulated = apply_hotword_phonetic_correction(accumulated, default_hotwords)

                            await client_websocket.send_json({
                                "event": "partial",
                                "text": accumulated,
                            })

                            if sentence_end:
                                await client_websocket.send_json({
                                    "event": "segment",
                                    "text": accumulated,
                                })

                    elif event_type == "task-finished":
                        final_text = "".join(finalized_segments)
                        if self.hotword_phonetic_correction:
                            final_text = apply_hotword_phonetic_correction(final_text, default_hotwords)
                        logger.info(f"Fun-ASR task finished, final text: {final_text}")
                        await client_websocket.send_json({
                            "event": "final",
                            "text": final_text,
                        })
                        return

                    elif event_type == "task-failed":
                        error_msg = header.get("error_message", header.get("message", "Fun-ASR task failed"))
                        logger.error(f"Fun-ASR task failed: {error_msg}")
                        await client_websocket.send_json({
                            "event": "error",
                            "error": str(error_msg),
                        })
                        return

                    elif event_type in (
                        "task-paused",
                        "task-resumed",
                        "task-cancelling",
                    ):
                        pass

            client_task = asyncio.create_task(relay_from_client())
            upstream_task = asyncio.create_task(relay_from_funasr())

            try:
                done, pending = await asyncio.wait(
                    {client_task, upstream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                for task in (client_task, upstream_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(client_task, upstream_task, return_exceptions=True)

        except Exception as e:
            logger.error(f"Fun-ASR proxy error: {e}")
            try:
                await client_websocket.send_json({
                    "event": "error",
                    "error": f"语音识别服务错误: {str(e)}",
                })
            except Exception:
                pass
        finally:
            try:
                await upstream.close()
            except Exception:
                pass
