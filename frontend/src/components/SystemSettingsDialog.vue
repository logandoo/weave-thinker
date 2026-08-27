<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <Transition name="system-settings">
      <div v-if="visible" class="system-settings-overlay" @click.self="close">
        <div class="system-settings-card">
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
                  <template v-if="tab.key === 'hotwords'">
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
                  <template v-else-if="tab.key === 'memory'">
                    <path d="M12 2a7 7 0 0 0-7 7c0 2.4 1.2 4.5 3 5.7V17a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-2.3c1.8-1.2 3-3.3 3-5.7a7 7 0 0 0-7-7z"/>
                    <line x1="9" y1="22" x2="15" y2="22"/>
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
              <div v-if="activeTab === 'hotwords'" class="tab-panel">
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

              <div v-else-if="activeTab === 'memory'" class="tab-panel">
                <MemoryPanel
                  embedded
                  @close="close"
                />
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
import AsrHotwordsPanel from './AsrHotwordsPanel.vue'
import PermissionSettingsPanel from './PermissionSettingsPanel.vue'
import SkillsPanel from './SkillsPanel.vue'
import MemoryPanel from './MemoryPanel.vue'
import SkinPanel from './SkinPanel.vue'
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
const skillsPanelRef = ref<InstanceType<typeof SkillsPanel> | null>(null)

const tabs = [
  { key: 'hotwords', label: '热词配置' },
  { key: 'permissions', label: '权限管理' },
  { key: 'skills', label: '技能管理' },
  { key: 'memory', label: '记忆管理' },
  { key: 'skins', label: '皮肤选择' },
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

  .system-settings-overlay {
    padding: 0;
  }
}
</style>
