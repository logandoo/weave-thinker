<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <Transition name="confirm-dialog">
      <div v-if="visible" class="confirm-overlay" @click.self="handleCancel">
        <div class="confirm-card">
          <h3 v-if="options.title" class="confirm-title">{{ options.title }}</h3>
          <p class="confirm-message">{{ options.message }}</p>
          <div v-if="options.threeButton" class="confirm-actions three-button">
            <button class="confirm-btn cancel" @click="handleCancel">
              {{ options.threeButton.cancelText }}
            </button>
            <button class="confirm-btn discard" @click="handleDiscard">
              {{ options.threeButton.discardText }}
            </button>
            <button class="confirm-btn ok" @click="handleConfirm">
              {{ options.threeButton.saveText }}
            </button>
          </div>
          <div v-else class="confirm-actions">
            <button class="confirm-btn cancel" @click="handleCancel">
              {{ options.cancelText || '取消' }}
            </button>
            <button
              class="confirm-btn ok"
              :class="{ danger: options.danger }"
              @click="handleConfirm"
            >
              {{ options.confirmText || '确定' }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const { visible, options, handleConfirm, handleDiscard, handleCancel } = useConfirmDialog()
</script>

<style scoped>
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-scrim);
  display: flex;
  align-items: center;
  justify-content: center;
  /* 必须高于所有业务弹层（system-settings 10003 / context-menu 10001）——
     确认框常嵌套打开（设置 tab 内删除），低 z 会被遮罩拦截（A4.9 修复） */
  z-index: 20000;
  padding: 20px;
}

.confirm-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--dialog-shadow);
  border-radius: var(--dialog-radius);
  padding: 28px;
  max-width: 360px;
  width: 100%;
}

.confirm-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text);
  margin-bottom: 10px;
  text-wrap: balance;
}

.confirm-message {
  font-size: 14px;
  color: var(--color-text-light);
  line-height: 1.6;
  margin-bottom: 22px;
  text-align: center;
  white-space: pre-line;
}

.confirm-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
}

.confirm-btn {
  min-width: 86px;
  padding: 10px 18px;
  border-radius: 16px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--transition-fast), opacity var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast);
  border: 1px solid transparent;
}

.confirm-btn.cancel {
  background: var(--surface-panel-subtle);
  border-color: var(--panel-border);
  color: var(--color-text);
}

.confirm-btn.cancel:hover {
  background: rgba(141, 104, 69, 0.08);
  transform: translateY(-1px);
}

.confirm-btn.ok {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: #fff;
  box-shadow: 0 14px 24px rgba(141, 104, 69, 0.18);
}

.confirm-btn.ok:hover {
  transform: translateY(-1px);
}

.confirm-btn.ok.danger {
  background: var(--color-error);
  box-shadow: 0 14px 24px rgba(193, 98, 75, 0.18);
}

.confirm-btn.ok.danger:hover {
  background: #b24f3f;
}

.confirm-actions.three-button {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.confirm-btn.discard {
  background: rgba(193, 98, 75, 0.08);
  border-color: rgba(193, 98, 75, 0.12);
  color: var(--color-error);
}

.confirm-btn.discard:hover {
  background: rgba(193, 98, 75, 0.12);
  transform: translateY(-1px);
}

.confirm-dialog-enter-active {
  transition: opacity 0.2s ease;
}

.confirm-dialog-enter-active .confirm-card {
  transition: transform 0.2s ease, opacity 0.2s ease;
}

.confirm-dialog-leave-active {
  transition: opacity 0.15s ease;
}

.confirm-dialog-leave-active .confirm-card {
  transition: transform 0.15s ease, opacity 0.15s ease;
}

.confirm-dialog-enter-from {
  opacity: 0;
}

.confirm-dialog-enter-from .confirm-card {
  transform: scale(0.95);
  opacity: 0;
}

.confirm-dialog-leave-to {
  opacity: 0;
}

.confirm-dialog-leave-to .confirm-card {
  transform: scale(0.95);
  opacity: 0;
}

@media (max-width: 480px) {
  .confirm-card {
    padding: 24px 20px;
    border-radius: 24px;
  }

  .confirm-actions,
  .confirm-actions.three-button {
    flex-direction: column-reverse;
  }

  .confirm-btn {
    width: 100%;
  }
}
</style>
