// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 片段插入构造器（笔记/工作台 LaTeX + 代码块直接输入）。
 * 纯函数、无 DOM —— 供 WysiwygEditor 插入 API 与单测共用（test_snippet_insert.cjs）。
 *
 * 契约（Consistency Hub，docs/PLAN_sidebar_zen_latex_20261003.md）：
 * - 代码块产出 `<pre><code class="hljs language-<lang>">…</code></pre>`，
 *   **不带** .code-block 渲染壳（header/语言标签/复制按钮）——htmlToMarkdown
 *   的 `case 'pre'` 直接序列化为 ```lang 围栏，语言标签不会泄漏成正文段落；
 *   重新载入时 renderMarkdownToHtml 会再包上 .code-block 展示壳。
 * - lang 白名单清洗 `[^A-Za-z0-9_+.#-]`（# 放行 c#/f#），与 useMarkdown.wrapCodeBlock 同口径。
 * - 数学公式 HTML 不在此处构造：统一用 useMarkdown 导出的 renderMathSafe
 *   （math-editable + data-tex 形状唯一源）。
 */

/** 清洗代码块语言标记：去首尾空白 + 仅保留 [A-Za-z0-9_+.#-]（# 放行 c#/f#）。空/纯非法 → ''。 */
export function sanitizeCodeLang(lang: string): string {
  return (lang || '').trim().replace(/[^A-Za-z0-9_+#.-]/g, '')
}

/** HTML 转义（代码文本入 pre/code 前必过）。 */
export function escapeSnippetHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 构造编辑器内插入用的代码块 HTML（pre>code，可编辑，序列化为 ```lang 围栏）。
 * @param code 代码原文（保留换行/制表符）
 * @param lang 语言标记（清洗后拼入 language-<lang> class；空则仅 hljs）
 */
export function buildCodeBlockHtml(code: string, lang: string): string {
  const safeLang = sanitizeCodeLang(lang)
  const cls = safeLang ? `hljs language-${safeLang}` : 'hljs'
  return `<pre><code class="${cls}">${escapeSnippetHtml(code)}</code></pre>`
}

/**
 * 构造 markdown 代码围栏（GFM）：
 * - 围栏长度 = max(3, 文内最长反引号游程 + 1)——内嵌 ``` 不会截断围栏；
 * - 文本**原样**发射（不 trim）——首尾空行 round-trip 保真（真 marked 实证：
 *   `\n?fence\n` + text + `\n?fence\n\n` 形状恰被 marked 各剥一行，text 恒等还原）。
 */
export function buildCodeFence(lang: string, text: string): string {
  const safeLang = sanitizeCodeLang(lang)
  const runs = text.match(/`+/g)
  const longest = runs ? Math.max(...runs.map((r) => r.length)) : 0
  const fence = '`'.repeat(Math.max(3, longest + 1))
  return '\n' + fence + safeLang + '\n' + text + '\n' + fence + '\n\n'
}

/**
 * 围栏感知的空行折叠（htmlToMarkdown 末段专用）：仅折叠**围栏外**的 3+ 连行
 * → 两个换行；围栏（``` 或更长）内部原样保留——否则 buildCodeFence 的首尾
 * 空行保真会被整体替换击穿（A4.9 round3 评审定位）。行扫描按 CommonMark
 * 口径：开栏=行首 3+ 反引号且 info 串不含反引号；闭栏=反引号游程 ≥ 开栏长度
 * 且行内无其他字符。未闭合围栏整体按围栏保护（保守）。
 */
export function collapseExtraBlankLines(md: string): string {
  const lines = md.split('\n')
  const kept: string[] = []
  const fences: string[] = []
  let fenceLen = 0
  let fenceLines: string[] = []
  for (const line of lines) {
    const m = /^(`{3,})(.*)$/.exec(line)
    if (fenceLen === 0) {
      if (m && !m[2].includes('`')) {
        fenceLen = m[1].length
        fenceLines = [line]
      } else {
        kept.push(line)
      }
    } else {
      fenceLines.push(line)
      if (m && m[1].length >= fenceLen && m[2].trim() === '') {
        fenceLen = 0
        fences.push(fenceLines.join('\n'))
        kept.push('\u0000FENCE' + (fences.length - 1) + '\u0000')
      }
    }
  }
  if (fenceLen !== 0 && fenceLines.length) {
    fences.push(fenceLines.join('\n'))
    kept.push('\u0000FENCE' + (fences.length - 1) + '\u0000')
  }
  const collapsed = kept.join('\n').replace(/\n{3,}/g, '\n\n')
  return collapsed.replace(/\u0000FENCE(\d+)\u0000/g, (_all, i) => fences[Number(i)])
}

/**
 * 数学定界符保护（serializeMathDelimited ↔ tokenizer 成对使用）：
 * tex 含「闭符序列」时把可触发提前闭合的字符映射进私用区，解析捕获后原样还原。
 * - inline `\(…\)`：闭符=\) → 序列 \) → PUA0（裸 ) 不动，`f(x) = $5` 源保持可读）
 * - display `\[…\]`：闭符=\] 或「独立行 ]」（LLM 容错分支）→ \]→PUA1、行首 ]→PUA2
 * 输入本身含 PUA0-2 时跳过编码（保留早闭风险也不损坏原文，罕见到不值得再加转义层）。
 */
// 闭合标签编解码（ + tag 单遍扫描；carve 自含，无模块常量）。
// tag 表：0=字面  · 1=\) · 2=\] · 3=行首 ] · 4=\n · 5=\r。
// 转义先行（输入字面  → +'0'），故**任意输入**编解码恒等（Q1/Q2 契约）；
// 旧 -8 直映方案对转义码自身开环，已废弃。
export function encodeMathClosers(tex: string, display: boolean): string {
  let out = tex.split('\uE0FF').join('\uE0FF0')
  if (display) {
    out = out.replace(/\\\]/g, '\uE0FF2')
    out = out.replace(/^([ \t]*)\]/gm, (_all, lead: string) => lead + '\uE0FF3')
    out = out.replace(/\n/g, '\uE0FF4').replace(/\r/g, '\uE0FF5')
    return out
  }
  out = out.replace(/\\\)/g, '\uE0FF1')
  out = out.replace(/\n/g, '\uE0FF4').replace(/\r/g, '\uE0FF5')
  return out
}

/** 仅转义输入标记符（块形态/美元形态用：内容不编码，但字面  仍须恒等）。 */
export function escapePuaMarkers(tex: string): string {
  return tex.split('\uE0FF').join('\uE0FF0')
}

export function decodeMathClosers(tex: string): string {
  const map = {
    '0': '\uE0FF', '1': '\\)', '2': '\\]', '3': ']', '4': '\n', '5': '\r',
  }
  let out = ''
  for (let i = 0; i < tex.length; i++) {
    if (tex.charCodeAt(i) === 0xE0FF && i + 1 < tex.length) {
      const rep = map[tex[i + 1]]
      if (rep !== undefined) {
        out += rep
        i++
        continue
      }
    }
    out += tex[i]
  }
  return out
}

/**
 * 精确剥一层框架换行（替代 .trim()：只剥定界形态自带的首尾换行，用户 tex 边缘空白保留）。
 */
export function stripMathFraming(s: string): string {
  let out = s
  if (out.startsWith('\n')) out = out.slice(1)
  if (out.endsWith('\n')) out = out.slice(0, -1)
  return out
}

/**
 * 字面 TeX 提示（raw-HTML 路径专用：math 在 raw-HTML 块内无法被 markdown 管线
 * 重渲染，按「保持 TeX 源为字面文本」契约输出）：
 * - 定界美元用 HTML 实体 &#36;——前端（实体渲染为 $）与导出提取正则（见不到 $）
 *   双侧都不解析，字面语义一致（旧 $…$/$$…$$ 提示会被导出层 typeset=分歧）；
 * - 全量 HTML 转义（data-tex 解码后的源不得开标签）；
 * - 内容自带 \) 等定界符序列同样双侧安全（无真定界符可配对）。
 */
export function mathLiteralHtml(decoded: string, display: boolean): string {
  // 反斜杠也实体化（&#92; 视觉恒等）：字面体内的 \)/\] 不得与文内他处真定界符误配对
  const body = escapeSnippetHtml(decoded).replace(/\\/g, '&#92;')
  const d = '&#36;'
  return display ? (d + d + body + d + d) : (d + body + d)
}
