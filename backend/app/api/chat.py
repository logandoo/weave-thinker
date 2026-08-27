# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse
import asyncio
import json
import logging
import os
import time
from datetime import datetime

import re as _re

from app.db.database import get_db, Message, Conversation, User, Assistant, ChatSession
from app.db.database import AsyncSessionLocal
from app.schemas.chat import ChatRequest
from app.services.agent_service import AgentService, should_use_custom_model, _load_identity_memory_context
from app.services.query_sanitizer import extract_search_query_and_note_context
from app.services.title_generator import TitleGeneratorService
from app.services.workspace_service import ensure_user_workspace
from app.services.agent_loop import AgentLoop, _strip_dsml_all, _strip_leading_orphan_punct
from app.services.citation_ledger import CitationLedger, normalize_url
from app.services.deathmatch_service import DeathmatchManager, MARKER_RE
from app.tools.skill_tools import build_skills_system_prompt, resolve_skill, is_system_skill
from app.tools.registry import registry
from app.services.provider_router import build_thinking_extra_body
from app.services.stream_buffer import stream_buffer_manager
from app.services.part_events import PartTranslator
from app.services.pre_tool_gate import PreToolGate
from app.core.deps import get_current_user
from app.core.config import get_config

# Patterns for stripping model-generated XML tags that leak into content
_THINK_RE = _re.compile(r'<think>.*?</think>', _re.DOTALL)
_TOOL_CALL_RE = _re.compile(r'<tool_call>.*?</tool_call>', _re.DOTALL)
_PARTIAL_OPEN_RE = _re.compile(r'<(?:think|tool_call)[^>]*$')  # incomplete opening tag at end
_PARTIAL_CLOSE_RE = _re.compile(r'^[^<]*</(?:think|tool_call)>')  # incomplete closing tag at start

logger = logging.getLogger(__name__)

# §5.4 澄清 fire-and-forget 任务强引用集（防 GC 中断在途任务）
_clarification_tasks: set = set()
router = APIRouter(prefix="/api/chat", tags=["chat"])

# Strong references to background persistence tasks spawned when a client
# disconnects mid-stream. Without this, the event loop might GC them before
# they finish saving the partial reply + title.
# Phase 5.5: Also tracked via shared_state for multi-instance awareness.
_INTERRUPT_BG_TASKS: set = set()
_DETACHED_AGENT_TASKS: dict[str, asyncio.Task] = {}
agent_service = AgentService()

from app.services.shared_state import shared_state

from app.services.active_agent_registry import ActiveAgentRegistry
_agent_registry = ActiveAgentRegistry.get_instance()


def _track_bg_task(task: asyncio.Task, conv_id: str, task_type: str) -> None:
    """Register a background task in both the local strong-reference set and
    the shared state backend (for cross-worker awareness in multi-instance)."""
    _INTERRUPT_BG_TASKS.add(task)
    task.add_done_callback(_INTERRUPT_BG_TASKS.discard)
    if shared_state.is_db_enabled:
        async def _cleanup():
            try:
                await shared_state.untrack_background_task(conv_id)
            except Exception:
                pass
        task.add_done_callback(lambda _t: asyncio.ensure_future(_cleanup()))


def _track_detached_task(task: asyncio.Task, conv_id: str) -> None:
    """Register a detached agent task for cross-worker awareness."""
    _DETACHED_AGENT_TASKS[conv_id] = task
    def _on_done(t):
        _DETACHED_AGENT_TASKS.pop(conv_id, None)
    task.add_done_callback(_on_done)
    if shared_state.is_db_enabled:
        async def _register():
            await shared_state.track_background_task(conv_id, {
                "type": "detached_agent",
                "worker": shared_state.worker_id,
            })
        asyncio.ensure_future(_register())
        async def _cleanup():
            try:
                await shared_state.untrack_background_task(conv_id)
            except Exception:
                pass
        task.add_done_callback(lambda _t: asyncio.ensure_future(_cleanup()))


async def _persist_deathmatch_failure(
    conversation_id: str,
    deathmatch_mgr,
    reason: str,
    message: str,
    *,
    expected_agent_state=None,
) -> None:
    """Persist a deathmatch goal-loop failure as a visible paused terminal
    state so the user sees WHY the loop stopped and can resume.

    conv 6b0faf81: the detached agent task crashed mid-loop (no terminal
    message, deathmatch_status left "active"); only the GET reconcile
    flipped it to "paused" ~18 minutes later with no explanation. Any
    exception/cancel in the goal loop must leave an explicit, visible,
    resumable state instead of a silent zombie.

    Safety guards (A4.9 review 2026-08-07):
    - NEVER clobber a deliberate terminal state: done / human_gate /
      partial_complete carry structured semantics (gate reports, partial
      summaries, deliverables) that a later exception in the same task's
      finalization must not overwrite.
    - When ``expected_agent_state`` is given, only persist if the registry
      still holds OUR run (identity check). A superseded task (its slot
      re-reserved by a newer /chat/stream) must not flip the conversation to
      paused after the new run already set it active.
    """
    if deathmatch_mgr is None:
        return
    try:
        if expected_agent_state is not None:
            from app.services.active_agent_registry import ActiveAgentRegistry
            _reg = ActiveAgentRegistry.get_instance()
            _cur = await _reg.get(conversation_id)
            if _cur is not expected_agent_state:
                logger.info(
                    "Deathmatch failure persist skipped: agent state superseded for %s",
                    conversation_id,
                )
                return
        async with AsyncSessionLocal() as _fail_db:
            _fail_conv = await _fail_db.get(Conversation, conversation_id)
            if _fail_conv is None or not _fail_conv.deathmatch_mode:
                return
            if _fail_conv.deathmatch_status in ("done", "human_gate", "partial_complete"):
                return
            _fail_conv.deathmatch_status = "paused"
            _fail_conv.deathmatch_reason = reason
            _fail_conv.updated_at = datetime.utcnow()
            _fail_msg = Message(
                conversation_id=conversation_id,
                role="assistant",
                content=message,
            )
            _fail_db.add(_fail_msg)
            await _fail_db.commit()
            logger.info(
                "Deathmatch failure persisted (conv=%s): status=paused reason=%s",
                conversation_id, reason[:120],
            )
    except Exception:
        logger.exception("Failed to persist deathmatch failure state for %s", conversation_id)


_DIGEST_ARCHIVE_RE = _re.compile(r"【全文存档】(\S+)")
_DIGEST_TEXT_RE = _re.compile(r"<digest>([\s\S]*?)</digest>")


def _recover_digested_search(envelope: str) -> str | None:
    """Return the original web_search result JSON from the digest archive.

    The tool-digest layer replaces a large web_search result with a
    ``<tool-digest>`` envelope and writes the ORIGINAL result to a lossless
    archive file (【全文存档】path). Recovering it lets the persisted message
    keep its rounds/results/tool card — without this, the digest envelope
    made ``_transform_tool_loop_results`` skip the entry entirely and the
    search evidence silently vanished from the final answer (conv 149ce886,
    2026-08-01: web_search ran per tool_trace, persisted results=0).
    """
    m = _DIGEST_ARCHIVE_RE.search(envelope)
    if not m:
        return None
    fpath = m.group(1).strip()
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and ("results" in data or "formatted" in data):
            return json.dumps(data, ensure_ascii=False)
    except (OSError, ValueError):
        pass
    return None


def _is_scratch_path(path: str | None) -> bool:
    """True when *path* lives under a per-call scratch/task_XXXX temp dir.

    Scratch dirs are by design intermediate per-call work dirs (the
    execute_code prompt tells the agent each call gets a fresh one). Files
    there are drafts/byproducts — never user-facing deliverables, so they
    must not surface as download cards (conv 2b36fb09 showed a scratch
    draft docx attached next to the real deliverable).

    Anchored on the exact ``scratch/task_`` layout created by
    code_execution (workspace/scratch/task_XXXX) so a user directory that
    merely contains a segment named ``scratch`` is never misclassified.
    """
    if not path:
        return False
    return "/scratch/task_" in str(path).replace("\\", "/")


def _collect_download_attachments(tool_results_accumulated: list[dict]) -> list[dict]:
    """Compute the download-card set from accumulated tool results.

    Single source of truth shared by the persist-time transform
    (``_transform_tool_loop_results``) and the live SSE path. The LIVE card
    set must always equal what the persisted message will show, otherwise the
    streamed count is wrong (conv fbf5779b, 2026-08-06: the SSE path streamed
    only the CURRENT tool call's ``generated_files`` while the frontend
    REPLACED the whole set on each event — the live count jumped 1→1→1→1→4
    and previously shown docx cards vanished mid-stream).

    Rules (mirror the transform contract):
    - scratch/task_XXXX intermediates never surface as cards;
    - an explicit ``provide_file`` set wins over byproduct auto-collection;
    - missing ``type`` is inferred from the file extension;
    - same-name files deduplicate keeping the largest reported size.
    """
    auto_attachments: list[dict] = []       # byproduct collection (execute_code/terminal/pdf_export)
    provided_attachments: list[dict] = []   # explicit provide_file set
    for tr in tool_results_accumulated:
        name = tr.get("name", "")
        raw_result = tr.get("result", "")
        if name in ("code_execution", "execute_code", "terminal"):
            try:
                parsed = json.loads(raw_result) if raw_result else {}
                gen_files = parsed.get("generated_files", [])
                if gen_files:
                    for gf in gen_files:
                        if not isinstance(gf, dict) or _is_scratch_path(gf.get("path")):
                            continue
                        auto_attachments.append(gf)
            except (json.JSONDecodeError, TypeError):
                pass
        elif name == "pdf_export":
            try:
                parsed = json.loads(raw_result) if raw_result else {}
                if parsed.get("success") and parsed.get("file_path"):
                    if not _is_scratch_path(parsed.get("file_path")):
                        auto_attachments.append({
                            "name": parsed.get("filename") or os.path.basename(parsed["file_path"]),
                            "path": parsed["file_path"],
                            "size": parsed.get("size") or 0,
                            "type": "pdf",
                        })
            except (json.JSONDecodeError, TypeError):
                pass
        elif name == "provide_file":
            try:
                parsed = json.loads(raw_result) if raw_result else {}
                gen_files = parsed.get("generated_files", [])
                if gen_files:
                    for gf in gen_files:
                        if isinstance(gf, dict):
                            provided_attachments.append(gf)
            except (json.JSONDecodeError, TypeError):
                pass

    # Explicit provide_file attachments are the agent's chosen deliverable set
    # and win over byproduct auto-collection (conv 2b36fb09: a scratch draft
    # + debug stats leaked in next to the final docx). When the agent did not
    # call provide_file, auto-collection remains the fallback.
    all_attachments = provided_attachments if provided_attachments else auto_attachments
    if not all_attachments:
        return []

    # Infer file type from extension when missing so download cards
    # always show the correct type badge.
    _ext_type_map = {
        ".pdf": "pdf", ".docx": "word", ".doc": "word",
        ".pptx": "ppt", ".ppt": "ppt",
        ".xlsx": "excel", ".xls": "excel", ".csv": "csv",
        ".txt": "text", ".md": "markdown", ".json": "json",
        ".py": "python", ".js": "javascript",
        ".html": "html", ".css": "css",
        ".png": "image", ".jpg": "image", ".jpeg": "image",
        ".gif": "image", ".webp": "image", ".bmp": "image", ".svg": "image",
        ".zip": "archive", ".gz": "archive", ".tar": "archive",
        ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".ogg": "audio",
        ".flac": "audio", ".aac": "audio",
        ".mp4": "video", ".webm": "video", ".mov": "video", ".m4v": "video", ".avi": "video",
    }
    for att in all_attachments:
        if not att.get("type") or att.get("type") == "file":
            fname = att.get("name") or att.get("filename") or ""
            ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            att["type"] = _ext_type_map.get(ext, "file")

    # Deduplicate attachments by name to avoid duplicate download cards.
    # For files that share the same name (e.g. a document being appended to
    # across multiple execute_code calls), keep the largest reported size
    # so the final, complete file is the one offered for download.
    seen_names: dict[str, dict] = {}
    for att in all_attachments:
        fname = att.get("name") or att.get("filename") or ""
        if not fname:
            continue
        existing = seen_names.get(fname)
        if existing is None or (att.get("size") or 0) > (existing.get("size") or 0):
            seen_names[fname] = att
    return list(seen_names.values())


def _derive_title_from_url(url: str) -> str:
    """Best-effort display title for a source URL without a page title
    (browser digest envelopes carry no <title>): the last path segment,
    else the network location."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        seg = [s for s in (parsed.path or "").split("/") if s]
        if seg:
            return seg[-1]
        return parsed.netloc or url
    except Exception:
        return url


_BROWSER_URL_RE = _re.compile(r"https?://[^\s<>\"'\]\)）】]+")
_DIGEST_HEADER_RE = _re.compile(r"<tool-digest>([\s\S]*?)<digest>", _re.IGNORECASE)
# conv 3b58af5b wave-2: real browser digests carry their fetched targets in a
# `## 来源` section INSIDE the digest body as backticked URLs with titles
# (`https://host/path` (标题)) — that declaration is deterministic evidence,
# unlike arbitrary links elsewhere in the body.
_DIGEST_SOURCES_RE = _re.compile(r"##\s*来源\s*\n([\s\S]*?)(?=\n##\s|\Z)")
_SOURCE_LINE_RE = _re.compile(r"`(https?://[^\s`]+)`(?:\s*[（(]([^）)]*)[）)])?")


def _extract_browser_sources(raw_result: str):
    """Yield (url, title, snippet) citation candidates from a browser tool
    result. conv 3b58af5b (R3a): browser-fetched pages are real sources the
    model reads and reasons over — they must join results[] like web_search
    hits or the persisted answer has dangling [N] markers with no badge to
    resolve them.

    Two result shapes exist:
      - JSON: {"pages": [{url, title, text}, ...]} — full fidelity.
      - <tool-digest> envelope: URLs live in the header block (来源/目标
        lines); the digest body may embed arbitrary content links, so ONLY
        the header is scanned. No title/text available → derived title,
        empty snippet.
    """
    raw = raw_result or ""
    if not raw:
        return
    if raw.startswith("<tool-digest>"):
        m = _DIGEST_HEADER_RE.search(raw)
        if not m:
            # A4.9 Minor-1: malformed envelope (no <digest> marker) — the
            # body may embed arbitrary content links, so scanning it would
            # violate the header-only contract. Register nothing.
            return
        seen: set[str] = set()

        def _collect_url(u: str, title: str = ""):
            u = u.rstrip(".,;、，。")
            key = normalize_url(u)
            if not u or key in seen:
                return
            seen.add(key)
            collected.append((u, title or _derive_title_from_url(u), ""))

        collected: list[tuple[str, str, str]] = []
        # A) envelope header 【目标】 line(s) — single/multi declared targets.
        for line in m.group(1).splitlines():
            if "【目标】" in line:
                for u in _BROWSER_URL_RE.findall(line):
                    _collect_url(u)
        # B) digest body `## 来源` section — the summarizer's own declared
        # fetched targets. Two observed layouts: backticked URLs with
        # optional (标题), and plain `目标：<url>` lines (older summaries).
        sm = _DIGEST_SOURCES_RE.search(raw[m.end():])
        if sm:
            for line in sm.group(1).splitlines():
                if "`http" in line:
                    for bm in _SOURCE_LINE_RE.finditer(line):
                        _collect_url(bm.group(1), (bm.group(2) or "").strip())
                elif "目标" in line:
                    for u in _BROWSER_URL_RE.findall(line):
                        _collect_url(u)
        yield from collected
        return
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    pages = parsed.get("pages") if isinstance(parsed, dict) else None
    if not isinstance(pages, list):
        return
    for p in pages:
        if not isinstance(p, dict):
            continue
        url = p.get("url") or ""
        if not url.startswith("http"):
            continue
        # A4.9 Minor-2: errored pages ({url, error} entries) are dead links —
        # they must not become citation badge sources.
        if p.get("error"):
            continue
        yield (
            url,
            (p.get("title") or "").strip() or _derive_title_from_url(url),
            ((p.get("text") or "").strip())[:300],
        )


def _transform_tool_loop_results(
    tool_results_accumulated: list[dict],
    search_queries_used: list[str] | None = None,
    queries_by_call: dict | None = None,
) -> str | None:
    """Convert raw agent-loop ToolCallResult array into legacy ToolResultsData
    format that the frontend MessageBubble understands.

    ``search_failed`` semantic (M-2, conv 8f27d43e lineage): True ONLY when
    EVERY web_search call in this answer failed (no qualified hits at all).
    Partial failures — some rounds empty, later rounds successful — keep
    ``search_failed=False`` so the frontend renders the real results and
    citation badges; the old per-round "any empty round sets the flag"
    semantic made the frontend discard successful partial results entirely.

    Returns JSON string or None.
    """
    if not tool_results_accumulated:
        return None

    results_list: list[dict] = []       # SearchResult[]
    rounds_list: list[dict] = []        # SearchRound[]
    agent_steps_list: list[dict] = []   # AgentStep[]
    search_failed = False
    # Citation-ledger alignment: the agent loop renumbers web_search results
    # with turn-GLOBAL ids (CitationLedger, deduped by normalized URL in
    # first-seen order). The flattened results[] here MUST dedupe with the
    # same normalization so `results[id-1]` resolves exactly the URL the
    # model's [id] cites — cross-round collisions were the failure mode when
    # each round numbered from 1 (conv 149ce886 lineage).
    _cite_seen: set = set()

    search_round_idx = 0
    _search_call_count = 0
    _search_success_count = 0
    for tr in tool_results_accumulated:
        name = tr.get("name", "")
        call_id = tr.get("call_id", "")
        raw_result = tr.get("result", "")
        is_error = tr.get("error", False)

        if name == "web_search":
            # A <tool-digest> envelope is not structured search JSON — the
            # digester replaced the raw result in the model context. The
            # ORIGINAL result is preserved lossless at the archive path in
            # the envelope (【全文存档】); recover it so the persisted message
            # keeps its rounds/results/tool card. Otherwise the entire search
            # evidence silently vanishes from the final answer (conv
            # 149ce886, 2026-08-01).
            if raw_result.startswith("<tool-digest>"):
                recovered = _recover_digested_search(raw_result)
                if recovered:
                    raw_result = recovered
                else:
                    # Archive unrecoverable: keep the tool card with the
                    # digest text so the search call stays visible.
                    digest_m = _DIGEST_TEXT_RE.search(raw_result)
                    search_round_idx += 1
                    agent_steps_list.append({
                        "name": call_id or f"web_search_{search_round_idx}",
                        "title": f"联网搜索 (第{search_round_idx}轮)",
                        "content": digest_m.group(1).strip() if digest_m else "(结果已摘要化，全文已存档)",
                        "step_type": "tool",
                    })
                    continue
            try:
                parsed = json.loads(raw_result) if raw_result else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}

            search_round_idx += 1
            hits = parsed.get("results", [])
            _search_call_count += 1
            if isinstance(hits, list) and len(hits) > 0:
                _search_success_count += 1
                # Per-call queries (conv a3cfb421 2026-08-09): each round must
                # carry ITS OWN query list, not the last call's. The legacy
                # `search_queries_used` holds only the FINAL web_search call's
                # queries (chat.py overwrites it on every tool_call event), so
                # all rounds previously displayed the same (last) queries.
                _call_queries = (queries_by_call or {}).get(call_id) or []
                _round_queries = (
                    _call_queries
                    if _call_queries
                    else search_queries_used
                    or [f"web_search_{search_round_idx}"]
                )
                rounds_list.append({
                    "round": search_round_idx,
                    "queries": _round_queries,
                    "qualified": True,
                    "cn_en_count": len(hits),
                    "total_count": len(hits),
                })
                for h in hits:
                    if isinstance(h, dict) and h.get("url"):
                        # Dedupe by normalized URL (fragment/trailing-slash
                        # insensitive) to mirror the ledger's id assignment —
                        # position in this list IS the citation id minus 1.
                        _cite_key = normalize_url(h["url"])
                        if _cite_key in _cite_seen:
                            continue
                        _cite_seen.add(_cite_key)
                        snippet = (h.get("snippet") or "")[:300]
                        results_list.append({
                            "id": len(results_list) + 1,
                            "title": (h.get("title") or "")[:200],
                            "url": h.get("url", ""),
                            "snippet": snippet,
                            "published_date": h.get("published_date"),
                        })
            # M-2: empty rounds no longer set search_failed per-round — the
            # flag is derived at the end from the call/success counters.

            agent_steps_list.append({
                "name": call_id or f"web_search_{search_round_idx}",
                "title": f"联网搜索 (第{search_round_idx}轮)",
                "content": parsed.get("formatted") or "未检索到可用网页结果。",
                "step_type": "tool",
            })

        elif name == "browser":
            agent_steps_list.append({
                "name": call_id or "browser",
                "title": "浏览网页",
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

        elif name in ("context7_resolve_library_id", "context7_query_docs"):
            step_title = "查找库文档ID" if "resolve" in name else "查询库文档"
            agent_steps_list.append({
                "name": call_id or name,
                "title": step_title,
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

        elif name == "code_execution" or name == "execute_code":
            agent_steps_list.append({
                "name": call_id or "code_execution",
                "title": "代码执行",
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

        elif name == "terminal":
            agent_steps_list.append({
                "name": call_id or "terminal",
                "title": "终端命令",
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

        elif name == "pdf_export":
            agent_steps_list.append({
                "name": call_id or "pdf_export",
                "title": "导出 PDF",
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

        elif name == "provide_file":
            agent_steps_list.append({
                "name": call_id or "provide_file",
                "title": "提供文件",
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

        elif name == "memory":
            agent_steps_list.append({
                "name": call_id or "memory",
                "title": "记忆操作",
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

        else:
            # Generic tool step
            agent_steps_list.append({
                "name": call_id or name,
                "title": f"工具调用 · {name}",
                "content": raw_result if raw_result else "(无内容)",
                "step_type": "tool",
            })

    search_failed = _search_call_count > 0 and _search_success_count == 0

    # conv 3b58af5b (R3a): browser-fetched pages are real sources — append
    # them AFTER the web_search hits so the live ledger's web_search-only
    # numbering (ids 1..K) stays aligned with results[0..K-1]; browser ids
    # continue K+1..M. Dedupe mirrors the ledger (same normalize_url).
    for tr in tool_results_accumulated:
        if tr.get("name") != "browser" or tr.get("error"):
            continue
        for url, title, snippet in _extract_browser_sources(tr.get("result") or ""):
            _cite_key = normalize_url(url)
            if _cite_key in _cite_seen:
                continue
            _cite_seen.add(_cite_key)
            results_list.append({
                "id": len(results_list) + 1,
                "title": title[:200],
                "url": url,
                "snippet": snippet,
                "published_date": None,
            })

    payload: dict = {
        "rounds": rounds_list,
        "results": results_list,
        "search_failed": search_failed,
        "agent_steps": agent_steps_list,
    }
    # Shared with the live SSE path so the streamed download-card set always
    # equals the persisted one (conv fbf5779b, 2026-08-06).
    attachments = _collect_download_attachments(tool_results_accumulated)
    if attachments:
        payload["attachments"] = attachments

    return json.dumps(payload, ensure_ascii=False)


def _build_content_fallback(joined_content: str, tool_results_json: str | None) -> str:
    if joined_content:
        return joined_content
    if not tool_results_json:
        return ""
    try:
        tr_obj = json.loads(tool_results_json)
        results = tr_obj.get("results") or []
        parts = []
        for r in results:
            result_text = r.get("result") or ""
            if not isinstance(result_text, str) or not result_text.strip():
                continue
            first_line = result_text.strip().split("\n")[0][:300]
            if first_line:
                parts.append(first_line)
        if parts:
            return "\n\n".join(parts)
    except Exception:
        pass
    return ""


async def _sanitize_cited_content(
    content: str,
    tool_results_json: str | None,
) -> tuple[str, str | None]:
    """Remove [N] citations that reference no real fetched source before the
    content is persisted. Grounded-citations port (hermes-agent verify step):
    every [N] in the final answer must exist in the turn's citation ledger
    (registered from the actual web_search tool results). Fabricated markers
    are removed with LLM-judged enumeration protection (see
    CitationLedger.sanitize_texts — [3]个要点 must survive).

    Sanitizes EVERY persisted text surface: the message ``content`` AND the
    ``content_segments``/``display_sequence`` text items inside
    ``tool_results_json`` (the frontend renders the display_sequence, so a
    content-only sanitize left dead [25]-style markers visible — conv
    8629bdfe). Returns ``(content, tool_results_json)`` — both sanitized.

    Operates on the TRANSFORMED tool_results JSON (whose results[] is
    deduped by normalized URL in ledger order), so id alignment is
    guaranteed: results[id-1] == the URL the model's [id] cites.
    """
    if not content and not tool_results_json:
        return content, tool_results_json
    try:
        tr_obj = None
        if tool_results_json:
            tr_obj = json.loads(tool_results_json)
        results = (tr_obj or {}).get("results") or []
        if not isinstance(results, list):
            results = []
        # conv 3b58af5b (R3b): the old guard early-returned whenever results
        # was empty — exactly the search-engine-failed case, leaving dangling
        # [N] markers the frontend cannot resolve. The docstring contract is
        # "strip [N] when any search ran": activity is now detected from
        # agent_steps too, and an EMPTY ledger proceeds so every [N] marker
        # goes through the enumeration disambiguation and true citations are
        # stripped. A turn with no tool activity at all stays untouched.
        has_activity = bool(results) or bool((tr_obj or {}).get("agent_steps"))
        if not has_activity:
            return content, tool_results_json
        ledger = CitationLedger()
        for r in results:
            if isinstance(r, dict) and r.get("url"):
                ledger.register(
                    r["url"],
                    title=r.get("title") or "",
                    snippet=r.get("snippet") or "",
                )

        # Collect every text surface in one shot, keeping the SAME order as
        # the write-back loop below (content first, then content_segments,
        # then display_sequence text items).
        surfaces: list[str] = [content]
        if tr_obj:
            _segs = tr_obj.get("content_segments")
            if isinstance(_segs, list):
                surfaces.extend(s for s in _segs if isinstance(s, str))
            _ds = tr_obj.get("display_sequence")
            if isinstance(_ds, list):
                surfaces.extend(
                    it.get("content") for it in _ds
                    if isinstance(it, dict) and it.get("type") == "text"
                    and isinstance(it.get("content"), str)
                )
        sanitized = await ledger.sanitize_texts(surfaces)
        new_content = sanitized[0]
        new_tr = tool_results_json
        if tr_obj is not None:
            _changed = sanitized[0] != surfaces[0]
            _idx = 1
            _segs = tr_obj.get("content_segments")
            if isinstance(_segs, list):
                for i, s in enumerate(_segs):
                    if isinstance(s, str):
                        if sanitized[_idx] != s:
                            _segs[i] = sanitized[_idx]
                            _changed = True
                        _idx += 1
            _ds = tr_obj.get("display_sequence")
            if isinstance(_ds, list):
                for it in _ds:
                    if isinstance(it, dict) and it.get("type") == "text" and isinstance(it.get("content"), str):
                        if sanitized[_idx] != it["content"]:
                            it["content"] = sanitized[_idx]
                            _changed = True
                        _idx += 1
            if _changed:
                new_tr = json.dumps(tr_obj, ensure_ascii=False)
        return new_content, new_tr
    except Exception:
        logger.exception("citation sanitize failed (keeping original)")
        return content, tool_results_json


async def _localize_media_for_persist(
    content: str | None,
    tool_results_json: str | None,
    user_id: str,
    username: str | None,
) -> tuple[str | None, str | None]:
    """Download remote media the agent decided to display into the user's
    workspace (content-addressed sha256) and rewrite to workspace-relative
    paths, so persisted rendering serves the local copy instead of
    re-fetching the remote link. Best-effort: any failure keeps the
    original URLs. Runs on a separate session because ensure_user_workspace
    commits internally (expire_on_commit=False keeps current_user attrs
    readable, but a fresh session keeps transactions isolated)."""
    try:
        from app.services.media_localizer import localize_message_payload
        async with AsyncSessionLocal() as ml_db:
            ws = await ensure_user_workspace(ml_db, user_id, username)
            return await localize_message_payload(content, tool_results_json, ws.root_path)
    except Exception:
        logger.exception("media localization failed; keeping original URLs")
        return content, tool_results_json


def _strip_leading_orphan_colon(text: str) -> str:
    """Strip exactly ONE leading orphan colon (U+FF1A / ASCII ':').

    Defense for a rare provider glitch (1/427 msgs, conv e7d51dcb 2026-08-19:
    qwen3.8_27b@vLLM thinking-xhigh opened the final answer with a bare '：'
    after a dangling label in its thinking). NO legitimate answer starts with
    a bare colon, and no pipeline stage can introduce one — so stripping one
    leading orphan colon is total-loss-free. A bare-colon-only string is kept
    as-is (never empty out a message); a second colon is content, not an
    orphan. Leading whitespace is preserved.
    """
    if not text:
        return text
    i = 0
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if not (i < len(text) and text[i] in "：:"):
        return text
    rest = text[i + 1:]
    if not rest.strip():
        return text
    # Full-width '：' is stripped unconditionally (no legitimate sentence-
    # initial use). ASCII ':' has legitimate heads (emoticons :)/:-),
    # scope resolution ::) — only strip it when the next non-whitespace
    # char rules those out (A4.9 review M4).
    if text[i] == ":":
        j = i + 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] in ")(:-":
            return text
    return text[:i] + rest


def _compose_terminal_content(
    joined_content: str,
    loop_error: str | None,
    had_tool_activity: bool,
) -> tuple[str, bool]:
    """Single guarantee point for answer completeness at persistence time.

    Returns ``(content, substituted)``. An empty answer is NEVER persisted
    silently: when the turn errored OR did real work (tool calls/results)
    but produced no visible content, substitute a visible failure message so
    every downstream save path (finalize / self-save / buffer replay / legacy
    resume) persists something the user can actually see and act on —
    conv daa19eac (51-char preamble) and conv 38ce8810 (content='' with 51KB
    of tool_results) are the production instances of the unguarded family.

    ``(empty, no error, no tools)`` stays empty: a genuinely content-free turn
    is skipped by the callers' own guards, not masked.
    """
    if joined_content.strip():
        # conv e7d51dcb: the model can open the final answer with a dangling
        # orphan colon (thinking→answer boundary glitch) — strip it at the
        # single persistence guarantee point so EVERY save path
        # (finalize / self-save / buffer replay / legacy resume) is clean.
        return _strip_leading_orphan_punct(_strip_leading_orphan_colon(joined_content)), False
    if loop_error:
        _err_short = " ".join(str(loop_error).split())[:200]
        return (
            f"回答生成失败：{_err_short}\n\n"
            "请重新发送消息重试；若多次失败，请简化问题或稍后再试。"
        ), True
    if had_tool_activity:
        return (
            "回答生成失败：模型在工具调用后未能产出有效回答。\n\n"
            "请重新发送消息重试；若多次失败，请简化问题或稍后再试。"
        ), True
    return "", False


async def _load_conversation_messages(db: AsyncSession, conversation_id: str, limit: int = None):
    stmt = select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


async def _trim_messages_for_regeneration(
    db: AsyncSession,
    *,
    conversation_id: str,
    regenerate_from_message_id: str,
):
    conversation_messages = await _load_conversation_messages(db, conversation_id)
    target_index = next(
        (index for index, message in enumerate(conversation_messages) if message.id == regenerate_from_message_id),
        -1,
    )
    if target_index == -1:
        raise HTTPException(status_code=404, detail="待重新生成的消息不存在")

    target_message = conversation_messages[target_index]
    if target_message.role != "assistant":
        raise HTTPException(status_code=400, detail="只能重新生成助手消息")

    for message in reversed(conversation_messages[target_index:]):
        await db.delete(message)
    await db.commit()
    return conversation_messages[:target_index]


async def _trim_messages_for_edit(
    db: AsyncSession,
    *,
    conversation_id: str,
    edit_message_id: str,
):
    """Delete the target user message and all subsequent messages for edit-and-resend."""
    conversation_messages = await _load_conversation_messages(db, conversation_id)
    target_index = next(
        (index for index, message in enumerate(conversation_messages) if message.id == edit_message_id),
        -1,
    )
    if target_index == -1:
        raise HTTPException(status_code=404, detail="待编辑的消息不存在")

    target_message = conversation_messages[target_index]
    if target_message.role != "user":
        raise HTTPException(status_code=400, detail="只能编辑用户消息")

    for message in reversed(conversation_messages[target_index:]):
        await db.delete(message)
    await db.commit()
    return conversation_messages[:target_index]


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    req: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation_id = request.conversation_id
    assistant_id = request.assistant_id

    # Capture user identity scalars UP FRONT: later code paths (media
    # localization in self-save / interrupted-save) run after the request
    # session may have rolled back, which EXPIRES current_user (rollback
    # expiry ignores expire_on_commit=False) — attribute access there would
    # raise DetachedInstanceError and silently drop the message being saved.
    _user_id_local: str = current_user.id
    _username_local: str | None = current_user.username

    chat_session = ChatSession(
        user_id=current_user.id,
        conversation_id=None,
        assistant_id=assistant_id,
        started_at=datetime.utcnow(),
        message_count=0
    )
    db.add(chat_session)
    await db.flush()

    assistant = None
    if assistant_id:
        result = await db.execute(
            select(Assistant).where(
                Assistant.id == assistant_id,
                Assistant.user_id == current_user.id
            )
        )
        assistant = result.scalar_one_or_none()

    if conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == current_user.id
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation and not assistant and conversation.assistant_id:
            fallback_result = await db.execute(
                select(Assistant).where(
                    Assistant.id == conversation.assistant_id,
                    Assistant.user_id == current_user.id
                )
            )
            assistant = fallback_result.scalar_one_or_none()
            if assistant:
                assistant_id = conversation.assistant_id
        if not conversation:
            conversation = Conversation(
                user_id=current_user.id,
                title="新对话",
                assistant_id=assistant_id
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
            conversation_id = conversation.id
        elif assistant_id and conversation.assistant_id is None:
            conversation.assistant_id = assistant_id
            await db.commit()
    else:
        conversation = Conversation(
            user_id=current_user.id,
            title="新对话",
            assistant_id=assistant_id
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        conversation_id = conversation.id

    chat_session.conversation_id = conversation_id
    await db.commit()

    # 4.8 session lock: at most one active agent per conversation. A second
    # POST while an agent is still running would spawn a concurrent loop
    # writing to the same conversation (double-saved messages, polluted
    # stream state, racing DB writes). reserve() atomically claims the slot
    # BEFORE the user message insert — closing the check-then-act race
    # between guard and agent start. Cross-worker liveness is covered by the
    # buffer check (stale/dead-worker running-claims are distrusted there).
    _reserved_agent_state = await _agent_registry.reserve(conversation_id)
    _agent_busy = _reserved_agent_state is None
    if not _agent_busy:
        try:
            _existing_buf = await stream_buffer_manager.get_buffer(conversation_id, current_user.id)
            _agent_busy = _existing_buf is not None and _existing_buf.is_running
        except Exception:
            _agent_busy = False
        if _agent_busy:
            # Buffer says a run is alive (possibly on another worker) — the
            # local reservation we just took must be released. Identity-checked
            # release that never touches shared state (the live run's
            # cross-worker snapshots belong to the other worker).
            if _reserved_agent_state is not None:
                await _agent_registry.release_reservation(conversation_id, _reserved_agent_state)
                _reserved_agent_state = None
    if _agent_busy:
        logger.info("Rejected concurrent /stream for conversation %s (agent already running)", conversation_id)

        async def _busy_events():
            yield {
                "event": "error",
                "data": json.dumps({
                    "error": "该会话已有正在进行的回答，请等待其完成或先停止。",
                    "code": "conversation_busy",
                    "conversation_id": conversation_id,
                }, ensure_ascii=False),
            }

        return EventSourceResponse(_busy_events())

    if request.regenerate_from_message_id:
        conversation_messages = await _trim_messages_for_regeneration(
            db,
            conversation_id=conversation_id,
            regenerate_from_message_id=request.regenerate_from_message_id,
        )
    elif request.edit_message_id:
        conversation_messages = await _trim_messages_for_edit(
            db,
            conversation_id=conversation_id,
            edit_message_id=request.edit_message_id,
        )
        # Save the new user message
        user_message = Message(
            conversation_id=conversation_id,
            role="user",
            content=request.messages[-1].content
        )
        conversation.updated_at = datetime.utcnow()
        db.add(user_message)
        await db.commit()
        conversation_messages = await _load_conversation_messages(db, conversation_id, limit=200)
    else:
        user_message = Message(
            conversation_id=conversation_id,
            role="user",
            content=request.messages[-1].content
        )
        conversation.updated_at = datetime.utcnow()
        db.add(user_message)
        await db.commit()
        conversation_messages = await _load_conversation_messages(db, conversation_id, limit=200)

    latest_user_query = next(
        (message.content for message in reversed(conversation_messages) if message.role == "user"),
        request.messages[-1].content if request.messages else "",
    )

    # §5.4 澄清检测：Stage 1 关键词命中（请求路径零 LLM）→ fire-and-forget
    # 异步 LLM 验证+应用修正（独立会话；本轮回答可能仍基于旧记忆，下轮即修正）
    try:
        from app.core.config import get_config as _gc_clar
        from app.services.memory_runtime_state import memory_runtime_enabled as _mem_rt_clar
        _cfg_clar = _gc_clar()
        if _mem_rt_clar(_cfg_clar):
            from app.services.memory_clarification_service import detect_signal
            if detect_signal(latest_user_query or ""):
                _clar_uid = current_user.id
                _clar_conv = conversation_id
                _clar_msg_id = getattr(user_message, "id", None)
                _clar_text = latest_user_query

                async def _clarification_bg():
                    try:
                        from app.db.database import AsyncSessionLocal as _ASL
                        from app.services.memory_clarification_service import process_clarification
                        async with _ASL() as _cdb:
                            await process_clarification(
                                _cdb, _clar_uid, _clar_text,
                                conversation_id=_clar_conv, message_id=_clar_msg_id)
                    except Exception:
                        logger.debug("background clarification failed", exc_info=True)

                _ct = asyncio.create_task(_clarification_bg())
                _clarification_tasks.add(_ct)
                _ct.add_done_callback(_clarification_tasks.discard)
    except Exception:
        logger.debug("clarification detect_signal failed", exc_info=True)

    # DEATHMATCH lifecycle handling
    if request.deathmatch_action:
        dm_mgr = DeathmatchManager(conversation)
        if request.deathmatch_action == "start":
            # If a previous deathmatch round has completed, preserve its compressed
            # summary before deciding whether to start a fresh grilling phase.
            if conversation.deathmatch_status == "done" and not conversation.deathmatch_context_summary:
                await dm_mgr.compress_conversation_context(db)
            # When the user explicitly starts deathmatch after a completed round,
            # use the sub-agent intent classifier to decide whether this is a new
            # task (NEW_ROUND/CLARIFY) or just a discussion of the previous result
            # (DISCUSS). This prevents re-entering grilling for messages like
            # "你觉得写得怎么样？".
            if (
                conversation.deathmatch_status == "done"
                and conversation.deathmatch_context_summary
            ):
                intent = await dm_mgr.classify_intent(latest_user_query, db)
                if intent == "NEW_ROUND":
                    await dm_mgr.compress_conversation_context(db)
                    dm_mgr.activate_grilling()
                elif intent == "CLARIFY":
                    dm_mgr.activate_grilling()
                # DISCUSS: keep deathmatch off; normal mode will carry the summary.
            else:
                # Starting deathmatch from normal mode (or a stale state). Capture
                # the existing conversation context first so the grilling phase can
                # see what was discussed before the mode switch.
                if not conversation.deathmatch_context_summary:
                    await dm_mgr.compress_conversation_context(db)
                dm_mgr.activate_grilling()
            await db.commit()
        elif request.deathmatch_action == "stop":
            # Compress context before exiting deathmatch so the conversation can
            # continue in normal mode with background context.
            if conversation.deathmatch_status in ("active", "done"):
                await dm_mgr.compress_conversation_context(db)
            dm_mgr.deactivate()
            await db.commit()
        elif request.deathmatch_action == "pause":
            dm_mgr.pause()
            await db.commit()
        elif request.deathmatch_action == "resume":
            dm_mgr.resume()
            await db.commit()
    elif request.deathmatch_mode:
        # Frontend says deathmatch is ON but no explicit action — ensure grilling
        # is active.  If the conversation is not already in "grilling" or
        # "active" goal-loop phase, force it into grilling so the user always
        # gets asked clarification questions first.
        if conversation.deathmatch_status == "active":
            # Goal loop is active but idle (between runs). A user message here
            # may be a side question about the results rather than a push to
            # continue. Classify intent: DISCUSS → pause the loop so the agent
            # just answers the question once instead of re-igniting the goal
            # loop; NEW_ROUND/CLARIFY → keep the loop active (current behavior).
            # Bare resume commands ("继续" etc.) never pause — they mean
            # "keep going", especially when the loop is zombie-active (the
            # runner died and the status just hasn't been reconciled yet).
            # The grilling-complete kickoff message ("目标已明确，请开始执行：…",
            # auto-sent by the frontend right after grilling completes) must
            # never be classified either — it IS the goal-loop start signal;
            # classifying it as DISCUSS paused the loop with 0 turns (conv
            # 51d74833).
            dm_mgr = DeathmatchManager(conversation)
            _q = latest_user_query.strip().lower()
            _is_resume_cmd = len(_q) <= 12 and any(
                h in _q for h in ("继续", "接着", "resume", "continue", "go on")
            )
            _is_goal_kickoff = latest_user_query.strip().startswith("目标已明确，请开始执行")
            if not _is_resume_cmd and not _is_goal_kickoff:
                intent = await dm_mgr.classify_intent(latest_user_query, db)
                if intent == "DISCUSS":
                    dm_mgr.pause(reason="user-discussion")
                    await db.commit()
                    logger.info(
                        "Deathmatch paused for discussion message in conversation %s",
                        conversation_id,
                    )
        elif conversation.deathmatch_status not in ("grilling", "active"):
            dm_mgr = DeathmatchManager(conversation)
            if conversation.deathmatch_status in ("paused", "human_gate"):
                # "paused"/"human_gate" = the deathmatch was interrupted (stop /
                # disconnect / discussion pause / wall-time or stall gate) but
                # the goal is NOT achieved.
                # Short resume commands ("继续" etc.) always resume the loop.
                # Anything else is classified: DISCUSS questions are answered
                # once while staying gated; NEW_ROUND/CLARIFY resumes.
                _q = latest_user_query.strip().lower()
                _is_resume_cmd = len(_q) <= 12 and any(
                    h in _q for h in ("继续", "接着", "resume", "continue", "go on")
                )
                if _is_resume_cmd:
                    dm_mgr.resume()
                    logger.info(
                        "Deathmatch resumed from paused for conversation %s (goal loop re-entered)",
                        conversation_id,
                    )
                else:
                    _intent = await dm_mgr.classify_intent(latest_user_query, db)
                    if _intent == "DISCUSS":
                        logger.info(
                            "Deathmatch stays gated (paused/human_gate) for discussion message in conversation %s",
                            conversation_id,
                        )
                    else:
                        dm_mgr.resume()
                        logger.info(
                            "Deathmatch resumed from paused for conversation %s (intent=%s)",
                            conversation_id, _intent,
                        )
            elif conversation.deathmatch_status == "partial_complete":
                # "partial_complete" = the goal is mostly done but stalled.
                # User sending a message means "continue pushing toward the goal".
                # Resume WITHOUT resetting the stall counter so repeated stalls
                # escalate to human_gate.
                dm_mgr.resume_from_partial()
                logger.info(
                    "Deathmatch resumed from partial_complete for conversation %s (stall count preserved)",
                    conversation_id,
                )
            elif conversation.deathmatch_context_summary:
                # "done" or other non-active state with a prior summary: use the
                # sub-agent intent classifier to decide new round vs discuss.
                intent = await dm_mgr.classify_intent(latest_user_query, db)
                if intent == "NEW_ROUND":
                    await dm_mgr.compress_conversation_context(db)
                    dm_mgr.activate_grilling()
                elif intent == "CLARIFY":
                    dm_mgr.activate_grilling()
                else:
                    # DISCUSS: keep deathmatch off, normal mode will carry summary.
                    pass
            else:
                # No prior summary yet: capture the existing normal-mode context
                # before entering grilling so the first round can reference it.
                await dm_mgr.compress_conversation_context(db)
                dm_mgr.activate_grilling()
            await db.commit()
    # Safety net: if conversation has deathmatch_mode=True from a previous
    # session but the status is stale (not grilling/active/paused/done), reactivate
    # grilling so the user always gets the clarification flow.  We exclude "done"
    # because a completed round is an intentional terminal state; re-entry must be
    # decided by the intent classifier above, not forced here.
    if (
        conversation.deathmatch_mode
        and conversation.deathmatch_status not in ("grilling", "active", "paused", "done", "human_gate", "partial_complete")
    ):
        dm_mgr = DeathmatchManager(conversation)
        dm_mgr.activate_grilling()
        await db.commit()
        # Reload conversation_messages after deathmatch state change
        conversation_messages = await _load_conversation_messages(db, conversation_id, limit=200)

    llm_service = agent_service.create_llm_service(assistant)

    # P0 (2026-08-21, user requirement): coordinator/audit/aux LLM calls
    # inherit the assistant's model settings. Custom-model assistants get
    # their main client as coordinator; non-custom keep the operator's
    # global aux keys. The task-local aux override below makes every in-loop
    # AuxiliaryClient (classifiers/judges/error classify/compression) follow
    # the same rule — contextvars are task-scoped, so no explicit reset is
    # needed when the request task ends.
    coordinator_llm = agent_service.resolve_aux_model_context(assistant, llm_service)
    if assistant is not None and should_use_custom_model(assistant):
        from app.services.auxiliary_client import set_aux_llm_override
        set_aux_llm_override(llm_service)

    import re as _re
    skill_match = _re.search(r'\[skill:([a-zA-Z0-9_-]+)\]\s*(.*?)(?=\[skill:|$)', latest_user_query, _re.DOTALL)
    skill_content = None
    skill_files = None
    if skill_match:
        skill_name = skill_match.group(1)
        skill_args = skill_match.group(2) or ''
        try:
            # Unified dual-source resolver: system skills (backend/skills/*)
            # first, then user DB skills. See loop_improve.md Phase 1.3.
            resolved = await resolve_skill(skill_name, current_user)
            if resolved:
                skill_content = resolved["content"]
                if resolved.get("files"):
                    skill_files = resolved["files"]
                skill_marker = f"[skill:{skill_name}] {skill_args}".strip()
                replacement = f"[使用技能: {skill_name}]\n{skill_args}".strip()
                latest_user_query = latest_user_query.replace(skill_marker, replacement, 1)
                conversation_messages[-1].content = latest_user_query
            else:
                logger.warning(f"Skill '{skill_name}' not found for user {current_user.id}")
        except Exception as e:
            logger.error(f"Failed to load skill '{skill_name}': {e}")

    # Deterministic skill injection for uploaded files: when the user message
    # carries a [file-ref:...] marker and no explicit [skill:...] marker was
    # resolved, inject the file_parsing skill so the parsing workflow rules
    # are always present for file tasks (no reliance on model self-trigger).
    if not skill_content and _re.search(r'\[file-ref:', latest_user_query):
        try:
            resolved = await resolve_skill("file_parsing", current_user)
            if resolved:
                skill_content = resolved["content"]
                if resolved.get("files"):
                    skill_files = resolved["files"]
        except Exception as e:
            logger.error(f"Failed to load file_parsing skill: {e}")

    search_query, note_context_for_search = extract_search_query_and_note_context(latest_user_query)
    message_count = 1

    async def _ensure_title(refreshed_conversation, full_response: str) -> str | None:
        """Generate a title for a brand-new conversation and persist it.
        Always returns a non-default title when called on a '新对话' row, using
        the fallback derivation if the LLM call fails. Safe to call multiple
        times (a no-op when the title has already been updated)."""
        if not refreshed_conversation or refreshed_conversation.title != "新对话":
            return refreshed_conversation.title if refreshed_conversation else None
        try:
            # P0 / A4.9 Minor-7: provider-config assistants (qwen3.8_vllm
            # with an empty row-level URL) resolve through the main client —
            # the title LLM must not fall back to global deepseek.
            title_generator = TitleGeneratorService(
                **agent_service.title_generator_kwargs(assistant, llm_service),
                provider_type=getattr(assistant, "provider_type", "deepseek") or "deepseek",
            )
            _title_query = search_query or latest_user_query or ""
            generated_title: str | None = None
            try:
                generated_title = await title_generator.generate_title(
                    user_query=_title_query,
                    assistant_response=full_response or "",
                )
            except Exception:
                import traceback as _tb
                _tb.print_exc()
                generated_title = None

            if not generated_title:
                generated_title = await title_generator.get_fallback_title(_title_query)
            # Extra safety: never leave "新对话" if we got here.
            if not generated_title or generated_title == "新对话":
                _src = (_title_query or full_response or "").strip()
                if _src:
                    generated_title = _src[:16]
                else:
                    generated_title = "未命名会话"

            refreshed_conversation.title = generated_title
            await db.commit()
            return generated_title
        except Exception:
            import traceback as _tb
            _tb.print_exc()
            return refreshed_conversation.title if refreshed_conversation else None

    async def _finalize_and_emit_done(
        *,
        assistant_message_id: str | None,
        final_content: str,
        final_tool_results_json: str | None,
        fallback: bool = False,
    ):
        """Unified terminal step: regenerate title if needed, push done event
        with the FINAL title (so the frontend never has to wait for a separate
        title_update event to display it), then optionally emit title_update
        for existing client-side listeners. Used by every exit path."""
        refreshed = None
        try:
            conv_result = await db.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            refreshed = conv_result.scalar_one_or_none()
        except Exception:
            refreshed = conversation

        final_title = await _ensure_title(refreshed, final_content)

        # Compress tool_results for the SSE done event. The full payload is
        # already persisted in the DB; a huge string (> 30 KB with browser
        # page content) would cause frontend JSON.parse to fail silently and
        # skip the done event entirely, leaving no assistant bubble rendered.
        # Instead of truncating (which breaks JSON), strip the large
        # agent_steps and keep only results/rounds needed for citation rendering.
        safe_tool_results = final_tool_results_json
        if final_tool_results_json and len(final_tool_results_json) > 8192:
            try:
                tr_obj = json.loads(final_tool_results_json)
                slim = {k: v for k, v in tr_obj.items() if k != "agent_steps"}
                safe_tool_results = json.dumps(slim, ensure_ascii=False)
                if len(safe_tool_results) > 8192:
                    slim["results"] = slim.get("results", [])[:20]
                    safe_tool_results = json.dumps(slim, ensure_ascii=False)
                if len(safe_tool_results) > 8192:
                    for r in slim.get("results", []):
                        if isinstance(r.get("result"), str) and len(r["result"]) > 500:
                            r["result"] = r["result"][:500] + "...[truncated]"
                    safe_tool_results = json.dumps(slim, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                safe_tool_results = json.dumps({"results": [], "error": "tool_results too large, stripped for SSE"}, ensure_ascii=False)

        payload = {
            "conversation_id": conversation_id,
            "message_id": assistant_message_id,
            "title": final_title,
            "tool_results": safe_tool_results,
            "done": True,
        }
        if fallback:
            payload["fallback"] = True
        yield {"event": "done", "data": json.dumps(payload)}

        # Keep the legacy title_update event for clients that still listen
        # to it — but it is purely cosmetic now, since `done` already
        # carries the final title.
        try:
            if not await req.is_disconnected():
                yield {"event": "message", "data": json.dumps({
                    "title_update": {
                        "conversation_id": conversation_id,
                        "title": final_title,
                    },
                })}
        except Exception:
            pass

    async def event_generator():
        nonlocal message_count
        full_response = ""
        full_reasoning = ""
        tool_results_json = None
        # PHASE 2B: serialized OpenAI-style tool_calls list for persistence.
        tool_calls_json: str | None = None
        _cap_tool_calls: str | None = None
        agent_steps: list[dict] = []
        thinking_history: list[dict] = []
        sub_agent_outputs: dict[str, dict[str, str]] = {}
        task_plan_data: dict | None = None
        response_saved = False
        client_disconnected = False
        detached_agent_task: asyncio.Task | None = None
        sse_cancelled_flag: asyncio.Event | None = None

        def _spawn_interrupted_save(_buf_ref=None):
            """Schedule a detached background task that writes the partial
            assistant reply + regenerated title using a FRESH DB session.
            Used when the request's own `db` session becomes unusable (e.g.
            client disconnected mid-commit → asyncpg connection closed)."""
            interrupted_content = full_response.strip() or (
                "回答在思考过程中被中断。已保存中间状态，您可以重新发送或追问以继续。"
            )
            _cap_reasoning = full_reasoning
            _cap_tool_results = tool_results_json
            _cap_conv_id = conversation_id
            _cap_already_saved = response_saved
            _cap_search_query = search_query
            _cap_latest_user_query = latest_user_query
            _cap_assistant = assistant
            _cap_llm_service = llm_service

            async def _job():
                try:
                    async with AsyncSessionLocal() as bg_db:
                        conv_row = (await bg_db.execute(
                            select(Conversation).where(Conversation.id == _cap_conv_id)
                        )).scalar_one_or_none()
                        if conv_row is None:
                            if _buf_ref:
                                try:
                                    await _buf_ref.mark_complete(None)
                                except Exception:
                                    pass
                            return
                        if not _cap_already_saved:
                            _loc_content, _loc_tr = await _localize_media_for_persist(
                                interrupted_content, _cap_tool_results,
                                _user_id_local, _username_local,
                            )
                            msg = Message(
                                conversation_id=_cap_conv_id,
                                role="assistant",
                                content=_loc_content,
                                reasoning_content=_cap_reasoning if _cap_reasoning else None,
                                tool_results=_loc_tr,
                                tool_calls=_cap_tool_calls,
                            )
                            bg_db.add(msg)
                            conv_row.updated_at = datetime.utcnow()
                            await bg_db.commit()
                            await bg_db.refresh(msg)
                            if _buf_ref:
                                try:
                                    await _buf_ref.mark_complete(str(msg.id))
                                except Exception:
                                    pass
                        if conv_row.title == "新对话":
                            title_generator = TitleGeneratorService(
                                **agent_service.title_generator_kwargs(_cap_assistant, _cap_llm_service),
                                provider_type=getattr(_cap_assistant, "provider_type", "deepseek") or "deepseek",
                            )
                            _tq = _cap_search_query or _cap_latest_user_query or ""
                            gt = None
                            try:
                                gt = await title_generator.generate_title(
                                    user_query=_tq,
                                    assistant_response=interrupted_content,
                                )
                            except Exception:
                                import traceback as _tb
                                _tb.print_exc()
                            if not gt:
                                gt = await title_generator.get_fallback_title(_tq)
                            if not gt or gt == "新对话":
                                _src = (_tq or interrupted_content or "").strip()
                                gt = _src[:16] if _src else "未命名会话"
                            conv_row.title = gt
                            await bg_db.commit()
                except Exception:
                    import traceback as _tb
                    _tb.print_exc()

            try:
                loop = asyncio.get_running_loop()
                _bg = loop.create_task(_job())
                _track_bg_task(_bg, _cap_conv_id, "interrupt_save")
            except Exception:
                import traceback as _tb
                _tb.print_exc()


        async def _run_tool_loop():
            """Phase 1.2: New tool-calling agent loop — LLM decides which tools to call.

            The agent loop runs in a detached asyncio.Task so it completes
            even if the SSE connection is cancelled by sse-starlette.
            Events are bridged via an asyncio.Queue.
            """
            nonlocal full_response, full_reasoning, tool_results_json, message_count, response_saved, client_disconnected

            # 4.8: refresh the reservation at setup milestones so the
            # provisional reaper never kills a legitimately-slow setup
            # (deathmatch classify + coordinator pre-pass LLM calls).
            if _reserved_agent_state is not None and _reserved_agent_state.provisional:
                _reserved_agent_state.reserved_at = time.time()

            import app.tools

            from app.services.memory_service import build_shared_agent_context
            from app.core.config import get_config as _gc
            from app.db.database import AsyncSessionLocal as _AsyncSessionLocal
            _cfg = _gc()

            async def _memory_ctx() -> str | None:
                """§5.2 工程要求 1：召回管线与 coordinator/准备工作并发（取 max 非 sum）。
                独立 DB 会话（AsyncSession 不可并发共用）；失败回退 None 走旧方案。"""
                from app.services.memory_runtime_state import memory_runtime_enabled as _mem_rt
                if not (_mem_rt(_cfg) and _cfg.memory.get("retrieval_enabled")):
                    return None
                try:
                    from app.services import memory_retrieval_service
                    async with _AsyncSessionLocal() as mem_db:
                        return await memory_retrieval_service.retrieve_and_build_context(
                            mem_db, current_user.id, conversation_messages)
                except Exception:
                    logger.exception("New memory retrieval failed, falling back")
                    return None

            # --- 与 sys_prompt 无关的准备工作前移（§5.2 工程要求 1：coordinator 与召回并发）---
            messages_for_model = [{"role": "system", "content": ""}]  # placeholder，sys_prompt 就绪后回填
            _config = get_config()
            context_limit = _config.agent_conversation_context_limit
            conv_msgs = conversation_messages
            if len(conv_msgs) > context_limit:
                conv_msgs = conv_msgs[-context_limit:]
            # PHASE 2A + 2B: rebuild structured OpenAI history so multi-turn
            # conversations show the model genuine assistant.tool_calls / tool
            # message pairs instead of opaque content text. Without this the
            # model loses the "I called tools and got results" pattern around
            # turn 5-6 and stops invoking tools (same failure mode hermes-agent
            # PR #3528 fixed).
            from app.services.agent_service import _sanitize_history_content
            from app.services.tool_history import (
                rebuild_structured_history,
                sanitize_api_messages,
            )
            for msg in conv_msgs:
                if msg.role == "assistant" and getattr(msg, "tool_calls", None):
                    messages_for_model.extend(
                        rebuild_structured_history(
                            role="assistant",
                            content=_sanitize_history_content(msg.content),
                            tool_calls_json=msg.tool_calls,
                            tool_results_json=getattr(msg, "tool_results", None),
                            reasoning_content=getattr(msg, "reasoning_content", None),
                        )
                    )
                else:
                    rebuilt = {
                        "role": msg.role,
                        "content": _sanitize_history_content(msg.content),
                    }
                    # DeepSeek thinking mode: assistant reasoning must round-trip
                    # on tool-call turns (400 otherwise); harmless elsewhere.
                    if msg.role == "assistant":
                        _rc = getattr(msg, "reasoning_content", None)
                        if _rc:
                            rebuilt["reasoning_content"] = _rc
                    messages_for_model.append(rebuilt)
            messages_for_model = sanitize_api_messages(messages_for_model)

            # PHASE 3: build the iteration (sub-task) LLM. If the assistant
            # has subtask_custom_* fields populated, spin up a separate
            # cheaper client for tool-calling iterations; otherwise reuse
            # the main llm_service. AgentLoop already forces iteration
            # extra_body to disable thinking either way.
            iteration_llm, iteration_provider_type = (
                agent_service.create_iteration_llm_service(assistant, main_llm=llm_service)
            )
            if iteration_llm is not llm_service:
                logger.info(
                    "Phase 3: subtask LLM enabled (provider=%s)", iteration_provider_type
                )

            # DEATHMATCH: Wire up deathmatch manager if conversation is in deathmatch mode
            _deathmatch_mgr: DeathmatchManager | None = None
            _blocked_tools: set[str] = set()
            if conversation.deathmatch_mode:
                _deathmatch_mgr = DeathmatchManager(conversation)
                # P0: judge/verifier inherit the assistant's model client.
                _deathmatch_mgr.set_assistant_llm(llm_service)
                # Block background_task during deathmatch — all work must
                # stay inline in the same conversation stream. Background
                # tasks create new conversations and scatter results.
                _blocked_tools.add("background_task")
                # Inject compressed context summary from previous rounds.
                messages_for_model = _deathmatch_mgr.build_context_messages(messages_for_model)
                # PEVR: Inject plan directive when goal loop is active so the
                # agent knows the plan from the very first turn — not just
                # from continuation prompts on turn 2+.
                if _deathmatch_mgr.is_goal_active:
                    plan_directive = _deathmatch_mgr.build_plan_directive()
                    if plan_directive:
                        # Insert as a user message right after the system prompt
                        # so the agent sees it before any conversation history.
                        messages_for_model.insert(1, {
                            "role": "user",
                            "content": plan_directive,
                        })

            identity_context = await _load_identity_memory_context(current_user.id)

            async def _permission_request_callback(
                conversation_id: str,
                tool_name: str,
                description: str,
                details: dict,
            ) -> bool:
                from app.services.permission_manager import request_permission
                request_id = f"perm_{conversation_id[:8]}_{tool_name}_{int(asyncio.get_event_loop().time() * 1000)}"
                perm_data = {
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "description": description,
                    "details": details,
                }
                perm_event = {"permission_request": perm_data}

                state = await _agent_registry.get(conversation_id)
                if state:
                    try:
                        await state.broadcast(perm_event)
                    except Exception:
                        logger.exception("Failed to broadcast permission request")

                buf = await stream_buffer_manager.get_buffer_no_auth(conversation_id)
                if buf:
                    try:
                        await buf.append(perm_event)
                    except Exception:
                        logger.exception("Failed to append permission request to stream buffer")

                return await request_permission(
                    conversation_id=conversation_id,
                    tool_name=tool_name,
                    description=description,
                    details=details,
                    timeout=120.0,
                    request_id=request_id,
                )

            # 遵循词 canary: the marker is injected into the system prompt
            # below (_build_system_prompt) AND handed to the loop so only this
            # path ever checks for it (worker/scheduler/delegate never do).
            from app.services.canary_marker import make_canary, strip_canary_streaming
            _canary_marker = make_canary(str(conversation_id)) if _config.agent_canary_enabled else None

            # Qwen3.8(Local): per-mode sampling param sets from the assistant
            # (thinking vs non-thinking), NULL fields fall back to the
            # model-card defaults (modelscope.cn/models/Qwen/Qwen3.8-27B-FP8).
            from app.services.provider_router import (
                QWEN38_VLLM_THINKING_DEFAULTS,
                QWEN38_VLLM_NON_THINKING_DEFAULTS,
            )
            _thinking_sampling: dict = {}
            _non_thinking_sampling: dict = {}
            _preserve_thinking: bool | None = None
            if (getattr(assistant, "provider_type", "") or "") == "qwen3.8_vllm":
                _thinking_sampling = {
                    k: getattr(assistant, f"thinking_{k}", None)
                    for k in QWEN38_VLLM_THINKING_DEFAULTS
                }
                _thinking_sampling = {
                    k: (v if v is not None else QWEN38_VLLM_THINKING_DEFAULTS[k])
                    for k, v in _thinking_sampling.items()
                }
                _non_thinking_sampling = {
                    k: getattr(assistant, k, None)
                    for k in QWEN38_VLLM_NON_THINKING_DEFAULTS
                }
                _non_thinking_sampling = {
                    k: (v if v is not None else QWEN38_VLLM_NON_THINKING_DEFAULTS[k])
                    for k, v in _non_thinking_sampling.items()
                }
                # M1 (A4.9): NULL column must keep the "default ON" contract
                _preserve_thinking = (
                    getattr(assistant, "preserve_thinking", True)
                    if getattr(assistant, "preserve_thinking", True) is not None
                    else True
                )

            agent_loop = AgentLoop(
                llm=llm_service,
                tool_schemas=None,
                max_iterations=_config.agent_tool_loop_max_iterations,
                workspace_path="",  # gather 后回填（workspace 未就绪；run 前必被覆盖）
                provider_type=getattr(assistant, "provider_type", "deepseek") or "deepseek",
                enable_reasoning=request.enable_reasoning,
                reasoning_effort=request.reasoning_effort,
                thinking_budget=request.thinking_budget,
                thinking_sampling=_thinking_sampling or None,
                non_thinking_sampling=_non_thinking_sampling or None,
                preserve_thinking=_preserve_thinking,
                iteration_llm=iteration_llm,
                iteration_provider_type=iteration_provider_type,
                blocked_tools=_blocked_tools if _blocked_tools else None,
                permission_callback=_permission_request_callback,
                session_factory=AsyncSessionLocal,
                identity_context=identity_context,
                coordinator_llm=coordinator_llm,
                # Must be passed here (not assigned post-init): the deathmatch
                # iteration-budget override is evaluated inside __init__.
                deathmatch_manager=_deathmatch_mgr,
                enable_compression=_config.agent_compression_enabled,
                canary_marker=_canary_marker,
                # 二期工具集按需发送：浏览器交互子工具（navigate/click/type 等）
                # 首轮不发送，模型调用 browser 入口后由 AgentLoop 自动追加
                # （只增不减，会话内最多破坏一次前缀缓存；省 ~1.5k tokens/轮）。
                lazy_tools=[
                    n for n in registry.get_tool_names_for_toolset("web")
                    if n not in ("browser", "pdf_export", "web_search")
                ],
            )

            # §5.2 工程要求 1：coordinator 与召回管线/工作区准备/skills 组装真并发
            # （合并等待取 max 非 sum）。coordinator 只读 user/assistant 消息，
            # 不依赖 system prompt，可提前启动；失败回退 _ALO_UNSET 由 run 内串行重试。
            from app.services.agent_loop import _UNSET as _ALO_UNSET

            async def _coord_prepass():
                try:
                    return await agent_loop._coordinate(list(messages_for_model))
                except Exception:
                    logger.exception("coordinator pre-pass failed; in-loop retry")
                    return _ALO_UNSET

            # Setup heartbeat: this gather includes the coordinator LLM
            # pre-pass and can legitimately exceed 30-60s. Before the buffer /
            # registry / agent keepalive exist, total silence would trip the
            # frontend's 30s stall watchdog and kill a healthy setup (and with
            # it the whole run — the detached task does not exist yet). Emit
            # raw SSE pings until the gather settles.
            from app.services.sse_heartbeat import HEARTBEAT as _HB, aiter_with_heartbeat as _aiter_hb

            async def _workspace_ctx():
                """Request-independent workspace fetch.

                A client disconnect during setup (tab switch / refresh /
                network blip) must NOT kill the run — the SSE abort path
                re-drives the tool loop in a detached task AFTER the request
                session is closed, so setup can no longer depend on the
                request-scoped ``db`` (conv b078987b, 2026-08-03: 20s-in
                abort → dead-setup release → user message left unanswered).
                """
                async with _AsyncSessionLocal() as _ws_db:
                    return await ensure_user_workspace(
                        _ws_db, current_user.id, current_user.username,
                    )

            _setup_result = None
            async for _hb_item in _aiter_hb(asyncio.gather(
                _memory_ctx(),
                _workspace_ctx(),
                build_skills_system_prompt(current_user),
                _coord_prepass(),
            ), interval=10.0):
                if _hb_item is _HB:
                    # Keep the provisional reservation alive: a live setup
                    # (SSE pings flowing) must never be reaped by the 180s
                    # provisional TTL — expiry would let a newer request
                    # re-reserve the slot and fail THIS run with
                    # conversation_superseded (user-visible "已被新的请求取代"
                    # error on a healthy stream). Identity-checked: only
                    # refresh while the slot is still ours.
                    try:
                        if (
                            _reserved_agent_state is not None
                            and _agent_registry.get_local(conversation_id) is _reserved_agent_state
                            and _reserved_agent_state.provisional
                        ):
                            _reserved_agent_state.reserved_at = time.time()
                    except Exception:
                        pass
                    yield {"event": "ping", "data": json.dumps({"ping": True})}
                else:
                    _setup_result = _hb_item
            memory_context, workspace, skills_sys_prompt, coord_result = _setup_result
            agent_loop.workspace_path = str(workspace.root_path)

            from types import SimpleNamespace
            if memory_context is not None and memory_context.strip():
                shared_context = SimpleNamespace(
                    agent_state=None,
                    memory_summary=memory_context or '',
                    dream_summary='',
                    memory_entries=[],
                )
            else:
                async with _AsyncSessionLocal() as _sctx_db:
                    shared_context = await build_shared_agent_context(_sctx_db, current_user.id)

            sys_prompt = await agent_service._build_system_prompt(
                assistant=assistant,
                shared_context=shared_context,
                workspace=workspace,
                user=current_user,
                user_skill_content=skill_content,
                skills_system_prompt=skills_sys_prompt,
                skill_files=skill_files,
                identity_context=identity_context,
                conversation_id=str(conversation_id),
                deathmatch_mode=bool(conversation.deathmatch_mode),
            )
            messages_for_model[0] = {"role": "system", "content": sys_prompt}

            # 上下文 token 用量：估算「系统提示词 + 历史 + 工具 schema」的完整
            # 请求量，随流发给前端显示（头部徽章）。压缩事件携带 before/after
            # 对比并在压缩后刷新本值。CJK 感知估算（与压缩决策同源）。
            _ctx_window = _config.agent_compression_context_length
            _ctx_tokens = 0
            try:
                from app.services.context_compressor import estimate_request_tokens_rough
                _ctx_tokens = estimate_request_tokens_rough(
                    messages_for_model, tools=agent_loop.tool_schemas
                )
            except Exception:
                logger.exception("Failed to estimate context tokens")

            _agent_queue: asyncio.Queue = asyncio.Queue(maxsize=256)
            _agent_done = object()
            nonlocal sse_cancelled_flag
            sse_cancelled_flag = asyncio.Event()
            _sse_cancelled = sse_cancelled_flag

            # 4.8 session lock handoff: adopt the slot reserved in the
            # endpoint (identity-checked). If the registry no longer holds
            # OUR state object, a newer request re-reserved the slot — this
            # run is stale and must abort instead of spawning a second loop.
            # Refresh first: a slow-but-alive setup must not abort itself.
            if _reserved_agent_state is not None:
                if _reserved_agent_state.provisional:
                    _reserved_agent_state.reserved_at = time.time()
                _agent_state = await _agent_registry.adopt_reservation(
                    conversation_id, _reserved_agent_state
                )
                if _agent_state is None:
                    yield {
                        "event": "error",
                        "data": json.dumps({
                            "error": "该会话的回答已被新的请求取代，已取消重复请求。",
                            "code": "conversation_superseded",
                            # A newer request legitimately owns the conversation —
                            # the frontend must end this stale stream SILENTLY
                            # (no error bubble) and resync from the DB instead
                            # of alarming the user.
                            "silent": True,
                        }, ensure_ascii=False),
                    }
                    return
            else:
                _agent_state = await _agent_registry.register(conversation_id)
            # Cancel any previous detached agent task for this conversation to
            # prevent stale tasks from emitting permission requests alongside the
            # new one.
            old_task = _DETACHED_AGENT_TASKS.pop(conversation_id, None)
            if old_task and not old_task.done():
                old_task.cancel()
                logger.info("Cancelled stale detached agent task for conversation %s", conversation_id)
            _agent_queue = await _agent_state.subscribe()

            _TOOL_STEP_TITLES: dict[str, str] = {
                "web_search": "联网检索结果",
                "browser": "浏览网页结果",
                "execute_code": "代码执行结果",
                "memory": "记忆操作",
                "delegate_task": "子任务执行结果",
                "context7_resolve_library_id": "查找库文档ID",
                "context7_query_docs": "查询库文档",
            }

            _cap_conversation_id = conversation_id
            _cap_assistant = assistant
            _cap_search_query = search_query
            _cap_latest_user_query = latest_user_query
            _cap_user_id = current_user.id
            _cap_username = current_user.username
            _cap_workspace_path = str(workspace.root_path)
            _cap_llm_service = llm_service

            async def _generate_title_bg(bg_db, conv_row, full_content: str) -> str | None:
                """Run TitleGeneratorService for background save paths so that
                conversations whose original SSE was interrupted still get a
                proper LLM-generated title (instead of just slicing the query)."""
                if not conv_row or conv_row.title != "新对话":
                    return conv_row.title if conv_row else None
                _tq = _cap_search_query or _cap_latest_user_query or ""
                generated_title: str | None = None
                try:
                    tg = TitleGeneratorService(
                        **agent_service.title_generator_kwargs(_cap_assistant, _cap_llm_service),
                        provider_type=getattr(_cap_assistant, "provider_type", "deepseek") or "deepseek",
                    )
                    try:
                        generated_title = await tg.generate_title(
                            user_query=_tq,
                            assistant_response=full_content or "",
                        )
                    except Exception:
                        logger.exception("Background title LLM call failed; using fallback")
                        generated_title = None
                    if not generated_title:
                        generated_title = await tg.get_fallback_title(_tq)
                except Exception:
                    logger.exception("Background TitleGeneratorService init failed")
                if not generated_title or generated_title == "新对话":
                    src = (_tq or full_content or "").strip()
                    generated_title = src[:16] if src else "未命名会话"
                conv_row.title = generated_title
                await bg_db.commit()
                return generated_title

            # Deathmatch terminal-status flag shared with the nested
            # _agent_loop_task (which sets it) and this function's finalize
            # path (which reads it). Must live in THIS scope — defining it
            # only inside _agent_loop_task caused a NameError at the final
            # save whenever the loop ended with empty content (conv 51d74833).
            _dm_terminal_status_saved = False

            # Stream buffer for this conversation, shared between the agent
            # task (creator) and the relay loop (post-disconnect save). Must
            # live in _run_tool_loop scope, NOT inside _agent_loop_task: the
            # relay's _bg_save / interrupted-save paths reference it after
            # the agent task completes (conv b078987b recovery hit a
            # NameError here when the post-disconnect relay path ran in the
            # detached re-run).
            _stream_buf = None

            async def _agent_loop_task():
                # Shared with _run_tool_loop (enclosing scope): set when the
                # deathmatch terminal status message (partial_complete/
                # human_gate/paused) was persisted during per-turn handling —
                # the outer finalize path then skips saving a redundant
                # empty-content assistant bubble. Defined in _run_tool_loop
                # before this def; bound here via nonlocal.
                nonlocal _stream_buf, _dm_terminal_status_saved
                _ttft_t0 = time.monotonic()
                assistant_content = ""
                assistant_reasoning = ""
                # Pre-tool text gating: on tool-requiring turns (coordinator
                # route=tool_loop + expects_tools=True) content produced BEFORE
                # the first tool call is transient — it is dropped from the
                # persisted answer, so streaming it makes the user see a
                # "first answer" that later vanishes or duplicates the real
                # one (conv 149ce886: the model claimed "我进行了真实检索"
                # before the auto-invoked web_search). Gated text is discarded
                # on the first tool_call, released at iteration boundaries and
                # flushed at turn end otherwise. The hold is BOUNDED
                # (HOLD_MAX_SECONDS) so a turn where the coordinator
                # over-predicted tools but the model answered directly can
                # never park the whole answer silently until `done` — the
                # answer streams live instead of popping whole (2026-08-06).
                _pre_tool_gate = PreToolGate()
                # Set when the agent loop terminated via an error event. The
                # completion block uses it to substitute a visible failure
                # message instead of persisting an empty/invisible turn.
                _loop_error: str | None = None
                current_reasoning_segment = ""
                # 遵循词 canary streaming strip: hold-back tails. LLM deltas are
                # 1-3 chars, so [遵循词:xxxxxx] is routinely split across chunks
                # and per-chunk regex strip misses it (conv 6227fb26 leak).
                _canary_tail = ""
                _reasoning_canary_tail = ""
                content_segments: list[str] = []
                display_sequence: list[dict] = []
                tool_results_accumulated: list[dict] = []
                # PHASE 2B: collect raw tool_call events parallel to results.
                # Used to populate Message.tool_calls so structured history
                # can be replayed in future turns.
                tool_call_events_accumulated: list[dict] = []
                search_queries_used: list[str] = []
                # Per-call web_search query map (conv a3cfb421 2026-08-09):
                # call_id -> queries, so the persisted rounds display each
                # round's OWN queries instead of the last call's for all.
                search_queries_by_call: dict[str, list[str]] = {}
                # Last accumulated download-card set streamed to the client
                # (conv fbf5779b: emit the accumulated set on each tool_result
                # so the live count equals the persisted one).
                _last_streamed_attachments: list[dict] = []
                # Deathmatch: deliverable file attachments collected when the
                # goal completes. Injected into the final summary message's
                # tool_results so download cards appear without relying on
                # per-tool-call attachments.
                _dm_final_attachments: list[dict] = []
                # Deathmatch: attachments from the most recent per-turn save,
                # used as fallback when _dm_final_attachments is empty (e.g.
                # paused/human_gate paths where collect_final_deliverables was
                # never called). Ensures the final summary message carries
                # download cards for files generated in the last turn.
                _dm_last_turn_attachments: list[dict] = []

                # Create stream buffer for this conversation
                try:
                    _stream_buf = await stream_buffer_manager.create_buffer(
                        _cap_conversation_id, _cap_user_id
                    )
                    await _stream_buf.set_sse_active(True)
                except Exception:
                    # Buffer creation failed before the main try below — no
                    # `_failed` would ever be broadcast, so the relay loop
                    # (drain mode in setup-recovery) would spin on 10s pings
                    # forever with an adopted slot. Broadcast failure and
                    # terminate; the task's finally releases the slot.
                    logger.exception(
                        "Stream buffer creation failed for conversation %s",
                        _cap_conversation_id,
                    )
                    try:
                        await _agent_state.broadcast({"_failed": "流缓冲创建失败"})
                        await _agent_state.broadcast_done()
                    except Exception:
                        pass
                    return

                # F1-1: per-stream part translator. Legacy events keep flowing
                # unchanged; each translated event additionally emits
                # part_started/part_delta/part_updated so v2 clients can drive
                # the timeline by stable part_id instead of positional heuristics.
                part_translator = PartTranslator()

                async def _put(event_dict):
                    # F1-1: translate + broadcast part events FIRST. Ordering
                    # matters: the frontend activates partMode on the first part
                    # event; legacy events arriving afterwards only update
                    # accumulators (content string, tool list), never the
                    # displaySequence — preventing double-rendered items.
                    #
                    # Ordering between buffer and broadcast: the stream buffer
                    # is the resume-replay authority — a client that
                    # disconnects and resumes must be able to reconstruct
                    # EVERYTHING it already displayed. Recording the buffer
                    # BEFORE broadcasting guarantees the replay snapshot is
                    # never behind the live stream; broadcasting first opened
                    # a window where the client received an event that the
                    # snapshot missed, so the resume replay regressed the
                    # timeline and already-displayed content vanished
                    # mid-answer (refresh restored it from the DB).
                    part_events = part_translator.translate(event_dict)
                    for _pe in part_events:
                        try:
                            await _stream_buf.append(_pe)
                        except Exception:
                            pass
                        await _agent_state.broadcast(_pe)

                    # Update registry state
                    if "content" in event_dict:
                        _agent_state.content += event_dict["content"]
                    elif "content_segment" in event_dict:
                        seg = event_dict["content_segment"]
                        if seg.strip():
                            _agent_state.content_segments.append(seg)
                    elif "reasoning_content" in event_dict:
                        _agent_state.reasoning += event_dict["reasoning_content"]
                    elif "tool_call" in event_dict:
                        _agent_state.tool_calls.append(event_dict["tool_call"])
                    elif "tool_result" in event_dict:
                        _agent_state.tool_results.append(event_dict["tool_result"])
                    elif "attachments" in event_dict:
                        _agent_state.file_attachments = list(event_dict["attachments"])
                    elif "iteration" in event_dict:
                        _agent_state.iteration = event_dict["iteration"]
                    elif "agent_step" in event_dict:
                        pass  # pass through to broadcast

                    # Also write to persistent stream buffer (before the
                    # broadcast — see ordering note above).
                    try:
                        await _stream_buf.append(event_dict)
                    except Exception:
                        pass

                    # Broadcast to all subscribers (last: buffer already
                    # recorded the event, so a disconnect right after the
                    # broadcast can always be recovered via resume replay).
                    await _agent_state.broadcast(event_dict)

                async def _persist_final_answer(
                    *,
                    conv_id: str,
                    content: str,
                    reasoning: str | None,
                    tr_json_str: str | None,
                    tc_json: str | None,
                ) -> str | None:
                    """conv 3b58af5b (R4, 2026-08-23): single-writer producer-
                    side persistence for the final answer. Runs BEFORE the
                    `_completed` broadcast so a client disconnect in the
                    completion window can never lose a fully-generated answer.
                    Returns the message id, or None on failure (callers fall
                    back to the legacy consumer/self-save paths)."""
                    try:
                        async with AsyncSessionLocal() as db:
                            conv_row = (await db.execute(
                                select(Conversation).where(Conversation.id == conv_id)
                            )).scalar_one_or_none()
                            if conv_row is None:
                                return None
                            msg = Message(
                                conversation_id=conv_id,
                                role="assistant",
                                content=content,
                                reasoning_content=reasoning or None,
                                tool_results=tr_json_str,
                                tool_calls=tc_json,
                            )
                            db.add(msg)
                            conv_row.updated_at = datetime.utcnow()
                            await db.commit()
                    except Exception:
                        logger.exception(
                            "Producer-side persistence failed for conversation %s", conv_id
                        )
                        return None
                    # A4.9 fix (Important-1): commit succeeded — the row is
                    # durable. Exceptions during refresh/close must NOT be
                    # reported as "not persisted" (callers would fall back to
                    # the legacy save path and insert a duplicate row).
                    try:
                        return str(msg.id)
                    except Exception:
                        logger.exception(
                            "Persisted message id unavailable for conversation %s "
                            "(row committed; falling back to legacy consumer save)",
                            conv_id,
                        )
                        return None

                # 每轮开始时推送上下文 token 用量（前端头部徽章）。
                if _ctx_tokens > 0:
                    await _put({"context_info": {
                        "tokens": _ctx_tokens,
                        "context_length": _ctx_window,
                    }})

                task_db = AsyncSessionLocal()
                try:
                    task_conversation = await task_db.get(Conversation, _cap_conversation_id)
                    task_user = await task_db.get(User, _cap_user_id)
                    # DEATHMATCH: Rebind deathmatch manager to task_db conversation
                    if _deathmatch_mgr is not None and task_conversation is not None:
                        _deathmatch_mgr._conv = task_conversation
                        agent_loop.deathmatch_manager = _deathmatch_mgr
                        # Emit initial deathmatch status for frontend
                        await _put({"deathmatch_verdict": {
                            "status": _deathmatch_mgr._conv.deathmatch_status,
                            "verdict": None,
                            "reason": None,
                            "turns": _deathmatch_mgr._conv.deathmatch_turns,
                            "max_turns": _deathmatch_mgr._conv.deathmatch_max_turns,
                            "grilling_completed": _deathmatch_mgr._conv.deathmatch_grilling_completed or 0,
                            "grilling_total": _deathmatch_mgr._conv.deathmatch_grilling_total or 0,
                            "message": "",
                        }})
                except Exception:
                    logger.warning("Agent task failed to load conversation/user from fresh DB session")
                    await _agent_queue.put({"_failed": "DB session init failed"})
                    try:
                        await task_db.close()
                    except Exception:
                        pass
                    return

                # DEATHMATCH: Generate grilling questions during grilling phase
                _grilling_generated = False
                if _deathmatch_mgr and _deathmatch_mgr.is_grilling:
                    # First, try to recover from a zombie grilling state
                    # (all tasks completed but goal never synthesized).
                    recovered = await _deathmatch_mgr.try_recover_stalled_grilling(task_db)
                    if recovered:
                        await task_db.commit()
                        await _put({
                            "deathmatch_verdict": {
                                "status": "active",
                                "verdict": "grilling_recovered",
                                "reason": "盘问已完成，目标已合成，死磕模式已恢复",
                                "turns": _deathmatch_mgr._conv.deathmatch_turns or 0,
                                "max_turns": _deathmatch_mgr._conv.deathmatch_max_turns or 0,
                                "grilling_completed": _deathmatch_mgr._conv.deathmatch_grilling_completed or 0,
                                "grilling_total": _deathmatch_mgr._conv.deathmatch_grilling_total or 0,
                                "message": "盘问阶段已完成，死磕目标执行已恢复",
                            }
                        })
                        _grilling_generated = False
                    else:
                        try:
                            grilling_questions = await _deathmatch_mgr.generate_grilling_questions(
                                query=_cap_latest_user_query,
                                db=task_db,
                                user_id=_cap_user_id,
                                assistant_id=_cap_assistant.id if _cap_assistant else None,
                            )
                            await task_db.commit()
                            completed, total = _deathmatch_mgr.grilling_progress
                            round_num = _deathmatch_mgr._conv.deathmatch_grilling_round or 1
                            round_total = _deathmatch_mgr._conv.deathmatch_grilling_round_total or 3
                            await _put({
                                "deathmatch_verdict": {
                                    "status": "grilling",
                                    "verdict": "grilling_questions_generated",
                                    "reason": f"已生成第{round_num}轮{total}个盘问问题",
                                    "turns": 0,
                                    "max_turns": 0,
                                    "grilling_completed": completed,
                                    "grilling_total": total,
                                    "grilling_round": round_num,
                                    "grilling_round_total": round_total,
                                    "message": f"盘问阶段 第{round_num}/{round_total}轮 — 请回答{total}个问题以明确目标",
                                    "grilling_questions": grilling_questions,
                                }
                            })
                            _grilling_generated = True
                        except Exception:
                            logger.exception("Failed to generate grilling questions")
                            await _put({
                                "deathmatch_verdict": {
                                    "status": "grilling",
                                    "verdict": "grilling_generation_failed",
                                    "reason": "盘问问题生成失败",
                                    "turns": 0,
                                    "max_turns": 0,
                                    "grilling_completed": 0,
                                    "grilling_total": 0,
                                    "grilling_round": 0,
                                    "grilling_round_total": 0,
                                    "message": "盘问问题生成失败，请重试或关闭死磕模式",
                                }
                            })
                            _grilling_generated = True

                # Release the setup session before the (potentially long) agent loop.
                try:
                    await task_db.close()
                except Exception:
                    pass
                task_db = None

                try:
                    # DEATHMATCH: If in grilling phase, skip the agent loop.
                    # Grilling questions were already generated and emitted above.
                    # The agent loop should only run during the active goal phase.
                    if _deathmatch_mgr and _deathmatch_mgr.is_grilling and _grilling_generated:
                        content_segments.append(f"盘问阶段：已生成 {_deathmatch_mgr._conv.deathmatch_grilling_total or 0} 个问题，等待回答")
                        display_sequence.append({"type": "text", "content": "盘问阶段：请回答上方的问题以明确目标"})
                        await _put({"done": True})
                    else:
                        _ttft_t1 = time.monotonic()
                        _ttft_t2 = 0  # coordinator return
                        _ttft_t3 = 0  # first content delta
                        async for event in agent_loop.run(
                            messages_for_model,
                            user=task_user,
                            conversation=task_conversation,
                            assistant=_cap_assistant,
                            precomputed_coord=coord_result,
                        ):
                            if "coord_done" in event:
                                _ttft_t2 = time.monotonic()

                            elif "coordinator" in event:
                                _coord_route = (event["coordinator"] or {}).get("route")
                                _coord_expects = (event["coordinator"] or {}).get("expects_tools")
                                _pre_tool_gate.arm(_coord_route, _coord_expects)

                            elif "reasoning_content" in event:
                                if not _ttft_t3:
                                    _ttft_t3 = time.monotonic()
                                    logger.info(
                                        "TTFT(reasoning) conv=%s setup=%dms coord=%dms prefill=%dms total=%dms",
                                        conversation_id,
                                        int((_ttft_t1 - _ttft_t0) * 1000),
                                        int((_ttft_t2 - _ttft_t1) * 1000) if _ttft_t2 else -1,
                                        int((_ttft_t3 - _ttft_t2) * 1000) if _ttft_t2 else -1,
                                        int((_ttft_t3 - _ttft_t0) * 1000),
                                    )
                                # 遵循词 canary marker may appear in CoT when
                                # the model restates the echo rule — strip it
                                # here (single funnel for display + persist).
                                # Streaming-aware: the marker can be split
                                # across deltas, so a tail buffer is held back
                                # (conv 6227fb26 leaked it into the thinking
                                # panel otherwise).
                                _reasoning_chunk, _reasoning_canary_tail = strip_canary_streaming(
                                    event["reasoning_content"], _reasoning_canary_tail
                                )
                                assistant_reasoning += _reasoning_chunk
                                current_reasoning_segment += _reasoning_chunk
                                await _put({"reasoning_content": _reasoning_chunk})

                            elif "content" in event:
                                if not _ttft_t3:
                                    _ttft_t3 = time.monotonic()
                                    logger.info(
                                        "TTFT conv=%s setup=%dms coord=%dms prefill=%dms total=%dms",
                                        conversation_id,
                                        int((_ttft_t1 - _ttft_t0) * 1000),
                                        int((_ttft_t2 - _ttft_t1) * 1000) if _ttft_t2 else -1,
                                        int((_ttft_t3 - _ttft_t2) * 1000) if _ttft_t2 else -1,
                                        int((_ttft_t3 - _ttft_t0) * 1000),
                                    )
                                _clean_content, _canary_tail = strip_canary_streaming(
                                    event["content"], _canary_tail
                                )
                                _clean_content = _strip_dsml_all(_clean_content)
                                assistant_content += _clean_content
                                if _clean_content:
                                    _gate_stream = _pre_tool_gate.on_content(
                                        _clean_content, time.monotonic()
                                    )
                                    if _gate_stream:
                                        # Hold cap released (or gate not armed):
                                        # the text must reach the client NOW.
                                        await _put({"content": _gate_stream})

                            elif "tool_call" in event:
                                reason_seg = current_reasoning_segment.strip()
                                if reason_seg:
                                    _reason_item = {
                                        "type": "reasoning_step",
                                        "title": "💭 思考过程",
                                        "content": reason_seg,
                                    }
                                    display_sequence.append(_reason_item)
                                    await _put({"reasoning_segment": _reason_item})
                                    current_reasoning_segment = ""
                                # When the assistant emits tool calls, the content
                                # produced so far is a transient pre-tool
                                # utterance (often a duplicate or placeholder).
                                # Do not persist it as part of the final message;
                                # only the answer produced after the tool results
                                # should be saved.
                                display_sequence.append({"type": "tool_placeholder"})
                                if not _pre_tool_gate.cap_released:
                                    # Pre-tool text on a tool-requiring turn was
                                    # held back (gating) — drop it silently; the
                                    # answer after the tool results is
                                    # authoritative. EXCEPTION: when the hold
                                    # cap already released the prose (it was
                                    # streamed to the user), keep it in the
                                    # persisted answer — the user saw it and it
                                    # must not vanish on refresh (A4.9 I2).
                                    assistant_content = ""
                                # Held canary tails belong to the discarded
                                # pre-tool draft — reset so no stale partial
                                # marker is prepended to post-tool content.
                                _canary_tail = ""
                                _reasoning_canary_tail = ""
                                _pre_tool_gate.on_tool_call()
                                tc = event["tool_call"]
                                tool_call_events_accumulated.append(tc)
                                if tc.get("name") == "web_search":
                                    args = tc.get("arguments", {})
                                    queries = args.get("queries", [])
                                    if isinstance(queries, list):
                                        search_queries_used = list(queries)
                                        # Per-call queries for round-level display
                                        # (conv a3cfb421 2026-08-09): keep each
                                        # web_search call's own query list keyed
                                        # by call id so the persisted rounds show
                                        # real per-round queries instead of the
                                        # last call's for every round. AgentLoop
                                        # emits tool_call events with `call_id`
                                        # (never `id` — A4.9 review finding).
                                        search_queries_by_call[tc.get("call_id") or tc.get("id") or ""] = list(queries)
                                await _put({"tool_call": tc})

                            elif "tool_result" in event:
                                tr = event["tool_result"]
                                tool_results_accumulated.append(tr)
                                await _put({"tool_result": tr})
                                # Live download-card set: recompute the
                                # ACCUMULATED set (same helper as the
                                # persist-time transform) so the streamed card
                                # count grows monotonically and never drops
                                # files. The previous per-call events replaced
                                # the whole set with only the current tool
                                # call's files, so the live count jumped
                                # 1→1→1→1→4 with docx cards vanishing
                                # (conv fbf5779b, 2026-08-06).
                                _live_atts = _collect_download_attachments(tool_results_accumulated)
                                if _live_atts and _live_atts != _last_streamed_attachments:
                                    _last_streamed_attachments = _live_atts
                                    await _put({"attachments": _live_atts})

                            elif "iteration" in event:
                                await _put({"iteration": event["iteration"]})

                            elif "context_info" in event:
                                # 紧急压缩 recovery（LLM 错误/final-thinking）路径
                                # 从 agent_loop 直接 yield context_info——转发给
                                # 中继层刷新前端 token 徽章。
                                await _put({"context_info": event["context_info"]})

                            elif "iteration_done" in event:
                                # Iteration boundary without a tool_call event:
                                # the previous iteration ended. A no-tool
                                # iteration means the held text IS the answer —
                                # stream it now instead of parking it until
                                # `done` (whole-answer pop, 2026-08-06). A
                                # tool iteration means the held text is stale
                                # pre-tool prose — drop it (also covers a
                                # tool_call event lost to subscriber-queue
                                # overflow, which would otherwise keep the
                                # gate armed for the whole turn).
                                _id_info = event["iteration_done"] or {}
                                _gate_flush = _pre_tool_gate.on_iteration_done(
                                    bool(_id_info.get("tool_calls")),
                                    time.monotonic(),
                                )
                                if bool(_id_info.get("tool_calls")):
                                    # Tool-call event lost to queue overflow:
                                    # the tool_call branch (which resets the
                                    # canary tails) never ran — reset here so
                                    # stale partial-marker text cannot merge
                                    # into post-tool content (A4.9 review).
                                    _canary_tail = ""
                                    _reasoning_canary_tail = ""
                                if _gate_flush.strip():
                                    logger.info(
                                        "Pre-tool gate released by iteration_done "
                                        "(iteration=%s, tool_calls=%s): streaming held text (%d chars)",
                                        _id_info.get("iteration"), _id_info.get("tool_calls"),
                                        len(_gate_flush),
                                    )
                                    await _put({"content": _gate_flush})

                            elif "ping" in event:
                                await _put({"ping": True})

                            elif "compression" in event:
                                # Canary/token-threshold compression fired
                                # mid-turn: the model will re-answer with the
                                # compacted context. Discard the pre-compression
                                # draft so the PERSISTED answer is the
                                # regenerated one, not a concatenation
                                # (reviewer A4.9 fix: I2). Note: the draft was
                                # already streamed live — the client shows it
                                # until the "上下文压缩" step, which is
                                # irreversible at the SSE layer (M2). The
                                # token-threshold trigger fires before any
                                # content, so resetting is a no-op there.
                                # Tool accumulators are reset too (M3): tool
                                # cards of the discarded draft would otherwise
                                # attach to the regenerated message, but the
                                # compression summary preserves their outcome.
                                logger.debug("Context compressed: %s", event["compression"])
                                assistant_content = ""
                                assistant_reasoning = ""
                                current_reasoning_segment = ""
                                content_segments = []
                                display_sequence = []
                                tool_results_accumulated = []
                                tool_call_events_accumulated = []
                                search_queries_used = []
                                search_queries_by_call = {}
                                _canary_tail = ""
                                _reasoning_canary_tail = ""
                                _pre_tool_gate.reset()
                                # 压缩前后 token 对比（含工具 schema 口径）——
                                # 前端在「上下文压缩」步骤块内展示 X → Y tokens 变化。
                                _comp = event["compression"] or {}
                                _c_before = int(_comp.get("before") or 0)
                                _c_after = int(_comp.get("after") or 0)
                                # A4 (2026-08-21): canary-triggered compressions
                                # are silent — no timeline step, only logs +
                                # token-badge refresh. Token-threshold
                                # compressions (pre-turn maintenance) stay
                                # visible as before.
                                if not _comp.get("silent"):
                                    from app.services.context_compressor import format_compression_step_content
                                    await _put({
                                        "agent_step": {
                                            "name": "context_compression",
                                            "title": "上下文压缩",
                                            "content": format_compression_step_content(_c_before, _c_after),
                                            "step_type": "system",
                                            "tokens_before": _c_before or None,
                                            "tokens_after": _c_after or None,
                                        }
                                    })
                                # 压缩后上下文已变小——刷新前端 token 徽章。
                                if _c_after > 0:
                                    await _put({"context_info": {
                                        "tokens": _c_after,
                                        "context_length": _ctx_window,
                                        "compressed": True,
                                    }})

                            elif "audit_reset" in event:
                                # A4 (2026-08-21): internal QC reset event.
                                # A draft was streamed live and then failed the
                                # internal quality audit (or the canary re-assert
                                # path) — reset the accumulators so the rejected
                                # draft never concatenates into the PERSISTED
                                # answer; the regeneration that follows is the
                                # only authoritative content. Nothing is
                                # rendered to the client (silent QC).
                                #
                                # conv 3b58af5b (2026-08-23, R1/R2): ONLY the
                                # rejected draft TEXT is discarded. The turn's
                                # real work survives:
                                #   - reasoning (assistant_reasoning +
                                #     display_sequence reasoning_step items) —
                                #     post-reject revisions run thinking-off
                                #     (revision_thinking_off, user decision
                                #     2026-08-21), so discarding pre-reject
                                #     thinking erased the turn's ENTIRE
                                #     thinking panel from the persisted answer;
                                #   - tool_placeholder items — they pair 1:1
                                #     with the preserved tool_results
                                #     accumulated entries at completion;
                                #     clearing them starved the pairing and
                                #     appended every agent_step AFTER the final
                                #     text (tools-at-bottom symptom).
                                assistant_content = ""
                                reason_seg = current_reasoning_segment.strip()
                                if reason_seg:
                                    # Thinking accrued since the last tool
                                    # boundary belongs to the rejected round —
                                    # flush it as a preserved reasoning_step.
                                    # Silent QC: NOT re-emitted live (the user
                                    # already saw these deltas stream); this
                                    # only fixes the persisted surfaces.
                                    display_sequence.append({
                                        "type": "reasoning_step",
                                        "title": "💭 思考过程",
                                        "content": reason_seg,
                                    })
                                    current_reasoning_segment = ""
                                # conv efaf8f9c (2026-08-20): do NOT clear
                                # tool_results_accumulated /
                                # tool_call_events_accumulated /
                                # search_queries_used / search_queries_by_call
                                # here. What must be discarded is the rejected
                                # DRAFT text/reasoning that was streamed live;
                                # the tool work from earlier iterations is the
                                # turn's evidence and must survive so the final
                                # persisted tool_results keeps results[],
                                # agent_steps[] and the citation mapping.
                                _canary_tail = ""
                                _reasoning_canary_tail = ""
                                _pre_tool_gate.reset()

                            elif "agent_step" in event:
                                await _put({"agent_step": event["agent_step"]})

                            elif "error" in event:
                                logger.error("Agent loop error: %s", event["error"])
                                _loop_error = str(event["error"])
                                await _agent_queue.put({"error": event["error"]})

                            elif "deathmatch_verdict" in event:
                                _dm_verdict = event["deathmatch_verdict"]
                                await _put({"deathmatch_verdict": _dm_verdict})
                                # Capture final deliverable attachments (set
                                # when the goal is deemed complete) so they
                                # can be attached to the final summary message.
                                _new_att = _dm_verdict.get("final_attachments") or []
                                if _new_att:
                                    _dm_final_attachments = _new_att
                                # _dm_last_turn_attachments is captured during
                                # per-turn persistence above.
                                try:
                                    async with AsyncSessionLocal() as _dm_db:
                                        _dm_conv = await _dm_db.get(Conversation, conversation_id)
                                        if _dm_conv:
                                            _dm_conv.deathmatch_status = task_conversation.deathmatch_status
                                            _dm_conv.deathmatch_turns = task_conversation.deathmatch_turns
                                            _dm_conv.deathmatch_verdict = task_conversation.deathmatch_verdict
                                            _dm_conv.deathmatch_reason = task_conversation.deathmatch_reason
                                            _dm_conv.deathmatch_consecutive_failures = task_conversation.deathmatch_consecutive_failures
                                            _dm_conv.deathmatch_marker_miss_count = task_conversation.deathmatch_marker_miss_count
                                            _dm_conv.deathmatch_grilling_completed = task_conversation.deathmatch_grilling_completed
                                            _dm_conv.deathmatch_grilling_total = task_conversation.deathmatch_grilling_total
                                            _dm_conv.deathmatch_grilling_round = task_conversation.deathmatch_grilling_round
                                            _dm_conv.deathmatch_plan = task_conversation.deathmatch_plan
                                            _dm_conv.deathmatch_plan_version = task_conversation.deathmatch_plan_version
                                            _dm_conv.deathmatch_verify_failures = task_conversation.deathmatch_verify_failures
                                            _dm_conv.deathmatch_last_verification_result = task_conversation.deathmatch_last_verification_result
                                            _dm_conv.deathmatch_human_gate = task_conversation.deathmatch_human_gate
                                            await _dm_db.commit()
                                except Exception:
                                    logger.exception("Failed to commit deathmatch state")

                                # ── Per-turn persistence: save each deathmatch turn's
                                # content as a separate DB message so it survives page
                                # refreshes. Without this, all intermediate turn content
                                # lives only in memory and is lost on disconnect/refresh.
                                _dm_should_continue = _dm_verdict.get("should_continue", False)
                                _dm_continuation_prompt = _dm_verdict.get("continuation_prompt", "")
                                _dm_status_val = _dm_verdict.get("status", "")
                                _dm_message_val = _dm_verdict.get("message", "")
                                _dm_turn_num = _dm_verdict.get("turns", 0)

                                # Flush current assistant_content to content_segments
                                # (A4.9 I1: per-turn deathmatch persistence is the
                                # same final-answer surface — same single strip).
                                _turn_content = _strip_leading_orphan_colon(_strip_leading_orphan_punct(_strip_dsml_all(assistant_content).strip()))
                                if _turn_content:
                                    content_segments.append(_turn_content)
                                    display_sequence.append({"type": "text", "content": _turn_content})

                                # Build joined content for this turn
                                _turn_joined = "\n\n<!-- segment_split -->\n\n".join(content_segments) if content_segments else ""

                                # Build tool results JSON for this turn
                                _turn_tr_json = None
                                if tool_results_accumulated:
                                    try:
                                        _turn_tr_json = _transform_tool_loop_results(
                                            tool_results_accumulated,
                                            search_queries_used=search_queries_used if search_queries_used else None,
                                            queries_by_call=search_queries_by_call or None,
                                        )
                                    except Exception:
                                        # A transform failure must never kill the
                                        # detached goal loop (conv 6b0faf81: an
                                        # unhandled raise here escaped to the
                                        # task catch-all, leaving "active" with
                                        # no terminal message — silent zombie).
                                        logger.exception(
                                            "Deathmatch turn transform failed (turn %d) — persisting raw tool results",
                                            _dm_turn_num,
                                        )
                                        _turn_tr_json = json.dumps({
                                            "content_segments": content_segments,
                                            "display_sequence": display_sequence,
                                        }, ensure_ascii=False)
                                    if _turn_tr_json:
                                        try:
                                            _tr_obj = json.loads(_turn_tr_json)
                                        except Exception:
                                            _tr_obj = None
                                        if _tr_obj:
                                            _a_steps = _tr_obj.get("agent_steps", [])
                                            _resolved_turn = []
                                            _step_idx = 0
                                            for _item in display_sequence:
                                                if _item.get("type") == "text":
                                                    _resolved_turn.append(_item)
                                                elif _item.get("type") == "reasoning_step":
                                                    _resolved_turn.append(_item)
                                                elif _item.get("type") == "tool_placeholder" and _step_idx < len(_a_steps):
                                                    _step = dict(_a_steps[_step_idx])
                                                    _step["type"] = _step.get("step_type", "tool")
                                                    _resolved_turn.append(_step)
                                                    _step_idx += 1
                                            while _step_idx < len(_a_steps):
                                                _step = dict(_a_steps[_step_idx])
                                                _step["type"] = _step.get("step_type", "tool")
                                                _resolved_turn.append(_step)
                                                _step_idx += 1
                                            _tr_obj["display_sequence"] = _resolved_turn
                                            _tr_obj["content_segments"] = content_segments
                                            _turn_tr_json = json.dumps(_tr_obj, ensure_ascii=False)
                                elif content_segments:
                                    _turn_tr_json = json.dumps({
                                        "content_segments": content_segments,
                                        "display_sequence": display_sequence,
                                    }, ensure_ascii=False)

                                # Serialize tool_calls for this turn
                                _turn_tc_json = None
                                if tool_call_events_accumulated:
                                    try:
                                        from app.services.tool_history import build_persisted_tool_calls
                                        _turn_tc_json = build_persisted_tool_calls(tool_call_events_accumulated)
                                    except Exception:
                                        pass

                                # Save this turn's assistant message to DB
                                if _turn_joined or _turn_tr_json:
                                    try:
                                        _turn_content = _build_content_fallback(_turn_joined, _turn_tr_json)
                                        _turn_content, _turn_tr_json = await _localize_media_for_persist(
                                            _turn_content, _turn_tr_json,
                                            _user_id_local, _username_local,
                                        )
                                        async with AsyncSessionLocal() as _turn_db:
                                            _turn_msg = Message(
                                                conversation_id=conversation_id,
                                                role="assistant",
                                                content=_turn_content,
                                                tool_results=_turn_tr_json,
                                                tool_calls=_turn_tc_json,
                                            )
                                            _turn_db.add(_turn_msg)
                                            await _turn_db.commit()
                                            logger.info(
                                                "Deathmatch turn %d persisted as message %s (%d chars)",
                                                _dm_turn_num, _turn_msg.id, len(_turn_joined or ""),
                                            )
                                            # Capture attachments from the most
                                            # recent per-turn tool_results so the
                                            # final summary message can carry
                                            # download cards even when the
                                            # deathmatch ver dict has no
                                            # final_attachments (paused/human_gate
                                            # paths).
                                            if _turn_tr_json:
                                                try:
                                                    _trp = json.loads(_turn_tr_json)
                                                    _ta = _trp.get("attachments")
                                                    if isinstance(_ta, list):
                                                        _dm_last_turn_attachments = list(_ta)
                                                except Exception:
                                                    pass
                                    except Exception:
                                        logger.exception("Failed to save deathmatch turn message")

                                # Save continuation prompt as user message
                                if _dm_should_continue and _dm_continuation_prompt:
                                    _clean_prompt = MARKER_RE.sub("", _dm_continuation_prompt).strip()
                                    if _clean_prompt:
                                        try:
                                            async with AsyncSessionLocal() as _turn_db:
                                                _turn_user_msg = Message(
                                                    conversation_id=conversation_id,
                                                    role="user",
                                                    content=_clean_prompt,
                                                )
                                                _turn_db.add(_turn_user_msg)
                                                await _turn_db.commit()
                                        except Exception:
                                            logger.exception("Failed to save deathmatch continuation prompt")
                                elif not _dm_should_continue and (
                                    _dm_status_val in ("human_gate", "paused", "partial_complete")
                                    or _dm_verdict.get("verdict") == "wait"
                                ) and _dm_message_val:
                                    # Save status message for human_gate/paused/
                                    # partial_complete. partial_complete carries
                                    # the collected deliverables as attachments so
                                    # the ending shows download cards next to the
                                    # status text instead of an empty bubble.
                                    _status_tr_json = None
                                    if _dm_final_attachments:
                                        try:
                                            _status_tr_json = json.dumps(
                                                {"attachments": _dm_final_attachments},
                                                ensure_ascii=False,
                                            )
                                        except Exception:
                                            _status_tr_json = None
                                    try:
                                        async with AsyncSessionLocal() as _turn_db:
                                            _turn_status_msg = Message(
                                                conversation_id=conversation_id,
                                                role="assistant",
                                                content=_dm_message_val,
                                                tool_results=_status_tr_json,
                                            )
                                            _turn_db.add(_turn_status_msg)
                                            await _turn_db.commit()
                                        _dm_terminal_status_saved = True
                                    except Exception:
                                        logger.exception("Failed to save deathmatch status message")

                                # Reset accumulators for next turn
                                assistant_content = ""
                                assistant_reasoning = ""
                                current_reasoning_segment = ""
                                content_segments = []
                                display_sequence = []
                                tool_results_accumulated = []
                                tool_call_events_accumulated = []
                                search_queries_used = []
                                search_queries_by_call = {}
                                _canary_tail = ""
                                _reasoning_canary_tail = ""
                                # Deathmatch per-turn persistence above writes
                                # assistant_content (which INCLUDES gated text);
                                # flush the gated text to the stream so the
                                # live view matches what is persisted, then
                                # reset the gate for the next turn.
                                _gate_flush = _pre_tool_gate.flush()
                                if _gate_flush.strip():
                                    await _put({"content": _gate_flush})

                            elif "done" in event:
                                # Turn ended without a tool call: flush any
                                # gated pre-tool text (it IS the final answer).
                                _gate_flush = _pre_tool_gate.flush()
                                if _gate_flush.strip():
                                    await _put({"content": _gate_flush})
                                break

                    # Strip a leading orphan colon HERE (not only at
                    # _compose_terminal_content) because the frontend renders
                    # the display_sequence / content_segments copies — a
                    # content-only strip would leave the dangling '：' visible
                    # in v2 rendering (same dual-surface lesson as conv 8629bdfe).
                    full_response_local = _strip_leading_orphan_colon(_strip_leading_orphan_punct(_strip_dsml_all(assistant_content).strip()))
                    if full_response_local:
                        content_segments.append(full_response_local)
                        display_sequence.append({"type": "text", "content": full_response_local})
                    joined_content = "\n\n<!-- segment_split -->\n\n".join(content_segments)

                    # Error-terminated OR content-empty-with-tool-work loop:
                    # never emit an empty/partial `_completed`. Substitute a
                    # visible failure message so every downstream path
                    # (finalize save, detached self-save, buffer replay,
                    # legacy resume save) persists something the user can
                    # actually see — an empty bubble is filtered by the
                    # frontend and renders as "query with nothing below"
                    # (conv 149ce886 msg f9d168bd, conv 38ce8810 msg 63f0e6ee).
                    _had_tool_activity = bool(tool_results_accumulated or tool_call_events_accumulated)
                    _terminal_content, _terminal_substituted = _compose_terminal_content(
                        joined_content, _loop_error, _had_tool_activity,
                    )
                    if _terminal_substituted:
                        content_segments.append(_terminal_content)
                        display_sequence.append({"type": "text", "content": _terminal_content})
                        joined_content = _terminal_content

                    # DEATHMATCH: grilling completion is now handled via
                    # the subagent question/answer flow, not [GOAL_SUMMARY].
                    full_reasoning_local = assistant_reasoning.strip()

                    # Insert any remaining (post-last-tool_call) reasoning as a
                    # final reasoning_step. Earlier per-turn reasoning chunks
                    # were already flushed into display_sequence at each
                    # tool_call boundary, so we only attach the trailing chunk
                    # here. Fallback: if no per-turn chunks were captured but
                    # we have full reasoning (e.g., no tool calls), attach it.
                    trailing_reason = current_reasoning_segment.strip()
                    has_inline_reasoning = any(
                        it.get("type") == "reasoning_step" for it in display_sequence
                    )
                    if trailing_reason and display_sequence:
                        _reasoning_item = {
                            "type": "reasoning_step",
                            "title": "💭 思考过程",
                            "content": trailing_reason,
                        }
                        _last_text_idx = -1
                        for _i in range(len(display_sequence) - 1, -1, -1):
                            if display_sequence[_i].get("type") == "text":
                                _last_text_idx = _i
                                break
                        if _last_text_idx >= 0:
                            display_sequence.insert(_last_text_idx, _reasoning_item)
                        else:
                            display_sequence.append(_reasoning_item)
                    elif not has_inline_reasoning and full_reasoning_local and display_sequence:
                        # No per-turn reasoning chunks ever observed (and no
                        # trailing leftover either): fall back to one global
                        # reasoning block before the final answer.
                        _reasoning_item = {
                            "type": "reasoning_step",
                            "title": "💭 思考过程",
                            "content": full_reasoning_local,
                        }
                        _last_text_idx = -1
                        for _i in range(len(display_sequence) - 1, -1, -1):
                            if display_sequence[_i].get("type") == "text":
                                _last_text_idx = _i
                                break
                        if _last_text_idx >= 0:
                            display_sequence.insert(_last_text_idx, _reasoning_item)
                        else:
                            display_sequence.append(_reasoning_item)

                    tr_json = None
                    if tool_results_accumulated:
                        logger.info(
                            "DBG transform: search_queries_used=%s map=%s accum_calls=%s",
                            search_queries_used,
                            search_queries_by_call,
                            [tr.get("call_id") for tr in tool_results_accumulated if tr.get("name") == "web_search"],
                        )
                        tr_json = _transform_tool_loop_results(
                            tool_results_accumulated,
                            search_queries_used=search_queries_used if search_queries_used else None,
                            queries_by_call=search_queries_by_call or None,
                        )
                    if content_segments:
                        if tr_json:
                            tr_obj = json.loads(tr_json)
                            a_steps = tr_obj.get("agent_steps", [])
                            step_idx = 0
                            resolved: list[dict] = []
                            for item in display_sequence:
                                if item.get("type") == "text":
                                    resolved.append(item)
                                elif item.get("type") == "reasoning_step":
                                    resolved.append(item)
                                elif item.get("type") == "tool_placeholder" and step_idx < len(a_steps):
                                    step = dict(a_steps[step_idx])
                                    step["type"] = step.get("step_type", "tool")
                                    resolved.append(step)
                                    step_idx += 1
                            while step_idx < len(a_steps):
                                step = dict(a_steps[step_idx])
                                step["type"] = step.get("step_type", "tool")
                                resolved.append(step)
                                step_idx += 1
                            tr_obj["display_sequence"] = resolved
                            tr_obj["content_segments"] = content_segments
                            tr_json = json.dumps(tr_obj, ensure_ascii=False)
                        else:
                            tr_json = json.dumps({
                                "content_segments": content_segments,
                                "display_sequence": display_sequence,
                            }, ensure_ascii=False)

                    # Deathmatch: attach final deliverable download cards to the
                    # final summary message. Three sources, merged in priority:
                    #   1. _dm_final_attachments — collected from ALL previous
                    #      turns via collect_final_deliverables_from_messages
                    #      (populated when verdict is "done" or "partial_complete")
                    #   2. _dm_last_turn_attachments — extracted from the most
                    #      recent per-turn save (captured above during per-turn
                    #      persistence). Critical fallback for paused/human_gate
                    #      paths where _dm_final_attachments is empty.
                    _dm_all_atts: list[dict] = []
                    if _dm_final_attachments:
                        _dm_all_atts.extend(_dm_final_attachments)
                    if _dm_last_turn_attachments:
                        _dm_all_atts.extend(_dm_last_turn_attachments)
                    if _dm_all_atts:
                        if tr_json:
                            _dm_tr_obj = json.loads(tr_json)
                        else:
                            _dm_tr_obj = {
                                "content_segments": content_segments,
                                "display_sequence": display_sequence,
                            }
                        existing_atts = _dm_tr_obj.get("attachments") or []
                        # Deduplicate by name, keeping the largest size.
                        _att_by_name: dict[str, dict] = {}
                        for _a in list(existing_atts) + _dm_all_atts:
                            _nm = _a.get("name") or _a.get("filename") or ""
                            if not _nm:
                                continue
                            _cur = _att_by_name.get(_nm)
                            if _cur is None or (_a.get("size") or 0) > (_cur.get("size") or 0):
                                _att_by_name[_nm] = _a
                        _dm_tr_obj["attachments"] = list(_att_by_name.values())
                        tr_json = json.dumps(_dm_tr_obj, ensure_ascii=False)

                    # PHASE 2B: serialize structured tool_calls for persistence.
                    from app.services.tool_history import build_persisted_tool_calls
                    tool_calls_json_local = build_persisted_tool_calls(
                        tool_call_events_accumulated
                    )

                    # conv 3b58af5b (R4, 2026-08-23): sanitize + localize +
                    # PERSIST here, in the producer, BEFORE any subscriber is
                    # notified. The old order broadcast `_completed` first and
                    # left the DB write to the SSE consumer (or the cancelled
                    # self-save path); a client disconnect in that window killed
                    # the consumer before it saved AND skipped the self-save
                    # branch — a fully-generated, audit-passed answer vanished
                    # without any log line. Persistence must not depend on any
                    # subscriber staying alive.
                    _cap_content = _build_content_fallback(joined_content, tr_json)
                    _cap_content, tr_json = await _sanitize_cited_content(
                        _cap_content, tr_json,
                    )
                    if _cap_content:
                        joined_content = _cap_content
                    # Localize remote media BEFORE persisting/broadcasting so
                    # the DB row, buffer done_data and every replay surface
                    # carry identical content.
                    _cap_content, tr_json = await _localize_media_for_persist(
                        _cap_content, tr_json, _user_id_local, _username_local,
                    )
                    # Guard mirrors the consumer's _skip_empty_final: when the
                    # deathmatch terminal status message was already persisted
                    # with text + deliverables, an empty fallback bubble would
                    # only duplicate its attachments.
                    _dm_skip_empty = (
                        not (_cap_content or "").strip() and _dm_terminal_status_saved
                    )
                    producer_message_id: str | None = None
                    if (_cap_content or "").strip() or (tr_json and not _dm_skip_empty):
                        producer_message_id = await _persist_final_answer(
                            conv_id=_cap_conversation_id,
                            content=_cap_content,
                            reasoning=full_reasoning_local,
                            tr_json_str=tr_json,
                            tc_json=tool_calls_json_local,
                        )
                        if producer_message_id:
                            try:
                                await _stream_buf.mark_complete(producer_message_id)
                            except Exception:
                                pass

                    completed_payload = {
                        "_completed": {
                            "joined_content": joined_content,
                            "full_reasoning": full_reasoning_local,
                            "tool_results_json": tr_json,
                            "tool_calls_json": tool_calls_json_local,
                            # R4: consumers (SSE + legacy resume) skip their own
                            # save when this is set — single-writer persistence.
                            "message_id": producer_message_id,
                        }
                    }
                    await _agent_state.broadcast(completed_payload)
                    _agent_state.is_running = False
                    _agent_state.completed_data = {
                        "joined_content": joined_content,
                        "full_reasoning": full_reasoning_local,
                        "tool_results_json": tr_json,
                    }
                    await _agent_state.broadcast_done()

                    # Pre-generate LLM title BEFORE notifying resume subscribers
                    # via the buffer's `done` event, so the title is already
                    # present in `done_data` when the resume `done` payload is
                    # resolved (avoids a race where the frontend marks the
                    # conversation finished while the sidebar title is still
                    # "新对话").
                    _pre_title: str | None = None
                    if _sse_cancelled.is_set() and (joined_content or "").strip():
                        try:
                            async with AsyncSessionLocal() as _title_db:
                                _conv_row_for_title = (await _title_db.execute(
                                    select(Conversation).where(Conversation.id == _cap_conversation_id)
                                )).scalar_one_or_none()
                                if _conv_row_for_title is not None:
                                    _pre_title = await _generate_title_bg(
                                        _title_db, _conv_row_for_title, joined_content,
                                    )
                        except Exception:
                            logger.exception("Pre-done title generation failed")
                    if _pre_title:
                        try:
                            if _stream_buf.done_data is None:
                                _stream_buf.done_data = {}
                            _stream_buf.done_data["title"] = _pre_title
                        except Exception:
                            pass

                    # (R4) citation sanitize + media localization already ran
                    # above, before the broadcast — the buffer's done payload
                    # feeds resume replay and must carry the SAME clean
                    # content/tool_results as the DB row (conv 8629bdfe).

                    # Mark stream buffer as done
                    try:
                        await _stream_buf.append({
                            "done": True,
                            "conversation_id": _cap_conversation_id,
                            "joined_content": joined_content,
                            "full_reasoning": full_reasoning_local,
                            "tool_results_json": tr_json,
                        })
                    except Exception:
                        pass

                    if _sse_cancelled.is_set():
                        if producer_message_id:
                            # R4: already persisted pre-broadcast — nothing left
                            # for the legacy self-save path to do.
                            logger.info(
                                "Agent loop completed after SSE cancellation; "
                                "answer already persisted (msg %s)",
                                producer_message_id,
                            )
                            return
                        _cap_content = _build_content_fallback(joined_content, tr_json)
                        _cap_content, tr_json = await _sanitize_cited_content(_cap_content, tr_json)
                        # Localize remote media (same as the finalize path —
                        # the self-save path persists the FULL final answer
                        # when SSE is cancelled after loop completion, so it
                        # must not bypass localization; conv 97ff355d).
                        _cap_content, tr_json = await _localize_media_for_persist(
                            _cap_content, tr_json, _user_id_local, _username_local,
                        )
                        # Guard: never self-save a completely empty bubble
                        # (no text, no tool results, no tool calls). The
                        # normal finalize path skips such messages via
                        # _skip_empty_final; the self-save path persisted one
                        # unconditionally, producing an empty card as the last
                        # message (conv 4d9a5289).
                        if not _cap_content and not tr_json and not tool_calls_json_local:
                            logger.info(
                                "Agent loop completed after SSE cancellation with empty payload; "
                                "skipping self-save (terminal status already persisted or nothing to save)"
                            )
                            try:
                                await _stream_buf.mark_complete(None)
                            except Exception:
                                pass
                            return
                        logger.info("Agent loop completed after SSE cancellation, self-saving results")
                        try:
                            for attempt in range(3):
                                try:
                                    async with AsyncSessionLocal() as bg_db:
                                        conv_row = (await bg_db.execute(
                                            select(Conversation).where(Conversation.id == _cap_conversation_id)
                                        )).scalar_one_or_none()
                                        if conv_row is None:
                                            break
                                        msg = Message(
                                            conversation_id=_cap_conversation_id,
                                            role="assistant",
                                            content=_cap_content,
                                            reasoning_content=full_reasoning_local if full_reasoning_local else None,
                                            tool_results=tr_json,
                                            tool_calls=tool_calls_json_local,
                                        )
                                        bg_db.add(msg)
                                        conv_row.updated_at = datetime.utcnow()
                                        await bg_db.commit()
                                        await bg_db.refresh(msg)
                                        try:
                                            await _stream_buf.mark_complete(str(msg.id))
                                        except Exception:
                                            pass
                                        try:
                                            _final_title = await _generate_title_bg(bg_db, conv_row, _cap_content)
                                            if _final_title:
                                                try:
                                                    if _stream_buf.done_data is None:
                                                        _stream_buf.done_data = {}
                                                    _stream_buf.done_data["title"] = _final_title
                                                except Exception:
                                                    pass
                                        except Exception:
                                            logger.exception("Background title generation failed (self-save path)")
                                        logger.info("Agent self-save succeeded on attempt %d", attempt + 1)
                                        break
                                except Exception:
                                    import traceback as _tb
                                    _tb.print_exc()
                                    await asyncio.sleep(2.0)
                        except Exception:
                            logger.exception("Agent self-save failed entirely")
                except asyncio.CancelledError:
                    # CancelledError (BaseException in 3.8+) bypasses
                    # `except Exception` below — handle it explicitly so the
                    # stream buffer is marked complete (is_running=False).
                    # Otherwise the safety-net reconciliation in
                    # GET /api/conversations/{id} sees is_running=True and
                    # refuses to reset a zombie deathmatch_status="active".
                    _agent_state.is_running = False
                    await _agent_state.broadcast_done()
                    logger.info(
                        "Stop-cancel conv=%s content=%d reasoning=%d segments=%d tools=%d state_content=%d dm=%s",
                        _cap_conversation_id, len(assistant_content), len(assistant_reasoning),
                        len(content_segments), len(tool_results_accumulated),
                        len(_agent_state.content), _deathmatch_mgr is not None,
                    )
                    # Deliberate stop (POST /stream/stop/{id}) or passive
                    # disconnect cancels the task mid-reply: persist the
                    # partial content accumulated so far, or the conversation
                    # shows only the user message and the generated text is
                    # lost (chat.spec "can stop a streaming response").
                    # Mirror the self-save path: fresh session (the request's
                    # `db` may be mid-cancellation) + empty-content guard.
                    if _deathmatch_mgr is None:
                        try:
                            _stop_partial_text = _strip_dsml_all(assistant_content).strip()
                            _stop_segments = [s for s in content_segments if s and s.strip()]
                            if _stop_partial_text:
                                _stop_segments.append(_stop_partial_text)
                            _stop_content = "\n\n<!-- segment_split -->\n\n".join(_stop_segments)
                            if not _stop_content.strip():
                                # Response-audit wipe: the rejected draft was
                                # streamed to the client then discarded from the
                                # local accumulators (regeneration is the only
                                # authoritative content). If the user stops
                                # during regeneration, nothing else is left to
                                # persist — fall back to what the client
                                # actually received (_agent_state.content is
                                # append-only and survives the audit reset).
                                _stop_content = _agent_state.content or ""
                            _stop_tool_results = (
                                json.dumps(tool_results_accumulated, ensure_ascii=False)
                                if tool_results_accumulated else None
                            )
                            _stop_reasoning = (assistant_reasoning or _agent_state.reasoning or "").strip() or None
                            # A4.9 I2: stop-interrupted persistence is another
                            # final-answer surface — the glitch lands on the
                            # FIRST token, and a mid-reply stop would persist
                            # it. Strip at the joined-content head (covers the
                            # segment join AND the _agent_state fallback).
                            _stop_content = _strip_leading_orphan_colon(_strip_leading_orphan_punct(_stop_content))
                            if _stop_content.strip() or _stop_tool_results or _stop_reasoning:
                                _stop_content, _stop_tool_results = await _localize_media_for_persist(
                                    _stop_content, _stop_tool_results,
                                    _user_id_local, _username_local,
                                )
                                async with AsyncSessionLocal() as _stop_db:
                                    _stop_conv = (await _stop_db.execute(
                                        select(Conversation).where(
                                            Conversation.id == _cap_conversation_id
                                        )
                                    )).scalar_one_or_none()
                                    if _stop_conv is not None:
                                        _stop_msg = Message(
                                            conversation_id=_cap_conversation_id,
                                            role="assistant",
                                            content=_stop_content,
                                            reasoning_content=_stop_reasoning,
                                            tool_results=_stop_tool_results,
                                        )
                                        _stop_db.add(_stop_msg)
                                        _stop_conv.updated_at = datetime.utcnow()
                                        await _stop_db.commit()
                                        await _stop_db.refresh(_stop_msg)
                                        try:
                                            await _stream_buf.mark_complete(str(_stop_msg.id))
                                        except Exception:
                                            pass
                                        logger.info(
                                            "Persisted partial reply after stop for conversation %s (%d chars)",
                                            _cap_conversation_id, len(_stop_content),
                                        )
                        except Exception:
                            logger.exception(
                                "Failed to persist partial reply after stop for conversation %s",
                                _cap_conversation_id,
                            )
                    try:
                        await _stream_buf.mark_complete(None)
                    except Exception:
                        pass
                    # DEATHMATCH (conv 6b0faf81): a cancelled/errored goal loop
                    # must NEVER leave deathmatch_status="active" with no
                    # terminal message — the user sees a silent stop. Persist
                    # a paused terminal state + reason so the conversation is
                    # visibly resumable instead of a zombie that only the
                    # reconcile flips 18 minutes later.
                    await _persist_deathmatch_failure(
                        _cap_conversation_id, _deathmatch_mgr,
                        "运行被取消",
                        "死磕模式运行被取消。发送任意消息可继续推进目标。",
                        expected_agent_state=_agent_state,
                    )
                    logger.info("Agent loop task cancelled for conversation %s", _cap_conversation_id)
                    raise
                except Exception as exc:
                    logger.exception("Agent loop task failed: %s", exc)
                    _agent_state.is_running = False
                    _agent_state.error = str(exc)
                    await _agent_state.broadcast({"_failed": str(exc)})
                    await _agent_state.broadcast_done()
                    try:
                        await _stream_buf.mark_complete(None)
                    except Exception:
                        pass
                    # DEATHMATCH (conv 6b0faf81): persist the failure as a
                    # visible paused terminal state instead of a silent
                    # active→paused zombie flip by the reconcile later. The
                    # user must see WHY the goal loop stopped and that they
                    # can resume.
                    await _persist_deathmatch_failure(
                        _cap_conversation_id, _deathmatch_mgr,
                        f"运行异常中断: {type(exc).__name__}: {str(exc)[:300]}",
                        (
                            "死磕模式运行异常中断。可发送任意消息继续推进目标；"
                            "若反复中断请检查模型服务或调整目标。"
                        ),
                        expected_agent_state=_agent_state,
                    )
                finally:
                    try:
                        if task_db is not None:
                            await task_db.close()
                    except Exception:
                        pass
                    # Identity-checked: a stale task (its slot re-reserved by
                    # a newer request) must never pop the new run's state.
                    await _agent_registry.unregister(_cap_conversation_id, expected=_agent_state)

            _agent_task = asyncio.create_task(_agent_loop_task(), name=f"agent-loop-{conversation_id[:8]}")
            nonlocal detached_agent_task
            detached_agent_task = _agent_task
            _track_detached_task(_agent_task, _cap_conversation_id)

            try:
                while True:
                    try:
                        # 10s server-side keepalive — must stay well under the
                        # frontend's 30s stall watchdog. Agent phases that emit
                        # no events on their own (e.g. the tool-digest batch,
                        # up to 300s) must never look like a dead connection.
                        event = await asyncio.wait_for(_agent_queue.get(), timeout=10.0)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": json.dumps({"ping": True})}
                        if client_disconnected:
                            continue
                        # In deathmatch mode, be more tolerant of slow agent
                        # iterations — the agent is detached and should keep
                        # running in the background regardless of the SSE
                        # viewer connection.
                        _dm_active = bool(_deathmatch_mgr and _deathmatch_mgr.is_active)
                        if _dm_active:
                            continue
                        try:
                            if await req.is_disconnected():
                                client_disconnected = True
                                logger.warning("Client disconnected during tool loop, agent continues in background")
                        except Exception:
                            pass
                        continue

                    if isinstance(event, object) and not isinstance(event, dict):
                        break

                    if "_completed" in event:
                        completed = event["_completed"]
                        # conv 3b58af5b (R4): the producer already sanitized,
                        # localized and persisted the answer BEFORE broadcasting.
                        # Single-writer: skip every consumer-side save path and
                        # go straight to the done emission — whether or not this
                        # client is still connected.
                        if completed.get("message_id"):
                            full_response = _build_content_fallback(
                                completed["joined_content"], completed.get("tool_results_json")
                            )
                            full_reasoning = completed["full_reasoning"]
                            tool_results_json = completed.get("tool_results_json")
                            tool_calls_json = completed.get("tool_calls_json")
                            assistant_message_id = completed["message_id"]
                            response_saved = True
                            # A4.9 fix (Important-2): keep chat_sessions
                            # bookkeeping on the now-default producer-persist
                            # path (legacy save path maintained this counter).
                            try:
                                message_count += 1
                                if chat_session is not None:
                                    chat_session.message_count = message_count
                                    await db.commit()
                            except Exception:
                                logger.exception(
                                    "chat_session message_count update failed after producer persist"
                                )
                            logger.info(
                                "Final answer already persisted by producer (msg %s) — skipping consumer save",
                                assistant_message_id,
                            )
                            try:
                                async for done_event in _finalize_and_emit_done(
                                    assistant_message_id=assistant_message_id,
                                    final_content=full_response,
                                    final_tool_results_json=tool_results_json,
                                    fallback=False,
                                ):
                                    yield done_event
                            except Exception as done_exc:
                                logger.exception("Failed to emit done event: %s", done_exc)
                                yield {
                                    "event": "done",
                                    "data": json.dumps({
                                        "conversation_id": conversation_id,
                                        "message_id": assistant_message_id,
                                        "title": conversation.title if conversation else None,
                                        "tool_results": None,
                                        "done": True,
                                        "fallback": False,
                                    }),
                                }
                            return
                        _cap_content = _build_content_fallback(completed["joined_content"], completed.get("tool_results_json"))
                        # Citation ledger verify step: strip [N] markers that
                        # reference no real fetched source before persistence
                        # (grounded-citations port — frontend cannot catch
                        # out-of-range [N] when any search ran). Sanitizes the
                        # content AND the tool_results content_segments /
                        # display_sequence text surfaces in one judgment.
                        _cap_content, _cap_tool_results = await _sanitize_cited_content(
                            _cap_content, completed.get("tool_results_json"),
                        )
                        _cap_reasoning = completed["full_reasoning"]
                        _cap_tool_calls = completed.get("tool_calls_json")

                        # Localize remote media the agent decided to display
                        # (content-addressed into the user's workspace).
                        _cap_content, _cap_tool_results = await _localize_media_for_persist(
                            _cap_content, _cap_tool_results, _user_id_local, _username_local,
                        )

                        full_response = _cap_content
                        full_reasoning = _cap_reasoning
                        tool_results_json = _cap_tool_results
                        tool_calls_json = _cap_tool_calls

                        if client_disconnected:
                            logger.info("Agent loop completed after disconnect, saving results via background task")
                            _bg_buf = _stream_buf
                            async def _bg_save():
                                # Guard: never persist a completely empty bubble
                                # (mirrors the self-save guard above).
                                if not (_cap_content or "").strip() and not _cap_tool_results and not _cap_tool_calls:
                                    logger.info("Post-disconnect save skipped: empty payload")
                                    try:
                                        await _bg_buf.mark_complete(None)
                                    except Exception:
                                        pass
                                    return
                                for attempt in range(3):
                                    try:
                                        async with AsyncSessionLocal() as bg_db:
                                            conv_row = (await bg_db.execute(
                                                select(Conversation).where(Conversation.id == _cap_conversation_id)
                                            )).scalar_one_or_none()
                                            if conv_row is None:
                                                return
                                            msg = Message(
                                                conversation_id=_cap_conversation_id,
                                                role="assistant",
                                                content=_cap_content,
                                                reasoning_content=_cap_reasoning if _cap_reasoning else None,
                                                tool_results=_cap_tool_results,
                                                tool_calls=_cap_tool_calls,
                                            )
                                            bg_db.add(msg)
                                            conv_row.updated_at = datetime.utcnow()
                                            await bg_db.commit()
                                            await bg_db.refresh(msg)
                                            try:
                                                await _bg_buf.mark_complete(str(msg.id))
                                            except Exception:
                                                pass
                                            try:
                                                _final_title = await _generate_title_bg(bg_db, conv_row, _cap_content)
                                                if _final_title:
                                                    try:
                                                        if _bg_buf.done_data is None:
                                                            _bg_buf.done_data = {}
                                                        _bg_buf.done_data["title"] = _final_title
                                                    except Exception:
                                                        pass
                                            except Exception:
                                                logger.exception("Background title generation failed (post-disconnect path)")
                                            logger.info("Background save succeeded on attempt %d (post-disconnect)", attempt + 1)
                                            return
                                    except Exception:
                                        import traceback as _tb
                                        _tb.print_exc()
                                        await asyncio.sleep(2.0)
                                logger.error("Background save failed after 3 attempts")
                            try:
                                _bg = asyncio.get_running_loop().create_task(_bg_save())
                                _track_bg_task(_bg, _cap_conversation_id, "post_disconnect_save")
                            except Exception:
                                pass
                            response_saved = True
                            return

                        # Resolve the final assistant message id. The variable
                        # must be BOUND (None) BEFORE the save attempt: a save
                        # failure (e.g. FK violation when the conversation was
                        # deleted mid-stream) falls through to
                        # _finalize_and_emit_done below, and an unbound
                        # reference would crash the whole SSE generator
                        # without a done/error event — the client loses the
                        # stream (and with the old broadcast-before-append
                        # ordering, part of the displayed answer with it)
                        # while the backend keeps the answer in the buffer.
                        assistant_message_id: str | None = None
                        try:
                            sanitized_content = _cap_content if _cap_content else _build_content_fallback("", _cap_tool_results)
                            # When the deathmatch terminal status message
                            # (partial_complete/human_gate/paused) was already
                            # persisted with text + deliverables, an empty
                            # fallback bubble would only duplicate its
                            # attachments — skip it (conv aadb26a3 ending).
                            _skip_empty_final = (
                                not sanitized_content
                                and _dm_terminal_status_saved
                            )
                            if sanitized_content or (_cap_tool_results and not _skip_empty_final):
                                assistant_message = Message(
                                    conversation_id=conversation_id,
                                    role="assistant",
                                    content=sanitized_content,
                                    reasoning_content=_cap_reasoning or None,
                                    tool_results=_cap_tool_results,
                                    tool_calls=_cap_tool_calls,
                                )
                                db.add(assistant_message)
                                conversation.updated_at = datetime.utcnow()
                                await db.commit()
                                await db.refresh(assistant_message)
                                response_saved = True
                                message_count += 1
                                assistant_message_id = assistant_message.id

                                chat_session.message_count = message_count
                                await db.commit()

                                try:
                                    await _stream_buf.mark_complete(str(assistant_message_id))
                                except Exception:
                                    pass
                            else:
                                try:
                                    await _stream_buf.mark_complete(None)
                                except Exception:
                                    pass
                                async for done_event in _finalize_and_emit_done(
                                    assistant_message_id=None,
                                    final_content="",
                                    final_tool_results_json=None,
                                    fallback=True,
                                ):
                                    yield done_event
                                return
                        except Exception as save_exc:
                            logger.exception("Failed to save tool-loop assistant message: %s", save_exc)
                            _spawn_interrupted_save(_stream_buf)

                        try:
                            async for done_event in _finalize_and_emit_done(
                                assistant_message_id=assistant_message_id,
                                final_content=full_response,
                                final_tool_results_json=tool_results_json,
                                fallback=assistant_message_id is None,
                            ):
                                yield done_event
                        except Exception as done_exc:
                            logger.exception("Failed to emit done event: %s", done_exc)
                            yield {
                                "event": "done",
                                "data": json.dumps({
                                    "conversation_id": conversation_id,
                                    "message_id": assistant_message_id,
                                    "title": conversation.title if conversation else None,
                                    "tool_results": None,
                                    "done": True,
                                    "fallback": assistant_message_id is None,
                                }),
                            }
                        return

                    if "_failed" in event:
                        if not client_disconnected:
                            yield {"event": "error", "data": json.dumps({"error": f"Agent管线故障: {event['_failed']}"})}
                        return

                    if client_disconnected:
                        continue

                    if "part_started" in event:
                        # F1-1: push new timeline part (v2 clients). Payload is
                        # wrapped in the type key — the frontend parser sniffs
                        # data payload keys and ignores the `event:` line.
                        yield {"event": "part_started", "data": json.dumps({"part_started": event["part_started"]}, ensure_ascii=False)}

                    elif "part_delta" in event:
                        # F1-1: incremental append to an existing part (v2 clients)
                        yield {"event": "part_delta", "data": json.dumps({"part_delta": event["part_delta"]}, ensure_ascii=False)}

                    elif "part_updated" in event:
                        # F1-1: structured state transition for an existing part
                        yield {"event": "part_updated", "data": json.dumps({"part_updated": event["part_updated"]}, ensure_ascii=False)}

                    elif "reasoning_content" in event:
                        # PHASE 5: forward the phase marker ("final" for
                        # _final_thinking / grace-call output) so the
                        # frontend can route iteration vs final reasoning.
                        _payload = {"reasoning_content": event["reasoning_content"]}
                        if event.get("phase"):
                            _payload["phase"] = event["phase"]
                        yield {"event": "reasoning_content", "data": json.dumps(_payload)}

                    elif "content" in event:
                        yield {"event": "content", "data": json.dumps({"content": event["content"]})}

                    elif "content_segment" in event:
                        yield {"event": "content_segment", "data": json.dumps({"segment_content": event["content_segment"]})}

                    elif "tool_call" in event:
                        tc = event["tool_call"]
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "agent_step": {
                                    "name": tc.get("name", "unknown"),
                                    "title": _TOOL_STEP_TITLES.get(tc.get("name", ""), f"工具调用: {tc.get('name', '')}"),
                                    "content": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                                    "step_type": "tool_call",
                                },
                                "done": False,
                            }),
                        }
                        yield {"event": "tool_call", "data": json.dumps(tc)}
                        message_count += 1

                    elif "tool_result" in event:
                        tr = event["tool_result"]
                        yield {"event": "tool_result", "data": json.dumps(tr)}

                    elif "attachments" in event:
                        # Accumulated download-card set (conv fbf5779b: the
                        # per-call subsets previously emitted here replaced
                        # the whole set on the client, so the live count was
                        # wrong — the tool loop now streams the accumulated
                        # set instead).
                        yield {
                            "event": "message",
                            "data": json.dumps({"attachments": event["attachments"], "done": False}),
                        }

                    elif "iteration" in event:
                        yield {"event": "iteration", "data": json.dumps(event["iteration"])}

                    elif "ping" in event:
                        yield {"event": "ping", "data": json.dumps({"ping": True})}

                    elif "agent_step" in event:
                        yield {"event": "message", "data": json.dumps({"agent_step": event["agent_step"], "done": False})}

                    elif "context_info" in event:
                        yield {"event": "message", "data": json.dumps({"context_info": event["context_info"], "done": False})}

                    elif "error" in event:
                        yield {"event": "error", "data": json.dumps({"error": event["error"]})}

                    elif "deathmatch_verdict" in event:
                        _dm = event["deathmatch_verdict"]
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "deathmatch_verdict": _dm,
                                "done": False,
                            }),
                        }

                    elif "permission_request" in event:
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "permission_request": event["permission_request"],
                                "done": False,
                            }),
                        }

                    try:
                        if not client_disconnected and await req.is_disconnected():
                            client_disconnected = True
                            try:
                                await _stream_buf.set_sse_active(False)
                            except Exception:
                                pass
                            logger.warning("Client disconnected during tool loop, agent continues in background")
                    except Exception:
                        pass

            except asyncio.CancelledError:
                if not _agent_task.done():
                    _sse_cancelled.set()
                    try:
                        await _stream_buf.set_sse_active(False)
                    except Exception:
                        pass
                    logger.warning("SSE cancelled but agent loop still running — it will complete and self-save results")
                raise

        # ---- Tool-calling loop path (AgentLoop is the only pipeline) ---
        _config = get_config()

        async def _release_setup_slot():
            """Free the 4.8 session-lock slot + stream buffer for a run that
            died before its detached agent task was spawned. The slot is
            claimed by reserve() at request start; if the SSE is cancelled
            (or setup throws) before task creation, nothing else ever
            unregisters it — the next /stream for this conversation is then
            rejected with conversation_busy (the stop → edit → resend → no
            answer bug). Running agents own both and clean up in their own
            CancelledError handler / finally, so this must only fire when no
            agent task was created."""
            if _reserved_agent_state is not None:
                try:
                    await _agent_registry.release_dead_setup(conversation_id, _reserved_agent_state)
                except Exception:
                    pass
            try:
                _dead_buf = await stream_buffer_manager.get_buffer(conversation_id, current_user.id)
                if _dead_buf is not None and _dead_buf.is_running:
                    await _dead_buf.mark_complete(None)
            except Exception:
                pass

        async def _release_setup_slot_safely():
            # A second cancellation during the cleanup awaits would re-raise
            # CancelledError out of the handler and re-leak the slot; the
            # shield lets the cleanup run to completion.
            try:
                await asyncio.shield(_release_setup_slot())
            except asyncio.CancelledError:
                pass

        async def _recover_tool_loop():
            """Re-drive the tool loop after a setup-phase SSE abort.

            When the SSE is cancelled during setup, the detached agent task
            does not exist yet — the run would otherwise die permanently and
            the already-saved user message would never get an answer (conv
            b078987b, 2026-08-03: 20s-in visibilitychange abort → dead-setup
            release → no answer ever produced). Re-invoking ``_run_tool_loop``
            in a background task: setup re-runs with fresh sessions, the
            (still-ours) reservation is adopted, the agent loop runs, and on
            completion the relay's post-disconnect path self-saves with a
            fresh session. If the stop endpoint already released the slot
            (deliberate stop), ``adopt_reservation`` returns None and the
            recovery dies with conversation_superseded — correct stop
            semantics preserved.
            """
            try:
                async for _ev in _run_tool_loop():
                    pass  # drain — no SSE client; agent self-saves on completion
            except asyncio.CancelledError:
                # The recovery task itself was cancelled (e.g. app shutdown).
                # Release the adopted slot so the conversation is never
                # wedged behind a dead running-claim (the cleanup loop only
                # reaps PROVISIONAL entries; an adopted one would leak
                # conversation_busy forever). Identity-checked: never pops a
                # slot re-reserved by a newer request.
                await _release_setup_slot_safely()
            except Exception:
                logger.exception(
                    "Setup-recovery tool loop failed for conversation %s",
                    conversation_id,
                )
                await _release_setup_slot_safely()

        _recovery_task: asyncio.Task | None = None

        try:
            async for event in _run_tool_loop():
                yield event
        except asyncio.CancelledError:
            # Stop/abort during setup: CancelledError (BaseException in 3.8+)
            # bypasses `except Exception` below. The loop-phase handler inside
            # _run_tool_loop already deals with the agent-running case; here
            # the run died pre-handoff. Releasing the slot AND killing the run
            # leaves the saved user message permanently unanswered — instead
            # re-drive the tool loop detached so the answer is produced and
            # self-saved. The slot is NOT released here: the recovery run
            # adopts it (identity-checked); a deliberate stop released it
            # already, so the recovery aborts cleanly via supersession.
            if detached_agent_task is None and _recovery_task is None:
                _slot_still_ours = False
                try:
                    _slot_still_ours = (
                        _reserved_agent_state is not None
                        and _agent_registry.get_local(conversation_id) is _reserved_agent_state
                    )
                except Exception:
                    _slot_still_ours = False
                if _slot_still_ours:
                    _recovery_task = asyncio.create_task(_recover_tool_loop())
                    _track_bg_task(_recovery_task, conversation_id, "setup_recovery")
                else:
                    # Stop endpoint already released the slot (deliberate
                    # stop) — no recovery; keep the dead-setup release so the
                    # conversation is not wedged.
                    await _release_setup_slot_safely()
            raise
        except Exception:
            # 4.8 session lock: setup died before the reservation handoff —
            # release the slot (identity-checked, adopted-or-provisional), so
            # the conversation is never wedged behind a dead running-claim.
            if detached_agent_task is None:
                await _release_setup_slot_safely()
            raise
        try:
            from app.services.proactive_learning_service import ProactiveLearningService
            _pls = ProactiveLearningService()
            await _pls.update_user_model(db, current_user.id)
        except Exception:
            pass

    await db.commit()
    try:
        db.expunge(conversation)
    except Exception:
        pass
    await db.close()

    return EventSourceResponse(event_generator())


@router.post("/stream/resume")
async def resume_stream(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Reconnect to an SSE stream via the persistent StreamBuffer.

    Accepts JSON body: {"conversation_id": "xxx"}
    Uses subscribe_with_snapshot() for atomic replay + subscribe —
    no duplication gap between replay and live deltas.
    """
    body = await request.json()
    conversation_id = body.get("conversation_id")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id required")

    buf = await stream_buffer_manager.get_buffer(conversation_id, current_user.id)

    _TOOL_STEP_TITLES = {
        "web_search": "联网检索结果",
        "browser": "浏览网页结果",
        "execute_code": "代码执行结果",
        "memory": "记忆操作",
        "delegate_task": "子任务执行结果",
        "context7_resolve_library_id": "查找库文档ID",
        "context7_query_docs": "查询库文档",
    }

    def _forward_delta(delta):
        """Convert a buffer delta to SSE yield dict, or None."""
        if delta["type"] == "part_started":
            return {"event": "part_started", "data": json.dumps({"part_started": delta["data"]}, ensure_ascii=False)}
        elif delta["type"] == "part_delta":
            return {"event": "part_delta", "data": json.dumps({"part_delta": delta["data"]}, ensure_ascii=False)}
        elif delta["type"] == "part_updated":
            return {"event": "part_updated", "data": json.dumps({"part_updated": delta["data"]}, ensure_ascii=False)}
        elif delta["type"] == "content":
            return {"event": "content", "data": json.dumps({"content": delta["data"]})}
        elif delta["type"] == "reasoning":
            return {"event": "reasoning_content", "data": json.dumps({"reasoning_content": delta["data"]})}
        elif delta["type"] == "content_segment":
            return {"event": "content_segment", "data": json.dumps({"segment_content": delta["data"]})}
        elif delta["type"] == "tool_call":
            tc = delta["data"]
            return [
                {
                    "event": "message",
                    "data": json.dumps({
                        "agent_step": {
                            "name": tc.get("name", "unknown"),
                            "title": _TOOL_STEP_TITLES.get(tc.get("name", ""), f"工具调用: {tc.get('name', '')}"),
                            "content": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                            "step_type": "tool_call",
                        },
                        "done": False,
                    }),
                },
                {"event": "tool_call", "data": json.dumps(tc)},
            ]
        elif delta["type"] == "tool_result":
            tr = delta["data"]
            return [{"event": "tool_result", "data": json.dumps(tr)}]
        elif delta["type"] == "iteration":
            return {"event": "iteration", "data": json.dumps(delta["data"])}
        elif delta["type"] == "agent_step":
            return {
                "event": "message",
                "data": json.dumps({"agent_step": delta["data"], "done": False}),
            }
        elif delta["type"] == "context_info":
            return {
                "event": "message",
                "data": json.dumps({"context_info": delta["data"], "done": False}),
            }
        elif delta["type"] == "attachments":
            # Accumulated download-card set streamed by the tool loop
            # (conv fbf5779b — the per-call subsets used to replace the whole
            # client set, making the live count wrong mid-stream).
            return {
                "event": "message",
                "data": json.dumps({"attachments": delta["data"], "done": False}),
            }
        elif delta["type"] == "permission_request":
            return {
                "event": "message",
                "data": json.dumps({"permission_request": delta["data"], "done": False}),
            }
        elif delta["type"] == "search_progress":
            return {
                "event": "message",
                "data": json.dumps({"search_progress": delta["data"], "done": False}),
            }
        elif delta["type"] == "search_failed":
            return {
                "event": "message",
                "data": json.dumps({"search_failed": delta["data"], "done": False}),
            }
        elif delta["type"] == "db_message_id":
            return {
                "event": "message",
                "data": json.dumps({"db_message_id_update": delta["data"], "done": False}),
            }
        elif delta["type"] == "reasoning_segment":
            # Mid-stream "thinking" block flushed between answer text and a
            # tool call. Surfaced to the client as an agent_step so it is
            # rendered inline by MessageBubble's displaySequence.
            item = delta["data"] or {}
            return {
                "event": "message",
                "data": json.dumps({
                    "agent_step": {
                        "name": "reasoning_step",
                        "title": item.get("title") or "💭 思考过程",
                        "content": item.get("content") or "",
                        "step_type": "reasoning_step",
                    },
                    "done": False,
                }),
            }
        return None

    async def _resolve_resume_title() -> str | None:
        """Return the conversation's final title for inclusion in the resume
        `done` payload, so the frontend can mark the conversation as finished
        without waiting for a separate title_update event (which never
        arrives on the resume code path)."""
        try:
            if buf and buf.done_data and buf.done_data.get("title"):
                return buf.done_data["title"]
        except Exception:
            pass
        try:
            async with AsyncSessionLocal() as _tdb:
                row = (await _tdb.execute(
                    select(Conversation).where(Conversation.id == conversation_id)
                )).scalar_one_or_none()
                if row and row.title and row.title != "新对话":
                    return row.title
        except Exception:
            logger.exception("Failed to load conversation title for resume")
        return None

    async def resume_generator():
        if buf:
            # ── Buffered path (primary) ──────────────────────────
            # Zombie guard: a buffer claiming is_running must be backed by a
            # LIVE agent state. If the agent finished/crashed and only the
            # stale buffer snapshot survives (e.g. a hard-killed worker left
            # its shared-state row behind), the `while buf.is_running` loop
            # below would ping the client every 10s forever — the frontend's
            # 30s watchdog is satisfied by the pings, so the streaming state
            # pins indefinitely and new queries are blocked (conv 149ce886,
            # 2026-08-01). Serve the replay then terminate immediately; the
            # frontend refreshes from the DB and clears streaming.
            if buf.is_running:
                _live_state = await _agent_registry.get(conversation_id)
                if _live_state is not None and _live_state.provisional:
                    _live_state = None
                if _live_state is None:
                    logger.warning(
                        "Resume zombie guard: buffer claims running for %s "
                        "but no live agent state — serving replay + terminal",
                        conversation_id,
                    )
                    sub_queue, replay = await buf.subscribe_with_snapshot()
                    try:
                        yield {
                            "event": "message",
                            "data": json.dumps({
                                "replay": replay,
                                "status": "none",
                                "is_running": False,
                                "done": False,
                            }),
                        }
                    finally:
                        await buf.unsubscribe(sub_queue)
                    yield {
                        "event": "done",
                        "data": json.dumps({
                            "conversation_id": conversation_id,
                            "message_id": None,
                            "title": None,
                            "tool_results": None,
                            "done": True,
                        }),
                    }
                    # Heal the zombie: mark the buffer complete (is_running=False
                    # + delete the shared-state row) so the snapshot loop stops
                    # re-owning it and new /stream calls are no longer blocked
                    # with conversation_busy.
                    try:
                        await buf.mark_complete(None)
                    except Exception:
                        logger.warning(
                            "Resume zombie guard: failed to heal buffer for %s",
                            conversation_id, exc_info=True,
                        )
                    return
            sub_queue, replay = await buf.subscribe_with_snapshot()

            yield {
                "event": "message",
                "data": json.dumps({
                    "replay": replay,
                    "status": replay["status"],
                    "is_running": replay["is_running"],
                    "done": False,
                }),
            }

            if replay["status"] == "complete":
                done_data = replay.get("done_data") or {}
                _title = await _resolve_resume_title()
                yield {
                    "event": "done",
                    "data": json.dumps({
                        "conversation_id": conversation_id,
                        "message_id": replay.get("db_message_id"),
                        "title": _title,
                        "tool_results": done_data.get("tool_results_json"),
                        "done": True,
                    }),
                }
                await buf.unsubscribe(sub_queue)
                return

            try:
                while buf.is_running:
                    # Check on every delta, not just ping timeouts: when the
                    # client is gone, yielding into the closed response raises
                    # ASGI errors (observed 2026-07-21/24). Mirrors the main
                    # generator's per-event disconnect check.
                    try:
                        if await request.is_disconnected():
                            logger.info("Resume client disconnected, ending resume stream")
                            return
                    except Exception:
                        return
                    try:
                        # 10s keepalive — must stay well under the frontend's
                        # 30s stall-watchdog threshold so a healthy resume
                        # stream is never mistaken for a dead connection.
                        delta = await asyncio.wait_for(sub_queue.get(), timeout=10.0)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": json.dumps({"ping": True})}
                        continue

                    if delta["type"] == "done":
                        done_info = delta["data"]
                        db_msg_id = buf.db_message_id
                        _title = await _resolve_resume_title()
                        yield {
                            "event": "done",
                            "data": json.dumps({
                                "conversation_id": conversation_id,
                                "message_id": db_msg_id,
                                "title": _title,
                                "tool_results": done_info.get("tool_results_json"),
                                "done": True,
                            }),
                        }
                        break

                    if delta["type"] == "error":
                        yield {"event": "error", "data": json.dumps({"error": delta["data"].get("error", "Unknown error")})}
                        break

                    result = _forward_delta(delta)
                    if result is not None:
                        if isinstance(result, list):
                            for ev in result:
                                yield ev
                        else:
                            yield result
            finally:
                await buf.unsubscribe(sub_queue)
            return

        # ── Fallback: legacy registry path ────────────────────
        state = await _agent_registry.get(conversation_id)
        # Provisional states are endpoint reservations in setup, not real
        # agents — subscribing here would hang on a possibly-dead claim.
        if state is not None and state.provisional:
            state = None
        if not state:
            yield {
                "event": "message",
                "data": json.dumps({
                    "replay": {
                        "content": "",
                        "reasoning": "",
                        "content_segments": [],
                        "display_sequence": [],
                        "tool_calls": [],
                        "tool_results": [],
                        "iteration": None,
                        "agent_steps": [],
                        "file_attachments": [],
                        "search_progress": [],
                        "search_failed": None,
                        "layer1": {"content": "", "reasoning": ""},
                        "layer2": {"content": "", "reasoning": ""},
                        "status": "none",
                        "db_message_id": None,
                    },
                    "status": "none",
                    "is_running": False,
                    "done": False,
                }),
            }
            yield {
                "event": "error",
                "data": json.dumps({"error": "No active agent for this conversation"}),
            }
            return

        snapshot = await state.snapshot()
        snapshot["layer1"] = {"content": snapshot.get("content", ""), "reasoning": snapshot.get("reasoning", "")}
        snapshot["layer2"] = {"content": "", "reasoning": ""}
        snapshot["status"] = "complete" if not snapshot["is_running"] and snapshot.get("completed_data") else "incomplete"
        snapshot["db_message_id"] = None

        if not snapshot["is_running"] and snapshot.get("completed_data"):
            completed = snapshot["completed_data"]
            yield {
                "event": "message",
                "data": json.dumps({
                    "replay": snapshot,
                    "status": "complete",
                    "is_running": False,
                    "completed": True,
                    "joined_content": completed.get("joined_content", ""),
                    "done": False,
                }),
            }
            yield {
                "event": "done",
                "data": json.dumps({
                    "conversation_id": conversation_id,
                    "message_id": None,
                    "title": None,
                    "tool_results": completed.get("tool_results_json"),
                    "done": True,
                }),
            }
            return

        if snapshot.get("error"):
            yield {
                "event": "message",
                "data": json.dumps({
                    "replay": snapshot,
                    "status": "incomplete",
                    "is_running": False,
                    "error": snapshot["error"],
                    "done": False,
                }),
            }
            yield {
                "event": "error",
                "data": json.dumps({"error": snapshot["error"]}),
            }
            return

        yield {
            "event": "message",
            "data": json.dumps({
                "replay": snapshot,
                "status": "incomplete",
                "is_running": True,
                "done": False,
            }),
        }

        sub_queue = await state.subscribe()
        try:
            while state.is_running:
                # Per-delta disconnect check — see the buffered path above.
                try:
                    if await request.is_disconnected():
                        logger.info("Resume client disconnected, ending resume stream")
                        return
                except Exception:
                    return
                try:
                    # 10s keepalive — see the buffered path above.
                    event = await asyncio.wait_for(sub_queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ping": True})}
                    continue

                if isinstance(event, object) and not isinstance(event, dict):
                    break

                if "_completed" in event:
                    completed = event["_completed"]
                    _cap_conv_id = conversation_id
                    # conv 3b58af5b (R4): when the producer already persisted
                    # the answer, the legacy resume path must NOT insert a
                    # duplicate row — just resolve the done event.
                    if completed.get("message_id"):
                        _r_title = None
                        try:
                            async with AsyncSessionLocal() as bg_db:
                                conv_row = (await bg_db.execute(
                                    select(Conversation).where(Conversation.id == _cap_conv_id)
                                )).scalar_one_or_none()
                                if conv_row is not None:
                                    _r_title = conv_row.title
                        except Exception:
                            pass
                        yield {
                            "event": "done",
                            "data": json.dumps({
                                "conversation_id": _cap_conv_id,
                                "message_id": completed["message_id"],
                                "title": _r_title,
                                "tool_results": completed.get("tool_results_json"),
                                "done": True,
                            }),
                        }
                        break
                    _cap_content = _build_content_fallback(completed.get("joined_content") or "", completed.get("tool_results_json"))
                    _cap_reasoning = completed.get("full_reasoning")
                    # Citation ledger verify step (same as the main flow): the
                    # legacy resume path persists _completed payloads directly,
                    # so sanitize content AND tool_results text surfaces here
                    # too (conv 8629bdfe lineage — dead [N] markers survived
                    # in display_sequence when only content was sanitized).
                    _cap_content, _cap_tr_json = await _sanitize_cited_content(
                        _cap_content, completed.get("tool_results_json"),
                    )

                    # Guard: never persist a completely empty bubble from the
                    # legacy resume path either (mirrors the main-flow guards).
                    if not (_cap_content or "").strip() and not (_cap_reasoning or "").strip() and not _cap_tr_json:
                        logger.info("Legacy resume _completed with empty payload; skipping save")
                        yield {
                            "event": "done",
                            "data": json.dumps({
                                "conversation_id": conversation_id,
                                "message_id": None,
                                "title": None,
                                "tool_results": None,
                                "done": True,
                            }),
                        }
                        break

                    msg_id = None
                    title = None
                    try:
                        async with AsyncSessionLocal() as bg_db:
                            conv_row = (await bg_db.execute(
                                select(Conversation).where(Conversation.id == _cap_conv_id)
                            )).scalar_one_or_none()
                            if conv_row:
                                msg = Message(
                                    conversation_id=_cap_conv_id,
                                    role="assistant",
                                    content=_cap_content,
                                    reasoning_content=_cap_reasoning if _cap_reasoning else None,
                                    tool_results=_cap_tr_json,
                                )
                                bg_db.add(msg)
                                conv_row.updated_at = datetime.utcnow()
                                await bg_db.commit()
                                await bg_db.refresh(msg)
                                msg_id = str(msg.id)
                                title = conv_row.title
                                try:
                                    buf_fallback = await stream_buffer_manager.get_buffer_no_auth(_cap_conv_id)
                                    if buf_fallback:
                                        await buf_fallback.mark_complete(msg_id)
                                except Exception:
                                    pass
                    except Exception:
                        logger.exception("Resume endpoint: failed to save completed results")

                    yield {
                        "event": "done",
                        "data": json.dumps({
                            "conversation_id": _cap_conv_id,
                            "message_id": msg_id,
                            "title": title,
                            "tool_results": _cap_tr_json,
                            "done": True,
                        }),
                    }
                    break

                if "_failed" in event:
                    yield {"event": "error", "data": json.dumps({"error": event["_failed"]})}
                    break

                if "reasoning_content" in event:
                    _payload = {"reasoning_content": event["reasoning_content"]}
                    if event.get("phase"):
                        _payload["phase"] = event["phase"]
                    yield {"event": "reasoning_content", "data": json.dumps(_payload)}
                elif "content" in event:
                    yield {"event": "content", "data": json.dumps({"content": event["content"]})}
                elif "content_segment" in event:
                    yield {"event": "content_segment", "data": json.dumps({"segment_content": event["content_segment"]})}
                elif "tool_call" in event:
                    tc = event["tool_call"]
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "agent_step": {
                                "name": tc.get("name", "unknown"),
                                "title": _TOOL_STEP_TITLES.get(tc.get("name", ""), f"工具调用: {tc.get('name', '')}"),
                                "content": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                                "step_type": "tool_call",
                            },
                            "done": False,
                        }),
                    }
                    yield {"event": "tool_call", "data": json.dumps(tc)}
                elif "tool_result" in event:
                    tr = event["tool_result"]
                    yield {"event": "tool_result", "data": json.dumps(tr)}
                elif "attachments" in event:
                    # Accumulated download-card set (conv fbf5779b — same as
                    # the live path; the old per-call subsets made the
                    # replayed card count wrong mid-stream).
                    yield {
                        "event": "message",
                        "data": json.dumps({"attachments": event["attachments"], "done": False}),
                    }
                elif "iteration" in event:
                    yield {"event": "iteration", "data": json.dumps(event["iteration"])}
                elif "ping" in event:
                    yield {"event": "ping", "data": json.dumps({"ping": True})}
                elif "agent_step" in event:
                    yield {"event": "message", "data": json.dumps({"agent_step": event["agent_step"], "done": False})}
                elif "error" in event:
                    yield {"event": "error", "data": json.dumps({"error": event["error"]})}

        finally:
            await state.unsubscribe(sub_queue)

    return EventSourceResponse(resume_generator())


@router.get("/stream/status/{conversation_id}")
async def stream_status(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Check if a conversation has an active stream buffer.

    Returns status information that the frontend uses to decide whether
    to resume streaming (incomplete) or read from DB (complete).
    """
    buf = await stream_buffer_manager.get_buffer(conversation_id, current_user.id)
    if buf:
        return {
            "has_buffer": True,
            "status": buf.status,
            "is_running": buf.is_running,
            "db_message_id": buf.db_message_id,
            "content_length": len(buf.content),
            "setup_in_progress": False,
        }
    # No buffer yet. The run may still be in its SETUP phase (coordinator
    # pre-pass / memory / workspace gather, or a detached setup-recovery
    # re-drive after an SSE abort — conv b078987b, 2026-08-03). A live
    # local registry state means an agent is claimed for this conversation
    # even though the buffer (created only when the agent task starts) does
    # not exist yet. Expose it so the frontend keeps waiting instead of
    # giving up after a few no-buffer polls and missing the self-saved
    # answer. Only IN-PROCESS states count — shared-state recovery rows are
    # distrusted by get_buffer, and the cleanup loop reaps expired
    # provisional entries after 180s, so this cannot wedge a conversation.
    _local_state = _agent_registry.get_local(conversation_id)
    _setup_live = bool(_local_state and _local_state.is_running)
    return {
        "has_buffer": False,
        "status": "incomplete" if _setup_live else "none",
        "is_running": _setup_live,
        "db_message_id": None,
        "content_length": 0,
        "setup_in_progress": _setup_live,
    }


@router.post("/stream/stop/{conversation_id}")
async def stop_agent_stream(
    conversation_id: str,
    current_user: User = Depends(get_current_user),
):
    """Explicitly cancel a running agent task for a conversation.

    This is the correct way for the frontend STOP button to cancel an agent.
    It does NOT rely on closing the SSE connection — closing the SSE would be
    indistinguishable from a passive client disconnect (tab switch, browser
    throttle, network blip), which must leave the detached agent running so it
    can self-save results. The agent task is tracked in
    ``_DETACHED_AGENT_TASKS`` and cancelled directly here.
    """
    task = _DETACHED_AGENT_TASKS.get(conversation_id)
    if task is not None and not task.done():
        task.cancel()
        logger.info("Stop endpoint: cancelled agent task for conversation %s", conversation_id)
        return {"status": "cancelled", "conversation_id": conversation_id}
    # No detached agent task: either the run is still in its setup phase (the
    # 4.8 session-lock slot is reserved but no task exists yet) or nothing is
    # running at all. A stop during setup previously left the slot claimed —
    # provisional → up to 180s of false conversation_busy, adopted → permanent —
    # so the next /stream (e.g. an edit+resend) was rejected with
    # conversation_busy and produced no answer. Release the slot + any
    # setup-created buffer so the conversation is usable again immediately;
    # the cancelled setup's adopt step then fails (conversation_superseded)
    # and the run terminates cleanly instead of spawning an agent.
    try:
        await _agent_registry.release_setup_slot(conversation_id)
    except Exception:
        pass
    # NOTE: no stream-buffer cleanup here. A buffer only ever exists once the
    # agent task is running (created inside _agent_loop_task), and the task
    # branch above already cancelled it — its CancelledError handler marks the
    # buffer complete. A recovered cross-worker buffer with is_running=True is
    # a TRUSTED live run on another worker (get_buffer applies its own zombie
    # distrust before returning it); marking it complete here would delete a
    # live run's recovery row and transiently permit a second concurrent run.
    return {"status": "not_running", "conversation_id": conversation_id}


@router.post("/permission/respond")
async def permission_respond(
    request_data: dict,
    current_user: User = Depends(get_current_user),
):
    """Respond to a pending permission request from the agent loop."""
    from app.services.permission_manager import respond_to_request
    request_id = request_data.get("request_id", "")
    approved = bool(request_data.get("approved", False))
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id is required")
    success = respond_to_request(request_id, approved)
    if not success:
        raise HTTPException(status_code=404, detail="Permission request not found or expired")
    return {"status": "ok", "approved": approved}

@router.post("/deathmatch/subgoal")
async def deathmatch_add_subgoal(
    request_data: dict,
    current_user: User = Depends(get_current_user),
):
    """Append an acceptance criterion to an ACTIVE deathmatch conversation
    (D3): the judge then checks every subgoal alongside the original goal,
    and the continuation prompt surfaces them so the agent works toward
    them. 400 when the conversation is not in deathmatch goal loop."""
    from app.db.database import Conversation, AsyncSessionLocal
    conversation_id = str(request_data.get("conversation_id") or "")
    text = str(request_data.get("text") or "").strip()
    if not conversation_id or not text:
        raise HTTPException(status_code=400, detail="conversation_id and text are required")
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="subgoal too long (max 500 chars)")
    async with AsyncSessionLocal() as db:
        conv = await db.get(Conversation, conversation_id)
        if conv is None or conv.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="conversation not found")
        if not conv.deathmatch_mode or conv.deathmatch_status != "active":
            raise HTTPException(status_code=400, detail="deathmatch goal loop is not active")
        subgoals = list(conv.deathmatch_subgoals or [])
        if len(subgoals) >= 20:
            raise HTTPException(status_code=400, detail="subgoal limit reached (20)")
        subgoals.append(text)
        conv.deathmatch_subgoals = subgoals
        await db.commit()
    return {"status": "ok", "subgoals": subgoals}
