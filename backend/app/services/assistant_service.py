# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.database import Assistant
from app.core.config import get_config

config = get_config()


async def create_default_assistant_if_needed(db: AsyncSession, user_id: str) -> Assistant:
    result = await db.execute(select(Assistant).where(Assistant.user_id == user_id))
    existing = result.scalars().first()
    if existing:
        return existing

    assistant = Assistant(
        user_id=user_id,
        name=config.default_assistant_name,
        system_prompt=config.default_assistant_system_prompt,
        temperature=config.default_assistant_temperature,
        top_p=config.default_assistant_top_p,
        top_k=config.default_assistant_top_k,
        presence_penalty=config.default_assistant_presence_penalty,
        frequency_penalty=config.default_assistant_frequency_penalty,
        max_tokens=config.default_assistant_max_tokens
    )
    db.add(assistant)
    await db.commit()
    await db.refresh(assistant)
    return assistant


VOICE_ASSISTANT_NAME = "语音助理"
LEGACY_VOICE_ASSISTANT_NAME = "酬"


async def ensure_voice_assistant(db: AsyncSession, user_id: str) -> Assistant:
    """Find or create the dedicated 语音助理 (voice) assistant.

    Voice-mode conversation transcripts are persisted as Conversation rows
    belonging to this assistant, so they appear under a 语音助理 entry in
    Agent (chat) mode and can be selected from the voice session picker.

    Rows created before the rename still carry the legacy name 酬; rename
    them on the fly so old installs converge without manual migration.
    """
    result = await db.execute(
        select(Assistant).where(
            Assistant.user_id == user_id,
            Assistant.name.in_([VOICE_ASSISTANT_NAME, LEGACY_VOICE_ASSISTANT_NAME]),
        )
    )
    existing = result.scalars().first()
    if existing:
        if existing.name != VOICE_ASSISTANT_NAME:
            existing.name = VOICE_ASSISTANT_NAME
            await db.commit()
            await db.refresh(existing)
        return existing

    assistant = Assistant(
        user_id=user_id,
        name=VOICE_ASSISTANT_NAME,
        system_prompt="",
    )
    db.add(assistant)
    await db.commit()
    await db.refresh(assistant)
    return assistant