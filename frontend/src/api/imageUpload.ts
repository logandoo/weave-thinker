// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import apiClient from './client'

const createdObjectUrls = new Set<string>()

export interface ImageUploadResult {
  path: string
  filename: string
  size: number
}

export async function uploadImage(file: File): Promise<ImageUploadResult> {
  const formData = new FormData()
  formData.append('file', file)
  const res = await apiClient.post('/images/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return res.data
}

export async function uploadMedia(file: File): Promise<ImageUploadResult> {
  const formData = new FormData()
  formData.append('file', file)
  const res = await apiClient.post('/images/upload-media', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
  return res.data
}

function trackObjectUrl(url: string): string {
  createdObjectUrls.add(url)
  return url
}

export function revokeImageObjectUrl(url: string) {
  if (createdObjectUrls.has(url)) {
    URL.revokeObjectURL(url)
    createdObjectUrls.delete(url)
  }
}

export async function resolveImageUrl(path: string): Promise<string> {
  if (path.startsWith('blob:') || path.startsWith('data:')) return path
  if (path.startsWith('http://') || path.startsWith('https://')) return path
  try {
    if (path.startsWith('/api/files/download')) {
      const res = await apiClient.get(path, { responseType: 'blob' })
      return trackObjectUrl(URL.createObjectURL(res.data))
    }
    const res = await apiClient.get('/images/serve', {
      params: { path },
      responseType: 'blob',
    })
    return trackObjectUrl(URL.createObjectURL(res.data))
  } catch {
    return path
  }
}
