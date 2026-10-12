# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""W16 设备执行模式 — 客户端执行器设备登记/配对/令牌（云端侧）。

设计见 docs/PLAN_device_execution_mode.md §6.2/§8：
- 配对码：云端签发（绑定 user_id）、一次性、短 TTL；换取长效设备 token。
- token：仅明文回传一次；云端只存 sha256 hash。
- 与 SyncDevice（sync 红线）独立；撤销即踢线拒新调用。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.db.database import ClientExecutorDevice

_DEVICE_TARGET_RE = re.compile(r"^device:[A-Za-z0-9_-]{4,64}$")
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def validate_rel_path(path: str) -> str:
    """workspace 相对路径硬化（A4.9 I-3/W19 I-2 共用校验）：双分隔符切段、盘符/UNC
    拒绝、`..`/空段/控制字符/尾点尾空格拒绝、长度上限。违规抛 ValueError。"""
    if not path or len(path) > 1024:
        raise ValueError("非法路径")
    if _CTRL_RE.search(path) or "\\" in path or ":" in path or path.startswith("~"):
        raise ValueError("仅允许 workspace 相对路径")
    if path.startswith("/") or path.startswith("//"):
        raise ValueError("仅允许 workspace 相对路径")
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError("仅允许 workspace 相对路径")
    if any(seg.endswith(".") or seg.endswith(" ") for seg in segments):
        raise ValueError("仅允许 workspace 相对路径")
    return path

# 防猜解（A4.9 I-1）：单码错试上限 + 全局失败窗口限速 + 在途码上限
MAX_CODE_ATTEMPTS = 5
MAX_OUTSTANDING_CODES_PER_USER = 3
_FAIL_WINDOW_SECONDS = 60
_FAIL_WINDOW_LIMIT = 60


@dataclass
class _PairingRecord:
    user_id: str
    expires_at: float
    used: bool = False
    attempts: int = 0


_pairing_codes: Dict[str, _PairingRecord] = {}
_fail_log: list = []  # (ts,) 失败时间戳（全局窗口）


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_device_token() -> Tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_device_token(token)


def _gc_expired_codes() -> None:
    now = time.time()
    for code in [c for c, r in _pairing_codes.items() if r.expires_at <= now]:
        _pairing_codes.pop(code, None)


def _throttled() -> bool:
    now = time.time()
    _fail_log[:] = [ts for ts in _fail_log if now - ts < _FAIL_WINDOW_SECONDS]
    return len(_fail_log) >= _FAIL_WINDOW_LIMIT


def issue_pairing_code(user_id: str, ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
    if ttl_seconds is None:
        ttl_seconds = get_config().client_exec_pairing_code_ttl_seconds
    _gc_expired_codes()
    # 在途码上限：超出即作废该用户最旧的未用码（A4.9 I-1：切 10^8/N 猜解面）
    outstanding = sorted(
        (r for r in _pairing_codes.values() if not r.used and r.user_id == user_id),
        key=lambda r: r.expires_at,
    )
    for r in outstanding[: max(0, len(outstanding) - MAX_OUTSTANDING_CODES_PER_USER + 1)]:
        r.used = True
    code = f"{secrets.randbelow(10**8):08d}"
    while code in _pairing_codes:
        code = f"{secrets.randbelow(10**8):08d}"
    _pairing_codes[code] = _PairingRecord(
        user_id=user_id, expires_at=time.time() + ttl_seconds
    )
    return {"code": code, "expires_in_seconds": ttl_seconds}


def consume_pairing_code(code: str, user_id: str) -> Optional[str]:
    rec = _pairing_codes.get(code)
    # <=（非 <）：同一时钟刻度下 ttl<=0 立即过期，避免 time.time() 同值竞态
    if rec is None or rec.used or rec.expires_at <= time.time():
        _fail_log.append(time.time())  # 未知/过期/已用码=猜解尝试
        return None
    if rec.user_id != user_id:
        # 错归属=猜解尝试：计次 + 全局限速（A4.9 I-1）
        rec.attempts += 1
        _fail_log.append(time.time())
        if rec.attempts >= MAX_CODE_ATTEMPTS:
            rec.used = True
        return None
    if _throttled():
        return None
    rec.used = True
    return rec.user_id


def normalize_execution_target(raw: Optional[str]) -> Optional[str]:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not _DEVICE_TARGET_RE.match(raw):
        raise ValueError("execution_target 须为 device:<id> 或留空")
    return raw


async def register_device(
    db: AsyncSession,
    *,
    code: str,
    name: Optional[str] = None,
    platform: Optional[str] = None,
    device_id: Optional[str] = None,
) -> Tuple[ClientExecutorDevice, str]:
    """配对码换长效设备 token。返回 (设备行, 明文 token——仅此一次)。

    user 归属由配对码携带（PLAN §8.1：码由用户 JWT 签发、绑定 user_id），
    设备侧不提供也不需要任何用户凭据。"""
    rec = _pairing_codes.get(code)
    if rec is None:
        # A4.9 r2 I-1：未知码=猜解主路径，必须进失败窗（否则限速永不生效）
        _fail_log.append(time.time())
        raise PermissionError("配对码无效或已过期")
    if _throttled():
        raise PermissionError("尝试过于频繁，请稍后再试")
    user_id = consume_pairing_code(code, rec.user_id)
    if user_id is None:
        raise PermissionError("配对码无效或已过期")
    if device_id is not None and not _DEVICE_ID_RE.match(str(device_id)):
        raise PermissionError("device_id 格式非法")
    token, token_hash = generate_device_token()
    row = ClientExecutorDevice(
        user_id=user_id,
        device_id=device_id or secrets.token_hex(16),
        name=(name or "client-device")[:255],
        platform=platform[:40] if platform else None,
        token_hash=token_hash,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, token


async def verify_device_token(db: AsyncSession, token: str) -> Optional[ClientExecutorDevice]:
    if not token:
        return None
    token_hash = hash_device_token(token)
    result = await db.execute(
        select(ClientExecutorDevice).where(
            ClientExecutorDevice.token_hash == token_hash,
            ClientExecutorDevice.revoked.is_(False),
        )
    )
    return result.scalar_one_or_none()


async def list_devices(db: AsyncSession, user_id: str) -> list:
    result = await db.execute(
        select(ClientExecutorDevice)
        .where(ClientExecutorDevice.user_id == user_id)
        .order_by(ClientExecutorDevice.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_device(db: AsyncSession, user_id: str, device_id: str) -> bool:
    result = await db.execute(
        select(ClientExecutorDevice).where(
            ClientExecutorDevice.device_id == device_id,
            ClientExecutorDevice.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False
    row.revoked = True
    await db.commit()
    return True


async def assert_device_usable(
    db: AsyncSession, user_id: str, execution_target: str
) -> ClientExecutorDevice:
    """execution_target=device:<device_id> → 属主+未撤销校验（IDOR fail-closed）。"""
    target = normalize_execution_target(execution_target)
    if target is None:
        raise ValueError("execution_target 为空")
    device_id = target.split(":", 1)[1]
    result = await db.execute(
        select(ClientExecutorDevice).where(
            ClientExecutorDevice.device_id == device_id,
            ClientExecutorDevice.user_id == user_id,
            ClientExecutorDevice.revoked.is_(False),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise LookupError("设备不存在或已解绑")
    return row
