# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import UserWorkspace

config = get_config()


def _slugify(value: Optional[str]) -> str:
    if not value:
        return "user"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-")
    return slug or "user"


def _workspace_metadata() -> str:
    metadata = {
        "layout": ["files", "artifacts", "scratch"],
        "bootstrap": "project-venv" if config.workspace_use_project_venv else "isolated-dir",
    }
    return json.dumps(metadata, ensure_ascii=False)


def _ensure_workspace_directories(root_path: Path) -> None:
    root_path.mkdir(parents=True, exist_ok=True)
    for child in ("files", "artifacts", "scratch"):
        (root_path / child).mkdir(exist_ok=True)

    if config.workspace_create_readme:
        readme_path = root_path / "README.md"
        if not readme_path.exists():
            readme_path.write_text(
                "# User Workspace\n\n"
                "This workspace is reserved for user-level agent tasks.\n",
                encoding="utf-8",
            )


async def ensure_user_workspace(
    db: AsyncSession,
    user_id: str,
    username: Optional[str] = None,
) -> UserWorkspace:
    result = await db.execute(select(UserWorkspace).where(UserWorkspace.user_id == user_id))
    workspace = result.scalar_one_or_none()

    workspace_name = f"{_slugify(username)}-{user_id[:8]}"
    root_path = (config.workspace_root / workspace_name).resolve()
    _ensure_workspace_directories(root_path)

    python_env_path = str((config.project_root / ".venv").resolve()) if config.workspace_use_project_venv else None

    if workspace is None:
        workspace = UserWorkspace(
            user_id=user_id,
            root_path=str(root_path),
            python_env_path=python_env_path,
            node_workspace_path=str(root_path),
            metadata_json=_workspace_metadata(),
        )
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)
        return workspace

    workspace.root_path = str(root_path)
    workspace.python_env_path = python_env_path
    workspace.node_workspace_path = str(root_path)
    if not workspace.metadata_json:
        workspace.metadata_json = _workspace_metadata()
    await db.commit()
    await db.refresh(workspace)
    return workspace