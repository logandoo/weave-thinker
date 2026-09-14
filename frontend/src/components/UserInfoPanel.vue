<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="user-info-panel" :data-loaded="loaded ? 'true' : 'false'">
    <div class="user-info-header">
      <h3 class="user-info-title">用户信息</h3>
    </div>

    <div v-if="errorMsg" class="user-info-error">{{ errorMsg }}</div>

    <div class="user-info-avatar-row">
      <div class="user-info-avatar">
        <img v-if="avatarPreview" :src="avatarPreview" alt="头像预览" />
        <span v-else>{{ avatarLetter }}</span>
      </div>
      <div class="user-info-avatar-actions">
        <input
          ref="fileInputRef"
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"
          class="user-info-file"
          @change="onFilePicked"
        />
        <button class="user-info-btn" :disabled="uploading" @click="pickFile">
          {{ uploading ? '上传中…' : '上传头像' }}
        </button>
        <button
          v-if="profile?.avatar_data"
          class="user-info-btn subtle"
          :disabled="uploading"
          @click="removeAvatar"
        >删除头像</button>
        <span class="user-info-file-hint">PNG/JPEG/WebP，≤2MB，自动裁剪为 256×256</span>
      </div>
    </div>

    <div class="user-info-field">
      <label class="user-info-label" for="user-info-nickname">昵称</label>
      <input
        id="user-info-nickname"
        v-model="nickname"
        type="text"
        maxlength="50"
        class="user-info-input"
        placeholder="留空则显示用户名"
      />
    </div>

    <div class="user-info-actions">
      <button class="user-info-btn primary" :disabled="saving || !nicknameDirty" @click="saveNickname">
        {{ saving ? '保存中…' : '保存昵称' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { userSettingsApi, type UserProfile } from '@/api/userSettings'
import { useAuth } from '@/composables/useAuth'

defineProps<{ embedded?: boolean }>()

const auth = useAuth()
const profile = ref<UserProfile | null>(null)
const nickname = ref('')
const initialNickname = ref('')
const uploading = ref(false)
const saving = ref(false)
const errorMsg = ref('')
const loaded = ref(false)
const fileInputRef = ref<HTMLInputElement | null>(null)

const avatarPreview = computed(() => profile.value?.avatar_data || '')
const avatarLetter = computed(() =>
  (profile.value?.nickname || profile.value?.username || auth.user.value?.username || '?')
    .charAt(0)
    .toUpperCase(),
)
const nicknameDirty = computed(() => nickname.value.trim() !== initialNickname.value)

function applyProfile(p: UserProfile) {
  profile.value = p
  nickname.value = p.nickname || ''
  initialNickname.value = p.nickname || ''
  auth.applyProfile(p)
}

async function loadProfile() {
  try {
    const { data } = await userSettingsApi.getProfile()
    applyProfile(data)
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail || '加载用户信息失败'
  } finally {
    loaded.value = true
  }
}

function pickFile() {
  fileInputRef.value?.click()
}

async function onFilePicked(evt: Event) {
  const input = evt.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  errorMsg.value = ''
  if (file.size > 2 * 1024 * 1024) {
    errorMsg.value = '头像文件不能超过 2MB'
    input.value = ''
    return
  }
  uploading.value = true
  try {
    const { data } = await userSettingsApi.uploadAvatar(file)
    applyProfile(data)
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail || '头像上传失败'
  } finally {
    uploading.value = false
    input.value = ''
  }
}

async function removeAvatar() {
  errorMsg.value = ''
  uploading.value = true
  try {
    const { data } = await userSettingsApi.deleteAvatar()
    applyProfile(data)
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail || '删除头像失败'
  } finally {
    uploading.value = false
  }
}

async function saveNickname() {
  errorMsg.value = ''
  saving.value = true
  try {
    const { data } = await userSettingsApi.updateProfile(nickname.value.trim() || null)
    applyProfile(data)
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail || '昵称保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(loadProfile)
</script>

<style scoped>
.user-info-panel {
  width: 100%;
  max-width: 460px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.user-info-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  width: 100%;
}

.user-info-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}

.user-info-error {
  width: 100%;
  font-size: 13px;
  color: var(--color-error);
  margin-bottom: 10px;
  text-align: center;
}

.user-info-avatar-row {
  display: flex;
  align-items: center;
  gap: 18px;
  width: 100%;
  padding: 12px 4px;
}

.user-info-avatar {
  width: 72px;
  height: 72px;
  flex-shrink: 0;
  border-radius: 50%;
  overflow: hidden;
  background: var(--color-border);
  color: var(--color-text-light);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  font-weight: 600;
}

.user-info-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.user-info-avatar-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.user-info-file {
  display: none;
}

.user-info-file-hint {
  width: 100%;
  font-size: 12px;
  color: var(--color-text-light);
}

.user-info-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  margin-top: 10px;
}

.user-info-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.user-info-input {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  font-size: 14px;
  outline: none;
}

.user-info-input:focus {
  border-color: var(--color-primary);
}

.user-info-actions {
  display: flex;
  gap: 10px;
  justify-content: center;
  margin-top: 18px;
  width: 100%;
}

.user-info-btn {
  padding: 9px 18px;
  border-radius: 14px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid var(--panel-border);
  background: var(--surface-panel-subtle);
  color: var(--color-text);
  transition: opacity var(--transition-fast), background var(--transition-fast);
}

.user-info-btn.subtle {
  color: var(--color-text-light);
}

.user-info-btn.primary {
  background: linear-gradient(135deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  border-color: transparent;
  color: #fff;
}

.user-info-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
