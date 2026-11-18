// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 任务 Workspace 作用域 API（2026-11-10 波 · T14；2026-11-15 任务管理中心扩展）。
 *
 * 后端契约：`backend/app/api/workspace.py`（能力门 / 项目 / 任务 / 会话绑定 CAS /
 * 跨域 grant / 两段式删除 / 笔记绑定）。约定：
 * - `mode=off` 时 UI 完全不出现任务徽标（现网零变化）；
 * - 改绑必须携带 `expected_binding_version`（CAS），版本漂移返回 409
 *   `workspace_binding_stale`，由调用方刷新快照后重试；
 * - 项目/任务编辑携带 `expected_row_version`（CAS），漂移返回 409
 *   `workspace_project_stale` / `workspace_task_stale`。
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
  description?: string | null
  row_version?: number
  kind?: string
  is_default?: boolean
  reserved_name?: string
  deleted_at?: string | null
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
  description?: string | null
  origin?: string | null
  row_version?: number
  deleted_at?: string | null
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
  /** WP4-M5：死磕活跃时改绑会换根并重置校验快照（前端要求二次确认）。 */
  deathmatch_active?: boolean
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

/** 工作台「项目列表」overview（2026-11-14 波；2026-11-15 加 description/row_version/origin/notes_count）。 */
export interface WorkspaceConversationRef {
  id: string
  title: string
  updated_at: string | null
}

export interface WorkspaceTaskOverview {
  id: string
  title: string | null
  status: string
  root_rel: string
  display_root: string
  /** 2026-11-18：跨项目移动时用于排除任务当前所在项目（后端 _task_dict 已发）。 */
  project_id?: string
  description?: string | null
  origin?: string | null
  row_version?: number
  notes_count?: number
  /** 2026-11-17：设置页展示真实继承值（storage 不在可编辑面）。 */
  storage_authority?: string
  system_memory_ingest?: string | null
  last_active_at: string | null
  conversations: WorkspaceConversationRef[]
  conversations_truncated: boolean
}

export interface WorkspaceProjectOverview {
  id: string
  name: string
  status: string
  root_rel: string
  description?: string | null
  row_version?: number
  kind?: string
  /** 2026-11-17：设置页回写/展示真实值（此前前端硬编码 user_facts/cloud）。 */
  storage_authority?: string
  system_memory_ingest?: string | null
  last_active_at: string | null
  tasks: WorkspaceTaskOverview[]
  tasks_count?: number
  tasks_truncated: boolean
  /** 默认项目（kind='inbox' 稳定标识）：可改名/写备注，不可归档/删除。 */
  is_default: boolean
}

export async function fetchProjectsOverview(signal?: AbortSignal): Promise<WorkspaceProjectOverview[]> {
  const { data } = await api.get<{ projects: WorkspaceProjectOverview[] }>(
    '/workspace/projects/overview',
    { signal },
  )
  return data.projects
}

/** 新建项目（工作台/设置「项目管理」共用）。 */
export interface WorkspaceProjectCreated {
  id: string
  name: string
  root_rel: string
  status?: string
  storage_authority?: string
  system_memory_ingest?: string
  description?: string | null
  row_version?: number
  kind?: string
  is_default?: boolean
}

export interface ProjectCreateInput {
  name: string
  description?: string | null
  storage_authority?: string
  system_memory_ingest?: string | null
}

export async function createProject(input: string | ProjectCreateInput): Promise<WorkspaceProjectCreated> {
  const body = typeof input === 'string' ? { name: input } : input
  const { data } = await api.post<WorkspaceProjectCreated>('/workspace/projects', body)
  return data
}

export interface ProjectUpdateInput {
  name?: string
  description?: string | null
  system_memory_ingest?: string | null
  expected_row_version?: number
}

/** 项目编辑（名称/备注/记忆策略 + CAS）。 */
export async function updateProject(
  projectId: string,
  input: ProjectUpdateInput,
): Promise<WorkspaceProjectCreated> {
  const { data } = await api.patch<WorkspaceProjectCreated>(
    `/workspace/projects/${encodeURIComponent(projectId)}`,
    input,
  )
  return data
}

/** 兼容旧调用点：只改名称。 */
export async function renameProject(projectId: string, name: string): Promise<WorkspaceProjectCreated> {
  return updateProject(projectId, { name })
}

export interface DeleteInput {
  mode: 'archive' | 'purge'
  confirm_name?: string
  confirm_title?: string
  expected_row_version?: number
  conversations?: 'unbind' | 'delete'
}

export interface DeleteOutcome {
  id: string
  status: string
  mode: string
  files_moved?: boolean
  files_moved_to_trash?: boolean
  trash_rel?: string | null
  tasks?: number
  conversations_unbound?: number
  conversations_deleted?: number
  notes_unbound?: number
  counts?: { tasks: number; conversations: number; notes: number }
}

export async function deleteProject(projectId: string, input: DeleteInput): Promise<DeleteOutcome> {
  const { data } = await api.delete<DeleteOutcome>(
    `/workspace/projects/${encodeURIComponent(projectId)}`,
    { data: input },
  )
  return data
}

export async function restoreProject(projectId: string): Promise<WorkspaceProjectCreated> {
  const { data } = await api.post<WorkspaceProjectCreated>(
    `/workspace/projects/${encodeURIComponent(projectId)}/restore`,
  )
  return data
}

export interface DeletePreview {
  project_id?: string
  task_id?: string
  name?: string
  title?: string
  row_version?: number
  is_default?: boolean
  counts: { tasks: number; conversations: number; notes: number }
}

export async function previewProjectDelete(projectId: string): Promise<DeletePreview> {
  const { data } = await api.get<DeletePreview>(
    `/workspace/projects/${encodeURIComponent(projectId)}/delete-preview`,
  )
  return data
}

export async function previewTaskDelete(taskId: string): Promise<DeletePreview> {
  const { data } = await api.get<DeletePreview>(
    `/workspace/tasks/${encodeURIComponent(taskId)}/delete-preview`,
  )
  return data
}

export interface TrashRow {
  kind: 'project' | 'task'
  id: string
  name: string | null
  deleted_at: string | null
  expires_at: string | null
  expired: boolean
}

export async function fetchTrash(signal?: AbortSignal): Promise<{ projects: TrashRow[]; tasks: TrashRow[] }> {
  const { data } = await api.get<{ projects: TrashRow[]; tasks: TrashRow[] }>('/workspace/trash', { signal })
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

export interface TaskCreateInput {
  title: string
  description?: string | null
  goal?: string
  system_memory_ingest?: string | null
  tags?: string[]
}

export async function createTask(projectId: string, input: TaskCreateInput): Promise<WorkspaceTask> {
  const { data } = await api.post<WorkspaceTask>(
    `/workspace/projects/${encodeURIComponent(projectId)}/tasks`,
    input,
  )
  return data
}

export interface TaskUpdateInput {
  title?: string
  description?: string | null
  status?: string
  expected_row_version?: number
}

export async function updateTask(taskId: string, input: TaskUpdateInput): Promise<WorkspaceTask> {
  const { data } = await api.patch<WorkspaceTask>(
    `/workspace/tasks/${encodeURIComponent(taskId)}`,
    input,
  )
  return data
}

export async function deleteTask(taskId: string, input: DeleteInput): Promise<DeleteOutcome> {
  const { data } = await api.delete<DeleteOutcome>(
    `/workspace/tasks/${encodeURIComponent(taskId)}`,
    { data: input },
  )
  return data
}

export async function restoreTask(taskId: string): Promise<WorkspaceTask> {
  const { data } = await api.post<WorkspaceTask>(
    `/workspace/tasks/${encodeURIComponent(taskId)}/restore`,
  )
  return data
}

/** 跨项目移动 1 个或多个任务（2026-11-18 issue1）。改挂项目 + 物理搬移任务目录。 */
export async function moveTasks(
  taskIds: string[],
  targetProjectId: string,
): Promise<{ moved: number; task_ids: string[]; dirs_moved: number }> {
  const { data } = await api.post<{ moved: number; task_ids: string[]; dirs_moved: number }>(
    '/workspace/tasks/move',
    { task_ids: taskIds, target_project_id: targetProjectId },
  )
  return data
}

export interface TaskConversationRow {
  id: string
  title: string
  updated_at: string | null
}

export async function fetchTaskConversations(
  taskId: string,
  signal?: AbortSignal,
): Promise<TaskConversationRow[]> {
  const { data } = await api.get<{ task_id: string; conversations: TaskConversationRow[] }>(
    `/workspace/tasks/${encodeURIComponent(taskId)}/conversations`,
    { signal },
  )
  return data.conversations
}

export async function createConversationInTask(
  taskId: string,
  title = '',
): Promise<{ conversation_id: string; task_id: string; binding_version: number }> {
  const { data } = await api.post(
    `/workspace/tasks/${encodeURIComponent(taskId)}/conversations`,
    { title },
  )
  return data
}

export interface TaskNoteRow {
  id: string
  title: string | null
  notebook_id: string
  /** 镜像列（= 最早那条绑定），2026-11-16 Wave B 起不再代表「唯一绑定」。 */
  task_id: string | null
  task_bound_at: string | null
  updated_at: string | null
  excerpt: string
  /** 该笔记当前的全部绑定任务（多对多）。 */
  bound_task_ids?: string[]
  /** 该笔记共绑了几个任务（>1 时卡片显示徽标）。 */
  bound_task_count?: number
}

export async function fetchTaskNotes(taskId: string, signal?: AbortSignal): Promise<TaskNoteRow[]> {
  const { data } = await api.get<{ task_id: string; notes: TaskNoteRow[] }>(
    `/workspace/tasks/${encodeURIComponent(taskId)}/notes`,
    { signal },
  )
  return data.notes
}

/** 批量把笔记绑到任务（幂等：已绑的进 already_bound，不产生重复关系行）。 */
export async function bindNotesToTask(
  taskId: string,
  noteIds: string[],
): Promise<{
  task_id: string
  bound: string[]
  already_bound: string[]
  failed: Array<{ note_id: string; code: string; error: string }>
  bound_task_count: number
}> {
  const { data } = await api.post(
    `/workspace/tasks/${encodeURIComponent(taskId)}/notes/bind`,
    { note_ids: noteIds },
  )
  return data
}

/** 从任务上批量解绑笔记（只解关系，笔记不删；其他任务的绑定保留）。 */
export async function unbindNotesFromTask(
  taskId: string,
  noteIds: string[],
): Promise<{ task_id: string; unbound: string[] }> {
  const { data } = await api.post(
    `/workspace/tasks/${encodeURIComponent(taskId)}/notes/unbind`,
    { note_ids: noteIds },
  )
  return data
}

export interface NoteBindingTaskRef {
  task_id: string
  title: string | null
  status: string
  project_id: string
  project_name: string | null
  bound_at: string | null
}

/** 批量反查「笔记 → 已绑任务数」（选择器徽标）。 */
export async function fetchNoteBindingCounts(
  noteIds: string[],
  signal?: AbortSignal,
): Promise<Record<string, number>> {
  if (!noteIds.length) return {}
  const { data } = await api.get<{ bindings: Array<{ note_id: string; task_count: number }> }>(
    '/workspace/notes/bindings',
    { params: { note_ids: noteIds.join(',') }, signal },
  )
  const out: Record<string, number> = {}
  for (const row of data.bindings || []) out[row.note_id] = row.task_count
  return out
}

/**
 * 单条笔记的绑定操作（多对多）。
 * `bind` = 加绑（幂等，不影响其他绑定）· `unbind` = 只解 task_id 那条 ·
 * `clear`/`task_id=null` = 清空全部绑定（旧「解绑」语义）。
 */
export async function setNoteTaskBinding(
  noteId: string,
  taskId: string | null,
  action: 'bind' | 'unbind' | 'clear' = 'bind',
): Promise<{
  id: string
  task_id: string | null
  task_bound_at: string | null
  task_ids: string[]
}> {
  const { data } = await api.post(
    `/notes/${encodeURIComponent(noteId)}/task-binding`,
    { task_id: taskId, action },
  )
  return data
}

export async function bindNoteToTask(
  noteId: string,
  taskId: string | null,
): Promise<{ id: string; task_id: string | null; task_bound_at: string | null }> {
  return setNoteTaskBinding(noteId, taskId, taskId ? 'bind' : 'clear')
}

export interface TaskFileEntry {
  name: string
  rel_path: string
  is_dir: boolean
  size: number
  mtime: number
  type: string
  /** 方案A Piece 1：跨任务「本会话产出」视图带来源任务归属（单任务/目录列表条目无此字段）。 */
  task_id?: string
  task_title?: string | null
  display_root?: string
}

export interface TaskFilesResponse {
  task_id: string
  display_root: string
  conversation_id: string | null
  listing?: { path: string; entries: TaskFileEntry[]; truncated: boolean }
  produced?: TaskFileEntry[]
}

export async function fetchTaskFiles(
  taskId: string,
  conversationId?: string,
  signal?: AbortSignal,
): Promise<TaskFilesResponse> {
  const { data } = await api.get<TaskFilesResponse>(
    `/workspace/tasks/${encodeURIComponent(taskId)}/files`,
    { params: conversationId ? { conversation_id: conversationId } : undefined, signal },
  )
  return data
}

/** 显式改绑（CAS）。409 detail.code=workspace_binding_stale → 调用方刷新后重试。 */
export async function rebindConversation(
  conversationId: string,
  taskId: string,
  expectedBindingVersion: number,
  reason = '',
): Promise<{ conversation_id: string; task_id: string; binding_version: number; parked_runs?: number; migrated_files?: number }> {
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

/** 后端错误 detail.error（人类可读）或兜底文案。 */
export function apiErrorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (detail && typeof detail === 'object' && 'error' in detail) {
    return String((detail as { error: unknown }).error)
  }
  if (typeof detail === 'string' && detail) return detail
  return fallback
}

/** 未绑定会话的首绑（歧义选择器）：CAS=「当前必须无绑定」，并发双选仅一成功。 */
export function bindConversation(
  conversationId: string,
  taskId: string,
  reason = '歧义选择',
): Promise<{ conversation_id: string; task_id: string; binding_version: number }> {
  return rebindConversation(conversationId, taskId, 0, reason)
}

/** 歧义「开新任务」：后端建默认项目空 Task 并绑定（已有绑定则幂等返回）。 */
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
