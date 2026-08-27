<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="chat-input-wrapper">
        

    <div v-if="showConfirmModal" class="voice-confirm-modal">
      <div class="modal-content">
        <div class="modal-title">语音识别结果</div>
        <div class="modal-text">{{ pendingAudioText }}</div>
        <div class="modal-actions">
          <button class="cancel-btn" @click="cancelVoiceSend">取消</button>
          <button class="confirm-btn" @click="confirmVoiceSend">发送</button>
        </div>
      </div>
    </div>

    <NotePicker
      v-if="showNotePicker"
      @selectMany="insertNoteReferences"
      @close="showNotePicker = false"
    />

    <FileUploadDialog
      v-if="showFileUpload"
      @close="showFileUpload = false"
      @uploaded="handleFileUploaded"
    />

    <!-- Drafts drawer: user's parked queries with note refs preserved -->
    <div class="drafts-panel" v-if="showDrafts">
      <div class="drafts-header">
        <span class="drafts-title">草稿箱 ({{ draftsStore.count }})</span>
        <div class="drafts-header-actions">
          <button
            v-if="inputText.trim() || referencedNotes.length > 0"
            class="drafts-save-current-btn"
            @click="saveCurrentAsDraft"
            title="将当前内容存为草稿"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
              <polyline points="17 21 17 13 7 13 7 21"/>
              <polyline points="7 3 7 8 15 8"/>
            </svg>
            存入
          </button>
          <button class="drafts-close-btn" @click="showDrafts = false" title="关闭">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="drafts-list" v-if="draftsStore.sortedDrafts.length > 0">
        <div
          v-for="draft in draftsStore.sortedDrafts"
          :key="draft.id"
          class="draft-item"
        >
          <div class="draft-preview" @click="loadDraftIntoInput(draft.id)">
            <div class="draft-refs" v-if="draft.references.length > 0">
              <span
                v-for="(ref, rIdx) in draft.references"
                :key="rIdx"
                class="draft-ref-tag"
              >📎 {{ ref.title }}</span>
            </div>
            <div class="draft-text">{{ draft.content.substring(0, 120) }}{{ draft.content.length > 120 ? '…' : '' }}</div>
            <div class="draft-meta">{{ formatDraftDate(draft.updatedAt) }}</div>
          </div>
          <div class="draft-actions">
            <button class="draft-send-btn" @click="sendDraftNow(draft.id)" :disabled="chatStore.isStreaming" title="立即发送到当前对话">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="22" y1="2" x2="11" y2="13"/>
                <polygon points="22 2 15 22 11 13 2 9 22 2"/>
              </svg>
            </button>
            <button class="draft-delete-btn" @click="draftsStore.removeDraft(draft.id)" title="删除草稿">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
              </svg>
            </button>
          </div>
        </div>
      </div>
      <div class="drafts-empty" v-else>草稿箱为空。输入内容后点击工具栏的 💾 按钮，或在此面板头部点击「存入」保存为草稿。</div>
    </div>

    <!-- Uploaded file previews (floating above input) -->
    <div v-if="uploadedFiles.length > 0" class="uploaded-files-area">
      <div v-for="(f, idx) in uploadedFiles" :key="idx" class="uploaded-file-chip">
        <span class="chip-icon">{{ getFileChipIcon(f.filename) }}</span>
        <span class="chip-name">{{ f.filename }}</span>
        <button class="chip-remove" @click="removeUploadedFile(idx)">&times;</button>
      </div>
    </div>

    <!-- Mobile voice mode: float toggle back to text (outside the overflow-clipped container) -->
    <button
      v-if="isMobile && isMobileVoiceMode"
      type="button"
      class="voice-mode-toggle-btn voice-mode-toggle-btn--float"
      :disabled="chatStore.isStreaming"
      @click="onVoiceToggleClick"
      aria-label="切换文字输入"
    >
      <svg class="toggle-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="2" y="5" width="20" height="14" rx="2"/>
        <path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M7 13h.01M17 13h.01M9 13h6"/>
      </svg>
    </button>

    <div class="chat-input-container" :class="{ 'voice-mode': isMobile && isMobileVoiceMode }">
      <!-- Voice waveform card (inside container, above input) -->
      <div v-if="showWaveformCard" class="voice-waveform-card">
        <div class="waveform-visual">
          <div class="wave-bar" v-for="i in 12" :key="i" :style="{ animationDelay: `${i * 0.08}s` }"></div>
        </div>
        <span class="waveform-status">{{ isProcessing ? '识别中...' : '录音中' }}</span>
        <button class="waveform-done-btn" @click="onWaveformDone" :disabled="isProcessing">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
          完成
        </button>
        <button class="waveform-cancel-btn" @click="onWaveformCancel" title="取消">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <div class="input-main-area">
        <!-- Note preview popup -->
        <Teleport to="body">
          <div v-if="previewingNoteIdx !== null && referencedNotes[previewingNoteIdx]" class="note-preview-overlay" @click="previewingNoteIdx = null">
            <div class="note-preview-card" @click.stop>
              <div class="note-preview-header">
                <span class="note-preview-title">{{ referencedNotes[previewingNoteIdx].title }}</span>
                <button class="note-preview-close" @click="previewingNoteIdx = null">×</button>
              </div>
              <div class="note-preview-body markdown-body" v-html="previewNoteHtml"></div>
            </div>
          </div>
        </Teleport>

        <!-- Input text area -->
        <div ref="inputAreaRef" class="input-text-area" @click="focusEditor">
          <div
            ref="editorRef"
            class="chat-input"
            :class="{ empty: !editorHasContent && !isComposing && referencedNotes.length === 0, composing: isComposing }"
            contenteditable="true"
            role="textbox"
            aria-multiline="true"
            aria-placeholder="输入消息..."
            aria-label="消息输入框"
            data-placeholder="输入消息..."
            @input="onEditorInput"
            @keyup="scrollCaretIntoView"
            @keydown.enter.exact="handleEnterKey"
            @keydown.escape="onEditorEscape"
            @compositionstart="isComposing = true"
            @compositionend="onCompositionEnd"
            @paste="onPaste"
            @click="onEditorClick"
          ></div>
        </div>

        <!-- Bottom toolbar: always-visible buttons + more menu -->
        <div class="input-bottom-toolbar">
          <button
            v-show="!isRecording"
            class="toolbar-btn note-ref-btn"
            @click="onNoteButtonClick"
            title="引用笔记"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
            <span class="toolbar-btn-label toolbar-btn-label--full">笔记引用</span>
            <span class="toolbar-btn-label toolbar-btn-label--short">笔记</span>
          </button>
          <div class="reasoning-toggle-group" ref="reasoningGroupRef">
            <button
              v-show="!isRecording"
              class="toolbar-btn reasoning-btn"
              :class="{ active: chatStore.enableReasoning }"
              @click="handleReasoningClick"
              :title="chatStore.enableReasoning ? '深度思考已开启' : '开启深度思考'"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 2a8 8 0 0 0-8 8c0 3.4 2.1 6.3 5 7.5V20h6v-2.5c2.9-1.2 5-4.1 5-7.5a8 8 0 0 0-8-8z"/>
                <line x1="9" y1="23" x2="15" y2="23"/>
              </svg>
              <span class="toolbar-btn-label toolbar-btn-label--full">思考模式</span>
              <span class="toolbar-btn-label toolbar-btn-label--short">思考</span>
            </button>
            <!-- Reasoning effort popup menu (DeepSeek: Max/High/Low/关闭; Qwen3.8(Local): xhigh/medium/low/关闭) -->
            <Teleport to="body">
              <div
                v-if="showReasoningMenu && (isDeepSeekProvider || isQwen38Provider)"
                class="reasoning-menu-overlay"
                @click="closeReasoningMenu"
              >
                <div
                  class="reasoning-menu"
                  :style="reasoningMenuStyle"
                  @click.stop
                >
                  <div class="reasoning-menu-title">思考模式</div>
                  <template v-if="isDeepSeekProvider">
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: chatStore.enableReasoning && chatStore.reasoningEffort === 'max' }"
                    @click="selectReasoningEffort('max')"
                  >
                    <span class="reasoning-menu-label">Max</span>
                    <span class="reasoning-menu-desc">最大深度思考</span>
                  </button>
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: chatStore.enableReasoning && (chatStore.reasoningEffort === 'high' || !chatStore.reasoningEffort) }"
                    @click="selectReasoningEffort('high')"
                  >
                    <span class="reasoning-menu-label">High</span>
                    <span class="reasoning-menu-desc">标准深度思考</span>
                  </button>
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: chatStore.enableReasoning && chatStore.reasoningEffort === 'low' }"
                    @click="selectReasoningEffort('low')"
                  >
                    <span class="reasoning-menu-label">Low</span>
                    <span class="reasoning-menu-desc">轻量快速推理</span>
                  </button>
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: !chatStore.enableReasoning }"
                    @click="disableReasoning()"
                  >
                    <span class="reasoning-menu-label">关闭</span>
                    <span class="reasoning-menu-desc">不使用深度思考</span>
                  </button>
                  </template>
                  <template v-if="isQwen38Provider">
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: chatStore.enableReasoning && chatStore.reasoningEffort === 'xhigh' }"
                    @click="selectReasoningEffort('xhigh')"
                  >
                    <span class="reasoning-menu-label">xhigh</span>
                    <span class="reasoning-menu-desc">最强推理深度</span>
                  </button>
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: chatStore.enableReasoning && chatStore.reasoningEffort === 'medium' }"
                    @click="selectReasoningEffort('medium')"
                  >
                    <span class="reasoning-menu-label">medium</span>
                    <span class="reasoning-menu-desc">均衡速度与深度</span>
                  </button>
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: chatStore.enableReasoning && chatStore.reasoningEffort === 'low' }"
                    @click="selectReasoningEffort('low')"
                  >
                    <span class="reasoning-menu-label">low</span>
                    <span class="reasoning-menu-desc">轻量快速推理</span>
                  </button>
                  <button
                    class="reasoning-menu-item"
                    :class="{ active: !chatStore.enableReasoning }"
                    @click="disableReasoning()"
                  >
                    <span class="reasoning-menu-label">关闭</span>
                    <span class="reasoning-menu-desc">不使用深度思考</span>
                  </button>
                  </template>
                </div>
              </div>
            </Teleport>
          </div>
          <!-- More menu (after thinking mode) -->
          <div class="more-menu-wrapper" ref="moreMenuRef">
            <button
              class="toolbar-btn more-btn"
              :class="{ active: showMoreMenu }"
              @click="toggleMoreMenu"
              title="更多功能"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <circle cx="12" cy="5" r="2"/>
                <circle cx="12" cy="12" r="2"/>
                <circle cx="12" cy="19" r="2"/>
              </svg>
              <span class="toolbar-btn-label">更多</span>
            </button>
          </div>
          <div class="toolbar-spacer"></div>
          <Teleport to="body">
            <div v-if="showMoreMenu" class="more-dropdown-teleport" :style="moreMenuStyle" @click.stop>
              <button class="more-dropdown-item" @click="showFileUpload = true; showMoreMenu = false">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
                </svg>
                <span>上传文件</span>
              </button>
              <button class="more-dropdown-item" :class="{ active: showDrafts }" @click="showDrafts = !showDrafts; showMoreMenu = false">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M22 12h-6l-2 3h-4l-2-3H2"/>
                  <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
                </svg>
                <span>草稿箱{{ draftsStore.count > 0 ? ` (${draftsStore.count})` : '' }}</span>
              </button>
              <button class="more-dropdown-item" :class="{ active: chatStore.deathmatchMode }" @click="handleDeathmatchClick(); showMoreMenu = false">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M15 12l-8.5 8.5c-.83.83-2.17.83-3 0 0 0 0 0 0 0a2.12 2.12 0 0 1 0-3L12 9"/>
                  <path d="M17.64 15L22 10.64"/>
                  <path d="M20.91 11.7l-1.25-1.25c-.6-.6-.93-1.4-.93-2.25V6.5a.5.5 0 0 0-.5-.5H16.5c-.85 0-1.65-.33-2.25-.93L13 3.82"/>
                  <path d="M13.04 3.82l1.77-1.77a2 2 0 0 1 2.83 0L21.8 6.21a2 2 0 0 1 0 2.83l-1.77 1.77"/>
                </svg>
                <span>死磕模式</span>
              </button>
            </div>
          </Teleport>
          <Teleport to="body">
            <div v-if="showSkillPopup" class="skill-popup" :style="skillPopupStyle" @mousedown.prevent @click.stop>
              <div class="skill-popup-header">
                <svg class="skill-popup-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
                </svg>
                <span class="skill-popup-title">技能</span>
              </div>
              <div v-if="filteredSkills.length > 0" class="skill-popup-list">
                <button
                  v-for="(skill, index) in filteredSkills"
                  :key="skill.id"
                  class="skill-popup-item"
                  :class="{ active: index === selectedSkillIndex, 'system-skill': skill.source === 'system' }"
                  @click="selectSkill(skill)"
                  @mouseenter="selectedSkillIndex = index"
                >
                  <span class="skill-item-name">
                    {{ skill.name }}
                    <span v-if="skill.source === 'system'" class="skill-source-badge">系统</span>
                  </span>
                  <span v-if="skill.description" class="skill-item-desc">{{ skill.description }}</span>
                </button>
              </div>
              <div v-else class="skill-popup-empty">无匹配技能</div>
            </div>
          </Teleport>
          <button
          v-show="isMobile || (!isRecording && !isProcessing)"
          class="voice-mode-toggle-btn"
          :class="{ active: isMobile && isMobileVoiceMode }"
          :disabled="chatStore.isStreaming"
          @click="onVoiceToggleClick"
          aria-label="语音输入"
        >
      <svg class="toggle-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
        <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
        <line x1="12" y1="19" x2="12" y2="23"/>
        <line x1="8" y1="23" x2="16" y2="23"/>
      </svg>
    </button>
          <button
            class="send-btn"
            :class="{ 'stop-mode': chatStore.isStreaming }"
            :disabled="!chatStore.isStreaming && !inputText.trim() && uploadedFiles.length === 0"
            @click="chatStore.isStreaming ? handleStopStreaming() : handleSend()"
            :aria-label="chatStore.isStreaming ? '停止生成' : '发送'"
          >
            <svg class="send-icon" :class="{ hidden: chatStore.isStreaming }" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="22" y1="2" x2="11" y2="13"/>
              <polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
            <svg class="stop-icon" :class="{ hidden: !chatStore.isStreaming }" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="6" y="6" width="12" height="12" rx="2"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Mobile press-to-record overlay -->
      <div v-if="isMobile && isMobileVoiceMode" class="mobile-voice-overlay">
        <div
          class="mobile-voice-bar voice-record-area"
          :class="{ recording: isRecording, processing: isProcessing, 'cancel-hint': isSwipeCancelHint }"
          @touchstart.prevent="onRecordTouchStart"
          @touchcancel="cancelMobileRecording"
          @click.prevent
          @mousedown.prevent
          @mouseup.prevent
        >
          <span v-if="isSwipeCancelHint">松开 取消</span>
          <span v-else>{{ isRecording ? '松开发送 上滑取消' : isProcessing ? '识别中...' : '按住 说话' }}</span>
          <div v-if="isSwipeCancelHint" class="swipe-cancel-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </div>
        </div>
      </div>

    </div>
    <div class="input-hint">
      <span v-if="isMobile">点击发送按钮发送，Enter 换行</span>
      <span v-else>按 Enter 发送，Shift + Enter 换行</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useAssistantStore } from '@/stores/assistant'
import { useDraftsStore } from '@/stores/drafts'
import { notesApi } from '@/api/notes'
import { renderMarkdownToHtml } from '@/composables/useMarkdown'
import { useAsrStreaming } from '@/composables/useAsrStreaming'
import { useAsrHotwords } from '@/composables/useAsrHotwords'
import { useToast } from '@/composables/useToast'
import { useMediaQuery } from '@/composables/useMediaQuery'
import NotePicker from './NotePicker.vue'
import FileUploadDialog from './FileUploadDialog.vue'
import { skillsApi } from '@/api/skills'
import type { Skill } from '@/types'
import type { FileParseResult } from '@/api/fileUpload'

const chatStore = useChatStore()
const assistantStore = useAssistantStore()
const draftsStore = useDraftsStore()
const { show: showToast } = useToast()
const { hotwords: asrHotwords } = useAsrHotwords()
const asrStreaming = useAsrStreaming({
  customHotwords: () => asrHotwords.value,
  onPartial(payload) {
    if (!payload.text) return
    inputText.value = payload.text
    editorHasContent.value = !!inputText.value.length
    if (editorRef.value) editorRef.value.textContent = payload.text
  },
})

const inputText = ref('')
const editorRef = ref<HTMLDivElement | null>(null)
const isComposing = ref(false)
const editorHasContent = ref(false)
const DRAFT_KEY = 'chatllm_draft_input'

// Note reference tags
interface NoteRef {
  id: string
  title: string
  content: string
}
const referencedNotes = ref<NoteRef[]>([])
const previewingNoteIdx = ref<number | null>(null)
const editMessageId = ref<string | null>(null)

const previewNoteHtml = computed(() => {
  if (previewingNoteIdx.value === null) return ''
  const note = referencedNotes.value[previewingNoteIdx.value]
  if (!note) return ''
  // Route through the shared render pipeline (DOMPurify + normalize) so the
  // preview cannot inject raw HTML and renders markdown like the editor.
  return renderMarkdownToHtml(note.content)
})

// Restore draft from sessionStorage
const savedDraft = sessionStorage.getItem(DRAFT_KEY)
if (savedDraft) {
  inputText.value = savedDraft
}

// Persist draft on change
watch(inputText, (val) => {
  if (val) {
    sessionStorage.setItem(DRAFT_KEY, val)
  } else {
    sessionStorage.removeItem(DRAFT_KEY)
  }
})

const isRecording = asrStreaming.isRecording
const isProcessing = computed(() => asrStreaming.isFinishing.value)
const showWaveformCard = computed(() => (isRecording.value || isProcessing.value) && !(isMobile.value && isMobileVoiceMode.value))
const showConfirmModal = ref(false)
const pendingAudioText = ref('')
const { isMobile } = useMediaQuery()
const isMobileVoiceMode = ref(false)
const showNotePicker = ref(false)
const showDrafts = ref(false)
const showFileUpload = ref(false)
const uploadedFiles = ref<FileParseResult[]>([])

const isDeepSeekProvider = computed(() => {
  const assistantId = assistantStore.currentAssistantId
  if (!assistantId) return true
  const assistant = assistantStore.getAssistantById(assistantId)
  return !assistant?.provider_type || assistant.provider_type === 'deepseek'
})

const isQwen38Provider = computed(() => {
  const assistantId = assistantStore.currentAssistantId
  if (!assistantId) return false
  const assistant = assistantStore.getAssistantById(assistantId)
  return assistant?.provider_type === 'qwen3.8_vllm'
})

const showReasoningMenu = ref(false)
const reasoningGroupRef = ref<HTMLDivElement | null>(null)
const reasoningMenuStyle = ref<Record<string, string>>({})
const showMoreMenu = ref(false)
const moreMenuRef = ref<HTMLDivElement | null>(null)

const showSkillPopup = ref(false)
const allSkills = ref<Skill[]>([])
const filteredSkills = ref<Skill[]>([])
const selectedSkillIndex = ref(0)
const skillSearchQuery = ref('')
const skillPopupStyle = ref<Record<string, string>>({})
const inputAreaRef = ref<HTMLDivElement | null>(null)
let skillPopupDismissed = false

const moreMenuStyle = computed(() => {
  if (!showMoreMenu.value || !moreMenuRef.value) return {}
  const rect = moreMenuRef.value.getBoundingClientRect()
  return {
    position: 'fixed' as const,
    bottom: `${window.innerHeight - rect.top + 6}px`,
    left: `${rect.left}px`,
    zIndex: '10000',
  }
})

function toggleMoreMenu() {
  showMoreMenu.value = !showMoreMenu.value
}

function closeMoreMenu() {
  showMoreMenu.value = false
}

function computeSkillPopupPosition() {
  if (!inputAreaRef.value) return
  const rect = inputAreaRef.value.getBoundingClientRect()
  skillPopupStyle.value = {
    position: 'fixed' as const,
    bottom: `${window.innerHeight - rect.top + 4}px`,
    left: `${rect.left + 12}px`,
    zIndex: '10000',
  }
}

async function openSkillPopup() {
  try {
    allSkills.value = (await skillsApi.getSkills()).filter(s => s.is_active)
  } catch {
    allSkills.value = []
  }
  filterSkills()
  computeSkillPopupPosition()
  showSkillPopup.value = true
}

function filterSkills() {
  const q = skillSearchQuery.value.toLowerCase()
  filteredSkills.value = q
    ? allSkills.value.filter(s => s.name.toLowerCase().includes(q) || (s.description && s.description.toLowerCase().includes(q)))
    : [...allSkills.value]
  selectedSkillIndex.value = 0
}

function closeSkillPopup() {
  showSkillPopup.value = false
  skillSearchQuery.value = ''
  selectedSkillIndex.value = 0
  skillPopupDismissed = true
  setTimeout(() => { skillPopupDismissed = false }, 300)
}

function selectSkill(skill: Skill) {
  if (!editorRef.value) return
  editorRef.value.textContent = `/${skill.name} `
  syncInputText()
  editorRef.value.focus()
  const range = document.createRange()
  range.selectNodeContents(editorRef.value)
  range.collapse(false)
  const sel = window.getSelection()
  if (sel) {
    sel.removeAllRanges()
    sel.addRange(range)
  }
  closeSkillPopup()
}

function handleSkillPopupKeydown(e: KeyboardEvent) {
  if (!showSkillPopup.value || filteredSkills.value.length === 0) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    selectedSkillIndex.value = (selectedSkillIndex.value + 1) % filteredSkills.value.length
    nextTick(() => {
      const active = document.querySelector('.skill-popup-item.active')
      active?.scrollIntoView({ block: 'nearest' })
    })
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    selectedSkillIndex.value = (selectedSkillIndex.value - 1 + filteredSkills.value.length) % filteredSkills.value.length
    nextTick(() => {
      const active = document.querySelector('.skill-popup-item.active')
      active?.scrollIntoView({ block: 'nearest' })
    })
  } else if (e.key === 'Enter') {
    e.preventDefault()
    selectSkill(filteredSkills.value[selectedSkillIndex.value])
  } else if (e.key === 'Escape') {
    e.preventDefault()
    closeSkillPopup()
  }
}

function handleReasoningClick() {
  if (isQwen38Provider.value || isDeepSeekProvider.value) {
    openReasoningMenu()
    return
  }
  chatStore.enableReasoning = !chatStore.enableReasoning
}

function handleDeathmatchClick() {
  if (chatStore.deathmatchMode) {
    chatStore.deathmatchMode = false
    chatStore.deathmatchAction = 'stop'
  } else {
    chatStore.deathmatchMode = true
    chatStore.deathmatchAction = 'start'
  }
}

function openReasoningMenu() {
  if (!reasoningGroupRef.value) return
  const rect = reasoningGroupRef.value.getBoundingClientRect()
  reasoningMenuStyle.value = {
    position: 'fixed',
    left: `${rect.left}px`,
    bottom: `${window.innerHeight - rect.top + 8}px`,
    zIndex: '10000',
  }
  showReasoningMenu.value = true
}

function closeReasoningMenu() {
  showReasoningMenu.value = false
}

function selectReasoningEffort(effort: 'high' | 'max' | 'xhigh' | 'medium' | 'low') {
  chatStore.reasoningEffort = effort
  chatStore.enableReasoning = true
  showReasoningMenu.value = false
}

function disableReasoning() {
  chatStore.enableReasoning = false
  chatStore.reasoningEffort = null
  showReasoningMenu.value = false
}

watch(isMobile, (val) => {
  if (!val && isMobileVoiceMode.value) {
    isMobileVoiceMode.value = false
    document.body.classList.remove('voice-mode-active')
  }
})

watch(() => assistantStore.currentAssistantId, () => {
  chatStore.enableReasoning = false
  chatStore.reasoningEffort = null
  showReasoningMenu.value = false
})

async function handleKeyDown(e: KeyboardEvent) {
  if (showSkillPopup.value) {
    handleSkillPopupKeydown(e)
    return
  }
  if (e.key === 'Escape' && isRecording.value) {
    await stopRecording(true)
  }
  if (e.key === 'Escape' && showMoreMenu.value) {
    showMoreMenu.value = false
  }
}

async function stopRecording(cancel: boolean = false) {
  if (!isRecording.value && !isProcessing.value) {
    return
  }

  if (cancel) {
    asrStreaming.cancel()
    return
  }

  try {
    const result = await asrStreaming.stop()
    const transcript = result?.text?.trim()
    if (!transcript) {
      return
    }

    inputText.value = transcript
    editorHasContent.value = !!transcript.length
    if (editorRef.value) editorRef.value.textContent = transcript
    nextTick(() => editorRef.value?.focus())
  } catch (error) {
    if (error instanceof Error && error.message === '录音已取消') {
      return
    }
    console.error('ASR failed:', error)
    const msg = error instanceof Error ? error.message : '语音识别失败'
    showToast(msg, 'error')
  }
}

function confirmVoiceSend() {
  // kept for backward compatibility, not used in new flow
}

function cancelVoiceSend() {
  // kept for backward compatibility, not used in new flow
}

async function insertNoteReferences(notes: Array<{ id: string; title: string | null; content_preview: string }>) {
  // Filter out already-referenced notes
  const newNotes = notes.filter(n => !referencedNotes.value.some(r => r.id === n.id))
  if (!newNotes.length) return

  // Fetch full content for each note in parallel
  const noteRefs: NoteRef[] = await Promise.all(newNotes.map(async (note) => {
    try {
      const fullNote = await notesApi.getNote(note.id)
      return { id: fullNote.id, title: fullNote.title || '笔记', content: fullNote.content || note.content_preview || '' }
    } catch {
      return { id: note.id, title: note.title || '笔记', content: note.content_preview || '' }
    }
  }))

  for (const noteRef of noteRefs) {
    referencedNotes.value.push(noteRef)
    if (editorRef.value) {
      const tagEl = createNoteTagElement(noteRef)
      if (savedRange && editorRef.value.contains(savedRange.startContainer)) {
        savedRange.insertNode(tagEl)
        const spacer = document.createTextNode('\u200B')
        tagEl.after(spacer)
        const newRange = document.createRange()
        newRange.setStart(spacer, 1)
        newRange.collapse(true)
        const sel = window.getSelection()
        sel?.removeAllRanges()
        sel?.addRange(newRange)
        savedRange = newRange
      } else {
        editorRef.value.appendChild(tagEl)
        const spacer = document.createTextNode('\u200B')
        editorRef.value.appendChild(spacer)
      }
    }
  }

  savedRange = null
  if (editorRef.value) {
    editorRef.value.focus()
    syncInputText()
  }
}

function removeNoteRef(index: number) {
  referencedNotes.value.splice(index, 1)
  if (previewingNoteIdx.value === index) {
    previewingNoteIdx.value = null
  } else if (previewingNoteIdx.value !== null && previewingNoteIdx.value > index) {
    previewingNoteIdx.value--
  }
}

let savedRange: Range | null = null

function onNoteButtonClick() {
  const sel = window.getSelection()
  if (sel && sel.rangeCount > 0 && editorRef.value?.contains(sel.anchorNode)) {
    savedRange = sel.getRangeAt(0).cloneRange()
  } else {
    savedRange = null
  }
  showNotePicker.value = true
}

function createNoteTagElement(noteRef: NoteRef): HTMLSpanElement {
  const tag = document.createElement('span')
  tag.className = 'note-tag'
  tag.contentEditable = 'false'
  tag.dataset.noteId = noteRef.id
  tag.title = noteRef.title

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('width', '10')
  svg.setAttribute('height', '10')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '2')
  const path1 = document.createElementNS('http://www.w3.org/2000/svg', 'path')
  path1.setAttribute('d', 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20')
  const path2 = document.createElementNS('http://www.w3.org/2000/svg', 'path')
  path2.setAttribute('d', 'M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z')
  svg.appendChild(path1)
  svg.appendChild(path2)
  tag.appendChild(svg)

  const label = document.createElement('span')
  label.className = 'note-tag-label'
  label.textContent = noteRef.title.slice(0, 2)
  tag.appendChild(label)

  const removeBtn = document.createElement('button')
  removeBtn.className = 'note-tag-remove'
  removeBtn.textContent = '\u00d7'
  tag.appendChild(removeBtn)

  return tag
}

function onEditorClick(e: MouseEvent) {
  const target = e.target as HTMLElement

  // Handle remove button click
  if (target.classList.contains('note-tag-remove')) {
    e.preventDefault()
    e.stopPropagation()
    const tagEl = target.closest('.note-tag') as HTMLElement
    if (tagEl && tagEl.dataset.noteId) {
      const idx = referencedNotes.value.findIndex(n => n.id === tagEl.dataset.noteId)
      if (idx !== -1) {
        referencedNotes.value.splice(idx, 1)
        previewingNoteIdx.value = null
      }
      tagEl.remove()
      syncInputText()
    }
    return
  }

  // Handle tag click (preview toggle)
  const tagEl = target.closest('.note-tag') as HTMLElement | null
  if (tagEl && tagEl.dataset.noteId) {
    e.stopPropagation()
    const idx = referencedNotes.value.findIndex(n => n.id === tagEl.dataset.noteId)
    if (idx !== -1) {
      previewingNoteIdx.value = previewingNoteIdx.value === idx ? null : idx
    }
  }
}

async function onMicClick() {
  if (isProcessing.value || chatStore.isStreaming) return
  try {
    await asrStreaming.start({ chunk_size_sec: 0.5 })
  } catch (error) {
    console.error('Failed to start recording:', error)
  }
}

async function onWaveformDone() {
  if (!isRecording.value) return
  await stopRecording(false)
}

async function onWaveformCancel() {
  if (isRecording.value) {
    await stopRecording(true)
  } else if (isProcessing.value) {
    asrStreaming.cancel()
  }
}

function onVoiceToggleClick() {
  if (isMobile.value) {
    if (isRecording.value || isProcessing.value) {
      asrStreaming.cancel()
    }
    isMobileVoiceMode.value = !isMobileVoiceMode.value
    if (isMobileVoiceMode.value) {
      document.body.classList.add('voice-mode-active')
    } else {
      document.body.classList.remove('voice-mode-active')
    }
    return
  }
  onMicClick()
}

const recordTouchStartY = ref(0)
const isSwipeCancelHint = ref(false)
const SWIPE_CANCEL_THRESHOLD = 60
let docTouchMoveHandler: ((e: TouchEvent) => void) | null = null
let docTouchEndHandler: ((e: TouchEvent) => void) | null = null
let lastCancelTimestamp = 0

function removeDocTouchListeners() {
  if (docTouchMoveHandler) {
    document.removeEventListener('touchmove', docTouchMoveHandler)
    docTouchMoveHandler = null
  }
  if (docTouchEndHandler) {
    document.removeEventListener('touchend', docTouchEndHandler)
    docTouchEndHandler = null
  }
}

function onRecordTouchStart(e: Event) {
  const te = e as TouchEvent
  recordTouchStartY.value = te.touches[0].clientY
  isSwipeCancelHint.value = false
  startMobileRecording(e)

  removeDocTouchListeners()

  docTouchMoveHandler = (ev: TouchEvent) => {
    if (!isRecording.value) return
    const deltaY = recordTouchStartY.value - ev.touches[0].clientY
    isSwipeCancelHint.value = deltaY > SWIPE_CANCEL_THRESHOLD
  }

  docTouchEndHandler = (ev: TouchEvent) => {
    if (isSwipeCancelHint.value) {
      isSwipeCancelHint.value = false
      if (isRecording.value || isProcessing.value) {
        asrStreaming.cancel()
      }
      lastCancelTimestamp = Date.now()
    } else {
      isSwipeCancelHint.value = false
      stopMobileRecording(ev)
    }
    removeDocTouchListeners()
  }

  document.addEventListener('touchmove', docTouchMoveHandler, { passive: false })
  document.addEventListener('touchend', docTouchEndHandler)
}

async function startMobileRecording(e: Event) {
  e.preventDefault()
  if (isProcessing.value || chatStore.isStreaming) return
  try {
    await asrStreaming.start({ chunk_size_sec: 0.5 })
  } catch (err) {
    console.error('Failed to start recording:', err)
    if (!asrStreaming.error.value) {
      showToast(err instanceof Error ? err.message : '录音启动失败', 'error')
    }
  }
}

async function stopMobileRecording(e?: Event) {
  e?.preventDefault()
  removeDocTouchListeners()
  if (Date.now() - lastCancelTimestamp < 500) return
  if (!isRecording.value && !isProcessing.value) {
    return
  }
  try {
    const result = await asrStreaming.stop()
    const transcript = result?.text?.trim()
    if (transcript) {
      inputText.value = transcript
      editorHasContent.value = !!transcript.length
      if (editorRef.value) editorRef.value.textContent = transcript
      await handleSend()
    }
  } catch (error) {
    if (error instanceof Error && error.message === '录音已取消') {
      return
    }
    console.error('ASR failed:', error)
    const msg = error instanceof Error ? error.message : '语音识别失败'
    showToast(msg, 'error')
  }
}

function cancelMobileRecording(e?: Event) {
  e?.preventDefault()
  removeDocTouchListeners()
  if (isRecording.value || isProcessing.value) {
    asrStreaming.cancel()
  }
}

async function handleSend() {
  // Flush the debounced input sync synchronously: `inputText` is normally
  // updated 80ms after the last keystroke, so typing then pressing Enter
  // within that window would read a stale (empty) value and silently drop
  // the message (repro 2026-08-03: keyboard.type + immediate Enter → no
  // POST /api/chat/stream fired).
  syncInputText()
  const text = inputText.value.trim()
  if ((!text && uploadedFiles.value.length === 0) || chatStore.isStreaming) return

  let fullContent = ''
  if (referencedNotes.value.length > 0) {
    for (const noteRef of referencedNotes.value) {
      fullContent += `[note-ref:${noteRef.id}|${noteRef.title}]\n${noteRef.content}\n[/note-ref]\n\n`
    }
  }
  if (uploadedFiles.value.length > 0) {
    for (const f of uploadedFiles.value) {
      if (f.file_path) {
        const sizeStr = f.size
          ? f.size < 1024
            ? `${f.size} B`
            : f.size < 1048576
              ? `${(f.size / 1024).toFixed(1)} KB`
              : `${(f.size / 1048576).toFixed(1)} MB`
          : '未知'
        fullContent += `[file-ref:${f.filename}]\n文件路径: ${f.file_path}\n文件类型: ${f.file_type || 'unknown'}\n文件大小: ${sizeStr}\n[/file-ref]\n\n`
      }
    }
  }

  const skillCommandMatch = text.match(/^\/([a-zA-Z0-9_-]+)\s*(.*)/s)
  if (skillCommandMatch) {
    const skillName = skillCommandMatch[1]
    const skillArgs = skillCommandMatch[2] || ''
    fullContent += `[skill:${skillName}]\n${skillArgs}`
  } else {
    fullContent += text
  }

  const currentEditMessageId = editMessageId.value
  inputText.value = ''
  if (editorRef.value) editorRef.value.textContent = ''
  editorHasContent.value = false
  referencedNotes.value = []
  previewingNoteIdx.value = null
  editMessageId.value = null
  uploadedFiles.value = []
  sessionStorage.removeItem(DRAFT_KEY)

  if (currentEditMessageId) {
    await chatStore.editAndResendMessage(currentEditMessageId, fullContent, assistantStore.currentAssistantId)
  } else {
    await chatStore.sendMessage(fullContent, assistantStore.currentAssistantId)
  }
  emit('sent')
}

async function handleStopStreaming() {
  await chatStore.stopStreaming()
}

function saveCurrentAsDraft() {
  const text = inputText.value.trim()
  if (!text && referencedNotes.value.length === 0) return
  draftsStore.addDraft({
    content: text,
    references: referencedNotes.value.map(r => ({ id: r.id, title: r.title, content: r.content })),
    conversationId: chatStore.currentConversationId,
    assistantId: assistantStore.currentAssistantId,
  })
  inputText.value = ''
  if (editorRef.value) editorRef.value.textContent = ''
  editorHasContent.value = false
  referencedNotes.value = []
  previewingNoteIdx.value = null
  sessionStorage.removeItem(DRAFT_KEY)
  showToast('已保存到草稿箱')
}

function handleFileUploaded(results: FileParseResult[], _saveToNotebook: boolean) {
  showFileUpload.value = false
  const successful = results.filter(r => r.success && r.file_path)
  if (successful.length > 0) {
    uploadedFiles.value = [...uploadedFiles.value, ...successful]
  }
}

function removeUploadedFile(idx: number) {
  uploadedFiles.value.splice(idx, 1)
}

function getFileChipIcon(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const icons: Record<string, string> = {
    docx: '📝', doc: '📝', pptx: '📊', ppt: '📊', xlsx: '📊', xls: '📊', csv: '📊', pdf: '📄',
  }
  return icons[ext] || '📎'
}

function loadDraftIntoInput(draftId: string) {
  const draft = draftsStore.getDraft(draftId)
  if (!draft) return
  inputText.value = draft.content
  if (editorRef.value) editorRef.value.textContent = draft.content
  editorHasContent.value = !!draft.content.length
  referencedNotes.value = draft.references.map(r => ({ ...r }))
  showDrafts.value = false
  draftsStore.removeDraft(draftId)
  nextTick(() => editorRef.value?.focus())
}

async function sendDraftNow(draftId: string) {
  const draft = draftsStore.getDraft(draftId)
  if (!draft || chatStore.isStreaming) return
  let fullContent = ''
  for (const noteRef of draft.references) {
    fullContent += `[note-ref:${noteRef.id}|${noteRef.title}]\n${noteRef.content}\n[/note-ref]\n\n`
  }
  fullContent += draft.content
  draftsStore.removeDraft(draftId)
  showDrafts.value = false
  await chatStore.sendMessage(fullContent, assistantStore.currentAssistantId)
  emit('sent')
}

function formatDraftDate(iso: string): string {
  try {
    const d = new Date(iso)
    const now = new Date()
    const diffMs = now.getTime() - d.getTime()
    const diffMin = Math.floor(diffMs / 60000)
    if (diffMin < 1) return '刚刚'
    if (diffMin < 60) return `${diffMin}分钟前`
    const diffHr = Math.floor(diffMin / 60)
    if (diffHr < 24) return `${diffHr}小时前`
    return d.toLocaleDateString() + ' ' + d.toTimeString().slice(0, 5)
  } catch {
    return iso
  }
}

function syncInputText() {
  if (editorRef.value) {
    const clone = editorRef.value.cloneNode(true) as HTMLElement
    clone.querySelectorAll('.note-tag').forEach(el => el.remove())
    // Strip zero-width spaces used for cursor anchoring
    inputText.value = (clone.innerText || '').replace(/\u200B/g, '')
    // Placeholder visibility must track the DOM synchronously: `inputText`
    // is debounced (80ms), so binding the `.empty` class to it makes the
    // "输入消息..." placeholder linger next to freshly typed English text
    // until the debounce fires (IME composition hides it via .composing,
    // which is why Chinese input never showed the flicker).
    editorHasContent.value = !!inputText.value.length
    // Sync referencedNotes with tags still in DOM (handles browser-initiated deletions)
    const tagIds = new Set(
      Array.from(editorRef.value.querySelectorAll('.note-tag'))
        .map(el => (el as HTMLElement).dataset.noteId)
    )
    if (referencedNotes.value.some(n => !tagIds.has(n.id))) {
      referencedNotes.value = referencedNotes.value.filter(n => tagIds.has(n.id))
      previewingNoteIdx.value = null
    }
  }
}

function getEditorText(): string {
  if (!editorRef.value) return inputText.value
  const clone = editorRef.value.cloneNode(true) as HTMLElement
  clone.querySelectorAll('.note-tag').forEach(el => el.remove())
  return (clone.innerText || '').replace(/\u200B/g, '')
}

let _syncInputDebounceTimer: ReturnType<typeof setTimeout> | null = null

function debouncedSyncInputText() {
  if (_syncInputDebounceTimer) {
    clearTimeout(_syncInputDebounceTimer)
  }
  _syncInputDebounceTimer = setTimeout(() => {
    syncInputText()
    _syncInputDebounceTimer = null
  }, 80)
}

function scrollCaretIntoView() {
  const area = inputAreaRef.value
  if (!area) return
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return
  const range = sel.getRangeAt(0)
  // 仅当选区在输入区内才跟随，防止外部选区误滚
  if (!area.contains(range.commonAncestorContainer)) return
  // 光标矩形：折叠选区优先取 getClientRects 末段（行尾/换行处），兜底用包围矩形
  const rects = range.getClientRects()
  let caretRect: DOMRect | null = rects.length ? rects[rects.length - 1] : null
  if (!caretRect || (caretRect.height === 0 && caretRect.width === 0 && caretRect.top === 0)) {
    const r = range.getBoundingClientRect()
    caretRect = r.height || r.top ? r : null
  }
  if (!caretRect) return
  const areaRect = area.getBoundingClientRect()
  const margin = 8
  if (caretRect.bottom > areaRect.bottom - margin) {
    area.scrollTop += caretRect.bottom - (areaRect.bottom - margin)
  } else if (caretRect.top < areaRect.top + margin) {
    area.scrollTop -= areaRect.top + margin - caretRect.top
  }
}

function onEditorInput() {
  // Caret-following: with the constant-height scrollable text area, keep the
  // caret visible on every edit (native caret-scroll-into-view is unreliable
  // for a contenteditable flex child).
  scrollCaretIntoView()
  // Synchronous DOM-derived state for the placeholder class — see the
  // syncInputText comment (debounced inputText must not gate `.empty`).
  const text = getEditorText()
  editorHasContent.value = !!text.length
  if (!isComposing.value) {
    debouncedSyncInputText()
    if (text.startsWith('/') && !skillPopupDismissed) {
      const query = text.slice(1)
      if (query.length <= 30 && !query.includes(' ')) {
        skillSearchQuery.value = query
        if (!showSkillPopup.value) {
          openSkillPopup()
        } else {
          filterSkills()
          computeSkillPopupPosition()
        }
        return
      }
    }
    if (showSkillPopup.value) {
      closeSkillPopup()
    }
  }
}

function onCompositionEnd() {
  isComposing.value = false
  syncInputText()
}

function onPaste(e: ClipboardEvent) {
  e.preventDefault()
  const text = e.clipboardData?.getData('text/plain') || ''
  document.execCommand('insertText', false, text)
}

function focusEditor(e?: MouseEvent) {
  if (!editorRef.value) return
  // If click originated inside contenteditable, let native cursor positioning work
  if (e && editorRef.value.contains(e.target as Node)) return
  editorRef.value.focus()
  // Place cursor at end
  const sel = window.getSelection()
  if (sel) {
    sel.selectAllChildren(editorRef.value)
    sel.collapseToEnd()
  }
}

function handleEnterKey(e: KeyboardEvent) {
  if (isComposing.value) return
  if (showSkillPopup.value) {
    e.preventDefault()
    if (filteredSkills.value.length > 0) {
      selectSkill(filteredSkills.value[selectedSkillIndex.value])
    }
    return
  }
  if (!isMobile.value) {
    e.preventDefault()
    handleSend()
  }
}

function onEditorEscape() {
  if (showSkillPopup.value) {
    closeSkillPopup()
  }
}

onMounted(() => {
  document.addEventListener('keydown', handleKeyDown)
    document.addEventListener('click', handleOutsideClick)
  if (editorRef.value && inputText.value) {
    editorRef.value.textContent = inputText.value
    // Draft restored into the DOM — the placeholder class binds to
    // editorHasContent (synchronous), not inputText (debounced), so it
    // must be set here or the "输入消息..." placeholder would sit next to
    // the restored draft until the next keystroke (A4.9 review finding).
    editorHasContent.value = true
  }
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeyDown)
    document.removeEventListener('click', handleOutsideClick)
  document.body.classList.remove('voice-mode-active')
  removeDocTouchListeners()
  if (isRecording.value || isProcessing.value) {
    asrStreaming.cancel()
  }
})

function handleOutsideClick(e: MouseEvent) {
  if (showSkillPopup.value) {
    const target = e.target as HTMLElement
    if (!target.closest('.skill-popup')) {
      closeSkillPopup()
    }
  }
  if (showMoreMenu.value) {
    const target = e.target as HTMLElement
    if (moreMenuRef.value && moreMenuRef.value.contains(target)) return
    if (target.closest('.more-dropdown-teleport')) return
    showMoreMenu.value = false
  }
}

function setEditContent(text: string, messageId: string) {
  inputText.value = text
  editorHasContent.value = !!text.length
  editMessageId.value = messageId
  if (editorRef.value) {
    editorRef.value.textContent = text
    editorRef.value.focus()
    // Place cursor at end
    const range = document.createRange()
    range.selectNodeContents(editorRef.value)
    range.collapse(false)
    const sel = window.getSelection()
    if (sel) {
      sel.removeAllRanges()
      sel.addRange(range)
    }
  }
}

const emit = defineEmits<{ (e: 'sent'): void }>()

defineExpose({ setEditContent })
</script>

<style scoped>
.chat-input-wrapper {
  flex-shrink: 0;
  padding: 12px 20px calc(16px + env(safe-area-inset-bottom, 0));
  background: transparent;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  overflow: visible;
  position: relative;
}

.chat-input-container {
  display: flex;
  flex-direction: column;
  background: var(--surface-input);
  border: 1px solid var(--input-container-border);
  border-radius: var(--input-container-radius);
  padding: 0;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  height: 20vh;
  min-height: var(--input-container-min-height);
  flex: none;
  overflow: hidden;
}

.chat-input-container:focus-within {
  border-color: rgba(122, 163, 90, 0.28);
  box-shadow: var(--panel-shadow), var(--input-container-shadow-focus);
}

.input-main-area {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.input-bottom-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px 8px;
  border-top: 1px solid var(--panel-border);
  flex-shrink: 0;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  -ms-overflow-style: none;
}

.input-bottom-toolbar::-webkit-scrollbar {
  display: none;
}

.toolbar-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 5px 10px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text-light);
  font-size: 12px;
  transition: color var(--transition-fast), background-color var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast);
  white-space: nowrap;
  -webkit-tap-highlight-color: transparent;
  height: 28px;
  line-height: 1;
}

.toolbar-btn:active {
  transform: scale(0.96);
}

.toolbar-btn:hover {
  background: var(--primary-tint);
  color: var(--color-primary);
  border-color: var(--panel-border-strong);
}

.toolbar-btn.active {
  background: var(--primary-tint-strong);
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.toolbar-btn-label {
  font-size: 12px;
  font-weight: 500;
}

.toolbar-spacer {
  flex: 1;
}

.reasoning-btn.active {
  background: var(--color-primary);
  color: #fff;
  border-color: var(--color-primary);
}

.reasoning-btn.active:hover {
  background: var(--color-primary);
  color: #fff;
}

.more-menu-wrapper {
  position: relative;
}

.more-dropdown-teleport {
  min-width: 180px;
  background: var(--surface-panel-strong);
  border: var(--menu-border);
  border-radius: var(--menu-radius);
  box-shadow: var(--menu-shadow);
  padding: 6px 0;
  animation: menu-pop-up 0.15s ease-out;
}

.more-dropdown-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 16px;
  color: var(--color-text);
  font-size: 13px;
  text-align: left;
  transition: background-color var(--transition-fast);
}

.more-dropdown-item:hover {
  background: var(--color-hover);
  color: var(--color-primary);
}

.more-dropdown-item.active {
  color: var(--color-primary);
  background: var(--primary-tint);
}

.recording-controls {
  display: flex;
  gap: 6px;
  padding: 6px 10px 8px;
  border-top: 1px solid var(--panel-border);
}

.stop-btn {
  flex-shrink: 0;
  width: 38px;
  height: 38px;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: var(--color-error);
  color: #fff;
  border-radius: var(--radius-pill);
  transition: background-color var(--transition-fast), opacity var(--transition-fast), transform var(--transition-fast);
  animation: pulse-record 1s ease-in-out infinite;
}

.stop-btn:active {
  transform: scale(0.96);
}

.stop-btn:hover {
  background-color: var(--color-error);
  opacity: 0.9;
}

.cancel-record-btn {
  flex-shrink: 0;
  width: 38px;
  height: 38px;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: var(--color-secondary);
  color: #fff;
  border-radius: var(--radius-pill);
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.cancel-record-btn:active {
  transform: scale(0.96);
}

.cancel-record-btn:hover {
  background-color: var(--color-text-light);
}

.mic-icon {
  transition: transform var(--transition-fast);
}

.processing-icon {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.wave-container {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  height: 18px;
}

.wave-bar {
  width: 2.5px;
  background-color: var(--color-white);
  border-radius: 1px;
  transition: height 0.08s ease-out;
  animation: wave 0.5s ease-in-out infinite alternate;
}

@keyframes wave {
  0% { transform: scaleY(0.5); }
  100% { transform: scaleY(1); }
}

.chat-input {
  display: inline-block;
  min-height: 1.5em;
  width: 100%;
  outline: none;
  word-break: break-word;
  line-height: 1.5;
  color: var(--color-text);
  white-space: pre-wrap;
  caret-color: var(--color-text);
  text-align: left;
}

/* 占位符用零宽 inline-block：字形向右溢出显示，插入符(offset-0)落在其
   左侧——即第一个字符将要出现的位置；内联/浮动/绝对定位都会把 caret
   挤到文字右侧或偏离文本起点 */
.chat-input.empty::before {
  content: attr(data-placeholder);
  display: inline-block;
  width: 0;
  white-space: nowrap;
  color: var(--color-text-light);
  pointer-events: none;
}

.chat-input.composing::before {
  display: none;
}

.send-btn {
  flex-shrink: 0;
  width: 38px;
  height: 38px;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: var(--color-primary);
  color: white;
  border-radius: var(--radius-pill);
  transition: background-color var(--transition-fast), transform var(--transition-fast), box-shadow var(--transition-fast);
  position: relative;
}

.send-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.send-btn .send-icon,
.send-btn .stop-icon {
  position: absolute;
  inset: 0;
  margin: auto;
  transition: opacity 0.2s cubic-bezier(0.2, 0, 0, 1), transform 0.2s cubic-bezier(0.2, 0, 0, 1), filter 0.2s cubic-bezier(0.2, 0, 0, 1);
}

.send-btn .send-icon.hidden,
.send-btn .stop-icon.hidden {
  opacity: 0;
  transform: scale(0.25);
  filter: blur(4px);
  pointer-events: none;
}

.send-btn:hover:not(:disabled) {
  background-color: var(--color-primary-dark);
}

.send-btn.stop-mode {
  background-color: var(--color-error);
}

.send-btn.stop-mode:hover:not(:disabled) {
  background-color: var(--color-error);
  opacity: 0.92;
}

.send-btn:disabled {
  background-color: var(--color-secondary);
  cursor: not-allowed;
}

.input-hint {
  text-align: center;
  font-size: 11px;
  color: var(--color-text-light);
  opacity: 0.7;
  margin-top: 8px;
}

.reasoning-toggle-group {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.reasoning-menu-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
}

.deathmatch-btn:hover {
  background-color: color-mix(in srgb, var(--color-danger, #dc2626) 20%, transparent);
  color: var(--color-danger, #dc2626);
}

.deathmatch-btn.active {
  background-color: var(--color-danger, #dc2626);
  color: #fff;
}

.deathmatch-btn.active:hover {
  background-color: var(--color-danger, #dc2626);
  opacity: 0.9;
  color: #fff;
}

.deathmatch-btn .deathmatch-label {
  position: absolute;
  top: -6px;
  right: -6px;
  font-size: 9px;
  background: var(--color-danger, #dc2626);
  color: #fff;
  border-radius: 4px;
  padding: 1px 3px;
  line-height: 1.2;
}

/* Toolbar layout */
.reasoning-toggle-group {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

 .reasoning-menu-overlay {
  position: fixed;
  inset: 0;
  z-index: 9999;
}

.reasoning-menu {
  background: var(--surface-panel-strong);
  border: var(--menu-border);
  border-radius: var(--menu-radius);
  box-shadow: var(--menu-shadow);
  padding: 8px;
  min-width: 160px;
  animation: menu-pop-up 0.15s ease-out;
}

@keyframes menu-pop-up {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.skill-popup {
  min-width: 260px;
  max-width: 360px;
  background: var(--surface-panel-strong);
  border: var(--menu-border);
  border-radius: var(--menu-radius);
  box-shadow: var(--menu-shadow);
  padding: 6px 0;
  animation: menu-pop-up 0.15s ease-out;
}

.skill-popup-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px 8px;
  border-bottom: 1px solid var(--color-border);
  margin-bottom: 4px;
}

.skill-popup-icon {
  color: var(--color-primary);
  flex-shrink: 0;
}

.skill-popup-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-light);
}

.skill-popup-list {
  max-height: 240px;
  overflow-y: auto;
  padding: 2px 0;
}

.skill-popup-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 8px 14px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  transition: background var(--transition-fast);
}

.skill-popup-item:hover,
.skill-popup-item.active {
  background: var(--color-hover);
}

.skill-popup-item.active {
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
}

.skill-item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  display: flex;
  align-items: center;
  gap: 6px;
}

.skill-popup-item.active .skill-item-name {
  color: var(--color-primary);
}

.skill-source-badge {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(59, 130, 246, 0.14);
  color: #2563eb;
  font-weight: 600;
  line-height: 1.4;
}

.skill-popup-item.system-skill .skill-item-name {
  color: #2563eb;
}

.skill-item-desc {
  font-size: 11px;
  color: var(--color-text-light);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.skill-popup-empty {
  padding: 12px 14px;
  font-size: 12px;
  color: var(--color-text-light);
  text-align: center;
}

.reasoning-menu-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-light);
  padding: 4px 8px 8px;
  border-bottom: 1px solid var(--color-border);
  margin-bottom: 4px;
}

.reasoning-menu-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.reasoning-menu-item:hover {
  background: var(--color-hover);
}

.reasoning-menu-item.active {
  background: color-mix(in srgb, var(--color-primary) 12%, transparent);
}

.reasoning-menu-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.reasoning-menu-item.active .reasoning-menu-label {
  color: var(--color-primary);
}

.reasoning-menu-desc {
  font-size: 11px;
  color: var(--color-text-light);
}


.input-text-area {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 20px;
  line-height: 1.5;
  cursor: text;
}

.input-text-area :deep(.note-tag) {
  display: inline-flex;
  align-items: center;
  vertical-align: middle;
  gap: 2px;
  padding: 0 8px;
  margin-right: 3px;
  margin-bottom: 1px;
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
  border-radius: var(--radius-pill);
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
  transition: background-color var(--transition-fast);
  line-height: 1.5;
  height: 22px;
  box-sizing: border-box;
}

.input-text-area :deep(.note-tag:hover) {
  background-color: color-mix(in srgb, var(--color-primary) 20%, transparent);
}

.input-text-area :deep(.note-tag svg) {
  flex-shrink: 0;
}

.input-text-area :deep(.note-tag-label) {
  max-width: 3em;
  overflow: hidden;
  text-overflow: ellipsis;
}

.input-text-area :deep(.note-tag-remove) {
  font-size: 13px;
  line-height: 1;
  color: var(--color-primary);
  opacity: 0.6;
  margin-left: 1px;
  padding: 0 2px;
  border-radius: var(--radius-pill);
}

.input-text-area :deep(.note-tag-remove:hover) {
  opacity: 1;
}

.input-text-area .chat-input {
  min-width: 1px;
}

.note-preview-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: var(--overlay-scrim);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.note-preview-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--shadow-md);
  border-radius: var(--radius-xl);
  max-width: 500px;
  width: 90%;
  max-height: 60vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.note-preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 24px 12px;
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
  background-color: var(--color-sidebar);
  padding: 12px;
  border-radius: var(--radius-sm);
  overflow-x: auto;
  font-size: 13px;
}

.note-preview-body :deep(code) {
  font-family: var(--font-mono);
  background-color: var(--color-sidebar);
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

.cached-queries {
  margin-top: 8px;
}

.cached-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background-color: var(--color-bg);
  border-radius: var(--radius-sm);
  margin-bottom: 4px;
}

.cached-header-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text);
}

.send-all-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  background-color: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 12px;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.send-all-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.send-all-btn:hover:not(:disabled) {
  background-color: var(--color-primary-dark);
}

.send-all-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.cached-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cached-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.cached-index {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary);
  min-width: 20px;
}

.cached-text {
  flex: 1;
  font-size: 12px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cached-remove {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.cached-remove:active {
  transform: scale(0.96);
}

.cached-remove:hover {
  color: var(--color-error);
  background-color: var(--color-hover);
}

.drafts-panel {
  position: absolute;
  bottom: 100%;
  left: 20px;
  right: 20px;
  margin: 0;
  margin-bottom: 8px;
  padding: 8px 20px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-xl);
  max-height: 320px;
  overflow-y: auto;
  z-index: 200;
}

.drafts-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--color-border);
  margin-bottom: 8px;
}

.drafts-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.drafts-close-btn {
  padding: 4px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.drafts-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.draft-item {
  display: flex;
  align-items: stretch;
  gap: 8px;
  padding: 8px;
  background-color: var(--color-bg);
  border-radius: var(--radius-md);
  border: 1px solid transparent;
}

.draft-item:hover {
  border-color: var(--color-primary);
}

.draft-preview {
  flex: 1;
  min-width: 0;
  cursor: pointer;
}

.draft-refs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 4px;
}

.draft-ref-tag {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: var(--radius-pill);
  background-color: var(--color-primary);
  color: white;
}

.draft-text {
  font-size: 12px;
  color: var(--color-text);
  white-space: normal;
  word-break: break-word;
  line-height: 1.4;
}

.draft-meta {
  font-size: 10px;
  color: var(--color-text-light);
  margin-top: 4px;
}

.draft-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.draft-send-btn, .draft-delete-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  color: var(--color-text-light);
  background: transparent;
}

.draft-send-btn:hover:not(:disabled) {
  background-color: var(--color-primary);
  color: white;
}

.draft-delete-btn:hover {
  background-color: var(--color-error);
  color: white;
}

.drafts-empty {
  font-size: 12px;
  color: var(--color-text-light);
  text-align: center;
  padding: 16px 8px;
}



.drafts-header-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.drafts-save-current-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  font-size: 12px;
  color: var(--color-primary);
  background-color: color-mix(in srgb, var(--color-primary) 10%, transparent);
  border-radius: var(--radius-pill);
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.drafts-save-current-btn:active {
  transform: scale(0.96);
}

.drafts-save-current-btn:hover {
  background-color: color-mix(in srgb, var(--color-primary) 20%, transparent);
}

.sub-agent-chunk-block {
  margin: 6px 0;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background-color: var(--color-bg);
  border-left: 3px solid var(--color-primary);
}

.sub-agent-reasoning {
  opacity: 0.75;
  font-size: 0.92em;
}

.recording-hint {
  color: var(--color-error);
  font-weight: 500;
}

.processing-hint {
  color: var(--color-primary);
  font-weight: 500;
}

.voice-mode-toggle-btn {
  display: flex;
  width: 38px;
  height: 38px;
  align-items: center;
  justify-content: center;
  background-color: var(--color-primary);
  color: #fff;
  border-radius: var(--radius-pill);
  transition: background-color var(--transition-fast), transform var(--transition-fast);
  margin-bottom: 0;
  align-self: center;
  flex-shrink: 0;
}

.voice-mode-toggle-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.voice-mode-toggle-btn:hover:not(:disabled) {
  background-color: var(--color-primary);
}

.voice-mode-toggle-btn.active {
  background-color: var(--color-primary);
}

.voice-mode-toggle-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.toggle-icon {
  transition: transform var(--transition-fast);
}

.voice-mode-toggle-btn:hover:not(:disabled) .toggle-icon {
  transform: scale(1.1);
}

.voice-waveform-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: 20px;
  box-shadow: var(--shadow-md);
  margin-bottom: 8px;
  animation: fadeIn 0.2s ease-out;
}

.waveform-visual {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 24px;
}

.waveform-visual .wave-bar {
  width: 3px;
  height: 100%;
  background-color: var(--color-primary);
  border-radius: 2px;
  animation: wave-anim 0.6s ease-in-out infinite alternate;
}

@keyframes wave-anim {
  0% { transform: scaleY(0.3); }
  50% { transform: scaleY(1); }
  100% { transform: scaleY(0.5); }
}

.waveform-status {
  font-size: 13px;
  color: var(--color-text);
  font-weight: 500;
  white-space: nowrap;
}

.waveform-done-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  background: var(--color-primary);
  color: white;
  border-radius: 16px;
  font-size: 13px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
  white-space: nowrap;
}

.waveform-done-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.waveform-done-btn:hover:not(:disabled) {
  background: var(--color-primary-dark);
  transform: translateY(-1px);
}

.waveform-done-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.waveform-cancel-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--color-text-light);
  border-radius: 50%;
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  flex-shrink: 0;
}

.waveform-cancel-btn:active {
  transform: scale(0.96);
}

.waveform-cancel-btn:hover {
  background: color-mix(in srgb, var(--color-error) 10%, transparent);
  color: var(--color-error);
}

.voice-confirm-modal {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  animation: fadeIn 0.2s ease-out;
}

.modal-content {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--shadow-md);
  border-radius: var(--radius-xl);
  padding: 20px;
  max-width: 400px;
  width: 90%;
  animation: scaleIn 0.2s ease-out;
}

.modal-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 12px;
}

.modal-text {
  font-size: 14px;
  color: var(--color-text);
  background-color: var(--color-bg);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  margin-bottom: 16px;
  max-height: 200px;
  overflow-y: auto;
  line-height: 1.5;
}

.modal-actions {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
}

.cancel-btn {
  padding: 8px 20px;
  border-radius: var(--radius-pill);
  background-color: var(--color-bg);
  color: var(--color-text);
  font-size: 14px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.cancel-btn:active {
  transform: scale(0.96);
}

.cancel-btn:hover {
  background-color: var(--color-hover);
}

.confirm-btn {
  padding: 8px 20px;
  border-radius: var(--radius-pill);
  background-color: var(--color-primary);
  color: white;
  font-size: 14px;
  font-weight: 500;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.confirm-btn:active {
  transform: scale(0.96);
}

.confirm-btn:hover {
  background-color: var(--color-primary-dark);
}

/* Visual refresh overrides */
.chat-input-wrapper {
  padding: 12px 20px calc(10px + env(safe-area-inset-bottom, 0));
  background: transparent;
}

.chat-input-container {
  background: var(--surface-input);
  border: 1px solid var(--input-container-border);
  border-radius: var(--input-container-radius);
  box-shadow: var(--panel-shadow);
}

.chat-input-container:focus-within {
  border-color: rgba(122, 163, 90, 0.28);
  box-shadow: var(--panel-shadow), var(--input-container-shadow-focus);
}

.chat-input {
  font-size: 15px;
  line-height: 1.6;
}

.input-text-area {
  padding: 12px 20px;
}

.input-hint {
  margin-top: 10px;
  opacity: 0.82;
}

.send-btn {
  width: 36px;
  height: 36px;
  border-radius: 14px;
  border: 1px solid transparent;
  background: var(--color-primary);
  color: white;
  box-shadow: var(--shadow-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color var(--transition-fast), transform var(--transition-fast), box-shadow var(--transition-fast), border-color var(--transition-fast), opacity var(--transition-fast);
  flex-shrink: 0;
  position: relative;
}

.send-btn:hover:not(:disabled) {
  transform: translateY(-1px);
}

.send-btn:active:not(:disabled) {
  transform: scale(0.96);
}

.send-btn.stop-mode {
  background: var(--color-error);
}

.send-btn.stop-mode:hover:not(:disabled) {
  opacity: 0.9;
}

.send-btn:disabled {
  background: var(--surface-panel-subtle);
  color: var(--color-text-light);
  border: 1px solid var(--panel-border);
  box-shadow: none;
}

.stop-btn,
.cancel-record-btn {
  width: 36px;
  height: 36px;
  border-radius: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color var(--transition-fast), opacity var(--transition-fast), transform var(--transition-fast);
  flex-shrink: 0;
}

.stop-btn:active,
.cancel-record-btn:active {
  transform: scale(0.96);
}

.stop-btn {
  background: var(--color-error);
  color: white;
  border: 1px solid transparent;
}

.cancel-record-btn {
  color: var(--color-error);
  background: rgba(224, 82, 82, 0.08);
  border: 1px solid rgba(224, 82, 82, 0.12);
}

.input-text-area :deep(.note-tag) {
  padding: 0 10px;
  background: rgba(122, 163, 90, 0.1);
  border: 1px solid rgba(122, 163, 90, 0.12);
  border-radius: 999px;
  color: var(--color-primary);
}

.input-text-area :deep(.note-tag:hover) {
  background: rgba(122, 163, 90, 0.16);
}

.note-preview-overlay,
.voice-confirm-modal {
  /* A4.9 R2：此前 `background` 简写在此处压过各单属性规则的 --overlay-scrim → 统一走令牌 */
  background-color: var(--overlay-scrim);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
}

.voice-confirm-modal {
  z-index: 10000;
}

.note-preview-card,
.modal-content {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow);
  border-radius: 30px;
}

.note-preview-header {
  border-bottom: 1px solid var(--panel-border);
}

.note-preview-close:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
}

.note-preview-body :deep(pre) {
  background: var(--surface-workbench);
  border: 1px solid var(--panel-border);
}

.note-preview-body :deep(code) {
  background: var(--surface-panel-subtle);
}

.note-preview-body :deep(th) {
  background: var(--surface-panel-subtle);
}

.note-preview-body :deep(tr:nth-child(even)) {
  background-color: rgba(122, 163, 90, 0.04);
}

.note-preview-body :deep(blockquote) {
  background: var(--surface-panel-subtle);
}

.drafts-panel {
  left: 20px;
  right: 20px;
  padding: 12px 18px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-xl);
}

.drafts-header {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--panel-border);
  margin-bottom: 12px;
}

.draft-item {
  padding: 10px;
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: 18px;
}

.draft-item:hover {
  border-color: var(--panel-border-strong);
  background: var(--color-hover);
}

.draft-ref-tag {
  background: rgba(122, 163, 90, 0.1);
  color: var(--color-primary);
  border: 1px solid rgba(122, 163, 90, 0.12);
}

.draft-send-btn,
.draft-delete-btn {
  width: 30px;
  height: 30px;
  border-radius: 12px;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-strong);
}

.draft-send-btn:hover:not(:disabled) {
  background: rgba(122, 163, 90, 0.1);
  color: var(--color-primary);
}

.draft-delete-btn:hover {
  background: color-mix(in srgb, var(--color-error) 8%, transparent);
  color: var(--color-error);
  border-color: color-mix(in srgb, var(--color-error) 12%, transparent);
}

.drafts-save-current-btn {
  padding: 6px 12px;
  border: 1px solid rgba(122, 163, 90, 0.12);
  background: rgba(122, 163, 90, 0.08);
  border-radius: 14px;
}

.drafts-save-current-btn:hover {
  background: rgba(122, 163, 90, 0.14);
}

.voice-record-area {
  border: 1px solid transparent;
  background: var(--color-primary);
  border-radius: 22px;
  box-shadow: var(--shadow-md);
}

.voice-record-area:active:not(.processing),
.voice-record-area.recording {
  background: var(--color-error);
  box-shadow: 0 16px 28px rgba(239, 68, 68, 0.18);
}

.voice-record-area.processing {
  background: var(--color-primary);
}

.mobile-voice-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  padding: 6px 10px;
  background: var(--surface-input);
  border-radius: inherit;
  z-index: 5;
}

.mobile-voice-bar {
  flex: 1;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 15px;
  font-weight: 500;
  user-select: none;
  -webkit-user-select: none;
  touch-action: none;
  -webkit-tap-highlight-color: transparent;
  border-radius: 22px;
}

.modal-text {
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: 18px;
}

.cancel-btn {
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: 16px;
}

.cancel-btn:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
}

.confirm-btn {
  background: var(--color-primary);
  border-radius: 16px;
  box-shadow: var(--shadow-md);
}

.confirm-btn:hover {
  transform: translateY(-1px);
}

.uploaded-files-area {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 12px 8px;
  /* Sit above the input box with a small gap so the chip's bottom edge
     never overlaps the input's top border. */
  margin-bottom: 6px;
  position: relative;
  z-index: 10;
}

.uploaded-file-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: 999px;
  font-size: 11px;
  color: var(--color-primary);
  max-width: 160px;
  box-shadow: var(--shadow-sm);
}

.chip-icon {
  flex-shrink: 0;
}

.chip-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chip-remove {
  font-size: 14px;
  line-height: 1;
  color: var(--color-primary);
  opacity: 0.6;
  padding: 0 2px;
  border-radius: 999px;
}

.chip-remove:hover {
  opacity: 1;
  background: rgba(224, 82, 82, 0.08);
  color: var(--color-error);
}

@media (max-width: 767px) {
  .chat-input-wrapper {
    padding: 4px 12px 2px;
    background: var(--surface-panel-strong);
  }

  .chat-input-wrapper.voice-active {}

  .drafts-panel {
    position: absolute;
    bottom: 100%;
    left: 12px;
    right: 12px;
    margin-top: 0;
    margin-bottom: 4px;
    padding: 8px 12px;
    border-radius: var(--radius-xl);
    border: 1px solid var(--panel-border);
    max-height: 200px;
    z-index: 200;
  }

  .voice-mode-toggle-btn {
    display: flex;
    width: 32px;
    height: 32px;
    border-radius: var(--radius-pill);
    margin-bottom: 0; /* 与发送按钮同一水平轴线 */
  }

  .voice-mode-toggle-btn svg {
    width: 15px;
    height: 15px;
  }

  /* 提升特异性压过 html[data-skin] 主题规则（verdant 7px 15px 等导致溢出横滑） */
  .chat-input-wrapper .chat-input-container {
    height: auto;
    min-height: 130px;
    max-height: 38vh; /* 底框随换行增高，上限内跟随；超出后内部滚动 */
    border-radius: var(--input-container-radius);
  }

  .input-bottom-toolbar {
    gap: 4px;
    padding: 6px 6px 8px;
    overflow-x: visible;
  }

  .input-bottom-toolbar .toolbar-btn {
    padding: 4px 5px;
    font-size: 11px;
    gap: 3px;
  }

  .input-bottom-toolbar .toolbar-btn svg {
    width: 14px;
    height: 14px;
  }

  .toolbar-btn-label {
    font-size: 11px;
  }

  .toolbar-spacer {
    flex: 1;
  }

  .input-text-area {
    padding: 10px 12px;
  }

  /* 语音模式：容器收缩恰好包住语音条（选择器压过皮肤 min-height 规则），
     条填满整个输入区域（padding 归零 + 条直角由容器圆角裁剪）；切回文字模式自动恢复 */
  .chat-input-wrapper .chat-input-container.voice-mode {
    height: auto;
    min-height: 0;
    max-height: none;
    padding: 0;
  }

  .chat-input-container.voice-mode .mobile-voice-bar {
    border-radius: 0;
  }

  .chat-input-container.voice-mode .input-main-area {
    display: none;
  }

  .chat-input-container.voice-mode .mobile-voice-overlay {
    position: relative;
    inset: auto;
    align-items: flex-end;
    padding: 0;
    background: transparent;
  }

  /* 切回文字输入：悬浮于输入框上方右侧（挂在 wrapper 层，避开容器 overflow 裁剪），不占语音条纵向空间 */
  .voice-mode-toggle-btn--float {
    position: absolute;
    bottom: 100%;
    right: 14px;
    margin-bottom: 6px;
    z-index: 6;
  }

  /* 按钮浮出容器与消息列表重叠时保证可点（wrapper 提升到消息内容之上） */
  body.voice-mode-active .chat-input-wrapper {
    z-index: 30;
  }

  body.voice-mode-active .input-hint {
    display: none;
  }

  .send-btn {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-pill);
  }

  .send-btn svg {
    width: 15px;
    height: 15px;
  }

  .wave-container {
    height: 15px;
    gap: 1.5px;
  }

  .wave-bar {
    width: 2px;
  }

  .voice-record-area {
    display: flex;
    height: 48px;
    position: relative;
    transition: background-color 0.15s ease, transform 0.15s ease;
  }

  .voice-record-area.cancel-hint {
    background: var(--color-error) !important;
    transform: scale(0.96);
    animation: shake-hint 0.3s ease;
  }

  @keyframes shake-hint {
    0%, 100% { transform: scale(0.96) translateY(0); }
    50% { transform: scale(0.96) translateY(-3px); }
  }

  .swipe-cancel-icon {
    position: absolute;
    top: 50%;
    right: 16px;
    transform: translateY(-50%);
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    background: rgba(255,255,255,0.2);
    border-radius: 50%;
  }

  .record-hint {
    font-size: 13px;
  }

  .input-hint {
    font-size: 10px;
    margin-top: 10px;
  }
}

@media (max-width: 480px) {
  .chat-input-wrapper {
    padding: 6px 8px 8px;
  }

  .chat-input-wrapper.voice-active {}

  .chat-input-wrapper .chat-input-container {
    height: auto;
    min-height: 110px;
    max-height: 36vh;
  }

  .drafts-panel {
    bottom: 100%;
  }

  .input-text-area {
    font-size: 15px;
  }
}
</style>