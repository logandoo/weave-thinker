// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Android WebView 壳的系统栏颜色解析（2026-11-07）。
 *
 * 背景：桥旧契约只传明暗布尔，原生硬编码两套色，与 3 皮肤 × 明暗 的页面
 * 背景永远对不上。现契约：前端按当前生效的 `--app-background`（页面真实
 * 背景，main.css `background: var(--app-background)`）解析出
 * 状态栏色（顶部边缘）与导航栏色（底部底色），经 `onThemeColors(status, nav)`
 * 下发小写 `#rrggbb`。规则（与 docs/PLAN_20261107_system_bars.md Consistency
 * Hub 一致）：
 * - 导航栏 = 最后一个顶层背景层的最后一个色字面量（多层背景的底层实色）；
 * - 状态栏 = 顶层若为 linear-gradient 取该层首个色字面量，否则同底色；
 * - 缺失回退链：--color-bg → 明暗默认（light #ffffff / dark #121212）。
 */

const HEX_RE = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i
const RGB_RE = /^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(\d*\.?\d+)\s*)?\)$/i
const LITERAL_RE =
  /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*\d*\.?\d+\s*)?\)/g

function toHex2(n: number): string {
  return Math.max(0, Math.min(255, n)).toString(16).padStart(2, '0')
}

/** 任意 CSS 色值 → 小写 `#rrggbb`；完全透明/非法/空 → null。rgba 的 alpha>0 时按 RGB 通道取值。 */
export function normalizeColor(raw: string | null | undefined): string | null {
  const s = (raw ?? '').trim()
  if (!s) return null
  if (HEX_RE.test(s)) {
    const hex = s.slice(1).toLowerCase()
    return hex.length === 3 ? '#' + hex.split('').map(c => c + c).join('') : '#' + hex
  }
  const m = RGB_RE.exec(s)
  if (m) {
    if (m[4] !== undefined && parseFloat(m[4]) === 0) return null
    return '#' + toHex2(Number(m[1])) + toHex2(Number(m[2])) + toHex2(Number(m[3]))
  }
  return null
}

/** 计算 `elementsFromPoint` 采样链的合色（wave-2 透明边到边）：
 * 自顶向下叠色，遇到不透明层停止；全程无层可叠且无 pageBase → null（调用方走回退解析）。
 * 不可解析项（关键字/var()）按透明跳过。 */
export function compositeSurfaceStack(layers: string[], pageBase: string | null): string | null {
  // 自顶向下累积「上方各层」的预乘合成 (acc=非预乘色, accA=总不透明度)。
  // 加入更下一层 c：new = acc over c。
  let acc: { r: number; g: number; b: number } | null = null
  let accA = 0
  for (const raw of layers) {
    const c = parseRgba(raw)
    if (!c || c.a === 0) continue
    if (!acc) {
      acc = { r: c.r, g: c.g, b: c.b }
      accA = c.a
    } else {
      const newA = accA + c.a * (1 - accA)
      acc = {
        r: (acc.r * accA + c.r * c.a * (1 - accA)) / newA,
        g: (acc.g * accA + c.g * c.a * (1 - accA)) / newA,
        b: (acc.b * accA + c.b * c.a * (1 - accA)) / newA,
      }
      accA = newA
    }
    if (accA >= 1) break // 已不透明：其下图层被遮挡，不再叠 pageBase
  }
  if (!acc) return null
  if (accA < 1) {
    const base = parseRgba(pageBase ?? '')
    if (base) {
      acc = { r: acc.r * accA + base.r * (1 - accA), g: acc.g * accA + base.g * (1 - accA), b: acc.b * accA + base.b * (1 - accA) }
    }
  }
  return '#' + toHex2(Math.round(acc.r)) + toHex2(Math.round(acc.g)) + toHex2(Math.round(acc.b))
}

/** CSS 颜色 → {r,g,b,a}；hex 展开、rgb()/rgba() 解析；失败 null。 */
export function parseRgba(raw: string | null | undefined): { r: number; g: number; b: number; a: number } | null {
  const s = (raw ?? '').trim()
  if (!s) return null
  if (HEX_RE.test(s)) {
    const hex = s.slice(1).toLowerCase()
    const full = hex.length === 3 ? hex.split('').map(c => c + c).join('') : hex
    return {
      r: parseInt(full.slice(0, 2), 16),
      g: parseInt(full.slice(2, 4), 16),
      b: parseInt(full.slice(4, 6), 16),
      a: 1,
    }
  }
  const m = RGB_RE.exec(s)
  if (m) {
    const a = m[4] !== undefined ? Math.max(0, Math.min(1, parseFloat(m[4]))) : 1
    return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]), a }
  }
  return null
}

/** 按出现顺序提取值中的色字面量（#hex / rgb() / rgba()；`transparent` 等关键字不算）。 */
export function colorLiterals(value: string | null | undefined): string[] {
  return ((value ?? '').match(LITERAL_RE) ?? []) as string[]
}

/** 按顶层逗号拆分多层背景（括号内的逗号不拆）。 */
export function splitTopLevelLayers(value: string | null | undefined): string[] {
  const s = value ?? ''
  const layers: string[] = []
  let depth = 0
  let start = 0
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (c === '(') depth++
    else if (c === ')') depth--
    else if (c === ',' && depth === 0) {
      layers.push(s.slice(start, i).trim())
      start = i + 1
    }
  }
  layers.push(s.slice(start).trim())
  return layers.filter(l => l !== '')
}

/** 计算状态栏/导航栏颜色（小写 `#rrggbb`）。入参为 html 计算样式中的 --app-background / --color-bg 原值。 */
export function resolveSystemBarColors(
  appBackground: string | null | undefined,
  colorBg: string | null | undefined,
  mode: 'light' | 'dark'
): { status: string; nav: string } {
  const layers = splitTopLevelLayers(appBackground)
  let base: string | null = null
  if (layers.length > 0) {
    const lastLiterals = colorLiterals(layers[layers.length - 1])
    base = normalizeColor(lastLiterals[lastLiterals.length - 1])
  }
  if (!base) base = normalizeColor(colorBg)
  if (!base) base = mode === 'dark' ? '#121212' : '#ffffff'

  let status = base
  const firstLayer = layers[0] ?? ''
  if (/^linear-gradient\(/i.test(firstLayer)) {
    status = normalizeColor(colorLiterals(firstLayer)[0]) ?? base
  }
  return { status, nav: base }
}
