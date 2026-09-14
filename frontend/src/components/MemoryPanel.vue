<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="memory-panel">
    <div class="memory-header">
      <h3 class="memory-title">记忆管理</h3>
    </div>

    <p class="memory-hint">
      查看和管理助手为你保留的长期记忆。删除后立即生效，不可恢复。
    </p>

    <div class="memory-subtabs">
      <button
        v-for="st in subTabs"
        :key="st.key"
        class="memory-subtab"
        :class="{ active: activeSubTab === st.key }"
        @click="activeSubTab = st.key"
      >{{ st.label }}</button>
    </div>

    <div v-if="errorMsg" class="memory-error">{{ errorMsg }}</div>

    <!-- 概念 -->
    <div v-if="activeSubTab === 'concepts'" class="memory-section">
      <div v-if="loading.concepts" class="memory-loading">加载中…</div>
      <div v-else-if="concepts.length === 0" class="memory-empty">
        暂无概念记忆。新记忆架构未启用或尚未积累概念。
      </div>
      <template v-else>
        <div
          v-for="c in concepts"
          :key="c.id"
          class="memory-item"
          :class="{ inactive: c.status !== 'active' || c.valid_to }"
        >
          <div class="memory-item-main">
            <div class="memory-item-name">
              {{ c.canonical_name }}
              <span v-if="c.source_trust === 'agent_inferred'" class="tag inferred">推断</span>
              <span v-if="c.status === 'silent'" class="tag silent">沉默</span>
              <span v-if="c.status === 'cold_forgotten'" class="tag cold">冷遗忘</span>
              <span v-if="c.valid_to" class="tag expired">已失效</span>
            </div>
            <div class="memory-item-desc">{{ c.description_short }}</div>
            <div class="memory-item-meta">
              重要度 {{ c.importance.toFixed(2) }} · 权重 {{ c.weight.toFixed(2) }} · {{ trustLabel(c.source_trust) }} · {{ typeLabel(c.memory_type) }}
              <template v-if="c.created_at"> · {{ formatDate(c.created_at) }}</template>
            </div>
            <button class="memory-expand-btn" @click="toggleConcept(c.id)">
              {{ expandedConcepts.has(c.id) ? '收起详情' : '详情' }}
            </button>
            <div v-if="expandedConcepts.has(c.id)" class="concept-detail">
              <div v-if="c.description_full" class="memory-item-desc">{{ c.description_full }}</div>
              <div v-if="c.aliases && c.aliases.length" class="memory-item-meta">
                别名：{{ c.aliases.join('、') }}
              </div>
              <div class="memory-item-meta">
                权重 {{ c.weight.toFixed(2) }} · 状态 {{ statusLabel(c.status) }}
                <template v-if="c.recurrence_count != null"> · 复现 {{ c.recurrence_count }} 次</template>
              </div>
            </div>
          </div>
          <div class="memory-item-actions">
            <button
              class="memory-delete-btn forget"
              :disabled="forgettingId === c.id"
              title="遗忘此概念（软遗忘）"
              @click="handleForgetConcept(c)"
            >{{ forgettingId === c.id ? '…' : '遗忘' }}</button>
            <button
              class="memory-delete-btn"
              :disabled="deletingId === c.id"
              title="删除此概念"
              @click="handleDeleteConcept(c)"
            >{{ deletingId === c.id ? '…' : '删除' }}</button>
          </div>
        </div>
        <button
          v-if="!conceptsExhausted"
          class="memory-more-btn"
          :disabled="loading.concepts"
          @click="loadMoreConcepts"
        >{{ loading.concepts ? '加载中…' : '加载更多' }}</button>
      </template>
    </div>

    <!-- Dream -->
    <div v-else-if="activeSubTab === 'dreams'" class="memory-section">
      <div v-if="loading.dreams" class="memory-loading">加载中…</div>
      <div v-else-if="dreams.length === 0" class="memory-empty">暂无 dream 记录</div>
      <div v-else v-for="d in dreams" :key="d.id" class="memory-item">
        <div class="memory-item-main">
          <div class="memory-item-name">
            {{ d.generated_for_date }}
            <span v-if="d.dream_type === 'legacy'" class="tag legacy">旧版</span>
          </div>
          <div
            class="memory-item-desc dream-summary"
            :class="{ expanded: expandedDreams.has(d.id) }"
            :ref="el => el && checkDreamClamp(el, d.id)"
          >{{ d.summary }}</div>
          <div class="memory-item-meta">
            来源概念 {{ d.source_concept_count }} · 集合 {{ d.source_cluster_count }}
          </div>
          <button
            v-if="dreamClamped.has(d.id)"
            class="memory-expand-btn"
            @click="toggleDream(d.id)"
          >{{ expandedDreams.has(d.id) ? '收起' : '展开全文' }}</button>
        </div>
      </div>
    </div>

    <!-- 澄清 -->
    <div v-else-if="activeSubTab === 'clarifications'" class="memory-section">
      <div v-if="loading.clarifications" class="memory-loading">加载中…</div>
      <div v-else-if="clarifications.length === 0" class="memory-empty">暂无澄清记录</div>
      <div v-else v-for="cl in clarifications" :key="cl.id" class="memory-item">
        <div class="memory-item-main">
          <div class="memory-item-name">
            {{ correctionLabel(cl.correction_type) }}
            <span v-if="cl.applied" class="tag applied">已应用</span>
            <span v-else class="tag pending">待复核</span>
          </div>
          <div class="memory-item-desc">"{{ cl.original_text }}"</div>
          <div v-if="cl.new_description" class="memory-item-meta">修正为：{{ cl.new_description }}</div>
          <div class="memory-item-meta">
            置信度 {{ cl.confidence.toFixed(2) }}<template v-if="cl.created_at"> · {{ formatDate(cl.created_at) }}</template>
          </div>
        </div>
        <button
          v-if="cl.applied && cl.correction_type !== 'forget'"
          class="memory-delete-btn revert"
          :disabled="revertingId === cl.id"
          title="撤销此澄清"
          @click="handleRevert(cl)"
        >{{ revertingId === cl.id ? '…' : '撤销' }}</button>
      </div>
    </div>

    <!-- 状态 -->
    <div v-else-if="activeSubTab === 'status'" class="memory-section">
      <div v-if="loading.status" class="memory-loading">加载中…</div>
      <template v-else-if="costStatus">
        <div class="status-card">
          <div class="status-row">
            <span class="status-label">LLM 调用（今日 / 7日日均）</span>
            <span class="status-value">{{ costStatus.today_calls }} / {{ costStatus.daily_avg_7d }}</span>
          </div>
          <div class="status-row">
            <span class="status-label">降级等级</span>
            <span class="status-value" :class="{ warn: costStatus.level > 0 }">
              {{ costStatus.level === 0 ? '正常' : `L${costStatus.level}` }}
            </span>
          </div>
          <div v-if="costStatus.disabled_steps.length" class="status-row">
            <span class="status-label">已降级步骤</span>
            <span class="status-value warn">{{ costStatus.disabled_steps.join(', ') }}</span>
          </div>
          <div v-if="costStatus.reason" class="status-row">
            <span class="status-label">触发原因</span>
            <span class="status-value">{{ costStatus.reason }}</span>
          </div>
          <div v-if="costStatus.last_change" class="status-row">
            <span class="status-label">最近变更</span>
            <span class="status-value">{{ costStatus.last_change }}</span>
          </div>
        </div>
      </template>
      <div v-else class="memory-empty">无法获取计费状态</div>

      <div class="danger-zone">
        <div class="danger-title">危险操作</div>
        <p class="danger-hint">
          全部擦除将删除你的所有长期记忆（概念、事件、集合、dream、文件记忆），对话与笔记保留。此操作不可恢复。
        </p>
        <div v-if="!confirmErase" class="danger-actions">
          <button class="danger-btn" @click="confirmErase = true">全部擦除…</button>
        </div>
        <div v-else class="danger-confirm">
          <p class="danger-confirm-text">请输入 <strong>擦除</strong> 以确认：</p>
          <div class="danger-confirm-row">
            <input v-model="eraseText" type="text" placeholder="擦除" />
            <button
              class="danger-btn"
              :disabled="eraseText !== '擦除' || erasing"
              @click="handleEraseAll"
            >{{ erasing ? '擦除中…' : '确认擦除' }}</button>
            <button class="memory-delete-btn" @click="confirmErase = false; eraseText = ''">取消</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 台账 -->
    <div v-else-if="activeSubTab === 'ledger'" class="memory-section">
      <div v-if="loading.ledger" class="memory-loading">加载中…</div>
      <div v-else-if="recallLog.length === 0" class="memory-empty">暂无召回台账记录</div>
      <template v-else>
        <div v-for="r in recallLog" :key="r.id" class="ledger-item">
          <div class="ledger-row">
            <span class="ledger-time">{{ formatDateTime(r.created_at) }}</span>
            <span class="ledger-hash" :title="r.query_hash || ''">{{ hashShort(r.query_hash) }}</span>
            <span class="ledger-badges">
              <span v-if="r.truncated" class="tag truncated">截断</span>
              <span v-if="r.cache_hit" class="tag cache">缓存</span>
            </span>
          </div>
          <div class="ledger-row ledger-meta">
            <span>门控 {{ r.gate_score.toFixed(3) }}</span>
            <span>注入 {{ r.injected_chars }}/{{ r.budget_chars }}</span>
            <span>耗时 {{ r.elapsed_ms }}ms</span>
          </div>
          <div v-if="tierSummary(r.tier_scores)" class="ledger-tiers">
            {{ tierSummary(r.tier_scores) }}
          </div>
        </div>
        <button
          v-if="recallHasMore"
          class="memory-more-btn"
          :disabled="loadingRecallMore"
          @click="loadMoreRecallLog"
        >{{ loadingRecallMore ? '加载中…' : '加载更多' }}</button>
        <div v-else class="ledger-end">已加载全部 {{ recallTotal }} 条</div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, nextTick } from 'vue'
import {
  memoryApi,
  type MemoryConcept,
  type MemoryDream,
  type MemoryClarification,
  type CostGovernanceStatus,
  type MemoryRecallLogItem,
} from '@/api/memory'

defineProps<{ embedded?: boolean }>()
defineEmits<{ (e: 'close'): void }>()

const subTabs = [
  { key: 'concepts', label: '概念' },
  { key: 'dreams', label: 'Dream' },
  { key: 'clarifications', label: '澄清' },
  { key: 'status', label: '状态' },
  { key: 'ledger', label: '台账' },
]
const activeSubTab = ref('concepts')

const CONCEPT_LIMIT_STEP = 100
const CONCEPT_LIMIT_MAX = 200
const conceptLimit = ref(CONCEPT_LIMIT_STEP)
const conceptsExhausted = ref(false)
const expandedConcepts = ref<Set<string>>(new Set())

const concepts = ref<MemoryConcept[]>([])
const dreams = ref<MemoryDream[]>([])
const clarifications = ref<MemoryClarification[]>([])
const costStatus = ref<CostGovernanceStatus | null>(null)
const recallLog = ref<MemoryRecallLogItem[]>([])
const recallTotal = ref(0)
const recallHasMore = ref(false)
const loadingRecallMore = ref(false)

// dream 展开/收起：仅当摘要被 4 行截断时才显示"展开全文"
const dreamClamped = ref<Set<string>>(new Set())
const expandedDreams = ref<Set<string>>(new Set())

function checkDreamClamp(el: HTMLElement | null, id: string): void {
  if (!el) return
  // nextTick：DOM 已更新后 layout 完成，scrollHeight 才是 clamp 后的真实高度
  nextTick(() => {
    if (el.scrollHeight > el.clientHeight + 1 && !dreamClamped.value.has(id)) {
      // 新 Set 赋值触发响应式（Set 内部 mutate 不会被 Vue 追踪）
      dreamClamped.value = new Set(dreamClamped.value).add(id)
    }
  })
}

function toggleDream(id: string): void {
  const next = new Set(expandedDreams.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  expandedDreams.value = next
}

const loading = ref({ concepts: false, dreams: false, clarifications: false, status: false, ledger: false })
const errorMsg = ref('')
const deletingId = ref('')
const forgettingId = ref('')
const revertingId = ref('')
const confirmErase = ref(false)
const eraseText = ref('')
const erasing = ref(false)

function trustLabel(t: string): string {
  return { user_stated: '用户陈述', user_authored: '用户笔记', agent_inferred: '助手推断', external: '外部内容' }[t] || t
}

function typeLabel(t: string): string {
  return { semantic: '语义', episodic: '事件', procedural: '方法' }[t] || t
}

function statusLabel(s: string): string {
  return { active: '活跃', silent: '沉默', cold_forgotten: '冷遗忘', forgotten: '已遗忘' }[s] || s
}

function correctionLabel(t: string): string {
  return { negate: '否定', refine: '修正', add_constraint: '补充约束', forget: '遗忘' }[t] || t
}

function tierLabel(t: string): string {
  return { concept: '概念', episodic: '事件', subconscious: '原文' }[t] || t
}

function formatDate(s: string): string {
  const d = new Date(s)
  return isNaN(d.getTime()) ? s : d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

function formatDateTime(s: string | null): string {
  if (!s) return '—'
  const d = new Date(s)
  if (isNaN(d.getTime())) return s
  return d.toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'
  })
}

function hashShort(h: string | null): string {
  if (!h) return '—'
  return h.length > 10 ? h.slice(0, 10) + '…' : h
}

function toggleConcept(id: string): void {
  const next = new Set(expandedConcepts.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  expandedConcepts.value = next
}

function tierSummary(t: Record<string, [string, number][]> | null): string {
  if (!t) return ''
  return Object.entries(t)
    .filter(([, v]) => Array.isArray(v) && v.length)
    .map(([k, v]) => `${tierLabel(k)}${v.length}`)
    .join(' · ')
}

async function loadConcepts() {
  loading.value.concepts = true
  try {
    const items = await memoryApi.getConcepts(conceptLimit.value)
    concepts.value = items
    conceptsExhausted.value = items.length < conceptLimit.value || conceptLimit.value >= CONCEPT_LIMIT_MAX
  } catch (e: any) {
    errorMsg.value = '概念加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.concepts = false
  }
}

function loadMoreConcepts() {
  conceptLimit.value = Math.min(conceptLimit.value + CONCEPT_LIMIT_STEP, CONCEPT_LIMIT_MAX)
  loadConcepts()
}

async function loadDreams() {
  loading.value.dreams = true
  try {
    dreams.value = await memoryApi.getDreams()
  } catch (e: any) {
    errorMsg.value = 'Dream 加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.dreams = false
  }
}

async function loadClarifications() {
  loading.value.clarifications = true
  try {
    clarifications.value = await memoryApi.getClarifications()
  } catch (e: any) {
    errorMsg.value = '澄清记录加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.clarifications = false
  }
}

async function loadStatus() {
  loading.value.status = true
  try {
    costStatus.value = await memoryApi.getCostGovernanceStatus()
  } catch (e: any) {
    errorMsg.value = '状态加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.status = false
  }
}

async function loadRecallLog() {
  loading.value.ledger = true
  try {
    const { items, total } = await memoryApi.getRecallLog(50)
    recallLog.value = items
    recallTotal.value = total
    recallHasMore.value = items.length >= 50 && items.length < total
  } catch (e: any) {
    errorMsg.value = '台账加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.ledger = false
  }
}

async function loadMoreRecallLog() {
  const last = recallLog.value[recallLog.value.length - 1]
  if (!last) return
  loadingRecallMore.value = true
  try {
    const { items, total } = await memoryApi.getRecallLog(50, last.id)
    recallLog.value = [...recallLog.value, ...items]
    recallTotal.value = total
    recallHasMore.value = items.length >= 50 && recallLog.value.length < total
  } catch (e: any) {
    errorMsg.value = '台账加载失败：' + (e?.message || '未知错误')
  } finally {
    loadingRecallMore.value = false
  }
}

async function handleDeleteConcept(c: MemoryConcept) {
  if (!window.confirm(`删除概念"${c.canonical_name}"？此操作不可恢复。`)) return
  deletingId.value = c.id
  try {
    await memoryApi.deleteConcept(c.id)
    concepts.value = concepts.value.filter(x => x.id !== c.id)
  } catch (e: any) {
    errorMsg.value = '删除失败：' + (e?.message || '未知错误')
  } finally {
    deletingId.value = ''
  }
}

async function handleForgetConcept(c: MemoryConcept) {
  if (!window.confirm(`遗忘概念"${c.canonical_name}"？遗忘后不再参与召回，可通过删除彻底移除。`)) return
  forgettingId.value = c.id
  try {
    await memoryApi.forgetConcept(c.id)
    await loadConcepts()
  } catch (e: any) {
    errorMsg.value = '遗忘失败：' + (e?.response?.data?.detail || e?.message || '未知错误')
  } finally {
    forgettingId.value = ''
  }
}

async function handleRevert(cl: MemoryClarification) {
  revertingId.value = cl.id
  try {
    await memoryApi.revertClarification(cl.id)
    cl.applied = false
  } catch (e: any) {
    errorMsg.value = '撤销失败：' + (e?.response?.data?.detail || e?.message || '未知错误')
  } finally {
    revertingId.value = ''
  }
}

async function handleEraseAll() {
  erasing.value = true
  try {
    await memoryApi.deleteAll()
    concepts.value = []
    dreams.value = []
    clarifications.value = []
    confirmErase.value = false
    eraseText.value = ''
  } catch (e: any) {
    errorMsg.value = '擦除失败：' + (e?.message || '未知错误')
  } finally {
    erasing.value = false
  }
}

watch(activeSubTab, (tab) => {
  errorMsg.value = ''
  if (tab === 'concepts' && !concepts.value.length) loadConcepts()
  else if (tab === 'dreams' && !dreams.value.length) loadDreams()
  else if (tab === 'clarifications' && !clarifications.value.length) loadClarifications()
  else if (tab === 'status' && !costStatus.value) loadStatus()
  else if (tab === 'ledger' && !recallLog.value.length) loadRecallLog()
})

onMounted(loadConcepts)
</script>

<style scoped>
.memory-panel {
  width: 100%;
  max-width: 640px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.memory-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.memory-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
}

.memory-hint {
  font-size: 13px;
  color: var(--color-text-light);
  margin: 0;
  line-height: 1.5;
}

.memory-subtabs {
  display: flex;
  gap: 6px;
  border-bottom: 1px solid var(--panel-border);
  padding-bottom: 8px;
}

.memory-subtab {
  padding: 6px 14px;
  border: none;
  background: transparent;
  color: var(--color-text-light);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border-radius: 8px;
  transition: background 0.15s ease, color 0.15s ease;
}

.memory-subtab:hover {
  background: var(--surface-panel-subtle);
  color: var(--color-text);
}

.memory-subtab.active {
  background: var(--color-primary);
  color: #fff;
}

.memory-error {
  font-size: 13px;
  color: var(--color-danger, #d93025);
  background: var(--surface-panel-subtle);
  border-radius: 8px;
  padding: 8px 12px;
}

.memory-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 50vh;
  overflow-y: auto;
}

.memory-loading,
.memory-empty {
  font-size: 13px;
  color: var(--color-text-light);
  text-align: center;
  padding: 24px 0;
}

.memory-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  background: var(--surface-panel-subtle);
}

.memory-item.inactive {
  opacity: 0.55;
}

.memory-item-main {
  flex: 1;
  min-width: 0;
}

.memory-item-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.memory-item-desc {
  font-size: 13px;
  color: var(--color-text);
  margin-top: 4px;
  line-height: 1.5;
  word-break: break-word;
}

.memory-item-desc.dream-summary {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-wrap;
}

.memory-item-desc.dream-summary.expanded {
  -webkit-line-clamp: unset;
}

.memory-expand-btn {
  margin-top: 6px;
  padding: 2px 10px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  background: #f6f8fa;
  color: #57606a;
  font-size: 12px;
  cursor: pointer;
  line-height: 1.6;
}

.memory-expand-btn:hover {
  background: #eaeef2;
  color: #24292f;
}

.memory-item-meta {
  font-size: 12px;
  color: var(--color-text-light);
  margin-top: 4px;
}

.tag {
  font-size: 11px;
  font-weight: 500;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  color: var(--color-text-light);
}

.tag.inferred { color: #b45309; border-color: #b45309; }
.tag.silent { color: #6b7280; }
.tag.cold { color: #6b7280; }
.tag.expired { color: #6b7280; text-decoration: line-through; }
.tag.legacy { color: #6b7280; }
.tag.applied { color: #15803d; border-color: #15803d; }
.tag.pending { color: #b45309; border-color: #b45309; }

.memory-delete-btn {
  flex-shrink: 0;
  padding: 5px 12px;
  font-size: 12px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  background: transparent;
  color: var(--color-text-light);
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.memory-delete-btn:hover:not(:disabled) {
  color: var(--color-danger, #d93025);
  border-color: var(--color-danger, #d93025);
}

.memory-delete-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.memory-delete-btn.revert:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.memory-item-actions {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.memory-delete-btn.forget:hover:not(:disabled) {
  color: var(--color-warning);
  border-color: var(--color-warning);
}

.concept-detail {
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  background: var(--surface-panel-strong);
}

.memory-more-btn {
  align-self: center;
  padding: 6px 16px;
  font-size: 13px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  background: var(--surface-panel-strong);
  color: var(--color-text);
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.memory-more-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.memory-more-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.ledger-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  background: var(--surface-panel-subtle);
}

.ledger-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--color-text);
}

.ledger-time {
  flex-shrink: 0;
  color: var(--color-text-light);
  font-variant-numeric: tabular-nums;
}

.ledger-hash {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--color-text-light);
}

.ledger-badges {
  flex-shrink: 0;
  display: flex;
  gap: 4px;
}

.ledger-meta {
  color: var(--color-text-light);
  gap: 12px;
  flex-wrap: wrap;
}

.ledger-tiers {
  font-size: 12px;
  color: var(--color-text-light);
}

.ledger-end {
  font-size: 12px;
  color: var(--color-text-light);
  text-align: center;
  padding: 4px 0;
}

.tag.truncated {
  color: var(--color-warning);
  border-color: var(--color-warning);
  background: var(--warning-tint);
}

.tag.cache {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--primary-tint);
}

.status-card {
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  background: var(--surface-panel-subtle);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.status-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
}

.status-label {
  color: var(--color-text-light);
}

.status-value {
  color: var(--color-text);
  font-weight: 500;
  text-align: right;
}

.status-value.warn {
  color: #b45309;
}

.danger-zone {
  margin-top: 8px;
  border: 1px solid var(--color-danger, #d93025);
  border-radius: 12px;
  padding: 12px;
}

.danger-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-danger, #d93025);
}

.danger-hint {
  font-size: 12px;
  color: var(--color-text-light);
  margin: 6px 0 10px;
  line-height: 1.5;
}

.danger-btn {
  padding: 6px 16px;
  font-size: 13px;
  border: 1px solid var(--color-danger, #d93025);
  border-radius: 8px;
  background: transparent;
  color: var(--color-danger, #d93025);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.danger-btn:hover:not(:disabled) {
  background: var(--color-danger, #d93025);
  color: #fff;
}

.danger-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.danger-confirm-text {
  font-size: 13px;
  color: var(--color-text);
  margin: 0 0 8px;
}

.danger-confirm-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.danger-confirm-row input {
  flex: 1;
  padding: 6px 10px;
  font-size: 13px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  background: var(--surface-panel-strong);
  color: var(--color-text);
}
</style>
