// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

export type MobileRowActionKind = 'note' | 'notebook'

export const useMobileUiStore = defineStore('mobile-ui', () => {
  const activeRowActionKind = ref<MobileRowActionKind | null>(null)
  const activeRowActionId = ref<string | null>(null)
  const rowActionDragging = ref(false)

  const hasOpenRowAction = computed(() => activeRowActionId.value !== null)
  const pageSwipeLocked = computed(() => rowActionDragging.value || hasOpenRowAction.value)

  function openRowAction(kind: MobileRowActionKind, id: string) {
    activeRowActionKind.value = kind
    activeRowActionId.value = id
    rowActionDragging.value = false
  }

  function startRowActionDrag(kind: MobileRowActionKind, id: string) {
    activeRowActionKind.value = kind
    activeRowActionId.value = id
    rowActionDragging.value = true
  }

  function stopRowActionDrag(kind?: MobileRowActionKind) {
    if (kind && activeRowActionKind.value !== kind) return
    rowActionDragging.value = false
  }

  function closeRowAction(kind?: MobileRowActionKind) {
    if (kind && activeRowActionKind.value !== kind) return
    activeRowActionKind.value = null
    activeRowActionId.value = null
    rowActionDragging.value = false
  }

  function isRowActionActive(kind: MobileRowActionKind, id: string) {
    return activeRowActionKind.value === kind && activeRowActionId.value === id
  }

  return {
    activeRowActionKind,
    activeRowActionId,
    rowActionDragging,
    hasOpenRowAction,
    pageSwipeLocked,
    openRowAction,
    startRowActionDrag,
    stopRowActionDrag,
    closeRowAction,
    isRowActionActive,
  }
})