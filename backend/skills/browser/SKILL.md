<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

---
name: browser
description: 浏览指定网页内容（用户给出 URL、需要深入阅读网页、搜索结果需要展开）
category: builtin
---

# browser — 网页浏览

## 适用场景
- 用户给出 URL，需要读取网页内容
- 搜索结果需要展开深入阅读
- 需要获取网页中的图片、表格、详细文字

## 用法
调用 `browser(url=...)`。

## 规则
1. 分页浏览最多 **5 页**，避免无限翻页。
2. 网页快照中会包含图片的 `src` 信息。当你需要向用户展示图片时，可在回答中使用 Markdown 图片语法 `![描述](图片URL)` 直接显示。
3. 显示图片时，必须在图片语法后紧跟来源引用标号，格式为 `![描述](图片URL) [N]`，其中 `[N]` 是该图片来源页面对应的搜索结果序号。
4. 禁止单独编写"来源：XXX [N]"等文字行——引用信息应通过 `[N]` 角标直接关联图片。
5. 仅在用户给出明确 URL 或搜索结果需要深入阅读时调用，不要用 browser 替代 web_search 做宽泛探索。
