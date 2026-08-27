// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles/main.css'
// 皮肤令牌覆盖层：必须在 main.css 之后引入（同优先级按序覆盖）
import './styles/themes/index.css'
import { installGlobalCodeBlockCopy } from '@/composables/useCodeBlockCopy'
import Sortable from 'sortablejs'
// Tab-恢复重连监听器（模块副作用注册）
import '@/stores/chatVisibility'

// Make SortableJS available globally for vuedraggable
;(window as any).Sortable = Sortable

const app = createApp(App)
app.config.errorHandler = (err, vm, info) => {
  console.error('Vue error:', err, info, vm)
}
window.addEventListener('unhandledrejection', (event) => {
  console.error('Unhandled promise rejection:', event.reason)
})
window.addEventListener('error', (event) => {
  console.error('Global error:', event.error)
})
app.use(createPinia())
app.use(router)
// One-click copy buttons inside markdown code blocks (delegated listener).
installGlobalCodeBlockCopy()
app.mount('#app')
