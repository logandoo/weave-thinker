// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from '@/api/client'

export interface UserProfile {
  id: string
  username: string
  created_at: string
  agent_permissions?: Record<string, boolean>
  nickname?: string | null
  avatar_data?: string | null
}

export interface ProviderOverride {
  enabled: boolean
  base_url: string
  model_name: string
  has_api_key: boolean
  api_key_tail: string
  params: Record<string, number | null>
}

export type ProviderOverrides = Record<string, ProviderOverride | null>

export interface ModelProviderStatus {
  kinds: string[]
  overrides: ProviderOverrides
}

export interface ProviderOverridePayload {
  enabled?: boolean
  base_url?: string
  /** null = 保持原值；"" = 显式清除；字符串 = 设置新值 */
  api_key?: string | null
  model_name?: string
  params?: Record<string, number | null>
}

export const userSettingsApi = {
  getProfile: () => api.get<UserProfile>('/users/me/profile'),
  updateProfile: (nickname: string | null) =>
    api.put<UserProfile>('/users/me/profile', { nickname }),
  uploadAvatar: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    // axios 实例默认 Content-Type: application/json 会压掉 multipart 边界
    // （wave-11 已知坑）→ 显式 multipart/form-data。
    return api.post<UserProfile>('/users/me/avatar', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  deleteAvatar: () => api.delete<UserProfile>('/users/me/avatar'),
  getModelProvider: () => api.get<ModelProviderStatus>('/users/me/model-provider'),
  updateModelProvider: (payload: Record<string, ProviderOverridePayload | null>) =>
    api.put<ModelProviderStatus>('/users/me/model-provider', payload),
  deleteModelProvider: () => api.delete('/users/me/model-provider'),
}
