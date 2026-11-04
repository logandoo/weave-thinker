// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * External clipboard normalization for notes.
 *
 * Kept framework-free so paste handling and tests can share one semantic pass
 * before the editor inserts foreign HTML. (The drop path still bypasses this
 * module; see the P0 audit disclosure.)
 */

const LIST_MARKER_RE = /^(?:\d+[.)]|[a-zA-Z][.)]|[-•●○▪◦])\s*/u

export function extractClipboardFragment(html: string): string {
  if (!html) return html
  const startToken = '<!--StartFragment-->'
  const endToken = '<!--EndFragment-->'
  const start = html.indexOf(startToken)
  const end = html.indexOf(endToken, start + startToken.length)
  if (start < 0 || end < 0) return html
  return html.slice(start + startToken.length, end)
}

interface OfficeListItem {
  paragraph: HTMLParagraphElement
  level: number
  ordered: boolean
  marker: string
  markerFromSpan: boolean
  markerSpan: HTMLSpanElement | null
  number: number | null
  textAlign: string
}

function isOfficeListParagraph(el: Element | null): el is HTMLParagraphElement {
  if (!el || el.tagName !== 'P') return false
  const style = el.getAttribute('style') || ''
  if (/mso-list\s*:/i.test(style)) return true
  return !!el.querySelector('[style*="mso-list" i]')
}

function readOfficeListItem(paragraph: HTMLParagraphElement): OfficeListItem {
  const style = paragraph.getAttribute('style') || ''
  const levelMatch = style.match(/\blevel(\d+)\b/i)
  const markerEl = Array.from(paragraph.querySelectorAll('span')).find((span) =>
    /mso-list\s*:\s*ignore/i.test(span.getAttribute('style') || ''),
  )
  const leadingText = (paragraph.textContent || '').replace(/\u00a0/g, ' ').trimStart()
  const leadingMarker = leadingText.match(LIST_MARKER_RE)?.[0]?.trim() || ''
  const marker = (markerEl?.textContent || '').replace(/\u00a0/g, ' ').trim() || leadingMarker
  const markerMatch = marker.match(LIST_MARKER_RE)
  let textAlign = paragraph.style.textAlign || paragraph.getAttribute('align') || ''
  if (!textAlign) {
    const m = style.match(/text-align\s*:\s*([^;]+)/i)
    if (m) textAlign = m[1].trim()
  }
  const ordered = /^\d+[.)]/.test(marker)
  const numMatch = marker.match(/^(\d+)[.)]/)
  return {
    paragraph,
    level: Math.max(1, Math.min(9, parseInt(levelMatch?.[1] || '1', 10) || 1)),
    ordered,
    marker,
    markerFromSpan: !!markerEl,
    markerSpan: markerEl ?? null,
    number: numMatch ? parseInt(numMatch[1], 10) : null,
    textAlign,
  }
}

function newList(doc: Document, ordered: boolean): HTMLOListElement | HTMLUListElement {
  return doc.createElement(ordered ? 'ol' : 'ul')
}

function buildOfficeList(doc: Document, items: OfficeListItem[]): HTMLElement {
  const root = newList(doc, items[0].ordered)
  const stack: Array<{ level: number; list: HTMLOListElement | HTMLUListElement; lastLi: HTMLLIElement | null }> = [
    { level: 1, list: root, lastLi: null },
  ]
  let expectedNumber = items[0].number
  if (root.tagName === 'OL' && expectedNumber && expectedNumber > 1) {
    (root as HTMLOListElement).start = expectedNumber
  }

  for (const item of items) {
    while (stack.length < item.level) {
      const parent = stack[stack.length - 1]
      let parentLi = parent.lastLi
      if (!parentLi) {
        parentLi = doc.createElement('li')
        parent.list.appendChild(parentLi)
        parent.lastLi = parentLi
      }
      const nested = newList(doc, item.ordered)
      parentLi.appendChild(nested)
      stack.push({ level: stack.length + 1, list: nested, lastLi: null })
    }
    while (stack.length > item.level) stack.pop()
    const current = stack[stack.length - 1]
    if ((item.ordered && current.list.tagName !== 'OL') || (!item.ordered && current.list.tagName !== 'UL')) {
      // Top-level type changes split the group before reaching here, so this is
      // a nested list switching type. Replacing a non-empty list would delete
      // its items; append a sibling list instead and continue there.
      const replacement = newList(doc, item.ordered)
      if (current.list.children.length === 0) {
        current.list.replaceWith(replacement)
      } else if (current.list.parentNode) {
        current.list.parentNode.insertBefore(replacement, current.list.nextSibling)
      }
      current.list = replacement
      current.lastLi = null
    }
    item.markerSpan?.remove()
    const li = doc.createElement('li')
    if (current.list.tagName === 'OL' && item.number != null && item.number !== (expectedNumber ?? 1)) {
      li.value = item.number
    }
    if (item.textAlign && item.textAlign !== 'left' && item.textAlign !== 'start') {
      li.style.textAlign = item.textAlign
    }
    while (item.paragraph.firstChild) li.appendChild(item.paragraph.firstChild)
    // Word usually supplies a marker span; if it supplied only text, trim it.
    if (!item.markerFromSpan && li.firstChild?.nodeType === Node.TEXT_NODE) {
      li.firstChild.textContent = (li.firstChild.textContent || '').replace(LIST_MARKER_RE, '')
    }
    current.list.appendChild(li)
    current.lastLi = li
    if (current.list.tagName === 'OL') expectedNumber = (item.number ?? (expectedNumber ?? 1)) + 1
  }
  return root
}

/** Rebuild Word/Office paragraph "lists" into semantic list containers. */
export function normalizeOfficeLists(doc: Document): void {
  const body = doc.body
  // Snapshot before mutating marker spans; sibling pointers are re-read from
  // the live tree so removing a marker span cannot terminate the group early.
  const candidates = Array.from(body.querySelectorAll('p'))
    .filter(isOfficeListParagraph)
  const candidateSet = new Set<Element>(candidates)
  let index = 0
  while (index < candidates.length) {
    const first = candidates[index]
    if (!first.isConnected) {
      index += 1
      continue
    }
    const firstItem = readOfficeListItem(first)
    const group: OfficeListItem[] = [firstItem]
    let next = first.nextElementSibling
    while (next && candidateSet.has(next)) {
      const item = readOfficeListItem(next as HTMLParagraphElement)
      if (item.level === 1 && item.ordered !== firstItem.ordered) break
      group.push(item)
      next = next.nextElementSibling
    }
    const root = buildOfficeList(doc, group)
    // Word puts list paragraphs inside table cells / blockquotes too; the list
    // must be inserted into their actual parent, never blindly into <body>
    // (body.insertBefore throws NotFoundError for a nested reference node and
    // would abort the whole paste).
    const parent = first.parentElement ?? body
    parent.insertBefore(root, first)
    group.forEach((item) => item.paragraph.remove())
    index += group.length
  }
}
