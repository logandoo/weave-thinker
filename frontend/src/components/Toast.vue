<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <TransitionGroup name="toast" tag="div" class="toast-container">
      <div
        v-for="toast in toasts"
        :key="toast.id"
        class="toast"
        :class="toast.type"
      >
        <div class="toast-main">
          <span class="toast-icon">
            <svg v-if="toast.type === 'success'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="20 6 9 17 4 12"/>
            </svg>
            <svg v-else-if="toast.type === 'error'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="10"/>
              <line x1="15" y1="9" x2="9" y2="15"/>
              <line x1="9" y1="9" x2="15" y2="15"/>
            </svg>
            <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="10"/>
              <line x1="12" y1="16" x2="12" y2="12"/>
              <line x1="12" y1="8" x2="12.01" y2="8"/>
            </svg>
          </span>
          <span class="toast-message">{{ toast.message }}</span>
        </div>
        <button
          v-if="toast.action"
          class="toast-action-btn"
          @click="triggerAction(toast.id)"
        >
          {{ toast.action.label }}
        </button>
      </div>
    </TransitionGroup>
  </Teleport>
</template>

<script setup lang="ts">
import { useToast } from '@/composables/useToast'

const { toasts, triggerAction } = useToast()
</script>

<style scoped>
.toast-container {
  position: fixed;
  top: 22px;
  right: 22px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  z-index: 10001;
}

.toast {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--panel-shadow);
  border-radius: 20px;
  min-width: 220px;
  max-width: 380px;
}

.toast-main {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.toast.success {
  border-left: 4px solid var(--color-success);
}

.toast.error {
  border-left: 4px solid var(--color-error);
}

.toast.info {
  border-left: 4px solid var(--color-primary);
}

.toast-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.38);
}

.toast.success .toast-icon {
  color: var(--color-success);
}

.toast.error .toast-icon {
  color: var(--color-error);
}

.toast.info .toast-icon {
  color: var(--color-primary);
}

.toast-message {
  font-size: 13px;
  color: var(--color-text);
  line-height: 1.5;
  flex: 1;
}

.toast-action-btn {
  flex-shrink: 0;
  padding: 9px 12px;
  border-radius: 14px;
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: white;
  font-size: 12px;
  font-weight: 600;
  transition: transform var(--transition-fast), opacity var(--transition-fast);
}

.toast-action-btn:hover {
  transform: translateY(-1px);
}

.toast-enter-active {
  transition: transform 0.3s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.3s cubic-bezier(0.22, 1, 0.36, 1);
}

.toast-leave-active {
  transition: transform 0.2s cubic-bezier(0.22, 1, 0.36, 1), opacity 0.2s cubic-bezier(0.22, 1, 0.36, 1);
}

.toast-enter-from {
  opacity: 0;
  transform: translateX(24px);
}

.toast-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

@media (max-width: 480px) {
  .toast-container {
    top: auto;
    bottom: 96px;
    left: 16px;
    right: 16px;
  }

  .toast {
    min-width: auto;
    max-width: 100%;
  }
}
</style>
