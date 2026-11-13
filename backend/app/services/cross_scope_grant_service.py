# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""跨域 capability grant（2026-11-10 波 · T5）。

设计契约 §7.5 / §11.2：

* claims 由**服务端**按 canonical payload 落库并哈希；agent 只能引用 grant id，
  不能通过参数（`approved=true` 等）自行铸造或扩权；
* files / memory / session / legacy / catalog 一律只读；MCP 另需声明 server/tool/action，
  执行时仍过 act gate；
* 过期、撤销、改绑（binding_version 漂移）、attempt 更替（run/attempt/lease_epoch 漂移）、
  来源 Task 归档 → 立即失效；
* 每次放行都写审计（grant id + 实际路径/工具 + 计数）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from app.db.database import WorkspaceCrossScopeGrant
from app.services import workspace_scope as ws

logger = logging.getLogger(__name__)

CODE_GRANT_UNAUTHORIZED = "workspace_grant_unauthorized"
CODE_GRANT_EXPIRED = "workspace_grant_expired"
CODE_GRANT_REVOKED = "workspace_grant_revoked"
CODE_GRANT_TAMPERED = "workspace_grant_tampered"

GRANT_KINDS = ("files", "memory", "session", "legacy", "catalog", "mcp")
READ_ONLY_KINDS = ("files", "memory", "session", "legacy", "catalog")
USER_ISSUERS = ("user", "ui")
MAX_LIFETIME_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class GrantCheck:
    ok: bool
    code: str | None = None
    grant: WorkspaceCrossScopeGrant | None = None
    message: str = ""


def _canonical_claims(*, user_id: str, current_task_id: str, kind: str, mode: str,
                      source_project_id: str | None, source_task_id: str | None,
                      allowed_paths: Sequence[str], server_tool: str | None,
                      allowed_actions: Sequence[str], run_id: str | None,
                      attempt_id: str | None, lease_epoch: int | None,
                      binding_version: int | None, not_after: datetime | None,
                      issuer: str) -> dict[str, Any]:
    """canonical claims：字段齐全、键序稳定，执行时逐字段校验（哈希只作完整性证明）。"""
    return {
        "version": 1,
        "user_id": str(user_id),
        "current_task_id": str(current_task_id),
        "kind": kind,
        "mode": mode,
        "source_project_id": source_project_id,
        "source_task_id": source_task_id,
        "allowed_paths": sorted({str(p) for p in allowed_paths}),
        "server_tool": server_tool,
        "allowed_actions": sorted({str(a) for a in allowed_actions}),
        "run_id": run_id,
        "attempt_id": attempt_id,
        "lease_epoch": lease_epoch,
        "binding_version": binding_version,
        "not_after": not_after.isoformat(timespec="seconds") + "Z" if not_after else None,
        "issuer": issuer,
    }


def _claims_hash(claims: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(claims, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


async def mint_grant(db, *, user_id: str, current_task_id: str, kind: str, mode: str = "read",
                     source_project_id: str | None = None, source_task_id: str | None = None,
                     allowed_paths: Sequence[str] = (), server_tool: str | None = None,
                     allowed_actions: Sequence[str] = (), not_after: datetime | None = None,
                     ttl_seconds: int | None = None, run_id: str | None = None,
                     attempt_id: str | None = None, lease_epoch: int | None = None,
                     binding_version: int | None = None, issuer: str = "user") -> WorkspaceCrossScopeGrant:
    """签发 grant。**只允许 user/ui 触发**（UI 来源选择器或一次性权限提示）。"""
    if str(issuer) not in USER_ISSUERS:
        raise ws.WorkspaceScopeError(
            CODE_GRANT_UNAUTHORIZED,
            "grants must be issued by the user or the UI, never by the agent", str(issuer))
    if kind not in GRANT_KINDS:
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID, "unknown grant kind", str(kind))
    if str(mode) != "read":
        # 跨域写入一律走 import/publish/merge 的另行授权（§7.2）
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "cross-scope grants are read-only", str(mode))
    if kind in ("files", "legacy") and not tuple(p for p in allowed_paths if str(p).strip()):
        # 空 allowed_paths 的 files/legacy grant = 整个根，签发面就拒绝
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     f"{kind} grant requires allowed_paths", kind)
    if kind == "mcp" and not (server_tool and tuple(allowed_actions)):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "mcp grant requires server_tool and allowed_actions", kind)
    if kind != "mcp" and (server_tool or allowed_actions):
        raise ws.WorkspaceScopeError(ws.CODE_RELATION_INVALID,
                                     "server_tool/allowed_actions are mcp-only", kind)
    if not_after is None:
        not_after = datetime.utcnow() + timedelta(seconds=min(int(ttl_seconds or 3600),
                                                               MAX_LIFETIME_SECONDS))
    clean_paths = tuple(str(p).strip().strip("/") for p in allowed_paths if str(p).strip())

    claims = _canonical_claims(
        user_id=user_id, current_task_id=current_task_id, kind=kind, mode="read",
        source_project_id=source_project_id, source_task_id=source_task_id,
        allowed_paths=clean_paths, server_tool=server_tool,
        allowed_actions=tuple(allowed_actions), run_id=run_id, attempt_id=attempt_id,
        lease_epoch=lease_epoch, binding_version=binding_version, not_after=not_after,
        issuer=str(issuer))
    grant = WorkspaceCrossScopeGrant(
        user_id=user_id, current_task_id=current_task_id, source_project_id=source_project_id,
        source_task_id=source_task_id, kind=kind, mode="read",
        claims_json=json.dumps(claims, ensure_ascii=False, sort_keys=True),
        claims_hash=_claims_hash(claims), issuer=str(issuer), status="active",
        run_id=run_id, attempt_id=attempt_id, lease_epoch=lease_epoch,
        binding_version=binding_version, not_after=not_after, audit_count=0,
    )
    db.add(grant)
    await db.flush()
    logger.info("workspace cross-scope grant minted: id=%s kind=%s task=%s source=%s",
                grant.id, kind, current_task_id, source_task_id)
    return grant


async def verify_grant(db, grant_id: str, *, ctx: ws.WorkspaceContext, kind: str,
                       mode: str = "read", path: str | None = None, tool: str | None = None,
                       action: str | None = None, requested_task_id: str | None = None,
                       requested_project_id: str | None = None,
                       count_audit: bool = True) -> GrantCheck:
    """按 canonical claims 逐字段校验；命中即记审计。"""
    res = await db.execute(
        select(WorkspaceCrossScopeGrant).where(WorkspaceCrossScopeGrant.id == grant_id))
    grant = res.scalar_one_or_none()
    if grant is None:
        # 不存在的 id 与「别人的 id」都拒绝；后者额外 raise 供审计定位越权尝试
        raise ws.WorkspaceScopeError(ws.CODE_CROSS_SCOPE_DENIED,
                                     "grant not found", str(grant_id))
    if str(grant.user_id) != str(ctx.user_id):
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "grant belongs to another user",
                                     str(grant_id))

    def denied(code: str, message: str) -> GrantCheck:
        logger.info("workspace grant denied: id=%s code=%s reason=%s", grant_id, code, message)
        return GrantCheck(ok=False, code=code, grant=grant, message=message)

    if str(grant.status or "active") != "active":
        return denied(CODE_GRANT_REVOKED, f"grant is {grant.status}")
    if grant.not_after is not None and grant.not_after <= datetime.utcnow():
        grant.status = "expired"
        return denied(CODE_GRANT_EXPIRED, "grant lifetime elapsed")
    try:
        claims = json.loads(grant.claims_json or "{}")
    except ValueError:
        claims = {}
    if _claims_hash(claims) != str(grant.claims_hash):
        return denied(CODE_GRANT_TAMPERED, "claims payload does not match its hash")
    if str(claims.get("kind") or "") != str(kind):
        return denied(ws.CODE_CROSS_SCOPE_DENIED, f"grant kind is {claims.get('kind')}, not {kind}")
    if str(claims.get("mode") or "read") != str(mode or "read"):
        return denied(ws.CODE_CROSS_SCOPE_DENIED, "grant mode mismatch")
    if str(claims.get("current_task_id") or "") != str(ctx.task_id):
        return denied(ws.CODE_CROSS_SCOPE_DENIED, "grant was issued for a different current task")
    if claims.get("binding_version") is not None and \
            int(claims["binding_version"]) != int(ctx.binding_version):
        return denied(ws.CODE_CROSS_SCOPE_DENIED, "binding_version drifted since issuance")
    for field_name, value in (("run_id", ctx.run_id), ("attempt_id", ctx.attempt_id),
                              ("lease_epoch", ctx.lease_epoch)):
        want = claims.get(field_name)
        if want is not None and str(want) != str(value):
            return denied(ws.CODE_CROSS_SCOPE_DENIED, f"{field_name} drifted since issuance")

    if kind in READ_ONLY_KINDS and str(mode or "read") != "read":
        return denied(ws.CODE_CROSS_SCOPE_DENIED, f"{kind} grants are read-only")

    # grant 只解锁它签发给的那个资源：请求别的 task/project 一律拒绝
    if requested_task_id and claims.get("source_task_id") and \
            str(claims["source_task_id"]) != str(requested_task_id):
        return denied(ws.CODE_CROSS_SCOPE_DENIED,
                      "grant was issued for a different source task")
    if requested_task_id and not claims.get("source_task_id"):
        return denied(ws.CODE_CROSS_SCOPE_DENIED, "grant has no source task binding")
    if requested_project_id and claims.get("source_project_id") and \
            str(claims["source_project_id"]) != str(requested_project_id):
        return denied(ws.CODE_CROSS_SCOPE_DENIED,
                      "grant was issued for a different source project")
    if kind in ("files", "memory", "session"):
        # 源绑定可以是任务（跨任务读）或项目（shared/memory 读）；两者皆空 = 整个账号，拒签
        if not (claims.get("source_task_id") or claims.get("source_project_id")):
            return denied(ws.CODE_CROSS_SCOPE_DENIED, "grant has no source binding")
        if claims.get("source_task_id"):
            from app.services import workspace_task_service as svc

            source = await svc.get_task(db, user_id=ctx.user_id,
                                        task_id=str(claims["source_task_id"]))
            if source is None:
                return denied(CODE_GRANT_EXPIRED, "source task no longer exists")
            if str(source.status or "active") == "archived":
                return denied(CODE_GRANT_EXPIRED, "source task archived")

    if kind in ("files", "legacy", "memory") and kind != "catalog":
        allowed = [str(p) for p in (claims.get("allowed_paths") or [])]
        if not allowed:
            return denied(ws.CODE_CROSS_SCOPE_DENIED,
                          "grant carries no allowed_paths; refusing the whole root")
        if not path:
            return denied(ws.CODE_CROSS_SCOPE_DENIED,
                          "cross-scope read needs an explicit target path to check")
        if not _path_allowed(allowed, path):
            return denied(ws.CODE_CROSS_SCOPE_DENIED, f"path {path!r} not in allowed paths")

    if kind == "mcp":
        if str(claims.get("server_tool") or "") != str(tool or ""):
            return denied(ws.CODE_CROSS_SCOPE_DENIED, "server tool not covered by this grant")
        actions = {str(a) for a in (claims.get("allowed_actions") or [])}
        if str(action or "") not in actions:
            return denied(ws.CODE_CROSS_SCOPE_DENIED, "action not covered by this grant")

    if count_audit:
        # 展示性复检（badge 元数据解析）不计数/不覆盖 last_audit，
        # 否则每交付 +2 失真审计口径（评审 r2 #13）
        grant.audit_count = int(grant.audit_count or 0) + 1
        grant.last_audit_json = json.dumps({
            "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "kind": kind, "path": path, "tool": tool, "action": action,
            "requested_task_id": requested_task_id, "requested_project_id": requested_project_id,
            "result_sha256": hashlib.sha256(
            f"{path or ''}|{tool or ''}|{action or ''}".encode("utf-8")).hexdigest(),
            "task_id": ctx.task_id, "run_id": ctx.run_id, "attempt_id": ctx.attempt_id,
            "lease_epoch": ctx.lease_epoch, "binding_version": ctx.binding_version,
        }, ensure_ascii=False, sort_keys=True)
    await db.flush()
    return GrantCheck(ok=True, grant=grant, message="ok")


def _path_allowed(allowed: Sequence[str], target: str) -> bool:
    """精确文件或目录前缀；`**` / `*` 通配只允许目录粒度的 `dir/**`。"""
    clean = str(target or "").strip().strip("/")
    for entry in allowed:
        want = str(entry).strip().strip("/")
        if not want:
            continue
        if want.endswith("/**"):
            if clean == want[:-3] or clean.startswith(want[:-3] + "/"):
                return True
        elif clean == want or clean.startswith(want + "/"):
            return True
    return False


async def revoke_grant(db, *, user_id: str, grant_id: str) -> WorkspaceCrossScopeGrant:
    res = await db.execute(
        select(WorkspaceCrossScopeGrant).where(WorkspaceCrossScopeGrant.id == grant_id,
                                              WorkspaceCrossScopeGrant.user_id == user_id))
    grant = res.scalar_one_or_none()
    if grant is None:
        raise ws.WorkspaceScopeError(ws.CODE_FOREIGN_SCOPE, "grant not found for this user", grant_id)
    grant.status = "revoked"
    grant.revoked_at = datetime.utcnow()
    await db.flush()
    return grant


async def list_grants(db, *, user_id: str, task_id: str | None = None,
                      include_inactive: bool = False) -> list[WorkspaceCrossScopeGrant]:
    stmt = select(WorkspaceCrossScopeGrant).where(WorkspaceCrossScopeGrant.user_id == user_id)
    if task_id:
        stmt = stmt.where(WorkspaceCrossScopeGrant.current_task_id == task_id)
    if not include_inactive:
        stmt = stmt.where(WorkspaceCrossScopeGrant.status == "active")
    res = await db.execute(stmt.order_by(WorkspaceCrossScopeGrant.created_at.desc()).limit(200))
    return list(res.scalars().all())


async def claims_of(grant: WorkspaceCrossScopeGrant) -> dict[str, Any]:
    try:
        return json.loads(grant.claims_json or "{}")
    except ValueError:
        return {}


__all__ = ["CODE_GRANT_EXPIRED", "CODE_GRANT_REVOKED", "CODE_GRANT_TAMPERED",
           "CODE_GRANT_UNAUTHORIZED", "GRANT_KINDS", "GrantCheck", "claims_of",
           "list_grants", "mint_grant", "revoke_grant", "verify_grant"]
