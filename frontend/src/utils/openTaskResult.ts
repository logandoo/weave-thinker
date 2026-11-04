// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import router from '@/router'
import { agentTaskApi } from '@/api/agentTasks'
import { useAssistantStore } from '@/stores/assistant'
import { useChatStore } from '@/stores/chat'
import { resolveTaskConversationId } from '@/utils/taskResult'
import type { AgentTaskInfo } from '@/types'

/**
 * Unified "open task result" action (§11.4).
 *
 * A mounted page cannot be trusted to switch by `router.push(?conv=...)`
 * alone: the target conversation belongs to the task's assistant and may not
 * be in the current sidebar list. This helper switches the assistant, loads
 * that assistant's conversations, selects C2 through the store, then syncs
 * the URL (the ChatLayout watcher keeps them consistent).
 *
 * After the 7-day AgentTask cleanup `GET /{id}` is 404 but the same-user
 * outbox still points at C2; the `/result` link is the fallback.
 */
export async function openTaskResult(taskOrId: AgentTaskInfo | string): Promise<boolean> {
  try {
    let conversationId: string | null = null
    let assistantId: string | null = null
    if (typeof taskOrId === 'string') {
      try {
        const task = await agentTaskApi.getTask(taskOrId)
        conversationId = resolveTaskConversationId(task)
        assistantId = task.assistant_id || null
      } catch {
        const link = await agentTaskApi.getTaskResult(taskOrId)
        if (link.conversation_deleted || !link.execution_conversation_id) return false
        conversationId = link.execution_conversation_id
        assistantId = link.assistant_id
      }
    } else {
      conversationId = resolveTaskConversationId(taskOrId)
      assistantId = taskOrId.assistant_id || null
    }
    if (!conversationId) return false

    const assistantStore = useAssistantStore()
    const chatStore = useChatStore()
    if (assistantId && assistantStore.currentAssistantId !== assistantId) {
      assistantStore.selectAssistant(assistantId)
      await chatStore.loadConversations(assistantId)
    }
    await chatStore.selectConversation(conversationId)
    const refreshed = await chatStore.refreshConversation(conversationId, { fullWindow: true })
    if (!refreshed) return false
    await router.push({ path: '/', query: { conv: conversationId } })
    return true
  } catch (error) {
    console.error('openTaskResult failed:', error)
    return false
  }
}
