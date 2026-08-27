// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

export interface Message {
  id: string
  conversation_id: string
  role: 'user' | 'assistant'
  content: string
  reasoning_content?: string | null
  tool_calls?: string | null
  tool_results?: string | null
  created_at: string
}

export interface SearchResult {
  title: string
  url: string
  snippet: string
  published_date?: string | null
}

export interface SearchRound {
  round: number
  queries: string[]
  qualified: boolean
  cn_en_count: number
  total_count: number
  reason?: string
}

export interface SearchProgress {
  round: number
  max_rounds: number
  queries: string[]
  status: 'searching' | 'qualified' | 'unqualified'
  result_count?: number
  cn_en_count?: number
  reason?: string
}

export interface SearchFailed {
  rounds: SearchRound[]
  unqualified_results: SearchResult[]
  failure_summary: string
}

export interface ToolStatus {
  name: string
  title: string
  results: SearchResult[]
  rounds?: SearchRound[]
}

export interface AgentStep {
  name: string
  title: string
  content: string
  step_type: 'llm' | 'tool' | 'tool_call' | 'tool_result' | 'status'
  subtask_id?: string
  subtask_name?: string
  subtask_status?: 'pending' | 'running' | 'completed' | 'failed'
  subtask_result_preview?: string
  tool_name?: string
  tool_args?: Record<string, unknown>
  result_count?: number
  /** 上下文压缩步骤：压缩前后 token 估算（消息+工具 schema 口径） */
  tokens_before?: number | null
  tokens_after?: number | null
}

/** 每轮请求的上下文 token 用量估算（SSE context_info 事件）。 */
export interface ContextInfo {
  tokens: number
  context_length: number
  /** True when this snapshot follows a context compression. */
  compressed?: boolean
}

export interface DisplaySequenceItem {
  type: 'text' | 'reasoning' | 'tool_call' | 'agent_step' | 'sub_agent_chunk' | string
  content: string
  part_id?: string
  name?: string
  title?: string
  step_type?: string
  call_id?: string
  status?: 'running' | 'completed' | 'error'
  arguments?: Record<string, unknown>
  result?: string
  error?: boolean
  subtask_id?: string
  subtask_name?: string
  reasoning_content?: string
}

/** F1-1 part protocol events (SSE protocol version 2) */
export interface PartStartedEvent {
  part_id: string
  part_type: string
  call_id?: string
  name?: string
  title?: string
  step_type?: string
  status?: 'running' | 'completed' | 'error'
  arguments?: Record<string, unknown>
  content?: string
  subtask_id?: string
  subtask_name?: string
}

export interface PartDeltaEvent {
  part_id: string
  part_type: string
  field: string
  delta: string
}

export interface PartUpdatedEvent {
  part_id: string | null
  call_id?: string
  name?: string
  status?: 'running' | 'completed' | 'error'
  result?: string
  error?: boolean
  content?: string
  title?: string
}

export interface FileAttachment {
  name: string
  path: string
  size: number
  type: string
}

export interface SubAgentOutput {
  name: string
  content: string
  reasoning: string
}

export interface TaskSubtask {
  id: string
  name: string
  goal: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  result?: string
}

export interface TaskProgress {
  task_id: string
  plan_name: string
  subtasks: TaskSubtask[]
  status: 'planning' | 'executing' | 'completed' | 'failed'
}

export interface ToolResultsData {
  rounds?: SearchRound[]
  results?: SearchResult[]
  search_failed?: boolean
  forced?: boolean
  unqualified_results?: SearchResult[]
  failure_summary?: string
  agent_steps?: AgentStep[]
  attachments?: FileAttachment[]
  content_segments?: string[]
  display_sequence?: DisplaySequenceItem[]
  task_plan?: TaskProgress | null
  sub_agent_outputs?: Record<string, SubAgentOutput>
  thinking_history?: SubAgentThinking[]
}

/** Agent tool-loop streaming events (use_tool_loop = true) */
export interface ToolCallEvent {
  call_id: string
  name: string
  arguments: Record<string, unknown>
}

export interface ToolResultEvent {
  call_id: string
  name: string
  result: string
  error: boolean
}

export interface IterationEvent {
  current: number
  max: number
  tool_calls: number
}

export interface ConversationGroup {
  id: string
  name: string
  color: string
  assistant_id?: string | null
  sort_order: number
  created_at: string
  updated_at: string
  conversation_count: number
}

export interface Conversation {
  id: string
  title: string
  group_id?: string | null
  sort_order: number
  created_at: string
  updated_at: string
  last_user_message_at?: string | null
  assistant_id?: string | null
  messages?: Message[]
  deathmatch_mode?: boolean
  deathmatch_status?: string
  deathmatch_reason?: string | null
  deathmatch_goal?: string | null
  deathmatch_turns?: number
  deathmatch_max_turns?: number
  deathmatch_grilling_total?: number
  deathmatch_grilling_completed?: number
  deathmatch_grilling_round?: number
  deathmatch_grilling_round_total?: number
  deathmatch_context_summary?: string | null
  deathmatch_expected_marker?: string | null
  deathmatch_marker_miss_count?: number
  deathmatch_compressed_context?: string | null
  deathmatch_plan?: { steps: Array<{ id: string; description: string; expected_output?: string; verification_method?: string; dependencies?: string[]; status?: string }> } | null
  deathmatch_plan_version?: number
}

export interface ConversationUpdate {
  title?: string | null
  group_id?: string | null
}

export interface ChatParams {
  temperature?: number | null
  top_p?: number | null
  top_k?: number | null
  presence_penalty?: number | null
  frequency_penalty?: number | null
  max_tokens?: number | null
}

export interface ChatRequest {
  conversation_id?: string | null
  assistant_id?: string | null
  messages: { role: 'user' | 'assistant'; content: string }[]
  regenerate_from_message_id?: string | null
  edit_message_id?: string | null
  force_search_results?: string | null
  temperature?: number | null
  top_p?: number | null
  top_k?: number | null
  presence_penalty?: number | null
  frequency_penalty?: number | null
  max_tokens?: number | null
  enable_reasoning?: boolean
  reasoning_effort?: string | null
  thinking_budget?: number | null
  deathmatch_mode?: boolean
  deathmatch_action?: string | null
}

export interface DeathmatchPlanStep {
  id: string
  description: string
  status: string
}

export interface DeathmatchVerdict {
  status: string
  verdict?: string | null
  reason?: string | null
  turns: number
  max_turns: number
  grilling_completed: number
  grilling_total: number
  grilling_round?: number
  grilling_round_total?: number
  message: string
  grilling_questions?: GrillingQuestion[]
  goal?: string | null
  // PEVR extension (loop_improve.md Phase 3.6)
  plan_version?: number
  plan_steps?: DeathmatchPlanStep[]
  verify_failures?: number
  last_verification?: any
  human_gate?: string | null
}

export interface GrillingQuestion {
  task_id: string
  question_id: string
  question: string
  recommendation: string
  options: string[]
  round?: number
  status?: string
  answer?: string | null
}

export interface Assistant {
  id: string
  user_id: string
  name: string
  system_prompt: string
  temperature?: number | null
  top_p?: number | null
  top_k?: number | null
  presence_penalty?: number | null
  frequency_penalty?: number | null
  max_tokens?: number | null
  use_custom_model?: boolean
  custom_api_url?: string | null
  custom_api_key?: string | null
  custom_model_name?: string | null
  provider_type?: string
  extra_body?: string | null
  use_subtask_model?: boolean
  subtask_custom_api_url?: string | null
  subtask_custom_api_key?: string | null
  subtask_custom_model_name?: string | null
  subtask_provider_type?: string | null
  subtask_extra_body?: string | null
  thinking_budget?: number | null
  min_p?: number | null
  repetition_penalty?: number | null
  thinking_temperature?: number | null
  thinking_top_p?: number | null
  thinking_top_k?: number | null
  thinking_min_p?: number | null
  thinking_presence_penalty?: number | null
  thinking_repetition_penalty?: number | null
  preserve_thinking?: boolean
  created_at: string
  updated_at: string
}

export interface AssistantFormData {
  name: string
  system_prompt: string
  temperature?: number | null
  top_p?: number | null
  top_k?: number | null
  presence_penalty?: number | null
  frequency_penalty?: number | null
  max_tokens?: number | null
  use_custom_model?: boolean
  custom_api_url?: string | null
  custom_api_key?: string | null
  custom_model_name?: string | null
  provider_type?: string
  extra_body?: string | null
  use_subtask_model?: boolean
  subtask_custom_api_url?: string | null
  subtask_custom_api_key?: string | null
  subtask_custom_model_name?: string | null
  subtask_provider_type?: string | null
  subtask_extra_body?: string | null
  thinking_budget?: number | null
  min_p?: number | null
  repetition_penalty?: number | null
  thinking_temperature?: number | null
  thinking_top_p?: number | null
  thinking_top_k?: number | null
  thinking_min_p?: number | null
  thinking_presence_penalty?: number | null
  thinking_repetition_penalty?: number | null
  preserve_thinking?: boolean
}

export interface User {
  id: string
  username: string
  created_at: string
}

export interface UserSession {
  id: string
  session_token: string
  ip_address: string | null
  user_agent: string | null
  last_active_at: string | null
  expires_at: string | null
  created_at: string
}

export interface ChatSession {
  id: string
  conversation_id: string | null
  assistant_id: string | null
  started_at: string | null
  ended_at: string | null
  message_count: number
  total_tokens: number | null
  created_at: string
}

export interface Notebook {
  id: string
  name: string
  is_default: boolean
  created_at: string
  updated_at: string
  note_count: number
}

export interface Note {
  id: string
  notebook_id: string
  title: string | null
  content: string
  raw_transcription: string | null
  created_at: string
  updated_at: string
}

export interface NoteListItem {
  id: string
  notebook_id: string
  title: string | null
  content_preview: string
  /** Raw character length of the note body. */
  content_length?: number
  /** Server-side approximate token count (cl100k-style). */
  token_estimate?: number
  created_at: string
  updated_at: string
}

export interface BulkDeleteResult {
  status: string
  deleted_count: number
}

export interface BulkMoveResult {
  status: string
  moved_count: number
}

export interface MatchedMessage {
  id: string
  role: string
  content_snippet: string
}

export interface ConversationSearchResult {
  conversation_id: string
  title: string
  updated_at: string
  matched_messages: MatchedMessage[]
}

export interface NoteSearchResult {
  note_id: string
  notebook_id: string
  notebook_name: string
  title: string | null
  content_snippet: string
}

export interface AgentTaskInfo {
  id: string
  title: string | null
  goal: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  iterations_done: number
  iterations_max: number
  elapsed_seconds: number | null
  task_type: string
  result: string | null
  error: string | null
  output_conversation_id: string | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
}

export interface AgentTaskSubmitResponse {
  task_submitted: boolean
  task_id: string
  message: string
}

export interface ExportTaskInfo {
  id: string
  task_type: 'single' | 'bulk'
  format: 'md' | 'pdf'
  note_id: string | null
  status: 'pending' | 'claimed' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  filename: string | null
  error: string | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
}

export interface Skill {
  id: string
  user_id: string
  name: string
  description: string | null
  content: string
  is_active: boolean
  source: 'system' | 'user'
  category?: string | null
  created_at: string
  updated_at: string
}

export interface SkillFormData {
  name: string
  description?: string | null
  content: string
  is_active?: boolean
}
