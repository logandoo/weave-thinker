# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Image upload API for note images."""
import asyncio
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db, User
from app.core.deps import get_current_user
from app.services.workspace_service import ensure_user_workspace

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/images", tags=["images"])

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".avi"}
MAX_AUDIO_SIZE = 50 * 1024 * 1024
MAX_VIDEO_SIZE = 200 * 1024 * 1024


@router.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="图片过大，最大支持 10MB")

    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的图片格式")

    workspace = await ensure_user_workspace(db, current_user.id, current_user.username)
    images_dir = Path(workspace.root_path) / "noteimg"
    await asyncio.to_thread(images_dir.mkdir, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{suffix}"
    file_path = images_dir / filename
    await asyncio.to_thread(file_path.write_bytes, content)

    rel_path = f"noteimg/{filename}"
    return {"path": rel_path, "filename": filename, "size": len(content)}


@router.post("/upload-media")
async def upload_note_media(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload an audio/video file into the note media store (noteimg/).

    Returns the same shape as /upload so the frontend insert pipeline is
    identical. Audio ≤50MB, video ≤200MB. Size caps are enforced WHILE
    streaming (never buffer the whole upload into memory first — a
    multi-GB file must be rejected without OOMing the backend).
    """
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix in AUDIO_EXTENSIONS:
        cap = MAX_AUDIO_SIZE
    elif suffix in VIDEO_EXTENSIONS:
        cap = MAX_VIDEO_SIZE
    else:
        raise HTTPException(status_code=400, detail="不支持的音视频格式")

    # Reject oversized uploads by declared Content-Length before reading.
    declared = file.size or 0
    if declared > cap:
        kind = "音频" if suffix in AUDIO_EXTENSIONS else "视频"
        raise HTTPException(
            status_code=400,
            detail=f"{kind}过大，最大支持 {cap // (1024 * 1024)}MB",
        )

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            kind = "音频" if suffix in AUDIO_EXTENSIONS else "视频"
            raise HTTPException(
                status_code=400,
                detail=f"{kind}过大，最大支持 {cap // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    content = b"".join(chunks)

    workspace = await ensure_user_workspace(db, current_user.id, current_user.username)
    images_dir = Path(workspace.root_path) / "noteimg"
    await asyncio.to_thread(images_dir.mkdir, exist_ok=True)

    filename = f"{uuid.uuid4().hex}{suffix}"
    file_path = images_dir / filename
    await asyncio.to_thread(file_path.write_bytes, content)

    rel_path = f"noteimg/{filename}"
    return {"path": rel_path, "filename": filename, "size": len(content)}


@router.get("/serve")
async def serve_note_image(
    path: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    workspace = await ensure_user_workspace(db, current_user.id, current_user.username)
    workspace_root = str(Path(workspace.root_path).resolve())

    resolved = str((Path(workspace.root_path) / path).resolve())
    if not resolved.startswith(workspace_root + os.sep):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")

    ext = os.path.splitext(resolved)[1].lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".bmp": "image/bmp",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac",
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".m4v": "video/x-m4v", ".avi": "video/x-msvideo",
    }
    return FileResponse(resolved, media_type=media_types.get(ext, "application/octet-stream"))
