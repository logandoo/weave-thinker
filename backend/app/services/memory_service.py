# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import List

from sqlalchemy import desc, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import AgentMemory, Conversation, Message, Note, Notebook, UserAgentState
from app.services.memory_llm_factory import _memory_llm

config = get_config()
logger = logging.getLogger(__name__)
from app.prompts.memory import (
    MEMORY_DREAM_SYSTEM_PROMPT,
    MEMORY_SUMMARY_SYSTEM_PROMPT,
)


_memory_gen_in_flight: set[str] = set()


@dataclass
class AgentSharedContext:
    agent_state: UserAgentState
    memory_summary: str
    dream_summary: str
    memory_entries: List[AgentMemory]


def _trim_text(value: str, limit: int = 1200) -> str:
    normalized = (value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _format_notes_payload(notes: List[Note]) -> str:
    chunks = []
    for note in notes:
        chunks.append(
            f"标题: {note.title or '无标题'}\n"
            f"内容: {_trim_text(note.content or '', 300)}"
        )
    return "\n\n".join(chunks)


def _format_messages_payload(messages: List[Message]) -> str:
    return "\n".join(f"- {_trim_text(message.content or '', 220)}" for message in messages)


def _build_fallback_memory_summary(notes: List[Note], messages: List[Message]) -> str:
    note_titles = [note.title or "无标题笔记" for note in notes[:5]]
    question_samples = [message.content.strip() for message in messages[:5] if message.content.strip()]

    sections = ["用户近期共享记忆："]
    if note_titles:
        sections.append("- 最近笔记主题：" + "、".join(note_titles))
    if question_samples:
        sections.append("- 最近高频问题：" + "；".join(_trim_text(sample, 40) for sample in question_samples))
    if len(sections) == 1:
        sections.append("- 暂无足够数据，先按当前会话上下文协作。")
    return "\n".join(sections)


def _build_fallback_dream_summary(notes: List[Note], messages: List[Message]) -> str:
    note_count = len(notes)
    message_count = len(messages)
    return (
        "近期 dream 摘要："
        f"用户最近整理了 {note_count} 条笔记线索，并提出了 {message_count} 个问题片段。"
        "优先延续这些主题的上下文。"
    )


async def ensure_user_agent_state(db: AsyncSession, user_id: str) -> UserAgentState:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    result = await db.execute(select(UserAgentState).where(UserAgentState.user_id == user_id))
    state = result.scalar_one_or_none()
    if state is not None:
        return state

    stmt = (
        pg_insert(UserAgentState)
        .values(user_id=user_id, agent_name=config.agent_name)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    await db.execute(stmt)
    await db.commit()

    result = await db.execute(select(UserAgentState).where(UserAgentState.user_id == user_id))
    state = result.scalar_one_or_none()
    if state is None:
        raise RuntimeError(f"Failed to create UserAgentState for user {user_id}")
    return state


async def _load_note_context(db: AsyncSession, user_id: str, limit: int) -> List[Note]:
    result = await db.execute(
        select(Note)
        .join(Notebook, Note.notebook_id == Notebook.id)
        .where(Notebook.user_id == user_id)
        .order_by(desc(Note.updated_at))
        .limit(limit)
    )
    rows = list(result.scalars().all())
    # 任务作用域（2026-11-10 §8.5）：任务关联笔记按当时策略过滤；
    # 未关联任何 Task 的用户笔记仍是独立的用户级来源。
    try:
        from app.services.workspace_ingest_filter import note_policies

        policies = await note_policies(db, user_id, [r.id for r in rows])
        if policies:
            from app.services.workspace_ingest_filter import policy_allows_note

            rows = [r for r in rows
                    if policy_allows_note(*(policies.get(r.id) or (None, None)))]
    except Exception:
        logger.exception("memory ingestion note-policy filter failed (fail open to legacy)")
    return rows


async def _load_user_message_context(db: AsyncSession, user_id: str, limit: int) -> List[Message]:
    result = await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.user_id == user_id, Message.role == "user")
        .order_by(desc(Message.created_at))
        .limit(limit)
    )
    rows = list(result.scalars().all())
    # 任务作用域（2026-11-10 §8.5）：exclude 的 Task 消息不进系统记忆；
    # user_facts 只保留结构清理后的 user-authored 文本（不带助手回答/工具卡片）。
    try:
        from app.services.workspace_ingest_filter import filter_user_messages

        rows = await filter_user_messages(db, rows)
    except Exception:
        logger.exception("memory ingestion message filter failed (fail open to legacy)")
    return rows


async def _generate_summary(prompt_messages: list, fallback: str) -> str:
    llm_service = _memory_llm("concept_extraction")
    mem_cfg = config.agent_memory
    try:
        response = await llm_service.complete_chat(
            prompt_messages,
            temperature=float(mem_cfg.get("summary_temperature", 0.2)),
            max_tokens=mem_cfg.get("summary_max_tokens") or None,
        )
    except Exception:
        logger.exception("Failed to generate agent memory summary via LLM")
        return fallback

    cleaned = (response or "").strip()
    return cleaned or fallback


def _sanitize_generated_memory(text: str) -> str:
    """Facts survive; self-propagating/injection instructions are removed.

    2026-10-18 (Mind Viruses): the daily summary/dream is injected into the
    system prompt, so it is a cross-session propagation surface. Scan at the
    write boundary, not only when the user later reads it.
    """
    try:
        from app.services.memory_security import sanitize_untrusted_text
        cleaned, reasons = sanitize_untrusted_text(text)
        if reasons:
            logger.warning("memory summary sanitized: %s", "; ".join(sorted(set(reasons))[:4]))
            return cleaned.strip() or "[已隔离指令]"
        return cleaned
    except Exception:
        logger.debug("memory summary sanitize failed (fail-open)", exc_info=True)
        return text


async def generate_user_agent_memory(db: AsyncSession, user_id: str, *, force: bool = False) -> UserAgentState:
    state = await ensure_user_agent_state(db, user_id)
    today = date.today()
    already_generated_today = (
        state.last_memory_generated_at is not None
        and state.last_dream_generated_at is not None
        and state.last_memory_generated_at.date() >= today
        and state.last_dream_generated_at.date() >= today
        and state.memory_summary
        and state.dream_summary
    )
    if already_generated_today and not force:
        return state

    notes = await _load_note_context(db, user_id, config.agent_memory_refresh_note_limit)
    user_messages = await _load_user_message_context(db, user_id, config.agent_memory_refresh_message_limit)

    notes_payload = _format_notes_payload(notes)
    messages_payload = _format_messages_payload(user_messages)
    fallback_memory = _build_fallback_memory_summary(notes, user_messages)
    fallback_dream = _build_fallback_dream_summary(notes, user_messages)

    memory_summary = await _generate_summary(
        [
            {
                "role": "system",
                "content": MEMORY_SUMMARY_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    f"最近笔记：\n{notes_payload or '无'}\n\n"
                    f"最近问题：\n{messages_payload or '无'}\n\n"
                    "请生成可直接注入系统提示的长期记忆摘要。"
                ),
            },
        ],
        fallback_memory,
    )
    dream_summary = await _generate_summary(
        [
            {
                "role": "system",
                "content": MEMORY_DREAM_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    f"最近笔记：\n{notes_payload or '无'}\n\n"
                    f"最近问题：\n{messages_payload or '无'}\n\n"
                    "请生成一段 4-6 句的中文摘要。"
                ),
            },
        ],
        fallback_dream,
    )

    now = datetime.utcnow()
    today_key = today.isoformat()

    state.memory_summary = _trim_text(
        _sanitize_generated_memory(memory_summary),
        int(config.agent_memory.get("summary_storage_limit", 4000)),
    )
    state.dream_summary = _trim_text(
        _sanitize_generated_memory(dream_summary),
        int(config.agent_memory.get("dream_storage_limit", 4000)),
    )
    state.last_memory_generated_at = now
    state.last_dream_generated_at = now

    # 2026-11-08（dreaming 收敛）：v1 nightly dream 写入已删除。
    # v2 的 consolidation dream 是唯一的 agent_dreams writer；历史 nightly/legacy
    # 行保留但不再新增，API/UI 默认不展示。

    memory_result = await db.execute(
        select(AgentMemory).where(
            AgentMemory.agent_state_id == state.id,
            AgentMemory.source_type == "daily-summary",
            AgentMemory.source_id == today_key,
        )
    )
    memory_entry = memory_result.scalar_one_or_none()
    _default_importance = float(config.agent_memory.get("default_importance", 0.85))
    if memory_entry is None:
        memory_entry = AgentMemory(
            agent_state_id=state.id,
            source_type="daily-summary",
            source_id=today_key,
            title=f"Daily memory {today_key}",
            content=state.memory_summary,
            importance=_default_importance,
        )
        db.add(memory_entry)
    else:
        memory_entry.content = state.memory_summary
        memory_entry.importance = _default_importance

    await db.commit()
    await db.refresh(state)
    return state


async def build_shared_agent_context(db: AsyncSession, user_id: str) -> AgentSharedContext:
    # W3-1（记忆/长程 v2，2026-10-23）双轨收敛：v2 运行时下 v1 共享上下文直接
    # 返回空、不再触发 v1 后台生成——只留 consolidation 轨（读取统一走
    # memory_retrieval_service；v1 nightly 行已在生成侧停写）。
    from app.services.memory_runtime_state import memory_runtime_enabled
    if memory_runtime_enabled(config):
        return AgentSharedContext(
            agent_state=None,
            memory_summary="",
            dream_summary="",
            memory_entries=[],
        )
    state = await ensure_user_agent_state(db, user_id)
    if not state.memory_summary or not state.dream_summary:
        if user_id not in _memory_gen_in_flight:
            _memory_gen_in_flight.add(user_id)
            asyncio.create_task(_generate_memory_background(user_id))
        # Use fallback content for first request — background task will
        # populate summaries for subsequent requests.

    memories_result = await db.execute(
        select(AgentMemory)
        .where(AgentMemory.agent_state_id == state.id)
        .order_by(desc(AgentMemory.importance), desc(AgentMemory.created_at))
        .limit(config.agent_memory_max_items)
    )
    memory_entries = memories_result.scalars().all()
    return AgentSharedContext(
        agent_state=state,
        memory_summary=state.memory_summary or "",
        dream_summary=state.dream_summary or "",
        memory_entries=memory_entries,
    )


async def _generate_memory_background(user_id: str) -> None:
    try:
        from app.db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as bg_db:
            await generate_user_agent_memory(bg_db, user_id, force=True)
    except Exception:
        logger.exception("background memory generation failed for user %s", user_id)
    finally:
        _memory_gen_in_flight.discard(user_id)