# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Citation style rendering service — 学术引用格式渲染器（CSL 引擎封装）。

设计稿：docs/CITATION_STYLE_SYSTEM_DESIGN.md
- 核心决策：[N] 是唯一面向模型的撰写语法；样式化渲染只发生在输出汇点。
- 引擎：citeproc-py（CSL 1.0.2）+ vendored 样式（backend/app/citation_styles/）。
- 文内五族：numeric-bracket（IEEE/Vancouver）、numeric-superscript（GB 顺序编码制/AMA）、
  author-date（APA/Harvard/Chicago-AD/GB 作者-年份制）、author-page（MLA）、
  notes（Chicago-NB/GB 注释制，首见全注、复见短注）。
- 审计修复语义内建：F1 strip_model_bibliography（剥模型自写参考节，含「参考来源」+冒号
  容错，三路径共享）；F2 issued 取条目 published_date；F3 只渲染正文实际引用的条目，
  零引用不出参考节。

纯函数 + 进程内样式缓存，无 I/O（样式文件读取除外）。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_STYLES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "citation_styles")

# 样式目录：逻辑 id → CSL 文件 / 文内族 / 展示元数据
STYLE_REGISTRY: Dict[str, Dict[str, Any]] = {
    "gb-t-7714-numeric": {
        "csl": "gb-t-7714-numeric.csl",
        "family": "numeric-superscript",
        "label": "GB/T 7714—2015 顺序编码制",
        "label_en": "GB/T 7714-2015 numeric",
        "locale": "zh-CN",
        "heading": "参考文献",
        "disciplines": "中国高校/期刊（全学科），中文课程论文、学位论文",
    },
    "gb-t-7714-author-date": {
        "csl": "gb-t-7714-author-date.csl",
        "family": "author-date",
        "label": "GB/T 7714—2015 著者-出版年制",
        "label_en": "GB/T 7714-2015 author-date",
        "locale": "zh-CN",
        "heading": "参考文献",
        "disciplines": "中国高校著者-出版年制要求",
    },
    "gb-t-7714-note": {
        "csl": "gb-t-7714-note.csl",
        "family": "notes",
        "label": "GB/T 7714—2015 注释制",
        "label_en": "GB/T 7714-2015 note",
        "locale": "zh-CN",
        "heading": "参考文献",
        "disciplines": "国标注释制要求",
    },
    "apa": {
        "csl": "apa.csl",
        "family": "author-date",
        "label": "APA 7th",
        "label_en": "APA 7th edition",
        "locale": "en-US",
        "heading": "References",
        "disciplines": "心理学/教育学/社科/护理/商科",
    },
    "mla": {
        "csl": "mla.csl",
        "family": "author-page",
        "label": "MLA 9th",
        "label_en": "MLA 9th edition",
        "locale": "en-US",
        "heading": "Works Cited",
        "disciplines": "文学/语言学/人文",
    },
    "chicago-notes": {
        "csl": "chicago-notes.csl",
        "family": "notes",
        "label": "Chicago 17th 注释-书目制",
        "label_en": "Chicago notes-bibliography",
        "locale": "en-US",
        "heading": "Bibliography",
        "disciplines": "历史/艺术史/神学",
    },
    "chicago-author-date": {
        "csl": "chicago-author-date.csl",
        "family": "author-date",
        "label": "Chicago 17th 作者-年制",
        "label_en": "Chicago author-date",
        "locale": "en-US",
        "heading": "References",
        "disciplines": "人类学/政治学/部分社科",
    },
    "ieee": {
        "csl": "ieee.csl",
        "family": "numeric-bracket",
        "label": "IEEE",
        "label_en": "IEEE Reference Guide",
        "locale": "en-US",
        "heading": "References",
        "disciplines": "工程/计算机/电子",
    },
    "vancouver": {
        "csl": "vancouver.csl",
        "family": "numeric-bracket",
        "label": "Vancouver (ICMJE/NLM)",
        "label_en": "Vancouver (ICMJE/NLM)",
        "locale": "en-US",
        "heading": "References",
        "disciplines": "医学/生物/临床",
    },
    "ama": {
        "csl": "ama.csl",
        "family": "numeric-superscript",
        "label": "AMA 11th",
        "label_en": "AMA 11th edition",
        "locale": "en-US",
        "heading": "References",
        "disciplines": "美国医学（JAMA 系）",
    },
    "harvard": {
        "csl": "harvard.csl",
        "family": "author-date",
        "label": "Harvard (Cite Them Right)",
        "label_en": "Harvard (Cite Them Right)",
        "locale": "en-US",
        "heading": "References",
        "disciplines": "英国/澳洲高校通用",
    },
}

DEFAULT_STYLE_ID = "gb-t-7714-numeric"

# [N] 引用标记（与 citation_ledger._CITE_RE 同族：排除 markdown 链接与引用标签）
_CITE_RE = re.compile(r"\[(\d{1,3})\](?![(:])")
# 围栏/行内代码——其中的 [N] 不是引用
_FENCE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")

# F1：模型自写参考文献节剥除（含「参考来源」+ 冒号容错——审计发现 ChatArea.vue 剥除
# 正则缺这两项导致笔记双参考节/自污染循环；此处为三路径共享的唯一口径）

# 审计 M1：行内「来源：XXX [N]」注记——剥前缀保 [N]（与 MessageBubble/StreamMarkdown
# 的 INLINE_SOURCE_REGEX 同口径），否则保存笔记残留行内来源行 + 参考文献节双重展示
INLINE_SOURCE_RE = re.compile(r"来源[：:]\s*[^\n\[]+?\s*\[(\d{1,3})\]")

# 参考文献节标题行（不带尾部贪婪——供 finditer 逐命中校验，A4.9 I2）
_BIB_HEADING_LINE_RE = re.compile(
    r"(?:^|\n)[^\S\n]*"
    r"(?:#{1,6}[^\S\n]*|\*{1,2}[^\S\n]*)?"
    r"(?:参考文献|参考资料|参考来源|References|Sources|Reference)"
    r"[：:]?[^\S\n]*(?:\*{1,2})?[^\S\n]*(?=\n)",
    re.IGNORECASE,
)


@dataclass
class RenderedCitations:
    """渲染结果：正文（[N] 已按样式族变换）+ 参考文献节 + 注释（notes 族）。"""
    content: str = ""
    bibliography_html: str = ""
    bibliography_markdown: str = ""
    bibliography_section: str = ""     # 可直接拼接到正文/笔记尾部的完整参考节
    notes: List[str] = field(default_factory=list)      # notes 族：逐出现的注释文本
    notes_section: str = ""            # notes 族：完整注释节（插在参考节前）
    style_id: str = ""
    family: str = ""
    used_ids: List[int] = field(default_factory=list)
    old_to_new: Dict[int, str] = field(default_factory=dict)


def strip_model_bibliography(content: str) -> str:
    """剥除模型自写参考文献节（标题行起到文末）。F1 统一口径。

    已知限制 #4 收紧（A4.9 I2/M2）：逐个标题命中做形态校验——标题后 800 字符内
    出现「[N] 条目行 + 4 位年份/URL」才认定参考节并剥除（剥第一个通过者）；
    「讲参考文献著录规范的文档」中段 References 标题（无条目）不再误杀，
    中段假标题 + 文末真参考节的组合也不再漏剥。"""
    if not content:
        return content
    for m in _BIB_HEADING_LINE_RE.finditer(content):
        # 形态校验：标题后首个非空行必须以 [N] 开头且同行含 4 位年份或 URL——
        # 真参考节的首个条目行形态；标题与条目行之间隔散文/空行的不认定。
        line_end = content.find("\n", m.end())
        if line_end == -1:
            line_end = len(content)
        rest = content[line_end:]
        m_entry = re.match(r"(?:[^\S\n]*\n)*[^\S\n]*\[\d{1,3}\](?P<line>[^\n]*)", rest)
        if not m_entry or not re.search(r"https?://|(?:19|20)\d{2}", m_entry.group("line")):
            continue
        head = content[:m.start()].rstrip()
        # 剥除紧邻的 --- 分隔线（参考节前的分隔线随节一起去掉）
        if head.endswith("---"):
            head = head[:-3].rstrip()
        return head
    return content


def _strip_inline_source_prefix(content: str) -> str:
    """剥行内「来源：XXX [N]」前缀保 [N]（审计 M1），代码围栏内不动。"""
    if not content:
        return content
    spans = list(_FENCE_RE.finditer(content))
    parts = []
    last = 0
    for m in spans:
        parts.append(INLINE_SOURCE_RE.sub(r"[\1]", content[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(INLINE_SOURCE_RE.sub(r"[\1]", content[last:]))
    return "".join(parts)


def list_styles() -> List[Dict[str, Any]]:
    """样式目录（供 API/UI）。"""
    out = []
    for sid, meta in STYLE_REGISTRY.items():
        out.append({
            "id": sid,
            "family": meta["family"],
            "label": meta["label"],
            "label_en": meta["label_en"],
            "locale": meta["locale"],
            "heading": meta["heading"],
            "disciplines": meta["disciplines"],
        })
    return out


def resolve_style_id(style_id: Optional[str] = None) -> str:
    """样式解析（存档汇点语义）：显式 > 配置默认 > gb-t-7714-numeric。
    论文/文件产物路径应在规划期解析并显式传入（设计 §4.1-A），此处仅兜底。"""
    if style_id and style_id in STYLE_REGISTRY:
        return style_id
    if style_id:
        logger.warning("unknown citation style %r — falling back to default", style_id)
    try:
        from app.core.config import get_config
        cfg = get_config()
        configured = getattr(cfg, "agent_citation_style", None) or (
            getattr(cfg, "agent_citation", {}) or {}
        ).get("style")
        if configured and configured in STYLE_REGISTRY:
            return configured
    except Exception:
        pass
    return DEFAULT_STYLE_ID


# ---------- 样式缓存 ----------

_style_cache: Dict[str, Any] = {}


def _styles_dir() -> str:
    """样式目录：配置覆盖（[agent.citation] styles_dir）> 内置 app/citation_styles/。"""
    try:
        from app.core.config import get_config
        override = getattr(get_config(), "agent_citation_styles_dir", "")
        if override and os.path.isdir(override):
            return override
    except Exception:
        pass
    return _STYLES_DIR


def _load_style(style_id: str) -> Any:
    cached = _style_cache.get(style_id)
    if cached is not None:
        return cached
    from citeproc import CitationStylesStyle

    meta = STYLE_REGISTRY[style_id]
    path = os.path.join(_styles_dir(), meta["csl"])
    style = CitationStylesStyle(path, validate=False)
    _style_cache[style_id] = style
    return style


# ---------- 台账条目 → CSL-JSON ----------

def _html_escape(value: str) -> str:
    """条目元数据（title/author/site_name 来自抓取网页，攻击者可控）进入
    citeproc HTML 输出前的边界转义（A4.9 ④：citeproc-py html formatter 不
    转义变量值，<script>/<img onerror> 会直通 bibliography_html）。"""
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_date_parts(value: str) -> Optional[Dict[str, Any]]:
    """'2025-03-01' / '2025-03' / '2025' / '2025年3月1日' → date-parts。"""
    if not value:
        return None
    m = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if m:
        return {"date-parts": [[int(m.group(1)), int(m.group(2)), int(m.group(3))]]}
    m = re.match(r"(\d{4})[-/.年](\d{1,2})", value)
    if m:
        return {"date-parts": [[int(m.group(1)), int(m.group(2))]]}
    m = re.match(r"(\d{4})", value)
    if m:
        return {"date-parts": [[int(m.group(1))]]}
    return None


def _parse_author(author: str) -> List[Dict[str, str]]:
    """'Family, Given; Family, Given' 或 '张三; 李四' → CSL name 列表。"""
    if not author:
        return []
    names = []
    for part in re.split(r"[;；]", author):
        part = part.strip()
        if not part:
            continue
        if "," in part:
            family, _, given = part.partition(",")
            names.append({"family": family.strip(), "given": given.strip()})
        else:
            names.append({"literal": part})
    return names


# 文献类型推断（确定性 URL 规则，设计 §6.2；不做学科判断）
_TYPE_INFER_RULES = (
    (re.compile(r"arxiv\.org|doi\.org|cnki\.net|wanfangdata|/journal|/article|/paper", re.I), "article-journal"),
    (re.compile(r"news\.|/news/|xinhuanet|people\.com|bbc\.|cnn\.|nytimes|thepaper|sina\.com|163\.com", re.I), "article-newspaper"),
    (re.compile(r"\.pdf(\?|$)", re.I), "report"),
)


def infer_csl_type(url: str) -> str:
    for pat, t in _TYPE_INFER_RULES:
        if pat.search(url or ""):
            return t
    return "webpage"


def _entry_to_csl(entry: Dict[str, Any], accessed: Tuple[int, int, int]) -> Dict[str, Any]:
    """台账/搜索条目 → CSL-JSON item。F2：issued 取 published_date，不从 URL 猜。"""
    url = (entry.get("url") or "").strip()
    etype = (entry.get("type") or "").strip() or infer_csl_type(url)
    item: Dict[str, Any] = {
        "id": str(entry.get("id") or url),
        "type": etype,
        "title": _html_escape((entry.get("title") or url).strip()),
        "URL": _html_escape(url),
        "accessed": {"date-parts": [list(accessed)]},
    }
    issued = _parse_date_parts((entry.get("published_date") or "").strip())
    if issued:
        item["issued"] = issued
    author = _parse_author(_html_escape((entry.get("author") or "").strip()))
    if author:
        item["author"] = author
    container = _html_escape((entry.get("site_name") or "").strip())
    if not container and url:
        m = re.match(r"https?://(?:www\.)?([^/]+)", url)
        if m:
            container = _html_escape(m.group(1))
    if container:
        item["container-title"] = container
    return item


# ---------- 围栏屏蔽替换 ----------

def _replace_outside_code(text: str, replacer) -> str:
    spans = list(_FENCE_RE.finditer(text))
    if not spans:
        return _CITE_RE.sub(replacer, text)
    parts = []
    last = 0
    for i, m in enumerate(spans):
        parts.append(_CITE_RE.sub(replacer, text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_CITE_RE.sub(replacer, text[last:]))
    return "".join(parts)


def _iter_cite_occurrences(text: str) -> List[Tuple[int, int, int]]:
    """正文（排除代码围栏）中 [N] 出现位置列表：(start, end, old_id)，按出现顺序。"""
    out: List[Tuple[int, int, int]] = []
    spans = list(_FENCE_RE.finditer(text))
    regions = []
    last = 0
    for m in spans:
        regions.append((last, m.start()))
        last = m.end()
    regions.append((last, len(text)))
    for a, b in regions:
        for m in _CITE_RE.finditer(text, a, b):
            out.append((m.start(), m.end(), int(m.group(1))))
    return out


def _short_note(entry: Dict[str, Any]) -> str:
    """注释族重复引用的短注：作者姓 + 短标题（Chicago 缩短式近似）。
    title/author 在此统一 _html_escape（A4.9 ④-b：短注曾绕过边界转义）。"""
    author = (entry.get("author") or "").strip()
    if author:
        first = re.split(r"[;；]", author)[0].strip()
        surname = first.split(",")[0].strip() if "," in first else first.split()[0] if first.split() else first
    else:
        surname = ""
    title = (entry.get("title") or "").strip()
    short_title = title[:18] + ("…" if len(title) > 18 else "")
    surname = _html_escape(surname)
    short_title = _html_escape(short_title)
    if surname:
        return f"{surname},《{short_title}》" if any("\u4e00" <= ch <= "\u9fff" for ch in title) else f'{surname}, "{short_title}"'
    return f"《{short_title}》" if any("\u4e00" <= ch <= "\u9fff" for ch in title) else f'"{short_title}"'


# ---------- 主入口 ----------

def render_citations(
    content: str,
    entries: Sequence[Dict[str, Any]],
    style_id: Optional[str] = None,
    accessed: Optional[Tuple[int, int, int]] = None,
    locale: Optional[str] = None,
) -> RenderedCitations:
    """把正文中的 [N] 角标按目标样式渲染为文内引用，并生成参考文献节。

    - 内容先剥模型自写参考节（F1），再抽取引用标记（F3：只有正文实际引用的
      条目进入参考文献表；零引用 → 不生成参考节，正文原样返回）。
    - 代码围栏/行内代码中的 [N] 不替换。
    - 编号重排由 CSL 按首次引用顺序同源生成（citeproc-py 注册顺序=首次出现顺序）。
    """
    content = content or ""
    sid = resolve_style_id(style_id)
    meta = STYLE_REGISTRY[sid]
    family = meta["family"]
    result = RenderedCitations(style_id=sid, family=family)

    stripped = _strip_inline_source_prefix(strip_model_bibliography(content))
    occurrences = _iter_cite_occurrences(stripped)
    entry_by_id = {int(e["id"]): e for e in entries if e.get("id") is not None and _is_int_id(e["id"])}
    used_ids = list(dict.fromkeys(old for _, _, old in occurrences if old in entry_by_id))
    result.used_ids = used_ids
    if not used_ids:
        # F3：零引用不出参考节；但 F1 仍生效——模型自写参考节必须剥掉，
        # 否则「双参考节」在零引用路径复活（A4.9 ③）。
        result.content = stripped
        return result

    accessed = accessed or (date.today().year, date.today().month, date.today().day)
    ordered_entries = [entry_by_id[i] for i in used_ids]

    try:
        from citeproc import Citation, CitationItem, CitationStylesBibliography
        from citeproc import formatter
        from citeproc.source.json import CiteProcJSON

        style = _load_style(sid)
        source = CiteProcJSON([_entry_to_csl(e, accessed) for e in ordered_entries])
        bib = CitationStylesBibliography(style, source, formatter.html)

        warn = lambda msg: logger.debug("citeproc warn: %s", msg)  # noqa: E731

        if family == "notes":
            _render_notes_family(result, stripped, occurrences, entry_by_id, bib, warn, meta, ordered_entries)
        else:
            # 非 notes 族：每条目一个 citation cluster（首次出现顺序 = CSL 编号顺序）
            in_text: Dict[int, str] = {}
            for e in ordered_entries:
                cit = Citation([CitationItem(str(e["id"]))])
                bib.register(cit)
                in_text[int(e["id"])] = str(bib.cite(cit, warn))
            result.old_to_new = {i: in_text[i] for i in used_ids}

            def repl(m):
                old = int(m.group(1))
                return in_text.get(old, m.group(0))

            result.content = _replace_outside_code(stripped, repl)
            bib.sort()
            entries_html = [str(x) for x in bib.bibliography()]
            result.bibliography_html = "\n\n".join(entries_html)
            result.bibliography_markdown = result.bibliography_html

        result.bibliography_section = _build_bibliography_section(meta, result)
    except Exception as exc:
        # 引擎失败 fail-safe：正文保留 [N] 原样（不重编号，避免与降级表错位——
        # A4.9 ③：重新编号会让 [N] 静默指错文献），模型自写参考节仍剥除；
        # 降级来源列表按条目原 id 编号，与正文 [N] 对齐。
        logger.warning("citation render failed for style %s: %s", sid, exc, exc_info=True)
        result.content = stripped
        result.bibliography_section = _fallback_section(ordered_entries)
        result.bibliography_html = result.bibliography_section
        result.bibliography_markdown = result.bibliography_section
        return result

    return result


def _is_int_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) or (isinstance(value, str) and value.isdigit())


def _render_notes_family(result, stripped, occurrences, entry_by_id, bib, warn, meta, ordered_entries):
    """注释-书目族：逐出现上标注号（1..m）+ 注释节（首见完整注，复见短注）+ 书目节。"""
    from citeproc import Citation, CitationItem

    notes: List[str] = []
    seen: Dict[int, int] = {}  # entry_id → 首见注号
    pieces = []
    last = 0
    for start, end, old in occurrences:
        if old not in entry_by_id:
            continue
        pieces.append(stripped[last:start])
        k = len(notes) + 1
        if old in seen:
            note_text = _short_note(entry_by_id[old])
        else:
            cit = Citation([CitationItem(str(old))])
            bib.register(cit)
            note_text = str(bib.cite(cit, warn)).strip()
            seen[old] = k
        notes.append(note_text)
        pieces.append(f"<sup>{k}</sup>")
        result.old_to_new.setdefault(old, str(k))
        last = end
    pieces.append(stripped[last:])
    result.content = "".join(pieces)
    result.notes = notes
    lines = [f"{k}. {t}" for k, t in enumerate(notes, 1)]
    result.notes_section = "\n\n---\n\n## 注释\n\n" + "\n\n".join(lines)
    bib.sort()
    entries_html = [str(x) for x in bib.bibliography()]
    result.bibliography_html = "\n\n".join(entries_html)
    result.bibliography_markdown = result.bibliography_html


def _build_bibliography_section(meta: Dict[str, Any], result: RenderedCitations) -> str:
    if not result.bibliography_html:
        return ""
    heading = meta.get("heading") or "参考文献"
    section = f"\n\n---\n\n## {heading}\n\n{result.bibliography_html}"
    if result.notes_section:
        # 注释节在书目之前（Chicago-NB：notes → bibliography）
        return result.notes_section + section
    return section


def _fallback_section(entries: Sequence[Dict[str, Any]]) -> str:
    """引擎失败的降级来源列表（不自称任何标准格式）。按条目原 id 编号，
    与正文未重编的 [N] 对齐（A4.9 ③）。"""
    lines = []
    for e in entries:
        eid = int(e.get("id") or 0)
        title = _html_escape((e.get("title") or e.get("url") or "").strip())
        url = _html_escape((e.get("url") or "").strip())
        pub = (e.get("published_date") or "").strip()
        tail = f" ({pub})" if pub else ""
        lines.append(f"[{eid}] {title} — {url}{tail}")
    if not lines:
        return ""
    return "\n\n---\n\n## 参考来源\n\n" + "\n\n".join(lines)
