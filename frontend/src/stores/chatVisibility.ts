// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * chatVisibility — tab 恢复（visibilitychange）重连监听器。
 *
 * 从 stores/chat.ts 模块底部抽出：回到前台时对有 live buffer 的流做
 * abort→resume，setup 阶段的流只刷新不动连接。注册为模块副作用；
 * 由 main.ts 导入。
 */
import { useChatStore } from './chat'
import { chatApi } from '@/api/chat'

let _visibilityChangeHandler: (() => void) | null = null

if (typeof window !== 'undefined' && !_visibilityChangeHandler) {
  _visibilityChangeHandler = () => {
    if (document.visibilityState !== 'visible') return
    const store = useChatStore()

    async function handleReconnect(convId: string) {
      const s = store.streamStates[convId]
      if (!s) return
      if (store.isResuming(convId)) return

      // Setup-phase guard (conv b078987b, 2026-08-03): a stream still in the
      // backend's SETUP phase has NO stream buffer yet (the buffer is only
      // created once the agent task starts) and NO detached agent task.
      // Aborting the SSE during setup makes the backend release the
      // dead-setup slot and kills the run permanently — the user message is
      // saved but no answer is ever produced, and resume has nothing to
      // replay. Only abort+resume when the backend has a LIVE, resumable
      // buffer (`has_buffer && incomplete && is_running`); otherwise keep
      // the connection (the backend pings every 10s during setup, so the
      // watchdog won't kill it) and just refresh state.
      if (s.abortController) {
        let liveBuffer = false
        try {
          const status = await chatApi.getStreamStatus(convId)
          liveBuffer = !status.error && status.has_buffer && status.status === 'incomplete' && status.is_running
        } catch {
          liveBuffer = false
        }
        if (!liveBuffer) {
          // Setup phase (no buffer yet) or already finished: aborting would
          // kill the run irrecoverably (or is pointless). Keep the in-flight
          // connection; touch the watchdog timestamp so a healthy setup
          // isn't mis-killed either.
          s._lastEventTime = Date.now()
          await store.refreshConversation(convId)
          return
        }
        // 如果当前还有活跃连接，先优雅 abort，让 streamChat catch 块走 tabSwitch 分支
        s.tabSwitchAbort = true
        try { s.abortController.abort() } catch {}
        // 给 abort 传播留出时间，然后再 resume
        await new Promise<void>(r => setTimeout(() => r(), 150))
      }

      // 如果经过 abort 后 streaming 已经停了，说明 catch 块已经走完；
      // 但仍然要尝试 resume，因为后端 buffer 可能还在跑
      if (!s.streaming) {
        // 先查一下后端状态，确认是否真有 buffer
        try {
          const status = await chatApi.getStreamStatus(convId)
          if (status.has_buffer && status.status === 'incomplete' && status.is_running) {
            // 只恢复 streaming 标志，不清空已有 state（避免清掉 content / displaySequence）
            s.streaming = true
            s.tabSwitchAbort = false
            store.currentError = null
            const resumed = await store.resumeActiveStream(convId)
            if (!resumed && s.streaming && !s.abortController) {
              void store.tryReconnectOrSync(convId, (store.messages[convId] || []).length + 1, s)
            }
            return
          }
        } catch {
          // 状态查询失败也要走下面的刷新兜底
        }
        // 兜底：agent 在后台完成但 buffer 已过期（或状态查询失败）时，
        // 也必须从 DB 刷新一次，否则切回 tab 只能看到空白/旧内容。
        await store.refreshConversation(convId)
        return
      }

      // streaming 仍为 true，说明 abort 还没走完 catch，直接 resume；
      // resume 失败时进入有界恢复（重试 resume → syncAfterAbort 轮询），
      // 不能停在 streaming=true 无人驱动的状态。
      const resumed = await store.resumeActiveStream(convId)
      if (!resumed && s.streaming && !s.abortController) {
        void store.tryReconnectOrSync(convId, (store.messages[convId] || []).length + 1, s)
      }
    }

    for (const convId of Object.keys(store.streamStates)) {
      handleReconnect(convId).catch(() => {})
    }
  }
  document.addEventListener('visibilitychange', _visibilityChangeHandler)
}

export function removeChatStoreVisibilityListener() {
  if (_visibilityChangeHandler) {
    document.removeEventListener('visibilitychange', _visibilityChangeHandler)
    _visibilityChangeHandler = null
  }
}
