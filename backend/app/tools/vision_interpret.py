# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""vision_interpret tool — 视觉模型（VLM）能力开放给 agent（2026-08-31）.

The multimodal endpoint (registry purpose "vlm") is callable BY THE AGENT
anywhere vision is needed — screenshots, error dialogs, charts, UI mockups,
scanned documents, uploaded images. The calling model never changes: the
image bytes go only to the vision endpoint and the text answer comes back
as the tool result. The deathmatch visual critic and the memory multimodal
service use the same purpose route.

2026-09-15 视觉能力探针：`[endpoints.vlm]` 未单独配置时，先对当前主模型
（助手端点优先）做行为探针——有视觉 → 主模型充当视觉模型；无视觉 → 返回
明确的「无视觉能力」错误（绝不假装看过图片）。见 model_gateway.vision_probe。
"""
import base64
import json
import logging
import os
from pathlib import Path

from app.services.tool_progress import report_tool_progress
from app.tools.registry import registry
from app.tools.workspace_read import _resolve_workspace_path

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


async def _resolve_vision(assistant=None):
    """视觉端点解析（vlm 已配置 → 用它；未配置 → 主模型能力探针）。

    返回 (ep | None, reason)；reason ∈ configured / main_model / no_vision / error:<msg>。"""
    from app.model_gateway.vision_probe import resolve_vision_endpoint
    return await resolve_vision_endpoint(assistant)


async def vision_interpret(args: dict, **kwargs) -> str:
    image_path = str(args.get("image_path") or "").strip()
    question = str(args.get("question") or "").strip() or "请详细描述这张图片的内容。"
    if not image_path:
        return json.dumps({"error": "image_path 不能为空", "success": False}, ensure_ascii=False)
    workspace_path = kwargs.get("workspace_path") or ""
    if not workspace_path:
        return json.dumps({"error": "无工作区上下文，无法解析图片路径", "success": False}, ensure_ascii=False)
    resolved = _resolve_workspace_path(image_path, workspace_path)
    if not resolved:
        return json.dumps(
            {"error": f"图片不存在或不在工作区内（路径越界会被拒绝）: {image_path}", "success": False},
            ensure_ascii=False,
        )
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in _IMAGE_EXTS:
        return json.dumps(
            {"error": f"不支持的图片类型 {ext}（支持: {', '.join(sorted(_IMAGE_EXTS))}）", "success": False},
            ensure_ascii=False,
        )
    ep, vlm_err = await _resolve_vision(kwargs.get("assistant"))
    if ep is None:
        if vlm_err == "no_vision":
            return json.dumps({
                "error": "当前主模型不具备视觉能力，且未配置独立视觉模型（vlm）："
                         "请在模型配置的 [endpoints.vlm] 填入 base_url/api_key/model_name，"
                         "或切换到支持视觉的主模型后重试。",
                "success": False, "configured": False, "vision_capable": False,
            }, ensure_ascii=False)
        return json.dumps({
            "error": f"视觉模型配置加载失败：{str(vlm_err)[6:]}（请检查模型配置文件后重试）",
            "success": False, "configured": False, "config_error": True,
        }, ensure_ascii=False)
    try:
        size = os.path.getsize(resolved)
        if size > _MAX_IMAGE_BYTES:
            return json.dumps(
                {"error": f"图片过大（{size} 字节 > {_MAX_IMAGE_BYTES}）", "success": False},
                ensure_ascii=False,
            )
        with open(resolved, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
    except Exception as exc:
        return json.dumps({"error": f"读取图片失败: {exc}", "success": False}, ensure_ascii=False)

    image_url = {"url": f"data:{_MIME_BY_EXT[ext]};base64,{b64}"}
    detail = str(args.get("detail") or "").strip().lower()
    if detail in ("low", "high"):
        image_url["detail"] = detail
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": image_url},
        ],
    }]
    try:
        from app.model_gateway import factory
        # A4.9 W2-I1 (Security): an empty vlm api_key must NOT fall back to
        # the main provider key on the wire (a keyless local vLLM endpoint
        # would otherwise receive the main credentials). Same "no-key"
        # convention as visual_critic.py.
        ep_wired = ep.with_overrides(api_key=ep.api_key or "no-key")
        llm = factory.build_llm_service(ep_wired)
        # 2026-09-03 (conv 827a6f78): STREAMING VLM call — each chunk is a
        # real liveness signal fed to the agent loop's stall watchdog
        # (report_tool_progress). The previous non-streaming call gave the
        # watchdog nothing to observe, so a hung socket silently occupied
        # the full 1800s hard cap twice in one turn.
        report_tool_progress("vlm_stream_started")
        parts: list = []
        async for event in llm.stream_chat_structured(messages, temperature=0):
            event_type = event.get("type")
            if event_type == "content":
                parts.append(event.get("data") or "")
                report_tool_progress("vlm_chunk")
            elif event_type == "reasoning":
                report_tool_progress("vlm_reasoning")
            elif event_type == "error":
                raise RuntimeError(str(event.get("data") or "vlm stream error"))
        text = "".join(parts)
    except Exception as exc:
        logger.warning("vision_interpret vlm call failed: %s", exc)
        return json.dumps({"error": f"视觉模型调用失败: {exc}", "success": False}, ensure_ascii=False)
    return json.dumps({
        "success": True,
        "image_path": os.path.relpath(resolved, str(Path(workspace_path).resolve())),
        "model": ep.model_name,
        "interpretation": (text or "").strip(),
    }, ensure_ascii=False)


registry.register(
    name="vision_interpret",
    toolset="core",
    schema={
        "name": "vision_interpret",
        "description": (
            "用视觉模型（VLM）解读工作区中的图片。任何需要「看」的场景都应主动调用："
            "截图、报错弹窗、图表/曲线、UI 设计稿、扫描件、用户上传的图片"
            "（对话中的 [file-ref] 标记给出文件路径）、browser_screenshot 保存的截图。\n"
            "传入工作区相对路径（如 uploads/xxx.png、media/abc.png）或工作区内绝对路径，"
            "以及你想问的问题（如「提取图中全部文字」「这个图表的趋势是什么」）。\n"
            "返回视觉模型的文字解读；若返回「不具备视觉能力」，说明独立视觉模型未配置"
            "且当前主模型经探针确认无视觉能力——如实告知用户，不要假装看过图片。"
            "仅支持 png/jpg/jpeg/webp/gif/bmp，单文件 ≤20MB；detail=high 用于需要看清"
            "小字/细节的场合。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "工作区内的图片路径（相对路径或工作区内绝对路径）",
                },
                "question": {
                    "type": "string",
                    "description": "想问视觉模型的问题；默认为「请详细描述这张图片的内容」",
                },
                "detail": {
                    "type": "string",
                    "enum": ["low", "high"],
                    "description": "可选：high=看清小字/细节（更贵更慢），low=快速概览",
                },
            },
            "required": ["image_path"],
        },
    },
    handler=vision_interpret,
    is_async=True,
    description="Interpret workspace images via the configured vision (VLM) model",
    emoji="",
)
