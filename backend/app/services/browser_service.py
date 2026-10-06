# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from app.core.config import get_config
from app.services.http_client import get_shared_async_client

config = get_config()
logger = logging.getLogger(__name__)

_br_cfg = config.browser
_PLAYWRIGHT_NETWORKIDLE_TIMEOUT = int(_br_cfg.get("playwright_networkidle_timeout_ms", 3000))
_PLAYWRIGHT_NAVIGATION_TIMEOUT = int(_br_cfg.get("playwright_navigation_timeout_ms", 5000))
_PLAYWRIGHT_CHALLENGE_WAIT_MS = int(_br_cfg.get("playwright_challenge_wait_ms", 8000))
_HTTPX_HTML_TRUNCATION = int(_br_cfg.get("httpx_html_truncation", 500_000))
_MIN_TEXT_FOR_SKIP_PLAYWRIGHT = int(_br_cfg.get("min_text_for_skip_playwright", 200))
_TITLE_TRUNCATION = int(_br_cfg.get("title_truncation", 500))
_IGNORE_HTTPS_ERRORS = bool(_br_cfg.get("ignore_https_errors", True))

_FETCH_SEMAPHORE = asyncio.Semaphore(10)

_insecure_httpx_client: Optional[httpx.AsyncClient] = None


def _get_fetch_client() -> httpx.AsyncClient:
    """Return the httpx client for page fetching.

    When ``browser.ignore_https_errors`` is enabled (default), use a dedicated
    client with TLS verification disabled so self-signed / intranet sites are
    reachable — mirroring what a user clicking "Advanced → Proceed" does in a
    regular browser. The shared client (used by API calls) keeps verification
    enabled.
    """
    if not _IGNORE_HTTPS_ERRORS:
        return get_shared_async_client()
    global _insecure_httpx_client
    if _insecure_httpx_client is None:
        _insecure_httpx_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=True,
            verify=False,
        )
    return _insecure_httpx_client

_CF_CHALLENGE_INDICATORS = [
    "just a moment",
    "checking your browser",
    "please wait",
    "security verification",
    "cf-browser-verification",
    "challenge-platform",
    "performing security check",
    # WeChat public-account verification wall (mp.weixin.qq.com)
    "环境异常",
    "完成验证",
    "请在微信客户端打开",
]

_DESKTOP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# WeChat article pages only serve real content to requests whose UA carries the
# MicroMessenger keyword; anything else gets a "环境异常" verification wall.
# This UA mirrors the WeChat iOS in-app WebView and must pair with a
# https://mp.weixin.qq.com Referer.
_WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.49(0x18003123) NetType/WIFI Language/zh_CN"
)
_WECHAT_REFERER = "https://mp.weixin.qq.com"
# Full wechat article HTML is ~3MB and the #js_content div sits past the
# generic 500KB truncation point, so fetch the whole document.
_WECHAT_HTML_LIMIT = 5_000_000


@dataclass
class PageContent:
    url: str
    title: str
    text: str
    error: Optional[str] = None
    # 图片能力缺口修复（2026-10-05，conv 0d4caea5）：页面图片直址——
    # og:image/<img>/srcset 抽取、绝对化、tracking/1x1/data 过滤、去重、
    # ≤[browser] max_images_per_page。agent 可据此内嵌 ![alt](src) 或下载。
    images: List[Dict[str, Any]] = field(default_factory=list)


# 图片候选过滤（2026-10-05）：明确的追踪/广告/占位图一律丢弃——
# 宁可多留内容图，也不让像素噪音稀释 agent 上下文。
# 评审 minor：/blanket、/ad-hoc 误伤收紧（/blank[.-]、去掉裸 /ad-）；
# 裸端点形态（/spacer、/blank 无扩展名=经典 1x1 handler）补兜（minor⑩）。
_IMAGE_URL_BLACKLIST = re.compile(
    r"(?:/pixel|/beacon|/blank[.-]|spacer[.-]|/1x1|/track(?:ing|er)?[./]|analytics|"
    r"doubleclick|googlesyndication|adsystem|adservice|/ads?/|"
    r"scorecardresearch|quantserve|/stats?[./]|spinner|/loading[.-]"
    r"|(?:/spacer|/blank)(?:[?]|$))",
    re.I,
)

# 图片 src 上限（minor⑫）：签名巨 URL（1-2KB presigned 常见）放行，>2048 丢弃。
_IMAGE_SRC_MAX_CHARS = 2048


def _image_dims(img_tag) -> tuple:
    def _d(v):
        try:
            return int(str(v).strip().rstrip("px"))
        except (TypeError, ValueError):
            return None
    return _d(img_tag.get("width")), _d(img_tag.get("height"))


_SRCSET_DESC_RE = re.compile(r"^\d+(?:\.\d+)?[wx]$", re.I)

# srcset 最大档解算（JS 版，与 _pick_srcset_largest 同语义——node 对拍测试钉住）：
# 空白切分 + 描述符分隔 + 描述符后逗号归一化（锚定行首/空白前导，URL query
# 内的 400w,800w 不腐坏）+ data-URI 候选续段跳过。
_SRCSET_PICK_JS = """function(ss) {
    if (!ss) return '';
    const norm = ss.replace(/(?:^|(\\s))(\\d+(?:\\.\\d+)?[wx])\\s*,\\s*/gi, '$1$2, ');
    let best = '', bw = -1, parts = [], skipData = false;
    const toks = norm.split(/\\s+/);
    const flush = (desc) => {
        const url = parts.join(' ').replace(/^,+|,+$/g, '');
        parts = [];
        if (!url || (!url.includes('/') && !url.includes('.'))) return;
        const w = desc ? (parseFloat(desc) || 0) : 0;
        if (w >= bw) { best = url; bw = w; }
    };
    for (let i = 0; i < toks.length; i++) {
        const tok = toks[i].replace(/,+$/, '');
        if (!tok) continue;
        if (skipData) {
            if (/^\\d+(\\.\\d+)?[wx]$/i.test(tok) && i + 1 < toks.length) skipData = false;
            continue;
        }
        if (tok.startsWith('data:')) { parts = []; skipData = true; continue; }
        const m = tok.match(/^(\\d+(?:\\.\\d+)?)([wx])$/i);
        if (m) flush(m[1]); else parts.push(tok);
    }
    flush(null);
    return best;
}"""

# 页面图片候选抽取 JS（Playwright 两路径共享）：og:image + <img>（currentSrc
# 已含懒加载/srcset 解算；data: 时回退 data-src/data-original/data-lazy-src；
# 再不行 srcset 解算）。
_IMAGES_JS = """() => {
    const out = [];
    const og = document.querySelector(
        'meta[property="og:image"],meta[name="og:image"],'
        + 'meta[property="og:image:url"],meta[name="twitter:image"]');
    if (og && og.content) {
        out.push({src: og.content, kind: 'og', alt: document.title || ''});
    }
    const pickSrcset = __SRCSET_PICK_JS__;
    for (const img of document.querySelectorAll('img')) {
        let src = img.currentSrc || img.src || '';
        if (!src || src.startsWith('data:')) {
            src = img.getAttribute('data-src')
                || img.getAttribute('data-original')
                || img.getAttribute('data-lazy-src') || '';
        }
        if (!src) src = pickSrcset(img.getAttribute('srcset') || '');
        if (!src) continue;
        const w = img.naturalWidth
            || (parseInt(img.getAttribute('width')) || null);
        const h = img.naturalHeight
            || (parseInt(img.getAttribute('height')) || null);
        out.push({src, alt: img.alt || '', width: w, height: h, kind: 'img'});
    }
    return out;
}""".replace("__SRCSET_PICK_JS__", "(" + _SRCSET_PICK_JS + ")")


def _pick_srcset_largest(srcset: str) -> Optional[str]:
    """srcset 取最大档（w/x 描述符；缺描述符取最后一项）。

    按**空白**切分（srcset 语法里 URL 不含空格）；描述符后的逗号先归一化
    补空格（`a.jpg 400w,b.jpg 800w` 合法形态——只切描述符紧跟的逗号，CDN
    URL 内逗号（Cloudinary w_400,h_400）不动）；data-URI 候选单独丢弃
    （minor⑨：URL query 里出现 `?fmt=data:uri` 不误杀整串）。
    """
    if not srcset:
        return None
    # 描述符后逗号归一化（`a.jpg 400w,b.jpg` → `a.jpg 400w, b.jpg`）——锚定
    # 行首/空白前导（I-B2：URL query 内的 `sizes=400w,800w` 不前导空白，不切）。
    norm = re.sub(r"(?:(?<=^)|(?<=\s))(\d+(?:\.\d+)?[wx])\s*,\s*", r"\1, ", srcset, flags=re.I)
    best_url, best_w = None, -1.0
    url_parts: List[str] = []
    skip_until_desc = False
    toks = norm.split()

    def _flush(desc: Optional[str]) -> None:
        nonlocal best_url, best_w
        if not url_parts:
            return
        url = " ".join(url_parts).strip().strip(",")
        url_parts.clear()
        if not url or ("/" not in url and "." not in url):
            return
        w = 0.0
        d = (desc or "").lower().rstrip("wx")
        if d:
            try:
                w = float(d)
            except ValueError:
                w = 0.0
        if w >= best_w:
            best_url, best_w = url, w

    for i, tok in enumerate(toks):
        t = tok.strip().rstrip(",")
        if not t:
            continue
        if skip_until_desc:
            # data-URI 内空白/逗号碎出的续段：仅在「后面还有内容」的描述符处
            # 退出跳过态（A-m5：尾巴恰像描述符的碎段不得越狱）
            if _SRCSET_DESC_RE.match(t) and i + 1 < len(toks):
                skip_until_desc = False
            continue
        if t.startswith("data:"):
            # data-URI 候选：丢弃该候选已累积的 URL 段并进入续段跳过态
            url_parts.clear()
            skip_until_desc = True
            continue
        if _SRCSET_DESC_RE.match(t):
            _flush(t)
        else:
            url_parts.append(t)
    _flush(None)
    return best_url


def _filter_image_candidates(cands: List[Dict[str, Any]], base_url: str, limit: int) -> List[Dict[str, Any]]:
    """候选清单统一过滤：http(s) 绝对化、tracking/1x1/data 丢弃、去重、限量。

    og:image 候选（kind=og）须排最前（调用方保证顺序），去重保留先见者。
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    if limit <= 0:
        return out  # 运营关闭档（评审 B#1 越位修复：limit=0 一张不出）
    for c in cands:
        src = (c.get("src") or "").strip()
        if not src or src.startswith(("data:", "blob:", "javascript:", "#")):
            continue
        if len(src) > _IMAGE_SRC_MAX_CHARS:
            continue  # 签名巨 URL（minor⑫）：丢弃不截断（截断即坏链）
        src = urljoin(base_url, src)
        if not src.startswith(("http://", "https://")):
            continue
        if _IMAGE_URL_BLACKLIST.search(src):
            continue
        w, h = c.get("width"), c.get("height")
        # 1x1 追踪像素：仅当尺寸确知（>0）且 ≤3px 才丢；0/None=未知一律保留
        if isinstance(w, int) and isinstance(h, int) and 0 < w <= 3 and 0 < h <= 3:
            continue
        if src in seen:
            continue
        seen.add(src)
        item: Dict[str, Any] = {"src": src, "kind": c.get("kind") or "img"}
        # 评审 A#2：alt 换行/连续空白折叠——页面可控文本不得伪造清单项注入
        # LLM 的图片挑选通道（防「alt 藏一条假图片项」）
        alt = re.sub(r"\s+", " ", (c.get("alt") or "").strip())
        if alt:
            item["alt"] = alt[:140]
        if isinstance(w, int) and w > 0:
            item["width"] = w
        if isinstance(h, int) and h > 0:
            item["height"] = h
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _is_challenge_page(title: str, text: str) -> bool:
    combined = (title + " " + text[:2000]).lower()
    return any(ind in combined for ind in _CF_CHALLENGE_INDICATORS)


def _is_wechat_url(url: str) -> bool:
    return "mp.weixin.qq.com" in (url or "").lower()


class BrowserService:
    """Fetch and extract readable text content from web pages.

    Strategy:
      1. Try httpx first (fast, no JS).  If sufficient text returned, done.
      2. Fall back to Playwright with stealth-like settings:
         - Desktop User-Agent
         - Realistic viewport
         - Challenge-page detection with wait-and-retry
      3. If Playwright also hits a challenge page, wait for JS resolution
         (Cloudflare turnstile etc.) before extracting content.
    """

    async def fetch_page(self, url: str, *, timeout: float = 15.0) -> PageContent:
        max_len = config.browser_max_content_length

        page = await self._fetch_httpx(url, timeout=timeout, max_len=max_len)
        if page and len(page.text) > _MIN_TEXT_FOR_SKIP_PLAYWRIGHT and not page.error:
            if not _is_challenge_page(page.title, page.text):
                return page

        pw_page = await self._fetch_playwright(url, timeout=timeout, max_len=max_len)
        if pw_page:
            return pw_page

        if page:
            return page

        return PageContent(url=url, title="", text="", error="Failed to fetch page")

    async def fetch_pages(
        self,
        urls: List[str],
        *,
        timeout: float = 15.0,
        max_pages: int = config.browser_max_pages,
    ) -> List[PageContent]:
        async def _fetch_one(url: str) -> PageContent:
            async with _FETCH_SEMAPHORE:
                return await self.fetch_page(url, timeout=timeout)

        tasks = [_fetch_one(url) for url in urls[:max_pages]]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    async def fetch_paginated(
        self,
        start_url: str,
        *,
        max_pages: int = 10,
        timeout: float = 20.0,
    ) -> List[PageContent]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.debug("Playwright not installed, paginated browse unavailable")
            page = await self.fetch_page(start_url, timeout=timeout)
            return [page] if page else []

        if not config.browser_enabled:
            return []

        max_len = config.browser_max_content_length
        collected: List[PageContent] = []
        seen_urls: set = set()

        next_selectors = [
            'a[title="下页"]',
            'a[title="下一页"]',
            'a.default_pgNext',
            'a.nextPage',
            'a.next',
            'a[rel="next"]',
            'link[rel="next"]',
            'a:has-text("下一页")',
            'a:has-text("Next")',
        ]

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            try:
                context = await browser.new_context(
                    user_agent=_DESKTOP_UA,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    java_script_enabled=True,
                    ignore_https_errors=_IGNORE_HTTPS_ERRORS,
                )
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = await context.new_page()
                current_url = start_url
                for _ in range(max_pages):
                    if current_url in seen_urls:
                        break
                    seen_urls.add(current_url)
                    try:
                        await page.goto(
                            current_url,
                            wait_until="domcontentloaded",
                            timeout=int(timeout * 1000),
                        )
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=_PLAYWRIGHT_NETWORKIDLE_TIMEOUT
                            )
                        except Exception:
                            pass

                        title_text = await page.title()
                        body_text = await page.evaluate("document.body.innerText")
                        if _is_challenge_page(title_text, body_text or ""):
                            body_text = await self._wait_for_challenge(
                                page, max_len=max_len
                            )
                            title_text = await page.title()

                        text = (body_text or "")[:max_len]
                        try:
                            raw_imgs = await page.evaluate(_IMAGES_JS)
                            images = _filter_image_candidates(
                                raw_imgs or [], current_url,
                                config.browser_max_images_per_page,
                            )
                        except Exception:
                            images = []
                    except Exception as exc:
                        logger.debug(
                            "Paginated fetch failed for %s: %s", current_url, exc
                        )
                        collected.append(
                            PageContent(
                                url=current_url,
                                title="",
                                text="",
                                error=str(exc),
                            )
                        )
                        break
                    collected.append(
                        PageContent(
                            url=current_url,
                            title=(title_text or "")[:_TITLE_TRUNCATION],
                            text=text,
                            images=images,
                        )
                    )

                    next_href: Optional[str] = None
                    for sel in next_selectors:
                        try:
                            el = await page.query_selector(sel)
                        except Exception:
                            el = None
                        if not el:
                            continue
                        try:
                            href = await el.get_attribute("href")
                        except Exception:
                            href = None
                        if not href:
                            continue
                        if href.strip().lower().startswith("javascript:"):
                            try:
                                async with page.expect_navigation(
                                    timeout=_PLAYWRIGHT_NAVIGATION_TIMEOUT, wait_until="domcontentloaded"
                                ):
                                    await el.click()
                                next_href = page.url
                            except Exception:
                                next_href = None
                            break
                        try:
                            next_href = urljoin(page.url, href)
                        except Exception:
                            next_href = href
                        break

                    if not next_href or next_href == current_url:
                        break
                    current_url = next_href
            finally:
                await browser.close()

        return collected

    @staticmethod
    async def _wait_for_challenge(page, *, max_len: int = 30000) -> str:
        logger.info("Challenge page detected, waiting for resolution...")
        try:
            await page.wait_for_url(
                lambda url: True,
                timeout=_PLAYWRIGHT_CHALLENGE_WAIT_MS,
            )
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        text = await page.evaluate("document.body.innerText")
        return (text or "")[:max_len]

    @staticmethod
    async def _fetch_httpx(
        url: str, *, timeout: float, max_len: int
    ) -> Optional[PageContent]:
        try:
            client = _get_fetch_client()
            wechat = _is_wechat_url(url)
            headers = {
                "User-Agent": _WECHAT_UA if wechat else _DESKTOP_UA,
            }
            if wechat:
                headers["Referer"] = _WECHAT_REFERER
                headers["Accept-Language"] = "zh-CN,zh;q=0.9"
            resp = await client.get(
                url,
                timeout=timeout,
                headers=headers,
            )
            if resp.status_code != 200:
                return None
            html_limit = _WECHAT_HTML_LIMIT if wechat else _HTTPX_HTML_TRUNCATION
            html = BrowserService._decode_html_bytes(
                resp.content, resp.headers.get("content-type")
            )[:html_limit]
        except Exception:
            logger.debug("httpx fetch failed for %s", url)
            return None

        return await BrowserService._extract_content(url, html, max_len)

    @staticmethod
    def _decode_html_bytes(raw: bytes, content_type: Optional[str]) -> str:
        """Decode HTML bytes using charset declarations (HTTP header → meta tag).

        Falls back to UTF-8 with replacement. Normalizes GB2312/GBK to GB18030
        for broader Chinese character coverage, which fixes mojibake on sites
        like chinanews.com.cn that ship GB2312 content without a charset in
        the Content-Type header.
        """
        if not raw:
            return ""

        encoding: Optional[str] = None
        # 1. HTTP Content-Type header
        if content_type:
            m = re.search(r"charset=([\w\-]+)", content_type, re.I)
            if m:
                encoding = m.group(1).strip().strip('"\'')
        # 2. <meta charset> or <meta http-equiv="Content-Type">
        if not encoding:
            head = raw[:4096]
            m = re.search(rb'<meta[^>]+charset\s*=\s*["\']?\s*([\w\-]+)', head, re.I)
            if m:
                try:
                    encoding = m.group(1).decode("ascii", "ignore").strip()
                except Exception:
                    encoding = None
        # 3. Normalize / default
        if not encoding:
            encoding = "utf-8"
        normalized = encoding.strip().lower().replace("_", "-")
        if normalized in ("gb2312", "gbk", "gb-2312"):
            encoding = "gb18030"
        elif normalized in ("big5", "big-5"):
            encoding = "big5hkscs"

        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    async def _fetch_playwright(
        url: str, *, timeout: float, max_len: int
    ) -> Optional[PageContent]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.debug("Playwright not installed, skipping fallback")
            return None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    wechat = _is_wechat_url(url)
                    context = await browser.new_context(
                        user_agent=_WECHAT_UA if wechat else _DESKTOP_UA,
                        viewport={"width": 1920, "height": 1080},
                        locale="zh-CN" if wechat else "en-US",
                        java_script_enabled=True,
                        ignore_https_errors=_IGNORE_HTTPS_ERRORS,
                        extra_http_headers={"Referer": _WECHAT_REFERER} if wechat else None,
                    )
                    await context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                    page = await context.new_page()
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(timeout * 1000),
                    )
                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=_PLAYWRIGHT_NETWORKIDLE_TIMEOUT
                        )
                    except Exception:
                        pass

                    title = await page.title()
                    text = await page.evaluate("document.body.innerText")

                    if _is_challenge_page(title, text or ""):
                        text = await BrowserService._wait_for_challenge(
                            page, max_len=max_len
                        )
                        title = await page.title()

                    text = (text or "")[:max_len]
                    # 图片抽取（图片能力缺口修复）：同一页面上下文内取 og/img
                    # 候选（currentSrc 已含懒加载解算），Python 侧统一过滤。
                    try:
                        raw_imgs = await page.evaluate(_IMAGES_JS)
                        images = _filter_image_candidates(
                            raw_imgs or [], url,
                            config.browser_max_images_per_page,
                        )
                    except Exception:
                        images = []
                    return PageContent(url=url, title=title or "", text=text, images=images)
                finally:
                    await browser.close()
        except Exception:
            logger.debug("Playwright fetch failed for %s", url)
            return None

    @staticmethod
    def _make_soup(html: str):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _extract_images(url: str, soup, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """从 soup 抽取页面图片直址（og:image 优先，其后 <img> 按文档序）。

        <img> 取址顺序：src（非 data: 时）→ data-src/data-original/data-lazy-src
        （懒加载真身）→ srcset 最大档。过滤/绝对化/去重由
        _filter_image_candidates 统一承担。
        """
        if limit is None:
            limit = config.browser_max_images_per_page
        cands: List[Dict[str, Any]] = []
        for prop in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image"):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                cands.append({"src": tag["content"].strip(), "kind": "og",
                              "alt": (soup.title.string.strip() if soup.title and soup.title.string else "")})
        for img in soup.find_all("img"):
            w, h = _image_dims(img)
            src = img.get("src") or ""
            if not src or src.startswith("data:"):
                src = (img.get("data-src") or img.get("data-original")
                       or img.get("data-lazy-src") or "")
            if not src and img.get("srcset"):
                src = _pick_srcset_largest(img.get("srcset") or "") or ""
            if src:
                cands.append({"src": src, "alt": img.get("alt") or "",
                              "width": w, "height": h, "kind": "img"})
        return _filter_image_candidates(cands, url, limit)

    @staticmethod
    async def _extract_content(url: str, html: str, max_len: int) -> PageContent:
        if _is_wechat_url(url):
            return await asyncio.to_thread(
                BrowserService._extract_wechat_content, url, html, max_len
            )
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return PageContent(url=url, title="", text=html[:max_len])

        def _parse():
            soup = BeautifulSoup(html, "html.parser")
            images = BrowserService._extract_images(url, soup)
            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()

            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = text[:max_len]
            return PageContent(url=url, title=title, text=text, images=images)

        return await asyncio.to_thread(_parse)

    @staticmethod
    def _extract_wechat_content(url: str, html: str, max_len: int) -> PageContent:
        """Extract title/account/body from a mp.weixin.qq.com article page.

        The article title lives in the JS var ``msg_title`` (single or double
        quoted) with ``og:title`` as fallback; the body is inside the
        ``#js_content`` div. Falls back to generic body extraction when the
        page is not a valid article (e.g. verification wall / deleted page).
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        images = BrowserService._extract_images(url, soup)
        title = ""
        m = re.search(r"var msg_title\s*=\s*(['\"])(.*?)\1", html, re.S)
        if m:
            title = m.group(2).strip()
        if not title:
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                title = og["content"].strip()

        account = ""
        m = re.search(r"var js_name\s*=\s*(['\"])(.*?)\1", html, re.S)
        if m:
            account = m.group(2).strip()

        content = soup.find("div", id="js_content")
        parts: List[str] = []
        if account:
            parts.append(f"公众号: {account}")
        if content:
            for tag in content.find_all(["script", "style"]):
                tag.decompose()
            body = content.get_text(separator="\n", strip=True)
            body = re.sub(r"\n{3,}", "\n\n", body)
            if body:
                parts.append(body)
        if not parts:
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                tag.decompose()
            fallback = soup.get_text(separator="\n", strip=True)
            fallback = re.sub(r"\n{3,}", "\n\n", fallback)
            parts.append(fallback)

        return PageContent(url=url, title=title, text="\n\n".join(parts)[:max_len], images=images)
