<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 会话标题栏的任务作用域徽标（2026-11-10 波 · T14）。
 *
 * `mode=off` 或未绑定（legacy 会话）时完全不渲染 —— 现网零变化。
 * 点击打开绑定面板（历史 + 改绑）。跟随 currentConversationId 拉快照。
 */
import { computed, ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useWorkspaceScopeStore } from '@/stores/workspaceScope'
import { useToast } from '@/composables/useToast'
import ConversationTaskDrawer from './workspace/ConversationTaskDrawer.vue'

const chatStore = useChatStore()
const scope = useWorkspaceScopeStore()
const { show: showToast } = useToast()
const drawerOpen = ref(false)
const pickerOpen = ref(false)

// §6.1「歧义只问一次」：后端 workspace_task_ambiguous → 本会话候选选择器
const ambiguousHere = computed(
  () => scope.ambiguous?.conversationId === chatStore.currentConversationId ? scope.ambiguous : null,
)

// 2026-11-14 v2：新会话归类建议（meta.workspace_suggestion）——本会话专属
// （服务端 id 或流发起时的客户端 id 任一匹配即可；未 remint 时二者不同）
const suggestionHere = computed(() => {
  const s = scope.suggestion
  const cur = chatStore.currentConversationId
  if (!s || !cur) return null
  return s.conversationId === cur || s.clientId === cur ? s : null
})
const suggestionOpen = ref(false)

// 建议变化（切换会话/接受/拒绝）时收起旧对话框，避免残留弹窗
watch(suggestionHere, () => { suggestionOpen.value = false })

async function acceptSuggestion() {
  const ok = await scope.acceptSuggestion()
  suggestionOpen.value = false
  if (ok) showToast('已归类到该任务，后续消息与文件进入新任务（旧任务文件不动）', 'success')
  else showToast(scope.lastError || '归类失败，请重试', 'error')
}

function dismissSuggestion() {
  scope.dismissSuggestion()
  suggestionOpen.value = false
}

function fmtCandidate(c: { title?: string; reason?: string; last_active_at?: string | null }): string {
  const bits: string[] = []
  if (c.reason) bits.push(c.reason)
  if (c.last_active_at) bits.push(`最近活跃 ${new Date(c.last_active_at).toLocaleString()}`)
  return bits.join(' · ')
}

async function chooseFresh() {
  const ok = await scope.chooseNewTask()
  pickerOpen.value = false
  if (ok) showToast('已开新任务，重新发送即可继续', 'success')
  else showToast(scope.lastError || '开新任务失败，请重试', 'error')
}

async function choose(taskId: string, title: string) {
  const ok = await scope.chooseTask(taskId)
  pickerOpen.value = false
  if (ok) showToast(`已绑定到「${title}」，请重新发送刚才的消息`, 'success')
  else showToast(scope.lastError || '绑定失败，请重试', 'error')
}

watch(
  () => chatStore.currentConversationId,
  (id) => {
    void scope.loadBinding(id)
  },
  { immediate: true },
)
</script>

<template>
  <button
    v-if="ambiguousHere"
    class="workspace-scope-chip is-ambiguous"
    data-testid="workspace-ambiguous-cta"
    :title="ambiguousHere.question"
    @click="pickerOpen = true"
  >
    ⚠ 选择要继续的任务
  </button>
  <Teleport v-if="ambiguousHere && pickerOpen" to="body">
    <div class="wap-mask" data-testid="workspace-ambiguous-dialog" @click.self="pickerOpen = false">
      <div class="wap-panel" role="dialog" aria-label="选择任务">
        <h3>{{ ambiguousHere.question }}</h3>
        <p class="wap-hint">选择一个任务后重新发送即可继续；不选也可直接发新话题。</p>
        <button
          v-for="c in ambiguousHere.candidates"
          :key="c.task_id"
          class="wap-item"
          data-testid="workspace-ambiguous-item"
          @click="choose(c.task_id, c.title || '未命名任务')"
        >
          <span class="wap-item__title">{{ c.title || '未命名任务' }}</span>
          <span class="wap-item__meta">{{ fmtCandidate(c) }}</span>
        </button>
        <button class="wap-dismiss" data-testid="workspace-ambiguous-newtask" :disabled="scope.loading" @click="chooseFresh">不是这些，开新任务</button>
      </div>
    </div>
  </Teleport>
  <button
    v-if="suggestionHere"
    class="workspace-scope-chip is-suggest"
    data-testid="workspace-suggestion-cta"
    :title="`这条对话看起来属于「${suggestionHere.task.title || '未命名任务'}」`"
    @click="suggestionOpen = true"
  >
    📁 归类到「{{ suggestionHere.task.title || '未命名任务' }}」？
  </button>
  <Teleport v-if="suggestionHere && suggestionOpen" to="body">
    <div class="wap-mask" data-testid="workspace-suggestion-dialog" @click.self="suggestionOpen = false">
      <div class="wap-panel" role="dialog" aria-label="归类建议">
        <h3>要归类到「{{ suggestionHere.task.title || '未命名任务' }}」吗？</h3>
        <p class="wap-hint">
          这条对话看起来和该任务相关。归类后，后续消息与文件进入该任务（旧任务文件不动）；
          不归类则保持在「默认项目」。
        </p>
        <p v-if="chatStore.isStreamingCurrentConversation" class="wap-hint" data-testid="workspace-suggestion-wait">
          回答完成后才能归类（本轮仍在运行）。
        </p>
        <button
          class="wap-item"
          data-testid="workspace-suggestion-accept"
          :disabled="scope.loading || chatStore.isStreamingCurrentConversation"
          @click="acceptSuggestion"
        >
          <span class="wap-item__title">归类到「{{ suggestionHere.task.title || '未命名任务' }}」</span>
          <span class="wap-item__meta">{{ suggestionHere.task.reason || '' }}</span>
        </button>
        <button
          class="wap-dismiss"
          data-testid="workspace-suggestion-dismiss"
          :disabled="scope.loading"
          @click="dismissSuggestion"
        >保持默认项目</button>
      </div>
    </div>
  </Teleport>
  <template v-if="scope.chipVisible">
    <button
      class="workspace-scope-chip"
      :class="{ 'is-shadow': scope.snapshot?.mode === 'shadow' }"
      :title="scope.modeHint || '当前任务'"
      data-testid="workspace-scope-chip"
      @click="drawerOpen = true"
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
      <span class="workspace-scope-chip__text">{{ scope.badgeText }}</span>
    </button>
    <ConversationTaskDrawer :visible="drawerOpen" @close="drawerOpen = false" />
  </template>
</template>

<style scoped>
.workspace-scope-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 220px;
  padding: 2px 8px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: var(--surface-panel-strong, var(--color-bg));
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
  cursor: pointer;
}
.workspace-scope-chip:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
}
.workspace-scope-chip.is-shadow {
  border-style: dashed;
}
.workspace-scope-chip.is-ambiguous {
  border-color: var(--color-primary);
  color: var(--color-primary);
}
/* 2026-11-14 v2：归类建议 CTA 与普通徽标区分（主色描边 + 浅底） */
.workspace-scope-chip.is-suggest {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 8%, var(--surface-panel-strong, var(--color-bg)));
}
.wap-mask {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
}
.wap-panel {
  width: min(420px, calc(100vw - 32px));
  max-height: 70vh;
  overflow-y: auto;
  background: var(--surface-panel-strong, var(--color-bg));
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px;
}
.wap-panel h3 {
  margin: 0 0 4px;
  font-size: 15px;
}
.wap-hint {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.wap-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  margin-bottom: 8px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  cursor: pointer;
  text-align: left;
}
.wap-item:hover {
  border-color: var(--color-primary);
}
.wap-item__title {
  font-size: 13px;
  font-weight: 600;
}
.wap-item__meta {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.wap-dismiss {
  width: 100%;
  padding: 6px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.workspace-scope-chip__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
