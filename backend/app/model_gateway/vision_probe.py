# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""视觉能力探针 — 主模型视觉能力的行为判定（2026-09-15）。

背景：`[endpoints.vlm]` 未单独配置时，视觉任务（vision_interpret 工具 /
死磕 visual_critic）此前直接失败/跳过。现按用户要求：先对「当前调用的主模型」
做一次测试探针——能看见 → 主模型充当视觉模型；看不见 → 明确返回无视觉能力。

判定是行为式的（绝非声明式）：生成一张随机 6 位 token + 纯色方块的探针图，
经该端点自己的 chat.completions 提问；回答必须同时命中随机 token 与颜色名
（中/英），否则视为不具备视觉能力（文本模型即使幻觉也几乎不可能命中随机
token——盲猜概率 ≈ 1/(32^6 × 4)）。

缓存：按 (base_url, model_name) 进程内 TTL 缓存（成功/失败均缓存，防重复
烧钱探测；配置热切换后 key 变化自动失效）。缓存无锁——并发首探可能重复，
属可接受的稀有成本。
"""
import asyncio
import base64
import io
import logging
import random
import time
from typing import Dict, List, Optional, Tuple

from app.model_gateway.schemas import ModelEndpoint

logger = logging.getLogger(__name__)

PROBE_IMAGE_SIZE = (320, 160)
PROBE_QUESTION = (
    "这是一张用于测试视觉能力的图片。请如实回答：图中写的是什么字符"
    "（大写字母或数字），以及左侧方块是什么颜色？只回答「字符 颜色」，不要解释。"
)
# 无易混字符（无 I/O/0/1）——降低 OCR 误读导致的假阴性。
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# (规范色名, RGB, 可接受别名) —— 中英文回答均接受。
PROBE_COLORS: List[Tuple[str, Tuple[int, int, int], Tuple[str, ...]]] = [
    ("red", (220, 30, 30), ("red", "红")),
    ("green", (30, 160, 60), ("green", "绿")),
    ("blue", (30, 80, 220), ("blue", "蓝")),
    ("purple", (140, 50, 180), ("purple", "紫")),
]

_PROBE_TIMEOUT_SECONDS = 60.0
# 正例 TTL 长（能力稳定）；负例 TTL 短——容忍思考模型偶发误读导致的假阴性，
# 代价是一次廉价重探。
_CACHE_TTL_SECONDS = 600.0
_NEGATIVE_CACHE_TTL_SECONDS = 120.0
_CACHE_MAX_ENTRIES = 256
# 思考模型（qwen3.8 实测 reasoning ~220-240 字）会把小预算全烧在思考上导致
# content 为空并触发内部重试——探针给足预算。
_PROBE_MAX_TOKENS = 512

_CACHE: Dict[str, Tuple[float, bool]] = {}


def make_probe_png() -> Tuple[bytes, str, str]:
    """生成探针图 → (png bytes, token, 规范色名)。"""
    from PIL import Image, ImageDraw, ImageFont

    token = "".join(random.choices(_TOKEN_ALPHABET, k=6))
    color_name, rgb, _aliases = random.choice(PROBE_COLORS)
    img = Image.new("RGB", PROBE_IMAGE_SIZE, "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 40, 100, 120], fill=rgb)
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:  # 旧 Pillow：load_default 无 size 参数
        font = ImageFont.load_default()
    draw.text((130, 55), token, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), token, color_name


def _color_aliases(color_name: str) -> Tuple[str, ...]:
    for name, _rgb, aliases in PROBE_COLORS:
        if name == color_name:
            return aliases
    return (color_name,)


def _token_matches(answer_lower: str, token: str) -> bool:
    """token 命中判定：精确子串，或任一等长窗口 ≥5/6 位置字符一致。

    容 1 字符 OCR 误差（vibeweaver mm_probe 同款判据）：真实视觉模型偶发
    单字符误读不应判为无视觉；而盲猜命中 5/6 的概率 ≈ C(6,1)·35/36^6 ≈ 3e-8，
    无感知模型仍不可能通过。"""
    token_l = token.lower()
    if token_l in answer_lower:
        return True
    import re
    need = len(token_l) - 1
    for run in re.findall(r"[a-z0-9]+", answer_lower):
        for i in range(0, len(run) - len(token_l) + 1):
            window = run[i:i + len(token_l)]
            if sum(1 for a, b in zip(window, token_l) if a == b) >= need:
                return True
    return False


async def _run_probe(ep: ModelEndpoint) -> Optional[bool]:
    """单次探针 → True/False（确定性判定）；None = 不确定（超时/连接/5xx）。

    A4.9 R1 Minor-4：确定性判定才进缓存——瞬时失败缓存 10 分钟会把一次上游
    抖动变成用户可见的确定性「不具备视觉能力」。4xx（图片不支持/模型拒绝）
    属确定性「无视觉」；超时/连接错误/5xx 属不确定。"""
    try:
        png, token, color_name = make_probe_png()
        b64 = base64.b64encode(png).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROBE_QUESTION},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }]
        from app.model_gateway import factory
        # 空 Key 绝不回落系统 Key（同 vision_interpret / visual_critic 约定）。
        ep_wired = ep.with_overrides(api_key=ep.api_key or "no-key")
        llm = factory.build_llm_service(ep_wired)
        content, _reasoning = await asyncio.wait_for(
            llm.complete_chat_parts(messages, temperature=0, max_tokens=_PROBE_MAX_TOKENS),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            logger.info(
                "vision probe: endpoint %s (%s/%s) rejected image input (%s) — "
                "definitive no-vision", ep.alias, ep.base_url, ep.model_name, exc,
            )
            return False
        logger.info(
            "vision probe: endpoint %s (%s/%s) probe indeterminate: %s — "
            "not cached, retried next time",
            ep.alias, ep.base_url, ep.model_name, exc,
        )
        return None
    answer = (content or "").lower()
    if not _token_matches(answer, token):
        logger.info(
            "vision probe: endpoint %s answered without the probe token — "
            "treating as no vision", ep.alias,
        )
        return False
    if not any(alias in answer for alias in _color_aliases(color_name)):
        logger.info(
            "vision probe: endpoint %s missed the probe color — treating as no vision",
            ep.alias,
        )
        return False
    return True


async def probe_vision(ep: ModelEndpoint) -> bool:
    """探针判定端点是否具备视觉能力（进程内 TTL 缓存；不确定结果不缓存）。"""
    key = f"{ep.base_url}|{ep.model_name}"
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    capable = await _run_probe(ep)
    if capable is None:
        # 不确定（瞬时失败）：本次返回 False（无法用于视觉任务），但不写缓存。
        return False
    ttl = _CACHE_TTL_SECONDS if capable else _NEGATIVE_CACHE_TTL_SECONDS
    _CACHE[key] = (time.monotonic() + ttl, capable)
    while len(_CACHE) > _CACHE_MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)), None)
    logger.info(
        "vision probe: %s/%s capable=%s (cached %.0fs)",
        ep.base_url, ep.model_name, capable, ttl,
    )
    return capable


async def resolve_vision_endpoint(
    assistant=None, main_endpoint: Optional[ModelEndpoint] = None,
) -> Tuple[Optional[ModelEndpoint], str]:
    """视觉端点解析 → (endpoint | None, reason)。

    reason ∈ {"configured", "main_model", "no_vision", "error:<msg>"}：
    1. `[endpoints.vlm]` 已配置 → 直接使用（行为不变，不探测）。
    2. 未配置 → 当前主模型（``main_endpoint`` 显式给定优先——调用方持有助手
       端点时用；否则助手端点，最后 main）探针：有视觉 → 主模型充当视觉模型；
       无视觉 → (None, "no_vision")。
    """
    try:
        from app.model_gateway.registry import get_model_registry
        registry = get_model_registry()
        ep = registry.resolve("vlm")
        if (ep.base_url or "") and (ep.model_name or ""):
            return ep, "configured"
        main_ep = main_endpoint if main_endpoint is not None else registry.endpoint_for_assistant(assistant)
        if not (main_ep.base_url or "") or not (main_ep.model_name or ""):
            return None, "no_vision"
        if await probe_vision(main_ep):
            return main_ep, "main_model"
        return None, "no_vision"
    except Exception as exc:
        logger.warning("vision endpoint resolution failed: %s", exc, exc_info=True)
        return None, f"error:{exc}"
