<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="device-exec-panel">
    <section class="device-exec-section">
      <h3 class="device-exec-title">最近远端执行</h3>
      <div v-if="items.length" class="exec-audit-list">
        <div v-for="item in pagedItems" :key="item.id" class="exec-audit-row">
          <span class="exec-online-dot" :class="{ online: item.ok }"></span>
          <span class="exec-audit-tool">{{ item.tool }}</span>
          <span class="exec-audit-meta">{{ item.ok ? `${item.duration_ms}ms` : (item.error || '失败') }}</span>
        </div>
        <PaginationBar v-model:page="currentPage" :total-pages="totalPages" />
      </div>
      <div v-else class="exec-audit-empty">暂无远端执行记录——在客户端设备上执行工具后在此显示</div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { clientDevicesApi, type ClientExecAuditItem } from '@/api/clientDevices'
import PaginationBar from './PaginationBar.vue'

defineProps<{ embedded?: boolean }>()

const PAGE_SIZE = 10
const items = ref<ClientExecAuditItem[]>([])
const currentPage = ref(1)

const totalPages = computed(() => Math.max(1, Math.ceil(items.value.length / PAGE_SIZE)))
const pagedItems = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return items.value.slice(start, start + PAGE_SIZE)
})

watch(totalPages, (pages) => {
  if (currentPage.value > pages) currentPage.value = pages
})

onMounted(async () => {
  try {
    // 审计端点单次最多 100 条；客户端分页保证条目不再一次性平铺。
    items.value = await clientDevicesApi.listExecutions(100)
  } catch {
    items.value = []
  }
})
</script>

<style scoped>
.device-exec-panel {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.device-exec-section { width: 100%; }

.device-exec-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0 0 10px;
}

.exec-audit-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.exec-audit-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--color-text-light);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  background-color: var(--surface-panel);
}

.exec-audit-tool {
  font-weight: 600;
  color: var(--color-text);
}

.exec-audit-meta {
  margin-left: auto;
  opacity: 0.8;
  overflow-wrap: anywhere;
  text-align: right;
}

.exec-audit-empty {
  padding: 12px 2px;
  font-size: 12px;
  color: var(--color-text-light);
  line-height: 1.6;
}

.exec-online-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-text-light);
  margin-right: 6px;
  vertical-align: middle;
  opacity: 0.5;
}

.exec-online-dot.online {
  background: #22c55e;
  opacity: 1;
}
</style>
