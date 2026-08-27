# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
StreamBuffer — Two-layer persistent accumulator for agent SSE output.

Each conversation gets its own buffer.  All agent events are appended
in-order.  The buffer uses a "rendered pointer" model:

  content            — single accumulated string, always grows in order
  rendered_content_len — how many characters were confirmed sent to a client

When a client reconnects it receives:
  Layer 1 = content[:rendered_content_len]   (already rendered before disconnect)
  Layer 2 = content[rendered_content_len:]   (arrived while disconnected)

This preserves content ordering regardless of connect/disconnect timing.

Each buffer tracks a `status` field:
  "incomplete" — agent is still running or finished but DB save pending.
  "complete"   — agent finished AND message saved to DB (db_message_id set).
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.services.shared_state import shared_state

logger = logging.getLogger(__name__)

_BUFFER_TTL_SECONDS = 300
_CLEANUP_INTERVAL_SECONDS = 60
# Snapshots of a live stream refresh every ~15s; a "running" snapshot older
# than this is residue from a finished/crashed run and must not be trusted.
_STALE_SNAPSHOT_SECONDS = 180
_MAX_CONTENT_CHARS = 500_000
_MAX_REASONING_CHARS = 200_000
_MAX_ACCUMULATED_SEGMENTS = 10_000

# Localized titles for tool_call display in resumed/replayed streams.
# Keep in sync with TOOL_NAME_MAP in frontend/src/components/ChatArea.vue.
_TOOL_TITLE_ZH: Dict[str, str] = {
    "web_search": "联网搜索",
    "browser": "浏览网页",
    "browser_interact": "浏览网页",
    "code_execution": "代码执行",
    "terminal": "终端执行",
    "memory": "记忆",
    "delegate_task": "任务委派",
    "schedule_tool": "定时任务",
    "background_task_tool": "后台任务",
    "word_count": "字数统计",
    "workspace_read": "文件读取",
    "workspace_glob": "文件搜索",
    "provide_file": "提供文件",
}


@dataclass
class ConversationBuffer:
    conversation_id: str
    user_id: int

    # Single accumulated content (always grows in order)
    content: str = ""
    reasoning: str = ""

    # Rendered pointer: how many chars were confirmed sent to a client
    _rendered_content_len: int = 0
    _rendered_reasoning_len: int = 0

    # Structured data (full accumulated, no layer split)
    content_segments: List[str] = field(default_factory=list)
    display_sequence: List[dict] = field(default_factory=list)
    tool_calls: List[dict] = field(default_factory=list)
    tool_results: List[dict] = field(default_factory=list)
    agent_steps: List[dict] = field(default_factory=list)
    file_attachments: List[dict] = field(default_factory=list)
    search_progress: List[dict] = field(default_factory=list)
    search_failed: Optional[dict] = None
    iteration: Optional[dict] = None
    # 每轮上下文 token 用量估算（context_info 事件的最新值）——resume 快照
    # 需携带它，否则重连后的客户端头部徽章丢失。
    context_info: Optional[dict] = None
    # F1-1: part_id -> display_sequence item reference, for O(1) in-place
    # part_delta / part_updated mutation. Rebuilt after segment truncation.
    _part_index: Dict[str, dict] = field(default_factory=dict)

    # Lifecycle
    status: str = "incomplete"
    is_running: bool = True
    error: Optional[str] = None
    done_data: Optional[dict] = None
    db_message_id: Optional[str] = None

    # Book-keeping
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    _last_write: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Live subscribers (buffer subscribers, typically the resume SSE endpoint)
    _subscribers: List[asyncio.Queue] = field(default_factory=list)

    # SSE connection count (initial SSE connections that are NOT buffer subscribers
    # but still count as "active clients" for rendered-pointer tracking)
    _sse_connection_count: int = 0

    # Sequence number for deltas
    _next_seq: int = 0

    @property
    def has_active_client(self) -> bool:
        return len(self._subscribers) > 0 or self._sse_connection_count > 0

    def _advance_rendered_pointers(self) -> None:
        """Move rendered pointers to current end (everything is now being sent)."""
        self._rendered_content_len = len(self.content)
        self._rendered_reasoning_len = len(self.reasoning)

    def _truncate_content(self) -> None:
        """Keep accumulated content within bounded memory."""
        if len(self.content) > _MAX_CONTENT_CHARS:
            excess = len(self.content) - _MAX_CONTENT_CHARS
            self.content = self.content[excess:]
            self._rendered_content_len = max(0, self._rendered_content_len - excess)

    def _truncate_reasoning(self) -> None:
        if len(self.reasoning) > _MAX_REASONING_CHARS:
            excess = len(self.reasoning) - _MAX_REASONING_CHARS
            self.reasoning = self.reasoning[excess:]
            self._rendered_reasoning_len = max(0, self._rendered_reasoning_len - excess)

    def _truncate_segments(self) -> None:
        if len(self.content_segments) > _MAX_ACCUMULATED_SEGMENTS:
            self.content_segments = self.content_segments[-_MAX_ACCUMULATED_SEGMENTS:]
        if len(self.display_sequence) > _MAX_ACCUMULATED_SEGMENTS:
            self.display_sequence = self.display_sequence[-_MAX_ACCUMULATED_SEGMENTS:]
            self._rebuild_part_index()
        if len(self.tool_calls) > _MAX_ACCUMULATED_SEGMENTS:
            self.tool_calls = self.tool_calls[-_MAX_ACCUMULATED_SEGMENTS:]
        if len(self.tool_results) > _MAX_ACCUMULATED_SEGMENTS:
            self.tool_results = self.tool_results[-_MAX_ACCUMULATED_SEGMENTS:]
        if len(self.agent_steps) > _MAX_ACCUMULATED_SEGMENTS:
            self.agent_steps = self.agent_steps[-_MAX_ACCUMULATED_SEGMENTS:]
        if len(self.search_progress) > _MAX_ACCUMULATED_SEGMENTS:
            self.search_progress = self.search_progress[-_MAX_ACCUMULATED_SEGMENTS:]

    def _rebuild_part_index(self) -> None:
        """Re-sync _part_index with display_sequence (after truncation)."""
        self._part_index = {
            item["part_id"]: item
            for item in self.display_sequence
            if isinstance(item, dict) and item.get("part_id")
        }

    def _find_part(self, part_id: str) -> Optional[dict]:
        """Locate a display_sequence item by part_id (index first, scan fallback)."""
        item = self._part_index.get(part_id)
        if item is not None:
            return item
        for candidate in reversed(self.display_sequence):
            if isinstance(candidate, dict) and candidate.get("part_id") == part_id:
                self._part_index[part_id] = candidate
                return candidate
        return None

    def to_replay(self) -> dict:
        """Return two-layer replay data for a reconnecting client."""
        return {
            # SSE protocol version. version >= 2 means display_sequence items
            # carry part_id and the live stream emits part_started/part_delta/
            # part_updated alongside legacy events (F1-1).
            "version": 2,
            "content": self.content,
            "reasoning": self.reasoning,
            "layer1": {
                "content": self.content[:self._rendered_content_len],
                "reasoning": self.reasoning[:self._rendered_reasoning_len],
            },
            "layer2": {
                "content": self.content[self._rendered_content_len:],
                "reasoning": self.reasoning[self._rendered_reasoning_len:],
            },
            "content_segments": list(self.content_segments),
            "display_sequence": list(self.display_sequence),
            "tool_calls": list(self.tool_calls),
            "tool_results": list(self.tool_results),
            "agent_steps": list(self.agent_steps),
            "file_attachments": list(self.file_attachments),
            "search_progress": list(self.search_progress),
            "search_failed": self.search_failed,
            "iteration": self.iteration,
            "context_info": self.context_info,
            "status": self.status,
            "is_running": self.is_running,
            "error": self.error,
            "db_message_id": self.db_message_id,
        }

    def to_snapshot(self) -> dict:
        """Backward-compatible full snapshot."""
        return {
            "content": self.content,
            "reasoning": self.reasoning,
            "content_segments": list(self.content_segments),
            "display_sequence": list(self.display_sequence),
            "tool_calls": list(self.tool_calls),
            "tool_results": list(self.tool_results),
            "agent_steps": list(self.agent_steps),
            "file_attachments": list(self.file_attachments),
            "search_progress": list(self.search_progress),
            "search_failed": self.search_failed,
            "iteration": self.iteration,
            "context_info": self.context_info,
            "is_running": self.is_running,
            "is_done": self.status == "complete",
            "error": self.error,
            "status": self.status,
            "db_message_id": self.db_message_id,
            "done_data": self.done_data if self.status == "complete" else None,
        }

    async def subscribe_with_snapshot(self) -> Tuple[asyncio.Queue, dict]:
        """Atomic subscribe + snapshot.  Eliminates the duplication gap.

        Takes the snapshot BEFORE advancing rendered pointers so the
        client can distinguish the two layers.  After the snapshot is
        captured, rendered pointers are advanced so that future
        append() calls with an active client update correctly.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        async with self._lock:
            self._subscribers.append(q)
            snapshot = self.to_replay()
            self._advance_rendered_pointers()
        return q, snapshot

    async def subscribe(self) -> asyncio.Queue:
        q, _ = await self.subscribe_with_snapshot()
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def set_sse_active(self, active: bool) -> None:
        """Mark that a direct SSE connection (not a buffer subscriber) is active.

        When activating, also advance the rendered pointers so that
        content already accumulated counts as "rendered" — the initial
        SSE generator streams everything accumulated so far.
        """
        async with self._lock:
            if active:
                self._sse_connection_count += 1
                self._advance_rendered_pointers()
            else:
                self._sse_connection_count = max(0, self._sse_connection_count - 1)

    async def append(self, event: Dict[str, Any]) -> None:
        """Append an agent event and broadcast to live subscribers."""
        delta = None

        async with self._lock:
            self._last_write = time.time()
            if "part_started" in event:
                # F1-1: push a new timeline item keyed by part_id. The item
                # shape mirrors what the frontend v2 reducer builds, so replay
                # restores the exact live structure (including part_id).
                part = event["part_started"]
                part_id = part.get("part_id")
                item: Dict[str, Any] = {
                    "type": part.get("part_type") or "text",
                    "part_id": part_id,
                    "content": part.get("content", ""),
                }
                for key in ("call_id", "name", "title", "step_type", "status",
                            "arguments", "subtask_id", "subtask_name"):
                    if part.get(key) is not None:
                        item[key] = part[key]
                self.display_sequence.append(item)
                if part_id:
                    self._part_index[part_id] = item
                delta = {"seq": self._next_seq, "type": "part_started", "data": part}

            elif "part_delta" in event:
                # F1-1: append to an existing part IN PLACE — never push.
                part = event["part_delta"]
                part_id = part.get("part_id")
                item = self._find_part(part_id) if part_id else None
                if item is None:
                    # Delta-before-start tolerance: synthesize the item so a
                    # late subscriber never drops streamed text.
                    item = {
                        "type": part.get("part_type") or "text",
                        "part_id": part_id,
                        "content": "",
                    }
                    self.display_sequence.append(item)
                    if part_id:
                        self._part_index[part_id] = item
                field_name = part.get("field") or "content"
                item[field_name] = (item.get(field_name) or "") + (part.get("delta") or "")
                delta = {"seq": self._next_seq, "type": "part_delta", "data": part}

            elif "part_updated" in event:
                # F1-1: structured state transition snapshot (e.g. tool
                # running -> completed/error), mutate the same slot in place.
                part = event["part_updated"]
                part_id = part.get("part_id")
                item = self._find_part(part_id) if part_id else None
                if item is None and part.get("call_id"):
                    for candidate in reversed(self.display_sequence):
                        if (isinstance(candidate, dict)
                                and candidate.get("type") == "tool_call"
                                and candidate.get("call_id") == part["call_id"]):
                            item = candidate
                            break
                if item is not None:
                    for key in ("status", "result", "error", "content", "title"):
                        if part.get(key) is not None:
                            item[key] = part[key]
                delta = {"seq": self._next_seq, "type": "part_updated", "data": part}

            elif "content" in event:
                self.content += event["content"]
                self._truncate_content()
                delta = {"seq": self._next_seq, "type": "content", "data": event["content"]}

            elif "reasoning_content" in event:
                self.reasoning += event["reasoning_content"]
                self._truncate_reasoning()
                delta = {"seq": self._next_seq, "type": "reasoning", "data": event["reasoning_content"]}

            elif "content_segment" in event:
                seg = event["content_segment"]
                if seg.strip():
                    self.content_segments.append(seg)
                    # Mirror the frontend's live behavior: push the just-completed
                    # text segment into display_sequence as a {type:"text"} item
                    # so a client that reconnects mid-tool-call sees the
                    # interleaved [text → reasoning → text → tool] history
                    # instead of a single un-segmented blob.
                    if not event.get("_part_stamped"):
                        self.display_sequence.append({"type": "text", "content": seg})
                delta = {"seq": self._next_seq, "type": "content_segment", "data": seg}

            elif "reasoning_segment" in event:
                # Per-turn reasoning chunk emitted at a tool_call boundary.
                # Append into display_sequence so the post-reload replay
                # renders the mid-stream thinking text inline.
                item = event["reasoning_segment"] or {}
                norm = {
                    "type": "reasoning_step",
                    "title": item.get("title") or "💭 思考过程",
                    "content": item.get("content") or "",
                }
                if not event.get("_part_stamped"):
                    self.display_sequence.append(norm)
                delta = {"seq": self._next_seq, "type": "reasoning_segment", "data": norm}

            elif "tool_call" in event:
                tc = event["tool_call"]
                self.tool_calls.append(tc)
                _tc_name = tc.get("name", "unknown")
                step = {
                    "name": _tc_name,
                    # Use a localized display title so that resumed/replayed
                    # streams render Chinese names instead of raw tool ids
                    # like "web_search".  Frontend also has a fallback map.
                    "title": _TOOL_TITLE_ZH.get(_tc_name, _tc_name or "工具调用"),
                    "content": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                    "step_type": "tool_call",
                }
                self.agent_steps.append(step)
                # Mirror the live flow's frontend behavior: every agent_step
                # is also pushed into display_sequence so a client that
                # reconnects mid-tool-call sees the tool call inline with the
                # surrounding text/reasoning instead of having it disappear.
                if not event.get("_part_stamped"):
                    self.display_sequence.append({"type": "tool_call", **step})
                delta = {"seq": self._next_seq, "type": "tool_call", "data": tc}

            elif "tool_result" in event:
                tr = event["tool_result"]
                self.tool_results.append(tr)
                delta = {"seq": self._next_seq, "type": "tool_result", "data": tr}

            elif "iteration" in event:
                self.iteration = event["iteration"]
                delta = {"seq": self._next_seq, "type": "iteration", "data": event["iteration"]}

            elif "context_info" in event:
                self.context_info = event["context_info"]
                delta = {"seq": self._next_seq, "type": "context_info", "data": event["context_info"]}

            elif "agent_step" in event:
                step = event["agent_step"]
                self.agent_steps.append(step)
                # Mirror the live flow: agent_step also feeds display_sequence
                # so the post-reload replay renders the step in its proper
                # interleaved position.
                if not event.get("_part_stamped"):
                    self.display_sequence.append({"type": step.get("step_type") or "tool", **step})
                delta = {"seq": self._next_seq, "type": "agent_step", "data": step}

            elif "attachments" in event:
                atts = event["attachments"]
                self.file_attachments = atts
                delta = {"seq": self._next_seq, "type": "attachments", "data": atts}

            elif "permission_request" in event or event.get("type") == "permission_request":
                perm_data = event.get("permission_request") or event.get("data", {})
                delta = {"seq": self._next_seq, "type": "permission_request", "data": perm_data}

            elif "search_progress" in event:
                sp = event["search_progress"]
                self.search_progress.append(sp)
                delta = {"seq": self._next_seq, "type": "search_progress", "data": sp}

            elif "search_failed" in event:
                self.search_failed = event["search_failed"]
                delta = {"seq": self._next_seq, "type": "search_failed", "data": event["search_failed"]}

            elif "done" in event:
                self.is_running = False
                self.status = "complete"
                self.completed_at = time.time()
                self.done_data = dict(event)
                delta = {"seq": self._next_seq, "type": "done", "data": event}

            elif "error" in event:
                self.error = event.get("error", str(event))
                self.is_running = False
                delta = {"seq": self._next_seq, "type": "error", "data": event}

            else:
                delta = {"seq": self._next_seq, "type": "unknown", "data": event}

            self._next_seq += 1
            self._truncate_segments()

            # If a client is connected, advance rendered pointer so
            # future reconnects know this content was "sent".
            if self.has_active_client and delta and delta["type"] in ("content", "reasoning"):
                self._advance_rendered_pointers()

        if delta:
            await self._broadcast(delta)

    async def mark_complete(self, db_message_id: Optional[str] = None) -> None:
        """Mark buffer as complete after DB save."""
        delta = None
        async with self._lock:
            self.status = "complete"
            self.is_running = False
            if db_message_id:
                self.db_message_id = db_message_id
            if not self.completed_at:
                self.completed_at = time.time()
            if db_message_id and self._subscribers:
                try:
                    delta = {"seq": self._next_seq, "type": "db_message_id", "data": {"db_message_id": db_message_id}}
                    self._next_seq += 1
                except Exception:
                    pass
        if delta:
            for q in self._subscribers:
                try:
                    q.put_nowait(delta)
                except asyncio.QueueFull:
                    pass
        # The shared-state recovery row exists only so a *running* stream can
        # be resumed cross-worker. Once complete, the snapshot loop stops
        # refreshing it and the stale row (is_running=True from mid-run) would
        # otherwise resurrect this buffer as an immortal "running" zombie.
        if shared_state.is_db_enabled:
            try:
                await shared_state.delete_buffer_state(self.conversation_id)
            except Exception:
                logger.warning(
                    "Failed to delete buffer state for %s on completion",
                    self.conversation_id, exc_info=True,
                )

    async def _broadcast(self, delta: Dict[str, Any]) -> None:
        dead: List[asyncio.Queue] = []
        async with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(delta)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass


class StreamBufferManager:
    _instance: Optional["StreamBufferManager"] = None

    def __init__(self):
        self._buffers: Dict[str, ConversationBuffer] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "StreamBufferManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def create_buffer(self, conversation_id: str, user_id: int) -> ConversationBuffer:
        async with self._lock:
            buf = self._buffers.get(conversation_id)
            if buf and buf.is_running:
                return buf
            buf = ConversationBuffer(conversation_id=conversation_id, user_id=user_id)
            self._buffers[conversation_id] = buf
            logger.info("Created stream buffer for conversation %s (worker=%s)",
                         conversation_id, shared_state.worker_id)
            return buf

    async def get_buffer(self, conversation_id: str, user_id: int) -> Optional[ConversationBuffer]:
        async with self._lock:
            buf = self._buffers.get(conversation_id)
            if buf is not None:
                if buf.user_id != user_id:
                    logger.warning("Buffer ownership mismatch: conv=%s expected_user=%s got_user=%s",
                                   conversation_id, buf.user_id, user_id)
                    return None
                return buf

        # Local miss — try recovery from shared state (cross-worker reconnect)
        if shared_state.is_db_enabled:
            recovery = await shared_state.load_buffer_state(conversation_id)
            if recovery:
                snap = recovery.get("snapshot", {})
                # Distrust is_running unless the snapshot is fresh AND its
                # owning worker is still alive. Snapshots of a live stream are
                # refreshed every ~15s; a stale row (or one from a stopped
                # worker) is the residue of a finished/crashed run — trusting
                # it resurrects an immortal "running" zombie buffer that
                # pins the frontend's streaming state forever.
                snap_running = bool(snap.get("is_running", False))
                if snap_running:
                    saved_at = recovery.get("saved_at") or 0
                    owner = recovery.get("worker_id", "")
                    try:
                        owner_alive = await shared_state.is_worker_active(owner)
                    except Exception:
                        owner_alive = True  # fail-open: single-instance mode
                    stale = (time.time() - saved_at) > _STALE_SNAPSHOT_SECONDS
                    # Ground-truth check: a running buffer must be backed by a
                    # LIVE agent state for the same conversation — the agent
                    # registry saves one every 15s while a run is active and
                    # deletes it on completion (unregister). A running-claim
                    # with no agent state is a zombie (conv 149ce886, 2026-08-01:
                    # a finished run left its buffer row behind; the snapshot
                    # loop then re-owned it, so neither staleness nor owner
                    # liveness could ever distrust it — every new /stream was
                    # rejected with "conversation_busy").
                    agent_state = None
                    try:
                        agent_state = await shared_state.load_agent_state(conversation_id)
                    except Exception:
                        pass
                    agent_live = bool(
                        agent_state
                        and (agent_state.get("snapshot") or {}).get("is_running")
                    )
                    if stale or not owner_alive or not agent_live:
                        logger.warning(
                            "Discarding stale running-claim for conversation %s "
                            "(worker=%s alive=%s age=%ds)",
                            conversation_id, owner, owner_alive,
                            int(time.time() - saved_at),
                        )
                        snap["is_running"] = False
                        try:
                            await shared_state.delete_buffer_state(conversation_id)
                        except Exception:
                            pass
                logger.info(
                    "Recovering buffer for conversation %s from worker %s",
                    conversation_id, recovery.get("worker_id", "unknown"),
                )
                buf = ConversationBuffer(
                    conversation_id=conversation_id,
                    user_id=user_id,
                )
                buf.content = snap.get("content", "")
                buf.reasoning = snap.get("reasoning", "")
                buf.status = snap.get("status", "incomplete")
                buf.is_running = snap.get("is_running", False)
                buf.db_message_id = snap.get("db_message_id")
                buf._rendered_content_len = snap.get("rendered_content_len", 0)
                buf.tool_calls = [dict(tc) for tc in snap.get("tool_calls", [])]
                buf.tool_results = [dict(tr) for tr in snap.get("tool_results", [])]
                buf.agent_steps = [dict(s) for s in snap.get("agent_steps", [])]
                buf.display_sequence = [dict(d) for d in snap.get("display_sequence", [])]
                buf._sse_connection_count = 0
                async with self._lock:
                    self._buffers[conversation_id] = buf
                return buf

        return None

    async def get_buffer_no_auth(self, conversation_id: str) -> Optional[ConversationBuffer]:
        async with self._lock:
            return self._buffers.get(conversation_id)

    async def delete_buffer(self, conversation_id: str) -> None:
        async with self._lock:
            self._buffers.pop(conversation_id, None)
        if shared_state.is_db_enabled:
            try:
                await shared_state.delete_buffer_state(conversation_id)
            except Exception:
                logger.warning(
                    "Failed to delete buffer state for %s from shared store",
                    conversation_id, exc_info=True,
                )

    async def start_cleanup(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        if self._snapshot_task is None and shared_state.is_db_enabled:
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            await self._run_cleanup_pass()

    async def _run_cleanup_pass(self) -> None:
        _ZOMBIE_TTL = 3600
        now = time.time()
        expired = []
        async with self._lock:
            for cid, buf in list(self._buffers.items()):
                if not buf.is_running:
                    completed_at = buf.completed_at or buf.created_at
                    if now - completed_at > _BUFFER_TTL_SECONDS:
                        expired.append(cid)
                elif now - buf._last_write > _ZOMBIE_TTL:
                    logger.warning("Cleaning up zombie buffer (is_running but no writes in %ds): %s", _ZOMBIE_TTL, cid)
                    expired.append(cid)
            for cid in expired:
                self._buffers.pop(cid, None)
                logger.info("Cleaned up expired stream buffer: %s", cid)
        # Also drop the shared-state recovery rows. A local-only expiry that
        # leaves the row behind lets the next status check resurrect the
        # buffer as a zombie (the row still claims is_running=True).
        if expired and shared_state.is_db_enabled:
            for cid in expired:
                try:
                    await shared_state.delete_buffer_state(cid)
                except Exception:
                    logger.warning(
                        "Failed to delete expired buffer state for %s", cid,
                        exc_info=True,
                    )

    async def _snapshot_loop(self) -> None:
        """Periodically persist buffer state for cross-worker recovery."""
        _SNAPSHOT_INTERVAL = 15
        while True:
            await asyncio.sleep(_SNAPSHOT_INTERVAL)
            await self._snapshot_running_buffers()

    async def _snapshot_running_buffers(self) -> None:
        async with self._lock:
            buffers = list(self._buffers.items())
        for cid, buf in buffers:
            if not buf.is_running:
                continue
            try:
                snap = {
                    "content": buf.content,
                    "reasoning": buf.reasoning,
                    "status": buf.status,
                    "is_running": buf.is_running,
                    "db_message_id": str(buf.db_message_id) if buf.db_message_id else None,
                    "rendered_content_len": buf._rendered_content_len,
                    "tool_calls": [dict(tc) for tc in buf.tool_calls],
                    "tool_results": [dict(tr) for tr in buf.tool_results],
                    "agent_steps": [dict(s) for s in buf.agent_steps],
                    "display_sequence": [dict(d) for d in buf.display_sequence],
                }
                # NOTE: never touch buf._last_write here — it is activity
                # bookkeeping for zombie detection (append() updates it on
                # every agent event, including keepalive pings), and
                # fabricating freshness makes zombie buffers immortal.
                await shared_state.save_buffer_state(cid, snap)
                if not buf.is_running:
                    # mark_complete landed mid-save: its row deletion was
                    # undone by the save above — delete again so the stale
                    # running-claim row cannot resurrect a zombie buffer.
                    try:
                        await shared_state.delete_buffer_state(cid)
                    except Exception:
                        pass
            except Exception:
                pass  # Snapshot is best-effort; don't disrupt streaming


stream_buffer_manager = StreamBufferManager.get_instance()
