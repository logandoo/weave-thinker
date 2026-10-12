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
    # 项8（W7）+ 8a（W8，UPSTREAM_TODO_20261007）：保留沿与缺口信号——
    # `min_available_seq` = 该用户现存最小 seq（空=None）；`resync_required` =
    # cursor>0 且（越过保留沿 `cursor < min_available_seq-1`，或低于 per-user
    # 裁剪水位 `cursor < max_deleted_seq`——日志被全部裁剪时 min=None 仍可判）。
    # 零事件用户（无水位、无存活事件）不再误报（D-w8-1 风暴修复）；客户端收到
    # 信号应放弃增量、走全量 resync（「绝不静默返回不完整数据」）。
    min_available_seq: Optional[int] = None
    resync_required: bool = False


class BlobResponse(BaseModel):
    sha256: str
    size: int
    dedup: bool = False


class ResyncResponse(BaseModel):
    entities: Dict[str, List[dict]]
    # W8 8c（D-w8-5）：per-user 游标 = max(该用户事件 max，per-user 裁剪水位)——
    # 跨用户提交不污染；水位设底防 delta 恒提示重同步。读取次序=游标→墓碑→快照，
    # 轮末复查推进即重试（≤3；同用户并发 seq 倒挂的残留见 D-w8-5）。
    cursor: int
    # 项8b（W8，UPSTREAM_TODO_20261007）：删除墓碑回放——本用户墓碑列表
    # [{entity_type, entity_id, deleted_at}]（memory 域随开关门控）。客户端契约=
    # **先应用墓碑再并快照**（墓碑=历史删除事实、快照=当前状态；先删后并可让
    # 同 id 重建行以快照为准）；旧客户端忽略本字段（维持 merge-only 现状，
    # 不劣化）。补上墓碑后 full resync 才真正「可恢复删除」。
    tombstones: List[dict] = Field(default_factory=list)
