<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 新建 / 编辑项目（S10）— 与任务同构的表单。
 *
 * - 新建：名称必填、备注可空、记忆策略、存储权威（创建后不可改）。
 * - 编辑：名称/备注/记忆策略 + CAS；存储权威只读。
 * - 默认项目（is_default）：可改名/写备注，但归档/删除按钮禁用（由父组件处理）。
 */
import { computed, nextTick, ref, watch } from 'vue'
import {
  apiErrorMessage,
  createProject,
  updateProject,
  type WorkspaceProjectCreated,
  type WorkspaceProjectOverview,
} from '@/api/workspace'

const props = defineProps<{
  visible: boolean
  mode: 'create' | 'edit'
  project?: WorkspaceProjectOverview | null
}>()

const emit = defineEmits<{ close: []; saved: [project: WorkspaceProjectCreated] }>()

const name = ref('')
const description = ref('')
const ingest = ref('user_facts')
const authority = ref('cloud')
const busy = ref(false)
const error = ref('')
const conflict = ref(false)
const nameRef = ref<HTMLInputElement | null>(null)

const isEdit = computed(() => props.mode === 'edit')
const canSubmit = computed(() => name.value.trim().length > 0 && !busy.value)

const ingestOptions = [
  { value: 'user_facts', label: '收录用户事实（推荐）' },
  { value: 'normal', label: '常规收录' },
  { value: 'exclude', label: '不收录' },
]

// 存储位置只读展示（真实继承值；设备权威不显示内部 id）
const storageLabel = computed(() => {
  const raw = String(props.project?.storage_authority || 'cloud')
  if (raw === 'cloud') return '云端'
  if (raw.startsWith('device:')) return '设备本地'
  return raw
})

function reset() {
  error.value = ''
  conflict.value = false
  busy.value = false
  if (props.mode === 'edit' && props.project) {
    name.value = props.project.name
    description.value = props.project.description || ''
    // 2026-11-17：回填真实策略（此前恒 user_facts，改名保存会把 exclude 重置）
    ingest.value = props.project.system_memory_ingest || 'user_facts'
    authority.value = props.project.storage_authority || 'cloud'
  } else {
    name.value = ''
    description.value = ''
    ingest.value = 'user_facts'
    authority.value = 'cloud'
  }
}

watch(
  () => props.visible,
  (v) => {
    if (v) {
      reset()
      void nextTick(() => nameRef.value?.focus())
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
    let saved: WorkspaceProjectCreated
    if (isEdit.value && props.project) {
      saved = await updateProject(props.project.id, {
        name: name.value.trim(),
        description: description.value.trim(),
        system_memory_ingest: ingest.value,
        expected_row_version: props.project.row_version,
      })
    } else {
      saved = await createProject({
        name: name.value.trim(),
        description: description.value.trim() || null,
        storage_authority: authority.value,
        system_memory_ingest: ingest.value,
      })
    }
    emit('saved', saved)
    emit('close')
  } catch (err: any) {
    const code = err?.response?.data?.detail?.code
    if (code === 'workspace_project_stale') {
      conflict.value = true
      error.value = '项目已被其他端修改'
    } else {
      error.value = apiErrorMessage(err, isEdit.value ? '保存失败，请重试' : '创建失败，请重试')
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="wtm-overlay" @mousedown.self="emit('close')">
      <div class="wtm-modal" role="dialog" aria-modal="true" :aria-label="isEdit ? '编辑项目' : '新建项目'">
        <header class="wtm-modal__head">
          <h3>{{ isEdit ? '编辑项目' : '新建项目' }}</h3>
          <span v-if="isEdit && project?.row_version" class="wtm-chip wtm-chip--dash">
            v{{ project.row_version }} · CAS
          </span>
          <button class="wtm-icon-btn" aria-label="关闭" @click="emit('close')">✕</button>
        </header>
        <div class="wtm-modal__body">
          <div class="wtm-field">
            <label>项目名称 <span class="wtm-req">*</span></label>
            <input ref="nameRef" v-model="name" class="wtm-input" placeholder="例如：客户交付"
                   @keydown.enter.prevent="submit" />
          </div>
          <div class="wtm-field">
            <label>项目备注（agent 可见）</label>
            <textarea v-model="description" class="wtm-textarea"
                      placeholder="这个项目装什么、给谁用……"></textarea>
          </div>
          <div class="wtm-field">
            <label>记忆收录（新任务默认继承）</label>
            <select v-model="ingest" class="wtm-select">
              <option v-for="o in ingestOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div class="wtm-field">
            <label>存储位置</label>
            <input v-if="isEdit" class="wtm-input" :value="storageLabel" disabled />
            <select v-else v-model="authority" class="wtm-select">
              <option value="cloud">云端</option>
            </select>
            <div class="wtm-hint">创建后不可改；跨位置迁移是独立设计项。</div>
          </div>
          <div v-if="conflict" class="wtm-conflict"><b>冲突提示：</b>{{ error }}</div>
          <div v-else-if="error" class="wtm-err">{{ error }}</div>
        </div>
        <footer class="wtm-modal__foot">
          <button class="wtm-btn" @click="emit('close')">取消</button>
          <button class="wtm-btn wtm-btn--primary" :disabled="!canSubmit" @click="submit">
            {{ busy ? '保存中…' : isEdit ? '保存' : '创建项目' }}
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
</style>
