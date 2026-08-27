# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.db.database import get_db, Assistant, Conversation, User
from app.schemas.assistant import (
    AssistantCreate,
    AssistantUpdate,
    AssistantResponse
)
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/assistants", tags=["assistants"])


def _serialize(a: Assistant) -> AssistantResponse:
    return AssistantResponse(
        id=a.id,
        user_id=a.user_id,
        name=a.name,
        system_prompt=a.system_prompt,
        temperature=a.temperature,
        top_p=a.top_p,
        top_k=a.top_k,
        presence_penalty=a.presence_penalty,
        frequency_penalty=a.frequency_penalty,
        max_tokens=a.max_tokens,
        use_custom_model=a.use_custom_model,
        custom_api_url=a.custom_api_url,
        custom_api_key=a.custom_api_key,
        custom_model_name=a.custom_model_name,
        provider_type=a.provider_type or "deepseek",
        extra_body=a.extra_body,
        use_subtask_model=bool(getattr(a, "use_subtask_model", False)),
        subtask_custom_api_url=getattr(a, "subtask_custom_api_url", None),
        subtask_custom_api_key=getattr(a, "subtask_custom_api_key", None),
        subtask_custom_model_name=getattr(a, "subtask_custom_model_name", None),
        subtask_provider_type=getattr(a, "subtask_provider_type", None),
        subtask_extra_body=getattr(a, "subtask_extra_body", None),
        thinking_budget=getattr(a, "thinking_budget", None),
        min_p=getattr(a, "min_p", None),
        repetition_penalty=getattr(a, "repetition_penalty", None),
        thinking_temperature=getattr(a, "thinking_temperature", None),
        thinking_top_p=getattr(a, "thinking_top_p", None),
        thinking_top_k=getattr(a, "thinking_top_k", None),
        thinking_min_p=getattr(a, "thinking_min_p", None),
        thinking_presence_penalty=getattr(a, "thinking_presence_penalty", None),
        thinking_repetition_penalty=getattr(a, "thinking_repetition_penalty", None),
        preserve_thinking=getattr(a, "preserve_thinking", True),
        created_at=a.created_at.isoformat(),
        updated_at=a.updated_at.isoformat(),
    )


@router.get("", response_model=list[AssistantResponse])
async def list_assistants(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Assistant).where(Assistant.user_id == current_user.id).order_by(desc(Assistant.updated_at))
    )
    assistants = result.scalars().all()
    return [_serialize(a) for a in assistants]


@router.post("", response_model=AssistantResponse)
async def create_assistant(
    assistant_data: AssistantCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    assistant = Assistant(
        user_id=current_user.id,
        name=assistant_data.name,
        system_prompt=assistant_data.system_prompt,
        temperature=assistant_data.temperature,
        top_p=assistant_data.top_p,
        top_k=assistant_data.top_k,
        presence_penalty=assistant_data.presence_penalty,
        frequency_penalty=assistant_data.frequency_penalty,
        max_tokens=assistant_data.max_tokens,
        use_custom_model=assistant_data.use_custom_model,
        custom_api_url=assistant_data.custom_api_url,
        custom_api_key=assistant_data.custom_api_key,
        custom_model_name=assistant_data.custom_model_name,
        provider_type=assistant_data.provider_type,
        extra_body=assistant_data.extra_body,
        use_subtask_model=assistant_data.use_subtask_model,
        subtask_custom_api_url=assistant_data.subtask_custom_api_url,
        subtask_custom_api_key=assistant_data.subtask_custom_api_key,
        subtask_custom_model_name=assistant_data.subtask_custom_model_name,
        subtask_provider_type=assistant_data.subtask_provider_type,
        subtask_extra_body=assistant_data.subtask_extra_body,
        thinking_budget=assistant_data.thinking_budget,
        min_p=assistant_data.min_p,
        repetition_penalty=assistant_data.repetition_penalty,
        thinking_temperature=assistant_data.thinking_temperature,
        thinking_top_p=assistant_data.thinking_top_p,
        thinking_top_k=assistant_data.thinking_top_k,
        thinking_min_p=assistant_data.thinking_min_p,
        thinking_presence_penalty=assistant_data.thinking_presence_penalty,
        thinking_repetition_penalty=assistant_data.thinking_repetition_penalty,
        preserve_thinking=assistant_data.preserve_thinking,
    )
    db.add(assistant)
    await db.commit()
    await db.refresh(assistant)
    return _serialize(assistant)


@router.get("/{assistant_id}", response_model=AssistantResponse)
async def get_assistant(
    assistant_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Assistant).where(
            Assistant.id == assistant_id,
            Assistant.user_id == current_user.id
        )
    )
    assistant = result.scalar_one_or_none()
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return _serialize(assistant)


@router.put("/{assistant_id}", response_model=AssistantResponse)
async def update_assistant(
    assistant_id: str,
    assistant_data: AssistantUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Assistant).where(
            Assistant.id == assistant_id,
            Assistant.user_id == current_user.id
        )
    )
    assistant = result.scalar_one_or_none()
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    update_data = assistant_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(assistant, field, value)

    await db.commit()
    await db.refresh(assistant)
    return _serialize(assistant)


@router.delete("/{assistant_id}")
async def delete_assistant(
    assistant_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Assistant).where(
            Assistant.id == assistant_id,
            Assistant.user_id == current_user.id
        )
    )
    assistant = result.scalar_one_or_none()
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    await db.delete(assistant)
    await db.commit()
    return {"status": "ok"}


@router.get("/{assistant_id}/conversations")
async def get_assistant_conversations(
    assistant_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Assistant).where(
            Assistant.id == assistant_id,
            Assistant.user_id == current_user.id
        )
    )
    assistant = result.scalar_one_or_none()
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.assistant_id == assistant_id,
            Conversation.user_id == current_user.id
        ).order_by(desc(Conversation.updated_at))
    )
    conversations = conv_result.scalars().all()

    return [
        {
            "id": c.id,
            "title": c.title,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat()
        }
        for c in conversations
    ]