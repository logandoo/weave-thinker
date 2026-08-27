// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { ref } from 'vue'

interface ToastAction {
  label: string
  onClick: () => void
}

interface ToastItem {
  id: number
  message: string
  type: 'success' | 'error' | 'info'
  action?: ToastAction
}

const toasts = ref<ToastItem[]>([])
let nextId = 0

export function useToast() {
  function show(
    message: string,
    type: 'success' | 'error' | 'info' = 'info',
    duration = 3000,
    action?: ToastAction,
  ) {
    const id = nextId++
    toasts.value.push({ id, message, type, action })

    setTimeout(() => {
      remove(id)
    }, duration)
  }

  function triggerAction(id: number) {
    const toast = toasts.value.find(item => item.id === id)
    if (!toast?.action) {
      return
    }

    toast.action.onClick()
    remove(id)
  }

  function remove(id: number) {
    const index = toasts.value.findIndex(t => t.id === id)
    if (index > -1) {
      toasts.value.splice(index, 1)
    }
  }

  return {
    toasts,
    show,
    triggerAction,
    remove
  }
}
