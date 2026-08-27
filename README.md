<!-- Copyright (c) 2026 Weave Thinker Contributors -->

<!-- SPDX-License-Identifier: Apache-2.0 -->

<div align="center">

<img src="frontend/src/logo.png" alt="Weave Thinker Logo" width="120" />

# Weave Thinker

**记得住你 · 做得完事 · 句句有据**<br/>
自托管个人 AI 智能体平台（Agent，不是聊天框）

FastAPI · PostgreSQL · Vue 3 · 全双工语音 · 死磕模式 · [N] 引用台账 · 三层仿生记忆 · 全双工语音对话

<p align="center">
  <img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" />
  <img alt="Version" src="https://img.shields.io/badge/version-v0.0.1-4c9f70.svg" />
</p>

</div>

---

## 简介

Weave Thinker 是一个**自托管的个人 AI Agent Harness**。你交给它一个目标，它还给你一个结果：自己拆步骤、调工具、做一步验一步，直到交付，而不是"问到哪答到哪"的聊天框。

- **33 个内置工具函数**：联网搜索、浏览器深读（10 件套）、代码沙箱、终端、笔记、记忆、文件工作区、
  子代理委派、后台/定时任务、技能系统（SKILL.md）、MCP 动态扩展…（完整清单见「内置工具列表」）
- **原生富内容渲染**：公式 KaTeX、流程图 Mermaid、交互图表 ECharts、图片/视频内嵌播放，流式输出
- **UI 字体自托管**：Inter + Noto Sans SC（SIL OFL 1.1）全量 vendored 于
  `frontend/public/fonts/`，运行时零第三方 CDN 请求，离线/内网可用
  （许可与再分发义务见 `docs/LICENSE-COMPLIANCE.md` 第五节）
- **全双工语音对话**：它说话的同时还在听，可随时插话、打断并断点续播
- **死磕模式**：自主长线执行复杂目标——盘问澄清 → 计划-执行-验证-重规划循环，
  直到裁判判定目标完成
- **防编造引用台账**：回答中的 `[N]` 全部对应真实检索来源，前端可点开溯源
- **三层仿生长期记忆**：DB 摘要记忆 + 文件记忆 + v2 概念/情景/潜意识记忆管线（检索/巩固/做梦/成本治理）
- **3 套皮肤 × 明暗双模式**：CSS 设计令牌体系，支持用户上传自定义皮肤（per-user，格式护栏）

> **本项目目前为初版，依然有很多待改进和待优化项。**
> **商业版将提供团队协作等更多能力，欢迎各企业联系我们（logandoo@126.com）。**

## 功能介绍

> 机制级详解（含文件/行号交叉验证）见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

**1. Agent：不是更聪明的搜索框，是能办事的 Agent** —— 对话里的六步推进循环：

> 你定目标（一句话说清要什么，文件/要求随手丢给它）→ 语义路由（协调器判定该直接回答还是动手干）→ 工具循环（该查资料就查、该跑代码就跑，最多 50 轮迭代调工具：搜索、代码、浏览器、笔记、文件、子代理并行委派；全程留痕可展开；删笔记/跑终端等敏感操作走**权限审批制**，先请示后动手）→ 审计把关（发送前 LLM 四态审计 + `[N]` 引用台账，句句有据、无中生有会被拦下）→ 流式交付（答案边生成边渲染，公式/图表/媒体原生呈现，大任务拆给子代理并行、主代理汇总验收）→ 记住你（偏好写入长期记忆，成果归档落笔记）。

**2. 公式 · 图表 · 媒体：对话与笔记同样原生渲染（核心能力）**

- **公式（LaTeX → KaTeX）**：对话/笔记内即时渲染，流式输出到最后一行仍稳定。**导出 Word 时每个公式转为 OMML 原生公式对象**（微软官方 MML2OMML 转换 + 自研 nary 规整，Word 里双击可继续编辑；转不出会明确告知，绝不静默降级）。导出 PDF 为矢量 SVG。
- **Mermaid**：流程图/时序图/甘特图/类图源码即渲染矢量图；流式只在稳定时绘制（不闪半图）、同号签名缓存不重算、hover 控件改源码即时重绘/放大、导出离线渲染不依赖网络。
- **ECharts**：交互图表是「活」的（悬停数值、刷选缩放）；生成端有专门技能手册（标准 JSON、布局防重叠硬约定），**数据必须来自真实检索否则质检驳回**；离屏实例自动回收。
- **媒体**：搜到的图片直接内嵌、点击 lightbox 放大/下载；音视频行内播放（直链自动下载进工作区嵌入）；YouTube/B 站走官方 embed 且经 iframe 白名单校验。

**3. 检索 · 引用 · 防幻觉：句句有据（核心能力）**

- **联网检索**：多引擎接力（Exa / 博查 / Firecrawl / Tavily / Serper，主引擎 + fallback 链可按配置调整）+ 低质农场域名黑名单；**来源逐条落库 `web_search_results` 可溯**，结果作为 `web_search` 工具呈给 agent。
- **`[N]` 引用台账**：引用编号由系统分配，模型只能引用真实存在的来源（无中生有会被机械校验 + LLM 判定清除）；前端点角标弹出来源预览；**存笔记/导出 PDF 时自动重建「参考来源」章节**。
- **防幻觉体系**：发送前 LLM 四态审计（accept/reject/unverifiable/needs_evidence；拒绝预算 + 有界 salvage + best-of 选优兜底）+ 遵循词 canary（长上下文「防走神」暗号，丢失即自动压缩重答）+ 假前言拦截（「我检索过了」在工具真正执行前暂存不放行）+ 协调器语义路由 + 前置工具门。

**4. 仿生记忆**：三层并存 —— v1 每日摘要/dream + 文件记忆工具（AGENT/USER/func.md）+ v2 概念/潜意识/情节管线（五段混合召回：BM25+embedding → 关系扩展 → rerank → LLM 打分 → RRF 融合；复现晋升、休眠淡忘、夜间梦境整理、成本治理降级，pgvector 可选）。记忆面板可见来源/状态/权重，支持修正、遗忘与一键擦除。

**5. 死磕模式**：不达目的一般来说不罢休。先盘问再动手（最多 3 轮递进追问）→ PEVR 目标循环；验证器要求「拿出文件来」对抗幻影完成；停滞时反思重规划 → 部分交付 → 人工介入三级升级；状态全落库、断网/隔夜可续跑。后台任务（≤5h）关窗继续执行、完成后主动播报。

**6. 全双工语音（核心能力）：像打电话一样说话，它说话的同时还在听，随时插嘴、随时打断**

> *注：双工语音目前仅针对 DashScope FunASR（识别）+ MiMo（合成）组合做过专项调优；其他 ASR/TTS 模型或供应商暂未经完整测试，部分功能可能无法完全生效。*

单条双向 WebSocket（`/api/voice/ws`），默认配置为 DashScope FunASR 流式识别 + MiMo 流式 TTS（供应商经 `[asr]` / `[voice]` 可配置），每用户专属语音助理：

- **真双工 + 打断（barge-in）**：播报中麦克风持续拾音；插话确认声学 pVAD 层即时暂停（不等约 2s 的识别+LLM 判定），再由分类器判「真打断」或「恢复播放」——**断点续播**且重新合成绝不从半句中间开播；开场 1.2s 不可打断窗（TTS 回声/混响最易误识别）+ 2.0s 冷却 + 近场声学门控（浏览器把麦克风输入分为近场/远场，电视/旁人对话永不打断）；「嗯」「哦」类短声经声学+LLM+回声防护三层把关，不会被当成喊停。
- **语义判端（EoT）**：完整句（句末标点）0.6s 静音即冲刷；无标点句由 LLM 子代理判语义完整性（口语应答比 1.6s 硬阈值更快，1.5s 超时兜底绝不判死）；被判碎的连续话语在应答端重新拼接（≤10s 窗口），自然停顿不打断。
- **噪音/离题门控**：≤3 字短句走 agentic 噪音判定（咳嗽、笑声、环境人声不回答，真实长问永不被吞）；最近对话轮次作为 ASR 上下文传入，识别偏主题、抑制背景语音。
- **会接话、会应声、有情绪**：用户句中停顿先应和一声（嗯/哦，单轮 ≤2 次）；生成回答期间先开一句填充词（「我来看看啊…」）避免干等，首段音频就绪无缝切换；插话子代理可对每句话简短评论（单轮 ≤3 次）；情绪状态（calm/interested/excited/upset/broken）影响插话频率与口吻，经 WS `emotion` 事件下发（将来 Live2D 表情驱动的信号源）；回答开头可写（温柔）（兴奋）（严肃）等风格标签控制 TTS 演绎。
- **语音 × 长期记忆 × 笔记**：每轮结束后异步起 v2 记忆召回（fire-and-forget，下轮生效）；强命中由仲裁 LLM 决定是否像真人一样顺口插一句（「对了，你之前说过…」，每会话有预算 + 20s 最小间隔）；语音里随口提的事自动转笔记。
- **热词表 + 工具执行**：行话/人名可预先登记（pypinyin 同音字纠音，识别中途与最终结果处处生效）；可在语音里调搜索/代码/笔记/记忆等工具、结果读出来，后台任务完成主动开口播报；语音轮次强制关 LLM 思考（延迟等不起推理时间），全部语音子代理 6s 硬超时 + 安全回退、429 限流两轮重试。

**7. 皮肤 · 技能 · 工作台**：3 套内置皮肤 × 明暗（设计令牌体系，纯 CSS 切换，自定义皮肤上传即生效，规范 [docs/SKINS.md](docs/SKINS.md)）；9 项系统技能 + 用户技能（SOP/文档即技能，可执行文件安全扫描）；多助手、笔记与对话互哺（引用 + 一键存回）、导出全家桶、SSRF/路径逃逸门控。

## 内置工具列表

后端 `app/tools/` 经 `registry.register()` 静态注册 **33 个工具函数**，另有 **MCP 动态扩展**（`mcp_client.py` 运行时将外部 MCP 服务注册为工具，上不封顶）与 **9 项系统技能**（`backend/skills/`，SKILL.md 机读操作手册）。

| 类别       | 工具函数                        | 说明                                                                       |
| ---------- | ------------------------------- | -------------------------------------------------------------------------- |
| 联网检索   | `web_search`                  | 多引擎接力搜索（主引擎 + fallback 链可配置），农场域名黑名单，来源逐条落库 |
| 网页深读   | `browser`                     | 打开网页并抽取正文（文章/文档快速阅读）                                    |
| 浏览器操作 | `browser_navigate`            | 导航到指定 URL                                                             |
|            | `browser_snapshot`            | 页面结构化快照（含可交互元素）                                             |
|            | `browser_click`               | 点击元素                                                                   |
|            | `browser_type`                | 表单输入                                                                   |
|            | `browser_scroll`              | 页面滚动                                                                   |
|            | `browser_press`               | 键盘按键                                                                   |
|            | `browser_back`                | 返回上一页                                                                 |
|            | `browser_extract`             | 抽取页面正文内容                                                           |
|            | `browser_execute_js`          | 执行自定义 JavaScript                                                      |
|            | `browser_screenshot`          | 页面截图                                                                   |
| 代码执行   | `execute_code`                | Python 代码沙箱（自动修复循环、中文字体内置、超时长任务自检引导）          |
| 终端       | `terminal`                    | 受控 shell 命令执行（敏感操作走审批）                                      |
| 文档查询   | `context7_resolve_library_id` | 库名 → Context7 库 ID 解析（查文档前必调）                                |
|            | `context7_query_docs`         | 查询库/框架官方文档（可指定版本）                                          |
| 笔记       | `notes`                       | 笔记本/笔记的列表、读取、创建、修改、删除                                  |
| 记忆       | `memory`                      | 跨会话长期记忆（agent/user 双目标，add/replace/remove）                    |
| 任务编排   | `delegate_task`               | 子代理并行委派（隔离上下文，深度 ≤2，主代理汇总验收）                     |
|            | `background_task`             | 后台长线任务（≤5h 超时，完成后主动播报）                                  |
|            | `schedule`                    | 定时任务（自然语言→cron；创建/列出/取消/立即触发）                        |
|            | `session_search`              | 跨会话全文搜索（翻旧账、找既有结论）                                       |
|            | `mixture_of_agents`           | 混合专家：多模型并行作答 + 聚合模型综合                                    |
| 文件工作区 | `workspace_read`              | 读取用户工作区文件                                                         |
|            | `workspace_glob`              | 按 glob 模式查找文件                                                       |
|            | `grep`                        | 工作区内容正则搜索                                                         |
|            | `diff`                        | 内容对比                                                                   |
|            | `word_count`                  | 字数/词数/行数统计（创作任务质检）                                         |
|            | `provide_file`                | 把工作区文件作为下载卡片交付给用户                                         |
| 导出       | `pdf_export`                  | 笔记/对话记录/工作区文件导出 PDF                                           |
| 技能       | `skill_view`                  | 加载技能操作手册（SKILL.md 全文）                                          |
|            | `skill_manage`                | 创建/修改用户技能（可执行文件安全扫描）                                    |
|            | `skill_run_script`            | 执行技能捆绑脚本                                                           |
| 扩展       | MCP（动态）                     | 运行时注册任意外部 MCP 工具服务                                            |

## 系统要求与部署

三平台分步指南（系统依赖 + pip/npm 依赖 + 部署方式，均含可直接复制的命令）：

| 平台                                        | 文档                                                        |
| ------------------------------------------- | ----------------------------------------------------------- |
| macOS                                       | [requirements/macos.md](requirements/macos.md)               |
| Ubuntu / Debian                             | [requirements/ubuntu.md](requirements/ubuntu.md)             |
| Windows / WSL2                              | [requirements/windows.md](requirements/windows.md)           |
| 完整依赖清单（系统/pip/npm + license 摘要） | [requirements/dependencies.md](requirements/dependencies.md) |
| 部署文档总览                                | [requirements/DEPLOYMENT.md](requirements/DEPLOYMENT.md)     |

最简依赖集：**Python ≥ 3.10（推荐 3.12/3.13）· Node.js ≥ 18（推荐 20/22）· PostgreSQL ≥ 14**，
外加（可选）Android SDK（做 APK 壳）与 Playwright Chromium（跑 E2E + 服务端浏览器工具，
npm/Python 双侧各装一次，见「测试」节）。
依赖 license 合规审核见 [docs/LICENSE-COMPLIANCE.md](docs/LICENSE-COMPLIANCE.md)。

### 部署流程（逐步命令；macOS 与 Ubuntu 差异处已标注，Windows/WSL2 见 windows.md）

```bash
# ── 0. 获取代码 ───────────────────────────────────────────────────
# 有 git 仓库：
git clone <your-fork-url> weave-thinker && cd weave-thinker
# 无 git 仓库 / 出网受限（源码包分发）：仓库根执行
export COPYFILE_DISABLE=1        # macOS：禁 BSD tar 写 AppleDouble 元数据（._* 文件）
tar -czf wt-src.tgz .
# → scp/内网传输到目标机 → mkdir weave-thinker && tar xzf wt-src.tgz -C weave-thinker && cd weave-thinker
# （tar 内结构即仓库根结构：backend/ frontend/ scripts/ 等；出网受限环境
#   可经内网/远程管理通道分块传输源码包）
# ⚠ 源码包分发的两个坑：
# 1. 目标机直连 github.com 时通时断（CN 机房）——git clone 整体超时失败时就走
#    本打包流程（本地 clone → tar → scp → 解压，含 .git 可增量同步）。
# 2. macOS 打包未设 COPYFILE_DISABLE=1 时，BSD tar 会把 xattr 写成 AppleDouble
#    的 ._* 文件，污染解压后的 .git 包索引（git 校验/命令异常）；已带入时解压端
#    先执行 find . -name "._*" -delete 清理。

# ── 1. 准备 PostgreSQL ────────────────────────────────────────────
#    （复用已有远端 PostgreSQL 时整节跳过：第 3 步 [database] 直接填远端
#      host/port/username/password/name 即可，服务与本机 PG 无区别。）
#    Ubuntu（sudo -u postgres 直连本机 postgres 账号）：
sudo apt install -y postgresql && sudo service postgresql start
sudo -u postgres psql -c "CREATE USER weavethinker WITH PASSWORD 'CHANGE_ME_strong_password';"
sudo -u postgres psql -c "CREATE DATABASE weavethinker OWNER weavethinker ENCODING 'UTF8' TEMPLATE template0;"
#    macOS（Homebrew 版不接受 sudo -u postgres，改用本机直连）：
#      brew install postgresql@16 && brew services start postgresql@16
#      psql -U postgres -h 127.0.0.1   # 然后执行上面两条 CREATE
#    完整差异与故障排查见 requirements/{ubuntu,macos}.md 第 2 节。
# 可选（记忆向量化检索，不装也能跑，仅 v2 记忆 embedding 功能降级）：
#   CREATE EXTENSION vector;
#   注意：Ubuntu 22.04 默认源无此扩展，需 pgdg apt 源或编译安装
#   （postgresql-16-pgvector）；macOS: brew install pgvector；
#   Windows 从 pgvector GitHub Releases 解压到 PG 安装目录。

# ── 2. 后端环境 ───────────────────────────────────────────────────
python3 -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate
python -m pip install -U pip             # 老 pip 装不动新 wheel，先升级
pip install -r backend/requirements.txt

# ── 3. 配置 ───────────────────────────────────────────────────────
cp backend/config.toml.example            backend/config.toml
cp backend/config_model.toml.example      backend/config_model.toml
# 编辑 backend/config.toml：
#   [server]   host/port（默认 0.0.0.0:8158）
#   [security] jwt_secret_key —— 务必改成随机长串（openssl rand -hex 32）
#   [database] host/username/password/name —— 指向你的库
# 编辑 backend/config_model.toml：
#   至少配置一个 LLM provider（[providers] 下，OpenAI 兼容格式；
#   可接云端 API，或本地 vLLM/Ollama 等自托管服务）
#   ASR/TTS（语音功能，可选）、embedding/rerank（记忆 v2，可选）
#   ※ 未填的 <YOUR-*> 占位**不阻塞启动**：仅对应功能在真实调用时报
#     缺配（实测：provider 全占位 → 服务正常、页面可登录、记忆
#     子系统自动降级并在日志记录原因）；可先起栈后补 key

# ── 4. TLS 证书（生产构建 & Android 壳建议）──────────────────────
cd backend
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=localhost" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost"
# 服务器部署请把 CN/subjectAltName 改成你的域名/内网 IP
cd ..

# ── 5. 前端构建（产物 → backend/static/）─────────────────────────
./scripts/project_build.sh    # 内部 = cd frontend && npm install && npm run build

# ── 6. 启动 / 停止 ───────────────────────────────────────────────
./scripts/start.sh            # nohup + PID 文件 + 自动检测 backend/ 下的证书
./scripts/status.sh
./scripts/stop.sh
./scripts/restart.sh
```

打开 `https://<host>:8158/app/frontend/`，注册第一个账号，创建助手
（选择 LLM 供应商与 API 地址）后即可对话。`GET /` 会 307 到前端；
`/docs` 是 Swagger UI。JWT 默认 7 天有效、支持滑动续期。

**复用（非空/共享）远端数据库时的两条注意**：

1. 「**首用户即管理员**」只对**全新空库**成立——复用已有库时以库中
   既有账号为准，不要期待注册获得管理员身份。
2. **多实例共库**：每个实例都会轮询/执行共享表上的定时任务
   （`agent_scheduler` / `agent_worker` / 记忆调度器），会产生重复
   消费竞态——同一数据面建议单实例写共享库，或独立库隔离多实例。

### 生产部署提示

- **部署端口先评估、后验数据面**：telnet/nc 握手成功 ≠ 可部署——部分云厂商
  边缘对未放行的端口做 TCP 被动应答（握手成但零数据，TLS 与非 TLS 均静默）。
  部署前用 telnet 筛端口可达性；部署后以真实响应验收：
  `curl -k https://<host>:8158/docs` 返回 200。若放行安全组后仍持续 000，
  生效层多半在供应商的 NAT/EIP 端口映射表而非安全组（该行为存在
  逐箱/逐端口差异，非通用规律）——按 [ubuntu.md「端口排查阶梯」](requirements/ubuntu.md)
  逐级定位；既有映射端口正常时亦可 Nginx 反代（ubuntu.md 第 7 节）。
- **systemd（Ubuntu 示例）**：见 [requirements/ubuntu.md](requirements/ubuntu.md) 第 7 节
- **反向代理**：Nginx 终结 TLS 后把 8158 转发到本机即可；注意 `Upgrade`/`Connection`
  头透传与 `proxy_buffering off`（SSE 与 WebSocket 需要，配置示例见 ubuntu.md）
- **数据备份**：全部状态在 PostgreSQL（`pg_dump -Fc`）+ 两个运行时目录
  `user_workspaces/`（用户上传/工作区文件）与 `backend/agent_memories/`（文件记忆）；
  `backend/config*.toml` 含密钥，纳入密钥管理而非普通备份
- **前端开发模式（Development Mode）**：`cd frontend && npm run dev`
  （Vite 起在 5173 端口，把 `/api` 代理到 `https://localhost:8158`）——因此
  开发时后端也必须带证书；后端可用
  `cd backend && uvicorn main:app --host 0.0.0.0 --port 8158 --reload` 手动起
  （仅开发用，生产一律走 scripts/start.sh）
- **Android 壳（可选）**：`./scripts/apk_generate.sh https://<your-server>:8158`
  （需要 Android SDK + JDK 11+；壳内信任自签证书）

### API 文档

完整后端接口参考（字段级表格 + 请求/返回示例 + SSE/死磕/语音语义）：
[docs/API.md](docs/API.md)

## 架构速览

```
frontend/  Vue 3 + TS + Vite + Pinia（SSE 流式渲染 · 全双工语音 UI · 3 皮肤令牌体系）
                     │  /api/*（JWT）
backend/    FastAPI + async SQLAlchemy 2.0
  ├─ app/api/        20+ 路由模块（auth/chat/conversations/notes/assistants/skills/voice/asr/…）
  ├─ app/services/   Agent 编排 · AgentLoop（工具循环）· 记忆三层 · 死磕 · 调度 · 导出
  ├─ app/tools/      工具体系（33 个工具函数 + MCP 动态扩展）
  ├─ app/db/         模型 + 启动幂等迁移（无 Alembic，STARTUP_MIGRATIONS）
  └─ skills/         系统技能（SKILL.md 目录，Agent 可加载执行）
webview-app/ Android WebView 壳（可选，JS 桥 window.WeaverNoteApp）
scripts/     构建/启停生命周期（PID 文件安全，stop 只杀记录的 PID）
```

## 项目结构

| 路径              | 内容                                                                                                           |
| ----------------- | -------------------------------------------------------------------------------------------------------------- |
| `backend/`      | FastAPI 服务（`main.py` 入口；`app/` 代码；`skills/` 系统技能；`agent_memories/func.md` 系统功能文档） |
| `frontend/`     | Vue 3 前端（`src/`；`e2e/` Playwright）                                                                    |
| `scripts/`      | `project_build.sh` / `start.sh` / `stop.sh` / `restart.sh` / `status.sh` / `apk_generate.sh`       |
| `webview-app/`  | Android WebView 壳源码（Gradle）                                                                               |
| `requirements/` | 分平台部署与依赖文档                                                                                           |
| `docs/`         | 产品介绍（ARCHITECTURE.md）、皮肤令牌契约（SKINS.md）                                                          |

## 文档

- [docs/API.md](docs/API.md) — 后端接口详版（字段级表格 + 示例 + SSE/WS 协议）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 功能与机制总览
- [docs/SKINS.md](docs/SKINS.md) — 皮肤系统令牌契约（前端/自定义皮肤开发必读）
- [docs/LICENSE-COMPLIANCE.md](docs/LICENSE-COMPLIANCE.md) — 依赖库 license 合规审核报告（逐项判定 + fork/PR 满足方式）
- [CONTRIBUTING.md](CONTRIBUTING.md) — 贡献指南（DCO / CCLA 流程）

## 测试

```bash
# 前端 E2E（Playwright，目标 = 8158 生产构建；需后端带证书运行）
cd frontend
npx playwright install chromium
npx playwright test e2e/chat.spec.ts --config playwright.prod8158.config.ts
```

服务端「网页深读 / 浏览器 10 件套」工具依赖 **Python 侧** Playwright Chromium：
`.venv/bin/python -m playwright install chromium`（npm 侧 `npx playwright install chromium` 服务前端 E2E。
两侧 playwright 精确锁定同一版本（npm `1.60.0` 与 pip `==1.60.0`），chromium 构建 revision 一致、
浏览器缓存共享——任一侧装一次即可双侧复用；不装仅该能力族受影响，部署与页面验证不受阻）。

本开源发行不含后端单元测试集；后端改动的验证要求见 [CONTRIBUTING.md](CONTRIBUTING.md)「测试要求」。

## 贡献 / How to contribute

贡献代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，其中包含 DCO sign-off 与公司 CCLA 的要求。

**本项目鼓励Fork，欢迎企业内部自行定制分支。**

> Fork 后建议先改写 `backend/agent_memories/func.md`——这是 agent 做自我介绍与
> 产品功能解答时读取的系统文档（只读，随仓分发），换成你们自己的产品文案后
> 对话中的「我是谁 / 我能做什么」即与品牌一致。

## 许可证

[Apache License 2.0](LICENSE)

> 免责声明：本项目按"现状"提供，无任何担保。使用者应自行评估依赖许可证（尤其 AGPL/GPL/LGPL 条目）与其分发场景的合规性，并妥善保管配置中的密钥。
