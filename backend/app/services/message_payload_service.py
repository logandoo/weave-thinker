# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Message payload externalization: keep `messages` rows slim while preserving
full tool_results / reasoning_content / tool_calls fidelity.

Design: docs/DESIGN_conv_open_performance.md §2 P1-1. When a persisted field
exceeds [agent] payload_inline_max_chars, the FULL text goes to the
`message_payloads` table (content-addressed by sha256) and the inline column
holds an explicit stub (or structure-preserving slim JSON for tool_results /
tool_calls) carrying payload_ref + size + preview. Reads that need full
fidelity call resolve_field / resolve_message_fields.

The stub is JSON with a top-level "_externalized": true marker so consumers can
branch (Claude Code issue #64306: truncation must be unmistakable).
"""
import asyncio
import contextvars
import hashlib
import json
import math
import logging
from types import SimpleNamespace

logger = logging.getLogger(__name__)

# Per-string truncation inside structure-preserving slim JSON (tool_results /
# tool_calls keep their shape so the UI can still render tool cards).
INNER_TRUNCATE_CHARS = 1500
INNER_PREVIEW_CHARS = 500

DEFAULT_INLINE_MAX_CHARS = 32_768
DEFAULT_PREVIEW_CHARS = 2_000


def _inline_max_chars() -> int:
    try:
        from app.core.config import get_config
        agent = getattr(get_config(), "agent", None) or {}
        val = agent.get("payload_inline_max_chars") if isinstance(agent, dict) else getattr(agent, "payload_inline_max_chars", None)
        return int(val) if val else DEFAULT_INLINE_MAX_CHARS
    except Exception:
        return DEFAULT_INLINE_MAX_CHARS


def _preview_chars() -> int:
    try:
        from app.core.config import get_config
        agent = getattr(get_config(), "agent", None) or {}
        val = agent.get("payload_preview_chars") if isinstance(agent, dict) else getattr(agent, "payload_preview_chars", None)
        return int(val) if val else DEFAULT_PREVIEW_CHARS
    except Exception:
        return DEFAULT_PREVIEW_CHARS


def compute_sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# 徽标 N 的计数口径（双侧同构，A4.9 C2 fix 定稿）：只计**内容键**——
# 标签/元数据键（type/status/name/id/message_id/conversation_id）双侧同跳，
# 字符串同值去重（toToolPartItem 的 result=content 复制不双计）。
_ITEM_LABEL_KEYS = {"type", "step_type", "name", "status", "id",
                    "message_id", "conversation_id"}


def _item_text_total(obj, _seen=None) -> int:
    """项文本总量：Σlen(内容键子树字符串)，同值去重——徽标 N 双侧同构口径。"""
    if _seen is None:
        _seen = set()
    if isinstance(obj, str):
        if obj in _seen:
            return 0
        _seen.add(obj)
        # UTF-16 码元计数=JS .length（A4.9 fix-round：astral 字符双侧同构）
        return sum(2 if ord(c) > 0xFFFF else 1 for c in obj)
    if isinstance(obj, list):
        return sum(_item_text_total(v, _seen) for v in obj)
    if isinstance(obj, dict):
        return sum(_item_text_total(v, _seen) for k, v in obj.items()
                   if not str(k).startswith("__") and k not in _ITEM_LABEL_KEYS)
    return 0


def _slim_seq_item(item, state=None):
    """display_sequence 直系项瘦身：type=="text" 的 content（回答正文段）豁免，
    其余项与项内嵌套结构照常预览（复审 N3：豁免只及直系项，不粘滞进嵌套字典）。
    state：外层剪裁标记 {"cut","hidden"}（出桩条件用）。
    项内嵌套剪裁（数组串/深层字典）落**项级** `__truncated__` 标记；`__size_bytes__`
    = 整值 `_item_text_total(item)`（A4.9 C2 定稿口径，非累加）。"""
    if not isinstance(item, dict):
        return slim_tool_results_json(item, state)
    own = {"cut": False, "hidden": 0}
    out = {}
    is_body = item.get("type") == "text"
    for k, v in item.items():
        if k in ("__truncated__", "__size_bytes__"):
            continue  # 源键剥除（UPSTREAM_TODO_20261006 项 12）：标记仅由后端签发
        if isinstance(v, str) and len(v) > INNER_TRUNCATE_CHARS and not (is_body and k == "content"):
            own["cut"] = True
            own["hidden"] += len(v)
            out[k] = v[:INNER_PREVIEW_CHARS]
        else:
            out[k] = slim_tool_results_json(v, own)
    if own["cut"]:
        out["__truncated__"] = True
        # W6 fix-round（A4.9 C2）：__size_bytes__ = 项文本总量 Σlen(所有字符串)——
        # 与前端取回后的递归 Σ 同构（N 跨取回恒稳，多切/参数切不失真）；
        # 整值覆盖（非累加源键）→ 内容伪造的同名键不可抬/压徽标（A4.9 M5）。
        out["__size_bytes__"] = _item_text_total(item)  # 原始项（预览长度≠真长）
        if state is not None:
            state["cut"] = True
            state["hidden"] += own["hidden"]
    return out


def slim_tool_results_json(obj, state=None):
    """Recursively truncate long string values, keeping structure intact.

    Truncated strings keep INNER_PREVIEW_CHARS of the head and are annotated
    with sibling markers `__truncated__` (bool) and `__size_bytes__` (int,
    **整值** `_item_text_total(obj)`=Σ内容键——UPSTREAM_TODO_20261006 项 12 修
    累加源键伪造面，与 `_slim_seq_item`/前端 `_textTotal` 同构). 正文豁免（2026-10-03 conv 8c03ff8e）：
    display_sequence 直系 type=="text" 的 content + `content_segments` 整键原样。
    数组串照剪（W6）：list 元素长串截断，剪裁计入所在 dict 范围（同级标记）。
    state：{"cut": bool, "hidden": int} 剪裁标记贯穿（禁内容嗅探）。"""
    if state is None:
        state = {"cut": False, "hidden": 0}
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        out = []
        for v in obj:
            if isinstance(v, str) and len(v) > INNER_TRUNCATE_CHARS:
                state["cut"] = True
                state["hidden"] += len(v)
                out.append(v[:INNER_PREVIEW_CHARS])
            else:
                out.append(slim_tool_results_json(v, state))
        return out
    if not isinstance(obj, dict):
        return obj
    own = {"cut": False, "hidden": 0}
    out = {}
    for k, v in obj.items():
        if k in ("__truncated__", "__size_bytes__"):
            continue  # 源键剥除（UPSTREAM_TODO_20261006 项 12）：标记仅由后端签发
        if k == "display_sequence" and isinstance(v, list):
            out[k] = [_slim_seq_item(i, state) for i in v]
        elif k == "content_segments":
            out[k] = v  # 正文段数组豁免（正文红线）
        elif isinstance(v, str) and len(v) > INNER_TRUNCATE_CHARS:
            own["cut"] = True
            own["hidden"] += len(v)
            out[k] = v[:INNER_PREVIEW_CHARS]
        else:
            out[k] = slim_tool_results_json(v, own)
    if own["cut"]:
        out["__truncated__"] = True
        # UPSTREAM_TODO_20261006 项 12（A4.9 M5 整值覆盖契约）：__size_bytes__ =
        # _item_text_total(obj)（Σ内容键·同值去重·跳标签键）——非累加源键，
        # 内容自带同名键不可抬/压徽标；与 _slim_seq_item 同口径。
        out["__size_bytes__"] = _item_text_total(obj)
        state["cut"] = True
        state["hidden"] += own["hidden"]
    return out


def build_field_stub_ex(field: str, value: str, preview_chars: int | None = None):
    """build_field_stub 的出桩判定版（A4.9 二波 I1）：返回 (stub, cut)。
    cut=展示基线之外真的发生了截断（slim 剪裁标记，非内容嗅探）——
    slim_message_dict 以它决定是否出桩（截断即出桩，D-11 修订）。"""
    pv = preview_chars if preview_chars is not None else _preview_chars()
    sha = compute_sha256(value)
    state = {"cut": False, "hidden": 0}
    meta = {"_externalized": True, "field": field, "payload_ref": sha,
            "size_bytes": len(value.encode("utf-8")), "size_chars": len(value)}
    if field in ("tool_results", "tool_calls"):
        try:
            obj = json.loads(value)
            slimmed = slim_tool_results_json(obj, state)
        except (ValueError, TypeError):
            slimmed = None
        if isinstance(slimmed, (dict, list)):
            if isinstance(slimmed, dict):
                slimmed = {**slimmed, **meta}
            else:
                slimmed = {**meta, "items": slimmed}
            return json.dumps(slimmed, ensure_ascii=False), state["cut"]
    if len(value) > pv:
        state["cut"] = True
    meta["preview"] = value[:pv]
    return json.dumps(meta, ensure_ascii=False), state["cut"]


def build_field_stub(field: str, value: str, preview_chars: int | None = None) -> str:
    """Build the inline replacement for an oversized field.

    - tool_results / tool_calls: structure-preserving slim JSON + `_externalized`
      metadata (payload_ref / size_bytes / field) so existing JSON.parse
      consumers keep working and truncated items are detectable.
    - reasoning_content (plain text): compact JSON stub with preview.
    """
    return build_field_stub_ex(field, value, preview_chars)[0]


def is_externalized_stub(s) -> bool:
    """外置桩识别（解析/取回触发用）——宽松形：`_externalized: true` 的 JSON 对象。
    解析路径本身安全（payload 按 (message_id, field) 归属寻址 + sha 完整性，伪造
    ref=404），故不做 meta 形校验（既有路由契约钉 test_message_payload_route_scoping）。"""
    if not s or not isinstance(s, str):
        return False
    stripped = s.lstrip()
    if not stripped.startswith("{"):
        return False
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and obj.get("_externalized") is True


def stub_markers_trusted(s) -> bool:
    """标记信任（评审 A Critical-1，fix-round 1）：徽标/愈合校验只信**后端签发**
    的桩标记——桩必须带 meta（`field` + 64hex `payload_ref` + 数值 `size_bytes`，
    与 build_field_stub_ex 同构）。内容伪造 `_externalized`+标记（无 meta/坏 meta）
    不可信：剥除其 `__truncated__`/`__size_bytes__` 后放行（宽松桩=解析语义不变）。"""
    if not is_externalized_stub(s):
        return False
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return False
    ref = obj.get("payload_ref")
    size = obj.get("size_bytes")
    return (isinstance(obj.get("field"), str) and bool(obj.get("field"))
            and isinstance(ref, str) and len(ref) == 64
            and all(c in "0123456789abcdef" for c in ref)
            and isinstance(size, (int, float)) and not isinstance(size, bool)
            # 幅值闸先行（复审 B Important）：巨整数 abs() 无损比较、isfinite 前拒
            # ——NaN/±Inf/巨整数同前端 Number.isFinite(JSON.parse→Infinity) 口径
            and abs(size) < 2 ** 1023 and math.isfinite(size))


def should_externalize(value: str | None, max_chars: int | None = None) -> bool:
    if not value:
        return False
    limit = max_chars if max_chars is not None else _inline_max_chars()
    return len(value) > limit


# ─── write-path hook (Task 2) ─────────────────────────────────────────────

# 评审 I5/voice 修复：tool_calls 不外置（voice/回放按结构解析 call id，桩会打断）。
_EXTERNALIZED_FIELDS = ("tool_results", "reasoning_content")
_RESOLVE_FIELDS = ("tool_results", "reasoning_content", "tool_calls")  # 读侧仍兼容历史桩
_hook_installed = False
# resolve 期间挂起 hook：否则 lookup 的 autoflush 会把刚还原的肥字段再次换回 stub。
# 评审 M2 修复：ContextVar（任务隔离），非进程级全局标志。
_hook_suspended_ctx: "contextvars.ContextVar[bool]" = contextvars.ContextVar("mp_hook_suspended", default=False)


class _HookSuspension:
    def __enter__(self):
        self._token = _hook_suspended_ctx.set(True)

    def __exit__(self, *exc):
        _hook_suspended_ctx.reset(self._token)
        return False


def uninstall_message_payload_hook():
    """Remove the hook (test hygiene — prevents cross-file flush contamination)."""
    global _hook_installed, _hook_handler
    if not _hook_installed:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _Sess
    try:
        event.remove(_Sess, "before_flush", _hook_handler)
    except Exception:
        pass
    _hook_installed = False
    _hook_handler = None


_hook_handler = None


def install_message_payload_hook():
    """ORM before_flush hook: externalize oversized Message fields.

    All Message() write sites (chat.py / agent_worker / agent_scheduler /
    voice_service / conversation import) are covered without touching any of
    them. Same-flush: the MessagePayload row commits atomically with the
    message. Idempotent — installing twice is a no-op.
    """
    global _hook_installed
    if _hook_installed:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session as _Sess

    from app.db.database import Message, MessagePayload

    def _before_flush(session, flush_context, instances):
        if _hook_suspended_ctx.get():
            return
        targets = list(session.new) + [s for s in session.dirty if session.is_modified(s)]
        for obj in targets:
            if not isinstance(obj, Message):
                continue
            if not obj.id:
                import uuid as _uuid
                obj.id = str(_uuid.uuid4())
            for field in _EXTERNALIZED_FIELDS:
                val = getattr(obj, field, None)
                if not val or is_externalized_stub(val) or not should_externalize(val):
                    continue
                sha = compute_sha256(val)
                # per-(message_id, field) 归属（评审 C1）：行随本消息 CASCADE，
                # 绝不跨消息共享——旧 sha 全局去重在删除时会毁掉他消息的共享行。
                existing = None
                for p in session.new:
                    if isinstance(p, MessagePayload) and p.message_id == obj.id and p.field == field:
                        existing = p
                        break
                if existing is None:
                    try:
                        existing = session.query(MessagePayload).filter_by(
                            message_id=obj.id, field=field).first()
                    except Exception:
                        existing = None
                if existing is None:
                    session.add(MessagePayload(
                        message_id=obj.id,
                        field=field,
                        content=val,
                        size_bytes=len(val.encode("utf-8")),
                        sha256=sha,
                    ))
                else:  # 更新既有行（幂等重写）
                    existing.content = val
                    existing.size_bytes = len(val.encode("utf-8"))
                    existing.sha256 = sha
                setattr(obj, field, build_field_stub(field, val))

    event.listen(_Sess, "before_flush", _before_flush)
    global _hook_handler
    _hook_handler = _before_flush
    _hook_installed = True


# ─── read-path resolve (Task 2) ───────────────────────────────────────────

def _is_async_session(db) -> bool:
    return hasattr(db, "execute") and asyncio.iscoroutinefunction(getattr(db, "execute", None))


def _lookup_payload(db, message_id: str, field: str):
    """Return this message's payload content (sync value or coroutine).

    评审 C2：按 (message_id, field) 归属查询——伪造桩（他消息的 payload_ref）
    查无本消息行 → 返回 None → 调用方原样返回 stored，杜绝跨消息内容替换。
    """
    from sqlalchemy import select as _select

    from app.db.database import MessagePayload

    stmt = (
        _select(MessagePayload.content)
        .where(MessagePayload.message_id == message_id, MessagePayload.field == field)
    )
    if _is_async_session(db):
        async def _run():
            result = await db.execute(stmt)
            row = result.first()
            return row[0] if row else None
        return _run()
    row = db.execute(stmt).first()
    return row[0] if row else None


async def resolve_field(message_id: str, field: str, stored: str | None, db) -> str | None:
    """Restore the full text of an externalized field (stub → payload table).

    Non-stub values pass through unchanged (byte-identical). Stub whose payload
    row is missing (forged / orphaned) also passes through — never substitutes
    foreign content.
    """
    if not stored or not is_externalized_stub(stored):
        return stored
    content = _lookup_payload(db, message_id, field)
    if asyncio.iscoroutine(content):
        content = await content
    return content if content is not None else stored


async def resolve_message_fields(messages, db) -> list:
    """Resolve externalized fields on ORM Message rows (returns new detached copies).

    评审 R1 Important：不再 setattr 会话内实例（脏化 session + 多 MB 状态）——
    返回字段已还原的轻量拷贝。
    """
    if not messages:
        return messages
    out = []
    with _HookSuspension():
        for m in messages:
            values = {}
            changed = False
            for field in _RESOLVE_FIELDS:
                val = getattr(m, field, None)
                if not val or not is_externalized_stub(val):
                    continue
                content = _lookup_payload(db, m.id, field)
                if asyncio.iscoroutine(content):
                    content = await content
                if content is not None:
                    values[field] = content
                    changed = True
            if not changed:
                out.append(m)
            else:
                copy = SimpleNamespace(**{
                    c.name: values.get(c.name, getattr(m, c.name, None))
                    for c in m.__table__.columns
                })
                out.append(copy)
    return out


async def resolve_dict_fields(rows: list, db) -> list:
    """Resolve externalized fields on dict rows (raw-SQL mappings) — returns new dicts."""
    if not rows:
        return rows
    out = []
    with _HookSuspension():
        for r in rows:
            d = dict(r)
            for field in _RESOLVE_FIELDS:
                val = d.get(field)
                if not val or not is_externalized_stub(val):
                    continue
                content = _lookup_payload(db, d.get("id"), field)
                if asyncio.iscoroutine(content):
                    content = await content
                if content is not None:
                    d[field] = content
            out.append(d)
    return out


def field_slim_cut(field: str, value: str) -> bool:
    """W6 热路轻探（A4）：只做 parse+walk 置剪裁标记，无 sha/dumps——thin 字段
    免建桩开销（conv-open 快路径）；返回展示基线之外是否真发生剪裁。"""
    if field == "reasoning_content":
        return len(value) > _preview_chars()
    try:
        obj = json.loads(value)
    except (ValueError, TypeError):
        return len(value) > _preview_chars()
    state = {"cut": False, "hidden": 0}
    slim_tool_results_json(obj, state)
    return state["cut"]
