// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

declare global {
  interface Window {
    WeaverNoteApp?: {
      onThemeChanged: (isDark: boolean) => void
      saveFile: (base64Data: string, filename: string, mimeType: string) => boolean
      isDownloadSupported: () => boolean
      downloadFile?: (url: string, filename: string) => boolean
      downloadFileWithProgress?: (url: string, filename: string, id: string) => boolean
    }
  }
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onloadend = () => {
      const result = reader.result as string
      const base64 = result.split(',')[1]
      resolve(base64)
    }
    reader.onerror = reject
    reader.readAsDataURL(blob)
  })
}

function isInWebView(): boolean {
  try {
    return typeof window.WeaverNoteApp !== 'undefined'
      && typeof window.WeaverNoteApp.isDownloadSupported === 'function'
      && window.WeaverNoteApp.isDownloadSupported()
  } catch {
    return false
  }
}

export interface DownloadProgress {
  loaded: number
  total: number
  percentage: number
}

export interface DownloadOptions {
  onProgress?: (progress: DownloadProgress) => void
  signal?: AbortSignal
}

export interface DownloadResult {
  success: boolean
  path?: string
  error?: string
}

const SMALL_FILE_THRESHOLD = 5 * 1024 * 1024 // 5MB

async function fetchWithAuth(url: string, signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = {}
  const token = localStorage.getItem('chatllm_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return fetch(url, { signal, headers })
}

async function streamDownload(url: string, filename: string, options: DownloadOptions = {}): Promise<DownloadResult> {
  const { onProgress, signal } = options
  try {
    const response = await fetchWithAuth(url, signal)
    if (!response.ok) {
      return { success: false, error: `下载失败: ${response.status}` }
    }
    const total = parseInt(response.headers.get('content-length') || '0', 10)
    const reader = response.body?.getReader()
    if (!reader) {
      return { success: false, error: '无法读取响应流' }
    }

    const chunks: Uint8Array[] = []
    let loaded = 0
    while (true) {
      if (signal?.aborted) {
        reader.cancel()
        return { success: false, error: '下载已取消' }
      }
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
      loaded += value.length
      if (onProgress && total > 0) {
        onProgress({ loaded, total, percentage: Math.round((loaded / total) * 100) })
      }
      // Memory guard: if accumulated chunks exceed threshold, abort in-memory accumulation
      if (loaded > 100 * 1024 * 1024) {
        reader.cancel()
        return { success: false, error: '文件过大，请使用原生下载' }
      }
    }

    const blob = new Blob(chunks)
    const objectUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(objectUrl)
    return { success: true }
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      return { success: false, error: '下载已取消' }
    }
    return { success: false, error: String(e) }
  }
}

// WebView 内下载：前端 fetch（WebView 网络栈信任自签名证书 + Bearer 认证），
// 再经 base64 → saveFile 写入系统下载目录。
// 不能把 HTTP URL 交给 DownloadManager：它不信任自签名证书，且相对 URL
// （/api/files/download?path=...）会被 Uri.parse 解析出空 scheme 而静默失败。
// base64 跨桥传输内存放大 ~3 倍，超大文件会 OOM/ANR —— 设上限并给出明确提示。
const WEBVIEW_MAX_FILE_SIZE = 50 * 1024 * 1024 // 50MB

async function streamToBlobAndSave(url: string, filename: string, options: DownloadOptions = {}): Promise<DownloadResult> {
  const { signal } = options
  try {
    const response = await fetchWithAuth(url, signal)
    if (!response.ok) {
      return { success: false, error: `下载失败: ${response.status}` }
    }
    const blob = await response.blob()
    if (blob.size > WEBVIEW_MAX_FILE_SIZE) {
      return { success: false, error: '文件过大（超过 50MB），无法在手机端保存' }
    }
    return await downloadBlob(blob, filename, blob.type || 'application/octet-stream', options)
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      return { success: false, error: '下载已取消' }
    }
    return { success: false, error: String(e) }
  }
}

export async function downloadBlob(blob: Blob, filename: string, mimeType: string, options: DownloadOptions = {}): Promise<DownloadResult> {
  if (isInWebView()) {
    if (!window.WeaverNoteApp?.saveFile) {
      return { success: false, error: 'Native saveFile bridge unavailable' }
    }
    try {
      const base64 = await blobToBase64(blob)
      const result = window.WeaverNoteApp.saveFile(base64, filename, mimeType)
      if (result) return { success: true, path: `Downloads/${filename}` }
      return { success: false, error: '保存文件失败' }
    } catch (e) {
      console.error('WeaverNoteApp.saveFile failed:', e)
      return { success: false, error: String(e) }
    }
  }

  // Browser fallback
  if (blob.size <= SMALL_FILE_THRESHOLD) {
    try {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      return { success: true }
    } catch (e) {
      return { success: false, error: String(e) }
    }
  }
  return { success: false, error: '文件过大，请通过文件详情页下载' }
}

export async function downloadUrl(url: string, filename: string, options: DownloadOptions = {}): Promise<DownloadResult> {
  if (isInWebView()) {
    return streamToBlobAndSave(url, filename, options)
  }
  return streamDownload(url, filename, options)
}
