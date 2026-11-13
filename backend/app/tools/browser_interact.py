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


async def _browser_session(kwargs: dict, service, conv_id: str):
    """§10 浏览器：enforce 未放开该模式 → 明确拒绝；放开则会话键含
    task/run/attempt/epoch/binding，改绑后不会复用旧 Task 的页面状态。"""
    from app.services.workspace_scope import WorkspaceScopeError as _BErr
    from app.services.workspace_scoping_runtime import entry_guard as _entry_guard

    ctx = kwargs.get("workspace_ctx")
    # 门只约束 scoped 执行（legacy 会话 ctx=None 保持旧行为，acceptance #31）
    if ctx is not None:
        try:
            _entry_guard("browser")
        except _BErr as exc:
            return None, json.dumps({"error": str(exc), "code": exc.code, "success": False},
                                    ensure_ascii=False)
    key = conv_id
    if ctx is not None:
        key = f"{conv_id}::{ctx.cache_key}"
    session = await service.get_or_create_session(key)
    return session, None


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
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
    if not session:
        return json.dumps({"error": "Failed to create browser session. Is Playwright installed?"}, ensure_ascii=False)

    from app.services.interactive_browser_service import navigate
    try:
        _timeout_ms = int(args.get("timeout_ms") or 0)
    except (TypeError, ValueError):
        return json.dumps({"error": "timeout_ms must be an integer"}, ensure_ascii=False)
    result = await navigate(
        session, url,
        wait_selector=str(args.get("wait_selector") or ""),
        timeout_ms=_timeout_ms,
        wait_js=str(args.get("wait_js") or ""),
    )
    return json.dumps(result, ensure_ascii=False)


async def browser_snapshot(args: dict, **kwargs) -> str:
    full = args.get("full", False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
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
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
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
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    # P0-3（上轮评估）：canvas 游戏/隐藏 IME 输入无 ref 可寻址——ref="_focused"
    # 走 CDP trusted 键盘逐字键入当前聚焦元素（浏览器点击聚焦后使用）。
    if ref == "_focused":
        from app.services.interactive_browser_service import type_focused
        submit = bool(args.get("submit", False))
        result = await type_focused(session, text, submit=submit)
        return json.dumps(result, ensure_ascii=False)

    from app.services.interactive_browser_service import type_text
    result = await type_text(session, ref, text)
    return json.dumps(result, ensure_ascii=False)


async def browser_scroll(args: dict, **kwargs) -> str:
    direction = args.get("direction", "down")
    if direction not in ("up", "down"):
        return json.dumps({"error": "Direction must be 'up' or 'down'"}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
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
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import press_key
    result = await press_key(session, key)
    return json.dumps(result, ensure_ascii=False)


async def browser_back(args: dict, **kwargs) -> str:
    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
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
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
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
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import execute_js
    result = await execute_js(session, script)
    return json.dumps(result, ensure_ascii=False)


async def browser_screenshot(args: dict, **kwargs) -> str:
    full_page = args.get("full_page", False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import screenshot
    result = await screenshot(session, full_page=full_page, name_prefix=conv_id)
    if result.get("success") and result.get("path"):
        # P0-2（上轮评估）：截图以图像信封回传——对话模型直接「看见」页面。
        # I7（A4.9）：构造期即尊重 inline_tool_images 开关（对齐 workspace_read）
        # ——关闭时只回路径 JSON，MB 级 base64 不入 tool_results 落库。
        try:
            from app.services.tool_image_results import (
                build_image_envelope_from_file,
                inline_images_enabled,
            )
            if inline_images_enabled():
                _env = await build_image_envelope_from_file(
                    result["path"],
                    f"浏览器截图已保存：{result.get('filename')}（url={result.get('url')}）",
                    display_path=str(result.get("filename") or ""),
                    # P1b：blob 按用户命名空间落盘；无 user 上下文时写侧退回内联。
                    user_id=str(getattr(kwargs.get("user"), "id", "") or "") or None,
                )
                if _env:
                    return _env
        except Exception:
            logger.debug("screenshot image envelope failed, falling back to path JSON")
    return json.dumps(result, ensure_ascii=False)


_NAVIGATE_SCHEMA = {
    "name": "browser_navigate",
    "description": (
        "Navigate to a URL in an interactive browser session. Returns a page snapshot with interactive elements "
        "labeled as [@__br_1], [@__br_2], etc. which can be used with browser_click and browser_type. "
        "Use this when you need to interact with a web page (click buttons, fill forms, navigate menus). "
        "For simple content reading, use the 'browser' tool instead (faster, no session overhead). "
        "READINESS WARNING: a selector appearing is NOT the same as the app being ready. Canvas/SPA apps "
        "(games, editors, node-graph tools, dashboards) often render their shell/loading canvas long before "
        "the real document finishes loading — nearly all canvas apps need several seconds of warm-up, and "
        "a boot/loading document can already satisfy a weak predicate. For such apps pass wait_js with a "
        "condition that identifies the TARGET document (a specific element, text, route/hash, or app-ready "
        "flag), never merely \"a canvas exists\" or \"the app object exists\". Verify the loaded document "
        "identity before acting, and never assume the boot/default document is the intended one or reuse "
        "its ids. If the target document is not loaded yet, inspect the snapshot/screenshot or load the "
        "intended document/template explicitly. "
        "RESULT FIELDS: large-canvas pages are observed automatically (even without wait_selector) and "
        "return settled (stable within the window), observed_change (whether any transition was seen), "
        "settle_mode (canvas_heuristic|heuristic|wait_js), settle_ms, canvas, and warnings. "
        "settled=true means only that the page was stable — NOT that the target document is ready; "
        "settled=true + observed_change=false on a canvas page may still be a static loading screen. "
        "settled=false means the page kept changing/observation failed — do not act on it as ready. "
        "warnings are diagnostics, not an automatic hard stop. "
        "Strategy note: for canvas/WebGL games or 「clear/complete this game」 style long-horizon tasks, "
        "prefer offline reverse-engineering of game assets/logic first (download + parse) — real-time "
        "gameplay loops are expensive and unreliable; use browser_mouse + screenshots only for targeted "
        "steps (e.g., entering a password)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to (e.g., 'https://example.com')",
            },
            "wait_selector": {
                "type": "string",
                "description": "Optional CSS selector to wait for before snapshot (e.g., 'canvas' for WASM games — DOM-ready is not UI-ready)",
            },
            "wait_js": {
                "type": "string",
                "description": (
                    "Optional JavaScript predicate (expression or arrow function) that must become truthy "
                    "before the tool returns. Use for canvas/SPA apps whose DOM shell is ready before their "
                    "app state/document: make it identify the TARGET document (a specific element, text, "
                    "route/hash, or app-ready flag), not merely \"canvas exists\" or \"app object exists\" "
                    "— the boot/loading document already satisfies those. Example shapes: "
                    "\"() => !!document.querySelector('[data-app-ready=true]')\", "
                    "\"() => location.hash === '#/editor' && !!document.querySelector('.toolbar')\", "
                    "\"() => window.__APP_READY__ === true\". If it never becomes truthy (capped at 15 s), "
                    "the call returns success=false with the current URL/title/snapshot; inspect or load "
                    "the intended document explicitly, do not treat the boot state as ready."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Navigation timeout in ms (default 20000, max 120000)",
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
        "Type text into an input field identified by its ref ID from the snapshot "
        "(for DOM inputs: clears the field first, then types). "
        "After typing, you typically need browser_press(key='Enter') to submit the form. "
        "Use ref=\"_focused\" to type into the CURRENTLY FOCUSED element with real key "
        "events — works on canvas games / hidden IME inputs after browser_mouse click "
        "focuses them (appends at the caret, does NOT clear; set submit=true to press "
        "Enter after typing). "
        "Requires browser_navigate to have been called first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The input element reference from the snapshot (e.g., '__br_3') or '_focused' for the focused element",
            },
            "text": {
                "type": "string",
                "description": "The text to type into the field",
            },
            "submit": {
                "type": "boolean",
                "description": "With ref='_focused': press Enter after typing (default false)",
                "default": False,
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
        "Vision-capable models receive the image inline (when [agent] inline_tool_images is on); "
        "the result also includes the saved path for vision_interpret. "
        "For canvas/WebGL pages whose snapshot is empty, take a screenshot first, then use "
        "browser_mouse with viewport CSS pixel coordinates (move/down/up/click/wheel). "
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


async def browser_video(args: dict, **kwargs) -> str:
    """P3/B5：会话录像证据（Playwright 原生）。action=save 把活体 webm
    拷贝到 output_files；status 报告录像状态。页内音频未采集（诚实降级
    标记 UNVERIFIED-audio）。

    2026-11-03：录像随 agent 回合结束销毁（record_video_dir 只在
    context.close 停止编码；回合结束会关闭会话以释放 Chromium/ffmpeg）。
    需要证据必须在同一回合内调用 action=save，跨回合无法补存。"""
    action = str(args.get("action") or "save").strip().lower()
    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import save_video, video_status
    if action == "status":
        result = await video_status(session)
    else:
        result = await save_video(session, note=str(args.get("note") or ""))
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="browser_video",
    toolset="web",
    schema={
        "name": "browser_video",
        "description": (
            "Save or inspect the interactive browser session's video recording as evidence. "
            "action='save' copies the live Playwright webm to output_files and returns its path; "
            "action='status' reports recording state. Recording starts automatically with the session "
            "and is destroyed when the agent turn ends — call action='save' in the SAME turn; it cannot "
            "be saved in a later turn. "
            "In-page audio is NOT captured (returns an UNVERIFIED-audio mark) — use the APPENDIX §A1 "
            "MediaRecorder template via browser_execute_js when audio evidence is required."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["save", "status"], "description": "save (default) or status"},
                "note": {"type": "string", "description": "Optional evidence note stored with the save result."},
            },
        },
    },
    handler=browser_video,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Save/inspect browser session video evidence",
    emoji="",
)


_MOUSE_SCHEMA = {
    "name": "browser_mouse",
    "description": (
        "Coordinate-based mouse actions in the interactive browser (real CDP mouse events). "
        "Works on ANY web page, not only canvas apps: use it whenever a control has no usable "
        "DOM ref or the task needs a real drag gesture — slider CAPTCHAs (press down on the "
        "handle, move along the track in small steps, release up), custom-drawn widgets, maps, "
        "drawing tools, drag-and-drop targets. Identify coordinates from a browser_screenshot "
        "first. action: move (hover), down/up (drag halves), click (x,y), wheel (scroll at "
        "position). Coordinates are viewport CSS pixels from the screenshot's top-left."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["move", "down", "up", "click", "wheel"],
                "description": "Mouse action",
            },
            "x": {"type": "number", "description": "Viewport X in CSS pixels (move/click)"},
            "y": {"type": "number", "description": "Viewport Y in CSS pixels (move/click)"},
            "button": {
                "type": "string",
                "enum": ["left", "right", "middle"],
                "description": "Button for down/up/click (default left)",
            },
            "clicks": {"type": "integer", "description": "Click count for click (2=double)", "default": 1},
            "delta_y": {"type": "number", "description": "Vertical scroll amount for wheel (positive=down)"},
        },
        "required": ["action"],
    },
}


async def browser_mouse(args: dict, **kwargs) -> str:
    """P0-1（上轮评估）：坐标鼠标原语——canvas/WebGL/自绘控件的唯一寻址方式。"""
    action = str(args.get("action") or "").strip().lower()
    if not action:
        return json.dumps({"error": "No mouse action provided"}, ensure_ascii=False)

    service = InteractiveBrowserService.get_instance()
    conv_id = _get_conversation_id(kwargs)
    session, _scope_err = await _browser_session(kwargs, service, conv_id)
    if _scope_err:
        return _scope_err
    if not session:
        return json.dumps({"error": "No active browser session. Use browser_navigate first."}, ensure_ascii=False)

    from app.services.interactive_browser_service import mouse
    try:
        _x = float(args.get("x", 0) or 0)
        _y = float(args.get("y", 0) or 0)
        _clicks = int(args.get("clicks", 1) or 1)
        _dy = float(args.get("delta_y", 0) or 0)
    except (TypeError, ValueError):
        return json.dumps({"error": "x/y/clicks/delta_y must be numeric"}, ensure_ascii=False)
    result = await mouse(
        session,
        action,
        x=_x,
        y=_y,
        button=str(args.get("button") or "left"),
        clicks=_clicks,
        delta_y=_dy,
    )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="browser_mouse",
    toolset="web",
    schema=_MOUSE_SCHEMA,
    handler=browser_mouse,
    check_fn=check_browser_interaction_requirements,
    is_async=True,
    description="Coordinate mouse actions (canvas/WebGL/custom widgets)",
    emoji="",
)
