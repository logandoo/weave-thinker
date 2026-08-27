# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Secure file download API for agent-generated files."""
import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.db.database import get_db, User
from app.core.deps import get_user_from_token
from app.core.config import get_config
from app.services.workspace_service import ensure_user_workspace

config = get_config()
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


@router.get("/download")
async def download_file(
    path: str = Query(..., description="Absolute path to the file"),
    token: Optional[str] = Query(None, description="JWT token for <img> tag auth"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
):
    """Download a file from the user's workspace.
    
    Auth: Bearer header (standard), or ?token= query param (for <img> tags).
    """
    user: User | None = None
    
    if credentials:
        user = await get_user_from_token(credentials.credentials, db)
    elif token:
        user = await get_user_from_token(token, db)
    
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    workspace = await ensure_user_workspace(db, user.id, user.username)
    workspace_root = str(Path(workspace.root_path).resolve())

    resolved = str(Path(path).resolve())
    is_within = resolved.startswith(workspace_root + os.sep) or resolved == workspace_root

    if not (is_within and os.path.isfile(resolved)) and not os.path.isabs(path):
        # Workspace-relative path (e.g. media/<sha256>.jpg from localized
        # answers): resolve directly against the workspace root before
        # falling back to the recursive filename walk.
        candidate = str((Path(workspace_root) / path).resolve())
        if candidate.startswith(workspace_root + os.sep) and os.path.isfile(candidate):
            resolved = candidate
            is_within = True

    if is_within and os.path.isfile(resolved):
        pass
    else:
        # Try filename-based search within workspace
        target_name = os.path.basename(resolved)
        if not target_name:
            raise HTTPException(status_code=400, detail="Invalid file path")
        found = None
        for dirpath, dirnames, filenames in os.walk(workspace_root):
            for fn in filenames:
                if fn == target_name:
                    candidate = os.path.join(dirpath, fn)
                    if os.path.isfile(candidate):
                        found = candidate
                        break
            if found:
                break
        if found:
            resolved = found
        elif is_within:
            raise HTTPException(status_code=404, detail="File not found")
        else:
            raise HTTPException(status_code=404, detail="File not found in workspace")

    filename = os.path.basename(resolved)
    media_type = _get_media_type(resolved)
    return FileResponse(
        path=resolved,
        filename=filename,
        media_type=media_type,
    )
