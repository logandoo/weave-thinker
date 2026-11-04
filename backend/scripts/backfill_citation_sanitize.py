#!/usr/bin/env python3
# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Backfill: sanitize persisted assistant messages whose tool_results JSON
carries dead [N] citation markers (content_segments / display_sequence text
surfaces were NOT sanitized before 2026-08-16 — conv 8629bdfe [25]/[27]).

Before the fix, _sanitize_cited_content stripped out-of-range [N] from
`messages.content` but persisted tool_results JSON unchanged, and the
frontend renders display_sequence text items — so dead markers stayed
visible. This script rebuilds the ledger per message and re-runs the same
sanitize (LLM-judged enumeration protection) over content + segments.

Usage (from backend/):
    python -m scripts.backfill_citation_sanitize [--execute] [--limit N]
Default is dry-run; --execute applies. Logs to ../tests/backfill_citation_sanitize.log
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("backfill_citation_sanitize")

from sqlalchemy import select  # noqa: E402

from app.db.database import AsyncSessionLocal, Message  # noqa: E402

LOG_PATH = Path(__file__).resolve().parents[2] / "tests" / "backfill_citation_sanitize.log"


async def _load_messages(limit: int | None):
    """Messages with a results-bearing tool_results JSON that ALSO carries
    content_segments or display_sequence text (the surfaces that were never
    sanitized)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Message)
            .where(Message.role == "assistant", Message.tool_results.isnot(None))
            .order_by(Message.created_at.desc())
            .limit(limit or 2000)
        )
        return list(result.scalars().all())


def _candidate(msg) -> tuple[dict | None, list[str]]:
    """Return (tr_obj, surfaces) when the message carries search results;
    surfaces may be empty (content-only dirty messages from the legacy
    resume path still get sanitized). None otherwise."""
    try:
        tr_obj = json.loads(msg.tool_results or "")
    except (json.JSONDecodeError, TypeError):
        return None, []
    results = tr_obj.get("results") or []
    if not isinstance(results, list):
        return None, []
    # re-review Minor-1/3：results 为空但回合有工具活动（agent_steps）也要放行；
    # 门禁与 _sanitize_cited_content 的 has_activity（results | agent_steps）对齐
    # ——search_failed 单独不构成活动（那里不看它）。
    if not results and not tr_obj.get("agent_steps"):
        return None, []
    surfaces: list[str] = []
    segs = tr_obj.get("content_segments")
    if isinstance(segs, list):
        surfaces.extend(s for s in segs if isinstance(s, str))
    ds = tr_obj.get("display_sequence")
    if isinstance(ds, list):
        surfaces.extend(
            it.get("content") for it in ds
            if isinstance(it, dict) and it.get("type") == "text"
            and isinstance(it.get("content"), str)
        )
    return tr_obj, surfaces


def _ledger_for(tr_obj: dict):
    """Rebuild the turn ledger from a persisted tool_results results[] list.

    逐条注册（含无 URL 的古籍/书籍条目）——与 ``_sanitize_cited_content`` 同口径；
    此前经 ``build_ledger_from_tool_results`` 的 register_hits 路径会丢弃无 URL
    命中（re-review Minor-2：纯书籍会话 ledger.size=0 被早退跳过、永不愈合）。
    """
    from app.services.citation_ledger import CitationLedger

    ledger = CitationLedger()
    for r in tr_obj.get("results") or []:
        if not isinstance(r, dict):
            continue
        ledger.register(
            r.get("url") or "",
            title=r.get("title") or "",
            snippet=r.get("snippet") or "",
            author=r.get("author") or None,
            year=str(r.get("year") or "") or None,
            edition=r.get("edition") or None,
            volume=r.get("volume") or None,
            container=r.get("container") or None,
        )
    return ledger


def _dirty_surfaces(content: str | None, surfaces: list[str], ledger) -> bool:
    """Rewrite predicate: unknown [N] OR any surface still carrying a
    model-native ``[cite:N,…]`` marker.

    Normalize arm applies ``normalize_cite_markers``'s own contract: ``[cite:N]``
    in prose (NOT the deliberate ``[cite:N](`` / ``[cite:N]:`` link/label
    shapes, and not fenced/inline code) counts as dirty.

    2026-10-22 (60 conv 5c89e092): the original predicate only looked at
    ``verify().unknown`` — after ``normalize_cite_markers`` landed, a legal
    ``[cite:10]`` normalizes to a CITED id (not unknown), so legacy rows with
    raw markers read as clean and were skipped. The normalize-delta arm heals
    the already-persisted incident shapes (valid ids included)."""
    from app.services.citation_ledger import normalize_cite_markers

    if any(normalize_cite_markers(s) != s for s in surfaces):
        return True
    if normalize_cite_markers(content or "") != (content or ""):
        return True
    return any(ledger.verify(s).unknown for s in surfaces) or bool(
        ledger.verify(content or "").unknown)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="apply changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="max messages to scan")
    args = ap.parse_args()
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"模式: {mode}")
    print("=" * 60)

    os.makedirs(LOG_PATH.parent, exist_ok=True)
    logfh = open(LOG_PATH, "a", encoding="utf-8")
    logfh.write(f"\n--- backfill_citation_sanitize {datetime.now(timezone.utc).isoformat()} {mode} ---\n")

    from app.api.chat import _sanitize_cited_content

    messages = await _load_messages(args.limit)
    print(f"扫描 {len(messages)} 条 assistant 消息 (tool_results 非空)")
    logfh.write(f"scanned={len(messages)}\n")

    changed = 0
    for msg in messages:
        tr_obj, surfaces = _candidate(msg)
        if tr_obj is None:
            continue
        # Fast path: skip when every surface is already citation-clean
        ledger = _ledger_for(tr_obj)
        # 空台账不早退：_dirty_surfaces 对空台账安全（normalize 臂与 unknown 臂
        # 均可判定），下游 _sanitize_cited_content 自带 has_activity 守卫。
        if not _dirty_surfaces(msg.content, surfaces, ledger):
            continue

        new_content, new_tr_json = await _sanitize_cited_content(
            msg.content or "", msg.tool_results,
        )
        if new_content == (msg.content or "") and new_tr_json == msg.tool_results:
            continue
        changed += 1
        if args.execute:
            msg.content = new_content
            msg.tool_results = new_tr_json
            async with AsyncSessionLocal() as db:
                await db.merge(msg)
                await db.commit()
        print(f"{'[FIX]' if args.execute else '[DRY]'} msg {msg.id} conv {msg.conversation_id}")
        logfh.write(f"changed msg={msg.id} conv={msg.conversation_id}\n")

    logfh.write(f"changed_total={changed}\n")
    logfh.close()
    print(f"需要修复的消息: {changed} (mode={mode})")
    print(f"日志: {LOG_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
