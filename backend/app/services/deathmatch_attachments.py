# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Move-only mixin extracted from app/services/deathmatch_service.py (C-03/C-04, 2026-10-19).

Module-level names owned by the service module are synced at call time so
monkeypatches of ``deathmatch_service.<name>`` keep working.
"""
from __future__ import annotations

_GLOBALS = (   'Any',
    'DeathmatchManager',
    'Dict',
    'List',
    'Optional',
    '_os',
    '_re',
    'json',
    'logger',
    'select')


def _sync_deathmatchattachments_globals() -> None:
    from app.services import deathmatch_service as _svc

    g = globals()
    for _name in _GLOBALS:
        if hasattr(_svc, _name):
            g[_name] = getattr(_svc, _name)


class DeathmatchAttachmentsMixin:
    def generate_final_summary_table(self) -> str:
        """Generate a markdown table summarizing all plan steps, their
        completion status, output content, and output file names.

        This table is injected into the final deathmatch summary message so
        the user can see at a glance what was done and what was produced.

        Sanitization (conv 6b0faf81 user report 2026-08-07): step
        descriptions can be the RAW goal text (the pre-fix degraded
        single-step plan carries the whole multi-line goal as its
        description) — newlines split the markdown table row and ``|``
        breaks columns. Every cell is normalized: ``|`` escaped, newlines
        collapsed to spaces, markdown headings stripped. Output files are
        deduplicated by basename preferring the workspace root over
        scratch/task_ intermediates.
        """
        _sync_deathmatchattachments_globals()
        plan = self._conv.deathmatch_plan
        if not plan or not isinstance(plan, dict):
            return ""
        steps = plan.get("steps") or []
        if not steps:
            return ""

        def _cell(text: str, limit: int = 80) -> str:
            """Normalize one markdown-table cell: strip markdown headings,
            collapse whitespace/newlines, escape pipes, cap length."""
            if not text:
                return ""
            t = text.strip()
            # Strip leading markdown heading markers and list markers so a
            # description that IS the raw goal text ("# 目标描述\n## 最终产出…")
            # renders as readable prose instead of heading fragments.
            t = _re.sub(r"^\s*(#{1,6}\s*|\*\s*|-\s*|\d+\.\s*)", "", t, flags=_re.M)
            t = _re.sub(r"\s+", " ", t).strip()
            t = t.replace("|", "\\|")
            if len(t) > limit:
                t = t[: limit - 1] + "…"
            return t

        goal = (self._conv.deathmatch_goal or "").strip()
        goal_norm = _re.sub(r"\s+", " ", goal)
        # Degraded single-step plan detection (A4.9 review r2): only when the
        # plan has EXACTLY ONE step AND the goal is non-empty AND the step
        # description is (or starts with) the raw goal text is it the poison
        # single-step plan. A multi-step plan whose first step legitimately
        # begins with goal phrasing must keep its real description; an empty
        # goal must never make every step match unconditionally.
        _first_desc_norm = _re.sub(r"\s+", " ", (steps[0].get("description") or "")).strip()
        _is_degraded_single = (
            len(steps) == 1
            and bool(goal_norm)
            and bool(_first_desc_norm)
            and (
                _first_desc_norm == goal_norm
                or _first_desc_norm.startswith(goal_norm[:80])
                or goal_norm.startswith(_first_desc_norm[:80])
            )
        )

        lines = [
            "",
            "## 死磕模式任务完成汇总表",
            "",
            "| 序号 | 任务步骤 | 完成情况 | 最终输出内容 | 输出文件名称 |",
            "|------|----------|----------|-------------|-------------|",
        ]
        for s in steps:
            sid = _cell(s.get("id", ""), 20)
            desc = s.get("description", "")
            # Degraded single-step plan: the description IS the whole goal
            # text (pre-fix poison plan). Show a clean label instead of
            # dumping the goal into the step cell.
            if _is_degraded_single:
                desc = f"完成整体目标（{_cell(goal_norm, 40)}…）"
            status = s.get("status", "pending")
            status_text = _cell({
                "done": "已完成",
                "in_progress": "进行中",
                "pending": "未完成",
            }.get(status, status), 20)
            output_summary = _cell(s.get("output_summary", ""), 100)
            files = s.get("output_files") or []
            # Dedup by basename preferring workspace-root files over
            # scratch/task_ intermediates (same file written per-call into
            # different task dirs produces duplicate basenames). Unlike a
            # first-wins pass, a scratch entry that appears first must NOT
            # shadow the final root-level file (A4.9 review r2).
            from app.api.chat import _is_scratch_path as _is_scratch
            seen: dict = {}
            for f in files:
                if not isinstance(f, str) or not f:
                    continue
                base = _os.path.basename(f) if ("/" in f or "\\" in f) else f
                if base in seen:
                    # Prefer the non-scratch entry when a duplicate exists.
                    if _is_scratch(f) and not _is_scratch(seen[base]):
                        continue
                    if not _is_scratch(f) and _is_scratch(seen[base]):
                        seen[base] = f
                    continue
                seen[base] = f
            if seen:
                file_names = ", ".join(sorted(seen.keys()))
            else:
                file_names = "—"
            lines.append(
                f"| {sid} | {_cell(desc, 60) or '—'} | {status_text} "
                f"| {output_summary or '—'} | {_cell(file_names, 100) or '—'} |"
            )
        lines.append("")
        return "\n".join(lines)
    @staticmethod
    def _deduplicate_attachments(all_atts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Deduplicate attachments by name, keeping the largest size.

        Folder cards (``type == "folder"``, 2026-09-19) deduplicate by
        ``rel_path`` so same-named folders in different directories survive.
        """
        _sync_deathmatchattachments_globals()
        by_name: Dict[str, Dict[str, Any]] = {}
        by_folder: Dict[str, Dict[str, Any]] = {}
        for att in all_atts:
            if att.get("type") == "folder":
                key = att.get("rel_path") or att.get("path") or att.get("name") or ""
                if not key:
                    continue
                existing = by_folder.get(key)
                if existing is None or (att.get("size") or 0) > (existing.get("size") or 0):
                    by_folder[key] = att
                continue
            name = att.get("name") or att.get("filename") or ""
            if not name:
                continue
            existing = by_name.get(name)
            if existing is None or (att.get("size") or 0) > (existing.get("size") or 0):
                by_name[name] = att
        return list(by_name.values()) + list(by_folder.values())
    async def _collect_from_provide_file_only(self) -> List[Dict[str, Any]]:
        """Collect files that were explicitly provided via ``provide_file`` tool calls.

        This prevents intermediate files (drafts, scripts, temp files generated
        by execute_code/terminal during intermediate steps) from appearing as
        final deliverables. Only files the agent deliberately chose to attach
        with ``provide_file`` are included.
        """
        _sync_deathmatchattachments_globals()
        from app.db.database import AsyncSessionLocal, Message
        from sqlalchemy import select

        conv_id = self._conv.id
        if not conv_id:
            return []

        all_atts: List[Dict[str, Any]] = []
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Message).where(
                        Message.conversation_id == conv_id,
                        Message.role == "assistant",
                        Message.delivery_status != "streaming",
                    ).order_by(Message.created_at)
                )
                messages = result.scalars().all()
                # 评审 I3：外置桩还原全文（否则 provide_file 收割读到 stub JSON）
                from app.services.message_payload_service import resolve_message_fields
                messages = await resolve_message_fields(list(messages), db)
                for msg in messages:
                    tr_json = msg.tool_results
                    if not tr_json:
                        continue
                    try:
                        tr_obj = json.loads(tr_json)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    agent_steps = tr_obj.get("agent_steps") or []
                    for step in agent_steps:
                        name = step.get("name", "")
                        title = step.get("title", "")
                        if name not in ("provide_file", "provide_folder") and title not in ("提供文件", "提供文件夹"):
                            continue
                        content = step.get("content", "")
                        if not content:
                            continue
                        try:
                            parsed = json.loads(content)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        for gf in parsed.get("generated_files") or []:
                            if isinstance(gf, dict) and gf.get("name") and (gf.get("rel_path") or gf.get("path")):
                                all_atts.append(gf)
                        for gf in parsed.get("generated_folders") or []:
                            if isinstance(gf, dict) and gf.get("name") and (gf.get("rel_path") or gf.get("path")):
                                if gf.get("type") != "folder":
                                    gf = {**gf, "type": "folder"}
                                all_atts.append(gf)
        except Exception as exc:
            logger.warning("_collect_from_provide_file_only DB query failed: %s", exc)

        return self._deduplicate_attachments(all_atts)
    async def _collect_all_attachments_legacy(self) -> List[Dict[str, Any]]:
        """Fallback: collect ALL attachments from ALL messages (V1 behaviour).

        Used when ``provide_file`` was never called (agent didn't use it,
        or the task completed before provide_file was introduced).
        """
        _sync_deathmatchattachments_globals()
        from app.db.database import AsyncSessionLocal, Message
        from sqlalchemy import select

        conv_id = self._conv.id
        if not conv_id:
            return []

        all_atts: List[Dict[str, Any]] = []
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Message).where(
                        Message.conversation_id == conv_id,
                        Message.role == "assistant",
                        Message.delivery_status != "streaming",
                    ).order_by(Message.created_at)
                )
                messages = result.scalars().all()
                # 评审 I3：外置桩还原全文（否则 provide_file 收割读到 stub JSON）
                from app.services.message_payload_service import resolve_message_fields
                messages = await resolve_message_fields(list(messages), db)
                for msg in messages:
                    tr_json = msg.tool_results
                    if not tr_json:
                        continue
                    try:
                        tr_obj = json.loads(tr_json)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    atts = tr_obj.get("attachments") or []
                    if isinstance(atts, list):
                        for att in atts:
                            if isinstance(att, dict) and att.get("name") and (att.get("rel_path") or att.get("path")):
                                all_atts.append(att)
        except Exception as exc:
            logger.warning("_collect_all_attachments_legacy DB query failed: %s", exc)

        return self._deduplicate_attachments(all_atts)
    async def _collect_generated_files_from_tool_outputs(self) -> List[Dict[str, Any]]:
        """Harvest every file the agent's tools reported across all turns.

        Walks every assistant message's ``tool_results`` and collects file
        dicts from TWO sources:
        1. ``agent_steps[].content`` JSON — recursively finds dicts with
           ``name``+``path`` (e.g. ``generated_files`` from
           execute_code/provide_file, ``files`` from word_count). Also
           recognizes ``filename``+``file_path`` as aliases (pdf_export,
           which uses singular keys instead of a ``generated_files`` list).
        2. ``attachments`` (top-level) — the canonical, framework-populated
           attachment list that chat.py builds consistently across ALL tools
           (provide_file, pdf_export, execute_code, terminal, ...). This is
           the reliable source because it normalizes the schema regardless
           of how each tool formats its content JSON.

        Grounded strictly in conversation tool output — no workspace
        filesystem scanning. Missing ``size``/``type`` are filled in
        from the reported path (stat of a known file, not a directory scan).
        """
        _sync_deathmatchattachments_globals()
        from app.db.database import AsyncSessionLocal, Message
        from sqlalchemy import select
        from app.tools.provide_file import _guess_file_type
        from app.api.chat import _is_scratch_path

        conv_id = self._conv.id
        if not conv_id:
            return []

        found: List[Dict[str, Any]] = []

        def _add_file(name: Any, path: Any, size: Any = None, ftype: Any = None) -> None:
            """Normalize and append a single file dict."""
            if not (isinstance(name, str) and name and isinstance(path, str) and path):
                return
            # scratch/task_XXXX intermediates are never deliverables — they
            # must not leak into deathmatch final deliverables even when the
            # agent cites them by name (conv 2b36fb09 pattern).
            if _is_scratch_path(path):
                return
            att = {"name": name, "path": path}
            if not isinstance(size, int) or size <= 0:
                try:
                    size = _os.path.getsize(path)
                except OSError:
                    size = 0
            att["size"] = size
            att["type"] = ftype or _guess_file_type(name)
            found.append(att)

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                # Standard schema: name + path (provide_file, execute_code,
                # word_count, and the top-level attachments list).
                name = node.get("name")
                path = node.get("path")
                if isinstance(name, str) and name and isinstance(path, str) and path:
                    _add_file(name, path, node.get("size"), node.get("type"))
                else:
                    # Alias schema: filename + file_path (pdf_export, which
                    # uses singular keys in its content JSON instead of a
                    # generated_files list). Conv 2fa87be4: the PDF was
                    # harvested from provide_file's generated_files but
                    # pdf_export's filename/file_path was missed, so the
                    # final deliverables showed 11 files with NO PDF even
                    # though the agent explicitly cited it.
                    fname = node.get("filename")
                    fpath = node.get("file_path")
                    if isinstance(fname, str) and fname and isinstance(fpath, str) and fpath:
                        _add_file(fname, fpath, node.get("size"), node.get("type"))
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Message).where(
                        Message.conversation_id == conv_id,
                        Message.role == "assistant",
                        Message.delivery_status != "streaming",
                    ).order_by(Message.created_at)
                )
                messages = result.scalars().all()
                # 评审 I3：外置桩还原全文（否则 provide_file 收割读到 stub JSON）
                from app.services.message_payload_service import resolve_message_fields
                messages = await resolve_message_fields(list(messages), db)
                for msg in messages:
                    tr_json = msg.tool_results
                    if not tr_json:
                        continue
                    try:
                        tr_obj = json.loads(tr_json)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    # Source 1: walk agent_steps[].content JSON recursively.
                    for step in tr_obj.get("agent_steps") or []:
                        content = step.get("content", "")
                        if not content:
                            continue
                        try:
                            parsed = json.loads(content)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        _walk(parsed)
                    # Source 2: top-level attachments list. This is the
                    # canonical, framework-populated list (chat.py builds it
                    # from all_attachments, normalizing name/path/size/type
                    # across ALL tools). Walking it catches files from tools
                    # whose content JSON uses a non-standard schema (e.g.
                    # pdf_export's filename/file_path) that _walk might miss
                    # if the schema changes in the future.
                    for att in tr_obj.get("attachments") or []:
                        if isinstance(att, dict):
                            _add_file(
                                att.get("name") or att.get("filename"),
                                att.get("path") or att.get("file_path") or att.get("rel_path"),
                                att.get("size"),
                                att.get("type"),
                            )
        except Exception as exc:
            logger.warning("_collect_generated_files_from_tool_outputs DB query failed: %s", exc)

        return self._deduplicate_attachments(found)
    @staticmethod
    def _expand_numbered_set(att: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Expand one cited attachment into its numbered-sibling family.

        conv 6b0faf81 user report 2026-08-07: the novel was delivered as 50
        numbered files (novel/ch01.md … ch50.md), but the agent's final reply
        only cited "ch01.md" → the citation filter returned 1 download card
        while 50 chapters existed. When a cited file matches
        ``<prefix><digits><suffix>`` (ch01.md, chapter1.txt, part_03.pdf), scan
        its directory for the consecutive numbered family and return all
        siblings that actually exist on disk. Strictly filesystem-grounded:
        nothing is invented — only real files in the same directory as a
        file the agent explicitly cited.
        """
        _sync_deathmatchattachments_globals()
        from app.tools.provide_file import _guess_file_type
        try:
            name = att.get("name") or ""
            path = att.get("path") or att.get("file_path") or att.get("rel_path") or ""
            m = _re.match(r"^(.+?)(\d+)(\.[A-Za-z0-9]+)$", name)
            if not m or not path:
                return [att]
            prefix, num_str, suffix = m.group(1), m.group(2), m.group(3)
            num_len = len(num_str)
            base_num = int(num_str)
            directory = _os.path.dirname(path)
            # rel_path cards (2026-09-19) have no absolute directory to scan —
            # the numbered-family expansion is an absolute-path-only legacy aid.
            if not directory or not _os.path.isabs(directory) or not _os.path.isdir(directory):
                return [att]
            found: Dict[int, Dict[str, Any]] = {base_num: att}
            try:
                entries = _os.listdir(directory)
            except OSError:
                return [att]
            for entry in entries:
                em = _re.match(rf"^{_re.escape(prefix)}(\d+){_re.escape(suffix)}$", entry)
                if not em or len(em.group(1)) != num_len:
                    continue
                n = int(em.group(1))
                if n <= 0 or n == base_num or n in found:
                    continue
                fp = _os.path.join(directory, entry)
                if not _os.path.isfile(fp):
                    continue
                try:
                    size = _os.path.getsize(fp)
                except OSError:
                    size = 0
                found[n] = {"name": entry, "path": fp, "size": size, "type": _guess_file_type(entry)}
            if len(found) <= 1:
                return [att]
            # Gap guard (A4.9 review r2): only expand when the family is
            # DENSE around the cited file — every number from the lowest to
            # the highest exists. A gapped set (ch01, ch03 present, ch02
            # missing) means the directory holds unrelated leftovers (workspace
            # roots are per-user and shared across conversations); attaching
            # them would fabricate a family the agent never produced.
            nums = sorted(found)
            if nums[-1] - nums[0] + 1 != len(nums):
                return [att]
            return [found[n] for n in nums]
        except Exception:
            return [att]
    @staticmethod
    def _filter_cited_deliverables(
        candidates: List[Dict[str, Any]], citation_texts: List[str]
    ) -> List[Dict[str, Any]]:
        """Keep only generated files whose filename is explicitly cited in the
        given texts (agent's final response, judge's done reason).

        A cited file that is part of a numbered family is expanded to its
        siblings (see ``_expand_numbered_set``) so a multi-file deliverable
        (50-chapter novel) is not reduced to the single file the reply
        happened to name.
        """
        _sync_deathmatchattachments_globals()
        cited: List[Dict[str, Any]] = []
        seen_paths: set = set()
        for att in candidates:
            name = att.get("name") or ""
            if len(name) < 6 or "." not in name:
                continue
            for text in citation_texts:
                if text and name in text:
                    for expanded in DeathmatchManager._expand_numbered_set(att):
                        _p = expanded.get("path") or expanded.get("name") or ""
                        if _p in seen_paths:
                            continue
                        seen_paths.add(_p)
                        cited.append(expanded)
                    break
        return cited
    async def collect_final_deliverables_from_messages(
        self, citation_texts: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Collect deliverable files from agent tool output across all turns.

        Uses a three-tier strategy:
        1. Primary: files explicitly attached via ``provide_file`` tool calls.
           This prevents intermediate/draft files from appearing as final deliverables.
        2. Supplement: files the agent's tools generated AND whose filename is
           explicitly cited in ``citation_texts`` (final response / judge reason).
           Catches final deliverables (e.g. a merged full-novel docx) that the
           agent produced via execute_code but forgot to pass to provide_file.
        3. Fallback: if neither yielded anything (provide_file never called),
           collects ALL attachments (V1 behaviour) so the user still sees something.

        Returns a list of attachment dicts ``{name, path, size, type}``.
        """
        _sync_deathmatchattachments_globals()
        provide_only = await self._collect_from_provide_file_only()
        cited: List[Dict[str, Any]] = []
        if citation_texts:
            generated = await self._collect_generated_files_from_tool_outputs()
            cited = self._filter_cited_deliverables(generated, citation_texts)

        merged = self._deduplicate_attachments(provide_only + cited)
        if merged:
            logger.info(
                "collect_final_deliverables: provide_file=%d cited=%d merged=%d",
                len(provide_only), len(cited), len(merged),
            )
            return merged

        logger.info(
            "collect_final_deliverables: provide_file never called, falling back to legacy (all attachments)"
        )
        return await self._collect_all_attachments_legacy()
