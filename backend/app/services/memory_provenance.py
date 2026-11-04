# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P0 TMA-NM: origin-bound sidecar signatures for file memory (advisory).

This is the P0 layer of `docs/PLAN_TMA_NM_SOTA_20261021.md`: an HMAC over
``(tenant, user, target, origin, content_hash)`` plus per-entry authority
metadata.  It detects external edits, prevents sidecar reuse across
tenants/users/targets, and classifies legacy entries as untrusted.

It is **not** full TMA-NM / IFC: there is no taint propagation across
summaries/tool echoes (P1), no deterministic act gate (P1), and no append-only
verdict hash chain (P1).  Readers must keep treating the metadata as advisory.

Sidecar v2:
    {"version": 2,
     "entries": {"<sha256(content)>": {"sig": "...", "origin": "agent",
                                       "tenant": "acme"}}}

Legacy v1 (``{"<hash>": "<sig>"}``) is read for damage classification only; its
signature cannot prove an origin, so it is never marked verified.

Signing is explicit: ``sync_entries(origins=None)`` signs nothing.  A partial
remove records a hash tombstone so a later ``add`` of the exact same text is
not minted as agent authority.  Strict-mode consumers (memory tool, identity
loader, subconscious ingestion) use ``verified_entries``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.core.config import get_config

logger = logging.getLogger(__name__)

SIDECAR_VERSION = 2
_MSG_DOMAIN = "memory-provenance-v2"
_ORIGINS = (
    "user",
    "trusted_tool",
    "agent",
    "untrusted_external",
    "legacy_untrusted",
)
ORIGIN_ACT_CLASS = {
    "user": "act",
    "trusted_tool": "act",
    "agent": "inform",
    "untrusted_external": "none",
    "legacy_untrusted": "none",
}
ORIGIN_TAINT = {
    "user": "trusted",
    "trusted_tool": "trusted",
    "agent": "agent",
    "untrusted_external": "untrusted",
    "legacy_untrusted": "untrusted",
}
DEFAULT_ORIGIN = "agent"
MAX_TOMBSTONES = 256

_WARNED_FALLBACK = False


def _secret() -> bytes:
    cfg = get_config()
    dedicated = str(cfg.security.get("memory_hmac_key", "") or "").strip()
    if dedicated:
        return dedicated.encode("utf-8")
    jwt = str(cfg.security_jwt_secret_key or "").strip()
    if jwt:
        # Domain separation: never sign memory with the raw JWT key.
        return ("memory-provenance\x00" + jwt).encode("utf-8")
    global _WARNED_FALLBACK
    if not _WARNED_FALLBACK:
        _WARNED_FALLBACK = True
        logger.warning("memory provenance uses dev fallback key (set [security].memory_hmac_key)")
    return b"weave-memory-provenance-dev"


def content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _safe_compare(left: str, right: str) -> bool:
    """compare_digest for arbitrary (possibly malformed/non-ASCII) strings."""
    try:
        return hmac.compare_digest(
            str(left).encode("utf-8"), str(right).encode("utf-8"),
        )
    except (TypeError, UnicodeEncodeError):
        return False


def _normalise_origin(origin: str) -> str:
    value = str(origin or "").strip()
    if value not in ORIGIN_ACT_CLASS:
        raise ValueError(
            f"Invalid memory origin {origin!r}; expected one of {', '.join(_ORIGINS)}"
        )
    return value


def _current_tenant(tenant: str | None) -> str:
    if tenant is not None:
        return str(tenant)
    from app.services.tenant_service import tenant_segment
    return tenant_segment()


def _lp(value: str) -> bytes:
    """Length-prefixed UTF-8 field — field boundaries are unambiguous."""
    data = str(value).encode("utf-8")
    return len(data).to_bytes(4, "big") + data


def _canonical_message(
    user_id: str, target: str, content: str, origin: str, tenant: str,
) -> bytes:
    return b"".join((
        _lp(_MSG_DOMAIN),
        _lp(tenant),
        _lp(str(user_id)),
        _lp(str(target)),
        _lp(origin),
        _lp(content_hash(content)),
    ))


def _legacy_signature(user_id: str, target: str, content: str) -> str:
    """Pre-P0 shape, used only to distinguish legacy-signed from tampered."""
    msg = f"{user_id}\x00{target}\x00{content_hash(content)}".encode("utf-8")
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def sign_entry(
    user_id: str,
    target: str,
    content: str,
    *,
    origin: str = DEFAULT_ORIGIN,
    tenant: str | None = None,
) -> str:
    """Sign one entry.

    ``origin`` is server-owned channel identity: only a call site that truly
    owns the ``user``/``trusted_tool`` channel may mint those classes.  The
    memory tool passes ``agent``.  Never derive an origin from content.
    """
    norm = _normalise_origin(origin)
    msg = _canonical_message(user_id, target, content, norm, _current_tenant(tenant))
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def verify_entry(
    user_id: str,
    target: str,
    content: str,
    signature: str,
    *,
    origin: str = DEFAULT_ORIGIN,
    tenant: str | None = None,
) -> bool:
    if not signature:
        return False
    try:
        expected = sign_entry(user_id, target, content, origin=origin, tenant=tenant)
    except ValueError:
        return False
    return _safe_compare(expected, str(signature))


def sidecar_path(memory_path: Path) -> Path:
    return memory_path.with_name(memory_path.stem + ".provenance.json")


def load_records(path: Path) -> dict[str, dict[str, Any]]:
    """Read v2 records; tolerate v1 flat maps for legacy classification.

    Unknown versions/shapes are ignored (fail closed: their entries become
    ``legacy-unsigned`` rather than trusted).
    """
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    if obj.get("version") == SIDECAR_VERSION and isinstance(obj.get("entries"), dict):
        out: dict[str, dict[str, Any]] = {}
        for key, value in obj["entries"].items():
            if not isinstance(value, dict):
                continue
            sig = str(value.get("sig") or "")
            origin = str(value.get("origin") or "")
            if not sig or origin not in ORIGIN_ACT_CLASS:
                continue
            out[str(key)] = {
                "sig": sig,
                "origin": origin,
                "tenant": str(value.get("tenant") or ""),
                "legacy": False,
            }
        return out
    out = {}
    for key, value in obj.items():
        if isinstance(value, str) and value:
            out[str(key)] = {
                "sig": value,
                "origin": None,
                "tenant": "",
                "legacy": True,
            }
    return out


def load_map(path: Path) -> dict[str, str]:
    """Compatibility view: content hash -> signature (v2 or v1)."""
    return {
        key: str(record.get("sig") or "")
        for key, record in load_records(path).items()
        if record.get("sig")
    }


def _load_removed_records(path: Path) -> dict[str, dict[str, Any]]:
    """Tombstone records (origin + insertion timestamp) for pruning order."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(obj, dict) or obj.get("version") != SIDECAR_VERSION:
        return {}
    removed = obj.get("removed")
    if not isinstance(removed, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in removed.items():
        if not isinstance(value, dict):
            continue
        origin = str(value.get("origin") or "legacy_untrusted")
        if origin not in ORIGIN_ACT_CLASS:
            origin = "legacy_untrusted"
        try:
            at = float(value.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        out[str(key)] = {"origin": origin, "at": at}
    return out


def load_removed(path: Path) -> dict[str, str]:
    """Tombstones for deleted/replaced entries: content hash -> last origin.

    A ``remove``/``replace`` keeps the hash here so a later ``add`` of the
    exact same text cannot be minted as agent authority.  Tombstones carry no
    signature — they can only *suppress or preserve* signing, never grant a
    higher class than the text previously had.
    """
    return {
        key: str(record.get("origin") or "legacy_untrusted")
        for key, record in _load_removed_records(path).items()
    }


def tombstone_origin(memory_path: Path, content: str) -> str | None:
    """Previously-known origin for this exact text, if it was removed."""
    return load_removed(sidecar_path(memory_path)).get(content_hash(content))


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Same-directory tempfile -> fsync -> os.replace (+ directory fsync).

    The sidecar is security metadata: owner-only (0600) and never observable
    half-written.  On failure the temp file is removed and the old sidecar is
    left untouched.
    """
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def sync_entries(
    memory_path: Path,
    user_id: str,
    target: str,
    entries: list[str],
    *,
    origins: list[str | None] | None = None,
    tombstones: dict[str, str | None] | None = None,
    tenant: str | None = None,
) -> bool:
    """Rewrite the sidecar for ``entries``.

    ``origins`` aligns 1:1 with ``entries``.  ``None`` means "do not sign this
    entry" — used to keep legacy/unsigned entries unsigned on unrelated
    rewrites (never launder them into agent-authored authority).  When
    ``origins`` is omitted nothing is signed: signing must be an explicit,
    channel-owned decision, never the default of a rewrite helper.

    ``tombstones`` records hashes removed by this rewrite (hash -> last known
    origin, ``None`` = legacy) so a later re-add cannot mint authority.
    Returns ``False`` only when the sidecar write failed (callers that delete
    content must then keep it: no tombstone = no proof the write is safe).
    """
    current_tenant = _current_tenant(tenant)
    if origins is None:
        origins = [None] * len(entries)
    if len(origins) != len(entries):
        raise ValueError("origins must align 1:1 with entries")
    # The file format has no per-entry id: identical content shares one hash.
    # If the same text carries different authority (e.g. a legacy copy plus a
    # new agent copy), signing it would elevate the legacy copy.  Fail closed:
    # ambiguous hashes are left unsigned.
    by_hash: dict[str, list[str | None]] = {}
    for entry, origin in zip(entries, origins):
        norm = _normalise_origin(origin) if origin is not None else None
        by_hash.setdefault(content_hash(entry), []).append(norm)
    ambiguous = {
        h for h, values in by_hash.items()
        if len(values) > 1 and len(set(values)) > 1
    }
    records: dict[str, dict[str, str]] = {}
    for entry, origin in zip(entries, origins):
        if origin is None:
            continue
        h = content_hash(entry)
        if h in ambiguous:
            continue
        norm = _normalise_origin(origin)
        records[h] = {
            "sig": sign_entry(user_id, target, entry, origin=norm, tenant=current_tenant),
            "origin": norm,
            "tenant": current_tenant,
        }
    path = sidecar_path(memory_path)
    removed = _load_removed_records(path)
    if tombstones:
        now = time.time()
        for h, origin in tombstones.items():
            norm = _normalise_origin(origin) if origin is not None else "legacy_untrusted"
            removed[str(h)] = {"origin": norm, "at": now}
        if len(removed) > MAX_TOMBSTONES:
            # sort_keys=True loses insertion order on reload; prune by the
            # recorded timestamp (missing/legacy timestamps sort oldest).
            keep = sorted(
                removed.items(),
                key=lambda item: float(item[1].get("at") or 0.0),
            )[-MAX_TOMBSTONES:]
            removed = dict(keep)
    try:
        if records or removed:
            doc: dict[str, Any] = {
                "version": SIDECAR_VERSION,
                "entries": records,
            }
            if removed:
                doc["removed"] = {
                    h: {
                        "origin": str(record.get("origin") or "legacy_untrusted"),
                        "tenant": current_tenant,
                        "at": float(record.get("at") or 0.0),
                    }
                    for h, record in removed.items()
                }
            _atomic_write_json(path, doc)
        elif path.exists():
            path.unlink()
        return True
    except OSError:
        logger.debug("memory provenance sidecar write failed", exc_info=True)
        return False


def preserved_origins(
    memory_path: Path,
    user_id: str,
    target: str,
    entries: list[str],
    *,
    tenant: str | None = None,
) -> list[str | None]:
    """Origins that may be carried over verbatim on a rewrite.

    Only a currently-valid v2 signature yields an origin.  Legacy/tampered
    entries yield ``None`` so the caller re-signs nothing it cannot attribute.
    """
    records = load_records(sidecar_path(memory_path))
    current_tenant = _current_tenant(tenant)
    out: list[str | None] = []
    for entry in entries:
        record = records.get(content_hash(entry))
        if not record or record.get("legacy"):
            out.append(None)
            continue
        if record.get("tenant", "") != current_tenant:
            out.append(None)
            continue
        if verify_entry(
            user_id, target, entry, record.get("sig", ""),
            origin=str(record.get("origin") or ""), tenant=current_tenant,
        ):
            out.append(str(record["origin"]))
        else:
            out.append(None)
    return out


def _key_ready() -> bool:
    try:
        return bool(get_config().memory_authority_key_ready)
    except Exception:
        return False


def _meta(
    verified: bool, status: str, origin: str, *, key_ready: bool,
) -> dict[str, Any]:
    act = ORIGIN_ACT_CLASS[origin]
    taint = ORIGIN_TAINT[origin]
    if verified and not key_ready and act == "act":
        # With a known/dev HMAC key any signature is forgeable: keep the
        # signature verdict but never expose act authority.
        act = "inform"
        taint = "agent"
    return {
        "verified": verified,
        "status": status,
        "origin": origin,
        "act_class": act,
        "taint": taint,
        "key_status": "ready" if key_ready else "dev",
    }


def annotate_entries(
    memory_path: Path,
    user_id: str,
    target: str,
    entries: list[str],
    *,
    tenant: str | None = None,
) -> list[dict[str, Any]]:
    records = load_records(sidecar_path(memory_path))
    current_tenant = _current_tenant(tenant)
    key_ready = _key_ready()
    out: list[dict[str, Any]] = []
    for entry in entries:
        record = records.get(content_hash(entry))
        if not record:
            out.append(_meta(False, "legacy-unsigned", "legacy_untrusted", key_ready=key_ready))
            continue
        if record.get("legacy"):
            if _safe_compare(_legacy_signature(user_id, target, entry),
                             str(record.get("sig") or "")):
                out.append(_meta(False, "legacy-signed", "legacy_untrusted", key_ready=key_ready))
            else:
                out.append(_meta(False, "tampered", "untrusted_external", key_ready=key_ready))
            continue
        if record.get("tenant", "") != current_tenant:
            out.append(_meta(False, "tampered", "untrusted_external", key_ready=key_ready))
            continue
        if not verify_entry(
            user_id, target, entry, str(record.get("sig") or ""),
            origin=str(record.get("origin") or ""), tenant=current_tenant,
        ):
            out.append(_meta(False, "tampered", "untrusted_external", key_ready=key_ready))
            continue
        out.append(_meta(True, "signed", str(record["origin"]), key_ready=key_ready))
    return out


def verified_entries(
    memory_path: Path,
    user_id: str,
    target: str,
    entries: list[str],
    *,
    tenant: str | None = None,
) -> list[str]:
    """Entries whose v2 signature verifies — the only ones strict mode trusts."""
    meta = annotate_entries(memory_path, user_id, target, entries, tenant=tenant)
    return [entry for entry, m in zip(entries, meta) if m.get("verified")]
