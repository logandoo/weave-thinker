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
import math
import os
import re
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
# P0.5.1：历史帧/当前轮帧分开限制；0 = keep none（kill switch）。
DEFAULT_MAX_HISTORY_IMAGE_FRAMES = 2
DEFAULT_MAX_CURRENT_TURN_IMAGE_FRAMES = 4
MAX_INLINE_IMAGE_FRAMES = DEFAULT_MAX_HISTORY_IMAGE_FRAMES  # 旧别名=历史默认；请求上限见下
MAX_TOTAL_INLINE_IMAGE_FRAMES = 8  # 硬总数上限（2+4 默认 6；配置再大也不超 8）
MAX_INLINE_TOTAL_B64_CHARS = 24 * 1024 * 1024  # 单请求 base64 总预算（约 24MB）
MAX_LEGACY_HEADER_B64_CHARS = 512 * 1024  # legacy 尺寸只解码头部，避免全量解码 DoS
_FRAME_LIMIT_MAX = 8
_COMPACTION_PREFIX = "[CONTEXT COMPACTION"

# P0.5.1 legacy 占位兜底：只从已知生产者的文本前缀取文件名；URL/任意
# prose 提及一律不采信（避免把攻击者控制的文件名变成 saved-at 指令）。
_BROWSER_SHOT_RE = re.compile(
    r"浏览器截图已保存：\s*([^\s（(]+\.(?:png|jpe?g|webp|gif|bmp))",
    re.IGNORECASE,
)
_WORKSPACE_READ_RE = re.compile(
    r"\[workspace_read\]\s*图像\s*([^\s（(]+\.(?:png|jpe?g|webp|gif|bmp))",
    re.IGNORECASE,
)
_ABS_IMAGE_PATH_RE = re.compile(
    r"(?<![\w.:])(?:[A-Za-z]:[\\/]|/)[^\s，。；;：:（）()]*?\.(?:png|jpe?g|webp|gif|bmp)",
    re.IGNORECASE,
)
# Anthropic vision 公式：tokens ≈ width*height/750（尺寸感知，避免按 base64
# 字符数估算造成 ~40x 高估）。OpenAI 分块公式不同但同数量级；这里取保守近似。
IMAGE_TOKEN_DIVISOR = 750
# 单帧上限：防止伪造 IHDR（如 100000×100000）把估算推到千万级并触发降级。
MAX_IMAGE_SIDE = 16384
MAX_IMAGE_TOKENS_PER_FRAME = 20_000
# 旧帧占位前缀：下游摘要/superseded 扫描据此跳过，保住 saved path。
IMAGE_PLACEHOLDER_PREFIX = "[image omitted from context"

# A4.9 I2：产图工具白名单——只有这些工具的 tool 消息允许展开图片信封。
# P0-2（2026-10-14 上轮评估）：browser_screenshot 入列——浏览器截图直接回传
# 对话模型（canvas 页视觉闭环），不再逼迫 terminal cp + vision_interpret 中转。
IMAGE_TOOL_ALLOWLIST = {"workspace_read", "browser_screenshot"}


def mime_for_path(path: str) -> Optional[str]:
    lower = str(path).lower()
    for ext, mime in _MIME_BY_EXT.items():
        if lower.endswith(ext):
            return mime
    return None


def is_image_envelope(content: Any) -> bool:
    """Cheap shape check — never parse a multi-MB base64 payload here."""
    if not isinstance(content, str):
        return False
    head = content.lstrip()[:80]
    return head.startswith('{"' + ENVELOPE_KEY) or f'"{ENVELOPE_KEY}"' in head


def parse_image_envelope(content: Any) -> Optional[Dict[str, Any]]:
    """Parse an image envelope; return None for plain text / malformed JSON."""
    if not is_image_envelope(content):
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if isinstance(payload, dict) and payload.get(ENVELOPE_KEY):
        images = payload.get("images")
        if images is not None and not isinstance(images, list):
            return None
        return payload
    return None


def _frame_limits() -> tuple:
    """Read [agent.vision] limits; fall back to defaults on missing/bad config."""
    try:
        from app.core.config import get_config

        cfg = get_config()
        return (
            max(0, min(_FRAME_LIMIT_MAX, int(cfg.agent_vision_max_history_image_frames))),
            max(0, min(_FRAME_LIMIT_MAX, int(cfg.agent_vision_max_current_turn_image_frames))),
        )
    except Exception:
        return (DEFAULT_MAX_HISTORY_IMAGE_FRAMES, DEFAULT_MAX_CURRENT_TURN_IMAGE_FRAMES)


def _tail(values: List[int], limit: int) -> List[int]:
    return [] if limit <= 0 else values[-limit:]


def _is_real_user_message(msg: Any) -> bool:
    """A user turn boundary, excluding interjections/synthetic/ephemeral
    directives and the compaction summary injected as ``role=user``."""
    if not isinstance(msg, dict) or str(msg.get("role") or "") != "user":
        return False
    if msg.get("synthetic") or msg.get("_synthetic"):
        return False
    if msg.get("_interjection") or msg.get("_ephemeral") or msg.get("_rejected"):
        return False
    content = msg.get("content")
    if isinstance(content, str) and content.lstrip().startswith(_COMPACTION_PREFIX):
        return False
    return True


def select_kept_image_frames(
    messages: List[Dict[str, Any]],
    parsed_frames: Optional[Dict[int, Dict[str, Any]]] = None,
    history_limit: Optional[int] = None,
    current_limit: Optional[int] = None,
) -> set:
    """Select which image frames stay inline for this request.

    Boundary = the last *real* user turn (skips interjections/synthetic/
    ephemeral/compaction summaries): frames at/before it are
    history (default 2), frames after it are the current turn (default 4).
    No real user message (subagent/tool-only) → keep the last current_limit frames.
    ``0`` keeps none (kill switch). Invalid/out-of-range values clamp 0..8.
    """
    if parsed_frames is None:
        parsed_frames = {}
        for i, msg in enumerate(messages):
            if (
                isinstance(msg, dict)
                and msg.get("role") == "tool"
                and str(msg.get("name") or "") in IMAGE_TOOL_ALLOWLIST
            ):
                payload = parse_image_envelope(msg.get("content"))
                if payload is not None:
                    parsed_frames[i] = payload
    if not parsed_frames:
        return set()

    default_history, default_current = _frame_limits()
    try:
        history = max(0, min(_FRAME_LIMIT_MAX, int(
            default_history if history_limit is None else history_limit
        )))
    except (TypeError, ValueError):
        history = default_history
    try:
        current = max(0, min(_FRAME_LIMIT_MAX, int(
            default_current if current_limit is None else current_limit
        )))
    except (TypeError, ValueError):
        current = default_current

    last_user = -1
    for i, msg in enumerate(messages):
        if _is_real_user_message(msg):
            last_user = i

    idxs = sorted(parsed_frames)
    if last_user < 0:
        kept = set(_tail(idxs, current))
    else:
        history_frames = [i for i in idxs if i <= last_user]
        current_frames = [i for i in idxs if i > last_user]
        kept = set(_tail(history_frames, history) + _tail(current_frames, current))
    if len(kept) > MAX_TOTAL_INLINE_IMAGE_FRAMES:
        kept = set(sorted(kept)[-MAX_TOTAL_INLINE_IMAGE_FRAMES:])
    return kept


def _png_size_from_b64(b64: str) -> Optional[tuple]:
    """Read PNG IHDR dimensions from the first base64 chunk (no full decode)."""
    try:
        chunk = str(b64 or "")[:64]
        chunk += "=" * ((4 - len(chunk) % 4) % 4)
        raw = base64.b64decode(chunk)
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    except Exception:
        pass
    return None


def _image_size_from_b64(b64: str) -> Optional[tuple]:
    """Legacy envelopes: PNG header first; then PIL for JPEG/WebP/GIF/BMP."""
    dims = _png_size_from_b64(b64)
    if dims:
        return dims
    try:
        from io import BytesIO

        from PIL import Image

        # Only the header is needed; bound the decode so a multi-MB legacy
        # envelope cannot block the event loop on every token estimate.
        raw = base64.b64decode(str(b64 or "")[:MAX_LEGACY_HEADER_B64_CHARS])
        with Image.open(BytesIO(raw)) as image:
            return image.size
    except Exception:
        return None


def _safe_display_path(value: Any) -> str:
    """Normalize a stored/derived path for model-facing placeholders.

    Absolute paths and ``..`` segments collapse to a basename; relative paths
    (e.g. workspace-relative) are preserved.
    """
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw) or raw.lower().startswith(("data:", "file:")):
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw) or ".." in raw.split("/"):
        return raw.rsplit("/", 1)[-1]
    return raw


def _path_from_text(text: Any) -> str:
    """Extract a filename only from the two known producer prefixes."""
    raw = str(text or "")
    for pattern in (_BROWSER_SHOT_RE, _WORKSPACE_READ_RE):
        match = pattern.search(raw)
        if match:
            return _safe_display_path(match.group(1))
    return ""


def _sanitize_placeholder_text(text: Any) -> str:
    """Drop absolute host paths from model-facing placeholder prose."""
    return _ABS_IMAGE_PATH_RE.sub(
        lambda m: os.path.basename(m.group(0).replace("\\", "/")),
        str(text or ""),
    )


def _positive_int(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def image_envelope_token_count(payload: Dict[str, Any]) -> int:
    """Estimate vision tokens from image dimensions (w*h/750).

    Falls back to PNG IHDR parsing for legacy envelopes without width/height;
    only then uses a conservative base64-length ratio (never the 4 chars/token
    text ratio).
    """
    images = payload.get("images")
    if not isinstance(images, list):
        return 0
    total = 0
    for img in images[:4]:
        if not isinstance(img, dict):
            continue
        width = _positive_int(img.get("width"))
        height = _positive_int(img.get("height"))
        if not (width and height):
            dims = _image_size_from_b64(str(img.get("data_b64") or ""))
            if dims:
                width, height = dims
        if width and height:
            width = min(width, MAX_IMAGE_SIDE)
            height = min(height, MAX_IMAGE_SIDE)
            frame = max(1, math.ceil(width * height / IMAGE_TOKEN_DIVISOR))
        else:
            frame = max(1, math.ceil(len(str(img.get("data_b64") or "")) / 160))
        total += min(frame, MAX_IMAGE_TOKENS_PER_FRAME)
    return total


def image_envelope_placeholder(content: Any) -> str:
    """Replace an old image frame with a compact, path-bearing placeholder."""
    payload = parse_image_envelope(content)
    if not payload:
        return str(content or "")
    raw_text = str(payload.get("text") or "").strip()
    text = _sanitize_placeholder_text(raw_text)
    if len(text) > 200:
        text = text[:197] + "..."
    images = payload.get("images")
    paths = [
        str(img.get("path"))
        for img in (images if isinstance(images, list) else [])
        if isinstance(img, dict) and img.get("path")
    ]
    if not paths:
        fallback = _path_from_text(raw_text)
        if fallback:
            paths = [fallback]
    # legacy rows may carry an absolute host path; never surface it to the model.
    paths = [_safe_display_path(p) for p in paths if _safe_display_path(p)]
    label = IMAGE_PLACEHOLDER_PREFIX
    if paths:
        label += f" — saved at {paths[0]}"
    label += "]"
    return f"{label} {text}".strip()


def build_image_envelope(text: str, images: List[Dict[str, Any]]) -> str:
    """构造工具结果信封（images: [{"mime": ..., "data_b64": ...}]）。"""
    return json.dumps(
        {ENVELOPE_KEY: True, "text": text or "", "images": images[:4]},
        ensure_ascii=False,
    )


def build_image_envelope_from_file(
    path: str,
    text: str,
    *,
    display_path: Optional[str] = None,
) -> Optional[str]:
    """从磁盘图片文件构造信封；非图片/超限/读失败返回 None。

    P0.5：附带 width/height/path——尺寸供 token 估算，path 供旧帧占位与
    vision_interpret 重读；三者都是可选字段，旧消费方无感。path 默认只写
    文件名（不把宿主机绝对路径送给模型/落库）；调用方可传工作区相对路径。
    """
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
    image: Dict[str, Any] = {
        "mime": mime,
        "data_b64": base64.b64encode(data).decode("ascii"),
        "path": str(display_path or os.path.basename(path)),
    }
    try:
        from PIL import Image

        with Image.open(path) as im:
            image["width"], image["height"] = im.size
    except Exception:
        dims = _png_size_from_b64(image["data_b64"])
        if dims:
            image["width"], image["height"] = dims
    return build_image_envelope(text, [image])


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
    # P0.5.1：历史 2 帧 + 当前轮 4 帧（wire 前唯一必经点：live loop /
    # history replay / worker 共用；compressor 用同一选择函数）。
    parsed_frames: Dict[int, Dict[str, Any]] = {}
    for i, msg in enumerate(messages):
        if (
            isinstance(msg, dict)
            and msg.get("role") == "tool"
            and isinstance(msg.get("content"), str)
            and str(msg.get("name") or "") in IMAGE_TOOL_ALLOWLIST
        ):
            payload = parse_image_envelope(msg["content"])
            if payload is not None:
                parsed_frames[i] = payload
    keep_frames = select_kept_image_frames(messages, parsed_frames)

    def _frame_b64_chars(index: int) -> int:
        payload = parsed_frames.get(index) or {}
        images = payload.get("images")
        if not isinstance(images, list):
            return 0
        return sum(
            len(str(img.get("data_b64") or ""))
            for img in images
            if isinstance(img, dict)
        )

    total_b64 = sum(_frame_b64_chars(i) for i in keep_frames)
    if total_b64 > MAX_INLINE_TOTAL_B64_CHARS:
        for index in sorted(keep_frames):
            if total_b64 <= MAX_INLINE_TOTAL_B64_CHARS or len(keep_frames) <= 1:
                break
            keep_frames.discard(index)
            total_b64 -= _frame_b64_chars(index)

    images_ok = inline_images_enabled()
    inlined = 0
    if images_ok:
        for index in keep_frames:
            payload = parsed_frames.get(index) or {}
            for img in (payload.get("images") or []):
                if isinstance(img, dict):
                    b64 = str(img.get("data_b64") or "")
                    if b64 and len(b64) <= max_b64:
                        inlined += 1
    if parsed_frames:
        selected = len(keep_frames)
        pruned = len(parsed_frames) - selected
        chars = sum(
            len(str(messages[i].get("content") or "")) for i in parsed_frames
        )
        _log = logger.info if pruned else logger.debug
        _log(
            "image frames: total=%d selected=%d pruned=%d inlined=%d envelope_chars=%d",
            len(parsed_frames), selected, pruned, inlined, chars,
        )
    for i, msg in enumerate(messages):
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            raw = msg["content"]
            tool_name = str(msg.get("name") or "")
            if tool_name in IMAGE_TOOL_ALLOWLIST:
                payload = parsed_frames.get(i)
                if payload is not None:
                    if i not in keep_frames:
                        older = dict(msg)
                        older["content"] = image_envelope_placeholder(raw)
                        out.append(older)
                        continue
                    parts: List[Dict[str, Any]] = []
                    text = str(payload.get("text") or "")
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
