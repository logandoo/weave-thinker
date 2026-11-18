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

/**
 * 工作区文件链接判定（2026-11-15 波 · S4）。
 *
 * Markdown 链接 `[下载](artifacts/x.pdf)` 原本原样输出 `<a href>`，浏览器按
 * **站点**相对解析 ⇒ `/app/frontend/artifacts/x.pdf` ⇒ 404。这里给出保守判据：
 * 只认「非 URL scheme + 扩展名在**已知文件类型白名单**内」的路径。
 *
 * 为什么用白名单而不是「任意 `.<1-8 alnum>`」（评审 Minor）：后者会把
 * `docs/v1.2`、`notes/2024.11.15` 这类版本号/日期路径也改写成下载 URL。
 * 未列入的扩展名**保持原样**（= 与改造前一致），宁可不解耦也不错改。
 */
const WORKSPACE_FILE_LINK_EXTS = new Set([
  // 文档
  'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp',
  'rtf', 'epub', 'csv', 'txt', 'md', 'rst', 'json', 'xml', 'yaml', 'yml',
  'toml', 'ini', 'log', 'html', 'htm', 'ipynb',
  // 图片
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'tif', 'tiff', 'heic',
  // 音视频
  'mp3', 'wav', 'm4a', 'ogg', 'flac', 'aac', 'mp4', 'webm', 'mov', 'm4v', 'avi', 'mkv',
  // 压缩包 / 数据
  'zip', 'tar', 'gz', 'tgz', '7z', 'rar', 'parquet', 'sqlite', 'db',
  // 代码
  'py', 'js', 'ts', 'tsx', 'jsx', 'sh', 'sql', 'css', 'java', 'go', 'rs', 'c', 'cpp', 'h',
])

export function isWorkspaceFileLink(href: string): boolean {
  const raw = (href || '').trim()
  if (!raw) return false
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(raw)) return false // http:, data:, mailto:, tel:, blob:
  if (raw.startsWith('//') || raw.startsWith('#')) return false
  // **站点绝对路径不碰**（2026-11-15 自查）：`/app/frontend/x.html`、`/static/a.pdf`
  // 这类链接本来就能用，改写成下载 URL 反而会 404。代价是「legacy 工作区内绝对路径
  // 链接」继续不解析——它们在改造前也是 404，不是回归。
  if (raw.startsWith('/')) return false
  const withoutQuery = raw.split(/[?#]/)[0] || ''
  const dot = withoutQuery.lastIndexOf('.')
  if (dot <= 0 || dot === withoutQuery.length - 1) return false
  const ext = withoutQuery.slice(dot + 1).toLowerCase()
  return WORKSPACE_FILE_LINK_EXTS.has(ext)
}

export function buildDeviceDownloadUrl(deviceId: string, relPath: string, token?: string | null): string {
  const tokenParam = token ? `&token=${encodeURIComponent(token)}` : ''
  return `/api/devices/${encodeURIComponent(deviceId)}/fs/download?path=${encodeURIComponent(relPath)}${tokenParam}`
}

export function resolveDownloadUrl(
  target: string,
  deviceId: string | null | undefined,
  token: string | null,
  conversationId?: string | null,
): string {
  // A4.9 M4（契约统一）：设备上下文下一律走设备代理——绝对路径经代理显式 400
  // 降级，绝不回退云端读服务器工作区（错误环境读）。云端上下文维持原语义。
  if (deviceId) {
    return buildDeviceDownloadUrl(deviceId, target, token)
  }
  const tokenParam = token ? `&token=${encodeURIComponent(token)}` : ''
  // 2026-11-15：会话作用域兜底——相对路径在用户根解析失败时，服务端回落到
  // 该会话绑定 Task 的 workspace 根（Task 内相对路径 `artifacts/x.png`）。
  const convParam = conversationId
    ? `&conversation_id=${encodeURIComponent(conversationId)}`
    : ''
  return `/api/files/download?path=${encodeURIComponent(target)}${tokenParam}${convParam}`
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
