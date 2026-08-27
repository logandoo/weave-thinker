<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="file-upload-overlay" @click.self="$emit('close')">
    <div class="file-upload-dialog">
      <div class="dialog-header">
        <h3>上传文件</h3>
        <button class="close-btn" @click="$emit('close')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <div class="dialog-body">
        <div
          class="drop-zone"
          :class="{ dragging: isDragging }"
          @dragenter.prevent="isDragging = true"
          @dragover.prevent="isDragging = true"
          @dragleave.prevent="isDragging = false"
          @drop.prevent="handleDrop"
          @click="triggerFileInput"
        >
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.5">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="17 8 12 3 7 8"/>
            <line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
          <p class="drop-text">点击或拖拽文件到此处</p>
          <p class="drop-hint">支持 Word、PPT、Excel、CSV、PDF、Markdown、音频（MP3/WAV/M4A/OGG）、视频（MP4/WebM/MOV）等；文件保存后由 AI 助手自主解析</p>
          <input
            ref="fileInputRef"
            type="file"
            multiple
            accept=".docx,.doc,.pptx,.ppt,.xlsx,.xls,.csv,.pdf,.md,.markdown,.mp3,.wav,.m4a,.ogg,.flac,.aac,.mp4,.webm,.mov,.m4v"
            style="display: none"
            @change="handleFileSelect"
          />
        </div>

        <div v-if="selectedFiles.length > 0" class="file-list">
          <div v-for="(f, idx) in selectedFiles" :key="idx" class="file-item">
            <span class="file-icon">{{ getFileIcon(f.name) }}</span>
            <span class="file-name">{{ f.name }}</span>
            <span class="file-size">{{ formatFileSize(f.size) }}</span>
            <button class="file-remove" @click="removeFile(idx)">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>
        </div>

        <label class="save-notebook-option" v-if="selectedFiles.length > 0">
          <input type="checkbox" v-model="saveToNotebook" />
          <span>同时保存到笔记</span>
          <span class="option-hint">（将新建笔记本，按文件保存为笔记）</span>
        </label>

        <div v-if="uploadProgress !== null" class="progress-bar">
          <div class="progress-fill" :style="{ width: uploadProgress + '%' }"></div>
          <span class="progress-text">上传中 {{ uploadProgress }}%</span>
        </div>

        <div v-if="parseResults.length > 0" class="parse-results">
          <div
            v-for="(r, idx) in parseResults"
            :key="idx"
            class="parse-result-item"
            :class="{ success: r.success, error: !r.success }"
          >
            <span class="result-icon">{{ r.success ? '✓' : '✗' }}</span>
            <div class="result-info">
              <span class="result-filename">{{ r.filename }}</span>
              <span v-if="r.error" class="result-error">{{ r.error }}</span>
              <span v-else class="result-type">已保存 ({{ formatFileSize(r.size || 0) }})</span>
            </div>
          </div>
        </div>
      </div>

      <div class="dialog-footer">
        <button class="btn-cancel" @click="$emit('close')" :disabled="uploading">取消</button>
        <button
          class="btn-upload"
          @click="handleUpload"
          :disabled="selectedFiles.length === 0 || uploading"
        >
          {{ uploading ? '上传中...' : '上传文件' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { fileUploadApi, type FileParseResult } from '@/api/fileUpload'
import { useToast } from '@/composables/useToast'
import { useNotesStore } from '@/stores/notes'

const emit = defineEmits<{
  close: []
  uploaded: [results: FileParseResult[], saveToNotebook: boolean]
}>()

const { show: showToast } = useToast()
const notesStore = useNotesStore()

const fileInputRef = ref<HTMLInputElement | null>(null)
const selectedFiles = ref<File[]>([])
const isDragging = ref(false)
const saveToNotebook = ref(false)
const uploading = ref(false)
const uploadProgress = ref<number | null>(null)
const parseResults = ref<FileParseResult[]>([])

function triggerFileInput() {
  fileInputRef.value?.click()
}

function handleFileSelect(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files) {
    addFiles(Array.from(input.files))
  }
  input.value = ''
}

function handleDrop(e: DragEvent) {
  isDragging.value = false
  if (e.dataTransfer?.files) {
    addFiles(Array.from(e.dataTransfer.files))
  }
}

function addFiles(newFiles: File[]) {
  const existing = new Set(selectedFiles.value.map(f => f.name))
  for (const f of newFiles) {
    if (!existing.has(f.name)) {
      selectedFiles.value.push(f)
    }
  }
}

function removeFile(idx: number) {
  selectedFiles.value.splice(idx, 1)
}

function getFileIcon(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  const icons: Record<string, string> = {
    docx: '📝', doc: '📝', pptx: '📊', ppt: '📊', xlsx: '📊', xls: '📊', csv: '📊', pdf: '📄',
    md: '📃', markdown: '📃',
  }
  return icons[ext] || '📎'
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1048576).toFixed(1)} MB`
}

async function handleUpload() {
  if (selectedFiles.value.length === 0 || uploading.value) return
  uploading.value = true
  uploadProgress.value = 0
  parseResults.value = []

  try {
    const response = await fileUploadApi.uploadFiles(
      selectedFiles.value,
      saveToNotebook.value,
      (percent) => { uploadProgress.value = percent },
    )

    parseResults.value = response.results

    const successCount = response.results.filter(r => r.success).length
    const failCount = response.results.filter(r => !r.success).length

    if (successCount > 0 && failCount === 0) {
      showToast(`${successCount} 个文件已保存`, 'success')
    } else if (successCount > 0 && failCount > 0) {
      showToast(`${successCount} 个已保存，${failCount} 个失败`, 'warning')
    } else {
      const firstError = response.results.find(r => r.error)?.error
      showToast(firstError || '所有文件保存失败', 'error')
    }

    if (saveToNotebook.value && response.notebook_id) {
      await notesStore.loadNotebooks()
    }

    emit('uploaded', response.results, saveToNotebook.value)
  } catch (e: any) {
    showToast(e?.message || '上传失败', 'error')
  } finally {
    uploading.value = false
    uploadProgress.value = null
  }
}
</script>

<style scoped>
.file-upload-overlay {
  position: fixed;
  inset: 0;
  background: rgba(10, 18, 30, 0.36);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  animation: fadeIn 0.2s ease-out;
}

.file-upload-dialog {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--frame-shadow);
  max-width: 520px;
  width: 100%;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: scaleIn 0.2s ease-out;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes scaleIn {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 24px;
  border-bottom: 1px solid var(--panel-border);
}

.dialog-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.close-btn {
  color: var(--color-text-light);
  padding: 4px;
  border-radius: var(--radius-sm);
}

.close-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.dialog-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.drop-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px 20px;
  border: 2px dashed var(--panel-border);
  border-radius: var(--radius-lg);
  cursor: pointer;
  transition: all 0.2s ease;
  background: var(--surface-panel-subtle);
}

.drop-zone:hover,
.drop-zone.dragging {
  border-color: var(--color-primary);
  background: rgba(53, 133, 197, 0.04);
}

.drop-text {
  font-size: 14px;
  color: var(--color-text);
  margin: 0;
}

.drop-hint {
  font-size: 12px;
  color: var(--color-text-light);
  margin: 0;
}

.file-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
}

.file-icon {
  font-size: 20px;
  flex-shrink: 0;
}

.file-name {
  flex: 1;
  font-size: 13px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-size {
  font-size: 11px;
  color: var(--color-text-light);
  flex-shrink: 0;
}

.file-remove {
  color: var(--color-text-light);
  padding: 2px;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.file-remove:hover {
  color: var(--color-error);
  background: rgba(224, 82, 82, 0.08);
}

.save-notebook-option {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
  padding: 8px 0;
}

.save-notebook-option input {
  cursor: pointer;
  accent-color: var(--color-primary);
}

.option-hint {
  font-size: 11px;
  color: var(--color-text-light);
}

.progress-bar {
  position: relative;
  height: 24px;
  background: var(--surface-panel-subtle);
  border-radius: var(--radius-pill);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: var(--radius-pill);
  transition: width 0.2s ease;
}

.progress-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 500;
  color: var(--color-text);
}

.parse-results {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.parse-result-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border-radius: var(--radius-md);
  font-size: 13px;
}

.parse-result-item.success {
  background: rgba(103, 194, 58, 0.06);
  border: 1px solid rgba(103, 194, 58, 0.15);
}

.parse-result-item.error {
  background: rgba(224, 82, 82, 0.06);
  border: 1px solid rgba(224, 82, 82, 0.15);
}

.result-icon {
  flex-shrink: 0;
  font-weight: 600;
  font-size: 14px;
}

.parse-result-item.success .result-icon {
  color: var(--color-success, #67c23a);
}

.parse-result-item.error .result-icon {
  color: var(--color-error);
}

.result-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.result-filename {
  font-weight: 500;
  color: var(--color-text);
}

.result-error {
  font-size: 12px;
  color: var(--color-error);
}

.result-type {
  font-size: 12px;
  color: var(--color-text-light);
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 24px;
  border-top: 1px solid var(--panel-border);
}

.btn-cancel {
  padding: 8px 20px;
  border-radius: var(--radius-pill);
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  font-size: 14px;
  font-weight: 500;
  transition: all var(--transition-fast);
}

.btn-cancel:hover {
  background: var(--color-hover);
}

.btn-upload {
  padding: 8px 20px;
  border-radius: var(--radius-pill);
  background: var(--color-primary);
  color: white;
  font-size: 14px;
  font-weight: 500;
  transition: all var(--transition-fast);
}

.btn-upload:hover:not(:disabled) {
  background: var(--color-primary-dark);
}

.btn-upload:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
