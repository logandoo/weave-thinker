<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="notes-panel-inline">
    <!-- 宿主头（独立模式侧栏：笔记本标题+新建笔记；工作台抽屉头在壳层） -->
    <slot name="header" />
    <div class="notes-panel-loading" v-if="notesPanelLoading">加载中…</div>
    <div class="notes-panel-list" v-else>
           <!-- "首页" entry: same hierarchy as a notebook, jumps to the
             notebook-picker page so the user can switch notebooks quickly. -->
        <div
          class="np-notebook np-home"
          :class="{ active: isOnNotesRoot }"
          @click="goToNotebooksHome"
          role="button"
          tabindex="0"
          @keydown.enter.prevent="goToNotebooksHome"
          @keydown.space.prevent="goToNotebooksHome"
        >
          <div class="np-notebook-row">
            <svg class="np-chevron np-chevron-placeholder" width="13" height="13" viewBox="0 0 24 24" aria-hidden="true"></svg>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 12l2-2 7-7 7 7 2 2"/>
              <path d="M5 10v10a1 1 0 0 0 1 1h3v-6h6v6h3a1 1 0 0 0 1-1V10"/>
            </svg>
            <span class="np-nb-name">首页</span>
          </div>
        </div>
        <div
          class="np-notebook"
          v-for="nb in notesStore.notebooks"
          :key="nb.id"
        >
          <div
            class="np-swipe-wrap np-swipe-wrap--nb"
            :class="{ 'np-swipe-open': npSwipedKey === 'nb:' + nb.id }"
            @touchstart="npHandleTouchStart($event, 'nb:' + nb.id)"
            @touchend="npHandleTouchEnd()"
            @touchcancel="npHandleTouchEnd()"
            @touchmove="npHandleTouchMove($event, 'nb:' + nb.id)"
          >
            <div class="np-swipe-actions np-swipe-actions--nb">
              <button class="np-swipe-action rename" @click.stop="npSwipeRenameNotebook(nb)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                  <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                </svg>
                <span>重命名</span>
              </button>
              <button class="np-swipe-action delete" @click.stop="npSwipeDeleteNotebook(nb)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
                <span>删除</span>
              </button>
            </div>
            <div
              class="np-notebook-row"
              :class="{ active: isActivePanelNotebook(nb.id) }"
              :style="npRowStyle('nb:' + nb.id)"
              @click="npRowClick('nb:' + nb.id, () => toggleNotesPanelNotebook(nb.id))"
              @dblclick="openNotebookInPanel(nb.id)"
            >
              <svg class="np-chevron" :class="{ expanded: !!notesPanelExpanded[nb.id] }" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="9 6 15 12 9 18"/>
              </svg>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
              </svg>
              <span class="np-nb-name">{{ nb.name }}</span>
              <span class="np-count">{{ nb.note_count }}</span>
              <button
                class="np-menu-btn hide-on-mobile"
                :class="{ active: npMenuId === 'nb:' + nb.id }"
                @click.stop="openNpNotebookMenu(nb, $event)"
                title="更多操作"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
              </button>
            </div>
          </div>
          <div class="np-notes-list" v-show="!!notesPanelExpanded[nb.id]">
            <div
              v-for="note in notesStore.notes[nb.id] || []"
              :key="note.id"
              class="np-swipe-wrap np-swipe-wrap--note"
              :class="{ 'np-swipe-open': npSwipedKey === 'note:' + note.id }"
              @touchstart="npHandleTouchStart($event, 'note:' + note.id)"
              @touchend="npHandleTouchEnd()"
              @touchcancel="npHandleTouchEnd()"
              @touchmove="npHandleTouchMove($event, 'note:' + note.id)"
            >
              <div class="np-swipe-actions np-swipe-actions--note">
                <button class="np-swipe-action rename" @click.stop="npSwipeRenameNote(note)">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                  </svg>
                  <span>重命名</span>
                </button>
                <button class="np-swipe-action move" @click.stop="npSwipeMoveNote(note)">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M5 12h14M12 5l7 7-7 7"/>
                  </svg>
                  <span>移动</span>
                </button>
                <button class="np-swipe-action delete" @click.stop="npSwipeDeleteNote(note)">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="3 6 5 6 21 6"/>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                  </svg>
                  <span>删除</span>
                </button>
              </div>
              <div
                class="np-note-row"
                :class="{ active: isActivePanelNote(nb.id, note.id) }"
                :style="npRowStyle('note:' + note.id)"
                @click="npRowClick('note:' + note.id, () => openNoteInPanel(nb.id, note.id))"
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                <span class="np-note-title">{{ note.title || '无标题' }}</span>
                <button
                  class="np-menu-btn hide-on-mobile"
                  :class="{ active: npMenuId === 'note:' + note.id }"
                  @click.stop="openNpNoteMenu(note, $event)"
                  title="更多操作"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>
                </button>
              </div>
            </div>
            <div v-if="!!notesPanelNotebookLoading[nb.id]" class="np-note-loading">加载中…</div>
            <div v-else-if="!(notesStore.notes[nb.id] || []).length" class="np-empty">暂无笔记</div>
          </div>
        </div>
        <div v-if="!notesStore.notebooks.length" class="np-empty">暂无笔记本</div>
      </div>

    <!-- Notes panel context menu (notebook: 重命名/删除；note: 重命名/移动到/删除，与工作台笔记菜单一致) -->
    <Teleport to="body">
      <div
        v-if="npMenuTarget"
        class="np-context-menu"
        :style="npMenuStyle"
        @click.stop
      >
        <template v-if="npMenuTarget.kind === 'notebook'">
          <button class="menu-item" @click="handleNpRenameNotebook">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            <span>重命名</span>
          </button>
          <button class="menu-item delete" @click="handleNpDeleteNotebook">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
            <span>删除</span>
          </button>
        </template>
        <template v-else>
          <button class="menu-item" @click="handleNpRenameNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            <span>重命名</span>
          </button>
          <button class="menu-item" @click="handleNpMoveNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              <line x1="12" y1="11" x2="12" y2="17"/>
              <line x1="9" y1="14" x2="15" y2="14"/>
            </svg>
            <span>移动到</span>
          </button>
          <div class="menu-divider"></div>
          <button class="menu-item delete" @click="handleNpDeleteNote">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
            <span>删除</span>
          </button>
        </template>
      </div>
    </Teleport>

    <!-- Rename notebook/note dialog (notes panel menu) -->
    <Teleport to="body">
      <div v-if="showNpRenameDialog" class="modal-overlay" @mousedown.self="showNpRenameDialog = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">{{ npRenameTarget?.kind === 'notebook' ? '重命名笔记本' : '重命名笔记' }}</h3>
          <div class="modal-body">
            <input
              ref="npRenameInputRef"
              v-model="npRenameValue"
              type="text"
              :placeholder="npRenameTarget?.kind === 'notebook' ? '输入笔记本名称' : '输入新标题'"
              @keyup.enter="confirmNpRename"
            />
          </div>
          <div class="modal-actions">
            <button class="modal-btn cancel" @click="showNpRenameDialog = false">取消</button>
            <button class="modal-btn confirm" @click="confirmNpRename">保存</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Move note dialog (notes panel menu) -->
    <Teleport to="body">
      <div v-if="showNpMoveDialog" class="modal-overlay" @mousedown.self="showNpMoveDialog = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">移动到笔记本</h3>
          <div class="modal-body">
            <div class="np-move-options">
              <button
                v-for="nb in notesStore.notebooks"
                :key="nb.id"
                class="np-move-option"
                :class="{ active: npMoveTargetNotebookId === nb.id }"
                @click="npMoveTargetNotebookId = nb.id"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                  <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                </svg>
                <span>{{ nb.name }}</span>
              </button>
            </div>
          </div>
          <div class="modal-actions">
            <button class="modal-btn cancel" @click="showNpMoveDialog = false">取消</button>
            <button class="modal-btn confirm" @click="confirmNpMove" :disabled="!npMoveTargetNotebookId">移动</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Create Notebook Dialog（工作台抽屉 header 能力） -->
    <Teleport to="body">
      <div v-if="showCreateNotebookDialog" class="modal-overlay" @mousedown.self="showCreateNotebookDialog = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">新建笔记本</h3>
          <div class="modal-body">
            <input
              ref="createNotebookInputRef"
              v-model="newNotebookName"
              type="text"
              placeholder="输入笔记本名称"
              @keyup.enter="confirmCreateNotebook"
            />
          </div>
          <div class="modal-actions">
            <button class="modal-btn cancel" @click="showCreateNotebookDialog = false">取消</button>
            <button class="modal-btn confirm" @click="confirmCreateNotebook">创建</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- New Note Picker -->
    <NotebookPicker
      v-if="showNewNotePicker"
      @close="showNewNotePicker = false"
      @select="handleNewNoteNotebookSelected"
    />
  </div>
</template>

<script setup lang="ts">

import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useNotesStore } from '@/stores/notes'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import NotebookPicker from './NotebookPicker.vue'

/**
 * 笔记树面板（独立模式侧栏 + 工作台笔记抽屉共享）。
 * 样式与能力单一来源：np-* 树 / 多笔记本同时展开 / 首页入口 / 右键菜单 /
 * 重命名·移动·删除对话框 / 新建笔记（NotebookPicker）/ 新建笔记本 / 行左划。
 * 导航解耦：宿主监听 open-note / open-notebook / open-home / note-created /
 * active-note-deleted / active-notebook-deleted 后自行路由（Sidebar）或写
 * zenStore（工作台）。active* 高亮由 props 传入。
 */
const props = defineProps<{
  activeNotebookId: string
  activeNoteId: string
}>()

const emit = defineEmits<{
  (e: 'open-note', notebookId: string, noteId: string): void
  (e: 'open-notebook', notebookId: string): void
  (e: 'open-home'): void
  (e: 'note-created', notebookId: string, noteId: string): void
  (e: 'active-note-deleted', notebookId: string): void
  (e: 'active-notebook-deleted'): void
}>()

const notesStore = useNotesStore()
const { show: showToast } = useToast()
const { confirm: showConfirm } = useConfirmDialog()

const showNewNotePicker = ref(false)

// ── notes-panel 行左划（移动端；与 agent 端会话卡交互一致） ──
const NP_SWIPE_WIDTH_NB = 120
const NP_SWIPE_WIDTH_NOTE = 168
let suppressNpClickUntil = 0
const npSwipedKey = ref<string | null>(null)
const npSwipeOffset = ref(0)
const npSwipeTrackingKey = ref<string | null>(null)
const npSwipeStartX = ref(0)
const npSwipeStartY = ref(0)
const npSwipeStartOffset = ref(0)
const npSwipeDragging = ref(false)

function npSwipeWidth(key: string): number {
  return key.startsWith('nb:') ? NP_SWIPE_WIDTH_NB : NP_SWIPE_WIDTH_NOTE
}

function npRowStyle(key: string): Record<string, string> {
  if (npSwipedKey.value === key) {
    return { transform: `translateX(${npSwipeOffset.value}px)` }
  }
  return {}
}

function closeNpSwipe() {
  npSwipedKey.value = null
  npSwipeOffset.value = 0
  npSwipeTrackingKey.value = null
  npSwipeDragging.value = false
}

function npHandleTouchStart(e: TouchEvent, key: string) {
  if (e.touches.length !== 1) return
  const touch = e.touches[0]
  npSwipeTrackingKey.value = key
  npSwipeStartX.value = touch.clientX
  npSwipeStartY.value = touch.clientY
  npSwipeStartOffset.value = npSwipedKey.value === key ? npSwipeOffset.value : 0
  npSwipeDragging.value = false
  if (npSwipedKey.value && npSwipedKey.value !== key) {
    closeNpSwipe()
  }
}

function npHandleTouchEnd() {
  if (!npSwipeTrackingKey.value) return
  if (npSwipeDragging.value) {
    suppressNpClickUntil = Date.now() + 300
    const width = npSwipeTrackingKey.value ? npSwipeWidth(npSwipeTrackingKey.value) : 0
    if (npSwipeOffset.value <= -width / 2 && npSwipeTrackingKey.value) {
      npSwipedKey.value = npSwipeTrackingKey.value
      npSwipeOffset.value = -width
    } else {
      closeNpSwipe()
      return
    }
  }
  npSwipeTrackingKey.value = null
  npSwipeDragging.value = false
}

function npHandleTouchMove(e: TouchEvent, key: string) {
  if (!npSwipeTrackingKey.value || npSwipeTrackingKey.value !== key || e.touches.length !== 1) {
    return
  }
  const touch = e.touches[0]
  const deltaX = touch.clientX - npSwipeStartX.value
  const deltaY = touch.clientY - npSwipeStartY.value
  if (!npSwipeDragging.value) {
    if (Math.abs(deltaY) > 10 && Math.abs(deltaY) > Math.abs(deltaX)) {
      npSwipeTrackingKey.value = null
      return
    }
    if (Math.abs(deltaX) < 10) return
    if (deltaX > 0 && npSwipeStartOffset.value === 0) {
      npSwipeTrackingKey.value = null
      return
    }
    npSwipeDragging.value = true
  }
  e.preventDefault()
  npSwipedKey.value = key
  npSwipeOffset.value = Math.max(-npSwipeWidth(key), Math.min(0, npSwipeStartOffset.value + deltaX))
}

function npRowClick(key: string, fn: () => void) {
  if (Date.now() < suppressNpClickUntil) return
  if (npSwipedKey.value === key) {
    closeNpSwipe()
    return
  }
  fn()
}

function npSwipeRenameNotebook(nb: { id: string; name: string }) {
  closeNpSwipe()
  npMenuTarget.value = { kind: 'notebook', id: nb.id, title: nb.name }
  handleNpRenameNotebook()
}

function npSwipeDeleteNotebook(nb: { id: string; name: string }) {
  closeNpSwipe()
  npMenuTarget.value = { kind: 'notebook', id: nb.id, title: nb.name }
  handleNpDeleteNotebook()
}

function npSwipeRenameNote(note: { id: string; title: string; notebook_id: string }) {
  closeNpSwipe()
  npMenuTarget.value = { kind: 'note', id: note.id, title: note.title || '', notebookId: note.notebook_id }
  handleNpRenameNote()
}

function npSwipeMoveNote(note: { id: string; title: string; notebook_id: string }) {
  closeNpSwipe()
  npMenuTarget.value = { kind: 'note', id: note.id, title: note.title || '', notebookId: note.notebook_id }
  handleNpMoveNote()
}

function npSwipeDeleteNote(note: { id: string; title: string; notebook_id: string }) {
  closeNpSwipe()
  npMenuTarget.value = { kind: 'note', id: note.id, title: note.title || '', notebookId: note.notebook_id }
  handleNpDeleteNote()
}


function goToNotebooksHome() {
  emit('open-home')
}

// "首页"行高亮：宿主未选中任何笔记本/笔记（如独立模式 /notes 根页）时点亮。
const isOnNotesRoot = computed(() => !props.activeNotebookId && !props.activeNoteId)

// Desktop inline notes panel — 面板数据（展开态持久化跨宿主共用一把 key）
const notesPanelLoading = ref(false)
const NOTES_PANEL_EXPANDED_KEY = 'chatllm_notes_panel_expanded_v2'
function _loadExpandedFromStorage(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(NOTES_PANEL_EXPANDED_KEY) || '{}')
  } catch {
    return {}
  }
}
const notesPanelExpanded = ref<Record<string, boolean>>(_loadExpandedFromStorage())
const notesPanelNotebookLoading = ref<Record<string, boolean>>({})

function _saveExpandedToStorage() {
  localStorage.setItem(NOTES_PANEL_EXPANDED_KEY, JSON.stringify(notesPanelExpanded.value))
}

function setNotesPanelExpanded(notebookId: string, expanded: boolean) {
  if (expanded) {
    notesPanelExpanded.value = { ...notesPanelExpanded.value, [notebookId]: true }
  } else {
    const next = { ...notesPanelExpanded.value }
    delete next[notebookId]
    notesPanelExpanded.value = next
  }
  _saveExpandedToStorage()
}

// When notebooks become available (loaded by NotebooksList or NotesList),
// ensure notes are loaded for expanded notebooks
watch(() => notesStore.notebooks.length, async (len, oldLen) => {
  if (len > 0 && (!oldLen || oldLen === 0)) {
    await _loadNotesPanelData()
  }
})

// 高亮跟随宿主导航：进入某笔记本时自动展开并拉取其笔记
watch(() => props.activeNotebookId, async (notebookId) => {
  if (!notebookId) return
  if (!notesStore.notebooks.length) await _loadNotesPanelData()
  if (!notesPanelExpanded.value[notebookId]) {
    setNotesPanelExpanded(notebookId, true)
  }
  await loadNotesPanelNotebook(notebookId)
})

async function _loadNotesPanelData() {
  if (!notesStore.notebooks.length) {
    await notesStore.loadNotebooks()
    if (!notesStore.notebooks.length) return
  }

  notesPanelLoading.value = true
  try {
    if (Object.keys(notesPanelExpanded.value).length === 0) {
      const firstId = notesStore.notebooks[0].id
      setNotesPanelExpanded(firstId, true)
    }
    const expandedIds = Object.keys(notesPanelExpanded.value).filter(k => notesPanelExpanded.value[k])
    const loadPromises = expandedIds
      .filter(id => !notesStore.notes[id] || notesStore.notes[id].length === 0)
      .map(id => loadNotesPanelNotebook(id))
    if (loadPromises.length > 0) {
      await Promise.all(loadPromises)
    }
  } finally {
    notesPanelLoading.value = false
  }
}

onMounted(() => {
  void _loadNotesPanelData()
})

async function loadNotesPanelNotebook(notebookId: string) {
  notesPanelNotebookLoading.value = { ...notesPanelNotebookLoading.value, [notebookId]: true }
  try {
    await notesStore.loadNotes(notebookId)
  } finally {
    const next = { ...notesPanelNotebookLoading.value }
    delete next[notebookId]
    notesPanelNotebookLoading.value = next
  }
}

async function toggleNotesPanelNotebook(notebookId: string) {
  if (notesPanelExpanded.value[notebookId]) {
    setNotesPanelExpanded(notebookId, false)
  } else {
    setNotesPanelExpanded(notebookId, true)
    await loadNotesPanelNotebook(notebookId)
  }
}

function openNotebookInPanel(notebookId: string) {
  emit('open-notebook', notebookId)
}

function openNoteInPanel(notebookId: string, noteId: string) {
  emit('open-note', notebookId, noteId)
}

function isActivePanelNotebook(notebookId: string) {
  return props.activeNotebookId === notebookId
}

function isActivePanelNote(notebookId: string, noteId: string) {
  return props.activeNotebookId === notebookId && props.activeNoteId === noteId
}

// ─── Notes panel context menu (notebook: 重命名/删除 · note: 重命名/移动到/删除) ───
const npMenuId = ref<string | null>(null)
const npMenuTarget = ref<{ kind: 'notebook' | 'note'; id: string; title: string; notebookId?: string } | null>(null)
const npMenuStyle = ref<{ top: string; left: string }>({ top: '0px', left: '0px' })

function openNpNotebookMenu(nb: { id: string; name: string }, event: MouseEvent) {
  if (npMenuId.value === 'nb:' + nb.id) {
    closeNpMenu()
    return
  }
  positionNpMenu(event)
  npMenuTarget.value = { kind: 'notebook', id: nb.id, title: nb.name }
  npMenuId.value = 'nb:' + nb.id
}

function openNpNoteMenu(note: { id: string; title: string | null; notebook_id: string }, event: MouseEvent) {
  if (npMenuId.value === 'note:' + note.id) {
    closeNpMenu()
    return
  }
  positionNpMenu(event)
  npMenuTarget.value = { kind: 'note', id: note.id, title: note.title || '', notebookId: note.notebook_id }
  npMenuId.value = 'note:' + note.id
}

function positionNpMenu(event: MouseEvent) {
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const MENU_WIDTH = 140
  const PADDING = 8
  const GAP = 2
  let top = rect.bottom + GAP
  let left = rect.right - MENU_WIDTH
  if (left < PADDING) left = PADDING
  if (left + MENU_WIDTH > window.innerWidth - PADDING) {
    left = window.innerWidth - MENU_WIDTH - PADDING
  }
  npMenuStyle.value = { top: `${top}px`, left: `${left}px` }
  nextTick(() => {
    const menuEl = document.querySelector('.np-context-menu') as HTMLElement
    if (!menuEl) return
    const spaceBelow = window.innerHeight - rect.bottom - PADDING
    const spaceAbove = rect.top - PADDING
    if (menuEl.getBoundingClientRect().height > spaceBelow && spaceAbove > spaceBelow) {
      const newTop = Math.max(PADDING, rect.top - menuEl.getBoundingClientRect().height - GAP)
      npMenuStyle.value = { top: `${newTop}px`, left: `${left}px` }
    }
  })
}

function closeNpMenu() {
  npMenuId.value = null
  npMenuTarget.value = null
}

// Rename
const showNpRenameDialog = ref(false)
const npRenameValue = ref('')
const npRenameTarget = ref<{ kind: 'notebook' | 'note'; id: string; title: string; notebookId?: string } | null>(null)
const npRenameInputRef = ref<HTMLInputElement | null>(null)

function handleNpRenameNotebook() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t) return
  npRenameTarget.value = t
  npRenameValue.value = t.title
  showNpRenameDialog.value = true
  nextTick(() => npRenameInputRef.value?.focus())
}

function handleNpRenameNote() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t) return
  npRenameTarget.value = t
  npRenameValue.value = t.title
  showNpRenameDialog.value = true
  nextTick(() => npRenameInputRef.value?.focus())
}

async function confirmNpRename() {
  const title = npRenameValue.value.trim()
  const target = npRenameTarget.value
  if (!title || !target) {
    showNpRenameDialog.value = false
    npRenameTarget.value = null
    return
  }
  try {
    if (target.kind === 'notebook') {
      await notesStore.updateNotebook(target.id, title)
    } else {
      await notesStore.updateNote(target.id, { title })
      await loadNotesPanelNotebook(target.notebookId!)
    }
    showToast('已重命名', 'success')
  } catch (e) {
    console.error('Failed to rename:', e)
    showToast('重命名失败', 'error')
  }
  showNpRenameDialog.value = false
  npRenameTarget.value = null
}

// Move note
const showNpMoveDialog = ref(false)
const npMoveTarget = ref<{ id: string; title: string; notebookId: string } | null>(null)
const npMoveTargetNotebookId = ref('')

function handleNpMoveNote() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t || t.kind !== 'note') return
  npMoveTarget.value = { id: t.id, title: t.title, notebookId: t.notebookId! }
  npMoveTargetNotebookId.value = ''
  showNpMoveDialog.value = true
}

async function confirmNpMove() {
  const note = npMoveTarget.value
  if (!note || !npMoveTargetNotebookId.value) {
    showNpMoveDialog.value = false
    npMoveTarget.value = null
    return
  }
  const from = note.notebookId
  try {
    await notesStore.moveNote(note.id, npMoveTargetNotebookId.value)
    await loadNotesPanelNotebook(from)
    if (npMoveTargetNotebookId.value !== from) {
      await loadNotesPanelNotebook(npMoveTargetNotebookId.value)
    }
    showToast('已移动', 'success')
  } catch (e) {
    console.error('Failed to move note:', e)
    showToast('移动失败', 'error')
  }
  npMoveTarget.value = null
  showNpMoveDialog.value = false
}

// Delete notebook — 与笔记本主页(NotebooksList.handleDeleteNotebook)逻辑一致
async function handleNpDeleteNotebook() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t || t.kind !== 'notebook') return
  const nb = notesStore.notebooks.find(n => n.id === t.id)
  if (!nb) return
  if (nb.is_default) {
    showToast('默认笔记本不能删除', 'error')
    return
  }
  if (!await showConfirm({ message: '确定要删除这个笔记本吗？\n所有笔记也会被删除。', danger: true, confirmText: '删除' })) {
    return
  }
  try {
    await notesStore.deleteNotebook(t.id)
    const panel = { ...notesPanelExpanded.value }
    delete panel[t.id]
    notesPanelExpanded.value = panel
    if (props.activeNotebookId === t.id) {
      emit('active-notebook-deleted')
    }
    showToast('笔记本已删除', 'success')
  } catch (e) {
    console.error('Failed to delete notebook:', e)
    showToast('删除笔记本失败', 'error')
  }
}

// Delete note
async function handleNpDeleteNote() {
  const t = npMenuTarget.value
  closeNpMenu()
  if (!t || t.kind !== 'note') return
  if (!await showConfirm({ message: '确定要删除这条笔记吗？', danger: true, confirmText: '删除' })) return
  const noteId = t.id
  const from = t.notebookId!
  try {
    await notesStore.deleteNote(noteId)
    await loadNotesPanelNotebook(from)
    if (props.activeNoteId === noteId) {
      emit('active-note-deleted', from)
    }
    showToast('已删除', 'success')
  } catch (e) {
    console.error('Failed to delete note:', e)
    showToast('删除失败', 'error')
  }
}

function onNpMenuOutsideClick(e: MouseEvent) {
  if (!npMenuId.value) return
  const target = e.target as HTMLElement
  if (target.closest('.np-context-menu') || target.closest('.np-menu-btn')) return
  closeNpMenu()
}

onMounted(() => {
  document.addEventListener('click', onNpMenuOutsideClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onNpMenuOutsideClick)
})

// ── 新建笔记（NotebookPicker，与独立模式侧栏同款） ──
function startNewNote() {
  void notesStore.loadNotebooks()
  showNewNotePicker.value = true
}

async function handleNewNoteNotebookSelected(notebookId: string) {
  showNewNotePicker.value = false
  try {
    const note = await notesStore.createNote(notebookId, { content: '' })
    emit('note-created', notebookId, note.id)
  } catch (e) {
    console.error('Failed to create note:', e)
    showToast('创建笔记失败', 'error')
  }
}

// ── 新建笔记本（工作台抽屉能力；抽屉 header 经 defineExpose 调用） ──
const showCreateNotebookDialog = ref(false)
const newNotebookName = ref('')
const createNotebookInputRef = ref<HTMLInputElement | null>(null)

function openCreateNotebook() {
  newNotebookName.value = ''
  showCreateNotebookDialog.value = true
  nextTick(() => createNotebookInputRef.value?.focus())
}

async function confirmCreateNotebook() {
  const name = newNotebookName.value.trim()
  if (!name) {
    showCreateNotebookDialog.value = false
    return
  }
  try {
    const nb = await notesStore.createNotebook(name)
    showToast('笔记本已创建', 'success')
    setNotesPanelExpanded(nb.id, true)
    await loadNotesPanelNotebook(nb.id)
  } catch (e) {
    console.error('Failed to create notebook:', e)
    showToast('创建失败', 'error')
  }
  showCreateNotebookDialog.value = false
}

async function expandNotebook(notebookId: string) {
  setNotesPanelExpanded(notebookId, true)
  await loadNotesPanelNotebook(notebookId)
}

defineExpose({
  openNewNote: startNewNote,
  openCreateNotebook,
  expandNotebook,
  refresh: _loadNotesPanelData,
})

</script>

<style scoped>
.notes-panel-inline {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background-color: var(--surface-panel-strong);
}

/* header chrome styles live in the host (Sidebar / ZenNotesDrawer) */
.notes-panel-loading {
  padding: 12px 14px;
  font-size: 12px;
  color: var(--color-text-light);
}

.notes-panel-list {
  padding: 4px 0;
}

.np-notebook { }

/* Desktop-only "首页" row — same visual hierarchy as a notebook row. */
.np-home {
  outline: none;
}
/* Ensure the home row stretches to the full sidebar width so the active
  background reads like the rest of the notebook tree. */
.np-notebook.np-home {
  display: block;
}
.np-home {
  margin: 0 8px 8px;
}
.np-home:focus-visible .np-notebook-row {
  background-color: var(--color-hover);
}
.np-home.active .np-notebook-row {
  background-color: color-mix(in srgb, var(--color-primary) 14%, var(--surface-panel-subtle));
  box-shadow: inset 0 0 0 1px var(--panel-border-strong);
  font-weight: 600;
}
.np-chevron-placeholder {
  visibility: hidden;
}

.np-notebook-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  margin: 0 8px;
  border-radius: var(--shell-workbench-radius);
  cursor: pointer;
  user-select: none;
}
.np-notebook-row:hover { background-color: var(--color-hover); }
.np-notebook-row.active {
  background-color: color-mix(in srgb, var(--color-primary) 14%, var(--surface-panel-subtle));
  box-shadow: inset 0 0 0 1px var(--panel-border-strong);
}
.np-notebook-row.active .np-nb-name {
  color: var(--color-primary);
}
.np-notebook-row.active .np-count {
  background: color-mix(in srgb, var(--color-primary) 16%, var(--surface-panel-strong));
  color: var(--color-primary);
}

.np-chevron {
  transition: transform var(--transition-fast);
  color: var(--color-text-light);
  flex-shrink: 0;
}
.np-chevron.expanded { transform: rotate(90deg); }

.np-nb-name {
  flex: 1;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  display: block;
}

.np-count {
  font-size: 11px;
  color: var(--color-text-light);
  background: var(--color-bg);
  padding: 1px 6px;
  border-radius: 10px;
  font-variant-numeric: tabular-nums;
  min-width: 18px;
  text-align: center;
}

.np-notes-list { padding-left: 18px; }

.np-note-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  cursor: pointer;
  border-radius: var(--shell-workbench-radius);
  margin: 5px 8px;
}
.np-note-row:hover { background-color: var(--color-hover); }
.np-note-row svg { flex-shrink: 0; color: var(--color-text-light); }
.np-note-row.active {
  background-color: color-mix(in srgb, var(--color-primary) 18%, var(--surface-panel-strong));
  box-shadow: inset 0 0 0 1px var(--panel-border-strong);
}
.np-note-row.active svg,
.np-note-row.active .np-note-title {
  color: var(--color-primary);
}
.np-note-row.active .np-note-title {
  font-weight: 600;
}

/* ── notes-panel 行左划（移动端；桌面零变化） ─────────────────── */
.np-swipe-wrap {
  position: relative;
}
.np-swipe-wrap--nb { margin: 0 8px; }
.np-swipe-wrap--note { margin: 5px 8px; }
.np-swipe-wrap .np-notebook-row,
.np-swipe-wrap .np-note-row { margin: 0; position: relative; z-index: 1; }

.np-swipe-actions {
  position: absolute;
  right: 0;
  top: 0;
  bottom: 0;
  display: flex;
  clip-path: inset(0 100% 0 0);
  pointer-events: none;
  transition: clip-path 0.3s cubic-bezier(0.25, 0.46, 0.45, 0.94);
  border-radius: var(--shell-workbench-radius);
  overflow: hidden;
}
.np-swipe-actions--nb { width: 120px; }
.np-swipe-actions--note { width: 168px; }
.np-swipe-wrap.np-swipe-open .np-swipe-actions {
  clip-path: inset(0 0 0 0);
  pointer-events: auto;
}

.np-swipe-action {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: #fff;
  font-size: 10px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  padding: 0;
  transition: transform 0.15s ease, filter 0.15s ease;
}
.np-swipe-action:active {
  transform: scale(0.94);
  filter: brightness(0.85);
}
.np-swipe-action.rename { background-color: var(--swipe-rename-bg); }
.np-swipe-action.move { background-color: var(--swipe-move-bg); }
.np-swipe-action.delete { background-color: var(--swipe-delete-bg); }

.np-note-title {
  font-size: 13px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  /* flex 子项默认 min-width:auto 不收缩 —— 长标题会把行撑破（溢出根因） */
  min-width: 0;
  display: block;
}

.np-note-loading, .np-empty {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--color-text-light);
}

.np-menu-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 3px 5px;
  opacity: 0;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  flex-shrink: 0;
}
.np-notebook-row:hover .np-menu-btn,
.np-note-row:hover .np-menu-btn,
.np-menu-btn.active {
  opacity: 1;
}
.np-menu-btn:hover {
  color: var(--color-text);
  background-color: var(--color-hover);
}
@media (hover: none) {
  .np-menu-btn { opacity: 1; }
}

.np-context-menu {
  position: fixed;
  background-color: var(--surface-panel-strong);
  border: var(--menu-border);
  border-radius: var(--menu-radius);
  box-shadow: var(--menu-shadow);
  z-index: 1000;
  min-width: 140px;
  padding: 4px 0;
}

.np-move-options {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 240px;
  overflow-y: auto;
}
.np-move-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: 13px;
  text-align: left;
  transition: background-color var(--transition-fast);
}
.np-move-option:hover {
  background-color: var(--color-hover);
}
.np-move-option.active {
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
}

</style>

<style scoped src="../styles/sidebar-panels.css"></style>
