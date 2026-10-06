<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div v-if="visible" class="zen-notes-drawer-overlay" @click="$emit('close')"></div>
  <div v-if="visible" class="zen-notes-drawer">
    <div class="drawer-header">
      <span class="drawer-title">笔记列表</span>
      <div class="drawer-actions">
        <button class="drawer-action-btn" @click="panelRef?.openCreateNotebook()" title="新建笔记本">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            <line x1="12" y1="11" x2="12" y2="17"/>
            <line x1="9" y1="14" x2="15" y2="14"/>
          </svg>
        </button>
        <button class="drawer-action-btn" @click="panelRef?.openNewNote()" title="新建笔记">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
            <line x1="12" y1="18" x2="12" y2="12"/>
            <line x1="9" y1="15" x2="15" y2="15"/>
          </svg>
        </button>
        <button class="drawer-close" @click="$emit('close')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
    </div>
    <NotesPanelInline
      ref="panelRef"
      :active-notebook-id="zenStore.currentNotebookId || ''"
      :active-note-id="zenStore.currentNoteId || ''"
      @open-note="onSelectNote"
      @open-notebook="onOpenNotebook"
      @open-home="onOpenHome"
      @note-created="onNoteCreated"
      @active-note-deleted="onActiveNoteDeleted"
      @active-notebook-deleted="onActiveNotebookDeleted"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useZenStore } from '@/stores/zen'
import NotesPanelInline from './NotesPanelInline.vue'

/**
 * 工作台笔记侧栏（抽屉壳）：树/菜单/对话框/划动与独立模式侧栏共用
 * NotesPanelInline（样式与能力单一来源）；本壳只负责 overlay、抽屉头与
 * zen 导航语义（selectNote 交给 ZenMode 做未保存拦截）。
 */
defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
  selectNote: [notebookId: string, noteId: string]
}>()

const zenStore = useZenStore()
const panelRef = ref<InstanceType<typeof NotesPanelInline> | null>(null)

function onSelectNote(notebookId: string, noteId: string) {
  emit('selectNote', notebookId, noteId)
}

function onOpenNotebook(notebookId: string) {
  // 工作台无笔记本页：语义=展开该笔记本（loadNotesPanelNotebook）
  panelRef.value?.expandNotebook(notebookId)
}

function onOpenHome() {
  // 工作台无笔记本首页：语义=清空当前笔记选择（右侧回空态）
  zenStore.setCurrentNote('', '')
}

function onNoteCreated(notebookId: string, noteId: string) {
  emit('selectNote', notebookId, noteId)
}

function onActiveNoteDeleted(notebookId: string) {
  if (zenStore.currentNoteId) {
    zenStore.setCurrentNote(notebookId, '')
  }
}

function onActiveNotebookDeleted() {
  zenStore.setCurrentNote('', '')
}
</script>

<style scoped>
.zen-notes-drawer-overlay {
  position: fixed;
  inset: 0;
  z-index: 250;
  background: rgba(0, 0, 0, 0.3);
}

.zen-notes-drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 320px;
  z-index: 260;
  background-color: var(--surface-panel-strong);
  border-left: 1px solid var(--panel-border);
  box-shadow: -4px 0 24px rgba(90, 130, 60, 0.1);
  display: flex;
  flex-direction: column;
  animation: zenNotesSlideIn 0.25s ease;
}

@keyframes zenNotesSlideIn {
  from {
    transform: translateX(100%);
    opacity: 0;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}

.drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
}

.drawer-title {
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text);
}

.drawer-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.drawer-action-btn {
  padding: 6px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.drawer-action-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.drawer-close {
  padding: 6px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.drawer-close:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

/* 树面板占满剩余高度（NotesPanelInline 自带滚动） */
.zen-notes-drawer :deep(.notes-panel-inline) {
  flex: 1;
  min-height: 0;
}
</style>
