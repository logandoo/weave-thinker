# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextvars
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
# 2026-11-03: after the caller's wait_selector is seen, wait for a real
# quiescence window. A single post-selector `networkidle` is NOT enough:
# Playwright fires that lifecycle event once per document, so a second call
# returns immediately when the pre-selector wait already saw idle. Canvas apps
# are the motivating class: they render a shell/loading canvas long before the
# app finishes loading its document, and nearly all need several seconds of
# warm-up (ComfyUI conv 7c9bad25: canvas ~5.3 s, target document ~7.3 s). The
# generic heuristic samples URL/title/DOM mutations/canvas geometry until it has
# been unchanged for a quiet window; explicit `wait_js` remains the
# deterministic path for known apps.
_CANVAS_SETTLE_QUIET_MS = 1000
_CANVAS_SETTLE_MIN_MS = 5000
_DEFAULT_SETTLE_QUIET_MS = 600
_SETTLE_MAX_MS = 15000

_SETTLE_OBSERVER_JS = """() => {
  if (window.__wt_settle_observer) return true;
  window.__wt_settle_mutations = 0;
  try {
    new MutationObserver(() => { window.__wt_settle_mutations += 1; })
      .observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
    window.__wt_settle_observer = true;
  } catch (e) {}
  return true;
}"""

# Generic page signature — deliberately no app-specific objects (no ComfyUI
# `app.graph`): URL, title, DOM mutation count, readyState, and canvas geometry.
# A canvas app that swaps its loaded document changes at least one of these.
_SETTLE_SIGNATURE_JS = """() => {
  let canvas = '';
  try {
    canvas = Array.from(document.querySelectorAll('canvas'))
      .map(c => c.width + 'x' + c.height + '@' + c.clientWidth + 'x' + c.clientHeight)
      .sort().join(',');
  } catch (e) {}
  return JSON.stringify({
    u: location.href,
    t: document.title,
    m: window.__wt_settle_mutations || 0,
    r: document.readyState,
    c: canvas,
  });
}"""

_HAS_LARGE_CANVAS_JS = """() => {
  try {
    const vw = Math.max(1, innerWidth * innerHeight);
    return Array.from(document.querySelectorAll('canvas')).some(c => {
      const area = (c.clientWidth || c.width) * (c.clientHeight || c.height);
      return area >= Math.max(40000, vw * 0.15);
    });
  } catch (e) { return false; }
}"""
# 2026-11-03: close each handle with a hard timeout. A wedged page/context must
# never leave the recorder (and its 1.3GB Chromium tree) alive forever.
_CLOSE_TIMEOUT_SECONDS = 10.0
# Crash leftovers: remove video dirs older than a day at service start.
_STALE_VIDEO_DIR_MAX_AGE = 24 * 3600
# 2026-11-03: only enforce max_lifetime on sessions that have also been idle
# this long — an actively used long turn must not be closed mid-tool-call.
_LIFETIME_IDLE_GRACE = 300
# Turn ownership: the chat transport sets a token around the agent turn so a
# turn-end close never kills a session created by a background task (whose
# token is None) or by a newer turn (different token).
BROWSER_TURN_TOKEN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "browser_turn_token", default=None
)
# P1：frame 内元素 ref 带 f{N}_ 前缀（iframe 遍历，2026-10-14）
_REF_PATTERN = re.compile(r'^__br_(?:f\d+_)?\d+$')

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

    // P1（2026-10-14 A4.9 同化 vercel-labs agent-browser）：cursor-interactive
    // 合成 ref——onclick/cursor:pointer/tabindex 的裸 div（自绘控件、role=none
    // 网格格子）也进 ref 表，消除「点不到的可点元素」盲区。
    const cursorClickable = (
      el.hasAttribute('onclick') ||
      el.getAttribute('role') === 'button' || el.getAttribute('role') === 'link' ||
      el.hasAttribute('tabindex') ||
      style.cursor === 'pointer'
    );

    if (INTERACTIVE_TAGS.has(tag) || cursorClickable) {
      const ref = getRef(el);
      const text = visibleText(el);
      let desc = tag.toLowerCase();
      const info = refMap[ref];
      if (!INTERACTIVE_TAGS.has(tag)) desc += ' clickable';
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
      // shadow DOM 穿透：自定义组件内部控件同样可达（el.children 不含 shadow 根）
      if (el.shadowRoot) {
        for (const child of el.shadowRoot.children) walk(child, depth + 1);
      }
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

    // P1：shadow DOM 穿透（el.children 不含 shadow 根——自定义组件内部不可达）
    if (el.shadowRoot) {
      for (const child of el.shadowRoot.children) walk(child, depth);
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
    # 2026-11-03: hard lifetime anchor — recording must not outlive this even
    # when the turn-end hook is missed (background tasks, crashes).
    created_at: float = field(default_factory=time.time)
    # None = owned by a background/worker path; "turn:<uuid>" = chat turn.
    turn_token: Optional[str] = None
    ref_map: Dict[str, Any] = field(default_factory=dict)
    _op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # P3/B5：Playwright 原生录像目录（context 创建即开录，活体 webm 可随时
    # 保存为证据；会话关闭时清理）。
    video_dir: str = ""

    def touch(self):
        self.last_activity = time.time()


def _sweep_stale_video_dirs(max_age_seconds: int = _STALE_VIDEO_DIR_MAX_AGE) -> int:
    """2026-11-03: remove crash leftovers under the system temp dir.

    Only dirs older than ``max_age_seconds`` are touched, so a concurrently
    running worker's live recording is never deleted.
    """
    import glob
    import os
    import shutil
    import tempfile

    removed = 0
    try:
        pattern = os.path.join(tempfile.gettempdir(), "wt_browser_video_*")
        now = time.time()
        for path in glob.glob(pattern):
            try:
                if not os.path.isdir(path):
                    continue
                if now - os.path.getmtime(path) <= max_age_seconds:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except Exception:
                logger.debug("stale video dir cleanup failed: %s", path, exc_info=True)
    except Exception:
        logger.debug("stale video dir sweep failed", exc_info=True)
    if removed:
        logger.info("Removed %d stale browser video dir(s)", removed)
    return removed


def _storage_state_path(conversation_id: str) -> str:
    try:
        import os
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9_-]", "", str(conversation_id or ""))[:64] or "anon"
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browser_state")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{safe}.json")
    except Exception:
        return ""


class InteractiveBrowserService:
    _instance: Optional["InteractiveBrowserService"] = None

    def __init__(self):
        self._sessions: Dict[str, BrowserSession] = {}
        self._playwright = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._started = False
        self._session_lock = asyncio.Lock()
        # Strong refs for detached eviction closes (never block the lock).
        self._evict_tasks: set = set()

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
                await asyncio.to_thread(_sweep_stale_video_dirs)
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
        try:
            max_lifetime = int(getattr(config, "browser_interaction_max_lifetime", 1800))
        except (TypeError, ValueError):
            max_lifetime = 1800
        now = time.time()
        # 2026-11-03: two independent bounds. Idle timeout keeps the old
        # semantics; the hard lifetime caps recording even when a session is
        # still being touched by a stuck/background run.
        expired: list[tuple[str, BrowserSession, str]] = []
        idle_grace = min(max(0, timeout), _LIFETIME_IDLE_GRACE)
        for sid, s in list(self._sessions.items()):
            if now - s.last_activity > timeout:
                expired.append((sid, s, f"idle>{timeout}s"))
            elif (
                max_lifetime > 0
                and now - s.created_at > max_lifetime
                and now - s.last_activity > idle_grace
            ):
                # Never close a session that is actively being used — the cap
                # only reaps stuck/background recorders.
                expired.append((sid, s, f"lifetime>{max_lifetime}s"))
        for sid, sess, reason in expired:
            # expected=sess: if the key was re-created while we were closing
            # an earlier session, do not kill the fresh one.
            if await self.close_session(sid, reason=reason, expected=sess):
                logger.info("Expired browser session: %s (%s)", sid, reason)

    async def get_or_create_session(self, conversation_id: str) -> Optional[BrowserSession]:
        await self._ensure_started()
        if not self._started:
            return None

        async with self._session_lock:
            if conversation_id in self._sessions:
                session = self._sessions[conversation_id]
                session.touch()
                _tok = BROWSER_TURN_TOKEN.get()
                # chat→chat handoff keeps turn ownership; a background-owned
                # session (turn_token=None) is never stolen by a chat turn.
                if _tok and (session.turn_token or "").startswith("turn:"):
                    session.turn_token = _tok
                return session

            max_sessions = getattr(config, "browser_interaction_max_concurrent", _MAX_CONCURRENT_SESSIONS)
            if len(self._sessions) >= max_sessions:
                oldest_id = min(self._sessions, key=lambda k: self._sessions[k].last_activity)
                # 2026-11-03: pop under the lock, close OUTSIDE it — a wedged
                # close used to block every other conversation for up to 40s.
                _evicted = self._sessions.pop(oldest_id, None)
                if _evicted is not None:
                    self._schedule_close(oldest_id, _evicted, "max_concurrent")

            browser = None
            context = None
            try:
                # P3/B5：录像目录（Evidence 视频；显式保存才落到 output_files）
                import tempfile
                video_dir = await asyncio.to_thread(
                    tempfile.mkdtemp, prefix=f"wt_browser_video_{conversation_id}_"
                )
                browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                # P1（2026-10-14）：storage_state 持久——会话超时重建不丢登录态
                # （cookies/localStorage 按会话落盘，路径含会话 id 防跨租户读）。
                # 损坏 JSON fail-open：解析失败剔除文件并裸启（绝不因此建不了会话）。
                import os
                _state_path = _storage_state_path(conversation_id)
                _state_arg = None
                if _state_path and os.path.isfile(_state_path):
                    try:
                        import json as _json
                        with open(_state_path, "r", encoding="utf-8") as _sf:
                            _json.load(_sf)
                        _state_arg = _state_path
                    except Exception:
                        try:
                            os.remove(_state_path)
                        except Exception:
                            pass
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    java_script_enabled=True,
                    ignore_https_errors=bool(config.browser.get("ignore_https_errors", True)),
                    record_video_dir=video_dir,
                    record_video_size={"width": 1280, "height": 720},
                    storage_state=_state_arg,
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
                    video_dir=video_dir,
                    turn_token=BROWSER_TURN_TOKEN.get(),
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

    def _schedule_close(
        self, conversation_id: str, session: BrowserSession, reason: str
    ) -> None:
        """Close a detached session without blocking the caller/lock."""
        async def _run() -> None:
            try:
                await self._close_session_obj(conversation_id, session, reason)
            except Exception:
                logger.exception("detached browser close failed for %s", conversation_id)

        try:
            task = asyncio.create_task(
                _run(), name=f"browser-evict-{conversation_id[:8]}"
            )
            self._evict_tasks.add(task)
            task.add_done_callback(self._evict_tasks.discard)
        except Exception:
            logger.debug("detached browser close scheduling failed", exc_info=True)

    async def close_for_turn_end(
        self, conversation_id: str, turn_token: Optional[str] = None
    ) -> bool:
        """2026-11-03: release the browser/recorder when an agent turn ends.

        Playwright's ``record_video_dir`` stops encoding only on
        ``context.close()``; without this hook a finished turn leaves the
        Chromium + ffmpeg tree running until the (long) idle timeout.

        ``turn_token`` guards ownership: a session created by a background
        task (None) or by a newer turn (different token) is never closed.
        Returns True when a session was closed.
        """
        if not getattr(config, "browser_interaction_close_on_turn_end", True):
            return False
        session = self._sessions.get(conversation_id)
        if session is None:
            return False
        if turn_token is not None and session.turn_token != turn_token:
            return False
        return await self.close_session(
            conversation_id, reason="turn_end", expected=session
        )

    async def close_session(
        self,
        conversation_id: str,
        reason: str = "manual",
        expected: Optional[BrowserSession] = None,
    ) -> bool:
        current = self._sessions.get(conversation_id)
        if expected is not None and current is not expected:
            # The key was re-created while a previous close was running.
            return False
        session = self._sessions.pop(conversation_id, None)
        if not session:
            return False
        await self._close_session_obj(conversation_id, session, reason)
        return True

    async def _close_session_obj(
        self, conversation_id: str, session: BrowserSession, reason: str
    ) -> None:
        started = time.monotonic()
        # P1：关会话前落盘登录态（重建可恢复）
        try:
            _sp = _storage_state_path(conversation_id)
            if _sp:
                await asyncio.wait_for(
                    session.context.storage_state(path=_sp), timeout=_CLOSE_TIMEOUT_SECONDS
                )
                try:
                    import os as _os
                    _os.chmod(_sp, 0o600)  # 登记态含 cookie——600（A4.9 Minor-10）
                except Exception:
                    pass
        except Exception:
            logger.debug("storage_state save failed for %s", conversation_id, exc_info=True)
        # Order matters: page → context (finalizes the webm) → browser.
        for label, closer in (
            ("page", session.page.close),
            ("context", session.context.close),
            ("browser", session.browser.close),
        ):
            try:
                await asyncio.wait_for(closer(), timeout=_CLOSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # Expected under a wedged page: keep going, free the rest.
                logger.info(
                    "browser %s close timed out after %ss for session %s",
                    label, _CLOSE_TIMEOUT_SECONDS, conversation_id,
                )
            except Exception:
                logger.warning(
                    "browser %s close failed for session %s",
                    label, conversation_id, exc_info=True,
                )
        if session.video_dir:
            try:
                import shutil
                await asyncio.to_thread(shutil.rmtree, session.video_dir, True)
            except Exception:
                pass
        logger.info(
            "Closed browser session: %s (reason=%s, %.2fs)",
            conversation_id, reason, time.monotonic() - started,
        )

    async def shutdown(self):
        """Bounded shutdown: a wedged browser must not delay process exit."""
        async def _close_all() -> None:
            for sid in list(self._sessions.keys()):
                try:
                    await self.close_session(sid, reason="shutdown")
                except BaseException:
                    pass

        try:
            await asyncio.wait_for(_close_all(), timeout=30.0)
        except Exception:
            logger.warning("browser session shutdown exceeded budget", exc_info=True)
        finally:
            for task in list(self._evict_tasks):
                task.cancel()
            self._evict_tasks.clear()
            if self._playwright:
                try:
                    await asyncio.wait_for(self._playwright.stop(), timeout=10.0)
                except Exception:
                    logger.warning("playwright stop failed/timed out", exc_info=True)
            if self._cleanup_task:
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except BaseException:
                    pass
            self._started = False
            logger.info("InteractiveBrowserService shut down")


def _sanitize_error(msg: str) -> str:
    return msg[:200]


def _frame_of(session: BrowserSession, node: dict):
    """P1：frame 内元素（refMap 带 frame 序号）定位到对应 Playwright frame。"""
    fi = node.get("frame") if isinstance(node, dict) else None
    if isinstance(fi, int) and 0 < fi < len(session.page.frames):
        try:
            return session.page.frames[fi]
        except Exception:
            return session.page
    return session.page


def _build_fallback_locator(session: BrowserSession, node: dict):
    _tgt = _frame_of(session, node)
    tag = node.get("tag", "").lower()
    aria_label = node.get("ariaLabel", "")
    name_attr = node.get("name", "")
    placeholder = node.get("placeholder", "")
    role = node.get("role", "")
    text_content = node.get("value", "") or aria_label or name_attr

    if tag == "a" and text_content:
        return _tgt.get_by_role("link", name=text_content, exact=False)
    if tag == "button" and text_content:
        return _tgt.get_by_role("button", name=text_content, exact=False)
    if tag in ("input", "textarea", "select"):
        if aria_label:
            return _tgt.get_by_label(aria_label, exact=False)
        if placeholder:
            return _tgt.get_by_placeholder(placeholder, exact=False)
        if name_attr:
            return _tgt.locator(f'{tag}[name="{name_attr}"]')
    if role == "button" and text_content:
        return _tgt.get_by_role("button", name=text_content, exact=False)
    if role == "link" and text_content:
        return _tgt.get_by_role("link", name=text_content, exact=False)
    return None


async def _take_snapshot(session: BrowserSession, full: bool = False) -> str:
    try:
        result = await session.page.evaluate(_EXTRACT_DOM_JS)
        lines = list((result or {}).get("lines", [])) if isinstance(result, dict) else []
        ref_map = dict((result or {}).get("refMap", {})) if isinstance(result, dict) else {}
        # P1（2026-10-14）：iframe 遍历——Playwright frame API 跨越同源/跨源
        # frame（页面 JS 无法读跨源 document），逐 frame 复用同一 walker，
        # ref 编号经前缀偏移保持全页唯一。
        try:
            frames = list(session.page.frames)
        except Exception:
            frames = []
        for fi, frame in enumerate(frames[1:6], start=1):  # 上限 5 个子 frame
            try:
                fr = await frame.evaluate(_EXTRACT_DOM_JS)
                if isinstance(fr, dict):
                    fmap = fr.get("refMap") or {}
                    offset = len(ref_map)
                    prefix = f"f{fi}"
                    for k, v in fmap.items():
                        new_k = f"__br_{prefix}_{k.split('__br_')[-1]}"
                        if isinstance(v, dict):
                            v = dict(v)
                            v["frame"] = fi
                        ref_map[new_k] = v
                    for ln in (fr.get("lines") or []):
                        def _remap(match, _off=offset, _pfx=prefix):
                            return f"[@__br_{_pfx}_{match.group(1)}]"
                        lines.append(f"[iframe {fi}] " + re.sub(r"\[@__br_(\d+)\]", _remap, ln))
            except Exception:
                continue
        if lines or ref_map:
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


async def _wait_for_page_settle(
    session: BrowserSession,
    *,
    quiet_ms: int,
    min_ms: int = 0,
    max_ms: int = 0,
) -> tuple[bool, int, bool]:
    """Sample page state until it has been unchanged for ``quiet_ms``.

    Returns ``(settled, elapsed_ms, observed_change)``.
    ``settled`` only means "the observed page signature was stable for the
    quiet window" — it does **not** prove the target document finished loading
    (a static loading/boot screen is also stable).  ``observed_change`` says
    whether any transition was seen during the window; callers treat
    ``settled and not observed_change`` as "verify identity / pass wait_js",
    not as ready.  ``settled=False`` means the page kept changing or the
    observation itself kept failing.
    """
    max_ms = max_ms or _SETTLE_MAX_MS
    if quiet_ms <= 0 and min_ms <= 0:
        return True, 0, False
    try:
        await session.page.evaluate(_SETTLE_OBSERVER_JS)
    except Exception:
        pass
    start = time.monotonic()
    last_sig = None
    last_change = start
    observed_change = False
    eval_failures = 0
    while True:
        try:
            sig = await session.page.evaluate(_SETTLE_SIGNATURE_JS)
            eval_failures = 0
        except Exception:
            sig = None
            eval_failures += 1
        now = time.monotonic()
        elapsed_ms = int((now - start) * 1000)
        if sig is None:
            # A dead/blocked page is not "stable"; never let a failed sample
            # fall through to the quiet-window check using a stale signature.
            if eval_failures >= 3:
                return False, elapsed_ms, observed_change
            await asyncio.sleep(0.2)
            continue
        if last_sig is None:
            last_sig = sig
            last_change = now
        elif sig != last_sig:
            last_sig = sig
            last_change = now
            observed_change = True
        if elapsed_ms >= max_ms:
            return False, elapsed_ms, observed_change
        if elapsed_ms >= min_ms and (now - last_change) * 1000 >= quiet_ms:
            return True, elapsed_ms, observed_change
        await asyncio.sleep(0.2)


async def navigate(session: BrowserSession, url: str, wait_selector: str = "",
                   timeout_ms: int = 0, wait_js: str = "") -> dict:
    session.touch()
    session.ref_map = {}
    async with session._op_lock:
        try:
            _nav_to = max(2000, min(int(timeout_ms or 20000), 120000))
            await session.page.goto(url, wait_until="domcontentloaded", timeout=_nav_to)
            try:
                await session.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            # P1（2026-10-14）：就绪等待——WASM/重资源游戏（Godot pck）DOM 就绪
            # ≠ UI 就绪，wait_selector 等目标控件出现后再快照（如 'canvas'）。
            warnings: list[str] = []
            selector_found = None
            if wait_selector:
                try:
                    await session.page.wait_for_selector(wait_selector, timeout=min(_nav_to, 30000))
                    selector_found = True
                except Exception:
                    selector_found = False
                    warnings.append(
                        f"wait_selector '{wait_selector}' not found within "
                        f"{min(_nav_to, 30000)} ms; page may not be ready"
                    )
                    logger.debug("wait_selector %s not seen within timeout", wait_selector)
            # 2026-11-03: selector-ready ≠ app-ready. ComfyUI creates <canvas>
            # ~2 s before the site replaces its boot/default graph with the real
            # workflow (conv 7c9bad25). If the caller supplied an app-state
            # predicate, it is authoritative; otherwise observe real quiescence
            # (URL/title/DOM mutations/readyState/canvas geometry) before we
            # snapshot and hand control back to the agent.
            settled = None
            settle_ms = None
            settle_mode = None
            observed_change = None
            try:
                _is_canvas = "canvas" in wait_selector.lower() or bool(
                    await session.page.evaluate(_HAS_LARGE_CANVAS_JS)
                )
            except Exception:
                _is_canvas = "canvas" in wait_selector.lower()
            if wait_js:
                _wait_js_budget = min(_nav_to, 15000)
                _wait_js_start = time.monotonic()
                try:
                    await session.page.wait_for_function(wait_js, timeout=_wait_js_budget)
                    settled, settle_mode = True, "wait_js"
                    settle_ms = int((time.monotonic() - _wait_js_start) * 1000)
                except Exception as e:
                    title = ""
                    try:
                        title = await session.page.title()
                    except Exception:
                        pass
                    snapshot_text = ""
                    try:
                        snapshot_text = await _take_snapshot(session)
                    except Exception:
                        pass
                    # Do not hand back live element refs for a page whose
                    # requested readiness condition was never met — a
                    # browser_click on them would repeat the original
                    # act-on-the-boot-page failure.
                    session.ref_map = {}
                    _is_timeout = type(e).__name__ in ("TimeoutError", "PlaywrightTimeoutError")
                    if _is_timeout:
                        _why = (
                            f"wait_js condition not met within {_wait_js_budget} ms; the page may "
                            "still be booting. Inspect the snapshot and load/select the intended "
                            "document explicitly (do not treat the boot/default document as ready)."
                        )
                    else:
                        # Advice first: _sanitize_error truncates long payloads,
                        # and the Playwright stack would otherwise eat the fix.
                        _detail = _sanitize_error(f"{type(e).__name__}: {e}")
                        _why = (
                            "wait_js evaluation failed; guard references with "
                            "typeof x !== 'undefined' / optional chaining. " + _detail
                        )
                    warnings.append("element refs were invalidated; take a fresh snapshot before any click")
                    out = {
                        "success": False,
                        "error": _why,
                        "url": session.page.url,
                        "title": title or "",
                        "snapshot": snapshot_text,
                        "settled": False,
                        "settle_mode": "wait_js",
                        "canvas": _is_canvas,
                    }
                    if selector_found is not None:
                        out["wait_selector_found"] = selector_found
                    if warnings:
                        out["warnings"] = warnings
                    return out
            else:
                # Canvas apps get a longer minimum observation + quiet window:
                # nearly all render a shell/loading canvas for several seconds
                # before the real document is ready. Non-canvas selector-gated
                # calls get a short settle; plain navigations keep old behavior.
                if wait_selector or _is_canvas:
                    _quiet = _CANVAS_SETTLE_QUIET_MS if _is_canvas else _DEFAULT_SETTLE_QUIET_MS
                    _min = _CANVAS_SETTLE_MIN_MS if _is_canvas else 0
                    settled, settle_ms, observed_change = await _wait_for_page_settle(
                        session, quiet_ms=_quiet, min_ms=_min, max_ms=_SETTLE_MAX_MS
                    )
                    settle_mode = "canvas_heuristic" if _is_canvas else "heuristic"
                    if not settled:
                        warnings.append(
                            f"page did not reach a stable state within {_SETTLE_MAX_MS} ms; "
                            "use wait_js with a condition that identifies the target document"
                        )
                    elif _is_canvas:
                        # A canvas boot/default document may transition once and
                        # still not be the target; stability is never readiness.
                        warnings.append(
                            "canvas page reached a stable state, but stability is not proof that "
                            "the target document loaded — verify identity (title/route/target "
                            "element/model) or pass wait_js"
                        )
                        if not observed_change:
                            warnings.append(
                                "canvas page showed no state transition during the stability "
                                "window; it may still be a static loading/boot screen"
                            )
            title = await session.page.title()
            snapshot_text = await _take_snapshot(session)
            result = {
                "success": True,
                "url": session.page.url,
                "title": title or "",
                "snapshot": snapshot_text,
            }
            if selector_found is not None:
                result["wait_selector_found"] = selector_found
            if settled is not None:
                result["settled"] = settled
                result["settle_mode"] = settle_mode
                if settle_ms is not None:
                    result["settle_ms"] = settle_ms
                if observed_change is not None:
                    result["observed_change"] = observed_change
            if _is_canvas:
                result["canvas"] = True
            if warnings:
                result["warnings"] = warnings
            return result
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
            _dom_ref = re.sub(r"^__br_f\d+_", "__br_", ref)
            locator = _frame_of(session, node).locator(f'[data-browser-ref="{_dom_ref}"]')
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
            _dom_ref = re.sub(r"^__br_f\d+_", "__br_", ref)
            locator = _frame_of(session, node).locator(f'[data-browser-ref="{_dom_ref}"]')
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


async def screenshot(session: BrowserSession, full_page: bool = False,
                     name_prefix: str = "") -> dict:
    session.touch()
    async with session._op_lock:
        try:
            import os
            from datetime import datetime
            output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output_files")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # I6（A4.9）：文件名带会话前缀——output_files 为多租户共享目录，
            # 前缀供 vision_interpret 归属校验（防跨用户读取截图）。
            safe_prefix = "".join(c for c in (name_prefix or "") if c.isalnum() or c in "-_")[:64]
            filename = f"screenshot_{safe_prefix}_{timestamp}.png" if safe_prefix else f"screenshot_{timestamp}.png"
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


async def save_video(session: BrowserSession, note: str = "") -> dict:
    """P3/B5：把会话录像（Playwright 活体 webm）保存为证据文件。

    录像在 context 创建时已开录（record_video_dir），本操作把当前已写入的
    webm 拷贝到 output_files/。活体拷贝可能缺尾部 cluster（可播放）。

    2026-11-03：录像随 agent 回合结束销毁（回合结束会 close 会话以停止
    Chromium/ffmpeg 编码），因此必须在同一回合内 save；跨回合补存不可能。
    页内音频未随录像采集——工具返回 UNVERIFIED-audio 标记（见 APPENDIX §A1
    的 MediaRecorder 模板，可经 browser_execute_js 自行采集）。
    """
    session.touch()
    async with session._op_lock:
        try:
            import os
            import shutil
            from datetime import datetime
            if not session.video_dir or not os.path.isdir(session.video_dir):
                return {"success": False, "error": "recording not available on this session"}
            candidates = [
                os.path.join(session.video_dir, f)
                for f in sorted(os.listdir(session.video_dir))
                if f.endswith(".webm")
            ]
            if not candidates:
                return {"success": False, "error": "no video written yet (navigate/interact first)"}
            src = max(candidates, key=lambda p: os.path.getsize(p))
            output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output_files")
            os.makedirs(output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"browser_video_{timestamp}.webm"
            filepath = os.path.join(output_dir, filename)
            await asyncio.to_thread(shutil.copyfile, src, filepath)
            return {
                "success": True,
                "path": filepath,
                "filename": filename,
                "bytes": os.path.getsize(filepath),
                "url": session.page.url,
                "note": note,
                "audio": "UNVERIFIED-audio (页内音频未随录像采集；需要音频证据时用 APPENDIX §A1 模板经 browser_execute_js 采集)",
            }
        except Exception as e:
            logger.debug("save_video failed: %s", e)
            return {"success": False, "error": _sanitize_error(f"save_video failed: {e}")}


async def video_status(session: BrowserSession) -> dict:
    session.touch()
    import os
    if not session.video_dir or not os.path.isdir(session.video_dir):
        return {"success": True, "recording": False}
    files = [f for f in os.listdir(session.video_dir) if f.endswith(".webm")]
    total = sum(os.path.getsize(os.path.join(session.video_dir, f)) for f in files)
    return {"success": True, "recording": True, "clips": len(files), "bytes": total}


async def mouse(session: BrowserSession, action: str, x: float = 0, y: float = 0,
                button: str = "left", clicks: int = 1, delta_y: float = 0) -> dict:
    """P0 坐标鼠标原语（Playwright vision mode 对齐，CDP trusted 事件）。

    canvas/WebGL/自绘控件无 DOM ref，坐标是唯一寻址方式（上轮评估 P0-1）。
    action: move/down/up/click/wheel；坐标=视口 CSS 像素。
    """
    session.touch()
    async with session._op_lock:
        try:
            if action == "move":
                await session.page.mouse.move(x, y)
            elif action == "down":
                await session.page.mouse.down(button=button)
            elif action == "up":
                await session.page.mouse.up(button=button)
            elif action == "click":
                await session.page.mouse.click(x, y, button=button, clicks=clicks)
            elif action == "wheel":
                # M1（A4.9）：滚动量只认 delta_y——不得把 y 坐标偷换成滚动量
                # （模型误传坐标即意外滚动）。
                await session.page.mouse.wheel(0, delta_y)
            else:
                return {"success": False, "error": f"Unknown mouse action: {action}"}
            await asyncio.sleep(0.2)
            return {"success": True, "action": action, "x": x, "y": y}
        except Exception as e:
            logger.debug("Mouse %s failed: %s", action, e)
            # M3（A4.9）：用户面错误不含裸异常串（URL/路径不外泄，口径对齐 click）。
            return {"success": False, "error": _sanitize_error(f"Mouse {action} failed")}


async def type_focused(session: BrowserSession, text: str, submit: bool = False) -> dict:
    """向当前聚焦元素键入字符串（CDP trusted 键盘事件，逐字符真实按键）。

    canvas 游戏/隐藏 IME 输入（Godot textarea）只能靠「点击聚焦后键入」——
    fill() 要求可见 input，对 canvas/隐藏 IME 必败（上轮评估 P0-3）。
    """
    session.touch()
    async with session._op_lock:
        try:
            await session.page.keyboard.type(text, delay=20)
            if submit:
                await session.page.keyboard.press("Enter")
                await asyncio.sleep(0.5)
                try:
                    await session.page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
            return {"success": True, "typed": text, "submit": submit}
        except Exception as e:
            logger.debug("Type-focused failed: %s", e)
            # M3（A4.9）：同上，用户面错误不含裸异常串。
            return {"success": False, "error": _sanitize_error("Type into focused element failed")}


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
