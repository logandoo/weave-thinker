// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * useContextTokens — CJK 感知的上下文 token 估算（纯函数）。
 *
 * 与后端 context_compressor 的估算口径同源：CJK 字符按 1 token、
 * 其余按 4 字符 1 token。从 stores/chat.ts 抽出以便 node 单测。
 */
import type { Message, ContextInfo } from '../types'

export const CONTEXT_INFO_STORAGE_KEY = 'chatllm_context_info'

export function loadContextInfoStorage(): Record<string, ContextInfo> {
  try {
    const raw = localStorage.getItem(CONTEXT_INFO_STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') return parsed
    }
  } catch {
    // ignore
  }
  return {}
}

export function persistContextInfoStorage(map: Record<string, ContextInfo>) {
  try {
    localStorage.setItem(CONTEXT_INFO_STORAGE_KEY, JSON.stringify(map))
  } catch {
    // ignore (private mode / quota)
  }
}

export function isCjkCodePoint(cp: number): boolean {
  return (
    (cp >= 0x3000 && cp <= 0x303f) || // CJK punctuation
    (cp >= 0x3400 && cp <= 0x4dbf) || // CJK Extension A
    (cp >= 0x4e00 && cp <= 0x9fff) || // CJK Unified Ideographs
    (cp >= 0xf900 && cp <= 0xfaff) || // CJK Compatibility
    (cp >= 0x20000 && cp <= 0x2fa1f)  // CJK Ext B..F
  )
}

export function estimateTextTokens(text: string): number {
  if (!text) return 0
  const chars = [...text]
  let cjk = 0
  for (const ch of chars) {
    const cp = ch.codePointAt(0)!
    if (isCjkCodePoint(cp)) cjk++
  }
  const other = chars.length - cjk
  return cjk + Math.floor(other / 4)
}

export function estimateMessagesTokens(messages: Message[]): number {
  let total = 0
  for (const m of messages) {
    total += estimateTextTokens(m.content || '') + 8
    if (m.reasoning_content) total += estimateTextTokens(m.reasoning_content)
    if (m.tool_calls) {
      try {
        const parsed = JSON.parse(m.tool_calls)
        if (Array.isArray(parsed)) {
          for (const tc of parsed) {
            const args = tc?.function?.arguments
            if (typeof args === 'string') total += estimateTextTokens(args)
          }
        }
      } catch {
        // tool_calls is sometimes an opaque persisted blob — skip
      }
    }
  }
  return total
}
