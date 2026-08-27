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
  size?: number
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
  ): Promise<FileUploadResponse> {
    const formData = new FormData()
    for (const file of files) {
      formData.append('files', file)
    }

    const params: Record<string, string | boolean> = { save_to_notebook: saveToNotebook }
    if (notebookId) {
      params.notebook_id = notebookId
    }

    const { data } = await api.post<FileUploadResponse>('/files/upload', formData, {
      params,
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (e) => {
        if (onProgress && e.total) {
          onProgress(Math.round((e.loaded / e.total) * 100))
        }
      },
    })
    return data
  },
}
