<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div class="note-picker-overlay" @click="$emit('close')">
      <div class="note-picker-modal" @click.stop>
        <div class="picker-header">
          <h3 class="picker-title">选择笔记引用</h3>
          <button class="close-btn" @click="$emit('close')">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        <div class="picker-search">
          <input
            v-model="searchQuery"
            class="search-input"
            placeholder="搜索笔记..."
            autofocus
          />
        </div>

        <div class="picker-content">
          <div
            class="notebook-section"
            v-for="notebook in filteredNotebooks"
            :key="notebook.id"
          >
            <div class="notebook-header">
              <label class="nb-check" @click.stop>
                <input
                  type="checkbox"
                  :checked="isNotebookFullySelected(notebook.id)"
                  @change="toggleNotebookSelection(notebook.id)"
                />
              </label>
              <div class="nb-label" @click="toggleNotebook(notebook.id)">
                <svg class="chevron" :class="{ expanded: expandedNotebooks.has(notebook.id) }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="6 9 12 15 18 9"/>
                </svg>
                <span class="notebook-name">{{ notebook.name }}</span>
                <span class="note-count">{{ notebook.note_count }}</span>
              </div>
            </div>

            <div class="notes-in-notebook" v-show="expandedNotebooks.has(notebook.id)">
              <div
                v-for="note in getNotesForNotebook(notebook.id)"
                :key="note.id"
                class="note-option"
                :class="{ selected: selectedIds.has(note.id) }"
                @click="toggleNote(note)"
              >
                <label class="note-check" @click.stop>
                  <input
                    type="checkbox"
                    :checked="selectedIds.has(note.id)"
                    @change="toggleNote(note)"
                  />
                </label>
                <div class="note-info">
                  <div class="note-title">{{ note.title || '无标题笔记' }}</div>
                  <div class="note-preview">{{ note.content_preview }}</div>
                </div>
              </div>
              <div v-if="notesLoading.has(notebook.id)" class="loading-notes">加载中…</div>
              <div v-else-if="!getNotesForNotebook(notebook.id).length" class="empty-notes">暂无笔记</div>
            </div>
          </div>
        </div>

        <div class="picker-footer">
          <div class="token-estimate" v-if="selectedNotes.length > 0">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="2" y="3" width="20" height="14" rx="2"/>
              <line x1="8" y1="21" x2="16" y2="21"/>
              <line x1="12" y1="17" x2="12" y2="21"/>
            </svg>
            <span>{{ selectedNotes.length }} 篇笔记 &nbsp;≈&nbsp;{{ estimatedTokens.toLocaleString() }} tokens (预估)</span>
          </div>
          <div class="footer-actions">
            <button class="cancel-btn" @click="$emit('close')">取消</button>
            <button
              class="confirm-btn"
              :disabled="selectedNotes.length === 0"
              @click="confirm"
            >
              引用选中 ({{ selectedNotes.length }})
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useNotesStore } from '@/stores/notes'
import type { NoteListItem } from '@/types'

const emit = defineEmits<{
  close: []
  selectMany: [notes: NoteListItem[]]
}>()

const notesStore = useNotesStore()
const searchQuery = ref('')
const expandedNotebooks = ref<Set<string>>(new Set())
const notesLoading = ref<Set<string>>(new Set())
const selectedIds = ref<Set<string>>(new Set())
const noteMap = ref<Record<string, NoteListItem>>({})

onMounted(async () => {
  await notesStore.loadNotebooks()
  if (notesStore.notebooks.length > 0) {
    const firstId = notesStore.notebooks[0].id
    expandedNotebooks.value.add(firstId)
    await loadNotebook(firstId)
  }
})

async function loadNotebook(notebookId: string) {
  const nb = notesStore.notebooks.find(n => n.id === notebookId)
  const cached = notesStore.notes[notebookId]
  if (cached && nb && cached.length >= (nb.note_count || 0)) {
    for (const n of cached) noteMap.value[n.id] = n
    return
  }
  notesLoading.value = new Set([...notesLoading.value, notebookId])
  try {
    await notesStore.loadNotes(notebookId)
    for (const n of notesStore.notes[notebookId] || []) noteMap.value[n.id] = n
  } finally {
    const s = new Set(notesLoading.value)
    s.delete(notebookId)
    notesLoading.value = s
  }
}

async function toggleNotebook(notebookId: string) {
  if (expandedNotebooks.value.has(notebookId)) {
    const s = new Set(expandedNotebooks.value)
    s.delete(notebookId)
    expandedNotebooks.value = s
  } else {
    expandedNotebooks.value = new Set([...expandedNotebooks.value, notebookId])
    await loadNotebook(notebookId)
  }
}

const filteredNotebooks = computed(() => {
  if (!searchQuery.value) return notesStore.notebooks
  const q = searchQuery.value.toLowerCase()
  return notesStore.notebooks.filter(nb => {
    const notes = notesStore.notes[nb.id] || []
    return nb.name.toLowerCase().includes(q) || notes.some(n =>
      (n.title?.toLowerCase().includes(q)) ||
      n.content_preview.toLowerCase().includes(q)
    )
  })
})

function getNotesForNotebook(notebookId: string): NoteListItem[] {
  const notes = notesStore.notes[notebookId] || []
  if (!searchQuery.value) return notes
  const q = searchQuery.value.toLowerCase()
  return notes.filter(n =>
    (n.title?.toLowerCase().includes(q)) ||
    n.content_preview.toLowerCase().includes(q)
  )
}

function toggleNote(note: NoteListItem) {
  noteMap.value[note.id] = note
  const ids = new Set(selectedIds.value)
  if (ids.has(note.id)) { ids.delete(note.id) } else { ids.add(note.id) }
  selectedIds.value = ids
}

function isNotebookFullySelected(notebookId: string): boolean {
  const notes = getNotesForNotebook(notebookId)
  return notes.length > 0 && notes.every(n => selectedIds.value.has(n.id))
}

function isNotebookPartiallySelected(notebookId: string): boolean {
  const notes = getNotesForNotebook(notebookId)
  if (!notes.length) return false
  const some = notes.some(n => selectedIds.value.has(n.id))
  const all = notes.every(n => selectedIds.value.has(n.id))
  return some && !all
}

function toggleNotebookSelection(notebookId: string) {
  const notes = getNotesForNotebook(notebookId)
  const ids = new Set(selectedIds.value)
  if (isNotebookFullySelected(notebookId)) {
    notes.forEach(n => ids.delete(n.id))
    selectedIds.value = ids
  } else {
    if (!expandedNotebooks.value.has(notebookId)) {
      expandedNotebooks.value = new Set([...expandedNotebooks.value, notebookId])
      loadNotebook(notebookId).then(() => {
        const freshIds = new Set(selectedIds.value)
        getNotesForNotebook(notebookId).forEach(n => { noteMap.value[n.id] = n; freshIds.add(n.id) })
        selectedIds.value = freshIds
      })
    } else {
      notes.forEach(n => { noteMap.value[n.id] = n; ids.add(n.id) })
      selectedIds.value = ids
    }
  }
}

const selectedNotes = computed<NoteListItem[]>(() =>
  [...selectedIds.value].map(id => noteMap.value[id]).filter(Boolean)
)

/** Per-note token count comes straight from the backend (server-side
 *  cl100k-style estimate based on each note's FULL content length), so
 *  we just sum the selected notes. Previously we multiplied the
 *  ~100-char preview by a guesswork factor of 5, which dramatically
 *  under-counted long notes (e.g. an 8 KB note showed up as ~148 tokens).
 *  Falls back to a per-note char-based estimate if the server didn't
 *  include the field (old backends or hand-inserted test rows). */
function tokenEstimateFor(n: NoteListItem): number {
  if (typeof n.token_estimate === 'number' && n.token_estimate > 0) {
    return n.token_estimate
  }
  if (typeof n.content_length === 'number' && n.content_length > 0) {
    // Conservative mixed-content fallback: ~1 token per 1.5 chars.
    return Math.ceil(n.content_length / 1.5)
  }
  // Last-resort heuristic from preview only — almost certainly wrong
  // for long notes, but matches the legacy behaviour so the UI at least
  // shows SOMETHING when the backend is out of date.
  return Math.ceil((n.content_preview?.length ?? 0) / 3.5)
}

const estimatedTokens = computed(() =>
  selectedNotes.value.reduce((sum, n) => sum + tokenEstimateFor(n), 0),
)

watch(searchQuery, async (q) => {
  if (!q) return
  for (const nb of notesStore.notebooks) {
    if (!notesStore.notes[nb.id]) await loadNotebook(nb.id)
    if (getNotesForNotebook(nb.id).length > 0) {
      expandedNotebooks.value = new Set([...expandedNotebooks.value, nb.id])
    }
  }
})

function confirm() {
  if (selectedNotes.value.length === 0) return
  emit('selectMany', selectedNotes.value)
  emit('close')
}
</script>

<style scoped>
.note-picker-overlay {
  position: fixed;
  inset: 0;
  background-color: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  animation: fadeIn 0.15s ease-out;
}
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

.note-picker-modal {
  background: var(--surface-panel-strong);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: 1px solid var(--panel-border);
  border-radius: var(--dialog-radius);
  width: 90%;
  max-width: 520px;
  max-height: 75vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--dialog-shadow);
}

.picker-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
}
.picker-title { font-size: 16px; font-weight: 600; color: var(--color-text); }
.close-btn { padding: 4px; color: var(--color-text-light); border-radius: var(--radius-sm); }
.close-btn:hover { background-color: var(--color-hover); }

.picker-search {
  padding: 12px 16px;
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
}
.search-input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  font-size: 14px;
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.search-input:focus { outline: none; border-color: var(--color-primary); box-shadow: 0 0 0 3px var(--focus-ring-color); }

.picker-content { flex: 1; overflow-y: auto; padding: 8px; }

.notebook-section { margin-bottom: 4px; }

.notebook-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  user-select: none;
}
.notebook-header:hover { background-color: var(--color-hover); }

.nb-check { display: flex; align-items: center; cursor: pointer; }
.nb-check input[type=checkbox] { width: 16px; height: 16px; cursor: pointer; accent-color: var(--color-primary); }

.nb-label { display: flex; align-items: center; gap: 6px; flex: 1; cursor: pointer; }

.chevron { transition: transform var(--transition-fast); color: var(--color-text-light); flex-shrink: 0; }
.chevron.expanded { transform: rotate(180deg); }

.notebook-name { flex: 1; font-size: 14px; font-weight: 500; color: var(--color-text); }
.note-count { font-size: 12px; color: var(--color-text-light); background-color: var(--surface-panel-subtle); padding: 2px 8px; border-radius: var(--radius-sm); }

.notes-in-notebook { padding-left: 20px; }

.note-option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  margin: 2px 0;
}
.note-option:hover { background-color: var(--color-hover); }
.note-option.selected { background-color: color-mix(in srgb, var(--color-primary) 10%, transparent); }

.note-check { display: flex; align-items: flex-start; padding-top: 2px; }
.note-check input[type=checkbox] { width: 15px; height: 15px; cursor: pointer; accent-color: var(--color-primary); }

.note-info { flex: 1; min-width: 0; }
.note-title { font-size: 14px; font-weight: 500; color: var(--color-text); margin-bottom: 2px; }
.note-preview { font-size: 12px; color: var(--color-text-light); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.loading-notes, .empty-notes { padding: 10px 12px; color: var(--color-text-light); font-size: 13px; text-align: center; }

.picker-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-top: 1px solid var(--panel-border);
  gap: 12px;
  flex-shrink: 0;
  flex-wrap: wrap;
}
.token-estimate { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--color-text-light); }
.footer-actions { display: flex; gap: 8px; margin-left: auto; }

.cancel-btn { padding: 8px 14px; font-size: 14px; color: var(--color-text); border-radius: var(--radius-sm); border: 1px solid var(--panel-border); background: var(--surface-panel-subtle); }
.cancel-btn:hover { background-color: var(--color-hover); }

.confirm-btn {
  padding: 8px 16px;
  font-size: 14px;
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  border-radius: var(--radius-sm);
  font-weight: 500;
  transition: all var(--transition-fast);
}
.confirm-btn:hover:not(:disabled) { filter: brightness(1.06); transform: translateY(-1px); }
.confirm-btn:disabled { opacity: 0.4; cursor: not-allowed; }

@media (max-width: 767px) {
  .note-picker-modal { max-height: 85vh; margin: 8px; width: calc(100% - 16px); }
  .token-estimate { width: 100%; }
  .footer-actions { width: 100%; justify-content: flex-end; }
}
</style>
