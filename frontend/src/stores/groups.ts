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
  // 全量分组（跨助手）：仅供「移动到分组」菜单使用，不参与主列表渲染，
  // 主列表始终用按当前助手过滤后的 `groups`。
  const allGroups = ref<ConversationGroup[]>([])
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

  function getAllGroupsForAssistant(assistantId: string | null): ConversationGroup[] {
    const key = assistantId || 'null'
    return allGroups.value.filter(g => (g.assistant_id || 'null') === key)
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

  // 加载全部助手的分组（无 assistant_id 过滤），供「移动到分组」菜单跨助手选择。
  // 与 loadGroups 同样的 stale-response 守卫；菜单打开时调用即可保证新鲜。
  let allGroupsLoadSeq = 0
  async function loadAllGroups() {
    const seq = ++allGroupsLoadSeq
    try {
      const res = await groupApi.getGroups()
      if (seq !== allGroupsLoadSeq) return // superseded by a newer load
      allGroups.value = res.data || []
    } catch (e) {
      if (seq !== allGroupsLoadSeq) return
      console.error('Failed to load all groups:', e)
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
    allGroups,
    collapsedGroups,
    loading,
    groupsByAssistant,
    getGroupsForAssistant,
    getAllGroupsForAssistant,
    isGroupCollapsed,
    loadGroups,
    loadAllGroups,
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
