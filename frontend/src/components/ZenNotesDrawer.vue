<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div v-if="visible" class="zen-notes-drawer-overlay" @click="$emit('close')"></div>
    <div v-if="visible" class="zen-notes-drawer">
      <div class="drawer-header">
        <span class="drawer-title">笔记列表</span>
        <div class="drawer-actions">
          <button class="drawer-action-btn" @click="startCreateNotebook" title="新建笔记本">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              <line x1="12" y1="11" x2="12" y2="17"/>
              <line x1="9" y1="14" x2="15" y2="14"/>
            </svg>
          </button>
          <button class="drawer-action-btn" @click="startNewNote" title="新建笔记">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="12" y1="18" x2="12" y2="12"/>
              <line x1="9" y1="15" x2="15" y2="15"/>
            </svg>
          </button>
          <button class="drawer-close" @click="$emit('close')">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>

      <div class="drawer-body">
        <!-- Notebook selector -->
        <div class="notebook-list">
          <div
            v-for="nb in notesStore.notebooks"
            :key="nb.id"
            class="notebook-item"
            :class="{ active: expandedNotebook === nb.id }"
          >
            <div class="notebook-header" @click="toggleNotebook(nb.id)">
              <svg
                class="notebook-chevron"
                :class="{ expanded: expandedNotebook === nb.id }"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <polyline points="9 6 15 12 9 18"/>
              </svg>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
              </svg>
              <span class="notebook-name">{{ nb.name }}</span>
              <span class="notebook-count">{{ nb.note_count }}</span>
              <button
                class="notebook-menu-btn"
                :class="{ active: activeMenuId === 'nb:' + nb.id }"
                @click.stop="openNotebookMenu(nb, $event)"
                title="更多操作"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                  <circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/>
                </svg>
              </button>
            </div>

            <div v-if="expandedNotebook === nb.id" class="notebook-notes">
              <div v-if="loadingNotes.has(nb.id)" class="notes-loading">加载中...</div>
              <div
                v-for="note in notesStore.notes[nb.id] || []"
                :key="note.id"
                class="note-item"
                :class="{ active: note.id === currentNoteId }"
                @click="selectNote(nb.id, note.id)"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                <span class="note-title">{{ note.title || '无标题' }}</span>
                <button
                  class="note-menu-btn"
                  :class="{ active: activeMenuId === note.id }"
                  @click.stop="openNoteMenu(note, $event)"
                  title="更多操作"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="5" r="1"/>
                    <circle cx="12" cy="12" r="1"/>
                    <circle cx="12" cy="19" r="1"/>
                  </svg>
                </button>
              </div>
              <div v-if="!(notesStore.notes[nb.id] || []).length && !loadingNotes.has(nb.id)" class="notes-empty">
                暂无笔记
              </div>
            </div>
          </div>
          <div v-if="!notesStore.notebooks.length" class="drawer-empty">
            暂无笔记本
          </div>
        </div>
      </div>
    </div>

    <!-- Notebook Context Menu (重命名/删除，与笔记本主页/侧栏菜单删除逻辑一致) -->
    <Teleport to="body">
      <div v-if="menuTargetNotebook" class="notebook-context-menu" :style="menuStyle" @click.stop>
        <button class="menu-item" @click="handleRenameNotebook">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7"/>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
          <span>重命名</span>
        </button>
        <button class="menu-item delete" @click="handleDeleteNotebook">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          </svg>
          <span>删除</span>
        </button>
      </div>
    </Teleport>

    <!-- Note Context Menu -->
    <Teleport to="body">
      <div v-if="menuTargetNote" class="note-context-menu" :style="menuStyle" @click.stop>
        <button class="menu-item" @click="handleRenameNote">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
          <span>重命名</span>
        </button>
        <button class="menu-item" @click="handleMoveNote">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            <line x1="12" y1="11" x2="12" y2="17"/>
            <line x1="9" y1="14" x2="15" y2="14"/>
          </svg>
          <span>移动到</span>
        </button>
        <div class="menu-divider"></div>
        <button class="menu-item delete" @click="handleDeleteNote">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          </svg>
          <span>删除</span>
        </button>
      </div>
    </Teleport>

    <!-- Rename Note Dialog -->
    <Teleport to="body">
      <div v-if="showRenameDialog" class="modal-overlay" @mousedown.self="showRenameDialog = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">{{ pendingRenameNotebook ? '重命名笔记本' : '重命名笔记' }}</h3>
          <div class="modal-body">
            <input
              ref="renameInputRef"
              v-model="renameValue"
              type="text"
              :placeholder="pendingRenameNotebook ? '输入笔记本名称' : '输入新标题'"
              @keyup.enter="confirmRename"
            />
          </div>
          <div class="modal-actions">
            <button class="modal-btn cancel" @click="showRenameDialog = false">取消</button>
            <button class="modal-btn confirm" @click="confirmRename">保存</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Move Note Dialog -->
    <Teleport to="body">
      <div v-if="showMoveDialog" class="modal-overlay" @mousedown.self="showMoveDialog = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">移动到笔记本</h3>
          <div class="modal-body">
            <div class="notebook-options">
              <button
                v-for="nb in notesStore.notebooks"
                :key="nb.id"
                class="notebook-option"
                :class="{ active: selectedMoveNotebookId === nb.id }"
                @click="selectedMoveNotebookId = nb.id"
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
            <button class="modal-btn cancel" @click="showMoveDialog = false">取消</button>
            <button class="modal-btn confirm" @click="confirmMove" :disabled="!selectedMoveNotebookId">移动</button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Create Notebook Dialog -->
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
    <Teleport to="body">
      <div v-if="showNewNotePicker" class="modal-overlay" @mousedown.self="showNewNotePicker = false">
        <div class="modal-content" @click.stop>
          <h3 class="modal-title">选择笔记本</h3>
          <div class="modal-body">
            <div class="notebook-options">
              <button
                v-for="nb in notesStore.notebooks"
                :key="nb.id"
                class="notebook-option"
                :class="{ active: selectedNewNoteNotebookId === nb.id }"
                @click="selectedNewNoteNotebookId = nb.id"
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
            <button class="modal-btn cancel" @click="showNewNotePicker = false">取消</button>
            <button class="modal-btn confirm" @click="confirmNewNote" :disabled="!selectedNewNoteNotebookId">创建</button>
          </div>
        </div>
      </div>
    </Teleport>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useNotesStore } from '@/stores/notes'
import { useZenStore } from '@/stores/zen'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const props = defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
  selectNote: [notebookId: string, noteId: string]
}>()

const notesStore = useNotesStore()
const zenStore = useZenStore()
const { show: showToast } = useToast()
const { confirm: showConfirm } = useConfirmDialog()

const expandedNotebook = ref('')
const loadingNotes = ref<Set<string>>(new Set())

const currentNoteId = computed(() => zenStore.currentNoteId)

watch(() => props.visible, async (visible) => {
  if (visible) {
    if (!notesStore.notebooks.length) {
      await notesStore.loadNotebooks()
    }
    // Auto-expand current notebook
    if (zenStore.currentNotebookId) {
      expandedNotebook.value = zenStore.currentNotebookId
      await loadNotes(zenStore.currentNotebookId)
    }
  }
})

async function toggleNotebook(notebookId: string) {
  if (expandedNotebook.value === notebookId) {
    expandedNotebook.value = ''
  } else {
    expandedNotebook.value = notebookId
    await loadNotes(notebookId)
  }
}

async function loadNotes(notebookId: string) {
  loadingNotes.value = new Set([...loadingNotes.value, notebookId])
  try {
    await notesStore.loadNotes(notebookId)
  } finally {
    const s = new Set(loadingNotes.value)
    s.delete(notebookId)
    loadingNotes.value = s
  }
}

function selectNote(notebookId: string, noteId: string) {
  emit('selectNote', notebookId, noteId)
}

// Note / Notebook Menu
const activeMenuId = ref<string | null>(null)
const menuTargetNote = ref<{ id: string; title: string; notebook_id: string } | null>(null)
const menuTargetNotebook = ref<{ id: string; title: string } | null>(null)
const menuStyle = ref<{ top: string; left: string }>({ top: '0px', left: '0px' })

function positionMenu(event: MouseEvent) {
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const MENU_WIDTH = 140
  const PADDING = 8
  let top = rect.bottom + 4
  let left = rect.right - MENU_WIDTH
  if (left < PADDING) left = PADDING
  menuStyle.value = { top: `${top}px`, left: `${left}px` }
}

function openNoteMenu(note: { id: string; title: string | null; notebook_id: string }, event: MouseEvent) {
  if (activeMenuId.value === note.id) {
    closeNoteMenu()
    return
  }
  menuTargetNote.value = { id: note.id, title: note.title || '', notebook_id: note.notebook_id }
  menuTargetNotebook.value = null
  activeMenuId.value = note.id
  positionMenu(event)
}

function openNotebookMenu(nb: { id: string; name: string }, event: MouseEvent) {
  if (activeMenuId.value === 'nb:' + nb.id) {
    closeNoteMenu()
    return
  }
  menuTargetNotebook.value = { id: nb.id, title: nb.name }
  menuTargetNote.value = null
  activeMenuId.value = 'nb:' + nb.id
  positionMenu(event)
}

function closeNoteMenu() {
  activeMenuId.value = null
  menuTargetNote.value = null
  menuTargetNotebook.value = null
}

function onDocumentClick(e: MouseEvent) {
  if (!activeMenuId.value) return
  const target = e.target as HTMLElement
  if (target.closest('.note-context-menu') || target.closest('.notebook-context-menu') || target.closest('.note-menu-btn') || target.closest('.notebook-menu-btn')) return
  closeNoteMenu()
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
})

// Rename
const showRenameDialog = ref(false)
const renameValue = ref('')
const renameInputRef = ref<HTMLInputElement | null>(null)
const pendingRenameNote = ref<{ id: string; title: string; notebook_id: string } | null>(null)
const pendingRenameNotebook = ref<{ id: string; title: string } | null>(null)

function handleRenameNote() {
  const note = menuTargetNote.value
  closeNoteMenu()
  if (!note) return
  pendingRenameNote.value = note
  pendingRenameNotebook.value = null
  renameValue.value = note.title
  showRenameDialog.value = true
  nextTick(() => renameInputRef.value?.focus())
}

function handleRenameNotebook() {
  const nb = menuTargetNotebook.value
  closeNoteMenu()
  if (!nb) return
  pendingRenameNotebook.value = nb
  pendingRenameNote.value = null
  renameValue.value = nb.title
  showRenameDialog.value = true
  nextTick(() => renameInputRef.value?.focus())
}

async function confirmRename() {
  const name = renameValue.value.trim()
  const note = pendingRenameNote.value
  const nb = pendingRenameNotebook.value
  if (!name) {
    showRenameDialog.value = false
    pendingRenameNote.value = null
    pendingRenameNotebook.value = null
    return
  }
  try {
    if (nb) {
      await notesStore.updateNotebook(nb.id, name)
    } else if (note) {
      await notesStore.updateNote(note.id, { title: name })
      delete notesStore.notes[note.notebook_id]
      await loadNotes(note.notebook_id)
    }
    showToast('已重命名', 'success')
  } catch (e) {
    console.error('Failed to rename:', e)
    showToast('重命名失败', 'error')
  }
  pendingRenameNote.value = null
  pendingRenameNotebook.value = null
  showRenameDialog.value = false
}

// Delete notebook — 与笔记本主页(NotebooksList.handleDeleteNotebook)逻辑一致
async function handleDeleteNotebook() {
  const nb = menuTargetNotebook.value
  closeNoteMenu()
  if (!nb) return
  const notebook = notesStore.notebooks.find(item => item.id === nb.id)
  if (!notebook) return
  if (notebook.is_default) {
    showToast('默认笔记本不能删除', 'error')
    return
  }
  if (!await showConfirm({ message: '确定要删除这个笔记本吗？\n所有笔记也会被删除。', danger: true, confirmText: '删除' })) {
    return
  }
  try {
    await notesStore.deleteNotebook(nb.id)
    delete notesStore.notes[nb.id]
    if (expandedNotebook.value === nb.id) expandedNotebook.value = ''
    if (zenStore.currentNotebookId === nb.id) {
      zenStore.setCurrentNote('', '')
      emit('selectNote', '', '')
    }
    showToast('笔记本已删除', 'success')
  } catch (e) {
    console.error('Failed to delete notebook:', e)
    showToast('删除笔记本失败', 'error')
  }
}

// Move
const showMoveDialog = ref(false)
const selectedMoveNotebookId = ref('')
const pendingMoveNote = ref<{ id: string; title: string; notebook_id: string } | null>(null)

function handleMoveNote() {
  const note = menuTargetNote.value
  closeNoteMenu()
  if (!note) return
  pendingMoveNote.value = note
  selectedMoveNotebookId.value = ''
  showMoveDialog.value = true
}

async function confirmMove() {
  const note = pendingMoveNote.value
  if (!note || !selectedMoveNotebookId.value) {
    showMoveDialog.value = false
    pendingMoveNote.value = null
    return
  }
  try {
    await notesStore.moveNote(note.id, selectedMoveNotebookId.value)
    showToast('已移动', 'success')
    if (note.notebook_id === expandedNotebook.value) {
      await loadNotes(expandedNotebook.value)
    }
    if (selectedMoveNotebookId.value === expandedNotebook.value) {
      await loadNotes(selectedMoveNotebookId.value)
    }
  } catch (e) {
    console.error('Failed to move note:', e)
    showToast('移动失败', 'error')
  }
  pendingMoveNote.value = null
  showMoveDialog.value = false
}

// Delete
async function handleDeleteNote() {
  const note = menuTargetNote.value
  closeNoteMenu()
  if (!note) return
  if (!await showConfirm({ message: '确定要删除这条笔记吗？', danger: true, confirmText: '删除' })) return
  try {
    await notesStore.deleteNote(note.id)
    delete notesStore.notes[note.notebook_id]
    await loadNotes(note.notebook_id)
    if (note.id === zenStore.currentNoteId) {
      const notes = notesStore.notes[note.notebook_id] || []
      if (notes.length > 0) {
        emit('selectNote', note.notebook_id, notes[0].id)
      } else {
        zenStore.setCurrentNote('', '')
      }
    }
    showToast('已删除', 'success')
  } catch (e) {
    console.error('Failed to delete note:', e)
    showToast('删除失败', 'error')
  }
}

// Create Notebook
const showCreateNotebookDialog = ref(false)
const newNotebookName = ref('')
const createNotebookInputRef = ref<HTMLInputElement | null>(null)

function startCreateNotebook() {
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
    expandedNotebook.value = nb.id
    await loadNotes(nb.id)
  } catch (e) {
    console.error('Failed to create notebook:', e)
    showToast('创建失败', 'error')
  }
  showCreateNotebookDialog.value = false
}

// New Note
const showNewNotePicker = ref(false)
const selectedNewNoteNotebookId = ref('')

function startNewNote() {
  selectedNewNoteNotebookId.value = zenStore.currentNotebookId || ''
  showNewNotePicker.value = true
}

async function confirmNewNote() {
  if (!selectedNewNoteNotebookId.value) {
    showNewNotePicker.value = false
    return
  }
  try {
    const note = await notesStore.createNote(selectedNewNoteNotebookId.value, { content: '' })
    emit('selectNote', selectedNewNoteNotebookId.value, note.id)
    showToast('笔记已创建', 'success')
    if (expandedNotebook.value === selectedNewNoteNotebookId.value) {
      await loadNotes(selectedNewNoteNotebookId.value)
    }
  } catch (e) {
    console.error('Failed to create note:', e)
    showToast('创建笔记失败', 'error')
  }
  showNewNotePicker.value = false
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
  animation: slideIn 0.25s ease;
}

@keyframes slideIn {
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

.drawer-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
}

.notebook-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.notebook-item {
  border-radius: var(--radius-md);
  overflow: hidden;
}

.notebook-item.active {
  background-color: var(--color-hover);
}

.notebook-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: background-color var(--transition-fast);
}

.notebook-header:hover {
  background-color: var(--color-hover);
}

.notebook-chevron {
  transition: transform 0.2s ease;
  color: var(--color-text-light);
  flex-shrink: 0;
}

.notebook-chevron.expanded {
  transform: rotate(90deg);
}

.notebook-name {
  flex: 1;
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.notebook-count {
  font-size: 11px;
  color: var(--color-text-light);
  background-color: var(--color-hover);
  padding: 1px 6px;
  border-radius: 10px;
  flex-shrink: 0;
}

.notebook-menu-btn {
  padding: 3px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  opacity: 0;
  transition: all var(--transition-fast);
  flex-shrink: 0;
  display: inline-flex;
}

.notebook-header:hover .notebook-menu-btn,
.notebook-menu-btn.active {
  opacity: 1;
}

.notebook-menu-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

@media (hover: none) {
  .notebook-menu-btn {
    opacity: 1;
  }
}

.notebook-notes {
  padding: 2px 0 4px 32px;
}

.note-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background-color var(--transition-fast);
}

.note-item:hover {
  background-color: var(--color-hover);
}

.note-item.active {
  background-color: color-mix(in srgb, var(--color-primary) 15%, transparent);
  color: var(--color-primary);
  border-left: 3px solid var(--color-primary);
  padding-left: 7px;
}

.note-title {
  flex: 1;
  font-size: 14px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.note-item.active .note-title {
  color: var(--color-primary);
  font-weight: 500;
}

.note-menu-btn {
  padding: 3px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  opacity: 0;
  transition: all var(--transition-fast);
  flex-shrink: 0;
}

.note-item:hover .note-menu-btn {
  opacity: 1;
}

.note-menu-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.note-menu-btn.active {
  opacity: 1;
  background-color: var(--color-hover);
  color: var(--color-text);
}

/* 源序需晚于 .note-menu-btn{opacity:0} 基础规则，否则 touch 下恒隐 */
@media (hover: none) {
  .note-menu-btn {
    opacity: 1;
  }
}

.notes-loading,
.notes-empty,
.drawer-empty {
  padding: 8px;
  font-size: 12px;
  color: var(--color-text-light);
  text-align: center;
}

/* Context Menu */
.note-context-menu {
  position: fixed;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 1000;
  min-width: 140px;
  padding: 4px 0;
}

.notebook-context-menu {
  position: fixed;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  z-index: 1000;
  min-width: 140px;
  padding: 4px 0;
}

.menu-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  color: var(--color-text);
  font-size: 13px;
  text-align: left;
  transition: background-color var(--transition-fast);
}

.menu-item:hover {
  background-color: var(--color-hover);
}

.menu-item.delete {
  color: var(--color-error);
}

.menu-item.delete:hover {
  background-color: rgba(229, 62, 62, 0.08);
}

.menu-divider {
  height: 1px;
  background-color: var(--color-border);
  margin: 4px 0;
}

/* Modals */
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 998;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
}

.modal-content {
  background: var(--color-white);
  border-radius: var(--radius-lg);
  padding: 24px;
  min-width: 320px;
  max-width: 90vw;
  box-shadow: 0 8px 32px rgba(90, 130, 60, 0.15);
}

.modal-title {
  margin: 0 0 16px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.modal-body {
  margin-bottom: 16px;
}

.modal-body input {
  width: 100%;
  padding: 10px 12px;
  font-size: 14px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  background-color: var(--color-bg);
  outline: none;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.modal-body input:focus {
  border-color: var(--color-primary);
  box-shadow: var(--input-container-shadow-focus, 0 0 0 3px rgba(122, 163, 90, 0.15));
}

.modal-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.modal-btn {
  padding: 8px 16px;
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 500;
  transition: all var(--transition-fast);
}

.modal-btn.cancel {
  background: none;
  border: 1px solid var(--color-border);
  color: var(--color-text-light);
}

.modal-btn.cancel:hover {
  background-color: var(--color-hover);
}

.modal-btn.confirm {
  background-color: var(--color-primary);
  color: white;
  border: none;
}

.modal-btn.confirm:hover:not(:disabled) {
  background-color: var(--color-primary-dark);
}

.modal-btn.confirm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.notebook-options {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 240px;
  overflow-y: auto;
}

.notebook-option {
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

.notebook-option:hover {
  background-color: var(--color-hover);
}

.notebook-option.active {
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
}
</style>
