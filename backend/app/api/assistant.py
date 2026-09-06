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


def _default_alias() -> str:
    from app.model_gateway.registry import get_model_registry
    try:
        return get_model_registry().default_alias()
    except Exception:
        return "deepseek"


def _serialize(a: Assistant) -> AssistantResponse:
    # 模型配置解耦（2026-08-30）：响应只含逻辑别名，绝不回传
    # provider_type / custom_* / 采样参数（含历史遗留的 api_key 明文问题一并根治）。
    # legacy NULL 行如实报告 ""（不冒名默认别名，A4.9 复审 Important-4）。
    return AssistantResponse(
        id=a.id,
        user_id=a.user_id,
        name=a.name,
        system_prompt=a.system_prompt,
        model_alias=getattr(a, "model_alias", None) or "",
        subtask_model_alias=getattr(a, "subtask_model_alias", None) or "",
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
        model_alias=assistant_data.model_alias or None,
        subtask_model_alias=assistant_data.subtask_model_alias or None,
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
        if field in ("model_alias", "subtask_model_alias") and value == "":
            value = None  # "" = 清除别名（存 NULL，回退 legacy/默认解析）
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