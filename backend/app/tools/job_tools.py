# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""job_tools — 持久作业（durable job runner）的 agent 工具面（toolset=system）。

六工具契约：
- job_submit(kind, command|code, workdir?, timeout_seconds?, idempotency_key?)
  → 立即返回句柄 {job_id, state}（5h+ 作业的入口；timeout_seconds=0=不限）
- job_status(job_id) / job_logs(job_id, offset) / job_cancel(job_id) /
  job_list(limit?) / job_artifacts(job_id)
"""
import json
import os
from typing import Any, Dict

from app.core.config import get_config
from app.tools.registry import registry

config = get_config()


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _runner():
    from app.services.job_runner_service import get_job_runner
    return get_job_runner()


async def job_submit(args: Dict[str, Any], **kwargs) -> str:
    user = kwargs.get("user")
    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        return _err("job_submit 需要已登录用户")
    if not config.agent_durable_jobs_enabled:
        return _err("持久作业已被配置禁用（[agent.durable_jobs] enabled=false）")
    kind = args.get("kind")
    # 与 terminal/execute_code 同级的执行前校验（fail-closed）
    if kind == "shell":
        from app.tools.terminal import _validate_command, _command_accesses_outside_workspace
        _v = _validate_command(str(args.get("command") or ""))
        if _v:
            if _v.get("_permission_needed"):
                return json.dumps({
                    "error": f"Permission needed: {_v.get('_permission_description', 'Dangerous command')}",
                    "_permission_needed": True,
                    "_command": _v.get("_command", str(args.get("command") or "")),
                    "_target_path": _v.get("_target_path", ""),
                    "_permission_description": _v.get("_permission_description", ""),
                }, ensure_ascii=False)
            return _err(f"Command blocked: {_v.get('error', 'Unknown safety error')}")
        _outside = _command_accesses_outside_workspace(
            str(args.get("command") or ""),
            str(kwargs.get("workspace_path") or config.workspace_root),
            str(config.workspace_root))
        if _outside:
            return _err(f"Command references path outside workspace: {_outside}")
    elif kind == "python":
        from app.services.code_execution_service import check_code_safety
        _safety = check_code_safety(
            str(args.get("code") or ""),
            cwd=str(kwargs.get("workspace_path") or config.workspace_root),
            workspace_root=str(config.workspace_root.resolve()))
        if _safety:
            return _err(f"Code failed safety check: {_safety}")
    # workdir 禁闭——normpath 消解 .. 后必须在工作区前缀内
    workdir = None
    _ws = str(kwargs.get("workspace_path") or "").rstrip("/")
    _ws_real = os.path.realpath(_ws) if _ws else ""
    if args.get("workdir"):
        _w = str(args["workdir"])
        _cand = os.path.realpath(_w if os.path.isabs(_w) else os.path.join(_ws or ".", _w))
        if _ws_real and not (_cand == _ws_real or _cand.startswith(_ws_real + os.sep)):
            return _err("workdir 超出用户工作区，拒绝提交")
        workdir = _cand
    spec: Dict[str, Any] = {"kind": kind, "workdir": workdir}
    if kind == "shell":
        spec["command"] = args.get("command")
    elif kind == "python":
        spec["code"] = args.get("code")
    if args.get("timeout_seconds") is not None:
        spec["timeout_seconds"] = args.get("timeout_seconds")
    source = args.get("source") or "tool"
    try:
        job = await _runner().submit(
            user_id=user_id, source=source, spec=spec,
            idempotency_key=args.get("idempotency_key") or None,
        )
    except ValueError as exc:
        return _err(f"invalid job spec: {exc}")
    return json.dumps({
        "job_id": job["id"],
        "state": job["state"],
        "handle": "作业已持久排队（detached，后端重启不中断）。用 job_status 轮询状态、job_logs(offset) 增量读日志、job_artifacts 取产物、job_cancel 取消。",
    }, ensure_ascii=False)


def _caller_id(kwargs: Dict[str, Any]) -> Any:
    """工具层归属传递——非属主一律 not found。"""
    return str(getattr(kwargs.get("user"), "id", "") or "") or None


async def job_status(args: Dict[str, Any], **kwargs) -> str:
    try:
        job = await _runner().poll(str(args.get("job_id")), user_id=_caller_id(kwargs))
    except KeyError:
        return _err(f"job not found: {args.get('job_id')}")
    return json.dumps({
        "job_id": job["id"], "state": job["state"], "exit_code": job.get("exit_code"),
        "error": job.get("error"), "cancel_state": job.get("cancel_state"),
        "attempts": job.get("attempts"), "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"), "timeout_seconds": job.get("timeout_seconds"),
    }, ensure_ascii=False, default=str)


async def job_logs(args: Dict[str, Any], **kwargs) -> str:
    try:
        chunk, next_offset = await _runner().logs(str(args.get("job_id")),
                                                  int(args.get("offset") or 0),
                                                  user_id=_caller_id(kwargs))
    except KeyError:
        return _err(f"job not found: {args.get('job_id')}")
    return json.dumps({"chunk": chunk, "next_offset": next_offset}, ensure_ascii=False)


async def job_cancel(args: Dict[str, Any], **kwargs) -> str:
    receipt = await _runner().cancel(str(args.get("job_id")), user_id=_caller_id(kwargs))
    return json.dumps(receipt, ensure_ascii=False, default=str)


async def job_list(args: Dict[str, Any], **kwargs) -> str:
    user = kwargs.get("user")
    user_id = str(getattr(user, "id", "") or "")
    jobs = await _runner().list(user_id, int(args.get("limit") or 20))
    slim = [{k: j.get(k) for k in ("id", "state", "source", "exit_code", "created_at", "finished_at")}
            for j in jobs]
    return json.dumps({"jobs": slim}, ensure_ascii=False, default=str)


async def job_artifacts(args: Dict[str, Any], **kwargs) -> str:
    arts = await _runner().artifacts(str(args.get("job_id")), user_id=_caller_id(kwargs))
    return json.dumps({"artifacts": arts}, ensure_ascii=False)


_HANDLE_HINT = "持久作业句柄（detached 子进程，后端重启不中断；timeout_seconds=0 表示不限时，适合 5h+ 重作业）。用 job_status/job_logs/job_artifacts/job_cancel 跟进。"

registry.register(
    name="job_submit",
    toolset="system",
    schema={
        "name": "job_submit",
        "description": (
            "Submit a long-running job (shell command or python code) to the durable job runner "
            "and get a persistent handle immediately. " + _HANDLE_HINT
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["shell", "python"], "description": "作业类型"},
                "command": {"type": "string", "description": "kind=shell 时的 shell 命令"},
                "code": {"type": "string", "description": "kind=python 时的 Python 源码"},
                "workdir": {"type": "string", "description": "工作目录（默认作业私有目录）"},
                "timeout_seconds": {"type": "number", "description": "超时秒数；0=不限（默认 0）"},
                "idempotency_key": {"type": "string", "description": "幂等键：同键重复提交返回既有作业"},
                "source": {"type": "string", "description": "来源标注（execute_code|terminal|process|tool）"},
            },
            "required": ["kind"],
        },
    },
    handler=job_submit,
    is_async=True,
    permission_key="terminal_execution",  # 与 terminal 同级执行权
    description="提交持久长作业（5h+）并立即返回句柄",
    emoji="🧰",
)

registry.register(
    name="job_status",
    toolset="system",
    schema={
        "name": "job_status",
        "description": "Poll a durable job's state (queued/leased/running/succeeded/failed/cancelled/unknown), exit_code, cancel receipt.",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    handler=job_status,
    is_async=True,
    description="查询持久作业状态",
    emoji="🔍",
)

registry.register(
    name="job_logs",
    toolset="system",
    schema={
        "name": "job_logs",
        "description": "Read a durable job's log incrementally from a byte offset (no repeats).",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "offset": {"type": "integer", "description": "字节偏移（0=从头；用返回的 next_offset 续读）"},
            },
            "required": ["job_id"],
        },
    },
    handler=job_logs,
    is_async=True,
    description="增量读取持久作业日志",
    emoji="📜",
)

registry.register(
    name="job_cancel",
    toolset="system",
    schema={
        "name": "job_cancel",
        "description": "Cancel a durable job. Returns a receipt: requested|acknowledged|too_late|failed.",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    handler=job_cancel,
    is_async=True,
    description="取消持久作业（带回执）",
    emoji="🛑",
)

registry.register(
    name="job_list",
    toolset="system",
    schema={
        "name": "job_list",
        "description": "List the current user's durable jobs (recent first).",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "默认 20"}},
        },
    },
    handler=job_list,
    is_async=True,
    description="列出持久作业",
    emoji="📋",
)

registry.register(
    name="job_artifacts",
    toolset="system",
    schema={
        "name": "job_artifacts",
        "description": "List registered artifacts (path+sha256+bytes) produced by a durable job.",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    handler=job_artifacts,
    is_async=True,
    description="列出持久作业产物（含 sha256）",
    emoji="📦",
)
