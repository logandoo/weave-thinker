<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div v-if="progress" class="task-progress">
    <div class="task-header">
      <div class="task-title">
        <span class="task-icon">📋</span>
        <span>{{ progress.plan_name }}</span>
      </div>
      <span class="task-badge" :class="statusClass">{{ statusLabel }}</span>
    </div>
    <div class="subtask-list">
      <div
        v-for="sub in progress.subtasks"
        :key="sub.id"
        class="subtask-item"
        :class="subtaskStatusClass(sub.status)"
      >
        <div class="subtask-status-icon">
          <span v-if="subtaskStatusClass(sub.status) === 'done'">✅</span>
          <span v-else-if="subtaskStatusClass(sub.status) === 'running'" class="spinner">⏳</span>
          <span v-else-if="subtaskStatusClass(sub.status) === 'error'">❌</span>
          <span v-else>⬜</span>
        </div>
        <div class="subtask-info">
          <span class="subtask-name">{{ sub.name }}</span>
          <span v-if="sub.goal" class="subtask-goal">{{ sub.goal }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TaskProgress, TaskSubtask } from '@/types'

const props = defineProps<{
  progress: TaskProgress | null
}>()

const statusClass = computed(() => {
  if (!props.progress) return 'pending'
  switch (props.progress.status) {
    case 'executing': return 'running'
    case 'completed': return 'done'
    case 'failed': return 'error'
    default: return 'pending'
  }
})

const statusLabel = computed(() => {
  if (!props.progress) return ''
  switch (props.progress.status) {
    case 'planning': return '规划中'
    case 'executing': return '执行中'
    case 'completed': return '已完成'
    case 'failed': return '出错'
    default: return '等待中'
  }
})

function subtaskStatusClass(status: TaskSubtask['status']) {
  switch (status) {
    case 'running': return 'running'
    case 'completed': return 'done'
    case 'failed': return 'error'
    default: return 'pending'
  }
}
</script>

<style scoped>
.task-progress {
  margin: 8px 0;
  padding: 10px 14px;
  background: var(--color-bg-secondary, #f0f2f5);
  border-radius: 10px;
  border-left: 3px solid var(--color-primary, #4A90D9);
}

.task-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.task-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary, #1f2937);
}

.task-icon {
  font-size: 14px;
}

.task-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 500;
}

.task-badge.running {
  background: #dbeafe;
  color: #2563eb;
}

.task-badge.done {
  background: #d1fae5;
  color: #059669;
}

.task-badge.error {
  background: #fee2e2;
  color: #dc2626;
}

.task-badge.pending {
  background: #e5e7eb;
  color: #6b7280;
}

.subtask-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.subtask-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  transition: background 0.2s;
}

.subtask-item.running {
  background: rgba(74, 144, 217, 0.08);
}

.subtask-item.done {
  background: rgba(5, 150, 105, 0.08);
}

.subtask-item.error {
  background: rgba(220, 38, 38, 0.08);
}

.subtask-status-icon {
  font-size: 14px;
  flex-shrink: 0;
  width: 20px;
  text-align: center;
}

.spinner {
  animation: spin-slow 2s linear infinite;
  display: inline-block;
}

@keyframes spin-slow {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.subtask-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.subtask-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-primary, #374151);
}

.subtask-goal {
  font-size: 11px;
  color: var(--color-text-secondary, #6b7280);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:root.dark .task-progress {
  background: var(--color-bg-secondary, #2a2a2a);
}

:root.dark .task-title {
  color: var(--color-text-primary, #e5e7eb);
}

:root.dark .subtask-name {
  color: var(--color-text-primary, #d1d5db);
}

:root.dark .task-badge.running {
  background: #1e3a5f;
  color: #93c5fd;
}

:root.dark .task-badge.done {
  background: #064e3b;
  color: #6ee7b7;
}

:root.dark .task-badge.error {
  background: #7f1d1d;
  color: #fca5a5;
}
</style>
