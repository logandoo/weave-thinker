# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Validity semantics for long-term memory (TODO 2026-11-08 A1/A2/A3/A6/J1).

`memory_concepts.valid_to` alone conflated five lifecycle events:
merge/supersede, source loss, user retraction, user forgetting and expiry.
This module is the single writer/reader vocabulary for the new
`invalid_reason` column so API, retrieval, UI and sync cannot drift apart.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

INVALID_REASONS = {"merged", "source_lost", "retracted", "forgotten", "expired"}

# Sync authority guard: validity columns are server-authoritative unless the
# incoming snapshot carries a newer `validity_updated_at`.
VALIDITY_FIELDS = frozenset({
    "valid_to", "superseded_by", "invalid_reason", "needs_review", "status",
    "validity_updated_at",
})

_KEY_NOISE_RE = re.compile(r"[\W_]+", re.UNICODE)
_KEY_PREFIX_RE = re.compile(r"^用户的?", re.IGNORECASE)


def derive_invalid_reason(
    *,
    invalid_reason: str | None = None,
    superseded_by: str | None = None,
    status: str | None = None,
    needs_review: bool = False,
) -> str | None:
    """Derive a reason for legacy rows (invalid_reason IS NULL).

    Precedence: explicit valid reason → merged → forgotten → source_lost →
    expired. An explicit *unknown* value is left undetermined (None) rather
    than silently rewritten.
    """
    if invalid_reason in INVALID_REASONS:
        return invalid_reason
    if invalid_reason:
        return None
    if superseded_by:
        return "merged"
    if status == "forgotten":
        return "forgotten"
    if needs_review:
        return "source_lost"
    return "expired"


def reset_verification(concept: Any) -> None:
    """A concept that stopped being current must not keep a green verify tag."""
    concept.verification_state = None
    concept.last_verified_at = None
    concept.verification_evidence = None
    if hasattr(concept, "verification_correction"):
        concept.verification_correction = None


def apply_invalidation(
    concept: Any,
    reason: str,
    *,
    superseded_by: str | None = None,
    status: str | None = None,
    zero_weight: bool | None = None,
    now: datetime | None = None,
) -> None:
    """Apply one validity transition and bump the sync authority timestamp.

    `merged`/`source_lost`/`expired` keep the fact's weight (it may still be
    informative as history). `retracted`/`forgotten` zero it.
    """
    if reason not in INVALID_REASONS:
        raise ValueError(f"unknown invalid reason: {reason!r}")
    now = now or datetime.utcnow()
    concept.valid_to = now
    concept.invalid_reason = reason
    if superseded_by is not None:
        concept.superseded_by = superseded_by
    if status is None and reason == "forgotten":
        status = "forgotten"
    if status is not None:
        concept.status = status
    should_zero = reason in ("retracted", "forgotten") if zero_weight is None else zero_weight
    if should_zero:
        concept.weight = 0
    reset_verification(concept)
    if hasattr(concept, "validity_updated_at"):
        concept.validity_updated_at = now
    concept.updated_at = now
    # 评审修复（P1-17）：失效即令该用户拼音扫描缓存失效——否则 TTL 内
    # active 口径的缓存命中仍可能返回刚被失效的概念（filters 兜底前的双保险）。
    uid = getattr(concept, "user_id", "")
    if uid:
        phonetic_cache_invalidate(uid)


def validity_label(
    reason: str | None,
    *,
    superseded_name: str | None = None,
    valid_to: datetime | None = None,
    status: str | None = None,
) -> str:
    """Human/LLM-facing label. Merged history must never read as "失效"."""
    if not reason:
        return ""
    if reason == "merged":
        target = (superseded_name or "").strip() or "新版本"
        return f"（历史版本，已并入 {target}）"
    if reason == "retracted":
        return "（用户已否认）"
    if reason == "source_lost":
        return "（来源已丢失，待核实）"
    if reason == "forgotten" or status == "forgotten":
        return "（用户已遗忘）"
    if reason == "expired":
        if valid_to:
            return f"（已于 {valid_to.strftime('%Y-%m-%d')} 失效）"
        return "（已过期）"
    return ""


def normalize_entity_key(value: str) -> str:
    """Deterministic entity/page key: strip spaces/punctuation + role prefix."""
    s = str(value or "").strip().lower()
    s = _KEY_PREFIX_RE.sub("", s)
    s = _KEY_NOISE_RE.sub("", s)
    return s[:120]


def phonetic_key(value: str) -> str:
    """C8：CJK 同音别名键（洛瞳/洛彤/洛童 → luotong）。

    Only CJK strings are mapped; Latin keys are left to exact/alias matching so
    unrelated English words are never merged. pypinyin is a pinned dependency.
    """
    s = str(value or "").strip()
    if not s or not re.search(r"[\u4e00-\u9fff]", s):
        return ""
    try:
        from pypinyin import lazy_pinyin
        out = "".join(lazy_pinyin(s))
    except Exception:
        return ""
    first = re.search(r"[\u4e00-\u9fff]", s)
    prefix = first.group(0) if first else ""
    return f"py:{prefix}:{out}"[:120]


def phonetic_pinyin(value: str) -> str:
    """P1-17（2026-11-08）：纯拼音键（不含首字腿）——只读同音召回用，
    覆盖 络瞳↔洛瞳 这类首字不同的转写变体。分组/别名合并仍用 phonetic_key
    （首字腿是防误并的保守守卫），两键各司其职。

    评审修复：带声调（TONE3）——无声调时 山西/陕西 同为 shanxi 会被跨词误召回；
    声调不拆同音变体（络瞳/洛瞳/洛彤/洛童 同为 luo4tong2）。"""
    s = str(value or "").strip()
    if not s or not re.search(r"[\u4e00-\u9fff]", s):
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
        return "".join(lazy_pinyin(s, style=Style.TONE3))
    except Exception:
        return ""


def entity_keys_for(name: str, aliases: Iterable[Any] | None = None) -> list[str]:
    """Name + aliases → ordered unique normalized keys (alias + homophone)."""
    out: list[str] = []
    for raw in [name, *(list(aliases or []))]:
        value = str(raw or "")
        key = normalize_entity_key(value)
        if key and key not in out:
            out.append(key)
        py = phonetic_key(value)
        if py and py not in out:
            out.append(py)
    return out


# ── P1-17（2026-11-08）：拼音回退扫描的进程内 TTL 缓存 ──────────────
# 回退匹配需要全量（≤500 concepts / ≤300 pages）内存比对；缓存命中后按 id
# 直查，避免每次 memory_search 都拉满扫描面。TTL 内新建/失效行最迟 60s 后可见
# （只读检索工具可接受；写入路径可调用 phonetic_cache_invalidate 立即失效）。
_PHONETIC_CACHE: dict[str, tuple[float, list[str]]] = {}
_PHONETIC_TTL_SECONDS = 60.0
_PHONETIC_CACHE_MAX = 512


def phonetic_cache_get(key: str) -> list[str] | None:
    import time as _t
    hit = _PHONETIC_CACHE.get(key)
    if not hit:
        return None
    expires, ids = hit
    if _t.monotonic() >= expires:
        _PHONETIC_CACHE.pop(key, None)
        return None
    return list(ids)


def phonetic_cache_put(key: str, ids: list[str]) -> None:
    import time as _t
    if len(_PHONETIC_CACHE) >= _PHONETIC_CACHE_MAX:
        _PHONETIC_CACHE.clear()
    _PHONETIC_CACHE[key] = (_t.monotonic() + _PHONETIC_TTL_SECONDS, list(ids))


def phonetic_cache_invalidate(user_id: str = "") -> None:
    """写入路径调用：立即失效该用户（或全量）的拼音扫描缓存。

    评审修复：键形如 `c:{uid}:{target}:a` / `p:{uid}:{target}`，旧前缀
    `*:{uid}:` 是字面子串判断、永不命中（invalidate 实为 no-op）。
    `":{uid}:"` 段匹配安全：uid 为 uuid 段，不含冒号，不会误伤他人键。
    """
    needle = f":{user_id}:" if user_id else ""
    for k in [k for k in _PHONETIC_CACHE if (not needle or needle in k)]:
        _PHONETIC_CACHE.pop(k, None)


def _ts(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return 0.0


def filter_validity_payload(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Drop stale validity fields from a sync payload (A6).

    A repair/forget bumps `validity_updated_at` server-side; an old device
    snapshot must not mirror valid_to/status back. Snapshots newer than the
    server win (normal cross-device LWW). Legacy rows without a timestamp are
    protected whenever the server row still carries validity state.
    """
    out = dict(incoming or {})
    current = current or {}
    cur_ts = _ts(current.get("validity_updated_at"))
    in_ts = _ts(out.get("validity_updated_at"))
    if in_ts > cur_ts:
        return out
    if in_ts == cur_ts and cur_ts > 0:
        return out
    has_current_validity = any(
        current.get(k) not in (None, False, "", "active")
        for k in VALIDITY_FIELDS if k != "validity_updated_at"
    )
    if cur_ts == 0 and not has_current_validity:
        return out
    for key in VALIDITY_FIELDS:
        out.pop(key, None)
    return out


async def expire_due_concepts(db: Any, user_id: str, now: datetime | None = None) -> int:
    """Expiry writer: rows whose valid_to has passed get `invalid_reason='expired'`.

    Keeps the original valid_to (that is the fact's expiry time) and only fills
    the missing reason / clears stale verification.
    """
    from sqlalchemy import select

    from app.db.database import MemoryConcept

    now = now or datetime.utcnow()
    rows = (await db.execute(
        select(MemoryConcept).where(
            MemoryConcept.user_id == user_id,
            MemoryConcept.valid_to.is_not(None),
            MemoryConcept.valid_to <= now,
            MemoryConcept.invalid_reason.is_(None),
        )
    )).scalars().all()
    for concept in rows:
        concept.invalid_reason = "expired"
        concept.validity_updated_at = now
        concept.updated_at = now
        reset_verification(concept)
    if rows:
        await db.flush()
    return len(rows)

