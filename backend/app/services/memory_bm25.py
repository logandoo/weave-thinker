# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import math
from collections import defaultdict

logger = logging.getLogger(__name__)

try:
    import jieba
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False
    logger.warning("jieba not installed; falling back to bigram tokenizer for BM25")


def _tokenize(text: str, method: str = "jieba") -> list[str]:
    if method == "jieba" and _JIEBA_AVAILABLE:
        return [w.strip() for w in jieba.cut(text) if w.strip()]
    return _bigram_tokenize(text)


def _bigram_tokenize(text: str) -> list[str]:
    tokens = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if '\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff':
            if i + 1 < len(text):
                nxt = text[i + 1]
                if ('\u4e00' <= nxt <= '\u9fff' or '\u3040' <= nxt <= '\u30ff'):
                    tokens.append(ch + nxt)
                    i += 2
                    continue
            tokens.append(ch)
            i += 1
        elif ch.isalpha() or ch.isdigit():
            start = i
            while i < len(text) and (text[i].isalpha() or text[i].isdigit() or text[i] in '._-'):
                i += 1
            tokens.append(text[start:i].lower())
        else:
            i += 1
    return tokens


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75, tokenizer: str = "jieba"):
        self.k1 = k1
        self.b = b
        self.tokenizer = tokenizer
        self.docs: dict[str, str] = {}
        self.doc_lens: dict[str, int] = {}
        self.doc_tokens: dict[str, list[str]] = {}
        self.avg_doc_len: float = 0.0
        self.inverted: dict[str, dict[str, int]] = defaultdict(dict)
        self.df: dict[str, int] = defaultdict(int)
        self._total_docs = 0

    def add_doc(self, doc_id: str, text: str) -> None:
        if not text:
            return
        self._remove_doc_internal(doc_id)
        tokens = _tokenize(text, self.tokenizer)
        self.docs[doc_id] = text
        self.doc_tokens[doc_id] = tokens
        self.doc_lens[doc_id] = len(tokens)
        self._total_docs = len(self.docs)
        self.avg_doc_len = sum(self.doc_lens.values()) / max(self._total_docs, 1)
        for token in set(tokens):
            tf = tokens.count(token)
            self.inverted[token][doc_id] = tf
            self.df[token] += 1

    def update_doc(self, doc_id: str, text: str) -> None:
        self.add_doc(doc_id, text)

    def remove_doc(self, doc_id: str) -> None:
        self._remove_doc_internal(doc_id)

    def _remove_doc_internal(self, doc_id: str) -> None:
        if doc_id not in self.docs:
            return
        old_tokens = self.doc_tokens.get(doc_id, [])
        del self.docs[doc_id]
        self.doc_tokens.pop(doc_id, None)
        self.doc_lens.pop(doc_id, None)
        self._total_docs -= 1
        self.avg_doc_len = sum(self.doc_lens.values()) / max(self._total_docs, 1)
        for token in set(old_tokens):
            self.inverted[token].pop(doc_id, None)
            if not self.inverted[token]:
                del self.inverted[token]
            self.df[token] -= 1
            if self.df[token] <= 0:
                del self.df[token]

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        if not self.docs:
            return []
        query_tokens = _tokenize(query, self.tokenizer)
        if not query_tokens:
            return []
        scores: dict[str, float] = defaultdict(float)
        for token in query_tokens:
            if token not in self.inverted:
                continue
            idf = math.log((self._total_docs - self.df[token] + 0.5) / (self.df[token] + 0.5) + 1.0)
            for doc_id, tf in self.inverted[token].items():
                doc_len = self.doc_lens.get(doc_id, 1)
                tf_norm = (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * doc_len / max(self.avg_doc_len, 1)))
                scores[doc_id] += idf * tf_norm
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_scores[:k]


_name_indexes: dict[str, BM25Index] = {}
_desc_indexes: dict[str, BM25Index] = {}
_epi_indexes: dict[str, BM25Index] = {}
_sub_indexes: dict[str, BM25Index] = {}


async def get_name_index(db, user_id: str) -> BM25Index:
    if user_id not in _name_indexes:
        idx = BM25Index()
        _name_indexes[user_id] = idx
        await _build_concept_name_index_from_db(db, user_id, idx)
    return _name_indexes[user_id]


async def get_desc_index(db, user_id: str) -> BM25Index:
    if user_id not in _desc_indexes:
        idx = BM25Index()
        _desc_indexes[user_id] = idx
        await _build_concept_desc_index_from_db(db, user_id, idx)
    return _desc_indexes[user_id]


async def get_epi_index(db, user_id: str) -> BM25Index:
    if user_id not in _epi_indexes:
        idx = BM25Index()
        _epi_indexes[user_id] = idx
        await _build_episode_index_from_db(db, user_id, idx)
    return _epi_indexes[user_id]


async def get_sub_index(db, user_id: str) -> BM25Index:
    if user_id not in _sub_indexes:
        idx = BM25Index()
        _sub_indexes[user_id] = idx
        await _build_subconscious_index_from_db(db, user_id, idx)
    return _sub_indexes[user_id]


async def _build_concept_name_index_from_db(db, user_id: str, idx: BM25Index) -> None:
    from sqlalchemy import text
    # 含已失效 + cold_forgotten 概念：as-of 历史查询（§5.2，不限制 status）需检索；
    # 常规查询由 retrieval 层 status/valid_to 过滤
    result = await db.execute(
        text("SELECT id, canonical_name, aliases FROM memory_concepts WHERE user_id = :uid"),
        {"uid": user_id},
    )
    for row in result.fetchall():
        cid, name, aliases_raw = row[0], row[1], row[2]
        text_parts = [name]
        if aliases_raw:
            try:
                import json as _json
                aliases = _json.loads(aliases_raw)
                if isinstance(aliases, list):
                    text_parts.extend(str(a) for a in aliases)
            except Exception:
                pass
        idx.add_doc(cid, " ".join(text_parts))


async def _build_concept_desc_index_from_db(db, user_id: str, idx: BM25Index) -> None:
    from sqlalchemy import text
    # 同 name 索引：含已失效 + cold_forgotten 供 as-of 查询；常规过滤在 retrieval 层
    result = await db.execute(
        text("SELECT id, description_full FROM memory_concepts WHERE user_id = :uid"),
        {"uid": user_id},
    )
    for row in result.fetchall():
        cid, desc = row[0], row[1]
        if desc:
            idx.add_doc(cid, desc)


async def _build_episode_index_from_db(db, user_id: str, idx: BM25Index) -> None:
    from sqlalchemy import text
    # 含已失效 episode：as-of 历史查询（§5.2）需检索；常规过滤在 retrieval 层
    result = await db.execute(
        text("SELECT id, narrative FROM memory_episodes WHERE user_id = :uid"),
        {"uid": user_id},
    )
    for row in result.fetchall():
        eid, narrative = row[0], row[1]
        if narrative:
            idx.add_doc(eid, narrative)


async def _build_subconscious_index_from_db(db, user_id: str, idx: BM25Index) -> None:
    from sqlalchemy import text
    result = await db.execute(
        text("SELECT id, raw_text FROM subconscious_log WHERE user_id = :uid AND embedding IS NOT NULL AND created_at >= now() - interval '30 days'"),
        {"uid": user_id},
    )
    for row in result.fetchall():
        sid, raw = row[0], row[1]
        if raw:
            idx.add_doc(sid, raw[:1000])
