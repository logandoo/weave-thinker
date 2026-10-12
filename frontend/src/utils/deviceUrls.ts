// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * W18 G3/G4 — 设备执行 URL 派生（纯函数，无浏览器/axios 依赖——单测缝）。
 *
 * 契约：设备上下文 + workspace 相对路径 → 设备 fs 代理；绝对路径/盘符/无设备
 * 上下文 → 云端下载（原语义）。token 注入由调用方传入（localStorage 封装在
 * api/clientDevices.ts / api/workspaceFiles.ts）。
 */

export function deviceIdFromTarget(target: string | null | undefined): string | null {
  if (target && target.startsWith('device:')) {
    return target.slice('device:'.length)
  }
  return null
}

export function isRoutableToDevice(relPath: string): boolean {
  return Boolean(relPath) && !relPath.startsWith('/') && !/^[A-Za-z]:/.test(relPath)
}

export function buildDeviceDownloadUrl(deviceId: string, relPath: string, token?: string | null): string {
  const tokenParam = token ? `&token=${encodeURIComponent(token)}` : ''
  return `/api/devices/${encodeURIComponent(deviceId)}/fs/download?path=${encodeURIComponent(relPath)}${tokenParam}`
}

export function resolveDownloadUrl(
  target: string,
  deviceId: string | null | undefined,
  token: string | null,
): string {
  // A4.9 M4（契约统一）：设备上下文下一律走设备代理——绝对路径经代理显式 400
  // 降级，绝不回退云端读服务器工作区（错误环境读）。云端上下文维持原语义。
  if (deviceId) {
    return buildDeviceDownloadUrl(deviceId, target, token)
  }
  const tokenParam = token ? `&token=${encodeURIComponent(token)}` : ''
  return `/api/files/download?path=${encodeURIComponent(target)}${tokenParam}`
}

// ---- 设备 fs/list 响应归一（A4.9 I-1：目录枚举宽容化）----
export interface NormalizedEntry {
  name: string
  rel_path: string
  is_dir: boolean
  size: number
  mtime: number
  type: string
}

const DIR_TYPES = new Set(['dir', 'directory', 'folder', 'd', 'D', 'DIRECTORY', 'Folder'])

export function normalizeDeviceEntries(
  entries: Array<Record<string, unknown>> | undefined | null,
  parentPath: string,
): NormalizedEntry[] {
  return (entries || []).map((e) => {
    const name = String(e.name ?? '')
    const typeRaw = e.type != null ? String(e.type) : ''
    // 契约（PLAN §5）：type ∈ {file, dir}；宽容读常见目录别名 + is_dir 布尔
    const isDir = Boolean(e.is_dir) || DIR_TYPES.has(typeRaw)
    return {
      name,
      rel_path: String(e.rel_path ?? (parentPath ? `${parentPath}/${name}` : name)),
      is_dir: isDir,
      size: Number(e.size ?? 0),
      mtime: Number(e.mtime ?? 0),
      type: typeRaw || (isDir ? 'dir' : 'file'),
    }
  })
}
