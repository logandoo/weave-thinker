# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deathmatch story-bible mixin (P5.4 move-only, 2026-10-18)."""
from __future__ import annotations

import json
import logging
import os as _os
import time as _time
from datetime import datetime
from typing import Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.prompts.deathmatch import BIBLE_EVOLUTION_PROMPT, BIBLE_GENERATION_PROMPT

config = get_config()
logger = logging.getLogger(__name__)


BIBLE_DIR_NAME = "bible"

BIBLE_FILE_NAMES = (
    "characters.md", "relationships.md", "world.md", "outline.md", "style.md",
)

_BIBLE_FINGERPRINT_PREFIX = "<!--bible_goal:"

_BIBLE_LOCK_NAME = ".writing"

def _bible_fingerprint(goal: str) -> str:
    """Goal fingerprint embedded in the bible so a SECOND creative goal in
    the same workspace cannot silently reuse the previous goal's spec files
    (A4.9 review: cross-goal stale bible)."""
    import hashlib
    return hashlib.sha256((goal or "").encode("utf-8")).hexdigest()[:12]


def _truncate(text: str, limit: int) -> str:
    from app.services.deathmatch_service import _truncate as _impl
    return _impl(text, limit)


def _ensure_creative_judged(*a, **k):
    from app.services.deathmatch_service import _ensure_creative_judged as f
    return f(*a, **k)


class DeathmatchBibleMixin:
    def _parse_bible_files(self, raw: str) -> Optional[Dict[str, str]]:
        """Parse the bible-generation LLM output into {filename: content}."""
        if not raw:
            return None
        parsed = self._parse_json_object(raw)
        if not isinstance(parsed, dict):
            return None
        files_map: Dict[str, str] = {}
        for name in BIBLE_FILE_NAMES:
            val = parsed.get(name)
            if isinstance(val, str) and val.strip():
                files_map[name] = val.strip()
        return files_map or None

    async def _ensure_bible_files(self, workspace_path: str) -> bool:
        """Story-bible spec for creative goals, stored as FILES in the user
        workspace (user-editable, compression-proof, agent-readable via the
        normal workspace tools; DB keeps only the goal text).

        One-shot per GOAL: a complete bible/ set carrying this goal's
        fingerprint is skipped (cross-goal stale reuse prevented); a
        concurrent writer is detected via a lock file. Fail-open: on any
        error write a minimal bible with the goal text (never blocks the
        goal loop)."""
        if not workspace_path:
            return False
        if not config.deathmatch_bible_enabled:
            return False
        goal = self._conv.deathmatch_goal or ""
        if not await _ensure_creative_judged(goal):
            return False
        fingerprint = _bible_fingerprint(goal)
        bible_dir = _os.path.join(workspace_path, BIBLE_DIR_NAME)
        marker_path = _os.path.join(bible_dir, "characters.md")
        try:
            if _os.path.isdir(bible_dir) and all(
                _os.path.isfile(_os.path.join(bible_dir, n))
                for n in BIBLE_FILE_NAMES
            ):
                try:
                    with open(marker_path, encoding="utf-8") as fh:
                        head = fh.read(120)
                except (OSError, UnicodeDecodeError, ValueError):
                    head = ""
                if _BIBLE_FINGERPRINT_PREFIX in head and fingerprint in head:
                    return True  # already written for THIS goal
            _os.makedirs(bible_dir, exist_ok=True)
        except OSError as exc:
            logger.warning("deathmatch bible dir error: %s", exc)
            return False

        # Concurrency guard: another request may be generating the bible.
        # Stale-lock self-heal: if the lock file is older than 5 minutes the
        # writer is presumed dead (crash during the LLM call) and we take
        # over (A4.9 r2 Important: a stale lock must never permanently block
        # bible generation for the workspace).
        lock_path = _os.path.join(bible_dir, _BIBLE_LOCK_NAME)
        _lock_acquired = False
        try:
            fd = _os.open(lock_path, _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
            _os.write(fd, str(int(_time.time())).encode("ascii"))
            _os.close(fd)
            _lock_acquired = True
        except OSError:
            try:
                stale = (int(_time.time()) - int(_os.stat(lock_path).st_mtime)) > 300
            except OSError:
                stale = False
            if stale:
                try:
                    _os.remove(lock_path)
                except OSError:
                    pass
                fd = _os.open(lock_path, _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
                _os.write(fd, str(int(_time.time())).encode("ascii"))
                _os.close(fd)
                _lock_acquired = True
            else:
                return True  # concurrent writer in progress — it will finish
        try:
            # Prefer the grilling-phase draft (answers fresh, no second LLM
            # call); fall back to lazy generation.
            files_map = None
            draft = getattr(self._conv, "deathmatch_bible_draft", None) or {}
            if isinstance(draft, dict):
                files_map = {
                    k: v for k, v in draft.items()
                    if isinstance(v, str) and v.strip()
                }
            if not files_map:
                history = self._format_history_for_prompt()
                llm = self._make_llm()
                raw = await self._llm_generate(
                    llm, BIBLE_GENERATION_PROMPT,
                    f"目标:\n{_truncate(goal, 2000)}\n\n盘问历史:\n{history or '(无)'}",
                    temperature=0.2,
                )
                files_map = self._parse_bible_files(raw)
        except Exception as exc:
            logger.warning("deathmatch bible generation failed (%s) — minimal bible", exc)
            files_map = None
        try:
            if not files_map:
                files_map = {
                    name: f"# {name}\n\n（未生成，后续补充）\n\n目标:\n{_truncate(goal, 800)}"
                    for name in BIBLE_FILE_NAMES
                }
            written = 0
            for name in BIBLE_FILE_NAMES:
                content = files_map.get(name) or f"# {name}\n\n（未生成）\n目标: {_truncate(goal, 300)}"
                # Fingerprint line marks which goal this bible belongs to.
                content = f"{_BIBLE_FINGERPRINT_PREFIX}{fingerprint}-->\n\n{content}"
                try:
                    with open(_os.path.join(bible_dir, name), "w", encoding="utf-8") as fh:
                        fh.write(content)
                    written += 1
                except OSError as exc:
                    logger.warning("deathmatch bible write failed for %s: %s", name, exc)
            logger.info(
                "deathmatch bible written: %d files in %s (conv %s, goal %s)",
                written, bible_dir, self._conv.id, fingerprint,
            )
            return written > 0
        finally:
            if _lock_acquired:
                try:
                    _os.remove(lock_path)
                except OSError:
                    pass

    def _read_bible_snippet(self, name: str, limit: int = 800) -> str:
        """Read a bible spec file from the workspace (fresh per round — the
        agent may have edited it). Returns '' when unavailable."""
        base = self._workspace_path or ""
        if not base:
            return ""
        path = _os.path.join(base, BIBLE_DIR_NAME, name)
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, UnicodeDecodeError, ValueError):
            return ""
        return _truncate(content.strip(), limit)

    def _build_bible_context_block(self) -> str:
        """Ring-structured bible context injected into the continuation:
        Ring1 = style guide (voice / kill list, stable project-level),
        Ring2 = outline + world (chapter-level continuity),
        Ring3 = characters + relationships (scene-level references).
        Fresh per round (files may be edited by agent or user)."""
        style = self._read_bible_snippet("style.md", 800)
        outline = self._read_bible_snippet("outline.md", 800)
        world = self._read_bible_snippet("world.md", 600)
        chars = self._read_bible_snippet("characters.md", 600)
        rels = self._read_bible_snippet("relationships.md", 500)
        parts = []
        if style:
            parts.append(f"[风格指南 Ring1]\n{style}")
        if outline or world:
            parts.append(f"[大纲与世界 Ring2]\n{outline or '(无大纲)'}\n\n{world or ''}".strip())
        if chars or rels:
            parts.append(f"[人物与关系 Ring3]\n{chars or ''}\n\n{rels or ''}".strip())
        if not parts:
            return ""
        return "<bible_context>\n" + "\n\n".join(parts) + "\n</bible_context>"

    async def _evolve_bible(self, workspace_path: str, step_id: str, last_response: str) -> None:
        """Bible evolution: after a step is marked done (creative goals),
        extract new canon facts (character state changes, relationship
        shifts, foreshadowing planted/paid off) and APPEND them to
        bible/evolution.md — the original spec files stay untouched, the
        evolution log accumulates the story's running truth. Fail-open."""
        if not workspace_path or not await _ensure_creative_judged(self._conv.deathmatch_goal or ""):
            return
        if not config.deathmatch_bible_enabled:
            return
        bible_dir = _os.path.join(workspace_path, BIBLE_DIR_NAME)
        if not _os.path.isdir(bible_dir):
            return
        evo_path = _os.path.join(bible_dir, "evolution.md")
        try:
            with open(evo_path, encoding="utf-8") as fh:
                prev = fh.read()[-800:]
        except (OSError, UnicodeDecodeError, ValueError):
            prev = ""
        try:
            llm = self._make_llm()
            raw = await self._llm_generate(
                llm, BIBLE_EVOLUTION_PROMPT,
                f"目标:\n{_truncate(self._conv.deathmatch_goal or '', 800)}\n\n"
                f"步骤 {step_id} 完成时的回复:\n{_truncate(last_response, 1500)}\n\n"
                f"evolution.md 已有内容（尾部）:\n{prev or '(空)'}",
                temperature=0.2,
            )
            parsed = self._parse_json_object(raw) or {}
            facts_list = parsed.get("canon_facts")
            if not isinstance(facts_list, list) or not facts_list:
                return
            facts_text = "\n".join(f"- {str(f)[:300]}" for f in facts_list[:8])
            block = (
                f"\n## 步骤 {step_id} 完成（{datetime.utcnow().isoformat(timespec='minutes')}）\n"
                f"{facts_text}\n"
            )
            with open(evo_path, "a", encoding="utf-8") as fh:
                fh.write(block)
            logger.info(
                "deathmatch bible evolution: %d facts appended (step %s, conv %s)",
                len(facts_list), step_id, self._conv.id,
            )
        except Exception as exc:
            logger.warning("deathmatch bible evolution failed (non-blocking): %s", exc)

    async def _draft_bible_from_grilling(self, db: AsyncSession, goal: str = "") -> None:
        """Draft the story bible right after grilling (the answers are
        fresh) and cache it in ``deathmatch_bible_draft`` so the baseline
        round writes it to workspace files without a second LLM call.
        Fail-open: no draft → the baseline round generates lazily.
        NOTE: the synthesized goal must be passed explicitly — it is NOT yet
        assigned to the conversation at the call sites (A4.9 C1)."""
        goal = goal or self._conv.deathmatch_goal or ""
        if not await _ensure_creative_judged(goal) or not config.deathmatch_bible_enabled:
            return
        try:
            history = self._format_history_for_prompt()
            llm = self._make_llm()
            raw = await self._llm_generate(
                llm, BIBLE_GENERATION_PROMPT,
                f"目标:\n{_truncate(goal, 2000)}\n\n盘问历史:\n{history or '(无)'}",
                temperature=0.2,
            )
            files_map = self._parse_bible_files(raw)
            if files_map:
                self._conv.deathmatch_bible_draft = files_map
                await db.flush()
                logger.info(
                    "deathmatch bible draft cached from grilling (conv %s, %d files)",
                    self._conv.id, len(files_map),
                )
        except Exception as exc:
            logger.warning("deathmatch bible draft failed (non-blocking): %s", exc)
