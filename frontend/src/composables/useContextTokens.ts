// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * useContextTokens — CJK 感知的上下文 token 估算（纯函数）。
 *
 * 与后端 context_compressor 的估算口径同源：CJK 字符按 1 token、
 * 其余按 4 字符 1 token。从 stores/chat.ts 抽出以便 node 单测。
 */
import type { ContextInfo, Message } from '../types'

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

// ---------- P2 (2026-09-05): 持久化 context_info 的前端口径 ----------

/** 解析消息行上的持久化 context_info（DB 存 JSON 字符串，与 tool_calls 同约定）。 */
export function parseContextInfo(raw: unknown): ContextInfo | null {
  if (!raw || typeof raw !== 'string') return null
  try {
    const o = JSON.parse(raw)
    if (
      o && typeof o === 'object'
      && typeof o.tokens === 'number' && o.tokens >= 0
      && typeof o.context_length === 'number' && o.context_length > 0
    ) {
      return o as ContextInfo
    }
  } catch {
    // ignore
  }
  return null

}

/** 从消息列表取最后一条带持久化 context_info 的 assistant 消息的快照
 *  （跨设备播种源：手机端发出的轮次在桌面端打开也能看到本轮 tokens）。 */
export function pickLatestContextInfo(msgs: readonly Message[]): ContextInfo | null {
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    const m = msgs[i]
    if (m.role === 'assistant' && m.context_info) {
      const parsed = parseContextInfo(m.context_info)
      if (parsed) return parsed
    }
  }
  return null
}

/** 头部徽章数字格式（千分位）。 */
export function formatContextTokens(n: number): string {
  return (n || 0).toLocaleString('en-US')
}

/** 徽章 tooltip 文案（实测/估算 + 窗口占比 + 口径说明）。 */
export function contextTokenTooltipText(info: ContextInfo | null | undefined): string {
  if (!info || !info.context_length) return ''
  const pct = info.context_length > 0
    ? ((info.tokens / info.context_length) * 100).toFixed(1)
    : '0.0'
  const src = info.measured ? '实测' : '估算'
  return `上下文窗口 ${info.context_length.toLocaleString('en-US')} tokens · 本轮请求 ${src} ${pct}%（含系统提示与工具定义）`
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

// estimateMessagesTokens 已删除（P4 2026-09-02）：纯可见消息口径漏掉系统提示
// 词与工具 schema，加载期 fallback 低估 14 倍（audit F2），徽章改为等权威快照。
