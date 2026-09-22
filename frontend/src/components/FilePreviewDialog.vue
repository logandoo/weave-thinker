<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div class="file-preview-overlay" @click.self="close">
      <div class="file-preview-dialog" :class="{ 'file-preview-dialog--fill': isFillKind }" role="dialog" aria-modal="true">
        <div class="file-preview-header">
          <div class="file-preview-heading">
            <span class="file-preview-title" :title="relPath || filename">{{ filename }}</span>
            <span v-if="kind === 'unknown' && relPath" class="file-preview-subpath">工作区路径: {{ relPath }}</span>
          </div>
          <div class="file-preview-actions">
            <button
              v-if="loadError || officeClientError"
              class="file-preview-action"
              title="重试"
              @click="reload"
            >重试</button>
            <button class="file-preview-action" title="下载" @click="download">下载</button>
            <button class="file-preview-close" aria-label="关闭预览" title="关闭" @click="close">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
        </div>
        <div class="file-preview-body" :class="{ 'file-preview-body--fill': isFillKind }">
          <div v-if="loading" class="file-preview-status">加载中...</div>

          <div v-else-if="loadError" class="file-preview-status file-preview-status--error">
            <p class="file-preview-error-text">{{ loadError }}</p>
            <button class="file-preview-action file-preview-action--primary" @click="reload">重试</button>
          </div>

          <img
            v-else-if="kind === 'image'"
            :src="url"
            class="file-preview-image"
            alt="图片预览"
            @error="loadError = '图片加载失败'"
          />

          <video
            v-else-if="kind === 'video'"
            :src="url"
            class="file-preview-video"
            controls
            playsinline
          ></video>

          <audio
            v-else-if="kind === 'audio'"
            :src="url"
            class="file-preview-audio"
            controls
          ></audio>

          <PdfViewer
            v-else-if="(kind === 'pdf' || officeMode === 'server') && pdfBytes"
            :bytes="pdfBytes"
            class="file-preview-pdf"
          />

          <iframe
            v-else-if="officeMode === 'html' && officeHtmlUrl"
            :src="officeHtmlUrl"
            class="file-preview-html"
            sandbox=""
            referrerpolicy="no-referrer"
            title="表格预览"
          ></iframe>

          <div v-else-if="officeMode === 'client'" class="file-preview-office">
            <component
              :is="officeComponent"
              v-if="officeComponent && officeBuffer"
              :src="officeBuffer"
              class="file-preview-office-host"
              @rendered="onOfficeRendered"
              @error="onOfficeError"
            />
            <div v-if="officeClientError" class="file-preview-status file-preview-status--error">
              <p class="file-preview-error-text">{{ officeClientError }}</p>
              <button class="file-preview-action file-preview-action--primary" @click="download">下载文件</button>
            </div>
          </div>

          <div
            v-else-if="kind === 'markdown'"
            class="file-preview-markdown markdown-body"
            v-html="renderedHtml"
          ></div>

          <pre v-else-if="kind === 'text' || kind === 'code'" class="file-preview-pre">{{ textContent }}</pre>

          <div v-else class="file-preview-unknown">
            <div class="file-preview-unknown-icon">📎</div>
            <div class="file-preview-unknown-title">{{ fileTypeLabel(kind) }} 类型暂不支持在线预览</div>
            <div v-if="kind === 'unknown' && relPath" class="file-preview-unknown-path">
              工作区路径：<code>{{ relPath }}</code>
            </div>
            <button class="file-preview-action file-preview-action--primary" @click="download">下载文件</button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, markRaw, onMounted, onUnmounted, ref, shallowRef, type Component } from 'vue'
import { renderMarkdownToHtml } from '@/composables/useMarkdown'
import { classifyFile, fileTypeLabel, type FileKind } from '@/composables/filePreview'
import { buildOfficeHtmlUrl, buildOfficePdfUrl } from '@/api/workspaceFiles'
import { downloadUrl } from '@/composables/useDownload'
import PdfViewer from './PdfViewer.vue'

const props = defineProps<{
  filename: string
  url: string
  relPath?: string | null
  type?: string | null
  /**
   * Path used for the server office conversion (`/api/files/office-pdf`).
   * New attachments carry a workspace-relative `rel_path`; legacy persisted
   * attachments only have an absolute in-workspace `path` — both are valid
   * for the endpoint (it enforces workspace containment), but only rel_path
   * is ever shown to the user. The download link uses `url`, which callers
   * build from the same fallback.
   */
  sourcePath?: string | null
}>()

const emit = defineEmits<{ close: [] }>()

const kind = computed<FileKind>(() => classifyFile(props.filename, props.type))
const isOfficeKind = computed(() => kind.value === 'word' || kind.value === 'excel' || kind.value === 'ppt')
// PDF / Office bodies own a fixed-height frame so the viewer gets real estate;
// other kinds (text, image, audio) keep an auto-height dialog.
const isFillKind = computed(
  () => kind.value === 'pdf' || isOfficeKind.value,
)
const officeSource = computed(() => props.sourcePath || props.relPath || '')

const loading = ref(false)
const loadError = ref('')
const textContent = ref('')
const pdfBytes = ref<ArrayBuffer | null>(null)
const officeMode = ref<'none' | 'server' | 'client' | 'html'>('none')
const officeHtmlUrl = ref('')
const officeBuffer = ref<ArrayBuffer | null>(null)
const officeComponent = shallowRef<Component | null>(null)
const officeClientError = ref('')

function releaseOfficeHtml() {
  if (officeHtmlUrl.value) {
    URL.revokeObjectURL(officeHtmlUrl.value)
    officeHtmlUrl.value = ''
  }
}

// Guards the async HTML fetch: incremented on every content (re)load and on
// unmount so a late-resolving response can never store a blob URL on a dead
// or superseded component (each unrevoked URL pins the whole document).
let officeHtmlLoadId = 0
let unmounted = false

const renderedHtml = computed(() =>
  kind.value === 'markdown' ? renderMarkdownToHtml(textContent.value) : ''
)

async function loadText() {
  const res = await fetch(props.url)
  if (!res.ok) throw new Error(`加载失败（${res.status}）`)
  textContent.value = await res.text()
}

async function loadPdf() {
  const res = await fetch(props.url)
  if (!res.ok) throw new Error(`加载失败（${res.status}）`)
  pdfBytes.value = await res.arrayBuffer()
}

async function loadOfficeComponent(): Promise<Component> {
  if (kind.value === 'word') {
    const mod = await import('@vue-office/docx')
    return markRaw((mod.default || mod) as Component)
  }
  if (kind.value === 'excel') {
    await import('@vue-office/excel/lib/index.css')
    const mod = await import('@vue-office/excel')
    return markRaw((mod.default || mod) as Component)
  }
  const mod = await import('@vue-office/pptx')
  return markRaw((mod.default || mod) as Component)
}

/**
 * Office chain (D-81, HTML path 2026-09-21): spreadsheets first try the
 * server HTML preview (natural-width tables + wrapped long text), then the
 * server LibreOffice→PDF path, then the client-side OOXML renderer.
 */
async function loadOffice() {
  const source = officeSource.value
  if (source) {
    if (kind.value === 'excel') {
      const loadId = ++officeHtmlLoadId
      try {
        const res = await fetch(buildOfficeHtmlUrl(source))
        if (res.ok) {
          const html = await res.text()
          if (unmounted || loadId !== officeHtmlLoadId) return
          releaseOfficeHtml()
          officeHtmlUrl.value = URL.createObjectURL(
            new Blob([html], { type: 'text/html' }),
          )
          officeMode.value = 'html'
          return
        }
        // 400/501/422 → fall through to the PDF path
      } catch {
        // network error → fall through
      }
      if (unmounted || loadId !== officeHtmlLoadId) return
    }
    try {
      const res = await fetch(buildOfficePdfUrl(source))
      if (res.ok) {
        pdfBytes.value = await res.arrayBuffer()
        officeMode.value = 'server'
        return
      }
      // 501/422/404 → fall through to the client renderer
    } catch {
      // network error → client renderer
    }
  }
  const res = await fetch(props.url)
  if (!res.ok) throw new Error(`加载失败（${res.status}）`)
  officeBuffer.value = await res.arrayBuffer()
  officeComponent.value = await loadOfficeComponent()
  officeMode.value = 'client'
}

async function loadContent() {
  loading.value = true
  loadError.value = ''
  officeClientError.value = ''
  officeMode.value = 'none'
  officeHtmlLoadId += 1
  releaseOfficeHtml()
  officeBuffer.value = null
  officeComponent.value = null
  pdfBytes.value = null
  textContent.value = ''
  try {
    if (kind.value === 'pdf') {
      await loadPdf()
    } else if (isOfficeKind.value) {
      await loadOffice()
    } else if (kind.value === 'markdown' || kind.value === 'text' || kind.value === 'code') {
      await loadText()
    }
    // image/video/audio/unknown/archive render directly from `url`
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function onOfficeRendered() {
  officeClientError.value = ''
}

function onOfficeError(e: unknown) {
  // Keep the raw renderer error for debugging, show an actionable message.
  console.error('office client renderer failed:', e)
  officeClientError.value = '该 Office 文件无法在线渲染，请下载后查看'
}

function reload() {
  loadContent()
}

async function download() {
  await downloadUrl(props.url, props.filename)
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
  unmounted = true
  officeHtmlLoadId += 1
  document.removeEventListener('keydown', onKeydown)
  releaseOfficeHtml()
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
  width: min(960px, 94vw);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* PDF / Office viewers need a definite height: an auto-height flex column
   collapses a flex:1 iframe to ~0 (user report 2026-09-19: "PDF 预览高度太
   低，几乎无法浏览"). Fixed frame + fill body gives the viewer the space. */
.file-preview-dialog--fill {
  height: min(88vh, 980px);
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

.file-preview-heading {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 2px;
}

.file-preview-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-preview-subpath {
  font-size: 11px;
  color: var(--color-text-light);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-preview-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.file-preview-action {
  font-size: 12px;
  color: var(--color-text-light);
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--panel-border);
  background: transparent;
  cursor: pointer;
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.file-preview-action:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
  color: var(--color-text);
}

.file-preview-action--primary {
  color: var(--color-primary);
  border-color: color-mix(in srgb, var(--color-primary) 40%, transparent);
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
  display: flex;
  flex-direction: column;
}

/* Fill kinds: the viewer (iframe / office host) owns the whole body box. */
.file-preview-body--fill {
  overflow: hidden;
  padding: 12px;
}

.file-preview-pdf {
  flex: 1 1 auto;
  width: 100%;
  min-height: 0;
}

/* Server-rendered spreadsheet HTML (sanitized, sandboxed): tables keep their
   natural column widths and scroll horizontally; long text wraps inside
   cells. Served from a blob URL with `sandbox` (no scripts). */
.file-preview-html {
  flex: 1 1 auto;
  width: 100%;
  min-height: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: #fff;
}

.file-preview-body--fill .file-preview-html {
  min-height: 0;
}

.file-preview-image {
  max-width: 100%;
  max-height: 76vh;
  object-fit: contain;
  align-self: center;
  border-radius: var(--radius-sm);
}

.file-preview-video {
  max-width: 100%;
  max-height: 76vh;
  background: #000;
  border-radius: var(--radius-sm);
  align-self: center;
}

.file-preview-audio {
  width: 100%;
  margin-top: 24px;
}

.file-preview-office {
  min-height: 60vh;
  display: flex;
  flex-direction: column;
  flex: 1;
  /* the client-side OOXML renderer document scrolls here (user report
     2026-09-19: weekly_report.docx could not scroll in the fallback path) */
  overflow: auto;
}

.file-preview-body--fill .file-preview-office {
  min-height: 0;
}

.file-preview-office-host {
  width: 100%;
  min-height: 0;
  flex: 1 1 auto;
  height: auto;
}

.file-preview-status {
  padding: 32px 8px;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-light);
}

.file-preview-status--error {
  color: var(--color-error);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.file-preview-error-text {
  margin: 0;
  word-break: break-word;
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

.file-preview-unknown {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 16px;
}

.file-preview-unknown-icon {
  font-size: 40px;
}

.file-preview-unknown-title {
  font-size: 14px;
  color: var(--color-text);
}

.file-preview-unknown-path {
  font-size: 12px;
  color: var(--color-text-light);
  max-width: 100%;
}

.file-preview-unknown-path code {
  font-family: var(--font-mono);
  background: var(--surface-workbench);
  border: 1px solid var(--panel-border);
  border-radius: 4px;
  padding: 2px 6px;
  word-break: break-all;
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
    max-height: 90vh;
  }

  .file-preview-dialog--fill {
    height: 90vh;
  }
}
</style>
