// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'

export interface User {
  id: string
  username: string
  created_at: string
}

export const adminApi = {
  async getUsers(): Promise<User[]> {
    const { data } = await api.get('/admin/users')
    return data
  },

  async getUser(userId: string): Promise<User> {
    const { data } = await api.get(`/admin/users/${userId}`)
    return data
  },

  // 2026-11-11 补救 #5b：口令走 body（此前是 query 参数 → 明文进 access log）
  async createUser(username: string, password: string): Promise<User> {
    const { data } = await api.post('/admin/users', { username, password })
    return data
  },

  async updateUser(
    userId: string,
    payload: { username?: string; password?: string; is_active?: boolean }
  ): Promise<void> {
    await api.put(`/admin/users/${userId}`, payload)
  },

  async deleteUser(userId: string): Promise<void> {
    await api.delete(`/admin/users/${userId}`)
  }
}