<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div class="notebook-picker-overlay" @click.self="$emit('close')">
      <div class="notebook-picker">
        <div class="picker-header">
          <h3>选择目标笔记本</h3>
          <button class="close-btn" @click="$emit('close')">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="picker-body">
          <div
            v-for="nb in availableNotebooks"
            :key="nb.id"
            class="notebook-option"
            @click="$emit('select', nb.id)"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
            <span class="notebook-name">{{ nb.name }}</span>
            <span class="notebook-count">{{ nb.note_count }} 条</span>
            <span v-if="nb.is_default" class="default-badge">默认</span>
          </div>
          <div v-if="availableNotebooks.length === 0" class="empty-hint">
            没有其他笔记本可选
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useNotesStore } from '@/stores/notes'

const props = defineProps<{
  excludeNotebookId?: string
}>()

defineEmits<{
  select: [notebookId: string]
  close: []
}>()

const notesStore = useNotesStore()

const availableNotebooks = computed(() => {
  const filtered = notesStore.notebooks.filter(nb => nb.id !== props.excludeNotebookId)
  // Sort: default notebook first
  return filtered.sort((a, b) => (b.is_default ? 1 : 0) - (a.is_default ? 1 : 0))
})
</script>

<style scoped>
.notebook-picker-overlay {
  position: fixed;
  inset: 0;
  background-color: rgba(10, 18, 30, 0.28);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100;
  animation: fadeIn 0.2s ease-out;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.notebook-picker {
  width: 400px;
  max-height: 70vh;
  background: var(--surface-panel-strong);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: 1px solid var(--panel-border);
  border-radius: 30px;
  box-shadow: var(--frame-shadow);
  display: flex;
  flex-direction: column;
  animation: scaleIn 0.2s ease-out;
}

@keyframes scaleIn {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}

.picker-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--panel-border);
}

.picker-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.close-btn {
  padding: 4px;
  color: var(--color-text-light);
  transition: color var(--transition-fast);
  border-radius: var(--radius-sm);
}

.close-btn:hover {
  color: var(--color-text);
  background: var(--color-hover);
}

.picker-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.notebook-option {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--transition-fast);
  color: var(--color-text);
}

.notebook-option:hover {
  background-color: var(--color-hover);
}

.notebook-name {
  flex: 1;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.notebook-count {
  font-size: 12px;
  color: var(--color-text-light);
}

.default-badge {
  font-size: 11px;
  padding: 2px 6px;
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  border-radius: var(--radius-sm);
}

.empty-hint {
  padding: 24px;
  text-align: center;
  color: var(--color-text-light);
  font-size: 14px;
}

@media (max-width: 767px) {
  .notebook-picker-overlay {
    padding: 0;
    align-items: flex-end;
  }

  .notebook-picker {
    width: 100%;
    max-height: 60vh;
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    padding-bottom: env(safe-area-inset-bottom, 0);
  }
}
</style>
