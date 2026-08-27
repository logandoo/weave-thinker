<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="chat-layout">
    <DrawerOverlay :visible="sidebarOpen" @close="sidebarOpen = false" />
    <Sidebar
      :class="{ 'drawer-open': sidebarOpen }"
      @logout="handleLogout"
      @close-drawer="sidebarOpen = false"
    />
    <main class="chat-main">
      <div class="mobile-header hide-on-desktop">
        <button class="menu-toggle" @click="sidebarOpen = true">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="3" y1="12" x2="21" y2="12"/>
            <line x1="3" y1="6" x2="21" y2="6"/>
            <line x1="3" y1="18" x2="21" y2="18"/>
          </svg>
        </button>
        <span class="mobile-title">{{ currentTitle }}</span>
        <button
          v-if="showMobileSaveNote"
          class="mobile-save-note-btn"
          @click="enterSaveMode"
          title="选择消息添加到笔记"
          aria-label="选择消息添加到笔记"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
          </svg>
        </button>
      </div>
      <router-view v-slot="{ Component, route: childRoute }">
        <Transition :name="transitionName">
          <component :is="Component" :key="childRoute.path" />
        </Transition>
      </router-view>
    </main>
    <nav class="mobile-tab-bar hide-on-desktop" :class="{ 'tab-bar-hidden': hideTabBarForKeyboard }">
      <button type="button" class="tab-item" :class="{ active: !isOnNotesPage && !isOnZenPage && !isOnVoicePage }" @click="navigateToChat">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
        </svg>
        <span>Agent</span>
      </button>
      <button type="button" class="tab-item" :class="{ active: isOnNotesPage }" @click="navigateToNotes">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
          <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
        </svg>
        <span>笔记</span>
      </button>
      <button type="button" class="tab-item hide-on-mobile" :class="{ active: isOnZenPage }" @click="navigateToZen">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M2 8h20"/>
          <path d="M2 11h20"/>
          <path d="M5 11v9"/>
          <path d="M19 11v9"/>
        </svg>
        <span>工作台</span>
      </button>
    </nav>
    <Toast />
    <PermissionDialog />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Sidebar from './Sidebar.vue'
import DrawerOverlay from './DrawerOverlay.vue'
import Toast from './Toast.vue'
import PermissionDialog from './PermissionDialog.vue'
import { useAuth } from '@/composables/useAuth'
import { navigateWithMobileHistory } from '@/composables/useMobileNavigation'
import { useChatStore } from '@/stores/chat'
import { useNotesStore } from '@/stores/notes'
import { useAssistantStore } from '@/stores/assistant'
const route = useRoute()
const router = useRouter()
const auth = useAuth()
const chatStore = useChatStore()
const notesStore = useNotesStore()
const assistantStore = useAssistantStore()
const sidebarOpen = ref(false)

const isOnNotesPage = computed(() => route.path.startsWith('/notes'))
const isOnZenPage = computed(() => route.path === '/zen')
const isOnVoicePage = computed(() => route.path === '/voice')
const isOnNoteEditorPage = computed(() => route.name === 'NoteEditor')

// Hide the floating tab bar while the on-screen keyboard shrinks the visual
// viewport (mobile IME). visualViewport.height is compared against the layout
// height captured at mount; >150px shrink means the keyboard is open. Works on
// iOS (vv shrinks, innerHeight fixed) and Android (both shrink together).
const keyboardOpen = ref(false)
let layoutBaseline = 0
let lastWidth = 0

function updateKeyboardState() {
  const vv = window.visualViewport
  if (vv && vv.scale > 1) {
    keyboardOpen.value = false
    return
  }
  const visibleHeight = vv ? vv.height : window.innerHeight
  const width = window.innerWidth
  if (!layoutBaseline || width !== lastWidth) {
    layoutBaseline = window.innerHeight
    lastWidth = width
  }
  keyboardOpen.value = layoutBaseline - visibleHeight > 150
}

const hideTabBarForKeyboard = computed(() => keyboardOpen.value && isOnNoteEditorPage.value)

function getRouteDepth(path: string): number {
  if (path === '/' || path === '') return 0
  if (path.startsWith('/notes')) {
    const segs = path.split('/').filter(Boolean)
    return segs.length
  }
  if (path === '/zen') return 1
  if (path === '/voice') return 1
  return 0
}

const transitionDirection = ref<'forward' | 'backward'>('forward')

router.beforeEach((to) => {
  const fromDepth = getRouteDepth(route.path)
  const toDepth = getRouteDepth(to.path)
  transitionDirection.value = toDepth >= fromDepth ? 'forward' : 'backward'
})

const transitionName = computed(() => {
  return transitionDirection.value === 'forward' ? 'page-slide-left' : 'page-slide-right'
})

const currentTitle = computed(() => {
  if (isOnNotesPage.value) return '笔记'
  if (isOnZenPage.value) return '工作台'
  if (isOnVoicePage.value) return '语音助理'
  if (!chatStore.currentConversationId) return 'Weave Thinker'
  const conv = chatStore.conversations.find(c => c.id === chatStore.currentConversationId)
  const meta = chatStore.conversationMeta[chatStore.currentConversationId]
  return conv?.title || meta?.title || '新对话'
})

// Icon-only save-to-note entry on the mobile session title bar (rightmost),
// mirroring the desktop chat-header bookmark button.
const showMobileSaveNote = computed(() => {
  if (isOnNotesPage.value || isOnZenPage.value || isOnVoicePage.value) return false
  if (!chatStore.currentConversationId) return false
  if (chatStore.currentMessages.length === 0) return false
  return !chatStore.saveModeActive
})

function enterSaveMode() {
  chatStore.saveModeActive = true
}

async function navigateToChat() {
  sidebarOpen.value = false
  if (!isOnNotesPage.value) return
  await navigateWithMobileHistory(router, '/')
}

async function navigateToNotes() {
  sidebarOpen.value = false
  if (isOnNotesPage.value) return
  await navigateWithMobileHistory(router, notesStore.lastNotesPath)
}

async function navigateToZen() {
  sidebarOpen.value = false
  if (isOnZenPage.value) return
  await navigateWithMobileHistory(router, '/zen')
}

async function navigateToVoice() {
  sidebarOpen.value = false
  if (isOnVoicePage.value) return
  await navigateWithMobileHistory(router, '/voice')
}

async function handleLogout() {
  await auth.logout()
}

onMounted(async () => {
  layoutBaseline = window.innerHeight
  lastWidth = window.innerWidth
  window.visualViewport?.addEventListener('resize', updateKeyboardState)
  window.addEventListener('resize', updateKeyboardState)

  await chatStore.loadConversations(assistantStore.currentAssistantId)
  // Handle deep links from background tasks or external redirects.
  const convId = route.query.conv as string | undefined
  if (convId) {
    await chatStore.selectConversation(convId)
    await chatStore.refreshConversation(convId)
  }

  // Ensure the active conversation ID is always reflected in the URL so that
  // refreshes and deep links keep the user in the same session. Some sidebar
  // navigations (e.g. router.push('/')) clear query params, so this guard
  // re-applies the conv param after any route change when a conversation is active.
  router.afterEach((to) => {
    const id = chatStore.currentConversationId
    if (id && to.query.conv !== id) {
      router.replace({ query: { ...to.query, conv: id } }).catch(() => {})
    } else if (!id && to.query.conv) {
      const { conv, ...rest } = to.query
      router.replace({ query: rest }).catch(() => {})
    }
  })
})

watch(() => chatStore.currentConversationId, (id) => {
  const currentConv = route.query.conv as string | undefined
  if (id && currentConv !== id) {
    router.replace({ query: { ...route.query, conv: id } }).catch(() => {})
  } else if (!id && currentConv) {
    const { conv, ...rest } = route.query
    router.replace({ query: rest }).catch(() => {})
  }
})

onBeforeUnmount(() => {
  window.visualViewport?.removeEventListener('resize', updateKeyboardState)
  window.removeEventListener('resize', updateKeyboardState)
})
</script>

<style scoped>
.chat-layout {
  display: flex;
  height: 100vh;
  height: 100dvh;
  width: 100vw;
  overflow: hidden;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  position: relative;
  background-color: var(--surface-workbench);
}

.mobile-header {
  display: none;
  align-items: center;
  gap: 12px;
  height: var(--header-height);
  padding: 0 16px;
  background-color: var(--surface-panel-strong);
  border-bottom: 1px solid var(--panel-border);
}

@media (max-width: 767px) {
  .chat-layout {
    background-color: var(--surface-workbench);
    touch-action: pan-y pinch-zoom;
    overscroll-behavior-x: none;
  }

  .chat-main {
    touch-action: pan-y pinch-zoom;
    overscroll-behavior-x: none;
  }

  .chat-main :deep(.chat-input-wrapper) {
    margin-bottom: calc(var(--mobile-tab-bar-offset) + env(safe-area-inset-bottom, 0px));
  }

  .mobile-header {
    display: flex;
  }
}

.menu-toggle {
  padding: 8px;
  color: var(--color-text);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.menu-toggle:hover {
  background-color: var(--color-hover);
}

.menu-toggle:active {
  transform: scale(0.96);
}

.mobile-title {
  font-size: 16px;
  font-weight: 500;
  color: var(--color-text);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mobile-save-note-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  color: var(--color-text-light);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.mobile-save-note-btn:hover {
  color: var(--color-primary);
  background-color: var(--color-hover);
}

.mobile-save-note-btn:active {
  transform: scale(0.96);
}

/* --- Mobile bottom tab bar --- */
.mobile-tab-bar {
  display: none;
}

@media (max-width: 767px) {
  .mobile-tab-bar {
    display: flex;
    align-items: center;
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 100;
    gap: 10px;
    padding: 6px 12px calc(6px + env(safe-area-inset-bottom, 0px));
    background: transparent;
    pointer-events: none;
    transition: transform var(--transition-fast), opacity var(--transition-fast);
  }

  .mobile-tab-bar .tab-item {
    pointer-events: auto;
  }

  .mobile-tab-bar.tab-bar-hidden {
    transform: translateY(calc(100% + 20px));
    opacity: 0;
    pointer-events: none;
  }
}

.tab-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  min-height: var(--mobile-tab-bar-height);
  padding: 4px 0;
  border: 1px solid var(--panel-border);
  border-radius: 999px;
  background-color: var(--surface-panel-strong);
  box-shadow: var(--panel-shadow);
  color: var(--color-text-light);
  font-size: 11px;
  font-weight: 500;
  transition: color var(--transition-fast), border-color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.tab-item:active {
  transform: scale(0.96);
}

.tab-item.hide-on-mobile {
  display: none;
}

.tab-item.active {
  color: var(--color-primary);
  border-color: var(--panel-border-strong);
  background-color: color-mix(in srgb, var(--surface-panel-strong) 82%, var(--color-primary) 18%);
}

.tab-item:hover {
  color: var(--color-primary);
  transform: translateY(-1px);
}


</style>
