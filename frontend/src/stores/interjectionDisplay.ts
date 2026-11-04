// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 插话内联展示（2026-11-01）。
 *
 * 后端在 run 内每次 commit 插话时，把此前正文/思考 flush 进 display_sequence，
 * 再追加一个 `{type:'interjection', message_id, content, after_index, after_char}`
 * 项；同一份小型侧车写到 `messages.interjection_items`（tool_results 可能被
 * 外置成桩，侧车仍随列表下发）。本模块只做纯函数：
 *  - 持久化序列缺项时按 after_index 插回（payload 桩/旧缓存兼容）；
 *  - 无 display_sequence 时按 after_char 切 content（最后兜底）；
 *  - 顶层 user 插话行只在其 assistant 父行存在时隐藏（不漏消息）；
 *  - 实时 echo 幂等追加（replay 与 live 双投递不重复）。
 */
import type { DisplaySequenceItem, InterjectionDisplayItem, Message } from '../types'

type SeqItem = DisplaySequenceItem & Record<string, unknown>

export function parseInterjectionItems(raw: unknown): InterjectionDisplayItem[] {
  let value = raw
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch {
      return []
    }
  }
  if (!Array.isArray(value)) return []
  return value
    .filter((it): it is Record<string, unknown> => !!it && typeof it === 'object')
    .map((it) => ({
      type: 'interjection' as const,
      message_id: String(it.message_id ?? ''),
      content: String(it.content ?? ''),
      created_at: it.created_at == null ? null : String(it.created_at),
      after_index: Number.isFinite(Number(it.after_index)) ? Number(it.after_index) : undefined,
      after_char: Number.isFinite(Number(it.after_char)) ? Number(it.after_char) : undefined,
    }))
    .filter((it) => !!it.message_id || !!it.content)
}

function toInterjectionItem(it: InterjectionDisplayItem): SeqItem {
  return {
    type: 'interjection',
    message_id: it.message_id,
    content: it.content,
    created_at: it.created_at ?? null,
    after_index: it.after_index,
    after_char: it.after_char,
  }
}

function hasInterjection(seq: SeqItem[], messageId: string): boolean {
  return seq.some((it) => it.type === 'interjection' && it.message_id === messageId)
}

/** Insert persisted items at their after_index; append-only order preserved.
 *  `after_index` is an index into the SAME backend display_sequence, so items
 *  whose boundary already exists (payload carried them) are left untouched. */
export function mergeInterjectionItems(
  seq: SeqItem[],
  items: InterjectionDisplayItem[],
): SeqItem[] {
  const out = [...seq]
  const ordered = [...items].sort(
    (a, b) => (a.after_index ?? Number.MAX_SAFE_INTEGER) - (b.after_index ?? Number.MAX_SAFE_INTEGER),
  )
  for (const item of ordered) {
    if (!item || hasInterjection(out, item.message_id)) continue
    const raw = item.after_index
    const idx = Number.isFinite(raw)
      ? Math.max(0, Math.min(Number(raw), out.length))
      : out.length
    out.splice(idx, 0, toInterjectionItem(item))
  }
  return out
}

/** No display_sequence (payload stub / legacy cache): split the message body
 *  at after_char offsets. Separator comments are left for the renderer, which
 *  already turns `<!-- segment_split -->` into blank lines. */
export function fallbackSequenceFromContent(
  content: string | null | undefined,
  items: InterjectionDisplayItem[],
): SeqItem[] {
  const body = typeof content === 'string' ? content : ''
  const ordered = [...items]
    .filter((it) => Number.isFinite(it.after_char))
    .sort((a, b) => Number(a.after_char) - Number(b.after_char))
  if (!ordered.length) return body ? [{ type: 'text', content: body }] : []
  const out: SeqItem[] = []
  let cursor = 0
  for (const item of ordered) {
    const cut = Math.max(cursor, Math.min(Number(item.after_char), body.length))
    if (cut > cursor) out.push({ type: 'text', content: body.slice(cursor, cut) })
    out.push(toInterjectionItem(item))
    cursor = cut
  }
  if (cursor < body.length) out.push({ type: 'text', content: body.slice(cursor) })
  return out
}

/** Authoritative persisted render sequence for one assistant message. */
export function resolvePersistedInterjectionSequence(
  seq: SeqItem[] | null | undefined,
  items: InterjectionDisplayItem[] | unknown,
  content: string | null | undefined,
): SeqItem[] {
  const base = Array.isArray(seq) ? seq : []
  const parsed = Array.isArray(items) ? items : parseInterjectionItems(items)
  if (!parsed.length) return base
  if (base.some((it) => it.type === 'interjection')) return base
  if (base.length > 0) return mergeInterjectionItems(base, parsed)
  return fallbackSequenceFromContent(content, parsed)
}

/** Hide top-level user interjection rows that are rendered inline by their
 *  assistant parent. Legacy rows (no embedded_in_message_id) stay visible. */
export function filterEmbeddedInterjections<T extends Pick<Message, 'id' | 'role' | 'embedded_in_message_id'>>(
  messages: T[],
  liveParentId?: string | null,
): T[] {
  const assistantIds = new Set(
    messages.filter((m) => m.role === 'assistant').map((m) => m.id),
  )
  return messages.filter((m) => {
    if (m.role !== 'user') return true
    const parent = m.embedded_in_message_id
    if (!parent) return true
    // The in-flight assistant row is excluded from GET /messages until final,
    // but the live stream is rendering its inline copy — hide the duplicate
    // top-level user row via the replay's streaming_message_id.
    return !assistantIds.has(parent) && parent !== (liveParentId || undefined)
  })
}

/** Live commit-echo: append exactly once (replay may deliver the same echo). */
export function applyLiveInterjection(
  seq: SeqItem[],
  item: InterjectionDisplayItem | null | undefined,
): SeqItem[] {
  if (!item) return seq
  if (hasInterjection(seq, item.message_id)) return seq
  return [...seq, toInterjectionItem(item)]
}
