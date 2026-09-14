<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="mp-panel" :data-loaded="loaded ? 'true' : 'false'">
    <div class="mp-header">
      <h3 class="mp-title">模型供应商</h3>
    </div>

    <p class="mp-hint">
      为你自己的账号覆盖系统模型供应商（URL / 模型名 / API Key / 采样参数）。留空即使用系统默认；设置仅对你自己生效，不影响其他用户。
    </p>

    <div class="mp-kinds">
      <button
        v-for="k in kinds"
        :key="k.key"
        class="mp-kind"
        :class="{ active: activeKind === k.key, overridden: hasOverride(k.key) }"
        @click="activeKind = k.key"
      >
        {{ k.label }}
        <span v-if="hasOverride(k.key)" class="mp-dot" title="已覆盖"></span>
      </button>
    </div>

    <div v-if="loading" class="mp-loading">加载中…</div>

    <template v-else-if="form">
      <p v-if="activeKind === 'asr'" class="mp-note mp-kind-note">
        自定义 URL 仅用于一次性转写（POST /transcribe，按填写的 Key 发送，缺省则不发送）。
        语音对话（实时流式）需要 DashScope/MiMo 引擎——设置自定义 ASR URL 后语音对话将不可用。
      </p>

      <div class="mp-form">
        <label class="mp-row mp-row-check">
          <input type="checkbox" v-model="form.enabled" />
          <span>启用此类型的覆盖</span>
        </label>

        <div class="mp-row">
          <label class="mp-label" :for="`mp-url-${activeKind}`">供应商 URL</label>
          <input
            :id="`mp-url-${activeKind}`"
            v-model="form.base_url"
            type="text"
            class="mp-input"
            placeholder="留空使用系统默认（如 https://api.example.com/v1）"
          />
        </div>

        <div class="mp-row">
          <label class="mp-label" :for="`mp-key-${activeKind}`">API Key</label>
          <div class="mp-key-wrap">
            <input
              :id="`mp-key-${activeKind}`"
              v-model="form.api_key"
              type="password"
              class="mp-input"
              :placeholder="keyPlaceholder"
              autocomplete="off"
              @input="form.clear_key = false"
            />
            <button
              v-if="form.has_api_key && !form.clear_key"
              class="mp-key-clear"
              type="button"
              @click="clearKey"
            >清除</button>
          </div>
          <span class="mp-note">
            {{ form.clear_key
              ? '保存后将清除 Key（该供应商按无 Key 调用）'
              : (form.has_api_key
                ? `已设置（尾号 ${form.api_key_tail}）。留空保持不变；填写则替换。`
                : '留空则调用时不发送 Key') }}
          </span>
        </div>

        <div class="mp-row">
          <label class="mp-label" :for="`mp-model-${activeKind}`">模型名称</label>
          <input
            :id="`mp-model-${activeKind}`"
            v-model="form.model_name"
            type="text"
            class="mp-input"
            placeholder="留空使用系统默认"
          />
        </div>

        <div class="mp-row">
          <span class="mp-label">采样参数</span>
          <div class="mp-params">
            <div v-for="p in PARAM_FIELDS" :key="p.key" class="mp-param">
              <label class="mp-param-label" :for="`mp-param-${activeKind}-${p.key}`">{{ p.label }}</label>
              <input
                :id="`mp-param-${activeKind}-${p.key}`"
                v-model="form.params[p.key]"
                type="text"
                inputmode="decimal"
                class="mp-input mp-param-input"
                placeholder="默认"
              />
            </div>
          </div>
          <span class="mp-note">留空即不覆盖该参数；数值校验：temperature 0-2 · top_p 0-1 · 其余见范围。</span>
        </div>
      </div>

      <div class="mp-status" :class="statusType" v-if="statusMsg">{{ statusMsg }}</div>

      <div class="mp-actions">
        <button class="mp-btn danger" :disabled="saving" @click="clearAll">清除全部覆盖</button>
        <button class="mp-btn" :disabled="saving" @click="clearCurrent">清除此类型</button>
        <button class="mp-btn primary" :disabled="saving" @click="saveCurrent">
          {{ saving ? '保存中…' : '保存此类型' }}
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  userSettingsApi,
  type ModelProviderStatus,
  type ProviderOverride,
  type ProviderOverridePayload,
} from '@/api/userSettings'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

defineProps<{ embedded?: boolean }>()

const { confirm: showConfirm } = useConfirmDialog()

const KINDS = [
  { key: 'llm', label: '大语言模型' },
  { key: 'vlm', label: '视觉模型' },
  { key: 'embedding', label: '向量模型' },
  { key: 'rerank', label: '重排序' },
  { key: 'asr', label: '语音识别' },
  { key: 'tts', label: '语音合成' },
]

const PARAM_FIELDS = [
  { key: 'temperature', label: 'temperature' },
  { key: 'top_p', label: 'top_p' },
  { key: 'top_k', label: 'top_k' },
  { key: 'max_tokens', label: 'max_tokens' },
  { key: 'presence_penalty', label: 'presence_penalty' },
  { key: 'frequency_penalty', label: 'frequency_penalty' },
]

// A4.9 Minor 修复：前端同源校验（服务端仍兜底 400）——防 Number("abc")→NaN→null 静默丢参
const PARAM_LIMITS: Record<string, [number, number]> = {
  temperature: [0, 2],
  top_p: [0, 1],
  top_k: [0, 10_000_000],
  max_tokens: [0, 10_000_000],
  presence_penalty: [-2, 2],
  frequency_penalty: [-2, 2],
}
const INT_PARAMS = new Set(['top_k', 'max_tokens'])

function validateParams(f: KindForm): string | null {
  for (const p of PARAM_FIELDS) {
    const rawVal = (f.params[p.key] ?? '').trim()
    if (rawVal === '') continue
    const num = Number(rawVal)
    if (!Number.isFinite(num)) return `${p.label} 必须是数字`
    const [lo, hi] = PARAM_LIMITS[p.key]
    if (num < lo || num > hi) return `${p.label} 超出范围 [${lo}, ${hi}]`
    if (INT_PARAMS.has(p.key) && !Number.isInteger(num)) return `${p.label} 必须是整数`
  }
  return null
}

interface KindForm {
  enabled: boolean
  base_url: string
  api_key: string
  clear_key: boolean
  has_api_key: boolean
  api_key_tail: string
  model_name: string
  params: Record<string, string>
}

const kinds = KINDS
const activeKind = ref('llm')
const loading = ref(true)
const saving = ref(false)
const statusMsg = ref('')
const statusType = ref<'ok' | 'error'>('ok')
// A4.9 Minor：序号防旧计时器清掉新消息（同文案也不误清）
let statusSeq = 0
const loaded = ref(false)
const raw = ref<ModelProviderStatus | null>(null)
const forms = ref<Record<string, KindForm>>({})

const form = computed<KindForm | null>(() => forms.value[activeKind.value] || null)

const keyPlaceholder = computed(() => {
  const f = form.value
  if (!f) return ''
  if (f.has_api_key && !f.clear_key) return '已设置（留空保持不变）'
  return '留空则不发送 Key'
})

function hasOverride(key: string): boolean {
  const ov = raw.value?.overrides?.[key]
  if (!ov) return false
  // A4.9 Minor 修复：params-only 覆盖也要显示"已覆盖"标记
  return !!ov.base_url || !!ov.model_name || ov.has_api_key
    || Object.values(ov.params || {}).some(v => v !== null && v !== undefined)
}

function buildForm(ov: ProviderOverride | null): KindForm {
  const params: Record<string, string> = {}
  for (const p of PARAM_FIELDS) {
    const v = ov?.params?.[p.key]
    params[p.key] = v === null || v === undefined ? '' : String(v)
  }
  return {
    enabled: ov?.enabled ?? true,
    base_url: ov?.base_url || '',
    api_key: '',
    clear_key: false,
    has_api_key: !!ov?.has_api_key,
    api_key_tail: ov?.api_key_tail || '',
    model_name: ov?.model_name || '',
    params,
  }
}

function applyStatus(status: ModelProviderStatus) {
  raw.value = status
  const next: Record<string, KindForm> = {}
  for (const k of KINDS) {
    next[k.key] = buildForm(status.overrides?.[k.key] || null)
  }
  forms.value = next
}

async function load() {
  loading.value = true
  try {
    const { data } = await userSettingsApi.getModelProvider()
    applyStatus(data)
  } catch (e: any) {
    statusMsg.value = e?.response?.data?.detail || '加载失败'
    statusType.value = 'error'
  } finally {
    loading.value = false
    loaded.value = true
  }
}

function setStatus(msg: string, type: 'ok' | 'error' = 'ok') {
  statusMsg.value = msg
  statusType.value = type
  const seq = ++statusSeq
  window.setTimeout(() => {
    if (seq === statusSeq) statusMsg.value = ''
  }, 4000)
}

function buildPayload(f: KindForm): ProviderOverridePayload | null {
  const params: Record<string, number | null> = {}
  for (const p of PARAM_FIELDS) {
    const rawVal = (f.params[p.key] ?? '').trim()
    params[p.key] = rawVal === '' ? null : Number(rawVal)
  }
  const payload: ProviderOverridePayload = {
    enabled: f.enabled,
    base_url: f.base_url.trim(),
    model_name: f.model_name.trim(),
    params,
  }
  if (f.clear_key) {
    payload.api_key = ''
  } else if (f.api_key.trim()) {
    payload.api_key = f.api_key.trim()
  } else {
    payload.api_key = null
  }
  const empty = !payload.base_url && !payload.model_name
    && !payload.api_key && !f.has_api_key
    && Object.values(params).every(v => v === null)
  if (empty && f.enabled) return null
  return payload
}

async function saveCurrent() {
  const f = form.value
  if (!f) return
  const invalid = validateParams(f)
  if (invalid) {
    setStatus(invalid, 'error')
    return
  }
  const payload = buildPayload(f)
  // A4.9 Minor：ASR 自定义 URL 会停用实时语音对话 → 保存前二次确认
  if (activeKind.value === 'asr' && payload && payload.base_url) {
    const ok = await showConfirm({
      message: '自定义 ASR URL 仅用于一次性转写；保存后实时语音对话（语音助理）将不可用。确认保存？',
      confirmText: '确认保存',
    })
    if (!ok) return
  }
  saving.value = true
  try {
    const body: Record<string, ProviderOverridePayload | null> = {}
    body[activeKind.value] = payload
    const { data } = await userSettingsApi.updateModelProvider(body)
    applyStatus(data)
    setStatus(payload === null ? '已恢复系统默认' : '已保存')
  } catch (e: any) {
    setStatus(e?.response?.data?.detail || '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

async function clearCurrent() {
  saving.value = true
  try {
    const body: Record<string, null> = {}
    body[activeKind.value] = null
    const { data } = await userSettingsApi.updateModelProvider(body)
    applyStatus(data)
    setStatus('已清除该类型覆盖')
  } catch (e: any) {
    setStatus(e?.response?.data?.detail || '清除失败', 'error')
  } finally {
    saving.value = false
  }
}

async function clearAll() {
  saving.value = true
  try {
    await userSettingsApi.deleteModelProvider()
    await load()
    setStatus('已清除全部覆盖')
  } catch (e: any) {
    setStatus(e?.response?.data?.detail || '清除失败', 'error')
  } finally {
    saving.value = false
  }
}

function clearKey() {
  const f = form.value
  if (!f) return
  f.clear_key = true
  f.api_key = ''
}

onMounted(load)
</script>

<style scoped>
.mp-panel {
  width: 100%;
  max-width: 560px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.mp-header {
  width: 100%;
  margin-bottom: 8px;
}

.mp-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.mp-hint {
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.5;
  margin: 0 0 14px 0;
  text-align: center;
}

.mp-kinds {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  width: 100%;
  margin-bottom: 14px;
}

.mp-kind {
  position: relative;
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text-light);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast), background var(--transition-fast);
}

.mp-kind.active {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.mp-kind.overridden {
  color: var(--color-text);
}

.mp-dot {
  position: absolute;
  top: -3px;
  right: -3px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
}

.mp-loading {
  font-size: 13px;
  color: var(--color-text-light);
  padding: 24px 0;
}

.mp-form {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.mp-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
}

.mp-row-check {
  flex-direction: row;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  color: var(--color-text);
}

.mp-row-check input[type="checkbox"] {
  width: 17px;
  height: 17px;
  accent-color: var(--color-primary);
  cursor: pointer;
}

.mp-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.mp-input {
  width: 100%;
  box-sizing: border-box;
  padding: 9px 12px;
  border-radius: 12px;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  font-size: 13px;
  outline: none;
}

.mp-input:focus {
  border-color: var(--color-primary);
}

.mp-key-wrap {
  display: flex;
  gap: 8px;
  align-items: center;
}

.mp-key-clear {
  flex-shrink: 0;
  padding: 8px 12px;
  border-radius: 12px;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text-light);
  font-size: 12px;
  cursor: pointer;
}

.mp-note {
  font-size: 12px;
  color: var(--color-text-light);
  line-height: 1.4;
}

.mp-kind-note {
  margin: 0 0 12px 0;
  text-align: center;
  width: 100%;
}

.mp-params {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.mp-param {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mp-param-label {
  font-size: 11px;
  color: var(--color-text-light);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.mp-param-input {
  padding: 7px 10px;
  font-size: 12px;
}

.mp-status {
  margin-top: 12px;
  font-size: 13px;
}

.mp-status.ok {
  color: var(--color-primary);
}

.mp-status.error {
  color: var(--color-error);
}

.mp-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  margin-top: 18px;
  width: 100%;
}

.mp-btn {
  padding: 9px 16px;
  border-radius: 14px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  transition: opacity var(--transition-fast), background var(--transition-fast);
}

.mp-btn.primary {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  border-color: transparent;
  color: #fff;
}

.mp-btn.danger {
  color: var(--color-error);
}

.mp-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

@media (max-width: 767px) {
  .mp-params {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
