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

import logging
from typing import Any, Dict, List, Optional

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
        self._conv.deathmatch_reflections = entries

    def add(
        self,
        *,
        turn: int,
        action_summary: str,
        verdict: str = "",
        issues: Optional[List[str]] = None,
        retry_instruction: str = "",
    ) -> Dict[str, Any]:
        """Append a reflection entry and persist."""
        entry = {
            "turn": int(turn),
            "action_summary": (action_summary or "").strip()[:600],
            "verdict": (verdict or "").strip()[:120],
            "issues": list(issues or [])[:8],
            "retry_instruction": (retry_instruction or "").strip()[:600],
        }
        entries = self._entries()
        entries.append(entry)
        self._save(entries)
        return entry

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
        entries = self._entries()
        if len(entries) < window:
            return None
        recent = entries[-window:]
        # Compare normalized issue signatures.
        signatures = []
        for e in recent:
            issues = e.get("issues") or []
            sig = "|".join(str(i).strip()[:40].lower() for i in issues)
            signatures.append(sig)
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
