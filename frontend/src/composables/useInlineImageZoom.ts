// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

/**
 * Inline-markdown image → lightbox wiring shared by StreamMarkdown.vue
 * (live streaming bubble) and MessageBubble.vue (persisted messages).
 *
 * Markdown images inside chat answers render small (constrained preview) and
 * open the zoomable MediaLightbox on click. The image src is already a signed
 * /api/files/download URL, so the lightbox reuses it directly; downloads go
 * through downloadUrl with the same signed URL.
 */
import { computed, ref } from 'vue'
import type { FileAttachment } from '@/types'
import { downloadUrl } from '@/composables/useDownload'
import { useToast } from '@/composables/useToast'

export function useInlineImageZoom() {
  const lightboxMedia = ref<FileAttachment | null>(null)
  const lightboxUrl = ref('')
  const { show: showToast } = useToast()

  const lightboxKind = computed<'image' | 'video'>(() => 'image')

  function nameFromSrc(src: string, alt: string): string {
    if (alt && alt.trim()) return alt.trim().slice(0, 200)
    try {
      const u = new URL(src, window.location.origin)
      const p = u.searchParams.get('path')
      if (p) return decodeURIComponent(p).split('/').pop() || '图片'
    } catch { /* fall through */ }
    try {
      const last = (src || '').split('/').pop() || ''
      return last ? decodeURIComponent(last).slice(0, 200) : '图片'
    } catch {
      return '图片'
    }
  }

  /** Open the lightbox for a clicked <img>. Returns true when opened. */
  function openImageLightbox(img: HTMLImageElement | null): boolean {
    if (!img) return false
    const src = img.currentSrc || img.getAttribute('src') || ''
    const lower = src.toLowerCase()
    if (!src || lower.startsWith('data:image/svg+xml')) return false
    if (img.complete && img.naturalWidth === 0) return false
    lightboxUrl.value = src
    lightboxMedia.value = {
      name: nameFromSrc(src, img.alt || ''),
      path: src,
      size: 0,
      type: 'image',
    }
    return true
  }

  function closeLightbox() {
    lightboxMedia.value = null
    lightboxUrl.value = ''
  }

  async function onLightboxDownload() {
    if (!lightboxUrl.value) return
    const name = lightboxMedia.value?.name || '图片'
    const result = await downloadUrl(lightboxUrl.value, name)
    if (!result.success && result.error !== '下载已取消') {
      showToast(result.error || '下载失败', 'error')
    }
  }

  return {
    lightboxMedia,
    lightboxUrl,
    lightboxKind,
    openImageLightbox,
    closeLightbox,
    onLightboxDownload,
  }
}
