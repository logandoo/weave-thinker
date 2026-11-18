<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 项目详情（S9）— Project 也是完整 CRUD 对象。
 *
 * 数据面：父组件传入 overview；写面 PATCH /api/workspace/projects/{id}（CAS）。
 * 文件 tab = 任务文件只读汇总（按任务分组）；`shared/` 写入方不在本期。
 */
import { computed, ref, watch } from 'vue'
import {
  apiErrorMessage,
  moveTasks,
  updateProject,
  type WorkspaceProjectOverview,
} from '@/api/workspace'

const props = defineProps<{
  project: WorkspaceProjectOverview
  /** 批量移动任务用（issue1）：可移动到的项目列表。 */
  projects?: WorkspaceProjectOverview[]
}>()

const emit = defineEmits<{
  edit: []
  delete: []
  archive: []
  unarchive: []
  refresh: []
  createTask: []
  openTask: [taskId: string]
  toast: [message: string]
  backToList: []
}>()

const isArchived = computed(() => props.project.status === 'archived')

type Tab = 'tasks' | 'files' | 'settings'
const tab = ref<Tab>('tasks')

const statusLabel: Record<string, string> = {
  active: '进行中',
  archived: '已归档',
  deleted: '已删除',
}

const totalConversations = computed(() =>
  props.project.tasks.reduce((n, t) => n + (t.conversations?.length || 0), 0),
)

// ---- 设置 ----
const editName = ref('')
const editDescription = ref('')
const editIngest = ref('user_facts')
const saveBusy = ref(false)
const saveError = ref('')

// 存储位置是只读展示；`cloud` 之外可能有 `device:<id>`（设备权威），只给用户看人话。
const storageLabel = computed(() => {
  const raw = String(props.project.storage_authority || 'cloud')
  if (raw === 'cloud') return '云端'
  if (raw.startsWith('device:')) return '设备本地'
  return raw
})

function syncSettings() {
  editName.value = props.project.name
  editDescription.value = props.project.description || ''
  // 2026-11-17：必须回填真实策略——此前硬编码 user_facts，任何一次改名保存
  // 都会把既有 exclude/normal 悄悄改回 user_facts（隐私语义被重置）。
  editIngest.value = props.project.system_memory_ingest || 'user_facts'
}

async function saveSettings() {
  if (saveBusy.value) return
  saveBusy.value = true
  saveError.value = ''
  try {
    await updateProject(props.project.id, {
      name: editName.value.trim() || undefined,
      description: editDescription.value.trim(),
      system_memory_ingest: editIngest.value,
      expected_row_version: props.project.row_version,
    })
    emit('toast', '已保存')
    emit('refresh')
  } catch (e: any) {
    const code = e?.response?.data?.detail?.code
    saveError.value =
      code === 'workspace_project_stale'
        ? '项目已被其他端修改，请刷新后重试'
        : apiErrorMessage(e, '保存失败')
  } finally {
    saveBusy.value = false
  }
}

watch(
  () => props.project.id,
  () => {
    tab.value = 'tasks'
    syncSettings()
    selectedTaskIds.value = new Set()   // 切换项目清空批量选择（评审 Minor：残留 id 会虚报「已选 N」）
    moveTarget.value = ''
  },
  { immediate: true },
)
watch(tab, (t) => {
  if (t === 'settings') syncSettings()
})

// ---- 批量移动任务到其他项目（issue1）----
const selectedTaskIds = ref<Set<string>>(new Set())
const moveTarget = ref('')
const moveBusy = ref(false)
const otherProjects = computed(() =>
  (props.projects || []).filter((p) => p.id !== props.project.id && p.status === 'active'))
const selectedCount = computed(() => selectedTaskIds.value.size)

function toggleTaskSel(id: string) {
  const next = new Set(selectedTaskIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedTaskIds.value = next
}

async function doBatchMove() {
  if (!moveTarget.value || !selectedCount.value || moveBusy.value) return
  moveBusy.value = true
  try {
    const ids = [...selectedTaskIds.value]
    const out = await moveTasks(ids, moveTarget.value)
    emit('toast', `已移动 ${out.moved} 个任务到目标项目`)
    selectedTaskIds.value = new Set()
    moveTarget.value = ''
    emit('refresh')
  } catch (e: any) {
    emit('toast', apiErrorMessage(e, '移动失败'))
  } finally {
    moveBusy.value = false
  }
}
</script>

<template>
  <div class="wtm-detail">
    <header class="wtm-detail__head">
      <div class="wtm-crumb">
        <button class="wtm-crumb__link" data-testid="wpm-project-back" title="返回项目列表" @click="emit('backToList')">← 项目管理</button>
        <span> / 项目</span>
      </div>
      <div class="wtm-title-row">
        <h2 class="wtm-title" :title="project.name">{{ project.name }}</h2>
        <button class="wtm-icon-btn" title="编辑" aria-label="编辑项目" @click="emit('edit')">✎</button>
        <span class="wtm-chip" :class="`wtm-chip--${project.status}`">{{ statusLabel[project.status] || project.status }}</span>
        <span v-if="project.is_default" class="wtm-chip wtm-chip--dash">系统保留</span>
        <div class="wtm-title-actions">
          <button class="wtm-btn wtm-btn--sm wtm-btn--primary" @click="emit('createTask')">新建任务</button>
          <button v-if="isArchived" class="wtm-btn wtm-btn--sm" @click="emit('unarchive')">取消归档</button>
          <button v-else class="wtm-btn wtm-btn--sm" :disabled="project.is_default" @click="emit('archive')">归档</button>
          <button class="wtm-btn wtm-btn--sm wtm-btn--danger" :disabled="project.is_default" @click="emit('delete')">删除</button>
        </div>
      </div>
      <p v-if="project.description" class="wtm-desc" :title="project.description">{{ project.description }}</p>
      <div class="wtm-meta">
        <span class="wtm-chip wtm-chip--lt wtm-chip--ver">v{{ project.row_version ?? 1 }}</span>
        <span class="wtm-chip wtm-chip--lt">{{ project.tasks.length }} 任务</span>
        <span class="wtm-chip wtm-chip--lt">{{ totalConversations }} 会话</span>
        <span v-if="project.last_active_at" class="wtm-chip wtm-chip--dash">最近活跃 {{ project.last_active_at.slice(0, 16).replace('T', ' ') }}</span>
      </div>
    </header>

    <div class="wtm-tabs" role="tablist">
      <button class="wtm-tab" :class="{ 'is-on': tab === 'tasks' }" @click="tab = 'tasks'">任务</button>
      <button class="wtm-tab" :class="{ 'is-on': tab === 'files' }" @click="tab = 'files'">文件</button>
      <button class="wtm-tab" :class="{ 'is-on': tab === 'settings' }" @click="tab = 'settings'">设置</button>
    </div>

    <div class="wtm-pane">
      <!-- 任务 -->
      <div v-if="tab === 'tasks'">
        <div class="wtm-pane__bar">
          <span class="wtm-muted">项目是任务容器：任务可在此新建，也可从其他项目 fork 输入。</span>
          <div class="wtm-grow">
            <button class="wtm-btn wtm-btn--sm wtm-btn--primary" @click="emit('createTask')">+ 新建任务</button>
          </div>
        </div>
        <div v-if="otherProjects.length && project.tasks.length" class="wtm-movebar">
          <span class="wtm-muted">勾选任务后可批量移动：</span>
          <span v-if="selectedCount" class="wtm-chip wtm-chip--lt">已选 {{ selectedCount }}</span>
          <div class="wtm-grow">
            <select v-model="moveTarget" class="wtm-select wtm-select--sm">
              <option value="" disabled>目标项目…</option>
              <option v-for="p in otherProjects" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <button class="wtm-btn wtm-btn--sm" :disabled="!moveTarget || !selectedCount || moveBusy"
                    data-testid="wpm-batch-move" @click="doBatchMove">
              {{ moveBusy ? '移动中…' : `移动所选${selectedCount ? '（' + selectedCount + '）' : ''}` }}
            </button>
          </div>
        </div>
        <div v-if="!project.tasks.length" class="wtm-empty">
          <b>项目还没有任务</b><span>点上方按钮新建第一个任务。</span>
        </div>
        <div v-else class="wtm-crow-list">
          <div v-for="t in project.tasks" :key="t.id" class="wtm-crow">
            <input v-if="otherProjects.length" type="checkbox" class="wtm-cks"
                   :checked="selectedTaskIds.has(t.id)" :aria-label="`选择 ${t.title || '任务'}`"
                   @change="toggleTaskSel(t.id)" />
            <span class="wtm-dot" :class="{ 'is-on': t.status === 'active' }"></span>
            <span class="wtm-crow__t">{{ t.title || '未命名任务' }}</span>
            <span class="wtm-chip" :class="`wtm-chip--${t.status}`">{{ statusLabel[t.status] || t.status }}</span>
            <span v-if="t.origin === 'auto'" class="wtm-chip wtm-chip--dash">自动</span>
            <span class="wtm-crow__m">{{ t.conversations?.length || 0 }} 会话 · {{ t.notes_count || 0 }} 笔记</span>
            <span class="wtm-crow__sp">
              <button class="wtm-btn wtm-btn--sm" @click="emit('openTask', t.id)">打开</button>
            </span>
          </div>
        </div>
      </div>

      <!-- 文件（只读汇总）-->
      <div v-else-if="tab === 'files'">
        <div class="wtm-pane__bar">
          <span class="wtm-muted">各任务的交付文件按任务汇总，点击任务可进入文件列表（只读）。</span>
          <div class="wtm-grow"><span class="wtm-chip wtm-chip--dash">只读</span></div>
        </div>
        <div v-if="!project.tasks.length" class="wtm-empty">
          <b>项目还没有任务文件</b><span>任务产出会按任务分组汇总到这里。</span>
        </div>
        <div v-else class="wtm-ftable">
          <div class="wtm-frow wtm-frow--head"><span>任务 / 交付文件</span><span>会话</span><span></span></div>
          <div v-for="t in project.tasks" :key="t.id" class="wtm-frow">
            <span class="wtm-fname">
              <i class="wtm-ficon">D</i>
              <span>{{ t.title || '未命名任务' }}</span>
            </span>
            <span class="wtm-fsz">{{ t.conversations?.length || 0 }}</span>
            <span class="wtm-fact">
              <button class="wtm-btn wtm-btn--sm" @click="emit('openTask', t.id)">打开</button>
            </span>
          </div>
        </div>
      </div>

      <!-- 设置 -->
      <div v-else class="wtm-settings">
        <div class="wtm-field">
          <label>项目名称 <span class="wtm-req">*</span></label>
          <input v-model="editName" class="wtm-input" />
        </div>
        <div class="wtm-field">
          <label>项目备注（agent 可见）</label>
          <textarea v-model="editDescription" class="wtm-textarea"></textarea>
        </div>
        <div class="wtm-field">
          <label>记忆收录（新任务默认继承）</label>
          <select v-model="editIngest" class="wtm-select">
            <option value="user_facts">收录用户事实（推荐）</option>
            <option value="normal">常规收录</option>
            <option value="exclude">不收录</option>
          </select>
        </div>
        <div class="wtm-field">
          <label>存储位置</label>
          <input class="wtm-input" :value="storageLabel" disabled />
          <div class="wtm-hint">从项目继承，创建后不可改。</div>
        </div>
        <div v-if="saveError" class="wtm-err">{{ saveError }}</div>
        <div class="wtm-settings__foot">
          <button class="wtm-btn wtm-btn--primary" :disabled="saveBusy" @click="saveSettings">
            {{ saveBusy ? '保存中…' : '保存' }}
          </button>
        </div>
        <div class="wtm-danger">
          <h4>危险区</h4>
          <p v-if="project.is_default">系统保留：默认项目是自动绑定的落点，不可归档或删除。</p>
          <p v-else>归档保留文件与绑定可恢复；彻底删除会级联删除 {{ project.tasks.length }} 个任务（文件进回收站 7 天），会话与笔记解绑。</p>
          <button class="wtm-btn wtm-btn--sm wtm-btn--danger" :disabled="project.is_default" @click="emit('delete')">
            删除项目…
          </button>
        </div>
      </div>
    </div>
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
.wtm-tab { padding: 9px 16px; font-weight: 700; font-size: 13px; border: 0; border-right: 1px solid var(--panel-border); background: transparent; cursor: pointer; color: var(--color-text); font-family: inherit; white-space: nowrap; }
.wtm-tab:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-tab.is-on { background: var(--color-primary); color: var(--color-white); }
.wtm-pane { padding: 14px 18px; flex: 1; min-height: 0; overflow: auto; }
.wtm-pane__bar { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.wtm-movebar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; padding: 8px 10px; border: 1px dashed var(--panel-border); border-radius: var(--radius-sm, 6px); flex-wrap: wrap; }
.wtm-select--sm { width: auto; min-width: 120px; padding: 4px 8px; font-size: 12px; }
.wtm-cks { width: 15px; height: 15px; flex: none; cursor: pointer; accent-color: var(--color-primary); }
.wtm-grow { margin-left: auto; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.wtm-mono { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.wtm-muted { color: var(--color-text-light); font-size: 12.5px; }
.wtm-crow-list { display: flex; flex-direction: column; gap: 8px; }
.wtm-crow { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border: 1px solid var(--panel-border); border-radius: var(--radius-sm, 6px); background: var(--surface-panel, var(--color-white)); flex-wrap: wrap; }
.wtm-crow__t { font-weight: 700; font-size: 13.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wtm-crow__m { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 11px; color: var(--color-text-light); }
.wtm-crow__sp { margin-left: auto; display: flex; gap: 6px; }
.wtm-dot { display: inline-block; width: 8px; height: 8px; border: 1px solid var(--panel-border); background: var(--color-text-light); border-radius: 50%; flex: none; }
.wtm-dot.is-on { background: var(--color-success); border-color: var(--color-success); }
.wtm-empty { border: 1px dashed var(--panel-border); padding: 26px; text-align: center; color: var(--color-text-light); font-size: 13px; border-radius: var(--radius-sm, 6px); }
.wtm-empty b { display: block; color: var(--color-text); font-size: 15px; margin-bottom: 4px; }
.wtm-ftable { border: 1px solid var(--panel-border); border-radius: var(--radius-sm, 6px); overflow: hidden; }
.wtm-frow { display: grid; grid-template-columns: 1fr 90px 80px; align-items: center; gap: 8px; padding: 7px 10px; border-bottom: 1px solid var(--panel-border); font-size: 13px; }
.wtm-frow:last-child { border-bottom: 0; }
.wtm-frow--head { background: var(--surface-panel-subtle, var(--color-bg-secondary)); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; font-weight: 700; color: var(--color-text-light); }
.wtm-fname { display: flex; align-items: center; gap: 8px; min-width: 0; }
.wtm-fname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wtm-ficon { width: 22px; height: 22px; border: 1px solid var(--panel-border); display: inline-flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 900; background: var(--surface-panel, var(--color-white)); flex: none; border-radius: 4px; }
.wtm-fsz { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 11.5px; color: var(--color-text-light); }
.wtm-fact { text-align: right; }
.wtm-legend { margin-top: 10px; font-size: 11.5px; color: var(--color-text-light); }
.wtm-legend code { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; }
.wtm-settings { max-width: 640px; }
.wtm-field { margin-bottom: 14px; }
.wtm-field label { display: block; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; font-weight: 700; margin-bottom: 5px; color: var(--color-text-light); }
.wtm-req { color: var(--color-error); }
.wtm-input, .wtm-textarea, .wtm-select { width: 100%; border: 1px solid var(--panel-border); background: var(--surface-input, var(--color-white)); color: var(--color-text); padding: 8px 10px; font-size: 13.5px; border-radius: var(--radius-sm, 6px); font-family: inherit; }
.wtm-textarea { min-height: 74px; resize: vertical; }
.wtm-hint { font-size: 11.5px; color: var(--color-text-light); margin-top: 4px; }
.wtm-settings__foot { display: flex; justify-content: flex-end; margin-bottom: 16px; }
.wtm-danger { border: 1px solid var(--color-danger); background: var(--surface-panel-subtle, #fbe9e7); padding: 12px; border-radius: var(--radius-sm, 6px); }
.wtm-danger h4 { margin: 0 0 4px; font-size: 13px; color: var(--color-danger); }
.wtm-danger p { margin: 0 0 8px; font-size: 12px; color: var(--color-text-light); }
.wtm-btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white)); color: var(--color-text); font-weight: 700; font-size: 13px; cursor: pointer; border-radius: var(--radius-sm, 6px); font-family: inherit; }
.wtm-btn:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-btn:disabled { opacity: .5; cursor: not-allowed; }
.wtm-btn--primary { background: var(--color-primary); color: var(--color-white); border-color: var(--color-primary); }
.wtm-btn--danger { background: var(--color-danger); color: var(--color-white); border-color: var(--color-danger); }
.wtm-btn--sm { padding: 3px 8px; font-size: 12px; }
/* 视觉可以小，命中区不小于 40×40（手机端「打开」是进入任务文件的唯一入口） */
.wtm-btn--sm, .wtm-icon-btn { position: relative; }
.wtm-btn--sm::after, .wtm-icon-btn::after {
  content: ''; position: absolute; top: 50%; left: 50%;
  width: max(100%, 40px); height: max(100%, 40px); transform: translate(-50%, -50%);
}
.wtm-icon-btn { width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white)); color: var(--color-text); cursor: pointer; border-radius: var(--radius-sm, 6px); font-size: 12px; }
.wtm-icon-btn:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-chip { display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border: 1px solid var(--panel-border); font-size: 11.5px; font-weight: 700; border-radius: 999px; background: var(--surface-panel, var(--color-white)); white-space: nowrap; }
.wtm-chip--lt { background: var(--surface-panel-subtle, var(--color-bg-secondary)); }
.wtm-chip--dash { border-style: dashed; color: var(--color-text-light); background: transparent; }
.wtm-chip--active { background: var(--color-success); color: #fff; border-color: var(--color-success); }
.wtm-chip--completed { background: var(--surface-panel-subtle, var(--color-bg-secondary)); }
.wtm-chip--archived { background: var(--color-text-light); color: #fff; border-color: var(--color-text-light); }
.wtm-err { color: var(--color-error); font-size: 12.5px; margin-bottom: 8px; }
@media (max-width: 900px) {
  .wtm-detail__head { padding: 12px 12px 10px; }
  /* 头部两行封顶（2026-11-18 Wave H · 真机反馈）：标题行与元信息行各自不换行。
     原来 `.wtm-title { flex: 1 1 100% }` 让标题独占一行，按钮与 chip 再各自换行，
     390px 实测头部 172px（≈4 行），把正文挤出首屏。窄屏让位的只有默认态装饰
     （进行中 / CAS 版本号），三个操作按钮一个不少。 */
  .wtm-title-row { flex-wrap: nowrap; gap: 6px; }
  /* 标题只给一行：拿剩余空间，放不下就省略号（用户明确要求） */
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
  /* 窄屏让位的只有「按钮文案已经说过一遍」的装饰：进行中（默认态）、已归档（按钮自己就
     变成「取消归档」）、系统保留（归档/删除本就是灰掉的 disabled 态，设置 tab 危险区有解释）。
     操作按钮一个不隐藏——评审 C2：装饰不让位时 320px 的默认/已归档项目会把标题压成 0 宽、
     把「删除」推出裁切框外点不到。 */
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
  /* 窄屏保留「打开」操作列（原来整列被隐藏=任务文件在手机上无法进入），
     只收起大小列。 */
  .wtm-frow { grid-template-columns: minmax(0, 1fr) auto; }
  .wtm-fsz { display: none; }
  .wtm-frow--head span:nth-child(2) { display: none; }
}
</style>
