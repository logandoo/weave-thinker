# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
ActiveAgentRegistry — In-memory state buffer for running agent loops.

Enables SSE reconnection: when a user switches browser tabs and returns,
the frontend reconnects and receives the accumulated state replay plus
live event streaming from the still-running agent.

Phase 5.5: Added shared-state persistence for multi-instance recovery.
Agent state snapshots are periodically saved to the shared state backend
so they can be recovered by a different worker process after a restart.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.shared_state import shared_state

logger = logging.getLogger(__name__)


@dataclass
class ActiveAgentState:
    conversation_id: str
    is_running: bool = True

    content_segments: List[str] = field(default_factory=list)
    display_sequence: List[dict] = field(default_factory=list)
    reasoning: str = ""
    agent_steps: List[dict] = field(default_factory=list)
    tool_calls: List[dict] = field(default_factory=list)
    tool_results: List[dict] = field(default_factory=list)
    iteration: Optional[dict] = None
    search_progress: List[dict] = field(default_factory=list)
    search_failed: Optional[dict] = None
    file_attachments: List[dict] = field(default_factory=list)
    content: str = ""

    completed_data: Optional[dict] = None
    error: Optional[str] = None

    # Session-lock reservation: True between the endpoint's atomic reserve()
    # and the agent task's handoff. Provisional entries older than
    # _PROVISIONAL_TTL_SECONDS are treated as setup-dead and auto-released
    # (self-healing against exceptions before the agent task starts).
    provisional: bool = False
    reserved_at: float = 0.0

    _subscribers: List[asyncio.Queue] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _created_at: float = field(default_factory=time.time)
    _last_snapshot_saved: float = 0

    async def snapshot(self) -> dict:
        async with self._lock:
            return {
                "content_segments": list(self.content_segments),
                "display_sequence": list(self.display_sequence),
                "reasoning": self.reasoning,
                "agent_steps": list(self.agent_steps),
                "tool_calls": list(self.tool_calls),
                "tool_results": list(self.tool_results),
                "iteration": self.iteration,
                "search_progress": list(self.search_progress),
                "search_failed": self.search_failed,
                "file_attachments": list(self.file_attachments),
                "content": self.content,
                "is_running": self.is_running,
                "completed_data": self.completed_data,
                "error": self.error,
            }

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def broadcast(self, event: dict) -> None:
        async with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Overflow with a slow client: drop the OLDEST backlog
                    # item and keep the newest event. Draining the whole
                    # queue (the previous behavior) lost EVERY intermediate
                    # event — reasoning/text deltas vanished mid-stream and a
                    # dropped tool_call could keep the SSE relay's pre-tool
                    # gate armed for the entire turn (2026-08-06 user report:
                    # thinking streams, long silence, whole answer pops).
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    async def broadcast_done(self) -> None:
        sentinel = object()
        async with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(sentinel)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(sentinel)
                    except asyncio.QueueFull:
                        pass


_TTL_SECONDS = 300
_SNAPSHOT_SAVE_INTERVAL = 15  # seconds between periodic snapshot saves
# Reservation handoff window. Generous on purpose: the reserve→adopt span
# covers deathmatch intent classification + coordinator pre-pass LLM calls
# (slow providers can take >60s), and the endpoint refreshes reserved_at at
# setup milestones. The reaper is only a backstop for setup-dead requests.
_PROVISIONAL_TTL_SECONDS = 180


class ActiveAgentRegistry:
    _instance: Optional["ActiveAgentRegistry"] = None

    def __init__(self):
        self._agents: Dict[str, ActiveAgentState] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "ActiveAgentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def reserve(self, conversation_id: str) -> "ActiveAgentState | None":
        """Atomically reserve the conversation's agent slot (session lock).

        Returns the reserved (provisional) state on success, or None when an
        agent is already running — closing the check-then-act race between the
        endpoint guard and the agent task's register() call. Provisional
        entries expire after _PROVISIONAL_TTL_SECONDS so a request that dies
        during setup cannot lock the conversation forever (actively reaped by
        the cleanup loop AND lazily released here).
        """
        async with self._lock:
            existing = self._agents.get(conversation_id)
            if existing is not None and existing.is_running:
                if existing.provisional and (time.time() - existing.reserved_at) > _PROVISIONAL_TTL_SECONDS:
                    logger.warning(
                        "Auto-releasing expired provisional reservation: %s",
                        conversation_id,
                    )
                    self._agents.pop(conversation_id, None)
                else:
                    return None
            state = ActiveAgentState(
                conversation_id=conversation_id,
                provisional=True,
                reserved_at=time.time(),
            )
            self._agents[conversation_id] = state
        logger.info("Reserved active agent slot: %s (worker=%s)", conversation_id, shared_state.worker_id)
        return state

    async def release_reservation(self, conversation_id: str, expected: "ActiveAgentState") -> None:
        """Release a provisional reservation WITHOUT touching shared state.

        Identity-checked: only removes the slot if it still holds the exact
        state object we reserved (a newer request may have re-reserved it).
        Used on abort paths where a *different* (possibly cross-worker) run
        owns the live stream — deleting shared rows here would punch a hole
        in that run's recovery snapshots.
        """
        async with self._lock:
            current = self._agents.get(conversation_id)
            if current is expected and current.provisional:
                self._agents.pop(conversation_id, None)
                logger.info("Released provisional reservation: %s", conversation_id)

    async def release_dead_setup(self, conversation_id: str, expected: "ActiveAgentState") -> None:
        """Release a slot whose run died BEFORE its agent task started.

        ``release_reservation`` only handles still-provisional slots. A run
        cancelled between ``adopt_reservation`` and task creation leaves an
        ADOPTED (provisional=False) entry with ``is_running=True`` and no task
        ever unregistering it — a PERMANENT conversation_busy zombie (the
        "stop during setup → edit/resend yields no answer" bug). Identity-
        checked: never pops a slot re-reserved by a newer request.
        """
        async with self._lock:
            current = self._agents.get(conversation_id)
            if current is expected:
                self._agents.pop(conversation_id, None)
                logger.info("Released dead-setup agent slot: %s", conversation_id)

    async def release_setup_slot(self, conversation_id: str) -> None:
        """Best-effort release of a SETUP-PHASE slot (no running agent task).

        Called from the stop endpoint when no detached task exists. Only
        PROVISIONAL reservations are popped: those are pre-adopt setup runs
        whose adopt step later fails (conversation_superseded) so the run
        terminates instead of spawning an agent. Adopted-but-task-less states
        are NOT popped here — the stop endpoint has no identity reference, and
        a slot adopted by a live run must never be freed by an unrelated
        caller;         such states are cleaned by the generator's own identity-
        checked ``release_dead_setup`` when the SSE is cancelled. The pop
        itself never touches shared state (setup reservations have no
        snapshots).
        """
        async with self._lock:
            current = self._agents.get(conversation_id)
            if current is not None and current.provisional:
                self._agents.pop(conversation_id, None)
                logger.info("Released setup slot via stop endpoint: %s", conversation_id)

    async def adopt_reservation(self, conversation_id: str, expected: "ActiveAgentState") -> "ActiveAgentState | None":
        """Hand off a reservation to the agent task, identity-checked.

        Returns the adopted state (provisional cleared) when the registry
        still holds the exact reserved object; None when the slot was
        re-reserved by a newer request (this request is stale and must abort).
        """
        async with self._lock:
            current = self._agents.get(conversation_id)
            if current is not expected:
                return None
            current.provisional = False
            return current

    async def register(self, conversation_id: str) -> ActiveAgentState:
        async with self._lock:
            state = ActiveAgentState(conversation_id=conversation_id)
            self._agents[conversation_id] = state
        logger.info("Registered active agent: %s (worker=%s)", conversation_id, shared_state.worker_id)
        return state

    def get_local(self, conversation_id: str) -> Optional[ActiveAgentState]:
        """Return the in-process agent state only (no shared-state recovery).

        Used by the session-lock busy guard: local entries are always fresh
        (popped on unregister; gone after a crash), unlike shared-state
        snapshots which can be stale running-claims from a dead worker.
        """
        return self._agents.get(conversation_id)

    async def get(self, conversation_id: str) -> Optional[ActiveAgentState]:
        async with self._lock:
            state = self._agents.get(conversation_id)

        # Local hit — still running in this process
        if state is not None:
            return state

        # Missed locally — try recovery from shared state (another worker may
        # have a snapshot, or a previous worker may have crashed and this worker
        # is now responsible for the reconnect).
        if shared_state.is_db_enabled:
            recovery = await shared_state.load_agent_state(conversation_id)
            if recovery:
                snapshot = recovery.get("snapshot", {})
                saved_worker = recovery.get("worker_id", "unknown")
                # Distrust running-claims from dead/stale workers — the buffer
                # recovery applies the same distrust. Without it, a finished
                # run whose snapshot row survived (or a crashed worker's) is
                # resurrected as "live", which blocks every new /stream with
                # conversation_busy and makes resume ping forever (conv
                # 149ce886, 2026-08-01). The owning worker's heartbeat is the
                # ground truth: a live run refreshes its snapshot every 15s.
                if snapshot.get("is_running"):
                    saved_at = recovery.get("saved_at") or 0
                    try:
                        owner_alive = await shared_state.is_worker_active(saved_worker)
                    except Exception:
                        owner_alive = True
                    stale = (time.time() - saved_at) > 180
                    if stale or not owner_alive:
                        logger.warning(
                            "Discarding stale agent running-claim for conversation %s "
                            "(worker=%s alive=%s age=%ds)",
                            conversation_id, saved_worker, owner_alive,
                            int(time.time() - saved_at),
                        )
                        try:
                            await shared_state.delete_agent_state(conversation_id)
                        except Exception:
                            pass
                        return None
                logger.info(
                    "Recovering agent state for conversation %s from worker %s",
                    conversation_id, saved_worker,
                )
                state = ActiveAgentState(conversation_id=conversation_id)
                state.content_segments = snapshot.get("content_segments", [])
                state.display_sequence = snapshot.get("display_sequence", [])
                state.reasoning = snapshot.get("reasoning", "")
                state.agent_steps = snapshot.get("agent_steps", [])
                state.tool_calls = snapshot.get("tool_calls", [])
                state.tool_results = snapshot.get("tool_results", [])
                state.iteration = snapshot.get("iteration")
                state.search_progress = snapshot.get("search_progress", [])
                state.search_failed = snapshot.get("search_failed")
                state.file_attachments = snapshot.get("file_attachments", [])
                state.content = snapshot.get("content", "")
                state.completed_data = snapshot.get("completed_data")
                state.error = snapshot.get("error")
                state.is_running = snapshot.get("is_running", False)

                async with self._lock:
                    self._agents[conversation_id] = state
                return state

        return None

    async def unregister(self, conversation_id: str, expected: "ActiveAgentState | None" = None) -> None:
        async with self._lock:
            if expected is not None and self._agents.get(conversation_id) is not expected:
                # Identity mismatch: the slot was re-reserved by a newer run —
                # never pop another run's live state (stale task finally path).
                return
            state = self._agents.pop(conversation_id, None)
        if state:
            state.is_running = False
            logger.info("Unregistered active agent: %s", conversation_id)
            if shared_state.is_db_enabled:
                try:
                    await shared_state.delete_agent_state(conversation_id)
                except Exception:
                    logger.warning(
                        "Failed to delete agent state for %s from shared store",
                        conversation_id, exc_info=True,
                    )

    async def start_cleanup(self) -> None:
        async with self._lock:
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            if self._snapshot_task is None:
                self._snapshot_task = asyncio.create_task(self._snapshot_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            async with self._lock:
                expired = [
                    cid for cid, s in self._agents.items()
                    if not s.is_running and (now - s._created_at > _TTL_SECONDS)
                ]
                # Setup-dead provisional reservations (exception between
                # reserve() and agent-task handoff) would otherwise wedge the
                # conversation forever — reap them actively, not just lazily
                # on the next reserve().
                expired_provisional = [
                    cid for cid, s in self._agents.items()
                    if s.is_running and s.provisional and (now - s.reserved_at > _PROVISIONAL_TTL_SECONDS)
                ]
            for cid in expired:
                await self.unregister(cid)
                logger.debug("Cleaned up expired agent state: %s", cid)
            # Provisional reaping uses a LOCAL pop only: the state was never
            # adopted (no snapshots of its own), and routing through
            # unregister would delete a *different* live run's shared-state
            # recovery row in cross-worker setups. Re-check under the lock:
            # the entry may have been adopted or re-reserved since collection.
            async with self._lock:
                for cid in expired_provisional:
                    current = self._agents.get(cid)
                    if current is not None and current.provisional and (now - current.reserved_at > _PROVISIONAL_TTL_SECONDS):
                        self._agents.pop(cid, None)
                        logger.warning("Reaped setup-dead provisional reservation: %s", cid)

    async def _snapshot_loop(self) -> None:
        """Periodically persist agent state snapshots for cross-worker recovery."""
        if not shared_state.is_db_enabled:
            return
        while True:
            await asyncio.sleep(_SNAPSHOT_SAVE_INTERVAL)
            async with self._lock:
                agents = list(self._agents.items())
            for cid, state in agents:
                if not state.is_running:
                    continue
                if state.provisional:
                    # Reservation in setup, not a real agent yet — snapshotting
                    # it would persist a content-less zombie running-claim.
                    continue
                now = time.time()
                if now - state._last_snapshot_saved < _SNAPSHOT_SAVE_INTERVAL:
                    continue
                try:
                    snap = await state.snapshot()
                    await shared_state.save_agent_state(cid, snap)
                    state._last_snapshot_saved = now
                except Exception:
                    logger.warning(
                        "Failed to save agent snapshot for %s", cid, exc_info=True,
                    )

    # ─── Housekeeping: recover orphaned agents on startup ──────────

    async def recover_orphaned_tasks(self) -> None:
        """On startup, check for background tasks launched by a previous
        worker instance that is no longer alive. Mark them as failed so they
        do not get stuck in 'running' state forever."""
        if not shared_state.is_db_enabled:
            return
        try:
            from app.db.database import AsyncSessionLocal
            from sqlalchemy import text

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        """UPDATE agent_tasks SET status = 'failed',
                           error = 'Worker instance no longer alive (auto-recovery on startup)'
                           WHERE status = 'running'
                           AND worker_id IS NOT NULL
                           AND worker_id != :current_worker
                           AND started_at < NOW() - INTERVAL '5 minutes'"""
                    ),
                    {"current_worker": shared_state.worker_id},
                )
                orphaned = result.rowcount
                if orphaned:
                    await session.commit()
                    logger.info("Recovered %d orphaned tasks from previous workers", orphaned)
        except Exception:
            logger.warning("Failed to recover orphaned tasks", exc_info=True)
