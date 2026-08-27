# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class AssistantBase(BaseModel):
    name: str
    system_prompt: str = ""
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    max_tokens: Optional[int] = None
    use_custom_model: bool = False
    custom_api_url: Optional[str] = None
    custom_api_key: Optional[str] = None
    custom_model_name: Optional[str] = None
    provider_type: str = "deepseek"
    extra_body: Optional[str] = None
    # Optional sub-agent / subtask override. When ``use_subtask_model`` is
    # true and the four ``subtask_custom_*`` fields are populated, the
    # tool-loop iterations and internal sub-agents (search decision,
    # keyword generation, sub-agent tasks) use this client instead of the
    # main one.
    use_subtask_model: bool = False
    subtask_custom_api_url: Optional[str] = None
    subtask_custom_api_key: Optional[str] = None
    subtask_custom_model_name: Optional[str] = None
    subtask_provider_type: Optional[str] = None
    subtask_extra_body: Optional[str] = None
    thinking_budget: Optional[int] = None
    # Qwen3.8(Local) provider: non-thinking sampling set + thinking-mode set
    # + preserve_thinking. All optional; None means use model-card defaults.
    min_p: Optional[float] = None
    repetition_penalty: Optional[float] = None
    thinking_temperature: Optional[float] = None
    thinking_top_p: Optional[float] = None
    thinking_top_k: Optional[int] = None
    thinking_min_p: Optional[float] = None
    thinking_presence_penalty: Optional[float] = None
    thinking_repetition_penalty: Optional[float] = None
    preserve_thinking: Optional[bool] = True


class AssistantCreate(AssistantBase):
    pass


class AssistantUpdate(BaseModel):
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    max_tokens: Optional[int] = None
    use_custom_model: Optional[bool] = None
    custom_api_url: Optional[str] = None
    custom_api_key: Optional[str] = None
    custom_model_name: Optional[str] = None
    provider_type: Optional[str] = None
    extra_body: Optional[str] = None
    use_subtask_model: Optional[bool] = None
    subtask_custom_api_url: Optional[str] = None
    subtask_custom_api_key: Optional[str] = None
    subtask_custom_model_name: Optional[str] = None
    subtask_provider_type: Optional[str] = None
    subtask_extra_body: Optional[str] = None
    thinking_budget: Optional[int] = None
    min_p: Optional[float] = None
    repetition_penalty: Optional[float] = None
    thinking_temperature: Optional[float] = None
    thinking_top_p: Optional[float] = None
    thinking_top_k: Optional[int] = None
    thinking_min_p: Optional[float] = None
    thinking_presence_penalty: Optional[float] = None
    thinking_repetition_penalty: Optional[float] = None
    preserve_thinking: Optional[bool] = None


class AssistantResponse(AssistantBase):
    id: str
    user_id: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True