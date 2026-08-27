<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Weave Thinker — Backend API 文档（详版）

> **文档结构**：
> - **一、接口明细**：全部 HTTP 接口，由生成器从**运行中后端的 `/openapi.json`** 自动生成
>   （每接口含 接口名 / 输入表 / 输出表 / 请求示例 / 返回示例；字段类型与必填性以 FastAPI Pydantic 模型为准）。
> - **二、专项语义**：OpenAPI 表达不了的行为级说明——SSE 事件协议、死磕模式全状态机、记忆检索管线、
>   语音事件与 WebSocket 帧、provider 语义、皮肤信任模型等（手工策展，与代码仓库同步维护）。
> - 两部分冲突时：字段级以「一」为准（生成自代码），行为级以「二」为准（生成器不理解业务）。
>
> 字段表由后端 OpenAPI 生成；接口行为变化后以生成器重跑再生（见「通用约定」）。

## 通用约定

- **Base URL**：`http(s)://<host>:8158`（默认端口，见 `backend/config.toml [server]`）
- **交互式文档**：运行时 `/docs`（Swagger UI）
- **接口规模**：132 个 HTTP operations（20 个业务模块 + main.py 根路由）+ 2 条 WebSocket
- **认证**：JWT Bearer Token（请求头 `Authorization: Bearer <token>`）。`POST /api/auth/login` 获取，
  有效期 `[security] token_expire_days`（默认 7 天）；`POST /api/auth/refresh` 用仍有效的 Token 滑动续期
  （单条原子 CAS 轮换，登出后其 token 永久不可续期——详见二.1）。WebSocket 以 `?token=<jwt>` 或
  `Authorization` 头认证。标注「公开」的接口无需 Token。
- **管理员路由**：`/api/admin/*` 目前仅校验登录、**无角色校验**——任何已登录用户均可调用（已知限制，
  商业版规划收口）。
- **错误约定**：4xx 响应体均为 `{"detail": "<原因>"}`。
  `401`=无/坏 Token；`403`=权限不足或敏感操作拒绝；`404`=资源不存在或非本人资源
  （越权访问他人资源一律 404 而非 403，防探测）；`409`=冲突（重名等）；`400`=参数/业务校验失败；
  `422`=Pydantic 请求模型校验失败（FastAPI 默认 `{"detail": [校验条目]}`）。
- **分页/排序**：列表接口默认不带分页 token，由 limit 参数（如 `limit=200`）或全量返回，排序见各接口说明。
- **时间字段**：全部 ISO-8601（UTC 存储，序列化含时区）；客户端本地按 UTC+8 展示（约定见 README）。

---

# 一、接口明细（OpenAPI 生成）

## 认证 Auth

前缀 `/api/auth`（`app/api/auth.py`）

### POST `/api/auth/login`
**Login**

认证：公开　|　源码模块：`app/api/auth.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | string | 是 |  |
| `password` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `access_token` | string | 是 |  |
| `token_type` | string | 是 |  |
| `user` | UserResponse | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/login" -H "Content-Type: application/json" -d {"username": "string", "password": "string"}
```

**返回示例**

```json
{
  "access_token": "string",
  "token_type": "string",
  "user": {
    "id": "string",
    "username": "string",
    "created_at": "string",
    "agent_permissions": null
  }
}
```

---

### POST `/api/auth/logout`
**Logout**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/logout" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/auth/me`
**Get Current User Info**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/auth/me" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {}
}
```

---

### PUT `/api/auth/me/permissions`
**Update Agent Permissions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/auth/me/permissions" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {}
}
```

---

### POST `/api/auth/refresh`
**Refresh Token**

> Sliding-session refresh: a still-valid token mints
> a fresh token whose window starts now — users who open the app within
> ``[security] token_expire_days`` are never re-asked for a password.
> ``get_current_user`` already rejects expired/invalid tokens (401/403), so
> nothing stale can roll forward.
> 
> Security (A4.9 I1): the rotation is a single atomic UPDATE ... WHERE
> session_token = <old> (CAS). A missing row — user already logged out, or
> a concurrent tab already rotated this token — yields 401 WITHOUT issuing
> a token: a logout stays terminal for that login and…

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `access_token` | string | 是 |  |
| `token_type` | string | 是 |  |
| `user` | UserResponse | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/refresh" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "access_token": "string",
  "token_type": "string",
  "user": {
    "id": "string",
    "username": "string",
    "created_at": "string",
    "agent_permissions": null
  }
}
```

---

### POST `/api/auth/register`
**Register**

认证：公开　|　源码模块：`app/api/auth.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | string | 是 |  |
| `password` | string | 是 |  |

**输出**

`201` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/register" -H "Content-Type: application/json" -d {"username": "string", "password": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {}
}
```

---


## 聊天 Chat（SSE 流式）

前缀 `/api/chat`（`app/api/chat.py`）

### POST `/api/chat/deathmatch/subgoal`
**Deathmatch Add Subgoal**

> Append an acceptance criterion to an ACTIVE deathmatch conversation
> (D3): the judge then checks every subgoal alongside the original goal,
> and the continuation prompt surfaces them so the agent works toward
> them. 400 when the conversation is not in deathmatch goal loop.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/deathmatch/subgoal" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/permission/respond`
**Permission Respond**

> Respond to a pending permission request from the agent loop.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/permission/respond" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/stream`
**Chat Stream**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `conversation_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `messages` | Message[] | 是 |  |
| `enable_web_search` | bool | 否 | （默认 False） |
| `regenerate_from_message_id` | ∪string|null | 否（可空） |  |
| `edit_message_id` | ∪string|null | 否（可空） |  |
| `force_search_results` | ∪string|null | 否（可空） |  |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `enable_reasoning` | bool | 否 | （默认 False） |
| `reasoning_effort` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_action` | ∪string|null | 否（可空） |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/stream" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"conversation_id": "string", "assistant_id": "string", "messages": [{"role": "user", "content": "string"}], "enable_web_search": false, "regenerate_from_message_id": "string", "edit_message_id": "string", "force_search_results": "string", "temperature": 0.0, "top_p": 0.0, "top_k": 0, "presence_penalty": 0.0, "frequency_penalty": 0.0, "max_tokens": 0, "enable_reasoning": false, "reasoning_effort": "string", "thinking_budget": 0, "deathmatch_mode": false, "deathmatch_action": "string"}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/stream/resume`
**Resume Stream**

> Reconnect to an SSE stream via the persistent StreamBuffer.
> 
> Accepts JSON body: 
> Uses subscribe_with_snapshot() for atomic replay + subscribe —
> no duplication gap between replay and live deltas.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/stream/resume" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/chat/stream/status/{conversation_id}`
**Stream Status**

> Check if a conversation has an active stream buffer.
> 
> Returns status information that the frontend uses to decide whether
> to resume streaming (incomplete) or read from DB (complete).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/chat/stream/status/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/stream/stop/{conversation_id}`
**Stop Agent Stream**

> Explicitly cancel a running agent task for a conversation.
> 
> This is the correct way for the frontend STOP button to cancel an agent.
> It does NOT rely on closing the SSE connection — closing the SSE would be
> indistinguishable from a passive client disconnect (tab switch, browser
> throttle, network blip), which must leave the detached agent running so it
> can self-save results. The agent task is tracked in
> ``_DETACHED_AGENT_TASKS`` and cancelled directly here.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/stream/stop/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 会话与分组 Conversations

前缀 `/api/conversations`（`app/api/conversation.py`）

### GET `/api/conversations`
**List Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | query | string | 否 |  |

**输出**

`200` 字段：`List[ConversationResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "title": "string",
    "group_id": "string",
    "assistant_id": "string",
    "sort_order": 0,
    "created_at": "string",
    "updated_at": "string",
    "last_user_message_at": "string",
    "deathmatch_mode": false,
    "deathmatch_status": "string",
    "deathmatch_reason": "string",
    "deathmatch_goal": "string",
    "deathmatch_turns": 0,
    "deathmatch_max_turns": 30,
    "deathmatch_grilling_total": 0,
    "deathmatch_grilling_completed": 0,
    "deathmatch_grilling_round": 0,
    "deathmatch_grilling_round_total": 3,
    "deathmatch_context_summary": "string",
    "deathmatch_expected_marker": "string",
    "deathmatch_marker_miss_count": 0,
    "deathmatch_compressed_context": "string",
    "deathmatch_plan": {},
    "deathmatch_plan_version": 0
  }
]
```

---

### POST `/api/conversations`
**Create Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0
}
```

---

### POST `/api/conversations/bulk-delete`
**Bulk Delete Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `conversation_ids` | string[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `deleted_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"conversation_ids": ["string"]}
```

**返回示例**

```json
{
  "status": "string",
  "deleted_count": 0
}
```

---

### POST `/api/conversations/export`
**Export Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `assistant_id` | string | 是 |  |
| `conversation_ids` | string[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/export" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"assistant_id": "string", "conversation_ids": ["string"]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/conversations/export-pdf`
**Export Messages Pdf**

> Export selected messages as PDF (single combined or per-message zip).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items` | ExportMessagesPDFRequest[] | 是 |  |
| `action` | string | 否 | （默认 'single'） |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/export-pdf" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"items": [{"title": "string", "content": "string", "role": "string"}], "action": "string"}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/conversations/groups`
**List Conversation Groups**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | query | string | 否 |  |

**输出**

`200` 字段：`List[ConversationGroupResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/groups" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "name": "string",
    "color": "string",
    "assistant_id": "string",
    "sort_order": 0,
    "created_at": "string",
    "updated_at": "string",
    "conversation_count": 0
  }
]
```

---

### POST `/api/conversations/groups`
**Create Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `color` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/groups" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "color": "string", "assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "color": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "conversation_count": 0
}
```

---

### POST `/api/conversations/groups/bulk-delete`
**Bulk Delete Groups**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `group_ids` | string[] | 是 |  |
| `delete_conversations` | bool | 否 | （默认 False） |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/groups/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"group_ids": ["string"], "delete_conversations": false}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/conversations/groups/reorder`
**Reorder Groups**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items` | ConversationGroupReorderItem[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/groups/reorder" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"items": [{"id": "string", "sort_order": 0}]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/conversations/groups/{group_id}`
**Delete Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `group_id` | path | string | 是 |  |
| `delete_conversations` | query | bool | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/conversations/groups/{group_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/conversations/groups/{group_id}`
**Update Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `group_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `color` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/groups/{group_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "color": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "color": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "conversation_count": 0
}
```

---

### PUT `/api/conversations/groups/{group_id}/move`
**Move Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `group_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `assistant_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/groups/{group_id}/move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "color": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "conversation_count": 0
}
```

---

### PUT `/api/conversations/reorder`
**Reorder Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items` | ConversationReorderItem[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/reorder" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"items": [{"id": "string", "sort_order": 0, "group_id": null}]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/conversations/search`
**Search Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `q` | query | string | 是 |  |

**输出**

`200` 字段：`List[ConversationSearchResult]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `conversation_id` | string | 是 |  |
| `title` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `matched_messages` | MatchedMessage[] | 否 | （默认 []） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/search?q=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "conversation_id": "string",
    "title": "string",
    "updated_at": "string",
    "matched_messages": [
      null
    ]
  }
]
```

---

### DELETE `/api/conversations/{conversation_id}`
**Delete Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/conversations/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/conversations/{conversation_id}`
**Get Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |
| `messages` | MessageResponse[] | 否 | （默认 []） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0,
  "messages": [
    {
      "id": null,
      "conversation_id": null,
      "role": null,
      "content": null,
      "reasoning_content": null,
      "tool_calls": null,
      "tool_results": null,
      "created_at": null
    }
  ]
}
```

---

### PUT `/api/conversations/{conversation_id}`
**Update Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `group_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/{conversation_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "group_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0
}
```

---

### GET `/api/conversations/{conversation_id}/messages`
**Get Messages**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

`200` 字段：`List[MessageResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `conversation_id` | string | 是 |  |
| `role` | string | 是 |  |
| `content` | string | 是 |  |
| `reasoning_content` | ∪string|null | 否（可空） |  |
| `tool_calls` | ∪string|null | 否（可空） |  |
| `tool_results` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/{conversation_id}/messages" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "conversation_id": "string",
    "role": "string",
    "content": "string",
    "reasoning_content": "string",
    "tool_calls": "string",
    "tool_results": "string",
    "created_at": "string"
  }
]
```

---

### PUT `/api/conversations/{conversation_id}/move`
**Move Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/{conversation_id}/move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"group_id": "string", "assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0
}
```

---


## 助手 Assistants

前缀 `/api/assistants`（`app/api/assistant.py`）

### GET `/api/assistants`
**List Assistants**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

**输出**

`200` 字段：`List[AssistantResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `use_custom_model` | bool | 否 | （默认 False） |
| `custom_api_url` | ∪string|null | 否（可空） |  |
| `custom_api_key` | ∪string|null | 否（可空） |  |
| `custom_model_name` | ∪string|null | 否（可空） |  |
| `provider_type` | string | 否 | （默认 'deepseek'） |
| `extra_body` | ∪string|null | 否（可空） |  |
| `use_subtask_model` | bool | 否 | （默认 False） |
| `subtask_custom_api_url` | ∪string|null | 否（可空） |  |
| `subtask_custom_api_key` | ∪string|null | 否（可空） |  |
| `subtask_custom_model_name` | ∪string|null | 否（可空） |  |
| `subtask_provider_type` | ∪string|null | 否（可空） |  |
| `subtask_extra_body` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `min_p` | ∪float|null | 否（可空） |  |
| `repetition_penalty` | ∪float|null | 否（可空） |  |
| `thinking_temperature` | ∪float|null | 否（可空） |  |
| `thinking_top_p` | ∪float|null | 否（可空） |  |
| `thinking_top_k` | ∪int|null | 否（可空） |  |
| `thinking_min_p` | ∪float|null | 否（可空） |  |
| `thinking_presence_penalty` | ∪float|null | 否（可空） |  |
| `thinking_repetition_penalty` | ∪float|null | 否（可空） |  |
| `preserve_thinking` | ∪bool|null | 否（可空） | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/assistants" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "name": "string",
    "system_prompt": "string",
    "temperature": 0.0,
    "top_p": 0.0,
    "top_k": 0,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "max_tokens": 0,
    "use_custom_model": false,
    "custom_api_url": "string",
    "custom_api_key": "string",
    "custom_model_name": "string",
    "provider_type": "string",
    "extra_body": "string",
    "use_subtask_model": false,
    "subtask_custom_api_url": "string",
    "subtask_custom_api_key": "string",
    "subtask_custom_model_name": "string",
    "subtask_provider_type": "string",
    "subtask_extra_body": "string",
    "thinking_budget": 0,
    "min_p": 0.0,
    "repetition_penalty": 0.0,
    "thinking_temperature": 0.0,
    "thinking_top_p": 0.0,
    "thinking_top_k": 0,
    "thinking_min_p": 0.0,
    "thinking_presence_penalty": 0.0,
    "thinking_repetition_penalty": 0.0,
    "preserve_thinking": true,
    "id": "string",
    "user_id": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/assistants`
**Create Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `use_custom_model` | bool | 否 | （默认 False） |
| `custom_api_url` | ∪string|null | 否（可空） |  |
| `custom_api_key` | ∪string|null | 否（可空） |  |
| `custom_model_name` | ∪string|null | 否（可空） |  |
| `provider_type` | string | 否 | （默认 'deepseek'） |
| `extra_body` | ∪string|null | 否（可空） |  |
| `use_subtask_model` | bool | 否 | （默认 False） |
| `subtask_custom_api_url` | ∪string|null | 否（可空） |  |
| `subtask_custom_api_key` | ∪string|null | 否（可空） |  |
| `subtask_custom_model_name` | ∪string|null | 否（可空） |  |
| `subtask_provider_type` | ∪string|null | 否（可空） |  |
| `subtask_extra_body` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `min_p` | ∪float|null | 否（可空） |  |
| `repetition_penalty` | ∪float|null | 否（可空） |  |
| `thinking_temperature` | ∪float|null | 否（可空） |  |
| `thinking_top_p` | ∪float|null | 否（可空） |  |
| `thinking_top_k` | ∪int|null | 否（可空） |  |
| `thinking_min_p` | ∪float|null | 否（可空） |  |
| `thinking_presence_penalty` | ∪float|null | 否（可空） |  |
| `thinking_repetition_penalty` | ∪float|null | 否（可空） |  |
| `preserve_thinking` | ∪bool|null | 否（可空） | （默认 True） |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `use_custom_model` | bool | 否 | （默认 False） |
| `custom_api_url` | ∪string|null | 否（可空） |  |
| `custom_api_key` | ∪string|null | 否（可空） |  |
| `custom_model_name` | ∪string|null | 否（可空） |  |
| `provider_type` | string | 否 | （默认 'deepseek'） |
| `extra_body` | ∪string|null | 否（可空） |  |
| `use_subtask_model` | bool | 否 | （默认 False） |
| `subtask_custom_api_url` | ∪string|null | 否（可空） |  |
| `subtask_custom_api_key` | ∪string|null | 否（可空） |  |
| `subtask_custom_model_name` | ∪string|null | 否（可空） |  |
| `subtask_provider_type` | ∪string|null | 否（可空） |  |
| `subtask_extra_body` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `min_p` | ∪float|null | 否（可空） |  |
| `repetition_penalty` | ∪float|null | 否（可空） |  |
| `thinking_temperature` | ∪float|null | 否（可空） |  |
| `thinking_top_p` | ∪float|null | 否（可空） |  |
| `thinking_top_k` | ∪int|null | 否（可空） |  |
| `thinking_min_p` | ∪float|null | 否（可空） |  |
| `thinking_presence_penalty` | ∪float|null | 否（可空） |  |
| `thinking_repetition_penalty` | ∪float|null | 否（可空） |  |
| `preserve_thinking` | ∪bool|null | 否（可空） | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/assistants" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "system_prompt": "string", "temperature": 0.0, "top_p": 0.0, "top_k": 0, "presence_penalty": 0.0, "frequency_penalty": 0.0, "max_tokens": 0, "use_custom_model": false, "custom_api_url": "string", "custom_api_key": "string", "custom_model_name": "string", "provider_type": "string", "extra_body": "string", "use_subtask_model": false, "subtask_custom_api_url": "string", "subtask_custom_api_key": "string", "subtask_custom_model_name": "string", "subtask_provider_type": "string", "subtask_extra_body": "string", "thinking_budget": 0, "min_p": 0.0, "repetition_penalty": 0.0, "thinking_temperature": 0.0, "thinking_top_p": 0.0, "thinking_top_k": 0, "thinking_min_p": 0.0, "thinking_presence_penalty": 0.0, "thinking_repetition_penalty": 0.0, "preserve_thinking": true}
```

**返回示例**

```json
{
  "name": "string",
  "system_prompt": "string",
  "temperature": 0.0,
  "top_p": 0.0,
  "top_k": 0,
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0,
  "max_tokens": 0,
  "use_custom_model": false,
  "custom_api_url": "string",
  "custom_api_key": "string",
  "custom_model_name": "string",
  "provider_type": "string",
  "extra_body": "string",
  "use_subtask_model": false,
  "subtask_custom_api_url": "string",
  "subtask_custom_api_key": "string",
  "subtask_custom_model_name": "string",
  "subtask_provider_type": "string",
  "subtask_extra_body": "string",
  "thinking_budget": 0,
  "min_p": 0.0,
  "repetition_penalty": 0.0,
  "thinking_temperature": 0.0,
  "thinking_top_p": 0.0,
  "thinking_top_k": 0,
  "thinking_min_p": 0.0,
  "thinking_presence_penalty": 0.0,
  "thinking_repetition_penalty": 0.0,
  "preserve_thinking": true,
  "id": "string",
  "user_id": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### DELETE `/api/assistants/{assistant_id}`
**Delete Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/assistants/{assistant_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/assistants/{assistant_id}`
**Get Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `use_custom_model` | bool | 否 | （默认 False） |
| `custom_api_url` | ∪string|null | 否（可空） |  |
| `custom_api_key` | ∪string|null | 否（可空） |  |
| `custom_model_name` | ∪string|null | 否（可空） |  |
| `provider_type` | string | 否 | （默认 'deepseek'） |
| `extra_body` | ∪string|null | 否（可空） |  |
| `use_subtask_model` | bool | 否 | （默认 False） |
| `subtask_custom_api_url` | ∪string|null | 否（可空） |  |
| `subtask_custom_api_key` | ∪string|null | 否（可空） |  |
| `subtask_custom_model_name` | ∪string|null | 否（可空） |  |
| `subtask_provider_type` | ∪string|null | 否（可空） |  |
| `subtask_extra_body` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `min_p` | ∪float|null | 否（可空） |  |
| `repetition_penalty` | ∪float|null | 否（可空） |  |
| `thinking_temperature` | ∪float|null | 否（可空） |  |
| `thinking_top_p` | ∪float|null | 否（可空） |  |
| `thinking_top_k` | ∪int|null | 否（可空） |  |
| `thinking_min_p` | ∪float|null | 否（可空） |  |
| `thinking_presence_penalty` | ∪float|null | 否（可空） |  |
| `thinking_repetition_penalty` | ∪float|null | 否（可空） |  |
| `preserve_thinking` | ∪bool|null | 否（可空） | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/assistants/{assistant_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "name": "string",
  "system_prompt": "string",
  "temperature": 0.0,
  "top_p": 0.0,
  "top_k": 0,
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0,
  "max_tokens": 0,
  "use_custom_model": false,
  "custom_api_url": "string",
  "custom_api_key": "string",
  "custom_model_name": "string",
  "provider_type": "string",
  "extra_body": "string",
  "use_subtask_model": false,
  "subtask_custom_api_url": "string",
  "subtask_custom_api_key": "string",
  "subtask_custom_model_name": "string",
  "subtask_provider_type": "string",
  "subtask_extra_body": "string",
  "thinking_budget": 0,
  "min_p": 0.0,
  "repetition_penalty": 0.0,
  "thinking_temperature": 0.0,
  "thinking_top_p": 0.0,
  "thinking_top_k": 0,
  "thinking_min_p": 0.0,
  "thinking_presence_penalty": 0.0,
  "thinking_repetition_penalty": 0.0,
  "preserve_thinking": true,
  "id": "string",
  "user_id": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### PUT `/api/assistants/{assistant_id}`
**Update Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `system_prompt` | ∪string|null | 否（可空） |  |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `use_custom_model` | ∪bool|null | 否（可空） |  |
| `custom_api_url` | ∪string|null | 否（可空） |  |
| `custom_api_key` | ∪string|null | 否（可空） |  |
| `custom_model_name` | ∪string|null | 否（可空） |  |
| `provider_type` | ∪string|null | 否（可空） |  |
| `extra_body` | ∪string|null | 否（可空） |  |
| `use_subtask_model` | ∪bool|null | 否（可空） |  |
| `subtask_custom_api_url` | ∪string|null | 否（可空） |  |
| `subtask_custom_api_key` | ∪string|null | 否（可空） |  |
| `subtask_custom_model_name` | ∪string|null | 否（可空） |  |
| `subtask_provider_type` | ∪string|null | 否（可空） |  |
| `subtask_extra_body` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `min_p` | ∪float|null | 否（可空） |  |
| `repetition_penalty` | ∪float|null | 否（可空） |  |
| `thinking_temperature` | ∪float|null | 否（可空） |  |
| `thinking_top_p` | ∪float|null | 否（可空） |  |
| `thinking_top_k` | ∪int|null | 否（可空） |  |
| `thinking_min_p` | ∪float|null | 否（可空） |  |
| `thinking_presence_penalty` | ∪float|null | 否（可空） |  |
| `thinking_repetition_penalty` | ∪float|null | 否（可空） |  |
| `preserve_thinking` | ∪bool|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `use_custom_model` | bool | 否 | （默认 False） |
| `custom_api_url` | ∪string|null | 否（可空） |  |
| `custom_api_key` | ∪string|null | 否（可空） |  |
| `custom_model_name` | ∪string|null | 否（可空） |  |
| `provider_type` | string | 否 | （默认 'deepseek'） |
| `extra_body` | ∪string|null | 否（可空） |  |
| `use_subtask_model` | bool | 否 | （默认 False） |
| `subtask_custom_api_url` | ∪string|null | 否（可空） |  |
| `subtask_custom_api_key` | ∪string|null | 否（可空） |  |
| `subtask_custom_model_name` | ∪string|null | 否（可空） |  |
| `subtask_provider_type` | ∪string|null | 否（可空） |  |
| `subtask_extra_body` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `min_p` | ∪float|null | 否（可空） |  |
| `repetition_penalty` | ∪float|null | 否（可空） |  |
| `thinking_temperature` | ∪float|null | 否（可空） |  |
| `thinking_top_p` | ∪float|null | 否（可空） |  |
| `thinking_top_k` | ∪int|null | 否（可空） |  |
| `thinking_min_p` | ∪float|null | 否（可空） |  |
| `thinking_presence_penalty` | ∪float|null | 否（可空） |  |
| `thinking_repetition_penalty` | ∪float|null | 否（可空） |  |
| `preserve_thinking` | ∪bool|null | 否（可空） | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/assistants/{assistant_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "system_prompt": "string", "temperature": 0.0, "top_p": 0.0, "top_k": 0, "presence_penalty": 0.0, "frequency_penalty": 0.0, "max_tokens": 0, "use_custom_model": true, "custom_api_url": "string", "custom_api_key": "string", "custom_model_name": "string", "provider_type": "string", "extra_body": "string", "use_subtask_model": true, "subtask_custom_api_url": "string", "subtask_custom_api_key": "string", "subtask_custom_model_name": "string", "subtask_provider_type": "string", "subtask_extra_body": "string", "thinking_budget": 0, "min_p": 0.0, "repetition_penalty": 0.0, "thinking_temperature": 0.0, "thinking_top_p": 0.0, "thinking_top_k": 0, "thinking_min_p": 0.0, "thinking_presence_penalty": 0.0, "thinking_repetition_penalty": 0.0, "preserve_thinking": true}
```

**返回示例**

```json
{
  "name": "string",
  "system_prompt": "string",
  "temperature": 0.0,
  "top_p": 0.0,
  "top_k": 0,
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0,
  "max_tokens": 0,
  "use_custom_model": false,
  "custom_api_url": "string",
  "custom_api_key": "string",
  "custom_model_name": "string",
  "provider_type": "string",
  "extra_body": "string",
  "use_subtask_model": false,
  "subtask_custom_api_url": "string",
  "subtask_custom_api_key": "string",
  "subtask_custom_model_name": "string",
  "subtask_provider_type": "string",
  "subtask_extra_body": "string",
  "thinking_budget": 0,
  "min_p": 0.0,
  "repetition_penalty": 0.0,
  "thinking_temperature": 0.0,
  "thinking_top_p": 0.0,
  "thinking_top_k": 0,
  "thinking_min_p": 0.0,
  "thinking_presence_penalty": 0.0,
  "thinking_repetition_penalty": 0.0,
  "preserve_thinking": true,
  "id": "string",
  "user_id": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/assistants/{assistant_id}/conversations`
**Get Assistant Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/assistants/{assistant_id}/conversations" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 笔记与笔记本 Notes

前缀 `/api/notes`（`app/api/notes.py`）

### GET `/api/notes/default-notebook`
**Get Default Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/default-notebook" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/notes/notebooks`
**List Notebooks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

**输出**

`200` 字段：`List[NotebookResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notebooks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "name": "string",
    "is_default": true,
    "created_at": "string",
    "updated_at": "string",
    "note_count": 0
  }
]
```

---

### POST `/api/notes/notebooks`
**Create Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "is_default": true,
  "created_at": "string",
  "updated_at": "string",
  "note_count": 0
}
```

---

### POST `/api/notes/notebooks/bulk-delete`
**Bulk Delete Notebooks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `notebook_ids` | string[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `deleted_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"notebook_ids": ["string"]}
```

**返回示例**

```json
{
  "status": "string",
  "deleted_count": 0
}
```

---

### POST `/api/notes/notebooks/bulk-export`
**Bulk Export Notebooks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `notebook_ids` | string[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks/bulk-export" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"notebook_ids": ["string"]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/notes/notebooks/{notebook_id}`
**Delete Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/notes/notebooks/{notebook_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/notes/notebooks/{notebook_id}`
**Update Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notebooks/{notebook_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "is_default": true,
  "created_at": "string",
  "updated_at": "string",
  "note_count": 0
}
```

---

### PUT `/api/notes/notebooks/{notebook_id}/default`
**Set Default Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notebooks/{notebook_id}/default" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "is_default": true,
  "created_at": "string",
  "updated_at": "string",
  "note_count": 0
}
```

---

### GET `/api/notes/notebooks/{notebook_id}/export`
**Export Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notebooks/{notebook_id}/export" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/notes/notebooks/{notebook_id}/notes`
**List Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

`200` 字段：`List[NoteListItem]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content_preview` | string | 是 |  |
| `content_length` | int | 否 | （默认 0） |
| `token_estimate` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notebooks/{notebook_id}/notes" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "notebook_id": "string",
    "title": "string",
    "content_preview": "string",
    "content_length": 0,
    "token_estimate": 0,
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/notes/notebooks/{notebook_id}/notes`
**Create Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `content` | string | 否 | （默认 ''） |
| `raw_transcription` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks/{notebook_id}/notes" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "content": "string", "raw_transcription": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### POST `/api/notes/notes/bulk-delete`
**Bulk Delete Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `note_ids` | string[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `deleted_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notes/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"note_ids": ["string"]}
```

**返回示例**

```json
{
  "status": "string",
  "deleted_count": 0
}
```

---

### POST `/api/notes/notes/bulk-export`
**Bulk Export Notes**

> Export multiple notes as PDF or MD. Returns a ZIP archive.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `note_ids` | string[] | 是 |  |
| `format` | string | 否 | （默认 'md'） |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notes/bulk-export" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"note_ids": ["string"], "format": "string"}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/notes/notes/bulk-move`
**Bulk Move Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `note_ids` | string[] | 是 |  |
| `target_notebook_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `moved_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notes/bulk-move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"note_ids": ["string"], "target_notebook_id": "string"}
```

**返回示例**

```json
{
  "status": "string",
  "moved_count": 0
}
```

---

### DELETE `/api/notes/notes/{note_id}`
**Delete Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/notes/notes/{note_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/notes/notes/{note_id}`
**Get Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notes/{note_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### PUT `/api/notes/notes/{note_id}`
**Update Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `content` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notes/{note_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "content": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/notes/notes/{note_id}/export`
**Export Note**

> Export a single note as PDF or Markdown.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |
| `format` | query | string | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notes/{note_id}/export" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/notes/notes/{note_id}/move`
**Move Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `target_notebook_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notes/{note_id}/move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"target_notebook_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### POST `/api/notes/quick`
**Create Quick Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `transcription` | string | 是 |  |
| `notebook_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/quick" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"transcription": "string", "notebook_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/notes/search`
**Search Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `q` | query | string | 是 |  |
| `notebook_id` | query | string | 否 |  |

**输出**

`200` 字段：`List[NoteSearchResult]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `note_id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `notebook_name` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content_snippet` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/search?q=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "note_id": "string",
    "notebook_id": "string",
    "notebook_name": "string",
    "title": "string",
    "content_snippet": "string"
  }
]
```

---


## 后台任务 Agent Tasks

前缀 `/api/agent-tasks`（`app/api/agent_tasks.py`）

### GET `/api/agent-tasks`
**List Tasks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `status` | query | string|null | 否 | Filter by status: pending, claimed, running, completed, failed, cancelled |
| `include_completed` | query | bool | 否 | Include completed/failed/cancelled tasks |
| `limit` | query | int | 否 |  |

**输出**

`200` 字段：`List[AgentTaskResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `goal` | string | 是 |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `iterations_done` | int | 是 |  |
| `iterations_max` | int | 是 |  |
| `elapsed_seconds` | ∪float|null | 否（可空） |  |
| `task_type` | string | 是 |  |
| `result` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `output_conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/agent-tasks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "title": "string",
    "goal": "string",
    "status": "string",
    "progress": 0.0,
    "iterations_done": 0,
    "iterations_max": 0,
    "elapsed_seconds": 0.0,
    "task_type": "string",
    "result": "string",
    "error": "string",
    "output_conversation_id": "string",
    "created_at": "2026-08-26T07:00:00Z",
    "started_at": "2026-08-26T07:00:00Z",
    "completed_at": "2026-08-26T07:00:00Z"
  }
]
```

---

### POST `/api/agent-tasks`
**Create Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `goal` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `task_type` | string | 否 | （默认 'general'） |
| `iterations_max` | int | 否 | （默认 30） |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `goal` | string | 是 |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `iterations_done` | int | 是 |  |
| `iterations_max` | int | 是 |  |
| `elapsed_seconds` | ∪float|null | 否（可空） |  |
| `task_type` | string | 是 |  |
| `result` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `output_conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"goal": "string", "title": "string", "assistant_id": "string", "conversation_id": "string", "task_type": "string", "iterations_max": 30}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "goal": "string",
  "status": "string",
  "progress": 0.0,
  "iterations_done": 0,
  "iterations_max": 0,
  "elapsed_seconds": 0.0,
  "task_type": "string",
  "result": "string",
  "error": "string",
  "output_conversation_id": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### GET `/api/agent-tasks/grilling/{conversation_id}`
**Get Grilling Questions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/agent-tasks/grilling/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/agent-tasks/grilling/{conversation_id}/round-answer`
**Answer Grilling Round**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `answers` | object[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks/grilling/{conversation_id}/round-answer" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"answers": [{}]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/agent-tasks/grilling/{task_id}/answer`
**Answer Grilling Question**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `answer` | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks/grilling/{task_id}/answer" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"answer": "string"}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/agent-tasks/{task_id}`
**Delete Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/agent-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/agent-tasks/{task_id}`
**Get Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `goal` | string | 是 |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `iterations_done` | int | 是 |  |
| `iterations_max` | int | 是 |  |
| `elapsed_seconds` | ∪float|null | 否（可空） |  |
| `task_type` | string | 是 |  |
| `result` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `output_conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/agent-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "goal": "string",
  "status": "string",
  "progress": 0.0,
  "iterations_done": 0,
  "iterations_max": 0,
  "elapsed_seconds": 0.0,
  "task_type": "string",
  "result": "string",
  "error": "string",
  "output_conversation_id": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### POST `/api/agent-tasks/{task_id}/cancel`
**Cancel Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks/{task_id}/cancel" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 定时任务 Scheduled Tasks

前缀 `/api/scheduled-tasks`（`app/api/scheduled_tasks.py`）

### GET `/api/scheduled-tasks`
**List Scheduled Tasks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `status` | query | string|null | 否 | Filter by status: active, paused, cancelled, completed, failed |
| `include_completed` | query | bool | 否 | Include completed/failed/cancelled tasks |

**输出**

`200` 字段：`List[ScheduledTaskResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/scheduled-tasks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "name": "string",
    "prompt": "string",
    "schedule_type": "string",
    "schedule_expr": "string",
    "next_run_at": "string",
    "last_run_at": "string",
    "status": "string",
    "repeat_count": 0,
    "run_count": 0,
    "assistant_id": "string",
    "conversation_id": "string",
    "created_at": "string"
  }
]
```

---

### POST `/api/scheduled-tasks`
**Create Scheduled Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_text` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `repeat_count` | ∪int|null | 否（可空） |  |

**输出**

`201` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/scheduled-tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "prompt": "string", "schedule_text": "string", "assistant_id": "string", "repeat_count": 0}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "prompt": "string",
  "schedule_type": "string",
  "schedule_expr": "string",
  "next_run_at": "string",
  "last_run_at": "string",
  "status": "string",
  "repeat_count": 0,
  "run_count": 0,
  "assistant_id": "string",
  "conversation_id": "string",
  "created_at": "string"
}
```

---

### DELETE `/api/scheduled-tasks/{task_id}`
**Delete Scheduled Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `204`— Successful Response

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/scheduled-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 204
# Successful Response

---

### PUT `/api/scheduled-tasks/{task_id}`
**Update Scheduled Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `prompt` | ∪string|null | 否（可空） |  |
| `schedule_text` | ∪string|null | 否（可空） |  |
| `status` | ∪string|null | 否（可空） |  |
| `repeat_count` | ∪int|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/scheduled-tasks/{task_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "prompt": "string", "schedule_text": "string", "status": "string", "repeat_count": 0}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "prompt": "string",
  "schedule_type": "string",
  "schedule_expr": "string",
  "next_run_at": "string",
  "last_run_at": "string",
  "status": "string",
  "repeat_count": 0,
  "run_count": 0,
  "assistant_id": "string",
  "conversation_id": "string",
  "created_at": "string"
}
```

---

### POST `/api/scheduled-tasks/{task_id}/trigger`
**Trigger Scheduled Task**

> Manually trigger a scheduled task immediately.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/scheduled-tasks/{task_id}/trigger" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "prompt": "string",
  "schedule_type": "string",
  "schedule_expr": "string",
  "next_run_at": "string",
  "last_run_at": "string",
  "status": "string",
  "repeat_count": 0,
  "run_count": 0,
  "assistant_id": "string",
  "conversation_id": "string",
  "created_at": "string"
}
```

---


## 导出任务 Export Tasks

前缀 `/api/export-tasks`（`app/api/export_tasks.py`）

### GET `/api/export-tasks`
**List Export Tasks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `status` | query | string|null | 否 |  |
| `limit` | query | int | 否 |  |

**输出**

`200` 字段：`List[ExportTaskResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `task_type` | string | 是 |  |
| `format` | string | 是 |  |
| `note_id` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `filename` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/export-tasks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "task_type": "string",
    "format": "string",
    "note_id": "string",
    "status": "string",
    "progress": 0.0,
    "filename": "string",
    "error": "string",
    "created_at": "2026-08-26T07:00:00Z",
    "started_at": "2026-08-26T07:00:00Z",
    "completed_at": "2026-08-26T07:00:00Z"
  }
]
```

---

### POST `/api/export-tasks`
**Create Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_type` | string | 否 | （默认 'single'） |
| `format` | string | 否 | （默认 'pdf'） |
| `note_id` | ∪string|null | 否（可空） |  |
| `note_ids` | ∪string[]|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `task_type` | string | 是 |  |
| `format` | string | 是 |  |
| `note_id` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `filename` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/export-tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"task_type": "string", "format": "string", "note_id": "string", "note_ids": ["string"]}
```

**返回示例**

```json
{
  "id": "string",
  "task_type": "string",
  "format": "string",
  "note_id": "string",
  "status": "string",
  "progress": 0.0,
  "filename": "string",
  "error": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### DELETE `/api/export-tasks/{task_id}`
**Delete Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/export-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/export-tasks/{task_id}`
**Get Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `task_type` | string | 是 |  |
| `format` | string | 是 |  |
| `note_id` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `filename` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/export-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "task_type": "string",
  "format": "string",
  "note_id": "string",
  "status": "string",
  "progress": 0.0,
  "filename": "string",
  "error": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### POST `/api/export-tasks/{task_id}/cancel`
**Cancel Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/export-tasks/{task_id}/cancel" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/export-tasks/{task_id}/download`
**Download Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/export-tasks/{task_id}/download" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 记忆 Memory（v2 端点）

前缀 `/api/memory`（`app/api/memory.py`）

### DELETE `/api/memory/all`
**Delete All Memory**

> §10.4 全量擦除（GDPR Art. 17）：新概念层 + 文件层 + 旧 agent_memories；保留 raw 对话与笔记。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/memory/all" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/clarifications`
**List Clarifications**

> §10.4：澄清记录列表（revert 端点的前置——用户需能看到已应用澄清的 ID）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `limit` | query | int | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/clarifications" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/memory/clarifications/{clarification_id}/revert`
**Revert Clarification Endpoint**

> §9.6/§10.4：撤销已应用的澄清（negate 恢复有效；refine/add_constraint 回滚旧版本）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `clarification_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/memory/clarifications/{clarification_id}/revert" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/concepts`
**List Concepts**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `limit` | query | int | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/concepts" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/memory/concepts/{concept_id}`
**Delete Concept**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `concept_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/memory/concepts/{concept_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/memory/concepts/{concept_id}/forget`
**Forget Concept**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `concept_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/memory/concepts/{concept_id}/forget" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/cost_governance/status`
**Get Cost Governance Status**

> §9.10：用户在设置页查看自己的降级状态与触发原因。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/cost_governance/status" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/dreams`
**List Dreams**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `limit` | query | int | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/dreams" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/memory/{user_id}/cost_governance/reset`
**Reset Cost Governance**

> §9.10：admin 手动 reset 用户降级状态（本人可 reset 自己）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/memory/{user_id}/cost_governance/reset" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 技能 Skills

前缀 `/api/skills`（`app/api/skills.py`）

### GET `/api/skills`
**List Skills**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

**输出**

`200` 字段：`List[SkillResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skills" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "name": "string",
    "description": "string",
    "content": "string",
    "is_active": true,
    "id": "string",
    "user_id": "string",
    "source": "string",
    "category": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/skills`
**Create Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "description": "string", "content": "string", "is_active": true}
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/skills/by-name/{skill_name}`
**Get Skill By Name**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_name` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skills/by-name/{skill_name}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### POST `/api/skills/scan-folder`
**Scan Folder Executables**

> Scan an uploaded folder for executable files and return warnings before upload.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.files` | str(binary) — 文件 | 是 |  |
| `form.paths` | string[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/scan-folder" -H "Authorization: Bearer $TOKEN" -F "files=<value>" -F "paths=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/skills/scan-zip`
**Scan Zip Executables**

> Scan zip file for executable files and return warnings before upload.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/scan-zip" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/skills/upload`
**Upload Skills**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `force` | query | bool | 否 | Force upload even with executable warnings |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

`200` 字段：`List[SkillResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/upload" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

```json
[
  {
    "name": "string",
    "description": "string",
    "content": "string",
    "is_active": true,
    "id": "string",
    "user_id": "string",
    "source": "string",
    "category": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/skills/upload-folder`
**Upload Skills Folder**

> Upload skills from a folder (e.g. selected via webkitdirectory).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `force` | query | bool | 否 | Force upload even with executable warnings |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.files` | str(binary) — 文件 | 是 |  |
| `form.paths` | string[] | 是 |  |

**输出**

`200` 字段：`List[SkillResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/upload-folder" -H "Authorization: Bearer $TOKEN" -F "files=<value>" -F "paths=<value>"
```

**返回示例**

```json
[
  {
    "name": "string",
    "description": "string",
    "content": "string",
    "is_active": true,
    "id": "string",
    "user_id": "string",
    "source": "string",
    "category": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### DELETE `/api/skills/{skill_id}`
**Delete Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/skills/{skill_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/skills/{skill_id}`
**Get Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skills/{skill_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### PUT `/api/skills/{skill_id}`
**Update Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | ∪string|null | 否（可空） |  |
| `is_active` | ∪bool|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/skills/{skill_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "description": "string", "content": "string", "is_active": true}
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---


## 语音识别 ASR

前缀 `/api/asr`（`app/api/asr.py`）

### GET `/api/asr/hotwords`
**Get Asr Hotwords**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/asr.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `hotwords` | HotwordItem[] | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/asr/hotwords" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "hotwords": [
    {
      "text": null,
      "weight": null,
      "lang": null
    }
  ]
}
```

---

### POST `/api/asr/hotwords`
**Save Asr Hotwords**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/asr.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hotwords` | HotwordItem[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `hotwords` | HotwordItem[] | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/asr/hotwords" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"hotwords": [{"text": "string", "weight": 0, "lang": null}]}
```

**返回示例**

```json
{
  "hotwords": [
    {
      "text": null,
      "weight": null,
      "lang": null
    }
  ]
}
```

---

### POST `/api/asr/transcribe`
**Transcribe Audio**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/asr.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |
| `form.custom_hotwords` | string|null | 否 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `text` | string | 是 |  |
| `language` | ∪string|null | 否（可空） |  |
| `timestamps` | ∪TimestampInfo[]|null | 否（可空） | （默认 []） |
| `segments` | ∪SegmentInfo[]|null | 否（可空） | （默认 []） |
| `hotwords_used` | ∪string[]|null | 否（可空） | （默认 []） |
| `speaker_mode` | ∪string|null | 否（可空） | （默认 'disabled'） |
| `duration` | ∪float|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/asr/transcribe" -H "Authorization: Bearer $TOKEN" -F "file=<value>" -F "custom_hotwords=<value>"
```

**返回示例**

```json
{
  "text": "string",
  "language": "string",
  "timestamps": [
    null
  ],
  "segments": [
    null
  ],
  "hotwords_used": [
    "string"
  ],
  "speaker_mode": "string",
  "duration": 0.0
}
```

---


## 语音对话 Voice

前缀 `/api/voice`（`app/api/voice.py`）

### GET `/api/voice/sessions`
**List Voice Sessions**

> List voice sessions (conversations belonging to the 酬 assistant).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/voice.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/voice/sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/voice/sessions`
**Create Voice Session**

> Create a new voice session under the 酬 assistant.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/voice.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/voice/sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/voice/sessions/{session_id}/messages`
**Get Voice Session Messages**

> Get messages for a voice session (for loading history).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/voice.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/voice/sessions/{session_id}/messages" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 文件 Files

前缀 `/api/files`（`app/api/file_upload.py + app/api/files.py`）

### GET `/api/files/download`
**Download File**

> Download a file from the user's workspace.
> 
> Auth: Bearer header (standard), or ?token= query param (for <img> tags).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 | Absolute path to the file |
| `token` | query | string|null | 否 | JWT token for <img> tag auth |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/files/download?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/files/upload`
**Upload And Parse Files**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `save_to_notebook` | query | bool | 否 |  |
| `notebook_id` | query | string|null | 否 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.files` | str(binary) — 文件 | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `results` | FileParseResult[] | 是 |  |
| `notebook_id` | ∪string|null | 否（可空） |  |
| `notebook_name` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/files/upload" -H "Authorization: Bearer $TOKEN" -F "files=<value>"
```

**返回示例**

```json
{
  "results": [
    {
      "success": null,
      "markdown": null,
      "error": null,
      "file_type": null,
      "filename": null,
      "file_path": null,
      "size": null
    }
  ],
  "notebook_id": "string",
  "notebook_name": "string"
}
```

---


## 图片 Images

前缀 `/api/images`（`app/api/image_upload.py`）

### GET `/api/images/serve`
**Serve Note Image**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/image_upload.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/images/serve?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/images/upload`
**Upload Image**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/image_upload.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/images/upload" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/images/upload-media`
**Upload Note Media**

> Upload an audio/video file into the note media store (noteimg/).
> 
> Returns the same shape as /upload so the frontend insert pipeline is
> identical. Audio ≤50MB, video ≤200MB. Size caps are enforced WHILE
> streaming (never buffer the whole upload into memory first — a
> multi-GB file must be rejected without OOMing the backend).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/image_upload.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/images/upload-media" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 登录/聊天会话 Sessions

前缀 `/api/sessions`（`app/api/sessions.py`）

### GET `/api/sessions/chat-sessions`
**List Chat Sessions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

**输出**

`200` 字段：`List[item]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/sessions/chat-sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {}
]
```

---

### DELETE `/api/sessions/chat-sessions/{session_id}`
**Delete Chat Session**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/sessions/chat-sessions/{session_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/sessions/chat-sessions/{session_id}`
**Get Chat Session**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `object`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/sessions/chat-sessions/{session_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/sessions/user-sessions`
**List User Sessions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

**输出**

`200` 字段：`List[item]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/sessions/user-sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {}
]
```

---

### DELETE `/api/sessions/user-sessions/{session_id}`
**Delete User Session**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/sessions/user-sessions/{session_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 用户管理 Admin

前缀 `/api/admin`（`app/api/admin.py`）

### POST `/api/admin/memory/migration/rollback`
**Admin Migration Rollback**

> §8.5.5：单用户回滚。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/memory/migration/rollback" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/admin/memory/migration/run`
**Admin Migration Run**

> §8.5.1：手动触发迁移。user_id 缺省全量排队；dry_run=true 只读统计。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

请求体为直接量：`object|null`。

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/memory/migration/run" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" --data {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/admin/memory/migration/status`
**Admin Migration Status**

> §8.5.1：各用户迁移进度（读取 metadata_json.migration）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | query | string|null | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/admin/memory/migration/status" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/admin/reload-config`
**Reload Config**

认证：公开　|　源码模块：`app/api/admin.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/reload-config"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/admin/users`
**List Users**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

**输出**

`200` 字段：`List[UserResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/admin/users" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "username": "string",
    "created_at": "string",
    "agent_permissions": {}
  }
]
```

---

### POST `/api/admin/users`
**Create User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `username` | query | string | 是 |  |
| `password` | query | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/users?username=<value>&password=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/admin/users/{user_id}`
**Delete User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/admin/users/{user_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/admin/users/{user_id}`
**Get User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/admin/users/{user_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {}
}
```

---

### PUT `/api/admin/users/{user_id}`
**Update User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |
| `username` | query | string | 否 |  |
| `password` | query | string | 否 |  |
| `is_active` | query | bool | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/admin/users/{user_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 配置 Config

前缀 `/api/config`（`app/api/config.py`）

### GET `/api/config/providers`
**Get Provider Configs**

> Return provider configurations (without sensitive API keys).

认证：公开　|　源码模块：`app/api/config.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/config/providers"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 系统 System

前缀 `/api/system`（`app/api/system.py`）

### GET `/api/system/capabilities`
**Get System Capabilities**

> Return a machine-readable manifest of the harness's capabilities.
> 
> Includes: tool list, subsystem flags, key limits/timeouts, and modes.
> Does NOT include: model names, API keys, file paths, user data.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/system.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/system/capabilities" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 用户偏好 Users（Skin 偏好）

前缀 `/api/users`（`app/api/users.py`）

### GET `/api/users/me/preferences`
**Get Ui Preferences**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/users.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `skin_id` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/users/me/preferences" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "skin_id": "string"
}
```

---

### PUT `/api/users/me/preferences`
**Update Ui Preferences**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/users.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `skin_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `skin_id` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/users/me/preferences" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"skin_id": "string"}
```

**返回示例**

```json
{
  "skin_id": "string"
}
```

---


## 皮肤 Skins

前缀 `/api/skins`（`app/api/skins.py`）

### GET `/api/skins`
**List Skins**

认证：公开　|　源码模块：`app/api/skins.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skins"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/skins/mine`
**List My Skins**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skins/mine" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/skins/upload`
**Upload Skin**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |
| `form.name` | string | 否 |  |
| `form.description` | string | 否 |  |

**输出**

- `201` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skins/upload" -H "Authorization: Bearer $TOKEN" -F "file=<value>" -F "name=<value>" -F "description=<value>"
```

**返回示例**

HTTP 201 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/skins/{skin_id}`
**Delete My Skin**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skin_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/skins/{skin_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/skins/{skin_id}/css`
**Get My Skin Css**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skin_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skins/{skin_id}/css" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200

---

## 其他（main.py / 根路由）

### GET `/`
**Root**

认证：公开　|　源码模块：`main.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/app/frontend/{full_path}`
**Spa Fallback**

认证：公开　|　源码模块：`main.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `full_path` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/app/frontend/{full_path}"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

---

# 二、专项语义（行为级补充）

## 1. 聊天流式（SSE）——`/api/chat/stream*`

`POST /api/chat/stream` 是主聊天接口，返回 **SSE 流**（`text/event-stream`）：创建或复用会话、持久化用户
消息、解析 `[skill:...]` 标记、处理重新生成/编辑（`regenerate_from_message_id` / `edit_message_id`）、管理
死磕模式生命周期，并在后台 `asyncio.Task` 中运行 AgentLoop。**客户端断开后 Agent 继续后台运行**并自行
保存结果；首次完成后自动生成会话标题。

### SSE 事件类型

| event | data 字段 | 说明 |
|---|---|---|
| `content` | `{content}` | 助手正文增量 |
| `reasoning_content` | `{reasoning_content, phase?}` | 思考增量 |
| `content_segment` | `{segment_content}` | 分段内容（死磕多轮输出） |
| `tool_call` | `{name, arguments, call_id}` | 工具调用 |
| `tool_result` | `{name, call_id, result, error}` | 工具执行结果 |
| `iteration` | `{iteration, max_iterations}` | 工具循环轮次（`[agent.tool_loop] max_iterations`，默认 50） |
| `ping` | `{ping: true}` | 心跳（30s 空闲） |
| `message` | `{agent_step: {...}}` | Agent 步骤（工具/推理/搜索进度） |
| `message` | `{attachments: [...]}` | 生成的文件附件（下载卡） |
| `message` | `{permission_request: {request_id, tool_name, description, details}}` | 敏感工具执行前的权限请求（经 `/api/chat/permission/respond` 应答） |
| `message` | `{deathmatch_verdict: {...}}` | 死磕裁判结论（结构见 2.8） |
| `message` | `{title_update: {conversation_id, title}}` | 会话标题自动命名（在 done 之后） |
| `error` | `{error}` | 错误 |
| `done` | `{conversation_id, message_id, title, tool_results, done: true}` | 终止事件；`tool_results` 为 JSON 字符串：`{rounds, results, search_failed, agent_steps, attachments?, display_sequence?, content_segments?}` |

> 前端以 `conversationId` 为键并行管理多路 SSE；引用台账 `[N]` 由 `citation_ledger` 单调编号，正文中的
> `[N]` 均能在 `done.tool_results.results` 中找到来源。

### 断线重连与状态查询

- `POST /api/chat/stream/resume`：重连进行中（或刚完成）的流。请求体 `{conversation_id}`。响应先发送原子
  快照事件 `message`：`{replay: {...}, status: "complete"|"incomplete", is_running}`，随后接续实时事件。
  若已完成则仅发快照 + `done`。优先读 `StreamBuffer`，否则回退 `ActiveAgentRegistry`。死磕模式同样适用。
- `GET /api/chat/stream/status/{conversation_id}`：`{has_buffer, status: "running"|"complete"|"error"|"none", is_running, db_message_id, content_length}`。
- `POST /api/chat/stream/stop/{conversation_id}`：停止后台任务。`{status: "cancelled"|"not_running", conversation_id}`。
  若 run 仍处 setup 阶段（无已创建 Agent 任务），会释放该会话仍处 provisional 状态的 session-lock 槽位，
  保证编辑后重发不被 `conversation_busy` 误拒。
- `POST /api/chat/permission/respond`：`{request_id, approved: bool}` → `{status: "ok", approved}`；
  request 不存在或过期 → 404。

## 2. 会话健壮性说明

- `sort_order` 允许被库级写入置 NULL（ORM 默认值不覆盖显式 NULL INSERT）；启动迁移
  `conversations_sort_order_backfill` / `conversation_groups_sort_order_backfill` 幂等回填 0，
  响应构造层再做 `None→0` 防御——单行脏数据不引发 500。
- `DELETE /api/conversations/{id}` 会连带取消该会话的活跃定时任务。
- `GET /api/conversations/{id}` 返回前执行死磕僵尸状态修复（见 2.7）。

## 3. provider 语义（助手模型配置）

`provider_type` 可选值（wire 值，前端下拉显示名括号内为中文名）：

| wire 值 | 前端显示名 | 语义 |
|---|---|---|
| `deepseek` | DeepSeek | OpenAI 兼容 + DeepSeek 思考参数格式（`reasoning_effort`: `low`/`high`/`max`） |
| `qwen3.8_vllm` | **Qwen3.8(Local)** | 按 modelscope vLLM 部署指南格式调用本地/自托管服务；**地址/密钥/模型名由助手配置**：`custom_api_url`/`custom_api_key`/`custom_model_name` 优先，留空回退服务器 `[providers."qwen3.8_27b"]` 配置 |
| `mimo` | MiMo (Xiaomi) | 小米 MiMo（含 TTS/ASR 供应商链） |
| `custom` | 自定义 | 任意 OpenAI 兼容端点（可 `extra_body` 直传 JSON） |
| `zhipu` / `qwen` | — | 后端分支保留兼容，无前端入口 |

Qwen3.8(Local) 供应商采样参数分两套（思考/非思考），字段为 NULL 时使用模型卡默认值（思考：
temperature 1.0 / top_p 0.95 / top_k 20 / min_p 0.0 / presence_penalty 0.0 / repetition_penalty 1.0；
非思考：0.7 / 0.80 / 20 / 0.0 / 1.5 / 1.0）。`preserve_thinking`（默认 true）经
`chat_template_kwargs.preserve_thinking` 传给 vLLM；`reasoning_effort` 取 `xhigh`/`medium`/`low` 走
`chat_template_kwargs`。`reasoning` 请求参数（`enable_reasoning` / `reasoning_effort` / `thinking_budget`）
全链路继承助手配置（P0：同助手下一切 LLM 行为默认走该助手模型）。

## 4. 技能 Skills 行为补充

- 双来源：**用户技能**（数据库，可增删改）+ **系统技能**（`backend/skills/*/SKILL.md`，只读，
  `source="system"`，ID 形如 `system:<name>`，数据库接口不解析系统技能 ID，用 `GET /api/skills/by-name/{name}`）。
- `POST /api/skills` 重名（用户/系统均查）拒绝；`PUT` 改名时再次检查冲突。
- ZIP 上传按根目录分组，需含 `SKILL.md`；`scan-zip` / `scan-folder` 先扫描可执行/危险文件（**不落库**），
  有告警时上传需 `?force=true`。
- 上传的脚本经 `skill_run_script` 工具在用户工作区沙箱执行（非服务端随意执行）。

## 5. 语音（ASR + Voice）

### REST（总览见一部分）

- `POST /api/asr/transcribe` 表单 `file`（音频）+ `custom_hotwords?`（JSON 字符串，如
  `[{"text":"foo","weight":3}]`）。响应含 `text`、可选 `timestamps` / `segments`（说话人分离）/
  `hotwords_used` / `speaker_mode` / `duration`。
- 热词 `GET/POST /api/asr/hotwords`：POST 整体替换（先删后插，权重 1–5，尝试同步 DashScope 词表）。
- 每用户有专属**语音助理**（`ensure_voice_assistant()` 在首次语音会话时创建），独立于默认助手。

### WebSocket

- `WS /api/voice/ws?conversation_id=`：全双工语音对话（`VoiceDuplexSession`，支持 barge-in 打断 + 断点续播）。
  语音模式禁用时以 `1013` 关闭。
- `WS /api/asr/ws/transcribe/stream`：实时流式转写（代理到底层 ASR）。认证失败以 `1008` 关闭；
  错误帧 `{"event":"error","error":"..."}`。
- 服务端 → 客户端节选事件：

| 事件 | 内容 | 说明 |
|---|---|---|
| `filler` | `{"event","text"}` | 回答生成期间先口播的随机填充词前缀（`[voice] filler_*`，可关闭） |
| `agent_backchannel` | `{"event","text"}` | 用户句间停顿内的短应和（`backchannel_*`，可关闭） |
| `interjection` | `{"event","text","emotion","raw_text","kind"?}` | 助手主动插话：`kind` 空=追问式；`memory_append`=记忆补充；`memory_correct`=自我纠正（默认关 `memory_correct_enabled=false`） |
| `tts_*` / `asr_*` | 音频分段与识别状态 | 播放进度/打断偏移量，前端渲染语音条 |

- **记忆插话（L0）**：每用户轮 fire-and-forget 调 `retrieve_with_meta`，结果以哨兵区块
  原地替换 identity prompt 记忆段（下轮回答自动接地）；另派仲裁 LLM 判是否值得插话（append/correct/none，
  LLM-only 无关键词分类），门控：`memory_interjection_min_score`(0.5)、预算 `max_append`(3)/`max_correct`(2)、
  冷却 20s、同 candidate-id 去重。插话落库为 assistant 消息（Web 可见、重连可重建）；仲裁输出强制过
  注入消毒（不受 `super_admin_bypass` 影响）。
- filler/backchannel 走 aux TTS 队列串行播报（绝不与回答/插话重叠），填充词在回答首段音频就绪时截断；
  记忆插话是普通项（完整播完，不被截断）。

## 6. 文件 / 图片 行为补充

- `POST /api/files/upload`：`save_to_notebook=false` 流式存工作区 `uploads/`（单文件 ≤1GB）；
  `=true` 解析文档（Word/PPT/Excel/CSV/PDF → Markdown，处理内嵌/本地/远程图片）存为笔记（图片文件拒绝）。
- `GET /api/files/download`：认证支持 `Authorization` 头**或** `?token=`（供 `<img>` 标签）；路径不在
  工作区时按文件名递归查找。
- `GET /api/images/serve`：路径逃逸工作区 403；不存在 404。
- 笔记导出 PDF 走 `POST /api/export-tasks` 异步（WeasyPrint，Markdown→HTML→PDF，支持 Mermaid/LaTeX、
  本地图片 base64 内嵌），`/download` 取件。

## 7. 死磕模式（Deathmatch）全状态机

死磕模式是持久化多轮目标完成循环：**盘问（Grilling）** + **目标循环（Goal Loop / PEVR）** 两阶段，
API 分布在聊天流（`/api/chat/stream`）、后台任务（`/api/agent-tasks/grilling/*`）与会话
（`/api/conversations`）三个模块。

- **盘问（Phase 1）**：2–3 轮、每轮 2–3 个澄清问题；每个问题是 `task_type="grilling"` 的 AgentTask
  （`pending`）。全部回答后 LLM 合成目标，转目标循环。
- **目标循环（Phase 2 / PEVR）**：P 计划 → E 执行 → V 裁判验证 → R 重规划。裁判每轮评估目标是否满足，
  满足则 `done`；停滞三级升级：`<3` 正常续接 · `≥3`（`stall_partial_threshold`）→ `partial_complete`
  · `≥6`（`stall_hard_threshold`）/ `max_turns` 耗尽 / `max_wall_time_seconds` 超时 → `human_gate`
  （均需用户经 `deathmatch_action="resume"` 介入）。

### 会话上的死磕字段（`ConversationResponse` 补齐）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `deathmatch_mode` | bool | false | 死磕开关 |
| `deathmatch_status` | str | "inactive" | `inactive`/`grilling`/`active`/`done`/`paused`/`human_gate`/`partial_complete` |
| `deathmatch_goal` | str? | null | 合成后的目标描述 |
| `deathmatch_turns` / `deathmatch_max_turns` | int / int | 0 / 30 | 当前轮 / 预算（`[deathmatch].max_turns` 覆盖） |
| `deathmatch_grilling_total` / `_completed` / `_round` / `_round_total` | int | 0/0/0/3 | 盘问进度 |
| `deathmatch_reason` | str? | null | 状态原因（用户可见） |
| `deathmatch_context_summary` | str? | null | 上下文压缩摘要 |
| `deathmatch_plan` / `deathmatch_plan_version` | dict? / int | null / 0 | PEVR 计划（`steps: [{id, description, status}]`）与修订计数 |
| `deathmatch_expected_marker` / `_marker_miss_count` | str? / int | null / 0 | 上下文标记与丢失计数 |
| `deathmatch_compressed_context` | str? | null | 压缩上下文（JSON） |

### start/stop 控制（经 `POST /api/chat/stream` 字段）

| `deathmatch_mode` | `deathmatch_action` | 行为 |
|---|---|---|
| true | null | 按当前状态自动推进（grilling→分类意图；paused→恢复检测；done→意图分类） |
| true | "start" | 任意状态激活：压缩上下文 + 启动盘问 |
| true | "stop" | 停用并清理状态 |
| true | "pause" / "resume" | 暂停 / 恢复（盘问中→grilling，盘问完成→active，重置墙钟） |
| false | — | 普通聊天（不受死磕逻辑影响） |

### `POST /api/chat/deathmatch/subgoal`（追加验收标准）

请求体 `{"conversation_id": "str", "text": "≤500 字符"}` → `{"status": "ok", "subgoals": [str, ...]}`；
裁判每轮检查全部标准。错误：400（非死磕目标循环/空/超 20 条）、404（会话不存在或非本人）。

### SSE 事件 `deathmatch_verdict`（结构）

```json
{
  "status": "grilling|active|done|paused|human_gate|partial_complete",
  "verdict": "grilling_questions_generated|grilling_recovered|continue|done|wait|…",
  "reason": "自然语言原因说明",
  "turns": 3, "max_turns": 30,
  "grilling_completed": 1, "grilling_total": 3,
  "grilling_round": 1, "grilling_round_total": 3,
  "grilling_questions": [],
  "verify_result": {},
  "human_gate": "...",
  "final_attachments": [],
  "final_summary_table": "...",
  "plan_version": 1,
  "plan_steps": []
}
```

多轮输出经 `content_segment` 事件分段，每轮内容作为独立 `Message` 落库。

### 盘问阶段 API（行为）

- `GET /api/agent-tasks/grilling/{conversation_id}`：列出当前轮问题
  （`task_id` / `question` / `recommendation` / `options`(2–4 个) / `round` / `status` / `answer?`）。
- `POST /api/agent-tasks/grilling/{task_id}/answer`：单题回答 `{"answer"}`；本轮答完 → 推进下一轮或
  合成目标转 `active`；`result.status ∈ {grilling_in_progress, next_round, grilling_complete}`。
- `POST /api/agent-tasks/grilling/{conversation_id}/round-answer`：批量 `{"answers": [{task_id, answer}]}`；
  不全 → `{"status": "incomplete", ...}`。

### 僵尸状态修复

`GET /api/conversations/{id}` 返回前执行 `_reconcile_deathmatch_status()`：
`status="active"` 但无 Agent 运行且 `updated_at` 超 60s → `paused`；`grilling` 但问题全答完 → 合成目标转
`active`；60s 内被写过的会话跳过（防与 `/chat/stream` 竞争）。

## 8. 记忆检索管线（内部行为，无独立端点；成本治理端点见一部分）

记忆上下文在聊天/语音请求内自动注入系统提示。行为与关键配置：

- **入口**：`retrieve_and_build_context(db, user_id, messages)`；meta 入口
  `retrieve_with_meta(...) -> (ctx, memory_ids, top_gate_score)`（语音每轮召回用；ctx 与旧入口逐字节一致）。
  门控：`[memory].enabled` + `[memory].retrieval_enabled` + 运行时探测（pgvector / embedding provider）。
- **四阶段**：Stage 0 jieba 分词 +（可选）LLM 相对时间归一 → Stage 1 BM25（概念/事件/原文三索引）→
  Stage 2 描述 BM25 + 1-hop 关系扩展 → Stage 3 embedding 余弦 + RRF 融合
  （同 tier 尺度一致化：BM25-only 候选归一化到 `[0, bm25_weight]`；`embedding_sim_threshold` 默认 0.3 滤弱）。
- **Stage 4 交叉编码器重排（模式 B）**：`rerank_mode=cross_encoder` 调
  `[memory].rerank_api_base`（`POST /rerank`），候选池 tier 均衡（concept 8 / episodic 4 / subconscious 4），
  raw logits min-max 归一化写 `calibrated_score`；失败回退 score_only。**早退**：top1-top3 差距 ≥
  `stage4_llm_trigger_score_gap` 或 top1 `embedding_sim` ≥ `stage4_ce_skip_strong_top_sim`(0.7) 时跳过。
- **画像写路径**：调度器每日从近 14 天 daily-summary 提炼 profile 事实 → upsert 为
  `memory_type='profile'` 概念（幂等，`profile_sync_enabled` 总开关）；读路径优先 profile 概念
  （≤600 字符恒定基底，优先级 0.30）。
- **冷启动回退**：概念 ≤5 时若近 30 天有带 embedding 的 subconscious 内容则继续正常管线；否则 v1 摘要
  / 多模态 fallback。
- **复合终分**：relevance/recency/weight 线性融合（`fusion_*`）；relevance 用**绝对校准分**（跨 tier 可比）。
- **注入组装**：段按绝对相关分降序，预算 `injection_total_token_budget`(默认 2000) 从最低优先段截断；
  恒定基底 `[用户画像 Profile]` + `[用户长期记忆总览]`；命中段 `[相关事件 Episodic]` /
  `[相关概念 Concept]` / `[近期原文片段 Subconscious]`，再后 `[近期 Dream]` / `[相关概念集合 Clusters]` /
  `[澄清提示]`。
- **Dream**：`[近期 Dream]` = `agent_dreams` 表 `dream_type='consolidation'` 最新行（consolidation 管线
  每日生成，不受 `dreaming_enabled` 门控）；v1 nightly dream 分离为 `dream_type='nightly'`（不注入）。
- **会话缓存**：最近 3 轮查询哈希，TTL 5min。
- **成本治理端点**（`/api/memory/cost_governance/status`、`/api/memory/{user_id}/cost_governance/reset`）：
  升级当 `today_calls > max(7 日均值 × warn_multiplier, min_today_calls)`（绝对下限默认 8）；
  恢复当 `today_calls ≤ 7 日均值 × recovery_ratio`（默认 1.0）。`reason` = 当前降级触发式（降级时保留）；
  `last_change` = 最近一次等级迁移动作记录。

## 9. 皮肤 Skins 信任模型（上传皮肤）

- `POST /api/skins/upload`（multipart：`file` .css ≤300KB、`name?`、`description?`）：
  id = 文件名 stem，须匹配 `^[a-z0-9][a-z0-9-]{0,49}$`；同名 upsert。
- 校验失败 400 `{"detail"}`：css file required / skin css too large / invalid skin id / decode /
  **missing anchor**（须含 `[data-skin="<id>"]`）/ **unbalanced braces** / **forbidden in skin css**
  （`<` 字符、`expression(`、`javascript:`、含 http 的 `@import`；`>` 子代选择器合法不禁）。
- 占用内置 id → 409。
- **信任模型**：护栏 = 格式校验 + per-user 权限边界，**非 CSS 沙箱**（`data:` 等变体不在格式护栏内；
  上传者对自己会话的 DOM 负全责——只上传自己审查过的 CSS）。
- 用户仅能查看/删除自己上传的皮肤（`/api/skins/mine`、`/api/skins/{id}/css`、`DELETE /api/skins/{id}` 越权
  一律 404/401）。内置目录 `GET /api/skins`（公开）；用户偏好 `GET/PUT /api/users/me/preferences`
  （`skin_id` 校验为「内置目录 ∪ 本人已上传」，否则 400）。契约与设计见 `docs/SKINS.md`。

## 10. 管理端点补充

- `POST /api/admin/reload-config`：需请求头 `x-admin-secret: <[security].jwt_secret_key>`，否则 403。
- `GET /app/frontend/{path}`：SPA 静态资源 + `index.html` 兜底（路由前缀见 `vite.config.ts` / `router`）。
- `GET /`：有前端构建 → 307 到 `/app/frontend/`；否则 JSON 运行标记。

## 11. 认证生命周期（refresh 语义）

`POST /auth/refresh`：持**仍有效**的 Bearer Token 调用，以单条原子 UPDATE
（`WHERE session_token = <旧 token>`）轮换 `UserSession` 行（新 `jti`，`exp ≈ now + token_expire_days`），
并刷新 `last_active_at`。该行 logout 后其 token **永久不可续期**（行缺失/并发轮换输家 → 401，不签发）；
取消/篡改 → 401；用户停用 → 403；无 Authorization 头 → 403（HTTPBearer）。前端应用启动时静默调用，
`/auth/refresh` 在 401 跳转白名单内。策略：持续活跃可无限续期（无 idle/绝对上限）——自托管单租户场景的
有意取舍。

---

*本文档由生成器从运行中后端的 `/openapi.json` 生成；接口行为变化后以运行中后端重跑生成器即可再生。*
