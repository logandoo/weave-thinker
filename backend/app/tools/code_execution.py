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


def _detect_file_type_label(ext: str) -> str:
    """Map a file extension to a display type label for attachments."""
    if not ext:
        return "file"
    return _EXT_TYPE_MAP.get(ext, "file")

_CODE_GENERATION_PROMPT = """\
你是一个精确的 Python 代码生成器。根据用户需求生成可直接运行的 Python 代码。

要求：
1. 只输出一个 JSON 对象，包含 "code" 字段
2. code 字段值是一段完整的、可以直接用 `python -c` 执行的 Python 代码
3. 如果是计算/数据处理类任务：用 print() 输出结果
4. 如果是文件生成类任务（PPT/Excel/Word/CSV/图片/PDF 等）：
   - 使用相对文件名将产物保存到**当前工作目录**（cwd 是本次调用专用的 scratch/task_XXXX 子目录，直接 `open("xxx.pptx","wb")` 即可）
   - **重要：每次工具调用都会新建独立的 scratch/task_XXXX 子目录，上一次调用 cwd 里的文件不会出现在本次 cwd 下**；需要跨多次调用读取/续写同一文件时，必须使用用户工作区根目录的绝对路径，例如 `open(r"{workspace_root}/xxx.md", "a")`
   - 生成后用 print() 打印所生成的文件名
   - 生成文件时，文件名中的时间戳必须使用本地时间（datetime.now()），禁止使用 utcnow()
5. 读取用户上传文件：上传文件保存在用户工作区的 `uploads/` 目录下。你可以通过相对路径（如 `uploads/xxx_sample.xlsx`）或工作区内的绝对路径访问它们。如果当前 cwd 是 scratch 子目录，可使用 `../uploads/xxx_sample.xlsx`。
6. 常用库已预装：python-pptx、openpyxl、reportlab、matplotlib、numpy、pandas、Pillow。
   如果需要其他第三方库，先用 try/except ImportError 检测，失败时在代码中 print("NEED_PIP: 包名")，让上层 agent 通过 terminal 工具执行 pip install 后再重试
7. 禁止使用 subprocess、os.system、eval、exec、__import__、ctypes、socket、requests、urllib、httpx、aiohttp
7. 禁止使用 os.makedirs、os.remove、os.rmdir、shutil 等写操作；但 os.listdir、os.path.exists、os.path.join 等只读操作可以安全使用
8. 禁止访问 /etc、/proc、/sys、/dev 等系统路径（字体查找不受此限制）
9. 不要尝试联网下载资源；所有内容均从用户需求中推导
10. 代码应简洁、健壮，处理可能的异常；不要使用交互式输入
11. 代码应简洁高效，避免不必要的冗余。如果任务需要生成大量内容（如长文本、多章小说等），可根据需要分多次调用工具，每次生成一部分内容。
12. 生成中文字符的 PDF/图片时，必须正确处理中文字体，防止出现方块或乱码：
    - 使用以下辅助函数自动查找可用中文字体：
      ```python
      import os
      def _find_cn_font():
          candidates = [
              '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
              '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
          ]
          for root_dir in ['/usr/share/fonts', '/usr/local/share/fonts', os.path.expanduser('~/.fonts')]:
              if os.path.isdir(root_dir):
                  for dirpath, dirnames, filenames in os.walk(root_dir):
                      for fn in filenames:
                          if any(kw in fn.lower() for kw in ['cjk', 'noto', 'wqy', 'simhei', 'simsun', 'microsoft', 'yahei', 'fang']):
                              fp = os.path.join(dirpath, fn)
                              if fp not in candidates:
                                  candidates.append(fp)
          for p in candidates:
              if os.path.exists(p):
                  return p
          return None
      _cn_font_path = _find_cn_font()
      ```
    - 对于 matplotlib: `matplotlib.font_manager.FontProperties(fname=_cn_font_path)` 或 `plt.rcParams['font.sans-serif'] = [_cn_font_path]`
    - 对于 reportlab: `pdfmetrics.registerFont(TTFont('CNFont', _cn_font_path))` 然后 `canvas.setFont('CNFont', 12)`
    - 对于 PIL/Pillow: `ImageFont.truetype(_cn_font_path, size)`
    - 如果 `_cn_font_path` 为 None，则输出 "NEED_FONT: 系统缺少中文字体" 并优雅降级（使用英文标注或无文字）
13. 文件交付规范：本次调用在**用户工作区根目录**写出的文件会自动成为展示给用户的下载卡片；cwd（scratch/task_XXXX 临时目录）内的文件不会出现在卡片中。因此：中间草稿、调试统计、日志、临时数据等辅助文件一律写入当前 cwd（scratch 临时目录）即可；只有**最终需要交付给用户下载的文件**才写入工作区根目录（用绝对路径），避免把无关文件混入交付列表。

用户工作目录: {workspace_path}
"""

_CODE_GENERATION_PROMPT_PTC = """\
你是一个精确的 Python 代码生成器。根据用户需求生成可直接运行的 Python 代码。

你可以在代码中调用工具来获取外部数据。使用方法：
  from _ptc_bridge import tools
  result = tools.工具名(参数=值)

可用工具：
{tools_doc}

要求：
1. 只输出一个 JSON 对象，包含 "code" 字段
2. code 字段值是一段完整的、可以直接用 `python -c` 执行的 Python 代码
3. 如果是计算/数据处理类任务：用 print() 输出结果
4. 如果是文件生成类任务（PPT/Excel/Word/CSV/图片/PDF 等）：
   - 使用相对文件名将产物保存到**当前工作目录**（cwd 是本次调用专用的 scratch/task_XXXX 子目录，直接 `open("xxx.pptx","wb")` 即可）
   - **重要：每次工具调用都会新建独立的 scratch/task_XXXX 子目录，上一次调用 cwd 里的文件不会出现在本次 cwd 下**；需要跨多次调用读取/续写同一文件时，必须使用用户工作区根目录的绝对路径，例如 `open(r"{workspace_root}/xxx.md", "a")`
   - 生成后用 print() 打印所生成的文件名
   - 生成文件时，文件名中的时间戳必须使用本地时间（datetime.now()），禁止使用 utcnow()
5. 读取用户上传文件：上传文件保存在用户工作区的 `uploads/` 目录下。你可以通过相对路径（如 `uploads/xxx_sample.xlsx`）或工作区内的绝对路径访问它们。如果当前 cwd 是 scratch 子目录，可使用 `../uploads/xxx_sample.xlsx`。
6. 常用库已预装：python-pptx、openpyxl、reportlab、matplotlib、numpy、pandas、Pillow。
   如果需要其他第三方库，先用 try/except ImportError 检测，失败时在代码中 print("NEED_PIP: 包名")，让上层 agent 通过 terminal 工具执行 pip install 后再重试
7. 禁止使用 subprocess、os.system、eval、exec、__import__、ctypes、socket、requests、urllib、httpx、aiohttp
7. 禁止使用 os.makedirs、os.remove、os.rmdir、shutil 等写操作；但 os.listdir、os.path.exists、os.path.join 等只读操作可以安全使用
8. 禁止访问 /etc、/proc、/sys、/dev 等系统路径（字体查找不受此限制）
9. 不要尝试联网下载资源；使用 tools 工具获取数据代替直接联网
10. 代码应简洁、健壮，处理可能的异常；不要使用交互式输入
11. tools 工具调用可能失败，请用 try/except 处理错误
12. 生成中文字符的 PDF/图片时，必须正确处理中文字体，防止出现方块或乱码：
    - 使用以下辅助函数自动查找可用中文字体：
      ```python
      import os
      def _find_cn_font():
          candidates = [
              '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
              '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
          ]
          for root_dir in ['/usr/share/fonts', '/usr/local/share/fonts', os.path.expanduser('~/.fonts')]:
              if os.path.isdir(root_dir):
                  for dirpath, dirnames, filenames in os.walk(root_dir):
                      for fn in filenames:
                          if any(kw in fn.lower() for kw in ['cjk', 'noto', 'wqy', 'simhei', 'simsun', 'microsoft', 'yahei', 'fang']):
                              fp = os.path.join(dirpath, fn)
                              if fp not in candidates:
                                  candidates.append(fp)
          for p in candidates:
              if os.path.exists(p):
                  return p
          return None
      _cn_font_path = _find_cn_font()
      ```
    - 对于 matplotlib: `matplotlib.font_manager.FontProperties(fname=_cn_font_path)`
    - 对于 reportlab: `pdfmetrics.registerFont(TTFont('CNFont', _cn_font_path))` 然后 `canvas.setFont('CNFont', 12)`
    - 对于 PIL/Pillow: `ImageFont.truetype(_cn_font_path, size)`
    - 如果 `_cn_font_path` 为 None，则输出 "NEED_FONT: 系统缺少中文字体" 并优雅降级
13. 文件交付规范：本次调用在**用户工作区根目录**写出的文件会自动成为展示给用户的下载卡片；cwd（scratch/task_XXXX 临时目录）内的文件不会出现在卡片中。因此：中间草稿、调试统计、日志、临时数据等辅助文件一律写入当前 cwd（scratch 临时目录）即可；只有**最终需要交付给用户下载的文件**才写入工作区根目录（用绝对路径），避免把无关文件混入交付列表。

用户工作目录: {workspace_path}
"""


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
    llm = get_aux_llm_override() or LLMService()
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
    if getattr(result, "timed_out", False):
        output["timed_out"] = True
        output["guidance"] = (
            "代码执行超过超时限制被终止。脚本在被终止前已产生上述部分 stdout 输出——"
            "请先查看部分输出判断进度，然后调整方案："
            "(1) 减少单次处理的数据量（如只处理部分样本）；"
            "(2) 分批执行，每批保存中间结果到文件，下一批从文件读取继续；"
            "(3) 将长任务拆分为多个小步骤，每步单独调用 execute_code；"
            "(4) 检查是否存在死循环或性能瓶颈。"
            "对于确实需要长时间运行的任务（如大规模评测、模型训练），"
            "使用 background_task 工具提交到后台执行（后台任务超时限制为 5 小时），"
            "不要用相同代码重复调用——会再次超时。"
        )
    if ptc_bridge:
        output["ptc_calls"] = ptc_bridge.call_count

    return json.dumps(output, ensure_ascii=False)


registry.register(
    name="execute_code",
    toolset="code",
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
            "Sandbox blocks subprocess/os.system — use the terminal tool for external commands."
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
