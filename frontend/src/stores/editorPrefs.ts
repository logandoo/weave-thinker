// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { ref } from 'vue'
import { defineStore } from 'pinia'
import { setEndnoteEnabled, clearRenderCache } from '@/composables/useMarkdown'

const ENDNOTE_KEY = 'wt-endnote-enabled'

/**
 * 编辑器偏好（wave-2 B1）。尾注总开关默认关：
 * 关 = `[^id]`/`[^id]:` 全应用保持字面（历史行为）；
 * 开 = 尾注渲染 + 工具栏「尾注」按钮可用。
 * 持久化 localStorage（与 wt-skin 同策略）；切换即清渲染缓存。
 */
export const useEditorPrefsStore = defineStore('editorPrefs', () => {
  const endnoteEnabled = ref<boolean>(localStorage.getItem(ENDNOTE_KEY) === '1')
  setEndnoteEnabled(endnoteEnabled.value)
  // 初始化同样清缓存：store 惰性创建前可能已有按关闭态渲染的缓存（复审 B#4）
  clearRenderCache()

  function setEndnote(v: boolean) {
    endnoteEnabled.value = v
    localStorage.setItem(ENDNOTE_KEY, v ? '1' : '0')
    setEndnoteEnabled(v)
    clearRenderCache()
  }

  return { endnoteEnabled, setEndnote }
})
