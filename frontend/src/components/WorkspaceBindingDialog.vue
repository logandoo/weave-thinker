<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 任务绑定面板（2026-11-10 波 · T14）：当前绑定、改绑历史、显式改绑（CAS）。
 *
 * 改绑只切「后续消息归属」，不移动旧文件、不追溯改写逐消息来源（§6.1 不变量）；
 * 版本漂移由 store 自动刷新快照并提示重试。
 */
import { computed, onMounted, ref } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useWorkspaceScopeStore } from '@/stores/workspaceScope'
import { historyLine } from '@/utils/workspaceScope'
import type { WorkspaceProject, WorkspaceTask } from '@/api/workspace'

const emit = defineEmits<{ close: [] }>()
const chatStore = useChatStore()
const scope = useWorkspaceScopeStore()

const projects = ref<WorkspaceProject[]>([])
const selectedProject = ref('')
const tasks = ref<WorkspaceTask[]>([])
const selectedTask = ref('')
const reason = ref('')
const busy = ref(false)
const notice = ref('')

const convId = computed(() => chatStore.currentConversationId)
const snap = computed(() => scope.snapshot)
const canRebind = computed(() => !!convId.value && !!selectedTask.value && !busy.value)

const titles = new Map<string, string>()
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

onMounted(async () => {
  busy.value = true
  try {
    projects.value = await scope.loadProjects()
    if (snap.value?.project?.id) selectedProject.value = snap.value.project.id
    else if (projects.value.length === 1) selectedProject.value = projects.value[0].id
    if (selectedProject.value) await pickProject(selectedProject.value)
    // 历史里的 from/to 任务标题尽量补全（当前项目内可解析的直接命中）
    for (const t of tasks.value) titles.set(t.id, t.title || '未命名任务')
    if (snap.value?.task) titles.set(snap.value.task.id, snap.value.task.title || '未命名任务')
  } finally {
    busy.value = false
  }
})

async function pickProject(projectId: string) {
  selectedProject.value = projectId
  selectedTask.value = ''
  tasks.value = []
  busy.value = true
  try {
    tasks.value = await scope.loadTasks(projectId)
    for (const t of tasks.value) titles.set(t.id, t.title || '未命名任务')
  } finally {
    busy.value = false
  }
}

async function doRebind() {
  if (!convId.value || !selectedTask.value) return
  notice.value = ''
  const ok = await scope.rebind(convId.value, selectedTask.value, reason.value.trim())
  if (ok) {
    notice.value = '已改绑：后续消息与文件进入新任务（旧任务文件不动）'
    selectedTask.value = ''
    reason.value = ''
  } else {
    notice.value = scope.lastError
  }
}
</script>

<template>
  <Teleport to="body">
    <div class="wsd-mask" data-testid="workspace-binding-dialog" @click.self="emit('close')">
      <div class="wsd-panel" role="dialog" aria-label="任务绑定">
        <header class="wsd-head">
          <h3>任务绑定</h3>
          <button class="wsd-close" aria-label="关闭" @click="emit('close')">✕</button>
        </header>

        <section class="wsd-current">
          <template v-if="snap?.bound && snap.task">
            <div class="wsd-current__title">
              {{ snap.project?.name ? `${snap.project.name} · ` : '' }}{{ snap.task.title || '未命名任务' }}
            </div>
            <div class="wsd-current__meta">
              版本 v{{ snap.binding_version }} · {{ snap.task.status }} · {{ snap.task.display_root }}
            </div>
          </template>
          <div v-else class="wsd-current__meta">当前会话未绑定任务（legacy）</div>
        </section>

        <section class="wsd-section">
          <div class="wsd-label">改绑到</div>
          <select
            v-if="projects.length > 1"
            class="wsd-select"
            :value="selectedProject"
            @change="pickProject(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <select v-model="selectedTask" class="wsd-select" data-testid="workspace-task-select">
            <option value="" disabled>{{ busy ? '加载中…' : '选择任务' }}</option>
            <option
              v-for="t in tasks"
              :key="t.id"
              :value="t.id"
              :disabled="t.id === snap?.task?.id"
            >
              {{ t.title || '未命名任务' }}{{ t.id === snap?.task?.id ? '（当前）' : '' }}
            </option>
          </select>
          <input v-model="reason" class="wsd-input" placeholder="改绑原因（可选，进绑定历史）" />
          <button class="wsd-rebind" :disabled="!canRebind" data-testid="workspace-rebind-btn" @click="doRebind">
            {{ scope.loading ? '提交中…' : '改绑（后续消息生效）' }}
          </button>
          <p v-if="notice" class="wsd-notice">{{ notice }}</p>
        </section>

        <section v-if="historyRows.length" class="wsd-section">
          <div class="wsd-label">绑定历史</div>
          <ul class="wsd-history">
            <li v-for="(row, i) in historyRows" :key="i">
              <span class="wsd-history__when">{{ row.when }}</span>
              <span>{{ row.text }}</span>
            </li>
          </ul>
        </section>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.wsd-mask {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
}
.wsd-panel {
  width: min(480px, calc(100vw - 32px));
  max-height: min(80vh, 640px);
  overflow-y: auto;
  background: var(--surface-panel-strong, var(--color-bg));
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px;
}
.wsd-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.wsd-head h3 {
  margin: 0;
  font-size: 15px;
}
.wsd-close {
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 14px;
}
.wsd-current {
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  margin-bottom: 12px;
}
.wsd-current__title {
  font-weight: 600;
  font-size: 14px;
}
.wsd-current__meta {
  color: var(--color-text-secondary);
  font-size: 12px;
  margin-top: 2px;
  word-break: break-all;
}
.wsd-section {
  margin-bottom: 12px;
}
.wsd-label {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
}
.wsd-select,
.wsd-input {
  width: 100%;
  box-sizing: border-box;
  margin-bottom: 8px;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  font-size: 13px;
}
.wsd-rebind {
  padding: 6px 12px;
  border: 1px solid var(--color-primary);
  border-radius: 8px;
  background: var(--color-primary);
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}
.wsd-rebind:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.wsd-notice {
  font-size: 12px;
  color: var(--color-primary);
  margin: 8px 0 0;
}
.wsd-history {
  list-style: none;
  margin: 0;
  padding: 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.wsd-history li {
  padding: 4px 0;
  border-bottom: 1px dashed var(--color-border);
}
.wsd-history__when {
  margin-right: 8px;
}
</style>
