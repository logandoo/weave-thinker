# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P0/P2 multi-tenant: filesystem namespace boundary (experimental).

This is a **filesystem namespace only** — not database/API tenant isolation.
Setting a tenant id requires ``[security].multi_tenant_experimental = true``;
the startup guard refuses to boot otherwise, and the memory tool refuses to
resolve a path through the guard (fail closed instead of silently claiming
isolation).  When no tenant id is configured the legacy single-tenant layout
is unchanged.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_TRUE_VALUES = {"1", "true", "yes", "on"}
_MAX_SLUG = 48


class TenantConfigError(RuntimeError):
    """Tenant mode is misconfigured; fail closed instead of claiming isolation."""


def _segment_digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def tenant_segment(raw_override: str | None = None) -> str:
    """Map a configured tenant id to a collision-resistant path segment.

    A lossy ``re.sub`` alone fails open: ``公司`` sanitises to ``""`` (which
    would silently use the single-tenant root), ``华北研究院`` and other CJK ids
    collapse together, and a 70-char id truncates onto a different tenant.
    Any id that does not survive the filesystem slug unchanged — non-ASCII,
    separators, uppercase (case-insensitive filesystems), over-length — gets a
    stable SHA-256 suffix of the RAW id, so distinct configured ids always map
    to distinct segments and an empty segment only means "no tenant".

    ``raw_override`` lets the verdict-chain CLI normalise a configured id
    without mutating env/config; an already-normalised segment passes through.
    """
    if raw_override is not None:
        raw = str(raw_override).strip()
    else:
        raw = os.environ.get("WT_TENANT_ID", "").strip()
        if not raw:
            try:
                from app.core.config import get_config
                raw = str(get_config().security.get("tenant_id", "") or "").strip()
            except Exception:
                raw = ""
    if not raw:
        return ""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-")
    if slug == raw and len(raw) <= _MAX_SLUG and raw == raw.lower():
        return slug
    prefix = (slug[:_MAX_SLUG].strip("-") or "tenant")
    return f"{prefix}-{_segment_digest(raw)}"[:64]


def multi_tenant_experimental() -> bool:
    """Explicit opt-in for the filesystem-namespace tenant mode.

    ``WT_MULTI_TENANT_EXPERIMENTAL`` wins so tests/ops can override a config
    file without editing it; otherwise ``[security].multi_tenant_experimental``
    (default false).
    """
    env = os.environ.get("WT_MULTI_TENANT_EXPERIMENTAL")
    if env is not None:
        return env.strip().lower() in _TRUE_VALUES
    try:
        from app.core.config import get_config
        return bool(get_config().security.get("multi_tenant_experimental", False))
    except Exception:
        return False


def tenant_config_errors() -> list[str]:
    """Startup guard: a tenant id without the experimental flag is a refusal.

    Rationale: the filesystem segment is not an isolation guarantee (no DB
    columns/RLS), so silently booting with it would over-claim tenancy.
    """
    segment = tenant_segment()
    if not segment:
        return []
    if not multi_tenant_experimental():
        return [
            f"tenant_id={segment!r} is set but [security].multi_tenant_experimental "
            "is not true — multi-tenant is a filesystem-namespace experiment; enable "
            "the flag explicitly or unset tenant_id"
        ]
    return []


def guard_memory_base(base: Path) -> Path:
    """Validate the file-memory base before ``scoped_root`` adds the segment.

    ``AGENT_MEMORY_DIR`` is accepted only as a base: the tenant segment is
    still appended by the caller, so an env override cannot escape the tenant
    namespace.  Tenant mode without the experimental flag raises instead of
    falling back to an unscoped path.
    """
    if tenant_segment() and not multi_tenant_experimental():
        raise TenantConfigError(
            "tenant mode requires [security].multi_tenant_experimental=true "
            "(filesystem namespace only; no DB/RLS isolation)"
        )
    return base


def scoped_root(base: Path, tenant: str | None = None) -> Path:
    t = tenant if tenant is not None else tenant_segment()
    return base / t if t else base
