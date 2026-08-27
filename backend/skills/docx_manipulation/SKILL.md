<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

---
name: docx_manipulation
description: 生成、读取或修改 Word 文档（.docx），通过 execute_code 调用 python-docx，支持页面设置、样式体系、页眉页脚、目录、列表、表格、图片、数学公式（OMML）
category: office
---

# docx_manipulation — Word 文档生成与编辑

## 适用场景
- 用户要求生成或修改 Word 文档（`.docx`）
- 需要带格式（标题、正文、表格、图片）的正式文档
- 读取/编辑用户上传的 docx（`uploads/` 目录）

## 用法
通过 `execute_code` 调用 `python-docx`（预装，版本 1.2.0）生成代码并执行，输出 `.docx` 文件保存到**工作区根目录**（用绝对路径，写在工作区根目录的文件才会成为下载卡片）。

## 页面设置（默认是 Letter，中文文档必须显式设 A4）
python-docx 默认模板是 Letter（8.5"×11"）而非 A4；中文正式文档一律显式设置：
```python
from docx import Document
from docx.shared import Cm

doc = Document()
sec = doc.sections[0]
sec.page_width = Cm(21)            # A4 竖版
sec.page_height = Cm(29.7)
sec.top_margin = Cm(2.54); sec.bottom_margin = Cm(2.54)
sec.left_margin = Cm(3.18); sec.right_margin = Cm(3.18)
```
横版：page_width / page_height 交换即可。多节（不同页面设置）用 `doc.add_section()` 后对 `doc.sections[-1]` 设置。

## 字体规范（中文文档）
- **标题**：SimHei（黑体）
- **正文**：SimSun（宋体）
- 设置中文字体需同时设置 `run.font.name` 和 `rFonts` 东亚字体：
```python
from docx.oxml.ns import qn
run.font.name = "SimSun"
run._element.rPr.rFonts.set(qn("w:eastAsia"), "SimSun")
```

## 样式体系（优先用内置样式，不要逐段手调）
- 标题必须用内置样式 `Heading 1`~`Heading 6`（目录、导航窗格、大纲结构依赖它们）
- 列表用 `List Bullet` / `List Number` 样式（禁止手工输入 `•`、`1.` 等字符）
- python-docx 模板的 Heading 样式默认是蓝色 Calibri Light，中文文档必须覆写：
```python
from docx.shared import Pt
from docx.oxml.ns import qn

def _set_cn_style(doc, name, font, size, bold=False):
    st = doc.styles[name]
    st.font.name = font
    st.font.size = Pt(size)
    st.font.bold = bold
    st.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), font)

_set_cn_style(doc, "Normal", "SimSun", 12)        # 正文
_set_cn_style(doc, "Heading 1", "SimHei", 16, True)
_set_cn_style(doc, "Heading 2", "SimHei", 14, True)
_set_cn_style(doc, "List Bullet", "SimSun", 12)
```

## 核心 API
```python
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
# 标题
doc.add_heading("一级标题", level=1)
# 段落
p = doc.add_paragraph("正文内容")
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.line_spacing = 1.5              # 中文文档常用 1.5 倍行距
p.paragraph_format.first_line_indent = Pt(24)      # 首行缩进 2 字符
# 列表
doc.add_paragraph("要点一", style="List Bullet")
doc.add_paragraph("要点二", style="List Number")
# 分页
doc.add_page_break()
# 表格
table = doc.add_table(rows=3, cols=3, style="Table Grid")
table.autofit = False                              # 固定列宽（Word 按 cell 宽度渲染）
for i, w in enumerate([Cm(3), Cm(4), Cm(4)]):
    table.columns[i].width = w
    for row in table.rows:
        row.cells[i].width = w
table.cell(0, 0).text = "单元格"
# 图片
doc.add_picture("uploads/logo.png", width=Cm(8))
# 保存
doc.save("/path/to/output.docx")
```

## 页眉页脚与页码
```python
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

def _add_page_number(paragraph):
    run = paragraph.add_run()
    f1 = OxmlElement('w:fldChar'); f1.set(qn('w:fldCharType'), 'begin')
    it = OxmlElement('w:instrText'); it.set(qn('xml:space'), 'preserve'); it.text = ' PAGE '
    f2 = OxmlElement('w:fldChar'); f2.set(qn('w:fldCharType'), 'end')
    run._r.append(f1); run._r.append(it); run._r.append(f2)

header = doc.sections[0].header
header.paragraphs[0].add_run("公司名称")           # 页眉文字
footer = doc.sections[0].footer
fp = footer.paragraphs[0]
fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
fp.add_run("第 "); _add_page_number(fp); fp.add_run(" 页")
```

## 目录（TOC）
```python
p = doc.add_paragraph()
run = p.add_run()
fb = OxmlElement('w:fldChar'); fb.set(qn('w:fldCharType'), 'begin')
ins = OxmlElement('w:instrText'); ins.set(qn('xml:space'), 'preserve')
ins.text = ' TOC \\o "1-3" \\h \\z \\u '
fs = OxmlElement('w:fldChar'); fs.set(qn('w:fldCharType'), 'separate')
t = OxmlElement('w:t'); t.text = "（打开文档后更新目录）"
fe = OxmlElement('w:fldChar'); fe.set(qn('w:fldCharType'), 'end')
for el in (fb, ins, fs, t, fe):
    run._r.append(el)

# 让 Word 打开时自动更新域（目录/页码）
upd = OxmlElement('w:updateFields'); upd.set(qn('w:val'), 'true')
doc.settings._element.append(upd)
```
前提：文档必须用内置 `Heading 1`~`Heading 3` 样式，TOC 才能正确抓取。

## 超链接
```python
def add_hyperlink(paragraph, url, text):
    r_id = paragraph.part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True)
    hl = OxmlElement('w:hyperlink'); hl.set(qn('r:id'), r_id)
    r = OxmlElement('w:r'); t = OxmlElement('w:t'); t.text = text
    r.append(t); hl.append(r)
    paragraph._p.append(hl)
```

## 编辑已有文档
```python
doc = Document("uploads/xxx.docx")       # python-docx 可直接打开修改
for p in doc.paragraphs:                 # 按样式遍历
    if p.style.name == "Heading 1":
        p.text = "新标题"
for table in doc.tables:
    for row in table.rows:
        row.cells[0].text = "新值"
doc.save("xxx_修改.docx")                 # 另存新文件，不要覆盖 uploads 原文件
```
读取已有 docx 内容也可直接用 `workspace_read` 工具（支持 .docx）。

## 生成后校验（必做）
保存后重新打开文件校验，再向用户交付：
```python
doc = Document("report.docx")
print("段落数:", len(doc.paragraphs), "| 表格数:", len(doc.tables))
for p in doc.paragraphs:
    if p.text.strip():
        print(p.style.name, p.text[:30])
```
检查：段落/表格数量符合预期、标题样式正确、内容非空。如需渲染成图核对版式，可用 `terminal` 调用 soffice 转 PDF（如 `/opt/homebrew/bin/soffice --headless --convert-to pdf report.docx`）。

## 规则
1. 输出文件必须保存到工作区根目录（绝对路径），返回路径供前端展示下载卡片；草稿/中间数据写 scratch 临时目录。
2. 不要用代码生成 PDF——PDF 导出使用 `pdf_export` 工具。
3. 表格使用 `style="Table Grid"` 以显示边框；固定列宽需同时设置 `table.autofit = False`、`columns[i].width` 与每格 `cell.width`。
4. 长文档先规划结构（标题层级），再逐节填充；标题必须用内置 Heading 样式。
5. 列表用 `List Bullet` / `List Number` 样式，禁止手工输入 `•` 或 `1.` 字符。
6. 中文文档显式设置 A4 页面；标题黑体、正文宋体（含 eastAsia 设置）。
7. 修改已有文件时另存为新文件（`xxx_修改.docx`），不要覆盖 uploads 原文件。
8. 生成后必须执行"生成后校验"步骤并如实报告结果。

## 数学公式规范（必须使用 Word 公式编辑器）
当文档中包含数学公式时，所有 LaTeX 公式必须转换为 **Word 公式编辑器原生格式（OMML / `m:oMath` 元素）** 保存，打开文档后公式是 Word 中可双击编辑的原生公式。

禁止：把公式写成纯文本（`E=mc^2`）、Unicode 数学符号（∫∑√π）、图片或截图。

转换步骤（**必须用微软官方 MML2OMML.XSL 转换器**，不要用 mathml2omml 库——它的输出是逐字符 run 且嵌套 `<m:box>`，在 Word 中会显示为大量空隙/框）：
1. 若 `latex2mathml` 未安装，先通过 `terminal` 安装：`pip install latex2mathml`
2. 在 `execute_code` 中使用**沙箱自动注入的 `omml_helper` 模块**（`.exec_tmp/omml_helper.py`，每次执行前由系统写入，已内置官方 XSL 转换 + nary 规整，禁止自行重写转换代码）：

```python
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), ".exec_tmp"))
from omml_helper import add_latex_equation
from docx import Document

doc = Document()
p = doc.add_paragraph("牛顿第二定律：")
add_latex_equation(p, r"F = m\,a")
p2 = doc.add_paragraph("积分公式：")
add_latex_equation(p2, r"\int_0^1 x^2\,dx = \frac{1}{3}")
doc.save("/path/to/output.docx")
```

注意：
- 行内公式与独立公式都用 `add_latex_equation` 插入；独立公式建议单独创建一个居中段落。
- 公式插入到表格单元格时，对 `cell.paragraphs[0]` 调用同一函数。
- 公式文本使用原始字符串 `r"..."` 防止 LaTeX 反斜杠被转义。
- 间距类命令（如 `\,`）经转换后视觉间距可能丢失，属正常现象，不影响公式结构。
- 转换失败（返回 False，或 stdout 出现 LATEX2OMML_FAILED）时必须如实告知用户，
  禁止把公式降级为纯文本写入文档。
- `omml_helper` 使用 `WAVETHINKER_MML2OMML_XSL` 环境变量（沙箱自动注入）定位官方 XSL 文件，
  无需自行查找路径；若 XSL 缺失会自动降级 mathml2omml（此时需在 stdout 中说明）。

