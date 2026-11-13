# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""跨域导入 / 受控发布 / change manifest / 上传 staging（2026-11-10 波 · T7+T13）。

设计契约 §5 目录规则 / §7.2 / §10 上传与首条消息前附件：

* 跨域结果要进入当前 Task，必须通过「导入/发布」生成**当前 Task 的新副本** + 来源记录
  （写入工具不开放任意 scope 写）；
* 稳定 Task 文件的写入必须由 storage service 在写前检查当前 attempt + epoch
  （`controlled_publish`），stale epoch 整体拒绝、不留半截文件；
* 每次任务文件副作用在 attempt root 追加 `workspace_change_manifest` 记录
  （用户根相对 path、task/run/attempt、lease_epoch、before/after hash、snapshot ref）；
* 首条消息前的附件写 `staging/<upload_id>`，未交接不对 agent 可见；首 run 激活时
  按 hash + CAS 原子交接为 inputs + artifact refs。
"""
from __future__ import annotations

import hashlib
import json
import re
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from app.db.database import WorkspaceArtifactRef, WorkspaceStagingLease
from app.services import workspace_scope as ws

logger = logging.getLogger(__name__)

MANIFEST_REL = ".vibeweaver/workspace-change-manifest.jsonl"
_INPUTS = "workspace/inputs"


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


# --- 来源记录 -----------------------------------------------------------------

async def record_ref(db, *, user_id: str, artifact_type: str, rel_path: str | None = None,
                     artifact_id: str | None = None, owner_task_id: str | None = None,
                     owner_run_id: str | None = None, owner_attempt_id: str | None = None,
                     source_task_id: str | None = None, source_run_id: str | None = None,
                     content_hash: str | None = None, origin: str = "user",
                     taint: str = "agent",
                     system_memory_ingest: str | None = None) -> WorkspaceArtifactRef:
    row = WorkspaceArtifactRef(
        user_id=user_id, artifact_type=artifact_type, artifact_id=artifact_id,
        rel_path=rel_path, owner_task_id=owner_task_id, owner_run_id=owner_run_id,
        owner_attempt_id=owner_attempt_id, source_task_id=source_task_id,
        source_run_id=source_run_id, content_hash=content_hash, origin=origin, taint=taint,
        system_memory_ingest=system_memory_ingest,
    )
    db.add(row)
    await db.flush()
    return row


# --- change manifest ----------------------------------------------------------

def append_change(ctx, *, path: str, before_hash: str | None, after_hash: str | None,
                  snapshot_ref: str | None = None,
                  operation: str = "write") -> dict[str, Any]:
    """attempt root 下追加一行 manifest（checker 用它评审稳定 Task 文件变更）。"""
    entry = {
        "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "path": str(path), "operation": operation,
        "task_id": ctx.task_id, "run_id": ctx.run_id, "attempt_id": ctx.attempt_id,
        "lease_epoch": ctx.lease_epoch, "binding_version": ctx.binding_version,
        "before_sha256": before_hash, "after_sha256": after_hash,
        "snapshot_ref": snapshot_ref,
    }
    target = Path(ctx.attempt_root) / MANIFEST_REL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("workspace change manifest write failed: %s", exc)
    return entry


def read_manifest(ctx, *, limit: int = 500) -> list[dict[str, Any]]:
    target = Path(ctx.attempt_root) / MANIFEST_REL
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines()[-limit:]:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out


# --- 受控发布 -----------------------------------------------------------------

async def assert_epoch_current(db, ctx) -> None:
    from app.services import workspace_run_service as yrs

    await yrs.assert_write_epoch(db, ctx)


async def controlled_publish(db, ctx, *, src: Path | str, rel_target: str,
                             snapshot_ref: str | None = None,
                             move: bool = False) -> dict[str, Any]:
    """把 attempt 内的产物发布到稳定 Task 文件域（expected attempt/epoch）。

    stale attempt/epoch → 结构化拒绝且零落盘（调用方不需要处理异常）。
    """
    try:
        await assert_epoch_current(db, ctx)
    except ws.WorkspaceScopeError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code}
    source = Path(src)
    if not source.is_file():
        return {"ok": False, "error": "source file not found", "code": ws.CODE_ROOT_INVALID}
    # rel_target 一律相对 write_root（= task/workspace）：不给「猜路径」的余地
    rel = ws.normalize_rel(rel_target, what="publish target")
    target = (ctx.write_root / Path(*rel.split("/")))
    if not _inside(ctx.write_root, target):
        return {"ok": False, "error": "target outside task write root",
                "code": ws.CODE_ROOT_INVALID}
    target.parent.mkdir(parents=True, exist_ok=True)
    before = sha256_file(target) if target.is_file() else None
    tmp = target.with_name(f".{target.name}.wt-tmp-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)   # 原子替换：不留半截文件
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"publish failed: {exc}", "code": "workspace_publish_failed"}
    if move:
        try:
            source.unlink()
        except OSError:
            pass
    after = sha256_file(target)
    rel_user = ws.os_relpath(target, ctx.user_root)
    entry = append_change(ctx, path=rel_user, before_hash=before, after_hash=after,
                          snapshot_ref=snapshot_ref, operation="publish")
    await record_ref(db, user_id=ctx.user_id, artifact_type="file", rel_path=rel_user,
                     owner_task_id=ctx.task_id, owner_run_id=ctx.run_id,
                     owner_attempt_id=ctx.attempt_id, content_hash=after,
                     origin="agent", taint="agent",
                     system_memory_ingest=ctx.system_memory_ingest)
    return {"ok": True, "path": rel_user, "sha256": after, "manifest": entry}


# --- 导入 / 合并（跨域只读 → 当前 Task 副本）---------------------------------

def _resolve_in_task(task_root: Path, rel_path: str) -> Path:
    rel = ws.normalize_rel(rel_path, what="import path")
    target = task_root.joinpath(*Path(rel).parts)
    if not _inside(task_root, target):
        raise ws.WorkspaceScopeError(ws.CODE_ROOT_INVALID, "path escapes task root", rel_path)
    return target


def copy_into_task(user_root: Path, *, src_root: Path, dst_root: Path, rel_path: str,
                   source_task_id: str, user_id: str) -> dict[str, Any]:
    """把来源 Task 的文件复制成当前 Task 的 inputs 副本（不建 symlink/hardlink）。"""
    try:
        source = _resolve_in_task(src_root, rel_path)
    except ws.WorkspaceScopeError as exc:
        return {"ok": False, "path": rel_path, "error": str(exc), "code": exc.code}
    if not source.is_file():
        return {"ok": False, "path": rel_path, "error": "source file not found"}
    rel = ws.normalize_rel(rel_path, what="import path")
    target = dst_root / _INPUTS / Path(*Path(rel).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        return {"ok": False, "path": rel, "error": f"copy failed: {exc}"}
    return {"ok": True, "path": f"{_INPUTS}/{rel}", "source_task_id": source_task_id,
            "sha256": sha256_file(target), "read_only_source": True}


def merge_into_task(user_root: Path, *, src_root: Path, dst_root: Path, rel_path: str,
                    source_task_id: str, user_id: str) -> dict[str, Any]:
    """合并：同名即冲突，绝不静默覆盖。"""
    outcome = copy_into_task(user_root, src_root=src_root, dst_root=dst_root, rel_path=rel_path,
                             source_task_id=source_task_id, user_id=user_id)
    if not outcome.get("ok"):
        return outcome
    rel = ws.normalize_rel(rel_path, what="merge path")
    landed = dst_root / _INPUTS / Path(*Path(rel).parts)
    # 冲突判**实际落点**（inputs 副本）与目标 Task 的同名稳定文件
    existing = dst_root / rel
    if existing.exists() or landed.exists():
        try:
            landed.unlink()
        except OSError:
            pass
        return {"ok": False, "path": rel, "conflict": True,
                "error": "目标任务已存在同名文件；请改名或显式替换后重试",
                "code": "workspace_merge_conflict"}
    return outcome


# --- 首条消息前附件的 staging lease ------------------------------------------

@dataclass(frozen=True)
class StagingLease:
    upload_id: str
    path: Path
    expires_at: datetime


async def issue_staging_lease(db, *, view, upload_id: str | None = None,
                              ttl_seconds: int = 86400,
                              filename_hint: str | None = None) -> StagingLease:
    """无 run 时的有界写入许可：文件落 `staging/<upload_id>[_name]`，不进入 agent 文件域。

    `filename_hint`（消毒后）保留扩展名，交接后 agent 侧按类型解析不失明。
    """
    upload_id = str(upload_id or uuid.uuid4())
    hint = re.sub(r'[\/:*?"<>|]', "_", str(filename_hint or "").replace("..", ".").strip())[:120]
    staging = Path(view.staging_root)
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / (f"{upload_id}_{hint}" if hint else upload_id)
    expires = datetime.utcnow() + timedelta(seconds=max(60, int(ttl_seconds)))
    db.add(WorkspaceStagingLease(id=upload_id, user_id=view.user_id, task_id=view.task_id,
                                 binding_version=view.binding_version, path=str(path),
                                 expires_at=expires, status="open"))
    await db.flush()
    return StagingLease(upload_id=upload_id, path=path, expires_at=expires)


async def commit_staging(db, ctx, *, upload_id: str, sha256: str) -> dict[str, Any]:
    """首 run 激活时按 hash + CAS 原子交接为 inputs + artifact refs。"""
    try:
        await assert_epoch_current(db, ctx)
    except ws.WorkspaceScopeError as exc:
        return {"ok": False, "error": str(exc), "code": exc.code}
    res = await db.execute(select(WorkspaceStagingLease).where(
        WorkspaceStagingLease.id == upload_id, WorkspaceStagingLease.user_id == ctx.user_id))
    lease = res.scalar_one_or_none()
    if lease is None:
        return {"ok": False, "error": "staging lease not found", "code": ws.CODE_SCOPE_MISSING}
    if str(lease.task_id) != str(ctx.task_id) or int(lease.binding_version or 0) != int(ctx.binding_version):
        lease.status = "orphaned"
        await db.flush()
        return {"ok": False, "error": "staging lease belongs to another binding",
                "code": ws.CODE_BINDING_STALE}
    if lease.expires_at is not None and lease.expires_at <= datetime.utcnow():
        lease.status = "expired"
        await db.flush()
        return {"ok": False, "error": "staging lease expired", "code": "workspace_staging_expired"}
    source = Path(lease.path)
    if not source.is_file():
        lease.status = "missing"
        return {"ok": False, "error": "staged bytes missing", "code": ws.CODE_ROOT_INVALID}
    actual = sha256_file(source)
    if actual != str(sha256):
        return {"ok": False, "error": "staged hash mismatch", "code": "workspace_staging_hash_mismatch"}
    # rel_target 相对 write_root(=task/workspace)，inputs 就是它下面的 inputs/
    published = await controlled_publish(db, ctx, src=source,
                                         rel_target=f"inputs/{source.name}", move=True)
    if not published.get("ok"):
        return published
    lease.status = "committed"
    await db.flush()
    return {"ok": True, "path": published["path"], "sha256": published["sha256"],
            "upload_id": upload_id}


async def auto_commit_staged(db, ctx) -> int:
    """首 run 激活：把本 Task+绑定下所有 open staging 附件按服务端实算 hash 交接为
    inputs（§10「未交接前对 agent 不可见」的交接面）。best-effort，单个失败不影响其余。"""
    res = await db.execute(
        select(WorkspaceStagingLease).where(
            WorkspaceStagingLease.user_id == ctx.user_id,
            WorkspaceStagingLease.task_id == ctx.task_id,
            WorkspaceStagingLease.binding_version == ctx.binding_version,
            WorkspaceStagingLease.status == "open"))
    count = 0
    for lease in res.scalars().all():
        source = Path(lease.path)
        if not source.is_file():
            lease.status = "missing"
            continue
        try:
            published = await controlled_publish(db, ctx, src=source,
                                                 rel_target=f"inputs/{source.name}", move=True)
        except Exception:  # noqa: BLE001 — 交接失败留在 staging 等 sweep/重试
            continue
        if published.get("ok"):
            lease.status = "committed"
            count += 1
    await db.flush()
    return count


async def sweep_staging(db, *, now: datetime | None = None) -> int:
    """过期/未交接的 staging 可安全清理（不产生不可追踪文件）。"""
    cutoff = now or datetime.utcnow()
    res = await db.execute(select(WorkspaceStagingLease).where(
        WorkspaceStagingLease.status == "open", WorkspaceStagingLease.expires_at <= cutoff))
    count = 0
    for lease in res.scalars().all():
        try:
            Path(lease.path).unlink(missing_ok=True)
        except OSError:
            continue
        lease.status = "swept"
        count += 1
    await db.flush()
    return count


__all__ = ["MANIFEST_REL", "StagingLease", "append_change", "commit_staging", "controlled_publish",
           "copy_into_task", "merge_into_task", "read_manifest", "record_ref", "sha256_file",
           "sweep_staging"]
