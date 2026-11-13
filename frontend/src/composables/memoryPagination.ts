// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 记忆面板概念列表分页纯函数（P1-19，2026-11-08）。
 *
 * 服务端 `GET /api/memory/concepts?limit=&offset=&history_offset=` 支持
 * 分段游标 + 分段 total；面板把翻页语义收敛成可单测的纯函数（node 侧
 * tests/workflows 直测）。评审修复后统一走 dual 版（单游标版已删除）。
 */

/** 按 id 去重合并两页（保持已有顺序，追加新条目）。 */
export function mergePages<T extends { id: string }>(current: T[], incoming: T[]): T[] {
  const seen = new Set(current.map((x) => String(x.id)))
  const out = current.slice()
  for (const item of incoming || []) {
    const key = String(item.id)
    if (!seen.has(key)) {
      seen.add(key)
      out.push(item)
    }
  }
  return out
}

/**
 * 历史模式双游标（评审修复 2026-11-08）：include_history=true 时服务端
 * `offset` 只偏移现行段、`history_offset` 只偏移历史段。用合并列表的总
 * loaded 做单一 offset 会让两段同时跳步产生空洞。
 */
export interface DualPageState {
  loadedActive: number
  activeTotal: number
  loadedHistory: number
  historyTotal: number
}

/** 任一未加载完就还能翻页。 */
export function dualShouldLoadMore(s: DualPageState): boolean {
  return s.loadedActive < s.activeTotal || s.loadedHistory < s.historyTotal
}

/** 下一页双游标（各自 = 已加载的本类条数）。 */
export function dualCursors(s: DualPageState): { offset: number; historyOffset: number } {
  return { offset: s.loadedActive, historyOffset: s.loadedHistory }
}

/**
 * 客户端分页纯函数（2026-11-12，记忆面板 Dream/澄清/约束/保留淘汰）。
 * 与 MCP/热词/技能/设备 面板同口径（PAGE_SIZE=10 + PaginationBar）。
 * page 带下限钳制：越界/NaN 一律落回第 1 页，绝不 slice 出负区间或错位行（评审 A6-I6）。
 */
export function totalPages(count: number, pageSize: number): number {
  return Math.max(1, Math.ceil((count || 0) / Math.max(1, pageSize)))
}

export function pageSlice<T>(arr: T[], page: number, pageSize: number): T[] {
  const p = Number.isFinite(page) ? Math.max(1, Math.trunc(page)) : 1
  const size = Math.max(1, pageSize)
  const s = (p - 1) * size
  return (arr || []).slice(s, s + size)
}
