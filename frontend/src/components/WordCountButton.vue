<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 字数统计按钮（R3，2026-09-13 审计 F-B）：NoteEditor 与 ZenNotePanel 的
 * 单一实现——按钮 + Teleport 弹层 + 定位 + 关闭监听 + 计数渲染全部收口。
 * 父组件只提供取文函数（点击时才读 DOM，避免每次输入触发 innerText）。
 *
 * 单根 = <button>（父 scoped 的 .toolbar-btn 样式经根节点 scopeId 继承）；
 * 弹层经 Teleport 到 body，使用本组件 scoped 样式（与迁移前一致）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { countNoteText, type NoteWordCount } from '@/utils/noteWordCount'

const props = defineProps<{ getText: () => string }>()

const btnRef = ref<HTMLButtonElement | null>(null)
const show = ref(false)
const stats = ref<NoteWordCount>(countNoteText(''))

const popoverStyle = computed(() => {
  const btn = btnRef.value
  if (!btn) return {}
  const rect = btn.getBoundingClientRect()
  return {
    position: 'fixed' as const,
    top: `${rect.bottom + 4}px`,
    right: `${Math.max(8, window.innerWidth - rect.right)}px`,
    zIndex: 9999,
  }
})

function close() {
  if (show.value) show.value = false
}

function toggle() {
  if (show.value) {
    show.value = false
    return
  }
  // 打开时直接取数（refresh() 有 open 守卫，不能用于首次计算）
  stats.value = countNoteText(props.getText ? props.getText() : '')
  show.value = true
}

/** 弹层打开时随内容变化实时刷新（父组件在内容变更钩子中调用）。 */
function refresh() {
  if (!show.value) return
  stats.value = countNoteText(props.getText ? props.getText() : '')
}

defineExpose({ refresh })

function onDocumentClick(e: MouseEvent) {
  if (!show.value) return
  const target = e.target as HTMLElement | null
  if (target && (target.closest('.word-count-teleport') || target.closest('.word-count-btn'))) return
  close()
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  window.addEventListener('resize', close)
  window.addEventListener('scroll', close, true)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  window.removeEventListener('resize', close)
  window.removeEventListener('scroll', close, true)
})
</script>

<template>
  <button
    ref="btnRef"
    class="toolbar-btn word-count-btn"
    :class="{ active: show }"
    @mousedown.prevent
    @click="toggle"
    title="字数统计"
  >
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
      <line x1="4" y1="9" x2="20" y2="9"/>
      <line x1="4" y1="15" x2="20" y2="15"/>
      <line x1="10" y1="3" x2="8" y2="21"/>
      <line x1="16" y1="3" x2="14" y2="21"/>
    </svg>
    <Teleport to="body">
      <div v-if="show" class="word-count-teleport" :style="popoverStyle" @click.stop>
        <div class="wc-title">字数统计</div>
        <div class="wc-row"><span>字数</span><b>{{ stats.wordCount }}</b></div>
        <div class="wc-row"><span>字符数</span><b>{{ stats.charCount }}</b></div>
        <div class="wc-row"><span>字符数（不含空格）</span><b>{{ stats.charCountNoSpaces }}</b></div>
        <div class="wc-row"><span>行数</span><b>{{ stats.lineCount }}</b></div>
        <div class="wc-row"><span>段落数</span><b>{{ stats.paragraphCount }}</b></div>
        <div class="wc-sub">中文 {{ stats.cjkChars }} · 英文 {{ stats.latinWords }}</div>
      </div>
    </Teleport>
  </button>
</template>

<style scoped>
.word-count-btn {
  position: sticky;
  right: 0;
  z-index: 2;
  background-color: var(--color-white);
  box-shadow: -8px 0 8px -8px color-mix(in srgb, var(--color-text) 22%, transparent);
}

.word-count-teleport {
  min-width: 190px;
  padding: 10px 12px;
  background-color: var(--color-white);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-md);
  color: var(--color-text);
  font-size: 13px;
}

.wc-title {
  font-weight: 600;
  margin-bottom: 6px;
}

.wc-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 18px;
  line-height: 1.9;
}

.wc-row b {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: var(--color-primary);
}

.wc-sub {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--color-border);
  font-size: 11px;
  color: var(--color-text-light);
}
</style>
