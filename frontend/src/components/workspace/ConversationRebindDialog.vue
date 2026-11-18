<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 会话改绑卡片（2026-11-16 Wave B）— 在任务管理中心里直接把某条会话改绑到别的任务。
 *
 * 为什么不是复用 `WorkspaceBindingDialog`：那个面板读 `workspaceScope` store 的快照，
 * 而 store 只跟踪「当前聊天会话」。任务管理中心的会话列表里点开的往往是**别的**会话，
 * 用 store 会把改绑目标悄悄指向当前聊天会话——所以这里按 conversationId 直连
 * `GET/POST /api/conversations/{id}/workspace-binding`，改绑哪条就是哪条。
 *
 * 不变量（§6.1）：改绑只切「后续消息归属」，不移动旧文件、不追溯改写逐消息来源。
 * CAS：提交必须带 `expected_binding_version`；409 `workspace_binding_stale` →
 * 自动刷新快照并提示重试（不静默覆盖另一端的改动）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  bindingErrorCode,
  fetchBinding,
  fetchProjectTasks,
  fetchProjects,
  rebindConversation,
  type BindingSnapshot,
  type WorkspaceProject,
  type WorkspaceTask,
} from '@/api/workspace'
import { historyLine } from '@/utils/workspaceScope'

const props = defineProps<{
  visible: boolean
  conversationId: string
  conversationTitle?: string
  /** 打开时所在的任务（用于「当前/目标」文案）。 */
  currentTaskId?: string
  currentTaskTitle?: string
}>()

const emit = defineEmits<{ close: []; done: [taskId: string] }>()

const snap = ref<BindingSnapshot | null>(null)
const projects = ref<WorkspaceProject[]>([])
const tasks = ref<WorkspaceTask[]>([])
const selectedProject = ref('')
const selectedTask = ref('')
const reason = ref('')
const loading = ref(false)
const busy = ref(false)
const notice = ref('')
const noticeKind = ref<'ok' | 'err'>('ok')
const riskAck = ref(false)

const titles = new Map<string, string>()
const deathmatchActive = computed(() => !!snap.value?.deathmatch_active)
const canSubmit = computed(() =>
  !!props.conversationId && !!selectedTask.value && !busy.value
  && (!deathmatchActive.value || riskAck.value))

function titleOf(taskId: string | null): string {
  if (!taskId) return '—'
  return titles.get(taskId) || `${taskId.slice(0, 8)}…`
}

const historyRows = computed(() =>
  (snap.value?.history || []).map((h) => ({
    when: h.created_at ? new Date(h.created_at).toLocaleString() : '',
    text: historyLine(h, titleOf),
  })),
)

async function load() {
  if (!props.conversationId) return
  loading.value = true
  notice.value = ''
  try {
    const [snapshot, projectList] = await Promise.all([
      fetchBinding(props.conversationId),
      fetchProjects(),
    ])
    snap.value = snapshot
    projects.value = projectList
    if (snapshot.project?.id) selectedProject.value = snapshot.project.id
    else if (projectList.length === 1) selectedProject.value = projectList[0].id
    if (snapshot.task) titles.set(snapshot.task.id, snapshot.task.title || '未命名任务')
    if (selectedProject.value) await pickProject(selectedProject.value, true)
  } catch (e: any) {
    const d = e?.response?.data?.detail
    notice.value = (d && typeof d === 'object' ? String(d.error || '') : String(d || '')) || '加载绑定信息失败'
    noticeKind.value = 'err'
  } finally {
    loading.value = false
  }
}

async function pickProject(projectId: string, keep = false) {
  selectedProject.value = projectId
  if (!keep) selectedTask.value = ''
  tasks.value = []
  try {
    tasks.value = await fetchProjectTasks(projectId)
    for (const t of tasks.value) titles.set(t.id, t.title || '未命名任务')
    if (keep && snap.value?.task?.id && tasks.value.some((t) => t.id === snap.value!.task!.id)) {
      selectedTask.value = ''
    }
  } catch {
    tasks.value = []
  }
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  notice.value = ''
  try {
    const res = await rebindConversation(
      props.conversationId,
      selectedTask.value,
      snap.value?.binding_version ?? 0,
      reason.value.trim(),
    )
    const parked = Number((res as any)?.parked_runs || 0)
    const migrated = Number((res as any)?.migrated_files || 0)
    const target = titles.get(selectedTask.value) || '目标任务'
    const targetId = selectedTask.value
    const tail = migrated > 0
      ? `旧任务已无其他会话，已把它的 ${migrated} 个文件迁入本任务`
      : '旧任务文件不移动'
    notice.value = parked > 0
      ? `已改绑到「${target}」：停止了 ${parked} 个进行中的运行，后续消息与文件进入新任务，${tail}`
      : `已改绑到「${target}」：后续消息与文件进入新任务，${tail}`
    noticeKind.value = 'ok'
    selectedTask.value = ''
    reason.value = ''
    riskAck.value = false
    // 刷新快照只是锦上添花：改绑已经成功，刷新失败不能反过来报「改绑失败」，
    // 否则用户会再提交一次（评审 m5）。
    try { snap.value = await fetchBinding(props.conversationId) } catch { /* 保留成功提示 */ }
    emit('done', targetId)
  } catch (e: any) {
    if (bindingErrorCode(e) === 'workspace_binding_stale') {
      notice.value = '这条会话在别处刚被改绑过，已刷新最新版本，请重新选择后再提交'
      try { snap.value = await fetchBinding(props.conversationId) } catch { /* 保留提示 */ }
    } else {
      const d = e?.response?.data?.detail
      notice.value = (d && typeof d === 'object' ? String(d.error || d.code || '') : String(d || '')) || '改绑失败'
    }
    noticeKind.value = 'err'
  } finally {
    busy.value = false
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}

watch(() => props.visible, (v) => {
  if (!v) {
    document.removeEventListener('keydown', onKeydown)
    notice.value = ''
    selectedTask.value = ''
    riskAck.value = false
    return
  }
  document.addEventListener('keydown', onKeydown)
  void load()
})

onMounted(() => { if (props.visible) { document.addEventListener('keydown', onKeydown); void load() } })
// 卡片开着时组件被卸载（路由切换 / 设置卡关闭）也要摘掉监听（评审 m7）
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <Transition name="crd">
      <div v-if="visible" class="crd-mask" @mousedown.self="emit('close')">
        <div class="crd-card" role="dialog" aria-modal="true" aria-label="改绑会话"
             data-testid="conversation-rebind-dialog">
          <header class="crd-head">
            <div class="crd-head__text">
              <h3 class="crd-title">改绑会话</h3>
              <p class="crd-sub">
                {{ conversationTitle || '未命名会话' }}
                <span v-if="currentTaskTitle"> · 现在在「{{ currentTaskTitle }}」</span>
              </p>
            </div>
            <button class="crd-x" aria-label="关闭" data-testid="conversation-rebind-close" @click="emit('close')">✕</button>
          </header>

          <div class="crd-body">
            <div v-if="loading" class="crd-status">加载中…</div>

            <template v-else>
              <section class="crd-current">
                <div class="crd-label">当前绑定</div>
                <template v-if="snap?.bound && snap.task">
                  <div class="crd-current__title">
                    {{ snap.project?.name ? `${snap.project.name} · ` : '' }}{{ snap.task.title || '未命名任务' }}
                  </div>
                  <div class="crd-current__meta">
                    绑定版本 v<span class="crd-tnum">{{ snap.binding_version ?? 0 }}</span>
                    · {{ snap.task.status }}
                  </div>
                </template>
                <div v-else class="crd-current__meta">这条会话还没有绑定任务（首次绑定同样在这里完成）。</div>
              </section>

              <section class="crd-section">
                <div class="crd-label">改绑到</div>
                <select v-if="projects.length > 1" class="crd-select" :value="selectedProject"
                        data-testid="conversation-rebind-project"
                        @change="pickProject(($event.target as HTMLSelectElement).value)">
                  <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
                </select>
                <select v-model="selectedTask" class="crd-select" data-testid="conversation-rebind-task"
                        @change="riskAck = false">
                  <option value="" disabled>{{ loading ? '加载中…' : '选择任务' }}</option>
                  <option v-for="t in tasks" :key="t.id" :value="t.id"
                          :disabled="t.id === snap?.task?.id">
                    {{ t.title || '未命名任务' }}{{ t.id === snap?.task?.id ? '（当前）' : '' }}
                  </option>
                </select>
                <input v-model="reason" class="crd-input" placeholder="改绑原因（可选，进绑定历史）" />

                <div v-if="deathmatchActive" class="crd-risk" data-testid="conversation-rebind-risk">
                  <strong>死磕进行中。</strong>
                  改绑会切换任务文件域并重置校验快照，已完成步骤可能被重新打开。
                  <label class="crd-risk__ack">
                    <input v-model="riskAck" type="checkbox" data-testid="conversation-rebind-risk-ack" />
                    我了解风险，仍要改绑
                  </label>
                </div>

                <button class="crd-btn crd-btn--primary" :disabled="!canSubmit" data-testid="conversation-rebind-submit"
                        @click="submit">
                  {{ busy ? '提交中…' : '改绑（后续消息生效）' }}
                </button>
                <p class="crd-hint">改绑不移动旧文件、不改写历史消息的来源归属。</p>
                <p v-if="notice" class="crd-notice" :class="`crd-notice--${noticeKind}`"
                   data-testid="conversation-rebind-notice">{{ notice }}</p>
              </section>

              <section v-if="historyRows.length" class="crd-section">
                <div class="crd-label">绑定历史</div>
                <ul class="crd-history">
                  <li v-for="(row, i) in historyRows" :key="i">
                    <span class="crd-history__when">{{ row.when }}</span>
                    <span>{{ row.text }}</span>
                  </li>
                </ul>
              </section>
            </template>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.crd-mask {
  position: fixed; inset: 0; z-index: 10006; display: flex; align-items: center; justify-content: center;
  padding: 20px; background: var(--overlay-scrim, rgba(0, 0, 0, .35));
}
.crd-card {
  width: 100%; max-width: 460px; display: flex; flex-direction: column;
  background: var(--surface-panel-strong, var(--color-white)); color: var(--color-text);
  border-radius: 16px; overflow: hidden;
  box-shadow: 0 1px 2px rgba(15, 23, 42, .06), 0 8px 24px rgba(15, 23, 42, .12), 0 24px 56px rgba(15, 23, 42, .18);
}
.crd-head { display: flex; align-items: flex-start; gap: 12px; padding: 16px 16px 8px; }
.crd-head__text { min-width: 0; flex: 1; }
.crd-title { margin: 0; font-size: 16px; font-weight: 800; letter-spacing: -.01em; text-wrap: balance; }
.crd-sub {
  margin: 4px 0 0; font-size: 12.5px; color: var(--color-text-light);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.crd-x {
  width: 28px; height: 28px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border: 0; background: transparent; color: var(--color-text-light); cursor: pointer; border-radius: 8px;
  font-size: 13px; transition-property: background-color, color, transform; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.crd-x:hover { background: var(--color-hover, rgba(0, 0, 0, .05)); color: var(--color-text); }
.crd-x:active { transform: scale(.96); }
.crd-body { padding: 4px 16px 16px; display: flex; flex-direction: column; gap: 12px; max-height: 70vh; overflow-y: auto; }
.crd-status { padding: 18px; text-align: center; color: var(--color-text-light); font-size: 13px; }
.crd-label { font-size: 11px; letter-spacing: .06em; text-transform: uppercase; font-weight: 700; color: var(--color-text-light); margin-bottom: 6px; }
.crd-current {
  padding: 10px 12px; border-radius: 10px; background: var(--surface-panel-subtle, rgba(0, 0, 0, .03));
}
.crd-current__title { font-weight: 800; font-size: 14px; }
.crd-current__meta { font-size: 12px; color: var(--color-text-light); margin-top: 3px; }
.crd-tnum { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1; }
.crd-section { display: flex; flex-direction: column; gap: 8px; }
.crd-select, .crd-input {
  width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid var(--panel-border);
  border-radius: 8px; background: var(--surface-input, var(--color-white)); color: var(--color-text);
  font-size: 13.5px; font-family: inherit;
  transition-property: border-color, box-shadow; transition-duration: .15s;
}
.crd-select:focus, .crd-input:focus {
  outline: none; border-color: var(--color-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary) 18%, transparent);
}
.crd-risk {
  padding: 10px 12px; border: 1px solid var(--color-danger); border-radius: 10px;
  background: var(--danger-tint, rgba(220, 38, 38, .06)); font-size: 12.5px; line-height: 1.6;
}
.crd-risk strong { color: var(--color-danger); }
.crd-risk__ack { display: flex; align-items: center; gap: 6px; margin-top: 6px; font-weight: 700; cursor: pointer; }
.crd-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px;
  padding: 7px 14px; border: 1px solid var(--panel-border); border-radius: 8px;
  background: var(--surface-panel, var(--color-white)); color: var(--color-text);
  font-weight: 700; font-size: 13px; cursor: pointer; font-family: inherit;
  transition-property: background-color, border-color, transform, opacity; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.crd-btn:active:not(:disabled) { transform: scale(.96); }
.crd-btn:disabled { opacity: .5; cursor: not-allowed; }
.crd-btn--primary { background: var(--color-primary); border-color: var(--color-primary); color: var(--color-white, #fff); }
.crd-btn--primary:hover:not(:disabled) { filter: brightness(1.05); }
.crd-hint { margin: 0; font-size: 11.5px; color: var(--color-text-light); text-wrap: pretty; }
.crd-notice { margin: 0; font-size: 12.5px; font-weight: 700; text-wrap: pretty; }
.crd-notice--ok { color: var(--color-success, var(--color-primary)); }
.crd-notice--err { color: var(--color-error); }
.crd-history { list-style: none; margin: 0; padding: 0; font-size: 12px; color: var(--color-text-light); }
.crd-history li { padding: 5px 0; border-bottom: 1px dashed var(--panel-border); }
.crd-history li:last-child { border-bottom: 0; }
.crd-history__when { margin-right: 8px; font-variant-numeric: tabular-nums; }

.crd-enter-active { transition-property: opacity; transition-duration: .18s; transition-timing-function: cubic-bezier(.2, 0, 0, 1); }
.crd-enter-active .crd-card { transition-property: opacity, transform; transition-duration: .22s; transition-timing-function: cubic-bezier(.16, 1, .3, 1); }
.crd-enter-from { opacity: 0; }
.crd-enter-from .crd-card { opacity: 0; transform: translateY(8px) scale(.985); }
.crd-leave-active { transition-property: opacity; transition-duration: .14s; }
.crd-leave-active .crd-card { transition-property: opacity, transform; transition-duration: .14s; }
.crd-leave-to { opacity: 0; }
.crd-leave-to .crd-card { opacity: 0; transform: translateY(4px) scale(.99); }
@media (prefers-reduced-motion: reduce) {
  .crd-enter-active .crd-card, .crd-leave-active .crd-card { transition-duration: .01s; }
}
@media (max-width: 767px) {
  .crd-mask { padding: 0; }
  .crd-card { max-width: none; border-radius: 0; }
}
</style>
