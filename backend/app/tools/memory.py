# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Optional

from app.tools.registry import registry
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

_ENTRY_DELIMITER = "\n§\n"

_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}

_MEMORY_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
]

_memory_lock: asyncio.Lock | None = None


def _get_memory_lock() -> asyncio.Lock:
    global _memory_lock
    if _memory_lock is None:
        _memory_lock = asyncio.Lock()
    return _memory_lock


def _get_memory_dir() -> Path:
    import os as _os
    memory_dir = _os.environ.get("AGENT_MEMORY_DIR")
    if memory_dir:
        return Path(memory_dir)
    backend_root = config.backend_root
    return backend_root / "agent_memories"


def _scan_threats(content: str) -> Optional[str]:
    """Pure threat scan (invisible unicode + known prompt-injection patterns).
    NOT affected by ``super_admin_bypass`` — callers who feed untrusted model
    output (e.g. the voice memory-interjection chain) must use THIS, not
    ``_scan_memory_content``."""
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X}"
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'"
    return None


def _scan_memory_content(content: str) -> Optional[str]:
    if config.super_admin_bypass:
        return None
    return _scan_threats(content)


def _ensure_memory_dir(user_id: str) -> Path:
    d = _get_memory_dir() / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _aensure_memory_dir(user_id: str) -> Path:
    return await asyncio.to_thread(_ensure_memory_dir, user_id)


def _get_memory_path(target: str, user_id: str, ensure_dir: bool = True) -> Path:
    valid = {"user", "agent", "system"}
    if target not in valid:
        raise ValueError(f"Invalid target '{target}'. Must be one of: {', '.join(sorted(valid))}")
    if target == "system":
        return _get_memory_dir() / "func.md"
    if ensure_dir:
        return _ensure_memory_dir(user_id) / f"{target.upper()}.md"
    return _get_memory_dir() / user_id / f"{target.upper()}.md"


def _read_entries(text: str) -> list[str]:
    if not text or not text.strip():
        return []
    return [entry.strip() for entry in text.split(_ENTRY_DELIMITER) if entry.strip()]


def _write_entries(path: Path, entries: list[str]) -> None:
    if entries:
        path.write_text(_ENTRY_DELIMITER.join(entries) + _ENTRY_DELIMITER, encoding="utf-8")
    else:
        path.write_text("", encoding="utf-8")


async def _notify_subconscious_hook(user_id: str, action: str, target: str, content: str) -> None:
    from app.services.memory_runtime_state import memory_runtime_enabled
    if not memory_runtime_enabled(config) or not config.memory.get("subconscious_enabled", True):
        return
    if user_id == "_shared":
        return
    # remove 走对账复核（M&D §3.3），不把被删内容重新写入 subconscious
    if action not in ("add", "replace"):
        return
    if not content.strip():
        return
    try:
        import hashlib
        from app.db.database import AsyncSessionLocal
        from app.services.memory_subconscious_service import ingest_raw_unit
        async with AsyncSessionLocal() as db:
            source_id = f"legacy:file_memory:{target.upper()}:{hashlib.sha1(content.encode('utf-8')).hexdigest()[:16]}"
            await ingest_raw_unit(db, user_id, "file_memory", content[:500], [source_id])
            await db.commit()
    except Exception:
        logger.debug("subconscious hook failed for user=%s target=%s", user_id, target, exc_info=True)


async def memory(args: dict, **kwargs) -> str:
    action = args.get("action", "read")
    target = args.get("target", "agent")
    content = args.get("content", "") or ""
    key = args.get("key", "") or ""
    old = args.get("old", "") or ""

    user = kwargs.get("user")
    user_id = getattr(user, "id", None) if user else None
    if not user_id:
        user_id = kwargs.get("user_id", "_shared")
        if not user_id or user_id == "":
            user_id = "_shared"

    try:
        path = _get_memory_path(target, user_id, ensure_dir=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    if target == "system":
        if action != "read":
            return json.dumps({"error": "system target is read-only, only action='read' is allowed"}, ensure_ascii=False)
        if not await asyncio.to_thread(path.exists):
            return json.dumps({"error": "system document (func.md) not found"}, ensure_ascii=False)
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return json.dumps({
            "action": "read",
            "target": "system",
            "content": text,
        }, ensure_ascii=False)

    lock = _get_memory_lock()

    async with lock:
        # NO disk side effect before a successful write: the old code created
        # {user_id}/AGENT.md + a 0-byte file unconditionally on every call,
        # producing dozens of meaningless empty per-user folders (53 shells
        # cleaned 2026-08-14). Reads must create nothing; failed writes
        # (replace/remove with no match) must create nothing either. The
        # store is created only at the moment a write actually lands.
        current_text = ""
        if await asyncio.to_thread(path.exists):
            try:
                current_text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            except FileNotFoundError:
                # TOCTOU: the GDPR erase path (api/memory.py) rmtree's the
                # user dir without taking this lock — treat a vanished
                # store as empty instead of failing the tool call.
                current_text = ""
        current_entries = _read_entries(current_text)

        if action == "read":
            entries_for_display = []
            for i, entry in enumerate(current_entries):
                entries_for_display.append(f"[{i}] {entry}")
            return json.dumps({
                "action": "read",
                "target": target,
                "count": len(current_entries),
                "entries": entries_for_display,
            }, ensure_ascii=False)

        elif action == "add":
            if not content.strip():
                return json.dumps({"error": "Content required for add action"}, ensure_ascii=False)
            scan_error = _scan_memory_content(content)
            if scan_error:
                logger.warning("Memory add blocked: %s", scan_error)
                content = "[BLOCKED: Potential prompt injection detected]"

            new_entry = f"{content}"
            current_entries.append(new_entry)
            await _aensure_memory_dir(user_id)
            await asyncio.to_thread(_write_entries, path, current_entries)
            logger.info("Memory: added entry to %s for user %s (%d entries total)", target, user_id, len(current_entries))
            asyncio.create_task(_notify_subconscious_hook(user_id, "add", target, content))
            return json.dumps({
                "action": "add",
                "target": target,
                "index": len(current_entries) - 1,
                "count": len(current_entries),
            }, ensure_ascii=False)

        elif action == "replace":
            if not old.strip():
                return json.dumps({"error": "Old content required for replace action (use substring matching)"}, ensure_ascii=False)
            if not content.strip():
                return json.dumps({"error": "Content required for replace action"}, ensure_ascii=False)

            scan_error = _scan_memory_content(content)
            if scan_error:
                logger.warning("Memory replace blocked: %s", scan_error)
                return json.dumps({"error": scan_error}, ensure_ascii=False)

            matched_count = 0
            new_entries = []
            for entry in current_entries:
                if old in entry and matched_count == 0:
                    new_entries.append(content)
                    matched_count += 1
                else:
                    new_entries.append(entry)

            if matched_count == 0:
                return json.dumps({"error": f"No entry matching '{old}' found"}, ensure_ascii=False)

            await _aensure_memory_dir(user_id)
            await asyncio.to_thread(_write_entries, path, new_entries)
            logger.info("Memory: replaced entry in %s for user %s", target, user_id)
            asyncio.create_task(_notify_subconscious_hook(user_id, "replace", target, content))
            return json.dumps({
                "action": "replace",
                "target": target,
                "count": len(new_entries),
            }, ensure_ascii=False)

        elif action == "remove":
            if not key.strip():
                return json.dumps({"error": "Key (substring) required for remove action"}, ensure_ascii=False)

            removed = False
            new_entries = []
            for entry in current_entries:
                if key in entry and not removed:
                    removed = True
                    continue
                new_entries.append(entry)

            if not removed:
                return json.dumps({"error": f"No entry matching '{key}' found"}, ensure_ascii=False)

            if new_entries:
                await _aensure_memory_dir(user_id)
                await asyncio.to_thread(_write_entries, path, new_entries)
            else:
                # Store fully emptied — delete the file instead of leaving a
                # 0-byte shell behind (rmdir tolerates a sibling USER.md).
                try:
                    path.unlink()
                    path.parent.rmdir()
                except OSError:
                    pass
            logger.info("Memory: removed entry from %s for user %s", target, user_id)
            return json.dumps({
                "action": "remove",
                "target": target,
                "count": len(new_entries),
            }, ensure_ascii=False)

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use read/add/replace/remove."}, ensure_ascii=False)


registry.register(
    name="memory",
    toolset="core",
    schema={
        "name": "memory",
        "description": (
            "Persistent cross-session memory. Three targets: "
            "agent (your notes/observations, incl. a user-given name/nickname), "
            "user (user profile/preferences), "
            "system (read-only product features doc func.md — read first when asked "
            "about system features or version updates).\n"
            "Actions: read (list entries / full system doc), add (new entry), "
            "replace (substring old→content), remove (substring key).\n"
            "Save durable facts (preferences, conventions, corrections). "
            "Do NOT save task progress or TODO state. "
            "Record user-given names, never invent one. Write facts, not instructions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "add", "replace", "remove"],
                    "description": "Action to perform on memory.",
                },
                "target": {
                    "type": "string",
                    "enum": ["agent", "user", "system"],
                    "description": "Which memory store to operate on. 'agent' for your observations, 'user' for user profile, 'system' for the product features document (read-only).",
                },
                "content": {
                    "type": "string",
                    "description": "Content for add/replace actions. The memory entry text.",
                },
                "key": {
                    "type": "string",
                    "description": "Substring to match for remove action.",
                },
                "old": {
                    "type": "string",
                    "description": "Substring to match for replace action.",
                },
            },
            "required": ["action", "target"],
        },
    },
    handler=memory,
    is_async=True,
    description="Persistent agent/user memory across sessions",
    emoji="",
)
