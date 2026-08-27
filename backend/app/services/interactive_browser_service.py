# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_SESSION_TIMEOUT_SECONDS = config.browser_interaction_session_timeout
_MAX_CONCURRENT_SESSIONS = config.browser_interaction_max_concurrent
_SNAPSHOT_MAX_LENGTH = config.browser_max_content_length
_REF_PATTERN = re.compile(r'^__br_\d+$')

_EXTRACT_DOM_JS = """
(() => {
  const INTERACTIVE_TAGS = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY', 'DETAILS']);
  const HEADING_TAGS = new Set(['H1', 'H2', 'H3', 'H4', 'H5', 'H6']);
  const TEXT_TAGS = new Set(['P', 'SPAN', 'LI', 'TD', 'TH', 'LABEL', 'STRONG', 'EM', 'B', 'I', 'SMALL', 'MARK', 'DEL', 'INS', 'SUB', 'SUP']);
  const STRUCTURE_TAGS = new Set(['NAV', 'MAIN', 'HEADER', 'FOOTER', 'ASIDE', 'SECTION', 'ARTICLE', 'FORM', 'DIALOG', 'FIELDSET']);
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG', 'PATH', 'META', 'LINK', 'HEAD', 'TEMPLATE', 'BR', 'HR']);

  let refCounter = 0;
  const refMap = {};
  const lines = [];

  function getRef(el) {
    const id = '__br_' + (++refCounter);
    el.setAttribute('data-browser-ref', id);
    const tagName = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || '';
    const ariaLabel = el.getAttribute('aria-label') || '';
    const name = el.getAttribute('name') || '';
    const placeholder = el.getAttribute('placeholder') || '';
    const href = el.getAttribute('href') || '';
    const type = el.getAttribute('type') || '';
    const value = el.value || el.getAttribute('value') || '';
    const alt = el.getAttribute('alt') || '';
    const title = el.getAttribute('title') || '';
    const checked = el.checked;
    const selected = (el.tagName === 'SELECT') ? (el.options[el.selectedIndex] || {}).text : '';
    const isDisabled = el.disabled;

    refMap[id] = {
      tag: tagName,
      role: role,
      ariaLabel: ariaLabel,
      name: name,
      placeholder: placeholder,
      href: href,
      type: type,
      value: typeof value === 'string' ? value.substring(0, 200) : '',
      alt: alt,
      title: title,
      checked: checked,
      selected: selected,
      disabled: isDisabled,
    };
    return id;
  }

  function visibleText(el) {
    const s = (el.innerText || el.textContent || '').trim();
    return s.substring(0, 200);
  }

  function walk(el, depth) {
    if (SKIP_TAGS.has(el.tagName)) return;
    if (depth > 20) return;
    if (refCounter > 500) return;

    if (el.offsetWidth === 0 && el.offsetHeight === 0) return;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;

    const indent = '  '.repeat(depth);
    const tag = el.tagName;

    if (INTERACTIVE_TAGS.has(tag) || el.getAttribute('role') === 'button' || el.getAttribute('role') === 'link') {
      const ref = getRef(el);
      const text = visibleText(el);
      let desc = tag.toLowerCase();
      const info = refMap[ref];
      if (info.type && tag === 'INPUT') desc += `[type="${info.type}"]`;
      if (info.ariaLabel) desc += ` "${info.ariaLabel}"`;
      else if (info.placeholder) desc += ` placeholder="${info.placeholder}"`;
      else if (info.name) desc += ` name="${info.name}"`;
      else if (text) desc += ` "${text.substring(0, 60)}"`;
      if (info.href && tag === 'A') desc += ` href="${info.href.substring(0, 80)}"`;
      if (info.value && tag === 'INPUT' && ['text','search','url','email','tel','password'].includes(info.type)) desc += ` value="${info.value}"`;
      if (info.checked) desc += ' [checked]';
      if (info.selected) desc += ` selected="${info.selected}"`;
      if (info.disabled) desc += ' [disabled]';
      lines.push(`${indent}[@${ref}] ${desc}`);
      return;
    }

    if (tag === 'IMG') {
      const ref = getRef(el);
      const alt = el.getAttribute('alt') || '';
      let rawSrc = el.getAttribute('src') || '';
      if (rawSrc) {
        try {
          rawSrc = new URL(rawSrc, window.location.href).href;
        } catch {}
      }
      const src = rawSrc.substring(0, 2000);
      lines.push(`${indent}[@${ref}] img alt="${alt}" src="${src}"`);
      return;
    }

    if (HEADING_TAGS.has(tag)) {
      const level = tag[1];
      const text = visibleText(el);
      if (text) lines.push(`${indent}[h${level}] ${text}`);
      return;
    }

    if (STRUCTURE_TAGS.has(tag)) {
      const label = el.getAttribute('aria-label') || el.getAttribute('role') || '';
      const sLabel = label ? ` "${label}"` : '';
      lines.push(`${indent}${tag.toLowerCase()}${sLabel}:`);
      for (const child of el.children) walk(child, depth + 1);
      return;
    }

    if (tag === 'SELECT') {
      const ref = getRef(el);
      const text = visibleText(el);
      let desc = 'select';
      const info = refMap[ref];
      if (info.ariaLabel) desc += ` "${info.ariaLabel}"`;
      else if (info.name) desc += ` name="${info.name}"`;
      if (info.selected) desc += ` current="${info.selected}"`;
      lines.push(`${indent}[@${ref}] ${desc}`);
      return;
    }

    if (TEXT_TAGS.has(tag) || tag === 'DIV' || tag === 'PRE' || tag === 'BLOCKQUOTE' || tag === 'FIGCAPTION') {
      const text = visibleText(el);
      const hasInteractiveChild = el.querySelector('a, button, input, select, textarea, [role="button"], [role="link"]');
      if (!text) {
        // No visible text: prune leaf containers, but STILL recurse when the
        // subtree holds interactive controls (e.g. a .password-wrapper div
        // containing only an <input> and an SVG eye button).
        if (!hasInteractiveChild) return;
        for (const child of el.children) walk(child, depth);
        return;
      }
      if (el.children.length === 0 && !hasInteractiveChild) {
        lines.push(`${indent}${text}`);
        return;
      }
      if (tag === 'P' || tag === 'PRE' || tag === 'BLOCKQUOTE' || tag === 'LI') {
        if (!hasInteractiveChild) {
          lines.push(`${indent}${text}`);
          return;
        }
        for (const child of el.children) walk(child, depth);
        return;
      }
    }

    for (const child of el.children) walk(child, depth);
  }

  walk(document.body, 0);
  return { lines, refMap };
})()
"""


@dataclass
class BrowserSession:
    session_id: str
    browser: Any
    context: Any
    page: Any
    last_activity: float = field(default_factory=time.time)
    ref_map: Dict[str, Any] = field(default_factory=dict)
    _op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self):
        self.last_activity = time.time()


class InteractiveBrowserService:
    _instance: Optional["InteractiveBrowserService"] = None

    def __init__(self):
        self._sessions: Dict[str, BrowserSession] = {}
        self._playwright = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._started = False
        self._session_lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "InteractiveBrowserService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def _ensure_started(self):
        if self._started:
            return
        async with self._session_lock:
            if self._started:
                return
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                self._started = True
                logger.info("InteractiveBrowserService started")
            except ImportError:
                logger.warning("Playwright not installed, browser interaction unavailable")
            except Exception:
                logger.exception("Failed to start InteractiveBrowserService")

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self._cleanup_expired()
            except Exception:
                logger.exception("Browser session cleanup error, continuing")

    async def _cleanup_expired(self):
        timeout = getattr(config, "browser_interaction_session_timeout", _SESSION_TIMEOUT_SECONDS)
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_activity > timeout
        ]
        for sid in expired:
            await self.close_session(sid)
            logger.info("Expired browser session: %s", sid)

    async def get_or_create_session(self, conversation_id: str) -> Optional[BrowserSession]:
        await self._ensure_started()
        if not self._started:
            return None

        async with self._session_lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                session.touch()
                return session

            max_sessions = getattr(config, "browser_interaction_max_concurrent", _MAX_CONCURRENT_SESSIONS)
            if len(self._sessions) >= max_sessions:
                oldest_id = min(self._sessions, key=lambda k: self._sessions[k].last_activity)
                await self.close_session(oldest_id)

            browser = None
            context = None
            try:
                browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    java_script_enabled=True,
                    ignore_https_errors=bool(config.browser.get("ignore_https_errors", True)),
                )
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = await context.new_page()
                session = BrowserSession(
                    session_id=conversation_id,
                    browser=browser,
                    context=context,
                    page=page,
                )
                self._sessions[conversation_id] = session
                logger.info("Created browser session for conversation: %s", conversation_id)
                return session
            except Exception:
                logger.exception("Failed to create browser session")
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                return None

    async def close_session(self, conversation_id: str):
        session = self._sessions.pop(conversation_id, None)
        if not session:
            return
        try:
            await session.page.close()
        except Exception:
            pass
        try:
            await session.context.close()
        except Exception:
            pass
        try:
            await session.browser.close()
        except Exception:
            pass
        logger.info("Closed browser session: %s", conversation_id)

    async def shutdown(self):
        for sid in list(self._sessions.keys()):
            await self.close_session(sid)
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._started = False
        logger.info("InteractiveBrowserService shut down")


def _sanitize_error(msg: str) -> str:
    return msg[:200]


def _build_fallback_locator(session: BrowserSession, node: dict):
    tag = node.get("tag", "").lower()
    aria_label = node.get("ariaLabel", "")
    name_attr = node.get("name", "")
    placeholder = node.get("placeholder", "")
    role = node.get("role", "")
    text_content = node.get("value", "") or aria_label or name_attr

    if tag == "a" and text_content:
        return session.page.get_by_role("link", name=text_content, exact=False)
    if tag == "button" and text_content:
        return session.page.get_by_role("button", name=text_content, exact=False)
    if tag in ("input", "textarea", "select"):
        if aria_label:
            return session.page.get_by_label(aria_label, exact=False)
        if placeholder:
            return session.page.get_by_placeholder(placeholder, exact=False)
        if name_attr:
            return session.page.locator(f'{tag}[name="{name_attr}"]')
    if role == "button" and text_content:
        return session.page.get_by_role("button", name=text_content, exact=False)
    if role == "link" and text_content:
        return session.page.get_by_role("link", name=text_content, exact=False)
    return None


async def _take_snapshot(session: BrowserSession, full: bool = False) -> str:
    try:
        result = await session.page.evaluate(_EXTRACT_DOM_JS)
        if result and isinstance(result, dict):
            lines = result.get("lines", [])
            ref_map = result.get("refMap", {})
            session.ref_map = ref_map
            text = "\n".join(lines)
            if not full and len(text) > _SNAPSHOT_MAX_LENGTH:
                text = text[:_SNAPSHOT_MAX_LENGTH] + "\n... (truncated, use full=true for complete content)"
            return text
        text = await session.page.evaluate("document.body ? document.body.innerText : ''")
        return (text or "")[:_SNAPSHOT_MAX_LENGTH]
    except Exception as e:
        logger.debug("DOM extraction snapshot failed: %s", e)
        try:
            text = await session.page.evaluate("document.body ? document.body.innerText : ''")
            return (text or "")[:_SNAPSHOT_MAX_LENGTH]
        except Exception:
            return "Failed to get page content"


async def navigate(session: BrowserSession, url: str) -> dict:
    session.touch()
    session.ref_map = {}
    async with session._op_lock:
        try:
            await session.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await session.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            title = await session.page.title()
            snapshot_text = await _take_snapshot(session)
            return {
                "success": True,
                "url": session.page.url,
                "title": title or "",
                "snapshot": snapshot_text,
            }
        except Exception as e:
            logger.debug("Navigate failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"Navigation failed: {e}")}


async def snapshot(session: BrowserSession, full: bool = False) -> dict:
    session.touch()
    async with session._op_lock:
        try:
            content = await _take_snapshot(session, full=full)
            title = await session.page.title()
            return {
                "success": True,
                "url": session.page.url,
                "title": title or "",
                "snapshot": content,
            }
        except Exception as e:
            logger.debug("Snapshot failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"Snapshot failed: {e}")}


async def click(session: BrowserSession, ref: str) -> dict:
    session.touch()
    if not _REF_PATTERN.match(ref):
        return {"success": False, "error": f"Invalid ref format: {ref}"}
    node = session.ref_map.get(ref)
    if not node:
        return {"success": False, "error": f"Element {ref} not found. Take a new snapshot first."}
    async with session._op_lock:
        try:
            locator = session.page.locator(f'[data-browser-ref="{ref}"]')
            count = await locator.count()
            if count == 0:
                fallback = _build_fallback_locator(session, node)
                if fallback:
                    count = await fallback.count()
                    if count > 0:
                        locator = fallback.first
                    else:
                        return {"success": False, "error": f"Element {ref} no longer exists on page. Page may have changed."}
                else:
                    return {"success": False, "error": f"Element {ref} no longer exists on page. Page may have changed."}
            else:
                locator = locator.first
            await locator.click(timeout=10000)
            await asyncio.sleep(0.5)
            try:
                await session.page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            new_snapshot = await _take_snapshot(session)
            title = await session.page.title()
            return {
                "success": True,
                "clicked": ref,
                "url": session.page.url,
                "title": title or "",
                "snapshot": new_snapshot,
            }
        except Exception as e:
            logger.debug("Click failed for %s: %s", ref, e)
            return {"success": False, "error": _sanitize_error(f"Click failed for {ref}")}


async def type_text(session: BrowserSession, ref: str, text: str) -> dict:
    session.touch()
    if not _REF_PATTERN.match(ref):
        return {"success": False, "error": f"Invalid ref format: {ref}"}
    node = session.ref_map.get(ref)
    if not node:
        return {"success": False, "error": f"Element {ref} not found. Take a new snapshot first."}
    async with session._op_lock:
        try:
            locator = session.page.locator(f'[data-browser-ref="{ref}"]')
            count = await locator.count()
            if count == 0:
                fallback = _build_fallback_locator(session, node)
                if fallback:
                    count = await fallback.count()
                    if count > 0:
                        locator = fallback.first
                    else:
                        return {"success": False, "error": f"Input element {ref} no longer exists on page."}
                else:
                    return {"success": False, "error": f"Input element {ref} no longer exists on page."}
            else:
                locator = locator.first
            tag = node.get("tag", "").lower()
            if tag == "select":
                await locator.select_option(label=text, timeout=10000)
            else:
                await locator.fill(text, timeout=10000)
            return {"success": True, "typed": text, "ref": ref}
        except Exception as e:
            logger.debug("Type failed for %s: %s", ref, e)
            return {"success": False, "error": _sanitize_error(f"Type failed for {ref}")}


async def scroll(session: BrowserSession, direction: str) -> dict:
    session.touch()
    async with session._op_lock:
        try:
            delta = 500 if direction == "down" else -500
            await session.page.mouse.wheel(0, delta)
            await asyncio.sleep(0.3)
            new_snapshot = await _take_snapshot(session)
            return {"success": True, "direction": direction, "snapshot": new_snapshot}
        except Exception as e:
            logger.debug("Scroll failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"Scroll failed")}


async def press_key(session: BrowserSession, key: str) -> dict:
    session.touch()
    async with session._op_lock:
        try:
            await session.page.keyboard.press(key)
            await asyncio.sleep(0.5)
            try:
                await session.page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            new_snapshot = await _take_snapshot(session)
            return {"success": True, "pressed": key, "url": session.page.url, "snapshot": new_snapshot}
        except Exception as e:
            logger.debug("Press key failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"Key press failed")}


async def extract(session: BrowserSession, selector: str, attributes: list = None) -> dict:
    session.touch()
    async with session._op_lock:
        try:
            elements = await session.page.query_selector_all(selector)
            if not elements:
                return {"success": True, "selector": selector, "count": 0, "results": []}
            results = []
            attrs_to_extract = attributes if attributes else ["textContent"]
            for el in elements:
                item = {}
                for attr in attrs_to_extract:
                    if attr == "textContent":
                        text = await el.text_content()
                        item["textContent"] = (text or "").strip()
                    elif attr == "innerHTML":
                        html = await el.inner_html()
                        item["innerHTML"] = (html or "").strip()
                    elif attr == "innerText":
                        text = await el.inner_text()
                        item["innerText"] = (text or "").strip()
                    elif attr == "href":
                        href = await el.get_attribute("href")
                        item["href"] = href or ""
                    elif attr == "src":
                        src = await el.get_attribute("src")
                        item["src"] = src or ""
                    else:
                        val = await el.get_attribute(attr)
                        item[attr] = val or ""
                results.append(item)
            return {"success": True, "selector": selector, "count": len(results), "results": results}
        except Exception as e:
            logger.debug("Extract failed for '%s': %s", selector, e)
            return {"success": False, "error": _sanitize_error(f"Extract failed: {e}")}


async def execute_js(session: BrowserSession, script: str) -> dict:
    session.touch()
    async with session._op_lock:
        try:
            result = await session.page.evaluate(script)
            serialized = result
            if result is not None:
                try:
                    import json as _json
                    _json.dumps(result)
                    serialized = result
                except (TypeError, ValueError):
                    serialized = str(result)
            return {"success": True, "result": serialized}
        except Exception as e:
            logger.debug("Execute JS failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"JavaScript execution failed: {e}")}


async def screenshot(session: BrowserSession, full_page: bool = False) -> dict:
    session.touch()
    async with session._op_lock:
        try:
            import os
            from datetime import datetime
            output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output_files")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.png"
            filepath = os.path.join(output_dir, filename)
            await session.page.screenshot(path=filepath, full_page=full_page)
            url = session.page.url
            title = await session.page.title()
            return {
                "success": True,
                "path": filepath,
                "filename": filename,
                "url": url,
                "title": title or "",
                "full_page": full_page,
            }
        except Exception as e:
            logger.debug("Screenshot failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"Screenshot failed: {e}")}


async def go_back(session: BrowserSession) -> dict:
    session.touch()
    session.ref_map = {}
    async with session._op_lock:
        try:
            await session.page.go_back(wait_until="domcontentloaded", timeout=15000)
            try:
                await session.page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            title = await session.page.title()
            new_snapshot = await _take_snapshot(session)
            return {
                "success": True,
                "url": session.page.url,
                "title": title or "",
                "snapshot": new_snapshot,
            }
        except Exception as e:
            logger.debug("Go back failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"Navigation back failed")}
