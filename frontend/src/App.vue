<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="app-root">
    <div class="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {{ liveAnnouncement }}
    </div>
    <router-view v-slot="{ Component, route }">
      <Transition :name="transitionName">
        <component :is="Component" :key="route.meta.layout || route.name" />
      </Transition>
    </router-view>
  </div>
  <ConfirmDialog />
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, provide, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '@/composables/useAuth'
import { useSkinStore } from '@/stores/skin'
import { handleAndroidBack } from '@/composables/useBackNavigation'
import ConfirmDialog from '@/components/ConfirmDialog.vue'

const auth = useAuth()
const skinStore = useSkinStore()
const router = useRouter()
const transitionName = ref('')
const liveAnnouncement = ref('')

// 皮肤/明暗在 Vue 挂载前同步应用，避免首帧闪烁（FOUC）
skinStore.initFromStorage()

function announce(message: string) {
  liveAnnouncement.value = ''
  nextTick(() => {
    liveAnnouncement.value = message
  })
}

provide('announce', announce)

function isZenRoute(path: string) {
  return path === '/zen'
}

function isAppRoute(path: string) {
  return path !== '/login'
}

function updateZenTransition(toPath: string, fromPath: string) {
  const toZen = isZenRoute(toPath)
  const fromZen = isZenRoute(fromPath)
  const toApp = isAppRoute(toPath)
  const fromApp = isAppRoute(fromPath)

  if (toApp && fromApp && ((toZen && !fromZen) || (!toZen && fromZen))) {
    transitionName.value = 'zen-mode'
  } else {
    transitionName.value = ''
  }
}

const unregisterBeforeEach = router.beforeEach((to, from) => {
  updateZenTransition(to.path, from.path)
})

router.onError((error) => {
  console.error('Router error:', error)
  const message = '页面加载失败，请刷新重试'
  if (typeof (window as any).$toast === 'function') {
    ;(window as any).$toast(message)
  } else {
    alert(message)
  }
})

// Expose to Android WebView JS bridge
;(window as any).handleAndroidBack = handleAndroidBack

onMounted(() => {
  auth.initAuth()
  // Sliding session (wave-3): a valid stored token silently rolls its
  // window forward so users active within [security] token_expire_days
  // never re-enter their password. Fire-and-forget (errors are no-ops;
  // an expired token falls through to the normal 401 → login flow).
  if (auth.isAuthenticated.value) {
    auth.refreshSession()
  }
})

onUnmounted(() => {
  unregisterBeforeEach()
})
</script>

<style scoped>
.app-root {
  position: relative;
  width: 100vw;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.zen-mode-enter-active,
.zen-mode-leave-active {
  position: absolute;
  inset: 0;
  overflow: hidden;
  will-change: transform, opacity, filter;
  transition: opacity 0.45s cubic-bezier(0.4, 0, 0.2, 1),
              transform 0.45s cubic-bezier(0.4, 0, 0.2, 1),
              filter 0.45s cubic-bezier(0.4, 0, 0.2, 1);
}

.zen-mode-enter-from,
.zen-mode-leave-to {
  opacity: 0;
  transform: scale(0.97);
  filter: blur(6px);
}
</style>
