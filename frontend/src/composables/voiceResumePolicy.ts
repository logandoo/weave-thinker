// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * voiceResumePolicy — 语音全双工会话「回前台修复」决策表（纯函数内核）。
 *
 * 事故（2026-11-12 用户报告，手机 App）：语音会话中切后台/待机后，播放被打断、
 * 收不到用户语音，回前台也不恢复。Android 在后台会：挂起两个 AudioContext、
 * 结束（end）mic 音轨、静默回收 WebSocket；后台冻结的 setTimeout 重连阶梯可能
 * 一次都没跑过。回到前台必须确定性修复：resume 挂起的 ctx、mic 轨已死则重启
 * 采集、socket 已死且无重连在途则立刻重连（并重置退避——后台的失败尝试不该
 * 吃掉前台的新机会）。终态判定（用户主动关闭 / 鉴权失败 / 重试耗尽）不得复活。
 *
 * 语义约定：
 * - socketReady 用 WebSocket.readyState 数值（0 CONNECTING 1 OPEN 2 CLOSING 3 CLOSED），
 *   null = 连接对象已不存在；0/1 视为「连接仍归事件循环管」，不动。
 * - terminal = 鉴权失败(1008)/后端 error 终态；userClosed = 用户主动挂断。
 * - micOn=false（用户关麦）时音轨死亡不是重启理由。
 */

export type VoiceResumeCtxState = 'suspended' | 'running' | 'closed' | null

export interface VoiceResumeInput {
  userClosed: boolean
  state: 'idle' | 'connecting' | 'listen' | 'thinking' | 'speaking' | 'error'
  socketReady: number | null
  reconnectPending: boolean
  terminal: boolean
  captureCtxState: VoiceResumeCtxState
  playCtxState: VoiceResumeCtxState
  micOn: boolean
  micTrackLive: boolean
}

export interface VoiceResumePlan {
  resumeCaptureCtx: boolean
  resumePlayCtx: boolean
  restartCapture: boolean
  reconnectNow: boolean
}

export function planVoiceResume(i: VoiceResumeInput): VoiceResumePlan {
  // 终态（鉴权失败/重连耗尽/后端 error 关闭）与用户挂断同级：任何通道都不得复活，
  // 包括重启采集——给死会话重新 getUserMedia = 麦克风指示灯常亮却无人收听。
  const dead = i.userClosed || i.state === 'idle' || i.terminal
  return {
    resumeCaptureCtx: !dead && i.captureCtxState === 'suspended',
    resumePlayCtx: !dead && i.playCtxState === 'suspended',
    // mic 轨被系统结束或采集 ctx 被关 → 只能重新 getUserMedia；用户主动关麦除外
    restartCapture: !dead && i.micOn && (i.micTrackLive === false || i.captureCtxState === 'closed'),
    // socket 已死（CLOSED/CLOSING/不存在）且无重连在途、非终态 → 立即重连
    reconnectNow: !dead && !i.reconnectPending
      && (i.socketReady === null || i.socketReady >= 2),
  }
}
