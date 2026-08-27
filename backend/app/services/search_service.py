# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json as _json
import logging
import re as _re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from app.core.config import get_config
from app.services.http_client import get_shared_async_client

config = get_config()
logger = logging.getLogger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    published_date: Optional[str] = field(default=None)
    # 溯源元数据（落库用）：实际命中的搜索引擎与产生该 hit 的查询词。
    # 由 search() 在每个 provider 命中后填充，不进 format_hits 展示。
    provider: Optional[str] = field(default=None)
    query: Optional[str] = field(default=None)

    def to_dict(self) -> dict:
        return asdict(self)


def _extract_year_from_url(url: str) -> int | None:
    """Try to extract a 4-digit year from URL path (e.g. /2024/03/ or /2024-03/)."""
    m = _re.search(r'/(?:20[12]\d)(?=[/\-])', url)
    if m:
        try:
            return int(m.group(0).strip('/'))
        except ValueError:
            return None
    return None


# Date patterns commonly found in page text / meta tags
_DATE_PATTERNS = [
    # 2025-01-15, 2025/01/15
    _re.compile(r'(20[12]\d[/\-](?:0[1-9]|1[0-2])[/\-](?:0[1-9]|[12]\d|3[01]))'),
    # 2025年1月15日
    _re.compile(r'(20[12]\d)\u5e74\s*((?:0?[1-9]|1[0-2]))\u6708\s*((?:0?[1-9]|[12]\d|3[01]))\u65e5'),
]


def _normalise_date(raw: str) -> Optional[str]:
    """Try to normalise a raw date string into YYYY-MM-DD."""
    raw = raw.strip()
    # Already YYYY-MM-DD or YYYY/MM/DD
    m = _re.match(r'^(20[12]\d)[/\-](0?[1-9]|1[0-2])[/\-](0?[1-9]|[12]\d|3[01])', raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # ISO 8601 with T
    m = _re.match(r'^(20[12]\d)[/\-](0?[1-9]|1[0-2])[/\-](0?[1-9]|[12]\d|3[01])T', raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


async def _extract_date_from_html(html: str) -> Optional[str]:
    """Extract publish date from HTML content using BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    def _parse():
        try:
            return BeautifulSoup(html, "html.parser")
        except Exception:
            return None

    soup = await asyncio.to_thread(_parse)
    if soup is None:
        return None

    # 1. Meta tags: property, name, and itemprop attributes
    meta_props = [
        "article:published_time", "og:article:published_time",
        "og:release_date",
        "datePublished", "publishdate", "pubdate",
        "date", "sailthru.date", "DC.date.issued",
    ]
    for prop in meta_props:
        tag = (
            soup.find("meta", attrs={"property": prop})
            or soup.find("meta", attrs={"name": prop})
            or soup.find("meta", attrs={"itemprop": prop})
        )
        if tag and tag.get("content"):
            d = _normalise_date(tag["content"])
            if d:
                return d

    # 2. <time> elements with datetime attribute
    for time_el in soup.find_all("time", attrs={"datetime": True}):
        d = _normalise_date(time_el["datetime"])
        if d:
            return d

    # 3. JSON-LD structured data
    for script_tag in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            ld = json.loads(script_tag.string or "")
            if isinstance(ld, list):
                ld = ld[0] if ld else {}
            for key in ("datePublished", "dateCreated", "uploadDate"):
                val = ld.get(key)
                if val:
                    d = _normalise_date(str(val))
                    if d:
                        return d
        except Exception:
            continue

    # 4. Text patterns in first 5000 chars of body text
    body_text = soup.get_text()[:5000]
    for pat in _DATE_PATTERNS:
        m = pat.search(body_text)
        if m:
            groups = m.groups()
            if len(groups) == 1:
                d = _normalise_date(groups[0])
            elif len(groups) >= 3:
                d = f"{groups[0]}-{int(groups[1]):02d}-{int(groups[2]):02d}"
            else:
                d = None
            if d:
                return d

    return None


async def _fetch_publish_date(url: str, timeout: float = 5.0) -> Optional[str]:
    """Fetch a URL and try to extract the publish date from HTML.
    Uses httpx first; falls back to Playwright for JS-rendered pages."""

    # --- Try httpx (fast, no JS) ---
    html = None
    try:
        client = get_shared_async_client()
        resp = await client.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
        )
        if resp.status_code == 200:
            html = resp.text[:200_000]
    except Exception:
        logger.debug("httpx fetch failed for %s", url)

    if html:
        date = await _extract_date_from_html(html)
        if date:
            return date

    return None


async def _fetch_date_playwright(url: str, browser, timeout: float = 10.0) -> Optional[str]:
    """Fetch a single URL with a shared Playwright browser and extract publish date."""
    try:
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            rendered_html = await page.content()
            return await _extract_date_from_html(rendered_html[:200_000])
        finally:
            await page.close()
    except Exception:
        logger.debug("Playwright fetch failed for %s", url)
        return None


async def _fetch_dates_for_hits(
    hits: List[SearchHit],
    max_fetch: int = 4,
    use_playwright_fallback: bool = False,
) -> None:
    """Concurrently fetch publish dates for the top hits in-place.
    First tries httpx for all URLs, then uses a single shared Playwright browser
    for URLs where httpx didn't find a date."""

    targets = [h for h in hits[:max_fetch] if not h.published_date]
    if not targets:
        return

    # Phase 1: httpx (fast, concurrent)
    async def _httpx_fetch(hit: SearchHit):
        date = await _fetch_publish_date(hit.url)
        if date:
            hit.published_date = date

    await asyncio.gather(*[_httpx_fetch(h) for h in targets], return_exceptions=True)

    if not use_playwright_fallback:
        return

    # Phase 2: Playwright fallback for hits still without dates (shared browser)
    remaining = [h for h in targets if not h.published_date]
    if not remaining:
        return

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                for hit in remaining:
                    date = await _fetch_date_playwright(hit.url, browser)
                    if date:
                        hit.published_date = date
            finally:
                await browser.close()
    except Exception:
        logger.debug("Playwright batch date fetching failed")


class WebSearchService:
    @property
    def is_available(self) -> bool:
        if not config.web_search_enabled:
            return False
        return any(self._provider_ready(p) for p in self._provider_chain())

    def _provider_chain(self) -> List[str]:
        """Ordered provider chain: primary first, then configured fallbacks (deduped)."""
        chain = [config.web_search_provider]
        for p in config.web_search_fallback_providers:
            if p not in chain:
                chain.append(p)
        return chain

    @staticmethod
    def _provider_ready(provider: str) -> bool:
        """A provider is ready when it needs no key, or its key is configured."""
        if provider == "exa":
            # Exa MCP works keyless (free tier, no API key required); rate
            # limits (429) surface as an error envelope and fall through the
            # chain. The shared api_key (e.g. Tavily's) is never forwarded to
            # mcp.exa.ai — _search_exa only appends its own key slot.
            return True
        if provider in ("tavily", "serper"):
            return bool(config.web_search_api_key)
        if provider == "bocha":
            return bool(config.web_search_bocha_api_key)
        if provider == "firecrawl":
            return bool(config.web_search_firecrawl_api_key)
        return False

    async def search(self, query: str, time_sensitive: bool = False) -> List[SearchHit]:
        if not self.is_available or not query.strip():
            return []

        # Walk the provider chain in order; first non-empty result wins.
        # Each leg is bounded by web_search_timeout_seconds so a hung provider
        # (Tavily/Serper network stall) can't stall the agent loop.
        timeout = config.web_search_timeout_seconds
        for provider in self._provider_chain():
            if not self._provider_ready(provider):
                logger.info("web_search: skipping provider %r (no API key configured)", provider)
                continue
            try:
                hits = await asyncio.wait_for(
                    self._search_via(provider, query, time_sensitive=time_sensitive),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("web_search: provider %r timed out after %.1fs — trying next in chain", provider, timeout)
                hits = None
            except Exception as e:
                logger.warning("web_search: provider %r failed (%s: %s) — trying next in chain", provider, type(e).__name__, e)
                hits = None
            if hits:
                logger.info("web_search: provider %r returned %d hits", provider, len(hits))
                # 溯源元数据：落库时按 hit 记录真实命中的 provider 与其查询词
                for hit in hits:
                    hit.provider = provider
                    hit.query = query
                return hits
            logger.warning("web_search: provider %r returned no usable results — trying next in chain", provider)
        return []

    async def persist_hits(self, hits: List[SearchHit],
                           user_id: Optional[str] = None,
                           conversation_id: Optional[str] = None) -> None:
        """Best-effort persistence of search hits into web_search_results.

        Opens its OWN AsyncSessionLocal — the caller's session (agent loop /
        voice service long-lived session) is never touched, so a failed or
        timed-out write cannot roll back the caller's pending work.
        Bounded by web_search_timeout_seconds and never raises: a hung or
        failed write must not break the search tool result.
        """
        if not hits:
            return

        from app.db.database import AsyncSessionLocal, WebSearchResult

        async def _do() -> None:
            async with AsyncSessionLocal() as session:
                rank_by_query: Dict[str, int] = {}
                for hit in hits:
                    q = hit.query or ""
                    rank_by_query[q] = rank_by_query.get(q, 0) + 1
                    session.add(WebSearchResult(
                        user_id=user_id,
                        conversation_id=conversation_id,
                        query=q,
                        provider=hit.provider or "",
                        result_rank=rank_by_query[q],
                        title=hit.title,
                        url=hit.url,
                        snippet=hit.snippet,
                        published_date=hit.published_date,
                    ))
                await session.commit()

        try:
            # wait_for 取消时 async with 的 __aexit__ 自动关闭会话并回滚
            await asyncio.wait_for(_do(), timeout=config.web_search_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("web_search: persistence of %d hits timed out — skipped", len(hits))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "web_search: failed to persist %d hits to web_search_results",
                len(hits), exc_info=True,
            )

    async def _search_via(
        self, provider: str, query: str, time_sensitive: bool = False
    ) -> Optional[List[SearchHit]]:
        """Dispatch one provider; None means 'no usable result, try next'."""
        if provider == "exa":
            try:
                return await asyncio.wait_for(
                    self._search_exa(query, time_sensitive=time_sensitive),
                    timeout=config.web_search_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning("Exa search timed out for query %r", query)
                return None
        if provider == "bocha":
            return await self._search_bocha(query, time_sensitive=time_sensitive)
        if provider == "firecrawl":
            return await self._search_firecrawl(query, time_sensitive=time_sensitive)
        if provider == "tavily":
            return await self._search_tavily(get_shared_async_client(), query)
        if provider == "serper":
            return await self._search_serper(get_shared_async_client(), query)
        logger.warning("web_search: unknown provider %r", provider)
        return None

    @staticmethod
    def _is_blocked_domain(url: str) -> bool:
        """Check if URL belongs to a blocked domain."""
        blocked = config.web_search_blocked_domains
        if not blocked:
            return False
        try:
            from urllib.parse import urlparse
            hostname = urlparse(url).hostname or ""
            hostname = hostname.lower()
            return any(hostname == d or hostname.endswith("." + d) for d in blocked)
        except Exception:
            return False

    async def search_multiple(self, queries: List[str], time_sensitive: bool = False) -> List[SearchHit]:
        """Run multiple search queries and interleave+deduplicate results.

        Pure retrieval: persistence is the tool layer's job (persist_hits),
        called after date enrichment so published_date is complete.
        """
        if not queries:
            return []

        # Parallelize the planner queries: each leg is independently bounded by
        # its own timeout and search() never raises (returns [] on failure), so
        # gather is safe. Sequential execution cost N × query latency per
        # search round (3 queries = 3×, the biggest single serialized wait in
        # the tool loop); parallel costs 1 × slowest query.
        per_query_results = await asyncio.gather(
            *(self.search(q, time_sensitive=time_sensitive) for q in queries)
        )
        per_query_results = [
            [h for h in hits if not self._is_blocked_domain(h.url)]
            for hits in per_query_results
        ]

        # Interleave results round-robin to ensure all queries contribute
        merged: List[SearchHit] = []
        seen_urls: set = set()
        max_len = max((len(r) for r in per_query_results), default=0)
        for i in range(max_len):
            for result_list in per_query_results:
                if i < len(result_list):
                    hit = result_list[i]
                    if hit.url not in seen_urls:
                        seen_urls.add(hit.url)
                        merged.append(hit)
                        if len(merged) >= config.web_search_max_results:
                            return merged

        return merged

    async def _search_exa(self, query: str, time_sensitive: bool = False) -> Optional[List[SearchHit]]:
        """Run an Exa MCP search.

        Returns:
            - list[SearchHit] on success (may be empty if Exa returned nothing).
            - None if Exa was rate-limited (HTTP 429); caller may fall back.
        """
        from app.tools.mcp_client import call_mcp_tool

        # Use Exa's own key slot only — never the shared `api_key` (which may
        # be a Tavily/Serper credential and must not be forwarded to Exa).
        exa_api_key = config.web_search_exa_api_key or ""
        exa_url = "https://mcp.exa.ai/mcp?tools=web_search_exa"
        if exa_api_key:
            exa_url += f"&exaApiKey={_json.dumps(exa_api_key).strip(chr(34))}"

        arguments: Dict[str, object] = {
            "query": query,
            "numResults": config.web_search_max_results,
        }
        if time_sensitive:
            # 时间敏感检索窗口：当年 1 月 1 日起（避免硬编码年份过期）
            arguments["startPublishedDate"] = f"{datetime.now().year}-01-01T00:00:00.000Z"

        try:
            raw = await call_mcp_tool(exa_url, "web_search_exa", arguments, api_key=exa_api_key)
        except Exception as e:
            logger.warning("Exa MCP search failed — signalling fallback: %r", e)
            return None

        if not raw:
            logger.warning("Exa MCP returned empty response — signalling fallback")
            return None

        # call_mcp_tool returns a JSON string on error (429, network exception,
        # parse failure, no-valid-response). Treat ANY error envelope as a
        # signal to fall back to the next provider in the chain so a flaky Exa
        # endpoint doesn't leave the agent without search results.
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            try:
                err_obj = _json.loads(stripped)
                if isinstance(err_obj, dict) and "error" in err_obj:
                    logger.warning(
                        "Exa MCP error %r — falling back to next provider in chain",
                        err_obj.get("error"),
                    )
                    return None
            except Exception:
                pass

        hits = self._parse_exa_results(raw)
        if not hits:
            # Exa accepted the request but returned nothing parseable — fall
            # back rather than yielding an empty list to the agent.
            logger.warning("Exa MCP returned no parseable hits — falling back to next provider in chain")
            return None
        return hits

    @staticmethod
    def _parse_exa_results(raw_text: str) -> List[SearchHit]:
        hits: List[SearchHit] = []
        blocks = raw_text.split("---")
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            title = ""
            url = ""
            published = ""
            snippet_parts: List[str] = []

            for line in block.split("\n"):
                line = line.strip()
                if line.startswith("Title:"):
                    title = line[6:].strip()
                elif line.startswith("URL:"):
                    url = line[4:].strip()
                elif line.startswith("Published:"):
                    pub_raw = line[10:].strip()
                    if pub_raw and pub_raw != "N/A":
                        published = pub_raw[:10]
                elif line.startswith("Author:") or line.startswith("Highlights:"):
                    continue
                elif line.startswith("[...]"):
                    continue
                elif line:
                    if len(snippet_parts) < 3 and len(line) > 20:
                        snippet_parts.append(line)

            if url and title:
                snippet = " ".join(snippet_parts)[:500] if snippet_parts else title
                hits.append(
                    SearchHit(
                        title=title[:300],
                        url=url,
                        snippet=snippet,
                        published_date=published or None,
                    )
                )

        return hits

    async def _search_bocha(self, query: str, time_sensitive: bool = False) -> Optional[List[SearchHit]]:
        """Bocha (博查) Web Search API — China-native, LLM-oriented search.

        Endpoint: POST https://api.bochaai.com/v1/web-search (Bearer auth).
        Returns None on any error/empty so the caller can fall through the chain.
        """
        api_key = config.web_search_bocha_api_key or ""
        if not api_key:
            logger.warning("Bocha search skipped — bocha_api_key not configured")
            return None

        freshness = "oneMonth" if time_sensitive else "noLimit"
        payload = {
            "query": query,
            "count": config.web_search_max_results,
            "freshness": freshness,
            "summary": False,
        }
        client = get_shared_async_client()
        try:
            resp = await client.post(
                "https://api.bochaai.com/v1/web-search",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=config.web_search_timeout_seconds,
            )
            if resp.status_code != 200:
                logger.warning("Bocha search failed: %d %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
        except Exception as e:
            logger.warning("Bocha search request failed: %r", e)
            return None

        hits = self._parse_bocha_results(data)
        if not hits:
            logger.warning("Bocha search returned no parseable hits")
            return None
        return hits

    @staticmethod
    def _parse_bocha_results(data: dict) -> List[SearchHit]:
        hits: List[SearchHit] = []
        # Response shape: {"code": 200, "data": {"webPages": {"value": [...]}}}
        web_pages = (data.get("data") or {}).get("webPages") or {}
        for item in web_pages.get("value") or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            title = item.get("name") or item.get("title") or ""
            if not url or not title:
                continue
            snippet = item.get("snippet") or item.get("summary") or ""
            published = ""
            date_raw = item.get("datePublished") or ""
            if date_raw:
                published = date_raw[:10]
            hits.append(
                SearchHit(
                    title=title[:300],
                    url=url,
                    snippet=snippet[:500],
                    published_date=published or None,
                )
            )
        return hits

    async def _search_firecrawl(self, query: str, time_sensitive: bool = False) -> Optional[List[SearchHit]]:
        """Firecrawl Search API (v2) — US-hosted, reliable fallback with scraping.

        Endpoint: POST https://api.firecrawl.dev/v2/search (Bearer auth).
        Returns None on any error/empty so the caller can fall through the chain.
        """
        api_key = config.web_search_firecrawl_api_key or ""
        if not api_key:
            logger.warning("Firecrawl search skipped — firecrawl_api_key not configured")
            return None

        payload: Dict[str, object] = {
            "query": query,
            "limit": config.web_search_max_results,
            "sources": [{"type": "web"}],
        }
        if time_sensitive:
            payload["tbs"] = "qdr:m"
        client = get_shared_async_client()
        try:
            resp = await client.post(
                "https://api.firecrawl.dev/v2/search",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=config.web_search_timeout_seconds,
            )
            if resp.status_code != 200:
                logger.warning("Firecrawl search failed: %d %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
        except Exception as e:
            logger.warning("Firecrawl search request failed: %r", e)
            return None

        hits = self._parse_firecrawl_results(data)
        if not hits:
            logger.warning("Firecrawl search returned no parseable hits")
            return None
        return hits

    @staticmethod
    def _parse_firecrawl_results(data: dict) -> List[SearchHit]:
        hits: List[SearchHit] = []
        # Response shape (v2): {"success": true, "data": {"web": [{"title","description","url"}]}}
        if not data.get("success"):
            return hits
        web_items = (data.get("data") or {}).get("web") or []
        for item in web_items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            title = item.get("title") or ""
            if not url or not title:
                continue
            snippet = item.get("description") or item.get("snippet") or ""
            published = ""
            date_raw = item.get("date") or ""
            if date_raw:
                published = date_raw[:10]
            hits.append(
                SearchHit(
                    title=title[:300],
                    url=url,
                    snippet=snippet[:500],
                    published_date=published or None,
                )
            )
        return hits

    async def _search_tavily(self, client: httpx.AsyncClient, query: str) -> List[SearchHit]:
        response = await client.post(
            config.web_search_api_url or "https://api.tavily.com/search",
            json={
                "api_key": config.web_search_api_key,
                "query": query,
                "max_results": config.web_search_max_results,
                "search_depth": "basic",
                "include_answer": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        return [
            SearchHit(
                title=item.get("title", "Untitled result"),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
            )
            for item in results[: config.web_search_max_results]
        ]

    async def _search_serper(self, client: httpx.AsyncClient, query: str) -> List[SearchHit]:
        response = await client.post(
            config.web_search_api_url or "https://google.serper.dev/search",
            headers={"X-API-KEY": config.web_search_api_key or "", "Content-Type": "application/json"},
            json={"q": query, "num": config.web_search_max_results},
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("organic", [])
        return [
            SearchHit(
                title=item.get("title", "Untitled result"),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
            )
            for item in results[: config.web_search_max_results]
        ]

    @staticmethod
    def _is_cn_or_en(text: str) -> bool:
        """Check if text is primarily Chinese or English."""
        if not text or not text.strip():
            return False
        cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        en_chars = sum(1 for c in text if c.isascii() and c.isalpha())
        total = len(text.strip())
        if total == 0:
            return False
        return (cn_chars + en_chars) / total > 0.3

    @staticmethod
    def evaluate_round_quality(hits: List[SearchHit]) -> tuple:
        """Evaluate if a search round is qualified.

        Returns (qualified, cn_en_count, total_count).
        A round is qualified if CN/EN results are the majority.
        """
        if not hits:
            return False, 0, 0
        cn_en_count = sum(
            1 for h in hits
            if WebSearchService._is_cn_or_en(h.title + " " + h.snippet)
        )
        qualified = cn_en_count > len(hits) / 2
        return qualified, cn_en_count, len(hits)

    def format_hits(self, hits: List[SearchHit]) -> str:
        if not hits:
            return "未检索到可用网页结果。"

        lines = []
        for index, hit in enumerate(hits, start=1):
            entry = (
                f"{index}. {hit.title}\n"
                f"URL: {hit.url}\n"
            )
            if hit.published_date:
                entry += f"发布日期: {hit.published_date}\n"
            entry += f"摘要: {hit.snippet.strip()}"
            lines.append(entry)
        return "\n\n".join(lines)

    @staticmethod
    def filter_old_results(hits: List[SearchHit], min_year: int) -> List[SearchHit]:
        """Filter out results whose URL contains a year older than min_year.
        Results with no detectable year are kept."""
        filtered = []
        for h in hits:
            url_year = _extract_year_from_url(h.url)
            if url_year is not None and url_year < min_year:
                logger.debug("Filtering old result (%d): %s", url_year, h.url)
                continue
            filtered.append(h)
        return filtered