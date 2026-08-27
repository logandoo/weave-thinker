<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="wysiwyg-editor-wrap" :class="tableCursorClass" style="position:relative;flex:1;overflow:hidden;display:flex;flex-direction:column">
    <div class="wysiwyg-editor" ref="editorRef" contenteditable="true" @input="onInput" @paste="onPaste" @click="onClick" @contextmenu="onContextMenu" @mousedown="handleMouseDown" v-html="renderedContent"></div>
  </div>
  <Teleport to="body">
    <div v-show="tableEdgeButton.visible" class="table-edge-btn" :style="{ top: tableEdgeButton.y + 'px', left: tableEdgeButton.x + 'px' }" @mousedown.stop.prevent="handleTableEdgeInsert" @mouseleave="hideTableEdgeButton">+</div>
  </Teleport>
  <Teleport to="body">
    <div v-if="imageResizeState.visible" class="image-resize-overlay" :style="{
      position: 'fixed',
      top: imageResizeState.y + 'px',
      left: imageResizeState.x + 'px',
      width: imageResizeState.width + 'px',
      height: imageResizeState.height + 'px',
      pointerEvents: 'none',
      zIndex: 9998,
    }">
      <div class="image-resize-border" style="position:absolute;inset:0;border:2px solid #4a90d9;pointer-events:none;"></div>
      <div class="image-resize-handle image-resize-handle-se" @mousedown.stop.prevent="onImageResizeMouseDown($event, 'se')" style="position:absolute;right:-5px;bottom:-5px;width:10px;height:10px;background:#4a90d9;border:1px solid #fff;cursor:nwse-resize;pointer-events:auto;border-radius:2px;"></div>
    </div>
  </Teleport>
  <Teleport to="body">
    <div v-if="tableContextMenu.visible" class="table-context-menu" :style="{ top: tableContextMenu.y + 'px', left: tableContextMenu.x + 'px' }" @click.stop>
      <button class="context-menu-item" @click="tableInsertRowAbove">
        <span class="context-menu-icon">↑</span>
        <span>在上方插入行</span>
      </button>
      <button class="context-menu-item" @click="tableInsertRowBelow">
        <span class="context-menu-icon">↓</span>
        <span>在下方插入行</span>
      </button>
      <button class="context-menu-item" @click="tableInsertColumnLeft">
        <span class="context-menu-icon">←</span>
        <span>在左侧插入列</span>
      </button>
      <button class="context-menu-item" @click="tableInsertColumnRight">
        <span class="context-menu-icon">→</span>
        <span>在右侧插入列</span>
      </button>
      <div class="context-menu-divider"></div>
      <button v-if="hasSelectedRows" class="context-menu-item danger" @click="deleteSelectedTableRows">
        <span class="context-menu-icon">×</span>
        <span>删除选中行 ({{ tableSelection.indices.size }})</span>
      </button>
      <button v-if="!hasSelectedRows" class="context-menu-item danger" @click="tableDeleteRow">
        <span class="context-menu-icon">×</span>
        <span>删除行</span>
      </button>
      <button v-if="hasSelectedCols" class="context-menu-item danger" @click="deleteSelectedTableCols">
        <span class="context-menu-icon">×</span>
        <span>删除选中列 ({{ tableSelection.indices.size }})</span>
      </button>
      <button v-if="!hasSelectedCols" class="context-menu-item danger" @click="tableDeleteColumn">
        <span class="context-menu-icon">×</span>
        <span>删除列</span>
      </button>
      <div class="context-menu-divider"></div>
      <button class="context-menu-item danger" @click="tableDeleteTable">
        <span class="context-menu-icon">🗑</span>
        <span>删除表格</span>
      </button>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { renderMarkdownToHtml, htmlToMarkdown, renderMermaidBlocks, renderEchartsBlocks, attachMathEditListeners } from '@/composables/useMarkdown'

const props = defineProps<{
  modelValue: string
  findQuery?: string
  findActive?: boolean
  findCurrentIndex?: number
  imageResolver?: (src: string) => Promise<string>
  imageUploader?: (file: File) => Promise<string>
  mediaResolver?: (src: string) => Promise<string>
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'change'): void
  (e: 'find-request', withReplace: boolean): void
  (e: 'image-upload-failed'): void
}>()

// Multi-selection state
const multiSelections = ref<Set<string>>(new Set())
const isMultiSelectMode = ref(false)
let multiSelectCounter = 0

// Undo/Redo state
interface SavedSelection {
  startPath: number[]
  startOffset: number
  endPath: number[]
  endOffset: number
}

interface EditorState {
  html: string
  selection: SavedSelection | null
}

const undoStack = ref<EditorState[]>([])
const redoStack = ref<EditorState[]>([])
let isUndoing = false
let isExternalContentUpdate = false
let inputDebounceTimer: ReturnType<typeof setTimeout> | null = null

function getNodePath(node: Node, root: Node): number[] {
  const path: number[] = []
  let current: Node | null = node
  while (current && current !== root) {
    const parent = current.parentNode
    if (!parent) break
    const index = Array.from(parent.childNodes).indexOf(current as ChildNode)
    path.unshift(index)
    current = parent
  }
  return path
}

function getNodeFromPath(path: number[], root: Node): Node | null {
  let current: Node = root
  for (const idx of path) {
    const children = current.childNodes
    if (idx < 0 || idx >= children.length) return null
    current = children[idx]
  }
  return current
}

function saveSelection(root: HTMLElement): SavedSelection | null {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return null
  const range = sel.getRangeAt(0)
  try {
    return {
      startPath: getNodePath(range.startContainer, root),
      startOffset: range.startOffset,
      endPath: getNodePath(range.endContainer, root),
      endOffset: range.endOffset,
    }
  } catch {
    return null
  }
}

function restoreSelection(root: HTMLElement, saved: SavedSelection | null): boolean {
  if (!saved) return false
  const startNode = getNodeFromPath(saved.startPath, root)
  const endNode = getNodeFromPath(saved.endPath, root)
  if (!startNode || !endNode) return false
  try {
    const range = document.createRange()
    range.setStart(startNode, Math.min(saved.startOffset, startNode.textContent?.length || 0))
    range.setEnd(endNode, Math.min(saved.endOffset, endNode.textContent?.length || 0))
    const sel = window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
    return true
  } catch {
    return false
  }
}

let isComposing = false

function onCompositionStart() {
  if (editorRef.value && !isUndoing) {
    pushUndoState(true)
  }
  isComposing = true
}

function onCompositionEnd() {
  isComposing = false
  if (editorRef.value && !isUndoing) {
    pushCurrentState()
  }
}

let savedEditorSelection: SavedSelection | null = null

function saveEditorSelection() {
  if (!editorRef.value) return
  savedEditorSelection = saveSelection(editorRef.value)
}

function restoreEditorSelection() {
  if (!editorRef.value || !savedEditorSelection) return
  restoreSelection(editorRef.value, savedEditorSelection)
  savedEditorSelection = null
}

function pushUndoState(force = false) {
  if (isUndoing || !editorRef.value) return
  const state: EditorState = {
    html: editorRef.value.innerHTML,
    selection: saveSelection(editorRef.value),
  }
  const last = undoStack.value[undoStack.value.length - 1]
  if (force || !last || last.html !== state.html) {
    undoStack.value.push(state)
    if (undoStack.value.length > 100) undoStack.value.shift()
    redoStack.value = []
  }
}

function pushCurrentState() {
  if (isUndoing || !editorRef.value) return
  const state: EditorState = {
    html: editorRef.value.innerHTML,
    selection: saveSelection(editorRef.value),
  }
  const last = undoStack.value[undoStack.value.length - 1]
  if (!last || last.html !== state.html) {
    undoStack.value.push(state)
    if (undoStack.value.length > 100) undoStack.value.shift()
    redoStack.value = []
  }
}

function scheduleInputUndo() {
  if (isExternalContentUpdate || isComposing) return
  if (inputDebounceTimer) clearTimeout(inputDebounceTimer)
  inputDebounceTimer = setTimeout(() => {
    if (!isExternalContentUpdate && !isComposing) {
      pushCurrentState()
    }
    inputDebounceTimer = null
  }, 500)
}

function resetUndoStack() {
  undoStack.value = []
  redoStack.value = []
  if (inputDebounceTimer) {
    clearTimeout(inputDebounceTimer)
    inputDebounceTimer = null
  }
}

function undo() {
  if (editorRef.value) {
    editorRef.value.dataset.undoAttempted = 'true'
    editorRef.value.dataset.undoStackSize = String(undoStack.value.length)
  }
  if (undoStack.value.length <= 1 || !editorRef.value) return
  const current = undoStack.value.pop()!
  redoStack.value.push(current)
  const prev = undoStack.value[undoStack.value.length - 1]
  if (editorRef.value) {
    editorRef.value.dataset.undoExecuted = 'true'
  }
  applyState(prev)
}

function redo() {
  if (redoStack.value.length === 0 || !editorRef.value) return
  const state = redoStack.value.pop()!
  undoStack.value.push(state)
  applyState(state)
}

function applyState(state: EditorState) {
  if (!editorRef.value) return
  isUndoing = true
  editorRef.value.innerHTML = state.html
  nextTick(() => {
    if (editorRef.value && state.selection) {
      restoreSelection(editorRef.value, state.selection)
    }
    onInput()
    isUndoing = false
  })
}

function getLineId(lineIndex: number): string {
  return `line-${lineIndex}`
}

function getLineIndexFromId(id: string): number {
  return parseInt(id.replace('line-', ''), 10)
}

let mouseDownPos = { x: 0, y: 0 }
let isMouseDown = false
let hasDragged = false
const DRAG_THRESHOLD = 5

function handleMouseDown(ev: MouseEvent) {
  onTableMouseDown(ev)
  const mermaidBtn = (ev.target as HTMLElement).closest?.('.mermaid-edit-btn, .mermaid-zoom-btn') as HTMLElement | null
  if (mermaidBtn && editorRef.value) {
    ev.preventDefault()
    ev.stopPropagation()
    const block = mermaidBtn.closest('.mermaid-block') as HTMLElement | null
    if (!block) return
    const source = block.getAttribute('data-mermaid-source') || ''
    const id = block.getAttribute('data-mermaid-id') || ''
    const isZoom = mermaidBtn.classList.contains('mermaid-zoom-btn')
    if (isZoom) {
      const svg = block.getAttribute('data-cached-svg') || block.querySelector('.mermaid-rendered-content')?.innerHTML || ''
      const event = new CustomEvent('mermaid-zoom', { detail: { source, id, svg, handled: false }, bubbles: true })
      block.dispatchEvent(event)
    } else {
      const event = new CustomEvent('mermaid-edit', { detail: { source, id, handled: false }, bubbles: true })
      block.dispatchEvent(event)
    }
    return
  }
  const mathBtn = (ev.target as HTMLElement).closest?.('.math-edit-btn') as HTMLElement | null
  if (mathBtn && editorRef.value) {
    ev.preventDefault()
    ev.stopPropagation()
    const mathEl = mathBtn.closest('.math-editable') as HTMLElement | null
    if (!mathEl) return
    const tex = mathEl.getAttribute('data-tex') || ''
    const displayMode = mathEl.getAttribute('data-display-mode') === 'true'
    const event = new CustomEvent('math-edit', { detail: { tex, displayMode, element: mathEl }, bubbles: true })
    mathEl.dispatchEvent(event)
    return
  }
  if (ev.metaKey || ev.ctrlKey) {
    isMouseDown = true
    hasDragged = false
    mouseDownPos = { x: ev.clientX, y: ev.clientY }
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp, { once: true })
  } else {
    if (multiSelections.value.size > 0) {
      clearMultiSelections()
    }
  }
}

function handleMouseMove(ev: MouseEvent) {
  if (!isMouseDown) return
  const dx = Math.abs(ev.clientX - mouseDownPos.x)
  const dy = Math.abs(ev.clientY - mouseDownPos.y)
  if (dx > DRAG_THRESHOLD || dy > DRAG_THRESHOLD) {
    hasDragged = true
  }
}

function handleMouseUp(ev: MouseEvent) {
  isMouseDown = false
  document.removeEventListener('mousemove', handleMouseMove)
  
  if (hasDragged) {
    // User dragged with ctrl/cmd: let browser handle multi-range text selection
    return
  }
  
  // User clicked with ctrl/cmd: toggle block multi-selection
  try {
    ev.preventDefault()
    const el = editorRef.value
    if (!el) return
    
    const range = (document as any).caretRangeFromPoint?.(ev.clientX, ev.clientY)
    if (!range) return
    
    let node: Node | null = range.startContainer
    let block: HTMLElement | null = null
    while (node && node !== el) {
      if (node.nodeType === 1) {
        const tag = (node as HTMLElement).tagName
        if (['P', 'DIV', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE'].includes(tag)) {
          block = node as HTMLElement
          break
        }
      }
      node = node.parentNode
    }
    
    if (block) {
      let lineId = block.getAttribute('data-line-id')
      if (!lineId) {
        lineId = `line-${multiSelectCounter++}`
        block.setAttribute('data-line-id', lineId)
      }
      
      const selections = new Set(multiSelections.value)
      if (selections.has(lineId)) {
        selections.delete(lineId)
        block.classList.remove('multi-selected')
      } else {
        selections.add(lineId)
        block.classList.add('multi-selected')
      }
      multiSelections.value = selections
      isMultiSelectMode.value = selections.size > 0
    }
  } catch {
    // Ignore errors from caretRangeFromPoint in unsupported browsers
  }
}

function clearMultiSelections() {
  const el = editorRef.value
  if (!el) return
  el.querySelectorAll('.multi-selected').forEach(block => {
    block.classList.remove('multi-selected')
  })
  multiSelections.value = new Set()
  isMultiSelectMode.value = false
}

function getMultiSelectedContentHtml(): string {
  const el = editorRef.value
  if (!el) return ''
  const blocks = el.querySelectorAll('.multi-selected')
  return Array.from(blocks).map(b => b.outerHTML || (b as HTMLElement).textContent).join('')
}

function saveCleanUndoState() {
  // Save current state as undo checkpoint, but strip multi-selected
  // classes and data-line-id attributes so undo restores clean content.
  if (!editorRef.value) return
  const el = editorRef.value
  const selBlocks = Array.from(el.querySelectorAll('.multi-selected')) as HTMLElement[]
  // Remember original data-line-id values
  const savedIds = selBlocks.map(b => b.getAttribute('data-line-id'))
  selBlocks.forEach(b => {
    b.classList.remove('multi-selected')
    b.removeAttribute('data-line-id')
  })
  pushUndoState(true)
  // Restore classes and ids for the ongoing operation
  selBlocks.forEach((b, i) => {
    b.classList.add('multi-selected')
    const id = savedIds[i] || `line-${multiSelectCounter++}`
    b.setAttribute('data-line-id', id)
  })
}

function applyToListSelections(listType: 'ol' | 'ul') {
  const el = editorRef.value
  if (!el || multiSelections.value.size === 0) return
  
  saveCleanUndoState()
  
  const selectedBlocks = Array.from(el.querySelectorAll('.multi-selected')) as HTMLElement[]
  if (selectedBlocks.length === 0) return
  
  // Sort by DOM order
  const allBlocks = Array.from(el.querySelectorAll('p, div, li, h1, h2, h3, h4, h5, h6, blockquote, pre'))
  const selectedSet = new Set(selectedBlocks)
  const orderedSelected = allBlocks.filter(b => selectedSet.has(b))
  let lastBlock: HTMLElement | null = null
  
  if (listType === 'ol') {
    const groups: HTMLElement[][] = []
    let currentGroup: HTMLElement[] = []
    for (const block of orderedSelected) {
      if (currentGroup.length === 0) {
        currentGroup.push(block)
      } else {
        const prev = currentGroup[currentGroup.length - 1]
        if (block.previousElementSibling === prev) {
          currentGroup.push(block)
        } else {
          groups.push(currentGroup)
          currentGroup = [block]
        }
      }
    }
    if (currentGroup.length > 0) groups.push(currentGroup)

    for (const group of groups) {
      const ol = document.createElement('ol')
      for (const block of group) {
        const li = document.createElement('li')
        li.innerHTML = block.innerHTML.replace(/^\d+\.?\s*/, '')
        ol.appendChild(li)
      }
      group[0].parentNode?.replaceChild(ol, group[0])
      for (let i = 1; i < group.length; i++) {
        group[i].parentNode?.removeChild(group[i])
      }
      const liInOl = ol.querySelector('li')
      if (liInOl) lastBlock = liInOl
    }
  } else {
    orderedSelected.forEach((block) => {
      const innerHTML = block.innerHTML
      const cleaned = innerHTML.replace(/^[-*]\s*/, '')
      block.innerHTML = `- ${cleaned}`
      if (block.tagName !== 'P') {
        const p = document.createElement('p')
        p.innerHTML = block.innerHTML
        p.setAttribute('data-line-id', block.getAttribute('data-line-id') || '')
        block.parentNode?.replaceChild(p, block)
        lastBlock = p
      } else {
        lastBlock = block
      }
    })
  }
  
  clearMultiSelections()
  onInput()

  if (lastBlock) {
    nextTick(() => {
      try {
        const range = document.createRange()
        range.selectNodeContents(lastBlock!)
        range.collapse(false)
        const sel = window.getSelection()
        sel?.removeAllRanges()
        sel?.addRange(range)
        lastBlock?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      } catch { /* ignore */ }
      pushCurrentState()
    })
  } else {
    nextTick(() => pushCurrentState())
  }
}

function applyBlockquoteToSelections() {
  const el = editorRef.value
  if (!el || multiSelections.value.size === 0) return
  
  saveCleanUndoState()
  
  const selectedBlocks = Array.from(el.querySelectorAll('.multi-selected')) as HTMLElement[]
  if (selectedBlocks.length === 0) return
  
  // Sort by DOM order
  const allBlocks = Array.from(el.querySelectorAll('p, div, li, h1, h2, h3, h4, h5, h6, blockquote, pre'))
  const selectedSet = new Set(selectedBlocks)
  const orderedSelected = allBlocks.filter(b => selectedSet.has(b))
  
  // Group consecutive selected blocks: adjacent or separated only by non-block nodes → same group
  const groups: HTMLElement[][] = []
  let currentGroup: HTMLElement[] = [orderedSelected[0]]
  for (let i = 1; i < orderedSelected.length; i++) {
    const prev = orderedSelected[i - 1]
    const curr = orderedSelected[i]
    // Walk from prev's next sibling to curr. If we encounter any block-level
    // element that is NOT selected (and NOT curr itself), they're not consecutive.
    let areConsecutive = true
    let sib: Node | null = prev.nextSibling
    while (sib && sib !== curr) {
      if (sib.nodeType === Node.ELEMENT_NODE) {
        const tag = (sib as HTMLElement).tagName
        if (['P', 'DIV', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE'].includes(tag)) {
          // Found a non-selected block element between prev and curr
          areConsecutive = false
          break
        }
      }
      sib = sib.nextSibling
    }
    if (areConsecutive) {
      currentGroup.push(curr)
    } else {
      groups.push(currentGroup)
      currentGroup = [curr]
    }
  }
  groups.push(currentGroup)
  
  let lastBlock: HTMLElement | null = null
  
  // Check if ALL selected blocks are already inside a blockquote
  const allInBlockquote = orderedSelected.every(b => {
    let parent = b.parentElement
    while (parent && parent !== el) {
      if (parent.tagName === 'BLOCKQUOTE') return true
      parent = parent.parentElement
    }
    return false
  })
  
  if (allInBlockquote) {
    // Remove blockquote: unwrap all selected blocks
    orderedSelected.forEach((block) => {
      const bq = block.closest('blockquote')
      if (bq && bq.parentNode) {
        bq.parentNode.insertBefore(block, bq)
        if (bq.childNodes.length === 0 || (bq.childNodes.length === 1 && bq.firstChild?.nodeType === Node.TEXT_NODE && !bq.firstChild.textContent?.trim())) {
          bq.parentNode.removeChild(bq)
        }
      }
      lastBlock = block
    })
  } else {
    // Wrap each contiguous group in its own blockquote
    groups.forEach((group) => {
      const firstBlock = group[0]
      const parent = firstBlock.parentNode
      if (!parent) return
      
      const blockquote = document.createElement('blockquote')
      parent.insertBefore(blockquote, firstBlock)
      
      group.forEach((b) => {
        blockquote.appendChild(b)
        lastBlock = b
      })
    })
  }
  
  clearMultiSelections()
  onInput()

  if (lastBlock) {
    nextTick(() => {
      try {
        const range = document.createRange()
        range.selectNodeContents(lastBlock!)
        range.collapse(false)
        const sel = window.getSelection()
        sel?.removeAllRanges()
        sel?.addRange(range)
        lastBlock?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      } catch { /* ignore */ }
      pushCurrentState()
    })
  } else {
    nextTick(() => pushCurrentState())
  }
}

function normalizeColor(cssColor: string): string {
  const tmp = document.createElement('span')
  tmp.style.backgroundColor = cssColor
  document.body.appendChild(tmp)
  const normalized = tmp.style.backgroundColor || ''
  document.body.removeChild(tmp)
  return normalized
}

function collectTextNodesInRange(range: Range, root: HTMLElement): Text[] {
  const walker = document.createTreeWalker(
    root,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode: (node) => {
        return range.intersectsNode(node)
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT
      }
    }
  )
  const textNodes: Text[] = []
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (range.toString().length === 0) continue
    textNodes.push(node as Text)
  }
  return textNodes
}

function applyHighlightToSelection(color: string = '#ffff00') {
  pushUndoState(true)
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return

  const root = editorRef.value
  if (!root) return

  const isRemoveMode = !color
  const normalizedTargetColor = isRemoveMode ? '' : normalizeColor(color)

  const ranges = Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i))

  for (const range of ranges) {
    if (range.collapsed) continue

    const textNodes = collectTextNodesInRange(range, root)
    if (textNodes.length === 0) continue

    const highlightAncestors = new Set<HTMLElement>()
    let allHighlighted = true
    let firstHighlightColor: string | null = null

    for (const textNode of textNodes) {
      const txt = textNode.textContent || ''
      if (!txt.trim()) continue

      const ancestor = findAncestorHighlight(root, textNode)
      if (!ancestor) {
        allHighlighted = false
        break
      }
      highlightAncestors.add(ancestor)
      const c = ancestor.style.backgroundColor || ''
      const nc = c ? normalizeColor(c) : ''
      if (firstHighlightColor === null) {
        firstHighlightColor = nc
      } else if (firstHighlightColor !== nc) {
        allHighlighted = false
        break
      }
    }

    if (isRemoveMode) {
      // Remove highlights from all highlighted text nodes in range,
      // even if not every text node is highlighted (partial selection)
      for (const textNode of textNodes) {
        const txt = textNode.textContent || ''
        if (!txt.trim()) continue
        const ancestor = findAncestorHighlight(root, textNode)
        if (ancestor) {
          unwrapElementKeepText(ancestor)
        }
      }
      continue
    }

    if (allHighlighted && firstHighlightColor === normalizedTargetColor) {
      for (const ancestor of highlightAncestors) {
        unwrapElementKeepText(ancestor)
      }
      continue
    }

    const contents = range.extractContents()
    stripStylePropertyInFragment(contents, 'background-color')
    const span = document.createElement('span')
    span.style.backgroundColor = color
    span.style.borderRadius = '2px'
    span.appendChild(contents)
    range.insertNode(span)
  }

  selection.removeAllRanges()
  onInput()
  nextTick(() => pushCurrentState())
}

function applyHighlightPerTextNode(range: Range, root: HTMLElement, color: string) {
  const textNodes = collectTextNodesInRange(range, root)

  const startContainer = range.startContainer
  const endContainer = range.endContainer
  const startOffset = range.startOffset
  const endOffset = range.endOffset

  for (const textNode of textNodes) {
    const nodeRange = document.createRange()

    if (textNode === startContainer) {
      nodeRange.setStart(textNode, startOffset)
    } else {
      nodeRange.setStart(textNode, 0)
    }

    if (textNode === endContainer) {
      nodeRange.setEnd(textNode, endOffset)
    } else {
      nodeRange.setEnd(textNode, textNode.length)
    }

    if (nodeRange.collapsed) continue

    // Extract + strip old background colors so the new highlight is not
    // overridden by a nested old highlight span.
    const contents = nodeRange.extractContents()
    stripStylePropertyInFragment(contents, 'background-color')
    const newSpan = document.createElement('span')
    newSpan.style.backgroundColor = color
    newSpan.style.borderRadius = '2px'
    newSpan.appendChild(contents)
    nodeRange.insertNode(newSpan)
  }
}

function findAncestorHighlight(root: HTMLElement, node: Node): HTMLElement | null {
  let current: Node | null = node
  while (current && current !== root) {
    if (current.nodeType === Node.ELEMENT_NODE) {
      const el = current as HTMLElement
      if ((el.tagName === 'SPAN' && el.style.backgroundColor) || el.tagName === 'MARK') {
        return el
      }
    }
    current = current.parentNode
  }
  return null
}

function unwrapElementKeepText(el: HTMLElement) {
  const parent = el.parentNode
  if (!parent) return
  while (el.firstChild) {
    parent.insertBefore(el.firstChild, el)
  }
  parent.removeChild(el)
  if (parent.normalize) parent.normalize()
}

function stripStylePropertyInFragment(fragment: DocumentFragment, prop: 'color' | 'background-color') {
  fragment.querySelectorAll<HTMLElement>('[style]').forEach(el => {
    el.style.removeProperty(prop)
    if (!el.getAttribute('style')) el.removeAttribute('style')
  })
  // Unwrap spans left with no attributes so they don't interfere with the
  // new wrapper or future serialization.
  Array.from(fragment.querySelectorAll('span')).forEach(s => {
    if (s.attributes.length === 0) unwrapElementKeepText(s)
  })
}

function applyFontColor(color: string) {
  pushUndoState(true)
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return

  const ranges = Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i))

  for (const range of ranges) {
    if (range.collapsed) continue

    // extractContents clones boundary ancestors into the fragment, so
    // stripping color inside the fragment only affects the selected text —
    // this guarantees the NEW color wins instead of being overridden by a
    // nested old color span.
    const contents = range.extractContents()
    stripStylePropertyInFragment(contents, 'color')

    const span = document.createElement('span')
    span.style.color = color
    span.appendChild(contents)
    range.insertNode(span)
  }
  selection.removeAllRanges()
  onInput()
  nextTick(() => pushCurrentState())
}

function findAncestorTag(node: Node, root: HTMLElement, tagName: string): HTMLElement | null {
  let current = node
  while (current && current !== root) {
    if (current.nodeType === Node.ELEMENT_NODE && (current as HTMLElement).tagName === tagName) {
      return current as HTMLElement
    }
    current = current.parentNode as Node
  }
  return null
}

function unwrapElement(el: HTMLElement, range: Range) {
  const parent = el.parentNode
  if (!parent) return
  while (el.firstChild) {
    parent.insertBefore(el.firstChild, el)
  }
  parent.removeChild(el)
  parent.normalize()
}

function applySuperscript() {
  pushUndoState(true)
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return

  const root = editorRef.value
  if (!root) return

  const ranges = Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i))

  for (const range of ranges) {
    if (range.collapsed) continue

    const startSup = findAncestorTag(range.startContainer, root, 'SUP')
    const endSup = findAncestorTag(range.endContainer, root, 'SUP')
    const commonSup = findAncestorTag(range.commonAncestorContainer, root, 'SUP')

    if (startSup && endSup && (startSup === endSup || startSup.contains(endSup) || endSup.contains(startSup))) {
      const targetSup = commonSup || startSup
      const spanText = targetSup.textContent || ''
      const selectedText = range.toString()

      if (spanText === selectedText || targetSup === startSup) {
        unwrapElement(targetSup, range)
      } else {
        const contents = range.extractContents()
        const tempDiv = document.createElement('div')
        tempDiv.appendChild(contents)
        const nestedSups = tempDiv.querySelectorAll('sup')
        nestedSups.forEach(sup => {
          const sParent = sup.parentNode
          if (sParent) {
            while (sup.firstChild) {
              sParent.insertBefore(sup.firstChild, sup)
            }
            sParent.removeChild(sup)
          }
        })
        while (tempDiv.firstChild) {
          range.insertNode(tempDiv.firstChild)
        }
        const p = targetSup.parentNode
        if (p) p.normalize()
      }
      continue
    }

    const sup = document.createElement('sup')
    try {
      range.surroundContents(sup)
    } catch {
      const contents = range.extractContents()
      sup.appendChild(contents)
      range.insertNode(sup)
    }
  }

  selection.removeAllRanges()
  onInput()
  nextTick(() => pushCurrentState())
}

function applySubscript() {
  pushUndoState(true)
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return

  const root = editorRef.value
  if (!root) return

  const ranges = Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i))

  for (const range of ranges) {
    if (range.collapsed) continue

    const startSub = findAncestorTag(range.startContainer, root, 'SUB')
    const endSub = findAncestorTag(range.endContainer, root, 'SUB')
    const commonSub = findAncestorTag(range.commonAncestorContainer, root, 'SUB')

    if (startSub && endSub && (startSub === endSub || startSub.contains(endSub) || endSub.contains(startSub))) {
      const targetSub = commonSub || startSub
      const spanText = targetSub.textContent || ''
      const selectedText = range.toString()

      if (spanText === selectedText || targetSub === startSub) {
        unwrapElement(targetSub, range)
      } else {
        const contents = range.extractContents()
        const tempDiv = document.createElement('div')
        tempDiv.appendChild(contents)
        const nestedSubs = tempDiv.querySelectorAll('sub')
        nestedSubs.forEach(sub => {
          const sParent = sub.parentNode
          if (sParent) {
            while (sub.firstChild) {
              sParent.insertBefore(sub.firstChild, sub)
            }
            sParent.removeChild(sub)
          }
        })
        while (tempDiv.firstChild) {
          range.insertNode(tempDiv.firstChild)
        }
        const p = targetSub.parentNode
        if (p) p.normalize()
      }
      continue
    }

    const sub = document.createElement('sub')
    try {
      range.surroundContents(sub)
    } catch {
      const contents = range.extractContents()
      sub.appendChild(contents)
      range.insertNode(sub)
    }
  }

  selection.removeAllRanges()
  onInput()
  nextTick(() => pushCurrentState())
}

const editorRef = ref<HTMLElement | null>(null)
const renderedContent = ref('')
const suppressInputDepth = ref(0)
let suppressChangeEmit = false
let lastEmittedMarkdown = ''
let hasEverEmittedMarkdown = false
let skipNextModelRender = false

const tableContextMenu = ref({
  visible: false,
  x: 0,
  y: 0,
  cell: null as HTMLTableCellElement | null,
  table: null as HTMLTableElement | null,
})

function onContextMenu(ev: MouseEvent) {
  const target = ev.target as HTMLElement
  const cell = target.closest('td, th') as HTMLTableCellElement | null
  const table = target.closest('table') as HTMLTableElement | null
  
  if (cell && table && editorRef.value?.contains(table)) {
    ev.preventDefault()
    tableContextMenu.value = {
      visible: true,
      x: ev.clientX,
      y: ev.clientY,
      cell,
      table,
    }
    document.addEventListener('click', closeTableContextMenu, { once: true })
    document.addEventListener('contextmenu', closeTableContextMenu, { once: true })
  }
}

function closeTableContextMenu() {
  tableContextMenu.value = { visible: false, x: 0, y: 0, cell: null, table: null }
  document.removeEventListener('click', closeTableContextMenu)
  document.removeEventListener('contextmenu', closeTableContextMenu)
}

function getCellPosition(cell: HTMLTableCellElement, table: HTMLTableElement): { row: number; col: number } {
  const rows = Array.from(table.rows)
  for (let i = 0; i < rows.length; i++) {
    const cells = Array.from(rows[i].cells)
    for (let j = 0; j < cells.length; j++) {
      if (cells[j] === cell) {
        return { row: i, col: j }
      }
    }
  }
  return { row: 0, col: 0 }
}

function tableInsertRowAbove() {
  const { cell, table } = tableContextMenu.value
  if (!cell || !table) return
  closeTableContextMenu()
  
  const { row, col } = getCellPosition(cell, table)
  const refRow = table.rows[row]
  const newRow = table.insertRow(row)
  
  for (let i = 0; i < refRow.cells.length; i++) {
    const newCell = newRow.insertCell(i)
    newCell.innerHTML = '<br>'
  }
  
  pushUndoState(true)
  onInput()
  nextTick(() => pushCurrentState())
}

function tableInsertRowBelow() {
  const { cell, table } = tableContextMenu.value
  if (!cell || !table) return
  closeTableContextMenu()
  
  const { row } = getCellPosition(cell, table)
  const refRow = table.rows[row]
  const newRow = table.insertRow(row + 1)
  
  for (let i = 0; i < refRow.cells.length; i++) {
    const newCell = newRow.insertCell(i)
    newCell.innerHTML = '<br>'
  }
  
  pushUndoState(true)
  onInput()
  nextTick(() => pushCurrentState())
}

function tableInsertColumnLeft() {
  const { cell, table } = tableContextMenu.value
  if (!cell || !table) return
  closeTableContextMenu()
  
  const { col } = getCellPosition(cell, table)
  
  for (let i = 0; i < table.rows.length; i++) {
    const row = table.rows[i]
    const newCell = row.insertCell(col)
    newCell.innerHTML = '<br>'
  }
  
  pushUndoState(true)
  onInput()
  nextTick(() => pushCurrentState())
}

function tableInsertColumnRight() {
  const { cell, table } = tableContextMenu.value
  if (!cell || !table) return
  closeTableContextMenu()
  
  const { col } = getCellPosition(cell, table)
  
  for (let i = 0; i < table.rows.length; i++) {
    const row = table.rows[i]
    const newCell = row.insertCell(col + 1)
    newCell.innerHTML = '<br>'
  }
  
  pushUndoState(true)
  onInput()
  nextTick(() => pushCurrentState())
}

function tableDeleteRow() {
  const { cell, table } = tableContextMenu.value
  if (!cell || !table) return
  closeTableContextMenu()
  
  const { row } = getCellPosition(cell, table)
  
  if (table.rows.length <= 1) {
    tableDeleteTable()
    return
  }
  
  table.deleteRow(row)
  
  pushUndoState(true)
  onInput()
  nextTick(() => pushCurrentState())
}

function tableDeleteColumn() {
  const { cell, table } = tableContextMenu.value
  if (!cell || !table) return
  closeTableContextMenu()
  
  const { col } = getCellPosition(cell, table)
  const colCount = table.rows[0]?.cells.length || 0
  
  if (colCount <= 1) {
    tableDeleteTable()
    return
  }
  
  for (let i = 0; i < table.rows.length; i++) {
    const row = table.rows[i]
    if (col < row.cells.length) {
      row.deleteCell(col)
    }
  }
  
  pushUndoState(true)
  onInput()
  nextTick(() => pushCurrentState())
}

function tableDeleteTable() {
  const { table } = tableContextMenu.value
  if (!table) return
  closeTableContextMenu()
  
  table.remove()
  
  pushUndoState(true)
  onInput()
  nextTick(() => pushCurrentState())
}

function insertTable(rows: number, cols: number) {
  if (!editorRef.value || rows < 1 || cols < 1) return
  pushUndoState(true)

  let html = '<table>'
  for (let r = 0; r < rows; r++) {
    html += '<tr>'
    for (let c = 0; c < cols; c++) {
      const tag = r === 0 ? 'th' : 'td'
      html += `<${tag}>${r === 0 ? 'Header' : '<br>'}</${tag}>`
    }
    html += '</tr>'
  }
  html += '</table><p><br></p>'

  const sel = window.getSelection()
  if (sel && sel.rangeCount > 0) {
    const range = sel.getRangeAt(0)
    range.deleteContents()
    const tmp = document.createElement('div')
    tmp.innerHTML = html
    const frag = document.createDocumentFragment()
    let lastNode: Node | null = null
    while (tmp.firstChild) {
      lastNode = frag.appendChild(tmp.firstChild)
    }
    range.insertNode(frag)
    if (lastNode) {
      range.setStartAfter(lastNode)
      range.collapse(true)
      sel.removeAllRanges()
      sel.addRange(range)
    }
  } else {
    document.execCommand('insertHTML', false, html)
  }

  focus()
  onInput()
  nextTick(() => pushCurrentState())
}

const tableSelection = ref<{
  table: HTMLTableElement | null
  type: 'row' | 'col' | null
  indices: Set<number>
}>({ table: null, type: null, indices: new Set() })

let tableSelectionAnchor: number | null = null

function clearTableSelection() {
  if (tableSelection.value.table) {
    tableSelection.value.table.querySelectorAll('.table-cell-selected').forEach(el => {
      el.classList.remove('table-cell-selected')
    })
  }
  tableSelection.value = { table: null, type: null, indices: new Set() }
  tableSelectionAnchor = null
}

function applyTableSelectionHighlight() {
  const { table, type, indices } = tableSelection.value
  if (!table) return
  table.querySelectorAll('.table-cell-selected').forEach(el => {
    el.classList.remove('table-cell-selected')
  })
  if (type === 'row') {
    for (const rowIdx of indices) {
      if (rowIdx < table.rows.length) {
        Array.from(table.rows[rowIdx].cells).forEach(cell => {
          cell.classList.add('table-cell-selected')
        })
      }
    }
  } else if (type === 'col') {
    for (let r = 0; r < table.rows.length; r++) {
      for (const colIdx of indices) {
        if (colIdx < table.rows[r].cells.length) {
          table.rows[r].cells[colIdx].classList.add('table-cell-selected')
        }
      }
    }
  }
}

function selectTableRow(table: HTMLTableElement, rowIdx: number, extend: boolean) {
  if (extend && tableSelection.value.table === table && tableSelection.value.type === 'row') {
    if (tableSelectionAnchor !== null) {
      const start = Math.min(tableSelectionAnchor, rowIdx)
      const end = Math.max(tableSelectionAnchor, rowIdx)
      const newIndices = new Set<number>()
      for (let i = start; i <= end; i++) newIndices.add(i)
      tableSelection.value = { table, type: 'row', indices: newIndices }
    } else {
      tableSelection.value.indices.add(rowIdx)
    }
  } else {
    tableSelectionAnchor = rowIdx
    tableSelection.value = { table, type: 'row', indices: new Set([rowIdx]) }
  }
  applyTableSelectionHighlight()
}

function selectTableCol(table: HTMLTableElement, colIdx: number, extend: boolean) {
  if (extend && tableSelection.value.table === table && tableSelection.value.type === 'col') {
    if (tableSelectionAnchor !== null) {
      const start = Math.min(tableSelectionAnchor, colIdx)
      const end = Math.max(tableSelectionAnchor, colIdx)
      const newIndices = new Set<number>()
      for (let i = start; i <= end; i++) newIndices.add(i)
      tableSelection.value = { table, type: 'col', indices: newIndices }
    } else {
      tableSelection.value.indices.add(colIdx)
    }
  } else {
    tableSelectionAnchor = colIdx
    tableSelection.value = { table, type: 'col', indices: new Set([colIdx]) }
  }
  applyTableSelectionHighlight()
}

function deleteSelectedTableRows() {
  const { table, type, indices } = tableSelection.value
  if (!table || type !== 'row' || indices.size === 0) return
  pushUndoState(true)
  const sorted = Array.from(indices).sort((a, b) => b - a)
  for (const rowIdx of sorted) {
    if (table.rows.length <= 1) {
      table.remove()
      clearTableSelection()
      onInput()
      nextTick(() => pushCurrentState())
      return
    }
    table.deleteRow(rowIdx)
  }
  clearTableSelection()
  onInput()
  nextTick(() => pushCurrentState())
}

function deleteSelectedTableCols() {
  const { table, type, indices } = tableSelection.value
  if (!table || type !== 'col' || indices.size === 0) return
  pushUndoState(true)
  const colCount = table.rows[0]?.cells.length || 0
  if (indices.size >= colCount) {
    table.remove()
    clearTableSelection()
    onInput()
    nextTick(() => pushCurrentState())
    return
  }
  const sorted = Array.from(indices).sort((a, b) => b - a)
  for (const colIdx of sorted) {
    for (let r = 0; r < table.rows.length; r++) {
      if (colIdx < table.rows[r].cells.length) {
        table.rows[r].deleteCell(colIdx)
      }
    }
  }
  clearTableSelection()
  onInput()
  nextTick(() => pushCurrentState())
}

function deleteSelectedTableRowsOrCols() {
  const { type } = tableSelection.value
  if (type === 'row') deleteSelectedTableRows()
  else if (type === 'col') deleteSelectedTableCols()
}

const hasSelectedRows = computed(() => tableSelection.value.type === 'row' && tableSelection.value.indices.size > 0)
const hasSelectedCols = computed(() => tableSelection.value.type === 'col' && tableSelection.value.indices.size > 0)

const tableCursorClass = computed(() => {
  if (tableCursor.value === 'select-row') return 'table-cursor-select-row'
  if (tableCursor.value === 'select-col') return 'table-cursor-select-col'
  if (tableCursor.value === 'add-row') return 'table-cursor-add-row'
  if (tableCursor.value === 'add-col') return 'table-cursor-add-col'
  return ''
})
const tableCursor = ref<'none' | 'select-row' | 'select-col' | 'add-row' | 'add-col'>('none')

const tableEdgeButton = ref<{
  visible: boolean
  x: number
  y: number
  type: 'row' | 'col'
  table: HTMLTableElement | null
  index: number
  insertBefore: boolean
}>({ visible: false, x: 0, y: 0, type: 'row', table: null, index: 0, insertBefore: false })

const SHOW_THRESHOLD = 8
const STICKY_THRESHOLD = 28

function hideTableEdgeButton() {
  tableEdgeButton.value.visible = false
  tableCursor.value = 'none'
}

function onTableMouseLeave(ev: MouseEvent) {
  // Don't hide if mouse moved to the + button itself (stops flickering)
  const related = ev.relatedTarget as HTMLElement | null
  if (related && related.closest('.table-edge-btn')) return
  hideTableEdgeButton()
}

function handleTableEdgeInsert() {
  const { type, table, index, insertBefore } = tableEdgeButton.value
  if (!table) return
  hideTableEdgeButton()

  if (type === 'row') {
    insertRowAt(table, index, insertBefore)
  } else {
    insertColAt(table, index, insertBefore)
  }
}

function onTableMouseMove(ev: MouseEvent) {
  const editor = editorRef.value
  if (!editor) return
  const edgeThreshold = 10
  let table = (ev.target as HTMLElement).closest('table') as HTMLTableElement | null
  if (!table || !editor.contains(table)) {
    const tables = editor.querySelectorAll('table')
    for (const t of tables) {
      const r = t.getBoundingClientRect()
      if (ev.clientX >= r.left - edgeThreshold && ev.clientX <= r.right + edgeThreshold && ev.clientY >= r.top - edgeThreshold && ev.clientY <= r.bottom + edgeThreshold) {
        table = t
        break
      }
    }
    if (!table) {
      if (tableEdgeButton.value.visible) hideTableEdgeButton()
      return
    }
  }

  const tableRect = table.getBoundingClientRect()
  const mouseX = ev.clientX
  const mouseY = ev.clientY
  const rows = Array.from(table.rows)
  if (rows.length === 0) return

  if (rows[0] && rows[0].cells.length > 0) {
    const firstRowFirstCell = rows[0].cells[0].getBoundingClientRect()
    const firstRowLastCell = rows[0].cells[rows[0].cells.length - 1].getBoundingClientRect()
    const lastRowFirstCell = rows[rows.length - 1].cells[0]?.getBoundingClientRect()
    if (mouseX >= firstRowFirstCell.left - edgeThreshold && mouseX <= firstRowLastCell.right + edgeThreshold && mouseY < firstRowFirstCell.top && mouseY >= firstRowFirstCell.top - edgeThreshold) {
      tableCursor.value = 'select-col'
      if (tableEdgeButton.value.visible) tableEdgeButton.value.visible = false
      return
    }
    if (lastRowFirstCell && mouseY >= firstRowFirstCell.top - edgeThreshold && mouseY <= lastRowFirstCell.bottom + edgeThreshold && mouseX < firstRowFirstCell.left && mouseX >= firstRowFirstCell.left - edgeThreshold) {
      tableCursor.value = 'select-row'
      if (tableEdgeButton.value.visible) tableEdgeButton.value.visible = false
      return
    }
  }

  const minX = tableRect.left - edgeThreshold
  const maxX = tableRect.right + edgeThreshold
  const minY = tableRect.top - edgeThreshold
  const maxY = tableRect.bottom + edgeThreshold
  if (mouseX < minX || mouseX > maxX || mouseY < minY || mouseY > maxY) {
    if (tableEdgeButton.value.visible) hideTableEdgeButton()
    return
  }

  const btn = tableEdgeButton.value
  const isSticky = btn.visible

  const numCols = rows[0].cells.length
  for (let c = 0; c < numCols; c++) {
    let colLeft = Infinity, colRight = -Infinity
    for (const row of rows) {
      if (c < row.cells.length) {
        const cr = row.cells[c].getBoundingClientRect()
        colLeft = Math.min(colLeft, cr.left)
        colRight = Math.max(colRight, cr.right)
      }
    }
    const firstColTop = rows[0].cells[c]?.getBoundingClientRect().top || tableRect.top
    const lastColBottom = rows[rows.length - 1].cells[c]?.getBoundingClientRect().bottom || tableRect.bottom
    const colMidY = firstColTop + (lastColBottom - firstColTop) / 2
    const colThreshold = (isSticky && btn.type === 'col' && btn.index === c) ? STICKY_THRESHOLD : SHOW_THRESHOLD

    if (c > 0 && Math.abs(mouseX - colLeft) < colThreshold && mouseY >= minY && mouseY <= maxY) {
      tableCursor.value = 'add-col'
      tableEdgeButton.value = { visible: true, x: colLeft - 2, y: colMidY, type: 'col', table, index: c, insertBefore: true }
      return
    }
    if (c < numCols - 1 && Math.abs(mouseX - colRight) < colThreshold && mouseY >= minY && mouseY <= maxY) {
      tableCursor.value = 'add-col'
      tableEdgeButton.value = { visible: true, x: colRight + 2, y: colMidY, type: 'col', table, index: c, insertBefore: false }
      return
    }
  }

  for (let i = 0; i < rows.length; i++) {
    const cells = Array.from(rows[i].cells)
    if (cells.length === 0) continue
    const firstCellRect = cells[0].getBoundingClientRect()
    const lastCellRect = cells[cells.length - 1].getBoundingClientRect()
    const rowTop = firstCellRect.top
    const rowBottom = firstCellRect.bottom
    const rowThreshold = (isSticky && btn.type === 'row' && btn.index === i) ? STICKY_THRESHOLD : SHOW_THRESHOLD

    if (i > 0 && Math.abs(mouseY - rowTop) < rowThreshold) {
      tableCursor.value = 'add-row'
      tableEdgeButton.value = { visible: true, x: firstCellRect.left + (lastCellRect.right - firstCellRect.left) / 2, y: rowTop - 2, type: 'row', table, index: i, insertBefore: true }
      return
    }
    if (i < rows.length - 1 && Math.abs(mouseY - rowBottom) < rowThreshold) {
      tableCursor.value = 'add-row'
      tableEdgeButton.value = { visible: true, x: firstCellRect.left + (lastCellRect.right - firstCellRect.left) / 2, y: rowBottom + 2, type: 'row', table, index: i, insertBefore: false }
      return
    }
  }

  // Mouse is over the table but not near any edge: reset any stale cursor
  // style and hide the "+" button so it doesn't linger from a previous edge.
  if (tableEdgeButton.value.visible || tableCursor.value !== 'none') {
    hideTableEdgeButton()
  }
}

function insertRowAt(table: HTMLTableElement, refRowIdx: number, before: boolean) {
  pushUndoState(true)
  const insertIdx = before ? refRowIdx : refRowIdx + 1
  const refRow = table.rows[before ? refRowIdx : refRowIdx]
  const newRow = table.insertRow(insertIdx)
  for (let i = 0; i < refRow.cells.length; i++) {
    const newCell = newRow.insertCell(i)
    newCell.innerHTML = '<br>'
  }
  onInput()
  nextTick(() => pushCurrentState())
}

function insertColAt(table: HTMLTableElement, refColIdx: number, before: boolean) {
  pushUndoState(true)
  const insertIdx = before ? refColIdx : refColIdx + 1
  for (let r = 0; r < table.rows.length; r++) {
    const row = table.rows[r]
    const isFirstRow = r === 0 && row.cells.length > 0 && row.cells[0].tagName === 'TH'
    const newCell = row.insertCell(insertIdx)
    if (isFirstRow) {
      const th = document.createElement('th')
      th.innerHTML = '<br>'
      newCell.replaceWith(th)
    } else {
      newCell.innerHTML = '<br>'
    }
  }
  onInput()
  nextTick(() => pushCurrentState())
}

function onTableMouseDown(ev: MouseEvent) {
  const target = ev.target as HTMLElement
  const editor = editorRef.value
  if (!editor) return
  let table = target.closest('table') as HTMLTableElement | null
  if (!table || !editor.contains(table)) {
    const tables = editor.querySelectorAll('table')
    const edgeThreshold = 10
    for (const t of tables) {
      const r = t.getBoundingClientRect()
      if (ev.clientX >= r.left - edgeThreshold && ev.clientX <= r.right + edgeThreshold && ev.clientY >= r.top - edgeThreshold && ev.clientY <= r.bottom + edgeThreshold) {
        table = t
        break
      }
    }
    if (!table) {
      if (tableSelection.value.table) {
        clearTableSelection()
      }
      return
    }
  }

  const rows = Array.from(table.rows)
  if (rows.length === 0) return

  const edgeThreshold = 8

  // Check for click on left edge (row selection)
  if (rows[0] && rows[0].cells.length > 0) {
    const firstCell = rows[0].cells[0]
    const firstCellRect = firstCell.getBoundingClientRect()
    if (ev.clientX < firstCellRect.left && ev.clientX >= firstCellRect.left - edgeThreshold) {
      for (let r = 0; r < rows.length; r++) {
        const rowRect = rows[r].cells[0].getBoundingClientRect()
        if (ev.clientY >= rowRect.top && ev.clientY <= rowRect.bottom) {
          ev.preventDefault()
          selectTableRow(table, r, ev.shiftKey)
          const scrollTop = editorRef.value!.scrollTop
          editorRef.value?.focus()
          editorRef.value!.scrollTop = scrollTop
          return
        }
      }
    }
  }

  // Check for click on top edge (column selection)
  if (rows[0] && rows[0].cells.length > 0) {
    const firstCell = rows[0].cells[0]
    const firstCellRect = firstCell.getBoundingClientRect()
    if (ev.clientY < firstCellRect.top && ev.clientY >= firstCellRect.top - edgeThreshold) {
      for (let c = 0; c < rows[0].cells.length; c++) {
        const cellRect = rows[0].cells[c].getBoundingClientRect()
        if (ev.clientX >= cellRect.left && ev.clientX <= cellRect.right) {
          ev.preventDefault()
          selectTableCol(table, c, ev.shiftKey)
          const scrollTop = editorRef.value!.scrollTop
          editorRef.value?.focus()
          editorRef.value!.scrollTop = scrollTop
          return
        }
      }
    }
  }

  if (tableSelection.value.table) {
    clearTableSelection()
  }
}

const resolvedImageUrls = new Map<string, string>()

async function processImagesInEditor() {
  if (!editorRef.value || !props.imageResolver) return
  const imgs = editorRef.value.querySelectorAll('img') as NodeListOf<HTMLImageElement>
  for (const img of imgs) {
    const originalSrc = img.getAttribute('data-original-src') || img.getAttribute('src') || ''
    if (!originalSrc) continue
    if (!img.hasAttribute('data-original-src')) {
      img.setAttribute('data-original-src', originalSrc)
    }
    if (originalSrc.startsWith('blob:') || originalSrc.startsWith('data:')) continue
    if (resolvedImageUrls.has(originalSrc)) {
      img.src = resolvedImageUrls.get(originalSrc)!
    } else {
      try {
        const resolved = await props.imageResolver(originalSrc)
        resolvedImageUrls.set(originalSrc, resolved)
        img.src = resolved
      } catch { /* keep original src */ }
    }
  }
}

const resolvedMediaUrls = new Map<string, string>()

async function processMediaInEditor() {
  if (!editorRef.value || !props.mediaResolver) return
  const media = editorRef.value.querySelectorAll('audio, video') as NodeListOf<HTMLMediaElement>
  for (const el of media) {
    // Element without its own src: fall back to the first <source src>
    // child so pasted <video><source> shapes still render.
    let sourceEl: HTMLSourceElement | null = null
    let originalSrc = el.getAttribute('data-original-src') || el.getAttribute('src') || ''
    if (!originalSrc) {
      sourceEl = el.querySelector('source[src]') as HTMLSourceElement | null
      originalSrc = sourceEl
        ? (sourceEl.getAttribute('data-original-src') || sourceEl.getAttribute('src') || '')
        : ''
    }
    if (!originalSrc) continue
    if (!el.hasAttribute('data-original-src')) {
      el.setAttribute('data-original-src', originalSrc)
    }
    if (originalSrc.startsWith('blob:') || originalSrc.startsWith('data:')) continue
    // Already-signed URLs (renderMarkdownToHtml rewrote them) play directly;
    // resolving them again would download the whole media into a blob URL
    // (up to 200MB per file) — skip.
    if (originalSrc.startsWith('/api/files/')) continue
    let resolved = resolvedMediaUrls.get(originalSrc)
    if (!resolved) {
      try {
        resolved = await props.mediaResolver(originalSrc)
        resolvedMediaUrls.set(originalSrc, resolved)
      } catch { /* keep original src */ }
    }
    if (!resolved || resolved === originalSrc) continue
    if (sourceEl) {
      sourceEl.src = resolved
      if (!sourceEl.hasAttribute('data-original-src')) {
        sourceEl.setAttribute('data-original-src', originalSrc)
      }
    } else {
      el.src = resolved
    }
  }
}

function insertMedia(kind: 'audio' | 'video', src: string, name = '') {
  if (!editorRef.value) return
  pushUndoState(true)
  const attrs = kind === 'video' ? ' controls playsinline' : ' controls'
  const html = `<${kind}${attrs} src="${src}" data-original-src="${src}"></${kind}>`
  let inserted = false
  if (savedEditorSelection) {
    restoreEditorSelection()
  }
  const sel = window.getSelection()
  if (sel && sel.rangeCount > 0 && editorRef.value.contains(sel.anchorNode)) {
    const range = sel.getRangeAt(0)
    range.deleteContents()
    const tmp = document.createElement('div')
    tmp.innerHTML = html
    const frag = document.createDocumentFragment()
    let lastNode: Node | null = null
    while (tmp.firstChild) {
      lastNode = frag.appendChild(tmp.firstChild)
    }
    range.insertNode(frag)
    if (lastNode) {
      range.setStartAfter(lastNode)
      range.collapse(true)
      sel.removeAllRanges()
      sel.addRange(range)
    }
    inserted = true
  }
  if (!inserted) {
    editorRef.value.focus()
    editorRef.value.appendChild(document.createElement('br'))
    const tmp = document.createElement('div')
    tmp.innerHTML = html
    while (tmp.firstChild) {
      editorRef.value.appendChild(tmp.firstChild)
    }
  }
  onInput()
  nextTick(() => {
    pushCurrentState()
    processMediaInEditor()
  })
}

function insertImage(src: string, alt = '') {
  if (!editorRef.value) return
  pushUndoState(true)
  const html = `<img src="${src}" data-original-src="${src}" alt="${alt}">`
  let inserted = false
  if (savedEditorSelection) {
    restoreEditorSelection()
  }
  const sel = window.getSelection()
  if (sel && sel.rangeCount > 0 && editorRef.value.contains(sel.anchorNode)) {
    const range = sel.getRangeAt(0)
    range.deleteContents()
    const tmp = document.createElement('div')
    tmp.innerHTML = html
    const frag = document.createDocumentFragment()
    let lastNode: Node | null = null
    while (tmp.firstChild) {
      lastNode = frag.appendChild(tmp.firstChild)
    }
    range.insertNode(frag)
    if (lastNode) {
      range.setStartAfter(lastNode)
      range.collapse(true)
      sel.removeAllRanges()
      sel.addRange(range)
    }
    inserted = true
  }
  if (!inserted) {
    editorRef.value.focus()
    editorRef.value.appendChild(document.createElement('br'))
    const tmp = document.createElement('div')
    tmp.innerHTML = html
    while (tmp.firstChild) {
      editorRef.value.appendChild(tmp.firstChild)
    }
  }
  onInput()
  nextTick(() => {
    pushCurrentState()
    processImagesInEditor()
  })
}

function setImageAlignment(img: HTMLImageElement, align: 'left' | 'center' | 'right' | 'none') {
  if (!img) return
  pushUndoState(true)
  const w = img.style.width
  const h = img.style.height
  img.style.cssText = w ? `width:${w};height:${h};` : ''
  if (align === 'none') {
    img.removeAttribute('align')
  } else if (align === 'center') {
    img.setAttribute('align', 'center')
    img.style.display = 'block'
    img.style.margin = '8px auto'
  } else {
    img.setAttribute('align', align)
    img.style.float = align
    img.style.margin = align === 'left' ? '8px 12px 8px 0' : '8px 0 8px 12px'
    img.style.maxWidth = '50%'
  }
  if (imageResizeState.value.visible && imageResizeState.value.img === img) {
    nextTick(() => updateImageResizeOverlay())
  }
  onInput(true)
  nextTick(() => pushCurrentState())
}

const imageResizeState = ref<{
  visible: boolean
  img: HTMLImageElement | null
  x: number
  y: number
  width: number
  height: number
  naturalWidth: number
  naturalHeight: number
}>({
  visible: false,
  img: null,
  x: 0,
  y: 0,
  width: 0,
  height: 0,
  naturalWidth: 0,
  naturalHeight: 0,
})

let isResizingImage = false
let resizeStartX = 0
let resizeStartY = 0
let resizeStartWidth = 0
let resizeStartHeight = 0
let resizeAspectRatio = 1

function updateImageResizeOverlay() {
  const img = imageResizeState.value.img
  if (!img || !editorRef.value) return
  const rect = img.getBoundingClientRect()
  imageResizeState.value.x = rect.left
  imageResizeState.value.y = rect.top
  imageResizeState.value.width = rect.width
  imageResizeState.value.height = rect.height
}

function selectImageForResize(img: HTMLImageElement) {
  const rect = img.getBoundingClientRect()
  imageResizeState.value = {
    visible: true,
    img,
    x: rect.left,
    y: rect.top,
    width: rect.width,
    height: rect.height,
    naturalWidth: img.naturalWidth || rect.width,
    naturalHeight: img.naturalHeight || rect.height,
  }
  resizeAspectRatio = (img.naturalWidth || rect.width) / (img.naturalHeight || rect.height)
}

function deselectImage() {
  imageResizeState.value.visible = false
  imageResizeState.value.img = null
}

function onImageResizeMouseDown(ev: MouseEvent, _corner: string) {
  ev.preventDefault()
  ev.stopPropagation()
  const img = imageResizeState.value.img
  if (!img) return
  isResizingImage = true
  resizeStartX = ev.clientX
  resizeStartY = ev.clientY
  resizeStartWidth = img.offsetWidth
  resizeStartHeight = img.offsetHeight
  pushUndoState(true)
  document.addEventListener('mousemove', onImageResizeMouseMove)
  document.addEventListener('mouseup', onImageResizeMouseUp)
}

function onImageResizeMouseMove(ev: MouseEvent) {
  if (!isResizingImage || !imageResizeState.value.img) return
  const img = imageResizeState.value.img
  const dx = ev.clientX - resizeStartX
  const dy = ev.clientY - resizeStartY
  const delta = Math.abs(dx) > Math.abs(dy) ? dx : dy
  let newWidth = Math.max(40, resizeStartWidth + delta)
  const editorWidth = editorRef.value?.clientWidth || 800
  newWidth = Math.min(newWidth, editorWidth - 48)
  const newHeight = newWidth / resizeAspectRatio
  img.style.width = `${newWidth}px`
  img.style.height = `${newHeight}px`
  img.removeAttribute('width')
  img.removeAttribute('height')
  updateImageResizeOverlay()
}

function onImageResizeMouseUp() {
  isResizingImage = false
  document.removeEventListener('mousemove', onImageResizeMouseMove)
  document.removeEventListener('mouseup', onImageResizeMouseUp)
  onInput()
  nextTick(() => pushCurrentState())
}

watch(() => props.modelValue, async (newValue) => {
  if (skipNextModelRender) {
    return
  }
  if (newValue === lastEmittedMarkdown && hasEverEmittedMarkdown) {
    lastEmittedMarkdown = ''
    hasEverEmittedMarkdown = false
    return
  }
  lastEmittedMarkdown = ''
  hasEverEmittedMarkdown = false
  isExternalContentUpdate = true
  if (imageResizeState.value.visible) {
    deselectImage()
  }
  if (editorRef.value) {
    suppressInputDepth.value++
    suppressChangeEmit = true
    try {
      renderedContent.value = renderMarkdownToHtml(newValue || '')
      await nextTick()
      if (editorRef.value) {
        await renderMermaidBlocks(editorRef.value)
        await renderEchartsBlocks(editorRef.value)
        await processImagesInEditor()
        await processMediaInEditor()
      }
    } finally {
      suppressInputDepth.value--
      suppressChangeEmit = false
    }
  }
  setTimeout(() => {
    isExternalContentUpdate = false
    if (editorRef.value && undoStack.value.length === 0) {
      pushUndoState(true)
    }
  }, 350)
}, { immediate: true })

// Also render mermaid on mount
onMounted(async () => {
  await nextTick()
  if (editorRef.value) {
    suppressInputDepth.value++
    try {
      await renderMermaidBlocks(editorRef.value)
        await renderEchartsBlocks(editorRef.value)
    } finally {
      suppressInputDepth.value--
    }
    editorRef.value.addEventListener('keydown', onKeydown)
    editorRef.value.addEventListener('mousemove', onTableMouseMove)
    editorRef.value.addEventListener('mouseleave', onTableMouseLeave)
    editorRef.value.addEventListener('scroll', onEditorScroll)
    editorRef.value.addEventListener('compositionstart', onCompositionStart)
    editorRef.value.addEventListener('compositionend', onCompositionEnd)
  }
  // Expose for testing via DOM element (more reliable in Playwright)
  if (editorRef.value) {
    const el = editorRef.value as any
    el.__undo = undo
    el.__redo = redo
    el.__getUndoStackSize = () => undoStack.value.length
    el.__pushUndoState = pushUndoState
    el.__applyHighlight = applyHighlightToSelection
  }
})

onUnmounted(() => {
  if (editorRef.value) {
    editorRef.value.removeEventListener('keydown', onKeydown)
    editorRef.value.removeEventListener('mousemove', onTableMouseMove)
    editorRef.value.removeEventListener('mouseleave', onTableMouseLeave)
    editorRef.value.removeEventListener('scroll', onEditorScroll)
    editorRef.value.removeEventListener('compositionstart', onCompositionStart)
    editorRef.value.removeEventListener('compositionend', onCompositionEnd)
  }
  if (inputDebounceTimer) {
    clearTimeout(inputDebounceTimer)
    inputDebounceTimer = null
  }
  closeTableContextMenu()
  document.removeEventListener('mousemove', onImageResizeMouseMove)
  document.removeEventListener('mouseup', onImageResizeMouseUp)
})

function scrollPreviewToElement(container: HTMLElement, element: HTMLElement) {
  const containerRect = container.getBoundingClientRect()
  const targetRect = element.getBoundingClientRect()
  const scrollMarginTop = Number.parseFloat(getComputedStyle(element).scrollMarginTop || '0') || 0
  const nextTop = container.scrollTop + (targetRect.top - containerRect.top) - scrollMarginTop
  container.scrollTo({
    top: Math.max(0, nextTop),
    behavior: 'smooth',
  })
}

function onClick(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null
  const root = editorRef.value
  if (!target || !root) return

  if (target.tagName === 'IMG' && root.contains(target)) {
    selectImageForResize(target as HTMLImageElement)
    return
  }
  if (imageResizeState.value.visible) {
    deselectImage()
  }

  // Handle anchor link clicks (#hash)
  const anchor = target.closest?.('a[href^="#"]') as HTMLAnchorElement | null
  if (anchor && root.contains(anchor)) {
    const hash = anchor.getAttribute('href') || ''
    const id = hash.replace(/^#/, '')
    if (id) {
      try {
        const decoded = decodeURIComponent(id)
        const el = root.querySelector(`#${CSS.escape(decoded)}`) as HTMLElement | null
        if (el) {
          ev.preventDefault()
          scrollPreviewToElement(root, el)
          try { history.replaceState(null, '', `#${decoded}`) } catch { /* ignore */ }
          return
        }
      } catch {
        const el = root.querySelector(`#${CSS.escape(id)}`) as HTMLElement | null
        if (el) {
          ev.preventDefault()
          scrollPreviewToElement(root, el)
          try { history.replaceState(null, '', `#${id}`) } catch { /* ignore */ }
          return
        }
      }
    }
  }

  // Handle direct heading clicks
  let node: HTMLElement | null = target
  while (node && node !== root && !/^H[1-6]$/.test(node.tagName)) {
    node = node.parentElement
  }
  if (!node || node === root || !/^H[1-6]$/.test(node.tagName)) return
  const id = node.id
  if (!id) return
  ev.preventDefault()
  scrollPreviewToElement(root, node)
  try {
    history.replaceState(null, '', `#${id}`)
  } catch { /* ignore */ }
}

function getCleanHtml(): string {
  if (!editorRef.value) return ''
  // Clone the editor DOM to extract clean HTML without find highlights
  const clone = editorRef.value.cloneNode(true) as HTMLElement
  const marks = clone.querySelectorAll('[data-find-match="true"]')
  marks.forEach(mark => {
    const parent = mark.parentNode
    if (!parent) return
    while (mark.firstChild) {
      parent.insertBefore(mark.firstChild, mark)
    }
    parent.removeChild(mark)
    if (parent.normalize) parent.normalize()
  })
  return clone.innerHTML
}

// Handle input events
let pendingSerialize: Promise<void> | null = null

// The DOM→markdown serialize walks the entire editor DOM, which makes typing
// sluggish on large notes. Defer it to a microtask so keystrokes stay
// responsive; multiple rapid inputs coalesce into one serialize. Consumers
// that READ the serialized value (save, autosave, exit-confirm) MUST await
// flushPendingSerialization() first so the emitted value is not stale.
function scheduleSerialize(force: boolean) {
  if (pendingSerialize) return
  pendingSerialize = Promise.resolve().then(() => {
    pendingSerialize = null
    if (!editorRef.value) return

    // Convert HTML to markdown WITHOUT removing highlights from the actual DOM.
    // Uses clone to get clean HTML so highlights stay visible in the editor.
    const cleanHtml = getCleanHtml()
    const markdown = htmlToMarkdown(cleanHtml)

    // Skip if content hasn't actually changed (prevents feedback loops)
    if (!force && markdown === props.modelValue) return

    // Track this as an editor-initiated change to avoid unnecessary re-renders
    lastEmittedMarkdown = markdown
    hasEverEmittedMarkdown = true
    emit('update:modelValue', markdown)
    if (!suppressChangeEmit) {
      emit('change')
    }
  })
}

async function flushPendingSerialization(): Promise<void> {
  const pending = pendingSerialize
  if (pending) {
    try {
      await pending
    } catch { /* the serialize callback never rejects */ }
  }
}

function onInput(force = false) {
  if (!editorRef.value || suppressInputDepth.value > 0) return

  // DOM-derived undo snapshots and find highlights stay synchronous; only the
  // serialization is deferred.
  if (!isUndoing && !isExternalContentUpdate && undoStack.value.length === 0) {
    pushUndoState()
  }
  if (!isUndoing && !isExternalContentUpdate) {
    scheduleInputUndo()
  }

  // Update highlights to reflect any content changes while find bar is active
  if (props.findActive && props.findQuery) {
    updateFindHighlights()
  }

  scheduleSerialize(!!force)
}

// Find/replace highlight functions
function removeFindHighlights() {
  if (!editorRef.value) return
  suppressInputDepth.value++
  try {
    const marks = editorRef.value.querySelectorAll('[data-find-match="true"]')
    marks.forEach(mark => {
      const parent = mark.parentNode
      if (!parent) return
      while (mark.firstChild) {
        parent.insertBefore(mark.firstChild, mark)
      }
      parent.removeChild(mark)
      if (parent.normalize) parent.normalize()
    })
  } finally {
    suppressInputDepth.value--
  }
}

function updateFindHighlights() {
  if (!editorRef.value || !props.findActive || !props.findQuery) {
    removeFindHighlights()
    return
  }

  // Suppress input events while we modify DOM for highlights
  // to prevent onInput from removing the highlights we just applied
  suppressInputDepth.value++
  try {
    removeFindHighlights()

    const query = props.findQuery
    const walker = document.createTreeWalker(editorRef.value, NodeFilter.SHOW_TEXT)
    interface TextMatch { node: Text; startOffset: number; endOffset: number }
    const matches: TextMatch[] = []
    while (walker.nextNode()) {
      const node = walker.currentNode as Text
      const text = node.textContent || ''
      let idx = 0
      while (idx < text.length) {
        const found = text.toLowerCase().indexOf(query.toLowerCase(), idx)
        if (found === -1) break
        matches.push({ node, startOffset: found, endOffset: found + query.length })
        idx = found + query.length
      }
    }

    // Process from last to first to avoid index shifts
    const currentIdx = props.findCurrentIndex || 0
    for (let i = matches.length - 1; i >= 0; i--) {
      const match = matches[i]
      const range = document.createRange()
      range.setStart(match.node, match.startOffset)
      range.setEnd(match.node, match.endOffset)

      const mark = document.createElement('span')
      mark.className = i === currentIdx ? 'find-match-current' : 'find-match'
      mark.dataset.findMatch = 'true'

      try {
        range.surroundContents(mark)
      } catch {
        // Skip if can't surround (crosses element boundary)
      }
    }

    // Scroll current match into view
    nextTick(() => {
      const currentMark = editorRef.value?.querySelector('.find-match-current') as HTMLElement | null
      if (currentMark) {
        currentMark.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }
    })
  } finally {
    suppressInputDepth.value--
  }
}

watch(() => [props.findQuery, props.findActive, props.findCurrentIndex], () => {
  updateFindHighlights()
}, { flush: 'post' })

function setSelectionByTextOffset(startOffset: number, endOffset: number) {
  if (!editorRef.value) return
  let currentOffset = 0
  const walker = document.createTreeWalker(editorRef.value, NodeFilter.SHOW_TEXT)
  let startNode: Node | null = null
  let startNodeOffset = 0
  let endNode: Node | null = null
  let endNodeOffset = 0

  while (walker.nextNode()) {
    const node = walker.currentNode
    const len = node.textContent?.length || 0

    if (!startNode && currentOffset + len >= startOffset) {
      startNode = node
      startNodeOffset = startOffset - currentOffset
    }
    if (!endNode && currentOffset + len >= endOffset) {
      endNode = node
      endNodeOffset = endOffset - currentOffset
      break
    }
    currentOffset += len
  }

  if (startNode && endNode) {
    const range = document.createRange()
    range.setStart(startNode, startNodeOffset)
    range.setEnd(endNode, endNodeOffset)
    const sel = window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
    // Scroll into view
    const rect = range.getBoundingClientRect()
    const editorRect = editorRef.value.getBoundingClientRect()
    if (rect.top < editorRect.top || rect.bottom > editorRect.bottom) {
      const scrollTop = editorRef.value.scrollTop + (rect.top - editorRect.top) - editorRect.height / 2
      editorRef.value.scrollTo({ top: Math.max(0, scrollTop), behavior: 'smooth' })
    }
  }
}

function replaceTextRange(startOffset: number, endOffset: number, replacement: string) {
  if (!editorRef.value) return false
  pushUndoState(true)
  setSelectionByTextOffset(startOffset, endOffset)
  const sel = window.getSelection()
  if (sel && sel.rangeCount > 0) {
    const range = sel.getRangeAt(0)
    range.deleteContents()
    range.insertNode(document.createTextNode(replacement))
    range.collapse(false)
    onInput()
    nextTick(() => pushCurrentState())
    return true
  }
  return false
}

function getTextContent(): string {
  return editorRef.value?.textContent || ''
}

function findCurrentListItem(): HTMLLIElement | null {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorRef.value) return null
  let node: Node | null = sel.getRangeAt(0).startContainer
  while (node && node !== editorRef.value) {
    if (node.nodeType === Node.ELEMENT_NODE && (node as HTMLElement).tagName === 'LI') {
      return node as HTMLLIElement
    }
    node = node.parentNode
  }
  return null
}

function indentListItem(li: HTMLLIElement) {
  const list = li.parentElement
  if (!list || !editorRef.value) return
  const prevLi = li.previousElementSibling as HTMLElement | null
  if (!prevLi || prevLi.tagName !== 'LI') {
    pushUndoState(true)
    const currentMargin = parseInt(li.style.marginLeft || '0', 10)
    li.style.marginLeft = `${currentMargin + 40}px`
    nextTick(() => {
      if (editorRef.value) {
        skipNextModelRender = true
        const md = htmlToMarkdown(getCleanHtml())
        lastEmittedMarkdown = md
        hasEverEmittedMarkdown = true
        emit('update:modelValue', md)
        emit('change')
      }
      nextTick(() => {
        skipNextModelRender = false
        pushCurrentState()
      })
    })
    return
  }

  pushUndoState(true)

  const sel = window.getSelection()
  const saved = sel && sel.rangeCount > 0 ? saveSelection(editorRef.value) : null

  const listTag = list.tagName.toLowerCase()
  let subList = prevLi.querySelector(`:scope > ${listTag}`) as HTMLElement | null
  if (!subList) {
    subList = document.createElement(listTag)
    prevLi.appendChild(subList)
  }
  subList.appendChild(li)

  if (listTag === 'ol') {
    updateOlNumbering(list as HTMLOListElement)
    updateOlNumbering(subList as HTMLOListElement)
  }

  if (saved) {
    nextTick(() => {
      if (editorRef.value) restoreSelection(editorRef.value, saved)
    })
  }
  nextTick(() => {
    if (editorRef.value) {
      skipNextModelRender = true
      const md = htmlToMarkdown(getCleanHtml())
      lastEmittedMarkdown = md
      hasEverEmittedMarkdown = true
      emit('update:modelValue', md)
      emit('change')
    }
    nextTick(() => {
      skipNextModelRender = false
      pushCurrentState()
    })
  })
}

function outdentListItem(li: HTMLLIElement) {
  const nestedList = li.parentElement
  if (!nestedList || !editorRef.value) return
  const parentLi = nestedList.parentElement
  if (!parentLi || parentLi.tagName !== 'LI') {
    const currentMargin = parseInt(li.style.marginLeft || '0', 10)
    if (currentMargin > 0) {
      pushUndoState(true)
      const nv = Math.max(0, currentMargin - 40)
      li.style.marginLeft = nv > 0 ? `${nv}px` : ''
      nextTick(() => {
        if (editorRef.value) {
          skipNextModelRender = true
          const md = htmlToMarkdown(getCleanHtml())
          lastEmittedMarkdown = md
          hasEverEmittedMarkdown = true
          emit('update:modelValue', md)
          emit('change')
        }
        nextTick(() => {
          skipNextModelRender = false
          pushCurrentState()
        })
      })
    }
    return
  }
  const outerList = parentLi.parentElement
  if (!outerList) return

  pushUndoState(true)

  const sel = window.getSelection()
  const saved = sel && sel.rangeCount > 0 ? saveSelection(editorRef.value) : null

  const refNode = parentLi.nextElementSibling
  if (refNode) {
    outerList.insertBefore(li, refNode)
  } else {
    outerList.appendChild(li)
  }

  if (nestedList.children.length === 0) {
    nestedList.remove()
    if (parentLi.childNodes.length === 0) {
      parentLi.appendChild(document.createElement('br'))
    }
  }

  if (outerList.tagName.toLowerCase() === 'ol') {
    updateOlNumbering(outerList as HTMLOListElement)
  }
  if (nestedList.children.length > 0 && nestedList.tagName.toLowerCase() === 'ol') {
    updateOlNumbering(nestedList as HTMLOListElement)
  }

  if (saved) {
    nextTick(() => {
      if (editorRef.value) restoreSelection(editorRef.value, saved)
    })
  }
  nextTick(() => {
    if (editorRef.value) {
      skipNextModelRender = true
      const md = htmlToMarkdown(getCleanHtml())
      lastEmittedMarkdown = md
      hasEverEmittedMarkdown = true
      emit('update:modelValue', md)
      emit('change')
    }
    nextTick(() => {
      skipNextModelRender = false
      pushCurrentState()
    })
  })
}

function updateOlNumbering(ol: HTMLOListElement | null) {
  if (!ol) return
  let idx = 1
  Array.from(ol.children).forEach((child) => {
    if (child.tagName === 'LI') {
      child.setAttribute('value', String(idx++))
    }
  })
}

function tryAutoNumberedList(): boolean {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorRef.value) return false
  let blockNode: Node | null = sel.anchorNode
  while (blockNode && blockNode !== editorRef.value) {
    if (blockNode.nodeType === Node.ELEMENT_NODE) {
      const tag = (blockNode as HTMLElement).tagName
      if (['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6'].includes(tag)) break
    }
    blockNode = blockNode.parentNode
  }
  if (!blockNode || blockNode === editorRef.value) return false
  const block = blockNode as HTMLElement
  const text = block.textContent || ''
  const match = text.match(/^(\d+)\.\s?$/)
  if (!match) return false
  const anchorNode = sel.anchorNode
  const anchorOffset = sel.anchorOffset
  if (anchorNode && anchorNode.nodeType === Node.TEXT_NODE) {
    const textBeforeCursor = (anchorNode.textContent || '').substring(0, anchorOffset)
    if (textBeforeCursor !== text) return false
  }
  pushUndoState(true)
  const ol = document.createElement('ol')
  const li = document.createElement('li')
  li.innerHTML = '<br>'
  ol.appendChild(li)
  block.parentNode?.replaceChild(ol, block)
  const range = document.createRange()
  range.setStart(li, 0)
  range.collapse(true)
  sel.removeAllRanges()
  sel.addRange(range)
  nextTick(() => {
    onInput()
    pushCurrentState()
  })
  return true
}

// Handle keydown events for formatting shortcuts
function onKeydown(e: KeyboardEvent) {
  if (!editorRef.value) return

  // Escape to clear multi-selection or table selection
  if (e.key === 'Escape') {
    if (tableSelection.value.table) {
      clearTableSelection()
      e.preventDefault()
      return
    }
    if (multiSelections.value.size > 0) {
      clearMultiSelections()
      e.preventDefault()
      return
    }
  }

  if ((e.key === 'Delete' || e.key === 'Backspace') && tableSelection.value.table && tableSelection.value.indices.size > 0) {
    e.preventDefault()
    deleteSelectedTableRowsOrCols()
    return
  }

  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
    const li = findCurrentListItem()
    if (li) {
      const parentList = li.parentElement
      if (parentList) {
        setTimeout(() => {
          if (!editorRef.value) return
          const allOls = editorRef.value.querySelectorAll('ol')
          allOls.forEach(ol => updateOlNumbering(ol as HTMLOListElement))
        }, 0)
      }
    }
  }

  // Ctrl/Cmd + Z for undo
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !e.shiftKey) {
    e.preventDefault()
    undo()
    return
  }

  // Ctrl/Cmd + Y or Ctrl/Cmd + Shift + Z for redo
  if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === 'y' || (e.key.toLowerCase() === 'z' && e.shiftKey))) {
    e.preventDefault()
    redo()
    return
  }

  // Ctrl/Cmd + B for bold
  if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
    e.preventDefault()
    document.execCommand('bold')
  }

  // Ctrl/Cmd + I for italic
  if ((e.ctrlKey || e.metaKey) && e.key === 'i') {
    e.preventDefault()
    document.execCommand('italic')
  }

  // Ctrl/Cmd + F for find
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault()
    emit('find-request', false)
    return
  }

  // Ctrl/Cmd + H for find & replace
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'h') {
    e.preventDefault()
    emit('find-request', true)
    return
  }

  // Ctrl/Cmd + K for link
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault()
    const url = prompt('Enter URL:')
    if (url) {
      document.execCommand('createLink', false, url)
    }
  }

  if (e.key === ' ' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (tryAutoNumberedList()) {
      e.preventDefault()
      return
    }
  }

  if (e.key === 'Tab') {
    e.preventDefault()
    const li = findCurrentListItem()
    if (li) {
      if (e.shiftKey) {
        outdentListItem(li)
      } else {
        indentListItem(li)
      }
      return
    }
    pushUndoState(true)
    const sel = window.getSelection()
    if (sel && sel.rangeCount > 0 && editorRef.value) {
      let blockNode: Node | null = sel.anchorNode
      while (blockNode && blockNode !== editorRef.value) {
        if (blockNode.nodeType === Node.ELEMENT_NODE) {
          const tag = (blockNode as HTMLElement).tagName
          if (['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6'].includes(tag)) break
        }
        blockNode = blockNode.parentNode
      }
      if ((!blockNode || blockNode === editorRef.value) && sel.anchorNode) {
        const p = document.createElement('p')
        const anchorNode = sel.anchorNode
        if (anchorNode.nodeType === Node.TEXT_NODE) {
          const parent = anchorNode.parentNode
          if (parent && parent !== editorRef.value) {
            parent.insertBefore(p, anchorNode)
            p.appendChild(anchorNode)
          } else if (parent === editorRef.value) {
            editorRef.value.insertBefore(p, anchorNode)
            p.appendChild(anchorNode)
          }
        }
        blockNode = p
      }
      if (blockNode && blockNode !== editorRef.value && blockNode.nodeType === Node.ELEMENT_NODE) {
        const block = blockNode as HTMLElement
        const currentMargin = parseInt(block.style.marginLeft || '0', 10)
        const STEP = 40
        if (e.shiftKey) {
          const nv = Math.max(0, currentMargin - STEP)
          block.style.marginLeft = nv > 0 ? `${nv}px` : ''
        } else {
          block.style.marginLeft = `${currentMargin + STEP}px`
        }
        skipNextModelRender = true
        const md = htmlToMarkdown(getCleanHtml())
        lastEmittedMarkdown = md
        hasEverEmittedMarkdown = true
        emit('update:modelValue', md)
        emit('change')
        nextTick(() => {
          skipNextModelRender = false
          pushCurrentState()
        })
        return
      }
    }
    nextTick(() => {
      onInput()
      pushCurrentState()
    })
  }
}

// Handle paste events - convert markdown to rendered HTML
function onPaste(e: ClipboardEvent) {
  const imageFiles = e.clipboardData?.files
    ? Array.from(e.clipboardData.files).filter(f => f.type.startsWith('image/'))
    : []

  if (imageFiles.length > 0 && props.imageUploader) {
    e.preventDefault()
    for (const file of imageFiles) {
      handleImageFileUpload(file)
    }
    return
  }

  e.preventDefault()

  let html = ''
  const clipHtml = e.clipboardData?.getData('text/html') || ''
  if (clipHtml) {
    const sanitized = sanitizePastedHtml(clipHtml)
    if (sanitized) html = sanitized
  }

  if (!html) {
    const text = e.clipboardData?.getData('text/plain') || ''
    if (!text) return
    html = renderMarkdownToHtml(text)
  }

  pushUndoState(true)

  const sel = window.getSelection()
  if (sel && sel.rangeCount > 0 && editorRef.value) {
    const range = sel.getRangeAt(0)
    range.deleteContents()
    const fragment = range.createContextualFragment(html)
    range.insertNode(fragment)
    range.collapse(false)
    sel.removeAllRanges()
    sel.addRange(range)
  } else {
    document.execCommand('insertHTML', false, html)
  }

  if (editorRef.value) {
    nextTick(async () => {
      if (editorRef.value) {
        suppressInputDepth.value++
        try {
          await renderMermaidBlocks(editorRef.value)
          await renderEchartsBlocks(editorRef.value)
          await processImagesInEditor()
        } finally {
          suppressInputDepth.value--
        }
      }
    })
    // Upload blob:/data: pasted images in the background; re-serializes when
    // done. Chain each upload after the previous one — a single slot would be
    // overwritten by a second paste, orphaning the first batch and letting
    // flushPendingImageUploads() return before it finished.
    trackImageUpload()
  }

  onInput()
}

function sanitizePastedHtml(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  inlinePastedStylesheetRules(doc)
  doc.querySelectorAll('script, style, link, meta, iframe, object, embed, form').forEach(el => el.remove())
  doc.querySelectorAll('*').forEach(el => {
    Array.from(el.attributes).forEach(attr => {
      if (attr.name.startsWith('on')) el.removeAttribute(attr.name)
    })
  })
  return doc.body.innerHTML.trim()
}

// Sources like Word / Google Docs / 飞书 put formatting in <style> blocks +
// class attributes. The serializer only understands inline styles, so class-
// based formatting would be lost entirely on save. Inline the key visual
// properties from stylesheet rules onto matching elements before the <style>
// tags are stripped. Element's own inline styles always win.
const PASTE_INLINE_PROPS = [
  'font-weight',
  'font-style',
  'color',
  'background-color',
  'text-align',
  'text-decoration',
  'text-decoration-line',
] as const

function inlinePastedStylesheetRules(doc: Document) {
  const sheets: CSSStyleSheet[] = []
  doc.querySelectorAll('style').forEach(styleEl => {
    try {
      const sheet = new CSSStyleSheet()
      sheet.replaceSync(styleEl.textContent || '')
      sheets.push(sheet)
    } catch { /* ignore unparseable stylesheets */ }
    styleEl.remove()
  })
  if (sheets.length === 0) return

  const ownInlineProps = new WeakMap<HTMLElement, Set<string>>()
  const getOwnProps = (el: HTMLElement): Set<string> => {
    let s = ownInlineProps.get(el)
    if (!s) {
      s = new Set(PASTE_INLINE_PROPS.filter(p => !!el.style.getPropertyValue(p)))
      ownInlineProps.set(el, s)
    }
    return s
  }

  for (const sheet of sheets) {
    for (const rule of Array.from(sheet.cssRules)) {
      if (!(rule instanceof CSSStyleRule)) continue
      let targets: Element[] = []
      try {
        targets = Array.from(doc.body.querySelectorAll(rule.selectorText))
      } catch { continue }
      targets.forEach(el => {
        const h = el as HTMLElement
        const own = getOwnProps(h)
        PASTE_INLINE_PROPS.forEach(prop => {
          if (own.has(prop)) return
          const v = rule.style.getPropertyValue(prop)
          if (v) h.style.setProperty(prop, v)
        })
      })
    }
  }
}

// Pasted images frequently carry blob:/data: URLs (Word, 微信, screenshots).
// Those are session-local and cannot be persisted — htmlToMarkdown drops
// them, which previously meant pasted images vanished on save. Upload them
// through the provided uploader and swap in the persisted URL.
//
// The first onInput() after a paste fires while uploads are still in flight,
// emitting markdown WITHOUT the images. Saving in that window persists the
// image-less version permanently. Parents must await
// flushPendingImageUploads() before saving.
let pendingImageUpload: Promise<void> | null = null

// Runs uploadPastedImages() chained after any in-flight batch, so the slot
// always tracks the LATEST tail (which awaits all previous batches).
function trackImageUpload() {
  const prev = pendingImageUpload
  const tail = (prev || Promise.resolve()).then(uploadPastedImages, uploadPastedImages)
  pendingImageUpload = tail
  tail.then(() => {
    if (pendingImageUpload === tail) pendingImageUpload = null
  })
}

async function flushPendingImageUploads(): Promise<void> {
  const pending = pendingImageUpload
  if (pending) {
    try {
      // A hung uploader (no timeout on the axios call) must not block saves
      // forever; proceed after 15s — the batch's images are lost in that
      // extreme case, but the note saves.
      await Promise.race([pending, new Promise((resolve) => setTimeout(resolve, 15000))])
    } catch { /* per-image failures are handled in uploadPastedImages */ }
  }
}

async function uploadPastedImages() {
  if (!editorRef.value || !props.imageUploader) return
  const imgs = Array.from(editorRef.value.querySelectorAll('img')).filter((img) => {
    const src = img.getAttribute('src') || ''
    return src.startsWith('blob:') || src.startsWith('data:')
  }) as HTMLImageElement[]
  if (imgs.length === 0) return
  let anyUploaded = false
  let anyFailed = false
  for (const img of imgs) {
    const src = img.getAttribute('src') || ''
    try {
      const res = await fetch(src)
      const blob = await res.blob()
      if (!blob.type.startsWith('image/')) continue
      const ext = (blob.type.split('/')[1] || 'png').split(';')[0].replace('jpeg', 'jpg')
      const file = new File([blob], `pasted-image.${ext}`, { type: blob.type })
      const uploaded = await props.imageUploader(file)
      img.setAttribute('src', uploaded)
      img.setAttribute('data-original-src', uploaded)
      anyUploaded = true
    } catch {
      anyFailed = true
      /* keep original src; unpersistable images are dropped on save */
    }
  }
  if (anyFailed) emit('image-upload-failed')
  if (anyUploaded && editorRef.value) {
    onInput()
    nextTick(() => processImagesInEditor())
  }
}

async function handleImageFileUpload(file: File) {
  if (!props.imageUploader) return
  try {
    const src = await props.imageUploader(file)
    if (src) insertImage(src, file.name)
  } catch (err) {
    console.error('Image upload failed:', err)
  }
}

// Focus the editor
function focus() {
  nextTick(() => {
    if (editorRef.value) {
      editorRef.value.focus()
    }
  })
}

// Execute formatting command
function execCommand(command: string, value?: string) {
  pushUndoState(true)

  if (command === 'formatBlock' && value === 'blockquote') {
    const selection = window.getSelection()
    if (selection && selection.rangeCount > 0 && editorRef.value) {
      const range = selection.getRangeAt(0)
      let node: Node | null = range.commonAncestorContainer
      while (node && node !== editorRef.value) {
        if (node.nodeType === Node.ELEMENT_NODE && (node as HTMLElement).tagName === 'BLOCKQUOTE') {
          const bq = node as HTMLElement
          const parent = bq.parentNode
          if (parent) {
            while (bq.firstChild) {
              parent.insertBefore(bq.firstChild, bq)
            }
            parent.removeChild(bq)
          }
          focus()
          nextTick(() => pushCurrentState())
          return
        }
        node = node.parentNode
      }
    }
  }

  document.execCommand(command, false, value)
  focus()
  nextTick(() => pushCurrentState())
}

// Update math element with new TeX content
async function updateMathElement(element: HTMLElement, newHtml: string) {
  element.outerHTML = newHtml
  await nextTick()
  if (editorRef.value) {
    suppressInputDepth.value++
    try {
      await renderMermaidBlocks(editorRef.value)
        await renderEchartsBlocks(editorRef.value)
    } finally {
      suppressInputDepth.value--
    }
  }
}

function getCurrentHeadingLevel(): string | null {
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0) return null
  const node = selection.rangeCount > 0 ? selection.getRangeAt(0).startContainer : null
  if (!node) return null
  const root = editorRef.value
  if (!root) return null

  let el: Node | null = node
  while (el && el !== root) {
    if (el.nodeType === Node.ELEMENT_NODE) {
      const tag = (el as HTMLElement).tagName
      if (/^H[1-6]$/.test(tag)) {
        return tag.toLowerCase()
      }
    }
    el = el.parentNode
  }
  return null
}

const HEADING_TYPO_PROPS = ['font-size', 'font-family', 'font-weight', 'line-height', 'letter-spacing'] as const

// After formatBlock promotes a block to a heading, any inline typography
// (font-size/font-family/font-weight/line-height) carried over from the
// original text would make this heading render differently from other
// headings of the same level. Strip those properties (recursively) so all
// headings of a level share identical typography. Color/background are kept.
function normalizeHeadingTypography(h: HTMLElement) {
  const strip = (el: HTMLElement) => {
    HEADING_TYPO_PROPS.forEach(p => el.style.removeProperty(p))
    if (!el.getAttribute('style')) el.removeAttribute('style')
  }
  strip(h)
  h.querySelectorAll<HTMLElement>('*').forEach(strip)
  // <font> tags carry face/size via attributes invisible to CSS — unwrap.
  Array.from(h.querySelectorAll('font')).forEach(f => unwrapElementKeepText(f as HTMLElement))
}

function applyHeadingCommand(heading: string) {
  pushUndoState(true)
  const root = editorRef.value
  const before = new Set(root ? Array.from(root.querySelectorAll('h1, h2, h3, h4, h5, h6')) : [])
  const currentLevel = getCurrentHeadingLevel()
  if (currentLevel === heading) {
    document.execCommand('formatBlock', false, '<p>')
  } else {
    document.execCommand('formatBlock', false, `<${heading}>`)
    if (root) {
      root.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach((h) => {
        if (!before.has(h as HTMLElement)) normalizeHeadingTypography(h as HTMLElement)
      })
    }
  }
  focus()
  nextTick(() => pushCurrentState())
}

function onEditorScroll() {
  if (imageResizeState.value.visible) {
    updateImageResizeOverlay()
  }
  // Hide the table edge "+" button on scroll: its fixed-position coordinates
  // become stale when the table moves under the viewport.
  if (tableEdgeButton.value.visible || tableCursor.value !== 'none') {
    hideTableEdgeButton()
  }
}

function restartOlNumbering(ol: HTMLOListElement | null) {
  if (!ol) return
  const li = findCurrentListItem()
  if (!li || !ol.contains(li)) return
  pushUndoState(true)
  const parentLi = ol.parentElement?.tagName === 'LI' ? ol.parentElement : null
  if (parentLi) {
    const parentOl = parentLi.parentElement?.tagName === 'OL' ? parentLi.parentElement : null
    if (parentOl) {
      if (li.previousElementSibling) {
        const newOl = document.createElement('ol')
        let current: Node | null = li
        while (current) {
          const next = current.nextSibling
          newOl.appendChild(current)
          current = next
        }
        if (ol.children.length === 0) parentLi.removeChild(ol)
        parentOl.parentNode?.insertBefore(newOl, parentOl.nextSibling)
      } else {
        parentOl.parentNode?.insertBefore(ol, parentOl.nextSibling)
        while (parentLi.firstChild) {
          parentOl.insertBefore(parentLi.firstChild, parentLi)
        }
        parentOl.removeChild(parentLi)
      }
    }
  } else if (li.previousElementSibling) {
    const newOl = document.createElement('ol')
    let current: Node | null = li
    while (current) {
      const next = current.nextSibling
      newOl.appendChild(current)
      current = next
    }
    ol.parentNode?.insertBefore(newOl, ol.nextSibling)
  } else {
    ol.style.counterReset = 'ol-counter 0'
    onInput(true)
    nextTick(() => pushCurrentState())
    return
  }
  onInput(true)
  nextTick(() => pushCurrentState())
}

function findCurrentOl(): HTMLOListElement | null {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorRef.value) return null
  let node: Node | null = sel.getRangeAt(0).startContainer
  while (node && node !== editorRef.value) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      if ((node as HTMLElement).tagName === 'OL') return node as HTMLOListElement
      if ((node as HTMLElement).tagName === 'LI') {
        const parent = (node as HTMLElement).parentElement
        if (parent && parent.tagName === 'OL') return parent
      }
    }
    node = node.parentNode
  }
  return null
}

function getCurrentBlockElement(): HTMLElement | null {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorRef.value) return null
  const editor = editorRef.value
  let node: Node | null = sel.getRangeAt(0).startContainer
  while (node && node !== editor) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = (node as HTMLElement).tagName
      if (['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE', 'LI'].includes(tag)) {
        return node as HTMLElement
      }
    }
    node = node.parentNode
  }
  // No block element found: the cursor is likely in a bare text node directly
  // under the editor (or a DIV container) — e.g. text following a raw <img> block
  // that wasn't wrapped in a <p>. Wrap it in a <p> so it becomes alignable.
  const startNode = sel.getRangeAt(0).startContainer
  if (startNode.nodeType === Node.TEXT_NODE && startNode.parentElement && editor.contains(startNode)) {
    const parent = startNode.parentElement
    // Only wrap if the parent is the editor itself or a DIV (container), not an inline element
    if (parent === editor || parent.tagName === 'DIV') {
      const p = document.createElement('p')
      parent.insertBefore(p, startNode)
      p.appendChild(startNode)
      // Restore selection inside the new <p>
      const range = document.createRange()
      range.setStart(startNode, Math.min(sel.getRangeAt(0).startOffset, (startNode.textContent || '').length))
      range.collapse(true)
      sel.removeAllRanges()
      sel.addRange(range)
      return p
    }
  }
  return null
}

function setBlockAlignment(element: HTMLElement | null, align: 'left' | 'center' | 'right') {
  if (!element || !editorRef.value) return
  pushUndoState(true)
  const tag = element.tagName
  if (tag === 'IMG') {
    setImageAlignment(element as HTMLImageElement, align)
    return
  }
  element.style.textAlign = align
  if (align === 'left') element.style.removeProperty('text-align')
  onInput(true)
  nextTick(() => pushCurrentState())
}

defineExpose({
  focus,
  execCommand,
  editorRef,
  updateMathElement,
  applyHighlightToSelection,
  applyFontColor,
  applySuperscript,
  applySubscript,
  applyToListSelections,
  applyBlockquoteToSelections,
  getMultiSelections: () => multiSelections.value,
  clearMultiSelections,
  undo,
  redo,
  resetUndoStack,
  getTextContent,
  setSelectionByTextOffset,
  replaceTextRange,
  getCurrentHeadingLevel,
  applyHeadingCommand,
  saveEditorSelection,
  restoreEditorSelection,
  insertTable,
  tableSelection,
  deleteSelectedTableRowsOrCols,
  clearTableSelection,
  tableEdgeButton,
  hideTableEdgeButton,
  handleTableEdgeInsert,
  onTableMouseMove,
  onTableMouseDown,
  deleteSelectedTableRows,
  deleteSelectedTableCols,
  hasSelectedRows,
  hasSelectedCols,
  insertImage,
  insertMedia,
  setImageAlignment,
  setBlockAlignment,
  getCurrentBlockElement,
  processImagesInEditor,
  processMediaInEditor,
  deselectImage,
  imageResizeState,
  indentListItem,
  outdentListItem,
  findCurrentListItem,
  restartOlNumbering,
  findCurrentOl,
  flushPendingImageUploads,
  flushPendingSerialization,
})
</script>

<style scoped>
.wysiwyg-editor {
  width: 100%;
  height: 100%;
  padding: 12px 24px 24px; /* wave-8 用户点名：首行与工具栏间距减半（24→12） */
  padding-bottom: var(--note-editor-padding-bottom, 24px);
  font-size: 15px;
  line-height: 1.6;
  color: var(--color-text);
  background-color: var(--color-bg);
  border: none;
  outline: none;
  overflow-y: auto;
  overscroll-behavior: contain;
}

@media (max-width: 767px) {
  .wysiwyg-editor {
    padding-bottom: calc(var(--mobile-tab-bar-offset, 66px) + 80px + env(safe-area-inset-bottom, 0px)) !important;
  }
}

.wysiwyg-editor:focus {
  outline: none;
}

.wysiwyg-editor :deep(h1) {
  font-size: 1.5em;
  font-weight: 600;
  margin: 16px 0 8px 0;
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 8px;
  cursor: pointer;
  scroll-margin-top: 16px;
}

.wysiwyg-editor :deep(h2) {
  font-size: 1.3em;
  font-weight: 600;
  margin: 16px 0 8px 0;
  border-bottom: 1px solid var(--color-border);
  padding-bottom: 6px;
  cursor: pointer;
  scroll-margin-top: 16px;
}

.wysiwyg-editor :deep(h3) {
  font-size: 1.15em;
  font-weight: 600;
  margin: 16px 0 8px 0;
  cursor: pointer;
  scroll-margin-top: 16px;
}

.wysiwyg-editor :deep(h4) {
  font-size: 1em;
  font-weight: 600;
  margin: 16px 0 8px 0;
  cursor: pointer;
  scroll-margin-top: 16px;
}

.wysiwyg-editor :deep(h1:hover),
.wysiwyg-editor :deep(h2:hover),
.wysiwyg-editor :deep(h3:hover),
.wysiwyg-editor :deep(h4:hover) {
  text-decoration: underline;
}

.wysiwyg-editor :deep(p) {
  margin: 8px 0;
}

.wysiwyg-editor :deep(audio),
.wysiwyg-editor :deep(video),
.wysiwyg-editor :deep(iframe) {
  display: block;
  max-width: 100%;
  margin: 8px 0;
  border-radius: var(--radius-sm);
}

.wysiwyg-editor :deep(video),
.wysiwyg-editor :deep(iframe) {
  max-height: 400px;
  background: #000;
  border: 0;
  aspect-ratio: 16 / 9;
}

.wysiwyg-editor :deep(p:first-child) {
  margin-top: 0;
}

.wysiwyg-editor :deep(p:last-child) {
  margin-bottom: 0;
}

.wysiwyg-editor :deep(strong),
.wysiwyg-editor :deep(b) {
  font-weight: 600;
}

.wysiwyg-editor :deep(em),
.wysiwyg-editor :deep(i) {
  font-style: italic;
}

.wysiwyg-editor :deep(a) {
  color: var(--color-primary-dark);
  text-decoration: none;
  border-bottom: 1px dashed var(--color-primary-dark);
  cursor: pointer;
}

.wysiwyg-editor :deep(a:hover) {
  border-bottom-style: solid;
}

.wysiwyg-editor :deep(ul),
.wysiwyg-editor :deep(ol) {
  margin: 4px 0;
  padding-left: 24px;
}

.wysiwyg-editor :deep(li) {
  margin: 2px 0;
  line-height: 1.6;
}

.wysiwyg-editor :deep(ul) {
  list-style-type: disc;
}

.wysiwyg-editor :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.wysiwyg-editor :deep(ol > li) {
  counter-increment: ol-counter;
}

.wysiwyg-editor :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  margin-right: 2px;
}

.wysiwyg-editor :deep(ul ul) {
  list-style-type: circle;
}

.wysiwyg-editor :deep(ul ul ul) {
  list-style-type: square;
}

.wysiwyg-editor :deep(blockquote) {
  margin: 12px 0;
  padding: 8px 16px;
  border-left: 4px solid var(--color-primary);
  background-color: var(--color-sidebar);
  color: var(--color-text-light);
  font-style: italic;
}

.wysiwyg-editor :deep(pre) {
  background-color: var(--color-code-bg);
  padding: 12px 16px;
  border-radius: var(--radius-md);
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--color-border);
}

.wysiwyg-editor :deep(.code-block pre) {
  background: transparent;
  border: 0;
  border-radius: 0;
  margin: 0;
  padding: 12px 16px;
}

.wysiwyg-editor :deep(code) {
  font-family: var(--font-mono);
  background-color: var(--color-code-bg);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

.wysiwyg-editor :deep(pre code) {
  background: none;
  padding: 0;
}

.wysiwyg-editor :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 16px 0;
}

.wysiwyg-editor :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
  font-size: 13px;
}

.wysiwyg-editor :deep(th),
.wysiwyg-editor :deep(td) {
  border: 1px solid var(--color-border);
  padding: 8px 12px;
  text-align: left;
}

.wysiwyg-editor :deep(th) {
  background-color: var(--color-bg);
  font-weight: 600;
}

.wysiwyg-editor :deep(.table-cell-selected) {
  background-color: rgba(59, 130, 246, 0.15) !important;
  outline: 2px solid rgba(59, 130, 246, 0.5);
  outline-offset: -2px;
}

.wysiwyg-editor :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-md);
  margin: 8px 0;
  cursor: pointer;
}

.wysiwyg-editor :deep(img[align="left"]) {
  float: left;
  margin: 8px 12px 8px 0;
  max-width: 50%;
}

.wysiwyg-editor :deep(img[align="right"]) {
  float: right;
  margin: 8px 0 8px 12px;
  max-width: 50%;
}

.wysiwyg-editor :deep(img[align="center"]) {
  display: block;
  margin: 8px auto;
}

.wysiwyg-editor :deep(img.img-selected) {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

.wysiwyg-editor :deep(.mermaid-block) {
  position: relative;
  display: flex;
  justify-content: center;
  margin: 12px 0;
  overflow-x: auto;
}

.wysiwyg-editor :deep(.mermaid-rendered-content) {
  display: flex;
  justify-content: center;
}

.wysiwyg-editor :deep(.mermaid-rendered-content svg) {
  max-width: 100%;
  height: auto;
}

.wysiwyg-editor :deep(.echarts-block) {
  position: relative;
  margin: 12px 0;
  overflow-x: auto;
}

.wysiwyg-editor :deep(.echarts-block svg) {
  max-width: 100%;
  height: auto;
  display: block;
}

.wysiwyg-editor :deep(.echarts-error) {
  background-color: color-mix(in srgb, var(--color-danger, #e53e3e) 10%, transparent);
  border: 1px solid var(--color-danger, #e53e3e);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  font-size: 13px;
  overflow-x: auto;
}

.wysiwyg-editor :deep(.mermaid-controls) {
  position: absolute;
  top: 8px;
  right: 8px;
  display: flex;
  gap: 6px;
  opacity: 0.6;
  transition: opacity 0.2s;
  z-index: 10;
}

.wysiwyg-editor :deep(.mermaid-block:hover .mermaid-controls) {
  opacity: 1;
}

.wysiwyg-editor :deep(.mermaid-edit-btn),
.wysiwyg-editor :deep(.mermaid-zoom-btn) {
  padding: 8px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.15);
}

.wysiwyg-editor :deep(.mermaid-edit-btn:hover),
.wysiwyg-editor :deep(.mermaid-zoom-btn:hover) {
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.2);
  transform: scale(1.05);
}

.wysiwyg-editor :deep(.math-editable) {
  position: relative;
  border-radius: var(--radius-sm);
  transition: background-color 0.2s;
}

.wysiwyg-editor :deep(div.math-editable) {
  display: block;
  text-align: center;
  margin: 8px 0;
}

.wysiwyg-editor :deep(span.math-editable) {
  display: inline;
}

.wysiwyg-editor :deep(.math-editable:hover) {
  background-color: var(--color-hover);
}

.wysiwyg-editor :deep(.math-rendered-content) {
  display: inline;
}

.wysiwyg-editor :deep(.math-controls) {
  position: absolute;
  top: -10px;
  right: -4px;
  display: inline-flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
  z-index: 10;
}

.wysiwyg-editor :deep(.math-editable:hover .math-controls) {
  opacity: 1;
}

.wysiwyg-editor :deep(.math-edit-btn) {
  padding: 6px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.15);
  line-height: 0;
}

.wysiwyg-editor :deep(.math-edit-btn:hover) {
  background-color: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
  box-shadow: 0 4px 12px rgba(90, 130, 60, 0.2);
  transform: scale(1.05);
}

@media (max-width: 767px) {
  .wysiwyg-editor {
    padding: 12px;
    font-size: 16px;
  }
}

.wysiwyg-editor :deep(.multi-selected) {
  background-color: rgba(100, 149, 237, 0.15) !important;
  outline: 1px dashed var(--color-primary);
  outline-offset: -1px;
}

.wysiwyg-editor :deep(sup) {
  font-size: 0.75em;
  vertical-align: super;
  line-height: 0;
}

.wysiwyg-editor :deep(sub) {
  font-size: 0.75em;
  vertical-align: sub;
  line-height: 0;
}

.wysiwyg-editor :deep(.editor-highlight) {
  background-color: #ffff00;
  border-radius: 2px;
}

.wysiwyg-editor :deep(.find-match) {
  background-color: rgba(255, 200, 0, 0.4);
  border-radius: 2px;
}

.wysiwyg-editor :deep(.find-match-current) {
  background-color: rgba(255, 140, 0, 0.7);
  border-radius: 2px;
}
</style>

<style>
.table-context-menu {
  position: fixed;
  z-index: 10000;
  background: var(--color-white, #fff);
  border: 1px solid var(--color-border, #e0e0e0);
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  padding: 4px 0;
  min-width: 160px;
}

.table-context-menu .context-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 14px;
  font-size: 13px;
  color: var(--color-text, #333);
  background: none;
  border: none;
  text-align: left;
  cursor: pointer;
  transition: background-color 0.15s;
}

.table-context-menu .context-menu-item:hover {
  background-color: var(--color-hover, #f5f5f5);
}

.table-context-menu .context-menu-item.danger {
  color: var(--color-error, #e53e3e);
}

.table-context-menu .context-menu-item.danger:hover {
  background-color: rgba(229, 62, 62, 0.08);
}

.table-context-menu .context-menu-icon {
  width: 16px;
  text-align: center;
  flex-shrink: 0;
}

.table-context-menu .context-menu-divider {
  height: 1px;
  background-color: var(--color-border, #e0e0e0);
  margin: 4px 0;
}

.wysiwyg-editor-wrap {
  position: relative;
}

.table-edge-btn {
  position: fixed;
  width: 20px;
  height: 20px;
  background-color: var(--color-primary);
  color: white;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 600;
  line-height: 1;
  cursor: pointer;
  z-index: 10000;
  box-shadow: 0 2px 6px rgba(0,0,0,0.15);
  transform: translate(-50%, -50%);
  user-select: none;
  transition: transform 0.1s;
  pointer-events: auto;
}

.table-edge-btn:hover {
  transform: translate(-50%, -50%) scale(1.2);
}

.wysiwyg-editor-wrap.table-cursor-select-row {
  cursor: e-resize;
}

.wysiwyg-editor-wrap.table-cursor-select-col {
  cursor: s-resize;
}

.wysiwyg-editor-wrap.table-cursor-select-row .wysiwyg-editor,
.wysiwyg-editor-wrap.table-cursor-select-row .wysiwyg-editor td,
.wysiwyg-editor-wrap.table-cursor-select-row .wysiwyg-editor th {
  cursor: e-resize;
}

.wysiwyg-editor-wrap.table-cursor-select-col .wysiwyg-editor,
.wysiwyg-editor-wrap.table-cursor-select-col .wysiwyg-editor td,
.wysiwyg-editor-wrap.table-cursor-select-col .wysiwyg-editor th {
  cursor: s-resize;
}
</style>