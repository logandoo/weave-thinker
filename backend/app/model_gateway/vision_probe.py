# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""视觉能力探针 — 主模型视觉能力的行为判定（2026-09-15，2026-11-01 优先顺序反转）。

背景：视觉能力（vision_interpret 工具 / 死磕 visual_critic）必须**优先使用当前
调用的主模型**——用户 2026-11-01 的明确限制。先对主模型做一次行为探针：能看见
→ 主模型充当视觉模型；看不见 → 才回落到系统 `[endpoints.vlm]`。

（2026-09-15 首版语义是「vlm 已配置则直接使用、不探针」；该语义会让过期的 vlm
配置压过有视觉的主模型——60 生产 `mimo-v2.5` 被上游下线后 vision_interpret 持续
400，而主模型 deepseek-flash 本身能看图。故反转为 main-model-first。）

判定是行为式的（绝非声明式）：生成一张随机 6 位 token + 纯色方块的探针图，
经该端点自己的 chat.completions 提问；回答必须同时命中随机 token 与颜色名
（中/英），否则视为不具备视觉能力（文本模型即使幻觉也几乎不可能命中随机
token——盲猜概率 ≈ 1/(32^6 × 4)）。

缓存：按 (base_url, model_name, api_key 指纹) 进程内 TTL 缓存（成功/失败均缓存，
防重复烧钱探测；密钥/模型热切换后键变化自动失效）。并发首探共享同一个
in-flight future（单飞），不确定结果不缓存但同样只打一次上游。
"""
import asyncio
import base64
import hashlib
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
# 并发首探去重（A4.9 R2-I1）：key = (loop id, _cache_key) → 共享 in-flight future。
_INFLIGHT: Dict[tuple, "asyncio.Future[bool]"] = {}


def _cache_key(ep: ModelEndpoint) -> str:
    """缓存键 = base_url | model_name | api_key 指纹。

    key 必须进键：用户级覆盖可「同 URL/模型、不同密钥」（A4.9 R2-I2），
    能力判定与负例都不能跨凭据复用；密钥轮换后旧判定也自动失效。"""
    fp = hashlib.sha256((ep.api_key or "").encode("utf-8")).hexdigest()[:16]
    return f"{ep.base_url}|{ep.model_name}|{fp}"


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

    A4.9 R1 Minor-4 / R2-I2：只有**确定性**判定才进缓存——瞬时失败缓存 10 分钟
    会把一次上游抖动变成用户可见的确定性「不具备视觉能力」。确定性「拒绝图片
    输入」= 400/415/422；鉴权(401/403)、模型不存在(404)、超时/连接/429/5xx
    属不确定（不缓存，下次重探）。"""
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
        # is_custom=True：必须用**这个端点自己的** base_url——is_custom=False 的
        # 端点会让 factory 回落 `[api]` 全局地址，探针就测错了对象（A4.9 R2-I4）。
        ep_wired = ep.with_overrides(api_key=ep.api_key or "no-key", is_custom=True)
        llm = factory.build_llm_service(ep_wired)
        content, _reasoning = await asyncio.wait_for(
            llm.complete_chat_parts(messages, temperature=0, max_tokens=_PROBE_MAX_TOKENS),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        # 确定性「拒绝图片输入」= 400 / 415 / 422；鉴权(401/403)、模型不存在(404)、
        # 超时/5xx/429 属不确定——A4.9 R2-I2：把它们判成「不具备视觉能力」会让
        # 配置故障伪装成能力结论，并在无 vlm 时给用户错误的处置建议。
        if status in (400, 415, 422):
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
    """探针判定端点是否具备视觉能力（进程内 TTL 缓存；不确定结果不缓存）。

    A4.9 R2-I1：并发去重（single-flight）——`vision_interpret` 是 parallel-safe
    工具，多个并行视觉调用会同时首探。这里共享**同一个 in-flight future**，
    而不是让等待者排队重探：不确定结果（超时/5xx）不进缓存，若用锁串行，
    N 个并发调用会退化成 N×60s（比修复前更糟）。等待者拿到首探者的同一个结论
    （含「不确定 → False」），不额外打上游。
    缓存键含 api_key 指纹：用户级覆盖可只换 key，能力判定不能跨凭据复用
    （A4.9 R2-I2：跨用户能力泄漏/负例污染）。future 按事件循环隔离，避免
    测试/多 loop 场景下复用已关闭 loop 的 future。
    """
    key = _cache_key(ep)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]

    loop_key = (id(asyncio.get_running_loop()), key)
    fut = _INFLIGHT.get(loop_key)
    if fut is not None:
        try:
            return await asyncio.shield(fut)
        except RuntimeError:
            # 已关闭的事件循环遗留的 future（测试/多 loop）：丢弃后自己重探。
            _INFLIGHT.pop(loop_key, None)

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _INFLIGHT[loop_key] = fut
    # 清理死循环遗留 + 超容（A4.9 R3-N4：id(loop) 复用前必须清掉）。
    for _k in list(_INFLIGHT):
        _f = _INFLIGHT[_k]
        try:
            if _f.done() or _f.get_loop().is_closed():
                _INFLIGHT.pop(_k, None)
        except RuntimeError:
            _INFLIGHT.pop(_k, None)
    while len(_INFLIGHT) > _CACHE_MAX_ENTRIES:
        _INFLIGHT.pop(next(iter(_INFLIGHT)), None)
    try:
        capable = await _run_probe(ep)
        if capable is None:
            # 不确定（瞬时失败）：本次返回 False（无法用于视觉任务），不写缓存。
            result = False
        else:
            ttl = _CACHE_TTL_SECONDS if capable else _NEGATIVE_CACHE_TTL_SECONDS
            _CACHE[key] = (time.monotonic() + ttl, capable)
            while len(_CACHE) > _CACHE_MAX_ENTRIES:
                _CACHE.pop(next(iter(_CACHE)), None)
            logger.info(
                "vision probe: %s/%s capable=%s (cached %.0fs)",
                ep.base_url, ep.model_name, capable, ttl,
            )
            result = capable
        if not fut.done():
            fut.set_result(result)
        return result
    except BaseException:
        if not fut.done():
            fut.set_result(False)  # 等待者 fail-closed，不永久挂起
        raise
    finally:
        if _INFLIGHT.get(loop_key) is fut:
            _INFLIGHT.pop(loop_key, None)


async def resolve_vision_endpoint(
    assistant=None, main_endpoint: Optional[ModelEndpoint] = None,
) -> Tuple[Optional[ModelEndpoint], str]:
    """视觉端点解析 → (endpoint | None, reason)。

    优先顺序（2026-11-01 用户明确限制：视觉能力优先用当前调用的主模型）：
    1. **主模型**（``main_endpoint`` 显式给定优先——调用方持有助手端点时用；
       否则助手端点，最后 `[routing] main` 目标）先做行为探针：通过 → 主模型
       充当视觉模型（reason="main_model"）。
    2. 主模型探针不通过 → `[endpoints.vlm]` 已配置（base_url + model_name）则
       回落到它（reason="configured"）。
    3. 两者都不可用 → (None, "no_vision")。

    A4.9 R1-I1：`[endpoints.main]` 为空但 `[routing] main` 指向池中别名时，
    `endpoint_for_assistant(None)` 会拿到空端点——必须再试 `resolve("main")`，
    否则 vlm 会在「零探针」的情况下胜出（正是本次事故的形态）。
    A4.9 R1-M2：主端点解析/探针抛错不应整体失败——回落 vlm 仍然可用。

    reason ∈ {"configured", "main_model", "no_vision", "error:<msg>"}。
    """
    try:
        from app.model_gateway.registry import get_model_registry
        registry = get_model_registry()
    except Exception as exc:  # 注册表本身不可用 → 明确配置错误
        logger.warning("vision endpoint resolution failed: %s", exc, exc_info=True)
        return None, f"error:{exc}"

    main_ep = main_endpoint
    if main_ep is None:
        try:
            main_ep = registry.endpoint_for_assistant(assistant)
        except Exception as exc:
            logger.info("vision: assistant main endpoint resolution failed: %s", exc)
            main_ep = None
    if main_ep is None or not (main_ep.base_url or "") or not (main_ep.model_name or ""):
        try:
            routed = registry.resolve("main")
            if (routed.base_url or "") and (routed.model_name or ""):
                main_ep = routed
        except Exception as exc:
            logger.info("vision: [routing] main resolution failed: %s", exc)

    if main_ep is not None and (main_ep.base_url or "") and (main_ep.model_name or ""):
        try:
            if await probe_vision(main_ep):
                return main_ep, "main_model"
        except Exception as exc:  # 探针自身故障 → 继续尝试 vlm 回落
            logger.info("vision: main probe errored, falling back: %s", exc)

    try:
        ep = registry.resolve("vlm")
    except Exception as exc:
        logger.warning("vision endpoint resolution failed: %s", exc, exc_info=True)
        return None, f"error:{exc}"
    if (ep.base_url or "") and (ep.model_name or ""):
        # 与主模型完全同一端点（同 url+model+key）不算「已配置视觉模型」——
        # 主模型探针已失败，再返回它只会把同一个错误搬个位置。显式把
        # [routing] vlm 指到主别名、或用户覆盖导致同端点时命中此分支。
        same_as_main = (
            ep is main_ep
            or (
                (ep.base_url or "") == (getattr(main_ep, "base_url", "") or "")
                and (ep.model_name or "") == (getattr(main_ep, "model_name", "") or "")
                and (ep.api_key or "") == (getattr(main_ep, "api_key", "") or "")
            )
        )
        if not same_as_main:
            return ep, "configured"
    return None, "no_vision"
