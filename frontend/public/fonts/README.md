<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# 自托管 UI 字体

`frontend/public/fonts/` 存放前端 UI 字体（Inter + Noto Sans SC，均为 SIL OFL 1.1，
再分发义务见 `FONTS_LICENSE.md`）。`index.html` 直接引用 `fonts.css`（构建产物里
带 `/app/frontend/` base 前缀），运行时**无任何第三方 CDN 请求**，离线可用。

- 文件命名 `{Family}-{sha1(css2字体URL)前10位}.woff2`，与 `fonts.css` 内
  @font-face 一一对应；变量字体按 unicode-range 分片，勿单独删片。
- 重新抓取（换字重/升级字体时）：以 Chrome UA `curl` Google Fonts css2 端点，
  解析全部 @font-face、下载 woff2 并按本目录规则重命名 + 重写 URL，
  同时刷新 `FONTS_LICENSE.md` 的版本/版权/日期。
