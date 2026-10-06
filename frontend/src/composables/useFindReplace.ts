// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { ref, nextTick, type Ref } from 'vue'

/**
 * 查找/替换（笔记模式 NoteEditor 与工作台模式 ZenNotePanel 共用 —— wave-2 C2 抽取）。
 * 两壳原为逐字复制（已漂移出注释级分歧）；逻辑单源化后行为天然同步。
 */

export interface FindReplaceEditor {
  getTextContent?: () => string
  replaceTextRange?: (start: number, end: number, text: string) => boolean | undefined
  editorRef?: HTMLElement
}

export interface FindReplaceDeps {
  getEditor: () => FindReplaceEditor | null | undefined
  findInputRef: Ref<HTMLInputElement | null>
  /** 替换落库标记（壳侧 hasChanges） */
  onMutate: () => void
}

export function useFindReplace(deps: FindReplaceDeps) {
  const showFindBar = ref(false)
  const showReplaceBar = ref(false)
  const findQuery = ref('')
  const replaceQuery = ref('')
  const matchPositions = ref<number[]>([])
  const currentMatchIndex = ref(-1)

  function computeMatches() {
    const text = deps.getEditor()?.getTextContent?.() || ''
    const query = findQuery.value
    if (!query) {
      matchPositions.value = []
      currentMatchIndex.value = -1
      return
    }
    const positions: number[] = []
    let idx = 0
    const lowerText = text.toLowerCase()
    const lowerQuery = query.toLowerCase()
    while (idx < lowerText.length) {
      const found = lowerText.indexOf(lowerQuery, idx)
      if (found === -1) break
      positions.push(found)
      idx = found + 1
    }
    matchPositions.value = positions
    if (positions.length === 0) {
      currentMatchIndex.value = -1
    } else if (currentMatchIndex.value >= positions.length) {
      currentMatchIndex.value = 0
    }
  }

  function highlightCurrentMatch() {
    if (matchPositions.value.length === 0 || currentMatchIndex.value < 0) return
    // Do NOT select text in editor - the highlight CSS already visually marks which match is current.
    // Selecting text would cause Enter key to replace the matched keyword with a newline.
    const editorEl = deps.getEditor()?.editorRef
    if (!editorEl) return
    const currentMark = editorEl.querySelector('.find-match-current') as HTMLElement | null
    if (currentMark) {
      currentMark.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }

  function onFindInput() {
    computeMatches()
    if (matchPositions.value.length > 0) {
      currentMatchIndex.value = 0
      nextTick(() => highlightCurrentMatch())
    }
  }

  function findNext() {
    if (matchPositions.value.length === 0) return
    currentMatchIndex.value = (currentMatchIndex.value + 1) % matchPositions.value.length
    nextTick(() => highlightCurrentMatch())
  }

  function findPrev() {
    if (matchPositions.value.length === 0) return
    currentMatchIndex.value = (currentMatchIndex.value - 1 + matchPositions.value.length) % matchPositions.value.length
    nextTick(() => highlightCurrentMatch())
  }

  function onFindKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (e.shiftKey) {
        findPrev()
      } else {
        findNext()
      }
    } else if (e.key === 'Escape') {
      e.preventDefault()
      closeFindBar()
    }
  }

  function onReplaceKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault()
      closeFindBar()
    }
  }

  function replaceCurrent() {
    const editor = deps.getEditor()
    if (!editor || matchPositions.value.length === 0 || currentMatchIndex.value < 0) return
    const pos = matchPositions.value[currentMatchIndex.value]
    const queryLen = findQuery.value.length
    const success = editor.replaceTextRange?.(pos, pos + queryLen, replaceQuery.value)
    if (success) {
      deps.onMutate()
      computeMatches()
      if (matchPositions.value.length === 0) {
        currentMatchIndex.value = -1
      } else if (currentMatchIndex.value >= matchPositions.value.length) {
        currentMatchIndex.value = 0
      }
      nextTick(() => highlightCurrentMatch())
    }
  }

  function replaceAll() {
    const query = findQuery.value
    if (!query || matchPositions.value.length === 0) return
    const editor = deps.getEditor()
    if (!editor) return

    // Replace from last match to first to avoid index shifts
    for (let i = matchPositions.value.length - 1; i >= 0; i--) {
      const pos = matchPositions.value[i]
      editor.replaceTextRange?.(pos, pos + query.length, replaceQuery.value)
    }
    deps.onMutate()
    computeMatches()
    currentMatchIndex.value = matchPositions.value.length > 0 ? 0 : -1
    nextTick(() => highlightCurrentMatch())
  }

  function openFindBar(withReplace: boolean) {
    if (showFindBar.value && showReplaceBar.value === withReplace) {
      closeFindBar()
      return
    }
    showFindBar.value = true
    showReplaceBar.value = withReplace
    nextTick(() => {
      deps.findInputRef.value?.focus()
      if (findQuery.value) {
        computeMatches()
        if (matchPositions.value.length > 0) {
          currentMatchIndex.value = 0
          nextTick(() => highlightCurrentMatch())
        }
      }
    })
  }

  function closeFindBar() {
    showFindBar.value = false
    showReplaceBar.value = false
    // 保留查询词（wave-3 #6）：重开找回上次查询；高亮清理由 find-active
    // 翻转触发引擎 prop watch → removeFindHighlights 负责，无需清词。
    matchPositions.value = []
    currentMatchIndex.value = -1
  }

  return {
    showFindBar,
    showReplaceBar,
    findQuery,
    replaceQuery,
    matchPositions,
    currentMatchIndex,
    computeMatches,
    highlightCurrentMatch,
    onFindInput,
    findNext,
    findPrev,
    onFindKeydown,
    onReplaceKeydown,
    replaceCurrent,
    replaceAll,
    openFindBar,
    closeFindBar,
  }
}
