// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Tab-separated table conversion for USER messages (conv a104fbc5, 2026-09-20).
 *
 * A user copying a table out of the rendered UI / Excel / Word gets
 * tab-separated rows on the clipboard. In Markdown rendering, tabs collapse
 * to single spaces and CommonMark softbreaks fold the rows into one run-on
 * paragraph — the pasted table becomes an unreadable blob (the "最后一个
 * query 完全乱掉" report: a 7-column table rendered as ~1900px of prose).
 *
 * This pass converts runs of consecutive tab-separated lines into a GFM
 * table before marked sees them. Conservative by construction:
 *   - only lines carrying >= 2 tab characters AND a non-empty first cell
 *     count as table rows (tab-indented code `\t\tx = 1` splits to an empty
 *     first cell and is never captured — A4.9 review Important-1);
 *   - a block needs >= 2 such consecutive lines (a single tabbed line is
 *     left alone — formula fragments / indentation must not be captured);
 *   - fenced code blocks are skipped with CommonMark fence pairing
 *     (backtick fence closes only with >= same-length backticks, tilde with
 *     tildes; <=3 leading spaces), so mixed fences cannot leak code rows
 *     (A4.9 Minor-2);
 *   - column count is the block max (short rows are padded), capped at 12;
 *     blocks are capped at MAX_BLOCK_LINES — beyond the cap the block is
 *     split and each chunk re-uses its first line as the GFM header
 *     (headers are mandatory in GFM; a 500+-row single paste is the only
 *     way to observe this, A4.9 Minor-4);
 *   - cell escaping backslash-first (`\` -> `\\` then `|` -> `\|`) so a
 *     literal `\|` cannot forge a cell boundary (A4.9 Minor-3);
 *   - idempotent: converted output contains no tabs, so a re-run is a no-op.
 *
 * Applied to user bubbles only (MessageBubble.vue) — assistant output is
 * authored as Markdown and must not be rewritten. Persisted content is
 * untouched (view-layer only; PDF export of user pastes still shows the raw
 * text — documented as deferred).
 */

const MAX_COLUMNS = 12
const MAX_BLOCK_LINES = 500

function fenceMatch(line: string): { char: string; length: number } | null {
  const m = line.match(/^ {0,3}(`{3,}|~{3,})/)
  if (!m) return null
  return { char: m[1][0], length: m[1].length }
}

function fenceCloses(line: string, fence: { char: string; length: number }): boolean {
  const m = line.match(/^ {0,3}(`{3,}|~{3,})\s*$/)
  if (!m) return false
  return m[1][0] === fence.char && m[1].length >= fence.length
}

function splitTabRow(line: string): string[] {
  return line.split('\t').map((cell) => cell.replace(/\\/g, '\\\\').replace(/\|/g, '\\|').trim())
}

function isTableRow(cells: string[]): boolean {
  return cells.length >= 3 && cells[0] !== ''
}

export function convertTabTablesToMarkdown(text: string): string {
  if (!text || !text.includes('\t')) return text
  const lines = text.split('\n')
  const out: string[] = []
  let openFence: { char: string; length: number } | null = null
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    if (openFence) {
      out.push(line)
      if (fenceCloses(line, openFence)) openFence = null
      i += 1
      continue
    }
    const fence = fenceMatch(line)
    if (fence) {
      openFence = fence
      out.push(line)
      i += 1
      continue
    }

    const cells = splitTabRow(line)
    if (isTableRow(cells)) {
      let end = i
      while (
        end + 1 < lines.length &&
        end + 2 - i <= MAX_BLOCK_LINES &&
        !fenceMatch(lines[end + 1]) &&
        isTableRow(splitTabRow(lines[end + 1]))
      ) {
        end += 1
      }
      if (end > i) {
        const block: string[][] = []
        for (let j = i; j <= end; j += 1) {
          block.push(splitTabRow(lines[j]))
        }
        const columns = Math.min(
          Math.max(...block.map((row) => row.length)),
          MAX_COLUMNS,
        )
        const pad = (row: string[]) => {
          const r = row.slice(0, columns)
          while (r.length < columns) r.push('')
          return r
        }
        out.push('| ' + pad(block[0]).join(' | ') + ' |')
        out.push('| ' + Array.from({ length: columns }, () => '---').join(' | ') + ' |')
        for (let j = 1; j < block.length; j += 1) {
          out.push('| ' + pad(block[j]).join(' | ') + ' |')
        }
        // The block stopped because the line cap was hit (the next line is
        // still a table row): separate the chunks with a blank line so marked
        // renders two tables instead of merging them (which would show the
        // second delimiter row as literal `<td>---</td>`, A4.9 r1 Minor-4).
        if (
          end + 1 < lines.length &&
          isTableRow(splitTabRow(lines[end + 1]))
        ) {
          out.push('')
        }
        i = end + 1
        continue
      }
    }

    out.push(line)
    i += 1
  }

  return out.join('\n')
}
