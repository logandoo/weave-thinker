# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`workspace_publish` — 把执行域（attempt scratch / write_root scratch）的产物
发布到稳定任务文件域（`ctx.write_root`）。

背景（2026-11-13 deathmatch parity 波 B1）：enforce 下 `terminal`/`process`/`skill_run_script`
的默认 cwd 是 `ctx.attempt_root/scratch`，而死磕校验器只快照 `ctx.write_root` ——
terminal 产出的交付物校验器永远看不见 → `done_check mode=file` 永不定案。本工具是
**模型可用的正规发布通道**：包住 `workspace_artifact_service.controlled_publish`
（attempt/epoch fence + 原子替换 + change manifest + artifact_refs）。

硬约束：
* 根只来自服务端 `workspace_ctx`；不读 `args["workspace_path"]`、不读模型 `scope`；
* `src` 必须落在 `ctx.attempt_root` 或 `ctx.write_root`（realpath + 逐段包含）；
* stale attempt/epoch → 结构化拒绝且零落盘（`controlled_publish` 已实现）；
* 发布前触发 `workspace_snapshot`（失败不阻断，回 `snapshot_error`）；
* 返回双口径：`rel_path` = 用户根相对（下载解析），`display_path` = Task 相对（展示/agent）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.services import workspace_artifact_service as was
from app.services import workspace_scope as ws
from app.services import workspace_scope_service as svs
from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _error(message: str, code: str | None = None) -> str:
    payload: dict[str, Any] = {"ok": False, "error": message, "success": False}
    if code:
        payload["code"] = code
    payload.setdefault("rel_path", None)
    payload.setdefault("display_path", None)
    return json.dumps(payload, ensure_ascii=False)


def _resolve_source(ctx, raw: str) -> Path | None:
    """把 `src` 解析到 attempt scratch / attempt root / write_root 内的真实文件。

    顺序对应生产来源：terminal/process 的 cwd 是 `attempt_root/scratch`；
    execute_code 落在 `write_root/scratch/task_*`；已发布文件可直接原位重发。
    """
    text = str(raw or "").strip().strip("'\"")
    if not text:
        return None
    candidate = Path(text)
    roots = (ctx.attempt_root / "scratch", ctx.attempt_root, ctx.write_root)
    checked: list[Path] = []
    if candidate.is_absolute():
        checked.append(candidate)
    else:
        checked.extend(root / candidate for root in roots)
    for item in checked:
        try:
            if not item.is_file():
                continue
            real = item.resolve()
        except (OSError, RuntimeError):
            continue
        if ws.is_inside(ctx.attempt_root, real) or ws.is_inside(ctx.write_root, real):
            return real
    return None


async def workspace_publish(args: dict, **kwargs) -> str:
    ctx = kwargs.get("workspace_ctx")
    user = kwargs.get("user")
    db = kwargs.get("db")
    src_raw = str((args or {}).get("src") or (args or {}).get("path") or "").strip()
    target_raw = str((args or {}).get("rel_target") or (args or {}).get("target")
                     or "").strip()

    if not src_raw or not target_raw:
        return _error("需要 src（执行域源文件）与 rel_target（任务文件域目标路径）",
                      ws.CODE_ROOT_INVALID)

    # 统一作用域收口：只为了拿到标准拒绝面（enforce 的 legacy/missing 语义）。
    _root, scope_err = await svs.apply_scope_or_error(
        kwargs, default_root=str(getattr(ctx, "write_root", "") or ""),
        args={"rel_path": target_raw}, access="write")
    if scope_err:
        return _error(str(scope_err.get("error") or "任务作用域拒绝发布"), scope_err.get("code"))
    if ctx is None or not isinstance(ctx, ws.WorkspaceContext):
        return _error(
            "workspace_publish 只能在任务作用域（enforce + 已绑定 Task）内使用；"
            "本轮没有 attempt 执行域，请直接写任务文件域。",
            ws.CODE_SCOPE_MISSING)
    if user is None or db is None:
        return _error("用户/数据库上下文缺失，无法发布", ws.CODE_SCOPE_MISSING)
    # 先过 attempt/epoch fence，再做任何副作用（快照 commit / 文件复制）——
    # stale attempt 必须零落盘（评审 #6）。
    try:
        await was.assert_epoch_current(db, ctx)
    except ws.WorkspaceScopeError as exc:
        return _error(str(exc), exc.code)

    source = _resolve_source(ctx, src_raw)
    if source is None:
        return _error(
            f"源文件不存在或越界：{src_raw}（允许 attempt scratch / 任务文件域内的文件）",
            ws.CODE_ROOT_INVALID)

    try:
        rel = ws.normalize_rel(target_raw, what="publish target")
    except ws.WorkspaceScopeError as exc:
        return _error(str(exc), exc.code)

    # 交付物目录约定（2026-11-15 波）：enforce 下裸文件名落到 <dir>/，
    # shadow 只记录、off 零变化；显式子路径一律保留（不覆盖组织意图）。
    deliverable_dir, deliverable_mode = was.publish_deliverable_policy()
    rel, placement = was.resolve_publish_target(
        rel, deliverable_dir=deliverable_dir, mode=deliverable_mode)
    if placement == was.DELIVERABLE_PLACEMENT_OUTSIDE:
        logger.warning(
            "workspace_publish: deliverable outside %s/ (mode=%s): %s",
            deliverable_dir, deliverable_mode, rel)

    snapshot_info: dict[str, Any] = {}
    try:
        from app.tools.workspace_write import _capture_snapshot

        snapshot_info = await _capture_snapshot(
            user, str(ctx.write_root), f"before workspace_publish {rel}")
    except Exception as exc:  # noqa: BLE001 - 快照失败不阻断发布
        logger.warning("workspace_publish snapshot failed: %s", exc)
        snapshot_info = {"snapshot_error": f"snapshot failed: {exc}"}

    from app.services.workspace_lock_service import canonical_key_path, path_lock

    _lock_key = canonical_key_path(str(ctx.write_root), rel)
    async with path_lock(str(getattr(user, "id", "") or ""), _lock_key):
        outcome = await was.controlled_publish(db, ctx, src=source, rel_target=rel)
    if not outcome.get("ok"):
        return _error(str(outcome.get("error") or "发布失败"),
                      outcome.get("code") or "workspace_publish_failed")

    target = ctx.write_root / Path(*rel.split("/"))
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    try:
        rel_path = ws.os_relpath(target, ctx.user_root)
    except ws.WorkspaceScopeError:
        rel_path = str(outcome.get("path") or rel)
    display_path = rel

    # 发布即写：让后续 workspace_edit 的 read-before-write 守卫放行。
    try:
        from app.services.workspace_file_state import read_tracker

        st = target.stat()
        read_tracker.record_read(str(getattr(user, "id", "") or ""), str(target),
                                 st.st_mtime_ns, st.st_size)
    except Exception:  # noqa: BLE001 - 记账失败不影响已完成的发布
        logger.debug("workspace_publish read-state record failed", exc_info=True)

    payload: dict[str, Any] = {
        "ok": True,
        "success": True,
        "name": target.name,
        # 绝对 path 供附件收集器（deathmatch）stat/收集；对外展示用双口径。
        "path": str(target),
        "rel_path": rel_path,
        "display_path": display_path,
        "size": size,
        "sha256": outcome.get("sha256"),
        "source": str(source),
        "deliverable_dir": deliverable_dir,
        "deliverable_placement": placement,
        "warning": (
            f"交付物不在 {deliverable_dir}/ 下（placement={placement}）："
            "死磕校验器与下载卡片口径可能不一致，建议改发到 "
            f"{deliverable_dir}/<文件名>"
            if placement == was.DELIVERABLE_PLACEMENT_OUTSIDE else None),
        **snapshot_info,
    }
    logger.info("workspace_publish: %s → %s (%d bytes)", source, display_path, size)
    return json.dumps(payload, ensure_ascii=False)


def _publish_available() -> bool:
    """off/shadow 没有 attempt 执行域：不把注定失败的工具塞给模型。"""
    try:
        return svs.scoping_mode() == "enforce"
    except Exception:  # noqa: BLE001 - 配置不可读时不暴露
        return False


registry.register(
    name="workspace_publish",
    toolset="files",
    schema={
        "name": "workspace_publish",
        "description": (
            "把执行域（terminal/process 的 attempt scratch，或 execute_code 的 "
            "workspace/scratch/task_*）里生成的文件发布到当前任务文件域。"
            "enforce 任务作用域下，terminal 产出的交付物不会自动出现在任务文件域："
            "必须先 publish，死磕校验器才看得见；要给用户下载卡片仍需 provide_file / pdf_export。"
            "src 是执行域内的相对路径（同名文件优先 attempt scratch，其次任务文件域）"
            "（如 report.md 或 scratch/task_ab/report.md）。"
            "rel_target 是任务文件域内的目标相对路径：**交付物请放 artifacts/**"
            "（如 artifacts/report.md）；裸文件名（如 report.md）在 enforce 下会自动落到 "
            "artifacts/ 下，显式子路径（如 notes/report.md）按原样保留。"
            "scratch/ 是本轮临时目录，不是交付面。"
            "返回 rel_path（下载口径）与 display_path（展示口径）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "src": {"type": "string",
                        "description": "执行域源文件（相对 attempt scratch / 任务文件域）。"},
                "rel_target": {"type": "string",
                               "description": "任务文件域内的目标相对路径（posix 分隔符）；"
                                              "交付物建议 artifacts/<name>。"},
            },
            "required": ["src", "rel_target"],
        },
    },
    handler=workspace_publish,
    is_async=True,
    check_fn=_publish_available,
    description="Publish an execution-domain artifact into the stable task workspace",
    emoji="",
    permission_key="workspace_write",
)


__all__ = ["workspace_publish"]
