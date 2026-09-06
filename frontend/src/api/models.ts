// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import type { ModelsResponse } from '@/types'

// GET /api/models — 模型逻辑别名列表（模型配置解耦 2026-08-30）。
// 响应只含别名与能力描述；供应商 url/key/真实模型名永不可见。
export const modelsApi = {
  async getModels(): Promise<ModelsResponse> {
    const { data } = await api.get('/models')
    return data
  }
}
