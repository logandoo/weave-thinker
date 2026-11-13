# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""`/api/auth/register` 与 `/api/auth/login` 的应用层速率限制（2026-11-11 补救清单 #2）。

背景（`docs/AUDIT_20261111_pr1_admin_gate.md` §3 I-1）：注册接口此前没有任何限流。
新增的邀请码是一道**秘密门**，没有退避的开放接口等于允许在线高速枚举该秘密。

设计要点
--------
- **键 = 客户端 IP**（`request.client.host`，与 `auth.py` 记录 `last_login_ip` 同源）。
  本模块**绝不读 `X-Forwarded-For`**：它由客户端自填，可任意伪造。但 `request.client.host`
  本身是否可信取决于部署面——uvicorn 的 `--proxy-headers` 会用 XFF 改写它。因此只有
  「反向代理确实覆写该头 + `--forwarded-allow-ips` 只列代理地址」时才可开启；
  `docker/entrypoint.sh` 自 2026-11-11 起默认**不**启用 proxy headers（此前默认 `*`，
  会让限流被一个请求头绕过、也能被伪造成任意受害者 IP 做定向锁定——评审 B Critical）。
- **两道窗，键的粒度不同**：
  - *尝试窗*（洪泛保护）按**来源 IP**，在 FastAPI 依赖里判定——超了直接 429，不进 handler；
  - *失败窗*（枚举刹车）在 handler 入口判定（只有那里拿得到用户名），且**分两级**：
    ① 按「来源 IP + 账号」——拦「同一来源反复试同一账号」，同事在共享出口上输错自己的
    口令不会连累别人；② 按**来源 IP**（`_IP_CEILING_BUCKETS` 的伴生桶，默认 60 次/15 分钟）
    ——拦「同一来源换着账号喷洒」。缺了 ②，攻击者用不同用户名就能把每账号配额乘成无限。
- **拒绝即 429 + `Retry-After`，且不进入 handler**——被挡的请求零建号、零 DB 写。
- **内存有界**：每个桶的键空间用 LRU 上限（默认 8192，见 `DEFAULT_MAX_KEYS` 的算术）
  封顶，限流器自身不能变成新的 DoS 面。
- **per-process 边界**：状态在进程内。当前部署为单 uvicorn 进程（`scripts/start.sh`
  未开 `--workers`），配额即全局；若将来多 worker，每进程各持一份配额（等效于阈值
  ×worker 数），需要全局一致时再换共享后端。
- 未声明的桶名 = 不限流（fail-open）。桶名只来自代码里的字面量，配置写错桶名不会
  把全站锁死，只会退化为不限流——安全可用性优先。
- **已知取舍**：失败窗的键含用户名 ⇒ 键空间理论上无界，但键只由「通过尝试窗的请求」
  生成，且被 LRU 上限封顶；跨 IP 的分布式口令喷洒不在本层职责内（那需要按账号维度的
  风控，属于后续独立设计）。
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, Dict, Optional, Tuple

from fastapi import HTTPException, Request, status

from app.core.config import AUTH_RATE_LIMIT_IP_CEILING_BUCKETS as _IP_CEILING_BUCKETS
from app.core.config import get_config

logger = logging.getLogger(__name__)

RATE_LIMIT_DETAIL = "操作过于频繁，请稍后再试"

# 每个桶最多跟踪多少个键。上限的算术：单键的两次窗时间戳各自被配额封顶
# （尝试数 ≤ attempts_per_window；失败数 ≤ failures_per_window + 1——撞墙后 429
# 直接返回，不再 note_failure），于是单键最坏 ~140 个 float ≈ 4 KB；8192 键 ≈
# 32 MB 上界、现实用量 < 1 MB。失败窗的键含账号（`IP\x00账号`），单源也能填表，
# 但每个填充键都必须先通过尝试窗（成本 = 8192 次付费尝试），且 LRU 淘汰只影响
# 精度不影响安全方向。
DEFAULT_MAX_KEYS = 8192

# bucket -> (attempts_per_window, window_seconds, failures_per_window, failure_window_seconds)
# 任一配额写成 0 = 关掉那道窗（只剩另一道生效）。
Rule = Tuple[float, float, float, float]


def _prune(events: Deque[float], now: float, window: float) -> None:
    cutoff = now - window
    while events and events[0] <= cutoff:
        events.popleft()


class _KeyState:
    __slots__ = ("attempts", "failures")

    def __init__(self) -> None:
        self.attempts: Deque[float] = deque()
        self.failures: Deque[float] = deque()


class AuthRateLimiter:
    """滑动窗口（时间戳日志）限流器。所有方法都是同步的，在单个 asyncio 事件循环内
    天然原子——内部没有 await，不存在「检查」与「记账」之间被别的请求插入的竞态。"""

    def __init__(
        self,
        rules: Dict[str, Rule],
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self._rules: Dict[str, Rule] = {}
        for bucket, rule in rules.items():
            attempts, window, failures, failure_window = rule
            self._rules[bucket] = (
                float(attempts), float(window), float(failures), float(failure_window),
            )
        self._clock = clock
        self._max_keys = max(1, int(max_keys))
        self._state: Dict[str, "OrderedDict[str, _KeyState]"] = {
            bucket: OrderedDict() for bucket in self._rules
        }

    def adopt_state_from(self, other: "AuthRateLimiter") -> None:
        """配置热重载后**保留**仍在的桶的窗口计数（D-50）。

        改阈值不应把攻击者已累积的失败计数清零（那等于给攻击者一次免费重置），
        也不应让正常用户的配额凭空恢复。只接管新规则里仍然存在的桶。
        """
        for bucket, table in other._state.items():
            if bucket not in self._state:
                continue
            self._state[bucket] = table
            while len(table) > self._max_keys:
                table.popitem(last=False)

    def key_count(self, bucket: str) -> int:
        table = self._state.get(bucket)
        return len(table) if table is not None else 0

    def _state_for(self, bucket: str, key: str) -> _KeyState:
        table = self._state[bucket]
        state = table.get(key)
        if state is None:
            state = _KeyState()
            table[key] = state
            while len(table) > self._max_keys:
                table.popitem(last=False)  # LRU：最久未触碰的键先出局
        else:
            table.move_to_end(key)
        return state

    def check(self, bucket: str, key: str) -> Optional[float]:
        """**尝试窗**：放行则记账这次尝试并返回 ``None``；被拒则返回建议的
        ``Retry-After`` 秒数。键是客户端 IP——这一层只做洪泛保护。

        被拒的尝试**不**记账——否则持续撞门的攻击会把封禁无限延长，
        而「窗口内已用完配额」这件事本身已经由既有事件表达过了。
        """
        rule = self._rules.get(bucket)
        if rule is None:
            return None
        attempts_limit, window, _failures_limit, _failure_window = rule
        now = self._clock()
        state = self._state_for(bucket, key)
        _prune(state.attempts, now, window)

        if attempts_limit > 0 and len(state.attempts) >= attempts_limit:
            return max(1.0, state.attempts[0] + window - now)

        state.attempts.append(now)
        return None

    def failure_retry_after(self, bucket: str, key: str) -> Optional[float]:
        """**失败窗**（只读判定）：返回 ``None``=未触顶；否则建议的 ``Retry-After``。

        与尝试窗分开是因为键的粒度不同：尝试窗按来源 IP（洪泛保护），失败窗按
        「来源 IP + 被尝试的账号」（枚举刹车）。后者必须在 handler 里做——只有那里
        才拿得到用户名；也正因如此，同事在共享出口上输错自己的口令**不会**连累别人。

        只读：不存在的键直接返回 ``None``，不建键（建键会挤占 LRU 表）。
        """
        rule = self._rules.get(bucket)
        if rule is None:
            return None
        _attempts_limit, _window, failures_limit, failure_window = rule
        if failures_limit <= 0:
            return None
        state = self._state.get(bucket, {}).get(key)
        if state is None:
            return None
        now = self._clock()
        _prune(state.failures, now, failure_window)
        if len(state.failures) >= failures_limit:
            return max(1.0, state.failures[0] + failure_window - now)
        return None

    def note_failure(self, bucket: str, key: str) -> None:
        """记一次「认证类失败」（401 凭据错 / 403 注册关闭或邀请码无效）。"""
        if bucket not in self._rules:
            return
        state = self._state_for(bucket, key)
        state.failures.append(self._clock())


_limiter: Optional[AuthRateLimiter] = None
_limiter_rules: Optional[Dict[str, Rule]] = None
_limiter_pinned = False


def get_auth_rate_limiter() -> AuthRateLimiter:
    """进程级限流器。规则跟着配置走：`POST /api/admin/reload-config` 之后
    `clear_config_cache()` 会让 `auth_rate_limit_rules` 变样，若单例永不重建，
    运维改了阈值却毫无效果（认证接口 QPS 天然低，每请求做一次规则比对可以接受）。

    `set_auth_rate_limiter(x)` 会**固定**实例（测试注入语义），传 None 解除固定。"""
    global _limiter, _limiter_rules, _limiter_pinned
    if _limiter is not None and _limiter_pinned:
        return _limiter
    rules = get_config().auth_rate_limit_rules
    if _limiter is None or rules != _limiter_rules:
        previous = _limiter
        _limiter = AuthRateLimiter(rules)
        if previous is not None:
            # D-50：改阈值不清零已有窗口计数（否则每次调档都给攻击者一次免费重置）
            _limiter.adopt_state_from(previous)
        _limiter_rules = rules
    return _limiter


def set_auth_rate_limiter(limiter: Optional[AuthRateLimiter]) -> None:
    """注入/清空进程级单例：传实例=固定（测试用），传 None=解除固定并按配置重建。"""
    global _limiter, _limiter_rules, _limiter_pinned
    _limiter = limiter
    _limiter_pinned = limiter is not None
    if limiter is None:
        _limiter_rules = None


def client_ip(request: Request) -> str:
    """认证接口的限流键。只信 socket 对端，不信任何请求头。"""
    host = request.client.host if request.client else None
    return (host or "unknown").strip()[:64]


class AuthHeadroom:
    """依赖返回值：handler 用它在拒绝分支上记失败、在入口处查失败窗。"""

    __slots__ = ("_limiter", "_bucket", "_key")

    def __init__(self, limiter: Optional[AuthRateLimiter], bucket: str, key: str) -> None:
        self._limiter = limiter
        self._bucket = bucket
        self._key = key

    def failure_key(self, subject: str = "") -> str:
        """失败窗的键：``<ip>\\x00<账号>``。账号为空（注册）时退化为纯 IP。

        分隔符用 NUL：HTTP 头值与用户名都不可能含它，因此不存在「构造用户名去撞
        别人的键」的拼接碰撞（评审 B Minor）。
        """
        subject = (subject or "").strip()[:64]
        return f"{self._key}\x00{subject}" if subject else self._key

    def _deny(self, retry_after: float, subject: str) -> None:
        logger.warning(
            "auth rate limit (failures): bucket=%s client=%s subject=%r retry_after=%.0fs",
            self._bucket, self._key, subject, retry_after,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMIT_DETAIL,
            headers={"Retry-After": str(int(math.ceil(retry_after)))},
        )

    def guard_failure(self, subject: str = "") -> None:
        """失败窗触顶则直接 429（在凭据校验/建号之前，零副作用）。

        两道判定：
        1. **按账号**（`IP|账号`）——拦「同一来源反复试同一账号」；
        2. **按来源**（`_IP_CEILING_BUCKETS` 的伴生桶，仅当 subject 非空）——拦
           「同一来源换着账号喷洒」。缺了第 2 道，攻击者用不同用户名就能把每账号
           配额乘成无限（评审 A/B 复核共同指出的回归）。
        """
        if self._limiter is None:
            return
        retry_after = self._limiter.failure_retry_after(self._bucket, self.failure_key(subject))
        if retry_after is None and subject:
            ceiling = _IP_CEILING_BUCKETS.get(self._bucket)
            if ceiling:
                retry_after = self._limiter.failure_retry_after(ceiling, self._key)
        if retry_after is not None:
            self._deny(retry_after, subject)

    def note_failure(self, subject: str = "") -> None:
        if self._limiter is None:
            return
        self._limiter.note_failure(self._bucket, self.failure_key(subject))
        ceiling = _IP_CEILING_BUCKETS.get(self._bucket) if subject else None
        if ceiling:
            self._limiter.note_failure(ceiling, self._key)


def rate_limited(bucket: str):
    """FastAPI 依赖工厂：``headroom: AuthHeadroom = Depends(rate_limited("register"))``。"""

    async def _dependency(request: Request) -> AuthHeadroom:
        if not get_config().auth_rate_limit_enabled:
            return AuthHeadroom(None, bucket, "")
        limiter = get_auth_rate_limiter()
        key = client_ip(request)
        retry_after = limiter.check(bucket, key)
        if retry_after is not None:
            logger.warning(
                "auth rate limit: bucket=%s client=%s retry_after=%.0fs", bucket, key, retry_after
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=RATE_LIMIT_DETAIL,
                headers={"Retry-After": str(int(math.ceil(retry_after)))},
            )
        return AuthHeadroom(limiter, bucket, key)

    return _dependency
