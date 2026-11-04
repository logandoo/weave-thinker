# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""T1b (2026-10-18, arXiv:2610.13889 RATED): tool reliability memory.

The registry already counted dispatches; it never recorded whether a call
succeeded. This service keeps a tiny, cheap reliability ledger and re-ranks
*discoverable* tool candidates. It never removes a tool and never changes an
explicit dispatch; cold start is neutral, so the default ordering is stable.

Persistence is best-effort (background DB write); the in-memory ledger is the
hot path and works even when the DB is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

HARD_FAILURE_TTL = 600.0  # seconds — repeated timeout/auth/5xx only


@dataclass
class _Stat:
    n: int = 0
    successes: int = 0
    failures: int = 0
    last_failure_at: float = 0.0
    last_error_class: str = ""
    total_latency_ms: float = 0.0


_lock = threading.Lock()
_stats: Dict[Tuple[str, str], _Stat] = {}
_persist_enabled = True


def _key(tool: str, provider: str = "") -> Tuple[str, str]:
    return (str(tool or ""), str(provider or ""))


def record(tool: str, ok: bool, error_class: str = "", latency_ms: float = 0.0,
           provider: str = "") -> None:
    """Record one execution outcome (sync, cheap, never raises)."""
    if not tool:
        return
    key = _key(tool, provider)
    with _lock:
        st = _stats.setdefault(key, _Stat())
        st.n += 1
        st.total_latency_ms += max(0.0, float(latency_ms or 0.0))
        if ok:
            st.successes += 1
        else:
            st.failures += 1
            st.last_failure_at = time.time()
            st.last_error_class = str(error_class or "")[:60]
    if _persist_enabled:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_persist_bg(key, ok, error_class, latency_ms))
        except RuntimeError:
            pass
        except Exception:
            logger.debug("tool reliability persist schedule failed", exc_info=True)


async def _persist_bg(key: Tuple[str, str], ok: bool, error_class: str,
                      latency_ms: float) -> None:
    try:
        from app.db.database import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            await db.execute(text("""
                INSERT INTO tool_reliability_stats
                  (tool_name, provider, n, successes, failures, total_latency_ms,
                   last_error_class, last_failure_at, updated_at)
                VALUES (:t, :p, 1, CAST(:s AS INTEGER), CAST(:f AS INTEGER),
                        CAST(:lat AS DOUBLE PRECISION), :ec,
                        CASE WHEN CAST(:f AS INTEGER) > 0 THEN NOW() ELSE NULL END, NOW())
                ON CONFLICT (tool_name, provider) DO UPDATE SET
                  n = tool_reliability_stats.n + 1,
                  successes = tool_reliability_stats.successes + CAST(:s AS INTEGER),
                  failures = tool_reliability_stats.failures + CAST(:f AS INTEGER),
                  total_latency_ms = tool_reliability_stats.total_latency_ms + CAST(:lat AS DOUBLE PRECISION),
                  last_error_class = CASE WHEN CAST(:f AS INTEGER) > 0 THEN EXCLUDED.last_error_class
                                          ELSE tool_reliability_stats.last_error_class END,
                  last_failure_at = CASE WHEN CAST(:f AS INTEGER) > 0 THEN NOW()
                                         ELSE tool_reliability_stats.last_failure_at END,
                  updated_at = NOW()
            """), {"t": key[0], "p": key[1], "s": 1 if ok else 0,
                   "f": 0 if ok else 1, "lat": max(0.0, float(latency_ms or 0.0)),
                   "ec": str(error_class or "")[:60]})
            await db.commit()
    except Exception:
        logger.debug("tool reliability persist failed (non-blocking)", exc_info=True)


def _reliability(tool: str, provider: str = "") -> Tuple[float, bool]:
    key = _key(tool, provider)
    with _lock:
        st = _stats.get(key)
        # Registry records tool-level outcomes (provider=""); discovery
        # candidates carry a toolset/server label. Fall back to the tool-level
        # ledger when no provider-specific observations exist.
        if st is None and provider:
            st = _stats.get(_key(tool, ""))
        if st is None or st.n <= 0:
            return 0.5, False
        # Beta(1,1) prior: cold start neutral, small samples stay conservative.
        mean = (st.successes + 1.0) / (st.n + 2.0)
        hard = (
            st.failures >= 2
            and st.last_failure_at > 0
            and (time.time() - st.last_failure_at) < HARD_FAILURE_TTL
        )
        return mean, hard


def rank_candidates(scored: Sequence[Tuple[float, dict]],
                    *, provider_of=None) -> List[Tuple[float, dict]]:
    """Stable re-rank by ``semantic_score * reliability_multiplier``.

    ``scored`` is the existing [(score, candidate_dict)] list. With no
    observations every multiplier is 1.0, so the original order is preserved.
    """
    out: List[Tuple[float, dict]] = []
    for score, cand in scored:
        name = str(cand.get("name") or "")
        provider = provider_of(cand) if provider_of else str(cand.get("server") or "")
        mean, hard = _reliability(name, provider)
        multiplier = 0.5 + 0.5 * mean
        if hard:
            multiplier *= 0.5
        out.append((float(score) * multiplier, cand))
    # Python's sort is stable; equal multipliers keep the semantic order.
    out.sort(key=lambda x: -x[0])
    return out


def provider_order(configured: Iterable[str], primary: str) -> List[str]:
    """Keep the user-ruled primary first; reorder only the fallbacks.

    Never adds/drops a configured provider. Stable for equal reliability.
    """
    primary = str(primary or "")
    rest = [str(p) for p in configured if str(p) and str(p) != primary]
    if not rest:
        return [primary] if primary else []
    decorated = []
    for idx, p in enumerate(rest):
        mean, hard = _reliability(f"web_search:{p}", p)
        decorated.append((0.5 if hard else 1.0, -mean, idx, p))
    decorated.sort()
    return ([primary] if primary else []) + [p for _h, _m, _i, p in decorated]


def reset_for_tests() -> None:
    global _persist_enabled
    with _lock:
        _stats.clear()
    _persist_enabled = False


def snapshot() -> Dict[str, dict]:
    with _lock:
        return {
            f"{t}|{p}": {
                "n": s.n, "successes": s.successes, "failures": s.failures,
                "last_error_class": s.last_error_class,
                "avg_latency_ms": round(s.total_latency_ms / s.n, 2) if s.n else 0.0,
            }
            for (t, p), s in _stats.items()
        }
