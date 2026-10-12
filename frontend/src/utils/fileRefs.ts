// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * User-upload [file-ref] block parsing — round-trip companion to the blocks
 * ChatInput.handleSend prepends to message content. Exists so edit-mode can
 * restore the attachments as upload chips instead of silently dropping them
 * (2026-10-09: editing a message with uploads stripped the markers from the
 * editable text and never restored them; the resent content replaced the DB
 * row, permanently losing the file cards and the backend `[file-ref:` skill
 * trigger — the agent then fell back to reading file paths from memory).
 *
 * Pure module (no vue/pinia imports) so it is unit-testable under plain Node.
 */
import type { FileParseResult } from '../api/fileUpload'

/** Same shape as MessageBubble's FILE_REF_REGEX — keep in sync. */
export const FILE_REF_REGEX = /\[file-ref:([^\]]*)\]\n([\s\S]*?)\n\[\/file-ref\]\n*/g

function refMeta(content: string, label: string): string {
  const m = content.match(new RegExp(`${label}[：:]\\s*(.+)`))
  return m ? m[1].trim() : ''
}

/** Parse the display size text ("123 B" / "1.2 KB" / "3.4 MB" / "未知") back
 *  to bytes; unparseable/unknown → undefined. */
export function parseFileSizeText(text: string): number | undefined {
  const t = (text || '').trim()
  let m = t.match(/^([\d.]+)\s*B$/)
  if (m) return Math.round(Number(m[1]))
  m = t.match(/^([\d.]+)\s*KB$/)
  if (m) return Math.round(Number(m[1]) * 1024)
  m = t.match(/^([\d.]+)\s*MB$/)
  if (m) return Math.round(Number(m[1]) * 1048576)
  return undefined
}

/** Extract every [file-ref] block as a FileParseResult chip (upload-store
 *  shape). New messages carry 工作区路径 (workspace-relative), legacy ones
 *  文件路径 (absolute) — both accepted, mirroring MessageBubble. */
export function parseFileRefBlocks(content: string): FileParseResult[] {
  const refs: FileParseResult[] = []
  let match: RegExpExecArray | null
  const re = new RegExp(FILE_REF_REGEX.source, 'g')
  while ((match = re.exec(content)) !== null) {
    const body = match[2] || ''
    const relPath = refMeta(body, '工作区路径')
    const absolutePath = refMeta(body, '文件路径')
    if (!relPath && !absolutePath) continue
    refs.push({
      success: true,
      filename: match[1],
      rel_path: relPath || null,
      file_path: absolutePath || null,
      file_type: refMeta(body, '文件类型') || null,
      size: parseFileSizeText(refMeta(body, '文件大小')),
    })
  }
  return refs
}

/** Scope restored chips to the edit session (A4.9 I-1, 2026-10-09): previous
 *  restored chips are REPLACED (edit A → edit B must not attach A's files),
 *  genuine pending uploads are kept, incoming chips are tagged `restored` so a
 *  later normal send can drop them (see dropRestoredChips). Dedupe by path. */
export function restoreEditChips(existing: FileParseResult[], incoming: FileParseResult[]): FileParseResult[] {
  const kept = existing.filter((f) => !f.restored)
  return mergeFileRefChips(kept, incoming.map((c) => ({ ...c, restored: true })))
}

/** Strip restored-tagged chips for a normal (non-edit) send: an abandoned
 *  edit must not leak the edited message's attachments into an unrelated
 *  message (A4.9 I-1, 2026-10-09). */
export function dropRestoredChips(files: FileParseResult[]): FileParseResult[] {
  return files.filter((f) => !f.restored)
}

/** Merge edit-restored chips into the pending upload list, deduped by
 *  workspace path (rel_path || file_path) so a file already pending is not
 *  doubled. */
export function mergeFileRefChips(existing: FileParseResult[], incoming: FileParseResult[]): FileParseResult[] {
  const seen = new Set(
    existing.map((f) => f.rel_path || f.file_path || '').filter((p) => p !== ''),
  )
  const merged = [...existing]
  for (const f of incoming) {
    const key = f.rel_path || f.file_path || ''
    if (key !== '' && seen.has(key)) continue
    if (key !== '') seen.add(key)
    merged.push(f)
  }
  return merged
}
