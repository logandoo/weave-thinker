<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div
    v-if="visible"
    class="drawer-overlay"
    :class="{ visible }"
    @click="$emit('close')"
  ></div>
</template>

<script setup lang="ts">
defineProps<{
  visible: boolean
}>()

defineEmits<{
  close: []
}>()
</script>

<style scoped>
.drawer-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(10, 18, 30, 0.18);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  z-index: 998;
  animation: fadeIn 0.2s ease-out;
}

.drawer-overlay.visible {
  display: block;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>