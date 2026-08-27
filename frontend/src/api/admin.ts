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

  async createUser(username: string, password: string): Promise<User> {
    const { data } = await api.post('/admin/users', null, {
      params: { username, password }
    })
    return data
  },

  async updateUser(userId: string, params: { username?: string; password?: string; is_active?: boolean }): Promise<void> {
    await api.put(`/admin/users/${userId}`, null, { params })
  },

  async deleteUser(userId: string): Promise<void> {
    await api.delete(`/admin/users/${userId}`)
  }
}