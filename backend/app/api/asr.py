# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import httpx
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, WebSocket, status, Form
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.services.asr_service import ASRService
from app.services.asr_vocabulary_service import ASRVocabularyService
from app.schemas.asr import ASRResponse, SegmentInfo, TimestampInfo, HotwordItem, HotwordListResponse, HotwordListRequest
from app.db.database import get_db, User, UserAsrHotword
from app.core.deps import get_current_user, get_current_user_from_websocket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/asr", tags=["asr"])

_service: Optional[ASRService] = None


def get_asr_service() -> ASRService:
    global _service
    if _service is None:
        _service = ASRService()
    return _service


async def send_ws_error(websocket: WebSocket, detail: str) -> None:
    await websocket.send_json({"event": "error", "error": detail})


@router.post("/transcribe", response_model=ASRResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    custom_hotwords: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user)
):
    if not file:
        raise HTTPException(status_code=400, detail="No audio file provided")

    allowed_types = ["audio/wav", "audio/wave", "audio/x-wav", "audio/webm", "audio/pcm", "audio/mp3", "audio/mpeg"]
    content_type = file.content_type or ""

    if content_type not in allowed_types and not content_type.startswith("audio/"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format: {content_type}. Supported: wav, webm, pcm, mp3"
        )

    service = get_asr_service()

    if not service.enabled:
        raise HTTPException(status_code=503, detail="ASR service is not configured")

    try:
        audio_data = await file.read()
        parsed_hotwords = None
        if custom_hotwords:
            try:
                parsed_hotwords = json.loads(custom_hotwords)
                if not isinstance(parsed_hotwords, list):
                    parsed_hotwords = None
            except json.JSONDecodeError:
                parsed_hotwords = None
        result = await service.transcribe(audio_data, file.filename or "audio.wav", custom_hotwords=parsed_hotwords)

        timestamps = []
        if result.get("timestamps"):
            for ts in result["timestamps"]:
                timestamps.append(TimestampInfo(
                    start_time=ts.get("start_time", 0),
                    end_time=ts.get("end_time", 0),
                    text=ts.get("text", "")
                ))

        segments = []
        if result.get("segments"):
            for seg in result["segments"]:
                segments.append(SegmentInfo(
                    speaker=seg.get("speaker", ""),
                    speaker_confidence=seg.get("speaker_confidence"),
                    start_time=seg.get("start_time", 0),
                    end_time=seg.get("end_time", 0),
                    text=seg.get("text", "")
                ))

        return ASRResponse(
            text=result.get("text", ""),
            language=result.get("language"),
            timestamps=timestamps,
            segments=segments,
            hotwords_used=result.get("hotwords_used", []),
            speaker_mode=result.get("speaker_mode", "disabled"),
            duration=result.get("duration")
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ASR service error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")


@router.get("/hotwords", response_model=HotwordListResponse)
async def get_asr_hotwords(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(UserAsrHotword).where(UserAsrHotword.user_id == current_user.id).order_by(UserAsrHotword.created_at)
    )
    items = result.scalars().all()
    return HotwordListResponse(hotwords=[
        HotwordItem(text=item.text, weight=item.weight, lang=item.lang)
        for item in items
    ])


@router.post("/hotwords", response_model=HotwordListResponse)
async def save_asr_hotwords(
    request: HotwordListRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # Delete existing hotwords for the user
    await db.execute(delete(UserAsrHotword).where(UserAsrHotword.user_id == current_user.id))

    # Insert new hotwords
    inserted = []
    for item in request.hotwords:
        if not item.text or not item.text.strip():
            continue
        db.add(UserAsrHotword(
            user_id=current_user.id,
            text=item.text.strip(),
            weight=max(1, min(5, item.weight)),
            lang=item.lang,
        ))
        inserted.append({"text": item.text.strip(), "weight": item.weight, "lang": item.lang})

    await db.commit()

    # Sync with DashScope vocabulary service (create/update/delete vocabulary list)
    vocab_service = ASRVocabularyService()
    try:
        await vocab_service.sync(current_user.id, inserted, db)
    except Exception as e:
        # Log but do not fail the save — local hotwords are still persisted
        logger.error("DashScope vocabulary sync failed: %s", e)

    result = await db.execute(
        select(UserAsrHotword).where(UserAsrHotword.user_id == current_user.id).order_by(UserAsrHotword.created_at)
    )
    items = result.scalars().all()
    return HotwordListResponse(hotwords=[
        HotwordItem(text=item.text, weight=item.weight, lang=item.lang)
        for item in items
    ])


@router.websocket("/ws/transcribe/stream")
async def transcribe_audio_stream(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    service = get_asr_service()

    try:
        current_user = await get_current_user_from_websocket(websocket, db)
    except HTTPException as exc:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason=str(exc.detail),
        )
        return

    await websocket.accept()

    # Load user's persisted hotwords and DashScope vocabulary_id
    hotword_result = await db.execute(
        select(UserAsrHotword).where(UserAsrHotword.user_id == current_user.id).order_by(UserAsrHotword.created_at)
    )
    hotword_items = hotword_result.scalars().all()
    user_hotwords = [
        {"text": item.text, "weight": item.weight, "lang": item.lang}
        for item in hotword_items
    ]

    # Read DashScope vocabulary_id from the first hotword row (all rows share the same id)
    vocabulary_id: Optional[str] = None
    for item in hotword_items:
        if item.dashscope_vocabulary_id:
            vocabulary_id = item.dashscope_vocabulary_id
            break

    if not service.enabled:
        await send_ws_error(websocket, "ASR service is not configured")
        await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
        return

    try:
        if service.is_mimo:
            await service.proxy_mimo_websocket_stream(websocket, default_hotwords=user_hotwords)
        elif service.is_dashscope:
            if service._is_funasr_model:
                await service.proxy_funasr_websocket_stream(
                    websocket,
                    default_hotwords=user_hotwords,
                    vocabulary_id=vocabulary_id,
                )
            else:
                await service.proxy_dashscope_websocket_stream(
                    websocket,
                    default_hotwords=user_hotwords,
                )
        else:
            await service.proxy_websocket_stream(websocket)
    except httpx.HTTPError as exc:
        await send_ws_error(websocket, f"ASR service error: {str(exc)}")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
    except Exception as exc:
        await send_ws_error(websocket, f"Transcription failed: {str(exc)}")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
