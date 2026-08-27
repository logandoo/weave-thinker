// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import { ref, onMounted } from 'vue'
import type { HotwordItem } from '@/api/asr'
import { asrApi } from '@/api/asr'

const STORAGE_KEY = 'chatllm_asr_hotwords'

const sharedHotwords = ref<HotwordItem[]>([])

function loadFromStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as HotwordItem[]
      sharedHotwords.value = parsed.filter(item => item.text && item.text.trim())
    } else {
      sharedHotwords.value = []
    }
  } catch {
    sharedHotwords.value = []
  }
}

function saveToStorage(hotwords: HotwordItem[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(hotwords))
  } catch {
    // ignore storage errors
  }
  sharedHotwords.value = hotwords
}

async function loadFromBackend() {
  try {
    const hotwords = await asrApi.getHotwords()
    sharedHotwords.value = hotwords.filter(item => item.text && item.text.trim())
    saveToStorage(sharedHotwords.value)
  } catch {
    // Fall back to localStorage on network/auth errors
    loadFromStorage()
  }
}

async function saveToBackend(hotwords: HotwordItem[]) {
  const valid = hotwords.filter(item => item.text && item.text.trim())
  try {
    const saved = await asrApi.saveHotwords(valid)
    sharedHotwords.value = saved
    saveToStorage(saved)
  } catch {
    // Keep local state even if backend fails; do not overwrite with stale data
    sharedHotwords.value = valid
    saveToStorage(valid)
  }
}

export function useAsrHotwords() {
  onMounted(loadFromBackend)

  return {
    hotwords: sharedHotwords,
    load: loadFromBackend,
    save: saveToBackend,
  }
}
