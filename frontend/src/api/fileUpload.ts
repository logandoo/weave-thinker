// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

export interface FileParseResult {
  success: boolean
  markdown?: string | null
  error?: string | null
  file_type?: string | null
  filename: string
  file_path?: string | null
  /** 用户根相对路径（`/api/files/*` 解析口径，§3 不变量 9）。 */
  rel_path?: string | null
  /** Task 内展示路径（agent 解析口径，§3 不变量 9：展示与解析分离）。 */
  display_path?: string | null
  /** 用户根相对下载路径（`[file-ref]` 的 `下载路径:` 行，2026-11-15 波 · C）。 */
  download_path?: string | null
  size?: number
  /** True for chips restored from a message's [file-ref] blocks by edit-mode
   *  (not a fresh upload) — scoped to the edit session: a normal send drops
   *  them (utils/fileRefs dropRestoredChips), a new edit session replaces
   *  them (restoreEditChips). The upload API never sets this. */
  restored?: boolean
}

export interface FileUploadResponse {
  results: FileParseResult[]
  notebook_id?: string | null
  notebook_name?: string | null
}

export const fileUploadApi = {
  async uploadFiles(
    files: File[],
    saveToNotebook: boolean,
    onProgress?: (percent: number) => void,
    notebookId?: string,
    signal?: AbortSignal,
    scope?: { conversationId: string },
  ): Promise<FileUploadResponse> {
    const formData = new FormData()
    for (const file of files) {
      formData.append('files', file)
    }
    // 任务作用域（§10）：带会话上下文时后端按绑定/活跃 run 决定落点
    // （Task inputs 或 staging）；run/attempt/epoch 由服务端推导，客户端不背书。
    if (scope?.conversationId) {
      formData.append('conversation_id', scope.conversationId)
    }

    const params: Record<string, string | boolean> = { save_to_notebook: saveToNotebook }
    if (notebookId) {
      params.notebook_id = notebookId
    }

    const { data } = await api.post<FileUploadResponse>('/files/upload', formData, {
      params,
      headers: { 'Content-Type': 'multipart/form-data' },
      signal,
      onUploadProgress: (e) => {
        if (onProgress && e.total) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      },
    })
    return data
  },
}
