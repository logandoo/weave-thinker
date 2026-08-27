<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="skills-dialog">
    <div class="skills-header">
      <h2>技能管理</h2>
      <button v-if="!embedded" class="close-btn" @click="close">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"></line>
          <line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>

    <div class="skills-content">
      <div class="skills-toolbar">
        <button class="toolbar-btn primary" @click="showCreateDialog = true">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="12" y1="5" x2="12" y2="19"></line>
            <line x1="5" y1="12" x2="19" y2="12"></line>
          </svg>
          新建技能
        </button>
        <button class="toolbar-btn" @click="triggerUpload">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <polyline points="17 8 12 3 7 8"></polyline>
            <line x1="12" y1="3" x2="12" y2="15"></line>
          </svg>
          上传文件
        </button>
        <button class="toolbar-btn" @click="triggerFolderUpload">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
          </svg>
          上传文件夹
        </button>
        <input
          ref="fileInput"
          type="file"
          accept=".md,.zip"
          style="display: none"
          @change="handleFileUpload"
        />
        <input
          ref="folderInput"
          type="file"
          webkitdirectory
          directory
          style="display: none"
          @change="handleFolderUpload"
        />
      </div>

      <div v-if="loading" class="skills-loading">
        <div class="loading-spinner"></div>
        <span>加载中...</span>
      </div>

      <div v-else-if="skills.length === 0" class="skills-empty">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.5">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <polyline points="14 2 14 8 20 8"></polyline>
          <line x1="12" y1="18" x2="12" y2="12"></line>
          <line x1="9" y1="15" x2="15" y2="15"></line>
        </svg>
        <p>暂无自定义技能</p>
        <p class="hint">点击"新建技能"或上传 .md / .zip / 文件夹</p>
      </div>

      <div v-else class="skills-list">
        <div
          v-for="skill in skills"
          :key="skill.id"
          class="skill-item"
          :class="{ inactive: !skill.is_active, 'system-skill': skill.source === 'system' }"
        >
          <div class="skill-info" @click="skill.source === 'system' ? viewSystemSkill(skill) : editSkill(skill)">
            <div class="skill-name">
              <span class="name">{{ skill.name }}</span>
              <span v-if="skill.source === 'system'" class="badge system">系统</span>
              <span v-else-if="!skill.is_active" class="badge inactive">已禁用</span>
            </div>
            <div v-if="skill.description" class="skill-desc">{{ skill.description }}</div>
          </div>
          <div class="skill-actions">
            <template v-if="skill.source === 'system'">
              <button class="action-btn" title="查看（系统技能只读）" @click="viewSystemSkill(skill)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                  <circle cx="12" cy="12" r="3"></circle>
                </svg>
              </button>
            </template>
            <template v-else>
              <button
                class="action-btn"
                :title="skill.is_active ? '禁用' : '启用'"
                @click="toggleSkill(skill)"
              >
                <svg v-if="skill.is_active" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                  <circle cx="12" cy="12" r="3"></circle>
                </svg>
                <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path>
                  <line x1="1" y1="1" x2="23" y2="23"></line>
                </svg>
              </button>
              <button class="action-btn" title="编辑" @click="editSkill(skill)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                  <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                </svg>
              </button>
              <button class="action-btn danger" title="删除" @click="confirmDelete(skill)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="3 6 5 6 21 6"></polyline>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                  <line x1="10" y1="11" x2="10" y2="17"></line>
                  <line x1="14" y1="11" x2="14" y2="17"></line>
                </svg>
              </button>
            </template>
          </div>
        </div>
      </div>
    </div>

    <div v-if="showCreateDialog || editingSkill" class="skill-form-overlay" @click.self="cancelForm">
      <div class="skill-form">
        <h3>{{ editingSkill ? '编辑技能' : '新建技能' }}</h3>
        <div class="form-group">
          <label>技能名称</label>
          <input
            v-model="form.name"
            type="text"
            placeholder="例如: my-workflow"
            :disabled="!!editingSkill"
          />
        </div>
        <div class="form-group">
          <label>描述（可选）</label>
          <input
            v-model="form.description"
            type="text"
            placeholder="简短描述技能用途"
          />
        </div>
        <div class="form-group">
          <label>技能内容</label>
          <textarea
            v-model="form.content"
            placeholder="输入技能的 Markdown 内容..."
            rows="15"
          ></textarea>
        </div>
        <div class="form-actions">
          <button class="cancel-btn" @click="cancelForm">取消</button>
          <button class="save-btn" @click="saveSkill" :disabled="saving">
            {{ saving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="showDeleteConfirm" class="skill-form-overlay" @click.self="cancelDelete">
      <div class="skill-form confirm-dialog">
        <h3>确认删除</h3>
        <p>确定要删除技能 <strong>{{ deletingSkill?.name }}</strong> 吗？此操作不可撤销。</p>
        <div class="form-actions">
          <button class="cancel-btn" @click="cancelDelete">取消</button>
          <button class="save-btn danger" @click="deleteSkill">删除</button>
        </div>
      </div>
    </div>

    <div v-if="viewingSystemSkill" class="skill-form-overlay" @click.self="closeSystemView">
      <div class="skill-form system-view-dialog">
        <h3>
          <span class="badge system">系统</span>
          {{ viewingSystemSkill.name }}
        </h3>
        <div v-if="viewingSystemSkill.description" class="system-view-desc">
          {{ viewingSystemSkill.description }}
        </div>
        <div class="system-view-content">
          <pre>{{ viewingSystemSkill.content }}</pre>
        </div>
        <div class="form-actions">
          <button class="cancel-btn" @click="closeSystemView">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="showExecutableWarning" class="skill-form-overlay" @click.self="cancelUpload">
      <div class="skill-form executable-warning-dialog">
        <h3>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
            <line x1="12" y1="9" x2="12" y2="13"></line>
            <line x1="12" y1="17" x2="12.01" y2="17"></line>
          </svg>
          安全警告
        </h3>
        <div class="warning-content">
          <p class="warning-message">
            上传的 {{ pendingFile ? 'ZIP 文件' : '文件夹' }} 包含可执行文件，可能存在安全风险。
          </p>
          <div v-if="scanResult?.dangerous_count && scanResult.dangerous_count > 0" class="dangerous-warning">
            <p class="danger-text">
              检测到 <strong>{{ scanResult.dangerous_count }}</strong> 个高风险文件（二进制可执行文件）：
            </p>
            <ul class="file-list dangerous">
              <li v-for="w in scanResult.warnings.filter(w => w.is_dangerous)" :key="w.file_path">
                <span class="file-icon">[!]</span>
                <span class="file-path">{{ w.file_path }}</span>
                <span class="file-type">({{ w.file_type }})</span>
              </li>
            </ul>
          </div>
          <div v-if="scanResult?.executable_count && scanResult.executable_count > 0" class="executable-warning">
            <p class="exec-text">
              检测到 <strong>{{ scanResult.executable_count - (scanResult.dangerous_count || 0) }}</strong> 个可执行文件（脚本/源代码）：
            </p>
            <ul class="file-list">
              <li v-for="w in scanResult.warnings.filter(w => !w.is_dangerous)" :key="w.file_path">
                <span class="file-icon">[*]</span>
                <span class="file-path">{{ w.file_path }}</span>
                <span class="file-type">({{ w.file_type }})</span>
              </li>
            </ul>
          </div>
          <p class="warning-note">
            如果您信任这些文件的来源，可以继续上传。否则建议取消上传以确保安全。
          </p>
        </div>
        <div class="form-actions">
          <button class="cancel-btn" @click="cancelUpload">取消上传</button>
          <button class="save-btn warning" @click="forceUpload" :disabled="uploading">
            {{ uploading ? '上传中...' : '确认上传（接受风险）' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { skillsApi } from '@/api/skills'
import type { ScanZipResult } from '@/api/skills'
import type { Skill, SkillFormData } from '@/types'

const props = defineProps<{
  embedded?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'updated'): void
}>()

const skills = ref<Skill[]>([])
const loading = ref(false)
const saving = ref(false)
const uploading = ref(false)
const showCreateDialog = ref(false)
const editingSkill = ref<Skill | null>(null)
const viewingSystemSkill = ref<Skill | null>(null)
const showDeleteConfirm = ref(false)
const deletingSkill = ref<Skill | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const folderInput = ref<HTMLInputElement | null>(null)
const showExecutableWarning = ref(false)
const scanResult = ref<ScanZipResult | null>(null)
const pendingFile = ref<File | null>(null)
const pendingFiles = ref<File[] | null>(null)
const pendingPaths = ref<string[] | null>(null)

const form = ref<SkillFormData>({
  name: '',
  description: '',
  content: '',
})

watch(() => props.embedded, () => {
  loadSkills()
}, { immediate: true })

async function loadSkills() {
  loading.value = true
  try {
    skills.value = await skillsApi.getSkills()
  } catch (error) {
    console.error('Failed to load skills:', error)
  } finally {
    loading.value = false
  }
}

function editSkill(skill: Skill) {
  editingSkill.value = skill
  form.value = {
    name: skill.name,
    description: skill.description || '',
    content: skill.content,
  }
}

function viewSystemSkill(skill: Skill) {
  viewingSystemSkill.value = skill
}

function closeSystemView() {
  viewingSystemSkill.value = null
}

function cancelForm() {
  showCreateDialog.value = false
  editingSkill.value = null
  form.value = { name: '', description: '', content: '' }
}

async function saveSkill() {
  if (!form.value.name.trim() || !form.value.content.trim()) {
    return
  }

  saving.value = true
  try {
    if (editingSkill.value) {
      await skillsApi.updateSkill(editingSkill.value.id, {
        description: form.value.description || null,
        content: form.value.content,
      })
    } else {
      await skillsApi.createSkill({
        name: form.value.name.trim(),
        description: form.value.description || null,
        content: form.value.content,
      })
    }
    await loadSkills()
    cancelForm()
    emit('updated')
  } catch (error) {
    console.error('Failed to save skill:', error)
    alert('保存失败: ' + (error as any).response?.data?.detail || '未知错误')
  } finally {
    saving.value = false
  }
}

async function toggleSkill(skill: Skill) {
  try {
    await skillsApi.updateSkill(skill.id, { is_active: !skill.is_active })
    await loadSkills()
    emit('updated')
  } catch (error) {
    console.error('Failed to toggle skill:', error)
  }
}

function confirmDelete(skill: Skill) {
  deletingSkill.value = skill
  showDeleteConfirm.value = true
}

function cancelDelete() {
  showDeleteConfirm.value = false
  deletingSkill.value = null
}

async function deleteSkill() {
  if (!deletingSkill.value) return

  try {
    await skillsApi.deleteSkill(deletingSkill.value.id)
    await loadSkills()
    cancelDelete()
    emit('updated')
  } catch (error) {
    console.error('Failed to delete skill:', error)
  }
}

function triggerUpload() {
  fileInput.value?.click()
}

function triggerFolderUpload() {
  folderInput.value?.click()
}

async function handleFileUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  if (file.name.endsWith('.zip')) {
    try {
      const result = await skillsApi.scanZip(file)
      if (result.has_executables) {
        scanResult.value = result
        pendingFile.value = file
        showExecutableWarning.value = true
        input.value = ''
        return
      }
    } catch (error) {
      console.error('Failed to scan zip:', error)
    }
  }

  await performUpload(file)
  input.value = ''
}

async function handleFolderUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const files = input.files
  if (!files || files.length === 0) return

  const fileArray = Array.from(files)
  const paths = fileArray.map(file => file.webkitRelativePath || file.name)

  try {
    const result = await skillsApi.scanFolder(fileArray, paths)
    if (result.has_executables) {
      scanResult.value = result
      pendingFiles.value = fileArray
      pendingPaths.value = paths
      showExecutableWarning.value = true
      input.value = ''
      return
    }
  } catch (error) {
    console.error('Failed to scan folder:', error)
  }

  await performFolderUpload(fileArray, paths)
  input.value = ''
}

async function performUpload(file: File, force: boolean = false) {
  uploading.value = true
  try {
    await skillsApi.uploadSkills(file, force)
    await loadSkills()
    emit('updated')
    showExecutableWarning.value = false
    scanResult.value = null
    pendingFile.value = null
  } catch (error: any) {
    console.error('Failed to upload skills:', error)
    const detail = error.response?.data?.detail
    if (typeof detail === 'object' && detail.error) {
      alert('上传失败: ' + detail.message)
    } else {
      alert('上传失败: ' + (detail || '未知错误'))
    }
  } finally {
    uploading.value = false
  }
}

async function performFolderUpload(files: File[], paths: string[], force: boolean = false) {
  uploading.value = true
  try {
    await skillsApi.uploadSkillsFolder(files, paths, force)
    await loadSkills()
    emit('updated')
    showExecutableWarning.value = false
    scanResult.value = null
    pendingFiles.value = null
    pendingPaths.value = null
  } catch (error: any) {
    console.error('Failed to upload folder:', error)
    const detail = error.response?.data?.detail
    if (typeof detail === 'object' && detail.error) {
      alert('上传失败: ' + detail.message)
    } else {
      alert('上传失败: ' + (detail || '未知错误'))
    }
  } finally {
    uploading.value = false
  }
}

function forceUpload() {
  if (pendingFile.value) {
    performUpload(pendingFile.value, true)
  } else if (pendingFiles.value && pendingPaths.value) {
    performFolderUpload(pendingFiles.value, pendingPaths.value, true)
  }
}

function cancelUpload() {
  showExecutableWarning.value = false
  scanResult.value = null
  pendingFile.value = null
  pendingFiles.value = null
  pendingPaths.value = null
}

function close() {
  cancelForm()
  cancelDelete()
  cancelUpload()
  emit('close')
}
</script>

<style scoped>
.skills-dialog {
  width: 100%;
  max-width: 600px;
  display: flex;
  flex-direction: column;
  align-items: center;
  position: relative;
}

.skills-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  width: 100%;
}

.skills-header h2 {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
}

.close-btn {
  background: transparent;
  border: none;
  color: var(--color-text-light);
  cursor: pointer;
  padding: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  transition: color var(--transition-fast), background var(--transition-fast);
}

.close-btn:hover {
  color: var(--color-text);
  background: var(--surface-panel-subtle);
}

.skills-content {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.skills-toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
  justify-content: center;
  width: 100%;
  max-width: 480px;
}

.toolbar-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border: 1px solid var(--panel-border);
  border-radius: 16px;
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: transform var(--transition-fast), opacity var(--transition-fast), background var(--transition-fast);
}

.toolbar-btn:hover {
  background: var(--surface-panel-strong);
}

.toolbar-btn.primary {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  border-color: transparent;
  color: #fff;
  box-shadow: 0 8px 16px rgba(141, 104, 69, 0.18);
}

.toolbar-btn.primary:hover {
  opacity: 0.9;
}

.skills-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 40px;
  color: var(--color-text-light);
}

.loading-spinner {
  width: 20px;
  height: 20px;
  border: 2px solid var(--panel-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.skills-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px;
  color: var(--color-text-light);
}

.skills-empty p {
  margin: 8px 0 0;
}

.skills-empty .hint {
  font-size: 13px;
  opacity: 0.7;
  text-align: center;
}

.skills-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  max-width: 480px;
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  padding: 8px;
}

.skill-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: 8px;
  transition: background var(--transition-fast);
}

.skill-item:hover {
  background: var(--surface-panel-subtle);
}

.skill-item.inactive {
  opacity: 0.6;
}

.skill-info {
  flex: 1;
  cursor: pointer;
  min-width: 0;
}

.skill-name {
  display: flex;
  align-items: center;
  gap: 8px;
}

.skill-name .name {
  font-weight: 500;
  font-size: 14px;
  color: var(--color-text);
}

.badge {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--surface-panel-subtle);
  color: var(--color-text-light);
}

.badge.inactive {
  background: rgba(245, 158, 11, 0.12);
  color: #d97706;
}

.badge.system {
  background: rgba(59, 130, 246, 0.14);
  color: #2563eb;
}

.skill-item.system-skill {
  background: linear-gradient(180deg, rgba(59, 130, 246, 0.04), transparent);
}

.system-view-dialog {
  max-width: 720px;
  width: 90vw;
}

.system-view-dialog h3 {
  display: flex;
  align-items: center;
  gap: 8px;
}

.system-view-desc {
  font-size: 13px;
  color: var(--color-text-light);
  margin-bottom: 12px;
}

.system-view-content {
  max-height: 55vh;
  overflow: auto;
  background: var(--surface-panel-subtle, #f7f7f8);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
}

.system-view-content pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'SF Mono', 'Menlo', monospace;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text);
}

.skill-desc {
  font-size: 13px;
  color: var(--color-text-light);
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-actions {
  display: flex;
  gap: 4px;
  margin-left: 12px;
}

.action-btn {
  background: transparent;
  border: none;
  padding: 4px;
  cursor: pointer;
  color: var(--color-text-light);
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color var(--transition-fast), background var(--transition-fast);
}

.action-btn:hover {
  color: var(--color-text);
  background: var(--surface-panel-subtle);
}

.action-btn.danger:hover {
  color: #ef4444;
  background: rgba(239, 68, 68, 0.1);
}

/* 嵌套弹层约定（同 ConfirmDialog）：技能管理嵌入系统设置 tab（overflow:hidden 卡片内），
   absolute 定位会被 600px 面板盒裁剪，改为 viewport 级 fixed 全表面弹层 */
.skill-form-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20000;
  padding: 20px;
}

.skill-form {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  border-radius: 12px;
  padding: 24px;
  width: 100%;
  max-width: 560px;
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
}

@media (max-width: 767px) {
  .skill-form {
    max-width: 100%;
  }
}

.skill-form h3 {
  margin: 0 0 20px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.form-group {
  margin-bottom: 16px;
}

.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-light);
  margin-bottom: 6px;
}

.form-group input,
.form-group textarea {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--panel-border);
  border-radius: 8px;
  font-size: 14px;
  color: var(--color-text);
  background: var(--color-bg);
  transition: border-color var(--transition-fast);
  box-sizing: border-box;
}

.form-group input:focus,
.form-group textarea:focus {
  outline: none;
  border-color: var(--color-primary);
}

.form-group input:disabled {
  background: var(--surface-panel-subtle);
  opacity: 0.7;
}

.form-group textarea {
  resize: vertical;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 13px;
  line-height: 1.5;
}

.form-actions {
  display: flex;
  justify-content: center;
  gap: 10px;
  margin-top: 20px;
}

.cancel-btn,
.save-btn {
  min-width: 86px;
  padding: 10px 20px;
  border-radius: 16px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--transition-fast), opacity var(--transition-fast), background var(--transition-fast);
  border: 1px solid transparent;
}

.cancel-btn {
  background: var(--surface-panel-subtle);
  border-color: var(--panel-border);
  color: var(--color-text);
}

.cancel-btn:hover {
  background: var(--surface-panel-strong);
}

.save-btn {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: #fff;
  box-shadow: 0 8px 16px rgba(141, 104, 69, 0.18);
}

.save-btn:hover {
  opacity: 0.9;
}

.save-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.save-btn.danger {
  background: #ef4444;
  box-shadow: none;
}

.save-btn.danger:hover {
  background: #dc2626;
  opacity: 1;
}

.confirm-dialog p {
  margin: 0 0 20px;
  color: var(--color-text);
  line-height: 1.5;
}

.executable-warning-dialog {
  max-width: 600px;
}

.executable-warning-dialog h3 {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #d97706;
}

.executable-warning-dialog h3 svg {
  flex-shrink: 0;
}

.warning-content {
  margin-bottom: 20px;
}

.warning-message {
  font-size: 15px;
  color: var(--color-text);
  margin-bottom: 16px;
  line-height: 1.5;
}

.dangerous-warning {
  background: rgba(239, 68, 68, 0.06);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}

.danger-text {
  color: #ef4444;
  font-weight: 500;
  margin: 0 0 10px;
}

.executable-warning {
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}

.exec-text {
  color: #d97706;
  font-weight: 500;
  margin: 0 0 10px;
}

.file-list {
  list-style: none;
  padding: 0;
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
}

.file-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px solid var(--panel-border);
  font-size: 13px;
}

.file-list li:last-child {
  border-bottom: none;
}

.file-list.dangerous li {
  color: #ef4444;
}

.file-icon {
  font-weight: bold;
  font-family: monospace;
  flex-shrink: 0;
}

.file-path {
  word-break: break-all;
}

.file-type {
  color: var(--color-text-light);
  font-size: 12px;
  flex-shrink: 0;
}

.warning-note {
  font-size: 13px;
  color: var(--color-text-light);
  margin: 12px 0 0;
  line-height: 1.4;
}

.save-btn.warning {
  background: #d97706;
  box-shadow: none;
}

.save-btn.warning:hover {
  background: #b45309;
  opacity: 1;
}

.save-btn.warning:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
