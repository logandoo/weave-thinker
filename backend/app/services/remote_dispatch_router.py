# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W16 设备执行模式 — 工具路由缝（route_and_dispatch 语义层）。

registry.dispatch 在执行前调用 try_remote_dispatch：
- 会话 execution_target=device:<id> 且工具∈远端白名单 → 经 WS RPC 在设备执行；
- 服务端拥有型工具（web_search/session_search/memory/…）→ None（本地照旧）；
- 白名单外工具 → fail-closed 拒绝（PLAN §7/§13，双侧白名单的云端侧）。

安全（PLAN §8.4 + A4.9 修订）：
- RPC 出境前剥离所有 `_`-前缀内部键（`_permission_granted` 哨兵会旁路 registry
  权限闸与 terminal 包含性检查；前缀剥离对齐 agent_loop._strip_internal_tool_args）；
- D-8 fail-closed：目标=设备时永不静默落回本地执行——feature 关闭/路由异常都
  返回 tool_error，绝不把设备会话的写落进云端 workspace。
- skill_run_script：云端解析技能脚本内容内联下发（PLAN §7），设备不依赖本地技能库。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from app.core.config import get_config
from app.services.device_session_service import (
    DeviceCallError,
    DeviceCallTimeoutError,
    DeviceOfflineError,
)
from app.tools.registry import tool_error

logger = logging.getLogger(__name__)

__all__ = [
    "DeviceOfflineError",
    "DeviceCallTimeoutError",
    "DeviceCallError",
    "resolve_execution_target",
    "strip_internal_args",
    "is_server_owned",
    "try_remote_dispatch",
]

DEFAULT_REMOTE_ALLOWED_TOOLS = frozenset({
    "workspace_read", "workspace_write", "workspace_edit", "workspace_glob",
    "grep", "diff", "word_count", "terminal", "process", "git", "execute_code",
    "skill_run_script", "provide_file", "provide_folder",
    # W18 G2：workspace FS 读写类补齐（citation_render 回写文件、pdf_export 落盘）
    "citation_render", "pdf_export",
})

SERVER_OWNED_TOOLS = frozenset({
    "web_search", "session_search", "notes", "memory", "schedule", "background_task",
    "vision_interpret", "asr_transcribe", "tts_synthesize", "mixture_of_agents",
    "calculate", "search_tools", "context7_resolve_library_id", "context7_query_docs",
    "delegate_task",
    # W18 G2：技能库=服务端拥有状态（skill_run_script 除外——脚本内容内联下发）
    "skill_view", "skill_manage",
})

# W18 G2：暂缓（P1/P2）——本地 FS/进程副作用工具，设备会话 fail-closed 拒绝，
# 不得当 server-owned 在云端执行（D-8 错误环境写：job_* 落 output_files、
# browser* 起本机 chromium）。
DEFERRED_TOOLS = frozenset({
    "job_submit", "job_status", "job_logs", "job_cancel", "job_list", "job_artifacts",
    "browser", "workspace_snapshot",
})


def _feature_enabled() -> bool:
    return get_config().client_exec_enabled


def _call_timeout() -> float:
    return float(get_config().client_exec_call_timeout_seconds)


_ALLOWED_TOOLS_DRIFT_WARNED = False


def _remote_allowed_tools() -> frozenset:
    global _ALLOWED_TOOLS_DRIFT_WARNED
    cfg_list = get_config().client_exec_allowed_tools
    if not cfg_list:
        return DEFAULT_REMOTE_ALLOWED_TOOLS
    # A4.9 I-4：升级漂移告警（一次性）——config 白名单是部署事实源，旧部署的
    # allowed_tools 不会自动获得 DEFAULT 新成员（fail-closed 安全但功能不生效）。
    missing = DEFAULT_REMOTE_ALLOWED_TOOLS - set(cfg_list)
    if missing and not _ALLOWED_TOOLS_DRIFT_WARNED:
        _ALLOWED_TOOLS_DRIFT_WARNED = True
        logger.warning(
            "client_exec.allowed_tools 缺少默认远端工具 %s——设备会话将拒绝这些工具；"
            "升级部署请同步 backend/config.toml [client_exec] allowed_tools",
            sorted(missing),
        )
    return frozenset(cfg_list)


def is_deferred(tool_name: str) -> bool:
    """暂缓分类（P1/P2）：本地 FS/进程副作用工具，设备会话 fail-closed。"""
    return tool_name in DEFERRED_TOOLS or tool_name.startswith("browser_")


def _tool_timeout(tool_name: str) -> float:
    """按工具取云端工具超时（含 overrides），远端预算=max(该值, 网络预算)
    （A4.9 I-9：长 terminal/execute_code 不被 120s 网络预算掐死）。"""
    try:
        default = float(get_config().agent_tool_loop_tool_call_timeout or 0)
    except Exception:
        default = 0.0
    try:
        overrides = get_config().agent_tool_loop_tool_timeout_overrides or {}
        if tool_name in overrides:
            default = max(default, float(overrides[tool_name]))
    except Exception:
        pass
    return max(default, _call_timeout()) if default else _call_timeout()


def is_server_owned(tool_name: str) -> bool:
    # W18 G1：MCP 注册名为 mcp_{server}_{tool}（下划线），toolset 才是 mcp-{server}——
    # 双前缀匹配，避免设备会话把 MCP 工具误判进 fail-closed。
    return (
        tool_name in SERVER_OWNED_TOOLS
        or tool_name.startswith("mcp-")
        or tool_name.startswith("mcp_")
    )


def strip_internal_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """剥离所有 `_`-前缀内部键（A4.9 M1：对齐 agent_loop 前缀语义）。"""
    return {k: v for k, v in args.items() if not str(k).startswith("_")}


def resolve_execution_target(kwargs: Dict[str, Any]) -> Optional[str]:
    target = kwargs.get("execution_target")
    if isinstance(target, str) and target:
        return target
    conv = kwargs.get("conversation")
    if conv is not None:
        try:
            target = getattr(conv, "execution_target", None)
        except Exception:
            return None
        return target if isinstance(target, str) and target else None
    return None


def _workspace_ctx(kwargs: Dict[str, Any]) -> Optional[str]:
    user = kwargs.get("user")
    if user is None:
        return None
    try:
        from app.services.workspace_service import _slugify
        username = getattr(user, "username", None) or ""
        user_id = str(getattr(user, "id", "") or "")
        return f"{_slugify(username)}-{user_id[:8]}"
    except Exception:
        return None


# W19 P1：混合工具——文件字节经设备代理取回，网关调用留云端（PLAN §7）
_HYBRID_BYTE_TOOLS = {
    "vision_interpret": "image_path",
    "asr_transcribe": "audio_path",
}


async def fetch_file_bytes(
    device_id: str, method: str, params: Dict[str, Any], max_bytes: Optional[int] = None
):
    from app.services.device_session_service import device_session_manager
    return await device_session_manager.fetch_file_bytes(
        device_id, method, params, max_bytes=max_bytes
    )


async def rpc_call(device_id: str, method: str, params: Dict[str, Any], timeout: Optional[float] = None):
    from app.services.device_session_service import device_session_manager
    return await device_session_manager.rpc_call(device_id, method, params, timeout=timeout)


async def record_exec_audit(kwargs, device_id, tool, ok, error, duration_ms):
    """W19 P3：远端执行审计（独立会话落库，不干扰调用方事务；失败不影响工具结果）。"""
    try:
        user = kwargs.get("user")
        user_id = str(getattr(user, "id", "") or "")
        if not user_id:
            return  # A4.9 M5：无用户上下文跳过（NOT NULL/FK 会静默丢行）
        from app.services.client_exec_audit_service import record_exec

        conv = kwargs.get("conversation")
        await record_exec(
            db=None,
            user_id=user_id,
            device_id=device_id,
            conversation_id=str(getattr(conv, "id", "") or "") or None,
            tool=tool,
            ok=ok,
            error=error,
            duration_ms=duration_ms,
        )
    except Exception:
        logger.exception("client exec audit write failed")


async def _resolve_skill_script(
    tool_name: str, args: Dict[str, Any], kwargs: Dict[str, Any]
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """skill_run_script → (script_content, interpreter_ext, error)。云端解析
    技能文件内容内联下发（PLAN §7 / A4.9 I-6）——设备不按名字猜本地技能，
    解析失败 fail-closed（绝不同名代跑）。"""
    if tool_name != "skill_run_script":
        return None, None, None
    skill_name = str(args.get("skill_name") or "").strip()
    script_path = str(args.get("script_path") or "").strip()
    if not skill_name or not script_path:
        return None, None, "skill_name 与 script_path 必填"
    user = kwargs.get("user")
    if user is None:
        return None, None, "skill_run_script 远端执行需要用户上下文"
    try:
        from app.tools.skill_tools import (
            _extract_skill_name,
            _get_skills_dir,
            _iter_skill_files,
            is_system_skill,
        )

        content: Optional[str] = None
        if is_system_skill(skill_name):
            import asyncio
            from pathlib import Path

            skills_dir = _get_skills_dir()
            skill_dir = None
            for skill_file in await asyncio.to_thread(
                lambda: list(_iter_skill_files(skills_dir))
            ):
                if _extract_skill_name(skill_file, skills_dir) == skill_name:
                    skill_dir = skill_file.parent
                    break
            if skill_dir is None:
                return None, None, f"技能 {skill_name} 不存在"
            script_full = (skill_dir / script_path).resolve()
            if skill_dir.resolve() not in script_full.parents:
                return None, None, "script_path 越出技能目录"
            if not script_full.is_file():
                return None, None, f"脚本 {script_path} 不在技能 {skill_name} 中"
            content = await asyncio.to_thread(
                script_full.read_text, encoding="utf-8", errors="replace"
            )
        else:
            from sqlalchemy import select

            from app.db.database import AsyncSessionLocal, SkillFile, UserSkill

            async with AsyncSessionLocal() as session:
                skill_result = await session.execute(
                    select(UserSkill).where(
                        UserSkill.user_id == user.id,
                        UserSkill.name == skill_name,
                        UserSkill.is_active == True,
                    )
                )
                skill = skill_result.scalar_one_or_none()
                if skill is None:
                    return None, None, f"技能 {skill_name} 不存在"
                file_result = await session.execute(
                    select(SkillFile).where(
                        SkillFile.skill_id == skill.id,
                        SkillFile.file_path == script_path,
                    )
                )
                skill_file = file_result.scalar_one_or_none()
                if skill_file is None:
                    return None, None, f"脚本 {script_path} 不在技能 {skill_name} 中"
                content = skill_file.file_content
        if content is None:
            return None, None, "技能脚本解析失败"
        ext = "." + script_path.rsplit(".", 1)[-1].lower() if "." in script_path else ""
        return content, ext, None
    except Exception as exc:
        logger.exception("skill script inline resolution failed")
        return None, None, "技能脚本解析失败"


async def try_remote_dispatch(
    tool_name: str, args: Dict[str, Any], kwargs: Dict[str, Any]
) -> Optional[str]:
    """远端执行返回工具结果字符串；None=调用方走本地。永不抛异常（fail-closed）。"""
    try:
        return await _try_remote_dispatch_inner(tool_name, args, kwargs)
    except Exception as exc:  # 路由层自身缺陷不得让调用静默落回云端
        logger.exception("device-exec routing failed for tool %s", tool_name)
        try:
            target = resolve_execution_target(kwargs)
        except Exception:
            target = None
        if isinstance(target, str) and target.startswith("device:"):
            return tool_error("设备执行路由失败（fail-closed，不回退云端执行）")
        return None


async def _try_remote_dispatch_inner(
    tool_name: str, args: Dict[str, Any], kwargs: Dict[str, Any]
) -> Optional[str]:
    # A4.9 I-8：先判目标再判 feature 开关——目标=设备且 feature 关闭必须
    # fail-closed 报错（D-8），不得借道本地执行。
    target = resolve_execution_target(kwargs)
    if not target or not target.startswith("device:"):
        return None
    device_id = target.split(":", 1)[1]
    if not _feature_enabled():
        return tool_error(
            "设备执行已停用（client_exec.enabled=false）：该会话执行环境为客户端设备，"
            "不回退云端执行。请新建云端会话或启用设备执行。"
        )

    # W19 P1 混合工具：文件字节经设备代理取回注入本地 handler（网关留云端）
    if tool_name in _HYBRID_BYTE_TOOLS:
        rel = str(args.get(_HYBRID_BYTE_TOOLS[tool_name]) or "").strip()
        if rel:
            # A4.9 I-2：云端先硬化路径（防御纵深——不全信设备侧包含性）
            from app.services.client_device_service import validate_rel_path

            try:
                validate_rel_path(rel)
            except ValueError:
                await record_exec_audit(kwargs, device_id, f"{tool_name}:bytes", False, "bad-path", 0)
                return tool_error("路径非法：仅允许 workspace 相对路径")
            import time as _time

            _t0 = _time.monotonic()

            def _ms() -> int:
                return int((_time.monotonic() - _t0) * 1000)

            try:
                data = await fetch_file_bytes(device_id, "fs/download", {"path": rel})
            except DeviceOfflineError:
                await record_exec_audit(kwargs, device_id, f"{tool_name}:bytes", False, "offline", _ms())
                return tool_error(
                    "设备已离线：该文件位于客户端设备工作区，无法取回字节。"
                    "请保持客户端在线，或改用云端执行环境（新建会话）。"
                )
            except DeviceCallTimeoutError:
                await record_exec_audit(kwargs, device_id, f"{tool_name}:bytes", False, "timeout", _ms())
                return tool_error("设备文件取回超时：请稍后重试。")
            except DeviceCallError as exc:
                msg = str(exc)
                await record_exec_audit(kwargs, device_id, f"{tool_name}:bytes", False, msg[:200], _ms())
                if "超过" in msg:
                    return tool_error("设备文件过大，超出取回上限：请改用更小的文件。")
                return tool_error("设备文件取回失败：请检查设备状态后重试。")
            await record_exec_audit(kwargs, device_id, f"{tool_name}:bytes", True, None, _ms())
            kwargs["file_bytes_override"] = bytes(data)
        return None  # 继续本地执行（模型网关在云端）

    if tool_name in _remote_allowed_tools():
        timeout = _tool_timeout(tool_name)
        params = {
            "tool": tool_name,
            "args": strip_internal_args(args),
            "workspace_ctx": _workspace_ctx(kwargs),
            "timeout_seconds": timeout,
        }
        if tool_name == "skill_run_script":
            content, ext, err = await _resolve_skill_script(tool_name, args, kwargs)
            if err is not None:
                return tool_error(err)
            params["script"] = content
            params["script_ext"] = ext
            params["args"] = strip_internal_args(args)
        import time as _time

        _t0 = _time.monotonic()

        def _ms() -> int:
            return int((_time.monotonic() - _t0) * 1000)

        try:
            result = await rpc_call(device_id, "tools/call", params, timeout=timeout)
        except DeviceOfflineError:
            await record_exec_audit(kwargs, device_id, tool_name, False, "offline", _ms())
            return tool_error(
                "设备已离线：执行环境为客户端设备但当前未连接。"
                "请保持客户端在线，或改用云端执行环境（新建会话）。"
            )
        except DeviceCallTimeoutError:
            await record_exec_audit(kwargs, device_id, tool_name, False, "timeout", _ms())
            return tool_error("设备执行超时：请检查设备负载后重试。")
        except DeviceCallError as exc:
            await record_exec_audit(kwargs, device_id, tool_name, False, str(exc)[:200], _ms())
            return tool_error(f"设备执行失败: {str(exc)[:200]}")
        if isinstance(result, dict) and result.get("ok") is False:
            _err = str(result.get("error") or "设备执行失败")[:200]
            await record_exec_audit(kwargs, device_id, tool_name, False, _err, _ms())
            return tool_error(_err)
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            await record_exec_audit(kwargs, device_id, tool_name, True, None, _ms())
            return result["content"]
        await record_exec_audit(kwargs, device_id, tool_name, False, "unparseable", _ms())
        return tool_error("设备返回了无法解析的执行结果")

    if is_server_owned(tool_name):
        return None

    if is_deferred(tool_name):
        return tool_error(
            f"工具 {tool_name} 在客户端执行模式下暂缓（P1/P2）：其本地进程/浏览副作用"
            "需在云端环境运行，但该会话执行环境为客户端设备（不静默改道，D-8）。"
            "请在云端执行环境的会话中使用。"
        )

    return tool_error(
        f"工具 {tool_name} 在客户端执行模式下暂不可用。"
        "当前仅支持文件/终端/git/代码执行类工具在设备上运行，"
        "其余工具请在云端执行环境的会话中使用。"
    )
