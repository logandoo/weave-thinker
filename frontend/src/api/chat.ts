// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import { downloadBlob, type DownloadResult } from '@/composables/useDownload'
import { clearStoredAuth } from '@/composables/useAuth'
import type { Conversation, Message, ChatRequest, ConversationUpdate, BulkDeleteResult, ConversationSearchResult } from '@/types'
import { dispatchStreamPayload, type StreamHandlers, type ResumeHandlers, type StreamStatusResult, type ReplayPayload } from './streamDispatch'

export { dispatchStreamPayload } from './streamDispatch'
export type { StreamHandlers, ResumeHandlers, StreamStatusResult, ReplayPayload } from './streamDispatch'

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('chatllm_token')
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'Accept': 'text/event-stream',
    'Cache-Control': 'no-cache'
  }
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

async function readSseStream(
  response: Response,
  h: StreamHandlers,
  processDataLine: (line: string) => boolean,
): Promise<void> {
  if (!response.body) {
    h.onError('HTTP response body is empty')
    return
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (value) {
      buffer += decoder.decode(value, { stream: !done })
    }
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (processDataLine(line)) return
    }

    if (done) {
      const trailing = buffer.trim()
      if (trailing && processDataLine(trailing)) return
      break
    }
  }
}

export const chatApi = {
  async createConversation(title?: string, assistantId?: string | null): Promise<Conversation> {
    const { data } = await api.post('/conversations', { title, assistant_id: assistantId })
    return data
  },

  async getConversations(assistantId?: string | null): Promise<Conversation[]> {
    const params = assistantId ? { assistant_id: assistantId } : {}
    const { data } = await api.get('/conversations', { params })
    return data
  },

  async getConversation(id: string): Promise<Conversation> {
    const { data } = await api.get(`/conversations/${id}`)
    return data
  },

  async updateConversation(id: string, data: ConversationUpdate): Promise<Conversation> {
    const { data: result } = await api.put(`/conversations/${id}`, data)
    return result
  },

  async deleteConversation(id: string): Promise<void> {
    await api.delete(`/conversations/${id}`)
  },

  async bulkDeleteConversations(conversationIds: string[]): Promise<BulkDeleteResult> {
    const { data } = await api.post<BulkDeleteResult>('/conversations/bulk-delete', {
      conversation_ids: conversationIds,
    })
    return data
  },

  async getMessages(conversationId: string): Promise<Message[]> {
    const { data } = await api.get(`/conversations/${conversationId}/messages`)
    return data
  },

  async searchConversations(query: string): Promise<ConversationSearchResult[]> {
    const { data } = await api.get('/conversations/search', { params: { q: query } })
    return data
  },

  async exportConversations(assistantId: string, conversationIds: string[]): Promise<DownloadResult> {
    const response = await api.post('/conversations/export', {
      assistant_id: assistantId,
      conversation_ids: conversationIds
    }, { responseType: 'blob' })
    const blob = new Blob([response.data], { type: 'application/zip' })
    const contentDisposition = response.headers['content-disposition']
    let filename = 'export.zip'
    if (contentDisposition) {
      const match = contentDisposition.match(/filename="?([^"]+)"?/)
      if (match) filename = match[1]
    }
    return await downloadBlob(blob, filename, 'application/zip')
  },

  async getStreamStatus(conversationId: string): Promise<StreamStatusResult> {
    try {
      const { data } = await api.get(`/chat/stream/status/${conversationId}`)
      return data
    } catch {
      return { has_buffer: false, status: 'none', is_running: false, db_message_id: null, content_length: 0, error: true }
    }
  },

  async stopStream(conversationId: string): Promise<void> {
    try {
      await api.post(`/chat/stream/stop/${conversationId}`)
    } catch {
      // ignore — best-effort explicit cancel
    }
  },

  async streamChat(request: ChatRequest, handlers: StreamHandlers): Promise<void> {
    const baseUrl = (import.meta as any).env?.VITE_API_BASE || ''
    const response = await fetch(`${baseUrl}/api/chat/stream`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify(request),
      signal: handlers.signal,
    })

    if (!response.ok) {
      if (response.status === 401) {
        clearStoredAuth()
        window.location.href = '/app/frontend/login?expired=1'
        return
      }
      handlers.onError(`HTTP error: ${response.status}`)
      return
    }

    const processDataLine = (line: string): boolean => {
      if (!line.startsWith('data: ')) {
        return false
      }

      const jsonStr = line.slice(6)
      if (jsonStr === '[DONE]') {
        return true
      }

      try {
        const data = JSON.parse(jsonStr)
        return dispatchStreamPayload(data, handlers)
      } catch {
        // Skip invalid JSON
      }

      return false
    }

    await readSseStream(response, handlers, processDataLine)
  },

  async resumeStream(conversationId: string, handlers: ResumeHandlers): Promise<void> {
    const baseUrl = (import.meta as any).env?.VITE_API_BASE || ''
    const response = await fetch(`${baseUrl}/api/chat/stream/resume`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ conversation_id: conversationId }),
      signal: handlers.signal,
    })

    if (!response.ok) {
      if (response.status === 404) {
        return
      }
      if (response.status === 401) {
        clearStoredAuth()
        window.location.href = '/app/frontend/login?expired=1'
        return
      }
      handlers.onError(`Resume failed: HTTP ${response.status}`)
      return
    }

    const processLine = (line: string): boolean => {
      if (!line.startsWith('data: ')) {
        return false
      }

      const jsonStr = line.slice(6)
      try {
        const data = JSON.parse(jsonStr)

        if (data.replay) {
          handlers.onReplay(data.replay)
          return false
        }

        if (data.db_message_id_update && handlers.onReplay) {
          handlers.onReplay({
            content: '',
            reasoning: '',
            layer1: { content: '', reasoning: '' },
            layer2: { content: '', reasoning: '' },
            tool_calls: [],
            tool_results: [],
            agent_steps: [],
            content_segments: [],
            display_sequence: [],
            file_attachments: [],
            search_progress: [],
            search_failed: null,
            iteration: null,
            status: 'complete',
            is_running: false,
            db_message_id: data.db_message_id_update.db_message_id || null,
          })
          return false
        }

        return dispatchStreamPayload(data, handlers)
      } catch {
        // Skip invalid JSON
      }
      return false
    }

    await readSseStream(response, handlers, processLine)
  },
}
