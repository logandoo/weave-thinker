# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel
from typing import Optional, List


class NotebookCreate(BaseModel):
    name: str


class NotebookUpdate(BaseModel):
    name: str


class NotebookResponse(BaseModel):
    id: str
    name: str
    is_default: bool
    created_at: str
    updated_at: str
    note_count: int = 0


class NoteCreate(BaseModel):
    title: Optional[str] = None
    content: str = ""
    raw_transcription: Optional[str] = None


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class NoteResponse(BaseModel):
    id: str
    notebook_id: str
    title: Optional[str]
    content: str
    raw_transcription: Optional[str]
    created_at: str
    updated_at: str


class NoteListItem(BaseModel):
    id: str
    notebook_id: str
    title: Optional[str]
    content_preview: str
    # Total character length of the raw note content (including markdown
    # syntax). The frontend uses this to estimate token counts for the
    # note-reference picker; showing only the preview length would
    # drastically under-count long notes.
    content_length: int = 0
    # Approximate token count (cl100k / tiktoken-style estimate):
    #   CJK characters   : ~1 token per character
    #   non-CJK characters: ~1 token per 4 characters
    # Computed server-side so every client renders a consistent number
    # without having to fetch the full note body.
    token_estimate: int = 0
    created_at: str
    updated_at: str


class QuickNoteCreate(BaseModel):
    transcription: str
    notebook_id: Optional[str] = None


class NotebookBulkDelete(BaseModel):
    notebook_ids: List[str]


class NoteBulkDelete(BaseModel):
    note_ids: List[str]


class BulkDeleteResponse(BaseModel):
    status: str
    deleted_count: int


class NoteMoveRequest(BaseModel):
    target_notebook_id: str


class NoteBulkMoveRequest(BaseModel):
    note_ids: List[str]
    target_notebook_id: str


class BulkMoveResponse(BaseModel):
    status: str
    moved_count: int


class NotebookBulkExport(BaseModel):
    notebook_ids: List[str]


class NoteBulkExport(BaseModel):
    note_ids: List[str]
    format: str = "md"  # "md" or "pdf"


class NoteSearchResult(BaseModel):
    note_id: str
    notebook_id: str
    notebook_name: str
    title: Optional[str]
    content_snippet: str
