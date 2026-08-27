<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="stream-markdown" ref="containerRef" @click="onContainerClick"></div>
  <MediaLightbox
    :media="lightboxMedia"
    :kind="lightboxKind"
    :url="lightboxUrl"
    @close="closeLightbox"
    @download="onLightboxDownload"
  />
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import {
  renderMarkdownToHtml,
  processIncompleteMarkdown,
  addCitationSuperscripts,
  renderMermaidBlocks,
  fixLostMermaidBlocks,
  renderEchartsBlocks,
} from '@/composables/useMarkdown'
import {
  planNodeAdoption,
  shouldRetryFailedImg,
  type LiveNodeDesc,
  type FreshNodeDesc,
} from '@/composables/streamDomPreserve'
import MediaLightbox from './MediaLightbox.vue'
import { useInlineImageZoom } from '@/composables/useInlineImageZoom'

const props = defineProps<{
  content: string
  hasSearchResults?: boolean
}>()

const containerRef = ref<HTMLElement | null>(null)

// Strip heading-based bibliography sections and inline source prefixes during streaming
const BIBLIOGRAPHY_SECTION_REGEX = /(?:\n\n?---[^\S\n]*\n+)?(?:^|\n)[^\S\n]*(?:#{1,6}[^\S\n]*|\*{1,2}[^\S\n]*)?(?:参考文献|参考资料|参考来源|References|Sources|Reference)[：:]?[^\S\n]*(?:\*{1,2})?[^\S\n]*\n[\s\S]*$/i
const INLINE_SOURCE_REGEX = /来源[：:]\s*[^\n\[]+?\s*\[(\d{1,2})\]/g

function renderMarkdown(content: string): string {
  if (!content) return ''
  let processed = processIncompleteMarkdown(content)
  // Strip bibliography section and inline source prefixes for clean streaming display
  processed = processed.replace(BIBLIOGRAPHY_SECTION_REGEX, '')
  processed = processed.replace(INLINE_SOURCE_REGEX, '[$1]')
  const html = renderMarkdownToHtml(processed)
  if (props.hasSearchResults) {
    return addCitationSuperscripts(html, true)
  }
  // No search results — strip [N] markers
  return html.replace(/\s*\[(\d{1,2})\]/g, '')
}

const RENDER_INTERVAL_MS = 80
const REPLAY_THRESHOLD = 50
const MERMAID_STABLE_MS = 400
const FAILED_IMG_RETRY_MS = 3000
const FINAL_STABLE_MS = 1500
let renderTimer: ReturnType<typeof setTimeout> | null = null
let pendingContent: string | null = null
let mermaidStableTimer: ReturnType<typeof setTimeout> | null = null
let finalStableTimer: ReturnType<typeof setTimeout> | null = null
let lastRenderedContent = ''

const { lightboxMedia, lightboxUrl, lightboxKind, openImageLightbox, closeLightbox, onLightboxDownload } =
  useInlineImageZoom()

function onContainerClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!(target instanceof Element)) return
  const img = target.tagName === 'IMG'
    ? (target as HTMLImageElement)
    : (target.closest('img') as HTMLImageElement | null)
  if (!img || !containerRef.value?.contains(img)) return
  if (openImageLightbox(img)) {
    e.preventDefault()
    e.stopPropagation()
  }
}

// ── Live-node preservation across render passes ─────────────────────────
// Every ~80ms re-render used to replace the whole innerHTML, destroying every
// <img> and rendered mermaid/echarts block. Detaching an <img> aborts its
// in-flight fetch, so images referenced while the answer streams never
// finished loading (only a refresh — single render — showed them, conv
// ba74a7f5 repro).
//
// The render now happens WITHOUT v-html: the new HTML is parsed into an inert
// <template> fragment (template contents never start subresource fetches),
// live nodes are adopted INTO the fragment by position (an <img> with the
// identical signed src, a rendered block with the same data-*-id), and the
// fragment's children are moved into the container with replaceChildren —
// all in one synchronous task, no paint in between. The adopted <img> keeps
// its ongoing load; only genuinely new images (not adopted) ever start a
// fetch, so a referenced-but-not-yet-written file does NOT spam a request
// per render pass.

function collectDescs(root: ParentNode, renderedOnly: boolean): {
  descs: LiveNodeDesc[] | FreshNodeDesc[]
  nodes: (HTMLImageElement | HTMLElement)[]
} {
  const descs: (LiveNodeDesc | FreshNodeDesc)[] = []
  const nodes: (HTMLImageElement | HTMLElement)[] = []
  root.querySelectorAll<HTMLImageElement>('img').forEach((img) => {
    const src = img.getAttribute('src') || ''
    if (!src) return
    descs.push({ kind: 'img', key: src })
    nodes.push(img)
  })
  const mermaidSel = renderedOnly
    ? '.mermaid-block.mermaid-rendered[data-mermaid-sig]'
    : '.mermaid-block:not(.mermaid-rendered)[data-mermaid-sig]'
  root.querySelectorAll<HTMLElement>(mermaidSel).forEach((b) => {
    const id = b.getAttribute('data-mermaid-sig') || ''
    if (!id) return
    descs.push({ kind: 'mermaid', key: id })
    nodes.push(b)
  })
  const echartsSel = renderedOnly
    ? '.echarts-block.echarts-rendered[data-echarts-sig]'
    : '.echarts-block:not(.echarts-rendered)[data-echarts-sig]'
  root.querySelectorAll<HTMLElement>(echartsSel).forEach((b) => {
    const id = b.getAttribute('data-echarts-sig') || ''
    if (!id) return
    descs.push({ kind: 'echarts', key: id })
    nodes.push(b)
  })
  return { descs, nodes }
}

function applyRenderedDom(content: string): boolean {
  const container = containerRef.value
  if (!container) return false

  const live = collectDescs(container, true)

  const tpl = document.createElement('template')
  tpl.innerHTML = renderMarkdown(content)
  const frag = tpl.content

  const fresh = collectDescs(frag, false)
  const plan = planNodeAdoption(live.descs, fresh.descs)
  plan.forEach((liveIdx, freshIdx) => {
    if (liveIdx === -1) return
    const old = live.nodes[liveIdx]
    const freshNode = fresh.nodes[freshIdx]
    if (old && freshNode && old !== freshNode) {
      freshNode.replaceWith(old)
    }
  })

  container.replaceChildren(...Array.from(frag.childNodes))

  retryFailedImages(false)
  scheduleMermaidRender()
  scheduleFinalRetry()
  return true
}

// Failed images (referenced before the file existed) are preserved but their
// src is reset on a throttle so a file written later in the stream loads
// without refresh — and without per-render request spam. The map is pruned
// when the broken img disappears from the DOM.
const failedImgRetryAt = new Map<string, number>()

function retryFailedImages(force: boolean) {
  const container = containerRef.value
  if (!container) return
  const now = Date.now()
  const liveSrcs = new Set<string>()
  container.querySelectorAll<HTMLImageElement>('img').forEach((img) => {
    const src = img.getAttribute('src') || ''
    if (!src) return
    liveSrcs.add(src)
    if (!img.complete || img.naturalWidth !== 0) return
    const last = failedImgRetryAt.get(src) || 0
    if (!shouldRetryFailedImg(now, last, force, FAILED_IMG_RETRY_MS)) return
    failedImgRetryAt.set(src, now)
    img.removeAttribute('src')
    img.setAttribute('src', src)
  })
  for (const key of Array.from(failedImgRetryAt.keys())) {
    if (!liveSrcs.has(key)) failedImgRetryAt.delete(key)
  }
}

// Stream-end retry: when content stops arriving (~1.5s), force-retry any
// image that was referenced before its file existed — the final chunk's
// render is throttled, this timer covers the "file created in the last
// seconds" case without a refresh.
function scheduleFinalRetry() {
  if (finalStableTimer !== null) clearTimeout(finalStableTimer)
  finalStableTimer = setTimeout(() => {
    finalStableTimer = null
    retryFailedImages(true)
  }, FINAL_STABLE_MS)
}

function scheduleMermaidRender() {
  if (mermaidStableTimer !== null) clearTimeout(mermaidStableTimer)
  mermaidStableTimer = setTimeout(() => {
    mermaidStableTimer = null
    if (containerRef.value) {
      const blocks = containerRef.value.querySelectorAll<HTMLElement>('.mermaid-block:not(.mermaid-rendered)')
      if (blocks.length > 0) {
        renderMermaidBlocks(containerRef.value)
      }
      const chartBlocks = containerRef.value.querySelectorAll<HTMLElement>('.echarts-block:not(.echarts-rendered)')
      if (chartBlocks.length > 0) {
        renderEchartsBlocks(containerRef.value)
      }
    }
  }, MERMAID_STABLE_MS)
}

function doRender(content: string) {
  if (content === lastRenderedContent) return
  // Only mark rendered when the container actually received it — a render
  // attempt before mount (containerRef null) must not suppress the onMounted
  // render of the same content (persisted steps / tool results are static:
  // props.content never changes after mount).
  if (applyRenderedDom(content)) {
    lastRenderedContent = content
  }
}

function scheduleRender(content: string) {
  pendingContent = content
  if (renderTimer !== null) return
  renderTimer = setTimeout(() => {
    renderTimer = null
    if (pendingContent !== null) {
      doRender(pendingContent)
      pendingContent = null
    }
  }, RENDER_INTERVAL_MS)
}

function flushRender() {
  if (renderTimer !== null) {
    clearTimeout(renderTimer)
    renderTimer = null
  }
  if (pendingContent !== null) {
    doRender(pendingContent)
    pendingContent = null
  } else {
    retryFailedImages(true)
  }
}

watch(
  () => props.content,
  (newContent, oldContent) => {
    if (!oldContent && newContent) {
      doRender(newContent)
    } else if (newContent.length - (oldContent?.length || 0) > REPLAY_THRESHOLD) {
      flushRender()
      doRender(newContent)
    } else {
      scheduleRender(newContent)
    }
  },
  { immediate: true }
)

watch(
  () => props.content,
  () => {},
  {
    flush: 'post',
    onCleanup: () => {
      flushRender()
      if (mermaidStableTimer !== null) {
        clearTimeout(mermaidStableTimer)
        mermaidStableTimer = null
      }
      if (containerRef.value) {
        renderMermaidBlocks(containerRef.value)
        renderEchartsBlocks(containerRef.value)
      }
    },
  }
)

onMounted(() => {
  // Static usages (persisted agent steps / tool results / sub-agent outputs)
  // get their content at mount time only — the immediate watch fired during
  // setup before containerRef existed, so render here too.
  if (props.content) doRender(props.content)
})

onBeforeUnmount(() => {
  flushRender()
  failedImgRetryAt.clear()
  if (mermaidStableTimer !== null) {
    clearTimeout(mermaidStableTimer)
    mermaidStableTimer = null
  }
  if (finalStableTimer !== null) {
    clearTimeout(finalStableTimer)
    finalStableTimer = null
  }
  if (containerRef.value) {
    renderMermaidBlocks(containerRef.value)
    renderEchartsBlocks(containerRef.value)
  }
})
</script>

<style scoped>
.stream-markdown {
  line-height: 1.6;
  word-break: break-word;
  overflow-x: auto;
}

.stream-markdown :deep(code) {
  font-family: var(--font-mono);
  background-color: var(--color-code-bg);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

.stream-markdown :deep(pre) {
  background-color: var(--color-code-bg);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--color-border);
}

/* The .code-block wrapper carries the frame; neutralize the bare-pre styling
 * for wrapped code blocks (see global .code-block styles in main.css). */
.stream-markdown :deep(.code-block pre) {
  background: transparent;
  border: 0;
  border-radius: 0;
  margin: 0;
  padding: 12px 16px;
}

.stream-markdown :deep(pre code) {
  background: none;
  padding: 0;
}

.stream-markdown :deep(h1),
.stream-markdown :deep(h2),
.stream-markdown :deep(h3),
.stream-markdown :deep(h4),
.stream-markdown :deep(h5),
.stream-markdown :deep(h6) {
  margin: 16px 0 8px 0;
  font-weight: 600;
  line-height: 1.4;
  color: var(--color-text);
}

.stream-markdown :deep(h1) { font-size: 1.5em; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
.stream-markdown :deep(h2) { font-size: 1.3em; border-bottom: 1px solid var(--color-border); padding-bottom: 6px; }
.stream-markdown :deep(h3) { font-size: 1.15em; }
.stream-markdown :deep(h4) { font-size: 1em; }
.stream-markdown :deep(h5) { font-size: 0.95em; }
.stream-markdown :deep(h6) { font-size: 0.9em; color: var(--color-text-light); }

.stream-markdown :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 13px;
  display: block;
  overflow-x: auto;
}

.stream-markdown :deep(thead),
.stream-markdown :deep(tbody) {
  display: table;
  width: 100%;
  table-layout: fixed;
}

.stream-markdown :deep(th),
.stream-markdown :deep(td) {
  border: 1px solid var(--color-border);
  padding: 8px 12px;
  text-align: left;
  word-break: normal;
  overflow-wrap: break-word;
}

.stream-markdown :deep(th) {
  background-color: var(--color-bg);
  font-weight: 600;
}

.stream-markdown :deep(tr:nth-child(even)) {
  background-color: var(--color-bg);
}

.stream-markdown :deep(ul),
.stream-markdown :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}

.stream-markdown :deep(li) {
  margin: 4px 0;
  line-height: 1.6;
}

.stream-markdown :deep(ul) {
  list-style-type: disc;
}

.stream-markdown :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.stream-markdown :deep(ol > li) {
  counter-increment: ol-counter;
}

.stream-markdown :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  margin-right: 2px;
}

.stream-markdown :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 16px;
  border-left: 4px solid var(--color-primary);
  background-color: var(--color-bg);
  color: var(--color-text-light);
  font-style: italic;
}

.stream-markdown :deep(blockquote p) {
  margin: 0;
}

.stream-markdown :deep(a) {
  color: var(--color-primary-dark);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
}

.stream-markdown :deep(.mermaid-block) {
  display: flex;
  justify-content: center;
  margin: 12px 0;
  overflow-x: auto;
}

.stream-markdown :deep(.mermaid-block svg) {
  max-width: 100%;
  height: auto;
}

.stream-markdown :deep(.echarts-block) {
  margin: 12px 0;
  overflow-x: auto;
}

.stream-markdown :deep(.echarts-block svg) {
  max-width: 100%;
  height: auto;
  display: block;
}

.stream-markdown :deep(.echarts-error) {
  background-color: color-mix(in srgb, var(--color-error) 10%, transparent);
  border: 1px solid var(--color-error);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
  overflow-x: auto;
}

.stream-markdown :deep(.mermaid-error) {
  background-color: color-mix(in srgb, var(--color-error) 10%, transparent);
  border: 1px solid var(--color-error);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
}

.stream-markdown :deep(.mermaid-controls) {
  display: flex;
  gap: 4px;
  justify-content: center;
  margin-top: 6px;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.stream-markdown :deep(.mermaid-block:hover .mermaid-controls) {
  opacity: 1;
}

.stream-markdown :deep(.mermaid-edit-btn),
.stream-markdown :deep(.mermaid-zoom-btn) {
  padding: 4px 8px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-white);
  color: var(--color-text-light);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  transition: all var(--transition-fast);
}

.stream-markdown :deep(.mermaid-edit-btn:hover),
.stream-markdown :deep(.mermaid-zoom-btn:hover) {
  background: var(--color-hover);
  color: var(--color-text);
  border-color: var(--color-primary);
}

.stream-markdown :deep(a:hover) {
  border-bottom-style: solid;
}

.stream-markdown :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 16px 0;
}

.stream-markdown :deep(p) {
  margin: 8px 0;
}

.stream-markdown :deep(p:first-child) {
  margin-top: 0;
}

.stream-markdown :deep(p:last-child) {
  margin-bottom: 0;
}

.stream-markdown :deep(img) {
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

.stream-markdown :deep(img:hover) {
  outline: 1px solid var(--color-border);
  outline-offset: 2px;
}

.stream-markdown :deep(.hljs) {
  background: transparent;
}

.stream-markdown :deep(.hljs-keyword),
.stream-markdown :deep(.hljs-selector-tag),
.stream-markdown :deep(.hljs-built_in),
.stream-markdown :deep(.hljs-name),
.stream-markdown :deep(.hljs-tag) {
  color: var(--code-keyword, #d73a49);
}

.stream-markdown :deep(.hljs-string),
.stream-markdown :deep(.hljs-title),
.stream-markdown :deep(.hljs-section),
.stream-markdown :deep(.hljs-attribute),
.stream-markdown :deep(.hljs-literal),
.stream-markdown :deep(.hljs-template-tag),
.stream-markdown :deep(.hljs-template-variable),
.stream-markdown :deep(.hljs-type) {
  color: var(--code-string, #032f62);
}

.stream-markdown :deep(.hljs-comment),
.stream-markdown :deep(.hljs-quote) {
  color: var(--code-comment, #6a737d);
  font-style: italic;
}

.stream-markdown :deep(.hljs-number),
.stream-markdown :deep(.hljs-addition) {
  color: var(--code-number, #005cc5);
}

.stream-markdown :deep(.hljs-function) {
  color: var(--code-function, #6f42c1);
}

.stream-markdown :deep(.hljs-variable),
.stream-markdown :deep(.hljs-params) {
  color: var(--code-variable, #e36209);
}

.stream-markdown :deep(.citation-ref) {
  font-size: 0.75em;
  vertical-align: super;
  line-height: 0;
  color: var(--color-primary);
  font-weight: 600;
  padding: 0 1px;
}
</style>
