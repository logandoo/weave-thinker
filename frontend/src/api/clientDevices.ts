// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import type { ClientDevice } from '@/types'
import {
  buildDeviceDownloadUrl as _buildDeviceDownloadUrl,
  deviceIdFromTarget,
} from '@/utils/deviceUrls'

// W16 设备执行模式：设备管理面（配对码/列表/撤销）+ 设备文件代理。
export const clientDevicesApi = {
  async list(): Promise<ClientDevice[]> {
    const { data } = await api.get<ClientDevice[]>('/devices')
    return data
  },

  async createPairingCode(): Promise<{ code: string; expires_in_seconds: number }> {
    const { data } = await api.post('/devices/pairing-code')
    return data
  },

  async revoke(deviceId: string): Promise<void> {
    await api.post(`/devices/${encodeURIComponent(deviceId)}/revoke`)
  },

  // W19 P3：远端执行审计（用户维度倒序）
  async listExecutions(limit = 8): Promise<ClientExecAuditItem[]> {
    const { data } = await api.get<ClientExecAuditItem[]>('/devices/executions', {
      params: { limit },
    })
    return data
  },
}

export interface ClientExecAuditItem {
  id: string
  device_id: string
  conversation_id?: string | null
  tool: string
  ok: boolean
  error?: string | null
  duration_ms: number
  created_at?: string | null
}

// 设备模式下的 workspace 相对路径 → 设备文件代理 URL（下载卡/预览用）。
// 纯派生在 utils/deviceUrls（单测缝）；此处注入 localStorage token。
export function buildDeviceDownloadUrl(deviceId: string, relPath: string): string {
  return _buildDeviceDownloadUrl(deviceId, relPath, localStorage.getItem('chatllm_token'))
}

export { deviceIdFromTarget }
