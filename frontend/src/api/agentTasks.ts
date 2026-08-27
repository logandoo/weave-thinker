// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import type { AgentTaskInfo } from '@/types'

export const agentTaskApi = {
  async listTasks(status?: string, includeCompleted?: boolean): Promise<AgentTaskInfo[]> {
    const params: Record<string, string | boolean> = {}
    if (status) params.status = status
    if (includeCompleted) params.include_completed = true
    const { data } = await api.get('/agent-tasks', { params })
    return data
  },

  async getTask(taskId: string): Promise<AgentTaskInfo> {
    const { data } = await api.get(`/agent-tasks/${taskId}`)
    return data
  },

  async createTask(goal: string, title?: string, assistantId?: string): Promise<AgentTaskInfo> {
    const { data } = await api.post('/agent-tasks', {
      goal,
      title: title || undefined,
      assistant_id: assistantId || undefined,
    })
    return data
  },

  async cancelTask(taskId: string): Promise<{ ok: boolean; task_id: string; status: string }> {
    const { data } = await api.post(`/agent-tasks/${taskId}/cancel`)
    return data
  },

  async deleteTask(taskId: string): Promise<{ ok: boolean; task_id: string }> {
    const { data } = await api.delete(`/agent-tasks/${taskId}`)
    return data
  },
}
