<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-overlay" @mousedown.self="$emit('close')">
      <div class="modal-content hotwords-dialog" @click.stop>
        <AsrHotwordsPanel @close="$emit('close')" @save="$emit('save', $event)" />
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import AsrHotwordsPanel from './AsrHotwordsPanel.vue'
import type { HotwordItem } from '@/api/asr'

defineProps<{
  visible: boolean
}>()

defineEmits<{
  close: []
  save: [hotwords: HotwordItem[]]
}>()
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background-color: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.hotwords-dialog {
  max-width: 480px;
  width: 100%;
  background: var(--bg-primary, #fff);
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
}
</style>
