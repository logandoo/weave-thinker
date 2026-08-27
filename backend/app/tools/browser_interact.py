# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import ipaddress
import json
import logging
import uuid
from urllib.parse import urlparse

from app.tools.registry import registry
from app.services.interactive_browser_service import InteractiveBrowserService
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_BLOCKED_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.internal",
}


def check_browser_interaction_requirements() -> bool:
    return getattr(config, "browser_interaction_enabled", False)


def _get_conversation_id(kwargs: dict) -> str:
    conv = kwargs.get("conversation")
    if conv and hasattr(conv, "id") and conv.id is not None:
        return str(conv.id)
    user = kwargs.get("user")
    if user and hasattr(user, "id"):
        return f"user_{user.id}_bg_{uuid.uuid4().hex[:8]}"
    return f"anon_{uuid.uuid4().hex[:8]}"


def _is_private_url(url: str) -> str:
    if config.super_admin_bypass:
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "Only http:// and https:// URLs are allowed"
        hostname = parsed.hostname
        if not hostname:
            return "Invalid URL: no hostname"
        if hostname in _BLOCKED_HOSTNAMES:
            return f"Access to {hostname} is blocked"
        if hostname.endswith(".internal") or hostname.endswith(".local"):
            return f"Access to {hostname} is blocked (internal domain)"
        try:
            import socket
            ip_str = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)[0][4][0]
            ip = ipaddress.ip_address(ip_str)
            for net in _PRIVATE_NETWORKS:
                if ip in net:
                    return f"Access to private IP {ip_str} is blocked"
        except (socket.gaierror, OSError):
            pass
        return ""
    except Exception:
        return "Invalid URL"


async def browser_navigate(args: dict, **kwargs) -> str:
    url = args.get("url", "").strip()
    if not url:
        return json.dumps({"error": "No URL provided"}, ensure_ascii=False)

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    ssrf_error = _is_private_url(url)
    if ssrf_error:
        return json.dumps({"error": ssrf_error}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "Failed to create browser session. Is Playwright installed?"}, ensure_ascii=False)

    from app.services.interactive_browser_service import navigate
    result = await navigate(session, url)
    return json.dumps(result, ensure_ascii=False)


async def browser_snapshot(args: dict, **kwargs) -> str:
    full = args.get("full", False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import snapshot
    result = await snapshot(session, full=full)
    return json.dumps(result, ensure_ascii=False)


async def browser_click(args: dict, **kwargs) -> str:
    ref = args.get("ref", "").strip()
    if not ref:
        return json.dumps({"error": "No element ref provided"}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import click
    result = await click(session, ref)
    return json.dumps(result, ensure_ascii=False)


async def browser_type(args: dict, **kwargs) -> str:
    ref = args.get("ref", "").strip()
    text = args.get("text", "")
    if not ref:
        return json.dumps({"error": "No element ref provided"}, ensure_ascii=False)
    if not text:
        return json.dumps({"error": "No text provided"}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import type_text
    result = await type_text(session, ref, text)
    return json.dumps(result, ensure_ascii=False)


async def browser_scroll(args: dict, **kwargs) -> str:
    direction = args.get("direction", "down")
    if direction not in ("up", "down"):
        return json.dumps({"error": "Direction must be 'up' or 'down'"}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import scroll
    result = await scroll(session, direction)
    return json.dumps(result, ensure_ascii=False)


async def browser_press(args: dict, **kwargs) -> str:
    key = args.get("key", "").strip()
    if not key:
        return json.dumps({"error": "No key provided"}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import press_key
    result = await press_key(session, key)
    return json.dumps(result, ensure_ascii=False)


async def browser_back(args: dict, **kwargs) -> str:
    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import go_back
    result = await go_back(session)
    return json.dumps(result, ensure_ascii=False)


async def browser_extract(args: dict, **kwargs) -> str:
    selector = args.get("selector", "").strip()
    if not selector:
        return json.dumps({"error": "No CSS selector provided"}, ensure_ascii=False)
    attributes = args.get("attributes", None)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import extract
    result = await extract(session, selector, attributes)
    return json.dumps(result, ensure_ascii=False)


async def browser_execute_js(args: dict, **kwargs) -> str:
    script = args.get("script", "").strip()
    if not script:
        return json.dumps({"error": "No JavaScript code provided"}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import execute_js
    result = await execute_js(session, script)
    return json.dumps(result, ensure_ascii=False)


async def browser_screenshot(args: dict, **kwargs) -> str:
    full_page = args.get("full_page", False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session = await service.get_or_create_session(conv_id)
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import screenshot
    result = await screenshot(session, full_page=full_page)
    return json.dumps(result, ensure_ascii=False)


_NAVIGATE_SCHEMA = {
    "name": "browser_navigate",
    "description": (
        "Navigate to a URL in an interactive browser session. Returns a page snapshot with interactive elements "
        "labeled as [@__br_1], [@__br_2], etc. which can be used with browser_click and browser_type. "
        "Use this when you need to interact with a web page (click buttons, fill forms, navigate menus). "
        "For simple content reading, use the 'browser' tool instead (faster, no session overhead)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to (e.g., 'https://example.com')",
            },
        },
        "required": ["url"],
    },
}

_SNAPSHOT_SCHEMA = {
    "name": "browser_snapshot",
    "description": (
        "Get a text snapshot of the current interactive browser page. Returns interactive elements with ref IDs "
        "like [@__br_1], [@__br_2] for use with browser_click and browser_type. Use full=true for complete page content, "
        "or full=false (default) for a compact view. Requires browser_navigate to have been called first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "full": {
                "type": "boolean",
                "description": "If true, returns complete page content. If false (default), returns compact view.",
                "default": False,
            },
        },
        "required": [],
    },
}

_CLICK_SCHEMA = {
    "name": "browser_click",
    "description": (
        "Click an element on the current browser page identified by its ref ID from the snapshot "
        "(e.g., __br_5, __br_12). After clicking, returns a new page snapshot showing the updated page state. "
        "Requires browser_navigate to have been called first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The element reference from the snapshot (e.g., '__br_5', '__br_12')",
            },
        },
        "required": ["ref"],
    },
}

_TYPE_SCHEMA = {
    "name": "browser_type",
    "description": (
        "Type text into an input field identified by its ref ID from the snapshot. "
        "Clears the field first, then types the new text. "
        "After typing, you typically need browser_press(key='Enter') to submit the form. "
        "Requires browser_navigate to have been called first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The input element reference from the snapshot (e.g., '__br_3')",
            },
            "text": {
                "type": "string",
                "description": "The text to type into the field",
            },
        },
        "required": ["ref", "text"],
    },
}

_SCROLL_SCHEMA = {
    "name": "browser_scroll",
    "description": (
        "Scroll the current browser page up or down to reveal more content. "
        "Returns a new snapshot after scrolling. Requires browser_navigate first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Direction to scroll",
            },
        },
        "required": ["direction"],
    },
}

_PRESS_SCHEMA = {
    "name": "browser_press",
    "description": (
        "Press a keyboard key in the browser. Useful for submitting forms (Enter), "
        "navigating (Tab, ArrowDown), or keyboard shortcuts. "
        "Returns a new snapshot after the key press. Requires browser_navigate first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown', 'Control+a')",
            },
        },
        "required": ["key"],
    },
}

_BACK_SCHEMA = {
    "name": "browser_back",
    "description": (
        "Navigate back to the previous page in browser history. "
        "Returns a new snapshot of the previous page. Requires browser_navigate first."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

_EXTRACT_SCHEMA = {
    "name": "browser_extract",
    "description": (
        "Extract data from the current browser page using a CSS selector. "
        "Returns a list of matching elements with their requested attributes. "
        "Use this for precise data extraction (e.g., all links, prices, article titles). "
        "Requires browser_navigate to have been called first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector to match elements (e.g., 'a[href]', '.price', 'h2.title', 'table tr td')",
            },
            "attributes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of attributes to extract from each matched element. "
                    "Defaults to ['textContent'] if not provided. "
                    "Supported special attributes: 'textContent', 'innerText', 'innerHTML', 'href', 'src'. "
                    "Any other string will be treated as an HTML attribute name (e.g., 'class', 'data-id')."
                ),
            },
        },
        "required": ["selector"],
    },
}

_EXECUTE_JS_SCHEMA = {
    "name": "browser_execute_js",
    "description": (
        "Execute arbitrary JavaScript code in the current browser page context. "
        "The script should be a valid JavaScript expression or function body that returns a value. "
        "Use this for custom DOM manipulation, data extraction, or page interaction not covered by other tools. "
        "Requires browser_navigate to have been called first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "JavaScript code to execute. Must return a JSON-serializable value. "
                    "Examples: 'document.title', 'Array.from(document.querySelectorAll(\"a\")).map(a=>a.href)', "
                    "'window.scrollTo(0, document.body.scrollHeight)'"
                ),
            },
        },
        "required": ["script"],
    },
}

_SCREENSHOT_SCHEMA = {
    "name": "browser_screenshot",
    "description": (
        "Take a screenshot of the current browser page and save it as a PNG file. "
        "Returns the file path of the saved screenshot. "
        "Requires browser_navigate to have been called first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean",
                "description": "If true, capture the entire scrollable page. If false (default), capture only the visible viewport.",
                "default": False,
            },
        },
        "required": [],
    },
}

registry.register(
    name="browser_navigate",
    toolset="web",
    schema=_NAVIGATE_SCHEMA,
    handler=browser_navigate,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Navigate to URL in interactive browser",
    emoji="",
)

registry.register(
    name="browser_snapshot",
    toolset="web",
    schema=_SNAPSHOT_SCHEMA,
    handler=browser_snapshot,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Get interactive browser page snapshot",
    emoji="",
)

registry.register(
    name="browser_click",
    toolset="web",
    schema=_CLICK_SCHEMA,
    handler=browser_click,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Click element in interactive browser",
    emoji="",
)

registry.register(
    name="browser_type",
    toolset="web",
    schema=_TYPE_SCHEMA,
    handler=browser_type,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Type text in interactive browser input field",
    emoji="",
)

registry.register(
    name="browser_scroll",
    toolset="web",
    schema=_SCROLL_SCHEMA,
    handler=browser_scroll,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Scroll interactive browser page",
    emoji="",
)

registry.register(
    name="browser_press",
    toolset="web",
    schema=_PRESS_SCHEMA,
    handler=browser_press,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Press keyboard key in interactive browser",
    emoji="",
)

registry.register(
    name="browser_back",
    toolset="web",
    schema=_BACK_SCHEMA,
    handler=browser_back,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Navigate back in interactive browser",
    emoji="",
)

registry.register(
    name="browser_extract",
    toolset="web",
    schema=_EXTRACT_SCHEMA,
    handler=browser_extract,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Extract data from browser page using CSS selector",
    emoji="",
)

registry.register(
    name="browser_execute_js",
    toolset="web",
    schema=_EXECUTE_JS_SCHEMA,
    handler=browser_execute_js,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Execute JavaScript in interactive browser",
    emoji="",
)

registry.register(
    name="browser_screenshot",
    toolset="web",
    schema=_SCREENSHOT_SCHEMA,
    handler=browser_screenshot,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Take screenshot of interactive browser page",
    emoji="",
)
