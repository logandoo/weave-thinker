// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 工具步骤状态推导（2026-11-01 审计 F4）。
 *
 * 实时流式：状态由后端 `tool_result.error` 给出（后端已把工具自报的
 * `timed_out: true` 并入该字段）。
 * 回放（历史消息）：持久化的 agent_steps 没有 status 字段，旧实现一律
 * `status ?? 'completed'`，于是超时步骤在重载后仍显示「✓ 完成」。
 * 这里按载荷内容兜底推断，保证同一调用「刷新前后同色」。
 */

export type ToolStepStatus = 'completed' | 'error' | 'running' | 'pending'

/**
 * 载荷是否自报超时（与后端 app/services/tool_payload_status.py 同语义）。
 *
 * 形状校验刻意收窄：`timed_out === true` 之外还要求 `return_code === -1`。
 * 否则用户用 workspace_read 读到一个内容恰为 `{"timed_out": true}` 的 JSON
 * 文件时，会被误判成失败（2026-11-01 复核 M4）。
 */
export function payloadIsTimeout(raw: unknown): boolean {
  const isTimeoutShape = (o: unknown): boolean => {
    if (!o || typeof o !== 'object') return false
    const obj = o as Record<string, unknown>
    return obj.timed_out === true && obj.return_code === -1
  }
  if (raw && typeof raw === 'object') return isTimeoutShape(raw)
  if (typeof raw !== 'string') return false
  if (!raw.includes('"timed_out"')) return false
  const text = raw.trim()
  if (!text.startsWith('{')) return false
  try {
    return isTimeoutShape(JSON.parse(text))
  } catch {
    return false
  }
}

/** 持久化/流式共用的状态推导：显式 status 优先，其次看载荷。 */
export function deriveToolStatus(item: {
  status?: string
  result?: unknown
  content?: unknown
} | null | undefined): ToolStepStatus {
  const explicit = item?.status
  if (explicit === 'running' || explicit === 'pending' || explicit === 'error' || explicit === 'completed') {
    return explicit
  }
  // result 可能是空串（部分代码路径），此时回落到 content
  const payload = item?.result || item?.content
  return payloadIsTimeout(payload) ? 'error' : 'completed'
}
