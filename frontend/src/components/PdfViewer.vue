<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="pdf-viewer">
    <div class="pdf-toolbar">
      <button class="pdf-btn" :disabled="page <= 1 || loading" title="上一页" @click="go(page - 1)">‹</button>
      <span class="pdf-page-info">{{ page }} / {{ numPages || '–' }}</span>
      <button class="pdf-btn" :disabled="page >= numPages || loading" title="下一页" @click="go(page + 1)">›</button>
      <span class="pdf-spacer"></span>
      <button class="pdf-btn" :disabled="loading" title="缩小" @click="zoomBy(1 / 1.2)">−</button>
      <span class="pdf-zoom-info">{{ Math.round(pageScale(page - 1) * 100) }}%</span>
      <button class="pdf-btn" :disabled="loading" title="放大" @click="zoomBy(1.2)">＋</button>
      <button class="pdf-btn" :disabled="loading" title="适应宽度" @click="fitWidth">适宽</button>
    </div>
    <div ref="scrollEl" class="pdf-scroll" @scroll.passive="onScroll">
      <div v-if="error" class="pdf-status pdf-status--error">{{ error }}</div>
      <div v-else-if="loading" class="pdf-status">渲染中...</div>
      <div
        v-for="p in numPages"
        :key="p"
        class="pdf-page"
        :data-page="p"
        :style="pageStyle(p - 1)"
      >
        <canvas class="pdf-canvas"></canvas>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps<{ bytes: ArrayBuffer }>()

const scrollEl = ref<HTMLElement | null>(null)
const page = ref(1)
const numPages = ref(0)
// `fitMode` (default) sizes EVERY page to the container width individually —
// single-page Calc exports produce per-sheet content-cropped page sizes, so a
// global scale would blow one page up (2026-09-19 sales_dashboard page 2).
const fitMode = ref(true)
const scale = ref(1)
// Reactive container width: the ResizeObserver updates it so page frames
// (pageStyle) and renders (pageScale) follow container resizes together
// (A4.9 fix4-R2 minor: frames used to keep stale sizes).
const viewportWidth = ref(0)
const loading = ref(false)
const error = ref('')

interface PageDim {
  width: number
  height: number
}
const pageDims = ref<PageDim[]>([])

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let pdfDoc: any = null
let observer: IntersectionObserver | null = null
const renderedScale: number[] = []
// In-flight pdfjs RenderTask per page + pages re-requested while busy.
// Never zero a canvas that a task is drawing into: pdfjs then calls
// drawImage with a 0-sized canvas and throws (user console error
// 2026-09-19), leaving a blank first paint until the next navigation.
const renderTasks = new Map<number, { task: any; scale: number; token: object }>()
const pendingRenders = new Set<number>()
const rendering = new Set<number>()
// Monotonic document generation: any render/load started before a newer load
// (or destroy) must never paint into the new document's canvases (A4.9 fix7
// I-2/I-3: stale tasks could collide on a canvas or write stale scales).
let docGeneration = 0
// Rendering every page forever would pin memory on huge PDFs; the observer
// only ever draws pages near the viewport (plus the current jump target).
const RENDER_MARGIN = '600px 0px'

function canvasOf(index: number): HTMLCanvasElement | null {
  const host = scrollEl.value?.querySelector<HTMLElement>(`.pdf-page[data-page="${index + 1}"]`)
  return host?.querySelector('canvas') ?? null
}

function pageScale(index: number): number {
  const dim = pageDims.value[index]
  if (!dim) return 1
  if (fitMode.value) {
    const raw = viewportWidth.value || scrollEl.value?.clientWidth || 900
    const width = Math.max(200, raw - 24)
    return Math.min(4, Math.max(0.2, width / dim.width))
  }
  return scale.value
}

function pageStyle(index: number): Record<string, string> {
  const dim = pageDims.value[index]
  if (!dim) return {}
  const s = pageScale(index)
  return {
    width: `${Math.floor(dim.width * s)}px`,
    height: `${Math.floor(dim.height * s)}px`,
  }
}

async function loadPdf() {
  loading.value = true
  error.value = ''
  destroyDoc()
  const generation = docGeneration
  try {
    const pdfjs = await import('pdfjs-dist')
    const workerUrl = (await import('pdfjs-dist/build/pdf.worker.min.mjs?url')).default
    pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
    const loaded = await pdfjs.getDocument({ data: props.bytes.slice(0) }).promise
    if (generation !== docGeneration) {
      loaded.destroy?.()
      return
    }
    pdfDoc = loaded
    numPages.value = pdfDoc.numPages
    page.value = 1
    const dims: PageDim[] = []
    for (let i = 1; i <= pdfDoc.numPages; i += 1) {
      const pdfPage = await pdfDoc.getPage(i)
      if (generation !== docGeneration) return
      const viewport = pdfPage.getViewport({ scale: 1 })
      dims.push({ width: viewport.width, height: viewport.height })
    }
    if (generation !== docGeneration) return
    pageDims.value = dims
    fitWidth()
    await nextTick()
    if (generation !== docGeneration) return
    setupObserver()
    renderVisibleNow()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'PDF 渲染失败'
  } finally {
    loading.value = false
  }
}

function destroyDoc() {
  docGeneration += 1
  observer?.disconnect()
  observer = null
  cancelRenderTasks()
  renderedScale.length = 0
  rendering.clear()
  pendingRenders.clear()
  pdfDoc?.destroy?.()
  pdfDoc = null
  numPages.value = 0
  pageDims.value = []
}

function fitWidth() {
  viewportWidth.value = scrollEl.value?.clientWidth ?? viewportWidth.value
  fitMode.value = true
  invalidateRendered()
}

function zoomBy(factor: number) {
  // step from what is actually on screen (fit mode keeps `scale` untouched)
  setScale(pageScale(page.value - 1) * factor)
}

function setScale(next: number) {
  const clamped = Math.min(4, Math.max(0.3, next))
  fitMode.value = false
  if (Math.abs(clamped - scale.value) < 0.001) return
  scale.value = clamped
  invalidateRendered()
}

function cancelRenderTasks() {
  for (const { task } of renderTasks.values()) {
    try {
      task?.cancel()
    } catch {
      /* already settled */
    }
  }
  renderTasks.clear()
}

function invalidateRendered() {
  cancelRenderTasks()
  renderedScale.length = 0
  // Do NOT zero canvas dimensions here — an in-flight pdfjs task drawing into
  // a 0-sized canvas throws `drawImage ... width or height of 0` and the page
  // stays blank until a later navigation (user report 2026-09-19).
  nextTick(() => renderVisibleNow())
}

function setupObserver() {
  observer?.disconnect()
  if (!scrollEl.value) return
  observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          const idx = Number((entry.target as HTMLElement).dataset.page || '0') - 1
          if (idx >= 0) void renderPage(idx)
        }
      }
    },
    { root: scrollEl.value, rootMargin: RENDER_MARGIN },
  )
  for (const el of Array.from(scrollEl.value.querySelectorAll('.pdf-page'))) {
    observer.observe(el)
  }
}

async function renderPage(index: number, force = false) {
  if (!pdfDoc) return
  const generation = docGeneration
  const targetScale = pageScale(index)
  if (!force && renderedScale[index] === targetScale) return
  if (rendering.has(index)) {
    // A render is in flight for this page: queue exactly one re-render after
    // it settles so the latest scale always wins.
    if (renderTasks.get(index)?.scale !== targetScale || force) {
      pendingRenders.add(index)
    }
    return
  }
  const token = {}
  rendering.add(index)
  try {
    const pdfPage = await pdfDoc.getPage(index + 1)
    if (generation !== docGeneration) return
    const viewport = pdfPage.getViewport({ scale: targetScale })
    const canvas = canvasOf(index)
    if (!canvas) return
    const cssWidth = Math.max(1, Math.floor(viewport.width))
    const cssHeight = Math.max(1, Math.floor(viewport.height))
    const dpr = window.devicePixelRatio || 1
    // Chromium silently blanks canvases beyond its max dimensions; cap the
    // backing store and let CSS scale it back up (A4.9 I-2 defense in depth).
    const MAX_CANVAS_DIM = 16000
    let pixelRatio = dpr
    const longest = Math.max(cssWidth, cssHeight)
    if (longest * pixelRatio > MAX_CANVAS_DIM) {
      pixelRatio = Math.max(0.2, MAX_CANVAS_DIM / longest)
    }
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('canvas 2d unavailable')
    const backingWidth = Math.max(1, Math.floor(cssWidth * pixelRatio))
    const backingHeight = Math.max(1, Math.floor(cssHeight * pixelRatio))
    if (canvas.width !== backingWidth) canvas.width = backingWidth
    if (canvas.height !== backingHeight) canvas.height = backingHeight
    canvas.style.width = `${cssWidth}px`
    canvas.style.height = `${cssHeight}px`
    const task = pdfPage.render({
      canvasContext: ctx,
      viewport,
      transform: pixelRatio !== 1 ? [pixelRatio, 0, 0, pixelRatio, 0, 0] : undefined,
    })
    renderTasks.set(index, { task, scale: targetScale, token })
    await task.promise
    if (renderTasks.get(index)?.token === token) {
      renderedScale[index] = targetScale
      if (error.value) error.value = ''
    }
  } catch (e: any) {
    if (e?.name !== 'RenderingCancelledException') {
      error.value = e instanceof Error ? e.message : 'PDF 渲染失败'
    }
  } finally {
    if (renderTasks.get(index)?.token === token) {
      renderTasks.delete(index)
      rendering.delete(index)
      if (pendingRenders.delete(index)) {
        void renderPage(index, true)
      }
    } else {
      // a newer invocation owns this page now
      rendering.delete(index)
    }
  }
}

/** Render every page whose reserved box intersects the scroll viewport. */
function renderVisibleNow() {
  const container = scrollEl.value
  if (!container || !pdfDoc) return
  const containerRect = container.getBoundingClientRect()
  const margin = 600
  for (const el of Array.from(container.querySelectorAll<HTMLElement>('.pdf-page'))) {
    const rect = el.getBoundingClientRect()
    if (rect.bottom >= containerRect.top - margin && rect.top <= containerRect.bottom + margin) {
      void renderPage(Number(el.dataset.page || '0') - 1)
    }
  }
}

function offsetWithinScroll(el: HTMLElement): number {
  const container = scrollEl.value
  if (!container) return el.offsetTop
  return (
    el.getBoundingClientRect().top -
    container.getBoundingClientRect().top +
    container.scrollTop
  )
}

function onScroll() {
  const container = scrollEl.value
  if (!container) return
  const marker = container.scrollTop + 60
  const pages = Array.from(container.querySelectorAll<HTMLElement>('.pdf-page'))
  let current = 1
  for (const el of pages) {
    if (offsetWithinScroll(el) <= marker) current = Number(el.dataset.page || '1')
    else break
  }
  page.value = current
}

async function go(target: number) {
  if (!pdfDoc || target < 1 || target > numPages.value) return
  page.value = target
  const el = scrollEl.value?.querySelector<HTMLElement>(`.pdf-page[data-page="${target}"]`)
  if (el && scrollEl.value) {
    scrollEl.value.scrollTo({ top: Math.max(0, offsetWithinScroll(el) - 8) })
  }
  await renderPage(target - 1)
}

watch(() => props.bytes, () => { loadPdf() })

let resizeObserver: ResizeObserver | null = null

onMounted(() => {
  loadPdf()
  if (typeof ResizeObserver !== 'undefined' && scrollEl.value) {
    resizeObserver = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect?.width ?? 0
      if (width > 0 && Math.abs(width - viewportWidth.value) > 0.5) {
        viewportWidth.value = width
        if (fitMode.value) invalidateRendered()
      }
    })
    resizeObserver.observe(scrollEl.value)
  }
})

onUnmounted(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  destroyDoc()
})
</script>

<style scoped>
.pdf-viewer {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
  width: 100%;
}

.pdf-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
}

.pdf-btn {
  min-width: 28px;
  height: 26px;
  padding: 0 8px;
  font-size: 13px;
  color: var(--color-text);
  background: transparent;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.pdf-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
}

.pdf-btn:disabled {
  opacity: 0.4;
  cursor: default;
}

.pdf-page-info,
.pdf-zoom-info {
  font-size: 12px;
  color: var(--color-text-light);
  min-width: 52px;
  text-align: center;
}

.pdf-spacer {
  flex: 1;
}

.pdf-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: auto;
  background: var(--surface-workbench);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 12px;
}

.pdf-page {
  flex-shrink: 0;
  background: #fff;
  box-shadow: var(--shadow-md);
  display: flex;
  align-items: flex-start;
  justify-content: center;
}

.pdf-canvas {
  display: block;
}

.pdf-status {
  padding: 32px 8px;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-light);
}

.pdf-status--error {
  color: var(--color-error);
}
</style>
