<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div class="folder-browser-overlay" @click.self="close">
      <div class="folder-browser-dialog" role="dialog" aria-modal="true">
        <div class="folder-browser-header">
          <div class="folder-browser-heading">
            <span class="folder-browser-icon">📁</span>
            <div class="folder-browser-titles">
              <span class="folder-browser-title">{{ folder.name }}</span>
              <span class="folder-browser-subpath">{{ currentPath }}</span>
            </div>
          </div>
          <div class="folder-browser-actions">
            <button class="fb-action" title="刷新" @click="reload">刷新</button>
            <button class="fb-action fb-action--primary" title="下载整个文件夹 (zip)" @click="downloadFolder">
              下载文件夹
            </button>
            <button class="fb-close" aria-label="关闭" title="关闭" @click="close">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
        </div>

        <div class="folder-browser-breadcrumb">
          <button class="crumb" :disabled="currentPath === folder.rel_path" @click="navigate(folder.rel_path)">
            {{ folder.name }}
          </button>
          <template v-for="seg in segments" :key="seg.path">
            <span class="crumb-sep">/</span>
            <button
              class="crumb"
              :disabled="seg.path === currentPath"
              @click="navigate(seg.path)"
            >{{ seg.name }}</button>
          </template>
        </div>

        <div class="folder-browser-body">
          <div v-if="loading" class="fb-status">加载中...</div>
          <div v-else-if="error" class="fb-status fb-status--error">
            <p>{{ error }}</p>
            <button class="fb-action fb-action--primary" @click="reload">重试</button>
          </div>
          <div v-else-if="entries.length === 0" class="fb-status">此文件夹为空</div>

          <div v-else class="fb-list">
            <div
              v-for="entry in entries"
              :key="entry.rel_path"
              class="fb-row"
              :class="{ 'fb-row--dir': entry.is_dir }"
              @click="onEntryClick(entry)"
            >
              <span class="fb-entry-icon">{{ entry.is_dir ? '📁' : fileIcon(entry.type || classifyFile(entry.name)) }}</span>
              <span class="fb-entry-name" :title="entry.name">{{ entry.name }}</span>
              <span class="fb-entry-type">{{ entry.is_dir ? '文件夹' : fileTypeLabel(classifyFile(entry.name, entry.type)) }}</span>
              <span class="fb-entry-size">{{ entry.is_dir ? '—' : formatSize(entry.size) }}</span>
              <span class="fb-entry-mtime">{{ formatMtime(entry.mtime) }}</span>
              <span class="fb-entry-actions">
                <button
                  class="fb-entry-download"
                  title="下载"
                  @click.stop="downloadEntry(entry)"
                >⬇</button>
              </span>
            </div>
          </div>
          <div v-if="listing && listing.truncated" class="fb-truncated">
            条目过多，仅展示前 {{ entries.length }} 项
          </div>
        </div>

        <FilePreviewDialog
          v-if="previewing"
          :filename="previewing.name"
          :url="buildDownloadUrl(previewing.rel_path)"
          :rel-path="previewing.rel_path"
          :source-path="previewing.rel_path"
          :type="previewing.type"
          @close="previewing = null"
        />
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { FolderAttachment } from '@/types'
import type { WorkspaceEntry } from '@/api/workspaceFiles'
import { buildDownloadUrl, buildZipUrl, fetchDirectory, formatMtime } from '@/api/workspaceFiles'
import { classifyFile, fileIcon, fileTypeLabel, formatSize } from '@/composables/filePreview'
import { downloadUrl } from '@/composables/useDownload'
import FilePreviewDialog from './FilePreviewDialog.vue'

const props = defineProps<{ folder: FolderAttachment }>()
const emit = defineEmits<{ close: [] }>()

const rootPath = computed(() => props.folder.rel_path || props.folder.path || '')
const currentPath = ref(rootPath.value)
const listing = ref<Awaited<ReturnType<typeof fetchDirectory>> | null>(null)
const loading = ref(false)
const error = ref('')
const previewing = ref<WorkspaceEntry | null>(null)
let abortController: AbortController | null = null

const entries = computed(() => listing.value?.entries ?? [])

const segments = computed(() => {
  const base = rootPath.value
  if (!base) return []
  const rest = currentPath.value.startsWith(base)
    ? currentPath.value.slice(base.length)
    : ''
  const parts = rest.split('/').filter(Boolean)
  const result: { name: string; path: string }[] = []
  let acc = base
  for (const part of parts) {
    acc = `${acc}/${part}`
    result.push({ name: part, path: acc })
  }
  return result
})

async function load() {
  if (!currentPath.value) {
    error.value = '文件夹路径缺失'
    return
  }
  abortController?.abort()
  abortController = new AbortController()
  loading.value = true
  error.value = ''
  try {
    listing.value = await fetchDirectory(currentPath.value, abortController.signal)
  } catch (e: any) {
    if (e?.name === 'CanceledError' || e?.code === 'ERR_CANCELED') return
    error.value = e?.response?.status === 404 ? '文件夹不存在或无权限' : '加载失败'
  } finally {
    loading.value = false
  }
}

function navigate(path: string) {
  if (!path || path === currentPath.value) return
  currentPath.value = path
  load()
}

function reload() {
  load()
}

function onEntryClick(entry: WorkspaceEntry) {
  if (entry.is_dir) {
    navigate(entry.rel_path)
  } else {
    previewing.value = entry
  }
}

async function downloadEntry(entry: WorkspaceEntry) {
  await downloadUrl(buildDownloadUrl(entry.rel_path), entry.name)
}

async function downloadFolder() {
  await downloadUrl(buildZipUrl(currentPath.value), `${props.folder.name || 'folder'}.zip`)
}

function close() {
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (previewing.value) {
    previewing.value = null
  } else {
    close()
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  load()
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  abortController?.abort()
})
</script>

<style scoped>
.folder-browser-overlay {
  position: fixed;
  inset: 0;
  background-color: var(--overlay-scrim);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.folder-browser-dialog {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow);
  border-radius: var(--radius-xl);
  width: min(820px, 94vw);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.folder-browser-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
}

.folder-browser-heading {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.folder-browser-icon {
  font-size: 22px;
  flex-shrink: 0;
}

.folder-browser-titles {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 2px;
}

.folder-browser-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-browser-subpath {
  font-size: 11px;
  color: var(--color-text-light);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-browser-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.fb-action {
  font-size: 12px;
  color: var(--color-text-light);
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--panel-border);
  background: transparent;
  cursor: pointer;
  transition: background-color var(--transition-fast), color var(--transition-fast);
}

.fb-action:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
  color: var(--color-text);
}

.fb-action--primary {
  color: var(--color-primary);
  border-color: color-mix(in srgb, var(--color-primary) 40%, transparent);
}

.fb-close {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.fb-close:hover {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
  color: var(--color-text);
}

.folder-browser-breadcrumb {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 20px;
  border-bottom: 1px solid var(--panel-border);
  flex-wrap: wrap;
  flex-shrink: 0;
}

.crumb {
  font-size: 12px;
  color: var(--color-primary-dark);
  background: none;
  border: none;
  padding: 2px 4px;
  border-radius: 4px;
  cursor: pointer;
}

.crumb:disabled {
  color: var(--color-text);
  font-weight: 600;
  cursor: default;
}

.crumb-sep {
  font-size: 11px;
  color: var(--color-text-light);
}

.folder-browser-body {
  flex: 1;
  min-height: 200px;
  overflow: auto;
  padding: 8px 12px 16px;
}

.fb-status {
  padding: 40px 8px;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-light);
}

.fb-status--error {
  color: var(--color-error);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.fb-list {
  display: flex;
  flex-direction: column;
}

.fb-row {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) 72px 72px 110px 36px;
  align-items: center;
  gap: 6px;
  padding: 7px 8px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background-color var(--transition-fast);
}

.fb-row:hover {
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
}

.fb-entry-icon {
  font-size: 16px;
  text-align: center;
}

.fb-entry-name {
  font-size: 13px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fb-row--dir .fb-entry-name {
  font-weight: 500;
}

.fb-entry-type,
.fb-entry-size,
.fb-entry-mtime {
  font-size: 11px;
  color: var(--color-text-light);
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fb-entry-actions {
  text-align: right;
}

.fb-entry-download {
  font-size: 14px;
  color: var(--color-primary);
  background: none;
  border: none;
  padding: 2px 4px;
  cursor: pointer;
}

.fb-truncated {
  padding: 8px;
  font-size: 11px;
  color: var(--color-text-light);
  text-align: center;
}

@media (max-width: 767px) {
  .folder-browser-overlay {
    padding: 10px;
  }

  .folder-browser-dialog {
    width: 100%;
    max-height: 92vh;
  }

  .fb-row {
    grid-template-columns: 24px minmax(0, 1fr) 64px 32px;
  }

  .fb-entry-type,
  .fb-entry-mtime {
    display: none;
  }
}
</style>
