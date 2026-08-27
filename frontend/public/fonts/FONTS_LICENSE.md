<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# 字体许可（frontend/public/fonts/）

本目录自托管两款开源字体（从 Google Fonts 镜像至本地，运行时不产生任何第三方
CDN 请求），均受 **SIL Open Font License 1.1 (OFL-1.1)** 许可：

| 字体 | 版本（name table） | 文件数 | 大小 | 版权（name table #0） | 许可指针（name table #14） |
|---|---|---|---|---|---|
| Inter | 4.001 | 7 | 214 KB | Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter) | https://openfontlicense.org |
| Noto Sans SC | 2.004-H2 | 101 | 4,409 KB | (c) 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source' | http://scripts.sil.org/OFL |

- **来源**：Google Fonts css2 API，2026-08-27 以 Chrome/126 UA 抓取
  `family=Inter:wght@400;500;600&family=Noto+Sans+SC:wght@400;500;600&display=swap`；
  两款在此用量下均为变量字体，按 unicode-range 分片（ Inter 7 片 + Noto 101 片），
  请求的 3 个字重由同一分片文件的 wght 轴覆盖。
- **命名**：`{Family}-{sha1(css2字体URL)前10位}.woff2`，与 `fonts.css` 内
  @font-face 一一对应；**不要单独删除某个分片**（unicode-range 覆盖会残缺），
  整体更新须重新抓取并生成 fonts.css（脚本见 README.md）。
- **再分发义务（OFL §2）**：本目录随软件分发时须同时分发上表的版权通知与下文
  OFL-1.1 全文——即保留 `FONTS_LICENSE.md` 于 `frontend/public/fonts/`。
- **保留字体名（OFL §3）**：'Source' 为 Adobe 保留名（Noto Sans SC 元数据声明），
  修改字体版本时不得以 'Source' 命名。
- 机器可读许可字段（name table #13/#14）已嵌入各 woff2 文件内。

---

## SIL OPEN FONT LICENSE Version 1.1（上表两款字体均适用）

```
Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter)
(c) 2014-2021 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'.

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
https://scripts.sil.org/OFL


-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```
