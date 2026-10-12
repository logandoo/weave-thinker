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


# 台账条目书目字段（M1 升格，全部 Optional，向后兼容——空值不写入条目）。
BIBLIO_FIELDS = (
    "type", "source_type", "author", "editor", "edition", "publisher_place",
    "publisher", "year", "volume", "page", "container", "genre", "standard_no",
)


_MANIFEST_WS_RE = re.compile(r"\s+")
# C2（遗留清零）：仅中和**空白定界**的 `[N]` token（伪条目前缀形，如 " [99] 恶意"）；
# 正文式方括号数字（图[3]、[123]页、[3]个要点）保持原样（外观保真）。
_MANIFEST_BRACKET_RE = re.compile(r"(?<!\S)\[(\d{1,3})\](?=\s|$)")


def sanitize_manifest_text(value: Any, cap: int = 50) -> str:
    """manifest/映射表字段消毒：换行折叠为空格；`[N]` 中和为全角括号
    （不可模仿为引用标记——与 neutralize 同一思路），防宿主网页向折叠保护的
    system 消息注入伪造条目行（A4.9 B3）。"""
    v = _MANIFEST_WS_RE.sub(" ", str(value or "")).strip()
    v = _MANIFEST_BRACKET_RE.sub(r"（\1）", v)
    return v[:cap]


def ledger_key(
    url: str,
    title: str = "",
    author: str = "",
    year: str = "",
    edition: str = "",
    volume: str = "",
    container: str = "",
) -> str:
    """台账身份键：URL 存在 → normalize_url(url)；无 URL → title+author+year+
    edition+volume+container 派生（M1：古籍/书籍类来源可无链接，仍须可去重；
    C1 遗留清零：同题的**不同卷次/版本/丛书**不得合并）。全空 → ""（调用方不去重）。"""
    u = normalize_url(url)
    if u:
        return u
    t = (title or "").strip()
    a = (author or "").strip()
    y = str(year or "").strip()
    ed = (edition or "").strip()
    vol = (volume or "").strip()
    c = (container or "").strip()
    if not (t or a or y or ed or vol or c):
        return ""
    return "\x00".join(("derived", t, a, y, ed, vol, c))


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
        published_date: Optional[str] = None,
        *,
        type: Optional[str] = None,
        source_type: Optional[str] = None,
        author: Optional[str] = None,
        editor: Optional[str] = None,
        edition: Optional[str] = None,
        publisher_place: Optional[str] = None,
        publisher: Optional[str] = None,
        year: Optional[str] = None,
        volume: Optional[str] = None,
        page: Optional[str] = None,
        container: Optional[str] = None,
        genre: Optional[str] = None,
        standard_no: Optional[str] = None,
        site_name: Optional[str] = None,
    ) -> int:
        """Register a source; returns its turn-global id.

        M1（引文体系改造）：条目升格为文献条目——URL 存在时键=normalize_url；
        无 URL（古籍/书籍）用 title+author+year 派生键（ledger_key）。全部书目
        字段 Optional，空值不写入（向后兼容旧条目）。"""
        biblio = {
            "type": type,
            "source_type": source_type, "author": author, "editor": editor,
            "edition": edition, "publisher_place": publisher_place,
            "publisher": publisher, "year": year, "volume": volume,
            "page": page, "container": container, "genre": genre,
            "standard_no": standard_no, "site_name": site_name,
        }
        biblio = {k: str(v).strip() for k, v in biblio.items() if v and str(v).strip()}
        key = ledger_key(
            url,
            title=title,
            author=biblio.get("author", ""),
            year=biblio.get("year", ""),
            edition=biblio.get("edition", ""),
            volume=biblio.get("volume", ""),
            container=biblio.get("container", ""),
        )
        if key:
            existing = self.url_to_id.get(key)
            if existing is not None:
                entry = self.entries[existing - 1]
                if title and not entry.get("title"):
                    entry["title"] = title.strip()
                cleaned = (published_date or "").strip()
                if cleaned and not entry.get("published_date"):
                    entry["published_date"] = cleaned
                for k, v in biblio.items():
                    if v and not entry.get(k):
                        entry[k] = v
                return existing
        entry_id = len(self.entries) + 1
        entry: Dict[str, Any] = {
            "id": entry_id,
            "url": normalize_url(url),
            "title": (title or "").strip(),
            "snippet": (snippet or "").strip(),
            "query": query,
            "published_date": (published_date or "").strip(),
        }
        entry.update(biblio)
        self.entries.append(entry)
        if key:
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
            extra = {
                k: h.get(k) for k in BIBLIO_FIELDS if h.get(k)
            }
            ids.append(self.register(
                url,
                title=h.get("title") or "",
                snippet=h.get("snippet") or "",
                query=query,
                published_date=h.get("published_date") or "",
                site_name=h.get("site_name") or None,
                **extra,
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
            line = f"{entry['id']}. {entry['title']}\nURL: {entry['url']}\n"
            if entry.get("published_date"):
                line += f"发布日期: {entry['published_date']}\n"
            line += f"摘要: {entry['snippet']}"
            lines.append(line)
        return "\n\n".join(lines)

    def format_manifest(self, max_entries: int = 120) -> str:
        """紧凑引用台账块（M5 免折叠 manifest）。

        每条 `[N] 标题 — 作者,年 | 卷页`——约 75 条约 3KB；由 agent_loop 在
        台账增长/压缩后重注入并要求折叠保护（编号可见性与检索深度解耦）。
        字段经 sanitize_manifest_text（换行折叠 + 方括号编号中和）——hostile
        网页元数据不得向折叠保护的 system 消息注入伪造条目行（A4.9 B3）。
        """
        if not self.entries:
            return ""
        lines = ["【引用台账（系统维护）——本轮 [N] 编号对应下表；引用时使用表内编号，禁止编造】"]
        for e in self.entries[:max_entries]:
            title = sanitize_manifest_text(
                e.get("title") or e.get("container") or e.get("url") or "未题名", cap=50,
            )
            author = sanitize_manifest_text(e.get("author") or "", cap=30)
            year = sanitize_manifest_text(
                str(e.get("year") or "").strip() or (e.get("published_date") or "").strip()[:4],
                cap=10,
            )
            head = f"[{e.get('id')}] {title}"
            who = ",".join(x for x in (author, year) if x)
            if who:
                head += f" — {who}"
            loc = " ".join(
                sanitize_manifest_text(x, cap=20)
                for x in (e.get("volume"), e.get("page")) if x
            )
            if loc:
                head += f" | {loc}"
            lines.append(head)
        if len(self.entries) > max_entries:
            lines.append(f"…（台账共 {len(self.entries)} 条，其余条目仍有效可引用，编号范围 [1]-[{self.size}]）")
        return "\n".join(lines)

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

    async def sanitize_texts(
        self,
        texts: list[str],
        *,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> list[str]:
        """Agentic sanitize across multiple text surfaces sharing ONE ledger.

        The union of unknown [N] ids across all surfaces is LLM-judged ONCE,
        then removed structurally from every surface — so the persisted
        ``content`` and the ``content_segments``/``display_sequence`` copies
        stay mutually consistent (conv 8629bdfe: [25]/[27] survived in
        segments while ``content`` was clean). Enumeration uses ([3]个要点 /
        第[12]条) are preserved; LLM failure → keep ALL unknown markers
        intact (never mangle prose; leftover fabricated citations are
        cosmetic).

        conv a040c24e (2026-09-10): the judge call is bounded by
        ``[agent.citation] disambiguate_timeout_seconds`` (default 30s — the
        old hardcoded 20s timed out under production congestion and let a
        dangling [11] persist) and retried ``disambiguate_retries`` times
        (default 1); total failure keeps the documented fail-open policy.
        """
        texts = [t or "" for t in texts]
        if not any(texts):
            return texts
        unknown: set[int] = set()
        for t in texts:
            unknown |= self.verify(t).unknown
        if not unknown:
            return texts
        if timeout is None or retries is None:
            try:
                from app.core.config import get_config

                _cfg = get_config()
                if timeout is None:
                    timeout = float(getattr(_cfg, "agent_citation_disambiguate_timeout", 60.0))
                if retries is None:
                    retries = int(getattr(_cfg, "agent_citation_disambiguate_retries", 1))
            except Exception:
                timeout = 60.0 if timeout is None else timeout
                retries = 1 if retries is None else retries
        try:
            from app.services.agentic_judge import judge_json

            ids_desc = ", ".join(str(n) for n in sorted(unknown))
            # conv 3b58af5b wave-2: the old 800-char/surface slice hid markers
            # sitting deeper in long answers (e.g. a [14] at char ~1100 was
            # invisible to the judge → fail-open kept a dangling citation).
            # Widen enough to cover typical answer bodies while bounding the
            # prompt for multi-surface turns.
            context = "\n\n---\n\n".join(t[:2400] for t in texts)[:16000]
            parsed = None
            for attempt in range(max(0, retries) + 1):
                parsed = await judge_json(
                    _CITE_DISAMBIGUATE_PROMPT.format(ids=ids_desc),
                    f"文本：\n{context}\n\n只输出JSON。",
                    task="citation_disambiguate",
                    default=None,
                    timeout=timeout,
                )
                if isinstance(parsed, dict):
                    break
                if attempt < max(0, retries):
                    logger.info("citation disambiguation judge failed — retrying (%d/%d)", attempt + 1, max(0, retries))
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
