// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Workspace file API helpers (2026-09-19).
 *
 * - `fetchDirectory` lists one folder (folder manager dialog).
 * - URL builders append the JWT as `?token=` so the browser can fetch/open
 *   zip and converted-PDF responses without custom headers (same pattern as
 *   the existing download cards).
 */
import api from './client'

export interface WorkspaceEntry {
  name: string
  rel_path: string
  is_dir: boolean
  size: number
  mtime: number
  type: string
}

export interface DirectoryListing {
  path: string
  entries: WorkspaceEntry[]
  truncated: boolean
}

function tokenParam(): string {
  const token = localStorage.getItem('chatllm_token')
  return token ? `&token=${encodeURIComponent(token)}` : ''
}

export function buildDownloadUrl(target: string): string {
  return `/api/files/download?path=${encodeURIComponent(target)}${tokenParam()}`
}

export function buildZipUrl(relPath: string): string {
  return `/api/files/zip?path=${encodeURIComponent(relPath)}${tokenParam()}`
}

export function buildOfficePdfUrl(relPath: string): string {
  return `/api/files/office-pdf?path=${encodeURIComponent(relPath)}${tokenParam()}`
}

export async function fetchDirectory(
  relPath: string,
  signal?: AbortSignal,
): Promise<DirectoryListing> {
  const { data } = await api.get<DirectoryListing>('/files/list', {
    params: { path: relPath },
    signal,
  })
  return data
}

export function baseName(relPath: string): string {
  const seg = (relPath || '').split('/').filter(Boolean)
  return seg.length ? seg[seg.length - 1] : relPath || ''
}

export function formatMtime(seconds: number): string {
  if (!seconds) return ''
  const d = new Date(seconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
