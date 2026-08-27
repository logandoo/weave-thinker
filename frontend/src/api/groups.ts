// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import client from './client'

export interface ConversationGroupCreate {
  name: string
  color?: string
  assistant_id?: string | null
}

export interface ConversationGroupUpdate {
  name?: string
  color?: string
}

export interface ConversationMoveRequest {
  group_id?: string | null
  assistant_id?: string | null
}

export interface ConversationGroupMoveRequest {
  assistant_id: string
}

export interface ConversationReorderItem {
  id: string
  sort_order: number
  group_id?: string | null
}

export interface ConversationGroupReorderItem {
  id: string
  sort_order: number
}

export const groupApi = {
  // Groups
  getGroups(assistantId?: string) {
    const params = assistantId ? `?assistant_id=${assistantId}` : ''
    return client.get(`/conversations/groups${params}`)
  },

  createGroup(data: ConversationGroupCreate) {
    return client.post('/conversations/groups', data)
  },

  updateGroup(id: string, data: ConversationGroupUpdate) {
    return client.put(`/conversations/groups/${id}`, data)
  },

  deleteGroup(id: string, deleteConversations: boolean = false) {
    return client.delete(`/conversations/groups/${id}?delete_conversations=${deleteConversations}`)
  },

  bulkDeleteGroups(groupIds: string[], deleteConversations: boolean = false) {
    return client.post('/conversations/groups/bulk-delete', {
      group_ids: groupIds,
      delete_conversations: deleteConversations
    })
  },

  // Move conversation to/from group (optionally across assistants)
  moveConversation(conversationId: string, groupId: string | null, assistantId?: string | null) {
    const body: ConversationMoveRequest = { group_id: groupId }
    if (assistantId !== undefined) body.assistant_id = assistantId
    return client.put(`/conversations/${conversationId}/move`, body)
  },

  // Move a whole group (with its conversations) to another assistant
  moveGroup(groupId: string, assistantId: string) {
    return client.put(`/conversations/groups/${groupId}/move`, { assistant_id: assistantId })
  },

  // Reorder conversations
  reorderConversations(items: ConversationReorderItem[]) {
    return client.put('/conversations/reorder', { items })
  },

  // Reorder groups
  reorderGroups(items: ConversationGroupReorderItem[]) {
    return client.put('/conversations/groups/reorder', { items })
  }
}
