// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import type { Assistant, AssistantFormData, Conversation } from '@/types'

export const assistantApi = {
  async getAssistants(): Promise<Assistant[]> {
    const { data } = await api.get('/assistants')
    return data
  },

  async createAssistant(assistant: AssistantFormData): Promise<Assistant> {
    const { data } = await api.post('/assistants', assistant)
    return data
  },

  async getAssistant(id: string): Promise<Assistant> {
    const { data } = await api.get(`/assistants/${id}`)
    return data
  },

  async updateAssistant(id: string, assistant: Partial<AssistantFormData>): Promise<Assistant> {
    const { data } = await api.put(`/assistants/${id}`, assistant)
    return data
  },

  async deleteAssistant(id: string): Promise<void> {
    await api.delete(`/assistants/${id}`)
  },

  async getAssistantConversations(id: string): Promise<Conversation[]> {
    const { data } = await api.get(`/assistants/${id}/conversations`)
    return data
  }
}
