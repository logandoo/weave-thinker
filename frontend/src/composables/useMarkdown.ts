// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { marked } from 'marked'
import hljs from 'highlight.js'
import mermaid from 'mermaid'
import * as echarts from 'echarts'
import katex from 'katex'
import DOMPurify from 'dompurify'
import 'katex/dist/katex.min.css'
import { nextTick } from 'vue'

// Preserve data-mermaid-source through DOMPurify sanitization.
// DOMPurify strips attributes whose decoded values contain "-->" (HTML
// comment terminator) as an XSS precaution. Mermaid arrow syntax "-->"
// triggers this check, causing the source to be lost and mermaid blocks to
// render as empty divs. These hooks save the attribute before sanitization
// and restore it afterward, so the mermaid source survives intact.
DOMPurify.addHook('beforeSanitizeAttributes', (node: Element) => {
  if (
    node.nodeType === 1 &&
    node.tagName === 'DIV' &&
    (node.classList?.contains('mermaid-block') || node.classList?.contains('echarts-block'))
  ) {
    const src = node.getAttribute('data-mermaid-source') ?? node.getAttribute('data-echarts-source')
    if (src !== null) {
      ;(node as any).__mdBlockSrc = src
    }
  }
})
DOMPurify.addHook('afterSanitizeAttributes', (node: Element) => {
  if ((node as any).__mdBlockSrc !== undefined) {
    if (node.classList?.contains('mermaid-block')) {
      node.setAttribute('data-mermaid-source', (node as any).__mdBlockSrc)
    } else {
      node.setAttribute('data-echarts-source', (node as any).__mdBlockSrc)
    }
    delete (node as any).__mdBlockSrc
  }
})

let mermaidInitialized = false
let mermaidIdCounter = 0
let echartsIdCounter = 0
let mermaidRenderQueue: Promise<void> = Promise.resolve()
// Live ECharts instances created by renderSingleEchartsBlock; swept by
// sweepEchartsInstances() on every render pass to dispose detached charts.
const liveEchartsInstances = new Set<echarts.ECharts>()

// Simple LRU cache for rendered Markdown HTML. Chat messages rarely change
// once finalized, and re-rendering the same long Markdown on every scroll
// or store update is expensive.
const RENDER_CACHE_MAX = 128
const renderCache = new Map<string, string>()

function getRenderCacheKey(content: string): string {
  // Fast stable key: length + a few boundary samples to avoid full hashing.
  const len = content.length
  if (len <= 8) return content
  const head = content.slice(0, 4)
  const tail = content.slice(-4)
  return `${len}:${head}:${tail}`
}

function getCachedRender(content: string): string | undefined {
  const key = getRenderCacheKey(content)
  const hit = renderCache.get(key)
  if (hit !== undefined) {
    // LRU: move to end
    renderCache.delete(key)
    renderCache.set(key, hit)
  }
  return hit
}

function setCachedRender(content: string, html: string): void {
  const key = getRenderCacheKey(content)
  if (renderCache.has(key)) renderCache.delete(key)
  else if (renderCache.size >= RENDER_CACHE_MAX) {
    const first = renderCache.keys().next().value
    if (first !== undefined) renderCache.delete(first)
  }
  renderCache.set(key, html)
}

// Cache for rendered Mermaid SVGs keyed by decoded source.
const MERMAID_SVG_CACHE_MAX = 32
const mermaidSvgCache = new Map<string, string>()

function getCachedMermaidSvg(source: string): string | undefined {
  const hit = mermaidSvgCache.get(source)
  if (hit !== undefined) {
    mermaidSvgCache.delete(source)
    mermaidSvgCache.set(source, hit)
  }
  return hit
}

function setCachedMermaidSvg(source: string, svg: string): void {
  if (mermaidSvgCache.has(source)) mermaidSvgCache.delete(source)
  else if (mermaidSvgCache.size >= MERMAID_SVG_CACHE_MAX) {
    const first = mermaidSvgCache.keys().next().value
    if (first !== undefined) mermaidSvgCache.delete(first)
  }
  mermaidSvgCache.set(source, svg)
}

/**
 * Strip DSML control tags (e.g. <｜｜DSML｜｜tool_calls>, <｜｜DSML｜｜invoke …>)
 * from content. These are DeepSeek-style inline tool-call markers that some
 * models emit inside the text stream instead of using native function-calling
 * deltas. They must never appear in the UI.
 *
 * Handles both complete blocks and partial fragments left over when a block
 * is split across streaming deltas.
 */
const _DSML_MARKER = '<\uff5c\uff5cDSML\uff5c\uff5c'
const _DSML_TOOL_CALLS_BLOCK_RE = /<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>[\s\S]*?<\/\uff5c\uff5cDSML\uff5c\uff5ctool_calls>/g
const _DSML_TOOL_CALLS_OPEN_RE = /<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>[\s\S]*$/g
const _DSML_ANY_TAG_RE = /<\/?\uff5c\uff5cDSML\uff5c\uff5c[^>]*>/g
const _DSML_PARTIAL_RE = /<\uff5c\uff5cDSML\uff5c\uff5c[^>]*$/

const _DM_CTX_RE = /<!--dm_ctx:round=\d+:hash=[a-f0-9]+:ts=\d+-->/g

export function stripDsmlTags(content: string): string {
  if (!content || !content.includes(_DSML_MARKER)) return content
  // 1. Remove complete <｜｜DSML｜｜tool_calls>…</｜｜DSML｜｜tool_calls> blocks
  content = content.replace(_DSML_TOOL_CALLS_BLOCK_RE, '')
  // 2. Remove incomplete tool_calls blocks (opening tag without matching close)
  content = content.replace(_DSML_TOOL_CALLS_OPEN_RE, '')
  // 3. Remove any remaining complete individual DSML tags
  content = content.replace(_DSML_ANY_TAG_RE, '')
  // 4. Remove trailing partial DSML fragment (streaming edge case: tag split
  //    across deltas so the closing ">" hasn't arrived yet)
  content = content.replace(_DSML_PARTIAL_RE, '')
  content = content.replace(_DM_CTX_RE, '')
  return content
}

function enqueueMermaidRender<T>(fn: () => Promise<T>): Promise<T> {
  let resolve: (v: T) => void
  let reject: (e: unknown) => void
  const p = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  mermaidRenderQueue = mermaidRenderQueue.then(async () => {
    try { resolve(await fn()) } catch (e) { reject(e) }
  })
  return p
}

function cleanupMermaidTempElements() {
  document.querySelectorAll('[id^="d"][id*="mermaid"]').forEach(el => {
    if (el.tagName === 'DIV' && !el.closest('.mermaid-block')) el.remove()
  })
}

function ensureMermaidInit() {
  if (mermaidInitialized) return
  mermaidInitialized = true
  mermaid.initialize({
    startOnLoad: false,
    theme: 'default',
    securityLevel: 'strict',
    fontFamily: 'inherit',
  })
}

/**
 * Generate a URL-safe slug from heading text. Supports CJK by keeping
 * letters/numbers/CJK characters and replacing the rest with dashes.
 */
export function slugifyHeading(text: string): string {
  // Strip HTML tags from the token text (marked passes the inline-rendered HTML
  // for headings, e.g. when there are <code> spans inside), then decode numeric
  // character references so escaped chars (e.g. &#126; lone tilde) don't leak
  // their digits into the anchor id.
  const plain = String(text || '')
    .replace(/<[^>]+>/g, '')
    .replace(/&#(\d+);/g, (_m, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_m, n) => String.fromCodePoint(parseInt(n, 16)))
    .trim()
  return plain
    .toLowerCase()
    .replace(/[\s]+/g, '-')
    // Remove everything except word chars, dashes, and CJK Unified Ideographs.
    // Use '' (remove) not '-' (replace) to match GitHub-style anchor IDs.
    .replace(/[^\w\-\u4e00-\u9fff]+/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
}

let headingAutoIdCounter = 0

/**
 * Escape text for safe interpolation into HTML.
 */
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * Wrap a highlighted fenced code block in a container with a header that
 * carries the language label and a one-click copy button. The button is
 * wired through a document-level delegated listener (see useCodeBlockCopy.ts),
 * so it works in every v-html consumer, including content teleported to
 * <body> (e.g. the note preview popup).
 */
function wrapCodeBlock(lang: string, highlighted: string): string {
  const safeLang = (lang || '').replace(/[^A-Za-z0-9_+.-]/g, '')
  const label = escapeHtml(lang || 'text')
  return (
    '<div class="code-block">' +
    '<div class="code-block-header">' +
    `<span class="code-block-lang">${label}</span>` +
    '<button class="code-block-copy-btn" type="button" title="复制代码" aria-label="复制代码">' +
    '<svg class="cb-copy-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>' +
    '</svg>' +
    '<svg class="cb-check-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<polyline points="20 6 9 17 4 12"></polyline>' +
    '</svg>' +
    '</button>' +
    '</div>' +
    `<pre><code class="hljs${safeLang ? ` language-${safeLang}` : ''}">${highlighted}</code></pre>` +
    '</div>'
  )
}

function createRenderer() {
  const renderer = new marked.Renderer()

  renderer.code = function (token: any): string {
    const code = typeof token === 'string' ? token : (token.text || '')
    const lang = typeof token === 'object' && token.lang ? token.lang : ''

    // Mermaid block: render as placeholder div for later processing.
    // data-mermaid-id stays globally unique (module counter) — mermaid.render
    // and the note editors resolve it document-wide; data-mermaid-sig is the
    // CONTENT hash, stable across render passes, used by the streaming
    // renderer to adopt already-rendered blocks across re-renders.
    if (lang === 'mermaid') {
      const hash = code.split('').reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0)
      const id = `mermaid-${(hash >>> 0).toString(36)}-${mermaidIdCounter++}`
      const escaped = code
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
      return `<div class="mermaid-block" data-mermaid-id="${id}" data-mermaid-sig="${hash}" data-mermaid-source="${escaped}"></div>`
    }

    // ECharts block: a ```echarts fence containing a standard JSON ECharts
    // option object renders as an interactive chart (SVG renderer).
    if (lang === 'echarts') {
      const hash = code.split('').reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0)
      const id = `echarts-${(hash >>> 0).toString(36)}-${echartsIdCounter++}`
      const escaped = code
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
      return `<div class="echarts-block" data-echarts-id="${id}" data-echarts-sig="${hash}" data-echarts-source="${escaped}"></div>`
    }

    if (lang && hljs.getLanguage(lang)) {
      try {
        const highlighted = hljs.highlight(code, { language: lang }).value
        return wrapCodeBlock(lang, highlighted)
      } catch {
        return wrapCodeBlock(lang, escapeHtml(code))
      }
    }

    try {
      const highlighted = hljs.highlightAuto(code).value
      return wrapCodeBlock('', highlighted)
    } catch {
      return wrapCodeBlock('', escapeHtml(code))
    }
  }

  renderer.link = function (token: any): string {
    const rawHref = typeof token === 'object' ? (token.href || '') : ''
    const rawTitle = typeof token === 'object' ? (token.title || '') : ''
    const text = typeof token === 'object'
      ? (token.tokens ? (this as any).parser.parseInline(token.tokens) : (token.text || ''))
      : ''
    const href = String(rawHref).replace(/"/g, '&quot;')
    const title = String(rawTitle).replace(/"/g, '&quot;')
    if (href.startsWith('#')) {
      return DOMPurify.sanitize(`<a href="${href}"${title ? ` title="${title}"` : ''}>${text}</a>`)
    }
    return DOMPurify.sanitize(`<a href="${href}" target="_blank" rel="noopener noreferrer"${title ? ` title="${title}"` : ''}>${text}</a>`)
  }

  // Emit an id on every heading so in-page anchor links work in the note
  // preview (and any other v-html container). The id is also used by the
  // backend PDF exporter so that the TOC in exported PDFs is clickable.
  renderer.heading = function (token: any): string {
    const depth: number = typeof token === 'object' && token.depth ? token.depth : 1
    // marked@17 passes the heading token; fall back to .text for older shapes.
    const rawText = typeof token === 'object'
      ? (token.text ?? '')
      : String(token ?? '')
    // Render inline children so we preserve formatting inside the heading.
    const inner = typeof token === 'object' && token.tokens
      ? (this as any).parser.parseInline(token.tokens)
      : rawText
    const id = slugifyHeading(rawText) || `heading-${++headingAutoIdCounter}`
    const idAttr = ` id="${id}"`
    return `<h${depth}${idAttr}>${inner}</h${depth}>\n`
  }

  return renderer
}

// Create and configure a single shared renderer
const sharedRenderer = createRenderer()
marked.use({ renderer: sharedRenderer, breaks: true, gfm: true })

function renderMathSafe(tex: string, displayMode: boolean): string {
  try {
    const html = katex.renderToString(tex, {
      displayMode,
      throwOnError: false,
      output: 'html',
      strict: 'ignore',
    })
    const escaped = tex.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    const tag = displayMode ? 'div' : 'span'
    const innerTag = displayMode ? 'div' : 'span'
    return `<${tag} class="math-editable" data-tex="${escaped}" data-display-mode="${displayMode}"><${innerTag} class="math-rendered-content">${html}</${innerTag}><span class="math-controls"><button class="math-edit-btn" title="编辑公式"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button></span></${tag}>`
  } catch {
    const escaped = tex.replace(/</g, '&lt;').replace(/>/g, '&gt;')
    const tag = displayMode ? 'div' : 'span'
    return `<${tag} class="katex-error">${escaped}</${tag}>`
  }
}

/**
 * A `$`-delimited span whose interior carries any of these characters is
 * unmistakably LaTeX: a backslash (commands), caret (superscript), underscore
 * (subscript), or braces. Currency amounts and prose ("$49 起，$", "免费~$$",
 * "$100,000") never contain them, so they are the discriminator between real
 * math and money when a dollar-delimited span must be classified. Adopted
 * from Quilltap's markdown preprocessing (foundry-9/quilltap-server).
 */
const LATEX_MARKER_RE = /[\\^_{}]/

/**
 * A `$$…$$` display-math span is real math when its first non-empty line
 * carries a LaTeX marker or a Latin letter — anything that currency amounts
 * never contain. Content that fails (e.g. a line starting `$$99 元`,
 * `$$5 + 3$$`) is left for escapeCurrencyDollars to treat as money. Checking
 * only the FIRST line prevents a money run at line start from pairing with a
 * later formula's closing `$$` and swallowing the lines between them.
 */
function isRealDisplayMath(content: string): boolean {
  const firstLine = content.split('\n').find((l) => l.trim()) || ''
  return /[A-Za-z\\^_{}]/.test(firstLine)
}

/**
 * Escape `$` characters that are money/currency indicators, not math
 * delimiters. LLMs routinely emit `$` runs as cost levels in markdown tables
 * (`免费~$$`, `$$~$$$`, `$$$`, `$$$$`) and after a tilde (`~$`). Without this
 * step the math extractors below treat them as LaTeX delimiters: two `$$`
 * cells in different rows pair into a fake `$$…$$` display-math span that
 * swallows every table row between them, and the mangled remainder is dumped
 * as raw markdown via the katex-error fallback (conv 149ce886, 2026-08-01).
 *
 * Real display math is extracted by extractMath BEFORE this function runs
 * (line-anchored + LATEX_MARKER_RE validated), so the rules here only ever see
 * mid-line / non-math `$`. Each rule is marker-aware so genuine formulas that
 * reach this stage (e.g. inline `$2x+3$`, table cells `$W$` / `$\lambda$`)
 * keep their delimiters instead of being destroyed like the digit-starting
 * display formula `$$240.83\text{B 参数}...$$` (conv 1c7eb282, 2026-08-05).
 *
 * Escaping uses the HTML entity &#36;: no literal `$` char remains for the
 * math regexes, and marked/DOMPurify pass the entity through untouched so the
 * browser renders the original character. Code spans are protected by the
 * caller BEFORE this step, so `$` inside code is never affected.
 */
function escapeCurrencyDollars(text: string): string {
  let out = text
  // 1. `~$` / `~$$` / `~$$$`… — approximate amounts ("免费~$$", "$~$$$$").
  //    The tilde itself is escaped too: this marked build pairs two lone `~`
  //    in one paragraph as strikethrough, so `5~10 … 免费~$` would otherwise
  //    strike the text between the two tildes.
  out = out.replace(/\~\$+/g, (m) => m.replace(/~/g, '&#126;').replace(/\$/g, '&#36;'))
  // 1b. Integer money closed by its OWN `$` (`$200$`, `$1,000$` — coupon
  //     shapes 满$200$减50 / 九折$1,000$封顶): escape BOTH dollars. Rule 2
  //     below fires only on the opener (no digit follows the closer), and the
  //     surviving live `$` would open a fake inline-math pair with the NEXT
  //     real formula on the same line, swallowing CJK text (A4.9 R2:
  //     满$200$减50券，公式$x^2$标定 → span `减50券，公式` + katex error +
  //     $x^2$ destroyed). Decimal pairs ($6.6$) are the deliberate math
  //     shape and stay untouched; bare-integer math ($7$) is sacrificed to
  //     the money reading — documented tradeoff (coupon $ > integer math $).
  out = out.replace(/\$(\d+(?:[.,]\d+)*)\$(?![\d.,$])/g, (m, nums) => {
    if (/\./.test(nums)) return m
    return '&#36;' + nums + '&#36;'
  })
  // 2. Runs of 3+ dollars ("$$$", "$$$$"), or dollars followed by a digit-run
  //    that does NOT continue into LaTeX syntax — cost/price indicators
  //    ("$$99", "$49", "$1,000"). The digit-run must be followed by a
  //    non-letter/non-backslash char (or line end): `$49 起` is a price, but
  //    `$2x+3$` and `$240.83\text{...}` are real math — a price `$49` paired
  //    with a later lone `$` would otherwise swallow the text between them as
  //    a fake `$…$` inline-math span.
  //    Pure-numeric math is likewise real, but ONLY in the two shapes money
  //    never takes (A4.9 round-1 refinement — bare-integer `$200$` closes are
  //    adjacent-price money like `满$200$减50券` and must stay escaped):
  //      a) the first digit-run carries a DECIMAL POINT (`.` — thousands
  //      commas alone don't count, `$1,000$打九折` is money) — `$6.6$`,
  //      `$3.2$–$6.6$` (conv 88a13446 2026-08-26); or
  //      b) ≥1 arithmetic-op+digit group follows (`$2+3$`, `$10^2$`,
  //      `$1/2$`). The closing `$` itself must not be followed by a digit or
  //      another `$` (`$9.9$5元档`, `尾 $6.6$$结束` are not clean pairs).
  //    NOTE (assertion position): the prefix `\d+…` must live in its OWN
  //    lookahead, closed before the MATH negation — nested inside one `(?=…)`
  //    the prefix consumes the digits and the inner math shape (which starts
  //    with `\d+`) is evaluated at the wrong, already-advanced position and
  //    never matches (silent regression, caught by A1 unit case).
  out = out.replace(
    /\$\$\$+|\$\$+(?=\d+(?:[.,]\d+)*(?![\d.,])(?![A-Za-z\\]))|\$(?=\d+(?:[.,]\d+)*(?![\d.,])(?![A-Za-z\\]))(?!(?:\d+\.\d+(?:[.,]\d+)*|\d+(?:[.,]\d+)*(?:[+\-*/^=%]\d+(?:[.,]\d+)*(?![\d.,]))+)\$(?![\d.$]))/g,
    (m) => m.replace(/\$/g, '&#36;')
  )
  // 3. GFM table lines: escape the remaining `$` that is currency while
  //    keeping real inline-math cells intact. A paired `$…$` span is math
  //    when its interior carries a LaTeX marker or is a short letter variable
  //    ("$W$", "$s$"); otherwise ("$49 起，$", "免费~$$", "$100,000") every
  //    `$` on the line is escaped. Display math never reaches this step.
  const escapeTableCellDollars = (line: string): string =>
    line.replace(/\$([^$\n]+?)\$(?!\d)|\$/g, (m, inner) => {
      if (inner !== undefined) {
        if (LATEX_MARKER_RE.test(inner)) return m
        if (/^[A-Za-z][A-Za-z0-9]{0,2}$/.test(inner)) return m
      }
      return m.replace(/\$/g, '&#36;')
    })
  const lines = out.split('\n')
  let inTable = false
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const isSep = /^[\s|:-]+$/.test(line) && /---/.test(line) && /\|/.test(line)
    if (isSep) {
      // Header = previous non-empty line; separator + following non-blank
      // lines form the table body.
      let h = i - 1
      while (h >= 0 && !lines[h].trim()) h--
      if (h >= 0 && lines[h].includes('|')) lines[h] = escapeTableCellDollars(lines[h])
      lines[i] = line.replace(/\$/g, '&#36;')
      inTable = true
      continue
    }
    if (inTable) {
      if (!line.trim()) {
        inTable = false
        continue
      }
      if (line.includes('|')) {
        lines[i] = escapeTableCellDollars(line)
      } else {
        inTable = false
      }
    }
  }
  return lines.join('\n')
}

/**
 * Extract math segments and replace them with placeholders so markdown parsing
 * doesn't corrupt TeX syntax (backslashes, underscores, asterisks in equations).
 * Returns the protected text and a map of placeholder→rendered HTML.
 */
/**
 * Escape `~` runs of length EXACTLY one so they cannot be consumed as
 * strikethrough delimiters. marked 17 (like GitHub) accepts a lone `~` as a
 * valid strikethrough delimiter, so Chinese range/approximation notation
 * (`700~900 t/s`, `1.5~2.5x`, `~510 倍`) mis-pairs: any two lone tildes in
 * one paragraph strike everything between them and eat both tildes (conv
 * efaf8f9c — the user read "700~900" as a struck-through "700900"; during
 * streaming the 80ms re-parse makes the struck span flicker as text accumulates).
 * Runs of 2+ tildes (explicit `~~…~~`) are left untouched, so genuine
 * GFM strikethrough keeps working. Must run AFTER code/math extraction in
 * extractMath so placeholders — and the ~ inside them — are never touched.
 * (Sister to escapeCurrencyDollars' `~$` escape, which only covered the
 * currency case.)
 */
export function escapeLoneTildes(text: string): string {
  if (!text.includes('~')) return text
  return text.replace(/(?<!~)~(?!~)/g, '&#126;')
}

function extractMath(text: string): { text: string; placeholders: Record<string, string> } {
  const placeholders: Record<string, string> = {}
  const codePlaceholders: Record<string, string> = {}
  let counter = 0
  const key = () => `@@KTX${Date.now().toString(36)}${counter++}@@`
  const codeKey = () => `@@CDB${Date.now().toString(36)}${counter++}@@`

  let processed = text

  // Step 1: Protect fenced code blocks from math extraction
  processed = processed.replace(/```[\s\S]*?```/g, (match) => {
    const k = codeKey()
    codePlaceholders[k] = match
    return k
  })

  // Step 2: Protect inline code from math extraction
  processed = processed.replace(/`[^`\n]+`/g, (match) => {
    const k = codeKey()
    codePlaceholders[k] = match
    return k
  })

  // Step 2.5: `$$ ... $$` display math — extracted BEFORE the currency escaper
  // (which would otherwise destroy any formula whose content starts with a
  // digit, e.g. `$$240.83\text{B 参数}...$$` — conv 1c7eb282, 2026-08-05).
  // Two guards separate real formulas from money:
  //   1. LINE-ANCHORED: opening `$$` at line start, closing `$$` at line end
  //      (the shape the system prompt mandates for display formulas; also
  //      covers fence style `$$\n…\n$$` and attached fences
  //      `$$\begin{cases}…\end{cases}$$`). Money cells (`免费~$$`, `$$~$$$`)
  //      sit mid-line and can never form this shape, so they can't pair into
  //      a fake display-math span that eats table rows (conv 149ce886).
  //   2. isRealDisplayMath validation: a span whose first non-empty line is
  //      not mathish (e.g. a line starting `$$99 元`) is returned unchanged
  //      so escapeCurrencyDollars can escape it as money.
  // Pass 1 extracts single-line spans first so a rejected multi-line span
  // (pass 2) can never swallow a real formula on a later line.
  processed = processed.replace(/^\s*\$\$(.+?)\$\$\s*$/gm, (m, inner) => {
    if (!isRealDisplayMath(inner)) return m
    const k = key()
    placeholders[k] = renderMathSafe(inner.trim(), true)
    return `\n\n${k}\n\n`
  })
  processed = processed.replace(/^\s*\$\$([\s\S]+?)\$\$\s*$/gm, (m, inner) => {
    if (!isRealDisplayMath(inner)) return m
    const k = key()
    placeholders[k] = renderMathSafe(inner.trim(), true)
    return `\n\n${k}\n\n`
  })

  // \[ ... \] display math — also tolerate a broken closer written as a lone `]`
  // on its own line (LLMs occasionally drop the backslash), which would otherwise
  // swallow everything up to the next \] and break all rendering in between.
  processed = processed.replace(/\\\[([\s\S]+?)(?:\\\]|\n[ \t]*\][ \t]*(?=\n|$))/g, (_m, inner) => {
    const k = key()
    placeholders[k] = renderMathSafe(inner.trim(), true)
    return `\n\n${k}\n\n`
  })

  // Step 3: escape currency/money `$` (tables + prose) so cost levels are
  // never mistaken for LaTeX delimiters. Runs AFTER display-math extraction
  // and code protection, so real formulas and `$` inside code stay untouched.
  processed = escapeCurrencyDollars(processed)

  // \( ... \) inline math
  processed = processed.replace(/\\\(([\s\S]+?)\\\)/g, (_m, inner) => {
    const k = key()
    placeholders[k] = renderMathSafe(inner.trim(), false)
    return k
  })

  // $ ... $ inline math — avoid currency like "$5 and $6" / "$100"
  // Rule: non-space after opening $, non-space before closing $, no digit after closing $
  processed = processed.replace(
    /(^|[^\\$])\$(?!\s)([^\$\n]+?)(?<!\s)\$(?!\d)/g,
    (_m, pre, inner) => {
      const k = key()
      placeholders[k] = renderMathSafe(inner, false)
      return `${pre}${k}`
    }
  )

  // Step 3b: escape lone-tilde runs NOW (math + code are placeholders, so
  // their ~ are protected) — see escapeLoneTildes for why marked pairs them.
  processed = escapeLoneTildes(processed)

  // Step 3: Restore code blocks
  for (const [k, v] of Object.entries(codePlaceholders)) {
    processed = processed.split(k).join(v)
  }

  return { text: processed, placeholders }
}

function restoreMath(html: string, placeholders: Record<string, string>): string {
  let out = html
  for (const [k, v] of Object.entries(placeholders)) {
    // Strip a surrounding <p>…</p> that the markdown parser may have wrapped
    // around a lone display-math placeholder.
    out = out.split(`<p>${k}</p>`).join(v)
    out = out.split(k).join(v)
  }
  return out
}

function fixMarkdownTables(text: string): string {
  const lines = text.split('\n')
  const result: string[] = []

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // Detect table separator line: contains only |, -, :, and spaces, with at least one ---
    if (/^[\s|:-]+$/.test(line) && /---/.test(line) && /\|/.test(line)) {
      // Look back for the table header (previous non-empty line)
      let headerIdx = i - 1
      while (headerIdx >= 0 && !lines[headerIdx].trim()) {
        headerIdx--
      }

      if (headerIdx >= 0) {
        const headerLine = lines[headerIdx]
        const headerInner = headerLine.trim().replace(/^\||\|$/g, '')
        const headerCellCount = headerInner.split('|').length

        const sepInner = line.trim().replace(/^\||\|$/g, '')
        const sepCellCount = sepInner.split('|').length

        // If mismatch, rebuild separator with correct number of columns
        if (headerCellCount !== sepCellCount && headerCellCount > 0) {
          const fixedSep = '|' + '---|'.repeat(headerCellCount)
          result.push(fixedSep)
          continue
        }
      }
    }

    result.push(line)
  }

  return result.join('\n')
}

/**
 * When a heading line contains table-like pipe characters and is immediately
 * followed by a table separator line, the heading fix (adding space after #)
 * breaks the table because marked treats the line as a heading instead of a
 * table header. This function detects such cases, splits the heading from the
 * table header, and rebuilds the separator row with the correct column count.
 */
function fixHeadingBeforeTable(text: string): string {
  const lines = text.split('\n')
  const result: string[] = []

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // Check if this is a heading line (starts with 1-6 # followed by space)
    // and contains pipe characters
    if (/^#{1,6}\s/.test(line) && line.includes('|')) {
      // Check if next non-empty line is a table separator
      let nextIdx = i + 1
      while (nextIdx < lines.length && !lines[nextIdx].trim()) {
        nextIdx++
      }

      if (nextIdx < lines.length) {
        const nextLine = lines[nextIdx]
        if (/^[\s|:-]+$/.test(nextLine) && /---/.test(nextLine) && /\|/.test(nextLine)) {
          // Split heading from table header
          const hashMatch = line.match(/^(#{1,6}\s)(.*)$/)
          if (hashMatch) {
            const prefix = hashMatch[1]
            const rest = hashMatch[2]
            const firstPipeIdx = rest.indexOf('|')
            if (firstPipeIdx > 0) {
              const heading = prefix + rest.substring(0, firstPipeIdx).trim()
              let tableHeader = rest.substring(firstPipeIdx)

              const headerInner = tableHeader.trim().replace(/^\||\|$/g, '')
              const headerCells = headerInner.split('|').map(c => c.trim())
              tableHeader = '| ' + headerCells.join(' | ') + ' |'

              const sep = '| ' + headerCells.map(() => '---').join(' | ') + ' |'

              result.push(heading)
              result.push('')
              result.push(tableHeader)
              result.push(sep)

              // Skip the original separator line
              i = nextIdx
              continue
            }
          }
        }
      }
    }

    result.push(line)
  }

  return result.join('\n')
}

/**
 * CommonMark flanking-rule fix for CJK bold runs (spec 0.30 §6.2 Emphasis
 * and strong emphasis). A closing `**` that is preceded by punctuation and
 * directly followed by a CJK/word character is NOT right-flanking, so the
 * strong emphasis never closes and the whole `**…。**` pair renders as
 * literal asterisks. LLMs routinely emit `**结论。**接着写` with no space
 * after the closing run (markedjs/marked#3838, commonmark/commonmark-spec#650;
 * conv 42fd2cb8 had 46 such leaks). Mirror CASE for the OPENING run (spec
 * example 380 `a**"foo"**`): an opening `**` followed by punctuation (e.g. a
 * `“`/`"` quote) only opens if PRECEDED by whitespace or punctuation —
 * `理解为**“…”` (CJK letter before) is not left-flanking, so LLM output like
 * `理解为**"把离散的 token 序列…"**` renders as literal asterisks
 * (conv c38ed824, 2026-08-26). Both fixes insert the same invisible-in-CJK
 * space — after the closing run / before the opening run — to make the run
 * flanking-legal. Runs of 1 or 3+ `*` are left untouched, and `**` runs
 * inside code spans / math are already placeholder-protected by extractMath
 * before this runs.
 */
export function fixCjkBoldFlanking(text: string): string {
  let out = ''
  let open = false
  let i = 0
  while (i < text.length) {
    const ch = text[i]
    if (ch === '*') {
      let j = i
      while (j < text.length && text[j] === '*') j++
      const runLen = j - i
      if (runLen === 2) {
        if (open) {
          open = false
          const prev = i > 0 ? text[i - 1] : ''
          const next = j < text.length ? text[j] : ''
          if (prev && next && /\p{P}/u.test(prev) && /[\p{L}\p{N}]/u.test(next)) {
            out += '** '
          } else {
            out += '**'
          }
        } else {
          open = true
          // Opening-side flanking repair: `**` + punctuation + word, with a
          // CJK/word char directly before, is not left-flanking (spec 0.31
          // §6.2, example 380) so marked would print the asterisks literally.
          const oPrev = i > 0 ? text[i - 1] : ''
          const oNext = j < text.length ? text[j] : ''
          const oNext2 = j + 1 < text.length ? text[j + 1] : ''
          if (oPrev && oNext && oNext2 && /[\p{L}\p{N}]/u.test(oPrev) && /\p{P}/u.test(oNext) && /[\p{L}\p{N}]/u.test(oNext2)) {
            out += ' '
          }
          out += '**'
        }
      } else {
        out += text.slice(i, j)
      }
      i = j
      continue
    }
    if (ch === '\n') {
      // Emphasis cannot span paragraphs (blank line) or heading blocks.
      if (text[i + 1] === '\n' || /^\s{0,3}#{1,6}\s/.test(text.slice(i + 1))) {
        open = false
      }
      out += ch
      i++
      continue
    }
    out += ch
    i++
  }
  return out
}

function normalizeMarkdownSpacing(text: string): string {
  let out = fixCjkBoldFlanking(text)

  // Replace segment_split comments with blank lines (tool-call segment separator)
  out = out.replace(/<!--\s*segment_split\s*-->/g, '\n\n')

  // Fix markdown tables where separator column count doesn't match header
  out = fixMarkdownTables(out)

  // Basic heading fix: ensure space after # at line start
  // This is needed because some LLMs generate ##Title instead of ## Title
  // Use ^ anchor to avoid breaking anchor links like [text](#id) in markdown
  out = out.replace(/^(#{1,6})([^\s#\n])/gm, '$1 $2')

  // Fix heading followed by table: insert blank line so both render correctly
  out = fixHeadingBeforeTable(out)

  // DISABLED: Ensure space after list markers - can cause issues with some content
  // out = out.replace(/^([-*+])([^\s\-])/gm, '$1 $2')

  // DISABLED: Ensure space after ordered list numbers
  // out = out.replace(/^(\d+\.)([^\s])/gm, '$1 $2')

  // DISABLED: Collapse 3+ consecutive blank lines to exactly two
  // out = out.replace(/\n{3,}/g, '\n\n')

  return out
}

/**
 * Decode HTML entities encoded by the mermaid placeholder renderer.
 */
function decodeMermaidSource(src: string): string {
  return src
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
}

/**
 * Clean up corrupted note content where mermaid blocks were saved as raw HTML
 * instead of markdown code fences. Extracts data-mermaid-source and rebuilds
 * proper ```mermaid fences, discarding the bulky SVG payload.
 *
 * Uses the browser's DOMParser so nested <div> structures, attribute order,
 * and HTML entities inside the source attribute are handled robustly. Falls
 * back to the original content on any parse error so we never lose data.
 */
function cleanupEmbeddedMermaidHtml(content: string): string {
  if (content.length < 100) return content

  // Quick check: does the content contain embedded mermaid blocks?
  if (!/<div[^>]*class="[^"]*mermaid-block[^"]*"/i.test(content)) return content

  try {
    const parser = new DOMParser()
    const doc = parser.parseFromString(content, 'text/html')

    function convertNode(node: Node): boolean {
      if (node.nodeType !== Node.ELEMENT_NODE) return false
      const el = node as HTMLElement
      if (el.tagName.toLowerCase() !== 'div' || !el.classList.contains('mermaid-block')) {
        // Recurse into children first so inner mermaid blocks are handled.
        const children = Array.from(el.childNodes)
        let changed = false
        for (const child of children) {
          if (convertNode(child)) changed = true
        }
        return changed
      }

      const src = el.getAttribute('data-mermaid-source') || ''
      const decoded = decodeMermaidSource(src)
      const replacement = document.createTextNode('\n```mermaid\n' + decoded + '\n```\n')
      el.parentNode?.replaceChild(replacement, el)
      return true
    }

    // Process the body contents. Top-level mermaid blocks as well as nested
    // ones (e.g. inside a <figure>) are converted back to markdown fences.
    convertNode(doc.body)

    // Serialize back to HTML string. innerHTML strips the wrapping <body> tags.
    return doc.body.innerHTML
  } catch {
    // If DOM parsing fails for any reason, preserve the original content.
    return content
  }
}

function splitOlContainingHeadings(html: string): string {
  // Inert <template> parse — a detached <div> would fetch <img> srcs (see
  // rewriteImageUrls).
  const tpl = document.createElement('template')
  tpl.innerHTML = html
  const container = tpl.content
  const ols = Array.from(container.querySelectorAll('ol'))
  for (const ol of ols) {
    const allLi = Array.from(ol.children).filter(c => (c as HTMLElement).tagName === 'LI') as HTMLElement[]
    for (const li of allLi) {
      if (!li.parentNode) continue
      const heading = li.querySelector('h1, h2, h3, h4, h5, h6') as HTMLElement | null
      if (!heading) continue
      const newH2 = heading.cloneNode(true) as HTMLElement
      const newOl = document.createElement('ol')
      let nextLi = li.nextElementSibling as HTMLElement | null
      while (nextLi && nextLi.tagName === 'LI') {
        const toMove = nextLi
        nextLi = nextLi.nextElementSibling as HTMLElement | null
        newOl.appendChild(toMove)
      }
      if (newOl.children.length === 0) {
        ol.parentNode?.insertBefore(newH2, li.nextSibling)
      } else {
        ol.parentNode?.insertBefore(newH2, li.nextSibling)
        ol.parentNode?.insertBefore(newOl, newH2.nextSibling)
      }
      li.parentNode?.removeChild(li)
    }
    if (ol.children.length === 0) {
      ol.parentNode?.removeChild(ol)
    }
  }
  return serializeFragment(container)
}


function isFilesystemPath(src: string): boolean {
  if (!src) return false
  if (src.startsWith('http://') || src.startsWith('https://') || src.startsWith('data:') || src.startsWith('blob:')) {
    return false
  }
  if (src.startsWith('/api/files/')) return false
  return true
}

/**
 * Third-party embed allowlist. Only these hosts' OFFICIAL embed endpoints
 * (the platforms' sanctioned share/embed links — YouTube `/embed/<id>`,
 * Bilibili `player.bilibili.com/player.html?bvid=...`) may be rendered as
 * <iframe> in chat/notes. Everything else is dropped at the source so a
 * hostile iframe never survives into the DOM. Embedding via these official
 * links is legal and expected (the user-facing contract: 平台官方分享内嵌
 * 链接是合法的，agent 应优先使用).
 */
const ALLOWED_EMBED_HOSTS: Array<{ host: string; test: (u: URL) => boolean }> = [
  {
    host: 'youtube.com',
    test: (u) => u.pathname.startsWith('/embed/') && /^[\w-]{6,20}$/.test(u.pathname.slice(7)),
  },
  {
    host: 'youtube-nocookie.com',
    test: (u) => u.pathname.startsWith('/embed/') && /^[\w-]{6,20}$/.test(u.pathname.slice(7)),
  },
  {
    host: 'player.bilibili.com',
    test: (u) => u.pathname === '/player.html' && /^BV[0-9A-Za-z]{10,12}$/.test(u.searchParams.get('bvid') || ''),
  },
]

const IFRAME_TAG_RE = /<iframe\b[^>]*>/gi

/**
 * Validate an iframe src against the embed allowlist and return the
 * canonical safe <iframe> markup, or null when the src is not allowlisted.
 */
function buildSafeIframe(src: string): string | null {
  let s = String(src || '').trim()
  if (!s) return null
  if (s.startsWith('//')) s = 'https:' + s
  let url: URL
  try {
    url = new URL(s)
  } catch {
    return null
  }
  if (url.protocol !== 'https:') return null
  const host = url.hostname.toLowerCase()
  const rule = ALLOWED_EMBED_HOSTS.find((r) => host === r.host || host.endsWith('.' + r.host))
  if (!rule || !rule.test(url)) return null
  const safeSrc = url.toString().replace(/"/g, '&quot;')
  return (
    `<iframe src="${safeSrc}" width="560" height="315" frameborder="0"` +
    ' allowfullscreen loading="lazy" referrerpolicy="no-referrer"></iframe>'
  )
}

/**
 * Extract <iframe> elements from markdown source BEFORE parsing/sanitizing:
 * allowlisted embeds become placeholders restored after DOMPurify (which
 * would otherwise strip every iframe); non-allowlisted iframes are removed
 * entirely. Returns the protected text and the placeholder map.
 */
function extractSafeIframes(text: string): { text: string; placeholders: Record<string, string> } {
  const placeholders: Record<string, string> = {}
  let counter = 0
  let processed = text
  processed = processed.replace(IFRAME_TAG_RE, (match) => {
    const srcMatch = match.match(/\bsrc\s*=\s*["']([^"']+)["']/i)
    const src = srcMatch ? srcMatch[1] : ''
    const safe = buildSafeIframe(src)
    if (safe) {
      const key = `@@IFRAME${Date.now().toString(36)}${counter++}@@`
      placeholders[key] = safe
      return key
    }
    // Hostile / unparseable iframe: drop the tag (the stray </iframe>, if
    // any, is removed by DOMPurify).
    return ''
  })
  return { text: processed, placeholders }
}

function restoreSafeIframes(html: string, placeholders: Record<string, string>): string {
  let out = html
  for (const [k, v] of Object.entries(placeholders)) {
    out = out.split(`<p>${k}</p>`).join(v)
    out = out.split(k).join(v)
  }
  return out
}

function rewriteImageUrls(html: string): string {
  if (!html) return html
  try {
    // Parse into an INERT <template>: a plain detached <div>.innerHTML would
    // still fetch every <img src> it contains (detached images load), firing
    // a duplicate remote request for hotlinked images and a junk
    // /app/frontend/media/... request for workspace-relative localized paths
    // on EVERY render pass. Template contents never load subresources.
    const tpl = document.createElement('template')
    tpl.innerHTML = html
    const container = tpl.content
    const imgs = container.querySelectorAll('img')
    if (imgs.length === 0) return html
    const token = localStorage.getItem('chatllm_token')
    if (!token) return html
    let changed = false
    imgs.forEach((img) => {
      const src = img.getAttribute('src') || ''
      if (isFilesystemPath(src)) {
        // Keep the canonical path in data-original-src BEFORE swapping in the
        // signed URL — otherwise the token URL becomes "original" downstream
        // (processImagesInEditor) and gets persisted into the note on save.
        if (!img.hasAttribute('data-original-src')) {
          img.setAttribute('data-original-src', src)
        }
        img.setAttribute('src', `/api/files/download?path=${encodeURIComponent(src)}&token=${encodeURIComponent(token)}`)
        changed = true
      }
    })
    return changed ? serializeFragment(container) : html
  } catch {
    return html
  }
}

function serializeFragment(frag: DocumentFragment): string {
  let out = ''
  frag.childNodes.forEach((n) => {
    if (n instanceof Element) {
      out += n.outerHTML
    } else if (n.nodeType === 8) {
      // Comment node — textContent lacks the <!-- --> wrapper; restore it
      // (`--` inside is invalid in HTML comments, neutralize defensively).
      out += `<!--${(n.textContent || '').replace(/--/g, '- -')}-->`
    } else {
      // Raw text node — re-escape so entities don't double-unescape on the
      // next parse.
      out += (n.textContent || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
    }
  })
  return out
}

/**
 * Rewrite local filesystem paths in <audio>/<video> src attributes (and
 * their <source src> children) to signed /api/files/download URLs (Bearer
 * headers cannot attach to media elements, same auth problem as <img>).
 * Mirrors rewriteImageUrls. <picture> sources use srcset (never src) and
 * are left untouched.
 */
function rewriteMediaUrls(html: string): string {
  if (!html) return html
  try {
    // Inert <template> parse (see rewriteImageUrls) — a detached <div> would
    // fetch every <video>/<audio>/<source> src during each render pass.
    const tpl = document.createElement('template')
    tpl.innerHTML = html
    const container = tpl.content
    const media = container.querySelectorAll('audio, video')
    if (media.length === 0) return html
    const token = localStorage.getItem('chatllm_token')
    if (!token) return html
    let changed = false
    const sign = (el: Element, attr: string): void => {
      const src = el.getAttribute(attr) || ''
      if (isFilesystemPath(src)) {
        if (!el.hasAttribute('data-original-src')) {
          el.setAttribute('data-original-src', src)
        }
        el.setAttribute(attr, `/api/files/download?path=${encodeURIComponent(src)}&token=${encodeURIComponent(token)}`)
        changed = true
      }
    }
    media.forEach((el) => {
      sign(el, 'src')
      el.querySelectorAll('source[src]').forEach((s) => sign(s, 'src'))
    })
    return changed ? serializeFragment(container) : html
  } catch {
    return html
  }
}

// Notes saved by older builds may already contain signed download URLs
// (/api/files/download?path=X&token=...) instead of the canonical path X.
// Normalize back so re-saving heals them instead of persisting expiring tokens.
function canonicalImageSrc(src: string): string {
  if (src.startsWith('/api/files/download')) {
    try {
      const u = new URL(src, window.location.origin)
      const p = u.searchParams.get('path')
      if (p) return p
    } catch { /* keep src */ }
  }
  return src
}

export function renderMarkdownToHtml(content: string): string {
  if (!content) return ''
  const cached = getCachedRender(content)
  if (cached !== undefined) return cached
  try {
    headingAutoIdCounter = 0
    const dsmlCleaned = stripDsmlTags(content)
    const cleaned = cleanupEmbeddedMermaidHtml(dsmlCleaned)
    const { text, placeholders } = extractMath(cleaned)
    const { text: iframeText, placeholders: iframePlaceholders } = extractSafeIframes(text)
    const normalized = normalizeMarkdownSpacing(iframeText)
    const html = marked.parse(normalized, { async: false }) as string
    const processed = splitOlContainingHeadings(html)
    const restored = restoreMath(processed, placeholders)
    const sanitized = DOMPurify.sanitize(restored, {
      ALLOW_DATA_ATTR: true,
      ADD_ATTR: ['target', 'rel', 'style'],
      ADD_TAGS: ['button'],
    })
    const withIframes = restoreSafeIframes(sanitized, iframePlaceholders)
    const rewritten = rewriteImageUrls(withIframes)
    const mediaRewritten = rewriteMediaUrls(rewritten)
    setCachedRender(content, mediaRewritten)
    return mediaRewritten
  } catch {
    return content
  }
}

export function addCitationSuperscripts(html: string, hasResults: boolean, validSet?: Set<number>): string {
  if (!hasResults) return html
  const parts = html.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>|<a[\s\S]*?<\/a>)/gi)
  return parts
    .map((part, i) => {
      if (i % 2 === 1) return part
      return part.replace(/\[(\d{1,3})\]/g, (_match, num) => {
        const n = parseInt(num, 10)
        // Only convert [N] markers that reference an actual search result.
        // Without this filter, a fabricated out-of-range [9] (5 results)
        // renders as a dead, non-clickable superscript (grounded-citations
        // integrity fix — the backend sanitizes the persisted content, this
        // keeps the live stream honest).
        if (validSet && !validSet.has(n)) return _match
        return `<sup class="citation-ref" data-cite-index="${n}">[${n}]</sup>`
      })
    })
    .join('')
}

export function renumberCitationSuperscripts(html: string, numMap: Map<number, number>): string {
  if (numMap.size === 0) return html
  return html.replace(
    /<sup[^>]*\bdata-cite-index="(\d{1,3})"[^>]*>\[(\d{1,3})\]<\/sup>/g,
    (_match, oldNum: string, _displayNum: string) => {
      const old = parseInt(oldNum, 10)
      const newNum = numMap.get(old)
      if (newNum !== undefined) {
        return `<sup class="citation-ref" data-cite-index="${newNum}">[${newNum}]</sup>`
      }
      return _match
    }
  )
}

export function processIncompleteMarkdown(content: string): string {
  let processed = stripDsmlTags(content)

  // Close unclosed code blocks so they render properly during streaming
  const codeBlockMatches = processed.match(/```/g)
  if (codeBlockMatches && codeBlockMatches.length % 2 !== 0) {
    processed += '\n```'
  }

  // Close unclosed inline code spans
  const inlineCodeMatches = processed.match(/(?<!`)`(?!`)/g)
  if (inlineCodeMatches && inlineCodeMatches.length % 2 !== 0) {
    processed += '`'
  }

  // Replace segment_split comments with blank lines (tool-call segment separator)
  processed = processed.replace(/<!--\s*segment_split\s*-->/g, '\n\n')

  // Fix markdown tables where separator column count doesn't match header
  processed = fixMarkdownTables(processed)

  // Basic heading fix: ensure space after #
  // This is needed because some LLMs generate ##Title instead of ## Title
  processed = processed.replace(/(#{1,6})([^\s#\n])/g, '$1 $2')

  // Fix heading followed by table: insert blank line so both render correctly
  processed = fixHeadingBeforeTable(processed)

  return processed
}

/**
 * Render all mermaid placeholder blocks within a container element.
 * Call this via nextTick after v-html content is updated.
 */
export async function renderMermaidBlocks(container: HTMLElement | null) {
  if (!container) return

  attachMathEditListeners(container)

  const blocks = container.querySelectorAll<HTMLElement>('.mermaid-block:not(.mermaid-rendered)')
  if (blocks.length === 0) return

  ensureMermaidInit()

  for (const block of blocks) {
    await renderSingleMermaidBlock(block)
  }
}

/**
 * Render a single mermaid block. Used for targeted re-rendering after edits.
 */
export async function renderSingleMermaidBlock(block: HTMLElement) {
  const source = block.dataset.mermaidSource
  const id = block.dataset.mermaidId
  if (!source || !id) return

  ensureMermaidInit()

  const decoded = source
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')

  const cachedSvg = getCachedMermaidSvg(decoded)
  if (cachedSvg) {
    applyMermaidSvgToBlock(block, id, decoded, cachedSvg)
    return
  }

  try {
    const svg = await enqueueMermaidRender(async () => {
      const existingEl = document.getElementById(id)
      if (existingEl) existingEl.remove()
      cleanupMermaidTempElements()
      let lastErr: unknown
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const result = await mermaid.render(id, decoded)
          return result.svg
        } catch (e) {
          lastErr = e
          const stale = document.getElementById(id)
          if (stale) stale.remove()
          cleanupMermaidTempElements()
        }
      }
      throw lastErr
    })

    if (!document.body.contains(block)) return

    setCachedMermaidSvg(decoded, svg)
    applyMermaidSvgToBlock(block, id, decoded, svg)
  } catch (err) {
    if (!document.body.contains(block)) return
    block.innerHTML = `<pre class="mermaid-error"><code>${source}</code></pre>`
    block.classList.add('mermaid-rendered')
  }
}

function applyMermaidSvgToBlock(block: HTMLElement, id: string, decoded: string, svg: string) {
  if (!document.body.contains(block)) return
  block.innerHTML = `
    <div class="mermaid-rendered-content">${svg}</div>
    <div class="mermaid-controls" contenteditable="false">
      <button class="mermaid-edit-btn" title="查看代码" contenteditable="false">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="16 18 22 12 16 6"/>
          <polyline points="8 6 2 12 8 18"/>
        </svg>
      </button>
      <button class="mermaid-zoom-btn" title="放大" contenteditable="false">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="15 3 21 3 21 9"/>
          <polyline points="9 21 3 21 3 15"/>
          <line x1="21" y1="3" x2="14" y2="10"/>
          <line x1="3" y1="21" x2="10" y2="14"/>
        </svg>
      </button>
    </div>
  `
  block.setAttribute('data-cached-svg', svg)
  block.classList.add('mermaid-rendered')
  block.setAttribute('contenteditable', 'false')

  const editBtn = block.querySelector('.mermaid-edit-btn')
  const zoomBtn = block.querySelector('.mermaid-zoom-btn')

  if (editBtn) {
    editBtn.setAttribute('data-mermaid-action', 'edit')
    editBtn.setAttribute('data-mermaid-block-id', id)
    editBtn.addEventListener('mousedown', (e) => {
      e.preventDefault()
      e.stopPropagation()
      const event = new CustomEvent('mermaid-edit', {
        detail: { source: decoded, id, handled: false },
        bubbles: true
      })
      block.dispatchEvent(event)
    })
  }

  if (zoomBtn) {
    zoomBtn.setAttribute('data-mermaid-action', 'zoom')
    zoomBtn.setAttribute('data-mermaid-block-id', id)
    zoomBtn.addEventListener('mousedown', (e) => {
      e.preventDefault()
      e.stopPropagation()
      const event = new CustomEvent('mermaid-zoom', {
        detail: { source: decoded, id, svg, handled: false },
        bubbles: true
      })
      block.dispatchEvent(event)
    })
  }
}

export function fixLostMermaidBlocks(container: HTMLElement | null) {
  if (!container) return
  const blocks = container.querySelectorAll<HTMLElement>('.mermaid-block.mermaid-rendered')
  let needsFix = false
  blocks.forEach((block) => {
    const renderedContent = block.querySelector('.mermaid-rendered-content')
    const hasSvg = renderedContent && renderedContent.querySelector('svg')
    if (!renderedContent || !hasSvg) {
      const cachedSvg = block.getAttribute('data-cached-svg')
      if (cachedSvg) {
        block.innerHTML = `
          <div class="mermaid-rendered-content">${cachedSvg}</div>
          <div class="mermaid-controls" contenteditable="false">
            <button class="mermaid-edit-btn" title="查看代码" contenteditable="false">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="16 18 22 12 16 6"/>
                <polyline points="8 6 2 12 8 18"/>
              </svg>
            </button>
            <button class="mermaid-zoom-btn" title="放大" contenteditable="false">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="15 3 21 3 21 9"/>
                <polyline points="9 21 3 21 3 15"/>
                <line x1="21" y1="3" x2="14" y2="10"/>
                <line x1="3" y1="21" x2="10" y2="14"/>
              </svg>
            </button>
          </div>
        `
        block.setAttribute('contenteditable', 'false')
        const source = block.dataset.mermaidSource || ''
        const decoded = source
          .replace(/&amp;/g, '&')
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>')
          .replace(/&quot;/g, '"')
        const id = block.dataset.mermaidId || ''
        const editBtn = block.querySelector('.mermaid-edit-btn')
        const zoomBtn = block.querySelector('.mermaid-zoom-btn')
        if (editBtn) {
          editBtn.setAttribute('data-mermaid-action', 'edit')
          editBtn.setAttribute('data-mermaid-block-id', id)
          editBtn.addEventListener('mousedown', (e) => {
            e.preventDefault()
            e.stopPropagation()
            block.dispatchEvent(new CustomEvent('mermaid-edit', {
              detail: { source: decoded, id, handled: false },
              bubbles: true
            }))
          })
        }
        if (zoomBtn) {
          zoomBtn.setAttribute('data-mermaid-action', 'zoom')
          zoomBtn.setAttribute('data-mermaid-block-id', id)
          zoomBtn.addEventListener('mousedown', (e) => {
            e.preventDefault()
            e.stopPropagation()
            block.dispatchEvent(new CustomEvent('mermaid-zoom', {
              detail: { source: decoded, id, svg: cachedSvg, handled: false },
              bubbles: true
            }))
          })
        }
      } else {
        block.classList.remove('mermaid-rendered')
        needsFix = true
      }
    }
  })
  if (needsFix) {
    renderMermaidBlocks(container)
  }
}

/**
 * Render all ECharts placeholder blocks within a container element.
 * Each ```echarts fence parsed as JSON becomes an interactive SVG chart.
 * Call this via nextTick after v-html content is updated.
 */
export async function renderEchartsBlocks(container: HTMLElement | null) {
  if (!container) return
  sweepEchartsInstances()
  const blocks = container.querySelectorAll<HTMLElement>('.echarts-block:not(.echarts-rendered)')
  if (blocks.length === 0) return
  for (const block of blocks) {
    renderSingleEchartsBlock(block)
  }
}

/**
 * Dispose chart instances whose DOM node was removed from the document
 * (v-html re-render, virtual-scroll remount, editor round-trips). ECharts
 * keeps every init'd chart in a module-level registry until dispose() —
 * without this sweep long conversations leak dozens of instances.
 */
function sweepEchartsInstances() {
  for (const chart of [...liveEchartsInstances]) {
    if (chart.isDisposed()) {
      liveEchartsInstances.delete(chart)
      continue
    }
    const dom = chart.getDom()
    if (!dom || !dom.isConnected) {
      chart.dispose()
      liveEchartsInstances.delete(chart)
    }
  }
}

function escapedForHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * Render a single ECharts block: parse the JSON option, init echarts (SVG
 * renderer) directly on the placeholder div. On parse/render failure the
 * block shows the raw source as a styled error <pre>.
 */
export function renderSingleEchartsBlock(block: HTMLElement) {
  const source = block.dataset.echartsSource
  if (!source) return
  // dataset already decodes the attribute entities once; decoding again
  // would corrupt option strings containing literal &quot;/&lt; text and
  // make JSON.parse fail
  const decoded = source

  let option: any
  try {
    option = JSON.parse(decoded)
  } catch (e) {
    if (!document.body.contains(block)) return
    block.innerHTML = `<pre class="echarts-error"><code>${escapedForHtml(decoded)}</code></pre>`
    block.classList.add('echarts-rendered')
    return
  }

  try {
    const existing = echarts.getInstanceByDom(block)
    if (existing && !existing.isDisposed()) existing.dispose()
    // animation: false — charts render to their final state instantly and
    // deterministically. Entry animations restart on every v-html re-render
    // during streaming and left some browsers with a stuck pre-animation
    // paint state (reported: charts black until hover).
    option.animation = false
    // Top spacing: LLM-authored options usually omit `grid`, and ECharts'
    // default grid.top=60 fits a title but not the labels that render above
    // the plot — markPoint labels default to position 'top' (overflowing the
    // grid) and markArea labels anchor above the area top, colliding with the
    // title. With a title + such labels + unpinned grid.top, reserve space
    // (mirrors backend _echarts_apply_top_spacing_defaults). Must run BEFORE
    // the legend-defaults block below so the legend is still unpinned here.
    const title = option.title
    if (title && (typeof title === 'object' ? !Array.isArray(title) : typeof title === 'string') && Array.isArray(option.series)) {
      const needsTopSpace = option.series.some((s: any) => {
        if (!s || typeof s !== 'object') return false
        const mp = s.markPoint
        if (mp && typeof mp === 'object' && Array.isArray(mp.data) && mp.data.length) return true
        const ma = s.markArea
        if (!(ma && typeof ma === 'object' && Array.isArray(ma.data) && ma.data.length)) return false
        return ma.data.some((pair: any) => {
          const items = Array.isArray(pair) ? pair : [pair]
          return items.some((it: any) => it && typeof it === 'object' && (it.name !== undefined || it.label !== undefined))
        })
      })
      const lg = option.legend
      const legendBelowTitle = !!(
        lg && typeof lg === 'object' && !Array.isArray(lg) &&
        lg.orient !== 'vertical' && lg.top === undefined && lg.bottom === undefined
      )
      if (needsTopSpace || legendBelowTitle) {
        let needed = 40
        if (legendBelowTitle) needed += 25
        needed += needsTopSpace ? 50 : 15
        if (needed > 60) {
          if (option.grid == null) option.grid = {}
          if (typeof option.grid === 'object' && !Array.isArray(option.grid) && option.grid.top === undefined) {
            option.grid.top = needed
          }
        }
      }
    }
    // ECharts 6 resolves the legend default (`top: 'auto'`) to the BOTTOM of
    // the container — legends overlap the plot. Restore the classic ECharts-5
    // placement unless the option pins it explicitly (mirrors the backend
    // _echarts_apply_legend_defaults so UI and exports match). A present key
    // (including null) is preserved: only absent keys get defaults. When the
    // option also has a title, a horizontal legend goes BELOW the title block
    // (top: 40) instead of the very top — at top: 0 the legend glyphs collide
    // with the title (legend baseline y=11.6 vs title glyph top y≈12.8, title
    // drawn last paints over the legend).
    if (option.legend && typeof option.legend === 'object' && !Array.isArray(option.legend)) {
      const lg = option.legend
      const vertical = lg.orient === 'vertical'
      if (lg.top === undefined && lg.bottom === undefined) {
        if (!vertical && title && (typeof title === 'object' || typeof title === 'string')) {
          lg.top = 40
        } else {
          lg.top = vertical ? 'middle' : 0
        }
      }
      if (vertical && lg.left === undefined && lg.right === undefined) {
        lg.right = 0
      }
    }
    // Series names from legend.data: ECharts 6 hides legend items whose name
    // matches no series — LLM-authored options often set legend.data while
    // leaving series[].name unset, silently dropping the whole legend
    // (mirrors backend _echarts_apply_legend_series_names).
    if (option.legend && typeof option.legend === 'object' && Array.isArray(option.legend.data)) {
      const ld = option.legend.data
      if (Array.isArray(option.series)) {
        option.series.forEach((s: any, i: number) => {
          if (!s || typeof s !== 'object') return
          if (s.type === 'pie' || s.type === 'radar') return
          if (s.name == null && typeof ld[i] === 'string') {
            s.name = ld[i]
          }
        })
      }
    }
    const width = Math.max(260, Math.min(block.clientWidth || 640, 720))
    const chart = echarts.init(block, null, { renderer: 'svg', width, height: 360 })
    chart.setOption(option, { notMerge: true })
    liveEchartsInstances.add(chart)
    block.classList.add('echarts-rendered')
    // keep the caret out of the chart (same as mermaid blocks): typing in a
    // note editor must never land inside the rendered SVG
    block.setAttribute('contenteditable', 'false')
  } catch (err) {
    if (!document.body.contains(block)) return
    block.innerHTML = `<pre class="echarts-error"><code>${escapedForHtml(decoded)}</code></pre>`
    block.classList.add('echarts-rendered')
  }
}

/**
 * Restore ECharts charts lost when Vue re-renders the v-html container
 * (echarts-injected SVG is not part of the rendered markup). Re-initializes
 * from the preserved data-echarts-source.
 */
export function fixLostEchartsBlocks(container: HTMLElement | null) {
  if (!container) return
  const blocks = container.querySelectorAll<HTMLElement>('.echarts-block.echarts-rendered')
  blocks.forEach((block) => {
    if (block.querySelector('.echarts-error')) return
    if (!block.querySelector('svg')) {
      block.classList.remove('echarts-rendered')
      block.innerHTML = ''
      renderSingleEchartsBlock(block)
    }
  })
}

export function attachMathEditListeners(container: HTMLElement) {
  const mathElements = container.querySelectorAll('.math-editable:not([data-math-edit-attached])')
  mathElements.forEach((el) => {
    el.setAttribute('data-math-edit-attached', 'true')
    const htmlEl = el as HTMLElement
    const editBtn = htmlEl.querySelector('.math-edit-btn')
    if (editBtn) {
      editBtn.addEventListener('mousedown', (e) => {
        e.stopPropagation()
        e.preventDefault()
        const tex = htmlEl.getAttribute('data-tex') || ''
        const displayMode = htmlEl.getAttribute('data-display-mode') === 'true'
        const event = new CustomEvent('math-edit', {
          detail: { tex, displayMode, element: htmlEl },
          bubbles: true
        })
        htmlEl.dispatchEvent(event)
      })
    }
  })
}

export { katex as katexModule }

/**
 * Helper: render mermaid blocks on next tick for a ref element.
 */
export function renderMermaidOnNextTick(getContainer: () => HTMLElement | null) {
  nextTick(() => {
    renderMermaidBlocks(getContainer())
  })
}

/**
 * Convert HTML to markdown string.
 * This is a simplified converter for WYSIWYG editing.
 */
export function htmlToMarkdown(html: string): string {
  if (!html) return ''

  const container = document.createElement('div')
  container.innerHTML = html

  let markdown = ''

  // Zero-width characters (U+200B-200D, BOM) sneak in from pasted content and
  // from the render-side CJK bold flanking fix; strip them on serialize so the
  // saved markdown stays clean (they carry no visible meaning).
  function stripZeroWidth(s: string): string {
    return s.replace(/[\u200b-\u200d\ufeff]/g, '')
  }

  // Escape markdown-active characters in plain text so user-typed symbols
  // (e.g. "2 * 3", "a_b", "1 < 2") do not turn into emphasis/links/tags on
  // the next render cycle. Square brackets and tilde use HTML entities —
  // backslash-escaped "\[" would be mistaken for a LaTeX \[...\] delimiter
  // by extractMath, and marked does not honor "\~" for strikethrough.
  function escapeMdText(s: string): string {
    return stripZeroWidth(s)
      .replace(/&/g, '&amp;')
      .replace(/[\\`*_<>|#]/g, (ch) => '\\' + ch)
      .replace(/\[/g, '&#91;')
      .replace(/\]/g, '&#93;')
      .replace(/~/g, '&#126;')
  }

  // CommonMark flanking rules (simplified): if a `**`/`*` delimiter run
  // cannot open or close emphasis at this position (e.g. `<strong>粗。</strong>好`
  // would serialize to `**粗。**好`, which marked will NOT parse as bold),
  // emit raw inline HTML instead — it round-trips stably.
  const PUNCT_RE = /[\p{P}\p{S}]/u
  const isWsChar = (ch: string) => ch === '' || /\s/.test(ch)
  const isPunctChar = (ch: string) => ch !== '' && PUNCT_RE.test(ch)

  function canOpenEm(firstInner: string, prev: string): boolean {
    if (firstInner === '' || /\s/.test(firstInner)) return false
    if (isPunctChar(firstInner) && !(isWsChar(prev) || isPunctChar(prev))) return false
    return true
  }

  function canCloseEm(lastInner: string, next: string): boolean {
    if (lastInner === '' || /\s/.test(lastInner)) return false
    if (isPunctChar(lastInner) && !(isWsChar(next) || isPunctChar(next))) return false
    return true
  }

  interface SiblingCtx { prev: string; next: string }

  // Pasted content often styles the BLOCK element itself (e.g. Word's
  // <p style="font-weight:bold;color:red">). Fold those into the serialized
  // content so block-level bold/italic/underline/strike is not dropped.
  function blockEmphasisWrap(el: HTMLElement, inner: string, asHtml: boolean, ctx: SiblingCtx): string {
    const fw = (el.style.fontWeight || '').toLowerCase()
    const bold = fw === 'bold' || fw === 'bolder' || parseInt(fw, 10) >= 600
    const italic = /italic|oblique/.test((el.style.fontStyle || '').toLowerCase())
    const deco = ((el.style.textDecoration || '') + ' ' + (el.style.textDecorationLine || '')).toLowerCase()
    let out = inner
    if (/line-through/.test(deco)) out = '<s>' + out + '</s>'
    if (/underline/.test(deco)) out = '<u>' + out + '</u>'
    if (italic) out = asHtml ? '<em>' + out + '</em>' : emitEmphasis(el, '*', 'em', ctx, out)
    if (bold) out = asHtml ? '<strong>' + out + '</strong>' : emitEmphasis(el, '**', 'strong', ctx, out)
    return out
  }

  function blockColorStyleParts(el: HTMLElement, parts: string[]) {
    const color = el.style.color || ''
    const bg = el.style.backgroundColor || ''
    if (color) parts.push('color:' + color)
    if (bg) parts.push('background-color:' + bg)
  }

  // Visible block-level typography (font-size/family/letter-spacing/transform)
  // pasted from Word/web would silently vanish on re-enter; keep it alongside
  // color/bg the same way. Headings deliberately exclude these (the heading
  // toolbar normalizes heading typography, see normalizeHeadingTypography).
  function blockTypographyStyleParts(el: HTMLElement, parts: string[]) {
    const fs = el.style.fontSize || ''
    const ff = el.style.fontFamily || ''
    const ls = el.style.letterSpacing || ''
    const tt = el.style.textTransform || ''
    if (fs) parts.push('font-size:' + fs)
    // CSSOM preserves the quotes in font-family values ("Times New Roman");
    // escape them or the emitted style="..." attribute breaks apart.
    if (ff) parts.push('font-family:' + ff.replace(/"/g, '&quot;'))
    if (ls) parts.push('letter-spacing:' + ls)
    if (tt) parts.push('text-transform:' + tt)
  }

  // Pasted content (Google Docs / 飞书 / Word / web pages) frequently wraps
  // PLAIN text in semantic tags whose inline style overrides the semantics,
  // e.g. Google Docs' canonical `<b style="font-weight:normal">` wrapper.
  // The browser honors the override (the text looks plain while editing), so
  // blindly emitting `**…**`/`<strong>` would persist formatting the user
  // never saw — the classic "plain text became bold after re-entering" bug.
  // These helpers return '' (no override → tag semantics apply), 'normal'
  // (explicitly NOT styled) or 'bold'/'italic'/'none' (explicitly styled).
  //
  // `inherit`/`unset` on font-weight/font-style resolve along the ancestor
  // chain (inline styles only, since this runs on a detached DOM clone): a
  // `<b style="font-weight:inherit">` inside a plain paragraph computes to
  // normal weight in the browser, so it must NOT persist as bold — the same
  // corruption class as Google Docs' wrapper. Stylesheet-inlined
  // `strong { font-weight: inherit }` (common in design systems) lands here.
  // Each ancestor contributes its inline declaration if any; otherwise its
  // UA tag default (b/strong/th/h1-h6 → bold, em/i/cite/var/dfn → italic,
  // u/s/del → underline/line-through); the chain root is the plain container
  // div → normal/none.
  function inheritedValue(
    el: HTMLElement,
    prop: string,
    tagDefault: (tag: string) => string,
  ): string {
    let node: HTMLElement | null = el.parentElement
    while (node) {
      const v = (node.style[prop as keyof CSSStyleDeclaration] || '').toString().trim().toLowerCase()
      if (v && v !== 'inherit' && v !== 'unset') return v
      const sem = tagDefault(node.tagName)
      if (sem) return sem
      node = node.parentElement
    }
    return ''
  }

  const WEIGHT_DEFAULT_TAGS = ['B', 'STRONG', 'TH', 'DT', 'SUMMARY', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6']
  const ITALIC_DEFAULT_TAGS = ['EM', 'I', 'CITE', 'VAR', 'DFN']

  function effectiveFontWeight(el: HTMLElement): '' | 'normal' | 'bold' {
    const own = (el.style.fontWeight || '').trim().toLowerCase()
    if (!own) return ''
    let fw = own
    // font-weight is inherited: `unset` behaves like `inherit`.
    if (fw === 'inherit' || fw === 'unset') {
      fw = inheritedValue(el, 'fontWeight', (tag) => (WEIGHT_DEFAULT_TAGS.includes(tag) ? 'bold' : ''))
      if (!fw) return 'normal' // chain exhausted → plain ancestors default to normal
    }
    if (fw === 'initial') return 'normal'
    if (fw === 'revert' || fw === 'revert-layer') return ''
    const n = parseInt(fw, 10)
    if (fw === 'bold' || fw === 'bolder' || (!Number.isNaN(n) && n >= 600)) return 'bold'
    if (fw === 'normal' || fw === 'lighter' || (!Number.isNaN(n) && n <= 500)) return 'normal'
    return ''
  }

  function effectiveFontStyle(el: HTMLElement): '' | 'normal' | 'italic' {
    const own = (el.style.fontStyle || '').trim().toLowerCase()
    if (!own) return ''
    let fs = own
    // font-style is inherited: `unset` behaves like `inherit`.
    if (fs === 'inherit' || fs === 'unset') {
      fs = inheritedValue(el, 'fontStyle', (tag) => (ITALIC_DEFAULT_TAGS.includes(tag) ? 'italic' : ''))
      if (!fs) return 'normal' // chain exhausted → plain ancestors default to normal
    }
    if (fs === 'initial') return 'normal'
    if (fs === 'revert' || fs === 'revert-layer') return ''
    if (fs === 'normal') return 'normal'
    if (/italic|oblique/.test(fs)) return 'italic'
    return ''
  }

  function effectiveTextDecoration(el: HTMLElement): '' | 'none' | 'line' {
    let td = (el.style.textDecoration || '').trim().toLowerCase()
    let tdLine = (el.style.textDecorationLine || '').trim().toLowerCase()
    if (td === 'inherit') td = inheritedValue(el, 'textDecoration', (tag) => {
      if (tag === 'U') return 'underline'
      if (tag === 'S' || tag === 'DEL' || tag === 'STRIKE') return 'line-through'
      return ''
    }) || 'none' // chain exhausted → no decoration on the ancestor chain
    if (tdLine === 'inherit') tdLine = inheritedValue(el, 'textDecorationLine', (tag) => {
      if (tag === 'U') return 'underline'
      if (tag === 'S' || tag === 'DEL' || tag === 'STRIKE') return 'line-through'
      return ''
    }) || 'none'
    // text-decoration is NOT inherited: `unset`/`initial` → initial value (none).
    if (td === 'unset' || td === 'initial') td = 'none'
    if (tdLine === 'unset' || tdLine === 'initial') tdLine = 'none'
    // The longhand is authoritative when it carries a concrete value: the
    // cascade resolves both declarations in order, so the parsed longhand is
    // the winner whenever both are present.
    if (tdLine === 'underline' || tdLine === 'line-through' || tdLine === 'overline') return 'line'
    if (tdLine === 'none') return 'none'
    if (!td) return ''
    if (/^none(\s|$)/.test(td)) return 'none'
    if (/^revert/.test(td)) return '' // revert → UA default → tag semantics (u/s keep the line, plain tags none)
    if (/underline|line-through|overline/.test(td)) return 'line'
    return ''
  }

  // Same family as the effective* helpers but for <mark>: an explicit
  // transparent background means the element shows NO highlight while
  // editing, so persisting `<mark>` would materialize the yellow browser
  // highlight after re-entering. `initial`/`unset` compute to transparent
  // (background-color is not inherited → its initial value is transparent);
  // `revert` restores the UA yellow, so it is deliberately not treated as
  // transparent. `inherit` resolves along the ancestor chain.
  function isTransparentBackground(el: HTMLElement): boolean {
    let bg = (el.style.backgroundColor || '').trim().toLowerCase()
    if (bg === 'inherit') {
      bg = inheritedValue(el, 'backgroundColor', () => '')
      if (!bg) return true // parent chain has no background → initial (transparent)
    }
    if (!bg) return false
    if (bg === 'transparent' || bg === 'initial' || bg === 'unset') return true
    return /^rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\)$/.test(bg)
  }

  function emitEmphasis(el: HTMLElement, marker: string, htmlTag: string, ctx: SiblingCtx, inner?: string): string {
    const content = inner ?? processChildren(el)
    const text = el.textContent || ''
    const first = text.charAt(0)
    const last = text.charAt(text.length - 1)
    if (canOpenEm(first, ctx.prev) && canCloseEm(last, ctx.next)) {
      return marker + content + marker
    }
    return '<' + htmlTag + '>' + content + '</' + htmlTag + '>'
  }

  // Shared inline-emphasis serializer for <span> and legacy <font> tags:
  // pasted/foreign content carries bold/italic/underline/strike as inline
  // CSS instead of semantic tags; convert so the formatting is not lost.
  function serializeStyledInline(el: HTMLElement, ctx: SiblingCtx): string {
    const style = el.getAttribute('style') || ''
    const bgMatch = style.match(/background-color\s*:\s*([^;]+)/)
    // Anchor to start/semicolon so "background-color" is not matched as "color".
    const colorMatch = style.match(/(?:^|;)\s*color\s*:\s*([^;]+)/)
    // Only legacy <font> carries a presentational color attribute; <span color>
    // is invalid HTML that browsers ignore — never materialize it.
    const colorAttr = el.tagName.toLowerCase() === 'font' ? (el.getAttribute('color') || '') : ''
    const fwMatch = style.match(/font-weight\s*:\s*([^;]+)/)
    const fsMatch = style.match(/font-style\s*:\s*([^;]+)/)
    const tdMatch = style.match(/text-decoration(?:-line)?\s*:\s*([^;]+)/)
    // Visible typography pasted as inline CSS would otherwise silently vanish
    // on re-enter; keep it the same way color/background are kept.
    const sizeMatch = style.match(/font-size\s*:\s*([^;]+)/)
    const familyMatch = style.match(/font-family\s*:\s*([^;]+)/)
    const letterSpacingMatch = style.match(/letter-spacing\s*:\s*([^;]+)/)
    const transformMatch = style.match(/text-transform\s*:\s*([^;]+)/)
    let inner = processChildren(el)
    const fwVal = fwMatch ? fwMatch[1].trim().toLowerCase() : ''
    const isBold = fwVal === 'bold' || fwVal === 'bolder' || (parseInt(fwVal, 10) >= 600)
    const isItalic = !!fsMatch && /italic|oblique/.test(fsMatch[1].trim().toLowerCase())
    const deco = tdMatch ? tdMatch[1].toLowerCase() : ''
    if (/line-through/.test(deco)) inner = '<s>' + inner + '</s>'
    if (/underline/.test(deco)) inner = '<u>' + inner + '</u>'
    if (isItalic) inner = emitEmphasis(el, '*', 'em', ctx, inner)
    if (isBold) inner = emitEmphasis(el, '**', 'strong', ctx, inner)
    const styleParts: string[] = []
    if (colorMatch) styleParts.push('color:' + colorMatch[1].trim())
    else if (colorAttr) styleParts.push('color:' + colorAttr)
    if (bgMatch) styleParts.push('background-color:' + bgMatch[1].trim())
    if (sizeMatch) styleParts.push('font-size:' + sizeMatch[1].trim())
    if (familyMatch) styleParts.push('font-family:' + familyMatch[1].trim().replace(/"/g, '&quot;'))
    if (letterSpacingMatch) styleParts.push('letter-spacing:' + letterSpacingMatch[1].trim())
    if (transformMatch) styleParts.push('text-transform:' + transformMatch[1].trim())
    if (styleParts.length > 0) {
      return '<span style="' + styleParts.join(';') + '">' + inner + '</span>'
    }
    return inner
  }

  function leadingBlockMarkerGuard(text: string): string {
    // A paragraph starting with "- ", "+ " or "1. " would be re-parsed as a
    // list; escape the first character so it stays literal text.
    if (/^([-+]|\d{1,9}[.)])(\s|$)/.test(text)) {
      return '\\' + text
    }
    return text
  }

  // GFM pipe tables cannot represent merged cells (colspan/rowspan) or cell
  // styling (background, color, alignment). Pasted tables from Word/web pages
  // routinely carry those — pipe serialization would flatten merges and drop
  // all colors. Such tables are emitted as normalized raw HTML instead, which
  // passes through marked unchanged and re-parses to the same DOM (stable
  // across round-trips). Simple tables keep the pipe form.
  function tableNeedsRawHtml(table: HTMLElement): boolean {
    const cells = Array.from(table.querySelectorAll('th, td')) as HTMLElement[]
    return cells.some((cell) => {
      if (parseInt(cell.getAttribute('colspan') || '1', 10) > 1) return true
      if (parseInt(cell.getAttribute('rowspan') || '1', 10) > 1) return true
      const ta = cell.style.textAlign || cell.getAttribute('align') || ''
      if (ta && ta !== 'left' && ta !== 'start') return true
      return !!(cell.style.backgroundColor || cell.style.color)
    })
  }

  function serializeTableAsHtml(table: HTMLElement): string {
    const cellHtml = (cell: HTMLElement): string => {
      const tag = cell.tagName.toLowerCase()
      let attrs = ''
      const cs = parseInt(cell.getAttribute('colspan') || '1', 10)
      const rs = parseInt(cell.getAttribute('rowspan') || '1', 10)
      if (cs > 1) attrs += ' colspan="' + cs + '"'
      if (rs > 1) attrs += ' rowspan="' + rs + '"'
      const parts: string[] = []
      const ta = cell.style.textAlign || cell.getAttribute('align') || ''
      if (ta && ta !== 'left' && ta !== 'start') parts.push('text-align:' + ta)
      blockColorStyleParts(cell, parts)
      if (parts.length > 0) attrs += ' style="' + parts.join(';') + '"'
      const inner = blockEmphasisWrap(cell, serializeChildrenAsHtml(cell), true, { prev: '', next: '' })
      return '<' + tag + attrs + '>' + (inner.trim() ? inner : '<br>') + '</' + tag + '>'
    }
    const renderRows = (rows: Element[]): string =>
      rows.map((tr) =>
        '<tr>' + Array.from(tr.children)
          .filter((c) => c.tagName === 'TH' || c.tagName === 'TD')
          .map((c) => cellHtml(c as HTMLElement))
          .join('') + '</tr>'
      ).join('')
    const directRows = (root: Element): Element[] =>
      Array.from(root.children).filter((c) => c.tagName === 'TR')

    const sections: string[] = []
    const explicitHead = Array.from(table.children).find((c) => c.tagName === 'THEAD')
    const bodies = Array.from(table.children).filter((c) => c.tagName === 'TBODY')
    const bodyRows = bodies.length > 0
      ? bodies.flatMap(directRows)
      : directRows(table)
    // Leading rows made entirely of <th> are treated as the header (browsers
    // auto-move pasted <tr><th> rows into an implicit tbody).
    const isAllTh = (tr: Element): boolean => {
      const cells = Array.from(tr.children).filter((c) => c.tagName === 'TH' || c.tagName === 'TD')
      return cells.length > 0 && cells.every((c) => c.tagName === 'TH')
    }
    const headRows: Element[] = []
    if (explicitHead) {
      headRows.push(...directRows(explicitHead))
    } else {
      while (bodyRows.length > 0 && isAllTh(bodyRows[0])) {
        headRows.push(bodyRows.shift() as Element)
      }
    }
    if (headRows.length > 0) sections.push('<thead>' + renderRows(headRows) + '</thead>')
    sections.push('<tbody>' + renderRows(bodyRows) + '</tbody>')
    return '<table>' + sections.join('') + '</table>\n\n'
  }

  // marked does NOT parse markdown inside raw HTML blocks, so any raw-HTML
  // emission (styled <p>/<hN>/<blockquote>/<li>) must serialize its children
  // as normalized HTML — otherwise "**bold**" would render as literal text.
  function serializeChildrenAsHtml(parent: HTMLElement): string {
    let out = ''
    const walk = (node: Node): void => {
      if (node.nodeType === Node.TEXT_NODE) {
        out += stripZeroWidth(node.textContent || '')
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
        return
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return
      const el = node as HTMLElement
      const tag = el.tagName.toLowerCase()
      const kids = () => {
        const saved = out
        out = ''
        Array.from(el.childNodes).forEach(walk)
        const inner = out
        out = saved
        return inner
      }
      switch (tag) {
        case 'strong':
        case 'b': {
          // Keep explicit inline overrides on the tag (raw-HTML contexts must
          // not lose them: `<b style="font-weight:normal">` inside a styled
          // paragraph would otherwise re-render as bold on the next entry).
          const fw = effectiveFontWeight(el)
          out += fw === 'normal' ? '<strong style="font-weight:normal">' + kids() + '</strong>'
            : '<strong>' + kids() + '</strong>'
          break
        }
        case 'em':
        case 'i': {
          const fs = effectiveFontStyle(el)
          out += fs === 'normal' ? '<em style="font-style:normal">' + kids() + '</em>'
            : '<em>' + kids() + '</em>'
          break
        }
        case 'u': {
          const deco = effectiveTextDecoration(el)
          out += deco === 'none' ? '<u style="text-decoration:none">' + kids() + '</u>'
            : '<u>' + kids() + '</u>'
          break
        }
        case 's':
        case 'strike':
        case 'del': {
          const deco = effectiveTextDecoration(el)
          out += deco === 'none' ? '<s style="text-decoration:none">' + kids() + '</s>'
            : '<s>' + kids() + '</s>'
          break
        }
        case 'sup': out += '<sup>' + kids() + '</sup>'; break
        case 'sub': out += '<sub>' + kids() + '</sub>'; break
        case 'mark': {
          // Keep the transparent override on the tag in raw-HTML contexts so
          // it cannot re-materialize as a yellow highlight on the next entry.
          out += isTransparentBackground(el)
            ? '<mark style="background-color:transparent">' + kids() + '</mark>'
            : '<mark>' + kids() + '</mark>'
          break
        }
        case 'br': out += '<br>'; break
        case 'code': {
          if (el.parentElement && el.parentElement.tagName.toLowerCase() === 'pre') {
            Array.from(el.childNodes).forEach(walk)
          } else {
            out += '<code>' + (el.textContent || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</code>'
          }
          break
        }
        case 'a': {
          const href = (el.getAttribute('href') || '').replace(/"/g, '&quot;')
          out += '<a href="' + href + '">' + kids() + '</a>'
          break
        }
        case 'img': {
          const src = canonicalImageSrc(el.getAttribute('data-original-src') || el.getAttribute('src') || '')
          if (src.startsWith('blob:') || src.startsWith('data:')) break
          const alt = (el.getAttribute('alt') || '').replace(/"/g, '&quot;')
          const styleAttr = el.getAttribute('style')
          const align = el.getAttribute('align')
          out += '<img src="' + src.replace(/"/g, '&quot;') + '" data-original-src="' + src.replace(/"/g, '&quot;') + '" alt="' + alt + '"'
            + (align ? ' align="' + align + '"' : '')
            + (styleAttr ? ' style="' + styleAttr.replace(/"/g, '&quot;') + '"' : '')
            + '>'
          break
        }
        case 'audio':
        case 'video': {
          let src = canonicalImageSrc(el.getAttribute('data-original-src') || el.getAttribute('src') || '')
          if (!src) {
            const sourceEl = el.querySelector('source[src]')
            if (sourceEl) {
              src = canonicalImageSrc(sourceEl.getAttribute('data-original-src') || sourceEl.getAttribute('src') || '')
            }
          }
          if (src.startsWith('blob:') || src.startsWith('data:')) break
          let attrs = `src="${src.replace(/"/g, '&quot;')}"`
          if (el.hasAttribute('controls')) attrs += ' controls'
          if (tag === 'video' && el.hasAttribute('playsinline')) attrs += ' playsinline'
          const poster = el.getAttribute('poster')
          if (poster) attrs += ` poster="${poster.replace(/"/g, '&quot;')}"`
          out += `<${tag} ${attrs}></${tag}>`
          break
        }
        case 'iframe': {
          const src = el.getAttribute('src') || ''
          if (!src) break
          let attrs = `src="${src.replace(/"/g, '&quot;')}"`
          const w = el.getAttribute('width')
          const h = el.getAttribute('height')
          if (w) attrs += ` width="${w.replace(/"/g, '&quot;')}"`
          if (h) attrs += ` height="${h.replace(/"/g, '&quot;')}"`
          if (el.hasAttribute('allowfullscreen')) attrs += ' allowfullscreen'
          out += `<iframe ${attrs}></iframe>`
          break
        }
        case 'font': {
          // Raw-HTML path: honor inline bold/italic/underline/strike and keep
          // color/bg like the main serializer, or <font> emphasis would be
          // silently dropped inside styled paragraphs/table cells.
          const color = el.style.color || el.getAttribute('color') || ''
          const bg = el.style.backgroundColor || ''
          const fw = effectiveFontWeight(el)
          const fs = effectiveFontStyle(el)
          const deco = effectiveTextDecoration(el)
          let inner = kids()
          if (fw === 'bold') inner = '<strong>' + inner + '</strong>'
          if (fs === 'italic') inner = '<em>' + inner + '</em>'
          if (deco === 'line') {
            // Nest strike inside underline (`<u><s>`) — same order as the main
            // serializer and blockEmphasisWrap, so markup doesn't flip when
            // content migrates between the raw and markdown paths.
            const rawDeco = ((el.style.textDecoration || '') + ' ' + (el.style.textDecorationLine || '')).toLowerCase()
            if (/line-through/.test(rawDeco)) inner = '<s>' + inner + '</s>'
            if (/underline/.test(rawDeco)) inner = '<u>' + inner + '</u>'
          }
          const styleParts: string[] = []
          if (color) styleParts.push('color:' + color)
          if (bg) styleParts.push('background-color:' + bg)
          out += styleParts.length > 0 ? '<span style="' + styleParts.join(';') + '">' + inner + '</span>' : inner
          break
        }
        case 'span': {
          if (el.classList.contains('math-editable')) {
            // Math inside raw-HTML blocks cannot be re-rendered by the
            // markdown pipeline; keep the TeX source as literal text
            // (same behavior as before this fix — no regression).
            const tex = el.getAttribute('data-tex') || ''
            const display = el.getAttribute('data-display-mode') === 'true'
            const decoded = htmlDecode(tex)
            out += display ? ('$$' + decoded + '$$') : ('$' + decoded + '$')
            break
          }
          const inner = kids()
          const color = el.style.color || ''
          const bg = el.style.backgroundColor || ''
          const styleParts: string[] = []
          if (color) styleParts.push('color:' + color)
          if (bg) styleParts.push('background-color:' + bg)
          blockTypographyStyleParts(el, styleParts)
          if (styleParts.length > 0) {
            out += '<span style="' + styleParts.join(';') + '">' + inner + '</span>'
          } else {
            out += inner
          }
          break
        }
        default:
          Array.from(el.childNodes).forEach(walk)
      }
    }
    Array.from(parent.childNodes).forEach(walk)
    return out
  }

  function processNode(node: Node, ctx: SiblingCtx = { prev: '', next: '' }): string {
    if (node.nodeType === Node.TEXT_NODE) {
      return escapeMdText(node.textContent || '')
    }

    if (node.nodeType !== Node.ELEMENT_NODE) return ''

    const el = node as HTMLElement
    const tag = el.tagName.toLowerCase()
    let result = ''

    switch (tag) {
      case 'h1':
      case 'h2':
      case 'h3':
      case 'h4':
      case 'h5':
      case 'h6': {
        const level = parseInt(tag.charAt(1), 10)
        const ta = el.style.textAlign || (el.getAttribute('align') || '')
        const hStyleParts: string[] = []
        if (ta && ta !== 'left' && ta !== 'start') hStyleParts.push('text-align:' + ta)
        blockColorStyleParts(el, hStyleParts)
        if (hStyleParts.length > 0) {
          // Preserve heading styling via raw HTML (stable across cycles).
          result = '<h' + level + ' style="' + hStyleParts.join(';') + '">' + blockEmphasisWrap(el, serializeChildrenAsHtml(el), true, ctx) + '</h' + level + '>\n\n'
        } else {
          result = '#'.repeat(level) + ' ' + processChildren(el) + '\n\n'
        }
        break
      }
      case 'p': {
        const ml = el.style.marginLeft
        const mlVal = ml ? parseInt(ml, 10) : 0
        const ta = el.style.textAlign || (el.getAttribute('align') || '')
        const styleParts: string[] = []
        if (mlVal > 0) styleParts.push('margin-left:' + mlVal + 'px')
        if (ta && ta !== 'left' && ta !== 'start') styleParts.push('text-align:' + ta)
        blockColorStyleParts(el, styleParts)
        blockTypographyStyleParts(el, styleParts)
        const inner = processChildren(el)
        const isEmpty = !inner.trim() && !el.querySelector('img, table, pre, .math-editable')
        const parentTag = el.parentElement ? el.parentElement.tagName : ''
        if (styleParts.length > 0) {
          result = '<p style="' + styleParts.join(';') + '">' + (isEmpty ? '<br>' : blockEmphasisWrap(el, serializeChildrenAsHtml(el), true, ctx)) + '</p>\n\n'
        } else if (isEmpty) {
          // Keep empty paragraphs as explicit raw HTML so blank lines the
          // user typed survive the markdown round-trip instead of collapsing.
          if (parentTag === 'DIV' || el.parentElement === container) {
            result = '<p><br></p>\n\n'
          } else {
            result = '\n\n'
          }
        } else {
          result = leadingBlockMarkerGuard(blockEmphasisWrap(el, inner, false, ctx)) + '\n\n'
        }
        break
      }
      case 'strong':
      case 'b': {
        // Explicit non-bold override (e.g. Google Docs' `<b style="font-weight:normal">`
        // wrapper around the whole copy) means the element is NOT bold — unwrap it
        // or the plain text would persist as `**…**` and render bold after re-entering.
        if (effectiveFontWeight(el) === 'normal') { result = processChildren(el); break }
        result = emitEmphasis(el, '**', 'strong', ctx)
        break
      }
      case 'em':
      case 'i': {
        if (effectiveFontStyle(el) === 'normal') { result = processChildren(el); break }
        result = emitEmphasis(el, '*', 'em', ctx)
        break
      }
      case 'br': result = '\n'; break
      case 'hr': result = '---\n\n'; break
      case 'blockquote': {
        const ta = el.style.textAlign || (el.getAttribute('align') || '')
        if (ta && ta !== 'left' && ta !== 'start') {
          result = '<blockquote style="text-align:' + ta + '">' + serializeChildrenAsHtml(el) + '</blockquote>\n\n'
        } else {
          result = '> ' + processChildren(el).split('\n').join('\n> ') + '\n\n'
        }
        break
      }
      case 'audio':
      case 'video': {
        // Media elements serialize as raw HTML (marked passes them through).
        // The canonical path is data-original-src (set before the signed URL
        // swap); blob:/data: srcs cannot persist and are dropped. When the
        // element itself has no src, fall back to its first <source src>
        // child (pasted HTML shape) so the media reference never survives
        // as a broken empty-src element.
        let src = canonicalImageSrc(el.getAttribute('data-original-src') || el.getAttribute('src') || '')
        if (!src) {
          const sourceEl = el.querySelector('source[src]')
          if (sourceEl) {
            src = canonicalImageSrc(sourceEl.getAttribute('data-original-src') || sourceEl.getAttribute('src') || '')
          }
        }
        if (src.startsWith('blob:') || src.startsWith('data:')) {
          result = ''
          break
        }
        let attrs = `src="${src.replace(/"/g, '&quot;')}"`
        if (el.hasAttribute('controls')) attrs += ' controls'
        if (tag === 'video' && el.hasAttribute('playsinline')) attrs += ' playsinline'
        const poster = el.getAttribute('poster')
        if (poster) attrs += ` poster="${poster.replace(/"/g, '&quot;')}"`
        const parentTag = el.parentElement ? el.parentElement.tagName : ''
        const needsSeparator = !['P', 'LI', 'TD', 'TH'].includes(parentTag)
        result = `<${tag} ${attrs}></${tag}>` + (needsSeparator ? '\n\n' : '')
        break
      }
      case 'iframe': {
        // Embed iframes (YouTube/Bilibili official share links) serialize
        // back as raw HTML so they survive the note round-trip.
        const src = el.getAttribute('src') || ''
        if (!src) {
          result = ''
          break
        }
        let attrs = `src="${src.replace(/"/g, '&quot;')}"`
        const w = el.getAttribute('width')
        const h = el.getAttribute('height')
        if (w) attrs += ` width="${w.replace(/"/g, '&quot;')}"`
        if (h) attrs += ` height="${h.replace(/"/g, '&quot;')}"`
        if (el.hasAttribute('allowfullscreen')) attrs += ' allowfullscreen'
        const parentTag = el.parentElement ? el.parentElement.tagName : ''
        const needsSeparator = !['P', 'LI', 'TD', 'TH'].includes(parentTag)
        result = `<iframe ${attrs}></iframe>` + (needsSeparator ? '\n\n' : '')
        break
      }
      case 'a': {
        const href = el.getAttribute('href') || ''
        result = '[' + processChildren(el) + '](' + href + ')'
        break
      }
      case 'img': {
        const src = canonicalImageSrc(el.getAttribute('data-original-src') || el.getAttribute('src') || '')
        const alt = el.getAttribute('alt') || ''
        const align = el.getAttribute('align') || ''
        if (src.startsWith('blob:') || src.startsWith('data:')) {
          result = ''
          break
        }
        const styleAttr = el.getAttribute('style') || ''
        const widthMatch = styleAttr.match(/width\s*:\s*([^;]+)/i)
        const heightMatch = styleAttr.match(/height\s*:\s*([^;]+)/i)
        const imgWidth = widthMatch ? widthMatch[1].trim() : ''
        const imgHeight = heightMatch ? heightMatch[1].trim() : ''
        // Pasted content (Word/web) positions images with CSS float instead of
        // the align attribute the toolbar uses. Preserve it the same way so
        // text-wrapping survives the round-trip.
        const floatMatch = styleAttr.match(/float\s*:\s*(left|right)/i)
        const imgFloat = !align && floatMatch ? floatMatch[1].toLowerCase() : ''
        const hasStyle = align || imgWidth || imgHeight || imgFloat
        if (hasStyle) {
          let attrs = 'src="' + src + '" alt="' + alt + '"'
          if (align) attrs += ' align="' + align + '"'
          let styleParts: string[] = []
          if (imgWidth) styleParts.push('width:' + imgWidth)
          if (imgHeight) styleParts.push('height:' + imgHeight)
          if (align === 'center') {
            styleParts.push('display:block')
            styleParts.push('margin:8px auto')
          } else if (align === 'left' || imgFloat === 'left') {
            styleParts.push('float:left')
            styleParts.push('margin:8px 12px 8px 0')
            styleParts.push('max-width:50%')
          } else if (align === 'right' || imgFloat === 'right') {
            styleParts.push('float:right')
            styleParts.push('margin:8px 0 8px 12px')
            styleParts.push('max-width:50%')
          }
          if (styleParts.length > 0) attrs += ' style="' + styleParts.join(';') + '"'
          // Add a blank-line separator when the <img> is a direct child of the
          // editor (or a <div>), so marked treats it as a standalone HTML block
          // and does NOT merge it with the following text (which would turn the
          // text into a bare text node unalignable by the toolbar). When the img
          // is inside a <p>/<li>/<td>/<th>, the parent case already appends the
          // needed spacing, so no extra separator is added there.
          const parentTag = el.parentElement ? el.parentElement.tagName : ''
          const needsSeparator = !['P', 'LI', 'TD', 'TH'].includes(parentTag)
          result = '<img ' + attrs + '>' + (needsSeparator ? '\n\n' : '')
        } else {
          result = '![' + alt + '](' + src + ')'
        }
        break
      }
      case 'ul':
      case 'ol': {
        if (tag === 'ol' && el.style.counterReset) {
          const inner = processListElement(el, 0)
          result = '<ol style="counter-reset:' + el.style.counterReset + '">\n' + inner + '</ol>\n'
        } else {
          result = processListElement(el, 0) + '\n'
        }
        break
      }
      case 'li': {
        let text = ''
        let nested = ''
        const liChildren = Array.from(el.childNodes)
        const liTexts = liChildren.map((c) => c.textContent || '')
        liChildren.forEach((node, i) => {
          if (node.nodeType === Node.ELEMENT_NODE) {
            const t = (node as HTMLElement).tagName.toLowerCase()
            if (t === 'ul' || t === 'ol') {
              nested += processListElement(node as HTMLElement, 1)
              return
            }
          }
          let prev = ''
          for (let j = i - 1; j >= 0; j--) {
            if (liTexts[j]) { prev = liTexts[j].charAt(liTexts[j].length - 1); break }
          }
          let next = ''
          for (let j = i + 1; j < liTexts.length; j++) {
            if (liTexts[j]) { next = liTexts[j].charAt(0); break }
          }
          text += processNode(node, { prev, next })
        })
        result = text.trim() + (nested ? '\n' + nested : '')
        break
      }
      case 'pre': {
        const codeEl = el.querySelector('code')
        if (codeEl) {
          const langClass = codeEl.className.match(/language-(\S+)/)
          const lang = langClass ? langClass[1] : ''
          const text = stripZeroWidth((codeEl.textContent || '').trim())
          if (el.classList.contains('mermaid-block')) {
            const src = el.getAttribute('data-mermaid-source') || ''
            result = '\n```mermaid\n' + htmlDecode(src) + '\n```\n'
          } else {
            result = '\n```' + lang + '\n' + text + '\n```\n\n'
          }
        }
        break
      }
      case 'code': {
        if (el.parentElement && el.parentElement.tagName.toLowerCase() === 'pre') {
          return ''
        }
        result = '`' + stripZeroWidth(el.textContent || '') + '`'
        break
      }
      case 'table': {
        if (tableNeedsRawHtml(el)) {
          result = serializeTableAsHtml(el)
          break
        }
        const rows: string[] = []
        Array.from(el.querySelectorAll('tr')).forEach((tr, rowIdx) => {
          const cells = Array.from(tr.querySelectorAll('th, td')).map((td) =>
            processChildren(td).trim().replace(/\n/g, ' ')
          )
          rows.push('| ' + cells.join(' | ') + ' |')
          if (rowIdx === 0) {
            rows.push('| ' + cells.map(() => '---').join(' | ') + ' |')
          }
        })
        result = rows.join('\n') + '\n\n'
        break
      }
      case 'div': {
        if (el.classList.contains('mermaid-block')) {
          const src = el.getAttribute('data-mermaid-source') || ''
          result = '\n```mermaid\n' + htmlDecode(src) + '\n```\n'
        } else if (el.classList.contains('echarts-block')) {
          const src = el.getAttribute('data-echarts-source') || ''
          result = '\n```echarts\n' + htmlDecode(src) + '\n```\n'
        } else if (el.classList.contains('math-editable')) {
          const tex = el.getAttribute('data-tex') || ''
          const display = el.getAttribute('data-display-mode') === 'true'
          const decoded = htmlDecode(tex)
          result = display ? ('\n$$\n' + decoded + '\n$$\n') : ('$' + decoded + '$')
        } else {
          const inner = processChildren(el)
        const isEmpty = !inner.trim() && !el.querySelector('img, audio, video, iframe, table, pre, .math-editable')
          const parentTag = el.parentElement ? el.parentElement.tagName : ''
          const ta = el.style.textAlign || (el.getAttribute('align') || '')
          const dStyleParts: string[] = []
          if (ta && ta !== 'left' && ta !== 'start') dStyleParts.push('text-align:' + ta)
          blockColorStyleParts(el, dStyleParts)
          blockTypographyStyleParts(el, dStyleParts)
          if (isEmpty && (parentTag === 'DIV' || el.parentElement === container)) {
            result = '<p><br></p>\n\n'
          } else if (dStyleParts.length > 0) {
            // Chrome contenteditable often wraps lines in <div>; a styled
            // div becomes a styled paragraph so the styling survives.
            result = '<p style="' + dStyleParts.join(';') + '">' + (isEmpty ? '<br>' : blockEmphasisWrap(el, serializeChildrenAsHtml(el), true, ctx)) + '</p>\n\n'
          } else {
            result = blockEmphasisWrap(el, inner, false, ctx) + '\n'
          }
        }
        break
      }
      case 'span': {
        if (el.classList.contains('math-editable')) {
          const tex = el.getAttribute('data-tex') || ''
          const display = el.getAttribute('data-display-mode') === 'true'
          const decoded = htmlDecode(tex)
          result = display ? ('\n$$\n' + decoded + '\n$$\n') : ('$' + decoded + '$')
          break
        }
        result = serializeStyledInline(el, ctx)
        break
      }
      case 'font': {
        // Legacy <font> from pasted content (Word etc.): keep the color
        // attribute and honor inline bold/italic/underline/strike overrides —
        // previously all inline emphasis on <font> silently disappeared on
        // the first save.
        result = serializeStyledInline(el, ctx)
        break
      }
      case 'u': {
        if (effectiveTextDecoration(el) === 'none') { result = processChildren(el); break }
        result = '<u>' + processChildren(el) + '</u>'; break
      }
      case 's':
      case 'strike':
      case 'del': {
        if (effectiveTextDecoration(el) === 'none') { result = processChildren(el); break }
        result = '<s>' + processChildren(el) + '</s>'; break
      }
      case 'mark': {
        // Explicit transparent background (e.g. pasted content that overrode
        // the highlight) means the element shows no highlight while editing —
        // persisting `<mark>` would materialize the yellow highlight after
        // re-entering. Unwrap it like the strong/b normal-weight case.
        if (isTransparentBackground(el)) { result = processChildren(el); break }
        result = '<mark>' + processChildren(el) + '</mark>'; break
      }
      case 'sup': result = '<sup>' + processChildren(el) + '</sup>'; break
      case 'sub': result = '<sub>' + processChildren(el) + '</sub>'; break
      default: result = processChildren(el); break
    }

    return result
  }

  function processChildren(parent: HTMLElement): string {
    const children = Array.from(parent.childNodes)
    // Pre-compute sibling boundary characters for flanking checks: the char
    // immediately before/after each child in the serialized output.
    const texts = children.map((c) => c.textContent || '')
    let s = ''
    children.forEach((child, i) => {
      let prev = ''
      for (let j = i - 1; j >= 0; j--) {
        if (texts[j]) { prev = texts[j].charAt(texts[j].length - 1); break }
      }
      let next = ''
      for (let j = i + 1; j < texts.length; j++) {
        if (texts[j]) { next = texts[j].charAt(0); break }
      }
      s += processNode(child, { prev, next })
    })
    return s
  }

  function processListElement(listEl: HTMLElement, depth: number): string {
    const isOl = listEl.tagName.toLowerCase() === 'ol'
    let md = ''
    let idx = 1
    const indent = '    '.repeat(depth)
    Array.from(listEl.children).forEach((child) => {
      if (child.tagName.toLowerCase() !== 'li') return
      const li = child as HTMLElement
      const marginLeft = li.style.marginLeft ? parseInt(li.style.marginLeft, 10) : 0
      const textAlign = li.style.textAlign || ''
      let textContent = ''
      let nestedListHtml = ''
      const liChildren = Array.from(li.childNodes)
      const liTexts = liChildren.map((c) => c.textContent || '')
      liChildren.forEach((node, i) => {
        if (node.nodeType === Node.ELEMENT_NODE) {
          const tag = (node as HTMLElement).tagName.toLowerCase()
          if (tag === 'ul' || tag === 'ol') {
            nestedListHtml += processListElement(node as HTMLElement, depth + 1)
            return
          }
        }
        let prev = ''
        for (let j = i - 1; j >= 0; j--) {
          if (liTexts[j]) { prev = liTexts[j].charAt(liTexts[j].length - 1); break }
        }
        let next = ''
        for (let j = i + 1; j < liTexts.length; j++) {
          if (liTexts[j]) { next = liTexts[j].charAt(0); break }
        }
        textContent += processNode(node, { prev, next })
      })
      textContent = leadingBlockMarkerGuard(textContent.trim())
      const liStyleParts: string[] = []
      if (marginLeft > 0) liStyleParts.push('margin-left:' + marginLeft + 'px')
      if (textAlign && textAlign !== 'left' && textAlign !== 'start') liStyleParts.push('text-align:' + textAlign)
      if (liStyleParts.length > 0) {
        md += indent + '<li style="' + liStyleParts.join(';') + '">' + serializeChildrenAsHtml(li) + '</li>\n'
      } else {
        const prefix = isOl ? (idx++ + '. ') : '- '
        md += indent + prefix + textContent + '\n'
      }
      if (nestedListHtml) {
        md += nestedListHtml
      }
    })
    return md
  }

  function htmlDecode(s: string): string {
    const textarea = document.createElement('textarea')
    textarea.innerHTML = s
    return textarea.value
  }

  markdown = processChildren(container)

  markdown = markdown.replace(/\n{3,}/g, '\n\n')
  markdown = markdown.replace(/\n+$/, '')

  return markdown
}
