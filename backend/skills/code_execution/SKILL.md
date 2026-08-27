<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

---
name: code_execution
description: 生成可下载文件（Excel/PPT/Word/CSV/图片/PDF除外）或执行复杂计算/数据处理/编程绘图
category: builtin
---

# code_execution — 代码执行

## 适用场景
- 用户明确要求生成可下载文件（Excel、PPT、Word、CSV、图片等，**PDF 除外**）
- 复杂计算 / 数据处理 / 编程绘图（需要渲染成图片的统计图）
- 无法通过文字直接完成的编程任务

## 用法
调用 `execute_code`，传入任务的完整描述，系统会生成并执行 Python 代码。

## 规则
1. **禁止**用代码生成本可直接用 Markdown / Mermaid 描述的流程图、示意图、思维导图。这些直接在回答中用文字描述。
2. **禁止**在 `execute_code` 里使用 `subprocess` 或 `os.system`。沙箱禁止 subprocess。需要执行外部 CLI 工具（xelatex、pandoc、ffmpeg、gcc 等）时改用 `terminal` 工具。
3. 常用库：`python-pptx`、`openpyxl`、`python-docx`、`reportlab`、`matplotlib`、`numpy`、`pandas`、`Pillow`。
4. 输出文件保存到 workspace，返回路径供前端展示下载卡片。
5. **PDF 导出例外**：用户要求导出 PDF 时，必须使用 `pdf_export` 工具，禁止用 `execute_code` 或 `terminal` 自行生成 PDF。`pdf_export` 调用与系统"导出 PDF"按钮相同的渲染接口，支持 Mermaid、LaTeX 等复杂 Markdown 元素。
6. 代码执行结果会自动返回，请基于输出向用户解释结果。
7. 不要只用文字描述数据或表格——用户要求生成文件时必须生成实际的文件。
