<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
import { ref, computed } from 'vue'
import StreamMarkdown from './StreamMarkdown.vue'
import type { DisplaySequenceItem } from '@/types'

const props = defineProps<{
  item: DisplaySequenceItem
}>()

const expanded = ref(false)

const toolName = computed(() => props.item.title || formatToolName(props.item.name || ''))
const statusIcon = computed(() => {
  switch (props.item.status) {
    case 'running': return '⟳'
    case 'completed': return '✓'
    case 'error': return '✗'
    default: return '○'
  }
})
const statusLabel = computed(() => {
  switch (props.item.status) {
    case 'running': return '执行中…'
    case 'completed': return '完成'
    case 'error': return '失败'
    default: return '等待中'
  }
})
const hasResult = computed(() => !!(props.item.result))
const isPending = computed(() => props.item.status !== 'running' && props.item.status !== 'completed' && props.item.status !== 'error')
// Tool results are often raw JSON strings. Prefer the tool's own human-readable
// "formatted" field (browser/search tools provide one); otherwise pretty-print
// the JSON in a code block instead of dumping a single-line blob.
const displayResult = computed(() => {
  const raw = props.item.result
  if (!raw) return ''
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object') {
      if (typeof parsed.formatted === 'string' && parsed.formatted.trim()) {
        return parsed.formatted
      }
      return '```json\n' + JSON.stringify(parsed, null, 2) + '\n```'
    }
  } catch {
    // not JSON — render as-is
  }
  return raw
})
const argsSummary = computed(() => {
  const args = props.item.arguments
  if (!args || Object.keys(args).length === 0) return ''
  const firstKey = Object.keys(args)[0]
  const firstVal = String(args[firstKey])
  if (firstVal.length <= 60) return firstVal
  return firstVal.substring(0, 60) + '…'
})

function toggle() {
  expanded.value = !expanded.value
}

function formatToolName(name: string): string {
  const map: Record<string, string> = {
    web_search: '联网检索',
    browser: '浏览网页',
    execute_code: '执行代码',
    terminal: '终端命令',
    memory: '记忆操作',
    notes: '笔记操作',
    delegate_task: '子任务',
    pdf_export: 'PDF导出',
    workspace_read: '读取文件',
    workspace_glob: '文件匹配',
    grep: '内容搜索',
    session_search: '会话检索',
    provide_file: '提供文件',
    context7_resolve_library_id: '查找文档库',
    context7_query_docs: '查询文档',
    word_count: '字数统计',
    diff: '差异对比',
    skill_view: '查看技能',
    skill_run_script: '执行技能脚本',
    background_task: '后台任务',
    schedule: '定时任务',
    browser_interact: '浏览器交互',
  }
  return map[name] || name
}
</script>

<template>
  <div
    class="tool-part-card"
    :class="{
      'tool-pending': isPending,
      'tool-running': item.status === 'running',
      'tool-completed': item.status === 'completed',
      'tool-error': item.status === 'error',
    }"
  >
    <div class="tool-card-header" @click="hasResult && toggle()" :class="{ clickable: hasResult }">
      <span class="tool-card-icon" :class="{ shimmer: item.status === 'running' }">
        {{ statusIcon }}
      </span>
      <span class="tool-card-name">{{ toolName }}</span>
      <span v-if="argsSummary" class="tool-card-args">{{ argsSummary }}</span>
      <span class="tool-card-status" :class="item.status">
        {{ statusLabel }}
      </span>
      <button
        v-if="hasResult"
        class="tool-card-toggle"
        @click.stop="toggle"
      >
        {{ expanded ? '收起结果' : '展开结果' }}
      </button>
    </div>
    <div v-if="expanded && item.result" class="tool-card-result">
      <StreamMarkdown :content="displayResult" />
    </div>
  </div>
</template>

<style scoped>
.tool-part-card {
  margin: 8px 0;
  border-radius: 10px;
  border: 1px solid var(--color-border, #e5e7eb);
  background: var(--color-bg-secondary, #f9fafb);
  overflow: hidden;
  transition: border-color 0.2s, box-shadow 0.2s;
}

.tool-part-card.tool-error {
  border-color: #fca5a5;
  background: #fef2f2;
}

.tool-part-card.tool-running {
  border-color: #93c5fd;
}

.tool-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  font-size: 13px;
  user-select: none;
}

.tool-card-header.clickable {
  cursor: pointer;
}

.tool-card-header.clickable:hover {
  background: rgba(0, 0, 0, 0.03);
}

.tool-card-icon {
  flex-shrink: 0;
  width: 18px;
  text-align: center;
  font-size: 14px;
  color: var(--color-secondary, #6b7280);
}

.tool-card-icon.shimmer {
  animation: tool-shimmer 1.2s ease-in-out infinite;
}

@keyframes tool-shimmer {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

.tool-completed .tool-card-icon {
  color: #16a34a;
}

.tool-error .tool-card-icon {
  color: #dc2626;
}

.tool-card-name {
  font-weight: 600;
  color: var(--color-text-primary, #1f2937);
  flex-shrink: 0;
}

.tool-card-args {
  color: var(--color-secondary, #6b7280);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.tool-card-status {
  flex-shrink: 0;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--color-bg-secondary, #f3f4f6);
}

.tool-card-status.running {
  color: #2563eb;
  background: #dbeafe;
}

.tool-card-status.completed {
  color: #16a34a;
  background: #dcfce7;
}

.tool-card-status.error {
  color: #dc2626;
  background: #fef2f2;
}

.tool-card-toggle {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--color-secondary, #6b7280);
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 6px;
}

.tool-card-toggle:hover {
  background: rgba(0, 0, 0, 0.06);
}

.tool-card-result {
  padding: 10px 14px;
  border-top: 1px solid var(--color-border, #e5e7eb);
  font-size: 13px;
  max-height: 400px;
  overflow-y: auto;
}

.tool-pending .tool-card-header {
  opacity: 0.6;
}
</style>
