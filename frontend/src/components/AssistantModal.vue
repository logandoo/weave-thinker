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
              <button class="btn btn-batch-import" @click="importInputRef?.click()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="17 8 12 3 7 8"/>
                  <line x1="12" y1="3" x2="12" y2="15"/>
                </svg>
                批量导入对话
              </button>
              <button class="btn btn-batch-delete" @click="emit('batch-delete')">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
                批量删除会话
              </button>
            </div>
            <input
              ref="importInputRef"
              class="batch-import-input"
              type="file"
              accept=".csv,.zip"
              multiple
              @change="onImportFilesChange"
            />
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

          <div class="section-title">模型</div>

          <div class="param-group">
            <label class="param-label">主模型</label>
            <select v-model="formData.model_alias" class="param-input" :disabled="!llmAliases.length">
              <option v-if="!llmAliases.length" :value="formData.model_alias">默认模型</option>
              <option v-if="!formData.model_alias" value="">系统默认（旧配置）</option>
              <option v-else-if="!llmAliases.some(a => a.alias === formData.model_alias)" :value="formData.model_alias">{{ formData.model_alias }}（旧配置）</option>
              <option v-for="a in llmAliases" :key="a.alias" :value="a.alias">{{ a.display_name }}（{{ a.alias }}）</option>
            </select>
            <div class="param-hint">可选模型由服务器统一管理（逻辑别名）；思考/推理强度可在输入区按消息调整</div>
          </div>

          <div class="param-group">
            <label class="param-label">Subagent 任务模型</label>
            <select v-model="formData.subtask_model_alias" class="param-input" :disabled="!llmAliases.length">
              <option value="">跟随主模型</option>
              <option v-for="a in llmAliases" :key="a.alias" :value="a.alias">{{ a.display_name }}（{{ a.alias }}）</option>
            </select>
            <div class="param-hint">为子任务（工具迭代、搜索决策等）使用独立模型；不选时复用主模型并自动关闭 thinking</div>
          </div>
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
import type { Assistant, AssistantFormData, ModelAlias } from '@/types'
import { modelsApi } from '@/api/models'

const props = defineProps<{
  visible: boolean
  assistant?: Assistant | null
}>()

const emit = defineEmits<{
  close: []
  save: [data: AssistantFormData]
  'batch-export': []
  'batch-import': [files: File[]]
  'batch-delete': []
}>()

const importInputRef = ref<HTMLInputElement | null>(null)

function onImportFilesChange(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (files.length) emit('batch-import', files)
  input.value = ''
}

const aliases = ref<ModelAlias[]>([])
const defaultAlias = ref('')

const llmAliases = computed(() => aliases.value.filter(a => a.kind === 'llm'))

const defaultFormData: AssistantFormData = {
  name: '',
  system_prompt: '',
  model_alias: '',
  subtask_model_alias: '',
}

const formData = ref<AssistantFormData>({ ...defaultFormData })

const isEdit = computed(() => !!props.assistant)

onMounted(async () => {
  try {
    const res = await modelsApi.getModels()
    aliases.value = res.aliases
    defaultAlias.value = res.default_alias || ''
    defaultFormData.model_alias = defaultAlias.value
    if (!formData.value.model_alias) formData.value.model_alias = defaultAlias.value
  } catch (e) {
    console.error('Failed to load model aliases:', e)
  }
})

watch(() => props.visible, (newVal) => {
  if (newVal && props.assistant) {
    formData.value = {
      name: props.assistant.name,
      system_prompt: props.assistant.system_prompt,
      model_alias: props.assistant.model_alias || defaultAlias.value,
      subtask_model_alias: props.assistant.subtask_model_alias || '',
    }
  } else if (newVal && !props.assistant) {
    formData.value = { ...defaultFormData }
  }
})

function handleReset() {
  formData.value = { ...defaultFormData }
}

function handleClose() {
  emit('close')
}

function handleSave() {
  if (!formData.value.name.trim()) return
  emit('save', { ...formData.value })
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

.batch-actions {
  display: flex;
  flex-wrap: wrap;
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

.btn-batch-import {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-primary-dark, var(--color-primary));
  background: var(--surface-panel-subtle);
  border: 1px solid var(--panel-border-strong, var(--panel-border));
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--transition-fast), transform var(--transition-fast);
}

.btn-batch-import:hover {
  background: var(--color-hover);
}

.btn-batch-import:active {
  transform: scale(0.96);
}

.batch-import-input {
  display: none;
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

  .btn {
    padding: 10px 16px;
    font-size: 13px;
  }
}
</style>
