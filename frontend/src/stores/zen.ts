// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useNotesStore } from './notes'

export const useZenStore = defineStore('zen', () => {
  const currentNotebookId = ref('')
  const currentNoteId = ref('')

  function setCurrentNote(notebookId: string, noteId: string) {
    currentNotebookId.value = notebookId
    currentNoteId.value = noteId
  }

  async function loadDefaultNote() {
    const notesStore = useNotesStore()
    if (!notesStore.notebooks.length) {
      await notesStore.loadNotebooks()
    }
    if (notesStore.notebooks.length > 0 && !currentNotebookId.value) {
      const firstNotebook = notesStore.notebooks[0]
      currentNotebookId.value = firstNotebook.id
      if (!notesStore.notes[firstNotebook.id]) {
        await notesStore.loadNotes(firstNotebook.id)
      }
      const notes = notesStore.notes[firstNotebook.id] || []
      if (notes.length > 0) {
        currentNoteId.value = notes[0].id
      }
    }
  }

  return {
    currentNotebookId,
    currentNoteId,
    setCurrentNote,
    loadDefaultNote,
  }
})
