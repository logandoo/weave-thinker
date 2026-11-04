# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""C-10: production-failure intake queue (2026-10-18).

Runtime hooks must never write into the git-tracked regression corpus (the
production container may be read-only and a runtime write would bypass
review).  Instead they append one JSON line per event to a runtime queue;
``python -m scripts.prompt_feedback intake`` merges the queue into
``tests/prompt_evals/regressions.json`` with the same idempotency key.

Best-effort by design: a failure to record feedback must never break a chat
turn or a tool call.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MAX_INPUT = 8000
_MAX_NOTE = 2000
_MAX_QUEUE_BYTES = 5_000_000
_KEYS_CACHE: Dict[str, set[str]] = {}
_KEYS_SIG: Dict[str, Any] = {}


def queue_path() -> Path:
    override = str(os.environ.get("WT_PROMPT_FEEDBACK_QUEUE", "") or "").strip()
    if override:
        return Path(override).expanduser()
    backend_root = Path(__file__).resolve().parents[2]
    # output_files/ is gitignored (runtime artifacts); agent_memories/ is not
    # guaranteed to be, and a queue in the source tree is a `git add -A` risk.
    return backend_root / "output_files" / "_prompt_feedback.jsonl"


def _redact(text: str) -> str:
    try:
        from app.prompts.observability import redact_secrets

        return redact_secrets(str(text or ""))
    except Exception:
        return str(text or "")


def _event_key(prompt_id: str, source: str, input_text: str, expected: str) -> str:
    raw = "\0".join((str(prompt_id), str(source), str(input_text), str(expected)))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _existing_keys(path: Path) -> set[str]:
    try:
        st = path.stat()
        signature: Any = (st.st_mtime_ns, st.st_size)
    except OSError:
        signature = None
    cached = _KEYS_CACHE.get(str(path))
    if cached is not None and _KEYS_SIG.get(str(path)) == signature:
        return cached
    keys: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and row.get("key"):
                    keys.add(str(row["key"]))
    except FileNotFoundError:
        pass
    _KEYS_CACHE[str(path)] = keys
    _KEYS_SIG[str(path)] = signature
    return keys


def record_feedback_event(
    *,
    prompt_id: str,
    source: str,
    input_text: str,
    expected: str = "reject",
    note: str = "",
    must_contain: Optional[List[str]] = None,
    must_not_contain: Optional[List[str]] = None,
) -> str:
    """Append one deduped intake event. Returns its key ("" on failure)."""
    prompt_id = str(prompt_id or "").strip()
    source = str(source or "").strip()
    input_text = _redact(str(input_text or ""))[:_MAX_INPUT]
    note = _redact(str(note or ""))[:_MAX_NOTE]
    if not prompt_id or not source or not input_text:
        return ""
    key = _event_key(prompt_id, source, input_text, expected)
    path = queue_path()
    try:
        with _LOCK:
            keys = _existing_keys(path)
            if key in keys:
                return key
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size > _MAX_QUEUE_BYTES:
                    path.replace(path.with_name(path.name + ".1"))
                    keys.clear()
                    logger.warning("prompt feedback queue rotated: %s", path)
            except OSError:
                pass
            row: Dict[str, Any] = {
                "key": key,
                "kind": "intake",
                "prompt_id": prompt_id,
                "source": source,
                "input": input_text,
                "expected": str(expected or ""),
                "note": note,
                "must_contain": list(must_contain or []),
                "must_not_contain": list(must_not_contain or []),
                "created": datetime.now(timezone.utc).isoformat(),
            }
            # 0600: drafts/tool errors can contain user content or credentials.
            import os

            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            keys.add(key)
            try:
                st = path.stat()
                _KEYS_SIG[str(path)] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        return key
    except Exception:
        logger.warning("prompt feedback intake skipped (write failed)", exc_info=True)
        return ""


def read_queue(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or queue_path()
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and row.get("key"):
                    rows.append(row)
    except FileNotFoundError:
        return []
    return rows
