# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Message payload endpoints + slim projection (P2-1 of
DESIGN_conv_open_performance.md).

- GET /api/messages/{message_id}/payload/{field} — on-demand full text of an
  externalized field (expand-to-load in the UI).
- slim_message_dict — drop fat inline fields to explicit stubs for the fast
  `?include=slim` conversation open path.

寻址契约（2026-10-05，上游TODO项2 P1）：payload 行只按 (message_id, field)
归属取回（message_payload_service._lookup_payload 同款）；sha256 仅作完整性
元数据，不作寻址——全局 sha 寻址会让伪造桩（本消息 stub 携带他消息
payload_ref）越权读到非本消息内容。孤儿桩（行缺失）一律 404，绝不把桩体
当内容回吐（前端 200=成功 → 静默停在预览，违背「截断必须可感知」红线）。
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.core.deps import get_current_user, get_db
from app.db.database import Message, User
from app.services.message_payload_service import (
    _inline_max_chars,
    _lookup_payload,
    build_field_stub,
    compute_sha256,
    is_externalized_stub,
)

router = APIRouter(prefix="/api/messages", tags=["message-payloads"])

_FIELDS = ("tool_results", "reasoning_content", "tool_calls")


def slim_message_dict(m):
    """Return a slim copy of a message dict: fat fields → stub shape.

    - Already-externalized stubs pass through (loose-shape `__truncated__`/
      `__size_bytes__` marker keys stripped — 评审 A Critical-1；payload_ref 等
      解析键原样保留).
    - tool_results / tool_calls: structure-preserving slim JSON (UI can render
      tool cards from titles/status; heavy bodies carry __truncated__).
    - reasoning_content: meta stub with preview.
    - Thin messages are returned unchanged (identity).
    - 出桩条件（D-11 修订，2026-10-06 懒加载回归）：**截断即出桩**——slim 形态
      发生过截断（任一字符串被切/预览被切）或超 preview 容量 → 出桩；否则
      原样返回（其全文即展示基线）。修复用户实测「全部都会加载」：≤32k 但
      内含长串的思考/工具字段曾整包随列表下发（旧条件只看 payload_inline_max_chars）。
    """
    if not isinstance(m, dict):
        return m
    out = dict(m)
    changed = False
    # W6 fix-round（A4.9 B-I7 逆转 D-13）：tool_calls 恢复出桩（数组形载荷以
    # {meta, items} 交付）——写侧 I5「不外置 tool_calls」不动（DB 数组恒定）；
    # 读侧消费端按 items 解包（stores/chat.ts）。防 MB 级 arguments 随列表满载。
    for field in ("tool_results", "reasoning_content", "tool_calls"):
        val = out.get(field)
        if not val or not isinstance(val, str):
            continue
        if is_externalized_stub(val):
            # 评审 A Critical-1（fix-round 1）：标记信任=严格 meta 形——宽松/伪造
            # 桩形（无 meta）剥除 __truncated__/__size_bytes__ 再放行（payload_ref
            # 等解析键原样保留，归属寻址语义不变）。
            from app.services.message_payload_service import stub_markers_trusted
            if not stub_markers_trusted(val):
                def _strip_mk(v):
                    if isinstance(v, list):
                        return [_strip_mk(x) for x in v]
                    if isinstance(v, dict):
                        return {k: _strip_mk(x) for k, x in v.items()
                                if k not in ("__truncated__", "__size_bytes__")}
                    return v
                try:
                    out[field] = json.dumps(_strip_mk(json.loads(val)), ensure_ascii=False)
                    changed = True  # 复审 round2 New-#1：剥除即变更（防 identity 快路径吞掉）
                except (ValueError, TypeError):
                    pass
            continue
        from app.services.message_payload_service import build_field_stub_ex, field_slim_cut
        # W6 A4 热路：轻探先行——thin（无剪裁且 ≤inline cap）直接原样，免建桩
        if not field_slim_cut(field, val) and len(val) <= _inline_max_chars():
            continue
        out[field] = build_field_stub_ex(field, val)[0]
        changed = True
    return out if changed else m


@router.get("/{message_id}/payload/{field}")
async def get_message_payload(
    message_id: str,
    field: str,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the full text of one message field (externalized or inline)."""
    if field not in _FIELDS:
        raise HTTPException(status_code=404, detail="Unknown field")
    result = await db.execute(
        select(Message).where(Message.id == message_id)
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    # ownership via conversation
    from app.db.database import Conversation
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == msg.conversation_id)
    )).scalar_one_or_none()
    if conv is None or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Message not found")

    stored = getattr(msg, field, None)
    if stored is None:
        raise HTTPException(status_code=404, detail="Field not present")

    externalized = is_externalized_stub(stored)
    content = stored
    if externalized:
        # 归属寻址（C2 同款）：只查本消息 (message_id, field) 的 payload 行。
        # 伪造桩（payload_ref 指向他消息 sha）查无本消息行 → 404；
        # 孤儿桩（行缺失）→ 404——桩体不得当内容回吐。
        row_content = _lookup_payload(db, message_id, field)
        if asyncio.iscoroutine(row_content):
            row_content = await row_content
        if row_content is None:
            raise HTTPException(status_code=404, detail="Payload not found")
        # 完整性校验（minor⑦）：sha256 元数据落地——行内容与桩 payload_ref
        # 不符（篡改/写坏）→ 404，绝不把校验失败的内容当全文回吐。
        ref = None
        try:
            ref = json.loads(stored).get("payload_ref")
        except (ValueError, TypeError):
            ref = None
        if ref and compute_sha256(row_content) != ref:
            raise HTTPException(status_code=404, detail="Payload integrity mismatch")
        content = row_content
    return {
        "message_id": message_id,
        "field": field,
        "content": content,
        # 与 build_field_stub 的 meta.size_bytes 同语义（UTF-8 字节数，minor④）
        "size_bytes": len((content or "").encode("utf-8")),
        "externalized": externalized,
    }
