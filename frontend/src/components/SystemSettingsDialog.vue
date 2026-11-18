<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <Transition name="system-settings">
      <div v-if="visible" class="system-settings-overlay" @click.self="close">
        <div class="system-settings-card" :class="{ 'system-settings-card--projects': activeTab === 'projects' }">
          <div class="system-settings-header">
            <h2 class="system-settings-title">系统设置</h2>
            <button class="system-settings-close" @click="close">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>

          <div class="system-settings-body">
            <div class="settings-tabs">
              <button
                v-for="tab in tabs"
                :key="tab.key"
                class="settings-tab"
                :class="{ active: activeTab === tab.key }"
                @click="activeTab = tab.key"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <template v-if="tab.key === 'user-info'">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                    <circle cx="12" cy="7" r="4"/>
                  </template>
                  <template v-else-if="tab.key === 'model-provider'">
                    <rect x="2" y="2" width="20" height="8" rx="2" ry="2"/>
                    <rect x="2" y="14" width="20" height="8" rx="2" ry="2"/>
                    <line x1="6" y1="6" x2="6.01" y2="6"/>
                    <line x1="6" y1="18" x2="6.01" y2="18"/>
                  </template>
                  <template v-else-if="tab.key === 'hotwords'">
                    <path d="M12 2a8 8 0 0 0-8 8c0 3.4 2.1 6.3 5 7.5V20h6v-2.5c2.9-1.2 5-4.1 5-7.5a8 8 0 0 0-8-8z"/>
                    <line x1="9" y1="23" x2="15" y2="23"/>
                  </template>
                  <template v-else-if="tab.key === 'permissions'">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                  </template>
                  <template v-else-if="tab.key === 'skills'">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                    <polyline points="14 2 14 8 20 8"/>
                    <line x1="12" y1="18" x2="12" y2="12"/>
                    <line x1="9" y1="15" x2="15" y2="15"/>
                  </template>
                  <template v-else-if="tab.key === 'mcp'">
                    <rect x="2" y="7" width="20" height="10" rx="2"/>
                    <line x1="7" y1="7" x2="7" y2="17"/>
                    <line x1="12" y1="7" x2="12" y2="17"/>
                    <line x1="17" y1="7" x2="17" y2="17"/>
                  </template>
                  <template v-else-if="tab.key === 'memory'">
                    <path d="M12 2a7 7 0 0 0-7 7c0 2.4 1.2 4.5 3 5.7V17a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-2.3c1.8-1.2 3-3.3 3-5.7a7 7 0 0 0-7-7z"/>
                    <line x1="9" y1="22" x2="15" y2="22"/>
                  </template>
                  <template v-else-if="tab.key === 'device-exec'">
                    <rect x="2" y="3" width="20" height="14" rx="2"/>
                    <line x1="8" y1="21" x2="16" y2="21"/>
                    <line x1="12" y1="17" x2="12" y2="21"/>
                  </template>
                  <template v-else-if="tab.key === 'editor'">
                    <path d="M12 20h9"/>
                    <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
                  </template>
                  <template v-else-if="tab.key === 'projects'">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                  </template>
                  <template v-else-if="tab.key === 'skins'">
                    <circle cx="13.5" cy="6.5" r=".5"/>
                    <circle cx="17.5" cy="10.5" r=".5"/>
                    <circle cx="8.5" cy="7.5" r=".5"/>
                    <circle cx="6.5" cy="12.5" r=".5"/>
                    <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/>
                  </template>
                </svg>
                {{ tab.label }}
              </button>
            </div>

            <div class="settings-tab-content">
              <div v-if="activeTab === 'user-info'" class="tab-panel">
                <UserInfoPanel embedded />
              </div>

              <div v-else-if="activeTab === 'model-provider'" class="tab-panel">
                <ModelProviderPanel embedded />
              </div>

              <div v-else-if="activeTab === 'hotwords'" class="tab-panel">
                <AsrHotwordsPanel
                  embedded
                  @close="close"
                  @save="handleHotwordsSave"
                />
              </div>

              <div v-else-if="activeTab === 'permissions'" class="tab-panel">
                <PermissionSettingsPanel
                  embedded
                  :permissions="permissions"
                  :saving="saving"
                  @close="close"
                  @save="handleSavePermissions"
                />
              </div>

              <div v-else-if="activeTab === 'skills'" class="tab-panel">
                <SkillsPanel
                  ref="skillsPanelRef"
                  embedded
                  @close="close"
                  @updated="handleSkillsUpdated"
                />
              </div>

              <div v-else-if="activeTab === 'mcp'" class="tab-panel">
                <McpServerPanel embedded />
              </div>

              <div v-else-if="activeTab === 'memory'" class="tab-panel">
                <MemoryPanel
                  embedded
                  @close="close"
                />
              </div>

              <div v-else-if="activeTab === 'projects'" class="tab-panel tab-panel--projects">
                <WorkspaceProjectsDialog mode="inline" @close="close" />
              </div>

              <div v-else-if="activeTab === 'device-exec'" class="tab-panel">
                <DeviceExecutionPanel embedded />
              </div>

              <div v-else-if="activeTab === 'editor'" class="tab-panel">
                <div class="editor-prefs-panel">
                  <div class="editor-pref-row">
                    <div class="editor-pref-text">
                      <div class="editor-pref-title">尾注功能</div>
                      <div class="editor-pref-desc">
                        开启后，笔记与消息中的 [^1] 与 [^1]: 语法渲染为可跳转尾注
                        （角标〔1〕↔ 文末注释双向跳转），工具栏出现「尾注」按钮；
                        关闭时 [^1] 保持普通文字显示。
                      </div>
                    </div>
                    <button
                      class="editor-pref-toggle"
                      :class="{ on: editorPrefs.endnoteEnabled }"
                      role="switch"
                      :aria-checked="editorPrefs.endnoteEnabled"
                      @click="editorPrefs.setEndnote(!editorPrefs.endnoteEnabled)"
                    >
                      <span class="toggle-knob"></span>
                    </button>
                  </div>
                </div>
              </div>

              <div v-else-if="activeTab === 'skins'" class="tab-panel">
                <SkinPanel
                  embedded
                  @close="close"
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useEditorPrefsStore } from '@/stores/editorPrefs'
import AsrHotwordsPanel from './AsrHotwordsPanel.vue'
import DeviceExecutionPanel from './DeviceExecutionPanel.vue'
import PermissionSettingsPanel from './PermissionSettingsPanel.vue'
import SkillsPanel from './SkillsPanel.vue'
import MemoryPanel from './MemoryPanel.vue'
import WorkspaceProjectsDialog from './WorkspaceProjectsDialog.vue'
import SkinPanel from './SkinPanel.vue'
import UserInfoPanel from './UserInfoPanel.vue'
import ModelProviderPanel from './ModelProviderPanel.vue'
import McpServerPanel from './McpServerPanel.vue'
import type { HotwordItem } from '@/api/asr'

interface Permissions {
  terminal_execution: boolean
  note_create: boolean
  note_edit: boolean
  note_delete: boolean
  notebook_create: boolean
  notebook_edit: boolean
  notebook_delete: boolean
}

const props = defineProps<{
  visible: boolean
  permissions: Permissions
  saving?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'permissions-save', value: Permissions): void
  (e: 'hotwords-save', value: HotwordItem[]): void
  (e: 'skills-updated'): void
}>()

const activeTab = ref('hotwords')
const editorPrefs = useEditorPrefsStore()
const skillsPanelRef = ref<InstanceType<typeof SkillsPanel> | null>(null)

const tabs = [
  { key: 'user-info', label: '用户信息' },
  { key: 'model-provider', label: '模型供应商' },
  { key: 'hotwords', label: '热词配置' },
  { key: 'permissions', label: '权限管理' },
  { key: 'skills', label: '技能管理' },
  { key: 'mcp', label: 'MCP' },
  { key: 'memory', label: '记忆管理' },
  { key: 'projects', label: '项目管理' },
  { key: 'skins', label: '皮肤选择' },
  { key: 'device-exec', label: '设备执行' },
  { key: 'editor', label: '编辑器' },
]

function close() {
  emit('close')
}

function handleHotwordsSave(hotwords: HotwordItem[]) {
  emit('hotwords-save', hotwords)
}

function handleSavePermissions(perms: Permissions) {
  emit('permissions-save', perms)
}

function handleSkillsUpdated() {
  emit('skills-updated')
}
</script>

<style scoped>
/* 编辑器偏好（wave-2 B1）*/
.editor-prefs-panel { padding: 8px 4px; }
.editor-pref-row {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 16px; border: 1px solid var(--panel-border); border-radius: var(--radius-md);
  background-color: var(--surface-panel);
}
.editor-pref-title { font-size: 14px; font-weight: 600; color: var(--color-text); margin-bottom: 4px; }
.editor-pref-desc { font-size: 12px; color: var(--color-text-light); line-height: 1.6; max-width: 520px; }
.editor-pref-toggle {
  position: relative; width: 44px; height: 24px; flex-shrink: 0;
  border: 1px solid var(--panel-border-strong); border-radius: 12px;
  background-color: var(--color-hover); cursor: pointer;
  transition: background-color var(--transition-fast);
}
.editor-pref-toggle.on { background-color: var(--color-primary, #2340b8); }
.editor-pref-toggle .toggle-knob {
  position: absolute; top: 2px; left: 2px; width: 18px; height: 18px;
  border-radius: 50%; background: var(--surface-panel-strong);
  transition: transform var(--transition-fast);
}
.editor-pref-toggle.on .toggle-knob { transform: translateX(20px); }

.system-settings-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10003;
  padding: 20px;
}

.system-settings-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow), 0 20px 40px rgba(0, 0, 0, 0.15);
  border-radius: 24px;
  width: 100%;
  max-width: 720px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.system-settings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px;
  border-bottom: 1px solid var(--panel-border);
}

.system-settings-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.system-settings-close {
  background: transparent;
  border: none;
  color: var(--color-text-light);
  cursor: pointer;
  padding: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  transition: color 0.15s ease, background 0.15s ease;
}

.system-settings-close:hover {
  color: var(--color-text);
  background: var(--surface-panel-subtle);
}

.system-settings-body {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex: 1;
  min-height: 0;
}

.settings-tabs {
  display: flex;
  gap: 8px;
  padding: 16px 24px 0;
  border-bottom: 1px solid var(--panel-border);
  overflow-x: auto;
}

.settings-tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  border: none;
  background: transparent;
  color: var(--color-text-light);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color 0.15s ease, border-color 0.15s ease;
  white-space: nowrap;
}

.settings-tab:hover {
  color: var(--color-text);
}

.settings-tab.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}

.settings-tab-content {
  flex: 1;
  min-height: 0; /* 移动端单滚动链：允许内容区收缩，否则子面板（项目管理）会顶出卡片底部 */
  overflow-y: auto;
  padding: 20px 24px 24px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.tab-panel {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  animation: tab-fade-in 0.2s ease;
}

/* 项目管理 tab：撑满内容区（单卡片分栏组件需要确定高度；设置卡只有 max-height，
   百分比高度会退化成 auto，因此这里给确定高度） */
.tab-panel--projects {
  align-items: stretch;
  flex: 1;
  height: min(640px, 68vh);
  min-height: 460px;
}

/* 「项目管理」是双栏管理器，720px 的通用设置卡放不下（右栏滚到底仍被卡片圆角切掉）。
   这个 tab 单独放宽卡片，并把高度按视口给足，底部（危险区 / 删除按钮）才完整可见。 */
.system-settings-card--projects {
  max-width: min(1080px, 94vw);
  max-height: 92vh;
}
.system-settings-card--projects .system-settings-header {
  padding: 16px 24px;
}
.system-settings-card--projects .settings-tab-content {
  padding: 16px 20px 20px;
}
.system-settings-card--projects .tab-panel--projects {
  height: 100%;
  min-height: 520px;
}
@media (max-width: 767px) {
  .tab-panel--projects {
    height: 60vh;
    min-height: 320px;
  }
}

@keyframes tab-fade-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.system-settings-enter-active {
  transition: opacity 0.2s ease;
}
.system-settings-enter-active .system-settings-card {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.system-settings-leave-active {
  transition: opacity 0.15s ease;
}
.system-settings-leave-active .system-settings-card {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.system-settings-enter-from {
  opacity: 0;
}
.system-settings-enter-from .system-settings-card {
  transform: scale(0.95);
  opacity: 0;
}
.system-settings-leave-to {
  opacity: 0;
}
.system-settings-leave-to .system-settings-card {
  transform: scale(0.95);
  opacity: 0;
}

@media (max-width: 767px) {
  .system-settings-card {
    max-width: 100%;
    max-height: 100%;
    border-radius: 0;
  }

  /* 2026-11-17（真机反馈）：全屏浮层必须避让系统栏——Android 壳注入的
     --safe-top/--safe-bottom 是状态栏/手势条高度；padding 为 0 时卡片标题栏
     与 ✕ 会落在状态栏下面，点击被系统手势接管（「关不掉」）。 */
  .system-settings-overlay {
    padding: var(--safe-top, 0px) 0 var(--safe-bottom, 0px);
  }

  /* 项目管理：移动端单滚动链。卡片填满安全区，内容区不滚、由任务详情的
     文件 pane 独占滚动——否则「height:100% + min-height:520px」的 tab 面板
     会把 pane 底部顶出视口，外层又因 scrollHeight==clientHeight 无法补偿
     （实测最后一行永远差 8–20px）。 */
  .system-settings-card--projects {
    height: 100%;
  }
  .system-settings-card--projects .settings-tab-content {
    overflow: hidden;
  }
  .system-settings-card--projects .tab-panel--projects {
    height: auto;
    min-height: 0;
    flex: 1 1 auto;
  }
}
</style>
