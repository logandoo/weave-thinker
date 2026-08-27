// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuth } from '@/composables/useAuth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/components/LoginView.vue'),
    meta: { requiresAuth: false }
  },
  {
    path: '/',
    component: () => import('@/components/ChatLayout.vue'),
    meta: { requiresAuth: true, layout: 'main' },
    children: [
      {
        path: '',
        name: 'Chat',
        component: () => import('@/components/ChatArea.vue')
      },
      {
        path: 'notes',
        name: 'Notebooks',
        component: () => import('@/components/NotebooksList.vue')
      },
      {
        path: 'notes/:notebookId',
        name: 'Notes',
        component: () => import('@/components/NotesList.vue')
      },
      {
        path: 'notes/:notebookId/:noteId',
        name: 'NoteEditor',
        component: () => import('@/components/NoteEditor.vue')
      }
    ]
  },
  {
    path: '/zen',
    name: 'Zen',
    component: () => import('@/components/ZenMode.vue'),
    meta: { requiresAuth: true, layout: 'zen' }
  },
  {
    path: '/voice',
    name: 'Voice',
    component: () => import('@/components/VoiceChat.vue'),
    meta: { requiresAuth: true, layout: 'voice' }
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/'
  }
]

const router = createRouter({
  history: createWebHistory('/app/frontend/'),
  routes
})

router.beforeEach(async (to, from, next) => {
  const auth = useAuth()
  auth.initAuth()

  const requiresAuth = to.matched.some(record => record.meta.requiresAuth !== false)

  if (requiresAuth && !auth.isAuthenticated.value) {
    next({ name: 'Login', query: { redirect: to.fullPath } })
  } else if (to.name === 'Login' && auth.isAuthenticated.value) {
    next({ name: 'Chat' })
  } else {
    next()
  }
})

export default router
