// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * User note-reference [note-ref] block parsing — round-trip companion to the
 * blocks ChatInput.handleSend / sendDraftNow prepend to message content.
 * Exists so edit-mode can restore the references as note chips instead of
 * silently dropping them (2026-11-04: editing a message that carried
 * [note-ref] blocks stripped the markers from the editable text and never
 * restored them; the resent content replaced the DB row, permanently losing
 * the note reference and its inlined body — same loss class as the 2026-10-09
 * [file-ref] wave, which utils/fileRefs covers).
 *
 * Pure module (no vue/pinia imports) so it is unit-testable under plain Node.
 *
 * Known wire-format limitations (pre-existing, shared with MessageBubble's
 * display regex — fixing them means an escaping/format change across all
 * consumers, out of scope here): a title containing `]` breaks the header
 * parse+strip (block falls back to literal text, no data loss), and a body
 * line that is exactly `[/note-ref]` truncates the snapshot at that line.
 */

/** Note reference identity + snapshot body (the note content as inlined at
 *  send time — drafts store the same shape). */
export interface NoteRefData {
  id: string
  title: string
  content: string
  /** Set on edit-restored refs (mirrors FileParseResult.restored): restored
   *  refs are scoped to the edit session — replaced by a later edit session
   *  (restoreEditNotes) and stripped from a normal send (dropRestoredNotes),
   *  so an abandoned edit cannot leak its notes into an unrelated message. */
  restored?: boolean
}

/** Same shape as MessageBubble's NOTE_REF_REGEX — keep in sync. */
export const NOTE_REF_REGEX = /\[note-ref:([^|]*)\|([^\]]*)\]\n([\s\S]*?)\n\[\/note-ref\]\n*/g

/** Broader strip form (matches ChatArea.handleEdit): drops header shapes the
 *  strict parse form skips (e.g. a `|`-less header), so the editable text is
 *  never left with raw block text. */
const NOTE_REF_STRIP_RE = /\[note-ref:[^\]]*\]\n[\s\S]*?\n\[\/note-ref\]\n*/g

/** Parse every [note-ref] block as a NoteRefData (id|title header + snapshot
 *  body), in document order. Malformed blocks (no `|` in the header) are
 *  skipped — mirrors MessageBubble. */
export function parseNoteRefBlocks(content: string): NoteRefData[] {
  const refs: NoteRefData[] = []
  let match: RegExpExecArray | null
  const re = new RegExp(NOTE_REF_REGEX.source, 'g')
  while ((match = re.exec(content)) !== null) {
    refs.push({ id: match[1], title: match[2], content: match[3] })
  }
  return refs
}

/** Remove every [note-ref] block (what remains is the editable message body). */
export function stripNoteRefBlocks(content: string): string {
  return content.replace(NOTE_REF_STRIP_RE, '')
}

/** Emit the [note-ref] block ChatInput prepends on send — single source for
 *  the wire format so parse/build round-trips are guaranteed by one code path. */
export function buildNoteRefBlock(n: { id: string; title: string; content: string }): string {
  return `[note-ref:${n.id}|${n.title}]\n${n.content}\n[/note-ref]\n\n`
}

/** Scope restored refs to the edit session (mirrors fileRefs.restoreEditChips):
 *  previous restored refs are REPLACED (edit A → edit B must not carry A's
 *  notes), genuine manually-added refs are kept, incoming refs are tagged
 *  `restored`. Dedupe by note id — a note already manually referenced is not
 *  doubled and keeps its manual (non-restored) status. */
export function restoreEditNotes(existing: NoteRefData[], incoming: NoteRefData[]): NoteRefData[] {
  const kept = existing.filter((n) => !n.restored)
  const seen = new Set(kept.map((n) => n.id))
  const merged = [...kept]
  for (const n of incoming) {
    if (n.id !== '' && seen.has(n.id)) continue
    if (n.id !== '') seen.add(n.id)
    merged.push({ ...n, restored: true })
  }
  return merged
}

/** Strip restored-tagged refs for a normal (non-edit) send (mirrors
 *  fileRefs.dropRestoredChips): an abandoned edit must not leak the edited
 *  message's notes into an unrelated message. */
export function dropRestoredNotes(notes: NoteRefData[]): NoteRefData[] {
  return notes.filter((n) => !n.restored)
}
