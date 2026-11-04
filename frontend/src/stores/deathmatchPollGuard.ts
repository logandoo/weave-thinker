// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

// B-6（复审残留收口，2026-10-14 遗留清偿波）：poll 在飞 GET 竞态守卫。
//
// 场景：5s 轮询发出 GET 后、响应落地前，一条新 verdict SSE 已应用——旧快照
// 不得覆盖 verdict/reason/message（human_gate/attachments 已另有沿用）。
// 语义：onDeathmatchVerdict 每次应用 verdict 时序号 +1；poll 在发 GET 前取
// 快照序号，响应后经 shouldApplyPollVerdict 判等——不等即弃用旧快照。

export function shouldApplyPollVerdict(seqBefore: number, seqNow: number): boolean {
  return seqBefore === seqNow
}
