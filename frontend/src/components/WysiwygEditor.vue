<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="wysiwyg-editor-wrap" :class="tableCursorClass" style="position:relative;flex:1;overflow:hidden;display:flex;flex-direction:column">
    <div class="wysiwyg-editor" ref="editorRef" contenteditable="true" @beforeinput="onBeforeInput" @input="onInput" @paste="onPaste" @click="onClick" @contextmenu="onContextMenu" @mousedown="handleMouseDown" v-html="renderedContent"></div>
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
import { renderMarkdownToHtml, addCitationSuperscripts, htmlToMarkdown, splitNumberingResets, renderMermaidBlocks, renderEchartsBlocks, attachMathEditListeners, renderMathSafe, buildEndnoteRefHtml, buildEndnoteDefHtml, endnoteEnabled } from '@/composables/useMarkdown'
import { extractClipboardFragment, normalizeOfficeLists } from '@/composables/pasteNormalization'
import { buildCodeBlockHtml } from '@/composables/snippetInsert'
import { normalizeHeadingBlocksAround, shouldNormalizeHeadingAfterInput, isHeadingTag, HEADING_TYPO_PROPS } from '@/composables/headingTypography'
import { shouldRestoreAfterRender, type SelectionSnapshot } from '@/composables/selectionRestore'
import { commitSpan, dropSpanCovering, expandWordRun, filterValidSpans, shouldHealComposingFromInput, shouldHealComposingFromKey, type Span } from '@/composables/multiRange'
import { CLEARABLE_INLINE_SELECTOR, isBlockResetTag, isClearableInlineTag, isLayoutTag, isStrippedStyleProperty, PROTECTED_SELECTOR, resolveClearScope, type ClearScope } from '@/composables/clearFormatting'

/** Top-level block tags that can hold inline content and may be converted. */
const INLINE_BLOCK_TAGS = new Set(['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'DIV'])
/** Blocks safe to merge a heading into (DIV containers such as code/mermaid/math are excluded). */
const MERGE_TARGET_TAGS = new Set(['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE'])
/** Structural previous blocks where Backspace must NOT merge (demote the heading in place instead). */
const STRUCTURAL_PREV_TAGS = new Set(['UL', 'OL', 'TABLE', 'PRE', 'HR', 'DIV', 'FIGURE'])

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

// 虚拟多 range（不连续选择，笔记波 2026-10-25 判据 #3/#4）：
// Chromium 的 contenteditable 不支持原生多 range（实测见 composables/multiRange.ts），
// 额外段以「编辑器根 flat 文本偏移」存储（与 clearTarget/undo 同一偏移口径），渲染走
// CSS Custom Highlight API（零 DOM 污染，不进 markdown 往返）。普通按下/文本输入/
// undo/整篇重渲染即清空；行内格式动作按「额外段+原生选区」逐段应用。
const extraSpans = ref<Span[]>([])
let multiGesture: { x: number; y: number; dragged: boolean; anchor: { node: Node; offset: number } | null } | null = null
const MULTI_HIGHLIGHT_NAME = 'wt-multi-range'
const DRAG_THRESHOLD = 5
/** 多 range 下需按段循环的原生行内样式命令（判据 #4）。 */
const MULTI_INLINE_COMMANDS = new Set(['bold', 'italic', 'underline', 'strikeThrough'])

// Undo/Redo state
// 选区以「全文文本偏移」持久化 —— 旧实现存 DOM 子节点路径，而快照 HTML 是
// 剥标记+重解析后的树（文本节点合并/拆分），路径必然漂移（查找高亮可见期
// undo 丢光标根因，wave-2 A1）。文本偏移与 span 结构无关，天然抗漂移。
interface SavedSelection {
  startOffset: number
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

/** 点位全文文本偏移：root 内 (node, offset) 之前的文本长度。 */
function textOffsetOf(root: HTMLElement, node: Node, offset: number): number {
  const r = document.createRange()
  r.selectNodeContents(root)
  try {
    r.setEnd(node, offset)
  } catch {
    return 0
  }
  return r.toString().length
}

function saveSelection(root: HTMLElement): SavedSelection | null {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return null
  const range = sel.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null
  try {
    return {
      startOffset: textOffsetOf(root, range.startContainer, range.startOffset),
      endOffset: textOffsetOf(root, range.endContainer, range.endOffset),
    }
  } catch {
    return null
  }
}

function restoreSelection(root: HTMLElement, saved: SavedSelection | null): boolean {
  if (!saved) return false
  try {
    return setSelectionByTextOffset(saved.startOffset, saved.endOffset)
  } catch {
    return false
  }
}

/**
 * 选区快照（遗留 #2）：偏移 + 当拍「可比文本」。恢复门禁见 composables/selectionRestore.ts。
 * 与 saveSelection 的区别：undo 栈只需要偏移；重渲染恢复还要「重渲染前后是否同一篇
 * 文档」的判据，故多带 text。
 *
 * 可比文本剔除非文本渲染产物：mermaid/echarts 块的源码在重渲染前已被替换成 SVG
 * （标签字会进 textContent），重渲染后又是占位 div —— 两侧永不相同，会把恢复整体
 * 关死（A4.9 评审 I2）。图片/音视频/公式节点同理按结构剔除。
 */
const COMPARABLE_EXCLUDE = '.mermaid-block, .echarts-block, .math-editable, .katex, img, video, audio, iframe, svg'

function comparableText(root: HTMLElement): string {
  const clone = root.cloneNode(true) as HTMLElement
  clone.querySelectorAll(COMPARABLE_EXCLUDE).forEach(n => n.remove())
  return clone.textContent || ''
}

/** 节点是否落在「非文本渲染产物」子树内（图表/公式/媒体）。 */
function isExcludedTextNode(node: Node): boolean {
  const el = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement
  return !!el && !!el.closest(COMPARABLE_EXCLUDE)
}

/**
 * (node, offset) 相对 container 的**可比文本**偏移；不同树返回 -1。
 * 与 `comparableText` 同一坐标空间（剔除图表/公式/媒体的渲染文本），否则快照
 * 偏移会被 SVG 标签字撑出偏差（A4.9 复审 N3）。
 */
function comparableOffsetWithin(container: Node, node: Node, offset: number): number {
  if (!container.contains(node)) return -1
  const r = document.createRange()
  r.selectNodeContents(container)
  try { r.setEnd(node, offset) } catch { return -1 }
  const frag = r.cloneContents()
  frag.querySelectorAll(COMPARABLE_EXCLUDE).forEach(n => n.remove())
  return (frag.textContent || '').length
}

/** 顶层块下标（root 的直系子元素），用于消除「块尾 == 下一块段首」的二义性。 */
function topLevelBlockIndex(root: HTMLElement, node: Node): number {
  let n: Node | null = node
  while (n && n.parentNode !== root) n = n.parentNode
  if (!n || n.nodeType !== Node.ELEMENT_NODE) return -1
  return Array.prototype.indexOf.call(root.children, n as Element)
}

/** 在 scope 内按可比文本偏移落折叠光标；无匹配文本节点返回 false。 */
function setCaretAtComparableOffset(scope: HTMLElement, offset: number): boolean {
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT)
  let acc = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (isExcludedTextNode(node)) continue
    const len = (node.textContent || '').length
    if (len === 0) continue
    if (acc + len >= offset) {
      const range = document.createRange()
      range.setStart(node, Math.max(0, Math.min(len, offset - acc)))
      range.collapse(true)
      const sel = window.getSelection()
      if (sel) {
        sel.removeAllRanges()
        sel.addRange(range)
      }
      return true
    }
    acc += len
  }
  return false
}

/** 在 scope 内按可比文本偏移设置选区（非折叠）。 */
function setSelectionByComparableOffset(scope: HTMLElement, startOffset: number, endOffset: number): boolean {
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT)
  let acc = 0
  let sn: Node | null = null
  let so = 0
  let en: Node | null = null
  let eo = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    if (isExcludedTextNode(node)) continue
    const len = (node.textContent || '').length
    if (len === 0) continue
    if (!sn && acc + len >= startOffset) { sn = node; so = startOffset - acc }
    if (!en && acc + len >= endOffset) { en = node; eo = endOffset - acc; break }
    acc += len
  }
  if (!sn || !en) return false
  const range = document.createRange()
  range.setStart(sn, Math.max(0, Math.min((sn.textContent || '').length, so)))
  range.setEnd(en, Math.max(0, Math.min((en.textContent || '').length, eo)))
  const sel = window.getSelection()
  if (sel) {
    sel.removeAllRanges()
    sel.addRange(range)
  }
  return true
}

/**
 * 选区快照（遗留 #2）：扁/块内偏移（flat + comparable 两套）+ 可比文本。
 *
 * - **flat**：`Range.toString()` 口径，同一份 DOM 内的操作（格式化解包、execCommand）
 *   用它复位 —— 保侧性（公式/图片前后不会翻边）；
 * - **comparable**：剔除图表/公式/媒体渲染文本的口径，只用于**整篇重渲染**后的复位
 *   （重渲染前后两侧的图表 DOM 形态不同，flat 口径不可比）；
 * - **blockIndex**：落点锚到顶层块（段/列表/引用），消除「块尾偏移 == 下一块段首」
 *   的二义性（A4.9 复审 N2：多段笔记列表命令后光标跳到下一段）。
 */
type EditorSelectionSnapshot = SelectionSnapshot & {
  blockIndex: number
  startInBlock: number
  endInBlock: number
  startInBlockCmp: number
  endInBlockCmp: number
  startInExcluded: boolean
  /** 起始顶层块的**元素引用**：块级归一可能改变子元素数量，下标会失效（A4.9 复审 NEW-1）。 */
  startBlock: HTMLElement | null
  childCount: number
}

/** (node, offset) 相对 container 的 flat 文本偏移；不同树返回 -1。 */
function flatOffsetWithin(container: Node, node: Node, offset: number): number {
  if (!container.contains(node)) return -1
  const r = document.createRange()
  r.selectNodeContents(container)
  try { r.setEnd(node, offset) } catch { return -1 }
  return r.toString().length
}

function captureSelectionSnapshot(): EditorSelectionSnapshot | null {
  const root = editorRef.value
  if (!root) return null
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return null
  const range = sel.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null
  const blockIndex = topLevelBlockIndex(root, range.startContainer)
  let startInBlock = -1
  let endInBlock = -1
  let startInBlockCmp = -1
  let endInBlockCmp = -1
  if (blockIndex >= 0) {
    const block = root.children[blockIndex] as HTMLElement
    startInBlock = flatOffsetWithin(block, range.startContainer, range.startOffset)
    endInBlock = flatOffsetWithin(block, range.endContainer, range.endOffset)
    startInBlockCmp = comparableOffsetWithin(block, range.startContainer, range.startOffset)
    endInBlockCmp = comparableOffsetWithin(block, range.endContainer, range.endOffset)
  }
  const startEl = range.startContainer.nodeType === Node.ELEMENT_NODE
    ? (range.startContainer as Element)
    : range.startContainer.parentElement
  return {
    startOffset: textOffsetOf(root, range.startContainer, range.startOffset),
    endOffset: textOffsetOf(root, range.endContainer, range.endOffset),
    text: comparableText(root),
    blockIndex,
    startInBlock,
    endInBlock,
    startInBlockCmp,
    endInBlockCmp,
    startInExcluded: !!startEl && !!startEl.closest(COMPARABLE_EXCLUDE),
    startBlock: blockIndex >= 0 ? (root.children[blockIndex] as HTMLElement) : null,
    childCount: root.children.length,
  }
}

/** 当前光标在「顶层块 + 块内 flat 偏移」坐标系下的位置（用于幂等判定）。 */
function currentCaretBlockPosition(): { blockIndex: number; inBlock: number } | null {
  const root = editorRef.value
  const sel = window.getSelection()
  if (!root || !sel || !sel.rangeCount) return null
  const r = sel.getRangeAt(0)
  if (!root.contains(r.startContainer)) return null
  const blockIndex = topLevelBlockIndex(root, r.startContainer)
  if (blockIndex < 0) return { blockIndex, inBlock: -1 }
  const block = root.children[blockIndex] as HTMLElement
  return { blockIndex, inBlock: flatOffsetWithin(block, r.startContainer, r.startOffset) }
}

type RestoreMode = 'flat' | 'comparable'

/**
 * 折叠光标复位。mode='flat' 用于同一份 DOM（保侧性）；mode='comparable' 用于整篇
 * 重渲染。which 决定用选区起点还是终点（解包格式后应落在原选区末端，判据 #69）。
 */
function restoreCollapsedSelection(saved: EditorSelectionSnapshot, mode: RestoreMode, which: 'start' | 'end') {
  const editor = editorRef.value
  if (!editor) return
  // 块级归一（标题/引用/代码块）会改变顶层子元素数量 —— 下标不再指向同一块时
  // 只能靠元素引用；引用也失效则**不猜**（保留浏览器当前选区，A4.9 复审 NEW-1）。
  const byRef = saved.startBlock && saved.startBlock.isConnected ? saved.startBlock : null
  const byIndex = editor.children[saved.blockIndex] as HTMLElement | undefined
  const structureChanged = editor.children.length !== saved.childCount
  const block = byRef ?? (structureChanged ? null : byIndex)
  if (saved.blockIndex >= 0) {
    if (block) {
      if (mode === 'comparable') {
        const cmp = which === 'end' ? saved.endInBlockCmp : saved.startInBlockCmp
        if (setCaretAtComparableOffset(block, cmp)) return
      } else {
        const flat = which === 'end' ? saved.endInBlock : saved.startInBlock
        setCaretAtTextOffset(block, flat) // 块内 `>=`；无文本节点时自带块首回落
        return
      }
      // comparable 解析失败（块被重建/空块）：落到块首，绝不跨块
      const range = document.createRange()
      range.selectNodeContents(block)
      range.collapse(true)
      const sel = window.getSelection()
      if (sel) {
        sel.removeAllRanges()
        sel.addRange(range)
      }
      return
    }
  }
  // 光标不在任何顶层块内（块间空白/根级位置）：不猜，保持浏览器默认
}

/** 选区（非折叠）复位：两端同块时块内解析，否则回落扁平偏移。 */
function restoreRangeSelection(saved: EditorSelectionSnapshot, mode: RestoreMode) { // 仅 comparable 调用
  const editor = editorRef.value
  if (!editor) return
  if (saved.blockIndex >= 0 && saved.startInBlock >= 0 && saved.endInBlock >= 0) {
    const block = editor.children[saved.blockIndex] as HTMLElement | undefined
    if (block) {
      if (mode === 'comparable') {
        if (setSelectionByComparableOffset(block, saved.startInBlockCmp, saved.endInBlockCmp)) return
      }
    }
  }
  setSelectionByTextOffset(saved.startOffset, saved.endOffset)
}

/**
 * 重渲染后按快照恢复选区；不满足门禁则不动（绝不把光标拽回旧位置）。
 * 光标原本落在图表/公式内部时跳过：可比重渲染两侧形态不同，复位会把光标拽出元素。
 */
function restoreSelectionAfterRender(saved: EditorSelectionSnapshot | null) {
  if (!editorRef.value || !saved) return
  const ok = shouldRestoreAfterRender(
    saved,
    comparableText(editorRef.value),
    document.activeElement === editorRef.value,
    isComposing,
  )
  if (!ok) return
  if (saved.startInExcluded) return
  if (saved.startOffset === saved.endOffset) restoreCollapsedSelection(saved, 'comparable', 'start')
  else restoreRangeSelection(saved, 'comparable')
}

/** 把光标放到元素之后（格式化落点：继续输入不再被吸进刚创建的 sup/sub）。 */
function setCaretAfterElement(el: Element) {
  const range = document.createRange()
  range.setStartAfter(el)
  range.collapse(true)
  const sel = window.getSelection()
  if (sel) {
    sel.removeAllRanges()
    sel.addRange(range)
  }
}

/**
 * 格式化自操作后的落点（判据 #69）：
 * - 包格式 → 光标放到**新元素之后**（不用文本偏移猜，块尾也不会越到下一块）；
 * - 解包格式 → 按块内 flat 偏移落到原选区**末端**（round-2 评审 I1）。
 * 折叠快照（no-op）不重落光标（A4.9 评审 A-I1）。
 */
function restoreCaretAfterFormat(saved: EditorSelectionSnapshot | null, wrappedHost: HTMLElement | null) {
  if (!editorRef.value || !saved || isComposing) return
  if (saved.startOffset === saved.endOffset) return
  if (wrappedHost && wrappedHost.isConnected) {
    setCaretAfterElement(wrappedHost)
    return
  }
  // 跨块选区：endInBlock = -1（末端不在起始块内）——不按块内偏移猜，保持浏览器默认
  if (saved.endInBlock < 0) return
  restoreCollapsedSelection(saved, 'flat', 'end')
}

/**
 * execCommand 之后的折叠光标校正（判据 #68）：Chromium 的列表命令会把行尾光标
 * 挪到新列表项首字符前（实测偏移 2 → 0，随后键入落成「Y甲乙」）。只在
 * ①快照是折叠光标 ②命令前后「可比文本」逐字相同 ③光标仍在编辑器内
 * ④光标确实不在原位（幂等，避免把光标从新插入的分隔线下方拽回上方）
 * 时按块内 flat 偏移复位。
 */
function restoreCaretAfterCommand(saved: EditorSelectionSnapshot | null) {
  if (!editorRef.value || !saved || isComposing) return
  if (saved.startOffset !== saved.endOffset) return
  if (comparableText(editorRef.value) !== saved.text) return
  if (caretTextOffsetIn(editorRef.value) == null) return // 光标已离开编辑器 —— 不抢回来（A4.9 评审 B-M5）
  // 幂等：「已在原位」判据按**特异性优先**（A4.9 第五轮 F2）：
  // ①光标在某个顶层块内 → 用块坐标（块尾偏移 == 下一块段首偏移时，只有块坐标能区分）
  // ②光标在根级/块间（Chromium 的 insertHorizontalRule 会把光标停在这里，块坐标拿不到）
  //   → 用全局 flat 偏移。
  const cur = currentCaretBlockPosition()
  if (cur && cur.blockIndex >= 0) {
    if (cur.blockIndex === saved.blockIndex && cur.inBlock === saved.startInBlock) return
  } else {
    const globalFlat = caretTextOffsetIn(editorRef.value)
    if (globalFlat != null && globalFlat === saved.startOffset) return
  }
  restoreCollapsedSelection(saved, 'flat', 'start')
}

let isComposing = false

/**
 * IME 组合标志陈旧自愈（判据 #2）。根因：组合期发生整篇重渲染（v-html 重建子树）后，
 * compositionend 在已 detach 的旧文本节点上触发、永不到达编辑器 → 标志永久卡 true →
 * 「输入法组合中」永驻（实测打字后清除格式仍弹）。浏览器的 input/keydown 事件自带权威
 * isComposing 标志，据此复位；focusout 兜底（失焦即组合必已结束）。
 */
function healStaleComposing(): boolean {
  if (!isComposing) return false
  isComposing = false
  settleCompositionBoundary()
  return true
}

function onCompositionStart() {
  if (editorRef.value && !isUndoing) {
    pushUndoState(true)
  }
  const sel = window.getSelection()
  const root = editorRef.value
  const b = sel && sel.rangeCount === 1 && root ? resolveScriptBoundary(sel.getRangeAt(0), root) : null
  compositionBoundary = b ? { host: b.host, atEnd: b.atEnd, len: (b.host.textContent || '').length } : null
  isComposing = true
}

function onCompositionEnd() {
  isComposing = false
  settleCompositionBoundary()
  if (editorRef.value && !isUndoing) {
    pushCurrentState()
  }
}

/** 失焦兜底（判据 #2）：焦点离开编辑器 ⇒ 组合必已结束；陈旧标志复位。 */
function onEditorFocusOut(ev: FocusEvent) {
  const root = editorRef.value
  if (!root) return
  const next = ev.relatedTarget as Node | null
  if (next && root.contains(next)) return // 焦点仍在编辑器内（如点图表控件）
  healStaleComposing()
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

/**
 * 快照用 HTML：先剥掉 [data-find-match] 搜索高亮标记再入栈。
 * 不剥则 undo/redo 会把陈旧搜索高亮复活进正文（用户须手动去除的根因）。
 */
function captureSnapshotHtml(): string {
  if (!editorRef.value) return ''
  const clone = editorRef.value.cloneNode(true) as HTMLElement
  clone.querySelectorAll('[data-find-match="true"]').forEach(mark => {
    const parent = mark.parentNode
    if (!parent) return
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark)
    parent.removeChild(mark)
  })
  stripFindStyleSpans(clone)
  return clone.innerHTML
}

function pushUndoState(force = false) {
  if (isUndoing || !editorRef.value) return
  const state: EditorState = {
    html: captureSnapshotHtml(),
    selection: saveSelection(editorRef.value),
  }
  const last = undoStack.value[undoStack.value.length - 1]
  if (force) {
    undoStack.value.push(state)
    if (undoStack.value.length > 100) undoStack.value.shift()
    redoStack.value = []
  } else if (last && last.html === state.html) {
    // 同内容只合并选区（wave-2 A1）：否则 pre-change 快照会被同 html 顶条目
    // 去重吞掉，顶条目遗留陈旧/空选区，撤销落点漂移
    last.selection = state.selection
    redoStack.value = []
  } else if (!last || last.html !== state.html) {
    undoStack.value.push(state)
    if (undoStack.value.length > 100) undoStack.value.shift()
    redoStack.value = []
  }
}

function pushCurrentState() {
  if (isUndoing || !editorRef.value) return
  const state: EditorState = {
    html: captureSnapshotHtml(),
    selection: saveSelection(editorRef.value),
  }
  const last = undoStack.value[undoStack.value.length - 1]
  if (last && last.html === state.html) {
    last.selection = state.selection
  } else if (!last || last.html !== state.html) {
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
  // 去抖窗口内先落盘在途状态（wave-2 复审 B#2）：否则一次撤销回退两步
  if (inputDebounceTimer) {
    clearTimeout(inputDebounceTimer)
    inputDebounceTimer = null
    pushCurrentState()
  }
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
  if (inputDebounceTimer) {
    clearTimeout(inputDebounceTimer)
    inputDebounceTimer = null
    pushCurrentState()
  }
  if (redoStack.value.length === 0 || !editorRef.value) return
  const state = redoStack.value.pop()!
  undoStack.value.push(state)
  applyState(state)
}

function applyState(state: EditorState) {
  if (!editorRef.value) return
  // undo/redo 重建 DOM ⇒ 额外段失去意义，先清（判据 #3 清除时机）
  clearMultiRanges()
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
    // Ctrl/Cmd 手势（判据 #3）：Chromium 对 ctrl+drag **不产生**原生选区（实测，
    // 且可能进 HTML5 拖拽）→ 接管手势：preventDefault 后由本组件在 mousemove 里
    // 手动构建选区；mouseup 把拖出的段提交为额外段并把原生选区塌到段尾。
    // 仅左键（评审 A2）：Ctrl+右键属右键语义（Linux/Win 替代右键），交回 contextmenu 通道。
    if (ev.button !== 0) return
    ev.preventDefault()
    if (editorRef.value) {
      try { editorRef.value.focus({ preventScroll: true }) } catch { /* 老浏览器 */ }
    }
    commitNativeSelectionToSpans()
    const anchor = caretAtPoint(ev.clientX, ev.clientY)
    if (anchor) collapseCaretTo(anchor)
    multiGesture = { x: ev.clientX, y: ev.clientY, dragged: false, anchor }
    document.addEventListener('mousemove', handleMouseMove)
    document.addEventListener('mouseup', handleMouseUp, { once: true })
  } else {
    // 普通按下 = 多 range 会话结束（判据 #5：旧块多选已删除，不再产生 .multi-selected）
    abortMultiGesture()
    clearMultiRanges()
  }
}

/** 手势滞留清理（评审 A3）：mouseup 漏送（窗口外松手/alt-tab）时防 contextmenu 永久被抑制。 */
function abortMultiGesture() {
  if (multiGesture) {
    multiGesture = null
    document.removeEventListener('mousemove', handleMouseMove)
  }
}

function onWindowBlurAbortGesture() {
  abortMultiGesture()
}

/** 屏幕点 → 编辑器内 (node, offset)；不在编辑器内返回 null。 */
function caretAtPoint(x: number, y: number): { node: Node; offset: number } | null {
  const root = editorRef.value
  if (!root) return null
  try {
    const r = (document as any).caretRangeFromPoint?.(x, y) as Range | undefined
    if (!r || !root.contains(r.startContainer)) return null
    return { node: r.startContainer, offset: r.startOffset }
  } catch {
    return null
  }
}

function collapseCaretTo(pos: { node: Node; offset: number }) {
  try {
    const r = document.createRange()
    r.setStart(pos.node, pos.offset)
    r.collapse(true)
    const sel = window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(r)
  } catch { /* 节点不可锚定 */ }
}

/** 拖拽中选区：anchor ↔ 当前点（手动构建，浏览器不参与）。
 *  定序（评审 A1 Critical）：反向（右→左/下→上）拖选时 head 在 anchor 之前，
 *  直接 setStart(anchor)+setEnd(head) 会按规范把 start 拽到 head 折叠掉 —— 必须先比
 *  较文档序再按 (前, 后) 设端点。 */
function setDragSelection(anchor: { node: Node; offset: number }, head: { node: Node; offset: number }) {
  const root = editorRef.value
  if (!root) return
  try {
    const r = document.createRange()
    const samePoint = head.node === anchor.node && head.offset === anchor.offset
    r.setStart(anchor.node, anchor.offset)
    r.setEnd(head.node, head.offset)
    if (r.collapsed) {
      if (samePoint) return
      // 被规范折叠 ⇒ head 在 anchor 之前（反向拖选，评审 A1）→ 按文档序重建
      r.setStart(head.node, head.offset)
      r.setEnd(anchor.node, anchor.offset)
      if (r.collapsed) return
    }
    const sel = window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(r)
  } catch { /* 跨不可比节点 */ }
}

function handleMouseMove(ev: MouseEvent) {
  if (!multiGesture) return
  const dx = Math.abs(ev.clientX - multiGesture.x)
  const dy = Math.abs(ev.clientY - multiGesture.y)
  if (dx > DRAG_THRESHOLD || dy > DRAG_THRESHOLD) {
    multiGesture.dragged = true
  }
  if (multiGesture.dragged && multiGesture.anchor) {
    const head = caretAtPoint(ev.clientX, ev.clientY)
    if (head) setDragSelection(multiGesture.anchor, head)
  }
}

function handleMouseUp(ev: MouseEvent) {
  const gesture = multiGesture
  multiGesture = null
  document.removeEventListener('mousemove', handleMouseMove)
  if (!gesture) return
  const root = editorRef.value
  if (!root) return

  if (gesture.dragged) {
    // 拖拽选段收尾：把拖出的原生段提交为额外段，并把原生选区塌到段尾
    // （所有已提交段统一由 CSS.highlights 呈现，避免与浏览器原生选区双画/误触
    // 下一次提交的 toggle 移除）。
    const sel = window.getSelection()
    if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
      const r = sel.getRangeAt(0)
      if (root.contains(r.startContainer) && root.contains(r.endContainer)) {
        const start = textOffsetOf(root, r.startContainer, r.startOffset)
        const end = textOffsetOf(root, r.endContainer, r.endOffset)
        if (start < end) {
          extraSpans.value = commitSpan(extraSpans.value, { start, end })
          try {
            const tail = document.createRange()
            tail.setStart(r.endContainer, r.endOffset)
            tail.collapse(true)
            sel.removeAllRanges()
            sel.addRange(tail)
          } catch { /* 塌缩失败不影响已提交段 */ }
        }
      }
    }
    renderMultiHighlights()
    return
  }

  // 无拖拽（ctrl+单击，判据 #3 · ADR mr5，Google Docs 语义）：
  // 命中已有段 → 整段移除；否则按点击点词段展开加入。
  try {
    const at0 = caretAtPoint(ev.clientX, ev.clientY)
    if (!at0) return
    const at = textOffsetOf(root, at0.node, at0.offset)
    const drop = dropSpanCovering(extraSpans.value, at)
    if (drop.removed) {
      extraSpans.value = drop.spans
    } else if (at0.node.nodeType === Node.TEXT_NODE) {
      // 词段展开只认文本锚（评审 A12-UNCERTAIN）：caretRangeFromPoint 命中块边缘会
      // 返回元素锚 (p, childIndex)，childIndex 当字符偏移用会错位 —— 直接放弃加段
      const text = at0.node.textContent || ''
      const word = expandWordRun(text, Math.min(at0.offset, text.length))
      if (word) {
        const gStart = textOffsetOf(root, at0.node, word.start)
        const gEnd = textOffsetOf(root, at0.node, word.end)
        if (gStart < gEnd) extraSpans.value = commitSpan(extraSpans.value, { start: gStart, end: gEnd })
      }
    }
    renderMultiHighlights()
  } catch {
    // caretRangeFromPoint 不可用时静默降级（手势本身已接管完成）
  }
}

// ===== 虚拟多 range 辅助（笔记波 2026-10-25，判据 #3/#4） =====

/** 编辑器根当前 flat 文本长度（额外段越界过滤用；与 setSelectionByTextOffset 同口径）。 */
function editorTextLength(): number {
  const root = editorRef.value
  if (!root) return 0
  let n = 0
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  while (walker.nextNode()) n += (walker.currentNode.textContent || '').length
  return n
}

/** 额外段 → 可施加格式的 Range 列表（跳过不可解析的越界段）。 */
function spansToRanges(spans: Span[]): Range[] {
  const root = editorRef.value
  const out: Range[] = []
  if (!root) return out
  for (const span of spans) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    let cur = 0
    let startNode: Node | null = null, so = 0, endNode: Node | null = null, eo = 0
    while (walker.nextNode()) {
      const node = walker.currentNode
      const len = node.textContent?.length || 0
      if (len === 0) continue
      if (!startNode && cur + len >= span.start) { startNode = node; so = span.start - cur }
      if (!endNode && cur + len >= span.end) { endNode = node; eo = span.end - cur; break }
      cur += len
    }
    if (startNode && endNode && !(startNode === endNode && so === eo)) {
      const r = document.createRange()
      r.setStart(startNode, so)
      r.setEnd(endNode, eo)
      out.push(r)
    }
  }
  return out
}

/** 有效选区段 = 额外段 ∪ 当前原生选区（去重、按 start 排序）。 */
function getEffectiveSpans(): Span[] {
  const root = editorRef.value
  const spans = extraSpans.value.slice()
  if (root) {
    const sel = window.getSelection()
    if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
      const r = sel.getRangeAt(0)
      if (root.contains(r.startContainer) && root.contains(r.endContainer)) {
        const s = textOffsetOf(root, r.startContainer, r.startOffset)
        const e = textOffsetOf(root, r.endContainer, r.endOffset)
        if (s < e && !spans.some((x) => x.start === s && x.end === e)) spans.push({ start: s, end: e })
      }
    }
  }
  spans.sort((a, b) => a.start - b.start)
  return spans
}

/**
 * 行内格式动作的选区来源：有额外段 → 用「额外段+原生」解析出的 ranges（判据 #4，跨段生效）；
 * 无额外段 → 沿用原生 selection.getRangeAt（行为与改动前逐字一致，零回归）。
 * 段间互不相交；同段多段时依赖浏览器对 live Range 的规范重映射（splitText 会调整越界
 * 端点），toggleScriptTag 另有倒序处理（判据 #42）双保险（评审 A7 澄清）。
 */
function getActionRanges(): { ranges: Range[]; multi: boolean } {
  const root = editorRef.value
  if (!root || extraSpans.value.length === 0) {
    const sel = window.getSelection()
    if (!sel || sel.rangeCount === 0) return { ranges: [], multi: false }
    return { ranges: Array.from({ length: sel.rangeCount }, (_, i) => sel.getRangeAt(i)), multi: false }
  }
  return { ranges: spansToRanges(getEffectiveSpans()), multi: true }
}

/** 原生选区（非折叠、在编辑器内）提交为额外段（ctrl 手势 mousedown 期，浏览器覆盖前）。 */
function commitNativeSelectionToSpans(): void {
  const root = editorRef.value
  const sel = window.getSelection()
  if (!root || !sel || sel.rangeCount === 0 || sel.isCollapsed) return
  const r = sel.getRangeAt(0)
  if (!root.contains(r.startContainer) || !root.contains(r.endContainer)) return
  const s = textOffsetOf(root, r.startContainer, r.startOffset)
  const e = textOffsetOf(root, r.endContainer, r.endOffset)
  if (s >= e) return
  extraSpans.value = commitSpan(extraSpans.value, { start: s, end: e })
  renderMultiHighlights()
}

/** 清空虚拟多 range（普通按下 / 输入 / undo / 重渲染）。 */
function clearMultiRanges(): void {
  if (extraSpans.value.length === 0) return
  extraSpans.value = []
  renderMultiHighlights()
}

/** 额外段失效化守卫（评审 A5/N1）：文档文本长度 ≠ 提交基线 ⇒ 偏移空间已漂移
 *  （表格删行列、公式/mermaid 外部编辑、任何未走 beforeinput 的文本手术）——
 *  结束多 range 会话；长度未变则只做越界钳制。 */
function pruneExtraSpans(): void {
  if (extraSpans.value.length === 0) return
  if (spansDocLen >= 0 && editorTextLength() !== spansDocLen) {
    clearMultiRanges()
    return
  }
  const kept = filterValidSpans(extraSpans.value, editorTextLength())
  // 内容比较（评审 A8）：docLen 变短但无段整体消失时，钳制结果与旧数组同长 —— 只比长度会丢弃钳制值
  const same = kept.length === extraSpans.value.length &&
    kept.every((s, i) => s.start === extraSpans.value[i].start && s.end === extraSpans.value[i].end)
  if (!same) {
    extraSpans.value = kept
    renderMultiHighlights()
  }
}

/** 额外段提交时的文档文本长度基线（偏移空间快照，评审 N1）：长度一变 ⇒ 偏移空间
 *  已漂移，段一律失效（格式手术不改长度 → 会话存活；删行/替换/公式编辑改长度 → 结束）。 */
let spansDocLen = -1

/** 渲染额外段高亮（CSS Custom Highlight API，零 DOM 污染）。 */
function renderMultiHighlights(): void {
  spansDocLen = editorTextLength()
  const C: any = (window as any).CSS
  if (!C || !C.highlights) return
  const HL: any = (window as any).Highlight
  if (!HL) return
  if (extraSpans.value.length === 0) {
    C.highlights.delete(MULTI_HIGHLIGHT_NAME)
    return
  }
  try {
    const ranges = spansToRanges(extraSpans.value)
    if (ranges.length === 0) C.highlights.delete(MULTI_HIGHLIGHT_NAME)
    else C.highlights.set(MULTI_HIGHLIGHT_NAME, new HL(...ranges))
  } catch {
    /* 高亮渲染失败不影响选区数据与格式动作 */
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

/**
 * 逐 text-node 包裹选区（ProseMirror mark 纪律）。
 * 绝不用 extractContents 整块包裹 —— 那会把块级元素（p/li/h*）拖进 inline span，
 * 产生 span>p 非法结构，浏览器修复/序列化回灌即「自动换行」与字号失效的根因。
 * 每个文本节点的选中片段各自包一个 span，块结构一概不动。
 */
function wrapRangePerTextNode(
  range: Range,
  root: HTMLElement,
  makeSpan: () => HTMLElement,
): HTMLElement[] {
  const created: HTMLElement[] = []
  const textNodes = collectTextNodesInRange(range, root)
  const startContainer = range.startContainer
  const endContainer = range.endContainer
  const startOffset = range.startOffset
  const endOffset = range.endOffset

  for (const textNode of textNodes) {
    if (!textNode.textContent) continue
    // 跳过不可编辑部件（mermaid/echarts/复制钮）内部文本与纯空白节点：
    // 包裹会破坏部件 DOM / 给空格加无意义 span（A4.9 N14）
    const parentEl = textNode.parentElement
    if (parentEl && parentEl.closest('[contenteditable="false"]')) continue
    if (!textNode.textContent.trim()) continue
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
    // 边界防过包：intersectsNode 含「仅触边」的节点；把子区间夹回外层 range，
    // 夹完为空（只触边）就跳过 —— 选 "hello " 不得连带包掉  里的 world
    try {
      if (range.comparePoint(nodeRange.startContainer, nodeRange.startOffset) < 0) {
        nodeRange.setStart(range.startContainer, range.startOffset)
      }
      if (range.comparePoint(nodeRange.endContainer, nodeRange.endOffset) > 0) {
        nodeRange.setEnd(range.endContainer, range.endOffset)
      }
    } catch { /* 跨树比较失败则维持原区间 */ }
    if (nodeRange.collapsed) continue
    const span = makeSpan()
    try {
      nodeRange.surroundContents(span)
      created.push(span)
    } catch {
      // 单文本节点内的 range 不应失败；失败则跳过该节点，绝不整块 extract
      continue
    }
  }
  return created
}

function applyFontSizeToSelection(size: string) {
  if (isComposing) return // IME 组合期不做格式变更（wave-2 A5）
  pushUndoState(true)
  const root = editorRef.value
  if (!root) return
  // 多 range（判据 #4）：额外段+原生；无额外段时与原生选区逐字等价
  const ranges = getActionRanges().ranges
  if (ranges.length === 0) return
  for (const range of ranges) {
    if (range.collapsed) continue
    // 先用「未变形」的 range 采样待清理的既有字号 span（wrap 之后 range 会失效），
    // 再包裹、最后统一清理 —— 多次改字号只换值不叠层（A4.9 I4）。
    const toClear: HTMLElement[] = []
    for (const textNode of collectTextNodesInRange(range, root)) {
      let n: Node | null = textNode.parentNode
      while (n && n !== root) {
        if (n.nodeType === Node.ELEMENT_NODE) {
          const el = n as HTMLElement
          if (el.tagName === 'SPAN' && el.style.fontSize && isElementFullyInRange(el, range)) {
            toClear.push(el)
          }
        }
        n = n.parentNode
      }
    }
    if (size) {
      // 逐文本节点包 span（px 绝对值，内层永远压过外层）。绝不 extractContents：
      // 块结构一概不动（span>p 非法结构=换行/字号失效根因）。
      wrapRangePerTextNode(range, root, () => {
        const span = document.createElement('span')
        span.style.fontSize = size
        return span
      })
    }
    for (const el of toClear) {
      if (!el.isConnected) continue
      el.style.removeProperty('font-size')
      if (!el.getAttribute('style')) unwrapElementKeepText(el)
    }
  }
  // 保留用户选区（wave-2 复审 #2）：格式化不应清空选区/挪动视口
  onInput()
  nextTick(() => pushCurrentState())
}

function isElementFullyInRange(el: HTMLElement, range: Range): boolean {
  // 以元素的首/末文本端点判覆盖 —— selectNodeContents 的元素锚点边界与
  // 常见的文本锚点选区（双击/拖选）比较会恒 false（A4.9 C2）。
  try {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    const texts: Text[] = []
    while (walker.nextNode()) texts.push(walker.currentNode as Text)
    if (texts.length === 0) {
      const elRange = document.createRange()
      elRange.selectNodeContents(el)
      return (
        range.compareBoundaryPoints(Range.START_TO_START, elRange) <= 0 &&
        range.compareBoundaryPoints(Range.END_TO_END, elRange) >= 0
      )
    }
    const first = texts[0]
    const last = texts[texts.length - 1]
    const a = range.comparePoint(first, 0)
    const b = range.comparePoint(last, last.length)
    // 首端在 range 前（-1）= 元素左侧溢出；末端在 range 后（1）= 右侧溢出
    return a !== -1 && b !== 1
  } catch {
    return false
  }
}

function applyHighlightToSelection(color: string = '#ffff00') {
  if (isComposing) return // IME 组合期不做格式变更（wave-2 A5）
  pushUndoState(true)
  const root = editorRef.value
  if (!root) return

  const isRemoveMode = !color
  const normalizedTargetColor = isRemoveMode ? '' : normalizeColor(color)

  // 多 range（判据 #4）：额外段+原生；无额外段时与原生选区逐字等价
  const ranges = getActionRanges().ranges
  if (ranges.length === 0) return

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
      // 先收集再统一解包：解包时 normalize() 会合并相邻文本节点，
      // 边遍历边解包会让后续节点引用失效 ——「只取消最先匹配一段」的根因。
      // 收集整条祖先链（不只最近层）：嵌套旧高亮一次全清（A4.9 I5）。
      const toUnwrap = new Set<HTMLElement>()
      for (const textNode of textNodes) {
        const txt = textNode.textContent || ''
        if (!txt.trim()) continue
        let n: Node | null = textNode.parentNode
        while (n && n !== root) {
          if (n.nodeType === Node.ELEMENT_NODE) {
            const el = n as HTMLElement
            if ((el.tagName === 'SPAN' && el.style.backgroundColor) || el.tagName === 'MARK') {
              toUnwrap.add(el)
            }
          }
          n = n.parentNode
        }
      }
      for (const ancestor of toUnwrap) {
        unwrapElementKeepText(ancestor)
      }
      continue
    }

    if (allHighlighted && firstHighlightColor === normalizedTargetColor) {
      for (const ancestor of highlightAncestors) {
        unwrapElementKeepText(ancestor)
      }
      continue
    }

    // 采样「被选区完全覆盖的外层旧高亮」—— 必须在 wrap 使 range 失效之前（A4.9 I3）
    const toStrip: HTMLElement[] = []
    for (const textNode of textNodes) {
      let n: Node | null = textNode.parentNode
      while (n && n !== root) {
        if (n.nodeType === Node.ELEMENT_NODE) {
          const el = n as HTMLElement
          if (
            el.tagName === 'SPAN' && el.style.backgroundColor &&
            isElementFullyInRange(el, range)
          ) {
            toStrip.push(el)
          }
        }
        n = n.parentNode
      }
    }

    // 逐文本节点包高亮 span —— 跨段选区绝不 extractContents（span>p 非法结构
    // 即换行/序列化回灌根因）。
    wrapRangePerTextNode(range, root, () => {
      const span = document.createElement('span')
      span.style.backgroundColor = color
      span.style.borderRadius = '2px'
      return span
    })
    for (const el of toStrip) {
      if (!el.isConnected) continue
      el.style.removeProperty('background-color')
      if (!el.getAttribute('style')) unwrapElementKeepText(el)
    }
  }

  // 保留用户选区（wave-2 复审 #2）：格式化不应清空选区/挪动视口
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
  if (isComposing) return // IME 组合期不做格式变更（wave-2 A5）
  pushUndoState(true)
  const root = editorRef.value
  if (!root) return

  // 多 range（判据 #4）：额外段+原生；无额外段时与原生选区逐字等价
  const ranges = getActionRanges().ranges
  if (ranges.length === 0) return

  for (const range of ranges) {
    if (range.collapsed) continue

    // 采样待清理的外层旧色 span（wrap 前，range 未变形）—— A4.9 I3
    const toStrip: HTMLElement[] = []
    for (const textNode of collectTextNodesInRange(range, root)) {
      let n: Node | null = textNode.parentNode
      while (n && n !== root) {
        if (n.nodeType === Node.ELEMENT_NODE) {
          const el = n as HTMLElement
          if (el.tagName === 'SPAN' && el.style.color && isElementFullyInRange(el, range)) {
            toStrip.push(el)
          }
        }
        n = n.parentNode
      }
    }

    // 逐文本节点包颜色 span —— 同 applyFontSizeToSelection，绝不 extractContents
    // 整块包裹（span>p 非法结构根因）。内层新色 span 压过外层旧色。
    wrapRangePerTextNode(range, root, () => {
      const span = document.createElement('span')
      span.style.color = color
      return span
    })
    for (const el of toStrip) {
      if (!el.isConnected) continue
      el.style.removeProperty('color')
      if (!el.getAttribute('style')) unwrapElementKeepText(el)
    }
  }
  // 保留用户选区（wave-2 复审 #2）：格式化不应清空选区/挪动视口
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

/**
 * 尾注（endnote）：在光标/选区末尾插入自动编号角标，文末生成对应定义段，
 * 光标移入定义段供用户即刻输入说明。点击角标/点击定义编号的双向跳转由
 * onClick 的锚点分支（a[href^="#"]）承担，本函数只负责建对。
 */
/**
 * 尾注全局重编号：引用首次出现序 = 显示编号（与渲染侧 preprocessEndnoteSyntax
 * 同口径），无引用的定义排其后。角标/回跳标签〔N〕同步改写（wave-2 A4/B2）。
 */
function updateEndnoteNumbering(root: HTMLElement) {
  const numById = new Map<string, number>()
  let next = 1
  root.querySelectorAll<HTMLElement>('.note-endnote-ref').forEach((el) => {
    const id = el.getAttribute('data-endnote-id') || ''
    if (id && !numById.has(id)) numById.set(id, next++)
  })
  root.querySelectorAll<HTMLElement>('.note-endnote-def').forEach((el) => {
    const id = el.getAttribute('data-endnote-id') || ''
    if (id && !numById.has(id)) numById.set(id, next++)
  })
  const apply = (el: HTMLElement, sel: string | null) => {
    const id = el.getAttribute('data-endnote-id') || ''
    const n = numById.get(id)
    if (n === undefined) return
    el.setAttribute('data-endnote-num', String(n))
    const a = (sel ? el.querySelector(sel) : el.querySelector('a')) as HTMLElement | null
    if (!a) return
    // 标签是纯 chrome（渲染器/编号器重建 find 标记）：replaceChildren 一次性
    // 归一，拆分文本节点场景（find 标记嵌入标签）不再写坏（复审 A#5）
    a.replaceChildren(document.createTextNode(`〔${n}〕`))
  }
  root.querySelectorAll<HTMLElement>('.note-endnote-ref').forEach((el) => apply(el, 'a'))
  root.querySelectorAll<HTMLElement>('.note-endnote-def').forEach((el) => apply(el, 'a.note-endnote-back'))
}

function insertEndnote() {
  if (isComposing) return // IME 组合期不做格式变更（wave-2 A5）
  const root = editorRef.value
  if (!root) return
  pushUndoState(true)

  // 自动编号 = 现有最大 endnote id + 1（编号计算不落库）
  let maxId = 0
  root.querySelectorAll<HTMLElement>('[data-endnote-id]').forEach((el) => {
    const n = parseInt(el.getAttribute('data-endnote-id') || '0', 10)
    if (!isNaN(n) && n > maxId) maxId = n
  })
  const id = maxId + 1

  const tmp = document.createElement('div')
  tmp.innerHTML = buildEndnoteRefHtml(String(id), id)
  const refEl = tmp.firstElementChild as HTMLElement

  const sel = window.getSelection()
  let inserted = false
  if (sel && sel.rangeCount > 0 && root.contains(sel.anchorNode)) {
    const anchorEl = (sel.anchorNode.nodeType === Node.TEXT_NODE
      ? (sel.anchorNode as Text).parentElement
      : (sel.anchorNode as Element)) as HTMLElement | null
    // 不可编辑部件（mermaid/echarts/复制钮）内禁插 —— insertNode 会破坏部件 DOM
    if (!anchorEl || !anchorEl.closest('[contenteditable="false"]')) {
      const range = sel.getRangeAt(0)
      // Word 式：选中文字保留，角标插在选区末尾
      range.collapse(false)
      // 光标落在旧角标文本内（文本偏移恢复的常态落点）时整体外移：
      // 新角标绝不能嵌套进旧角标/锚点内部（A4 排障实锤）
      const hostRef = (range.startContainer.nodeType === Node.TEXT_NODE
        ? (range.startContainer as Text).parentElement
        : (range.startContainer as Element))?.closest?.('.note-endnote-ref, a.note-endnote-back')
      if (hostRef) {
        range.setStartAfter(hostRef)
        range.collapse(true)
      }
      range.insertNode(refEl)
      inserted = true
    }
  }
  if (!inserted) {
    root.appendChild(refEl)
  }

  const tmpDef = document.createElement('div')
  tmpDef.innerHTML = buildEndnoteDefHtml(String(id), id, '')
  const defEl = tmpDef.firstElementChild as HTMLElement
  root.appendChild(defEl)

  // 显示编号 = 文档序出现位；插入后全部角标/定义就地重排（wave-2 A4：
  // 新旧并存时旧角标不同步会出现重复编号）
  updateEndnoteNumbering(root)

  // 光标移入定义段（回跳编号之后），用户即刻输入说明文字
  const defRange = document.createRange()
  defRange.setStart(defEl, defEl.childNodes.length)
  defRange.collapse(true)
  if (sel) {
    sel.removeAllRanges()
    sel.addRange(defRange)
  }
  defEl.scrollIntoView({ behavior: 'smooth', block: 'center' })

  onInput()
  nextTick(() => pushCurrentState())
}

function applySuperscript() {
  toggleScriptTag('SUP')
}

function applySubscript() {
  toggleScriptTag('SUB')
}

/** 在 (node, offset) 处拆分 el（node 在 el 内）：el 留左半，返回拆出的右半元素。 */
function splitElementAt(el: HTMLElement, node: Node, offset: number): HTMLElement | null {
  let target: Node | null = node
  if (target.nodeType === Node.TEXT_NODE) {
    const t = target as Text
    if (offset <= 0) {
      target = t
    } else if (offset < t.length) {
      target = t.splitText(offset)
    } else {
      target = t.nextSibling
      while (target && target.nodeType === Node.TEXT_NODE && !(target.textContent || '')) {
        target = target.nextSibling
      }
      if (!target) return null
    }
  } else {
    target = (target as Element).childNodes[offset] ?? null
    if (!target) return null
  }
  // target 是「右半」首节点；沿 target→el 逐层克隆搬移
  const chain: HTMLElement[] = []
  // 边界落在**元素间隙**时（target 本身是元素），克隆链必须从它的父级开始 ——
  // 否则 splitNode 会把这个元素自己搬进它的克隆里（inner.parentNode === clone），
  // 随后 insertBefore 抛 HierarchyRequestError，且此时兄弟节点已被摘走（静默丢内容）。
  let cur: Node | null = target.nodeType === Node.ELEMENT_NODE ? target.parentNode : target
  while (cur && cur !== el) {
    if (cur.nodeType === Node.ELEMENT_NODE) chain.push(cur as HTMLElement)
    cur = cur.parentNode
  }
  if (cur !== el) return null
  let splitNode: Node = target
  for (const inner of chain) {
    const clone = inner.cloneNode(false) as HTMLElement
    let n: Node | null = splitNode
    while (n) {
      const next = n.nextSibling
      clone.appendChild(n)
      n = next
    }
    inner.parentNode!.insertBefore(clone, inner.nextSibling)
    splitNode = clone
  }
  const cloneEl = el.cloneNode(false) as HTMLElement
  let n: Node | null = splitNode
  while (n) {
    const next = n.nextSibling
    cloneEl.appendChild(n)
    n = next
  }
  el.parentNode!.insertBefore(cloneEl, el.nextSibling)
  return cloneEl
}

/**
 * 范围级解包（判据 #30）：只把 range 覆盖的段落从 tagName 元素里剥出来，
 * 元素其余部分保持原格式 —— 旧实现 unwrapElement 整元素解包，选中追加文本
 * 取消上标会连带把原有上标一起还原。
 */
function unwrapRangeInTag(range: Range, tagName: string, root: HTMLElement) {
  const sel = Array.from(root.querySelectorAll(tagName)) as HTMLElement[]
  for (const el of sel) {
    if (!el.isConnected) continue
    // 数据载体/渲染器内部不参与解包（引文角标、尾注 ref、公式、图表、代码块）
    if (el.closest(PROTECTED_SELECTOR)) continue
    try {
      if (!range.intersectsNode(el)) continue
    } catch {
      continue
    }
    let target: HTMLElement = el
    if (el.contains(range.startContainer)) {
      const right = splitElementAt(el, range.startContainer, range.startOffset)
      if (right) {
        target = right
      } else {
        // 拆不出右半 = range.start 落在 el 内容末尾之后 —— el 整体在 range 之前，
        // 不得解包（评审 B：相邻 sup 边界触碰过包，如 <sup>ab</sup><sup>cd</sup>
        // 从 (ab,2) 选到 cd 不许伤及 ab）。
        continue
      }
    } else if (!el.contains(range.endContainer)) {
      // 两端边界都不在 el 内：仅当 el 整体严格位于 range 之内才解包 ——
      // intersectsNode 含「仅触边」（评审 A#2：<sup>a</sup><sup>b</sup> 选 a
      // 不得连带解包 b）。
      const elRange = document.createRange()
      elRange.selectNodeContents(el)
      const strictlyInside =
        range.compareBoundaryPoints(Range.START_TO_START, elRange) <= 0 &&
        range.compareBoundaryPoints(Range.END_TO_END, elRange) >= 0
      if (!strictlyInside) continue
    }
    if (target.contains(range.endContainer)) {
      // 选区终点恰好落在本元素首个文本节点 offset 0 = 元素整体在选区之后，
      // 不得再拆（否则会把未选中的后半段也解包 —— A4.9 评审 I1 实测：只选
      // 中间一个字时右侧 span 被连带清除）。
      const endAtStart = range.endContainer.nodeType === Node.TEXT_NODE && range.endOffset === 0
      if (endAtStart) continue // 元素整体在选区之后
      splitElementAt(target, range.endContainer, range.endOffset)
    }
    unwrapElementKeepText(target)
  }
  // 清理拆分残留的空元素（仅限 tagName，空元素无用户内容；保护子树跳过 ——
  // KaTeX 的 strut/pstrut/frac-line 恰是空 span，无差别回收会把公式骨架吃掉）
  root.querySelectorAll(tagName).forEach(el => {
    const e = el as HTMLElement
    if (e.closest(PROTECTED_SELECTOR)) return
    if (!e.textContent && !e.querySelector('img,br')) unwrapElementKeepText(e)
  })
}

/**
 * 上/下标范围级切换（判据 #30）：
 * 方向判定 = 选区内全部文本已在 tag 内 → 拆范围解包（部分还原）；否则 → 只包
 * 尚不在 tag 内的文本（不叠加嵌套）。折叠选区不动作（旧语义保留）。
 */
/**
 * 空行内残壳清理（判据 #38）：splitElementAt/切换操作会留下空 strong/em/u/s/
 * span/sup/sub，序列化成 `****` 级噪音。无内容（文本/嵌入）即解包。
 */
function cleanupEmptyInline(root: HTMLElement) {
  root.querySelectorAll('strong,em,u,s,span,sup,sub,a').forEach(el => {
    const e = el as HTMLElement
    if (e.hasAttribute('data-tex') || e.hasAttribute('data-mermaid-id') || e.hasAttribute('data-endnote-id') || e.hasAttribute('data-cite-index')) return
    // 数据载体/渲染器内部一律不动：KaTeX 的排版骨架恰是**空 span**（strut/pstrut/
    // frac-line/mspace），无差别回收会把公式打散（A4.9 双评审 Critical 实测：
    // 清除格式把全篇公式的高度骨架吃掉，指数错位且不回自愈）。
    if (e.closest(PROTECTED_SELECTOR)) return
    if (!e.textContent && !e.querySelector('img,br,video,audio,iframe,math,pre,input,hr,svg,.math-editable,.katex')) unwrapElementKeepText(e)
  })
}

function toggleScriptTag(tagName: 'SUP' | 'SUB') {
  if (isComposing) return // IME 护栏（wave-3 #1）
  // 遗留 #2：先取选区快照 —— DOM 手术（拆分/重包/空壳回收）会把 Range 端点重映射到
  // 新元素起点（实测上/下标后折叠到 sup 内首字符前，随后键入被吸进上标）。
  const snapshot = captureSelectionSnapshot()
  pushUndoState(true)
  const root = editorRef.value
  if (!root) return

  // 多 range（判据 #4）：有额外段时按「额外段+原生」解析；无则原生选区（行为不变）
  const action = getActionRanges()
  const ranges = action.ranges
  if (ranges.length === 0) return

  // 尾注/引文角标是数据载体（data-endnote-id / [N] 台账），任何方向都不许碰
  // （评审 B：选中 ref 触发 unwrap 会永久毁掉尾注链路）。
  const isRefSup = (t: Text): boolean => {
    const host = t.parentElement
    return !!host && !!host.closest('.citation-ref, .note-endnote-ref, .note-endnote-back')
  }

  // 多 range 倒序处理（判据 #42）：后创建的 range 不受先前 DOM 位移影响，
  // 倒序保证每个 range 处理时其边界仍有效。
  let mutated = false
  let wrappedHost: HTMLElement | null = null
  for (let ri = ranges.length - 1; ri >= 0; ri--) {
    const range = ranges[ri]
    if (range.collapsed) continue

    const texts = collectTextNodesInRange(range, root)
      .filter(t => (t.textContent || '').trim().length > 0 && !isRefSup(t))
    if (!texts.length) continue
    const allIn = texts.every(t => !!findAncestorTag(t, root, tagName))

    if (allIn) {
      mutated = true
      unwrapRangeInTag(range, tagName, root)
    } else {
      // 复用 wrapRangePerTextNode：逐节点夹取选中片段（不得整节点过包）+
      // 跳过 contenteditable=false 部件（评审 B：x2+y3 选 2 不得连带包 x2+y3，
      // mermaid/echarts 部件内部不得进 sup）。
      const created = wrapRangePerTextNode(range, root, () => document.createElement(tagName.toLowerCase()))
      // mutated 以实际创建的元素为准：纯不可编辑部件选区（图表标签岛）不产生包裹，
      // 不得误判为已改树（A4.9 复审 M1）
      if (created.length > 0) {
        mutated = true
        wrappedHost = created[created.length - 1]
      }
      // 混合选区（部分已在 tag 内）会形成嵌套同名元素 —— 解包内层归一，
      // 文本仍保持格式、DOM 不叠层。
      root.querySelectorAll(tagName + ' ' + tagName).forEach(inner => {
        unwrapElementKeepText(inner as HTMLElement)
      })
    }
  }

  // 保留用户选区（wave-2 复审 #2）：格式化不应清空选区/挪动视口
  cleanupEmptyInline(root) // 判据 #38：拆分/切换残壳
  // 遗留 #2：仅当这次操作真的改过树时重落光标（折叠 / 纯角标选区是 no-op，
  // 重落会把光标从当前段落拽走 —— A4.9 评审 A-I1/B-I2 实测）
  if (mutated && !action.multi) restoreCaretAfterFormat(snapshot, wrappedHost)
  if (action.multi) renderMultiHighlights()
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
  // macOS 的 Ctrl+单击 = 原生右键：多 range 手势（ctrl/meta mousedown 起、mouseup 止）
  // 期间的 contextmenu 一律抑制 —— 该手势属于判据 #3 的选择操作。用 multiGesture 标志
  // 而非 ev.ctrlKey 判定：macOS 真右键的 contextmenu 也带 ctrlKey=true，会误伤表格菜单。
  if (multiGesture) {
    ev.preventDefault()
    return
  }
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

/** 块级插入候选标签（block-in-inline 防护的切块范围；LI 不切=保列表结构）。 */
const BLOCK_INSERT_TAGS = new Set(['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE'])

/**
 * 块级片段插入前把光标所在块级元素切开（遗留项 A：块级公式/代码块插进段落中
 * 会产生 div-in-p 的 block-in-inline DOM）。切开后插入点位于两块之间。
 */
function splitBlockForBlockInsert(range: Range) {
  const editor = editorRef.value
  if (!editor) return
  // F3: 光标落在行内公式内时把切点吸附到公式边界——extractContents 深拷贝
  // 半选 .math-editable 会产生两份 data-tex（公式重复）。
  const startEl = range.startContainer.nodeType === Node.ELEMENT_NODE
    ? (range.startContainer as HTMLElement)
    : (range.startContainer.parentElement || null)
  const mathAncestor = startEl && startEl.closest ? startEl.closest('.math-editable') : null
  if (mathAncestor && editor.contains(mathAncestor)) {
    range.setStartAfter(mathAncestor)
    range.collapse(true)
  }
  let node: Node | null = range.startContainer
  let block: HTMLElement | null = null
  while (node && node !== editor) {
    if (node.nodeType === Node.ELEMENT_NODE && BLOCK_INSERT_TAGS.has((node as HTMLElement).tagName)) {
      block = node as HTMLElement
      break
    }
    node = node.parentNode
  }
  if (!block) return
  const after = document.createRange()
  after.setStart(range.startContainer, range.startOffset)
  if (block.lastChild) after.setEndAfter(block.lastChild)
  else after.setEnd(block, block.childNodes.length)
  const frag = after.extractContents()
  // F4: 可见内容判空（extractContents 常留空文本节点，childNodes.length 会骗人）
  const hasVisible = (root: Node): boolean => {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_ALL)
    let n: Node | null = w.currentNode
    while (n) {
      if (n.nodeType === Node.ELEMENT_NODE) {
        const tag = (n as HTMLElement).tagName
        if (tag === 'IMG' || tag === 'BR' || tag === 'TABLE' || tag === 'SVG' || tag === 'MATH') return true
      }
      if (n.nodeType === Node.TEXT_NODE && (n.textContent || '').replace(/\u200b/g, '').length) return true
      n = w.nextNode()
    }
    return false
  }
  const fragVisible = hasVisible(frag)
  const blockVisible = hasVisible(block)
  if (!fragVisible && !blockVisible) return
  if (!fragVisible) {
    // 光标在块尾/块内容已尽——无需新块，插入点即块后
    range.setStartAfter(block)
    range.collapse(true)
    return
  }
  const newBlock = block.cloneNode(false) as HTMLElement
  newBlock.removeAttribute('id')  // F5: 拷贝块不得复制 id（渲染锚点分裂）
  newBlock.removeAttribute('name')
  newBlock.appendChild(frag)
  block.parentNode!.insertBefore(newBlock, block.nextSibling)
  if (!blockVisible) {
    // 光标在块首——左半已空：去掉空壳，插入点即原位置（不留 <p></p> 残渣）
    block.parentNode!.removeChild(block)
    range.setStartBefore(newBlock)
  } else {
    range.setStartAfter(block)
  }
  range.collapse(true)
}

/** 通用片段插入（恢复已保存选区 → 光标处插入 html → 序列化）。blockLevel=块级形态（先切块）。 */
function insertSnippetHtml(html: string, blockLevel = false) {
  // 片段插入改文本 ⇒ 额外段失效（评审 N1）
  clearMultiRanges()
  if (!editorRef.value) return
  pushUndoState(true)
  let inserted = false
  if (savedEditorSelection) {
    restoreEditorSelection()
  }
  const sel = window.getSelection()
  if (sel && sel.rangeCount > 0 && editorRef.value.contains(sel.anchorNode)) {
    const range = sel.getRangeAt(0)
    // F6: 先删选区（替换语义）再切块——切块会把 range 折叠到块边界
    range.deleteContents()
    // 光标在 pre/code 内时吸出到块后：widget 进代码块会被 textContent 泄漏成字面
    const startEl2 = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? (range.startContainer as HTMLElement)
      : (range.startContainer.parentElement || null)
    const preAncestor = startEl2 && startEl2.closest ? startEl2.closest('pre') : null
    if (preAncestor && editorRef.value!.contains(preAncestor)) {
      range.setStartAfter(preAncestor)
      range.collapse(true)
    }
    if (blockLevel) splitBlockForBlockInsert(range)
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
    const tmp = document.createElement('div')
    tmp.innerHTML = html
    while (tmp.firstChild) {
      editorRef.value.appendChild(tmp.firstChild)
    }
  }
  onInput()
  nextTick(() => {
    pushCurrentState()
    attachMathEditListeners(editorRef.value!)
  })
}

/** 插入 LaTeX 公式（行内 $…$ / 块级 $$…$$ 由 displayMode 决定）；data-tex 保原文可再编辑。 */
function insertMath(tex: string, displayMode: boolean) {
  // 块级公式（displayMode）插前切块；行内保持原位
  insertSnippetHtml(renderMathSafe(tex, displayMode), displayMode)
}

/** 插入代码块（pre>code，序列化为 ```lang 围栏；无渲染壳 chrome 防泄漏）。 */
function insertCodeBlock(code: string, lang: string) {
  insertSnippetHtml(buildCodeBlockHtml(code, lang), true)
}

function insertLink() {
  const raw = window.prompt('输入链接地址（留空取消）：')
  if (!raw) return
  const value = raw.trim()
  if (!value) return
  const href = /^(https?:|mailto:|#|\/)/i.test(value) ? value : `https://${value}`
  document.execCommand('createLink', false, href)
  onInput(true)
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

watch([() => props.modelValue, endnoteEnabled], async ([newValue]) => {
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
  await rerenderFromModel(newValue)
  setTimeout(() => {
    isExternalContentUpdate = false
    if (editorRef.value && undoStack.value.length === 0) {
      pushUndoState(true)
    }
  }, 350)
}, { immediate: true })

/**
 * 整篇重渲染（遗留 #2 的第二个失选区通道）：`renderedContent` 经 v-html 重建整棵子树，
 * 旧 Range 端点随旧文本节点一起失效。恢复门禁 = 文本未变 + 编辑器持焦点 + 非组合期。
 * `__rerenderFromModel`（onMounted 暴露）复用同一函数供 E2E 验证判据 #70。
 */
async function rerenderFromModel(value: string | null | undefined, snapshotOverride?: EditorSelectionSnapshot | null) {
  if (!editorRef.value) return
  // 整篇重渲染 = 外部内容更新 ⇒ 虚拟多 range 会话结束（判据 #3 清除时机）
  clearMultiRanges()
  const snapshot = snapshotOverride === undefined ? captureSelectionSnapshot() : snapshotOverride
  suppressInputDepth.value++
  suppressChangeEmit = true
  try {
    // [N] 引用角标渲染为上标；htmlToMarkdown 的 citation-ref 分支会把
    // <sup class="citation-ref">[N]</sup> 原样还原为 [N]，往返守恒。
    renderedContent.value = addCitationSuperscripts(renderMarkdownToHtml(value || ''), true)
    await nextTick()
    if (editorRef.value) {
      // mermaid/echarts 之前恢复：图表渲染会把源码块替换成 SVG（文本随之改变），
      // 放在后面会让「同一篇文档」判定失真。
      restoreSelectionAfterRender(snapshot)
      normalizeEmptyListItems(editorRef.value)
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
    // 失焦兜底自愈（判据 #2）：浏览器在 blur 时提交组合，compositionend 若因节点
    // detach 丢失，focusout 是最后的复位通道
    editorRef.value.addEventListener('focusout', onEditorFocusOut)
    // 手势滞留清理（评审 A3）：窗口失焦时 ctrl 手势的 mouseup 可能永不送达
    window.addEventListener('blur', onWindowBlurAbortGesture)
  }
  // Expose for testing via DOM element (more reliable in Playwright)
  if (editorRef.value) {
    const el = editorRef.value as any
    el.__undo = undo
    el.__redo = redo
    el.__getUndoStackSize = () => undoStack.value.length
    el.__pushUndoState = pushUndoState
    el.__applyHighlight = applyHighlightToSelection
    el.__applyFontSize = applyFontSizeToSelection
    // 遗留 #2 测试点（与 __undo/__applyHighlight 同风格的 DOM 挂点）
    el.__execCommand = (cmd: string, value?: string) => execCommand(cmd, value)
    // 判据 #70 的 E2E 测试点：走与 watch 完全相同的重渲染管线（非桩）。
    // 同一 HTML 串 Vue 不会重设 innerHTML（选区自然存活），故先清空子树逼出真重建
    // —— 那才是「v-html 重渲染丢选区」的真实通道；快照在清空之前取。
    el.__rerenderFromModel = async () => {
      // 先落地在途序列化：否则 props.modelValue 落后一个微任务，重建会吞掉最新键入
      await flushPendingSerialization()
      const snap = captureSelectionSnapshot()
      renderedContent.value = ''
      await nextTick()
      await rerenderFromModel(props.modelValue, snap)
    }
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
    editorRef.value.removeEventListener('focusout', onEditorFocusOut)
  }
  // 多 range 会话收尾（评审 A11）：卸载清 document 级监听 + 全局高亮通道
  window.removeEventListener('blur', onWindowBlurAbortGesture)
  abortMultiGesture()
  clearMultiRanges()
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
  // 尾注角标/定义整宿主可点（wave-2 C3）：移动端上标 `<a>` 命中盒极小，
  // 点击常落在 sup/p 上 —— 按 data-endnote-id 直接解析对端目标。
  const endnoteHost = target.closest?.('.note-endnote-ref, .note-endnote-def') as HTMLElement | null
  if (!anchor && endnoteHost && root.contains(endnoteHost)) {
    const eid = endnoteHost.getAttribute('data-endnote-id') || ''
    const isRef = endnoteHost.classList.contains('note-endnote-ref')
    const targetEl = eid
      ? (root.querySelector(isRef ? `#note-endnote-def-${CSS.escape(eid)}` : `#note-endnote-ref-${CSS.escape(eid)}`) as HTMLElement | null)
      : null
    if (targetEl) {
      ev.preventDefault()
      targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' })
      targetEl.classList.remove('note-endnote-flash')
      void targetEl.offsetWidth
      targetEl.classList.add('note-endnote-flash')
    }
    return
  }
  if (anchor && root.contains(anchor)) {
    const hash = anchor.getAttribute('href') || ''
    const id = hash.replace(/^#/, '')
    if (id) {
      try {
        const decoded = decodeURIComponent(id)
        const el = root.querySelector(`#${CSS.escape(decoded)}`) as HTMLElement | null
        if (el) {
          ev.preventDefault()
          // scrollIntoView 自适应真实滚动容器（编辑器内滚 vs 移动端整页滚），
          // scrollPreviewToElement 只滚编辑器自身 —— 移动端跳转失效根因（wave-2 C3）
          el.scrollIntoView({ behavior: 'smooth', block: 'center' })
          // 尾注双向跳转：到达目标后闪烁提示（角标↔定义互跳的视觉闭环）
          if (el.classList.contains('note-endnote-ref') || el.classList.contains('note-endnote-def')) {
            el.classList.remove('note-endnote-flash')
            // 触发 reflow 以便重复点击也能重放动画
            void el.offsetWidth
            el.classList.add('note-endnote-flash')
          }
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
  stripFindStyleSpans(clone)
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

/**
 * 解析「贴着 sup/sub 的折叠光标」边界（判据 #30/#34 与遗留 #2）：
 * ①光标在 sup/sub 内（返回 atEnd/atStart）②光标紧贴其外侧文本节点首/尾。
 * 其余返回 null（中部输入保持格式，不干预）。
 */
function resolveScriptBoundary(range: Range, root: HTMLElement): { host: HTMLElement; atEnd: boolean; atStart: boolean } | null {
  const ancHost = (findAncestorTag(range.startContainer, root, 'SUP') ||
    findAncestorTag(range.startContainer, root, 'SUB')) as HTMLElement | null
  if (ancHost && range.startContainer.nodeType === Node.TEXT_NODE) {
    const pre = document.createRange()
    pre.selectNodeContents(ancHost)
    try { pre.setEnd(range.startContainer, range.startOffset) } catch { return null }
    const before = pre.toString()
    const full = ancHost.textContent || ''
    const atEnd = before.length >= full.length
    const atStart = before.length === 0
    if (atEnd || atStart) return { host: ancHost, atEnd, atStart }
    return null
  }
  if (ancHost) return null
  const isScript = (n: Node | null | undefined): n is HTMLElement =>
    !!n && n.nodeType === Node.ELEMENT_NODE && /^(SUP|SUB)$/.test((n as HTMLElement).tagName)
  if (range.startContainer.nodeType === Node.TEXT_NODE) {
    // 贴格式元素**外侧**的折叠光标（遗留 #2 实测）：Blink 把 <sup>/<sub> 当
    // vertical-align 样式区间，紧邻其右的文本首字符前键入会被吸进元素
    // （<sup>甲乙</sup>|丙丁 + X → <sup>甲乙X</sup>）；左侧镜像同理。
    const t = range.startContainer as Text
    const prev = t.previousSibling
    const next = t.nextSibling
    if (range.startOffset === 0 && isScript(prev)) return { host: prev, atEnd: true, atStart: false }
    if (range.startOffset === (t.textContent || '').length && isScript(next)) return { host: next, atEnd: false, atStart: true }
    return null
  }
  // 元素级光标（块尾/元素间的 (el, n) 位置，Chromium 常见）——同样会被吸进相邻格式
  // 元素（A4.9 复审 M2：文末上标后键入仍落进 sup）。
  const kids = range.startContainer.childNodes
  const before = kids[range.startOffset - 1]
  const after = kids[range.startOffset]
  if (isScript(before)) return { host: before, atEnd: true, atStart: false }
  if (isScript(after)) return { host: after, atEnd: false, atStart: true }
  return null
}

/**
 * IME 组合文本的边界结算（遗留 #2 · A4.9 I1）：组合期浏览器把组合串直接塞进
 * 相邻的 sup/sub（实测 CDP `insertCompositionText` → `<sup>甲乙你</sup>`），
 * 普通键击护栏因 isComposing 早退覆盖不到；逐次接管组合事件会与浏览器 IME 状态
 * 打架（实测导致提交重复一个字）。折中做法：组合期完全交给浏览器，`compositionend`
 * 时把「被吸进格式元素的那部分文本」搬回元素外侧，并把光标放到搬出的文本之后。
 */
let compositionBoundary: { host: HTMLElement; atEnd: boolean; len: number } | null = null

/** 从 host 的头部/尾部文本节点裁掉 n 个字符（保留内部标注结构）。 */
function trimHostText(host: HTMLElement, side: 'start' | 'end', n: number) {
  let left = n
  const nodes: Text[] = []
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT)
  let cur: Node | null
  while ((cur = walker.nextNode())) nodes.push(cur as Text)
  const ordered = side === 'start' ? nodes : nodes.slice().reverse()
  for (const t of ordered) {
    if (left <= 0) break
    const len = t.data.length
    if (len === 0) continue
    if (len <= left) {
      left -= len
      t.data = side === 'start' ? t.data.slice(len) : t.data.slice(0, 0)
    } else {
      t.data = side === 'start' ? t.data.slice(left) : t.data.slice(0, len - left)
      left = 0
    }
  }
}

function settleCompositionBoundary() {
  const b = compositionBoundary
  compositionBoundary = null
  const root = editorRef.value
  if (!b || !root || !b.host.isConnected) return
  const full = b.host.textContent || ''
  const grew = full.length - b.len
  if (grew <= 0) return // 取消组合 / 无净输入
  const moved = b.atEnd ? full.slice(b.len) : full.slice(0, grew)
  if (!moved) return
  trimHostText(b.host, b.atEnd ? 'end' : 'start', grew)
  const tn = document.createTextNode(moved)
  if (b.atEnd) b.host.parentNode?.insertBefore(tn, b.host.nextSibling)
  else b.host.parentNode?.insertBefore(tn, b.host)
  const sel = window.getSelection()
  if (sel) {
    const r = document.createRange()
    r.setStart(tn, tn.data.length)
    r.collapse(true)
    sel.removeAllRanges()
    sel.addRange(r)
  }
  onInput()
}

/**
 * pre-change 快照（wave-2 A1）：新一轮输入开始前（beforeinput，浏览器尚未改树）
 * 压入变更前 DOM+选区 —— 撤销链才是「回到键入前」而不是「回到上一拍之后」。
 * 突发判定 = 无待决去抖（inputDebounceTimer 为空 = 新一轮编辑）。
 */
function onBeforeInput(e?: InputEvent) {
  // 陈旧组合标志自愈（判据 #2）：浏览器在本事件里权威报「非组合」且非组合系 inputType
  // ⇒ 标志必陈旧（compositionend 丢失路径），先复位再走正常护栏；真组合期 e.isComposing=true 不触发。
  if (isComposing && shouldHealComposingFromInput(e?.inputType, e?.isComposing)) healStaleComposing()
  // 文本增删使额外段失效（判据 #3 清除时机；ADR mr2 边界：跨段替换不做，直接结束会话）。
  // 注意 execCommand 样式命令的 inputType（bold/italic/…）不在此列 —— 多段格式动作自身管理。
  if (extraSpans.value.length > 0 && e && /^(insert|delete|history)/.test(e.inputType)) clearMultiRanges()
  if (isComposing) return // IME 组合期不动作（wave-3 #1）
  // 撤销执行期/外部重渲染期一律不动作（评审 A#7：护栏不得在 isExternalContentUpdate
  // 窗口内强制改树并把瞬态渲染态压进撤销栈）—— 但必须仍在 inputDebounceTimer
  // 早退之前（RED 复现：输入突发期 beforeinput 被去抖早退吞掉，护栏整段失效）。
  if (isUndoing || isExternalContentUpdate) return
  // 上/下标边界护栏（判据 #30）：折叠光标贴着 sup/sub 首/末边界输入时，浏览器
  // 会把新输入吸进元素（追加文本默认带上标/下标）。浏览器插入位在 beforeinput
  // 前已定，改选区无效 —— 唯一可靠做法是 preventDefault 后在边界外手动插入。
  // 元素中部输入保持格式不变（护栏只认首/末边界）。
  // 注意：护栏必须位于 inputDebounceTimer 早退之前 —— 输入突发期 beforeinput
  // 会被去抖早退整段吞掉（RED 复现：ababc 上标后键入 xy 仍被吸进 sup）。
  try {
    // insertReplacementText（听写/自动更正）与 insertHTML（代码片段等 execCommand
    // 通道）与 insertText 同受边界护栏约束（判据 #34）。
    const isGuarded = !!e && (e.inputType === 'insertText' || e.inputType === 'insertReplacementText' || e.inputType === 'insertHTML')
    // insertHTML 负载过 sanitizePastedHtml（评审 A I7/B I2：边界拦截是外部 HTML 的
    // 潜在 XSS 汇聚点，与粘贴路径同标准防御）。负载位置：Input Events 规范里
    // insertHTML 的 HTML 在 e.data；部分实现走 dataTransfer —— 双取（评审 B I1）。
    const payloadText = isGuarded && e!.inputType !== 'insertHTML' ? (e!.data || '') : ''
    const payloadHtml = isGuarded && e!.inputType === 'insertHTML'
      ? (sanitizePastedHtml(e!.data || e!.dataTransfer?.getData('text/html') || '') || '')
      : ''
    if (isGuarded && !e!.isComposing && (payloadText || payloadHtml)) {
      const sel = window.getSelection()
      if (sel && sel.rangeCount === 1 && sel.isCollapsed && editorRef.value) {
        const r = sel.getRangeAt(0)
        const b = resolveScriptBoundary(r, editorRef.value)
        if (b) {
          const { host, atEnd, atStart } = b
          if (atEnd || atStart) {
            e!.preventDefault() // 先定干预（评审 B M2：插入中途抛错不得再走默认二次插入）
            pushUndoState()
            const nr = document.createRange()
            if (atEnd) nr.setStartAfter(host)
            else nr.setStartBefore(host)
            nr.collapse(true)
            if (payloadText) {
              const tn = document.createTextNode(payloadText)
              nr.insertNode(tn)
              nr.setStart(tn, tn.length)
            } else {
              const frag = nr.createContextualFragment(payloadHtml)
              const last = frag.lastChild
              nr.insertNode(frag)
              if (last) nr.setStartAfter(last)
            }
            nr.collapse(false)
            sel.removeAllRanges()
            sel.addRange(nr)
            e!.preventDefault()
            onInput()
            return
          }
        }
      }
    }
  } catch { /* 护栏失败则回落浏览器默认输入 */ }
  if (inputDebounceTimer) return
  pushUndoState()
}

function onInput(force: boolean | Event = false) {
  if (!editorRef.value || suppressInputDepth.value > 0) return

  // 需求 3：Enter 切块 / 块边界处 Backspace 合块会在标题内留下浏览器样式伪影
  // （<span style="font-size:…">，见 composables/headingTypography.ts）。
  // 双重门槛：inputType ∈ 切块/合块集合 **且** onKeydown 判定确实切/合了块。
  // 后者不可省 —— deleteContentBackward 也覆盖"块中间删一个字符"，那里的字号
  // 可能是用户用字号按钮显式设置的，不该被抹掉（评审 A4）。
  const nativeEvent = typeof force === 'object' && force !== null ? force as InputEvent : null
  if (nativeEvent) {
    // 陈旧组合标志自愈（判据 #2，与 onBeforeInput 双通道：beforeinput 可能被护栏吞掉）
    if (isComposing && shouldHealComposingFromInput(nativeEvent.inputType, nativeEvent.isComposing)) healStaleComposing()
    // 任何 native input 都消费掉标记（匹配与否都要清）—— 标记的寿命就是
    // 「keydown 之后的那一次 input」，绝不跨事件存活（评审复审 New-1）。
    const shouldNormalize = shouldNormalizeHeadingAfterInput(nativeEvent.inputType) && pendingHeadingNormalize
    pendingHeadingNormalize = false
    if (shouldNormalize) normalizeHeadingBlocksAroundCaret()
  }

  // DOM-derived undo snapshots and find highlights stay synchronous; only the
  // serialization is deferred.
  // pre-change 基线由 onBeforeInput 在突变前压入（wave-2 A1）；
  // 此处不再补「空栈首拍」事后快照（那会把基线锚在变更后且常无选区）。
  if (!isUndoing && !isExternalContentUpdate) {
    scheduleInputUndo()
  }

  // Update highlights to reflect any content changes while find bar is active
  if (props.findActive && props.findQuery) {
    updateFindHighlights()
  } else if (editorRef.value?.querySelector('[data-find-match="true"]')) {
    // 查找未激活（或无查询词）却残留搜索高亮标记 —— 无条件清除。
    // 不用存在位旗标：粘贴/innerHTML 引入的标记不经 updateFindHighlights，
    // 旗标漏报会让污染永驻（wave-3 C1-8 实证后回退，正确性优先）。
    removeFindHighlights()
  }

  // 文本可能已变：额外段失效化守卫（判据 #3；无额外段时 O(1) 早退）。
  // 长度 ≠ 提交基线即结束会话 —— 覆盖表格删行列/外部公式编辑等不走 beforeinput 的手术面（评审 N1）。
  pruneExtraSpans()
  // 程序化内容手术总闸（评审 A5）：onInput(true) 调用面（重编号/对齐/图片媒体等）
  // 即使不改文本长度也结束会话（块结构语义已变）。
  if (force === true && extraSpans.value.length > 0) clearMultiRanges()

  scheduleSerialize(!!force)
}

/** 切块/合块之后：归一化光标块与前一兄弟里的标题（Backspace 合并目标）。 */
let pendingHeadingNormalize = false

/** 光标是否位于当前块的文本首/尾（只有这里删字符才会真正合块）。 */
function isCaretAtBlockEdge(edge: 'start' | 'end'): boolean {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return false
  const range = sel.getRangeAt(0)
  if (!range.collapsed) return false
  const root = editorRef.value
  if (!root) return false
  const block = getCurrentBlockElement()
  if (!block) return false
  const probe = document.createRange()
  probe.selectNodeContents(block)
  if (edge === 'start') {
    probe.setEnd(range.startContainer, range.startOffset)
    return probe.toString().length === 0
  }
  probe.setStart(range.endContainer, range.endOffset)
  return probe.toString().length === 0
}

function normalizeHeadingBlocksAroundCaret() {
  const root = editorRef.value
  if (!root) return
  const block = getCurrentBlockElement()
  if (!block) return
  const prev = block.previousElementSibling as HTMLElement | null
  normalizeHeadingBlocksAround(block as HTMLElement, prev)
}

/**
 * Controlled Backspace merge at the start of a heading: merge the heading's
 * text into the previous block. If the previous block is a paragraph, the
 * merged text becomes body text (Chromium's heading-style span artifact is
 * stripped); if it is a heading, the merged text stays in that heading and
 * the heading's existing user typography is preserved. The caret stays at
 * the merge point.
 */
function mergeHeadingIntoPrevious(block: HTMLElement, prev: HTMLElement): boolean {
  // blockquote 的合块目标是其末块（原生语义）；直接追加裸行内节点会破坏
  // 引用的块结构并可能在 markdown 往返中丢字。
  const target = prev.tagName === 'BLOCKQUOTE'
    ? (() => {
        const last = prev.lastElementChild as HTMLElement | null
        return last && MERGE_TARGET_TAGS.has(last.tagName) ? last : prev
      })()
    : prev
  if (!MERGE_TARGET_TAGS.has(target.tagName)) return false
  // Strip Chromium's heading-style span artifact from the heading content
  // before moving it; preserve user-set px sizes and colors.
  stripHeadingArtifactsInBlock(block)
  const mergeIndex = target.childNodes.length
  const frag = document.createDocumentFragment()
  flattenBlockContent(block, frag)
  Array.from(frag.childNodes).forEach((n) => target.appendChild(n))
  block.remove()
  const range = document.createRange()
  range.setStart(target, mergeIndex)
  range.collapse(true)
  const sel = window.getSelection()
  sel?.removeAllRanges()
  sel?.addRange(range)
  return true
}

// Find/replace highlight functions
// find 配色（与 .find-match / .find-match-current 的 CSS 值一致）——搜索期瞬态样式，
// 不属于内容。Chrome 的 typingStyle 会把刚拆掉的标记计算样式以**内联**形式续给
// 随后键入的无关文字（wave-2 复现实证：键入首字符即出现 rgba(255,140,0,0.7) span），
// 只认 [data-find-match] 的清理器永远漏掉它。凡清理/出快照一律剥掉。
const FIND_PALETTE_COLORS = ['rgba(255, 200, 0, 0.4)', 'rgba(255, 140, 0, 0.7)']
// find 标记存在位（wave-2 性能）：onInput 每键不再 querySelector 全树
let findMarksPresent = false

function stripFindStyleSpans(root: HTMLElement) {
  root.querySelectorAll<HTMLElement>('span[style]').forEach(span => {
    const bg = (span.style.backgroundColor || '').replace(/\s+/g, '')
    const hit = FIND_PALETTE_COLORS.some(c => bg === c.replace(/\s+/g, ''))
    if (!hit) return
    span.style.removeProperty('background-color')
    span.style.removeProperty('border-radius')
    if (!span.getAttribute('style')) unwrapElementKeepText(span)
  })
}

function removeFindHighlights() {
  if (!editorRef.value || isComposing) return // IME 组合期不动树（wave-3 #1）
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
    stripFindStyleSpans(editorRef.value)
    findMarksPresent = false
  } finally {
    suppressInputDepth.value--
  }
}

function updateFindHighlights() {
  if (!editorRef.value || isComposing) return // IME 组合期不动树（wave-3 #1）
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
        findMarksPresent = true
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
    // 空文本节点贡献 0 长度却会「吞掉」偏移 0 的归属（落进空节点后选区
    // 语义全错：字号读数/格式命令找错祖先）—— 解析一律跳过（wave-2 B3a 排障实锤）
    if (len === 0) continue

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
    // 不做滚动（wave-2 复审 #2）：restoreSelection 供 undo/格式恢复等静默路径使用，
    // 自带滚动会把视口拽到选区处 —— 用户感知为「格式化后跳到选中文字首部」。
    // 查找导航的滚动由 find-match-current 的 scrollIntoView 单独负责。
  }
}

function replaceTextRange(startOffset: number, endOffset: number, replacement: string) {
  // 查找替换改文本长度（评审 A5）：额外段按旧偏移锚定，必须失效
  clearMultiRanges()
  if (isComposing) return false // IME 护栏（wave-3 #1）
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

// True when the collapsed caret sits at the first text position of the li's
// own content: the fragment between the li's start and the caret is empty
// (zero-length text, no nested list content).
function isCaretAtLiFirstText(li: HTMLLIElement, sel: Selection): boolean {
  const range = sel.getRangeAt(0)
  if (!li.contains(range.startContainer)) return false
  const pre = document.createRange()
  pre.selectNodeContents(li)
  try {
    pre.setEnd(range.startContainer, range.startOffset)
  } catch {
    return false
  }
  const frag = pre.cloneContents()
  if (frag.querySelector('ol, ul')) return false
  return (frag.textContent || '').length === 0
}

// Shared post-mutation serialization for indent-style operations (margin,
// unwrap): emit markdown without a model re-render, then re-push undo state.
function emitIndentChange() {
  nextTick(() => {
    if (!editorRef.value) return
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
  })
}

// Remove the list membership of a top-level li: its inline content becomes a
// paragraph at the item's position, nested lists (if any) become sibling
// blocks, and any items after it continue in a same-kind list. Content is
// fully preserved; the marker simply disappears.
function unwrapListItem(li: HTMLLIElement) {
  const list = li.parentElement
  if (!list || !editorRef.value || (list.tagName !== 'OL' && list.tagName !== 'UL')) return
  pushUndoState(true)

  const inlineNodes: Node[] = []
  const nestedLists: HTMLElement[] = []
  Array.from(li.childNodes).forEach(n => {
    const el = n.nodeType === Node.ELEMENT_NODE ? (n as HTMLElement) : null
    if (el && (el.tagName === 'OL' || el.tagName === 'UL')) nestedLists.push(el)
    else inlineNodes.push(n)
  })

  let p: HTMLElement
  const only = inlineNodes.length === 1 && inlineNodes[0].nodeType === Node.ELEMENT_NODE
    ? (inlineNodes[0] as HTMLElement)
    : null
  if (only && only.tagName === 'P') {
    p = only
  } else {
    p = document.createElement('p')
    inlineNodes.forEach(n => p.appendChild(n))
  }
  if (!p.firstChild) p.appendChild(document.createElement('br'))

  const following: Element[] = []
  let sib = li.nextElementSibling
  while (sib) {
    const nx = sib.nextElementSibling
    following.push(sib)
    sib = nx
  }
  let continuation: HTMLElement | null = null
  if (following.length) {
    continuation = document.createElement(list.tagName)
    following.forEach(el => continuation!.appendChild(el))
  }

  const parent = list.parentNode
  if (!parent) return
  const anchor = list.nextSibling
  li.remove()
  const listEmpty = !list.querySelector('li')
  if (listEmpty) {
    parent.removeChild(list)
  } else if (list.tagName === 'OL') {
    updateOlNumbering(list as HTMLOListElement)
  }

  const ref: Node | null = listEmpty ? anchor : list.nextSibling
  parent.insertBefore(p, ref)
  nestedLists.forEach(nl => parent.insertBefore(nl, ref))
  if (continuation) {
    parent.insertBefore(continuation, ref)
    if (continuation.tagName === 'OL') updateOlNumbering(continuation as HTMLOListElement)
  }

  const sel = window.getSelection()
  if (sel) {
    const range = document.createRange()
    range.setStart(p, 0)
    range.collapse(true)
    sel.removeAllRanges()
    sel.addRange(range)
  }
  emitIndentChange()
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

  // Anchor the caret to its text offset inside the li: the tree shifts when
  // the item is moved, so path-based selection restore lands on the WRONG
  // element afterwards.
  const caretOffset = caretTextOffsetIn(li)

  const listTag = list.tagName.toLowerCase()
  // Actually nesting: drop any stale margin-based indent so Tab/Shift+Tab
  // stay reversible (margin is only used when nesting is impossible).
  li.style.marginLeft = ''
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

  if (caretOffset !== null) {
    nextTick(() => {
      if (editorRef.value) setCaretAtTextOffset(li, caretOffset)
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

// Caret position measured as the number of characters preceding it inside
// root (null when the caret is outside root).
function caretTextOffsetIn(root: Node): number | null {
  const sel = window.getSelection()
  if (!sel || !sel.rangeCount || !root.contains(sel.getRangeAt(0).startContainer)) return null
  const r = sel.getRangeAt(0)
  const pre = document.createRange()
  pre.selectNodeContents(root)
  try {
    pre.setEnd(r.startContainer, r.startOffset)
  } catch {
    return null
  }
  return pre.toString().length
}

// Place the collapsed caret at the given character offset within root's text.
// Falls back to the root's element start when root carries no text nodes
// (e.g. an empty <li>) so the caret never silently stays where the browser
// remapped it after a DOM move.
function setCaretAtTextOffset(root: Node, offset: number) {
  if (!Number.isFinite(offset) || offset < 0) return // 越界（跨块选区/负偏移）不得抛 IndexSizeError
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let node: Node | null
  let acc = 0
  while ((node = walker.nextNode())) {
    const len = (node.textContent || '').length
    if (acc + len >= offset) {
      const range = document.createRange()
      range.setStart(node, offset - acc)
      range.collapse(true)
      const sel = window.getSelection()
      if (sel) {
        sel.removeAllRanges()
        sel.addRange(range)
      }
      return
    }
    acc += len
  }
  const range = document.createRange()
  range.selectNodeContents(root)
  range.collapse(true)
  const sel = window.getSelection()
  if (sel) {
    sel.removeAllRanges()
    sel.addRange(range)
  }
}

// marked emits `<li></li>` for empty items ("2. " with no text): a
// zero-height box that is impossible to click or place a caret in, yet its
// absolutely-positioned ::before marker still paints a ghost number. Give
// every empty item a line box so it behaves like an empty paragraph line.
function normalizeEmptyListItems(root: HTMLElement) {
  root.querySelectorAll('li').forEach(li => {
    if (li.querySelector('br')) return
    const hasEmbed = li.querySelector('img,video,audio,iframe,math,hr,pre,ol,ul')
    if (hasEmbed) return
    if (!li.textContent?.trim()) {
      li.appendChild(document.createElement('br'))
    }
  })
}

// Remove an empty list item entirely (Backspace on an empty line merges it
// into the previous line = the item just disappears), caret to the end of
// the previous li / block.
function removeEmptyListItem(li: HTMLLIElement) {
  const list = li.parentElement
  if (!list || !editorRef.value || (list.tagName !== 'OL' && list.tagName !== 'UL')) return
  pushUndoState(true)

  let caretTarget: Element | null = li.previousElementSibling
  while (caretTarget && caretTarget.tagName !== 'LI') {
    caretTarget = caretTarget.previousElementSibling
  }
  if (!caretTarget) {
    if (list.parentElement && list.parentElement.tagName === 'LI') {
      // First sub-item: its previous line is the parent li's own content.
      caretTarget = list.parentElement
    } else {
      let sib: Node | null = list.previousSibling
      while (sib && sib.nodeType === Node.TEXT_NODE && !(sib.textContent || '').trim()) {
        sib = sib.previousSibling
      }
      caretTarget = sib && sib.nodeType === Node.ELEMENT_NODE ? (sib as Element) : null
    }
  }

  const parentLiEl = list.parentElement && list.parentElement.tagName === 'LI'
    ? (list.parentElement as HTMLLIElement)
    : null

  li.remove()
  if (!list.querySelector('li')) {
    list.remove()
  } else if (list.tagName === 'OL') {
    updateOlNumbering(list as HTMLOListElement)
  }

  // A parent li whose only content was the removed item/list is now an empty
  // ghost itself — drop it recursively (it performs its own caret + emit).
  if (parentLiEl && !parentLiEl.textContent?.trim() && !parentLiEl.querySelector('img,video,audio,iframe,math,hr,pre,ol,ul')) {
    removeEmptyListItem(parentLiEl)
    return
  }

  const sel = window.getSelection()
  if (sel) {
    const range = document.createRange()
    if (caretTarget) {
      range.selectNodeContents(caretTarget)
      range.collapse(false)
    } else if (editorRef.value) {
      // First block in the editor: place the caret at the editor start.
      range.setStart(editorRef.value, 0)
      range.collapse(true)
    } else {
      return
    }
    sel.removeAllRanges()
    sel.addRange(range)
  }
  emitIndentChange()
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
  // Nested item still carrying a margin-based indent: Shift+Tab first walks
  // the margin back (reversible pairing with Tab's margin branch); the tree
  // restructure happens only once the margin is exhausted.
  const nestedMargin = parseInt(li.style.marginLeft || '0', 10)
  if (nestedMargin > 0) {
    pushUndoState(true)
    const nv = Math.max(0, nestedMargin - 40)
    li.style.marginLeft = nv > 0 ? `${nv}px` : ''
    emitIndentChange()
    return
  }
  const outerList = parentLi.parentElement
  if (!outerList) return

  pushUndoState(true)

  const caretOffset = caretTextOffsetIn(li)
  const sel = window.getSelection()
  const saved = sel && sel.rangeCount > 0 ? saveSelection(editorRef.value) : null

  // Moving up a level: drop any stale margin-based indent so the item lands
  // flush at its new level (Tab/Shift+Tab stay reversible).
  li.style.marginLeft = ''

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

  if (caretOffset !== null) {
    nextTick(() => {
      if (editorRef.value) setCaretAtTextOffset(li, caretOffset)
    })
  } else if (saved) {
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

/**
 * 深层空 li 清理（判据 #39）：嵌套列表被移除/搬走后，无内容的 host li 链逐级
 * 回收（与 removeEmptyListItem 的递归口径一致），不留幽灵编号项。
 */
function pruneEmptyLis(from: HTMLElement | null) {
  let cur: HTMLElement | null = from
  // 空判定=零可见字符（含 &nbsp;）且无任何载荷/占位（评审 A I5：<li><br></li>/
  // 勾选框/&nbsp; 是刻意内容，不得回收）
  while (cur && cur.tagName === 'LI' &&
    !(cur.textContent || '').replace(/[\s\u00A0]/g, '') &&
    !cur.querySelector('img,video,audio,iframe,math,pre,br,input,hr,svg,.math-editable')) {
    const parent = cur.parentElement
    cur.remove()
    if (parent && (parent.tagName === 'OL' || parent.tagName === 'UL') && !parent.querySelector('li')) {
      const up = parent.parentElement
      parent.remove()
      cur = up && up.tagName === 'LI' ? up : null
    } else {
      cur = null
    }
  }
}

/**
 * 输入 "N. " 自动列表（判据 #29 · D-101）。三种宿主形态：
 * ① LI（紧凑列表）：空/被重敲标记的项 = 「从这里重新编号」意图 —— 拆列表并把
 *    拆出的新列表 hoist 到最外层列表之后（与 restartOlNumbering 嵌套分支同语义），
 *    首项显示 "1."，绝不再显示 4/3.1。
 * ② P/DIV 在松散 li 内（li>p）：块替换为新列表后，若该 li 除新列表外已无内容，
 *    同样 hoist 到最外层列表之后并清掉空项（旧实现原地嵌套 → 显示 3.1）。
 * ③ 顶层块：原地替换为新列表；相邻不合并由 processChildren 的
 *    `<!--list-break-->` 屏障保真（旧实现往返被并成 4）。
 */
function tryAutoNumberedList(): boolean {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !editorRef.value) return false
  let blockNode: Node | null = sel.anchorNode
  while (blockNode && blockNode !== editorRef.value) {
    if (blockNode.nodeType === Node.ELEMENT_NODE) {
      const tag = (blockNode as HTMLElement).tagName
      if (['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'LI'].includes(tag)) break
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
  // 非文本载荷护栏（评审 B Critical）：block.textContent 忽略 img/video/iframe/
  // math/pre/hr 等载荷 —— `<li>1. <img></li>` 的 text 恰为 "1. "，直接手术会把
  // 图片静默抹掉并立即存档（数据丢失，最坏缺陷类）。块内存在任何非文本载荷
  // 即不触发自动列表（与 removeEmptyListItem 的既有护栏口径一致）。
  const hasPayload = !!block.querySelector('img,video,audio,iframe,math,hr,pre,table')
  if (hasPayload) return false
  if (block.tagName === 'LI') {
    // 松散 li 的嵌套列表也算载荷（其文本参与 textContent）
    const nested = Array.from(block.children).some(c => c.tagName === 'OL' || c.tagName === 'UL')
    if (nested) return false
  }
  pushUndoState(true)

  /** 最外层 ol/ul 祖先（hoist 目标：新列表显示从 1 开始，而非层级 3.1）。 */
  const outermostList = (from: HTMLElement): HTMLElement | null => {
    let list = from.parentElement
    while (list && list !== editorRef.value) {
      if (list.tagName === 'OL' || list.tagName === 'UL') {
        const host = list.parentElement
        if (host && host.tagName === 'LI') {
          list = host.parentElement
          continue
        }
        return list
      }
      list = list.parentElement
    }
    return null
  }

  if (block.tagName === 'LI') {
    const liEl = block as HTMLLIElement
    const list = liEl.parentElement
    if (!list || (list.tagName !== 'OL' && list.tagName !== 'UL')) {
      // 不做无意义的强制快照（评审 B Minor：push 后空转会让一次 undo 无效果）
      return false
    }
    liEl.innerHTML = '<br>'
    const outer = outermostList(liEl) || list
    const split = document.createElement(list.tagName)
    let cur: Node | null = liEl
    while (cur) {
      const next = cur.nextSibling
      split.appendChild(cur)
      cur = next
    }
    outer.parentNode?.insertBefore(split, outer.nextSibling)
    if (!list.querySelector('li')) list.remove()
    else if (list.tagName === 'OL') updateOlNumbering(list as HTMLOListElement)
    pruneEmptyLis(list.parentElement) // 判据 #39：深层空 host li 回收
    if (split.tagName === 'OL') updateOlNumbering(split as HTMLOListElement)
    if (outer !== list && outer.tagName === 'OL') updateOlNumbering(outer as HTMLOListElement)
    const range = document.createRange()
    range.setStart(liEl, 0)
    range.collapse(true)
    sel.removeAllRanges()
    sel.addRange(range)
    nextTick(() => {
      onInput()
      pushCurrentState()
    })
    return true
  }

  const ol = document.createElement('ol')
  const li = document.createElement('li')
  li.innerHTML = '<br>'
  ol.appendChild(li)
  const hostLi = block.closest('li') as HTMLLIElement | null
  const siblings: Element[] = hostLi
    ? Array.from(hostLi.children).filter(c => c !== block)
    : []
  const liRestEmpty = !!hostLi && siblings.every(c =>
    !c.textContent?.trim() && !c.querySelector('img,video,audio,iframe,math,pre,ol,ul'))
  block.parentNode?.replaceChild(ol, block)
  if (hostLi && liRestEmpty) {
    const list = hostLi.parentElement
    const outer = outermostList(hostLi) || list
    hostLi.remove()
    if (list && !list.querySelector('li')) list.remove()
    else if (list && list.tagName === 'OL') updateOlNumbering(list as HTMLOListElement)
    pruneEmptyLis(list ? list.parentElement : null) // 判据 #39：深层空 host li 回收
    if (outer && outer.parentNode && outer !== list) outer.parentNode.insertBefore(ol, outer.nextSibling)
  }
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
  // 陈旧组合标志自愈（判据 #2）：可打印键且浏览器报非组合 ⇒ 组合早已不在
  if (isComposing && shouldHealComposingFromKey(e.key, e.isComposing)) healStaleComposing()

  // Escape to clear table selection / 虚拟多 range（判据 #3）
  if (e.key === 'Escape') {
    if (tableSelection.value.table) {
      clearTableSelection()
      e.preventDefault()
      return
    }
    if (extraSpans.value.length > 0) {
      clearMultiRanges()
      e.preventDefault()
      return
    }
  }

  if ((e.key === 'Delete' || e.key === 'Backspace') && tableSelection.value.table && tableSelection.value.indices.size > 0) {
    e.preventDefault()
    deleteSelectedTableRowsOrCols()
    return
  }

  // 切/合块标记（需求 3）：Enter 恒切块；Backspace/Delete 只有在块边界或
  // 跨块选区时才合块 —— 其余删字符不动标题排版（保护用户显式设置的字号）。
  // 触发键之外的任何按键一律清掉陈旧标记：列表项的自定义 Backspace 会
  // preventDefault（无对应 input）、IME Enter 不切块 —— 若不清，标记会活到
  // 下一次无关 input 上被误用（评审复审 New-1）。
  if (e.key !== 'Enter' && e.key !== 'Backspace' && e.key !== 'Delete') {
    pendingHeadingNormalize = false
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
    pendingHeadingNormalize = true
  } else if (e.key === 'Backspace' || e.key === 'Delete') {
    const sel = window.getSelection()
    if (sel && sel.rangeCount > 0) {
      const collapsed = sel.isCollapsed
      pendingHeadingNormalize = !collapsed ||
        isCaretAtBlockEdge(e.key === 'Backspace' ? 'start' : 'end')
    }
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

  // Backspace at the very start of a heading: controlled merge into the
  // previous block as body text. The browser's native merge leaves the
  // heading's computed style on the merged text (a <span style="font-size:…">
  // artifact) and can move the caret to the wrong side; this keeps the
  // merged text plain and the caret at the merge point.
  if (e.key === 'Backspace' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && !isComposing) {
    const sel = window.getSelection()
    if (sel && sel.rangeCount === 1 && sel.isCollapsed && !tableSelection.value.table) {
      const block = getCurrentBlockElement()
      if (block && isHeadingTag(block.tagName) && isCaretAtBlockEdge('start')) {
        const prev = block.previousElementSibling as HTMLElement | null
        if (prev && MERGE_TARGET_TAGS.has(prev.tagName)) {
          e.preventDefault()
          pendingHeadingNormalize = false
          pushUndoState(true)
          if (mergeHeadingIntoPrevious(block, prev)) {
            onInput(true)
            return
          }
        } else if (prev && STRUCTURAL_PREV_TAGS.has(prev.tagName)) {
          // Previous block is a list/table/pre/DIV container and cannot hold
          // merged inline content safely. Demote the heading to a paragraph in
          // place instead of letting native merge swallow it into the structure.
          // Inline prev siblings (<b>/<span>/…) fall through to native merge.
          e.preventDefault()
          pendingHeadingNormalize = false
          pushUndoState(true)
          stripHeadingArtifactsInBlock(block)
          const converted = convertBlockTag(block, 'p')
          HEADING_TYPO_PROPS.forEach((p) => converted.style.removeProperty(p))
          if (!converted.getAttribute('style')) converted.removeAttribute('style')
          const range = document.createRange()
          range.setStart(converted, 0)
          range.collapse(true)
          const sel2 = window.getSelection()
          sel2?.removeAllRanges()
          sel2?.addRange(range)
          onInput(true)
          return
        }
      }
    }
  }

  // Backspace at the very start of a list item's first line: controlled
  // unwrap/outdent instead of the native contenteditable behavior, which
  // merges the item into the previous block (e.g. swallows it into a
  // heading) or destroys text. Mirrors mainstream block editors: nested
  // item → outdent one level; margin-indented item → reduce margin;
  // top-level item → unwrap to a paragraph (list marker removed, content
  // fully preserved).
  if (e.key === 'Backspace' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
    const sel = window.getSelection()
    if (sel && sel.rangeCount === 1 && sel.isCollapsed && !tableSelection.value.table) {
      const li = findCurrentListItem()
      if (li && isCaretAtLiFirstText(li, sel)) {
        // Empty item: Backspace deletes the whole item (an empty line merges
        // into the previous line — nothing to unwrap, no ghost left behind).
        if (!li.textContent?.trim() && !li.querySelector('img,video,audio,iframe,math,hr,pre')) {
          e.preventDefault()
          removeEmptyListItem(li)
          return
        }
        const list = li.parentElement
        const parentLi = list && list.parentElement && list.parentElement.tagName === 'LI'
          ? list.parentElement
          : null
        if (parentLi) {
          e.preventDefault()
          outdentListItem(li)
          return
        }
        const currentMargin = parseInt(li.style.marginLeft || '0', 10)
        if (currentMargin > 0) {
          e.preventDefault()
          pushUndoState(true)
          const nv = Math.max(0, currentMargin - 40)
          li.style.marginLeft = nv > 0 ? `${nv}px` : ''
          emitIndentChange()
          return
        }
        e.preventDefault()
        unwrapListItem(li)
        return
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
    document.dispatchEvent(new Event('selectionchange'))
  }

  // Ctrl/Cmd + I for italic
  if ((e.ctrlKey || e.metaKey) && e.key === 'i') {
    e.preventDefault()
    document.execCommand('italic')
    document.dispatchEvent(new Event('selectionchange'))
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
    insertLink()
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
  if (isComposing) return // IME 护栏（wave-3 #1）
  const imageFiles = e.clipboardData?.files
    ? Array.from(e.clipboardData.files).filter(f => f.type.startsWith('image/'))
    : []
  const clipHtml = e.clipboardData?.getData('text/html') || ''
  const plainText = e.clipboardData?.getData('text/plain') || ''
  const directImageUploads = imageFiles.length > 0 && props.imageUploader
    ? imageFiles.map((file) => handleImageFileUpload(file))
    : []

  if (imageFiles.length > 0 && props.imageUploader) {
    e.preventDefault()
    // Image-only clipboard: insert uploads and stop. Mixed image+text payloads
    // must continue into the normal HTML/plain path below.
    if (!clipHtml && !plainText) {
      trackImageUpload(Promise.all(directImageUploads).then(() => undefined))
      return
    }
  }

  e.preventDefault()

  let html = ''
  if (clipHtml) {
    const sanitized = sanitizePastedHtml(clipHtml)
    if (sanitized) html = sanitized
  }

  if (!html) {
    const text = e.clipboardData?.getData('text/plain') || ''
    if (!text) {
      // HTML sanitized to nothing and no plain text: still own the in-flight
      // file uploads so a save/export cannot miss them.
      if (directImageUploads.length > 0) {
        trackImageUpload(Promise.all(directImageUploads).then(() => undefined))
      }
      return
    }
    // 粘贴文本面：可见编号重置的连续有序列表拆为独立列表（判据 #40）
    html = renderMarkdownToHtml(splitNumberingResets(text))
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
    trackImageUpload(
      directImageUploads.length > 0
        ? Promise.all(directImageUploads).then(() => undefined)
        : undefined,
    )
  }

  onInput()
}

function sanitizePastedHtml(html: string): string {
  const doc = new DOMParser().parseFromString(extractClipboardFragment(html), 'text/html')
  try {
    normalizeOfficeLists(doc)
  } catch (err) {
    // A malformed foreign tree must degrade to the raw fragment, never discard
    // the whole paste (onPaste already preventDefault()ed).
    console.warn('Office list normalization skipped:', err)
  }
  inlinePastedStylesheetRules(doc)
  doc.querySelectorAll('script, style, link, meta, iframe, object, embed, form').forEach(el => el.remove())
  doc.querySelectorAll('*').forEach(el => {
    Array.from(el.attributes).forEach(attr => {
      if (attr.name.startsWith('on')) el.removeAttribute(attr.name)
    })
  })
  // Live-DOM scheme guard: persisted content is cleaned by DOMPurify on render,
  // but the pasted tree becomes interactive immediately (Ctrl/Cmd+click).
  const unsafeUrl = (value: string) => {
    const v = value.trim().toLowerCase()
    return v.startsWith('javascript:') || v.startsWith('vbscript:') ||
      (v.startsWith('data:') && !v.startsWith('data:image/'))
  }
  doc.querySelectorAll('[href], [src]').forEach((el) => {
    for (const name of ['href', 'src']) {
      const value = el.getAttribute(name)
      if (value && unsafeUrl(value)) el.removeAttribute(name)
    }
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
function trackImageUpload(precedingTask?: Promise<void>) {
  const prev = pendingImageUpload
  const work = () => precedingTask
    ? precedingTask.then(uploadPastedImages, uploadPastedImages)
    : uploadPastedImages()
  const tail = (prev || Promise.resolve()).then(work, work)
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

// ---------- 清除格式（遗留 #1 方案 A） ----------

/**
 * 「清除格式」的选区缓存：工具栏按钮按下瞬间记录**非折叠**选区。
 * 目的：浏览器/移动端可能在 pointerdown→click 之间把选区折叠掉，此时若按实时选区判断
 * 就会误判成「没选文字」→ 弹整篇确认。缓存只在按钮交互前保存，且折叠选区不会覆盖它
 * （用户主动点别处会先清空缓存，见 `saveClearTarget`）。
 */
let clearTargetSelection: SavedSelection | null = null
let clearTargetAt = 0
const CLEAR_TARGET_TTL_MS = 30000

/** 按钮 pointerdown 时调用：记录非折叠选区；实时选区折叠则视为用户已放弃选区（清空缓存）。 */
function saveClearTarget() {
  const root = editorRef.value
  const sel = window.getSelection()
  if (!root || !sel || sel.rangeCount === 0) return
  const r = sel.getRangeAt(0)
  const inEditor = root.contains(r.startContainer) && root.contains(r.endContainer)
  if (r.collapsed || !inEditor) {
    clearTargetSelection = null
    clearTargetAt = 0
    return
  }
  clearTargetSelection = {
    startOffset: textOffsetOf(root, r.startContainer, r.startOffset),
    endOffset: textOffsetOf(root, r.endContainer, r.endOffset),
  }
  clearTargetAt = Date.now()
}

/** 恢复缓存选区；成功返回 true（调用方据此走「只清选区」而不是整篇确认）。 */
function restoreClearTarget(): boolean {
  const saved = clearTargetSelection
  if (!saved || !editorRef.value) return false
  if (Date.now() - clearTargetAt > CLEAR_TARGET_TTL_MS) return false
  if (saved.startOffset >= saved.endOffset) return false
  setSelectionByTextOffset(saved.startOffset, saved.endOffset)
  const sel = window.getSelection()
  return !!sel && sel.rangeCount > 0 && !sel.isCollapsed && editorRef.value.contains(sel.getRangeAt(0).startContainer)
}

function clearClearTarget() {
  clearTargetSelection = null
  clearTargetAt = 0
}

/** 组合期（IME）标志 —— 父组件据此避免「先弹确认、再静默不执行」。
 * 活性校验（判据 #2）：编辑器已失焦 ⇒ 组合必已提交/取消（浏览器 blur 即结束组合），
 * 标志为陈旧值，先自愈再回答。 */
function isEditorComposing(): boolean {
  if (
    isComposing &&
    editorRef.value &&
    document.activeElement !== editorRef.value &&
    !(document.activeElement && editorRef.value.contains(document.activeElement))
  ) {
    healStaleComposing()
  }
  return isComposing
}

/** 当前是否存在编辑器内的非折叠选区（父组件据此决定要不要弹「整篇」确认）。
 *  虚拟多 range 存在时同样算「有选区」（判据 #4：清选区不弹整篇确认）。 */
function hasTextSelection(): boolean {
  if (extraSpans.value.length > 0) return true
  const root = editorRef.value
  const sel = window.getSelection()
  if (!root || !sel || sel.rangeCount === 0) return false
  if (sel.isCollapsed) return false
  return root.contains(sel.getRangeAt(0).startContainer) && root.contains(sel.getRangeAt(0).endContainer)
}

/** 元素深度（根为 0），用于「先解内层、再解外层」。 */
function elementDepth(el: Element, stop: Element): number {
  let d = 0
  let cur: Element | null = el
  while (cur && cur !== stop) { d++; cur = cur.parentElement }
  return d
}

/** 剥掉元素上的字体类 style（保留对齐/尺寸等布局属性）。 */
function stripFontStyles(el: HTMLElement, opts: { keepVerticalAlign?: boolean } = {}) {
  const props: string[] = []
  for (let i = 0; i < el.style.length; i++) props.push(el.style.item(i))
  props.forEach((prop) => {
    if (opts.keepVerticalAlign && prop.toLowerCase() === 'vertical-align') return
    if (isStrippedStyleProperty(prop)) el.style.removeProperty(prop)
  })
  if (!el.getAttribute('style')) el.removeAttribute('style')
}

/** 保护子树内部一律不动；受保护标签自身只跳过（不递归清除其内部）。 */
function insideProtected(el: Element, root: HTMLElement): boolean {
  const hit = el.closest(PROTECTED_SELECTOR)
  return !!hit && hit !== root
}

/** 元素在整篇文本中的区间（数值偏移，避免 compareBoundaryPoints 的方向陷阱）。 */
function elementTextSpan(root: HTMLElement, el: Element): { start: number; end: number } {
  const before = document.createRange()
  before.selectNodeContents(root)
  try { before.setEndBefore(el) } catch { /* 不在树内 */ }
  const start = before.toString().length
  const after = document.createRange()
  after.selectNodeContents(root)
  try { after.setEndAfter(el) } catch { /* 不在树内 */ }
  return { start, end: after.toString().length }
}

/** 文本偏移 → (node, offset) 落点（清除格式的碎片拆分用）。 */
function positionAtTextOffset(root: HTMLElement, offset: number): { node: Node; offset: number } | null {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let acc = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    const len = (node.textContent || '').length
    if (len === 0) continue
    if (acc + len >= offset) return { node, offset: Math.max(0, Math.min(len, offset - acc)) }
    acc += len
  }
  return null
}

/** 一次性元素文本区间索引（O(n)，避免每个元素各跑一次全篇 Range.toString()）。 */
function buildElementSpanIndex(root: HTMLElement): Map<Element, { start: number; end: number }> {
  const index = new Map<Element, { start: number; end: number }>()
  let offset = 0
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      offset += (node.textContent || '').length
      return
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return
    const el = node as Element
    const start = offset
    for (const child of Array.from(el.childNodes)) walk(child)
    index.set(el, { start, end: offset })
  }
  for (const child of Array.from(root.childNodes)) walk(child)
  return index
}

/** 块级元素（块样式作用于整段，无法碎片化：触达即清）。 */
const BLOCK_LIKE_TAGS = new Set(['P', 'DIV', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE', 'TD', 'TH', 'TABLE', 'SECTION'])

/** 选区（或整篇）的文本区间。 */
function rangesTextSpan(ranges: Range[], root: HTMLElement): { start: number; end: number } {
  let start = Number.POSITIVE_INFINITY
  let end = Number.NEGATIVE_INFINITY
  for (const r of ranges) {
    try {
      const a = document.createRange()
      a.selectNodeContents(root)
      a.setEnd(r.startContainer, r.startOffset)
      start = Math.min(start, a.toString().length)
      const b = document.createRange()
      b.selectNodeContents(root)
      b.setEnd(r.endContainer, r.endOffset)
      end = Math.max(end, b.toString().length)
    } catch { /* 跨树忽略 */ }
  }
  if (!Number.isFinite(start) || !Number.isFinite(end)) return { start: 0, end: 0 }
  return { start, end }
}

/** 选中区间与该元素是否有内容重叠 / 是否完整覆盖该元素。 */
function spanRelations(sel: { start: number; end: number }, elSpan: { start: number; end: number }) {
  return {
    overlaps: sel.start < elSpan.end && sel.end > elSpan.start,
    fullyCovers: sel.start <= elSpan.start && sel.end >= elSpan.end,
  }
}

/** 纯视觉标签（解包即改变语义格式）；无属性的 span 解包对 markdown 无影响，不算工作量。 */
const SEMANTIC_CLEAR_TAGS = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'S', 'STRIKE', 'DEL', 'MARK', 'SMALL', 'BIG', 'FONT', 'SUP', 'SUB', 'CODE'])

/** 把元素就地换成 `<p>`（保留子节点）。 */
function replaceWithParagraph(el: HTMLElement): HTMLElement {
  const p = document.createElement('p')
  while (el.firstChild) p.appendChild(el.firstChild)
  el.parentNode?.replaceChild(p, el)
  return p
}

/** 代码块 → 普通段落：**逐行**一段（行文本与顺序保留；纯空行折叠）。 */
function preToParagraphs(pre: HTMLElement): HTMLElement[] {
  const text = (pre.textContent || '').replace(/\r\n?/g, '\n').replace(/\n+$/, '')
  const lines = text.split('\n')
  const frag = document.createDocumentFragment()
  const created: HTMLElement[] = []
  for (const line of lines) {
    if (!line.trim()) continue
    const p = document.createElement('p')
    // 段落在 markdown 里无法保留行首空格（4 空格还会被重解析成代码块）：
    // 用 NBSP 保留缩进视觉，避免「清除后再保存又变回代码块」。
    const indentMatch = line.match(/^[ \t]+/)
    const indent = indentMatch ? indentMatch[0].replace(/\t/g, '    ').replace(/ /g, '\u00A0') : ''
    p.textContent = indent + line.slice(indentMatch ? indentMatch[0].length : 0)
    frag.appendChild(p)
    created.push(p)
  }
  if (created.length === 0) {
    const p = document.createElement('p')
    p.appendChild(document.createElement('br'))
    frag.appendChild(p)
    created.push(p)
  }
  pre.parentNode?.replaceChild(frag, pre)
  return created
}

/** 引用块 → 普通段落（含块级子元素则就地解包，否则包成一段）。 */
function unwrapBlockquote(bq: HTMLElement) {
  const parent = bq.parentNode
  if (!parent) return
  if (bq.querySelector('p, div, ul, ol, h1, h2, h3, h4, h5, h6, pre, table')) {
    while (bq.firstChild) parent.insertBefore(bq.firstChild, bq)
  } else {
    const p = document.createElement('p')
    while (bq.firstChild) p.appendChild(bq.firstChild)
    parent.insertBefore(p, bq)
  }
  parent.removeChild(bq)
}

/**
 * 块级样式归普通文本（用户口径 2026-10-25）：
 * 标题 h1-h6 / 引用 blockquote / 代码块（`.code-block` 或裸 `pre`）→ 普通段落。
 * **保留**列表与表格（与 Google Docs 的 Clear formatting 一致）。
 * 返回最后一个新段落（落点用）与是否有变化。
 */
function normalizeBlockFormatting(ranges: Range[], root: HTMLElement): { changed: boolean; last: HTMLElement | null } {
  const selSpans = ranges.map((r) => rangesTextSpan([r], root))
  const index = buildElementSpanIndex(root)
  let changed = false
  let last: HTMLElement | null = null
  const noteLast = (el: HTMLElement | null | undefined) => {
    if (!el || !el.isConnected) return
    if (!last) { last = el; return }
    // 两个处理循环的先后不等于文档顺序 —— 取文档位置更后的那个
    const rel = last.compareDocumentPosition(el)
    if (rel & Node.DOCUMENT_POSITION_FOLLOWING) last = el
  }
  const touched = (el: HTMLElement) => {
    const sp = index.get(el) ?? elementTextSpan(root, el)
    return selSpans.some((sel) => {
      if (spanRelations(sel, sp).overlaps) return true
      // 零文本块（空标题/仅图片的标题）区间为 [k,k]，严格重叠永假 —— 用包含判定兜底
      return sp.start === sp.end && sel.start <= sp.start && sel.end >= sp.end
    })
  }
  // 先处理 `.code-block`（含 pre 与头部 chrome），再处理裸 pre / 标题 / 引用
  const wrappers = Array.from(root.querySelectorAll<HTMLElement>('.code-block'))
  for (const wrapper of wrappers) {
    if (!wrapper.isConnected || !touched(wrapper)) continue
    // 与裸 pre 分支同一护栏：受保护渲染器内部不动；无 <pre> 的畸形包裹跳过
    // （否则会把头部 chrome「js」当正文搬进笔记）
    if (wrapper.closest('.math-editable, .katex, .mermaid-block, .echarts-block')) continue
    const pre = wrapper.querySelector('pre')
    if (!pre) continue
    const created = preToParagraphs(pre)
    const parent = wrapper.parentNode
    if (parent) {
      for (const p of created) parent.insertBefore(p, wrapper)
    }
    wrapper.remove()
    changed = true
    noteLast(created[created.length - 1])
  }
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6, blockquote, pre'))) {
    if (!el.isConnected) continue
    if (el.closest('.math-editable, .katex, .mermaid-block, .echarts-block')) continue
    if (!touched(el)) continue
    if (isBlockResetTag(el.tagName)) {
      if (el.tagName === 'PRE') {
        const created = preToParagraphs(el)
        noteLast(created[created.length - 1])
      } else if (el.tagName === 'BLOCKQUOTE') {
        const before = Array.from(el.children) as HTMLElement[]
        unwrapBlockquote(el)
        // 落点取**最后一个文本块**（不是第一个）：多段引用清完后继续输入应落在末段；
        // 若末段是列表/表格则回退到最后一个 <p>，避免把光标放进结构元素外侧。
        const textBlocks = before.filter((c) => c.isConnected && /^(P|LI|DIV)$/.test(c.tagName))
        noteLast(textBlocks[textBlocks.length - 1])
      } else {
        noteLast(replaceWithParagraph(el))
      }
      changed = true
    }
  }
  return { changed, last }
}

/** 该元素解包/剥样式是否能带来真实变化（避免「空操作也报成功」）。 */
function hasRealClearWork(el: HTMLElement): boolean {
  if (SEMANTIC_CLEAR_TAGS.has(el.tagName)) return true
  if (el.hasAttribute('style') || el.hasAttribute('class') || el.hasAttribute('color') || el.hasAttribute('face') || el.hasAttribute('size')) return true
  return false
}

/** 选区（或整篇）里是否存在需要清除的内容 —— 用于「空操作不压撤销栈」。 */
function hasClearableContent(ranges: Range[], root: HTMLElement): boolean {
  const spans = ranges.map((r) => rangesTextSpan([r], root))
  const index = buildElementSpanIndex(root)
  for (const el of Array.from(root.querySelectorAll<HTMLElement>(CLEARABLE_INLINE_SELECTOR))) {
    if (insideProtected(el, root)) continue
    if (!hasRealClearWork(el)) continue
    const sp = index.get(el) ?? elementTextSpan(root, el)
    if (spans.some((sel) => spanRelations(sel, sp).overlaps)) return true
  }
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6, blockquote, pre, .code-block'))) {
    if (el.closest('.math-editable, .katex, .mermaid-block, .echarts-block')) continue
    // 与手术分支同护栏：无 <pre> 的畸形 .code-block 不产生任何变化
    if (el.classList.contains('code-block') && !el.querySelector('pre')) continue
    const sp = index.get(el) ?? elementTextSpan(root, el)
    if (spans.some((sel) => {
      if (spanRelations(sel, sp).overlaps) return true
      return sp.start === sp.end && sel.start <= sp.start && sel.end >= sp.end // 零文本块
    })) return true
  }
  for (const el of Array.from(root.querySelectorAll<HTMLElement>('[style]'))) {
    if (insideProtected(el, root)) continue
    if (isLayoutTag(el.tagName)) continue
    const sp = index.get(el) ?? elementTextSpan(root, el)
    if (!spans.some((sel) => spanRelations(sel, sp).overlaps)) continue
    for (let i = 0; i < el.style.length; i++) {
      if (isStrippedStyleProperty(el.style.item(i))) return true
    }
  }
  return false
}

/**
 * 清除格式：`selection` = 只清选中范围；`document` = 整篇。
 *
 * 语义（A4.9 双评审修订）：
 * - 行内标签按**选区碎片**解包（数值文本区间 + 边界拆分）——只选中粗体中间一个字时，
 *   不会连带清掉两侧未选中的格式；
 * - 块级元素的字体类 style 按「触达即清」（块样式作用于整段，无法碎片化）；
 * - 数据载体（引文/尾注/公式/图表/代码块）与布局标签（图片/音视频/iframe/hr）不动；
 * - 空操作不压撤销栈；组合期返回 'busy'（调用方提示，不弹确认框）。
 */
function clearFormatting(scopeOverride?: ClearScope): ClearScope | 'busy' | null {
  const root = editorRef.value
  if (!root) return null
  if (isComposing) return 'busy'
  const sel = window.getSelection()
  const inEditor = !!sel && sel.rangeCount > 0 && !!root.contains(sel.getRangeAt(0).startContainer)
  const caretSnapshot = inEditor ? captureSelectionSnapshot() : null
  const scope: ClearScope = scopeOverride
    ?? resolveClearScope((!inEditor || !sel || sel.isCollapsed) && extraSpans.value.length === 0)
  const ranges: Range[] = []
  if (scope === 'selection') {
    // 多 range（判据 #4）：额外段+原生；无额外段时与原生选区逐字等价（判据 #79 语义）
    const action = getActionRanges()
    for (const r of action.ranges) {
      if (!r.collapsed && root.contains(r.startContainer) && root.contains(r.endContainer)) ranges.push(r)
    }
    if (ranges.length === 0) return null
  } else {
    const whole = document.createRange()
    whole.selectNodeContents(root)
    ranges.push(whole)
  }
  const selSpans = ranges.map((r) => rangesTextSpan([r], root))

  if (!hasClearableContent(ranges, root)) return null // 空操作：不压撤销栈、不报成功
  pushUndoState(true)
  const htmlBefore = captureSnapshotHtml()
  let blockResult: { changed: boolean; last: HTMLElement | null } = { changed: false, last: null }

  try {
    // ① 行内标签：碎片级解包（未完整覆盖的先按选区边界拆分；多趟收敛嵌套）
    for (let pass = 0; pass < 4; pass++) {
      const index = buildElementSpanIndex(root)
      let changed = false
      for (const selSpan of selSpans) {
        for (const el of Array.from(root.querySelectorAll<HTMLElement>(CLEARABLE_INLINE_SELECTOR))) {
          if (!el.isConnected) continue
          if (insideProtected(el, root)) continue
          const sp = index.get(el) ?? elementTextSpan(root, el)
          const rel = spanRelations(selSpan, sp)
          if (!rel.overlaps) continue
          if (rel.fullyCovers) {
            if (!hasRealClearWork(el)) continue
            unwrapElementKeepText(el)
            changed = true
            continue
          }
          if (selSpan.start > sp.start) {
            const pos = positionAtTextOffset(root, selSpan.start)
            if (pos && el.contains(pos.node) && splitElementAt(el, pos.node, pos.offset)) changed = true
          }
          if (selSpan.end < sp.end) {
            const pos = positionAtTextOffset(root, selSpan.end)
            if (pos && el.contains(pos.node) && splitElementAt(el, pos.node, pos.offset)) changed = true
          }
        }
      }
      if (!changed) break
    }

    // ② 字体类 style：逐 range 判定（多选区的并集不得清掉两段之间的内容）；
    //    行内元素仅完整覆盖才剥，块级触达即清；表格单元格保留 vertical-align（布局）。
    const styleIndex = buildElementSpanIndex(root)
    root.querySelectorAll<HTMLElement>('[style]').forEach((el) => {
      if (insideProtected(el, root)) return
      if (isLayoutTag(el.tagName)) return
      const sp = styleIndex.get(el) ?? elementTextSpan(root, el)
      const blockLike = BLOCK_LIKE_TAGS.has(el.tagName)
      const hit = selSpans.some((sel) => {
        const rel = spanRelations(sel, sp)
        return rel.overlaps && (blockLike || rel.fullyCovers)
      })
      if (!hit) return
      stripFontStyles(el, { keepVerticalAlign: blockLike })
    })

    // ③ 块级样式归普通文本（标题/引用/代码块 → 段落；列表与表格保留）
    blockResult = normalizeBlockFormatting(ranges, root)
    // ④ 残壳回收（已内建保护子树跳过）
    cleanupEmptyInline(root)
  } catch (err) {
    // 手术护栏（A4.9 评审 N1）：任何几何异常都回滚到清除前，绝不留下半残 DOM
    root.innerHTML = htmlBefore
    console.error('[clearFormatting] rolled back:', err)
    return null
  }

  // ⑤ 复位选区/光标：块级归一后偏移已变 —— 落到最后一个新段落末尾；
  //    否则选区按文本偏移、整篇按**块感知**快照（段首不被拽回上一段）
  if (scope === 'selection' && blockResult.changed && blockResult.last) {
    // 选区清除：光标落到归一后最后一段的末尾（块结构已重建，旧偏移不可复用）
    const range = document.createRange()
    range.selectNodeContents(blockResult.last)
    range.collapse(false)
    const selNow = window.getSelection()
    if (selNow) {
      selNow.removeAllRanges()
      selNow.addRange(range)
    }
  } else if (scope === 'selection') {
    if (ranges.length > 1) {
      // 多段（评审 A4）：不得重建「首段起点→末段终点」的外接选区——它会把段间
      // 未选中文本一并选中，随后一键入即替换整段（数据丢失）。塌到末段尾。
      const endRange = ranges[ranges.length - 1]
      if (endRange.endContainer && root.contains(endRange.endContainer)) {
        const tail = document.createRange()
        tail.setStart(endRange.endContainer, endRange.endOffset)
        tail.collapse(true)
        const selNow = window.getSelection()
        selNow?.removeAllRanges()
        selNow?.addRange(tail)
      }
    } else if (ranges[0].startContainer && root.contains(ranges[0].startContainer)) {
      const start = textOffsetOf(root, ranges[0].startContainer, ranges[0].startOffset)
      const endRange = ranges[ranges.length - 1]
      const end = textOffsetOf(root, endRange.endContainer, endRange.endOffset)
      setSelectionByTextOffset(start, end)
    }
  } else if (caretSnapshot) {
    restoreCollapsedSelection(caretSnapshot, 'flat', 'start')
  }

  onInput()
  nextTick(() => pushCurrentState())
  return scope
}

// Execute formatting command
function execCommand(command: string, value?: string) {
  if (isComposing) return // IME 护栏（wave-3 #1）
  const snapshot = captureSelectionSnapshot()
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
          nextTick(() => {
            restoreCaretAfterCommand(snapshot)
            pushCurrentState()
          })
          return
        }
        node = node.parentNode
      }
    }
  }

  // 多 range（判据 #4）：行内样式命令按「额外段+原生段」逐段应用（倒序：右段 DOM
  // 手术不动左段偏移）。每段先置原生选区再走既有 execCommand 通道，语义与单段一致。
  if (extraSpans.value.length > 0 && MULTI_INLINE_COMMANDS.has(command)) {
    const spans = getEffectiveSpans()
    const sel = window.getSelection()
    for (let i = spans.length - 1; i >= 0; i--) {
      const ranges = spansToRanges([spans[i]])
      if (ranges.length === 0) continue
      sel?.removeAllRanges()
      sel?.addRange(ranges[0])
      document.execCommand(command, false, value)
    }
    // 循环末原生选区停在首段（评审 A9）：与高亮双画且随后键入会吞掉首段 ——
    // 塌到末段尾（与 mouseup 提交路径同法）。
    if (spans.length > 0) {
      const tailRanges = spansToRanges([spans[spans.length - 1]])
      const tail = tailRanges[0]
      if (tail) {
        tail.collapse(false)
        sel?.removeAllRanges()
        sel?.addRange(tail)
      }
    }
    renderMultiHighlights()
    focus()
    nextTick(() => pushCurrentState())
    return
  }

  document.execCommand(command, false, value)
  focus()
  nextTick(() => {
    restoreCaretAfterCommand(snapshot)
    pushCurrentState()
  })
}

// Update math element with new TeX content
async function updateMathElement(element: HTMLElement, newHtml: string) {
  if (isComposing) return // IME 护栏（wave-3 #1）
  pushUndoState(true) // 数学编辑可撤销（复审 B#10）
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

/** Top-level block (direct child of the editor root) containing `node`. */
function topLevelBlockForNode(root: HTMLElement, node: Node): HTMLElement | null {
  let n: Node | null = node
  while (n && n.parentNode !== root) n = n.parentNode
  if (!n || n.nodeType !== Node.ELEMENT_NODE) return null
  return n as HTMLElement
}

/** Every top-level block touched by the selection, in document order. */
function topLevelBlocksInRange(root: HTMLElement, range: Range): HTMLElement[] {
  const startBlock = topLevelBlockForNode(root, range.startContainer)
  const endBlock = topLevelBlockForNode(root, range.endContainer)
  if (!startBlock) return []
  if (!endBlock || startBlock === endBlock) return [startBlock]
  const children = Array.from(root.children) as HTMLElement[]
  const si = children.indexOf(startBlock)
  const ei = children.indexOf(endBlock)
  if (si < 0 || ei < 0) return [startBlock]
  return children.slice(Math.min(si, ei), Math.max(si, ei) + 1)
}

/**
 * Copy a block's content into a fragment, unwrapping nested block elements
 * into inline text with <br> separators. Chromium's formatBlock leaves a
 * nested <p> inside an <h3> when the heading contains a <br>; replacing the
 * whole block tag avoids that half-converted state.
 */
function flattenBlockContent(node: Node, out: DocumentFragment) {
  node.childNodes.forEach((child) => {
    if (child.nodeType === Node.ELEMENT_NODE) {
      const el = child as HTMLElement
      if (INLINE_BLOCK_TAGS.has(el.tagName)) {
        if (out.lastChild && out.lastChild.nodeName !== 'BR') out.appendChild(document.createElement('br'))
        flattenBlockContent(el, out)
        if (out.lastChild && out.lastChild.nodeName !== 'BR') out.appendChild(document.createElement('br'))
      } else {
        out.appendChild(child.cloneNode(true))
      }
    } else {
      out.appendChild(child.cloneNode(true))
    }
  })
}

/** Replace a block's tag while preserving its attributes and inline content. */
function convertBlockTag(block: HTMLElement, targetTag: string): HTMLElement {
  if (block.tagName.toLowerCase() === targetTag) return block
  const replacement = document.createElement(targetTag)
  Array.from(block.attributes).forEach((attr) => replacement.setAttribute(attr.name, attr.value))
  const frag = document.createDocumentFragment()
  flattenBlockContent(block, frag)
  replacement.appendChild(frag)
  block.replaceWith(replacement)
  return replacement
}

/**
 * Strip Chromium's heading-style span artifact before demoting/merging a
 * heading. Artifact test (回归探针 M/K): a span whose font-size **restates
 * the heading's own computed size in the destination base** (em × dest base
 * == heading computed, or computed == heading computed) is redundant — it
 * would multiply against the destination's font-size. User-set px sizes,
 * different em ratios and colors are preserved.
 */
function stripHeadingArtifactsInBlock(block: HTMLElement) {
  const cs = getComputedStyle(block)
  const heading = {
    fontSize: cs.fontSize,
    fontWeight: cs.fontWeight,
    fontFamily: cs.fontFamily,
    lineHeight: cs.lineHeight,
    letterSpacing: cs.letterSpacing,
  }
  const headingPx = parseFloat(cs.fontSize) || 0
  // After demote/merge the content lands in a sibling-level block; its base
  // is the parent's font-size (em spans there resolve against that base).
  const parent = block.parentElement
  const destBasePx = parseFloat(parent ? getComputedStyle(parent).fontSize : cs.fontSize) || 0
  block.querySelectorAll<HTMLElement>('span[style]').forEach((span) => {
    const scs = getComputedStyle(span)
    const declared = span.style.fontSize
    const props: string[] = []
    let fontRestated = scs.fontSize === heading.fontSize
    if (declared && /em$/i.test(declared)) {
      const em = parseFloat(declared)
      if (em > 0 && destBasePx > 0 && Math.abs(em * destBasePx - headingPx) < 0.51) fontRestated = true
    }
    if (fontRestated) props.push('font-size')
    if (scs.fontWeight === heading.fontWeight) props.push('font-weight')
    if (scs.fontFamily === heading.fontFamily) props.push('font-family')
    if (scs.lineHeight === heading.lineHeight) props.push('line-height')
    if (scs.letterSpacing === heading.letterSpacing) props.push('letter-spacing')
    props.forEach((p) => span.style.removeProperty(p))
    if (!span.getAttribute('style')) span.removeAttribute('style')
  })
}

/** Block-aware selection snapshot: avoids the "end of block == start of next" ambiguity. */
function saveBlockSelection(root: HTMLElement): {
  startBlockIndex: number; startInBlock: number; endBlockIndex: number; endInBlock: number
} | null {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return null
  const range = sel.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null
  const startBlockIndex = topLevelBlockIndex(root, range.startContainer)
  const endBlockIndex = topLevelBlockIndex(root, range.endContainer)
  if (startBlockIndex < 0 || endBlockIndex < 0) return null
  const startBlock = root.children[startBlockIndex] as HTMLElement
  const endBlock = root.children[endBlockIndex] as HTMLElement
  return {
    startBlockIndex,
    startInBlock: blockCaretOffset(startBlock, range.startContainer, range.startOffset),
    endBlockIndex,
    endInBlock: blockCaretOffset(endBlock, range.endContainer, range.endOffset),
  }
}

/**
 * 块内光标偏移（br 感知）：文本按字符计，每个 `<br>` 计 1 单位。
 * `flatOffsetWithin` 的 Range.toString() 口径把 br 计 0 宽 —— 「br 后行首」
 * 与「上一行行尾」同值，恢复后 Backspace 删错侧字符（回归探针 B）。
 */
function blockCaretOffset(block: HTMLElement, node: Node, offset: number): number {
  if (!block.contains(node) && block !== node) return -1
  const r = document.createRange()
  r.selectNodeContents(block)
  try {
    r.setEnd(node, offset)
  } catch {
    return -1
  }
  const frag = r.cloneContents()
  let acc = 0
  const walk = (n: Node) => {
    n.childNodes.forEach((c) => {
      if (c.nodeType === Node.TEXT_NODE) acc += (c.textContent || '').length
      else if (c.nodeType === Node.ELEMENT_NODE) {
        if ((c as HTMLElement).tagName === 'BR') acc += 1
        else walk(c)
      }
    })
  }
  walk(frag)
  return acc
}

/** Resolve a br-aware block offset back to a DOM position ({container, offset}). */
function resolveBlockCaretOffset(scope: HTMLElement, off: number): { container: Node; offset: number } | null {
  let acc = 0
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT)
  while (walker.nextNode()) {
    const n = walker.currentNode
    if (n.nodeType === Node.TEXT_NODE) {
      const len = (n.textContent || '').length
      if (len === 0) continue
      if (off <= acc + len) return { container: n, offset: Math.max(0, off - acc) }
      acc += len
    } else if ((n as HTMLElement).tagName === 'BR') {
      const parent = n.parentNode
      const idx = parent ? Array.prototype.indexOf.call(parent.childNodes, n) : -1
      if (parent && idx >= 0 && off <= acc) return { container: parent, offset: idx }
      acc += 1
      if (parent && idx >= 0 && off <= acc) return { container: parent, offset: idx + 1 }
    }
  }
  return null
}

function restoreBlockSelection(root: HTMLElement, saved: {
  startBlockIndex: number; startInBlock: number; endBlockIndex: number; endInBlock: number
} | null): boolean {
  if (!saved) return false
  const startBlock = root.children[saved.startBlockIndex] as HTMLElement | undefined
  const endBlock = root.children[saved.endBlockIndex] as HTMLElement | undefined
  if (!startBlock || !endBlock) return false
  const startPos = resolveBlockCaretOffset(startBlock, saved.startInBlock)
  const endPos = resolveBlockCaretOffset(endBlock, saved.endInBlock)
  if (!startPos || !endPos) return false
  const range = document.createRange()
  range.setStart(startPos.container, startPos.offset)
  range.setEnd(endPos.container, endPos.offset)
  const sel = window.getSelection()
  sel?.removeAllRanges()
  sel?.addRange(range)
  return true
}

function applyHeadingCommand(heading: string) {
  const root = editorRef.value
  if (!root || tableSelection.value.table) return
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return
  const range = sel.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return

  const currentLevel = getCurrentHeadingLevel()
  // 'p' = 「正文」：取消标题格式（与同级再点一次的既有语义一致 —— 逐字保留
  // 内容，只换块级标签；用户显式 px 字号/颜色保留，只剥 Chromium 的标题伪影）。
  const targetTag = heading === 'p' || currentLevel === heading ? 'p' : heading
  // 作用域 = 最近的 P/H1-H6 祖先（formattable）：blockquote>p 内段落可设标题
  //（旧 formatBlock 语义），表格/列表/PRE/DIV 容器永不重写（数据安全）。
  const formattable = (node: Node): HTMLElement | null => {
    let n: Node | null = node
    while (n && n !== root) {
      if (n.nodeType === Node.ELEMENT_NODE) {
        const tag = (n as HTMLElement).tagName
        if (tag === 'P' || isHeadingTag(tag)) return n as HTMLElement
      }
      n = n.parentNode
    }
    return null
  }
  const startB = formattable(range.startContainer)
  const endB = formattable(range.endContainer)
  const collected = new Set<HTMLElement>()
  if (startB) collected.add(startB)
  if (endB) collected.add(endB)
  // 顶层多块选区：补齐两锚点之间的全部 P/H1-H6（与旧 formatBlock 同覆盖面）。
  if (startB && endB && startB !== endB && startB.parentElement === root && endB.parentElement === root) {
    const children = Array.from(root.children) as HTMLElement[]
    const si = children.indexOf(startB)
    const ei = children.indexOf(endB)
    for (let i = Math.min(si, ei); i <= Math.max(si, ei); i++) {
      const tag = children[i].tagName
      if (tag === 'P' || isHeadingTag(tag)) collected.add(children[i])
    }
  }
  const blocks = Array.from(collected)
  if (blocks.length === 0) {
    // Bare text directly under the editor: let formatBlock create a block.
    // Do not run it when the selection reaches a structural block.
    const startParent = range.startContainer.nodeType === 3 ? range.startContainer.parentNode : range.startContainer
    const endParent = range.endContainer.nodeType === 3 ? range.endContainer.parentNode : range.endContainer
    if (startParent === root && endParent === root) {
      pushUndoState(true)
      document.execCommand('formatBlock', false, targetTag === 'p' ? '<p>' : `<${targetTag}>`)
    }
    return
  }

  pushUndoState(true)
  const saved = saveBlockSelection(root)
  const convertedHeadings: HTMLElement[] = []
  blocks.forEach((block) => {
    const isHeading = isHeadingTag(block.tagName)
    if (targetTag === 'p') {
      if (isHeading) {
        stripHeadingArtifactsInBlock(block)
        const converted = convertBlockTag(block, 'p')
        // 降级必须真回正文：块级标题排版（Word 粘贴的 <h3 style="font-size:…">）
        // 也要剥掉，否则标签换了视觉仍是标题（旧 formatBlock 同样保留，属残留缺口）。
        HEADING_TYPO_PROPS.forEach((p) => converted.style.removeProperty(p))
        if (!converted.getAttribute('style')) converted.removeAttribute('style')
      }
    } else {
      const converted = convertBlockTag(block, targetTag)
      if (converted !== block) convertedHeadings.push(converted)
    }
  })
  // Strip only Chromium artifacts from the headings actually created by this
  // command; untouched headings and user-set px typography are preserved.
  convertedHeadings.forEach((h) => {
    stripHeadingArtifactsInBlock(h)
    HEADING_TYPO_PROPS.forEach((p) => h.style.removeProperty(p))
    if (!h.getAttribute('style')) h.removeAttribute('style')
  })
  restoreBlockSelection(root, saved)
  // Manual DOM surgery does not fire an input event; serialize/emit explicitly
  // so the parent save/autosave persists the heading change.
  scheduleSerialize(true)
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

function restartOlNumbering(ol: HTMLOListElement | null): 'split' | 'nested' | 'already-first' | 'none' {
  if (!ol) return 'none'
  const li = findCurrentListItem()
  if (!li || !ol.contains(li)) return 'none'
  pushUndoState(true)
  const parentLi = ol.parentElement?.tagName === 'LI' ? ol.parentElement : null
  if (parentLi) {
    const parentOl = parentLi.parentElement?.tagName === 'OL' ? parentLi.parentElement : null
    if (!parentOl) return 'none'
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
    onInput(true)
    nextTick(() => pushCurrentState())
    return 'nested'
  }
  if (li.previousElementSibling) {
    const newOl = document.createElement('ol')
    let current: Node | null = li
    while (current) {
      const next = current.nextSibling
      newOl.appendChild(current)
      current = next
    }
    ol.parentNode?.insertBefore(newOl, ol.nextSibling)
    onInput(true)
    nextTick(() => pushCurrentState())
    return 'split'
  }
  // 已是首项：CSS 计数器每个 ol 天然从 1 起，无可重置（判据 #7）。旧实现写
  // inline counter-reset —— 不进 markdown 往返、视觉也无变化 = 静默无操作，删除；
  // 调用方按返回值提示用户。
  return 'already-first'
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
  applyFontSizeToSelection,
  insertEndnote,
  insertLink,
  applySuperscript,
  applySubscript,
  // 多 range 失效化通知（评审 N1）：宿主面板在编辑器外直接改 DOM 的保存面
  // （公式/mermaid 编辑）没有任何输入事件可依赖，须显式结束会话
  clearMultiRanges,
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
  insertMath,
  insertCodeBlock,
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
  clearFormatting,
  hasTextSelection,
  isEditorComposing,
  saveClearTarget,
  restoreClearTarget,
  clearClearTarget,
})
</script>

<style scoped>
.wysiwyg-editor {
  width: 100%;
  height: 100%;
  padding: 12px 24px 24px; /* wave-8 用户点名：首行与工具栏间距减半（24→12） */
  padding-bottom: var(--note-editor-padding-bottom, 24px);
  font-size: 14px;
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
    padding-bottom: calc(var(--mobile-tab-bar-offset, 72px) + 80px + env(safe-area-inset-bottom, 0px)) !important;
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

/* Insurance for paths that bypass render normalization (e.g. paste): an
   empty li must still be a clickable, caret-targetable line. */
.wysiwyg-editor :deep(li:empty) {
  min-height: 1.6em;
}

.wysiwyg-editor :deep(ul) {
  list-style-type: disc;
}

.wysiwyg-editor :deep(ol) {
  list-style-type: none;
  counter-reset: ol-counter;
}

.wysiwyg-editor :deep(ol > li) {
  position: relative;
  counter-increment: ol-counter;
}

/* Marker hangs in the left gutter (right-aligned) instead of flowing inline:
   an inline ::before before a block-level first child (<p> in loose list
   items) otherwise forms its own line and pushes the number onto the line
   ABOVE the text. Absolute positioning keeps number and first text line on
   the same row, matching tight items and ul disc markers. */
.wysiwyg-editor :deep(ol > li::before) {
  content: counters(ol-counter, ".") ". ";
  position: absolute;
  right: 100%;
  margin-right: 4px;
  white-space: nowrap;
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

/* 尾注：角标上标化 + 可点击；与引用角标 [N] 视觉区分（wave-2 B2）：
   〔N〕括形 + 主色；定义段文末；双向跳转到达时闪烁 */
.wysiwyg-editor :deep(.note-endnote-ref) {
  vertical-align: super;
  font-size: 0.75em;
  cursor: pointer;
  color: var(--color-primary, #2340b8);
  font-weight: 600;
}

.wysiwyg-editor :deep(.note-endnote-ref a),
.wysiwyg-editor :deep(.note-endnote-back) {
  color: inherit;
  text-decoration: none;
  cursor: pointer;
}

.wysiwyg-editor :deep(.note-endnote-def) {
  font-size: 0.9em;
  color: inherit;
  opacity: 0.95;
}

.wysiwyg-editor :deep(.note-endnote-flash) {
  animation: note-endnote-flash 1.2s ease;
}

@keyframes note-endnote-flash {
  0% { background-color: rgba(255, 200, 0, 0.55); }
  100% { background-color: transparent; }
}
</style>

<style>
/* 虚拟多 range（不连续选择，判据 #3）—— CSS Custom Highlight API 渲染通道：
   高亮不写 DOM（零 markdown 往返污染），只画在编辑器子树的文本上。
   颜色走皮肤主色（评审 A11；AGENTS 皮肤 token 纪律），color-mix 不支持时回退固定色。 */
.wysiwyg-editor::highlight(wt-multi-range),
.wysiwyg-editor ::highlight(wt-multi-range) {
  background-color: rgba(100, 149, 237, 0.35);
  background-color: color-mix(in srgb, var(--color-primary, #7AA35A) 32%, transparent);
}

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