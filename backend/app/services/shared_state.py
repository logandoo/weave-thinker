# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""
SharedStateManager — Multi-instance state backend abstraction.

Provides a unified interface for state that must survive worker restarts
and be accessible across multiple process instances.

Backends:
  - LocalMemoryBackend: In-process dict (current single-instance mode)
  - DatabaseBackend:   PostgreSQL-backed (enables multi-instance recovery)

When a PostgreSQL backend is active the following state is persisted:
  - Active agent snapshots (for cross-worker SSE reconnect recovery)
  - Stream buffer content (for reconnect replay)
  - Background/detached task references (in agent_tasks table)
"""
import asyncio
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

WORKER_INSTANCE_ID = os.environ.get("WORKER_INSTANCE_ID", str(uuid.uuid4()))
WORKER_STARTED_AT = time.time()

# ─── Backend interface ─────────────────────────────────────────────


class SharedStateBackend(ABC):
    """Abstract backend for shared state operations."""

    @abstractmethod
    async def get(self, key: str) -> Optional[bytes]:
        """Retrieve a raw value by key."""

    @abstractmethod
    async def set(self, key: str, value: bytes, ttl_seconds: Optional[int] = None) -> None:
        """Store a raw value with optional TTL."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a key."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if a key exists."""

    @abstractmethod
    async def keys(self, pattern: str = "*") -> List[str]:
        """List keys matching a pattern."""


# ─── Local memory backend (single-instance default) ────────────────


class LocalMemoryBackend(SharedStateBackend):
    """In-process memory backend for single-worker deployments."""

    def __init__(self):
        self._data: Dict[str, bytes] = {}
        self._expiry: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[bytes]:
        async with self._lock:
            if key in self._expiry and time.time() > self._expiry[key]:
                self._data.pop(key, None)
                self._expiry.pop(key, None)
                return None
            return self._data.get(key)

    async def set(self, key: str, value: bytes, ttl_seconds: Optional[int] = None) -> None:
        async with self._lock:
            self._data[key] = value
            if ttl_seconds is not None:
                self._expiry[key] = time.time() + ttl_seconds

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)
            self._expiry.pop(key, None)

    async def exists(self, key: str) -> bool:
        async with self._lock:
            if key in self._expiry and time.time() > self._expiry[key]:
                self._data.pop(key, None)
                self._expiry.pop(key, None)
                return False
            return key in self._data

    async def keys(self, pattern: str = "*") -> List[str]:
        async with self._lock:
            now = time.time()
            expired = [k for k, t in self._expiry.items() if t <= now]
            for k in expired:
                self._data.pop(k, None)
                self._expiry.pop(k, None)
            return list(self._data.keys())


# ─── Database backend (multi-instance via PostgreSQL) ──────────────


@dataclass
class _DbBackendConfig:
    """Holds a session-factory so the backend can create independent sessions."""

    session_factory: Any = None


_db_config = _DbBackendConfig()


def configure_db_backend(session_factory) -> None:
    """Wire the async session factory into the DB backend."""
    _db_config.session_factory = session_factory


class DatabaseBackend(SharedStateBackend):
    """PostgreSQL-backed shared state using a dedicated key-value table."""

    async def _ensure_table(self, session: AsyncSession) -> None:
        await session.execute(text(
            """CREATE TABLE IF NOT EXISTS shared_kv_store (
                key TEXT PRIMARY KEY,
                value BYTEA NOT NULL,
                expires_at DOUBLE PRECISION,
                worker_id TEXT,
                created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
            )"""
        ))
        await session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_shared_kv_expires ON shared_kv_store(expires_at)"
        ))
        await session.commit()

    async def _session(self) -> AsyncSession:
        if _db_config.session_factory is None:
            raise RuntimeError("DatabaseBackend: session_factory not configured")
        return _db_config.session_factory()

    async def get(self, key: str) -> Optional[bytes]:
        session = await self._session()
        try:
            await self._ensure_table(session)
            result = await session.execute(
                text("SELECT value, expires_at FROM shared_kv_store WHERE key = :key"),
                {"key": key},
            )
            row = result.fetchone()
            if row is None:
                return None
            if row[1] is not None and time.time() > float(row[1]):
                await session.execute(
                    text("DELETE FROM shared_kv_store WHERE key = :key"),
                    {"key": key},
                )
                await session.commit()
                return None
            return row[0] if isinstance(row[0], bytes) else row[0].encode()
        finally:
            await session.close()

    async def set(self, key: str, value: bytes, ttl_seconds: Optional[int] = None) -> None:
        expires_at = (time.time() + ttl_seconds) if ttl_seconds is not None else None
        session = await self._session()
        try:
            await self._ensure_table(session)
            await session.execute(
                text("""INSERT INTO shared_kv_store (key, value, expires_at, worker_id)
                        VALUES (:key, :value, :expires_at, :worker_id)
                        ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            expires_at = EXCLUDED.expires_at,
                            worker_id = EXCLUDED.worker_id"""),
                {
                    "key": key,
                    "value": value,
                    "expires_at": expires_at,
                    "worker_id": WORKER_INSTANCE_ID,
                },
            )
            await session.commit()
        finally:
            await session.close()

    async def delete(self, key: str) -> None:
        session = await self._session()
        try:
            await self._ensure_table(session)
            await session.execute(
                text("DELETE FROM shared_kv_store WHERE key = :key"),
                {"key": key},
            )
            await session.commit()
        finally:
            await session.close()

    async def exists(self, key: str) -> bool:
        val = await self.get(key)
        return val is not None

    async def keys(self, pattern: str = "*") -> List[str]:
        session = await self._session()
        try:
            await self._ensure_table(session)
            if pattern == "*":
                result = await session.execute(text("SELECT key FROM shared_kv_store"))
            else:
                like_pattern = pattern.replace("*", "%")
                result = await session.execute(
                    text("SELECT key FROM shared_kv_store WHERE key LIKE :pattern"),
                    {"pattern": like_pattern},
                )
            return [row[0] for row in result.fetchall()]
        finally:
            await session.close()


# ─── Manager (singleton, chooses backend) ──────────────────────────


class SharedStateManager:
    """Singleton facade that selects the appropriate backend."""

    _instance: Optional["SharedStateManager"] = None

    def __init__(self):
        self._backend: SharedStateBackend = LocalMemoryBackend()
        self._db_enabled = False
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> "SharedStateManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def enable_db_backend(self) -> None:
        """Switch to database backend (call at startup after session factory is wired)."""
        async with self._lock:
            self._backend = DatabaseBackend()
            self._db_enabled = True
            logger.info("SharedStateManager: switched to DatabaseBackend (worker=%s)", WORKER_INSTANCE_ID)

    @property
    def backend(self) -> SharedStateBackend:
        return self._backend

    @property
    def worker_id(self) -> str:
        return WORKER_INSTANCE_ID

    @property
    def worker_started_at(self) -> float:
        return WORKER_STARTED_AT

    @property
    def is_db_enabled(self) -> bool:
        return self._db_enabled

    # ── Convenience methods for JSON-serializable Python objects ──

    async def get_json(self, key: str) -> Optional[Any]:
        raw = await self._backend.get(key)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    async def set_json(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        raw = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        await self._backend.set(key, raw, ttl_seconds=ttl_seconds)

    # ── Agent recovery state ──

    _AGENT_KEY_PREFIX = "agent:state:"
    _AGENT_TTL = 600  # 10 minutes

    async def save_agent_state(self, conversation_id: str, snapshot: dict) -> None:
        """Persist agent state snapshot for cross-worker recovery."""
        key = f"{self._AGENT_KEY_PREFIX}{conversation_id}"
        payload = {
            "worker_id": WORKER_INSTANCE_ID,
            "conversation_id": conversation_id,
            "snapshot": snapshot,
            "saved_at": time.time(),
        }
        await self.set_json(key, payload, ttl_seconds=self._AGENT_TTL)

    async def load_agent_state(self, conversation_id: str) -> Optional[dict]:
        """Load agent state snapshot (may have been saved by a different worker)."""
        key = f"{self._AGENT_KEY_PREFIX}{conversation_id}"
        payload = await self.get_json(key)
        if payload is None:
            return None
        logger.info(
            "Loaded agent state for conversation %s (saved by worker %s)",
            conversation_id,
            payload.get("worker_id", "unknown"),
        )
        return payload

    async def delete_agent_state(self, conversation_id: str) -> None:
        key = f"{self._AGENT_KEY_PREFIX}{conversation_id}"
        await self._backend.delete(key)

    # ── Stream buffer recovery state ──

    _BUFFER_KEY_PREFIX = "buffer:state:"
    _BUFFER_TTL = 600  # 10 minutes

    async def save_buffer_state(self, conversation_id: str, snapshot: dict) -> None:
        key = f"{self._BUFFER_KEY_PREFIX}{conversation_id}"
        payload = {
            "worker_id": WORKER_INSTANCE_ID,
            "conversation_id": conversation_id,
            "snapshot": snapshot,
            "saved_at": time.time(),
        }
        await self.set_json(key, payload, ttl_seconds=self._BUFFER_TTL)

    async def load_buffer_state(self, conversation_id: str) -> Optional[dict]:
        key = f"{self._BUFFER_KEY_PREFIX}{conversation_id}"
        return await self.get_json(key)

    async def delete_buffer_state(self, conversation_id: str) -> None:
        key = f"{self._BUFFER_KEY_PREFIX}{conversation_id}"
        await self._backend.delete(key)

    async def is_worker_active(self, worker_id: str) -> bool:
        """Whether the given worker instance is registered as active.

        Used to distrust buffer snapshots left behind by stopped workers;
        unknown/empty worker ids fail open (single-instance mode).

        An 'active' status row alone is NOT enough: hard kills (SIGKILL,
        crash) never run the shutdown handler that flips the row to
        'stopped', so a dead worker can claim active forever. Heartbeat
        freshness closes the gap — the heartbeat loop refreshes every 30s,
        so a stale heartbeat means the owner is gone and any snapshot it
        left behind is untrustworthy (conv 149ce886 zombie buffer,
        2026-08-01).
        """
        _HEARTBEAT_FRESH_WINDOW = 120
        if not worker_id:
            return True
        if worker_id == WORKER_INSTANCE_ID:
            return True
        if not self._db_enabled or _db_config.session_factory is None:
            return True
        session = _db_config.session_factory()
        try:
            result = await session.execute(
                text("SELECT status, last_heartbeat FROM worker_instances WHERE id = :id"),
                {"id": worker_id},
            )
            row = result.fetchone()
            if row is None:
                return False
            if row[0] != "active":
                return False
            last_hb = float(row[1] or 0)
            if last_hb <= 0 or time.time() - last_hb > _HEARTBEAT_FRESH_WINDOW:
                return False
            return True
        finally:
            await session.close()

    # ── Background task tracking ──

    _BG_TASK_KEY_PREFIX = "bg_task:"
    _BG_TASK_TTL = 7200  # 2 hours

    async def track_background_task(self, conversation_id: str, task_meta: dict) -> None:
        """Record a detached/background task for cross-worker awareness."""
        key = f"{self._BG_TASK_KEY_PREFIX}{conversation_id}"
        payload = {
            "worker_id": WORKER_INSTANCE_ID,
            "conversation_id": conversation_id,
            "meta": task_meta,
            "tracked_at": time.time(),
        }
        await self.set_json(key, payload, ttl_seconds=self._BG_TASK_TTL)

    async def untrack_background_task(self, conversation_id: str) -> None:
        key = f"{self._BG_TASK_KEY_PREFIX}{conversation_id}"
        await self._backend.delete(key)

    async def get_active_background_tasks(self) -> List[dict]:
        """List all tracked background tasks across all workers."""
        if not self._db_enabled:
            return []
        keys = await self._backend.keys(f"{self._BG_TASK_KEY_PREFIX}*")
        tasks = []
        for key in keys:
            payload = await self.get_json(key)
            if payload:
                tasks.append(payload)
        return tasks


# ─── Module-level singleton ────────────────────────────────────────

shared_state = SharedStateManager.get_instance()
