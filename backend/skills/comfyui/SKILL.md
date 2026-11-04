---
name: comfyui
description: 在 ComfyUI（节点式生图/生视频界面）中安全地选择、核对并提交工作流。适用于 Qwen-Image、MiniMax-H3、Stable Diffusion、Flux 等 ComfyUI 服务，或用户要求“进入 ComfyUI / 找到某个工作流 / 生成图片或视频 / 调节点参数”的任务。核心规则：启动默认工作流不是目标工作流；先确认当前工作流身份和模型三件套，再改节点、再提交。
category: builtin
---

<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# comfyui — ComfyUI 工作流操作

## 适用场景
- 用户给出 ComfyUI / 面板入口，要求在某个服务里生图、生视频或编辑图片
- 需要找到并加载站点提供的官方模板工作流
- 需要核对当前图是否为目标工作流、模型/CLIP/VAE 是否匹配
- 需要按 `prompt_id` 查历史并取回输出文件

## 0. 先过 canvas 就绪关
ComfyUI 的 `<canvas>` 出现 ≠ 工作流已加载。按 `browser` 技能的
“Canvas / SPA 就绪”执行：优先用 `wait_js` 等**目标工作流独有**的信号。
`browser_navigate` 的 `settled=true` 只表示页面在观察窗口内稳定，**不证明**
目标工作流已加载；`settled=false` 才表示页面一直在变。`warnings` 是诊断信息
（例如 canvas 在窗口内没有任何状态变化），不是“一律禁止操作”的硬门。
无论哪种情况，先做第 1 步身份核对；确认前不要改节点、不要提交。

## 1. 确认当前工作流身份（改任何节点之前）
用 `browser_execute_js` 打印：
```js
JSON.stringify({
  hash: location.hash,
  title: document.title,
  nodes: app.graph.nodes.map(n => n.type + ':' + n.id),
  loaders: app.graph.nodes
    .filter(n => /Loader|TextEncode|KSampler|SaveImage|SaveVideo/.test(n.type))
    .map(n => ({type: n.type, id: n.id, w: n.widgets_values})),
})
```
判据（看**类型与模型装载值**，不要只看节点数量）：
- 目标工作流的节点类型/数量应与站点模板一致；`location.hash` 可能指向模板名；
- 启动/默认工作流的模型装载值往往不匹配（例如 CLIPLoader 的 `type` 是
  `lumina2`，而 UNET 是 `qwen_image_2.1`）；
- 节点 id 只在本页/本版本有效，**不要跨页面复用**。发现图是空的或是默认图，
  不要改 id，直接走第 2 步加载模板。

## 2. 加载站点官方模板
`/templates/index.json` 是**分类 + 元数据**，不是工作流本体；真正的工作流在
`/templates/<name>.json`：
```js
const idx = await (await fetch('/templates/index.json')).json();
// 在 idx 的分类/模板列表中按 name/title 找到目标，例如：
//   image_qwen_image_2_1_t2i      Qwen Image 2.1: Text to Image
//   video_minimax_h3_i2v          MiniMax H3: Image to Video
const tpl = await (await fetch('/templates/image_qwen_image_2_1_t2i.json')).json();
await app.loadGraphData(tpl);   // ComfyUI 0.37.x 实测可用
// 更新版本可能暴露 workflow manager / workflow service；以页面实际 API 为准，
// 不要凭记忆调用不存在的 app.workflowManager。
```
加载后再执行第 1 步核对。`loadGraphData` 需要第 2 段取到的 workflow JSON，
传 `index.json` 的元数据条目会得到空图。

## 3. 站点实例经验（ComfyUI 0.37.4）
- **Qwen-Image 2.1 图像生成**：
  - 目标工作流：9 节点，`UNETLoader` + `QwenImage21Cache` + `CLIPLoader`
    （`qwen3vl_8b_int8_convrot.safetensors`, type=`qwen_image`）+
    `TextEncodeQwenImage21` + `EmptyLatentImage` + `KSampler` +
    `VAELoader` + `VAEDecode` + `SaveImage`；URL hash 可能是 `#qwen21-t2i-flat`。
  - 启动默认图（坏）：10 节点（摘要：`CLIPLoader` type=`lumina2`、
    两个 `CLIPTextEncode`、`KSampler`、`SaveImage`，另有 `UNETLoader`/`VAELoader`/
    `VAEDecode`/cache 节点）；用它提交会出噪点。
  - 目标图的正向 prompt 节点是 `TextEncodeQwenImage21`；尺寸/采样在
    `EmptyLatentImage`/`KSampler`。可用的 `wait_js`：
    ```js
    () => typeof app !== 'undefined' && app.graph?.nodes?.some(n => n.type === 'TextEncodeQwenImage21')
    ```
- **MiniMax-H3 视频生成**：
  - 入口**不会自动加载目标工作流**（可能先加载默认图），hash 可能一直为空；必须显式加载
    `video_minimax_h3_i2v`（或 `video_minimax_h3_t2v`），再用
    `() => typeof app !== 'undefined' && app.graph?.nodes?.some(n => n.type.includes('MiniMaxH3'))`
    确认。
  - 模型是音视频联合模型；帧数参数按 24fps 的 `17k+5` 网格对齐，实测最小值 5
    （设为 1 会被服务端拒绝）。要静帧就生成 5 帧再抽第 1 帧；具体上下限以页面
    widget 的 `min/max/step` 为准。
  - 输出是 `SaveVideo` 的 mp4（含音频轨），不是 PNG；用 ffmpeg 抽帧。

## 4. 提交与取回
1. 改参数后打印实际要 POST 的图摘要：节点类型、模型名、正/负 prompt 节点 id、
   尺寸、步数、seed。
2. 提交并检查 `node_errors`：
   ```js
   const p = await app.graphToPrompt();
   const res = await fetch('/prompt', {
     method: 'POST',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({prompt: p.output, client_id: 'webui'}),
   });
   const j = await res.json();   // {prompt_id, number, node_errors}
   if (j.node_errors && Object.keys(j.node_errors).length) throw new Error(JSON.stringify(j.node_errors));
   return j.prompt_id;
   ```
3. 轮询 `GET /history/<prompt_id>`，结构是
   `{<prompt_id>: {status: {status_str}, outputs: {<save_node_id>: {images: [{filename, subfolder, type}]}}}}`；
   从 `outputs`（不是从 graph 的节点）读 filename/subfolder/type，用 `GET /view`
   取文件；不要写死 `ComfyUI_00001_.png`。
4. 输出为空/噪点时先回查第 1 步：工作流身份、CLIP type、UNET/VAE 三件套是否匹配，
   再怀疑 prompt 或模型。不要用“默认工作流坏了”掩盖“根本没加载目标工作流”。

## 5. 鉴权
面板入口通常 302 到 ComfyUI 并设置短效 cookie/JWT；浏览器会话内跟随跳转即可。
用 `curl` 直接访问 `/view` 需要同源 JWT（cookie 或 `Authorization: Bearer`），
否则 401。不要复用其他用户或过期的 token。
