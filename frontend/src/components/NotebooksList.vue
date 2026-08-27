<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="notebooks-page">
    <div class="page-header">
      <button class="back-btn hide-on-desktop" @click="goBackToChat">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="15 18 9 12 15 6"/>
        </svg>
      </button>
      <h1 class="page-title">笔记本</h1>
      <button v-if="!selectionMode && notesStore.notebooks.length > 0 && !notebookSearchQuery" class="header-action-btn" @click="enterExportMode" title="批量导出">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
      </button>
      <button v-if="!selectionMode && notesStore.notebooks.length > 0 && !notebookSearchQuery" class="header-action-btn danger" @click="enterDeleteMode" title="批量删除">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
        </svg>
      </button>
      <button class="add-btn" @click="showCreateModal = true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          <line x1="12" y1="9" x2="12" y2="15"/>
          <line x1="9" y1="12" x2="15" y2="12"/>
        </svg>
      </button>
    </div>

    <div class="search-bar">
      <div class="search-input-wrapper">
        <svg class="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input
          v-model="notebookSearchQuery"
          class="search-input"
          placeholder="搜索全部笔记内容..."
          @input="onNotebookSearchInput"
        />
        <button v-if="notebookSearchQuery" class="search-clear-btn" @click="clearNotebookSearch">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
    </div>

    <div class="search-results" v-if="notebookSearchQuery">
      <div v-if="notesStore.noteSearchResults.length === 0 && !nbSearchLoading" class="search-empty">
        <p>未找到相关笔记</p>
      </div>
      <div v-if="nbSearchLoading" class="search-empty">
        <p>搜索中...</p>
      </div>
      <div
        v-for="result in notesStore.noteSearchResults"
        :key="result.note_id"
        class="search-result-item"
        @click="goToSearchResult(result)"
      >
        <div class="search-result-notebook">{{ result.notebook_name }}</div>
        <div class="search-result-title">{{ result.title || '无标题' }}</div>
        <div class="search-result-snippet" v-html="highlightKeyword(result.content_snippet, notebookSearchQuery)"></div>
      </div>
    </div>

    <div class="selection-bar" v-if="selectionMode">
      <div class="selection-bar-header">
        <label class="select-all-label">
          <input
            type="checkbox"
            :checked="selectionMode === 'delete' ? allDeletableSelected : allSelected"
            @change="toggleSelectAll"
          />
          全选
        </label>
        <span class="selected-count">{{ selectedNotebookIds.size }} 已选</span>
      </div>
      <div class="selection-bar-actions">
        <button
          v-if="selectionMode === 'export'"
          class="export-confirm-btn"
          @click="handleBulkExport"
          :disabled="selectedNotebookIds.size === 0 || selectionPending"
        >
          {{ selectionPending ? '导出中...' : '导出' }}
        </button>
        <button
          v-if="selectionMode === 'delete'"
          class="delete-confirm-btn"
          @click="handleBulkDelete"
          :disabled="selectedNotebookIds.size === 0 || selectionPending"
        >
          {{ selectionPending ? '删除中...' : '删除' }}
        </button>
        <button class="selection-cancel-btn" @click="exitSelectionMode">取消</button>
      </div>
      <div class="selection-progress" v-if="selectionProgress">{{ selectionProgress }}</div>
    </div>

    <template v-if="notesStore.notebooks.length > 0 && !notebookSearchQuery">
    <div ref="notebooksListRef" class="notebooks-list">
     <div
       v-for="(column, colIndex) in stackedColumns"
       :key="colIndex"
       class="stack-column"
     >
      <div
        v-for="(notebook, rowIndex) in column"
        :key="notebook.id"
        class="notebook-row"
        :class="{ 'swipe-open': swipedNotebookId === notebook.id && !selectionMode }"
        :style="{ zIndex: rowIndex + 1 }"
        @touchstart="handleTouchStart($event, notebook.id)"
        @touchmove="handleTouchMove($event, notebook.id)"
        @touchend="handleTouchEnd"
        @touchcancel="handleTouchEnd"
        @contextmenu.prevent
      >
        <div v-if="!selectionMode" class="notebook-actions">
          <button class="swipe-action export" @click.stop="handleExportNotebook(notebook.id)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            <span>导出</span>
          </button>
          <button class="swipe-action edit" @click.stop="startEditNotebook(notebook)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            <span>名称</span>
          </button>
          <button
            class="swipe-action default"
            :class="{ active: notebook.is_default }"
            :disabled="notebook.is_default"
            @click.stop="handleSetDefaultNotebook(notebook.id)"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 2l3.09 6.26L22 9.27l-5 4.87L18.18 22 12 18.56 5.82 22 7 14.14l-5-4.87 6.91-1.01L12 2z"/>
            </svg>
            <span>{{ notebook.is_default ? '默认' : '设默认' }}</span>
          </button>
          <button
            class="swipe-action delete"
            :disabled="notebook.is_default"
            @click.stop="handleDeleteNotebook(notebook.id)"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
            </svg>
            <span>删除</span>
          </button>
        </div>

        <div
          class="notebook-item"
          :style="getNotebookItemStyle(notebook.id)"
          @click="handleNotebookClick(notebook)"
        >
          <input
            v-if="selectionMode"
            type="checkbox"
            class="selection-checkbox"
            :checked="selectedNotebookIds.has(notebook.id)"
            :disabled="selectionMode === 'delete' && notebook.is_default"
            @click.stop
            @change="toggleNotebookSelection(notebook.id)"
          />

          <div class="notebook-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
          </div>

          <div class="notebook-info">
            <template v-if="editingNotebookId === notebook.id && !selectionMode">
              <div class="inline-editor" @click.stop>
                <input
                  ref="editingInput"
                  v-model="editingName"
                  class="notebook-name-input"
                  placeholder="笔记本名称"
                  @keyup.enter="saveNotebook(notebook.id)"
                  @keyup.escape="cancelEditNotebook"
                  @click.stop
                />
                <div class="inline-editor-actions">
                  <button class="inline-action save" @mousedown.prevent @click.stop="saveNotebook(notebook.id)">保存</button>
                  <button class="inline-action cancel" @mousedown.prevent @click.stop="cancelEditNotebook">取消</button>
                </div>
              </div>
              <span class="notebook-count">{{ notebook.note_count }} 条笔记</span>
            </template>
            <template v-else>
              <div class="notebook-name-row">
                <span class="notebook-name">{{ notebook.name }}</span>
                <span class="default-badge" v-if="notebook.is_default">默认</span>
              </div>
              <span class="notebook-count">{{ notebook.note_count }} 条笔记</span>
            </template>
          </div>

          <span v-if="selectionMode === 'delete' && notebook.is_default" class="selection-disabled-tip">默认不可删</span>

          <button
            v-if="!selectionMode && editingNotebookId !== notebook.id"
            class="menu-btn hide-on-mobile"
            @click.stop="showMenu(notebook, $event)"
            title="更多操作"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
              <circle cx="12" cy="5" r="2"/>
              <circle cx="12" cy="12" r="2"/>
              <circle cx="12" cy="19" r="2"/>
            </svg>
          </button>
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

    <div class="empty-state" v-else-if="!notesStore.isLoading && !notebookSearchQuery">
      <div class="empty-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--color-text-light)" stroke-width="1.5">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
      </div>
      <p>还没有笔记本</p>
      <button class="create-btn" @click="showCreateModal = true">创建笔记本</button>
    </div>

    <div class="modal-overlay" v-if="showCreateModal" @click="showCreateModal = false">
      <div class="modal-content" @click.stop>
        <div class="modal-title">新建笔记本</div>
        <input
          v-model="newNotebookName"
          class="modal-input"
          placeholder="笔记本名称"
          @keyup.enter="createNotebook"
          autofocus
        />
        <div class="modal-actions">
          <button class="cancel-btn" @click="showCreateModal = false">取消</button>
          <button class="confirm-btn" @click="createNotebook" :disabled="!newNotebookName.trim()">创建</button>
        </div>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="activeMenuNotebook" class="context-menu" :style="menuStyle" @click.stop>
        <button class="menu-item" @click="handleMenuEdit">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
          <span>重命名</span>
        </button>
        <button class="menu-item" @click="handleMenuExport">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          <span>导出笔记本</span>
        </button>
        <button v-if="!activeMenuNotebook.is_default" class="menu-item" @click="handleMenuSetDefault">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87L18.18 22 12 18.56 5.82 22 7 14.14l-5-4.87 6.91-1.01L12 2z"/>
          </svg>
          <span>设为默认</span>
        </button>
        <button v-else class="menu-item disabled" disabled>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M9 12l2 2 4-4"/>
            <path d="M21 12c0 1.66-.39 3.23-1.09 4.62A9 9 0 1 1 12 3a8.96 8.96 0 0 1 6 2.3"/>
          </svg>
          <span>当前默认</span>
        </button>
        <button v-if="!activeMenuNotebook.is_default" class="menu-item delete" @click="handleMenuDelete">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          </svg>
          <span>删除</span>
        </button>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { navigateWithMobileHistory } from '@/composables/useMobileNavigation'
import { useNotesStore } from '@/stores/notes'
import { useMobileUiStore } from '@/stores/mobileUi'
import { useToast } from '@/composables/useToast'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { notesApi } from '@/api/notes'
import type { Notebook } from '@/types'

const route = useRoute()
const router = useRouter()
const notesStore = useNotesStore()
const mobileUi = useMobileUiStore()
const { show } = useToast()
const { confirm: showConfirm } = useConfirmDialog()

onMounted(() => {
  notesStore.saveLastNotesPath(route.fullPath)
})

onUnmounted(() => {
  notesStore.saveLastNotesPath(route.fullPath)
})

const showCreateModal = ref(false)
const newNotebookName = ref('')

// Dynamic pagination: calculate cards per column based on viewport height to avoid scrollbars
const COLUMN_COUNT = 3
const CARD_HEIGHT = 130
const CARD_OVERLAP = 40
const LIST_PADDING_TOP = 48
const LIST_PADDING_BOTTOM = 32
const cardsPerColumn = ref(8)
const PAGE_SIZE = computed(() => cardsPerColumn.value * COLUMN_COUNT)
const currentPage = ref(1)
const totalPages = computed(() => Math.max(1, Math.ceil(notesStore.notebooks.length / PAGE_SIZE.value)))
const pagedNotebooks = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE.value
  return notesStore.notebooks.slice(start, start + PAGE_SIZE.value)
})
const stackedColumns = computed(() => {
  const items = pagedNotebooks.value
  const perCol = cardsPerColumn.value
  const cols: typeof items[] = []
  for (let i = 0; i < COLUMN_COUNT; i++) {
    const start = i * perCol
    cols.push(items.slice(start, start + perCol))
  }
  return cols
})
const notebooksListRef = ref<HTMLElement | null>(null)

function calculateCardsPerColumn() {
  const listEl = notebooksListRef.value
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
  const n = Math.floor((contentHeight - CARD_HEIGHT) / (CARD_HEIGHT - CARD_OVERLAP)) + 1
  cardsPerColumn.value = Math.max(1, n)
}

function goToPage(page: number) {
  const target = Math.min(Math.max(1, page), totalPages.value)
  if (target === currentPage.value) return
  currentPage.value = target
  nextTick(() => {
    notebooksListRef.value?.scrollTo({ top: 0 })
  })
}

let resizeObserver: ResizeObserver | null = null

function connectResizeObserver() {
  const el = notebooksListRef.value
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
watch(notebooksListRef, (el) => {
  if (el) {
    connectResizeObserver()
    nextTick(calculateCardsPerColumn)
  }
})

watch(() => notesStore.notebooks.length, () => {
  if (currentPage.value > totalPages.value) {
    currentPage.value = totalPages.value
  }
})
const editingNotebookId = ref<string | null>(null)
const editingName = ref('')
const editingInput = ref<HTMLInputElement | null>(null)
const activeMenuNotebook = ref<Notebook | null>(null)
const menuStyle = ref({ top: '0px', left: '0px' })
const selectionMode = ref<'export' | 'delete' | null>(null)
const selectedNotebookIds = ref<Set<string>>(new Set())
const selectionPending = ref(false)
const selectionProgress = ref('')
const swipedNotebookId = ref<string | null>(null)
const swipeOffset = ref(0)
const swipeTrackingId = ref<string | null>(null)
const swipeStartX = ref(0)
const swipeStartY = ref(0)
const swipeStartOffset = ref(0)
const isSwipeTracking = ref(false)
const isSwipeDragging = ref(false)

const SWIPE_ACTION_WIDTH = 240
let suppressNotebookClickUntil = 0

// Notebook search
const notebookSearchQuery = ref('')
const nbSearchLoading = ref(false)
let nbSearchDebounceTimer: ReturnType<typeof setTimeout> | null = null

function onNotebookSearchInput() {
  if (nbSearchDebounceTimer) clearTimeout(nbSearchDebounceTimer)
  nbSearchDebounceTimer = setTimeout(async () => {
    const q = notebookSearchQuery.value.trim()
    if (!q) {
      notesStore.noteSearchResults = []
      notesStore.noteSearchQuery = ''
      nbSearchLoading.value = false
      return
    }
    nbSearchLoading.value = true
    await notesStore.searchNotes(q)
    nbSearchLoading.value = false
  }, 300)
}

function clearNotebookSearch() {
  notebookSearchQuery.value = ''
  notesStore.noteSearchResults = []
  notesStore.noteSearchQuery = ''
  nbSearchLoading.value = false
  if (nbSearchDebounceTimer) clearTimeout(nbSearchDebounceTimer)
}

async function goToSearchResult(result: { note_id: string, notebook_id: string }) {
  const q = notebookSearchQuery.value.trim()
  notesStore.noteSearchHighlightQuery = q
  notesStore.noteSearchHighlightNonce++
  clearNotebookSearch()
  await navigateWithMobileHistory(router, `/notes/${result.notebook_id}/${result.note_id}`)
}

function goBackToChat() {
  void navigateWithMobileHistory(router, '/')
}

function highlightKeyword(text: string, keyword: string): string {
  if (!keyword.trim()) return text
  const escaped = keyword.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const regex = new RegExp(`(${escaped})`, 'gi')
  return text.replace(regex, '<mark>$1</mark>')
}

const deletableNotebookIds = computed(() => {
  return notesStore.notebooks.filter(notebook => !notebook.is_default).map(notebook => notebook.id)
})

const allSelected = computed(() => {
  return notesStore.notebooks.length > 0 && selectedNotebookIds.value.size === notesStore.notebooks.length
})

const allDeletableSelected = computed(() => {
  return deletableNotebookIds.value.length > 0 && selectedNotebookIds.value.size === deletableNotebookIds.value.length
})

onMounted(async () => {
  await notesStore.loadNotebooks()
  document.addEventListener('click', handleDocumentClick)
  nextTick(() => {
    connectResizeObserver()
    calculateCardsPerColumn()
  })
  window.addEventListener('resize', calculateCardsPerColumn)
})

onUnmounted(() => {
  document.removeEventListener('click', handleDocumentClick)
  closeSwipeActions()
  window.removeEventListener('resize', calculateCardsPerColumn)
  resizeObserver?.disconnect()
  resizeObserver = null
})

function handleDocumentClick() {
  closeMenu()
  if (!editingNotebookId.value) {
    closeSwipeActions()
  }
}

function closeMenu() {
  activeMenuNotebook.value = null
}

function closeSwipeActions() {
  swipedNotebookId.value = null
  swipeOffset.value = 0
  swipeTrackingId.value = null
  swipeStartOffset.value = 0
  isSwipeTracking.value = false
  isSwipeDragging.value = false
  mobileUi.closeRowAction('notebook')
}

function cancelEditNotebook() {
  editingNotebookId.value = null
  editingName.value = ''
}

async function openNotebook(id: string) {
  await notesStore.selectNotebook(id)
  await navigateWithMobileHistory(router, `/notes/${id}`)
}

function enterExportMode() {
  closeMenu()
  closeSwipeActions()
  cancelEditNotebook()
  selectionMode.value = 'export'
  selectedNotebookIds.value = new Set()
  selectionPending.value = false
  selectionProgress.value = ''
}

function enterDeleteMode() {
  closeMenu()
  closeSwipeActions()
  cancelEditNotebook()
  selectionMode.value = 'delete'
  selectedNotebookIds.value = new Set()
  selectionPending.value = false
  selectionProgress.value = ''
}

function exitSelectionMode() {
  selectionMode.value = null
  selectedNotebookIds.value = new Set()
  selectionPending.value = false
  selectionProgress.value = ''
}

async function createNotebook() {
  const name = newNotebookName.value.trim()
  if (!name) return

  try {
    await notesStore.createNotebook(name)
    show('笔记本已创建', 'success')
    newNotebookName.value = ''
    showCreateModal.value = false
  } catch (error) {
    console.error('Failed to create notebook:', error)
    show('创建笔记本失败', 'error')
  }
}

function showMenu(notebook: Notebook, event: MouseEvent) {
  if (activeMenuNotebook.value?.id === notebook.id) {
    closeMenu()
    return
  }
  closeSwipeActions()
  const target = event.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  menuStyle.value = {
    top: `${rect.bottom + 4}px`,
    left: `${rect.right - 160}px`,
  }
  activeMenuNotebook.value = notebook
}

async function startEditNotebook(notebook: Notebook) {
  closeMenu()
  closeSwipeActions()
  editingNotebookId.value = notebook.id
  editingName.value = notebook.name
  await nextTick()
  editingInput.value?.focus()
  editingInput.value?.select()
}

async function saveNotebook(id: string) {
  const name = editingName.value.trim()
  if (!name) {
    cancelEditNotebook()
    return
  }

  try {
    await notesStore.updateNotebook(id, name)
    show('笔记本名称已更新', 'success')
  } catch (error) {
    console.error('Failed to update notebook:', error)
    show('更新笔记本失败', 'error')
  } finally {
    cancelEditNotebook()
  }
}

async function handleExportNotebook(id: string) {
  closeMenu()
  closeSwipeActions()

  try {
    const result = await notesStore.exportNotebook(id)
    if (result.success) {
      show(result.path ? `已保存到 ${result.path}` : '笔记本导出成功', 'success')
    } else {
      show(result.error || '导出笔记本失败', 'error')
    }
  } catch (error) {
    console.error('Failed to export notebook:', error)
    show('导出笔记本失败', 'error')
  }
}

async function handleSetDefaultNotebook(id: string) {
  closeMenu()
  closeSwipeActions()

  try {
    await notesStore.setDefaultNotebook(id)
    show('默认笔记本已更新', 'success')
  } catch (error) {
    console.error('Failed to set default notebook:', error)
    show('设置默认笔记本失败', 'error')
  }
}

async function handleDeleteNotebook(id: string) {
  const notebook = notesStore.notebooks.find(item => item.id === id)
  closeMenu()
  closeSwipeActions()

  if (!notebook) {
    return
  }

  if (notebook.is_default) {
    show('默认笔记本不能删除', 'error')
    return
  }

  if (!await showConfirm({ message: '确定要删除这个笔记本吗？\n所有笔记也会被删除。', danger: true, confirmText: '删除' })) {
    return
  }

  try {
    await notesStore.deleteNotebook(id)
    show('笔记本已删除', 'success')
  } catch (error) {
    console.error('Failed to delete notebook:', error)
    show('删除笔记本失败', 'error')
  }
}

function toggleNotebookSelection(id: string) {
  const notebook = notesStore.notebooks.find(item => item.id === id)
  if (!notebook) return
  if (selectionMode.value === 'delete' && notebook.is_default) return

  const nextSelected = new Set(selectedNotebookIds.value)
  if (nextSelected.has(id)) {
    nextSelected.delete(id)
  } else {
    nextSelected.add(id)
  }
  selectedNotebookIds.value = nextSelected
}

function toggleSelectAll() {
  if (selectionMode.value === 'delete') {
    if (allDeletableSelected.value) {
      selectedNotebookIds.value = new Set()
    } else {
      selectedNotebookIds.value = new Set(deletableNotebookIds.value)
    }
  } else {
    if (allSelected.value) {
      selectedNotebookIds.value = new Set()
    } else {
      selectedNotebookIds.value = new Set(notesStore.notebooks.map(nb => nb.id))
    }
  }
}

async function handleBulkExport() {
  if (selectedNotebookIds.value.size === 0) return

  selectionPending.value = true
  selectionProgress.value = '正在导出...'

  try {
    const result = await notesApi.bulkExportNotebooks(Array.from(selectedNotebookIds.value))
    if (result.success) {
      selectionProgress.value = result.path ? `已保存到 ${result.path}` : '导出成功！'
    } else {
      selectionProgress.value = result.error || '导出失败'
      show(result.error || '批量导出笔记本失败', 'error')
    }
    setTimeout(() => {
      exitSelectionMode()
    }, 1200)
  } catch (error) {
    console.error('Failed to bulk export notebooks:', error)
    selectionProgress.value = '导出失败'
    show('批量导出笔记本失败', 'error')
  } finally {
    selectionPending.value = false
  }
}

async function handleBulkDelete() {
  if (selectedNotebookIds.value.size === 0) {
    return
  }

  if (!await showConfirm({ message: '确定要删除选中的笔记本吗？默认笔记本不会被删除。', danger: true, confirmText: '删除' })) {
    return
  }

  try {
    await notesStore.bulkDeleteNotebooks(Array.from(selectedNotebookIds.value))
    show('已删除选中的笔记本', 'success')
    exitSelectionMode()
  } catch (error) {
    console.error('Failed to bulk delete notebooks:', error)
    show('批量删除笔记本失败', 'error')
  }
}

function getNotebookItemStyle(id: string): Record<string, string> {
  if (selectionMode.value || swipedNotebookId.value !== id) {
    return { transform: 'translateX(0px)' }
  }

  return {
    transform: `translateX(${swipeOffset.value}px)`,
  }
}

async function handleNotebookClick(notebook: Notebook) {
  if (Date.now() < suppressNotebookClickUntil) {
    return
  }

  if (selectionMode.value) {
    toggleNotebookSelection(notebook.id)
    return
  }

  if (editingNotebookId.value === notebook.id) {
    return
  }

  if (swipedNotebookId.value === notebook.id) {
    closeSwipeActions()
    return
  }

  await openNotebook(notebook.id)
}

function handleTouchStart(event: TouchEvent, notebookId: string) {
  if (selectionMode.value || editingNotebookId.value || event.touches.length !== 1) {
    return
  }

  const touch = event.touches[0]
  swipeTrackingId.value = notebookId
  swipeStartX.value = touch.clientX
  swipeStartY.value = touch.clientY
  swipeStartOffset.value = swipedNotebookId.value === notebookId ? swipeOffset.value : 0
  isSwipeTracking.value = true
  isSwipeDragging.value = false

  if (swipedNotebookId.value && swipedNotebookId.value !== notebookId) {
    closeSwipeActions()
    swipeTrackingId.value = notebookId
    swipeStartX.value = touch.clientX
    swipeStartY.value = touch.clientY
    swipeStartOffset.value = 0
    isSwipeTracking.value = true
    isSwipeDragging.value = false
  }

  if (swipedNotebookId.value === notebookId) {
    event.stopPropagation()
    mobileUi.openRowAction('notebook', notebookId)
  }
}

function handleTouchMove(event: TouchEvent, notebookId: string) {
  if (
    selectionMode.value ||
    !isSwipeTracking.value ||
    swipeTrackingId.value !== notebookId ||
    event.touches.length !== 1
  ) {
    return
  }

  const touch = event.touches[0]
  const deltaX = touch.clientX - swipeStartX.value
  const deltaY = touch.clientY - swipeStartY.value

  if (!isSwipeDragging.value) {
    if (Math.abs(deltaY) > 10 && Math.abs(deltaY) > Math.abs(deltaX)) {
      swipeTrackingId.value = null
      isSwipeTracking.value = false
      mobileUi.stopRowActionDrag('notebook')
      return
    }

    if (Math.abs(deltaX) < 10) {
      return
    }

    if (deltaX > 0 && swipeStartOffset.value === 0) {
      swipeTrackingId.value = null
      isSwipeTracking.value = false
      mobileUi.stopRowActionDrag('notebook')
      return
    }

    isSwipeDragging.value = true
  }

  event.preventDefault()
  mobileUi.startRowActionDrag('notebook', notebookId)
  swipedNotebookId.value = notebookId
  swipeOffset.value = Math.max(-SWIPE_ACTION_WIDTH, Math.min(0, swipeStartOffset.value + deltaX))
}

function handleTouchEnd() {
  if (!isSwipeTracking.value) {
    return
  }

  if (isSwipeDragging.value) {
    suppressNotebookClickUntil = Date.now() + 300
    if (swipeOffset.value <= -SWIPE_ACTION_WIDTH / 2 && swipeTrackingId.value) {
      swipedNotebookId.value = swipeTrackingId.value
      swipeOffset.value = -SWIPE_ACTION_WIDTH
      mobileUi.openRowAction('notebook', swipeTrackingId.value)
    } else {
      closeSwipeActions()
      return
    }
  }

  swipeTrackingId.value = null
  swipeStartOffset.value = swipedNotebookId.value ? -SWIPE_ACTION_WIDTH : 0
  isSwipeTracking.value = false
  isSwipeDragging.value = false
  mobileUi.stopRowActionDrag('notebook')
}

function handleMenuEdit() {
  if (!activeMenuNotebook.value) return
  void startEditNotebook(activeMenuNotebook.value)
}

function handleMenuExport() {
  if (!activeMenuNotebook.value) return
  void handleExportNotebook(activeMenuNotebook.value.id)
}

function handleMenuSetDefault() {
  if (!activeMenuNotebook.value) return
  void handleSetDefaultNotebook(activeMenuNotebook.value.id)
}

function handleMenuDelete() {
  if (!activeMenuNotebook.value) return
  void handleDeleteNotebook(activeMenuNotebook.value.id)
}
</script>

<style scoped>
.notebooks-page {
  flex: 1;
  min-height: 0;
  height: 0;
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

.page-title {
  flex: 1;
  font-size: 18px;
  font-weight: 600;
  color: var(--color-text);
}

.add-btn {
  padding: 8px;
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
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

.export-confirm-btn {
  flex: 1;
  padding: 8px 12px;
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
}

.export-confirm-btn:hover:not(:disabled) {
  filter: brightness(1.06);
}

.export-confirm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.selection-progress {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-primary);
  text-align: center;
}

.selection-cancel-btn {
  padding: 8px 12px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  font-size: 13px;
}

.notebooks-list {
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

.notebook-row {
  position: relative;
  margin-bottom: 8px;
  border-radius: var(--radius-md);
  overflow: hidden;
}

.notebook-actions {
  position: absolute;
  inset: 0 0 0 auto;
  width: 240px;
  display: flex;
  align-items: stretch;
  justify-content: flex-end;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transition: opacity var(--transition-fast), visibility var(--transition-fast);
}

.notebook-row.swipe-open .notebook-actions {
  opacity: 1;
  visibility: visible;
  pointer-events: auto;
}

.swipe-action {
  width: 60px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: white;
  font-size: 10px;
  font-weight: 500;
}

.swipe-action.export {
  background-color: var(--color-primary);
}

.swipe-action.edit {
  background-color: var(--action-edit-bg);
}

.swipe-action.default {
  background-color: var(--color-success);
}

.swipe-action.default.active {
  background-color: var(--success-strong);
}

.swipe-action.delete {
  background-color: var(--color-error);
}

.swipe-action:disabled {
  opacity: 0.55;
}

.notebook-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: transform var(--transition-fast), background-color var(--transition-fast), box-shadow var(--transition-fast);
  position: relative;
  z-index: 1;
}

.notebook-item:hover {
  background-color: var(--color-hover);
  box-shadow: var(--shadow-sm);
}

.selection-checkbox {
  cursor: pointer;
  accent-color: var(--color-primary);
}

.notebook-icon {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
  border-radius: var(--radius-sm);
}

.notebook-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.notebook-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.notebook-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text);
}

.notebook-count {
  font-size: 12px;
  color: var(--color-text-light);
}

.default-badge {
  font-size: 11px;
  padding: 4px 8px;
  background-color: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.selection-disabled-tip {
  font-size: 11px;
  color: var(--color-text-light);
  flex-shrink: 0;
}

.inline-editor {
  display: flex;
  align-items: center;
  gap: 8px;
}

.notebook-name-input {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-sm);
  font-size: 14px;
  color: var(--color-text);
  background: var(--surface-panel-subtle);
  outline: none;
  box-shadow: 0 0 0 3px var(--focus-ring-color);
}

.inline-editor-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.inline-action {
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
}

.inline-action.save {
  background: var(--color-primary);
  color: white;
}

.inline-action.cancel {
  background-color: var(--surface-panel-subtle);
  color: var(--color-text);
  border: 1px solid var(--panel-border);
}

.menu-btn {
  opacity: 0;
  padding: 4px 6px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  flex-shrink: 0;
}

.notebook-item:hover .menu-btn {
  opacity: 1;
}

.menu-btn:hover {
  color: var(--color-text);
  background-color: var(--surface-panel-subtle);
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
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-md);
  font-weight: 500;
}

.modal-overlay {
  position: fixed;
  inset: 0;
  background-color: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-content {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--dialog-radius);
  box-shadow: var(--dialog-shadow);
  padding: 20px;
  width: 90%;
  max-width: 400px;
}

.modal-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 16px;
}

.modal-input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  font-size: 14px;
  margin-bottom: 16px;
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.modal-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--focus-ring-color);
}

.modal-actions {
  display: flex;
  gap: 12px;
  justify-content: flex-end;
}

.cancel-btn {
  padding: 8px 16px;
  background-color: var(--surface-panel-subtle);
  color: var(--color-text);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
}

.confirm-btn {
  padding: 8px 16px;
  background: var(--color-primary);
  color: white;
  border-radius: var(--radius-sm);
}

.confirm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

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

.menu-item:hover:not(.disabled) {
  background-color: var(--color-hover);
}

.menu-item.delete {
  color: var(--color-error);
}

.menu-item.delete:hover {
  background-color: color-mix(in srgb, var(--color-error) 8%, transparent);
}

.menu-item.disabled {
  color: var(--color-text-light);
  cursor: default;
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

  .notebooks-list {
    padding: 8px;
    padding-bottom: calc(8px + var(--mobile-tab-bar-offset, 60px) + env(safe-area-inset-bottom, 0px));
  }

  .pagination-bar {
    margin-bottom: calc(var(--mobile-tab-bar-offset, 60px) + env(safe-area-inset-bottom, 0px));
  }

  .notebook-item {
    padding: 12px 14px;
  }

  .notebook-icon {
    width: 36px;
    height: 36px;
  }

  .inline-editor {
    flex-direction: column;
    align-items: stretch;
  }

  .inline-editor-actions {
    justify-content: flex-end;
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

.search-result-notebook {
  font-size: 11px;
  color: var(--color-primary);
  margin-bottom: 2px;
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

/* Desktop: 3 columns responsive wallet-stack, hide swipe actions */
@media (min-width: 768px) {
  .notebooks-list {
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

  .stack-column > .notebook-row {
    margin-bottom: 0;
    display: flex;
    transition: transform 220ms cubic-bezier(0.22, 1, 0.36, 1),
                box-shadow 220ms cubic-bezier(0.22, 1, 0.36, 1);
    box-shadow: var(--stack-card-shadow);
    border-radius: var(--stack-card-radius);
    contain: paint;
  }

  .stack-column > .notebook-row:not(:first-child) {
    margin-top: var(--stack-overlap);
  }

  .stack-column > .notebook-row:hover {
    transform: translateY(var(--stack-hover-lift));
    box-shadow: var(--stack-card-shadow-hover);
  }

  .notebook-item {
    height: 130px;
    overflow: hidden;
    width: 100%;
    box-sizing: border-box;
  }

  .notebook-actions {
    display: none;
  }
}
</style>
