<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="notes-page">
    <div class="page-header">
      <button class="back-btn" @click="goBack">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="15 18 9 12 15 6"/>
        </svg>
      </button>
      <div class="header-info">
        <h1 class="page-title">{{ notebook?.name || '笔记' }}</h1>
        <span class="note-count">{{ notesStore.currentNotes.length }} 条笔记</span>
      </div>
      <button v-if="!selectionMode && notesStore.currentNotes.length > 0 && !noteSearchQuery" class="header-action-btn" @click="enterMoveMode" title="批量移动">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M5 12h14M12 5l7 7-7 7"/>
        </svg>
      </button>
      <button v-if="!selectionMode && notesStore.currentNotes.length > 0 && !noteSearchQuery" class="header-action-btn" @click="enterExportMode" title="批量导出">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
      </button>
      <button v-if="!selectionMode && notesStore.currentNotes.length > 0 && !noteSearchQuery" class="header-action-btn danger" @click="enterDeleteMode" title="批量删除">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
        </svg>
      </button>
      <button class="import-btn" @click="triggerImport" title="导入文件">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="17 8 12 3 7 8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
      </button>
      <button class="add-btn" @click="createNewNote">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="12" y1="18" x2="12" y2="12"/>
          <line x1="9" y1="15" x2="15" y2="15"/>
        </svg>
      </button>
      <input
        ref="importFileInput"
        type="file"
        multiple
        accept=".pdf,.md,.markdown,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.csv"
        style="display:none;"
        @change="handleImportFiles"
      />
    </div>

    <div class="search-bar">
      <div class="search-input-wrapper">
        <svg class="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input
          v-model="noteSearchQuery"
          class="search-input"
          placeholder="搜索当前笔记本..."
          @input="onNoteSearchInput"
        />
        <button v-if="noteSearchQuery" class="search-clear-btn" @click="clearNoteSearch">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
    </div>

    <div class="search-results" v-if="noteSearchQuery">
      <div v-if="notesStore.noteSearchResults.length === 0 && !noteSearchLoading" class="search-empty">
        <p>未找到相关笔记</p>
      </div>
      <div v-if="noteSearchLoading" class="search-empty">
        <p>搜索中...</p>
      </div>
      <div
        v-for="result in notesStore.noteSearchResults"
        :key="result.note_id"
        class="search-result-item"
        @click="goToSearchResult(result.note_id)"
      >
        <div class="search-result-title">{{ result.title || '无标题' }}</div>
        <div class="search-result-snippet" v-html="highlightKeyword(result.content_snippet, noteSearchQuery)"></div>
      </div>
    </div>

    <div class="selection-bar" v-if="selectionMode">
      <div class="selection-bar-header">
        <label class="select-all-label">
          <input type="checkbox" :checked="allSelected" @change="toggleSelectAll" />
          全选
        </label>
        <span class="selected-count">{{ selectedNoteIds.size }} 已选</span>
      </div>
      <div class="selection-bar-actions">
        <button
          v-if="selectionMode === 'move'"
          class="move-confirm-btn"
          @click="showNotebookPicker = true"
          :disabled="selectedNoteIds.size === 0"
        >移动到...</button>
        <button
          v-if="selectionMode === 'export'"
          class="move-confirm-btn"
          @click="showExportFormatPicker = true"
          :disabled="selectedNoteIds.size === 0"
        >导出</button>
        <button
          v-if="selectionMode === 'delete'"
          class="delete-confirm-btn"
          @click="handleBulkDelete"
          :disabled="selectedNoteIds.size === 0"
        >删除</button>
        <button class="selection-cancel-btn" @click="exitSelectionMode">取消</button>
      </div>
    </div>

    <template v-if="notesStore.currentNotes.length > 0 && !noteSearchQuery">
    <div ref="notesListRef" class="notes-list">
     <div
       v-for="(column, colIndex) in stackedColumns"
       :key="colIndex"
       class="stack-column"
     >
      <div
        v-for="(note, rowIndex) in column"
        :key="note.id"
        class="note-row"
        :class="{
          'swipe-open': swipedNoteId === note.id && !selectionMode,
          'swipe-dragging': isNoteDragging(note.id) && !selectionMode,
        }"
        :style="{ zIndex: rowIndex + 1 }"
        @touchstart="handleTouchStart($event, note.id)"
        @touchmove="handleTouchMove($event, note.id)"
        @touchend="handleTouchEnd"
        @touchcancel="handleTouchEnd"
        @contextmenu.prevent
      >
        <div v-if="!selectionMode" class="note-actions">
          <button class="swipe-action rename" @click.stop="handleSwipeRename(note.id, note.title || '')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            <span>重命名</span>
          </button>
          <button class="swipe-action move" @click.stop="handleSwipeMove(note.id)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M5 12h14M12 5l7 7-7 7"/>
            </svg>
            <span>移动到</span>
          </button>
          <button class="swipe-action export" @click.stop="handleSwipeExport(note.id)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            <span>导出</span>
          </button>
          <button class="swipe-action delete" @click.stop="handleSwipeDelete(note.id)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
            <span>删除</span>
          </button>
        </div>
        <div
          class="note-item"
          :style="getNoteItemStyle(note.id)"
          @click="handleNoteClick(note.id)"
        >
          <input
            v-if="selectionMode"
            type="checkbox"
            class="selection-checkbox"
            :checked="selectedNoteIds.has(note.id)"
            @click.stop
            @change="toggleNoteSelection(note.id)"
          />
          <template v-if="renamingNoteId === note.id && !selectionMode">
            <div class="title-edit-row" @click.stop>
              <input
                v-model="renamingTitle"
                class="title-input"
                @keyup.enter="saveRename(note.id)"
                @keyup.escape="cancelRename"
                @click.stop
                autofocus
              />
              <div class="title-edit-actions">
                <button class="title-edit-btn save" @mousedown.prevent @click.stop="saveRename(note.id)">保存</button>
                <button class="title-edit-btn cancel" @mousedown.prevent @click.stop="cancelRename">取消</button>
              </div>
            </div>
          </template>
          <template v-else>
            <div class="note-content">
              <div class="note-title" v-if="note.title">{{ note.title }}</div>
              <div class="note-title untitled" v-else>无标题笔记</div>
              <div class="note-preview">{{ note.content_preview }}</div>
              <div class="note-date">{{ formatDate(note.updated_at) }}</div>
            </div>
            <button
              v-if="!selectionMode"
              class="menu-btn hide-on-mobile"
              @click.stop="showNoteMenu(note, $event)"
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
     </div>
    </div>

    <div class="pagination-bar">
      <button
        class="page-btn"
        @click="goToPage(currentPage - 1)"
        :disabled="currentPage <= 1"
      >上一页</button>
      <span class="page-indicator">{{ currentPage }} / {{ totalPages }}</span>
      <button
        class="page-btn"
        @click="goToPage(currentPage + 1)"
        :disabled="currentPage >= totalPages"
      >下一页</button>
    </div>
    </template>

    <div class="empty-state" v-else-if="!notesStore.isLoading && !noteSearchQuery">
      <div class="empty-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--color-text-light)" stroke-width="1.5">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
          <polyline points="10 9 9 9 8 9"/>
        </svg>
      </div>
      <p>还没有笔记</p>
      <button class="create-btn" @click="createNewNote">创建笔记</button>
    </div>

    <div class="loading-state" v-if="notesStore.isLoading">
      <div class="spinner"></div>
    </div>

    <div class="import-progress" v-if="importing">
      <div class="spinner-sm"></div>
      <span>正在导入文件...</span>
    </div>

    <NotebookPicker
      v-if="showNotebookPicker"
      :exclude-notebook-id="notebookId"
      @select="handleMoveToNotebook"
      @close="showNotebookPicker = false"
    />

    <Teleport to="body">
      <div v-if="activeMenuNote" class="context-menu" :style="noteMenuStyle" @click.stop>
        <button class="menu-item" @click="handleMenuRename">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
          <span>重命名</span>
        </button>
        <button class="menu-item" @click="handleMenuMove">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M5 12h14M12 5l7 7-7 7"/>
          </svg>
          <span>移动到</span>
        </button>
        <button class="menu-item" @click="handleMenuExport">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          <span>导出</span>
        </button>
        <button class="menu-item delete" @click="handleMenuDelete">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          </svg>
          <span>删除</span>
        </button>
      </div>
    </Teleport>

    <Teleport to="body">
      <div v-if="showExportFormatPicker" class="export-format-overlay" @click="showExportFormatPicker = false">
        <div class="export-format-dialog" @click.stop>
          <h3>选择导出格式</h3>
          <div class="export-format-options">
            <button class="export-format-btn" @click="doExport('md')">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <span>Markdown (.md)</span>
            </button>
            <button class="export-format-btn" @click="doExport('pdf')">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="16" y1="13" x2="8" y2="13"/>
                <line x1="16" y1="17" x2="8" y2="17"/>
              </svg>
              <span>PDF (.pdf)</span>
            </button>
          </div>
          <button class="export-cancel-btn" @click="showExportFormatPicker = false">取消</button>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <ExportProgressDialog
        :visible="exporting || exportStatus === 'success'"
        :format="exportFormat"
        :status="exportStatus"
        :progress="exportProgress"
        @cancel="cancelExport"
        @close="closeExportDialog"
      />
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, watch, ref, nextTick, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { navigateWithMobileHistory } from '@/composables/useMobileNavigation'
import { useNotesStore } from '@/stores/notes'
import { useMobileUiStore } from '@/stores/mobileUi'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { fileUploadApi } from '@/api/fileUpload'
import { notesApi } from '@/api/notes'
import NotebookPicker from '@/components/NotebookPicker.vue'
import ExportProgressDialog from '@/components/ExportProgressDialog.vue'
import type { NoteListItem } from '@/types'

const route = useRoute()
const router = useRouter()
const notesStore = useNotesStore()
const mobileUi = useMobileUiStore()

onMounted(() => {
  notesStore.saveLastNotesPath(route.fullPath)
  nextTick(() => {
    connectResizeObserver()
    calculateCardsPerColumn()
  })
  window.addEventListener('resize', calculateCardsPerColumn)
})

onUnmounted(() => {
  notesStore.saveLastNotesPath(route.fullPath)
  window.removeEventListener('resize', calculateCardsPerColumn)
  resizeObserver?.disconnect()
  resizeObserver = null
})

onUnmounted(() => {
  notesStore.saveLastNotesPath(route.fullPath)
})
const { show } = useToast()
const { confirm: showConfirm } = useConfirmDialog()

const notebookId = computed(() => route.params.notebookId as string)
const notebook = computed(() => notesStore.currentNotebook)

// Dynamic pagination: calculate cards per column based on viewport height to avoid scrollbars
const COLUMN_COUNT = 3
const CARD_HEIGHT = 130
const CARD_OVERLAP = 40
const LIST_PADDING_TOP = 48
const LIST_PADDING_BOTTOM = 32
const cardsPerColumn = ref(8)
const PAGE_SIZE = computed(() => cardsPerColumn.value * COLUMN_COUNT)
const currentPage = ref(1)
const totalPages = computed(() => Math.max(1, Math.ceil(notesStore.currentNotes.length / PAGE_SIZE.value)))
const pagedNotes = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE.value
  return notesStore.currentNotes.slice(start, start + PAGE_SIZE.value)
})
const stackedColumns = computed(() => {
  const items = pagedNotes.value
  const perCol = cardsPerColumn.value
  const cols: typeof items[] = []
  for (let i = 0; i < COLUMN_COUNT; i++) {
    const start = i * perCol
    cols.push(items.slice(start, start + perCol))
  }
  return cols
})
const notesListRef = ref<HTMLElement | null>(null)

function calculateCardsPerColumn() {
  const listEl = notesListRef.value
  if (!listEl) return
  const listHeight = listEl.clientHeight
  // During SPA route transitions the list container is position:absolute with
  // height:0, yielding a bogus measurement. Skip — the ResizeObserver fires
  // again once layout settles with the real height.
  if (!listHeight) return
  const contentHeight = listHeight - LIST_PADDING_TOP - LIST_PADDING_BOTTOM
  if (contentHeight < CARD_HEIGHT) {
    cardsPerColumn.value = 1
    return
  }
  // First card takes full height, subsequent cards overlap by CARD_OVERLAP
  const n = Math.floor((contentHeight - CARD_HEIGHT) / (CARD_HEIGHT - CARD_OVERLAP)) + 1
  cardsPerColumn.value = Math.max(1, n)
}

function resetPagination() {
  currentPage.value = 1
  nextTick(() => {
    notesListRef.value?.scrollTo({ top: 0 })
  })
}

function goToPage(page: number) {
  const target = Math.min(Math.max(1, page), totalPages.value)
  if (target === currentPage.value) return
  currentPage.value = target
  nextTick(() => {
    notesListRef.value?.scrollTo({ top: 0 })
  })
}

let resizeObserver: ResizeObserver | null = null

function connectResizeObserver() {
  const el = notesListRef.value
  if (!el) return
  if (resizeObserver) resizeObserver.disconnect()
  resizeObserver = new ResizeObserver(() => {
    calculateCardsPerColumn()
  })
  resizeObserver.observe(el)
}

// Recalculate whenever the page size changes (e.g. after layout settles) so we
// never leave currentPage pointing past the last page.
watch(PAGE_SIZE, () => {
  if (currentPage.value > totalPages.value) {
    currentPage.value = totalPages.value
  }
})

// Connect the observer whenever the list element (re)appears in the DOM.
watch(notesListRef, (el) => {
  if (el) {
    connectResizeObserver()
    nextTick(calculateCardsPerColumn)
  }
})

watch(() => notesStore.currentNotes.length, () => {
  if (currentPage.value > totalPages.value) {
    currentPage.value = totalPages.value
  }
})

// Selection mode: null | 'delete' | 'move' | 'export'
const selectionMode = ref<'delete' | 'move' | 'export' | null>(null)
const selectedNoteIds = ref<Set<string>>(new Set())

// Export format picker
const showExportFormatPicker = ref(false)
const singleExportNoteId = ref<string | null>(null)
const exporting = ref(false)
const exportFormat = ref<'md' | 'pdf'>('md')
const exportStatus = ref<'idle' | 'exporting' | 'success'>('idle')
const exportTaskId = ref<string | null>(null)
const exportProgress = ref(0)

// Swipe state
const swipedNoteId = ref<string | null>(null)
const touchStartX = ref(0)
const touchStartY = ref(0)
const touchStartOffset = ref(0)
const touchDeltaX = ref(0)
const swipeTrackingNoteId = ref<string | null>(null)
const isSwipeTracking = ref(false)
const isSwipeDragging = ref(false)

// Inline rename state
const renamingNoteId = ref<string | null>(null)
const renamingTitle = ref('')

// NotebookPicker
const showNotebookPicker = ref(false)
const singleMoveNoteId = ref<string | null>(null)

// File import
const importFileInput = ref<HTMLInputElement | null>(null)
const importing = ref(false)

function triggerImport() {
  importFileInput.value?.click()
}

  async function handleImportFiles(event: Event) {
    const input = event.target as HTMLInputElement
    if (!input.files || input.files.length === 0) return

    const files = Array.from(input.files)
    input.value = ''

    importing.value = true

    try {
      const result = await fileUploadApi.uploadFiles(
        files,
        true,
        undefined,
        notebookId.value,
      )
      const successful = result.results.filter(r => r.success)
      const importedCount = successful.length
      const failed = result.results.filter(r => !r.success)
      if (failed.length > 0) {
        const errors = failed.map(f => `${f.filename}: ${f.error}`).join('\n')
        show(`部分文件导入失败:\n${errors}`, 'error')
      }
      if (importedCount > 0) {
        show(`成功导入 ${importedCount} 个文件`, 'success')
      }
      await notesStore.loadNotes(notebookId.value)
    } catch (e) {
      show('导入失败', 'error')
    } finally {
      importing.value = false
    }
  }

// Desktop context menu
const activeMenuNote = ref<NoteListItem | null>(null)
const noteMenuStyle = ref({ top: '0px', left: '0px' })

// Note search within current notebook
const noteSearchQuery = ref('')
const noteSearchLoading = ref(false)
let noteSearchDebounceTimer: ReturnType<typeof setTimeout> | null = null
let suppressNoteClickUntil = 0

function onNoteSearchInput() {
  if (noteSearchDebounceTimer) clearTimeout(noteSearchDebounceTimer)
  noteSearchDebounceTimer = setTimeout(async () => {
    const q = noteSearchQuery.value.trim()
    if (!q) {
      notesStore.noteSearchResults = []
      notesStore.noteSearchQuery = ''
      noteSearchLoading.value = false
      return
    }
    noteSearchLoading.value = true
    await notesStore.searchNotes(q, notebookId.value)
    noteSearchLoading.value = false
  }, 300)
}

function clearNoteSearch() {
  noteSearchQuery.value = ''
  notesStore.noteSearchResults = []
  notesStore.noteSearchQuery = ''
  noteSearchLoading.value = false
  if (noteSearchDebounceTimer) clearTimeout(noteSearchDebounceTimer)
}

async function goToSearchResult(noteId: string) {
  const q = noteSearchQuery.value.trim()
  notesStore.noteSearchHighlightQuery = q
  notesStore.noteSearchHighlightNonce++
  clearNoteSearch()
  await navigateWithMobileHistory(router, `/notes/${notebookId.value}/${noteId}`)
}

function highlightKeyword(text: string, keyword: string): string {
  if (!keyword.trim()) return text
  const escaped = keyword.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const regex = new RegExp(`(${escaped})`, 'gi')
  return text.replace(regex, '<mark>$1</mark>')
}

const allSelected = computed(() => {
  return notesStore.currentNotes.length > 0 && selectedNoteIds.value.size === notesStore.currentNotes.length
})

watch(
  notebookId,
  async (id) => {
    if (!id) return
    if (notesStore.currentNotebookId !== id) {
      await notesStore.selectNotebook(id)
    }
    resetPagination()
    exitSelectionMode()
    closeSwipe()
    cancelRename()
  },
  { immediate: true },
)

// --- Desktop context menu ---
onMounted(() => {
  document.addEventListener('click', handleDocumentClick)
})

onUnmounted(() => {
  document.removeEventListener('click', handleDocumentClick)
  closeSwipe()
})

function handleDocumentClick() {
  closeNoteMenu()
  if (!renamingNoteId.value) {
    closeSwipe()
  }
}

function closeNoteMenu() {
  activeMenuNote.value = null
}

function showNoteMenu(note: NoteListItem, event: MouseEvent) {
  if (activeMenuNote.value?.id === note.id) {
    closeNoteMenu()
    return
  }
  closeSwipe()
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  noteMenuStyle.value = {
    top: `${rect.bottom + 4}px`,
    left: `${rect.right - 160}px`,
  }
  activeMenuNote.value = note
}

function handleMenuRename() {
  if (!activeMenuNote.value) return
  const note = activeMenuNote.value
  closeNoteMenu()
  handleSwipeRename(note.id, note.title || '')
}

function handleMenuMove() {
  if (!activeMenuNote.value) return
  const note = activeMenuNote.value
  closeNoteMenu()
  handleSwipeMove(note.id)
}

function handleMenuDelete() {
  if (!activeMenuNote.value) return
  const note = activeMenuNote.value
  closeNoteMenu()
  void handleSwipeDelete(note.id)
}

function handleMenuExport() {
  if (!activeMenuNote.value) return
  singleExportNoteId.value = activeMenuNote.value.id
  closeNoteMenu()
  showExportFormatPicker.value = true
}

async function doExport(format: 'md' | 'pdf') {
  showExportFormatPicker.value = false
  exportFormat.value = format
  exportStatus.value = 'exporting'
  exporting.value = true
  exportProgress.value = 0
  exportTaskId.value = null
  try {
    if (singleExportNoteId.value) {
      await notesStore.exportNoteAsync(singleExportNoteId.value, format, (task) => {
        exportTaskId.value = task.id
        exportProgress.value = Math.round(task.progress * 100)
      })
      singleExportNoteId.value = null
      exportStatus.value = 'success'
      show('导出成功', 'success')
    } else if (selectionMode.value === 'export' && selectedNoteIds.value.size > 0) {
      await notesStore.bulkExportNotesAsync(Array.from(selectedNoteIds.value), format, (task) => {
        exportTaskId.value = task.id
        exportProgress.value = Math.round(task.progress * 100)
      })
      exportStatus.value = 'success'
      show(`已导出 ${selectedNoteIds.value.size} 条笔记`, 'success')
      exitSelectionMode()
    }
  } catch (error: any) {
    exportStatus.value = 'idle'
    if (error?.message === '导出已取消') {
      show('已取消导出', 'info')
    } else {
      console.error('Export failed:', error)
      show(error?.message || '导出失败', 'error')
    }
  } finally {
    exporting.value = false
  }
}

function cancelExport() {
  if (exportTaskId.value) {
    notesStore.cancelExportTask(exportTaskId.value)
  }
  exportStatus.value = 'idle'
}

function closeExportDialog() {
  exportStatus.value = 'idle'
}

// --- Navigation ---
function goBack() {
  void navigateWithMobileHistory(router, '/notes')
}

async function openNote(noteId: string) {
  await navigateWithMobileHistory(router, `/notes/${notebookId.value}/${noteId}`)
}

function handleNoteClick(noteId: string) {
  if (Date.now() < suppressNoteClickUntil) {
    return
  }
  if (renamingNoteId.value === noteId) return
  if (selectionMode.value) {
    toggleNoteSelection(noteId)
    return
  }
  if (swipedNoteId.value) {
    closeSwipe()
    return
  }
  void openNote(noteId)
}

// --- Create ---
async function createNewNote() {
  try {
    const note = await notesStore.createNote(notebookId.value, { content: '' })
    await navigateWithMobileHistory(router, `/notes/${notebookId.value}/${note.id}`)
  } catch (error) {
    console.error('Failed to create note:', error)
    show('创建笔记失败', 'error')
  }
}

// --- Selection mode ---
function enterDeleteMode() {
  selectionMode.value = 'delete'
  selectedNoteIds.value = new Set()
  closeSwipe()
}

function enterMoveMode() {
  selectionMode.value = 'move'
  selectedNoteIds.value = new Set()
  closeSwipe()
}

function enterExportMode() {
  selectionMode.value = 'export'
  selectedNoteIds.value = new Set()
  closeSwipe()
}

function exitSelectionMode() {
  selectionMode.value = null
  selectedNoteIds.value = new Set()
}

function toggleNoteSelection(noteId: string) {
  const next = new Set(selectedNoteIds.value)
  if (next.has(noteId)) {
    next.delete(noteId)
  } else {
    next.add(noteId)
  }
  selectedNoteIds.value = next
}

function toggleSelectAll() {
  if (allSelected.value) {
    selectedNoteIds.value = new Set()
  } else {
    selectedNoteIds.value = new Set(notesStore.currentNotes.map(n => n.id))
  }
}

// --- Bulk delete ---
async function handleBulkDelete() {
  if (selectedNoteIds.value.size === 0) return
  if (!await showConfirm({ message: '确定要删除选中的笔记吗？', danger: true, confirmText: '删除' })) return

  try {
    await notesStore.bulkDeleteNotes(Array.from(selectedNoteIds.value))
    show('已删除选中的笔记', 'success')
    exitSelectionMode()
  } catch (error) {
    console.error('Failed to bulk delete notes:', error)
    show('批量删除笔记失败', 'error')
  }
}

// --- Move ---
async function handleMoveToNotebook(targetNotebookId: string) {
  showNotebookPicker.value = false

  if (singleMoveNoteId.value) {
    // Single swipe move
    try {
      await notesStore.moveNote(singleMoveNoteId.value, targetNotebookId)
      show('笔记已移动', 'success')
    } catch (error) {
      console.error('Failed to move note:', error)
      show('移动笔记失败', 'error')
    }
    singleMoveNoteId.value = null
    closeSwipe()
    return
  }

  // Bulk move
  if (selectedNoteIds.value.size === 0) return
  try {
    await notesStore.bulkMoveNotes(Array.from(selectedNoteIds.value), targetNotebookId)
    show(`已移动 ${selectedNoteIds.value.size} 条笔记`, 'success')
    exitSelectionMode()
  } catch (error) {
    console.error('Failed to bulk move notes:', error)
    show('批量移动笔记失败', 'error')
  }
}

// --- Swipe ---
const SWIPE_THRESHOLD = 50
const SWIPE_ACTIONS_WIDTH = 180

function handleTouchStart(e: TouchEvent, noteId: string) {
  if (selectionMode.value || renamingNoteId.value || e.touches.length !== 1) return
  if (swipedNoteId.value && swipedNoteId.value !== noteId) {
    closeSwipe()
  }
  const touch = e.touches[0]
  swipeTrackingNoteId.value = noteId
  touchStartX.value = touch.clientX
  touchStartY.value = touch.clientY
  touchStartOffset.value = swipedNoteId.value === noteId ? touchDeltaX.value : 0
  touchDeltaX.value = touchStartOffset.value
  isSwipeTracking.value = true
  isSwipeDragging.value = false
  if (swipedNoteId.value === noteId) {
    e.stopPropagation()
    mobileUi.openRowAction('note', noteId)
  }
}

function handleTouchMove(e: TouchEvent, noteId: string) {
  if (
    selectionMode.value ||
    !isSwipeTracking.value ||
    swipeTrackingNoteId.value !== noteId ||
    e.touches.length !== 1
  ) {
    return
  }

  const dx = e.touches[0].clientX - touchStartX.value
  const dy = e.touches[0].clientY - touchStartY.value

  if (!isSwipeDragging.value) {
    if (Math.abs(dy) > 10 && Math.abs(dy) > Math.abs(dx)) {
      swipeTrackingNoteId.value = null
      isSwipeTracking.value = false
      mobileUi.stopRowActionDrag('note')
      return
    }

    if (Math.abs(dx) < 10) {
      return
    }

    if (dx > 0 && touchStartOffset.value === 0) {
      swipeTrackingNoteId.value = null
      isSwipeTracking.value = false
      mobileUi.stopRowActionDrag('note')
      return
    }

    isSwipeDragging.value = true
  }

  mobileUi.startRowActionDrag('note', noteId)

  e.preventDefault()
  swipedNoteId.value = noteId
  touchDeltaX.value = Math.max(-SWIPE_ACTIONS_WIDTH, Math.min(0, touchStartOffset.value + dx))
}

function handleTouchEnd() {
  if (!isSwipeTracking.value || !swipeTrackingNoteId.value) {
    return
  }

  const noteId = swipeTrackingNoteId.value

  if (isSwipeDragging.value) {
    suppressNoteClickUntil = Date.now() + 300
    if (touchDeltaX.value <= -SWIPE_ACTIONS_WIDTH / 2) {
      swipedNoteId.value = noteId
      touchDeltaX.value = -SWIPE_ACTIONS_WIDTH
      mobileUi.openRowAction('note', noteId)
    } else {
      closeSwipe()
      return
    }
  } else if (swipedNoteId.value === noteId && touchDeltaX.value <= -SWIPE_THRESHOLD) {
    mobileUi.openRowAction('note', noteId)
  }

  swipeTrackingNoteId.value = null
  touchStartOffset.value = 0
  isSwipeTracking.value = false
  isSwipeDragging.value = false
  mobileUi.stopRowActionDrag('note')
}

function closeSwipe() {
  swipedNoteId.value = null
  touchDeltaX.value = 0
  touchStartOffset.value = 0
  swipeTrackingNoteId.value = null
  isSwipeTracking.value = false
  isSwipeDragging.value = false
  mobileUi.closeRowAction('note')
}

function getNoteItemStyle(noteId: string) {
  if (selectionMode.value) return {}
  if (swipedNoteId.value === noteId) {
    return { transform: `translateX(${touchDeltaX.value}px)` }
  }
  return {}
}

function isNoteDragging(noteId: string) {
  return isSwipeDragging.value && swipeTrackingNoteId.value === noteId && touchDeltaX.value < 0
}

// --- Swipe actions ---
function handleSwipeRename(noteId: string, currentTitle: string) {
  renamingNoteId.value = noteId
  renamingTitle.value = currentTitle
  closeSwipe()
  nextTick(() => {
    const input = document.querySelector('.title-input') as HTMLInputElement
    input?.focus()
    input?.select()
  })
}

async function saveRename(noteId: string) {
  const title = renamingTitle.value.trim()
  if (!title) {
    show('标题不能为空', 'error')
    return
  }
  try {
    await notesStore.updateNote(noteId, { title })
    show('笔记已重命名', 'success')
  } catch (error) {
    console.error('Failed to rename note:', error)
    show('重命名失败', 'error')
  }
  cancelRename()
}

function cancelRename() {
  renamingNoteId.value = null
  renamingTitle.value = ''
}

function handleSwipeMove(noteId: string) {
  singleMoveNoteId.value = noteId
  showNotebookPicker.value = true
  closeSwipe()
}

function handleSwipeExport(noteId: string) {
  singleExportNoteId.value = noteId
  showExportFormatPicker.value = true
  closeSwipe()
}

async function handleSwipeDelete(noteId: string) {
  if (!await showConfirm({ message: '确定要删除这条笔记吗？', danger: true, confirmText: '删除' })) return
  try {
    await notesStore.deleteNote(noteId)
    show('笔记已删除', 'success')
    closeSwipe()
  } catch (error) {
    console.error('Failed to delete note:', error)
    show('删除笔记失败', 'error')
  }
}

// --- Format ---
function formatDate(dateStr: string): string {
  const isoStr = dateStr.endsWith('Z') ? dateStr : dateStr + 'Z'
  const date = new Date(isoStr)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))

  if (days === 0) {
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  if (days === 1) {
    return '昨天'
  }
  if (days < 7) {
    return `${days}天前`
  }
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}
</script>

<style scoped>
.notes-page {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background-color: var(--surface-panel-subtle);
}

.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  background: var(--surface-panel-strong);
  border-bottom: 1px solid var(--panel-border);
}

.back-btn {
  padding: 8px;
  color: var(--color-text);
  border-radius: var(--radius-sm);
}

.header-info {
  flex: 1;
  min-width: 0;
}

.page-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.note-count {
  font-size: 12px;
  color: var(--color-text-light);
}

.add-btn {
  padding: 8px;
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  border-radius: var(--radius-sm);
}

.import-btn {
  padding: 8px;
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.import-btn:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--primary-tint);
}

.import-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  margin: 0 12px;
  background: var(--primary-tint);
  border-radius: var(--radius-md);
  color: var(--color-primary);
  font-size: 13px;
}

.spinner-sm {
  width: 16px;
  height: 16px;
  border: 2px solid color-mix(in srgb, var(--color-primary) 20%, transparent);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

.header-action-btn {
  padding: 8px;
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.header-action-btn:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--primary-tint);
}

.header-action-btn.danger:hover {
  color: var(--color-error);
  border-color: var(--color-error);
  background: var(--danger-tint);
}

.selection-bar {
  padding: 12px;
  margin: 0 12px 8px;
  background-color: var(--color-hover);
  border-radius: var(--radius-md);
}

.selection-bar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.select-all-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  cursor: pointer;
}

.selected-count {
  font-size: 12px;
  color: var(--color-text-light);
}

.selection-bar-actions {
  display: flex;
  gap: 8px;
}

.move-confirm-btn {
  flex: 1;
  padding: 8px 12px;
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
}

.move-confirm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.delete-confirm-btn {
  flex: 1;
  padding: 8px 12px;
  background-color: var(--color-error);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
}

.delete-confirm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.selection-cancel-btn {
  padding: 8px 12px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  font-size: 13px;
}

.notes-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
}

.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 12px;
  min-height: 58px;
  box-sizing: border-box;
  border-top: 1px solid var(--color-border);
  background: var(--surface-panel-strong);
  flex-shrink: 0;
}

.page-btn {
  padding: 6px 14px;
  font-size: 13px;
  color: var(--color-text);
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background-color var(--transition-fast);
}

.page-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
}

.page-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.page-indicator {
  font-size: 13px;
  color: var(--color-text-light);
  min-width: 56px;
  text-align: center;
}

.stack-column {
  display: flex;
  flex-direction: column;
}

/* --- Swipe row --- */
.note-row {
  position: relative;
  overflow: hidden;
  border-radius: var(--radius-md);
  margin-bottom: 8px;
}

.note-actions {
  position: absolute;
  right: 0;
  top: 0;
  bottom: 0;
  width: 180px;
  display: flex;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
}

.note-row.swipe-open .note-actions,
.note-row.swipe-dragging .note-actions {
  opacity: 1;
  visibility: visible;
}

.note-row.swipe-open .note-actions {
  pointer-events: auto;
}

.swipe-action {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  border: none;
  color: white;
  font-size: 11px;
  cursor: pointer;
  padding: 0;
}

.swipe-action.rename {
  background-color: var(--color-primary);
}

.swipe-action.move {
  background-color: var(--swipe-move-bg);
}

.swipe-action.export {
  background-color: var(--color-success);
}

.swipe-action.delete {
  background-color: var(--color-error);
}

.note-item {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: transform 0.2s ease;
}

.note-item:hover {
  background-color: var(--color-hover);
  box-shadow: var(--shadow-sm);
}

.selection-checkbox {
  margin-top: 2px;
  cursor: pointer;
  accent-color: var(--color-primary);
}

.note-content {
  flex: 1;
  min-width: 0;
}

.note-title {
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text);
  margin-bottom: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.note-title.untitled {
  color: var(--color-text-light);
}

.note-preview {
  font-size: 13px;
  color: var(--color-text-light);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: 8px;
}

.note-date {
  font-size: 11px;
  color: var(--color-text-light);
}

/* --- Inline rename --- */
.title-edit-row {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.title-input {
  width: 100%;
  padding: 8px 10px;
  font-size: 14px;
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-sm);
  outline: none;
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  box-shadow: 0 0 0 3px var(--focus-ring-color);
}

.title-edit-actions {
  display: flex;
  gap: 6px;
}

.title-edit-btn {
  padding: 4px 12px;
  font-size: 12px;
  border-radius: var(--radius-sm);
  font-weight: 500;
}

.title-edit-btn.save {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
}

.title-edit-btn.cancel {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  color: var(--color-text);
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  color: var(--color-text-light);
}

.create-btn {
  padding: 10px 24px;
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  border-radius: var(--radius-md);
  font-weight: 500;
}

.loading-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--panel-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 767px) {
  .page-header {
    padding: 12px 16px;
  }

  .selection-controls {
    padding: 8px 8px 0;
  }

  .selection-bar {
    margin: 0 8px 8px;
    padding: 10px;
  }

  .notes-list {
    padding: 8px;
    padding-bottom: calc(8px + var(--mobile-tab-bar-offset, 60px) + env(safe-area-inset-bottom, 0px));
  }

  .pagination-bar {
    margin-bottom: calc(var(--mobile-tab-bar-offset, 60px) + env(safe-area-inset-bottom, 0px));
  }

  .note-item {
    padding: 12px 14px;
  }
}

/* Search */
.search-bar {
  padding: 8px 16px;
}

.search-input-wrapper {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background-color: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast);
}

.search-input-wrapper:focus-within {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--focus-ring-color);
}

.search-icon {
  flex-shrink: 0;
  color: var(--color-text-light);
}

.search-input {
  flex: 1;
  border: none;
  outline: none;
  font-size: 13px;
  color: var(--color-text);
  background: transparent;
  min-width: 0;
}

.search-input::placeholder {
  color: var(--color-text-light);
}

.search-clear-btn {
  flex-shrink: 0;
  padding: 2px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
}

.search-clear-btn:hover {
  color: var(--color-text);
}

.search-results {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.search-empty {
  padding: 24px 16px;
  text-align: center;
  color: var(--color-text-light);
  font-size: 13px;
}

.search-result-item {
  padding: 10px 16px;
  cursor: pointer;
  border-bottom: 1px solid var(--panel-border);
  transition: background-color var(--transition-fast);
}

.search-result-item:hover {
  background-color: var(--color-hover);
}

.search-result-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.search-result-snippet {
  font-size: 12px;
  color: var(--color-text-light);
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.search-result-snippet :deep(mark) {
  background-color: var(--color-primary);
  color: white;
  padding: 0 2px;
  border-radius: 2px;
}

/* Desktop menu button */
.menu-btn {
  opacity: 0;
  padding: 4px 6px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  flex-shrink: 0;
  align-self: center;
}

.note-item:hover .menu-btn {
  opacity: 1;
}

.menu-btn:hover {
  color: var(--color-text);
  background-color: var(--surface-panel-subtle);
}

/* Desktop context menu */
.context-menu {
  position: fixed;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  z-index: 1001;
  min-width: 160px;
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
  background-color: color-mix(in srgb, var(--color-error) 8%, transparent);
}

/* Desktop: 3 columns responsive wallet-stack, hide swipe actions */
@media (min-width: 768px) {
  .notes-list {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 16px;
    align-items: start;
    align-content: start;
    overflow-y: auto;
    min-height: 0;
    padding: 48px 20px 32px;
  }

  .stack-column {
    display: flex;
    flex-direction: column;
    position: relative;
    min-width: 0;
  }

  .stack-column > .note-row {
    margin-bottom: 0;
    display: flex;
    transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1),
                box-shadow 220ms cubic-bezier(0.22, 1, 0.36, 1);
    box-shadow: var(--stack-card-shadow);
    border-radius: var(--stack-card-radius);
    contain: paint;
  }

  .stack-column > .note-row:not(:first-child) {
    margin-top: var(--stack-overlap);
  }

  .stack-column > .note-row:hover {
    transform: translateY(var(--stack-hover-lift));
    box-shadow: var(--stack-card-shadow-hover);
  }

  .note-item {
    height: 130px;
    overflow: hidden;
    width: 100%;
    box-sizing: border-box;
  }

  .note-content {
    display: flex;
    flex-direction: column;
    justify-content: center;
  }

  .load-more-sentinel {
    grid-column: 1 / -1;
    height: 20px;
  }

  .load-more-hint {
    grid-column: 1 / -1;
    text-align: center;
    padding: 12px;
    font-size: 13px;
    color: var(--color-text-light);
  }

  .note-actions {
    display: none;
  }
}

.export-format-overlay {
  position: fixed;
  inset: 0;
  z-index: 998;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
}

.export-format-dialog {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--dialog-radius);
  box-shadow: var(--dialog-shadow);
  padding: 24px;
  min-width: 280px;
}

.export-format-dialog h3 {
  margin: 0 0 16px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.export-format-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.export-format-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--color-hover);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: 14px;
  cursor: pointer;
  transition: all 0.15s;
}

.export-format-btn:hover {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.export-cancel-btn {
  width: 100%;
  margin-top: 12px;
  padding: 10px;
  background: none;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  color: var(--color-text-light);
  font-size: 14px;
  cursor: pointer;
}

.export-cancel-btn:hover {
  background: var(--color-hover);
}
</style>
