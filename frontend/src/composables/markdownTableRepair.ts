// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * GFM table repairs for LLM-generated markdown.
 *
 * Pure and import-free — safe to unit-test in Node via type stripping
 * (mirrors streamDomPreserve.ts).
 *
 * Why this exists: marked/GFM requires a delimiter row (`| --- | --- |`)
 * immediately after the table header; without it the pipe rows render as
 * plain text (markedjs/marked#1429, #1733, #2196). LLMs occasionally omit
 * the delimiter even when the system prompt demands it — production conv
 * dbcb5269 survey: 105/106 table blocks carried the delimiter, one did not
 * and rendered as raw pipes. This repair synthesizes the missing delimiter
 * from the header's cell count so the table renders instead of leaking
 * markdown source.
 *
 * Fence-aware: pipe rows inside ``` / ~~~ code fences are code, not tables.
 * Conservative: a lone pipe line, or a pair of single-cell pipe lines, is
 * left alone (more likely literal text than a table).
 */

/** A table row (GFM allows up to 3 leading spaces before the pipe). */
const TABLE_ROW_RE = /^ {0,3}\|/
/** Opening/closing code fence (backtick or tilde, 3+). */
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})(.*)$/

interface FenceInfo {
  char: string
  len: number
}

function fenceInfo(line: string): FenceInfo | null {
  const m = FENCE_RE.exec(line)
  if (!m) return null
  return { char: m[1][0], len: m[1].length }
}

/** Delimiter row: only pipes, hyphens, colons and spaces, with ≥1 hyphen. */
function isSeparatorRow(line: string): boolean {
  const t = line.trim()
  if (!t.includes('|') || !t.includes('-')) return false
  return /^[\s|:-]+$/.test(t)
}

/** Cell count of a pipe row; `\|` escapes are one character, not a cell break. */
function countCells(line: string): number {
  const inner = line.trim().replace(/^\|/, '').replace(/\|$/, '').replace(/\\\|/g, '')
  return inner.split('|').length
}

export function fixMissingTableSeparators(text: string): string {
  if (!text.includes('|')) return text

  const lines = text.split('\n')
  const out: string[] = []
  let fence: FenceInfo | null = null

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const f = fenceInfo(line)
    if (f) {
      if (!fence) {
        fence = f
      } else if (f.char === fence.char && f.len >= fence.len && line.trim().slice(f.len).trim() === '') {
        fence = null
      }
      out.push(line)
      continue
    }

    out.push(line)
    if (fence) continue
    if (!TABLE_ROW_RE.test(line)) continue
    // Only the first row of a pipe block can be a header.
    if (i > 0 && TABLE_ROW_RE.test(lines[i - 1])) continue

    const next = i + 1 < lines.length ? lines[i + 1] : ''
    if (!TABLE_ROW_RE.test(next)) continue
    if (isSeparatorRow(next)) continue

    const cells = countCells(line)
    if (cells < 2) continue
    out.push('|' + '---|'.repeat(cells))
  }

  return out.join('\n')
}

/**
 * Rebuild a delimiter row whose column count does not match its header row
 * (moved verbatim from useMarkdown.ts — same pipeline stage, now unit-testable
 * together with fixMissingTableSeparators so composed behavior is pinned).
 */
export function fixMarkdownTables(text: string): string {
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
        // Escape-aware counting (`\|` is one cell, not a column break) so a
        // correct delimiter is not "repaired" to the wrong column count.
        const headerCellCount = countCells(lines[headerIdx])
        const sepCellCount = countCells(line)

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
