# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Browser 来源抽取 + 古籍书目轻量提取（引文体系改造 M2）。

live（agent_loop._apply_citation_ledger）与 persistence（chat.py.
_transform_tool_loop_results）共用本模块：同一解析器、同一调用顺序 → 同一
URL 序列 → live 台账编号与落库 results[] 逐位对齐（「入账即定号」）。

两种 browser 结果形态（自 chat.py 迁入并扩展）：
  - JSON: {"pages": [{url, title, text}, ...]} —— 全保真，可抽取书目项。
  - <tool-digest> 信封: 头部 【目标】 行 + digest 体 `## 来源` 段（仅头部/来源
    段被扫描——正文可能内嵌任意链接）。

书目抽取为**轻量、宁缺毋滥**（GB/T 3792.7 信息源=正文首卷卷端；页眉/题跋的
「作者.书名[M].刻本.地:者,年」形态）：抽不到就留空，绝不猜。
"""
import json
import re
from typing import Dict, List, Tuple

from app.services.citation_ledger import normalize_url

_BROWSER_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)）】]+")
_DIGEST_HEADER_RE = re.compile(r"<tool-digest>([\s\S]*?)<digest>", re.IGNORECASE)
# conv 3b58af5b wave-2: real browser digests carry fetched targets in a
# `## 来源` section INSIDE the digest body as backticked URLs with titles
# (`https://host/path` (标题)) — deterministic evidence, unlike arbitrary
# links elsewhere in the body.
_DIGEST_SOURCES_RE = re.compile(r"##\s*来源\s*\n([\s\S]*?)(?=\n##\s|\Z)")
_SOURCE_LINE_RE = re.compile(r"`(https?://[^\s`]+)`(?:\s*[（(]([^）)]*)[）)])?")

# ---------- 古籍书目轻量抽取（M2） ----------

_CITE_MARK_TYPES = {
    "M": "book", "J": "article-journal", "N": "article-newspaper",
    "C": "paper-conference", "D": "thesis", "S": "standard", "P": "patent",
    "R": "report", "DB": "dataset", "A": "manuscript",
}

# 「作者.书名[M].刻本.地:者,年」形态（GB/T 7714 引文串；行内出现即可）
_GB_INLINE_RE = re.compile(
    r"(?P<author>[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z·．\s]{0,20}?)\s*[.。]\s*"
    r"[《]?(?P<title>[^\[\]《》\n，,。;；]{2,60}?)[》]?\s*"
    r"[\[［](?P<mark>[A-Z]{1,2})[\]］](?P<rest>[^\n]{0,160})"
)
_EDITION_RE = re.compile(
    r"(刻本|写本|抄本|活字本|影印本|石印本|铅印本|稿本|点校本|整理本|明刻本|清刻本)"
)
_VOLUME_RE = re.compile(r"(卷[一二三四五六七八九十百千\d]{1,4})")
_PUB_RE = re.compile(
    r"(?P<place>[\u4e00-\u9fff]{2,10})\s*[:：]\s*"
    r"(?P<publisher>[\u4e00-\u9fffA-Za-z0-9·．\-]{1,30}?)\s*[,，]\s*"
    r"(?P<year>(?:1[0-9]{3}|20[0-9]{2})|[\u4e00-\u9fff]{2,12}\[?(?:1[0-9]{3}|20[0-9]{2})?\]?)"
)

_HOST_HINTS = (
    (re.compile(r"shidianguji\.com", re.I), {"container": "识典古籍", "source_type": "book", "genre": "古籍"}),
    (re.compile(r"ctext\.org", re.I), {"container": "中国哲学书电子化计划", "source_type": "book", "genre": "古籍"}),
    (re.compile(r"zh\.wikisource\.org", re.I), {"container": "维基文库", "source_type": "book", "genre": "古籍"}),
)


def extract_host_hints(url: str) -> Dict[str, str]:
    for pat, hints in _HOST_HINTS:
        if pat.search(url or ""):
            return dict(hints)
    return {}


def extract_biblio_hints(text: str) -> Dict[str, str]:
    """从页面正文/digest 文首抽取 GB/T 7714 引文串形态的书目项（轻量）。"""
    out: Dict[str, str] = {}
    m = _GB_INLINE_RE.search(text or "")
    if not m:
        return out
    rest = m.group("rest") or ""
    author = m.group("author").strip()
    title = m.group("title").strip()
    if author:
        out["author"] = author
    if title:
        out["title"] = title
    mark = (m.group("mark") or "").strip().upper()
    csl_type = _CITE_MARK_TYPES.get(mark)
    if csl_type:
        out["type"] = csl_type
        out["source_type"] = (
            "book" if csl_type == "book"
            else "periodical" if csl_type.startswith("article")
            else "archive" if csl_type == "manuscript"
            else "web"
        )
    ed = _EDITION_RE.search(rest)
    if ed:
        out["edition"] = ed.group(1)
        out.setdefault("genre", "古籍")
        out["source_type"] = "book"
    vol = _VOLUME_RE.search(rest)
    if vol:
        out["volume"] = vol.group(1)
    pm = _PUB_RE.search(rest)
    if pm:
        out["publisher_place"] = pm.group("place").strip()
        out["publisher"] = pm.group("publisher").strip()
        y = re.search(r"\d{4}", pm.group("year") or "")
        if y:
            out["year"] = y.group(0)
    return out


def _merge_biblio(base: Dict[str, str], extra: Dict[str, str]) -> Dict[str, str]:
    out = dict(base)
    for k, v in (extra or {}).items():
        if v and not out.get(k):
            out[k] = v
    return out


# ---------- browser 来源抽取（自 chat.py 迁入） ----------

def extract_browser_sources(raw_result: str) -> List[Tuple[str, str, str, Dict[str, str]]]:
    """Yield (url, title, snippet, biblio) citation candidates from a browser
    tool result. browser-fetched pages are real sources the model reads and
    reasons over — they must join the ledger like web_search hits.

    Two result shapes exist:
      - JSON: {"pages": [{url, title, text}, ...]} — full fidelity.
      - <tool-digest> envelope: URLs live in the header block (来源/目标
        lines); the digest body may embed arbitrary content links, so ONLY
        the header is scanned.
    """
    raw = raw_result or ""
    if not raw:
        return []
    if raw.startswith("<tool-digest>"):
        m = _DIGEST_HEADER_RE.search(raw)
        if not m:
            # malformed envelope (no <digest> marker) — the body may embed
            # arbitrary content links, so scanning it would violate the
            # header-only contract. Register nothing.
            return []
        collected: List[Tuple[str, str, str, Dict[str, str]]] = []
        seen: set = set()

        def _collect_url(u: str, title: str = "", hint_text: str = ""):
            u = u.rstrip(".,;、，。")
            key = normalize_url(u)
            if not u or key in seen:
                return
            seen.add(key)
            biblio = _merge_biblio(extract_host_hints(u), extract_biblio_hints(hint_text))
            collected.append((u, title or _derive_title_from_url(u), "", biblio))

        # A) envelope header 【目标】 line(s) — single/multi declared targets.
        for line in m.group(1).splitlines():
            if "【目标】" in line:
                for u in _BROWSER_URL_RE.findall(line):
                    _collect_url(u)
        # B) digest body `## 来源` section — the summarizer's own declared
        # fetched targets. Two observed layouts: backticked URLs with
        # optional (标题), and plain `目标：<url>` lines (older summaries).
        sm = _DIGEST_SOURCES_RE.search(raw[m.end():])
        if sm:
            section_text = sm.group(1)
            for line in section_text.splitlines():
                if "`http" in line:
                    for bm in _SOURCE_LINE_RE.finditer(line):
                        _collect_url(bm.group(1), (bm.group(2) or "").strip(), section_text[:3000])
                elif "目标" in line:
                    for u in _BROWSER_URL_RE.findall(line):
                        _collect_url(u, hint_text=section_text[:3000])
        return collected
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    pages = parsed.get("pages") if isinstance(parsed, dict) else None
    if not isinstance(pages, list):
        return []
    out: List[Tuple[str, str, str, Dict[str, str]]] = []
    for p in pages:
        if not isinstance(p, dict):
            continue
        url = p.get("url") or ""
        if not url.startswith("http"):
            continue
        # errored pages ({url, error} entries) are dead links — they must
        # not become citation badge sources.
        if p.get("error"):
            continue
        text = (p.get("text") or "").strip()
        biblio = _merge_biblio(
            extract_host_hints(url),
            extract_biblio_hints(text[:3000]),
        )
        out.append((
            url,
            (p.get("title") or "").strip() or _derive_title_from_url(url),
            text[:300],
            biblio,
        ))
    return out


def _derive_title_from_url(url: str):
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            return parts[-1][:100]
        return parsed.netloc or url
    except Exception:
        return url
