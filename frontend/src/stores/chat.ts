// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineStore } from 'pinia'
import { ref, computed, reactive, nextTick } from 'vue'
import type {
  Conversation, Message, ConversationSearchResult, ToolStatus,
  SearchProgress, SearchFailed, AgentStep, SubAgentThinking,
  FileAttachment, TaskProgress,
  ToolCallEvent, ToolResultEvent, IterationEvent,
  DisplaySequenceItem, DeathmatchVerdict,
  PartStartedEvent, PartDeltaEvent, PartUpdatedEvent,
  ContextInfo,
} from '@/types'
import { chatApi } from '@/api/chat'
import { useAssistantStore } from '@/stores/assistant'
import { useNotesStore } from '@/stores/notes'
import { mergeReplayIntoSequence, pickStreamText, shouldApplyRefresh } from '@/stores/streamReducer'
import { estimateTextTokens, estimateMessagesTokens } from '@/composables/useContextTokens'
import { loadContextInfoStorage, persistContextInfoStorage } from '@/composables/useContextTokens'

interface StreamState {
  content: string
  contentSegments: string[]
  reasoning: string
  toolStatuses: ToolStatus[]
  searchProgress: SearchProgress[]
  searchFailed: SearchFailed | null
  agentSteps: AgentStep[]
  subAgentThinking: SubAgentThinking | null
  fileAttachments: FileAttachment[]
  taskProgress: TaskProgress | null
  subAgentChunks: Record<string, { content: string; reasoning: string; name: string }>
  toolCalls: ToolCallEvent[]
  toolResults: ToolResultEvent[]
  iteration: IterationEvent | null
  displaySequence: DisplaySequenceItem[]
  /** F1-1: true once the first part.* event arrives — the timeline is then
   *  driven by part_id reducer instead of legacy positional heuristics. */
  partMode: boolean
  deathmatchVerdict: DeathmatchVerdict | null
  streaming: boolean
  abortController: AbortController | null
  tabSwitchAbort: boolean
  _lastEventTime: number
  /** False until the first SSE event of this stream arrives. The backend's
   *  setup phase (coordinator pre-pass / deathmatch classify — LLM calls that
   *  can exceed 30-60s before any byte reaches the client) is indistinguishable
   *  from a dead connection, so the watchdog grants a longer pre-first-event
   *  grace window and the strict 30s threshold only once events are flowing
   *  (backend then pings every ~10s). */
  _gotFirstEvent: boolean
}

function createStreamState(): StreamState {
  return {
    content: '',
    reasoning: '',
    toolStatuses: [],
    searchProgress: [],
    searchFailed: null,
    agentSteps: [],
    subAgentThinking: null,
    fileAttachments: [],
    taskProgress: null,
    subAgentChunks: {},
    toolCalls: [],
    toolResults: [],
    iteration: null,
    displaySequence: [],
    partMode: false,
    deathmatchVerdict: null,
    streaming: false,
    abortController: null,
    tabSwitchAbort: false,
    _lastEventTime: 0,
    _gotFirstEvent: false,
  }
}

const EMPTY_STREAM: Readonly<StreamState> = Object.freeze(createStreamState())

// A persisted assistant message with no text, no reasoning and no tool
// payload renders as a blank card (avatar + timestamp only) — filter it out
// at load time. Historical example: conv 4d9a5289's deathmatch self-save
// wrote an empty row after the terminal status message.
function isBlankPersistedMessage(m: Message): boolean {
  if (m.role !== 'assistant') return false
  if ((m.content || '').trim()) return false
  if ((m.reasoning_content || '').trim()) return false
  if (m.tool_results) return false
  if (m.tool_calls) return false
  return true
}

function filterBlankPersistedMessages(list: Message[] | undefined | null): Message[] {
  if (!Array.isArray(list)) return []
  return list.filter(m => !isBlankPersistedMessage(m))
}

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<Conversation[]>([])
  const currentConversationId = ref<string | null>(null)
  // Metadata cache for conversations fetched by id (selectConversation /
  // refreshConversation) that may NOT be present in `conversations` — e.g. a
  // deep-linked conversation owned by another assistant. The chat header
  // reads the title from here so foreign-assistant opens keep their title.
  const conversationMeta = ref<Record<string, Conversation>>({})
  const messages = ref<Record<string, Message[]>>({})
  const streamStates: Record<string, StreamState> = reactive({})
  const currentError = ref<string | null>(null)
  const searchResults = ref<ConversationSearchResult[]>([])
  const searchQuery = ref('')
  const searchHighlightQuery = ref('')
  const searchHighlightMessageId = ref('')
  const searchHighlightNonce = ref(0)
  // 每轮 SSE context_info 事件携带的上下文 token 用量（按会话缓存，跨轮保留，
  // 头部徽章读取；endStreaming 清理流状态时不清除它）。
  // 持久化到 localStorage（chatllm_context_info），刷新后按会话恢复——
  // 「每个 session 的上下文一直显式」。
  const contextInfoByConversation = ref<Record<string, ContextInfo>>(loadContextInfoStorage())

  // ── 上下文 token 动态估算（CJK 感知，与后端 context_compressor 同源）─────
  // 后端只在轮首/压缩后发 context_info 快照；轮内 query + 回复流式文本由前端
  // 增量估算叠加，使徽章随对话增长（估算值在下一轮 context_info 到达时被权威
  // 快照取代）。
  const _pendingContextChars: Record<string, number> = {}
  const _pendingContextTokens: Record<string, number> = {}
  const _pendingContextFlushTimers: Record<string, ReturnType<typeof setTimeout>> = {}
  const _CONTEXT_FLUSH_CHARS = 100
  const _CONTEXT_FLUSH_MS = 500

  function flushContextTokenDelta(convId: string) {
    _pendingContextChars[convId] = 0
    const est = _pendingContextTokens[convId] || 0
    _pendingContextTokens[convId] = 0
    if (est <= 0) return
    const cur = contextInfoByConversation.value[convId]
    const next: ContextInfo = cur
      ? { ...cur, tokens: cur.tokens + est }
      : { tokens: est, context_length: 0 }
    contextInfoByConversation.value = { ...contextInfoByConversation.value, [convId]: next }
    persistContextInfoStorage(contextInfoByConversation.value)
  }

  function addContextTokenDelta(convId: string, text: string) {
    if (!convId || !text) return
    // 增量按真实文本做 CJK 感知估算后累加（throttle 只看字符数）。
    _pendingContextTokens[convId] = (_pendingContextTokens[convId] || 0) + estimateTextTokens(text)
    _pendingContextChars[convId] = (_pendingContextChars[convId] || 0) + text.length
    if ((_pendingContextChars[convId] || 0) >= _CONTEXT_FLUSH_CHARS) {
      if (_pendingContextFlushTimers[convId]) {
        clearTimeout(_pendingContextFlushTimers[convId])
        delete _pendingContextFlushTimers[convId]
      }
      flushContextTokenDelta(convId)
    } else if (!_pendingContextFlushTimers[convId]) {
      _pendingContextFlushTimers[convId] = window.setTimeout(() => {
        delete _pendingContextFlushTimers[convId]
        flushContextTokenDelta(convId)
      }, _CONTEXT_FLUSH_MS)
    }
  }

  function cancelPendingContextDelta(convId: string) {
    delete _pendingContextChars[convId]
    delete _pendingContextTokens[convId]
    const t = _pendingContextFlushTimers[convId]
    if (t) {
      clearTimeout(t)
      delete _pendingContextFlushTimers[convId]
    }
  }

  function setContextInfo(convId: string, info: ContextInfo) {
    cancelPendingContextDelta(convId)
    contextInfoByConversation.value = { ...contextInfoByConversation.value, [convId]: info }
    persistContextInfoStorage(contextInfoByConversation.value)
  }

  /** 会话删除时同步清理内存 + localStorage 中的 token 快照。 */
  function dropContextInfo(convId: string) {
    cancelPendingContextDelta(convId)
    if (contextInfoByConversation.value[convId]) {
      delete contextInfoByConversation.value[convId]
      persistContextInfoStorage(contextInfoByConversation.value)
    }
  }

  const localOnlyMessageIds = new Set<string>()
  const _resumingSet = new Set<string>()
  const enableReasoning = ref(false)
  const reasoningEffort = ref<string | null>(null)
  const thinkingBudget = ref<number | null>(null)
  const deathmatchMode = ref(false)
  const deathmatchAction = ref<string | null>(null)
  const grillingQuestions = ref<GrillingQuestion[]>([])
  const grillingAnswers = ref<Record<string, string>>({})

  interface PermissionRequest {
    request_id: string
    tool_name: string
    description: string
    details: Record<string, any>
  }
  const pendingPermissionRequest = ref<PermissionRequest | null>(null)
  // Save-to-note selection mode. Shared between ChatLayout (mobile header
  // button) and ChatArea (selection bar) so either can toggle it.
  const saveModeActive = ref(false)

  async function respondToPermission(approved: boolean) {
    const req = pendingPermissionRequest.value
    if (!req) return
    pendingPermissionRequest.value = null
    try {
      const { default: apiClient } = await import('@/api/client')
      await apiClient.post('/chat/permission/respond', {
        request_id: req.request_id,
        approved,
      })
    } catch (e) {
      console.error('Permission response failed:', e)
    }
  }

  function getStream(convId: string): StreamState {
    if (!streamStates[convId]) {
      streamStates[convId] = createStreamState()
      _streamVersion.value++
    }
    return streamStates[convId]
  }

  const _streamVersion = ref(0)

  // Per-conversation message-list epoch: bumped by beginStreaming whenever a
  // NEW flow (send / edit-resend / regenerate) takes ownership of the
  // conversation's stream state. refreshConversation skips its message-list
  // assignment when the epoch changed while its GET was in flight — a stale
  // server snapshot (e.g. a leftover stop-recovery poll landing between the
  // edit POST and the backend commit) must never clobber a newer flow's
  // optimistic bubbles (observed: stop → edit → resend showed the PRE-EDIT
  // query in the user bubble during the whole stream, correcting only after
  // the new answer completed).
  const messagesEpoch: Record<string, number> = {}

  // Same-flow stale-response guard: multiple refreshConversation calls can be
  // in flight for one conversation (syncAfterAbort Phase-1 polls, onError
  // immediate + delayed re-refresh, resume onDone/onError, visibilitychange).
  // Their GETs resolve OUT OF ORDER — an older pre-commit snapshot resolving
  // last replaces the message list with a pre-answer state and the completed
  // answer vanishes from the UI until the next refresh. Only the LATEST
  // refresh call may apply its response; older responses are discarded
  // (last-request-wins, mirroring loadConversations' conversationsLoadSeq).
  const refreshSeqs: Record<string, number> = {}

  function getCurrentStream(): Readonly<StreamState> {
    const id = currentConversationId.value
    if (!id) return EMPTY_STREAM
    void _streamVersion.value
    return streamStates[id] || EMPTY_STREAM
  }

  function beginStreaming(conversationId: string) {
    const s = getStream(conversationId)
    // A new flow is taking over this conversation's stream state — any
    // in-flight connection (main SSE or resume) must be aborted FIRST.
    // Otherwise the old connection keeps delivering into the freshly reset
    // state, and two connections applying the same part events produce
    // duplicated timeline items (identical part_id keys → text rendered 2-3x
    // and tool cards lost to v-for key collisions).
    if (s.abortController) {
      try { s.abortController.abort() } catch { /* best-effort */ }
      // The aborted resume's `_resumingSet` entry is only removed in its own
      // finally block, which runs asynchronously after the abort propagates.
      // A takeover resume right after this beginStreaming would otherwise be
      // blocked by the stale guard. Clearing it here is safe: the aborted
      // resume is dead by definition, and its finally re-deletes idempotently.
      _resumingSet.delete(conversationId)
    }
    const prevDeathmatchStatus = s.deathmatchVerdict?.status
    s.content = ''
    s.reasoning = ''
    s.toolStatuses = []
    s.searchProgress = []
    s.searchFailed = null
    s.agentSteps = []
    s.subAgentThinking = null
    s.fileAttachments = []
    s.taskProgress = null
    s.subAgentChunks = {}
    s.toolCalls = []
    s.toolResults = []
    s.iteration = null
    s.displaySequence = []
    s.partMode = false
    _partIndexes.delete(conversationId)
    // Preserve deathmatchVerdict when transitioning between grilling/active phases
    if (prevDeathmatchStatus === 'grilling' || prevDeathmatchStatus === 'active') {
      s.deathmatchVerdict = { ...s.deathmatchVerdict!, message: '' }
    } else {
      s.deathmatchVerdict = null
    }
    s.streaming = true
    s.tabSwitchAbort = false
    s._lastEventTime = Date.now()
    s._gotFirstEvent = false
    messagesEpoch[conversationId] = (messagesEpoch[conversationId] || 0) + 1
    ensureStallWatchdog()
    currentError.value = null
    _streamVersion.value++
  }

  // SSE stall watchdog. The fetch reader can hang indefinitely when the
  // connection dies silently — no error, no data (observed: stream froze
  // mid-turn, the answer only appeared after a manual page refresh; conv
  // 3bc79c4c 2026-07-21). The backend emits a keepalive ping every ~10s and
  // every event (including pings) refreshes `_lastEventTime`, so >30s of
  // total silence means the connection is dead even during long tool runs.
  // Aborting the fetch routes into each flow's AbortError ->
  // tryReconnectOrSync recovery (resume the buffered stream, else syncAfterAbort).
  // Streams stuck at streaming=true with NO controller (e.g. a resume that was
  // aborted before this watchdog could see it) are driven through the same
  // recovery by the safety-net branch below — no stream may stall forever.
  const STREAM_STALL_THRESHOLD_MS = 30000
  // Pre-first-event grace: backend setup (coordinator/classify LLM calls) can
  // legitimately run 60s+ before the first byte. Killing such a stream aborts
  // the ENTIRE run server-side (the detached agent task does not exist yet).
  const STREAM_FIRST_EVENT_GRACE_MS = 90000
  let _stallWatchdogTimer: ReturnType<typeof setInterval> | null = null
  function ensureStallWatchdog() {
    if (_stallWatchdogTimer) return
    _stallWatchdogTimer = setInterval(() => {
      const now = Date.now()
      for (const convId of Object.keys(streamStates)) {
        const s = streamStates[convId]
        if (!s || !s.streaming) continue
        if (!s._lastEventTime) continue
        const threshold = s._gotFirstEvent ? STREAM_STALL_THRESHOLD_MS : STREAM_FIRST_EVENT_GRACE_MS
        if (now - s._lastEventTime <= threshold) continue
        if (s.abortController) {
          try { s.abortController.abort() } catch { /* best-effort */ }
        } else if (!_resumingSet.has(convId) && !_recoveryInFlight.has(convId)) {
          // Safety net: streaming=true with no controller and no resume in
          // flight is an unrecoverable-by-anyone-else state (the UI shows a
          // blinking cursor forever). Drive it back through the standard
          // reconnect-or-sync recovery so it always terminates.
          void tryReconnectOrSync(convId, (messages.value[convId] || []).length + 1, s)
        }
      }
    }, 5000)
  }

  function endStreaming(conversationId: string) {
    const s = streamStates[conversationId]
    if (!s) return
    s.streaming = false
    s.abortController = null
    s.content = ''
    s.reasoning = ''
    s.contentSegments = []
    s.toolStatuses = []
    s.searchProgress = []
    s.searchFailed = null
    s.agentSteps = []
    s.subAgentThinking = null
    s.taskProgress = null
    s.subAgentChunks = {}
    s.toolCalls = []
    s.toolResults = []
    s.iteration = null
    s.displaySequence = []
    s.partMode = false
    _partIndexes.delete(conversationId)
    // Don't clear deathmatchVerdict — persist it after streaming ends
    // so the status bar remains visible for active deathmatch sessions
    // Start polling if deathmatch is still active
    if (s.deathmatchVerdict?.status === 'active' || s.deathmatchVerdict?.status === 'grilling') {
      startDeathmatchPolling()
    }
    _streamVersion.value++
  }

  const currentMessages = computed(() => {
    if (!currentConversationId.value) return []
    return messages.value[currentConversationId.value] || []
  })

  const isStreamingCurrentConversation = computed(() => {
    return getCurrentStream().streaming
  })

  const currentStreamingContent = computed(() => getCurrentStream().content)
  const currentStreamingReasoningContent = computed(() => getCurrentStream().reasoning)
  const currentStreamingToolStatuses = computed(() => getCurrentStream().toolStatuses)
  const currentStreamingSearchProgress = computed(() => getCurrentStream().searchProgress)
  const currentStreamingSearchFailed = computed(() => getCurrentStream().searchFailed)
  const currentStreamingAgentSteps = computed(() => getCurrentStream().agentSteps)
  const currentStreamingSubAgentThinking = computed(() => getCurrentStream().subAgentThinking)
  const currentStreamingFileAttachments = computed(() => getCurrentStream().fileAttachments)
  const currentStreamingContentSegments = computed(() => getCurrentStream().contentSegments)
  const currentStreamingTaskProgress = computed(() => getCurrentStream().taskProgress)
  const currentStreamingSubAgentChunks = computed(() => getCurrentStream().subAgentChunks)

  const currentStreamingToolCalls = computed(() => getCurrentStream().toolCalls)
  const currentStreamingToolResults = computed(() => getCurrentStream().toolResults)
  const currentStreamingIteration = computed(() => getCurrentStream().iteration)
  const currentStreamingDisplaySequence = computed(() => getCurrentStream().displaySequence)
  const currentStreamingDeathmatchVerdict = computed(() => getCurrentStream().deathmatchVerdict)

  /** 当前会话最近一次上报的上下文 token 用量（头部徽章）。 */
  const currentContextInfo = computed(() => {
    const id = currentConversationId.value
    return id ? contextInfoByConversation.value[id] || null : null
  })

  const streamingContent = currentStreamingContent
  const streamingReasoningContent = currentStreamingReasoningContent
  const streamingToolStatuses = currentStreamingToolStatuses
  const streamingSearchProgress = currentStreamingSearchProgress
  const streamingSearchFailed = currentStreamingSearchFailed
  const streamingSubAgentThinking = currentStreamingSubAgentThinking
  const streamingFileAttachments = currentStreamingFileAttachments
  const streamingTaskProgress = currentStreamingTaskProgress
  const streamingConversationId = computed(() => {
    for (const [cid, s] of Object.entries(streamStates)) {
      if (s.streaming) return cid
    }
    return null
  })
  const isStreaming = computed(() => {
    for (const s of Object.values(streamStates)) {
      if (s.streaming) return true
    }
    return false
  })

  const activeStreamingConversationIds = computed(() => {
    const ids = new Set<string>()
    for (const [cid, s] of Object.entries(streamStates)) {
      if (s.streaming) ids.add(cid)
    }
    return ids
  })

  async function refreshConversation(conversationId: string) {
    const epochAtStart = messagesEpoch[conversationId] || 0
    const seqAtStart = (refreshSeqs[conversationId] || 0) + 1
    refreshSeqs[conversationId] = seqAtStart
    try {
      const conversation = await chatApi.getConversation(conversationId)
      conversationMeta.value[conversationId] = {
        ...conversationMeta.value[conversationId],
        id: conversationId,
        title: conversation.title,
        group_id: conversation.group_id,
        assistant_id: conversation.assistant_id,
        deathmatch_mode: conversation.deathmatch_mode,
        deathmatch_status: conversation.deathmatch_status,
        deathmatch_goal: conversation.deathmatch_goal,
        deathmatch_turns: conversation.deathmatch_turns,
        deathmatch_max_turns: conversation.deathmatch_max_turns,
        deathmatch_grilling_round: conversation.deathmatch_grilling_round,
        deathmatch_grilling_round_total: conversation.deathmatch_grilling_round_total,
      } as Conversation
      // A newer flow (send / edit-resend / regenerate) started while this GET
      // was in flight — its optimistic bubbles must not be replaced by this
      // (possibly pre-commit) server snapshot. Skip only the message-list
      // assignment; metadata sync stays harmless.
      // Two guards (same-flow race, conv: already-displayed answer vanished
      // mid-answer while the DB had the full content):
      //  1. last-request-wins: a stale refresh resolving after a newer one
      //     must not apply (its snapshot predates the newer one's commit).
      //  2. monotonicity: even the LATEST call must not apply a snapshot that
      //     is strictly OLDER than the local list (missing the newest real
      //     message the user can already see).
      if (
        (messagesEpoch[conversationId] || 0) === epochAtStart
        && (refreshSeqs[conversationId] || 0) === seqAtStart
      ) {
        const serverMessages = filterBlankPersistedMessages(conversation.messages)
        const localMessages = messages.value[conversationId] || []
        if (shouldApplyRefresh(localMessages, serverMessages)) {
          // If the newest local message is a synthetic placeholder the server
          // list lacks (save-failure fallback done / resume with no real id /
          // optimistic bubble), PRESERVE it on top of the applied list ONLY
          // while the server list is shorter (pre-commit) — the user is
          // looking at that content; dropping it would recreate the mid-answer
          // disappearance. Once the server list catches up (same length),
          // apply cleanly so the synthetic is replaced by its real row and
          // never duplicates it.
          const newestLocal = localMessages[localMessages.length - 1]
          const serverHasNewest = newestLocal && serverMessages.some(m => m.id === newestLocal.id)
          const preserveSynthetic = newestLocal && !serverHasNewest
            && serverMessages.length < localMessages.length
          messages.value[conversationId] = preserveSynthetic
            ? [...serverMessages, newestLocal]
            : serverMessages
        }
      }
      // 历史会话无 SSE 快照缓存时，按已加载消息做一次本地估算，保证徽章显式。
      if (!contextInfoByConversation.value[conversationId]) {
        const base = estimateMessagesTokens(conversation.messages || [])
        if (base > 0) setContextInfo(conversationId, { tokens: base, context_length: 0 })
      }
      for (const message of conversation.messages || []) {
        localOnlyMessageIds.delete(message.id)
      }
      const conv = conversations.value.find(item => item.id === conversationId)
      if (conv) {
        conv.title = conversation.title
        conv.updated_at = conversation.updated_at
        conv.deathmatch_mode = conversation.deathmatch_mode
        conv.deathmatch_status = conversation.deathmatch_status
        conv.deathmatch_goal = conversation.deathmatch_goal
        conv.deathmatch_turns = conversation.deathmatch_turns
        conv.deathmatch_max_turns = conversation.deathmatch_max_turns
        conv.deathmatch_grilling_total = conversation.deathmatch_grilling_total
        conv.deathmatch_grilling_completed = conversation.deathmatch_grilling_completed
        conv.deathmatch_grilling_round = conversation.deathmatch_grilling_round
        conv.deathmatch_grilling_round_total = conversation.deathmatch_grilling_round_total
      }
      // Sync deathmatch verdict from conversation state when SSE is not active
      const s = getStream(conversationId)
      if (!s.streaming && conversation.deathmatch_mode && conversation.deathmatch_status !== 'inactive') {
        const planSteps = (conversation.deathmatch_plan?.steps || []).map((step: any) => ({
          id: step.id || '',
          description: step.description || '',
          status: step.status || 'pending',
        }))
        s.deathmatchVerdict = {
          status: conversation.deathmatch_status || 'active',
          verdict: null,
          reason: null,
          turns: conversation.deathmatch_turns || 0,
          max_turns: conversation.deathmatch_max_turns || 30,
          grilling_completed: conversation.deathmatch_grilling_completed || 0,
          grilling_total: conversation.deathmatch_grilling_total || 0,
          grilling_round: conversation.deathmatch_grilling_round || 0,
          grilling_round_total: conversation.deathmatch_grilling_round_total || 3,
          message: conversation.deathmatch_status === 'done' ? '目标已完成' : conversation.deathmatch_status === 'partial_complete' ? '目标未完全达成，发送消息继续推进，或关闭死磕模式接受当前结果' : conversation.deathmatch_status === 'human_gate' ? (conversation.deathmatch_reason || '需要人工介入，发送消息继续或调整目标') : '',
          plan_version: conversation.deathmatch_plan_version || 0,
          plan_steps: planSteps,
        }
        deathmatchMode.value = conversation.deathmatch_status !== 'done'
        _streamVersion.value++
      }
    } catch (e) {
      console.error('Failed to refresh conversation:', e)
    }
  }

  async function syncAfterAbort(
    conversationId: string,
    expectedMessageCount: number,
    partialContent: string,
    partialReasoningContent: string,
  ) {
    const s = getStream(conversationId)
    const sleep = (ms: number) => new Promise<void>(resolve => { window.setTimeout(() => resolve(), ms) })
    // Capture the message-list epoch at entry: Phase-1 polls refreshConversation
    // unconditionally, and a leftover stop-recovery loop must stop the moment a
    // newer flow (edit-resend / send / regenerate) takes over the conversation
    // — its beginStreaming bumps the epoch, so any further refresh here would
    // just re-clobber the newer flow's optimistic bubbles (the refresh's own
    // epoch guard already skips the stale in-flight assignment).
    const inheritedEpoch = messagesEpoch[conversationId] || 0

    // Phase 1: fast polls — covers the common case where the aborted stream
    // had already saved its final message before the abort landed.
    for (let attempt = 0; attempt < 8; attempt += 1) {
      await sleep(attempt === 0 ? 150 : 300)
      // Superseded by a newer flow on this conversation — stop touching state.
      if ((messagesEpoch[conversationId] || 0) !== inheritedEpoch) return
      await refreshConversation(conversationId)

      if ((messages.value[conversationId] || []).length >= expectedMessageCount) {
        currentError.value = null
        endStreaming(conversationId)
        return
      }
    }

    // Phase 2: the agent is likely still running server-side (detached-agent
    // design). Keep polling the stream status instead of freezing the partial
    // content as the "final answer"; refresh once the agent completes.
    // Bounded at ~5 minutes, then fall back to showing the partial content.
    // Network-error polls are NOT counted as "agent stopped" — only definitive
    // answers (complete / stopped / no buffer from a healthy response) are.
    let stalePolls = 0
    let errorPolls = 0
    for (let poll = 0; poll < 100; poll += 1) {
      await sleep(3000)
      // Superseded: another flow took over this conversation's stream state
      // (e.g. user resent a message, or a resume attached a controller).
      if (!s.streaming || s.abortController) return
      // Epoch check closes the same supersession hole for Phase-2: a newer
      // flow that begins AND completes between two 3s sleeps leaves both the
      // streaming flag and the controller cleared, so without this check the
      // loop would keep polling and could append a stale local-abort message.
      if ((messagesEpoch[conversationId] || 0) !== inheritedEpoch) return

      const status = await chatApi.getStreamStatus(conversationId)
      if (status.error) {
        errorPolls += 1
        if (errorPolls >= 20) break
        continue
      }
      errorPolls = 0
      if (status.setup_in_progress || (status.has_buffer && status.status === 'incomplete' && status.is_running)) {
        stalePolls = 0
        // setup_in_progress: the backend run is still in its SETUP phase —
        // no buffer exists yet (it is created only when the agent task
        // starts), so "no buffer" here must NOT count as "agent stopped".
        // The setup can take 20-60s+ (coordinator pre-pass / memory /
        // workspace gather, or a detached setup-recovery re-drive after an
        // SSE abort); the answer is produced and self-saved once it
        // completes (conv b078987b, 2026-08-03).
        // The buffer claims the agent is still running. That is normally
        // true (long agent / deathmatch runs), but a stale buffer can claim
        // it forever even though the answer is already saved — so every few
        // polls, cross-check the DB: if the final message is there, stop
        // waiting instead of pinning the streaming state indefinitely.
        // Skipped for deathmatch: its goal loop persists per-turn messages
        // mid-run, so the count check would false-positive and detach the
        // live stream long before the loop finishes.
        const dmStatus = s.deathmatchVerdict?.status
        const dmActive = dmStatus === 'active' || dmStatus === 'grilling'
        if (!dmActive && poll > 0 && poll % 10 === 0) {
          await refreshConversation(conversationId)
          if ((messages.value[conversationId] || []).length >= expectedMessageCount) {
            currentError.value = null
            endStreaming(conversationId)
            return
          }
        }
        continue
      }

      // Buffer complete / agent stopped / buffer expired: the final message
      // should now be in the DB (or about to be saved).
      await refreshConversation(conversationId)
      if ((messages.value[conversationId] || []).length >= expectedMessageCount) {
        currentError.value = null
        endStreaming(conversationId)
        return
      }
      stalePolls += 1
      if (stalePolls >= 3) break
    }

    if (partialContent.trim()) {
      const tempMessageId = `local-abort-${Date.now()}`
      localOnlyMessageIds.add(tempMessageId)
      messages.value[conversationId] = [
        ...(messages.value[conversationId] || []),
        {
          id: tempMessageId,
          conversation_id: conversationId,
          role: 'assistant',
          content: partialContent,
          reasoning_content: partialReasoningContent || null,
          created_at: new Date().toISOString(),
        },
      ]
    }

    currentError.value = null
    endStreaming(conversationId)
  }

  async function resumeActiveStream(conversationId: string): Promise<boolean> {
    if (_resumingSet.has(conversationId)) return false
    const s = getStream(conversationId)
    if (!s.streaming) {
      return false
    }
    _resumingSet.add(conversationId)

    const abortController = new AbortController()
    s.abortController = abortController
    // Give the fresh resume connection a full watchdog grace window — the
    // inherited `_lastEventTime` belongs to the previous (stalled) stream and
    // would let the watchdog kill this healthy resume within seconds.
    const touch = () => { s._lastEventTime = Date.now(); s._gotFirstEvent = true }
    // Entry: refresh the timestamp only — `_gotFirstEvent` is set by the first
    // REAL event (replay / ping / delta), preserving the setup grace window.
    s._lastEventTime = Date.now()
    // Consume any stale tab-switch flag; by the time a resume starts, the
    // original stream's AbortError catch has already run.
    s.tabSwitchAbort = false

    let replayStatus: string = 'incomplete'
    let replayDbMessageId: string | null = null

    // dispatchStreamPayload calls h.onDone(conversation_id, message_id,
    // title, tool_results, search_failed, task_submitted) — the resume
    // callback must declare the SAME positional order or the local message
    // gets id=conversation_id and tool_results=title (conv 692deb04: after
    // SSE-cancel → resume, only the last thinking + answer rendered until a
    // manual refresh; the corrupted message also failed shouldApplyRefresh,
    // so the auto-refresh never replaced it).
    const onDoneCallback = (newConvId: string, messageId: string, title?: string, toolResults?: string | null, _searchFailed?: boolean, _taskSubmitted?: boolean) => {
      touch()
      const displayContent = s.content
        || '系统未能生成有效回答，请重新尝试。'
      const assistantMessage: Message = {
        id: messageId || `resume-${Date.now()}`,
        conversation_id: conversationId,
        role: 'assistant',
        content: displayContent,
        reasoning_content: s.reasoning || null,
        tool_results: toolResults || null,
        created_at: new Date().toISOString(),
      }
      if (!messages.value[conversationId]) messages.value[conversationId] = []
      messages.value[conversationId].push(assistantMessage)
      if (title) {
        const conv = conversations.value.find(c => c.id === conversationId)
        if (conv) conv.title = title
      }
      endStreaming(conversationId)
      void refreshConversation(conversationId)
    }

    try {
      // F1-1: reuse the shared callback set (eliminates the duplicated
      // inline handlers that had drifted out of sync — missing
      // onSubAgentChunk/onPing slots, non-debounced notes refresh, and no
      // part.* handling). Only replay/done/error stay resume-specific.
      const shared = wireStreamCallbacks(conversationId, () => {})
      await chatApi.resumeStream(conversationId, {
        ...shared,
        signal: abortController.signal,
        onReplay: (replay) => {
          touch()
          if (replay.db_message_id && !replay.content) {
            replayDbMessageId = replay.db_message_id
            return
          }
          const fullContent = replay.content || ''
          const isV2 = (replay.version ?? 1) >= 2
          if (isV2) {
            // v2 replay: display_sequence items carry part_id and cover the
            // ENTIRE timeline (including the in-progress text tail), so the
            // accumulator takes the full content — no tail carve-out.
            //
            // NEVER shrink the live state on replay: the buffer records
            // events AFTER the client receives them (broadcast → append), so
            // a disconnect can leave the snapshot missing events the client
            // already displayed. Wholesale replacement makes the tail of the
            // answer vanish mid-answer (refresh restores it from the DB).
            // Merge: keep every live timeline item, append snapshot items
            // whose part_id is missing; accumulators take the longer value.
            s.content = pickStreamText({ live: s.content, snapshot: fullContent })
            s.partMode = true
          } else {
            // v1 replay: if the backend's display_sequence already includes
            // text segments, inline rendering shows them. To avoid
            // duplicating the same text in the trailing div.text bubble, set
            // s.content to only the *tail* — the part of full content that
            // hasn't been baked into a {type:'text'} display_sequence item.
            const seqTextLen = (replay.display_sequence || [])
              .filter((it: any) => it && it.type === 'text' && typeof it.content === 'string')
              .reduce((acc: number, it: any) => acc + (it.content as string).length, 0)
            s.content = seqTextLen > 0 && seqTextLen <= fullContent.length
              ? fullContent.slice(seqTextLen)
              : fullContent
          }
          s.reasoning = pickStreamText({ live: s.reasoning, snapshot: replay.reasoning || '' })
          s.contentSegments = replay.content_segments || []
          s.displaySequence = mergeReplayIntoSequence(s.displaySequence, replay.display_sequence || [])
          s.toolCalls = replay.tool_calls || []
          s.toolResults = replay.tool_results || []
          s.agentSteps = replay.agent_steps || []
          s.fileAttachments = replay.file_attachments || []
          s.searchProgress = replay.search_progress || []
          s.searchFailed = replay.search_failed || null
          s.iteration = replay.iteration || null
          if (replay.context_info) {
            setContextInfo(conversationId, replay.context_info)
          }
          if (isV2) {
            _resetPartIndex(conversationId, s.displaySequence)
          }
          replayStatus = replay.status || 'incomplete'
          replayDbMessageId = replay.db_message_id || null
          _streamVersion.value++
        },
        onDone: onDoneCallback,
        onError: (error) => {
          touch()
          // replayStatus 'none' means the backend has no buffer and no live
          // agent for this conversation (typically: the run finished long ago
          // and the buffer expired). Not an error worth surfacing — just
          // resync from the DB so the finalized answer appears.
          if (replayStatus !== 'none') {
            currentError.value = error
          }
          endStreaming(conversationId)
          void refreshConversation(conversationId)
        },
      })

      if (replayStatus === 'complete') {
        if (replayDbMessageId) {
          await refreshConversation(conversationId)
          const existing = messages.value[conversationId] || []
          if (!existing.find(m => m.id === replayDbMessageId)) {
            const assistantMessage: Message = {
              id: replayDbMessageId,
              conversation_id: conversationId,
              role: 'assistant',
              content: s.content || '系统未能生成有效回答，请重新尝试。',
              reasoning_content: s.reasoning || null,
              created_at: new Date().toISOString(),
            }
            messages.value[conversationId] = [...existing, assistantMessage]
          }
        } else {
          const displayContent = s.content || '系统未能生成有效回答，请重新尝试。'
          const existing = messages.value[conversationId] || []
          const hasAssistant = existing.some(m => m.role === 'assistant' && m.content === displayContent)
          if (!hasAssistant) {
            const assistantMessage: Message = {
              id: `resume-complete-${Date.now()}`,
              conversation_id: conversationId,
              role: 'assistant',
              content: displayContent,
              reasoning_content: s.reasoning || null,
              created_at: new Date().toISOString(),
            }
            messages.value[conversationId] = [...existing, assistantMessage]
          }
        }
        endStreaming(conversationId)
      }

      return true
    } catch (e: any) {
      // An aborted resume must NOT be reported as "reconnected" — a watchdog
      // abort means the resume connection died too. Returning false routes the
      // caller into syncAfterAbort so the stream always reaches a terminal
      // state instead of getting stuck at streaming=true with no controller.
      if (e?.name === 'AbortError') return false
      return false
    } finally {
      _resumingSet.delete(conversationId)
      if (s.abortController === abortController) s.abortController = null
    }
  }

  const _recoveryInFlight = new Set<string>()

  async function tryReconnectOrSync(
    conversationId: string,
    expectedMessageCount: number,
    s: StreamState,
  ) {
    // Reentrancy guard: the watchdog safety net may fire while an earlier
    // recovery for the same conversation is between retry attempts.
    if (_recoveryInFlight.has(conversationId)) return
    _recoveryInFlight.add(conversationId)
    // Supersession sentinel: a newer flow (sendMessage / selectConversation)
    // may take over this conversation's stream state while the recovery loop
    // is running. Attaching another resume on top of it would duplicate every
    // event into the same state — the exact double-connection bug that makes
    // streamed text appear 2-3x. Bail as soon as the controller is no longer
    // the one we inherited (an aborted one is fine: aborting clears it).
    const entryController = s.abortController
    try {
      // Bounded retry: a single failed resume may just be an unlucky
      // connection (flaky proxy / network handoff). A fresh resume gets a
      // new connection with fresh pings, so retry before giving up to the
      // poll-and-terminate fallback.
      for (let attempt = 0; attempt < 2; attempt += 1) {
        if (!s.streaming) return
        if (s.abortController && s.abortController !== entryController) return
        try {
          const reconnected = await resumeActiveStream(conversationId)
          if (reconnected) return
        } catch {
          // Resume failed, fall through to retry / syncAfterAbort
        }
        if (attempt < 1) {
          await new Promise<void>(resolve => { window.setTimeout(() => resolve(), 1500) })
        }
      }
      await syncAfterAbort(conversationId, expectedMessageCount, s.content, s.reasoning)
    } finally {
      _recoveryInFlight.delete(conversationId)
    }
  }

  function buildRequestMessages(conversationId: string) {
    return (messages.value[conversationId] || [])
      .filter(message => !localOnlyMessageIds.has(message.id))
      .map(message => ({
        role: message.role,
        content: message.content,
      }))
  }

  // Stale-response guard: loadConversations can be fired concurrently from
  // several places (assistant switch, voice exit, deep links, sidebar mount)
  // and the HTTP responses may resolve OUT OF ORDER — e.g. a slow voice
  // assistant request resolving after the default assistant's request would
  // overwrite the sidebar with voice sessions while in the default assistant.
  // Only the LATEST requested load may apply its result (last-request-wins).
  let conversationsLoadSeq = 0
  async function loadConversations(assistantId?: string | null) {
    const seq = ++conversationsLoadSeq
    try {
      const result = await chatApi.getConversations(assistantId)
      if (seq !== conversationsLoadSeq) return // superseded by a newer load
      conversations.value = result
    } catch (e) {
      if (seq !== conversationsLoadSeq) return
      console.error('Failed to load conversations:', e)
    }
  }

  async function createConversation(title?: string, assistantId?: string | null) {
    try {
      const conversation = await chatApi.createConversation(title, assistantId)
      conversations.value.unshift(conversation)
      currentConversationId.value = conversation.id
      messages.value[conversation.id] = []
      return conversation
    } catch (e) {
      console.error('Failed to create conversation:', e)
      throw e
    }
  }

  async function selectConversation(id: string) {
    if (currentConversationId.value === id) return

    currentConversationId.value = id
    try {
      const conversation = await chatApi.getConversation(id)
      // Cache the fetched conversation metadata independently of the sidebar
      // list: a deep-linked conversation from ANOTHER assistant is (correctly)
      // not injected into the current assistant's list (guard below), but the
      // chat header still needs its title/deathmatch state.
      conversationMeta.value[id] = {
        ...conversationMeta.value[id],
        id: conversation.id,
        title: conversation.title,
        group_id: conversation.group_id,
        assistant_id: conversation.assistant_id,
        deathmatch_mode: conversation.deathmatch_mode,
        deathmatch_status: conversation.deathmatch_status,
        deathmatch_goal: conversation.deathmatch_goal,
        deathmatch_turns: conversation.deathmatch_turns,
        deathmatch_max_turns: conversation.deathmatch_max_turns,
        deathmatch_grilling_round: conversation.deathmatch_grilling_round,
        deathmatch_grilling_round_total: conversation.deathmatch_grilling_round_total,
      } as Conversation
      // Same stale-snapshot guard as refreshConversation: a slow open-time GET
      // resolving after a newer message-list update (e.g. a visibilitychange
      // refresh that already applied the completed answer) must not replace
      // the list with its older open-time snapshot.
      {
        const serverMessages = filterBlankPersistedMessages(conversation.messages)
        const localMessages = messages.value[id] || []
        if (shouldApplyRefresh(localMessages, serverMessages)) {
          messages.value[id] = serverMessages
        }
      }
      // 历史会话无 SSE 快照缓存时，按已加载消息做一次本地估算，保证徽章显式。
      if (!contextInfoByConversation.value[id]) {
        const base = estimateMessagesTokens(conversation.messages || [])
        if (base > 0) setContextInfo(id, { tokens: base, context_length: 0 })
      }
      for (const message of conversation.messages || []) {
        localOnlyMessageIds.delete(message.id)
      }
      // Sync the sidebar title from DB — it may have been generated by
      // the agent background self-save after the page was reloaded.
      const conv = conversations.value.find(item => item.id === id)
      if (conv) {
        if (conversation.title) conv.title = conversation.title
        if (conversation.updated_at) conv.updated_at = conversation.updated_at
        // Sync deathmatch state from backend
        conv.deathmatch_mode = conversation.deathmatch_mode
        conv.deathmatch_status = conversation.deathmatch_status
        conv.deathmatch_goal = conversation.deathmatch_goal
        conv.deathmatch_turns = conversation.deathmatch_turns
        conv.deathmatch_max_turns = conversation.deathmatch_max_turns
      } else if (conversation.assistant_id === useAssistantStore().currentAssistantId) {
        // Guard (conv 6b0faf81, 2026-08-07): only inject the conversation
        // into the sidebar list when it actually belongs to the currently
        // selected assistant. The sidebar renders chatStore.conversations
        // as-is (the list is kept per-assistant by backend filtering in
        // loadConversations), so unshifting a foreign-assistant
        // conversation here leaks it into the current assistant's list —
        // e.g. opening ?conv=<Novel conv> while 默认助手 is selected showed
        // the Novel conversation under 默认助手 until switching assistants.
        // Strict equality (no NULL-assistant allowance): NULL-assistant
        // conversations are excluded from assistant-filtered backend lists
        // (SQL `assistant_id == x` skips NULL), so injecting them here would
        // only produce a flicker until the next loadConversations replaces
        // the list. They are reachable via the meta cache for the header.
        conversations.value.unshift({
          id: conversation.id,
          title: conversation.title || '新对话',
          group_id: conversation.group_id ?? null,
          assistant_id: conversation.assistant_id ?? null,
          sort_order: conversation.sort_order ?? 0,
          created_at: conversation.created_at,
          updated_at: conversation.updated_at,
          last_user_message_at: conversation.last_user_message_at,
          deathmatch_mode: conversation.deathmatch_mode,
          deathmatch_status: conversation.deathmatch_status,
          deathmatch_goal: conversation.deathmatch_goal,
          deathmatch_turns: conversation.deathmatch_turns,
          deathmatch_max_turns: conversation.deathmatch_max_turns,
          deathmatch_grilling_total: conversation.deathmatch_grilling_total ?? 0,
          deathmatch_grilling_completed: conversation.deathmatch_grilling_completed ?? 0,
          deathmatch_grilling_round: conversation.deathmatch_grilling_round ?? 0,
          deathmatch_grilling_round_total: conversation.deathmatch_grilling_round_total ?? 0,
        } as Conversation)
      }
      // Restore deathmatchMode from conversation state
      deathmatchMode.value = !!(conversation.deathmatch_mode && conversation.deathmatch_status !== 'inactive' && conversation.deathmatch_status !== 'done' && conversation.deathmatch_status !== 'cleared')
      deathmatchAction.value = null
    } catch (e) {
      console.error('Failed to load messages:', e)
      if (!messages.value[id]) {
        messages.value[id] = []
      }
    }

    try {
      const status = await chatApi.getStreamStatus(id)
      if (status.has_buffer && status.status === 'incomplete' && status.is_running) {
        beginStreaming(id)
        await resumeActiveStream(id)
      } else if (status.has_buffer && status.status === 'complete' && status.db_message_id) {
        const existing = messages.value[id] || []
        if (!existing.find(m => m.id === status.db_message_id)) {
          await refreshConversation(id)
        }
      } else if (status.has_buffer && status.status === 'complete' && !status.db_message_id) {
        const existing = messages.value[id] || []
        const hasAssistant = existing.some(m => m.role === 'assistant')
        if (!hasAssistant && status.content_length > 0) {
          beginStreaming(id)
          await resumeActiveStream(id)
        }
      }
    } catch {
      // Stream status check failed, ignore
    }
  }

  /** Cross-assistant move: drop the conversation from the current assistant's
   *  sidebar list without touching messages/meta (the conversation still
   *  exists — it now belongs to another assistant). */
  function removeConversationFromList(id: string) {
    conversations.value = conversations.value.filter(c => c.id !== id)
  }

  async function deleteConversation(id: string) {
    try {
      await chatApi.deleteConversation(id)
      conversations.value = conversations.value.filter(c => c.id !== id)
      delete messages.value[id]
      delete conversationMeta.value[id]
      dropContextInfo(id)
      const s = streamStates[id]
      if (s?.abortController) {
        try { s.abortController.abort() } catch {}
      }
      delete streamStates[id]
      if (currentConversationId.value === id) {
        currentConversationId.value = null
      }
    } catch (e) {
      console.error('Failed to delete conversation:', e)
      throw e
    }
  }

  async function bulkDeleteConversations(ids: string[]) {
    if (ids.length === 0) return

    try {
      await chatApi.bulkDeleteConversations(ids)
      const idSet = new Set(ids)
      conversations.value = conversations.value.filter(c => !idSet.has(c.id))

      for (const id of ids) {
        delete messages.value[id]
        delete conversationMeta.value[id]
        dropContextInfo(id)
        const s = streamStates[id]
        if (s?.abortController) { try { s.abortController.abort() } catch {} }
        delete streamStates[id]
      }

      if (currentConversationId.value && idSet.has(currentConversationId.value)) {
        currentConversationId.value = null
      }
    } catch (e) {
      console.error('Failed to bulk delete conversations:', e)
      throw e
    }
  }

  async function updateConversationTitle(id: string, title: string) {
    try {
      const updated = await chatApi.updateConversation(id, { title })
      const conv = conversations.value.find(c => c.id === id)
      if (conv) {
        conv.title = updated.title
      }
      return updated
    } catch (e) {
      console.error('Failed to update conversation title:', e)
      throw e
    }
  }

  // F1-1: part_id → timeline item index per conversation (opencode-style
  // reducer semantics: part_started pushes ONCE; part_delta / part_updated
  // mutate the same slot IN PLACE — a tool call's whole
  // pending→running→completed/error lifecycle occupies one array position).
  const _partIndexes = new Map<string, Map<string, DisplaySequenceItem>>()

  function _partIndex(convId: string): Map<string, DisplaySequenceItem> {
    let idx = _partIndexes.get(convId)
    if (!idx) {
      idx = new Map()
      _partIndexes.set(convId, idx)
    }
    return idx
  }

  function _resetPartIndex(convId: string, items: DisplaySequenceItem[]) {
    const idx = _partIndex(convId)
    idx.clear()
    for (const item of items) {
      if (item.part_id) idx.set(item.part_id, item)
    }
  }

  function applyPartStarted(s: StreamState, convId: string, part: PartStartedEvent) {
    s.partMode = true
    // Reducer semantics: part_started pushes ONCE. A duplicate part_started
    // for the same part_id (e.g. a stale second SSE connection delivering the
    // same event) must be a no-op — pushing again creates duplicate v-for
    // keys, which double-renders text and drops tool cards.
    const idx = _partIndex(convId)
    if (part.part_id && idx.has(part.part_id)) return
    const item: DisplaySequenceItem = {
      type: part.part_type || 'text',
      part_id: part.part_id,
      content: part.content ?? '',
    }
    if (part.call_id != null) item.call_id = part.call_id
    if (part.name != null) item.name = part.name
    if (part.title != null) item.title = part.title
    if (part.step_type != null) item.step_type = part.step_type
    if (part.status != null) item.status = part.status
    if (part.arguments != null) item.arguments = part.arguments
    if (part.subtask_id != null) item.subtask_id = part.subtask_id
    if (part.subtask_name != null) item.subtask_name = part.subtask_name
    s.displaySequence.push(item)
    // Store the REACTIVE proxy (read back out of the reactive array) so later
    // in-place mutations through the index fire Vue reactivity.
    idx.set(part.part_id, s.displaySequence[s.displaySequence.length - 1])
  }

  function applyPartDelta(s: StreamState, convId: string, part: PartDeltaEvent) {
    s.partMode = true
    let item = _partIndex(convId).get(part.part_id)
    if (!item) {
      // Delta-before-start tolerance (opencode #26924): synthesize the slot
      // instead of dropping the chunk.
      applyPartStarted(s, convId, { part_id: part.part_id, part_type: part.part_type || 'text' })
      item = _partIndex(convId).get(part.part_id)!
    }
    const field = (part.field || 'content') as 'content' | 'reasoning_content'
    item[field] = ((item[field] as string | undefined) || '') + part.delta
  }

  function applyPartUpdated(s: StreamState, convId: string, part: PartUpdatedEvent) {
    s.partMode = true
    let item = part.part_id ? _partIndex(convId).get(part.part_id) : undefined
    if (!item && part.call_id) {
      for (let i = s.displaySequence.length - 1; i >= 0; i--) {
        const cand = s.displaySequence[i]
        if (cand.type === 'tool_call' && cand.call_id === part.call_id) {
          item = cand
          break
        }
      }
    }
    if (!item) return
    if (part.status != null) item.status = part.status
    if (part.result != null) item.result = part.result
    if (part.error != null) item.error = part.error
    if (part.content != null) item.content = part.content
    if (part.title != null) item.title = part.title
  }

  function wireStreamCallbacks(conversationId: string, onFinalize: (messageId: string, title: string | undefined, toolResults: string | null | undefined, searchFailed: boolean | undefined, taskSubmitted?: boolean) => void) {
    const s = getStream(conversationId)
    const touch = () => { s._lastEventTime = Date.now(); s._gotFirstEvent = true }
    // Debounced reload of the notes store after the agent's notes tool mutates
    // notes/notebooks server-side, so sidebars & lists stay in sync.
    let notesRefreshTimer: ReturnType<typeof setTimeout> | null = null
    function scheduleNotesRefresh() {
      if (notesRefreshTimer) clearTimeout(notesRefreshTimer)
      notesRefreshTimer = setTimeout(() => {
        useNotesStore().refreshFromExternalChange()
      }, 500)
    }
    return {
      onMessage: (chunk: string) => {
        touch()
        s.content += chunk
        addContextTokenDelta(conversationId, chunk)
        if (s.partMode) return
        const seq = s.displaySequence
        const last = seq.length > 0 ? seq[seq.length - 1] : null
        if (last && last.type === 'text') {
          last.content += chunk
        } else {
          seq.push({ type: 'text', content: chunk })
        }
      },
      onDone: (newConvId: string, messageId: string, title?: string, toolResults?: string | null, searchFailed?: boolean, taskSubmitted?: boolean) => {
        touch()
        onFinalize(messageId, title, toolResults, searchFailed, taskSubmitted)
        endStreaming(conversationId)
        if (title) {
          const conv = conversations.value.find(c => c.id === newConvId)
          if (conv) conv.title = title
        }
      },
      onError: (error: string) => {
        touch()
        currentError.value = error
        endStreaming(conversationId)
        void refreshConversation(conversationId)
        // The backend persists a visible failure message AFTER emitting the
        // error event (error-terminated loop); the immediate refresh above
        // usually races the save. Re-refresh once so the failure bubble
        // appears without a manual page reload. Captured epoch: if a newer
        // flow (edit-resend / send) took over while the retry was scheduled,
        // skip the refresh — its pre-commit snapshot would otherwise clobber
        // the new flow's optimistic bubbles (same bug class as syncAfterAbort
        // Phase-1 clobbering, via the error path).
        const scheduledEpoch = messagesEpoch[conversationId] || 0
        window.setTimeout(() => {
          if ((messagesEpoch[conversationId] || 0) !== scheduledEpoch) return
          void refreshConversation(conversationId)
        }, 1500)
      },
      onReasoning: (chunk: string) => {
        touch()
        s.reasoning += chunk
        addContextTokenDelta(conversationId, chunk)
        if (s.partMode) return
        const seq = s.displaySequence
        const last = seq.length > 0 ? seq[seq.length - 1] : null
        if (last && last.type === 'reasoning') {
          last.content += chunk
        } else {
          seq.push({ type: 'reasoning', content: chunk })
        }
      },
      onPartStarted: (part: PartStartedEvent) => { touch(); applyPartStarted(s, conversationId, part) },
      onPartDelta: (part: PartDeltaEvent) => { touch(); applyPartDelta(s, conversationId, part) },
      onPartUpdated: (part: PartUpdatedEvent) => { touch(); applyPartUpdated(s, conversationId, part) },
      onToolStatus: (status: ToolStatus) => { touch(); s.toolStatuses = [...s.toolStatuses, status] },
      onSearchProgress: (progress: SearchProgress) => {
        touch()
        const idx = s.searchProgress.findIndex(p => p.round === progress.round)
        if (idx >= 0) s.searchProgress[idx] = progress
        else s.searchProgress = [...s.searchProgress, progress]
      },
      onSearchFailed: (failed: SearchFailed) => { touch(); s.searchFailed = failed },
      onAgentStep: (step: AgentStep) => {
        touch()
        s.agentSteps = [...s.agentSteps, step]
        if (s.partMode) return
        s.displaySequence = [...s.displaySequence, { type: step.step_type || 'tool', ...step }]
      },
      onTitleUpdate: (cid: string, t: string) => {
        touch()
        const conv = conversations.value.find(c => c.id === cid)
        if (conv) conv.title = t
      },
      onSubAgentThinking: (thinking: SubAgentThinking) => { touch(); s.subAgentThinking = thinking },
      onFileAttachment: (attachments: FileAttachment[]) => { touch(); s.fileAttachments = [...attachments] },
      onTaskProgress: (tp: TaskProgress) => { touch(); s.taskProgress = tp },
      onSubAgentChunk: (chunk: { subtask_id: string; subtask_name: string; kind: 'content' | 'reasoning'; delta: string }) => {
        touch()
        const bucket = s.subAgentChunks[chunk.subtask_id] || { content: '', reasoning: '', name: chunk.subtask_name }
        bucket.name = chunk.subtask_name
        if (chunk.kind === 'content') bucket.content += chunk.delta
        else bucket.reasoning += chunk.delta
        s.subAgentChunks = { ...s.subAgentChunks, [chunk.subtask_id]: bucket }
        const seq = s.displaySequence
        let found = false
        for (let i = seq.length - 1; i >= 0; i--) {
          if (seq[i].type === 'sub_agent_chunk' && seq[i].subtask_id === chunk.subtask_id) {
            if (chunk.kind === 'content') seq[i].content += chunk.delta
            else seq[i].reasoning_content = (seq[i].reasoning_content || '') + chunk.delta
            found = true
            break
          }
        }
        if (!found) {
          seq.push({
            type: 'sub_agent_chunk',
            content: chunk.kind === 'content' ? chunk.delta : '',
            reasoning_content: chunk.kind === 'reasoning' ? chunk.delta : '',
            subtask_id: chunk.subtask_id,
            subtask_name: chunk.subtask_name,
          })
        }
      },
      onToolCall: (tc: ToolCallEvent) => {
        touch()
        s.toolCalls = [...s.toolCalls, tc]
        if (s.partMode) return
        s.displaySequence.push({
          type: 'tool_call',
          content: '',
          call_id: tc.call_id,
          name: tc.name,
          status: 'running',
          arguments: tc.arguments,
        })
      },
      onToolResult: (tr: ToolResultEvent) => {
        touch()
        s.toolResults = [...s.toolResults, tr]
        if (!s.partMode) {
          const seq = s.displaySequence
          for (let i = seq.length - 1; i >= 0; i--) {
            if (seq[i].type === 'tool_call' && seq[i].call_id === tr.call_id) {
              seq[i].status = tr.error ? 'error' : 'completed'
              seq[i].result = tr.result
              seq[i].error = tr.error
              break
            }
          }
        }
        if (tr.name === 'notes') scheduleNotesRefresh()
      },
      onIteration: (it: IterationEvent) => { touch(); s.iteration = it },
      onContextInfo: (info: ContextInfo) => {
        touch()
        setContextInfo(conversationId, info)
      },
      onContentSegment: (seg: string) => {
        touch()
        addContextTokenDelta(conversationId, seg)
        if (!s.partMode && s.content.trim()) {
          s.displaySequence = [...s.displaySequence, { type: 'text', content: s.content }]
          s.content = ''
        }
        if (seg.trim()) {
          s.contentSegments = [...s.contentSegments, seg]
        }
      },
      onDeathmatchVerdict: (verdict: DeathmatchVerdict) => {
        touch()
        s.deathmatchVerdict = verdict
        if (verdict.status === 'active') {
          deathmatchMode.value = true
          grillingQuestions.value = []
          grillingAnswers.value = {}
          startDeathmatchPolling()
        } else if (verdict.status === 'grilling') {
          deathmatchMode.value = true
          startDeathmatchPolling()
        } else if (verdict.status === 'partial_complete') {
          // Partial completion: stop auto-continuation but keep deathmatch mode
          // on so the user can resume by sending a message.
          deathmatchMode.value = true
          stopDeathmatchPolling()
        } else if (verdict.status === 'done' || verdict.status === 'inactive' || verdict.status === 'cleared') {
          deathmatchMode.value = false
          grillingQuestions.value = []
          grillingAnswers.value = {}
          stopDeathmatchPolling()
        }
        if (verdict.grilling_questions && verdict.grilling_questions.length > 0) {
          const incomingRound = verdict.grilling_round || 1
          const currentMaxRound = grillingQuestions.value.length > 0
            ? Math.max(...grillingQuestions.value.map(q => q.round || 1))
            : 0
          if (currentMaxRound === 0 || incomingRound >= currentMaxRound) {
            grillingQuestions.value = verdict.grilling_questions.map((q: any) => ({
              task_id: q.task_id,
              question_id: q.question_id,
              question: q.question,
              recommendation: q.recommendation,
              options: q.options || [],
              round: q.round || verdict.grilling_round || 1,
              status: q.status,
              answer: q.answer,
            }))
          }
        }
        const conv = conversations.value.find(item => item.id === conversationId)
        if (conv) {
          conv.deathmatch_mode = deathmatchMode.value
          conv.deathmatch_status = verdict.status
          conv.deathmatch_turns = verdict.turns
          conv.deathmatch_max_turns = verdict.max_turns
          conv.deathmatch_grilling_round = verdict.grilling_round
          conv.deathmatch_grilling_round_total = verdict.grilling_round_total
        }
      },
      onPermissionRequest: (request: { request_id: string; tool_name: string; description: string; details: Record<string, any> }) => {
        touch()
        pendingPermissionRequest.value = request
      },
      onPing: () => { touch() },
    }
  }

  async function fetchGrillingQuestions(convId: string) {
    try {
      const resp = await fetch(`/api/agent-tasks/grilling/${convId}`, {
        headers: { 'Authorization': `Bearer ${localStorage.getItem('chatllm_token')}` }
      })
      if (resp.ok) {
        const data = await resp.json()
        if (data.questions && data.questions.length > 0) {
          grillingQuestions.value = data.questions.map((q: any) => ({
            task_id: q.task_id,
            question_id: q.question_id,
            question: q.question,
            recommendation: q.recommendation,
            options: q.options || [],
            round: q.round || 1,
            status: q.status,
            answer: q.answer,
          }))
          for (const q of data.questions) {
            if (q.answer) {
              grillingAnswers.value[q.task_id] = q.answer
            }
          }
        }
      }
    } catch (e) {
      console.error('Failed to fetch grilling questions', e)
    }
  }

  async function submitGrillingRound(answers: { task_id: string; answer: string }[]) {
    const convId = currentConversationId.value
    if (!convId) return null
    try {
      // Optimistically mark answers locally.
      for (const a of answers) {
        grillingAnswers.value[a.task_id] = a.answer
      }
      const resp = await fetch(`/api/agent-tasks/grilling/${convId}/round-answer`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('chatllm_token')}`
        },
        body: JSON.stringify({ answers })
      })
      if (resp.ok) {
        const data = await resp.json()

        const conv = conversations.value.find(item => item.id === convId)
        if (conv) {
          conv.deathmatch_status = data.deathmatch_status ?? conv.deathmatch_status
          conv.deathmatch_grilling_completed = data.grilling_completed ?? conv.deathmatch_grilling_completed
          conv.deathmatch_grilling_total = data.grilling_total ?? conv.deathmatch_grilling_total
          conv.deathmatch_grilling_round = data.grilling_round ?? conv.deathmatch_grilling_round
          conv.deathmatch_grilling_round_total = data.grilling_round_total ?? conv.deathmatch_grilling_round_total
        }

        if (data.result?.status === 'grilling_complete') {
          const goal = data.result.goal || ''
          grillingQuestions.value = []
          grillingAnswers.value = {}
          deathmatchMode.value = true
          deathmatchAction.value = null

          const stream = getStream(convId)
          stream.deathmatchVerdict = {
            status: 'active',
            verdict: null,
            reason: null,
            turns: 0,
            max_turns: 30,
            grilling_completed: data.grilling_completed || 0,
            grilling_total: data.grilling_total || 0,
            grilling_round: data.grilling_round || 0,
            grilling_round_total: data.grilling_round_total || 3,
            message: '目标已明确，正在开始执行...',
          }
          _streamVersion.value++
          await sendMessage(`目标已明确，请开始执行：${goal}`)
        } else if (data.result?.status === 'next_round') {
          // Render next round questions.
          grillingQuestions.value = (data.result.questions || []).map((q: any) => ({
            task_id: q.task_id,
            question_id: q.question_id,
            question: q.question,
            recommendation: q.recommendation,
            options: q.options || [],
            round: q.round || data.result.round || 1,
          }))
          grillingAnswers.value = {}
          const stream = getStream(convId)
          if (stream.deathmatchVerdict) {
            const { grilling_questions: _gq, ...prevVerdict } = stream.deathmatchVerdict
            stream.deathmatchVerdict = {
              ...prevVerdict,
              status: 'grilling',
              grilling_completed: data.grilling_completed || 0,
              grilling_total: data.grilling_total || 0,
              grilling_round: data.grilling_round || 0,
              grilling_round_total: data.grilling_round_total || 3,
              message: `盘问阶段 第${data.result.round}/${data.result.max_rounds}轮`,
            }
            _streamVersion.value++
          }
        }
        return data
      }
    } catch (e) {
      console.error('Failed to submit grilling round', e)
    }
    return null
  }

  async function answerGrillingQuestion(taskId: string, answer: string) {
    try {
      grillingAnswers.value[taskId] = answer
      const resp = await fetch(`/api/agent-tasks/grilling/${taskId}/answer`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('chatllm_token')}`
        },
        body: JSON.stringify({ answer })
      })
      if (resp.ok) {
        const data = await resp.json()

        // Sync conversation-level deathmatch fields immediately
        const conv = conversations.value.find(item => item.id === currentConversationId.value)
        if (conv) {
          conv.deathmatch_status = data.deathmatch_status ?? conv.deathmatch_status
          conv.deathmatch_grilling_completed = data.grilling_completed ?? conv.deathmatch_grilling_completed
          conv.deathmatch_grilling_total = data.grilling_total ?? conv.deathmatch_grilling_total
        }

        if (data.result?.status === 'grilling_complete') {
          const goal = data.result.goal || ''

          // Step 1: Clear grilling UI state
          grillingQuestions.value = []
          grillingAnswers.value = {}
          deathmatchMode.value = true
          deathmatchAction.value = null

          // Step 2: Set verdict to active BEFORE sendMessage/beginStreaming
          const convId = currentConversationId.value
          if (convId) {
            const stream = getStream(convId)
            stream.deathmatchVerdict = {
              status: 'active',
              verdict: null,
              reason: null,
              turns: 0,
              max_turns: 30,
              grilling_completed: data.grilling_completed || 0,
              grilling_total: data.grilling_total || 0,
              message: '目标已明确，正在开始执行...',
            }
            _streamVersion.value++

            // Step 3: Send the goal message.
            await sendMessage(`目标已明确，请开始执行：${goal}`)
          }
        } else if (data.result?.status === 'next_round') {
          // Per-question flow: backend auto-advanced to the next round.
          grillingQuestions.value = (data.result.questions || []).map((q: any) => ({
            task_id: q.task_id,
            question_id: q.question_id,
            question: q.question,
            recommendation: q.recommendation,
            options: q.options || [],
            round: q.round || data.result.round || 1,
          }))
          grillingAnswers.value = {}
          const stream = getStream(currentConversationId.value!)
          if (stream.deathmatchVerdict) {
            const { grilling_questions: _gq, ...prevVerdict } = stream.deathmatchVerdict
            stream.deathmatchVerdict = {
              ...prevVerdict,
              status: 'grilling',
              grilling_completed: data.grilling_completed || 0,
              grilling_total: data.grilling_total || 0,
              grilling_round: data.grilling_round || 0,
              grilling_round_total: data.grilling_round_total || 3,
              message: `盘问阶段 第${data.result.round}/${data.result.max_rounds}轮`,
            }
            _streamVersion.value++
          }
        } else {
          // Non-final answer: sync grilling progress counters
          const convId = currentConversationId.value
          if (convId) {
            const stream = getStream(convId)
            if (stream.deathmatchVerdict) {
              stream.deathmatchVerdict = {
                ...stream.deathmatchVerdict,
                grilling_completed: data.grilling_completed ?? stream.deathmatchVerdict.grilling_completed,
                grilling_total: data.grilling_total ?? stream.deathmatchVerdict.grilling_total,
                status: data.deathmatch_status ?? stream.deathmatchVerdict.status,
              }
              _streamVersion.value++
            }
          }
        }
        return data
      }
    } catch (e) {
      console.error('Failed to answer grilling question', e)
    }
    return null
  }

  async function sendMessage(content: string, assistantId?: string | null, _busyAttempts = 0) {
    if (!currentConversationId.value) {
      await createConversation(undefined, assistantId)
    }

    const conversationId = currentConversationId.value!
    const userMessage: Message = {
      id: `temp-${Date.now()}`,
      conversation_id: conversationId,
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    }

    if (!messages.value[conversationId]) messages.value[conversationId] = []
    messages.value[conversationId].push(userMessage)
    addContextTokenDelta(conversationId, content)
    beginStreaming(conversationId)

    const abortController = new AbortController()
    const s = getStream(conversationId)
    s.abortController = abortController
    const expectedMessageCount = messages.value[conversationId].length + 1

    const callbacks = wireStreamCallbacks(conversationId, (messageId, _title, toolResults, searchFailed, taskSubmitted) => {
      const finalContent = s.content
      const finalReasoning = s.reasoning
      const displayContent = finalContent
        || (taskSubmitted ? '任务已提交至后台执行，点击上方「后台任务」面板可查看进度。' : '')
        || (searchFailed ? '联网检索完成，但检索质量未达标。' : '系统未能生成有效回答，请重新尝试。')
      const assistantMessage: Message = {
        id: messageId || `bg-${Date.now()}`,
        conversation_id: conversationId,
        role: 'assistant',
        content: displayContent,
        reasoning_content: finalReasoning || null,
        tool_results: toolResults || null,
        created_at: new Date().toISOString(),
      }
      messages.value[conversationId].push(assistantMessage)
      deathmatchAction.value = null
    })

    try {
      const doStream = () => chatApi.streamChat(
        {
          conversation_id: conversationId,
          assistant_id: assistantId,
          messages: buildRequestMessages(conversationId),
          enable_reasoning: enableReasoning.value,
          reasoning_effort: reasoningEffort.value,
          thinking_budget: useAssistantStore().getAssistantById(assistantId ?? '')?.thinking_budget ?? thinkingBudget.value,
          deathmatch_mode: deathmatchMode.value,
          deathmatch_action: deathmatchAction.value,
        },
        {
          ...callbacks,
          signal: abortController.signal,
          onConversationBusy: () => { busyRejected = true },
          onConversationSuperseded: () => { supersededRejected = true },
        },
      )

      // 4.8 session lock: the backend rejects a second concurrent agent on
      // the same conversation. Cancel the stale run and retry once; if it is
      // genuinely live (another tab/device), attach to it via resume and
      // QUEUE the message — auto-send once the in-flight run completes
      // (opencode run-merge semantics; the user's text is never dropped).
      let busyRejected = false
      let supersededRejected = false
      await doStream()
      if (supersededRejected) {
        // Silent takeover (conversation_superseded): a newer request owns
        // the conversation. No error bubble — end this stale stream and
        // resync from the DB so the newer run's answer (and this orphaned
        // user message) both appear. Never loop here.
        endStreaming(conversationId)
        void refreshConversation(conversationId)
        return
      }
      if (busyRejected) {
        await chatApi.stopStream(conversationId)
        await new Promise<void>(resolve => { window.setTimeout(() => resolve(), 800) })
        busyRejected = false
        await doStream()
        if (supersededRejected) {
          endStreaming(conversationId)
          void refreshConversation(conversationId)
          return
        }
        if (busyRejected) {
          // The other run is still alive — the new message never reached an
          // agent. Roll back the optimistic local echo, attach to the
          // in-flight stream, then re-send once it completes.
          const arr = messages.value[conversationId] || []
          if (arr.length && arr[arr.length - 1].id === userMessage.id) {
            messages.value[conversationId] = arr.slice(0, -1)
          }
          const attached = await resumeActiveStream(conversationId)
          if (attached && _busyAttempts < 2) {
            // The attached run has now finished (resume resolves at done) —
            // the slot is free, deliver the queued message. Capped at 2
            // resends so a persistent busy/resume disagreement (e.g.
            // cross-worker claim churn) can't loop stopStream forever.
            await sendMessage(content, assistantId, _busyAttempts + 1)
          } else {
            if (!attached) {
              currentError.value = '该会话已有正在进行的回答，请稍后重试。'
              endStreaming(conversationId)
            } else {
              // Attached but resend cap exhausted — never drop the user's
              // intent silently; surface it so they can resend manually.
              currentError.value = '消息未能送达：当前回答占用时间过长，请稍后重新发送。'
            }
          }
          return
        }
      }

      if (s.streaming) {
        await tryReconnectOrSync(conversationId, expectedMessageCount, s)
        return
      }
      // Sync temp user-message IDs and any server-side state back into the store
      // so subsequent edits/regenerations reference real DB IDs.
      await refreshConversation(conversationId)
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        if (s.abortController !== abortController) {
          return
        }
        if (s.tabSwitchAbort) {
          s.tabSwitchAbort = false
          return
        }
        // Watchdog abort = the connection died silently. Prefer reconnecting
        // (resume replays the buffer and continues the live stream); fall back
        // to syncAfterAbort only when resume is impossible.
        await tryReconnectOrSync(conversationId, expectedMessageCount, s)
        return
      }
      // 非 AbortError（如 TypeError / 网络断连）：
      // 不要立即 endStreaming——visibilitychange handler 会
      // 在 tab 恢复时通过 resume 接管。此处只 fallback 到 sync。
      if (s.streaming) {
        await tryReconnectOrSync(conversationId, expectedMessageCount, s)
      } else {
        currentError.value = e.message || '发送失败'
        await refreshConversation(conversationId)
      }
    } finally {
      if (s.abortController === abortController) s.abortController = null
    }
  }

  async function regenerateLastAssistantMessage() {
    const convId = currentConversationId.value
    if (!convId) return
    const s = getStream(convId)
    if (s.streaming) return

    const existingMessages = messages.value[convId] || []
    const targetIndex = [...existingMessages]
      .map((message, index) => ({ message, index }))
      .reverse()
      .find(item => item.message.role === 'assistant')?.index
    if (targetIndex === undefined) return

    const targetMessage = existingMessages[targetIndex]
    messages.value[convId] = existingMessages.slice(0, targetIndex)
    beginStreaming(convId)

    const abortController = new AbortController()
    s.abortController = abortController
    const expectedMessageCount = messages.value[convId].length + 1

    const callbacks = wireStreamCallbacks(convId, (messageId, _title, toolResults, searchFailed) => {
      const assistantMessage: Message = {
        id: messageId,
        conversation_id: convId,
        role: 'assistant',
        content: s.content || (searchFailed ? '联网检索完成，但检索质量未达标。' : ''),
        reasoning_content: s.reasoning || null,
        tool_results: toolResults || null,
        created_at: new Date().toISOString(),
      }
      messages.value[convId].push(assistantMessage)
    })

    try {
      const assistantStore = useAssistantStore()
      let supersededRejected = false
      await chatApi.streamChat(
        {
          conversation_id: convId,
          assistant_id: assistantStore.currentAssistantId,
          regenerate_from_message_id: targetMessage.id,
          messages: buildRequestMessages(convId),
          enable_reasoning: enableReasoning.value,
          reasoning_effort: reasoningEffort.value,
          thinking_budget: assistantStore.getAssistantById(assistantStore.currentAssistantId ?? '')?.thinking_budget ?? thinkingBudget.value,
        },
        {
          ...callbacks,
          signal: abortController.signal,
          onConversationSuperseded: () => { supersededRejected = true },
        },
      )
      if (supersededRejected) {
        // Silent takeover: a newer request owns the conversation — no error
        // bubble; resync from the DB (same semantics as sendMessage).
        endStreaming(convId)
        void refreshConversation(convId)
        return
      }
      if (s.streaming) {
        await tryReconnectOrSync(convId, expectedMessageCount, s)
        return
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        if (s.abortController !== abortController) {
          return
        }
        if (s.tabSwitchAbort) {
          s.tabSwitchAbort = false
          return
        }
        // Watchdog abort = the connection died silently. Prefer reconnecting
        // (resume replays the buffer and continues the live stream); fall back
        // to syncAfterAbort only when resume is impossible.
        await tryReconnectOrSync(convId, expectedMessageCount, s)
        return
      }
      currentError.value = e.message || '重新生成失败'
      endStreaming(convId)
      await refreshConversation(convId)
    } finally {
      if (s.abortController === abortController) s.abortController = null
    }
  }

  async function regenerateWithForceResults(messageId: string, forceSearchResults: string) {
    const convId = currentConversationId.value
    if (!convId) return
    const s = getStream(convId)
    if (s.streaming) return

    const existingMessages = messages.value[convId] || []
    const targetIndex = existingMessages.findIndex(m => m.id === messageId)
    if (targetIndex === -1) return

    messages.value[convId] = existingMessages.slice(0, targetIndex)
    beginStreaming(convId)

    const abortController = new AbortController()
    s.abortController = abortController
    const expectedMessageCount = messages.value[convId].length + 1

    const callbacks = wireStreamCallbacks(convId, (newMessageId, _title, toolResults) => {
      const assistantMessage: Message = {
        id: newMessageId,
        conversation_id: convId,
        role: 'assistant',
        content: s.content,
        reasoning_content: s.reasoning || null,
        tool_results: toolResults || null,
        created_at: new Date().toISOString(),
      }
      messages.value[convId].push(assistantMessage)
    })

    try {
      const assistantStore = useAssistantStore()
      let supersededRejected = false
      await chatApi.streamChat(
        {
          conversation_id: convId,
          assistant_id: assistantStore.currentAssistantId,
          regenerate_from_message_id: messageId,
          force_search_results: forceSearchResults,
          messages: buildRequestMessages(convId),
          enable_reasoning: enableReasoning.value,
          reasoning_effort: reasoningEffort.value,
          thinking_budget: assistantStore.getAssistantById(assistantStore.currentAssistantId ?? '')?.thinking_budget ?? thinkingBudget.value,
        },
        {
          ...callbacks,
          signal: abortController.signal,
          onConversationSuperseded: () => { supersededRejected = true },
        },
      )
      if (supersededRejected) {
        // Silent takeover: a newer request owns the conversation — no error
        // bubble; resync from the DB (same semantics as sendMessage).
        endStreaming(convId)
        void refreshConversation(convId)
        return
      }
      if (s.streaming) {
        await tryReconnectOrSync(convId, expectedMessageCount, s)
        return
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        if (s.abortController !== abortController) {
          return
        }
        if (s.tabSwitchAbort) {
          s.tabSwitchAbort = false
          return
        }
        // Watchdog abort = the connection died silently. Prefer reconnecting
        // (resume replays the buffer and continues the live stream); fall back
        // to syncAfterAbort only when resume is impossible.
        await tryReconnectOrSync(convId, expectedMessageCount, s)
        return
      }
      currentError.value = e.message || '重新生成失败'
      endStreaming(convId)
      await refreshConversation(convId)
    } finally {
      if (s.abortController === abortController) s.abortController = null
    }
  }

  async function editAndResendMessage(editMessageId: string, newContent: string, assistantId?: string | null) {
    const convId = currentConversationId.value
    if (!convId) return
    const s = getStream(convId)
    // If a stream is still active on this conversation (e.g. user clicked edit
    // before the previous turn fully settled), abort it so the edit can proceed.
    // Otherwise the input is already cleared and the user sees no response.
    if (s.streaming) {
      if (s.abortController) {
        try { s.abortController.abort() } catch {}
      }
      // 4.8 session lock: the detached backend agent keeps running after a
      // local abort (by design) and would reject the edit request as
      // conversation_busy. Explicitly cancel it first, then give the cancel
      // a moment to register before posting.
      await chatApi.stopStream(convId)
      await new Promise<void>(resolve => { window.setTimeout(() => resolve(), 800) })
      endStreaming(convId)
    }

    // Capture the edit target BEFORE refresh: after a quick stop the store may
    // still hold the optimistic temp-* id (the usual post-stream refresh that
    // reconciles temp ids never ran), and refreshConversation replaces the
    // array with server messages carrying different (real) ids. Falling back
    // to content/position matching keeps the edit target resolvable so the
    // resend is never silently dropped.
    const preRefreshMessages = messages.value[convId] || []
    const preRefreshIndex = preRefreshMessages.findIndex(m => m.id === editMessageId)
    const preRefreshTarget = preRefreshIndex >= 0 ? preRefreshMessages[preRefreshIndex] : null

    // Refresh conversation first to ensure we have real DB IDs
    await refreshConversation(convId)

    const existingMessages = messages.value[convId] || []
    let targetIndex = existingMessages.findIndex(m => m.id === editMessageId)
    if (targetIndex === -1 && preRefreshTarget && editMessageId.startsWith('temp-')) {
      // temp id never reconciled: locate the server row for the edited message
      // (same content, most recent occurrence). Bounded to temp ids — a real
      // id missing after refresh means the message was genuinely deleted, and
      // editing some other duplicate would be wrong.
      const contentMatches = existingMessages
        .map((m, i) => ({ m, i }))
        .filter(({ m }) => m.role === 'user' && m.content === preRefreshTarget.content)
      if (contentMatches.length > 0) {
        targetIndex = contentMatches[contentMatches.length - 1].i
      } else if (preRefreshIndex >= 0 && preRefreshIndex < existingMessages.length) {
        targetIndex = preRefreshIndex
      }
    }
    if (targetIndex === -1) return

    const realEditMessageId = existingMessages[targetIndex].id
    const preEditMessages = [...existingMessages]
    messages.value[convId] = existingMessages.slice(0, targetIndex)
    const userMessage: Message = {
      id: `temp-edit-${Date.now()}`,
      conversation_id: convId,
      role: 'user',
      content: newContent,
      created_at: new Date().toISOString(),
    }
    messages.value[convId].push(userMessage)

    beginStreaming(convId)
    const abortController = new AbortController()
    s.abortController = abortController
    const expectedMessageCount = messages.value[convId].length + 1

    const callbacks = wireStreamCallbacks(convId, (messageId, _title, toolResults, searchFailed) => {
      const assistantMessage: Message = {
        id: messageId,
        conversation_id: convId,
        role: 'assistant',
        content: s.content || (searchFailed ? '联网检索完成，但检索质量未达标。' : ''),
        reasoning_content: s.reasoning || null,
        tool_results: toolResults || null,
        created_at: new Date().toISOString(),
      }
      void refreshConversation(convId).then(() => {
        const current = messages.value[convId] || []
        if (!current.find(m => m.id === messageId)) {
          messages.value[convId] = [...current, assistantMessage]
        }
      })
    })

    let busyRejected = false
    let supersededRejected = false
    const doEditStream = () => chatApi.streamChat(
      {
        conversation_id: convId,
        assistant_id: assistantId,
        edit_message_id: realEditMessageId,
        messages: [{ role: 'user', content: newContent }],
        enable_reasoning: enableReasoning.value,
        reasoning_effort: reasoningEffort.value,
        thinking_budget: useAssistantStore().getAssistantById(assistantId ?? '')?.thinking_budget ?? thinkingBudget.value,
      },
      {
        ...callbacks,
        signal: abortController.signal,
        onConversationBusy: () => { busyRejected = true },
        onConversationSuperseded: () => { supersededRejected = true },
      },
    )

    try {
      // 4.8 session lock: the backend rejects a second concurrent agent on
      // the same conversation. Cancel the stale run and retry once (mirrors
      // sendMessage). Deliberate divergence from sendMessage's
      // resume-and-queue fallback: an edited turn cannot be replayed onto an
      // in-flight run, so on final busy the other run (possibly another
      // device) was stopped and we surface the error instead of silently
      // dropping the edit.
      await doEditStream()
      if (supersededRejected) {
        // Silent takeover: a newer request owns the conversation. The edited
        // user message is already persisted server-side; resync shows it
        // alongside the newer run's answer — no error bubble.
        endStreaming(convId)
        void refreshConversation(convId)
        return
      }
      if (busyRejected) {
        await chatApi.stopStream(convId)
        await new Promise<void>(resolve => { window.setTimeout(() => resolve(), 800) })
        busyRejected = false
        await doEditStream()
        if (supersededRejected) {
          endStreaming(convId)
          void refreshConversation(convId)
          return
        }
        if (busyRejected) {
          messages.value[convId] = preEditMessages
          currentError.value = '该会话已有正在进行的回答，请稍后重试。'
          endStreaming(convId)
          return
        }
      }
      if (s.streaming) {
        await tryReconnectOrSync(convId, expectedMessageCount, s)
        return
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        if (s.abortController !== abortController) {
          return
        }
        if (s.tabSwitchAbort) {
          s.tabSwitchAbort = false
          return
        }
        // Watchdog abort = the connection died silently. Prefer reconnecting
        // (resume replays the buffer and continues the live stream); fall back
        // to syncAfterAbort only when resume is impossible.
        await tryReconnectOrSync(convId, expectedMessageCount, s)
        return
      }
      currentError.value = e.message || '编辑失败'
      endStreaming(convId)
      await refreshConversation(convId)
    } finally {
      if (s.abortController === abortController) s.abortController = null
    }
  }

  async function stopStreaming(conversationId?: string) {
    const convId = conversationId || currentConversationId.value
    if (!convId) return
    // Explicitly cancel the detached agent task via the dedicated endpoint.
    // This must NOT be done by closing the SSE connection alone — a passive
    // disconnect (tab switch / browser throttle) would be indistinguishable
    // and would wrongly kill a background agent.
    const expectedMessageCount = (messages.value[convId] || []).length + 1
    await chatApi.stopStream(convId)
    const s = streamStates[convId]
    const partialContent = s?.content || ''
    const partialReasoning = s?.reasoning || ''
    if (s?.abortController) {
      try { s.abortController.abort() } catch {}
    }
    endStreaming(convId)
    currentError.value = null
    // The backend persists the partial reply in the cancel handler — poll the
    // conversation so the partial assistant bubble appears without a manual
    // reload (the deliberate stop does not go through the onError recovery
    // path that normally triggers syncAfterAbort).
    await syncAfterAbort(convId, expectedMessageCount, partialContent, partialReasoning)
  }

  function resetState() {
    for (const s of Object.values(streamStates)) {
      if (s.abortController) {
        try { s.abortController.abort() } catch {}
      }
    }
    Object.keys(streamStates).forEach(k => delete streamStates[k])
    Object.keys(messagesEpoch).forEach(k => delete messagesEpoch[k])
    Object.keys(refreshSeqs).forEach(k => delete refreshSeqs[k])
    conversations.value = []
    currentConversationId.value = null
    messages.value = {}
    contextInfoByConversation.value = {}
    try {
      localStorage.removeItem(CONTEXT_INFO_STORAGE_KEY)
    } catch {
      // ignore
    }
    localOnlyMessageIds.clear()
    currentError.value = null
    saveModeActive.value = false
    searchResults.value = []
    searchQuery.value = ''
    enableReasoning.value = false
    reasoningEffort.value = null
    thinkingBudget.value = null
    if (_deathmatchPollTimer) {
      clearInterval(_deathmatchPollTimer)
      _deathmatchPollTimer = null
    }
  }

  // Last-request-wins guard for store-level conversation search (same race
  // as the sidebar spotlight: a slow in-flight query must never overwrite a
  // newer one — 2026-08-07).
  let _searchConversationsSeq = 0

  async function searchConversations(query: string) {
    searchQuery.value = query
    if (!query.trim()) {
      _searchConversationsSeq++ // discard any in-flight response
      searchResults.value = []
      return
    }
    const seq = ++_searchConversationsSeq
    try {
      const data = await chatApi.searchConversations(query)
      if (seq !== _searchConversationsSeq) return // stale response
      searchResults.value = data
    } catch (e) {
      if (seq !== _searchConversationsSeq) return
      console.error('Failed to search conversations:', e)
      searchResults.value = []
    }
  }

  let _deathmatchPollTimer: ReturnType<typeof setInterval> | null = null

  async function pollDeathmatchStatus() {
    const convId = currentConversationId.value
    if (!convId) return
    const s = getStream(convId)
    // Only skip polling if actively receiving SSE events (last event within 30s)
    if (s.streaming && s._lastEventTime && (Date.now() - s._lastEventTime) < 30000) return

    const conv = conversations.value.find(c => c.id === convId)
    if (!conv?.deathmatch_mode || !conv.deathmatch_status || conv.deathmatch_status === 'inactive') {
      // Deathmatch ended out-of-band (cleared elsewhere / no final verdict
      // SSE): a preserved active/grilling verdict would permanently suppress
      // syncAfterAbort's stale-buffer cross-check for this conversation.
      if (s.deathmatchVerdict?.status === 'active' || s.deathmatchVerdict?.status === 'grilling') {
        s.deathmatchVerdict = null
        _streamVersion.value++
      }
      return
    }

    try {
      const conversation = await chatApi.getConversation(convId)
      conv.deathmatch_mode = conversation.deathmatch_mode
      conv.deathmatch_status = conversation.deathmatch_status
      conv.deathmatch_goal = conversation.deathmatch_goal
      conv.deathmatch_turns = conversation.deathmatch_turns
      conv.deathmatch_max_turns = conversation.deathmatch_max_turns
      conv.deathmatch_grilling_total = conversation.deathmatch_grilling_total
      conv.deathmatch_grilling_completed = conversation.deathmatch_grilling_completed
      conv.deathmatch_grilling_round = conversation.deathmatch_grilling_round
      conv.deathmatch_grilling_round_total = conversation.deathmatch_grilling_round_total

      s.deathmatchVerdict = {
        status: conversation.deathmatch_status || 'active',
        verdict: null,
        reason: null,
        turns: conversation.deathmatch_turns || 0,
        max_turns: conversation.deathmatch_max_turns || 30,
        grilling_completed: conversation.deathmatch_grilling_completed || 0,
        grilling_total: conversation.deathmatch_grilling_total || 0,
        grilling_round: conversation.deathmatch_grilling_round || 0,
        grilling_round_total: conversation.deathmatch_grilling_round_total || 3,
        message: '',
      }
      _streamVersion.value++

      if (conversation.deathmatch_status === 'done') {
        deathmatchMode.value = false
        if (_deathmatchPollTimer) {
          clearInterval(_deathmatchPollTimer)
          _deathmatchPollTimer = null
        }
        await refreshConversation(convId)
      } else if (conversation.deathmatch_status === 'partial_complete') {
        // Partial completion: stop polling but keep deathmatch mode on
        // so the user can resume by sending a message.
        deathmatchMode.value = true
        if (_deathmatchPollTimer) {
          clearInterval(_deathmatchPollTimer)
          _deathmatchPollTimer = null
        }
        await refreshConversation(convId)
      }
    } catch {
      // ignore poll errors
    }
  }

  function startDeathmatchPolling() {
    if (_deathmatchPollTimer) return
    _deathmatchPollTimer = setInterval(() => {
      pollDeathmatchStatus()
    }, 5000)
  }

  function stopDeathmatchPolling() {
    if (_deathmatchPollTimer) {
      clearInterval(_deathmatchPollTimer)
      _deathmatchPollTimer = null
    }
  }

  return {
    conversations,
    conversationMeta,
    currentConversationId,
    messages,
    streamStates,
    streamingContent,
    streamingReasoningContent,
    streamingToolStatuses,
    streamingSearchProgress,
    streamingSearchFailed,
    streamingConversationId,
    streamingSubAgentThinking,
    streamingFileAttachments,
    streamingTaskProgress,
    isStreaming,
    activeStreamingConversationIds,
    currentError,
    currentMessages,
    isStreamingCurrentConversation,
    currentStreamingContent,
    currentStreamingReasoningContent,
    currentStreamingToolStatuses,
    currentStreamingSearchProgress,
    currentStreamingSearchFailed,
    currentStreamingAgentSteps,
    currentStreamingSubAgentThinking,
    currentStreamingFileAttachments,
    currentStreamingContentSegments,
    currentStreamingTaskProgress,
    currentStreamingSubAgentChunks,
    currentStreamingDisplaySequence,
    searchResults,
    searchQuery,
    searchHighlightQuery,
    searchHighlightMessageId,
    searchHighlightNonce,
    enableReasoning,
    reasoningEffort,
    thinkingBudget,
    deathmatchMode,
    deathmatchAction,
    grillingQuestions,
    grillingAnswers,
    pendingPermissionRequest,
    saveModeActive,
    respondToPermission,
    currentStreamingToolCalls,
    currentStreamingToolResults,
    currentStreamingIteration,
    currentStreamingDeathmatchVerdict,
    currentContextInfo,
    contextInfoByConversation,
    loadConversations,
    createConversation,
    selectConversation,
    deleteConversation,
    removeConversationFromList,
    bulkDeleteConversations,
    updateConversationTitle,
    sendMessage,
    beginStreaming,
    regenerateLastAssistantMessage,
    regenerateWithForceResults,
    editAndResendMessage,
    stopStreaming,
    resumeActiveStream,
    tryReconnectOrSync,
    isResuming: (id: string) => _resumingSet.has(id),
    searchConversations,
    refreshConversation,
    resetState,
    fetchGrillingQuestions,
    answerGrillingQuestion,
    submitGrillingRound,
    startDeathmatchPolling,
    stopDeathmatchPolling,
    pollDeathmatchStatus,
  }
})
