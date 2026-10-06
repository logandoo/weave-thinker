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
  ContextInfo, ModelAlias,
} from '@/types'
import { chatApi, isExternalizedStub } from '@/api/chat'
import { modelsApi } from '@/api/models'
import { useAssistantStore } from '@/stores/assistant'
import { useNotesStore } from '@/stores/notes'
import { mergeReplayIntoSequence, pickStreamText, shouldApplyRefresh, dropDraftTextAfterLastTool, keepLongerDisplayed, caughtUpAfterAnchor, recoveryCaughtUp, trimWindowAfterFullFetch, isSyntheticMessageId, type TurnAnchor } from '@/stores/streamReducer'
import { abortPredecessor, isNoneReplayTransient } from '@/stores/streamConnection'
import { compareConversationsBySidebarOrder, reconcilePendingPromotions, rollbackPendingPromotion, serverCaughtUp, serverLastUserMessageMs } from '@/stores/conversationOrder'
import type { PendingPromotion } from '@/stores/conversationOrder'
import { estimateTextTokens } from '@/composables/useContextTokens'
import { loadContextInfoStorage, persistContextInfoStorage, pickLatestContextInfo } from '@/composables/useContextTokens'

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
  // P0-2 深链去重：selectConversation 刚拉完的会话在 TTL 内跳过 refreshConversation
  // 的重复全量 GET（实测双拉 33MB/16MB×2 的来源之一）。
  const lastFullGetAt: Record<string, number> = {}
  const LAST_FULL_GET_TTL_MS = 5000
  const streamStates: Record<string, StreamState> = reactive({})
  // 插话（2026-08-29，codex steer/deepseek-harness inbox 模式）：用户向运行中
  // run 提交的插话文本，先以乐观气泡上屏，等待后端 interjection_committed
  // commit-echo 确认；自然 done 时仍未确认 → 自动转普通发送（竞态兜底），
  // 用户主动 stop 时仍未确认 → 文本回填输入框（尊重停止意图）。
  // committed=true 表示后端已在迭代边界把它注入 run 上下文（commit-echo 已到），
  // 由 run 自己作答；false 表示仍躺在队列里，终态冲刷时兜底重发。两类都只活在
  // 这个队列里，不再作为乐观气泡写进 messages —— 显示位次因此恒为「回答最下方
  // 的队列区」而不是流式回答之上的消息气泡（需求 4①）。
  const pendingInterjections: Record<string, Array<{ localId: string; content: string; committed?: boolean; assistantId?: string | null }>> = reactive({})
  const interjectionRestoreText = ref('')
  // 本轮 run 的起始边界（turn 发起前最后一条服务器消息 id）：commit-dedup
  // 闸门只扫边界之后的消息（A4.9 R2 B1）。仅 sendMessage 发起新 turn 时设置；
  // resume/重连不触碰（内存内跨 tab-switch 存活；页面刷新后 pendings 同样
  // 丢失，无需边界）。
  const _runBoundaryMessageIds: Record<string, string | null> = {}
  // echo 已确认的 commit 行 id（A4.9 R3 N1）：dedup 闸门必须排除它们——
  // 同内容二次插话中，第一次 commit 且 echo 正常到达的行不是「echo 丢失」
  // 场景，若参与匹配会把第二次真正未送达的插话静默吞掉。随 refresh 存活；
  // 新 turn 发起时清空（边界同步前移，旧确认行不再被扫描）。
  const _echoConfirmedCommittedIds: Record<string, Set<string>> = {}

  function _isServerMessageId(id: string): boolean {
    return !id.startsWith('temp-') && !id.startsWith('bg-') && !id.startsWith('resume-') &&
      !id.startsWith('interject-pending-') && !id.startsWith('interject-committed-')
  }
  /** 项3 追平锚：严格持久化 id（local-abort- 等合成族一律排除——锚必须在
   *  服务端快照里找得到，否则 caughtUpAfterAnchor 只能回 null 降级）。 */
  function _newestPersistedId(list: readonly Message[]): string | null {
    for (let i = list.length - 1; i >= 0; i -= 1) {
      const id = list[i]?.id
      if (id && _isServerMessageId(id) && !isSyntheticMessageId(id)) return id
    }
    return null
  }

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
    // A4.9 R1 M5：混入增量后的值含估算成分，不再标"实测"。
    const next: ContextInfo = cur
      ? { ...cur, tokens: cur.tokens + est, measured: false }
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

  /** 回滚一条乐观加量（deferred #5，2026-08-29）：插话气泡被移除（回退/
   *  rate_limited/restore——消息未进入任何 run 上下文）时对称扣减。优先扣
   *  待刷新增量；已被 500ms 定时器刷进权威缓存则从缓存扣（钳 0）。 */
  function removeContextTokenDelta(convId: string, text: string) {
    if (!convId || !text) return
    const est = estimateTextTokens(text)
    const pendingTokens = _pendingContextTokens[convId] || 0
    if (pendingTokens > 0) {
      _pendingContextTokens[convId] = Math.max(0, pendingTokens - est)
      _pendingContextChars[convId] = Math.max(0, (_pendingContextChars[convId] || 0) - text.length)
      return
    }
    const cur = contextInfoByConversation.value[convId]
    if (cur && cur.tokens > 0) {
      // 遗留②：扣减混入估算成分，不再继承"实测"标签。
      const next: ContextInfo = { ...cur, tokens: Math.max(0, cur.tokens - est), measured: false }
      contextInfoByConversation.value = { ...contextInfoByConversation.value, [convId]: next }
      persistContextInfoStorage(contextInfoByConversation.value)
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

  /** P2 (2026-09-05)：从持久化消息播种 token 徽章——最后一条带 context_info
   *  的 assistant 消息即该会话最近一轮的权威快照，跨设备可见（手机端发出的
   *  轮次在桌面端打开也能看到）。直播中的会话跳过（实时 SSE context_info
   *  更新更准，避免旧快照回盖造成数值闪动）。 */
  function seedContextInfoFromMessages(conversationId: string, msgs: readonly Message[]) {
    const s = streamStates[conversationId]
    if (s?.streaming) return
    const parsed = pickLatestContextInfo(msgs)
    if (parsed) setContextInfo(conversationId, parsed)
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

  // ---- 模型别名（模型配置解耦 2026-08-30：GET /api/models，懒加载一次） ----
  const modelAliases = ref<ModelAlias[]>([])
  const modelsDefaultAlias = ref('')
  const modelsLoaded = ref(false)
  async function loadModels() {
    if (modelsLoaded.value) return
    modelsLoaded.value = true
    try {
      const res = await modelsApi.getModels()
      modelAliases.value = res.aliases
      modelsDefaultAlias.value = res.default_alias || ''
    } catch (e) {
      modelsLoaded.value = false // 失败允许下次重试
      console.error('Failed to load model aliases:', e)
    }
  }
  function capabilitiesForAlias(alias: string): ModelAlias['capabilities'] {
    const a = modelAliases.value.find(x => x.alias === alias && x.kind === 'llm')
    return a?.capabilities ?? {}
  }
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
    if (abortPredecessor(s)) {
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
          void tryReconnectOrSync(convId, null, s)
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

  /** @returns true = 服务端快照已成功取得（追平判定可信）；false = 请求失败
   *  （调用方不得据此做盲回滚，见 busy-final 的 refresh→rollback 序列）。 */
  // 在途去重：同一 (message, field 集合) 只发一次 payload GET
  const _payloadInflight = new Map<string, Promise<boolean>>()
  // P2-1 外置字段全集（读侧兼容历史 tool_calls 桩；写侧不外置 tool_calls）
  const PAYLOAD_FIELDS = ['tool_results', 'reasoning_content', 'tool_calls'] as const
  type PayloadField = typeof PAYLOAD_FIELDS[number]

  /** P2-1：展开巨块前取回单字段全文（payload stub → 原文），写回本地消息。
   *  @param fields 2026-10-05 起支持字段粒度：点击展开哪个块就只取回哪个字段
   *    （工具卡→tool_results · 思考块→reasoning_content），缺省=全字段
   *    （导出/正文愈合等需要整单全文的调用方不变）。
   *  @returns true = 所请字段全部持有全文（取回成功或本就无需取回）；
   *           false = 存在取回失败的桩字段——调用方须给出可重试的显式失败态
   *           （红线：截断必须可感知，静默停在预览=不可接受）。
   *  在途去重按 (msg, field) 粒度（minors 全清③：字段集序/重叠不再双取）。 */
  async function ensureMessageFull(
    conversationId: string,
    messageId: string,
    fields?: readonly PayloadField[],
  ): Promise<boolean> {
    const tasks = [...(fields ?? PAYLOAD_FIELDS)].map((field) => {
      const key = `${conversationId}:${messageId}:${field}`
      let task = _payloadInflight.get(key)
      if (!task) {
        task = _fetchFieldFull(conversationId, messageId, field).finally(() => {
          _payloadInflight.delete(key)
        })
        _payloadInflight.set(key, task)
      }
      return task
    })
    return (await Promise.all(tasks)).every(Boolean)
  }

  async function _fetchFieldFull(
    conversationId: string,
    messageId: string,
    field: PayloadField,
  ): Promise<boolean> {
    // 写回按 id 补丁（评审 R1：不用入口快照数组整体替换，避免与 loadOlder/refresh 竞态互毁）
    const list = messages.value[conversationId] || []
    const idx = list.findIndex((m) => m.id === messageId)
    if (idx < 0) return true
    const msg = list[idx]
    if (!isExternalizedStub((msg as Record<string, unknown>)[field])) return true
    try {
      const payload = await chatApi.getMessagePayload(messageId, field)
      const cur = messages.value[conversationId] || []
      const curIdx = cur.findIndex((m) => m.id === messageId)
      if (curIdx >= 0) {
        const next = [...cur]
        next[curIdx] = { ...next[curIdx], [field]: payload.content } as Message
        messages.value[conversationId] = next
      }
      return true
    } catch {
      /* 保留 stub（preview）——false 上抛给调用方渲染可重试失败态 */
      return false
    }
  }

  /** P2-1：导出/保存笔记等需全文的路径前置——展开式批量取回 stub 字段。 */
  async function ensureFullPayloads(conversationId: string): Promise<void> {
    const list = messages.value[conversationId] || []
    for (const m of list) {
      const anyStub = ['tool_results', 'reasoning_content', 'tool_calls'].some((f) =>
        isExternalizedStub((m as Record<string, unknown>)[f]))
      if (anyStub) await ensureMessageFull(conversationId, m.id)
    }
  }

  // P2-2 分页：打开路径默认最新 N 条，上滚加载更早（游标不重不漏）。
  const MESSAGE_PAGE_SIZE = 50
  const hasMoreMessages: Record<string, boolean> = reactive({})
  const oldestMessageId: Record<string, string | null> = reactive({})

  async function loadOlderMessages(conversationId: string): Promise<boolean> {
    const before = oldestMessageId[conversationId]
    if (!before || !hasMoreMessages[conversationId]) return false
    try {
      const older = await chatApi.getMessagesPaged(conversationId, before, MESSAGE_PAGE_SIZE)
      const cur = messages.value[conversationId] || []
      const existing = new Set(cur.map((m) => m.id))
      const fresh = (older.messages || []).filter((m: Message) => !existing.has(m.id))
      if (fresh.length) {
        messages.value[conversationId] = [...fresh, ...cur]
      }
      oldestMessageId[conversationId] = older.oldest_message_id || null
      hasMoreMessages[conversationId] = !!older.has_more_messages
      return fresh.length > 0
    } catch {
      return false
    }
  }

  async function refreshConversation(conversationId: string, opts?: { reuseFresh?: boolean; fullWindow?: boolean }): Promise<boolean> {
    // P0-2 新鲜窗（评审 I2 修复：仅深链去重调用点显式开启）——默认语义=必拉，
    // 否则发送/abort/流末的权威同步与回滚判定会拿到陈旧快照。
    if (
      opts?.reuseFresh
      && messages.value[conversationId]
      && Date.now() - (lastFullGetAt[conversationId] || 0) < LAST_FULL_GET_TTL_MS
    ) {
      return true
    }
    const epochAtStart = messagesEpoch[conversationId] || 0
    const seqAtStart = (refreshSeqs[conversationId] || 0) + 1
    refreshSeqs[conversationId] = seqAtStart
    try {
      // fullWindow（项3 锚降级）：锚被挤出分页窗时拉全量再判——一轮至多一次
      const conversation = await chatApi.getConversation(conversationId,
        opts?.fullWindow ? { include: 'slim' } : { include: 'slim', messageLimit: MESSAGE_PAGE_SIZE })
      lastFullGetAt[conversationId] = Date.now()
      hasMoreMessages[conversationId] = !!(conversation as unknown as { has_more_messages?: boolean }).has_more_messages
      oldestMessageId[conversationId] = (conversation as unknown as { oldest_message_id?: string | null }).oldest_message_id ?? null
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
        // P2：从持久化消息播种徽章——仅在快照被采用（非陈旧）时播种，避免
        // in-flight 旧快照回盖更近一轮的徽章（A4.9 R1 M1）；直播中的会话由
        // 实时 SSE 事件主导（seed 内部跳过）。
        const appliedRefresh = shouldApplyRefresh(localMessages, serverMessages)
        if (appliedRefresh) {
          // If the newest local message is a synthetic placeholder the server
          // list lacks (save-failure fallback done / resume with no real id /
          // optimistic bubble), PRESERVE it on top of the applied list ONLY
          // while the server list is shorter (pre-commit) — the user is
          // looking at that content; dropping it would recreate the mid-answer
          // disappearance. Once the server list catches up (same length),
          // apply cleanly so the synthetic is replaced by its real row and
          // never duplicates it.
          // conv 4e159a79 shrink-guard: a server row that is a strict suffix
          // of the displayed content lost its HEAD server-side — keep the
          // longer displayed content for rendering (see keepLongerDisplayed).
          const localById = new Map(localMessages.map(m => [m.id, m]))
          const guardedServerMessages = serverMessages.map(m => {
            const local = localById.get(m.id)
            if (!local) return m
            const content = keepLongerDisplayed(local.content, m.content)
            return content && content !== m.content ? { ...m, content } : m
          })
          const newestLocal = localMessages[localMessages.length - 1]
          const serverHasNewest = newestLocal && serverMessages.some(m => m.id === newestLocal.id)
          let preserveSynthetic = newestLocal && !serverHasNewest
            && serverMessages.length < localMessages.length
          // 项3（2026-10-05）：窗口化刷新下 serverMessages.length 被 PAGE_SIZE
          // 夹住，length 比较恒真 → 合成气泡会被永久保留成重复气泡。锚定等价
          // 判定：服务端窗口在边界（本轮发起前最后一条服务器消息）之后已有同
          // role+content 的持久化行 → 合成气泡已落库，不再保留（重复答案根治）。
          if (preserveSynthetic && isSyntheticMessageId(newestLocal.id)) {
            const boundaryId = _runBoundaryMessageIds[conversationId] ?? null
            const bIdx = boundaryId ? serverMessages.findIndex(m => m.id === boundaryId) : -1
            // A4.9 I1：边界缺席（本会话未发过新 turn / 已被挤出窗口）时不做等价
            // 判重——扫整个窗口会把「同内容重发（继续/好）」的旧落库行误判为本次
            // 合成气泡已落库，未 commit 的文本被提前丢弃。宁可暂留重复（下一
            // 次干净刷新自愈），绝不丢用户正在看的气泡。
            if (bIdx >= 0) {
              const tail = serverMessages.slice(bIdx + 1)
              const committed = tail.some(m => !isSyntheticMessageId(m.id)
                && m.role === newestLocal.role && m.content === newestLocal.content)
              if (committed) preserveSynthetic = false
            }
          }
          messages.value[conversationId] = preserveSynthetic
            ? [...guardedServerMessages, newestLocal]
            : guardedServerMessages
        }
        // P2：快照被采用（非陈旧）才播种（A4.9 R1 M1）。
        if (appliedRefresh) {
          seedContextInfoFromMessages(conversationId, serverMessages)
        }
      }
      // P4（2026-09-02）：不再用纯可见消息估算做加载期 fallback——该口径漏掉
      // 系统提示词与工具 schema（实测 ~17k vs 估算 2.4k，14 倍失真）。徽章保留
      // 到首个权威 context_info 快照（轮首即到）或发送增量。
      for (const message of conversation.messages || []) {
        localOnlyMessageIds.delete(message.id)
      }
      const conv = conversations.value.find(item => item.id === conversationId)
      if (conv) {
        conv.title = conversation.title
        conv.updated_at = conversation.updated_at
        // 置顶字段自愈（A4.9 Important-1）：仅当服务端快照追平本地乐观置顶时
        // 清除标记、应用权威 sort_order / last_user_message_at 并重排 ——
        // 常规 refresh（无 pending）不触碰排序字段，避免旧 GET 把刚拖拽的顺序
        // 打回（A4.9 R2 new-Minor）。追平判定纯服务端时间戳互比（Minor-2）。
        const pending = _pendingPromotions[conversationId]
        if (pending && serverCaughtUp(conversation, pending)) {
          delete _pendingPromotions[conversationId]
          if (typeof conversation.sort_order === 'number') conv.sort_order = conversation.sort_order
          if (conversation.last_user_message_at) conv.last_user_message_at = conversation.last_user_message_at
          conversations.value = [...conversations.value].sort(compareConversationsBySidebarOrder)
        }
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
          max_turns: conversation.deathmatch_max_turns ?? 0,
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
        // 死磕盘问：SSE 事件不重放——刷新/重开会话时必须从权威 GET 恢复
        // 待答问题，否则 grillingQuestions 恒空 → ChatArea `isGrilling` 恒
        // false，用户只看到「请回答上方的问题」而上方零气泡（conv 7579bdc7,
        // 2026-09-19）。仅当前会话 + 未过期快照（A4.9 r1 Important-1：陈旧
        // refresh 不得覆盖新轮次/新会话刚拉取的问题）；后台会话的 refresh
        // 不得污染当前视图。
        if (
          conversationId === currentConversationId.value
          && (messagesEpoch[conversationId] || 0) === epochAtStart
          && (refreshSeqs[conversationId] || 0) === seqAtStart
        ) {
          if (conversation.deathmatch_status === 'grilling') {
            await fetchGrillingQuestions(conversationId)
          } else if (grillingQuestions.value.length > 0) {
            grillingQuestions.value = []
            grillingAnswers.value = {}
          }
        }
      }
      return true
    } catch (e) {
      console.error('Failed to refresh conversation:', e)
      return false
    }
  }

  async function syncAfterAbort(
    conversationId: string,
    // null = 恢复模式（W3 项1 r2）：尾部形态判定 recoveryCaughtUp，无需锚。
    turnAnchor: TurnAnchor | null,
    partialContent: string,
    partialReasoningContent: string,
    // P1（2026-09-05）：恢复路径（tryReconnectOrSync）传 true——setup 完成、
    // buffer 出现时重挂直播，而不是干等整轮跑完才从 DB 刷出答案。用户主动
    // STOP 的调用方（stopStreaming）绝不重挂（默认 false）。
    allowReattach = false,
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

    // 项3（2026-10-05）：分页感知追平判定。旧计数启发式（内存长度+1）在
    // MESSAGE_PAGE_SIZE 窗口下永不满足（≥49 条历史会话）；锚定判定只看锚后
    // 新增的持久化消息。锚被挤出窗口（null）→ 全量拉取一次再判（降级路径，
    // 一轮至多一次；仍缺席 = 数据异常，按未追平继续走 Phase-2 兜底）。
    let fullRefetchUsed = false
    const caughtUp = async (): Promise<boolean> => {
      const listNow = messages.value[conversationId] || []
      if (!turnAnchor) return recoveryCaughtUp(listNow)
      const r = caughtUpAfterAnchor(listNow, turnAnchor)
      if (r !== null) return r
      if (fullRefetchUsed) return false
      // A4.9 M2：拉取失败不消耗一次性额度（refresh 内部捕获返回 false）
      const ok = await refreshConversation(conversationId, { fullWindow: true })
      if (!ok) return false
      fullRefetchUsed = true
      const verdict = caughtUpAfterAnchor(messages.value[conversationId] || [], turnAnchor) === true
      // minor⑱：全量拉取后把整史收回分页窗——防 MB 级整史在 store 驻留整会话。
      // W3 项3（2026-10-06）：仅 verdict=true 收窗——未追平时锚还在列表里，收窗
      // 会逐出锚令后续永远不可判（Phase-2 陈旧轮破照旧叠 local-abort 气泡）。
      // A4.9 B-m5：收窗前复查 epoch——await 期间新流接管时不得裁剪别人的列表。
      if (stillOurs() && verdict) {
        const before = messages.value[conversationId] || []
        const after = trimWindowAfterFullFetch(before, true, MESSAGE_PAGE_SIZE)
        if (after !== before) {
          messages.value[conversationId] = after
          hasMoreMessages[conversationId] = true
          oldestMessageId[conversationId] = after[0]?.id ?? null
        }
      }
      return verdict
    }
    // A4.9 B2：死磕 run 逐轮落库（mid-run 即有 assistant 行在锚后）——锚定判定
    // 与旧计数同样会误判追平，沿用 Phase-2 的 dmActive 豁免：死磕活跃期不做
    // 追平快判，交给流状态轮询（Phase-2 自带重挂）。
    const dmActiveNow = (): boolean => {
      const st = s.deathmatchVerdict?.status
      return st === 'active' || st === 'grilling'
    }
    // A4.9 I2：caughtUp 内可能多跑一次 fullWindow RTT——收尾前复查 epoch，
    // 防止 await 窗口期内新流接管后被旧循环 endStreaming 掐断。
    const stillOurs = (): boolean =>
      (messagesEpoch[conversationId] || 0) === inheritedEpoch

    // Phase 1: fast polls — covers the common case where the aborted stream
    // had already saved its final message before the abort landed.
    for (let attempt = 0; attempt < 8; attempt += 1) {
      await sleep(attempt === 0 ? 150 : 300)
      // Superseded by a newer flow on this conversation — stop touching state.
      if (!stillOurs()) return
      // W3 项1（2026-10-06）：refresh 抛错/失败时当轮不做判定（recoveryCaughtUp
      // 是尾部形态判定，对陈旧列表保守；此处门仍拒绝的是 refresh 异常路径）。
      const refreshed = await refreshConversation(conversationId)
      if (!refreshed) continue

      if (!dmActiveNow() && await caughtUp()) {
        if (!stillOurs()) return
        // W3 项1：caughtUp 可能跑 fullWindow 补拉（多一次 RTT）——采信前要求在
        // 其之后列表仍归本流（stillOurs 已查）且该补拉未失败（caughtUp 内保证）。
        currentError.value = null
        endStreaming(conversationId)
        // 需求 4②：这是 syncAfterAbort 自述的**最常见**终态（abort 落地前回答已
        // 存库）—— 挂着的插话必须继续作答，不得就此搁置。
        void flushUnconfirmedInterjections(conversationId, 'auto-send')
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
        // P1：buffer 已出现（setup 完成、agent 任务开跑）→ 立即重挂直播，
        // 让界面恢复流式输出，而不是干等整轮跑完才从 DB 刷出。resume 失败
        // （暂态/竞态）则继续轮询，下一轮再试。
        if (
          allowReattach
          && !status.setup_in_progress
          && status.has_buffer && status.status === 'incomplete' && status.is_running
          && !s.abortController
        ) {
          const reattached = await resumeActiveStream(conversationId)
          if (reattached) return
        }
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
        if (!dmActiveNow() && poll > 0 && poll % 10 === 0) {
          await refreshConversation(conversationId)
          if (await caughtUp()) {
            if (!stillOurs()) return
            currentError.value = null
            endStreaming(conversationId)
            // 需求 4②：run 已收尾，挂着的插话必须继续作答，不得随刷新静默丢弃。
            void flushUnconfirmedInterjections(conversationId, 'auto-send')
            return
          }
        }
        continue
      }

      // Buffer complete / agent stopped / buffer expired: the final message
      // should now be in the DB (or about to be saved).
      await refreshConversation(conversationId)
      if (!dmActiveNow() && await caughtUp()) {
        if (!stillOurs()) return
        currentError.value = null
        endStreaming(conversationId)
        // 需求 4②：同上 —— 终态冲刷队列。
        void flushUnconfirmedInterjections(conversationId, 'auto-send')
        return
      }
      stalePolls += 1
      if (stalePolls >= 3) break
    }

    // W3 项3（2026-10-06）：Phase-2 陈旧轮破后的终局复核——收窗/整页新行退化
    // 时序下锚可能已被逐出分页窗，叠局部气泡前重置一次性全量额度再判一次：
    // 真追平（答案已落库）→ 干净收线不叠气泡；否则才落 local-abort 兜底。
    if (partialContent.trim() || partialReasoningContent.trim()) {
      fullRefetchUsed = false
      if (!dmActiveNow() && await caughtUp()) {
        if (!stillOurs()) return
        currentError.value = null
        endStreaming(conversationId)
        void flushUnconfirmedInterjections(conversationId, 'auto-send')
        return
      }
      // A4.9 Minor（评审 B）：复核窗口内可能已被新流接管——兜底叠气泡/收尾
      // 前必须再查 epoch，绝不把 local-abort 挂到新流的列表上。
      if (!stillOurs()) return
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

    if (!stillOurs()) return
    currentError.value = null
    endStreaming(conversationId)
    // 需求 4②：轮询兜底也走到了终态 —— 挂着的插话继续作答，不得静默丢弃。
    void flushUnconfirmedInterjections(conversationId, 'auto-send')
  }

  async function resumeActiveStream(conversationId: string): Promise<boolean> {
    if (_resumingSet.has(conversationId)) return false
    const s = getStream(conversationId)
    if (!s.streaming) {
      return false
    }
    // 接管守卫（结构不变量）：resume 的语义是「重挂一条**已经断掉**的流」，
    // 不是「替换任意活流」。本地已有一条未中止的健康连接时必须让路 —— 否则
    // 下面的 abortPredecessor 会掐断插话兜底刚开启的下一 turn（实测：回答已
    // 生成并落库、前端却收不到，chatllm.log "Client disconnected during tool
    // loop" + "Final answer already persisted by producer" = 插话被静默丢弃）。
    // 中止过的前任（watchdog abort / tab-switch abort）不拦，照常重挂。
    if (s.abortController && !s.abortController.signal?.aborted) {
      return false
    }
    _resumingSet.add(conversationId)

    // conv 3a216a51 (2026-09-03): 接管前必须先 abort 前任连接——直接替换
    // s.abortController 会在 abort 传播慢于接管（思考流 ~90 事件/秒拥塞的
    // 移动 WebView + visibilitychange 的 150ms 固定等待）时留下两条活连接
    // 同时投递，同一 delta 相邻精确双写 → 思考面板整段「叠字」。
    abortPredecessor(s)
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
      if (s.abortController !== abortController) return // 连接令牌：被更新的连接接管后不得收尾
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
      // 需求 4②：resume 流的自然 done 同样必须冲刷队列。此前只有主发送流的
      // onDone 冲刷，resume（切后台回来 / watchdog 重连 / busy-attach）收尾时
      // 挂着的插话随 refresh 静默消失 —— 「回答完毕后插话被丢弃」的主要来源。
      // 自然结束 = 队列该被继续作答，语义与主 onDone 一致取 auto-send。
      void flushUnconfirmedInterjections(conversationId, 'auto-send')
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
          if (s.abortController !== abortController) return // 连接令牌：被更新的连接接管后不得报错收尾
          // replayStatus 'none' means the backend has no buffer and no live
          // agent for this conversation (typically: the run finished long ago
          // and the buffer expired). Not an error worth surfacing.
          // P1（2026-09-05）：'none' 的终态裁决移到 resumeStream 返回后
          // （需先查 stream/status 区分真无 run vs SETUP 期暂态）——此处
          // 不再无条件 endStreaming，否则 setup 期一切恢复尝试都被误杀，
          // 手机端切后台回来只剩最后一条 query 而生成在后台继续。
          if (replayStatus !== 'none') {
            currentError.value = error
            endStreaming(conversationId)
            // 插话兜底（deferred #2，I3 的 resume 孪生）：resume 流报错时未确认
            // 插话绝不随 refresh 静默丢失 —— 语义与主发送流 onError 一致。
            if (conversationId === currentConversationId.value) {
              void flushUnconfirmedInterjections(conversationId, 'restore')
            } else {
              void flushUnconfirmedInterjections(conversationId, 'auto-send')
            }
            void refreshConversation(conversationId)
          }
        },
      })

      if (replayStatus === 'none') {
        // 'none' 回放双义二判：run 仍在 SETUP 期（或 buffer 恰好在 resume
        // 后出现）→ 暂态，return false 交还调用方走有界恢复（重试 resume →
        // syncAfterAbort 轮询，buffer 出现后重挂直播）；真无 run 才终态收尾。
        let transient = false
        try {
          transient = isNoneReplayTransient(await chatApi.getStreamStatus(conversationId))
        } catch {
          transient = false
        }
        if (transient && s.streaming && s.abortController === abortController) {
          return false
        }
        // 连接令牌（A4.9 R1 I1）：getStreamStatus RTT 期间本会话可能已被更新
        // 的流程接管（重发/编辑重发/重选）——终态收尾（endStreaming/插话冲刷）
        // 只许在仍是本连接时执行，否则交还接管方。
        if (s.abortController !== abortController) {
          return false
        }
        endStreaming(conversationId)
        if (conversationId === currentConversationId.value) {
          void flushUnconfirmedInterjections(conversationId, 'restore')
        } else {
          void flushUnconfirmedInterjections(conversationId, 'auto-send')
        }
        void refreshConversation(conversationId)
        return true
      }

      if (replayStatus === 'complete') {
        if (replayDbMessageId) {
          await refreshConversation(conversationId)
          // 需求 4②：resume 收到 complete（切后台回来时 run 已跑完）也是终态。
          void flushUnconfirmedInterjections(conversationId, 'auto-send')
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
    // null = 恢复模式（W3 项1 r2）；流程内（send/regen/edit）传本 flow 的锚。
    turnAnchor: TurnAnchor | null,
    s: StreamState,
  ) {
    // 不是"恢复"的对象就别碰：已有一条**健康**活连接在跑时（例如插话兜底
    // 刚开启的下一 turn）直接交还。否则走到底部的 syncAfterAbort 会用旧 turn
    // 的追平锚去收尾新 turn —— Phase-1 锚定检查被旧 turn 的
    // 完成满足，于是把新 turn endStreaming 掉（回答已生成、前端却收不到 = 插话
    // 又被静默丢弃）。中止过的前任不算健康，照常恢复。
    if (s.abortController && !s.abortController.signal?.aborted) return
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
      await syncAfterAbort(conversationId, turnAnchor, s.content, s.reasoning, true)
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

  // 继续旧会话后置顶（2026-09-15；2026-09-26 起重新生成同口径）：侧栏顺序 =
  // 后端 list_conversations 的 ORDER BY —— (sort_order asc, 最近活动 desc)，
  // 纯函数见 stores/conversationOrder.ts（单测
  // frontend/tests/workflows/test_conversation_order.cjs）。
  // 发送新消息 / 编辑重发 / 重新生成时把该会话提升到 sort_order=0 并刷新
  // last_user_message_at（=最近活动时间），使其立刻跳到所在分组/时间分类的最
  // 上方；后端在 chat.py 相应分支做同样的持久化提升，刷新后顺序保持一致。
  //
  // _pendingPromotions：本地乐观置顶的 convId → 前值快照。并发快照
  // （loadConversations / refreshConversation 的 GET）可能在本次发送的服务端提交
  // 之前生成、之后才到达 —— 旧快照不得把刚置顶的会话打回原位（A4.9 Important-1）；
  // 追平判定只用服务端时间戳互比（客户端时钟偏差不影响，A4.9 Minor-2）；
  // 发送被最终拒绝时按快照回滚（A4.9 Minor-3）。
  const _pendingPromotions: Record<string, PendingPromotion> = {}

  function promoteConversationToTop(conversationId: string) {
    const conv = conversations.value.find(c => c.id === conversationId)
    if (!conv) return
    // 前值快照只记一次（busy 重试会再次 promote，不得用乐观值覆盖前值）。
    if (!_pendingPromotions[conversationId]) {
      _pendingPromotions[conversationId] = {
        promotedAt: Date.now(),
        previousActivityMs: serverLastUserMessageMs(conv),
        previousSortOrder: conv.sort_order ?? 0,
        previousLastUserMessageAt: conv.last_user_message_at ?? null,
      }
    }
    const pending = _pendingPromotions[conversationId]
    conv.last_user_message_at = new Date(pending.promotedAt).toISOString()
    conv.sort_order = 0
    conversations.value = [...conversations.value].sort(compareConversationsBySidebarOrder)
  }

  /** 发送未送达（busy 最终拒绝）→ 回滚乐观置顶，恢复提升前的列表位置。 */
  function rollbackPromotion(conversationId: string) {
    const conv = conversations.value.find(c => c.id === conversationId)
    if (!conv) {
      delete _pendingPromotions[conversationId]
      return
    }
    if (rollbackPendingPromotion(conv, _pendingPromotions)) {
      conversations.value = [...conversations.value].sort(compareConversationsBySidebarOrder)
    }
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
      // 旧快照（生成于本次发送提交之前）不得打回本地乐观置顶 —— 见
      // _pendingPromotions 注释；服务端已追平的条目在此清除。
      reconcilePendingPromotions(conversations.value, _pendingPromotions)
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
    // 盘问问题/答案是会话级 UI 状态：切换会话立即清空，防止上一会话的
    // 问题泄漏到新会话（A4.9 r1 Important-3）；grilling 状态由
    // refreshConversation 重新从权威 GET 拉取。
    grillingQuestions.value = []
    grillingAnswers.value = {}
    try {
      const conversation = await chatApi.getConversation(id, { include: 'slim', messageLimit: MESSAGE_PAGE_SIZE })
      lastFullGetAt[id] = Date.now()
      hasMoreMessages[id] = !!(conversation as unknown as { has_more_messages?: boolean }).has_more_messages
      oldestMessageId[id] = (conversation as unknown as { oldest_message_id?: string | null }).oldest_message_id ?? null
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
          // P2：从持久化消息播种徽章（快照被采用才播种——A4.9 R1 M1；
          // 直播会话由 seed 内部跳过防回盖）。
          seedContextInfoFromMessages(id, serverMessages)
        }
      }
      // P4：加载期 fallback 移除（纯可见消息口径漏系统提示词与工具 schema，
      // 低估 14 倍）。徽章保留到首个权威 context_info 快照或发送增量。
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
      // 首次从侧栏打开死磕会话（无 deep link、本会话无历史流）：种入
      // verdict——状态栏与 isGrilling 均读 getCurrentStream().deathmatchVerdict；
      // 缺它则问题虽拉到也不渲染（A4.9 r2 residual）。仅在无既有 verdict
      // 且非流式中种入，绝不覆盖实时 SSE 状态；形状与 refreshConversation
      // 的权威快照一致。
      const _selStream = getStream(id)
      if (
        !_selStream.streaming
        && !_selStream.deathmatchVerdict
        && conversation.deathmatch_mode
        && conversation.deathmatch_status
        && conversation.deathmatch_status !== 'inactive'
      ) {
        _selStream.deathmatchVerdict = {
          status: conversation.deathmatch_status,
          verdict: null,
          reason: null,
          turns: conversation.deathmatch_turns || 0,
          max_turns: conversation.deathmatch_max_turns ?? 0,
          grilling_completed: conversation.deathmatch_grilling_completed || 0,
          grilling_total: conversation.deathmatch_grilling_total || 0,
          grilling_round: conversation.deathmatch_grilling_round || 0,
          grilling_round_total: conversation.deathmatch_grilling_round_total || 3,
          message: conversation.deathmatch_status === 'done' ? '目标已完成' : '',
          plan_version: conversation.deathmatch_plan_version || 0,
          plan_steps: (conversation.deathmatch_plan?.steps || []).map((step: any) => ({
            id: step.id || '',
            description: step.description || '',
            status: step.status || 'pending',
          })),
        }
        _streamVersion.value++
      }
      // 应用内切换（Sidebar/Zen 只调 selectConversation，不走 refreshConversation）：
      // 顶部已清空本会话的问题，这里必须从权威 GET 重拉，否则切走再切回
      // grilling 会话时问题消失、isGrilling 恒 false（A4.9 r2 新回归）。
      if (conversation.deathmatch_status === 'grilling') {
        await fetchGrillingQuestions(id)
      }
    } catch (e) {
      console.error('Failed to load messages:', e)
      if (!messages.value[id]) {
        messages.value[id] = []
      }
    }

    // 本地流仍存活（切走后后台持续累积）时跳过状态检查与重连 —— 切回生成中
    // 会话不应 abort 健康连接再 resume（重连抖动 + 无意义的全量 replay）。
    // 本地无活跃流时才走 status → resume 路径（跨标签/刷新后的重挂）。
    const _localStream = streamStates[id]
    if (_localStream?.streaming && _localStream?.abortController) {
      return
    }

    try {
      const status = await chatApi.getStreamStatus(id)
      // 上面的存活检查发生在 await **之前**；await 期间本地可能已经有新流接管
      // （最典型：插话兜底自动发送刚开启下一 turn）。此时 status 看到的正是那条
      // 本地流的 buffer —— 继续走 resume 的话 abortPredecessor 会把它掐断，
      // 于是回答生成并落库、前端却收不到（chatllm.log 实证："Client disconnected
      // during tool loop" + "Final answer already persisted by producer"）= 用户的
      // 插话被静默丢弃。接管前必须再确认一次。
      const _liveAfterStatus = streamStates[id]
      if (_liveAfterStatus?.streaming && _liveAfterStatus.abortController) {
        return
      }
      if (status.has_buffer && status.status === 'incomplete' && status.is_running) {
        beginStreaming(id)
        const resumed = await resumeActiveStream(id)
        if (!resumed) {
          // A4.9 R1 M5：resume 暂态失败（如恰逢 buffer 尾声收 'none'）不能
          // 干等 30s 看门狗——直接走有界恢复（重试 → 轮询 → 重挂）。
          const s = streamStates[id]
          if (s?.streaming && !s.abortController) {
            void tryReconnectOrSync(id, null, s)
          }
        }
      } else if (status.setup_in_progress) {
        // P1（2026-09-05）：页面重载/进程重生恰逢后端 SETUP 期（provisional
        // 注册、buffer 未创建）——此前此分支不存在，界面只剩最后一条 query
        // 干等到用户手动重进。标记 streaming（显示"正在思考"）并走有界恢复：
        // resume 在 setup 期判暂态返回 false → syncAfterAbort 轮询 → buffer
        // 出现后重挂直播。
        beginStreaming(id)
        const s = streamStates[id]
        void tryReconnectOrSync(id, null, s)
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
        } else {
          // run 已 complete 且不需要重挂 —— 同样是终态，挂着的插话不得搁置
          // （评审复审 New-2 / A3 残留）。
          void flushUnconfirmedInterjections(id, 'auto-send')
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
    // conv 827a6f78: a non-string delta would render "true"/"null" via string
    // concat into the timeline. Drop it at the store boundary as well (the
    // dispatch layer already coerces; this guards legacy/direct callers).
    if (typeof part.delta !== 'string') return
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
    // conv 3a216a51 (2026-09-03): 连接令牌——本组 handler 只属于当前连接。
    // 被接管的旧连接（abort 传播延迟/积压 chunk 回放）投递的迟到事件必须在
    // 入口整体丢弃：思考流 ~90 事件/秒拥塞移动 WebView 主线程时，旧连接的
    // onmessage 积压会在 resume 新连接开始投递后才被处理，同一 delta 相邻
    // 精确双写（用户看到整段叠字，入库正常）。
    const connection = s.abortController
    const rawHandlers = {
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
        // 连接令牌（与 resume 的 onDoneCallback 同一不变量）：迟到的 done 绝不能
        // 拆掉后继 turn 的流。插话兜底自动发送会立刻开启下一 turn，此时上一 run
        // 的 done 才到达是常态 —— 无守卫的 endStreaming 会把新 turn 的
        // abortController 置 null 并连带中止其 SSE（后台日志表现为
        // "Client disconnected during tool loop"，回答已生成却没人收到 = 静默丢弃）。
        if (s.abortController !== connection) return
        onFinalize(messageId, title, toolResults, searchFailed, taskSubmitted)
        endStreaming(conversationId)
        if (title) {
          const conv = conversations.value.find(c => c.id === newConvId)
          if (conv) conv.title = title
        }
        // 自然结束时仍未确认的插话（run 在最后一个迭代中完成，队列不再被
        // 消费）：自动转为普通新消息发送 —— 用户的文字永不丢失。
        void flushUnconfirmedInterjections(conversationId, 'auto-send')
      },
      onError: (error: string) => {
        touch()
        // 同上：连接令牌守卫，迟到的 error 不得拆掉后继 turn 的流。
        if (s.abortController !== connection) return
        currentError.value = error
        endStreaming(conversationId)
        // 插话兜底（A4.9 I3）：run 错误终止时未确认插话绝不随 refresh 静默
        // 丢失 —— 当前会话回填输入框（错误后隐式重发可能复发同一错误），
        // 后台会话定向自动发送（文字永不丢失）。
        if (conversationId === currentConversationId.value) {
          void flushUnconfirmedInterjections(conversationId, 'restore')
        } else {
          void flushUnconfirmedInterjections(conversationId, 'auto-send')
        }
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
      onInterjectionCommitted: (payload) => {
        touch()
        // 插话 commit-echo：按内容配对确认（deferred #3 + 第四波 ①）——
        // resume 后迟到的 T2 echo 不得错配到已丢 echo 的 T1 条目；内容不
        // 匹配任何待确认条目（跨标签/未知回声）时不再退回 FIFO-0（那会把
        // 回声内容错配进最老气泡），仅按 id 去重追加服务器行。确认后将
        // 乐观气泡原地替换为服务器消息（id 来自落库行）。
        const pend = pendingInterjections[conversationId] || []
        const matchIdx = pend.findIndex(p => p.content === payload.content)
        // 确认后**留在队列里**并标记 committed —— 显示位次恒为「回答最下方的
        // 队列区」，绝不在流式回答之上插入一条用户气泡（需求 4①）。出队统一
        // 发生在回答终态的冲刷里。
        if (matchIdx >= 0) {
          pend[matchIdx] = { ...pend[matchIdx], committed: true }
        }
        // A4.9 R3 N1：echo 已确认的 commit 行登记入册，dedup 闸门排除之
        // （echo 丢失的行不在册，保持可匹配）。
        if (payload.id) {
          if (!_echoConfirmedCommittedIds[conversationId]) {
            _echoConfirmedCommittedIds[conversationId] = new Set()
          }
          _echoConfirmedCommittedIds[conversationId].add(payload.id)
        }
      },
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
      onAuditReset: () => {
        touch()
        // 静默质检同步（conv 827a6f78 turn-B，2026-09-03）：后端打回了已流式
        // 展示的草稿并重置其累加器。丢弃最后工具卡之后的草稿文本（思考/工具
        // 卡保留，A4 对齐）并重置 content 累加器——否则 done 气泡会包含全部
        // 被拒草稿（刷新才恢复 DB 干净行）。
        s.content = ''
        const { kept, changed } = dropDraftTextAfterLastTool(s.displaySequence)
        if (changed) {
          s.displaySequence = kept as DisplaySequenceItem[]
          _resetPartIndex(conversationId, s.displaySequence)
        }
      },
    }
    // 连接令牌守卫：接管发生后（s.abortController 已换成新连接的 controller），
    // 旧连接的一切迟到事件——含积压的 content/reasoning/part_delta 与 ping——
    // 不得再触碰 store 状态（onDone/onError 同样拦截，防止旧连接收尾重复落库）。
    type RawHandlers = typeof rawHandlers
    const guarded = {} as RawHandlers
    for (const key of Object.keys(rawHandlers) as (keyof RawHandlers)[]) {
      const original = rawHandlers[key] as (...args: unknown[]) => unknown
      guarded[key] = ((...args: unknown[]) => {
        if (s.abortController !== connection) return
        return original(...args)
      }) as RawHandlers[typeof key]
    }
    return guarded
  }

  // 每会话请求序号：并发的 GET 只允许最新一次写入（A4.9 r1 Important-1：
  // 旧响应不得覆盖新轮次/新会话刚拉取的问题）。
  const _grillingFetchSeq: Record<string, number> = {}

  async function fetchGrillingQuestions(convId: string) {
    const seq = (_grillingFetchSeq[convId] || 0) + 1
    _grillingFetchSeq[convId] = seq
    try {
      const resp = await fetch(`/api/agent-tasks/grilling/${convId}`, {
        headers: { 'Authorization': `Bearer ${localStorage.getItem('chatllm_token')}` }
      })
      if (resp.ok) {
        const data = await resp.json()
        // 请求期间用户已切走会话或有更新的请求发出：丢弃本次迟到响应。
        if (convId !== currentConversationId.value) return
        if (_grillingFetchSeq[convId] !== seq) return
        const incoming: any[] = Array.isArray(data.questions) ? data.questions : []
        const incomingRound = incoming.reduce((m: number, q: any) => Math.max(m, q.round || 1), 0)
        const currentMaxRound = grillingQuestions.value.length > 0
          ? Math.max(...grillingQuestions.value.map(q => q.round || 1))
          : 0
        if (incoming.length > 0 && incomingRound >= currentMaxRound) {
          // 轮次单调不回退（与 onDeathmatchVerdict 同规则）：等轮次可刷新
          // 已答状态，旧轮次响应不得覆盖新轮次。
          grillingQuestions.value = incoming.map((q: any) => ({
            task_id: q.task_id,
            question_id: q.question_id,
            question: q.question,
            recommendation: q.recommendation,
            options: q.options || [],
            round: q.round || 1,
            status: q.status,
            answer: q.answer,
          }))
          for (const q of incoming) {
            if (q.answer) {
              grillingAnswers.value[q.task_id] = q.answer
            }
          }
        } else if (incoming.length === 0 && data.deathmatch_status === 'grilling') {
          // 服务端在当前轮确认无待答问题：清空本地残留，避免旧会话问题
          // 在新会话/新轮次下渲染（A4.9 r1 Important-3）。
          grillingQuestions.value = []
          grillingAnswers.value = {}
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
            max_turns: conv?.deathmatch_max_turns ?? 0,
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
              max_turns: conv?.deathmatch_max_turns ?? 0,
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

  async function sendMessage(content: string, assistantId?: string | null, _busyAttempts = 0, _targetConversationId?: string) {
    if (!_targetConversationId && !currentConversationId.value) {
      await createConversation(undefined, assistantId)
    }

    const conversationId = _targetConversationId || currentConversationId.value!
    // A4.9 R2 B1：记录本轮 run 的起始边界（乐观气泡入列前的最后一条服务器
    // 消息），commit-dedup 闸门只扫边界之后。仅新 turn 发起处设置。
    {
      const arr = messages.value[conversationId] || []
      const lastServer = [...arr].reverse().find(m => _isServerMessageId(m.id))
      _runBoundaryMessageIds[conversationId] = lastServer ? lastServer.id : null
      _echoConfirmedCommittedIds[conversationId] = new Set()
    }
    const userMessage: Message = {
      id: `temp-${Date.now()}`,
      conversation_id: conversationId,
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    }

    if (!messages.value[conversationId]) messages.value[conversationId] = []
    messages.value[conversationId].push(userMessage)
    promoteConversationToTop(conversationId)
    addContextTokenDelta(conversationId, content)
    beginStreaming(conversationId)

    const abortController = new AbortController()
    const s = getStream(conversationId)
    s.abortController = abortController
    // 项3：锚定追平（分页感知）。锚=本轮最新持久化消息（严格谓词，合成族
    // ——含 local-abort 残留气泡——一律跳过）；need=2（用户行+助手行）。
    const turnAnchor: TurnAnchor = { id: _newestPersistedId(messages.value[conversationId] || []), need: 2 }

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
          thinking_budget: thinkingBudget.value,
          // A4.9 M4：定向发送（插话兜底，目标必非 deathmatch——端点已拒）不
          // 能泄漏当前会话的 deathmatchMode 全局 ref。
          deathmatch_mode: _targetConversationId ? false : deathmatchMode.value,
          deathmatch_action: _targetConversationId ? null : deathmatchAction.value,
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
          // 请求已经结算（busy 拒绝），controller 却还挂着且未中止 —— 若不清掉，
          // resume 的活连接接管守卫会把它当成"健康连接"而拒绝重挂（用户文本就
          // 真的丢了）。这里显式宣告本 flow 的连接已死。
          if (s.abortController === abortController) s.abortController = null
          const attached = await resumeActiveStream(conversationId)
          if (attached && _busyAttempts < 2) {
            // The attached run has now finished (resume resolves at done) —
            // the slot is free, deliver the queued message. Capped at 2
            // resends so a persistent busy/resume disagreement (e.g.
            // cross-worker claim churn) can't loop stopStream forever.
            // A4.9 M4：定向发送的重试必须携带原目标会话，不得落回当前会话。
            await sendMessage(content, assistantId, _busyAttempts + 1, _targetConversationId)
          } else {
            if (!attached) {
              currentError.value = '该会话已有正在进行的回答，请稍后重试。'
              endStreaming(conversationId)
              // 终态：挂着的插话不得静默搁置（回填给用户，由其决定何时重发）。
              void flushUnconfirmedInterjections(conversationId, 'restore')
            } else {
              // Attached but resend cap exhausted — never drop the user's
              // intent silently; surface it so they can resend manually.
              currentError.value = '消息未能送达：当前回答占用时间过长，请稍后重新发送。'
            }
            // 消息未送达 → 回滚乐观置顶（A4.9 Minor-3；重试成功路径不回滚）。
            // 回滚前先取服务端权威：若同会话另一次发送已送达，refresh 会追平并清除
            // pending 标记 → 回滚自动 no-op，不会覆盖那次成功发送的置顶（A4.9 R3 Minor）。
            // refresh 失败（false）→ 无法判定，宁可保留乐观置顶也不盲回滚（A4.9 R4 Minor）。
            if (await refreshConversation(conversationId)) {
              rollbackPromotion(conversationId)
            }
          }
          return
        }
      }

      if (s.streaming) {
        await tryReconnectOrSync(conversationId, turnAnchor, s)
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
        // 本 flow 的请求已结算：显式清掉 controller，否则恢复链会把它当成
        // "健康的活连接"而整体放弃（P3）。
        if (s.abortController === abortController) s.abortController = null
        await tryReconnectOrSync(conversationId, turnAnchor, s)
        return
      }
      // 非 AbortError（如 TypeError / 网络断连）：
      // 不要立即 endStreaming——visibilitychange handler 会
      // 在 tab 恢复时通过 resume 接管。此处只 fallback 到 sync。
      if (s.streaming) {
        if (s.abortController === abortController) s.abortController = null
        await tryReconnectOrSync(conversationId, turnAnchor, s)
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
    // 重新生成 = 最新操作（2026-09-26）：乐观置顶，服务端在 chat.py
    // regenerate 分支持久化 sort_order=0，活动时间随新助手消息落库推进。
    promoteConversationToTop(convId)
    beginStreaming(convId)

    const abortController = new AbortController()
    s.abortController = abortController
    // 项3：锚=截断后最新持久化消息；need=1（assistant-tail 把关：regenerate/编辑重发
    // 中途旧助手行已删、编辑行原位紧随锚后——锚后助手行落库即追平）。
    // A4.9 I1：同步 run 边界——preserveSynthetic 等价判重以锚后窗口为界。
    const _anchorId = _newestPersistedId(messages.value[convId] || [])
    _runBoundaryMessageIds[convId] = _anchorId
    const turnAnchor: TurnAnchor = { id: _anchorId, need: 1 }

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
      let busyRejected = false
      await chatApi.streamChat(
        {
          conversation_id: convId,
          assistant_id: assistantStore.currentAssistantId,
          regenerate_from_message_id: targetMessage.id,
          messages: buildRequestMessages(convId),
          enable_reasoning: enableReasoning.value,
          reasoning_effort: reasoningEffort.value,
          thinking_budget: thinkingBudget.value,
        },
        {
          ...callbacks,
          signal: abortController.signal,
          onConversationBusy: () => { busyRejected = true },
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
      if (busyRejected) {
        // 另一 run 占用中：请求未到达服务端（零写入）——恢复被裁掉的消息并
        // 回滚乐观置顶（A4.9 R1 I-1）。refresh 追平（同会话另一流已落库新
        // 活动）则回滚自动 no-op；refresh 失败宁可保留置顶也不盲回滚（与发送
        // 路径的 A4.9 R3/R4 口径一致）。
        currentError.value = '该会话已有正在进行的回答，请稍后重试。'
        endStreaming(convId)
        if (await refreshConversation(convId)) {
          rollbackPromotion(convId)
        }
        return
      }
      if (s.streaming) {
        await tryReconnectOrSync(convId, turnAnchor, s)
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
        await tryReconnectOrSync(convId, turnAnchor, s)
        return
      }
      currentError.value = e.message || '重新生成失败'
      endStreaming(convId)
      // 请求可能未到达服务端（网络失败=零写入）；也可能死于 trim/sort_order
      // 已提交但新助手消息未落库的窗口（此时 refresh 未追平 → 回滚本地置顶，
      // 与服务端 sort_order=0 短暂分歧，下一次 loadConversations 自愈——与发送
      // 路径同形态，A4.9 R2 Minor-2 记录）。服务端已落库新助手消息则 refresh
      // 追平清标记，回滚自动 no-op（A4.9 R1 I-1）。
      if (await refreshConversation(convId)) {
        rollbackPromotion(convId)
      }
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
    // 重新生成（强制检索结果）= 最新操作：与 regenerateLastAssistantMessage 同样置顶。
    promoteConversationToTop(convId)
    beginStreaming(convId)

    const abortController = new AbortController()
    s.abortController = abortController
    // 项3：锚=截断后最新持久化消息；need=1（assistant-tail 把关：regenerate/编辑重发
    // 中途旧助手行已删、编辑行原位紧随锚后——锚后助手行落库即追平）。
    // A4.9 I1：同步 run 边界——preserveSynthetic 等价判重以锚后窗口为界。
    const _anchorId = _newestPersistedId(messages.value[convId] || [])
    _runBoundaryMessageIds[convId] = _anchorId
    const turnAnchor: TurnAnchor = { id: _anchorId, need: 1 }

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
      let busyRejected = false
      await chatApi.streamChat(
        {
          conversation_id: convId,
          assistant_id: assistantStore.currentAssistantId,
          regenerate_from_message_id: messageId,
          force_search_results: forceSearchResults,
          messages: buildRequestMessages(convId),
          enable_reasoning: enableReasoning.value,
          reasoning_effort: reasoningEffort.value,
          thinking_budget: thinkingBudget.value,
        },
        {
          ...callbacks,
          signal: abortController.signal,
          onConversationBusy: () => { busyRejected = true },
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
      if (busyRejected) {
        // 同 regenerateLastAssistantMessage：零写入拒绝 → 回滚乐观置顶。
        currentError.value = '该会话已有正在进行的回答，请稍后重试。'
        endStreaming(convId)
        if (await refreshConversation(convId)) {
          rollbackPromotion(convId)
        }
        return
      }
      if (s.streaming) {
        await tryReconnectOrSync(convId, turnAnchor, s)
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
        await tryReconnectOrSync(convId, turnAnchor, s)
        return
      }
      currentError.value = e.message || '重新生成失败'
      endStreaming(convId)
      // 同 regenerateLastAssistantMessage：refresh 追平则 no-op，否则回滚置顶。
      if (await refreshConversation(convId)) {
        rollbackPromotion(convId)
      }
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
    promoteConversationToTop(convId)

    beginStreaming(convId)
    const abortController = new AbortController()
    s.abortController = abortController
    // 项3：锚=截断后最新持久化消息；need=1（assistant-tail 把关：regenerate/编辑重发
    // 中途旧助手行已删、编辑行原位紧随锚后——锚后助手行落库即追平）。
    // A4.9 I1：同步 run 边界——preserveSynthetic 等价判重以锚后窗口为界。
    const _anchorId = _newestPersistedId(messages.value[convId] || [])
    _runBoundaryMessageIds[convId] = _anchorId
    const turnAnchor: TurnAnchor = { id: _anchorId, need: 1 }

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
        thinking_budget: thinkingBudget.value,
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
          // 消息未送达 → 回滚乐观置顶（A4.9 Minor-3）。先 refresh 取服务端权威：
          // 同会话另一次发送已送达时追平清标记 → 回滚 no-op（A4.9 R3 Minor）；
          // refresh 失败则保留乐观置顶不盲回滚（A4.9 R4 Minor）。
          if (await refreshConversation(convId)) {
            rollbackPromotion(convId)
          }
          return
        }
      }
      if (s.streaming) {
        await tryReconnectOrSync(convId, turnAnchor, s)
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
        await tryReconnectOrSync(convId, turnAnchor, s)
        return
      }
      currentError.value = e.message || '编辑失败'
      endStreaming(convId)
      await refreshConversation(convId)
    } finally {
      if (s.abortController === abortController) s.abortController = null
    }
  }

  // ── 插话（interjection）───────────────────────────────────────────────
  /** 移除一条待确认插话队列条目；返回是否真的移除了（已被 done/stop 兜底
   *  排走的条目返回 false —— 调用方据此避免重复发送，A4.9 I2）。
   *  插话不再作为乐观气泡写进 messages（显示位次固定在回答最下方的队列区），
   *  因此这里只做队列出队。未确认条目顺带回滚乐观 token 增量；已 commit 的
   *  文本已进 run 上下文，增量是真实的，不回滚。 */
  function _removePendingInterjection(convId: string, localId: string): boolean {
    const pend = pendingInterjections[convId] || []
    const entry = pend.find(p => p.localId === localId)
    const had = !!entry
    if (had) {
      pendingInterjections[convId] = pend.filter(p => p.localId !== localId)
      // 已 commit 的文本已经进入 run 上下文，token 增量是真实的，不回滚。
      if (!entry.committed) removeContextTokenDelta(convId, entry.content)
    }
    return had
  }

  /** 自然 done / 用户 stop 时仍滞留在队列里的插话：移除乐观气泡并返回文本列表。 */
  function _drainUnconfirmedInterjections(convId: string): string[] {
    const pend = pendingInterjections[convId] || []
    if (!pend.length) return []
    // 未确认的才返回给调用方兜底；已 commit 的只出队（run 自己作答，回填或
    // 重发都会造成重复提问）。
    const texts = pend.filter(p => !p.committed).map(p => p.content)
    for (const p of [...pend]) _removePendingInterjection(convId, p.localId)
    return texts
  }

  /** 未确认条目对应的助手绑定（与 _drainUnconfirmedInterjections 同序）。 */
  function _drainUnconfirmedAssistantIds(convId: string): Array<string | null> {
    const pend = pendingInterjections[convId] || []
    return pend.filter(p => !p.committed).map(p => p.assistantId ?? null)
  }

  /** 该文本是否已在本轮 run 中 commit（echo 丢失的 resume 场景，A4.9 I4）——
   *  只扫 (boundary, drainTail] 区间的服务器 user 消息：上限=兜底启动时的
   *  末条服务器消息（第四波 ②）——flush 循环内 sendMessage 新物化的行
   *  （如同批先发文本自己的落库行）落在上限之后，不参与匹配，同内容对
   *  [X,Y] 中 Y 不会误吞 X 的行；且逐条消耗匹配（A4.9 R2 B1）。
   *  返回命中的消息 id，无命中返回 null。 */
  function _findCommittedInterjection(
    convId: string,
    text: string,
    boundaryId: string | null,
    drainTailId: string | null,
    echoConfirmed: Set<string>,
    excludeIds: Set<string>,
  ): string | null {
    const arr = messages.value[convId] || []
    if (!boundaryId) return null  // 无边界（恢复场景）→ 不去重，宁发不丢
    const boundaryIdx = arr.findIndex(m => m.id === boundaryId)
    if (boundaryIdx < 0) return null
    let upperIdx = arr.length - 1
    if (drainTailId) {
      const tailIdx = arr.findIndex(m => m.id === drainTailId)
      if (tailIdx > boundaryIdx) upperIdx = tailIdx
      // tailIdx 丢失（被刷新重排等）→ 保持全量扫描的旧语义（宁发不丢）
    }
    // 边界后的**首条 user 行**是本轮自己的 query（sendMessage 的乐观气泡，
    // 中途 refresh 会换成真实服务器 id）。它不是被 commit 的插话 —— 同文时
    // 绝不能据此跳过兜底重发，否则用户那句话被静默吞掉（需求 4② 的丢弃源）。
    let turnQueryIdx = -1
    for (let i = boundaryIdx + 1; i <= upperIdx; i++) {
      if (arr[i].role === 'user') { turnQueryIdx = i; break }
    }
    for (let i = upperIdx; i > boundaryIdx; i--) {
      if (i === turnQueryIdx) continue
      const m = arr[i]
      if (m.role === 'user' && m.content === text && _isServerMessageId(m.id) &&
          !excludeIds.has(m.id) && !echoConfirmed.has(m.id)) {
        return m.id
      }
    }
    return null
  }

  /** 向当前会话运行中的 run 提交插话。四态：steered → 等 commit-echo；
   *  not_running / unsupported_mode / 网络错误 → 移除乐观气泡，回退普通发送；
   *  rate_limited（429）→ 文本回填输入框（绝不回退发送杀在途 run）。 */
  async function sendInterjection(content: string, assistantId?: string | null) {
    const convId = currentConversationId.value
    const text = content.trim()
    if (!convId || !text) return
    const localId = `interject-pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    if (!pendingInterjections[convId]) pendingInterjections[convId] = []
    // assistantId 一并挂在条目上：兜底重发时透传，避免丢助手绑定
    // （此前只是把硬编码 null 换成一个从未被传入的形参 —— 是死管道）。
    pendingInterjections[convId].push({ localId, content: text, assistantId: assistantId ?? null })
    addContextTokenDelta(convId, text)
    const res = await chatApi.interjectStream(convId, text)
    if (res.status === 'steered') return
    // A4.9 I2：条目可能已被 done 兜底排走并自动发送——remove 返回 false 时
    // 绝不再发，杜绝同文本双 turn。
    const had = _removePendingInterjection(convId, localId)
    if (res.status === 'rate_limited') {
      // 429（队列积压，A4.9 R2 B2）：文本回填输入框稍后重发 —— 绝不回退普通
      // 发送（那会走 busy stop+retry 杀掉用户本想插话的 run）。A4.9 R3 N2：
      // had=false（done 兜底已排走并自动发送）时不得回填，否则文本既已发送
      // 又躺进输入框诱发手动重发。
      if (had) interjectionRestoreText.value = text
      return
    }
    // A4.9 I1：显式定向 convId —— RTT 期间用户可能已切换会话，回退发送绝
    // 不能落到新当前会话。
    if (had) {
      await sendMessage(text, assistantId, 0, convId)
    }
  }

  /** done/stop/error 兜底：auto-send = 自然结束竞态，未确认插话自动转为新消息
   *  （已 commit 但 echo 丢失的文本跳过——A4.9 I4 防重复）；restore = 用户主动
   *  停止，文本回填输入框。 */
  async function flushUnconfirmedInterjections(convId: string, mode: 'auto-send' | 'restore', assistantId?: string | null) {
    const pendAll = pendingInterjections[convId] || []
    if (!pendAll.length) return
    // 已 commit 的条目已进 run 上下文并由 chat.py 落库，由 run 自己作答；
    // 只有未确认的才需要兜底重发 / 回填。两类都出队 —— 队列不得滞留。
    const assistantIds = _drainUnconfirmedAssistantIds(convId)
    const texts = _drainUnconfirmedInterjections(convId)
    if (!texts.length) return
    if (mode === 'restore') {
      interjectionRestoreText.value = texts.join('\n')
      return
    }
    // A4.9 R4 残留修复：drain 时快照边界与 echo 确认集——循环内每次
    // sendMessage 都会前移活值边界并清空 echo 集，若读活值，后面的文本会
    // 失去本轮基准（echo-lost 行被划出扫描范围 → 重复发送）。第四波 ②：
    // 同时快照 drain 时末条服务器消息作为扫描上限——循环内新物化的行
    // （同批先发文本自己的落库行）不参与后续文本的匹配。
    const boundarySnapshot = _runBoundaryMessageIds[convId] ?? null
    const echoSnapshot = new Set(_echoConfirmedCommittedIds[convId] ?? [])
    const drainTailSnapshot = [...(messages.value[convId] || [])]
      .reverse().find(m => _isServerMessageId(m.id))?.id ?? null
    const matchedIds = new Set<string>()
    for (let i = 0; i < texts.length; i += 1) {
      const text = texts[i]
      const hit = _findCommittedInterjection(convId, text, boundarySnapshot, drainTailSnapshot, echoSnapshot, matchedIds)
      if (hit) {
        matchedIds.add(hit)
        continue
      }
      // 显式优先条目上记的助手绑定，其次才是调用方传入的兜底值。
      await sendMessage(text, assistantIds[i] ?? assistantId ?? null, 0, convId)
    }
  }

  async function stopStreaming(conversationId?: string) {
    const convId = conversationId || currentConversationId.value
    if (!convId) return
    // Explicitly cancel the detached agent task via the dedicated endpoint.
    // This must NOT be done by closing the SSE connection alone — a passive
    // disconnect (tab switch / browser throttle) would be indistinguishable
    // and would wrongly kill a background agent.
    await chatApi.stopStream(convId)
    const s = streamStates[convId]
    const partialContent = s?.content || ''
    const partialReasoning = s?.reasoning || ''
    if (s?.abortController) {
      try { s.abortController.abort() } catch {}
    }
    endStreaming(convId)
    currentError.value = null
    // 停止时仍未确认的插话：run 已被取消，队列不再被消费 —— 文本回填输入框，
    // 绝不在用户明确停止后隐式开启新 turn。先于锚计算
    // （A4.9 M1：气泡移除会改变列表，先取锚会被合成气泡干扰）。
    await flushUnconfirmedInterjections(convId, 'restore')
    // minor㉑（A4.9 第四波）：零部分回答（首 token 前停止）时后端 cancel 处理器
    // 不落库空回答（chat.py 空内容守卫）——syncAfterAbort 的追平目标永不出现，
    // 白轮询 Phase-1+Phase-2 约 14s。此形态一次 refresh 直收即可。
    if (!partialContent.trim() && !partialReasoning.trim()) {
      await refreshConversation(convId)
      // A4.9 I-A（minors 波评审）：客户端零内容 ≠ 服务端零落库——工具阶段停止时
      // 服务端 tool_results 累积非空仍会落库（chat.py:3590 谓词含 _stop_tool_results），
      // 且媒体本地化可超 2s 看守使 commit 晚于本次 refresh。3s 后补一次复查刷新
      // （epoch/流态守卫：新流接管则不碰）。
      const epochAtStop = messagesEpoch[convId] || 0
      window.setTimeout(() => {
        if ((messagesEpoch[convId] || 0) !== epochAtStop) return
        if (streamStates[convId]?.streaming) return
        void refreshConversation(convId)
      }, 3000)
      return
    }
    // 项3：锚=当前最新服务器消息（被停轮的用户行通常已落库或被合成）；need=1
    // （cancel 处理器落库的部分回答）。若本轮用户气泡仍是合成 id，锚会自动
    // 回退到上一条服务器消息——锚后需出现 U'+A' 两条，need=1 依旧正确
    // （尾部持久化须为 assistant）。
    const turnAnchor: TurnAnchor = {
      id: _newestPersistedId(messages.value[convId] || []),
      need: 1,
    }
    // The backend persists the partial reply in the cancel handler — poll the
    // conversation so the partial assistant bubble appears without a manual
    // reload (the deliberate stop does not go through the onError recovery
    // path that normally triggers syncAfterAbort).
    await syncAfterAbort(convId, turnAnchor, partialContent, partialReasoning)
  }

  function resetState() {
    for (const s of Object.values(streamStates)) {
      if (s.abortController) {
        try { s.abortController.abort() } catch {}
      }
    }
    Object.keys(streamStates).forEach(k => delete streamStates[k])
    Object.keys(pendingInterjections).forEach(k => delete pendingInterjections[k])
    Object.keys(_runBoundaryMessageIds).forEach(k => delete _runBoundaryMessageIds[k])
    Object.keys(_echoConfirmedCommittedIds).forEach(k => delete _echoConfirmedCommittedIds[k])
    interjectionRestoreText.value = ''
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
      const conversation = await chatApi.getConversation(convId, { include: 'slim' })
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
        max_turns: conversation.deathmatch_max_turns ?? 0,
        grilling_completed: conversation.deathmatch_grilling_completed || 0,
        grilling_total: conversation.deathmatch_grilling_total || 0,
        grilling_round: conversation.deathmatch_grilling_round || 0,
        grilling_round_total: conversation.deathmatch_grilling_round_total || 3,
        message: '',
      }
      _streamVersion.value++

      // 死磕盘问兜底：轮询发现 grilling 且本地产问题为空或落后于服务端轮次
      // （跨标签页回答/后端侧状态变化等非本页 SSE 路径）时补拉权威问题列表
      // ——仅判断为空会让「另一标签页答完本轮」的页面卡在空轮次 spinner
      // （A4.9 r1 Important-2）。
      const grillingRoundNow = conversation.deathmatch_grilling_round || 1
      const localMaxGrillingRound = grillingQuestions.value.length > 0
        ? Math.max(...grillingQuestions.value.map(q => q.round || 1))
        : 0
      if (conversation.deathmatch_status === 'grilling' && localMaxGrillingRound < grillingRoundNow) {
        void fetchGrillingQuestions(convId)
      }

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
    pendingInterjections,
    interjectionRestoreText,
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
    modelAliases,
    modelsDefaultAlias,
    loadModels,
    capabilitiesForAlias,
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
    ensureMessageFull,
    ensureFullPayloads,
    loadOlderMessages,
    hasMoreMessages,
    deleteConversation,
    removeConversationFromList,
    bulkDeleteConversations,
    updateConversationTitle,
    sendMessage,
    sendInterjection,
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
