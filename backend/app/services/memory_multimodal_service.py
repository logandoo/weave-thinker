# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""多模态冷启动 fallback（M&D §6 [memory.multimodal] / §9.9 / §5.2 冷启动回退次优路径）

OCR-Memory (acl-long.474) harness-layer 范式：把最近 N 条 subconscious raw 单元
渲染为带 Set-of-Mark 编号的 PNG，调用主多模态 LLM locate-and-transcribe，
把命中的原文单元作为冷启动上下文注入。替代旧 5-30s 阻塞摘要式 fallback。

显式 opt-in：[memory.multimodal].enabled = true（默认 false）。
失败模式（§9.9）：
  - PNG 渲染失败 → 返回 None（调用方回退），不计禁用计数
  - 多模态 LLM 失败 → 返回 None；连续 3 次失败 → 进程内禁用 + logger.error（admin 报警）
"""
import asyncio
import base64
import io
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config

config = get_config()
logger = logging.getLogger(__name__)

_failure_state = {"consecutive": 0, "disabled": False}

_CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

_CARD_WIDTH = 1000
_CARD_MARGIN = 16
_CARD_PADDING = 12
_BADGE_SIZE = 34
_TEXT_SIZE = 22
_INDEX_SIZE = 24
_MAX_TEXT_CHARS_PER_CARD = 260


def _mm_cfg() -> dict:
    return config.memory_multimodal


def _load_cjk_font(size: int):
    from PIL import ImageFont
    for path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    raise RuntimeError("no CJK font available for snapshot rendering")


def _wrap_cjk(text_str: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for para in text_str.split("\n"):
        current = ""
        for ch in para:
            trial = current + ch
            if font.getlength(trial) > max_width and current:
                lines.append(current)
                current = ch
            else:
                current = trial
        if current or not para:
            lines.append(current)
    return lines


def _token_budget_max_height() -> int:
    """OpenAI 高细节视觉计费 ≈ 85 + 170 × (⌈w/512⌉ × ⌈h/512⌉)；反推高度上限。"""
    max_tokens = int(_mm_cfg().get("max_image_tokens", 4000))
    w_tiles = max(1, -(-_CARD_WIDTH // 512))
    budget_tiles = max(1, (max_tokens - 85) // 170)
    h_tiles = max(1, budget_tiles // w_tiles)
    return h_tiles * 512


def render_subconscious_snapshot_png(units: list[dict], set_of_mark: bool = True) -> bytes:
    """把 raw 单元渲染为编号卡片 PNG（SoM 编号红框）。空列表 → ValueError。"""
    if not units:
        raise ValueError("units must be non-empty")

    from PIL import Image, ImageDraw

    text_font = _load_cjk_font(_TEXT_SIZE)
    index_font = _load_cjk_font(_INDEX_SIZE)

    max_text_width = _CARD_WIDTH - 2 * _CARD_PADDING - (set_of_mark * (_BADGE_SIZE + 8))
    max_height = _token_budget_max_height()

    cards: list[list[str]] = []
    total_height = _CARD_MARGIN
    for i, u in enumerate(units):
        kind = u.get("unit_kind", "message")
        created = u.get("created_at", "")
        if isinstance(created, datetime):
            created = created.strftime("%m-%d %H:%M")
        header = f"[{i + 1}] ({kind}) {created}"
        body = (u.get("raw_text") or "")[:_MAX_TEXT_CHARS_PER_CARD]
        lines = [header] + _wrap_cjk(body, text_font, max_text_width)
        card_h = len(lines) * (_TEXT_SIZE + 8) + 2 * _CARD_PADDING
        if total_height + card_h + _CARD_MARGIN > max_height and cards:
            break
        cards.append(lines)
        total_height += card_h + _CARD_MARGIN

    if not cards:
        raise ValueError("no card fits within image token budget")

    img = Image.new("RGB", (_CARD_WIDTH + 2 * _CARD_MARGIN, total_height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    y = _CARD_MARGIN
    for i, lines in enumerate(cards):
        card_h = len(lines) * (_TEXT_SIZE + 8) + 2 * _CARD_PADDING
        x0, y0 = _CARD_MARGIN, y
        x1, y1 = _CARD_MARGIN + _CARD_WIDTH, y + card_h
        if set_of_mark:
            draw.rectangle([x0, y0, x1, y1], outline=(220, 38, 38), width=3)
            draw.rectangle([x0, y0, x0 + _BADGE_SIZE, y0 + _BADGE_SIZE], fill=(220, 38, 38))
            badge = str(i + 1)
            tb = draw.textbbox((0, 0), badge, font=index_font)
            draw.text(
                (x0 + (_BADGE_SIZE - (tb[2] - tb[0])) / 2, y0 + (_BADGE_SIZE - (tb[3] - tb[1])) / 2 - tb[1]),
                badge, fill=(255, 255, 255), font=index_font,
            )
        else:
            draw.rectangle([x0, y0, x1, y1], outline=(120, 120, 120), width=1)

        tx = x0 + _CARD_PADDING + (set_of_mark * (_BADGE_SIZE + 8))
        ty = y0 + _CARD_PADDING
        for ln in lines:
            draw.text((tx, ty), ln, fill=(17, 17, 17), font=text_font)
            ty += _TEXT_SIZE + 8
        y = y1 + _CARD_MARGIN

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _call_mm_llm_messages(messages: list, user_id: str, db: Optional[AsyncSession] = None) -> str:
    """调用主多模态 LLM（chat completions，含 image_url content）。返回文本。"""
    from app.services.llm_service import LLMService

    provider_name = _mm_cfg().get("provider", "default")
    if provider_name and provider_name != "default":
        from app.services.provider_router import get_provider_router
        router = get_provider_router()
        kwargs = router.get_client_kwargs(provider_name)
        llm = LLMService(
            custom_api_url=kwargs.get("base_url"),
            custom_api_key=kwargs.get("api_key"),
            custom_model_name=router.get_model_name(provider_name),
        )
    else:
        llm = LLMService()

    response = await asyncio.wait_for(
        llm.complete_chat(messages, temperature=0.1),
        timeout=60,
    )
    return (response or "").strip()


async def multimodal_locate_indices(
    image_png: bytes, query: str, user_id: str, db: Optional[AsyncSession] = None,
) -> list[int]:
    """locate-and-transcribe：返回相关卡片的 1-based 编号列表。"""
    b64 = base64.b64encode(image_png).decode("ascii")
    messages = [
        {"role": "system", "content": (
            "你是记忆检索助手。图片中包含编号 1..N 的记忆原文卡片。"
            "请找出与用户问题最相关的卡片编号（0 个或多个）。"
            '只输出 JSON：{"relevant_indices": [1, 3]}；若无相关，输出 {"relevant_indices": []}。'
        )},
        {"role": "user", "content": [
            {"type": "text", "text": f"用户问题：{query}"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]},
    ]
    raw = await _call_mm_llm_messages(messages, user_id, db)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```"))
    data = json.loads(raw)
    indices = data.get("relevant_indices") or []
    return [int(i) for i in indices if isinstance(i, (int, float)) or (isinstance(i, str) and i.isdigit())]


async def _load_recent_units(db: AsyncSession, user_id: str, top_k: int) -> list[dict]:
    result = await db.execute(
        text(
            "SELECT id, unit_kind, raw_text, created_at FROM subconscious_log"
            " WHERE user_id = :uid ORDER BY created_at DESC LIMIT :k"
        ),
        {"uid": user_id, "k": top_k},
    )
    rows = result.fetchall()
    units = [
        {"id": r[0], "unit_kind": r[1], "raw_text": r[2], "created_at": r[3]}
        for r in rows
    ]
    units.reverse()
    return units


async def fallback_cold_start_context(
    db: AsyncSession, user_id: str, conversation_messages: list[dict],
) -> Optional[str]:
    """冷启动多模态 fallback 入口。失败（渲染/LLM/禁用）→ None（调用方回退）。"""
    if _failure_state["disabled"]:
        return None

    cfg = _mm_cfg()
    top_k = int(cfg.get("snapshot_top_k", 10))
    set_of_mark = bool(cfg.get("set_of_mark", True))

    units = await _load_recent_units(db, user_id, top_k)
    if not units:
        return None

    query = ""
    for m in reversed(conversation_messages):
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        role = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
        if role == "user" and content:
            query = content
            break

    try:
        png = render_subconscious_snapshot_png(units, set_of_mark=set_of_mark)
    except Exception:
        logger.warning("multimodal snapshot render failed for user=%s", user_id, exc_info=True)
        return None

    try:
        indices = await multimodal_locate_indices(png, query, user_id, db)
    except Exception:
        _failure_state["consecutive"] += 1
        logger.warning(
            "multimodal locate failed for user=%s (consecutive=%d)",
            user_id, _failure_state["consecutive"], exc_info=True,
        )
        if _failure_state["consecutive"] >= 3:
            _failure_state["disabled"] = True
            logger.error(
                "ADMIN ALERT: multimodal cold-start fallback disabled after 3 consecutive failures"
                " (user=%s)；请确认是否切换回 [memory.multimodal].enabled=false", user_id,
            )
        return None

    _failure_state["consecutive"] = 0
    picked = [units[i - 1] for i in indices if 1 <= i <= len(units)]
    if not picked:
        return None

    try:
        from app.services.memory_cost_governance_service import record_llm_call
        await record_llm_call(db, user_id, "multimodal")
    except Exception:
        logger.debug("record_llm_call(multimodal) failed", exc_info=True)

    lines = ["[近期原文片段 Subconscious]"]
    for u in picked:
        created = u.get("created_at", "")
        if isinstance(created, datetime):
            created = created.strftime("%Y-%m-%d %H:%M")
        lines.append(f"- [{created}] {(u.get('raw_text') or '')[:200]} (未经整理)")
    return "\n".join(lines)
