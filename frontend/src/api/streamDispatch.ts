// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * SSE payload dispatch + handler contracts for the chat stream.
 *
 * Extracted from api/chat.ts into a dependency-free module so the dispatch
 * logic (busy / superseded / part-event routing) can be unit-tested under
 * plain Node without pulling in axios / pinia / vue imports.
 */
import type { Conversation, Message, ChatRequest, ConversationUpdate, BulkDeleteResult, ConversationSearchResult, SearchProgress, SearchFailed, ToolStatus, AgentStep, SubAgentThinking, FileAttachment, TaskProgress, ToolCallEvent, ToolResultEvent, IterationEvent, PartStartedEvent, PartDeltaEvent, PartUpdatedEvent, ContextInfo } from '../types'

export interface StreamStatusResult {
  has_buffer: boolean
  status: 'incomplete' | 'complete' | 'none'
  is_running: boolean
  db_message_id: string | null
  content_length: number
  /** True when the backend run is still in its SETUP phase (no buffer
   *  created yet — coordinator pre-pass / memory / workspace gather, or a
   *  detached setup-recovery re-drive after an SSE abort). The frontend
   *  must keep waiting instead of treating "no buffer" as "agent stopped":
   *  the answer is produced and self-saved once setup completes (conv
   *  b078987b, 2026-08-03). */
  setup_in_progress?: boolean
  /** True when the status request itself failed (network down) — callers must
   *  not treat this as a definitive "no buffer / agent stopped" answer. */
  error?: boolean
}

/** Replay snapshot payload from POST /chat/stream/resume. */
export interface ReplayPayload {
  /** SSE protocol version. >= 2 means display_sequence items carry part_id
   *  and the live stream emits part_started/part_delta/part_updated. */
  version?: number
  content: string
  reasoning: string
  layer1: { content: string; reasoning: string }
  layer2: { content: string; reasoning: string }
  tool_calls: any[]
  tool_results: any[]
  agent_steps: any[]
  content_segments: string[]
  display_sequence: any[]
  file_attachments: any[]
  search_progress: any[]
  search_failed: any | null
  iteration: any
  context_info?: ContextInfo
  status: string
  is_running: boolean
  db_message_id: string | null
}

/** Handler bundle for streamChat. Options-object form replaces the previous
 * 22-positional-parameter signature — positional drift had already silently
 * miswired 3 of 5 callsites (onDeathmatchVerdict/onPermissionRequest/onPing
 * landing in each other's slots). */
export interface StreamHandlers {
  onMessage: (content: string) => void
  onDone: (conversationId: string, messageId: string, title?: string, toolResults?: string | null, searchFailed?: boolean, taskSubmitted?: boolean) => void
  onError: (error: string) => void
  onReasoning?: (content: string) => void
  onToolStatus?: (status: ToolStatus) => void
  signal?: AbortSignal
  onSearchProgress?: (progress: SearchProgress) => void
  onSearchFailed?: (failed: SearchFailed) => void
  onAgentStep?: (step: AgentStep) => void
  onTitleUpdate?: (conversationId: string, title: string) => void
  onSubAgentThinking?: (thinking: SubAgentThinking) => void
  onFileAttachment?: (attachments: FileAttachment[]) => void
  onTaskProgress?: (progress: TaskProgress) => void
  onSubAgentChunk?: (chunk: { subtask_id: string; subtask_name: string; kind: 'content' | 'reasoning'; delta: string }) => void
  onToolCall?: (toolCall: ToolCallEvent) => void
  onToolResult?: (toolResult: ToolResultEvent) => void
  onIteration?: (iteration: IterationEvent) => void
  onContentSegment?: (content: string) => void
  onDeathmatchVerdict?: (verdict: any) => void
  onPermissionRequest?: (request: { request_id: string; tool_name: string; description: string; details: Record<string, any> }) => void
  onPing?: () => void
  onPartStarted?: (part: PartStartedEvent) => void
  onPartDelta?: (part: PartDeltaEvent) => void
  onPartUpdated?: (part: PartUpdatedEvent) => void
  /** 每轮请求的上下文 token 用量估算（含系统提示词+历史+工具 schema）。 */
  onContextInfo?: (info: ContextInfo) => void
  /** 4.8 session lock: the conversation already has a running agent. */
  onConversationBusy?: (conversationId?: string) => void
  /** A newer request took over the conversation while this stream was still
   *  in its setup phase. The stream must end silently (no error bubble) and
   *  resync from the DB — the newer run owns the answer. */
  onConversationSuperseded?: () => void
}

export interface ResumeHandlers extends StreamHandlers {
  onReplay: (replay: ReplayPayload) => void
}

/** Shared SSE payload dispatch. Returns true when the stream must terminate. */
export function dispatchStreamPayload(data: any, h: StreamHandlers): boolean {
  if (data.error) {
    if (data.code === 'conversation_busy' && h.onConversationBusy) {
      h.onConversationBusy(data.conversation_id)
      return true
    }
    if (data.code === 'conversation_superseded' && h.onConversationSuperseded) {
      h.onConversationSuperseded()
      return true
    }
    h.onError(data.error)
    return true
  }

  if (data.part_started && h.onPartStarted) h.onPartStarted(data.part_started)
  if (data.part_delta && h.onPartDelta) h.onPartDelta(data.part_delta)
  if (data.part_updated && h.onPartUpdated) h.onPartUpdated(data.part_updated)

  if (data.search_progress && h.onSearchProgress) h.onSearchProgress(data.search_progress)
  if (data.search_failed && h.onSearchFailed) h.onSearchFailed(data.search_failed)
  if (data.tool_status && h.onToolStatus) h.onToolStatus(data.tool_status)
  if (data.agent_step && h.onAgentStep) h.onAgentStep(data.agent_step)
  if (data.context_info && h.onContextInfo) h.onContextInfo(data.context_info)
  if (data.sub_agent_thinking && h.onSubAgentThinking) h.onSubAgentThinking(data.sub_agent_thinking)
  if (data.attachments && h.onFileAttachment) h.onFileAttachment(data.attachments)
  if (data.task_progress && h.onTaskProgress) h.onTaskProgress(data.task_progress)
  if (data.sub_agent_chunk && h.onSubAgentChunk) h.onSubAgentChunk(data.sub_agent_chunk)
  if (data.title_update && h.onTitleUpdate) h.onTitleUpdate(data.title_update.conversation_id, data.title_update.title)

  if (data.reasoning_content !== undefined && h.onReasoning) h.onReasoning(data.reasoning_content)
  if (data.content !== undefined) h.onMessage(data.content)

  if (data.done) {
    h.onDone(data.conversation_id, data.message_id, data.title, data.tool_results, data.search_failed, !!data.task_submitted)
    return false
  }

  if (data.call_id !== undefined && data.name !== undefined && data.arguments !== undefined && h.onToolCall) {
    h.onToolCall(data as ToolCallEvent)
  }
  if (data.call_id !== undefined && data.name !== undefined && 'result' in data && h.onToolResult) {
    h.onToolResult(data as ToolResultEvent)
  }
  if (data.current !== undefined && data.max !== undefined && h.onIteration) {
    h.onIteration(data as IterationEvent)
  }
  if (data.segment_content !== undefined && h.onContentSegment) h.onContentSegment(data.segment_content)
  if (data.deathmatch_verdict && h.onDeathmatchVerdict) h.onDeathmatchVerdict(data.deathmatch_verdict)
  if (data.permission_request && h.onPermissionRequest) h.onPermissionRequest(data.permission_request)

  if (data.ping) {
    // Keepalive: lets the store's stall watchdog distinguish a healthy
    // but silent stream (long tool runs) from a dead connection.
    if (h.onPing) h.onPing()
  }
  return false
}

// Re-exported types so api/chat.ts's public surface stays unchanged.
export type {
  Conversation, Message, ChatRequest, ConversationUpdate, BulkDeleteResult,
  ConversationSearchResult, SearchProgress, SearchFailed, ToolStatus, AgentStep,
  SubAgentThinking, FileAttachment, TaskProgress, ToolCallEvent, ToolResultEvent,
  IterationEvent, PartStartedEvent, PartDeltaEvent, PartUpdatedEvent, ContextInfo,
}
