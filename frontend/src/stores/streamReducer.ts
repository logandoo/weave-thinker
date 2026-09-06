// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure stream-state reducer helpers (erasable-syntax TS only, import-free —
 * safe to unit-test in Node via type stripping).
 *
 * These encapsulate the ordering/consistency semantics of the live SSE stream
 * vs. the reconnect replay snapshot:
 *
 * - `mergeReplayIntoSequence`  — the resume snapshot must never REGRESS the
 *   live timeline. The live `displaySequence` and the snapshot can each hold
 *   chunks the other lacks (the buffer records events after the client
 *   receives them, so a disconnect leaves a broadcast-into-the-gap event in
 *   the live timeline but not the snapshot; deltas arriving while the client
 *   was down are in the snapshot but not the live timeline). Replacing the
 *   whole timeline with the snapshot makes already-answered content vanish
 *   mid-answer (observed: content disappears from the UI, refresh shows it
 *   saved in the DB). Merge instead: keep every live item, append snapshot
 *   items whose part_id is missing, and for a part present in BOTH take the
 *   longer content (superset semantics) per field.
 * - `pickStreamText`           — accumulators (content / reasoning) only ever
 *   grow to the longer of {live, snapshot}; never shrink.
 * - `shouldApplyRefresh`       — a server message-list snapshot is only
 *   applied when it is not strictly OLDER than the local list (guards the
 *   same-flow concurrent-refresh race: a pre-commit GET resolving after the
 *   post-commit one wipes the just-completed answer).
 */
import type { DisplaySequenceItem } from '../types'

/** Display-sequence item shape with the streaming-only fields the reducer
 *  merges (reasoning_content/result) and a permissive index signature. */
export interface TimelineItem extends DisplaySequenceItem {
  reasoning_content?: string
  result?: string
  [key: string]: unknown
}

export interface StreamTextAccumulator {
  /** Monotonic text accumulator. Pass the CURRENT live value. */
  live: string
  /** Reconnect snapshot value for the same accumulator. */
  snapshot: string
}

/** Keep the LONGER of live vs snapshot. Equal length: keep live (newer
 *  in-place mutations may have happened). */
export function pickStreamText({ live, snapshot }: StreamTextAccumulator): string {
  if (!snapshot) return live
  if (!live) return snapshot
  return live.length >= snapshot.length ? live : snapshot
}

/** Live reasoning tail-window (conv 827a6f78 turn-B, 2026-09-03).
 *
 *  A 113k-char reasoning stream made StreamMarkdown's 80ms whole-tree v-html
 *  re-render explode in cost — the live view froze on a reasoning frame and
 *  the user saw "answer = only the thinking text" (DB stayed clean; refresh
 *  recovered). While a reasoning block is the ACTIVELY-streaming last item,
 *  render only its tail: bounded re-render cost + the streaming tail is what
 *  the user wants to watch anyway. Completed blocks and the persisted message
 *  render full content (non-string defense per the ingest contract). */
export const LIVE_REASONING_TAIL_CHARS = 6000

export function liveReasoningTail(
  content: unknown,
  isStreamingLast: boolean,
  cap: number = LIVE_REASONING_TAIL_CHARS,
): string {
  const c = typeof content === 'string' ? content : ''
  if (!isStreamingLast || c.length <= cap) return c
  return `…（思考已生成 ${c.length} 字，实时显示尾部，完成后展开全文）\n\n` + c.slice(-cap)
}

/** audit_reset client-side timeline surgery (conv 827a6f78 turn-B, 2026-09-03).
 *
 *  The backend's silent QC rejects the streamed draft and resets ITS
 *  accumulators; until now the client never heard about it, so s.content
 *  kept every rejected draft and the done-time answer bubble contained the
 *  whole mess (the clean DB row only appeared after a refresh). Contract:
 *  drop the draft TEXT items after the LAST tool card — tool cards and
 *  reasoning items are preserved (A4 parity: the backend keeps reasoning and
 *  tools on reset, only the rejected draft text goes). With no tool card at
 *  all, every text item is draft. */
export function dropDraftTextAfterLastTool(seq: Array<{ type?: string }> | null | undefined): {
  kept: Array<{ type?: string }>
  changed: boolean
} {
  const items = Array.isArray(seq) ? seq : []
  let lastTool = -1
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i] && items[i].type === 'tool_call') { lastTool = i; break }
  }
  const kept = lastTool >= 0
    ? items.filter((it, i) => i <= lastTool || it?.type !== 'text')
    : items.filter(it => it?.type !== 'text')
  return { kept, changed: kept.length !== items.length }
}

/** Field-level superset merge for a text-ish part present in both the live
 *  timeline and the snapshot: take the LONGER content. For delta-accumulated
 *  parts both values are prefixes of the same underlying text, so the longer
 *  one is the superset. */
export function mergePartContent(live: string | undefined, snapshot: string | undefined): string {
  const l = live || ''
  const s = snapshot || ''
  if (!s) return l
  if (!l) return s
  return l.length >= s.length ? l : s
}

/** Merge the resume replay's display_sequence into the live one.
 *
 *  Rules:
 *  - Every LIVE item is kept as-is (it is at least as new as the snapshot).
 *  - A snapshot item whose part_id is already present live is MERGED into the
 *    live item field-by-field: content / reasoning_content take the LONGER
 *    value (the snapshot may hold deltas that arrived while the client was
 *    disconnected — dropping them would leave a display hole), other fields
 *    fill in when the live item lacks them.
 *  - A snapshot item with a part_id the live timeline lacks is APPENDED.
 *  - Snapshot items WITHOUT a part_id (legacy shapes) are appended unless an
 *    identical (type, content) item already exists anywhere in the result.
 */
export function mergeReplayIntoSequence(
  currentItems: TimelineItem[],
  replayItems: TimelineItem[] | null | undefined,
): TimelineItem[] {
  const out: TimelineItem[] = [...currentItems]
  if (!Array.isArray(replayItems)) return out
  const byId = new Map<string, TimelineItem>()
  for (const it of currentItems) {
    if (it.part_id) byId.set(it.part_id, it)
  }
  for (const rp of replayItems) {
    if (!rp) continue
    if (rp.part_id) {
      const liveItem = byId.get(rp.part_id)
      if (liveItem) {
        // Same part in both: superset-merge the streaming fields, fill gaps
        // in the rest. Mutating the LIVE item keeps its reactive proxy valid.
        liveItem.content = mergePartContent(
          typeof liveItem.content === 'string' ? liveItem.content : '',
          typeof rp.content === 'string' ? rp.content : '',
        )
        for (const key of ['reasoning_content', 'result']) {
          const lv = liveItem[key]
          const sv = rp[key]
          if (typeof lv === 'string' && typeof sv === 'string') {
            liveItem[key] = mergePartContent(lv, sv)
          } else if (lv === undefined || lv === null || lv === '') {
            if (sv !== undefined && sv !== null) liveItem[key] = sv
          }
        }
        for (const key of ['status', 'title', 'error']) {
          if (liveItem[key] === undefined || liveItem[key] === null) {
            if (rp[key] !== undefined && rp[key] !== null) liveItem[key] = rp[key]
          }
        }
        continue
      }
      out.push({ ...rp })
      byId.set(rp.part_id, out[out.length - 1])
    } else {
      // Legacy (no part_id) items cannot be keyed. Dedup by identity
      // anywhere in the result (tail-only dedup double-renders [A,B]+[A,B]).
      const dup = out.some(it => !it.part_id && it.type === rp.type && it.content === rp.content)
      if (dup) continue
      out.push({ ...rp })
    }
  }
  return out
}

/** Is this local message id a store-synthesized placeholder (optimistic
 *  bubble / local fallback) rather than a server row? The server legitimately
 *  lacks these ids until the next refresh reconciles them. */
export function isSyntheticMessageId(id: string): boolean {
  return /^(temp-|local-abort-|resume-|resume-complete-|bg-)/.test(id)
}

/**
 * May a fetched server message-list snapshot replace the local list?
 *
 * The snapshot is strictly older than the local view when the newest local
 * message (a REAL id, not a synthetic placeholder) is absent from it.
 * Applying such a snapshot wipes messages the user can already see (the
 * completed answer pushed at `done` but not yet visible to a pre-commit GET).
 * Synthetic placeholders are exempt: the server legitimately lacks them until
 * the next refresh reconciles ids.
 */
export function shouldApplyRefresh(local: Array<{ id: string }>, server: Array<{ id: string }>): boolean {
  if (!Array.isArray(local) || local.length === 0) return true
  const newest = local[local.length - 1]
  if (!newest || typeof newest.id !== 'string') return true
  if (!Array.isArray(server)) return false
  if (server.some(m => m.id === newest.id)) return true
  if (isSyntheticMessageId(newest.id)) return true
  return false
}
