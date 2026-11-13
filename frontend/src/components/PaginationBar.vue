<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div v-if="totalPages > 1" class="pagination-bar">
    <button
      type="button"
      class="page-btn"
      :disabled="page <= 1"
      aria-label="上一页"
      @click="emit('update:page', page - 1)"
    >上一页</button>
    <span class="page-indicator" aria-live="polite">{{ page }} / {{ totalPages }}</span>
    <button
      type="button"
      class="page-btn"
      :disabled="page >= totalPages"
      aria-label="下一页"
      @click="emit('update:page', page + 1)"
    >下一页</button>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  page: number
  totalPages: number
}>()

const emit = defineEmits<{
  (e: 'update:page', value: number): void
}>()
</script>

<style scoped>
.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 8px 0 2px;
}

.page-btn {
  padding: 6px 14px;
  font-size: 12px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  background: var(--surface-panel-strong);
  color: var(--color-text);
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.page-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.page-btn:disabled {
  opacity: 0.45;
  cursor: default;
}

.page-indicator {
  font-size: 12px;
  color: var(--color-text-light);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
</style>
