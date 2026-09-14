<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Weave Thinker 使用手册

> 适用版本：v0.0.1（2026-09）。本文面向**使用者与自部署管理员**：从零部署 → 首次配置 → 全部功能操作 → 数据备份与故障排查。
> 相关文档：[README](../README.md)（项目门面）· [docs/ARCHITECTURE.md](ARCHITECTURE.md)（机制级架构）· [docs/API.md](API.md)（接口参考）· [requirements/](../requirements/DEPLOYMENT.md)（分平台部署）。

## 目录

1. [快速开始](#1-快速开始)
2. [首次配置](#2-首次配置)
3. [账号与登录](#3-账号与登录)
4. [界面导览](#4-界面导览)
5. [对话](#5-对话)
6. [助手](#6-助手)
7. [语音对话](#7-语音对话)
8. [死磕模式](#8-死磕模式)
9. [后台任务与定时任务](#9-后台任务与定时任务)
10. [笔记与知识库](#10-笔记与知识库)
11. [记忆系统](#11-记忆系统)
12. [技能与 MCP](#12-技能与-mcp)
13. [皮肤与外观](#13-皮肤与外观)
14. [数据、备份与升级](#14-数据备份与升级)
15. [运维与故障排查](#15-运维与故障排查)
16. [安全说明](#16-安全说明)
17. [附录](#17-附录)

---

## 1. 快速开始

Weave Thinker 是自托管服务，两种部署方式任选：

| 方式 | 适合场景 | 需要什么 |
|---|---|---|
| **Docker 快速部署**（推荐） | 一条命令拉起「应用 + PostgreSQL」，不污染宿主机环境 | Docker / Docker Desktop / colima |
| 手动部署 | 已有 Python/Node/PostgreSQL 环境；需要直接改代码 | Python ≥ 3.10（推荐 3.12/3.13）· Node ≥ 18（推荐 20/22）· PostgreSQL ≥ 14 |

### 1.1 Docker 快速部署（推荐）

前置：安装 Docker（Windows/macOS 用 Docker Desktop；Linux 用 docker engine + compose 插件；macOS 也可用 `brew install colima docker docker-compose`）。

```bash
# 1) 获取代码
git clone <你的仓库地址> weave-thinker
cd weave-thinker

# 2) 生成两份配置（模板 → 实配）
cp backend/config.toml.example       backend/config.toml
cp backend/config_model.toml.example backend/config_model.toml
cp docker/.env.example               .env

# 3) 编辑 backend/config.toml（三处必改）
#    [database] host     = "db"            # compose 里的数据库服务名，不能是 127.0.0.1
#    [database] username/password/name     # 与下面 .env 的 POSTGRES_* 保持一致
#    [security] jwt_secret_key             # 随机长串：openssl rand -hex 32
#
#    编辑 .env（可选）
#    POSTGRES_PASSWORD=...                 # 与 config.toml [database] 一致
#    APP_PORT=8158                         # 对外端口（被占用时改这里）
#
#    编辑 backend/config_model.toml
#    至少填写一个 LLM 端点（base_url / api_key / model_name）

# 4) 一键构建并启动
./scripts/docker_start.sh
```

启动完成后访问 `http://<服务器IP>:8158/app/frontend/`，注册第一个账号即可开始。

常用命令：

```bash
./scripts/docker_start.sh        # 构建 + 后台启动 + 等待就绪（等价：docker compose up -d --build）
./scripts/docker_stop.sh         # 停止（保留数据卷）；加 --volumes 连数据一起删
./scripts/docker_build.sh        # 只构建镜像（默认 tag weave-thinker:local）
docker compose logs -f app       # 跟踪应用日志
docker compose ps                # 查看容器健康状态
```

说明：

- **数据持久化**：PostgreSQL 数据与用户工作区/记忆/音频/导出/自定义皮肤分别落在命名卷
  `pgdata`、`workspace_data`、`memories_data`、`audio_data`、`output_data`、`skins_data` 中，
  `docker compose down` 不会丢数据。
- **浏览器工具（可选）**：镜像默认不装 Chromium（体积小、构建快）。需要「浏览器 10 件套」时在 `.env` 加 `WITH_BROWSER=1` 后重新 `./scripts/docker_start.sh`（镜像会明显变大）。
- **HTTPS（可选）**：默认 HTTP，生产建议由 Nginx/网关终结 TLS 后反代到 8158；也可以把
  `key.pem`/`cert.pem` 放进 `backend/certs/` 目录并挂载，容器入口会自动检测并启用 HTTPS
  （详见 [15.4](#154-tls-与反向代理)）。
- **不挂载配置的试跑**：直接 `docker run` 或未提供配置文件时，容器入口会从镜像内模板 +
  `.env` 变量自动生成一份临时配置（重建容器即丢）。长期使用请按上面挂载两份配置。

### 1.2 手动部署（摘要）

分平台逐步命令（含系统依赖、数据库初始化、证书、构建、启停与排查）：

| 平台 | 文档 |
|---|---|
| macOS | [requirements/macos.md](../requirements/macos.md) |
| Ubuntu / Debian | [requirements/ubuntu.md](../requirements/ubuntu.md) |
| Windows / WSL2 | [requirements/windows.md](../requirements/windows.md) |
| 依赖清单 | [requirements/dependencies.md](../requirements/dependencies.md) |

最简流程：

```bash
# PostgreSQL 建库建用户（已有库可跳过，[database] 指向远端即可）
# 后端环境
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 配置（同 1.1 第 2-3 步；手动部署时 [database] host 用真实地址）
cp backend/config.toml.example backend/config.toml
cp backend/config_model.toml.example backend/config_model.toml

# 自签证书（生产建议换成正式证书；Android 壳与前端 dev 必须走 TLS）
cd backend && openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=localhost" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" && cd ..

# 构建前端 → backend/static/，然后启停
./scripts/project_build.sh
./scripts/start.sh      # stop.sh / restart.sh / status.sh
```

### 1.3 验证部署成功

```bash
curl -k https://<host>:8158/docs              # Swagger UI，返回 200
curl -k -o /dev/null -w '%{http_code}\n' \
     https://<host>:8158/app/frontend/login   # 登录页，返回 200
```

Docker 部署默认是 HTTP，去掉 `-k https` 用 `http://` 即可。若公网访问不通，按
[requirements/ubuntu.md「端口排查阶梯」](../requirements/ubuntu.md)（安全组 → NAT/端口映射层 → 供应商）逐级定位。

---

## 2. 首次配置

配置文件只有两个（都在 `backend/` 下）：

| 文件 | 内容 | 是否入库 |
|---|---|---|
| `config.toml` | 基础设施与行为参数：`[server]` `[security]` `[database]` `[workspace]` `[browser]` `[asr]` `[voice]` `[deathmatch]` `[memory.*]` `[agent.*]` 等 | 是（**含密钥，注意权限**） |
| `config_model.toml` | 全部模型端点与路由：`[endpoints.*]` 端点池（按 LLM/VLM/ASR/TTS/Embedding/Rerank 分区）、`[routing]` 用途路由、`[defaults]` 采样默认 | 否（gitignored，**切勿提交**） |

> `[secrets]`（web_search / context7 等第三方检索 key）与 `[mcp]`（外部 MCP 服务）都属于**基础设施段，配置在 `config.toml`**（`config_model.toml` 只保留模型端点/路由/采样默认；旧版把 `[secrets]` 放在模型文件里仍可兼容合并，但新部署请写在 `config.toml`）。

模板：`backend/config.toml.example`、`backend/config_model.toml.example`（或 `backend/config_model.toml` 的注释，逐键说明见 `backend/app/core/config.py`）。

### 2.1 必改项

```toml
# backend/config.toml
[server]
host = "0.0.0.0"
port = 8158

[security]
jwt_secret_key = "<openssl rand -hex 32 生成>"   # 必改；空值会拒绝启动

[database]
host = "127.0.0.1"        # Docker 部署填 "db"
port = 5432
username = "weavethinker"
password = "你的数据库密码"
name = "weavethinker"
```

### 2.2 模型端点（`config_model.toml`）

结构三区：**① 端点池**（信息只定义一次）→ **② 路由**（用途 → 池中别名）→ **③ 采样默认与密钥**。

```toml
# ① 至少一个 LLM 端点
[endpoints.main]
kind = "llm"
base_url = "https://api.deepseek.com/v1"
api_key = "sk-..."            # 你的 key
model_name = "deepseek-v4-flash"
display_name = "默认模型"

# ② 用途路由（把用途指向端点别名）
[routing]
main = "main"
```

要点：

- **OpenAI 兼容格式**：云端 API 或本地 vLLM/Ollama 等自托管服务均可（`base_url` 指向你的服务）。
- **语音（可选）**：`[endpoints.asr]`（DashScope FunASR / MiMo / general）、`[endpoints.tts]`（MiMo）——不配置时文字对话不受影响，语音功能不可用。
- **记忆 v2 向量检索（可选）**：`[endpoints.embedding]`、`[endpoints.rerank]`——不配置时记忆子系统自动降级并在日志记录原因。
- **视觉（可选）**：`[endpoints.vlm]` 供 `vision_interpret` 工具与死磕视觉评审使用；未配置时该工具返回友好错误。
- **占位不阻塞启动**：`<YOUR-*>` 占位未填时服务照常启动、页面可登录，仅对应功能在真实调用时报缺配。
- 修改 `[endpoints.*]` / `[routing]` 后重启服务生效（或调用 `model_gateway.reload_model_registry()`）。

### 2.3 前端如何选模型

助手只按**逻辑别名**选择模型（不出现供应商 URL/key/参数）：系统设置 →「模型供应商」里可为当前用户配置自定义供应商与 key（仅自己可见），在「用户信息」维护昵称/头像。管理员也可以在 `config_model.toml` 里为全站准备多个端点别名，创建助手时按别名选择。

---

## 3. 账号与登录

- **注册**：登录页 →「注册新用户」。用户名 2-50 字符，密码至少 6 位；注册成功后自动创建默认助手。
- **账号与管理员**：注册默认 `role=user`（系统没有「首用户自动成为管理员」的机制）。少数管理端点
  （如记忆迁移 `/api/admin/memory/*`）要求 `role='admin'`，可在数据库中提升：
  `UPDATE users SET role='admin' WHERE username='你的用户名';`（改完重新登录）。
- **登录态**：JWT 存于浏览器 `localStorage`（key `chatllm_token`），默认 7 天有效并滑动续期；退出登录会清除本地令牌。
- **多用户**：每个用户的数据（会话、笔记、记忆、工作区、语音热词、皮肤偏好）互相隔离。

---

## 4. 界面导览

主界面（`/app/frontend/`）分三块：

```
┌──────────────┬──────────────────────────────────────────────┐
│ 侧边栏        │ 对话区                                        │
│ · 助手切换/新建 │ · 消息流（Markdown/公式/图表/媒体/工具卡片）    │
│ · 会话列表/分组 │ · 死磕状态条（启用时）                        │
│ · 笔记本/笔记  │ · 输入区（附件/深度思考/死磕/语音/插话）        │
│ · 语音助理     │                                              │
│ · 系统设置     │                                              │
└──────────────┴──────────────────────────────────────────────┘
```

- **侧边栏**：顶部切换/新建助手；中部是当前助手的会话列表（可分组、拖拽排序、右键菜单：重命名/移动/导出/删除）；「笔记」入口打开笔记本面板（新建/重命名/移动/删除、点开即在工作台编辑）；底部是「语音助理」「系统设置」与头像菜单（皮肤、明暗、退出登录）。
- **对话区**：顶部显示当前会话标题与上下文 token 徽章（CJK 感知估算）；消息流中的工具调用、思考过程、引用角标都可展开或点开；底部输入区。
- **移动端**：窄屏（≤767px）自动切换为抽屉式布局，笔记/会话列表全屏展示；Android 壳（`webview-app/`）内置信任自签证书。

### 4.1 系统设置（七个面板）

| 面板 | 作用 |
|---|---|
| 用户信息 | 昵称、头像等个人资料 |
| 模型供应商 | 用户级模型供应商覆盖（仅本人生效；自定义 URL 的 Key 绝不回落系统 Key） |
| 热词配置 | 语音识别热词（人名/行话，同音字纠音） |
| 权限管理 | 工具权限开关（终端执行、笔记删除等敏感操作的审批策略） |
| 技能管理 | 用户技能列表、上传（zip/文件夹）、删除 |
| 记忆管理 | 记忆概念/梦境/澄清记录浏览，遗忘与一键擦除 |
| 皮肤 | 三套内置皮肤 × 明暗，自定义皮肤上传 |

---

## 5. 对话

### 5.1 基本操作

| 操作 | 方式 |
|---|---|
| 发送 | Enter（可在设置中改为 Ctrl/⌘+Enter 语义以界面为准）；Shift+Enter 换行 |
| 停止生成 | 生成中点击停止按钮；已生成的部分会保留并落库 |
| 重新生成 | 消息操作菜单 →「重新生成」 |
| 编辑重发 | 用户消息 →「编辑」修改后重发 |
| 插话 | 生成中继续输入并发送 = 插话（当前轮继续，不会被当成新会话）；插话暂不支持附件 |
| 草稿箱 | 输入区草稿按钮：把未发送内容存为草稿，随时一键发送/删除 |
| 新建会话 | 侧边栏「新建对话」；深链 `?conv=<会话ID>` 可直达 |

### 5.2 文件上传

支持拖拽到输入区、点击「+ → 上传文件」或粘贴图片：

| 类型 | 处理方式 |
|---|---|
| 图片 | `vision_interpret`（VLM 视觉解读）：截图、报错弹窗、图表、设计稿、扫描件 |
| 音频 | `asr_transcribe`（ASR 转写）后进入对话；转写结果可存为笔记 |
| 文档 | PDF（pdfplumber 主 + pdfminer 兜底）、Word / Excel / PPT（Office 技能）、CSV/文本 |
| 音视频媒体 | 行内播放；直链媒体自动下载进工作区后嵌入 |

上传的文件落在**每用户隔离的工作区**（`user_workspaces/<用户名>-<ID前8位>/`），agent 可通过工作区工具继续读写。

### 5.3 工具调用与权限审批

- agent 的每一步工具调用都会在消息流里生成**可展开的卡片**（参数、结果、耗时、错误）。
- **敏感操作走审批**：删除笔记、执行终端命令等权限键开启时，会弹出审批对话框（允许一次/拒绝），选择会回传给 agent 继续执行；可在「系统设置 → 权限管理」调整各权限键的默认策略。
- 死磕模式下带权限键的工具自动放行（由死磕的 judge 流程兜底）。

### 5.4 引用 `[N]` 与来源溯源

- 联网搜索的结果由系统分配全局编号 `[N]`，**模型只能引用真实存在的来源**；编造引用会在落库前被机械校验 + LLM 判定清除。
- 点击正文里的引用角标或消息下方的来源胶囊 → 弹出来源预览（标题/域名/URL/发布日期）。
- 把对话**存为笔记**或**导出 PDF** 时，会自动重建「参考来源」章节并重排编号，保证正文与来源一一对应、无断号。

### 5.5 富内容渲染

- **公式**：`$...$` / `$$...$$` 即时渲染（KaTeX）；导出 Word 时逐个转为 OMML 原生公式对象（双击可编辑）。
- **Mermaid**：代码块自动渲染为矢量图（流程图/时序图/甘特图等），hover 可改源码、放大、导出。
- **ECharts**：交互图表（悬停数值、缩放），数据须来自真实检索，否则质检驳回。
- **媒体**：图片点击 lightbox 放大/下载；音视频行内播放；YouTube/B 站走官方 embed（iframe 白名单校验）。

### 5.6 上下文与成本

- 顶部 **token 徽章**显示本轮上下文估算（CJK 感知），压缩前后会有对比提示。
- 上下文接近阈值时系统自动压缩（保护开头与最近内容）；长会话可新建会话以降低成本。
- 「深度思考」开关控制是否启用推理模型/推理预算（按端点与助手配置生效）。

---

## 6. 助手

**助手 = 一个可配置的 Agent 人格与模型设置**。每个助手拥有独立的会话列表。

创建/编辑（侧边栏「新建助手」或助手菜单）：

| 配置项 | 说明 |
|---|---|
| 名称 / 头像 | 展示用 |
| 系统提示词 | 人格、口吻、领域约束、工作流程 |
| 模型别名 | 主对话模型（逻辑别名；具体端点由 `config_model.toml` 决定） |
| 子任务模型 | 协调器/审计/摘要等子任务可单独指定（留空继承主模型） |
| 采样参数 | temperature / top_p 等（按端点默认，可覆盖） |
| 思考预算 | 推理模型 thinking budget（按端点能力） |

约定与建议：

- 同一助手下的**所有** LLM 行为（子代理、审计、协调器、压缩、死磕 judge 等）默认继承该助手模型；仅显式专门配置时例外。
- 多助手适合按场景拆分：写作 / 调研 / 代码 / 语音 / 死磕专用。
- 每个用户注册时会自动获得一个专属「语音助理」助手（用于语音对话归档，见第 7 章）。

---

## 7. 语音对话

**入口**：侧边栏底部「语音助理」（或路由 `/app/frontend/voice`）。语音轮次全部落到专属语音助理的会话里，可在侧边栏回看、继续用文字追问。

### 7.1 特性

- **真双工**：它说话的同时持续拾音；随时插话（barge-in）即时暂停播报，识别判为「真打断」后按语义断点续播（不会从半句中间重播）。
- **语义判端（EoT）**：完整句子快速冲刷，无标点句由 LLM 判语义完整性，自然停顿不会被截断。
- **噪音/离题门控**：咳嗽、环境人声、超短语气词不会触发回答；近场/远场声学门控避免电视/旁人对话打断。
- **情绪与应声**：会接话、应声（嗯/哦）、有情绪状态；回答开头可写 `（温柔）（兴奋）` 等风格标签控制语气。
- **热词**：「系统设置 → 热词配置」登记人名/行话，识别中途与最终结果都生效（pypinyin 同音纠音）。
- **语音 × 记忆 × 笔记**：每轮结束异步检索长期记忆（下轮生效）；强命中会自然插话提起；随口说的事可自动转笔记。
- **工具执行与播报**：可以在语音里调搜索/代码/笔记/记忆等工具，结果读出来；后台任务完成会主动开口播报。

### 7.2 前置条件与调优

- 需要在 `config_model.toml` 配置 `[endpoints.asr]` 与 `[endpoints.tts]`（默认调优组合：DashScope FunASR + MiMo TTS）。
- 浏览器需允许麦克风权限；首次使用建议在安静环境校准。
- 行为参数（判端静音时长、打断冷却、填充词等）在 `config.toml` 的 `[asr]` / `[voice]` 中，可按网络与口音微调。

---

## 8. 死磕模式

**死磕 = 交给它一个目标，它自己盘问澄清、拆计划、逐步执行并自我验证，直到判定完成**。适合长报告、系列创作、多步调研等「一次说不清、需要长线推进」的任务。

### 8.1 开启与盘问

1. 输入区「+ → 死磕模式」开启（可先描述目标再发送）。
2. **盘问阶段**（最多 3 轮、每轮 3 题）：系统递进式追问关键决策点，每题带推荐选项；单选或整轮提交均可。盘问完成后自动合成目标并开始执行。
3. 顶部**状态条**实时显示：盘问进度 → 第 N 轮 · 计划完成 x/y → 已暂停 / 需人工介入 / 目标已完成。

### 8.2 执行与干预

| 操作 | 说明 |
|---|---|
| 暂停 | 状态条暂停；恢复后累计墙钟不重置 |
| 继续 | 暂停后发送任意短指令（如「继续」）即恢复；普通讨论消息会暂停死磕 |
| 追加验收标准 | 对话中直接补充要求（≤500 字/条，总数 ≤20），会注入后续判定 |
| 停止 | 停止按钮终止本轮；已完成步骤与产物保留 |
| 人工介入（human_gate） | 连续停滞或受阻时系统给出结构化报告与建议动作，等你指示后继续 |

### 8.3 交付与验证

- **验证器只认工作区里的真实文件**：声称完成但拿不出新文件会被拦截（证据门）；已定稿步骤的产物消失会被判为完整性违规并重开。
- 完成时给出「任务完成汇总表」，交付物以下载卡片形式列出。
- 全过程落库：断网、关窗、隔夜后可以续跑（后台任务同样）。

---

## 9. 后台任务与定时任务

- **后台任务**：让 agent 执行 ≤5 小时的长线任务，关掉页面也会继续；完成后写回会话、默认笔记本生成笔记，并在语音会话里主动播报。侧边栏「后台任务」面板可查看进度/结果。
- **定时任务**：直接在对话里说「每天早上 9 点总结昨天的会话」之类，agent 调用 `schedule` 工具创建（自然语言 → cron）；支持列出/取消/立即触发。任务输出可用 `[SILENT]` 标记抑制投递。
- **导出任务**：批量导出/PDF 生成在后台队列执行，进度对话框可查看。

---

## 10. 笔记与知识库

### 10.1 笔记本与笔记

- 侧边栏「笔记」面板：新建笔记本/笔记、重命名、移动、删除；默认笔记本不可删除。
- **工作台编辑器**（点开笔记）：标题、正文富文本、标题级别、插入表格、目录（TOC）、插入图片/音频/视频、Mermaid 编辑与放大、公式、代码高亮、字数统计（行/词/段落）、快捷键（Tab/Shift+Tab 缩进、退格等）。
- 支持从本地导入 `.md`，也支持把上传的文档解析后沉淀为笔记。

### 10.2 对话与笔记互哺

- **引用笔记进对话**：输入区「引用笔记」选择笔记，正文以内联引用进入上下文。
- **对话存为笔记**：消息操作 →「保存到笔记」（自动重建参考来源章节）。
- **解析结果存笔记**：上传的 PDF/Office/音频解析结果可一键存为笔记继续追问。

### 10.3 导出

- 单条笔记：Markdown / PDF；批量：多选后导出（PDF/MD/ZIP）。
- 会话：单条消息/整个会话导出 PDF、批量导出 zip；导出的 PDF 含矢量公式、图表与「参考来源」附录。
- 导出产物落 `backend/output_files/`（Docker 部署在 `output_data` 卷）。

---

## 11. 记忆系统

Weave Thinker 的记忆分三层，自动协同，无需手动管理：

| 层 | 内容 | 用户可见处 |
|---|---|---|
| 摘要记忆（v1） | 每用户每日的对话摘要与 dream，注入系统提示词 | 记忆管理面板 |
| 文件记忆 | agent 的长期笔记（AGENT.md）与用户画像（USER.md），跨会话可读写 | 记忆管理面板 / 工作区 |
| v2 概念记忆 | 概念提取、复发晋升、情节合并、梦境整理、成本治理 | 记忆管理面板（概念/梦境/澄清） |

**记忆管理面板**（系统设置 → 记忆管理）：

- 浏览概念及其权重/来源/状态；查看 dream 与澄清记录。
- **修正**：对记忆提出纠正（如「我不再使用 X」），高置信度自动应用。
- **遗忘**：删除单条记忆；**一键擦除**清空个人记忆。
- 成本治理：v2 管线在预算内运行，超预算自动降级（日志有记录）。

提示：记忆写入是异步的（通常下一轮生效）；想要它「记住」，直接说清楚即可（「请记住：……」）。

---

## 12. 技能与 MCP

### 12.1 系统技能

内置 10 项系统技能（`backend/skills/`，每项含 SKILL.md 操作手册）：web_search、browser、code_execution、file_parsing、media_playback、echarts_chart、docx_manipulation、pptx_manipulation、xlsx_manipulation、rempilot-mcp。agent 会按需加载手册并按其规范操作。

### 12.2 用户技能

「系统设置 → 技能管理」可上传/创建自己的技能：

- 上传 zip 或文件夹（含 SKILL.md 与可选脚本；可执行文件会做安全扫描）。
- agent 可通过 `skill_view`（读手册）、`skill_run_script`（跑捆绑脚本）、`skill_manage`（创建/修改）使用。
- 适合沉淀团队 SOP：把流程写成 SKILL.md，agent 每次按你的规范执行。

### 12.3 MCP（模型上下文协议）

在 `config.toml` 配置外部 MCP 服务（stdio 或 HTTP），启动后动态注册为工具（上不封顶）：

```toml
# backend/config.toml
[mcp.servers.my-tools]
transport = "stdio"
command = "npx"
args = ["-y", "@your/mcp-server"]
```

工具数量多时启用渐进式发现（`search_tools` 元工具按需加载），避免 schema 占满上下文。

---

## 13. 皮肤与外观

- **三套内置皮肤**：青野平面（默认）/ 墨韵纸间 / 黑白构成，各支持明暗双模式；纯 CSS 设计令牌切换，无运行时开销。
- **切换**：头像菜单或「系统设置 → 皮肤」；偏好保存在本地并同步到账号。
- **自定义皮肤**：上传 CSS（≤300KB，安全校验）即生效；开发规范见 [docs/SKINS.md](SKINS.md)（约 40 枚设计令牌）。
- 移动端跟随同一套皮肤与明暗设置。

---

## 14. 数据、备份与升级

### 14.1 数据在哪

| 数据 | 手动部署位置 | Docker 部署位置（卷） |
|---|---|---|
| 全部结构化数据（用户/会话/笔记/记忆/任务） | PostgreSQL `weavethinker` 库 | `pgdata` |
| 用户上传与工作区文件 | `user_workspaces/` | `workspace_data` |
| 文件记忆 | `backend/agent_memories/` | `memories_data` |
| 语音音频缓存 | `backend/audio_files/` | `audio_data` |
| 导出产物 | `backend/output_files/` | `output_data` |
| 自定义皮肤 | `backend/skins_custom/` | `skins_data` |
| 配置（含密钥） | `backend/config*.toml` | 宿主机挂载文件 |

### 14.2 备份

```bash
# ① 数据库（最重要）
#    Docker：
docker compose exec -T db pg_dump -U weavethinker -d weavethinker -Fc > backup-$(date +%F).dump
#    手动：
pg_dump -U weavethinker -Fc weavethinker > backup-$(date +%F).dump

# ② 文件类数据
#    Docker：直接备份卷（示例：工作区）
docker run --rm -v weave-thinker_workspace_data:/data -v "$PWD":/backup \
  alpine tar czf /backup/workspace_data-$(date +%F).tgz -C /data .
#    手动：
tar czf workspace-$(date +%F).tgz user_workspaces backend/agent_memories
```

建议：数据库每日备份 + 保留最近 7 份；`config*.toml` 含密钥，纳入密钥管理（不要进普通备份/代码库）。

### 14.3 恢复

```bash
# 数据库（新库）
docker compose exec -T db pg_restore -U weavethinker -d weavethinker --clean --if-exists < backup-2026-09-14.dump
# 或手动： pg_restore -U weavethinker -d weavethinker --clean --if-exists backup-*.dump
```

### 14.4 升级

```bash
# Docker：拉取新代码后重建（数据库迁移在启动时自动执行，幂等）
git pull
./scripts/docker_start.sh

# 手动：更新依赖 → 重建前端 → 重启
source .venv/bin/activate && pip install -r backend/requirements.txt
./scripts/project_build.sh
./scripts/restart.sh
```

升级前先做 14.2 的备份。启动日志出现迁移版本号即表示迁移已执行。

---

## 15. 运维与故障排查

### 15.1 日志与状态

```bash
# Docker
docker compose ps                  # 容器与健康状态
docker compose logs -f app         # 应用日志（Ctrl+C 退出）
docker compose logs --tail 200 db  # 数据库日志

# 手动
./scripts/status.sh
tail -f chatllm.log
```

### 15.2 常见问题（FAQ）

**Q：登录页能打开，但对话报「缺少模型配置」？**
A：`config_model.toml` 里没有可用的 LLM 端点。至少配置一个 `[endpoints.*]`（`kind = "llm"`）并在 `[routing]` 指向它，重启生效。

**Q：Docker 启动报「config.toml 是一个目录」？**
A：compose 挂载源文件不存在时 Docker 会把它建成目录。先删除再复制：
`rm -rf backend/config.toml && cp backend/config.toml.example backend/config.toml`（`config_model.toml` 同理），然后重启。

**Q：Docker 起不来/一直 unhealthy？**
A：`docker compose logs app` 看首条错误；常见原因：`[database] host` 未改成 `db`、`.env` 与 `config.toml` 的库账号不一致、端口被占用（改 `.env` 的 `APP_PORT`）。

**Q：浏览器工具报 Playwright/Chromium 缺失？**
A：Docker 镜像默认不带 Chromium，`.env` 设 `WITH_BROWSER=1` 重建；手动部署执行 `.venv/bin/python -m playwright install chromium`。

**Q：语音没反应/识别不到？**
A：确认已配置 `[endpoints.asr]`/`[endpoints.tts]`、浏览器麦克风权限、页面为 HTTPS 或 localhost（浏览器对非安全上下文禁用麦克风）。热词可在设置中补。

**Q：`pgvector` 必须装吗？**
A：不必须。缺失时 memory v2 的向量表跳过创建、记忆自动降级，其余功能不受影响；Docker 部署使用 `pgvector/pgvector:pg16` 镜像并自动 `CREATE EXTENSION`。

**Q：公网访问超时，但本机 curl 正常？**
A：云安全组/NAT 端口映射未放行，或供应商边缘对端口做被动应答。按 [ubuntu.md 端口排查阶梯](../requirements/ubuntu.md) 逐级定位。

**Q：多人共用一台服务器可以吗？**
A：可以（多用户彼此隔离）。注意：同一数据库建议单实例写（调度器/worker 在多实例下会产生重复消费竞态）。

### 15.3 端口与进程

- 默认端口 8158（`config.toml [server].port`）。
- 手动部署启停一律用 `./scripts/start.sh|stop.sh|restart.sh`（PID 文件管理，安全停止，不会误杀其他进程）。

### 15.4 TLS 与反向代理

- **手动部署**：把 `key.pem`/`cert.pem` 放在 `backend/` 下，`start.sh` 自动启用 HTTPS。
- **Docker**：默认 HTTP。推荐 Nginx 终结 TLS 后反代 `127.0.0.1:8158`；配置注意 SSE 与 WebSocket：
  `proxy_buffering off;`、`proxy_read_timeout` 调大、透传 `Upgrade`/`Connection` 头（示例见 [ubuntu.md 第 7 节](../requirements/ubuntu.md)）。
- 也可以把证书放到 `backend/certs/key.pem`、`backend/certs/cert.pem` 并挂载进容器，入口自动检测启用 HTTPS。

### 15.5 性能与成本

- 上下文越长成本越高：长会话及时开新会话；顶部 token 徽章可观察用量。
- 子任务模型可配更便宜的端点（助手设置），审计/协调等高频调用随之降本。
- 记忆 v2 有成本治理与降级阶梯，超预算自动降级并记录日志。

---

## 16. 安全说明

- **服务边界**：本项目面向个人/小团队自托管。管理接口（`/api/admin/*`）当前没有独立角色校验（任何登录用户可调用），**不要把服务直接暴露到不可信网络**；公网部署请置于 VPN、内网或加一层带认证的反向代理之后。
- **密钥**：`config.toml` 与 `config_model.toml` 含数据库密码、JWT 密钥、模型 API key；不要提交到代码库、不要打进镜像（本项目 Docker 配置已排除并运行时挂载）。
- **权限审批**：终端执行、删除笔记等敏感工具默认走审批；可在「权限管理」收紧或放宽（放宽前请确认使用场景）。
- **提示词注入防护**：网页/文件内容会被当作数据处理；记忆写入有注入扫描与不可见字符防护。
- **JWT**：`[security] jwt_secret_key` 必须为随机长串；泄露后立即更换并重启（所有人需重新登录）。

---

## 17. 附录

### 17.1 生命周期脚本速查

| 脚本 | 用途 |
|---|---|
| `scripts/docker_start.sh` | Docker：构建 + 启动 + 等待就绪 |
| `scripts/docker_stop.sh` | Docker：停止（`--volumes` 连数据删除） |
| `scripts/docker_build.sh` | Docker：只构建镜像 |
| `scripts/project_build.sh` | 前端生产构建 → `backend/static/` |
| `scripts/start.sh` / `stop.sh` / `restart.sh` / `status.sh` | 手动部署的进程生命周期（PID 文件安全） |
| `scripts/dev_frontend.sh` | 前端开发服务器（8159，供联调/E2E） |
| `scripts/apk_generate.sh` | 生成 Android WebView 壳 APK |

### 17.2 关键配置索引

| 配置 | 文件 | 说明 |
|---|---|---|
| `[server]` `[security]` `[database]` | config.toml | 端口、JWT、数据库 |
| `[workspace]` | config.toml | 用户工作区根目录、是否复用项目 venv |
| `[browser]` | config.toml | 浏览器工具超时/内容上限 |
| `[asr]` `[voice]` | config.toml | 语音行为（判端、打断、填充词、热词） |
| `[deathmatch]` `[deathmatch.judge]` | config.toml | 死磕自治、轮次/墙钟、judge 模型 |
| `[memory.*]` | config.toml | v2 记忆各阶段阈值与预算 |
| `[agent.*]` | config.toml | 工具循环、审计、压缩、canary、后台任务 |
| `[endpoints.*]` `[routing]` `[defaults]` | config_model.toml | 模型端点池 / 用途路由 / 采样默认 |
| `[secrets]` `[mcp.servers.*]` | config.toml | 第三方检索 key / 外部 MCP 服务 |

逐键完整注释见 `backend/app/core/config.py` 与 `backend/config_model.toml.example`。

### 17.3 文档导航

| 文档 | 内容 |
|---|---|
| [README](../README.md) | 项目介绍与特性总览 |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | 机制级架构（Agent 循环/记忆/语音/死磕实现） |
| [docs/API.md](API.md) | 后端接口参考（REST/SSE/WS） |
| [docs/SKINS.md](SKINS.md) | 皮肤令牌契约（自定义皮肤开发必读） |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | 贡献指南（DCO/CCLA） |
| [requirements/](../requirements/DEPLOYMENT.md) | 分平台部署与依赖 |

---

*手册与代码不一致时以代码为准；发现文档问题欢迎提 Issue / PR。*
