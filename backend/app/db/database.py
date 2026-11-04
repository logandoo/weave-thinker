# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, Boolean, Float, Integer, JSON, UniqueConstraint, BigInteger, Index, PrimaryKeyConstraint
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
    mcp_servers = relationship("UserMcpServer", back_populates="user", cascade="all, delete-orphan")


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


class UserMcpServer(Base):
    """用户级 MCP server（2026-11-01）。

    每个用户拥有自己的 stdio/HTTP MCP server；运行期注册为
    `mcp_u<uid8>_<server>_<tool>`，``ToolEntry.owner_user_id`` 限定可见性。
    ``api_key``/``env_json`` 为写侧凭据，GET 只回掩码；``args_json`` 是 JSON
    数组（stdio 参数），``env_json`` 是 JSON 对象。全局 ``[mcp.servers.*]``
    继续存在，二者互不影响（全局热重载不会触碰本表注册的工具）。
    """
    __tablename__ = "user_mcp_servers"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_mcp_servers_user_name"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(64), nullable=False)
    transport = Column(String(10), nullable=False, default="http")  # http | stdio
    url = Column(String(1000), nullable=True)
    api_key = Column(String(1000), nullable=True)
    command = Column(String(1000), nullable=True)
    args_json = Column(Text, nullable=True)
    env_json = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="mcp_servers")


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
    citation_style = Column(String(64), nullable=True)
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
    # W2 证据视图 v1（2026-10-14，ACG 2609.32754）：证据单元归档
    # [{id, kind: OBSERVED|FACT|FAIL|CLAIM|PATCH_SUMMARY, source: judge|verifier,
    # step_id, text(截300), ts, attributed}]，cap 200 保留最新。A3 勘正：
    # derived state，不入 DEATHMATCH_SYNC_FIELDS（不同步客户端）。
    deathmatch_evidence_units = Column(JSON, default=list)
    # W2b: consecutive no-progress replans (global convergence circuit breaker;
    # reset on any progress). Cap -> resumable human_gate.
    deathmatch_no_progress_replans = Column(Integer, default=0)
    # P1-5 (2026-08-30): settled-verdict ledger — step completions and
    # reconcile overturns; injected into judge/verifier prompts with the
    # no-flip-without-new-evidence rule. MUST be ORM-mapped (A4.9 W2-C1:
    # an unmapped attribute silently persists nothing across requests).
    deathmatch_settled_ledger = Column(JSON, nullable=True)
    # AEWM 波（2026-09-25，arXiv 2609.28416 anti-contamination）：证伪台账
    # —— 被验证器/仲裁/分诊证伪的断言与假设。与 settled_ledger（正向定案，
    # 禁翻案）语义相反：这里是反向禁用（不得再作为后续决策依据）。
    deathmatch_retracted_claims = Column(JSON, nullable=True)
    deathmatch_last_verification_result = Column(JSON, nullable=True)
    deathmatch_verify_failures = Column(Integer, default=0)   # consecutive verifier non-complete
    deathmatch_human_gate = Column(Text, nullable=True)       # structured human-gate report
    # W16 设备执行模式：执行环境（NULL=云端 workspace；device:<device_id>=配对设备）。
    # 创建即定（D-6）：首设 adopt，之后 API 拒绝变更——切换=新建会话。
    execution_target = Column(String(80), nullable=True)

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
    # 2026-10-06（conv ae9aa092）：渐进落库交付状态。
    # streaming=在途（run 开始即建行、周期刷写；默认列表/同步过滤，不展示）；
    # final=正常完成（含用户主动 stop 的部分保存）；interrupted=进程死亡/
    # 断连等非预期中断（前端「已中断」徽标）。
    delivery_status = Column(String(16), nullable=False, default="final", index=True)
    # 2026-11-01 插话内联定位：assistant 行上的小型侧车（JSON 数组，每项
    # {message_id,content,created_at,after_index,after_char}）。tool_results 可能
    # 被外置成桩，侧车列保证前端始终拿得到插话边界；user 行的
    # embedded_in_message_id 指向渲染它的 assistant 行（置空 = 旧式顶层气泡）。
    interjection_items = Column(Text, nullable=True)
    embedded_in_message_id = Column(String(36), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")
    # 2026-10-22（60 后台任务 3af90d5a FK 违规根修）：UOW 的 INSERT 依赖序只由
    # relationship() 依赖处理器建立（canonical SQLAlchemy 行为——表级 FK 仅影响
    # create_all 排序）。此前 Message↔MessagePayload 无 relationship，新 Message
    # 超限字段同 flush 外置时 payload INSERT 先行 → message_payloads_message_id_
    # fkey 违规。passive_deletes=True：删除语义仍交给 DB CASCADE（ondelete=
    # "CASCADE"），ORM 侧零行为变更——此 relationship 只为建立 UOW 依赖序。
    payloads = relationship(
        "MessagePayload", back_populates="message", passive_deletes=True)


class MessagePayload(Base):
    """外置的超限消息字段全文（tool_results / reasoning_content / tool_calls）。

    行内 columns 只留显式 stub（payload_ref + size + preview），全文在此表按
    (message_id, field) 归属唯一存一行（随本消息 CASCADE）；sha256 仅作完整性
    元数据，不作寻址。DESIGN_conv_open_performance §2 P1-1。
    """

    __tablename__ = "message_payloads"

    # 评审 C1/C2 修复：payload 行按 (message_id, field) 唯一、归属单条消息——
    # 不跨消息共享（旧 sha 全局去重 + message_id CASCADE 会在删除任一消息时
    # 连带销毁他消息的共享行=静默数据丢失）；sha256 仅作完整性元数据不作寻址。
    # 索引（2026-10-06 冗余去重）：message_id 不再单列建索引——uq_message_payloads_
    # msg_field(message_id,field) 前缀已覆盖归属查询，单列索引是纯写放大。
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id = Column(String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    field = Column(String(32), nullable=False)
    content = Column(Text, nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("message_id", "field", name="uq_message_payloads_msg_field"),)

    # 见 Message.payloads 注记（2026-10-22 FK 根修：仅为 UOW 依赖序而建，
    # passive_deletes=True 保持 DB CASCADE 删除语义不变）。
    message = relationship(
        "Message", back_populates="payloads", passive_deletes=True)


class MessageAttempt(Base):
    """被拒/中断的草稿尝试全文台账（2026-10-06，conv ae9aa092，用户裁定：
    被打回重写的回答保留并标记）。

    每次审计打回（remand/reject/needs_evidence 触发的重写）落一行：
    草稿全文 + 裁决元数据（verdict/problem/source）。status:
    rejected=审计打回；interrupted=进程死亡时仍未完成的在途草稿；
    accepted=（预留）最终被接受的草稿。只读审计面，不进渲染契约/同步。
    """

    __tablename__ = "message_attempts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # A4.9 r1 M3：message_id 可空 + SET NULL——空终态清理删除 streaming 空行时，
    # 该轮的被打回草稿台账不随行的 CASCADE 消失（用户裁定「保留并标记」）。
    # 索引（2026-10-06 冗余去重）：message_id 不再单列建索引——
    # idx_message_attempts_message(message_id,attempt_no) 前缀已覆盖。
    message_id = Column(String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_no = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="rejected")
    content = Column(Text, nullable=False, default="")
    verdict_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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
    # W3-2（记忆/长程 v2，2026-10-23）：dream 安全——入库前注入扫描标记 + 派生
    # taint（继承来源最严 taint）；scanned=FALSE / 高 taint 的行不进入注入
    provenance_taint = Column(String(16), nullable=True)
    scanned = Column(Boolean, nullable=False, default=False)
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
    # W5-3（记忆/长程 v2，2026-10-23）：保留/淘汰。retention_score 由
    # scripts/backfill_retention_score.py 回填、memory_retention_service 维护；
    # fidelity_state: full → compressed → archived（降级 > 删除，P2 可逆）
    retention_score = Column(Float, nullable=False, default=0.0)
    fidelity_state = Column(String(16), nullable=False, default="full")
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
    # M2b（论文 2609.37559 APM-Bench）：零命中遥测（仅元数据）
    miss = Column(Boolean, nullable=False, default=False)
    providers_queried = Column(Text, nullable=True)  # JSON list
    # W2-3（2026-10-23）：injection_min_relevance 门槛拒绝面（shadow 可观测，
    # 误杀审计口径见 memory_baseline_service.quarantine_stats）
    rejects_json = Column(Text, nullable=True)  # JSON list
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
    # 断点续跑快照（JSON
    # {version,cursor,messages,budget_used,elapsed,updated_at}）。有值时
    # 启动恢复置 resumable 并从 cursor 续跑；无值维持旧行为（标 failed）。
    checkpoint = Column(Text, nullable=True)
    worker_id = Column(String(36), nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── separate_v1 execution mode (voice delegation, 2026-11-04) ─────────
    # NULL execution_mode = legacy behavior (conversation_id owns history,
    # result write-back and notification origin). separate_v1 keeps C1 and C2
    # permanently separate and freezes its execution config at submission.
    source_conversation_id = Column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    source_message_id = Column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    execution_conversation_id = Column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    goal_message_id = Column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    execution_mode = Column(String(20), nullable=True)   # NULL=legacy; separate_v1
    origin_kind = Column(String(20), nullable=True)      # voice / chat / scheduled
    execution_config = Column(Text, nullable=True)       # JSON Execution Manifest
    idempotency_key = Column(String(128), nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    attempt_id = Column(String(36), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    cancel_requested_at = Column(DateTime, nullable=True)
    status_version = Column(Integer, nullable=False, default=0)
    output_message_id = Column(String(36), nullable=True)
    reasoning_status = Column(String(20), nullable=True)  # requested/fulfilled/degraded/unavailable/disabled
    runtime_meta = Column(Text, nullable=True)
    usage_meta = Column(Text, nullable=True)

    # ── Wave 2 (2026-11-04): waiting states / worktree / merge ────────────
    # waiting_reason = JSON {kind, question, tool_name, action_fingerprint,
    # expires_at}; only meaningful while status is waiting_input/auth/budget.
    waiting_reason = Column(Text, nullable=True)
    workspace_mode = Column(String(20), nullable=True)   # scratch | worktree
    workspace_meta = Column(Text, nullable=True)         # JSON worktree/branch/commit
    merge_status = Column(String(20), nullable=True)     # none|merged|conflict
    last_attempt_started_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "uq_agent_tasks_user_idempotency", "user_id", "idempotency_key",
            unique=True,
        ),
        Index(
            "uq_agent_tasks_execution_conversation", "execution_conversation_id",
            unique=True,
        ),
    )

    parent = relationship("AgentTask", remote_side="AgentTask.id", backref="subtasks")


class VoiceNotificationOutbox(Base):
    """Durable completion notices for voice-originated background tasks.

    A row is written even when no WebSocket is active. `pending` means the
    notice still needs an audible delivery attempt; only a client playback
    drain advances it to `delivered` (enqueue/sent alone is not delivery).
    """
    __tablename__ = "voice_notification_outbox"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # task_id is nullable + SET NULL: the 7-day AgentTask retention cleanup must
    # never cascade-delete a pending notice. logical_task_id stays addressable.
    task_id = Column(String(36), ForeignKey("agent_tasks.id", ondelete="SET NULL"), nullable=True)
    logical_task_id = Column(String(36), nullable=True)
    recipient_user_id = Column(String(36), nullable=True, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # conversation_id = delivery conversation (legacy contract). New code also
    # records the origin C1 and the long-lived execution C2.
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True)
    origin_conversation_id = Column(String(36), nullable=True)
    execution_conversation_id = Column(String(36), nullable=True, index=True)
    output_message_id = Column(String(36), nullable=True)
    event_type = Column(String(32), nullable=False, default="terminal")
    status_version = Column(Integer, nullable=False, default=0)
    delivery_channel = Column(String(16), nullable=True, default="voice")
    delivery_attempt_id = Column(String(36), nullable=True)
    delivery_lease_owner = Column(String(64), nullable=True)
    delivery_lease_expires_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)
    status = Column(String(16), nullable=False, default="pending")
    task_status = Column(String(16), nullable=False)
    payload = Column(Text, nullable=False, default="{}")
    attempt_count = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index(
            "uq_voice_notification_outbox_event",
            "recipient_user_id", "logical_task_id", "status_version", "event_type",
            unique=True,
        ),
    )


class AgentTaskPermit(Base):
    """One-time, task-scoped act-gate permit (Wave 2, P1).

    Created only by an explicit user action (Web approval or a real voice
    turn). The worker consumes it in the act-gate approval callback; replay or
    expiry fails closed. The permit never grants blanket authority: it binds
    task + tool + canonical action fingerprint.
    """
    __tablename__ = "agent_task_permits"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(36), ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tool_name = Column(String(120), nullable=False)
    action_fingerprint = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_agent_task_permits_task", "task_id", "consumed_at"),
    )


class AgentTaskSummary(Base):
    """Cached tool-less summary keyed by (logical_task_id, result_hash)."""
    __tablename__ = "agent_task_summaries"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    logical_task_id = Column(String(36), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    result_hash = Column(String(64), nullable=False)
    part_index = Column(Integer, nullable=False, default=0)
    summary = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "logical_task_id", "result_hash", "part_index",
            name="uq_agent_task_summary_hash"),
    )


class DurableJob(Base):
    """持久作业（durable job runner）的作业句柄。

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
    """副作用台账：幂等键含 call_id，重放同 call 命中即跳过（failed 允许重试）。

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
    # W6（2026-10-06 三波）：导出内容指纹——同内容命中即复用产物零渲染（D-16）
    content_hash = Column(String(64), nullable=True, index=True)
    # 挂账清理波（2026-10-07）：尾注渲染开关（尊重 UI 开关默认开；D-18 全链穿线）
    endnote_enabled = Column(Boolean, nullable=False, default=True, server_default="TRUE")
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


class SyncEvent(Base):
    """append-only 同步变更日志（sync 波 2026-09-26 自 weave-thinker-client 上游；Joplin delta 模式）。

    seq 自增即客户端游标；payload=全行快照 JSON（delete 为 None）。由
    app/services/sync_capture.py 的 mapper 事件在同事务内 connection.execute
    写入；delta 端点按 (entity_type, entity_id) 窗口内压缩（四规则）。
    捕获域=conversations/messages/notes/notebooks/assistants
    （+memory_enabled 时 memory_episodes/memory_concepts）。"""
    __tablename__ = "sync_events"

    seq = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    entity_type = Column(String(40), nullable=False)
    entity_id = Column(String(36), nullable=False)
    op = Column(String(10), nullable=False)  # create | update | delete
    payload = Column(JSONB, nullable=True)
    origin_device = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_sync_events_user_seq", "user_id", "seq"),
    )


class SyncTombstone(Base):
    """删除墓碑（业务表全硬删除，同步必须显式记录删除事实）。

    `payload`（W9 项9，UPSTREAM_TODO_20261007）：删除载荷——当前仅
    user_model_providers 写自然键 `{kind, provider}`（两端独立建行同自然键 id
    不同，墓碑回放时 apply_change 需 payload 才能定位归并行）；其余类型 NULL。"""
    __tablename__ = "sync_tombstones"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    entity_type = Column(String(40), nullable=False)
    entity_id = Column(String(36), nullable=False)
    deleted_at = Column(DateTime, default=datetime.utcnow)
    # none_as_null=True（W9 复审 A4）：空载荷存 SQL NULL（而非 JSON null 字符串），
    # 与文档「其余保持 NULL」及迁移/审计的 IS NULL 语义一致。
    payload = Column(JSONB(none_as_null=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "entity_type", "entity_id", name="uq_sync_tombstone"),
    )


class SyncDevice(Base):
    """设备注册/撤销（delta 携带 device_id 时抑制 origin 回显；撤销断写+设备级读）。"""
    __tablename__ = "sync_devices"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=True)
    platform = Column(String(40), nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ClientExecutorDevice(Base):
    """W16 设备执行模式：可被云端 agent 驱动执行工具的客户端设备注册表。

    与 SyncDevice（sync 红线，两仓 byte-identical）刻意独立——本表只服务
    /api/device/ws 工具执行通道（docs/PLAN_device_execution_mode.md §6.1）。
    token 仅存 sha256 hash；撤销即踢线拒新调用。"""
    __tablename__ = "client_executor_devices"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id = Column(String(64), nullable=False, unique=True)
    name = Column(String(255), nullable=True)
    platform = Column(String(40), nullable=True)
    token_hash = Column(String(64), nullable=False)
    capabilities = Column(JSON, nullable=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ClientExecutorDeviceTombstone(Base):
    """设备墓碑（2026-10-12 遗留清偿）：revoke=删行后 device_id 不裸放——仅原属主
    可认领（重配对连续性），他户认领拒绝（防抢占 DoS/劫持纵深第二道）。"""
    __tablename__ = "client_executor_device_tombstones"

    device_id = Column(String(64), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    deleted_at = Column(DateTime, default=datetime.utcnow)


class ClientExecAudit(Base):
    """W19 P3：远端执行审计——每次设备工具调用一条（ok/err/duration），
    用户维度查询入口 GET /api/devices/executions。"""
    __tablename__ = "client_exec_audit"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id = Column(String(64), nullable=False)
    conversation_id = Column(String(36), nullable=True)
    tool = Column(String(64), nullable=False)
    ok = Column(Boolean, default=False)
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class SyncBlob(Base):
    """内容寻址文件元数据（workspace 文件同步底座；sha256 即天然去重键）。

    复合主键 (sha256, user_id)：同一内容跨用户各持一行（越权 404 语义），
    底层文件按 sha256 全局只存一份。"""
    __tablename__ = "sync_blobs"

    sha256 = Column(String(64), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    size = Column(BigInteger, nullable=False, default=0)
    path = Column(String(512), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        PrimaryKeyConstraint("sha256", "user_id", name="pk_sync_blobs"),
    )


class SyncState(Base):
    """客户端同步游标/云账号绑定（每本地用户一行；per-user 绑定波扩列）。

    push_cursor=已推送到远端的最大本地 seq（本地 sync_events 即出站队列）；
    pull_cursor=已从远端应用的最大远端 seq。device_id 首周期生成并持久。
    server_url/cloud_username/cloud_password/enabled = 该用户的云端绑定。"""
    __tablename__ = "sync_state"

    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    device_id = Column(String(64), nullable=False)
    push_cursor = Column(BigInteger, default=0)
    pull_cursor = Column(BigInteger, default=0)
    last_sync_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    server_url = Column(String(512), nullable=True)
    cloud_username = Column(String(255), nullable=True)
    cloud_password = Column(Text, nullable=True)  # 本机同盒威胁模型（D-13，成文）
    sync_enabled = Column(Boolean, default=False)
    tls_verify = Column(Boolean, default=True)  # 自签内网服务器置 false（D-15）


class SyncPruneWatermark(Base):
    """per-user 裁剪水位（W8 8a，UPSTREAM_TODO_20261007）：该用户被 prune 删除的
    最大事件 seq。

    缺口信号修正（D-w8-1）：delta 的 `resync_required` 判定引入本表——零事件用户
    无水位不误报（原 `min_seq is None` 恒真致 resync 风暴）；日志被全部裁剪的
    滞后客户端（cursor < 水位）仍收到 `resync_required=true`（「绝不静默返回
    不完整数据」）。水位只增不减（max 合并），由
    `sync_retention.prune_sync_history` 落库。"""

    __tablename__ = "sync_prune_watermarks"

    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    max_deleted_seq = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)


class SyncConflict(Base):
    """LWW 败方留痕：pull 时本地更新被远端覆盖 / push 时远端拒收。"""

    __tablename__ = "sync_conflicts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    entity_type = Column(String(40), nullable=False)
    entity_id = Column(String(36), nullable=False)
    direction = Column(String(10), nullable=False)  # push | pull
    reason = Column(String(40), nullable=False)  # lww_remote_newer | lww_local_newer | skipped_remote | apply_error
    local_updated_at = Column(DateTime, nullable=True)
    remote_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationPinnedConstraint(Base):
    """W1-2（记忆/长程 v2，2026-10-23）：压缩后原样重放的用户约束（P1 来源先于
    召回 / B1 约束固定信任门）。仅 source_role=user + provenance≥trusted 可入；
    pin 前过 injection scan；每会话上限 [memory.constraints] max_pins=20；
    revoked_at 非空 = 已撤销（可逆，P2），UI 可见可撤销（P5）。按 user_id 擦除。"""

    __tablename__ = "conversation_pinned_constraints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text, nullable=False)
    source_message_id = Column(String(36), nullable=True)
    source_role = Column(String(20), nullable=False, default="user")
    provenance = Column(String(20), nullable=False, default="trusted")
    scope = Column(String(20), nullable=False, default="conversation")
    pinned_by = Column(String(20), nullable=False, default="user")
    created_at = Column(DateTime, default=datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(String(20), nullable=True)


class DeletionManifest(Base):
    """W0-4（记忆/长程 v2，2026-10-23）：遗忘请求清单 + 残余证据（B4 遗忘闭环）。
    D1=a memory-only：删记忆层，保留对话/笔记（scope_json 明示保留面）。
    保留审计（residual_json 只存命中位置/计数，不含正文）。按 user_id 擦除。"""

    __tablename__ = "deletion_manifests"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_id = Column(String(64), nullable=False)
    scope_json = Column(JSONB, nullable=False, default=dict)
    status = Column(String(20), nullable=False, default="pending")
    residual_json = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class DreamRepairReport(Base):
    """W3-3（记忆/长程 v2，2026-10-23）：梦境 probe→hit/miss→只修 miss 的可验证
    记录（hit@k before/after + cost_tokens，P6 成本是验收项）。默认关，14 天无
    正收益自动回退方案 B（W3-4）。按 user_id 擦除。"""

    __tablename__ = "dream_repair_reports"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    run_date = Column(String(10), nullable=False)
    probe_count = Column(Integer, nullable=False, default=0)
    hit_count = Column(Integer, nullable=False, default=0)
    miss_count = Column(Integer, nullable=False, default=0)
    repairs_json = Column(JSONB, nullable=False, default=list)
    hit_at_k_before = Column(Float, nullable=True)
    hit_at_k_after = Column(Float, nullable=True)
    cost_tokens = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class StepCheckpoint(Base):
    """W4-1（记忆/长程 v2，2026-10-23）：死磕步骤对齐回退三元组
    (step_id, context_cursor, snapshot_id)（AgentRewind 2608.14380）。随会话删。"""

    __tablename__ = "step_checkpoints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    deathmatch_id = Column(String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    step_id = Column(String(64), nullable=False)
    context_cursor = Column(Integer, nullable=False, default=0)
    snapshot_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MemoryEvalRun(Base):
    """W0-2（记忆/长程 v2，2026-10-23）：离线评测结果落库（测试/影子库口径，
    不进生产判定；确定性 gold 为主，judge 只辅助——REFLECT <55%）。"""

    __tablename__ = "memory_eval_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String(64), nullable=False)
    suite = Column(String(64), nullable=False)
    arm = Column(String(64), nullable=False)
    metrics_json = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaintState(Base):
    """P1a TMA-NM: monotonic per-conversation taint (M2)."""

    __tablename__ = "taint_states"

    conversation_id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=False, default="", index=True)
    max_taint = Column(String(20), nullable=False, default="trusted")
    sources_json = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArtifactTaint(Base):
    """P1d: per-artifact provenance for notes/session-derived memory (M2)."""

    __tablename__ = "artifact_taint"

    user_id = Column(String(36), primary_key=True)
    artifact_type = Column(String(32), primary_key=True)
    artifact_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False, default="")
    taint = Column(String(20), nullable=False, default="untrusted")
    origin = Column(String(40), nullable=False, default="legacy_untrusted")
    parent_ids = Column(JSONB, nullable=False, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MemoryVerdict(Base):
    """P1a TMA-NM: append-only verdict hash chain (M4)."""

    __tablename__ = "memory_verdicts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # tenant_id/conversation_id are covered by the composite indexes below;
    # single-column indexes on their leading column would be redundant.
    tenant_id = Column(String(64), nullable=False, default="")
    seq = Column(Integer, nullable=False, default=0)
    user_id = Column(String(36), nullable=False, default="", index=True)
    conversation_id = Column(String(36), nullable=False, default="")
    action_fingerprint = Column(String(64), nullable=False, default="", index=True)
    tool_name = Column(String(80), nullable=False, default="")
    decision = Column(String(20), nullable=False)
    reason = Column(Text, nullable=False, default="")
    taint = Column(String(20), nullable=False, default="untrusted")
    corroborators = Column(JSON, nullable=False, default=list)
    mode = Column(String(20), nullable=False, default="shadow")
    blocked = Column(Boolean, nullable=False, default=False)
    created_at_epoch = Column(BigInteger, nullable=False, default=0)
    prev_hash = Column(String(64), nullable=False, default="")
    entry_hash = Column(String(64), nullable=False, unique=True)
    sig = Column(String(64), nullable=False, default="")
    key_id = Column(String(20), nullable=False, default="v1")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "seq", name="uq_memory_verdict_tenant_seq"),
    )


class MemoryVerdictHead(Base):
    """P1a TMA-NM: chain head serialised with FOR UPDATE on append."""

    __tablename__ = "memory_verdict_heads"

    tenant_id = Column(String(64), primary_key=True)
    last_hash = Column(String(64), nullable=False, default="")
    last_seq = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MemoryVerdictRedacted(Base):
    """P2: hash-only tombstone for a pruned verdict (sensitive payload removed)."""

    __tablename__ = "memory_verdict_redacted"

    tenant_id = Column(String(64), primary_key=True, default="")
    seq = Column(Integer, primary_key=True, default=0)
    prev_hash = Column(String(64), nullable=False, default="")
    entry_hash = Column(String(64), nullable=False, default="")
    key_id = Column(String(20), nullable=False, default="v1")


class MemoryVerdictRetention(Base):
    """P2: signed, monotonic boundary over the pruned verdict prefix."""

    __tablename__ = "memory_verdict_retention"

    tenant_id = Column(String(64), primary_key=True)
    pruned_through_seq = Column(Integer, nullable=False, default=0)
    boundary_hash = Column(String(64), nullable=False, default="")
    boundary_prev_hash = Column(String(64), nullable=False, default="")
    sig = Column(String(64), nullable=False, default="")
    key_id = Column(String(20), nullable=False, default="v1")
    updated_at_epoch = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class InjectionQuarantine(Base):
    """P3: hash-only quarantine telemetry for detector hits (no raw text)."""

    __tablename__ = "injection_quarantine"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(64), nullable=False, default="", index=True)
    user_id = Column(String(36), nullable=False, default="")
    conversation_id = Column(String(36), nullable=False, default="")
    channel = Column(String(40), nullable=False, default="")
    tool_name = Column(String(80), nullable=False, default="")
    origin = Column(String(40), nullable=False, default="")
    content_hash = Column(String(64), nullable=False, default="", index=True)
    detector = Column(String(80), nullable=False, default="")
    score = Column(Float, nullable=False, default=0.0)
    reason = Column(Text, nullable=False, default="")
    mode = Column(String(20), nullable=False, default="shadow")
    created_at_epoch = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class MemoryActionToken(Base):
    """P1d: one-time cryptographic user-authorisation token."""

    __tablename__ = "memory_action_tokens"

    token_id = Column(String(36), primary_key=True)
    tenant_id = Column(String(64), nullable=False, default="")
    user_id = Column(String(36), nullable=False, default="")
    conversation_id = Column(String(36), nullable=False, default="")
    action_fingerprint = Column(String(64), nullable=False, default="")
    tool_name = Column(String(80), nullable=False, default="")
    nonce = Column(String(64), nullable=False, unique=True)
    issued_at_epoch = Column(BigInteger, nullable=False, default=0)
    expires_at_epoch = Column(BigInteger, nullable=False, default=0)
    used_at_epoch = Column(BigInteger, nullable=False, default=0)
    sig = Column(String(64), nullable=False, default="")


class MemoryVerdictCheckpoint(Base):
    """P1c TMA-NM: signed Merkle checkpoint over a verdict-chain prefix."""

    __tablename__ = "memory_verdict_checkpoints"

    tenant_id = Column(String(64), primary_key=True)
    seq = Column(Integer, primary_key=True)
    merkle_root = Column(String(64), nullable=False, default="")
    last_hash = Column(String(64), nullable=False, default="")
    sig = Column(String(64), nullable=False, default="")
    key_id = Column(String(20), nullable=False, default="v1")
    witness_required = Column(Boolean, nullable=False, default=False)
    created_at_epoch = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()