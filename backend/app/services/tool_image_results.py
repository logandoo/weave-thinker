# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""工具结果图片回传 — 多模态 tool message content part（P3 / B4）。

工具 handler 可返回 ``build_image_envelope()`` 生成的 JSON 信封：

    {"_image_result": true, "text": "…说明…", "images": [{"mime": "image/png", "data_b64": "…"}]}

出线前由 ``expand_tool_images()``（LLMService._build_params 单点调用）展开为
OpenAI 兼容的 tool 消息 content parts：text + image_url(data URL)。

降级约定：模型侧不支持图片 content part 时上游会 4xx——工具描述引导改用
vision_interpret（外部 VLM）；``[agent] inline_tool_images=false`` 可整体关闭
（信封只回文本说明，不回图）。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

ENVELOPE_KEY = "_image_result"
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

MAX_INLINE_BYTES = 8 * 1024 * 1024  # 单图 8MB 硬顶

# A4.9 I2：产图工具白名单——只有这些工具的 tool 消息允许展开图片信封。
IMAGE_TOOL_ALLOWLIST = {"workspace_read"}


def mime_for_path(path: str) -> Optional[str]:
    lower = str(path).lower()
    for ext, mime in _MIME_BY_EXT.items():
        if lower.endswith(ext):
            return mime
    return None


def build_image_envelope(text: str, images: List[Dict[str, Any]]) -> str:
    """构造工具结果信封（images: [{"mime": ..., "data_b64": ...}]）。"""
    return json.dumps(
        {ENVELOPE_KEY: True, "text": text or "", "images": images[:4]},
        ensure_ascii=False,
    )


def build_image_envelope_from_file(path: str, text: str) -> Optional[str]:
    """从磁盘图片文件构造信封；非图片/超限/读失败返回 None。"""
    import os

    mime = mime_for_path(path)
    if not mime:
        return None
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_INLINE_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return build_image_envelope(text, [{"mime": mime, "data_b64": base64.b64encode(data).decode("ascii")}])


def expand_tool_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """出线前展开图片信封为 content parts。普通消息原样透传（浅拷贝）。

    A4.9 I2 硬化：
    - **工具名白名单**——只有产图工具（workspace_read 图像分支）的 tool 消息
      才会展开；terminal 等文本输出里伪造的信封 JSON 不生效（防图像型提示
      注入绕过文本消毒）。
    - **展开期上限**——单图 base64 长度 ≤ MAX_INLINE_BYTES×4/3，总数 ≤ 4。
    - **遵守 off 开关**——inline_tool_images=false 时只回文本，不回图。
    """
    out: List[Dict[str, Any]] = []
    max_b64 = (MAX_INLINE_BYTES * 4) // 3 + 16
    for msg in messages:
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            raw = msg["content"]
            tool_name = str(msg.get("name") or "")
            if tool_name in IMAGE_TOOL_ALLOWLIST and (
                raw.lstrip().startswith('{"' + ENVELOPE_KEY) or f'"{ENVELOPE_KEY}"' in raw[:80]
            ):
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and payload.get(ENVELOPE_KEY):
                    parts: List[Dict[str, Any]] = []
                    text = str(payload.get("text") or "")
                    images_ok = inline_images_enabled()
                    if text:
                        parts.append({"type": "text", "text": text})
                    if images_ok:
                        for img in (payload.get("images") or [])[:4]:
                            if not isinstance(img, dict):
                                continue
                            mime = str(img.get("mime") or "image/png")
                            b64 = str(img.get("data_b64") or "")
                            if not b64 or len(b64) > max_b64:
                                continue
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            })
                    if parts:
                        new_msg = dict(msg)
                        new_msg["content"] = parts if any(
                            p["type"] == "image_url" for p in parts
                        ) else (parts[0]["text"] if len(parts) == 1 else parts)
                        out.append(new_msg)
                        continue
        out.append(msg)
    return out


def inline_images_enabled() -> bool:
    try:
        from app.core.config import get_config
        return bool(get_config().agent_inline_tool_images)
    except Exception:
        return True
