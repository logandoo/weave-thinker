<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Citation styles — 出处与许可

本目录的 CSL 样式文件经 `citeproc-py-styles` 包（`citeproc_styles/styles/`）复制固化，
上游为 Citation Style Language 官方样式仓 <https://github.com/citation-style-language/styles>
（众包维护，CC BY-SA 3.0 许可）。

固化目的（设计 §9）：锁定已通过黄金样例（`tests/test_citation_style_service.py`）验证的
样式版本，防 CSL 仓升级漂移。更新样式 = 重新复制 + 重跑黄金样例。

| 本目录文件 | 上游文件 | 样式 |
|---|---|---|
| gb-t-7714-numeric.csl | china-national-standard-gb-t-7714-2015-numeric.csl | GB/T 7714—2015 顺序编码制 |
| gb-t-7714-author-date.csl | china-national-standard-gb-t-7714-2015-author-date.csl | GB/T 7714—2015 著者-出版年制 |
| gb-t-7714-note.csl | china-national-standard-gb-t-7714-2015-note.csl | GB/T 7714—2015 注释制 |
| apa.csl | apa.csl | APA 7th |
| mla.csl | modern-language-association.csl | MLA 9th |
| chicago-notes.csl | chicago-notes-bibliography.csl | Chicago 注释-书目制 |
| chicago-author-date.csl | chicago-author-date.csl | Chicago 作者-年制 |
| ieee.csl | ieee.csl | IEEE |
| vancouver.csl | nlm-citation-sequence-brackets.csl | Vancouver（ICMJE/NLM 方括号编号） |
| ama.csl | american-medical-association.csl | AMA 11th |
| harvard.csl | harvard-cite-them-right.csl | Harvard (Cite Them Right) |

样式引擎：citeproc-py 0.11.1（CSL 1.0.2，MIT 许可）。
