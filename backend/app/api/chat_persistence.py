# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""Chat persistence helpers (P5.3 move-only, 2026-10-18)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete, func, select

from app.db.database import AsyncSessionLocal, Conversation, Message

logger = logging.getLogger(__name__)


def interjection_items_from_buffer(buf) -> str | None:
    """Reconstruct the inline sidecar from the stream buffer's commit echoes.

    Terminal paths that only have a buffer (legacy resume / post-disconnect /
    interrupted self-save) still need the sidecar when they insert a new row;
    the buffer's ``interjections_committed[*].display_item`` is the authoritative
    position record. Dedupes by message_id and preserves commit order.
    """
    try:
        items = []
        seen = set()
        for echo in getattr(buf, "interjections_committed", None) or []:
            if not isinstance(echo, dict):
                continue
            item = echo.get("display_item")
            if not isinstance(item, dict):
                continue
            mid = str(item.get("message_id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            items.append({
                "message_id": mid,
                "content": str(item.get("content") or ""),
                "created_at": item.get("created_at"),
                "after_index": item.get("after_index"),
                "after_char": item.get("after_char"),
            })
        return json.dumps(items, ensure_ascii=False) if items else None
    except Exception:
        logger.exception("interjection sidecar extraction failed (no inline items)")
        return None


def interjection_anchor_from_buffer(buf) -> datetime | None:
    """本 run 已 commit 插话的最大 created_at（stream buffer 回执是权威来源）。

    A4.9 I2：不能用「全会话 user 消息 MAX」——延迟终态（断连后后台补存）会把
    本轮回答排到下一轮的提问之后。buffer 缺失/无插话 → None（调用方保持原
    created_at，绝不猜测）。
    """
    try:
        stamps = []
        for item in getattr(buf, "interjections_committed", None) or []:
            raw = item.get("created_at") if isinstance(item, dict) else None
            if not raw:
                continue
            try:
                stamp = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            if stamp.tzinfo is not None:
                # Message.created_at 是 naive UTC（database.py default=utcnow）；
                # 转 UTC 再去 tz，避免本地时区造成偏移（A4.9 NB-2）。
                from datetime import timezone as _tz
                stamp = stamp.astimezone(_tz.utc).replace(tzinfo=None)
            stamps.append(stamp)
        return max(stamps) if stamps else None
    except Exception:
        logger.exception("interjection anchor extraction failed (no reorder)")
        return None


async def _save_assistant_terminal(
    db,
    *,
    conversation_id: str,
    content: str,
    reasoning: str | None,
    tool_results: str | None,
    tool_calls: str | None,
    context_info,
    status: str = "final",
    row_id: str | None = None,
    interjection_anchor: datetime | None = None,
    interjection_items: str | None = None,
) -> Message:
    """终态落库（2026-10-06 渐进落库）：本会话若存在 delivery_status='streaming'
    的在途行则 update-in-place（翻 status），否则插入新行（旧行为兜底——
    死磕按轮落库等未建 streaming 行的路径）。

    row_id（2026-10-31 UPSTREAM_TODO_20261031 P1）：stop 路径传入**本 run** 的
    streaming 行 id——按 id 精确绑定，防止「最新 streaming 行」反查命中别的 run
    的僵尸行（错翻他人行 + 本 run 行漏翻）。row_id 给定但未命中 streaming 行时
    插入新行（与 row=None 且无在途行时的兜底一致，不回头横扫）。

    返回落库行（调用方负责 commit/refresh，与既有路径一致）。
    """
    if row_id:
        row = (await db.execute(
            select(Message)
            .where(Message.id == row_id,
                   Message.delivery_status == "streaming")
        )).scalars().first()
    else:
        row = (await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id,
                   Message.delivery_status == "streaming")
            .order_by(Message.created_at.desc())
            .limit(1)
        )).scalars().first()
    if row is None:
        row = Message(
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            reasoning_content=reasoning or None,
            tool_results=tool_results,
            tool_calls=tool_calls,
            context_info=context_info,
            delivery_status=status,
            interjection_items=interjection_items,
        )
        db.add(row)
        return row
    # 2026-11-01 插话排序（conv a4d54ec7）——必须在改行字段**之前**算好时间戳：
    # autoflush=True 时，行一旦脏化，下面任何 SELECT 都会先把旧 created_at
    # 刷成一次 update 事件（sync 侧 append-only，后一次将被 SKIPPED，跨设备
    # 永远停在错误顺序）。anchor 由调用方从本 run 的 buffer 插话回执取得
    # （A4.9 I2：不能用全会话 MAX——延迟终态会把本轮回答排到下一轮提问之后）。
    try:
        if (interjection_anchor is not None and row.created_at is not None
                and interjection_anchor > row.created_at):
            row.created_at = interjection_anchor + timedelta(milliseconds=1)
    except TypeError:
        # 类型异常（aware/naive 混用）绝不能中断终态落库——排序修复是尽力而为。
        logger.exception(
            "Interjection anchor comparison failed for conversation %s "
            "(row kept at original timestamp)", conversation_id,
        )
    row.content = content
    row.reasoning_content = reasoning or None
    row.tool_results = tool_results
    # 2026-11-02（C4）：tool_calls=None 表示「本次调用没带工具调用数据」，
    # 不是「清空」。W11 的渐进落库会把 tool_call_events_accumulated 写进在途
    # 行；stop 分支与 legacy-resume 不持有该数据（传 None），无条件赋值会把
    # 它抹掉，随后 tool_history.rebuild_structured_history 因 tool_calls 为空
    # 丢弃已落库的 tool_results —— 与「断点续接」契约相反。
    if tool_calls is not None:
        row.tool_calls = tool_calls
    if context_info is not None:
        row.context_info = context_info
    if interjection_items is not None:
        row.interjection_items = interjection_items
    row.delivery_status = status
    return row

def _has_turn_evidence(row, *, include_reasoning: bool = True) -> bool:
    """在途行是否留下任何证据（正文/思考/工具调用/工具结果）。

    2026-11-01（conv 861cb5af）：只有工具调用或思考、尚未产出正文的长任务
    （视频生成等）被杀后同样算证据——旧判据只看 content，把它当空壳删掉，
    用户看到「提问下面什么都没有」（无回答、无中断标记、无任何痕迹）。
    空 JSON 数组/None 不算证据。

    2026-11-02（C5）：这是「空壳」判据的**唯一来源**。`include_reasoning` 是
    两条路径的**显式**分歧点，不再是两套隐式判据：
    - `True`（默认）：崩溃 sweep 用——非预期死亡时 reasoning 也是证据，保留为
      `interrupted` 让用户看到「该问题已被部分回答」。
    - `False`：主动 stop / 空终态清理用——reasoning-only 不落 final（W5 裁定
      `D-20261031-msgdel-p1-semantics`，conv d7ea65b8 空气泡根因），但**必须**
      计入 tool_calls/tool_results，否则工具证据在 stop 时蒸发。
    """
    def _non_empty(value) -> bool:
        text = str(value or "").strip()
        return bool(text) and text not in ("[]", "{}", "null", "None")

    return (
        bool((row.content or "").strip())
        or (include_reasoning and bool((row.reasoning_content or "").strip()))
        or bool(_non_empty(row.tool_calls))
        or bool(_non_empty(row.tool_results))
    )


async def mark_interrupted_streams(db) -> int:
    """启动 sweep（B）：进程死亡遗留的 streaming 行——
    有证据（正文/思考/工具调用/工具结果）的翻 interrupted（徽标展示，可展开
    查看已完成的工具调用）；只有完全空壳（连工具调用都没有——杀在首 token 前）
    才直接删除（空 bubble 无信息，展示即噪音）。
    返回翻 interrupted 的行数。

    部署约束（A4.9 r1 I1）：本 sweep 无实例归属——**同一数据库只支持单实例
    运行本服务**（多实例共库时 A 实例重启会把 B 实例的在途行翻 interrupted）。
    同库多副本（如 weave_blah 旧版）不建 streaming 行，不受影响。

    W4 项5（2026-10-06）：翻转/删除必须走 ORM 实例路径而非 Core bulk DML——
    bulk update()/delete() 不触发 mapper 事件（仅 flush 逐对象触发），sync_capture
    捕获不到 streaming→interrupted 的终态翻转，残稿跨设备不可见直至全量 resync，
    与 sync_capture「翻 terminal 后由当次提交正常发射」承诺相悖。ORM 翻转经
    after_update 发射（_emit 将 update coerce 为 create）；空壳 ORM 删除的
    after_delete 被 _emit 的 streaming 守卫抑制（该行 create 从未发射，delete
    也不该发射/留墓碑）。
    """
    rows = (await db.execute(
        select(Message)
        .where(Message.delivery_status == "streaming")
    )).scalars().all()
    flipped = 0
    for row in rows:
        # P1（2026-10-31）：whitespace-only 正文视同空正文；2026-11-01：工具/思考
        # 证据优先（有证据就保留为 interrupted，绝不静默删除整轮）。
        if _has_turn_evidence(row):
            row.delivery_status = "interrupted"
            flipped += 1
        else:
            await db.delete(row)
    return flipped

async def _delete_empty_streaming_row(db, row_id: str | None) -> bool:
    """stop-cancel 零内容收尾（2026-10-17，conv 92398a80 僵尸行 55d66144）：
    删除本 run 的空 streaming 行。三守卫——行 id 精确 + delivery_status=
    'streaming' + **无任何证据**：绝不误删其他 run 的行或已落内容行。

    2026-11-02（C5）：判据从「content 为空」改为与启动 sweep **同源**的
    `_has_turn_evidence(..., include_reasoning=False)`。旧判据只看 content，
    会把「已 flush 工具调用、正文尚未产出」的行当空壳删掉——同一行若遇到进程
    死亡反而被 sweep 保留为 interrupted，两条路径对同形状行给出相反处置。
    reasoning 不参与（W5 裁定 reasoning-only 不落 final，见 D-20261031-msgdel-p1-semantics），
    但 tool_calls/tool_results 必须参与，否则工具证据在 stop 时蒸发。
    """
    if not row_id:
        return False
    # 2026-11-02（评审 Minor-9）：SELECT→DELETE 之间若有渐进落库写入，裸按
    # 主键删除会静默丢掉刚写进来的证据。加行锁（PG 生效；SQLite 忽略）把
    # 「判证据 + 删」放回同一临界区。
    row = (await db.execute(
        select(Message).where(
            Message.id == row_id,
            Message.delivery_status == "streaming",
        ).with_for_update()
    )).scalars().first()
    if row is None:
        return False
    if _has_turn_evidence(row, include_reasoning=False):
        # 2026-11-02（评审 I2）：有证据就不能删——但也**不能留在 streaming**。
        # 留在在途态意味着：UI 全部列表路径都过滤它（用户以为丢了）、下一次
        # 启动 sweep 才翻 interrupted（延迟的空气泡），而且它会进模型上下文。
        # 与 sweep 同源判据的正确收口是「当场翻 interrupted」，不是「等重启」。
        row.delivery_status = "interrupted"
        return False
    await db.delete(row)
    return True

async def _persist_deathmatch_failure(
    conversation_id: str,
    deathmatch_mgr,
    reason: str,
    message: str,
    *,
    expected_agent_state=None,
) -> None:
    """Persist a deathmatch goal-loop failure as a visible paused terminal
    state so the user sees WHY the loop stopped and can resume.

    conv 6b0faf81: the detached agent task crashed mid-loop (no terminal
    message, deathmatch_status left "active"); only the GET reconcile
    flipped it to "paused" ~18 minutes later with no explanation. Any
    exception/cancel in the goal loop must leave an explicit, visible,
    resumable state instead of a silent zombie.

    Safety guards (A4.9 review 2026-08-07):
    - NEVER clobber a deliberate terminal state: done / human_gate /
      partial_complete carry structured semantics (gate reports, partial
      summaries, deliverables) that a later exception in the same task's
      finalization must not overwrite.
    - When ``expected_agent_state`` is given, only persist if the registry
      still holds OUR run (identity check). A superseded task (its slot
      re-reserved by a newer /chat/stream) must not flip the conversation to
      paused after the new run already set it active.
    """
    if deathmatch_mgr is None:
        return
    try:
        if expected_agent_state is not None:
            from app.services.active_agent_registry import ActiveAgentRegistry
            _reg = ActiveAgentRegistry.get_instance()
            _cur = await _reg.get(conversation_id)
            if _cur is not expected_agent_state:
                logger.info(
                    "Deathmatch failure persist skipped: agent state superseded for %s",
                    conversation_id,
                )
                return
        async with AsyncSessionLocal() as _fail_db:
            _fail_conv = await _fail_db.get(Conversation, conversation_id)
            if _fail_conv is None or not _fail_conv.deathmatch_mode:
                return
            if _fail_conv.deathmatch_status in ("done", "human_gate", "partial_complete"):
                return
            _fail_conv.deathmatch_status = "paused"
            _fail_conv.deathmatch_reason = reason
            _fail_conv.updated_at = datetime.utcnow()
            _fail_msg = Message(
                conversation_id=conversation_id,
                role="assistant",
                content=message,
            )
            _fail_db.add(_fail_msg)
            await _fail_db.commit()
            logger.info(
                "Deathmatch failure persisted (conv=%s): status=paused reason=%s",
                conversation_id, reason[:120],
            )
    except Exception:
        logger.exception("Failed to persist deathmatch failure state for %s", conversation_id)

async def _delete_message_slice(db: AsyncSession, slice_rows, user_id: str | None = None) -> None:
    """按 id 重查 ORM 实例逐行删除消息切片——不自带 commit。

    2026-10-31（UPSTREAM_TODO_20261031 P0）：旧实现按 id Core 批量 DELETE，绕过
    SQLAlchemy mapper 事件——sync_capture 收不到 after_delete，编辑重发/重新生成
    零 delete 事件、零墓碑，其它设备永久残留被删消息（conv d7ea65b8 空回答事故）。
    改为按 id 重查 ORM 行逐行 `db.delete`：after_delete → `_emit` 自动产生
    `messages/delete` 事件 + 墓碑，形状与全站一致（conversation.py 批量删除会话
    的既有范式）。

    2026-10-04 conv 4e159a79 教训保留：`slice_rows` 可能是 resolve_message_fields
    对外置 stub 行返回的 SimpleNamespace 拷贝，直接 `db.delete(拷贝)` 抛
    UnmappedInstanceError→500——**按 id 重新查询 ORM 行**同时避开拷贝与事件旁路。
    带 conversation_id 过滤，防脏 slice 跨会话误删；显式 `flush()` 保证 delete 事件
    seq 早于调用方随后插入的新 user 消息 create（对端不得短暂见新旧并存）；flush 不
    提交，payload 行仍随 messages FK ON DELETE CASCADE 清理。

    事务边界：**编辑路径**由调用方与新用户消息插入同一次 commit（其间失败回滚即
    完整恢复，见 test_trim_edit_defers_commit_atomic_replace）；**regenerate 路径**
    的删除随调用方 sort_order 提交点落库——替代的助手回复由 agent 循环在流中晚于
    提交生成，生成失败时旧答案不恢复（删先于建为该架构固有时序，旧实现同序且更早
    提交，本波不改，见 tests/decisions.md D-q6）。
    """
    ids = list(dict.fromkeys(
        str(row.id) for row in slice_rows if getattr(row, "id", None)
    ))
    if not ids:
        return
    _cid = next(
        (getattr(row, "conversation_id", None) for row in slice_rows
         if getattr(row, "conversation_id", None)),
        None,
    )
    # 重查 ORM 实例逐行删除（分块 IN 防 PG 绑定上限 65535）：delete 事件/墓碑由
    # mapper after_delete 在同事务发射；streaming 行由 _emit 的 streaming 守卫抑制。
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        stmt = select(Message).where(Message.id.in_(chunk))
        if _cid:
            stmt = stmt.where(Message.conversation_id == _cid)
        for row in (await db.execute(stmt)).scalars().all():
            await db.delete(row)
    # 显式 flush：UOW 可能把新 user 消息 INSERT 排在 pending DELETE 之前，create 的
    # seq 低于 delete；最终状态仍对，但对端会短暂看到「新消息 + 旧消息」并存。
    await db.flush()
    # P1 S5: any message-prefix deletion invalidates the anchored rolling
    # summary / resume pack; otherwise edit/regenerate re-injects stale state.
    if _cid:
        from app.services.context_continuity_service import invalidate_continuity
        await invalidate_continuity(db, str(_cid))
    if user_id:
        # BUG-B（2026-10-09 用户报告「编辑/重发后新回答仍称我已经…」）：DB 切片
        # 删除正确，但 subconscious_log 已摄入的「用户+助手各 500 字」原始单元不随
        # 消息删除失效（调度器 15min 水位摄入），检索层随即将已删轮次重放进新尝试
        # 的上下文。source_ids 记录来源消息 id（JSON 文本）——按引用同源 purge，
        # LIKE '%"{id}"%' 引号定界防子串误伤；仅同用户，file_memory/无关行不动。
        # 与消息删除同事务（不自带 commit），回滚即完整恢复。
        from app.db.database import SubconsciousLog
        from sqlalchemy import or_ as _or
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            await db.execute(
                sa_delete(SubconsciousLog).where(
                    SubconsciousLog.user_id == user_id,
                    _or(*[SubconsciousLog.source_ids.like(f'%"{mid}"%') for mid in chunk]),
                )
            )
        # 检索会话缓存（5min TTL，key=最近 3 user query 哈希）：regenerate 同 key
        # 命中会重放删除前的记忆上下文——随切片删除一起失效。进程内字典操作，
        # 失败不阻断删除主路径。
        try:
            from app.services.memory_retrieval_service import invalidate_session_cache_for_user
            invalidate_session_cache_for_user(user_id)
        except Exception:
            logger.debug("invalidate_session_cache_for_user failed for user=%s", user_id, exc_info=True)
