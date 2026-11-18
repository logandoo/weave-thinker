<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 任务详情（S1 右栏 / S2 文件 / S3 会话 / S4 笔记 / 设置 tab）。
 *
 * 数据面：GET /api/workspace/tasks/{id}/files|conversations|notes + /api/files/list；
 * 写面：PATCH /api/workspace/tasks/{id}（CAS）、POST .../conversations、POST /api/notes/{id}/task-binding。
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  apiErrorMessage,
  bindNoteToTask,
  bindNotesToTask,
  createConversationInTask,
  fetchTaskConversations,
  fetchTaskFiles,
  fetchTaskNotes,
  moveTasks,
  setNoteTaskBinding,
  updateTask,
  type TaskConversationRow,
  type TaskFileEntry,
  type TaskNoteRow,
  type WorkspaceProjectOverview,
  type WorkspaceTaskOverview,
} from '@/api/workspace'
import { fetchDirectory, formatMtime, type DirectoryListing, type WorkspaceEntry } from '@/api/workspaceFiles'
import { classifyFile, fileIcon, fileTypeLabel, formatSize } from '@/composables/filePreview'
import { downloadUrl } from '@/composables/useDownload'
import { buildDownloadUrl } from '@/api/workspaceFiles'
import ConversationRebindDialog from './ConversationRebindDialog.vue'
import FilePreviewDialog from '../FilePreviewDialog.vue'
import NoteBindPickerDialog from './NoteBindPickerDialog.vue'
import { useNotesStore } from '@/stores/notes'

/**
 * 内嵌预览层级（2026-11-15 同族隐患波契约）：本组件可能嵌在设置弹窗（遮罩 10003）
 * 里的项目管理中，预览默认 10000 会被压住 → 显式 10004（高于两处宿主，低于确认框）。
 */
const PREVIEW_Z_INDEX = 10004

const props = defineProps<{
  task: WorkspaceTaskOverview
  projectName: string
  currentConversationId?: string
  /** 跨项目移动任务用（issue1）：可移动到的项目列表。 */
  projects?: WorkspaceProjectOverview[]
}>()

const emit = defineEmits<{
  edit: []
  delete: []
  archive: []
  unarchive: []
  refresh: []
  openConversation: [id: string]
  openNote: [id: string]
  toast: [message: string]
  backToProject: []
}>()

const isArchived = computed(() => props.task.status === 'archived')

type Tab = 'files' | 'convs' | 'notes' | 'settings'
const tab = ref<Tab>('files')

const statusLabel: Record<string, string> = {
  active: '进行中',
  completed: '已完成',
  archived: '已归档',
  deleted: '已删除',
}

const notesStore = useNotesStore()

// ---- 文件 ----
const fileMode = ref<'all' | 'session'>('all')
const currentPath = ref('')
const listing = ref<DirectoryListing | null>(null)
const produced = ref<TaskFileEntry[]>([])
const fileLoading = ref(false)
const fileError = ref('')
const previewing = ref<WorkspaceEntry | null>(null)
// 「本会话产出」条目的预览（2026-11-17）：产出是跨任务的，路径必须按条目自带
// 的 display_root 解析（见 previewFilePath），否则改绑后会预览到同名另一文件。
const previewingProduced = ref<TaskFileEntry | null>(null)
let fileSeq = 0

/**
 * 浏览历史（2026-11-16 Wave B）：文件树原先只有面包屑，进两层就出不来——
 * 后退/前进/上一级是目录浏览的基本动作，这里做一个有界历史栈（上限 50）。
 */
const MAX_FILE_HISTORY = 50
const fileHistory = ref<string[]>([])
const historyIndex = ref(-1)

const rootPath = computed(() => props.task.display_root)
const entries = computed(() => listing.value?.entries ?? [])
const segments = computed(() => {
  const base = rootPath.value
  if (!base || !currentPath.value.startsWith(base)) return []
  const rest = currentPath.value.slice(base.length)
  const parts = rest.split('/').filter(Boolean)
  const out: { name: string; path: string }[] = []
  let acc = base
  for (const part of parts) {
    acc = `${acc}/${part}`
    out.push({ name: part, path: acc })
  }
  return out
})
/**
 * 用户态位置串（2026-11-17）：绝不渲染 `projects/p_…/tasks/t_…/workspace` 这种
 * 存储实现路径——用户只该看到「任务文件 / artifacts / 报告」。保留 `.wtm-mono`
 * 测试缝（既有 E2E 用前后串差异判定后退/前进）。
 */
const displayPath = computed(() => {
  const names = segments.value.map((s) => s.name)
  return ['任务文件', ...names].join(' / ')
})
// 存储位置只读展示（真实继承值；设备权威不显示内部 id）
const storageLabel = computed(() => {
  const raw = String(props.task.storage_authority || 'cloud')
  if (raw === 'cloud') return '云端'
  if (raw.startsWith('device:')) return '设备本地'
  return raw
})
const atRoot = computed(() => !currentPath.value || currentPath.value === rootPath.value)
const parentPath = computed(() => {
  if (atRoot.value) return rootPath.value
  const trimmed = currentPath.value.replace(/\/+$/, '')
  const cut = trimmed.lastIndexOf('/')
  const base = rootPath.value
  if (cut <= 0) return base
  const up = trimmed.slice(0, cut)
  return up.length < base.length ? base : up
})
const canBack = computed(() => historyIndex.value > 0)
const canForward = computed(() => historyIndex.value >= 0 && historyIndex.value < fileHistory.value.length - 1)

function resetHistory(path: string) {
  fileHistory.value = [path]
  historyIndex.value = 0
}

function pushHistory(path: string) {
  const cur = fileHistory.value[historyIndex.value]
  if (cur === path) return
  const next = [...fileHistory.value.slice(0, historyIndex.value + 1), path]
  while (next.length > MAX_FILE_HISTORY) next.shift()
  fileHistory.value = next
  historyIndex.value = next.length - 1
}

async function loadAllFiles() {
  const seq = ++fileSeq
  fileLoading.value = true
  fileError.value = ''
  try {
    listing.value = await fetchDirectory(currentPath.value || rootPath.value)
  } catch (e: any) {
    if (seq !== fileSeq) return
    fileError.value = e?.response?.status === 404 ? '目录不存在或无权限' : '加载失败'
  } finally {
    if (seq === fileSeq) fileLoading.value = false
  }
}

async function loadSessionFiles() {
  if (!props.currentConversationId) {
    produced.value = []
    return
  }
  const seq = ++fileSeq
  fileLoading.value = true
  fileError.value = ''
  try {
    const res = await fetchTaskFiles(props.task.id, props.currentConversationId)
    produced.value = res.produced || []
  } catch (e: any) {
    if (seq !== fileSeq) return
    fileError.value = apiErrorMessage(e, '加载失败')
  } finally {
    if (seq === fileSeq) fileLoading.value = false
  }
}

function setFileMode(mode: 'all' | 'session') {
  fileMode.value = mode
  if (mode === 'all') void loadAllFiles()
  else void loadSessionFiles()
}

function navigate(path: string, record = true) {
  if (!path) return
  if (record) pushHistory(path)
  if (path === currentPath.value) return
  currentPath.value = path
  void loadAllFiles()
}

function goBack() {
  if (!canBack.value) return
  historyIndex.value -= 1
  currentPath.value = fileHistory.value[historyIndex.value]
  void loadAllFiles()
}

function goForward() {
  if (!canForward.value) return
  historyIndex.value += 1
  currentPath.value = fileHistory.value[historyIndex.value]
  void loadAllFiles()
}

function goUp() {
  if (atRoot.value) return
  navigate(parentPath.value)
}

function goRoot() {
  navigate(rootPath.value)
}

function refreshListing() {
  if (fileMode.value === 'all') void loadAllFiles()
  else void loadSessionFiles()
}

function onEntryClick(e: WorkspaceEntry) {
  if (e.is_dir) navigate(e.rel_path)
  else previewing.value = e
}

async function downloadEntry(e: WorkspaceEntry) {
  await downloadUrl(buildDownloadUrl(e.rel_path), e.name)
}

// ---- 会话 ----
const conversations = ref<TaskConversationRow[]>([])
const convLoading = ref(false)
const convBusy = ref(false)

async function loadConversations() {
  convLoading.value = true
  try {
    conversations.value = await fetchTaskConversations(props.task.id)
  } catch {
    conversations.value = []
  } finally {
    convLoading.value = false
  }
}

async function newConversation() {
  if (convBusy.value) return
  convBusy.value = true
  try {
    const res = await createConversationInTask(props.task.id)
    emit('toast', '已在任务内新建会话')
    emit('openConversation', res.conversation_id)
    await loadConversations()
    emit('refresh')
  } catch (e: any) {
    emit('toast', apiErrorMessage(e, '新建会话失败'))
  } finally {
    convBusy.value = false
  }
}

// ---- 笔记 ----
const notes = ref<TaskNoteRow[]>([])
const noteLoading = ref(false)
const noteBusy = ref(false)
const pickerOpen = ref(false)
const picker = ref<InstanceType<typeof NoteBindPickerDialog> | null>(null)

async function loadNotes() {
  noteLoading.value = true
  try {
    notes.value = await fetchTaskNotes(props.task.id)
  } catch {
    notes.value = []
  } finally {
    noteLoading.value = false
  }
}

async function unbindNote(id: string) {
  if (noteBusy.value) return
  noteBusy.value = true
  try {
    // 多对多：只解本任务这一条关系，该笔记在其他任务/项目上的绑定保留
    await setNoteTaskBinding(id, props.task.id, 'unbind')
    emit('toast', '已从本任务解绑（笔记未删除）')
    await loadNotes()
    emit('refresh')
  } catch (e: any) {
    emit('toast', apiErrorMessage(e, '解绑失败'))
  } finally {
    noteBusy.value = false
  }
}

async function createAndBindNote() {
  if (noteBusy.value) return
  noteBusy.value = true
  try {
    if (!notesStore.defaultNotebook) await notesStore.loadDefaultNotebook()
    const nb = notesStore.defaultNotebook
    if (!nb) throw new Error('no default notebook')
    const created = await notesStore.createNote(nb.id, { title: '任务笔记', content: '' })
    await bindNoteToTask(created.id, props.task.id)
    emit('toast', '已新建并绑定笔记')
    await loadNotes()
    emit('refresh')
  } catch (e: any) {
    emit('toast', apiErrorMessage(e, '新建笔记失败'))
  } finally {
    noteBusy.value = false
  }
}

async function onPickerConfirm(noteIds: string[]) {
  if (!noteIds.length) { picker.value?.doneBinding(); return }
  noteBusy.value = true
  try {
    const out = await bindNotesToTask(props.task.id, noteIds)
    const failed = out.failed?.length || 0
    const parts = []
    if (out.bound.length) parts.push(`已绑定 ${out.bound.length} 条`)
    if (out.already_bound.length) parts.push(`${out.already_bound.length} 条原本已绑`)
    if (failed) parts.push(`${failed} 条无权访问`)
    emit('toast', parts.join(' · ') || '没有变化')
    picker.value?.doneBinding()
    pickerOpen.value = false
    await loadNotes()
    emit('refresh')
  } catch (e: any) {
    emit('toast', apiErrorMessage(e, '绑定失败'))
  } finally {
    noteBusy.value = false
    picker.value?.doneBinding()
  }
}

// ---- 改绑会话（2026-11-16 Wave B：任务管理中心内直接改绑，不再踢回会话侧抽屉）----
const rebindTarget = ref<TaskConversationRow | null>(null)

function openRebind(c: TaskConversationRow) {
  rebindTarget.value = c
}

async function onRebound() {
  await loadConversations()
  emit('refresh')
}

// ---- 设置（内联编辑）----
const editTitle = ref('')
const editDescription = ref('')
const editStatus = ref('active')
const saveBusy = ref(false)
const saveError = ref('')

function syncSettings() {
  editTitle.value = props.task.title || ''
  editDescription.value = props.task.description || ''
  editStatus.value = props.task.status || 'active'
}

async function saveSettings() {
  if (saveBusy.value) return
  saveBusy.value = true
  saveError.value = ''
  try {
    await updateTask(props.task.id, {
      title: editTitle.value.trim() || undefined,
      description: editDescription.value.trim(),
      status: editStatus.value,
      expected_row_version: props.task.row_version,
    })
    emit('toast', '已保存')
    emit('refresh')
  } catch (e: any) {
    const code = e?.response?.data?.detail?.code
    saveError.value =
      code === 'workspace_task_stale'
        ? '任务已被其他端修改，请刷新后重试'
        : apiErrorMessage(e, '保存失败')
  } finally {
    saveBusy.value = false
  }
}

function loadTab() {
  if (tab.value === 'files') {
    if (fileMode.value === 'session') void loadSessionFiles()
    else void loadAllFiles()
  } else if (tab.value === 'convs') void loadConversations()
  else if (tab.value === 'notes') void loadNotes()
  else if (tab.value === 'settings') syncSettings()
}

// ---- 跨项目移动任务（issue1）----
const moveProjectId = ref('')
const moveBusy = ref(false)
const otherProjects = computed(() =>
  (props.projects || []).filter((p) => p.id !== props.task.project_id && p.status === 'active'))

async function doMove() {
  if (!moveProjectId.value || moveBusy.value) return
  moveBusy.value = true
  try {
    const out = await moveTasks([props.task.id], moveProjectId.value)
    emit('toast', out.moved > 0 ? '已移动到目标项目（含文件目录）' : '任务已在该项目')
    moveProjectId.value = ''
    emit('refresh')
  } catch (e: any) {
    emit('toast', apiErrorMessage(e, '移动失败'))
  } finally {
    moveBusy.value = false
  }
}

watch(tab, loadTab)
watch(
  () => props.task.id,
  () => {
    tab.value = 'files'
    fileMode.value = 'all'
    currentPath.value = props.task.display_root
    resetHistory(props.task.display_root)
    previewing.value = null
    pickerOpen.value = false
    rebindTarget.value = null
    syncSettings()
    void loadAllFiles()
  },
  { immediate: true },
)

onMounted(() => {
  currentPath.value = props.task.display_root
  resetHistory(props.task.display_root)
  syncSettings()
  void loadAllFiles()
})

function entryOf(e: WorkspaceEntry) {
  return e
}
/** produced.rel_path 相对「产出所在任务」workspace 根；display_root 已含 /workspace。 */
function producedPreviewPath(e: TaskFileEntry): string {
  const root = e.display_root || rootPath.value || ''
  return root ? `${root}/${e.rel_path}` : e.rel_path
}
/**
 * 预览标题/路径提示用**任务相对**路径（2026-11-17 评审 Important）：
 * 目录列表条目的 rel_path 是用户根相对（`projects/p_…/tasks/t_…/workspace/…`），
 * 直接喂 FilePreviewDialog 会在 tooltip/未知类型提示里把原始目录又露出来。
 * 可解析路径由 `source-path` 单独承载，这里只负责「给用户看」。
 */
function displayRelPath(e: WorkspaceEntry | TaskFileEntry): string {
  const p = e.rel_path || ''
  const base = rootPath.value || ''
  if (base && p.startsWith(base + '/')) return p.slice(base.length + 1)
  if (base && p === base) return ''
  return p
}
function openProducedPreview(e: TaskFileEntry) {
  previewingProduced.value = e
}
async function downloadProduced(e: TaskFileEntry) {
  await downloadUrl(buildDownloadUrl(producedPreviewPath(e)), e.name)
}
function fileKind(name: string, type?: string) {
  return classifyFile(name, type)
}
function fmtTime(sec: number) {
  return formatMtime(sec)
}
// 跨任务「本会话产出」可能有两任务同名文件（rel_path 相同）→ 组合 key 防 Vue 重复键。
function fileKey(e: TaskFileEntry, i: number): string {
  return e.task_id ? `${e.task_id}:${e.rel_path}` : `${i}:${e.rel_path}`
}
// 条目来自别的任务（改绑后旧任务产出）时显示来源任务名，消歧同名文件。
function originLabel(e: TaskFileEntry): string {
  const cur = props.task?.id || ''
  return e.task_id && e.task_id !== cur ? (e.task_title || '其他任务') : ''
}
</script>

<template>
  <div class="wtm-detail">
    <header class="wtm-detail__head">
      <div class="wtm-crumb">
        <button class="wtm-crumb__link" data-testid="wpm-task-back" :title="`返回项目任务列表：${projectName}`" @click="emit('backToProject')">← {{ projectName }}</button>
        <span> / 任务</span>
      </div>
      <div class="wtm-title-row">
        <h2 class="wtm-title" :title="task.title || '未命名任务'">{{ task.title || '未命名任务' }}</h2>
        <button class="wtm-icon-btn" title="编辑" aria-label="编辑任务" @click="emit('edit')">✎</button>
        <span class="wtm-chip" :class="`wtm-chip--${task.status}`">{{ statusLabel[task.status] || task.status }}</span>
        <span v-if="task.origin === 'auto'" class="wtm-chip wtm-chip--dash">自动创建</span>
        <span v-else-if="task.origin === 'user'" class="wtm-chip wtm-chip--dash">手动创建</span>
        <div class="wtm-title-actions">
          <button class="wtm-btn wtm-btn--sm" @click="newConversation">新建会话</button>
          <button v-if="isArchived" class="wtm-btn wtm-btn--sm" @click="emit('unarchive')">取消归档</button>
          <button v-else class="wtm-btn wtm-btn--sm" @click="emit('archive')">归档</button>
          <button class="wtm-btn wtm-btn--sm wtm-btn--danger" @click="emit('delete')">删除</button>
        </div>
      </div>
      <p v-if="task.description" class="wtm-desc" :title="task.description">{{ task.description }}</p>
      <div class="wtm-meta">
        <span class="wtm-chip wtm-chip--lt wtm-chip--ver">v{{ task.row_version ?? 1 }}</span>
        <span class="wtm-chip wtm-chip--lt">{{ conversations.length }} 会话</span>
        <span class="wtm-chip wtm-chip--lt">{{ notes.length }} 笔记</span>
        <span v-if="task.last_active_at" class="wtm-chip wtm-chip--dash">最近活跃 {{ fmtTime(Date.parse(task.last_active_at) / 1000) }}</span>
      </div>
    </header>

    <div class="wtm-tabs" role="tablist">
      <button class="wtm-tab" :class="{ 'is-on': tab === 'files' }" @click="tab = 'files'">文件</button>
      <button class="wtm-tab" :class="{ 'is-on': tab === 'convs' }" @click="tab = 'convs'">会话</button>
      <button class="wtm-tab" :class="{ 'is-on': tab === 'notes' }" @click="tab = 'notes'">笔记</button>
      <button class="wtm-tab" :class="{ 'is-on': tab === 'settings' }" @click="tab = 'settings'">设置</button>
    </div>

    <div class="wtm-pane">
      <!-- 文件 -->
      <div v-if="tab === 'files'" class="wpm-files">
        <div class="wtm-pane__bar wpm-files-head">
          <div class="wpm-filenav">
            <button class="wtm-icon-btn wtm-icon-btn--nav" title="后退" aria-label="后退"
                    data-testid="wpm-file-back" :disabled="!canBack || fileMode !== 'all'" @click="goBack">←</button>
            <button class="wtm-icon-btn wtm-icon-btn--nav" title="前进" aria-label="前进"
                    data-testid="wpm-file-forward" :disabled="!canForward || fileMode !== 'all'" @click="goForward">→</button>
            <button class="wtm-icon-btn wtm-icon-btn--nav" title="上一级" aria-label="上一级"
                    data-testid="wpm-file-up" :disabled="atRoot || fileMode !== 'all'" @click="goUp">↑</button>
            <button class="wtm-icon-btn wtm-icon-btn--nav" title="回到任务根目录" aria-label="回到任务根目录"
                    data-testid="wpm-file-root" :disabled="atRoot || fileMode !== 'all'" @click="goRoot">⌂</button>
          </div>
          <span class="wtm-mono" data-testid="wpm-file-path">{{ displayPath }}</span>
          <div class="wtm-grow">
            <div class="wtm-seg">
              <button :class="{ 'is-on': fileMode === 'all' }" @click="setFileMode('all')">全部文件</button>
              <button :class="{ 'is-on': fileMode === 'session' }" @click="setFileMode('session')"
                      :disabled="!currentConversationId">本会话产出</button>
            </div>
            <button class="wtm-icon-btn" title="刷新" aria-label="刷新" @click="refreshListing">⟳</button>
          </div>
        </div>
        <div v-if="fileLoading" class="wtm-status">加载中…</div>
        <div v-else-if="fileError" class="wtm-status wtm-status--err">{{ fileError }}</div>
        <template v-else-if="fileMode === 'all'">
          <div v-if="segments.length" class="wtm-seg-path">
            <button class="wtm-link" @click="navigate(rootPath)">任务文件</button>
            <template v-for="s in segments" :key="s.path">
              <span>/</span><button class="wtm-link" @click="navigate(s.path)">{{ s.name }}</button>
            </template>
          </div>
          <div v-if="!entries.length" class="wtm-empty">
            <b>这个任务还没有文件</b>
            <span>上传输入让 agent 开始，或从其他任务 fork 需要的输入。</span>
          </div>
          <div v-else class="wtm-ftable">
            <div class="wtm-frow wtm-frow--head"><span>名称</span><span>大小</span><span>时间</span><span></span></div>
            <div v-for="e in entries" :key="e.rel_path" class="wtm-frow wtm-frow--click"
                 data-testid="wpm-file-entry" @click="onEntryClick(e)">
              <span class="wtm-fname">
                <i class="wtm-ficon">{{ e.is_dir ? 'D' : fileIcon(fileKind(e.name, e.type)) }}</i>
                <span>{{ e.name }}{{ e.is_dir ? '/' : '' }}</span>
              </span>
              <span class="wtm-fsz">{{ e.is_dir ? '—' : formatSize(e.size) }}</span>
              <span class="wtm-ftm">{{ fmtTime(e.mtime) }}</span>
              <span class="wtm-fact">
                <button v-if="e.is_dir" class="wtm-btn wtm-btn--sm" @click.stop="navigate(e.rel_path)">打开</button>
                <template v-else>
                  <button class="wtm-btn wtm-btn--sm" @click.stop="onEntryClick(e)">预览</button>
                  <button class="wtm-btn wtm-btn--sm" @click.stop="downloadEntry(e)">下载</button>
                </template>
              </span>
            </div>
          </div>
        </template>
        <template v-else>
          <div v-if="!currentConversationId" class="wtm-empty">
            <b>没有当前会话</b><span>从会话侧抽屉查看本会话产出。</span>
          </div>
          <div v-else-if="!produced.length" class="wtm-empty">
            <b>本会话还没有产出</b><span>agent 发布的交付文件会出现在这里，点击即可预览。</span>
          </div>
          <div v-else class="wtm-ftable">
            <div class="wtm-frow wtm-frow--head"><span>名称</span><span>大小</span><span>时间</span><span></span></div>
            <div v-for="(e, ei) in produced" :key="fileKey(e, ei)" class="wtm-frow wtm-frow--click"
                 data-testid="wpm-produced-entry"
                 @click="openProducedPreview(e)">
              <span class="wtm-fname">
                <i class="wtm-ficon">{{ fileIcon(fileKind(e.name, e.type)) }}</i>
                <span>{{ e.rel_path }}</span>
                <span v-if="originLabel(e)" class="wtm-forigin" :title="`来自任务：${originLabel(e)}`">↳ {{ originLabel(e) }}</span>
              </span>
              <span class="wtm-fsz">{{ formatSize(e.size) }}</span>
              <span class="wtm-ftm">{{ fmtTime(e.mtime) }}</span>
              <span class="wtm-fact">
                <button class="wtm-btn wtm-btn--sm" @click.stop="openProducedPreview(e)">预览</button>
                <button class="wtm-btn wtm-btn--sm" @click.stop="downloadProduced(e)">下载</button>
              </span>
            </div>
          </div>
        </template>
      </div>

      <!-- 会话 -->
      <div v-else-if="tab === 'convs'">
        <div class="wtm-pane__bar">
          <span class="wtm-muted">同一任务的会话共享文件域；改绑不移动文件、不改写历史来源。</span>
          <div class="wtm-grow">
            <button class="wtm-btn wtm-btn--sm wtm-btn--primary" :disabled="convBusy" @click="newConversation">
              + 在任务内新建会话
            </button>
          </div>
        </div>
        <div v-if="convLoading" class="wtm-status">加载中…</div>
        <div v-else-if="!conversations.length" class="wtm-empty">
          <b>任务内还没有会话</b><span>点上方按钮新建，或从其他任务改绑进来。</span>
        </div>
        <div v-else class="wtm-crow-list">
          <div v-for="c in conversations" :key="c.id" class="wtm-crow" data-testid="wpm-conversation-item">
            <span class="wtm-dot" :class="{ 'is-on': c.id === currentConversationId }"></span>
            <span class="wtm-crow__t">{{ c.title || '新对话' }}</span>
            <span v-if="c.id === currentConversationId" class="wtm-chip wtm-chip--yl">当前</span>
            <span class="wtm-crow__m">{{ fmtTime(Date.parse(c.updated_at || '') / 1000) }}</span>
            <span class="wtm-crow__sp">
              <button class="wtm-btn wtm-btn--sm" @click="emit('openConversation', c.id)">打开</button>
              <button class="wtm-btn wtm-btn--sm" data-testid="wpm-conversation-rebind"
                      @click="openRebind(c)">改绑…</button>
            </span>
          </div>
        </div>
      </div>

      <!-- 笔记 -->
      <div v-else-if="tab === 'notes'">
        <div class="wtm-pane__bar">
          <span class="wtm-muted">笔记是任务成员：绑定/解绑不复制内容，解绑不删除笔记；同一条笔记可以同时属于多个任务。</span>
          <div class="wtm-grow">
            <button class="wtm-btn wtm-btn--sm wtm-btn--primary" :disabled="noteBusy" @click="createAndBindNote">
              + 新建笔记
            </button>
            <button class="wtm-btn wtm-btn--sm" data-testid="wpm-note-bind-open" :disabled="noteBusy"
                    @click="pickerOpen = true">绑定已有笔记…</button>
          </div>
        </div>
        <div v-if="noteLoading" class="wtm-status">加载中…</div>
        <div v-else-if="!notes.length" class="wtm-empty">
          <b>任务还没有笔记</b><span>新建一条，或把已有笔记绑定进来（可一次选多条）。</span>
        </div>
        <div v-else class="wtm-notes">
          <div v-for="n in notes" :key="n.id" class="wtm-note" data-testid="wpm-note-card">
            <h4>{{ n.title || '未命名笔记' }}</h4>
            <p>{{ n.excerpt }}</p>
            <div class="wtm-note__foot">
              <span v-if="(n.bound_task_count || 1) > 1" class="wtm-chip wtm-chip--multi"
                    data-testid="wpm-note-multi-badge">共绑 {{ n.bound_task_count }} 个任务</span>
              <span v-else class="wtm-chip wtm-chip--yl">已绑定</span>
              <button class="wtm-btn wtm-btn--sm" @click="emit('openNote', n.id)">打开</button>
              <button class="wtm-btn wtm-btn--sm" :disabled="noteBusy" @click="unbindNote(n.id)">解绑</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 设置 -->
      <div v-else class="wtm-settings">
        <div class="wtm-field">
          <label>任务名称 <span class="wtm-req">*</span></label>
          <input v-model="editTitle" class="wtm-input" />
        </div>
        <div class="wtm-field">
          <label>备注（agent 可见）</label>
          <textarea v-model="editDescription" class="wtm-textarea"></textarea>
        </div>
        <div class="wtm-field">
          <label>状态</label>
          <select v-model="editStatus" class="wtm-select">
            <option value="active">进行中</option>
            <option value="completed">已完成</option>
            <option value="archived">已归档</option>
          </select>
        </div>
        <div class="wtm-field">
          <label>存储位置</label>
          <input class="wtm-input" :value="storageLabel" disabled />
          <div class="wtm-hint">从项目继承；创建后不可改。</div>
        </div>
        <div v-if="otherProjects.length" class="wtm-field">
          <label>移动到项目</label>
          <div class="wtm-move">
            <select v-model="moveProjectId" class="wtm-select">
              <option value="" disabled>选择目标项目…</option>
              <option v-for="p in otherProjects" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <button class="wtm-btn wtm-btn--sm" :disabled="!moveProjectId || moveBusy" @click="doMove">
              {{ moveBusy ? '移动中…' : '移动' }}
            </button>
          </div>
          <div class="wtm-hint">移动会把任务连同其文件目录一起改挂到目标项目（存储位置需一致）。</div>
        </div>
        <div v-if="saveError" class="wtm-err">{{ saveError }}</div>
        <div class="wtm-settings__foot">
          <button class="wtm-btn wtm-btn--primary" :disabled="saveBusy" @click="saveSettings">
            {{ saveBusy ? '保存中…' : '保存' }}
          </button>
        </div>
        <div class="wtm-danger">
          <h4>危险区</h4>
          <p>归档保留文件可恢复；彻底删除需输入任务名，文件进回收站 7 天。</p>
          <button class="wtm-btn wtm-btn--sm wtm-btn--danger" @click="emit('delete')">删除任务…</button>
        </div>
      </div>
    </div>

    <FilePreviewDialog
      v-if="previewing"
      :filename="previewing.name"
      :url="buildDownloadUrl(previewing.rel_path)"
      :rel-path="displayRelPath(previewing)"
      :source-path="previewing.rel_path"
      :type="previewing.type"
      :conversation-id="currentConversationId"
      :z-index="PREVIEW_Z_INDEX"
      @close="previewing = null"
    />

    <!-- 本会话产出预览（跨任务条目按自身 display_root 解析） -->
    <FilePreviewDialog
      v-if="previewingProduced"
      :filename="previewingProduced.name"
      :url="buildDownloadUrl(producedPreviewPath(previewingProduced), null, currentConversationId || null)"
      :rel-path="displayRelPath(previewingProduced)"
      :source-path="producedPreviewPath(previewingProduced)"
      :type="previewingProduced.type"
      :conversation-id="currentConversationId"
      :z-index="PREVIEW_Z_INDEX"
      @close="previewingProduced = null"
    />

    <NoteBindPickerDialog ref="picker" :visible="pickerOpen"
                          :bound-note-ids="notes.map((n) => n.id)" :task-title="task.title || ''"
                          @close="pickerOpen = false" @confirm="onPickerConfirm" />

    <ConversationRebindDialog :visible="!!rebindTarget"
                              :conversation-id="rebindTarget?.id || ''"
                              :conversation-title="rebindTarget?.title || ''"
                              :current-task-id="task.id" :current-task-title="task.title || ''"
                              @close="rebindTarget = null" @done="onRebound" />
  </div>
</template>

<style scoped>
.wtm-detail { display: flex; flex-direction: column; min-width: 0; min-height: 0; height: 100%; flex: 1 1 auto; }
.wtm-detail__head { padding: 16px 18px 12px; border-bottom: 1px solid var(--panel-border); }
.wtm-crumb { display: flex; align-items: center; gap: 4px; flex-wrap: nowrap; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--color-text-light); margin-bottom: 4px; }
.wtm-crumb__link { border: 0; background: transparent; padding: 0; font: inherit; color: var(--color-primary); cursor: pointer; text-transform: uppercase; letter-spacing: .08em; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wtm-crumb > span { flex: none; }
.wtm-crumb__link:hover { text-decoration: underline; }
.wtm-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.wtm-title { font-size: 20px; font-weight: 800; margin: 0; letter-spacing: -.01em; }
.wtm-title-actions { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; }
.wtm-desc { margin: 6px 0 0; color: var(--color-text-light); font-size: 13px; max-width: 70ch; }
.wtm-meta { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.wtm-tabs { display: flex; border-bottom: 1px solid var(--panel-border); background: var(--surface-panel-subtle, var(--color-bg-secondary)); overflow-x: auto; }
.wtm-tab {
  padding: 9px 16px; font-weight: 700; font-size: 13px; border: 0; border-right: 1px solid var(--panel-border);
  background: transparent; cursor: pointer; color: var(--color-text); font-family: inherit; white-space: nowrap;
}
.wtm-tab:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-tab.is-on { background: var(--color-primary); color: var(--color-white); }
.wtm-pane { padding: 14px 18px; flex: 1; min-height: 0; overflow: auto; }
.wtm-pane__bar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.wtm-grow { margin-left: auto; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.wtm-mono { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.wtm-muted { color: var(--color-text-light); font-size: 12.5px; }
.wtm-seg { display: inline-flex; border: 1px solid var(--panel-border); border-radius: var(--radius-sm, 6px); overflow: hidden; }
.wtm-seg button { border: 0; background: transparent; padding: 4px 10px; font-size: 12px; font-weight: 700; cursor: pointer; color: var(--color-text); font-family: inherit; }
.wtm-seg button.is-on { background: var(--color-primary); color: var(--color-white); }
.wtm-seg button:disabled { opacity: .45; cursor: not-allowed; }
.wtm-seg button + button { border-left: 1px solid var(--panel-border); }
.wtm-seg-path { display: flex; gap: 4px; align-items: center; font-size: 12px; margin-bottom: 8px; flex-wrap: wrap; }
.wpm-filenav { display: inline-flex; gap: 2px; align-items: center; }
.wtm-link { border: 0; background: transparent; color: var(--color-primary); cursor: pointer; font-size: inherit; padding: 0; font-family: inherit; }
.wtm-link:hover { text-decoration: underline; }
.wtm-ftable { border: 1px solid var(--panel-border); border-radius: var(--radius-sm, 6px); overflow: hidden; }
.wtm-frow { display: grid; grid-template-columns: 1fr 90px 120px 130px; align-items: center; gap: 8px; padding: 7px 10px; border-bottom: 1px solid var(--panel-border); font-size: 13px; }
.wtm-frow:last-child { border-bottom: 0; }
.wtm-frow--head { background: var(--surface-panel-subtle, var(--color-bg-secondary)); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; font-weight: 700; color: var(--color-text-light); }
.wtm-fact { text-align: right; display: flex; gap: 6px; justify-content: flex-end; }
.wtm-frow--click { cursor: pointer; }
.wtm-frow--click:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-fname { display: flex; align-items: center; gap: 8px; min-width: 0; }
.wtm-fname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wtm-forigin { flex: none; color: var(--color-text-light); font-size: 11px; }
.wtm-ficon { width: 22px; height: 22px; border: 1px solid var(--panel-border); display: inline-flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 900; background: var(--surface-panel, var(--color-white)); flex: none; border-radius: 4px; }
/* 动态数字一律等宽：文件大小/时间/计数/版本在流式刷新时不得抖动 */
.wtm-fsz, .wtm-ftm, .wtm-crow__m, .wtm-trow__meta, .wtm-prow__count, .wtm-history__when {
  font-variant-numeric: tabular-nums;
  font-feature-settings: 'tnum' 1;
}
.wtm-fsz, .wtm-ftm { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 11.5px; color: var(--color-text-light); }
.wtm-status { padding: 20px; text-align: center; color: var(--color-text-light); }
.wtm-status--err { color: var(--color-error); }
.wtm-empty { border: 1px dashed var(--panel-border); padding: 26px; text-align: center; color: var(--color-text-light); font-size: 13px; border-radius: var(--radius-sm, 6px); }
.wtm-empty b { display: block; color: var(--color-text); font-size: 15px; margin-bottom: 4px; }
.wtm-crow-list { display: flex; flex-direction: column; gap: 8px; }
.wtm-crow { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border: 1px solid var(--panel-border); border-radius: var(--radius-sm, 6px); background: var(--surface-panel, var(--color-white)); flex-wrap: wrap; }
.wtm-crow__t { font-weight: 700; font-size: 13.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wtm-crow__m { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 11px; color: var(--color-text-light); }
.wtm-crow__sp { margin-left: auto; display: flex; gap: 6px; }
.wtm-dot { display: inline-block; width: 8px; height: 8px; border: 1px solid var(--panel-border); background: var(--color-text-light); border-radius: 50%; flex: none; }
.wtm-dot.is-on { background: var(--color-success); border-color: var(--color-success); }
.wtm-notes { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; }
/* 卡片用分层阴影代替硬边框做层次；内层按钮 6px + 10px 内边距 → 外层 12px 保持同心 */
.wtm-note {
  border: 1px solid color-mix(in srgb, var(--panel-border) 60%, transparent);
  background: var(--surface-panel, var(--color-white)); border-radius: 12px; padding: 10px;
  display: flex; flex-direction: column; gap: 6px;
  box-shadow: 0 1px 1px rgba(15, 23, 42, .04), 0 2px 6px rgba(15, 23, 42, .06);
  transition-property: box-shadow, transform; transition-duration: .18s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.wtm-note:hover { box-shadow: 0 1px 2px rgba(15, 23, 42, .05), 0 6px 16px rgba(15, 23, 42, .10); }
.wtm-note h4 { margin: 0; font-size: 13.5px; font-weight: 800; text-wrap: balance; }
.wtm-note p { margin: 0; font-size: 12px; color: var(--color-text-light); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; text-wrap: pretty; }
.wtm-note__foot { display: flex; gap: 6px; align-items: center; margin-top: auto; flex-wrap: wrap; }
.wtm-settings { max-width: 640px; }
.wtm-field { margin-bottom: 14px; }
.wtm-field label { display: block; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; font-weight: 700; margin-bottom: 5px; color: var(--color-text-light); }
.wtm-req { color: var(--color-error); }
.wtm-input, .wtm-textarea, .wtm-select { width: 100%; border: 1px solid var(--panel-border); background: var(--surface-input, var(--color-white)); color: var(--color-text); padding: 8px 10px; font-size: 13.5px; border-radius: var(--radius-sm, 6px); font-family: inherit; }
.wtm-textarea { min-height: 74px; resize: vertical; }
.wtm-hint { font-size: 11.5px; color: var(--color-text-light); margin-top: 4px; }
.wtm-move { display: flex; gap: 6px; align-items: center; }
.wtm-move .wtm-select { flex: 1 1 auto; }
.wtm-settings__foot { display: flex; justify-content: flex-end; margin-bottom: 16px; }
.wtm-danger { border: 1px solid var(--color-danger); background: var(--surface-panel-subtle, #fbe9e7); padding: 12px; border-radius: var(--radius-sm, 6px); }
.wtm-danger h4 { margin: 0 0 4px; font-size: 13px; color: var(--color-danger); }
.wtm-danger p { margin: 0 0 8px; font-size: 12px; color: var(--color-text-light); }
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
.wtm-btn--danger { background: var(--color-danger); color: var(--color-white); border-color: var(--color-danger); }
.wtm-btn--sm { padding: 3px 8px; font-size: 12px; }
/* 视觉可以小，命中区不小于 40×40：伪元素撑开可点范围，不改排版 */
.wtm-btn--sm, .wtm-icon-btn, .wtm-more, .wtm-caret { position: relative; }
.wtm-btn--sm::after, .wtm-icon-btn::after, .wtm-more::after, .wtm-caret::after {
  content: ''; position: absolute; top: 50%; left: 50%;
  width: max(100%, 40px); height: max(100%, 40px); transform: translate(-50%, -50%);
}
.wtm-icon-btn { width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white)); color: var(--color-text); cursor: pointer; border-radius: var(--radius-sm, 6px); font-size: 12px; transition-property: background-color, border-color, color, transform; transition-duration: .15s; transition-timing-function: cubic-bezier(.2, 0, 0, 1); }
.wtm-icon-btn:hover:not(:disabled) { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-icon-btn:active:not(:disabled) { transform: scale(.96); }
.wtm-icon-btn:disabled { opacity: .4; cursor: not-allowed; }
.wtm-icon-btn--nav { font-size: 13px; line-height: 1; }
.wtm-chip { display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border: 1px solid var(--panel-border); font-size: 11.5px; font-weight: 700; border-radius: 999px; background: var(--surface-panel, var(--color-white)); white-space: nowrap; font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1; }
.wtm-chip--lt { background: var(--surface-panel-subtle, var(--color-bg-secondary)); }
.wtm-chip--dash { border-style: dashed; color: var(--color-text-light); background: transparent; }
.wtm-chip--multi { border-style: dashed; border-color: var(--color-primary); color: var(--color-primary); background: transparent; }
.wtm-chip--yl { background: var(--color-warning, #f5c518); color: #111; border-color: var(--color-warning, #f5c518); }
.wtm-chip--active { background: var(--color-success); color: #fff; border-color: var(--color-success); }
.wtm-chip--completed { background: var(--surface-panel-subtle, var(--color-bg-secondary)); }
.wtm-chip--archived { background: var(--color-text-light); color: #fff; border-color: var(--color-text-light); }
.wtm-err { color: var(--color-error); font-size: 12.5px; margin-bottom: 8px; }
@media (max-width: 900px) {
  .wtm-detail__head { padding: 12px 12px 10px; }
  /* 头部两行封顶（2026-11-18 Wave H · 真机反馈，与项目详情同一条 CSS）：标题行与
     元信息行各自不换行。原来 `.wtm-title { flex: 1 1 100% }` 让任务标题独占一行，
     按钮与 chip 再各自换行，390px 实测头部 206px（≈4 行）把正文挤出首屏。
     窄屏让位的只有次要装饰（进行中 / 自动·手动创建 / CAS 版本号），操作按钮一个不少。 */
  .wtm-title-row { flex-wrap: nowrap; gap: 6px; }
  /* 任务标题只给一行：拿剩余空间，放不下就省略号（用户明确要求） */
  .wtm-title { flex: 0 1 auto; min-width: 0; font-size: 18px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wtm-title-actions { margin-left: auto; flex: 0 0 auto; flex-wrap: nowrap; }
  /* 头部按钮压一档把宽度还给标题（正文里的 .wtm-btn--sm 不受影响） */
  .wtm-title-actions .wtm-btn--sm { padding: 3px 6px; }
  .wtm-title-row .wtm-icon-btn { flex: 0 0 auto; }
  .wtm-title-row .wtm-chip { flex: 0 0 auto; }
  /* ≤360px 档把「快速改名」的 ✎ 也让给标题（设置 tab 里就是完整改名表单）：
     评审 C2 实测 320px 已归档项目（按钮变「取消归档」+64px）标题只剩 26px。 */
  @media (max-width: 360px) {
    .wtm-title-row .wtm-icon-btn { display: none; }
  }
  /* 窄屏让位的只有「按钮文案/其他视图已经说过一遍」的装饰：进行中（默认态）、已归档（按钮
     自己就变成「取消归档」）、自动·手动创建（项目详情的任务行仍带「自动」标记）。
     「已完成」没有按钮回声，保留。操作按钮一个不隐藏——评审 C2：装饰不让位时 320px 会把
     标题压成 0 宽、把「删除」推出裁切框外点不到。 */
  .wtm-title-row .wtm-chip--active,
  .wtm-title-row .wtm-chip--archived,
  .wtm-title-row .wtm-chip--dash { display: none; }
  .wtm-desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wtm-meta { flex-wrap: nowrap; gap: 5px; }
  /* 计数不缩；只有「最近活跃」允许收缩，且必须真出省略号——评审 C1：统一 `flex: 0 0 auto`
     会让 shrink 永不发生，pill 被祖先 .wtm-detail-wrap 的 overflow:hidden 硬切成半截日期。 */
  .wtm-meta .wtm-chip--lt { flex: 0 0 auto; }
  /* CAS 版本号是排障信息，不该占用户的一行 */
  .wtm-meta .wtm-chip--ver { display: none; }
  .wtm-meta .wtm-chip--dash { flex: 0 1 auto; display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .wtm-pane { padding: 10px 12px; }
  /* 窄屏：名称 + 操作一行放下（大小/时间隐藏；操作列 auto 宽度靠右），
     避免 4 列硬塞把「预览/下载」挤出可视区。 */
  .wtm-frow { grid-template-columns: minmax(0, 1fr) auto; }
  .wtm-fsz, .wtm-ftm { display: none; }
  .wtm-frow--head span:nth-child(2), .wtm-frow--head span:nth-child(3) { display: none; }
  .wtm-notes { grid-template-columns: 1fr; }
}
</style>
