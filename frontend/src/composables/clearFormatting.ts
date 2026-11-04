// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 「清除格式」规则真源（遗留 #1 方案 A）。
 *
 * 语义（与 TinyMCE `formats.removeformat` 同构，但按本编辑器的数据载体做白名单）：
 * - **清除**：纯视觉行内标签（粗体/斜体/下划线/删除线/高亮/字体/上下标/span…）
 *   与字体类 `style` 属性（字体栈/字号/字距/大小写/颜色/底色/行高/字重/字形/装饰）；
 * - **保留**：数据载体（引文角标 `.citation-ref`、尾注 `.note-endnote-*`、公式
 *   `.math-editable`/`.katex`、图表 `.mermaid-block`/`.echarts-block`）与语义结构
 *   （链接 `a`、代码 `code`/`pre`、代码块 `.code-block`、图片/音视频/iframe/表格/分隔线）。
 *   引文角标被剥掉会永久毁掉 `[N]` 台账（评审 B 结论），因此必须在清除前就排除。
 * - **范围**：有选区 → 只清选区；折叠光标 → 整篇（由调用方弹确认框）。
 *
 * 纯函数，无 DOM 依赖 —— 逻辑先测后写；DOM 手术在 `WysiwygEditor.clearFormatting`。
 */

/** 可整段解包的纯视觉行内标签（大写比较）。 */
const CLEARABLE_INLINE_TAGS = new Set([
  'B', 'STRONG', 'I', 'EM', 'U', 'S', 'STRIKE', 'DEL', 'MARK', 'SMALL', 'BIG', 'FONT', 'SUP', 'SUB', 'SPAN', 'CODE',
])

/** 必须整体保留的语义/载荷标签（大写）。 */
export const PROTECTED_TAGS = [
  'A', 'IMG', 'VIDEO', 'AUDIO', 'IFRAME', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TD', 'TH', 'HR', 'BR', 'MATH',
]

/**
 * 块级「样式」标签：清除格式时归为普通段落（用户口径，2026-10-25）：
 * 标题 h1-h6 / 引用 blockquote / 代码 pre（含 `.code-block` 包裹）。
 * **保留**：列表（ul/ol/li）、表格、图片/音视频/分隔线（与 Google Docs 一致）。
 */
export const BLOCK_RESET_TAGS = ['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE']

/** 该标签是否应在清除格式时降级为普通段落。 */
export function isBlockResetTag(tagName: string): boolean {
  return BLOCK_RESET_TAGS.includes(String(tagName || '').toUpperCase())
}

/** 必须整体保留的复合元素（本编辑器的数据载体与结构块）。 */
export const PROTECTED_SELECTOR = [
  '.citation-ref',
  '.note-endnote-ref',
  '.note-endnote-def',
  '.note-endnote-back',
  '.math-editable',
  '.katex',
  '.mermaid-block',
  '.echarts-block',
  '.code-block',
].join(', ')

/** 字体/颜色/装饰类 style 属性 —— 清除格式要剥掉它们。 */
const STRIPPED_STYLE_PROPS = new Set([
  'font-family',
  'font-size',
  'font-weight',
  'font-style',
  'letter-spacing',
  'word-spacing',
  'text-transform',
  'text-decoration',
  'text-decoration-line',
  'text-decoration-color',
  'color',
  'background',
  'background-color',
  'line-height',
  'vertical-align',
  'border-radius',
])

/** `unwrapRangeInTag` 用的标签选择器（小写，逗号分隔）。 */
export const CLEARABLE_INLINE_SELECTOR = 'b, strong, i, em, u, s, strike, del, mark, small, big, font, sup, sub, span, code'

/**
 * 载荷/布局标签：结构保留，且**不剥其 style**（其样式是布局而非字体垃圾：
 * img 的宽高、iframe 的尺寸、hr 的边框）。
 */
export const LAYOUT_TAGS = ['IMG', 'VIDEO', 'AUDIO', 'IFRAME', 'HR', 'BR', 'MATH']

export type ClearScope = 'selection' | 'document'

/** 折叠光标触发 = 整篇（需确认）；有选区 = 只清选区。 */
export function resolveClearScope(collapsed: boolean): ClearScope {
  return collapsed ? 'document' : 'selection'
}

/** 该行内标签是否应被解包（保留文字）。 */
export function isClearableInlineTag(tagName: string): boolean {
  return CLEARABLE_INLINE_TAGS.has(String(tagName || '').toUpperCase())
}

/** 该 style 属性是否属于字体类脏格式。 */
export function isStrippedStyleProperty(prop: string): boolean {
  return STRIPPED_STYLE_PROPS.has(String(prop || '').toLowerCase())
}

/** 该标签是否属于必须整体保留的语义/载荷元素。 */
export function isProtectedTag(tagName: string): boolean {
  return PROTECTED_TAGS.includes(String(tagName || '').toUpperCase())
}

/** 该标签的 style 是否属于「布局」而非字体垃圾（剥字体样式时跳过）。 */
export function isLayoutTag(tagName: string): boolean {
  return LAYOUT_TAGS.includes(String(tagName || '').toUpperCase())
}
