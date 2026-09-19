// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * File preview classification — single source of truth (2026-09-19).
 *
 * Shared by FileAttachment / FilePreviewDialog / FolderBrowserDialog /
 * MessageBubble / ChatInput so a file's kind is decided in one place.
 * `classifyFile` accepts the backend-provided `type` first (authoritative for
 * folder entries and normalized labels), then falls back to the extension.
 */

export type FileKind =
  | 'image' | 'video' | 'audio' | 'pdf'
  | 'word' | 'excel' | 'ppt'
  | 'markdown' | 'code' | 'text'
  | 'archive' | 'folder' | 'unknown'

const EXT_KIND: Record<string, FileKind> = {
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image',
  bmp: 'image', svg: 'image', ico: 'image', avif: 'image',
  mp4: 'video', webm: 'video', mov: 'video', m4v: 'video', avi: 'video', mkv: 'video',
  mp3: 'audio', wav: 'audio', m4a: 'audio', ogg: 'audio', flac: 'audio', aac: 'audio',
  pdf: 'pdf',
  doc: 'word', docx: 'word', odt: 'word', rtf: 'word',
  xls: 'excel', xlsx: 'excel', csv: 'excel', ods: 'excel',
  ppt: 'ppt', pptx: 'ppt', odp: 'ppt',
  md: 'markdown', markdown: 'markdown',
  py: 'code', js: 'code', ts: 'code', tsx: 'code', jsx: 'code', json: 'code',
  sh: 'code', bash: 'code', yaml: 'code', yml: 'code', toml: 'code', ini: 'code',
  html: 'code', htm: 'code', css: 'code', scss: 'code', sql: 'code',
  go: 'code', rs: 'code', java: 'code', c: 'code', cpp: 'code', h: 'code',
  xml: 'code', vue: 'code', log: 'text',
  txt: 'text',
  zip: 'archive', gz: 'archive', tar: 'archive', rar: 'archive', '7z': 'archive',
}

const BACKEND_TYPE_KIND: Record<string, FileKind> = {
  folder: 'folder',
  image: 'image',
  video: 'video',
  audio: 'audio',
  pdf: 'pdf',
  word: 'word',
  excel: 'excel',
  ppt: 'ppt',
  markdown: 'markdown',
  python: 'code',
  javascript: 'code',
  json: 'code',
  html: 'code',
  css: 'code',
  text: 'text',
  archive: 'archive',
}

const KIND_LABEL: Record<FileKind, string> = {
  image: '图片', video: '视频', audio: '音频', pdf: 'PDF',
  word: 'Word', excel: 'Excel', ppt: 'PPT',
  markdown: 'Markdown', code: '代码', text: '文本',
  archive: '压缩包', folder: '文件夹', unknown: '文件',
}

const KIND_ICON: Record<FileKind, string> = {
  image: '🖼️', video: '🎬', audio: '🎵', pdf: '📄',
  word: '📝', excel: '📊', ppt: '📽️',
  markdown: '📃', code: '⌨️', text: '📃',
  archive: '📦', folder: '📁', unknown: '📎',
}

export function extensionOf(name: string): string {
  const base = (name || '').split(/[?#]/)[0]
  const idx = base.lastIndexOf('.')
  if (idx === -1 || idx === base.length - 1) return ''
  return base.slice(idx + 1).toLowerCase()
}

export function classifyFile(name: string, explicitType?: string | null): FileKind {
  const explicit = (explicitType || '').trim().toLowerCase()
  if (explicit && explicit !== 'file' && BACKEND_TYPE_KIND[explicit]) {
    return BACKEND_TYPE_KIND[explicit]
  }
  const ext = extensionOf(name)
  return EXT_KIND[ext] || 'unknown'
}

export function isPreviewableKind(kind: FileKind): boolean {
  return !(kind === 'unknown' || kind === 'archive' || kind === 'folder')
}

export function fileTypeLabel(kind: string): string {
  return KIND_LABEL[kind as FileKind] || (kind || 'FILE').toUpperCase()
}

export function fileIcon(kind: string): string {
  return KIND_ICON[kind as FileKind] || '📎'
}

export function formatSize(bytes: number | undefined | null): string {
  const n = Number(bytes) || 0
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`
  return `${(n / 1073741824).toFixed(1)} GB`
}
