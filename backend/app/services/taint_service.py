# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""P1a TMA-NM: monotonic conversation taint ledger (M2).

The ledger never raises authority: every source is marked with the lowest
untrust it carries, derived content takes the maximum (lowest) taint, and a
later trusted source cannot lower the conversation max.  It is intentionally
coarse (conversation-level, not per-value) for P1a; per-chunk parent_ids and
cross-agent delegation are P1b.

Persisted state (``taint_states``) lets a resumed conversation keep its taint;
the file-memory sidecar remains the persistence layer for memory entries.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.core.config import get_config

logger = logging.getLogger(__name__)

TAINT_TRUSTED = "trusted"
TAINT_AGENT = "agent"
TAINT_UNTRUSTED = "untrusted"
TAINT_RANK = {TAINT_TRUSTED: 3, TAINT_AGENT: 2, TAINT_UNTRUSTED: 1}

ACT_ACT = "act"
ACT_INFORM = "inform"
ACT_NONE = "none"
ACT_RANK = {ACT_ACT: 3, ACT_INFORM: 2, ACT_NONE: 1}

ORIGIN_ACT_CLASS = {
    "user": ACT_ACT,
    "trusted_tool": ACT_ACT,
    "agent": ACT_INFORM,
    "untrusted_external": ACT_NONE,
    "legacy_untrusted": ACT_NONE,
}
ORIGIN_TAINT = {
    "user": TAINT_TRUSTED,
    "trusted_tool": TAINT_TRUSTED,
    "agent": TAINT_AGENT,
    "untrusted_external": TAINT_UNTRUSTED,
    "legacy_untrusted": TAINT_UNTRUSTED,
}

# Content-retrieval tools are externally sourced.  Unknown tools fail closed
# to untrusted: a newly added content tool must opt IN to agent output, never
# silently inherit it.
_RETRIEVAL_PREFIXES = (
    "browser_", "workspace_read", "workspace_list", "session_search",
    "file_", "pdf_read", "image_", "vision_", "mcp_", "web_search",
    "web_fetch", "fetch", "grep", "context7_",
)
_RETRIEVAL_TOOLS = frozenset({
    "browser",              # the primary page reader (no underscore)
    "web_search",
    "web_fetch",
    "fetch",
    "workspace_read",
    "workspace_list",
    "session_search",
    "grep",
    "context7_query_docs",
    "job_logs",             # stdout of an arbitrary job can contain fetched content
    "vision_interpret",     # VLM reading of workspace/browser screenshots
})

# Tools whose output is agent-produced (command execution, local writes,
# confirmations, local skill catalog).  They still inherit max(input, agent)
# so untrusted input is never washed.
_AGENT_TOOLS = frozenset({
    "terminal",
    "execute_code",
    "process",
    "job_submit",
    "job_cancel",
    "job_status",
    "job_list",
    "job_artifacts",
    "workspace_write",
    "workspace_edit",
    "workspace_glob",
    "workspace_snapshot",
    "provide_file",
    "provide_folder",
    "pdf_export",
    "search_tools",
    "skill_view",
    "skill_manage",
    # 2026-11-02（C1）：同构于 skill_manage——agent 自产的 MCP 管理结果
    # （服务器名/掩码键）不是外部内容。缺此条会落到 classify_tool_result 的
    # UNTRUSTED 兜底，经 TaintLedger.max_taint 单调不可逆下降，enforce 下
    # 让整个会话的 act 类工具自锁。
    "manage_mcp_server",
    "skill_run_script",
    "background_task",
    "schedule",
    "delegate_task",
    "delegate_agent_task",
    "git",
    "tts_synthesize",
    "asr_transcribe",
    "system",
    # Pure/local derived tools: their output carries no external content.
    "calculate",
    "word_count",
    "citation_render",
    "diff",
    "mixture_of_agents",
})

_NOTES_WRITE_ACTIONS = frozenset({
    "create_note", "update_note", "append_note", "delete_note",
    "create_notebook", "update_notebook", "delete_notebook",
    # Generic verbs kept for forward compatibility.
    "create", "update", "delete", "remove", "add",
})

_LEDGERS: dict[str, "TaintLedger"] = {}
_LEDGER_TTL_SECONDS = 6 * 3600
_LEDGER_LAST_SEEN: dict[str, float] = {}
_MAX_SOURCES = 128


def normalise_taint(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in TAINT_RANK else TAINT_UNTRUSTED


def max_taint(*values: Any) -> str:
    """Lattice max = lowest authority (the taint that sticks)."""
    ranks = [TAINT_RANK[normalise_taint(v)] for v in values if str(v or "").strip()]
    if not ranks:
        return TAINT_AGENT
    rank = min(ranks)
    return next(key for key, value in TAINT_RANK.items() if value == rank)


def min_act_class(*values: Any) -> str:
    ranks = []
    for value in values:
        text = str(value or "").strip().lower()
        if text in ACT_RANK:
            ranks.append(ACT_RANK[text])
    if not ranks:
        return ACT_NONE
    rank = min(ranks)
    return next(k for k, v in ACT_RANK.items() if v == rank)


@dataclass
class TaintSource:
    source_id: str
    taint: str
    origin: str = "agent"
    act_class: str | None = None
    principal: str = "agent"
    parent_ids: tuple[str, ...] = ()
    label: str = ""

    def __post_init__(self) -> None:
        self.taint = normalise_taint(self.taint)
        self.origin = str(self.origin or "agent")
        if self.act_class not in ACT_RANK:
            self.act_class = ORIGIN_ACT_CLASS.get(self.origin, ACT_NONE)
        self.parent_ids = tuple(str(x) for x in self.parent_ids)
        self.principal = str(self.principal or "agent")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "taint": self.taint,
            "origin": self.origin,
            "act_class": self.act_class,
            "principal": self.principal,
            "parent_ids": list(self.parent_ids),
            "label": self.label,
        }


def derived(inputs: Iterable[TaintSource]) -> tuple[str, str]:
    values = list(inputs)
    return (
        max_taint(*[v.taint for v in values]),
        min_act_class(*[v.act_class for v in values]),
    )


@dataclass
class TaintLedger:
    conversation_id: str
    user_id: str = ""
    max_taint: str = TAINT_TRUSTED
    sources: dict[str, TaintSource] = field(default_factory=dict)
    loaded: bool = False
    dirty: bool = False
    version: int = 0

    def mark(
        self,
        source_id: str,
        taint: str,
        *,
        origin: str = "agent",
        act_class: str | None = None,
        principal: str = "agent",
        parent_ids: Iterable[str] = (),
        label: str = "",
    ) -> TaintSource:
        sid = str(source_id or "")
        current = self.sources.get(sid)
        if current is not None:
            # A source cannot be laundered by re-marking it with higher trust.
            merged = max_taint(current.taint, taint)
            if merged == current.taint:
                # No new information: keep the original provenance fields.
                return current
            taint = merged
        source = TaintSource(
            source_id=sid,
            taint=taint,
            origin=origin,
            act_class=act_class,
            principal=principal,
            parent_ids=parent_ids,
            label=label,
        )
        self.sources[sid] = source
        if len(self.sources) > _MAX_SOURCES:
            self.sources = dict(list(self.sources.items())[-_MAX_SOURCES:])
        self.max_taint = max_taint(self.max_taint, source.taint)
        self.dirty = True
        self.version += 1
        return source

    def trusted_principals(self) -> list[str]:
        """Out-of-band trusted principals only (M3 corroboration).

        A tool echo is never an independent corroborator: only a real user
        channel or an explicitly allow-listed trusted tool may count.  This
        prevents `memory`/`calculate` echoes from fabricating a second
        principal next to the user turn.
        """
        return sorted({
            source.principal
            for source in self.sources.values()
            if source.taint == TAINT_TRUSTED
            and source.origin in ("user", "trusted_tool")
        })

    def snapshot(self) -> list[dict[str, Any]]:
        return [source.as_dict() for source in self.sources.values()]


def reset_cache_for_tests() -> None:
    _LEDGERS.clear()
    _LEDGER_LAST_SEEN.clear()


def forget_user(user_id: str) -> int:
    """Drop cached ledgers for a user after a GDPR erase.

    Without this, the next mark would re-persist the deleted sources from the
    in-process snapshot (the endpoint already evicts its other caches).
    """
    uid = str(user_id or "")
    removed = [
        cid for cid, ledger in _LEDGERS.items()
        if str(getattr(ledger, "user_id", "")) == uid
    ]
    for cid in removed:
        _LEDGERS.pop(cid, None)
        _LEDGER_LAST_SEEN.pop(cid, None)
    return len(removed)


def _prune_cache() -> None:
    now = time.time()
    stale = [
        key for key, seen in _LEDGER_LAST_SEEN.items()
        if now - seen > _LEDGER_TTL_SECONDS
    ]
    for key in stale:
        _LEDGERS.pop(key, None)
        _LEDGER_LAST_SEEN.pop(key, None)
    while len(_LEDGERS) > 2000:
        oldest = min(_LEDGER_LAST_SEEN, key=_LEDGER_LAST_SEEN.get)
        _LEDGERS.pop(oldest, None)
        _LEDGER_LAST_SEEN.pop(oldest, None)


def get_or_create_ledger(conversation_id: str, user_id: str = "") -> TaintLedger:
    cid = str(conversation_id or "_shared")
    _prune_cache()
    ledger = _LEDGERS.get(cid)
    if ledger is None:
        ledger = TaintLedger(cid, str(user_id or ""))
        _LEDGERS[cid] = ledger
    elif user_id:
        ledger.user_id = str(user_id)
    _LEDGER_LAST_SEEN[cid] = time.time()
    return ledger


def cached_ledger(conversation_id: str) -> TaintLedger | None:
    return _LEDGERS.get(str(conversation_id or "_shared"))


def background_conversation_key(user_id: str) -> str:
    """Stable <=36-char taint key for tasks without a conversation.

    The DB column is String(36); a raw ``_background:<uuid>`` key overflows and
    made live/recovery ledgers miss each other.  Hash to a short stable key.
    """
    raw = str(user_id or "")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"bg:{digest}"


_PERSIST_WARNED: set[str] = set()


def _warn_once(kind: str, conversation_id: str, exc: BaseException) -> None:
    key = f"{kind}:{conversation_id}"
    if key in _PERSIST_WARNED:
        return
    _PERSIST_WARNED.add(key)
    logger.warning(
        "taint ledger %s failed for conversation=%s (will retry; "
        "check the taint_states migration): %s",
        kind, conversation_id, exc,
    )


async def _load_committed(conversation_id: str) -> tuple[str, list[dict]] | None:
    """Read committed state in its own session (never poison the tool session)."""
    from sqlalchemy import text
    from app.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT max_taint, sources_json FROM taint_states "
                "WHERE conversation_id = :cid"
            ),
            {"cid": conversation_id},
        )
        row = result.fetchone()
    if row is None:
        return None
    sources = row[1] or []
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except ValueError:
            sources = []
    return str(row[0] or TAINT_TRUSTED), sources if isinstance(sources, list) else []


async def load_ledger(db: Any, conversation_id: str, user_id: str = "") -> TaintLedger:
    """Hydrate the cached ledger from committed ``taint_states`` on first use.

    Uses its own short session: a missing table or transient DB error must not
    abort the caller's tool transaction.  ``loaded`` is set only after a
    successful read so a later call can recover.
    """
    ledger = get_or_create_ledger(conversation_id, user_id)
    if ledger.loaded:
        return ledger
    try:
        state = await _load_committed(ledger.conversation_id)
    except Exception as exc:  # table missing / DB down
        _warn_once("load", ledger.conversation_id, exc)
        return ledger
    ledger.loaded = True
    if state is None:
        return ledger
    stored_max, sources = state
    ledger.max_taint = max_taint(ledger.max_taint, stored_max)
    for item in sources:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "")
        if not sid:
            continue
        ledger.mark(
            sid,
            item.get("taint") or TAINT_UNTRUSTED,
            origin=item.get("origin") or "agent",
            act_class=item.get("act_class"),
            principal=item.get("principal") or "agent",
            parent_ids=item.get("parent_ids") or (),
            label=item.get("label") or "",
        )
    ledger.dirty = False
    return ledger


async def persist_ledger(db: Any, ledger: TaintLedger) -> None:
    """Durably upsert the monotonic state in its own committed session.

    Merges with the committed snapshot under a row lock so a stale/concurrent
    writer cannot drop the sources that justify ``untrusted``.  ``db`` is
    accepted for call-site compatibility but deliberately unused.
    """
    if ledger is None or not ledger.dirty:
        return
    snapshot_version = ledger.version
    try:
        from sqlalchemy import text
        from app.db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            # A missing row locks nothing; materialise it first so two
            # first-time writers serialise on the same row.
            await session.execute(
                text(
                    "INSERT INTO taint_states "
                    "(conversation_id, user_id, max_taint, sources_json, updated_at) "
                    "VALUES (:cid, :uid, 'trusted', '[]'::jsonb, NOW()) "
                    "ON CONFLICT (conversation_id) DO NOTHING"
                ),
                {"cid": ledger.conversation_id, "uid": ledger.user_id},
            )
            row = (await session.execute(
                text(
                    "SELECT max_taint, sources_json FROM taint_states "
                    "WHERE conversation_id = :cid FOR UPDATE"
                ),
                {"cid": ledger.conversation_id},
            )).fetchone()
            merged: dict[str, dict] = {}
            committed_max = TAINT_TRUSTED
            if row is not None:
                committed_max = str(row[0] or TAINT_TRUSTED)
                existing = row[1] or []
                if isinstance(existing, str):
                    try:
                        existing = json.loads(existing)
                    except ValueError:
                        existing = []
                for item in existing if isinstance(existing, list) else []:
                    if isinstance(item, dict) and item.get("source_id"):
                        merged[str(item["source_id"])] = item
            for item in ledger.snapshot():
                sid = str(item.get("source_id") or "")
                if not sid:
                    continue
                previous = merged.get(sid)
                if (
                    previous
                    and previous.get("taint") == TAINT_UNTRUSTED
                    and item.get("taint") != TAINT_UNTRUSTED
                ):
                    continue  # never drop an existing untrusted source
                merged[sid] = item
            final_max = max_taint(committed_max, ledger.max_taint)
            await session.execute(
                text(
                    """
                    INSERT INTO taint_states
                        (conversation_id, user_id, max_taint, sources_json, updated_at)
                    VALUES
                        (:cid, :uid, :taint, CAST(:sources AS JSONB), NOW())
                    ON CONFLICT (conversation_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        max_taint = CASE
                            WHEN EXCLUDED.max_taint = 'untrusted'
                                 OR taint_states.max_taint = 'untrusted' THEN 'untrusted'
                            WHEN EXCLUDED.max_taint = 'agent'
                                 OR taint_states.max_taint = 'agent' THEN 'agent'
                            ELSE 'trusted'
                        END,
                        sources_json = CASE
                            WHEN taint_states.max_taint = 'untrusted'
                                 AND EXCLUDED.max_taint <> 'untrusted'
                            THEN taint_states.sources_json
                            ELSE EXCLUDED.sources_json
                        END,
                        updated_at = NOW()
                    """
                ),
                {
                    "cid": ledger.conversation_id,
                    "uid": ledger.user_id,
                    "taint": final_max,
                    "sources": json.dumps(list(merged.values()), ensure_ascii=False),
                },
            )
            await session.commit()
        # Never lower the cached max if a mark landed during the commit.
        ledger.max_taint = max_taint(ledger.max_taint, final_max)
        if ledger.version == snapshot_version:
            ledger.dirty = False
    except Exception as exc:
        _warn_once("persist", ledger.conversation_id, exc)


def provenance_enabled() -> bool:
    """P1d artifact provenance follows the global taint rollback switch."""
    return get_config().tma_mode != "off"


async def record_artifact_taint(
    db: Any,
    user_id: str,
    artifact_type: str,
    artifact_id: str,
    taint: str,
    *,
    origin: str = "agent",
    parent_ids: Iterable[str] = (),
    tenant_id: str = "",
) -> None:
    """Upsert per-artifact provenance without ever lowering its taint."""
    from sqlalchemy import text

    if not provenance_enabled():
        return
    uid = str(user_id or "")
    art_type = str(artifact_type or "")
    art_id = str(artifact_id or "")
    if not uid or not art_type or not art_id:
        return
    new_taint = normalise_taint(taint)
    parents = sorted({str(p) for p in parent_ids if str(p)})
    await db.execute(
        text(
            """
            INSERT INTO artifact_taint
                (user_id, artifact_type, artifact_id, tenant_id, taint,
                 origin, parent_ids, updated_at)
            VALUES
                (:uid, :atype, :aid, :tenant, :taint, :origin,
                 CAST(:parents AS JSONB), NOW())
            ON CONFLICT (user_id, artifact_type, artifact_id) DO UPDATE SET
                tenant_id = CASE
                    WHEN EXCLUDED.tenant_id <> '' THEN EXCLUDED.tenant_id
                    ELSE artifact_taint.tenant_id END,
                taint = CASE
                    WHEN artifact_taint.taint = 'untrusted'
                         OR EXCLUDED.taint = 'untrusted' THEN 'untrusted'
                    WHEN artifact_taint.taint = 'agent'
                         OR EXCLUDED.taint = 'agent' THEN 'agent'
                    ELSE 'trusted' END,
                origin = CASE
                    WHEN artifact_taint.origin = 'untrusted_external'
                         OR EXCLUDED.origin = 'untrusted_external'
                    THEN 'untrusted_external'
                    WHEN artifact_taint.origin = 'user'
                         OR EXCLUDED.origin = 'user' THEN 'user'
                    ELSE EXCLUDED.origin END,
                parent_ids = (
                    SELECT COALESCE(jsonb_agg(DISTINCT value), '[]'::jsonb)
                    FROM jsonb_array_elements_text(
                        artifact_taint.parent_ids || EXCLUDED.parent_ids
                    ) AS value
                ),
                updated_at = NOW()
            """
        ),
        {
            "uid": uid,
            "atype": art_type,
            "aid": art_id,
            "tenant": str(tenant_id or ""),
            "taint": new_taint,
            "origin": str(origin or "agent"),
            "parents": json.dumps(parents, ensure_ascii=False),
        },
    )
    await db.commit()


async def get_artifact_taints(
    db: Any,
    user_id: str,
    artifact_type: str,
    artifact_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    from sqlalchemy import text

    if not provenance_enabled():
        return {}
    ids = [str(a) for a in artifact_ids if str(a)]
    if not ids:
        return {}
    params: dict[str, Any] = {
        "uid": str(user_id or ""), "atype": str(artifact_type or ""),
    }
    placeholders = []
    for index, artifact_id in enumerate(ids):
        key = f"a{index}"
        placeholders.append(f":{key}")
        params[key] = artifact_id
    rows = (await db.execute(
        text(
            "SELECT artifact_id, taint, origin, parent_ids FROM artifact_taint "
            "WHERE user_id = :uid AND artifact_type = :atype "
            f"AND artifact_id IN ({', '.join(placeholders)})"
        ),
        params,
    )).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        parents = row[3] or []
        if isinstance(parents, str):
            try:
                parents = json.loads(parents)
            except ValueError:
                parents = []
        result[str(row[0])] = {
            "taint": str(row[1] or TAINT_UNTRUSTED),
            "origin": str(row[2] or "legacy_untrusted"),
            "parent_ids": list(parents) if isinstance(parents, list) else [],
        }
    return result


def _notes_result_taint(
    result_text: str, input_taint: str, action: str = "read",
) -> str:
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return TAINT_UNTRUSTED
    if not isinstance(payload, dict):
        return TAINT_UNTRUSTED
    notes = payload.get("notes")
    if isinstance(notes, list):
        items = notes
    elif payload.get("id"):
        items = [payload]
    else:
        # A malformed read fails closed; a write/delete confirmation does not.
        if action in {"list_notes", "get_note"}:
            return TAINT_UNTRUSTED
        return max_taint(input_taint, TAINT_AGENT)
    if not items:
        return max_taint(input_taint, TAINT_AGENT)
    taints = [normalise_taint(item.get("taint") or TAINT_UNTRUSTED)
              for item in items if isinstance(item, dict)]
    if len(taints) != len(items) or any(t == TAINT_UNTRUSTED for t in taints):
        return TAINT_UNTRUSTED
    return max_taint(input_taint, TAINT_AGENT)


def _session_search_result_taint(result_text: str, input_taint: str) -> str:
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return TAINT_UNTRUSTED
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return max_taint(input_taint, TAINT_AGENT)
    if not results:
        return max_taint(input_taint, TAINT_AGENT)
    for item in results:
        if not isinstance(item, dict) or "taint" not in item:
            return TAINT_UNTRUSTED
        if normalise_taint(item.get("taint")) == TAINT_UNTRUSTED:
            return TAINT_UNTRUSTED
    return max_taint(input_taint, TAINT_AGENT)


def classify_tool_result(
    tool_name: str,
    result_text: str,
    *,
    input_taint: str = TAINT_AGENT,
    trusted_tools: Iterable[str] = (),
    tool_args: dict | None = None,
) -> str:
    """Taint of a tool's output, at the call boundary (M2 L-b).

    Unknown tools fail closed to ``untrusted``: a new content-bearing tool must
    opt in to agent output explicitly.  Only an allow-listed tool with
    ALL-trusted inputs may output ``trusted``.
    """
    name = str(tool_name or "")
    base = normalise_taint(input_taint)
    allow = {str(x) for x in trusted_tools}

    if name == "memory":
        return _memory_result_taint(result_text, base)
    if not provenance_enabled() and name in {"notes", "session_search"}:
        return max_taint(base, TAINT_AGENT)
    if name == "notes":
        action = str((tool_args or {}).get("action") or "read").lower()
        if action in _NOTES_WRITE_ACTIONS:
            return max_taint(base, TAINT_AGENT)
        return _notes_result_taint(result_text, base, action=action)
    if name == "session_search":
        return _session_search_result_taint(result_text, base)
    if name in allow and base == TAINT_TRUSTED:
        return TAINT_TRUSTED
    if name in _AGENT_TOOLS:
        return max_taint(base, TAINT_AGENT)
    if name in _RETRIEVAL_TOOLS or any(name.startswith(p) for p in _RETRIEVAL_PREFIXES):
        return TAINT_UNTRUSTED
    return TAINT_UNTRUSTED


def _memory_result_taint(result_text: str, input_taint: str) -> str:
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        # A plain-text memory read is still a tool echo: cap at agent.
        return max_taint(input_taint, TAINT_AGENT)
    if not isinstance(payload, dict):
        return max_taint(input_taint, TAINT_AGENT)
    taints: list[str] = []
    for meta in payload.get("entries_meta") or []:
        if isinstance(meta, dict) and meta.get("taint"):
            taints.append(str(meta["taint"]))
    if payload.get("quarantined_entries"):
        taints.append(TAINT_UNTRUSTED)
    if not taints:
        # Empty read/write confirmation: no new information.  A tool echo can
        # never mint trusted authority (M2 L-b): cap it at agent.
        return max_taint(input_taint, TAINT_AGENT)
    if any(normalise_taint(t) == TAINT_UNTRUSTED for t in taints):
        return TAINT_UNTRUSTED
    # Even a user-authored entry read through a tool is a tool echo; keep the
    # conversation at agent, never trusted, so it cannot corroborate an act.
    return max_taint(input_taint, TAINT_AGENT)
