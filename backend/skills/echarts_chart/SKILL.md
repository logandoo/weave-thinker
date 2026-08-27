<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

---
name: echarts_chart
description: 在对话中直接输出 ECharts 交互图表（```echarts JSON 代码块），含格式、布局防重叠规范。用户要求统计图表、柱状图、折线图、饼图等时使用
category: builtin
---

# echarts_chart — 对话内 ECharts 交互图表

## 适用场景
- 用户要求展示统计图表（柱状图、折线图、饼图、散点图、雷达图、热力图、K线图、仪表盘等）
- 数据适合在对话内直接展示，无需生成可下载图片文件

## 用法
直接在回答中使用 ```echarts 代码块输出一个标准 JSON 对象（ECharts option 配置），系统会渲染为可交互图表。
禁止为此调用 execute_code 绘图生成图片文件，禁止用 ASCII 艺术图或纯文本表格代替图表。

## 格式要求
1. ```echarts 代码块内必须是可被 JSON.parse 直接解析的标准 JSON：禁止 JavaScript 函数/表达式、注释、尾逗号、单引号；字段名和字符串必须用双引号。
2. 常用字段：title.text（标题）、tooltip、legend、xAxis（type:'category'+data 数组）、yAxis（type:'value'）、series（type 用 'bar'/'line'/'pie'/'scatter'/'radar'/'heatmap'/'candlestick'/'gauge' 等标准类型，data 为数值数组）。
3. 图表说明文字写在代码块外的正文里，不要写在 JSON 内。

## 布局要求（防止元素重叠，最重要）
1. series 必须设置 name 字段（与 legend.data 一一对应，否则图例不会显示）。
2. 当图表含 title 且使用 markPoint（如标注峰值/最低点）或 markArea（如标注时间区间）时，必须配置 `grid: {top: 90}` 或更大（若同时含图例，建议 `top: 115`），为顶部标注预留空间，否则标注会与标题/图例重叠。
3. 省略 grid 时系统也会自动预留顶部空间，但显式配置更可靠。

## 规则
1. 只有当用户明确要求生成可下载的图片/Excel 文件（如 PNG 附件、xlsx 数据表）时，才允许使用 execute_code 绘图。
2. 图表会直接在对话界面渲染为可交互图表；导出笔记或对话为 MD/PDF 时，图表会自动转换为图片保存，用户无需任何额外操作。
