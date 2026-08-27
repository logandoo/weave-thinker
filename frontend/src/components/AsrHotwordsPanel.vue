<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="hotwords-panel">
    <div class="hotwords-header">
      <h3 class="hotwords-title">语音识别热词配置</h3>
      <button v-if="!embedded" class="hotwords-close" @click="$emit('close')">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"/>
          <line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>

    <p class="hotwords-hint">
      添加专业术语、产品名、人名等，可提升语音识别准确率。权重越高，模型越倾向于识别该词。
    </p>

    <div class="hotword-form">
      <div class="form-row">
        <input
          ref="textInputRef"
          v-model="newHotword.text"
          type="text"
          placeholder="输入热词文本"
          maxlength="30"
          @keyup.enter="handleAdd"
        />
        <select v-model="newHotword.lang" title="语言">
          <option value="">自动</option>
          <option value="zh">中文</option>
          <option value="en">英文</option>
        </select>
      </div>
      <div class="form-row weight-row">
        <span class="weight-label">权重</span>
        <input
          v-model.number="newHotword.weight"
          type="range"
          min="1"
          max="5"
          step="1"
        />
        <span class="weight-value">{{ newHotword.weight }}</span>
        <button class="add-btn" @click="handleAdd">添加</button>
      </div>
    </div>

    <div class="hotwords-list">
      <div v-if="hotwords.length === 0" class="hotwords-empty">暂无热词</div>
      <div
        v-for="(item, index) in hotwords"
        :key="index"
        class="hotword-item"
      >
        <span class="hotword-text">{{ item.text }}</span>
        <span class="hotword-meta">权重 {{ item.weight }}{{ item.lang ? ` · ${item.lang}` : '' }}</span>
        <button class="remove-btn" @click="removeHotword(index)" title="删除">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>
    </div>

    <div class="hotwords-actions">
      <button v-if="!embedded" class="modal-btn cancel" @click="$emit('close')">关闭</button>
      <button class="modal-btn confirm" @click="handleSave" :disabled="saving">
        {{ saving ? '保存中...' : '保存' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { useAsrHotwords } from '@/composables/useAsrHotwords'
import type { HotwordItem } from '@/api/asr'

const props = defineProps<{
  embedded?: boolean
}>()

const emit = defineEmits<{
  close: []
  save: [hotwords: HotwordItem[]]
}>()

const { hotwords: sharedHotwords, load: loadSharedHotwords, save: saveSharedHotwords } = useAsrHotwords()

const hotwords = ref<HotwordItem[]>([])
const newHotword = ref<HotwordItem>({ text: '', weight: 4, lang: '' })
const textInputRef = ref<HTMLInputElement | null>(null)
const saving = ref(false)

async function resetDialogState() {
  await loadSharedHotwords()
  hotwords.value = sharedHotwords.value.map(item => ({ ...item }))
  newHotword.value = { text: '', weight: 4, lang: '' }
}

function handleAdd() {
  const text = newHotword.value.text.trim()
  if (!text) return
  if (hotwords.value.some(h => h.text === text)) {
    newHotword.value.text = ''
    return
  }
  hotwords.value.push({
    text,
    weight: Math.max(1, Math.min(5, Number(newHotword.value.weight) || 4)),
    lang: newHotword.value.lang || undefined,
  })
  newHotword.value.text = ''
  nextTick(() => textInputRef.value?.focus())
}

function removeHotword(index: number) {
  hotwords.value.splice(index, 1)
}

async function handleSave() {
  saving.value = true
  try {
    await saveSharedHotwords(hotwords.value)
    emit('save', hotwords.value)
    if (!props.embedded) {
      emit('close')
    }
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  resetDialogState()
  nextTick(() => textInputRef.value?.focus())
})
</script>

<style scoped>
.hotwords-panel {
  width: 100%;
  max-width: 480px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.hotwords-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  width: 100%;
}

.hotwords-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.hotwords-close {
  background: transparent;
  border: none;
  color: var(--color-text-light);
  cursor: pointer;
  padding: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.hotwords-close:hover {
  color: var(--color-text);
}

.hotwords-hint {
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
  margin: 0 0 16px 0;
  text-align: center;
}

.hotword-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 16px;
  align-items: center;
  width: 100%;
  max-width: 400px;
}

.form-row {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
}

.form-row input[type="text"] {
  flex: 1;
  padding: 10px 12px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  font-size: 14px;
  color: var(--color-text);
  background: var(--color-bg);
}

.form-row input[type="text"]:focus {
  outline: none;
  border-color: var(--color-primary);
}

.form-row select {
  padding: 10px 12px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  font-size: 14px;
  color: var(--color-text);
  background: var(--color-bg);
}

.weight-row {
  justify-content: center;
}

.weight-label {
  font-size: 13px;
  color: var(--color-text-light);
  min-width: 36px;
}

.weight-value {
  font-size: 14px;
  color: var(--color-text);
  min-width: 16px;
  text-align: center;
}

.add-btn {
  padding: 8px 16px;
  border: none;
  border-radius: 8px;
  background: var(--color-primary);
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.2s;
}

.add-btn:hover {
  opacity: 0.9;
}

.hotwords-list {
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  padding: 8px;
  margin-bottom: 16px;
  width: 100%;
}

.hotwords-empty {
  padding: 20px;
  text-align: center;
  color: var(--color-text-light);
  font-size: 14px;
}

.hotword-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  transition: background 0.15s ease;
}

.hotword-item:hover {
  background: var(--surface-panel-subtle);
}

.hotword-text {
  flex: 1;
  font-size: 14px;
  color: var(--color-text);
}

.hotword-meta {
  font-size: 12px;
  color: var(--color-text-light);
  white-space: nowrap;
}

.remove-btn {
  background: transparent;
  border: none;
  color: var(--color-text-light);
  cursor: pointer;
  padding: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
}

.remove-btn:hover {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.1);
}

.hotwords-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
}

.modal-btn {
  min-width: 86px;
  padding: 10px 20px;
  border-radius: 16px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--transition-fast), opacity var(--transition-fast), background var(--transition-fast);
  border: 1px solid transparent;
}

.modal-btn.cancel {
  background: var(--surface-panel-subtle);
  border-color: var(--panel-border);
  color: var(--color-text);
}

.modal-btn.confirm {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: #fff;
  box-shadow: 0 8px 16px rgba(141, 104, 69, 0.18);
}

.modal-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
