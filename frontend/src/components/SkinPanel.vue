<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="skin-panel">
    <!-- 明暗模式 -->
    <section class="skin-section">
      <h3 class="skin-section-title">明暗模式</h3>
      <div class="mode-switch" role="group" aria-label="明暗模式">
        <button
          class="mode-option"
          :class="{ active: !isDark }"
          @click="setMode('light')"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="4"/>
            <path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>
          </svg>
          浅色
        </button>
        <button
          class="mode-option"
          :class="{ active: isDark }"
          @click="setMode('dark')"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>
          </svg>
          深色
        </button>
      </div>
      <p v-if="syncState === 'failed'" class="skin-sync-note skin-sync-failed">
        云端同步失败，已在本设备生效
      </p>
    </section>

    <!-- 皮肤卡片 -->
    <section class="skin-section">
      <div class="skin-section-head">
        <h3 class="skin-section-title">皮肤</h3>
        <button class="skin-upload-btn" :disabled="uploading" @click="toggleUploadForm">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="17 8 12 3 7 8"/>
            <line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
          上传皮肤
        </button>
      </div>

      <!-- 上传表单（wave-11：开发者皮肤） -->
      <div v-if="showUploadForm" class="upload-form">
        <div class="upload-row">
          <label class="upload-label" for="skin-css-file">皮肤文件（.css）</label>
          <input
            id="skin-css-file"
            ref="fileInputRef"
            type="file"
            accept=".css,text/css"
            class="upload-file"
            :disabled="uploading"
            @change="onFilePicked"
          />
        </div>
        <div class="upload-row">
          <label class="upload-label" for="skin-name-input">名称</label>
          <input
            id="skin-name-input"
            v-model="uploadName"
            type="text"
            class="upload-text"
            :disabled="uploading"
            placeholder="默认取文件名"
          />
        </div>
        <div class="upload-row">
          <label class="upload-label" for="skin-desc-input">描述</label>
          <input
            id="skin-desc-input"
            v-model="uploadDesc"
            type="text"
            class="upload-text"
            :disabled="uploading"
            placeholder="可选，一句话气质"
          />
        </div>
        <p v-if="uploadError" class="upload-error">{{ uploadError }}</p>
        <div class="upload-actions">
          <button class="upload-cancel" :disabled="uploading" @click="toggleUploadForm(false)">取消</button>
          <button class="upload-confirm" :disabled="uploading || !uploadFile" @click="startUpload">
            {{ uploading ? '上传中…' : '上传' }}
          </button>
        </div>
      </div>

      <div class="skin-grid">
        <div
          v-for="skin in skins"
          :key="skin.id"
          class="skin-card"
          :class="{ active: skin.id === currentSkinId }"
          :data-skin-card="skin.id"
          role="button"
          tabindex="0"
          :aria-label="`选择皮肤 ${skin.name}`"
          @click="selectSkin(skin.id)"
          @keydown.enter.self="selectSkin(skin.id)"
          @keydown.space.self.prevent="selectSkin(skin.id)"
        >
          <button
            v-if="skin.source === 'uploaded'"
            class="skin-delete"
            :title="`删除皮肤 ${skin.name}`"
            :aria-label="`删除皮肤 ${skin.name}`"
            :disabled="deletingId === skin.id"
            @click.stop="deleteSkin(skin.id)"
            @keydown.enter.stop="deleteSkin(skin.id)"
            @keydown.space.stop.prevent
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
          </button>
          <span class="skin-preview">
            <span class="sw sw-bg" :style="{ background: skin.preview.bg }"/>
            <span class="sw sw-surface" :style="{ background: skin.preview.surface }"/>
            <span class="sw sw-primary" :style="{ background: skin.preview.primary }"/>
            <span class="sw sw-text" :style="{ background: skin.preview.text }"/>
            <span class="sw sw-accent" :style="{ background: skin.preview.accent }"/>
          </span>
          <span class="skin-meta">
            <span class="skin-name">
              {{ skin.name }}
              <span v-if="skin.isDefault" class="skin-badge default">默认</span>
              <span v-else-if="skin.source === 'uploaded'" class="skin-badge custom">自定义</span>
              <span v-if="skin.id === currentSkinId" class="skin-badge using">使用中</span>
            </span>
            <span class="skin-desc">{{ skin.description }}</span>
          </span>
        </div>
      </div>
    </section>

    <p class="skin-footer">
      想设计自己的皮肤？按《皮肤设计与接入指南》开发 CSS 后直接上传。
      内置目录经 <code>GET /api/skins</code> 暴露，上传皮肤仅本人可见可选。
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useSkinStore, type SkinMode } from '@/stores/skin'
import type { SkinMeta } from '@/config/skins'
import { deleteMySkin, uploadSkin } from '@/api/skins'

defineProps<{ embedded?: boolean }>()
defineEmits<{ (e: 'close'): void }>()

const skinStore = useSkinStore()
const toast = useToast()
const { confirm: showConfirm } = useConfirmDialog()
const skins = computed<SkinMeta[]>(() => skinStore.allSkins)
const currentSkinId = computed(() => skinStore.skinId)
const isDark = computed(() => skinStore.isDark)
const syncState = computed(() => skinStore.syncState)

// 上传表单状态
const showUploadForm = ref(false)
const fileInputRef = ref<HTMLInputElement | null>(null)
const uploadFile = ref<File | null>(null)
const uploadName = ref('')
const uploadDesc = ref('')
const uploading = ref(false)
const uploadError = ref('')
const deletingId = ref('')

onMounted(() => {
  // 面板打开时确保上传皮肤列表已刷新（登录态）
  void skinStore.loadUploaded()
})

function toggleUploadForm(next?: boolean) {
  showUploadForm.value = typeof next === 'boolean' ? next : !showUploadForm.value
  if (!showUploadForm.value) uploadError.value = ''
}

function onFilePicked(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  uploadFile.value = file
  uploadError.value = ''
  if (file && !uploadName.value) {
    uploadName.value = file.name.replace(/\.css$/i, '')
  }
}

async function startUpload() {
  if (!uploadFile.value) return
  uploading.value = true
  uploadError.value = ''
  try {
    const entry = await uploadSkin(uploadFile.value, uploadName.value || undefined, uploadDesc.value || undefined)
    await skinStore.loadUploaded()
    skinStore.setSkin(entry.id)
    toast.show(`皮肤「${entry.name}」上传成功并已应用`, 'success')
    toggleUploadForm(false)
    uploadFile.value = null
    uploadName.value = ''
    uploadDesc.value = ''
    if (fileInputRef.value) fileInputRef.value.value = ''
  } catch (err: any) {
    uploadError.value = err?.response?.data?.detail || '上传失败，请检查 .css 文件（需含 [data-skin="文件同名"] 锚点，≤300KB）'
  } finally {
    uploading.value = false
  }
}

async function deleteSkin(id: string) {
  const skin = skins.value.find(s => s.id === id)
  // 应用内确认框（window.confirm 在 Android WebView 默认返回 false → 删除永久失效）
  const ok = await showConfirm({
    message: `删除皮肤「${skin?.name ?? id}」？此操作不可恢复。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  deletingId.value = id
  try {
    await deleteMySkin(id)
    skinStore.removeSkin(id)
    toast.show('皮肤已删除', 'success')
  } catch {
    toast.show('删除失败', 'error')
  } finally {
    deletingId.value = ''
  }
}

function selectSkin(id: string) {
  skinStore.setSkin(id)
}

function setMode(mode: SkinMode) {
  skinStore.setMode(mode)
}
</script>

<style scoped>
.skin-panel {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 22px;
}

.skin-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.skin-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.skin-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-light);
  margin: 0;
}

.skin-upload-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-primary);
  background: transparent;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background-color var(--transition-fast), border-color var(--transition-fast);
}

.skin-upload-btn:hover:not(:disabled) {
  background: var(--primary-tint);
  border-color: var(--color-primary);
}

.skin-upload-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.upload-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  background: var(--surface-panel-subtle);
}

.upload-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.upload-label {
  flex-shrink: 0;
  width: 84px;
  font-size: 12px;
  color: var(--color-text-light);
}

.upload-file {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--color-text);
}

.upload-text {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  font-size: 13px;
  color: var(--color-text);
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.upload-text:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--focus-ring-color);
}

.upload-error {
  margin: 0;
  font-size: 12px;
  color: var(--color-error);
  background: var(--danger-tint);
  padding: 8px 10px;
  border-radius: var(--radius-sm);
}

.upload-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.upload-cancel,
.upload-confirm {
  padding: 6px 14px;
  font-size: 13px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.upload-cancel {
  background: var(--surface-panel-strong);
  color: var(--color-text);
  border: 1px solid var(--panel-border);
}

.upload-cancel:hover:not(:disabled) {
  background: var(--color-hover);
}

.upload-confirm {
  background: var(--color-primary);
  color: #ffffff;
  border: 1px solid var(--color-primary);
}

.upload-confirm:hover:not(:disabled) {
  filter: brightness(1.06);
}

.upload-cancel:disabled,
.upload-confirm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.mode-switch {
  display: inline-flex;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  background: var(--surface-panel-subtle);
  width: fit-content;
}

.mode-option {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 18px;
  font-size: 13px;
  color: var(--color-text-light);
  border-radius: calc(var(--radius-md) - 2px);
  transition: background var(--transition-fast), color var(--transition-fast);
}

.mode-option:hover {
  color: var(--color-text);
}

.mode-option.active {
  background: var(--color-primary);
  color: #ffffff;
}

.skin-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
}

.skin-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  text-align: left;
  background: var(--surface-panel-strong);
  border: 2px solid var(--panel-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--panel-shadow);
  cursor: pointer;
  transition: border-color var(--transition-fast), transform var(--transition-fast),
              box-shadow var(--transition-fast);
}

.skin-card:hover {
  transform: translateY(-2px);
}

.skin-card:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.skin-card.active {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary) 18%, transparent);
}

.skin-delete {
  position: absolute;
  top: 6px;
  right: 6px;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  color: var(--color-text-light);
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--transition-fast), color var(--transition-fast);
}

.skin-card:hover .skin-delete,
.skin-delete:focus-visible {
  opacity: 1;
}

.skin-delete:hover:not(:disabled) {
  color: var(--color-error);
  border-color: var(--color-error);
  background: var(--danger-tint);
}

.skin-delete:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.skin-preview {
  display: flex;
  height: 52px;
  border-radius: calc(var(--radius-lg) - 4px);
  overflow: hidden;
  border: 1px solid var(--panel-border);
}

.sw {
  flex: 1;
}

.sw-primary,
.sw-accent {
  flex: 1.4;
}

.skin-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.skin-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
}

.skin-badge {
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  padding: 3px 6px;
  border-radius: 999px;
}

.skin-badge.default {
  color: var(--color-info);
  border: 1px solid currentColor;
}

.skin-badge.custom {
  color: var(--color-warning, #c8871c);
  border: 1px solid currentColor;
}

.skin-badge.using {
  color: #ffffff;
  background: var(--color-primary);
}

.skin-desc {
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-light);
}

.skin-footer {
  font-size: 12px;
  color: var(--color-text-light);
  margin: 0;
}

.skin-footer code {
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
}

.skin-sync-note {
  font-size: 12px;
  margin: 0;
}

.skin-sync-failed {
  color: var(--color-error);
}

@media (max-width: 767px) {
  .skin-grid {
    grid-template-columns: 1fr;
  }
}
</style>
