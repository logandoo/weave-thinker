# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Localize remote media embedded in agent answers into the user's workspace.

When the agent decides to display a remote image/video/audio in its final
answer (markdown `![...](https://...)` or raw `<img>/<video>/<audio>/<source>`
tags), the bytes are downloaded once into `{workspace}/media/`, named by
content sha256, and the answer is rewritten to the workspace-relative path.
The frontend's existing rewriteImageUrls/rewriteMediaUrls then signs that
relative path into `/api/files/download?...&token=...`, so the conversation
renders the local copy instead of re-fetching the remote link.

Failures (404, oversize, non-media MIME, timeout, private hosts) leave the
original remote URL untouched — localization is best-effort and never blocks
persistence.
"""
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from app.core.config import get_config
from app.services.http_client import get_shared_async_client

logger = logging.getLogger(__name__)

MEDIA_DIRNAME = "media"
_INDEX_FILENAME = "index.json"

# Combined display-context media pattern (single pass → document order).
# Group 1/2/3: markdown image (balanced one-level parens in URL allowed).
# Group 4/5/6: <img|video|audio|source ... src="..."> (whitespace before src,
# so data-src is not matched).
_MEDIA_RE = re.compile(
    r"(!\[[^\]]*\]\(\s*)(https?://(?:[^()\s]|\([^()\s]*\))+)((?:\s+[^)]*)?\))"
    r"|(<(?:img|video|audio|source)\b[^>]*?\ssrc\s*=\s*[\"'])(https?://[^\"']+)([\"'])",
    re.IGNORECASE,
)
_INLINE_CODE_RE = re.compile(r"(`+)[^`\n]*?\1")
_FENCE_OPEN_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif", ".ico"}
_AUD_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
_VID_EXT = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".m3u8"}

_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp",
    "image/avif": ".avif", "image/x-icon": ".ico",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-m4v": ".m4v", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/flac": ".flac",
    "audio/aac": ".aac", "audio/opus": ".opus",
}

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_INDEX_LOCK = asyncio.Lock()
_IN_FLIGHT: dict[tuple[str, str], asyncio.Task] = {}


@dataclass
class MediaLocalizeSettings:
    enabled: bool = True
    max_per_message: int = 8
    timeout_seconds: float = 15.0
    max_image_bytes: int = 20 * 1024 * 1024
    max_audio_bytes: int = 50 * 1024 * 1024
    max_video_bytes: int = 200 * 1024 * 1024
    allow_private_hosts: bool = False
    total_timeout_seconds: float = 12.0
    max_dir_bytes: int = 1024 * 1024 * 1024
    neg_cache_ttl_seconds: int = 3600


def settings_from_config() -> MediaLocalizeSettings:
    cfg = get_config()
    sec = cfg.agent_media_localize
    return MediaLocalizeSettings(
        enabled=bool(sec.get("enabled", True)),
        max_per_message=int(sec.get("max_per_message", 8)),
        timeout_seconds=float(sec.get("timeout_seconds", 15)),
        max_image_bytes=int(sec.get("max_image_bytes", 20 * 1024 * 1024)),
        max_audio_bytes=int(sec.get("max_audio_bytes", 50 * 1024 * 1024)),
        max_video_bytes=int(sec.get("max_video_bytes", 200 * 1024 * 1024)),
        allow_private_hosts=bool(sec.get("allow_private_hosts", False)),
        total_timeout_seconds=float(sec.get("total_timeout_seconds", 12)),
        max_dir_bytes=int(sec.get("max_dir_bytes", 1024 * 1024 * 1024)),
        neg_cache_ttl_seconds=int(sec.get("neg_cache_ttl_seconds", 3600)),
    )


def _code_spans(content: str) -> list[tuple[int, int]]:
    """(start, end) offsets of fenced code blocks, indented code blocks and
    inline code spans. Indented code follows the CommonMark rule: an indented
    line starts a code block only after a blank line / another indented-code
    line (it cannot interrupt a paragraph)."""
    spans: list[tuple[int, int]] = []
    off = 0
    fence_start: int | None = None
    fence_char = ""
    fence_len = 0
    prev_allows_indented = True
    for line in content.split("\n"):
        line_end = off + len(line)
        if fence_start is None:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                fence_start = off
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                prev_allows_indented = False
            else:
                stripped = line.strip()
                indented = line.startswith(("    ", "\t"))
                if indented and stripped and prev_allows_indented:
                    spans.append((off, line_end))
                    prev_allows_indented = True
                else:
                    for im in _INLINE_CODE_RE.finditer(line):
                        spans.append((off + im.start(), off + im.end()))
                    prev_allows_indented = (stripped == "")
        else:
            if re.match(rf"^\s{{0,3}}{re.escape(fence_char)}{{{fence_len},}}\s*$", line):
                spans.append((fence_start, line_end))
                fence_start = None
                prev_allows_indented = True
        off = line_end + 1
    if fence_start is not None:
        spans.append((fence_start, len(content)))
    return spans


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def _match_parts(m: re.Match) -> tuple[str, str, str]:
    if m.group(2) is not None:
        return m.group(1), m.group(2), m.group(3)
    return m.group(4), m.group(5), m.group(6)


def extract_remote_media_urls(content: str) -> list[str]:
    """Ordered, de-duplicated remote media URLs outside code spans."""
    if not content:
        return []
    spans = _code_spans(content)
    seen: set[str] = set()
    out: list[str] = []
    for m in _MEDIA_RE.finditer(content):
        if _in_spans(m.start(), spans):
            continue
        url = _match_parts(m)[1].strip()
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _apply_mapping(content: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return content
    spans = _code_spans(content)
    out: list[str] = []
    pos = 0
    for m in _MEDIA_RE.finditer(content):
        pre, url, post = _match_parts(m)
        target = mapping.get(url.strip())
        if target is None or _in_spans(m.start(), spans):
            continue
        out.append(content[pos:m.start()])
        out.append(pre + target + post)
        pos = m.end()
    out.append(content[pos:])
    return "".join(out)


def _url_ext(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext or "") else ""


def _classify(url: str, content_type: str, s: MediaLocalizeSettings):
    """Return (max_bytes, ext) or None if not displayable media.

    Extension prefers the MIME-derived value when the Content-Type itself is a
    media type (a `.svg` URL serving PNG bytes is stored as `.png`).
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    ext = _url_ext(url)
    if mime.startswith("image/"):
        return s.max_image_bytes, _MIME_EXT.get(mime) or ext or ".png"
    if mime.startswith("video/"):
        return s.max_video_bytes, _MIME_EXT.get(mime) or ext or ".mp4"
    if mime.startswith("audio/"):
        return s.max_audio_bytes, _MIME_EXT.get(mime) or ext or ".mp3"
    if mime in ("application/octet-stream", "binary/octet-stream", ""):
        if ext in _IMG_EXT:
            return s.max_image_bytes, ext
        if ext in _VID_EXT:
            return s.max_video_bytes, ext
        if ext in _AUD_EXT:
            return s.max_audio_bytes, ext
    return None


async def _host_allowed(url: str, allow_private: bool) -> bool:
    if allow_private:
        return True
    try:
        host = urlparse(url).hostname
    except Exception:
        return False
    if not host:
        return False
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    ips = {info[4][0] for info in infos}
    if not ips:
        return False
    for ip_s in ips:
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


async def _download_one(url: str, media_dir: Path, s: MediaLocalizeSettings):
    """Fetch url (manual redirects, per-hop host re-validation) → media_dir.

    Returns an index entry dict, or None on any failure (caller keeps the
    original remote URL).
    """
    client = get_shared_async_client()
    current = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            if not await _host_allowed(current, s.allow_private_hosts):
                logger.info("media_localize: %s -> host not allowed", current)
                return None
            async with client.stream(
                "GET", current, timeout=s.timeout_seconds,
                follow_redirects=False,
                headers={"User-Agent": _BROWSER_UA},
            ) as resp:
                if resp.status_code in _REDIRECT_CODES:
                    loc = resp.headers.get("location")
                    if not loc:
                        return None
                    current = urljoin(current, loc)
                    continue
                if resp.status_code != 200:
                    logger.info("media_localize: %s -> HTTP %s", current, resp.status_code)
                    return None
                klass = _classify(current, resp.headers.get("content-type", ""), s)
                if klass is None:
                    logger.info("media_localize: %s -> non-media Content-Type %s",
                                current, resp.headers.get("content-type"))
                    return None
                cap, ext = klass
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > cap:
                    logger.info("media_localize: %s -> Content-Length %s over cap", current, cl)
                    return None
                hasher = hashlib.sha256()
                total = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > cap:
                        logger.info("media_localize: %s -> streamed %s over cap", current, total)
                        return None
                    hasher.update(chunk)
                    chunks.append(chunk)
                if total == 0:
                    return None
                name = hasher.hexdigest() + ext
                path = media_dir / name
                if not path.exists():
                    await asyncio.to_thread(path.write_bytes, b"".join(chunks))
                return {
                    "file": name,
                    "sha256": hasher.hexdigest(),
                    "size": total,
                    "mime": resp.headers.get("content-type", ""),
                    "ts": time.time(),
                }
        logger.info("media_localize: %s -> too many redirects", url)
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info("media_localize: %s -> download failed: %r", current, exc)
        return None


def _load_index(media_dir: Path) -> dict:
    try:
        data = json.loads((media_dir / _INDEX_FILENAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _enforce_quota(media_dir: Path, index: dict, s: MediaLocalizeSettings) -> None:
    """Evict oldest media files (and their index entries) until under cap."""
    cap = s.max_dir_bytes
    if cap <= 0:
        return
    try:
        files = []
        total = 0
        for p in media_dir.iterdir():
            if p.name == _INDEX_FILENAME or not p.is_file():
                continue
            st = p.stat()
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= cap:
            return
        files.sort()
        file_to_url = {ent.get("file"): u for u, ent in index.items()
                       if isinstance(ent, dict) and ent.get("file")}
        for _, size, p in files:
            if total <= cap:
                break
            try:
                p.unlink()
                total -= size
                u = file_to_url.get(p.name)
                if u is not None:
                    index.pop(u, None)
                logger.info("media_localize: quota evicted %s", p.name)
            except OSError:
                pass
    except Exception:
        logger.exception("media_localize: quota enforcement failed")


async def _fetch_dedup(url: str, media_dir: Path, s: MediaLocalizeSettings):
    """De-duplicate concurrent downloads of the same (dir, url) across messages.

    The shared download is shielded: one caller's budget timeout cancels only
    its own wait — the download finishes detached and still writes its
    content-addressed file for the other caller (and the index).
    """
    key = (str(media_dir), url)
    task = _IN_FLIGHT.get(key)
    if task is None:
        task = asyncio.ensure_future(_download_one(url, media_dir, s))
        _IN_FLIGHT[key] = task
        task.add_done_callback(lambda _t: _IN_FLIGHT.pop(key, None))
    return await asyncio.shield(task)


async def _resolve_url_map(urls: list[str], workspace_root: str,
                           s: MediaLocalizeSettings) -> dict[str, str]:
    """url → workspace-relative `media/<hash><ext>` for every localizable URL."""
    media_dir = Path(workspace_root) / MEDIA_DIRNAME
    mapping: dict[str, str] = {}
    try:
        await asyncio.to_thread(media_dir.mkdir, parents=True, exist_ok=True)
    except Exception:
        logger.exception("media_localize: cannot create %s", media_dir)
        return {}
    async with _INDEX_LOCK:
        index = await asyncio.to_thread(_load_index, media_dir)
    now = time.time()
    todo: list[str] = []
    for url in urls:
        ent = index.get(url)
        if not isinstance(ent, dict):
            todo.append(url)
        elif ent.get("error"):
            try:
                ent_ts = float(ent.get("ts") or 0)
            except (TypeError, ValueError):
                ent_ts = 0.0
            if now - ent_ts >= s.neg_cache_ttl_seconds:
                todo.append(url)  # retry after TTL
        elif (media_dir / ent.get("file", "")).is_file():
            mapping[url] = f"{MEDIA_DIRNAME}/{ent['file']}"
        else:
            todo.append(url)  # index entry but file evicted → re-download
    if not todo:
        return mapping
    sem = asyncio.Semaphore(4)

    async def _one(u: str):
        async with sem:
            return u, await _fetch_dedup(u, media_dir, s)

    tasks = [asyncio.ensure_future(_one(u)) for u in todo]
    done, pending = await asyncio.wait(tasks, timeout=s.total_timeout_seconds)
    for t in pending:
        t.cancel()
    new_entries: dict[str, dict] = {}
    for t in done:
        try:
            u, ent = t.result()
        except asyncio.CancelledError:
            continue
        except Exception:
            continue
        if ent:
            new_entries[u] = ent
        else:
            new_entries[u] = {"error": True, "ts": now}
    if new_entries:
        async with _INDEX_LOCK:
            index = await asyncio.to_thread(_load_index, media_dir)
            index.update(new_entries)
            _enforce_quota(media_dir, index, s)
            try:
                await asyncio.to_thread(
                    (media_dir / _INDEX_FILENAME).write_text,
                    json.dumps(index, ensure_ascii=False, indent=1), "utf-8")
            except Exception:
                logger.exception("media_localize: index write failed")
        for u, ent in new_entries.items():
            if not ent.get("error") and (media_dir / ent.get("file", "")).is_file():
                mapping[u] = f"{MEDIA_DIRNAME}/{ent['file']}"
    return mapping


async def localize_media_in_text(content: str, workspace_root: str,
                                 settings: MediaLocalizeSettings | None = None) -> str:
    if not content:
        return content
    s = settings or settings_from_config()
    if not s.enabled:
        return content
    urls = extract_remote_media_urls(content)[: s.max_per_message]
    if not urls:
        return content
    try:
        mapping = await _resolve_url_map(urls, workspace_root, s)
    except Exception:
        logger.exception("media_localize: resolve failed")
        return content
    return _apply_mapping(content, mapping)


async def localize_message_payload(content: str, tool_results_json: str | None,
                                   workspace_root: str,
                                   settings: MediaLocalizeSettings | None = None,
                                   ) -> tuple[str, str | None]:
    """Localize media in the persisted message content AND the tool_results JSON.

    Only displayable text fields are rewritten: top-level content,
    `content_segments[]`, and `display_sequence[]` items of type "text".
    Reasoning steps, tool cards and attachments keep their original URLs.
    """
    s = settings or settings_from_config()
    if not s.enabled:
        return content, tool_results_json
    tr_obj = None
    if tool_results_json:
        try:
            tr_obj = json.loads(tool_results_json)
        except Exception:
            tr_obj = None
    texts: list[str] = [content or ""]
    if tr_obj:
        for seg in tr_obj.get("content_segments") or []:
            if isinstance(seg, str):
                texts.append(seg)
        for item in tr_obj.get("display_sequence") or []:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("content"), str):
                texts.append(item["content"])
    urls: list[str] = []
    seen: set[str] = set()
    for t in texts:
        for u in extract_remote_media_urls(t):
            if u not in seen:
                seen.add(u)
                urls.append(u)
    if not urls:
        return content, tool_results_json
    try:
        mapping = await _resolve_url_map(urls[: s.max_per_message], workspace_root, s)
    except Exception:
        logger.exception("media_localize: resolve failed")
        return content, tool_results_json
    if not mapping:
        return content, tool_results_json
    new_content = _apply_mapping(content, mapping) if content else content
    new_tr_json = tool_results_json
    if tr_obj:
        if isinstance(tr_obj.get("content_segments"), list):
            tr_obj["content_segments"] = [
                _apply_mapping(seg, mapping) if isinstance(seg, str) else seg
                for seg in tr_obj["content_segments"]
            ]
        if isinstance(tr_obj.get("display_sequence"), list):
            for item in tr_obj["display_sequence"]:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("content"), str):
                    item["content"] = _apply_mapping(item["content"], mapping)
        new_tr_json = json.dumps(tr_obj, ensure_ascii=False)
    return new_content, new_tr_json
