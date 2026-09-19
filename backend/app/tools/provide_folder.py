# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""provide_folder tool — attach existing workspace folders as folder cards.

Companion of ``provide_file`` (2026-09-19): the agent explicitly offers one
or more folders; the chat UI renders a folder card that opens a read-only
folder manager (browse / preview / download file / download folder as zip).

Security model mirrors ``provide_file``: resolution is delegated to
``app.services.workspace_paths`` and only directories inside the requesting
user's workspace are ever returned — absolute paths outside, ``..`` traversal
and escaping symlinks are rejected by the system layer.  Cards carry a
workspace-relative ``rel_path`` (never the full system path); directory
statistics skip symlinks entirely.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.tools.registry import registry
from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_MAX_WALK_ENTRIES = 20_000


def _dir_stats(directory: Path) -> Tuple[int, int]:
    """Return ``(total_size, file_count)`` for regular files under *directory*.

    Symlinked directories are not descended and symlinked files are not
    counted (defense in depth against escape links).  Bounded by
    ``_MAX_WALK_ENTRIES`` so a pathological tree cannot stall the tool.
    """
    total = 0
    count = 0
    seen = 0
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [
            d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
        ]
        for fn in filenames:
            seen += 1
            if seen > _MAX_WALK_ENTRIES:
                return total, count
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            total += st.st_size
            count += 1
    return total, count


async def provide_folder(args: Dict[str, Any], **kwargs) -> str:
    """Attach one or more existing workspace folders as folder cards."""
    user = kwargs.get("user")
    db = kwargs.get("db")
    if user is None or db is None:
        return json.dumps(
            {"error": "用户上下文缺失，无法定位工作区", "generated_folders": []},
            ensure_ascii=False,
        )

    from app.services.workspace_service import ensure_user_workspace
    from app.services.workspace_paths import (
        WorkspacePathError,
        resolve_workspace_dir,
        to_rel_path,
    )

    workspace = await ensure_user_workspace(db, user.id, getattr(user, "username", None))
    workspace_root = str(Path(workspace.root_path).resolve())

    # Accept a single folder_path string and/or a folders list.
    raw_folders: List[str] = []
    single = args.get("folder_path") or args.get("path")
    if single:
        raw_folders.append(single)
    folders_list = args.get("folders")
    if isinstance(folders_list, list):
        raw_folders.extend(str(f) for f in folders_list)

    if not raw_folders:
        return json.dumps(
            {"error": "未提供文件夹路径。请通过 folder_path 或 folders 参数指定要提供的文件夹。", "generated_folders": []},
            ensure_ascii=False,
        )

    generated: List[Dict[str, Any]] = []
    not_found: List[str] = []
    for target in raw_folders:
        target = str(target).strip().strip("'\"")
        if not target:
            continue
        try:
            resolved = await asyncio.to_thread(
                resolve_workspace_dir, target, workspace_root
            )
        except WorkspacePathError as exc:
            not_found.append(target)
            logger.info("provide_folder: rejected %r (%s)", target, exc.code)
            continue
        try:
            rel_path = to_rel_path(resolved, workspace_root)
        except WorkspacePathError as exc:  # pragma: no cover - resolver guarantees inside
            not_found.append(target)
            logger.warning("provide_folder: rel path failed for %r (%s)", target, exc.code)
            continue
        if not rel_path:
            # The workspace root itself is not a provideable folder card
            # (empty rel_path cannot be browsed/downloaded — A4.9 minor).
            not_found.append(target)
            logger.info("provide_folder: workspace root is not provideable")
            continue
        size, file_count = await asyncio.to_thread(_dir_stats, resolved)
        generated.append({
            "name": resolved.name,
            "rel_path": rel_path,
            "size": size,
            "file_count": file_count,
            "type": "folder",
        })
        logger.info(
            "provide_folder: attached %s (%d files, %d bytes) at %s",
            resolved.name, file_count, size, rel_path,
        )

    summary_parts: List[str] = []
    if generated:
        names = ", ".join(g["name"] for g in generated)
        summary_parts.append(f"已提供 {len(generated)} 个文件夹作为文件夹卡片：{names}")
    if not_found:
        summary_parts.append(
            f"未找到的文件夹：{', '.join(not_found)}（只能提供当前用户工作区内的文件夹）"
        )
    if not generated:
        summary_parts.append("没有可提供的文件夹。请确认路径正确且文件夹位于用户工作区内。")

    return json.dumps(
        {
            "success": len(generated) > 0,
            "message": "；".join(summary_parts),
            "generated_folders": generated,
            "not_found": not_found,
        },
        ensure_ascii=False,
    )


registry.register(
    name="provide_folder",
    toolset="files",
    # 显式提供文件夹：文件夹卡片的唯一来源（与 provide_file 同契约）
    produces_files=True,
    schema={
        "name": "provide_folder",
        "description": (
            "将用户工作区中已存在的文件夹作为文件夹卡片提供给用户。"
            "当用户要求'把整个文件夹给我'、'打包这个目录'、'提供这个文件夹'，"
            "或交付物本身是一个目录（如一个项目/一组文件）时，调用此工具。"
            "卡片打开后可浏览文件夹内文件、预览与下载单个文件、下载整个文件夹（服务器打包 zip）。"
            "支持绝对路径、工作区相对路径；可同时提供多个文件夹（folders 参数）。"
            "安全边界：只能提供当前用户工作区内的文件夹；工作区外的系统目录或其他用户"
            "目录即使被要求也会被系统拒绝，不要尝试提供。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {
                    "type": "string",
                    "description": "单个文件夹的路径（绝对路径或工作区相对路径）。与 folders 二选一。",
                },
                "folders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "多个文件夹路径列表。当需要同时提供多个文件夹时使用。",
                },
            },
        },
    },
    handler=provide_folder,
    is_async=True,
    description="Attach existing workspace folders as folder cards",
    emoji="",
)
