// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import type { ExportTaskInfo } from '@/types'

export const exportTasksApi = {
  async create(params: {
    task_type: 'single' | 'bulk'
    format: 'md' | 'pdf'
    note_id?: string
    note_ids?: string[]
  }): Promise<ExportTaskInfo> {
    const { data } = await api.post<ExportTaskInfo>('/export-tasks', params)
    return data
  },

  async get(taskId: string): Promise<ExportTaskInfo> {
    const { data } = await api.get<ExportTaskInfo>(`/export-tasks/${taskId}`)
    return data
  },

  async list(status?: string, limit = 20): Promise<ExportTaskInfo[]> {
    const params: Record<string, string | number> = { limit }
    if (status) params.status = status
    const { data } = await api.get<ExportTaskInfo[]>('/export-tasks', { params })
    return data
  },

  async cancel(taskId: string): Promise<void> {
    await api.post(`/export-tasks/${taskId}/cancel`)
  },

  async delete(taskId: string): Promise<void> {
    await api.delete(`/export-tasks/${taskId}`)
  },

  getDownloadUrl(taskId: string): string {
    const baseURL = api.defaults.baseURL || '/api'
    return `${baseURL}/export-tasks/${taskId}/download`
  },
}
