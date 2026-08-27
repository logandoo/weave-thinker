// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { groupApi } from '@/api/groups'
import type { ConversationGroup, Conversation } from '@/types'

const COLLAPSED_GROUPS_KEY = 'chatllm_collapsed_groups'

function loadCollapsedGroups(): Set<string> {
  try {
    const stored = localStorage.getItem(COLLAPSED_GROUPS_KEY)
    if (stored) {
      return new Set(JSON.parse(stored))
    }
  } catch {
    // ignore
  }
  return new Set()
}

function saveCollapsedGroups(groups: Set<string>) {
  localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify(Array.from(groups)))
}

export const useGroupStore = defineStore('groups', () => {
  // State
  const groups = ref<ConversationGroup[]>([])
  const collapsedGroups = ref<Set<string>>(loadCollapsedGroups())
  const loading = ref(false)

  // Getters
  const groupsByAssistant = computed(() => {
    const map: Record<string, ConversationGroup[]> = {}
    for (const g of groups.value) {
      const key = g.assistant_id || 'null'
      if (!map[key]) map[key] = []
      map[key].push(g)
    }
    return map
  })

  function getGroupsForAssistant(assistantId: string | null): ConversationGroup[] {
    const key = assistantId || 'null'
    return groupsByAssistant.value[key] || []
  }

  function isGroupCollapsed(groupId: string): boolean {
    return collapsedGroups.value.has(groupId)
  }

  // Actions
  // Stale-response guard (same race as chatStore.loadConversations): only the
  // latest requested load may apply its result.
  let groupsLoadSeq = 0
  async function loadGroups(assistantId?: string) {
    const seq = ++groupsLoadSeq
    loading.value = true
    try {
      const res = await groupApi.getGroups(assistantId)
      if (seq !== groupsLoadSeq) return // superseded by a newer load
      groups.value = res.data || []
    } catch (e) {
      if (seq !== groupsLoadSeq) return
      console.error('Failed to load groups:', e)
    } finally {
      if (seq === groupsLoadSeq) loading.value = false
    }
  }

  async function createGroup(name: string, color: string = '#3b82f6', assistantId?: string | null) {
    const res = await groupApi.createGroup({
      name,
      color,
      assistant_id: assistantId
    })
    groups.value.push(res.data)
    return res.data
  }

  async function updateGroup(id: string, updates: Partial<{ name: string; color: string }>) {
    const res = await groupApi.updateGroup(id, updates)
    const idx = groups.value.findIndex(g => g.id === id)
    if (idx >= 0) {
      groups.value[idx] = res.data
    }
    return res.data
  }

  async function deleteGroup(id: string, deleteConversations: boolean = false) {
    await groupApi.deleteGroup(id, deleteConversations)
    groups.value = groups.value.filter(g => g.id !== id)
  }

  async function bulkDeleteGroups(ids: string[], deleteConversations: boolean = false) {
    await groupApi.bulkDeleteGroups(ids, deleteConversations)
    const idSet = new Set(ids)
    groups.value = groups.value.filter(g => !idSet.has(g.id))
  }

  async function moveConversation(conversationId: string, groupId: string | null, assistantId?: string | null) {
    const res = await groupApi.moveConversation(conversationId, groupId, assistantId)
    return res.data
  }

  async function moveGroupToAssistant(groupId: string, assistantId: string) {
    const res = await groupApi.moveGroup(groupId, assistantId)
    const idx = groups.value.findIndex(g => g.id === groupId)
    if (idx >= 0) groups.value[idx] = res.data
    return res.data
  }

  async function reorderGroups(items: { id: string; sort_order: number }[]) {
    await groupApi.reorderGroups(items)
    // Update local state
    for (const item of items) {
      const g = groups.value.find(g => g.id === item.id)
      if (g) g.sort_order = item.sort_order
    }
    groups.value.sort((a, b) => a.sort_order - b.sort_order)
  }

  async function reorderConversations(items: { id: string; sort_order: number; group_id?: string | null }[]) {
    await groupApi.reorderConversations(items)
  }

  function toggleGroupCollapse(groupId: string) {
    if (collapsedGroups.value.has(groupId)) {
      collapsedGroups.value.delete(groupId)
    } else {
      collapsedGroups.value.add(groupId)
    }
    saveCollapsedGroups(collapsedGroups.value)
  }

  function expandGroup(groupId: string) {
    collapsedGroups.value.delete(groupId)
    saveCollapsedGroups(collapsedGroups.value)
  }

  function collapseGroup(groupId: string) {
    collapsedGroups.value.add(groupId)
    saveCollapsedGroups(collapsedGroups.value)
  }

  return {
    groups,
    collapsedGroups,
    loading,
    groupsByAssistant,
    getGroupsForAssistant,
    isGroupCollapsed,
    loadGroups,
    createGroup,
    updateGroup,
    deleteGroup,
    bulkDeleteGroups,
    moveConversation,
    moveGroupToAssistant,
    reorderGroups,
    reorderConversations,
    toggleGroupCollapse,
    expandGroup,
    collapseGroup
  }
})
