// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { ref } from 'vue'

export interface ConfirmDialogOptions {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
  /** Enable three-button mode: returns 'save' | 'discard' | 'cancel' */
  threeButton?: {
    saveText: string
    discardText: string
    cancelText: string
  }
}

const visible = ref(false)
const options = ref<ConfirmDialogOptions>({ message: '' })
let resolvePromise: ((value: boolean) => void) | null = null
let resolveThreeButton: ((value: 'save' | 'discard' | 'cancel') => void) | null = null

export function useConfirmDialog() {
  function confirm(opts: ConfirmDialogOptions): Promise<boolean> {
    options.value = opts
    visible.value = true
    return new Promise<boolean>((resolve) => {
      resolvePromise = resolve
      resolveThreeButton = null
    })
  }

  function confirmThreeWay(opts: ConfirmDialogOptions): Promise<'save' | 'discard' | 'cancel'> {
    options.value = opts
    visible.value = true
    return new Promise<'save' | 'discard' | 'cancel'>((resolve) => {
      resolveThreeButton = resolve
      resolvePromise = null
    })
  }

  function handleConfirm() {
    visible.value = false
    if (resolveThreeButton) {
      resolveThreeButton('save')
      resolveThreeButton = null
    } else {
      resolvePromise?.(true)
      resolvePromise = null
    }
  }

  function handleDiscard() {
    visible.value = false
    if (resolveThreeButton) {
      resolveThreeButton('discard')
      resolveThreeButton = null
    }
  }

  function handleCancel() {
    visible.value = false
    if (resolveThreeButton) {
      resolveThreeButton('cancel')
      resolveThreeButton = null
    } else {
      resolvePromise?.(false)
      resolvePromise = null
    }
  }

  return {
    visible,
    options,
    confirm,
    confirmThreeWay,
    handleConfirm,
    handleDiscard,
    handleCancel,
  }
}
