---
name: browser
description: 浏览指定网页内容（用户给出 URL、需要深入阅读网页、搜索结果需要展开）
category: builtin
---

<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

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

## Canvas / SPA 就绪（交互浏览器，2026-11-03）

`browser_navigate(wait_selector="canvas")` 只证明画布出现，**不证明应用已就绪**。
几乎所有 canvas 应用都会先渲染外壳/加载页，再异步加载真正的文档（数秒到十几秒
很常见）；只等 canvas 或只等 `networkidle` 会把加载态当成可用态。

`browser_navigate` 对“大画布”页面（不要求 `wait_selector` 含 canvas）自动做
稳定性观察：至少观察 5 秒，并在 URL / title / DOM 变更 / `readyState` / canvas
尺寸连续 1000ms 不变后才返回（最长 15 秒）。返回字段：
- `settled: true|false` — 页面在观察窗口内是否稳定；**true 不等于目标文档已就绪**；
- `observed_change: true|false` — 窗口内是否观察到过状态变化；canvas 页面为
  `false` 表示从第一次采样起就没变过，可能仍是静态加载页；
- `settle_mode: canvas_heuristic|heuristic|wait_js`；
- `warnings` — 诊断信息（例如“未观察到状态变化”“wait_selector 未命中”），
  不全是硬错误；按条目阅读。

`settled=false` 表示页面一直在变或观测失败：**不得**把它当作就绪页继续操作。
`settled=true` 且 `observed_change=false` 时，先核对文档身份（标题/路由/目标元素）
或改用 `wait_js`，不要因为“稳定”就认为目标文档已加载。

### 通用操作建议

1. **已知应用优先用 `wait_js` 等目标文档**，而不是等“canvas 存在”或“app 对象存在”
   （加载页同样满足这些弱条件）。谓词应指向目标文档独有的信号：
   ```text
   browser_navigate(
     url="...",
     wait_selector="canvas",
     wait_js="() => !!document.querySelector('[data-app-ready=true]')"
   )
   ```
   其他形状：`() => location.hash === '#/editor'`、
   `() => !document.querySelector('.loading-overlay')`、
   `() => window.__APP_READY__ === true`。
2. **没有明确就绪信号时**：导航后先 `browser_screenshot`，看是否仍是加载页/空画布；
   必要时再 `browser_snapshot(full=true)` 或截图对比，稳定后再操作。不要凭一次
   snapshot 就认为应用已加载。
3. **canvas 输入**用 `browser_mouse` 的视口坐标（先截图定位），键盘输入用
   `browser_press` / `browser_type(ref="_focused", ...)`；canvas 内部没有可点击的
   DOM ref，不要等 ref。
4. **多文档/多工作区应用**（编辑器、节点图、设计工具）：启动时可能自动打开
   默认/上次文档。操作前确认当前文档身份（标题、路由、URL hash、状态栏），
   需要时显式新建/打开目标文档；不要把启动默认文档的节点/元素 id 当成目标文档的 id。
5. 若目标应用是 ComfyUI（节点工作流、Qwen-Image/MiniMax 等），另见 `comfyui` 技能。

