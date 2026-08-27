# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Turn-level citation ledger — grounded-citations pattern ported from
hermes-agent skills/research/grounded-citations/scripts/sources.py.

The core invariant from the hermes skill: "the model only ever emits small
integers it was handed." The system assigns stable ids to fetched URLs; the
model's [N] markers are verified against the ledger before persistence so a
hallucinated or cross-round-ambiguous citation is mechanically removed.

Why this exists (conv 149ce886 lineage): web_search numbers each round from
1, but multi-round turns flatten into one results[] and the frontend maps
[N] positionally — a [1] meant for round 2's first hit resolves to round
1's first hit (cross-round collision). With a turn-global ledger the
numbering never resets, so [N] is unambiguous; `sanitize` removes
out-of-range/fabricated [N] that the frontend could not catch when ANY
search ran.

Pure stdlib, no I/O — unit-testable (tests/citation_ledger_unit.py).
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# [N] citation marker in prose. The negative lookahead excludes markdown
# links [text](url) and reference-style labels [text]: — same family as the
# hermes _CITE_RE. Whether an unknown [N] is a real citation (remove) or an
# enumeration like "[3]个要点" / "第[12]条" (keep) is judged by an LLM in
# sanitize_async — no measure-word heuristic (agentic principle 2026-07-20).
_CITE_RE = re.compile(r"\[(\d{1,3})\](?![(:])")

# Fenced/inline code — citations inside code are not citations.
_FENCE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")

_CITE_DISAMBIGUATE_PROMPT = (
    "你是引用标记判断器。下面的文本中出现了数字方括号标记 [N]，其中一部分是"
    "引用标记（指向上文列出的来源编号），另一部分是普通序号/枚举（如 '[3]个要点'、"
    "'第[12]条规则'）。\n"
    "待判断的下标（引用账本中不存在的编号）：{ids}\n"
    "请判断这些编号中哪些是**真正的引用标记**（应删除），哪些是**序号枚举**（应保留）。\n"
    '输出JSON：{{"citation_ids": [应删除的编号]}}（只列出应删除的；全部是枚举则输出 []）\n'
    "只输出JSON，不要输出其他内容。"
)


def normalize_url(url: str) -> str:
    """URL normalization for ledger identity: fragment and trailing slash are
    insignificant (same page), query strings are significant (different page).
    Mirrors hermes sources.py normalize_url."""
    u = (url or "").strip()
    if "#" in u:
        u = u.split("#", 1)[0]
    stripped = u.rstrip("/")
    return stripped or u


@dataclass
class CitationReport:
    cited: Set[int] = field(default_factory=set)    # valid ids referenced in prose
    unknown: Set[int] = field(default_factory=set)  # ids referenced but not in ledger
    total: int = 0                                   # ledger size


@dataclass
class CitationLedger:
    """Stable url → id registry for one conversation turn.

    IDs are monotonic (1-based) in first-seen order and never reused for a
    normalized URL — the same URL always returns the same id, so numbering
    stays stable across many search rounds within the turn.
    """

    entries: List[Dict[str, Any]] = field(default_factory=list)
    url_to_id: Dict[str, int] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.entries)

    def register(
        self,
        url: str,
        title: str = "",
        snippet: str = "",
        query: Optional[str] = None,
    ) -> int:
        key = normalize_url(url)
        existing = self.url_to_id.get(key)
        if existing is not None:
            entry = self.entries[existing - 1]
            if title and not entry.get("title"):
                entry["title"] = title
            return existing
        entry_id = len(self.entries) + 1
        self.entries.append({
            "id": entry_id,
            "url": key,
            "title": (title or "").strip(),
            "snippet": (snippet or "").strip(),
            "query": query,
        })
        self.url_to_id[key] = entry_id
        return entry_id

    def register_hits(
        self,
        hits: List[Dict[str, Any]],
        query: Optional[str] = None,
    ) -> List[int]:
        """Register a search-round's hit list, returning the global ids in
        hit order (deduplicated against earlier rounds)."""
        ids: List[int] = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            url = h.get("url") or ""
            if not url:
                continue
            ids.append(self.register(
                url,
                title=h.get("title") or "",
                snippet=h.get("snippet") or "",
                query=query,
            ))
        return ids

    def format_hits(self, ids: List[int]) -> str:
        """Render the numbered list the model sees, using GLOBAL ledger ids
        (replaces the per-round 1-based numbering of format_hits)."""
        if not ids:
            return "未检索到可用网页结果。"
        lines = []
        for entry_id in ids:
            entry = self.entries[entry_id - 1]
            line = f"{entry['id']}. {entry['title']}\nURL: {entry['url']}\n摘要: {entry['snippet']}"
            lines.append(line)
        return "\n\n".join(lines)

    @staticmethod
    def _cited_ids_from_text(text: str) -> Set[int]:
        """Extract [N] markers from prose, ignoring fenced/inline code."""
        if not text:
            return set()
        prose = _FENCE_RE.sub("", text)
        return {int(m) for m in _CITE_RE.findall(prose)}

    def verify(self, text: str) -> CitationReport:
        cited = self._cited_ids_from_text(text)
        report = CitationReport(total=self.size)
        for n in cited:
            if 1 <= n <= self.size and self.entries[n - 1]["id"] == n:
                report.cited.add(n)
            else:
                report.unknown.add(n)
        return report

    def _sanitize_remove_ids(self, text: str, unknown_ids: set[int]) -> str:
        """Structurally remove [N] markers for the given ids, masking code
        fences so their brackets are never touched, then restore."""
        if not text or not unknown_ids:
            return text
        spans = list(_FENCE_RE.finditer(text))
        if spans:
            parts = []
            last = 0
            for i, m in enumerate(spans):
                parts.append(text[last:m.start()])
                parts.append(f"\x00{i}\x00")
                last = m.end()
            parts.append(text[last:])
            masked = "".join(parts)
            masked = _CITE_RE.sub(
                lambda m: "" if int(m.group(1)) in unknown_ids else m.group(0),
                masked,
            )
            for i in range(len(spans) - 1, -1, -1):
                masked = masked.replace("\x00" + str(i) + "\x00", spans[i].group(0))
            return masked
        return _CITE_RE.sub(
            lambda m: "" if int(m.group(1)) in unknown_ids else m.group(0),
            text,
        )

    def sanitize(self, text: str) -> str:
        """Synchronous sanitize (structural only): removes [N] citations that
        do not exist in the ledger. Enumeration protection is LLM-judged in
        ``sanitize_async`` — the runtime caller must use that; this sync
        variant is the deterministic baseline (also used when no unknown
        markers exist)."""
        if not text:
            return text
        report = self.verify(text)
        if not report.unknown:
            return text
        return self._sanitize_remove_ids(text, report.unknown)

    async def sanitize_texts(self, texts: list[str]) -> list[str]:
        """Agentic sanitize across multiple text surfaces sharing ONE ledger.

        The union of unknown [N] ids across all surfaces is LLM-judged ONCE,
        then removed structurally from every surface — so the persisted
        ``content`` and the ``content_segments``/``display_sequence`` copies
        stay mutually consistent (conv 8629bdfe: [25]/[27] survived in
        segments while ``content`` was clean). Enumeration uses ([3]个要点 /
        第[12]条) are preserved; LLM failure → keep ALL unknown markers
        intact (never mangle prose; leftover fabricated citations are
        cosmetic)."""
        texts = [t or "" for t in texts]
        if not any(texts):
            return texts
        unknown: set[int] = set()
        for t in texts:
            unknown |= self.verify(t).unknown
        if not unknown:
            return texts
        try:
            from app.services.agentic_judge import judge_json

            ids_desc = ", ".join(str(n) for n in sorted(unknown))
            # conv 3b58af5b wave-2: the old 800-char/surface slice hid markers
            # sitting deeper in long answers (e.g. a [14] at char ~1100 was
            # invisible to the judge → fail-open kept a dangling citation).
            # Widen enough to cover typical answer bodies while bounding the
            # prompt for multi-surface turns.
            context = "\n\n---\n\n".join(t[:2400] for t in texts)[:16000]
            parsed = await judge_json(
                _CITE_DISAMBIGUATE_PROMPT.format(ids=ids_desc),
                f"文本：\n{context}\n\n只输出JSON。",
                task="citation_disambiguate",
                default=None,
                timeout=20.0,
            )
            if not isinstance(parsed, dict):
                return texts
            remove_ids = {
                int(i) for i in (parsed.get("citation_ids") or [])
                if (isinstance(i, (int, float)) and type(i) is not bool)
                or (isinstance(i, str) and i.isdigit())
            }
            remove_ids &= unknown
            if not remove_ids:
                return texts
            return [self._sanitize_remove_ids(t, remove_ids) for t in texts]
        except Exception as exc:
            logger.warning("citation disambiguation LLM failed: %s", exc)
            return texts

    async def sanitize_async(self, text: str) -> str:
        """Agentic sanitize (single text): delegate to :meth:`sanitize_texts`
        so the one-text and multi-text paths share the identical judgment."""
        if not text:
            return text
        return (await self.sanitize_texts([text]))[0]


def build_ledger_from_tool_results(tool_results: List[Dict[str, Any]]) -> CitationLedger:
    """Reconstruct the turn ledger from accumulated tool results — the same
    deterministic registration the agent loop performs live, so chat.py can
    verify/sanitize the final answer without extra state plumbing."""
    ledger = CitationLedger()
    for tr in tool_results or []:
        if tr.get("name") != "web_search" or tr.get("error"):
            continue
        try:
            payload = json.loads(tr.get("result") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        hits = payload.get("results")
        if not isinstance(hits, list):
            continue
        ledger.register_hits(hits)
    return ledger
