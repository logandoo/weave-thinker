<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div v-if="tasks.length > 0 || panelExpanded || unreadCount > 0" class="background-task-panel" :class="{ expanded: panelExpanded }">
    <div class="btp-header" @click="togglePanel">
      <div class="btp-header-left">
        <span class="btp-icon">&#9200;</span>
        <span class="btp-title">后台任务</span>
        <span v-if="runningCount > 0" class="btp-badge running">
          {{ runningCount }} 运行中
          <template v-if="runningTask">
            (<span class="btp-inline-progress">{{ Math.round(runningTask.progress * 100) }}%</span>)
          </template>
        </span>
        <span v-if="pendingCount > 0" class="btp-badge pending">{{ pendingCount }} 等待中</span>
        <span v-if="completedCount > 0 && runningCount === 0" class="btp-badge completed">
          {{ completedCount }} 已完成
        </span>
        <span v-if="unreadCount > 0" class="btp-badge pending">{{ unreadCount }} 未读</span>
        <span v-if="justCompleted" class="btp-badge just-completed">&#10003; 新完成</span>
      </div>
      <span class="btp-toggle">{{ panelExpanded ? '▲' : '▼' }}</span>
    </div>

    <div v-if="panelExpanded" class="btp-body">
      <div class="btp-tabs">
        <button class="btp-tab" :class="{ active: !showHistory }" @click.stop="setHistory(false)">
          最近
        </button>
        <button class="btp-tab" :class="{ active: showHistory }" @click.stop="setHistory(true)">
          全部任务 / 未读
        </button>
      </div>
      <div v-if="showHistory && notifications.length > 0" class="btp-notifications">
        <div
          v-for="note in notifications"
          :key="note.event_id"
          class="btp-notification"
          @click.stop="openNotification(note)"
        >
          <span class="btp-notification-title">
            {{ note.title || note.logical_task_id || '委派任务' }}
          </span>
          <span class="btp-notification-status">{{ statusLabel(note.status) }}</span>
          <button class="btp-dismiss-btn" @click.stop="dismissNotification(note.event_id)">忽略</button>
        </div>
      </div>
      <div v-if="tasks.length === 0" class="btp-empty">暂无后台任务</div>
      <div
        v-for="task in tasks"
        :key="task.id"
        class="btp-task"
        :class="[task.status, { 'just-completed-flash': task.id === justCompletedId }]"
        @click="navigateToTask(task)"
      >
        <div class="btp-task-header">
          <span class="btp-task-title">{{ task.title || task.goal?.slice(0, 40) || '未命名任务' }}</span>
          <span class="btp-status-badge" :class="task.status">
            {{ statusLabel(task.status) }}
          </span>
        </div>

        <div v-if="task.status === 'running'" class="btp-progress">
          <div class="btp-progress-bar">
            <div class="btp-progress-fill" :style="{ width: Math.max(task.progress * 100, 2) + '%' }"></div>
          </div>
          <span class="btp-progress-text">
            第 {{ task.iterations_done }}/{{ task.iterations_max }} 轮
            <template v-if="task.elapsed_seconds && task.elapsed_seconds > 0"> · {{ formatElapsed(task.elapsed_seconds) }}</template>
          </span>
        </div>

        <div v-if="task.status === 'completed'" class="btp-completed-section">
          <span v-if="task.output_conversation_id" class="btp-view-link">&#x1F4AC; 点击查看对话结果</span>
          <span v-if="task.elapsed_seconds && task.elapsed_seconds > 0" class="btp-time">耗时 {{ formatElapsed(task.elapsed_seconds) }}</span>
          <span v-if="task.result" class="btp-result-preview">{{ task.result.slice(0, 120) }}...</span>
        </div>

        <div v-if="task.status === 'failed'" class="btp-error">
          {{ task.error?.slice(0, 120) || '未知错误' }}
        </div>

        <div v-if="taskSummaries[task.id]" class="btp-summary">
          {{ taskSummaries[task.id].summary }}
          <button
            v-if="taskSummaries[task.id].has_more"
            class="btp-dismiss-btn"
            @click.stop="continueSummary(task)"
          >
            继续
          </button>
        </div>

        <div class="btp-actions">
          <button
            v-if="task.status === 'waiting_auth' && task.waiting_reason?.tool_name && task.waiting_reason?.action_fingerprint"
            class="btp-view-btn"
            @click.stop="grantPermit(task)"
          >
            授权继续
          </button>
          <button
            v-if="['pending', 'claimed', 'running', 'resumable', 'waiting_input', 'waiting_auth', 'waiting_budget'].includes(task.status)"
            class="btp-cancel-btn"
            @click.stop="cancelTask(task.id)"
          >
            取消
          </button>
          <button
            v-if="['completed', 'timed_out'].includes(task.status)"
            class="btp-dismiss-btn"
            @click.stop="summarizeTask(task)"
          >
            摘要
          </button>
          <button
            v-if="task.execution_conversation_id || task.output_conversation_id"
            class="btp-view-btn"
            @click.stop="navigateToTask(task)"
          >
            {{ task.status === 'completed' ? '查看结果' : '打开会话' }}
          </button>
          <button
            v-if="['completed', 'failed', 'cancelled', 'timed_out'].includes(task.status)"
            class="btp-dismiss-btn"
            @click.stop="deleteTask(task.id)"
          >
            清除
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { agentTaskApi } from '@/api/agentTasks'
import api from '@/api/client'
import { openTaskResult } from '@/utils/openTaskResult'
import type { AgentTaskInfo, AgentTaskNotification } from '@/types'

const tasks = ref<AgentTaskInfo[]>([])
const panelExpanded = ref(false)
const justCompletedId = ref<string | null>(null)
const showHistory = ref(false)
const notifications = ref<AgentTaskNotification[]>([])
const taskSummaries = ref<Record<string, { summary: string; has_more: boolean; part: number }>>({})
const unreadCount = computed(() => notifications.value.length)
const _pollTimer: ReturnType<typeof setInterval> | null = null
const _previousCompletedIds = new Set<string>()

const runningCount = computed(() => tasks.value.filter(t => t.status === 'running').length)
const pendingCount = computed(() => tasks.value.filter(t => t.status === 'pending').length)
const completedCount = computed(() => tasks.value.filter(t => t.status === 'completed').length)
const runningTask = computed(() => tasks.value.find(t => t.status === 'running') || null)
const justCompleted = computed(() => justCompletedId.value !== null)

watch(runningCount, (newVal, oldVal) => {
  if (newVal > 0 && oldVal === 0) {
    panelExpanded.value = true
  }
})

function togglePanel() {
  panelExpanded.value = !panelExpanded.value
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    pending: '等待中',
    claimed: '已领取',
    running: '运行中',
    resumable: '待恢复',
    waiting_input: '待补充信息',
    waiting_auth: '待授权',
    waiting_budget: '待确认预算',
    cancelling: '取消中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    timed_out: '已超时',
  }
  return map[status] || status
}

function formatElapsed(seconds: number): string {
  if (seconds <= 0) return '0秒'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  const h = Math.floor(m / 60)
  if (h > 0) return `${h}时${m % 60}分${s}秒`
  if (m > 0) return `${m}分${s}秒`
  return `${s}秒`
}

async function navigateToTask(task: AgentTaskInfo) {
  if (task.execution_conversation_id || task.output_conversation_id) {
    await openTaskResult(task)
  }
}

async function openNotification(note: AgentTaskNotification) {
  if (!note.logical_task_id) return
  const opened = await openTaskResult(note.logical_task_id)
  if (opened) {
    await dismissNotification(note.event_id)
  }
}

async function dismissNotification(eventId: string) {
  try {
    await agentTaskApi.dismissNotification(eventId)
    notifications.value = notifications.value.filter(n => n.event_id !== eventId)
  } catch (e) {
    console.error('Failed to dismiss notification:', e)
  }
}

async function grantPermit(task: AgentTaskInfo) {
  const reason = (task.waiting_reason || {}) as Record<string, unknown>
  const toolName = String(reason.tool_name || '')
  const fingerprint = String(reason.action_fingerprint || '')
  if (!toolName || !fingerprint) return
  try {
    await agentTaskApi.grantPermit(task.id, toolName, fingerprint)
    await refreshTasks()
  } catch (e) {
    console.error('Failed to grant task permit:', e)
  }
}

async function summarizeTask(task: AgentTaskInfo, part = 0) {
  try {
    const result = await agentTaskApi.summarizeTask(task.id, part)
    taskSummaries.value = {
      ...taskSummaries.value,
      [task.id]: {
        summary: result.summary || result.message || '暂无可转述的结果',
        has_more: !!result.has_more,
        part: result.part ?? part,
      },
    }
  } catch (e) {
    taskSummaries.value = {
      ...taskSummaries.value,
      [task.id]: { summary: '摘要生成失败；可打开结果会话查看权威内容', has_more: false, part },
    }
  }
}

function continueSummary(task: AgentTaskInfo) {
  const current = taskSummaries.value[task.id]
  if (!current) return
  void summarizeTask(task, current.part + 1)
}

function setHistory(value: boolean) {
  showHistory.value = value
  void refreshTasks()
}

async function cancelTask(taskId: string) {
  try {
    await agentTaskApi.cancelTask(taskId)
    await refreshTasks()
  } catch (e) {
    console.error('Failed to cancel task:', e)
  }
}

async function deleteTask(taskId: string) {
  try {
    await agentTaskApi.deleteTask(taskId)
    await refreshTasks()
  } catch (e) {
    console.error('Failed to delete task:', e)
  }
}

async function refreshTasks() {
  try {
    const all = await agentTaskApi.listTasks(undefined, true)
    const newCompletedIds = new Set(
      all.filter(t => t.status === 'completed').map(t => t.id)
    )

    for (const id of newCompletedIds) {
      if (!_previousCompletedIds.has(id)) {
        justCompletedId.value = id
        setTimeout(() => { justCompletedId.value = null }, 5000)
        // F1/H1（2026-09-14）：后台任务完成事件（零后端）——唤醒父级聊天界面
        // 刷新对应会话（ChatArea 监听 background-task-completed）。
        const finished = all.find(t => t.id === id)
        const convId = finished?.output_conversation_id || finished?.conversation_id
        if (convId) {
          window.dispatchEvent(new CustomEvent('background-task-completed', {
            detail: { task_id: id, conversation_id: convId }
          }))
        }
        break
      }
    }
    _previousCompletedIds.clear()
    for (const id of newCompletedIds) _previousCompletedIds.add(id)

    const visible = all.filter(t => t.task_type !== 'grilling')
    tasks.value = showHistory.value
      ? visible.slice(0, 50)
      : visible.filter(t => !['completed', 'failed', 'cancelled', 'timed_out'].includes(t.status)
          || isRecentCompleted(t))

    try {
      notifications.value = await agentTaskApi.listNotifications(false)
    } catch (e) {
      // notifications are an additive surface; polling keeps tasks usable
    }
    refreshScheduledTasks()
  } catch (e) {
  }
}

// Track previous run counts to detect new scheduled task executions
const _prevSchedRuns: Record<string, number> = {}

async function refreshScheduledTasks() {
  try {
    const { data } = await api.get('/scheduled-tasks', { params: { include_completed: true } })
    if (!Array.isArray(data)) return
    for (const st of data) {
      if (st.conversation_id && st.run_count > 0) {
        const prev = _prevSchedRuns[st.id] || 0
        if (st.run_count > prev) {
          _prevSchedRuns[st.id] = st.run_count
          window.dispatchEvent(new CustomEvent('scheduled-task-result', {
            detail: { conversation_id: st.conversation_id }
          }))
        }
      }
    }
  } catch (e) {
  }
}

function isRecentCompleted(task: AgentTaskInfo): boolean {
  if (!task.completed_at) return false
  const completed = new Date(task.completed_at).getTime()
  const now = Date.now()
  return now - completed < 30 * 60 * 1000
}

let _pollTimerRef: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  refreshTasks()
  _pollTimerRef = setInterval(refreshTasks, 3000)
})

onUnmounted(() => {
  if (_pollTimerRef) {
    clearInterval(_pollTimerRef)
    _pollTimerRef = null
  }
})
</script>

<style scoped>
.background-task-panel {
  border-bottom: 1px solid var(--color-border, #e0e0e0);
  background: var(--color-bg-secondary, #f8f8f8);
  font-size: 13px;
}

.btp-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  cursor: pointer;
  user-select: none;
}

.btp-header:hover {
  background: var(--color-hover, #f0f0f0);
}

.btp-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.btp-icon {
  font-size: 16px;
}

.btp-title {
  font-weight: 600;
  color: var(--color-text-primary, #333);
}

.btp-badge {
  padding: 1px 6px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}

.btp-badge.running {
  background: var(--info-tint);
  color: var(--color-info);
}

.btp-badge.pending {
  background: var(--color-hover);
  color: var(--color-text-light);
}

.btp-badge.completed {
  background: var(--success-tint);
  color: var(--color-success);
}

.btp-badge.just-completed {
  background: var(--success-tint-strong);
  color: var(--color-success);
  animation: btp-pulse 0.6s ease-in-out 3;
}

@keyframes btp-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.btp-inline-progress {
  font-weight: 600;
  color: var(--color-info);
}

.btp-toggle {
  color: var(--color-text-light);
  font-size: 11px;
}

.btp-body {
  padding: 0 16px 12px;
}

.btp-empty {
  padding: 12px 0;
  color: var(--color-text-light);
  text-align: center;
  font-size: 13px;
}

.btp-task {
  padding: 8px 10px;
  margin-bottom: 6px;
  border-radius: 6px;
  border: 1px solid var(--color-border, #e0e0e0);
  background: var(--surface-panel-strong);
  cursor: pointer;
  transition: border-color 0.2s, background-color 0.3s;
}

.btp-task:hover {
  border-color: var(--color-primary, #1976d2);
}

.btp-task.running {
  border-left: 3px solid var(--color-info);
}

.btp-task.completed {
  border-left: 3px solid var(--color-success);
}

.btp-task.completed:hover {
  background: var(--success-tint);
}

.btp-task.failed {
  border-left: 3px solid var(--color-danger);
}

.btp-task.just-completed-flash {
  animation: btp-flash 0.5s ease-in-out 2;
}

@keyframes btp-flash {
  0%, 100% { background: var(--surface-panel-strong); }
  50% { background: var(--success-tint); }
}

.btp-task-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.btp-task-title {
  font-weight: 500;
  color: var(--color-text-primary, #333);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.btp-status-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  font-weight: 500;
  flex-shrink: 0;
  margin-left: 8px;
}

.btp-status-badge.pending {
  background: var(--color-hover);
  color: var(--color-text-light);
}

.btp-status-badge.running {
  background: var(--info-tint);
  color: var(--color-info);
}

.btp-status-badge.completed {
  background: var(--success-tint);
  color: var(--color-success);
}

.btp-status-badge.failed {
  background: var(--danger-tint);
  color: var(--color-danger);
}

.btp-status-badge.cancelled {
  background: var(--color-hover);
  color: var(--color-text-light);
}

.btp-progress {
  margin: 6px 0 4px;
}

.btp-progress-bar {
  height: 4px;
  background: var(--color-border);
  border-radius: 2px;
  overflow: hidden;
}

.btp-progress-fill {
  height: 100%;
  background: var(--color-info);
  border-radius: 2px;
  transition: width 1s ease;
}

.btp-progress-text {
  font-size: 11px;
  color: var(--color-text-light);
  margin-top: 2px;
}

.btp-completed-section {
  margin-top: 6px;
}

.btp-view-link {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-primary, #1976d2);
  margin-bottom: 2px;
}

.btp-time {
  font-size: 11px;
  color: var(--color-text-light);
}

.btp-result-preview {
  display: block;
  font-size: 11px;
  color: var(--color-text-light);
  margin-top: 4px;
  line-height: 1.3;
}

.btp-error {
  font-size: 11px;
  color: var(--color-danger);
  margin-top: 4px;
}

.btp-actions {
  display: flex;
  gap: 4px;
  margin-top: 6px;
}

.btp-cancel-btn,
.btp-dismiss-btn,
.btp-view-btn {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  background: var(--surface-panel-strong);
  color: var(--color-text-light);
  cursor: pointer;
}

.btp-view-btn {
  border-color: var(--color-primary, #1976d2);
  color: var(--color-primary, #1976d2);
}

.btp-view-btn:hover {
  background: var(--info-tint);
}

.btp-cancel-btn:hover {
  background: var(--danger-tint);
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.btp-dismiss-btn:hover {
  background: var(--color-hover);
  border-color: var(--color-text-light);
}

.btp-tabs {
  display: flex;
  gap: 6px;
  padding: 4px 10px 6px;
  border-bottom: 1px solid var(--color-border);
}
.btp-tab {
  border: 1px solid transparent;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 12px;
  padding: 3px 8px;
  border-radius: var(--radius-pill);
  cursor: pointer;
}
.btp-tab.active {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-hover);
}
.btp-notifications {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-border);
}
.btp-notification {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-md);
  background: var(--color-bg-secondary);
  cursor: pointer;
  font-size: 12px;
}
.btp-notification:hover {
  background: var(--color-hover);
}
.btp-notification-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.btp-notification-status {
  color: var(--color-text-secondary);
  flex: none;
}
.btp-summary {
  margin: 4px 0 0;
  padding: 6px 8px;
  border-radius: var(--radius-md);
  background: var(--color-bg-secondary);
  color: var(--color-text-primary);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
}
</style>
