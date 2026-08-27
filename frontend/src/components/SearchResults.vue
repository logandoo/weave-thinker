<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="search-results-card" :class="{ expanded }">
    <button class="search-results-header" @click="expanded = !expanded">
      <span class="search-icon">🔍</span>
      <span v-if="searchFailed && hasUnqualified" class="search-label search-label--failed">检索质量未达标</span>
      <span v-else-if="searchFailed" class="search-label search-label--failed">部分检索轮次未达质量要求</span>
      <span v-else class="search-label">已检索 {{ displayedResultCount }} 个网页</span>
      <svg
        class="chevron"
        :class="{ open: expanded }"
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
      >
        <polyline points="6 9 12 15 18 9" />
      </svg>
    </button>
    <div class="search-results-body" ref="bodyRef">
      <div class="search-results-inner">
        <!-- Search rounds progress -->
        <div v-if="(rounds?.length ?? 0) > 0" class="search-rounds">
          <div
            v-for="round in rounds"
            :key="round.round"
            class="search-round"
            :class="{
              'round--qualified': round.qualified,
              'round--unqualified': !round.qualified,
            }"
          >
            <div class="round-header">
              <span class="round-badge">第{{ round.round }}轮</span>
              <span class="round-queries">{{ round.queries.join(', ') }}</span>
              <span v-if="round.qualified" class="round-status round-status--pass">✓ 合格</span>
              <span v-else class="round-status round-status--fail">✗ 不合格</span>
            </div>
            <div class="round-detail">
              中英文结果 {{ round.cn_en_count }}/{{ round.total_count }}
            </div>
          </div>
        </div>

        <!-- Streaming search progress (during search) -->
        <div v-if="(progressRounds?.length ?? 0) > 0 && (rounds?.length ?? 0) === 0" class="search-rounds">
          <div
            v-for="p in progressRounds"
            :key="p.round"
            class="search-round"
            :class="{
              'round--searching': p.status === 'searching',
              'round--qualified': p.status === 'qualified',
              'round--unqualified': p.status === 'unqualified',
            }"
          >
            <div class="round-header">
              <span class="round-badge">第{{ p.round }}轮</span>
              <span class="round-queries">{{ p.queries.join(', ') }}</span>
              <span v-if="p.status === 'searching'" class="round-status round-status--searching">
                <span class="spinner"></span> 检索中…
              </span>
              <span v-else-if="p.status === 'qualified'" class="round-status round-status--pass">✓ 合格</span>
              <span v-else class="round-status round-status--fail">✗ 不合格</span>
            </div>
            <div v-if="p.status !== 'searching'" class="round-detail">
              中英文结果 {{ p.cn_en_count }}/{{ p.result_count }}
            </div>
          </div>
        </div>

        <!-- Search failure message + confirmation button (only when the
             backend actually supplied unqualified fallback results — partial
             failures carry real results and must not show this CTA) -->
        <div v-if="searchFailed && hasUnqualified && failedData" class="search-failed-section">
          <div class="failure-summary">{{ failedData.failure_summary || '部分检索轮次未达质量要求' }}</div>
          <button
            v-if="onUseUnqualified"
            class="use-unqualified-btn"
            @click="onUseUnqualified?.()"
          >
            使用不合格结果回答
          </button>
        </div>

        <!-- Actual search results -->
        <a
          v-for="(item, idx) in results"
          :key="idx"
          class="search-result-item"
          :href="item.url"
          target="_blank"
          rel="noopener noreferrer"
        >
          <span class="result-index" :title="`正文引用 [${idx + 1}] 对应此来源`">[{{ idx + 1 }}]</span>
          <img
            class="favicon"
            :src="faviconUrl(item.url)"
            width="16"
            height="16"
            loading="lazy"
            @error="($event.target as HTMLImageElement).style.display = 'none'"
          />
          <div class="result-body">
            <div class="result-title">{{ item.title }}</div>
            <div class="result-meta">
              <span class="result-domain">{{ domain(item.url) }}</span>
              <span v-if="item.published_date" class="result-date">{{ item.published_date }}</span>
            </div>
            <div class="result-snippet">{{ item.snippet }}</div>
          </div>
        </a>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, nextTick } from 'vue'
import type { SearchResult, SearchRound, SearchProgress, SearchFailed } from '@/types'

const props = defineProps<{
  results: SearchResult[]
  rounds?: SearchRound[]
  progressRounds?: SearchProgress[]
  searchFailed?: boolean
  failedData?: SearchFailed | null
  onUseUnqualified?: (() => void) | null
}>()

// 后端仅在"全部轮次失败且保留未合格结果"时填充 unqualified_results；
// 部分失败（results 仍有内容）时失败区块/按钮必须隐藏（conv 8f27d43e 系）。
const hasUnqualified = computed(() => (props.failedData?.unqualified_results?.length ?? 0) > 0)

const expanded = ref(false)
const bodyRef = ref<HTMLElement | null>(null)

const displayedResultCount = computed(() => {
  if ((props.results?.length ?? 0) > 0) {
    return props.results.length
  }

  const roundCounts = (props.rounds ?? []).map(round => round.total_count || 0)
  const progressCounts = (props.progressRounds ?? []).map(round => round.result_count || 0)

  return Math.max(0, ...roundCounts, ...progressCounts)
})

// Auto-expand during search progress
watch(
  () => props.progressRounds?.length,
  (len) => {
    if (len && len > 0) expanded.value = true
  },
)

// Auto-expand on failure
watch(
  () => props.searchFailed,
  (failed) => {
    if (failed) expanded.value = true
  },
)

watch(expanded, async () => {
  await nextTick()
  if (bodyRef.value) {
    bodyRef.value.style.maxHeight = expanded.value
      ? bodyRef.value.scrollHeight + 'px'
      : '0px'
  }
})

// Also watch content changes to recalc max-height when expanded
watch(
  () => [props.results?.length ?? 0, props.rounds?.length ?? 0, props.progressRounds?.length ?? 0, props.searchFailed],
  async () => {
    if (expanded.value && bodyRef.value) {
      await nextTick()
      bodyRef.value.style.maxHeight = bodyRef.value.scrollHeight + 'px'
    }
  },
)

function domain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}

function faviconUrl(url: string): string {
  try {
    const host = new URL(url).hostname
    return `https://www.google.com/s2/favicons?sz=32&domain=${host}`
  } catch {
    return ''
  }
}
</script>

<style scoped>
.search-results-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  margin-bottom: 8px;
  overflow: hidden;
  transition: box-shadow var(--transition-fast);
}
.search-results-card:hover {
  box-shadow: var(--shadow-sm);
}

.search-results-header {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 13px;
  color: var(--color-text-light);
  font-family: var(--font-main);
  transition: color var(--transition-fast);
}
.search-results-header:hover {
  color: var(--color-text);
}

.search-icon {
  font-size: 14px;
  flex-shrink: 0;
}

.search-label {
  flex: 1;
  text-align: left;
  font-weight: 500;
}
.search-label--failed {
  color: var(--color-error);
}

.chevron {
  flex-shrink: 0;
  transition: transform var(--transition-normal);
}
.chevron.open {
  transform: rotate(180deg);
}

.search-results-body {
  max-height: 0;
  overflow: hidden;
  transition: max-height var(--transition-normal);
}

.search-results-inner {
  padding: 0 12px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* --- Search rounds --- */
.search-rounds {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 6px;
}

.search-round {
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  border-left: 3px solid transparent;
}
.round--qualified {
  border-left-color: var(--color-success);
  background: color-mix(in srgb, var(--color-success) 6%, transparent);
}
.round--unqualified {
  border-left-color: var(--color-error);
  background: color-mix(in srgb, var(--color-error) 6%, transparent);
}
.round--searching {
  border-left-color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
}

.round-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.round-badge {
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
}

.round-queries {
  flex: 1;
  color: var(--color-text-light);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.round-status {
  font-weight: 500;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 4px;
}
.round-status--pass {
  color: var(--color-success);
}
.round-status--fail {
  color: var(--color-error);
}
.round-status--searching {
  color: var(--color-primary);
}

.round-detail {
  margin-top: 2px;
  color: var(--color-text-light);
  font-size: 11px;
  opacity: 0.7;
}

.spinner {
  display: inline-block;
  width: 10px;
  height: 10px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

/* --- Search failed --- */
.search-failed-section {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--color-error) 6%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-error) 15%, transparent);
}

.failure-summary {
  font-size: 12px;
  color: var(--color-text-light);
  white-space: pre-line;
  line-height: 1.5;
}

.use-unqualified-btn {
  margin-top: 8px;
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 500;
  color: #fff;
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition-fast);
  font-family: var(--font-main);
}
.use-unqualified-btn:hover {
  background: var(--color-primary-dark);
}

/* --- Search result items --- */
.search-result-item {
  display: flex;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-panel-subtle);
  text-decoration: none;
  color: inherit;
  transition: background var(--transition-fast);
  align-items: flex-start;
}
.search-result-item:hover {
  background: var(--color-hover);
}

.result-index {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 26px;
  height: 20px;
  padding: 0 6px;
  margin-top: 1px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--color-primary) 14%, transparent);
  color: var(--color-primary);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
  line-height: 1;
  user-select: none;
}
.search-result-item:hover .result-index {
  background: color-mix(in srgb, var(--color-primary) 22%, transparent);
}

.favicon {
  flex-shrink: 0;
  margin-top: 2px;
  border-radius: 3px;
}

.result-body {
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.result-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1.4;
}

.result-domain {
  font-size: 11px;
  color: var(--color-text-light);
  margin-top: 1px;
}

.result-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 1px;
}

.result-date {
  font-size: 11px;
  color: var(--color-text-light);
  opacity: 0.7;
}

.result-snippet {
  font-size: 12px;
  color: var(--color-text-light);
  margin-top: 3px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.4;
}
</style>
