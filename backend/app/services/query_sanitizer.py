# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import re as _re


_NOTE_REF_BLOCK_RE = _re.compile(r'\[note-ref:([^\]]*)\]\s*(.*?)\s*\[/note-ref\]', _re.DOTALL)
_MULTISPACE_RE = _re.compile(r'\s+')
_NOTE_CONTEXT_MARKUP_RE = _re.compile(r'\[(?:/?用户引用的笔记摘要|引用笔记摘要:[^\]]*)\]')
_USER_QUESTION_PREFIX_RE = _re.compile(r'^用户提问[:：]\s*')


def _collapse_whitespace(value: str) -> str:
    return _MULTISPACE_RE.sub(' ', value or '').strip()


def _build_note_context_entry(header: str, body: str, *, snippet_limit: int) -> str:
    note_header = (header or '').strip()
    note_title = note_header.split('|', 1)[1].strip() if '|' in note_header else note_header
    note_body = _collapse_whitespace(body)
    snippet = note_body[:snippet_limit].rstrip()

    if note_title and snippet:
        return f'《{note_title}》: {snippet}'
    if note_title:
        return f'《{note_title}》'
    return snippet


def sanitize_search_query(query_text: str) -> str:
    sanitized = _NOTE_REF_BLOCK_RE.sub(' ', query_text or '')
    sanitized = _NOTE_CONTEXT_MARKUP_RE.sub(' ', sanitized)
    sanitized = _USER_QUESTION_PREFIX_RE.sub('', sanitized.strip())
    return _collapse_whitespace(sanitized)


def extract_search_query_and_note_context(message_text: str, *, snippet_limit: int = 120) -> tuple[str, str]:
    note_context_entries: list[str] = []

    def _replace(match: _re.Match[str]) -> str:
        entry = _build_note_context_entry(match.group(1), match.group(2), snippet_limit=snippet_limit)
        if entry:
            note_context_entries.append(entry)
        return ' '

    search_query = sanitize_search_query(_NOTE_REF_BLOCK_RE.sub(_replace, message_text or ''))
    note_context = '\n'.join(note_context_entries)
    return search_query, note_context