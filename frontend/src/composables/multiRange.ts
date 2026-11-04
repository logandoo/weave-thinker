// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 虚拟多 range（不连续文本选择）的纯决策逻辑（笔记波 2026-10-25，判据 #3/#4）。
 *
 * 背景（实测取证 tests/verification_log.md R3）：Chromium 的 contenteditable **不支持**
 * 原生多 range 选区（ctrl+click/dblclick/drag 与程序化 addRange 均只保留一个 range），
 * 所以 Google Docs 式「按住 Cmd/Ctrl 选不连续文本」必须自研：编辑器维护一组**扁平文本
 * 偏移段**（Span），渲染走 CSS Custom Highlight API（零 DOM 污染），格式动作按段循环。
 *
 * 本模块只含纯函数（无 DOM）：段展开/提交/命中移除/有效性过滤 + IME 组合标志陈旧
 * 自愈判定。DOM 手术与手势接线在 `WysiwygEditor.vue`；行为级证据在
 * `frontend/e2e/note_multirange_toolbar_renumber_20261025.spec.ts`。
 *
 * 语义固定见 docs/PLAN_notes_multirange_20261025.md 的 Consistency Hub。
 */

export interface Span {
  /** 编辑器根 flat 文本偏移（含）。 */
  start: number
  /** 结束偏移（不含）；恒 start < end。 */
  end: number
}

/** 拉丁字母/数字/下划线（词段字符类）；CJK 单独按「单字」处理。 */
const WORD_CHAR = /[\p{L}\p{N}_]/u
const CJK_CHAR = /\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}/u

function isWordChar(ch: string | undefined): boolean {
  return !!ch && WORD_CHAR.test(ch) && !CJK_CHAR.test(ch)
}

/**
 * ctrl+单击（无拖拽）的词段展开（ADR D-20261025-mr5，对齐 Google Docs「选词」）：
 * - 光标处或左邻为拉丁词字符 → 扩到整个连续词段；
 * - 光标处（或左邻）为 CJK/假名 → 取单字（无分词器时的保守口径，已知限制）；
 * - 空白/标点/越界空文本 → null（不产生段）。
 * caret 越界自动钳制。
 */
export function expandWordRun(text: string, caret: number): Span | null {
  if (!text || text.length === 0) return null
  const c = Math.max(0, Math.min(caret, text.length))
  const pick = (i: number): string | undefined => text[i]
  // 词段：优先光标右，其次光标左（caret=词尾时右邻是空白）
  let anchor = -1
  if (isWordChar(pick(c))) anchor = c
  else if (c > 0 && isWordChar(pick(c - 1))) anchor = c - 1
  if (anchor >= 0) {
    let s = anchor
    let e = anchor + 1
    while (s > 0 && isWordChar(pick(s - 1))) s--
    while (e < text.length && isWordChar(pick(e))) e++
    return { start: s, end: e }
  }
  // CJK 单字
  if (pick(c) && CJK_CHAR.test(pick(c) as string)) return { start: c, end: c + 1 }
  if (c > 0 && pick(c - 1) && CJK_CHAR.test(pick(c - 1) as string)) return { start: c - 1, end: c }
  return null
}

/**
 * 提交一个段（ctrl 手势 mouseup / 单击词段）：
 * - 非法段（start>=end，钳制后）丢弃；
 * - 与既有段**全等** → 移除（toggle 取消语义，Google Docs 同口径）；
 * - 否则加入并把**相交或首尾相接**的段合并成并集；结果按 start 排序。
 */
export function commitSpan(spans: Span[], span: Span): Span[] {
  let start = Math.max(0, Math.min(span.start, span.end))
  let end = Math.max(span.start, span.end)
  if (start >= end) return spans.slice()
  const rest = spans.filter((s) => !(s.start === start && s.end === end))
  if (rest.length !== spans.length) return rest // 全等 → toggle 移除
  const merged: Span[] = []
  let lo = start
  let hi = end
  for (const s of spans) {
    if (s.end < lo || s.start > hi) {
      merged.push(s)
    } else {
      lo = Math.min(lo, s.start)
      hi = Math.max(hi, s.end)
    }
  }
  merged.push({ start: lo, end: hi })
  merged.sort((a, b) => a.start - b.start)
  void start // 显式：start/end 只作输入钳制用
  return merged
}

/** ctrl+单击命中已有段 → 整段移除（取消该段）。右开端点不算命中。 */
export function dropSpanCovering(spans: Span[], at: number): { spans: Span[]; removed: boolean } {
  const idx = spans.findIndex((s) => s.start <= at && at < s.end)
  if (idx < 0) return { spans, removed: false }
  const next = spans.slice()
  next.splice(idx, 1)
  return { spans: next, removed: true }
}

/** 文档长度变化后过滤：越界钳制、空段剔除（选区所在文本被删时段的自然失效）。 */
export function filterValidSpans(spans: Span[], docLen: number): Span[] {
  const out: Span[] = []
  for (const s of spans) {
    const start = Math.max(0, Math.min(s.start, docLen))
    const end = Math.max(0, Math.min(s.end, docLen))
    if (start < end) out.push({ start, end })
  }
  return out
}

const COMPOSITION_INPUT_TYPES = /^(insertCompositionText|insertFromComposition|deleteCompositionText|cancelComposition)$/

/**
 * IME 组合标志陈旧自愈判定（判据 #2）。根因：组合期发生整篇重渲染（v-html 重建子树）
 * 后，compositionend 在 detach 的旧文本节点上触发、永不到达编辑器 → `isComposing`
 * 永久卡 true → 「输入法组合中」永驻（实测打字后仍弹）。
 *
 * 输入事件判定：浏览器在事件里带权威 `isComposing` 标志 —— 事件报「非组合」且
 * inputType 非组合系 → 标志必陈旧，可复位。`isComposing` 属性缺失（老浏览器）
 * 保守不复位。
 */
export function shouldHealComposingFromInput(
  inputType: string | undefined,
  isComposingProp: boolean | undefined,
): boolean {
  if (isComposingProp !== false) return false
  if (!inputType) return false
  return !COMPOSITION_INPUT_TYPES.test(inputType)
}

/**
 * keydown 判定：可打印字符键（key.length===1）且浏览器报非组合 → 陈旧复位。
 * 修饰键/功能键（'Shift'/'Process' 等 length>1）不参与（组合期也会以非组合态触发）。
 */
export function shouldHealComposingFromKey(
  key: string | undefined,
  isComposingProp: boolean | undefined,
): boolean {
  if (isComposingProp !== false) return false
  return !!key && key.length === 1
}
