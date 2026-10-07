# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import io
import json
import logging
import os
import time as _time
import uuid
import zipfile
from contextlib import suppress
from datetime import datetime

from sqlalchemy import select, update

from app.core.config import get_config
from app.db.database import AsyncSessionLocal, ExportTask, Note, Notebook, User
from app.api.notes import _build_note_md, _render_note_pdf, _get_note_workspace_root

config = get_config()
logger = logging.getLogger(__name__)

MAX_CONCURRENT_EXPORTS = 2
POLL_INTERVAL = 2
TASK_TIMEOUT_SECONDS = 600
CLEANUP_AGE_HOURS = 24


class ExportWorker:
    def __init__(self):
        self._poll_task: asyncio.Task | None = None
        self._running_task_ids: set[str] = set()
        self._worker_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._run_poll(), name="export-worker-poll")
            logger.info("ExportWorker started (max_concurrent=%d, poll_interval=%ds)",
                        MAX_CONCURRENT_EXPORTS, POLL_INTERVAL)

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        for task in list(self._worker_tasks):
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()
        logger.info("ExportWorker stopped")

    async def _run_poll(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                await self._poll_pending_tasks()
            except Exception:
                logger.exception("Error in export worker poll")
            await asyncio.sleep(POLL_INTERVAL)
            try:
                await self._cleanup_old_files()
            except Exception:
                logger.exception("Error in export worker cleanup")

    async def _poll_pending_tasks(self) -> None:
        available_slots = MAX_CONCURRENT_EXPORTS - len(self._worker_tasks)
        if available_slots <= 0:
            return

        async with AsyncSessionLocal() as db:
            stmt = (
                select(ExportTask)
                .where(ExportTask.status == "pending")
                .order_by(ExportTask.created_at.asc())
                .limit(available_slots)
                .with_for_update(skip_locked=True)
            )
            result = await db.execute(stmt)
            pending_tasks = result.scalars().all()

            claimed_ids = []
            for task in pending_tasks:
                if task.id in self._running_task_ids:
                    continue
                task.status = "claimed"
                claimed_ids.append(task.id)

            if claimed_ids:
                await db.commit()

            for task_id in claimed_ids:
                self._running_task_ids.add(task_id)
                worker = asyncio.create_task(self._execute_task(task_id), name=f"export-worker-{task_id[:8]}")
                self._worker_tasks.add(worker)
                worker.add_done_callback(self._on_worker_done)

    def _on_worker_done(self, worker_task: asyncio.Task) -> None:
        self._worker_tasks.discard(worker_task)
        if not worker_task.cancelled():
            try:
                exc = worker_task.exception()
                if exc:
                    logger.error("Export worker failed: %s", exc)
            except asyncio.InvalidStateError:
                pass

    async def _execute_task(self, task_id: str) -> None:
        try:
            await self._run_task(task_id)
        except Exception:
            logger.exception("Failed to execute export task %s", task_id)
        finally:
            self._running_task_ids.discard(task_id)

    async def _run_task(self, task_id: str) -> None:
        async with AsyncSessionLocal() as db:
            task = await db.get(ExportTask, task_id)
            if task is None or task.status not in ("pending", "claimed"):
                return
            task.status = "running"
            task.started_at = datetime.utcnow()
            task.progress = 0.0
            await db.commit()

        logger.info("Export task started: %s (type=%s, format=%s)", task_id, task.task_type, task.format)

        try:
            output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output_files")
            await asyncio.to_thread(os.makedirs, output_dir, exist_ok=True)

            if task.task_type == "single":
                file_path, filename, chash = await self._export_single(task, output_dir)
            elif task.task_type == "bulk":
                file_path, filename, chash = await self._export_bulk(task, output_dir)
            else:
                raise ValueError(f"Unknown task_type: {task.task_type}")

            async with AsyncSessionLocal() as db:
                t = await db.get(ExportTask, task_id)
                if t is None or t.status == "cancelled":
                    # W6 fix-round：产物可能是去重共享文件——不删（_cleanup_old_files TTL 兜底）
                    return
                t.status = "completed"
                t.progress = 1.0
                t.file_path = file_path
                t.filename = filename
                t.content_hash = chash  # W6 fix-round A4.9 C1：哈希随完成块落库
                t.completed_at = datetime.utcnow()
                await db.commit()

            logger.info("Export task completed: %s (file=%s)", task_id, filename)

        except asyncio.CancelledError:
            await self._mark_cancelled(task_id)
            raise
        except Exception as exc:
            logger.exception("Export task %s failed", task_id)
            await self._mark_failed(task_id, str(exc))

    async def _export_single(self, task: ExportTask, output_dir: str) -> tuple[str, str, str]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Note).join(Notebook).where(
                    Note.id == task.note_id,
                    Notebook.user_id == task.user_id,
                )
            )
            note = result.scalar_one_or_none()
            workspace_root = await _get_note_workspace_root(db, task.user_id)

        if note is None:
            raise ValueError("Note not found")

        title = note.title or "untitled"
        from app.api.notes import sanitize_filename, _render_note_pdf, _build_note_md
        from app.services.export_registry import (
            fingerprint, render_with_dedup, workspace_fingerprint)
        safe_name = sanitize_filename(title)
        fmt = task.format or "pdf"

        # W6 fix-round（A4.9 C1）：笔记导出同享登记/去重/单飞/护栏——
        # render_with_dedup 的 registry 行落在单飞窗口内（等待方复用首个产物）；
        # 指纹含工作区指纹+渲染版本盐（改图/改管线不失配旧缓存）。
        ee = getattr(task, "endnote_enabled", True)
        # 挂账清零波 T3：ee 仅 pdf 渲染输入——md 键不得按 ee 分叉（缓存恒等）
        ee_key = str(ee) if fmt == "pdf" else "-"
        key_parts = ("note", fmt, title, getattr(note, "content", "") or "",
                     str(note.updated_at), ee_key, workspace_fingerprint(workspace_root))
        if fmt == "pdf":
            render_fn = lambda: _render_note_pdf(note, workspace_root, ee)
            ext = "pdf"
        else:
            render_fn = lambda: _build_note_md(note, workspace_root).encode("utf-8")
            ext = "md"

        async with AsyncSessionLocal() as db:
            file_path, file_name, _cached = await render_with_dedup(
                db, task.user_id, task_type="note", fmt=fmt,
                key_parts=key_parts, render_fn=render_fn,
                output_dir=output_dir, filename_base=safe_name, ext=ext,
                endnote_enabled=ee)
        await self._update_progress(task.id, 1.0)
        return file_path, file_name, fingerprint(task.user_id, *key_parts)

    async def _export_bulk(self, task: ExportTask, output_dir: str) -> tuple[str, str, str]:
        note_ids = json.loads(task.note_ids) if task.note_ids else []
        if not note_ids:
            raise ValueError("No notes selected")

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Note).join(Notebook).where(
                    Note.id.in_(note_ids),
                    Notebook.user_id == task.user_id,
                ).order_by(Note.updated_at.desc())
            )
            notes = result.scalars().all()
            workspace_root = await _get_note_workspace_root(db, task.user_id)

        if not notes:
            raise ValueError("Notes not found")

        from app.api.notes import sanitize_filename, _render_note_pdf, _build_note_md
        from app.services.export_registry import (
            fingerprint, render_with_dedup, workspace_fingerprint)
        fmt = task.format or "pdf"

        # W6 fix-round（A4.9 C1/B-I6）：批量导出同样走 render_with_dedup——
        # 登记 fmt=task.format（旧 "zip" 查询永不命中已修）；单飞覆盖完成窗口；
        # 指纹含工作区指纹+渲染版本盐。
        ee = getattr(task, "endnote_enabled", True)
        # 挂账清零波 T3：ee 仅 pdf 渲染输入——md 键不得按 ee 分叉（缓存恒等）
        ee_key = str(ee) if fmt == "pdf" else "-"
        key_parts = (
            "notes-bulk", fmt,
            *(f"{n.id}:{n.updated_at}:{getattr(n, 'content', '') or ''}" for n in notes),
            ee_key, workspace_fingerprint(workspace_root))
        chash = fingerprint(task.user_id, *key_parts)

        def build_zip() -> bytes:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for note in notes:
                    title = note.title or "untitled"
                    safe_name = sanitize_filename(title)
                    if fmt == "pdf":
                        pdf_bytes = _render_note_pdf(note, workspace_root, ee)
                        zf.writestr(f"{safe_name}.pdf", pdf_bytes)
                    else:
                        content = _build_note_md(note, workspace_root)
                        zf.writestr(f"{safe_name}.md", content)
            return zip_buffer.getvalue()

        await self._update_progress(task.id, 0.05)
        async with AsyncSessionLocal() as db:
            file_path, file_name, _cached = await render_with_dedup(
                db, task.user_id, task_type="note-bulk", fmt=fmt,
                key_parts=key_parts, render_fn=build_zip,
                output_dir=output_dir, filename_base="notes_export", ext="zip",
                endnote_enabled=ee)
        await self._update_progress(task.id, 1.0)
        return file_path, "notes_export.zip", chash

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)

    async def _update_progress(self, task_id: str, progress: float) -> None:
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    update(ExportTask)
                    .where(ExportTask.id == task_id, ExportTask.status == "running")
                    .values(progress=min(progress, 0.99))  # W6 fix-round：updated_at 列不存在（幽灵列，曾误claim已修）
                )
                await db.execute(stmt)
                await db.commit()
        except Exception:
            pass

    async def _mark_failed(self, task_id: str, error: str) -> None:
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    update(ExportTask)
                    .where(ExportTask.id == task_id)
                    .values(
                        status="failed",
                        error=error[:5000],
                        completed_at=datetime.utcnow(),
                    )
                )
                await db.execute(stmt)
                await db.commit()
        except Exception:
            logger.exception("Failed to mark export task %s as failed", task_id)

    async def _mark_cancelled(self, task_id: str) -> None:
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    update(ExportTask)
                    .where(ExportTask.id == task_id)
                    .values(
                        status="cancelled",
                        completed_at=datetime.utcnow(),
                    )
                )
                await db.execute(stmt)
                await db.commit()
        except Exception:
            logger.exception("Failed to mark export task %s as cancelled", task_id)

    async def _cleanup_old_files(self) -> None:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=CLEANUP_AGE_HOURS)
        try:
            async with AsyncSessionLocal() as db:
                stmt = select(ExportTask).where(
                    ExportTask.status.in_(["completed", "failed"]),
                    ExportTask.completed_at < cutoff,
                )
                result = await db.execute(stmt)
                old_tasks = result.scalars().all()
                # UPSTREAM_TODO_20261006 项 8 + 评审 B-1/B-2（fix-round 1）：
                # content-addressed 产物共享命名——删行+独占收割同文件锁原子序
                # （delete_row_and_reap_file：在写产物不收割、并发双删由后到者收割）。
                from app.services.export_registry import delete_row_and_reap_file

                for t in old_tasks:
                    await delete_row_and_reap_file(db, t)
        except Exception:
            pass


export_worker = ExportWorker()
