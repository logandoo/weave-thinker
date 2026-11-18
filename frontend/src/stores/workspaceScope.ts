// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 任务 Workspace 作用域 store（2026-11-10 波 · T14）。
 *
 * 只管「当前会话绑定 + 能力门」两份服务端态：
 * - capabilities 进程内加载一次（mode 变了要刷新页面，与皮肤同步同级语义）；
 * - snapshot 跟随 currentConversationId 切换；改绑走 CAS，
 *   版本漂移（workspace_binding_stale）自动刷新一次快照并抛出 `stale` 供 UI 提示。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  bindConversation,
  bindNewTask,
  bindingErrorCode,
  fetchBinding,
  fetchCapabilities,
  fetchProjects,
  fetchProjectTasks,
  rebindConversation,
  type BindingSnapshot,
  type WorkspaceCapabilities,
  type WorkspaceProject,
  type WorkspaceTask,
} from '@/api/workspace'
import { bindingBadgeText, isBindingStale, scopeChipVisible, scopeModeHint } from '@/utils/workspaceScope'

export interface AmbiguousCandidate {
  task_id: string
  project_id?: string
  title?: string
  reason?: string
  last_active_at?: string | null
}

export const useWorkspaceScopeStore = defineStore('workspaceScope', () => {
  const capabilities = ref<WorkspaceCapabilities | null>(null)
  // §6.1 歧义只问一次：后端 workspace_task_ambiguous 事件的候选集，
  // 按会话挂载；选择 → expected=0 首绑 → 用户重发消息即续跑。
  const ambiguous = ref<{ conversationId: string; question: string;
                          candidates: AmbiguousCandidate[] } | null>(null)
  // 2026-11-14 v2：新会话归类建议（meta.workspace_suggestion）——非阻塞询问
  // conversationId=服务端权威 id；clientId=流发起时的客户端 id（未 remint 时二者不同，
  // 建议必须两者都能匹配当前会话，否则新会话永远看不到询问条）。
  const suggestion = ref<{ conversationId: string; clientId?: string;
                           task: AmbiguousCandidate } | null>(null)
  const snapshot = ref<BindingSnapshot | null>(null)
  const loading = ref(false)
  const lastError = ref('')
  // 2026-11-16 Wave D（issue4）：改绑时服务端自动停泊的活跃 run 数，供 UI 说明「已停止进行中的任务」。
  const lastParkedRuns = ref(0)
  // 2026-11-16 Wave D（issue3）：当前会话「新出现一个绑定任务」时递增，
  // 让已打开的项目管理面板无需整页刷新即自动重载总览（右上角 chip 走 loadBinding）。
  const overviewRevision = ref(0)
  let _lastBoundTaskByConv: Record<string, string> = {}
  let _capsPromise: Promise<void> | null = null

  const chipVisible = computed(() => scopeChipVisible(capabilities.value, snapshot.value))
  const badgeText = computed(() => bindingBadgeText(snapshot.value))
  const modeHint = computed(() => scopeModeHint(capabilities.value, snapshot.value))
  /** 面板可选任务列表用的加载器（点开面板才拉，避免每次切会话打两个请求）。 */
  async function loadProjects(): Promise<WorkspaceProject[]> {
    return fetchProjects()
  }
  async function loadTasks(projectId: string): Promise<WorkspaceTask[]> {
    return fetchProjectTasks(projectId)
  }

  async function ensureCapabilities(): Promise<void> {
    if (capabilities.value) return
    if (!_capsPromise) {
      _capsPromise = fetchCapabilities()
        .then((c) => {
          capabilities.value = c
        })
        .finally(() => {
          _capsPromise = null
        })
    }
    await _capsPromise
  }

  let _bindingSeq = 0
  async function loadBinding(conversationId: string | null): Promise<void> {
    if (!conversationId) {
      snapshot.value = null
      return
    }
    // 桌面+移动双 header 各挂一个 chip → 同一会话可能并发触发；序号守卫
    // 保证「最后发起的请求」写快照，且重复请求直接丢弃在途结果。
    const seq = ++_bindingSeq
    try {
      await ensureCapabilities()
      if (capabilities.value?.mode === 'off') {
        snapshot.value = null
        return
      }
      const snap = await fetchBinding(conversationId)
      if (seq === _bindingSeq) {
        snapshot.value = snap
        // 会话已被（其他路径）绑定 → 歧义问题已过期，撤下陈旧 CTA（评审 r3 #5）
        if (snap?.bound && ambiguous.value?.conversationId === conversationId) {
          ambiguous.value = null
        }
        // issue3：本轮首次出现绑定任务（自动建任务/别处改绑）→ 通知已打开的项目管理重载。
        const boundTaskId = snap?.bound ? String(snap.task?.id || '') : ''
        if (boundTaskId && _lastBoundTaskByConv[conversationId] !== boundTaskId) {
          _lastBoundTaskByConv[conversationId] = boundTaskId
          overviewRevision.value++
        }
      }
    } catch (err) {
      if (seq === _bindingSeq) {
        snapshot.value = null
        lastError.value = String((err as Error)?.message || err)
      }
    }
  }

  /**
   * 显式改绑（CAS）。返回 true=成功；409 stale → 自动刷新快照后返回 false，
   * 调用方（面板）据此提示「绑定已变更，已刷新，请重试」。
   */
  async function rebind(conversationId: string, taskId: string, reason = ''): Promise<boolean> {
    const version = snapshot.value?.binding_version ?? 0
    loading.value = true
    lastError.value = ''
    lastParkedRuns.value = 0
    try {
      const res = await rebindConversation(conversationId, taskId, version, reason)
      lastParkedRuns.value = Number(res?.parked_runs || 0)
      await loadBinding(conversationId)
      return true
    } catch (err) {
      const code = bindingErrorCode(err)
      if (isBindingStale(code)) {
        await loadBinding(conversationId)
        lastError.value = '绑定已变更（其他端改绑或新 run），已刷新，请重试'
      } else {
        lastError.value = code || String((err as Error)?.message || err)
      }
      return false
    } finally {
      loading.value = false
    }
  }

  function setAmbiguous(conversationId: string, payload: { question: string; candidates: AmbiguousCandidate[] }) {
    ambiguous.value = { conversationId, question: payload.question, candidates: payload.candidates }
  }
  function clearAmbiguous() {
    ambiguous.value = null
  }

  /** 归类建议：服务端 meta.workspace_suggestion（本轮已在默认项目执行）。 */
  function setSuggestion(conversationId: string, task: AmbiguousCandidate, clientId?: string) {
    if (!conversationId || !task?.task_id) return
    suggestion.value = { conversationId, clientId: clientId || undefined, task }
  }
  function clearSuggestion() {
    suggestion.value = null
  }
  /** 接受建议：CAS 改绑到候选任务（后续消息生效；旧任务文件不动）。 */
  async function acceptSuggestion(): Promise<boolean> {
    const s = suggestion.value
    if (!s) return false
    loading.value = true
    lastError.value = ''
    try {
      const snap = await fetchBinding(s.conversationId)
      await rebindConversation(s.conversationId, s.task.task_id,
                               snap.binding_version ?? 0, '归类建议')
      await loadBinding(s.conversationId)
      suggestion.value = null
      return true
    } catch (err) {
      const code = bindingErrorCode(err)
      if (isBindingStale(code)) {
        lastError.value = '绑定已变更（其他端改绑或新 run），已刷新，请重试'
        await loadBinding(s.conversationId)
      } else if (code === 'workspace_run_active') {
        // 2026-11-16 Wave D：后端已改为自动停泊活跃 run，正常情况下不再返回此码；
        // 保留为对旧后端的兜底，文案不再要求用户「等回答完成」。
        lastError.value = '归类失败：进行中的任务未能自动停止，请稍后重试'
      } else if (code === 'workspace_scope_mode_unsupported') {
        lastError.value = '该会话的执行模式暂不支持任务归类'
      } else {
        lastError.value = code || String((err as Error)?.message || err)
      }
      return false
    } finally {
      loading.value = false
    }
  }
  /** 拒绝建议：保持默认项目（不产生任何写入）。 */
  function dismissSuggestion() {
    suggestion.value = null
  }
  async function chooseTask(taskId: string): Promise<boolean> {
    const conv = ambiguous.value?.conversationId
    if (!conv) return false
    loading.value = true
    try {
      await bindConversation(conv, taskId)
      await loadBinding(conv)
      ambiguous.value = null
      return true
    } catch (err) {
      lastError.value = bindingErrorCode(err) || String((err as Error)?.message || err)
      return false
    } finally {
      loading.value = false
    }
  }

  async function chooseNewTask(): Promise<boolean> {
    const conv = ambiguous.value?.conversationId
    if (!conv) return false
    loading.value = true
    try {
      await bindNewTask(conv)
      await loadBinding(conv)
      ambiguous.value = null
      return true
    } catch (err) {
      const code = bindingErrorCode(err)
      lastError.value = isBindingStale(code)
        ? '该会话已绑定任务（可能刚开新任务或另一处已选择），请重新发送消息即可继续'
        : (code || String((err as Error)?.message || err))
      return false
    } finally {
      loading.value = false
    }
  }

  return {
    capabilities,
    ambiguous,
    setAmbiguous,
    clearAmbiguous,
    suggestion,
    setSuggestion,
    clearSuggestion,
    acceptSuggestion,
    dismissSuggestion,
    chooseTask,
    chooseNewTask,
    snapshot,
    loading,
    lastError,
    lastParkedRuns,
    overviewRevision,
    chipVisible,
    badgeText,
    modeHint,
    ensureCapabilities,
    loadBinding,
    loadProjects,
    loadTasks,
    rebind,
  }
})
