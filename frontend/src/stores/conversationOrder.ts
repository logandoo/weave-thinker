// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Sidebar conversation ordering — pure helpers (2026-09-15).
 *
 * Single source of truth for the sidebar list order, mirroring the backend
 * `list_conversations` ORDER BY:
 *     coalesce(sort_order, 0) ASC ,
 *     coalesce(last_user_message_at, updated_at) DESC
 * (PG `DESC` defaults to NULLS FIRST — mirrored by the +Infinity sentinel.)
 *
 * Kept dependency-free (structural type, no '@/types' import) so the node unit
 * test can import it directly — see frontend/tests/workflows/test_conversation_order.cjs.
 */

export interface SidebarOrderableConversation {
  id: string
  sort_order?: number | null
  last_user_message_at?: string | null
  updated_at?: string | null
}

/** 本地乐观置顶的完整前值快照：追平判定用服务端时钟域（previous*），
 *  乐观展示值用客户端时钟域（promotedAt）——两者不混用（A4.9 Minor-2）。
 *  注：last_user_message_at 字段语义 = 最近活动时间（2026-09-26 起含助手
 *  消息，后端 MAX 全角色 created_at），字段名沿用仅为 API 兼容。 */
export interface PendingPromotion {
  /** 客户端时钟：乐观 last_user_message_at 展示值（不参与追平判定）。 */
  promotedAt: number
  /** 服务端时钟：提升前的最近活动时间（epoch ms；无 = 0）——追平基准。 */
  previousActivityMs: number
  previousSortOrder: number
  previousLastUserMessageAt: string | null
}

/** 排序用活动时间：最近活动（全角色消息，2026-09-26 起），缺失回退 updated_at；两者皆缺 → +∞
 *  （对齐 PG `ORDER BY coalesce(...) DESC` 的 NULLS FIRST）。 */
export function conversationActivityMs(conv: SidebarOrderableConversation): number {
  const iso = conv.last_user_message_at || conv.updated_at
  if (!iso) return Number.POSITIVE_INFINITY
  const ms = new Date(iso).getTime()
  return Number.isFinite(ms) ? ms : 0
}

/** 服务端「最近活动」时间（仅 last_user_message_at 字段，不用 updated_at 回退）
 *  ——追平判定的唯一权威输入；缺失/非法 = 0。 */
export function serverLastUserMessageMs(conv: SidebarOrderableConversation): number {
  const ms = conv.last_user_message_at ? new Date(conv.last_user_message_at).getTime() : 0
  return Number.isFinite(ms) && ms > 0 ? ms : 0
}

/** 服务端快照是否已包含本次操作的新活动（最近活动严格晚于提升前值）——纯服务端
 *  时间戳互比，客户端时钟偏差不影响判定（A4.9 Minor-2）。发送/编辑=新用户消息，
 *  重新生成=新助手消息落库后追平（2026-09-26 全角色口径）。 */
export function serverCaughtUp(
  conv: SidebarOrderableConversation,
  pending: PendingPromotion,
): boolean {
  return serverLastUserMessageMs(conv) > pending.previousActivityMs
}

/** Comparator mirroring the backend ORDER BY (ascending return = earlier). */
export function compareConversationsBySidebarOrder(
  a: SidebarOrderableConversation,
  b: SidebarOrderableConversation,
): number {
  const orderA = a.sort_order ?? 0
  const orderB = b.sort_order ?? 0
  if (orderA !== orderB) return orderA - orderB
  return conversationActivityMs(b) - conversationActivityMs(a)
}

/**
 * 本地乐观置顶的服务端追平检查。
 *
 * 并发快照（loadConversations / refreshConversation 的 GET）可能在本次发送的
 * 服务端提交之前生成、在本地置顶之后才到达：只要服务端快照的最近活动仍
 * 未晚于提升前值，就保留本地置顶（否则刚置顶的会话会被旧快照打回原位）；服务端
 * 追平后清除 pending 标记（此后由服务端值主导）。
 *
 * @param pending convId → 置顶前值快照，由调用方持有并原地更新。
 * @returns true = 列表已被就地重排（调用方如需响应式更新需自行触发）。
 */
export function reconcilePendingPromotions(
  list: SidebarOrderableConversation[],
  pending: Record<string, PendingPromotion>,
): boolean {
  let changed = false
  for (const conv of list) {
    const p = pending[conv.id]
    if (!p) continue
    if (serverCaughtUp(conv, p)) {
      delete pending[conv.id]
      continue
    }
    conv.sort_order = 0
    conv.last_user_message_at = new Date(p.promotedAt).toISOString()
    changed = true
  }
  if (changed) list.sort(compareConversationsBySidebarOrder)
  return changed
}

/**
 * 回滚本地乐观置顶（发送被最终拒绝、消息未送达时）——恢复提升前的
 * sort_order / last_user_message_at 并清除 pending 标记（A4.9 Minor-3）。
 *
 * @returns true = 发生了回滚（调用方需重排列表）。
 */
export function rollbackPendingPromotion(
  conv: SidebarOrderableConversation,
  pending: Record<string, PendingPromotion>,
): boolean {
  const p = pending[conv.id]
  if (!p) return false
  delete pending[conv.id]
  conv.sort_order = p.previousSortOrder
  conv.last_user_message_at = p.previousLastUserMessageAt
  return true
}
