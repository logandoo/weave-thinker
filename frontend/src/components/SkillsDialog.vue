<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <Transition name="skills-dialog">
      <div v-if="visible" class="skills-overlay" @click.self="close">
        <div class="skills-card">
          <SkillsPanel ref="panelRef" embedded @close="close" @updated="updated" />
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import SkillsPanel from './SkillsPanel.vue'

const props = defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'updated'): void
}>()

const panelRef = ref<InstanceType<typeof SkillsPanel> | null>(null)

function close() {
  panelRef.value?.cancelForm()
  panelRef.value?.cancelDelete()
  panelRef.value?.cancelUpload()
  emit('close')
}

function updated() {
  emit('updated')
}
</script>

<style scoped>
.skills-overlay {
  position: fixed;
  inset: 0;
  background: rgba(24, 18, 14, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10003;
  padding: 20px;
}

.skills-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow), 0 20px 40px rgba(0, 0, 0, 0.15);
  border-radius: 24px;
  padding: 24px;
  width: 100%;
  max-width: 720px;
  max-height: 90vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.skills-dialog-enter-active {
  transition: opacity 0.2s ease;
}
.skills-dialog-enter-active .skills-card {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.skills-dialog-leave-active {
  transition: opacity 0.15s ease;
}
.skills-dialog-leave-active .skills-card {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.skills-dialog-enter-from {
  opacity: 0;
}
.skills-dialog-enter-from .skills-card {
  transform: scale(0.95);
  opacity: 0;
}
.skills-dialog-leave-to {
  opacity: 0;
}
.skills-dialog-leave-to .skills-card {
  transform: scale(0.95);
  opacity: 0;
}

@media (max-width: 767px) {
  .skills-card {
    max-width: 100%;
    max-height: 100%;
    border-radius: 0;
  }

  .skills-overlay {
    padding: 0;
  }
}
</style>
