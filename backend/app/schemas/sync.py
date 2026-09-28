# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class DeviceRegister(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    platform: Optional[str] = None


class DeviceResponse(BaseModel):
    id: str
    name: Optional[str] = None
    platform: Optional[str] = None
    last_seen_at: Optional[str] = None
    revoked: bool = False
    created_at: Optional[str] = None


class PushEvent(BaseModel):
    entity_type: str
    entity_id: str
    op: str
    payload: Optional[dict] = None


class PushRequest(BaseModel):
    device_id: str
    events: List[PushEvent] = Field(default_factory=list, max_length=500)


class PushResponse(BaseModel):
    applied: List[str]
    skipped: List[str]


class DeltaEvent(BaseModel):
    seq: int
    entity_type: str
    entity_id: str
    op: str
    payload: Optional[dict] = None
    origin_device: Optional[str] = None
    created_at: Optional[str] = None


class DeltaResponse(BaseModel):
    events: List[DeltaEvent]
    cursor: int
    has_more: bool


class BlobResponse(BaseModel):
    sha256: str
    size: int
    dedup: bool = False


class ResyncResponse(BaseModel):
    entities: Dict[str, List[dict]]
    cursor: int
