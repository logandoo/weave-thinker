// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 任务作用域 UI 纯派生逻辑（2026-11-10 波 · T14）。
 *
 * 放 utils 层做单测缝（tests/workflows/test_workspace_scope_chip.cjs）：
 * 徽标可见性、文案、改绑 CAS 冲突判定与历史行摘要都不依赖 Vue 运行时。
 */
import type { BindingHistoryEntry, BindingSnapshot, WorkspaceCapabilities } from '@/api/workspace'

export const BINDING_STALE_CODE = 'workspace_binding_stale'

/** `off` = 现网零变化，徽标完全不出现；未绑定（legacy 会话）也不出现。 */
export function scopeChipVisible(
  caps: WorkspaceCapabilities | null,
  snap: BindingSnapshot | null,
): boolean {
  if (!caps || caps.mode === 'off') return false
  return !!snap?.bound && !!snap.task
}

/** 徽标文案：`项目名 · 任务标题`（无项目时只任务名）。 */
export function bindingBadgeText(snap: BindingSnapshot | null): string {
  if (!snap?.bound || !snap.task) return ''
  const title = snap.task.title || '未命名任务'
  return snap.project?.name ? `${snap.project.name} · ${title}` : title
}

/** shadow 档徽标要能自证「只是观测」，避免用户误以为已隔离。 */
export function scopeModeHint(caps: WorkspaceCapabilities | null, snap: BindingSnapshot | null): string {
  const mode = snap?.mode || caps?.mode || 'off'
  if (mode === 'shadow') return '任务作用域（shadow：仅记录，不拦截）'
  if (mode === 'enforce') return '任务作用域（enforce：文件只进当前任务）'
  return ''
}

/** 改绑 CAS 冲突：版本漂移 → 需要刷新快照重试，而不是重复提交。 */
export function isBindingStale(errorCode: string): boolean {
  return errorCode === BINDING_STALE_CODE
}

/** 历史行：`旧任务 → 新任务（actor：原因）`；首绑 from 为空。 */
export function historyLine(
  entry: BindingHistoryEntry,
  titleOf: (taskId: string | null) => string,
): string {
  const from = entry.from_task_id ? titleOf(entry.from_task_id) : '（首绑）'
  const to = titleOf(entry.to_task_id)
  const reason = entry.reason ? `：${entry.reason}` : ''
  return `${from} → ${to}（${entry.actor}${reason}）`
}

/** 跨域交付卡来源徽标文案（无来源 = 本域，不显示）。 */
export function sourceBadgeText(att: {
  source_scope?: string
  source_label?: string
}): string {
  if (!att.source_scope || !att.source_label) return ''
  return `来自${att.source_label}`
}
