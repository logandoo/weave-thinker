<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 删除任务 / 项目（S6 / S10）— 两段式。
 *
 * - 归档（默认，可恢复）与彻底删除分开；主按钮是「归档」。
 * - 彻底删除：需逐字输入对象名 + 级联计数 + 会话处理单选（默认仅解绑）。
 * - 默认项目（is_default）：删除/归档禁用，提示「系统保留」。
 * - 活跃 run → 后端 409，前端显示错误。
 */
import { computed, ref, watch } from 'vue'
import {
  apiErrorMessage,
  deleteProject,
  deleteTask,
  type DeleteOutcome,
  type DeletePreview,
} from '@/api/workspace'

const props = defineProps<{
  visible: boolean
  kind: 'task' | 'project'
  target: { id: string; name: string; row_version?: number; is_default?: boolean } | null
  preview: DeletePreview | null
  previewLoading?: boolean
}>()

const emit = defineEmits<{ close: []; deleted: [outcome: DeleteOutcome] }>()

const mode = ref<'archive' | 'purge'>('archive')
const conversations = ref<'unbind' | 'delete'>('unbind')
const confirmText = ref('')
const busy = ref(false)
const error = ref('')

const isDefault = computed(() => !!props.target?.is_default)
const isPurge = computed(() => mode.value === 'purge')
const counts = computed(() => props.preview?.counts || { tasks: 0, conversations: 0, notes: 0 })
const confirmOk = computed(
  () => !isPurge.value || confirmText.value.trim() === (props.target?.name || '').trim(),
)
const canSubmit = computed(() => !busy.value && !isDefault.value && confirmOk.value)

function reset() {
  mode.value = 'archive'
  conversations.value = 'unbind'
  confirmText.value = ''
  busy.value = false
  error.value = ''
}

watch(
  () => props.visible,
  (v) => {
    if (v) reset()
  },
  { immediate: true },
)

async function submit() {
  if (!props.target || !canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    const input = {
      mode: mode.value,
      expected_row_version: props.target.row_version,
      conversations: conversations.value,
      ...(props.kind === 'task'
        ? { confirm_title: confirmText.value.trim() }
        : { confirm_name: confirmText.value.trim() }),
    }
    const outcome =
      props.kind === 'task'
        ? await deleteTask(props.target.id, input)
        : await deleteProject(props.target.id, input)
    emit('deleted', outcome)
    emit('close')
  } catch (err: any) {
    const code = err?.response?.data?.detail?.code
    if (code === 'workspace_run_active') {
      error.value = '有活跃执行（run）或委派任务，请先停止后再删除'
    } else if (code === 'workspace_project_reserved') {
      error.value = '默认项目是系统保留：自动绑定必须有落点，不可删除'
    } else if (code === 'workspace_task_stale' || code === 'workspace_project_stale') {
      error.value = '对象已被其他端修改，请刷新后重试'
    } else {
      error.value = apiErrorMessage(err, '删除失败，请重试')
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="visible && target" class="wtm-overlay" @mousedown.self="emit('close')">
      <div class="wtm-modal wtm-modal--wide" role="dialog" aria-modal="true"
           :aria-label="`删除${kind === 'task' ? '任务' : '项目'}`">
        <header class="wtm-modal__head">
          <h3>删除{{ kind === 'task' ? '任务' : '项目' }}「{{ target.name }}」</h3>
          <button class="wtm-icon-btn" aria-label="关闭" @click="emit('close')">✕</button>
        </header>
        <div class="wtm-modal__body">
          <div v-if="isDefault" class="wtm-reserved">
            系统保留：默认项目是自动绑定的落点，不可归档或删除。可改名、可写备注。
          </div>

          <div class="wtm-radio-group">
            <div class="wtm-radio-row" :class="{ 'is-on': mode === 'archive' }"
                 @click="mode = 'archive'">
              <span class="wtm-rb">{{ mode === 'archive' ? '✓' : '' }}</span>
              <div>
                <b>归档（推荐）</b>
                <span>移出列表，文件、绑定、笔记全部保留；随时可恢复。</span>
              </div>
            </div>
            <div class="wtm-radio-row" :class="{ 'is-on': mode === 'purge' }"
                 @click="mode = 'purge'">
              <span class="wtm-rb">{{ mode === 'purge' ? '✓' : '' }}</span>
              <div>
                <b>彻底删除</b>
                <span v-if="previewLoading">正在统计级联影响…</span>
                <span v-else>
                  删除{{ kind === 'project' ? ` ${counts.tasks} 个任务与` : '' }}文件（进回收站 7 天）；
                  {{ counts.conversations }} 个会话按下方选择处理、{{ counts.notes }} 个笔记解绑。不可撤销。
                </span>
              </div>
            </div>
          </div>

          <template v-if="isPurge">
            <div class="wtm-field wtm-field--mt">
              <label>会话处理</label>
            </div>
            <div class="wtm-radio-group">
              <div class="wtm-radio-row" :class="{ 'is-on': conversations === 'unbind' }"
                   @click="conversations = 'unbind'">
                <span class="wtm-rb">{{ conversations === 'unbind' ? '✓' : '' }}</span>
                <div>
                  <b>仅解绑（默认）</b>
                  <span>会话保留在侧边栏并进入「未归类」；历史消息与附件不变；下一回合按默认路由重新铸任务（文件域切换）。</span>
                </div>
              </div>
              <div class="wtm-radio-row" :class="{ 'is-on': conversations === 'delete' }"
                   @click="conversations = 'delete'">
                <span class="wtm-rb">{{ conversations === 'delete' ? '✓' : '' }}</span>
                <div>
                  <b>连同会话一起删除</b>
                  <span>删除 {{ counts.conversations }} 个会话及其全部消息与附件；不可撤销。</span>
                </div>
              </div>
            </div>
            <div class="wtm-field wtm-field--mt">
              <label>输入{{ kind === 'task' ? '任务名' : '项目名' }}以确认 <span class="wtm-req">*</span></label>
              <input v-model="confirmText" class="wtm-input" :placeholder="target.name"
                     @keydown.enter.prevent="submit" />
            </div>
            <div class="wtm-hint">活跃 run 期间不可删除；请先停止当前执行。</div>
          </template>

          <div v-if="error" class="wtm-err">{{ error }}</div>
        </div>
        <footer class="wtm-modal__foot">
          <button class="wtm-btn" @click="emit('close')">取消</button>
          <button v-if="!isPurge" class="wtm-btn wtm-btn--primary"
                  :disabled="!canSubmit" @click="submit">
            {{ busy ? '处理中…' : '归档' }}
          </button>
          <button v-else class="wtm-btn wtm-btn--danger"
                  :disabled="!canSubmit" @click="submit">
            {{ busy ? '删除中…' : '彻底删除' }}
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
.wtm-modal--wide { max-width: 580px; }
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
.wtm-field { margin-bottom: 8px; }
.wtm-field--mt { margin-top: 14px; }
.wtm-field label {
  display: block; font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
  font-weight: 700; margin-bottom: 5px; color: var(--color-text-light);
}
.wtm-req { color: var(--color-error); }
.wtm-input {
  width: 100%; border: 1px solid var(--panel-border); background: var(--surface-input, var(--color-white));
  color: var(--color-text); padding: 8px 10px; font-size: 13.5px; border-radius: var(--radius-sm, 6px);
  font-family: inherit;
}
.wtm-input:focus { outline: 2px solid var(--focus-ring-color, var(--color-primary)); outline-offset: 0; }
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
.wtm-btn--danger { background: var(--color-danger); color: var(--color-white); border-color: var(--color-danger); }
.wtm-icon-btn {
  width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
  color: var(--color-text); cursor: pointer; border-radius: var(--radius-sm, 6px); font-size: 12px;
}
.wtm-icon-btn:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.wtm-radio-group { display: flex; flex-direction: column; gap: 8px; }
.wtm-radio-row {
  display: flex; gap: 10px; align-items: flex-start; border: 1px solid var(--panel-border);
  padding: 10px; background: var(--surface-panel, var(--color-white)); cursor: pointer;
  border-radius: var(--radius-sm, 6px);
}
.wtm-radio-row.is-on { border-color: var(--color-primary); box-shadow: var(--frame-shadow); }
.wtm-rb {
  width: 16px; height: 16px; border: 1px solid var(--panel-border); flex: none; margin-top: 2px;
  display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 900;
  border-radius: 3px;
}
.wtm-radio-row.is-on .wtm-rb { background: var(--color-primary); color: var(--color-white); border-color: var(--color-primary); }
.wtm-radio-row b { display: block; font-size: 13px; }
.wtm-radio-row span:not(.wtm-rb) { font-size: 12px; color: var(--color-text-light); }
.wtm-reserved {
  border: 1px solid var(--color-warning, var(--color-error)); background: var(--surface-panel-subtle, #fff8e1);
  padding: 8px 10px; font-size: 12px; border-radius: var(--radius-sm, 6px); margin-bottom: 12px;
}
.wtm-err { color: var(--color-error); font-size: 12.5px; margin-top: 8px; }
</style>
