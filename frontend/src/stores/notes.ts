// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Notebook, Note, NoteListItem, NoteSearchResult, ExportTaskInfo } from '@/types'
import { notesApi } from '@/api/notes'
import { exportTasksApi } from '@/api/exportTasks'
import { downloadBlob, type DownloadResult } from '@/composables/useDownload'

function buildContentPreview(content: string, maxLength = 100) {
  const text = content.trim()
  if (text.length <= maxLength) {
    return text
  }
  return `${text.slice(0, maxLength)}...`
}

export const useNotesStore = defineStore('notes', () => {
  const notebooks = ref<Notebook[]>([])
  const currentNotebookId = ref<string | null>(null)
  const notes = ref<Record<string, NoteListItem[]>>({})
  const currentNote = ref<Note | null>(null)
  const defaultNotebook = ref<Notebook | null>(null)
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  let notebooksLoadRequestId = 0

  const currentNotebook = computed(() => {
    if (!currentNotebookId.value) return null
    return notebooks.value.find(n => n.id === currentNotebookId.value) || null
  })

  const currentNotes = computed(() => {
    if (!currentNotebookId.value) return []
    return notes.value[currentNotebookId.value] || []
  })

  function findNoteListItem(noteId: string): NoteListItem | null {
    for (const noteList of Object.values(notes.value)) {
      const item = noteList.find(note => note.id === noteId)
      if (item) {
        return item
      }
    }
    return null
  }

  function applyNotebookUpdate(updated: Notebook) {
    notebooks.value = notebooks.value.map(notebook => {
      if (notebook.id === updated.id) {
        return { ...notebook, ...updated }
      }

      if (updated.is_default && notebook.is_default) {
        return { ...notebook, is_default: false }
      }

      return notebook
    })

    if (updated.is_default) {
      defaultNotebook.value = updated
    }
  }

  async function loadNotebooks() {
    const requestId = ++notebooksLoadRequestId
    isLoading.value = true
    error.value = null
    try {
      const loadedNotebooks = await notesApi.getNotebooks()
      if (requestId !== notebooksLoadRequestId) {
        return
      }

      notebooks.value = loadedNotebooks
      defaultNotebook.value = loadedNotebooks.find(notebook => notebook.is_default) || null
    } catch (e: any) {
      if (requestId === notebooksLoadRequestId) {
        error.value = e.message || '加载笔记本失败'
      }
      console.error('Failed to load notebooks:', e)
    } finally {
      if (requestId === notebooksLoadRequestId) {
        isLoading.value = false
      }
    }
  }

  async function createNotebook(name: string) {
    const notebook = await notesApi.createNotebook(name)
    notebooks.value.unshift(notebook)
    await loadNotebooks()
    return notebook
  }

  async function updateNotebook(id: string, name: string) {
    const updated = await notesApi.updateNotebook(id, name)
    applyNotebookUpdate(updated)
    await loadNotebooks()
    return updated
  }

  async function setDefaultNotebook(id: string) {
    const updated = await notesApi.setDefaultNotebook(id)
    applyNotebookUpdate(updated)
    await loadNotebooks()
    return updated
  }

  async function exportNotebook(id: string) {
    return await notesApi.exportNotebook(id)
  }

  async function exportNote(noteId: string, format: 'md' | 'pdf' = 'md', signal?: AbortSignal) {
    return await notesApi.exportNote(noteId, format, signal)
  }

  async function bulkExportNotes(noteIds: string[], format: 'md' | 'pdf' = 'md', signal?: AbortSignal) {
    return await notesApi.bulkExportNotes(noteIds, format, signal)
  }

  async function deleteNotebook(id: string) {
    await notesApi.deleteNotebook(id)
    notebooks.value = notebooks.value.filter(n => n.id !== id)
    delete notes.value[id]
    if (currentNotebookId.value === id) {
      currentNotebookId.value = null
    }
    await loadNotebooks()
  }

  async function bulkDeleteNotebooks(ids: string[]) {
    if (ids.length === 0) return

    await notesApi.bulkDeleteNotebooks(ids)
    const idSet = new Set(ids)

    notebooks.value = notebooks.value.filter(notebook => !idSet.has(notebook.id))
    for (const id of ids) {
      delete notes.value[id]
    }

    if (currentNotebookId.value && idSet.has(currentNotebookId.value)) {
      currentNotebookId.value = null
      currentNote.value = null
    }

    await loadNotebooks()
  }

  async function selectNotebook(id: string) {
    currentNotebookId.value = id
    currentNote.value = null
    await loadNotes(id)
  }

  async function loadNotes(notebookId: string) {
    isLoading.value = true
    error.value = null
    try {
      notes.value[notebookId] = await notesApi.getNotes(notebookId)
    } catch (e: any) {
      error.value = e.message || '加载笔记失败'
      console.error('Failed to load notes:', e)
    } finally {
      isLoading.value = false
    }
  }

  async function loadNote(noteId: string) {
    isLoading.value = true
    error.value = null
    try {
      currentNote.value = await notesApi.getNote(noteId)
    } catch (e: any) {
      error.value = e.message || '加载笔记失败'
      console.error('Failed to load note:', e)
    } finally {
      isLoading.value = false
    }
  }

  // Silently reload notebooks + every already-cached notebook's notes.
  // Used after the agent notes tool mutates data server-side, so open drawers
  // and lists reflect the latest notes/notebooks without toggling isLoading
  // (which would flash a loading spinner on the active NotesList page).
  async function refreshFromExternalChange() {
    const requestId = ++notebooksLoadRequestId
    try {
      const loadedNotebooks = await notesApi.getNotebooks()
      if (requestId !== notebooksLoadRequestId) return
      notebooks.value = loadedNotebooks
      defaultNotebook.value = loadedNotebooks.find(n => n.is_default) || null
    } catch (e) {
      console.error('Failed to refresh notebooks from external change:', e)
    }
    const cachedNotebookIds = Object.keys(notes.value)
    await Promise.all(cachedNotebookIds.map(async (id) => {
      try {
        notes.value[id] = await notesApi.getNotes(id)
      } catch (e) {
        console.error('Failed to refresh notes for notebook:', id, e)
      }
    }))
  }

  async function createNote(notebookId: string, data: { title?: string; content: string; raw_transcription?: string }) {
    const note = await notesApi.createNote(notebookId, data)
    if (!notes.value[notebookId]) {
      notes.value[notebookId] = []
    }
    notes.value[notebookId].unshift({
      id: note.id,
      notebook_id: note.notebook_id,
      title: note.title,
      content_preview: buildContentPreview(note.content),
      created_at: note.created_at,
      updated_at: note.updated_at
    })
    const nb = notebooks.value.find(n => n.id === notebookId)
    if (nb) {
      nb.note_count++
    }
    return note
  }

  async function updateNote(noteId: string, data: { title?: string; content?: string }) {
    const note = await notesApi.updateNote(noteId, data)
    currentNote.value = note

    const noteList = notes.value[note.notebook_id]
    if (noteList) {
      const item = noteList.find(n => n.id === noteId)
      if (item) {
        item.title = note.title
        item.content_preview = buildContentPreview(note.content)
        item.updated_at = note.updated_at
      }
    }
    return note
  }

  async function deleteNote(noteId: string) {
    const note = currentNote.value?.id === noteId ? currentNote.value : findNoteListItem(noteId)
    await notesApi.deleteNote(noteId)
    if (note) {
      const noteList = notes.value[note.notebook_id]
      if (noteList) {
        notes.value[note.notebook_id] = noteList.filter(n => n.id !== noteId)
        const nb = notebooks.value.find(n => n.id === note.notebook_id)
        if (nb) {
          nb.note_count--
        }
      }
    }

    if (currentNote.value?.id === noteId) {
      currentNote.value = null
    }
  }

  async function bulkDeleteNotes(noteIds: string[]) {
    if (noteIds.length === 0) return

    await notesApi.bulkDeleteNotes(noteIds)
    const idSet = new Set(noteIds)
    const deletedCounts = new Map<string, number>()

    for (const [notebookId, noteList] of Object.entries(notes.value)) {
      const beforeCount = noteList.length
      const filtered = noteList.filter(note => !idSet.has(note.id))
      if (filtered.length !== beforeCount) {
        notes.value[notebookId] = filtered
        deletedCounts.set(notebookId, beforeCount - filtered.length)
      }
    }

    notebooks.value = notebooks.value.map(notebook => {
      const deletedCount = deletedCounts.get(notebook.id)
      if (!deletedCount) {
        return notebook
      }

      return {
        ...notebook,
        note_count: Math.max(0, notebook.note_count - deletedCount),
      }
    })

    if (currentNote.value && idSet.has(currentNote.value.id)) {
      currentNote.value = null
    }
  }

  async function createQuickNote(transcription: string, notebookId?: string) {
    isLoading.value = true
    error.value = null
    try {
      const note = await notesApi.createQuickNote(transcription, notebookId)
      if (!notes.value[note.notebook_id]) {
        notes.value[note.notebook_id] = []
      }
      notes.value[note.notebook_id].unshift({
        id: note.id,
        notebook_id: note.notebook_id,
        title: note.title,
        content_preview: buildContentPreview(note.content),
        created_at: note.created_at,
        updated_at: note.updated_at
      })
      const nb = notebooks.value.find(n => n.id === note.notebook_id)
      if (nb) {
        nb.note_count++
      }
      return note
    } catch (e: any) {
      error.value = e.message || '创建快速笔记失败'
      console.error('Failed to create quick note:', e)
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function loadDefaultNotebook() {
    try {
      defaultNotebook.value = await notesApi.getDefaultNotebook()
    } catch (e) {
      console.error('Failed to load default notebook:', e)
    }
  }

  async function moveNote(noteId: string, targetNotebookId: string) {
    const note = await notesApi.moveNote(noteId, targetNotebookId)

    // Remove from source notebook list
    for (const [notebookId, noteList] of Object.entries(notes.value)) {
      const idx = noteList.findIndex(n => n.id === noteId)
      if (idx !== -1) {
        noteList.splice(idx, 1)
        const srcNb = notebooks.value.find(nb => nb.id === notebookId)
        if (srcNb) srcNb.note_count = Math.max(0, srcNb.note_count - 1)
        break
      }
    }

    // Add to target notebook list if loaded
    if (notes.value[targetNotebookId]) {
      notes.value[targetNotebookId].unshift({
        id: note.id,
        notebook_id: note.notebook_id,
        title: note.title,
        content_preview: buildContentPreview(note.content),
        created_at: note.created_at,
        updated_at: note.updated_at,
      })
    }
    const targetNb = notebooks.value.find(nb => nb.id === targetNotebookId)
    if (targetNb) targetNb.note_count++

    if (currentNote.value?.id === noteId) {
      currentNote.value = note
    }

    return note
  }

  async function bulkMoveNotes(noteIds: string[], targetNotebookId: string) {
    if (noteIds.length === 0) return
    const result = await notesApi.bulkMoveNotes(noteIds, targetNotebookId)
    const idSet = new Set(noteIds)

    // Remove from source lists
    for (const [notebookId, noteList] of Object.entries(notes.value)) {
      const beforeLen = noteList.length
      notes.value[notebookId] = noteList.filter(n => !idSet.has(n.id))
      const removed = beforeLen - notes.value[notebookId].length
      if (removed > 0) {
        const srcNb = notebooks.value.find(nb => nb.id === notebookId)
        if (srcNb) srcNb.note_count = Math.max(0, srcNb.note_count - removed)
      }
    }

    // Update target notebook count
    const targetNb = notebooks.value.find(nb => nb.id === targetNotebookId)
    if (targetNb) targetNb.note_count += result.moved_count

    // Reload target notes if loaded
    if (notes.value[targetNotebookId]) {
      await loadNotes(targetNotebookId)
    }

    if (currentNote.value && idSet.has(currentNote.value.id)) {
      currentNote.value = null
    }

    return result
  }

  async function exportNoteAsync(
    noteId: string,
    format: 'md' | 'pdf',
    onProgress?: (task: ExportTaskInfo) => void,
  ): Promise<DownloadResult> {
    const task = await exportTasksApi.create({ task_type: 'single', format, note_id: noteId })
    onProgress?.(task)
    const result = await pollExportTask(task.id, onProgress)
    return await downloadExportResult(result)
  }

  async function bulkExportNotesAsync(
    noteIds: string[],
    format: 'md' | 'pdf',
    onProgress?: (task: ExportTaskInfo) => void,
  ): Promise<DownloadResult> {
    const task = await exportTasksApi.create({ task_type: 'bulk', format, note_ids: noteIds })
    onProgress?.(task)
    const result = await pollExportTask(task.id, onProgress)
    return await downloadExportResult(result)
  }

  async function pollExportTask(
    taskId: string,
    onProgress?: (task: ExportTaskInfo) => void,
  ): Promise<ExportTaskInfo> {
    return new Promise((resolve, reject) => {
      const interval = setInterval(async () => {
        try {
          const task = await exportTasksApi.get(taskId)
          onProgress?.(task)
          if (task.status === 'completed') {
            clearInterval(interval)
            resolve(task)
          } else if (task.status === 'failed') {
            clearInterval(interval)
            reject(new Error(task.error || '导出失败'))
          } else if (task.status === 'cancelled') {
            clearInterval(interval)
            reject(new Error('导出已取消'))
          }
        } catch (e) {
          clearInterval(interval)
          reject(e)
        }
      }, 2000)
    })
  }

  async function downloadExportResult(task: ExportTaskInfo): Promise<DownloadResult> {
    const url = exportTasksApi.getDownloadUrl(task.id)
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${localStorage.getItem('chatllm_token')}` },
    })
    if (!response.ok) throw new Error('下载失败')
    const blob = await response.blob()
    const filename = task.filename || (task.format === 'pdf' ? 'export.pdf' : 'export.md')
    const mimeType = task.format === 'pdf'
      ? 'application/pdf'
      : task.task_type === 'bulk'
        ? 'application/zip'
        : 'text/markdown'
    return await downloadBlob(blob, filename, mimeType)
  }

  async function cancelExportTask(taskId: string): Promise<void> {
    await exportTasksApi.cancel(taskId)
  }

  function resetState() {
    notebooks.value = []
    currentNotebookId.value = null
    notes.value = {}
    currentNote.value = null
    defaultNotebook.value = null
    isLoading.value = false
    error.value = null
    noteSearchResults.value = []
    noteSearchQuery.value = ''
  }

  const noteSearchResults = ref<NoteSearchResult[]>([])
  const noteSearchQuery = ref('')
  // One-shot query consumed by NoteEditor to highlight + scroll to a keyword
  // after navigating from a search result.
  const noteSearchHighlightQuery = ref('')
  const noteSearchHighlightNonce = ref(0)
  const lastNotesPath = ref<string>('/notes')

  function saveLastNotesPath(path: string) {
    if (!path.startsWith('/notes')) {
      return
    }
    lastNotesPath.value = path
  }

  async function searchNotes(query: string, notebookId?: string) {
    noteSearchQuery.value = query
    if (!query.trim()) {
      noteSearchResults.value = []
      return
    }
    try {
      noteSearchResults.value = await notesApi.searchNotes(query, notebookId)
    } catch (e) {
      console.error('Failed to search notes:', e)
      noteSearchResults.value = []
    }
  }

  return {
    notebooks,
    currentNotebookId,
    notes,
    currentNote,
    defaultNotebook,
    isLoading,
    error,
    currentNotebook,
    currentNotes,
    noteSearchResults,
    noteSearchQuery,
    noteSearchHighlightQuery,
    noteSearchHighlightNonce,
    loadNotebooks,
    createNotebook,
    updateNotebook,
    setDefaultNotebook,
    exportNotebook,
    exportNote,
    bulkExportNotes,
    exportNoteAsync,
    bulkExportNotesAsync,
    cancelExportTask,
    deleteNotebook,
    bulkDeleteNotebooks,
    selectNotebook,
    loadNotes,
    loadNote,
    refreshFromExternalChange,
    createNote,
    updateNote,
    deleteNote,
    bulkDeleteNotes,
    createQuickNote,
    loadDefaultNotebook,
    moveNote,
    bulkMoveNotes,
    searchNotes,
    resetState,
    lastNotesPath,
    saveLastNotesPath
  }
})
