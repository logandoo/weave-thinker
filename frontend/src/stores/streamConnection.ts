// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * streamConnection — 会话级 SSE 单连接不变量的纯函数内核。
 *
 * 事故（conv 3a216a51, 2026-09-03 用户报告）：思考模式长轮中 visibilitychange
 * （手机锁屏/切 App 回来）触发 abort→resume 接管；resumeActiveStream 直接
 * *替换* s.abortController 而从不停掉前任连接——在 abort 传播慢于接管
 * （主线程被思考流 ~90 事件/秒拥塞的移动 WebView）时，旧连接 W1 未死、
 * 新连接 R1 已投递，同一 delta 相邻精确双写 → 思考面板整段「叠字」。
 *
 * 不变量：任何时刻每会话至多一条活 SSE 连接。凡是安装新 controller 的
 * 接管点（beginStreaming / resumeActiveStream / 后续新增者）必须先
 * abort 前任——与 beginStreaming 既有模式一致，抽为纯函数便于 Node
 * 直测（type stripping，无依赖）。
 */

export interface Abortable {
  abort(): void
}

export interface ConnectionHolder {
  abortController?: Abortable | null
}

/** Abort the predecessor controller reference (if any), suppressing abort()
 *  errors (best-effort — mirrors the inline try/catch used at every existing
 *  takeover site). Returns true when a non-null predecessor reference existed.
 *  Note: re-aborting an already-aborted one-shot signal is a no-op — the
 *  runtime backstop for late deliveries of a superseded connection is the
 *  connection-token guard in wireStreamCallbacks (stores/chat.ts). */
export function abortPredecessor(s: ConnectionHolder): boolean {
  if (s.abortController) {
    try {
      s.abortController.abort()
    } catch {
      /* best-effort */
    }
    return true
  }
  return false
}

/** 后端 GET /api/chat/stream/status/{id} 的响应形状（仅判定所需字段）。 */
export interface StreamStatusLike {
  error?: boolean
  has_buffer?: boolean
  status?: string
  is_running?: boolean
  setup_in_progress?: boolean
}

/** P1（2026-09-05 手机切后台断直播修复）：resume 收到 `status:'none'` 回放
 *  时，用 stream/status 二判该会话是否其实仍在跑。
 *
 *  背景：resume 端点把 SETUP 期（provisional 注册、buffer 尚未创建）的 run
 *  视为不存在而回放 'none'。若前端把 'none' 一律当终态（endStreaming +
 *  返回"已处理"），setup 期的恢复就会误杀 streaming 状态——界面只剩最后
 *  一条 query、无任何直播，而生成在后台继续直到自存（用户只能重新进入
 *  会话才看到答案）。返回 true = 暂态，调用方必须继续重试/轮询。
 *
 *  查询失败（error）一律按终态 false 处理：恢复环路的轮询兜底
 *  （syncAfterAbort）有自己的 error 容忍，不能因一次抖动把流挂死。 */
export function isNoneReplayTransient(status: StreamStatusLike | null | undefined): boolean {
  if (!status || status.error) return false
  if (status.setup_in_progress) return true
  return !!(status.has_buffer && status.status === 'incomplete' && status.is_running)
}
