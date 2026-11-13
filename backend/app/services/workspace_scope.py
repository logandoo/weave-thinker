# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""任务作用域：`WorkspaceContext` 的构建与校验（2026-11-10 波 · T2）。

设计契约 `docs/DESIGN_20261108_task_workspace_and_memory.md` §4.3 / §5 / §11：

* 用户根不变，其下增加稳定的 Project → Task → Run/Attempt 作用域；
* **所有 root 由服务端从用户根相对路径重建**，realpath 后必须仍在用户根内，
  且 Task root 必须逐段等于 canonical path（`projects/p_<pid>/tasks/t_<tid>`）；
* 空 root、用户根本身、别名（symlink 重定向）、嵌套、与其他 Task 重叠的 root 全部拒绝；
* 单次 run 内冻结（frozen dataclass）：路由、恢复、压缩、工具调用共用同一个
  `binding_version` 与 attempt/epoch，不在执行中途静默换根；
* 缺任一必要字段即 fail closed（`workspace_scope_missing`），**绝不回退用户根**。

这里只有纯逻辑与文件系统判定，不查库；DB 归属查询在 `task_binding_service` /
`workspace_task_service`，工具侧统一入口在 `workspace_scope_service`。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# --- 错误码（与 PLAN Consistency Hub 一致，勿随意增删）------------------------
CODE_ROOT_INVALID = "workspace_root_invalid"
CODE_SCOPE_MISSING = "workspace_scope_missing"
CODE_FOREIGN_SCOPE = "workspace_foreign_scope"
CODE_RELATION_INVALID = "workspace_relation_invalid"
CODE_BINDING_STALE = "workspace_binding_stale"
CODE_ATTEMPT_STALE = "workspace_attempt_stale"
CODE_MODE_UNSUPPORTED = "workspace_scope_mode_unsupported"
CODE_CROSS_SCOPE_DENIED = "workspace_cross_scope_denied"
CODE_RUN_ACTIVE = "workspace_run_active"

PROJECT_DIR_PREFIX = "p_"
TASK_DIR_PREFIX = "t_"
RUN_DIR_PREFIX = "r_"
ATTEMPT_DIR_PREFIX = "a_"

TASK_ACTIVE_STATUSES = ("active", "completed")
TASK_STATUS_ARCHIVED = "archived"

_INVALID_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UUIDISH = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class WorkspaceScopeError(ValueError):
    """作用域校验失败；`code` 是对外可见的稳定错误码。"""

    def __init__(self, code: str, message: str, target: str = "") -> None:
        self.code = code
        self.target = target
        super().__init__(f"{code}: {message}")


# --- canonical 相对路径 -------------------------------------------------------

def _check_id(value: str, kind: str) -> str:
    text = str(value or "").strip()
    if not _UUIDISH.match(text):
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"invalid {kind} id", text)
    return text


def project_rel(project_id: str) -> str:
    return f"projects/{PROJECT_DIR_PREFIX}{_check_id(project_id, 'project')}"


def task_rel(project_id: str, task_id: str) -> str:
    return f"{project_rel(project_id)}/tasks/{TASK_DIR_PREFIX}{_check_id(task_id, 'task')}"


def run_rel(project_id: str, task_id: str, run_id: str) -> str:
    return f"{task_rel(project_id, task_id)}/runs/{RUN_DIR_PREFIX}{_check_id(run_id, 'run')}"


def attempt_rel(project_id: str, task_id: str, run_id: str, attempt_id: str) -> str:
    return f"{run_rel(project_id, task_id, run_id)}/attempts/{ATTEMPT_DIR_PREFIX}{_check_id(attempt_id, 'attempt')}"


# --- 路径工具 -----------------------------------------------------------------

def is_inside(root: Path, candidate: Path, *, allow_equal: bool = False) -> bool:
    """`candidate`（realpath）是否落在 `root`（realpath）内。"""
    try:
        base = Path(root).resolve()
        real = Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if real == base:
        return allow_equal
    try:
        real.relative_to(base)
        return True
    except ValueError:
        return False


def normalize_rel(raw: Any, *, what: str = "root") -> str:
    """校验并返回规范的用户根相对 POSIX 路径（拒绝绝对/穿越/别名拼写）。"""
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"empty {what}", text)
    if _INVALID_CHARS.search(text):
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"{what} contains control characters", text)
    if text.startswith("/") or text.startswith("\\") or re.match(r"^[A-Za-z]:[\\/]", text):
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"{what} must be workspace-relative", text)
    if "\\" in text:
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"{what} must use posix separators", text)
    parts = PurePosixPath(text).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        # `.` / `..` / 空段 / 尾斜杠 / 双斜杠 —— 都不是规范拼写
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"{what} is not canonical", text)
    if text != PurePosixPath(*parts).as_posix():
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"{what} is not canonical", text)
    return text


def _resolve_no_redirect(user_root: Path, rel: str, *, what: str) -> Path:
    """把相对路径重建为绝对路径，并拒绝任何 symlink 重定向与越界。"""
    base = Path(user_root).resolve()
    expected = base.joinpath(*PurePosixPath(rel).parts)
    try:
        real = expected.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"unresolvable {what}: {exc}", rel) from exc
    if real != expected:
        # 路径上任一组件是 symlink（别名）：不允许作为作用域根
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"{what} resolves through a symlink alias", rel)
    if not is_inside(base, real):
        raise WorkspaceScopeError(CODE_ROOT_INVALID, f"{what} escapes the user root", rel)
    return real


def _segments(a: str) -> tuple[str, ...]:
    return PurePosixPath(a).parts


def roots_conflict(a: str, b: str) -> bool:
    """两个 root 是否互为同一目录或父子（重叠）。兄弟目录返回 False。"""
    sa, sb = _segments(a), _segments(b)
    n = min(len(sa), len(sb))
    if sa[:n] == sb[:n]:
        return True   # 相等，或一个是另一个的前缀目录
    return False


def validate_task_root(user_root: Path, root_rel: Any, *, project_id: str, task_id: str,
                       existing: Sequence[str] = ()) -> str:
    """Task root：必须等于 canonical path，且不与任何其他 Task 重叠。"""
    rel = normalize_rel(root_rel, what="task root")
    canonical = task_rel(project_id, task_id)
    if rel != canonical:
        raise WorkspaceScopeError(
            CODE_ROOT_INVALID, f"task root must equal canonical {canonical!r}", str(root_rel))
    _resolve_no_redirect(user_root, rel, what="task root")  # containment + 无别名
    mine = _segments(canonical)
    for other in existing:
        try:
            other_rel = normalize_rel(other, what="existing task root")
        except WorkspaceScopeError:
            continue  # 别的行拼错不由本 Task 负责，跳过比较
        # 调用方传入的是**其他** Task 的 root：出现同一目录即抢占，必须拒绝。
        so = _segments(other_rel)
        n = min(len(mine), len(so))
        if mine[:n] == so[:n]:
            raise WorkspaceScopeError(
                CODE_ROOT_INVALID,
                f"task root overlaps existing task root {other_rel!r}", canonical)
    return canonical


def legacy_workspace_path(user_root: Path | str) -> str:
    """过渡期适配器：legacy 会话继续用用户根（不授予任何 scoped 语义）。"""
    return str(Path(user_root).resolve())


# --- 运行时上下文 -------------------------------------------------------------

@dataclass(frozen=True)
class WorkspaceContext:
    """提案的唯一运行时传递对象（设计 §4.3）。执行上下文必填 attempt。"""

    user_id: str
    user_root: Path
    project_id: str
    task_id: str
    run_id: str
    attempt_id: str
    binding_version: int
    task_root: Path
    write_root: Path
    run_root: Path
    attempt_root: Path
    artifact_root: Path
    state_root: Path
    vibeweaver_root: Path
    storage_authority: str
    system_memory_ingest: str
    lease_epoch: int
    execution_target: str | None = None
    project_root: Path | None = None
    task_inputs_root: Path | None = None
    task_artifacts_root: Path | None = None
    task_memory_root: Path | None = None
    project_memory_root: Path | None = None
    conversation_id: str | None = None
    project_name: str | None = None
    task_title: str | None = None
    # 相对路径（对外统一用用户根相对路径，见 §3 不变量 9）
    rel: Mapping[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        """attempt.frozen_context_json 用；只存相对路径 + 身份，不存绝对路径。"""
        return {
            "user_id": self.user_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "binding_version": self.binding_version,
            "storage_authority": self.storage_authority,
            "system_memory_ingest": self.system_memory_ingest,
            "lease_epoch": self.lease_epoch,
            "execution_target": self.execution_target,
            "conversation_id": self.conversation_id,
            "rel": dict(self.rel),
        }

    @property
    def cache_key(self) -> str:
        """run state / pendingResume / snapshot / cue / gate result 的统一键。"""
        return (f"{self.task_id}:{self.run_id}:{self.attempt_id}"
                f":{self.binding_version}:{self.lease_epoch}")


@dataclass(frozen=True)
class WorkspaceBindingView:
    """pre-run / UI / 上传暂存用的只读视图：不含 attempt-derived root。"""

    user_id: str
    user_root: Path
    project_id: str
    task_id: str
    binding_version: int
    storage_authority: str
    system_memory_ingest: str
    task_root: Path
    project_root: Path
    staging_root: Path
    task_memory_root: Path
    project_memory_root: Path
    conversation_id: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "binding_version": self.binding_version,
            "storage_authority": self.storage_authority,
            "system_memory_ingest": self.system_memory_ingest,
            "conversation_id": self.conversation_id,
        }


@dataclass(frozen=True)
class ScopeRoot:
    """工具侧的统一取根结果（`workspace_scope_service.resolve_tool_root` 产出）。"""

    root: Path
    scope: str                 # current_task | project | task | legacy | staging
    task_id: str | None = None
    source: str = "current_task"   # 展示用来源：当前任务 / 项目共享 / 任务:<标题> / legacy
    read_only: bool = False
    grant_id: str | None = None


# --- 构建与校验 ---------------------------------------------------------------

def _row(value: Any, what: str) -> Any:
    if value is None:
        raise WorkspaceScopeError(CODE_SCOPE_MISSING, f"missing {what}", what)
    return value


def build_binding_view(*, user_id: str, user_root: Path | str, project, task, binding,
                       conversation_id: str | None = None) -> WorkspaceBindingView:
    """pre-run 视图：校验归属与 root，但不涉及 run/attempt。"""
    base = Path(user_root).resolve()
    project = _row(project, "project")
    task = _row(task, "task")
    binding = _row(binding, "binding")
    _assert_owner(project, user_id, "project")
    _assert_owner(task, user_id, "task")
    _assert_owner(binding, user_id, "binding")
    _assert_relations(project, task, binding)
    task_root_rel = validate_task_root(base, task.root_rel, project_id=project.id, task_id=task.id)
    project_root = _resolve_no_redirect(base, project_rel(project.id), what="project root")
    task_root = _resolve_no_redirect(base, task_root_rel, what="task root")
    return WorkspaceBindingView(
        user_id=user_id,
        user_root=base,
        project_id=project.id,
        task_id=task.id,
        binding_version=int(binding.binding_version),
        storage_authority=str(task.storage_authority or "cloud"),
        system_memory_ingest=str(binding.system_memory_ingest or task.system_memory_ingest
                                 or project.system_memory_ingest or "user_facts"),
        task_root=task_root,
        project_root=project_root,
        staging_root=task_root / "staging",
        task_memory_root=task_root / "memory",
        project_memory_root=project_root / "memory",
        conversation_id=conversation_id or getattr(binding, "conversation_id", None),
    )


def _assert_owner(row, user_id: str, kind: str) -> None:
    if str(getattr(row, "user_id", "") or "") != str(user_id):
        raise WorkspaceScopeError(CODE_FOREIGN_SCOPE, f"{kind} does not belong to user", kind)


def _assert_relations(project, task, binding, run=None, attempt=None) -> None:
    if str(task.project_id or "") != str(project.id):
        raise WorkspaceScopeError(CODE_RELATION_INVALID, "task does not belong to project", task.id)
    if str(task.storage_authority or "cloud") != str(project.storage_authority or "cloud"):
        # 设计 §6.5：Task 的存储权威必须与 Project 一致
        raise WorkspaceScopeError(
            CODE_RELATION_INVALID, "task storage_authority differs from project", task.id)
    if str(task.status or "active") not in TASK_ACTIVE_STATUSES:
        raise WorkspaceScopeError(CODE_RELATION_INVALID, "task is not usable", str(task.status))
    if str(binding.task_id or "") != str(task.id):
        raise WorkspaceScopeError(CODE_RELATION_INVALID, "binding points to another task", binding.task_id)
    if run is not None:
        if str(run.task_id or "") != str(task.id) or str(run.project_id or "") != str(project.id):
            raise WorkspaceScopeError(CODE_RELATION_INVALID, "run does not belong to task", run.id)
        if int(run.binding_version) != int(binding.binding_version):
            # 绑定已变（改绑/回滚），本 run 的后续写入必须失效
            raise WorkspaceScopeError(
                CODE_BINDING_STALE,
                f"run binding_version {run.binding_version} != current {binding.binding_version}",
                run.id)
    if attempt is not None:
        if str(attempt.run_id or "") != str(run.id):
            raise WorkspaceScopeError(CODE_ATTEMPT_STALE, "attempt belongs to another run", attempt.id)
        if str(run.current_attempt_id or "") != str(attempt.id):
            raise WorkspaceScopeError(
                CODE_ATTEMPT_STALE, "attempt is not the run's current attempt", attempt.id)
        if int(attempt.lease_epoch) != int(run.lease_epoch or 0):
            raise WorkspaceScopeError(
                CODE_ATTEMPT_STALE,
                f"attempt lease_epoch {attempt.lease_epoch} != run epoch {run.lease_epoch}",
                attempt.id)


def build_workspace_context(*, user_id: str, user_root: Path | str, project, task, binding,
                            run, attempt, existing_task_roots: Iterable[str] = (),
                            execution_target: str | None = None) -> WorkspaceContext:
    """执行期唯一上下文对象；任何一项校验不过即 raise（fail closed）。"""
    base = Path(user_root).resolve()
    project = _row(project, "project")
    task = _row(task, "task")
    binding = _row(binding, "binding")
    run = _row(run, "run")
    attempt = _row(attempt, "attempt")   # 执行上下文必填；pre-run 用 BindingView
    for row, kind in ((project, "project"), (task, "task"), (binding, "binding"),
                      (run, "run"), (attempt, "attempt")):
        _assert_owner(row, user_id, kind)
    _assert_relations(project, task, binding, run, attempt)

    task_root_rel = validate_task_root(base, task.root_rel, project_id=project.id,
                                       task_id=task.id, existing=tuple(existing_task_roots or ()))
    project_root = _resolve_no_redirect(base, project_rel(project.id), what="project root")
    task_root = _resolve_no_redirect(base, task_root_rel, what="task root")
    run_root = _resolve_no_redirect(base, run_rel(project.id, task.id, run.id), what="run root")
    attempt_root = _resolve_no_redirect(
        base, attempt_rel(project.id, task.id, run.id, attempt.id), what="attempt root")

    ingest = str(binding.system_memory_ingest or task.system_memory_ingest
                 or project.system_memory_ingest or "user_facts")
    rel = {
        "project": project_rel(project.id),
        "task": task_root_rel,
        "run": run_rel(project.id, task.id, run.id),
        "attempt": attempt_rel(project.id, task.id, run.id, attempt.id),
        "workspace": f"{task_root_rel}/workspace",
        "inputs": f"{task_root_rel}/workspace/inputs",
        "task_artifacts": f"{task_root_rel}/workspace/artifacts",
        "staging": f"{task_root_rel}/staging",
        "task_memory": f"{task_root_rel}/memory",
        "project_memory": f"{project_rel(project.id)}/memory",
    }
    return WorkspaceContext(
        user_id=user_id,
        user_root=base,
        project_id=project.id,
        task_id=task.id,
        run_id=run.id,
        attempt_id=attempt.id,
        binding_version=int(binding.binding_version),
        task_root=task_root,
        write_root=task_root / "workspace",
        run_root=run_root,
        attempt_root=attempt_root,
        artifact_root=attempt_root / "artifacts",
        state_root=attempt_root,
        vibeweaver_root=attempt_root,
        storage_authority=str(task.storage_authority or "cloud"),
        system_memory_ingest=ingest,
        lease_epoch=int(attempt.lease_epoch),
        execution_target=execution_target,
        project_root=project_root,
        task_inputs_root=task_root / "workspace" / "inputs",
        task_artifacts_root=task_root / "workspace" / "artifacts",
        task_memory_root=task_root / "memory",
        project_memory_root=project_root / "memory",
        conversation_id=getattr(binding, "conversation_id", None) or getattr(run, "conversation_id", None),
        project_name=getattr(project, "name", None),
        task_title=getattr(task, "title", None),
        rel=rel,
    )


def context_from_frozen_json(payload: Any, *, user_root: Path | str, user_id: str,
                             project, task, binding, run, attempt) -> WorkspaceContext:
    """重启后按 run + current attempt 重建，而不是从 cwd 或最新文件猜测（§5 目录规则）。"""
    import json

    data = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    stored_epoch = data.get("lease_epoch")
    if stored_epoch is not None and int(stored_epoch) != int(attempt.lease_epoch or 0):
        raise WorkspaceScopeError(
            CODE_ATTEMPT_STALE, "frozen context epoch differs from attempt", str(attempt.id))
    stored_version = data.get("binding_version")
    if stored_version is not None and int(stored_version) != int(binding.binding_version):
        raise WorkspaceScopeError(
            CODE_BINDING_STALE, "frozen context binding differs from current", str(attempt.id))
    return build_workspace_context(user_id=user_id, user_root=user_root, project=project,
                                   task=task, binding=binding, run=run, attempt=attempt,
                                   execution_target=data.get("execution_target"))


# --- 工具/链路侧的 fail-closed 入口 -------------------------------------------

def require_context(kwargs: Mapping[str, Any] | None, *, mode: str) -> WorkspaceContext | None:
    """scoped 模式缺 context → 拒绝；off（legacy）→ 返回 None 由调用方走旧路径。

    这是「不允许悄悄退回用户根」的单一实现点：`shadow` 也返回 None（只记录不拦截），
    由 `workspace_scope_service` 负责记录比较日志。
    """
    ctx = (kwargs or {}).get("workspace_ctx")
    if isinstance(ctx, WorkspaceContext):
        return ctx
    if str(mode or "off").strip().lower() == "enforce":
        raise WorkspaceScopeError(CODE_SCOPE_MISSING,
                                  "scoped execution requires a frozen WorkspaceContext")
    return None


def capability_guard(mode_name: str, supported: Sequence[str]) -> None:
    """分阶段能力门：未支持的模式明确拒绝，不静默使用 legacy root。"""
    if str(mode_name or "") not in set(supported or ()):
        raise WorkspaceScopeError(
            CODE_MODE_UNSUPPORTED,
            f"task scoping is not enabled for '{mode_name}' yet", str(mode_name))


def os_relpath(path: Path | str, base: Path | str) -> str:
    """用户根相对 POSIX 路径（对外展示与交付统一用这个形式）。"""
    real = Path(path).resolve()
    root = Path(base).resolve()
    try:
        return real.relative_to(root).as_posix()
    except ValueError:
        raise WorkspaceScopeError(CODE_ROOT_INVALID, "path outside base", str(path))


__all__ = [
    "ATTEMPT_DIR_PREFIX", "CODE_ATTEMPT_STALE", "CODE_BINDING_STALE", "CODE_CROSS_SCOPE_DENIED",
    "CODE_FOREIGN_SCOPE", "CODE_MODE_UNSUPPORTED", "CODE_RELATION_INVALID", "CODE_ROOT_INVALID",
    "CODE_RUN_ACTIVE", "CODE_SCOPE_MISSING", "PROJECT_DIR_PREFIX", "RUN_DIR_PREFIX",
    "TASK_DIR_PREFIX", "ScopeRoot", "WorkspaceBindingView", "WorkspaceContext",
    "WorkspaceScopeError", "attempt_rel", "build_binding_view", "build_workspace_context",
    "capability_guard", "context_from_frozen_json", "is_inside", "legacy_workspace_path",
    "normalize_rel", "os_relpath", "project_rel", "require_context", "roots_conflict",
    "run_rel", "task_rel", "validate_task_root",
]
