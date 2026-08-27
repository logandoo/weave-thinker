// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * useMediaQuery — 电脑/手机模式断点的唯一来源。
 *
 * 全项目移动端断点统一为 MOBILE_BREAKPOINT (767px)，与 CSS 的
 * `@media (max-width: 767px)` 保持一致。模块级单例 ref + 共享 resize
 * 监听器，取代散落在 ChatInput / Sidebar / useMobileNavigation 里的
 * `window.innerWidth < 768` 判断（768 与 CSS 767 存在 off-by-one）。
 */
import { computed, readonly, ref } from 'vue'

export const MOBILE_BREAKPOINT = 767

const isMobile = ref(
  typeof window !== 'undefined' ? window.innerWidth <= MOBILE_BREAKPOINT : false,
)

if (typeof window !== 'undefined') {
  window.addEventListener('resize', () => {
    isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
  })
}

/** 同步判断（事件处理器内使用）。 */
export function isMobileViewport(): boolean {
  return isMobile.value
}

export function useMediaQuery() {
  return {
    /** 响应式移动端状态（readonly ref）。 */
    isMobile: readonly(isMobile),
    isDesktop: computed(() => !isMobile.value),
    isMobileViewport,
  }
}
