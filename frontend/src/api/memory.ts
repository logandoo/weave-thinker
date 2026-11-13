// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

export interface MemoryEvidence {
  source_ids: string[]
  unit_ids: string[]
  source_kind?: string | null
  quote: string
  reason: string
  checked_at?: string | null
  manual?: boolean | null
}

export interface MemoryConcept {
  id: string
  canonical_name: string
  description_short: string
  description_full: string | null
  weight: number
  importance: number
  source_trust: string
  memory_type: string
  activation_strength: number
  status: string
  valid_from: string | null
  valid_to: string | null
  created_at: string | null
  verification_state?: string | null
  last_verified_at?: string | null
  invalid_reason?: string | null
  superseded_by?: string | null
  superseded_by_name?: string | null
  needs_review?: boolean
  aliases?: string[] | null
  recurrence_count?: number | null
  source_type?: string | null
  source_kind?: string | null
  source_message_ids?: string[] | null
  evidence?: MemoryEvidence | null
  verification_correction?: string | null
  is_history?: boolean
}

export interface MemoryHealth {
  total: number
  active: number
  merged: number
  retracted: number
  source_lost: number
  forgotten: number
  expired: number
  needs_review: number
  verified: number
  contradicted: number
  insufficient: number
  verify_coverage: number
  dream_injectable_rate: number | null
  last_verify: Record<string, any> | null
  recall_7d_avg_injected_chars: number | null
}

export interface MemoryRecallLogItem {
  id: string
  conversation_id: string | null
  query_hash: string | null
  candidate_ids: string[] | null
  tier_scores: Record<string, [string, number][]> | null
  gate_score: number
  budget_chars: number
  injected_chars: number
  truncated: boolean
  elapsed_ms: number
  cache_hit: boolean
  created_at: string | null
}

export interface MemoryDream {
  id: string
  generated_for_date: string
  summary: string
  source_concept_count: number
  source_cluster_count: number
  dream_type: string
  created_at: string | null
}

export interface MemoryClarification {
  id: string
  original_text: string
  correction_type: string
  affected_concept_ids: string | null
  new_description: string | null
  confidence: number
  applied: boolean
  applied_at: string | null
  created_at: string | null
}

export interface CostGovernanceStatus {
  level: number
  disabled_steps: string[]
  reason: string
  last_change?: string
  today_calls: number
  daily_avg_7d: number
  warn_multiplier: number
}

export const memoryApi = {
  async getConcepts(limit = 100, opts: {
    includeHistory?: boolean
    verificationState?: string
    q?: string
    offset?: number
    historyOffset?: number
  } = {}): Promise<{ items: MemoryConcept[]; total: number; activeTotal: number; historyTotal: number }> {
    const params: Record<string, any> = { limit }
    if (opts.includeHistory) params.include_history = true
    if (opts.verificationState && opts.verificationState !== 'any') params.verification_state = opts.verificationState
    if (opts.q) params.q = opts.q
    if (opts.offset) params.offset = opts.offset
    if (opts.historyOffset) params.history_offset = opts.historyOffset
    const { data } = await api.get<{
      concepts: MemoryConcept[]; total?: number;
      active_total?: number; history_total?: number
    }>('/memory/concepts', { params })
    const items = data.concepts || []
    return {
      items,
      total: Number(data.total ?? items.length),
      // 后端未升级时退化：active_total 缺省按合并列表长度与 total 取小
      activeTotal: Number(data.active_total ?? Math.min(items.length, Number(data.total ?? 0))),
      historyTotal: Number(data.history_total ?? 0),
    }
  },

  async verifyConcept(conceptId: string, action: 'confirm' | 'reject' | 'retry' | 'apply_correction', note = '') {
    const { data } = await api.post(`/memory/concepts/${conceptId}/verify`, { action, note })
    return data
  },

  async getHealth(): Promise<MemoryHealth> {
    const { data } = await api.get<MemoryHealth>('/memory/health')
    return data
  },

  async getDreams(limit = 10): Promise<MemoryDream[]> {
    const { data } = await api.get<{ dreams: MemoryDream[] }>('/memory/dreams', {
      params: { limit }
    })
    return data.dreams || []
  },

  async getClarifications(limit = 50): Promise<MemoryClarification[]> {
    const { data } = await api.get<{ clarifications: MemoryClarification[] }>('/memory/clarifications', {
      params: { limit }
    })
    return data.clarifications || []
  },

  async getRecallLog(limit = 50, beforeId?: string): Promise<{ items: MemoryRecallLogItem[]; total: number }> {
    const { data } = await api.get<{ items: MemoryRecallLogItem[]; total: number }>('/memory/recall_log', {
      params: beforeId ? { limit, before_id: beforeId } : { limit }
    })
    return { items: data.items || [], total: data.total || 0 }
  },

  async deleteConcept(conceptId: string): Promise<void> {
    await api.delete(`/memory/concepts/${conceptId}`)
  },

  async forgetConcept(conceptId: string): Promise<void> {
    await api.post(`/memory/concepts/${conceptId}/forget`)
  },

  async revertClarification(clarificationId: string): Promise<void> {
    await api.post(`/memory/clarifications/${clarificationId}/revert`)
  },

  async deleteAll(): Promise<void> {
    await api.delete('/memory/all')
  },

  async getCostGovernanceStatus(): Promise<CostGovernanceStatus> {
    const { data } = await api.get<CostGovernanceStatus>('/memory/cost_governance/status')
    return data
  },

  // W1-2 约束固定（可见可撤销）
  async getConstraints(conversationId?: string): Promise<any[]> {
    const { data } = await api.get<{ items: any[] }>('/memory/constraints', {
      params: conversationId ? { conversation_id: conversationId } : {}
    })
    return data.items || []
  },

  async revokeConstraint(pinId: string): Promise<void> {
    await api.post(`/memory/constraints/${pinId}/revoke`)
  },

  // W5-3 保留/淘汰预览（shadow 只记录；可撤销）
  async getRetentionPreview(capacity?: number): Promise<any> {
    const { data } = await api.get<any>('/memory/retention_preview', {
      params: capacity ? { capacity } : {}
    })
    return data
  },

  async undoRetention(conceptIds: string[]): Promise<void> {
    await api.post('/memory/retention/undo', { concept_ids: conceptIds })
  }
}
