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
    convert_to_html,
    convert_to_pdf,
    ensure_soffice,
    is_spreadsheet as is_spreadsheet_office,
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


# --- 会话作用域解析（2026-11-15 波 · A 方案）--------------------------------
# 背景：Task 作用域 enforce 下，agent 写出的相对路径（`artifacts/x.png`、
# `inputs/y.jpg`）相对的是**任务文件域根**，而本模块所有端点按**用户根**解析
# ⇒ 用户面上引用 Task 内文件的图片/媒体/链接一律 404（60 生产 2026-11-15）。
#
# 契约（§3 不变量 9 的补全，不是替代）：
#   * 解析顺序固定 **用户根优先** —— 既有能解析的路径永不变成另一个文件；
#     只有用户根解析失败时才试该会话绑定 Task 的 `workspace/` 根。
#   * 第二个根**只能由服务端从绑定推导**，绝不接受客户端传入的根；
#     会话属主 / 绑定属主 / Task 属主三重校验，任一不成立即不启用（fail-closed）。
#   * 不传 `conversation_id` ⇒ 行为与今天逐字节相同（旧客户端零影响）。


async def _task_workspace_root(
    db: AsyncSession, *, user_id: str, conversation_id: str, user_root: str | Path
) -> Optional[Path]:
    """会话当前绑定 Task 的 `workspace/` 绝对路径；任一环不成立返回 None。"""
    if not conversation_id:
        return None
    from app.db.database import Conversation, WorkspaceTask
    from app.services import task_binding_service

    conv = await db.get(Conversation, str(conversation_id))
    if conv is None or str(conv.user_id) != str(user_id):
        return None
    binding = await task_binding_service.get_binding(
        db, user_id=str(user_id), conversation_id=str(conversation_id)
    )
    task_id = getattr(binding, "task_id", None) if binding is not None else None
    if not task_id:
        return None
    task = await db.get(WorkspaceTask, str(task_id))
    if task is None or str(task.user_id) != str(user_id):
        return None
    root_rel = str(getattr(task, "root_rel", "") or "").strip()
    if not root_rel:
        return None
    candidate = Path(user_root) / root_rel / "workspace"
    try:
        resolved_root = Path(user_root).resolve()
        resolved = candidate.resolve()
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


async def _iter_scoped_roots(
    db: AsyncSession, *, user: User, workspace, conversation_id: Optional[str]
):
    """解析根序列：用户根在前，会话绑定 Task 的 workspace 根在后。

    惰性求值（2026-11-15 评审 Minor）：Task 根的三次 DB 查询只在用户根**未命中**
    时才发生——否则每个带 `conversation_id` 的图片请求都要白付 3 次查询。
    """
    yield str(workspace.root_path)
    if conversation_id:
        task_ws = await _task_workspace_root(
            db,
            user_id=str(user.id),
            conversation_id=str(conversation_id),
            user_root=workspace.root_path,
        )
        if task_ws is not None and task_ws.is_dir():
            yield str(task_ws)


async def _resolve_scoped_file(
    db: AsyncSession, *, user: User, workspace, path: str,
    conversation_id: Optional[str],
) -> Path:
    """用户根 → Task 根 依次解析一个文件；全失败抛 WorkspacePathError。"""
    last: Exception | None = None
    async for root in _iter_scoped_roots(
        db, user=user, workspace=workspace, conversation_id=conversation_id
    ):
        try:
            return await asyncio.to_thread(resolve_workspace_file, path, root)
        except (WorkspacePathError, OSError, ValueError) as exc:
            last = exc
    if isinstance(last, WorkspacePathError):
        raise last
    raise WorkspacePathError(
        "workspace_path_not_found", "file not found in workspace", path
    ) from last


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
    conversation_id: Optional[str] = Query(
        None,
        description="Optional conversation id — enables Task-workspace fallback resolution",
    ),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """Download a file from the user's workspace.

    Auth: Bearer header (standard), or ?token= query param (for <img> tags).
    Path resolution is the shared workspace resolver (no filename-walk around
    the containment/symlink rules — A4.9 Critical 2026-09-19); the response is
    pinned to the validated file descriptor.

    ``conversation_id`` (2026-11-15): when the relative path is not found under
    the user root, it is retried under that conversation's bound Task
    ``workspace/`` root (server-derived, owner-checked). Omitted ⇒ unchanged.
    """
    user = await _authenticate(credentials, token, db)

    workspace = await ensure_user_workspace(db, user.id, user.username)

    try:
        resolved = await _resolve_scoped_file(
            db, user=user, workspace=workspace, path=path,
            conversation_id=conversation_id,
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
    conversation_id: Optional[str] = Query(
        None,
        description="Optional conversation id — enables Task-workspace fallback resolution",
    ),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """List one directory inside the user's workspace (folder manager).

    2026-11-15（A）：`conversation_id` 给出时，用户根解析失败会回落到该会话绑定
    Task 的 `workspace/` 根。**返回的 `rel_path` 相对的是实际命中的那个根**
    （D-78）——调用方要把条目再喂给 `/download|/zip` 时必须带同一个
    `conversation_id`，否则 Task 相对的条目会 404。
    """
    user = await _authenticate(credentials, token, db)
    workspace = await ensure_user_workspace(db, user.id, user.username)

    last: Exception | None = None
    async for root in _iter_scoped_roots(
        db, user=user, workspace=workspace, conversation_id=conversation_id
    ):
        try:
            result = await asyncio.to_thread(list_directory, root, path)
            break
        except (WorkspacePathError, OSError, ValueError) as exc:
            last = exc
    else:
        logger.info("list rejected path=%r", path)
        raise _not_found() from last
    return result


@router.get("/zip")
async def download_zip(
    path: str = Query(..., description="Workspace-relative or in-workspace directory path"),
    token: Optional[str] = Query(None, description="JWT token for link-based auth"),
    conversation_id: Optional[str] = Query(
        None,
        description="Optional conversation id — enables Task-workspace fallback resolution",
    ),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
    background: BackgroundTasks = None,
):
    """Zip one folder inside the user's workspace and stream it as a download.

    2026-11-15（A）：`conversation_id` 语义与 `/list` 相同（用户根优先，Task 根兜底）。
    """
    user = await _authenticate(credentials, token, db)
    workspace = await ensure_user_workspace(db, user.id, user.username)

    _prune_stale_zips()
    tmp = tempfile.NamedTemporaryFile(prefix="wt_ws_zip_", suffix=".zip", delete=False)
    tmp.close()
    info = None
    last: Exception | None = None
    try:
        async for root in _iter_scoped_roots(
            db, user=user, workspace=workspace, conversation_id=conversation_id
        ):
            try:
                info = await asyncio.to_thread(build_folder_zip, root, path, tmp.name)
                break
            except (WorkspacePathError, OSError, ValueError) as exc:
                last = exc
        if info is None:
            _safe_unlink(tmp.name)
            logger.info("zip rejected path=%r", path)
            raise _not_found() from last
    except HTTPException:
        raise
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
    conversation_id: Optional[str] = Query(
        None,
        description="Optional conversation id — enables Task-workspace fallback resolution",
    ),
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
        resolved = await _resolve_scoped_file(
            db, user=user, workspace=workspace, path=path,
            conversation_id=conversation_id,
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


@router.get("/office-html")
async def office_html(
    path: str = Query(..., description="Workspace-relative or in-workspace spreadsheet path"),
    token: Optional[str] = Query(None, description="JWT token for link-based auth"),
    conversation_id: Optional[str] = Query(
        None,
        description="Optional conversation id — enables Task-workspace fallback resolution",
    ),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """Convert a spreadsheet to a sanitized, self-contained HTML preview.

    2026-09-21: PDF pagination split wide tables across pages and clipped
    long cell text at page boundaries; the Calc HTML export keeps every sheet
    as one natural-width table (horizontal scroll in the viewer) with wrapped
    text and charts inlined as images. The document is sanitized server-side
    and served to a sandboxed iframe. ``400`` for non-spreadsheets, ``501``
    when LibreOffice/preview is unavailable and ``422`` on conversion failure
    — the frontend then falls back to ``/office-pdf`` and finally to its
    client-side renderer.
    """
    user = await _authenticate(credentials, token, db)
    workspace = await ensure_user_workspace(db, user.id, user.username)

    try:
        resolved = await _resolve_scoped_file(
            db, user=user, workspace=workspace, path=path,
            conversation_id=conversation_id,
        )
    except (WorkspacePathError, OSError, ValueError) as exc:
        logger.info("office-html rejected path=%r", path)
        raise _not_found() from exc

    if not is_spreadsheet_office(resolved.name):
        raise HTTPException(status_code=400, detail="Not a spreadsheet file")

    if not config.office_preview_enabled or not config.office_preview_html_enabled:
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

    try:
        fd = await asyncio.to_thread(_open_regular_nofollow, resolved)
    except OSError as exc:
        logger.info("office-html rejected (open) path=%r: %s", path, exc)
        raise _not_found() from exc
    try:
        src_stat = os.fstat(fd)
        tmp_src = await asyncio.to_thread(
            _spool_fd_to_temp, fd, resolved.suffix, config.office_preview_cache_dir
        )
    except OSError as exc:
        logger.warning("office-html spool failed path=%r: %s", path, exc)
        raise HTTPException(status_code=500, detail="Preview preparation failed") from exc
    finally:
        os.close(fd)

    try:
        html_path = await convert_to_html(
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
        path=str(html_path),
        media_type="text/html",
        headers={
            "X-Content-Type-Options": "nosniff",
            # direct/top-level opens of the token URL get the same lockdown as
            # the sandboxed iframe (A4.9 2026-09-21)
            "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'",
        },
    )
