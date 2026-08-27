<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <!-- Fullscreen card state -->
  <div
    v-if="visible && !minimized"
    class="export-progress-overlay"
    @click.self="onOverlayClick"
  >
    <div class="export-progress-card" role="dialog" aria-live="polite">
      <div v-if="status !== 'success'" class="export-progress-spinner" aria-hidden="true">
        <svg viewBox="0 0 50 50" class="spinner-svg">
          <circle cx="25" cy="25" r="20" fill="none" stroke-width="4" />
        </svg>
      </div>
      <div v-else class="export-progress-success" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="32" height="32">
          <circle cx="12" cy="12" r="11" fill="none" stroke="currentColor" stroke-width="2"/>
          <path d="M7 12.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <div class="export-progress-body">
        <div class="export-progress-title">{{ resolvedTitle }}</div>
        <div class="export-progress-subtitle">{{ resolvedSubtitle }}</div>
      </div>
      <button
        v-if="status !== 'success'"
        type="button"
        class="export-progress-cancel"
        @click="onCancel"
      >取消</button>
      <button
        v-else
        type="button"
        class="export-progress-cancel"
        @click="onClose"
      >关闭</button>
    </div>
  </div>

  <!-- Minimized corner pill -->
  <div
    v-if="visible && minimized"
    class="export-progress-mini"
    role="status"
    aria-live="polite"
    @click="restore"
  >
    <div v-if="status !== 'success'" class="export-progress-mini-spinner" aria-hidden="true">
      <svg viewBox="0 0 50 50" class="spinner-svg">
        <circle cx="25" cy="25" r="20" fill="none" stroke-width="5" />
      </svg>
    </div>
    <div v-else class="export-progress-mini-success" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="20" height="20">
        <circle cx="12" cy="12" r="11" fill="none" stroke="currentColor" stroke-width="2"/>
        <path d="M7 12.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>
    <span class="export-progress-mini-label">{{ miniLabel }}</span>
    <button
      v-if="status === 'success'"
      type="button"
      class="export-progress-mini-close"
      @click.stop="onClose"
      aria-label="关闭"
    >×</button>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{
  visible: boolean
  title?: string
  subtitle?: string
  format?: 'md' | 'pdf'
  status?: 'exporting' | 'success'
  progress?: number
}>()

const emit = defineEmits<{
  (e: 'cancel'): void
  (e: 'close'): void
}>()

const minimized = ref(false)

// Reset minimized flag when the dialog is hidden so the next export
// opens as a full card.
watch(() => props.visible, (v) => {
  if (!v) minimized.value = false
})

const status = computed<'exporting' | 'success'>(() => props.status ?? 'exporting')

const resolvedTitle = computed(() => {
  if (status.value === 'success') return '导出成功'
  if (props.title) return props.title
  if (props.format === 'pdf') return '正在导出 PDF'
  if (props.format === 'md') return '正在导出 Markdown'
  return '正在导出'
})

const resolvedSubtitle = computed(() => {
  if (status.value === 'success') return '文件已保存。'
  if (props.subtitle) return props.subtitle
  const pct = props.progress != null && props.progress > 0 ? ` (${props.progress}%)` : ''
  if (props.format === 'pdf') return `PDF 渲染可能需要一些时间，请稍候…${pct}`
  if (props.format === 'md') return `正在生成 Markdown 文件，请稍候…${pct}`
  return `正在生成文件，请稍候…${pct}`
})

const miniLabel = computed(() => {
  if (status.value === 'success') return '导出成功'
  if (props.format === 'pdf') return '正在导出 PDF…'
  if (props.format === 'md') return '正在导出 Markdown…'
  return '正在导出…'
})

function onOverlayClick() {
  // Click outside card: while exporting, minimize to the corner rather
  // than cancelling. While success, close.
  if (status.value === 'success') {
    onClose()
  } else {
    minimized.value = true
  }
}

function onCancel() {
  emit('cancel')
}

function onClose() {
  emit('close')
  minimized.value = false
}

function restore() {
  minimized.value = false
}
</script>

<style scoped>
.export-progress-overlay {
  position: fixed;
  inset: 0;
  background: rgba(10, 18, 30, 0.28);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
  padding: 20px;
}

.export-progress-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow);
  color: var(--color-text);
  border-radius: 30px;
  padding: 24px 26px;
  min-width: 300px;
  max-width: min(80vw, 520px);
  display: flex;
  align-items: center;
  gap: 16px;
}

.export-progress-spinner,
.export-progress-success {
  flex: 0 0 36px;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-success);
}

.spinner-svg {
  width: 36px;
  height: 36px;
  animation: export-spin 1.2s linear infinite;
}

.spinner-svg circle {
  stroke: var(--color-primary);
  stroke-linecap: round;
  stroke-dasharray: 90 150;
  stroke-dashoffset: 0;
}

@keyframes export-spin {
  to { transform: rotate(360deg); }
}

.export-progress-body {
  flex: 1 1 auto;
  min-width: 0;
}

.export-progress-title {
  font-weight: 700;
  font-size: 16px;
  margin-bottom: 6px;
}

.export-progress-subtitle {
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
}

.export-progress-cancel {
  flex: 0 0 auto;
  padding: 10px 16px;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: inherit;
  border-radius: 16px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast);
}

.export-progress-cancel:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
  border-color: var(--panel-border-strong);
  transform: translateY(-1px);
}

.export-progress-mini {
  position: fixed;
  top: 18px;
  right: 18px;
  z-index: 10000;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px 10px 10px;
  background: var(--surface-panel-strong);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  color: var(--color-text);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-pill);
  box-shadow: var(--panel-shadow);
  cursor: pointer;
  max-width: 280px;
  font-size: 13px;
  animation: mini-in 220ms ease-out;
}

@keyframes mini-in {
  from { opacity: 0; transform: translateY(-6px); }
  to { opacity: 1; transform: translateY(0); }
}

.export-progress-mini-spinner,
.export-progress-mini-success {
  flex: 0 0 22px;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-success);
}

.export-progress-mini-spinner .spinner-svg {
  width: 22px;
  height: 22px;
}

.export-progress-mini-label {
  flex: 1 1 auto;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.export-progress-mini-close {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border: none;
  background: transparent;
  color: var(--color-text-light);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  border-radius: 50%;
}

.export-progress-mini-close:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
}

@media (max-width: 640px) {
  .export-progress-card {
    min-width: 0;
    width: 100%;
    padding: 22px 20px;
    border-radius: 24px;
    flex-direction: column;
    align-items: flex-start;
  }

  .export-progress-cancel {
    width: 100%;
  }

  .export-progress-mini {
    top: auto;
    right: 16px;
    left: 16px;
    bottom: 88px;
    max-width: none;
  }
}
</style>
