# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import re

from sqlalchemy import text

logger = logging.getLogger(__name__)

_EXT_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


STARTUP_MIGRATIONS = [
    ("assistants_use_custom_model", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS use_custom_model BOOLEAN DEFAULT FALSE"),
    ("assistants_custom_api_url", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS custom_api_url VARCHAR(500)"),
    ("assistants_custom_api_key", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS custom_api_key VARCHAR(500)"),
    ("assistants_custom_model_name", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS custom_model_name VARCHAR(200)"),
    ("messages_reasoning_content", "ALTER TABLE messages ADD COLUMN IF NOT EXISTS reasoning_content TEXT"),
    # PHASE 2B: structured tool-call history. Stores the OpenAI-style
    # tool_calls array emitted by the model for this assistant message,
    # paired with tool_results to enable structured replay so the model
    # sees prior actions instead of fuzzy narrative text.
    ("messages_tool_calls", "ALTER TABLE messages ADD COLUMN IF NOT EXISTS tool_calls TEXT"),
    # PHASE 3: per-assistant override of the LLM used for tool-calling
    # iterations. When NULL the main client is reused with thinking forced
    # off; when populated, iterations get a separate cheaper client so the
    # main model (often a reasoner) only runs during final synthesis.
    ("assistants_subtask_custom_api_url", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS subtask_custom_api_url VARCHAR(500)"),
    ("assistants_subtask_custom_api_key", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS subtask_custom_api_key VARCHAR(500)"),
    ("assistants_subtask_custom_model_name", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS subtask_custom_model_name VARCHAR(200)"),
    ("assistants_subtask_provider_type", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS subtask_provider_type VARCHAR(20)"),
    ("assistants_subtask_extra_body", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS subtask_extra_body TEXT"),
    ("assistants_use_subtask_model", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS use_subtask_model BOOLEAN DEFAULT FALSE"),

    ("assistants_thinking_budget", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS thinking_budget INTEGER"),
    # Qwen3.8(Local) provider sampling params: non-thinking set (min_p,
    # repetition_penalty join the pre-existing temperature/top_p/top_k/
    # presence_penalty) + thinking-mode set + preserve_thinking.
    ("assistants_min_p", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS min_p FLOAT"),
    ("assistants_repetition_penalty", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS repetition_penalty FLOAT"),
    ("assistants_thinking_temperature", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS thinking_temperature FLOAT"),
    ("assistants_thinking_top_p", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS thinking_top_p FLOAT"),
    ("assistants_thinking_top_k", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS thinking_top_k INTEGER"),
    ("assistants_thinking_min_p", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS thinking_min_p FLOAT"),
    ("assistants_thinking_presence_penalty", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS thinking_presence_penalty FLOAT"),
    ("assistants_thinking_repetition_penalty", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS thinking_repetition_penalty FLOAT"),
    ("assistants_preserve_thinking", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS preserve_thinking BOOLEAN DEFAULT TRUE"),
    # Background task system columns
    ("agent_tasks_title", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS title VARCHAR(255)"),
    ("agent_tasks_assistant_id", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS assistant_id VARCHAR(36)"),
    ("agent_tasks_progress", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS progress FLOAT DEFAULT 0"),
    ("agent_tasks_iterations_done", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS iterations_done INTEGER DEFAULT 0"),
    ("agent_tasks_iterations_max", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS iterations_max INTEGER DEFAULT 30"),
    ("agent_tasks_elapsed_seconds", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS elapsed_seconds FLOAT DEFAULT 0"),
    ("agent_tasks_started_at", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMP"),
    ("agent_tasks_completed_at", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP"),
    ("agent_tasks_output_conversation_id", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS output_conversation_id VARCHAR(36)"),
    ("agent_tasks_output_note_id", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS output_note_id VARCHAR(36)"),
    ("agent_tasks_search_results", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS search_results TEXT"),
    ("agent_tasks_browser_results", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS browser_results TEXT"),
    ("agent_tasks_intermediate_steps", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS intermediate_steps TEXT"),
    # Scheduled task conversation persistence
    ("scheduled_tasks_conversation_id", "ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(36)"),
    # FTS support for session_search tool
    ("messages_search_vector", "ALTER TABLE messages ADD COLUMN IF NOT EXISTS search_vector tsvector"),
    ("idx_messages_search_vector", "CREATE INDEX IF NOT EXISTS idx_messages_search_vector ON messages USING GIN(search_vector)"),
    # Conversation-scoped message queries (conversation search snippets, message
    # listing) were doing full-table Seq Scans — every per-conversation lookup
    # scanned all 4000+ rows. With 100+ matched conversations a search turn
    # cost 13-43s (measured 2026-08-07).
    ("idx_messages_conversation_id", "CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id)"),
    ("messages_search_vector_function", """CREATE OR REPLACE FUNCTION messages_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', COALESCE(NEW.content, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(NEW.reasoning_content, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql"""),
    ("messages_search_vector_trigger", """DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'messages_search_vector_trigger') THEN
        CREATE TRIGGER messages_search_vector_trigger
            BEFORE INSERT OR UPDATE ON messages
            FOR EACH ROW EXECUTE FUNCTION messages_search_vector_update();
    END IF;
END;
$$"""),
    ("assistants_provider_type", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS provider_type VARCHAR(20) DEFAULT 'deepseek'"),
    ("assistants_provider_type_backfill", "UPDATE assistants SET provider_type = 'deepseek' WHERE provider_type IS NULL"),
    ("assistants_extra_body", "ALTER TABLE assistants ADD COLUMN IF NOT EXISTS extra_body TEXT"),
    ("create_export_tasks", """CREATE TABLE IF NOT EXISTS export_tasks (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) REFERENCES users(id) ON DELETE CASCADE NOT NULL,
        task_type VARCHAR(20) NOT NULL DEFAULT 'single',
        format VARCHAR(10) NOT NULL DEFAULT 'pdf',
        note_id VARCHAR(36),
        note_ids TEXT,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        progress FLOAT DEFAULT 0.0,
        file_path VARCHAR(500),
        filename VARCHAR(255),
        error TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        started_at TIMESTAMP,
        completed_at TIMESTAMP
    )"""),
    # Conversation groups
    ("create_conversation_groups", """CREATE TABLE IF NOT EXISTS conversation_groups (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) REFERENCES users(id) ON DELETE CASCADE NOT NULL,
        assistant_id VARCHAR(36) REFERENCES assistants(id) ON DELETE CASCADE,
        name VARCHAR(100) NOT NULL DEFAULT '新分组',
        color VARCHAR(20) NOT NULL DEFAULT '#3b82f6',
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("conversations_group_id", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS group_id VARCHAR(36) REFERENCES conversation_groups(id) ON DELETE SET NULL"),
    ("conversations_sort_order", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0"),
    ("conversations_sort_order_backfill", "UPDATE conversations SET sort_order = 0 WHERE sort_order IS NULL"),
    # 同族防御:conversation_groups 同列同渠道(NULL 毒化),见 conversations 条目
    ("conversation_groups_sort_order_backfill", "UPDATE conversation_groups SET sort_order = 0 WHERE sort_order IS NULL"),
    ("idx_conversations_group_id", "CREATE INDEX IF NOT EXISTS idx_conversations_group_id ON conversations(group_id)"),
    ("idx_conversation_groups_user", "CREATE INDEX IF NOT EXISTS idx_conversation_groups_user ON conversation_groups(user_id)"),
    ("idx_conversation_groups_assistant", "CREATE INDEX IF NOT EXISTS idx_conversation_groups_assistant ON conversation_groups(assistant_id)"),
    # Deathmatch (死磕) mode columns on conversations
    ("conversations_deathmatch_mode", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_mode BOOLEAN DEFAULT FALSE"),
    ("conversations_deathmatch_goal", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_goal TEXT"),
    ("conversations_deathmatch_status", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_status VARCHAR(20) DEFAULT 'inactive'"),
    ("conversations_deathmatch_turns", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_turns INTEGER DEFAULT 0"),
    ("conversations_deathmatch_max_turns", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_max_turns INTEGER DEFAULT 30"),
    ("conversations_deathmatch_consecutive_failures", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_consecutive_failures INTEGER DEFAULT 0"),
    ("conversations_deathmatch_verdict", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_verdict TEXT"),
    ("conversations_deathmatch_reason", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_reason TEXT"),
    ("conversations_deathmatch_grilling_complete", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_grilling_complete BOOLEAN DEFAULT FALSE"),
    ("conversations_deathmatch_grilling_total", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_grilling_total INTEGER DEFAULT 0"),
    ("conversations_deathmatch_grilling_completed", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_grilling_completed INTEGER DEFAULT 0"),
    ("conversations_deathmatch_grilling_round", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_grilling_round INTEGER DEFAULT 0"),
    ("conversations_deathmatch_grilling_round_total", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_grilling_round_total INTEGER DEFAULT 3"),
    ("conversations_deathmatch_grilling_qa_history", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_grilling_qa_history JSONB DEFAULT '[]'::jsonb"),
    ("conversations_deathmatch_context_summary", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_context_summary TEXT"),
    ("conversations_deathmatch_expected_marker", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_expected_marker TEXT"),
    ("conversations_deathmatch_marker_miss_count", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_marker_miss_count INTEGER DEFAULT 0"),
    ("conversations_deathmatch_compressed_context", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_compressed_context TEXT"),
    # PEVR extension (loop_improve.md Phase 3)
    ("conversations_deathmatch_plan", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_plan JSONB DEFAULT NULL"),
    ("conversations_deathmatch_plan_version", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_plan_version INTEGER DEFAULT 0"),
    ("conversations_deathmatch_reflections", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_reflections JSONB DEFAULT '[]'::jsonb"),
    ("conversations_deathmatch_wall_time_started_at", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_wall_time_started_at TIMESTAMP DEFAULT NULL"),
    ("conversations_deathmatch_max_wall_time_seconds", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_max_wall_time_seconds INTEGER DEFAULT 3600"),
    ("conversations_deathmatch_last_verification_result", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_last_verification_result JSONB DEFAULT NULL"),
    ("conversations_deathmatch_verify_failures", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_verify_failures INTEGER DEFAULT 0"),
    ("conversations_deathmatch_human_gate", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_human_gate TEXT"),
    ("conversations_deathmatch_wall_time_used_seconds", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_wall_time_used_seconds INTEGER DEFAULT 0"),
    ("conversations_deathmatch_bible_draft", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_bible_draft JSONB DEFAULT NULL"),
    ("conversations_deathmatch_subgoals", "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deathmatch_subgoals JSONB DEFAULT NULL"),
    ("create_user_asr_hotwords", """CREATE TABLE IF NOT EXISTS user_asr_hotwords (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) REFERENCES users(id) ON DELETE CASCADE NOT NULL,
        text VARCHAR(120) NOT NULL,
        weight INTEGER NOT NULL DEFAULT 4,
        lang VARCHAR(10),
        dashscope_vocabulary_id VARCHAR(120),
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_user_asr_hotwords_user", "CREATE INDEX IF NOT EXISTS idx_user_asr_hotwords_user ON user_asr_hotwords(user_id)"),
    ("user_asr_hotwords_dashscope_vocabulary_id", "ALTER TABLE user_asr_hotwords ADD COLUMN IF NOT EXISTS dashscope_vocabulary_id VARCHAR(120)"),
    # User skills table
    ("create_user_skills", """CREATE TABLE IF NOT EXISTS user_skills (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) REFERENCES users(id) ON DELETE CASCADE NOT NULL,
        name VARCHAR(100) NOT NULL,
        description TEXT,
        content TEXT NOT NULL,
        is_active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_user_skills_user", "CREATE INDEX IF NOT EXISTS idx_user_skills_user ON user_skills(user_id)"),
    ("idx_user_skills_user_name", "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_skills_user_name ON user_skills(user_id, name)"),
    # Skill files table
    ("create_skill_files", """CREATE TABLE IF NOT EXISTS skill_files (
        id VARCHAR(36) PRIMARY KEY,
        skill_id VARCHAR(36) REFERENCES user_skills(id) ON DELETE CASCADE NOT NULL,
        file_path VARCHAR(500) NOT NULL,
        file_content TEXT NOT NULL,
        file_type VARCHAR(50) NOT NULL,
        is_executable BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_skill_files_skill", "CREATE INDEX IF NOT EXISTS idx_skill_files_skill ON skill_files(skill_id)"),
    # Agent permission settings per user
    ("users_agent_permissions", "ALTER TABLE users ADD COLUMN IF NOT EXISTS agent_permissions TEXT"),
    # Scheduled task retry support
    ("scheduled_tasks_fail_count", "ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS fail_count INTEGER DEFAULT 0"),
    # Phase 5.5: Multi-instance shared state support
    ("create_shared_kv_store", """CREATE TABLE IF NOT EXISTS shared_kv_store (
        key TEXT PRIMARY KEY,
        value BYTEA NOT NULL,
        expires_at DOUBLE PRECISION,
        worker_id TEXT,
        created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
    )"""),
    ("idx_shared_kv_expires", "CREATE INDEX IF NOT EXISTS idx_shared_kv_expires ON shared_kv_store(expires_at)"),
    # Track which worker is handling an agent task
    ("agent_tasks_worker_id", "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS worker_id TEXT"),
    # Worker instance registry for cross-worker health checks
    ("create_worker_instances", """CREATE TABLE IF NOT EXISTS worker_instances (
        id TEXT PRIMARY KEY,
        host TEXT,
        port INTEGER,
        pid INTEGER,
        started_at DOUBLE PRECISION NOT NULL,
        last_heartbeat DOUBLE PRECISION NOT NULL,
        status TEXT DEFAULT 'active',
        metadata TEXT
    )"""),
    # Rename the legacy voice assistant (酬) to 语音助理 for existing installs
    ("assistants_rename_voice_assistant", "UPDATE assistants SET name = '语音助理' WHERE name = '酬'"),
    # ---- Memory & Dreaming v2: 纯列 ALTER（不依赖 pgvector，pgvector 缺失时也应执行）----
    # ALTER user_agent_states: new columns for concept extraction watermarks + management
    ("uas_last_note_processed_at", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS last_note_processed_at TIMESTAMP"),
    ("uas_last_message_processed_at", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS last_message_processed_at TIMESTAMP"),
    ("uas_last_file_memory_processed_at", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS last_file_memory_processed_at TIMESTAMP"),
    ("uas_last_subconscious_scan_at", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS last_subconscious_scan_at TIMESTAMP"),
    ("uas_last_consolidation_at", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS last_consolidation_at TIMESTAMP"),
    ("uas_total_concept_count", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS total_concept_count INTEGER DEFAULT 0"),
    ("uas_total_episode_count", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS total_episode_count INTEGER DEFAULT 0"),
    ("uas_latest_dream_id", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS latest_dream_id VARCHAR(36)"),
    ("uas_metadata_json", "ALTER TABLE user_agent_states ADD COLUMN IF NOT EXISTS metadata_json TEXT"),
    # ALTER agent_dreams: new columns
    ("ad_source_concept_count", "ALTER TABLE agent_dreams ADD COLUMN IF NOT EXISTS source_concept_count INTEGER DEFAULT 0"),
    ("ad_source_cluster_count", "ALTER TABLE agent_dreams ADD COLUMN IF NOT EXISTS source_cluster_count INTEGER DEFAULT 0"),
    ("ad_metadata_json", "ALTER TABLE agent_dreams ADD COLUMN IF NOT EXISTS metadata_json TEXT"),
    ("ad_dream_type", "ALTER TABLE agent_dreams ADD COLUMN IF NOT EXISTS dream_type VARCHAR(20) DEFAULT 'consolidation'"),
    # 2026-08-09 数据迁移：历史 v1 nightly dream 误标为 consolidation 且 metadata 为空
    # （v2 consolidation 恒写 metadata，metadata_json IS NULL 是稳健判别式——
    # 用 source_note_count>0 会漏掉零笔记用户的 v1 行）；改为 nightly + provenance，
    # 注入路径（_get_latest_dream 限定 consolidation）不再读到 v1 内容
    ("ad_mislabeled_v1_nightly", """UPDATE agent_dreams SET dream_type = 'nightly', metadata_json = '{"source":"v1_nightly"}' WHERE dream_type = 'consolidation' AND (metadata_json IS NULL OR metadata_json = '')"""),
    # web_search_results: 每次联网检索命中的结果逐条落库（溯源/记忆用）。
    # 与 memory v2 无关，必须置于 pgvector_extension 之前——否则 pgvector 缺失时
    # 该块随 memory 迁移整体跳过（见 run_startup_migrations 的 mem_start 区间）。
    ("create_web_search_results", """CREATE TABLE IF NOT EXISTS web_search_results (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL,
        conversation_id VARCHAR(36) REFERENCES conversations(id) ON DELETE SET NULL,
        query TEXT NOT NULL,
        provider VARCHAR(20) NOT NULL,
        result_rank INTEGER NOT NULL DEFAULT 0,
        title TEXT,
        url TEXT NOT NULL,
        snippet TEXT,
        published_date VARCHAR(10),
        extra JSONB,
        created_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_wsr_url", "CREATE INDEX IF NOT EXISTS idx_wsr_url ON web_search_results(url)"),
    ("idx_wsr_query", "CREATE INDEX IF NOT EXISTS idx_wsr_query ON web_search_results(query)"),
    ("idx_wsr_created_at", "CREATE INDEX IF NOT EXISTS idx_wsr_created_at ON web_search_results(created_at DESC)"),
    ("idx_wsr_user_ts", "CREATE INDEX IF NOT EXISTS idx_wsr_user_ts ON web_search_results(user_id, created_at DESC)"),
    # ---- Memory & Dreaming v2: pgvector + schema（以下块依赖 pgvector，缺失时整体跳过）----
    ("pgvector_extension", "CREATE EXTENSION IF NOT EXISTS vector"),
    # memory_concepts
    ("create_memory_concepts", """CREATE TABLE IF NOT EXISTS memory_concepts (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        canonical_name VARCHAR(500) NOT NULL,
        description_short VARCHAR(80) NOT NULL,
        description_full TEXT,
        aliases TEXT,
        weight FLOAT NOT NULL DEFAULT 0.5,
        stability FLOAT NOT NULL DEFAULT 14.0,
        source_trust VARCHAR(20) NOT NULL DEFAULT 'user_stated',
        memory_type VARCHAR(20) NOT NULL DEFAULT 'semantic',
        activation_strength FLOAT NOT NULL DEFAULT 1.0,
        recurrence_count INTEGER NOT NULL DEFAULT 0,
        last_recurrence_at TIMESTAMP,
        hot_forget_count INTEGER NOT NULL DEFAULT 0,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        source_type VARCHAR(50) NOT NULL DEFAULT 'extracted',
        source_raw_ids TEXT,
        source_unit_ids TEXT,
        needs_review BOOLEAN NOT NULL DEFAULT FALSE,
        metadata_json TEXT,
        last_recalled_at TIMESTAMP,
        valid_from TIMESTAMP DEFAULT NOW(),
        valid_to TIMESTAMP,
        superseded_by VARCHAR(36),
        embedding vector(1536),
        embedding_updated_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_concepts_user_status", "CREATE INDEX IF NOT EXISTS idx_concepts_user_status ON memory_concepts(user_id, status)"),
    ("idx_concepts_user_weight", "CREATE INDEX IF NOT EXISTS idx_concepts_user_weight ON memory_concepts(user_id, weight DESC)"),
    ("idx_concepts_user_recalled", "CREATE INDEX IF NOT EXISTS idx_concepts_user_recalled ON memory_concepts(user_id, last_recalled_at DESC)"),
    ("idx_concepts_needs_review", "CREATE INDEX IF NOT EXISTS idx_concepts_needs_review ON memory_concepts(user_id, needs_review) WHERE needs_review = TRUE"),
    ("idx_concepts_embedding", "CREATE INDEX IF NOT EXISTS idx_concepts_embedding ON memory_concepts USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"),
    # memory_clusters
    ("create_memory_clusters", """CREATE TABLE IF NOT EXISTS memory_clusters (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name VARCHAR(255) NOT NULL,
        summary TEXT,
        weight FLOAT NOT NULL DEFAULT 0.5,
        embedding vector(1536),
        member_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_clusters_user", "CREATE INDEX IF NOT EXISTS idx_clusters_user ON memory_clusters(user_id)"),
    # concept_cluster_members
    ("create_concept_cluster_members", """CREATE TABLE IF NOT EXISTS concept_cluster_members (
        concept_id VARCHAR(36) NOT NULL REFERENCES memory_concepts(id) ON DELETE CASCADE,
        cluster_id VARCHAR(36) NOT NULL REFERENCES memory_clusters(id) ON DELETE CASCADE,
        PRIMARY KEY (concept_id, cluster_id)
    )"""),
    ("idx_ccm_cluster", "CREATE INDEX IF NOT EXISTS idx_ccm_cluster ON concept_cluster_members(cluster_id)"),
    # concept_relations
    ("create_concept_relations", """CREATE TABLE IF NOT EXISTS concept_relations (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        source_id VARCHAR(36) NOT NULL REFERENCES memory_concepts(id) ON DELETE CASCADE,
        target_id VARCHAR(36) NOT NULL REFERENCES memory_concepts(id) ON DELETE CASCADE,
        relation_type VARCHAR(50) NOT NULL,
        description TEXT,
        weight FLOAT NOT NULL DEFAULT 0.5,
        created_at TIMESTAMP DEFAULT NOW(),
        CHECK (source_id != target_id)
    )"""),
    ("idx_rel_source", "CREATE INDEX IF NOT EXISTS idx_rel_source ON concept_relations(source_id)"),
    ("idx_rel_target", "CREATE INDEX IF NOT EXISTS idx_rel_target ON concept_relations(target_id)"),
    ("idx_rel_user", "CREATE INDEX IF NOT EXISTS idx_rel_user ON concept_relations(user_id)"),
    # memory_clarifications
    ("create_memory_clarifications", """CREATE TABLE IF NOT EXISTS memory_clarifications (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        conversation_id VARCHAR(36),
        message_id VARCHAR(36),
        original_text TEXT NOT NULL,
        correction_type VARCHAR(30) NOT NULL,
        affected_concept_ids TEXT,
        new_description TEXT,
        confidence FLOAT NOT NULL DEFAULT 0.0,
        applied BOOLEAN DEFAULT FALSE,
        applied_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_clar_user", "CREATE INDEX IF NOT EXISTS idx_clar_user ON memory_clarifications(user_id)"),
    ("idx_clar_applied", "CREATE INDEX IF NOT EXISTS idx_clar_applied ON memory_clarifications(user_id, applied)"),
    # subconscious_log
    ("create_subconscious_log", """CREATE TABLE IF NOT EXISTS subconscious_log (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        unit_kind VARCHAR(20) NOT NULL DEFAULT 'message',
        raw_text TEXT NOT NULL,
        source_ids TEXT NOT NULL,
        embedding vector(1536),
        promoted BOOLEAN NOT NULL DEFAULT FALSE,
        promoted_at TIMESTAMP,
        recurrence_count INTEGER NOT NULL DEFAULT 0,
        last_recurrence_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_sub_user_created", "CREATE INDEX IF NOT EXISTS idx_sub_user_created ON subconscious_log(user_id, created_at DESC)"),
    ("idx_sub_user_unpromoted", "CREATE INDEX IF NOT EXISTS idx_sub_user_unpromoted ON subconscious_log(user_id, promoted) WHERE promoted = FALSE"),
    ("idx_sub_embedding", "CREATE INDEX IF NOT EXISTS idx_sub_embedding ON subconscious_log USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"),
    # 2026-08-10 队头阻塞修复：单元作为批次头的扫描次数预算（防最旧 50 条永不晋升挡住新单元）
    ("add_subconscious_recurrence_scan_count", "ALTER TABLE subconscious_log ADD COLUMN IF NOT EXISTS recurrence_scan_count INTEGER NOT NULL DEFAULT 0"),
    # 2026-08-10 权重语义修正：importance = 持久重要性（与 weight 热度正交），
    # 创建时由提取 LLM 判定/规则兜底；展示与检索排序以 importance 主导
    ("add_concept_importance", "ALTER TABLE memory_concepts ADD COLUMN IF NOT EXISTS importance DOUBLE PRECISION NOT NULL DEFAULT 0.5"),
    # 2026-08-10 F1 修复：独立"已评估"标记（importance=0.5 是合法 verdict，不能当哨兵）
    ("add_concept_importance_evaluated", "ALTER TABLE memory_concepts ADD COLUMN IF NOT EXISTS importance_evaluated BOOLEAN NOT NULL DEFAULT FALSE"),
    # memory_episodes
    ("create_memory_episodes", """CREATE TABLE IF NOT EXISTS memory_episodes (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        narrative TEXT NOT NULL,
        source_unit_ids TEXT NOT NULL,
        source_concept_ids TEXT,
        valid_from TIMESTAMP DEFAULT NOW(),
        valid_to TIMESTAMP,
        superseded_by VARCHAR(36),
        embedding vector(1536),
        merged_from VARCHAR(36),
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_epi_user_created", "CREATE INDEX IF NOT EXISTS idx_epi_user_created ON memory_episodes(user_id, valid_from DESC)"),
    ("idx_epi_embedding", "CREATE INDEX IF NOT EXISTS idx_epi_embedding ON memory_episodes USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"),
    # memory_concepts: 防御性 ALTER（老库 CREATE 无此列时补齐；新库 CREATE 已含则幂等跳过）
    ("mc_last_recurrence_at", "ALTER TABLE memory_concepts ADD COLUMN IF NOT EXISTS last_recurrence_at TIMESTAMP"),
    # memory_episodes: recall tracking (M&D §5.3.1a-2)
    ("me_last_recalled_at", "ALTER TABLE memory_episodes ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMP"),
    # memory_episodes: 来源标记（§8.5.5 回滚按 source_type='migration' 精确删除）
    ("me_source_type", "ALTER TABLE memory_episodes ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) DEFAULT 'extracted'"),
    # memory_concepts: 衰减写回时间戳（2026-08-16）——run_weight_decay 写回
    # weight 时记录 anchor，dream/active-dreaming 的有效权重用"距上次写回/召回"
    # 的残差衰减，避免同一事务内二次全量衰减（A4.9 审查 C1/C2）
    ("mc_weight_decayed_at", "ALTER TABLE memory_concepts ADD COLUMN IF NOT EXISTS weight_decayed_at TIMESTAMP"),
    ("idx_epi_source_type", "CREATE INDEX IF NOT EXISTS idx_epi_source_type ON memory_episodes(user_id, source_type)"),
    # memory_llm_calls
    ("create_memory_llm_calls", """CREATE TABLE IF NOT EXISTS memory_llm_calls (
        id VARCHAR(36) PRIMARY KEY,
        user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind VARCHAR(50) NOT NULL,
        model VARCHAR(100),
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW()
    )"""),
    ("idx_mlc_user_ts", "CREATE INDEX IF NOT EXISTS idx_mlc_user_ts ON memory_llm_calls(user_id, created_at DESC)"),
    # UI 偏好（皮肤选择等）：JSON 字符串，见 app/api/skins.py
    ("users_ui_preferences", "ALTER TABLE users ADD COLUMN IF NOT EXISTS ui_preferences TEXT"),
]


# §9.5 pgvector 缺失降级：启动探测结果（run_startup_migrations 期间写入）。
# False 时 memory v2 迁移整体跳过、init_db 的 create_all 排除 memory 表、
# main.py 强制 memory.enabled=false —— 服务继续以旧记忆方案运行，不崩溃。
PGVECTOR_AVAILABLE = True

_MEMORY_MIGRATION_START = "pgvector_extension"

_VECTOR_TABLES = [
    ("subconscious_log", "embedding", "idx_sub_embedding"),
    ("memory_concepts", "embedding", "idx_concepts_embedding"),
    ("memory_episodes", "embedding", "idx_epi_embedding"),
    ("memory_clusters", "embedding", None),
]


async def probe_pgvector(conn, extension: str = "vector") -> bool:
    """§9.5 启动探测：pgvector 扩展是否可用（已安装或可创建）。

    CREATE EXTENSION 失败（无权限/未安装）会被 PG 拒绝并使事务进入 aborted
    状态——savepoint 隔离保证探测失败不毒化调用方事务（init_db 后续
    create_all 依赖同一事务）。
    """
    if not _EXT_IDENT_RE.fullmatch(extension):
        logger.error("probe_pgvector: 非法扩展名标识符 %r", extension)
        return False
    try:
        async with conn.begin_nested():
            await conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))
    except Exception:
        return False
    try:
        r = await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = :ext"),
                               {"ext": extension})
    except Exception:
        # SELECT 失败属异常环境（事务已毒化等），与"扩展缺失"区分开——
        # 不当静默禁用：记录 warning 便于排查当次启动 memory 被禁的原因
        logger.warning("probe_pgvector: pg_extension 查询失败，按不可用处理", exc_info=True)
        return False
    return r.scalar() is not None


async def _reconcile_vector_dims(conn) -> None:
    """检测 DB 中 vector 列维度是否与配置 embedding_dim 一致，不一致则 ALTER 重建。

    典型场景：旧迁移创建 vector(1536)，用户切换 embedding 模型后配置改为 1024。
    """
    try:
        from app.core.config import get_config
        cfg = get_config()
        expected = int(cfg.memory.get("embedding_dim", 1536))
    except Exception:
        return

    for table, col, idx_name in _VECTOR_TABLES:
        try:
            async with conn.begin_nested():
                r = await conn.execute(text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = CAST(:tbl AS regclass) AND attname = :col AND NOT attisdropped"
                ), {"tbl": table, "col": col})
                fmt = r.scalar()
                if not fmt:
                    continue
                match = re.search(r"vector\((\d+)\)", fmt)
                if not match:
                    continue
                current = int(match.group(1))
                if current == expected:
                    continue
                logger.warning(
                    "vector dim mismatch: %s.%s is vector(%d), config expects %d — altering",
                    table, col, current, expected,
                )
                if idx_name:
                    await conn.execute(text(f"DROP INDEX IF EXISTS {idx_name}"))
                await conn.execute(text(
                    f"ALTER TABLE {table} ALTER COLUMN {col} TYPE vector({expected})"
                ))
                if idx_name:
                    await conn.execute(text(
                        f"CREATE INDEX {idx_name} ON {table} USING hnsw ({col} vector_cosine_ops) "
                        f"WITH (m = 16, ef_construction = 64)"
                    ))
                logger.info("vector dim reconciled: %s.%s → vector(%d)", table, col, expected)
        except Exception:
            logger.warning("vector dim reconcile failed for %s.%s", table, col, exc_info=True)


async def run_startup_migrations(conn) -> None:
    global PGVECTOR_AVAILABLE
    # §9.5：无条件探测（ cheap + 幂等）——已记录 applied 的旧库亦需覆盖
    # “migration_versions 被恢复进无 pgvector 集群”场景，不能靠 applied 跳过探测
    PGVECTOR_AVAILABLE = await probe_pgvector(conn)

    await conn.execute(text(
        """CREATE TABLE IF NOT EXISTS migration_versions (
            version VARCHAR(255) PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT NOW()
        )"""
    ))

    versions = [v for v, _ in STARTUP_MIGRATIONS]
    mem_start = versions.index(_MEMORY_MIGRATION_START) if _MEMORY_MIGRATION_START in versions else len(STARTUP_MIGRATIONS)

    for idx, (version, statement) in enumerate(STARTUP_MIGRATIONS):
        if idx >= mem_start and not PGVECTOR_AVAILABLE:
            # memory v2 迁移块（pgvector_extension 起，必须保持连续后缀——
            # 由 tests/memory_md_round4_test.py #7 后缀纯度断言守护）整体跳过，
            # 不记录版本号，安装 pgvector 后重启可补跑
            if idx == mem_start:
                msg = (
                    "pgvector 扩展不可用（CREATE EXTENSION vector 失败或未安装）；"
                    "跳过 memory v2 全部迁移，memory 子系统将被禁用。"
                    "请安装 pgvector（如 brew install pgvector）并授予 CREATE EXTENSION 权限后重启。")
                try:
                    from app.core.config import get_config
                    _mem_requested = bool(get_config().memory.get("enabled")) or \
                        bool(get_config().memory.get("migration_enabled"))
                except Exception:
                    _mem_requested = True  # 配置读不出时按高调处理，不错过告警
                if _mem_requested:
                    logger.error(msg)
                else:
                    # memory 未开启时降级为 info，避免每次启动刷错误日志
                    logger.info(msg)
            continue

        result = await conn.execute(
            text("SELECT 1 FROM migration_versions WHERE version = :version"),
            {"version": version},
        )
        if result.scalar_one_or_none():
            continue

        await conn.execute(text(statement))
        await conn.execute(
            text("INSERT INTO migration_versions (version) VALUES (:version)"),
            {"version": version},
        )

    # 静态迁移跑完后，校验 vector 列维度是否与配置一致
    if PGVECTOR_AVAILABLE:
        await _reconcile_vector_dims(conn)
