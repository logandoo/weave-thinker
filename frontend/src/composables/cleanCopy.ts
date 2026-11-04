// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * 笔记复制/剪切净化通道（判据 #28/#31/#33/#36/#37/#43）。
 *
 * 背景：浏览器原生复制会把 **computed style 逐 run 内联**进 text/html ——
 * 全字体栈（如 `Inter, "Noto Sans SC", -apple-system, …`）、背景色、边框全部
 * 跟进剪贴板。粘贴 Word/WPS 后：
 *   ① 字体错乱：行级 run 携带内联 font-family，粘贴显示回退字体（WPS 上常见
 *      "明朝"），且在 Word 里换字体无法统一（各 run 各自的内联字体互相打架）；
 *   ② "无法删除的横线"：h1/h2 的 border-bottom、hr、链接下框线被 Word 转成
 *      **段落边框**（普通用户无法删除）。
 * 本模块在 copy/cut 事件里接管剪贴板，产出**自包含、Word 友好**的 HTML：
 *   - 剥掉字体/字号/字距/背景/边框等 run 级样式，语义标签保留
 *     （strong/em/u/s/sup/sub/a/标题/列表/表格）；
 *   - 字体只在**段级**显式声明（中文栈 + 代码等宽栈），行级 run 零字体 ——
 *     粘贴后字体一致且在 Word 里可整体切换（D-102）；
 *   - **对齐与文字色保留**（D-102 修订：字体统一 ≠ 抹掉用户排版意图）；
 *   - hr → 可删除的文本分隔线；表格边框仅 td/th 显式 1px（Word 真表格边框，
 *     可选中删除）；
 *   - text/plain 与 HTML 语义一致（列表带编号、标题带 #、表格行化）；
 *   - payload 为 CF_HTML（StartFragment/EndFragment + 字节偏移）；
 *   - 工具条/按钮/SVG/注释节点等不进剪贴板；KaTeX/mermaid/echarts 部件降级为
 *     源码文本，避免部件字体与图形噪音。
 *
 * 作用域：`.wysiwyg-editor`（笔记板块 + 工作台模式共用）、`.note-preview-body`
 * （笔记预览弹层）、`.file-preview-markdown`（md 文件预览）。
 */

const FONT = "font-family:'PingFang SC','Microsoft YaHei',sans-serif"
const MONO = "font-family:Consolas,'Courier New',monospace"
const BLOCK_SELECTOR = 'p,li,h1,h2,h3,h4,h5,h6,td,th,blockquote,div'

const DROP_SELECTOR = 'button,sup.math-controls,svg,script,style,.code-block-header,.math-controls,.mermaid-controls'

function wrapWith(el: HTMLElement, tag: string) {
  const w = document.createElement(tag)
  el.parentNode?.insertBefore(w, el)
  w.appendChild(el)
}

function replaceWithText(el: HTMLElement, text: string, mono = false) {
  // 行内宿主（span.math-editable 等）必须换 span —— 换 <p> 会产生非法
  // 嵌套 <p> 并把段落切断（评审 A#5：复制含行内公式的段落被劈成多段）。
  const inline = el.tagName === 'SPAN' ||
    (el.parentElement && !/^(P|DIV|LI|TD|TH|BLOCKQUOTE|BODY)$/.test(el.parentElement.tagName))
  const p = document.createElement(inline ? 'span' : 'p')
  p.setAttribute('style', mono ? MONO : FONT)
  p.textContent = text
  el.parentNode?.replaceChild(p, el)
}

/** 从净化后的 DOM 派生与 HTML 语义一致的纯文本（判据 #36）。 */
export function buildCleanCopyText(root: HTMLElement): string {
  const lines: string[] = []
  const inlineText = (n: Node): string => {
    const read = (node: Node): string => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent || ''
      if (node.nodeType !== Node.ELEMENT_NODE) return ''
      const el = node as HTMLElement
      if (el.tagName === 'BR') return '\n'
      return Array.from(node.childNodes).map(read).join('')
    }
    return read(n)
      .replace(/\u00a0/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .replace(/ *\n */g, '\n')
      .replace(/\n{3,}/g, '\n\n')
  }
  const walkList = (list: Element, depth: number) => {
    // 编号与 HTML/可见一致（评审 A I4）：honour ol[start] 与 li[value]
    const startAttr = parseInt((list.getAttribute('start') || '1'), 10)
    let idx = (isNaN(startAttr) ? 1 : startAttr) - 1
    for (const li of Array.from(list.children)) {
      if (li.tagName !== 'LI') continue
      const vAttr = parseInt(li.getAttribute('value') || '', 10)
      idx = isNaN(vAttr) ? idx + 1 : vAttr
      const pad = '    '.repeat(depth)
      const marker = list.tagName === 'OL' ? `${idx}. ` : '- '
      let head = ''
      const subs: Element[] = []
      for (const c of Array.from(li.childNodes)) {
        if (c.nodeType === Node.ELEMENT_NODE && ((c as Element).tagName === 'OL' || (c as Element).tagName === 'UL')) {
          subs.push(c as Element)
        } else if (c.nodeType !== Node.COMMENT_NODE) {
          // 注释节点（list-break 等）不得进纯文本（评审 A I1）
          head += inlineText(c)
        }
      }
      lines.push(pad + marker + head.trim())
      subs.forEach(s => walkList(s, depth + 1))
    }
  }
  const walk = (node: Node) => {
    for (const c of Array.from(node.childNodes)) {
      if (c.nodeType === Node.TEXT_NODE) {
        const t = (c.textContent || '').trim()
        if (t) lines.push(t)
        continue
      }
      if (c.nodeType !== Node.ELEMENT_NODE) continue
      const el = c as HTMLElement
      switch (el.tagName) {
        case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': {
          const n = parseInt(el.tagName.slice(1), 10)
          lines.push('#'.repeat(n) + ' ' + inlineText(el).trim())
          break
        }
        case 'OL': case 'UL':
          walkList(el, 0)
          break
        case 'PRE':
          lines.push((el.textContent || '').replace(/\n+$/, ''))
          break
        case 'TABLE': {
          for (const tr of Array.from(el.querySelectorAll('tr'))) {
            const cells = Array.from(tr.children).map(td => inlineText(td).trim())
            lines.push('| ' + cells.join(' | ') + ' |')
          }
          break
        }
        case 'BLOCKQUOTE':
          lines.push('> ' + inlineText(el).trim())
          break
        case 'P': case 'DIV': case 'SECTION': case 'ARTICLE': {
          if (el.querySelector('ol,ul,table,pre,blockquote')) walk(el)
          else {
            const t = inlineText(el).trim()
            if (t) lines.push(t)
          }
          break
        }
        default: {
          const t = inlineText(el).trim()
          if (t) lines.push(t)
        }
      }
    }
  }
  walk(root)
  return lines.join('\n')
}

/**
 * 把一个已克隆的 DOM 子树净化为 Word 友好 HTML（就地变换，返回 innerHTML）。
 */
export function buildCleanCopyHtml(root: HTMLElement): string {
  // ① 剥界面 chrome + 注释节点（`<!--list-break-->` 等序列化标记不得进剪贴板）
  root.querySelectorAll(DROP_SELECTOR).forEach(el => el.remove())
  const stripComments = (node: Node) => {
    Array.from(node.childNodes).forEach(c => {
      if (c.nodeType === Node.COMMENT_NODE) c.remove()
      else if (c.nodeType === Node.ELEMENT_NODE) stripComments(c)
    })
  }
  stripComments(root)

  // ② 代码块外壳 → <pre><code>
  root.querySelectorAll('.code-block').forEach(block => {
    const code = block.querySelector('code')
    const pre = document.createElement('pre')
    pre.setAttribute('style', MONO)
    const c = document.createElement('code')
    c.textContent = code ? (code.textContent || '') : (block.textContent || '')
    pre.appendChild(c)
    block.parentNode?.replaceChild(pre, block)
  })

  // ③ 公式部件 → TeX 源码文本（KaTeX 嵌套 span 的字体噪音不进剪贴板）
  root.querySelectorAll('.math-editable, .katex-display, .math-rendered-content').forEach(el => {
    const host = el.closest('.math-editable') as HTMLElement | null
    const target = host || (el as HTMLElement)
    if (!root.contains(target)) return
    const tex = target.getAttribute('data-tex') || (el as HTMLElement).getAttribute('data-tex') || (el.textContent || '')
    const display = target.getAttribute('data-display-mode') === 'true' || el.classList.contains('katex-display')
    replaceWithText(target, display ? `$$${tex}$$` : `$${tex}$`)
  })
  root.querySelectorAll('.katex').forEach(el => {
    const t = el.textContent || ''
    el.replaceWith(document.createTextNode(t))
  })

  // ④ 图表部件 → fenced source（<pre> 同时保住 plain 换行与 HTML 可读性）
  const replaceWithFence = (el: HTMLElement, lang: string, source: string) => {
    const pre = document.createElement('pre')
    pre.setAttribute('style', MONO)
    const code = document.createElement('code')
    code.textContent = '```' + lang + '\n' + source.replace(/\n+$/, '') + '\n```'
    pre.appendChild(code)
    el.parentNode?.replaceChild(pre, el)
  }
  root.querySelectorAll('.mermaid-block, .mermaid-rendered-content').forEach(el => {
    const host = (el.closest('.mermaid-block') || el) as HTMLElement
    if (!root.contains(host)) return
    const src = host.getAttribute('data-mermaid-source') || host.textContent || ''
    replaceWithFence(host, 'mermaid', src)
  })
  root.querySelectorAll('.echarts-block').forEach(el => {
    const host = el as HTMLElement
    if (!root.contains(host)) return
    const src = host.getAttribute('data-echarts-source') || host.textContent || ''
    replaceWithFence(host, 'echarts', src)
  })

  // ⑤ hr → 可删除的文本分隔线（Word 里 hr/边框=「无法删除的横线」）
  root.querySelectorAll('hr').forEach(el => replaceWithText(el as HTMLElement, '──────'))

  // ⑥ 采样「保留项」（对齐/文字色，D-102 修订：字体统一但排版意图保留）
  const aligns = new Map<Element, string>()
  const colors = new Map<Element, string>()
  const semanticOverrides = new Map<Element, string[]>()
  root.querySelectorAll('[style]').forEach(el => {
    const st = (el as HTMLElement).style
    if (st.textAlign && st.textAlign !== 'left' && st.textAlign !== 'start') aligns.set(el, st.textAlign)
    if (st.color) colors.set(el, st.color)
    const overrides: string[] = []
    if (st.fontWeight === 'normal' || st.fontWeight === '400') overrides.push('font-weight:normal')
    if (st.fontStyle === 'normal') overrides.push('font-style:normal')
    if (st.textDecorationLine === 'none' || st.textDecoration === 'none') overrides.push('text-decoration:none')
    if (overrides.length) semanticOverrides.set(el, overrides)
  })

  // ⑦ 行级样式 → 语义标签（粗/斜/下划线/删除线），然后清空全部 style
  root.querySelectorAll('span[style]').forEach(sp => {
    const st = (sp as HTMLElement).style
    const weight = st.fontWeight || ''
    if (st.fontStyle === 'italic') wrapWith(sp as HTMLElement, 'em')
    if (weight === 'bold' || weight === 'bolder' || parseInt(weight, 10) >= 600) wrapWith(sp as HTMLElement, 'strong')
    const deco = st.textDecorationLine || st.textDecoration || ''
    if (/underline/.test(deco)) wrapWith(sp as HTMLElement, 'u')
    if (/line-through/.test(deco)) wrapWith(sp as HTMLElement, 's')
  })
  // 任务列表勾选框 → 文本
  root.querySelectorAll('input[type="checkbox"]').forEach(inp => {
    const box = (inp as HTMLInputElement).checked ? '[x] ' : '[ ] '
    inp.replaceWith(document.createTextNode(box))
  })

  // ⑧ 属性白名单：class/id/data-*/style 全部剥离
  const KEEP: Record<string, string[]> = {
    A: ['href', 'title'],
    IMG: ['src', 'alt', 'width', 'height'],
    OL: ['start'],
    LI: ['value'], // 编号真值随行（评审 A I4：与纯文本/可见一致）
    TD: ['colspan', 'rowspan'],
    TH: ['colspan', 'rowspan'],
    // 媒体/嵌入载荷（评审 A#6：src 被剥后回贴笔记会在保存时被整块丢弃 = 丢失）
    AUDIO: ['src', 'controls', 'poster', 'width', 'height'],
    VIDEO: ['src', 'controls', 'poster', 'playsinline', 'width', 'height'],
    SOURCE: ['src', 'type'],
    IFRAME: ['src', 'allowfullscreen', 'width', 'height'],
  }
  root.querySelectorAll('*').forEach(el => {
    const keep = KEEP[el.tagName] || []
    Array.from(el.attributes).forEach(attr => {
      if (!keep.includes(attr.name)) el.removeAttribute(attr.name)
    })
  })

  // ⑨ 字体只在段级显式（D-102）：统一中文栈；代码等宽；恢复对齐/文字色；链接显式下划线色
  root.querySelectorAll(BLOCK_SELECTOR).forEach(el => {
    if (el.closest('pre')) return
    let style = FONT
    const align = aligns.get(el)
    if (align) style += ';text-align:' + align
    const color = colors.get(el)
    if (color) style += ';color:' + color
    ;(el as HTMLElement).setAttribute('style', style)
  })
  root.querySelectorAll('pre').forEach(el => (el as HTMLElement).setAttribute('style', MONO))
  root.querySelectorAll('table').forEach(el =>
    (el as HTMLElement).setAttribute('style', 'border-collapse:collapse'))
  root.querySelectorAll('td,th').forEach(el => {
    let style = FONT + ';border:1px solid #999'
    const align = aligns.get(el)
    if (align) style += ';text-align:' + align
    const color = colors.get(el)
    if (color) style += ';color:' + color
    ;(el as HTMLElement).setAttribute('style', style)
  })
  // 非块级承载的文字色（span 等）保留为显式 color（零字体、可整体改色）。
  // 注意：holder 是游离克隆树，el.isConnected 恒 false —— 用 root.contains 判存活。
  colors.forEach((color, el) => {
    if (!root.contains(el)) return
    if (el.matches(BLOCK_SELECTOR) || el.tagName === 'TD' || el.tagName === 'TH') return
    const override = semanticOverrides.get(el) || []
    ;(el as HTMLElement).setAttribute('style', [...override, 'color:' + color].join(';'))
  })
  semanticOverrides.forEach((overrides, el) => {
    if (!root.contains(el) || colors.has(el)) return
    // Append instead of overwriting: block elements already carry the segment
    // font stack / border / color written by step ⑨.
    const existing = (el as HTMLElement).getAttribute('style')
    const merged = existing ? existing + ';' + overrides.join(';') : overrides.join(';')
    ;(el as HTMLElement).setAttribute('style', merged)
  })
  root.querySelectorAll('a[href]').forEach(el => {
    const href = (el as HTMLElement).getAttribute('href') || ''
    // 只放行安全协议（评审 B Minor：javascript:/data: 不得随剪贴板扩散）
    if (!/^(https?:|mailto:|#|\/|\.)/i.test(href.trim())) {
      el.removeAttribute('href')
      return
    }
    ;(el as HTMLElement).setAttribute('style', 'color:#0563C1;text-decoration:underline')
  })

  return '<meta charset="utf-8">' + root.innerHTML
}

/**
 * 剪贴板 Fragment 标记（判据 #43 · D-105 修订）：只发 StartFragment/EndFragment
 * 注释标记。CF_HTML 头（Version/StartHTML 字节偏移）由浏览器/OS 在写系统剪贴板
 * 时组装 —— 页面自发头会把头文本当正文粘进目标（评审 A C1 实证：应用内复制→
 * 粘贴把 5 行 Version/StartHTML 垃圾写进笔记并存档），偏移也算不直（C2 实证 +48
 * 字节偏差）。此为平台契约修正，非功能回退。
 */
export function wrapFragmentMarkers(inner: string): string {
  return '<!--StartFragment-->' + inner + '<!--EndFragment-->'
}

let cleanCopyInstalled = false;

// IME 组合期检测（评审 B M3：组合中删除会卡死 IME 状态）
let cleanCopyComposing = false;

/**
 * 全局 copy/cut 拦截：作用域内选区复制/剪切时写入净化负载（原生 computed-style
 * 内联不进剪贴板）。cut 额外执行删除 + 通知编辑器序列化（判据 #33）。
 */
export function installGlobalCleanCopy() {
  if (cleanCopyInstalled) return // HMR 幂等（与 useCodeBlockCopy 同口径）
  cleanCopyInstalled = true
  document.addEventListener('compositionstart', () => { cleanCopyComposing = true }, true)
  document.addEventListener('compositionend', () => { cleanCopyComposing = false }, true)

  const buildPayload = (sel: Selection): { html: string; text: string } | null => {
    if (sel.rangeCount === 0 || sel.isCollapsed) return null
    const inScope = (n: Node): boolean => {
      const el = n.nodeType === Node.ELEMENT_NODE ? (n as Element) : n.parentElement
      return !!el && !!el.closest('.wysiwyg-editor, .note-preview-body, .file-preview-markdown')
    }
    // 多 range 选区（Firefox）必须全部在笔记作用域内，避免无关内容被净化改写
    for (let i = 0; i < sel.rangeCount; i++) {
      if (!inScope(sel.getRangeAt(i).commonAncestorContainer)) return null
    }
    const holder = document.createElement('div')
    for (let i = 0; i < sel.rangeCount; i++) {
      holder.appendChild(sel.getRangeAt(i).cloneContents())
    }
    const html = buildCleanCopyHtml(holder)
    // 纯文本从**规整后**的树派生（评审 A I1）：代码块外壳/公式/图表/勾选/hr 已归一，
    // 与 HTML 语义一致；注释节点已在此前剥离。
    const text = buildCleanCopyText(holder)
    return { html, text }
  }

  document.addEventListener('copy', (e: ClipboardEvent) => {
    try {
      const sel = window.getSelection()
      if (!sel) return
      const payload = buildPayload(sel)
      if (!payload || !e.clipboardData) return
      e.clipboardData.setData('text/html', wrapFragmentMarkers(payload.html))
      e.clipboardData.setData('text/plain', payload.text)
      e.preventDefault()
    } catch {
      // 任何异常都回落到浏览器原生复制，绝不阻断用户复制
    }
  })

  document.addEventListener('cut', (e: ClipboardEvent) => {
    try {
      const sel = window.getSelection()
      if (!sel || cleanCopyComposing) return // IME 组合期不动（评审 B M3）
      const payload = buildPayload(sel)
      if (!payload || !e.clipboardData) return
      // 受影响的编辑器宿主（评审 A I6：多 range 全量收集；只读预览面不删除）
      const hosts = new Set<HTMLElement>()
      for (let i = 0; i < sel.rangeCount; i++) {
        const r = sel.getRangeAt(i)
        const el = r.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
          ? (r.commonAncestorContainer as Element)
          : r.commonAncestorContainer.parentElement
        const host = el?.closest('.wysiwyg-editor') as HTMLElement | null
        if (host) hosts.add(host)
      }
      e.clipboardData.setData('text/html', wrapFragmentMarkers(payload.html))
      e.clipboardData.setData('text/plain', payload.text)
      e.preventDefault()
      if (!hosts.size) return // 只读预览面：剪切退化为复制，绝不删只读 DOM
      // pre-change 快照（评审 A I2 / B I3）：cut 先于 beforeinput，且 preventDefault
      // 会抑制 deleteByCut 的快照 —— 显式压栈保证 Ctrl+Z 可回。
      hosts.forEach(h => (h as unknown as { __pushUndoState?: (f?: boolean) => void }).__pushUndoState?.(true))
      // 编辑规范删除（评审 A I3）：execCommand('delete') 执行块合并级删除并自带
      // input 事件（触发重新序列化）；失败回落 Range 删除 + 合成双事件通知全部宿主。
      const ok = document.execCommand('delete')
      if (!ok) {
        sel.deleteFromDocument()
        hosts.forEach(h => {
          h.dispatchEvent(new InputEvent('beforeinput', { inputType: 'deleteByCut', bubbles: true, cancelable: true }))
          h.dispatchEvent(new InputEvent('input', { bubbles: true }))
        })
      }
    } catch {
      // 异常时净化负载已在剪贴板且默认动作已被取消（默认动作会用原生脏负载覆盖）。
      // 若删除前抛错则内容未删（安全向）；若删除后通知抛错则待下次编辑序列化。
    }
  })
}
