// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  DEFAULT_SKIN_ID,
  previewFromCss,
  SKIN_REGISTRY,
  type SkinMeta,
  type SkinId,
} from '@/config/skins'
import {
  fetchMySkinCss,
  getSkinPreference,
  listMySkins,
  saveSkinPreference,
} from '@/api/skins'

export type SkinMode = 'light' | 'dark'

const SKIN_STORAGE_KEY = 'wt-skin'
const MODE_STORAGE_KEY = 'theme'
const CSS_LINK_PREFIX = 'wt-skin-css-'

/**
 * 皮肤状态唯一入口：skin（皮肤）× mode（明暗）双轴。
 * - DOM 载体：<html data-skin="<id>"> + <html data-theme="dark">（light 移除属性）
 * - 持久化：localStorage wt-skin / theme；登录态下 skin 另经
 *   PUT /api/users/me/preferences 跨设备同步（设备本地优先）。
 * - 上传皮肤（wave-11）：per-user 运行时注册表 uploadedSkins；CSS 经认证
 *   fetch → Blob URL <link>（id=wt-skin-css-<skin_id>）注入，不产生公开 URL。
 */
export const useSkinStore = defineStore('skin', () => {
  const skinId = ref<string>(DEFAULT_SKIN_ID)
  const mode = ref<SkinMode>('light')
  const syncState = ref<'idle' | 'syncing' | 'saved' | 'failed'>('idle')
  const uploadedSkins = ref<SkinMeta[]>([])
  const cssCache = new Map<string, string>()

  const isDark = computed(() => mode.value === 'dark')
  const allSkins = computed<SkinMeta[]>(() => [
    ...SKIN_REGISTRY,
    ...uploadedSkins.value.filter(s => !SKIN_REGISTRY.some(b => b.id === s.id)),
  ])

  function applyToDom() {
    const el = document.documentElement
    el.setAttribute('data-skin', skinId.value)
    if (mode.value === 'dark') {
      el.setAttribute('data-theme', 'dark')
    } else {
      el.removeAttribute('data-theme')
    }
  }

  function notifyNativeBridge() {
    // Android WebView 桥契约（useDownload.ts 声明）：按布尔明暗通知
    try {
      window.WeaverNoteApp?.onThemeChanged?.(mode.value === 'dark')
    } catch {
      // 非 WebView 环境忽略
    }
  }

  function isUploadedSkin(id: string): boolean {
    return uploadedSkins.value.some(s => s.id === id) && !SKIN_REGISTRY.some(b => b.id === id)
  }

  /** 幂等地确保指定上传皮肤的 <link> 已装入 document.head（Blob URL）。 */
  async function ensureSkinCss(id: string): Promise<boolean> {
    if (!isUploadedSkin(id)) return false
    if (document.getElementById(CSS_LINK_PREFIX + id)) return true
    let css = cssCache.get(id)
    if (css == null) {
      try {
        css = await fetchMySkinCss(id)
        cssCache.set(id, css)
      } catch {
        return false
      }
    }
    const url = URL.createObjectURL(new Blob([css], { type: 'text/css' }))
    const link = document.createElement('link')
    link.id = CSS_LINK_PREFIX + id
    link.rel = 'stylesheet'
    link.href = url
    document.head.appendChild(link)
    return true
  }

  function revokeSkinCss(id: string) {
    const link = document.getElementById(CSS_LINK_PREFIX + id) as HTMLLinkElement | null
    if (link) {
      if (link.href.startsWith('blob:')) URL.revokeObjectURL(link.href)
      link.remove()
    }
    cssCache.delete(id)
  }

  /** 登录后调用：拉取本人上传皮肤并补装当前所选皮肤的 CSS。
   * 归属判定只看 LIST（权威数据源）；单个 CSS 取用失败不触发回退
   * （A4.9 #5：避免 cs 端点瞬时抖动把在用偏好永久降级成默认）。 */
  async function loadUploaded(): Promise<void> {
    let entries
    try {
      entries = (await listMySkins()).skins
    } catch {
      // 未登录/网络失败：静默（未登录时本地 id 若为上传皮肤无从验证，保持现状）
      return
    }
    const metas: SkinMeta[] = []
    for (const e of entries) {
      let css: string | null = null
      try {
        css = await fetchMySkinCss(e.id)
        cssCache.set(e.id, css)
      } catch {
        // 单文件取用失败：保留条目（归属成立），CSS 由 ensureSkinCss 重试
      }
      metas.push({
        id: e.id,
        name: e.name,
        description: e.description,
        isDefault: false,
        source: 'uploaded',
        preview: css ? previewFromCss(css) : previewFromCss('[data-skin="x"] { }'),
      })
    }
    uploadedSkins.value = metas
    // 当前在用皮肤：已装 link → 用本次拉取的原文重挂（覆盖同名重传）；未装 → 补装
    if (isUploadedSkin(skinId.value)) {
      const fresh = cssCache.get(skinId.value)
      const linked = document.getElementById(CSS_LINK_PREFIX + skinId.value) as HTMLLinkElement | null
      if (fresh != null && linked) {
        URL.revokeObjectURL(linked.href)
        linked.href = URL.createObjectURL(new Blob([fresh], { type: 'text/css' }))
      } else {
        void ensureSkinCss(skinId.value)
      }
    }
    // 本地保存的 id 已失效（被删/非本人）→ 回退默认
    if (!allSkins.value.some(s => s.id === skinId.value)) {
      skinId.value = DEFAULT_SKIN_ID
      localStorage.setItem(SKIN_STORAGE_KEY, DEFAULT_SKIN_ID)
      applyToDom()
      void persistRemote()
    }
  }

  /** 应用启动时同步调用（App.vue setup，先于首帧，避免闪烁）。
   * 非内置 id 不再直接清退——留给 loadUploaded() 登录后做归属判定。 */
  function initFromStorage() {
    const stored = localStorage.getItem(SKIN_STORAGE_KEY)
    if (stored) {
      skinId.value = stored
    } else {
      skinId.value = DEFAULT_SKIN_ID
    }
    mode.value = localStorage.getItem(MODE_STORAGE_KEY) === 'dark' ? 'dark' : 'light'
    applyToDom()
  }

  /** 登录态下调用：本地无记录时采用远端偏好（设备本地优先策略）。 */
  async function syncFromServer(): Promise<void> {
    if (localStorage.getItem(SKIN_STORAGE_KEY)) return
    try {
      const data = await getSkinPreference()
      if (!localStorage.getItem(SKIN_STORAGE_KEY) && allSkins.value.some(s => s.id === data.skin_id)) {
        skinId.value = data.skin_id
        applyToDom()
        if (isUploadedSkin(skinId.value)) void ensureSkinCss(skinId.value)
      }
    } catch {
      // 未登录/网络失败：静默保持本地缺省
    }
  }

  function setSkin(id: string): void {
    if (id !== skinId.value && !allSkins.value.some(s => s.id === id)) return
    skinId.value = id
    localStorage.setItem(SKIN_STORAGE_KEY, id)
    applyToDom()
    if (isUploadedSkin(id)) {
      void ensureSkinCss(id).then(ok => {
        if (!ok) {
          // 归属失效（后端已删）→ 回退默认，UI 列表由调用方刷新
          skinId.value = DEFAULT_SKIN_ID
          localStorage.setItem(SKIN_STORAGE_KEY, DEFAULT_SKIN_ID)
          applyToDom()
        }
      })
    }
    void persistRemote()
  }

  async function persistRemote(): Promise<void> {
    if (!localStorage.getItem('chatllm_token')) return
    syncState.value = 'syncing'
    try {
      await saveSkinPreference(skinId.value)
      syncState.value = 'saved'
    } catch {
      // 401（未登录）或网络失败：本地已生效，不回滚不打扰
      syncState.value = localStorage.getItem('chatllm_token') ? 'failed' : 'idle'
    }
  }

  /** 删除上传皮肤（调用方先经 API 完成服务端删除）。 */
  function removeSkin(id: string): void {
    const wasCurrent = skinId.value === id
    revokeSkinCss(id)
    uploadedSkins.value = uploadedSkins.value.filter(s => s.id !== id)
    if (wasCurrent) {
      skinId.value = DEFAULT_SKIN_ID
      localStorage.setItem(SKIN_STORAGE_KEY, DEFAULT_SKIN_ID)
    }
    applyToDom()
    if (wasCurrent) void persistRemote()
  }

  function setMode(next: SkinMode): void {
    if (mode.value === next) return
    mode.value = next
    localStorage.setItem(MODE_STORAGE_KEY, next)
    applyToDom()
    notifyNativeBridge()
  }

  /** View Transition 涟漪切换明暗（自 Sidebar.toggleTheme 迁移，行为不变）。 */
  function toggleMode(event?: MouseEvent): void {
    const next: SkinMode = mode.value === 'dark' ? 'light' : 'dark'

    if (typeof event !== 'undefined') {
      const x = event.clientX
      const y = event.clientY
      const endRadius = Math.hypot(
        Math.max(x, window.innerWidth - x),
        Math.max(y, window.innerHeight - y)
      )

      if (!(document as any).startViewTransition) {
        setMode(next)
        return
      }

      const transition = (document as any).startViewTransition(() => {
        setMode(next)
      })

      transition.ready.then(() => {
        document.documentElement.animate(
          {
            clipPath: [
              `circle(0px at ${x}px ${y}px)`,
              `circle(${endRadius}px at ${x}px ${y}px)`,
            ],
          },
          {
            duration: 280,
            easing: 'ease-out',
            pseudoElement: '::view-transition-new(root)',
          }
        )
      })
      return
    }

    setMode(next)
  }

  return {
    skinId,
    mode,
    isDark,
    syncState,
    uploadedSkins,
    allSkins,
    initFromStorage,
    syncFromServer,
    loadUploaded,
    ensureSkinCss,
    setSkin,
    removeSkin,
    setMode,
    toggleMode,
  }
})

export type { SkinId }
