# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Typed response models for the memory API (J8: OpenAPI must describe fields)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MemoryEvidence(BaseModel):
    source_ids: list[str] = Field(default_factory=list)
    unit_ids: list[str] = Field(default_factory=list)
    source_kind: str | None = None
    quote: str = ""
    reason: str = ""
    checked_at: str | None = None
    manual: bool | None = None


class MemoryConceptItem(BaseModel):
    id: str
    canonical_name: str
    description_short: str
    description_full: str | None = None
    weight: float
    importance: float
    source_trust: str
    memory_type: str
    activation_strength: float
    status: str
    valid_from: str | None = None
    valid_to: str | None = None
    created_at: str | None = None
    verification_state: str | None = None
    last_verified_at: str | None = None
    invalid_reason: str | None = None
    superseded_by: str | None = None
    superseded_by_name: str | None = None
    needs_review: bool = False
    aliases: list[str] = Field(default_factory=list)
    recurrence_count: int = 0
    source_type: str | None = None
    source_kind: str | None = None
    source_message_ids: list[str] = Field(default_factory=list)
    evidence: MemoryEvidence | None = None
    verification_correction: str | None = None
    is_history: bool = False


class MemoryConceptList(BaseModel):
    concepts: list[MemoryConceptItem]
    count: int
    total: int
    # 评审修复（2026-11-08）：分段 total —— offset 偏移现行段、history_offset 偏移
    # 历史段，前端按各自已加载条数推进游标（共享游标会翻页漏行）。
    active_total: int = 0
    history_total: int = 0
    include_history: bool


class MemoryVerifyRequest(BaseModel):
    action: Literal["confirm", "reject", "retry", "apply_correction"]
    note: str = ""


class MemoryVerifyResponse(BaseModel):
    id: str
    action: str
    verification_state: str | None = None
    description_short: str | None = None
    description_full: str | None = None
    applied: bool = False
    audit_id: str | None = None


class MemoryPageLinkItem(BaseModel):
    target_page_id: str
    target_title: str | None = None
    relation_type: str
    weight: float


class MemoryPageItem(BaseModel):
    id: str
    entity_key: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    summary: str | None = None
    concept_ids: list[str] = Field(default_factory=list)
    importance: float = 0.0
    last_built_at: str | None = None
    links: list[MemoryPageLinkItem] = Field(default_factory=list)


class MemoryPageList(BaseModel):
    pages: list[MemoryPageItem]
    count: int


class MemoryHealth(BaseModel):
    total: int = 0
    active: int = 0
    merged: int = 0
    retracted: int = 0
    source_lost: int = 0
    forgotten: int = 0
    expired: int = 0
    needs_review: int = 0
    verified: int = 0
    contradicted: int = 0
    insufficient: int = 0
    verify_coverage: float = 0.0
    dream_injectable_rate: float | None = None
    last_verify: dict[str, Any] | None = None
    recall_7d_avg_injected_chars: float | None = None
    # P1-15（2026-11-08）：无法归属用户的 legacy 记账行数（全局卫生计数，不含内容）。
    eval_runs_legacy: int = 0
    # G5/P1-11（2026-11-08）：健康告警（阈值 [memory.alerts]；level ok|warn|crit）。
    alerts: list[dict[str, Any]] | None = None
