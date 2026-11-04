// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import type {
  AgentTaskActiveInfo,
  AgentTaskInfo,
  AgentTaskNotification,
  AgentTaskSummaryResponse,
} from '@/types'

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

  async getTaskResult(taskId: string): Promise<{
    task_id: string | null
    logical_task_id: string
    status: string
    assistant_name: string
    assistant_id: string | null
    execution_conversation_id: string | null
    output_message_id: string | null
    result_missing: boolean
    conversation_deleted: boolean
  }> {
    const { data } = await api.get(`/agent-tasks/${taskId}/result`)
    return data
  },

  async getActiveTask(conversationId: string): Promise<AgentTaskActiveInfo | null> {
    const { data } = await api.get('/agent-tasks/active', {
      params: { conversation_id: conversationId },
    })
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

  async steerTask(
    taskId: string,
    content: string,
  ): Promise<{ ok: boolean; message_id: string; queued: number; status: string }> {
    const { data } = await api.post(`/agent-tasks/${taskId}/messages`, { content })
    return data
  },

  async summarizeTask(taskId: string, part = 0): Promise<AgentTaskSummaryResponse> {
    const { data } = await api.post(`/agent-tasks/${taskId}/summary`, { part })
    return data
  },

  async listNotifications(includeResolved = false): Promise<AgentTaskNotification[]> {
    const { data } = await api.get('/agent-tasks/notifications', {
      params: { include_resolved: includeResolved },
    })
    return data
  },

  async dismissNotification(eventId: string): Promise<void> {
    await api.post(`/agent-tasks/notifications/${eventId}/dismiss`)
  },

  async grantPermit(
    taskId: string,
    toolName: string,
    actionFingerprint: string,
    ttlSeconds = 300,
  ): Promise<{ permit_id: string; status: string }> {
    const { data } = await api.post(`/agent-tasks/${taskId}/permit`, {
      tool_name: toolName,
      action_fingerprint: actionFingerprint,
      ttl_seconds: ttlSeconds,
    })
    return data
  },

  async mergeTask(taskId: string): Promise<{ merged: boolean; commit: string }> {
    const { data } = await api.post(`/agent-tasks/${taskId}/merge`)
    return data
  },

  async deleteTask(taskId: string): Promise<{ ok: boolean; task_id: string }> {
    const { data } = await api.delete(`/agent-tasks/${taskId}`)
    return data
  },
}
