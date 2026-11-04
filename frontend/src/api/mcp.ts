// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

/** 2026-11-01 user-level MCP servers (no restart; owner-scoped). */
export interface McpRuntimeStatus {
  registered: boolean
  tools_count: number
  tools: string[]
  error?: string | null
}

export interface McpServer {
  id: string
  name: string
  transport: 'http' | 'stdio'
  url: string
  api_key: string
  has_api_key: boolean
  command: string
  args: string[]
  env: Record<string, string>
  enabled: boolean
  created_at?: string
  updated_at?: string
  runtime?: McpRuntimeStatus
  ok?: boolean
  error?: string | null
}

export interface McpServerPayload {
  name: string
  transport: 'http' | 'stdio'
  url?: string
  api_key?: string
  command?: string
  args?: string[]
  env?: Record<string, string>
  enabled?: boolean
}

export const mcpApi = {
  async list(): Promise<{ servers: McpServer[] }> {
    const { data } = await api.get('/mcp/servers')
    return data
  },
  async create(payload: McpServerPayload): Promise<McpServer> {
    const { data } = await api.post('/mcp/servers', payload)
    return data
  },
  async update(id: string, payload: Partial<McpServerPayload>): Promise<McpServer> {
    const { data } = await api.patch(`/mcp/servers/${id}`, payload)
    return data
  },
  async remove(id: string): Promise<{ ok: boolean }> {
    const { data } = await api.delete(`/mcp/servers/${id}`)
    return data
  },
  async reloadServer(id: string): Promise<Record<string, unknown>> {
    const { data } = await api.post(`/mcp/servers/${id}/reload`)
    return data
  },
}
