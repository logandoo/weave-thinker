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
            part = f"{idx}. {page.url}{title_line}\n{page.text}"
            # 图片直址清单（图片能力缺口修复）：正文之后列出，agent 可据此
            # 内嵌 ![alt](src) 或下载后 provide_file。
            if page.images:
                lines = [f"页面图片 ({len(page.images)}):"]
                for i, img in enumerate(page.images, 1):
                    alt = f" — {img['alt']}" if img.get("alt") else ""
                    lines.append(f"  {i}. {img['src']}{alt}")
                part += "\n\n" + "\n".join(lines)
            content_parts.append(part)

    return json.dumps({
        "url_count": len(pages),
        "pages": [{"url": p.url, "title": p.title, "text": p.text, "error": p.error,
                   "images": p.images} for p in pages],
        "formatted": "\n\n".join(content_parts),
    }, ensure_ascii=False)


registry.register(
    name="browser",
    toolset="web",
    schema={
        "name": "browser",
        "description": (
            "Browse and extract text content from web pages. Use this to read articles, "
            "documentation, or any web page the user asks about. 结果含每页 images[] 图片直址清单"
            "（og:image 优先）。当用户要求展示/查看图片时：从 images[] 挑选语义匹配的图片，"
            "用其 src 原值以 markdown 图片语法内嵌显示（![描述](src 原值)，src 必须取自 "
            "images[]，禁止自造或输出占位符）；如需更稳妥（防盗链），可用 execute_code "
            "下载到工作区后用 provide_file 发给用户。"
        ),
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
