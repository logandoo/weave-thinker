// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import type { AgentTaskInfo } from '@/types'

/**
 * Pure task-result routing helpers (unit-tested without Pinia/Vue).
 */
export function resolveTaskConversationId(
  task: Pick<AgentTaskInfo, 'execution_conversation_id' | 'output_conversation_id'>,
): string | null {
  return task.execution_conversation_id || task.output_conversation_id || null
}

export function describeTaskState(status: string): string {
  const map: Record<string, string> = {
    pending: '已接受，等待执行',
    claimed: '已领取，即将开始',
    running: '执行中',
    resumable: '等待恢复执行',
    waiting_input: '等待你补充信息',
    waiting_auth: '等待你授权',
    waiting_budget: '等待你确认预算',
    cancelling: '正在请求取消',
    completed: '执行结束（不等于目标已验证达成）',
    failed: '执行失败',
    cancelled: '已取消',
    timed_out: '已超时，可能只有部分结果',
  }
  return map[status] || status
}
