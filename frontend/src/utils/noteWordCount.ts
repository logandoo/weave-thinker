// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * CJK-aware note text counter — mirrors backend `app/tools/word_count.py`
 * semantics so the editor's live stats match the `word_count` agent tool:
 *   word_count = CJK chars + latin words
 *   char_count / char_count_no_spaces (space, tab, newline)
 *   line_count (single blank line → 0) / paragraph_count (non-blank lines)
 */
export interface NoteWordCount {
  wordCount: number
  charCount: number
  charCountNoSpaces: number
  lineCount: number
  paragraphCount: number
  cjkChars: number
  latinWords: number
}

const CJK_RE =
  /[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]/g

export function countNoteText(text: string): NoteWordCount {
  const source = text ?? ''
  const cjkChars = (source.match(CJK_RE) || []).length
  const nonCjk = source.replace(CJK_RE, ' ')
  const latinWords = nonCjk.split(/\s+/).filter(w => w.length > 0).length
  const lines = source.split('\n')
  let lineCount = lines.length
  if (lineCount === 1 && !source.trim()) lineCount = 0
  return {
    wordCount: cjkChars + latinWords,
    charCount: source.length,
    charCountNoSpaces: source.replace(/[ \t\n]/g, '').length,
    lineCount,
    paragraphCount: lines.filter(l => l.trim()).length,
    cjkChars,
    latinWords,
  }
}

/**
 * Visible editor text for counting (R3, 2026-09-13 audit F-B): innerText
 * includes KaTeX's visually-hidden MathML + TeX annotation, so a formula
 * would be counted ~3x. Hide those layers for the synchronous read (restored
 * immediately) so only the visible rendering contributes. Single source for
 * both editors — previously duplicated verbatim in NoteEditor and
 * ZenNotePanel.
 */
export function visibleEditorText(container: HTMLElement): string {
  const hidden: HTMLElement[] = []
  container.querySelectorAll<HTMLElement>('.katex-mathml, .math-controls').forEach((el) => {
    hidden.push(el)
    el.dataset.wcPrevDisplay = el.style.display
    el.style.display = 'none'
  })
  const text = container.innerText || ''
  hidden.forEach((el) => {
    el.style.display = el.dataset.wcPrevDisplay || ''
    delete el.dataset.wcPrevDisplay
  })
  return text
}
