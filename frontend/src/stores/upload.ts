// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { FileParseResult } from '@/api/fileUpload'

/**
 * Upload/attachment state lives in Pinia instead of ChatInput so chips
 * survive ChatInput remounts (conversation switches, layout changes) and
 * parallel upload flows can share one cancel handle.
 */
export const useUploadStore = defineStore('upload', () => {
  const files = ref<FileParseResult[]>([])
  const uploading = ref(false)
  const error = ref<string | null>(null)
  const abortController = ref<AbortController | null>(null)

  function setFiles(next: FileParseResult[]) {
    files.value = [...next]
  }

  function addFiles(next: FileParseResult[]) {
    files.value = [...files.value, ...next]
  }

  function removeFile(index: number) {
    if (index < 0 || index >= files.value.length) return
    files.value.splice(index, 1)
  }

  /** Drop the pending attachment list (e.g. after a send). Does not abort an
   *  in-flight upload — cancelUpload() owns abort semantics. */
  function clear() {
    files.value = []
    error.value = null
  }

  /** Begin an upload: store a fresh controller so cancelUpload() can abort it. */
  function startUpload(): AbortController {
    const controller = new AbortController()
    abortController.value = controller
    uploading.value = true
    error.value = null
    return controller
  }

  /** User-initiated cancel: abort the in-flight request only. Files that were
   *  already uploaded stay in the list. */
  function cancelUpload() {
    abortController.value?.abort()
    abortController.value = null
    uploading.value = false
  }

  function finishUpload() {
    uploading.value = false
    abortController.value = null
  }

  return {
    files,
    uploading,
    error,
    abortController,
    setFiles,
    addFiles,
    removeFile,
    clear,
    startUpload,
    cancelUpload,
    finishUpload,
  }
})
