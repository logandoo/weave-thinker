<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# 依赖清单（跨平台汇总）

> 版本与 license 数据来自 2026-08-26 的依赖合规审核（见根目录
> [LICENSE-COMPLIANCE.md](../docs/LICENSE-COMPLIANCE.md)，证据在 `tests/license_audit/`）。
> 锁精确版本：pip 见 `backend/requirements.txt`，npm 见 `frontend/package-lock.json`
> （构建请一律走 `scripts/project_build.sh`（内部 `npm install` + 锁文件）/
> `pip install -r`，不要放宽版本）。

## 1. 系统级依赖

| 依赖 | 最低版本 | 推荐版本 | 必需性 | 用途 |
|---|---|---|---|---|
| PostgreSQL | 14 | 16 | **必需** | 主数据库（会话/笔记/任务/记忆全部状态） |
| Python | 3.10 | 3.12 / 3.13 | **必需** | 后端运行时（venv 隔离） |
| Node.js + npm | 18 | 20 / 22 | **必需（构建期）** | 前端构建（Vite） |
| OpenSSL | 3.x | 3.x | 建议 | 生成自签证书（key.pem/cert.pem） |
| pgvector 扩展 | PG15+ 内置 | 16 | 可选 | 记忆 v2 向量化检索；缺失时仅该功能降级 |
| Playwright Chromium（×2 侧） | npm 侧随 lock 1.58.2 / Python 侧 1.60.0 | — | 可选（E2E + 浏览器工具） | 两侧 revision 不一致：E2E `npx playwright install chromium`；服务端工具 `python -m playwright install chromium` |
| Android SDK + JDK 11+ | — | Studio 稳定版 | 可选 | 仅构建 `webview-app/` APK 时需要 |
| ffmpeg | — | 6+ | 可选 | 语音音轨处理/转码（部分导出场景） |
| Pango / HarfBuzz / GDK-PixBuf | pango ≥ 1.44 | — | 可选（PDF 导出） | weasyprint 系统库，包名见 ubuntu.md / macos.md §1 |
| CJK 字体 | — | — | 可选（PDF/代码输出中文） | 本发行不含 `backend/Fonts/`：Linux 装 fonts-noto-cjk，macOS 自带 PingFang |

## 2. Python 依赖（31 条直接声明 = 31 个包 + 主要传递依赖，`backend/requirements.txt`）

| 包 | 锁定版本 | License | 说明 |
|---|---|---|---|
| fastapi | 0.136.3 | MIT | Web 框架 |
| uvicorn | 0.49.0 | BSD-3-Clause | ASGI 服务器 |
| sqlalchemy | 2.0.50 | MIT | ORM 异步（`[asyncio]` extra 仅带 greenlet，**不含** driver） |
| asyncpg | 0.31.0 | Apache-2.0 | PG 异步驱动（`postgresql+asyncpg` URL 后端，必需；2026-08-27 部署核查补入） |
| alembic | 1.18.4 | MIT | 迁移工具（仓库未启用 Alembic 流程，保留依赖；schema 变更走 app/db/migrations.py） |
| pyjwt | 2.13.0 | MIT | JWT |
| bcrypt | 5.0.0 | Apache Software License | 口令哈希 |
| python-multipart | 0.0.32 | Apache-2.0 | multipart 解析（上传/表单） |
| sse-starlette | 3.4.4 | BSD-3-Clause | SSE 流式响应 |
| websockets | 16.0 | BSD-3-Clause | WebSocket（语音全双工） |
| httpx | 0.28.1 | BSD License | 出站 HTTP（LLM/搜索/浏览器） |
| openai | 2.41.0 | Apache Software License | OpenAI 兼容 LLM 客户端 |
| pgvector | 0.5.0 | MIT | PG 向量类型绑定 |
| toml | 0.10.2 | MIT License | 配置解析（Python<3.11 兜底；3.11+ 内置 tomllib 优先） |
| markdown | 3.10.2 | BSD-3-Clause | Markdown→HTML（导出/笔记） |
| markdown-it-py | 4.2.0 | MIT License | Markdown 解析（引用清洗等） |
| beautifulsoup4 | 4.15.0 | MIT License | HTML 解析（web 抓取） |
| jieba | 0.42.1 | MIT License | 中文分词（记忆检索 Stage 0） |
| pypinyin | 0.55.0 | MIT License | 拼音（热词/TTS 辅助） |
| pillow | 12.2.0 | MIT-CMU | 图片处理 |
| matplotlib | 3.10.9 | Python Software Foundation License | 图表（代码沙箱内） |
| openpyxl | 3.1.5 | MIT License | Excel 解析/生成 |
| python-docx | 1.2.0 | MIT License | Word 解析/生成 |
| python-pptx | 1.0.2 | MIT License | PPT 解析/生成 |
| pdfminer.six | 20260107 | MIT | PDF 解析基座（pdfplumber 内核 + 兜底纯文本 + 扫描件探测） |
| pdfplumber | 0.11.10 | MIT | **PDF 解析主引擎**（行级排版 + 表格检测） |
| weasyprint | 69.0 | BSD License | HTML→PDF 导出（传递 pyphen：GPL/LGPL/MPL 三许可可选，按 MPL-1.1 行使，见 license-compliance 3.1） |
| reportlab | 4.5.1 | BSD License | PDF 组件 |
| latex2mathml | 3.81.0 | MIT | LaTeX→MathML |
| mathml2omml | 0.0.2 | MIT License | MathML→OMML（Word 公式） |
| playwright | 1.60.0 | Apache-2.0 | 浏览器自动化（web_browse 工具 + E2E） |
| lxml | —（传递） | MIT | BS4 HTML/XML 后端 |
| numpy | —（传递） | BSD-3-Clause（多许可聚合可选） | 数值 |
> fresh `pip install -r backend/requirements.txt` 解析安装约 **84 包**
> （31 条直接声明 + ≈53 传递；以平台实装 `pip list` 为准）。
> [LICENSE-COMPLIANCE.md](../docs/LICENSE-COMPLIANCE.md) 第一节的"129 包"是
> **审计时主仓开发 venv 的实装快照**（含本地开发额外依赖，非 fresh 安装结果），
> 仅作 license 判定证据引用。

> 注：`SQLAlchemy[asyncio]` **不传递** 任何 PG driver——`asyncpg` 必须显式声明（否则
> `create_async_engine` import 即崩，2026-08-27 双服务器部署实证）。

## 3. npm 生产依赖（14 个，`frontend/package.json` dependencies）

| 包 | 锁定版本 | License | 说明 |
|---|---|---|---|
| vue | 3.5.31 | MIT | 框架 |
| vue-router | 4.6.4 | MIT | 路由（history 模式，base /app/frontend/） |
| pinia | 2.3.1 | MIT | 状态 |
| axios | 1.14.0 | MIT | HTTP 客户端（JWT 拦截器） |
| dompurify | 3.4.0 | (MPL-2.0 OR Apache-2.0) | HTML 消毒（双许可，分发选 Apache-2.0） |
| echarts | 6.1.0 | Apache-2.0 | 交互图表 |
| mermaid | 11.14.0 | MIT | 流程图/时序图 |
| katex | 0.16.45 | MIT | 公式渲染 |
| marked | 17.0.5 | MIT | Markdown（流式） |
| marked-katex-extension | 5.1.8 | MIT | marked 公式扩展 |
| highlight.js | 11.11.1 | BSD-3-Clause | 代码高亮 |
| @tanstack/vue-virtual | 3.13.31 | MIT | 长列表虚拟化 |
| vuedraggable | 4.1.0 | MIT | 拖拽排序 |
| simple-rnnoise-wasm | 1.1.0 | MIT | 浏览器端噪声抑制（语音） |

> devDependencies（仅构建/测试，不进产物）：vite、@vitejs/plugin-vue、typescript、vue-tsc、
> @playwright/test、@types/dompurify。npm 生产传递依赖共 180 包，完整表见
> [LICENSE-COMPLIANCE.md](../docs/LICENSE-COMPLIANCE.md) 第二节（全部 permissive，无风险）。
