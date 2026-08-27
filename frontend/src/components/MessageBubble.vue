<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="message-bubble" :class="[message.role, { 'save-mode': saveMode }]" :data-message-id="message.id" @click="saveMode && $emit('toggle-select')">
    <input
      v-if="saveMode"
      type="checkbox"
      class="msg-checkbox"
      :checked="selected"
      @click.stop
      @change="$emit('toggle-select')"
    />
    <div class="avatar" v-if="message.role === 'assistant'">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="10" fill="var(--color-primary)"/>
        <path d="M8 10h8M8 14h5" stroke="white" stroke-width="2" stroke-linecap="round"/>
      </svg>
    </div>
    <div class="content" ref="contentRef">
      <div class="msg-timestamp">{{ formatTimestamp(message.created_at) }}</div>
      <div v-if="noteRefs.length > 0" class="msg-note-tags">
        <span
          v-for="(ref, idx) in noteRefs"
          :key="idx"
          class="msg-note-tag"
          @click.stop="previewIdx = previewIdx === idx ? null : idx"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
          {{ ref.title }}
        </span>
      </div>
      <div v-if="fileRefs.length > 0" class="msg-note-tags">
        <span
          v-for="(ref, idx) in fileRefs"
          :key="`file-${idx}`"
          class="msg-note-tag msg-file-tag"
          @click.stop="filePreviewIdx = filePreviewIdx === idx ? null : idx"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>
            <polyline points="13 2 13 9 20 9"/>
          </svg>
          {{ ref.filename }}
        </span>
      </div>
      <!-- Top reasoning block: only shown when there is NO reasoning_step in displaySequence -->
      <div v-if="message.reasoning_content && message.role === 'assistant' && !hasReasoningStep" class="reasoning-block">
        <details>
          <summary class="reasoning-summary">💭 思考过程</summary>
          <div class="reasoning-text" v-html="formattedReasoning"></div>
        </details>
      </div>
      <div v-if="message.role === 'assistant' && displaySequence.length > 0" class="alternating-content">
        <template v-for="(item, idx) in displaySequence" :key="idx">
          <div v-if="item.type === 'text'" class="text segment-text" v-html="formatTextWithCitations(item.content)"></div>
          <div v-else-if="item.type === 'reasoning_step'" class="reasoning-block">
            <details>
              <summary class="reasoning-summary">{{ item.title || '💭 思考过程' }}</summary>
              <div class="reasoning-text" v-html="renderMarkdownToHtml(item.content)"></div>
            </details>
          </div>
          <ToolPartCard v-else-if="isToolSequenceItem(item)" :item="toToolPartItem(item)" />
          <div v-else class="agent-step-block">
            <details>
              <summary class="agent-step-summary">⚙️ {{ item.title || item.name }}</summary>
              <div v-if="item.step_type === 'llm'" class="agent-step-markdown">
                <StreamMarkdown :content="item.content" />
              </div>
              <div v-else class="agent-step-text" v-html="formatAgentStepContent(item.content)"></div>
            </details>
          </div>
        </template>
      </div>
      <div v-else class="text" v-html="formattedContent"></div>

      <div v-if="message.role === 'assistant' && hasPersistedProcess && displaySequence.length === 0" class="persisted-process">
        <div v-if="parsedTaskPlan" class="agent-step-block agent-step-block--task">
          <details open>
            <summary class="agent-step-summary">📋 任务执行过程</summary>
            <TaskProgressComp :progress="parsedTaskPlan" />
          </details>
        </div>

        <div v-if="parsedThinkingHistory.length > 0" class="agent-step-block agent-step-block--history">
          <details>
            <summary class="agent-step-summary">🗂️ 阶段记录</summary>
            <div class="thinking-history-list">
              <div
                v-for="(thinking, idx) in parsedThinkingHistory"
                :key="`thinking-${idx}`"
                class="thinking-history-item"
              >
                <span class="thinking-history-name">{{ thinking.agent_name }}</span>
                <span class="thinking-history-phase" :class="thinking.status">{{ thinking.phase }}</span>
                <span class="thinking-history-text">{{ thinking.content }}</span>
              </div>
            </div>
          </details>
        </div>

        <template v-if="displaySequence.length === 0">
        <div
          v-for="(step, idx) in parsedAgentSteps"
          :key="`step-block-${idx}`"
          class="agent-step-block"
        >
          <details>
            <summary class="agent-step-summary">⚙️ {{ step.title }}</summary>
            <div v-if="isLlmStep(step)" class="agent-step-markdown">
              <StreamMarkdown :content="step.content" />
            </div>
            <div v-else class="agent-step-text" v-html="formatAgentStepContent(step.content)"></div>
          </details>
        </div>
        </template>

        <div
          v-for="output in parsedSubAgentOutputs"
          :key="`sub-agent-output-${output.id}`"
          class="agent-step-block"
        >
          <details>
            <summary class="agent-step-summary">🧠 子代理 · {{ output.name }}</summary>
            <div v-if="output.reasoning" class="reasoning-text sub-agent-reasoning">
              <StreamMarkdown :content="output.reasoning" />
            </div>
            <div v-if="output.content" class="agent-step-markdown">
              <StreamMarkdown :content="output.content" />
            </div>
          </details>
        </div>
      </div>

      <!-- Search result tags at answer end (only those actually cited in body) -->
      <div v-if="message.role === 'assistant' && displayedToolResults.length > 0" class="msg-ref-tags">
        <span
          v-for="{ result, num, bibText } in displayedToolResults"
          :key="'search-' + num"
          class="msg-ref-tag msg-ref-tag--search"
          :class="{ 'msg-ref-tag--active': searchPreviewIdx === num }"
          :title="`[${num}] ${bibText || result.title || extractDomain(result.url)}`"
          @click.stop="searchPreviewIdx = searchPreviewIdx === num ? null : num"
        >
          <span class="msg-ref-index">[{{ num }}]</span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          {{ result.url ? extractDomain(result.url) : (bibText || result.title || '').slice(0, 40) }}
        </span>
      </div>

      <!-- Search failure card: legacy SearchResults component. Rendered ONLY
           when the backend actually supplied unqualified fallback results
           (full failure with salvageable data). Partial failures carry real
           results and are covered by the msg-ref-tags above — showing this
           old-style card there duplicated the search UI (conv 8f27d43e:
           "老样式的已检索 20 个网页"). -->
      <SearchResults
        v-if="isSearchFailed && (parsedToolResultsData?.unqualified_results?.length ?? 0) > 0"
        :results="parsedToolResults"
        :rounds="parsedToolRounds"
        :searchFailed="isSearchFailed"
        :failedData="{
          rounds: parsedToolRounds,
          unqualified_results: parsedToolResultsData?.unqualified_results ?? [],
          failure_summary: parsedToolResultsData?.failure_summary ?? '',
        }"
        :onUseUnqualified="handleUseUnqualified"
      />

      <FileAttachmentComp
        v-if="parsedAttachments.length"
        :attachments="parsedAttachments"
      />

      <div v-if="message.role === 'assistant' && !saveMode" class="message-actions">
        <button class="message-action-btn icon-btn copy-btn" :title="copyTooltip" @click.stop="copyMessage">
          <svg class="copy-icon" :class="{ hidden: copied }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
          <svg class="check-icon" :class="{ hidden: !copied }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
        </button>
        <button v-if="canRegenerate" class="message-action-btn icon-btn" title="重新生成" @click.stop="$emit('regenerate')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
        </button>
      </div>
      <div v-if="message.role === 'user' && !saveMode" class="message-actions">
        <button class="message-action-btn icon-btn copy-btn" :title="copyTooltip" @click.stop="copyMessage">
          <svg class="copy-icon" :class="{ hidden: copied }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
          <svg class="check-icon" :class="{ hidden: !copied }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
        </button>
        <button class="message-action-btn icon-btn" title="修改" @click.stop="$emit('edit', message.id, message.content)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Note preview popup -->
    <Teleport to="body">
      <div v-if="previewIdx !== null && noteRefs[previewIdx]" class="note-preview-overlay" @click="previewIdx = null">
        <div class="note-preview-card" @click.stop>
          <div class="note-preview-header">
            <span class="note-preview-title">{{ noteRefs[previewIdx].title }}</span>
            <button class="note-preview-close" @click="previewIdx = null">×</button>
          </div>
          <div class="note-preview-body markdown-body" v-html="previewHtml"></div>
        </div>
      </div>
    </Teleport>

    <!-- File preview popup -->
    <Teleport to="body">
      <div v-if="filePreviewIdx !== null && fileRefs[filePreviewIdx]" class="note-preview-overlay" @click="filePreviewIdx = null">
        <div class="note-preview-card" @click.stop>
          <div class="note-preview-header">
            <span class="note-preview-title">{{ fileRefs[filePreviewIdx].filename }}</span>
            <button class="note-preview-close" @click="filePreviewIdx = null">×</button>
          </div>
          <div class="note-preview-body markdown-body" v-html="filePreviewHtml"></div>
        </div>
      </div>
    </Teleport>

    <!-- Search result preview popup -->
    <Teleport to="body">
      <div v-if="previewedResult" class="note-preview-overlay" @click="searchPreviewIdx = null">
        <div class="note-preview-card" @click.stop>
          <div class="note-preview-header">
            <span class="note-preview-title">{{ previewedResult.result.title || previewedResult.bibText || '未知来源' }}</span>
            <button class="note-preview-close" @click="searchPreviewIdx = null">×</button>
          </div>
          <div class="note-preview-body">
            <a v-if="previewedResult.result.url" class="search-preview-url" :href="previewedResult.result.url" target="_blank" rel="noopener noreferrer">
              {{ previewedResult.result.url }}
            </a>
            <p v-if="previewedResult.bibText" class="search-preview-bib">{{ previewedResult.bibText }}</p>
            <p v-if="previewedResult.result.snippet" class="search-preview-snippet">{{ previewedResult.result.snippet }}</p>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Mermaid code view dialog (read-only in chat) -->
    <Teleport to="body">
      <div v-if="showMermaidCodeDialog" class="mermaid-dialog-overlay" @click="closeMermaidCodeDialog">
        <div class="mermaid-dialog" @click.stop>
          <div class="mermaid-dialog-header">
            <h3>Mermaid 代码</h3>
            <button class="mermaid-dialog-close" @click="closeMermaidCodeDialog">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="mermaid-dialog-content">
            <textarea
              :value="mermaidCodeSource"
              class="mermaid-code-textarea"
              readonly
            ></textarea>
          </div>
          <div class="mermaid-dialog-footer">
            <button class="mermaid-dialog-cancel" @click="copyMermaidCode">复制代码</button>
            <button class="mermaid-dialog-save" @click="closeMermaidCodeDialog">关闭</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Mermaid zoom dialog (read-only in chat, no inline editing) -->
    <Teleport to="body">
      <div v-if="showMermaidZoomDialog" class="mermaid-zoom-overlay" @click="closeMermaidZoomDialog" @touchstart.passive="onMermaidPinchStart" @touchmove="onMermaidPinchMove" @touchend.passive="onMermaidPinchEnd">
        <div class="mermaid-zoom-card" @click.stop>
          <div class="mermaid-zoom-header">
            <h3>Mermaid 图表</h3>
            <div class="mermaid-zoom-controls">
              <label class="zoom-label">显示比例:</label>
              <input
                type="range"
                min="25"
                max="300"
                step="5"
                v-model.number="mermaidZoomScale"
                class="zoom-slider"
              />
              <span class="zoom-value">{{ mermaidZoomScale }}%</span>
              <button class="zoom-reset-btn" @click="mermaidZoomScale = 100" title="重置">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
                  <path d="M3 3v5h5"/>
                </svg>
              </button>
            </div>
            <button class="mermaid-zoom-close" @click="closeMermaidZoomDialog">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="mermaid-zoom-content" ref="mermaidZoomContentRef">
            <div class="mermaid-zoom-spacer" :style="mermaidZoomSpacerStyle">
              <div class="mermaid-zoom-svg" :style="mermaidZoomSvgStyle" ref="mermaidZoomSvgRef"></div>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Math formula view dialog (read-only in chat) -->
    <Teleport to="body">
      <div v-if="showMathViewDialog" class="math-dialog-overlay" @click="showMathViewDialog = false">
        <div class="math-dialog" @click.stop>
          <div class="math-dialog-header">
            <h3>数学公式</h3>
            <button class="math-dialog-close" @click="showMathViewDialog = false">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
          <div class="math-dialog-content">
            <div class="math-view-label">LaTeX 代码:</div>
            <textarea
              :value="mathViewTex"
              class="math-view-textarea"
              readonly
            ></textarea>
            <div class="math-view-label">预览:</div>
            <div class="math-preview" v-html="mathViewPreviewHtml"></div>
          </div>
          <div class="math-dialog-footer">
            <button class="math-dialog-cancel" @click="copyMathCode">复制代码</button>
            <button class="math-dialog-save" @click="showMathViewDialog = false">关闭</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Inline markdown image lightbox (click an embedded image to enlarge) -->
    <MediaLightbox
      :media="lightboxMedia"
      :kind="lightboxKind"
      :url="lightboxUrl"
      @close="closeLightbox"
      @download="onLightboxDownload"
    />

  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import type {
  Message, SearchResult, SearchRound, ToolResultsData, AgentStep,
  FileAttachment, TaskProgress as TaskPlan, SubAgentOutput, SubAgentThinking,
  DisplaySequenceItem,
} from '@/types'
import SearchResults from './SearchResults.vue'
import FileAttachmentComp from './FileAttachment.vue'
import StreamMarkdown from './StreamMarkdown.vue'
import TaskProgressComp from './TaskProgress.vue'
import ToolPartCard from './ToolPartCard.vue'
import MediaLightbox from './MediaLightbox.vue'
import { useInlineImageZoom } from '@/composables/useInlineImageZoom'
import { useChatStore } from '@/stores/chat'
import {
  renderMarkdownToHtml,
  addCitationSuperscripts,
  renumberCitationSuperscripts,
  renderMermaidBlocks,
  fixLostMermaidBlocks,
  renderEchartsBlocks,
  fixLostEchartsBlocks,
  attachMathEditListeners,
  katexModule,
  stripDsmlTags,
} from '@/composables/useMarkdown'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

// Pre-compile bibliography regexes to avoid re-creating them on every computed
// re-evaluation during streaming.
const BIB_HEADER_RE = /(?:^|\n)\s*(?:#{1,6}\s|\*{1,2})?(?:参考文献|参考资料|参考来源|References|Sources)\s*[：:]?\s*(?:\*{1,2})?\s*/i
const BIB_LINE_RE = /(?:^|\n)\s*\[(\d{1,3})\]\s*([^\n]+(?:\n(?!\s*\[\d{1,3}\])[^\n]+)*)/g
// Inline source annotation pattern: "来源：XXX [N]" — strip the prefix, keep [N].
// The capture group intentionally excludes '[' so it won't greedily swallow the
// citation marker, and leading/trailing markdown bold/italic markers are
// stripped in parseBibliography to avoid rendering "**" as the card text.
const BIB_INLINE_RE = /来源[：:]\s*([^\n\[]+?)\s*\[(\d{1,3})\]/g

// Strip markdown formatting markers (bold/italic/heading) from bibliography text
// so reference cards never display raw "**" or "##" characters.
function stripBibMarkdown(text: string): string {
  return text
    .replace(/^[\s#*_]+/, '')
    .replace(/[\s#*_]+$/, '')
    .trim()
}

// Lightweight memoization helper keyed by a derived string. Used to avoid
// re-scanning bibliography content when the message is streaming and only the
// suffix of content is changing.
function createMemo<T>(fn: (key: string) => T): (key: string) => T {
  const cache = new Map<string, T>()
  const max = 64
  return (key: string) => {
    let hit = cache.get(key)
    if (hit === undefined) {
      hit = fn(key)
      if (cache.size >= max) {
        const first = cache.keys().next().value
        if (first !== undefined) cache.delete(first)
      }
      cache.set(key, hit)
    } else {
      // LRU
      cache.delete(key)
      cache.set(key, hit)
    }
    return hit
  }
}

const props = defineProps<{
  message: Message
  saveMode?: boolean
  selected?: boolean
  canRegenerate?: boolean
}>()

defineEmits<{
  'toggle-select': []
  regenerate: []
  edit: [messageId: string, content: string]
}>()

const chatStore = useChatStore()
const { show: showToast } = useToast()
const { confirm: showLinkConfirm } = useConfirmDialog()
const { lightboxMedia, lightboxUrl, lightboxKind, openImageLightbox, closeLightbox, onLightboxDownload } =
  useInlineImageZoom()
const contentRef = ref<HTMLElement | null>(null)
const previewIdx = ref<number | null>(null)
// Currently previewed citation number (1-based, matches inline [N] markers).
// null when no preview is open.
const searchPreviewIdx = ref<number | null>(null)
const copied = ref(false)
const copyTooltip = computed(() => copied.value ? '已复制' : '复制')

const showMermaidCodeDialog = ref(false)
const showMermaidZoomDialog = ref(false)
const mermaidCodeSource = ref('')
const mermaidZoomSource = ref('')
const mermaidZoomScale = ref(100)
const mermaidZoomContentRef = ref<HTMLElement | null>(null)
const mermaidZoomSvgRef = ref<HTMLElement | null>(null)
const mermaidSvgWidth = ref(400)
const mermaidSvgHeight = ref(300)
const mermaidSvgViewBox = ref('0 0 400 300')
const REFERENCE_SIZE = 1200

const showMathViewDialog = ref(false)
const mathViewTex = ref('')
const mathViewDisplayMode = ref(false)

const mathViewPreviewHtml = computed(() => {
  if (!mathViewTex.value) return ''
  try {
    return katexModule.renderToString(mathViewTex.value, {
      displayMode: mathViewDisplayMode.value,
      throwOnError: false,
      output: 'html',
      strict: 'ignore',
    })
  } catch {
    return mathViewTex.value
  }
})

function copyMathCode() {
  navigator.clipboard.writeText(mathViewTex.value).then(() => {
    showToast('公式代码已复制', 'success')
  })
}

const mermaidZoomSpacerStyle = computed(() => {
  const scale = mermaidZoomScale.value / 100
  return {
    width: `${mermaidSvgWidth.value * scale}px`,
    height: `${mermaidSvgHeight.value * scale}px`,
  }
})

const mermaidZoomSvgStyle = computed(() => ({
  width: `${mermaidSvgWidth.value * mermaidZoomScale.value / 100}px`,
  height: `${mermaidSvgHeight.value * mermaidZoomScale.value / 100}px`,
}))

function handleMermaidCodeView(source: string) {
  mermaidCodeSource.value = source
  showMermaidCodeDialog.value = true
}

function closeMermaidCodeDialog() {
  showMermaidCodeDialog.value = false
}

function copyMermaidCode() {
  navigator.clipboard.writeText(mermaidCodeSource.value).then(() => {
    showToast('代码已复制', 'success')
  })
}

function handleMermaidZoom(source: string, _id: string, svg: string) {
  mermaidZoomSource.value = source
  mermaidZoomScale.value = 100
  const parser = new DOMParser()
  const doc = parser.parseFromString(svg, 'image/svg+xml')
  const svgEl = doc.querySelector('svg')
  let vbWidth = 400
  let vbHeight = 300
  if (svgEl) {
    const vb = svgEl.getAttribute('viewBox')
    if (vb) {
      mermaidSvgViewBox.value = vb
      const parts = vb.split(/\s+/).map(Number)
      vbWidth = parts[2] || 400
      vbHeight = parts[3] || 300
    }
  }
  const maxDim = Math.max(vbWidth, vbHeight)
  const baseScale = REFERENCE_SIZE / maxDim
  mermaidSvgWidth.value = vbWidth * baseScale
  mermaidSvgHeight.value = vbHeight * baseScale
  let processedSvg = svg
  processedSvg = processedSvg.replace(/width="100%"/, `width="${vbWidth}" height="${vbHeight}"`)
  processedSvg = processedSvg.replace(/max-width:\s*[^;"]+;?/g, '')
  processedSvg = processedSvg.replace(/font-size:\s*\d+(\.\d+)?px/g, 'font-size: 14px')
  showMermaidZoomDialog.value = true
  nextTick(() => {
    const container = mermaidZoomSvgRef.value
    if (container) {
      container.innerHTML = processedSvg
      const innerSvg = container.querySelector('svg')
      if (innerSvg) {
        innerSvg.style.display = 'block'
        innerSvg.style.maxWidth = 'none'
        innerSvg.setAttribute('width', `${vbWidth * baseScale}`)
        innerSvg.setAttribute('height', `${vbHeight * baseScale}`)
        innerSvg.setAttribute('viewBox', mermaidSvgViewBox.value)
      }
      const scrollContainer = mermaidZoomContentRef.value
      if (scrollContainer) {
        scrollContainer.scrollLeft = (scrollContainer.scrollWidth - scrollContainer.clientWidth) / 2
        scrollContainer.scrollTop = (scrollContainer.scrollHeight - scrollContainer.clientHeight) / 2
      }
    }
  })
}

function closeMermaidZoomDialog() {
  showMermaidZoomDialog.value = false
}

let pinchStartDistance = 0
let pinchStartScale = 100

function onMermaidPinchStart(e: TouchEvent) {
  if (e.touches.length !== 2) return
  const dx = e.touches[0].clientX - e.touches[1].clientX
  const dy = e.touches[0].clientY - e.touches[1].clientY
  pinchStartDistance = Math.hypot(dx, dy)
  pinchStartScale = mermaidZoomScale.value
}

function onMermaidPinchMove(e: TouchEvent) {
  if (e.touches.length !== 2 || pinchStartDistance === 0) return
  e.preventDefault()
  const dx = e.touches[0].clientX - e.touches[1].clientX
  const dy = e.touches[0].clientY - e.touches[1].clientY
  const distance = Math.hypot(dx, dy)
  const ratio = distance / pinchStartDistance
  const newScale = Math.round(pinchStartScale * ratio)
  mermaidZoomScale.value = Math.min(300, Math.max(25, newScale))
}

function onMermaidPinchEnd(e: TouchEvent) {
  if (e.touches.length < 2) {
    pinchStartDistance = 0
  }
}

watch(mermaidZoomScale, () => {
  const container = mermaidZoomSvgRef.value
  if (!container) return
  const innerSvg = container.querySelector('svg')
  if (innerSvg) {
    const scale = mermaidZoomScale.value / 100
    const w = mermaidSvgWidth.value * scale
    const h = mermaidSvgHeight.value * scale
    innerSvg.setAttribute('width', `${w}`)
    innerSvg.setAttribute('height', `${h}`)
    innerSvg.setAttribute('viewBox', mermaidSvgViewBox.value)
    innerSvg.style.width = `${w}px`
    innerSvg.style.height = `${h}px`
  }
})

async function onContentClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  const img = target.tagName === 'IMG'
    ? (target as HTMLImageElement)
    : (target.closest('img') as HTMLImageElement | null)
  if (img && contentRef.value?.contains(img) && !img.closest('.image-card')) {
    if (openImageLightbox(img)) {
      e.preventDefault()
      e.stopPropagation()
      return
    }
  }
  if (target.classList.contains('citation-ref')) {
    e.stopPropagation()
    const idx = parseInt(target.dataset.citeIndex || '', 10)
    const maxIdx = citationNumberMap.value.size || maxCitationIndex.value
    if (!isNaN(idx) && idx >= 1 && idx <= maxIdx) {
      searchPreviewIdx.value = searchPreviewIdx.value === idx ? null : idx
    }
    return
  }
  const anchor = target.closest('a[href]') as HTMLAnchorElement | null
  if (anchor && contentRef.value?.contains(anchor)) {
    const href = anchor.getAttribute('href') || ''
    if (href.startsWith('#')) return
    if (href && !href.startsWith('javascript:')) {
      e.preventDefault()
      e.stopPropagation()
      const confirmed = await showLinkConfirm({ message: `确定要打开链接吗？\n${href}` })
      if (confirmed) {
        window.open(href, '_blank', 'noopener,noreferrer')
      }
    }
  }
}

function onMermaidEdit(e: Event) {
  const detail = (e as CustomEvent).detail
  if (detail?.handled) return
  const target = (e as Event).target as HTMLElement
  if (!target || !contentRef.value?.contains(target)) return
  detail.handled = true
  handleMermaidCodeView(detail.source)
}

function onMermaidZoom(e: Event) {
  const detail = (e as CustomEvent).detail
  if (detail?.handled) return
  const target = (e as Event).target as HTMLElement
  if (!target || !contentRef.value?.contains(target)) return
  detail.handled = true
  handleMermaidZoom(detail.source, detail.id, detail.svg)
}

function onMathEdit(e: Event) {
  const detail = (e as CustomEvent).detail
  if (detail?.handled) return
  const target = (e as Event).target as HTMLElement
  if (!target || !contentRef.value?.contains(target)) return
  detail.handled = true
  mathViewTex.value = detail.tex
  mathViewDisplayMode.value = detail.displayMode
  showMathViewDialog.value = true
}

onMounted(() => {
  contentRef.value?.addEventListener('click', onContentClick)
  document.addEventListener('mermaid-edit', onMermaidEdit as EventListener)
  document.addEventListener('mermaid-zoom', onMermaidZoom as EventListener)
  document.addEventListener('math-edit', onMathEdit as EventListener)
})

onBeforeUnmount(() => {
  contentRef.value?.removeEventListener('click', onContentClick)
  document.removeEventListener('mermaid-edit', onMermaidEdit as EventListener)
  document.removeEventListener('mermaid-zoom', onMermaidZoom as EventListener)
  document.removeEventListener('math-edit', onMathEdit as EventListener)
})

function copyMessage() {
  const text = strippedContent.value || props.message.content
  navigator.clipboard.writeText(text).then(() => {
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  })
}

const _VOICE_TOOL_TITLES: Record<string, string> = {
  web_search: '联网搜索',
  browser: '浏览网页',
  execute_code: '代码执行',
  memory: '记忆操作',
  notes: '笔记操作',
  pdf_export: '导出 PDF',
  terminal: '终端命令',
  context7_resolve_library_id: '查找库文档ID',
  context7_query_docs: '查询库文档',
}

const parsedToolResultsData = computed<ToolResultsData | null>(() => {
  if (!props.message.tool_results) return null
  try {
    const parsed = JSON.parse(props.message.tool_results)
    // Old format: plain array — could be SearchResult[] or voice-mode flat tool results
    if (Array.isArray(parsed)) {
      // Detect voice-mode flat format: items have tool_call_id + name + content
      if (parsed.length > 0 && parsed[0]?.tool_call_id !== undefined) {
        const agent_steps: AgentStep[] = parsed.map((tr: any) => ({
          name: tr.tool_call_id || tr.name || '',
          title: _VOICE_TOOL_TITLES[tr.name] || `工具调用 · ${tr.name || ''}`,
          content: (tr.content || '(无内容)').slice(0, 6000),
          step_type: 'tool' as const,
        }))
        return { results: [], search_failed: false, agent_steps }
      }
      return { results: parsed, search_failed: false }
    }
    // New format: object with rounds, results, search_failed
    return parsed as ToolResultsData
  } catch {
    return null
  }
})

const parsedToolResults = computed<SearchResult[]>(() => {
  const data = parsedToolResultsData.value
  if (!data) return []
  if (data.search_failed) {
    // 后端 search_failed=True 的语义是"至少有一轮失败"（chat.py flatten：
    // 任一轮 hits 为空即置位），部分轮次成功时 results 仍有内容，且后端
    // 从不填充 unqualified_results。此时若 unqualified 为空必须回退到
    // results，否则成功的 [N] 引用会被全部剥掉（conv 8f27d43e 复现：
    // 前 2 轮 0 命中、后 2 轮 20 条，引用卡片全部丢失）。
    const unqualified = data.unqualified_results ?? []
    if (unqualified.length > 0) return unqualified
    return data.results ?? []
  }
  return data.results ?? []
})

// Parse the LLM-emitted bibliography section (e.g. "**参考文献**" /
// "## References") into [N] entries. The LLM frequently renumbers its sources
// independently of the raw tool_results array, so the inline [N] markers refer
// to *this* bibliography, not to positions in `parsedToolResults`.
// Also parses inline "来源：XXX [N]" annotations (common in image-search answers)
// so that [N] markers can be fuzzy-matched to the correct tool_result.
interface BibEntry { num: number; text: string }

const parseBibliography = createMemo((content: string): BibEntry[] => {
  const entries: BibEntry[] = []
  const m = content.match(BIB_HEADER_RE)
  if (m) {
    const after = content.slice((m.index ?? 0) + m[0].length)
    // Try line-based entries first.
    let lm: RegExpExecArray | null
    while ((lm = BIB_LINE_RE.exec(after)) !== null) {
      entries.push({ num: parseInt(lm[1], 10), text: stripBibMarkdown(lm[2]) })
    }
    // Fallback for inline semicolon-separated entries such as:
    // "**引用来源：** [1] A; [2] B; [3] C"
    if (entries.length === 0) {
      const inline = after.replace(/\n+/g, ' ').trim()
      const inlineRe = /\[(\d{1,3})\]\s*([^\[\n;]+?)(?=\[\d{1,3}\]|$|;)/g
      let im: RegExpExecArray | null
      while ((im = inlineRe.exec(inline)) !== null) {
        const text = stripBibMarkdown(im[2])
        if (text) entries.push({ num: parseInt(im[1], 10), text })
      }
    }
  }
  if (entries.length === 0) {
    let im: RegExpExecArray | null
    while ((im = BIB_INLINE_RE.exec(content)) !== null) {
      const text = stripBibMarkdown(im[1])
      if (text) {
        entries.push({ num: parseInt(im[2], 10), text })
      }
    }
  }
  return entries
})

const parsedBibliography = computed<BibEntry[]>(() => {
  const content = props.message.content || ''
  return parseBibliography(content)
})

function tokenizeBibText(text: string): string[] {
  const cleaned = text
    .replace(/\d{4}[\-./]\d{1,2}(?:[\-./]\d{1,2})?/g, ' ')
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/[\s\/.,;:\-\u3001\u3002\uff0c\uff0e\uff1a\uff1b\uff08\uff09\u201c\u201d\u2018\u2019()\[\]\u3010\u3011\*<>"'`]+/g, ' ')
  const tokens = new Set<string>()
  for (const raw of cleaned.split(' ')) {
    const t = raw.trim()
    if (t.length >= 2) tokens.add(t)
  }
  return Array.from(tokens)
}

// For each bibliography entry, find the best-matching tool_result by counting
// distinctive token overlap against title+url. CJK tokens are weighted higher
// (weight 3) since they are more distinctive than short ASCII tokens.
const matchBibliography = createMemo((key: string): Array<{ entry: BibEntry; result: SearchResult | null }> => {
  const [bibJson, resultsJson] = key.split('\x00')
  const bib: BibEntry[] = JSON.parse(bibJson || '[]')
  const results: SearchResult[] = JSON.parse(resultsJson || '[]')
  if (bib.length === 0) return []
  const isCJK = (s: string) => /[\u4e00-\u9fff\u3040-\u30ff]/.test(s)
  return bib.map(entry => {
    if (results.length === 0) return { entry, result: null }
    const tokens = tokenizeBibText(entry.text)
    let bestScore = 0
    let best: SearchResult | null = null
    for (const r of results) {
      const hay = `${r.title || ''} ${r.url || ''}`
      let score = 0
      for (const tok of tokens) {
        if (hay.includes(tok)) score += isCJK(tok) ? 3 : 1
      }
      if (score > bestScore) {
        bestScore = score
        best = r
      }
    }
    return { entry, result: bestScore > 0 ? best : null }
  })
})

const bibliographyMatches = computed<Array<{ entry: BibEntry; result: SearchResult | null }>>(() => {
  const bib = parsedBibliography.value
  const results = parsedToolResults.value
  return matchBibliography(`${JSON.stringify(bib)}\x00${JSON.stringify(results)}`)
})

// Highest valid citation number for inline-marker clicks.
const maxCitationIndex = computed(() => {
  return parsedBibliography.value.length > 0
    ? parsedBibliography.value.length
    : parsedToolResults.value.length
})

// Indices [1..N] actually cited in the body text (numbers in [..] within the
// valid range). Used to filter the chip list so unreferenced search noise
// (common with DuckDuckGo fallback) doesn't pollute the UI.
const referencedCitationIndices = computed<Set<number>>(() => {
  const total = maxCitationIndex.value
  if (total === 0) return new Set()
  const text = props.message.content || ''
  // Strip fenced code / inline code so [N] inside code isn't treated as a cite
  const cleaned = text
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`\n]+`/g, '')
  const found = new Set<number>()
  // Backend alignment (citation_ledger._CITE_RE): exclude markdown links
  // [n](url) and reference labels [n]: from cite detection.
  const re = /\[(\d{1,3})\](?![(:])/g
  let m: RegExpExecArray | null
  while ((m = re.exec(cleaned)) !== null) {
    const n = parseInt(m[1], 10)
    if (n >= 1 && n <= total) found.add(n)
  }
  return found
})

// Map old citation numbers to sequential new numbers (e.g., 1→1, 3→2, 5→3).
const citationNumberMap = computed<Map<number, number>>(() => {
  const refs = referencedCitationIndices.value
  if (refs.size === 0) return new Map()
  const sorted = Array.from(refs).sort((a, b) => a - b)
  const map = new Map<number, number>()
  sorted.forEach((oldNum, i) => map.set(oldNum, i + 1))
  return map
})

// Reverse map: new sequential number → old citation number
const citationNumberReverseMap = computed<Map<number, number>>(() => {
  const map = new Map<number, number>()
  citationNumberMap.value.forEach((newNum, oldNum) => map.set(newNum, oldNum))
  return map
})

// Chips/cards to actually render. The `num` field is the 1-based citation
// index shown on the badge — renumbered sequentially to eliminate gaps.
const displayedToolResults = computed<Array<{ result: SearchResult; num: number; bibText?: string }>>(() => {
  // Don't show any chips if there are no actual search results — fabricated
  // bibliography entries (when the LLM answers from its own knowledge without
  // web_search) should not appear as reference tags.
  if (parsedToolResults.value.length === 0) return []
  const refs = referencedCitationIndices.value
  if (refs.size === 0) return []
  const numMap = citationNumberMap.value
  const bib = bibliographyMatches.value
  if (bib.length > 0) {
    // Only entries that fuzzy-matched a REAL search result become chips.
    // An entry with no match is a fabricated source — rendering it as a
    // chip with an empty URL gave hallucinated sources a UI badge
    // (grounded-citations integrity fix).
    const matched = bib.filter(({ entry, result }) => refs.has(entry.num) && result)
    if (matched.length > 0) {
      const items = matched.map(({ entry, result }) => ({
        result: result as SearchResult,
        num: numMap.get(entry.num) || entry.num,
        bibText: entry.text,
      }))
      items.sort((a, b) => a.num - b.num)
      return items
    }
    // No bibliography entry matched a real result — fall through to the
    // positional mapping so genuine citations still render as chips.
  }
  let items = parsedToolResults.value
    .map((result, i) => ({ result, num: i + 1 }))
    .filter(({ num }) => refs.has(num))
    .map(({ result, num }) => ({
      result,
      num: numMap.get(num) || num,
    }))
  items.sort((a, b) => a.num - b.num)
  return items
})

// Result currently shown in the preview popup, resolved by citation number.
const previewedResult = computed<{ result: SearchResult; bibText?: string } | null>(() => {
  const n = searchPreviewIdx.value
  if (n === null) return null
  const hit = displayedToolResults.value.find(d => d.num === n)
  if (hit) return { result: hit.result, bibText: hit.bibText }
  // Fallback: reverse map to original index for positional lookup
  const oldNum = citationNumberReverseMap.value.get(n)
  if (oldNum) {
    const r = parsedToolResults.value[oldNum - 1]
    if (r) return { result: r }
  }
  return null
})

const parsedToolRounds = computed<SearchRound[]>(() => {
  return parsedToolResultsData.value?.rounds ?? []
})

const isSearchFailed = computed(() => {
  return parsedToolResultsData.value?.search_failed === true
})

function handleUseUnqualified() {
  const data = parsedToolResultsData.value
  if (!data?.unqualified_results) return
  const forceJson = JSON.stringify(data.unqualified_results)
  chatStore.regenerateWithForceResults(props.message.id, forceJson)
}

const parsedAgentSteps = computed<AgentStep[]>(() => {
  return parsedToolResultsData.value?.agent_steps ?? []
})

const parsedAttachments = computed<FileAttachment[]>(() => {
  const raw = parsedToolResultsData.value?.attachments ?? []
  // Deduplicate by name so duplicate download cards never appear
  const seen = new Set<string>()
  return raw.filter((att) => {
    const name = att.name || att.filename || ''
    if (!name || seen.has(name)) return false
    seen.add(name)
    return true
  })
})

const displaySequence = computed<DisplaySequenceItem[]>(() => {
  return parsedToolResultsData.value?.display_sequence ?? []
})

// Persisted tool items reuse the streaming ToolPartCard so a tool call looks
// identical during streaming and after reload. Backend stores them as
// { type: 'tool'|'tool_call', name: <call_id>, title: <localized name>,
//   content: <result JSON> } — map onto the streaming item shape.
function isToolSequenceItem(item: DisplaySequenceItem): boolean {
  const t = item.type || item.step_type || ''
  return t === 'tool' || t === 'tool_call'
}

function toToolPartItem(item: DisplaySequenceItem): DisplaySequenceItem {
  return {
    ...item,
    type: 'tool_call',
    status: item.status ?? 'completed',
    result: item.result ?? item.content,
  }
}

const hasReasoningStep = computed(() => {
  return displaySequence.value.some(item => item.type === 'reasoning_step')
})

const parsedTaskPlan = computed<TaskPlan | null>(() => {
  return parsedToolResultsData.value?.task_plan ?? null
})

const parsedThinkingHistory = computed<SubAgentThinking[]>(() => {
  return parsedToolResultsData.value?.thinking_history ?? []
})

const parsedSubAgentOutputs = computed<Array<SubAgentOutput & { id: string }>>(() => {
  const outputs = parsedToolResultsData.value?.sub_agent_outputs ?? {}
  return Object.entries(outputs).map(([id, output]) => ({
    id,
    name: output?.name ?? id,
    content: output?.content ?? '',
    reasoning: output?.reasoning ?? '',
  }))
})

const hasPersistedProcess = computed(() => {
  return !!parsedTaskPlan.value
    || parsedThinkingHistory.value.length > 0
    || parsedAgentSteps.value.length > 0
    || parsedSubAgentOutputs.value.length > 0
})

function isLlmStep(step: AgentStep): boolean {
  return step.step_type === 'llm'
}

function escapeHtml(unsafe: string): string {
  return unsafe
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

function formatAgentStepContent(content: string): string {
  if (!content) return ''
  
  // Try to parse JSON content
  try {
    const parsed = JSON.parse(content)
    if (typeof parsed === 'object' && parsed !== null) {
      // Format code execution results
      if (parsed.task) {
        let formatted = `<div class="step-task"><strong>任务:</strong> ${escapeHtml(String(parsed.task))}</div>`
        if (parsed.use_tools !== undefined) {
          formatted += `<div class="step-meta"><small>使用工具: ${parsed.use_tools ? '是' : '否'}</small></div>`
        }
        return formatted
      }
      // Format other JSON results
      const keys = Object.keys(parsed)
      if (keys.length > 0) {
        let formatted = '<div class="step-json">'
        for (const key of keys) {
          const value = parsed[key]
          const safeKey = escapeHtml(String(key))
          if (typeof value === 'string' && value.length > 100) {
            formatted += `<div class="step-json-item"><strong>${safeKey}:</strong> <span class="step-json-value">${escapeHtml(value.substring(0, 100))}...</span></div>`
          } else {
            formatted += `<div class="step-json-item"><strong>${safeKey}:</strong> <span class="step-json-value">${escapeHtml(JSON.stringify(value))}</span></div>`
          }
        }
        formatted += '</div>'
        return formatted
      }
    }
  } catch {
    // Not JSON, continue with text formatting
  }
  
  // Replace escaped newlines and format as HTML
  let formatted = content
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\"/g, '"')
    .replace(/\\'/g, "'")
    .replace(/\\\\/g, '\\')
  
  // Convert newlines to HTML breaks
  formatted = formatted.split('\n').map(line => `<div class="step-line">${escapeHtml(line)}</div>`).join('')
  
  return formatted || content
}

function extractDomain(url: string): string {
  if (!url) return ''
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url.slice(0, 30)
  }
}

function formatTimestamp(dateStr: string): string {
  if (!dateStr) return ''
  try {
    const normalized = dateStr.endsWith('Z') || dateStr.includes('+') || dateStr.includes('-', 10) ? dateStr : dateStr + 'Z'
    const d = new Date(normalized)
    if (isNaN(d.getTime())) return ''
    const year = d.getFullYear()
    const month = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    const hours = String(d.getHours()).padStart(2, '0')
    const minutes = String(d.getMinutes()).padStart(2, '0')
    const seconds = String(d.getSeconds()).padStart(2, '0')
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`
  } catch {
    return ''
  }
}

// Parse note references from content
const NOTE_REF_REGEX = /\[note-ref:([^|]*)\|([^\]]*)\]\n([\s\S]*?)\n\[\/note-ref\]\n*/g

// Strip heading-based bibliography sections (参考来源/参考资料/参考文献/References/Sources)
// from the displayed content. References should only appear as tag chips below the answer,
// not as inline text sections. Chicago-style references are added only when saving to notes.
const BIBLIOGRAPHY_SECTION_REGEX = /(?:\n\n?---[^\S\n]*\n+)?(?:^|\n)[^\S\n]*(?:#{1,6}[^\S\n]*|\*{1,2}[^\S\n]*)?(?:参考文献|参考资料|参考来源|References|Sources|Reference)[：:]?[^\S\n]*(?:\*{1,2})?[^\S\n]*\n[\s\S]*$/i

// Inline source annotation pattern: "来源：XXX [N]" — strip the prefix, keep [N]
const INLINE_SOURCE_REGEX = /来源[：:]\s*[^\n\[]+?\s*\[(\d{1,2})\]/g

interface NoteRefInfo {
  id: string
  title: string
  content: string
}

const noteRefs = computed<NoteRefInfo[]>(() => {
  const refs: NoteRefInfo[] = []
  let match: RegExpExecArray | null
  const re = new RegExp(NOTE_REF_REGEX.source, 'g')
  while ((match = re.exec(props.message.content)) !== null) {
    refs.push({ id: match[1], title: match[2], content: match[3] })
  }
  return refs
})

// Parse file references from content
const FILE_REF_REGEX = /\[file-ref:([^\]]*)\]\n([\s\S]*?)\n\[\/file-ref\]\n*/g

interface FileRefInfo {
  filename: string
  content: string
}

const fileRefs = computed<FileRefInfo[]>(() => {
  const refs: FileRefInfo[] = []
  let match: RegExpExecArray | null
  const re = new RegExp(FILE_REF_REGEX.source, 'g')
  while ((match = re.exec(props.message.content)) !== null) {
    refs.push({ filename: match[1], content: match[2] })
  }
  return refs
})

const filePreviewIdx = ref<number | null>(null)
const filePreviewHtml = computed(() => {
  if (filePreviewIdx.value === null) return ''
  const ref = fileRefs.value[filePreviewIdx.value]
  if (!ref) return ''
  return renderMarkdownToHtml(ref.content)
})

const strippedContent = computed(() => {
  let content = stripDsmlTags(props.message.content)
    .replace(NOTE_REF_REGEX, '')
    .replace(FILE_REF_REGEX, '')
    .replace(/<!--\s*segment_split\s*-->/g, '\n\n')
  // Strip heading-based bibliography section (参考来源/参考资料/参考文献/References/Sources)
  content = content.replace(BIBLIOGRAPHY_SECTION_REGEX, '')
  // Strip "来源：XXX " prefix from inline source annotations, keeping [N]
  content = content.replace(INLINE_SOURCE_REGEX, '[$1]')
  return content.trim()
})

const previewHtml = computed(() => {
  if (previewIdx.value === null) return ''
  const ref = noteRefs.value[previewIdx.value]
  if (!ref) return ''
  return renderMarkdownToHtml(ref.content)
})

const formattedContent = computed(() => {
  let html = renderMarkdownToHtml(strippedContent.value)
  if (parsedToolResults.value.length > 0) {
    html = addCitationSuperscripts(html, true, referencedCitationIndices.value)
    if (citationNumberMap.value.size > 0) {
      html = renumberCitationSuperscripts(html, citationNumberMap.value)
    }
  } else {
    // No actual search results — strip any remaining [N] markers so the body
    // is clean (the LLM may have written [1] [2] referencing fabricated sources)
    html = html.replace(/\s*\[(\d{1,3})\](?![(:]|[个条项种款次位名件点步轮天年月人台张篇章节段行字句页分秒小时家枚])(?<!第)/g, '')
  }
  return html
})

function formatTextWithCitations(content: string): string {
  // Apply the same stripping as strippedContent: remove bibliography sections
  // and inline source prefixes so references only appear as tag chips below
  let processed = content
    .replace(BIBLIOGRAPHY_SECTION_REGEX, '')
    .replace(INLINE_SOURCE_REGEX, '[$1]')
  let html = renderMarkdownToHtml(processed)
  if (parsedToolResults.value.length > 0) {
    html = addCitationSuperscripts(html, true, referencedCitationIndices.value)
    if (citationNumberMap.value.size > 0) {
      html = renumberCitationSuperscripts(html, citationNumberMap.value)
    }
  } else {
    html = html.replace(/\s*\[(\d{1,3})\](?![(:]|[个条项种款次位名件点步轮天年月人台张篇章节段行字句页分秒小时家枚])(?<!第)/g, '')
  }
  return html
}

const formattedReasoning = computed(() => {
  if (!props.message.reasoning_content) return ''
  return renderMarkdownToHtml(props.message.reasoning_content)
})

watch(
  [formattedContent, formattedReasoning],
  () => {
    nextTick(() => {
      renderMermaidBlocks(contentRef.value)
      renderEchartsBlocks(contentRef.value)
    })
  },
  { flush: 'post' }
)

watch(
  [() => props.saveMode, () => props.selected],
  () => {
    nextTick(() => {
      setTimeout(() => {
        const container = contentRef.value
        if (!container) return
        fixLostMermaidBlocks(container)
        renderMermaidBlocks(container)
        fixLostEchartsBlocks(container)
        renderEchartsBlocks(container)
        attachMathEditListeners(container)
      }, 200)
    })
  }
)

watch(
  [showMermaidZoomDialog, showMermaidCodeDialog, showMathViewDialog],
  () => {
    nextTick(() => {
      setTimeout(() => {
        if (contentRef.value) {
          fixLostMermaidBlocks(contentRef.value)
          renderMermaidBlocks(contentRef.value)
          fixLostEchartsBlocks(contentRef.value)
          renderEchartsBlocks(contentRef.value)
          attachMathEditListeners(contentRef.value)
        }
      }, 50)
    })
  }
)

onMounted(() => {
  renderMermaidBlocks(contentRef.value)
  renderEchartsBlocks(contentRef.value)
})
</script>

<style scoped>
.message-bubble {
  display: flex;
  gap: 12px;
  padding: 12px 20px;
  animation: fadeIn 0.3s ease-out;
  contain: layout style;
}

@keyframes fadeIn {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.message-bubble.user {
  flex-direction: row-reverse;
}

.avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.content {
  max-width: 70%;
  position: relative;
  display: flex;
  flex-direction: column;
}

.msg-timestamp {
  font-size: 11px;
  color: var(--color-text-light);
  opacity: 0.6;
  margin-bottom: 4px;
  user-select: none;
  font-variant-numeric: tabular-nums;
}

.user .msg-timestamp {
  text-align: right;
}

.msg-checkbox {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  cursor: pointer;
  align-self: center;
  accent-color: var(--color-primary);
}

.message-bubble.save-mode {
  cursor: pointer;
}

.message-bubble.save-mode:hover {
  background-color: var(--color-hover);
  border-radius: var(--radius-sm);
}

/* Note reference tags in message */
.msg-note-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

/* Reasoning content block */
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

.msg-note-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
  border-radius: 12px;
  font-size: 12px;
  cursor: pointer;
  transition: background-color var(--transition-fast);
  line-height: 1.4;
}

.msg-note-tag:hover {
  background-color: color-mix(in srgb, var(--color-primary) 20%, transparent);
}

.msg-note-tag svg {
  flex-shrink: 0;
}

.msg-file-tag {
  background-color: color-mix(in srgb, var(--color-secondary) 12%, transparent);
  color: var(--color-secondary);
}

.msg-file-tag:hover {
  background-color: color-mix(in srgb, var(--color-secondary) 20%, transparent);
}

.message-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
  gap: 4px;
}

.message-action-btn {
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  background-color: var(--color-hover);
  color: var(--color-text-light);
  font-size: 12px;
  transition: background-color var(--transition-fast), color var(--transition-fast), transform var(--transition-fast);
}

.message-action-btn.icon-btn {
  padding: 11px;
  min-width: 36px;
  min-height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  line-height: 0;
  position: relative;
}

.message-action-btn:active {
  transform: scale(0.96);
}

.message-action-btn .copy-icon,
.message-action-btn .check-icon {
  position: absolute;
  inset: 0;
  margin: auto;
  transition: opacity 0.2s cubic-bezier(0.2, 0, 0, 1), transform 0.2s cubic-bezier(0.2, 0, 0, 1), filter 0.2s cubic-bezier(0.2, 0, 0, 1);
}

.message-action-btn .copy-icon.hidden,
.message-action-btn .check-icon.hidden {
  opacity: 0;
  transform: scale(0.25);
  filter: blur(4px);
  pointer-events: none;
}

/* Note preview popup */
.note-preview-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: rgba(0, 0, 0, 0.4);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.note-preview-card {
  background-color: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 600px;
  width: 100%;
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.note-preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 24px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.note-preview-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
}

.note-preview-close {
  font-size: 20px;
  color: var(--color-text-light);
  padding: 4px 8px;
  border-radius: var(--radius-sm);
}

.note-preview-close:hover {
  background-color: var(--color-hover);
}

.note-preview-body {
  padding: 20px 24px 24px;
  overflow-y: auto;
  font-size: 14px;
  line-height: 1.7;
  color: var(--color-text);
}

.note-preview-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 12px 0;
  display: block;
  overflow-x: auto;
  font-size: 13px;
}

.note-preview-body :deep(thead),
.note-preview-body :deep(tbody) {
  display: table;
  width: 100%;
  table-layout: fixed;
}

.note-preview-body :deep(tr) {
  display: table-row;
}

.note-preview-body :deep(th),
.note-preview-body :deep(td) {
  border: 1px solid var(--color-border);
  padding: 8px 12px;
  text-align: left;
  display: table-cell;
  word-break: normal;
  overflow-wrap: break-word;
}

.note-preview-body :deep(th) {
  background-color: var(--color-hover);
  font-weight: 600;
}

.note-preview-body :deep(tr:nth-child(even)) {
  background-color: color-mix(in srgb, var(--color-hover) 50%, transparent);
}

.note-preview-body :deep(pre) {
  background-color: var(--color-code-bg);
  padding: 12px;
  border-radius: var(--radius-sm);
  overflow-x: auto;
  font-size: 13px;
  border: 1px solid var(--color-border);
}

.note-preview-body :deep(.code-block pre) {
  background: transparent;
  border: 0;
  border-radius: 0;
  margin: 0;
  padding: 12px 16px;
}

.note-preview-body :deep(code) {
  font-family: var(--font-mono);
  background-color: var(--color-code-bg);
  padding: 2px 4px;
  border-radius: 3px;
  font-size: 13px;
}

.note-preview-body :deep(pre code) {
  background: none;
  padding: 0;
}

.note-preview-body :deep(h1),
.note-preview-body :deep(h2),
.note-preview-body :deep(h3),
.note-preview-body :deep(h4),
.note-preview-body :deep(h5),
.note-preview-body :deep(h6) {
  margin: 16px 0 8px 0;
  font-weight: 600;
  line-height: 1.4;
  color: var(--color-text);
}

.note-preview-body :deep(h1) { font-size: 1.5em; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
.note-preview-body :deep(h2) { font-size: 1.3em; border-bottom: 1px solid var(--color-border); padding-bottom: 6px; }
.note-preview-body :deep(h3) { font-size: 1.15em; }

.note-preview-body :deep(ul),
.note-preview-body :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.note-preview-body :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.note-preview-body :deep(ul) {
  list-style-type: disc;
}

.note-preview-body :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.note-preview-body :deep(ol > li) {
  counter-increment: ol-counter;
}

.note-preview-body :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  margin-right: 2px;
}

.note-preview-body :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 16px;
  border-left: 4px solid var(--color-primary);
  background-color: var(--color-bg);
  color: var(--color-text-light);
  font-style: italic;
}

.note-preview-body :deep(blockquote p) {
  margin: 0;
}

.note-preview-body :deep(p) {
  margin: 8px 0;
}

.note-preview-body :deep(p:first-child) {
  margin-top: 0;
}

.note-preview-body :deep(p:last-child) {
  margin-bottom: 0;
}

.note-preview-body :deep(a) {
  color: var(--color-primary-dark);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
}

.note-preview-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 16px 0;
}

.note-preview-body :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-md);
  margin: 8px 0;
}

.text {
  padding: 12px 16px;
  border-radius: var(--msg-bubble-radius);
  line-height: 1.6;
  word-break: break-word;
  overflow-x: auto;
  border: var(--msg-bubble-border);
  box-shadow: var(--msg-bubble-shadow);
}

.user .text {
  background-color: var(--msg-user-bg);
  border-bottom-right-radius: var(--msg-bubble-br);
  /* Hug the text: the .content column is as wide as the timestamp row, so a
     stretch-width bubble shows a large blank area right of short queries. */
  align-self: flex-end;
}

.assistant .text {
  background-color: var(--msg-assistant-bg);
  border-bottom-left-radius: var(--msg-bubble-bl);
}

.text :deep(del),
.text :deep(s),
.text :deep(strike) {
  text-decoration: none;
}

.text :deep(code) {
  font-family: var(--font-mono);
  background-color: var(--color-code-bg);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

.text :deep(pre) {
  background-color: var(--color-code-bg);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--color-border);
}

.text :deep(.code-block pre) {
  background: transparent;
  border: 0;
  border-radius: 0;
  margin: 0;
  padding: 12px 16px;
}

.text :deep(pre code) {
  background: none;
  padding: 0;
}

.text :deep(h1),
.text :deep(h2),
.text :deep(h3),
.text :deep(h4),
.text :deep(h5),
.text :deep(h6) {
  margin: 16px 0 8px 0;
  font-weight: 600;
  line-height: 1.4;
  color: var(--color-text);
}

.text :deep(h1) { font-size: 1.5em; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
.text :deep(h2) { font-size: 1.3em; border-bottom: 1px solid var(--color-border); padding-bottom: 6px; }
.text :deep(h3) { font-size: 1.15em; }
.text :deep(h4) { font-size: 1em; }
.text :deep(h5) { font-size: 0.95em; }
.text :deep(h6) { font-size: 0.9em; color: var(--color-text-light); }

.text :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 13px;
  display: block;
  overflow-x: auto;
}

.text :deep(thead),
.text :deep(tbody) {
  display: table;
  width: 100%;
  table-layout: fixed;
}

.text :deep(th),
.text :deep(td) {
  border: 1px solid var(--color-border);
  padding: 8px 12px;
  text-align: left;
  word-break: normal;
  overflow-wrap: break-word;
}

.text :deep(th) {
  background-color: var(--color-bg);
  font-weight: 600;
}

.text :deep(tr:nth-child(even)) {
  background-color: var(--color-bg);
}

.text :deep(ul),
.text :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.text :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.text :deep(ul) {
  list-style-type: disc;
}

.text :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.text :deep(ol > li) {
  counter-increment: ol-counter;
}

.text :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  margin-right: 2px;
}

.text :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 16px;
  border-left: 4px solid var(--color-primary);
  background-color: var(--color-bg);
  color: var(--color-text-light);
  font-style: italic;
}

.text :deep(blockquote p) {
  margin: 0;
}

.text :deep(a) {
  color: var(--color-primary-dark);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
}

.text :deep(a:hover) {
  border-bottom-style: solid;
}

.text :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 16px 0;
}

.text :deep(p) {
  margin: 8px 0;
}

.text :deep(p:first-child) {
  margin-top: 0;
}

.text :deep(p:last-child) {
  margin-bottom: 0;
}

.text :deep(img),
.reasoning-text :deep(img) {
  max-width: min(100%, 420px);
  max-height: 320px;
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: var(--radius-md);
  margin: 8px 0;
  cursor: zoom-in;
  vertical-align: middle;
}

.text :deep(img:hover),
.reasoning-text :deep(img:hover) {
  outline: 1px solid var(--color-border);
  outline-offset: 2px;
}

.text :deep(.hljs) {
  background: transparent;
}

.text :deep(.mermaid-block) {
  position: relative;
  display: flex;
  justify-content: center;
  margin: 12px 0;
  overflow-x: auto;
}

.text :deep(.mermaid-block svg) {
  max-width: 100%;
  height: auto;
}

.text :deep(.mermaid-error) {
  background-color: color-mix(in srgb, var(--color-danger, #e53e3e) 10%, transparent);
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
}

.text :deep(.echarts-block) {
  position: relative;
  margin: 12px 0;
  overflow-x: auto;
}

.text :deep(.echarts-block svg) {
  max-width: 100%;
  height: auto;
  display: block;
}

.text :deep(.echarts-error) {
  background-color: color-mix(in srgb, var(--color-danger, #e53e3e) 10%, transparent);
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
  overflow-x: auto;
}

.text :deep(.hljs-keyword),
.text :deep(.hljs-selector-tag),
.text :deep(.hljs-built_in),
.text :deep(.hljs-name),
.text :deep(.hljs-tag) {
  color: var(--code-keyword, #d73a49);
}

.text :deep(.hljs-string),
.text :deep(.hljs-title),
.text :deep(.hljs-section),
.text :deep(.hljs-attribute),
.text :deep(.hljs-literal),
.text :deep(.hljs-template-tag),
.text :deep(.hljs-template-variable),
.text :deep(.hljs-type) {
  color: var(--code-string, #032f62);
}

.text :deep(.hljs-comment),
.text :deep(.hljs-quote) {
  color: var(--code-comment, #6a737d);
  font-style: italic;
}

.text :deep(.hljs-number),
.text :deep(.hljs-addition) {
  color: var(--code-number, #005cc5);
}

.text :deep(.hljs-function) {
  color: var(--code-function, #6f42c1);
}

.text :deep(.hljs-variable),
.text :deep(.hljs-params) {
  color: var(--code-variable, #e36209);
}

@media (max-width: 767px) {
  .message-bubble {
    padding: 8px 12px;
  }

  .message-bubble .content {
    max-width: 85%;
  }

  /* 手机端：助手回答的复制/重新生成按钮组靠左（仅水平位置，其余不变） */
  .assistant .message-actions {
    justify-content: flex-start;
  }
}

@media (max-width: 480px) {
  .message-bubble {
    padding: 6px 10px;
  }

  .message-bubble .avatar {
    width: 28px;
    height: 28px;
  }

  .message-bubble .avatar svg {
    width: 16px;
    height: 16px;
  }

  .message-bubble .content {
    max-width: 88%;
  }

  .message-bubble .text {
    padding: 10px 12px;
    font-size: 13px;
  }

  .text :deep(pre) {
    padding: 10px 12px;
    font-size: 12px;
  }

  .text :deep(.code-block pre) {
    padding: 10px 12px;
  }
}

/* Reference tags (search results) */
.msg-ref-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.msg-ref-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 12px;
  cursor: pointer;
  transition: background-color var(--transition-fast);
  line-height: 1.4;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-ref-tag svg {
  flex-shrink: 0;
}

.msg-ref-index {
  flex-shrink: 0;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
  opacity: 0.95;
}

.msg-ref-tag--search {
  background-color: color-mix(in srgb, var(--color-primary) 10%, transparent);
  color: var(--color-primary);
}

.msg-ref-tag--search:hover {
  background-color: color-mix(in srgb, var(--color-primary) 18%, transparent);
}

.msg-ref-tag--active {
  background-color: color-mix(in srgb, var(--color-primary) 26%, transparent) !important;
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

.persisted-process {
  margin-top: 10px;
}

.agent-step-block {
  margin-top: 12px;
  margin-bottom: 8px;
  padding: 10px 14px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-secondary);
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
}

/* Ensure adjacent text segments inside the alternating-content stream
   never touch the next tool-call bubble. */
.alternating-content .segment-text + .agent-step-block,
.alternating-content .segment-text + .reasoning-block,
.alternating-content .agent-step-block + .segment-text,
.alternating-content .reasoning-block + .segment-text {
  margin-top: 12px;
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
  font-size: 12px;
  line-height: 1.6;
}

.agent-step-text .step-task {
  padding: 8px 12px;
  background: var(--color-bg);
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
  border-left: 3px solid var(--color-primary);
}

.agent-step-text .step-meta {
  margin-top: 4px;
  color: var(--color-text-light);
}

.agent-step-text .step-json {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.agent-step-text .step-json-item {
  padding: 4px 8px;
  background: var(--color-bg);
  border-radius: var(--radius-sm);
}

.agent-step-text .step-json-value {
  color: var(--color-text-light);
  word-break: break-word;
}

.agent-step-text .step-line {
  padding: 2px 0;
  border-bottom: 1px solid var(--color-border);
}

.agent-step-text .step-line:last-child {
  border-bottom: none;
}

.agent-step-markdown {
  margin-top: 6px;
}

.agent-step-block--task :deep(.task-progress) {
  margin: 8px 0 0;
}

.thinking-history-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

.thinking-history-item {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  font-size: 12px;
}

.thinking-history-name {
  font-weight: 600;
  color: var(--color-text);
}

.thinking-history-phase {
  padding: 1px 8px;
  border-radius: 999px;
  background-color: color-mix(in srgb, var(--color-border) 55%, transparent);
  color: var(--color-text-light);
}

.thinking-history-phase.running {
  background-color: color-mix(in srgb, var(--color-primary) 14%, transparent);
  color: var(--color-primary);
}

.thinking-history-phase.done {
  background-color: color-mix(in srgb, #059669 14%, transparent);
  color: #059669;
}

.thinking-history-text {
  flex: 1;
  min-width: 0;
}

.sub-agent-reasoning {
  margin-top: 8px;
}

/* Search preview */
.search-preview-url {
  display: block;
  color: var(--color-primary-dark);
  font-size: 13px;
  margin-bottom: 12px;
  word-break: break-all;
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
}

.search-preview-url:hover {
  border-bottom-style: solid;
}

.search-preview-snippet {
  font-size: 14px;
  line-height: 1.7;
  color: var(--color-text);
  margin: 0;
}

/* User message actions (edit button) */
.user .message-actions {
  justify-content: flex-start;
}

/* Inline citation superscripts */
.text :deep(.citation-ref) {
  font-size: 0.75em;
  vertical-align: super;
  line-height: 0;
  color: var(--color-primary);
  cursor: pointer;
  font-weight: 600;
  padding: 0 1px;
  transition: color var(--transition-fast);
}

.text :deep(.citation-ref:hover) {
  color: var(--color-primary-dark);
  text-decoration: underline;
}

.text :deep(.mermaid-controls) {
  position: absolute;
  top: 6px;
  right: 6px;
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
  z-index: 10;
}

.text :deep(.mermaid-block:hover .mermaid-controls) {
  opacity: 0.7;
}

.text :deep(.mermaid-edit-btn),
.text :deep(.mermaid-zoom-btn) {
  padding: 4px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-light);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.2s, background-color 0.2s, color 0.2s, border-color 0.2s, transform 0.2s;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
}

.text :deep(.mermaid-edit-btn:active),
.text :deep(.mermaid-zoom-btn:active) {
  transform: scale(0.96);
}

.text :deep(.mermaid-edit-btn:hover),
.text :deep(.mermaid-zoom-btn:hover) {
  opacity: 1;
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.alternating-content :deep(.mermaid-block) {
  position: relative;
}

.alternating-content :deep(.mermaid-controls) {
  position: absolute;
  top: 6px;
  right: 6px;
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
  z-index: 10;
}

.alternating-content :deep(.mermaid-block:hover .mermaid-controls) {
  opacity: 0.7;
}

.alternating-content :deep(.mermaid-edit-btn),
.alternating-content :deep(.mermaid-zoom-btn) {
  padding: 4px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-light);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.2s, background-color 0.2s, color 0.2s, border-color 0.2s, transform 0.2s;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
}

.alternating-content :deep(.mermaid-edit-btn:active),
.alternating-content :deep(.mermaid-zoom-btn:active) {
  transform: scale(0.96);
}

.alternating-content :deep(.mermaid-edit-btn:hover),
.alternating-content :deep(.mermaid-zoom-btn:hover) {
  opacity: 1;
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.mermaid-dialog-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-scrim);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.mermaid-dialog {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 700px;
  width: 100%;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.mermaid-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
}

.mermaid-dialog-header h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
  color: var(--color-text);
}

.mermaid-dialog-close {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast), color var(--transition-fast), transform var(--transition-fast);
}

.mermaid-dialog-close:hover {
  background: var(--color-hover);
  color: var(--color-text);
}

.mermaid-dialog-close:active {
  transform: scale(0.96);
}

.mermaid-dialog-content {
  flex: 1;
  padding: 16px 20px;
  overflow-y: auto;
  min-height: 200px;
}

.mermaid-code-textarea {
  width: 100%;
  min-height: 250px;
  max-height: 60vh;
  padding: 12px;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-code-bg);
  color: var(--color-text);
  resize: vertical;
  box-sizing: border-box;
  outline: none;
}

.mermaid-code-textarea:focus {
  border-color: var(--color-primary);
}

.mermaid-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border);
}

.mermaid-dialog-cancel {
  padding: 6px 16px;
  background: var(--color-hover);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.mermaid-dialog-cancel:hover {
  background: color-mix(in srgb, var(--color-text) 12%, var(--color-hover));
}

.mermaid-dialog-cancel:active {
  transform: scale(0.96);
}

.mermaid-dialog-save {
  padding: 6px 16px;
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.mermaid-dialog-save:hover {
  background: var(--color-primary-dark);
}

.mermaid-dialog-save:active {
  transform: scale(0.96);
}

.mermaid-zoom-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.mermaid-zoom-card {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 90vw;
  width: 900px;
  height: 85vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.mermaid-zoom-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--color-border);
  flex-wrap: wrap;
  gap: 12px;
}

.mermaid-zoom-header h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
  color: var(--color-text);
}

.mermaid-zoom-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  justify-content: center;
}

.zoom-label {
  font-size: 12px;
  color: var(--color-text-light);
}

.zoom-slider {
  width: 120px;
  accent-color: var(--color-primary);
}

.zoom-value {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  min-width: 36px;
  font-variant-numeric: tabular-nums;
}

.zoom-reset-btn {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  transition: background-color var(--transition-fast), color var(--transition-fast), transform var(--transition-fast);
}

.zoom-reset-btn:hover {
  background: var(--color-hover);
  color: var(--color-text);
}

.zoom-reset-btn:active {
  transform: scale(0.96);
}

.mermaid-zoom-close {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast), color var(--transition-fast), transform var(--transition-fast);
}

.mermaid-zoom-close:hover {
  background: var(--color-hover);
  color: var(--color-text);
}

.mermaid-zoom-close:active {
  transform: scale(0.96);
}

.mermaid-zoom-close:hover {
  background: var(--color-hover);
}

.mermaid-zoom-content {
  flex: 1;
  overflow: auto;
  padding: 20px;
}

.mermaid-zoom-spacer {
  margin: auto;
  pointer-events: none;
  position: relative;
}

.mermaid-zoom-svg {
  pointer-events: none;
}

.mermaid-zoom-svg :deep(svg) {
  display: block;
  max-width: none;
  pointer-events: none;
}

.math-dialog-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-scrim);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.math-dialog {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  max-width: 700px;
  width: 100%;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.math-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
}

.math-dialog-header h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
  color: var(--color-text);
}

.math-dialog-close {
  padding: 8px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast), color var(--transition-fast), transform var(--transition-fast);
}

.math-dialog-close:hover {
  background: var(--color-hover);
  color: var(--color-text);
}

.math-dialog-close:active {
  transform: scale(0.96);
}

.math-dialog-content {
  flex: 1;
  padding: 16px 20px;
  overflow-y: auto;
  min-height: 200px;
}

.math-view-label {
  font-size: 12px;
  color: var(--color-text-light);
  margin: 12px 0 6px;
  font-weight: 500;
}

.math-view-label:first-child {
  margin-top: 0;
}

.math-view-textarea {
  width: 100%;
  min-height: 80px;
  max-height: 200px;
  padding: 12px;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-code-bg);
  color: var(--color-text);
  resize: vertical;
  box-sizing: border-box;
  outline: none;
}

.math-view-textarea:focus {
  border-color: var(--color-primary);
}

.math-preview {
  padding: 16px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  min-height: 40px;
  overflow-x: auto;
}

.math-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border);
}

.math-dialog-cancel {
  padding: 6px 16px;
  background: var(--color-hover);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.math-dialog-cancel:hover {
  background: color-mix(in srgb, var(--color-text) 12%, var(--color-hover));
}

.math-dialog-cancel:active {
  transform: scale(0.96);
}

.math-dialog-save {
  padding: 6px 16px;
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.math-dialog-save:hover {
  background: var(--color-primary-dark);
}

.math-dialog-save:active {
  transform: scale(0.96);
}

.text :deep(.math-editable) {
  position: relative;
  display: inline-block;
  cursor: default;
}

.text :deep(div.math-editable) {
  display: block;
  margin: 12px 0;
  text-align: center;
}

.text :deep(span.math-editable) {
  vertical-align: middle;
}

.text :deep(.math-rendered-content) {
  display: inline;
}

.text :deep(div.math-editable .math-rendered-content) {
  display: block;
}

.text :deep(.math-controls) {
  display: inline-flex;
  opacity: 0;
  transition: opacity var(--transition-fast);
  margin-left: 4px;
  vertical-align: middle;
}

.text :deep(.math-editable:hover .math-controls) {
  opacity: 1;
}

.text :deep(.math-edit-btn) {
  padding: 2px 6px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-white);
  color: var(--color-text-light);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  transition: background-color var(--transition-fast), color var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast);
}

.text :deep(.math-edit-btn:hover) {
  background: var(--color-hover);
  color: var(--color-text);
  border-color: var(--color-primary);
}

.text :deep(.math-edit-btn:active) {
  transform: scale(0.96);
}

.alternating-content :deep(.math-editable) {
  position: relative;
  display: inline-block;
  cursor: default;
}

.alternating-content :deep(div.math-editable) {
  display: block;
  margin: 12px 0;
  text-align: center;
}

.alternating-content :deep(.math-rendered-content) {
  display: inline;
}

.alternating-content :deep(div.math-editable .math-rendered-content) {
  display: block;
}

.alternating-content :deep(.math-controls) {
  display: inline-flex;
  opacity: 0;
  transition: opacity var(--transition-fast);
  margin-left: 4px;
  vertical-align: middle;
}

.alternating-content :deep(.math-editable:hover .math-controls) {
  opacity: 1;
}

.alternating-content :deep(.math-edit-btn) {
  padding: 2px 6px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-white);
  color: var(--color-text-light);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  transition: background-color var(--transition-fast), color var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast);
}

.alternating-content :deep(.math-edit-btn:hover) {
  background: var(--color-hover);
  color: var(--color-text);
  border-color: var(--color-primary);
}

.alternating-content :deep(.math-edit-btn:active) {
  transform: scale(0.96);
}
</style>
