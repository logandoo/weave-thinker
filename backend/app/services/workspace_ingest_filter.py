# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""系统记忆 ingestion 过滤（2026-11-10 波 · T11）。

设计契约 §8.5 —— 可执行的承诺是：**vibeweaver 任务状态、run evidence、助手执行输出
和自动文件元数据绝不自动进入系统记忆；scoped 用户对话只保留经过结构清理的
user-authored 文本通道，且可完全关闭。** 不对用户主动文本做「永不含任务事实」的
语义保证（那是虚假承诺）。

策略按 `Project → Task → workspace_bindings → workspace_message_contexts` 继承并
**逐消息快照**：读取时以消息发生时的快照为准，改绑后的当前 Task 不能反推历史。
没有快照的旧消息 = legacy 原行为，并在统计里显式报告，不伪装成 scoped 零污染。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sqlalchemy import text

logger = logging.getLogger(__name__)

POLICY_NORMAL = "normal"
POLICY_USER_FACTS = "user_facts"
POLICY_EXCLUDE = "exclude"
POLICIES = (POLICY_NORMAL, POLICY_USER_FACTS, POLICY_EXCLUDE)

# 自动附加的结构化元数据形态（保守白名单式剔除；宁可少剔，不可改语义）
_FENCE_BLOCK = re.compile(r"```[\s\S]*?```")
_UPLOAD_PATH = re.compile(r"(?m)^\s*(?:[-*]\s*)?(?:uploads|noteimg|files|artifacts)/[^\s'\"`)]+\s*$")
_ABS_WORKSPACE_PATH = re.compile(r"(?m)^\s*(?:[-*]\s*)?/(?:[^/\s]+/)+[^\s'\"`)]*\.(?:png|jpg|jpeg|gif|webp|pdf|docx?|xlsx?|pptx?|csv|md|txt|json|wav|mp3|mp4)\s*$")
_FILE_REF = re.compile(r"<file\b[^>]*>[\s\S]*?</file>|<file\b[^>]*/>", re.IGNORECASE)
_TOOL_CARD = re.compile(r"(?m)^\s*(?:\[\d+\]\s*)?(?:工具|Tool)[:：].{0,200}$")
_WORKSPACE_JSON = re.compile(r"\{[\s\S]{0,4000}?\"(?:generated_files|tool_results|workspace_path)\"[\s\S]{0,2000}?\}")
_MEMORY_TAG = re.compile(r"<memory\b[^>]*>[\s\S]*?</memory>", re.IGNORECASE)


@dataclass
class IngestStats:
    """backfill / 运行期都要显式报告的面（设计 §8.5 末段）。"""

    kept: int = 0
    dropped_excluded: int = 0
    dropped_untrusted_note: int = 0
    stripped_metadata: int = 0
    legacy_unscopied: int = 0
    by_policy: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept": self.kept,
            "dropped_excluded": self.dropped_excluded,
            "dropped_untrusted_note": self.dropped_untrusted_note,
            "stripped_metadata": self.stripped_metadata,
            "legacy_unscopied": self.legacy_unscopied,
            "by_policy": dict(self.by_policy),
        }


def strip_autometadata(raw: str) -> tuple[str, bool]:
    """结构剔除文件引用块 / 上传路径 / 工具卡片 / 自动 JSON 元数据。

    只做**确定性**剔除，不做语义分类：用户自己写下的目标、进度、事实会保留
    （它们本来就属于用户长期记忆的合法来源）。
    """
    text_in = str(raw or "")
    out = text_in
    out = _MEMORY_TAG.sub(" ", out)
    out = _FILE_REF.sub(" ", out)
    out = _WORKSPACE_JSON.sub(" ", out)
    out = _TOOL_CARD.sub(" ", out)
    out = _UPLOAD_PATH.sub(" ", out)
    out = _ABS_WORKSPACE_PATH.sub(" ", out)
    out = _FENCE_BLOCK.sub(" ", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, out != text_in.strip()


async def policies_for_messages(db, message_ids: Sequence[str]) -> dict[str, str | None]:
    """逐消息策略快照；未快照（legacy 消息）→ None。"""
    ids = [str(m) for m in message_ids if m]
    if not ids:
        return {}
    placeholders = []
    params: dict[str, Any] = {}
    for i, mid in enumerate(ids):
        key = f"m{i}"
        placeholders.append(f":{key}")
        params[key] = mid
    res = await db.execute(
        text("SELECT message_id, system_memory_ingest FROM workspace_message_contexts "
             f"WHERE message_id IN ({', '.join(placeholders)})"),
        params,
    )
    found = {str(r[0]): (str(r[1]) if r[1] else None) for r in res.fetchall()}
    return {mid: found.get(mid) for mid in ids}


async def note_policies(db, user_id: str, note_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
    """任务关联笔记的 (policy, origin)；未关联任何 Task 的笔记不在结果里
    （它们是独立的用户级来源，不受 Task 开关控制）。"""
    ids = [str(n) for n in note_ids if n]
    if not ids:
        return {}
    placeholders = []
    params: dict[str, Any] = {"uid": user_id}
    for i, nid in enumerate(ids):
        key = f"n{i}"
        placeholders.append(f":{key}")
        params[key] = nid
    res = await db.execute(
        text("SELECT artifact_id, system_memory_ingest, origin FROM workspace_artifact_refs "
             "WHERE user_id = :uid AND artifact_type = 'note' "
             f"AND artifact_id IN ({', '.join(placeholders)})"),
        params,
    )
    out: dict[str, tuple[str, str]] = {}
    for row in res.fetchall():
        policy = str(row[1] or POLICY_USER_FACTS)
        origin = str(row[2] or "unknown")
        # 同一笔记多条来源记录时取最受限者（单调合并，永不因「已发布」而放宽）
        rank = {POLICY_EXCLUDE: 0, POLICY_USER_FACTS: 1, POLICY_NORMAL: 2}
        if row[0] and (str(row[0]) not in out or rank.get(policy, 1) < rank.get(out[str(row[0])][0], 1)):
            out[str(row[0])] = (policy, origin)
    return out


def policy_allows_message(policy: str | None) -> bool:
    if policy is None:      # legacy：保持原行为
        return True
    return policy != POLICY_EXCLUDE


def policy_allows_note(policy: str | None, origin: str | None) -> bool:
    if policy is None:
        return True
    if policy == POLICY_EXCLUDE:
        return False
    if policy == POLICY_USER_FACTS:
        # 只接受可证明为用户撰写的任务笔记；agent 生成或来源不明不进该通道
        return str(origin or "") == "user"
    return True


async def filter_user_messages(db, rows: Iterable[Any], *, stats: IngestStats | None = None,
                               id_attr: str = "id", content_attr: str = "content"
                               ) -> list[Any]:
    """v1 用：`rows` 是 Message 样对象序列。返回保留对象（user_facts 已结构清理）。"""
    materialized = list(rows)
    stats = stats or IngestStats()
    policies = await policies_for_messages(db, [getattr(r, id_attr, None) for r in materialized])
    kept: list[Any] = []
    for row in materialized:
        mid = str(getattr(row, id_attr, "") or "")
        policy = policies.get(mid)
        stats.by_policy[policy or "legacy"] = stats.by_policy.get(policy or "legacy", 0) + 1
        if policy is None:
            stats.legacy_unscopied += 1
            stats.kept += 1
            kept.append(row)
            continue
        if policy == POLICY_EXCLUDE:
            stats.dropped_excluded += 1
            continue
        cleaned, changed = strip_autometadata(getattr(row, content_attr, "") or "")
        if changed:
            stats.stripped_metadata += 1
            # 不修改 DB 行：只在内存副本上清理后交给记忆生成
            row = _ContentProxy(row, cleaned)
        stats.kept += 1
        kept.append(row)
    return kept


class _ContentProxy:
    """只读代理：替换 content 供 prompt 拼接，其余属性透传（绝不写回 DB）。"""

    __slots__ = ("_inner", "_content")

    def __init__(self, inner: Any, content: str) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_content", content)

    @property
    def content(self) -> str:
        return self._content

    def __getattr__(self, item: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), item)


__all__ = ["IngestStats", "POLICIES", "POLICY_EXCLUDE", "POLICY_NORMAL", "POLICY_USER_FACTS",
           "filter_user_messages", "note_policies", "policies_for_messages",
           "policy_allows_message", "policy_allows_note", "strip_autometadata"]
