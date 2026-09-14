<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div class="file-preview-overlay" @click.self="close">
      <div class="file-preview-dialog" role="dialog" aria-modal="true">
        <div class="file-preview-header">
          <span class="file-preview-title" :title="filename">{{ filename }}</span>
          <button class="file-preview-close" aria-label="关闭预览" title="关闭" @click="close">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="file-preview-body">
          <iframe
            v-if="kind === 'pdf' && pdfBlobUrl"
            :src="pdfBlobUrl"
            class="file-preview-iframe"
            title="PDF 预览"
          ></iframe>
          <div v-else-if="loading" class="file-preview-status">加载中...</div>
          <div v-else-if="loadError" class="file-preview-status file-preview-status--error">{{ loadError }}</div>
          <div
            v-else-if="kind === 'markdown'"
            class="file-preview-markdown markdown-body"
            v-html="renderedHtml"
          ></div>
          <pre v-else class="file-preview-pre">{{ textContent }}</pre>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { renderMarkdownToHtml } from '@/composables/useMarkdown'

const props = defineProps<{
  filename: string
  url: string
}>()

const emit = defineEmits<{ close: [] }>()

const MARKDOWN_EXTS = new Set(['md', 'markdown'])

function extOf(name: string): string {
  const base = (name || '').split(/[?#]/)[0]
  const idx = base.lastIndexOf('.')
  return idx === -1 ? '' : base.slice(idx + 1).toLowerCase()
}

const kind = computed<'pdf' | 'markdown' | 'text'>(() => {
  const ext = extOf(props.filename)
  if (ext === 'pdf') return 'pdf'
  if (MARKDOWN_EXTS.has(ext)) return 'markdown'
  return 'text'
})

const loading = ref(false)
const loadError = ref('')
const textContent = ref('')
const pdfBlobUrl = ref('')
let objectUrl: string | null = null

const renderedHtml = computed(() =>
  kind.value === 'markdown' ? renderMarkdownToHtml(textContent.value) : ''
)

async function loadContent() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetch(props.url)
    if (!res.ok) throw new Error(`加载失败（${res.status}）`)
    if (kind.value === 'pdf') {
      // 下载端点带 Content-Disposition: attachment，直接塞进 iframe 会触发下载；
      // 先取回字节再以 blob URL 内联展示（同一文件 URL，无需改后端）。
      const blob = await res.blob()
      releaseObjectUrl()
      // 强制 application/pdf：blob URL 的类型决定 iframe 是内联渲染还是下载
      objectUrl = URL.createObjectURL(new Blob([blob], { type: 'application/pdf' }))
      pdfBlobUrl.value = objectUrl
    } else {
      textContent.value = await res.text()
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function releaseObjectUrl() {
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl)
    objectUrl = null
  }
}

function close() {
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    close()
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  loadContent()
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  releaseObjectUrl()
})
</script>

<style scoped>
.file-preview-overlay {
  position: fixed;
  inset: 0;
  background-color: var(--overlay-scrim);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.file-preview-dialog {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow);
  border-radius: var(--radius-xl);
  width: min(860px, 92vw);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.file-preview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
}

.file-preview-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-preview-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex-shrink: 0;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.file-preview-close:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
  color: var(--color-text);
}

.file-preview-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 16px 20px 20px;
}

.file-preview-iframe {
  width: 100%;
  height: 72vh;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--color-white);
}

.file-preview-status {
  padding: 32px 8px;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-light);
}

.file-preview-status--error {
  color: var(--color-error);
}

.file-preview-pre {
  margin: 0;
  padding: 14px 16px;
  background: var(--surface-workbench);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}

.file-preview-markdown {
  font-size: 14px;
  line-height: 1.7;
  color: var(--color-text);
}

.file-preview-markdown :deep(h1),
.file-preview-markdown :deep(h2),
.file-preview-markdown :deep(h3),
.file-preview-markdown :deep(h4),
.file-preview-markdown :deep(h5),
.file-preview-markdown :deep(h6) {
  margin: 16px 0 8px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--color-text);
}

.file-preview-markdown :deep(h1) { font-size: 1.5em; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
.file-preview-markdown :deep(h2) { font-size: 1.3em; border-bottom: 1px solid var(--color-border); padding-bottom: 6px; }
.file-preview-markdown :deep(h3) { font-size: 1.15em; }

.file-preview-markdown :deep(p) {
  margin: 8px 0;
}

.file-preview-markdown :deep(p:first-child) {
  margin-top: 0;
}

.file-preview-markdown :deep(p:last-child) {
  margin-bottom: 0;
}

.file-preview-markdown :deep(pre) {
  background: var(--surface-workbench);
  border: 1px solid var(--panel-border);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  overflow-x: auto;
  margin: 8px 0;
}

.file-preview-markdown :deep(pre code) {
  background: none;
  padding: 0;
}

.file-preview-markdown :deep(code) {
  font-family: var(--font-mono);
  background: var(--surface-panel-subtle);
  padding: 2px 4px;
  border-radius: 3px;
  font-size: 13px;
}

.file-preview-markdown :deep(ul),
.file-preview-markdown :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.file-preview-markdown :deep(ul) { list-style-type: disc; }
.file-preview-markdown :deep(ol) { list-style-type: decimal; }

.file-preview-markdown :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.file-preview-markdown :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 16px;
  border-left: 4px solid var(--color-primary);
  background: var(--surface-panel-subtle);
  color: var(--color-text-light);
}

.file-preview-markdown :deep(blockquote p) {
  margin: 0;
}

.file-preview-markdown :deep(a) {
  color: var(--color-primary-dark);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
}

.file-preview-markdown :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 12px 0;
  display: block;
  overflow-x: auto;
  font-size: 13px;
}

.file-preview-markdown :deep(th),
.file-preview-markdown :deep(td) {
  border: 1px solid var(--color-border);
  padding: 8px 12px;
  text-align: left;
  overflow-wrap: break-word;
}

.file-preview-markdown :deep(th) {
  background: var(--surface-panel-subtle);
  font-weight: 600;
}

.file-preview-markdown :deep(tr:nth-child(even)) {
  background: color-mix(in srgb, var(--color-hover) 50%, transparent);
}

.file-preview-markdown :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-md);
  margin: 8px 0;
}

.file-preview-markdown :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 16px 0;
}

@media (max-width: 767px) {
  .file-preview-overlay {
    padding: 12px;
  }

  .file-preview-dialog {
    width: 100%;
    max-height: 88vh;
  }

  .file-preview-iframe {
    height: 64vh;
  }
}
</style>
