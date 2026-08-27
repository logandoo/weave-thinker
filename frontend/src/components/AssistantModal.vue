<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-overlay" @click.self="handleClose">
      <div class="modal">
        <div class="modal-header">
          <h3>{{ isEdit ? '编辑助手' : '新建助手' }}</h3>
          <button class="close-btn" @click="handleClose">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        <div class="modal-body">
          <div class="form-group">
            <label class="form-label">助手名称</label>
            <input
              v-model="formData.name"
              type="text"
              placeholder="给助手起个名字"
              class="form-input"
            />
          </div>

          <div v-if="isEdit" class="form-group">
            <label class="form-label">会话管理</label>
            <div class="batch-actions">
              <button class="btn btn-batch-export" @click="emit('batch-export')">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="7 10 12 15 17 10"/>
                  <line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
                批量导出会话
              </button>
              <button class="btn btn-batch-delete" @click="emit('batch-delete')">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
                批量删除会话
              </button>
            </div>
          </div>

          <div class="form-group">
            <label class="form-label">系统提示词</label>
            <textarea
              v-model="formData.system_prompt"
              placeholder="设置助手的角色和行为规则。留空则直接发送用户消息，不添加系统提示。"
              class="form-textarea"
              rows="4"
            ></textarea>
            <div class="form-hint">定义助手的人设、专长和交互规则</div>
          </div>

          <div class="divider"></div>

          <div class="section-title">LLM 参数配置</div>

          <template v-if="formData.provider_type !== 'qwen3.8_vllm'">
          <div class="param-group">
            <label class="param-label">
              Temperature
              <span class="param-value">{{ formData.temperature ?? '默认' }}</span>
            </label>
            <input
              type="range"
              v-model.number="formData.temperature"
              min="0"
              max="2"
              step="0.1"
              class="param-slider"
            />
            <div class="param-hint">控制随机性，较高的值会产生更随机的输出</div>
          </div>

          <div class="param-group">
            <label class="param-label">
              Top P
              <span class="param-value">{{ formData.top_p ?? '默认' }}</span>
            </label>
            <input
              type="range"
              v-model.number="formData.top_p"
              min="0"
              max="1"
              step="0.05"
              class="param-slider"
            />
            <div class="param-hint">核采样，较低的值会限制候选词范围</div>
          </div>

          <div class="param-group">
            <label class="param-label">
              Top K
              <span class="param-value">{{ formData.top_k ?? '默认' }}</span>
            </label>
            <input
              type="number"
              v-model.number="formData.top_k"
              min="1"
              max="100"
              placeholder="默认"
              class="param-input"
            />
            <div class="param-hint">限制每一步考虑的候选词数量</div>
          </div>

          <div class="param-group">
            <label class="param-label">
              Presence Penalty
              <span class="param-value">{{ formData.presence_penalty ?? '默认' }}</span>
            </label>
            <input
              type="range"
              v-model.number="formData.presence_penalty"
              min="-2"
              max="2"
              step="0.1"
              class="param-slider"
            />
            <div class="param-hint">正值会惩罚已出现的词，鼓励谈论新话题</div>
          </div>

          <div class="param-group">
            <label class="param-label">
              Frequency Penalty
              <span class="param-value">{{ formData.frequency_penalty ?? '默认' }}</span>
            </label>
            <input
              type="range"
              v-model.number="formData.frequency_penalty"
              min="-2"
              max="2"
              step="0.1"
              class="param-slider"
            />
            <div class="param-hint">正值会惩罚高频词，减少重复</div>
          </div>

          <div class="param-group">
            <label class="param-label">
              Max Tokens
              <span class="param-value">{{ formData.max_tokens ?? '默认' }}</span>
            </label>
            <input
              type="number"
              v-model.number="formData.max_tokens"
              min="1"
              max="4096"
              placeholder="默认"
              class="param-input"
            />
            <div class="param-hint">限制单次回复的最大 token 数</div>
          </div>

          <div class="param-group">
            <label class="param-label">
              Thinking Budget
              <span class="param-value">{{ formData.thinking_budget ?? '默认' }}</span>
            </label>
            <input
              type="number"
              v-model.number="formData.thinking_budget"
              min="0"
              step="100"
              placeholder="默认"
              class="param-input"
            />
            <div class="param-hint">限制思考阶段最大 token 数，0 表示不限制</div>
          </div>
          </template>

          <template v-if="formData.provider_type === 'qwen3.8_vllm'">
            <div class="section-title">思考模式采样参数</div>
            <div class="param-hint qwen38-section-hint">思考模式默认：temperature 1.0 · top_p 0.95 · top_k 20 · min_p 0.0 · presence_penalty 0.0 · repetition_penalty 1.0（模型卡推荐）</div>

            <div class="param-group">
              <label class="param-label">
                Temperature（思考）
                <span class="param-value">{{ formData.thinking_temperature ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.thinking_temperature"
                min="0"
                max="2"
                step="0.1"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Top P（思考）
                <span class="param-value">{{ formData.thinking_top_p ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.thinking_top_p"
                min="0"
                max="1"
                step="0.05"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Top K（思考）
                <span class="param-value">{{ formData.thinking_top_k ?? '默认' }}</span>
              </label>
              <input
                type="number"
                v-model.number="formData.thinking_top_k"
                min="1"
                max="100"
                placeholder="默认"
                class="param-input"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Min P（思考）
                <span class="param-value">{{ formData.thinking_min_p ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.thinking_min_p"
                min="0"
                max="1"
                step="0.05"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Presence Penalty（思考）
                <span class="param-value">{{ formData.thinking_presence_penalty ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.thinking_presence_penalty"
                min="-2"
                max="2"
                step="0.1"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Repetition Penalty（思考）
                <span class="param-value">{{ formData.thinking_repetition_penalty ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.thinking_repetition_penalty"
                min="0"
                max="2"
                step="0.05"
                class="param-slider"
              />
            </div>

            <div class="divider"></div>

            <div class="section-title">非思考模式采样参数</div>
            <div class="param-hint qwen38-section-hint">非思考模式默认：temperature 0.7 · top_p 0.80 · top_k 20 · min_p 0.0 · presence_penalty 1.5 · repetition_penalty 1.0（模型卡推荐）</div>

            <div class="param-group">
              <label class="param-label">
                Temperature（非思考）
                <span class="param-value">{{ formData.temperature ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.temperature"
                min="0"
                max="2"
                step="0.1"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Top P（非思考）
                <span class="param-value">{{ formData.top_p ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.top_p"
                min="0"
                max="1"
                step="0.05"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Top K（非思考）
                <span class="param-value">{{ formData.top_k ?? '默认' }}</span>
              </label>
              <input
                type="number"
                v-model.number="formData.top_k"
                min="1"
                max="100"
                placeholder="默认"
                class="param-input"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Min P（非思考）
                <span class="param-value">{{ formData.min_p ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.min_p"
                min="0"
                max="1"
                step="0.05"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Presence Penalty（非思考）
                <span class="param-value">{{ formData.presence_penalty ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.presence_penalty"
                min="-2"
                max="2"
                step="0.1"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-label">
                Repetition Penalty（非思考）
                <span class="param-value">{{ formData.repetition_penalty ?? '默认' }}</span>
              </label>
              <input
                type="range"
                v-model.number="formData.repetition_penalty"
                min="0"
                max="2"
                step="0.05"
                class="param-slider"
              />
            </div>

            <div class="param-group">
              <label class="param-checkbox">
                <input type="checkbox" v-model="formData.preserve_thinking" />
                <span>preserve_thinking（保留历史思考链）</span>
              </label>
              <div class="param-hint">保留并利用历史消息中的思考过程（chat_template_kwargs.preserve_thinking），默认开启</div>
            </div>
          </template>

          <div class="divider"></div>

          <div class="section-title">供应商</div>

          <div class="param-group">
            <label class="param-label">供应商类型</label>
            <select v-model="formData.provider_type" class="param-input" @change="onProviderChange">
              <option value="deepseek">DeepSeek</option>
              <option value="qwen3.8_vllm">Qwen3.8(Local)</option>
              <option value="mimo">MiMo (Xiaomi)</option>
              <option value="custom">自定义</option>
            </select>
            <div class="param-hint">不同供应商使用不同的 Thinking 模式参数格式</div>
          </div>

          <div class="divider"></div>

          <div class="section-title">模型配置</div>

          <template v-if="formData.provider_type === 'custom'">
            <div class="param-group">
              <label class="param-label">API 地址</label>
              <input
                v-model="formData.custom_api_url"
                type="text"
                placeholder="https://api.example.com/v1"
                class="param-input"
              />
              <div class="param-hint">OpenAI 兼容的 API 地址</div>
            </div>

            <div class="param-group">
              <label class="param-label">API Key</label>
              <input
                v-model="formData.custom_api_key"
                type="password"
                placeholder="sk-..."
                class="param-input"
              />
              <div class="param-hint">API 密钥（可选），自部署模型可留空</div>
            </div>

            <div class="param-group">
              <label class="param-label">模型名称</label>
              <input
                v-model="formData.custom_model_name"
                type="text"
                placeholder="gpt-4o / deepseek-reasoner"
                class="param-input"
              />
              <div class="param-hint">要使用的模型名称</div>
            </div>

            <div class="param-group">
              <label class="param-label">Extra Body (JSON)</label>
              <textarea
                v-model="formData.extra_body"
                placeholder='{"thinking": {"type": "enabled"}}'
                class="form-textarea"
                rows="3"
              ></textarea>
              <div class="param-hint">直接输入 extra_body 的完整 JSON，将原样传递给 LLM API</div>
            </div>
          </template>

          <template v-else-if="formData.provider_type !== 'qwen3.8_vllm'">
            <div class="param-group">
              <label class="param-label">API Key</label>
              <input
                v-model="formData.custom_api_key"
                type="password"
                :placeholder="apiKeyPlaceholder"
                class="param-input"
              />
              <div class="param-hint">{{ apiKeyHint }}</div>
            </div>

            <div class="param-group">
              <label class="param-label">模型名称</label>
              <input
                v-model="formData.custom_model_name"
                type="text"
                :placeholder="modelNamePlaceholder"
                class="param-input"
              />
              <div class="param-hint">{{ modelNameHint }}</div>
            </div>
          </template>

          <template v-if="formData.provider_type === 'qwen3.8_vllm'">
            <div class="param-group">
              <label class="param-label">API 地址</label>
              <input
                v-model="formData.custom_api_url"
                type="text"
                :placeholder="qwen38ApiUrlPlaceholder"
                class="param-input"
              />
              <div class="param-hint">vLLM 服务地址（OpenAI 兼容，modelscope 部署指南格式），留空使用服务器配置</div>
            </div>

            <div class="param-group">
              <label class="param-label">API Key</label>
              <input
                v-model="formData.custom_api_key"
                type="password"
                placeholder="无需密钥（本地 vLLM 服务）"
                class="param-input"
              />
              <div class="param-hint">自部署 vLLM 服务通常无需鉴权，留空即可</div>
            </div>

            <div class="param-group">
              <label class="param-label">模型名称</label>
              <input
                v-model="formData.custom_model_name"
                type="text"
                :placeholder="modelNamePlaceholder"
                class="param-input"
              />
              <div class="param-hint">vLLM 服务的模型名（如 qwen3.8_27b），留空使用服务器配置</div>
            </div>
          </template>

          <div class="divider"></div>

          <div class="section-title">Subagent 任务模型</div>

          <div class="param-group">
            <label class="param-checkbox">
              <input type="checkbox" v-model="formData.use_subtask_model" />
              <span>启用 subagent 任务模型</span>
            </label>
            <div class="param-hint">为子任务（工具迭代、搜索决策、关键词生成等）使用独立的轻量模型；不勾选时复用主模型并自动关闭 thinking</div>
          </div>

          <template v-if="formData.use_subtask_model">
            <div class="param-group">
              <label class="param-label">供应商类型</label>
              <select v-model="formData.subtask_provider_type" class="param-input">
                <option :value="null">跟随主模型</option>
                <option value="deepseek">DeepSeek</option>
                <option value="qwen3.8_vllm">Qwen3.8(Local)</option>
                <option value="mimo">MiMo (Xiaomi)</option>
                <option value="custom">自定义</option>
              </select>
              <div class="param-hint">用于决定 thinking 参数格式</div>
            </div>

            <div class="param-group">
              <label class="param-label">API 地址</label>
              <input
                v-model="formData.subtask_custom_api_url"
                type="text"
                placeholder="https://api.example.com/v1"
                class="param-input"
              />
              <div class="param-hint">OpenAI 兼容的 API 地址</div>
            </div>

            <div class="param-group">
              <label class="param-label">API Key</label>
              <input
                v-model="formData.subtask_custom_api_key"
                type="password"
                placeholder="sk-..."
                class="param-input"
              />
            </div>

            <div class="param-group">
              <label class="param-label">模型名称</label>
              <input
                v-model="formData.subtask_custom_model_name"
                type="text"
                placeholder="例如 deepseek-chat"
                class="param-input"
              />
              <div class="param-hint">建议选择一个不带思考链的轻量模型</div>
            </div>

            <div class="param-group">
              <label class="param-label">Extra Body (JSON)</label>
              <textarea
                v-model="formData.subtask_extra_body"
                placeholder='{"enable_thinking": false}'
                class="form-textarea"
                rows="3"
              ></textarea>
              <div class="param-hint">原样传递给 subagent LLM 调用</div>
            </div>
          </template>
        </div>

        <div class="modal-footer">
          <button class="btn btn-secondary" @click="handleReset">重置</button>
          <button class="btn btn-primary" @click="handleSave" :disabled="!formData.name.trim()">
            {{ isEdit ? '保存' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import type { Assistant, AssistantFormData } from '@/types'
import { configApi, type ProviderConfig } from '@/api/config'

const props = defineProps<{
  visible: boolean
  assistant?: Assistant | null
}>()

const emit = defineEmits<{
  close: []
  save: [data: AssistantFormData]
  'batch-export': []
  'batch-delete': []
}>()

const providerConfigs = ref<Record<string, ProviderConfig>>({})

// Qwen3.8-27B-FP8 model-card defaults (modelscope.cn/models/Qwen/Qwen3.8-27B-FP8)
const QWEN38_THINKING_DEFAULTS = {
  thinking_temperature: 1.0,
  thinking_top_p: 0.95,
  thinking_top_k: 20,
  thinking_min_p: 0.0,
  thinking_presence_penalty: 0.0,
  thinking_repetition_penalty: 1.0,
} as const

const QWEN38_NON_THINKING_DEFAULTS = {
  temperature: 0.7,
  top_p: 0.8,
  top_k: 20,
  min_p: 0.0,
  presence_penalty: 1.5,
  repetition_penalty: 1.0,
} as const

const defaultFormData: AssistantFormData = {
  name: '',
  system_prompt: '',
  temperature: null,
  top_p: null,
  top_k: null,
  presence_penalty: null,
  frequency_penalty: null,
  max_tokens: null,
  use_custom_model: false,
  custom_api_url: null,
  custom_api_key: null,
  custom_model_name: null,
  provider_type: 'deepseek',
  extra_body: null,
  use_subtask_model: false,
  subtask_custom_api_url: null,
  subtask_custom_api_key: null,
  subtask_custom_model_name: null,
  subtask_provider_type: null,
  subtask_extra_body: null,
  thinking_budget: null,
  min_p: null,
  repetition_penalty: null,
  thinking_temperature: null,
  thinking_top_p: null,
  thinking_top_k: null,
  thinking_min_p: null,
  thinking_presence_penalty: null,
  thinking_repetition_penalty: null,
  preserve_thinking: true,
}

const formData = ref<AssistantFormData>({ ...defaultFormData })

const isEdit = computed(() => !!props.assistant)

const apiKeyPlaceholder = computed(() => {
  if (formData.value.provider_type === 'deepseek') return '使用系统默认密钥（可选）'
  if (formData.value.provider_type === 'qwen3.8_vllm') return '无需密钥（本地 vLLM 服务）'
  return '请输入 API Key'
})

const apiKeyHint = computed(() => {
  if (formData.value.provider_type === 'deepseek') {
    return '留空则使用系统默认的 DeepSeek API Key'
  }
  if (formData.value.provider_type === 'qwen3.8_vllm') {
    return '自部署 vLLM 服务通常无需鉴权，留空即可'
  }
  return '请输入您的 API Key'
})

const modelNamePlaceholder = computed(() => {
  const pt = formData.value.provider_type || 'deepseek'
  const cfg = providerConfigs.value[pt] || providerConfigs.value['qwen3.8_27b']
  if (cfg?.model_name) return cfg.model_name
  return '请输入模型名称'
})

const qwen38ApiUrlPlaceholder = computed(() => {
  return providerConfigs.value['qwen3.8_27b']?.base_url || 'http://127.0.0.1:8000/v1'
})

const modelNameHint = computed(() => {
  if (formData.value.provider_type === 'deepseek') {
    return '留空则使用系统默认模型名称'
  }
  if (formData.value.provider_type === 'qwen3.8_vllm') {
    return '留空则使用服务器配置的模型名称'
  }
  return '请输入模型名称'
})

onMounted(async () => {
  try {
    const res = await configApi.getProviderConfigs()
    providerConfigs.value = res.providers
  } catch (e) {
    console.error('Failed to load provider configs:', e)
  }
})

function applyQwen38Defaults() {
  // Fill only NULL fields so saved values survive editing; defaults follow
  // the modelscope model card. preserve_thinking defaults to ON. The vLLM
  // address/model prefill from the server provider config ([providers."qwen3.8_27b"])
  // so the dialog is usable out of the box yet fully overridable.
  if (formData.value.preserve_thinking == null) formData.value.preserve_thinking = true
  const fd = formData.value
  for (const [k, v] of Object.entries(QWEN38_THINKING_DEFAULTS)) {
    if (fd[k as keyof AssistantFormData] == null) {
      ;(fd as any)[k] = v
    }
  }
  for (const [k, v] of Object.entries(QWEN38_NON_THINKING_DEFAULTS)) {
    if (fd[k as keyof AssistantFormData] == null) {
      ;(fd as any)[k] = v
    }
  }
  const q38 = providerConfigs.value['qwen3.8_27b']
  if (!fd.custom_api_url && q38?.base_url) fd.custom_api_url = q38.base_url
  if (!fd.custom_model_name && q38?.model_name) fd.custom_model_name = q38.model_name
}

function applyProviderDefaults() {
  const pt = formData.value.provider_type || 'deepseek'
  const cfg = providerConfigs.value[pt]
  if (pt === 'qwen3.8_vllm') {
    applyQwen38Defaults()
    return
  }
  if (!cfg) return

  if (pt === 'deepseek') {
    // For deepseek, pre-fill model name from config if empty
    if (!formData.value.custom_model_name && cfg.model_name) {
      formData.value.custom_model_name = cfg.model_name
    }
  } else if (pt === 'mimo') {
    // For MiMo, pre-fill base URL and model name from config if empty
    if (!formData.value.custom_model_name && cfg.model_name) {
      formData.value.custom_model_name = cfg.model_name
    }
  } else if (pt === 'custom') {
    // For custom, clear built-in defaults
    if (!formData.value.custom_api_url) {
      formData.value.custom_api_url = ''
    }
  }
}

function onProviderChange() {
  // Clear fields when switching providers to avoid confusion
  formData.value.custom_api_url = null
  formData.value.custom_api_key = null
  formData.value.custom_model_name = null
  formData.value.extra_body = null
  applyProviderDefaults()
}

watch(() => props.visible, (newVal) => {
  if (newVal && props.assistant) {
    formData.value = {
      name: props.assistant.name,
      system_prompt: props.assistant.system_prompt,
      temperature: props.assistant.temperature ?? null,
      top_p: props.assistant.top_p ?? null,
      top_k: props.assistant.top_k ?? null,
      presence_penalty: props.assistant.presence_penalty ?? null,
      frequency_penalty: props.assistant.frequency_penalty ?? null,
      max_tokens: props.assistant.max_tokens ?? null,
      use_custom_model: props.assistant.use_custom_model ?? false,
      custom_api_url: props.assistant.custom_api_url ?? null,
      custom_api_key: props.assistant.custom_api_key ?? null,
      custom_model_name: props.assistant.custom_model_name ?? null,
      provider_type: props.assistant.provider_type ?? 'deepseek',
      extra_body: props.assistant.extra_body ?? null,
      use_subtask_model: props.assistant.use_subtask_model ?? false,
      subtask_custom_api_url: props.assistant.subtask_custom_api_url ?? null,
      subtask_custom_api_key: props.assistant.subtask_custom_api_key ?? null,
      subtask_custom_model_name: props.assistant.subtask_custom_model_name ?? null,
      subtask_provider_type: props.assistant.subtask_provider_type ?? null,
      subtask_extra_body: props.assistant.subtask_extra_body ?? null,
      thinking_budget: props.assistant.thinking_budget ?? null,
      min_p: props.assistant.min_p ?? null,
      repetition_penalty: props.assistant.repetition_penalty ?? null,
      thinking_temperature: props.assistant.thinking_temperature ?? null,
      thinking_top_p: props.assistant.thinking_top_p ?? null,
      thinking_top_k: props.assistant.thinking_top_k ?? null,
      thinking_min_p: props.assistant.thinking_min_p ?? null,
      thinking_presence_penalty: props.assistant.thinking_presence_penalty ?? null,
      thinking_repetition_penalty: props.assistant.thinking_repetition_penalty ?? null,
      preserve_thinking: props.assistant.preserve_thinking ?? true,
    }
    if (formData.value.provider_type === 'qwen3.8_vllm') applyQwen38Defaults()
  } else if (newVal && !props.assistant) {
    formData.value = { ...defaultFormData }
    applyProviderDefaults()
  }
})

function handleReset() {
  formData.value = { ...defaultFormData }
  applyProviderDefaults()
}

function handleClose() {
  emit('close')
}

function handleSave() {
  if (!formData.value.name.trim()) return
  // Clean up empty strings to null
  const data = { ...formData.value }
  if (data.custom_api_url === '') data.custom_api_url = null
  if (data.custom_api_key === '') data.custom_api_key = null
  if (data.custom_model_name === '') data.custom_model_name = null
  if (data.extra_body === '') data.extra_body = null
  if (data.subtask_custom_api_url === '') data.subtask_custom_api_url = null
  if (data.subtask_custom_api_key === '') data.subtask_custom_api_key = null
  if (data.subtask_custom_model_name === '') data.subtask_custom_model_name = null
  if (data.subtask_extra_body === '') data.subtask_extra_body = null
  if (!data.use_subtask_model) {
    // Persist empty subtask fields when the toggle is off so old configs
    // don't accidentally re-enable themselves later.
    data.subtask_custom_api_url = null
    data.subtask_custom_api_key = null
    data.subtask_custom_model_name = null
    data.subtask_provider_type = null
    data.subtask_extra_body = null
  }
  emit('save', data)
}
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background-color: rgba(10, 18, 30, 0.28);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  animation: fadeIn 0.2s ease-out;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.modal {
  width: 520px;
  max-height: 85vh;
  background: var(--surface-panel-strong);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: 1px solid var(--panel-border);
  border-radius: 30px;
  box-shadow: var(--frame-shadow);
  display: flex;
  flex-direction: column;
  animation: scaleIn 0.2s ease-out;
}

@keyframes scaleIn {
  from {
    opacity: 0;
    transform: scale(0.95);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px;
  border-bottom: 1px solid var(--panel-border);
}

.modal-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  text-wrap: balance;
}

.close-btn {
  padding: 8px;
  color: var(--color-text-light);
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
}

.close-btn:hover {
  color: var(--color-text);
  background: var(--color-hover);
}

.close-btn:active {
  transform: scale(0.96);
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
}

.form-group {
  margin-bottom: 20px;
}

.form-label {
  display: block;
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
  margin-bottom: 8px;
}

.form-input {
  width: 100%;
  padding: 10px 14px;
  background-color: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  transition: background-color var(--transition-fast), border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.form-input:focus {
  background-color: var(--surface-panel-strong);
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(53, 133, 197, 0.10);
  outline: none;
}

.form-textarea {
  width: 100%;
  padding: 10px 14px;
  background-color: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  resize: vertical;
  min-height: 100px;
  transition: background-color var(--transition-fast), border-color var(--transition-fast), box-shadow var(--transition-fast);
  font-family: inherit;
}

.form-textarea:focus {
  background-color: var(--surface-panel-strong);
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(53, 133, 197, 0.10);
  outline: none;
}

.form-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--color-text-light);
}

.divider {
  height: 1px;
  background-color: var(--panel-border);
  margin: 24px 0;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 20px;
}

.param-group {
  margin-bottom: 24px;
}

.param-group:last-child {
  margin-bottom: 0;
}

.param-checkbox {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  color: var(--text-primary);
  cursor: pointer;
}

.param-checkbox input[type="checkbox"] {
  width: 16px;
  height: 16px;
  cursor: pointer;
}

.param-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
  margin-bottom: 10px;
}

.param-value {
  font-weight: 400;
  color: var(--color-primary);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.param-slider {
  width: 100%;
  height: 6px;
  appearance: none;
  background-color: var(--surface-panel-subtle);
  border-radius: 3px;
  cursor: pointer;
}

.param-slider::-webkit-slider-thumb {
  appearance: none;
  width: 18px;
  height: 18px;
  background-color: var(--color-primary);
  border-radius: 50%;
  cursor: pointer;
  transition: transform var(--transition-fast), background-color var(--transition-fast);
}

.param-slider::-webkit-slider-thumb:hover {
  transform: scale(1.1);
}

.param-input {
  width: 100%;
  padding: 10px 14px;
  background-color: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  transition: background-color var(--transition-fast), border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.param-input:focus {
  background-color: var(--surface-panel-strong);
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(53, 133, 197, 0.10);
  outline: none;
}

.param-input::placeholder {
  color: var(--color-text-light);
}

.param-input option {
  background: var(--surface-panel-strong);
  color: var(--color-text);
}

.custom-model-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 14px;
  color: var(--color-text);
}

.custom-model-toggle input {
  cursor: pointer;
  accent-color: var(--color-primary);
}

.param-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text-light);
}

.qwen38-section-hint {
  margin-top: -12px;
  margin-bottom: 20px;
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding: 16px 24px;
  border-top: 1px solid var(--panel-border);
}

.btn {
  padding: 10px 20px;
  border-radius: var(--radius-md);
  font-weight: 500;
  font-size: 14px;
  transition: background-color var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast), filter var(--transition-fast), opacity var(--transition-fast);
}

.btn:active {
  transform: scale(0.96);
}

.btn-secondary {
  background-color: var(--surface-panel-subtle);
  color: var(--color-text);
  border: 1px solid var(--panel-border);
}

.btn-secondary:hover {
  background-color: var(--color-hover);
  border-color: var(--panel-border-strong);
}

.btn-primary {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  border: 1px solid transparent;
}

.btn-primary:hover:not(:disabled) {
  filter: brightness(1.06);
  transform: translateY(-1px);
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 767px) {
  .modal-overlay {
    padding: 0;
    align-items: flex-end;
  }

  .modal {
    width: 100%;
    max-height: 90vh;
    max-height: 90dvh;
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    max-width: 100%;
    padding-bottom: env(safe-area-inset-bottom, 0);
  }
}

@media (max-width: 480px) {
  .modal-header {
    padding: 16px;
  }

  .modal-body {
    padding: 16px;
  }

  .modal-footer {
    padding: 12px 16px;
  }

  .param-group {
    margin-bottom: 20px;
  }

.batch-actions {
  display: flex;
  gap: 8px;
}

.btn-batch-export {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-primary);
  background: rgba(53, 133, 197, 0.08);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.btn-batch-export:hover {
  background: rgba(53, 133, 197, 0.14);
}

.btn-batch-export:active {
  transform: scale(0.96);
}

.btn-batch-delete {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-error);
  background: rgba(229, 62, 62, 0.08);
  border: 1px solid var(--color-error);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.btn-batch-delete:hover {
  background: rgba(229, 62, 62, 0.14);
}

.btn-batch-delete:active {
  transform: scale(0.96);
}

.btn {
    padding: 10px 16px;
    font-size: 13px;
  }
}
</style>
