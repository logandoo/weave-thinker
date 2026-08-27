// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { TOKEN_KEY } from './client'
import api from './client'

export type VoiceState =
  | 'idle'
  | 'connecting'
  | 'listen'
  | 'think'
  | 'speak'
  | 'dual'
  | 'error'

export interface VoiceEvent {
  event: string
  state?: VoiceState
  text?: string
  done?: boolean
  tts?: boolean
  level?: string
  suggestion?: string
  reason?: string
  name?: string
  error?: string
  session_id?: string
  assistant_id?: string
  emotion?: string
  raw_text?: string
  task_id?: string
  title?: string
  status?: string
}

export interface VoiceSession {
  id: string
  title: string
  updated_at?: string | null
  created_at?: string | null
}

export function getVoiceWebSocketUrl(
  token = localStorage.getItem(TOKEN_KEY) || '',
  conversationId?: string | null,
): string {
  const url = new URL('/api/voice/ws', window.location.origin)
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  if (token) {
    url.searchParams.set('token', token)
  }
  if (conversationId) {
    url.searchParams.set('conversation_id', conversationId)
  }
  return url.toString()
}

export async function getVoiceSessions(): Promise<VoiceSession[]> {
  const { data } = await api.get('/voice/sessions')
  return data
}

export async function createVoiceSession(): Promise<VoiceSession> {
  const { data } = await api.post('/voice/sessions')
  return data
}

export async function getVoiceSessionMessages(sessionId: string): Promise<{ role: string; content: string }[]> {
  const { data } = await api.get(`/voice/sessions/${sessionId}/messages`)
  return data
}

