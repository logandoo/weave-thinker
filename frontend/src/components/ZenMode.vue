<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="zen-layout">
    <!-- Left drawer: conversation history (independent from main sidebar) -->
    <ZenHistoryDrawer
      :visible="leftDrawerOpen"
      @close="leftDrawerOpen = false"
      @select-conversation="leftDrawerOpen = false"
    />

    <!-- Left panel: Chat -->
    <div class="zen-left-panel">
      <div class="zen-panel-header">
        <button class="zen-drawer-toggle" @click="leftDrawerOpen = true" title="Agent历史">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="3" y1="12" x2="21" y2="12"/>
            <line x1="3" y1="6" x2="21" y2="6"/>
            <line x1="3" y1="18" x2="21" y2="18"/>
          </svg>
        </button>
        <span class="zen-panel-title zen-panel-title-center">Agent</span>
        <div class="zen-panel-actions">
        </div>
      </div>
      <div class="zen-panel-body">
        <ChatArea zen />
      </div>
    </div>

    <!-- Resizer -->
    <div class="zen-resizer" @mousedown="startResize"></div>

    <!-- Right panel: Note -->
    <div class="zen-right-panel" :style="{ width: rightPanelWidth + 'px' }">
      <div class="zen-panel-header">
        <span class="zen-panel-title zen-panel-title-center">笔记</span>
        <div class="zen-panel-actions">
          <button class="zen-drawer-toggle" @click="rightDrawerOpen = true" title="笔记列表">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="zen-panel-body">
        <ZenNotePanel
          ref="zenNotePanelRef"
          :notebook-id="zenStore.currentNotebookId"
          :note-id="zenStore.currentNoteId"
          @select-note="handleSelectNote"
        />
      </div>
    </div>

    <!-- Right drawer: notes list -->
    <ZenNotesDrawer
      :visible="rightDrawerOpen"
      @close="rightDrawerOpen = false"
      @select-note="handleSelectNoteFromDrawer"
    />

    <!-- Zen exit dropdown -->
    <Teleport to="body">
      <div class="zen-exit-dropdown" :class="{ expanded: showZenExitMenu }">
        <Transition name="zen-curtain">
          <div v-if="showZenExitMenu" class="zen-exit-menu" :class="{ 'is-fresh': menuFresh }">
            <button
              class="zen-menu-btn"
              data-testid="zen-projects-btn"
              @click="showProjectsDialog = true; showZenExitMenu = false"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              </svg>
              <span>项目管理</span>
            </button>
            <button class="zen-exit-btn" @click="exitZenMode(); showZenExitMenu = false">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M4 14h6v6M20 10h-6V4"/>
                <path d="M14 10l7-7M3 21l7-7"/>
              </svg>
              <span>退出工作台</span>
            </button>
          </div>
        </Transition>
        <button class="zen-exit-arrow" @click="toggleExitMenu" title="工作台选项">
          <svg class="zen-exit-gear" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
          <span class="zen-exit-label">工作台选项</span>
          <svg v-if="showZenExitMenu" class="zen-exit-caret" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 15 12 9 18 15"/></svg>
          <svg v-else class="zen-exit-caret" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </Teleport>

    <WorkspaceProjectsDialog v-if="showProjectsDialog" @close="showProjectsDialog = false" />

    <Toast />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import ChatArea from './ChatArea.vue'
import ZenHistoryDrawer from './ZenHistoryDrawer.vue'
import Toast from './Toast.vue'
import ZenNotePanel from './ZenNotePanel.vue'
import ZenNotesDrawer from './ZenNotesDrawer.vue'
import WorkspaceProjectsDialog from './WorkspaceProjectsDialog.vue'
import { useZenStore } from '@/stores/zen'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const router = useRouter()
const zenStore = useZenStore()
const { confirmThreeWay } = useConfirmDialog()

const leftDrawerOpen = ref(false)
const rightDrawerOpen = ref(false)
const showZenExitMenu = ref(false)
// 2026-11-14 用户反馈：菜单打开后**不得有任何 hover 状态**。
// 菜单在光标下方展开时浏览器会立刻命中 :hover——解药是打开瞬间给菜单项
// 关闭 pointer-events，直到用户真正移动鼠标（首次 mousemove）再恢复。
const menuFresh = ref(false)
let menuFreshTimer: ReturnType<typeof setTimeout> | null = null
let menuFreshCleanup: (() => void) | null = null

function clearMenuFresh() {
  menuFresh.value = false
  if (menuFreshTimer) {
    clearTimeout(menuFreshTimer)
    menuFreshTimer = null
  }
  menuFreshCleanup?.()
  menuFreshCleanup = null
}

function armMenuFresh() {
  clearMenuFresh()
  menuFresh.value = true
  const onMove = () => clearMenuFresh()
  window.addEventListener('mousemove', onMove, { once: true, passive: true })
  menuFreshCleanup = () => window.removeEventListener('mousemove', onMove)
  // 兜底：1.5s 内完全没动鼠标也恢复，避免菜单永久不可点
  menuFreshTimer = setTimeout(() => clearMenuFresh(), 1500)
}

/** 2026-11-14 用户反馈：菜单打开后不得有默认焦点（退出按钮不得显示"被选中"）。
 *  鼠标点击打开时主动移除触发器焦点；键盘用户仍可用 Tab 进入菜单。 */
function toggleExitMenu() {
  showZenExitMenu.value = !showZenExitMenu.value
  if (showZenExitMenu.value) {
    armMenuFresh()
    void nextTick(() => {
      const el = document.activeElement as HTMLElement | null
      if (el && el.closest('.zen-exit-dropdown')) el.blur()
    })
  } else {
    clearMenuFresh()
  }
}
const showProjectsDialog = ref(false)
const zenNotePanelRef = ref<InstanceType<typeof ZenNotePanel> | null>(null)

function getHalfWidth() {
  return Math.max(320, Math.round(window.innerWidth / 2))
}

const rightPanelWidth = ref(getHalfWidth())
const isResizing = ref(false)

// rAF 合帧 + 值守卫：连续 resize 每帧最多写一次，值未变不触发重渲染。
let resizeRaf = 0

function onWindowResize() {
  if (resizeRaf) return
  resizeRaf = requestAnimationFrame(() => {
    resizeRaf = 0
    const next = getHalfWidth()
    if (next !== rightPanelWidth.value) rightPanelWidth.value = next
  })
}

function startResize(e: MouseEvent) {
  e.preventDefault()
  isResizing.value = true
  document.body.style.userSelect = 'none'
  document.body.style.webkitUserSelect = 'none'
  document.body.style.cursor = 'col-resize'
  const startX = e.clientX
  const startWidth = rightPanelWidth.value

  function onMouseMove(e: MouseEvent) {
    if (!isResizing.value) return
    e.preventDefault()
    const delta = startX - e.clientX
    const maxAllowed = window.innerWidth - 320
    const newWidth = Math.max(320, Math.min(maxAllowed, startWidth + delta))
    rightPanelWidth.value = newWidth
  }

  function onMouseUp() {
    isResizing.value = false
    document.body.style.userSelect = ''
    document.body.style.webkitUserSelect = ''
    document.body.style.cursor = ''
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
  }

  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

async function checkUnsavedChanges(): Promise<boolean> {
  const panel = zenNotePanelRef.value
  if (panel && panel.isDeleting) return true
  if (panel && panel.hasChanges) {
    const result = await confirmThreeWay({
      message: '当前笔记有未保存的修改，是否保存？',
      threeButton: { saveText: '保存', discardText: '不保存', cancelText: '取消' }
    })
    if (result === 'cancel') return false
    if (result === 'save') {
      await panel.saveNote()
    }
  }
  return true
}

async function handleSelectNote(notebookId: string, noteId: string) {
  if (noteId === zenStore.currentNoteId) return
  const proceed = await checkUnsavedChanges()
  if (!proceed) return
  zenStore.setCurrentNote(notebookId, noteId)
}

async function handleSelectNoteFromDrawer(notebookId: string, noteId: string) {
  if (noteId === zenStore.currentNoteId) {
    rightDrawerOpen.value = false
    return
  }
  const proceed = await checkUnsavedChanges()
  if (!proceed) return
  zenStore.setCurrentNote(notebookId, noteId)
  rightDrawerOpen.value = false
}

async function exitZenMode() {
  const proceed = await checkUnsavedChanges()
  if (!proceed) return
  router.push('/')
}

onMounted(() => {
  zenStore.loadDefaultNote()
  window.addEventListener('resize', onWindowResize)
})

onUnmounted(() => {
  if (isResizing.value) {
    isResizing.value = false
  }
  window.removeEventListener('resize', onWindowResize)
  clearMenuFresh()
})
</script>

<style scoped>
.zen-layout {
  display: flex;
  height: 100vh;
  height: 100dvh;
  width: 100vw;
  overflow: hidden;
  background-color: var(--surface-workbench);
}

.zen-left-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  border-right: 1px solid var(--panel-border);
}

.zen-right-panel {
  display: flex;
  flex-direction: column;
  min-width: 320px;
  overflow: hidden;
  background-color: var(--surface-workbench);
}

.zen-resizer {
  width: 4px;
  cursor: col-resize;
  background-color: var(--panel-border);
  transition: background-color 0.15s;
  flex-shrink: 0;
}

.zen-resizer:hover {
  background-color: var(--color-primary);
}

.zen-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--header-height);
  padding: 0 16px;
  background-color: var(--surface-panel-strong);
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
  position: relative;
}

.zen-panel-title {
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text);
}

.zen-panel-title-center {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
}

.zen-panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.zen-drawer-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: all var(--transition-fast);
}

.zen-drawer-toggle:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

.zen-exit-dropdown {
  position: fixed;
  top: 0;
  left: 50%;
  transform: translateX(-50%);
  z-index: 9997;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.zen-exit-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 20px;
  color: var(--color-text);
  font-size: 13px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-top: none;
  border-bottom: none;
  border-radius: 0 0 var(--radius-md) var(--radius-md);
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.05);
  white-space: nowrap;
  animation: slideDown 0.3s ease forwards;
}

.zen-exit-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-primary);
}

/* 下拉展开容器（项目列表 + 退出工作台，2026-11-14）：容器只承担堆叠与入场动画，
   卡片外观完全交给各皮肤主题的 .zen-exit-btn / .zen-menu-btn 规则（评审 r1 I1：
   容器不得自带 chrome，否则 mono-brutal 硬边硬影与 verdant 14px 卡全被顶掉）。 */
.zen-exit-menu {
  display: flex;
  flex-direction: column;
  align-items: center;
  animation: slideDown 0.3s ease forwards;
}

.zen-exit-menu .zen-exit-btn,
.zen-exit-menu .zen-menu-btn {
  animation: none;
}

/* 打开瞬间抑制 hover（直到用户真正移动鼠标），见 armMenuFresh */
.zen-exit-menu.is-fresh .zen-exit-btn,
.zen-exit-menu.is-fresh .zen-menu-btn {
  pointer-events: none;
}

/* 「项目列表」按钮基线外观（与 .zen-exit-btn 同几何、中性色）；三皮肤各自覆盖。 */
.zen-menu-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 20px;
  color: var(--color-text);
  font-size: 13px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  box-shadow: 0 2px 8px rgba(90, 130, 60, 0.05);
  white-space: nowrap;
}

.zen-menu-btn:hover {
  background-color: var(--color-hover);
  color: var(--color-primary);
}

.zen-exit-arrow {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  width: auto;
  height: 36px;
  padding: 0 14px;
  color: var(--color-text-light);
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-top: none;
  border-radius: var(--radius-sm) var(--radius-sm);
  transition: all var(--transition-fast);
}

.zen-exit-arrow:hover {
  background-color: var(--color-hover);
  color: var(--color-text);
}

@keyframes slideDown {
  from {
    opacity: 0;
    transform: translateY(-100%);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.zen-curtain-enter-active {
  animation: slideDown 0.3s ease forwards;
}

.zen-curtain-leave-active {
  animation: slideDown 0.3s ease reverse forwards;
}

.zen-curtain-enter-from {
  opacity: 0;
  transform: translateY(-100%);
}

.zen-curtain-leave-to {
  opacity: 0;
  transform: translateY(-100%);
}

.zen-panel-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  position: relative;
}

.zen-panel-body :deep(.chat-area) {
  border-radius: 0;
  border: none;
  box-shadow: none;
}

@media (max-width: 767px) {
  .zen-layout {
    flex-direction: column;
  }

  .zen-right-panel {
    display: none;
  }

  .zen-resizer {
    display: none;
  }
}
</style>
