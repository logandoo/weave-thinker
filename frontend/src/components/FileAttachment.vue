<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div v-if="attachments && attachments.length" class="file-attachments">
    <template v-for="file in attachments" :key="attachmentKey(file)">
      <FolderAttachment v-if="file.type === 'folder'" :folder="file" />

      <div v-else-if="kindOf(file) === 'image'" class="image-card" title="点击查看大图" @click="openLightbox(file)">
        <img :src="targetUrl(file)" :alt="file.name" class="image-preview" loading="lazy" @error="handleImgError" />
        <div class="image-name">{{ file.name }}</div>
      </div>

      <div v-else-if="kindOf(file) === 'audio'" class="media-card">
        <div class="media-head">
          <span class="media-icon">🎵</span>
          <span class="file-name">{{ file.name }}</span>
          <button class="media-download" title="下载" @click="downloadFile(file)">⬇</button>
        </div>
        <audio :src="targetUrl(file)" controls preload="metadata" class="media-player"></audio>
      </div>

      <div v-else-if="kindOf(file) === 'video'" class="media-card">
        <div class="media-head">
          <span class="media-icon">🎬</span>
          <span class="file-name">{{ file.name }}</span>
          <button class="media-download" title="放大播放" @click="openLightbox(file)">⛶</button>
          <button class="media-download" title="下载" @click="downloadFile(file)">⬇</button>
        </div>
        <video :src="targetUrl(file)" controls playsinline preload="metadata" class="media-player media-video"></video>
      </div>

      <div v-else class="file-card" @click="onFileCardClick(file)">
        <div class="file-icon">{{ fileIcon(kindOf(file)) }}</div>
        <div class="file-info">
          <span class="file-name">{{ file.name }}</span>
          <span class="file-meta">
            <span class="file-type-badge">{{ fileTypeLabel(kindOf(file)) }}</span>
            <span class="file-size">{{ formatSize(file.size) }}</span>
          </span>
          <span
            v-if="kindOf(file) === 'unknown' && displayPath(file)"
            class="file-workspace-path"
            :title="displayPath(file)"
          >工作区路径: {{ displayPath(file) }}</span>
        </div>
        <button class="download-icon" title="下载" @click.stop="downloadFile(file)">⬇</button>
      </div>
    </template>
  </div>

  <MediaLightbox
    :media="lightboxFile"
    :kind="lightboxKind"
    :url="lightboxUrl"
    @close="lightboxFile = null"
    @download="downloadFile"
  />

  <FilePreviewDialog
    v-if="previewFile"
    :filename="previewFile.name"
    :url="targetUrl(previewFile)"
    :rel-path="relPathOf(previewFile)"
    :source-path="previewFile.rel_path || previewFile.path || null"
    :type="previewFile.type"
    @close="previewFile = null"
  />
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Attachment, FileAttachment } from '@/types'
import MediaLightbox from './MediaLightbox.vue'
import FilePreviewDialog from './FilePreviewDialog.vue'
import FolderAttachment from './FolderAttachment.vue'
import { buildDownloadUrl } from '@/api/workspaceFiles'
import { classifyFile, fileIcon, fileTypeLabel, formatSize, type FileKind } from '@/composables/filePreview'
import { downloadUrl } from '@/composables/useDownload'
import { useToast } from '@/composables/useToast'

const props = defineProps<{
  attachments: Attachment[]
}>()

const lightboxFile = ref<FileAttachment | null>(null)
const previewFile = ref<FileAttachment | null>(null)
const abortController = ref<AbortController | null>(null)
const { show: showToast } = useToast()

function kindOf(file: FileAttachment): FileKind {
  return classifyFile(file.name, file.type)
}

function relPathOf(file: FileAttachment): string {
  return file.rel_path || ''
}

function attachmentKey(file: Attachment): string {
  return file.rel_path || file.path || `${file.type}:${file.name}`
}

function displayPath(file: FileAttachment): string {
  return file.rel_path || ''
}

function targetUrl(file: FileAttachment): string {
  return buildDownloadUrl(file.rel_path || file.path || '')
}

function onFileCardClick(file: FileAttachment) {
  const kind = kindOf(file)
  if (kind === 'archive') {
    // Recognized but not previewable in-app: straight to download.
    downloadFile(file)
  } else {
    previewFile.value = file
  }
}

const lightboxKind = computed<'image' | 'video'>(() => {
  const f = lightboxFile.value
  if (!f) return 'image'
  return kindOf(f) === 'video' ? 'video' : 'image'
})

const lightboxUrl = computed(() => {
  const f = lightboxFile.value
  return f ? targetUrl(f) : ''
})

function handleImgError(ev: Event) {
  (ev.target as HTMLImageElement).style.display = 'none'
}

function openLightbox(file: FileAttachment) {
  lightboxFile.value = file
}

async function downloadFile(file: FileAttachment) {
  try {
    const url = targetUrl(file)

    abortController.value?.abort()
    abortController.value = new AbortController()

    const result = await downloadUrl(url, file.name, { signal: abortController.value.signal })
    if (!result.success) {
      console.error('Download failed:', result.error)
      if (result.error !== '下载已取消') {
        showToast(result.error || '下载失败', 'error')
      }
    }
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    if (e?.response?.status === 404) {
      console.warn('Download 404 for:', displayPath(file), '- file may have been cleaned up')
    } else {
      console.error('Download failed:', e)
    }
  }
}
</script>

<style scoped>
.file-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 8px 0;
}

.file-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
  max-width: 320px;
  min-width: 180px;
}

.file-card:hover {
  background: var(--surface-panel-strong);
  border-color: var(--panel-border-strong);
  box-shadow: var(--shadow-sm);
}

.media-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 14px;
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  max-width: 360px;
  min-width: 240px;
}

.media-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.media-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.media-head .file-name {
  flex: 1;
}

.media-download {
  border: none;
  background: transparent;
  color: var(--color-primary);
  font-size: 14px;
  cursor: pointer;
  padding: 2px 4px;
  flex-shrink: 0;
}

.media-player {
  width: 100%;
  height: 40px;
}

.media-video {
  height: auto;
  max-height: min(36vh, 300px);
  max-width: 320px;
  border-radius: var(--radius-sm);
  background: #000;
}

.image-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  cursor: zoom-in;
  transition: all var(--transition-fast);
  max-width: 320px;
}

.image-card:hover {
  border-color: var(--panel-border-strong);
  box-shadow: var(--shadow-md);
}

/* 小比例完整展示（不裁切）：contain 保持原比例。桌面端以高度为上限，
   保证 9:16 竖图在不滚动窗口的情况下全貌可见；移动端以宽度为上限，
   保证 16:9 横图全貌可见。点击卡片进入可缩放 lightbox 查看细节。 */
.image-preview {
  width: auto;
  height: auto;
  max-width: 320px;
  max-height: min(45vh, 420px);
  object-fit: contain;
  display: block;
  background: var(--surface-panel-subtle);
}

.image-name {
  max-width: 320px;
  width: 100%;
  box-sizing: border-box;
  padding: 4px 8px;
  font-size: 11px;
  color: var(--color-text-light);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  background: var(--surface-panel-subtle);
}

.file-icon {
  font-size: 24px;
  flex-shrink: 0;
}

.file-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
  gap: 2px;
}

.file-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}

.file-type-badge {
  font-size: 10px;
  font-weight: 600;
  color: var(--color-primary, #6B9E5A);
  background: var(--primary-tint);
  border-radius: 3px;
  padding: 1px 5px;
  letter-spacing: 0.3px;
  flex-shrink: 0;
}

.file-size {
  font-size: 11px;
  color: var(--color-text-light);
}

.file-workspace-path {
  font-size: 10px;
  color: var(--color-text-light);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
}

.download-icon {
  font-size: 16px;
  color: var(--color-primary);
  opacity: 1;
  padding: 0 2px;
  line-height: 1;
  flex-shrink: 0;
  transition: opacity var(--transition-fast);
}

.file-card:hover .download-icon {
  opacity: 1;
}

/* 手机模式（≤767px）：宽度为上限，16:9 横图/横视频全貌可见 */
@media (max-width: 767px) {
  .image-card {
    max-width: 76vw;
  }

  .image-preview {
    max-width: 76vw;
    max-height: 40vh;
  }

  .image-name {
    max-width: 76vw;
  }

  .media-card {
    max-width: 76vw;
    min-width: 0;
  }

  .media-video {
    max-width: 76vw;
    max-height: 36vh;
  }
}
</style>
