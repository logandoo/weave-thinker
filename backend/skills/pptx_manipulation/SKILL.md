---
name: pptx_manipulation
description: 生成、读取或修改 PowerPoint 演示文稿（.pptx），通过 execute_code 调用 python-pptx，支持版式、图表、表格、图片、演讲者备注
category: office
---

<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# pptx_manipulation — PowerPoint 演示文稿生成与编辑

## 适用场景
- 用户要求生成或修改 PowerPoint 演示文稿（`.pptx`）
- 需要版式、文本框、形状、图片、表格、图表的幻灯片
- 读取/提取已有 pptx 的内容

## 用法
通过 `execute_code` 调用 `python-pptx`（预装，版本 1.0.2）生成代码并执行，输出 `.pptx` 文件保存到**工作区根目录**（用绝对路径；需要给用户下载时调用 `provide_file` 生成下载卡片，写文件本身不会自动出卡片）。

## 核心 API

### 1. 画布尺寸（重要）
python-pptx 默认模板是 **4:3（10"×7.5"）**，不是 16:9。要 16:9 必须显式设置：
```python
from pptx import Presentation
from pptx.util import Inches, Pt, Emu

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
```

### 2. 版式（Layout）与占位符
```python
from pptx.dml.color import RGBColor

# 常见版式索引：0=标题页 1=标题+内容 5=仅标题 6=空白
# 同一份演示文稿中要变化版式，不要所有页都用同一个版式
slide = prs.slides.add_slide(prs.slide_layouts[0])   # 封面
slide.shapes.title.text = "季度报告"
# 标题一律默认黑色：默认模板标题页/标题占位符文字可能是白色或主题色，必须显式设黑
tp = slide.shapes.title.text_frame.paragraphs[0]
tp.font.color.rgb = RGBColor(0, 0, 0)
tp.font.name = "SimHei"
slide.placeholders[1].text = "2026 Q1 汇报"

s2 = prs.slides.add_slide(prs.slide_layouts[1])      # 标题+要点
s2.shapes.title.text = "核心要点"
t2p = s2.shapes.title.text_frame.paragraphs[0]
t2p.font.color.rgb = RGBColor(0, 0, 0)
t2p.font.name = "SimHei"
body = s2.placeholders[1].text_frame
body.text = "第一条要点"
p = body.add_paragraph(); p.text = "子要点"; p.level = 1
```

### 3. 文本框
```python
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1.5))
tf = tb.text_frame; tf.text = "标题文字"
p = tf.paragraphs[0]
p.font.size = Pt(28); p.font.bold = True
p.font.name = "SimHei"                                   # 中文标题黑体
p.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
p.alignment = PP_ALIGN.CENTER                            # LEFT / RIGHT / JUSTIFY
```

### 4. 形状
```python
from pptx.enum.shapes import MSO_SHAPE

shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                               Inches(1), Inches(2), Inches(3), Inches(1.2))
shape.text = "标签"
shape.fill.solid(); shape.fill.fore_color.rgb = RGBColor(0x44, 0x72, 0xC4)
shape.line.color.rgb = RGBColor(0x00, 0x00, 0x00); shape.line.width = Pt(1)
```

### 5. 图片
```python
# 图片放工作区根目录或 uploads/ 下，用相对路径引用（沙箱 cwd 是 scratch 目录，可写绝对路径）
slide.shapes.add_picture("uploads/logo.png", Inches(0.5), Inches(0.5), width=Inches(2))
```

### 6. 表格
```python
table = slide.shapes.add_table(rows=4, cols=3,
                               left=Inches(1), top=Inches(3),
                               width=Inches(8), height=Inches(2.5)).table
table.columns[0].width = Inches(2)
for i, h in enumerate(["产品", "Q3", "Q4"]):
    cell = table.cell(0, i); cell.text = h
    cell.text_frame.paragraphs[0].font.name = "SimHei"
    cell.text_frame.paragraphs[0].font.bold = True
    cell.text_frame.paragraphs[0].font.color.rgb = RGBColor(0, 0, 0)   # 表头黑字
table.cell(1, 0).text = "示例数据"
```

### 6b. Markdown 表格映射（对话中的表格 → pptx，必须完整还原）
你在对话里输出的 markdown 管道表，转成幻灯片表格时必须逐格完整映射：
表头加粗+黑色、对齐（`:---` 左 / `:---:` 中 / `---:` 右）、单元格内换行（`<br>`）、
转义竖线（`\|`）。配方（沙箱可整体粘贴执行）：

```python
import re
from pptx.util import Pt, Inches
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN

def md_table_to_rows(md_text):
    rows, aligns = [], []
    for ln in [l.strip() for l in md_text.strip().splitlines() if l.strip()]:
        cells = [c.strip().replace('\\|', '|').replace('<br>', '\n')
                 for c in re.split(r'(?<!\\)\|', ln.strip('|'))]
        if cells and all(re.fullmatch(r':?-{3,}:?', c) for c in cells):
            aligns = ['center' if c.startswith(':') and c.endswith(':')
                      else 'right' if c.endswith(':') else 'left' for c in cells]
            continue
        rows.append(cells)
    return rows, aligns

def add_md_table(slide, md_text, left, top, width, height, font_size=Pt(12)):
    rows, aligns = md_table_to_rows(md_text)
    n = max(len(r) for r in rows)
    amap = {'left': PP_ALIGN.LEFT, 'center': PP_ALIGN.CENTER, 'right': PP_ALIGN.RIGHT}
    table = slide.shapes.add_table(len(rows), n, left, top, width, height).table
    for i, row in enumerate(rows):
        for j in range(n):
            tf = table.cell(i, j).text_frame
            tf.text = row[j] if j < len(row) else ""   # \n 自动成为单元格内多行
            for p in tf.paragraphs:
                p.font.size = font_size
                p.font.color.rgb = RGBColor(0, 0, 0)   # 全表默认黑色文字
                if i == 0:
                    p.font.bold = True                 # 表头加粗
                if aligns and j < len(aligns):
                    p.alignment = amap[aligns[j]]
    return table

# 用法：add_md_table(slide, md_text, Inches(1), Inches(2), Inches(8), Inches(3))
# 行数多时拆成多页或缩小 font_size，禁止为塞下一页而删行删列。
```

**反向映射**（读取用户 pptx → 在对话里展示 markdown 表）：
```python
def pptx_table_to_md(table):
    lines = []
    for i, row in enumerate(table.rows):
        cells = [c.text.replace('\n', '<br>').replace('|', '\\|') for c in row.cells]
        lines.append('| ' + ' | '.join(cells) + ' |')
        if i == 0:
            lines.append('|' + '---|' * len(cells))
    return '\n'.join(lines)
# 遍历：for shape in slide.shapes: if shape.has_table: ...
```

### 7. 图表
```python
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

cd = CategoryChartData()
cd.categories = ["Q1", "Q2", "Q3", "Q4"]
cd.add_series("销售额", (19.2, 21.4, 16.7, 23.8))
chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                               Inches(1), Inches(2), Inches(8), Inches(4), cd).chart
chart.has_legend = True
# 其他常用类型：XL_CHART_TYPE.LINE、PIE、BAR_CLUSTERED
```

### 8. 演讲者备注
```python
slide.notes_slide.notes_text_frame.text = "本页讲解重点：数据来源与解读"
```

### 9. 编辑已有 pptx / 读取内容
```python
prs = Presentation("uploads/old.pptx")     # 打开已有文件修改
prs.slide_width = Inches(13.333); prs.slide_height = Inches(7.5)  # 如需要调整尺寸
for slide in prs.slides:
    title = slide.shapes.title
    if title:
        print(title.text)
# 修改完成后另存为工作区根目录新文件，如 "old_修改.pptx"
```

## 生成后校验（必做）
保存后重新打开文件校验，再向用户交付：
```python
prs = Presentation("report.pptx")
print(len(prs.slides), "页")
for i, slide in enumerate(prs.slides, 1):
    t = slide.shapes.title
    print(i, t.text if t else "(无标题)")
```
检查：页数符合预期、每页有标题、表格行列数正确、图表已生成（`slide.shapes` 中包含 chart 时 `shape.has_chart` 为 True）。

## 规则
1. **默认使用 16:9**：必须显式 `prs.slide_width = Inches(13.333); prs.slide_height = Inches(7.5)`；python-pptx 默认是 4:3，不要依赖默认值。用户要求 4:3 时用 `Inches(10) × Inches(7.5)`。
2. 变化版式：封面用版式 0、内容页用版式 1/5/6，避免全部页面堆在同一版式；优先用占位符（`shapes.title` / `placeholders[1]`）而不是到处加文本框。
3. 中文排版：标题/表头用 SimHei（黑体），正文用 SimSun（宋体）；**标题一律默认黑色**（封面标题页与每个标题占位符都要显式 `RGBColor(0, 0, 0)`——默认模板标题页文字可能是白色）；字体名只是字符串，由用户端 PowerPoint 解析，直接设置即可。
4. 每页幻灯片内容精简，标题 + 要点为主，避免大段文字（超过 6 行要点应拆页）。
5. 配色使用 `RGBColor`，整套幻灯片风格统一（主色 + 强调色，建议一套 2~3 色）；标题文字不参与配色，恒为黑色。
6. 修改已有文件时另存为新文件（如 `xxx_修改.pptx`），不要覆盖 uploads 原文件。
7. 输出文件保存到工作区根目录（绝对路径），并调用 `provide_file` 生成下载卡片；草稿/中间数据写 scratch 临时目录。
8. 不要用代码生成 PDF——PDF 导出使用 `pdf_export` 工具。
9. 生成后必须执行"生成后校验"步骤并如实报告页数/标题等结果。
10. 对话中的 markdown 表格转 pptx 必须使用「Markdown 表格映射」节（6b）的配方逐格完整还原（对齐/转义/换行/表头加粗黑色），禁止丢列、丢行或静默省略单元格；放不下就拆页或缩字号。
