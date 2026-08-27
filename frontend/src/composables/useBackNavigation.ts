// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import router from '@/router'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { navigateWithMobileHistory } from '@/composables/useMobileNavigation'

/**
 * Route hierarchy for Android back navigation.
 * Maps route names to their parent route paths.
 */
const parentMap: Record<string, string> = {
  NoteEditor: '', // dynamic — handled in getParentRoute
  Notes: '/notes',
  Notebooks: '/',
  Chat: '/',
  // 语音助理 (full-duplex voice) and 工作台 (zen) are top-level modules entered FROM
  // the main UI — the back button must return to the main UI, not pop the
  // exit-app confirmation.
  Voice: '/',
  Zen: '/',
}

function getParentRoute(): string | null {
  const route = router.currentRoute.value
  const name = route.name as string

  if (name === 'NoteEditor') {
    const notebookId = route.params.notebookId
    return `/notes/${notebookId}`
  }

  return parentMap[name] ?? null
}

/**
 * Called from Android WebView's onBackPressed.
 * Always returns true so Android never exits directly.
 * Shows exit confirmation dialog when at root route.
 */
export function handleAndroidBack(): boolean {
  const parent = getParentRoute()
  if (parent === null || parent === router.currentRoute.value.path) {
    // At root — show exit confirmation
    showExitConfirmation()
    return true
  }
  void navigateWithMobileHistory(router, parent)
  return true
}

async function showExitConfirmation() {
  const { confirm } = useConfirmDialog()
  const shouldExit = await confirm({
    message: '确定要退出 Weave Thinker 吗？',
    confirmText: '退出',
    cancelText: '取消',
  })
  if (shouldExit) {
    // Call Android bridge to exit app, or close window for browser
    if ((window as any).WeaverNoteApp?.exitApp) {
      (window as any).WeaverNoteApp.exitApp()
    } else {
      window.close()
    }
  }
}
