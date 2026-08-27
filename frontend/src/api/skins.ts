// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

export interface SkinCatalogEntry {
  id: string
  name: string
  description: string
  is_default: boolean
  modes: string[]
}

export interface UploadedSkinEntry extends SkinCatalogEntry {
  source: 'uploaded'
  size: number
  sha256: string
  uploaded_at: string
}

export interface SkinCatalogResponse {
  token_contract_version: string
  default_skin: string
  skins: SkinCatalogEntry[]
}

export async function fetchSkinCatalog(): Promise<SkinCatalogResponse> {
  const response = await api.get<SkinCatalogResponse>('/skins')
  return response.data
}

export async function getSkinPreference(): Promise<{ skin_id: string }> {
  const response = await api.get<{ skin_id: string }>('/users/me/preferences')
  return response.data
}

export async function saveSkinPreference(skinId: string): Promise<{ skin_id: string }> {
  const response = await api.put<{ skin_id: string }>('/users/me/preferences', { skin_id: skinId })
  return response.data
}

export async function uploadSkin(
  file: File,
  name?: string,
  description?: string,
): Promise<UploadedSkinEntry> {
  const form = new FormData()
  form.append('file', file)
  if (name) form.append('name', name)
  if (description) form.append('description', description)
  const response = await api.post<UploadedSkinEntry>('/skins/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return response.data
}

export async function listMySkins(): Promise<{ skins: UploadedSkinEntry[] }> {
  const response = await api.get<{ skins: UploadedSkinEntry[] }>('/skins/mine')
  return response.data
}

export async function fetchMySkinCss(skinId: string): Promise<string> {
  const response = await api.get<string>(`/skins/${encodeURIComponent(skinId)}/css`)
  return response.data as unknown as string
}

export async function deleteMySkin(skinId: string): Promise<{ deleted: string }> {
  const response = await api.delete<{ deleted: string }>(`/skins/${encodeURIComponent(skinId)}`)
  return response.data
}
