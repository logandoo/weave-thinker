<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div v-if="thinking && thinking.status === 'running'" class="sub-agent-status">
    <div class="thinking-indicator">
      <div class="pulse-dot"></div>
      <span class="thinking-text">{{ thinking.content }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { SubAgentThinking } from '@/types'

defineProps<{
  thinking: SubAgentThinking | null
}>()
</script>

<style scoped>
.sub-agent-status {
  padding: 8px 12px;
  margin: 4px 0;
}

.thinking-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: var(--surface-panel-subtle);
  border-radius: var(--radius-sm);
  border-left: 3px solid var(--color-primary);
  border: 1px solid var(--panel-border);
  border-left: 3px solid var(--color-primary);
}

.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: pulse 1.2s ease-in-out infinite;
  flex-shrink: 0;
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.2); }
}

.thinking-text {
  font-size: 13px;
  color: var(--color-text-light);
  line-height: 1.4;
}
</style>
