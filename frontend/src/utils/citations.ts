// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Citation bibliography / inline-source heuristics — extracted from
 * MessageBubble.vue into a pure module so the parse chain is unit-testable
 * under plain Node (the SFC is not importable there).
 *
 * 来源-anchor rule (W14, prod conv f1499710): the inline patterns fire only on
 * BARE 「来源：」 annotations (line start / after non-CJK punctuation / inside
 * bold markers). A letter/digit immediately before 来源 means a compound
 * prose word (技术来源/信息来源/数据来源/GPT来源/データ来源…) — never an
 * annotation. Without the lookbehind, prose like 「4. 技术来源：尊界…超20家[30]」
 * yielded ONE bogus bibliography entry → maxCitationIndex collapsed to 1 →
 * only [1] of 30 cited markers rendered (引用卡片仅留第一个), and the strip
 * regex ATE the prose sentence. Range = \p{L}\p{N} under /u (review R2:
 * [一-鿿] missed Ext-A/astral/kana/hangul/ASCII-prefixed compounds).
 */
export const BIB_HEADER_RE = /(?:^|\n)\s*(?:#{1,6}\s|\*{1,2})?(?:参考文献|参考资料|参考来源|References|Sources)\s*[：:]?\s*(?:\*{1,2})?\s*/i
export const BIB_LINE_RE = /(?:^|\n)\s*\[(\d{1,3})\]\s*([^\n]+(?:\n(?!\s*\[\d{1,3}\])[^\n]+)*)/g
export const BIB_INLINE_RE = /(?<![\p{L}\p{N}_])来源[：:]\s*([^\n\[]+?)\s*\[(\d{1,3})\]/gu
export const INLINE_SOURCE_REGEX = /(?<![\p{L}\p{N}_])来源[：:]\s*[^\n\[]+?\s*\[(\d{1,3})\]/gu

export function stripBibMarkdown(text: string): string {
  return text
    .replace(/^[\s#*_]+/, '')
    .replace(/[\s#*_]+$/, '')
    .trim()
}

export interface BibEntry { num: number; text: string }

/** Citation range (review r2 I1，fix-round 2)：input = **matched** bib entries
 *  ONLY（fuzzy 匹配到真实检索结果的条目）——fabricated 条目（未匹配）不得把
 *  范围抬出结果数之上，否则 prose 的 [38]-[99] 括号数字变死徽标（validSet
 *  陷阱）。并集语义：total = max(匹配条目最大编号, 检索结果数)。 */
export function maxCitationIndexFor(matchedBib: BibEntry[], resultsLen: number): number {
  const bibMax = matchedBib.reduce((m, e) => Math.max(m, e.num), 0)
  return Math.max(bibMax, resultsLen)
}

export function assembleChips(
  matched: Array<{ entry: BibEntry; result: { url?: string } }>,
  results: Array<{ url?: string }>,
  refs: Set<number>,
  numMap: Map<number, number>,
): Array<{ result: { url?: string }; num: number; bibText?: string }> {
  // One chip PER referenced marker (click-resolvability > URL dedupe): bib
  // interpretation wins where a matched entry covers the number, positional
  // fallback covers the rest — a short bibliography must never collapse the
  // chip list below the cited-marker count (review R1/R2 Critical).
  const items: Array<{ result: { url?: string }; num: number; bibText?: string }> =
    matched
      .filter(({ entry }) => refs.has(entry.num))
      .map(({ entry, result }) => ({ result, num: numMap.get(entry.num) || entry.num, bibText: entry.text }))
  const covered = new Set(matched.map(({ entry }) => entry.num))
  for (let i = 0; i < results.length; i++) {
    const oldNum = i + 1
    if (!refs.has(oldNum) || covered.has(oldNum)) continue
    items.push({ result: results[i], num: numMap.get(oldNum) || oldNum })
  }
  items.sort((a, b) => a.num - b.num)
  return items
}

/** Parse an LLM-emitted bibliography section or inline 来源 annotations into
 *  [N] entries (mirrors the former MessageBubble.parseBibliography body). */
export function parseBibliographyEntries(content: string): BibEntry[] {
  const entries: BibEntry[] = []
  const m = content.match(BIB_HEADER_RE)
  if (m) {
    const after = content.slice((m.index ?? 0) + m[0].length)
    let lm: RegExpExecArray | null
    while ((lm = BIB_LINE_RE.exec(after)) !== null) {
      entries.push({ num: parseInt(lm[1], 10), text: stripBibMarkdown(lm[2]) })
    }
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
}

// ─── M6：服务端 citations 载荷（引文体系改造 2026-10-11） ────────────────────
// 后端在出货点用 CSL 渲染逐条规范 reference 与文内族形态（in_text），前端仅
// 按载荷展示：卡片=reference 文本（URL 可有可无）、文内角标形态随样式族。
// 载荷存在时不再走本地 [N] 启发式/重排（防双重重排错位）。

export interface CitationPayloadEntry {
  id: number
  num: number
  in_text?: string
  reference: string
  url?: string
  title?: string
  snippet?: string
  author?: string
  year?: string
}

export interface CitationsPayload {
  style_id: string
  family: string
  heading: string
  entries: CitationPayloadEntry[]
}

export interface CitationChip {
  result: { url?: string; title?: string; snippet?: string }
  num: number
  bibText?: string
}

/** 载荷 → 引用卡片（每被引条目恰一芯片；num 为服务端编号，点击/预览键）。 */
export function buildCitationChips(payload: CitationsPayload | null | undefined): CitationChip[] {
  const entries = payload?.entries ?? []
  return entries
    .filter((e) => e && Number.isFinite(e.num))
    .slice()
    .sort((a, b) => a.num - b.num)
    .map((e) => ({
      result: {
        url: e.url || '',
        title: e.title || '',
        snippet: e.snippet || '',
      },
      num: e.num,
      bibText: e.reference || '',
    }))
}

/** in_text 消毒（A4.9 B2/N1，纵深防御）：**转义优先**——先整体转义 `<`/`>`，
 *  再仅还原四个精确裸标签 `<i></i><sup></sup>`。未闭合标签（`<img …//`）不再
 *  泄漏（旧白名单正则匹配不到无 `>` 的标签，可被外层 `</span>` 补全执行）。
 *  `&` 不重复转义（citeproc 已产出实体，实体无法构成标签）。 */
function _safeInText(t: string): string {
  return t
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/&lt;(\/?)(i|sup)&gt;/gi, '<$1$2>')
}

/** 文内 [N] → 样式族形态（可点击角标）。notes 族数字渲染上标；其余族按载荷
 *  in_text（含 <sup>/<i> 的 HTML 由 CSL 边界转义产出 + _safeInText 白名单）。 */
export function applyCitationInText(
  html: string,
  payload: CitationsPayload | null | undefined,
): string {
  if (!html || !payload?.entries?.length) return html
  const map = new Map<number, CitationPayloadEntry>()
  for (const e of payload.entries) map.set(e.id, e)
  const parts = html.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>|<a[\s\S]*?<\/a>|<sup[\s\S]*?<\/sup>)/gi)
  return parts
    .map((part, i) => {
      if (i % 2 === 1) return part
      return part.replace(/\[(\d{1,3})\](?![(:])/g, (match, digits: string) => {
        const id = parseInt(digits, 10)
        const e = map.get(id)
        if (!e) return match
        const num = Number.isFinite(e.num) ? Math.trunc(e.num) : id
        const t = _safeInText((e.in_text || '').trim())
        if (t && /^<sup\b/i.test(t)) {
          // 上标族（GB 顺序编码/AMA）：注入 class/data-cite-index 保点击
          return t.replace(
            /^<sup\b[^>]*>/i,
            `<sup class="citation-ref" data-cite-index="${num}">`,
          )
        }
        if (payload.family === 'notes' && /^\d{1,3}$/.test(t)) {
          return `<sup class="citation-ref" data-cite-index="${num}">${t}</sup>`
        }
        const label = t || `[${num}]`
        return `<span class="citation-ref" data-cite-index="${num}">${label}</span>`
      })
    })
    .join('')
}
