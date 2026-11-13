# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Per-user MCP server API + global hot-reload trigger (2026-11-01).

All user endpoints are owner-scoped (404 for another user's id, never 403 with
existence leakage). The global reload endpoint is operator-only: local dev with
`super_admin_bypass` or an admin/super_admin role. Per-user CRUD applies the
change in-process immediately (service → mcp_client diff), so the next run on
this worker can use the tools without a restart.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.deps import ADMIN_ROLES, get_current_user
from app.db.database import User, get_db
from app.tools import mcp_client
from app.services import user_mcp_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get("/servers")
async def list_mcp_servers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return {"servers": await user_mcp_service.list_servers(db, current_user.id)}


@router.post("/servers")
async def create_mcp_server(
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await user_mcp_service.create_server(db, current_user.id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return result


@router.patch("/servers/{server_id}")
async def update_mcp_server(
    server_id: str,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await user_mcp_service.update_server(
            db, current_user.id, server_id, payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return result


@router.delete("/servers/{server_id}")
async def delete_mcp_server(
    server_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await user_mcp_service.delete_server(db, current_user.id, server_id)
    if not ok:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return {"ok": True}


@router.post("/servers/{server_id}/reload")
async def reload_mcp_server(
    server_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    server = await user_mcp_service.get_server(db, current_user.id, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    result = await user_mcp_service.ensure_user_mcp_tools(
        db, current_user.id, force=True,
    )
    return {**result, "server": server}


@router.post("/reload")
async def reload_global_mcp(
    current_user: User = Depends(get_current_user),
):
    cfg = get_config()
    if not (cfg.super_admin_bypass or (current_user.role or "") in ADMIN_ROLES):
        raise HTTPException(status_code=403, detail="global MCP reload is operator-only")
    result = await mcp_client.reload_mcp_servers_from_config()
    return result


@router.get("/status")
async def mcp_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cfg = get_config()
    is_operator = bool(cfg.super_admin_bypass or (current_user.role or "") in ADMIN_ROLES)
    user_servers = await user_mcp_service.list_servers(db, current_user.id)
    return {
        "global": [
            {
                "name": name,
                "transport": state.get("transport"),
                # Operator-only: URLs may embed query credentials.
                "url": state.get("url") if is_operator else None,
                "tools_count": len(state.get("tools") or []),
                "tools": sorted(state.get("tools") or []),
            }
            for name, state in sorted(mcp_client._GLOBAL_MCP_STATE.items())
        ],
        "user": user_servers,
        "hot_reload": {
            "enabled": bool(get_config().mcp_hot_reload_enabled),
            "interval_seconds": get_config().mcp_hot_reload_interval_seconds,
        },
    }
