<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 任务管理中心（2026-11-15 任务管理中心 · S1/S9/S10/S5/S6 外壳）。
 *
 * 左树 项目 → 任务（含「会话工作区」自动任务折叠组）；右详情 = 项目详情（S9）或
 * 任务详情（S1 四 tab）。同一组件用于工作台弹窗（mode=dialog）与系统设置 tab（inline）。
 *
 * 数据面：GET /api/workspace/projects/overview（只读聚合）；写面全部走
 * `@/api/workspace` 的 CRUD/CAS 端点；删除两段式（archive/purge + 会话处理单选）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  apiErrorMessage,
  deleteProject,
  deleteTask,
  fetchProjectsOverview,
  fetchTrash,
  previewProjectDelete,
  previewTaskDelete,
  restoreProject,
  restoreTask,
  type DeleteOutcome,
  type DeletePreview,
  type TrashRow,
  type WorkspaceProjectOverview,
  type WorkspaceTaskOverview,
} from '@/api/workspace'
import { useChatStore } from '@/stores/chat'
import { useAssistantStore } from '@/stores/assistant'
import { useGroupStore } from '@/stores/groups'
import { useWorkspaceScopeStore } from '@/stores/workspaceScope'
import DeleteConfirmDialog from './workspace/DeleteConfirmDialog.vue'
import ProjectDetailPane from './workspace/ProjectDetailPane.vue'
import ProjectFormDialog from './workspace/ProjectFormDialog.vue'
import TaskDetailPane from './workspace/TaskDetailPane.vue'
import TaskFormDialog from './workspace/TaskFormDialog.vue'

const props = withDefaults(defineProps<{ mode?: 'dialog' | 'inline' }>(), { mode: 'dialog' })
const emit = defineEmits<{ close: [] }>()

const router = useRouter()
const chatStore = useChatStore()
const assistantStore = useAssistantStore()
const groupStore = useGroupStore()
const scope = useWorkspaceScopeStore()

const statusLabel: Record<string, string> = {
  active: '进行中',
  completed: '已完成',
  archived: '已归档',
  deleted: '已删除',
}

// ---- 数据 ----
const projects = ref<WorkspaceProjectOverview[]>([])
const loading = ref(true)
const error = ref('')
const expanded = ref<Set<string>>(new Set())
const autoExpanded = ref<Set<string>>(new Set())
const selectedProjectId = ref('')
const selectedTaskId = ref('')
const search = ref('')
const menuProjectId = ref('')
// 窄容器（移动端 / 极窄窗口）→ 两级导航：列表 ↔ 详情 + 返回。
// 内嵌设置页也走同一条判定：卡片在「项目管理」tab 已放宽到 1080px，够放双栏，
// 再强制两级导航只会白白吃掉一屏高度（用户反馈的「底部显示不全」正是这么来的）。
const isCompact = ref(false)
const compactView = ref<'list' | 'detail'>('list')
const toast = ref('')
let toastTimer: ReturnType<typeof setTimeout> | null = null

// ---- 对话框 ----
const taskForm = ref<{ visible: boolean; mode: 'create' | 'edit'; projectId: string; task: any | null }>({
  visible: false, mode: 'create', projectId: '', task: null,
})
const projectForm = ref<{ visible: boolean; mode: 'create' | 'edit'; project: WorkspaceProjectOverview | null }>({
  visible: false, mode: 'create', project: null,
})
const deleteDialog = ref<{
  visible: boolean
  kind: 'task' | 'project'
  target: { id: string; name: string; row_version?: number; is_default?: boolean } | null
  preview: DeletePreview | null
  previewLoading: boolean
}>({ visible: false, kind: 'task', target: null, preview: null, previewLoading: false })

// ---- 回收站 ----
const trashOpen = ref(false)
const trash = ref<{ projects: TrashRow[]; tasks: TrashRow[] }>({ projects: [], tasks: [] })
const trashLoading = ref(false)

let overviewAbort: AbortController | null = null
let overviewSeq = 0

function showToast(message: string) {
  toast.value = message
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}

async function loadOverview(silent = false) {
  const seq = ++overviewSeq
  overviewAbort?.abort()
  overviewAbort = new AbortController()
  if (!silent) loading.value = true
  error.value = ''
  try {
    projects.value = await fetchProjectsOverview(overviewAbort.signal)
    // 静默后台重载不得抢占用户当前视图：自动选中首项目 + 重置 expanded 只在显式加载时做
    // （否则桌面 backToList 清选中后，一次后台 bump 会偷偷重选 projects[0] 并收起手动展开的树）。
    if (!silent && !selectedProjectId.value && projects.value.length) {
      const first = projects.value[0]
      expanded.value = new Set([first.id])
      selectedProjectId.value = first.id
    }
    // 选中任务若已不在树中 → 回落到项目详情
    if (selectedTaskId.value) {
      const still = projects.value.some((p) => p.tasks.some((t) => t.id === selectedTaskId.value))
      if (!still) selectedTaskId.value = ''
    }
    if (selectedProjectId.value && !projects.value.some((p) => p.id === selectedProjectId.value)) {
      selectedProjectId.value = projects.value[0]?.id || ''
      selectedTaskId.value = ''
    }
  } catch (err: any) {
    if (err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED') return
    error.value = err?.response?.status ? `加载失败（${err.response.status}）` : '加载失败，请重试'
  } finally {
    if (seq === overviewSeq) loading.value = false
  }
}

// ---- 树 ----
const filteredProjects = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return projects.value
  return projects.value
    .map((p) => {
      const projectHit = p.name.toLowerCase().includes(q)
      const tasks = projectHit ? p.tasks : p.tasks.filter((t) => (t.title || '').toLowerCase().includes(q))
      return projectHit || tasks.length ? { ...p, tasks } : null
    })
    .filter(Boolean) as WorkspaceProjectOverview[]
})

function manualTasks(p: WorkspaceProjectOverview) {
  return p.tasks.filter((t) => t.origin !== 'auto')
}
function autoTasks(p: WorkspaceProjectOverview) {
  return p.tasks.filter((t) => t.origin === 'auto')
}

const selectedProject = computed(() => projects.value.find((p) => p.id === selectedProjectId.value) || null)
const selectedTask = computed(() => {
  if (!selectedTaskId.value) return null
  for (const p of projects.value) {
    const t = p.tasks.find((x) => x.id === selectedTaskId.value)
    if (t) return t
  }
  return null
})
const selectedTaskProjectName = computed(() => {
  if (!selectedTaskId.value) return ''
  const p = projects.value.find((x) => x.tasks.some((t) => t.id === selectedTaskId.value))
  return p?.name || ''
})
// 「本会话产出」只对**当前会话真正绑定的任务**开放（2026-11-17 评审 Minor-4）：
// 否则从笔记页/切换助手后残留的 currentConversationId 会把一段无关会话的产出
// 显示在别的任务下（跨任务视图的语义仅限「改绑后仍可见自己的产出」）。
const currentConversationIdForSelectedTask = computed(() => {
  const conv = chatStore.currentConversationId
  const task = selectedTask.value
  if (!conv || !task) return undefined
  return (task.conversations || []).some((c) => c.id === conv) ? conv : undefined
})

const totalTasks = computed(() => projects.value.reduce((n, p) => n + p.tasks.length, 0))

function toggleProject(id: string) {
  const next = new Set(expanded.value)
  if (next.has(id)) {
    next.delete(id)
    expanded.value = next
    menuProjectId.value = ''
    return
  }
  next.add(id)
  expanded.value = next
  // A2（issue1）：展开项目时一并展开「会话工作区」自动组，让会话绑定的任务直接可见可点。
  autoExpanded.value = new Set([...autoExpanded.value, id])
  menuProjectId.value = ''
}

function toggleAuto(id: string) {
  const next = new Set(autoExpanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  autoExpanded.value = next
}

function selectProject(id: string) {
  trashOpen.value = false   // issue6：回收站打开时点 sidebar 任意项要能切走，不锁死回收站
  selectedProjectId.value = id
  selectedTaskId.value = ''
  menuProjectId.value = ''
  if (!expanded.value.has(id)) expanded.value = new Set([...expanded.value, id])
  autoExpanded.value = new Set([...autoExpanded.value, id])
  // A3（issue1/2 根因，2026-11-16 Wave D）：compact 档点项目名=在树里就地展开该项目
  // （会话工作区 + 任务直接可见可点），不再跳详情把整棵树藏掉。项目详情改由 ⋯→「查看详情」进入。
  if (isCompact.value) compactView.value = 'list'
}

function openProjectDetail(id: string) {
  trashOpen.value = false   // issue6
  selectedProjectId.value = id
  selectedTaskId.value = ''
  menuProjectId.value = ''
  if (!expanded.value.has(id)) expanded.value = new Set([...expanded.value, id])
  if (isCompact.value) compactView.value = 'detail'
}

function selectTask(task: WorkspaceTaskOverview) {
  trashOpen.value = false   // issue6：回收站打开时点任务要立即切到任务详情
  selectedTaskId.value = task.id
  selectedProjectId.value = projects.value.find((p) => p.tasks.some((t) => t.id === task.id))?.id || ''
  menuProjectId.value = ''
  if (isCompact.value) compactView.value = 'detail'
}

function backToList() {
  if (trashOpen.value) { trashOpen.value = false; return }
  compactView.value = 'list'
  // 桌面档左树常驻，「返回列表」= 清空选中回到「选择左侧项目或任务」空态（否则面包屑返回是死控件）。
  if (!isCompact.value) { selectedProjectId.value = ''; selectedTaskId.value = '' }
}

function toggleMenu(id: string) {
  menuProjectId.value = menuProjectId.value === id ? '' : id
}

// ---- 项目 CRUD ----
function openCreateProject() {
  projectForm.value = { visible: true, mode: 'create', project: null }
}
function openEditProject(p: WorkspaceProjectOverview) {
  menuProjectId.value = ''
  projectForm.value = { visible: true, mode: 'edit', project: p }
}
function onProjectSaved(saved: any) {
  showToast('项目已保存')
  void loadOverview()
  if (saved?.id) {
    selectedProjectId.value = saved.id
    selectedTaskId.value = ''
    expanded.value = new Set([...expanded.value, saved.id])
  }
}

async function archiveProject(p: WorkspaceProjectOverview) {
  menuProjectId.value = ''
  try {
    await deleteProject(p.id, { mode: 'archive', expected_row_version: p.row_version })
    showToast('项目已归档')
    await loadOverview()
  } catch (e: any) {
    showToast(apiErrorMessage(e, '归档失败'))
  }
}

// issue2：已归档项目的「取消归档」（restore 同时复活其任务）。
async function unarchiveProject(p: WorkspaceProjectOverview) {
  menuProjectId.value = ''
  try {
    await restoreProject(p.id)
    showToast('已取消归档')
    await loadOverview()
  } catch (e: any) {
    showToast(apiErrorMessage(e, '取消归档失败'))
  }
}

async function openDeleteProject(p: WorkspaceProjectOverview) {
  menuProjectId.value = ''
  deleteDialog.value = {
    visible: true, kind: 'project',
    target: { id: p.id, name: p.name, row_version: p.row_version, is_default: p.is_default },
    preview: null, previewLoading: true,
  }
  try {
    deleteDialog.value.preview = await previewProjectDelete(p.id)
  } catch {
    deleteDialog.value.preview = null
  } finally {
    deleteDialog.value.previewLoading = false
  }
}

// ---- 任务 CRUD ----
function openCreateTask(projectId?: string) {
  taskForm.value = {
    visible: true, mode: 'create',
    projectId: projectId || selectedProjectId.value || projects.value[0]?.id || '',
    task: null,
  }
}
function openEditTask(t: WorkspaceTaskOverview) {
  taskForm.value = { visible: true, mode: 'edit', projectId: t.id ? (t as any).project_id || selectedProjectId.value : '', task: t }
}
function onTaskSaved(saved: any) {
  showToast('任务已保存')
  void loadOverview()
  if (saved?.id) {
    selectedTaskId.value = saved.id
    selectedProjectId.value = saved.project_id || selectedProjectId.value
  }
}

async function archiveTask(t: WorkspaceTaskOverview) {
  try {
    await deleteTask(t.id, { mode: 'archive', expected_row_version: t.row_version })
    showToast('任务已归档')
    await loadOverview()
  } catch (e: any) {
    showToast(apiErrorMessage(e, '归档失败'))
  }
}

// issue2：已归档任务的「取消归档」。
async function unarchiveTask(t: WorkspaceTaskOverview) {
  try {
    await restoreTask(t.id)
    showToast('已取消归档')
    await loadOverview()
  } catch (e: any) {
    showToast(apiErrorMessage(e, '取消归档失败'))
  }
}

async function openDeleteTask(t: WorkspaceTaskOverview) {
  deleteDialog.value = {
    visible: true, kind: 'task',
    target: { id: t.id, name: t.title || '未命名任务', row_version: t.row_version },
    preview: null, previewLoading: true,
  }
  try {
    deleteDialog.value.preview = await previewTaskDelete(t.id)
  } catch {
    deleteDialog.value.preview = null
  } finally {
    deleteDialog.value.previewLoading = false
  }
}

function onDeleted(outcome: DeleteOutcome) {
  if (outcome.mode === 'purge') {
    showToast('已彻底删除（回收站保留 7 天）')
    selectedTaskId.value = ''
  } else {
    showToast('已归档')
  }
  void loadOverview()
}

// ---- 回收站 ----
async function openTrash() {
  trashOpen.value = true
  trashLoading.value = true
  try {
    trash.value = await fetchTrash()
  } catch {
    trash.value = { projects: [], tasks: [] }
  } finally {
    trashLoading.value = false
  }
}
async function restore(row: TrashRow) {
  try {
    if (row.kind === 'project') await restoreProject(row.id)
    else await restoreTask(row.id)
    showToast('已恢复')
    await openTrash()
    await loadOverview()
  } catch (e: any) {
    showToast(apiErrorMessage(e, '恢复失败'))
  }
}

// ---- 打开会话 / 笔记 ----
async function openConversation(id: string) {
  try {
    // 先选中以拿到会话归属助手（selectConversation 会填 conversationMeta）。
    await chatStore.selectConversation(id)
    const assistantId = chatStore.conversationMeta[id]?.assistant_id ?? null
    // 跨助手同步：切到该会话所属助手并重载其会话/分组列表后重选。顺序复用
    // utils/openTaskResult.ts —— loadConversations 的网络往返给 Sidebar 的
    // currentAssistantId watcher（它会把 currentConversationId 置 null）充分
    // flush 时间，重选落在置空之后，选中才稳定。
    if (assistantId && assistantId !== assistantStore.currentAssistantId) {
      assistantStore.selectAssistant(assistantId)
      await Promise.all([
        chatStore.loadConversations(assistantId),
        groupStore.loadGroups(assistantId),
      ])
      await chatStore.selectConversation(id)
    }
    // 工作台模式（/zen）不退出：只切会话 + 关弹窗；agent 模式回聊天页并同步 ?conv。
    const inZen = router.currentRoute.value.path.startsWith('/zen')
    if (!inZen) {
      await router.push({ path: '/', query: { conv: id } })
    }
    // 让侧栏/抽屉展开所属分组并滚动到行（普通侧栏点击不走这条，保持原滚动语义）。
    chatStore.requestRevealConversation(id)
    emit('close')
  } catch {
    showToast('打开会话失败')
  }
}
async function openNote(noteId: string, notebookId?: string) {
  try {
    await router.push(notebookId ? `/notes/${notebookId}/${noteId}` : `/notes`)
    emit('close')
  } catch {
    showToast('打开笔记失败')
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (deleteDialog.value.visible) deleteDialog.value.visible = false
  else if (taskForm.value.visible) taskForm.value.visible = false
  else if (projectForm.value.visible) projectForm.value.visible = false
  else if (menuProjectId.value) menuProjectId.value = ''
  else if (props.mode === 'dialog') emit('close')
}

// issue3（2026-11-16 Wave D）：当前会话新出现绑定任务（自动建任务/别处改绑）→ 已打开的
// 项目管理面板自动重载总览，无需整页刷新。silent=后台静默重载，不闪「加载中…」（评审 r2）。
watch(() => scope.overviewRevision, () => { void loadOverview(true) })

let compactMq: MediaQueryList | null = null
function syncCompact() {
  isCompact.value = !!compactMq?.matches
}
onMounted(() => {
  void loadOverview()
  document.addEventListener('keydown', onKeydown)
  // 1024px 以下双栏挤到不可读（树 240px + 详情正文换行成灾），才降级为两级导航
  compactMq = window.matchMedia('(max-width: 1023px)')
  syncCompact()
  compactMq.addEventListener('change', syncCompact)
})
onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  compactMq?.removeEventListener('change', syncCompact)
  overviewAbort?.abort()
  if (toastTimer) clearTimeout(toastTimer)
})
</script>

<template>
  <Teleport to="body" :disabled="mode === 'inline'">
    <div :class="mode === 'dialog' ? 'wtm-mask' : 'wtm-inline-host'"
         @mousedown.self="mode === 'dialog' && emit('close')">
      <div :class="mode === 'dialog' ? 'wtm-card wpm-card' : 'wtm-inline wpm-card'"
           data-testid="workspace-projects-dialog"
           :role="mode === 'dialog' ? 'dialog' : 'region'"
           :aria-modal="mode === 'dialog' ? 'true' : undefined" aria-label="任务管理">
        <header class="wtm-head">
          <span class="wtm-head__title">任务管理</span>
          <span class="wtm-chip wtm-chip--lt">{{ projects.length }} 个项目 · {{ totalTasks }} 个任务</span>
          <div class="wtm-head__actions">
            <button class="wtm-btn wtm-btn--sm" @click="trashOpen ? (trashOpen = false) : openTrash()">
              {{ trashOpen ? '返回' : '回收站' }}
            </button>
            <button v-if="mode === 'dialog'" class="wtm-icon-btn" aria-label="关闭" @click="emit('close')">✕</button>
          </div>
        </header>

        <div class="wtm-body" :class="{ 'wtm-body--inline': mode === 'inline', 'is-compact': isCompact }">
          <!-- 左树 -->
          <aside class="wtm-tree" v-show="!isCompact || compactView === 'list'">
            <div class="wtm-tree__head">
              <b>项目</b>
              <button class="wtm-btn wtm-btn--sm wtm-btn--primary" @click="openCreateProject">+ 新建项目</button>
            </div>
            <div class="wtm-tree__search">
              <input v-model="search" class="wtm-input" placeholder="搜索项目 / 任务…" />
            </div>
            <div class="wtm-tree__scroll">
              <div v-if="loading" class="wtm-status">加载中…</div>
              <div v-else-if="error" class="wtm-status wtm-status--err">{{ error }}</div>
              <div v-else-if="!filteredProjects.length" class="wtm-status">
                {{ search ? '没有匹配的项目 / 任务' : '暂无项目' }}
              </div>
              <template v-else>
                <section v-for="p in filteredProjects" :key="p.id" class="wtm-project">
                  <div class="wtm-prow" :class="{ 'is-sel': p.id === selectedProjectId && !selectedTaskId }"
                       role="button" tabindex="0" :aria-pressed="p.id === selectedProjectId && !selectedTaskId"
                       @click="selectProject(p.id)"
                       @keydown.enter="selectProject(p.id)" @keydown.space.prevent="selectProject(p.id)">
                    <button class="wtm-caret" @click.stop="toggleProject(p.id)">{{ expanded.has(p.id) ? '▾' : '▸' }}</button>
                    <span class="wtm-prow__name" data-testid="wpm-project-toggle">{{ p.name }}</span>
                    <span v-if="p.is_default" class="wtm-chip wtm-chip--dash">系统保留</span>
                    <span v-if="p.status === 'archived'" class="wtm-chip wtm-chip--archived">已归档</span>
                    <span class="wtm-prow__count">{{ p.tasks.length }}</span>
                    <button class="wtm-more" title="项目操作" aria-label="项目操作" @click.stop="toggleMenu(p.id)">⋯</button>
                  </div>
                  <div v-if="menuProjectId === p.id" class="wtm-moremenu">
                    <button data-testid="wpm-project-detail" @click="openProjectDetail(p.id)">查看详情</button>
                    <button @click="openEditProject(p)">编辑项目…</button>
                    <button v-if="p.status === 'archived'" @click="unarchiveProject(p)">取消归档</button>
                    <button v-else :disabled="p.is_default" @click="archiveProject(p)">归档项目</button>
                    <button class="danger" :disabled="p.is_default" @click="openDeleteProject(p)">删除项目…</button>
                  </div>

                  <template v-if="expanded.has(p.id)">
                    <div v-for="t in manualTasks(p)" :key="t.id" class="wtm-trow"
                         data-testid="wpm-task-row"
                         :class="{ 'is-sel': t.id === selectedTaskId }" @click="selectTask(t)">
                      <span class="wtm-dot" :class="{ 'is-on': t.status === 'active', 'is-done': t.status === 'completed', 'is-arch': t.status === 'archived' }"></span>
                      <span class="wtm-trow__title">{{ t.title || '未命名任务' }}</span>
                      <span class="wtm-trow__meta">{{ t.conversations?.length || 0 }} 会话</span>
                    </div>
                    <div v-if="autoTasks(p).length" class="wtm-prow wtm-prow--auto wtm-prow--toggle"
                         role="button" tabindex="0" :aria-expanded="autoExpanded.has(p.id)"
                         data-testid="wpm-auto-group"
                         @click="toggleAuto(p.id)"
                         @keydown.enter="toggleAuto(p.id)" @keydown.space.prevent="toggleAuto(p.id)">
                      <span class="wtm-caret wtm-caret--static">{{ autoExpanded.has(p.id) ? '▾' : '▸' }}</span>
                      <span class="wtm-prow__name">会话工作区</span>
                      <span class="wtm-chip wtm-chip--dash">自动创建 {{ autoTasks(p).length }}</span>
                    </div>
                    <template v-if="autoExpanded.has(p.id)">
                      <div v-for="t in autoTasks(p)" :key="t.id" class="wtm-trow wtm-trow--auto"
                           data-testid="wpm-task-row"
                           :class="{ 'is-sel': t.id === selectedTaskId }" @click="selectTask(t)">
                        <span class="wtm-dot" :class="{ 'is-on': t.status === 'active', 'is-done': t.status === 'completed', 'is-arch': t.status === 'archived' }"></span>
                        <span class="wtm-trow__title">{{ t.title || '未命名任务' }}</span>
                        <span class="wtm-trow__meta">{{ t.conversations?.length || 0 }} 会话</span>
                      </div>
                    </template>
                  </template>
                </section>
              </template>
            </div>
            <div class="wtm-tree__foot">
              <div class="wtm-trow wtm-trow--muted"><span class="wtm-dot"></span> 未归类会话</div>
              <div class="wtm-trow wtm-trow--muted"><span class="wtm-dot"></span> 未归类笔记</div>
            </div>
          </aside>

          <!-- 右详情 -->
          <div class="wtm-detail-wrap" v-show="!isCompact || compactView === 'detail'">
            <div v-if="isCompact" class="wtm-backbar">
              <button class="wtm-btn wtm-btn--sm" @click="backToList">← 返回列表</button>
            </div>
            <!-- 回收站 -->
            <div v-if="trashOpen" class="wtm-trash">
              <div class="wtm-trash__head">
                <b>回收站</b><span class="wtm-muted">彻底删除的对象保留 7 天，可恢复。</span>
              </div>
              <div v-if="trashLoading" class="wtm-status">加载中…</div>
              <template v-else>
                <div v-if="!trash.projects.length && !trash.tasks.length" class="wtm-empty">
                  <b>回收站是空的</b><span>归档的对象不在这里（归档可随时恢复）。</span>
                </div>
                <div v-else class="wtm-trash__list">
                  <div v-for="row in [...trash.projects, ...trash.tasks]" :key="`${row.kind}-${row.id}`" class="wtm-crow">
                    <span class="wtm-chip wtm-chip--dash">{{ row.kind === 'project' ? '项目' : '任务' }}</span>
                    <span class="wtm-crow__t">{{ row.name || '(未命名)' }}</span>
                    <span class="wtm-crow__m">{{ row.expires_at ? `到期 ${row.expires_at.slice(0, 10)}` : '' }}</span>
                    <span class="wtm-crow__sp">
                      <button class="wtm-btn wtm-btn--sm wtm-btn--primary" @click="restore(row)">恢复</button>
                    </span>
                  </div>
                </div>
              </template>
            </div>

            <!-- 任务详情 -->
            <TaskDetailPane v-else-if="selectedTask" :key="selectedTask.id"
                            :task="selectedTask" :project-name="selectedTaskProjectName"
                            :projects="projects"
                            :current-conversation-id="currentConversationIdForSelectedTask"
                            @edit="openEditTask(selectedTask!)"
                            @delete="openDeleteTask(selectedTask!)"
                            @archive="archiveTask(selectedTask!)"
                            @unarchive="unarchiveTask(selectedTask!)"
                            @refresh="loadOverview"
                            @open-conversation="openConversation"
                            @open-note="(nid) => openNote(nid)"
                            @back-to-project="openProjectDetail(selectedProjectId)"
                            @toast="showToast" />

            <!-- 项目详情 -->
            <ProjectDetailPane v-else-if="selectedProject" :key="selectedProject.id"
                               :project="selectedProject" :projects="projects"
                               @edit="openEditProject(selectedProject!)"
                               @delete="openDeleteProject(selectedProject!)"
                               @archive="archiveProject(selectedProject!)"
                               @unarchive="unarchiveProject(selectedProject!)"
                               @refresh="loadOverview"
                               @create-task="openCreateTask(selectedProject!.id)"
                               @open-task="(tid) => { const t = selectedProject?.tasks.find(x => x.id === tid); if (t) selectTask(t) }"
                               @back-to-list="backToList"
                               @toast="showToast" />

            <!-- 空状态 -->
            <div v-else class="wtm-empty wtm-empty--center">
              <b>选择左侧项目或任务</b><span>项目是容器，任务是最底层可管理对象。</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>

  <!-- 对话框 -->
  <TaskFormDialog :visible="taskForm.visible" :mode="taskForm.mode" :projects="projects"
                  :default-project-id="taskForm.projectId" :task="taskForm.task"
                  @close="taskForm.visible = false" @saved="onTaskSaved" />
  <ProjectFormDialog :visible="projectForm.visible" :mode="projectForm.mode" :project="projectForm.project"
                     @close="projectForm.visible = false" @saved="onProjectSaved" />
  <DeleteConfirmDialog :visible="deleteDialog.visible" :kind="deleteDialog.kind"
                       :target="deleteDialog.target" :preview="deleteDialog.preview"
                       :preview-loading="deleteDialog.previewLoading"
                       @close="deleteDialog.visible = false" @deleted="onDeleted" />

  <!-- toast -->
  <Teleport to="body">
    <div v-if="toast" class="wtm-toast">{{ toast }}</div>
  </Teleport>
</template>

<style scoped>
.wtm-mask {
  position: fixed; inset: 0; z-index: 9998; display: flex; align-items: center;
  justify-content: center; padding: 24px; background: var(--overlay-scrim, rgba(0, 0, 0, .35));
}
.wtm-card {
  width: 100%; max-width: 1180px; height: min(86vh, 820px); display: flex; flex-direction: column;
  background: var(--surface-panel-strong, var(--color-white)); color: var(--color-text);
  border: 1px solid var(--panel-border); border-radius: var(--dialog-radius, 12px);
  box-shadow: var(--dialog-shadow, var(--frame-shadow)); overflow: hidden;
}
.wtm-inline-host { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; overflow: hidden; }
.wtm-inline { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; border: 1px solid var(--panel-border); border-radius: var(--radius-lg, 10px); overflow: hidden; }
.wtm-head {
  display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  border-bottom: 1px solid var(--panel-border); background: var(--surface-panel-subtle, var(--color-bg-secondary));
}
.wtm-head__title { font-weight: 800; font-size: 14px; letter-spacing: .02em; }
.wtm-head__actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.wtm-body { flex: 1; min-height: 0; display: grid; grid-template-columns: 300px 1fr; }
.wtm-body--inline { grid-template-columns: 240px 1fr; }
.wtm-backbar { padding: 8px 12px; border-bottom: 1px solid var(--panel-border); background: var(--surface-panel-subtle, var(--color-bg-secondary)); }
.wtm-body.is-compact { grid-template-columns: 1fr; }
.wtm-body.is-compact .wtm-tree { border-right: 0; }
.wtm-tree { border-right: 1px solid var(--panel-border); display: flex; flex-direction: column; min-height: 0; background: var(--surface-panel-subtle, var(--color-bg-secondary)); }
.wtm-tree__head { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border-bottom: 1px solid var(--panel-border); }
.wtm-tree__head b { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }
.wtm-tree__search { padding: 8px 8px 0; }
.wtm-tree__scroll { padding: 8px; overflow: auto; flex: 1; }
.wtm-tree__foot { border-top: 1px solid var(--panel-border); padding: 8px; }
.wtm-project { margin-bottom: 2px; }
.wtm-prow { display: flex; align-items: center; gap: 6px; padding: 6px 8px; font-weight: 700; font-size: 13px; border-radius: var(--radius-sm, 6px); cursor: pointer; user-select: none; }
.wtm-prow:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-prow.is-sel { background: var(--color-primary); color: var(--color-white); }
.wtm-prow.is-sel .wtm-prow__count { color: var(--color-white); }
.wtm-prow--auto { padding-left: 20px; font-weight: 600; color: var(--color-text-light); }
.wtm-prow--toggle { cursor: pointer; user-select: none; }
.wtm-prow--toggle:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-caret--static { display: inline-block; width: 16px; text-align: center; }
.wtm-caret { border: 0; background: transparent; cursor: pointer; font-size: 10px; width: 16px; color: inherit; padding: 0; }
.wtm-prow__name { border: 0; background: transparent; cursor: inherit; font: inherit; color: inherit; text-align: left; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1 1 auto; }
.wtm-prow__count { margin-left: auto; font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 11px; color: var(--color-text-light); }
.wtm-more { width: 22px; height: 22px; border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white)); cursor: pointer; font-weight: 900; line-height: 1; padding: 0; border-radius: 4px; color: var(--color-text); }
.wtm-more:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-moremenu { margin: 2px 0 8px 22px; border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white)); border-radius: var(--radius-sm, 6px); display: flex; flex-direction: column; overflow: hidden; }
.wtm-moremenu button { border: 0; border-bottom: 1px solid var(--panel-border); background: transparent; text-align: left; padding: 6px 10px; font-size: 12.5px; font-weight: 700; cursor: pointer; font-family: inherit; color: var(--color-text); }
.wtm-moremenu button:last-child { border-bottom: 0; }
.wtm-moremenu button:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-moremenu button:disabled { opacity: .45; cursor: not-allowed; }
.wtm-moremenu button.danger { color: var(--color-danger); }
.wtm-trow { display: flex; align-items: center; gap: 7px; padding: 5px 8px 5px 26px; font-size: 13px; cursor: pointer; border-radius: var(--radius-sm, 6px); }
.wtm-trow:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-trow.is-sel { background: var(--color-primary); color: var(--color-white); }
.wtm-trow.is-sel .wtm-trow__meta { color: var(--color-white); }
.wtm-trow--auto { padding-left: 40px; }
.wtm-trow--muted { color: var(--color-text-light); cursor: default; }
.wtm-trow__title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wtm-trow__meta { margin-left: auto; font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 10.5px; color: var(--color-text-light); }
.wtm-dot { display: inline-block; width: 8px; height: 8px; border: 1px solid var(--panel-border); background: var(--color-text-light); border-radius: 50%; flex: none; }
.wtm-dot.is-on { background: var(--color-success); border-color: var(--color-success); }
.wtm-dot.is-done { background: var(--color-text); border-color: var(--color-text); }
.wtm-dot.is-arch { background: var(--color-text-light); border-color: var(--color-text-light); }
.wtm-detail-wrap { min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.wtm-trash { padding: 16px 18px; overflow: auto; }
.wtm-trash__head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }
.wtm-trash__head b { font-size: 16px; }
.wtm-trash__list { display: flex; flex-direction: column; gap: 8px; }
.wtm-crow { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border: 1px solid var(--panel-border); border-radius: var(--radius-sm, 6px); background: var(--surface-panel, var(--color-white)); flex-wrap: wrap; }
.wtm-crow__t { font-weight: 700; font-size: 13.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wtm-crow__m { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 11px; color: var(--color-text-light); }
.wtm-crow__sp { margin-left: auto; display: flex; gap: 6px; }
.wtm-status { padding: 20px; text-align: center; color: var(--color-text-light); }
.wtm-status--err { color: var(--color-error); }
.wtm-empty { border: 1px dashed var(--panel-border); padding: 26px; text-align: center; color: var(--color-text-light); font-size: 13px; border-radius: var(--radius-sm, 6px); margin: 16px; }
.wtm-empty b { display: block; color: var(--color-text); font-size: 15px; margin-bottom: 4px; }
.wtm-empty--center { margin: auto; max-width: 360px; }
.wtm-muted { color: var(--color-text-light); font-size: 12.5px; }
.wtm-input { width: 100%; border: 1px solid var(--panel-border); background: var(--surface-input, var(--color-white)); color: var(--color-text); padding: 7px 10px; font-size: 13px; border-radius: var(--radius-sm, 6px); font-family: inherit; }
.wtm-btn {
  position: relative; display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px;
  border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
  color: var(--color-text); font-weight: 700; font-size: 13px; cursor: pointer;
  border-radius: var(--radius-sm, 6px); font-family: inherit;
  transition-property: background-color, border-color, color, transform; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.wtm-btn:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-btn:active:not(:disabled) { transform: scale(.96); }
.wtm-btn:disabled { opacity: .5; cursor: not-allowed; }
.wtm-btn--primary { background: var(--color-primary); color: var(--color-white); border-color: var(--color-primary); }
.wtm-btn--sm { padding: 3px 8px; font-size: 12px; }
.wtm-icon-btn {
  position: relative; width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
  color: var(--color-text); cursor: pointer; border-radius: var(--radius-sm, 6px); font-size: 12px;
  transition-property: background-color, border-color, color, transform; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.wtm-icon-btn:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-icon-btn:active { transform: scale(.96); }
/* 小控件视觉尺寸不动，命中区撑到 40×40（触控与粗指针的最低标准） */
.wtm-btn--sm, .wtm-icon-btn, .wtm-more, .wtm-caret { position: relative; }
.wtm-btn--sm::after, .wtm-icon-btn::after, .wtm-more::after, .wtm-caret::after {
  content: ''; position: absolute; top: 50%; left: 50%;
  width: max(100%, 40px); height: max(100%, 40px); transform: translate(-50%, -50%);
}
.wtm-chip { display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border: 1px solid var(--panel-border); font-size: 11.5px; font-weight: 700; border-radius: 999px; background: var(--surface-panel, var(--color-white)); white-space: nowrap; font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1; }
.wtm-chip--lt { background: var(--surface-panel-subtle, var(--color-bg-secondary)); }
.wtm-chip--dash { border-style: dashed; color: var(--color-text-light); background: transparent; }
.wtm-chip--archived { background: var(--color-text-light); color: #fff; border-color: var(--color-text-light); }
.wtm-toast {
  position: fixed; left: 50%; bottom: 40px; transform: translateX(-50%); z-index: 10020;
  background: var(--color-text, #222); color: var(--color-white, #fff); padding: 8px 16px;
  border-radius: 999px; font-size: 13px; box-shadow: var(--frame-shadow);
  animation: wtm-toast-in .22s cubic-bezier(.16, 1, .3, 1) both;
}
@keyframes wtm-toast-in { from { opacity: 0; transform: translate(-50%, 8px); } to { opacity: 1; transform: translate(-50%, 0); } }
@media (prefers-reduced-motion: reduce) { .wtm-toast { animation: none; } }
@media (max-width: 1023px) {
  .wtm-mask { padding: 0; }
  .wtm-card { height: 100%; max-width: none; border-radius: 0; }
  .wtm-body, .wtm-body--inline { grid-template-columns: 1fr; }
  .wtm-tree { border-right: 0; border-bottom: 1px solid var(--panel-border); }
}
@media (max-width: 767px) {
  /* 2026-11-17（真机反馈）：工作台「项目管理」也是全屏弹窗——必须避让系统栏，
     否则卡片头落在状态栏下面、✕ 点不到。 */
  .wtm-mask { padding: var(--safe-top, 0px) 0 var(--safe-bottom, 0px); }
}
</style>
