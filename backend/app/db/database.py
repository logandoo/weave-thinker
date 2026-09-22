# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, Boolean, Float, Integer, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector
from datetime import datetime
import uuid

from app.core.config import get_config
from app.db import migrations
from app.db.migrations import run_startup_migrations

import logging

logger = logging.getLogger(__name__)

config = get_config()

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="user")
    is_active = Column(Boolean, default=True)
    last_login_at = Column(DateTime, nullable=True)
    last_login_ip = Column(String(45), nullable=True)
    agent_permissions = Column(Text, nullable=True)
    ui_preferences = Column(Text, nullable=True)
    # 用户信息（2026-09-13）：昵称与头像（data URL，256×256 JPEG，服务端重编码）。
    nickname = Column(String(100), nullable=True)
    avatar_data = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    conversation_groups = relationship("ConversationGroup", back_populates="user", cascade="all, delete-orphan")
    assistants = relationship("Assistant", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    notebooks = relationship("Notebook", back_populates="user", cascade="all, delete-orphan")
    agent_state = relationship("UserAgentState", back_populates="user", uselist=False, cascade="all, delete-orphan")
    workspace = relationship("UserWorkspace", back_populates="user", uselist=False, cascade="all, delete-orphan")
    asr_hotwords = relationship("UserAsrHotword", back_populates="user", cascade="all, delete-orphan")
    skills = relationship("UserSkill", back_populates="user", cascade="all, delete-orphan")
    model_providers = relationship("UserModelProvider", back_populates="user", cascade="all, delete-orphan")


class UserModelProvider(Base):
    """用户级模型供应商覆盖（2026-09-13；逐供应商维度 2026-09-15）。

    LLM 类型按供应商（registry 别名）各至多一行：kind='llm' + provider=<alias>
    （如 qwen3.8/doubao/main）→ 覆盖键 "llm:<alias>"；provider='' 的 llm 行为
    legacy 全局回落（仅当该别名无专属行时生效）。其余 kind（vlm/embedding/
    rerank/asr/tts）每用户至多一行、provider 恒 ''，语义不变。
    行存在且 enabled=true 且（base_url/model_name/params 任一非空）时，该用户
    上下文内对应端点解析被覆盖（model_gateway.user_overrides.apply_user_override）。
    api_key 仅写不读（GET 只回掩码）；自定义 base_url + 空 key → 发空（no-key 哨兵）。
    """
    __tablename__ = "user_model_providers"
    # 每用户每 (kind, provider) 至多一行（A4.9 Important 修复：create_all 先于
    # 迁移执行，唯一性必须由 ORM 声明；迁移侧另有 CREATE UNIQUE INDEX
    # IF NOT EXISTS 覆盖既有表）。
    __table_args__ = (
        UniqueConstraint(
            "user_id", "kind", "provider",
            name="uq_user_model_providers_user_kind_provider",
        ),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(20), nullable=False)
    # LLM 供应商别名（其余类型恒 ''）；默认 '' 使存量行语义不变。
    provider = Column(String(64), nullable=False, default="", server_default="")
    enabled = Column(Boolean, nullable=False, default=True)
    base_url = Column(String(500), nullable=True)
    api_key = Column(String(500), nullable=True)
    model_name = Column(String(200), nullable=True)
    params_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="model_providers")


class Assistant(Base):
    __tablename__ = "assistants"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    system_prompt = Column(Text, default="")
    temperature = Column(Float, nullable=True)
    top_p = Column(Float, nullable=True)
    top_k = Column(Integer, nullable=True)
    presence_penalty = Column(Float, nullable=True)
    frequency_penalty = Column(Float, nullable=True)
    max_tokens = Column(Integer, nullable=True)
    use_custom_model = Column(Boolean, default=False)
    custom_api_url = Column(String(500), nullable=True)
    custom_api_key = Column(String(500), nullable=True)
    custom_model_name = Column(String(200), nullable=True)
    provider_type = Column(String(20), default="deepseek")
    extra_body = Column(Text, nullable=True)
    # PHASE 3: optional sub-task LLM override. Keeping these NULL means
    # iterations reuse the main client with thinking forced off (the
    # default for all existing assistants).
    subtask_custom_api_url = Column(String(500), nullable=True)
    subtask_custom_api_key = Column(String(500), nullable=True)
    subtask_custom_model_name = Column(String(200), nullable=True)
    subtask_provider_type = Column(String(20), nullable=True)
    subtask_extra_body = Column(Text, nullable=True)
    use_subtask_model = Column(Boolean, default=False)
    thinking_budget = Column(Integer, nullable=True)
    # Qwen3.8(Local) provider: non-thinking sampling set (min_p / repetition_penalty
    # join the existing temperature/top_p/top_k/presence_penalty) plus the
    # thinking-mode sampling set and preserve_thinking (chat_template_kwargs).
    min_p = Column(Float, nullable=True)
    repetition_penalty = Column(Float, nullable=True)
    thinking_temperature = Column(Float, nullable=True)
    thinking_top_p = Column(Float, nullable=True)
    thinking_top_k = Column(Integer, nullable=True)
    thinking_min_p = Column(Float, nullable=True)
    thinking_presence_penalty = Column(Float, nullable=True)
    thinking_repetition_penalty = Column(Float, nullable=True)
    preserve_thinking = Column(Boolean, default=True)
    # 模型网关解耦（2026-08-31 两文件合并）：模型选择 = 逻辑别名（config_model.toml 的
    # [endpoints.<alias>]）；NULL = 未显式选择（legacy provider_type/custom_* 语义）。
    model_alias = Column(String(64), nullable=True)
    subtask_model_alias = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="assistant", cascade="all, delete-orphan")
    conversation_groups = relationship("ConversationGroup", back_populates="assistant", cascade="all, delete-orphan")
    user = relationship("User", back_populates="assistants")
    chat_sessions = relationship("ChatSession", back_populates="assistant")


class ConversationGroup(Base):
    __tablename__ = "conversation_groups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    assistant_id = Column(String(36), ForeignKey("assistants.id", ondelete="CASCADE"), nullable=True)
    name = Column(String(100), nullable=False, default="新分组")
    color = Column(String(20), nullable=False, default="#3b82f6")
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversation_groups")
    assistant = relationship("Assistant", back_populates="conversation_groups")
    conversations = relationship("Conversation", back_populates="group")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    assistant_id = Column(String(36), ForeignKey("assistants.id", ondelete="SET NULL"), nullable=True)
    group_id = Column(String(36), ForeignKey("conversation_groups.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(255), default="新对话")
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Deathmatch (死磕) mode
    deathmatch_mode = Column(Boolean, default=False)
    deathmatch_goal = Column(Text, nullable=True)
    deathmatch_status = Column(String(20), default="inactive")
    deathmatch_turns = Column(Integer, default=0)
    deathmatch_max_turns = Column(Integer, default=0)  # 0 = unlimited (autonomy wave 2026-08-31); snapshotted from config at grilling completion / resume
    deathmatch_consecutive_failures = Column(Integer, default=0)
    deathmatch_verdict = Column(Text, nullable=True)
    deathmatch_reason = Column(Text, nullable=True)
    deathmatch_grilling_complete = Column(Boolean, default=False)
    deathmatch_grilling_total = Column(Integer, default=0)
    deathmatch_grilling_completed = Column(Integer, default=0)
    deathmatch_grilling_round = Column(Integer, default=0)
    deathmatch_grilling_round_total = Column(Integer, default=3)
    deathmatch_grilling_qa_history = Column(JSON, default=list)
    deathmatch_context_summary = Column(Text, nullable=True)
    deathmatch_expected_marker = Column(Text, nullable=True)
    deathmatch_marker_miss_count = Column(Integer, default=0)
    deathmatch_compressed_context = Column(Text, nullable=True)
    # PEVR (Plan-Execute-Verify-Replan) extension — see loop_improve.md Phase 3
    deathmatch_plan = Column(JSON, nullable=True)            # structured plan {steps:[...]}
    deathmatch_plan_version = Column(Integer, default=0)     # bumped on each replan
    deathmatch_reflections = Column(JSON, default=list)       # recent reflection entries
    deathmatch_wall_time_started_at = Column(DateTime, nullable=True)
    deathmatch_max_wall_time_seconds = Column(Integer, default=0)  # 0 = unlimited (autonomy wave 2026-08-31); snapshotted from config at grilling completion / resume
    deathmatch_wall_time_used_seconds = Column(Integer, default=0)  # cumulative across resume cycles (C1)
    deathmatch_bible_draft = Column(JSON, nullable=True)  # story-bible draft written right after grilling (creative goals)
    deathmatch_subgoals = Column(JSON, default=list)  # user-appended acceptance criteria mid-loop (D3)
    # ── 死磕 DAG 波次 W0-W2（2026-09-18）契约/机械化/恢复层 ──────────────
    # W1a: first-class acceptance criteria [{id,text,type:mechanical|judgmental,
    # source:system|user,check:{kind:file|gate|none,...}}] — synthesized after
    # grilling, append-only, injected into judge/verifier/continuation/handoff
    # and executed mechanically by the goal comparator at finalize.
    deathmatch_acceptance_criteria = Column(JSON, nullable=True)
    # W2b: failed-direction memory [{family,reason,count,forbidden,ts}] —
    # injected into continuation/replan so a direction recorded as failed
    # (>= threshold) is never retried without a new insight.
    deathmatch_failed_directions = Column(JSON, nullable=True)
    # W2c: PAUSED resume packet {gate,question,options,default_if_continue,state}
    # written on every human_gate / crash normalization, cleared on resume.
    deathmatch_pause_state = Column(JSON, nullable=True)
    # W2c: append-only decision/event log (capped by [deathmatch] events_cap):
    # plan_audit / comparator / local_patch / replan / statement_violation /
    # human_gate / finalized_without_step_evidence / ...
    deathmatch_events = Column(JSON, nullable=True)
    # W2b: consecutive no-progress replans (global convergence circuit breaker;
    # reset on any progress). Cap -> resumable human_gate.
    deathmatch_no_progress_replans = Column(Integer, default=0)
    # P1-5 (2026-08-30): settled-verdict ledger — step completions and
    # reconcile overturns; injected into judge/verifier prompts with the
    # no-flip-without-new-evidence rule. MUST be ORM-mapped (A4.9 W2-C1:
    # an unmapped attribute silently persists nothing across requests).
    deathmatch_settled_ledger = Column(JSON, nullable=True)
    deathmatch_last_verification_result = Column(JSON, nullable=True)
    deathmatch_verify_failures = Column(Integer, default=0)   # consecutive verifier non-complete
    deathmatch_human_gate = Column(Text, nullable=True)       # structured human-gate report

    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    user = relationship("User", back_populates="conversations")
    assistant = relationship("Assistant", back_populates="conversations")
    group = relationship("ConversationGroup", back_populates="conversations")
    chat_sessions = relationship("ChatSession", back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"))
    role = Column(String(20))
    content = Column(Text)
    reasoning_content = Column(Text, nullable=True)
    tool_results = Column(Text, nullable=True)
    # PHASE 2B: OpenAI-style tool_calls array (JSON-encoded list of
    # {id, type:"function", function:{name, arguments}}). Persisted so
    # multi-turn conversations replay structured tool history through the
    # LLM context instead of just opaque content text.
    tool_calls = Column(Text, nullable=True)
    # 本轮上下文 token 用量（context_info 事件的最新值，JSON 字符串）——
    # 持久化后跨设备可见（手机端发出的轮次在电脑端打开也能看到本轮 tokens）。
    context_info = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_token = Column(String(512), unique=True, index=True, nullable=False)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    last_active_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="sessions")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True)
    assistant_id = Column(String(36), ForeignKey("assistants.id", ondelete="SET NULL"), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    message_count = Column(Integer, default=0)
    total_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="chat_sessions")
    conversation = relationship("Conversation", back_populates="chat_sessions")
    assistant = relationship("Assistant", back_populates="chat_sessions")


class UserAgentState(Base):
    __tablename__ = "user_agent_states"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    agent_name = Column(String(120), nullable=False, default="共享智能体")
    memory_summary = Column(Text, nullable=True)
    dream_summary = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    last_memory_generated_at = Column(DateTime, nullable=True)
    last_dream_generated_at = Column(DateTime, nullable=True)
    last_note_processed_at = Column(DateTime, nullable=True)
    last_message_processed_at = Column(DateTime, nullable=True)
    last_file_memory_processed_at = Column(DateTime, nullable=True)
    last_subconscious_scan_at = Column(DateTime, nullable=True)
    last_consolidation_at = Column(DateTime, nullable=True)
    total_concept_count = Column(Integer, default=0)
    total_episode_count = Column(Integer, default=0)
    latest_dream_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="agent_state")
    memories = relationship("AgentMemory", back_populates="agent_state", cascade="all, delete-orphan")
    dreams = relationship("AgentDream", back_populates="agent_state", cascade="all, delete-orphan")


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_state_id = Column(String(36), ForeignKey("user_agent_states.id", ondelete="CASCADE"), nullable=False)
    source_type = Column(String(50), nullable=False, default="daily-summary")
    source_id = Column(String(64), nullable=True)
    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    importance = Column(Float, nullable=False, default=0.5)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agent_state = relationship("UserAgentState", back_populates="memories")


class AgentDream(Base):
    __tablename__ = "agent_dreams"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_state_id = Column(String(36), ForeignKey("user_agent_states.id", ondelete="CASCADE"), nullable=False)
    generated_for_date = Column(String(10), nullable=False)
    summary = Column(Text, nullable=False)
    source_note_count = Column(Integer, nullable=False, default=0)
    source_message_count = Column(Integer, nullable=False, default=0)
    source_concept_count = Column(Integer, nullable=False, default=0)
    source_cluster_count = Column(Integer, nullable=False, default=0)
    metadata_json = Column(Text, nullable=True)
    dream_type = Column(String(20), nullable=False, default="consolidation")
    created_at = Column(DateTime, default=datetime.utcnow)

    agent_state = relationship("UserAgentState", back_populates="dreams")


class MemoryConcept(Base):
    __tablename__ = "memory_concepts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    canonical_name = Column(String(500), nullable=False)
    description_short = Column(String(80), nullable=False)
    description_full = Column(Text, nullable=True)
    aliases = Column(Text, nullable=True)
    weight = Column(Float, nullable=False, default=0.5)
    importance = Column(Float, nullable=False, default=0.5)
    importance_evaluated = Column(Boolean, nullable=False, default=False)
    stability = Column(Float, nullable=False, default=14.0)
    source_trust = Column(String(20), nullable=False, default="user_stated")
    memory_type = Column(String(20), nullable=False, default="semantic")
    activation_strength = Column(Float, nullable=False, default=1.0)
    recurrence_count = Column(Integer, nullable=False, default=0)
    last_recurrence_at = Column(DateTime, nullable=True)
    hot_forget_count = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="active")
    source_type = Column(String(50), nullable=False, default="extracted")
    source_raw_ids = Column(Text, nullable=True)
    source_unit_ids = Column(Text, nullable=True)
    needs_review = Column(Boolean, nullable=False, default=False)
    metadata_json = Column(Text, nullable=True)
    last_recalled_at = Column(DateTime, nullable=True)
    valid_from = Column(DateTime, default=datetime.utcnow)
    valid_to = Column(DateTime, nullable=True)
    superseded_by = Column(String(36), nullable=True)
    embedding = Column(Vector(1024), nullable=True)  # 维度须与 [endpoints.embedding].extra.dim 一致（Wave D）
    embedding_updated_at = Column(DateTime, nullable=True)
    # DC2（2026-09-14）：向量溯源
    embedding_model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MemoryCluster(Base):
    __tablename__ = "memory_clusters"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    summary = Column(Text, nullable=True)
    weight = Column(Float, nullable=False, default=0.5)
    embedding = Column(Vector(1024), nullable=True)  # 维度须与 [endpoints.embedding].extra.dim 一致（Wave D）
    # DC2（2026-09-14）：向量溯源——记录生成该向量的模型名，防跨模型混空间
    embedding_model = Column(String(100), nullable=True)
    member_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConceptClusterMember(Base):
    __tablename__ = "concept_cluster_members"

    concept_id = Column(String(36), ForeignKey("memory_concepts.id", ondelete="CASCADE"), primary_key=True)
    cluster_id = Column(String(36), ForeignKey("memory_clusters.id", ondelete="CASCADE"), primary_key=True)


class ConceptRelation(Base):
    __tablename__ = "concept_relations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_id = Column(String(36), ForeignKey("memory_concepts.id", ondelete="CASCADE"), nullable=False)
    target_id = Column(String(36), ForeignKey("memory_concepts.id", ondelete="CASCADE"), nullable=False)
    relation_type = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    weight = Column(Float, nullable=False, default=0.5)
    # D1（2026-09-14）：边来源（'llm'=LLM 抽取 / 'deterministic'=确定性构边）；
    # 存量行为 NULL，读侧按 'llm' 语义兼容（DC5 门控）
    edge_source = Column(String(20), nullable=True, default="llm")
    created_at = Column(DateTime, default=datetime.utcnow)


class MemoryClarification(Base):
    __tablename__ = "memory_clarifications"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(String(36), nullable=True)
    message_id = Column(String(36), nullable=True)
    original_text = Column(Text, nullable=False)
    correction_type = Column(String(30), nullable=False)
    affected_concept_ids = Column(Text, nullable=True)
    new_description = Column(Text, nullable=True)
    confidence = Column(Float, nullable=False, default=0.0)
    applied = Column(Boolean, default=False)
    applied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SubconsciousLog(Base):
    __tablename__ = "subconscious_log"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    unit_kind = Column(String(20), nullable=False, default="message")
    raw_text = Column(Text, nullable=False)
    source_ids = Column(Text, nullable=False)
    embedding = Column(Vector(1024), nullable=True)  # 维度须与 [endpoints.embedding].extra.dim 一致（Wave D）
    # DC2/B2（2026-09-14）：向量溯源 + 幂等去重键 + 待补嵌标记
    embedding_model = Column(String(100), nullable=True)
    content_hash = Column(String(64), nullable=True)
    needs_embedding = Column(Boolean, nullable=False, default=False)
    promoted = Column(Boolean, nullable=False, default=False)
    promoted_at = Column(DateTime, nullable=True)
    recurrence_count = Column(Integer, nullable=False, default=0)
    last_recurrence_at = Column(DateTime, nullable=True)
    recurrence_scan_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class MemoryEpisode(Base):
    __tablename__ = "memory_episodes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    narrative = Column(Text, nullable=False)
    source_unit_ids = Column(Text, nullable=False)
    source_concept_ids = Column(Text, nullable=True)
    valid_from = Column(DateTime, default=datetime.utcnow)
    valid_to = Column(DateTime, nullable=True)
    superseded_by = Column(String(36), nullable=True)
    embedding = Column(Vector(1024), nullable=True)  # 维度须与 [endpoints.embedding].extra.dim 一致（Wave D）
    # DC2（2026-09-14）：向量溯源
    embedding_model = Column(String(100), nullable=True)
    merged_from = Column(String(36), nullable=True)
    last_recalled_at = Column(DateTime, nullable=True)
    source_type = Column(String(50), default="extracted")  # 'extracted' | 'migration'
    # D1（2026-09-14）：RippleMem 式 P/L/T 线索（可空，JSON 数组字符串；存量行保持 NULL）
    participants = Column(String(1000), nullable=True)
    locations = Column(String(1000), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MemoryLLMCall(Base):
    __tablename__ = "memory_llm_calls"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(50), nullable=False)
    model = Column(String(100), nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    # DC1（2026-09-14）：'write'=计费（进入成本治理降级口径）/'read'=遥测
    # （读热路径调用，只入账不参与降级）
    billing_class = Column(String(20), nullable=True, default="write")
    created_at = Column(DateTime, default=datetime.utcnow)


class MemoryRecallLog(Base):
    """C1/N16（2026-09-14）：逐轮召回台账。

    只存元数据（候选 ids/分层分/门控/预算/字符/截断/耗时/缓存命中），
    **不存任何记忆内容**（帕累托 D6 隐私约束）。异步 fire-and-forget 写，
    失败静默——台账永不阻塞或影响检索主流程。FK CASCADE 对齐用户/会话
    （DC7）；保留策略（30 天 + per-user 上限）只作用本表。
    """
    __tablename__ = "memory_recall_log"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True)
    query_hash = Column(String(32), nullable=True)
    candidate_ids = Column(Text, nullable=True)
    tier_scores = Column(Text, nullable=True)
    gate_score = Column(Float, nullable=True, default=0.0)
    budget_chars = Column(Integer, nullable=False, default=0)
    injected_chars = Column(Integer, nullable=False, default=0)
    truncated = Column(Boolean, nullable=False, default=False)
    elapsed_ms = Column(Integer, nullable=False, default=0)
    cache_hit = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserWorkspace(Base):
    __tablename__ = "user_workspaces"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    root_path = Column(String(500), nullable=False)
    python_env_path = Column(String(500), nullable=True)
    node_workspace_path = Column(String(500), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="workspace")


class UserAsrHotword(Base):
    __tablename__ = "user_asr_hotwords"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    text = Column(String(120), nullable=False)
    weight = Column(Integer, nullable=False, default=4)
    lang = Column(String(10), nullable=True)
    dashscope_vocabulary_id = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="asr_hotwords")


class UserSkill(Base):
    __tablename__ = "user_skills"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="skills")
    files = relationship("SkillFile", back_populates="skill", cascade="all, delete-orphan")


class SkillFile(Base):
    __tablename__ = "skill_files"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    skill_id = Column(String(36), ForeignKey("user_skills.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_content = Column(Text, nullable=False)
    file_type = Column(String(50), nullable=False)  # 'script', 'config', 'data', 'other'
    is_executable = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    skill = relationship("UserSkill", back_populates="files")


class Notebook(Base):
    __tablename__ = "notebooks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False, default="新笔记本")
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="notebooks")
    notes = relationship("Note", back_populates="notebook", cascade="all, delete-orphan")


class Note(Base):
    __tablename__ = "notes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    notebook_id = Column(String(36), ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=True)
    content = Column(Text, default="")
    raw_transcription = Column(Text, nullable=True)  # Original voice transcription before editing
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    notebook = relationship("Notebook", back_populates="notes")


class AgentTask(Base):
    """Tracks individual tasks — both synchronous sub-tasks and background long-running tasks."""
    __tablename__ = "agent_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True)
    parent_task_id = Column(String(36), ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=True)
    assistant_id = Column(String(36), ForeignKey("assistants.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(255), nullable=True)
    task_type = Column(String(50), nullable=False, default="general")  # plan, search, browse, code, generate, synthesize
    goal = Column(Text, nullable=False)
    context = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending, running, completed, failed, cancelled
    progress = Column(Float, default=0.0)
    iterations_done = Column(Integer, default=0)
    iterations_max = Column(Integer, default=30)
    elapsed_seconds = Column(Float, default=0.0)
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    search_results = Column(Text, nullable=True)
    browser_results = Column(Text, nullable=True)
    intermediate_steps = Column(Text, nullable=True)
    output_conversation_id = Column(String(36), nullable=True)
    output_note_id = Column(String(36), nullable=True)
    # F1（2026-09-14）：运行中补充消息（JSON 数组 [{id, content, created_at}]，
    # worker 每 5s 轮询推入 interjection_queue 并在投递后清空；可空）
    pending_messages = Column(Text, nullable=True)
    # D-轻（2026-09-22 durable execution 波）：断点续跑快照（JSON
    # {version,cursor,messages,budget_used,elapsed,updated_at}）。有值时
    # 启动恢复置 resumable 并从 cursor 续跑；无值维持旧行为（标 failed）。
    checkpoint = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    parent = relationship("AgentTask", remote_side="AgentTask.id", backref="subtasks")


class DurableJob(Base):
    """D-重（2026-09-22）：durable job runner 的持久作业句柄。

    execute_code/process/terminal 的 5h+ 重作业提交为 detached 子进程作业：
    状态机 queued→leased→running→{succeeded,failed,cancelled,unknown}（迁移
    白名单见 app.services.durable_types.can_transition）；退出回执=作业根目录
    exit_code 文件；重复 idempotency_key 返回既有作业（不新建）。
    """
    __tablename__ = "durable_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(20), nullable=False, default="tool")  # execute_code|terminal|process|tool
    spec = Column(Text, nullable=False)  # JSON {kind, command|code, workdir, timeout_seconds, env}
    idempotency_key = Column(String(128), nullable=True, unique=True)
    state = Column(String(20), nullable=False, default="queued")
    attempts = Column(Integer, default=0)
    lease_owner = Column(String(64), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    pid = Column(Integer, nullable=True)
    log_path = Column(String(512), nullable=True)
    exit_code = Column(Integer, nullable=True)
    result_digest = Column(Text, nullable=True)
    cancel_requested_at = Column(DateTime, nullable=True)
    cancel_state = Column(String(20), nullable=True)  # requested|acknowledged|too_late|failed
    error = Column(Text, nullable=True)
    timeout_seconds = Column(Float, default=0.0)  # 0=不限（5h+ 重作业的真实需求）
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DurableJobEvent(Base):
    """append-only 事件 WAL（seq per job）——恢复凭据而非仅远端状态。"""
    __tablename__ = "durable_job_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("durable_jobs.id", ondelete="CASCADE"), nullable=False)
    seq = Column(Integer, nullable=False)
    event_type = Column(String(40), nullable=False)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_durable_job_event_seq"),)


class DurableJobArtifact(Base):
    """作业产物登记（path+sha256+bytes）——「产出了什么」的证据面。"""
    __tablename__ = "durable_job_artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("durable_jobs.id", ondelete="CASCADE"), nullable=False)
    path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=False)
    bytes = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class SideEffectLedger(Base):
    """副作用台账（D-轻）：幂等键 v2 含 call_id，重放同 call 命中即跳过（failed 允许重试）。

    键格式见 app.services.durable_types.make_key（keyed on cursor+call_id 不是 seq）。
    """
    __tablename__ = "side_effect_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    principal_type = Column(String(20), nullable=False)   # agent_task|deathmatch
    principal_id = Column(String(36), nullable=False)
    cursor = Column(Integer, nullable=False)
    tool_name = Column(String(64), nullable=False)
    idempotency_key = Column(String(160), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="pending")  # pending|done|failed|unknown
    result_ref = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScheduledTask(Base):
    """User-created scheduled/recurring tasks executed by the agent."""
    __tablename__ = "scheduled_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    assistant_id = Column(String(36), ForeignKey("assistants.id", ondelete="SET NULL"), nullable=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(255), nullable=False)
    prompt = Column(Text, nullable=False)
    schedule_type = Column(String(20), nullable=False, default="cron")
    schedule_expr = Column(String(100), nullable=False)
    next_run_at = Column(DateTime, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="active")
    repeat_count = Column(Integer, nullable=True)
    run_count = Column(Integer, nullable=False, default=0)
    fail_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    assistant = relationship("Assistant")


class ExportTask(Base):
    __tablename__ = "export_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    task_type = Column(String(20), nullable=False, default="single")
    format = Column(String(10), nullable=False, default="pdf")
    note_id = Column(String(36), nullable=True)
    note_ids = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    progress = Column(Float, default=0.0)
    file_path = Column(String(500), nullable=True)
    filename = Column(String(255), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


engine = create_async_engine(
    config.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=config.database_pool_size,
    max_overflow=config.database_max_overflow,
    pool_timeout=config.database_pool_timeout,
    pool_recycle=config.database_pool_recycle,
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def register_advisory_lock_cleanup(target_engine) -> None:
    """连接归还池时释放该会话持有的全部 session 级 advisory 锁。

    背景（2026-08-10 线上事故）：memory scheduler 用 session 级 pg_try_advisory_lock
    做 per-user 互斥；当 per-user 处理异常（超时/事务中止）时 finally 里的 unlock 失败，
    锁随连接回池残留（SQLAlchemy 池 reset 只 rollback 事务、不释放 advisory 锁），
    导致 25/25 用户锁全部挂死在 idle 池连接上，调度器（扫描+consolidation）静默跳过
    所有用户。此监听器在每条连接归还时执行 pg_advisory_unlock_all() 根治泄漏类问题。
    失败必须留痕（降频 warn）：静默吞异常正是本次事故"无任何告警"的原罪。
    """
    from sqlalchemy import event

    _reset_failure_count = 0

    @event.listens_for(target_engine.sync_engine, "reset")
    def _reset_advisory_locks(dbapi_conn, record):
        nonlocal _reset_failure_count
        try:
            dbapi_conn.await_(dbapi_conn._connection.execute("SELECT pg_advisory_unlock_all()"))
        except Exception:
            # 失败路径安全（连接会被池 invalidate 关闭，session 锁随连接消亡自愈），
            # 但必须留痕：连续失败说明清理机制失效，不能回到静默状态。
            _reset_failure_count += 1
            if _reset_failure_count <= 3 or _reset_failure_count % 50 == 0:
                logger.warning(
                    "pg_advisory_unlock_all on pool reset failed (%d times so far) — "
                    "advisory lock cleanup may be broken", _reset_failure_count,
                )


register_advisory_lock_cleanup(engine)


async def init_db():
    async with engine.begin() as conn:
        # 先建 ORM 表（create_all 幂等，旧库 no-op），再跑 idempotent 迁移：
        # 迁移含 ALTER TABLE ...，全新空库时表尚不存在——旧顺序（迁移在前）
        # 首条 ALTER 即 UndefinedTableError，全新部署直接起不来。
        # create_all 依赖 PGVECTOR_AVAILABLE 决定 memory 表去留，故先显式探测
        # （模块初值 True 仅防 import 期属性缺失；探测幂等，run_startup_migrations
        # 内部会再探一次，无副作用）。
        migrations.PGVECTOR_AVAILABLE = await migrations.probe_pgvector(conn)
        if migrations.PGVECTOR_AVAILABLE:
            await conn.run_sync(Base.metadata.create_all)
        else:
            # §9.5：pgvector 缺失时 memory v2 表（含 vector 列）无法建，排除后照常启动
            memory_tables = {
                "memory_concepts", "memory_clusters", "concept_cluster_members",
                "concept_relations", "memory_clarifications", "subconscious_log",
                "memory_episodes", "memory_llm_calls",
            }
            tables = [t for t in Base.metadata.sorted_tables if t.name not in memory_tables]
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
        await run_startup_migrations(conn)


class WebSearchResult(Base):
    """web_search 工具每次联网检索命中的结果，逐条落库（溯源/记忆用）。"""

    __tablename__ = "web_search_results"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    query = Column(Text, nullable=False)
    provider = Column(String(20), nullable=False)
    result_rank = Column(Integer, nullable=False, default=0)
    title = Column(Text, nullable=True)
    url = Column(Text, nullable=False)
    snippet = Column(Text, nullable=True)
    published_date = Column(String(10), nullable=True)
    extra = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()