<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="folder-card" title="点击浏览文件夹" @click="open = true">
    <div class="folder-icon">📁</div>
    <div class="folder-info">
      <span class="folder-name">{{ folder.name }}</span>
      <span class="folder-meta">
        <span class="folder-type-badge">文件夹</span>
        <span class="folder-count">{{ folder.file_count ?? 0 }} 个文件</span>
        <span v-if="folder.size" class="folder-size">{{ formatSize(folder.size) }}</span>
      </span>
    </div>
    <button class="folder-download" title="下载文件夹 (zip)" @click.stop="downloadZip">⬇</button>
  </div>
  <FolderBrowserDialog v-if="open" :folder="folder" @close="open = false" />
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { FolderAttachment } from '@/types'
import FolderBrowserDialog from './FolderBrowserDialog.vue'
import { buildZipUrl } from '@/api/workspaceFiles'
import { downloadUrl } from '@/composables/useDownload'
import { formatSize } from '@/composables/filePreview'

const props = defineProps<{ folder: FolderAttachment }>()

const open = ref(false)

async function downloadZip() {
  const rel = props.folder.rel_path || props.folder.path
  if (!rel) return
  await downloadUrl(buildZipUrl(rel), `${props.folder.name || 'folder'}.zip`)
}
</script>

<style scoped>
.folder-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
  max-width: 300px;
  min-width: 200px;
}

.folder-card:hover {
  background: var(--surface-panel-strong);
  border-color: var(--panel-border-strong);
  box-shadow: var(--shadow-sm);
}

.folder-icon {
  font-size: 24px;
  flex-shrink: 0;
}

.folder-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
  gap: 2px;
}

.folder-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}

.folder-type-badge {
  font-size: 10px;
  font-weight: 600;
  color: var(--color-primary, #6B9E5A);
  background: var(--primary-tint);
  border-radius: 3px;
  padding: 1px 5px;
  letter-spacing: 0.3px;
  flex-shrink: 0;
}

.folder-count,
.folder-size {
  font-size: 11px;
  color: var(--color-text-light);
}

.folder-download {
  font-size: 16px;
  color: var(--color-primary);
  padding: 0 2px;
  line-height: 1;
  flex-shrink: 0;
}

@media (max-width: 767px) {
  .folder-card {
    max-width: 76vw;
    min-width: 0;
  }
}
</style>
