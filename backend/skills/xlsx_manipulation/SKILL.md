<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

---
name: xlsx_manipulation
description: 生成、读取或修改 Excel 表格（.xlsx/.xlsm），通过 execute_code 调用 openpyxl/pandas，支持公式、格式、条件格式、图表、数据验证
category: office
---

# xlsx_manipulation — Excel 表格生成与编辑

## 适用场景
- 用户要求生成或修改 Excel 表格（`.xlsx`/`.xlsm`）
- 需要带公式、格式、条件格式、图表、数据验证的数据表
- 分析/清洗用户上传的表格文件（`uploads/` 目录）

## 用法
通过 `execute_code` 调用 `openpyxl`（预装，版本 3.1.5；`pandas` 3.x 也已预装）生成代码并执行，输出 `.xlsx` 文件保存到**工作区根目录**（用绝对路径，写在工作区根目录的文件才会成为下载卡片）。

## 核心 API

### 1. 新建 / 打开 / 读取
```python
from openpyxl import Workbook, load_workbook

# 新建
wb = Workbook(); ws = wb.active; ws.title = "数据"

# 打开已有文件（编辑/修改用户上传的文件：uploads/ 目录）
wb = load_workbook("uploads/xxx.xlsx")          # 保留公式
ws = wb["Sheet1"]  # 或 wb.active

# 两遍读取：第 1 遍拿公式，第 2 遍 data_only=True 拿 Excel 缓存的计算结果
wb_f = load_workbook("uploads/xxx.xlsx")                  # ws["D2"].value -> "=SUM(...)"
wb_v = load_workbook("uploads/xxx.xlsx", data_only=True)  # ws["D2"].value -> 数值或 None
```
注意：openpyxl **不计算**公式，`data_only=True` 只能读出 Excel 曾计算过的缓存值；新写入的公式该值是 `None`（属正常现象）。

### 2. 写入与读取单元格
```python
ws["A1"] = "标题"; ws.cell(row=2, column=2, value=42)
ws.append(["姓名", "年龄", "城市"]); ws.append(["张三", 28, "北京"])
value = ws["A1"].value
for row in ws.iter_rows(min_row=1, max_row=10, min_col=1, max_col=5):
    for cell in row: print(cell.value)
```

### 3. 样式
```python
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

cell = ws["A1"]
cell.font = Font(name="SimHei", bold=True, size=12, color="FFFFFF")
cell.fill = PatternFill("solid", fgColor="4472C4")          # 表头蓝
cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
thin = Side(style="thin", color="000000")
cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
cell.number_format = "¥#,##0.00"        # 货币；百分比用 "0.00%"；日期用 "YYYY-MM-DD"
```

### 4. 行列操作
```python
ws.column_dimensions["A"].width = 15
ws.row_dimensions[1].height = 24
ws.freeze_panes = "A2"            # 冻结首行
ws.auto_filter.ref = "A1:D100"    # 筛选
ws.merge_cells("F1:G1"); ws["F1"] = "合并标题"
```
openpyxl 无法自动适配列宽，按内容长度估算（中文约 2 个英文字符宽度）。

### 5. 公式
```python
ws["D2"] = "=SUM(B2:C2)"
ws["E2"] = "=IF(D2>10000,\"达标\",\"不达标\")"
```
公式以 `=` 开头、保持为公式字符串（禁止硬编码计算结果）；由 Excel 打开时自动计算。

### 6. 条件格式
```python
from openpyxl.formatting.rule import ColorScaleRule, CellIsRule

ws.conditional_formatting.add("B2:B100", CellIsRule(
    operator="greaterThan", formula=["12000"],
    fill=PatternFill("solid", fgColor="FFCCCC")))
ws.conditional_formatting.add("B2:B100", ColorScaleRule(
    start_type="min", start_color="FFFFFF", end_type="max", end_color="00FF00"))
```

### 7. 数据验证（下拉列表）
```python
from openpyxl.worksheet.datavalidation import DataValidation
dv = DataValidation(type="list", formula1='"是,否"', allow_blank=True)
dv.error = "请从列表选择"; dv.errorTitle = "输入无效"
ws.add_data_validation(dv); dv.add("E2:E100")
```

### 8. 图表
```python
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
data = Reference(ws, min_col=2, min_row=1, max_col=2, max_row=100)  # 含表头
cats = Reference(ws, min_col=1, min_row=2, max_row=100)
bar = BarChart(); bar.type = "col"          # "col" 柱状 / "bar" 横向
bar.title = "标题"; bar.add_data(data, titles_from_data=True); bar.set_categories(cats)
ws.add_chart(bar, "H2")
# LineChart / PieChart 用法相同（PieChart 只有单系列）
```

### 9. 多 Sheet 结构
```python
ws2 = wb.create_sheet("汇总")               # 或 create_sheet("汇总", 0) 指定位置
del wb["Sheet1"]; wb.move_sheet("汇总", offset=0)
```
复杂报表建议"原始数据 sheet + 汇总 sheet + 图表 sheet"分层，而不是塞进一个 sheet。

### 10. 批量数据（大数据量用 pandas）
```python
import pandas as pd
df = pd.DataFrame({"月份": ["1月", "2月"], "销售额": [12000, 15000]})
with pd.ExcelWriter("report.xlsx", engine="openpyxl") as writer:
    df.to_excel(writer, index=False, sheet_name="数据")
```
超过几百行时优先 pandas / `ws.append` 逐行追加，避免逐单元格赋值。

## 生成后校验（必做）
保存后重新打开文件做一次校验，再向用户交付：
```python
from openpyxl import load_workbook
wb = load_workbook("report.xlsx")
ws = wb.active
print(ws.max_row, ws.max_column, wb.sheetnames)
# 逐个扫描公式单元格，检查有无错误值残留（=#REF!、=#DIV/0!、=#VALUE!、=#N/A 等字符串开头）
for row in ws.iter_rows():
    for c in row:
        if isinstance(c.value, str) and c.value.startswith("="):
            assert "ERROR" not in c.value.upper(), f"公式疑似错误: {c.coordinate}"
```

## 规则
1. 最终交付文件必须写入**工作区根目录**（绝对路径），返回路径供前端展示下载卡片；草稿/中间数据写 scratch 临时目录即可。
2. 中文表格字体规范：标题用 SimHei（黑体）、正文用 SimSun（宋体）。xlsx 中字体名只是字符串，由用户端 Excel 解析，直接设置即可。
3. 修改用户上传的文件：先 `load_workbook("uploads/xxx.xlsx")` 读取，另存为工作区根目录新文件（如 `xxx_修改.xlsx`），不要覆盖 uploads 原文件。
4. 不要用代码生成 PDF——PDF 导出使用 `pdf_export` 工具。
5. 公式保持为公式（`=` 开头字符串），禁止把计算结果硬编码为常量。
6. 生成含公式的文件后必须执行"生成后校验"步骤并如实报告结果。
