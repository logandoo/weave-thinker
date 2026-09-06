# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""agent 语音工具（2026-08-31，U2）：系统配置的 ASR / TTS 模型开放给 agent。

- asr_transcribe：工作区音频文件 → 文本（provider 分派：dashscope WS 一次性 /
  mimo 非流式 / general POST）
- tts_synthesize：文本 → PCM16 → WAV 落工作区（返回路径，agent 可再
  provide_file 生成下载卡）

端点一律来自 model_gateway 类型池（kind asr / tts）；未配置返回友好错误。
"""
import asyncio
import json
import logging
import os
import time
import wave
from pathlib import Path

from app.tools.registry import registry
from app.tools.workspace_read import _resolve_workspace_path

logger = logging.getLogger(__name__)

_AUDIO_EXTS = {".wav", ".mp3", ".webm", ".flac", ".m4a", ".ogg", ".pcm"}
_MAX_AUDIO_BYTES = 50 * 1024 * 1024
_MAX_TTS_CHARS = 5000
_TTS_SAMPLE_RATE = 24000  # MiMo tts pcm16 输出采样率（前端 useVoiceDuplex 同源）


def _get_asr_service():
    from app.services.asr_service import ASRService
    return ASRService()


def _get_tts_service():
    from app.services.tts_service import get_tts_service
    return get_tts_service()


def _err(msg: str, **extra) -> str:
    return json.dumps({"success": False, "error": msg, **extra}, ensure_ascii=False)


async def asr_transcribe(args: dict, **kwargs) -> str:
    audio_path = str(args.get("audio_path") or "").strip()
    language = str(args.get("language") or "auto").strip() or "auto"
    if not audio_path:
        return _err("audio_path 不能为空")
    workspace_path = kwargs.get("workspace_path") or ""
    if not workspace_path:
        return _err("无工作区上下文，无法解析音频路径")
    resolved = _resolve_workspace_path(audio_path, workspace_path)
    if not resolved:
        return _err(f"音频文件不存在或不在工作区内（路径越界会被拒绝）: {audio_path}")
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in _AUDIO_EXTS:
        return _err(f"不支持的音频类型 {ext}（支持: {', '.join(sorted(_AUDIO_EXTS))}）")
    try:
        size = os.path.getsize(resolved)
        if size > _MAX_AUDIO_BYTES:
            return _err(f"音频过大（{size} 字节 > {_MAX_AUDIO_BYTES}）")
        data = await asyncio_read(resolved)
    except Exception as exc:
        return _err(f"读取音频失败: {exc}")
    svc = _get_asr_service()
    if not svc.enabled:
        return _err(
            "语音识别（asr）未配置：请在模型配置的 [endpoints.asr] 填入 "
            "provider/base_url/api_key/model_name 后重试。",
            configured=False,
        )
    try:
        result = await svc.transcribe_file(data, filename=os.path.basename(resolved), language=language)
    except Exception as exc:
        logger.warning("asr_transcribe failed: %s", exc)
        return _err(f"语音识别调用失败: {exc}")
    text = str((result or {}).get("text", "") or "").strip()
    return json.dumps({
        "success": True,
        "text": text,
        "language": (result or {}).get("language"),
        "segments": len((result or {}).get("segments") or []),
        "audio_path": os.path.relpath(resolved, str(Path(workspace_path).resolve())),
    }, ensure_ascii=False)


async def asyncio_read(path: str) -> bytes:
    import asyncio
    return await asyncio.to_thread(Path(path).read_bytes)


async def tts_synthesize(args: dict, **kwargs) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return _err("text 不能为空")
    if len(text) > _MAX_TTS_CHARS:
        return _err(f"text 过长（{len(text)} 字 > {_MAX_TTS_CHARS}，请分段合成）")
    voice = str(args.get("voice") or "").strip() or None
    style = str(args.get("style") or "").strip() or None
    workspace_path = kwargs.get("workspace_path") or ""
    if not workspace_path:
        return _err("无工作区上下文，无法写入音频文件")
    svc = _get_tts_service()
    if not svc.is_configured():
        return _err(
            "语音合成（tts）未配置：请在模型配置的 [endpoints.tts] 填入 "
            "base_url/api_key/model_name 后重试。",
            configured=False,
        )
    out_dir = Path(workspace_path) / "tts"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        out_file = out_dir / f"tts_{ts}.wav"
        pcm_buf = bytearray()
        async for pcm in svc.stream_tts(text, style_instruction=style, voice=voice):
            pcm_buf.extend(pcm)
        if not pcm_buf:
            return _err("语音合成返回空音频")
        with wave.open(str(out_file), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_TTS_SAMPLE_RATE)
            wf.writeframes(bytes(pcm_buf))
    except Exception as exc:
        logger.warning("tts_synthesize failed: %s", exc)
        return _err(f"语音合成调用失败: {exc}")
    rel = os.path.relpath(out_file, str(Path(workspace_path).resolve()))
    return json.dumps({
        "success": True,
        "path": rel,
        "audio_bytes": len(pcm_buf),
        "voice": voice or svc.voice,
        "model": svc.model,
        "hint": "音频已写入工作区；需要提供给用户下载时调用 provide_file。",
    }, ensure_ascii=False)


registry.register(
    name="asr_transcribe",
    toolset="core",
    schema={
        "name": "asr_transcribe",
        "description": (
            "用系统配置的语音识别模型（ASR）把音频转成文字。适用：用户上传的录音/"
            "语音消息（[file-ref] 标记给出路径）、workspace 里的音频文件需要提取"
            "文字的场合。\n传入工作区相对路径，返回识别文本。"
            "返回「未配置」时如实告知用户，不要编造转写内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "audio_path": {"type": "string", "description": "工作区内的音频路径"},
                "language": {"type": "string", "description": "可选：语言提示（默认 auto）"},
            },
            "required": ["audio_path"],
        },
    },
    handler=asr_transcribe,
    is_async=True,
    description="Transcribe workspace audio files via the configured ASR model",
    emoji="",
)

registry.register(
    name="tts_synthesize",
    toolset="core",
    schema={
        "name": "tts_synthesize",
        "description": (
            "用系统配置的语音合成模型（TTS）把文字变成语音文件（WAV，写入工作区）。"
            "适用：用户明确要求朗读/生成语音版内容、制作有声材料。\n"
            "返回音频文件路径；需要给用户下载时再用 provide_file 生成下载卡。"
            "返回「未配置」时如实告知用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要合成的文本（≤5000 字，长文本请分段）"},
                "voice": {"type": "string", "description": "可选：临时指定音色（默认用系统配置音色）"},
                "style": {"type": "string", "description": "可选：风格指令（如「用开心的语气」）"},
            },
            "required": ["text"],
        },
    },
    handler=tts_synthesize,
    is_async=True,
    description="Synthesize text to a WAV file via the configured TTS model",
    emoji="",
)
