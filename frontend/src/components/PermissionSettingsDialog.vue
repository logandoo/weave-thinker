<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <Transition name="perm-settings">
      <div v-if="visible" class="perm-settings-overlay" @click.self="onClose">
        <div class="perm-settings-card">
          <PermissionSettingsPanel
            :permissions="permissions"
            :saving="saving"
            @close="onClose"
            @save="onSave"
          />
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import PermissionSettingsPanel from './PermissionSettingsPanel.vue'

interface Permissions {
  terminal_execution: boolean
  note_create: boolean
  note_edit: boolean
  note_delete: boolean
}

const props = defineProps<{
  visible: boolean
  permissions: Permissions
  saving?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'save', value: Permissions): void
}>()

const local = ref<Permissions>({ ...props.permissions })

watch(() => props.permissions, (val) => {
  local.value = { ...val }
}, { deep: true })

watch(() => props.visible, (visible) => {
  if (visible) {
    local.value = { ...props.permissions }
  }
})

function onClose() {
  emit('close')
}

function onSave(value: Permissions) {
  emit('save', value)
}
</script>

<style scoped>
.perm-settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(24, 18, 14, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10002;
  padding: 20px;
}

.perm-settings-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow), 0 20px 40px rgba(0, 0, 0, 0.15);
  border-radius: 24px;
  padding: 24px;
  max-width: 420px;
  width: 100%;
}

.perm-settings-enter-active {
  transition: opacity 0.2s ease;
}
.perm-settings-enter-active .perm-settings-card {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.perm-settings-leave-active {
  transition: opacity 0.15s ease;
}
.perm-settings-leave-active .perm-settings-card {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.perm-settings-enter-from {
  opacity: 0;
}
.perm-settings-enter-from .perm-settings-card {
  transform: scale(0.95);
  opacity: 0;
}
.perm-settings-leave-to {
  opacity: 0;
}
.perm-settings-leave-to .perm-settings-card {
  transform: scale(0.95);
  opacity: 0;
}
</style>
