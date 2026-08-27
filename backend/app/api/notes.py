# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import csv
import io
import re
import os
import mimetypes
import zipfile
import subprocess
import tempfile
import base64
from pathlib import Path
from urllib.parse import quote, urlparse, unquote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from typing import List

from app.db.database import get_db, User, Notebook, Note
from app.services.workspace_service import ensure_user_workspace
from app.schemas.notes import (
    NotebookCreate,
    NotebookUpdate,
    NotebookResponse,
    NoteCreate,
    NoteUpdate,
    NoteResponse,
    NoteListItem,
    QuickNoteCreate,
    NotebookBulkDelete,
    NotebookBulkExport,
    NoteBulkDelete,
    NoteBulkExport,
    BulkDeleteResponse,
    NoteMoveRequest,
    NoteBulkMoveRequest,
    BulkMoveResponse,
    NoteSearchResult,
)
from app.core.deps import get_current_user

import logging
import markdown as md_lib
from markdown_it import MarkdownIt as _MarkdownIt
from app.services.pdf_fonts import get_font_config_and_css

logger = logging.getLogger(__name__)

# CommonMark-compliant Markdown renderer used for PDF export.  python-markdown
# flattens nested lists with 3-space indentation (GFM/CommonMark standard),
# while markdown-it-py handles them correctly – matching the frontend (marked.js).
_md_it = _MarkdownIt("commonmark", {"html": True}).enable("table")

router = APIRouter(prefix="/api/notes", tags=["notes"])


def get_content_preview(content: str, max_length: int = 100) -> str:
    text = content.strip()
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


# Audio/video elements are not representable in exported Markdown or PDF
# (WeasyPrint cannot render them). Per product decision, exported files omit
# media resources entirely: strip <audio>/<video>/<source>/<track> tags
# before building the export. Images, links, and all other content survive.
# Stripping uses a tolerant HTML tokenizer (not regex): it survives
# unbalanced pairs, unclosed tags, multiline/attribute edge cases, and
# preserves <source>/<track> inside <picture> (responsive images). Fenced
# and inline code are protected first so code examples are never destroyed.
from html.parser import HTMLParser as _HTMLParser

_MEDIA_STRIP_FENCE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")


class _MediaStripper(_HTMLParser):
    """Drop <audio>/<video> elements (content included) and bare
    <source>/<track> tags. An unclosed media element drops to EOF; a stray
    closing tag is discarded. <source>/<track> inside <picture> survive."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.skip_depth = 0       # >0 while inside audio/video
        self.picture_depth = 0    # >0 while inside picture

    def _emit(self, text: str) -> None:
        if self.skip_depth == 0:
            self.out.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("audio", "video", "iframe"):
            self.skip_depth += 1
            return
        if tag == "picture":
            self.picture_depth += 1
        elif tag in ("source", "track") and self.picture_depth == 0:
            return
        self._emit(self.get_starttag_text() or f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in ("audio", "video", "iframe", "source", "track"):
            return
        self._emit(self.get_starttag_text() or f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("audio", "video", "iframe"):
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if tag == "picture" and self.picture_depth > 0:
            self.picture_depth -= 1
        self._emit(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def handle_entityref(self, name: str) -> None:
        self._emit(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._emit(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self._emit(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self._emit(f"<!{decl}>")


def _strip_media_tags(content: str) -> str:
    """Remove audio/video HTML elements so exports never contain media."""
    if not content:
        return content
    low = content.lower()
    if not any(t in low for t in (
        "<audio", "<video", "<source", "<track", "</audio", "</video", "<iframe",
    )):
        return content
    # Protect fenced/inline code blocks so code examples survive.
    protected: list[str] = []

    def _save(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"@@MEDIAPROT{len(protected) - 1}@@"

    content = _MEDIA_STRIP_FENCE_RE.sub(_save, content)
    parser = _MediaStripper()
    parser.feed(content)
    parser.close()
    out = "".join(parser.out)
    for i, frag in enumerate(protected):
        out = out.replace(f"@@MEDIAPROT{i}@@", frag)
    return out


def estimate_tokens(content: str) -> int:
    """Rough token estimate for the note-reference picker.

    Approximates cl100k / tiktoken behaviour without requiring the
    `tiktoken` dependency (which is ~10MB and slow to import):
      * CJK Unified Ideographs (U+4E00-U+9FFF) and CJK symbols
        (U+3000-U+303F, U+FF00-U+FFEF) → 1 token per character.
      * Everything else → 1 token per ~4 characters.

    This is intentionally slightly conservative on the high side so
    users aren't surprised by over-budget behaviour after selecting
    notes.
    """
    if not content:
        return 0
    cjk = 0
    other = 0
    for ch in content:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3000 <= cp <= 0x303F
            or 0xFF00 <= cp <= 0xFFEF
            or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
        ):
            cjk += 1
        elif not ch.isspace():
            other += 1
    # 4 non-CJK chars ≈ 1 token; round up so a short English phrase
    # still counts as at least 1 token.
    return cjk + (other + 3) // 4


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
    if not name:
        name = 'untitled'
    return name[:100]


import json as _json_mod
def _json_dumps_safe(obj) -> str:
    return _json_mod.dumps(obj, ensure_ascii=False)

_PDF_FONT_FAMILY = (
    "'Noto Sans CJK SC', 'Noto Sans Mono CJK SC', "
    "'Times New Roman', 'Songti SC', 'STSong', 'SimSun', 'Songti TC', "
    "'Noto Serif CJK SC', 'Noto Serif CJK TC', "
    "'Heiti SC', 'STHeiti', 'SimHei', "
    "'Noto Sans CJK TC', 'PingFang SC', "
    "'Microsoft YaHei', 'WenQuanYi Micro Hei', "
    "serif, monospace"
)

# For SVG text elements, WeasyPrint does not properly resolve
# multi-font fallback chains (it picks one font for the entire
# text run).  Listing the Chinese serif font first ensures that
# both Chinese and English characters inside the same SVG node
# are rendered with a single consistent serif face.
_SVG_FONT_FAMILY = (
    "'Noto Sans CJK SC', 'Times New Roman', 'STSong', 'SimSun', 'Songti TC', "
    "'Noto Serif CJK SC', 'Noto Serif CJK TC', "
    "'Heiti SC', 'STHeiti', 'SimHei', "
    "'Noto Sans CJK TC', 'PingFang SC', "
    "'Microsoft YaHei', 'WenQuanYi Micro Hei', "
    "serif"
)

_PDF_CSS = f"""\
@page {{ size: A4; margin: 2cm; }}
body {{ font-family: {_PDF_FONT_FAMILY}; font-size: 12pt; line-height: 1.6; color: #333; }}
h1 {{ font-size: 20pt; border-bottom: 1px solid #ccc; padding-bottom: 4pt; }}
h2 {{ font-size: 16pt; }}
h3 {{ font-size: 14pt; }}
/* Long code lines must wrap inside the fixed PDF page; otherwise the right
 * edge is clipped. ``white-space: pre-wrap`` preserves source newlines and
 * still wraps long lines, and ``overflow-wrap/word-break`` make sure even
 * unbroken tokens (URLs, long identifiers) wrap instead of overflowing. */
pre {{
  background: #f5f5f5;
  padding: 8pt;
  border-radius: 4pt;
  font-size: 10pt;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  word-break: break-word;
  max-width: 100%;
}}
pre code {{
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  word-break: break-word;
  display: block;
}}
code {{
  background: #f5f5f5;
  padding: 1pt 3pt;
  border-radius: 2pt;
  font-size: 10pt;
  word-break: break-word;
  overflow-wrap: anywhere;
}}
blockquote {{ border-left: 3pt solid #ccc; margin-left: 0; padding-left: 12pt; color: #666; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 6pt 8pt; text-align: left; }}
th {{ background: #f5f5f5; }}
.meta {{ color: #999; font-size: 9pt; margin-bottom: 12pt; }}
.mermaid-diagram {{ margin: 12pt 0; text-align: center; page-break-inside: avoid; break-inside: avoid-page; overflow: visible; }}
.mermaid-diagram svg, .mermaid-diagram img {{ display: block; margin: 0 auto; max-width: 100%; height: auto; page-break-inside: avoid; break-inside: avoid-page; }}
.echarts-diagram {{ margin: 12pt 0; text-align: center; page-break-inside: avoid; break-inside: avoid-page; overflow: visible; }}
.echarts-diagram svg, .echarts-diagram img {{ display: block; margin: 0 auto; max-width: 100%; height: auto; page-break-inside: avoid; break-inside: avoid-page; }}
.math-inline {{ display: inline-block; vertical-align: middle; }}
.math-inline svg {{ vertical-align: middle; height: 1.05em; width: auto; }}
.math-display {{ display: block; text-align: center; margin: 10pt 0; page-break-inside: avoid; }}
.math-display svg {{ display: inline-block; height: auto; max-width: 100%; }}
/* PDF bookmark generation from heading IDs so the exported PDF has a
 * navigable outline and internal ``<a href="#id">`` links work. */
h1[id], h2[id], h3[id], h4[id], h5[id], h6[id] {{
  string-set: current-heading content();
}}
h1 {{ bookmark-level: 1; bookmark-state: open; }}
h2 {{ bookmark-level: 2; bookmark-state: open; }}
h3 {{ bookmark-level: 3; bookmark-state: open; }}
h4 {{ bookmark-level: 4; bookmark-state: open; }}
h5 {{ bookmark-level: 5; bookmark-state: open; }}
h6 {{ bookmark-level: 6; bookmark-state: open; }}
a {{ color: #2563eb; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
/* --- Image styling ---
 * Images in the editor may carry inline styles (width, height, max-width,
 * margin, display, float) set via the resize/alignment toolbar.  The PDF
 * renderer must (a) respect those inline constraints, (b) prevent any image
 * from overflowing the A4 page, and (c) keep the image and its surrounding
 * text together when possible. */
/* Media elements are stripped before export; hide any stragglers. */
audio, video, source, track {{ display: none !important; }}
img {{
  max-width: 100% !important;
  height: auto !important;
  display: block;
  margin: 10pt 0;
  page-break-inside: avoid;
}}
/* Stand-alone block image inside a paragraph (common Markdown output).
 * Remove the paragraph's normal text-indent / margin so the image sits
 * flush against its container. */
p > img:only-child {{
  margin: 12pt 0;
}}
/* When an image is left-aligned with `float: left` or `display: inline`
 * (inline/small-icon use), let it sit inline without forcing block. */
img[style*="float:left"], img[style*="float: left"],
img[style*="display:inline"], img[style*="display: inline"],
img[style*="float:right"], img[style*="float: right"] {{
  display: inline !important;
  margin: 0 8pt 4pt 0 !important;
  max-width: 48% !important;
}}
img[style*="float:right"], img[style*="float: right"] {{
  margin: 0 0 4pt 8pt !important;
}}
/* Centred image: markdown-it-py may wrap in <p>, but inline style
 * `display:block;margin:0 auto` from the editor should still work. */
img[style*="margin-left:auto"], img[style*="margin-right:auto"],
img[style*="margin: 0 auto"], img[style*="margin:0 auto"] {{
  margin-left: auto !important;
  margin-right: auto !important;
}}
/* When a paragraph has text-align:center, centre the image inside it. */
p[style*="text-align:center"] > img,
p[style*="text-align: center"] > img,
div[style*="text-align:center"] > img,
div[style*="text-align: center"] > img {{
  margin-left: auto !important;
  margin-right: auto !important;
}}
/* Similarly for right-aligned containers. */
p[style*="text-align:right"] > img,
p[style*="text-align: right"] > img,
div[style*="text-align:right"] > img,
div[style*="text-align: right"] > img {{
  margin-left: auto !important;
  margin-right: 0 !important;
}}
/* Paragraph spacing */
p {{ margin: 6pt 0; }}
"""


def _strip_foreign_objects(svg: str) -> str:
    """Convert mermaid's foreignObject/HTML labels into native SVG <text>.

    Mermaid renders node labels inside ``<foreignObject>`` containers
    holding HTML spans.  WeasyPrint cannot render foreignObject, so we
    extract the inner text and emit a ``<text>`` element at the same
    position so the diagram stays readable in the PDF. Line breaks
    (``<br>``/``<br/>``) and HTML paragraph boundaries inside the label
    are preserved as separate ``<tspan>`` rows so multi-line labels stay
    multi-line in the rendered PDF.
    """
    def _replace(match: re.Match) -> str:
        inner_raw = match.group(2) or ''
        # Normalise <br> variants / <p>/<div> block boundaries to newlines.
        inner_norm = re.sub(r'<br\s*/?>', '\n', inner_raw, flags=re.IGNORECASE)
        inner_norm = re.sub(r'</(p|div)>', '\n', inner_norm, flags=re.IGNORECASE)
        inner_norm = re.sub(r'<[^>]+>', '', inner_norm)
        from html import unescape as _unesc
        inner_norm = _unesc(inner_norm)
        # Collapse horizontal whitespace but keep newlines.
        lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in inner_norm.split('\n')]
        lines = [ln for ln in lines if ln]
        if not lines:
            return ''
        attrs = match.group(1) or ''
        def _attr(name: str, default: str = '0') -> str:
            m = re.search(rf'\b{name}="([^"]*)"', attrs)
            return m.group(1) if m else default
        fx = float(_attr('x', '0') or '0')
        fy = float(_attr('y', '0') or '0')
        fw = float(_attr('width', '0') or '0')
        fh = float(_attr('height', '20') or '20')
        cx = fx + fw / 2.0
        font_size = 14
        line_h = 16.0  # pt at font-size 14
        # Mermaid already sizes node boxes to fit the text, so we do NOT
        # attempt character-based wrapping here — the avg char width
        # estimate is unreliable and causes incorrect line breaks.
        # Explicit <br>/<p> boundaries are already converted to newlines
        # above, which is sufficient.
        wrapped_lines = lines

        # Vertically centre the block of lines around the box centre.
        total_h = line_h * len(wrapped_lines)
        start_y = fy + fh / 2.0 - total_h / 2.0 + line_h * 0.8
        from html import escape as _esc
        tspans = []
        for idx, ln in enumerate(wrapped_lines):
            y = start_y + idx * line_h
            tspans.append(
                f'<tspan x="{cx:.2f}" y="{y:.2f}">{_esc(ln)}</tspan>'
            )
        return (
            f'<text text-anchor="middle" font-size="{font_size}" fill="#333" '
            f'style="font-family: {_SVG_FONT_FAMILY};">{"".join(tspans)}</text>'
        )

    return re.sub(
        r'<foreignObject([^>]*)>(.*?)</foreignObject>',
        _replace,
        svg,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _wrap_mermaid_labels(src: str, max_chars: int = 25) -> str:
    """Insert ``<br/>`` breaks into long node labels so mermaid can render
    multi-line text.

    Mermaid does not auto-wrap CJK labels (no word boundaries), so long
    Chinese labels overflow their node boxes in rendered SVG. We walk
    through label-like delimiters (``[...]``, ``(...)``, ``{...}``,
    ``[[...]]``, ``([...])`` etc.) and inject ``<br/>`` every ``max_chars``
    characters. We skip labels that already contain explicit breaks
    (``<br``, ``\\n``) so user-authored line breaks are preserved.

    Only applies to flowchart / graph diagrams. Sequence, gantt, pie, etc.
    use different label syntax and are returned unchanged.
    """
    # Only wrap for flowchart-style diagrams.
    first_directive = ''
    for raw_line in src.split('\n'):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('%%') or stripped.startswith('---'):
            continue
        first_directive = stripped.split()[0].lower().rstrip(':')
        break
    if first_directive not in ('graph', 'flowchart'):
        return src
    # Only break plain text inside the canonical label delimiters. Match
    # balanced pairs of single-char delimiters (flowchart nodes) and the
    # double-bracket / paren variants.
    pattern = re.compile(
        r'(\[\[|\]\]|\(\[|\]\)|\[\(|\)\]|\{\{|\}\}|[\[\](){}])'
    )

    def _split_long(text: str) -> str:
        if '<br' in text.lower() or '\\n' in text:
            return text
        # Preserve leading/trailing spaces
        stripped = text.strip()
        if len(stripped) <= max_chars:
            return text
        # Wrap both CJK and non-CJK labels. For non-CJK text, try to
        # break at word boundaries; for CJK, break every max_chars.
        cjk = sum(1 for c in stripped if '\u4e00' <= c <= '\u9fff')
        if cjk >= 4:
            # CJK: fixed-width chunking
            chunks = [stripped[i:i + max_chars] for i in range(0, len(stripped), max_chars)]
        else:
            # Non-CJK: word-boundary wrapping
            words = stripped.split()
            chunks = []
            cur = ''
            for w in words:
                if cur and len(cur) + 1 + len(w) > max_chars:
                    chunks.append(cur)
                    cur = w
                else:
                    cur = f'{cur} {w}' if cur else w
            if cur:
                chunks.append(cur)
            if len(chunks) <= 1:
                # Fall back to fixed-width chunking for single long words
                chunks = [stripped[i:i + max_chars] for i in range(0, len(stripped), max_chars)]
        wrapped = '<br/>'.join(chunks)
        return text.replace(stripped, wrapped, 1)

    out_lines = []
    for line in src.split('\n'):
        # Skip directives / comments
        s = line.lstrip()
        if s.startswith('%%') or s.startswith('---'):
            out_lines.append(line)
            continue
        # Find label spans: simple bracket matching per line.
        new_line = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch in '[({':
                # Find matching close on the same line.
                close_map = {'[': ']', '(': ')', '{': '}'}
                target = close_map[ch]
                depth = 1
                j = i + 1
                while j < len(line) and depth > 0:
                    if line[j] == ch:
                        depth += 1
                    elif line[j] == target:
                        depth -= 1
                    j += 1
                if depth == 0:
                    inner = line[i + 1:j - 1]
                    # Strip surrounding quotes if present
                    if len(inner) >= 2 and inner[0] == '"' and inner[-1] == '"':
                        inner_core = inner[1:-1]
                        wrapped = _split_long(inner_core)
                        new_line.append(f'{ch}"{wrapped}"{target}')
                    else:
                        wrapped = _split_long(inner)
                        new_line.append(f'{ch}{wrapped}{target}')
                    i = j
                    continue
            new_line.append(ch)
            i += 1
        out_lines.append(''.join(new_line))
    return '\n'.join(out_lines)


def _inline_svg_styles(svg: str) -> str:
    """Bake ``<style>`` blocks inside the SVG into inline ``style=""``
    attributes on matching elements.

    WeasyPrint's SVG renderer does NOT apply CSS from ``<style>`` tags
    inside an SVG, so mermaid sequence / state / gantt diagrams render
    with invisible message lines (elements carry ``stroke="none"`` and
    rely on the stylesheet to provide the real colour). After this pass
    every rule is expressed as an ``style="stroke:#333; ..."`` attribute
    that WeasyPrint does honour. Any presentation attribute (``stroke``
    / ``fill``) that the inlined CSS overrides with a real value is
    removed so the inline style wins.
    """
    try:
        import tinycss2
        from lxml import etree
        from lxml.cssselect import CSSSelector, SelectorError
    except Exception:
        return svg

    try:
        parser = etree.XMLParser(remove_comments=False, recover=True, huge_tree=True)
        root = etree.fromstring(svg.encode("utf-8"), parser)
    except Exception:
        return svg
    if root is None:
        return svg

    # Strip the SVG namespace off every tag/attr so cssselect selectors
    # like ``.messageLine0`` or ``line`` match without namespace prefixes.
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        # Also strip namespace from attribute keys.
        for key in list(el.attrib.keys()):
            if "}" in key:
                new_key = key.split("}", 1)[1]
                el.attrib[new_key] = el.attrib.pop(key)

    style_texts: list[str] = []
    for style_el in list(root.iter("style")):
        if style_el.text:
            style_texts.append(style_el.text)
        parent = style_el.getparent()
        if parent is not None:
            parent.remove(style_el)
    if not style_texts:
        return etree.tostring(root, encoding="unicode")

    stylesheet = "\n".join(style_texts)
    try:
        rules = tinycss2.parse_stylesheet(
            stylesheet, skip_whitespace=True, skip_comments=True,
        )
    except Exception:
        return etree.tostring(root, encoding="unicode")

    for rule in rules:
        if rule.type != "qualified-rule":
            continue
        selectors_text = tinycss2.serialize(rule.prelude).strip()
        decls_text = tinycss2.serialize(rule.content).strip().rstrip(";")
        if not selectors_text or not decls_text:
            continue

        # Parse declarations once — we want the list of property names to
        # know which presentation attributes to strip.
        # NOTE: font-family is excluded so mermaid's default theme
        # ("trebuchet ms", verdana, ...) does not override the PDF font
        # family (Times New Roman + 宋体) that is already set in the
        # rendering HTML page and _strip_foreign_objects().
        try:
            all_decls = [
                d for d in tinycss2.parse_declaration_list(
                    decls_text, skip_whitespace=True, skip_comments=True,
                ) if d.type == "declaration"
            ]
        except Exception:
            all_decls = []
        parsed_decls = [d for d in all_decls if d.lower_name != "font-family"]
        prop_names = {d.lower_name for d in parsed_decls}
        # Re-serialize without font-family declarations.
        if len(parsed_decls) != len(all_decls):
            decls_text = ";".join(
                tinycss2.serialize([d]) for d in parsed_decls
            )

        for sel_text in [s.strip() for s in selectors_text.split(",") if s.strip()]:
            try:
                sel = CSSSelector(sel_text)
            except (SelectorError, Exception):
                continue
            try:
                matches = sel(root)
            except Exception:
                continue
            for el in matches:
                existing = el.get("style", "")
                merged = decls_text if not existing else f"{existing.rstrip(';')};{decls_text}"
                el.set("style", merged)
                # Drop conflicting presentation attributes so the CSS wins.
                for prop in ("stroke", "fill", "stroke-width", "stroke-dasharray",
                             "font-size", "font-family", "opacity"):
                    if prop in prop_names and prop in el.attrib:
                        del el.attrib[prop]

    return etree.tostring(root, encoding="unicode")


# ── Shared browser pool (session-scoped, reused across PDF renders) ──────

import atexit as _atexit
import threading as _threading

class _BrowserPool:
    """Thread-local Playwright Chromium browser + context shared across PDF
    renders.

    Playwright's sync API is thread-bound — a browser can only be used from
    the thread that created it.  We keep one browser AND one browser context
    per OS thread so that CDN resources (Mermaid.js, MathJax → ~4 MB total)
    are cached in the browser's HTTP cache and not re-downloaded on every
    render call.
    """

    def __init__(self):
        self._lock = _threading.Lock()
        self._local = _threading.local()
        self._all_pws: list = []

    def new_page(self, **kwargs):
        """Create a new page from the (per-thread) shared browser + context.

        Keyword arguments (e.g. ``viewport``) are forwarded to
        ``context.new_page()``.  ``viewport`` is intercepted and applied
        via ``page.set_viewport_size()`` because ``context.new_page()``
        does not accept ``viewport`` directly (only ``browser.new_page()``
        does).
        """
        viewport = kwargs.pop('viewport', None)
        local = self._local
        if not hasattr(local, 'browser'):
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            context = browser.new_context()
            local.playwright = pw
            local.browser = browser
            local.context = context
            with self._lock:
                self._all_pws.append(pw)
            logger.info("BrowserPool: launched (thread %s)", _threading.get_ident())
        page = local.context.new_page(**kwargs)
        if viewport:
            page.set_viewport_size(viewport)
        return page

    def close(self):
        with self._lock:
            for pw in self._all_pws:
                try:
                    pw.stop()
                except Exception:
                    pass
            self._all_pws.clear()
            logger.info("BrowserPool: shut down")

_browser_pool = _BrowserPool()
_atexit.register(_browser_pool.close)


# ── SVG post-processing (extracted so batch renderer can reuse it) ────────

def _process_mermaid_svg(svg_content: str) -> str:
    """Apply all PDF-specific SVG cleanups to a Mermaid diagram.

    Strips XML decls / doctypes, replaces foreignObject labels with native
    ``<text>``, inlines ``<style>`` rules, normalises fonts, cleans the
    ``<svg>`` root, and scales the diagram to fit the A4 page.
    """
    svg_content = re.sub(r'<\?xml[^>]*\?>\s*', '', svg_content)
    svg_content = re.sub(
        r'<!DOCTYPE[^>]*>\s*', '', svg_content, flags=re.IGNORECASE,
    )
    svg_content = _strip_foreign_objects(svg_content)
    svg_content = _inline_svg_styles(svg_content)

    svg_content = re.sub(r'font-family:[^;"]*;?', '', svg_content)
    svg_content = re.sub(r'font-family="[^"]*"', '', svg_content)
    svg_content = re.sub(
        r'(<(?:text|tspan)\s)',
        rf'\1font-family="{_SVG_FONT_FAMILY}" ',
        svg_content,
    )

    vb_match = re.search(
        r'viewBox="\s*[\d.\-]+\s+[\d.\-]+\s+([\d.]+)\s+([\d.]+)"',
        svg_content,
    )
    vb_w, vb_h = (None, None)
    if vb_match:
        vb_w, vb_h = vb_match.group(1), vb_match.group(2)

    def _clean_svg_root(match: re.Match) -> str:
        root = match.group(0)
        root = re.sub(r'\swidth="[^"]*"', '', root, count=1)
        root = re.sub(r'\sheight="[^"]*"', '', root, count=1)
        root = re.sub(r'\sstyle="[^"]*"', '', root, count=1)
        return root
    svg_content = re.sub(
        r'<svg\s[^>]*>', _clean_svg_root, svg_content, count=1,
    )

    if vb_w and vb_h:
        try:
            vb_w_f = float(vb_w)
            vb_h_f = float(vb_h)
            w_pt = vb_w_f * 0.85
            h_pt = vb_h_f * 0.85
            MAX_W = 460.0
            MAX_H = 620.0
            if w_pt > MAX_W:
                scale_w = MAX_W / w_pt
                w_pt *= scale_w
                h_pt *= scale_w
            if h_pt > MAX_H:
                scale_h = MAX_H / h_pt
                w_pt *= scale_h
                h_pt *= scale_h
            svg_content = svg_content.replace(
                '<svg ',
                f'<svg width="{w_pt:.1f}pt" height="{h_pt:.1f}pt" ',
                1,
            )
        except (ValueError, ZeroDivisionError):
            pass

    return f'<div class="mermaid-diagram">{svg_content}</div>'


# ── ECharts support (rendered as inline SVG images for export) ─────────────
#
# Contract: a ```echarts code fence containing a standard JSON ECharts option
# object renders as an interactive chart in the chat/note UI (frontend
# echarts SVG renderer). When exporting to Markdown or PDF, the backend
# renders each option to an SVG image in a Playwright session and embeds it:
#   - Markdown export: <img src="data:image/svg+xml;base64,...">
#   - PDF export:      <div class="echarts-diagram">…svg…</div> (WeasyPrint,
#                      same inline-SVG path as mermaid diagrams)

def _echarts_fallback_html(option: str) -> str:
    """Escaped <pre> fallback shown when a chart fails to render."""
    from html import escape as _html_escape
    return (
        f'<pre style="background:#f5f5f5;padding:8pt;'
        f'border-radius:4pt;font-size:10pt">'
        f'{_html_escape(option)}</pre>'
    )


def _echarts_result_html(svg: str, *, as_markdown: bool = False) -> str:
    """Wrap a rendered ECharts SVG for the export target.

    ``as_markdown=True`` produces a self-contained <img> data URI for .md
    files (browsers/markdown viewers render raw ECharts SVG fine);
    ``False`` produces the inline ``<div class="echarts-diagram">`` block
    used by the PDF (WeasyPrint) pipeline, with the SVG post-processed for
    A4 sizing + PDF font normalization (same treatment as mermaid).
    """
    if as_markdown:
        b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return (
            f'<img src="data:image/svg+xml;base64,{b64}" '
            f'alt="echarts 图表" style="max-width:100%">'
        )
    # _process_echarts_svg already returns the full wrapped block
    return _process_echarts_svg(svg)


def _extract_echarts_fences(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace every top-level ```echarts fence with a placeholder key.

    Returns ``(content, [(key, option_json), ...])``. The placeholders are
    substituted back with rendered SVGs (or fallbacks) afterwards. A line
    scanner (not a regex) so echarts examples nested inside OTHER code
    fences stay untouched.
    """
    batch: list[tuple[str, str]] = []
    out_lines: list[str] = []
    in_fence = False
    fence_lang = ""
    fence_buf: list[str] = []

    for line in content.split("\n"):
        stripped = line.strip()
        if not in_fence and stripped.startswith("```"):
            in_fence = True
            fence_lang = stripped[3:].strip().split()[0] if len(stripped) > 3 else ""
            fence_buf = [line]
            continue
        if in_fence:
            if stripped.startswith("```"):
                if fence_lang == "echarts" and "```" not in "\n".join(fence_buf[1:]):
                    body = "\n".join(fence_buf[1:])
                    key = f"ZZZPDFPLACEHOLDERECHARTS{len(batch)}ZZZ"
                    batch.append((key, body))
                    out_lines.append(key)
                else:
                    out_lines.extend(fence_buf)
                    out_lines.append(line)
                in_fence = False
            else:
                fence_buf.append(line)
            continue
        out_lines.append(line)

    if in_fence:
        # unclosed fence — leave untouched
        out_lines.extend(fence_buf)
    return "\n".join(out_lines), batch


def _materialize_echarts_blocks(
    content: str,
    *,
    as_markdown: bool = False,
) -> str:
    """Render all ```echarts fences in *content* to SVG images.

    Called at the start of the PDF/MD export pipelines. Falls back to an
    escaped <pre> per-fence when rendering fails so exports never break on
    invalid chart options.
    """
    if not content or "echarts" not in content:
        return content or ""
    content, batch = _extract_echarts_fences(content)
    if not batch:
        return content
    results = _batch_render_echarts_svg(batch)
    for key, option in batch:
        svg = results.get(key)
        rendered = (
            _echarts_result_html(svg, as_markdown=as_markdown)
            if svg else _echarts_fallback_html(option)
        )
        content = content.replace(key, rendered)
    return content


def _process_echarts_svg(svg_content: str) -> str:
    """Apply the same PDF-specific SVG cleanups as mermaid diagrams.

    ECharts SVG output carries most presentation attributes inline; we still
    inline any <style> rules (WeasyPrint ignores <style> inside SVG), fix the
    font-family so CJK text uses the PDF font, strip width/height so the
    viewBox governs scaling, and cap the size to fit the A4 page.
    """
    svg_content = re.sub(r'<\?xml[^>]*\?>\s*', '', svg_content)
    svg_content = re.sub(
        r'<!DOCTYPE[^>]*>\s*', '', svg_content, flags=re.IGNORECASE,
    )
    svg_content = _strip_foreign_objects(svg_content)
    svg_content = _inline_svg_styles(svg_content)

    svg_content = re.sub(r'font-family:[^;"]*;?', '', svg_content)
    svg_content = re.sub(r'font-family="[^"]*"', '', svg_content)
    svg_content = re.sub(
        r'(<(?:text|tspan)\s)',
        rf'\1font-family="{_SVG_FONT_FAMILY}" ',
        svg_content,
    )

    vb_match = re.search(
        r'viewBox="\s*[\d.\-]+\s+[\d.\-]+\s+([\d.]+)\s+([\d.]+)"',
        svg_content,
    )
    vb_w, vb_h = (None, None)
    if vb_match:
        vb_w, vb_h = vb_match.group(1), vb_match.group(2)

    def _clean_svg_root(match: re.Match) -> str:
        root = match.group(0)
        root = re.sub(r'\swidth="[^"]*"', '', root, count=1)
        root = re.sub(r'\sheight="[^"]*"', '', root, count=1)
        root = re.sub(r'\sstyle="[^"]*"', '', root, count=1)
        return root
    svg_content = re.sub(
        r'<svg\s[^>]*>', _clean_svg_root, svg_content, count=1,
    )

    if vb_w and vb_h:
        try:
            w_pt = float(vb_w) * 0.85
            h_pt = float(vb_h) * 0.85
            MAX_W = 460.0
            MAX_H = 620.0
            if w_pt > MAX_W:
                scale_w = MAX_W / w_pt
                w_pt *= scale_w
                h_pt *= scale_w
            if h_pt > MAX_H:
                scale_h = MAX_H / h_pt
                w_pt *= scale_h
                h_pt *= scale_h
            svg_content = svg_content.replace(
                '<svg ',
                f'<svg width="{w_pt:.1f}pt" height="{h_pt:.1f}pt" ',
                1,
            )
        except (ValueError, ZeroDivisionError):
            pass

    return f'<div class="echarts-diagram">{svg_content}</div>'


def _echarts_apply_legend_defaults(option: dict) -> dict:
    """Restore the classic (ECharts 5) legend placement.

    ECharts 6.1.0 resolves the legend default (``top: 'auto'``) to the BOTTOM
    of the container — legends render crammed against / overlapping the plot
    (probe: legend text y=331 in a 360px-tall svg vs x-axis labels y=340).
    Vertical-orient legends similarly lose their right-side default. Only
    applies when the option does not pin the position explicitly: a present
    key (including ``null`` or an explicit ``"auto"``) is preserved.

    When the option also has a title, a horizontal legend is placed BELOW the
    title block (top: 40) instead of the very top — at top: 0 the legend
    glyphs collide with the title (measured: legend baseline y=11.6 vs title
    glyph top y≈12.8 in a 699px-wide render, title drawn last paints over the
    legend). The author-pinned legend position always wins.
    """
    lg = option.get("legend")
    if not isinstance(lg, dict):
        return option
    vertical = lg.get("orient") == "vertical"
    if "top" not in lg and "bottom" not in lg:
        if not vertical and isinstance(option.get("title"), (dict, str)):
            lg["top"] = 40
        else:
            lg["top"] = "middle" if vertical else 0
    if vertical and "left" not in lg and "right" not in lg:
        lg["right"] = 0
    return option


def _echarts_apply_legend_series_names(option: dict) -> dict:
    """Give series names matching ``legend.data`` so legend items render.

    ECharts 6 hides legend items whose name matches no series. LLM-authored
    options routinely set ``legend.data`` while leaving ``series[].name``
    unset — the whole legend silently disappears (conv 7c3c225e「谋杀率」
    legend absent from the rendered SVG). Assign ``series[i].name =
    legend.data[i]`` for series that do not pin a name; explicit names are
    preserved. Non-string legend entries are skipped.
    """
    lg = option.get("legend")
    if not isinstance(lg, dict):
        return option
    data = lg.get("data")
    if not isinstance(data, list) or not data:
        return option
    series = option.get("series")
    if not isinstance(series, list):
        return option
    for i, s in enumerate(series):
        if not isinstance(s, dict):
            continue
        # pie/radar legends match data[].name, not series.name — injecting a
        # series-level name there would fabricate a stray legend candidate.
        if s.get("type") in ("pie", "radar"):
            continue
        if s.get("name") is None and i < len(data) and isinstance(data[i], str):
            s["name"] = data[i]
    return option


def _echarts_apply_top_spacing_defaults(option: dict) -> dict:
    """Reserve room above the plot for top-placed labels when a title exists.

    LLM-authored options typically omit ``grid`` entirely; ECharts then uses
    the default ``grid.top=60``, which fits the title but not the labels that
    render above the grid top edge — markPoint labels default to
    ``position: 'top'`` (above the point, overflowing the plot) and markArea
    labels anchor above the area top (conv 7c3c225e: "6.9" label bbox
    overlapped the title bbox by 8.1px, glyph gap ≈0.4px). When the option
    has a title, the space above the plot must also clear the legend when the
    legend defaults place it below the title.

    Top zone budget (title block 40 + optional legend block 25 + optional
    label zone 50, all measured against a 699px-wide 360px-tall render; the
    40px title budget assumes a single-line title):
    title+labels → 90 (measured: "6.9" clears the title by 20px); title+
    legend → 80; title+legend+labels → 115. Below 60 no injection happens
    (the ECharts default already clears). An explicit ``grid.top`` is
    preserved — the author takes responsibility for the layout.
    """
    title = option.get("title")
    if not isinstance(title, (dict, str)):
        return option
    series = option.get("series")
    if not isinstance(series, list):
        return option
    needs_top_space = False
    for s in series:
        if not isinstance(s, dict):
            continue
        mp = s.get("markPoint")
        if isinstance(mp, dict) and isinstance(mp.get("data"), list) and mp["data"]:
            needs_top_space = True
            break
        ma = s.get("markArea")
        if not (isinstance(ma, dict) and isinstance(ma.get("data"), list) and ma["data"]):
            continue
        for pair in ma["data"]:
            items = pair if isinstance(pair, list) else [pair]
            if any(
                isinstance(it, dict) and ("name" in it or "label" in it)
                for it in items
            ):
                needs_top_space = True
                break
        if needs_top_space:
            break
    lg = option.get("legend")
    legend_below_title = (
        isinstance(lg, dict)
        and lg.get("orient") != "vertical"
        and "top" not in lg
        and "bottom" not in lg
    )
    if not needs_top_space and not legend_below_title:
        return option
    needed = 40
    if legend_below_title:
        needed += 25
    if needs_top_space:
        needed += 50
    else:
        needed += 15
    if needed <= 60:
        return option
    grid = option.get("grid")
    if grid is None:
        grid = option["grid"] = {}
    if isinstance(grid, dict) and "top" not in grid:
        grid["top"] = needed
    return option


def _normalize_echarts_option(raw: str) -> str:
    """Normalize one echarts option string for the render page.

    Returns the option re-serialized as a JSON STRING (the page JSON.parse()s
    it) with legend defaults applied. Invalid JSON passes through untouched —
    the page-side per-item try/catch then skips only that chart, so one bad
    fence never kills the whole batch.
    """
    try:
        opt = _json_mod.loads(raw)
        _echarts_apply_top_spacing_defaults(opt)
        _echarts_apply_legend_defaults(opt)
        _echarts_apply_legend_series_names(opt)
        return _json_mod.dumps(opt)
    except Exception:
        return raw


def _batch_render_echarts_svg(
    entries: list[tuple[str, str]],
) -> dict[str, str]:
    """Render ECharts option JSON strings to SVG via a single Playwright page.

    Returns a dict keyed by placeholder key. Failed charts are skipped (the
    caller falls back to an escaped <pre>).
    """
    results: dict[str, str] = {}
    html_path = None
    if not entries:
        return results

    import os as _os

    try:
        # Normalize ECharts-6 legend defaults in Python (unit-testable) so the
        # browser page receives fully-resolved options. The option must stay a
        # JSON STRING in the payload — the page JSON.parse()s it.
        echarts_json = _json_mod.dumps(
            [{"key": k, "option": _normalize_echarts_option(o)} for k, o in entries],
        )
        # `<\/script` keeps the JSON text from terminating the inline script
        # tag if an option string contains "</script>"; JSON.parse decodes
        # \u003c back to `<`.
        echarts_json = echarts_json.replace("<", "\\u003c")

        render_js = f"""<script>
(function() {{
  const items = {echarts_json};
  const results = {{}};
  const container = document.getElementById('echarts-container');
  for (const item of items) {{
    const div = document.createElement('div');
    div.style.width = '800px';
    div.style.height = '500px';
    container.appendChild(div);
    try {{
      const option = JSON.parse(item.option);
      option.animation = false;
      const chart = echarts.init(div, null, {{renderer: 'svg'}});
      chart.setOption(option, {{notMerge: true}});
      results[item.key] = chart.renderToSVGString();
    }} catch (e) {{
      results[item.key] = '';
    }}
  }}
  window.__echartsResults = results;
}})();
</script>"""

        _app_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        _vendor_js = _os.path.join(_app_dir, "vendor_js")
        _echarts_path = _os.path.join(_vendor_js, "echarts.min.js")
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="file://{_echarts_path}"></script>
<style>
body{{margin:0;padding:8px;background:white;}}
</style>
</head><body>
<div id="echarts-container"></div>
{render_js}
</body></html>"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html)
            html_path = f.name

        page = _browser_pool.new_page(viewport={"width": 2400, "height": 1600})
        try:
            page.goto(f'file://{html_path}', wait_until='networkidle', timeout=30000)
            try:
                page.wait_for_function(
                    "() => Object.keys(window.__echartsResults).length >= "
                    + str(len(entries)),
                    timeout=15000,
                )
            except Exception:
                page.wait_for_timeout(2000)
            for key, _opt in entries:
                svg = page.evaluate(
                    "(k) => window.__echartsResults[k] || ''", key,
                )
                if svg:
                    results[key] = svg
        finally:
            page.close()
    except Exception as e:
        logger.warning("Batch echarts render failed: %s", e)
    finally:
        if html_path:
            try:
                os.remove(html_path)
            except OSError:
                pass
    return results


# ── Batch Mermaid + MathJax renderer (single Playwright session) ──────────

_MERMAID_INIT_JS = """\
mermaid.initialize({
    startOnLoad: true,
    theme: "default",
    flowchart: {htmlLabels: true, useMaxWidth: true, wrap: true, curve: "basis", diagramPadding: 8, nodeSpacing: 40, rankSpacing: 50},
    sequence: {wrap: true, useMaxWidth: true, actorMargin: 50, messageMargin: 35, mirrorActors: true},
    gantt: {useMaxWidth: true},
    themeVariables: {fontSize: "14px"}
});
"""

_MATHJAX_CONFIG_JS = """\
MathJax = {
  tex: { inlineMath: [['$','$']], displayMath: [['$$','$$']] },
  svg: { fontCache: 'local' },
  startup: { typeset: false }
};
"""


def _batch_render_mermaid_and_math(
    mermaid_entries: list[tuple[str, str]],
    math_entries: list[tuple[str, str, bool]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Render all Mermaid diagrams AND MathJax formulas in a single
    Playwright Chromium page.

    Returns ``(mermaid_results, math_results)`` dicts keyed by the
    caller-supplied placeholder key.
    """
    mermaid_results: dict[str, str] = {}
    math_results: dict[str, str] = {}
    html_path = None

    if not mermaid_entries and not math_entries:
        return mermaid_results, math_results

    import os as _os

    try:
        mermaid_divs = ""
        if mermaid_entries:
            for key, code in mermaid_entries:
                prepared = _wrap_mermaid_labels(code)
                escaped = (
                    prepared.replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;')
                    .replace('"', '&quot;')
                )
                mermaid_divs += f'<div class="mermaid" data-key="{key}">{escaped}</div>\n'

        math_js = ""
        if math_entries:
            math_json = _json_mod.dumps(
                [{"key": k, "latex": _preprocess_latex_for_mathjax(l), "display": d}
                 for k, l, d in math_entries]
            )
            math_js = f"""<script>
MathJax.startup.promise.then(function() {{
  const formulas = {math_json};
  const container = document.getElementById('math-results');
  for (const f of formulas) {{
    const div = document.createElement('div');
    div.className = 'math-item';
    div.setAttribute('data-key', f.key);
    try {{
      const node = MathJax.tex2svg(f.latex, {{ display: f.display }});
      div.appendChild(node);
    }} catch(e) {{
      div.textContent = 'FAIL';
    }}
    container.appendChild(div);
  }}
}});
</script>"""

        _app_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        _vendor_js = _os.path.join(_app_dir, "vendor_js")
        _mermaid_path = _os.path.join(_vendor_js, "mermaid.min.js")
        _mathjax_path = _os.path.join(_vendor_js, "tex-svg.js")
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="file://{_mermaid_path}"></script>
<script>{_MERMAID_INIT_JS}</script>
<script>{_MATHJAX_CONFIG_JS}</script>
<script src="file://{_mathjax_path}"></script>
<style>
body{{margin:0;padding:8px;background:white;font-family:{_PDF_FONT_FAMILY};}}
.mermaid{{font-family:{_PDF_FONT_FAMILY};margin:16px 0;}}
.math-item{{margin:4px 0;}}
</style>
</head><body>
{mermaid_divs}
<div id="math-results"></div>
{math_js}
</body></html>"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html)
            html_path = f.name

        page = _browser_pool.new_page(viewport={"width": 2400, "height": 1600})
        try:
            page.goto(f'file://{html_path}', wait_until='networkidle', timeout=30000)

            if mermaid_entries:
                try:
                    page.wait_for_function(
                        "() => { const svgs = document.querySelectorAll('.mermaid svg'); "
                        "return svgs.length >= " + str(len(mermaid_entries)) + " && "
                        "Array.from(svgs).every(s => s.getAttribute('aria-roledescription')); }",
                        timeout=15000,
                    )
                except Exception:
                    page.wait_for_timeout(2000)
                page.wait_for_timeout(300)

            if math_entries:
                try:
                    page.wait_for_function(
                        "() => document.querySelectorAll('#math-results .math-item svg').length > 0",
                        timeout=15000,
                    )
                except Exception:
                    page.wait_for_timeout(2000)
                page.wait_for_timeout(300)

            # Extract Mermaid SVGs.
            if mermaid_entries:
                mermaid_els = page.query_selector_all('.mermaid')
                for el in mermaid_els:
                    key = el.get_attribute('data-key')
                    svg = el.query_selector('svg')
                    if key and svg:
                        raw = page.evaluate('(e) => e.outerHTML', svg)
                        mermaid_results[key] = _process_mermaid_svg(raw)
                    elif key:
                        code = [c for k, c in mermaid_entries if k == key]
                        code_str = code[0] if code else ""
                        from html import escape
                        mermaid_results[key] = (
                            f'<pre style="background:#f5f5f5;padding:8pt;'
                            f'border-radius:4pt;font-size:10pt">{escape(code_str)}</pre>'
                        )

            # Extract MathJax SVGs.
            if math_entries:
                items = page.query_selector_all('#math-results .math-item')
                for item in items:
                    key = item.get_attribute('data-key')
                    svg_el = item.query_selector('svg')
                    if key and svg_el:
                        raw = page.evaluate('(e) => e.outerHTML', svg_el)
                        if raw and 'FAIL' not in raw:
                            math_results[key] = _clean_mathjax_svg(raw)

            return mermaid_results, math_results

        finally:
            try:
                page.close()
            except Exception:
                pass

    except Exception as e:
        logger.warning("Batch mermaid+math render failed: %s", e)
        return mermaid_results, math_results
    finally:
        if html_path:
            try:
                _os.unlink(html_path)
            except OSError:
                pass


# ── Legacy single-diagram renderer (delegates to shared browser pool) ─────

def _render_mermaid_to_img(mermaid_code: str) -> str:
    """Render a single Mermaid diagram via the shared browser pool.

    Kept for backward-compatibility; new callers should prefer
    ``_batch_render_mermaid_and_math``.
    """
    from html import escape
    results, _ = _batch_render_mermaid_and_math(
        [("__single__", mermaid_code)], [],
    )
    rendered = results.get("__single__")
    if rendered:
        return rendered
    return f'<pre style="background:#f5f5f5;padding:8pt;border-radius:4pt;font-size:10pt">{escape(mermaid_code)}</pre>'


def _render_math_fallback_html(latex: str, display: bool) -> str:
    """Best-effort HTML rendering of LaTeX fragments matplotlib cannot
    render (``\\begin{cases}``, ``\\begin{aligned}``, ``\\begin{matrix}``).

    The goal is not pixel-perfect math typesetting but to keep the PDF
    export usable instead of leaving a raw ``\\begin{cases} ...`` string
    in place. The substitution is literal enough that the reader can
    still understand the equation.
    """
    from html import escape as _esc

    def _fmt(tex: str) -> str:
        t = tex
        # Replace \_ with HTML entity so it is never mistaken for a
        # subscript marker by the regexes below.
        t = re.sub(r"\\_", "&#95;", t)
        t = re.sub(r"\\boldsymbol\{", r"\\mathbf{", t)
        t = re.sub(r"\\mathcal\{", r"\\mathit{", t)
        t = re.sub(r"\\mathbb\{", r"\\mathbf{", t)
        t = re.sub(r"\\displaystyle", "", t)
        t = re.sub(r"\\left(?=[\[\(\{\\|])", "", t)
        t = re.sub(r"\\right(?=[\]\)\}\\|\.])", "", t)
        t = re.sub(r"\\big[lr]?", "", t)
        t = re.sub(r"\\Big[lr]?", "", t)
        t = re.sub(r"\\bigg[lr]?", "", t)
        t = re.sub(r"\\Bigg[lr]?", "", t)
        greek = {
            r"\\alpha": "α", r"\\beta": "β", r"\\gamma": "γ", r"\\delta": "δ",
            r"\\epsilon": "ε", r"\\zeta": "ζ", r"\\eta": "η", r"\\theta": "θ",
            r"\\iota": "ι", r"\\kappa": "κ", r"\\lambda": "λ", r"\\mu": "μ",
            r"\\nu": "ν", r"\\xi": "ξ", r"\\pi": "π", r"\\rho": "ρ",
            r"\\sigma": "σ", r"\\tau": "τ", r"\\phi": "φ", r"\\chi": "χ",
            r"\\psi": "ψ", r"\\omega": "ω",
            r"\\Gamma": "Γ", r"\\Delta": "Δ", r"\\Theta": "Θ", r"\\Lambda": "Λ",
            r"\\Pi": "Π", r"\\Sigma": "Σ", r"\\Phi": "Φ", r"\\Psi": "Ψ",
            r"\\Omega": "Ω",
            r"\\cdot": "·", r"\\times": "×", r"\\div": "÷", r"\\pm": "±",
            r"\\leq": "≤", r"\\geq": "≥", r"\\neq": "≠", r"\\approx": "≈",
            r"\\mid": "∣", r"\\to": "→", r"\\rightarrow": "→", r"\\leftarrow": "←",
            r"\\infty": "∞", r"\\sum": "∑", r"\\prod": "∏", r"\\int": "∫",
            r"\\partial": "∂", r"\\nabla": "∇",
        }
        for k, v in greek.items():
            t = re.sub(k + r"(?![A-Za-z])", v, t)
        t = re.sub(r"\\textit\{([^{}]*)\}", r"<i>\1</i>", t)
        t = re.sub(r"\\text\{([^{}]*)\}", r"\1", t)
        t = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", t)
        t = re.sub(r"\\mathbf\{([^{}]*)\}", r"<b>\1</b>", t)
        t = re.sub(r"\\mathit\{([^{}]*)\}", r"<i>\1</i>", t)
        t = re.sub(r"\\operatorname\{([^{}]*)\}", r"\1", t)
        # Known math operators — emit as plain text so they survive the
        # subscript-extraction step below instead of being blanked.
        t = re.sub(r"\\max", "max", t)
        t = re.sub(r"\\min", "min", t)
        t = re.sub(r"\\log", "log", t)
        t = re.sub(r"\\sin", "sin", t)
        t = re.sub(r"\\cos", "cos", t)
        t = re.sub(r"\\tan", "tan", t)
        t = re.sub(r"\\lim", "lim", t)
        t = re.sub(r"\\inf", "inf", t)
        t = re.sub(r"\\sup", "sup", t)
        t = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"<span style='display:inline-block;vertical-align:middle;text-align:center'><span style='display:block;border-bottom:1px solid currentColor;padding:0 4pt'>\1</span><span style='display:block;padding:0 4pt'>\2</span></span>", t)
        t = re.sub(r"_\{([^{}]+)\}", r"<sub>\1</sub>", t)
        t = re.sub(r"\^\{([^{}]+)\}", r"<sup>\1</sup>", t)
        t = re.sub(r"_([A-Za-z0-9])", r"<sub>\1</sub>", t)
        t = re.sub(r"\^([A-Za-z0-9])", r"<sup>\1</sup>", t)
        t = t.replace(r"\,", " ").replace(r"\;", " ").replace(r"\!", "")
        t = re.sub(r"\\[a-zA-Z]+", "", t)
        return t

    m = re.search(r"\\begin\{(cases|aligned|align|matrix|pmatrix|bmatrix)\}(.*?)\\end\{\1\}",
                  latex, flags=re.DOTALL)
    if m:
        env, body = m.group(1), m.group(2)
        # Capture and render text before / after the environment so that
        # e.g. ``\text{penalty} = \begin{cases}...`` keeps the prefix.
        pre_text = latex[:m.start()].strip()
        post_text = latex[m.end():].strip()
        pre_html = _fmt(pre_text) + " " if pre_text else ""
        post_html = " " + _fmt(post_text) if post_text else ""
        rows = [r.strip() for r in re.split(r"\\\\", body) if r.strip()]
        brace_open, brace_close = "", ""
        if env == "cases":
            brace_open = "{"
        elif env == "pmatrix":
            brace_open, brace_close = "(", ")"
        elif env == "bmatrix":
            brace_open, brace_close = "[", "]"
        html_rows = []
        for row in rows:
            cells = [_fmt(c.strip()) for c in row.split("&")]
            html_rows.append(
                "<tr>" +
                "".join(f'<td style="padding:0 6pt">{c}</td>' for c in cells) +
                "</tr>"
            )
        table_html = (
            f'<table style="display:inline-table;border-collapse:collapse;'
            f'vertical-align:middle">{"".join(html_rows)}</table>'
        )
        prefix = f'<span style="font-size:{"1.5em" if display else "1.2em"}">{brace_open}</span>' if brace_open else ""
        suffix = f'<span style="font-size:{"1.5em" if display else "1.2em"}">{brace_close}</span>' if brace_close else ""
        wrapper_class = "math-display" if display else "math-inline"
        style = "display:block;text-align:center;margin:6pt 0;" if display else ""
        return (
            f'<span class="{wrapper_class}" style="{style}font-family:serif">'
            f'{pre_html}{prefix}{table_html}{suffix}{post_html}</span>'
        )

    # Unknown construct — emit the formatted LaTeX as best-effort inline text.
    wrapper_class = "math-display" if display else "math-inline"
    return (
        f'<span class="{wrapper_class}" style="font-family:serif">'
        f'{_fmt(_esc(latex))}</span>'
    )


def _preprocess_latex_for_mathtext(latex: str) -> str:
    """Replace LaTeX commands that matplotlib's mathtext engine does not
    support with close equivalents it *does* understand.
    """
    t = latex
    t = re.sub(r"\\boldsymbol\{", r"\\mathbf{", t)
    t = re.sub(r"\\mathcal\{", r"\\mathit{", t)
    t = re.sub(r"\\mathbb\{", r"\\mathbf{", t)
    t = re.sub(r"\\mathfrak\{", r"\\mathit{", t)
    t = re.sub(r"\\operatorname\{([^{}]*)\}", r"\\mathrm{\1}", t)
    t = re.sub(r"\\displaystyle", "", t)
    t = re.sub(r"\\textstyle", "", t)
    t = re.sub(r"\\left(?=[\[\(\{\\|])", "", t)
    t = re.sub(r"\\right(?=[\]\)\}\\|\.])", "", t)
    t = re.sub(r"\\big[lr]?", "", t)
    t = re.sub(r"\\Big[lr]?", "", t)
    t = re.sub(r"\\bigg[lr]?", "", t)
    t = re.sub(r"\\Bigg[lr]?", "", t)
    t = re.sub(r"\\,", " ", t)
    t = re.sub(r"\\;", " ", t)
    t = re.sub(r"\\!", "", t)
    return t


def _preprocess_latex_for_mathjax(latex: str) -> str:
    """Normalize LaTeX so MathJax renders it the same way the browser's
    KaTeX preview does.

    MathJax treats ``\\_`` inside ``\\text{}`` as two literal glyphs
    (backslash + underscore), whereas users expect an underscore.  Replace
    ``\\_`` with ``_`` before rendering so identifiers like
    ``\\text{GPU\\_Mem\\_Free}`` come out as ``GPU_Mem_Free``.
    """
    t = latex
    t = t.replace(r"\_", "_")
    return t


def _render_math_to_svg(latex: str, display: bool = False) -> str:
    """Render a LaTeX math expression to inline vector SVG via matplotlib.

    Uses matplotlib's built-in mathtext engine (no LaTeX install required).
    Returns an inline SVG string suitable for embedding inside HTML. All
    math glyphs are true vectors, so they stay crisp at any PDF zoom level.
    Falls back to Playwright + MathJax when matplotlib cannot handle the formula.
    """
    # Matplotlib's mathtext engine does not support \\begin{...} environments
    # (cases, aligned, matrix, etc.). Skip matplotlib and go directly to
    # MathJax which handles all standard LaTeX environments correctly.
    if re.search(r'\\begin\{', latex):
        mathjax_html = _render_math_with_mathjax(latex, display)
        if mathjax_html:
            return mathjax_html
        return _render_math_fallback_html(latex, display=display)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        latex_preprocessed = _preprocess_latex_for_mathtext(latex)

        fontsize = 14 if display else 11
        expr = f"${latex_preprocessed}$"

        # First pass: measure the rendered text so the figure is tight.
        fig = plt.figure(figsize=(0.01, 0.01))
        txt = fig.text(0, 0, expr, fontsize=fontsize)
        fig.canvas.draw()
        bbox = txt.get_window_extent(renderer=fig.canvas.get_renderer())
        dpi = fig.dpi
        w_in = max(bbox.width / dpi + 0.04, 0.1)
        h_in = max(bbox.height / dpi + 0.04, 0.1)
        plt.close(fig)

        fig = plt.figure(figsize=(w_in, h_in))
        fig.text(0.02, 0.5, expr, fontsize=fontsize, va="center")
        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="svg",
            bbox_inches="tight",
            pad_inches=0.01,
            transparent=True,
        )
        plt.close(fig)

        svg = buf.getvalue().decode("utf-8")
        # Drop XML declaration / doctype so the SVG can be inlined.
        svg = re.sub(r"<\?xml[^>]*\?>\s*", "", svg)
        svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg, flags=re.IGNORECASE)

        wrapper_class = "math-display" if display else "math-inline"
        return f'<span class="{wrapper_class}">{svg}</span>'
    except Exception as e:
        logger.warning("matplotlib math rendering failed for %r: %s, trying MathJax", latex, e)
        mathjax_html = _render_math_with_mathjax(latex, display)
        if mathjax_html:
            return mathjax_html
        return _render_math_fallback_html(latex, display=display)


def _clean_mathjax_svg(svg: str) -> str:
    """Clean MathJax SVG output for WeasyPrint compatibility.

    MathJax embeds MathML metadata elements.  The ``<mtext>`` element
    holds raw LaTeX text (e.g. ``\\_`` instead of ``_``) that WeasyPrint
    renders as visible text *alongside* the correct ``<use>`` / ``<path>``
    glyphs, producing garbled output.

    MathJax 3 also appends an ``<mjx-assistive-mml>`` element (and
    ``<mjx-container>`` wrappers) outside the ``<svg>`` for screen-reader
    accessibility.  WeasyPrint renders those MathML nodes as visible text,
    producing duplicate garbled content below the formula.

    This function strips ``<mtext>`` text content, ``<mjx-assistive-mml>``
    blocks, and ``<mjx-container>`` wrappers.
    """
    s = svg.replace('currentColor', '#333')
    s = re.sub(
        r'<mtext[^>]*>.*?</mtext>',
        '',
        s,
        flags=re.DOTALL,
    )
    s = re.sub(
        r'<mjx-assistive-mml[^>]*>.*?</mjx-assistive-mml>',
        '',
        s,
        flags=re.DOTALL,
    )
    s = re.sub(r'</?mjx-container[^>]*>', '', s)
    return s


def _render_math_with_mathjax(latex: str, display: bool) -> str | None:
    """Render LaTeX via Playwright + MathJax CDN (SVG output).
    Returns self-contained SVG string or None."""
    latex = _preprocess_latex_for_mathjax(latex)
    html_path = None
    try:
        wrapper_class = "math-display" if display else "math-inline"
        wrapper_style = "display:block;text-align:center;margin:6pt 0;" if display else ""
        display_js = "true" if display else "false"

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<script>
MathJax = {{ tex: {{ inlineMath: [['$','$']], displayMath: [['$$','$$']] }}, svg: {{ fontCache: 'local' }}, startup: {{ typeset: false }} }};
</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
<style>body {{ margin: 0; padding: 8px; background: white; font-family: {_PDF_FONT_FAMILY}; }}</style>
</head><body>
<div id="math-container"></div>
<script>
try {{
  const node = MathJax.tex2svg({_json_dumps_safe(latex)}, {{ display: {display_js} }});
  document.getElementById('math-container').appendChild(node);
}} catch(e) {{
  document.getElementById('math-container').textContent = 'ERROR: ' + e.message;
}}
</script>
</body></html>"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html)
            html_path = f.name

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 800})
                page.goto(f'file://{html_path}', wait_until='networkidle', timeout=30000)
                try:
                    page.wait_for_selector('svg', timeout=10000)
                except Exception:
                    page.wait_for_timeout(3000)
                page.wait_for_timeout(500)

                svg_el = page.query_selector('#math-container svg')
                if not svg_el:
                    logger.warning("MathJax SVG not found for %r", latex)
                    return None

                svg_html = page.evaluate('(el) => el.outerHTML', svg_el)
                svg_html = _clean_mathjax_svg(svg_html)
                return (
                    f'<span class="{wrapper_class}" style="{wrapper_style}">'
                    f'{svg_html}</span>'
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning("MathJax rendering failed for %r: %s", latex, e)
        return None
    finally:
        if html_path:
            try:
                import os as _os
                _os.unlink(html_path)
            except OSError:
                pass


def _batch_render_math_with_mathjax(
    formulas: list[tuple[str, str, bool]]
) -> dict[str, str]:
    """Batch-render multiple LaTeX formulas in a single Playwright session
    using MathJax SVG output (self-contained SVGs that WeasyPrint can render).

    Args:
        formulas: list of (key, latex, display_mode) tuples.

    Returns:
        dict mapping key -> rendered SVG/HTML string.
    """
    formulas = [(k, _preprocess_latex_for_mathjax(l), d) for k, l, d in formulas]
    if not formulas:
        return {}

    html_path = None
    results: dict[str, str] = {}
    try:
        import json as _json

        formulas_json = _json.dumps(
            [{"key": k, "latex": l, "display": d} for k, l, d in formulas]
        )

        import os as _os2
        _app_dir2 = _os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__)))
        _mathjax_path2 = _os2.path.join(_app_dir2, "vendor_js", "tex-svg.js")
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<script>
MathJax = {{
  tex: {{ inlineMath: [['$','$']], displayMath: [['$$','$$']] }},
  svg: {{ fontCache: 'local' }},
  startup: {{ typeset: false }}
}};
</script>
<script src="file://{_mathjax_path2}"></script>
<style>
body {{ margin: 0; padding: 8px; background: white; font-family: {_PDF_FONT_FAMILY}; }}
.math-item {{ margin: 4px 0; }}
</style>
</head><body>
<div id="results"></div>
<script>
async function renderAll() {{
  const formulas = {formulas_json};
  const container = document.getElementById('results');
  for (const f of formulas) {{
    const div = document.createElement('div');
    div.className = 'math-item';
    div.setAttribute('data-key', f.key);
    try {{
      const node = MathJax.tex2svg(f.latex, {{ display: f.display }});
      div.appendChild(node);
    }} catch(e) {{
      div.textContent = 'FAIL';
    }}
    container.appendChild(div);
  }}
}}
renderAll();
</script>
</body></html>"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html)
            html_path = f.name

        page = _browser_pool.new_page(viewport={"width": 1200, "height": 800})
        try:
            page.goto(f'file://{html_path}', wait_until='networkidle', timeout=30000)
            try:
                page.wait_for_function(
                    "() => document.querySelectorAll('.math-item svg').length > 0",
                    timeout=15000,
                )
            except Exception:
                page.wait_for_timeout(3000)
            page.wait_for_timeout(500)

            items = page.query_selector_all('.math-item')
            for item in items:
                key = item.get_attribute('data-key')
                svg_el = item.query_selector('svg')
                if key and svg_el:
                    svg_html = svg_el.evaluate('(el) => el.outerHTML', svg_el)
                    if svg_html and 'FAIL' not in svg_html:
                        svg_html = _clean_mathjax_svg(svg_html)
                        results[key] = svg_html
        finally:
            try:
                page.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("Batch MathJax rendering failed: %s", e)
    finally:
        if html_path:
            try:
                import os as _os
                _os.unlink(html_path)
            except OSError:
                pass

    return results



def _markdown_to_html_with_mermaid(content: str) -> str:
    """Convert Markdown to HTML with Mermaid diagrams + LaTeX math.

    Strategy: replace Mermaid code fences, fenced code blocks, inline code
    spans, and LaTeX math (``$$...$$`` and ``$...$``) with unique placeholders
    *before* running the Markdown parser, then substitute the rendered SVG /
    code back in afterwards. This prevents the parser from mangling SVG tags
    or LaTeX (e.g. turning ``_`` into italics, eating ``\\``) and keeps
    surrounding text intact.
    """
    placeholders: dict[str, str] = {}
    counter = 0

    def _next_key(tag: str) -> str:
        nonlocal counter
        key = f"ZZZPDFPLACEHOLDER{tag}{counter}ZZZ"
        counter += 1
        return key

    from html import escape as _html_escape

    # 0. Render ECharts fences to inline SVG images FIRST so the fenced-code
    # protection below does not consume them. Exports never break on invalid
    # chart JSON — failed charts become escaped <pre> fallbacks.
    content = _materialize_echarts_blocks(content, as_markdown=False)

    # 1a. Collect Mermaid code fences — DEFER rendering so we can batch
    # all diagrams + math into a single Playwright session.
    mermaid_batch: list[tuple[str, str]] = []

    def replace_mermaid(match: re.Match) -> str:
        key = _next_key("MERMAID")
        mermaid_batch.append((key, match.group(1)))
        placeholders[key] = (
            f'<pre style="background:#f5f5f5;padding:8pt;'
            f'border-radius:4pt;font-size:10pt">'
            f'{_html_escape(match.group(1))}</pre>'
        )
        return key

    content = re.sub(
        r'```mermaid\n(.*?)\n```',
        replace_mermaid,
        content,
        flags=re.DOTALL,
    )

    # 1b. Protect remaining fenced code blocks and inline code spans so math
    # patterns inside them are NOT rendered as math. Pre-render them to HTML
    # ``<pre><code>`` / ``<code>`` so their placeholders survive the Markdown
    # parser unchanged and are restored as valid HTML afterwards.

    def replace_code_fence(match: re.Match) -> str:
        body = match.group(0)
        # Strip the leading ```lang and trailing ```
        m = re.match(r'```(\w+)?\n?([\s\S]*?)```\s*$', body)
        lang = (m.group(1) if m else "") or ""
        code = (m.group(2) if m else body) or ""
        lang_attr = f' class="language-{_html_escape(lang)}"' if lang else ""
        html_block = (
            f'<pre><code{lang_attr}>{_html_escape(code)}</code></pre>'
        )
        key = _next_key("CODEFENCE")
        placeholders[key] = html_block
        # Surround with blank lines so the Markdown parser treats the
        # placeholder as a block-level element rather than wrapping it in <p>.
        return f"\n\n{key}\n\n"

    content = re.sub(r'```[\s\S]*?```', replace_code_fence, content)

    def replace_inline_code(match: re.Match) -> str:
        body = match.group(0)[1:-1]  # strip surrounding backticks
        key = _next_key("CODEINLINE")
        placeholders[key] = f'<code>{_html_escape(body)}</code>'
        return key

    content = re.sub(r'`[^`\n]+`', replace_inline_code, content)

    # 1c. Render LaTeX display-math delimiters (\\[...\\] and $$...$$)
    # BEFORE inline math so ``$$`` is not consumed as two inline ``$``
    # delimiters.  \\[...\\] is the standard LaTeX display-math syntax and
    # is handled by the frontend; processing it here keeps PDF exports
    # consistent with the browser rendering.
    #
    # ALL formulas are routed through the batch MathJax renderer (one
    # Playwright session) so every formula uses the same high-quality
    # MathJax font instead of the matplotlib mathtext font.
    mathjax_batch: list[tuple[str, str, bool]] = []

    def _try_math_render(latex: str, display: bool, key: str) -> None:
        mathjax_batch.append((key, latex, display))
        placeholders[key] = _render_math_fallback_html(latex, display=display)

    def _replace_display_math(match: re.Match) -> str:
        key = _next_key("MATHDISP")
        _try_math_render(match.group(1).strip(), True, key)
        return key

    # \\[...\\] display math
    content = re.sub(
        r'\\\[([\s\S]*?)\\\]',
        _replace_display_math,
        content,
    )

    # $$...$$ display math
    content = re.sub(
        r'\$\$([^\$]+?)\$\$',
        _replace_display_math,
        content,
        flags=re.DOTALL,
    )

    # 1d. Render inline math: \\(...\\) and $...$.
    # Require a non-space after the opening delimiter and a non-space
    # before the closing delimiter so currency like "$5 and $6" is not
    # accidentally parsed as math.  Also forbid a digit immediately after
    # the closing ``$`` to skip "$100" style prose.

    def _replace_inline_math(match: re.Match) -> str:
        key = _next_key("MATHINLINE")
        _try_math_render(match.group(1), False, key)
        return key

    # \\(...\\) inline math
    content = re.sub(
        r'\\\(([\s\S]*?)\\\)',
        _replace_inline_math,
        content,
    )

    # $...$ inline math
    content = re.sub(
        r'(?<![\\\$])\$(?!\s)([^\$\n]+?)(?<!\s)\$(?!\d)',
        _replace_inline_math,
        content,
    )

    # 1e. Batch-render ALL Mermaid diagrams + MathJax formulas in a SINGLE
    # Playwright session — eliminates per-diagram browser cold-starts.
    if mermaid_batch or mathjax_batch:
        logger.info("Batch-rendering %d mermaid + %d math in one session",
                     len(mermaid_batch), len(mathjax_batch))
        mermaid_results, mathjax_results = _batch_render_mermaid_and_math(
            mermaid_batch, mathjax_batch,
        )
        for key, _code in mermaid_batch:
            if key in mermaid_results:
                placeholders[key] = mermaid_results[key]
        for key, latex, display in mathjax_batch:
            if key in mathjax_results:
                wrapper_class = "math-display" if display else "math-inline"
                wrapper_style = "display:block;text-align:center;margin:6pt 0;" if display else ""
                placeholders[key] = (
                    f'<span class="{wrapper_class}" style="{wrapper_style}">'
                    f'{mathjax_results[key]}</span>'
                )

    # 2. Convert the (now placeholder-only) Markdown to HTML.
    # Use markdown-it-py (CommonMark) which handles nested lists with
    # 3-space indentation correctly – matching the frontend (marked.js).
    html = _md_it.render(content)

    # 3. Restore the rendered SVG / code in place of each placeholder. Strip
    # any wrapping ``<p>...</p>`` the Markdown parser may have added around
    # a lone placeholder line (common for display math / mermaid).
    for key, rendered in placeholders.items():
        html = html.replace(f"<p>{key}</p>", rendered)
        html = html.replace(key, rendered)

    return html


def _slugify_heading(text: str) -> str:
    """URL-safe slug for a heading. Keeps CJK characters intact."""
    import unicodedata
    plain = re.sub(r'<[^>]+>', '', text or '').strip()
    plain = unicodedata.normalize('NFKC', plain).lower()
    slug = re.sub(r'\s+', '-', plain)
    slug = re.sub(r'[^\w\-\u4e00-\u9fff]+', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug or 'section'


def _ensure_heading_ids(html: str) -> str:
    """Make sure every heading has a stable ``id`` attribute so anchor
    links work inside the exported PDF and the WeasyPrint bookmark rules
    can pick them up."""
    used: dict[str, int] = {}

    def _replace(match: re.Match) -> str:
        level = match.group(1)
        attrs = match.group(2) or ''
        inner = match.group(3)
        # If the heading already has an id attribute, keep it.
        if re.search(r'\bid\s*=\s*"', attrs):
            return match.group(0)
        slug = _slugify_heading(inner)
        count = used.get(slug, 0)
        used[slug] = count + 1
        if count:
            slug = f"{slug}-{count}"
        return f'<h{level}{attrs} id="{slug}">{inner}</h{level}>'

    return re.sub(
        r'<h([1-6])([^>]*)>(.*?)</h\1>',
        _replace,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _guess_image_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _resolve_image_path(src: str, workspace_root: str) -> Path | None:
    """Resolve an image reference to an absolute file path inside the workspace."""
    if not src or src.startswith(("http://", "https://", "data:", "blob:")):
        return None
    if src.startswith("/api/images/serve"):
        parsed = urlparse(src)
        path_param = parse_qs(parsed.query).get("path", [""])[0]
        if path_param:
            src = unquote(path_param)
    root = Path(workspace_root).resolve()
    candidate = (root / src).resolve()
    if candidate.exists() and str(candidate).startswith(str(root) + os.sep):
        return candidate
    basename = os.path.basename(src)
    if basename:
        candidate = (root / "noteimg" / basename).resolve()
        if candidate.exists() and str(candidate).startswith(str(root) + os.sep):
            return candidate
    return None


def _embed_image_as_data_uri(file_path: Path) -> str | None:
    try:
        data = file_path.read_bytes()
        mime = _guess_image_mime(str(file_path))
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:
        return None


def _materialize_note_images(content: str, workspace_root: str) -> str:
    """Replace local image references with base64 data URIs for self-contained export."""
    if not content or not workspace_root:
        return content or ""

    def replace_markdown_img(match: re.Match) -> str:
        alt = match.group(1)
        src = match.group(2)
        resolved = _resolve_image_path(src, workspace_root)
        if resolved:
            data_uri = _embed_image_as_data_uri(resolved)
            if data_uri:
                return f"![{alt}]({data_uri})"
        return match.group(0)

    def replace_html_img(match: re.Match) -> str:
        prefix = match.group(1)
        src = match.group(2)
        suffix = match.group(3)
        resolved = _resolve_image_path(src, workspace_root)
        if resolved:
            data_uri = _embed_image_as_data_uri(resolved)
            if data_uri:
                return f'{prefix}src="{data_uri}"{suffix}'
        return match.group(0)

    content = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_markdown_img, content)
    content = re.sub(
        r'(<img[^>]*\s)(?:data-original-src|src)="([^"]+)"([^>]*>)',
        replace_html_img,
        content,
        flags=re.IGNORECASE,
    )
    return content


async def _get_note_workspace_root(db: AsyncSession, user_id: str) -> str | None:
    """Return the user's workspace root path for resolving note images."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    workspace = await ensure_user_workspace(db, user_id, user.username if user else None)
    return workspace.root_path


def _build_note_md(note, workspace_root: str | None = None) -> str:
    """Build a Markdown string for a single note."""
    parts = []
    if note.title:
        parts.append(f"# {note.title}\n")
    content = note.content or ""
    # Exports never embed media resources (product decision): strip them.
    content = _strip_media_tags(content)
    # ECharts fences become self-contained SVG data-URI images in the .md.
    content = _materialize_echarts_blocks(content, as_markdown=True)
    if workspace_root:
        content = _materialize_note_images(content, workspace_root)
    parts.append(content)
    return "\n".join(parts)


def _render_note_pdf(note, workspace_root: str | None = None) -> bytes:
    """Render a single note to PDF bytes via Markdown → HTML (with Mermaid) → WeasyPrint."""
    from weasyprint import HTML, CSS
    from app.services.pdf_fonts import get_font_config_and_css

    content = note.content or ""
    # Exports never embed media resources (product decision): strip them.
    content = _strip_media_tags(content)
    if workspace_root:
        content = _materialize_note_images(content, workspace_root)
    html_body = _markdown_to_html_with_mermaid(content)
    html_body = _ensure_heading_ids(html_body)
    title_html = (
        f'<h1 id="_note_title">{note.title}</h1>' if note.title else ""
    )
    created = note.created_at.strftime("%Y-%m-%d %H:%M") if note.created_at else ""
    meta = f'<div class="meta">{created}</div>' if created else ""

    full_html = (
        f"<!DOCTYPE html><html><head>"
        f"<meta charset='utf-8'>"
        f"</head><body>{title_html}{meta}{html_body}</body></html>"
    )
    font_config, font_css = get_font_config_and_css()
    return HTML(string=full_html).write_pdf(
        stylesheets=[font_css, CSS(string=_PDF_CSS, font_config=font_config)],
        font_config=font_config,
    )


def build_notebook_response(notebook: Notebook, note_count: int) -> NotebookResponse:
    return NotebookResponse(
        id=notebook.id,
        name=notebook.name,
        is_default=notebook.is_default,
        created_at=notebook.created_at.isoformat(),
        updated_at=notebook.updated_at.isoformat(),
        note_count=note_count,
    )


async def get_notebook_note_count(db: AsyncSession, notebook_id: str) -> int:
    count_result = await db.execute(
        select(func.count(Note.id)).where(Note.notebook_id == notebook_id)
    )
    return count_result.scalar() or 0


async def get_user_notebook(
    db: AsyncSession,
    notebook_id: str,
    user_id: str,
) -> Notebook | None:
    result = await db.execute(
        select(Notebook).where(
            Notebook.id == notebook_id,
            Notebook.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


@router.get("/notebooks", response_model=List[NotebookResponse])
async def list_notebooks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Notebook)
        .where(Notebook.user_id == current_user.id)
        .order_by(desc(Notebook.updated_at))
    )
    notebooks = result.scalars().all()

    responses = []
    for nb in notebooks:
        note_count = await get_notebook_note_count(db, nb.id)
        responses.append(build_notebook_response(nb, note_count))
    return responses


@router.get("/search", response_model=List[NoteSearchResult])
async def search_notes(
    q: str,
    notebook_id: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not q or not q.strip():
        return []

    keyword = f"%{q.strip()}%"

    # Build the base query joining Note with Notebook
    query = (
        select(Note, Notebook.name.label("notebook_name"))
        .join(Notebook, Note.notebook_id == Notebook.id)
        .where(
            Notebook.user_id == current_user.id,
            (Note.title.ilike(keyword)) | (Note.content.ilike(keyword))
        )
    )

    if notebook_id:
        query = query.where(Note.notebook_id == notebook_id)

    query = query.order_by(desc(Note.updated_at)).limit(50)
    result = await db.execute(query)
    rows = result.all()

    results = []
    for note, nb_name in rows:
        content = note.content or ""
        lower_content = content.lower()
        lower_q = q.strip().lower()
        idx = lower_content.find(lower_q)
        if idx >= 0:
            start = max(0, idx - 30)
            end = min(len(content), idx + len(q.strip()) + 30)
            snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
        else:
            snippet = content[:80] + ("..." if len(content) > 80 else "")

        results.append(NoteSearchResult(
            note_id=note.id,
            notebook_id=note.notebook_id,
            notebook_name=nb_name,
            title=note.title,
            content_snippet=snippet,
        ))

    return results


@router.post("/notebooks/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_notebooks(
    delete_data: NotebookBulkDelete,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook_ids = list(dict.fromkeys(delete_data.notebook_ids))
    if not notebook_ids:
        raise HTTPException(status_code=400, detail="No notebooks selected")

    result = await db.execute(
        select(Notebook).where(
            Notebook.user_id == current_user.id,
            Notebook.id.in_(notebook_ids),
        )
    )
    notebooks = result.scalars().all()
    if not notebooks:
        raise HTTPException(status_code=404, detail="Notebook not found")

    if any(notebook.is_default for notebook in notebooks):
        raise HTTPException(status_code=400, detail="Cannot delete default notebook")

    for notebook in notebooks:
        await db.delete(notebook)

    await db.commit()
    return BulkDeleteResponse(status="ok", deleted_count=len(notebooks))


@router.post("/notebooks/bulk-export")
async def bulk_export_notebooks(
    export_data: NotebookBulkExport,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook_ids = list(dict.fromkeys(export_data.notebook_ids))
    if not notebook_ids:
        raise HTTPException(status_code=400, detail="No notebooks selected")

    result = await db.execute(
        select(Notebook).where(
            Notebook.user_id == current_user.id,
            Notebook.id.in_(notebook_ids),
        )
    )
    notebooks = result.scalars().all()
    if not notebooks:
        raise HTTPException(status_code=404, detail="Notebooks not found")

    records: list[tuple[str, list[Note]]] = []
    for notebook in notebooks:
        notes_result = await db.execute(
            select(Note)
            .where(Note.notebook_id == notebook.id)
            .order_by(desc(Note.updated_at))
        )
        records.append((notebook.name, notes_result.scalars().all()))

    def _build_zip() -> bytes:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for nb_name, notes in records:
                csv_buffer = io.StringIO()
                writer = csv.DictWriter(
                    csv_buffer,
                    fieldnames=["title", "content", "raw_transcription", "created_at", "updated_at"],
                )
                writer.writeheader()
                for note in notes:
                    writer.writerow({
                        "title": note.title or "",
                        "content": _strip_media_tags(note.content or ""),
                        "raw_transcription": note.raw_transcription or "",
                        "created_at": note.created_at.isoformat(),
                        "updated_at": note.updated_at.isoformat(),
                    })
                csv_filename = f"{sanitize_filename(nb_name)}.csv"
                zf.writestr(csv_filename, csv_buffer.getvalue())
        return zip_buffer.getvalue()

    zip_data = await asyncio.to_thread(_build_zip)
    return StreamingResponse(
        io.BytesIO(zip_data),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="notebooks_export.zip"'
        },
    )


@router.post("/notebooks", response_model=NotebookResponse)
async def create_notebook(
    notebook_data: NotebookCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook = Notebook(
        user_id=current_user.id,
        name=notebook_data.name
    )
    db.add(notebook)
    await db.commit()
    await db.refresh(notebook)
    return build_notebook_response(notebook, 0)


@router.put("/notebooks/{notebook_id}", response_model=NotebookResponse)
async def update_notebook(
    notebook_id: str,
    notebook_data: NotebookUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook = await get_user_notebook(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    notebook.name = notebook_data.name
    await db.commit()
    await db.refresh(notebook)

    note_count = await get_notebook_note_count(db, notebook.id)
    return build_notebook_response(notebook, note_count)


@router.put("/notebooks/{notebook_id}/default", response_model=NotebookResponse)
async def set_default_notebook(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook = await get_user_notebook(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    result = await db.execute(
        select(Notebook).where(
            Notebook.user_id == current_user.id,
            Notebook.is_default == True,
        )
    )
    current_defaults = result.scalars().all()
    for current_default in current_defaults:
        current_default.is_default = False

    notebook.is_default = True

    await db.commit()
    await db.refresh(notebook)

    note_count = await get_notebook_note_count(db, notebook.id)
    return build_notebook_response(notebook, note_count)


@router.get("/notebooks/{notebook_id}/export")
async def export_notebook(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook = await get_user_notebook(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    result = await db.execute(
        select(Note)
        .where(Note.notebook_id == notebook.id)
        .order_by(desc(Note.updated_at))
    )
    notes = result.scalars().all()

    csv_buffer = io.StringIO()
    writer = csv.DictWriter(
        csv_buffer,
        fieldnames=["title", "content", "raw_transcription", "created_at", "updated_at"],
    )
    writer.writeheader()
    for note in notes:
        writer.writerow(
            {
                "title": note.title or "",
                "content": _strip_media_tags(note.content or ""),
                "raw_transcription": note.raw_transcription or "",
                "created_at": note.created_at.isoformat(),
                "updated_at": note.updated_at.isoformat(),
            }
        )

    filename = f"{sanitize_filename(notebook.name)}_{notebook.id}.csv"
    encoded_filename = quote(filename, safe='')
    file_content = csv_buffer.getvalue().encode("utf-8-sig")

    return StreamingResponse(
        io.BytesIO(file_content),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=\"export.csv\"; filename*=UTF-8''{encoded_filename}"
        },
    )


@router.delete("/notebooks/{notebook_id}")
async def delete_notebook(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook = await get_user_notebook(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    if notebook.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete default notebook")

    await db.delete(notebook)
    await db.commit()
    return {"status": "ok"}


@router.get("/notebooks/{notebook_id}/notes", response_model=List[NoteListItem])
async def list_notes(
    notebook_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook = await get_user_notebook(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    result = await db.execute(
        select(Note)
        .where(Note.notebook_id == notebook_id)
        .order_by(desc(Note.updated_at))
    )
    notes = result.scalars().all()

    return [
        NoteListItem(
            id=n.id,
            notebook_id=n.notebook_id,
            title=n.title,
            content_preview=get_content_preview(n.content or ""),
            content_length=len(n.content or ""),
            token_estimate=estimate_tokens(n.content or ""),
            created_at=n.created_at.isoformat(),
            updated_at=n.updated_at.isoformat()
        )
        for n in notes
    ]


@router.post("/notes/bulk-export")
async def bulk_export_notes(
    export_data: NoteBulkExport,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export multiple notes as PDF or MD. Returns a ZIP archive."""
    note_ids = list(dict.fromkeys(export_data.note_ids))
    fmt = export_data.format.lower()
    if fmt not in ("md", "pdf"):
        raise HTTPException(status_code=400, detail="Format must be 'md' or 'pdf'")
    if not note_ids:
        raise HTTPException(status_code=400, detail="No notes selected")

    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id.in_(note_ids),
            Notebook.user_id == current_user.id,
        ).order_by(desc(Note.updated_at))
    )
    notes = result.scalars().all()
    if not notes:
        raise HTTPException(status_code=404, detail="Notes not found")

    workspace_root = await _get_note_workspace_root(db, current_user.id)

    items: list[tuple[str, str, bytes]] = []
    for note in notes:
        title = note.title or "untitled"
        safe_name = sanitize_filename(title)
        if fmt == "md":
            content = await asyncio.to_thread(_build_note_md, note, workspace_root)
            items.append((safe_name, "md", content.encode("utf-8")))
        else:
            try:
                pdf_bytes = await asyncio.to_thread(_render_note_pdf, note, workspace_root)
            except Exception:
                logger.exception("PDF rendering failed for note %s", note.id)
                pdf_bytes = b""
            items.append((safe_name, "pdf", pdf_bytes))

    def _build_zip() -> bytes:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for safe_name, ext, data in items:
                zf.writestr(f"{safe_name}.{ext}", data)
        return zip_buffer.getvalue()

    zip_data = await asyncio.to_thread(_build_zip)
    return StreamingResponse(
        io.BytesIO(zip_data),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="notes_export.zip"'
        },
    )


@router.get("/notes/{note_id}/export")
async def export_note(
    note_id: str,
    format: str = "md",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export a single note as PDF or Markdown."""
    fmt = format.lower()
    if fmt not in ("md", "pdf"):
        raise HTTPException(status_code=400, detail="Format must be 'md' or 'pdf'")

    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id == note_id,
            Notebook.user_id == current_user.id,
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    workspace_root = await _get_note_workspace_root(db, current_user.id)

    title = note.title or "untitled"
    safe_name = sanitize_filename(title)

    if fmt == "md":
        content = await asyncio.to_thread(_build_note_md, note, workspace_root)
        encoded = quote(f"{safe_name}.md", safe='')
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=\"note.md\"; filename*=UTF-8''{encoded}"
            },
        )
    else:
        try:
            pdf_bytes = await asyncio.to_thread(_render_note_pdf, note, workspace_root)
        except Exception:
            logger.exception("PDF rendering failed")
            raise HTTPException(status_code=500, detail="PDF rendering failed")
        encoded = quote(f"{safe_name}.pdf", safe='')
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=\"note.pdf\"; filename*=UTF-8''{encoded}"
            },
        )


@router.post("/notes/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_notes(
    delete_data: NoteBulkDelete,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    note_ids = list(dict.fromkeys(delete_data.note_ids))
    if not note_ids:
        raise HTTPException(status_code=400, detail="No notes selected")

    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id.in_(note_ids),
            Notebook.user_id == current_user.id,
        )
    )
    notes = result.scalars().all()
    if not notes:
        raise HTTPException(status_code=404, detail="Note not found")

    for note in notes:
        await db.delete(note)

    await db.commit()
    return BulkDeleteResponse(status="ok", deleted_count=len(notes))


@router.get("/notes/{note_id}", response_model=NoteResponse)
async def get_note(
    note_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id == note_id,
            Notebook.user_id == current_user.id
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    return NoteResponse(
        id=note.id,
        notebook_id=note.notebook_id,
        title=note.title,
        content=note.content,
        raw_transcription=note.raw_transcription,
        created_at=note.created_at.isoformat(),
        updated_at=note.updated_at.isoformat()
    )


@router.post("/notebooks/{notebook_id}/notes", response_model=NoteResponse)
async def create_note(
    notebook_id: str,
    note_data: NoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    notebook = await get_user_notebook(db, notebook_id, current_user.id)
    if not notebook:
        raise HTTPException(status_code=404, detail="Notebook not found")

    note = Note(
        notebook_id=notebook_id,
        title=note_data.title,
        content=note_data.content,
        raw_transcription=note_data.raw_transcription
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    return NoteResponse(
        id=note.id,
        notebook_id=note.notebook_id,
        title=note.title,
        content=note.content,
        raw_transcription=note.raw_transcription,
        created_at=note.created_at.isoformat(),
        updated_at=note.updated_at.isoformat()
    )


@router.put("/notes/{note_id}", response_model=NoteResponse)
async def update_note(
    note_id: str,
    note_data: NoteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id == note_id,
            Notebook.user_id == current_user.id
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    if note_data.title is not None:
        note.title = note_data.title
    if note_data.content is not None:
        note.content = note_data.content

    await db.commit()
    await db.refresh(note)

    return NoteResponse(
        id=note.id,
        notebook_id=note.notebook_id,
        title=note.title,
        content=note.content,
        raw_transcription=note.raw_transcription,
        created_at=note.created_at.isoformat(),
        updated_at=note.updated_at.isoformat()
    )


@router.delete("/notes/{note_id}")
async def delete_note(
    note_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id == note_id,
            Notebook.user_id == current_user.id
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    await db.delete(note)
    await db.commit()
    return {"status": "ok"}


@router.post("/quick", response_model=NoteResponse)
async def create_quick_note(
    note_data: QuickNoteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if note_data.notebook_id:
        notebook = await get_user_notebook(db, note_data.notebook_id, current_user.id)
        if not notebook:
            raise HTTPException(status_code=404, detail="Notebook not found")
    else:
        nb_result = await db.execute(
            select(Notebook).where(
                Notebook.user_id == current_user.id,
                Notebook.is_default == True
            )
        )
        notebook = nb_result.scalar_one_or_none()

        if not notebook:
            notebook = Notebook(
                user_id=current_user.id,
                name="默认笔记本",
                is_default=True
            )
            db.add(notebook)
            await db.commit()
            await db.refresh(notebook)

    note = Note(
        notebook_id=notebook.id,
        content=note_data.transcription,
        raw_transcription=note_data.transcription
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    return NoteResponse(
        id=note.id,
        notebook_id=note.notebook_id,
        title=note.title,
        content=note.content,
        raw_transcription=note.raw_transcription,
        created_at=note.created_at.isoformat(),
        updated_at=note.updated_at.isoformat()
    )


@router.get("/default-notebook")
async def get_default_notebook(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Notebook).where(
            Notebook.user_id == current_user.id,
            Notebook.is_default == True
        )
    )
    notebook = result.scalar_one_or_none()

    if not notebook:
        notebook = Notebook(
            user_id=current_user.id,
            name="默认笔记本",
            is_default=True
        )
        db.add(notebook)
        await db.commit()
        await db.refresh(notebook)

    note_count = await get_notebook_note_count(db, notebook.id)
    return build_notebook_response(notebook, note_count)


@router.put("/notes/{note_id}/move", response_model=NoteResponse)
async def move_note(
    note_id: str,
    move_data: NoteMoveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id == note_id,
            Notebook.user_id == current_user.id,
        )
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    target_notebook = await get_user_notebook(db, move_data.target_notebook_id, current_user.id)
    if not target_notebook:
        raise HTTPException(status_code=404, detail="Target notebook not found")

    note.notebook_id = target_notebook.id
    await db.commit()
    await db.refresh(note)

    return NoteResponse(
        id=note.id,
        notebook_id=note.notebook_id,
        title=note.title,
        content=note.content,
        raw_transcription=note.raw_transcription,
        created_at=note.created_at.isoformat(),
        updated_at=note.updated_at.isoformat(),
    )


@router.post("/notes/bulk-move", response_model=BulkMoveResponse)
async def bulk_move_notes(
    move_data: NoteBulkMoveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    note_ids = list(dict.fromkeys(move_data.note_ids))
    if not note_ids:
        raise HTTPException(status_code=400, detail="No notes selected")

    target_notebook = await get_user_notebook(db, move_data.target_notebook_id, current_user.id)
    if not target_notebook:
        raise HTTPException(status_code=404, detail="Target notebook not found")

    result = await db.execute(
        select(Note).join(Notebook).where(
            Note.id.in_(note_ids),
            Notebook.user_id == current_user.id,
        )
    )
    notes = result.scalars().all()
    if not notes:
        raise HTTPException(status_code=404, detail="Notes not found")

    for note in notes:
        note.notebook_id = target_notebook.id

    await db.commit()
    return BulkMoveResponse(status="ok", moved_count=len(notes))
