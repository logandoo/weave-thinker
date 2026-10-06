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


def _slim_seq_item(item):
    """display_sequence 直系项瘦身：type=="text" 的 content（回答正文段）豁免，
    其余项与项内嵌套结构照常预览（复审 N3：豁免只及直系项，不粘滞进嵌套字典）。"""
    if not isinstance(item, dict):
        return slim_tool_results_json(item)
    out = {}
    is_body = item.get("type") == "text"
    for k, v in item.items():
        if isinstance(v, str) and len(v) > INNER_TRUNCATE_CHARS and not (is_body and k == "content"):
            out[k] = v[:INNER_PREVIEW_CHARS]
            out["__truncated__"] = True
            out["__size_bytes__"] = len(v)
        else:
            out[k] = slim_tool_results_json(v)
    return out


def slim_tool_results_json(obj):
    """Recursively truncate long string values, keeping structure intact.

    Truncated strings keep INNER_PREVIEW_CHARS of the head and are annotated
    with sibling markers `__truncated__` (bool) and `__size_bytes__` (int,
    码位计数——命名历史遗留，取值为 len(v) 即字符数).

    正文豁免（2026-10-03 断层修复，红线：回答正文永不静默截断）：仅
    ``display_sequence`` **直系项**中 ``type == "text"`` 的 ``content``
    原样通过——正文 KB 级且 messages.content 本就全量内联，预览收益为零、
    截断代价是用户可见的回答残缺（conv 8c03ff8e 事故）。树内其他 type:"text"
    字典与工具/思考 content 维持预览（性能不回吐；折叠卡后有展开取全文链路）。
    """
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return [slim_tool_results_json(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if k == "display_sequence" and isinstance(v, list):
            out[k] = [_slim_seq_item(i) for i in v]
        elif isinstance(v, str) and len(v) > INNER_TRUNCATE_CHARS:
            out[k] = v[:INNER_PREVIEW_CHARS]
            out["__truncated__"] = True
            out["__size_bytes__"] = len(v)
        else:
            out[k] = slim_tool_results_json(v)
    return out


def build_field_stub(field: str, value: str, preview_chars: int | None = None) -> str:
    """Build the inline replacement for an oversized field.

    - tool_results / tool_calls: structure-preserving slim JSON + `_externalized`
      metadata (payload_ref / size_bytes / field) so existing JSON.parse
      consumers keep working and truncated items are detectable.
    - reasoning_content (plain text): compact JSON stub with preview.
    """
    pv = preview_chars if preview_chars is not None else _preview_chars()
    sha = compute_sha256(value)
    meta = {"_externalized": True, "field": field, "payload_ref": sha, "size_bytes": len(value.encode("utf-8"))}
    if field in ("tool_results", "tool_calls"):
        try:
            obj = json.loads(value)
            slimmed = slim_tool_results_json(obj)
        except (ValueError, TypeError):
            slimmed = None
        if isinstance(slimmed, (dict, list)):
            if isinstance(slimmed, dict):
                slimmed = {**slimmed, **meta}
            else:
                slimmed = {**meta, "items": slimmed}
            return json.dumps(slimmed, ensure_ascii=False)
    meta["preview"] = value[:pv]
    return json.dumps(meta, ensure_ascii=False)


def is_externalized_stub(s) -> bool:
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
