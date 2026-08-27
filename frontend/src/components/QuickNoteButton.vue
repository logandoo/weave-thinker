<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div
    class="quick-note-wrapper"
    :style="wrapperStyle"
    @touchstart.passive="onDragStart"
    @touchmove.prevent="onDragMove"
    @touchend="onDragEnd"
  >
    <button
      class="quick-note-btn"
      :class="{ recording: isRecording, processing: isProcessing }"
      @click="onBtnClick"
      :disabled="isProcessing"
      title="快速语音笔记"
    >
      <svg class="qnb-icon qnb-icon-mic" :class="{ hidden: isRecording || isProcessing }" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
        <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
        <line x1="12" y1="19" x2="12" y2="23"/>
        <line x1="8" y1="23" x2="16" y2="23"/>
      </svg>
      <svg class="qnb-icon qnb-icon-stop" :class="{ hidden: !isRecording }" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="6" y="6" width="12" height="12" rx="2"/>
      </svg>
      <svg class="qnb-icon qnb-icon-spin" :class="{ hidden: !isProcessing }" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="10" stroke-dasharray="32" stroke-dashoffset="32"/>
      </svg>
    </button>

    <div class="recording-indicator" v-if="isRecording">
      <span class="dot"></span>
      录音中...
    </div>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useNotesStore } from '@/stores/notes'
import { useToast } from '@/composables/useToast'
import { useAsrStreaming } from '@/composables/useAsrStreaming'
import { useAsrHotwords } from '@/composables/useAsrHotwords'

const notesStore = useNotesStore()
const router = useRouter()
const { show } = useToast()
const { hotwords: asrHotwords } = useAsrHotwords()

const savingQuickNote = ref(false)
const asrStreaming = useAsrStreaming({
  customHotwords: () => asrHotwords.value,
  onError(message) {
    show(message, 'error')
  },
})
const isRecording = asrStreaming.isRecording
const isProcessing = computed(() => asrStreaming.isFinishing.value || savingQuickNote.value)

// Drag state
const isDragging = ref(false)
const dragStartX = ref(0)
const dragStartY = ref(0)
const posX = ref<number | null>(null)
const posY = ref<number | null>(null)
const dragMoved = ref(false)

const wrapperStyle = computed(() => {
  if (posX.value !== null && posY.value !== null) {
    return {
      position: 'fixed' as const,
      right: 'auto',
      bottom: 'auto',
      left: `${posX.value}px`,
      top: `${posY.value}px`,
    }
  }
  return {}
})

function onDragStart(e: TouchEvent) {
  if (e.touches.length !== 1) return
  const touch = e.touches[0]
  const el = (e.currentTarget as HTMLElement)
  const rect = el.getBoundingClientRect()
  dragStartX.value = touch.clientX - rect.left
  dragStartY.value = touch.clientY - rect.top
  dragMoved.value = false
  isDragging.value = true
}

function onDragMove(e: TouchEvent) {
  if (!isDragging.value || e.touches.length !== 1) return
  const touch = e.touches[0]
  dragMoved.value = true
  let newX = touch.clientX - dragStartX.value
  let newY = touch.clientY - dragStartY.value
  // Constrain to viewport
  const el = (e.currentTarget as HTMLElement)
  const w = el.offsetWidth
  const h = el.offsetHeight
  newX = Math.max(0, Math.min(newX, window.innerWidth - w))
  newY = Math.max(0, Math.min(newY, window.innerHeight - h))
  posX.value = newX
  posY.value = newY
}

function onDragEnd() {
  isDragging.value = false
}

function onBtnClick() {
  if (dragMoved.value) {
    dragMoved.value = false
    return
  }
  toggleRecording()
}
onMounted(async () => {
  await notesStore.loadDefaultNotebook()
})

async function toggleRecording() {
  if (isProcessing.value) return

  if (isRecording.value) {
    await stopRecording()
  } else {
    await startRecording()
  }
}

async function startRecording() {
  try {
    await asrStreaming.start({ chunk_size_sec: 0.5 })
  } catch (err) {
    console.error('Failed to start recording:', err)
    if (!asrStreaming.error.value) {
      show(err instanceof Error ? err.message : '录音启动失败', 'error')
    }
  }
}

async function stopRecording() {
  try {
    const result = await asrStreaming.stop()
    const transcript = result?.text?.trim()
    if (!transcript) {
      return
    }

    savingQuickNote.value = true
    const note = await notesStore.createQuickNote(transcript)
    const notebookName = notesStore.defaultNotebook?.name || '默认笔记本'
    const noteName = note.title || '无标题笔记'

    show(
      `笔记已保存在 ${notebookName} > ${noteName}`,
      'success',
      4000,
      {
        label: '跳转',
        onClick: () => {
          void router.push(`/notes/${note.notebook_id}/${note.id}`)
        },
      },
    )
  } catch (error) {
    if (error instanceof Error && error.message === '录音已取消') {
      return
    }
    console.error('Quick note failed:', error)
    const msg = error instanceof Error ? error.message : '语音识别失败'
    show(msg, 'error')
  } finally {
    savingQuickNote.value = false
  }
}
</script>

<style scoped>
.quick-note-wrapper {
  position: fixed;
  bottom: calc(var(--mobile-tab-bar-offset, 80px) + 12px + env(safe-area-inset-bottom, 0));
  right: 20px;
  z-index: 100;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}

.quick-note-btn {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background-color: var(--color-primary);
  color: white;
  box-shadow: var(--shadow-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color var(--transition-fast), transform var(--transition-fast), box-shadow var(--transition-fast);
  position: relative;
}

.quick-note-btn:hover {
  transform: scale(1.05);
}

.quick-note-btn:active {
  transform: scale(0.96);
}

.quick-note-btn .qnb-icon {
  position: absolute;
  inset: 0;
  margin: auto;
  transition: opacity 0.2s cubic-bezier(0.2, 0, 0, 1), transform 0.2s cubic-bezier(0.2, 0, 0, 1), filter 0.2s cubic-bezier(0.2, 0, 0, 1);
}

.quick-note-btn .qnb-icon.hidden {
  opacity: 0;
  transform: scale(0.25);
  filter: blur(4px);
  pointer-events: none;
}

.quick-note-btn .qnb-icon-spin {
  animation: spin 1s linear infinite;
}

.quick-note-btn.recording {
  background-color: var(--color-error);
  animation: pulse 1s infinite;
}

.quick-note-btn.processing {
  background-color: var(--color-secondary);
}

.quick-note-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

@keyframes pulse {
  0%, 100% {
    box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.4);
  }
  50% {
    box-shadow: 0 0 0 12px rgba(239, 68, 68, 0);
  }
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.recording-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background-color: var(--color-error);
  color: white;
  border-radius: var(--radius-lg);
  font-size: 12px;
  white-space: nowrap;
}

.dot {
  width: 6px;
  height: 6px;
  background-color: white;
  border-radius: 50%;
  animation: blink 1s infinite;
}

@keyframes blink {
  0%, 50% { opacity: 1; }
  51%, 100% { opacity: 0.3; }
}

@media (max-width: 767px) {
  .quick-note-wrapper {
    bottom: calc(var(--mobile-tab-bar-offset, 70px) + 12px + env(safe-area-inset-bottom, 0));
    right: 12px;
  }

  body.voice-mode-active .quick-note-wrapper {
    display: none;
  }

  .quick-note-btn {
    width: 48px;
    height: 48px;
  }

  .quick-note-btn svg {
    width: 18px;
    height: 18px;
  }
}

@media (min-width: 768px) {
  .quick-note-wrapper {
    display: none;
  }
}
</style>
