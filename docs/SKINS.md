<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# 皮肤设计与接入指南

---

## 1. 皮肤系统概述

界面皮肤与系统完全解耦：**CSS 设计令牌（Design Tokens）+ 皮肤注册表 + 组件级覆写**，三套内置皮肤任选，另支持开发者经 API 上传自有皮肤（第 6 节）。

- **双轴正交**：皮肤（verdant-flat / ink-paper / mono-brutal / …上传皮肤）× 明暗（light / dark）
- **DOM 载体**：`<html data-skin="<id>">` + `<html data-theme="dark">`（light 时移除属性）
- **持久化**：localStorage `wt-skin` + 后端 `users.ui_preferences`（跨设备同步，设备本地优先）
- **零运行时成本（内置皮肤）**：纯 CSS 变量切换，无 JS 重渲染；上传皮肤经认证 fetch → Blob `<link>` 注入
- **级联纪律**：皮肤文件组件级覆写一律 `html[data-skin="<id>"] .<class>` 前缀（特异性 0,2,1 / 0,3,1，稳胜 scoped `[data-v]` 的 0,2,0 / 0,3,0，与注入顺序无关）
- **对比度纪律**：内置三皮肤 light/dark 均经全表面对比度审计（正文 ≥ 4.5:1 逐项核验）；新增界面表面必须 light/dark 成对交付（第 5 节红线 7）
- **登录页即入口**：登录页右上带皮肤/明暗控件，三皮肤 × 明暗 6 组合在登录页均生效；未登录选皮本地先生效，登录后同步

## 2. 内置皮肤目录

| id | 名称 | 设计范式 | 气质 |
|---|---|---|---|
| `verdant-flat` | 青野平面（默认） | 浮动玻璃卡（大圆角 + 软影 + hover 上浮） | 苔绿画布上的扁平自然系 |
| `ink-paper` | 墨韵纸间 | 贴边发丝线（1px `--skin-line` + 小圆角 + 宋体标题） | 宣纸暖底、朱砂点墨的文房气质 |
| `mono-brutal` | 黑白构成 | 硬边直角（2–3px 黑框 + 偏移硬影 + 900 黑体标题） | 高对比黑白构成，橙色锐利点缀 |

皮肤目录经 `GET /api/skins` 公开暴露（含 `token_contract_version`）。前端注册表 `frontend/src/config/skins.ts` 与后端 `backend/app/api/skins.py` 的 `SKIN_CATALOG` **id 必须一致**（皮肤面板 = 后端目录 ∪ 前端注册表合并渲染，任一漂移都会造成选皮不生效或重复卡）。

## 3. 令牌契约（Token Contract）

默认皮肤（`:root` / `[data-theme="dark"]`，`frontend/src/styles/main.css`）= verdant-flat。皮肤在 `[data-skin="<id>"]`（light）与 `[data-skin="<id>"][data-theme="dark"]`（dark，**必须完整定义基础令牌**、文件内放最后）两组块中覆写。

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

### 3.2 组件对齐令牌（皮肤可覆写）

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

### 3.3 共享交互态令牌 —— 六界面/菜单/卡片家族的解耦层

| 令牌 | 用途 | verdant-flat | ink-paper | mono-brutal |
|---|---|---|---|---|
| `--overlay-scrim` | 对话框遮罩底色 | `rgba(24,18,14,.35)` / dark `rgba(0,0,0,.62)` | `rgba(42,37,33,.30)` / `rgba(0,0,0,.55)` | `rgba(0,0,0,.5)` / `rgba(0,0,0,.6)` |
| `--focus-ring-color` | 输入 focus 环色 | `16% primary` / `24%` | `10% primary` / `16%` | `18% primary` / `24%` |
| `--primary-tint` / `--primary-tint-strong` | hover/active 主色淡底（8%/12% color-mix，随 --color-primary 自动适配明暗） | 默认皮肤表达式 | 同（继承） | 同（继承） |
| `--danger-tint` / `--warning-tint` | 错误/警告条底色（8%/10%，dark 12%/14%） | 默认皮肤 | 同 | 同 |
| `--primary-glow` | 主按钮 hover 光晕 | `0 8px 20px 25% primary` / dark 黑影 | 同 | 同 |
| `--action-edit-bg` / `--swipe-move-bg` / `--success-strong` | 滑动操作条（编辑 #6b7280 / 移动 #f59e0b / 设默认激活 #2e7d32） | 默认皮肤 | 同 | 同 |
| `--menu-radius` / `--menu-border` / `--menu-shadow` | 弹出菜单家族（思考/更多/技能/上下文菜单容器） | `16px` / `1px var(--panel-border)` / `0 4px 16px 10% text` | `4px` / `1px var(--skin-line)` / `0 10px 34px rgba(60,50,30,.14)` | `0` / `2px var(--skin-line)` / `5px 5px 0 var(--skin-line)` |
| `--stack-card-radius` / `--stack-card-shadow` / `--stack-card-shadow-hover` | 桌面 wallet-stack 卡片（笔记本/笔记 3 列卡） | `18px` / 绿调双层软影 | `6px` / 纸调软影 | `0` / `4px 4px 0` 硬影 |
| `--stack-hover-lift` / `--stack-overlap` | stack hover 抬升 / 卡叠压 | `-18px` / `-40px` | `-14px` / `-40px` | `-10px` / `-40px` |

### 3.4 拓展令牌族

| 令牌 | 用途 | 内置三皮语言 |
|---|---|---|
| `--logo-*` ×16（tile-1/2 · ring(-sw) · dash(-sw) · tick-n(-sw) · tick-se(-sw) · letter · letter-font · letter-weight · needle-n · needle-s · dot） | Logo 图标令牌化（`LogoIcon.vue` 全量 `var()`，fallback=青野默认值零漂移，**零 JS 分支**）；登录/空态/侧栏三处生效 | 墨韵=奶油 tile + 墨环 #2a2521 + 金虚线 #c9a86a + 朱砂北针 #b23b2e + Georgia 字母（明暗恒值）；黑白=白 tile + 可见 #111 描边 + 黑环 + 灰虚线 + 橙针 #D64008 + 黄心点 #DBAF00 + Arial Black；青野=默认皮肤 |
| `--info-tint` / `--info-tint-strong` / `--success-tint` / `--success-tint-strong` | 信息/成功语义淡底：死磕状态条、附件卡、盘问卡、后台任务面板；light/dark `color-mix` 12/22% · 14/24% 随主题自动加深 | 默认皮肤表达式，明暗自动适配 |
| `--formula-bg` / `--formula-border` / `--formula-radius` | 公式/Mermaid 查看卡：上下文无关全局规则（`main.css` 800 行起，消息内公式走 `.math-editable`/`.math-rendered-content`、不在 `.stream-markdown` 下）；NoteEditor 数学/mermaid 对话框同族 | 青野 14px 软影 / 墨韵 4px 发丝 + serif 斜体提示 / 黑白 0 直角 + 硬边 |

**解耦规则**：组件 scoped 样式只准消费令牌（`var(--…)`），不得出现具体色值；新增「组件家族共享的视觉语义」时必须先落一枚令牌再接入组件（否则上传皮肤无法覆盖该语义）。

## 4. 六界面皮肤覆盖矩阵

| 界面 | 组件 | verdant-flat | ink-paper | mono-brutal |
|---|---|---|---|---|
| 登录 | `.login-card` `.login-title` `.login-btn` 表单（墨韵=纸卡金调双框+seal-mini+水墨远山竖排诗场景，黑白=3px 硬边+8px 偏移硬影+LOGIN/01 角标+巨型描边字场景，`body:has(.login-page)` 门控） | 24px 白卡软影 + 渐变绿按钮 | 6px 发丝卡 + 4px 输入/按钮 + 宋体标题 | 0 直角 + 3px 硬边 + 5px 偏移硬影 + 900 黑体标题 |
| 登录页控件 | `.login-prefs`（皮肤 popover + 明暗钮） | 全令牌零硬编码（`--surface-panel-strong`/`--panel-border`/`--radius-md`/`--primary-tint`），6 组合自动适配，无逐皮覆写 | 同 | 同 |
| 系统设置 | `.system-settings-overlay` `.system-settings-card` `.settings-tab` `.skin-card` `.mode-switch` | 24px dialog + 绿 accent tab | 暖 scrim + 6px 发丝卡 + 宋体 tab + 2px active 线 + 4px 发丝皮肤卡 | 黑 scrim + 0/3px 偏移硬影 + AA 900 tab + 3px 硬边皮肤卡 + 反色 active |
| 笔记引用 | `.note-picker-overlay` `.note-picker-modal` `.note-option` `.search-input` 按钮 | 24px 玻璃卡 + 11-14px item | 6px 纸卡 + 宋体 `.picker-title` + 发丝 hover + 4px 按钮 | 0+3px 硬边 + AA 标题 + 直角反色 hover + 2px 硬边按钮 |
| 思考模式菜单 | `.reasoning-menu` `.reasoning-menu-item`（同 `.more-dropdown-teleport`/`.skill-popup`/`.context-menu` 家族） | 16px 玻璃卡 + 11px item | 4px 发丝纸卡 + 宋体标题 ls2 + 3px item + 朱红淡底 active | 0+2px 硬边 + 5px 偏移硬影 + 900 标题 + 直角反色 active |
| 笔记本卡片（桌面 3 列 stack） | `.stack-column > .notebook-row` `.notebook-item` `.notebook-name` | 18px + 绿调软影 + -18px 抬升 | 6px + 纸调软影 + 发丝内卡 + -14px | 0 + 2px 硬边内卡 + 4px 偏移硬影 + -10px + 800 标题 |
| 笔记卡片（桌面 3 列 stack） | `.stack-column > .note-row` `.note-item` `.note-title` | 同上家族 | 同上家族 | 同上家族 |
| 深层界面 | 死磕状态条 / 后台任务面板 / 草稿箱 / 附件卡 / 盘问卡（信语义淡底走 `--info·success-tint` 族）· 公式/Mermaid 查看卡（`--formula-*` 族）· 代码窗复制钮 hover（禁 `--color-border` 作底，见红线 8） | 逐面覆写齐三皮 | 同 | 同 |

皮肤文件位置：`frontend/src/styles/themes/<id>.css`（三个内置皮肤各 ~1700–2000 行，light 令牌块 → dark 令牌块 → 组件级覆写 → 移动端 media 豁免 → 夜晚态微调）。

**登录控件持久化**：`.login-prefs` 复用 `useSkinStore`（allSkins/setSkin/toggleMode 涟漪）；未登录 PUT 偏好 401 静默早退（本地先生效），登录后 `syncFromServer` 统一。ARIA：disclosure 模式 `aria-expanded`/`aria-pressed`，Escape 关闭+焦点回触发钮，外点监听 capture 相位。

## 5. 皮肤文件结构（开发者模板）

```css
/* 皮肤：示例 (example-skin) —— id 必须与文件名一致 */

[data-skin="example-skin"] {
  /* 1. 基础令牌（第 3.1 节全集，light 值必须完整） */
  --color-primary: #2f6f8f;
  /* …… 至少覆盖：primary 族 / bg 族 / surface 族 / text 族 / 状态色 /
        border/hover / panel-border / shadow 三档 / scrollbar；
        组件对齐令牌（3.2/3.3/3.4）按自家语言挑选覆写，未覆写=继承默认皮肤 */
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

**红线（务必遵守）：**
1. **笔记正文 = 用户内容**：`.zen-note-content` 及其排版任何属性（颜色/字体/字号/行高/引用/标题）一律不得覆写；列表卡标题属 UI 层可覆写。
2. 组件覆写必须 `html[data-skin="<id>"] .x` 前缀（0,2,1）；裸 `[data-skin] .x`（0,1,1）压不过 scoped。
3. 移动端覆写必须防压默认皮肤：桌面规则（如 `position:relative`）会压过移动默认（`position:fixed`）→ 移动豁免块补 `!important`。
4. 每皮肤必须同时定义 light + dark 两组令牌；dark 块选择器双属性优先级最高、放最后。
5. 对比度：正文 ≥ 4.5:1；主按钮白字可读（6 组合逐一核验）。全暗色审计纪律：对整树复合背景做 getComputedStyle 遍历（含祖先链与 gradient 首停靠色）+ icon-only 探针 + disabled 按 WCAG 1.4.11 记豁免；每次改皮肤或加 UI 表面后跑一遍。
6. **级联 hover 陷阱（特异性战争）**：组件 scoped `.x[data-v]:hover:not(:disabled)` 特异性 (0,5,0) 压皮肤 (0,4,1)——鼠标划过即触发（静态检查全绿、交互中翻车）。每个 :hover/:active 皮肤覆写都要对组件 scoped 同族选择器算特异性（`[data-v]`+伪类常数在 (0,4,0)~(0,5,0)），压不过时用重复类名 `(0,5,1)+` 锁死全部互动态。
7. **dark 成对**：组件级覆写必须 light/dark 成对交付，禁止只写浅色硬编码（系统性浅底浅字风险）；「只改被点名的元素」会漏同族——发现问题时立即 grep 同色族全补。
8. **`--color-border` 禁作 hover 底色**：mono 下 `--color-border`=#111 → 黑底黑字（代码窗复制钮即此症状）；hover 底色用专门语义或 `color-mix(in srgb, var(--color-text) 12%, var(--color-hover))` 全皮自适应（实测 ≈12.4:1）。
9. **颜色探针格式**：Chromium 对 color-mix 结果返回 `color(srgb R G B)`（0-1 分量），探针正则必须双格式；对比公式必须 max/min（`0.08` 与 `1/12.4` 互为倒数，方向写反=假败）。

## 6. REST API

### 内置与偏好

- `GET /api/skins`（公开）→ `{token_contract_version, default_skin, skins:[{id,name,description,is_default,modes}]}`
- `GET /api/users/me/preferences`（登录）→ `{skin_id}`（未设置过 → `verdant-flat`；**上传皮肤 id 亦合法回读**）
- `PUT /api/users/me/preferences`（登录）→ 合法域 = **内置 ∪ 本人上传**；非法 → 400 `unknown skin_id: …`；未登录调用 → 401（前端静默早退）
- 上传皮肤的 CSS 原文不再走公共静态路径，见下 `GET /api/skins/{id}/css`（认证 fetch → 前端 Blob 注入）。

### 上传皮肤（API 契约，见上节「皮肤目录」相关端点）

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
- 启动零闪烁不变式：`App.vue` 同步 `initFromStorage` 先设 `data-skin`（未知 id 暂以默认皮肤渲染），上传皮肤样式待登录往返补装；内置皮肤无此窗口。
- 预览条：上传皮肤从 CSS 文本解析 light 锚点块前五个语义令牌作展示数据（`previewFromCss`），解析失败兜底默认皮肤色。
- 删除：撤 link + `URL.revokeObjectURL` + 注册表移出；当前在使用 → 回退默认。

## 8. 解耦约束与验证

- **红线 1**：全部组件 `.vue` 的 scoped style 中 `[data-skin` 选择器 = **0**（皮肤知识单点存于 `styles/themes/*.css` + 运行时注册表），静态扫描可复核。
- **红线 2**：JS 皮肤-id 条件分支（白名单 `stores/skin.ts` · `config/skins.ts` · `api/skins.ts` · `index.html` 预引导外）= **0**，静态扫描可复核。
- **余量（非阻断，硬编码色带）**：ChatArea 40 · Sidebar 36 · ChatInput 34 · VoiceChat 31 · MemoryPanel 26 · SkillsPanel 25 · TaskProgress 25 · NoteEditor 24 · …（静态扫描 scoped style 计数）。这些色带在上传皮肤下仍保持青野基调，列入后续 token 化。

**判定**：内置三套皮肤与上传皮肤对本节六界面 + 第 3 节全部令牌面，均可经「令牌覆写 + 组件覆写」完成样式替换，无单独写死路径；已知例外 = 余量色带（上表）。

*有意保留的既有状态*：移动端（<768px）列表单列卡的半径/内边框沿用默认皮肤 `--radius-md` 语言，三皮肤外观一致——桌面 stack 范式（第 4 节）是 skin-card 语言的载体；若后续要求移动端卡随皮变形，覆写 `--radius-md` 或补移动媒体块即可（上传皮肤同样生效）。

## 9. 开发者接入步骤（上传路线）

1. 新建 `my-skin.css`（工作目录任意处），按第 5 节模板：锚点 `[data-skin="my-skin"]`（**与文件名一致**）+ light/dark 令牌全集 + 所需组件覆写。
2. 系统设置 → 皮肤选择 → 「上传皮肤」→ 选文件 → 名称/描述 → 上传。成功 toast 后**自动应用**，卡片出现「自定义」徽章。
3. 验证：明暗切换均正常；第 4 节各界面 + 聊天主区对照三内置皮肤检查无破版；`data-skin` 页面重载后保持。
4. 修改后重传同名文件 = 覆盖（幂等）；换文件名 = 新皮肤（卡片并列存在）。
5. 不想用了：卡片 hover 删除。

**最小可用皮肤**（只覆写基色，其余全部继承默认皮肤语言）：

```css
[data-skin="mymind"] { --color-primary:#3b6ea5; --color-primary-dark:#2f5a8a; --color-bg:#eef3f8; --color-sidebar:#e5edf5; --color-text:#1f2d3d; --color-text-light:#5b6b7d; --color-border:#d4dfeb; --color-hover:#e3ecf5; --surface-panel-strong:#ffffff; --surface-panel-subtle:#f4f8fb; --panel-border:rgba(59,110,165,.18); color-scheme: light; }
[data-skin="mymind"][data-theme="dark"] { --color-primary:#6ea8dc; --color-primary-dark:#6ea8dc; --color-bg:#10151b; --color-sidebar:#151c24; --color-text:#dce6f0; --color-text-light:#8fa2b5; --color-border:#26313d; --color-hover:#1c2530; --surface-panel-strong:#1a222c; --surface-panel-subtle:#141b23; --panel-border:#26313d; color-scheme: dark; }
```

## 10. 工程与测试纪律

- 验收面 = **8158 生产构建**（`./scripts/project_build.sh`；服务占用时 `stop.sh`/`start.sh` 以 PID 文件方式重启，禁 pkill 模式匹配）。**改源码后必须先构建再测**——构建产物才是被测面。
- 皮肤回归：`frontend/e2e/` 提供 `chat.spec.ts`（主链路）+ `baseline_smoke.spec.ts`（冒烟）全套回归；皮肤修改后按第 4 节覆盖矩阵人工验证——light/dark × 三皮肤，六界面 + 聊天主区逐面无破版，像素观感以目检为准（无目检条件时用计算样式断言 mockup 精确值，比像素比对更强）。
- 登录页 6 组合对比度、sidebar 两行等宽/中心线对齐为出厂验收项（±1px）。
- 深色全表面对比度纪律见第 5 节红线 5，每次皮肤改动必跑。

## 11. 社区皮肤路线图

运行时 CSS 皮肤上传（per-user 隔离）为当前能力。可选演进：
1. **JSON 令牌皮肤**（轻量方案 B）：只传令牌 map（白名单键），`setProperty` 注入——不能做组件级语言，适合色彩微调。
2. **皮肤市场**：公开共享目录 + 评分（需要把信任模型从 per-user 升级为签名/审核）。
3. **可视化编辑器**：令牌表单 + 实时预览（基于第 3 节契约生成草稿）。
