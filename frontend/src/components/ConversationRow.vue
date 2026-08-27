<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div
    class="conversation-row"
    :class="{ 
      'swipe-open': swipedConversationId === conv.id && !selectionModeActive,
      'swipe-dragging': isSwipeDragging && swipedConversationId === conv.id && !selectionModeActive
    }"
    @touchstart="$emit('swipeStart', $event, conv.id)"
    @touchend="$emit('swipeEnd')"
    @touchcancel="$emit('swipeEnd')"
    @touchmove="$emit('swipeMove', $event, conv.id)"
    @contextmenu.prevent
  >
    <div v-if="!selectionModeActive" class="conversation-actions">
      <button class="swipe-action export" @click.stop="$emit('swipeExport', conv.id)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        <span>导出</span>
      </button>
      <button class="swipe-action edit" @click.stop="$emit('swipeEdit', conv.id, conv.title || '')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
        </svg>
        <span>标题</span>
      </button>
      <button class="swipe-action move-group" @click.stop="$emit('swipeMoveGroup', conv.id)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          <line x1="12" y1="11" x2="12" y2="17"/>
          <line x1="9" y1="14" x2="15" y2="14"/>
        </svg>
        <span>分组</span>
      </button>
      <button class="swipe-action save-note" @click.stop="$emit('swipeSaveNote', conv.id, conv.title || '新对话')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
        </svg>
        <span>笔记</span>
      </button>
      <button class="swipe-action delete" @click.stop="$emit('swipeDelete', conv.id)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
        </svg>
        <span>删除</span>
      </button>
    </div>
    <div
      class="conversation-item"
      :class="{ active: conv.id === currentConversationId && !selectionModeActive }"
      :style="getConversationItemStyle(conv.id)"
      :data-id="conv.id"
      @click="$emit('click', conv.id)"
      @touchstart="$emit('longPressStart', conv)"
      @touchend="$emit('longPressEnd')"
      @touchcancel="$emit('longPressEnd')"
    >
      <input
        v-if="selectionModeActive"
        type="checkbox"
        class="export-checkbox selection-checkbox"
        :checked="selected"
        @click.stop
        @change="$emit('toggleSelect', conv.id)"
      />
      <template v-if="editingTitleId === conv.id && !selectionModeActive">
        <div class="title-edit-row" @click.stop>
          <input
            :value="editingTitle"
            class="title-input"
            @input="updateTitle"
            @keyup.enter="$emit('saveTitle', conv.id)"
            @keyup.escape="$emit('cancelEdit')"
            @click.stop
            ref="editInput"
            autofocus
          />
          <div class="title-edit-actions">
            <button class="title-edit-btn save" @mousedown.prevent @click.stop="$emit('saveTitle', conv.id)">保存</button>
            <button class="title-edit-btn cancel" @mousedown.prevent @click.stop="$emit('cancelEdit')">取消</button>
          </div>
        </div>
      </template>
      <template v-else>
        <span class="conversation-title">{{ conv.title || '新对话' }}</span>
        <button
          v-if="!selectionModeActive"
          class="menu-btn hide-on-mobile"
          @click.stop="$emit('toggleMenu', conv.id, $event)"
          title="更多操作"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="12" cy="5" r="2"/>
            <circle cx="12" cy="12" r="2"/>
            <circle cx="12" cy="19" r="2"/>
          </svg>
        </button>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick, watch } from 'vue'
import type { Conversation } from '@/types'

const props = defineProps<{
  conv: Conversation
  selectionModeActive: boolean
  selected: boolean
  editingTitleId: string | null
  editingTitle: string
  swipedConversationId: string | null
  swipeOffset: number
  isSwipeDragging: boolean
  currentConversationId: string | null
}>()

const emit = defineEmits<{
  click: [id: string]
  toggleSelect: [id: string]
  startEdit: [conv: { id: string; title: string }]
  saveTitle: [id: string]
  cancelEdit: []
  updateTitle: [value: string]
  swipeStart: [event: TouchEvent, id: string]
  swipeEnd: []
  swipeMove: [event: TouchEvent, id: string]
  swipeExport: [id: string]
  swipeEdit: [id: string, title: string]
  swipeSaveNote: [id: string, title: string]
  swipeDelete: [id: string]
  swipeMoveGroup: [id: string]
  toggleMenu: [id: string, event: MouseEvent]
  longPressStart: [conv: { id: string; title: string; group_id?: string | null }]
  longPressEnd: []
}>()

function getConversationItemStyle(id: string): Record<string, string> {
  if (props.selectionModeActive) return {}
  if (props.swipedConversationId === id) {
    return { transform: `translateX(${props.swipeOffset}px)` }
  }
  return {}
}

const editInput = ref<HTMLInputElement | null>(null)

function updateTitle(e: Event) {
  const value = (e.target as HTMLInputElement).value
  emit('updateTitle', value)
}

watch(() => props.editingTitleId, (newId) => {
  if (newId === props.conv.id) {
    nextTick(() => {
      editInput.value?.focus()
      editInput.value?.select()
    })
  }
})
</script>

<style scoped>
.conversation-row {
  position: relative;
  margin-bottom: 4px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  content-visibility: auto;
  contain-intrinsic-size: auto 44px;
  contain: layout style paint;
}

.conversation-actions {
  position: absolute;
  right: 0;
  top: 0;
  bottom: 0;
  width: 300px;
  display: flex;
  clip-path: inset(0 100% 0 0);
  pointer-events: none;
  transition: clip-path 0.3s cubic-bezier(0.25, 0.46, 0.45, 0.94);
}

.conversation-row.swipe-open .conversation-actions,
.conversation-row.swipe-dragging .conversation-actions {
  clip-path: inset(0 0 0 0);
}

.conversation-row.swipe-open .conversation-actions {
  pointer-events: auto;
}

.swipe-action {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: white;
  font-size: 11px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  padding: 0;
  transition: transform 0.15s ease, filter 0.15s ease;
}

.swipe-action:active {
  transform: scale(0.92);
  filter: brightness(0.85);
}

.swipe-action.export {
  background-color: var(--color-info);
}

.swipe-action.edit {
  background-color: var(--color-text-light);
}

.swipe-action.delete {
  background-color: var(--color-error);
}

.swipe-action.save-note {
  background-color: var(--color-success);
}

.swipe-action.move-group {
  background-color: #8b5cf6;
}

.conversation-item {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: transform 0.3s cubic-bezier(0.25, 0.46, 0.45, 0.94), background-color var(--transition-fast), box-shadow var(--transition-fast);
  background-color: transparent;
}

.conversation-item:hover {
  background-color: var(--color-hover);
}

.conversation-item.active {
  background-color: color-mix(in srgb, var(--color-primary) 10%, var(--color-white, #fff));
  box-shadow: inset 3px 0 0 var(--color-primary), var(--shadow-sm);
  font-weight: 500;
}

.conversation-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  cursor: text;
}

.title-input {
  flex: 1;
  min-width: 0;
  padding: 4px 8px;
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-sm);
  font-size: 13px;
  outline: none;
}

.title-edit-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.title-edit-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.title-edit-btn {
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  border: none;
  cursor: pointer;
}

.title-edit-btn.save {
  background-color: var(--color-primary);
  color: white;
}

.title-edit-btn.cancel {
  background-color: var(--color-bg);
  color: var(--color-text);
}

.export-checkbox {
  margin-right: 8px;
  cursor: pointer;
}

.menu-btn {
  display: inline-flex;
  opacity: 0;
  padding: 4px 6px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  flex-shrink: 0;
}

.conversation-item:hover .menu-btn {
  opacity: 1;
}

.menu-btn:hover {
  color: var(--color-text);
  background-color: var(--color-sidebar);
}

@media (max-width: 767px) {
  .hide-on-mobile {
    display: none !important;
  }

  .title-edit-row {
    flex-direction: column;
    align-items: stretch;
  }

  .title-edit-actions {
    justify-content: flex-end;
  }

  .conversation-actions {
    width: 250px;
  }

  .swipe-action {
    width: 50px;
    font-size: 10px;
    gap: 4px;
  }

  .swipe-action svg {
    width: 16px;
    height: 16px;
  }
}
</style>
