<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 笔记绑定选择器（2026-11-16 Wave B）— 按 **笔记本 → 笔记** 两级挑多条笔记，一次绑到当前任务。
 *
 * 为什么是卡片而不是原来的内嵌扁平列表：原实现只读「默认笔记本」，用户绝大多数笔记
 * 根本选不到；且一次只能绑一条。这里改成居中卡片：
 *   · 浏览态 = 笔记本分组（可折叠、懒加载该组笔记、组级全选）；
 *   · 搜索态 = 跨全部笔记本的标题/正文检索（`GET /api/notes/search`）；
 *   · 选择态 = 复选框多选，跨分组/跨两种视图保持；
 *   · 已绑到本任务的笔记 disabled + 「已绑定」徽标（幂等由后端兜底，UI 先挡住重复）。
 *
 * 绑定是多对多：绑到本任务不影响这条笔记在其他任务/项目上的绑定，所以「已绑 N 处」
 * 徽标要显式画出来，避免用户以为绑定是「移动」。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { notesApi } from '@/api/notes'
import type { NoteListItem, Notebook } from '@/types'
import { fetchNoteBindingCounts } from '@/api/workspace'

const props = defineProps<{
  visible: boolean
  /** 当前任务已绑的笔记 id（这些行 disabled）。 */
  boundNoteIds: string[]
  /** 卡片副标题里显示的任务名。 */
  taskTitle?: string
}>()

const emit = defineEmits<{
  close: []
  confirm: [noteIds: string[]]
}>()

interface GroupState {
  notebook: Notebook
  loaded: boolean
  loading: boolean
  expanded: boolean
  notes: NoteListItem[]
}

const loading = ref(false)
const error = ref('')
const search = ref('')
const searching = ref(false)
const groups = ref<GroupState[]>([])
const searchResults = ref<Awaited<ReturnType<typeof notesApi.searchNotes>>>([])
const selected = ref<Set<string>>(new Set())
const boundCounts = ref<Record<string, number>>({})
const busy = ref(false)

const boundSet = computed(() => new Set(props.boundNoteIds))
const selectedIds = computed(() => [...selected.value])
const canBind = computed(() => !busy.value && selectedIds.value.length > 0)

/** 空笔记本没有可绑对象，列出来只是噪音（测试账号有 300+ 个空簿）。 */
const hasNotes = (nb: Notebook) => (nb.note_count ?? 0) > 0

/** 分组顺序：默认笔记本 → 最近动过的笔记本 → 名称。
 *  不按笔记数排：一个 180 篇的老簿会把首屏刷满，而用户找的是「我刚写过东西的那本」。 */
function sortNotebooks(list: Notebook[]): Notebook[] {
  return [...list].sort((a, b) => {
    if (a.is_default !== b.is_default) return a.is_default ? -1 : 1
    const at = String(a.updated_at || '').localeCompare(String(b.updated_at || ''))
    if (at !== 0) return -at
    return (a.name || '').localeCompare(b.name || '', 'zh-Hans-CN')
  })
}

/** 首屏自动展开的阈值：超过这个量的笔记本不自动摊开（滚到它再加载）。 */
const AUTO_EXPAND_MAX = 60

async function loadGroups() {
  loading.value = true
  error.value = ''
  try {
    const nbs = sortNotebooks((await notesApi.getNotebooks()).filter(hasNotes))
    groups.value = nbs.map((notebook) => ({
      notebook, loaded: false, loading: false, expanded: false, notes: [],
    }))
    // 展开第一个「摊得开」的笔记本，用户一进来就能看到可选项
    const first = groups.value.find((g) => (g.notebook.note_count ?? 0) <= AUTO_EXPAND_MAX)
      || groups.value[0]
    if (first) {
      first.expanded = true
      await loadGroupNotes(first)
    }
    void refreshBoundCounts()
  } catch (e: any) {
    error.value = e?.response?.status ? `加载笔记本失败（${e.response.status}）` : '加载笔记本失败'
  } finally {
    loading.value = false
  }
}

async function loadGroupNotes(group: GroupState) {
  if (group.loaded || group.loading) return
  group.loading = true
  try {
    group.notes = await notesApi.getNotes(group.notebook.id)
    group.loaded = true
    group.expanded = true
    void refreshBoundCounts()
  } catch {
    group.notes = []
  } finally {
    group.loading = false
  }
}

async function refreshBoundCounts() {
  const ids = visibleNoteIds.value.filter((id) => !(id in boundCounts.value))
  if (!ids.length) return
  try {
    boundCounts.value = { ...boundCounts.value, ...(await fetchNoteBindingCounts(ids)) }
  } catch {
    /* 徽标是辅助信息，拉不到不阻塞绑定 */
  }
}

const isSearchMode = computed(() => search.value.trim().length > 0)

async function runSearch() {
  const q = search.value.trim()
  if (!q) {
    searching.value = false
    searchResults.value = []
    return
  }
  searching.value = true
  error.value = ''
  try {
    searchResults.value = await notesApi.searchNotes(q)
    const ids = searchResults.value.map((r) => r.note_id).filter((id) => !(id in boundCounts.value))
    if (ids.length) boundCounts.value = { ...boundCounts.value, ...(await fetchNoteBindingCounts(ids)) }
  } catch (e: any) {
    error.value = apiDetail(e) || '搜索失败'
  } finally {
    searching.value = false
  }
}

function apiDetail(e: any): string {
  const d = e?.response?.data?.detail
  if (d && typeof d === 'object') return String(d.error || '')
  return typeof d === 'string' ? d : ''
}

let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(search, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(runSearch, 260)
})

/** 浏览态：全部笔记本分组都要出现（未加载的靠展开时懒加载）——
 *  把「未加载」的行过滤掉会让用户永远看不到第二个笔记本，等于回到旧 bug。 */
const filteredGroups = computed(() => groups.value)

const visibleNoteIds = computed(() => {
  if (isSearchMode.value) return searchResults.value.map((r) => r.note_id)
  return groups.value.filter((g) => g.loaded).flatMap((g) => g.notes.map((n) => n.id))
})

const searchGroups = computed(() => {
  const byNb = new Map<string, { name: string; items: typeof searchResults.value }>()
  for (const r of searchResults.value) {
    if (!byNb.has(r.notebook_id)) byNb.set(r.notebook_id, { name: r.notebook_name, items: [] })
    byNb.get(r.notebook_id)!.items.push(r)
  }
  return [...byNb.values()]
})

const selectedCount = computed(() => selected.value.size)

function toggle(noteId: string) {
  if (boundSet.value.has(noteId)) return
  const next = new Set(selected.value)
  if (next.has(noteId)) next.delete(noteId)
  else next.add(noteId)
  selected.value = next
}

function groupNoteIds(group: { notes: NoteListItem[] }): string[] {
  return group.notes.filter((n) => !boundSet.value.has(n.id)).map((n) => n.id)
}

function groupState(group: { notes: NoteListItem[] }): 'none' | 'all' | 'some' {
  const ids = groupNoteIds(group)
  if (!ids.length) return 'none'
  const hit = ids.filter((id) => selected.value.has(id)).length
  if (hit === 0) return 'none'
  return hit === ids.length ? 'all' : 'some'
}

function toggleGroup(group: { notes: NoteListItem[] }) {
  const ids = groupNoteIds(group)
  if (!ids.length) return
  const next = new Set(selected.value)
  if (groupState(group) === 'all') ids.forEach((id) => next.delete(id))
  else ids.forEach((id) => next.add(id))
  selected.value = next
}

function toggleSearchGroup(items: { note_id: string }[]) {
  const ids = items.map((i) => i.note_id).filter((id) => !boundSet.value.has(id))
  if (!ids.length) return
  const next = new Set(selected.value)
  const allIn = ids.every((id) => next.has(id))
  ids.forEach((id) => (allIn ? next.delete(id) : next.add(id)))
  selected.value = next
}

async function toggleExpand(group: GroupState) {
  if (group.expanded) { group.expanded = false; return }
  group.expanded = true
  await loadGroupNotes(group)
}

function confirmBind() {
  if (!canBind.value) return
  busy.value = true
  emit('confirm', selectedIds.value)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}

watch(() => props.visible, (v) => {
  if (!v) {
    selected.value = new Set()
    search.value = ''
    searchResults.value = []
    busy.value = false
    return
  }
  // 每次打开都重拉「已绑 N 处」：缓存跨开合复用会把刚绑好的徽标停在旧值（评审 m8）
  boundCounts.value = {}
  document.addEventListener('keydown', onKeydown)
  void loadGroups()
})

onMounted(() => { if (props.visible) { document.addEventListener('keydown', onKeydown); void loadGroups() } })
onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  if (searchTimer) clearTimeout(searchTimer)
})

/** 父组件在绑定完成后调用：清选择态并重新拉列表。 */
function doneBinding() {
  busy.value = false
  selected.value = new Set()
}
defineExpose({ doneBinding, reload: loadGroups })
</script>

<template>
  <Teleport to="body">
    <Transition name="nbp">
      <div v-if="visible" class="nbp-mask" @mousedown.self="emit('close')">
        <div class="nbp-card" role="dialog" aria-modal="true" aria-label="绑定已有笔记"
             data-testid="note-bind-picker">
          <header class="nbp-head">
            <div class="nbp-head__text">
              <h3 class="nbp-title">绑定已有笔记</h3>
              <p class="nbp-sub">
                {{ taskTitle ? `绑到「${taskTitle}」` : '绑到当前任务' }} ·
                一条笔记可同时属于多个任务，绑定不复制内容
              </p>
            </div>
            <button class="nbp-x" aria-label="关闭" data-testid="note-bind-close" @click="emit('close')">✕</button>
          </header>

          <div class="nbp-search">
            <input v-model="search" class="nbp-input" type="search"
                   data-testid="note-bind-search" placeholder="搜索全部笔记本的笔记标题 / 正文…" />
          </div>

          <div class="nbp-body">
            <div v-if="loading || searching" class="nbp-status">加载中…</div>
            <div v-else-if="error" class="nbp-status nbp-status--err">{{ error }}</div>

            <!-- 搜索态：跨笔记本命中，按笔记本分组 -->
            <template v-else-if="isSearchMode">
              <div v-if="!searchGroups.length" class="nbp-empty">
                <b>没有匹配的笔记</b><span>换个关键词，或回到浏览态按笔记本翻。</span>
              </div>
              <section v-for="(g, gi) in searchGroups" :key="`s-${gi}`" class="nbp-group"
                       :style="{ '--nbp-i': gi }">
                <div class="nbp-group__head">
                  <label class="nbp-check nbp-check--group">
                    <input type="checkbox"
                           :checked="g.items.every(i => selected.has(i.note_id) || boundSet.has(i.note_id))"
                           @change="toggleSearchGroup(g.items)" />
                    <span class="nbp-group__name">{{ g.name }}</span>
                  </label>
                  <span class="nbp-count">{{ g.items.length }}</span>
                </div>
                <label v-for="r in g.items" :key="r.note_id" class="nbp-row"
                       :class="{ 'is-bound': boundSet.has(r.note_id) }" data-testid="note-bind-row">
                  <span class="nbp-check">
                    <input type="checkbox" data-testid="note-bind-check"
                           :checked="selected.has(r.note_id)"
                           :disabled="boundSet.has(r.note_id)"
                           @change="toggle(r.note_id)" />
                  </span>
                  <span class="nbp-row__text">
                    <b class="nbp-row__title">{{ r.title || '未命名笔记' }}</b>
                    <span class="nbp-row__snip">{{ r.content_snippet }}</span>
                  </span>
                  <span class="nbp-row__meta">
                    <span v-if="(boundCounts[r.note_id] || 0) > 0" class="nbp-chip nbp-chip--dash">
                      已绑 {{ boundCounts[r.note_id] }} 处
                    </span>
                    <span v-if="boundSet.has(r.note_id)" class="nbp-chip nbp-chip--on">已绑定</span>
                  </span>
                </label>
              </section>
            </template>

            <!-- 浏览态：笔记本 → 笔记 -->
            <template v-else>
              <div v-if="!groups.length" class="nbp-empty">
                <b>还没有可绑定的笔记</b><span>先到「笔记」里建几条，或在任务里点「+ 新建笔记」。</span>
              </div>
              <section v-for="(g, gi) in filteredGroups" :key="g.notebook.id" class="nbp-group"
                       :style="{ '--nbp-i': gi }">
                <div class="nbp-group__head">
                  <button class="nbp-caret" :aria-label="g.expanded ? '折叠' : '展开'"
                          :data-testid="`note-bind-group-${g.notebook.id}`" @click="toggleExpand(g)">
                    {{ g.expanded ? '▾' : '▸' }}
                  </button>
                  <label class="nbp-check nbp-check--group" @click.stop>
                    <input type="checkbox"
                           :checked="groupState(g) === 'all'"
                           :indeterminate.prop="groupState(g) === 'some'"
                           :disabled="!g.loaded || !groupNoteIds(g).length"
                           @change="toggleGroup(g)" />
                    <span class="nbp-group__name">{{ g.notebook.name }}</span>
                  </label>
                  <span v-if="g.notebook.is_default" class="nbp-chip nbp-chip--dash">默认</span>
                  <span class="nbp-count">{{ g.notebook.note_count }}</span>
                </div>
                <div v-if="g.loading" class="nbp-status nbp-status--sm">加载该笔记本…</div>
                <template v-else-if="g.expanded">
                  <div v-if="!g.notes.length" class="nbp-status nbp-status--sm">这个笔记本是空的。</div>
                  <label v-for="n in g.notes" :key="n.id" class="nbp-row"
                         :class="{ 'is-bound': boundSet.has(n.id) }" data-testid="note-bind-row">
                    <span class="nbp-check">
                      <input type="checkbox" data-testid="note-bind-check"
                             :checked="selected.has(n.id)"
                             :disabled="boundSet.has(n.id)"
                             @change="toggle(n.id)" />
                    </span>
                    <span class="nbp-row__text">
                      <b class="nbp-row__title">{{ n.title || '未命名笔记' }}</b>
                      <span class="nbp-row__snip">{{ n.content_preview }}</span>
                    </span>
                    <span class="nbp-row__meta">
                      <span v-if="(boundCounts[n.id] || 0) > 0" class="nbp-chip nbp-chip--dash">
                        已绑 {{ boundCounts[n.id] }} 处
                      </span>
                      <span v-if="boundSet.has(n.id)" class="nbp-chip nbp-chip--on">已绑定</span>
                    </span>
                  </label>
                </template>
              </section>
            </template>
          </div>

          <footer class="nbp-foot">
            <span class="nbp-foot__count">已选 <b>{{ selectedCount }}</b> 条</span>
            <div class="nbp-foot__actions">
              <button class="nbp-btn" @click="emit('close')">取消</button>
              <button class="nbp-btn nbp-btn--primary" :disabled="!canBind"
                      data-testid="note-bind-confirm" @click="confirmBind">
                {{ busy ? '绑定中…' : `绑定所选${selectedCount ? `（${selectedCount}）` : ''}` }}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.nbp-mask {
  position: fixed; inset: 0; z-index: 10006; display: flex; align-items: center; justify-content: center;
  padding: 20px; background: var(--overlay-scrim, rgba(0, 0, 0, .35));
}
/* 同心圆角：卡片 16px，内层行/输入 8px + 8px 内边距 */
.nbp-card {
  width: 100%; max-width: 560px; max-height: min(78vh, 680px); display: flex; flex-direction: column;
  background: var(--surface-panel-strong, var(--color-white)); color: var(--color-text);
  border-radius: 16px; overflow: hidden;
  box-shadow: 0 1px 2px rgba(15, 23, 42, .06), 0 8px 24px rgba(15, 23, 42, .12), 0 24px 56px rgba(15, 23, 42, .18);
}
.nbp-head { display: flex; align-items: flex-start; gap: 12px; padding: 16px 16px 10px; }
.nbp-head__text { min-width: 0; flex: 1; }
.nbp-title { margin: 0; font-size: 16px; font-weight: 800; letter-spacing: -.01em; text-wrap: balance; }
.nbp-sub { margin: 4px 0 0; font-size: 12.5px; line-height: 1.5; color: var(--color-text-light); text-wrap: pretty; }
.nbp-x {
  width: 28px; height: 28px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border: 0; background: transparent; color: var(--color-text-light); cursor: pointer; border-radius: 8px;
  font-size: 13px; transition-property: background-color, color, transform; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.nbp-x:hover { background: var(--color-hover, rgba(0, 0, 0, .05)); color: var(--color-text); }
.nbp-x:active { transform: scale(.96); }
.nbp-search { padding: 0 16px 10px; }
.nbp-input {
  width: 100%; box-sizing: border-box; border: 1px solid var(--panel-border); border-radius: 8px;
  background: var(--surface-input, var(--color-white)); color: var(--color-text);
  padding: 9px 12px; font-size: 13.5px; font-family: inherit;
  transition-property: border-color, box-shadow; transition-duration: .15s;
}
.nbp-input:focus {
  outline: none; border-color: var(--color-primary);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary) 18%, transparent);
}
.nbp-body { flex: 1; min-height: 0; overflow-y: auto; padding: 0 8px 8px; overscroll-behavior: contain; }
.nbp-status { padding: 18px; text-align: center; color: var(--color-text-light); font-size: 13px; }
.nbp-status--sm { padding: 8px 12px; font-size: 12px; }
.nbp-status--err { color: var(--color-error); }
.nbp-empty {
  margin: 8px; padding: 24px; text-align: center; border: 1px dashed var(--panel-border);
  border-radius: 12px; color: var(--color-text-light); font-size: 13px;
}
.nbp-empty b { display: block; color: var(--color-text); font-size: 14px; margin-bottom: 4px; }
.nbp-group { margin: 4px 0 10px; }
.nbp-group__head {
  display: flex; align-items: center; gap: 4px; padding: 4px 8px; border-radius: 8px;
  background: var(--surface-panel-subtle, rgba(0, 0, 0, .03));
}
.nbp-caret {
  width: 28px; height: 28px; flex: none; border: 0; background: transparent; cursor: pointer;
  color: var(--color-text-light); font-size: 11px; border-radius: 8px; display: inline-flex;
  align-items: center; justify-content: center;
  transition-property: background-color, transform; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.nbp-caret:hover { background: var(--color-hover, rgba(0, 0, 0, .05)); }
.nbp-caret:active { transform: scale(.96); }
.nbp-group__name { font-weight: 800; font-size: 13px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nbp-count {
  margin-left: auto; font-size: 11.5px; color: var(--color-text-light);
  font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1;
}
.nbp-check { display: inline-flex; align-items: center; gap: 8px; min-width: 0; cursor: pointer; }
.nbp-check--group { flex: 1; min-width: 0; padding: 6px 0; }
.nbp-check input[type=checkbox] { width: 15px; height: 15px; flex: none; accent-color: var(--color-primary); cursor: pointer; }
.nbp-check input[type=checkbox]:disabled { cursor: not-allowed; }
.nbp-row {
  display: flex; align-items: flex-start; gap: 10px; padding: 8px; margin: 2px 0; border-radius: 8px;
  cursor: pointer; transition-property: background-color, opacity; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.nbp-row:hover { background: var(--color-hover, rgba(0, 0, 0, .04)); }
.nbp-row.is-bound { opacity: .55; cursor: not-allowed; }
.nbp-row__check-pad { padding: 6px 0; }
.nbp-row__text { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 2px; }
.nbp-row__title { font-size: 13.5px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nbp-row__snip {
  font-size: 12px; color: var(--color-text-light); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; text-wrap: pretty;
}
.nbp-row__meta { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; max-width: 130px; }
.nbp-chip {
  display: inline-flex; align-items: center; padding: 1px 8px; font-size: 11px; font-weight: 700;
  border-radius: 999px; white-space: nowrap; font-variant-numeric: tabular-nums;
  border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
}
.nbp-chip--dash { border-style: dashed; color: var(--color-text-light); background: transparent; }
.nbp-chip--on { background: var(--color-primary); border-color: var(--color-primary); color: var(--color-white, #fff); }
.nbp-foot {
  display: flex; align-items: center; gap: 12px; padding: 12px 16px; flex-wrap: wrap;
  border-top: 1px solid var(--panel-border); background: var(--surface-panel-subtle, rgba(0, 0, 0, .02));
}
.nbp-foot__count { font-size: 12.5px; color: var(--color-text-light); font-variant-numeric: tabular-nums; }
.nbp-foot__count b { color: var(--color-text); font-size: 14px; }
.nbp-foot__actions { margin-left: auto; display: flex; gap: 8px; }
.nbp-btn {
  display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 6px 14px;
  border: 1px solid var(--panel-border); border-radius: 8px; background: var(--surface-panel, var(--color-white));
  color: var(--color-text); font-weight: 700; font-size: 13px; cursor: pointer; font-family: inherit;
  transition-property: background-color, border-color, transform, opacity; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.nbp-btn:hover:not(:disabled) { background: var(--color-hover, rgba(0, 0, 0, .04)); }
.nbp-btn:active:not(:disabled) { transform: scale(.96); }
.nbp-btn:disabled { opacity: .5; cursor: not-allowed; }
.nbp-btn--primary { background: var(--color-primary); border-color: var(--color-primary); color: var(--color-white, #fff); }
.nbp-btn--primary:hover:not(:disabled) { filter: brightness(1.05); background: var(--color-primary); }

/* 入场：遮罩淡入 + 卡片轻微上浮缩放；分组按序错峰进入 */
.nbp-enter-active { transition-property: opacity; transition-duration: .18s; transition-timing-function: cubic-bezier(.2, 0, 0, 1); }
.nbp-enter-active .nbp-card { transition-property: opacity, transform; transition-duration: .22s; transition-timing-function: cubic-bezier(.16, 1, .3, 1); }
.nbp-enter-from { opacity: 0; }
.nbp-enter-from .nbp-card { opacity: 0; transform: translateY(8px) scale(.985); }
.nbp-leave-active { transition-property: opacity; transition-duration: .14s; transition-timing-function: cubic-bezier(.4, 0, 1, 1); }
.nbp-leave-active .nbp-card { transition-property: opacity, transform; transition-duration: .14s; }
.nbp-leave-to { opacity: 0; }
.nbp-leave-to .nbp-card { opacity: 0; transform: translateY(4px) scale(.99); }
.nbp-group { animation: nbp-in .26s cubic-bezier(.16, 1, .3, 1) both; animation-delay: calc(var(--nbp-i, 0) * 40ms); }
@keyframes nbp-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  .nbp-group { animation: none; }
  .nbp-enter-active .nbp-card, .nbp-leave-active .nbp-card { transition-duration: .01s; }
}
@media (max-width: 767px) {
  .nbp-mask { padding: 0; }
  .nbp-card { max-width: none; max-height: 100%; height: 100%; border-radius: 0; }
}
</style>
