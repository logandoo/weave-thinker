# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""同步应用层（S1 服务端 push 与 S2 客户端 pull 共用的 LWW/归属/净化语义）。

事实源：server/API.md 二.13。任何一侧应用同步变更都必须经 apply_change，
保证两端仲裁规则逐字节一致（否则双端会得出不同的胜出版本）。
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import (
    Assistant,
    Conversation,
    ConversationGroup,
    Message,
    Note,
    Notebook,
    User,
    UserModelProvider,
)
from app.services.sync_capture import MEMORY_DYNAMICS_COLS, MEMORY_SYNC_ENTITIES, SYNC_ENTITIES

DIRECT_USER_TYPES = {"conversations", "conversation_groups", "notebooks", "assistants",
                     "memory_episodes", "memory_concepts",
                     "user_asr_hotwords", "user_skills", "user_model_providers"}
APPEND_ONLY_TYPES = {"messages"}
# 服务端权威/敏感列——同步载荷一律不携带（入端丢弃）
EXCLUDE_PAYLOAD_COLS = {"embedding", "embedding_model", "embedding_updated_at",
                        *MEMORY_DYNAMICS_COLS}
# 记忆域 create 必填守卫（评审 M3：防手造缺字段事件 flush 失败成噪音）
REQUIRED_ON_CREATE = {
    "memory_episodes": ("narrative", "source_unit_ids"),
    "memory_concepts": ("canonical_name", "description_short"),
}

APPLIED = "applied"
SKIPPED = "skipped"          # 幂等/规则拒绝（非错误）
STALE = "stale"              # LWW 败方（incoming.updated_at 不严格大于现有行）


def parse_dt(value) -> Optional[datetime]:
    """ISO 字符串 → naive UTC datetime（列均为 naive；aware 输入归一化）。"""
    if value is None or isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


PROFILE_COLS = ("nickname", "avatar_data", "ui_preferences", "agent_permissions")


def sanitize_payload(entity_type: str, model, payload: dict) -> dict:
    """payload → 可入库字段：仅保留模型列；id/user_id 服务端权威；datetime 归一；
    敏感/运行态列排除（EXCLUDE_PAYLOAD_COLS + deathmatch_*）。"""
    columns = model.__table__.columns
    data = {}
    for key, value in (payload or {}).items():
        if entity_type == "user_profile" and key not in (*PROFILE_COLS, "updated_at"):
            continue
        if key not in columns or key in ("id", "user_id"):
            continue
        if key in EXCLUDE_PAYLOAD_COLS or key.startswith("deathmatch_"):
            continue
        if type(columns[key].type).__name__ == "Vector":  # pgvector 列类型腿
            continue
        # datetime 按列类型判定（非名称后缀——valid_from/valid_to 等不以 _at
        # 结尾的 DateTime 列会被快照携带，名称腿会漏判导致 asyncpg 绑型错误）
        if isinstance(columns[key].type, DateTime):
            value = parse_dt(value)
        data[key] = value
    return data


async def owns(db: AsyncSession, user: User, entity_type: str, row) -> bool:
    if entity_type == "user_profile":
        return getattr(row, "id", None) == user.id
    if entity_type in DIRECT_USER_TYPES:
        return getattr(row, "user_id", None) == user.id
    if entity_type == "notes":
        nb = await db.get(Notebook, row.notebook_id)
        return bool(nb and nb.user_id == user.id)
    if entity_type == "messages":
        conv = await db.get(Conversation, row.conversation_id)
        return bool(conv and conv.user_id == user.id)
    return False


async def parents_owned(db: AsyncSession, user: User, entity_type: str, data: dict) -> bool:
    if entity_type == "user_profile":
        return True  # 归属由 entity_id==user.id 前置校验
    if entity_type == "skill_files" and "skill_id" in data:
        from app.db.database import UserSkill

        skill = await db.get(UserSkill, data["skill_id"])
        return bool(skill and skill.user_id == user.id)
    """create/update 中新父引用归属校验（跨租户重父化防护）。"""
    if entity_type == "notes" and "notebook_id" in data:
        nb = await db.get(Notebook, data["notebook_id"])
        return bool(nb and nb.user_id == user.id)
    if entity_type == "messages" and "conversation_id" in data:
        conv = await db.get(Conversation, data["conversation_id"])
        return bool(conv and conv.user_id == user.id)
    if entity_type == "conversations":
        if data.get("assistant_id"):
            assistant = await db.get(Assistant, data["assistant_id"])
            if not assistant or assistant.user_id != user.id:
                return False
        if data.get("group_id"):
            group = await db.get(ConversationGroup, data["group_id"])
            if not group or group.user_id != user.id:
                return False
    return True


async def apply_change(
    db: AsyncSession,
    user: User,
    entity_type: str,
    entity_id: str,
    op: str,
    payload: Optional[dict],
) -> str:
    """应用单条同步变更（upsert + LWW + 归属校验）。返回 APPLIED/SKIPPED/STALE。

    LWW：update（及 create 撞已有行）要求 incoming updated_at 严格大于现有行，
    否则 STALE；行不存在时按 upsert 插入（delta/resync 两侧语义一致）。
    messages 追加型：create 幂等插入，update 恒 SKIPPED，delete 存在即删。
    delete：存在即删（时间戳不参与），不存在 SKIPPED。
    """
    if entity_type == "user_profile":
        # users 自行四列 LWW（无 create/delete）。entity_id 携带的是来源实例的
        # 本地用户 id——跨部署必然不同，校验身份由端点鉴权（push/pull 均按
        # 认证用户隔离）承担，不比 id（全量保真波 iter 2 根因：比对即恒 skip）。
        if op != "update":
            return SKIPPED
        row = user
        new_ts = parse_dt((payload or {}).get("updated_at"))
        if new_ts is None:
            return SKIPPED
        # I3（评审）：LWW 时钟不能读 users.updated_at（onupdate 被登录 churn 污染
        # ——每次登录都推进它，后续资料同步恒 STALE）。改用本端 profile 事件流的
        # 最近载荷时间戳（仅资料四列变更才发事件=干净的资料版本钟）。
        from app.db.database import SyncEvent as _SE

        # N1（复评）：事件流时钟取载荷 updated_at 时间线（created_at 是本地
        # 应用墙钟，rx: 应用会污染 LWW 仲裁——跨机时钟偏斜下静默丢编辑）。
        last_ev_raw = (
            await db.execute(
                select(func.max(_SE.payload["updated_at"].as_string())).where(
                    _SE.user_id == user.id,
                    _SE.entity_type == "user_profile",
                )
            )
        ).scalar()
        current_ts = parse_dt(last_ev_raw) if last_ev_raw else None
        if current_ts is None:
            current_ts = row.updated_at  # 首事件前退回行时钟（新设备冷启动）
        if current_ts is not None and new_ts <= current_ts:
            return STALE
        data = sanitize_payload(entity_type, User, payload or {})
        for key, value in data.items():
            if key != "updated_at":
                setattr(row, key, value)
        row.updated_at = new_ts
        await db.flush()
        return APPLIED
    model = SYNC_ENTITIES.get(entity_type)
    if model is None and entity_type in MEMORY_SYNC_ENTITIES:
        # 记忆域按 [sync] memory_enabled 门控（S5；两端同一开关语义）
        from app.core.config import get_config

        if not get_config().sync_memory_enabled:
            return SKIPPED
        model = MEMORY_SYNC_ENTITIES[entity_type]
    if model is None:
        return SKIPPED
    row = await db.get(model, entity_id)

    if op == "delete":
        if row is None and entity_type == "user_model_providers" and payload:
            # I4：按自然键兜底定位（双端独立建行 id 不同步）
            row = (
                await db.execute(
                    select(UserModelProvider).where(
                        UserModelProvider.user_id == user.id,
                        UserModelProvider.kind == payload.get("kind", ""),
                        UserModelProvider.provider == payload.get("provider", ""),
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return SKIPPED
        if not await owns(db, user, entity_type, row):
            return SKIPPED
        await db.delete(row)
        await db.flush()
        return APPLIED

    data = sanitize_payload(entity_type, model, payload or {})
    # 缺父拒绝（Minor 收口）：notes/messages 无父键不可归属（owner 无法解析）——
    # 与 S1 重构前语义对齐，防孤儿行/IntegrityError 毒事件
    if entity_type == "notes" and not data.get("notebook_id") and row is None:
        return SKIPPED
    if entity_type == "messages" and not data.get("conversation_id") and row is None:
        return SKIPPED
    # 记忆域 create 必填守卫
    required = REQUIRED_ON_CREATE.get(entity_type)
    if required and row is None and any(not data.get(k) for k in required):
        return SKIPPED
    if not await parents_owned(db, user, entity_type, data):
        return SKIPPED

    if entity_type in APPEND_ONLY_TYPES:
        if op != "create":
            return SKIPPED
        if row is not None:
            return SKIPPED
        data["id"] = entity_id
        obj = model(**data)
        db.add(obj)
        await db.flush()
        return APPLIED

    # LWW 域（conversations/notes/notebooks/assistants）
    new_ts = parse_dt((payload or {}).get("updated_at"))
    if row is None and entity_type == "user_model_providers":
        # 自然键 upsert：双端独立配置同 (kind,provider) 行 id 不同——按自然键归并
        # （全量保真波；唯一键 uq_user_model_providers_user_kind_provider）
        from sqlalchemy import select as _select

        existing = (
            await db.execute(
                _select(UserModelProvider).where(
                    UserModelProvider.user_id == user.id,
                    UserModelProvider.kind == data.get("kind", ""),
                    UserModelProvider.provider == data.get("provider", ""),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            row = existing
    if row is None:
        if op not in ("create", "update"):
            return SKIPPED
        data["id"] = entity_id
        if entity_type in DIRECT_USER_TYPES:
            data["user_id"] = user.id
        obj = model(**data)
        db.add(obj)
        await db.flush()
        return APPLIED
    # 已有行：create 撞车按 update 语义走 LWW（同 UUID 双端独立创建）
    if new_ts is None:
        return SKIPPED
    current_ts = getattr(row, "updated_at", None)
    if current_ts is not None and new_ts <= current_ts:
        return STALE
    if not await owns(db, user, entity_type, row):
        return SKIPPED
    for key, value in data.items():
        if key != "updated_at":
            setattr(row, key, value)
    row.updated_at = new_ts
    await db.flush()
    return APPLIED
