<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 新建 / 编辑任务（S5）— 同一表单。
 *
 * - 新建：名称必填、备注可空、所属项目、记忆策略；**不暴露存储权威**（从项目继承）。
 * - 编辑：名称/备注/状态 + CAS（`expected_row_version`）；冲突时提示刷新/以我的为准。
 */
import { computed, nextTick, ref, watch } from 'vue'
import {
  apiErrorMessage,
  createTask,
  updateTask,
  type WorkspaceProjectOverview,
  type WorkspaceTask,
} from '@/api/workspace'

const props = defineProps<{
  visible: boolean
  mode: 'create' | 'edit'
  projects: WorkspaceProjectOverview[]
  /** 新建时的默认项目 */
  defaultProjectId?: string
  /** 编辑时的任务（含 row_version） */
  task?: WorkspaceTask | null
}>()

const emit = defineEmits<{ close: []; saved: [task: WorkspaceTask] }>()

const title = ref('')
const description = ref('')
const projectId = ref('')
const ingest = ref('user_facts')
const status = ref('active')
const busy = ref(false)
const error = ref('')
const conflict = ref(false)
const titleRef = ref<HTMLInputElement | null>(null)

const isEdit = computed(() => props.mode === 'edit')
const canSubmit = computed(() => title.value.trim().length > 0 && !busy.value)

const statusOptions = [
  { value: 'active', label: '进行中' },
  { value: 'completed', label: '已完成' },
  { value: 'archived', label: '已归档' },
]

const ingestOptions = [
  { value: 'user_facts', label: '收录用户事实（推荐）' },
  { value: 'normal', label: '常规收录' },
  { value: 'exclude', label: '不收录' },
]

function reset() {
  error.value = ''
  conflict.value = false
  busy.value = false
  if (props.mode === 'edit' && props.task) {
    title.value = props.task.title || ''
    description.value = props.task.description || ''
    status.value = props.task.status || 'active'
    projectId.value = props.task.project_id || ''
  } else {
    title.value = ''
    description.value = ''
    status.value = 'active'
    projectId.value = props.defaultProjectId || props.projects[0]?.id || ''
    ingest.value = 'user_facts'
  }
}

watch(
  () => props.visible,
  (v) => {
    if (v) {
      reset()
      void nextTick(() => titleRef.value?.focus())
    }
  },
  { immediate: true },
)

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  conflict.value = false
  try {
    let saved: WorkspaceTask
    if (isEdit.value && props.task) {
      saved = await updateTask(props.task.id, {
        title: title.value.trim(),
        description: description.value.trim(),
        status: status.value,
        expected_row_version: props.task.row_version,
      })
    } else {
      saved = await createTask(projectId.value, {
        title: title.value.trim(),
        description: description.value.trim() || null,
        system_memory_ingest: ingest.value,
      })
    }
    emit('saved', saved)
    emit('close')
  } catch (err: any) {
    const code = err?.response?.data?.detail?.code
    if (code === 'workspace_task_stale') {
      conflict.value = true
      error.value = '任务已被其他端修改'
    } else {
      error.value = apiErrorMessage(err, isEdit.value ? '保存失败，请重试' : '创建失败，请重试')
    }
  } finally {
    busy.value = false
  }
}

/** 冲突时「以我的为准」：用最新版本号重试（先刷新拿版本）。 */
async function overwrite() {
  if (!props.task) return
  busy.value = true
  error.value = ''
  try {
    // 重新拉一次最新版本（overview 由父组件刷新后传入；这里用 task.row_version+1 兜底不可靠，
    // 因此要求父组件在冲突后刷新并重新打开编辑）。这里直接提示刷新。
    conflict.value = false
    error.value = '请刷新后重新编辑（版本已变化）'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="wtm-overlay" @mousedown.self="emit('close')">
      <div class="wtm-modal" role="dialog" aria-modal="true" :aria-label="isEdit ? '编辑任务' : '新建任务'">
        <header class="wtm-modal__head">
          <h3>{{ isEdit ? '编辑任务' : '新建任务' }}</h3>
          <span v-if="isEdit && task?.row_version" class="wtm-chip wtm-chip--dash">
            v{{ task.row_version }} · CAS
          </span>
          <button class="wtm-icon-btn" aria-label="关闭" @click="emit('close')">✕</button>
        </header>
        <div class="wtm-modal__body">
          <div class="wtm-field">
            <label>任务名称 <span class="wtm-req">*</span></label>
            <input ref="titleRef" v-model="title" class="wtm-input" placeholder="例如：季度财报分析"
                   @keydown.enter.prevent="submit" />
          </div>
          <div class="wtm-field">
            <label>备注（agent 可见）</label>
            <textarea v-model="description" class="wtm-textarea"
                      placeholder="目标、约束、交付物形态……"></textarea>
            <div class="wtm-hint">备注进入 agent 的任务上下文；Markdown 纯文本。</div>
          </div>
          <div v-if="!isEdit" class="wtm-field">
            <label>所属项目</label>
            <select v-model="projectId" class="wtm-select">
              <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            <div class="wtm-hint">项目决定任务归类与文件域；创建后不可移动。</div>
          </div>
          <div v-if="!isEdit" class="wtm-field">
            <label>记忆收录</label>
            <select v-model="ingest" class="wtm-select">
              <option v-for="o in ingestOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div v-if="isEdit" class="wtm-field">
            <label>状态</label>
            <select v-model="status" class="wtm-select">
              <option v-for="o in statusOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div v-if="conflict" class="wtm-conflict">
            <b>冲突提示：</b>{{ error }}
            <span class="wtm-conflict__actions">
              <button class="wtm-btn wtm-btn--sm" @click="overwrite">刷新</button>
            </span>
          </div>
          <div v-else-if="error" class="wtm-err">{{ error }}</div>
        </div>
        <footer class="wtm-modal__foot">
          <button class="wtm-btn" @click="emit('close')">取消</button>
          <button class="wtm-btn wtm-btn--primary" :disabled="!canSubmit" @click="submit">
            {{ busy ? '保存中…' : isEdit ? '保存' : '创建任务' }}
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.wtm-overlay {
  position: fixed; inset: 0; z-index: 10010; display: flex; align-items: center;
  justify-content: center; padding: 24px; background: var(--overlay-scrim, rgba(0, 0, 0, .35));
}
.wtm-modal {
  width: 100%; max-width: 520px; max-height: 90vh; overflow: auto;
  background: var(--surface-panel-strong, var(--color-white)); color: var(--color-text);
  border: 1px solid var(--panel-border); border-radius: var(--dialog-radius, 12px);
  box-shadow: var(--dialog-shadow, var(--frame-shadow));
}
.wtm-modal__head {
  display: flex; align-items: center; gap: 8px; padding: 12px 16px;
  border-bottom: 1px solid var(--panel-border);
}
.wtm-modal__head h3 { margin: 0; font-size: 16px; font-weight: 800; }
.wtm-modal__head .wtm-icon-btn { margin-left: auto; }
.wtm-modal__body { padding: 16px; }
.wtm-modal__foot {
  display: flex; justify-content: flex-end; gap: 8px; padding: 12px 16px;
  border-top: 1px solid var(--panel-border);
}
.wtm-field { margin-bottom: 14px; }
.wtm-field label {
  display: block; font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
  font-weight: 700; margin-bottom: 5px; color: var(--color-text-light);
}
.wtm-req { color: var(--color-error); }
.wtm-input, .wtm-textarea, .wtm-select {
  width: 100%; border: 1px solid var(--panel-border); background: var(--surface-input, var(--color-white));
  color: var(--color-text); padding: 8px 10px; font-size: 13.5px; border-radius: var(--radius-sm, 6px);
  font-family: inherit;
}
.wtm-textarea { min-height: 74px; resize: vertical; }
.wtm-input:focus, .wtm-textarea:focus, .wtm-select:focus {
  outline: 2px solid var(--focus-ring-color, var(--color-primary)); outline-offset: 0;
}
.wtm-hint { font-size: 11.5px; color: var(--color-text-light); margin-top: 4px; }
.wtm-btn {
  display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px;
  border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
  color: var(--color-text); font-weight: 700; font-size: 13px; cursor: pointer;
  border-radius: var(--radius-sm, 6px); font-family: inherit;
}
.wtm-btn:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-btn:disabled { opacity: .5; cursor: not-allowed; }
.wtm-btn--primary { background: var(--color-primary); color: var(--color-white); border-color: var(--color-primary); }
.wtm-btn--primary:hover { background: var(--color-primary-dark, var(--color-primary)); }
.wtm-btn--sm { padding: 3px 8px; font-size: 12px; }
.wtm-icon-btn {
  width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
  color: var(--color-text); cursor: pointer; border-radius: var(--radius-sm, 6px); font-size: 12px;
}
.wtm-icon-btn:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px;
  border: 1px solid var(--panel-border); font-size: 11.5px; font-weight: 700;
  border-radius: 999px; background: var(--surface-panel, var(--color-white)); white-space: nowrap;
}
.wtm-chip--dash { border-style: dashed; color: var(--color-text-light); background: transparent; }
.wtm-err { color: var(--color-error); font-size: 12.5px; }
.wtm-conflict {
  border: 1px solid var(--color-warning, var(--color-error)); background: var(--surface-panel-subtle, #fff8e1);
  padding: 8px 10px; font-size: 12px; border-radius: var(--radius-sm, 6px);
}
.wtm-conflict__actions { display: inline-flex; gap: 6px; margin-left: 6px; }
</style>
