# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.db.database import get_db, ExportTask, User
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/export-tasks", tags=["export-tasks"])
logger = logging.getLogger(__name__)


class ExportTaskCreate(BaseModel):
    task_type: str = "single"
    format: str = "pdf"
    note_id: Optional[str] = None
    note_ids: Optional[List[str]] = None


class ExportTaskResponse(BaseModel):
    id: str
    task_type: str
    format: str
    note_id: Optional[str] = None
    status: str
    progress: float
    filename: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@router.post("", response_model=ExportTaskResponse)
async def create_export_task(
    payload: ExportTaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fmt = payload.format.lower()
    if fmt not in ("md", "pdf"):
        raise HTTPException(status_code=400, detail="Format must be 'md' or 'pdf'")
    if payload.task_type == "single":
        if not payload.note_id:
            raise HTTPException(status_code=400, detail="note_id required for single export")
    elif payload.task_type == "bulk":
        if not payload.note_ids or len(payload.note_ids) == 0:
            raise HTTPException(status_code=400, detail="note_ids required for bulk export")
    else:
        raise HTTPException(status_code=400, detail="task_type must be 'single' or 'bulk'")

    task = ExportTask(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        task_type=payload.task_type,
        format=fmt,
        note_id=payload.note_id,
        note_ids=__import__('json').dumps(payload.note_ids) if payload.note_ids else None,
        status="pending",
        progress=0.0,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return _to_response(task)


@router.get("", response_model=List[ExportTaskResponse])
async def list_export_tasks(
    status: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = select(ExportTask).where(ExportTask.user_id == current_user.id)
    if status:
        stmt = stmt.where(ExportTask.status == status)
    stmt = stmt.order_by(desc(ExportTask.created_at)).limit(limit)
    result = await db.execute(stmt)
    tasks = result.scalars().all()
    return [_to_response(t) for t in tasks]


@router.get("/{task_id}", response_model=ExportTaskResponse)
async def get_export_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(ExportTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return _to_response(task)


@router.get("/{task_id}/download")
async def download_export_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(ExportTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "completed":
        raise HTTPException(status_code=400, detail="Task not completed yet")
    if not task.file_path or not os.path.isfile(task.file_path):
        raise HTTPException(status_code=404, detail="Export file not found")

    filename = task.filename or "export"
    if task.format == "pdf":
        media_type = "application/pdf"
    elif task.task_type == "bulk":
        media_type = "application/zip"
    else:
        media_type = "text/markdown; charset=utf-8"

    encoded = quote(filename, safe='')
    ascii_fallback = f"export.{task.format}"
    return FileResponse(
        task.file_path,
        media_type=media_type,
        filename=ascii_fallback,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"
        },
    )


@router.post("/{task_id}/cancel")
async def cancel_export_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(ExportTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("pending", "claimed", "running"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel task in status '{task.status}'")
    task.status = "cancelled"
    task.completed_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "task_id": task_id, "status": "cancelled"}


@router.delete("/{task_id}")
async def delete_export_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await db.get(ExportTask, task_id)
    if task is None or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.file_path and os.path.isfile(task.file_path):
        try:
            os.remove(task.file_path)
        except Exception:
            pass
    await db.delete(task)
    await db.commit()
    return {"ok": True, "task_id": task_id}


def _to_response(task: ExportTask) -> ExportTaskResponse:
    return ExportTaskResponse(
        id=task.id,
        task_type=task.task_type,
        format=task.format,
        note_id=task.note_id,
        status=task.status,
        progress=task.progress or 0.0,
        filename=task.filename,
        error=task.error,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )
