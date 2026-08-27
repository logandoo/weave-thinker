// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

export interface ProviderConfig {
  base_url: string
  model_name: string
}

export interface ProvidersResponse {
  providers: Record<string, ProviderConfig>
  default_provider: string
}

export const configApi = {
  async getProviderConfigs(): Promise<ProvidersResponse> {
    const { data } = await api.get('/config/providers')
    return data
  }
}
