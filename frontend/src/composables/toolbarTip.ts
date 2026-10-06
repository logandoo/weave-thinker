// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 工具栏说明浮层锚定（笔记模式/工作台模式共用）。
 *
 * .editor-toolbar / .zen-note-toolbar 是 overflow-x:auto 滚动容器 —— 里面
 * absolute 定位的 ::after 会被裁剪（overflow-y 计算为 auto，A4.9 H2 实测
 * 悬停浮层不可见）。fixed 定位脱离滚动容器裁剪；坐标经 CSS 变量注入。
 */
export function useToolbarTip() {
  function onTipPointer(e: Event) {
    const target = e.target as HTMLElement | null
    const btn = target?.closest?.('[data-tip]') as HTMLElement | null
    if (!btn || !btn.hasAttribute('data-tip')) return
    const r = btn.getBoundingClientRect()
    btn.style.setProperty('--tip-x', `${Math.round(r.left + r.width / 2)}px`)
    btn.style.setProperty('--tip-y', `${Math.round(r.bottom + 6)}px`)
  }
  return { onTipPointer }
}
