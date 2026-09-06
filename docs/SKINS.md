<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# 皮肤设计与接入指南

> 版本：4 · 日期：2026-08-28 · 适用系统：Weave Thinker
> 相关：design/FLOW_DESIGN_skin_system.html · design/BACKEND_DESIGN_skin_api.html · design/PAGE_DESIGN_skin_settings.html · docs/PLAN.md
> v4 变更：wave-17 sidebar 二级菜单/分组编辑卡/swipe 条/移动端笔记页/笔记侧栏左划 皮肤补全（22 判据）· wave-17 swipe 令牌族 7 枚（§3.4）· 移动端卡裁决更新（§8：mono/ink 无圆角遮罩，帧上移行承载）· 注释 `*/` 截断陷阱（§5 红线 10）· 测试清单增 skin_wave17.spec.ts（31 例）
> v3 变更：深色全表面对比度修复（wave-12）· 登录页皮肤/明暗控件（wave-14）· 五盲区+四盲区皮肤化（wave-15/16）· 新令牌族 `--logo-*`/`--formula-*`/`--info·success-tint`（wave-13/15/16）· backlog 与测试清单刷新
> v2 变更：六界面补全覆盖（登录/系统设置/笔记引用/思考菜单/笔记本卡片/笔记卡片）· 皮肤上传接口 · 解耦审计结论 · 新令牌族

---

## 1. 皮肤系统概述

界面皮肤与系统完全解耦：**CSS 设计令牌（Design Tokens）+ 皮肤注册表 + 组件级覆写**，三套内置皮肤任选，另支持开发者经 API 上传自有皮肤（第 6 节）。内置三皮肤自 wave-12 起完成深色全表面对比度审计（正文 ≥4.5:1 逐项过），新表面必须 light/dark 成对交付（第 5 节红线 7）。登录页带皮肤/明暗控件（wave-14，三皮肤 × 明暗 6 组合均生效），未登录可选皮、登录后同步。

- **双轴正交**：皮肤（verdant-flat / ink-paper / mono-brutal / …上传皮肤）× 明暗（light / dark）
- **DOM 载体**：`<html data-skin="<id>">` + `<html data-theme="dark">`（light 时移除属性）
- **持久化**：localStorage `wt-skin` + 后端 `users.ui_preferences`（跨设备同步，设备本地优先）
- **零运行时成本（内置皮肤）**：纯 CSS 变量切换，无 JS 重渲染；上传皮肤经认证 fetch → Blob `<link>` 注入
- **级联纪律**：皮肤文件组件级覆写一律 `html[data-skin="<id>"] .<class>` 前缀（特异性 0,2,1 / 0,3,1，稳胜 scoped `[data-v]` 的 0,2,0 / 0,3,0，与注入顺序无关）

## 2. 内置皮肤目录

| id | 名称 | 设计范式 | 气质 |
|---|---|---|---|
| `verdant-flat` | 青野平面（默认） | 浮动玻璃卡（大圆角 + 软影 + hover 上浮） | 苔绿画布上的扁平自然系 |
| `ink-paper` | 墨韵纸间 | 贴边发丝线（1px `--skin-line` + 小圆角 + 宋体标题） | 宣纸暖底、朱砂点墨的文房气质 |
| `mono-brutal` | 黑白构成 | 硬边直角（2–3px 黑框 + 偏移硬影 + 900 黑体标题） | 高对比黑白构成，橙色锐利点缀 |

皮肤目录经 `GET /api/skins` 公开暴露（含 `token_contract_version`）。前端注册表 `frontend/src/config/skins.ts` 与后端 `backend/app/api/skins.py SKIN_CATALOG` **id 必须一致**（`tests/api/test_skin_api.py` + `frontend/e2e/skin_system.spec.ts` 双向断言）。

## 3. 令牌契约（Token Contract）

基线（`:root` / `[data-theme="dark"]`，`frontend/src/styles/main.css`）= verdant-flat。皮肤在 `[data-skin="<id>"]`（light）与 `[data-skin="<id>"][data-theme="dark"]`（dark，**必须完整定义基础令牌**、文件内放最后）两组块中覆写。

### 3.1 基础颜色令牌

```css
:root {
  /* 品牌色 */
  --color-primary; --color-primary-dark; --color-secondary; --color-accent;
  /* 背景与表面 */
  --color-bg; --color-bg-secondary; --color-sidebar; --color-code-bg; --color-white;
  /* 文字 */
  --color-text; --color-text-light; --color-text-primary; --color-text-secondary;
  /* 状态色 */
  --color-error; --color-danger; --color-success; --color-warning; --color-info;
  /* 交互 */
  --color-border; --color-hover; --color-user-bubble;
  /* 表面/边框/阴影 */
  --surface-panel-strong; --surface-panel-subtle; --surface-input; --surface-workbench;
  --panel-border; --panel-border-strong; --frame-shadow; --panel-shadow; --glass-blur;
  --shadow-sm; --shadow-md; --shadow-lg; --scrollbar-thumb; --scrollbar-thumb-hover;
  /* 圆角与字体 */
  --radius-sm; --radius-md; --radius-lg; --radius-xl; --radius-pill;
  --font-main; --font-mono;
  /* 代码高亮 */
  --code-block-bg; --code-block-header-bg; --code-block-text; --code-keyword; --code-string;
  --code-comment; --code-number; --code-function; --code-variable;
}
```

### 3.2 组件对齐令牌（皮肤可覆写，第 1、2 波建立）

```css
:root {
  /* Sidebar 卡片化 */ --sidebar-card-bg; --sidebar-card-radius; --sidebar-card-shadow; --sidebar-card-border;
  /* 导航 pill 容器 */ --nav-pill-bg; --nav-pill-radius; --nav-pill-padding; --nav-pill-gap;
  /* 消息气泡 */ --msg-bubble-radius; --msg-bubble-bl; --msg-bubble-br; --msg-bubble-border; --msg-bubble-shadow; --msg-user-bg; --msg-assistant-bg;
  /* 输入区 */ --input-container-radius; --input-container-min-height; --input-container-border; --input-container-shadow-focus;
  /* 思考块 */ --reasoning-border-left;
  /* 顶部栏卡片 */ --chat-topbar-bg; --chat-topbar-radius; --chat-topbar-border; --chat-topbar-shadow;
  --btn-hover-lift;
  /* 弹窗卡片 */ --dialog-radius; --dialog-shadow;
}
```

### 3.3 共享交互态令牌（wave-11 新增 —— 六界面/菜单/卡片家族的解耦层）

| 令牌 | 用途 | verdant-flat | ink-paper | mono-brutal |
|---|---|---|---|---|
| `--overlay-scrim` | 对话框遮罩底色 | `rgba(24,18,14,.35)` / dark `rgba(0,0,0,.62)` | `rgba(42,37,33,.30)` / `rgba(0,0,0,.55)` | `rgba(0,0,0,.5)` / `rgba(0,0,0,.6)` |
| `--focus-ring-color` | 输入 focus 环色（替代历史硬编码蓝/绿） | `16% primary` / `24%` | `10% primary` / `16%` | `18% primary` / `24%` |
| `--primary-tint` / `--primary-tint-strong` | hover/active 主色淡底（8%/12% color-mix，随 --color-primary 自动适配明暗） | 基线表达式 | 同（继承） | 同（继承） |
| `--danger-tint` / `--warning-tint` | 错误/警告条底色（8%/10%，dark 12%/14%） | 基线 | 同 | 同 |
| `--primary-glow` | 主按钮 hover 光晕 | `0 8px 20px 25% primary` / dark 黑影 | 同 | 同 |
| `--action-edit-bg` / `--swipe-move-bg` / `--success-strong` | 滑动操作条（编辑 #6b7280 / 移动 #f59e0b / 设默认激活 #2e7d32） | 基线 | 同 | 同 |
| `--menu-radius` / `--menu-border` / `--menu-shadow` | 弹出菜单家族（思考/更多/技能/上下文菜单容器） | `16px` / `1px var(--panel-border)` / `0 4px 16px 10% text` | `4px` / `1px var(--skin-line)` / `0 10px 34px rgba(60,50,30,.14)` | `0` / `2px var(--skin-line)` / `5px 5px 0 var(--skin-line)` |
| `--stack-card-radius` / `--stack-card-shadow` / `--stack-card-shadow-hover` | 桌面 wallet-stack 卡片（笔记本/笔记 3 列卡） | `18px` / 绿调双层软影 | `6px` / 纸调软影 | `0` / `4px 4px 0` 硬影 |
| `--stack-hover-lift` / `--stack-overlap` | stack hover 抬升 / 卡叠压 | `-18px` / `-40px` | `-14px` / `-40px` | `-10px` / `-40px` |

**解耦规则**：组件 scoped 样式只准消费令牌（`var(--…)`），不得出现具体色值；新增「组件家族共享的视觉语义」时必须先落一枚令牌再接入组件（否则上传皮肤无法覆盖该语义）。

### 3.4 wave-13 ~ wave-17 新增令牌族

| 令牌 | 用途 | 内置三皮语言 |
|---|---|---|
| `--logo-*` ×16（tile-1/2 · ring(-sw) · dash(-sw) · tick-n(-sw) · tick-se(-sw) · letter · letter-font · letter-weight · needle-n · needle-s · dot） | Logo 图标令牌化（`LogoIcon.vue` 全量 `var()`，fallback=青野原值零回归，**零 JS 分支**）；登录/空态/侧栏三处生效 | 墨韵=奶油 tile + 墨环 #2a2521 + 金虚线 #c9a86a + 朱砂北针 #b23b2e + Georgia 字母（明暗恒值）；黑白=白 tile + 可见 #111 描边 + 黑环 + 灰虚线 + 橙针 #D64008 + 黄心点 #DBAF00 + Arial Black；青野=基线 |
| `--info-tint` / `--info-tint-strong` / `--success-tint` / `--success-tint-strong`（wave-15） | 信息/成功语义淡底：死磕状态条、附件卡、盘问卡、后台任务面板；light/dark `color-mix` 12/22% · 14/24% 随主题自动加深 | 基线表达式，明暗自动适配 |
| `--formula-bg` / `--formula-border` / `--formula-radius`（wave-16） | 公式/Mermaid 查看卡：上下文无关全局规则（`main.css` 800 行起，消息内公式走 `.math-editable`/`.math-rendered-content`、不在 `.stream-markdown` 下——DOM 路径实证）；NoteEditor 数学/mermaid 对话框同族 | 青野 14px 软影 / 墨韵 4px 发丝 + serif 斜体提示 / 黑白 0 直角 + 硬边 |
| `--swipe-rename-bg` / `--swipe-export-bg` / `--swipe-delete-bg` / `--swipe-save-note-bg` / `--swipe-move-group-bg` / `--swipe-default-bg` / `--swipe-default-active-bg`（wave-17） | 左划动作条七色板：会话行（导出/标题/分组/笔记/删除）、笔记行（重命名/移动/导出/删除）、笔记本行（导出/名称/设默认/删除）、笔记侧栏行（重命名/移动/删除）共用。基线值引用 `--color-*` 状态令牌（明暗自动适配）；与既有 `--swipe-move-bg` / `--action-edit-bg` / `--success-strong` 同族。三内置主题 light+dark 逐钮覆写完整色板 + 形状语言（黑白钮间 2px 硬边分隔+800 字重 / 墨韵 1px 发丝分隔 / 青野无分隔） | 基线表达式，主题文件逐钮 light+dark 整段覆写 |

## 4. 六界面皮肤覆盖矩阵（wave-11 完成面，wave-14 增补登录控件行）

| 界面 | 组件 | verdant-flat | ink-paper | mono-brutal |
|---|---|---|---|---|
| 登录 | `.login-card` `.login-title` `.login-btn` 表单（wave-13 起墨韵=纸卡金调双框+seal-mini+水墨远山竖诗场景、黑白=3px 硬边+8px 偏移硬影+LOGIN/01 角标+巨型描边场景，`body:has(.login-page)` 门控） | 24px 白卡软影 + 渐变绿按钮 | 6px 发丝卡 + 4px 输入/按钮 + 宋体标题 | 0 直角 + 3px 硬边 + 5px 偏移硬影 + 900 黑体标题 |
| 登录页控件 | `.login-prefs`（皮肤 popover + 明暗钮，wave-14） | 全令牌零硬编码（`--surface-panel-strong`/`--panel-border`/`--radius-md`/`--primary-tint`），6 组合自动适配，无逐皮覆写 | 同 | 同 |
| 系统设置 | `.system-settings-overlay` `.system-settings-card` `.settings-tab` `.skin-card` `.mode-switch` | 24px dialog + 绿 accent tab | 暖 scrim + 6px 发丝卡 + 宋体 tab + 2px active 线 + 4px 发丝皮肤卡 | 黑 scrim + 0/3px 偏移硬影 + AA 900 tab + 3px 硬边皮肤卡 + 反色 active |
| 笔记引用 | `.note-picker-overlay` `.note-picker-modal` `.note-option` `.search-input` 按钮 | 24px 玻璃卡 + 11-14px item | 6px 纸卡 + 宋体 `.picker-title` + 发丝 hover + 4px 按钮 | 0+3px 硬边 + AA 标题 + 直角反色 hover + 2px 硬边按钮 |
| 思考模式菜单 | `.reasoning-menu` `.reasoning-menu-item`（同 `.more-dropdown-teleport`/`.skill-popup`/`.context-menu` 家族） | 16px 玻璃卡 + 11px item | 4px 发丝纸卡 + 宋体标题 ls2 + 3px item + 朱红淡底 active | 0+2px 硬边 + 5px 偏移硬影 + 900 标题 + 直角反色 active |
| 笔记本卡片（桌面 3 列 stack） | `.stack-column > .notebook-row` `.notebook-item` `.notebook-name` | 18px + 绿调软影 + -18px 抬升 | 6px + 纸调软影 + 发丝内卡 + -14px | 0 + 2px 硬边内卡 + 4px 偏移硬影 + -10px + 800 标题 |
| 笔记卡片（桌面 3 列 stack） | `.stack-column > .note-row` `.note-item` `.note-title` | 同上家族 | 同上家族 | 同上家族 |
| sidebar 二级菜单（弹层家族） | `.conversation-menu` `.np-context-menu` `.move-group-menu` `.tools-dropdown` + `.menu-item`/`.tools-item`（wave-17 起容器走 `--menu-*` 令牌自动适配） | 16px 玻璃卡 + primary-tint hover（dark #262b26 成对） | 4px 发丝卡 + 宋体节标题 + 纸色 hover（dark #30281c）+ 朱砂淡底 active（dark 16% 成对） | 0 + 2px 硬边 + 5px 偏移硬影 + 反色 hover + 700 字重 |
| 分组编辑卡（对话框家族） | `.modal-overlay` `.modal-content(.group-dialog/.delete-dialog)` `.modal-title` `.modal-btn` `.form-group input`（wave-17 起走 `--overlay-scrim`/`--dialog-*`/`--surface-input` 令牌自动适配） | 24px + 软影 + scrim rgba(24,18,14,.35) | 6px 发丝 + 宋体标题 ls.06em + scrim rgba(42,37,33,.30) | 3px 硬边 + 5px 偏移影 + 900 标题 + 直角 2px 边按钮 + scrim rgba(0,0,0,.5) |
| 会话左划条（移动端） | `.conversation-actions .swipe-action`（wave-17：5 钮色板令牌化 `--swipe-*-bg`，移动钮内容 svg 13/字 9/gap 2 防溢出） | 无分隔 + 完整 light/dark 色板 | 1px 发丝分隔 + 完整色板 | 2px 硬边分隔 + 800 字重 + 完整色板 |
| 移动端笔记页 | NotebooksList/NotesList `.page-header` 按钮（移动/导出/删除/上传/新建/返回）`.search-input-wrapper` `.selection-bar` 按钮 `.page-btn` 新建弹窗（wave-17，<768px 主题媒体块；add-btn 等渐变已改纯色主令牌） | 基线令牌语言（radius-sm/md 继承） | 4px 发丝按钮族 + 方卡（radius 0，wave-17b 无圆角遮罩） | 方钮 2px 硬边 + 卡帧上移行承载（wave-17b：行=radius 0 + 2px 边 + 3px 3px 0 影 + 面，卡内无边无影） |
| 笔记侧栏左划（移动端） | Sidebar `.np-swipe-wrap .np-swipe-actions .np-swipe-action`（wave-17：笔记本行=重命名/删除、笔记行=重命名/移动/删除，触控专用，桌面零变化；np 点钮移动端隐藏对齐 agent 交互） | 28px 圆玻璃条（继承 `--shell-workbench-radius`） | 方条（radius 0）+ 发丝分隔 | 方条（radius 0）+ 2px 硬边分隔 + 800 字重 |

皮肤文件位置：`frontend/src/styles/themes/<id>.css`（三个内置皮肤各 ~1700–2000 行，light 令牌块 → dark 令牌块 → 组件级覆写 → 移动端 media 豁免 → 夜晚态微调）。

**登录控件持久化（wave-14）**：`.login-prefs` 复用 `useSkinStore`（allSkins/setSkin/toggleMode 涟漪）；未登录 PUT 偏好 401 静默早退（本地先生效），登录后 `syncFromServer` 统一。ARIA：disclosure 模式 `aria-expanded`/`aria-pressed`，Escape 关闭+焦点回触发钮，外点监听 capture 相位。

## 5. 皮肤文件结构（开发者模板）

```css
/* 皮肤：示例 (example-skin) —— id 必须与文件名一致 */

[data-skin="example-skin"] {
  /* 1. 基础令牌（第 3.1 节全集，light 值必须完整） */
  --color-primary: #2f6f8f;
  /* …… 至少覆盖：primary 族 / bg 族 / surface 族 / text 族 / 状态色 /
        border/hover / panel-border / shadow 三档 / scrollbar；
        组件对齐令牌（3.2/3.3）按自家语言挑选覆写，未覆写=继承基线 */
  color-scheme: light;
}

[data-skin="example-skin"][data-theme="dark"] {
  /* 2. dark 令牌——必须完整重新定义基础令牌，放在文件后段 */
  --color-primary: #5aa8c8;
  /* …… */
  color-scheme: dark;
}

/* 3. 组件级覆写（可选；语言差异处才写，一律 html 前缀提级） */
html[data-skin="example-skin"] .reasoning-menu {
  border-radius: 8px;
  border: 1px solid var(--panel-border);
}
```

**红线（血泪教训，见 memory/fix_skin_system_decoupling.md）：**
1. **笔记正文 = 用户内容**：`.zen-note-content` 及其排版任何属性（颜色/字体/字号/行高/引用/标题）一律不得覆写；列表卡标题属 UI 层可覆写。
2. 组件覆写必须 `html[data-skin="<id>"] .x` 前缀（0,2,1）；裸 `[data-skin] .x`（0,1,1）压不过 scoped。
3. 移动端覆写必须防压基线：桌面规则（如 `position:relative`）会压过移动基线（`position:fixed`）→ 移动豁免块补 `!important`。
4. 每皮肤必须同时定义 light + dark 两组令牌；dark 块选择器双属性优先级最高、放最后。
5. 对比度：正文 ≥ 4.5:1；主按钮白字可读（参考 `design/mockups/tests/check_contrast.mjs`）。wave-12 起**深色全表面审计**为纪律：`tests/w12_capture.py` 自建 DOM 审计器（getComputedStyle 全树行走含祖先链复合背景/gradient 首停靠色 + icon-only 探针 + disabled 按 WCAG 1.4.11 记豁免），改皮肤或加 UI 表面后手工跑一遍。
6. **级联 hover 陷阱（wave-12 血泪）**：组件 scoped `.x[data-v]:hover:not(:disabled)` 特异性 (0,5,0) 压皮肤 (0,4,1)——**鼠标划过即触发**（静态探针全绿、流式中翻车）。每个 :hover/:active 皮肤覆写都要对组件 scoped 同族选择器算特异性（`[data-v]`+伪类常数在 (0,4,0)~(0,5,0)），压不过时用重复类名 `(0,5,1)+` 锁死全部互动态。
7. **dark 成对（wave-12 起）**：wave-5~11 八轮的病根=组件级覆写只写浅色硬编码、dark 只逐点名补 → 系统性浅底浅字（assistmenu 45 fail 基线）。新表面必须 light/dark 成对交付；只补用户点名项=教训（wave-13 首轮 3 hover → 二报同族 17 处），收到点报立刻 grep 同色族全补。
8. **`--color-border` 禁作 hover 底色（wave-16）**：mono 下 `--color-border`=#111 → 黑底黑字（代码窗复制钮用户现场）；hover 底色用专门语义或 `color-mix(in srgb, var(--color-text) 12%, var(--color-hover))` 全皮自适应（实测 ≈12.4:1）。
9. **颜色探针格式（wave-16）**：Chromium 对 color-mix 结果返回 `color(srgb R G B)`（0-1 分量），探针正则必须双格式；对比公式必须 max/min（`0.08` 与 `1/12.4` 互为倒数，方向写反=假败）。
10. **注释禁 `*/` 字面序列（wave-17）**：`/* … --menu-*/--dialog-* … */` 这类在注释里写令牌族名的写法，中间 `*/` 会**提前终止注释** → esbuild "Unexpected *" 警告 + 垃圾文本入样式表（可能吞掉紧跟的首条规则）。令牌族名改用 `--menu- 系列 / --dialog- 系列` 等不含 `*/` 的措辞。

## 6. REST API

### 内置与偏好

- `GET /api/skins`（公开）→ `{token_contract_version, default_skin, skins:[{id,name,description,is_default,modes}]}`
- `GET /api/users/me/preferences`（登录）→ `{skin_id}`（未设置过 → `verdant-flat`；**上传皮肤 id 亦合法回读**）
- `PUT /api/users/me/preferences`（登录）→ 合法域 = **内置 ∪ 本人上传**；非法 → 400 `unknown skin_id: …`
- 上传皮肤的 CSS 原文不再走公共静态路径，见下 `GET /api/skins/{id}/css`（认证 fetch → 前端 Blob 注入）。

### 上传皮肤（wave-11，design/BACKEND_DESIGN_skin_api.html §6）

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| POST | `/api/skins/upload` | Bearer | multipart：`file`(.css,≤300KB) + `name`? + `description`?；**id=文件名 stem**；同名 upsert |
| GET | `/api/skins/mine` | Bearer | 本人上传列表 |
| GET | `/api/skins/{id}/css` | Bearer | 本人皮肤 CSS 原文（`text/css`）；内置/他人/不存在 → 404 |
| DELETE | `/api/skins/{id}` | Bearer | 仅本人；若为当前偏好顺带复位默认 |

**校验红线（POST 顺序执行，失败 400/409）：**
1. 文件名 `.css` 且非空 — `css file required`
2. ≤ 300,000 字节 — `skin css too large`
3. id 匹配 `^[a-z0-9][a-z0-9-]{0,49}$` — `invalid skin id`
4. 不得占用内置 id — `409 reserved builtin skin id`
5. UTF-8 可解码 — `decode`
6. 必含锚点 `[data-skin="<id>"]` — `missing anchor`
7. 大括号配平 — `unbalanced braces`
8. 禁 外部 `@import http…`、`expression(`、`javascript:`、`<` 字符（`>` 是合法子代选择器）— `forbidden in skin css`

**存储**：`backend/skins_custom/{user_id}/{skin_id}/{skin.css, manifest.json}`（运行时目录，gitignore）。manifest：`{id,name,description,size,sha256,uploaded_at,token_contract_version}`。

**信任模型**：上传皮肤是**可信开发者**通道——CSS 能力等同内置皮肤（令牌 + 组件级覆写），校验是格式护栏 + 权限边界（per-user 目录 / owner-only 端点 / owner-only 偏好域），**不做逐声明沙箱**。请只上传自己审查过、自己信任的 CSS。

## 7. 前端运行时机制（上传皮肤）

- 注册表合并：`stores/skin.ts` — `allSkins = SKIN_REGISTRY + uploadedSkins`（SkinPanel 卡片渲染源；`source: 'builtin'|'uploaded'`，上传卡带「自定义」徽章 + hover 删除钮）。
- 登录后 `Sidebar.onMounted → loadUploaded() → syncFromServer()`：拉取本人皮肤清单与 CSS 原文（内存缓存），本地 `wt-skin` 若指向本人皮肤 → `ensureSkinCss(id)` 装 `<link id="wt-skin-css-<id>" href="blob:…">`（幂等）；指向已删/他人皮肤 → 回退 `verdant-flat` 并 PUT。
- 启动零闪烁不变式：`App.vue` 同步 `initFromStorage` 先设 `data-skin`（未知 id 暂以基线渲染），上传皮肤样式待登录往返补装；内置皮肤无此窗口。
- 预览条：上传皮肤从 CSS 文本解析 light 锚点块前五个语义令牌作展示数据（`previewFromCss`），解析失败兜底基线色。
- 删除：撤 link + `URL.revokeObjectURL` + 注册表移出；当前在使用 → 回退默认。

## 8. 解耦审计（wave-11 基线，backlog 更新至 wave-15/16）

`tests/skin_audit.py`（输出 `tests/skin_audit_report.md`，CI 化前可手工跑）：

- **红线 1**：全部组件 `.vue` 的 scoped style 中 `[data-skin` 选择器 = **0**（皮肤知识单点存于 `styles/themes/*.css` + 运行时注册表）✅
- **红线 2**：JS 皮肤-id 条件分支（白名单 `stores/skin.ts` `config/skins.ts` `api/skins.ts` `index.html` 预引导外）= **0** ✅
- **余量 backlog（非阻断，历史遗留硬编码色，wave-15/16 后刷新）**：ChatArea 40 · Sidebar 36 · ChatInput 34 · VoiceChat 31 · MemoryPanel 26 · SkillsPanel 25 · TaskProgress 25 · NoteEditor 24 · …（全表 `tests/skin_audit_report.md` §3，重跑 `python3 tests/skin_audit.py` 再生）。对比 wave-11 口径：BackgroundTaskPanel 53→10（后台任务面板 Material 色 28 处令牌化）、ChatArea 107→40（死磕状态条/盘问卡/附件卡 54 处令牌化）。这些色带在上传皮肤下仍保持青野基调，列入后续波次 token 化。

**判定**：内置三套皮肤与上传皮肤对本节六界面 + 第 3 节全部令牌面，均可经「令牌覆写 + 组件覆写」完成样式替换，无单独写死路径；已知例外 = backlog 色带（上表）。

*移动端列表卡裁决更新（wave-17b，2026-08-28，用户裁决推翻 wave-11 在案状态）*：移动端（<768px）列表单列卡不再三皮同基线——黑白构成无圆角遮罩（radius 0；2px 硬边 + 3px 3px 0 偏移影 + 面令牌由**行**容器 `.note-row`/`.notebook-row` 承载——行 `overflow:hidden` 裁内容不裁自身 box-shadow，卡内 `.note-item`/`.notebook-item` 无边无影透明底）；墨韵纸间方卡（行+卡 radius 0，发丝边保留）；青野平面保持基线 `--radius-md` 圆玻璃。语言载体=主题文件 `@media (max-width:767px)` 块（上传皮肤同路径生效）。遗留对账：墨韵/青野移动卡 hover 纸影被行 overflow 裁成视觉死规则（mono 已显式置 none），清理排后续波次。

## 9. 开发者接入步骤（上传路线）

1. 新建 `my-skin.css`（工作目录任意处），按第 5 节模板：锚点 `[data-skin="my-skin"]`（**与文件名一致**）+ light/dark 令牌全集 + 所需组件覆写。
2. 系统设置 → 皮肤选择 → 「上传皮肤」→ 选文件 → 名称/描述 → 上传。成功 toast 后**自动应用**，卡片出现「自定义」徽章。
3. 验证：明暗切换均正常；第 4 节六界面 + 聊天主区对照三内置皮肤检查无破版；`data-skin` 刷新后保持。
4. 修改/重传同名文件 = 覆盖（幂等）；不要 = 新文件名。
5. 不想用了：卡片 hover 删除。

**最小可用皮肤**（只覆写基色，其余全部继承基线语言）：

```css
[data-skin="mymind"] { --color-primary:#3b6ea5; --color-primary-dark:#2f5a8a; --color-bg:#eef3f8; --color-sidebar:#e5edf5; --color-text:#1f2d3d; --color-text-light:#5b6b7d; --color-border:#d4dfeb; --color-hover:#e3ecf5; --surface-panel-strong:#ffffff; --surface-panel-subtle:#f4f8fb; --panel-border:rgba(59,110,165,.18); color-scheme: light; }
[data-skin="mymind"][data-theme="dark"] { --color-primary:#6ea8dc; --color-primary-dark:#6ea8dc; --color-bg:#10151b; --color-sidebar:#151c24; --color-text:#dce6f0; --color-text-light:#8fa2b5; --color-border:#26313d; --color-hover:#1c2530; --surface-panel-strong:#1a222c; --surface-panel-subtle:#141b23; --panel-border:#26313d; color-scheme: dark; }
```

## 10. 工程与测试纪律

- 测试只跑 **8158 生产构建**（`./scripts/project_build.sh`；占用时 `stop.sh/start.sh` PID 文件方式重启，禁 pkill 模式匹配）。**改源码后必须重新构建再测**——8158 验收面是构建产物，忘构建=测旧 bundle（wave-14 教训）。
- 皮肤回归 spec 全集（`frontend/e2e/`）：`skin_system.spec.ts`（内置三皮肤组件对齐，6 例）· `skin_wave8/9/10.spec.ts`（逐波元素对齐）· `skin_wave11.spec.ts`（六界面 + 上传流）· `skin_wave14.spec.ts`（登录页 6 组合 + 明暗翻转 + sidebar 两行等宽/中心线 ±1px）· `skin_wave15.spec.ts`（五盲区：后台任务/死磕状态条/草稿箱/附件卡/盘问卡，6 例）· `skin_wave16.spec.ts`（mono 四盲区 + 公式/mermaid 卡 + 复制钮 hover，8 例）· `skin_wave17.spec.ts`（wave-17/17b：sidebar 二级菜单六组 + 分组编辑卡三皮 + 移动端会话左划色板×2 态 + 笔记页头部/卡/附属面 + np 侧栏左划交互 + 桌面回归 + 卡片无遮罩/np 方条/左划内容尺寸，31 例）· `mobile_voice_mode_toggle_return.spec.ts`（移动端三皮肤语音浮层）；生产构建取证 `skin_wave8_prod.spec.ts`。
- e2e 造数配方（wave-15 沉淀，可复用）：后台任务=POST /api/agent-tasks；草稿=localStorage `weaver_drafts_v1`；附件=route-mock GET /api/conversations/{id}（selectConversation 走详情端点内嵌 messages）；死磕状态条=SQL 种 deathmatch_mode/status（ConversationUpdate 不可写）；盘问卡=route-mock POST /api/chat/stream SSE `deathmatch_verdict`（grillingQuestions 只经 SSE 进入）。
- 深色全表面对比度：`tests/w12_capture.py` DOM 审计器（见第 5 节红线 5），skin 波次收尾必跑。
- 后端 API 测试：`tests/api/test_skin_api.py`（目录/偏好 22 断言）· `tests/api/test_skin_upload.py`（上传 23 断言）；运行需后端在位（默认 `https://localhost:8158`）。
- 媒体证据评级走 mm-sensor（`tests/mm_ratings/`），不外发设计稿；像素级视觉留用户目检（wave-13 用户裁决：本模型无图像输入，降级=计算样式 oracle 断言 mockup 精确值）。

## 11. 社区皮肤路线图

已完成：运行时 CSS 皮肤上传（方案 A+，per-user 隔离）。后续可选：
1. **JSON 令牌皮肤**（轻量方案 B）：只传令牌 map（白名单键），`setProperty` 注入——不能做组件级语言，适合色彩微调。
2. **皮肤市场**：公开共享目录 + 评分（需要把信任模型从 per-user 升级为签名/审核）。
3. **可视化编辑器**：令牌表单 + 实时预览（基于第 3 节契约生成草稿）。
