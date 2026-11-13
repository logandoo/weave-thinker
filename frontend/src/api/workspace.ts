// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 任务 Workspace 作用域 API（2026-11-10 波 · T14）。
 *
 * 后端契约：`backend/app/api/workspace.py`（能力门 / 项目 / 任务 / 会话绑定 CAS /
 * 跨域 grant）。约定：
 * - `mode=off` 时 UI 完全不出现任务徽标（现网零变化）；
 * - 改绑必须携带 `expected_binding_version`（CAS），版本漂移返回 409
 *   `workspace_binding_stale`，由调用方刷新快照后重试。
 */
import api from './client'

export type ScopingMode = 'off' | 'shadow' | 'enforce'

export interface WorkspaceCapabilities {
  mode: ScopingMode
  supported_modes: string[]
  all_modes: string[]
  enforcement: Record<string, string>
}

export interface WorkspaceProject {
  id: string
  name: string
  root_rel: string
  status: string
  storage_authority: string
  system_memory_ingest: string
  last_active_at: string | null
}

export interface WorkspaceTask {
  id: string
  project_id: string
  title: string | null
  goal: string | null
  status: string
  root_rel: string
  summary: string | null
  display_root: string
  last_active_at: string | null
}

export interface BindingHistoryEntry {
  from_task_id: string | null
  to_task_id: string | null
  from_version: number | null
  to_version: number | null
  actor: string
  reason: string | null
  created_at: string | null
}

/** GET /api/conversations/{id}/workspace-binding 的快照（含改绑历史）。 */
export interface BindingSnapshot {
  mode: ScopingMode
  bound: boolean
  task: { id: string; title: string | null; status: string; display_root: string } | null
  project: { id: string; name: string } | null
  binding_version: number | null
  system_memory_ingest?: string
  supported_modes?: string[]
  note?: string
  history: BindingHistoryEntry[]
}

export async function fetchCapabilities(signal?: AbortSignal): Promise<WorkspaceCapabilities> {
  const { data } = await api.get<WorkspaceCapabilities>('/workspace/capabilities', { signal })
  return data
}

export async function fetchBinding(
  conversationId: string,
  signal?: AbortSignal,
): Promise<BindingSnapshot> {
  const { data } = await api.get<BindingSnapshot>(
    `/conversations/${encodeURIComponent(conversationId)}/workspace-binding`,
    { signal },
  )
  return data
}

export async function fetchProjects(signal?: AbortSignal): Promise<WorkspaceProject[]> {
  const { data } = await api.get<{ projects: WorkspaceProject[] }>('/workspace/projects', {
    signal,
  })
  return data.projects
}

export async function fetchProjectTasks(
  projectId: string,
  signal?: AbortSignal,
): Promise<WorkspaceTask[]> {
  const { data } = await api.get<{ tasks: WorkspaceTask[] }>(
    `/workspace/projects/${encodeURIComponent(projectId)}/tasks`,
    { signal },
  )
  return data.tasks
}

/** 显式改绑（CAS）。409 detail.code=workspace_binding_stale → 调用方刷新后重试。 */
export async function rebindConversation(
  conversationId: string,
  taskId: string,
  expectedBindingVersion: number,
  reason = '',
): Promise<{ conversation_id: string; task_id: string; binding_version: number }> {
  const { data } = await api.post(
    `/conversations/${encodeURIComponent(conversationId)}/workspace-binding`,
    { task_id: taskId, expected_binding_version: expectedBindingVersion, reason },
  )
  return data
}

/** 改绑错误的稳定错误码（后端 409 detail 为 {error, code} 结构）。 */
export function bindingErrorCode(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (detail && typeof detail === 'object' && 'code' in detail) {
    return String((detail as { code: unknown }).code)
  }
  return typeof detail === 'string' ? detail : ''
}

/** 未绑定会话的首绑（歧义选择器）：CAS=「当前必须无绑定」，并发双选仅一成功。 */
export function bindConversation(
  conversationId: string,
  taskId: string,
  reason = '歧义选择',
): Promise<{ conversation_id: string; task_id: string; binding_version: number }> {
  return rebindConversation(conversationId, taskId, 0, reason)
}

/** 歧义「开新任务」：后端建收件箱空 Task 并绑定（已有绑定则幂等返回）。 */
export async function bindNewTask(
  conversationId: string,
  title = '',
): Promise<{ conversation_id: string; task_id: string; binding_version: number }> {
  const { data } = await api.post(
    `/conversations/${encodeURIComponent(conversationId)}/workspace-binding/new-task`,
    { title },
  )
  return data
}
