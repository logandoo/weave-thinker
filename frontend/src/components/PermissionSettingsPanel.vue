<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="perm-settings-panel">
    <div class="perm-settings-header">
      <h3 class="perm-settings-title">权限设置</h3>
      <button v-if="!embedded" class="perm-settings-close" @click="onClose">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"/>
          <line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>
    <div class="perm-settings-body">
      <p class="perm-settings-hint">
        以下权限默认开启。关闭后，Agent 在执行对应的高风险操作前会先向您请求确认。
      </p>
      <label class="perm-settings-row">
        <input type="checkbox" v-model="local.terminal_execution" />
        <span class="perm-settings-label">允许终端命令执行</span>
        <span class="perm-settings-tag risk">高风险</span>
      </label>
      <label class="perm-settings-row">
        <input type="checkbox" v-model="local.note_create" />
        <span class="perm-settings-label">允许 Agent 新增笔记</span>
        <span class="perm-settings-tag risk">写入</span>
      </label>
      <label class="perm-settings-row">
        <input type="checkbox" v-model="local.note_edit" />
        <span class="perm-settings-label">允许 Agent 编辑笔记</span>
        <span class="perm-settings-tag risk">写入</span>
      </label>
      <label class="perm-settings-row">
        <input type="checkbox" v-model="local.note_delete" />
        <span class="perm-settings-label">允许 Agent 删除笔记</span>
        <span class="perm-settings-tag risk">删除</span>
      </label>
      <label class="perm-settings-row">
        <input type="checkbox" v-model="local.notebook_create" />
        <span class="perm-settings-label">允许 Agent 创建笔记本</span>
        <span class="perm-settings-tag risk">写入</span>
      </label>
      <label class="perm-settings-row">
        <input type="checkbox" v-model="local.notebook_edit" />
        <span class="perm-settings-label">允许 Agent 修改笔记本</span>
        <span class="perm-settings-tag risk">写入</span>
      </label>
      <label class="perm-settings-row">
        <input type="checkbox" v-model="local.notebook_delete" />
        <span class="perm-settings-label">允许 Agent 删除笔记本</span>
        <span class="perm-settings-tag risk">删除</span>
      </label>
    </div>
    <div class="perm-settings-actions">
      <button v-if="!embedded" class="perm-settings-btn cancel" @click="onClose">取消</button>
      <button class="perm-settings-btn save" @click="onSave" :disabled="saving">
        {{ saving ? '保存中...' : '保存' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

interface Permissions {
  terminal_execution: boolean
  note_create: boolean
  note_edit: boolean
  note_delete: boolean
  notebook_create: boolean
  notebook_edit: boolean
  notebook_delete: boolean
}

const props = defineProps<{
  embedded?: boolean
  permissions: Permissions
  saving?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'save', value: Permissions): void
}>()

const local = ref<Permissions>({ ...props.permissions })

watch(() => props.permissions, (val) => {
  local.value = { ...val }
}, { deep: true })

watch(() => props.embedded, () => {
  local.value = { ...props.permissions }
}, { immediate: true })

function onClose() {
  emit('close')
}

function onSave() {
  emit('save', { ...local.value })
}
</script>

<style scoped>
.perm-settings-panel {
  width: 100%;
  max-width: 420px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.perm-settings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  width: 100%;
}

.perm-settings-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.perm-settings-close {
  background: transparent;
  border: none;
  color: var(--color-text-light);
  cursor: pointer;
  padding: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.perm-settings-close:hover {
  color: var(--color-text);
}

.perm-settings-hint {
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
  margin: 0 0 16px 0;
  text-align: center;
}

.perm-settings-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-radius: 12px;
  cursor: pointer;
  transition: background 0.15s ease;
}

.perm-settings-row:hover {
  background: var(--surface-panel-subtle);
}

.perm-settings-row input[type="checkbox"] {
  width: 18px;
  height: 18px;
  accent-color: var(--color-primary);
  cursor: pointer;
  flex-shrink: 0;
}

.perm-settings-label {
  flex: 1;
  font-size: 14px;
  color: var(--color-text);
}

.perm-settings-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 999px;
  flex-shrink: 0;
}

.perm-settings-tag.risk {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}

.perm-settings-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
  margin-top: 20px;
  width: 100%;
}

.perm-settings-btn {
  min-width: 86px;
  padding: 10px 20px;
  border-radius: 16px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--transition-fast), opacity var(--transition-fast), background var(--transition-fast);
  border: 1px solid transparent;
}

.perm-settings-btn.cancel {
  background: var(--surface-panel-subtle);
  border-color: var(--panel-border);
  color: var(--color-text);
}

.perm-settings-btn.save {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: #fff;
  box-shadow: 0 8px 16px rgba(141, 104, 69, 0.18);
}

.perm-settings-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
