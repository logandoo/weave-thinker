# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P1a TMA-NM: append-only, tamper-evident verdict hash chain (M4).

Pure chain functions (``next_entry`` / ``verify_records``) are the testable
core; ``append_verdict`` persists them under a ``SELECT ... FOR UPDATE`` head
row so concurrent writers cannot fork the chain.  A failed append never
changes the act gate's allow/deny decision — M4 is evidence, not authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Iterable

from app.core.config import get_config

logger = logging.getLogger(__name__)

KEY_ID = "v1"
_CHAIN_FIELDS = ("prev_hash", "entry_hash", "sig")
_DEV_MARKERS = (
    "dev-secret", "change-in-production", "changeme", "placeholder",
    "weave-tma-verdict-dev", "weave-memory-provenance-dev",
)


def _key_material() -> str:
    cfg = get_config()
    dedicated = str(cfg.security.get("tma_verdict_key", "") or "").strip()
    if dedicated:
        return dedicated
    base = str(cfg.security.get("memory_hmac_key", "") or "").strip()
    if not base:
        base = str(cfg.security_jwt_secret_key or "")
    return base


def _keyring() -> dict[str, str]:
    raw = get_config().security.get("tma_verdict_keyring") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(kid).strip(): str(material).strip()
        for kid, material in raw.items()
        if str(kid).strip() and str(material).strip()
    }


def _current_kid() -> str:
    raw = str(get_config().security.get("tma_verdict_key_id", KEY_ID) or KEY_ID).strip()
    kid = raw or KEY_ID
    kid = kid[:-4] if kid.endswith("-dev") else kid
    # DB columns are VARCHAR(20); the dev suffix consumes 4 chars.
    if len(kid) > 16:
        logger.warning(
            "tma_verdict_key_id too long for VARCHAR(20)+dev suffix; using v1",
        )
        kid = KEY_ID
    return kid


def _material_for_kid(kid: str) -> str:
    base = str(kid or "").strip()
    ring = _keyring()
    # Exact kid wins, so a rotated box can keep the old dev key addressable
    # as ``v1-dev`` while the current kid ``v1`` uses the new key.
    if base in ring:
        return ring[base]
    if base.endswith("-dev"):
        stripped = base[:-4]
        if stripped in ring:
            return ring[stripped]
        if stripped == _current_kid():
            return _key_material()
        return ""
    if base == _current_kid():
        return _key_material()
    return ""


def _secret_for_kid(kid: str) -> bytes | None:
    material = _material_for_kid(kid)
    if not material:
        return None
    return ("tma-verdict\x00" + material).encode("utf-8")


def verdict_key_status() -> str:
    """ready | dev — a dev key makes the chain forgeable (advisory evidence)."""
    material = _material_for_kid(_current_kid()) or _key_material()
    if not material or len(material) < 32:
        return "dev"
    low = material.casefold()
    return "dev" if any(marker in low for marker in _DEV_MARKERS) else "ready"


def verdict_key_id() -> str:
    return _current_kid() if verdict_key_status() == "ready" else f"{_current_kid()}-dev"


def key_status_for_kids(kids: Iterable[str]) -> str:
    """Chain-level trust: any dev/unknown historical kid taints the whole chain."""
    statuses = set()
    for raw in kids:
        kid = str(raw or "").strip()
        if not kid or kid.endswith("-dev"):
            statuses.add("dev")
            continue
        material = _material_for_kid(kid)
        if not material:
            statuses.add("unknown")
        elif len(material) < 32 or any(
            marker in material.casefold() for marker in _DEV_MARKERS
        ):
            statuses.add("dev")
        else:
            statuses.add("ready")
    if "dev" in statuses:
        return "dev"
    if "unknown" in statuses:
        return "unknown"
    return "ready"


async def chain_key_status(db: Any, tenant_id: str = "") -> str:
    from sqlalchemy import text

    tenant = str(tenant_id or "")
    kids: set[str] = set()
    for table in (
        "memory_verdicts", "memory_verdict_checkpoints",
        "memory_verdict_redacted",
    ):
        try:
            rows = (await db.execute(
                text(f"SELECT DISTINCT key_id FROM {table} WHERE tenant_id = :tenant"),
                {"tenant": tenant},
            )).fetchall()
        except Exception:
            continue
        kids.update(str(row[0] or "") for row in rows)
    return key_status_for_kids(kids)


def _secret(secret: bytes | None = None) -> bytes:
    if secret is not None:
        return secret
    material = _material_for_kid(verdict_key_id()) or _key_material()
    if not material:
        return b"weave-tma-verdict-" + b"dev"
    return ("tma-verdict\x00" + material).encode("utf-8")


def _signing_key(kid: str) -> bytes | None:
    return _secret_for_kid(kid)


def canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sign_payload(payload: dict[str, Any], *, secret: bytes | None = None) -> str:
    return hmac.new(
        _secret(secret), canonical_payload(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# P2: write-time redaction + hash-only retention.
#
# The chain signs the payload, so stored rows can never be redacted after the
# fact without breaking verification.  Redaction therefore happens BEFORE
# signing, and old payloads leave the chain only as hash-only tombstones that
# are covered by a signed retention boundary.
# ---------------------------------------------------------------------------

_REDACTION_RULES = (
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [redacted]"),
    # key=value / key: value for secret-named keys.  The key may be quoted
    # (JSON) or carry a vendor/DB prefix (client_secret, db_password, ...);
    # anchoring on a non-word boundary after the prefix catches both.
    (
        re.compile(
            r"(?i)([\"']?[A-Za-z0-9_.-]{0,64}(?:api[_-]?key|access[_-]?key|secret|"
            r"password|passwd|token|private[_-]?key)[A-Za-z0-9_.-]*[\"']?)"
            r"\s*[:=]\s*[\"']?[^\s\"',;}\]]{3,}[\"']?"
        ),
        r"\1=[redacted]",
    ),
    # PEM private keys (bounded; multiline).
    (
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]{0,4000}?"
            r"-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
        "[redacted]",
    ),
    # Vendor token shapes.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), "[redacted]"),
    # URL userinfo (https://user:pass@host).
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@"),
        r"\1[redacted]@",
    ),
    # Long hex blobs (HMACs/keys/activation codes).
    (re.compile(r"\b[A-Fa-f0-9]{40,}\b"), "[redacted]"),
)


def redact_verdict_reason(text: Any, *, limit: int = 1000) -> str:
    """Mask credential-shaped substrings and bound the stored reason.

    The input is capped before the rules run: the key-value rule still has a
    bounded look-around, and the stored reason is limited to 1000 chars anyway.
    """
    out = str(text or "")[: max(4000, int(limit) * 4)]
    for pattern, replacement in _REDACTION_RULES:
        out = pattern.sub(replacement, out)
    if limit and len(out) > int(limit):
        out = out[: max(0, int(limit) - 1)].rstrip() + "…"
    return out


def build_verdict_payload(
    *,
    tenant_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    action_fingerprint: str = "",
    tool_name: str = "",
    decision: str = "",
    reason: str = "",
    taint: str = "untrusted",
    corroborators: list[str] | None = None,
    mode: str = "shadow",
    blocked: bool = False,
    record_id: str | None = None,
    redact: bool = True,
    created_at_epoch: int | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    """Build the signed payload exactly as it will be stored.

    Redaction is part of payload construction (before ``next_entry`` signs it),
    so a later verifier re-signs the same redacted body.
    """
    return {
        "id": str(record_id or uuid.uuid4()),
        "tenant_id": str(tenant_id or ""),
        "user_id": str(user_id or ""),
        "conversation_id": str(conversation_id or ""),
        "action_fingerprint": str(action_fingerprint or ""),
        "tool_name": str(tool_name or ""),
        "decision": str(decision or ""),
        "reason": redact_verdict_reason(reason) if redact else str(reason or ""),
        "taint": str(taint or "untrusted"),
        "corroborators": list(corroborators or []),
        "mode": str(mode or "shadow"),
        "blocked": bool(blocked),
        "created_at_epoch": int(
            created_at_epoch if created_at_epoch is not None else time.time()
        ),
        "key_id": str(key_id or verdict_key_id()),
    }


def build_retention_record(
    tenant_id: str,
    pruned_through_seq: int,
    boundary_hash: str,
    boundary_prev_hash: str,
    *,
    key_id: str | None = None,
    secret: bytes | None = None,
    updated_at_epoch: int | None = None,
) -> dict[str, Any]:
    """Signed retention boundary over the pruned prefix of a verdict chain."""
    kid = str(key_id or verdict_key_id())
    body = {
        "tenant_id": str(tenant_id or ""),
        "pruned_through_seq": int(pruned_through_seq or 0),
        "boundary_hash": str(boundary_hash or ""),
        "boundary_prev_hash": str(boundary_prev_hash or ""),
        "key_id": kid,
        "updated_at_epoch": int(
            updated_at_epoch if updated_at_epoch is not None else time.time()
        ),
    }
    signing_key = secret if secret is not None else _secret_for_kid(kid)
    if signing_key is None:
        raise ValueError(f"unknown verdict key id: {kid}")
    body["sig"] = sign_payload(body, secret=signing_key)
    return body


def verify_retention_record(
    record: dict[str, Any] | None,
    *,
    secret: bytes | None = None,
) -> tuple[bool, str]:
    if not isinstance(record, dict):
        return False, "malformed_retention"
    kid = str(record.get("key_id") or "")
    signing_key = secret if secret is not None else _secret_for_kid(kid)
    if signing_key is None:
        return False, f"key:{kid or 'unknown'}"
    body = {key: value for key, value in record.items() if key != "sig"}
    expected = sign_payload(body, secret=signing_key)
    if hmac.compare_digest(
        expected.encode("utf-8"), str(record.get("sig") or "").encode("utf-8"),
    ):
        return True, ""
    return False, f"retention:{record.get('pruned_through_seq')}"


def check_retention_forward(
    previous: dict[str, Any] | None,
    new: dict[str, Any],
) -> tuple[bool, str]:
    """Retention may only move forward; an equal boundary must be identical."""
    if not isinstance(new, dict):
        return False, "malformed_retention"
    if previous is None:
        return True, ""
    try:
        prev_seq = int(previous.get("pruned_through_seq") or 0)
        new_seq = int(new.get("pruned_through_seq") or 0)
    except (TypeError, ValueError):
        return False, "malformed_retention"
    if new_seq < prev_seq:
        return False, "retention_shrink"
    if new_seq == prev_seq:
        for field in ("boundary_hash", "boundary_prev_hash", "tenant_id"):
            if str(previous.get(field) or "") != str(new.get(field) or ""):
                return False, "retention_rewrite"
    return True, ""


def plan_retention(
    rows: Iterable[dict[str, Any]],
    *,
    pruned_through_seq: int = 0,
    checkpoint_seq: int = 0,
    cutoff_epoch: int = 0,
) -> dict[str, Any]:
    """Choose the next prunable prefix (pure; the CLI executes it).

    Only a contiguous prefix that is fully covered by a signed checkpoint,
    older than the cutoff, and not a deny/blocked verdict may be pruned.
    Stopping at the first exception keeps the chain contiguous by construction.
    """
    base = int(pruned_through_seq or 0)
    checkpoint = int(checkpoint_seq or 0)
    cutoff = int(cutoff_epoch or 0)
    selected: list[dict[str, Any]] = []
    stop = "end"
    expected = base + 1
    for row in sorted(list(rows or []), key=lambda r: int(r.get("seq") or 0)):
        seq = int(row.get("seq") or 0)
        if seq <= base:
            continue
        if seq != expected:
            stop = "gap"
            break
        if seq > checkpoint:
            stop = "checkpoint"
            break
        if str(row.get("decision") or "") == "deny" or bool(row.get("blocked")):
            stop = "deny_kept"
            break
        try:
            created_epoch = int(row.get("created_at_epoch") or 0)
        except (TypeError, ValueError):
            created_epoch = 0
        if created_epoch <= 0:
            # Legacy rows upgraded with DEFAULT 0 have an unknown real age;
            # never treat them as ancient (fail closed).
            stop = "unknown_age"
            break
        if created_epoch >= cutoff:
            stop = "too_new"
            break
        selected.append(row)
        expected += 1
    if not selected:
        return {
            "selected": [],
            "pruned_through_seq": base,
            "boundary_hash": "",
            "boundary_prev_hash": "",
            "stop": stop if stop != "end" else "none",
        }
    last = selected[-1]
    return {
        "selected": selected,
        "pruned_through_seq": int(last.get("seq") or 0),
        "boundary_hash": str(last.get("entry_hash") or ""),
        "boundary_prev_hash": str(last.get("prev_hash") or ""),
        "stop": stop,
    }


def _retention_start(
    retention: dict[str, Any] | None,
) -> tuple[bool, str, int, str]:
    """Validate the retention record.

    The loaded chain still starts at seq 1: pruned rows are hash-only
    tombstones, so chain linkage and checkpoint Merkle roots are verifiable
    across the boundary without a special start offset.  A caller that loads
    only live rows sees a seq gap and fails closed.
    """
    if retention is None:
        return True, "", 0, ""
    ok, reason = verify_retention_record(retention)
    if not ok:
        return False, reason, 0, ""
    return True, "", 0, ""


def _check_retention_consistency(
    retention: dict[str, Any] | None,
    records: list[dict[str, Any]],
    head_seq: int,
) -> tuple[bool, str]:
    """A tombstone is trusted only inside an exact, signed pruned prefix.

    Without these checks a DB-only attacker could convert any live row
    (including a deny/blocked verdict) into a hash-only tombstone and keep a
    green chain.  Every pruned row must therefore be (a) covered by a signed
    retention record, (b) contiguous from seq 1, and (c) no higher than the
    signed boundary.  Shrinking or replaying the boundary leaves tombstones
    above it and fails.
    """
    pruned_seqs: set[int] = set()
    for record in records:
        if record.get("pruned") is not True:
            continue
        try:
            pruned_seqs.add(int(record.get("seq")))
        except (TypeError, ValueError):
            return False, "malformed_pruned"
    if retention is None:
        return (False, "unsigned_pruned") if pruned_seqs else (True, "")
    try:
        through = int(retention.get("pruned_through_seq") or 0)
        head = int(head_seq or 0)
    except (TypeError, ValueError):
        return False, "malformed_retention"
    if through <= 0:
        return (False, "unsigned_pruned") if pruned_seqs else (True, "")
    if through > head:
        return False, "retention_ahead_of_head"
    if pruned_seqs != set(range(1, through + 1)):
        return False, "retention_mismatch"
    boundary_row = next(
        (r for r in records if int(r.get("seq") or 0) == through), None,
    )
    if boundary_row is None or boundary_row.get("pruned") is not True:
        return False, "retention_missing"
    if str(boundary_row.get("entry_hash") or "") != str(
        retention.get("boundary_hash") or ""
    ):
        return False, "retention_mismatch"
    if str(boundary_row.get("prev_hash") or "") != str(
        retention.get("boundary_prev_hash") or ""
    ):
        return False, "retention_mismatch"
    return True, ""



def next_entry(
    prev_hash: str,
    seq: int,
    payload: dict[str, Any],
    *,
    secret: bytes | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    body = {key: value for key, value in payload.items() if key not in _CHAIN_FIELDS}
    body["seq"] = int(seq)
    # Always sign an explicit key id so verification of a stored row (which
    # carries key_id) hashes exactly the same body.
    body["key_id"] = str(key_id or body.get("key_id") or verdict_key_id())
    signing_key = secret
    if signing_key is None:
        signing_key = _signing_key(body["key_id"])
    if signing_key is None:
        raise ValueError(f"unknown verdict key id: {body['key_id']}")
    signature = sign_payload(body, secret=signing_key)
    material = f"{prev_hash}\n{int(seq)}\n{canonical_payload(body)}\n{signature}"
    entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return {
        **body,
        "prev_hash": str(prev_hash or ""),
        "sig": signature,
        "entry_hash": entry_hash,
        "key_id": str(body.get("key_id") or KEY_ID),
    }


def verify_records(
    records: Iterable[dict[str, Any]],
    *,
    secret: bytes | None = None,
    head_hash: str | None = None,
    head_seq: int | None = None,
    start_seq: int = 0,
    start_prev_hash: str = "",
) -> tuple[bool, str]:
    entries = list(records)
    prev_hash = str(start_prev_hash or "")
    prev_seq = int(start_seq or 0)
    for record in entries:
        if not isinstance(record, dict):
            return False, "malformed"
        record_id = str(record.get("id") or f"seq:{record.get('seq')}")
        try:
            seq = int(record.get("seq"))
        except (TypeError, ValueError):
            return False, record_id
        if seq != prev_seq + 1:
            return False, record_id
        if str(record.get("prev_hash") or "") != prev_hash:
            return False, record_id
        if record.get("pruned") is True:
            # P2 retention: the signed payload is gone; only the hash-only
            # tombstone remains.  Linkage is checked here and the hash is
            # anchored by the next row's prev_hash and the signed Merkle
            # checkpoints.
            entry_hash = str(record.get("entry_hash") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", entry_hash):
                return False, record_id
            prev_hash = entry_hash
            prev_seq = seq
            continue
        kid = str(record.get("key_id") or "").strip()
        if secret is not None:
            # Explicit test/legacy secret wins (single-key callers).
            row_key = secret
        else:
            row_key = _secret_for_kid(kid) if kid else None
        if row_key is None:
            return False, f"key:{kid or 'unknown'}"
        expected = next_entry(
            prev_hash, seq, record, secret=row_key, key_id=kid or None,
        )
        if not hmac.compare_digest(
            expected["sig"].encode("utf-8"),
            str(record.get("sig") or "").encode("utf-8"),
        ):
            return False, record_id
        if not hmac.compare_digest(
            expected["entry_hash"].encode("utf-8"),
            str(record.get("entry_hash") or "").encode("utf-8"),
        ):
            return False, record_id
        prev_hash = str(record["entry_hash"])
        prev_seq = seq
    if head_seq is not None or head_hash is not None:
        expected_seq = int(head_seq or 0)
        if not entries:
            if expected_seq > prev_seq:
                return False, "missing"
            if (
                expected_seq == prev_seq
                and prev_hash
                and str(head_hash or "") != prev_hash
            ):
                return False, "missing"
            return True, ""
        last = entries[-1]
        if int(last.get("seq") or 0) != expected_seq:
            return False, str(last.get("id") or "")
        if str(last.get("entry_hash") or "") != str(head_hash or ""):
            return False, str(last.get("id") or "")
    return True, ""


def merkle_root(hashes: Iterable[str]) -> str:
    """Binary Merkle root over entry hashes; odd nodes duplicate the last."""
    nodes = [str(h) for h in hashes]
    if not nodes:
        return hashlib.sha256(b"").hexdigest()
    while len(nodes) > 1:
        if len(nodes) % 2:
            nodes.append(nodes[-1])
        nodes = [
            hashlib.sha256((nodes[i] + nodes[i + 1]).encode("utf-8")).hexdigest()
            for i in range(0, len(nodes), 2)
        ]
    return nodes[0]


def checkpoint_anchor_dir() -> pathlib.Path:
    override = os.environ.get("TMA_CHECKPOINT_DIR")
    if override:
        return pathlib.Path(override)
    # backend/app/services/verdict_log_service.py -> backend/
    return pathlib.Path(__file__).resolve().parents[2] / "tma_checkpoints"


def checkpoint_anchor_path(tenant_id: str) -> pathlib.Path:
    digest = hashlib.sha256(str(tenant_id or "").encode("utf-8")).hexdigest()[:16]
    return checkpoint_anchor_dir() / f"{digest}.json"


def head_witness_path(tenant_id: str) -> pathlib.Path:
    digest = hashlib.sha256(str(tenant_id or "").encode("utf-8")).hexdigest()[:16]
    return checkpoint_anchor_dir() / f"{digest}.head.json"


def witness_copy_dir() -> pathlib.Path | None:
    """Optional second medium for anchor/head copies (P1d external witness)."""
    raw = str(
        os.environ.get("TMA_WITNESS_DIR")
        or get_config().security.get("tma_witness_dir", "")
        or ""
    ).strip()
    if not raw:
        return None
    root = pathlib.Path(raw).resolve()
    if root == checkpoint_anchor_dir().resolve():
        logger.error(
            "TMA_WITNESS_DIR must differ from TMA_CHECKPOINT_DIR; "
            "external witness is disabled",
        )
        return None
    return root


def _witness_copy_path(kind: str, tenant_id: str) -> pathlib.Path | None:
    root = witness_copy_dir()
    if root is None:
        return None
    digest = hashlib.sha256(str(tenant_id or "").encode("utf-8")).hexdigest()[:16]
    suffix = "head.json" if kind == "head" else "anchor.json"
    return root / f"{digest}.{suffix}"


def _read_json(path: pathlib.Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_anchor_copy(tenant_id: str) -> dict[str, Any] | None:
    return _read_json(_witness_copy_path("anchor", tenant_id))


def read_head_witness_copy(tenant_id: str) -> dict[str, Any] | None:
    return _read_json(_witness_copy_path("head", tenant_id))


_TENANT_LOCKS: dict[str, threading.Lock] = {}
_TENANT_LOCKS_GUARD = threading.Lock()


def _tenant_lock(tenant_id: str) -> threading.Lock:
    key = str(tenant_id or "")
    with _TENANT_LOCKS_GUARD:
        return _TENANT_LOCKS.setdefault(key, threading.Lock())


def _atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tma-checkpoint-", suffix=".tmp",
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_anchor(checkpoint: dict[str, Any]) -> pathlib.Path:
    """Atomically write a 0600 anchor chain outside the DB."""
    tenant = str(checkpoint.get("tenant_id") or "")
    path = checkpoint_anchor_path(tenant)
    existing = read_anchor(tenant) or {}
    chain = existing.get("chain")
    anchor_chain = [c for c in chain if isinstance(c, dict)] if isinstance(chain, list) else []
    if not anchor_chain and existing:
        anchor_chain = [existing]
    seq = int(checkpoint.get("seq") or 0)
    anchor_chain = [
        c for c in anchor_chain if int(c.get("seq") or 0) != seq
    ]
    anchor_chain.append(checkpoint)
    anchor_chain.sort(key=lambda c: int(c.get("seq") or 0))
    latest = anchor_chain[-1] if anchor_chain else checkpoint
    payload = {**latest, "chain": anchor_chain}
    _atomic_write_json(path, payload)
    return path


def write_anchor_chain(
    tenant_id: str, checkpoints: list[dict[str, Any]],
) -> pathlib.Path:
    chain = [c for c in checkpoints if isinstance(c, dict)]
    chain.sort(key=lambda c: int(c.get("seq") or 0))
    if not chain:
        raise ValueError("empty checkpoint chain")
    latest = chain[-1]
    witness_required = witness_copy_dir() is not None
    payload = {
        **latest, "chain": chain, "witness_required": witness_required,
    }
    path = checkpoint_anchor_path(tenant_id)
    _atomic_write_json(path, payload)
    copy_path = _witness_copy_path("anchor", tenant_id)
    if copy_path is not None:
        _atomic_write_json(copy_path, payload)
    return path


def read_anchor(tenant_id: str) -> dict[str, Any] | None:
    path = checkpoint_anchor_path(tenant_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def build_head_witness(
    tenant_id: str,
    seq: int,
    entry_hash: str,
    *,
    key_id: str | None = None,
    secret: bytes | None = None,
    created_at_epoch: int | None = None,
    retention_seq: int | None = None,
) -> dict[str, Any]:
    tenant = str(tenant_id or "")
    kid = str(key_id or verdict_key_id())
    body = {
        "tenant_id": tenant,
        "seq": int(seq),
        "entry_hash": str(entry_hash or ""),
        "key_id": kid,
        "created_at_epoch": int(created_at_epoch if created_at_epoch is not None else time.time()),
    }
    # Positive marker only: witnesses written before P2 keep their old body and
    # signature.  Binding the signed retention boundary here means a surgical
    # DB restore (older boundary + authentic live rows) is detected even when
    # no tombstone survives.
    if retention_seq is not None and int(retention_seq) > 0:
        body["retention_seq"] = int(retention_seq)
    signing_key = secret if secret is not None else _secret_for_kid(kid)
    if signing_key is None:
        raise ValueError(f"unknown verdict key id: {kid}")
    body["sig"] = sign_payload(body, secret=signing_key)
    return body


def write_head_witness(
    tenant_id: str,
    seq: int,
    entry_hash: str,
    *,
    key_id: str | None = None,
    retention_seq: int | None = None,
) -> pathlib.Path:
    with _tenant_lock(tenant_id):
        return _write_head_witness_locked(
            tenant_id, seq, entry_hash, key_id=key_id,
            retention_seq=retention_seq,
        )


def _sync_head_copy(tenant: str, witness: dict[str, Any]) -> None:
    copy_path = _witness_copy_path("head", tenant)
    if copy_path is None:
        return
    if read_head_witness_copy(tenant) == witness:
        return
    _atomic_write_json(copy_path, witness)


def _write_head_witness_locked(
    tenant_id: str,
    seq: int,
    entry_hash: str,
    *,
    key_id: str | None = None,
    retention_seq: int | None = None,
) -> pathlib.Path:
    tenant = str(tenant_id or "")
    path = head_witness_path(tenant)
    existing = read_head_witness(tenant)
    effective_retention = retention_seq
    if effective_retention is None and existing is not None:
        # Rebuild/recovery must not drop an already-bound boundary.
        try:
            effective_retention = int(existing.get("retention_seq") or 0) or None
        except (TypeError, ValueError):
            effective_retention = None
    if existing is not None:
        try:
            existing_seq = int(existing.get("seq") or 0)
        except (TypeError, ValueError):
            existing_seq = 0
        if existing_seq > int(seq):
            # A concurrent append already witnessed a newer head; never
            # downgrade the witness (that would look like a truncation).
            _sync_head_copy(tenant, existing)
            return path
        if existing_seq == int(seq):
            if str(existing.get("entry_hash") or "") == str(entry_hash or ""):
                try:
                    existing_retention = int(existing.get("retention_seq") or 0)
                except (TypeError, ValueError):
                    existing_retention = 0
                if not effective_retention or existing_retention == int(effective_retention):
                    _sync_head_copy(tenant, existing)
                    return path
                # Same head, newly bound retention: re-sign to add the marker.
                witness = build_head_witness(
                    tenant, seq, entry_hash, key_id=key_id,
                    retention_seq=effective_retention,
                )
                _atomic_write_json(path, witness)
                _sync_head_copy(tenant, witness)
                return path
            logger.warning(
                "head witness seq=%s already exists with a different hash; "
                "keeping the first witness", seq,
            )
            _sync_head_copy(tenant, existing)
            return path
    witness = build_head_witness(
        tenant, seq, entry_hash, key_id=key_id,
        retention_seq=effective_retention,
    )
    _atomic_write_json(path, witness)
    copy_path = _witness_copy_path("head", tenant)
    if copy_path is not None:
        _atomic_write_json(copy_path, witness)
    return path


def read_head_witness(tenant_id: str) -> dict[str, Any] | None:
    path = head_witness_path(tenant_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def verify_head_witness(witness: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(witness, dict):
        return False, "malformed_head_witness"
    kid = str(witness.get("key_id") or "")
    signing_key = _secret_for_kid(kid)
    if signing_key is None:
        return False, f"key:{kid or 'unknown'}"
    body = {key: value for key, value in witness.items() if key != "sig"}
    expected = sign_payload(body, secret=signing_key)
    if not hmac.compare_digest(
        expected.encode("utf-8"), str(witness.get("sig") or "").encode("utf-8"),
    ):
        return False, "head_witness_sig"
    return True, ""


def build_checkpoint(
    tenant_id: str,
    seq: int,
    root: str,
    last_hash: str,
    *,
    key_id: str | None = None,
    secret: bytes | None = None,
    created_at_epoch: int | None = None,
    witness_required: bool | None = None,
) -> dict[str, Any]:
    kid = str(key_id or verdict_key_id())
    body = {
        "tenant_id": str(tenant_id or ""),
        "seq": int(seq),
        "merkle_root": str(root or ""),
        "last_hash": str(last_hash or ""),
        "key_id": kid,
        "created_at_epoch": int(created_at_epoch if created_at_epoch is not None else time.time()),
    }
    # Only sign the positive marker: checkpoints written before P1d (and
    # witness-disabled checkpoints) keep the legacy body/signature.
    if witness_required:
        body["witness_required"] = True
    signing_key = secret if secret is not None else _secret_for_kid(kid)
    if signing_key is None:
        raise ValueError(f"unknown verdict key id: {kid}")
    body["sig"] = sign_payload(body, secret=signing_key)
    return body


def verify_checkpoint(
    checkpoint: dict[str, Any],
    *,
    secret: bytes | None = None,
) -> tuple[bool, str]:
    if not isinstance(checkpoint, dict):
        return False, "malformed_checkpoint"
    kid = str(checkpoint.get("key_id") or "")
    signing_key = secret if secret is not None else _secret_for_kid(kid)
    if signing_key is None:
        return False, f"key:{kid or 'unknown'}"
    body = {
        key: value for key, value in checkpoint.items()
        if key not in ("sig", "chain", "anchor_written", "witness_written")
    }
    expected = sign_payload(body, secret=signing_key)
    if hmac.compare_digest(
        expected.encode("utf-8"), str(checkpoint.get("sig") or "").encode("utf-8"),
    ):
        return True, ""
    # Round-3 briefly signed an explicit `witness_required: false`; a false
    # marker is semantically legacy, so accept the body without it too.
    if checkpoint.get("witness_required") is False:
        legacy_body = {
            key: value for key, value in body.items()
            if key != "witness_required"
        }
        legacy_expected = sign_payload(legacy_body, secret=signing_key)
        if hmac.compare_digest(
            legacy_expected.encode("utf-8"),
            str(checkpoint.get("sig") or "").encode("utf-8"),
        ):
            return True, ""
    return False, f"checkpoint:{checkpoint.get('seq')}"


def verify_checkpoint_prefix(
    records: list[dict[str, Any]],
    checkpoint: dict[str, Any],
    *,
    anchor: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    ok, reason = verify_checkpoint(checkpoint)
    if not ok:
        return False, reason
    try:
        seq = int(checkpoint.get("seq") or 0)
    except (TypeError, ValueError):
        return False, "malformed_checkpoint"
    if seq <= 0:
        return False, "malformed_checkpoint"
    if len(records) < seq:
        return False, "truncated_before_checkpoint"
    prefix = records[:seq]
    if str(prefix[-1].get("entry_hash") or "") != str(checkpoint.get("last_hash") or ""):
        return False, f"checkpoint:{seq}"
    if merkle_root(str(r.get("entry_hash") or "") for r in prefix) != str(
        checkpoint.get("merkle_root") or ""
    ):
        return False, f"checkpoint:{seq}"
    if anchor is not None:
        if isinstance(anchor.get("chain"), list) and anchor["chain"]:
            anchor = anchor["chain"][-1]
        anchor_ok, anchor_reason = verify_checkpoint(anchor)
        if not anchor_ok:
            return False, f"anchor:{anchor_reason}"
        if int(anchor.get("seq") or 0) < seq:
            return False, "anchor_stale"
        if int(anchor.get("seq") or 0) > seq:
            return False, "checkpoint_missing"
        for field in (
            "tenant_id", "seq", "merkle_root", "last_hash", "key_id", "sig",
        ):
            if anchor.get(field) != checkpoint.get(field):
                return False, "anchor_mismatch"
    return True, ""


def verify_checkpoint_chain(
    records: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    anchor: dict[str, Any],
) -> tuple[bool, str]:
    chain = anchor.get("chain")
    anchor_chain = [c for c in chain if isinstance(c, dict)] if isinstance(chain, list) else []
    if not anchor_chain:
        anchor_chain = [anchor]
    anchor_by_seq: dict[int, dict[str, Any]] = {}
    for entry in anchor_chain:
        ok, reason = verify_checkpoint(entry)
        if not ok:
            return False, f"anchor:{reason}"
        seq = int(entry.get("seq") or 0)
        if seq <= 0 or seq in anchor_by_seq:
            return False, "anchor_mismatch"
        anchor_by_seq[seq] = entry
    db_seqs = {int(cp.get("seq") or 0) for cp in checkpoints}
    if set(anchor_by_seq) != db_seqs:
        return False, "checkpoint_missing"
    for checkpoint in checkpoints:
        ok, reason = verify_checkpoint_prefix(records, checkpoint)
        if not ok:
            return ok, reason
        seq = int(checkpoint.get("seq") or 0)
        anchored = anchor_by_seq.get(seq)
        if anchored is None:
            return False, "checkpoint_missing"
        for field in (
            "tenant_id", "seq", "merkle_root", "last_hash", "key_id", "sig",
        ):
            if anchored.get(field) != checkpoint.get(field):
                return False, "anchor_mismatch"
    return True, ""


def verify_db_snapshot(
    records: list[dict[str, Any]],
    *,
    head_hash: str,
    head_seq: int,
    checkpoints: list[dict[str, Any]],
    retention: dict[str, Any] | None = None,
    require_nonempty: bool = False,
) -> tuple[bool, str]:
    """Chain/checkpoint integrity without requiring external copies.

    Used only by the rebuild CLI after an operator has decided to restore the
    external medium; it never treats a broken chain as repairable.
    """
    ok, reason, start_seq, start_prev = _retention_start(retention)
    if not ok:
        return ok, reason
    if require_nonempty and not records and head_seq == 0:
        return False, "missing"
    ok, broken = verify_records(
        records,
        head_hash=head_hash,
        head_seq=head_seq,
        start_seq=start_seq,
        start_prev_hash=start_prev,
    )
    if not ok:
        return ok, broken
    ok, reason = _check_retention_consistency(retention, records, head_seq)
    if not ok:
        return ok, reason
    for checkpoint in checkpoints:
        ok, reason = verify_checkpoint_prefix(records, checkpoint)
        if not ok:
            return ok, reason
    return True, ""


def verify_snapshot(
    records: list[dict[str, Any]],
    *,
    head_hash: str,
    head_seq: int,
    checkpoints: list[dict[str, Any]],
    anchor: dict[str, Any] | None,
    head_witness: dict[str, Any] | None,
    anchor_copy: dict[str, Any] | None = None,
    head_witness_copy: dict[str, Any] | None = None,
    witness_copy_required: bool = False,
    retention: dict[str, Any] | None = None,
    require_nonempty: bool = False,
) -> tuple[bool, str]:
    """Pure verification of a loaded chain snapshot (DB + external witnesses)."""
    ok, reason, start_seq, start_prev = _retention_start(retention)
    if not ok:
        return ok, reason
    if witness_copy_required:
        if anchor is not None:
            if anchor_copy is None:
                return False, "witness_copy_missing"
            if anchor_copy != anchor:
                return False, "witness_copy_mismatch"
        elif anchor_copy is not None:
            return False, "witness_primary_missing"
        if head_witness is not None:
            if head_witness_copy is None:
                return False, "witness_copy_missing"
            if head_witness_copy != head_witness:
                return False, "witness_copy_mismatch"
        elif head_witness_copy is not None:
            return False, "witness_primary_missing"
    if not records and head_seq == 0:
        if head_witness is not None:
            return False, "truncated_after_checkpoint"
        if anchor is not None:
            return False, "checkpoint_missing"
        if require_nonempty:
            return False, "missing"
    ok, broken = verify_records(
        records,
        head_hash=head_hash,
        head_seq=head_seq,
        start_seq=start_seq,
        start_prev_hash=start_prev,
    )
    if not ok:
        return ok, broken
    ok, reason = _check_retention_consistency(retention, records, head_seq)
    if not ok:
        return ok, reason
    if head_witness is not None:
        ok, reason = verify_head_witness(head_witness)
        if not ok:
            return ok, reason
        try:
            witness_seq = int(head_witness.get("seq") or 0)
        except (TypeError, ValueError):
            return False, "malformed_head_witness"
        if witness_seq > head_seq:
            return False, "truncated_after_checkpoint"
        if witness_seq < head_seq:
            return False, "head_witness_stale"
        if str(head_witness.get("entry_hash") or "") != str(head_hash or ""):
            return False, "head_witness_mismatch"
        try:
            anchored_retention = int(head_witness.get("retention_seq") or 0)
        except (TypeError, ValueError):
            return False, "malformed_head_witness"
        if retention is not None:
            try:
                expected_retention = int(
                    retention.get("pruned_through_seq") or 0
                )
            except (TypeError, ValueError):
                return False, "malformed_retention"
            if anchored_retention != expected_retention:
                return False, "retention_unanchored"
        elif anchored_retention > 0:
            # A surgical restore that removed the retention row (and the
            # tombstones) still leaves the signed marker on the witness.
            return False, "retention_missing"
    elif checkpoints:
        return False, "head_witness_missing"
    if checkpoints:
        if anchor is None:
            return False, "anchor_missing"
        return verify_checkpoint_chain(records, checkpoints, anchor)
    if anchor is not None:
        return False, "checkpoint_missing"
    return True, ""


def _checkpoint_skip(tenant: str, reason: str) -> None:
    logger.warning("checkpoint skipped tenant=%s: %s", tenant, reason)
    return None


async def write_checkpoint(
    db: Any,
    tenant_id: str,
    *,
    seq: int | None = None,
) -> dict[str, Any] | None:
    """Build/sign/store a checkpoint + anchor for a tenant prefix."""
    from sqlalchemy import text

    tenant = str(tenant_id or "")
    if db is None:
        from app.db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            return await write_checkpoint(session, tenant, seq=seq)

    head = (await db.execute(
        text(
            "SELECT last_hash, last_seq FROM memory_verdict_heads "
            "WHERE tenant_id = :tenant"
        ),
        {"tenant": tenant},
    )).fetchone()
    head_hash = str(head[0] or "") if head else ""
    head_seq = int(head[1] or 0) if head else 0
    target = int(seq if seq is not None else head_seq)
    if target <= 0 or target > head_seq:
        return _checkpoint_skip(tenant, f"target={target} head={head_seq}")
    try:
        result = await db.execute(
            text(
                "SELECT seq, entry_hash FROM memory_verdicts "
                "WHERE tenant_id = :tenant AND seq <= :target "
                "UNION ALL "
                "SELECT seq, entry_hash FROM memory_verdict_redacted "
                "WHERE tenant_id = :tenant AND seq <= :target "
                "ORDER BY seq ASC"
            ),
            {"tenant": tenant, "target": target},
        )
        rows = result.fetchall()
    except Exception:
        # Pre-migration replica without the tombstone table: fail closed (a
        # SELECT after a failed statement would raise PendingRollbackError).
        logger.warning("tombstone table unavailable for checkpoint", exc_info=True)
        return _checkpoint_skip(tenant, "tombstone table unavailable")
    if len(rows) != target:
        return _checkpoint_skip(
            tenant, f"missing prefix rows ({len(rows)}/{target})",
        )
    hashes: list[str] = []
    for expected_seq, row in enumerate(rows, start=1):
        if int(row[0] or 0) != expected_seq:
            return _checkpoint_skip(
                tenant, f"non-contiguous seq at {expected_seq}",
            )
        hashes.append(str(row[1] or ""))
    if hashes[-1] != head_hash and target == head_seq:
        return _checkpoint_skip(tenant, "head hash changed during checkpoint")
    existing = (await db.execute(
        text(
            "SELECT tenant_id, seq, merkle_root, last_hash, sig, key_id, "
            "witness_required, created_at_epoch FROM memory_verdict_checkpoints "
            "WHERE tenant_id = :tenant AND seq = :target"
        ),
        {"tenant": tenant, "target": target},
    )).fetchone()
    if existing:
        # Idempotent: reuse the stored signature/epoch instead of re-signing.
        checkpoint = {
            "tenant_id": existing[0],
            "seq": int(existing[1] or 0),
            "merkle_root": existing[2],
            "last_hash": existing[3],
            "sig": existing[4],
            "key_id": existing[5],
            "created_at_epoch": int(existing[7] or 0),
        }
        checkpoint["witness_required"] = bool(existing[6])
    else:
        witness_required = witness_copy_dir() is not None
        checkpoint = build_checkpoint(
            tenant, target, merkle_root(hashes), hashes[-1],
            witness_required=witness_required,
        )
        # The DB row always carries the flag; a false flag is not part of the
        # signature (legacy body), so verification has a false-body fallback.
        checkpoint["witness_required"] = witness_required
        await db.execute(
            text(
                """
                INSERT INTO memory_verdict_checkpoints
                    (tenant_id, seq, merkle_root, last_hash, sig, key_id,
                     witness_required, created_at_epoch, created_at)
                VALUES
                    (:tenant_id, :seq, :merkle_root, :last_hash, :sig, :key_id,
                     :witness_required, :created_at_epoch, NOW())
                ON CONFLICT (tenant_id, seq) DO NOTHING
                """
            ),
            checkpoint,
        )
    await db.commit()
    # Anchor chain: every DB checkpoint must have a matching anchor entry, so
    # deletion of an older checkpoint row is detectable too.
    try:
        rows = (await db.execute(
            text(
                "SELECT tenant_id, seq, merkle_root, last_hash, sig, key_id, "
                "witness_required, created_at_epoch FROM memory_verdict_checkpoints "
                "WHERE tenant_id = :tenant ORDER BY seq ASC"
            ),
            {"tenant": tenant},
        )).fetchall()
        chain = []
        for row in rows:
            entry = {
                "tenant_id": row[0],
                "seq": int(row[1] or 0),
                "merkle_root": row[2],
                "last_hash": row[3],
                "sig": row[4],
                "key_id": row[5],
                "created_at_epoch": int(row[7] or 0),
            }
            entry["witness_required"] = bool(row[6])
            chain.append(entry)
        existing_anchor = read_anchor(tenant)
        existing_chain = (
            existing_anchor.get("chain")
            if isinstance(existing_anchor, dict) else None
        )
        anchored = {
            int(c.get("seq") or 0)
            for c in (existing_chain or [])
            if isinstance(c, dict)
        }
        if not anchored and isinstance(existing_anchor, dict):
            try:
                anchor_seq = int(existing_anchor.get("seq") or 0)
            except (TypeError, ValueError):
                anchor_seq = 0
            if anchor_seq > 0:
                anchored = {anchor_seq}
            else:
                logger.warning(
                    "ignoring corrupt/seq-less anchor for tenant=%s", tenant,
                )
        db_seqs = {c["seq"] for c in chain}
        missing = anchored - db_seqs
        if missing:
            # Never let a later checkpoint "heal" a deleted older checkpoint
            # by rewriting the anchor from the doctored DB.
            logger.error(
                "refusing to shrink checkpoint anchor: DB is missing seqs %s",
                sorted(missing),
            )
            checkpoint["anchor_written"] = False
            return checkpoint
        write_anchor_chain(tenant, chain)
        checkpoint["anchor_written"] = True
        try:
            write_head_witness(tenant, head_seq, head_hash)
            checkpoint["witness_written"] = True
        except Exception:
            logger.warning(
                "head witness write failed after anchor", exc_info=True,
            )
            checkpoint["witness_written"] = False
    except Exception:
        logger.warning("checkpoint anchor write failed", exc_info=True)
        checkpoint["anchor_written"] = False
    return checkpoint


async def append_verdict(
    *,
    tenant_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    action_fingerprint: str = "",
    tool_name: str = "",
    decision: str = "",
    reason: str = "",
    taint: str = "untrusted",
    corroborators: list[str] | None = None,
    mode: str = "shadow",
    blocked: bool = False,
) -> bool:
    """Append one verdict in its own short session; failure is non-blocking."""
    tenant = str(tenant_id or "")
    record_id = str(uuid.uuid4())
    payload = build_verdict_payload(
        tenant_id=tenant,
        user_id=user_id,
        conversation_id=conversation_id,
        action_fingerprint=action_fingerprint,
        tool_name=tool_name,
        decision=decision,
        reason=reason,
        taint=taint,
        corroborators=corroborators,
        mode=mode,
        blocked=blocked,
        record_id=record_id,
        redact=bool(get_config().verdict_reason_redaction),
    )
    try:
        from sqlalchemy import text
        from app.db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            # Serialise the head row so concurrent appends cannot fork.
            await db.execute(
                text(
                    "INSERT INTO memory_verdict_heads "
                    "(tenant_id, last_hash, last_seq, updated_at) "
                    "VALUES (:tenant, '', 0, NOW()) "
                    "ON CONFLICT (tenant_id) DO NOTHING"
                ),
                {"tenant": tenant},
            )
            row = (await db.execute(
                text(
                    "SELECT last_hash, last_seq FROM memory_verdict_heads "
                    "WHERE tenant_id = :tenant FOR UPDATE"
                ),
                {"tenant": tenant},
            )).fetchone()
            prev_hash = str(row[0] or "") if row else ""
            seq = (int(row[1]) if row else 0) + 1
            entry = next_entry(prev_hash, seq, payload)
            await db.execute(
                text(
                    """
                    INSERT INTO memory_verdicts
                        (id, tenant_id, seq, user_id, conversation_id,
                         action_fingerprint, tool_name, decision, reason, taint,
                         corroborators, mode, blocked, created_at_epoch,
                         prev_hash, entry_hash, sig, key_id, created_at)
                    VALUES
                        (:id, :tenant_id, :seq, :user_id, :conversation_id,
                         :action_fingerprint, :tool_name, :decision, :reason, :taint,
                         CAST(:corroborators AS JSONB), :mode, :blocked,
                         :created_at_epoch, :prev_hash, :entry_hash,
                         :sig, :key_id, NOW())
                    """
                ),
                {
                    **entry,
                    "corroborators": json.dumps(entry["corroborators"], ensure_ascii=False),
                },
            )
            await db.execute(
                text(
                    "UPDATE memory_verdict_heads SET last_hash = :hash, "
                    "last_seq = :seq, updated_at = NOW() WHERE tenant_id = :tenant"
                ),
                {"hash": entry["entry_hash"], "seq": seq, "tenant": tenant},
            )
            await db.commit()
        try:
            await asyncio.to_thread(
                write_head_witness, tenant, seq, entry["entry_hash"],
            )
        except Exception:
            logger.warning(
                "head witness write failed (non-blocking)", exc_info=True,
            )
        interval = int(get_config().tma_checkpoint_interval or 0)
        if interval and seq % interval == 0:
            try:
                await write_checkpoint(None, tenant, seq=seq)
            except Exception:
                logger.warning(
                    "automatic verdict checkpoint failed (non-blocking)",
                    exc_info=True,
                )
        return True
    except Exception:
        logger.warning("memory verdict append failed (non-blocking)", exc_info=True)
        return False


async def verify_chain(
    db: Any,
    tenant_id: str = "",
    *,
    require_nonempty: bool = False,
    allow_missing_witness: bool = False,
) -> tuple[bool, str]:
    """Verify one tenant's chain in seq order; returns (ok, broken_record_id).

    ``require_nonempty`` makes a tenant with no head row and no records report
    ``broken="missing"`` instead of a vacuous OK (the audit CLI uses it).
    """
    from sqlalchemy import text

    tenant = str(tenant_id or "")
    head = (await db.execute(
        text(
            "SELECT last_hash, last_seq FROM memory_verdict_heads "
            "WHERE tenant_id = :tenant"
        ),
        {"tenant": tenant},
    )).fetchone()
    head_hash = str(head[0] or "") if head else ""
    head_seq = int(head[1] or 0) if head else 0

    result = await db.execute(
        text(
            "SELECT id, tenant_id, seq, user_id, conversation_id, "
            "action_fingerprint, tool_name, decision, reason, taint, "
            "corroborators, mode, blocked, created_at_epoch, prev_hash, "
            "entry_hash, sig, key_id FROM memory_verdicts "
            "WHERE tenant_id = :tenant ORDER BY seq ASC"
        ),
        {"tenant": tenant},
    )
    records = []
    # P2 retention: the signed prefix boundary (missing table on a
    # pre-migration replica is a no-retention legacy chain).
    retention = None
    try:
        retention_row = (await db.execute(
            text(
                "SELECT tenant_id, pruned_through_seq, boundary_hash, "
                "boundary_prev_hash, key_id, updated_at_epoch, sig "
                "FROM memory_verdict_retention WHERE tenant_id = :tenant"
            ),
            {"tenant": tenant},
        )).fetchone()
        if retention_row is not None:
            retention = {
                "tenant_id": str(retention_row[0] or ""),
                "pruned_through_seq": int(retention_row[1] or 0),
                "boundary_hash": str(retention_row[2] or ""),
                "boundary_prev_hash": str(retention_row[3] or ""),
                "key_id": str(retention_row[4] or ""),
                "updated_at_epoch": int(retention_row[5] or 0),
                "sig": str(retention_row[6] or ""),
            }
    except Exception:
        logger.warning("retention table unavailable", exc_info=True)
        retention = None
    pruned_records: list[dict[str, Any]] = []
    try:
        pruned_rows = (await db.execute(
            text(
                "SELECT seq, prev_hash, entry_hash, key_id "
                "FROM memory_verdict_redacted WHERE tenant_id = :tenant "
                "ORDER BY seq ASC"
            ),
            {"tenant": tenant},
        )).fetchall()
        pruned_records = [
            {
                "seq": int(row[0] or 0),
                "prev_hash": str(row[1] or ""),
                "entry_hash": str(row[2] or ""),
                "key_id": str(row[3] or ""),
                "pruned": True,
            }
            for row in pruned_rows
        ]
    except Exception:
        logger.warning("verdict tombstone table unavailable", exc_info=True)
        pruned_records = []

    for row in result.fetchall():
        corroborators = row[10]
        if isinstance(corroborators, str):
            try:
                corroborators = json.loads(corroborators)
            except ValueError:
                corroborators = []
        if not isinstance(corroborators, list):
            corroborators = []
        records.append({
            "id": row[0],
            "tenant_id": row[1],
            "seq": row[2],
            "user_id": row[3],
            "conversation_id": row[4],
            "action_fingerprint": row[5],
            "tool_name": row[6],
            "decision": row[7],
            "reason": row[8],
            "taint": row[9],
            "corroborators": corroborators,
            "mode": row[11],
            "blocked": bool(row[12]),
            "created_at_epoch": int(row[13] or 0),
            "prev_hash": row[14],
            "entry_hash": row[15],
            "sig": row[16],
            "key_id": row[17],
        })
    records = sorted(pruned_records + records, key=lambda r: int(r.get("seq") or 0))
    try:
        checkpoint_rows = (await db.execute(
            text(
                "SELECT tenant_id, seq, merkle_root, last_hash, sig, key_id, "
                "witness_required, created_at_epoch FROM memory_verdict_checkpoints "
                "WHERE tenant_id = :tenant ORDER BY seq ASC"
            ),
            {"tenant": tenant},
        )).fetchall()
    except Exception:
        # Pre-migration replica / dropped table: verify the P1a chain but
        # never let a DB error escape as a traceback.
        logger.warning("checkpoint table unavailable", exc_info=True)
        checkpoint_rows = []
    checkpoints = []
    for row in checkpoint_rows:
        entry = {
            "tenant_id": row[0],
            "seq": int(row[1] or 0),
            "merkle_root": row[2],
            "last_hash": row[3],
            "sig": row[4],
            "key_id": row[5],
            "created_at_epoch": int(row[7] or 0),
        }
        entry["witness_required"] = bool(row[6])
        checkpoints.append(entry)
    if allow_missing_witness:
        return verify_db_snapshot(
            records, head_hash=head_hash, head_seq=head_seq,
            checkpoints=checkpoints, retention=retention,
            require_nonempty=require_nonempty,
        )
    anchor = read_anchor(tenant)
    witness_required = (
        witness_copy_dir() is not None
        or bool(anchor and anchor.get("witness_required"))
        or any(bool(cp.get("witness_required")) for cp in checkpoints)
    )
    return verify_snapshot(
        records,
        head_hash=head_hash,
        head_seq=head_seq,
        checkpoints=checkpoints,
        anchor=anchor,
        head_witness=read_head_witness(tenant),
        anchor_copy=read_anchor_copy(tenant),
        head_witness_copy=read_head_witness_copy(tenant),
        witness_copy_required=witness_required,
        retention=retention,
        require_nonempty=require_nonempty,
    )
