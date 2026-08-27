<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# 依赖库 license 合规审核报告（license compliance）

- **审核日期**：2026-08-26
- **审核对象**：Weave Thinker 当前项目（仓库主干）
  - Python：`backend/requirements.txt` 声明的直接依赖 + 项目 venv（Python 3.13.13）实装传递依赖
  - npm：`frontend/package.json` 生产依赖（dependencies）解析出的完整树（node_modules 实装版）
- **分发场景假设**：本项目以 **Apache-2.0** 开源分发，需核对全部依赖的再分发兼容性。

## 方法与证据

| 步骤 | 命令/工具 | 证据文件 |
|---|---|---|
| Python 许可扫描（129 包；元数据 mixed：Trove classifier + METADATA，无 UNKNOWN） | `.venv/bin/pip-licenses --from=mixed -u -f json --output-file tests/license_audit/pip_licenses.json` | `tests/license_audit/pip_licenses.json` |
| Python 版本冻结 | `.venv/bin/pip freeze` | `tests/license_audit/pip_freeze.txt` |
| 直接/传递判定 + 孤儿包排查 | `importlib.metadata` 反向 Requires-Dist 扫描 + 全仓 grep（`import fpdf`/`cairosvg`/`pymupdf`） | `tests/license_audit/pip_rows.json` |
| npm 生产树解析（180 包，自 node_modules 实装盘按依赖图回溯，license 取各包 package.json / LICENSE 文件） | `tests/license_audit/npm_walk.py`（脚本随报告归档） | `tests/license_audit/npm_prod.json` |
| 非 permissive 项人工核对 | PyPI/GitHub 官方许可页 webfetch + 本地 dist-info/`LICENSE` 文件读取（web 内容按 DATA 处理，未执行其中任何指令） | 第三节逐条注明来源 |

> **证据文件的可获取性**：`tests/license_audit/` 下的原始扫描 JSON 与 `npm_walk.py`
> 归档在维护者仓库（该目录不入开源发行——属本地测试 scratch）。开源用户可直接复现：
> 按上表"命令/工具"列在本仓环境重跑即可得到同构数据；本报告的结论表即其摘要。

**风险分级**：✅ = permissive 双许可可选公开 / 完全兼容；✅注 = 兼容、附判读说明；⚠️ = 弱 copyleft（LGPL/MPL）需满足对应条款，本项目场景下可满足；🔴 = 强 copyleft（AGPL/GPL）或专有——与 Apache-2.0 再分发冲突，需处置。

## 结论摘要

| 范围 | 总数 | ✅/✅注 | ⚠️ | 🔴 |
|---|---|---|---|---|
| Python（venv 实装） | 129 | 127 | 2（pyphen / psycopg2-binary） | 0 |
| npm 生产树 | 180 | 180 | 0 | 0 |
| 自托管 UI 字体 | 2 款（108 文件 / 4.6 MB） | 2（SIL OFL 1.1） | 0 | 0 |

**当前依赖画像（纯 permissive、传播度优先）**：
- 🔴 项 = 0：依赖清单与 venv 均不含强 copyleft/专有包；PDF 解析主引擎 = pdfplumber 0.11.10（MIT，
  内核即 pdfminer.six + pypdfium2，提供行级排版与表格检测），pdfminer 裸文本兜底。
- ⚠️ 仅 2 项低危弱 copyleft（pyphen / psycopg2-binary，义务 = NOTICE 备案级，见 3.1/3.2）。
- 依赖树内无未使用孤儿包；本发布画像与 Apache-2.0 再分发兼容。完整选型论证与商业化分析见
  维护者内部文档（不随开源发行；商业/依赖处置事宜请联系 logandoo@126.com）。
- 自托管 UI 字体：Inter 4.001（7 个 unicode-range 分片，214 KB）+ Noto Sans SC
  2.004-H2（101 片，4.4 MB）vendored 于 `frontend/public/fonts/`，SIL OFL 1.1；
  版权通知与 OFL 全文随 `FONTS_LICENSE.md` 分发（OFL §2 义务），woff2 内嵌
  机器可读许可字段（name table #13/#14），见第五节。

---

## 一、Python 依赖全表（129 包，venv 实装）

| 包名 | 版本 | License | 依赖类型 | 风险 | 说明/来源 |
|---|---|---|---|---|---|
| alembic | 1.18.4 | MIT | 直接 | ✅ |  |
| bcrypt | 5.0.0 | Apache Software License | 直接 | ✅ |  |
| beautifulsoup4 | 4.15.0 | MIT License | 直接 | ✅ |  |
| fastapi | 0.136.3 | MIT | 直接 | ✅ |  |
| httpx | 0.28.1 | BSD License | 直接 | ✅ |  |
| jieba | 0.42.1 | MIT License | 直接 | ✅ |  |
| latex2mathml | 3.81.0 | MIT | 直接 | ✅ |  |
| Markdown | 3.10.2 | BSD-3-Clause | 直接 | ✅ |  |
| markdown-it-py | 4.2.0 | MIT License | 直接 | ✅ |  |
| mathml2omml | 0.0.2 | MIT License | 直接 | ✅ |  |
| matplotlib | 3.10.9 | Python Software Foundation License | 直接 | ✅注 | PSF 许可（matplotlib 自定义，含 BSD/Python 条款，permissive） |
| openai | 2.41.0 | Apache Software License | 直接 | ✅ |  |
| openpyxl | 3.1.5 | MIT License | 直接 | ✅ |  |
| pdfminer.six | 20260107 | MIT | 直接 | ✅ |  |
| pdfplumber | 0.11.10 | MIT License | 直接 | ✅ | PDF 解析主引擎（MIT；内核 pdfminer.six + pypdfium2，另具表格检测） |
| pgvector | 0.5.0 | MIT | 直接 | ✅ |  |
| pillow | 12.2.0 | MIT-CMU | 直接 | ✅注 | MIT-CMU（PIL 许可，permissive） |
| playwright | 1.60.0 | Apache-2.0 | 直接 | ✅ |  |
| PyJWT | 2.13.0 | MIT | 直接 | ✅ |  |
| pypinyin | 0.55.0 | MIT License | 直接 | ✅ |  |
| python-docx | 1.2.0 | MIT License | 直接 | ✅ |  |
| python-multipart | 0.0.32 | Apache-2.0 | 直接 | ✅ |  |
| python-pptx | 1.0.2 | MIT License | 直接 | ✅ |  |
| reportlab | 4.5.1 | BSD License | 直接 | ✅注 | BSD（含 ReportLab 附录条款，permissive） |
| SQLAlchemy | 2.0.50 | MIT | 直接 | ✅ |  |
| sse-starlette | 3.4.4 | BSD-3-Clause | 直接 | ✅ |  |
| toml | 0.10.2 | MIT License | 直接 | ✅ |  |
| uvicorn | 0.49.0 | BSD-3-Clause | 直接 | ✅ |  |
| weasyprint | 69.0 | BSD License | 直接 | ✅注 | BSD-3-Clause |
| websockets | 16.0 | BSD-3-Clause | 直接 | ✅ |  |
| aiohttp | 1.0.5 | Apache Software License | 传递 | ✅ |  |
| annotated-doc | 0.0.4 | MIT | 传递 | ✅ |  |
| annotated-types | 0.7.0 | MIT License | 传递 | ✅ |  |
| anyio | 4.13.0 | MIT | 传递 | ✅ |  |
| async-timeout | 5.0.1 | Apache Software License | 传递 | ✅ |  |
| asyncpg | 0.31.0 | Apache-2.0 | 传递 | ✅ |  |
| brotli | 1.2.0 | MIT | 传递 | ✅ |  |
| cairocffi | 1.7.1 | BSD License | 传递 | ✅ |  |
| certifi | 2026.5.20 | Mozilla Public License 2.0 (MPL 2.0) | 传递 | ✅注 | MPL-2.0（文件级弱 copyleft，分发无障碍） |
| cffi | 2.0.0 | MIT | 传递 | ✅ |  |
| chardet | 7.4.3 | 0BSD | 传递 | ✅注 | 0BSD |
| charset-normalizer | 3.4.7 | MIT | 传递 | ✅ |  |
| click | 8.4.2 | BSD-3-Clause | 传递 | ✅ |  |
| contourpy | 1.3.3 | BSD License | 传递 | ✅ |  |
| cryptography | 48.0.0 | Apache-2.0 OR BSD-3-Clause | 传递 | ✅注 | Apache-2.0 OR BSD-3-Clause（双许可可选 Apache） |
| cssselect2 | 0.9.0 | BSD License | 传递 | ✅ |  |
| cycler | 0.12.1 | BSD License | 传递 | ✅ |  |
| datasets | 5.0.0 | Apache Software License | 传递 | ✅ |  |
| ddgs | 9.14.4 | MIT | 传递 | ✅ |  |
| defusedxml | 0.7.1 | Python Software Foundation License | 传递 | ✅ |  |
| dill | 0.4.1 | BSD License | 传递 | ✅ |  |
| distro | 1.9.0 | Apache Software License | 传递 | ✅ |  |
| et_xmlfile | 2.0.0 | MIT License | 传递 | ✅ |  |
| fake-useragent | 2.2.0 | Apache-2.0 | 传递 | ✅ |  |
| filelock | 3.31.0 | MIT | 传递 | ✅ |  |
| flatbuffers | 25.12.19 | Apache Software License | 传递 | ✅ |  |
| fonttools | 4.63.0 | MIT | 传递 | ✅ |  |
| fsspec | 2026.4.0 | BSD-3-Clause | 传递 | ✅ |  |
| greenlet | 3.5.1 | MIT AND PSF-2.0 | 传递 | ✅注 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT License | 传递 | ✅ |  |
| h2 | 4.3.0 | MIT License | 传递 | ✅ |  |
| hf-xet | 1.5.2 | Apache-2.0 | 传递 | ✅ |  |
| hpack | 4.1.0 | MIT License | 传递 | ✅ |  |
| httpcore | 1.0.9 | BSD-3-Clause | 传递 | ✅ |  |
| httptools | 0.8.0 | MIT | 传递 | ✅ |  |
| huggingface_hub | 1.24.0 | Apache Software License | 传递 | ✅ |  |
| hyperframe | 6.1.0 | MIT License | 传递 | ✅ |  |
| idna | 3.17 | BSD-3-Clause | 传递 | ✅ |  |
| iniconfig | 2.3.0 | MIT | 传递 | ✅ |  |
| jiter | 0.15.0 | MIT | 传递 | ✅ |  |
| jwt | 1.4.0 | Apache Software License | 传递 | ✅ |  |
| kiwisolver | 1.5.0 | BSD License | 传递 | ✅ |  |
| lxml | 6.1.1 | BSD-3-Clause | 传递 | ✅ |  |
| Mako | 1.3.12 | MIT License | 传递 | ✅ |  |
| MarkupSafe | 3.0.3 | BSD-3-Clause | 传递 | ✅ |  |
| mdurl | 0.1.2 | MIT License | 传递 | ✅ |  |
| multidict | 6.7.1 | Apache License 2.0 | 传递 | ✅ |  |
| multiprocess | 0.70.19 | BSD License | 传递 | ✅ |  |
| networkx | 3.6.1 | BSD-3-Clause | 传递 | ✅ |  |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 传递 | ✅注 | 多许可聚合，可选 BSD-3-Clause |
| olefile | 0.47 | BSD License | 传递 | ✅ |  |
| onnxruntime | 1.27.0 | MIT License | 传递 | ✅ |  |
| opencv-python | 5.0.0.93 | Apache Software License | 传递 | ✅ |  |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause | 传递 | ✅注 | Apache-2.0 OR BSD-2-Clause |
| pandas | 3.0.3 | BSD License | 传递 | ✅ |  |
| pdf2image | 1.17.0 | MIT License | 传递 | ✅ |  |
| pluggy | 1.6.0 | MIT License | 传递 | ✅ |  |
| primp | 1.3.1 | MIT License | 传递 | ✅ |  |
| protobuf | 7.35.1 | 3-Clause BSD License | 传递 | ✅ |  |
| psycopg2-binary | 2.9.12 | GNU Library or Lesser General Public License (LGPL) | 审计 venv 实装（非 fresh 安装引入） | ⚠️ | LGPL-2.1+；C 扩展动态链接，行业通用做法。**注意：`SQLAlchemy[asyncio]` 并不传递任何 PG driver（fresh 安装的驱动是 asyncpg，见 requirements.txt，依赖要求实测）** |
| pyarrow | 25.0.0 | Apache-2.0 | 传递 | ✅ |  |
| pyclipper | 1.4.0 | MIT License | 传递 | ✅ |  |
| pycparser | 3.0 | BSD-3-Clause | 传递 | ✅ |  |
| pydantic | 2.13.4 | MIT | 传递 | ✅ |  |
| pydantic_core | 2.46.4 | MIT | 传递 | ✅ |  |
| pydyf | 0.12.1 | BSD License | 传递 | ✅ |  |
| pyee | 13.0.1 | MIT License | 传递 | ✅ |  |
| Pygments | 2.20.0 | BSD-2-Clause | 传递 | ✅ |  |
| pyparsing | 3.3.2 | MIT | 传递 | ✅ |  |
| PyPDF2 | 3.0.1 | BSD License | 传递 | ✅ |  |
| pypdfium2 | 5.10.1 | BSD-3-Clause, Apache-2.0, dependency licenses | 传递 | ✅ |  |
| pyphen | 0.17.2 | GNU General Public License v2 or later (GPLv2+); GNU Lesser General Public License v2 or later (LGPLv2+); Mozilla Public License 1.1 (MPL 1.1) | 传递 | ⚠️ | 三许可可选（GPLv2+ / LGPLv2+ / MPL 1.1）；选 MPL 1.1 可分发。weasyprint 传递依赖 |
| pytest | 9.1.1 | MIT | 传递 | ✅ |  |
| python-dateutil | 2.9.0.post0 | Apache Software License; BSD License | 传递 | ✅ |  |
| python-dotenv | 1.2.2 | BSD-3-Clause | 传递 | ✅ |  |
| PyYAML | 6.0.3 | MIT License | 传递 | ✅ |  |
| rapidocr-onnxruntime | 1.2.3 | Apache-2.0 | 传递 | ✅ |  |
| requests | 2.34.2 | Apache Software License | 传递 | ✅ |  |
| seaborn | 0.13.2 | BSD License | 传递 | ✅ |  |
| shapely | 2.1.2 | BSD License | 传递 | ✅ |  |
| six | 1.17.0 | MIT License | 传递 | ✅ |  |
| sniffio | 1.3.1 | Apache Software License; MIT License | 传递 | ✅ |  |
| socksio | 1.0.0 | MIT License | 传递 | ✅ |  |
| soupsieve | 2.8.4 | MIT | 传递 | ✅ |  |
| starlette | 1.2.1 | BSD-3-Clause | 传递 | ✅ |  |
| tabulate | 0.10.0 | MIT | 传递 | ✅ |  |
| tinycss2 | 1.5.1 | BSD License | 传递 | ✅ |  |
| tinyhtml5 | 2.1.0 | MIT License | 传递 | ✅ |  |
| tqdm | 4.67.3 | MPL-2.0 AND MIT | 传递 | ✅注 | MPL-2.0 AND MIT（可选 MIT） |
| typing-inspection | 0.4.2 | MIT | 传递 | ✅ |  |
| typing_extensions | 4.15.0 | PSF-2.0 | 传递 | ✅注 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT | 传递 | ✅ |  |
| uvloop | 0.22.1 | Apache Software License; MIT License | 传递 | ✅ |  |
| watchfiles | 1.2.0 | MIT License | 传递 | ✅ |  |
| webencodings | 0.5.1 | BSD License | 传递 | ✅ |  |
| xlsxwriter | 3.2.9 | BSD License | 传递 | ✅ |  |
| xxhash | 3.8.1 | BSD-2-Clause | 传递 | ✅ |  |
| yt-dlp | 2026.7.4 | Unlicense | 传递 | ✅ |  |
| zopfli | 0.4.2 | Apache Software License | 传递 | ✅ |  |

## 二、npm 生产依赖全表（180 包，frontend 生产树）

| 包名 | 版本 | License | 直接/传递 | 风险 | 说明/来源 |
|---|---|---|---|---|---|
| @tanstack/vue-virtual | 3.13.31 | MIT | 直接 | ✅ |  |
| axios | 1.14.0 | MIT | 直接 | ✅ |  |
| dompurify | 3.4.0 | (MPL-2.0 OR Apache-2.0) | 直接 | ✅注 | MPL-2.0 OR Apache-2.0——分发时可选 Apache-2.0 |
| echarts | 6.1.0 | Apache-2.0 | 直接 | ✅ |  |
| highlight.js | 11.11.1 | BSD-3-Clause | 直接 | ✅ |  |
| katex | 0.16.45 | MIT | 直接 | ✅ |  |
| marked | 17.0.5 | MIT | 直接 | ✅ |  |
| marked-katex-extension | 5.1.8 | MIT | 直接 | ✅ |  |
| mermaid | 11.14.0 | MIT | 直接 | ✅ |  |
| pinia | 2.3.1 | MIT | 直接 | ✅ |  |
| simple-rnnoise-wasm | 1.1.0 | MIT | 直接 | ✅ |  |
| vue | 3.5.31 | MIT | 直接 | ✅ |  |
| vue-router | 4.6.4 | MIT | 直接 | ✅ |  |
| vuedraggable | 4.1.0 | MIT | 直接 | ✅ |  |
| @antfu/install-pkg | 1.1.0 | MIT | 传递 | ✅ |  |
| @babel/helper-string-parser | 7.27.1 | MIT | 传递 | ✅ |  |
| @babel/helper-validator-identifier | 7.28.5 | MIT | 传递 | ✅ |  |
| @babel/parser | 7.29.2 | MIT | 传递 | ✅ |  |
| @babel/types | 7.29.0 | MIT | 传递 | ✅ |  |
| @braintree/sanitize-url | 7.1.2 | MIT | 传递 | ✅ |  |
| @chevrotain/cst-dts-gen | 12.0.0 | Apache-2.0 | 传递 | ✅ |  |
| @chevrotain/gast | 12.0.0 | Apache-2.0 | 传递 | ✅ |  |
| @chevrotain/regexp-to-ast | 12.0.0 | Apache-2.0 | 传递 | ✅ |  |
| @chevrotain/types | 12.0.0 | Apache-2.0 | 传递 | ✅ |  |
| @chevrotain/utils | 12.0.0 | Apache-2.0 | 传递 | ✅ |  |
| @iconify/types | 2.0.0 | MIT | 传递 | ✅ |  |
| @iconify/utils | 3.1.0 | MIT | 传递 | ✅ |  |
| @jridgewell/sourcemap-codec | 1.5.5 | MIT | 传递 | ✅ |  |
| @mermaid-js/parser | 1.1.0 | MIT | 传递 | ✅ |  |
| @tanstack/virtual-core | 3.17.3 | MIT | 传递 | ✅ |  |
| @types/d3 | 7.4.3 | MIT | 传递 | ✅ |  |
| @types/d3-array | 3.2.2 | MIT | 传递 | ✅ |  |
| @types/d3-axis | 3.0.6 | MIT | 传递 | ✅ |  |
| @types/d3-brush | 3.0.6 | MIT | 传递 | ✅ |  |
| @types/d3-chord | 3.0.6 | MIT | 传递 | ✅ |  |
| @types/d3-color | 3.1.3 | MIT | 传递 | ✅ |  |
| @types/d3-contour | 3.0.6 | MIT | 传递 | ✅ |  |
| @types/d3-delaunay | 6.0.4 | MIT | 传递 | ✅ |  |
| @types/d3-dispatch | 3.0.7 | MIT | 传递 | ✅ |  |
| @types/d3-drag | 3.0.7 | MIT | 传递 | ✅ |  |
| @types/d3-dsv | 3.0.7 | MIT | 传递 | ✅ |  |
| @types/d3-ease | 3.0.2 | MIT | 传递 | ✅ |  |
| @types/d3-fetch | 3.0.7 | MIT | 传递 | ✅ |  |
| @types/d3-force | 3.0.10 | MIT | 传递 | ✅ |  |
| @types/d3-format | 3.0.4 | MIT | 传递 | ✅ |  |
| @types/d3-geo | 3.1.0 | MIT | 传递 | ✅ |  |
| @types/d3-hierarchy | 3.1.7 | MIT | 传递 | ✅ |  |
| @types/d3-interpolate | 3.0.4 | MIT | 传递 | ✅ |  |
| @types/d3-path | 3.1.1 | MIT | 传递 | ✅ |  |
| @types/d3-polygon | 3.0.2 | MIT | 传递 | ✅ |  |
| @types/d3-quadtree | 3.0.6 | MIT | 传递 | ✅ |  |
| @types/d3-random | 3.0.3 | MIT | 传递 | ✅ |  |
| @types/d3-scale | 4.0.9 | MIT | 传递 | ✅ |  |
| @types/d3-scale-chromatic | 3.1.0 | MIT | 传递 | ✅ |  |
| @types/d3-selection | 3.0.11 | MIT | 传递 | ✅ |  |
| @types/d3-shape | 3.1.8 | MIT | 传递 | ✅ |  |
| @types/d3-time | 3.0.4 | MIT | 传递 | ✅ |  |
| @types/d3-time-format | 4.0.3 | MIT | 传递 | ✅ |  |
| @types/d3-timer | 3.0.2 | MIT | 传递 | ✅ |  |
| @types/d3-transition | 3.0.9 | MIT | 传递 | ✅ |  |
| @types/d3-zoom | 3.0.8 | MIT | 传递 | ✅ |  |
| @types/geojson | 7946.0.16 | MIT | 传递 | ✅ |  |
| @upsetjs/venn.js | 2.0.0 | MIT | 传递 | ✅ |  |
| @vue/compiler-core | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/compiler-dom | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/compiler-sfc | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/compiler-ssr | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/devtools-api | 6.6.4 | MIT | 传递 | ✅ |  |
| @vue/reactivity | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/runtime-core | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/runtime-dom | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/server-renderer | 3.5.31 | MIT | 传递 | ✅ |  |
| @vue/shared | 3.5.31 | MIT | 传递 | ✅ |  |
| acorn | 8.16.0 | MIT | 传递 | ✅ |  |
| asynckit | 0.4.0 | MIT | 传递 | ✅ |  |
| call-bind-apply-helpers | 1.0.2 | MIT | 传递 | ✅ |  |
| chevrotain | 12.0.0 | Apache-2.0 | 传递 | ✅ |  |
| chevrotain-allstar | 0.4.1 | MIT | 传递 | ✅ |  |
| combined-stream | 1.0.8 | MIT | 传递 | ✅ |  |
| commander | 7.2.0 | MIT | 传递 | ✅ |  |
| confbox | 0.1.8 | MIT | 传递 | ✅ |  |
| cose-base | 1.0.3 | MIT | 传递 | ✅ |  |
| csstype | 3.2.3 | MIT | 传递 | ✅ |  |
| cytoscape | 3.33.2 | MIT | 传递 | ✅ |  |
| cytoscape-cose-bilkent | 4.1.0 | MIT | 传递 | ✅ |  |
| cytoscape-fcose | 2.2.0 | MIT | 传递 | ✅ |  |
| d3 | 7.9.0 | ISC | 传递 | ✅ |  |
| d3-array | 3.2.4 | ISC | 传递 | ✅ |  |
| d3-axis | 3.0.0 | ISC | 传递 | ✅ |  |
| d3-brush | 3.0.0 | ISC | 传递 | ✅ |  |
| d3-chord | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-color | 3.1.0 | ISC | 传递 | ✅ |  |
| d3-contour | 4.0.2 | ISC | 传递 | ✅ |  |
| d3-delaunay | 6.0.4 | ISC | 传递 | ✅ |  |
| d3-dispatch | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-drag | 3.0.0 | ISC | 传递 | ✅ |  |
| d3-dsv | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-ease | 3.0.1 | BSD-3-Clause | 传递 | ✅ |  |
| d3-fetch | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-force | 3.0.0 | ISC | 传递 | ✅ |  |
| d3-format | 3.1.2 | ISC | 传递 | ✅ |  |
| d3-geo | 3.1.1 | ISC | 传递 | ✅ |  |
| d3-hierarchy | 3.1.2 | ISC | 传递 | ✅ |  |
| d3-interpolate | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-path | 3.1.0 | ISC | 传递 | ✅ |  |
| d3-polygon | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-quadtree | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-random | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-sankey | 0.12.3 | BSD-3-Clause | 传递 | ✅ |  |
| d3-scale | 4.0.2 | ISC | 传递 | ✅ |  |
| d3-scale-chromatic | 3.1.0 | ISC | 传递 | ✅ |  |
| d3-selection | 3.0.0 | ISC | 传递 | ✅ |  |
| d3-shape | 3.2.0 | ISC | 传递 | ✅ |  |
| d3-time | 3.1.0 | ISC | 传递 | ✅ |  |
| d3-time-format | 4.1.0 | ISC | 传递 | ✅ |  |
| d3-timer | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-transition | 3.0.1 | ISC | 传递 | ✅ |  |
| d3-zoom | 3.0.0 | ISC | 传递 | ✅ |  |
| dagre-d3-es | 7.0.14 | MIT | 传递 | ✅ |  |
| dayjs | 1.11.20 | MIT | 传递 | ✅ |  |
| delaunator | 5.1.0 | ISC | 传递 | ✅ |  |
| delayed-stream | 1.0.0 | MIT | 传递 | ✅ |  |
| dunder-proto | 1.0.1 | MIT | 传递 | ✅ |  |
| entities | 7.0.1 | BSD-2-Clause | 传递 | ✅ |  |
| es-define-property | 1.0.1 | MIT | 传递 | ✅ |  |
| es-errors | 1.3.0 | MIT | 传递 | ✅ |  |
| es-object-atoms | 1.1.1 | MIT | 传递 | ✅ |  |
| es-set-tostringtag | 2.1.0 | MIT | 传递 | ✅ |  |
| estree-walker | 2.0.2 | MIT | 传递 | ✅ |  |
| follow-redirects | 1.15.11 | MIT | 传递 | ✅ |  |
| form-data | 4.0.5 | MIT | 传递 | ✅ |  |
| function-bind | 1.1.2 | MIT | 传递 | ✅ |  |
| get-intrinsic | 1.3.0 | MIT | 传递 | ✅ |  |
| get-proto | 1.0.1 | MIT | 传递 | ✅ |  |
| gopd | 1.2.0 | MIT | 传递 | ✅ |  |
| hachure-fill | 0.5.2 | MIT | 传递 | ✅ |  |
| has-symbols | 1.1.0 | MIT | 传递 | ✅ |  |
| has-tostringtag | 1.0.2 | MIT | 传递 | ✅ |  |
| hasown | 2.0.2 | MIT | 传递 | ✅ |  |
| iconv-lite | 0.6.3 | MIT | 传递 | ✅ |  |
| internmap | 2.0.3 | ISC | 传递 | ✅ |  |
| khroma | 2.1.0 | [FILE:LICENSE] | 传递 | ✅注 | 元数据无 license 字段，人工核对其 LICENSE 文件 = MIT |
| langium | 4.2.2 | MIT | 传递 | ✅ |  |
| layout-base | 1.0.2 | MIT | 传递 | ✅ |  |
| lodash-es | 4.18.1 | MIT | 传递 | ✅ |  |
| magic-string | 0.30.21 | MIT | 传递 | ✅ |  |
| math-intrinsics | 1.1.0 | MIT | 传递 | ✅ |  |
| mime-db | 1.52.0 | MIT | 传递 | ✅ |  |
| mime-types | 2.1.35 | MIT | 传递 | ✅ |  |
| mlly | 1.8.2 | MIT | 传递 | ✅ |  |
| nanoid | 3.3.11 | MIT | 传递 | ✅ |  |
| package-manager-detector | 1.6.0 | MIT | 传递 | ✅ |  |
| path-data-parser | 0.1.0 | MIT | 传递 | ✅ |  |
| pathe | 2.0.3 | MIT | 传递 | ✅ |  |
| picocolors | 1.1.1 | ISC | 传递 | ✅ |  |
| pkg-types | 1.3.1 | MIT | 传递 | ✅ |  |
| points-on-curve | 0.2.0 | MIT | 传递 | ✅ |  |
| points-on-path | 0.2.1 | MIT | 传递 | ✅ |  |
| postcss | 8.5.8 | MIT | 传递 | ✅ |  |
| proxy-from-env | 2.1.0 | MIT | 传递 | ✅ |  |
| robust-predicates | 3.0.3 | Unlicense | 传递 | ✅注 | "Unlicense" = 公有领域（Unlicense 文本），人工核对 LICENSE 确认 |
| roughjs | 4.6.6 | MIT | 传递 | ✅ |  |
| rw | 1.3.3 | BSD-3-Clause | 传递 | ✅ |  |
| safer-buffer | 2.1.2 | MIT | 传递 | ✅ |  |
| sortablejs | 1.14.0 | MIT | 传递 | ✅ |  |
| source-map-js | 1.2.1 | BSD-3-Clause | 传递 | ✅ |  |
| stylis | 4.3.6 | MIT | 传递 | ✅ |  |
| tinyexec | 1.1.1 | MIT | 传递 | ✅ |  |
| ts-dedent | 2.2.0 | MIT | 传递 | ✅ |  |
| tslib | 2.3.0 | 0BSD | 传递 | ✅ |  |
| ufo | 1.6.3 | MIT | 传递 | ✅ |  |
| uuid | 11.1.0 | MIT | 传递 | ✅ |  |
| vscode-jsonrpc | 8.2.0 | MIT | 传递 | ✅ |  |
| vscode-languageserver | 9.0.1 | MIT | 传递 | ✅ |  |
| vscode-languageserver-protocol | 3.17.5 | MIT | 传递 | ✅ |  |
| vscode-languageserver-textdocument | 1.0.12 | MIT | 传递 | ✅ |  |
| vscode-languageserver-types | 3.17.5 | MIT | 传递 | ✅ |  |
| vscode-uri | 3.1.0 | MIT | 传递 | ✅ |  |
| vue-demi | 0.14.10 | MIT | 传递 | ✅ |  |
| zrender | 6.1.0 | BSD-3-Clause | 传递 | ✅ |  |

---

## 三、风险项逐条分析（⚠️ 逐项判定 + fork/PR 满足方式，全部列明依据）

> **判定总则**：本报告对每个 ⚠️ 项给出明确结论——本项目**是否满足条款**；并分别给出
> **fork（再分发）** 与 **PR（向本项目贡献代码）** 两个场景下如何满足条款的具体操作。
> 未在本节列出的包均为 permissive（✅/✅注），无再分发义务，无需额外动作。

### 3.1 ⚠️ pyphen（GPL-2.0+ or LGPL-2.1+ or MPL-1.1，三许可可选）

- **引入方式**：`weasyprint>=0.9.1` 传递依赖（Requires-Dist 证据同上）。
- **许可事实**（依据）：PyPI 项目页 <https://pypi.org/project/pyphen/> ——
  "Free software: GPL 2.0+ or LGPL 2.1+ or MPL 1.1 for the code"；仓库
  <https://github.com/Kozea/Pyphen>。
- **判定（本项目是否满足条款）**：**满足**。pyphen 三许可可选，本项目行使 **MPL-1.1**
  （文件级弱 copyleft）：pyphen 代码未修改、未并入本项目源码，MPL 义务不传染本项目
  Apache-2.0 代码，本项目以 Apache-2.0 分发不违反 pyphen 任何许可分支。
- **fork（再分发）如何满足条款**：
  1. 随发行物保留 pyphen 的 MPL-1.1 许可文本与版权声明（pip 安装的 dist-info 自带 LICENSE，随发行即可）；
  2. 在 `NOTICE`/依赖表标注「pyphen：MPL-1.1」；
  3. 若修改 pyphen 的源码文件，须按 MPL-1.1 §3.1 以可获取的源码形式（Source Code Form）提供修改版并保留原许可头——本项目未修改，故当前无需此步。
- **PR（贡献）如何满足条款**：本项目仓库不包含 pyphen 源码（仅 `requirements.txt` 依赖声明），
  向本项目提交代码不触发 pyphen 任何义务；若向 pyphen 上游提交修改，按 MPL 要求提供修改文件的
  可获取源码即可。
- **附加注意**：包内附带 Hunspell 连字符字典源自 LibreOffice 仓库
  （<https://git.libreoffice.org/dictionaries>），字典同系 GPL/LGPL/MPL 多许可——同样按
  MPL 口径处置；本项目未修改字典（"Dictionaries are not modified in this repository"，PyPI 页原文），
  再分发保留字典原样即可。

### 3.2 ⚠️ psycopg2-binary（LGPL-2.1+）

- **引入方式**：仅存在于审计时主仓开发 venv（历史 pip 安装残留）；**并非** `SQLAlchemy[asyncio]`
  extra 传递——该 extra 不拉取任何 driver（fresh `pip install -r backend/requirements.txt`
  实测 ≈84 包，PG driver 只有 asyncpg）。fresh 开源部署不会安装本包，该条目仅对
  开发 venv 的合规记账有意义。
- **许可事实**（依据）：<https://github.com/psycopg/psycopg2>（LGPL-2.1 起；C 扩展）。
- **判定（本项目是否满足条款）**：**满足**。本项目以 Python import 方式使用 psycopg2-binary
  （独立作品 + 动态链接等价物），LGPL-2.1 §6 对该场景仅要求：①随发行提供 LGPL 许可声明；
  ②允许用户替换该库。本项目未修改其源码、未静态链接、未将源码并入仓库，且 pip 分发形态
  天然满足"可替换"（`pip uninstall psycopg2-binary` 后 `pip install psycopg2` 即可）。
- **fork（再分发）如何满足条款**：
  1. 随发行物保留 psycopg2 的 LGPL 许可文本（dist-info 自带 LICENSE）；
  2. 在 `NOTICE`/依赖表标注「psycopg2-binary：LGPL-2.1+」；
  3. 保持依赖外置（`requirements.txt` 声明），**禁止**将 psycopg2 源码 vendor 进仓库。
- **PR（贡献）如何满足条款**：本项目代码不含 psycopg2 源码，常规提交无额外义务；
  需要改动其行为时走依赖升级或向上游提交，不得把其源码拷贝进仓库。
- **结论**：行业通用实践，风险低。

### 3.3 ✅注——多许可/缺元数据项（已人工核对，非风险）

| 包 | 判读 | 依据 |
|---|---|---|
| dompurify (npm) | `MPL-2.0 OR Apache-2.0` → 可选 Apache-2.0 | node_modules/dompurify/package.json `license` 字段（实测 `(MPL-2.0 OR Apache-2.0)`）+ <https://github.com/cure53/DOMPurify> |
| khroma (npm) | package.json 无 license 字段 → 核对 `LICENSE` 文件 = **MIT** | node_modules/khroma/LICENSE（本报告 npm_walk.py `[FILE:LICENSE]` 标记） |
| robust-predicates (npm) | 字段值 `Unlicense` = **公有领域**（Unlicensed 文本） | node_modules/robust-predicates/LICENSE（Unlicense 全文） |
| numpy (py) | 聚合许可，可整体按 **BSD-3-Clause** 分发 | <https://numpy.org/doc/stable/license.html> |
| cryptography (py) | `Apache-2.0 OR BSD-3-Clause` → 可选 Apache | <https://cryptography.io/en/latest/about.html> |
| certifi / tqdm (py) | MPL-2.0（certifi 单金文件；tqdm 双许可可选 MIT） | <https://github.com/certifi/python-certifi> / <https://github.com/tqdm/tqdm> |
| pillow (py) | MIT-CMU（PIL 许可） | <https://github.com/python-pillow/Pillow/blob/main/LICENSE> |
| matplotlib (py) | PSF/专用 permissive（BSD 系条款 + `misc/license/`） | <https://matplotlib.org/stable/users/project/license.html> |
| reportlab (py) | BSD + ReportLab 附录（permissive） | <https://www.reportlab.com/license/> |
| greenlet / typing_extensions (py) | MIT AND PSF-2.0 / PSF-2.0 | PyPI 元数据 |
| chardet (py) | 0BSD | <https://github.com/ankitrohatgi/chardet> |

> 注：本项目锁定的 dompurify 3.4.0 声明 `(MPL-2.0 OR Apache-2.0)` 双许可，分发时取 Apache-2.0
> 分支即可；升级 dompurify 版本时须重新核对 license 字段。

**判定（本项目是否满足条款）**：**满足**——以上各项均为 permissive，或双许可中可选
permissive 分支（dompurify/numpy/cryptography/tqdm 取 Apache-2.0/MIT/BSD 分支），不产生
再分发义务。

- **fork（再分发）如何满足条款**：无强制动作；随发行物保留各包自带许可文本即可
  （pip/npm 安装产物已含 LICENSE，无需手工复制），发布包附 `NOTICE` 汇总更佳。
- **PR（贡献）如何满足条款**：无额外动作；新增依赖时须经「维护建议 §1」CI 许可闸门
  检查（permissive 或可选项含 permissive 分支，并把所选分支加入 allow-only 白名单）。

---

## 四、维护建议

1. **CI 许可闸门**：构建/发布流水线加入正向白名单（推荐值）：
   ```bash
   pip-licenses --from=mixed --allow-only="MIT License;MIT;BSD License;BSD-3-Clause;BSD-2-Clause;Apache Software License;Apache-2.0;Python Software Foundation License;PSF-2.0;Mozilla Public License 2.0 (MPL 2.0);0BSD;MIT-CMU;3-Clause BSD License"
   ```
   （当前 129 包全部落在该白名单内；出现新增依赖时此命令非零退出即阻断合并。
   若未来引入双许可包，把实际选择的分支 license 加进 allow-only 并在 NOTICE 记录。）
   npm 侧用本报告 `npm_walk.py`（或等价 license-checker）守生产树。
2. **版本升级重扫**：`pip freeze` / `package-lock.json` hash 变化即重跑本流程
   （本报告全部命令可复现，证据在 `tests/license_audit/`）。
3. **许可文本归档**：发布包建议附 `NOTICE` + 依赖许可全文（REUSE 规范或 pip-licenses
   `--with-license-file` 产物），降低二次分发者负担。
4. **依赖树清洁度**：Python ⚠️ 面仅剩 pyphen（MPL 可接受）与 psycopg2-binary（LGPL 可接受）
   两个低危传递项；依赖树内无未使用孤儿包。

---

## 五、自托管 UI 字体（SIL OFL 1.1）

前端 UI 字体自 2026-08-27 起**全量本地化**：`index.html` 不再引用 Google Fonts
CDN（原 5 处外链含 noscript 回退），改为构建期随 `frontend/public/fonts/` 分发。

| 项 | Inter | Noto Sans SC |
|---|---|---|
| 版本（name table #5） | 4.001;git-66647c0bb | 2.004-H2;hotconv 1.0.118;makeotfexe 2.5.65603 |
| 版权（name table #0） | Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter) | (c) 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source' |
| 许可参考（name table #13/#14） | https://openfontlicense.org | http://scripts.sil.org/OFL |
| 许可 | SIL OFL 1.1 | SIL OFL 1.1 |
| 文件 | 7 × woff2（变量，wght 400–600，7 个 unicode-range 分片） | 101 × woff2（变量，101 片，含 CJK 大分片） |
| 大小 | 214 KB | 4,409 KB |
| 判定 | ✅ permissive 分支（OFL 允许与 Apache-2.0 软件捆绑再分发，义务 = 保留版权通知 + OFL 全文） | ✅ 同左；Noto 谱系承自 Adobe/Google Source Han Sans 项目，版权串以元数据为准 |

**证据与复现命令**：字体名表以 `fontTools`（venv 4.63.0）逐文件读取 `name` 表
（108/108 解析通过）；来源为 Google Fonts css2 端点（chrome UA）2026-08-27 抓取，
文件名 = `{Family}-{sha1(css2字体URL)前10位}.woff2`，与 `fonts.css` 一一对应
（324 条 @font-face / 0 缺失引用）。

**再分发义务（OFL §2，已满足）**：`frontend/public/fonts/FONTS_LICENSE.md` 随包分发
两款字体的版权通知 + OFL-1.1 全文；各 woff2 另内嵌机器可读许可字段。
**保留名（OFL §3）**：'Source' 为 Adobe 保留字体名，修改 Noto Sans SC 派生版本时
不得以其命名。

**维护**：换字重/升级字体须整体重抓并同步刷新 `FONTS_LICENSE.md`（流程见
`frontend/public/fonts/README.md`）；删除单个分片会造成 unicode-range 覆盖残缺。
