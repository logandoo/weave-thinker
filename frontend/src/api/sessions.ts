// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

export interface UserSession {
  id: string
  session_token: string
  ip_address: string | null
  user_agent: string | null
  last_active_at: string | null
  expires_at: string | null
  created_at: string
}

export interface ChatSession {
  id: string
  conversation_id: string | null
  assistant_id: string | null
  started_at: string | null
  ended_at: string | null
  message_count: number
  total_tokens: number | null
  created_at: string
}

export const sessionsApi = {
  async getUserSessions(): Promise<UserSession[]> {
    const { data } = await api.get('/sessions/user-sessions')
    return data
  },

  async deleteUserSession(sessionId: string): Promise<void> {
    await api.delete(`/sessions/user-sessions/${sessionId}`)
  },

  async getChatSessions(): Promise<ChatSession[]> {
    const { data } = await api.get('/sessions/chat-sessions')
    return data
  },

  async getChatSession(sessionId: string): Promise<ChatSession> {
    const { data } = await api.get(`/sessions/chat-sessions/${sessionId}`)
    return data
  },

  async deleteChatSession(sessionId: string): Promise<void> {
    await api.delete(`/sessions/chat-sessions/${sessionId}`)
  }
}