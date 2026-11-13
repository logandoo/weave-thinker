// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Keyboard inset (2026-11-10): 鸿蒙 5.x / ArkWeb、iOS Safari、Chrome ≥108
 * (interactive-widget=resizes-visual 默认) 弹出软键盘时只缩「视觉视口」，
 * 布局视口（innerHeight / 100dvh）不变 —— 100dvh 应用外壳的底部输入框被
 * 键盘盖住。需要顶起的距离恰为 innerHeight − visualViewport.height：
 * Android adjustResize 机型两者同缩 → 结果为 0，不会双重顶起。
 *
 * 消费方：ChatLayout.vue 写入 CSS 变量 --keyboard-inset（移动端媒体查询里
 * .chat-layout 高度与 .mobile-tab-bar bottom 各减去/加上它）。
 */

/** 小于该值的高度差视为瞬时 UI（工具条等）噪声，不认定为键盘。 */
export const KEYBOARD_INSET_MIN_PX = 60

/**
 * @param innerHeight 布局视口高度（window.innerHeight）
 * @param visualHeight 视觉视口高度（visualViewport.height）
 * @param visualScale 视觉视口缩放（捏合缩放 >1 时不可用作键盘判据）
 * @returns 需要顶起的像素数；0 表示无需干预
 */
export function computeKeyboardInset(
  innerHeight: number,
  visualHeight: number,
  visualScale = 1,
): number {
  if (visualScale > 1) return 0
  const overlap = innerHeight - visualHeight
  return overlap > KEYBOARD_INSET_MIN_PX ? overlap : 0
}
