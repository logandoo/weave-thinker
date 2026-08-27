// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import type { RouteLocationRaw, Router } from 'vue-router'
import { isMobileViewport as _isMobileViewport, MOBILE_BREAKPOINT as _MOBILE_BREAKPOINT } from './useMediaQuery'

// 兼容旧导入：断点判断统一收敛到 useMediaQuery 单例。
export const MOBILE_BREAKPOINT = _MOBILE_BREAKPOINT

export function isMobileViewport() {
  return _isMobileViewport()
}

export async function navigateWithMobileHistory(
  router: Router,
  to: RouteLocationRaw,
  options: { replace?: boolean } = {},
) {
  if (options.replace === true) {
    return router.replace(to)
  }
  return router.push(to)
}