// Copyright (c) 2026 Weave Thinker Contributors
// SPDX-License-Identifier: Apache-2.0

import api from './client'
import type { Skill, SkillFormData } from '@/types'

export interface ExecutableWarning {
  file_path: string
  file_type: string
  is_dangerous: boolean
  warning_message: string
}

export interface ScanZipResult {
  filename: string
  has_executables: boolean
  executable_count: number
  warnings: ExecutableWarning[]
  dangerous_count: number
}

export const skillsApi = {
  async getSkills(): Promise<Skill[]> {
    const { data } = await api.get('/skills')
    return data
  },

  async getSkill(id: string): Promise<Skill> {
    const { data } = await api.get(`/skills/${id}`)
    return data
  },

  async createSkill(skill: SkillFormData): Promise<Skill> {
    const { data } = await api.post('/skills', skill)
    return data
  },

  async updateSkill(id: string, skill: Partial<SkillFormData>): Promise<Skill> {
    const { data } = await api.put(`/skills/${id}`, skill)
    return data
  },

  async deleteSkill(id: string): Promise<void> {
    await api.delete(`/skills/${id}`)
  },

  async scanZip(file: File): Promise<ScanZipResult> {
    const formData = new FormData()
    formData.append('file', file)
    const { data } = await api.post('/skills/scan-zip', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
    return data
  },

  async uploadSkills(file: File, force: boolean = false): Promise<Skill[]> {
    const formData = new FormData()
    formData.append('file', file)
    const { data } = await api.post(`/skills/upload?force=${force}`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
    return data
  },

  async scanFolder(files: File[], paths: string[]): Promise<ScanZipResult> {
    const formData = new FormData()
    files.forEach((file, index) => {
      formData.append('files', file)
      formData.append('paths', paths[index])
    })
    const { data } = await api.post('/skills/scan-folder', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
    return data
  },

  async uploadSkillsFolder(files: File[], paths: string[], force: boolean = false): Promise<Skill[]> {
    const formData = new FormData()
    files.forEach((file, index) => {
      formData.append('files', file)
      formData.append('paths', paths[index])
    })
    const { data } = await api.post(`/skills/upload-folder?force=${force}`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
    return data
  },

  async getSkillByName(name: string): Promise<Skill> {
    const { data } = await api.get(`/skills/by-name/${name}`)
    return data
  },
}
