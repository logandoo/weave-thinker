// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

// 渲染缓存键 —— 纯函数，独立成模以便 node 单测（useMarkdown 直接依赖重，
// 无法在 node 环境整模块导入）。
//
// 旧实现 `len + head4 + tail4` 会碰撞：任何同长度、同头尾的内容共键串稿
// （A4.9 复审 C6 盲区）。现改为全串 FNV-1a 64 风格混合散列（十六进制），
// 碰撞概率与全串哈希同级，键长 O(1)。
//
// in-memory-only 缓存键不是安全边界；不追求密码学强度，只求不工程性碰撞。

export function stableRenderCacheKey(content: string): string {
  // FNV-1a 32-bit ×2 轮（不同素数偏移）拼 64 位等价宽度，降低同尾碰撞
  let h1 = 0x811c9dc5
  let h2 = 0x01000193 ^ 0x811c9dc5
  for (let i = 0; i < content.length; i++) {
    const c = content.charCodeAt(i)
    h1 ^= c
    h1 = Math.imul(h1, 0x01000193) >>> 0
    h2 ^= (c + i) & 0xffff
    h2 = Math.imul(h2, 0x85ebca6b) >>> 0
  }
  return `${content.length.toString(36)}:${h1.toString(36)}${h2.toString(36)}`
}
