# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict, Any


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    assistant_id: Optional[str] = None
    messages: List[Message]
    enable_web_search: bool = False
    regenerate_from_message_id: Optional[str] = None
    edit_message_id: Optional[str] = None
    force_search_results: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    max_tokens: Optional[int] = None
    enable_reasoning: bool = False
    reasoning_effort: Optional[str] = None
    thinking_budget: Optional[int] = None
    deathmatch_mode: bool = False
    deathmatch_action: Optional[str] = None  # "start", "stop", "pause", "resume"


class ConversationCreate(BaseModel):
    title: Optional[str] = None
    assistant_id: Optional[str] = None


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    group_id: Optional[str] = None


class ConversationResponse(BaseModel):
    id: str
    title: str
    group_id: Optional[str] = None
    assistant_id: Optional[str] = None
    sort_order: int = 0
    created_at: str
    updated_at: str
    last_user_message_at: Optional[str] = None
    deathmatch_mode: bool = False
    deathmatch_status: str = "inactive"
    deathmatch_reason: Optional[str] = None
    deathmatch_goal: Optional[str] = None
    deathmatch_turns: int = 0
    deathmatch_max_turns: int = 30
    deathmatch_grilling_total: int = 0
    deathmatch_grilling_completed: int = 0
    deathmatch_grilling_round: int = 0
    deathmatch_grilling_round_total: int = 3
    deathmatch_context_summary: Optional[str] = None
    deathmatch_expected_marker: Optional[str] = None
    deathmatch_marker_miss_count: int = 0
    deathmatch_compressed_context: Optional[str] = None
    deathmatch_plan: Optional[Dict[str, Any]] = None
    deathmatch_plan_version: int = 0


class ConversationGroupCreate(BaseModel):
    name: str
    color: Optional[str] = None
    assistant_id: Optional[str] = None


class ConversationGroupUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class ConversationGroupResponse(BaseModel):
    id: str
    name: str
    color: str
    assistant_id: Optional[str] = None
    sort_order: int
    created_at: str
    updated_at: str
    conversation_count: int = 0


class ConversationMoveRequest(BaseModel):
    group_id: Optional[str] = None
    # 跨助手移动：目标助手 id（属于当前用户）。与 group_id 同时提供时，
    # 分组必须属于目标助手，否则 400。
    assistant_id: Optional[str] = None


class GroupMoveRequest(BaseModel):
    assistant_id: str


class ConversationReorderItem(BaseModel):
    id: str
    sort_order: int
    group_id: Optional[str] = None


class ConversationReorderRequest(BaseModel):
    items: List[ConversationReorderItem]


class ConversationGroupReorderItem(BaseModel):
    id: str
    sort_order: int


class ConversationGroupReorderRequest(BaseModel):
    items: List[ConversationGroupReorderItem]


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    reasoning_content: Optional[str] = None
    tool_calls: Optional[str] = None
    tool_results: Optional[str] = None
    created_at: str


class ConversationWithMessages(ConversationResponse):
    messages: List[MessageResponse] = []


class MatchedMessage(BaseModel):
    id: str
    role: str
    content_snippet: str


class ConversationSearchResult(BaseModel):
    conversation_id: str
    title: str
    updated_at: str
    matched_messages: List[MatchedMessage] = []


class UserCreate(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: str
    username: str
    created_at: str
    agent_permissions: Optional[Dict[str, Any]] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse
