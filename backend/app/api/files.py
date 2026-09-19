# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Secure file API for the user workspace (download / list / zip / office-pdf).

All endpoints authenticate per user and resolve every path through
``app.services.workspace_paths`` — only files and folders inside the
requesting user's workspace are reachable. Invalid and out-of-workspace
targets share one not-found response so paths cannot be probed.
"""
import asyncio
import logging
import mimetypes
import os
import stat as stat_module
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.database import get_db, User
from app.core.deps import get_user_from_token
from app.core.config import get_config
from app.services.workspace_service import ensure_user_workspace
from app.services.workspace_paths import WorkspacePathError, resolve_workspace_file
from app.services.workspace_files_service import build_folder_zip, list_directory
from app.services.office_preview_service import (
    OfficePreviewFailed,
    OfficePreviewUnavailable,
    convert_to_pdf,
    ensure_soffice,
    is_supported as is_supported_office,
)

config = get_config()
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["files"])

_MIME_OVERRIDES = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".py": "text/x-python; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _get_media_type(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    mt, _ = mimetypes.guess_type(filepath)
    return mt or "application/octet-stream"


_bearer = HTTPBearer(auto_error=False)


async def _authenticate(
    credentials: Optional[HTTPAuthorizationCredentials],
    token: Optional[str],
    db: AsyncSession,
) -> User:
    """Bearerv header (standard) or ``?token=`` (for img/iframe/anchors).

    A valid query token still authenticates when a stale Bearer header is
    sent alongside it (2026-09-19 A4.9 minor).
    """
    user: User | None = None
    if credentials:
        try:
            user = await get_user_from_token(credentials.credentials, db)
        except HTTPException as exc:
            # Only an invalid/expired Bearer may fall back to ?token=
            # (inactive-user 403 keeps its status; A4.9 R2 minor).
            if exc.status_code != 401:
                raise
            user = None
    if user is None and token:
        try:
            user = await get_user_from_token(token, db)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            user = None
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _open_regular_nofollow(path: Path) -> int:
    """Open *path* read-only refusing to follow a final symlink.

    Returns an fd whose fstat is a regular file; callers keep it open for the
    whole response so a concurrent workspace process cannot swap the file for
    an escaping symlink between validation and read (A4.9 TOCTOU).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {path}")
    except OSError:
        os.close(fd)
        raise
    return fd


def _frozen_response_path(fd: int, fallback: Path) -> str:
    """Path for FileResponse that reuses the validated fd (keeps Range support).

    ``/dev/fd/<fd>`` resolves to the already-open file, so the response cannot
    be redirected by a later symlink swap; falls back to the resolved path on
    platforms without /dev/fd.
    """
    if os.path.isdir("/dev/fd"):
        return f"/dev/fd/{fd}"
    return str(fallback)


def _spool_fd_to_temp(fd: int, suffix: str, cache_dir: str | Path) -> Path:
    """Copy an open fd into a private temp file (for subprocess consumers)."""
    target_dir = Path(cache_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target_dir, 0o700)
    except OSError:
        pass
    handle = tempfile.NamedTemporaryFile(
        prefix="wt_office_src_", suffix=suffix, dir=str(target_dir), delete=False
    )
    try:
        with handle:
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except OSError:
        _safe_unlink(handle.name)
        raise
    return Path(handle.name)


def _not_found() -> HTTPException:
    """Uniform not-found response for invalid and out-of-workspace targets."""
    return HTTPException(status_code=404, detail="File not found in workspace")


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _prune_stale_zips(max_age_seconds: int = 3600) -> None:
    """Best-effort cleanup of temp archives left by crashed/aborted requests."""
    import glob
    import time

    cutoff = time.time() - max_age_seconds
    for stale in glob.glob(os.path.join(tempfile.gettempdir(), "wt_ws_zip_*.zip")):
        try:
            if os.path.getmtime(stale) < cutoff:
                os.unlink(stale)
        except OSError:
            continue


@router.get("/download")
async def download_file(
    path: str = Query(..., description="Absolute path or workspace-relative path to the file"),
    token: Optional[str] = Query(None, description="JWT token for <img> tag auth"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """Download a file from the user's workspace.

    Auth: Bearer header (standard), or ?token= query param (for <img> tags).
    Path resolution is the shared workspace resolver (no filename-walk around
    the containment/symlink rules — A4.9 Critical 2026-09-19); the response is
    pinned to the validated file descriptor.
    """
    user = await _authenticate(credentials, token, db)

    workspace = await ensure_user_workspace(db, user.id, user.username)

    try:
        resolved = await asyncio.to_thread(
            resolve_workspace_file, path, workspace.root_path
        )
    except (WorkspacePathError, OSError, ValueError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        logger.info("download rejected path=%r (%s)", path, code)
        raise _not_found() from exc

    try:
        fd = await asyncio.to_thread(_open_regular_nofollow, resolved)
    except OSError as exc:
        logger.info("download rejected (open) path=%r: %s", path, exc)
        raise _not_found() from exc

    response = FileResponse(
        path=_frozen_response_path(fd, resolved),
        filename=resolved.name,
        media_type=_get_media_type(str(resolved)),
    )
    response.background = BackgroundTask(os.close, fd)
    return response


@router.get("/list")
async def list_files(
    path: str = Query(..., description="Workspace-relative or in-workspace directory path"),
    token: Optional[str] = Query(None, description="JWT token for link-based auth"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """List one directory inside the user's workspace (folder manager)."""
    user = await _authenticate(credentials, token, db)
    workspace = await ensure_user_workspace(db, user.id, user.username)

    try:
        result = await asyncio.to_thread(list_directory, workspace.root_path, path)
    except (WorkspacePathError, OSError, ValueError) as exc:
        logger.info("list rejected path=%r", path)
        raise _not_found() from exc
    return result


@router.get("/zip")
async def download_zip(
    path: str = Query(..., description="Workspace-relative or in-workspace directory path"),
    token: Optional[str] = Query(None, description="JWT token for link-based auth"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
    background: BackgroundTasks = None,
):
    """Zip one folder inside the user's workspace and stream it as a download."""
    user = await _authenticate(credentials, token, db)
    workspace = await ensure_user_workspace(db, user.id, user.username)

    _prune_stale_zips()
    tmp = tempfile.NamedTemporaryFile(prefix="wt_ws_zip_", suffix=".zip", delete=False)
    tmp.close()
    try:
        info = await asyncio.to_thread(build_folder_zip, workspace.root_path, path, tmp.name)
    except (WorkspacePathError, OSError, ValueError) as exc:
        _safe_unlink(tmp.name)
        logger.info("zip rejected path=%r", path)
        raise _not_found() from exc
    except Exception as exc:  # noqa: BLE001 - surface as 500, never leak internals
        _safe_unlink(tmp.name)
        logger.exception("zip failed path=%r", path)
        raise HTTPException(status_code=500, detail="Archive failed") from exc

    if background is not None:
        background.add_task(_safe_unlink, tmp.name)
    return FileResponse(
        path=tmp.name,
        media_type="application/zip",
        filename=info["name"],
    )


@router.get("/office-pdf")
async def office_pdf(
    path: str = Query(..., description="Workspace-relative or in-workspace office file path"),
    token: Optional[str] = Query(None, description="JWT token for link-based auth"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """Convert an Office file to PDF for inline preview (server-side.

    ``501 office_preview_unavailable`` when LibreOffice is missing — the
    frontend then falls back to its client-side renderers.
    """
    user = await _authenticate(credentials, token, db)
    workspace = await ensure_user_workspace(db, user.id, user.username)

    try:
        resolved = await asyncio.to_thread(
            resolve_workspace_file, path, workspace.root_path
        )
    except (WorkspacePathError, OSError, ValueError) as exc:
        logger.info("office-pdf rejected path=%r", path)
        raise _not_found() from exc

    if not is_supported_office(resolved.name):
        raise HTTPException(status_code=400, detail="Unsupported office file type")

    if not config.office_preview_enabled:
        raise HTTPException(
            status_code=501,
            detail={"code": "office_preview_unavailable", "message": "Office preview conversion is disabled"},
        )

    soffice = await ensure_soffice(config.office_preview_soffice_path)
    if not soffice:
        raise HTTPException(
            status_code=501,
            detail={"code": "office_preview_unavailable", "message": "LibreOffice is not installed"},
        )

    # Freeze the source bytes through a no-follow fd before handing anything
    # to the converter subprocess (TOCTOU), then clean the copy up.
    try:
        fd = await asyncio.to_thread(_open_regular_nofollow, resolved)
    except OSError as exc:
        logger.info("office-pdf rejected (open) path=%r: %s", path, exc)
        raise _not_found() from exc
    try:
        src_stat = os.fstat(fd)
        tmp_src = await asyncio.to_thread(
            _spool_fd_to_temp, fd, resolved.suffix, config.office_preview_cache_dir
        )
    except OSError as exc:
        logger.warning("office-pdf spool failed path=%r: %s", path, exc)
        raise HTTPException(status_code=500, detail="Preview preparation failed") from exc
    finally:
        os.close(fd)

    try:
        pdf_path = await convert_to_pdf(
            tmp_src,
            soffice=soffice,
            cache_dir=config.office_preview_cache_dir,
            timeout=config.office_preview_timeout_seconds,
            max_cache_mb=config.office_preview_cache_max_mb,
            cache_identity=str(resolved),
            cache_stat=(src_stat.st_mtime_ns, src_stat.st_size),
        )
    except OfficePreviewUnavailable as exc:
        raise HTTPException(
            status_code=501,
            detail={"code": "office_preview_unavailable", "message": str(exc)[:200]},
        ) from exc
    except OfficePreviewFailed as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "office_preview_failed", "message": str(exc)[:200]},
        ) from exc
    finally:
        _safe_unlink(str(tmp_src))

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=resolved.with_suffix(".pdf").name,
        content_disposition_type="inline",
    )
