// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * resumeRefreshPolicy — 回前台（visibilitychange→visible）刷新决策的纯函数内核。
 *
 * 事故（2026-11-12 用户报告，手机 App）：手机端切后台/待机期间，网页端或其他
 * 客户端对同一会话发送 query，回到前台后 App 仍停在旧内容，必须重启才刷新。
 * 根因：chatVisibility 的恢复循环只遍历 `store.streamStates`——本机没发起过流的
 * 会话根本没有条目，回前台等于零动作。
 *
 * 不变量：回到前台时侧栏会话列表始终重载（他端新建会话也要可见）；当前会话
 * 只有在**本机没有活跃流**时才整表刷新——活跃流的消息归 stream reducer 所有，
 * 整表刷新会与增量投递竞态。refreshConversation 自带 stale-response 守卫
 * （last-request-wins），与既有调用点共用，不引入第二条数据路径。
 */

export interface ResumeRefreshInput {
  /** 当前打开的会话 id；null = 不在会话页（设置等）。 */
  currentConversationId: string | null
  /** 本机对该会话是否有活跃流（streaming 旗标，含 setup 期）。 */
  currentStreaming: boolean
}

export interface ResumeRefreshPlan {
  reloadConversationList: boolean
  refreshConversationId: string | null
}

export function planResumeRefresh(input: ResumeRefreshInput): ResumeRefreshPlan {
  return {
    reloadConversationList: true,
    refreshConversationId:
      input.currentConversationId && !input.currentStreaming
        ? input.currentConversationId
        : null,
  }
}
