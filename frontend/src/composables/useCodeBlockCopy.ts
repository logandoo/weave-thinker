// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

// One-click copy for Markdown code blocks.
//
// The markdown renderer (useMarkdown.ts) emits every fenced code block as
//
//   <div class="code-block">
//     <div class="code-block-header">
//       <span class="code-block-lang">…</span>
//       <button class="code-block-copy-btn" …>…</button>
//     </div>
//     <pre><code class="hljs">…</code></pre>
//   </div>
//
// v-html content cannot carry inline event handlers (DOMPurify strips them),
// so a single document-level delegated click listener wires every copy
// button — including markdown teleported to <body> (the note preview popup)
// and content inside contenteditable note editors. No per-component wiring is
// required.

const FEEDBACK_MS = 1600

function legacyCopyText(text: string): boolean {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.top = '-9999px'
  ta.style.left = '-9999px'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  const sel = document.getSelection()
  const prevRange = sel && sel.rangeCount > 0 ? sel.getRangeAt(0) : null
  ta.focus()
  ta.select()
  ta.setSelectionRange(0, text.length)
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(ta)
  if (prevRange && sel) {
    sel.removeAllRanges()
    sel.addRange(prevRange)
  }
  return ok
}

async function copyTextToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through to the execCommand path (clipboard permission denied).
    }
  }
  return legacyCopyText(text)
}

let installed = false

export function installGlobalCodeBlockCopy(): void {
  if (installed || typeof document === 'undefined') return
  installed = true
  document.addEventListener('click', (e) => {
    const target = e.target as Element | null
    const btn =
      target && typeof target.closest === 'function'
        ? target.closest('.code-block-copy-btn')
        : null
    if (!btn) return
    e.preventDefault()
    e.stopPropagation()
    // Ignore clicks while the success feedback is showing.
    if (btn.classList.contains('copied')) return
    const block = btn.closest('.code-block')
    const codeEl = block ? block.querySelector('pre code') : null
    if (!codeEl) return
    const text = codeEl.textContent || ''
    void copyTextToClipboard(text).then((ok) => {
      if (!ok || !btn.isConnected) return
      btn.classList.add('copied')
      btn.setAttribute('title', '已复制')
      btn.setAttribute('aria-label', '已复制')
      const prev = btn.getAttribute('data-copy-timer')
      if (prev) window.clearTimeout(Number(prev))
      btn.setAttribute(
        'data-copy-timer',
        String(
          window.setTimeout(() => {
            btn.classList.remove('copied')
            btn.setAttribute('title', '复制代码')
            btn.setAttribute('aria-label', '复制代码')
            btn.removeAttribute('data-copy-timer')
          }, FEEDBACK_MS),
        ),
      )
    })
  })
}
