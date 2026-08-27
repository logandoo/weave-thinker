<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <Teleport to="body">
    <Transition name="perm-dialog">
      <div v-if="chatStore.pendingPermissionRequest" class="perm-overlay" @click.self="handleDeny">
        <div class="perm-card">
          <div class="perm-header">
            <div class="perm-icon">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              </svg>
            </div>
            <h3 class="perm-title">权限请求</h3>
          </div>
          <div class="perm-body">
            <p class="perm-description">{{ chatStore.pendingPermissionRequest.description }}</p>
            <div v-if="chatStore.pendingPermissionRequest.details?.command" class="perm-detail">
              <span class="perm-detail-label">命令</span>
              <code class="perm-detail-value">{{ chatStore.pendingPermissionRequest.details.command }}</code>
            </div>
            <div v-if="chatStore.pendingPermissionRequest.details?.target_path" class="perm-detail">
              <span class="perm-detail-label">目标路径</span>
              <code class="perm-detail-value">{{ chatStore.pendingPermissionRequest.details.target_path }}</code>
            </div>
            <div v-if="chatStore.pendingPermissionRequest.details?.action" class="perm-detail">
              <span class="perm-detail-label">操作</span>
              <code class="perm-detail-value">{{ chatStore.pendingPermissionRequest.details.action }}</code>
            </div>
            <div v-if="chatStore.pendingPermissionRequest.details?.title" class="perm-detail">
              <span class="perm-detail-label">标题</span>
              <code class="perm-detail-value">{{ chatStore.pendingPermissionRequest.details.title }}</code>
            </div>
            <div v-if="chatStore.pendingPermissionRequest.details?._permission_description && !chatStore.pendingPermissionRequest.description" class="perm-detail">
              <span class="perm-detail-label">说明</span>
              <code class="perm-detail-value">{{ chatStore.pendingPermissionRequest.details._permission_description }}</code>
            </div>
          </div>
          <div class="perm-actions">
            <button class="perm-btn deny" @click="handleDeny">拒绝</button>
            <button class="perm-btn approve" @click="handleApprove">允许</button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

function handleApprove() {
  chatStore.respondToPermission(true)
}

function handleDeny() {
  chatStore.respondToPermission(false)
}
</script>

<style scoped>
.perm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(24, 18, 14, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10001;
  padding: 20px;
}

.perm-card {
  background: var(--surface-panel-strong);
  border: 1px solid var(--panel-border);
  box-shadow: var(--frame-shadow), 0 20px 40px rgba(0, 0, 0, 0.15);
  border-radius: 24px;
  padding: 28px;
  max-width: 420px;
  width: 100%;
}

.perm-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.perm-icon {
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: linear-gradient(135deg, #fbbf24, #f59e0b);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  flex-shrink: 0;
}

.perm-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.perm-body {
  margin-bottom: 20px;
}

.perm-description {
  font-size: 14px;
  color: var(--color-text);
  line-height: 1.6;
  margin: 0 0 12px 0;
}

.perm-detail {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 6px 10px;
  background: var(--color-bg);
  border-radius: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}

.perm-detail-label {
  color: var(--color-text-light);
  flex-shrink: 0;
  min-width: 60px;
}

.perm-detail-value {
  color: var(--color-text);
  word-break: break-all;
  font-family: var(--font-mono);
  font-size: 12px;
  background: none;
  padding: 0;
}

.perm-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}

.perm-btn {
  min-width: 86px;
  padding: 10px 20px;
  border-radius: 16px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--transition-fast), opacity var(--transition-fast), background var(--transition-fast);
  border: 1px solid transparent;
}

.perm-btn.deny {
  background: var(--surface-panel-subtle);
  border-color: var(--panel-border);
  color: var(--color-text);
}

.perm-btn.deny:hover {
  background: rgba(141, 104, 69, 0.08);
  transform: translateY(-1px);
}

.perm-btn.approve {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  color: #fff;
  box-shadow: 0 8px 16px rgba(141, 104, 69, 0.18);
}

.perm-btn.approve:hover {
  transform: translateY(-1px);
}

.perm-dialog-enter-active {
  transition: opacity 0.2s ease;
}
.perm-dialog-enter-active .perm-card {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.perm-dialog-leave-active {
  transition: opacity 0.15s ease;
}
.perm-dialog-leave-active .perm-card {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.perm-dialog-enter-from {
  opacity: 0;
}
.perm-dialog-enter-from .perm-card {
  transform: scale(0.95);
  opacity: 0;
}
.perm-dialog-leave-to {
  opacity: 0;
}
.perm-dialog-leave-to .perm-card {
  transform: scale(0.95);
  opacity: 0;
}
</style>
