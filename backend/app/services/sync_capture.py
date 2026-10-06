# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""同步变更捕获（S1 波，2026-09-24；Joplin delta 同步的服务端事件源）。

机制选型（Design Gate A 裁决）：SQLAlchemy mapper 持久化事件
（after_insert/after_update/after_delete）+ 同事务 connection.execute 写
sync_events（delete 时附带 sync_tombstones）。事件在 flush 内逐对象触发，
与本仓全 ORM 写路径同覆盖；bulk DML（update()/delete() 语句）不触发 mapper
事件——实测同步域（conversations/messages/notes/notebooks/assistants）无 bulk
写路径（唯一 bulk 点 asr.py UserAsrHotword 已随全量保真波入同步域
SYNC_ENTITIES（user_asr_hotwords）且其写路径为逐行 ORM + delete+create，
无 bulk update 缺口），故 mapper 覆盖面无缺口。
⚠ 已知捕获缺口（后续项，非 mapper 面）：memory 域**后台维护批处理**仍走
Core-SQL/text() 写同步列（scheduler 驱动的 weight_decay/consolidation/dreaming/
backfill/migration/embedding 刷新，~30 点清单见 sync 收口波记录）——零事件零
墓碑；用户可达路径（memory.py forget/delete/delete_all、clarification
negate/forget、召回内联 try_cold_resurrect）已 ORM 化恢复捕获（余留清零波
2026-10-02）。本 docstring 的「无缺口」断言仅限 mapper 覆盖面。

origin 回显抑制：push 端点应用客户端变更前经 `set_origin_device()` 置
ContextVar，捕获写入 sync_events.origin_device；delta 携带 device_id 时过滤
同源事件（推送者已持有该状态，无需回传）。
"""
import contextvars
import logging
import uuid
from datetime import datetime

from sqlalchemy import event, inspect as sa_inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.database import (
    Assistant,
    Conversation,
    ConversationGroup,
    MemoryConcept,
    MemoryEpisode,
    Message,
    Note,
    Notebook,
    SkillFile,
    SyncEvent,
    SyncTombstone,
    User,
    UserAsrHotword,
    UserModelProvider,
    UserSkill,
)

logger = logging.getLogger(__name__)

# entity_type（= 表名，Consistency Hub 事实源）→ ORM 模型
SYNC_ENTITIES = {
    "conversations": Conversation,
    "messages": Message,
    "notes": Note,
    "notebooks": Notebook,
    "assistants": Assistant,
    "conversation_groups": ConversationGroup,
    "user_asr_hotwords": UserAsrHotword,
    "user_skills": UserSkill,
    "skill_files": SkillFile,
    "user_model_providers": UserModelProvider,
}

# 记忆域（S5）：默认纳入捕获/应用（全量保真波口径），[sync] memory_enabled=false 可关；向量每设备本地重算
MEMORY_SYNC_ENTITIES = {
    "memory_episodes": MemoryEpisode,
    "memory_concepts": MemoryConcept,
}

# 嵌入本地态列——按名排除（载荷净化的名称腿；类型腿见 row_snapshot 的 Vector 判型）
_EMBED_STATE_COLS = {"embedding", "embedding_model", "embedding_updated_at"}

# 记忆动态列（S5 评审 I1 裁决）：weight/activation_strength/recurrence_count/
# hot_forget_count/last_recalled_at = 每设备本地计算态（召回/衰减/做梦管线维护），
# 同步=跨设备 LWW 互相覆盖本地动态——剥离；内容列（narrative/canonical_name/
# importance/stability/status/valid_*/participants/locations）照常同步。
MEMORY_DYNAMICS_COLS = {
    "weight", "activation_strength", "recurrence_count",
    "hot_forget_count", "last_recalled_at",
}

# user_profile 白名单（全量保真波）：users 自行仅四列可同步
PROFILE_COLS = ("nickname", "avatar_data", "ui_preferences", "agent_permissions")

_origin_device: contextvars.ContextVar = contextvars.ContextVar(
    "sync_origin_device", default=None
)

_registered = False
_LISTENERS: list = []  # (model, event_name, fn) 引用表——M1 升级：支持测试反注册


def set_origin_device(device_id):
    """push 端点在应用客户端变更前调用（返回 token，用后 reset_origin_device）。"""
    return _origin_device.set(device_id)


def reset_origin_device(token) -> None:
    _origin_device.reset(token)


# D-16（2026-09-27 用户原则「全一致」逆转 S1-I2）：api_key 类列随同步
# （两端自托管+TLS）；向量/嵌入本地态仍排除（本地重算）。


def row_snapshot(target) -> dict:
    """全行快照：列名→值；datetime→ISO 字符串（Z 后缀声明 UTC）；嵌入本地态列排除（api_key 类自 D-16 起随同步）；Vector 类型列与嵌入本地态列排除（S5：向量每设备本地重算）。"""
    out = {}
    for col in sa_inspect(target).mapper.columns:
        if col.name in _EMBED_STATE_COLS:
            continue
        if col.name in MEMORY_DYNAMICS_COLS:
            continue
        if type(col.type).__name__ == "Vector":  # pgvector 列永不入载荷（类型腿）
            continue
        v = getattr(target, col.name)
        if isinstance(v, datetime):
            v = v.isoformat() + "Z"  # naive UTC 列显式声明时区（仓序列化约定）
        elif isinstance(v, uuid.UUID):
            v = str(v)
        out[col.name] = v
    return out


def _resolve_user_id(connection, entity_type: str, target):
    """事件归属：conversations/notebooks/assistants 直取 user_id；
    messages/notes 经父表单行 SELECT（级联删除时子行先删、父行尚在同一事务内可见）。"""
    if entity_type in ("conversations", "notebooks", "assistants",
                       "conversation_groups", "user_asr_hotwords", "user_skills",
                       "user_model_providers"):
        return getattr(target, "user_id", None)
    if entity_type == "user_profile":
        return getattr(target, "id", None)  # users 自行
    if entity_type == "skill_files":
        skill_id = getattr(target, "skill_id", None)
        if not skill_id:
            return None
        row = connection.execute(
            select(UserSkill.user_id).where(UserSkill.id == skill_id)
        ).first()
        return row[0] if row else None
    if entity_type in MEMORY_SYNC_ENTITIES:
        return getattr(target, "user_id", None)
    if entity_type == "messages":
        conv_id = getattr(target, "conversation_id", None)
        if not conv_id:
            return None
        row = connection.execute(
            select(Conversation.user_id).where(Conversation.id == conv_id)
        ).first()
        return row[0] if row else None
    if entity_type == "notes":
        nb_id = getattr(target, "notebook_id", None)
        if not nb_id:
            return None
        row = connection.execute(
            select(Notebook.user_id).where(Notebook.id == nb_id)
        ).first()
        return row[0] if row else None
    return None


def _emit(connection, entity_type: str, op: str, target) -> None:
    # 运行时开关（A4.9 I3）：捕获在发射期读配置而非仅启动期注册门——SIGHUP
    # 热改 [sync].enabled 时捕获与端点（请求期 _sync_enabled）同进同退。
    from app.core.config import get_config

    config = get_config()
    if not config.sync_enabled:
        return
    # 渐进落库（2026-10-06）：streaming 在途行不参与同步下发（周期刷写会造成
    # LWW 噪音风暴）；翻 terminal（final/interrupted）后由当次提交正常发射。
    if entity_type == "messages":
        if getattr(target, "delivery_status", "final") == "streaming":
            return
        if op == "update":
            # A4.9 r1 B#1 + scoped 复审 Critical：streaming 期 create 被跳过，
            # 终态翻转的 update 对端无行可命中（update-without-create = 跨设备
            # 丢失）。op 词表是 create/update/delete（sync_apply 对 append-only
            # messages 只认 create，其余 SKIPPED）——终态翻转必须按 "create"
            # 发射（对端无行→幂等插入；有行→SKIPPED，正是 append-only 语义）。
            op = "create"
    # 记忆域独立开关（S5）：默认捕获（全量保真波口径），[sync] memory_enabled=false 可关
    if entity_type in MEMORY_SYNC_ENTITIES and not config.sync_memory_enabled:
        return
    user_id = _resolve_user_id(connection, entity_type, target)
    if not user_id:
        logger.warning(
            "sync_capture: skip %s %s — owner unresolved", entity_type, op
        )
        return
    entity_id = getattr(target, "id", None)
    if not entity_id:
        return
    if entity_type == "user_profile":
        snap = row_snapshot(target)
        payload = {k: snap.get(k) for k in (*PROFILE_COLS, "updated_at")}
    elif op == "delete" and entity_type == "user_model_providers":
        # I4（评审）：删除载荷带自然键——双端独立建行的同 (kind,provider) 行 id 不同，
        # 按 id 删不到对端自然键归并行；应用侧据此兜底
        payload = {"kind": getattr(target, "kind", None), "provider": getattr(target, "provider", None)}
    else:
        payload = None if op == "delete" else row_snapshot(target)
    connection.execute(
        SyncEvent.__table__.insert().values(
            user_id=user_id,
            entity_type=entity_type,
            entity_id=str(entity_id),
            op=op,
            payload=payload,
            origin_device=_origin_device.get(),
            created_at=datetime.utcnow(),
        )
    )
    if op == "delete":
        connection.execute(
            pg_insert(SyncTombstone.__table__)
            .values(
                id=str(uuid.uuid4()),
                user_id=user_id,
                entity_type=entity_type,
                entity_id=str(entity_id),
                deleted_at=datetime.utcnow(),
            )
            .on_conflict_do_nothing(constraint="uq_sync_tombstone")
        )


def register_sync_capture() -> None:
    """幂等注册 mapper 事件监听器（main.py startup 调用一次）。"""
    global _registered
    if _registered:
        return
    # 监听器引用登记（M1 升级）：支持 unregister_sync_capture 测试隔离——
    # 内联 lambda 无法被 event.remove 定位，故统一经 _add_listener 收集。
    def _add_listener(target, name: str, fn) -> None:
        event.listen(target, name, fn)
        _LISTENERS.append((target, name, fn))

    # user_profile：users 自行仅四列净变更才发事件（last_login 等不触发）
    def _user_after_update(mapper, connection, target):
        state = sa_inspect(target)
        if not any(
            getattr(state.attrs, c).history.has_changes() for c in PROFILE_COLS
        ):
            return
        _emit(connection, "user_profile", "update", target)

    _add_listener(User, "after_update", _user_after_update)
    # 记忆域监听一并注册（发射期按 [sync] memory_enabled 逐事件门控，SIGHUP 安全）
    for entity_type, model in {**SYNC_ENTITIES, **MEMORY_SYNC_ENTITIES}.items():
        _add_listener(
            model,
            "after_insert",
            lambda m, c, t, et=entity_type: _emit(c, et, "create", t),
        )
        _add_listener(
            model,
            "after_update",
            lambda m, c, t, et=entity_type: _emit(c, et, "update", t),
        )
        _add_listener(
            model,
            "after_delete",
            lambda m, c, t, et=entity_type: _emit(c, et, "delete", t),
        )
    _registered = True
    logger.info(
        "sync_capture registered for %d entity types (+%d memory-gated)",
        len(SYNC_ENTITIES), len(MEMORY_SYNC_ENTITIES),
    )


def unregister_sync_capture() -> None:
    """整体摘除监听器并复位幂等标志（测试隔离专用，生产不调用）。

    M1 升级：监听器经 _LISTENERS 持引用，event.remove 可精确定位；
    反注册后 re-register 幂等标志复位，可重新注册。"""
    global _registered
    for target, name, fn in _LISTENERS:
        event.remove(target, name, fn)
    _LISTENERS.clear()
    _registered = False
