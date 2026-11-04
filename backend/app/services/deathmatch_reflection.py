# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Reflection memory for deathmatch (死磕) PEVR mode.

Records per-turn reflections (what was attempted, the verdict, issues found,
and the retry instruction) and injects the most recent N into the continuation
prompt so the executor avoids repeating the same dead ends.

Persistence: ``conversation.deathmatch_reflections`` (JSON list). Capped at
``config.deathmatch_reflection_memory_max_items`` (default 10).

See loop_improve.md §2.7 (Reflexion) and Phase 3.3.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect as sa_inspect

from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()


class ReflectionMemory:
    """Per-conversation reflection memory backed by Conversation.deathmatch_reflections."""

    def __init__(self, conversation: Any):
        self._conv = conversation

    def _entries(self) -> List[Dict[str, Any]]:
        raw = self._conv.deathmatch_reflections
        if not raw:
            return []
        if isinstance(raw, list):
            return raw
        try:
            import json
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, entries: List[Dict[str, Any]]) -> None:
        limit = config.deathmatch_reflection_memory_max_items
        # Keep the most recent `limit` entries.
        if len(entries) > limit:
            entries = entries[-limit:]
        # R3 陷阱护栏（复审 I-persistence；同 test_deathmatch_json_persistence
        # pin 的浅拷贝/同对象重赋值 no-op）：必须深拷贝出全新对象 + 触脏，
        # 否则原地改 entry + 重赋同一 list 在 SQLAlchemy 眼里无变更。
        self._conv.deathmatch_reflections = copy.deepcopy(entries)
        try:
            from sqlalchemy.orm.attributes import flag_modified
            if sa_inspect(self._conv, raiseerr=False) is not None:
                flag_modified(self._conv, "deathmatch_reflections")
        except Exception:
            pass
        # B-8（D-15）：反思写-through（数据列，不碰控制字段）
        from app.services.deathmatch_write_through import write_through_bg
        write_through_bg(self._conv, deathmatch_reflections=self._conv.deathmatch_reflections)

    def add(
        self,
        *,
        turn: int,
        action_summary: str,
        verdict: str = "",
        issues: Optional[List[str]] = None,
        retry_instruction: str = "",
    ) -> Dict[str, Any]:
        """Append a reflection entry and persist.

        M3b①（勘正 A5）：条目带 id/retracted 字段（JSON 演化；旧条目缺省兼容）。"""
        import uuid
        entry = {
            "id": uuid.uuid4().hex,
            "turn": int(turn),
            "action_summary": (action_summary or "").strip()[:600],
            "verdict": (verdict or "").strip()[:120],
            "issues": list(issues or [])[:8],
            "retry_instruction": (retry_instruction or "").strip()[:600],
            "retracted": False,
        }
        entries = self._entries()
        entries.append(entry)
        self._save(entries)
        return entry

    @staticmethod
    def _signature(entry: Dict[str, Any]) -> str:
        issues = entry.get("issues") or []
        return "|".join(str(i).strip()[:40].lower() for i in issues)

    def retract_repeated(self, *, window: int = 3, note: str = "") -> int:
        """M3b①：撤回同签名窗口内的反思（标 retracted + 修正指令）。

        修正指令 grounded 于条目自身 issues/retry（失败路线的证据面）；
        返回撤回条数（无同签名窗口 = 0）。"""
        entries = self._entries()
        alive = [e for e in entries if not e.get("retracted")]
        if len(alive) < window:
            return 0
        recent = alive[-window:]
        sigs = {self._signature(e) for e in recent}
        if len(sigs) != 1 or not next(iter(sigs)):
            return 0
        first_issue = str((recent[-1].get("issues") or ["未知"])[0])[:120]
        correction = (note or "").strip()[:300] or (
            f"该路线已被证伪（同签名问题连续 {window} 轮未解：{first_issue}）——请避免重复"
        )
        n = 0
        for e in recent:
            e["retracted"] = True
            e["retraction_note"] = correction
            n += 1
        self._save(entries)
        return n

    def maybe_retract_on_escalation(self, *, window: int = 3) -> int:
        """M3b①：升级事件触发的自动撤回（开关 deathmatch_reflection_retract_enabled
        默认关——D-960 先例，记忆自动回写须保守）。"""
        if not getattr(config, "deathmatch_reflection_retract_enabled", False):
            return 0
        if not self.detect_repeated_failures(window=window):
            return 0
        return self.retract_repeated(window=window)

    def recent(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        n = limit if limit is not None else config.deathmatch_reflection_memory_max_items
        entries = self._entries()
        return entries[-n:] if n > 0 else entries

    def build_injection_prompt(self) -> str:
        """Build a prompt fragment summarizing recent reflections.

        Returns an empty string when there are no reflections, so callers can
        unconditionally append the result.
        """
        entries = self.recent()
        if not entries:
            return ""
        lines = [
            "<deathmatch_reflections>",
            "以下是最近几轮的反思记录，请避免重复同样的失败策略：",
            "",
        ]
        for e in entries:
            turn = e.get("turn", "?")
            summary = e.get("action_summary", "")
            issues = e.get("issues") or []
            retry = e.get("retry_instruction", "")
            if e.get("retracted"):
                # M3b①：撤回条目渲染为「避免」块（修正指令随行）。
                note = e.get("retraction_note") or "该路线已被证伪"
                lines.append(f"[第{turn}轮·已撤回] 请避免：{summary}")
                lines.append(f"  修正: {note}")
                lines.append("")
                continue
            lines.append(f"[第{turn}轮] {summary}")
            if issues:
                lines.append("  问题: " + "; ".join(issues))
            if retry:
                lines.append(f"  建议: {retry}")
            lines.append("")
        lines.append("</deathmatch_reflections>")
        return "\n".join(lines)

    def detect_repeated_failures(self, *, window: int = 3) -> Optional[str]:
        """Detect when the last `window` reflections share the same issue prefix.

        Returns an escalation prompt when a repeated failure pattern is found,
        else None. Used to nudge the executor to switch strategies.
        """
        # M3b①：升级窗口只统计未撤回条目（撤回后须重算，否则旧签名持续触发）。
        entries = [e for e in self._entries() if not e.get("retracted")]
        if len(entries) < window:
            return None
        recent = entries[-window:]
        # Compare normalized issue signatures.
        signatures = [self._signature(e) for e in recent]
        if any(not s for s in signatures):
            return None
        if len(set(signatures)) == 1:
            return (
                "<deathmatch_escalation>\n"
                f"最近 {window} 轮都遇到了相同的问题：{recent[-1].get('issues', ['未知'])[0]}。\n"
                "你必须换一种完全不同的策略。不要再重复之前的做法。\n"
                "</deathmatch_escalation>"
            )
        return None

    def clear(self) -> None:
        self._conv.deathmatch_reflections = []
