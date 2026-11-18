// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 下载命名 / Blob MIME 派生（2026-11-15）。
 *
 * 背景（60 生产 conv a544c7c0 实测）：放大预览里点下载，得到
 * `江南水乡水巷，两岸粉墙黛瓦，河中小船，红灯笼点缀.txt` —— 字节其实是 JPEG。
 * 两个独立缺陷叠加：
 *   ① 文件名取 `<img alt>`（一句描述，没有后缀）；
 *   ② `new Blob(chunks)` 不带 MIME，Chrome 对「无后缀 + 无 type」按 text/plain
 *      推断后缀，于是追加 `.txt`。
 *
 * 契约：
 *   - 后缀**只从 URL 的真实文件名取**（`?path=` 的 basename，即服务器上的文件名；
 *     无该参数时退回 URL 自身 pathname），**绝不从 alt 猜**；
 *   - alt 只用作人类可读基名（保留原有 UX），非法文件名字符替换为 `_`；
 *   - 无可信后缀时不编造后缀 —— 此时由 Blob 的真实 MIME 让浏览器补对后缀。
 *
 * 安全边界（2026-11-15 评审）：`searchParams.get()` **已解码一次**，不得再解一次
 * （否则模型写的 `media/x%2Eexe` 会变成 `.exe` 后缀）；alt 与 path 都由模型控制，
 * 故须挡掉路径分隔符、控制字符、双向控制符、Windows 保留设备名与代理对切裂。
 *
 * 纯函数、无 Vue / DOM 依赖，可被 node 单测直接 import（同 `utils/deviceUrls`）。
 */

/** 相对 URL 的解析基底：固定常量，浏览器与 node 单测行为一致（不读 window）。 */
const URL_BASE = 'http://localhost'

/** 文件名尾部后缀：1-8 位字母数字。 */
const EXT_RE = /\.([A-Za-z0-9]{1,8})$/

/**
 * 非法文件名字符：路径分隔符 / Windows 保留字符 / 控制字符 /
 * 双向控制符（U+202A–U+202E、U+2066–U+2069 —— 经典的「扩展名反转」伪装）。
 */
const ILLEGAL_RE = /[\\/:*?"<>|\u0000-\u001f\u202A-\u202E\u2066-\u2069]/g

/** Windows 保留设备名：带任何后缀都会被系统当设备（保存失败或数据丢进 NUL）。 */
const RESERVED_RE = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i

/**
 * 已知媒体/文档后缀：只有这些才从 alt 尾部剥掉再补真实后缀，
 * 避免把 `v1.2` 这类普通点号误当后缀。
 */
const KNOWN_EXT = new Set([
  'jpg', 'jpeg', 'jfif', 'png', 'gif', 'webp', 'bmp', 'svg', 'avif', 'heic', 'heif', 'tif', 'tiff', 'ico',
  'mp4', 'webm', 'mov', 'm4v', 'avi', 'mkv',
  'mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac',
  'pdf', 'txt', 'md', 'csv', 'json', 'zip',
])

/** 基名长度上限（按码点计，后缀另计）。 */
const MAX_BASE = 120

const FALLBACK_MIME = 'application/octet-stream'

/** 把任意文本变成可安全落盘的文件名基名（不含后缀）。 */
export function sanitizeBaseName(raw: string): string {
  let out = (raw || '')
    .replace(ILLEGAL_RE, '_')
    .replace(/\s+/g, ' ')
    .trim()
  // 按码点截断：`slice()` 数的是 UTF-16 码元，会切裂 emoji 代理对。
  const codePoints = Array.from(out)
  if (codePoints.length > MAX_BASE) out = codePoints.slice(0, MAX_BASE).join('')
  out = out.replace(/[.\s]+$/, '')
  if (RESERVED_RE.test(out)) out = `_${out}`
  return out
}

/** 取文件名尾部后缀（含点，保留原大小写）；无后缀返回 ''。 */
export function extensionOf(name: string): string {
  const m = EXT_RE.exec(name || '')
  return m ? m[0] : ''
}

/**
 * 真实文件名里的后缀是否可信。
 *
 * 防 `chart.v1.2` → `.2`、`IMG.20241115` → `.20241115` 这类「版本号/日期被当后缀」，
 * 它们会让用户拿到一个打不开的文件（正是本 bug 的同类症状）。
 */
function plausibleExtensionOf(name: string): string {
  const ext = extensionOf(name)
  if (!ext) return ''
  const body = ext.slice(1)
  if (KNOWN_EXT.has(body.toLowerCase())) return ext
  return body.length >= 2 && !/^\d+$/.test(body) ? ext : ''
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

/** URL 指向的真实文件名：优先 `?path=` 的 basename，其次 URL 自身 pathname。 */
function realFileName(src: string): string {
  if (!src) return ''
  try {
    const u = new URL(src, URL_BASE)
    // searchParams.get() 已解码一次（再解一次会把 %2E 变成 '.'，模型可借此伪造后缀）；
    // pathname 仍是百分号编码，需要自己解一次。
    const candidate = u.searchParams.get('path') || safeDecode(u.pathname)
    const segments = candidate.split('/').filter(Boolean)
    return segments.length ? segments[segments.length - 1] : ''
  } catch {
    return ''
  }
}

/** alt 尾部若是一个已知后缀（"photo.png"），剥掉它 —— 后缀只认真实文件名。 */
function stripKnownExtension(base: string): string {
  const tail = extensionOf(base)
  if (tail && KNOWN_EXT.has(tail.slice(1).toLowerCase())) {
    return sanitizeBaseName(base.slice(0, -tail.length))
  }
  return base
}

/**
 * 下载文件名：`<alt 或真实基名>.<真实后缀>`。
 *
 * @param src 图片/媒体的 src（signed `/api/files/download?path=...` 或任意 URL）
 * @param alt `<img alt>`（人类可读描述；可为空）
 * @param fallbackBase 两者都取不到时的兜底基名
 */
export function downloadNameFromSrc(src: string, alt?: string | null, fallbackBase = '图片'): string {
  const real = realFileName(src)
  const ext = plausibleExtensionOf(real)
  const fallback = sanitizeBaseName(fallbackBase) || '图片'

  let base = sanitizeBaseName(alt || '')
  if (!base) base = sanitizeBaseName(ext ? real.slice(0, -ext.length) : real)
  if (!base) base = fallback

  if (!ext) {
    // 没有可信的真实后缀：alt 自带的已知后缀也剥掉，交给 Blob MIME 决定。
    return stripKnownExtension(base) || fallback
  }
  // alt 已经带同一个后缀（"photo.jpg"）→ 原样保留，不叠加。
  if (base.toLowerCase().endsWith(ext.toLowerCase())) return base
  // alt 自带别的已知后缀（"photo.png" 但真实是 .jpg）→ 剥掉，换成真实后缀。
  return `${stripKnownExtension(base) || fallback}${ext}`
}

/**
 * 响应 `content-type` → Blob type。
 *
 * 关键：`new Blob(chunks)` 不带 type 时，Chrome 会把「无后缀文件名」存成 `.txt`；
 * 带上真实 type 后即使名字没有后缀，浏览器也能补出正确后缀。
 *
 * 注：type 是全局生效的（所有 `downloadUrl` 调用方都拿服务器 content-type）；
 * 对 `.html`/`.svg` 这类可渲染类型，`a.download` 仍强制下载，不会导航。
 */
export function mimeFromContentType(value: string | null | undefined): string {
  const base = (value || '').split(';')[0].trim().toLowerCase()
  return /^[a-z0-9.+-]+\/[a-z0-9.+-]+$/.test(base) ? base : FALLBACK_MIME
}
