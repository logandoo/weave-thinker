# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Chat citation rendering/sanitizing (P5.3 move-only, 2026-10-18).

Moved verbatim from ``app/api/chat.py``; the endpoint module re-exports the
same callables so call sites, monkeypatches and tests are unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.services.citation_ledger import CitationLedger, strip_cite_markers

logger = logging.getLogger(__name__)


def _write_back_surfaces(
    content: str,
    tool_results_json: str | None,
    tr_obj: dict | None,
    surfaces: list[str],
    sanitized: list[str],
) -> tuple[str, str | None]:
    """Write sanitized surfaces back to ``content`` + ``tool_results_json``.

    ``surfaces``/``sanitized`` share the same order: content first, then
    ``content_segments`` strings, then ``display_sequence`` text items.
    """
    new_content = sanitized[0]
    if tr_obj is None:
        return new_content, tool_results_json
    _changed = new_content != surfaces[0]
    _idx = 1
    _segs = tr_obj.get("content_segments")
    if isinstance(_segs, list):
        for i, s in enumerate(_segs):
            if isinstance(s, str):
                if sanitized[_idx] != s:
                    _segs[i] = sanitized[_idx]
                    _changed = True
                _idx += 1
    _ds = tr_obj.get("display_sequence")
    if isinstance(_ds, list):
        for it in _ds:
            if isinstance(it, dict) and it.get("type") == "text" and isinstance(it.get("content"), str):
                if sanitized[_idx] != it["content"]:
                    it["content"] = sanitized[_idx]
                    _changed = True
                _idx += 1
    return new_content, (
        json.dumps(tr_obj, ensure_ascii=False) if _changed else tool_results_json
    )


async def _render_answer_citations(
    content: str,
    tool_results_json: str | None,
    style_id: str | None = None,
) -> tuple[str, str | None]:
    """M4：终稿出货时产出 citations 载荷（服务端样式化 reference + in_text 映射）。

    - content 保持 [N] 撰写语法（ADR D-1：neutralize_historical_citations /
      sanitize / 前端重排链路不变）；载荷携带逐条规范 reference 与文内族形态
      （由 render_citations 的 CSL 输出提取），前端按载荷展示（卡片=reference、
      四族角标），杜绝双重重排。
    - style_id 取助手级 citation_style → 全局默认（resolve_style_id）。
    - 零引用不产出载荷（F3）；tr_json 已有 citations 时直接返回（幂等，防
      多路径重复渲染）。
    """
    if not tool_results_json:
        return content, tool_results_json
    try:
        tr_obj = json.loads(tool_results_json)
    except (json.JSONDecodeError, TypeError):
        return content, tool_results_json
    if not isinstance(tr_obj, dict) or tr_obj.get("citations"):
        return content, tool_results_json
    results = tr_obj.get("results") or []
    if not isinstance(results, list) or not results:
        return content, tool_results_json

    def _sync() -> tuple[str, str | None]:
        from app.services.citation_style_service import (
            STYLE_REGISTRY,
            render_citations,
            render_entry_references,
            resolve_style_id,
        )

        sid = resolve_style_id(style_id)

        def _id_key(v):
            # A4.9 ⑤：render_citations 接受数字字符串 id（_is_int_id）——载荷键
            # 与之对齐，避免 string id 条目被静默丢弃。
            if isinstance(v, str) and v.isdigit():
                return int(v)
            return v

        # C4（遗留清零）：重复 id（畸形数据）首见胜——防 last-wins 串用他人
        # reference；生产 _transform 顺序编号不可能重复，此为防御层。
        _seen_ids: set = set()
        deduped: list = []
        for _e in results:
            if not isinstance(_e, dict):
                continue
            _k = _id_key(_e.get("id"))
            if _k is None:
                continue
            if _k in _seen_ids:
                logger.warning(
                    "answer citations: duplicate tool_results id %r — keeping first entry", _k,
                )
                continue
            _seen_ids.add(_k)
            deduped.append(_e)
        if not deduped:
            return content, tool_results_json

        rendered = render_citations(content or "", deduped, style_id=sid)
        used = rendered.used_ids
        if not used:
            return content, tool_results_json
        by_id = {
            _id_key(e.get("id")): e
            for e in deduped
            if isinstance(e, dict) and e.get("id") is not None
        }
        ordered = [by_id[i] for i in used if i in by_id]
        refs = render_entry_references(ordered, style_id=sid)
        ref_by_id = {_id_key(r.get("id")): r for r in refs}
        entries = []
        for idx, e in enumerate(ordered):
            eid = _id_key(e.get("id"))
            ref = ref_by_id.get(eid) or {}
            entries.append({
                "id": eid,
                "num": idx + 1,
                "in_text": rendered.old_to_new.get(eid) or "",
                "reference": ref.get("reference") or "",
                "url": e.get("url") or "",
                "title": e.get("title") or "",
                "snippet": (e.get("snippet") or "")[:300],
                "author": e.get("author") or "",
                "year": str(e.get("year") or ""),
            })
        tr_obj["citations"] = {
            "style_id": sid,
            "family": rendered.family,
            "heading": (STYLE_REGISTRY.get(sid) or {}).get("heading") or "参考文献",
            "entries": entries,
        }
        return content, json.dumps(tr_obj, ensure_ascii=False)

    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        logger.exception("answer citation payload failed (keeping original)")
        return content, tool_results_json


def _build_content_fallback(joined_content: str, tool_results_json: str | None) -> str:
    if joined_content:
        return joined_content
    if not tool_results_json:
        return ""
    try:
        tr_obj = json.loads(tool_results_json)
        results = tr_obj.get("results") or []
        parts = []
        for r in results:
            result_text = r.get("result") or ""
            if not isinstance(result_text, str) or not result_text.strip():
                continue
            first_line = result_text.strip().split("\n")[0][:300]
            if first_line:
                parts.append(first_line)
        if parts:
            return "\n\n".join(parts)
    except Exception:
        pass
    return ""


def strip_stop_citation_echo(
    content: str,
    raw_tool_results: list | None,
    search_queries_used: list | None = None,
    queries_by_call: dict | None = None,
) -> str:
    """Stop-path citation cleanup WITHOUT the async LLM judge.

    Rebuilds the turn ledger from the raw tool results (the AgentLoopState
    ledger is local to the loop) and applies the deterministic sanitize +
    trailing-manifest strip.  A stop must not await a 60s disambiguation call.
    """
    if not content or not raw_tool_results:
        return content
    try:
        from app.api.chat import _transform_tool_loop_results
        from app.services.citation_ledger import CitationLedger

        # Transform first: it recovers <tool-digest> envelopes via the archive
        # path, so a digested 21-hit search still yields a ledger (review R1
        # round-4 Important).  Then register the flattened results[] exactly
        # as _sanitize_cited_content does.
        tr_json = _transform_tool_loop_results(
            raw_tool_results,
            search_queries_used=search_queries_used or None,
            queries_by_call=queries_by_call or None,
        )
        if not tr_json:
            return content
        obj = json.loads(tr_json)
        ledger = CitationLedger()
        for r in obj.get("results") or []:
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
        if ledger.size <= 0:
            return content
        return ledger.strip_trailing_manifest_echo(ledger.sanitize(content))
    except Exception:
        logger.exception("stop-path citation sanitize failed; keeping original")
        return content


async def _sanitize_cited_content(
    content: str,
    tool_results_json: str | None,
) -> tuple[str, str | None]:
    """Remove [N] citations that reference no real fetched source before the
    content is persisted. Grounded-citations port (hermes-agent verify step):
    every [N] in the final answer must exist in the turn's citation ledger
    (registered from the actual web_search tool results). Fabricated markers
    are removed with LLM-judged enumeration protection (see
    CitationLedger.sanitize_texts — [3]个要点 must survive).

    Sanitizes EVERY persisted text surface: the message ``content`` AND the
    ``content_segments``/``display_sequence`` text items inside
    ``tool_results_json`` (the frontend renders the display_sequence, so a
    content-only sanitize left dead [25]-style markers visible — conv
    8629bdfe). Returns ``(content, tool_results_json)`` — both sanitized.

    Operates on the TRANSFORMED tool_results JSON (whose results[] is
    deduped by normalized URL in ledger order), so id alignment is
    guaranteed: results[id-1] == the URL the model's [id] cites.
    """
    if not content and not tool_results_json:
        return content, tool_results_json
    try:
        tr_obj = None
        if tool_results_json:
            tr_obj = json.loads(tool_results_json)
        # Raw tool-result lists (stop path/legacy) have no surface map; keep
        # them untouched instead of raising AttributeError in the sanitizer.
        if tr_obj is not None and not isinstance(tr_obj, dict):
            return content, tool_results_json
        results = (tr_obj or {}).get("results") or []
        if not isinstance(results, list):
            results = []
        # conv 3b58af5b (R3b): the old guard early-returned whenever results
        # was empty — exactly the search-engine-failed case, leaving dangling
        # [N] markers the frontend cannot resolve. The docstring contract is
        # "strip [N] when any search ran": activity is now detected from
        # agent_steps too, and an EMPTY ledger proceeds so every [N] marker
        # goes through the enumeration disambiguation and true citations are
        # stripped. A turn with no tool activity at all stays untouched.
        has_activity = bool(results) or bool((tr_obj or {}).get("agent_steps"))

        # Collect every text surface in one shot, keeping the SAME order as
        # the write-back helper (content first, then content_segments, then
        # display_sequence text items).
        surfaces: list[str] = [content]
        if tr_obj:
            _segs = tr_obj.get("content_segments")
            if isinstance(_segs, list):
                surfaces.extend(s for s in _segs if isinstance(s, str))
            _ds = tr_obj.get("display_sequence")
            if isinstance(_ds, list):
                surfaces.extend(
                    it.get("content") for it in _ds
                    if isinstance(it, dict) and it.get("type") == "text"
                    and isinstance(it.get("content"), str)
                )
        if not has_activity:
            # 2026-10-22 遗留收口（A4.9 A-Minor-1）：零工具活动 = 无台账 →
            # 任何 [cite:…] 都是伪造引用，且没有可保留的合法编号——整枚剥除
            # （围栏/行内代码不动；普通 [N] 枚举保持既有「无活动不触碰」语义）。
            stripped = [strip_cite_markers(t) for t in surfaces]
            if stripped == surfaces:
                return content, tool_results_json
            return _write_back_surfaces(
                content, tool_results_json, tr_obj, surfaces, stripped)
        ledger = CitationLedger()
        # A4.9 Important-4（引文改造 M4）：无 URL 条目（古籍/书籍）必须同序注册，
        # 否则 ledger.size < len(results)、[N] 校验错位（合法引用被剥/越界误判）。
        for r in results:
            if not isinstance(r, dict):
                continue
            ledger.register(
                r.get("url") or "",
                title=r.get("title") or "",
                snippet=r.get("snippet") or "",
                author=r.get("author") or None,
                year=str(r.get("year") or "") or None,
                # C1：与入账侧同参派生键（多卷/多版本不合并、位置不位移）
                edition=r.get("edition") or None,
                volume=r.get("volume") or None,
                container=r.get("container") or None,
            )

        # Collect every text surface in one shot, keeping the SAME order as
        # the write-back helper (content first, then content_segments, then
        # display_sequence text items).
        sanitized = await ledger.sanitize_texts(surfaces)
        # P0 R1 (conv 08249cf0): the model copied the synthetic system
        # manifest into the answer tail.  Strip a trailing ledger echo from
        # every persisted surface BEFORE write-back; inline [N] citations and
        # the structured ``citations`` payload are untouched.  Deterministic
        # (no keyword/semantic classifier), so it runs even when the LLM
        # disambiguation call fails.
        sanitized = [ledger.strip_trailing_manifest_echo(t) for t in sanitized]
        return _write_back_surfaces(
            content, tool_results_json, tr_obj, surfaces, sanitized)
    except Exception:
        logger.exception("citation sanitize failed (keeping original)")
        return content, tool_results_json
