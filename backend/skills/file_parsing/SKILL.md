<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

---
name: file_parsing
description: 解析用户上传的文件（[file-ref:文件名] 标记），完全自主决定解析方式：技能优先、扩展名判断、web_search 辅助、execute_code/skill_run_script/terminal 执行
category: builtin
---

# file_parsing — 上传文件解析（完全自主）

## 适用场景
用户消息中包含 `[file-ref:文件名]` 标记（用户上传了文件，文件已保存在系统路径中）时需要解析文件内容。

## 处理流程
1. 如果用户明确要求使用某个 skill（例如消息中出现 `[skill:skill_name]` 或显式说"用 xlsx 技能解析"），直接调用 `skill_view(name=...)` 加载该 skill 并遵循其指令，不要自行搜索其他方式。
2. 如果没有用户指定的 skill，先查看文件扩展名和文件路径，形成对文件类型的初步判断。
3. 调用 `skill_manage(action='list')` 查看可用技能，寻找名称或描述与文件类型相关的技能（如 xlsx_manipulation、docx_manipulation、pptx_manipulation 等）。如果存在相关技能，使用 `skill_view(name=...)` 加载完整指令并严格遵循。
4. 如果没有匹配技能，或技能不足以完成任务，使用 `web_search` 搜索该文件类型的最佳解析方式（例如：'python parse .xlsx file'、'extract text from pdf python library'）。
5. 根据获得的信息，选择合适工具：
   - 使用 `execute_code` 编写 Python 代码读取并解析文件。execute_code 可以访问用户工作区中的文件路径。
   - 使用 `skill_run_script` 执行已加载技能中的可执行脚本。
   - 使用 `terminal` 调用外部命令行工具（如 pandoc、ffmpeg、xelatex 等）。
6. 如果执行代码时缺少第三方库，先用 `terminal` 执行 `pip install 包名` 安装，然后再次尝试执行。允许安装任何必要的 PyPI 包，但禁止使用 --break-system-packages。
7. 解析完成后，基于文件内容回答用户的问题。

## 规则
0. 许可合规：本部署的依赖画像为纯 permissive（MIT/BSD/Apache/PSF）。若你在第 6 步自主 `pip install` 引入 PDF 解析类库（如 pymupdf/fitz 家族、marker 等 AGPL/GPL 许可库），其许可义务由部署方自行承担——引用前先确认该库 license 与部署场景兼容；优先选择环境已预装的 permissive 库（pdfplumber / pdfminer.six 等）。
1. 禁止在系统提示或任何工具结果中硬编码"某类型文件必须用某库解析"。所有解析方式必须由你根据当前可用技能、搜索结果和代码执行能力自主决定。
2. 必须完全自主地判断文件应如何解析，禁止依赖任何硬编码的文件类型映射。
