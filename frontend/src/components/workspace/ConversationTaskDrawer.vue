<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<script setup lang="ts">
/**
 * 会话侧任务卡片（S7 · 2026-11-16 Wave B 由右侧全高抽屉改为居中卡片）。
 *
 * 抽屉的问题：420px 全高贴右边，内容是一条纵向长列表，文件缩略被压成 2 列小方块，
 * 且和聊天页正文抢视线。卡片化后：宽度受限、高度自适应内容、遮罩明确「这是一次问答」，
 * 关闭方式补齐（✕ / 遮罩 / Esc）。
 *
 * 类名 `.ctd-drawer` / `.ctd-icon` 与「本会话的任务」「本会话产出」两处文案是既有
 * E2E 契约（e2e/conversation_task_drawer.spec.ts），保留不改。
 *
 * 数据面：`workspaceScope.snapshot`（当前绑定）+ GET /api/workspace/tasks/{id}/files?conversation_id
 * + GET /api/workspace/tasks/{id}/notes。与任务详情共用同一数据源，不是复制。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useWorkspaceScopeStore } from '@/stores/workspaceScope'
import {
  apiErrorMessage,
  fetchTaskFiles,
  fetchTaskNotes,
  setNoteTaskBinding,
  type TaskFileEntry,
  type TaskNoteRow,
} from '@/api/workspace'
import { classifyFile, fileIcon, formatSize } from '@/composables/filePreview'
import { buildDownloadUrl } from '@/api/workspaceFiles'
import { notesApi } from '@/api/notes'
import { renderMarkdownToHtml } from '@/composables/useMarkdown'
import FilePreviewDialog from '../FilePreviewDialog.vue'
import WorkspaceBindingDialog from '../WorkspaceBindingDialog.vue'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ close: [] }>()

const chatStore = useChatStore()
const scope = useWorkspaceScopeStore()

const produced = ref<TaskFileEntry[]>([])
const notes = ref<TaskNoteRow[]>([])
const loading = ref(false)
const error = ref('')
const busyNote = ref('')
const rebindOpen = ref(false)
// 单击预览（2026-11-16 Wave C）：文件走 FilePreviewDialog，笔记走卡内 markdown 浮层。
const previewFile = ref<TaskFileEntry | null>(null)
const previewNote = ref<{ id: string; title: string; html: string } | null>(null)
const noteBusy = ref('')
let seq = 0

/**
 * 本会话产出按文件夹分组（2026-11-17 用户反馈 4）：产出都在 `artifacts/` 下时，
 * 平铺会让「一个文件夹里的 N 个文件」看起来像 N 个互不相关的条目。分组后
 * 文件夹一行（可折叠，默认展开）+ 组内文件；只有一个根组时不渲染组头（零变化）。
 */
const collapsedGroups = ref<Set<string>>(new Set())
function dirOf(relPath: string): string {
  const idx = relPath.lastIndexOf('/')
  return idx > 0 ? relPath.slice(0, idx) : ''
}
const producedGroups = computed(() => {
  const map = new Map<string, TaskFileEntry[]>()
  for (const e of produced.value) {
    const key = dirOf(e.rel_path || '')
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(e)
  }
  return Array.from(map.entries())
    .map(([dir, files]) => ({ dir, files }))
    .sort((a, b) => a.dir.localeCompare(b.dir, 'zh-Hans-CN'))
})
const showGroupHeaders = computed(
  () => producedGroups.value.length > 1 || producedGroups.value.some((g) => !!g.dir),
)
function isGroupCollapsed(dir: string): boolean {
  return collapsedGroups.value.has(dir)
}
function toggleGroup(dir: string) {
  const next = new Set(collapsedGroups.value)
  if (next.has(dir)) next.delete(dir)
  else next.add(dir)
  collapsedGroups.value = next
}

const task = computed(() => scope.snapshot?.task || null)
const projectName = computed(() => scope.snapshot?.project?.name || '')
const conversationId = computed(() => chatStore.currentConversationId || '')

async function load() {
  if (!task.value) {
    produced.value = []
    notes.value = []
    return
  }
  const my = ++seq
  loading.value = true
  error.value = ''
  try {
    const [files, taskNotes] = await Promise.all([
      fetchTaskFiles(task.value.id, conversationId.value || undefined),
      fetchTaskNotes(task.value.id),
    ])
    if (my !== seq) return
    produced.value = files.produced || []
    notes.value = taskNotes
  } catch (e: any) {
    if (my !== seq) return
    error.value = apiErrorMessage(e, '加载失败')
  } finally {
    if (my === seq) loading.value = false
  }
}

watch(
  () => [props.visible, task.value?.id, conversationId.value],
  ([v]) => {
    if (v) void load()
  },
  { immediate: true },
)

watch(() => props.visible, (v) => {
  if (typeof document === 'undefined') return
  document.removeEventListener('keydown', onKeydown)
  if (v) document.addEventListener('keydown', onKeydown)
})

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (previewNote.value) previewNote.value = null
  else if (previewFile.value) previewFile.value = null
  else if (rebindOpen.value) rebindOpen.value = false
  else emit('close')
}

// 文件预览路径：produced.rel_path 是「**产出所在任务** workspace 根」相对。跨任务视图里
// 条目可能来自别的任务（改绑后旧任务产出），故优先用条目自带的 display_root；单任务/
// 目录列表条目无 display_root 时回落当前任务根。绝不混用「当前任务根 + 他任务 rel_path」
// （会静默预览到同名另一文件的字节）。
function previewFilePath(e: TaskFileEntry): string {
  const root = e.display_root || task.value?.display_root || ''
  return root ? `${root}/${e.rel_path}` : e.rel_path
}
function openFilePreview(e: TaskFileEntry) {
  previewFile.value = e
}
// 仅当焦点在卡片本身（非内部「解绑」按钮）才响应键盘激活（评审 Important-2）。
function onActivate(e: KeyboardEvent, fn: () => void) {
  if (e.target !== e.currentTarget) return
  e.preventDefault()
  fn()
}
async function openNotePreview(n: TaskNoteRow) {
  if (noteBusy.value) return
  noteBusy.value = n.id
  try {
    const note = await notesApi.getNote(n.id)
    previewNote.value = {
      id: note.id,
      title: note.title || n.title || '未命名笔记',
      html: renderMarkdownToHtml(note.content || '', { conversationId: conversationId.value || null }),
    }
  } catch {
    // 失败只在预览浮层内提示，不污染 error.value（否则整张卡被错误态顶掉，评审 Minor-1）
    previewNote.value = { id: n.id, title: n.title || '未命名笔记', html: '<p>笔记预览加载失败，请重试。</p>' }
  } finally {
    noteBusy.value = ''
  }
}

async function unbindNote(id: string) {
  if (busyNote.value) return
  busyNote.value = id
  try {
    // 多对多：只解本任务这一条关系，笔记在别处的绑定不动、笔记本身不删
    await setNoteTaskBinding(id, task.value?.id || null, 'unbind')
    await load()
  } catch (e: any) {
    error.value = apiErrorMessage(e, '解绑失败')
  } finally {
    busyNote.value = ''
  }
}

function kindIcon(e: TaskFileEntry) {
  return fileIcon(classifyFile(e.name, e.type))
}

// 跨任务「本会话产出」可能有两任务同名文件（rel_path 相同）→ 组合 key 防 Vue 重复键。
function fileKey(e: TaskFileEntry, i: number): string {
  return e.task_id ? `${e.task_id}:${e.rel_path}` : `${i}:${e.rel_path}`
}
// 条目来自别的任务（改绑后旧任务产出）时显示来源任务名，消歧同名文件。
function originLabel(e: TaskFileEntry): string {
  const cur = task.value?.id || ''
  return e.task_id && e.task_id !== cur ? (e.task_title || '其他任务') : ''
}

// 卡片开着时组件被卸载（切会话 / 路由变化）也要摘掉 Esc 监听（评审 m7）
onUnmounted(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <Transition name="ctd">
      <div v-if="visible" class="ctd-mask" @mousedown.self="emit('close')">
        <section class="ctd-drawer" role="dialog" aria-modal="true" aria-label="本会话的任务"
                 data-testid="conversation-task-card">
          <header class="ctd-head">
            <div class="ctd-head__main">
              <div class="ctd-eyebrow">
                <span>本会话的任务</span>
                <span v-if="task" class="ctd-chip">{{ task.status === 'active' ? '进行中' : task.status }}</span>
              </div>
              <h4 class="ctd-title" :title="task?.title || ''">{{ task?.title || '未绑定任务' }}</h4>
              <div v-if="task && projectName" class="ctd-sub">{{ projectName }} · 项目</div>
            </div>
            <button class="ctd-icon" aria-label="关闭" @click="emit('close')">✕</button>
          </header>

          <div class="ctd-body">
            <div v-if="!task" class="ctd-empty">
              <b>本会话尚未绑定任务</b>
              <span>发送一条消息后会自动进入默认项目；也可在任务管理里显式归类。</span>
            </div>

            <template v-else>

              <div v-if="loading" class="ctd-status">加载中…</div>
              <div v-else-if="error" class="ctd-status ctd-status--err">{{ error }}</div>

              <section v-else class="ctd-section">
                <div class="ctd-section__head">
                  <b>本会话产出 · <span class="ctd-tnum">{{ produced.length }}</span></b>
                  <span v-if="produced.length" class="ctd-section__hint">点击文件可预览</span>
                </div>
                <div v-if="!produced.length" class="ctd-empty ctd-empty--sm">
                  本会话还没有产出（agent 发布的交付文件会出现在这里）。
                </div>
                <div v-else class="ctd-groups">
                  <section v-for="g in producedGroups" :key="g.dir || '__root__'" class="ctd-group">
                    <button v-if="showGroupHeaders" type="button" class="ctd-group__head"
                            data-testid="ctd-folder-group"
                            :aria-expanded="!isGroupCollapsed(g.dir)"
                            @click="toggleGroup(g.dir)">
                      <span class="ctd-group__fold" aria-hidden="true">{{ isGroupCollapsed(g.dir) ? '▸' : '▾' }}</span>
                      <span class="ctd-group__icon" aria-hidden="true">📁</span>
                      <span class="ctd-group__name">{{ g.dir || '根目录' }}</span>
                      <span class="ctd-group__count ctd-tnum">{{ g.files.length }}</span>
                    </button>
                    <div v-show="!isGroupCollapsed(g.dir)" class="ctd-grid">
                      <div v-for="(e, ei) in g.files" :key="fileKey(e, ei)" class="ctd-tile ctd-tile--click"
                           :style="{ '--ctd-i': ei }"
                           role="button" tabindex="0" :title="`预览 ${e.name}`"
                           :aria-label="`预览 ${e.name}`"
                           data-testid="ctd-file-tile"
                           @click="openFilePreview(e)"
                           @keydown.enter="onActivate($event, () => openFilePreview(e))"
                           @keydown.space="onActivate($event, () => openFilePreview(e))">
                        <div class="ctd-thumb">
                          <span>{{ kindIcon(e) }}</span>
                          <span class="ctd-thumb__peek" aria-hidden="true">预览</span>
                        </div>
                        <div class="ctd-tn" :title="e.rel_path">{{ e.name }}</div>
                        <div v-if="originLabel(e)" class="ctd-tfrom" :title="`来自任务：${originLabel(e)}`">↳ {{ originLabel(e) }}</div>
                        <div class="ctd-tm">{{ formatSize(e.size) }}</div>
                      </div>
                    </div>
                  </section>
                </div>
              </section>

              <section v-if="!loading && !error" class="ctd-section">
                <div class="ctd-section__head">
                  <b>任务笔记 · <span class="ctd-tnum">{{ notes.length }}</span></b>
                </div>
                <div v-if="!notes.length" class="ctd-empty ctd-empty--sm">任务还没有笔记。</div>
                <div v-else class="ctd-notelist">
                  <div v-for="(n, ni) in notes" :key="n.id" class="ctd-crow ctd-crow--click"
                       :style="{ '--ctd-i': ni }"
                       role="button" tabindex="0" :title="`预览 ${n.title || '未命名笔记'}`"
                       data-testid="ctd-note-row"
                       @click="openNotePreview(n)"
                       @keydown.enter="onActivate($event, () => openNotePreview(n))"
                       @keydown.space="onActivate($event, () => openNotePreview(n))">
                    <span class="ctd-crow__t">{{ n.title || '未命名笔记' }}</span>
                    <span v-if="(n.bound_task_count || 1) > 1" class="ctd-chip ctd-chip--multi">
                      共绑 {{ n.bound_task_count }} 个任务
                    </span>
                    <span v-else class="ctd-chip">已绑定</span>
                    <button class="ctd-btn ctd-btn--sm" :disabled="busyNote === n.id"
                            @click.stop="unbindNote(n.id)">解绑</button>
                  </div>
                </div>
              </section>

              <div class="ctd-foot">
                <button class="ctd-btn ctd-btn--sm" @click="rebindOpen = true">改绑到其他任务…</button>
              </div>
            </template>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>

  <WorkspaceBindingDialog v-if="rebindOpen" @close="rebindOpen = false" />

  <!-- 文件预览（复用任务详情同一 FilePreviewDialog）-->
  <FilePreviewDialog
    v-if="previewFile"
    :filename="previewFile.name"
    :url="buildDownloadUrl(previewFilePath(previewFile), null, conversationId || null)"
    :rel-path="previewFile.rel_path"
    :source-path="previewFilePath(previewFile)"
    :type="previewFile.type"
    :conversation-id="conversationId || null"
    :z-index="10010"
    @close="previewFile = null"
  />

  <!-- 笔记预览浮层（卡内渲染 markdown，不打断会话上下文）-->
  <Teleport to="body">
    <div v-if="previewNote" class="ctd-note-mask" @mousedown.self="previewNote = null">
      <section class="ctd-note-card" role="dialog" aria-modal="true"
               :aria-label="previewNote.title" data-testid="ctd-note-preview">
        <header class="ctd-note-head">
          <span class="ctd-note-title" :title="previewNote.title">{{ previewNote.title }}</span>
          <button class="ctd-icon" aria-label="关闭" @click="previewNote = null">✕</button>
        </header>
        <div class="ctd-note-body markdown-body" v-html="previewNote.html"></div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.ctd-mask {
  position: fixed; inset: 0; z-index: 9997; display: flex; align-items: center; justify-content: center;
  padding: 20px; background: var(--overlay-scrim, rgba(0, 0, 0, .35));
}
/* 卡片：宽度受限 + 高度随内容（上限 84vh 后内部滚动）。
   同心圆角：卡片 16px，内层任务卡/缩略块 8px + 10px 内边距。 */
.ctd-drawer {
  width: 100%; max-width: 520px; max-height: min(84vh, 760px); display: flex; flex-direction: column;
  background: var(--surface-panel-strong, var(--color-white)); color: var(--color-text);
  border-radius: 16px; overflow: hidden;
  box-shadow: 0 1px 2px rgba(15, 23, 42, .06), 0 8px 24px rgba(15, 23, 42, .12), 0 24px 56px rgba(15, 23, 42, .18);
}
/* 头部只留一层信息：眉题（这是什么）+ 任务名（是哪一个）+ 所属项目。
   原来这里还嵌了一张卡（项目眉题 / 任务名 / 两个计数芯片），计数在下面两节
   标题里又各写一遍，右上聊天页也已经显示「项目 · 任务」——四层同级文字挤在
   200px 里，就是「顶部拥挤」的根因。 */
.ctd-head {
  display: flex; align-items: flex-start; gap: 12px; flex: none;
  padding: 18px 18px 14px; border-bottom: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle, rgba(0, 0, 0, .02));
}
.ctd-head__main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.ctd-eyebrow {
  display: flex; align-items: center; gap: 8px; min-width: 0;
  font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
  color: var(--color-text-light);
}
.ctd-title {
  margin: 0; font-size: 18px; font-weight: 800; letter-spacing: -.01em; line-height: 1.25;
  text-wrap: balance; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ctd-sub { font-size: 12px; color: var(--color-text-light); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctd-body { flex: 1; min-height: 0; overflow: auto; padding: 16px 18px 18px; display: flex; flex-direction: column; gap: 18px; overscroll-behavior: contain; }
.ctd-section__head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.ctd-section__head b { font-size: 12px; letter-spacing: .06em; }
.ctd-section__hint { margin-left: auto; font-size: 11px; color: var(--color-text-light); }
.ctd-tnum { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1; }
.ctd-groups { display: flex; flex-direction: column; gap: 10px; }
.ctd-group { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.ctd-group__head {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 5px 8px;
  border: 1px dashed var(--panel-border); border-radius: 8px; background: transparent;
  color: var(--color-text); font-family: inherit; font-size: 12px; font-weight: 700;
  cursor: pointer; text-align: left; min-width: 0;
}
.ctd-group__head:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.ctd-group__fold { width: 10px; flex: none; font-size: 10px; color: var(--color-text-light); }
.ctd-group__icon { flex: none; font-size: 12px; }
.ctd-group__name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctd-group__count { margin-left: auto; font-size: 11px; color: var(--color-text-light); }
.ctd-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
.ctd-tile {
  border-radius: 8px; padding: 8px; display: flex; flex-direction: column; gap: 4px; min-width: 0;
  background: var(--surface-panel, var(--color-white));
  box-shadow: 0 0 0 1px rgba(0, 0, 0, .06), 0 1px 2px rgba(15, 23, 42, .05);
}
.ctd-thumb {
  position: relative;
  height: 40px; display: flex; align-items: center; justify-content: center;
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 11px; font-weight: 800;
  border-radius: 6px; background: var(--surface-panel-subtle, var(--color-bg-secondary));
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .08);
}
/* 可预览的可发现性（手机没有 hover）：缩略块右下角常驻「预览」角标 */
.ctd-thumb__peek {
  position: absolute; right: 3px; bottom: 3px; padding: 0 4px;
  border-radius: 4px; background: var(--color-primary); color: var(--color-white);
  font-family: var(--font-main); font-size: 9px; font-weight: 700; line-height: 1.5;
  letter-spacing: .02em;
}
.ctd-tn { font-size: 12px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctd-tm { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 10.5px; color: var(--color-text-light); font-variant-numeric: tabular-nums; }
.ctd-tfrom { font-size: 10.5px; color: var(--color-text-light); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctd-notelist { display: flex; flex-direction: column; gap: 6px; }
.ctd-crow {
  display: flex; align-items: center; gap: 8px; padding: 7px 9px; border-radius: 8px;
  background: var(--surface-panel, var(--color-white));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--panel-border) 70%, transparent);
}
.ctd-crow__t { font-weight: 700; font-size: 13px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctd-crow .ctd-btn { margin-left: auto; }
/* 单击预览的可点性提示（卡片整体可点，hover/键盘聚焦给反馈）*/
.ctd-tile--click, .ctd-crow--click { cursor: pointer; transition: box-shadow .15s cubic-bezier(.2,0,0,1), transform .15s cubic-bezier(.2,0,0,1); }
.ctd-tile--click:hover, .ctd-crow--click:hover { box-shadow: 0 0 0 1px var(--color-primary), 0 2px 8px rgba(15,23,42,.10); }
.ctd-tile--click:active, .ctd-crow--click:active { transform: scale(.985); }
.ctd-tile--click:focus-visible, .ctd-crow--click:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 1px; }
.ctd-foot { border-top: 1px solid var(--panel-border); padding-top: 12px; display: flex; gap: 6px; }
.ctd-empty {
  padding: 20px; text-align: center; color: var(--color-text-light); font-size: 12.5px;
  border: 1px dashed var(--panel-border); border-radius: 10px; text-wrap: pretty;
}
.ctd-empty b { display: block; color: var(--color-text); font-size: 14px; margin-bottom: 4px; }
.ctd-empty--sm { padding: 12px; }
.ctd-status { padding: 16px; text-align: center; color: var(--color-text-light); }
.ctd-status--err { color: var(--color-error); }
.ctd-btn {
  position: relative; display: inline-flex; align-items: center; gap: 6px; min-height: 28px;
  padding: 6px 12px; border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
  color: var(--color-text); font-weight: 700; font-size: 13px; cursor: pointer; border-radius: 8px;
  font-family: inherit;
  transition-property: background-color, border-color, color, transform; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.ctd-btn:hover:not(:disabled) { background: var(--color-hover, var(--surface-panel-subtle)); }
.ctd-btn:active:not(:disabled) { transform: scale(.96); }
.ctd-btn:disabled { opacity: .5; cursor: not-allowed; }
.ctd-btn--sm { padding: 3px 8px; font-size: 12px; }
.ctd-btn--sm::after {
  content: ''; position: absolute; top: 50%; left: 50%;
  width: max(100%, 40px); height: max(100%, 40px); transform: translate(-50%, -50%);
}
.ctd-icon {
  position: relative; width: 28px; height: 28px; flex: none; margin-top: -3px;
  display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--panel-border); background: var(--surface-panel, var(--color-white));
  color: var(--color-text); cursor: pointer; border-radius: 8px; font-size: 12px;
  transition-property: background-color, color, transform; transition-duration: .15s;
  transition-timing-function: cubic-bezier(.2, 0, 0, 1);
}
.ctd-icon::after {
  content: ''; position: absolute; top: 50%; left: 50%;
  width: max(100%, 40px); height: max(100%, 40px); transform: translate(-50%, -50%);
}
.ctd-icon:hover { background: var(--color-hover, var(--surface-panel-subtle)); }
.ctd-icon:active { transform: scale(.96); }
.ctd-chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 1px 8px; border: 1px solid var(--panel-border);
  font-size: 11.5px; font-weight: 700; border-radius: 999px; background: var(--surface-panel-subtle, var(--color-bg-secondary));
  white-space: nowrap; font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1;
}
.ctd-chip--multi { border-style: dashed; border-color: var(--color-primary); color: var(--color-primary); background: transparent; }

/* 入场：遮罩淡入 + 卡片上浮缩放；列表项错峰进入 */
.ctd-enter-active { transition-property: opacity; transition-duration: .18s; transition-timing-function: cubic-bezier(.2, 0, 0, 1); }
.ctd-enter-active .ctd-drawer { transition-property: opacity, transform; transition-duration: .24s; transition-timing-function: cubic-bezier(.16, 1, .3, 1); }
.ctd-enter-from { opacity: 0; }
.ctd-enter-from .ctd-drawer { opacity: 0; transform: translateY(10px) scale(.98); }
.ctd-leave-active { transition-property: opacity; transition-duration: .14s; }
.ctd-leave-active .ctd-drawer { transition-property: opacity, transform; transition-duration: .14s; }
.ctd-leave-to { opacity: 0; }
.ctd-leave-to .ctd-drawer { opacity: 0; transform: translateY(4px) scale(.995); }
.ctd-tile, .ctd-crow { animation: ctd-in .24s cubic-bezier(.16, 1, .3, 1) both; animation-delay: calc(var(--ctd-i, 0) * 40ms); }
@keyframes ctd-in { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
/* 笔记预览浮层：层级高于本卡遮罩（9997），卡内只读渲染 markdown */
.ctd-note-mask {
  position: fixed; inset: 0; z-index: 10010; display: flex; align-items: center; justify-content: center;
  padding: 24px; background: var(--overlay-scrim, rgba(0, 0, 0, .35));
}
.ctd-note-card {
  width: 100%; max-width: 680px; max-height: min(82vh, 760px); display: flex; flex-direction: column;
  background: var(--surface-panel-strong, var(--color-white)); color: var(--color-text);
  border: 1px solid var(--panel-border); border-radius: 14px; overflow: hidden;
  box-shadow: 0 1px 2px rgba(15,23,42,.06), 0 12px 32px rgba(15,23,42,.18);
}
.ctd-note-head {
  display: flex; align-items: center; gap: 10px; flex: none; padding: 14px 16px;
  border-bottom: 1px solid var(--panel-border); background: var(--surface-panel-subtle, rgba(0,0,0,.02));
}
.ctd-note-title { font-size: 15px; font-weight: 800; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.ctd-note-body { flex: 1; min-height: 0; overflow: auto; padding: 16px 18px; font-size: 14px; line-height: 1.7; word-break: break-word; }
.ctd-note-body :deep(h1), .ctd-note-body :deep(h2), .ctd-note-body :deep(h3) { margin: .8em 0 .4em; line-height: 1.3; }
.ctd-note-body :deep(h1) { font-size: 1.5em; border-bottom: 1px solid var(--color-border); padding-bottom: 6px; }
.ctd-note-body :deep(h2) { font-size: 1.3em; }
.ctd-note-body :deep(h3) { font-size: 1.15em; }
.ctd-note-body :deep(p) { margin: .5em 0; }
.ctd-note-body :deep(ul), .ctd-note-body :deep(ol) { margin: .5em 0; padding-left: 1.4em; }
.ctd-note-body :deep(li) { margin: .2em 0; }
.ctd-note-body :deep(blockquote) { margin: .6em 0; padding: .2em .9em; border-left: 3px solid var(--color-border); color: var(--color-text-light); }
.ctd-note-body :deep(code) { background: var(--surface-panel-subtle, rgba(0,0,0,.05)); padding: .1em .35em; border-radius: 4px; font-size: .92em; }
.ctd-note-body :deep(pre) { background: var(--surface-panel-subtle, rgba(0,0,0,.05)); padding: 10px 12px; border-radius: 8px; overflow: auto; }
/* 围栏代码块底色归 `.code-block` 外壳（皮肤近白字压浅色底=不可读，2026-11-18 同族缺陷） */
.ctd-note-body :deep(.code-block pre) { background: transparent; border: 0; border-radius: 0; margin: 0; padding: 10px 12px; }
.ctd-note-body :deep(pre code) { background: none; padding: 0; }
.ctd-note-body :deep(table) { border-collapse: collapse; width: 100%; margin: .6em 0; }
.ctd-note-body :deep(th), .ctd-note-body :deep(td) { border: 1px solid var(--color-border); padding: 6px 9px; text-align: left; }
.ctd-note-body :deep(img) { max-width: 100%; border-radius: 6px; }
.ctd-note-body :deep(hr) { border: 0; border-top: 1px solid var(--color-border); margin: 1em 0; }
@media (prefers-reduced-motion: reduce) {
  .ctd-tile, .ctd-crow { animation: none; }
  .ctd-enter-active .ctd-drawer, .ctd-leave-active .ctd-drawer { transition-duration: .01s; }
}
@media (max-width: 767px) {
  /* 2026-11-17（真机反馈）：全屏卡片必须避让系统栏。padding 为 0 时卡片头
     （含 ✕）落在状态栏下面，点击被系统手势接管 → 「关不掉」。 */
  .ctd-mask { padding: var(--safe-top, 0px) 0 var(--safe-bottom, 0px); }
  .ctd-drawer { max-width: none; max-height: 100%; height: 100%; border-radius: 0; }
}
</style>
