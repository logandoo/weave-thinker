// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 皮肤注册表 —— 前端唯一皮肤目录。
 * 与后端目录 backend/app/api/skins.py 的 SKIN_CATALOG 保持 id 一致
 * （tests/api/test_skin_api.py 与 frontend/e2e/skin_system.spec.ts 双向断言）。
 *
 * 皮肤令牌契约与社区接入指南：docs/SKINS.md
 * - verdant-flat：默认，令牌源头为 main.css :root / [data-theme="dark"]
 * - ink-paper / mono-brutal：styles/themes/*.css 覆盖块
 */

export type SkinId = 'verdant-flat' | 'ink-paper' | 'mono-brutal'

export interface SkinMeta {
  id: SkinId | (string & {})
  name: string
  description: string
  isDefault: boolean
  /** builtin=内置注册表；uploaded=经 POST /api/skins/upload 上传的开发者皮肤 */
  source?: 'builtin' | 'uploaded'
  /** 皮肤选择卡片预览色（取该皮肤 light 模式值，仅作展示数据） */
  preview: {
    bg: string
    surface: string
    primary: string
    text: string
    accent: string
  }
}

export const DEFAULT_SKIN_ID: SkinId = 'verdant-flat'

export const SKIN_REGISTRY: SkinMeta[] = [
  {
    id: 'verdant-flat',
    name: '青野平面',
    description: '苔绿画布上的扁平自然系',
    isDefault: true,
    source: 'builtin',
    preview: { bg: '#f3f6f0', surface: '#ffffff', primary: '#7AA35A', text: '#2d3b24', accent: '#4E9A8F' },
  },
  {
    id: 'ink-paper',
    name: '墨韵纸间',
    description: '宣纸暖底、朱砂点墨的文房气质',
    isDefault: false,
    source: 'builtin',
    preview: { bg: '#f5efe3', surface: '#fbf7ec', primary: '#b23b2e', text: '#2a2521', accent: '#c9a86a' },
  },
  {
    id: 'mono-brutal',
    name: '黑白构成',
    description: '高对比黑白构成，橙色锐利点缀',
    isDefault: false,
    source: 'builtin',
    preview: { bg: '#F5F4EC', surface: '#ffffff', primary: '#D64008', text: '#111111', accent: '#2340B8' },
  },
]

/** 从上传皮肤 CSS 文本解析卡片预览色（取 light 锚点块内前五个语义令牌，缺失用中性灰兜底）。 */
export function previewFromCss(css: string): SkinMeta['preview'] {
  const fallback = (v: string) => v
  const pick = (key: string, def: string) => {
    const block = css.split('[data-theme="dark"]')[0]
    const re = new RegExp(`--${key}\\s*:\\s*([^;]+)`)
    const m = block.match(re)
    return m ? m[1].trim() : def
  }
  return {
    bg: pick('color-bg', '#f3f6f0'),
    surface: pick('surface-panel-strong', pick('color-white', '#ffffff')),
    primary: pick('color-primary', '#7AA35A'),
    text: pick('color-text', '#2d3b24'),
    accent: pick('color-accent', pick('color-info', '#4E9A8F')),
  }
}

const SKIN_IDS = new Set<string>(SKIN_REGISTRY.map(s => s.id))

export function isValidSkinId(value: unknown): value is SkinId {
  return typeof value === 'string' && SKIN_IDS.has(value)
}
