// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 「移动到分组」菜单分区（Sidebar / 工作台抽屉共用纯逻辑）。
 *
 * 背景：菜单此前只列「当前助手」的分组 + （Sidebar）一排「移动到其他助手」按钮，
 * 用户想把对话直接移动到「其他助手的某个分组」必须先切到那个助手再操作。后端
 * `PUT /conversations/{id}/move` 已支持 `group_id` + `assistant_id` 同发做跨助手移动
 * （conversation.py 校验目标分组必须属于 resulting assistant），缺的只是把
 * 「助手 × 分组」摊成一个可选的菜单分区。
 *
 * 本模块只做纯映射：给定助手列表、当前助手、全量分组 → 每个助手一节，节内只放该
 * 助手的分组，当前助手排在最前。无副作用、不触网、不依赖 vue/pinia，可单测。
 */
import type { Assistant, ConversationGroup } from '@/types'

export interface MoveMenuSection {
  /** 该节对应的助手 id；当前助手节可能为 null（未选助手）。 */
  assistantId: string | null
  /** 助手显示名；当前助手节由 UI 决定是否打标签（isCurrent 驱动）。 */
  assistantName: string
  /** 是否为当前助手节（排最前、UI 可不显示标签）。 */
  isCurrent: boolean
  /** 该助手名下的分组，按 sort_order 升序稳定排序。 */
  groups: ConversationGroup[]
}

/** 分组归属判定：null / undefined 视为同一无归属桶。 */
function sameOwner(a: string | null | undefined, b: string | null | undefined): boolean {
  return (a ?? null) === (b ?? null)
}

/**
 * 把「助手 × 分组」摊成菜单分区。
 * - 当前助手节永远排第一（即便 currentAssistantId 不在 assistants 里）。
 * - 其余助手按传入顺序跟随，当前助手不重复出现。
 * - 分组按 assistant_id 归入对应节；归属助手不在列表中的孤儿分组被丢弃
 *   （移动到不存在的助手无意义）。
 * - 无分组的助手仍保留一节（至少可移到「未分组」）。
 * - 不修改入参。
 */
export function buildMoveMenuSections(
  assistants: readonly Pick<Assistant, 'id' | 'name'>[],
  currentAssistantId: string | null,
  groups: readonly ConversationGroup[],
): MoveMenuSection[] {
  const groupsFor = (assistantId: string | null): ConversationGroup[] =>
    groups
      .filter((g) => sameOwner(g.assistant_id, assistantId))
      .slice()
      .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))

  const currentName = assistants.find((a) => sameOwner(a.id, currentAssistantId))?.name ?? ''
  const sections: MoveMenuSection[] = [
    {
      assistantId: currentAssistantId,
      assistantName: currentName,
      isCurrent: true,
      groups: groupsFor(currentAssistantId),
    },
  ]

  for (const a of assistants) {
    if (sameOwner(a.id, currentAssistantId)) continue
    sections.push({
      assistantId: a.id,
      assistantName: a.name,
      isCurrent: false,
      groups: groupsFor(a.id),
    })
  }

  return sections
}

/**
 * 合并「当前助手分组（列表已加载）」与「全量分组（菜单打开时加载）」，按 id 去重，
 * 全量优先（含 conversation_count 等最新字段）。保证菜单在全量请求返回前也能先显示
 * 当前助手的分组，避免只有「未分组」的闪烁。纯函数，不修改入参。
 */
export function mergeGroupsById(
  primary: readonly ConversationGroup[],
  extra: readonly ConversationGroup[],
): ConversationGroup[] {
  const byId = new Map<string, ConversationGroup>()
  for (const g of primary) byId.set(g.id, g)
  for (const g of extra) byId.set(g.id, g)
  return Array.from(byId.values())
}

/**
 * 按搜索词过滤菜单分区（助手/分组很多时靠搜索定位，避免几百项长列表）。
 * - 空查询 → 原样返回（不修改入参）。
 * - 助手名命中 → 保留该节全部（含其未分组与所有分组）。
 * - 否则只保留名称命中的分组；该节无命中则整节丢弃。
 * 大小写不敏感（中英文均可）。
 */
export function filterMoveMenuSections(
  sections: readonly MoveMenuSection[],
  query: string,
): MoveMenuSection[] {
  const q = query.trim().toLowerCase()
  if (!q) return sections.slice()
  const out: MoveMenuSection[] = []
  for (const s of sections) {
    if (s.assistantName.toLowerCase().includes(q)) {
      out.push(s)
      continue
    }
    const matched = s.groups.filter((g) => g.name.toLowerCase().includes(q))
    if (matched.length) out.push({ ...s, groups: matched })
  }
  return out
}
