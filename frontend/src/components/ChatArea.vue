<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="chat-area">
    <BackgroundTaskPanel />
    <div class="chat-header">
      <h2 class="chat-title">
        {{ currentTitle }}
      </h2>
      <span
        v-if="chatStore.currentContextInfo"
        class="context-token-chip"
        :title="contextTokenTooltip"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
        上下文 {{ formatContextTokens(chatStore.currentContextInfo.tokens) }} tokens
      </span>
      <button
        v-if="chatStore.currentConversationId && chatStore.currentMessages.length > 0 && !saveMode"
        class="header-save-note-btn"
        @click="enterSaveMode"
        title="选择消息添加到笔记"
        aria-label="选择消息添加到笔记"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
        </svg>
      </button>
    </div>

    <div v-if="saveMode" class="save-selection-bar">
      <div class="save-bar-left">
        <label class="select-all-label">
          <input type="checkbox" :checked="selectedMessageIds.size === chatStore.currentMessages.length && chatStore.currentMessages.length > 0" @change="toggleSelectAll" />
          全选
        </label>
        <span class="selected-count">{{ selectedMessageIds.size }} 已选</span>
      </div>
      <div class="save-bar-right" ref="saveBarRightRef">
        <button class="save-download-btn" :disabled="selectedMessageIds.size === 0" @click.stop="handleDownloadClick">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          下载PDF
        </button>
        <button class="save-confirm-btn" :disabled="selectedMessageIds.size === 0" @click="openNotebookPicker">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
          </svg>
          添加到笔记
        </button>
        <button class="save-cancel-btn" @click="exitSaveMode">取消</button>
        <div v-if="showDownloadPopup" class="download-popup">
          <button class="download-popup-btn" @click="handleDownloadSingle">将多个对话作为一个 PDF 下载</button>
          <button class="download-popup-btn" @click="handleDownloadBulk">将每个对话作为一个 PDF 下载</button>
        </div>
      </div>
    </div>

    <ConversationTimeline :zen="zen" @jump-to-message="scrollToMessageById" />

    <!-- Deathmatch (死磕) mode status bar -->
    <div
      v-if="chatStore.deathmatchMode || chatStore.currentStreamingDeathmatchVerdict"
      class="deathmatch-status-bar"
      :class="{
        'dm-grilling': chatStore.currentStreamingDeathmatchVerdict?.status === 'grilling',
        'dm-active': chatStore.currentStreamingDeathmatchVerdict?.status === 'active',
        'dm-paused': chatStore.currentStreamingDeathmatchVerdict?.status === 'paused',
        'dm-partial': chatStore.currentStreamingDeathmatchVerdict?.status === 'partial_complete',
        'dm-done': chatStore.currentStreamingDeathmatchVerdict?.status === 'done',
        'dm-human-gate': chatStore.currentStreamingDeathmatchVerdict?.status === 'human_gate',
      }"
    >
      <div class="dm-status-icon">
        <svg v-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'grilling'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="11" y1="14" x2="14" y2="11"/>
        </svg>
        <svg v-else-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'active'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M15 12l-8.5 8.5c-.83.83-2.17.83-3 0 0 0 0 0 0 0a2.12 2.12 0 0 1 0-3L12 9"/>
          <path d="M17.64 15L22 10.64"/>
        </svg>
        <svg v-else-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'done'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
        </svg>
        <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M15 12l-8.5 8.5c-.83.83-2.17.83-3 0 0 0 0 0 0 0a2.12 2.12 0 0 1 0-3L12 9"/>
          <path d="M17.64 15L22 10.64"/>
        </svg>
      </div>
      <div class="dm-status-text">
        <span v-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'grilling'">
          死磕模式 · 盘问阶段 {{ chatStore.currentStreamingDeathmatchVerdict?.grilling_total ? `(${chatStore.currentStreamingDeathmatchVerdict.grilling_completed}/${chatStore.currentStreamingDeathmatchVerdict.grilling_total})` : '' }} — 请回答问题以明确目标
        </span>
        <span v-else-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'active'">
          死磕模式 · 第 {{ chatStore.currentStreamingDeathmatchVerdict.turns }} 轮
          <span v-if="chatStore.currentStreamingDeathmatchVerdict?.plan_steps?.length" class="dm-plan-progress">
            · 计划 {{ chatStore.currentStreamingDeathmatchVerdict.plan_steps.filter(s => s.status === 'done').length }}/{{ chatStore.currentStreamingDeathmatchVerdict.plan_steps.length }}
          </span>
        </span>
        <span v-else-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'paused'">死磕模式 · 已暂停</span>
        <span v-else-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'partial_complete'">
          死磕模式 · 目标未完全达成
          <span v-if="chatStore.currentStreamingDeathmatchVerdict?.plan_steps?.length" class="dm-plan-progress">
            · 计划 {{ chatStore.currentStreamingDeathmatchVerdict.plan_steps.filter(s => s.status === 'done').length }}/{{ chatStore.currentStreamingDeathmatchVerdict.plan_steps.length }}
          </span>
        </span>
        <span v-else-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'human_gate'">死磕模式 · 需人工介入</span>
        <span v-else-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'done'">死磕模式 · 目标已完成</span>
        <span v-else>死磕模式 · 已激活</span>
      </div>
      <div v-if="chatStore.currentStreamingDeathmatchVerdict?.message" class="dm-status-message">
        {{ chatStore.currentStreamingDeathmatchVerdict.message }}
      </div>
      <div v-if="chatStore.currentStreamingDeathmatchVerdict?.status === 'human_gate' && chatStore.currentStreamingDeathmatchVerdict?.human_gate" class="dm-human-gate-report">
        <pre>{{ chatStore.currentStreamingDeathmatchVerdict.human_gate }}</pre>
      </div>
      <div v-else-if="chatStore.currentStreamingDeathmatchVerdict?.plan_steps?.length && chatStore.currentStreamingDeathmatchVerdict?.status === 'active'" class="dm-plan-list">
        <div v-for="step in chatStore.currentStreamingDeathmatchVerdict.plan_steps" :key="step.id" class="dm-plan-step" :class="{ done: step.status === 'done' }">
          <span class="dm-step-mark">{{ step.status === 'done' ? '✓' : '○' }}</span>
          <span class="dm-step-id">{{ step.id }}</span>
          <span class="dm-step-desc">{{ step.description }}</span>
        </div>
      </div>
      <div v-if="chatStore.currentStreamingDeathmatchVerdict?.verdict === 'continue' && chatStore.currentStreamingDeathmatchVerdict?.status !== 'partial_complete'" class="dm-continue-indicator">
        <span class="dm-pulse"></span> 继续推进...
      </div>
    </div>

    <div class="message-list" ref="messageListRef" role="log" aria-live="polite" aria-atomic="false" aria-label="消息列表">
      <div v-if="!chatStore.currentConversationId || (chatStore.currentMessages.length === 0 && !chatStore.isStreamingCurrentConversation && !chatStore.currentStreamingContent)" class="empty-state">
        <div class="empty-icon">
          <LogoIcon :size="80" class="empty-logo" />
        </div>
        <div class="empty-brand">
          <p class="empty-brand-title">Weave Thinker</p>
          <p class="empty-brand-subtitle">Weave Thinker</p>
        </div>
        <p class="empty-lead">一个会思考、能调研、记得住你的 AI 伙伴</p>
        <div class="empty-try">试试这些：</div>
        <div class="empty-suggest-grid">
          <button
            v-for="(sg, i) in emptySuggestions"
            :key="i"
            class="empty-suggest-card"
            type="button"
            @click="onEmptySuggest(sg.q)"
          >
            <i class="es-no">{{ String(i + 1).padStart(2, '0') }}</i>
            <svg class="es-ico" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" v-html="sg.icon"></svg>
            <span class="es-cat">{{ sg.cat }}</span>
            <span class="es-q">{{ sg.q }}</span>
          </button>
        </div>
      </div>
      <template v-else>
        <MessageBubble
          v-if="!useVirtualMessageList"
          v-for="msg in chatStore.currentMessages"
          :key="msg.id"
          :message="msg"
          :save-mode="saveMode"
          :selected="selectedMessageIds.has(msg.id)"
          :can-regenerate="canRegenerateMessage(msg.id)"
          @toggle-select="toggleMessageSelect(msg.id)"
          @regenerate="handleRegenerate"
          @edit="handleEdit"
        />

        <div
          v-else
          :style="{ height: `${virtualMessageTotalSize}px`, position: 'relative', width: '100%' }"
        >
          <div
            v-for="virtualRow in virtualMessageItems"
            :key="virtualRow.key"
            :ref="messageVirtualizer.measureElement"
            :data-index="virtualRow.index"
            :data-message-id="chatStore.currentMessages[virtualRow.index]?.id"
            :style="{
              position: 'absolute',
              top: 0,
              left: 0,
              width: '100%',
              transform: `translateY(${virtualRow.start}px)`,
            }"
          >
            <MessageBubble
              :message="chatStore.currentMessages[virtualRow.index]"
              :save-mode="saveMode"
              :selected="selectedMessageIds.has(chatStore.currentMessages[virtualRow.index].id)"
              :can-regenerate="canRegenerateMessage(chatStore.currentMessages[virtualRow.index].id)"
              @toggle-select="toggleMessageSelect(chatStore.currentMessages[virtualRow.index].id)"
              @regenerate="handleRegenerate"
              @edit="handleEdit"
            />
          </div>
        </div>

        <div v-if="chatStore.isStreamingCurrentConversation || chatStore.currentStreamingContent" class="streaming-message" :class="{ 'processing': showProcessingSpinner }">
          <div class="avatar">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="10" fill="var(--color-primary)"/>
              <path d="M8 10h8M8 14h5" stroke="white" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="content">
            <!-- During grilling: only show progress spinner, hide all subagent details -->
            <template v-if="isGrilling">
              <div class="processing-spinner">
                <div class="spinner-circle"></div>
                <span class="spinner-label">{{ grillingLoadingText }}</span>
              </div>
            </template>
            <!-- Normal (non-grilling) streaming: show all details -->
            <template v-else>
              <!-- Waiting for the first SSE event: blinking cursor + hint so
                   the user gets immediate feedback after sending a query -->
              <div v-if="showWaitingIndicator" class="waiting-indicator">
                <span class="waiting-indicator-text">正在思考…</span>
                <span class="cursor"></span>
              </div>
              <div v-if="showProcessingSpinner" class="processing-spinner">
                <div class="spinner-circle"></div>
                <span class="spinner-label">{{ processingStatusText }}</span>
              </div>
              <SubAgentStatus :thinking="chatStore.currentStreamingSubAgentThinking" />
              <TaskProgress :progress="chatStore.currentStreamingTaskProgress" />
              <!-- Tool loop metadata: iteration badge (contextual info, not a panel) -->
              <div
                v-if="chatStore.currentStreamingIteration"
                class="iteration-context"
              >
                <span class="iteration-badge">第 {{ chatStore.currentStreamingIteration.current }} 轮</span>
              </div>
              <FileAttachmentComp
                v-if="chatStore.currentStreamingFileAttachments && chatStore.currentStreamingFileAttachments.length"
                :attachments="chatStore.currentStreamingFileAttachments"
              />
              <SearchResults
                v-if="streamingSearchResults.length > 0 || chatStore.currentStreamingSearchProgress.length > 0 || chatStore.currentStreamingSearchFailed"
                :results="streamingSearchResults"
                :rounds="streamingSearchRounds"
                :progressRounds="chatStore.currentStreamingSearchProgress"
                :searchFailed="!!chatStore.currentStreamingSearchFailed"
                :failedData="chatStore.currentStreamingSearchFailed"
              />
              <!-- Single chronological timeline: all events rendered by arrival order -->
              <template v-if="timelineItems.length > 0">
                <template v-for="(item, idx) in timelineItems" :key="item.part_id || 'tl-' + idx">
                  <!-- Reasoning: collapsible thinking panel (💭 思考过程).
                       Live-thinking streams one reasoning block PER iteration;
                       the actively-streaming block (last timeline item) stays
                       expanded while generating, earlier rounds auto-collapse
                       once newer items arrive, and every block can be toggled
                       manually. Previously these rendered inline with no
                       <details>, so the first round's thinking could never be
                       folded away. -->
                  <div v-if="item.type === 'reasoning'" class="reasoning-block">
                    <details :open="isReasoningBlockOpen(idx)">
                      <summary class="reasoning-summary" @click.prevent="toggleReasoningBlock(idx)">💭 思考过程</summary>
                      <div class="reasoning-text">
                        <StreamMarkdown :content="item.content" />
                      </div>
                    </details>
                  </div>
                  <!-- Tool call: self-contained state card (F1-2) -->
                  <ToolPartCard v-else-if="item.type === 'tool_call'" :item="item" />
                  <!-- Context tool group: folded consecutive read-only tools (F0-2) -->
                  <div v-else-if="item.type === 'context_tool_group'" class="timeline-tool-call" :class="{ 'tool-error': item.status === 'error' }">
                    <details>
                      <summary class="tool-card-header">
                        <span class="tool-card-icon">{{ item.status === 'running' ? '⟳' : '✓' }}</span>
                        <span class="tool-card-name">正在检索上下文…</span>
                        <span v-if="item.status === 'running'" class="tool-card-status running">执行中…</span>
                        <span v-else class="tool-card-status done">完成</span>
                      </summary>
                      <div class="tool-card-result">
                        <div v-for="(ci, ciIdx) in parseContextTools(item.result)" :key="'ct-' + ciIdx" class="context-tool-subitem">
                          <span class="context-tool-subname">{{ formatToolName(ci.name || '') }}</span>
                          <span v-if="ci.status === 'completed'" class="tool-card-status done">完成</span>
                          <span v-else-if="ci.status === 'error'" class="tool-card-status error">失败</span>
                          <span v-else class="tool-card-status running">执行中…</span>
                          <div v-if="ci.result" class="tool-card-result-inner">
                            <StreamMarkdown :content="ci.result" />
                          </div>
                        </div>
                      </div>
                    </details>
                  </div>
                  <!-- Text: regular markdown stream -->
                  <div v-else-if="item.type === 'text'" class="timeline-text">
                    <StreamMarkdown :content="item.content" :hasSearchResults="streamingSearchResults.length > 0" /><span v-if="chatStore.isStreamingCurrentConversation && idx === timelineItems.length - 1" class="cursor"></span>
                  </div>
                  <!-- Sub-agent chunk: muted markdown with name prefix -->
                  <div v-else-if="item.type === 'sub_agent_chunk'" class="timeline-subagent">
                    <details>
                      <summary class="timeline-subagent-summary">🧠 子代理 · {{ item.subtask_name || 'subtask' }}</summary>
                      <div v-if="item.reasoning_content" class="timeline-reasoning">
                        <StreamMarkdown :content="item.reasoning_content" />
                      </div>
                      <div v-if="item.content" class="timeline-text">
                        <StreamMarkdown :content="item.content" />
                      </div>
                    </details>
                  </div>
                  <!-- Agent step: existing details block -->
                  <div v-else class="agent-step-block">
                    <details open>
                      <summary class="agent-step-summary">⚙️ {{ formatStepTitle(item) }}</summary>
                      <div class="agent-step-text" v-if="item.step_type !== 'llm'">{{ item.content }}</div>
                      <div class="agent-step-text" v-else>{{ stepRevealedTexts[idx] ?? item.content }}<span v-if="stepRevealing[idx]" class="cursor"></span></div>
                    </details>
                  </div>
                </template>
              </template>
              <!-- Fallback: only when no displaySequence and legacy content exists -->
              <template v-else-if="chatStore.currentStreamingContent || chatStore.currentStreamingReasoningContent">
                <div v-if="chatStore.currentStreamingReasoningContent" class="reasoning-block">
                  <details open>
                    <summary class="reasoning-summary">💭 思考过程</summary>
                    <div class="reasoning-text">
                      <StreamMarkdown :content="chatStore.currentStreamingReasoningContent" />
                    </div>
                  </details>
                </div>
                <div class="text">
                  <StreamMarkdown :content="chatStore.currentStreamingContent" :hasSearchResults="streamingSearchResults.length > 0" />
                </div>
              </template>
            </template>
          </div>
        </div>

        <div v-if="chatStore.currentError" class="error-message">
          {{ chatStore.currentError }}
        </div>

        <!-- Grilling questions rendered as chat bubbles inside the message list -->
        <template v-if="isGrilling">
          <div class="grilling-round-header">
            第 {{ currentGrillingRound }}/{{ currentGrillingRoundTotal }} 轮盘问
          </div>
          <div v-if="currentRoundQuestions.length === 0 || grillingGenerating" class="grilling-loading">
            <div class="spinner-circle"></div>
            <span class="spinner-label">{{ grillingLoadingText }}</span>
          </div>
          <div
            v-for="(q, idx) in currentRoundQuestions"
            :key="'grill-' + q.task_id"
            class="grilling-bubble"
            :class="{ 'grilling-bubble-answered': !!chatStore.grillingAnswers[q.task_id] }"
          >
            <div class="grilling-bubble-avatar">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="10" fill="var(--color-primary)"/>
                <path d="M8 10h8M8 14h5" stroke="white" stroke-width="2" stroke-linecap="round"/>
              </svg>
            </div>
            <div class="grilling-bubble-content">
              <div class="grilling-q-header">
                <span class="grilling-q-number">{{ idx + 1 }}</span>
                <span class="grilling-q-text">{{ q.question }}</span>
                <span v-if="chatStore.grillingAnswers[q.task_id]" class="grilling-q-status">已回答</span>
              </div>
              <div v-if="q.recommendation && !chatStore.grillingAnswers[q.task_id]" class="grilling-q-recommendation">
                {{ q.recommendation }}
              </div>
              <div v-if="chatStore.grillingAnswers[q.task_id]" class="grilling-q-answered-text">
                {{ chatStore.grillingAnswers[q.task_id] }}
              </div>
              <div v-if="!chatStore.grillingAnswers[q.task_id]" class="grilling-q-answer">
                <div class="grilling-options" v-if="q.options && q.options.length > 0">
                  <button
                    v-for="(opt, oi) in q.options"
                    :key="oi"
                    class="grilling-option-btn"
                    :class="{ 'grilling-option-selected': grillingInputMap[q.task_id] === opt }"
                    @click="grillingInputMap[q.task_id] = opt; grillingOtherMap[q.task_id] = false"
                    :disabled="grillingSubmitting[q.task_id]"
                  >
                    {{ opt }}
                  </button>
                  <button
                    class="grilling-option-btn grilling-option-other"
                    :class="{ 'grilling-option-selected': grillingOtherMap[q.task_id] }"
                    @click="grillingOtherMap[q.task_id] = true; grillingInputMap[q.task_id] = ''"
                    :disabled="grillingSubmitting[q.task_id]"
                  >
                    其他答案
                  </button>
                </div>
                <textarea
                  v-if="grillingOtherMap[q.task_id] || (!q.options || q.options.length === 0)"
                  v-model="grillingInputMap[q.task_id]"
                  placeholder="请输入你的回答..."
                  class="grilling-answer-input"
                  rows="2"
                  :disabled="grillingSubmitting[q.task_id]"
                />
                <button
                  class="grilling-submit-btn grilling-answer-submit-btn"
                  :disabled="!grillingInputMap[q.task_id]?.trim() || grillingSubmitting[q.task_id]"
                  @click="submitGrillingAnswer(q.task_id)"
                >
                  <span v-if="grillingSubmitting[q.task_id]" class="grilling-btn-spinner"></span>
                  <span>{{ grillingSubmitting[q.task_id] ? '提交中...' : '提交回答' }}</span>
                </button>
              </div>
            </div>
          </div>
        </template>
      </template>
    </div>

    <NotebookPicker
      v-if="showPicker"
      @select="handleSaveToNotebook"
      @close="showPicker = false"
    />

    <Teleport to="body">
      <ExportProgressDialog
        :visible="exportDialogVisible"
        format="pdf"
        :status="exportDialogStatus"
        :progress="exportDialogProgress"
        @cancel="cancelChatExport"
        @close="closeExportDialog"
      />
    </Teleport>

    <ChatInput v-if="!saveMode" ref="chatInputRef" @sent="onUserMessageSent" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { useChatStore } from '@/stores/chat'
import { useNotesStore } from '@/stores/notes'
import { useToast } from '@/composables/useToast'
import { downloadBlob } from '@/composables/useDownload'
import { stripDsmlTags } from '@/composables/useMarkdown'
import api from '@/api/client'
import type { SearchResult, DisplaySequenceItem } from '@/types'
import MessageBubble from './MessageBubble.vue'
import ChatInput from './ChatInput.vue'
import StreamMarkdown from './StreamMarkdown.vue'
import ToolPartCard from './ToolPartCard.vue'
import NotebookPicker from './NotebookPicker.vue'
import SearchResults from './SearchResults.vue'
import SubAgentStatus from './SubAgentStatus.vue'
import TaskProgress from './TaskProgress.vue'
import FileAttachmentComp from './FileAttachment.vue'
import BackgroundTaskPanel from './BackgroundTaskPanel.vue'
import ConversationTimeline from './ConversationTimeline.vue'
import LogoIcon from './LogoIcon.vue'
import ExportProgressDialog from './ExportProgressDialog.vue'

const chatStore = useChatStore()
const notesStore = useNotesStore()
const { show: showToast } = useToast()

defineProps<{ zen?: boolean }>()

const streamingSearchResults = computed(() => {
  try {
    const statuses = chatStore.currentStreamingToolStatuses
    const webSearch = statuses.find(s => s.name === 'web_search')
    if (webSearch?.results?.length) return webSearch.results

    const toolResults = chatStore.currentStreamingToolResults
    if (!Array.isArray(toolResults)) return []

    const searchResults: SearchResult[] = []
    for (const tr of toolResults) {
      if (tr?.name === 'web_search' && !tr.error) {
        try {
          const parsed = JSON.parse(tr.result)
          const hits = parsed.results
          if (Array.isArray(hits)) {
            for (const h of hits) {
              if (h?.url) {
                searchResults.push({
                  title: (h.title || '').slice(0, 200),
                  url: h.url,
                  snippet: (h.snippet || '').slice(0, 300),
                  published_date: h.published_date || null,
                })
              }
            }
          }
        } catch {}
      }
    }
    return searchResults
  } catch {
    return []
  }
})

const streamingSearchRounds = computed(() => {
  try {
    // Live rounds built from the actual web_search tool_call events (conv
    // a3cfb421 2026-08-09): the backend's `search_progress`/`tool_status`
    // events are never emitted, so the old code always fell back to the
    // `['web_search']` placeholder. Each web_search call (in arrival order)
    // is one round carrying ITS OWN queries — mirroring the persisted
    // rounds built by `_transform_tool_loop_results` on the backend, so the
    // live view matches what the message shows after refresh.
    //
    // Round-emission mirrors the backend (A4.9 review finding): a round is
    // only appended when the search RESULT exists and has hits — a failed or
    // empty search never produces a round in the persisted view, and the
    // live view must not count it either (otherwise live shows N rounds
    // while the persisted message shows fewer).
    const calls = chatStore.currentStreamingToolCalls || []
    const results = chatStore.currentStreamingToolResults || []
    const rounds: Array<Record<string, unknown>> = []
    let emitted = 0
    // Mirror the backend `search_round_idx` numbering exactly: the round
    // number is the call's POSITION among all web_search calls (failed /
    // empty searches still consume a number, producing gaps like
    // [{round:1},{round:3}]) — matching _transform_tool_loop_results.
    let wsIndex = 0
    for (const c of calls) {
      if (c?.name !== 'web_search') continue
      wsIndex += 1
      const result = results.find(r => r.call_id === c.call_id)
      let hitCount = 0
      let isDigest = false
      if (result && !result.error) {
        const raw = (result.result || '').trim()
        if (raw.startsWith('<tool-digest>')) {
          // Digested search results (agent.tool_digest): the raw JSON is
          // replaced by an envelope before the tool_result event — treat it
          // as a round with hits (the persisted view recovers the archive
          // and shows the round), otherwise live view would under-report
          // rounds for every large search (A4.9 re-review finding).
          isDigest = true
          hitCount = 1
        } else {
          try {
            const parsed = JSON.parse(raw)
            if (Array.isArray(parsed.results)) hitCount = parsed.results.length
          } catch {}
        }
      }
      if (!result || hitCount === 0) continue
      emitted += 1
      const queries = Array.isArray((c?.arguments as any)?.queries)
        ? (c.arguments as any).queries.map(String)
        : []
      rounds.push({
        round: wsIndex,
        queries: queries.length ? queries : [`web_search_${wsIndex}`],
        qualified: true,
        cn_en_count: hitCount,
        total_count: hitCount,
        _digested: isDigest || undefined,
      })
    }
    if (rounds.length > 0) return rounds
    if (streamingSearchResults.value?.length > 0) {
      return [{ round: 1, queries: ['web_search'], qualified: true, cn_en_count: streamingSearchResults.value.length, total_count: streamingSearchResults.value.length }]
    }
    return []
  } catch {
    return []
  }
})

// Tool loop helpers
function findToolResult(callId: string) {
  return chatStore.currentStreamingToolResults.find(r => r.call_id === callId)
}

// F0: single chronological timeline — all streaming events ordered by arrival
const CONTEXT_TOOLS = new Set([
  'workspace_read', 'workspace_glob', 'grep', 'session_search',
  'web_search', 'diff', 'context7_resolve_library_id', 'context7_query_docs',
  'word_count', 'skill_view',
])

const timelineItems = computed(() => {
  const seq = chatStore.currentStreamingDisplaySequence || []
  if (seq.length === 0) return []

  // F0-2: fold consecutive context-gathering tools into groups
  const grouped: DisplaySequenceItem[] = []
  let i = 0
  while (i < seq.length) {
    const item = seq[i]
    if (item.type === 'tool_call' && CONTEXT_TOOLS.has(item.name || '')) {
      const groupItems = [item]
      let j = i + 1
      while (j < seq.length && seq[j].type === 'tool_call' && CONTEXT_TOOLS.has(seq[j].name || '')) {
        groupItems.push(seq[j])
        j++
      }
      if (groupItems.length > 1) {
        // Fold into a context-tool-group item
        const allCompleted = groupItems.every(it => it.status === 'completed' || it.status === 'error')
        grouped.push({
          type: 'context_tool_group',
          content: '',
          name: 'context_tools',
          status: allCompleted ? 'completed' : 'running',
          result: JSON.stringify(groupItems.map(it => ({
            call_id: it.call_id,
            name: it.name,
            status: it.status,
            result: it.result,
          }))),
        })
      } else {
        grouped.push(item)
      }
      i = j
    } else {
      grouped.push(item)
      i++
    }
  }
  return grouped
})

// Tool result expand/collapse state (per timeline index)
const toolResultExpanded = ref<Record<number, boolean>>({})

// The actively-streaming reasoning block stays expanded so the user watches
// the thinking live. "Active" = the LAST reasoning block in the timeline —
// NOT merely the last item: when the answer/tool part arrives after the
// thinking, the thinking block must NOT collapse. Collapsing it removed its
// ~10k+ px of content from the layout and the browser clamped the viewport
// back up to the block start — a bottom-pinned user following the thinking
// got yanked to "the start of the thinking block" and could not scroll down
// past it (scroll-yank regression). Earlier rounds still auto-collapse once
// a NEWER reasoning block arrives. Manual toggling always wins: once the
// user clicks a thinking block, that state persists across stream re-renders
// (without this, the `:open` binding re-applied over the user's choice).
const reasoningExpanded = ref<Record<number, boolean>>({})
const lastReasoningIndex = computed(() => {
  const items = timelineItems.value
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].type === 'reasoning') return i
  }
  return -1
})
const isReasoningBlockActive = (idx: number): boolean =>
  chatStore.isStreamingCurrentConversation && idx === lastReasoningIndex.value
const isReasoningBlockOpen = (idx: number): boolean =>
  reasoningExpanded.value[idx] !== undefined
    ? reasoningExpanded.value[idx]
    : isReasoningBlockActive(idx)
function toggleReasoningBlock(idx: number) {
  reasoningExpanded.value = { ...reasoningExpanded.value, [idx]: !isReasoningBlockOpen(idx) }
}
function toggleToolResult(idx: number) {
  toolResultExpanded.value = { ...toolResultExpanded.value, [idx]: !toolResultExpanded.value[idx] }
}

function parseContextTools(resultJson: string): Array<{ call_id?: string; name?: string; status?: string; result?: string }> {
  try {
    return JSON.parse(resultJson || '[]')
  } catch {
    return []
  }
}

// Blinking-cursor waiting state for two windows:
// (1) right after the user sends a query, before the first SSE event arrives;
// (2) between the end of a tool round and the start of the next output part
// (thinking / final answer) — the stream is alive but no visible content has
// been produced yet. It is suppressed whenever the LAST timeline item already
// carries visible output (streaming text / reasoning / sub-agent content),
// so tool cards alone no longer leave this gap without any indicator.
//
// Deathmatch note: a deathmatch goal loop is ONE long SSE stream spanning
// many rounds, and `s.content`/`s.reasoning` accumulate across rounds (only
// cleared at stream end). The accumulated `currentStreamingContent` must NOT
// suppress the indicator in later rounds — the last timeline item is the
// authoritative signal in partMode (a completed tool card has no visible
// output → show the indicator while the next LLM part is generated).
const showWaitingIndicator = computed(() => {
  if (!chatStore.isStreamingCurrentConversation) return false
  if (showProcessingSpinner.value) return false
  const seq = chatStore.currentStreamingDisplaySequence || []
  if (seq.length > 0) {
    const last = seq[seq.length - 1]
    return !(last.content || last.reasoning_content)
  }
  // No timeline items yet: the legacy pre-part fallback streams raw content
  // directly into the bubble — hide the indicator while that content flows.
  if (chatStore.currentStreamingContent || chatStore.currentStreamingReasoningContent) return false
  return true
})

const showProcessingSpinner = computed(() => {  const thinking = chatStore.currentStreamingSubAgentThinking
  if (thinking && thinking.status === 'running') return true

  const task = chatStore.currentStreamingTaskProgress
  if (task && (task.status === 'planning' || task.status === 'executing')) return true

  const tools = chatStore.currentStreamingToolCalls || []
  if (tools.length === 0) return false
  // Spinner is visible when at least one tool call hasn't received a result yet
  return tools.some(tc => !findToolResult(tc.call_id))
})

const processingStatusText = computed(() => {
  const tools = chatStore.currentStreamingToolCalls || []
  const pendingTools = tools.filter(tc => !findToolResult(tc.call_id))
  if (pendingTools.length > 0) {
    const names = pendingTools.map(tc => formatToolName(tc.name)).join('、')
    return `正在执行: ${names}...`
  }
  const thinking = chatStore.currentStreamingSubAgentThinking
  if (thinking && thinking.status === 'running') {
    return thinking.content || '正在分析...'
  }
  const task = chatStore.currentStreamingTaskProgress
  if (task && (task.status === 'planning' || task.status === 'executing')) {
    return task.status === 'planning' ? '正在规划任务...' : '正在执行任务...'
  }
  return '处理中...'
})

const TOOL_NAME_MAP: Record<string, string> = {
  web_search: '联网搜索',
  browser: '浏览网页',
  code_execution: '代码执行',
  memory: '记忆',
  delegate_task: '任务委派',
  pdf_export: '导出 PDF',
  word_count: '字数统计',
  workspace_read: '文件读取',
  workspace_glob: '文件搜索',
  provide_file: '提供文件',
}
function formatToolName(name: string): string {
  return TOOL_NAME_MAP[name] || name
}
// Localize agent-step / display-sequence titles when the backend stored the
// raw English tool name as the title (happens for resumed/replayed streams).
function formatStepTitle(item: any): string {
  if (!item) return ''
  const isToolStep = item.type === 'tool_call' || item.step_type === 'tool_call' || item.step_type === 'tool'
  const rawName = item.name || ''
  if (isToolStep && rawName && TOOL_NAME_MAP[rawName]) return TOOL_NAME_MAP[rawName]
  const title = item.title || ''
  if (title && TOOL_NAME_MAP[title]) return TOOL_NAME_MAP[title]
  return title || rawName || '工具调用'
}

// 上下文 token 用量显示（头部徽章）。千分位 + 窗口占比 tooltip。
function formatContextTokens(n: number): string {
  return (n || 0).toLocaleString('en-US')
}
const contextTokenTooltip = computed(() => {
  const info = chatStore.currentContextInfo
  if (!info || !info.context_length) return ''
  const pct = info.context_length > 0
    ? ((info.tokens / info.context_length) * 100).toFixed(1)
    : '0.0'
  return `上下文窗口 ${info.context_length.toLocaleString('en-US')} tokens · 本轮占 ${pct}%`
})
const messageListRef = ref<HTMLElement | null>(null)

// Virtual scrolling for the message list. Long conversations with heavy
// Markdown/KaTeX/Mermaid payloads can render hundreds of DOM nodes; the
// virtualizer keeps only the visible messages mounted.
const MESSAGE_VIRTUAL_SCROLL_THRESHOLD = 30
const useVirtualMessageList = computed(() =>
  chatStore.currentMessages.length > MESSAGE_VIRTUAL_SCROLL_THRESHOLD &&
  !chatStore.searchHighlightNonce &&
  !saveMode.value
)

const messageVirtualizer = useVirtualizer(
  computed(() => ({
    count: chatStore.currentMessages.length,
    getScrollElement: () => messageListRef.value,
    estimateSize: () => 120,
    measureElement: (el) => {
      const h = el.getBoundingClientRect().height
      return h > 0 ? h : 120
    },
    overscan: 5,
    getItemKey: (index) => chatStore.currentMessages[index]?.id ?? index,
  }))
)

const virtualMessageItems = computed(() => messageVirtualizer.value.getVirtualItems())
const virtualMessageTotalSize = computed(() => messageVirtualizer.value.getTotalSize())

const chatInputRef = ref<InstanceType<typeof ChatInput> | null>(null)

const emptySuggestions = [
  { cat: '写作', q: '帮我写一封本周工作周报', icon: '<path d="M17 3a2.83 2.83 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>' },
  { cat: '学习', q: '用费曼技巧解释量子纠缠', icon: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>' },
  { cat: '代码', q: '调试一段 Python 报错', icon: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>' },
  { cat: '调研', q: '对比三款旗舰手机的影像系统', icon: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>' },
]
function onEmptySuggest(q: string) {
  chatInputRef.value?.setEditContent(q)
}
const saveBarRightRef = ref<HTMLElement | null>(null)
// Shared with ChatLayout's mobile header button (icon-only entry point there).
const saveMode = computed({
  get: () => chatStore.saveModeActive,
  set: (v: boolean) => { chatStore.saveModeActive = v },
})
const selectedMessageIds = ref<Set<string>>(new Set())
const showPicker = ref(false)
const showDownloadPopup = ref(false)
const downloadLoading = ref(false)
const exportDialogVisible = ref(false)
const exportDialogStatus = ref<'exporting' | 'success'>('exporting')
const exportDialogProgress = ref(0)
const grillingInputMap = ref<Record<string, string>>({})
const grillingOtherMap = ref<Record<string, boolean>>({})
const grillingSubmitting = ref<Record<string, boolean>>({})
const grillingGenerating = ref(false)

const isGrilling = computed(() =>
  chatStore.currentStreamingDeathmatchVerdict?.status === 'grilling' &&
  chatStore.grillingQuestions.length > 0
)

const currentGrillingRound = computed(() =>
  chatStore.currentStreamingDeathmatchVerdict?.grilling_round ||
  chatStore.conversations.find(c => c.id === chatStore.currentConversationId)?.deathmatch_grilling_round ||
  1
)

const currentGrillingRoundTotal = computed(() =>
  chatStore.currentStreamingDeathmatchVerdict?.grilling_round_total ||
  chatStore.conversations.find(c => c.id === chatStore.currentConversationId)?.deathmatch_grilling_round_total ||
  3
)

const currentRoundQuestions = computed(() => {
  const round = currentGrillingRound.value
  return chatStore.grillingQuestions.filter(q => (q.round || 1) === round)
})

const grillingLoadingText = computed(() => {
  if (currentGrillingRound.value >= currentGrillingRoundTotal.value && grillingGenerating.value) {
    return '正在生成咨询摘要...'
  }
  return '正在生成细节咨询...'
})

async function submitGrillingAnswer(taskId: string) {
  const answer = grillingInputMap.value[taskId]?.trim()
  if (!answer) return
  grillingSubmitting.value[taskId] = true
  // Show a global generating indicator when this answer may complete the round.
  const pendingInRound = currentRoundQuestions.value.filter(q => !chatStore.grillingAnswers[q.task_id] && q.task_id !== taskId).length
  if (pendingInRound === 0) {
    grillingGenerating.value = true
  }
  try {
    const data = await chatStore.answerGrillingQuestion(taskId, answer)
    grillingInputMap.value[taskId] = ''
    grillingOtherMap.value[taskId] = false
    if (data?.result?.status === 'next_round') {
      grillingInputMap.value = {}
      grillingOtherMap.value = {}
    } else if (data?.result?.status === 'grilling_complete') {
      grillingInputMap.value = {}
      grillingOtherMap.value = {}
    }
  } finally {
    grillingSubmitting.value[taskId] = false
    grillingGenerating.value = false
  }
}

// Agent step typing reveal state
const stepRevealedTexts = ref<Record<number, string>>({})
const stepRevealing = ref<Record<number, boolean>>({})
const stepTimers: Record<number, ReturnType<typeof setInterval>> = {}

watch(() => chatStore.currentStreamingAgentSteps, (steps) => {
  for (let idx = 0; idx < steps.length; idx++) {
    const step = steps[idx]
    if (step.step_type !== 'llm' || stepTimers[idx] !== undefined) continue
    // Start reveal animation for new LLM step
    stepRevealing.value[idx] = true
    stepRevealedTexts.value[idx] = ''
    let charIdx = 0
    const fullText = step.content
    stepTimers[idx] = setInterval(() => {
      if (charIdx >= fullText.length) {
        clearInterval(stepTimers[idx])
        stepRevealing.value = { ...stepRevealing.value, [idx]: false }
        return
      }
      charIdx += 4
      stepRevealedTexts.value = { ...stepRevealedTexts.value, [idx]: fullText.slice(0, charIdx) }
    }, 30)
  }
}, { deep: true })

watch(() => chatStore.isStreaming, (streaming) => {
  if (!streaming) {
    // Clear all timers on stream end
    for (const key in stepTimers) {
      clearInterval(stepTimers[key])
      delete stepTimers[key]
    }
    stepRevealedTexts.value = {}
    stepRevealing.value = {}
  }
})

const currentTitle = computed(() => {
  if (!chatStore.currentConversationId) return '新对话'
  // conversationMeta covers conversations fetched by id that are NOT in the
  // sidebar list — deep-linked foreign-assistant conversations (conv
  // 6b0faf81): those are intentionally excluded from `conversations` (which
  // only holds the current assistant's rows) but must still show their title.
  // List item first: updateConversationTitle refreshes conv.title in place,
  // so the list entry is always as fresh as (or fresher than) the meta cache.
  const conv = chatStore.conversations.find(c => c.id === chatStore.currentConversationId)
  const meta = chatStore.conversationMeta[chatStore.currentConversationId]
  return conv?.title || meta?.title || '新对话'
})

const latestAssistantMessageId = computed(() => {
  const reversed = [...chatStore.currentMessages].reverse()
  return reversed.find(message => message.role === 'assistant')?.id ?? null
})

function canRegenerateMessage(messageId: string) {
  return !saveMode.value && !chatStore.isStreaming && latestAssistantMessageId.value === messageId
}

function enterSaveMode() {
  saveMode.value = true
  selectedMessageIds.value = new Set()
}

function exitSaveMode() {
  saveMode.value = false
  selectedMessageIds.value = new Set()
  showDownloadPopup.value = false
}

// saveMode now lives in the shared store (for the mobile header button), so
// it must not leak across conversations.
watch(() => chatStore.currentConversationId, () => {
  if (saveMode.value) exitSaveMode()
})

function handleDownloadClick() {
  if (selectedMessageIds.value.size === 0) return
  if (selectedMessageIds.value.size === 1) {
    handleDownloadSingle()
  } else {
    showDownloadPopup.value = !showDownloadPopup.value
  }
}

function showExportDialog() {
  exportDialogVisible.value = true
  exportDialogStatus.value = 'exporting'
  exportDialogProgress.value = 0
}

function cancelChatExport() {
  exportDialogVisible.value = false
  exportDialogStatus.value = 'exporting'
}

function closeExportDialog() {
  exportDialogVisible.value = false
  exportDialogStatus.value = 'exporting'
}

function processMessageContentForExport(m: { role: string; content: string; tool_results?: string }): string {
  let text = m.content
  if (m.role === 'assistant' && m.tool_results) {
    text = stripExistingReferenceSection(text)
    const indexMap = computeCitationIndexMap(m.content, m.tool_results)
    if (indexMap.size > 0) {
      text = renumberInlineCitations(text, indexMap)
    }
    text += buildCitationsSection(m.tool_results, m.content)
    try {
      const tr = JSON.parse(m.tool_results)
      const attachments = (tr?.attachments ?? []) as Array<{ name?: string; path?: string; type?: string }>
      const imageAttachments = attachments.filter((a: any) => a.type === 'image' && a.path)
      if (imageAttachments.length > 0) {
        text += '\n\n' + imageAttachments.map((a: any) => `![${a.name || 'image'}](${a.path})`).join('\n')
      }
    } catch { /* ignore parse errors */ }
  }
  return text
}

async function handleDownloadSingle() {
  showDownloadPopup.value = false
  showExportDialog()
  try {
    const selected = chatStore.currentMessages.filter(m => selectedMessageIds.value.has(m.id))
    const items = selected.map(m => ({
      title: m.role === 'user' ? '用户' : '助手',
      content: processMessageContentForExport(m),
      role: m.role,
    }))
    const response = await api.post('/conversations/export-pdf', {
      items,
      action: 'single',
    }, { responseType: 'blob' })
    const titleText = selected[0].content.replace(/[#*`\n]/g, '').trim().slice(0, 30)
    const filename = `对话记录_${titleText || '对话片段'}.pdf`
    await downloadBlob(response.data, filename, 'application/pdf')
    exportDialogStatus.value = 'success'
    setTimeout(() => {
      exportDialogVisible.value = false
      exitSaveMode()
    }, 2000)
  } catch (e) {
    console.error('PDF download failed:', e)
    exportDialogVisible.value = false
    showToast('PDF 下载失败', 'error')
  }
}

async function handleDownloadBulk() {
  showDownloadPopup.value = false
  showExportDialog()
  try {
    const selected = chatStore.currentMessages.filter(m => selectedMessageIds.value.has(m.id))
    const items = selected.map((m, _idx) => ({
      title: m.role === 'user' ? '用户' : '助手',
      content: processMessageContentForExport(m),
      role: m.role,
    }))
    const response = await api.post('/conversations/export-pdf', {
      items,
      action: 'bulk',
    }, { responseType: 'blob' })
    const titleText = selected[0].content.replace(/[#*`\n]/g, '').trim().slice(0, 30)
    const filename = `对话记录_${titleText || '对话片段'}.zip`
    await downloadBlob(response.data, filename, 'application/zip')
    exportDialogStatus.value = 'success'
    setTimeout(() => {
      exportDialogVisible.value = false
      exitSaveMode()
    }, 2000)
  } catch (e) {
    console.error('PDF bulk download failed:', e)
    exportDialogVisible.value = false
    showToast('PDF 下载失败', 'error')
  }
}

function onScheduledTaskResult(event: CustomEvent) {
  const convId = event.detail?.conversation_id
  if (convId && convId === chatStore.currentConversationId) {
    chatStore.refreshConversation(convId)
  }
}

function toggleMessageSelect(id: string) {
  const next = new Set(selectedMessageIds.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  selectedMessageIds.value = next
}

function toggleSelectAll() {
  if (selectedMessageIds.value.size === chatStore.currentMessages.length) {
    selectedMessageIds.value = new Set()
  } else {
    selectedMessageIds.value = new Set(chatStore.currentMessages.map(m => m.id))
  }
}

function openNotebookPicker() {
  notesStore.loadNotebooks()
  showPicker.value = true
}

function extractPublishDateFromUrl(url: string): string {
  // Try to extract a date from URL path like /2026/04/12/ or /2026-04-12/
  const m = url.match(/\/(20[12]\d)[\/\-](0[1-9]|1[0-2])[\/\-]?(0[1-9]|[12]\d|3[01])?/)
  if (m) {
    const year = m[1]
    const month = m[2]
    const day = m[3]
    if (day) return `${year}年${parseInt(month)}月${parseInt(day)}日`
    return `${year}年${parseInt(month)}月`
  }
  // Try year-only pattern like /2026/
  const ym = url.match(/\/(20[12]\d)\//)
  if (ym) return `${ym[1]}年`
  return ''
}

function buildCitationsSection(toolResultsJson: string, messageContent: string): string {
  try {
    const data = JSON.parse(toolResultsJson)
    const results = Array.isArray(data) ? data : (data.results ?? [])
    if (!results.length) return ''

    // Find which citation numbers [N] are actually used in the message
    const usedIndices = new Set<number>()
    const citationRe = /\[(\d{1,2})\]/g
    let match: RegExpExecArray | null
    while ((match = citationRe.exec(messageContent)) !== null) {
      usedIndices.add(parseInt(match[1], 10))
    }
    if (usedIndices.size === 0) {
      results.forEach((_: any, i: number) => usedIndices.add(i + 1))
    }

    // Sequential renumber: map original indices to 1,2,3... in sorted order
    const sortedIndices = Array.from(usedIndices).sort((a, b) => a - b)
    const indexMap = new Map<number, number>()
    sortedIndices.forEach((oldIdx, i) => indexMap.set(oldIdx, i + 1))

    const lines: string[] = []
    for (const oldIdx of sortedIndices) {
      const r = results[oldIdx - 1]
      if (!r) continue
      let domain = ''
      try { domain = new URL(r.url).hostname.replace(/^www\./, '') } catch { domain = r.url }
      const pubDate = extractPublishDateFromUrl(r.url)
      const dateStr = pubDate ? ` (${pubDate})` : ''
      const newIdx = indexMap.get(oldIdx)!
      lines.push(`[${newIdx}] "${r.title}." *${domain}.* ${r.url}${dateStr}.`)
    }
    if (!lines.length) return ''
    return `\n\n---\n\n**参考来源**\n\n${lines.join('\n\n')}`
  } catch {
    return ''
  }
}

function stripExistingReferenceSection(content: string): string {
  const headerRe = /(?:\n\n?---[^\S\n]*\n+)?(?:^|\n)[^\S\n]*(?:#{1,6}[^\S\n]*|\*{1,2}[^\S\n]*)?(?:参考文献|参考资料|References|Sources|Reference)[^\S\n]*(?:\*{1,2})?[^\S\n]*\n[\s\S]*$/i
  return content.replace(headerRe, '').trimEnd()
}

function renumberInlineCitations(content: string, indexMap: Map<number, number>): string {
  if (indexMap.size === 0) return content
  const parts = content.split(/(```[\s\S]*?```|`[^`\n]+`)/g)
  return parts.map((part, i) => {
    if (i % 2 === 1) return part
    return part.replace(/\[(\d{1,2})\]/g, (_match, num) => {
      const oldNum = parseInt(num, 10)
      const newNum = indexMap.get(oldNum)
      return newNum !== undefined ? `[${newNum}]` : `[${oldNum}]`
    })
  }).join('')
}

function computeCitationIndexMap(messageContent: string, toolResultsJson: string): Map<number, number> {
  try {
    const data = JSON.parse(toolResultsJson)
    const results = Array.isArray(data) ? data : (data.results ?? [])
    if (!results.length) return new Map()

    const usedIndices = new Set<number>()
    const citationRe = /\[(\d{1,2})\]/g
    let match: RegExpExecArray | null
    while ((match = citationRe.exec(messageContent)) !== null) {
      usedIndices.add(parseInt(match[1], 10))
    }
    if (usedIndices.size === 0) {
      results.forEach((_: any, i: number) => usedIndices.add(i + 1))
    }

    const sortedIndices = Array.from(usedIndices).sort((a, b) => a - b)
    const indexMap = new Map<number, number>()
    sortedIndices.forEach((oldIdx, i) => indexMap.set(oldIdx, i + 1))
    return indexMap
  } catch {
    return new Map()
  }
}

async function handleSaveToNotebook(notebookId: string) {
  showPicker.value = false
  const selected = chatStore.currentMessages.filter(m => selectedMessageIds.value.has(m.id))
  if (selected.length === 0) return
  const content = selected.map(m => {
    const role = m.role === 'user' ? '**用户**' : '**助手**'
    let text = m.content
    if (m.role === 'assistant' && m.tool_results) {
      text = stripExistingReferenceSection(text)
      const indexMap = computeCitationIndexMap(m.content, m.tool_results)
      if (indexMap.size > 0) {
        text = renumberInlineCitations(text, indexMap)
      }
      text += buildCitationsSection(m.tool_results, m.content)
      try {
        const tr = JSON.parse(m.tool_results)
        const attachments = (tr?.attachments ?? []) as Array<{ name?: string; path?: string; type?: string }>
        const imageAttachments = attachments.filter((a: any) => a.type === 'image' && a.path)
        if (imageAttachments.length > 0) {
          text += '\n\n' + imageAttachments.map((a: any) => `![${a.name || 'image'}](${a.path})`).join('\n')
        }
      } catch { /* ignore parse errors */ }
    }
    return `${role}\n\n${text}`
  }).join('\n\n---\n\n')
  const titleText = selected[0].content.replace(/[#*`\n]/g, '').trim().slice(0, 30)
  const title = `对话记录: ${titleText || '对话片段'}`
  try {
    await notesStore.createNote(notebookId, { title, content })
    showToast('已添加到笔记', 'success')
    exitSaveMode()
  } catch (e) {
    console.error('Failed to save to notebook:', e)
    showToast('添加到笔记失败', 'error')
  }
}

async function handleRegenerate() {
  userHasScrolledUp.value = false
  await chatStore.regenerateLastAssistantMessage()
}

function onUserMessageSent() {
  // Local user send: always jump to the live edge.
  userHasScrolledUp.value = false
  nextTick().then(() => scrollToBottom())
}

function handleEdit(messageId: string, content: string) {
  // Strip note-ref and file-ref markers from the content for editing
  const strippedContent = stripDsmlTags(content)
    .replace(/\[note-ref:[^\]]*\]\n[\s\S]*?\n\[\/note-ref\]\n*/g, '')
    .replace(/\[file-ref:[^\]]*\]\n[\s\S]*?\n\[\/file-ref\]\n*/g, '')
    .trim()
  chatInputRef.value?.setEditContent(strippedContent, messageId)
}

const userHasScrolledUp = ref(false)
let _lastScrollTop = 0

function _cancelPendingAutoScroll() {
  if (scrollTimer !== null) {
    clearTimeout(scrollTimer)
    scrollTimer = null
  }
}

function onMessageListWheel(e: WheelEvent) {
  // Latch user intent SYNCHRONOUSLY on input, not on the scroll event:
  // scroll events are async and get coalesced with the streaming
  // scrollToBottom calls (100ms throttle), so a scroll-event-based latch
  // almost always lost the race during fast streaming — the user scrolled
  // up and got re-pinned within the same frame (scroll-yank regression,
  // conv 3bc79c4c 2026-07-21).
  if (e.deltaY < 0) {
    userHasScrolledUp.value = true
    _cancelPendingAutoScroll()
  }
}

let _touchLastY: number | null = null
function onMessageListTouchStart(e: TouchEvent) {
  _touchLastY = e.touches[0]?.clientY ?? null
}
function onMessageListTouchMove(e: TouchEvent) {
  if (_touchLastY === null) return
  const y = e.touches[0]?.clientY ?? _touchLastY
  // Finger moves down => content scrolls up.
  if (y - _touchLastY > 8) {
    userHasScrolledUp.value = true
    _cancelPendingAutoScroll()
  }
  _touchLastY = y
}

function onMessageListScroll() {
  // Backstop for scrollbar drags / keyboard scrolling (no wheel/touch
  // events). Programmatic auto-scrolls only ever move DOWN towards the
  // live edge, so upward movement with meaningful distance from the
  // bottom is user intent.
  const el = messageListRef.value
  if (!el) return
  const top = el.scrollTop
  const distanceFromBottom = el.scrollHeight - top - el.clientHeight
  if (top < _lastScrollTop - 1 && distanceFromBottom > 50) {
    userHasScrolledUp.value = true
  } else if (distanceFromBottom <= 50) {
    userHasScrolledUp.value = false
  }
  _lastScrollTop = top
}

function scrollToBottom() {
  // Pin the raw message-list bottom in EVERY mode. In virtual mode
  // scrollToIndex(last, {align:'end'}) scrolls via TanStack's scroll state,
  // whose rAF reconciliation then keeps re-applying the target for up to 5s
  // — fighting any wheel input that arrives right after the pin (the user
  // gets yanked back toward the pin target mid-scroll). A raw scrollTop
  // write leaves no scroll state behind: the virtualizer follows via its own
  // scroll observer, and the user's next wheel input wins immediately.
  const el = messageListRef.value
  if (!el) return
  el.scrollTop = el.scrollHeight
}

function scrollToMessageById(messageId: string) {
  const idx = chatStore.currentMessages.findIndex(m => m.id === messageId)
  if (idx === -1) return
  if (useVirtualMessageList.value) {
    messageVirtualizer.value.scrollToIndex(idx, { align: 'center', behavior: 'auto' })
  } else {
    const el = messageListRef.value?.querySelector(`[data-message-id="${messageId}"]`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}

function onDocumentClick(e: MouseEvent) {
  if (!showDownloadPopup.value) return
  const target = e.target as HTMLElement
  if (saveBarRightRef.value?.contains(target)) return
  showDownloadPopup.value = false
}

onMounted(() => {
  messageListRef.value?.addEventListener('scroll', onMessageListScroll)
  messageListRef.value?.addEventListener('wheel', onMessageListWheel, { passive: true })
  messageListRef.value?.addEventListener('touchstart', onMessageListTouchStart, { passive: true })
  messageListRef.value?.addEventListener('touchmove', onMessageListTouchMove, { passive: true })
  window.addEventListener('scheduled-task-result', onScheduledTaskResult as EventListener)
  document.addEventListener('click', onDocumentClick)
  // Ensure the message list is scrolled to the bottom when the component
  // mounts with an already-loaded conversation. The watch on
  // currentMessages.length fires immediately, but the DOM may not be fully
  // rendered yet (especially after a deep-link ?conv= load), so retry once.
  nextTick().then(() => {
    setTimeout(() => scrollToBottom(), 300)
  })
})

onBeforeUnmount(() => {
  messageListRef.value?.removeEventListener('scroll', onMessageListScroll)
  messageListRef.value?.removeEventListener('wheel', onMessageListWheel)
  messageListRef.value?.removeEventListener('touchstart', onMessageListTouchStart)
  messageListRef.value?.removeEventListener('touchmove', onMessageListTouchMove)
  window.removeEventListener('scheduled-task-result', onScheduledTaskResult as EventListener)
  document.removeEventListener('click', onDocumentClick)
  for (const key in stepTimers) {
    clearInterval(stepTimers[key])
  }
  if (scrollTimer !== null) {
    clearTimeout(scrollTimer)
    scrollTimer = null
  }
})

watch(
  () => chatStore.currentMessages.length,
  async () => {
    // New message added — scroll to bottom ONLY if the user hasn't
    // deliberately scrolled up. Resetting the flag here (old behavior)
    // defeated the streaming scroll gate: any mid-run message append
    // (per-turn persistence, polling merges, tool messages) yanked the
    // user back to the bottom while they were reading history.
    if (userHasScrolledUp.value) return
    await nextTick()
    scrollToBottom()
  },
  { immediate: true }
)

// Throttle scroll-to-bottom during streaming to avoid layout thrashing
let scrollTimer: ReturnType<typeof setTimeout> | null = null
const SCROLL_THROTTLE_MS = 100

watch(
  () => chatStore.currentStreamingContent,
  async () => {
    if (userHasScrolledUp.value) return
    if (scrollTimer !== null) return
    scrollTimer = setTimeout(async () => {
      scrollTimer = null
      await nextTick()
      scrollToBottom()
    }, SCROLL_THROTTLE_MS)
  }
)

// The thinking stage only streams part_delta (reasoning) events — the content
// watch above never fires, so a bottom-pinned user was left behind as the
// thinking block grew: the newest thinking stayed below the fold and the page
// appeared unable to scroll down. Follow the display sequence too (same
// throttle) so the live edge is pinned during the reasoning output stage.
watch(
  () => chatStore.currentStreamingDisplaySequence,
  async () => {
    if (userHasScrolledUp.value) return
    if (scrollTimer !== null) return
    scrollTimer = setTimeout(async () => {
      scrollTimer = null
      await nextTick()
      scrollToBottom()
    }, SCROLL_THROTTLE_MS)
  },
  { deep: true }
)

// Highlight search keywords in messages when navigating from search results.
// Triggered by a nonce so repeated searches with the same query still fire,
// and reads the target message id to scroll to the EXACT matched message.
watch(
  () => chatStore.searchHighlightNonce,
  async () => {
    const query = chatStore.searchHighlightQuery
    if (!query || !query.trim()) return
    const targetMessageId = chatStore.searchHighlightMessageId
    // Wait for messages to load and render from API
    let retries = 0
    let container: HTMLElement | null = null
    while (retries < 30) {
      await nextTick()
      container = messageListRef.value
      if (container) {
        // Check if message bubbles exist (not just empty state)
        const bubbles = container.querySelectorAll('.message-bubble')
        if (bubbles.length > 0) break
        // Also break if we see the empty state (no messages for this conversation)
        const emptyState = container.querySelector('.empty-state')
        if (emptyState) break
      }
      await new Promise(r => setTimeout(r, 200))
      retries++
    }
    if (!container) return
    // If a specific message was requested, wait until that message is rendered
    if (targetMessageId) {
      let msgRetries = 0
      while (msgRetries < 30) {
        if (container.querySelector(`[data-message-id="${targetMessageId}"]`)) break
        await new Promise(r => setTimeout(r, 200))
        msgRetries++
      }
    }
    const regex = new RegExp(`(${query.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi')
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null)
    const textNodes: Text[] = []
    let node
    while ((node = walker.nextNode())) {
      if (node.parentElement?.closest('script, style, code, pre, .search-highlight')) continue
      if (regex.test(node.textContent || '')) {
        textNodes.push(node as Text)
      }
    }
    for (const textNode of textNodes) {
      const parent = textNode.parentNode
      if (!parent) continue
      const fragment = document.createDocumentFragment()
      let text = textNode.textContent || ''
      let lastIdx = 0
      let match
      while ((match = regex.exec(text)) !== null) {
        fragment.appendChild(document.createTextNode(text.slice(lastIdx, match.index)))
        const mark = document.createElement('mark')
        mark.className = 'search-highlight'
        mark.textContent = match[0]
        fragment.appendChild(mark)
        lastIdx = match.index + match[0].length
      }
      if (lastIdx < text.length) {
        fragment.appendChild(document.createTextNode(text.slice(lastIdx)))
      }
      if (fragment.childNodes.length > 0) {
        parent.replaceChild(fragment, textNode)
      }
    }
    // Scroll to the exact matched message (if specified), otherwise the first highlight
    await nextTick()
    let scrollTarget: Element | null = null
    if (targetMessageId) {
      const msgEl = container.querySelector(`[data-message-id="${targetMessageId}"]`)
      if (msgEl) {
        scrollTarget = msgEl.querySelector('.search-highlight') || msgEl
      }
    }
    if (!scrollTarget) {
      scrollTarget = container.querySelector('.search-highlight')
    }
    if (scrollTarget) {
      scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }
)

watch(
  () => chatStore.isStreamingCurrentConversation,
  async (streaming, wasStreaming) => {
    if (!wasStreaming || streaming) return
    // Stream finished. Do NOT reset userHasScrolledUp here — the scroll
    // handler owns that flag; resetting it yanked a scrolled-up user to the
    // bottom at completion. When the user IS at the live edge, re-pin once
    // so the persisted message swaps into the streaming bubble's place
    // (virtual lists may leave the new item just outside the mounted
    // window, making the finished answer look like it vanished).
    if (userHasScrolledUp.value) return
    await nextTick()
    scrollToBottom()
  }
)

watch(
  () => chatStore.currentConversationId,
  async () => {
    // Switched conversation — reset and scroll to bottom
    userHasScrolledUp.value = false
    await nextTick()
    scrollToBottom()
  }
)
</script>

<style scoped>
.chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  background-color: var(--surface-workbench);
  position: relative;
}

.chat-header {
  height: var(--header-height);
  padding: 0 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--panel-border);
  background-color: var(--surface-panel-strong);
}

.chat-title {
  font-size: 16px;
  font-weight: 500;
  color: var(--color-text);
  text-wrap: balance;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.context-token-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-left: 12px;
  padding: 3px 10px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--color-text-light);
  background-color: var(--color-hover);
  border: 1px solid var(--panel-border);
  border-radius: 999px;
  white-space: nowrap;
  flex-shrink: 0;
  cursor: default;
}

.context-token-chip svg {
  opacity: 0.7;
}

.message-list {
  flex: 1;
  overflow-y: auto;
  overflow-anchor: none;
  padding: 16px 56px 16px 0;
}

.empty-state {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
}

.empty-icon {
  opacity: 1;
}

.empty-icon svg {
  width: 48px;
  height: 48px;
}

@media (min-width: 480px) {
  .empty-icon svg {
    width: 64px;
    height: 64px;
  }
}

.empty-logo {
  width: 64px;
  height: 64px;
  border-radius: 12px;
  opacity: 1;
}

.empty-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  text-align: center;
}

.empty-brand-title {
  margin: 0;
  font-family: var(--font-main);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--color-text);
}

.empty-brand-subtitle {
  margin: 0;
  font-family: var(--font-main);
  font-size: 13px;
  color: var(--color-text-light);
}

@media (min-width: 480px) {
  .empty-logo {
    width: 80px;
    height: 80px;
  }

  .empty-brand-title {
    font-size: 26px;
  }

  .empty-brand-subtitle {
    font-size: 14px;
  }
}

.empty-text {
  color: var(--color-text-light);
  font-size: 14px;
}

.empty-lead {
  margin: 4px 0 0;
  font-size: 13.5px;
  color: var(--color-text-light);
  line-height: 1.7;
}

.empty-try {
  margin-top: 22px;
  font-size: 12px;
  font-weight: 700;
  color: var(--color-primary-dark);
  letter-spacing: 2px;
}

.empty-suggest-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  width: 100%;
  max-width: 660px;
  margin-top: 12px;
}

.empty-suggest-card {
  position: relative;
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-rows: auto auto;
  column-gap: 11px;
  row-gap: 3px;
  align-items: start;
  padding: 14px 16px;
  text-align: left;
  font-family: inherit;
  cursor: pointer;
  border-radius: 18px;
  border: 1px solid #dfe7d8;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(45, 59, 36, 0.06);
  transition: transform 0.2s ease, border-color 0.2s ease, background 0.2s ease;
}

.empty-suggest-card:hover {
  transform: translateY(-3px);
  border-color: #cfdbc4;
  background: #f8faf6;
}

.empty-suggest-card .es-ico {
  grid-row: span 2;
  width: 19px;
  height: 19px;
  color: #5d7c44;
}

.empty-suggest-card .es-cat {
  font-size: 13.5px;
  font-weight: 700;
  color: var(--color-text);
}

.empty-suggest-card .es-q {
  grid-column: 2;
  font-size: 12px;
  color: var(--color-text-light);
  line-height: 1.6;
}

.empty-suggest-card .es-no {
  display: none;
}

.streaming-message {
  display: flex;
  gap: 12px;
  padding: 12px 20px;
  /* Extra top spacing so the in-progress tool-call/agent bubble doesn't
     visually crowd the previous assistant message. */
  margin-top: 16px;
}

.streaming-message .avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.streaming-message .content {
  max-width: 70%;
}

@media (max-width: 480px) {
  .streaming-message .content {
    max-width: 85%;
  }
}

.reasoning-block {
  margin-bottom: 8px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-secondary);
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
}

.reasoning-summary {
  cursor: pointer;
  font-weight: 500;
  font-size: 12px;
  color: var(--color-text-light);
  user-select: none;
}

.reasoning-text {
  margin-top: 6px;
  padding-left: 8px;
}

.reasoning-text :deep(ol),
.reasoning-text :deep(ul) {
  padding-left: 1.5em;
}

.reasoning-text :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.reasoning-text :deep(ol > li) {
  counter-increment: ol-counter;
}

.reasoning-text :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  margin-right: 2px;
}

.streaming-message .text {
  padding: 12px 16px;
  background-color: var(--surface-panel-strong);
  border-radius: var(--radius-lg);
  border-bottom-left-radius: 4px;
  box-shadow: var(--panel-shadow);
  line-height: 1.6;
}

.streaming-message .segment-text {
  margin-bottom: 8px;
}

.cursor {
  display: inline-block;
  width: 2px;
  height: 14px;
  background-color: var(--color-primary);
  margin-left: 2px;
  animation: blink 1s infinite;
  vertical-align: middle;
}

@keyframes blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0; }
}

/* Pre-first-token waiting state (right after the user sends a query).
   Styled like the reasoning/thinking blocks so the pre-thinking hint and the
   streamed thinking content read as one continuous visual element. */
.waiting-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-secondary);
}

.waiting-indicator .cursor {
  margin-left: 0;
}

.waiting-indicator-text {
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
}

/* Loading spinner for agent processing state */
.processing-spinner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  margin-bottom: 10px;
  border-radius: var(--radius-md);
  background: var(--surface-panel);
  border: 1px solid var(--panel-border);
  animation: fadeIn 0.3s ease-out;
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.06);
}

.spinner-circle {
  width: 20px;
  height: 20px;
  border: 3px solid var(--color-primary);
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  flex-shrink: 0;
}

.spinner-label {
  font-size: 13px;
  color: var(--color-text);
  font-weight: 500;
  animation: pulse-text 2s ease-in-out infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@keyframes pulse-text {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

/* Pulse animation for streaming message when processing */
.streaming-message.processing {
  animation: processing-pulse 2s ease-in-out infinite;
}

@keyframes processing-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.85; }
}

.error-message {
  padding: 12px 20px;
  color: var(--color-error);
  font-size: 13px;
}

.deathmatch-status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 16px;
  background: var(--danger-tint);
  border-bottom: 2px solid var(--color-danger);
  font-size: 13px;
  flex-wrap: wrap;
}

.deathmatch-status-bar.dm-grilling {
  background: var(--info-tint);
  border-bottom-color: var(--color-info);
}

.deathmatch-status-bar.dm-active {
  background: var(--danger-tint);
  border-bottom-color: var(--color-danger);
}

.deathmatch-status-bar.dm-paused {
  background: var(--warning-tint);
  border-bottom-color: var(--color-warning);
}

.deathmatch-status-bar.dm-partial {
  background: var(--info-tint);
  border-bottom-color: var(--color-info);
}

.deathmatch-status-bar.dm-done {
  background: var(--success-tint);
  border-bottom-color: var(--color-success);
}

.deathmatch-status-bar.dm-human-gate {
  background: var(--warning-tint);
  border-bottom-color: var(--color-warning);
}

.dm-human-gate .dm-status-text { color: var(--color-warning); }
.dm-partial .dm-status-text { color: var(--color-info); }
.dm-partial .dm-status-icon { color: var(--color-info); }

.dm-status-icon {
  color: var(--color-danger, #dc2626);
  display: flex;
  align-items: center;
}

.dm-status-text {
  font-weight: 600;
  color: var(--color-danger);
}

.dm-grilling .dm-status-text { color: var(--color-info); }
.dm-paused .dm-status-text { color: var(--color-warning); }
.dm-done .dm-status-text { color: var(--color-success); }

.dm-status-message {
  font-size: 12px;
  color: var(--color-text-light);
  flex-basis: 100%;
}

.dm-continue-indicator {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--color-danger);
  margin-left: auto;
}

.dm-pulse {
  width: 8px;
  height: 8px;
  background: var(--color-danger);
  border-radius: 50%;
  animation: dm-pulse-anim 1.5s ease-in-out infinite;
}

@keyframes dm-pulse-anim {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.dm-plan-progress {
  color: var(--color-info);
  font-weight: 600;
}

.dm-plan-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 6px;
  padding: 8px 10px;
  background: var(--info-tint);
  border-radius: 6px;
  max-height: 120px;
  overflow: auto;
}

.dm-plan-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-light, #666);
}

.dm-plan-step.done {
  color: var(--color-success);
  text-decoration: line-through;
  text-decoration-color: color-mix(in srgb, var(--color-success) 40%, transparent);
}

.dm-step-mark {
  font-weight: 700;
  width: 14px;
  text-align: center;
}

.dm-step-id {
  font-family: 'SF Mono', 'Menlo', monospace;
  font-size: 11px;
  color: var(--color-info);
  min-width: 24px;
}

.dm-step-desc {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dm-human-gate-report {
  margin-top: 6px;
  padding: 8px 10px;
  background: var(--warning-tint);
  border-left: 3px solid var(--color-warning);
  border-radius: 4px;
  max-height: 140px;
  overflow: auto;
}

.dm-human-gate-report pre {
  margin: 0;
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-warning);
  font-family: 'SF Mono', 'Menlo', monospace;
}

.grilling-bubble {
  display: flex;
  gap: 12px;
  padding: 12px 20px;
  margin-left: 16px;
  animation: fadeIn 0.3s ease-out;
}

.grilling-bubble-avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.grilling-bubble-content {
  max-width: 70%;
  background: var(--info-tint);
  border-radius: var(--radius-lg);
  border-bottom-left-radius: 4px;
  padding: 12px 16px;
  border: 1px solid color-mix(in srgb, var(--color-info) 32%, transparent);
  box-shadow: var(--panel-shadow);
}

.grilling-bubble-answered .grilling-bubble-content {
  background: var(--success-tint);
  border-color: color-mix(in srgb, var(--color-success) 40%, transparent);
}

@media (max-width: 480px) {
  .grilling-bubble-content {
    max-width: 85%;
  }
}

.grilling-q-header {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 6px;
}

.grilling-q-number {
  background: var(--color-info);
  color: #fff;
  border-radius: 50%;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}

.grilling-bubble-answered .grilling-q-number {
  background: var(--color-success);
}

.grilling-q-text {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
  flex: 1;
  line-height: 1.5;
}

.grilling-q-status {
  font-size: 11px;
  color: var(--color-success);
  font-weight: 600;
  flex-shrink: 0;
}

.grilling-q-recommendation {
  font-size: 12px;
  color: var(--color-text-light);
  margin-left: 30px;
  margin-bottom: 8px;
  line-height: 1.4;
}

.grilling-q-answered-text {
  font-size: 13px;
  color: var(--color-success);
  margin-left: 30px;
  padding: 6px 10px;
  background: var(--success-tint);
  border-radius: 6px;
  line-height: 1.4;
}

.grilling-q-answer {
  margin-left: 30px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.grilling-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.grilling-option-btn {
  padding: 5px 14px;
  border: 1px solid color-mix(in srgb, var(--color-info) 45%, transparent);
  border-radius: var(--radius-pill);
  background: var(--surface-panel-strong);
  color: var(--color-info);
  font-size: 13px;
  cursor: pointer;
  transition: background-color 0.15s, border-color 0.15s, color 0.15s, transform 0.15s;
  white-space: nowrap;
}

.grilling-option-btn:active {
  transform: scale(0.96);
}

.grilling-option-btn:hover {
  background: var(--info-tint);
  border-color: var(--color-info);
}

.grilling-option-btn.grilling-option-selected {
  background: var(--color-info);
  color: #fff;
  border-color: var(--color-info);
}

.grilling-option-btn.grilling-option-other {
  border-style: dashed;
  color: var(--color-text-light);
}

.grilling-option-btn.grilling-option-other.grilling-option-selected {
  background: var(--color-text-light);
  color: #fff;
  border-color: var(--color-text-light);
  border-style: solid;
}

.grilling-answer-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--panel-border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
  resize: vertical;
  min-height: 40px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.2s;
}

.grilling-answer-input:focus {
  border-color: var(--color-info);
  box-shadow: 0 0 0 2px var(--info-tint);
}

.grilling-submit-btn {
  align-self: flex-end;
  background: var(--color-info);
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.2s;
}

.grilling-submit-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-info) 85%, var(--color-text));
}

.grilling-submit-btn:disabled {
  background: var(--color-text-light);
  cursor: not-allowed;
}

.save-selection-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background-color: var(--surface-panel-strong);
  border-bottom: 1px solid var(--panel-border);
  gap: 12px;
}

.save-bar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.select-all-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
}

.select-all-label input {
  cursor: pointer;
}

.selected-count {
  font-size: 12px;
  color: var(--color-text-light);
}

.save-bar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  position: relative;
}

.save-confirm-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  background-color: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast), opacity var(--transition-fast);
}

.save-confirm-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.save-download-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  background-color: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast), opacity var(--transition-fast);
  position: relative;
}

.save-download-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.save-download-btn:hover:not(:disabled) {
  background-color: var(--color-primary-dark);
}

.save-download-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.download-popup {
  position: absolute;
  top: 100%;
  right: 80px;
  margin-top: 4px;
  background: var(--color-white);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 100;
  padding: 4px;
  min-width: 240px;
}

.download-popup-btn {
  display: block;
  width: 100%;
  text-align: left;
  padding: 10px 14px;
  font-size: 13px;
  color: var(--color-text);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast);
}

.download-popup-btn:hover {
  background-color: var(--color-hover);
}

.save-confirm-btn:hover:not(:disabled) {
  background-color: var(--color-primary-dark);
}

.save-confirm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.save-cancel-btn {
  padding: 6px 14px;
  background-color: var(--color-hover);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.save-cancel-btn:active {
  transform: scale(0.96);
}

.save-cancel-btn:hover {
  background-color: color-mix(in srgb, var(--color-text) 12%, var(--color-hover));
}

.header-save-note-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  flex-shrink: 0;
}

.header-save-note-btn:hover {
  color: var(--color-primary);
  background-color: var(--color-hover);
}

.header-save-note-btn:active {
  transform: scale(0.96);
}

.hide-on-desktop {
  display: none;
}

.grilling-round-header {
  text-align: center;
  font-size: 13px;
  color: var(--color-text-light);
  margin: 12px 20px 8px;
}

.grilling-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 24px;
  margin: 8px 20px 24px;
  border-radius: var(--radius-lg);
  background: var(--info-tint);
  border: 1px solid color-mix(in srgb, var(--color-info) 32%, transparent);
  animation: fadeIn 0.3s ease-out;
}

@media (max-width: 767px) {
  .chat-header {
    display: none;
  }

  .hide-on-desktop {
    display: flex;
  }

  .message-list {
    padding: 8px 0;
  }

  .streaming-message {
    padding: 8px 12px;
  }

.grilling-answer-submit-btn {
  margin-top: 8px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.grilling-btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.4);
  border-top-color: #ffffff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.grilling-loading {
  padding: 16px;
  margin: 8px 12px 24px;
}

.grilling-answer-input:disabled,
.grilling-option-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.save-selection-bar {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
    padding: 10px 12px;
  }

  .save-bar-left {
    justify-content: space-between;
  }

  .save-bar-right {
    justify-content: flex-end;
  }

.grilling-round-header {
  margin: 12px 12px 8px;
}

  .grilling-bubble {
    padding: 8px 12px;
    margin-left: 8px;
  }

  .grilling-options {
    gap: 4px;
  }

  .grilling-option-btn {
    padding: 3px 8px;
    font-size: 11px;
  }
}

/* Streaming agent step blocks (thinking-like) */
.agent-step-block {
  margin-bottom: 8px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-secondary);
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
  animation: fadeIn 0.3s ease-out;
}

.agent-step-summary {
  cursor: pointer;
  font-weight: 500;
  font-size: 12px;
  color: var(--color-text-light);
  user-select: none;
}

.agent-step-text {
  margin-top: 6px;
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
}

/* Tool loop activity (use_tool_loop = true) */
.tool-loop-activity {
  margin-bottom: 8px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-warning, #e6a23c);
  font-size: 13px;
  animation: fadeIn 0.3s ease-out;
}

.iteration-badge {
  margin-left: 8px;
  padding: 1px 8px;
  background-color: var(--color-primary);
  color: #fff;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

.tool-loop-list {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tool-loop-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 12px;
  transition: background-color var(--transition-fast);
}

.tool-loop-item--complete {
  background-color: rgba(0, 180, 100, 0.08);
}

.tool-loop-name {
  font-weight: 500;
  color: var(--color-text);
}

.tool-loop-status {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
}

.tool-loop-status--running {
  color: var(--color-warning, #e6a23c);
  background-color: rgba(230, 162, 60, 0.1);
  animation: pulse 1.5s infinite;
}

.tool-loop-status--done {
  color: var(--color-success, #67c23a);
  background-color: rgba(103, 194, 58, 0.1);
}

.tool-loop-status--error {
  color: var(--color-error, #f56c6c);
  background-color: rgba(245, 108, 108, 0.1);
}

/* F0: single-timeline item styles */

.iteration-context {
  margin-bottom: 6px;
}

/* Reasoning in timeline: unified style with reasoning-block */
.timeline-reasoning {
  margin-bottom: 8px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-secondary);
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
}

.timeline-reasoning :deep(ol),
.timeline-reasoning :deep(ul) {
  padding-left: 1.5em;
}

/* Text segment in timeline */
.timeline-text {
  margin-bottom: 6px;
  padding: 12px 16px;
  background-color: var(--surface-panel-strong);
  border-radius: var(--radius-lg);
  border-bottom-left-radius: 4px;
  box-shadow: var(--panel-shadow);
  line-height: 1.6;
}

/* Tool call card: status-driven, inline in timeline */
.timeline-tool-call {
  margin-bottom: 6px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-warning, #e6a23c);
  font-size: 13px;
  transition: border-color 0.3s;
}

.timeline-tool-call.tool-error {
  border-left-color: var(--color-error, #f56c6c);
}

.tool-card-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.tool-card-icon {
  font-size: 14px;
  flex-shrink: 0;
}

.tool-card-name {
  font-weight: 500;
  color: var(--color-text);
}

.tool-card-status {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 500;
}

.tool-card-status.running {
  color: var(--color-warning, #e6a23c);
  background-color: rgba(230, 162, 60, 0.1);
  animation: pulse 1.5s infinite;
}

.tool-card-status.done {
  color: var(--color-success, #67c23a);
  background-color: rgba(103, 194, 58, 0.1);
}

.tool-card-status.error {
  color: var(--color-error, #f56c6c);
  background-color: rgba(245, 108, 108, 0.1);
}

.tool-card-toggle {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-primary);
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
}

.tool-card-toggle:hover {
  background-color: rgba(122, 163, 90, 0.1);
}

.tool-card-result {
  margin-top: 8px;
  padding: 8px 10px;
  background: var(--surface-panel, #fff);
  border-radius: var(--radius-md);
  font-size: 13px;
  max-height: 400px;
  overflow-y: auto;
  line-height: 1.5;
}

/* Sub-agent chunk in timeline */
.timeline-subagent {
  margin-bottom: 8px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-secondary);
  font-size: 13px;
}

.timeline-subagent-summary {
  cursor: pointer;
  font-weight: 500;
  font-size: 12px;
  color: var(--color-text-light);
  user-select: none;
}

/* Context tool group sub-items */
.context-tool-subitem {
  padding: 6px 0;
  border-bottom: 1px solid var(--color-border-light, #eee);
}

.context-tool-subitem:last-child {
  border-bottom: none;
}

.context-tool-subname {
  font-weight: 500;
  color: var(--color-text);
  font-size: 12px;
  margin-right: 8px;
}

.tool-card-result-inner {
  margin-top: 4px;
  font-size: 13px;
  line-height: 1.5;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.search-highlight {
  background: var(--color-primary);
  color: #fff;
  padding: 0 2px;
  border-radius: 2px;
  animation: highlight-fade 3s ease-out forwards;
}

@keyframes highlight-fade {
  0% { background: #f59e0b; color: #fff; }
  50% { background: var(--color-primary); color: #fff; }
  100% { background: rgba(122, 163, 90, 0.2); color: inherit; }
}
</style>
