<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

---
name: media_playback
description: 在回答中内嵌播放视频/音频（HTML video/audio 标签与 YouTube、Bilibili 官方 embed iframe）。用户要求播放、显示在线视频或音频时使用
category: builtin
---

# media_playback — 对话内媒体播放

## 适用场景
- 用户要求"直接显示/播放视频或音频"
- 用户给出 YouTube、Bilibili 等平台链接要求播放

## 用法
对话界面原生支持内嵌音频/视频播放器，在最终回答中使用原始 HTML 标签即可，不要声明界面不支持播放。

### 本地/直链文件
优先获取一个真实可播放的媒体文件：用 `terminal`/`execute_code` 将可直链的视频/音频文件（如公开的 .mp4/.webm/.mp3 直链）下载到用户工作区（如 workspace 根目录），然后在回答中内嵌播放（工作区相对路径会被系统自动转换为可访问的播放地址）：
- 视频：`<video controls src="文件名.mp4"></video>`
- 音频：`<audio controls src="文件名.mp3"></audio>`
也可调用 `provide_file` 提供媒体文件（系统会渲染成带播放器的附件卡片，附下载按钮）。

### 第三方平台内嵌（合法且受支持）
YouTube、Bilibili 等平台提供官方分享/内嵌链接，通过 iframe 内嵌是平台官方支持、完全合法的播放方式，绝不构成版权违规，不要宣称"版权限制无法内嵌"。当用户要求播放某个在线视频时，优先用 `web_search` 找到该视频页面 URL，然后构造官方 embed 链接：
- YouTube：把页面 URL 中的视频 ID（watch?v= 后面的部分，如 5XEN4vtH4Ic；youtu.be/ID 同理）拼成 `https://www.youtube.com/embed/<视频ID>`，输出 `<iframe width="560" height="315" src="https://www.youtube.com/embed/<视频ID>" frameborder="0" allowfullscreen></iframe>`。
- Bilibili：把页面 URL 中的 BV 号（bilibili.com/video/BVxxxxx 的 BV 部分）拼成 `https://player.bilibili.com/player.html?bvid=<BV号>&page=1`，输出 `<iframe src="https://player.bilibili.com/player.html?bvid=<BV号>&page=1" width="560" height="315" frameborder="0" allowfullscreen></iframe>`。

## 规则
1. 仅支持上述官方 embed 端点；系统只渲染白名单内的 iframe，其他来源会被拒绝。
2. 媒体文件优先保存到工作区，避免外链失效。
