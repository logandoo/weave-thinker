// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import { downloadBlob, type DownloadResult } from '@/composables/useDownload'
import type { Notebook, Note, NoteListItem, BulkDeleteResult, BulkMoveResult, NoteSearchResult } from '@/types'

// 尾注开关（与 stores/editorPrefs 同源 localStorage 键）——导出渲染随开关（wave-3 #5）
function endnoteExportQuery(): { endnote_enabled: boolean } {
  return { endnote_enabled: localStorage.getItem('wt-endnote-enabled') === '1' }
}

export const notesApi = {
  async getNotebooks(): Promise<Notebook[]> {
    const { data } = await api.get<Notebook[]>('/notes/notebooks')
    return data
  },

  async createNotebook(name: string): Promise<Notebook> {
    const { data } = await api.post<Notebook>('/notes/notebooks', { name })
    return data
  },

  async updateNotebook(id: string, name: string): Promise<Notebook> {
    const { data } = await api.put<Notebook>(`/notes/notebooks/${id}`, { name })
    return data
  },

  async deleteNotebook(id: string): Promise<void> {
    await api.delete(`/notes/notebooks/${id}`)
  },

  async bulkDeleteNotebooks(notebookIds: string[]): Promise<BulkDeleteResult> {
    const { data } = await api.post<BulkDeleteResult>('/notes/notebooks/bulk-delete', {
      notebook_ids: notebookIds,
    })
    return data
  },

  async setDefaultNotebook(id: string): Promise<Notebook> {
    const { data } = await api.put<Notebook>(`/notes/notebooks/${id}/default`)
    return data
  },

  async exportNotebook(id: string): Promise<DownloadResult> {
    // responseType:'blob'（2026-10-05 上游TODO项5）：二进制/大导出语义——
    // axios 直收 Blob，不再经 string→Blob 往返（大 CSV 字符串化有内存/编码损耗）。
    const response = await api.get(`/notes/notebooks/${id}/export`, { params: { ...endnoteExportQuery() }, responseType: 'blob' })
    const blob = response.data as Blob
    const contentDisposition = response.headers['content-disposition']
    let filename = 'notebook.csv'
    if (contentDisposition) {
      const match = contentDisposition.match(/filename="?([^\"]+)"?/) 
      if (match) filename = match[1]
    }

    return await downloadBlob(blob, filename, 'text/csv')
  },

  async getNotes(notebookId: string): Promise<NoteListItem[]> {
    const { data } = await api.get<NoteListItem[]>(`/notes/notebooks/${notebookId}/notes`)
    return data
  },

  async getNote(noteId: string): Promise<Note> {
    const { data } = await api.get<Note>(`/notes/notes/${noteId}`)
    return data
  },

  async createNote(notebookId: string, note: { title?: string; content: string; raw_transcription?: string }): Promise<Note> {
    const { data } = await api.post<Note>(`/notes/notebooks/${notebookId}/notes`, note)
    return data
  },

  async updateNote(noteId: string, note: { title?: string; content?: string }): Promise<Note> {
    const { data } = await api.put<Note>(`/notes/notes/${noteId}`, note)
    return data
  },

  async deleteNote(noteId: string): Promise<void> {
    await api.delete(`/notes/notes/${noteId}`)
  },

  async bulkDeleteNotes(noteIds: string[]): Promise<BulkDeleteResult> {
    const { data } = await api.post<BulkDeleteResult>('/notes/notes/bulk-delete', {
      note_ids: noteIds,
    })
    return data
  },

  async createQuickNote(transcription: string, notebookId?: string): Promise<Note> {
    const { data } = await api.post<Note>('/notes/quick', { transcription, notebook_id: notebookId })
    return data
  },

  async getDefaultNotebook(): Promise<Notebook> {
    const { data } = await api.get<Notebook>('/notes/default-notebook')
    return data
  },

  async moveNote(noteId: string, targetNotebookId: string): Promise<Note> {
    const { data } = await api.put<Note>(`/notes/notes/${noteId}/move`, {
      target_notebook_id: targetNotebookId,
    })
    return data
  },

  async bulkMoveNotes(noteIds: string[], targetNotebookId: string): Promise<BulkMoveResult> {
    const { data } = await api.post<BulkMoveResult>('/notes/notes/bulk-move', {
      note_ids: noteIds,
      target_notebook_id: targetNotebookId,
    })
    return data
  },

  async bulkExportNotebooks(notebookIds: string[]): Promise<DownloadResult> {
    const response = await api.post('/notes/notebooks/bulk-export', {
      notebook_ids: notebookIds,
    }, { responseType: 'blob' })
    const blob = new Blob([response.data], { type: 'application/zip' })
    const contentDisposition = response.headers['content-disposition']
    let filename = 'notebooks_export.zip'
    if (contentDisposition) {
      const match = contentDisposition.match(/filename="?([^"]+)"?/)
      if (match) filename = match[1]
    }
    return await downloadBlob(blob, filename, 'application/zip')
  },

  async searchNotes(query: string, notebookId?: string): Promise<NoteSearchResult[]> {
    const params: Record<string, string> = { q: query }
    if (notebookId) params.notebook_id = notebookId
    const { data } = await api.get<NoteSearchResult[]>('/notes/search', { params })
    return data
  },

  async exportNote(noteId: string, format: 'md' | 'pdf' = 'md', signal?: AbortSignal): Promise<DownloadResult> {
    const response = await api.get(`/notes/notes/${noteId}/export`, {
      params: { format, ...endnoteExportQuery() },
      responseType: 'blob',
      signal,
    })
    const mimeType = format === 'pdf' ? 'application/pdf' : 'text/markdown'
    const blob = new Blob([response.data], { type: mimeType })
    const contentDisposition = response.headers['content-disposition']
    let filename = `note.${format}`
    if (contentDisposition) {
      const match = contentDisposition.match(/filename\*=UTF-8''([^;]+)/)
      if (match) filename = decodeURIComponent(match[1])
      else {
        const fallback = contentDisposition.match(/filename="?([^"]+)"?/)
        if (fallback) filename = fallback[1]
      }
    }
    return await downloadBlob(blob, filename, mimeType)
  },

  async bulkExportNotes(noteIds: string[], format: 'md' | 'pdf' = 'md', signal?: AbortSignal): Promise<DownloadResult> {
    const response = await api.post('/notes/notes/bulk-export', {
      note_ids: noteIds,
      format,
    }, { responseType: 'blob', signal })
    const blob = new Blob([response.data], { type: 'application/zip' })
    return await downloadBlob(blob, 'notes_export.zip', 'application/zip')
  },
}
