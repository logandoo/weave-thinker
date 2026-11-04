// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 选区/光标恢复的纯决策逻辑（笔记遗留 #2）。
 *
 * 编辑器每次 DOM 手术（格式化的拆分/重包/空壳回收）或整篇重渲染都会重建文本节点，
 * 浏览器的 Range 端点随节点移动被重映射（实测：上/下标后折叠到新元素首字符前，
 * 后续键入被吸进上标）。恢复策略必须**有门禁**，否则会把光标从用户当前操作位置
 * 拽回旧位置（外部插入、切换笔记、IME 组合期）。
 *
 * 语义固定见 docs/PLAN_notes_caret_restore_20261024.md 的 Consistency Hub。
 */

export interface SelectionSnapshot {
  /** 选区起点 = root 内该点之前的文本长度。 */
  startOffset: number
  /** 选区终点；自操作后的折叠落点即此值。 */
  endOffset: number
  /** 快照时刻的「可比文本」（剔除图表/公式/媒体渲染产物）—— 恢复前必须逐字相同。 */
  text: string
}

/** 重渲染后是否恢复选区：文本未变 + 编辑器仍持焦点 + 非 IME 组合期。 */
export function shouldRestoreAfterRender(
  snapshot: SelectionSnapshot | null,
  currentText: string,
  editorHasFocus: boolean,
  composing: boolean,
): boolean {
  if (!snapshot) return false
  if (composing) return false
  if (!editorHasFocus) return false
  return currentText === snapshot.text
}
