// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { ref, type CSSProperties, type Ref } from 'vue'

/**
 * 工具栏浮层（字体/高亮/表格拾取器）锚定定位 —— 笔记模式与工作台模式共用。
 *
 * 教训（fix_five_wave_20261003 ⛔）：缓存 computed 禁包 getBoundingClientRect ——
 * computed 的响应式依赖只有模板 ref（挂载后不变），矩形会被永久缓存，面板拖宽/
 * 改比例后浮层仍开在旧位置。正确形态：**打开瞬间**用 ref 重新测量。
 *
 * 同时把浮层钳制在视口内：工作台模式工具栏靠右，浮层从按钮 left 向右展开会
 * 溢出视口右缘（「取消高亮」按钮点不到）。
 */
export function useAnchoredPickerStyle(btnRef: Ref<HTMLElement | null>) {
  const pickerStyle = ref<CSSProperties>({})
  const PICKER_MAX_WIDTH = 280

  function refreshPickerStyle() {
    const btn = btnRef.value
    if (!btn) {
      pickerStyle.value = {}
      return
    }
    const rect = btn.getBoundingClientRect()
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - PICKER_MAX_WIDTH - 8))
    const top = Math.min(rect.bottom + 4, Math.max(8, window.innerHeight - 80))
    pickerStyle.value = {
      position: 'fixed',
      top: `${top}px`,
      left: `${left}px`,
      zIndex: 9999,
    }
  }

  return { pickerStyle, refreshPickerStyle }
}
