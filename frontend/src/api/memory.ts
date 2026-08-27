// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

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
  async getConcepts(limit = 100): Promise<MemoryConcept[]> {
    const { data } = await api.get<{ concepts: MemoryConcept[] }>('/memory/concepts', {
      params: { limit }
    })
    return data.concepts || []
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

  async deleteConcept(conceptId: string): Promise<void> {
    await api.delete(`/memory/concepts/${conceptId}`)
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
  }
}
