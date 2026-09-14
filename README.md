<!-- Copyright (c) 2026 Weave Thinker Contributors -->

<!-- SPDX-License-Identifier: Apache-2.0 -->

<div align="center">

<img src="frontend/src/logo.png" alt="Weave Thinker Logo" width="120" />

# Weave Thinker

自托管个人 AI Agent 平台：给一个目标，它自己拆步骤、调工具、逐步验证并交付结果。

<p align="center"><strong>记得住你 · 做得完事 · 句句有据</strong><br/>FastAPI · PostgreSQL · Vue 3 · 全双工语音 · 死磕模式 · [N] 引用台账 · 三层仿生记忆</p>

<p align="center"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" /> <img alt="Version" src="https://img.shields.io/badge/version-v0.0.1-4c9f70.svg" /> <a href="https://github.com/logandoo/weave-thinker/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/logandoo/weave-thinker/actions/workflows/ci.yml/badge.svg" /></a></p>

</div>

> 初版项目，仍在持续改进；商业版将提供团队协作等能力，欢迎企业联系（logandoo@126.com）。

## 快速开始

Docker 一键部署（需 Docker 与 Compose 插件）：

```bash
git clone <your-fork-url> weave-thinker && cd weave-thinker
cp backend/config.toml.example       backend/config.toml        # [database] 四项与 .env 一致（host 填 "db"）；填 jwt_secret_key
cp backend/config_model.toml.example backend/config_model.toml  # 至少填一个 LLM 端点
cp docker/.env.example               .env                       # 改 POSTGRES_PASSWORD
./scripts/docker_start.sh
```

打开 `http://<host>:8158/app/frontend/` 注册账号即可使用。数据落在命名卷，`docker compose down` 不丢；浏览器工具在 `.env` 里设 `WITH_BROWSER=1` 后重建启用；HTTPS 由反向代理终结或挂载证书启用。备份、升级与故障排查见[使用手册](docs/USER_MANUAL.md)。

手动部署（Python ≥ 3.10，推荐 3.12/3.13 · Node.js ≥ 18，推荐 20/22 · PostgreSQL ≥ 14）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
# 配置同 Docker 第 2、3 步，[database] 填真实地址
./scripts/project_build.sh      # 前端产物 → backend/static/
./scripts/start.sh              # 配套 stop.sh / restart.sh / status.sh
```

分平台逐步命令（系统依赖、数据库初始化、TLS 证书、systemd、端口排查）：

| 平台 | 文档 |
| --- | --- |
| macOS | [requirements/macos.md](requirements/macos.md) |
| Ubuntu / Debian | [requirements/ubuntu.md](requirements/ubuntu.md) |
| Windows / WSL2 | [requirements/windows.md](requirements/windows.md) |
| 完整依赖清单（系统 / pip / npm + license 摘要） | [requirements/dependencies.md](requirements/dependencies.md) |
| 部署文档总览 | [requirements/DEPLOYMENT.md](requirements/DEPLOYMENT.md) |

<details>
<summary>手动部署逐步命令（含数据库初始化、证书与启动细节）</summary>

```bash
# ── 0. 获取代码 ───────────────────────────────────────────────────
# 有 git 仓库：
git clone <your-fork-url> weave-thinker && cd weave-thinker
# 无 git 仓库 / 出网受限（源码包分发）：仓库根执行
export COPYFILE_DISABLE=1        # macOS：禁 BSD tar 写 AppleDouble 元数据（._* 文件）
tar -czf wt-src.tgz .
# → scp/内网传输到目标机 → mkdir weave-thinker && tar xzf wt-src.tgz -C weave-thinker && cd weave-thinker
# （tar 内结构即仓库根结构：backend/ frontend/ scripts/ 等）
# 两个坑：目标机直连 github.com 时通时断时走打包流程；macOS 打包未设
# COPYFILE_DISABLE=1 会带入 ._* 文件，解压端用 find . -name "._*" -delete 清理。

# ── 1. 准备 PostgreSQL ────────────────────────────────────────────
#    （复用已有远端 PostgreSQL 时整节跳过：第 3 步 [database] 直接填远端
#      host/port/username/password/name 即可。）
#    Ubuntu：
sudo apt install -y postgresql && sudo service postgresql start
sudo -u postgres psql -c "CREATE USER weavethinker WITH PASSWORD 'CHANGE_ME_strong_password';"
sudo -u postgres psql -c "CREATE DATABASE weavethinker OWNER weavethinker ENCODING 'UTF8' TEMPLATE template0;"
#    macOS：brew install postgresql@16 && brew services start postgresql@16
#           psql -U postgres -h 127.0.0.1   # 然后执行上面两条 CREATE
#    完整差异与故障排查见 requirements/{ubuntu,macos}.md 第 2 节。
# 可选（记忆向量化检索，不装也能跑，仅 v2 记忆 embedding 功能降级）：
#   CREATE EXTENSION vector;
#   Ubuntu 22.04 默认源无此扩展，需 pgdg apt 源或 postgresql-16-pgvector；
#   macOS: brew install pgvector。

# ── 2. 后端环境 ───────────────────────────────────────────────────
python3 -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate
python -m pip install -U pip
pip install -r backend/requirements.txt

# ── 3. 配置 ───────────────────────────────────────────────────────
cp backend/config.toml.example            backend/config.toml
cp backend/config_model.toml.example      backend/config_model.toml
# backend/config.toml：
#   [security] jwt_secret_key —— 改成随机长串（openssl rand -hex 32）
#   [database] host/username/password/name —— 指向你的库
# backend/config_model.toml：
#   至少配置一个 LLM 端点（[endpoints.*] 按类型分区：[endpoints.main] 主对话、
#   [endpoints.asr]/[endpoints.tts] 语音、[endpoints.embedding]/[endpoints.rerank]
#   记忆 v2，OpenAI 兼容格式，可接云端 API 或本地 vLLM/Ollama；[routing] 把
#   用途路由到端点别名）。未填的 <YOUR-*> 占位不阻塞启动，仅对应功能报缺配。

# ── 4. TLS 证书（生产构建与 Android 壳建议）──────────────────────
cd backend
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=localhost" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost"
# 服务器部署把 CN/subjectAltName 改成你的域名/内网 IP
cd ..

# ── 5. 前端构建（产物 → backend/static/）─────────────────────────
./scripts/project_build.sh    # 内部 = cd frontend && npm install && npm run build

# ── 6. 启动 / 停止 ───────────────────────────────────────────────
./scripts/start.sh            # nohup + PID 文件 + 自动检测 backend/ 下的证书
./scripts/status.sh
./scripts/stop.sh
./scripts/restart.sh
```

</details>

首次启动后可先验证数据面：`curl -k https://<host>:8158/docs` 返回 200。公网部署若握手成功但无响应，按 [ubuntu.md 的端口排查阶梯](requirements/ubuntu.md)逐级定位（安全组 → NAT/端口映射层 → 供应商）。

## 使用

1. 注册登录后创建助手，选择模型别名。端点地址与密钥只在服务端 `config_model.toml` 配置，界面不接触。
2. 在输入框描述目标，或把图片、录音、PDF、Office 文档拖进输入框。生成过程中可停止、可插话；工具调用与引用角标随时展开、点开溯源。
3. 需要长线任务时打开输入区的「死磕模式」；需要说话时用侧边栏「语音助理」；产出可以存成笔记或导出 PDF。

典型场景：

- **联网调研**：回答里的 `[N]` 全部对应真实检索来源，点击角标查看标题、域名与发布日期。
- **长报告与系列创作**：死磕模式先盘问澄清，再进入计划、执行、验证循环，交付物以下载卡片列出。
- **实时语音**：像打电话一样对话，可以随时插话打断，被打断的播报从断点续播。

## 功能特性

**Agent 循环**：协调器先判定直接回答还是调用工具；工具循环默认 ≤50 轮，独立工具并行执行，每一步都留痕可展开；发送前审计通过后才流式交付。删除笔记、执行终端等敏感操作先请求权限，批准后动手。

**句句有据**：`[N]` 引用编号由系统分配，模型只能引用真实存在的来源，编造的引用会在落库前被机械校验与 LLM 判定清除。存笔记或导出 PDF 时自动重建「参考来源」章节。

**防幻觉体系**：发送前四态审计（accept / reject / unverifiable / needs_evidence）+ 拒绝预算与 salvage 重生成 + 遵循词 canary（长上下文防走神）+ 数值溯源闸门 NPG（关键数值须逐字溯源，默认记录、可配置强制拦截）。

**三层记忆**：每日摘要与 dream、文件记忆（AGENT.md / USER.md）、v2 概念/情节/潜意识管线并存。五段混合召回（BM25 + embedding → 关系扩展 → rerank → LLM 打分 → RRF 融合），支持复现晋升、休眠淡忘、梦境整理与成本治理降级，pgvector 可选。记忆面板可见来源与权重，支持修正、遗忘与一键擦除。

**死磕模式**：先盘问再动手（最多 3 轮递进追问），随后进入 PEVR 目标循环；验证器要求工作区里有真实文件产出，幻影完成会被拦下；停滞按重规划、部分交付、人工介入三级升级；状态全部落库，断网或隔夜可以续跑。

**全双工语音**：单条 WebSocket 上做流式识别与流式合成，播报中持续拾音，插话即时暂停并从断点续播；语义判端、近场声学门控、情绪应声、热词纠音；语音里可以调用搜索、代码、笔记、记忆等工具，随口提的事自动转笔记。当前针对 DashScope FunASR 识别 + MiMo 合成组合调优，其他 ASR/TTS 供应商未经完整测试。

**原生渲染**：LaTeX 公式即时渲染；代码沙箱生成 Word 文档时，公式转为 OMML 原生对象（双击可编辑）；Mermaid 矢量图、ECharts 交互图表（数据须来自真实检索）；图片 lightbox、音视频行内播放。

**文件解析**：图片走 VLM 视觉解读，音频经 ASR 转写，PDF 与 Office 文档按类型解析；解析结果可存为笔记继续追问。

**可信计算**：`calculate` 是 AST 白名单安全计算器（无 eval），复杂计算走代码沙箱；模型口算的数字不作依据。

**皮肤与技能**：3 套内置皮肤 × 明暗双模式（设计令牌体系，支持上传自定义皮肤，规范见 [docs/SKINS.md](docs/SKINS.md)）；10 项系统技能 + 用户技能，SKILL.md 即操作手册，捆绑脚本执行前做安全扫描。

**字体自托管**：Inter 与 Noto Sans SC（SIL OFL 1.1）全量存放在 `frontend/public/fonts/`，运行时零第三方 CDN 请求，离线与内网可用（许可与再分发义务见 [docs/license-compliance.md](docs/license-compliance.md) 第五节）。

## 内置工具与技能

后端 `app/tools/` 经 `registry.register()` 静态注册 38 个工具函数；外部 MCP 服务可在运行时动态注册为工具，`search_tools` 提供渐进式发现；`backend/skills/` 另有 10 项系统技能。

<details>
<summary>完整工具清单（38 个）</summary>

| 类别 | 工具函数 | 说明 |
| --- | --- | --- |
| 联网检索 | `web_search` | 多引擎接力搜索（主引擎 + fallback 链可配置），农场域名黑名单，来源逐条落库 |
| 网页深读 | `browser` | 打开网页并抽取正文 |
| 浏览器操作 | `browser_navigate` / `browser_snapshot` / `browser_click` / `browser_type` / `browser_scroll` / `browser_press` / `browser_back` / `browser_extract` / `browser_execute_js` / `browser_screenshot` | 交互式会话全套操作（10 件套） |
| 代码执行 | `execute_code` | Python 代码沙箱（自动修复循环、中文字体内置、超时长任务自检引导） |
| 计算 | `calculate` | AST 白名单安全计算 |
| 视觉 | `vision_interpret` | VLM 图片解读（purpose `vlm`，未配置返回友好错误） |
| 终端 | `terminal` | 受控 shell 命令执行（敏感操作走审批） |
| 文档查询 | `context7_resolve_library_id` / `context7_query_docs` | Context7 库 ID 解析与官方文档查询 |
| 笔记 | `notes` | 笔记本与笔记的列表、读取、创建、修改、删除 |
| 记忆 | `memory` | 跨会话长期记忆（agent/user 双目标 + system 只读系统文档） |
| 任务编排 | `delegate_task` / `background_task` / `schedule` / `session_search` / `mixture_of_agents` | 子代理并行委派、后台长线任务、定时任务、跨会话搜索、混合专家 |
| 文件工作区 | `workspace_read` / `workspace_glob` / `grep` / `diff` / `word_count` / `provide_file` | 读取、查找、对比、字数统计与文件交付 |
| 导出 | `pdf_export` | 笔记、对话记录、工作区文件导出 PDF |
| 技能 | `skill_view` / `skill_manage` / `skill_run_script` | 加载 SKILL.md、创建用户技能、执行捆绑脚本 |
| 语音 | `asr_transcribe` / `tts_synthesize` | 系统 ASR / TTS 端点开放给 agent |
| 扩展 | MCP（动态） + `search_tools` | 运行时注册外部 MCP 工具服务；渐进式发现元工具 |

</details>

## 配置

| 文件 | 内容 |
| --- | --- |
| `backend/config.toml` | 基础设施与行为：`[server]` `[security]` `[database]` `[workspace]` `[browser]` `[asr]` `[voice]` `[deathmatch]` `[memory.*]` `[agent.*]` `[mcp]` `[secrets]` |
| `backend/config_model.toml` | 模型端点池 `[endpoints.*]`（LLM / VLM / ASR / TTS / Embedding / Rerank）、`[routing]` 用途路由、`[defaults]` 采样默认 |

模板见 `backend/config.toml.example` 与 `backend/config_model.toml.example`，逐键说明在 `backend/app/core/config.py`。模型端点未填不阻塞启动，只在对应功能被调用时报缺配；`[database]` 与 `[security] jwt_secret_key` 必须填对，否则启动失败或使用公开的签名密钥。

## 架构速览

```
frontend/  Vue 3 + TS + Vite + Pinia（SSE 流式渲染 · 全双工语音 UI · 3 皮肤令牌体系）
                     │  /api/*（JWT）
backend/    FastAPI + async SQLAlchemy 2.0
  ├─ app/api/        20+ 路由模块（auth/chat/conversations/notes/assistants/skills/voice/asr/…）
  ├─ app/services/   Agent 编排 · AgentLoop（工具循环）· 记忆三层 · 死磕 · 调度 · 导出
  ├─ app/tools/      工具体系（38 个工具函数 + MCP 动态扩展）
  ├─ app/db/         模型 + 启动幂等迁移（无 Alembic，STARTUP_MIGRATIONS）
  └─ skills/         系统技能（SKILL.md 目录，Agent 可加载执行）
webview-app/ Android WebView 壳（可选，JS 桥 window.WeaverNoteApp）
scripts/     构建与启停生命周期（PID 文件安全，stop 只杀记录的 PID）
```

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `backend/` | FastAPI 服务（`main.py` 入口；`app/` 代码；`skills/` 系统技能；`agent_memories/changelog.md` 系统文档） |
| `frontend/` | Vue 3 前端（`src/`；`e2e/` Playwright） |
| `scripts/` | `docker_build.sh` / `docker_start.sh` / `docker_stop.sh` / `project_build.sh` / `start.sh` / `stop.sh` / `restart.sh` / `status.sh` / `apk_generate.sh` |
| `docker/` | 容器入口、配置生成脚本与 `.env` 模板 |
| `webview-app/` | Android WebView 壳源码（Gradle） |
| `requirements/` | 分平台部署与依赖文档 |
| `docs/` | 使用手册（USER_MANUAL.md）、功能与机制总览（ARCHITECTURE.md）、皮肤契约（SKINS.md） |

## 文档

- [docs/USER_MANUAL.md](docs/USER_MANUAL.md)：完整使用手册（Docker 快速部署 · 手动部署 · 全部功能操作 · 备份升级 · 故障排查 FAQ）
- [docs/API.md](docs/API.md)：后端接口详版（字段级表格 + 示例 + SSE/WS 协议）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：功能与机制总览
- [docs/SKINS.md](docs/SKINS.md)：皮肤系统令牌契约（前端与自定义皮肤开发必读）
- [docs/license-compliance.md](docs/license-compliance.md)：依赖库 license 合规审核报告
- [CONTRIBUTING.md](CONTRIBUTING.md)：贡献指南（DCO / CCLA 流程）

## 测试

```bash
# 前端 E2E（Playwright，目标 = 8158 生产构建；需后端带证书运行）
cd frontend
npx playwright install chromium
npx playwright test e2e/chat.spec.ts --config playwright.prod8158.config.ts
```

服务端「网页深读 / 浏览器 10 件套」依赖 Python 侧 Playwright Chromium：`.venv/bin/python -m playwright install chromium`。两侧 playwright 锁定同一版本（npm `1.60.0` 与 pip `==1.60.0`），浏览器缓存共享，任一侧装一次即可双侧复用。

本开源发行不含后端单元测试集；后端改动的验证要求见 [CONTRIBUTING.md](CONTRIBUTING.md) 的「测试要求」。

## 贡献

贡献代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，其中包含 DCO sign-off 与公司 CCLA 的要求。

**本项目鼓励 Fork，欢迎企业内部自行定制分支。**

> Fork 后建议先改写 `backend/agent_memories/changelog.md`。这是 agent 做自我介绍与功能解答时读取的系统文档（只读，随仓分发），换成自己的产品文案后，对话中的「我是谁 / 我能做什么」即与品牌一致。

## 变更记录

版本增量见 [backend/agent_memories/changelog.md](backend/agent_memories/changelog.md)，以产品视角按版本累积记录。

## 许可证

[Apache License 2.0](LICENSE)

> 免责声明：本项目按「现状」提供，无任何担保。使用者应自行评估依赖许可证（尤其 AGPL/GPL/LGPL 条目）与其分发场景的合规性，并妥善保管配置中的密钥。
