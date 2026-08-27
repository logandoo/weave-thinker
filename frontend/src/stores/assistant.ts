// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { Assistant, AssistantFormData } from '@/types'
import { assistantApi } from '@/api/assistant'
import { useChatStore } from '@/stores/chat'

export const useAssistantStore = defineStore('assistant', () => {
  const assistants = ref<Assistant[]>([])
  const currentAssistantId = ref<string | null>(localStorage.getItem('currentAssistantId'))

  if (typeof window !== 'undefined') {
    window.addEventListener('storage', (e) => {
      if (e.key === 'currentAssistantId') {
        currentAssistantId.value = e.newValue
      }
    })
  }

  watch(currentAssistantId, (newId) => {
    if (newId) {
      localStorage.setItem('currentAssistantId', newId)
    } else {
      localStorage.removeItem('currentAssistantId')
    }
  })

  async function loadAssistants() {
    try {
      assistants.value = await assistantApi.getAssistants()
    } catch (e) {
      console.error('Failed to load assistants:', e)
    }
  }

  async function createAssistant(data: AssistantFormData): Promise<Assistant> {
    try {
      const assistant = await assistantApi.createAssistant(data)
      assistants.value.unshift(assistant)
      return assistant
    } catch (e) {
      console.error('Failed to create assistant:', e)
      throw e
    }
  }

  async function updateAssistant(id: string, data: Partial<AssistantFormData>): Promise<Assistant> {
    try {
      const updated = await assistantApi.updateAssistant(id, data)
      const index = assistants.value.findIndex(a => a.id === id)
      if (index !== -1) {
        assistants.value[index] = updated
      }
      return updated
    } catch (e) {
      console.error('Failed to update assistant:', e)
      throw e
    }
  }

  async function deleteAssistant(id: string) {
    try {
      await assistantApi.deleteAssistant(id)
      assistants.value = assistants.value.filter(a => a.id !== id)
      if (currentAssistantId.value === id) {
        currentAssistantId.value = null
      }
      // Clear stale conversation reference — the deleted assistant's
      // conversations are cascade-deleted in the DB
      const chatStore = useChatStore()
      chatStore.currentConversationId = null
    } catch (e) {
      console.error('Failed to delete assistant:', e)
      throw e
    }
  }

  function selectAssistant(id: string | null) {
    currentAssistantId.value = id
  }

  function getAssistantById(id: string): Assistant | undefined {
    return assistants.value.find(a => a.id === id)
  }

  function resetState() {
    assistants.value = []
    currentAssistantId.value = null
    localStorage.removeItem('currentAssistantId')
  }

  return {
    assistants,
    currentAssistantId,
    loadAssistants,
    createAssistant,
    updateAssistant,
    deleteAssistant,
    selectAssistant,
    getAssistantById,
    resetState
  }
})
