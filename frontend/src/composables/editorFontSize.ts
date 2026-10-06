// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 选区字号读数（笔记模式/工作台模式共用 —— wave-2 C2 抽取）。
 *
 * 旧实现按 anchor/focus 祖先取字号：选区端点落在 run 边界时（如从 24px 段末
 * 到 18px 段），边界外的 run 被误计入 → 混合判定失真（B3a 排障实锤）。
 * 现按「选区真正覆盖的文本段」逐段取祖先字号，全部一致才返回，否则空串。
 */

const DEFAULT_OPTIONS = ['12px', '14px', '16px', '18px', '20px', '24px', '28px', '32px', '36px']

function normalizeSize(raw: string, options: string[]): string {
  if (!raw) return ''
  if (options.includes(raw)) return raw
  const px = parseFloat(raw)
  if (isNaN(px)) return ''
  let closest = ''
  let minDiff = Infinity
  for (const opt of options) {
    const diff = Math.abs(px - parseFloat(opt))
    if (diff < minDiff) {
      minDiff = diff
      closest = opt
    }
  }
  return closest
}

function sizeFromNode(node: Node, editorEl: HTMLElement, options: string[]): string {
  let n: Node | null = node
  let firstElement: HTMLElement | null = null
  while (n && n !== editorEl) {
    if (n.nodeType === Node.ELEMENT_NODE) {
      const el = n as HTMLElement
      if (!firstElement) firstElement = el
      const fs = el.style?.fontSize
      if (fs) {
        const norm = normalizeSize(fs, options)
        if (norm) return norm
      }
    }
    n = n.parentNode
  }
  if (firstElement) {
    const norm = normalizeSize(getComputedStyle(firstElement).fontSize, options)
    if (norm) return norm
  }
  return ''
}

export function readSelectionFontSize(
  editorEl: HTMLElement | null | undefined,
  options: string[] = DEFAULT_OPTIONS,
): string {
  if (!editorEl) return ''
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorEl.contains(sel.anchorNode)) return ''
  const range = sel.getRangeAt(0)

  // 折叠光标：读光标处字号（旧语义；否则用户点进 18px 文本打开面板显示空白）
  if (range.collapsed) {
    const anchor = sel.anchorNode
    if (!anchor) return ''
    return sizeFromNode(anchor, editorEl, options)
  }

  // 逐文本段夹取选区真实覆盖部分（区间交集；跳过空节点与仅触边节点）
  const w = document.createTreeWalker(editorEl, NodeFilter.SHOW_TEXT)
  const sizes: string[] = []
  let n: Node | null
  while ((n = w.nextNode())) {
    const t = n as Text
    const len = t.textContent?.length || 0
    if (!len) continue
    // 纯空白段不参与混合判定（否则空格节点的继承字号污染读数）
    if (!(t.textContent || '').trim()) continue
    const sub = range.cloneRange()
    try {
      // 交集 [max(S,A), min(E,B)]：A 在 S 之后 → 起点收至 (t,0)；B 在 E 之前 → 终点收至 (t,len)
      if (range.comparePoint(t, 0) >= 0) sub.setStart(t, 0)
      if (range.comparePoint(t, len) <= 0) sub.setEnd(t, len)
    } catch {
      continue
    }
    if (sub.collapsed) continue
    const s = sizeFromNode(t, editorEl, options)
    if (s && !sizes.includes(s)) sizes.push(s)
    if (sizes.length > 1) return '' // 混合字号
  }
  return sizes[0] || (sel.anchorNode ? sizeFromNode(sel.anchorNode, editorEl, options) : '')
}
