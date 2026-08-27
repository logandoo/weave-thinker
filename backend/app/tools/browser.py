# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from app.tools.registry import registry
from app.services.browser_service import BrowserService
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


def check_browser_requirements() -> bool:
    return config.browser_enabled


async def browser(args: dict, **kwargs) -> str:
    urls = args.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return json.dumps({"error": "No URLs provided"}, ensure_ascii=False)

    paginated = args.get("paginated", False)
    max_pages = min(int(args.get("max_pages", 1) or 1), config.browser_max_pages)

    browser_service = BrowserService()
    if paginated and urls:
        pages = await browser_service.fetch_paginated(urls[0], max_pages=max_pages)
    else:
        pages = await browser_service.fetch_pages(urls, max_pages=config.browser_max_pages)

    content_parts = []
    for idx, page in enumerate(pages, 1):
        if page.error and not page.text:
            content_parts.append(f"{idx}. [ERROR] {page.url}: {page.error}")
        else:
            title_line = f" - {page.title}" if page.title else ""
            content_parts.append(f"{idx}. {page.url}{title_line}\n{page.text}")

    return json.dumps({
        "url_count": len(pages),
        "pages": [{"url": p.url, "title": p.title, "text": p.text, "error": p.error} for p in pages],
        "formatted": "\n\n".join(content_parts),
    }, ensure_ascii=False)


registry.register(
    name="browser",
    toolset="web",
    schema={
        "name": "browser",
        "description": "Browse and extract text content from web pages. Use this to read articles, documentation, or any web page the user asks about.",
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs to fetch and extract content from.",
                },
                "paginated": {
                    "type": "boolean",
                    "description": "Whether to paginate through multiple pages from the first URL.",
                },
                "max_pages": {
                    "type": "integer",
                    "description": "Maximum number of pages to fetch when paginated.",
                },
            },
            "required": ["urls"],
        },
    },
    handler=browser,
    check_fn=check_browser_requirements,
    is_async=True,
    description="Web page browsing and content extraction",
    emoji="",
)
