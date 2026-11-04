# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import os as _os
import re as _re
import uuid as _uuid
from pathlib import Path as _Path
from typing import Optional

from app.tools.registry import registry
from app.services.llm_service import LLMService
from app.services.code_execution_service import CodeExecutionService, check_code_safety
from app.services.provider_router import build_thinking_extra_body
from app.core.config import get_config

logger = logging.getLogger(__name__)
config = get_config()

_EXT_TYPE_MAP = {
    "pdf": "pdf", "docx": "word", "doc": "word",
    "pptx": "ppt", "ppt": "ppt",
    "xlsx": "excel", "xls": "excel", "csv": "csv",
    "txt": "text", "md": "markdown", "json": "json",
    "py": "python", "js": "javascript", "ts": "javascript",
    "html": "html", "css": "css",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
    "webp": "image", "bmp": "image", "svg": "image",
    "zip": "archive", "gz": "archive", "tar": "archive", "rar": "archive",
    "mp3": "audio", "wav": "audio", "mp4": "video", "mov": "video",
}


from app.prompts.tools import (
    _CODE_GENERATION_PROMPT,
    _CODE_GENERATION_PROMPT_PTC,
)

def _detect_file_type_label(ext: str) -> str:
    """Map a file extension to a display type label for attachments."""
    if not ext:
        return "file"
    return _EXT_TYPE_MAP.get(ext, "file")




def check_code_execution_requirements() -> bool:
    return config.code_execution_enabled


def _strip_llm_wrappers(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = _re.sub(r'<think>.*?</think>', '', cleaned, flags=_re.DOTALL).strip()
    if cleaned.startswith("```"):
        # Strip ONLY the leading fence line (``` or ```json) and the trailing
        # fence line. NEVER split() on inner fences: generated Python code may
        # legitimately contain ```mermaid / ``` ... ``` blocks inside its
        # string literals, and the old split("```", 2)[1] cut the JSON at the
        # first inner fence -> unterminated string -> "Code generation failed"
        # (conv dbdd7df8-566c-498a-a285-3d4df7c42587, 2026-08-04).
        first_nl = cleaned.find("\n")
        if first_nl == -1:
            # Compact single-line fence (```json{...}```): strip only the
            # fence token + optional language tag; never touch inner content.
            cleaned = cleaned[3:]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        else:
            cleaned = cleaned[first_nl + 1:]
    cleaned = cleaned.strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    return cleaned


def _extract_json_object(raw: str) -> dict:
    cleaned = _strip_llm_wrappers(raw)
    if not cleaned:
        raise ValueError("empty LLM response")

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"No JSON object found in LLM response: {cleaned[:200]}")


def _extract_json_from_response(content: str, reasoning: str) -> dict:
    candidates: list[str] = []
    for candidate in (content, reasoning,
                      f"{content}\n{reasoning}" if content and reasoning else ""):
        text = (candidate or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    if not candidates:
        raise ValueError("empty LLM response")

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return _extract_json_object(candidate)
        except Exception as exc:
            last_error = exc

    assert last_error is not None
    raise last_error


async def _request_sub_agent_json(
    llm: LLMService,
    messages: list,
    *,
    temperature: float = 0.0,
    max_tokens: int = 2000,
    repair_max_tokens: int = 2400,
    extra_body: Optional[dict] = None,
    max_attempts: int = 3,
) -> dict:
    last_error_text = "empty LLM response"

    for attempt in range(1, max_attempts + 1):
        try:
            current_messages = messages
            current_max = max_tokens
            if attempt > 1:
                current_messages = list(messages)
                current_messages.append({
                    "role": "user",
                    "content": (
                        f"Previous generation failed: {last_error_text}. "
                        "Please output a valid JSON object with a 'code' field "
                        "containing executable Python code."
                    ),
                })
                current_max = repair_max_tokens

            kwargs: dict = {"temperature": temperature, "max_tokens": current_max}
            if extra_body is not None:
                kwargs["extra_body"] = extra_body

            content, reasoning = await llm.complete_chat_parts(
                current_messages, **kwargs
            )
            parsed = _extract_json_from_response(content, reasoning)

            if "code" not in parsed:
                raise ValueError("missing 'code' key in response")
            return parsed
        except Exception as exc:
            last_error_text = str(exc)
            logger.warning(
                "Code generation attempt %d/%d failed: %s",
                attempt, max_attempts, exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(2.0)

    raise ValueError(
        f"Code generation failed after {max_attempts} attempts: {last_error_text}"
    )


async def execute_code(args: dict, **kwargs) -> str:
    return json.dumps({"error": "Direct code execution is disabled. Use execute_code with a task description."}, ensure_ascii=False)


def _build_ptc_tools_doc(allowed_tools: list) -> str:
    lines = []
    for tool_name in allowed_tools:
        schema = registry.get_schema(tool_name)
        if not schema:
            continue
        desc = schema.get("description", "")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        params = schema.get("parameters", {}).get("properties", {})
        param_parts = []
        for pname, pdef in params.items():
            ptype = pdef.get("type", "any")
            param_parts.append(f"{pname}: {ptype}")
        param_list = ", ".join(param_parts)
        lines.append(f"- tools.{tool_name}({param_list}) → {desc}")
    return "\n".join(lines)


async def generate_and_execute_code(args: dict, **kwargs) -> str:
    task = args.get("task", "").strip()
    use_tools = args.get("use_tools", False)
    if not task:
        return json.dumps({"error": "No task description provided"}, ensure_ascii=False)

    workspace_path = kwargs.get("workspace_path", "")
    assistant = kwargs.get("assistant")
    provider_type = getattr(assistant, "provider_type", "deepseek") or "deepseek"
    ptc_enabled = use_tools and config.agent_ptc_enabled

    task_dir_name = f"task_{_uuid.uuid4().hex[:8]}"
    if workspace_path:
        task_dir = _Path(workspace_path) / "scratch" / task_dir_name
        task_dir.mkdir(parents=True, exist_ok=True)
        exec_cwd = str(task_dir)
    else:
        exec_cwd = workspace_path

    if ptc_enabled:
        prompt = _CODE_GENERATION_PROMPT_PTC.format(
            workspace_path=exec_cwd,
            workspace_root=workspace_path or exec_cwd,
            tools_doc=_build_ptc_tools_doc(config.agent_ptc_allowed_tools),
        )
    else:
        prompt = _CODE_GENERATION_PROMPT.format(
            workspace_path=exec_cwd,
            workspace_root=workspace_path or exec_cwd,
        )

    from app.services.auxiliary_client import get_aux_llm_override
    llm = get_aux_llm_override()
    if llm is None:
        # model_gateway 收口（2026-08-30）：裸构造 ≡ main 端点。
        from app.model_gateway import factory
        from app.model_gateway.registry import get_model_registry
        llm = factory.build_llm_service(get_model_registry().get("main"))
    # 模型网关（A4.9 复审 R2）：wire provider_type 以解析端点为准
    from app.model_gateway.factory import wire_provider_type
    provider_type = wire_provider_type(llm, provider_type)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": task},
    ]

    _no_think = build_thinking_extra_body(provider_type, False)

    try:
        parsed = await _request_sub_agent_json(
            llm, messages,
            temperature=0.0,
            max_tokens=config.code_execution_gen_max_tokens,
            repair_max_tokens=config.code_execution_gen_max_tokens + 400,
            extra_body=_no_think,
        )
        code = parsed.get("code", "").strip()
    except Exception:
        logger.exception("Code generation failed")
        return json.dumps({"error": "Code generation failed"}, ensure_ascii=False)

    if not code:
        return json.dumps({"error": "Generated code was empty"}, ensure_ascii=False)

    safety_error = check_code_safety(code, cwd=exec_cwd, workspace_root=str(config.workspace_root.resolve()))
    if safety_error:
        return json.dumps({"error": f"Generated code failed safety check: {safety_error}"}, ensure_ascii=False)

    # durable=true → 提交持久作业立即返回句柄
    # （detached 子进程，跳过内联 240s 总顶；5h+ 重作业的真实需求出口）。
    if args.get("durable"):
        _uid = str(getattr(kwargs.get("user"), "id", "") or "")
        if not _uid:
            return json.dumps({"error": "durable execute_code job requires an authenticated user"},
                              ensure_ascii=False)
        if not config.agent_durable_jobs_enabled:
            return json.dumps({"error": "持久作业已被配置禁用（[agent.durable_jobs] enabled=false）"},
                              ensure_ascii=False)
        if args.get("use_tools"):
            return json.dumps(
                {"error": "durable=true 不支持 use_tools（PTC 为进程内桥，持久作业不可用）"},
                ensure_ascii=False)
        from app.services.job_runner_service import get_job_runner
        # 持久作业落在稳定工作区（scratch 是本次调用的临时目录，重启后即失）
        _durable_workdir = str(kwargs.get("workspace_path") or "").strip() or str(exec_cwd)
        _spec = {"kind": "python", "code": code, "workdir": _durable_workdir}
        if args.get("timeout_seconds") is not None:
            _spec["timeout_seconds"] = float(args["timeout_seconds"])
        job = await get_job_runner().submit(
            user_id=_uid,
            source="execute_code",
            spec=_spec,
            idempotency_key=args.get("idempotency_key") or None,
        )
        return json.dumps({
            "durable": True,
            "job_id": job["id"],
            "state": job["state"],
            "handle": "代码已提交持久作业（detached，后端重启不中断）。job_status 轮询、job_logs(offset) 读日志、job_artifacts 取产物、job_cancel 取消。",
        }, ensure_ascii=False)

    import time as _time
    from app.services.workspace_scan import snapshot_files, detect_generated_files
    # Snapshot both scratch dir and workspace root
    _scan_dirs = {exec_cwd}
    if workspace_path and str(workspace_path).rstrip("/") != str(exec_cwd).rstrip("/"):
        _scan_dirs.add(str(workspace_path))
    _pre_files = await asyncio.to_thread(snapshot_files, _scan_dirs)
    _exec_start = _time.time()

    code_service = CodeExecutionService()

    ptc_bridge = None
    if ptc_enabled:
        from app.services.ptc_service import PTCBridge
        dispatch_kwargs = {
            k: kwargs.get(k)
            for k in ("db", "user", "conversation", "assistant", "workspace_path")
            if kwargs.get(k) is not None
        }
        ptc_bridge = PTCBridge(
            allowed_tools=config.agent_ptc_allowed_tools,
            sandbox_dir=exec_cwd,
            dispatch_kwargs=dispatch_kwargs,
        )
        await ptc_bridge.start()
        code = "from _ptc_bridge import tools\n" + code

    try:
        result = await code_service.execute_python(
            code,
            cwd=exec_cwd,
            extra_env=ptc_bridge.extra_env if ptc_bridge else None,
        )
    finally:
        if ptc_bridge:
            await ptc_bridge.stop()

    generated_files = await asyncio.to_thread(
        detect_generated_files, _pre_files, _scan_dirs, _exec_start, _detect_file_type_label,
    )

    output = {
        "code": code[:500] + ("...[truncated]" if len(code) > 500 else ""),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "return_code": result.return_code,
        "error": result.error,
        "generated_files": generated_files,
        "cwd": exec_cwd,
    }
    # When the script timed out, include the timed_out flag + actionable
    # guidance so the agent can self-correct instead of blindly retrying
    # the same long script (conv 01d08b67 spin loop). The partial stdout
    # (captured before the kill) is already in result.stdout.
    #
    # 2026-11-01 审计 F3：旧文案无条件断言「已产生上述部分 stdout 输出」——
    # 而 Python 子进程默认块缓冲被 SIGKILL 后缓冲丢失（conv a4d54ec7 即空输出），
    # 且逃生口错指研究型 background_task。现按实际有无输出分叉，并与 terminal 共用
    # `app/tools/timeout_guidance.py`。
    if getattr(result, "timed_out", False):
        from app.tools.timeout_guidance import build_timeout_guidance
        _has_output = bool((result.stdout or "").strip() or (result.stderr or "").strip())
        output["timed_out"] = True
        # F5：-1 只表示"未正常结束"，补可判读信号与生效超时
        output["killed_by"] = getattr(result, "killed_by", "") or "SIGTERM+SIGKILL"
        _te = getattr(result, "timeout_effective", None) or config.code_execution_timeout
        output["timeout_effective"] = _te
        output["guidance"] = build_timeout_guidance(
            float(_te),
            _has_output,
            tool_name="execute_code",
            durable_hint="用**同一个** execute_code 工具的 durable=true 提交持久作业",
        )
    if ptc_bridge:
        output["ptc_calls"] = ptc_bridge.call_count

    return json.dumps(output, ensure_ascii=False)


registry.register(
    name="execute_code",
    toolset="code",
    # F3（2026-09-14）：产物声明——收集器按元数据收集（不再按名硬编码）
    produces_files=True,
    schema={
        "name": "execute_code",
        "description": (
            "Generate and execute Python code to solve programmatic tasks. "
            "Only use when the user explicitly asks to generate a downloadable file "
            "(Excel/PPT/Word/CSV/image, NOT PDF — use pdf_export) or needs complex "
            "computation/data processing. "
            "Do NOT use for explanations, flowcharts/diagrams (use Markdown/Mermaid), "
            "or statistical charts (use ```echarts JSON fences — rendered natively).\n"
            "Runs in a sandboxed workspace. Available libraries: python-pptx, openpyxl, "
            "reportlab, matplotlib, numpy, pandas, Pillow. "
            "Sandbox blocks subprocess/os.system — use the terminal tool for external commands. "
            "Long computations MUST print progress periodically (e.g. per-batch/per-epoch): "
            "a script with no output for several minutes is treated as hung and killed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The programming task to complete. Describe what code should do and what output is expected.",
                },
                "use_tools": {
                    "type": "boolean",
                    "description": "If true, the generated code can call tools (web_search, browser, memory) via a tools proxy. Use this when the task requires fetching live data from the web or reading agent memory.",
                    "default": False,
                },
                "durable": {
                    "type": "boolean",
                    "description": "If true, submit to the durable job runner and return a handle immediately (detached, survives backend restarts; track with job_status/job_logs/job_artifacts). Use for long computations expected to exceed ~4 minutes. Note: durable=true is incompatible with use_tools=true (PTC is an in-process bridge) and runs in the user's workspace directory, not a scratch dir.",
                    "default": False,
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Durable-job timeout in seconds; 0=unlimited (only used when durable=true).",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Durable-job idempotency key (only used when durable=true); same key returns the existing job.",
                },
            },
            "required": ["task"],
        },
    },
    handler=generate_and_execute_code,
    check_fn=check_code_execution_requirements,
    is_async=True,
    description="Generate and execute Python code in sandbox",
    emoji="",
    permission_key="code_execution",
)
