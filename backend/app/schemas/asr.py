# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class HotwordItem(BaseModel):
    text: str
    weight: int
    lang: Optional[str] = None


class HotwordListResponse(BaseModel):
    hotwords: List[HotwordItem]


class HotwordListRequest(BaseModel):
    hotwords: List[HotwordItem]


class ASRRequest(BaseModel):
    language: Optional[str] = "auto"
    format: Optional[str] = "wav"
    return_timestamps: Optional[bool] = True
    use_hotwords: Optional[bool] = True
    custom_hotwords: Optional[List[HotwordItem]] = None
    context: Optional[str] = None


class SegmentInfo(BaseModel):
    speaker: str
    speaker_confidence: Optional[float] = None
    start_time: float
    end_time: float
    text: str


class TimestampInfo(BaseModel):
    start_time: float
    end_time: float
    text: str


class ASRResponse(BaseModel):
    text: str
    language: Optional[str] = None
    timestamps: Optional[List[TimestampInfo]] = []
    segments: Optional[List[SegmentInfo]] = []
    hotwords_used: Optional[List[str]] = []
    speaker_mode: Optional[str] = "disabled"
    duration: Optional[float] = None
