// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'

/**
 * A draft is an unfinished query the user has temporarily parked.
 * Unlike the old "delayed send" mode, drafts survive page reloads,
 * preserve note references exactly, and can be sent to any conversation
 * when the user is ready.
 */
export interface DraftNoteRef {
  id: string
  title: string
  content: string
}

export interface Draft {
  id: string
  content: string
  references: DraftNoteRef[]
  conversationId: string | null
  assistantId: string | null
  createdAt: string
  updatedAt: string
}

const STORAGE_KEY = 'weaver_drafts_v1'

function loadFromStorage(): Draft[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(d => d && typeof d.id === 'string' && typeof d.content === 'string')
  } catch {
    return []
  }
}

function saveToStorage(drafts: Draft[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(drafts))
  } catch (e) {
    console.warn('Failed to persist drafts:', e)
  }
}

export const useDraftsStore = defineStore('drafts', () => {
  const drafts = ref<Draft[]>(loadFromStorage())

  const count = computed(() => drafts.value.length)

  /** Drafts sorted newest first. */
  const sortedDrafts = computed(() => {
    return [...drafts.value].sort((a, b) => {
      return (b.updatedAt || b.createdAt).localeCompare(a.updatedAt || a.createdAt)
    })
  })

  watch(drafts, (val) => saveToStorage(val), { deep: true })

  function addDraft(payload: {
    content: string
    references: DraftNoteRef[]
    conversationId?: string | null
    assistantId?: string | null
  }): Draft {
    const now = new Date().toISOString()
    const draft: Draft = {
      id: `draft-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      content: payload.content,
      references: payload.references.map(r => ({ ...r })),
      conversationId: payload.conversationId || null,
      assistantId: payload.assistantId || null,
      createdAt: now,
      updatedAt: now,
    }
    drafts.value = [draft, ...drafts.value]
    return draft
  }

  function updateDraft(id: string, patch: Partial<Draft>) {
    const idx = drafts.value.findIndex(d => d.id === id)
    if (idx === -1) return
    drafts.value[idx] = {
      ...drafts.value[idx],
      ...patch,
      updatedAt: new Date().toISOString(),
    }
  }

  function removeDraft(id: string) {
    drafts.value = drafts.value.filter(d => d.id !== id)
  }

  function clearAll() {
    drafts.value = []
  }

  function getDraft(id: string): Draft | undefined {
    return drafts.value.find(d => d.id === id)
  }

  return {
    drafts,
    sortedDrafts,
    count,
    addDraft,
    updateDraft,
    removeDraft,
    clearAll,
    getDraft,
  }
})
