# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W1-2 约束固定（记忆/长程 v2，2026-10-23，PLAN W1-2 / B1）。

问题（ConstraintRot，论文 2606.22528）：压缩会丢治理约束，违规率 0%→30%；
约束固定后回 0%。但"约束固定本身是新的注入面"（B1）：网页/工具输出里的
"永远不要读 X" 若被 pin 成持久后门 = 灾难。

信任门（全部 fail-closed，缺一不 pin）：
1. source_role 必须是 user（工具/助手/伪造 `[USER]:` 标签内容 0 pin）；
2. provenance 必须 ≥ trusted（taint_service.TAINT_RANK）；
3. pin 前过 memory_security.scan_injection；
4. 每会话上限 [memory.constraints] max_pins（默认 20）；
5. mode=off 不 pin；shadow 只记录不重放；enforce 记录 + 每轮 system 段重放。

抽取是**规则**（精确优先）：句首标记（记住/请记住/从现在起/今后/以后/固定约束/
always/never/from now on）才抽取；含角色标签的文本整体跳过（W1-1 洗白面的
兄弟通道）。LLM 兜底默认关（[memory.constraints] llm_fallback_enabled=false）。

UI 可见可撤销（P5）：GET /api/memory/constraints · POST .../revoke。
按 user_id 擦除（W5-1 擦除面含 pinned constraints）。
"""
import logging
import re
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from app.services.memory_security import scan_injection

logger = logging.getLogger(__name__)

try:
    from app.core.config import get_config

    config = get_config()
except Exception:  # pragma: no cover - 配置不可用时门内 fail-closed 路径兜底
    config = None
_MAX_TEXT = 500

# 伪造/真实角色标签（内容来源出现即跳过抽取——与 W1-1 `_escape_role_labels`
# 同一族变体：空格/全角/【】/零宽）。
_ROLE_LABEL_RE = re.compile(
    r"[\[［【]\s*(?:USER|ASSISTANT|SYSTEM|AI|HUMAN|TOOL(?:\s*RESULT)?)"
    r"\s*[\]］】]\s*[:：]",
    re.IGNORECASE,
)

_SENT_SPLIT_RE = re.compile(r"[。！？!?\n]+")

# 句首标记 → 约束子句（精确优先：无标记不抽取）。
_MARKER_RES: Tuple[re.Pattern, ...] = (
    re.compile(
        r"^(?:请记住|记住|固定约束|约束|规矩)\s*[:：]?\s*(.{4,200})$"),
    re.compile(
        r"^(?:从现在起|今后|以后|往后|从今以后)(?:都|一直|每次)?\s*"
        r"[:：,，]?\s*(.{4,200})$"),
    re.compile(
        r"^(?:每次|每回|今后每次)(?:都|要)?\s*[:：,，]?\s*(.{4,200})$"),
    re.compile(
        r"^(?:always|never|from now on|going forward|remember that|"
        r"don'?t ever|do not ever)\b[:,，]?\s*(.{4,200})$",
        re.IGNORECASE),
)

_TRAIL_PUNCT_RE = re.compile(r"[。．.、，,;；:：!！?？\s]+$")


def extract_user_constraints(text: str) -> List[str]:
    """规则抽取用户约束（句首标记才抽取；含角色标签的文本整体跳过）。"""
    raw = str(text or "")
    if not raw.strip():
        return []
    if _ROLE_LABEL_RE.search(raw):
        # 伪造 `[USER]:` 内容 0 pin：整条消息不抽（宁漏勿误，P1 来源先于召回）
        logger.info("constraint extraction skipped: role label in user text")
        return []
    out: List[str] = []
    for sentence in _SENT_SPLIT_RE.split(raw):
        s = sentence.strip()
        if not s:
            continue
        for pat in _MARKER_RES:
            m = pat.match(s)
            if not m:
                continue
            clause = _TRAIL_PUNCT_RE.sub("", m.group(1).strip())
            if len(clause) >= 4 and not _ROLE_LABEL_RE.search(clause):
                out.append(clause[:_MAX_TEXT])
            break
    return out


def decide_pin(
    *,
    text: str,
    source_role: str,
    provenance: str,
    existing_count: int,
    max_pins: int = 20,
    mode: str = "enforce",
) -> Tuple[bool, str, str]:
    """纯门控决策（fail-closed）：(ok, reason, normalized_text)。"""
    if str(mode).strip().lower() == "off":
        return False, "mode=off", ""
    if str(source_role).strip().lower() != "user":
        return False, f"role not user: {source_role}", ""
    try:
        from app.services.taint_service import TAINT_RANK, normalise_taint
        if TAINT_RANK.get(normalise_taint(provenance), 0) < TAINT_RANK.get("trusted", 0):
            return False, f"provenance not >= trusted: {provenance}", ""
    except Exception:
        # taint 服务不可用时 fail-closed（P4：动作闸门失败即拒绝）
        return False, "provenance check unavailable (fail-closed)", ""
    norm = str(text or "").strip()
    if not norm:
        return False, "empty text", ""
    if _ROLE_LABEL_RE.search(norm):
        return False, "role label in text", ""
    if len(norm) > _MAX_TEXT:
        norm = norm[:_MAX_TEXT]
    hit = scan_injection(norm)
    if hit:
        return False, f"scan: {hit}", ""
    if int(existing_count) >= int(max_pins):
        return False, f"cap reached ({existing_count}>={max_pins})", ""
    return True, "ok", norm


def format_pinned_block(texts: Sequence[str]) -> str:
    """system 段重放块（内容角色标签转义，防伪造标签经约束面洗白）。"""
    from app.services.context_compressor import _escape_role_labels

    lines = ["【固定约束（用户指定，经信任门；可于记忆面板撤销）】"]
    for t in texts:
        lines.append(f"- {_escape_role_labels(str(t))}")
    return "\n".join(lines)


def build_replay_message(texts: Sequence[str]) -> Optional[dict]:
    texts = [str(t) for t in texts if str(t or "").strip()]
    if not texts:
        return None
    return {
        "role": "system",
        "content": format_pinned_block(texts),
        "_ephemeral": "pinned_constraints",
    }


# ---------------- 持久化（ORM；调用点 fail-open，门内 fail-closed） ----------------

async def pin_user_constraint(
    db: Any,
    user_id: str,
    conversation_id: str,
    text: str,
    *,
    source_message_id: Optional[str] = None,
    source_role: str = "user",
    provenance: str = "trusted",
    scope: str = "conversation",
    mode: Optional[str] = None,
    max_pins: Optional[int] = None,
) -> Optional[Any]:
    """过门则落 conversation_pinned_constraints；拒绝返回 None（不留行）。"""
    from sqlalchemy import func, select

    from app.db.database import ConversationPinnedConstraint

    cfg = config
    mode = mode if mode is not None else (
        cfg.memory_constraints_mode if cfg is not None else "enforce")
    max_pins = max_pins if max_pins is not None else (
        cfg.memory_constraints_max_pins if cfg is not None else 20)

    existing = await db.scalar(
        select(func.count()).select_from(ConversationPinnedConstraint).where(
            ConversationPinnedConstraint.conversation_id == conversation_id,
            ConversationPinnedConstraint.user_id == user_id,
            ConversationPinnedConstraint.revoked_at.is_(None),
        )) or 0

    ok, reason, norm = decide_pin(
        text=text, source_role=source_role, provenance=provenance,
        existing_count=int(existing), max_pins=int(max_pins), mode=mode)
    if not ok:
        logger.info("pin rejected (conv=%s): %s", conversation_id, reason)
        return None

    # 幂等：同文本活跃约束不重复 pin
    dup = await db.scalar(
        select(ConversationPinnedConstraint).where(
            ConversationPinnedConstraint.conversation_id == conversation_id,
            ConversationPinnedConstraint.user_id == user_id,
            ConversationPinnedConstraint.revoked_at.is_(None),
            ConversationPinnedConstraint.text == norm,
        ).limit(1))
    if dup is not None:
        return dup

    row = ConversationPinnedConstraint(
        user_id=user_id, conversation_id=conversation_id, text=norm,
        source_message_id=source_message_id, source_role=source_role,
        provenance=provenance, scope=scope, pinned_by="user")
    db.add(row)
    await db.flush()
    logger.info("pin accepted (conv=%s): %s...", conversation_id, norm[:40])
    return row


async def list_active_pins(db: Any, user_id: str, conversation_id: str) -> List[Any]:
    from sqlalchemy import select

    from app.db.database import ConversationPinnedConstraint

    res = await db.execute(
        select(ConversationPinnedConstraint).where(
            ConversationPinnedConstraint.conversation_id == conversation_id,
            ConversationPinnedConstraint.user_id == user_id,
            ConversationPinnedConstraint.revoked_at.is_(None),
        ).order_by(ConversationPinnedConstraint.created_at))
    return list(res.scalars().all())


async def list_all_pins(db: Any, user_id: str, conversation_id: Optional[str] = None) -> List[Any]:
    """UI 面板：含已撤销（可审计）。"""
    from sqlalchemy import select

    from app.db.database import ConversationPinnedConstraint

    stmt = select(ConversationPinnedConstraint).where(
        ConversationPinnedConstraint.user_id == user_id)
    if conversation_id:
        stmt = stmt.where(ConversationPinnedConstraint.conversation_id == conversation_id)
    res = await db.execute(stmt.order_by(ConversationPinnedConstraint.created_at.desc()))
    return list(res.scalars().all())


async def revoke_pin(db: Any, user_id: str, pin_id: str) -> bool:
    from sqlalchemy import select

    from app.db.database import ConversationPinnedConstraint

    row = await db.scalar(
        select(ConversationPinnedConstraint).where(
            ConversationPinnedConstraint.id == pin_id,
            ConversationPinnedConstraint.user_id == user_id))
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.utcnow()
    row.revoked_by = "user"
    await db.flush()
    return True


async def pin_constraints_from_user_turn(
    db_factory: Any,
    user_id: str,
    conversation_id: str,
    user_text: str,
) -> int:
    """用户轮规则抽取 → pin（调用点 fail-open；门内 fail-closed）。返回 pin 数。"""
    if config is None or config.memory_constraints_mode == "off":
        return 0
    candidates = extract_user_constraints(user_text)
    if not candidates:
        return 0
    # 评审 A I3/B I4：provenance 取自会话 taint ledger（非硬编码 trusted）——
    # untrusted 污染会话的用户文本不得经约束面升格为 trusted 并 system 重放。
    provenance = "trusted"
    try:
        from app.services import taint_service
        async with db_factory() as db:
            ledger = await taint_service.load_ledger(db, str(conversation_id),
                                                     str(user_id))
        if ledger is not None and getattr(ledger, "loaded", False):
            provenance = taint_service.max_taint(ledger.max_taint, "trusted")
        elif ledger is not None:
            provenance = "untrusted"
    except Exception:
        provenance = "untrusted"  # 门内 fail-closed：无法证明来源即不 pin
    pinned = 0
    async with db_factory() as db:
        for c in candidates:
            row = await pin_user_constraint(
                db, user_id, conversation_id, c, provenance=provenance)
            if row is not None and getattr(row, "created_at", None) is not None:
                pinned += 1
        await db.commit()
    return pinned


async def build_replay_message_for(
    db_factory: Any,
    user_id: str,
    conversation_id: str,
) -> Optional[dict]:
    """每轮 system 段重放（仅 mode=enforce；shadow/off 返回 None）。"""
    if config is None or config.memory_constraints_mode != "enforce":
        return None
    async with db_factory() as db:
        rows = await list_active_pins(db, user_id, conversation_id)
        texts = [r.text for r in rows]
    return build_replay_message(texts)
