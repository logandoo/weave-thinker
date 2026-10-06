// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 标题排版归一化（笔记/工作台共用纯逻辑）。
 *
 * 背景：Chromium 在标题内 `insertParagraph`（Enter 切块）后接
 * `deleteContentBackward`（Backspace 合块）时，会按
 * `HandleStyleSpansBeforeInsertion` / `FollowBlockElementStyle` 的语义把合并点
 * 右侧内容包进 `<span style="font-size:…">`，以"保留"它在被合块里的计算样式。
 * 该值若是 em，落回标题内会与标题自身的 em 相乘 —— 焦点右侧文字被放大
 * （WPT：merge-span-with-style-after-pressing-enter-followed-by-backspace-in-contenteditable-div）。
 *
 * 本模块只做三件纯事：判定何时归一化、判定归一化哪些块、剥离排版内联样式。
 * DOM 侧的选区定位由 WysiwygEditor 负责，传入已定位的块节点即可。
 */

/** 标题排版属性白名单：颜色/背景是用户表达，不在剥离范围。 */
export const HEADING_TYPO_PROPS = [
  'font-size',
  'font-family',
  'font-weight',
  'line-height',
  'letter-spacing',
] as const

/**
 * 只有"切块/合块"类 inputType 会在标题里留下浏览器样式伪影。
 * `insertText` 等打字路径**刻意排除**——那里的字号是用户通过字号按钮显式设置的。
 */
export const BLOCK_SPLIT_MERGE_INPUT_TYPES: ReadonlySet<string> = new Set([
  'insertParagraph',
  'insertLineBreak',
  'deleteContentBackward',
  'deleteContentForward',
])

export function shouldNormalizeHeadingAfterInput(inputType: string | undefined | null): boolean {
  return !!inputType && BLOCK_SPLIT_MERGE_INPUT_TYPES.has(inputType)
}

export function isHeadingTag(tag: string | undefined | null): boolean {
  return /^H[1-6]$/.test(String(tag || '').toUpperCase())
}

function stripTypography(el: HTMLElement) {
  HEADING_TYPO_PROPS.forEach((p) => el.style.removeProperty(p))
  if (!el.getAttribute('style')) el.removeAttribute('style')
}

/** `<font size face>` 的呈现属性对 CSS 不可见 —— 展开保文本。 */
function unwrapKeepText(el: HTMLElement) {
  const parent = el.parentNode
  if (!parent) return
  while (el.firstChild) {
    parent.insertBefore(el.firstChild, el)
  }
  parent.removeChild(el)
  if (parent.normalize) parent.normalize()
}

/** 剥掉标题及其后代上的排版内联样式；不动颜色/背景。 */
export function normalizeHeadingTypography(h: HTMLElement) {
  stripTypography(h)
  Array.from(h.querySelectorAll<HTMLElement>('*')).forEach(stripTypography)
  Array.from(h.querySelectorAll<HTMLElement>('font')).forEach(unwrapKeepText)
}

/**
 * 在切块/合块之后归一化受影响的标题块。
 *
 * @param block            光标当前所在块（可为非标题）
 * @param previousSibling  该块的前一兄弟（Backspace 合块时被合并进的目标，可为 null）
 * @returns 实际归一化的标题块数量
 */
export function normalizeHeadingBlocksAround(
  block: HTMLElement | null,
  previousSibling: HTMLElement | null,
): number {
  const targets: HTMLElement[] = []
  if (block && isHeadingTag(block.tagName)) targets.push(block)
  if (previousSibling && isHeadingTag(previousSibling.tagName)) targets.push(previousSibling)
  targets.forEach(normalizeHeadingTypography)
  return targets.length
}
