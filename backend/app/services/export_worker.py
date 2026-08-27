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
                file_path, filename = await self._export_single(task, output_dir)
            elif task.task_type == "bulk":
                file_path, filename = await self._export_bulk(task, output_dir)
            else:
                raise ValueError(f"Unknown task_type: {task.task_type}")

            async with AsyncSessionLocal() as db:
                t = await db.get(ExportTask, task_id)
                if t is None or t.status == "cancelled":
                    if await asyncio.to_thread(os.path.isfile, file_path):
                        await asyncio.to_thread(os.remove, file_path)
                    return
                t.status = "completed"
                t.progress = 1.0
                t.file_path = file_path
                t.filename = filename
                t.completed_at = datetime.utcnow()
                await db.commit()

            logger.info("Export task completed: %s (file=%s)", task_id, filename)

        except asyncio.CancelledError:
            await self._mark_cancelled(task_id)
            raise
        except Exception as exc:
            logger.exception("Export task %s failed", task_id)
            await self._mark_failed(task_id, str(exc))

    async def _export_single(self, task: ExportTask, output_dir: str) -> tuple[str, str]:
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
        safe_name = sanitize_filename(title)

        if task.format == "pdf":
            await self._update_progress(task.id, 0.1)
            pdf_bytes = await asyncio.to_thread(_render_note_pdf, note, workspace_root)
            ext = "pdf"
            file_data = pdf_bytes
        else:
            content = await asyncio.to_thread(_build_note_md, note, workspace_root)
            ext = "md"
            file_data = content.encode("utf-8")

        unique_name = f"{safe_name}_{task.id[:8]}.{ext}"
        file_path = os.path.join(output_dir, unique_name)
        await asyncio.to_thread(self._write_file, file_path, file_data)
        await self._update_progress(task.id, 1.0)

        return file_path, f"{safe_name}.{ext}"

    async def _export_bulk(self, task: ExportTask, output_dir: str) -> tuple[str, str]:
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

        unique_name = f"notes_export_{task.id[:8]}.zip"
        file_path = os.path.join(output_dir, unique_name)

        def build_zip() -> bytes:
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for note in notes:
                    title = note.title or "untitled"
                    safe_name = sanitize_filename(title)
                    if task.format == "pdf":
                        pdf_bytes = _render_note_pdf(note, workspace_root)
                        zf.writestr(f"{safe_name}.pdf", pdf_bytes)
                    else:
                        content = _build_note_md(note, workspace_root)
                        zf.writestr(f"{safe_name}.md", content)
            return zip_buffer.getvalue()

        total = len(notes)
        await self._update_progress(task.id, 0.05)

        if task.format == "pdf" and total > 1:
            pdf_items: list[tuple[str, bytes]] = []
            for idx, note in enumerate(notes):
                title = note.title or "untitled"
                safe_name = sanitize_filename(title)
                pdf_bytes = await asyncio.to_thread(_render_note_pdf, note, workspace_root)
                pdf_items.append((safe_name, pdf_bytes))
                progress = 0.05 + 0.9 * ((idx + 1) / total)
                await self._update_progress(task.id, min(progress, 0.95))

            def _build_pdf_zip(items: list[tuple[str, bytes]]) -> bytes:
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for safe_name, pdf_bytes in items:
                        zf.writestr(f"{safe_name}.pdf", pdf_bytes)
                return zip_buffer.getvalue()

            zip_data = await asyncio.to_thread(_build_pdf_zip, pdf_items)
        else:
            zip_data = await asyncio.to_thread(build_zip)

        await asyncio.to_thread(self._write_file, file_path, zip_data)
        await self._update_progress(task.id, 1.0)

        return file_path, "notes_export.zip"

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
                    .values(progress=min(progress, 0.99), updated_at=datetime.utcnow())
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
                for t in old_tasks:
                    if t.file_path and await asyncio.to_thread(os.path.isfile, t.file_path):
                        await asyncio.to_thread(os.remove, t.file_path)
                    await db.delete(t)
                if old_tasks:
                    await db.commit()
                    logger.info("Cleaned up %d old export tasks", len(old_tasks))
        except Exception:
            pass


export_worker = ExportWorker()
