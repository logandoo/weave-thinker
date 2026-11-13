<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

<template>
  <div class="mcp-panel">
    <div class="mcp-head">
      <h3 class="mcp-title">MCP 服务器</h3>
      <button class="mcp-primary" :disabled="loading || saving" @click="startAdd">
        + 添加 MCP
      </button>
    </div>

    <div v-if="error" class="mcp-error">{{ error }}</div>

    <div v-if="editing" class="mcp-form">
      <div class="mcp-form-row">
        <label>名称</label>
        <input v-model.trim="form.name" class="mcp-input" placeholder="my-tools" :disabled="saving" />
      </div>
      <div class="mcp-form-row">
        <label>传输</label>
        <select v-model="form.transport" class="mcp-input" :disabled="saving">
          <option value="http">HTTP（Streamable HTTP）</option>
          <option value="stdio">stdio（本地子进程）</option>
        </select>
      </div>
      <template v-if="form.transport === 'http'">
        <div class="mcp-form-row">
          <label>URL</label>
          <input v-model.trim="form.url" class="mcp-input" placeholder="https://example.com/mcp" :disabled="saving" />
        </div>
        <div class="mcp-form-row">
          <label>API Key</label>
          <input v-model="form.apiKey" class="mcp-input" type="password" :placeholder="editingId ? '留空保持原值' : '可留空'" :disabled="saving" />
        </div>
      </template>
      <template v-else>
        <div class="mcp-form-row">
          <label>命令</label>
          <input v-model.trim="form.command" class="mcp-input" placeholder="/path/to/python 或 npx" :disabled="saving" />
        </div>
        <div class="mcp-form-row mcp-form-row--block">
          <label>参数（每行一个）</label>
          <textarea v-model="form.argsText" class="mcp-input mcp-textarea" placeholder="-m&#10;your_server" :disabled="saving"></textarea>
        </div>
        <div class="mcp-form-row mcp-form-row--block">
          <label>环境变量（KEY=VALUE，每行一个；留 *** 保持原值）</label>
          <textarea v-model="form.envText" class="mcp-input mcp-textarea" placeholder="TOKEN=***" :disabled="saving"></textarea>
        </div>
      </template>
      <div class="mcp-form-row">
        <label>启用</label>
        <label class="mcp-check"><input v-model="form.enabled" type="checkbox" :disabled="saving" /> 保存后允许 agent 发现/调用</label>
      </div>
      <div class="mcp-form-actions">
        <button class="mcp-secondary" :disabled="saving" @click="cancelEdit">取消</button>
        <button class="mcp-primary" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存并应用' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="mcp-empty">加载中…</div>
    <div v-else-if="!servers.length && !editing" class="mcp-empty">
      还没有 MCP 服务器。添加一个后，agent 可以用 search_tools 发现并调用它的工具。
    </div>

    <div v-else class="mcp-list">
      <div v-for="server in pagedServers" :key="server.id" class="mcp-card">
        <div class="mcp-card-main">
          <div class="mcp-card-title">
            <span class="mcp-name">{{ server.name }}</span>
            <span class="mcp-badge">{{ server.transport.toUpperCase() }}</span>
            <span v-if="!server.enabled" class="mcp-badge mcp-badge--off">已禁用</span>
          </div>
          <div class="mcp-meta">
            <span v-if="server.url">{{ server.url }}</span>
            <span v-else>{{ server.command }} {{ (server.args || []).join(' ') }}</span>
          </div>
          <div class="mcp-status">
            <template v-if="server.runtime?.registered">
              <span class="mcp-dot mcp-dot--ok"></span>
              已注册 · {{ server.runtime.tools_count }} 个工具
            </template>
            <template v-else-if="server.runtime?.error">
              <span class="mcp-dot mcp-dot--err"></span>
              {{ server.runtime.error }}
            </template>
            <template v-else>
              <span class="mcp-dot"></span>
              尚未连接（下一轮或保存时重试）
            </template>
          </div>
        </div>
        <div class="mcp-card-actions">
          <button class="mcp-link" :disabled="saving" @click="startEdit(server)">编辑</button>
          <button class="mcp-link" :disabled="saving" @click="reload(server)">重连</button>
          <button
            v-if="confirmingDeleteId !== server.id"
            class="mcp-link mcp-link--danger"
            :disabled="saving"
            @click="confirmingDeleteId = server.id"
          >删除</button>
          <button
            v-else
            class="mcp-link mcp-link--danger"
            :disabled="saving"
            @click="remove(server)"
          >确认删除</button>
        </div>
      </div>
      <PaginationBar v-model:page="currentPage" :total-pages="totalPages" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { mcpApi, type McpServer, type McpServerPayload } from '@/api/mcp'
import { useToast } from '@/composables/useToast'
import PaginationBar from './PaginationBar.vue'

defineProps<{ embedded?: boolean }>()

const { show: showToast } = useToast()
const PAGE_SIZE = 10
const servers = ref<McpServer[]>([])
const currentPage = ref(1)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const editing = ref(false)
const editingId = ref<string | null>(null)
const confirmingDeleteId = ref<string | null>(null)

const totalPages = computed(() => Math.max(1, Math.ceil(servers.value.length / PAGE_SIZE)))
const pagedServers = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return servers.value.slice(start, start + PAGE_SIZE)
})

watch(totalPages, (pages) => {
  if (currentPage.value > pages) currentPage.value = pages
})

const form = reactive({
  name: '',
  transport: 'http' as 'http' | 'stdio',
  url: '',
  apiKey: '',
  command: '',
  argsText: '',
  envText: '',
  enabled: true,
})

function resetForm() {
  form.name = ''
  form.transport = 'http'
  form.url = ''
  form.apiKey = ''
  form.command = ''
  form.argsText = ''
  form.envText = ''
  form.enabled = true
}

function lines(value: string): string[] {
  return value.split('\n').map(s => s.trim()).filter(Boolean)
}

function envFromText(value: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of lines(value)) {
    const idx = line.indexOf('=')
    if (idx <= 0) continue
    out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim()
  }
  return out
}

function textFromEnv(env: Record<string, string> | undefined): string {
  return Object.entries(env || {}).map(([k, v]) => `${k}=${v}`).join('\n')
}

function startAdd() {
  editingId.value = null
  resetForm()
  error.value = ''
  editing.value = true
}

function startEdit(server: McpServer) {
  editingId.value = server.id
  form.name = server.name
  form.transport = server.transport
  form.url = server.url || ''
  form.apiKey = ''
  form.command = server.command || ''
  form.argsText = (server.args || []).join('\n')
  form.envText = textFromEnv(server.env)
  form.enabled = server.enabled !== false
  error.value = ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  editingId.value = null
  error.value = ''
}

function buildPayload(): McpServerPayload {
  const payload: McpServerPayload = {
    name: form.name,
    transport: form.transport,
    enabled: form.enabled,
  }
  if (form.transport === 'http') {
    payload.url = form.url
    // Switching from stdio: explicitly clear the stale command/args/env.
    payload.command = ''
    payload.args = []
    payload.env = {}
    if (form.apiKey.trim()) payload.api_key = form.apiKey.trim()
  } else {
    payload.command = form.command
    payload.args = lines(form.argsText)
    payload.env = envFromText(form.envText)
    // Switching from http: explicitly clear the stale URL.
    payload.url = ''
  }
  return payload
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await mcpApi.list()
    servers.value = data.servers || []
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '加载 MCP 服务器失败'
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!form.name.trim()) {
    error.value = '名称不能为空'
    return
  }
  saving.value = true
  error.value = ''
  try {
    const payload = buildPayload()
    const result = editingId.value
      ? await mcpApi.update(editingId.value, payload)
      : await mcpApi.create(payload)
    if (result?.ok === false) {
      error.value = result.error || '保存成功，但连接失败；请检查配置后点「重连」'
      showToast('已保存，但 MCP 连接失败', 'error')
    } else {
      showToast(editingId.value ? 'MCP 已更新并生效' : 'MCP 已添加并生效', 'success')
    }
    editing.value = false
    editingId.value = null
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '保存 MCP 失败'
  } finally {
    saving.value = false
  }
}

async function remove(server: McpServer) {
  saving.value = true
  error.value = ''
  try {
    await mcpApi.remove(server.id)
    confirmingDeleteId.value = null
    showToast('MCP 已删除', 'success')
    await load()
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '删除 MCP 失败'
  } finally {
    saving.value = false
  }
}

async function reload(server: McpServer) {
  saving.value = true
  error.value = ''
  try {
    await mcpApi.reloadServer(server.id)
    await load()
    showToast('已重新连接 MCP', 'success')
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '重连 MCP 失败'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.mcp-panel { display: flex; flex-direction: column; gap: 14px; padding: 4px 2px; }
.mcp-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.mcp-title { margin: 0; font-size: 15px; color: var(--color-text); }
.mcp-primary, .mcp-secondary {
  border: 1px solid var(--panel-border); border-radius: 8px; padding: 7px 12px;
  font-size: 12px; cursor: pointer; background: var(--surface-panel-strong); color: var(--color-text);
  white-space: nowrap;
}
.mcp-primary { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }
.mcp-primary:disabled, .mcp-secondary:disabled { opacity: .55; cursor: default; }
.mcp-error { padding: 8px 10px; border-radius: 8px; background: color-mix(in srgb, var(--color-error) 10%, transparent); color: var(--color-error); font-size: 12px; }
.mcp-form { border: 1px solid var(--panel-border); border-radius: 10px; padding: 12px; background: var(--surface-panel-subtle); display: flex; flex-direction: column; gap: 10px; }
.mcp-form-row { display: grid; grid-template-columns: 110px 1fr; align-items: center; gap: 10px; }
.mcp-form-row--block { align-items: start; }
.mcp-form-row label { font-size: 12px; color: var(--color-text-light); }
.mcp-check { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--color-text-light); }
.mcp-input {
  width: 100%; box-sizing: border-box; border: 1px solid var(--panel-border); border-radius: 8px;
  padding: 7px 9px; font-size: 13px; background: var(--surface-input); color: var(--color-text);
}
.mcp-textarea { min-height: 64px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.mcp-form-actions { display: flex; justify-content: flex-end; gap: 8px; }
.mcp-empty { padding: 24px 12px; text-align: center; color: var(--color-text-light); font-size: 12px; border: 1px dashed var(--panel-border); border-radius: 10px; }
.mcp-list { display: flex; flex-direction: column; gap: 8px; }
.mcp-card { display: flex; justify-content: space-between; gap: 12px; border: 1px solid var(--panel-border); border-radius: 10px; padding: 10px 12px; background: var(--surface-panel-strong); }
.mcp-card-main { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.mcp-card-title { display: flex; align-items: center; gap: 8px; }
.mcp-name { font-size: 13px; font-weight: 600; color: var(--color-text); }
.mcp-badge { font-size: 10px; padding: 1px 6px; border-radius: 999px; background: var(--surface-panel-subtle); color: var(--color-text-light); border: 1px solid var(--panel-border); }
.mcp-badge--off { color: var(--color-warning); }
.mcp-meta { font-size: 11px; color: var(--color-text-light); word-break: break-all; }
.mcp-status { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--color-text-light); }
.mcp-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--color-text-light); flex: 0 0 auto; }
.mcp-dot--ok { background: var(--color-success); }
.mcp-dot--err { background: var(--color-error); }
.mcp-card-actions { display: flex; align-items: flex-start; gap: 8px; flex: 0 0 auto; }
.mcp-link { border: none; background: none; padding: 2px; font-size: 12px; color: var(--color-primary); cursor: pointer; }
.mcp-link:disabled { opacity: .5; cursor: default; }
.mcp-link--danger { color: var(--color-error); }

@media (max-width: 767px) {
  .mcp-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }
  .mcp-primary {
    align-self: flex-start;
  }
}
</style>
