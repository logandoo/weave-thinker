// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/** W26 §B3 — 插话 commit-echo ↔ pending 条目的配对规则（纯函数，便于单测）。
 *
 * 规则（与 HANDOFF_upstream_w26 §B3 对齐）：
 * 1. echo 带 `client_message_id` → **只**按 id 配对。id 不匹配（旧 replay /
 *    他端连接 / 被拒的过长 id）绝不回落内容 —— 那会确认一条无关的同文条目，
 *    随后被当成已 commit 出队（用户文本静默丢失）。
 * 2. echo 无 id（旧后端 live echo / agent_worker 字符串队列项）→ 内容兜底：
 *    - live：可确认任意未确认同文条目（旧后端无 replay，无错配放大面）；
 *    - replay 复放：只确认**不带 cmid** 的条目 —— 新客户端条目都带 cmid，
 *      旧 echo 复放不得把它们错标为已 commit。
 */

export interface PendingInterjectionLike {
  content: string
  committed?: boolean
  clientMessageId?: string
}

export interface InterjectionEchoLike {
  content?: string | null
  client_message_id?: string | null
}

/** 返回该 echo 确认的 pending 条目下标；-1 = 无可确认条目。 */
export function pairInterjectionEcho(
  pending: ReadonlyArray<PendingInterjectionLike>,
  payload: InterjectionEchoLike,
  opts?: { fromReplay?: boolean },
): number {
  if (payload.client_message_id) {
    return pending.findIndex(p => p.clientMessageId === payload.client_message_id)
  }
  if (!payload.content) return -1
  const matches = pending
    .map((p, i) => ({ p, i }))
    .filter(x => x.p.content === payload.content)
  let pick = matches.find(x => !x.p.committed && (!opts?.fromReplay || !x.p.clientMessageId))
  if (!pick && !opts?.fromReplay) {
    pick = matches.find(x => !x.p.committed) ?? matches[0]
  }
  return pick ? pick.i : -1
}
