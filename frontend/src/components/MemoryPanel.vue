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
      <div class="memory-toolbar">
        <label class="memory-toggle">
          <input v-model="includeHistory" type="checkbox" />
          显示历史版本
        </label>
        <select v-model="verificationFilter" class="memory-select">
          <option value="any">全部验证状态</option>
          <option value="confirmed">已验证</option>
          <option value="contradicted">待核实</option>
          <option value="insufficient">证据不足</option>
          <option value="unverified">未验证</option>
        </select>
      </div>
      <div v-if="loading.concepts" class="memory-loading">加载中…</div>
      <div v-else-if="concepts.length === 0" class="memory-empty">
        暂无概念记忆。新记忆架构未启用或尚未积累概念。
      </div>
      <template v-else>
        <div
          v-for="c in concepts"
          :key="c.id"
          class="memory-item"
          :class="{ inactive: c.is_history || c.status !== 'active' }"
        >
          <div class="memory-item-main">
            <div class="memory-item-name">
              {{ c.canonical_name }}
              <span v-if="c.source_trust === 'agent_inferred'" class="tag inferred">推断</span>
              <span v-if="c.status === 'silent'" class="tag silent">沉默</span>
              <span v-if="c.status === 'cold_forgotten'" class="tag cold">冷遗忘</span>
              <span v-if="c.invalid_reason" class="tag" :class="reasonTagClass(c.invalid_reason)">
                {{ reasonLabel(c.invalid_reason) }}
              </span>
              <span v-if="c.needs_review" class="tag review" title="来源待复核（nightly 溯源链）">需复核</span>
              <span v-if="c.verification_state === 'confirmed'" class="tag verified">已验证</span>
              <span v-else-if="c.verification_state === 'contradicted'" class="tag contradicted">待核实</span>
              <span v-else-if="c.verification_state === 'insufficient'" class="tag insufficient">证据不足</span>
            </div>
            <div class="memory-item-desc">{{ c.description_short }}</div>
            <div class="memory-item-meta">
              重要度 {{ c.importance.toFixed(2) }} · 权重 {{ c.weight.toFixed(2) }} · {{ trustLabel(c.source_trust) }} · {{ typeLabel(c.memory_type) }}
              <template v-if="c.created_at"> · {{ formatDate(c.created_at) }}</template>
              <template v-if="c.is_history"> · 历史</template>
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
              <div v-if="c.invalid_reason" class="memory-item-meta">
                生命周期：{{ validityText(c) }}
              </div>
              <div v-if="c.superseded_by_name" class="memory-item-meta">
                后继版本：{{ c.superseded_by_name }}
              </div>
              <div v-if="c.source_message_ids && c.source_message_ids.length" class="memory-item-meta">
                来源消息：{{ c.source_message_ids.slice(0, 3).join('、') }}
              </div>
              <div v-if="c.evidence" class="verify-evidence">
                <div class="verify-evidence-title">
                  验证证据
                  <span v-if="c.evidence.manual" class="tag applied">人工</span>
                </div>
                <div v-if="c.evidence.quote" class="verify-quote">“{{ truncate(c.evidence.quote, 200) }}”</div>
                <div class="memory-item-meta">
                  {{ c.evidence.reason || '—' }}
                  <template v-if="c.evidence.checked_at"> · {{ formatDateTime(c.evidence.checked_at) }}</template>
                </div>
                <div v-if="c.evidence.source_ids && c.evidence.source_ids.length" class="memory-item-meta">
                  来源：{{ c.evidence.source_ids.slice(0, 3).join('、') }}
                </div>
              </div>
              <div v-if="c.verification_correction" class="verify-correction">
                <div class="verify-evidence-title">修正建议</div>
                <div class="memory-item-desc">{{ c.verification_correction }}</div>
                <button class="memory-delete-btn" :disabled="verifyingId === c.id" @click="handleVerify(c, 'apply_correction')">
                  {{ verifyingId === c.id ? '应用中…' : '应用修正' }}
                </button>
              </div>
            </div>
          </div>
          <div class="memory-item-actions">
            <template v-if="!c.is_history">
              <button
                class="memory-delete-btn verify"
                :disabled="verifyingId === c.id || c.verification_state === 'confirmed'"
                title="确认仍然有效"
                @click="handleVerify(c, 'confirm')"
              >{{ verifyingId === c.id ? '…' : '仍然有效' }}</button>
              <button
                class="memory-delete-btn verify"
                :disabled="verifyingId === c.id || c.verification_state === 'contradicted'"
                title="否认这条记忆（待核实，不写失效）"
                @click="handleVerify(c, 'reject')"
              >{{ verifyingId === c.id ? '…' : '否认' }}</button>
              <button
                v-if="c.verification_state"
                class="memory-delete-btn verify"
                :disabled="verifyingId === c.id"
                title="清除验证状态，等待重新对照"
                @click="handleVerify(c, 'retry')"
              >{{ verifyingId === c.id ? '…' : '重验' }}</button>
            </template>
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
        >{{ loading.concepts ? '加载中…' : `加载更多（已载 ${concepts.length}/${conceptsTotal}）` }}</button>
      </template>
    </div>

    <!-- 健康度 -->
    <div v-else-if="activeSubTab === 'health'" class="memory-section">
      <div v-if="loading.health" class="memory-loading">加载中…</div>
      <template v-else-if="health">
        <div class="status-card">
          <div class="status-row"><span class="status-label">概念总数</span><span class="status-value">{{ health.total }}</span></div>
          <div class="status-row"><span class="status-label">现行 / 合并 / 撤回 / 来源丢失</span><span class="status-value">{{ health.active }} / {{ health.merged }} / {{ health.retracted }} / {{ health.source_lost }}</span></div>
          <div class="status-row"><span class="status-label">遗忘 / 过期</span><span class="status-value">{{ health.forgotten }} / {{ health.expired }}</span></div>
          <div class="status-row"><span class="status-label">需复核 / 验证覆盖</span><span class="status-value">{{ health.needs_review }} / {{ (health.verify_coverage * 100).toFixed(1) }}%</span></div>
          <div class="status-row"><span class="status-label">已验证 / 待核实 / 证据不足（均现行口径）</span><span class="status-value">{{ health.verified }} / {{ health.contradicted }} / {{ health.insufficient }}</span></div>
          <div v-if="health.dream_injectable_rate != null" class="status-row"><span class="status-label">Dream 可注入率</span><span class="status-value">{{ (health.dream_injectable_rate * 100).toFixed(1) }}%</span></div>
          <div v-if="health.recall_7d_avg_injected_chars != null" class="status-row"><span class="status-label">7 日平均注入字符</span><span class="status-value">{{ health.recall_7d_avg_injected_chars }}</span></div>
        </div>
        <div v-if="health.last_verify" class="memory-note">
          最近一次 Dream Verify：selected {{ health.last_verify.selected ?? 0 }} · confirmed {{ health.last_verify.confirmed ?? 0 }} · contradicted {{ health.last_verify.contradicted ?? 0 }} · insufficient {{ health.last_verify.insufficient ?? 0 }}
        </div>
      </template>
      <div v-else class="memory-empty">暂无健康度数据</div>
    </div>

    <!-- Dream -->
    <div v-else-if="activeSubTab === 'dreams'" class="memory-section">
      <div v-if="loading.dreams" class="memory-loading">加载中…</div>
      <div v-else-if="dreams.length === 0" class="memory-empty">暂无 dream 记录</div>
      <template v-else>
      <div v-for="d in pagedDreams" :key="d.id" class="memory-item">
        <div class="memory-item-main">
          <div class="memory-item-name">
            {{ d.generated_for_date }}
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
      <PaginationBar v-model:page="dreamsPage" :total-pages="dreamsPages" />
      <div v-if="dreamsTruncated" class="memory-note">仅显示最近 {{ DREAM_CAP }} 条 dream，更早的记录未在此列出。</div>
      </template>
    </div>

    <!-- 澄清 -->
    <div v-else-if="activeSubTab === 'clarifications'" class="memory-section">
      <div v-if="loading.clarifications" class="memory-loading">加载中…</div>
      <div v-else-if="clarifications.length === 0" class="memory-empty">暂无澄清记录</div>
      <template v-else>
      <div v-for="cl in pagedClarifications" :key="cl.id" class="memory-item">
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
      <PaginationBar v-model:page="clarPage" :total-pages="clarPages" />
      <div v-if="clarTruncated" class="memory-note">仅显示最近 {{ CLAR_CAP }} 条澄清记录，更早的记录未在此列出。</div>
      </template>
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

    <!-- 约束（W1-2：固定约束可见可撤销，P5） -->
    <div v-else-if="activeSubTab === 'constraints'" class="memory-section">
      <div class="memory-note">
        固定约束由你在对话中的明确指令经信任门固定（仅本人消息、每会上限 20 条），压缩后原样重放。此处可撤销（立即不再重放）。
      </div>
      <div v-if="loading.constraints" class="memory-loading">加载中…</div>
      <div v-else-if="constraints.length === 0" class="memory-empty">暂无固定约束</div>
      <template v-else>
        <div v-for="c in pagedConstraints" :key="c.id" class="ledger-item">
          <div class="ledger-row">
            <span class="constraint-text">{{ c.text }}</span>
            <span class="ledger-badges">
              <span v-if="c.revoked_at" class="tag truncated">已撤销</span>
              <span v-else class="tag cache">生效中</span>
            </span>
          </div>
          <div class="ledger-row ledger-meta">
            <span>{{ formatDateTime(c.created_at) }}</span>
            <span>{{ c.conversation_id ? c.conversation_id.slice(0, 8) : '' }}</span>
            <button
              v-if="!c.revoked_at"
              class="memory-delete-btn"
              :disabled="revokingId === c.id"
              @click="handleRevokeConstraint(c.id)"
            >{{ revokingId === c.id ? '撤销中…' : '撤销' }}</button>
          </div>
        </div>
        <PaginationBar v-model:page="constraintsPage" :total-pages="constraintsPages" />
      </template>
    </div>

    <!-- 保留/淘汰（W5-3：容量淘汰预览，shadow 只记录，可撤销） -->
    <div v-else-if="activeSubTab === 'retention'" class="memory-section">
      <div class="memory-note">
        保留/淘汰按容量计算（retention_score：权重 + 新近度 + 复现）。默认 shadow——只预览不删除；淘汰=归档降级（fidelity_state），可一键恢复。
      </div>
      <div v-if="loading.retention" class="memory-loading">加载中…</div>
      <div v-else-if="retentionPreview.candidates.length === 0" class="memory-empty">
        活跃概念 {{ retentionPreview.active }} / 容量 {{ retentionPreview.capacity }}——未超容量，无淘汰候选
      </div>
      <template v-else>
        <div class="ledger-row ledger-meta">
          <span>活跃 {{ retentionPreview.active }} / 容量 {{ retentionPreview.capacity }}</span>
          <span>mode: {{ retentionPreview.mode }}</span>
        </div>
        <div v-for="c in pagedRetentionCandidates" :key="c.id" class="ledger-item">
          <div class="ledger-row">
            <span class="constraint-text">{{ c.canonical_name }}</span>
            <span class="ledger-badges">
              <span class="tag truncated">{{ c.fidelity_state }}</span>
              <span class="tag cache">分 {{ (c.retention_score ?? 0).toFixed(3) }}</span>
            </span>
          </div>
          <div class="ledger-row ledger-meta">
            <span>候选动作：{{ c.action }}</span>
            <button
              class="memory-delete-btn"
              :disabled="undoingId === c.id"
              @click="handleUndoRetention(c.id)"
            >{{ undoingId === c.id ? '恢复中…' : (c.fidelity_state === 'archived' ? '恢复 full' : '预览淘汰（未执行）') }}</button>
          </div>
        </div>
        <PaginationBar v-model:page="retentionPage" :total-pages="retentionPages" />
      </template>
    </div>

    <!-- Dream 修复报告（W3-3）已随 dreaming 收敛删除（2026-11-08） -->
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, computed, nextTick } from 'vue'
import {
  memoryApi,
  type MemoryConcept,
  type MemoryDream,
  type MemoryClarification,
  type CostGovernanceStatus,
  type MemoryHealth,
  type MemoryRecallLogItem,
} from '@/api/memory'
import { dualCursors, dualShouldLoadMore, mergePages, totalPages as calcTotalPages, pageSlice as calcPageSlice } from '@/composables/memoryPagination'
import PaginationBar from './PaginationBar.vue'

defineProps<{ embedded?: boolean }>()
defineEmits<{ (e: 'close'): void }>()

const subTabs = [
  { key: 'concepts', label: '概念' },
  { key: 'health', label: '健康度' },
  { key: 'dreams', label: 'Dream' },
  { key: 'clarifications', label: '澄清' },
  { key: 'constraints', label: '约束' },
  { key: 'retention', label: '保留/淘汰' },
  { key: 'status', label: '状态' },
  { key: 'ledger', label: '台账' },
]
const activeSubTab = ref('concepts')

const CONCEPT_LIMIT_STEP = 100
const conceptLimit = ref(CONCEPT_LIMIT_STEP)
const conceptsExhausted = ref(false)
// P1-19（2026-11-08）：offset 翻页取代「加大 limit」（旧实现 200 封顶，645+ 现行看不全）
const conceptsTotal = ref(0)
// 评审修复：include_history 模式下现行/历史两段独立游标（各自 total）
const conceptsActiveTotal = ref(0)
const conceptsHistoryTotal = ref(0)

function conceptPageState() {
  const loadedActive = concepts.value.filter((c) => !c.valid_to).length
  return {
    loadedActive,
    activeTotal: conceptsActiveTotal.value,
    loadedHistory: concepts.value.length - loadedActive,
    historyTotal: conceptsHistoryTotal.value,
  }
}
const expandedConcepts = ref<Set<string>>(new Set())

const concepts = ref<MemoryConcept[]>([])
const includeHistory = ref(false)
const verificationFilter = ref('any')
const health = ref<MemoryHealth | null>(null)
const verifyingId = ref('')
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

const loading = ref({ concepts: false, health: false, dreams: false, clarifications: false, status: false, ledger: false, constraints: false, retention: false })
const errorMsg = ref('')
const deletingId = ref('')
const forgettingId = ref('')
const revertingId = ref('')
const revokingId = ref('')
const undoingId = ref('')
const confirmErase = ref(false)
const eraseText = ref('')
const erasing = ref(false)

// W1-2/W5-3/W3-3（记忆/长程 v2）：约束 / 保留淘汰预览 / dream 修复报告
interface PinnedConstraint {
  id: string
  conversation_id: string
  text: string
  source_role: string
  provenance: string
  created_at: string | null
  revoked_at: string | null
}
interface RetentionCandidate {
  id: string
  canonical_name: string
  retention_score: number | null
  fidelity_state: string
  action: string
}
const constraints = ref<PinnedConstraint[]>([])
const retentionPreview = ref<{ capacity: number; active: number; mode: string; loaded: boolean; candidates: RetentionCandidate[] }>(
  { capacity: 0, active: 0, mode: 'shadow', loaded: false, candidates: [] })

// 客户端分页（Dream/澄清/约束/保留淘汰）：与 MCP/热词/技能/设备 面板同口径
// PAGE_SIZE=10 + PaginationBar；概念/台账走各自服务端翻页，不在此列（ADR D-20261112-mem-pagination）。
const PAGE_SIZE = 10
const dreamsPage = ref(1)
const clarPage = ref(1)
const constraintsPage = ref(1)
const retentionPage = ref(1)

// 后端上限（dreams min(limit,50)、clarifications min(limit,200)），用于「仅显示最近 N 条」披露
const DREAM_CAP = 50
const CLAR_CAP = 200
const totalPages = (n: number) => calcTotalPages(n, PAGE_SIZE)
const pageSlice = <T>(arr: T[], page: number) => calcPageSlice(arr, page, PAGE_SIZE)
const dreamsPages = computed(() => totalPages(dreams.value.length))
const clarPages = computed(() => totalPages(clarifications.value.length))
const constraintsPages = computed(() => totalPages(constraints.value.length))
const retentionPages = computed(() => totalPages(retentionPreview.value.candidates.length))
const pagedDreams = computed(() => pageSlice(dreams.value, dreamsPage.value))
const pagedClarifications = computed(() => pageSlice(clarifications.value, clarPage.value))
const pagedConstraints = computed(() => pageSlice(constraints.value, constraintsPage.value))
const pagedRetentionCandidates = computed(() => pageSlice(retentionPreview.value.candidates, retentionPage.value))
// 命中后端上限 ⇒ 更早的记录不可达（客户端分页无法触达），显式披露避免「看起来完整」（评审 A6-I5）
const dreamsTruncated = computed(() => dreams.value.length >= DREAM_CAP)
const clarTruncated = computed(() => clarifications.value.length >= CLAR_CAP)
// 数据变化后钳制页码（删除/重载/切筛选都可能缩短页数）
watch(dreamsPages, (p) => { if (dreamsPage.value > p) dreamsPage.value = p })
watch(clarPages, (p) => { if (clarPage.value > p) clarPage.value = p })
watch(constraintsPages, (p) => { if (constraintsPage.value > p) constraintsPage.value = p })
watch(retentionPages, (p) => { if (retentionPage.value > p) retentionPage.value = p })

function trustLabel(t: string): string {
  return { user_stated: '用户陈述', user_authored: '用户笔记', agent_inferred: '助手推断', external: '外部内容' }[t] || t
}

function typeLabel(t: string): string {
  return { semantic: '语义', episodic: '事件', procedural: '方法' }[t] || t
}

function statusLabel(s: string): string {
  return { active: '活跃', silent: '沉默', cold_forgotten: '冷遗忘', forgotten: '已遗忘' }[s] || s
}

function reasonLabel(r: string | null | undefined): string {
  return {
    merged: '历史版本',
    retracted: '用户已否认',
    source_lost: '来源已丢失',
    forgotten: '用户已遗忘',
    expired: '已过期',
  }[r || ''] || (r || '')
}

function reasonTagClass(r: string | null | undefined): string {
  return {
    merged: 'history',
    retracted: 'retracted',
    source_lost: 'source-lost',
    forgotten: 'forgotten',
    expired: 'expired',
  }[r || ''] || 'expired'
}

function validityText(c: MemoryConcept): string {
  if (c.invalid_reason === 'merged') {
    return `历史版本，已并入 ${c.superseded_by_name || '新版本'}`
  }
  return reasonLabel(c.invalid_reason)
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + '…' : s
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
    const { items, total, activeTotal, historyTotal } = await memoryApi.getConcepts(conceptLimit.value, {
      includeHistory: includeHistory.value,
      verificationState: verificationFilter.value,
    })
    concepts.value = items
    conceptsTotal.value = total
    conceptsActiveTotal.value = activeTotal
    conceptsHistoryTotal.value = historyTotal
    conceptsExhausted.value = !dualShouldLoadMore(conceptPageState())
  } catch (e: any) {
    errorMsg.value = '概念加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.concepts = false
  }
}

function reloadConcepts() {
  conceptLimit.value = CONCEPT_LIMIT_STEP
  loadConcepts()
}

async function handleVerify(c: MemoryConcept, action: 'confirm' | 'reject' | 'retry' | 'apply_correction') {
  verifyingId.value = c.id
  errorMsg.value = ''
  try {
    await memoryApi.verifyConcept(c.id, action)
    await loadConcepts()
    if (activeSubTab.value === 'health') await loadHealth()
  } catch (e: any) {
    errorMsg.value = '验证操作失败：' + (e?.response?.data?.detail || e?.message || '未知错误')
  } finally {
    verifyingId.value = ''
  }
}

function loadMoreConcepts() {
  // P1-19 + 评审修复：现行/历史双游标各自推进（合并列表的总 loaded 数做单
  // offset 会让两段同时跳步漏行）；按 id 去重追加，不受 200 上限约束。
  const { offset, historyOffset } = dualCursors(conceptPageState())
  loading.value.concepts = true
  memoryApi.getConcepts(conceptLimit.value, {
    includeHistory: includeHistory.value,
    verificationState: verificationFilter.value,
    offset,
    historyOffset,
  }).then(({ items, total, activeTotal, historyTotal }) => {
    concepts.value = mergePages(concepts.value, items)
    conceptsTotal.value = total
    conceptsActiveTotal.value = activeTotal
    conceptsHistoryTotal.value = historyTotal
    conceptsExhausted.value = items.length === 0 || !dualShouldLoadMore(conceptPageState())
  }).catch((e: any) => {
    errorMsg.value = '概念加载失败：' + (e?.message || '未知错误')
  }).finally(() => {
    loading.value.concepts = false
  })
}

async function loadDreams() {
  loading.value.dreams = true
  try {
    // 拉满后端上限（50）再客户端分页；旧值 10 会让分页恒为单页
    dreams.value = await memoryApi.getDreams(50)
  } catch (e: any) {
    errorMsg.value = 'Dream 加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.dreams = false
  }
}

async function loadClarifications() {
  loading.value.clarifications = true
  try {
    // 拉满后端上限（200）再客户端分页
    clarifications.value = await memoryApi.getClarifications(200)
  } catch (e: any) {
    errorMsg.value = '澄清记录加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.clarifications = false
  }
}

async function loadHealth() {
  loading.value.health = true
  errorMsg.value = ''
  try {
    health.value = await memoryApi.getHealth()
  } catch (e: any) {
    errorMsg.value = '健康度加载失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.health = false
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

async function loadConstraints(): Promise<void> {
  loading.value.constraints = true
  errorMsg.value = ''
  try {
    constraints.value = await memoryApi.getConstraints()
  } catch (e: any) {
    errorMsg.value = '加载约束失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.constraints = false
  }
}

async function handleRevokeConstraint(id: string): Promise<void> {
  revokingId.value = id
  errorMsg.value = ''
  try {
    await memoryApi.revokeConstraint(id)
    await loadConstraints()
  } catch (e: any) {
    errorMsg.value = '撤销失败：' + (e?.message || '未知错误')
  } finally {
    revokingId.value = ''
  }
}

async function loadRetention(): Promise<void> {
  loading.value.retention = true
  errorMsg.value = ''
  try {
    const data = await memoryApi.getRetentionPreview()
    retentionPreview.value = { ...data, loaded: true }
  } catch (e: any) {
    errorMsg.value = '加载保留预览失败：' + (e?.message || '未知错误')
  } finally {
    loading.value.retention = false
  }
}

async function handleUndoRetention(id: string): Promise<void> {
  undoingId.value = id
  errorMsg.value = ''
  try {
    await memoryApi.undoRetention([id])
    await loadRetention()
  } catch (e: any) {
    errorMsg.value = '恢复失败：' + (e?.message || '未知错误')
  } finally {
    undoingId.value = ''
  }
}

watch([includeHistory, verificationFilter], () => reloadConcepts())

watch(activeSubTab, (tab) => {
  errorMsg.value = ''
  if (tab === 'concepts' && !concepts.value.length) loadConcepts()
  else if (tab === 'health') loadHealth()
  else if (tab === 'dreams' && !dreams.value.length) loadDreams()
  else if (tab === 'clarifications' && !clarifications.value.length) loadClarifications()
  else if (tab === 'status' && !costStatus.value) loadStatus()
  else if (tab === 'ledger' && !recallLog.value.length) loadRecallLog()
  else if (tab === 'constraints' && !constraints.value.length) loadConstraints()
  else if (tab === 'retention' && !retentionPreview.value.candidates.length && !retentionPreview.value.loaded) loadRetention()
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
  flex-wrap: nowrap;
  gap: 6px;
  border-bottom: 1px solid var(--panel-border);
  padding-bottom: 8px;
  overflow-x: auto;
  overflow-y: hidden;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: thin;
}

.memory-subtab {
  flex: 0 0 auto;
  white-space: nowrap;
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
.tag.verified { color: #15803d; border-color: #15803d; }
.tag.contradicted { color: #b91c1c; border-color: #b91c1c; }
.tag.insufficient { color: #b45309; border-color: #b45309; }
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

/* W1-2/W5-3/W3-3（记忆/长程 v2）：约束 / 保留淘汰 / dream 修复面板 */
.memory-note {
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text);
  opacity: 0.75;
  margin-bottom: 10px;
  padding: 8px 10px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  background: var(--surface-panel-subtle);
}

.constraint-text {
  flex: 1;
  font-size: 13px;
  color: var(--color-text);
  word-break: break-word;
}

/* 2026-11-08 validity / verify / health */
.memory-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.memory-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-light);
}

.memory-select {
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 8px;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text);
}

.tag.history { color: var(--color-text-light); }
.tag.retracted { color: var(--color-danger, #b91c1c); border-color: var(--color-danger, #b91c1c); }
.tag.source-lost { color: var(--color-warning, #b45309); border-color: var(--color-warning, #b45309); }
.tag.forgotten { color: var(--color-text-light); text-decoration: line-through; }
.tag.review { color: var(--color-warning, #b45309); border-color: var(--color-warning, #b45309); }

.verify-evidence,
.verify-correction {
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px dashed var(--panel-border);
  border-radius: 8px;
  background: var(--surface-panel-subtle);
}

.verify-evidence-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
}

.verify-quote {
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text);
  word-break: break-word;
}

.memory-delete-btn.verify {
  color: var(--color-primary);
  border-color: var(--color-primary);
}
</style>
