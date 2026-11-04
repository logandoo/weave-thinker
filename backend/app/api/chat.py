# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete as sa_delete, select, update as sa_update
from sse_starlette.sse import EventSourceResponse
import asyncio
import json
import logging
import os
import time
from datetime import datetime

import re as _re

from app.db.database import get_db, Message, MessageAttempt, Conversation, User, Assistant, ChatSession
from app.db.database import AsyncSessionLocal
from app.schemas.chat import ChatRequest
from app.services.agent_service import AgentService, should_use_custom_model, _load_identity_memory_context
from app.services.query_sanitizer import extract_search_query_and_note_context
from app.services.title_generator import TitleGeneratorService
from app.services.workspace_service import ensure_user_workspace
from app.services.agent_loop import AgentLoop, _strip_dsml_all, _strip_leading_orphan_punct
from app.services.citation_ledger import CitationLedger, normalize_url
from app.services.citation_sources import extract_browser_sources
from app.services.reasoning_sanitizer import scrub_reasoning_meta, scrub_reasoning_streaming
from app.services.deathmatch_service import DeathmatchManager, MARKER_RE
from app.tools.skill_tools import (
    build_skills_system_prompt,
    build_force_injected_skills_prompt,
    resolve_skill,
    is_system_skill,
)
from app.tools.registry import registry

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
        # A4.9 R1 #5：identity 守卫——stop→edit-resend supersede 场景下旧 task
        # 的回调不得 pop 新 run 注册的条目（否则新 run 的 stop 找不到任务）。
        if _DETACHED_AGENT_TASKS.get(conv_id) is t:
            _DETACHED_AGENT_TASKS.pop(conv_id, None)
        # 兜底①（conv 5228db60）：task 以任何形态死亡（含 pre-main-try 取消
        # 逃逸）后，若 adopted 槽仍绑定在本 task 上即释放——identity 校验防
        # 误删新 run 的槽；强引用防 GC 中断（模式同 _clarification_tasks）。
        async def _release_orphan():
            try:
                await _agent_registry.release_if_task_dead(conv_id, expected_task=t)
            except Exception:
                logger.exception("Orphaned slot release failed for %s", conv_id)
        _fut = asyncio.ensure_future(_release_orphan())
        _INTERRUPT_BG_TASKS.add(_fut)
        _fut.add_done_callback(_INTERRUPT_BG_TASKS.discard)
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




_DIGEST_ARCHIVE_RE = _re.compile(r"【全文存档】(\S+)")
_DIGEST_TEXT_RE = _re.compile(r"<digest>([\s\S]*?)</digest>")
# <persisted-output> 包装（budget 层）：Full output saved to: <path>
_PERSISTED_ARCHIVE_RE = _re.compile(r"Full output saved to:\s*([^\n]+)")


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
        # budget 层 <persisted-output> 包装（未走 digest 的大结果）同样按指针恢复
        pm = _PERSISTED_ARCHIVE_RE.search(envelope)
        if not pm:
            return None
        fpath = pm.group(1).strip()
    else:
        fpath = m.group(1).strip()
    try:
        with open(fpath.strip(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and ("results" in data or "formatted" in data):
            return json.dumps(data, ensure_ascii=False)
    except (OSError, ValueError):
        pass
    return None


def _recover_digested_browser(envelope: str) -> str | None:
    """Return the original browser result JSON from the digest archive.

    M2：live 台账在 digest 之前从原始 JSON pages 注册 browser 来源；持久化
    端从全文存档恢复同一原始 JSON → 同一 URL 序列 → 编号逐位对齐。
    """
    m = _DIGEST_ARCHIVE_RE.search(envelope)
    if not m:
        # budget 层 <persisted-output> 包装（digest 关闭/失败时的大结果）：
        # live 注册发生在 budget 包装之前（原始 JSON），此处按指针恢复同源
        # JSON —— 编号逐位对齐（A4.9 Important-3）。
        pm = _PERSISTED_ARCHIVE_RE.search(envelope)
        if not pm:
            return None
        fpath = pm.group(1).strip()
    else:
        fpath = m.group(1).strip()
    try:
        with open(fpath.strip(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("pages"), list):
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

    Explicit-delivery contract (user directive 2026-09-18 prod conv 8b382ad8;
    revised 2026-09-27 for pdf_export, user ruling D-302, prod conv b8dcedc8):
    download cards come from the agent's **explicit delivery tools** —
    ``provide_file`` / ``provide_folder`` (2026-09-19) and ``pdf_export``
    (2026-09-27: a successful export IS the user-facing deliverable the user
    asked for; conv b8dcedc8 exported a note PDF that never surfaced as a
    card). Byproducts of ``terminal`` / ``execute_code`` / ``code_execution``
    are never surfaced: an analysis turn that clones or reads reference trees
    must not offer the analyzed files as downloads (prod: 278 cards from
    ``_refs/*``).

    Same-file dedup keeps a single card when the agent also calls
    ``provide_file`` on an already-exported file (name-keyed, largest size).

    Single source of truth shared by the persist-time transform
    (``_transform_tool_loop_results``) and the live SSE path, so the live card
    set always equals the persisted one (conv fbf5779b, 2026-08-06).

    Missing ``type`` is inferred from the file extension (files only — folder
    entries are structurally tagged); same-name files deduplicate keeping the
    largest reported size; folders deduplicate by ``rel_path``.
    """
    provided_attachments: list[dict] = []
    for tr in tool_results_accumulated:
        if tr.get("name") not in ("provide_file", "provide_folder", "pdf_export"):
            continue
        raw_result = tr.get("result", "")
        try:
            parsed = json.loads(raw_result) if raw_result else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if tr.get("name") == "pdf_export" and not parsed.get("success"):
            continue
        for gf in parsed.get("generated_files") or []:
            if isinstance(gf, dict):
                provided_attachments.append(gf)
        for gf in parsed.get("generated_folders") or []:
            if isinstance(gf, dict):
                if gf.get("type") != "folder":
                    gf = {**gf, "type": "folder"}
                provided_attachments.append(gf)

    if not provided_attachments:
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
    for att in provided_attachments:
        if att.get("type") == "folder":
            continue
        if not att.get("type") or att.get("type") == "file":
            fname = att.get("name") or att.get("filename") or ""
            ext = "." + fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            att["type"] = _ext_type_map.get(ext, "file")

    # Deduplicate attachments. Files: by name, keeping the largest reported
    # size so the final, complete file is offered (a document appended to
    # across multiple calls). Folders: by rel_path, same size rule.
    seen_names: dict[str, dict] = {}
    seen_folders: dict[str, dict] = {}
    for att in provided_attachments:
        if att.get("type") == "folder":
            key = att.get("rel_path") or att.get("name") or ""
            if not key:
                continue
            existing = seen_folders.get(key)
            if existing is None or (att.get("size") or 0) > (existing.get("size") or 0):
                seen_folders[key] = att
            continue
        fname = att.get("name") or att.get("filename") or ""
        if not fname:
            continue
        existing = seen_names.get(fname)
        if existing is None or (att.get("size") or 0) > (existing.get("size") or 0):
            seen_names[fname] = att
    return list(seen_names.values()) + list(seen_folders.values())


# M2（引文体系改造）：browser 来源抽取迁至 app.services.citation_sources
# （live 注册与持久化共用同一解析器/同一顺序 —— 编号逐位对齐）。
def _extract_browser_sources(raw_result: str):
    """向后兼容包装：返回 (url, title, snippet) 三元组迭代器。"""
    for url, title, snippet, _biblio in extract_browser_sources(raw_result):
        yield url, title, snippet


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
            if raw_result.startswith("<tool-digest>") or raw_result.startswith("<persisted-output>"):
                # A4.9 r2：budget 包装的大搜索结果同样按指针恢复（与 browser 同款）
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
                            # 引用格式波盲区①：作者/站点元数据必须随持久化透传，
                            # 否则存笔记主链路拿不到 author（作者-年制样式失效）。
                            "author": h.get("author"),
                            "site_name": h.get("site_name"),
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
            # M2（引文体系改造）：browser 来源按**调用顺序**入账（与 live ledger
            # 同序 —— results[id-1] == 模型 [id] 指向的 URL）。digest 信封用
            # 全文存档恢复原始 JSON（与 live 注册同源，保证 URL 序列一致）；
            # 书目字段随条目落库，供规范 reference 渲染。
            if not is_error:
                source_text = raw_result or ""
                if source_text.startswith("<tool-digest>") or source_text.startswith("<persisted-output>"):
                    recovered = _recover_digested_browser(source_text)
                    if recovered:
                        source_text = recovered
                for url, _btitle, _bsnippet, _biblio in extract_browser_sources(source_text):
                    _entry_key = normalize_url(url) or f"{_btitle}\x00{_bsnippet[:50]}"
                    if not url or _entry_key in _cite_seen:
                        continue
                    _cite_seen.add(_entry_key)
                    # C5（遗留清零）：与 live 注册同优先级——biblio 书目题名 > 页面 title
                    _biblio_title = (_biblio or {}).get("title") or ""
                    _entry: dict = {
                        "id": len(results_list) + 1,
                        "title": ((_biblio_title or _btitle) or "")[:200],
                        "url": url,
                        "snippet": _bsnippet,
                        "published_date": None,
                    }
                    from app.services.citation_ledger import BIBLIO_FIELDS as _BIBLIO

                    for _k, _v in (_biblio or {}).items():
                        if _v and _k in _BIBLIO and not _entry.get(_k):
                            _entry[_k] = _v
                    results_list.append(_entry)

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

    # F4/I3（2026-11-01 审计）：持久化步骤带上 status，避免"后端已知超时、
    # 回放却一律 completed"。判定复用后端同一个纯函数（工具自报 timed_out），
    # 前端 `deriveToolStatus` 只作旧数据的兜底。
    from app.services.tool_payload_status import payload_is_timeout as _payload_timeout
    # 复核 M1：预算折叠后的内容是 <persisted-output> 桩，不能再靠内容嗅探
    # （桩不以 "{" 开头）——优先用本次调用已在 SSE/台账侧算好的 error 标记。
    _err_by_call = {
        (tr.get("call_id") or ""): bool(tr.get("error"))
        for tr in tool_results_accumulated
        if tr.get("call_id")
    }
    for _step in agent_steps_list:
        if "status" in _step:
            continue
        _name = str(_step.get("name") or "")
        if _name in _err_by_call:
            _step["status"] = "error" if _err_by_call[_name] else "completed"
        else:
            # 兜底：老数据没有 error 标记；`_step["name"]` 是 call_id 而非工具名，
            # 因此这里不做工具白名单（形状校验仍要求 return_code==-1）。
            _step["status"] = (
                "error"
                if _payload_timeout(_step.get("content") or "")
                else "completed"
            )

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


class ProgressiveFlusher:
    """在途 assistant 行的周期刷写器（2026-10-06，conv ae9aa092 渐进落库）。

    run 开始即建 delivery_status='streaming' 的 assistant 行；本类按阈值把
    当前已流式正文刷进该行——进程被杀死（部署/崩溃）时用户刷新仍能看到
    部分正文（启动 sweep 翻 interrupted 后带徽标展示），而不是整段蒸发。

    - 触发：距上次刷写累计新增 ≥ min_chars，或距上次 ≥ min_seconds 且有增长；
    - 单 flight：在途写未完成时跳过（下一 tick 再试），绝不并发写同一行；
    - fail-open：写失败只记日志（流式主路径不受影响）。
    """

    def __init__(self, writer, *, clock=time.monotonic, min_chars: int = 2000,
                 min_seconds: float = 5.0):
        self._writer = writer
        self._clock = clock
        self._min_chars = min_chars
        self._min_seconds = min_seconds
        self._last_flush_at = clock()
        self._last_len = 0
        self._pending: "asyncio.Task | None" = None

    def feed(self, text: str) -> None:
        cur = len(text or "")
        if cur <= self._last_len:
            return
        grew = cur - self._last_len
        elapsed = self._clock() - self._last_flush_at
        if grew < self._min_chars and not (elapsed >= self._min_seconds and grew > 0):
            return
        if self._pending is not None and not self._pending.done():
            return  # 单 flight：在途写未完成，下个 tick 再刷
        self._last_len = cur
        self._last_flush_at = self._clock()
        snapshot = text
        try:
            self._pending = asyncio.get_running_loop().create_task(self._writer(snapshot))
        except Exception:
            logger.debug("progressive flush schedule failed (fail-open)", exc_info=True)

    def tick(self, text: str) -> None:
        """证据刷新（2026-11-01，conv 861cb5af）：纯工具型长任务正文长期为空，
        `feed()` 永远不触发 → 进程被杀时在途行连工具调用/结果都没有。

        这里按时间间隔强制刷一次**当前快照**（正文 + 工具调用/结果 + 思考由
        writer 从 run 状态取）；与 feed 共用单 flight 与最小间隔，避免写放大。
        """
        if self._clock() - self._last_flush_at < self._min_seconds:
            return
        if self._pending is not None and not self._pending.done():
            return
        self._last_flush_at = self._clock()
        self._last_len = len(text or "")
        snapshot = text
        try:
            self._pending = asyncio.get_running_loop().create_task(self._writer(snapshot))
        except Exception:
            logger.debug("progressive evidence tick failed (fail-open)", exc_info=True)

    def reset(self) -> None:
        """打回边界（audit_reset）：被拒草稿不进在途行（它已入 message_attempts
        台账）——累加基线归零，让下一稿的增量刷写正常工作。"""
        self._last_len = 0

    async def revert_to(self, text: str) -> None:
        """打回撤回（A4.9 r1 M1）：被拒草稿可能已被周期刷进在途行——先等在途
        写落地（保序），再把行内容撤回到本稿开始前基线，绝不让 SIGKILL 窗口里
        的 interrupted 残稿展示被审计否决的文字。"""
        self._last_len = len(text or "")
        if self._pending is not None and not self._pending.done():
            try:
                # scoped 复审 Minor：有界等待——在途写卡死不得拖住打回路径
                await asyncio.wait_for(asyncio.shield(self._pending), timeout=5.0)
            except Exception:
                pass
        try:
            await self._writer(text)
        except Exception:
            logger.debug("progressive revert failed (fail-open)", exc_info=True)








def _split_pre_tool_prose(assistant_content: str, gate, stitch_active: bool) -> tuple[str, str]:
    """工具边界处预工具散文的输出单元处置（用户裁定 2026-10-14）。

    「只有在真正是长输出被截断时才拼接，不然就是正常的多轮次输出。」

    - stitch_active=True（finish_reason=length 续写链，唯一合法拼接）：
      链上文本全量保留于终局答案（conv 4e159a79 小说 1-6 块契约），不停车。
    - 否则：预工具散文是独立输出单元——已展示部分（gate 释放/未武装直发）
      停车进 display_sequence 时间线（过程区），**不拼进终局答案**；
      从未展示的 hold 仍丢弃（conv 149ce886 占位/复述散文契约不变）。

    返回 (kept_for_final, parked_for_timeline)。held 恒为 assistant_content
    的未展示后缀；endswith 失配时保守视为全部已展示。"""
    if stitch_active:
        return assistant_content, ""
    unshown = getattr(gate, "held", "") or ""
    if not unshown:
        shown = assistant_content
    elif assistant_content.endswith(unshown):
        shown = assistant_content[: len(assistant_content) - len(unshown)]
    else:
        shown = assistant_content
    return "", shown


def _parked_prose_item(text: str) -> dict:
    """Build the persisted timeline item for already-shown pre-tool prose.

    P0 R2 (conv 08249cf0): the text must remain (A4.9 I2 — the user saw it),
    but it is a DRAFT/process unit, not a second answer.  ``superseded`` lets
    the frontend collapse it into the process/draft area while keeping the
    terminal answer as the only main content.
    """
    return {"type": "text", "content": text, "superseded": True}


def _drop_unshown_pre_tool_prose(assistant_content: str, gate) -> str:
    """兼容入口：非拼接链语义下工具边界的 kept 恒为空串（散文全部停车/丢弃）。
    保留函数名以稳定旧调用面；实际装配一律走 _split_pre_tool_prose。"""
    kept, _parked = _split_pre_tool_prose(assistant_content, gate, stitch_active=False)
    return kept


def _last_measured_prompt_tokens(messages) -> int:
    """Most recent provider-reported prompt_tokens from stored context_info.

    2026-10-18 (conv dca19082): seeds AgentLoop's compression calibration so
    the first iteration of a turn does not trust the inflated rough estimate.
    `context_info` is stored as a JSON string on assistant rows.
    """
    for msg in reversed(list(messages or [])):
        try:
            if getattr(msg, "role", "") != "assistant":
                continue
            ci = getattr(msg, "context_info", None)
            if isinstance(ci, str):
                ci = json.loads(ci) if ci.strip() else None
            if isinstance(ci, dict):
                # Only provider-measured values may seed calibration. Several
                # producers store rough estimates under the same `tokens` key;
                # seeding from one can suppress compression for the whole turn.
                if ci.get("measured") is not True:
                    continue
                tokens = ci.get("tokens")
                if tokens:
                    return int(tokens)
        except Exception:
            continue
    return 0


def _display_sequence_keep_evidence(seq):
    """压缩/重答时 display_sequence 的取舍——与客户端 dropDraftTextAfterLastTool
    **镜像对称**（A4.9 R1 Important-3）：保留 reasoning/tool 与最后一张工具卡之前
    的 text 项（cap_released 的预工具散文用户已看见，A4.9 I2 不得刷新即蒸发），
    仅丢弃最后一张工具卡之后的草稿 text 项。
    conv 038e837a：旧实现整表清空，把工具卡与思考一并抹掉，刷新后证据链缺失。"""
    items = list(seq or [])
    last_tool = -1
    for i in range(len(items) - 1, -1, -1):
        it = items[i]
        if isinstance(it, dict) and it.get("type") in (
            "tool_placeholder", "tool_call", "tool",
        ):
            last_tool = i
            break
    kept = []
    for i, it in enumerate(items):
        if (
            isinstance(it, dict)
            and it.get("type") == "text"
            and i > last_tool
            and not it.get("superseded")
        ):
            continue
        kept.append(it)
    return kept


from app.api.chat_persistence import (  # P5.3 move-only re-exports
    _delete_empty_streaming_row,
    _save_assistant_terminal,
    interjection_items_from_buffer,
    mark_interrupted_streams,
)

from app.api.chat_persistence import (
    _delete_message_slice,
    _persist_deathmatch_failure,
)
from app.api.chat_targets import _resolve_execution_target_for_conversation

from app.api.chat_citations import (  # P5.3 move-only re-export
    _build_content_fallback,
    _render_answer_citations,
    _sanitize_cited_content,
    strip_stop_citation_echo,
)


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
    qwen3.8@vLLM (model qwen3.8_27b) thinking-xhigh opened the final answer with a bare '：'
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
    """Load a conversation's messages in chronological (ASC) order.

    ``limit`` selects the NEWEST ``limit`` messages, still returned ASC —
    conv a104fbc5 (2026-09-20): ordering ASC + LIMIT returned the OLDEST 200
    of a 219-message conversation, so the newest user message never reached
    the model (it continued stale deathmatch context for an hour and then
    persisted an empty-answer failure). The downstream ``[-context_limit:]``
    slice cannot recover messages the query already dropped — the newest
    window must be taken here, at the SQL boundary.
    """
    # 渐进落库（2026-10-06，A4.9 r1 I2）：streaming 在途/僵尸行绝不进模型
    # 上下文（UI 不可见的半稿会污染下一轮）；interrupted 残稿正常进——
    # 让模型看到「该问题已被部分回答」，减轻重复回答（conv ae9aa092 Q3）。
    if limit is not None:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id,
                   Message.delivery_status != "streaming")
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = list(reversed(result.scalars().all()))
    else:
        stmt = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.delivery_status != "streaming",
        ).order_by(Message.created_at)
        result = await db.execute(stmt)
        rows = result.scalars().all()
    # conv-open P1-1：外置字段（payload stub）必须还原全文后再进 LLM 上下文/
    # regenerate/导出 —— 语义与落库前逐字节一致（DESIGN_conv_open_performance §4）。
    from app.services.message_payload_service import resolve_message_fields
    return await resolve_message_fields(rows, db)


async def _trim_target_status(db: AsyncSession, message_id: str,
                              conversation_id: str | None = None) -> str | None:
    """404 区分（2026-10-17 判据#2，conv 92398a80）：目标行的 delivery_status
    （含 streaming 残留行——_load_conversation_messages 会将其滤除）。

    会话收敛（评审 M1）：带 conversation_id 时按会话限定——他会话 id 视同
    不存在，消除「该 UUID 存在且在生成」的 1-bit 跨会话状态 oracle。"""
    stmt = select(Message.delivery_status).where(Message.id == message_id)
    if conversation_id:
        stmt = stmt.where(Message.conversation_id == conversation_id)
    res = await db.execute(stmt)
    return res.scalar_one_or_none()


def _trim_missing_detail(status: str | None, kind: str) -> str:
    """trim 404 的可区分文案：streaming 残留行（stop/中断未保存，刷新自愈）
    vs 目标真不存在（被删/合成 id）。kind ∈ regenerate|edit。"""
    if status == "streaming":
        if kind == "regenerate":
            return "该回答的生成被中断且未保存成功，无法对其重新生成，请刷新会话后重试"
        return "该消息状态已变化，请刷新会话后重试"
    return "待重新生成的消息不存在" if kind == "regenerate" else "待编辑的消息不存在"


async def _trim_messages_for_regeneration(
    db: AsyncSession,
    *,
    conversation_id: str,
    regenerate_from_message_id: str,
    user_id: str | None = None,
):
    conversation_messages = await _load_conversation_messages(db, conversation_id)
    target_index = next(
        (index for index, message in enumerate(conversation_messages) if message.id == regenerate_from_message_id),
        -1,
    )
    if target_index == -1:
        _st = await _trim_target_status(db, regenerate_from_message_id, conversation_id=conversation_id)
        raise HTTPException(status_code=404, detail=_trim_missing_detail(_st, "regenerate"))

    target_message = conversation_messages[target_index]
    if target_message.role != "assistant":
        raise HTTPException(status_code=400, detail="只能重新生成助手消息")

    # 2026-11-01: inline interjections from this run were anchored BEFORE the
    # assistant row (created_at = anchor+1ms), so they sit before target_index
    # and would survive a naive [target:] trim as orphan top-level user rows.
    deleted_assistant_ids = {
        m.id for m in conversation_messages[target_index:] if m.role == "assistant"
    }
    orphan_interjections = [
        m for m in conversation_messages[:target_index]
        if m.role == "user"
        and getattr(m, "embedded_in_message_id", None) in deleted_assistant_ids
    ]
    await _delete_message_slice(
        db,
        orphan_interjections + conversation_messages[target_index:],
        user_id=user_id,
    )
    return [
        m for m in conversation_messages[:target_index]
        if m not in orphan_interjections
    ]




async def _trim_messages_for_edit(
    db: AsyncSession,
    *,
    conversation_id: str,
    edit_message_id: str,
    user_id: str | None = None,
):
    """Delete the target user message and all subsequent messages for edit-and-resend.

    删除不自带 commit（见 _delete_message_slice）：与新用户消息的插入同事务落库。"""
    conversation_messages = await _load_conversation_messages(db, conversation_id)
    target_index = next(
        (index for index, message in enumerate(conversation_messages) if message.id == edit_message_id),
        -1,
    )
    if target_index == -1:
        _st = await _trim_target_status(db, edit_message_id, conversation_id=conversation_id)
        raise HTTPException(status_code=404, detail=_trim_missing_detail(_st, "edit"))

    target_message = conversation_messages[target_index]
    if target_message.role != "user":
        raise HTTPException(status_code=400, detail="只能编辑用户消息")

    await _delete_message_slice(db, conversation_messages[target_index:], user_id=user_id)
    return conversation_messages[:target_index]




@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    req: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from app.api.chat_stream_transport import stream_chat_response

    return await stream_chat_response(request, req, db, current_user)


@router.post("/stream/resume")
async def resume_stream(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Reconnect to an SSE stream via the persistent StreamBuffer.

    Accepts JSON body: {"conversation_id": "xxx"}
    Uses subscribe_with_snapshot() for atomic replay + subscribe —
    no duplication gap between replay and live deltas.

    部署排空扩面（D-12，2026-10-06）：drain 中拒收 resume——排空窗口不接入
    新客户端流（重连请等新进程），先于任何 buffer 访问。
    """
    if _agent_registry.is_draining:
        raise HTTPException(
            status_code=503,
            detail="server is draining for restart — please retry in a few seconds",
            headers={"Retry-After": "5"},
        )
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
            return {"event": "reasoning_content", "data": json.dumps(
                {"reasoning_content": scrub_reasoning_meta(delta["data"])})}
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
        elif delta["type"] == "interjection_committed":
            # W26（B2）：插话 commit-echo 的实时增量转发。断连期间 commit 的
            # 那一条由 replay 的 interjections_committed 补齐（buffer 权威）。
            return {
                "event": "message",
                "data": json.dumps({
                    "interjection_committed": delta["data"],
                    "done": False,
                }),
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
                    # M4：legacy resume 路径无 assistant 上下文 → 全局默认样式；
                    # 载荷已存在时 _render_answer_citations 幂等跳过（保留产出方版本）。
                    _cap_content, _cap_tr_json = await _render_answer_citations(
                        _cap_content, _cap_tr_json, None,
                    )

                    # Guard: never persist a completely empty bubble from the
                    # legacy resume path either (mirrors the main-flow guards).
                    # P1（2026-10-31 UPSTREAM_TODO_20261031）：reasoning 不参与落库
                    # 判定——「无正文且无工具结果」即跳过，reasoning-only 不再落
                    # final（conv d7ea65b8 空气泡第二落库面，与 stop 分支同裁定）。
                    if not (_cap_content or "").strip() and not _cap_tr_json:
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
                                # 渐进落库：resume 完成的 run 若在别的请求里建过
                                # streaming 行，按 (conv, streaming) 反查原地更新。
                                buf_fallback = None
                                try:
                                    buf_fallback = await stream_buffer_manager.get_buffer_no_auth(_cap_conv_id)
                                except Exception:
                                    pass
                                msg = await _save_assistant_terminal(
                                    bg_db,
                                    conversation_id=_cap_conv_id,
                                    content=_cap_content,
                                    reasoning=_cap_reasoning if _cap_reasoning else None,
                                    tool_results=_cap_tr_json,
                                    tool_calls=None,
                                    context_info=None,
                                    status="final",
                                    # 2026-11-02（C6）：绑定本 run 的 streaming 行。
                                    # 缺此参数会回退「本会话最新 streaming 行」反查，
                                    # 可能错翻别的 run 的僵尸行（W5 引入 row_id 正是
                                    # 为修这个 bug）。
                                    row_id=getattr(buf_fallback, "streaming_message_id", None),
                                    interjection_items=interjection_items_from_buffer(buf_fallback),
                                )
                                conv_row.updated_at = datetime.utcnow()
                                await bg_db.commit()
                                await bg_db.refresh(msg)
                                msg_id = str(msg.id)
                                title = conv_row.title
                                try:
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
                    _payload = {"reasoning_content": scrub_reasoning_meta(
                        event["reasoning_content"])}
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
                elif "interjection_committed" in event:
                    # W26（B2）：legacy resume 路径同样转发插话 echo（主路径
                    # 的 replay 已携带 interjections_committed）。
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "interjection_committed": event["interjection_committed"],
                            "done": False,
                        }),
                    }
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
        # 兜底③（conv 5228db60）：cancel 落在主 try 之前时 task 自身的
        # finally/unregister 不会运行——等 task 真正死透后 identity 兜底释放；
        # 2s 超时未死透（真身在正常收尾）则不动，让其 finally 自洁。
        try:
            await asyncio.wait({task}, timeout=2.0)
        except Exception:
            pass
        try:
            await _agent_registry.release_if_task_dead(conversation_id, expected_task=task)
        except Exception:
            pass
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


def _interject_target_alive(state) -> bool:
    """interject 目标是否为活 run（conv 5228db60 兜底⑥）。

    task 已死而 is_running=True 的孤儿槽若继续接单插话，消息会排进死队列的
    interjection_queue —— commit-time 落库语义下永不消费即永不落库，用户
    消息静默蒸发。判活口径与 release_if_task_dead 一致：无句柄=setup 期
    （保留旧行为），有句柄且 done=死槽。
    """
    if state is None or not state.is_running:
        return False
    handle = getattr(state, "task_handle", None)
    if handle is not None:
        return not handle.done()
    # 无句柄：setup 期（provisional）→ 活（旧行为保留）；adopted 无主 → 死槽
    #（A4.9 R1 #3，PLAN Task3 ⑤ 原契约——插话进死队列=静默丢消息）。
    return bool(state.provisional)


_MAX_INTERJECT_CLIENT_MESSAGE_ID = 128


def _normalize_interject_request(request_data: dict | None) -> tuple[str, str | None]:
    """Normalize the interject POST body (W26 §B3).

    ``client_message_id`` is client-generated and echoed back in
    ``interjection_committed`` so the frontend can pair echoes by id (same
    conversation, identical text) instead of by content. Legacy clients omit
    it — the queue item then carries ``None`` and the client falls back to
    content pairing.

    Non-string values are treated as absent (never coerced via ``str()``:
    ``str({"a": 1})`` would smuggle a Python repr into the model context).
    Length validation belongs to the endpoint so it can return HTTP 400
    instead of silently truncating an id the client can never match again.
    """
    data = request_data or {}
    raw_content = data.get("content")
    content = raw_content.strip() if isinstance(raw_content, str) else ""
    raw_id = data.get("client_message_id")
    client_message_id = raw_id.strip() if isinstance(raw_id, str) else ""
    return content, (client_message_id or None)


@router.post("/stream/interject/{conversation_id}")
async def interject_agent_stream(
    conversation_id: str,
    request_data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a user interjection into a RUNNING agent turn.

    Iteration-boundary steering (codex pending-input / deepseek-harness
    next-step inbox): the text is queued on the conversation's active agent
    state and injected as a real user message at the next tool-loop iteration
    boundary — the in-flight LLM stream is never aborted. The loop emits an
    ``interjection_committed`` SSE event when the message enters the model
    context, and the producer persists it as a normal ``role=user`` message
    (commit-time persistence — a queued-but-never-consumed interjection is
    never written to the DB, so no orphaned unanswered rows can appear).

    Tri-state response: ``steered`` (queued into the live run) ·
    ``not_running`` (no live run — the client should fall back to a normal
    send) · ``unsupported_mode`` (deathmatch conversations have their own
    human-gate interaction semantics).

    Request body: ``{"content": str, "client_message_id"?: str}``.
    ``client_message_id`` (≤128 chars, optional — legacy clients omit it) is
    echoed back unchanged in the ``interjection_committed`` payload so the
    client can pair echoes by id instead of by content (W26 §B3).
    """
    # 归属校验先于内容校验（第四波 ④）：非属主探测一律 404，不泄漏端点
    # 内容规则（authz-first）。
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conversation = result.scalar_one_or_none()
    if conversation is None or conversation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    content, client_message_id = _normalize_interject_request(request_data)
    if not content:
        raise HTTPException(status_code=400, detail="插话内容不能为空")
    if client_message_id and len(client_message_id) > _MAX_INTERJECT_CLIENT_MESSAGE_ID:
        # echo 必须与客户端保存的 id 逐字相同才能配对：截断会静默降级为
        # 内容配对（B3 要根治的错配面），故直接 400。
        raise HTTPException(
            status_code=400,
            detail=f"client_message_id 过长（上限 {_MAX_INTERJECT_CLIENT_MESSAGE_ID} 字符）",
        )
    if len(content) > 8000:
        raise HTTPException(status_code=400, detail="插话内容过长（上限 8000 字符）")
    if (conversation.deathmatch_status or "") in ("grilling", "active", "paused"):
        return {"status": "unsupported_mode", "conversation_id": conversation_id}
    state = _agent_registry.get_local(conversation_id)
    if not _interject_target_alive(state):
        # 兜底⑥（conv 5228db60）：死槽诚实报 not_running（客户端回退普通
        # 发送 → conversation_busy 可见反馈），绝不吞进死队列静默丢失。
        return {"status": "not_running", "conversation_id": conversation_id}
    if state.interjection_queue.qsize() >= 10:
        # 待注入积压上限（A4.9 M3）：全部会在下一迭代边界进入模型上下文，
        # 无上限是上下文/成本放大面。客户端收到 429 将文本回填输入框稍后
        # 重发（rate_limited，绝不回退普通发送——那会杀掉在途 run）。
        raise HTTPException(status_code=429, detail="插话队列已满，请等待当前插话被接收")
    state.interjection_queue.put_nowait({
        "content": content,
        "client_message_id": client_message_id,
    })
    logger.info(
        "Interjection queued for conversation %s (%d chars, user=%s)",
        conversation_id, len(content), current_user.id,
    )
    return {"status": "steered", "conversation_id": conversation_id}


@router.post("/permission/respond")
async def permission_respond(
    request_data: dict,
    current_user: User = Depends(get_current_user),
):
    """Respond to a pending permission request from the agent loop."""
    from app.services.permission_manager import get_pending_request, respond_to_request
    request_id = request_data.get("request_id", "")
    approved = bool(request_data.get("approved", False))
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id is required")
    pending = get_pending_request(request_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Permission request not found or expired")
    # P1b: only the conversation owner may answer an act-gate/permission
    # prompt; an unbound request fails closed.
    if not pending.user_id or str(pending.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Permission request belongs to another user")
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
