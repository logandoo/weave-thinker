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

import asyncio
import base64
import json
import logging
import math
import os
import re
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.services import image_blob_store as _blob_store
from app.services.image_blob_store import (
    b64_chars_for_bytes as _b64_chars_for_bytes,
    frame_b64_chars as _blob_frame_chars,
    materialize_frame as _materialize_frame,
    note_disk_activity as _note_disk_activity,
)

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


# ---------------------------------------------------------------------------
# P0（2026-11-09 图片信封零损失内存改造 §6）：按载荷对象身份记忆化信封解析
#
# 事故放大点：一轮内 context_compressor（4 处）、history_window_service（3 处）、
# tool_result_budget（2 处）与 wire 展开各自对**全部**信封跑 `parse_image_envelope`
# = `json.loads` 全量 base64。75M 字符的会话每轮因此重复解析 ~10 次。
#
# 键 = `id(content)`（载荷字符串对象身份），条目里**强引用**那个字符串：
#   · 字符串不可变 ⇒ 同一对象永远对应同一解析结果，结构上不可能读到过期载荷；
#   · 强引用让 id() 在条目存活期间不可能被复用 ⇒ 不会张冠李戴；
#   · 字节上限 LRU（按 content + 解析副本 ≈ 2× 计账）保证它不会变成第二份无界驻留。
# 为什么不是"按消息字典身份"：CPython 的 `dict` **不支持弱引用**
# （`weakref.ref({})` → TypeError，本波实测），只持弱引用的方案在 dict 上根本不成立；
# 而消息字典的 content 一旦被就地改写，按消息缓存反而会读到过期载荷。
# 见 tests/verification_log.md iter 4 的 H1 证伪记录。
# ---------------------------------------------------------------------------

_PARSE_MEMO_MAX_BYTES = 32 * 1024 * 1024   # 与 blob LRU 同档；0 = 关闭记忆化
_PARSE_MEMO_MAX_ENTRIES = 512

_parse_memo: "OrderedDict[int, Tuple[str, Any]]" = OrderedDict()
_json_memo: "OrderedDict[int, Tuple[Any, Callable[[Any], Any], Any]]" = OrderedDict()
_parse_memo_bytes = 0
_json_memo_bytes = 0
_parse_stats = {"hits": 0, "misses": 0, "parses": 0}


def _parse_memo_limits() -> Tuple[int, int]:
    try:
        from app.core.config import get_config

        blobs = (get_config().agent or {}).get("image_blobs", {}) or {}
        max_bytes = int(blobs.get("parse_memo_max_bytes", _PARSE_MEMO_MAX_BYTES))
        max_entries = int(blobs.get("parse_memo_max_entries", _PARSE_MEMO_MAX_ENTRIES))
    except Exception:
        return _PARSE_MEMO_MAX_BYTES, _PARSE_MEMO_MAX_ENTRIES
    return max(0, max_bytes), max(1, max_entries)


def parse_cache_stats() -> Dict[str, int]:
    """命中/未命中/实际全量解析次数（spec §6 P0 测试缝「解析调用计数下降的可断言点」）。"""
    return dict(_parse_stats)


def clear_parse_cache() -> None:
    """清空记忆化与计数器（测试隔离；生产路径不需要调用）。"""
    global _parse_memo_bytes, _json_memo_bytes
    _parse_memo.clear()
    _json_memo.clear()
    _parse_memo_bytes = 0
    _json_memo_bytes = 0
    for key in _parse_stats:
        _parse_stats[key] = 0


def _memo_weight(raw: Any, payload: Any) -> int:
    """条目字节账：载荷字符串 + json.loads 出来的副本（≈ 同量级）。"""
    return 2 * len(str(raw or ""))


def _memo_used(cache: dict) -> int:
    return _parse_memo_bytes if cache is _parse_memo else _json_memo_bytes


def _memo_add(cache: dict, delta: int) -> None:
    global _parse_memo_bytes, _json_memo_bytes
    if cache is _parse_memo:
        _parse_memo_bytes = max(0, _parse_memo_bytes + delta)
    else:
        _json_memo_bytes = max(0, _json_memo_bytes + delta)


def _memo_store(cache: "OrderedDict[int, Any]", key: int, entry: Any, weight: int) -> None:
    max_bytes, max_entries = _parse_memo_limits()
    if max_bytes <= 0:
        return  # 记忆化被关闭（[agent.image_blobs].parse_memo_max_bytes = 0）
    # 覆盖同名条目时必须先扣掉**旧条目**的字节账，否则每覆盖一次 `_parse_memo_bytes`
    # 单向虚增：同一对象被两个 loader 交替解析（`history_window_service` 就同时存在
    # 两条 loader）会让表"看起来"装满，把真正的热信封逐出。与 image_blob_store
    # 的 `_cache_put` 同一个修法（A4.9 二轮评审 A·I7）。
    _replaced = cache.get(key)
    if _replaced is not None:
        _memo_add(cache, -2 * len(str(_replaced[0] or "")))
    cache[key] = entry
    _memo_add(cache, weight)
    while cache and (_memo_used(cache) > max_bytes or len(cache) > max_entries):
        _old_key, _old_entry = cache.popitem(last=False)
        _memo_add(cache, -2 * len(str(_old_entry[0] or "")))


def parse_image_envelope_memoized(content: str) -> Optional[Dict[str, Any]]:
    """`parse_image_envelope` 的记忆化版本（键 = 载荷字符串对象身份）。

    形状检查先行：非信封字符串不进表也不计数，否则普通工具结果会把真正的大信封
    从 LRU 里挤出去。任何"手里只有 content 字符串"的热路径都应走这里而不是裸
    `parse_image_envelope`（占位符与摘要路径就是被漏掉的两个点，A4.9 评审命中）。
    """
    if not is_image_envelope(content):
        return None
    key = id(content)
    entry = _parse_memo.get(key)
    if entry is not None and entry[0] is content:
        _parse_stats["hits"] += 1
        _parse_memo.move_to_end(key)
        return entry[1]
    payload = parse_image_envelope(content)
    _parse_stats["misses"] += 1
    _memo_store(_parse_memo, key, (content, payload), _memo_weight(content, payload))
    return payload


def memoized_json_for_message(msg: Any, raw: Any, loader: Callable[[Any], Any]) -> Any:
    """按「载荷对象身份 + loader」记忆化一次 JSON 解析。

    `raw` 必须是调用方长期持有的那个对象（str / dict / list），否则一律重解析。
    与 `parse_image_envelope_memoized` 分表存放：同一对象被不同 loader 解析时
    绝不互相污染。
    """
    if not isinstance(raw, (str, dict, list)):
        return loader(raw)
    key = id(raw)
    entry = _json_memo.get(key)
    if entry is not None and entry[0] is raw and entry[1] is loader:
        _parse_stats["hits"] += 1
        _json_memo.move_to_end(key)
        return entry[2]
    value = loader(raw)
    _parse_stats["misses"] += 1
    _memo_store(_json_memo, key, (raw, loader, value), _memo_weight(raw, value))
    return value


def parse_image_envelope_for_message(msg: Any) -> Optional[Dict[str, Any]]:
    """`parse_image_envelope` 的记忆化入口（消费方一律走这里）。

    廉价形状检查不命中时**不进缓存也不计数**：普通工具结果不是本机制的目标，
    给它们建条目只会把真正的大信封从 LRU 里挤出去。
    """
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    if not isinstance(content, str) or not is_image_envelope(content):
        return None
    return parse_image_envelope_memoized(content)


def _parse_tool_results_payload(raw: Any) -> Any:
    """ORM/legacy `tool_results`  blob → 解析后的结构（失败返回 None，调用方回落）。"""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def parse_tool_results_for_message(msg: Any, raw: Any) -> Any:
    """`tool_results` 大 blob 的按消息记忆化解析（history_window 热点 :164）。"""
    return memoized_json_for_message(msg, raw, _parse_tool_results_payload)


# ---------------------------------------------------------------------------
# 驻留/上线双口径上报（spec §4.7；60 OOM 的直接教训是没有驻留指标 + 口径误读）
# ---------------------------------------------------------------------------


def envelope_chars_of_result(result: Any) -> int:
    """廉价（**不** json.loads）测算一个 tool 结果载荷里的信封字符数。

    只做形状检查（头部 80 字符），因此可以安全地挂在每个 SSE 事件上。
    """
    if isinstance(result, str) and is_image_envelope(result):
        return len(result)
    return 0


def envelope_chars_of_tool_result(tr: Any) -> int:
    """SSE `tool_result` 载荷 / 累积数组元素 → 信封字符数（非产图工具恒为 0）。"""
    if not isinstance(tr, dict):
        return envelope_chars_of_result(tr)
    if str(tr.get("name") or "") not in IMAGE_TOOL_ALLOWLIST:
        return 0
    return envelope_chars_of_result(tr.get("result") or tr.get("content"))


def _note_residency(resident_chars: int, wire_chars: int, has_blob_refs: bool) -> None:
    """wire 前上报两个口径并按分档软阈值告警。指标异常绝不影响请求（HC-5）。"""
    try:
        from app.services import image_blob_metrics as _metrics
        from app.services.image_blob_context import current_session_key

        _metrics.set_carrier("messages", resident_chars)
        _metrics.note_wire_chars(wire_chars)
        _metrics.evaluate_soft_thresholds(
            session_key=current_session_key(),
            wire_chars=wire_chars,
            has_blob_refs=has_blob_refs,
        )
    except Exception:
        logger.debug("image residency metrics failed", exc_info=True)


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
    """Parse an image envelope; return None for plain text / malformed JSON.

    每次调用都会真跑一次 `json.loads`（全量 base64）——重复估算的热度请用
    `parse_image_envelope_for_message(msg)`，它按消息身份记忆化（P0 2026-11-09）。
    """
    if not is_image_envelope(content):
        return None
    _parse_stats["parses"] += 1
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
                payload = parse_image_envelope_for_message(msg)
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


def _png_size_from_bytes(data: bytes) -> Optional[tuple]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _png_size_from_b64(b64: str) -> Optional[tuple]:
    """Read PNG IHDR dimensions from the first base64 chunk (no full decode)."""
    try:
        chunk = str(b64 or "")[:64]
        chunk += "=" * ((4 - len(chunk) % 4) % 4)
        return _png_size_from_bytes(base64.b64decode(chunk))
    except Exception:
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
    """Replace an old image frame with a compact, path-bearing placeholder.

    走记忆化入口：被裁掉的每一帧在**每次** wire 构造时都会被占位一次，用裸
    `parse_image_envelope` 就等于每轮把注定不上线的 MB 级 base64 再全量
    `json.loads` 一遍——正是 P0 要消灭的那个放大点（A4.9 评审 B·C3）。
    """
    payload = parse_image_envelope_memoized(content)
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


async def build_image_envelope_from_file(
    path: str,
    text: str,
    *,
    display_path: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """从磁盘图片文件构造信封；非图片/超限/读失败返回 None。

    P0.5：附带 width/height/path——尺寸供 token 估算，path 供旧帧占位与
    vision_interpret 重读；三者都是可选字段，旧消费方无感。path 默认只写
    文件名（不把宿主机绝对路径送给模型/落库）；调用方可传工作区相对路径。

    P1b（2026-11-09，spec §4.2）：`[agent.image_blobs].offload = true` 时字节落到
    blob 目录，信封里只留 `{blob_ref, b64_chars, sha256}`、**不再有 `data_b64`**。
    四条硬约束：
      1. 读文件 / 探尺寸 / 落盘全部在别的线程（同步写 8MB 会卡事件循环几百 ms，
         60 机同机 4 服务抢 IO ⇒ HC-5）；
      2. width/height **必须**取得，否则不许出 ref（缺尺寸 ⇒ token 估算退化 ⇒
         压缩迟发 ⇒ 上游 4xx = 降质）；
      3. 落盘失败 / 没有 user_id ⇒ **保持今天的行为**（内联 data_b64）+ 计数 +
         WARN 升级。宁可多占内存，绝不丢内容；
      4. 开关 off ⇒ 与今天逐字节相同（含 key 顺序，回滚面零变化）。
    """
    mime = mime_for_path(path)
    if not mime:
        return None
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_INLINE_BYTES:
            return None
        data = await asyncio_to_thread(_read_image_bytes, path)
    except OSError:
        return None

    dims = await asyncio_to_thread(_probe_image_dims, path, data)
    offload = _blob_offload_enabled()
    ref: Optional[str] = None
    sha256_hex: Optional[str] = None
    b64_chars = 0

    if offload:
        if not dims:
            _note_blob_write_failure("dims_unavailable")
        elif not user_id:
            _note_blob_write_failure("no_user_context")
        else:
            try:
                # 落盘走 `asyncio_to_thread`（本模块级别名 = 测试断言"写不在事件
                # 循环上"的唯一窄缝；绕过去那条测试就看不见写）。
                ref, sha256_hex, b64_chars = await asyncio_to_thread(
                    _write_blob_off_loop, user_id, data
                )
            except Exception as exc:
                logger.warning(
                    "image blob write failed (%s) — 退回内联 base64（OOM 向量回归计数 +1）: %s",
                    str(path).rsplit("/", 1)[-1], exc,
                )
                ref = None
                b64_chars = 0
                _note_blob_write_failure("write_error")

    # key 顺序与今天一致：mime → data_b64 → path → width → height
    image: Dict[str, Any] = {"mime": mime}
    if ref is None:
        image["data_b64"] = base64.b64encode(data).decode("ascii")
    image["path"] = str(display_path or os.path.basename(path))
    if dims:
        image["width"], image["height"] = dims
    if ref is not None:
        image["blob_ref"] = ref
        image["b64_chars"] = b64_chars
        image["sha256"] = sha256_hex
    return build_image_envelope(text, [image])


def _write_blob_off_loop(user_id: str, data: bytes) -> Tuple[str, Optional[str], int]:
    """线程里完成"落盘 + 磁盘记账 + 算 b64 长度"，一个字节都不留在事件循环上。

    返回 `(ref, sha256, b64_chars)`。`b64_chars` 用 `4·⌈n/3⌉` 而不是真编码：
    写侧只需要这个数字喂给 24MB 上线裁剪判定。
    """
    ref, sha256_hex = _blob_store.write_blob_sync(user_id, data)
    try:
        from app.services.image_blob_context import uid8_for_user

        _note_disk_activity(uid8_for_user(user_id))
    except Exception:
        logger.debug("blob write-side disk accounting failed", exc_info=True)
    return ref, sha256_hex, _b64_chars_for_bytes(len(data))


def _note_blob_write_failure(reason: str) -> None:
    try:
        from app.services import image_blob_metrics as _metrics

        _metrics.note_write_failure(reason)
    except Exception:
        logger.debug("blob write-failure metric failed", exc_info=True)


async def expand_tool_images(
    messages: List[Dict[str, Any]],
    *,
    owner_uid8: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """出线前展开图片信封为 content parts。普通消息原样透传（浅拷贝）。

    A4.9 I2 硬化：
    - **工具名白名单**——只有产图工具（workspace_read 图像分支）的 tool 消息
      才会展开；terminal 等文本输出里伪造的信封 JSON 不生效（防图像型提示
      注入绕过文本消毒）。
    - **展开期上限**——单图 base64 长度 ≤ MAX_INLINE_BYTES×4/3，总数 ≤ 4。
    - **遵守 off 开关**——inline_tool_images=false 时只回文本，不回图。

    P1a（2026-11-09）：这里是 live loop / history replay / worker 的**唯一必经
    点**，blob 物化只加在这里（spec §4.3）：
      · legacy 信封（带 `data_b64`）→ 用原值，今天行为零变化；
      · ref 信封 → owner 校验 → LRU → `asyncio.to_thread` 读盘 → sha256 校验 →
        物化成与今天**逐字节相同**的 base64；任何一步失败都抛
        `ImageBlobMaterializeError` 冒泡（HC-3 fail-closed，绝不带裸 ref 上线）。
    因此本函数是 async；`select_kept_image_frames` 仍被压缩器同步复用（不物化）。
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
            payload = parse_image_envelope_for_message(msg)
            if payload is not None:
                parsed_frames[i] = payload
    keep_frames = select_kept_image_frames(messages, parsed_frames)

    def _frame_b64_chars(index: int) -> int:
        # spec §4.3：24MB 上线裁剪判定是**唯一**改读的估算点——ref 信封读
        # `b64_chars`（就是原 base64 长度 ⇒ 判定逐位等价），legacy 仍读
        # `data_b64` 长度。`image_envelope_token_count` 保持尺寸驱动，不动。
        payload = parsed_frames.get(index) or {}
        images = payload.get("images")
        if not isinstance(images, list):
            return 0
        return sum(_blob_frame_chars(img) for img in images if isinstance(img, dict))

    total_b64 = sum(_frame_b64_chars(i) for i in keep_frames)
    if total_b64 > MAX_INLINE_TOTAL_B64_CHARS:
        for index in sorted(keep_frames):
            if total_b64 <= MAX_INLINE_TOTAL_B64_CHARS or len(keep_frames) <= 1:
                break
            keep_frames.discard(index)
            total_b64 -= _frame_b64_chars(index)

    images_ok = inline_images_enabled()
    # P1a：只给**留下来**的帧物化（24MB 裁剪之后），省掉注定不上线的磁盘读。
    # 物化失败直接冒泡 —— 这里绝不能 try/except：那正是 spec §4.3 要点名消灭的
    # "裸 ref 进模型上下文"路径。
    wire_b64: Dict[int, List[Optional[str]]] = {}
    if images_ok:
        for index in sorted(keep_frames):
            payload = parsed_frames.get(index) or {}
            per_frame: List[Optional[str]] = []
            for img in (payload.get("images") or [])[:4]:
                if not isinstance(img, dict):
                    per_frame.append(None)
                    continue
                b64 = str(img.get("data_b64") or "")
                if not b64 and img.get("blob_ref"):
                    b64 = await _materialize_frame(img, owner_uid8)
                per_frame.append(b64 or None)
            wire_b64[index] = per_frame

    inlined = 0
    wire_chars = 0
    if images_ok:
        for index in keep_frames:
            for b64 in wire_b64.get(index) or []:
                if b64 and len(b64) <= max_b64:
                    inlined += 1
                    wire_chars += len(b64)
    if parsed_frames:
        selected = len(keep_frames)
        pruned = len(parsed_frames) - selected
        # 两个口径分开（P0 2026-11-09；60 OOM 事故里 `envelope_chars` 被普遍
        # 误读成"上线体积"，实际是消息列表里的**常驻**字符总和）：
        #   resident = 驻留（消息列表这一份，另有累积/stream buffer 两份由各自
        #              载体上报，见 image_blob_metrics.set_carrier）
        #   wire     = 本轮真正上线的 base64 字符量
        resident_chars = sum(
            len(str(messages[i].get("content") or "")) for i in parsed_frames
        )
        has_blob_refs = any(
            any(
                isinstance(im, dict) and im.get("blob_ref")
                for im in (payload.get("images") or [])
            )
            for payload in parsed_frames.values()
        )
        _note_residency(resident_chars, wire_chars, has_blob_refs)
        _log = logger.info if pruned else logger.debug
        _log(
            "image frames: total=%d selected=%d pruned=%d inlined=%d "
            "resident_chars=%d (消息列表内全部信封的驻留字符，非上线量) "
            "wire_chars=%d (本轮实际上线 base64 字符)",
            len(parsed_frames), selected, pruned, inlined, resident_chars, wire_chars,
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
                        frame_b64s = wire_b64.get(i) or []
                        for pos, img in enumerate((payload.get("images") or [])[:4]):
                            if not isinstance(img, dict):
                                continue
                            mime = str(img.get("mime") or "image/png")
                            b64 = frame_b64s[pos] if pos < len(frame_b64s) else None
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


# asyncio.to_thread 的模块级别名：写侧测试要断言"落盘不在事件循环上"，
# 直接 patch asyncio.to_thread 会波及整个进程，这里留一个窄口子。
asyncio_to_thread = asyncio.to_thread


def _blob_offload_enabled() -> bool:
    """写侧开关（P1b）。默认 false = 与今天逐字节相同；读取点对两种信封都工作。"""
    try:
        from app.core.config import get_config

        return bool(get_config().agent_image_blob_offload)
    except Exception:
        return False


def _read_image_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _probe_image_dims(path: str, data: bytes) -> Optional[tuple]:
    """尺寸探测：PIL 读文件优先，失败回落 PNG IHDR。取不到 → None。

    P1b 强约束（spec §4.2 第 3 条）：**没有尺寸就不许出 ref**——token 估算是
    尺寸驱动，缺尺寸会退化成 `len/160` 甚至 1 token/帧 ⇒ 低估 ⇒ 压缩迟发 ⇒
    上游 4xx = 降质。legacy 内联路径仍允许无尺寸（与今天一致）。
    """
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        pass
    return _png_size_from_bytes(data)
